# Common Compiler Patterns and Idioms

## Abstract

Tileiras uses a small set of recurring structural patterns drawn from upstream MLIR, LLVM, and libstdc++. Once a reader can spot them, hundreds of pages of architecture collapse to a handful of moves repeated across every subsystem: a public handle with one `void *` to a per-pass state struct, a hand-rolled vtable table with two fixed sentinel slots, a switch on a tag byte that drives every parse and conversion boundary, a single bit in a status word that ferries pass failure across passes, an Itanium guard byte that gates every first-use initialiser, a 24-byte string with the small-string mode encoded in its last byte, a stack-buffered vector whose overflow is one pointer indirection, and an `alloca`-style allocation that packs a header and several trailing arrays into one block.

This page is the pattern catalogue. Each entry describes the shape, the canonical recognition fingerprint, and the wiki page that documents the pattern in production. A reader who internalises the catalogue reads any other page faster: the structural moves are already named, and the semantic story is what remains to learn.

## PIMPL State Objects

A public class holds one pointer named `*self` (sometimes `_impl`, sometimes a field with no separate name). The actual state lives in a heap-allocated struct whose layout is fixed across one subsystem and known at every call site. Each TileAS pass extends the base layout with its own fields; the total size lands somewhere between 0x150 and 0x3C0 bytes depending on the pass.

```c
struct PassObject {
    /*+0x00*/ MLIRContext     *context;            // first slot is always the owning context
    /*+0x08*/ DiagnosticEngine *engine;            // shared with every other pass
    /*+0x10*/ void             *analysis_manager;
    /*+0x18*/ void             *pass_manager;
    /*+0x20*/ void             *options_blob;
    /*+0x28*/ uint32_t          status_word;       // bit 2 = soft failure (see below)
    /*+0x2C*/ uint32_t          opt_level;
    /*+0x30*/ /* pass-specific fields, sized to round the object to 0x150..0x3C0 */
};
```

The fixed prefix at `+0x00..+0x28` is the cross-pass contract: the context pointer for every accessor, the engine for diagnostics, the analysis and pass managers for inter-pass lookup, and the status word that carries the failure handshake. Pass-specific state — options, caches, temporary maps — lives in the trailing area and never appears in cross-pass code.

Recognition is one instruction: any function whose first argument is loaded with `[rdi]` to read an `MLIRContext *` is operating on a PassObject. Pages that lean on this shape include [TileAS Pass-Failure Handshake](../mlir-infra/pass-failure-handshake.md) and [Pass Manager Internals](../pipeline/pass-manager-internals.md).

## Vtable Banks

LLVM disables RTTI through `-fno-rtti`. Polymorphism is hand-rolled: every class declares a static array of function pointers; instances hold a pointer-to-the-array in their first slot. Subclasses provide their own array. Two array shapes dominate tileiras: an 8-slot vtable for `OpConversionPattern`-style classes and a 5-slot vtable for plain `RewritePattern`.

```c
static const PatternVtable kArithGenericOpPatternVtable = {
    /*+0x00*/ &typeinfo_arith_GenericOpPattern,    // RTTI helper
    /*+0x08*/ &delete_pattern_object,              // deleting destructor
    /*+0x10*/ &sub_36C8EC0,                        // non-deleting destructor (invariant body)
    /*+0x18*/ &nullsub_11937,                      // empty trait callback (invariant slot)
    /*+0x20*/ &get_debug_name,                     // returns typeinfo string
    /*+0x28*/ &match,                              // may stub to slot 6
    /*+0x30*/ &match_and_rewrite,                  // the rewrite body
    /*+0x38*/ &get_dependent_operation_names,      // returns generatedOps SmallVector
};
```

Slot 2 and slot 3 are invariant: `sub_36C8EC0` for the non-deleting destructor body, `nullsub_11937` at `0x447F250` for the empty trait callback. That pair is the strongest fingerprint for an 8-slot pattern vtable in stripped code. The 5-slot vtable has no empty-trait slot and no dependent-operation accessor; slot 3 is the rewrite body, not a stub. The two shapes are catalogued in [Pattern Vtables and Shapes](../mlir-infra/pattern-vtables-and-shapes.md) and [Binary Vtable Banks and Static Constructors](../infra/binary-vtable-banks-and-static-ctors.md).

## Dispatcher Tables

A large `switch` on a tag value appears at every parse and conversion boundary. The compiler lowers dense ranges to a jump table; sparse cases stay as compares. The shape is the same everywhere: read one byte, switch over it, call a handler.

The principal dispatchers in tileiras:

| Dispatcher                       | Cases | Where                                        |
|----------------------------------|-------|----------------------------------------------|
| MLIR bytecode `OpTag` reader     | 110   | dialect-by-dialect op-tag table              |
| MLIR bytecode `AttrTag` reader   | 13    | wire-format-breaking vs upstream's 17        |
| AsmWriter MC instruction print   | ~6400 | one case per NVPTX backend opcode            |
| AsmPrinter non-MMA partition     | 18    | one case per non-tensor-core op family       |
| `cute_nvgpu` mnemonic switch     | 64    | one case per atom family                     |

```c
Op read_op(BytecodeReader *r) {
    uint8_t tag = read_byte(r);
    switch (tag) {                              // jump table dense over [0..N]
        case OP_RETURN:        return parse_return(r);
        case OP_BRANCH:        return parse_branch(r);
        case OP_CALL:          return parse_call(r);
        /* ... 107 more cases ... */
        default:               return parse_extension(r, tag);
    }
}
```

Recognition is a function with a jump table at its head; the table itself sits in `.rodata` and is referenced by an indirect `jmp`. Pages that catalogue the per-table contents include [MLIR Bytecode Format](../bytecode/mlir-bc-format.md) and [AsmPrinter Status](../bytecode/asm-printer-status.md).

## Failure-Bit Handshake

TileAS passes communicate soft failure by ORing `4` into the status word at offset `+0x28` of their PassObject. The bit signals "this pass could not complete its work, but the IR remains valid and the pipeline should continue." The pass manager reads the bit when the walk terminates; downstream passes that depend on the predecessor read the same bit and either short-circuit or run a fallback.

```c
static inline void pass_mark_soft_failure(PassObject *self) {
    self->status_word |= 4;             // *(self+0x28) |= 4
}

static inline bool pass_soft_failed(const PassObject *self) {
    return (self->status_word & 4) != 0;
}
```

The pattern is `*(self+40) |= 4` in disassembly — a 32-bit `OR` of an immediate `4` into the dword at `+0x28`. The bit always pairs with a diagnostic: the pass emits its error or remark first, sets the bit second, and returns. The convention is documented in full in [TileAS Pass-Failure Handshake](../mlir-infra/pass-failure-handshake.md); the broader three-layer error story is in [Error Handling and Diagnostics](error-handling-and-diagnostics.md).

## Lazy-Init Guards

First-use initialisation of singletons uses one of two guard families. The Itanium ABI `__cxa_guard_acquire` / `__cxa_guard_release` pair gates function-local statics; `pthread_once` gates larger pool decodings and dialect registrations.

```c
static const Pool *cached_pool;
static atomic<uint64_t> pool_guard;                 // Itanium guard byte in low bit

const Pool *get_pool(void) {
    if (__cxa_guard_acquire(&pool_guard)) {         // returns nonzero on first call
        cached_pool = build_pool();                 // runs exactly once
        __cxa_guard_release(&pool_guard);           // publishes through release fence
    }
    return cached_pool;                             // every subsequent call: plain load
}
```

The acquire load and release store form a release-acquire pair: subsequent threads see the initialised state without an extra fence. The low bit of the guard byte encodes "initialised"; uncontended subsequent calls inline to a single byte load and a branch. The `pthread_once` form is the equivalent for larger init work — the threading machinery, the spin-vs-block trade-off, and the weak-symbol single-threaded collapse are catalogued in [Threading and Synchronization](../infra/threading-and-synchronization.md).

## SSO Strings

libstdc++ `std::string` is 24 bytes on x86-64 and stores up to 15 characters inline. In small mode the layout is `{ char *_M_dataplus, size_t _M_string_length, char _M_local_buf[16] }` — the data pointer points into the inline buffer at the end of the same object. In heap mode the same struct stores `{ char *heap_ptr, size_t length, size_t capacity }` and the data pointer points at a separate heap allocation.

```c
struct sso_string {
    /*+0x00*/ char   *data;                         // points to &local_buf in small mode
    /*+0x08*/ size_t  length;
    union {                                         // anonymous union at +0x10
        /*+0x10*/ char   local_buf[16];             // small-string inline storage
        /*+0x10*/ size_t capacity;                  // heap mode capacity
    };
};                                                  // sizeof == 24
```

The discriminator is the data pointer at `+0x00`: if it equals `this + 0x10`, the string is in small mode and the 16 trailing bytes are the inline content; otherwise the string is on the heap and `+0x10` is the capacity. Recognition in a binary is a 24-byte field whose first 8 bytes either point into the same object (small) or point to a separate heap chunk (heap).

## SmallVector

LLVM's `SmallVector<T, N>` carries an inline buffer of N elements directly in the object. When the size exceeds N, the vector spills to a heap allocation and the inline buffer is unused. The layout is `{ T *begin, T *end, T *capacity_end, T inline_buf[N] }` — the same three pointers describe both inline and heap modes; the discriminator is whether `begin` points into the inline buffer or to a separate allocation.

```c
struct SmallVectorBase {
    /*+0x00*/ void *begin;                          // points into inline_buf when small
    /*+0x08*/ void *end;
    /*+0x10*/ void *capacity_end;
    /*+0x18*/ /* inline_buf[N * sizeof(T)] follows */
};
```

The pattern fingerprint is three contiguous pointers followed by a small array, with `begin` either pointing into the same object or to a separate heap buffer. The 0x60-byte pattern prefix described in [Pattern Vtables and Shapes](../mlir-infra/pattern-vtables-and-shapes.md) embeds a `SmallVector<OperationName, 4>` at `+0x38`; the empty-vector marker `0x400000000` in the size word is the inline-storage discriminator for that specific instantiation.

## TypeID Meyers Caches

Every MLIR type, attribute, op, and dialect carries a `TypeID`. The implementation puts a single byte-sized static variable in an anonymous namespace per class; the address of that variable is the `TypeID`. The variable is never written; its address is stable across the whole process lifetime, and `TypeID::get<T>()` returns it.

```c
template <typename T>
struct TypeIDResolver {
    static const char id_storage;                   // never read, only addressed
};

template <typename T>
const char TypeIDResolver<T>::id_storage = 0;

template <typename T>
TypeID TypeID::get(void) {
    return TypeID(&TypeIDResolver<T>::id_storage);  // pointer identity is the type's ID
}
```

The byte itself is irrelevant; the `&` operator and the linker's per-class single-definition guarantee are what produce the unique identity. Recognition in a binary is a one-byte `.rodata` symbol whose only references are address-of in dispatch code. The sentinel records that back the per-op `OperationName` slots at `+0x40` follow the same model and are catalogued in [TypeID Sentinels and Anchors](../mlir-infra/typeid-sentinels-and-anchors.md).

## TrailingObjects

LLVM allocates "header plus variable-length tail arrays" as one block. The allocator returns `sizeof(Header) + sum_of_tails` bytes; the header occupies the leading bytes; each tail array follows at a computed offset. Accessors compute the offset from `this` and the per-field counts stored in the header.

```c
Operation *create_operation(unsigned n_results,
                            unsigned n_operands,
                            unsigned n_regions,
                            unsigned n_successors) {
    size_t bytes = sizeof(Operation)
                 + n_results   * sizeof(OpResult)
                 + n_operands  * sizeof(OpOperand)
                 + n_regions   * sizeof(Region)
                 + n_successors * sizeof(BlockOperand)
                 + sizeof(DictionaryAttr *);
    void *block = arena_alloc(bytes);
    /* placement-new Header at block, then placement-new each trailing run */
    return reinterpret_cast<Operation *>(block);
}
```

The canonical example is the MLIR `Operation` header (0x48 bytes) followed by inline results, operands, regions, successors, and the attribute-dictionary slot, all in one allocation. The seven-line decoder at `sub_4492630` computes the operand base via `(hdr + 16*trailing + 8*n_inline + 64 + 7) & ~7`. Full layout is in [MLIR Operation Layout](../mlir-infra/operation-layout.md).

## Recognising Patterns in Practice

A short workflow for any unfamiliar function:

1. **First argument loaded as `MLIRContext *`** at `[rdi]`? Almost certainly a PIMPL state object; the next 0x28 bytes are the shared prefix.
2. **First field a pointer to `.rodata`** followed by 5 or 8 contiguous function pointers? A vtable bank; check slot 2 against `sub_36C8EC0` and slot 3 against `nullsub_11937` to confirm an 8-slot pattern vtable.
3. **A `switch` with more than 50 cases or a `jmp [table + tag*8]` at function entry**? A dispatcher table; the `.rodata` jump table reveals the case count.
4. **An `OR` of `4` into the dword at `[rdi+0x28]`** preceded by a diagnostic call? A TileAS soft-failure handshake.
5. **`__cxa_guard_acquire` on a byte symbol**, or a `pthread_once_t` global followed by a call to `pthread_once`? A first-use initialiser; the cached value lives in a sibling static.
6. **A 24-byte field whose first qword either points into the same object or into a separate allocation**? A libstdc++ `std::string` in small or heap mode.
7. **Three contiguous pointers followed by an inline array**, with the first pointer optionally pointing back into the array? A `SmallVector` in inline mode.
8. **A one-byte `.rodata` symbol whose only references are address-of**? A `TypeID` resolver storage byte.
9. **An allocation of `sizeof(Header) + N * stride`** followed by pointer arithmetic from `this` to reach trailing arrays? A `TrailingObjects` block.

These nine shapes account for the structural bulk of tileiras's complexity. Anything that does not match one of them is either domain-specific algorithm code (the scheduler, the lattice solvers, the layout algebra) or a one-off helper. Recognising the shape lets a reader skip the bookkeeping and focus on what each function actually computes.

## Cross-References

[Pattern Vtables and Shapes](../mlir-infra/pattern-vtables-and-shapes.md) is the in-depth catalogue of the two vtable shapes summarised above. [MLIR Operation Layout](../mlir-infra/operation-layout.md) is the canonical `TrailingObjects` example. [TileAS Pass-Failure Handshake](../mlir-infra/pass-failure-handshake.md) documents the soft-failure bit. [Threading and Synchronization](../infra/threading-and-synchronization.md) covers the lazy-init guard families. [TypeID Sentinels and Anchors](../mlir-infra/typeid-sentinels-and-anchors.md) catalogues the per-class identity bytes. [Binary Vtable Banks and Static Constructors](../infra/binary-vtable-banks-and-static-ctors.md) shows how the vtable arrays land in the binary at link time. [Error Handling and Diagnostics](error-handling-and-diagnostics.md) ties the failure handshake to the broader diagnostic story.
