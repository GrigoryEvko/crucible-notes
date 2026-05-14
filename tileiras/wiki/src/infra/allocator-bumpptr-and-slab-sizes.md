# Allocator + BumpPtr + Slab Sizes

## Abstract

The `tileiras` binary leans on three intertwined allocator layers — a generic `malloc`-retry shim, a bump-pointer arena following LLVM's `BumpPtrAllocator::Allocate` contract, and a per-`MLIRContextImpl` lattice of fixed-size slab requests that fan out into the `StorageUniquer` hash-cons machinery. Together they explain why the dominant fixed-size allocations land on a small number of well-known C++ class sizes: each is the byte image of a published LLVM 18 / MLIR storage record, and every one reconciles against an upstream `sizeof()` in LLVM 18 (which the producer string `a2100git` independently dates the binary to, modulo the LLVM 21 development tag the bitcode loader reports).

The layers are documented in the order a `getOrCreate` call visits them: the `SDNode`-shaped `BumpPtrAllocator::Allocate` wrapper, the four pattern-object slab sizes, the 24/96-byte `Region` / `Block` strides, the custom MLIR allocation wrappers, and the per-`MLIRContextImpl` arena that owns all of the above. Cross-links: `infra/data-section-decryption.md` and the `StorageUniquer` page for the 88-byte `StorageAllocator` slot allocated atop this stack.

## BumpPtrAllocator vs Allocator

Two allocator types sit in the storage stack and they are not interchangeable. `BumpPtrAllocator` is LLVM's raw forward-only arena — a singly-linked list of slabs, a current-slab pointer, and a high-water mark within the current slab. Every `Allocate(size, count)` call carves the next aligned region out of the current slab, advances the high-water mark, and returns the carved pointer; when the slab fills, the allocator allocates a fresh slab from the next size class. There is no per-object free path: deallocation happens once at allocator destruction, when every slab is released in one pass.

`Allocator` (the per-`MLIRContext` allocator the `StorageUniquer` owns) wraps a `BumpPtrAllocator` and adds three responsibilities. First, it picks a slab-size class for the request based on the requested allocation size and the alignment requirement of the result type. Second, it routes oversized allocations — anything larger than the largest fixed slab class — through a separate large-allocation path that allocates a dedicated slab for the single object. Third, it tracks per-TypeID allocation counts so the `StorageAllocator` can decide when to grow its Level-2 hash table.

The two-layer split matters for reimplementation. The lower-level `BumpPtrAllocator` is the cold-path workhorse: callers that don't need slab-class selection (the bytecode reader, the dialect-registration walker, the diagnostic-message formatter) call into it directly. The upper-level `Allocator` is the hot-path entry point for the storage uniquer, because every interned type, attribute, and affine expression has to pick a slab class matched to its `sizeof` and natural alignment.

## BumpPtrAllocator Allocate Wrapper

The bump-pointer wrapper follows LLVM's `BumpPtrAllocator::Allocate(size, count)` contract. It computes a header area proportional to the element count, delegates the actual allocation to the retrying allocator, then initializes fixed 32-byte slot metadata before returning the usable pointer. Two allocation shapes identify the LLVM layout family: 72-byte `SDNode` records and 88-byte `GlobalVariable` records. Both sizes align with LLVM 18 object layouts, even though the linked toolchain otherwise carries the later LLVM development tag used by CUDA 13.1.

## Pattern object slab sizes

Four pattern-object footprints dominate fixed-size requests. Each is the `sizeof()` of an `OpRewritePattern<T>` / `ConversionPattern<T>` subclass after `RewritePattern` base inflation (vtable ptr + `PatternBenefit` + op-name SmallVector + `MLIRContext*` + any custom members).

| Slab size | Site count | Upstream identity                                                          | When you see it |
|-----------|-----------:|----------------------------------------------------------------------------|-----------------|
| `0x60`    |        286 | `RewritePattern` base (vtable + 80 B inherited + 16 B benefit/tag tail)    | cuda_tile / nv_tileaa canonicalisers, dialect-internal folds |
| `0x68`    |        201 | `OpRewritePattern<T>` + one inline `Value`/`Type` member                   | typed canonicalisers that retain one dispatch handle |
| `0x70`    |        902 | `ConversionPattern<T>` (adds `TypeConverter*` to the `0x60` base)          | every `*-to-LLVM` / `*-to-NVVM` lowering, dominant slab |
| `0x78`    |         66 | conversion pattern + one inline storage member (e.g. layout, `IntegerSet`) | conversion patterns that carry a layout, set, or address-space tag |

The `0x70` slab dominates because every dialect-to-NVVM lowering instantiates `ConvertOpToLLVMPattern<T>` or `OpConversionPattern<T>`, both 112 B after inheritance flattening (vtable + 80 B `RewritePattern` + 24 B `ConversionPattern` extension carrying the `TypeConverter*` plus padding). Second place `0x60` is plain `RewritePattern` (no type converter), used by the cuda_tile canonicalisers. The `0x68` and `0x78` slabs differ from their `0x60` / `0x70` neighbours by exactly one trailing 8-byte member — typically an `Attribute` handle or a `TypeID` retained for dispatch — matching upstream's practice of stashing dispatch keys inline rather than chasing them through the op-name SmallVector.

## MLIR Region / Block strides

Walkers assume two fixed strides for the IR backbone. `mlir::Region` is 24 bytes: one `Operation*` parent, one `Block` ilist sentinel, and an 8-byte tail flag word. `mlir::Block` is 96 bytes in the LLVM 18 layout: IList header, `BlockArgument` vector, operation-list sentinel, region back-pointer, and successor/predecessor vectors. Every region/block walker either steps a contiguous `Region` array by 24 bytes or follows a `Block::next` ilist link with no stride assumption.

## Custom MLIR alloc wrappers

Two helpers wrap raw allocation for MLIR-specific contracts:

- The `malloc`-retry trampoline implements LLVM's allocation contract: zero-byte requests are rounded up to one byte, failed allocations invoke the active `new_handler`, and fixed-size slabs bottom out in the same path.
- `InterfaceMap::insert` keeps `(TypeID, void*)` pairs sorted by TypeID. It binary-searches the 16-byte-strided vector, appends or shifts elements right, frees duplicate implementation pointers, and delegates growth to LLVM's aligned buffer allocator.

## Hot-Path Subsystem Mapping

The dominant slab classes (`0x60` / `0x68` / `0x70` / `0x78`) account for nearly every fixed-size allocation in steady state, but each class is fed by different subsystems. The mapping below tracks the four hot consumers against the slab class they overwhelmingly land on.

| Subsystem | Primary slab | What lands there |
|---|---|---|
| Dialect-to-NVVM conversion driver | `0x70` (112 B) | every `ConvertOpToLLVMPattern<T>` and `OpConversionPattern<T>` instantiation — one per source op the pass set rewrites |
| cuda_tile / nv_tileaa canonicaliser registry | `0x60` (96 B) | every `RewritePattern` the canonicaliser pass registers — folds, identity removals, swap-operand patterns |
| Scheduler schedule-node allocator | `0x60` / `0x68` | `Schedule::Node` records and the inline `SmallVector`-backed dependence-edge lists each node carries |
| Bytecode reader IR-node array allocator | `0x70` / `0x78` | parsed-IR-node arrays, where the trailing 8-byte slot on `0x78` carries the IR-attribute index back to the dialect |

The slab-class fan-out is what gives the hot path its locality. A bytecode reader that parses a function reads contiguous arrays of `0x70`-shaped records into a single slab; the slab fits in two or three cache lines per ten records, and the prefetcher walks the slab forward naturally. The conversion driver hits the same effect: every `OpConversionPattern<T>` it registers in the order they're requested lands on the same slab, so the dialect-conversion walker's locality is determined entirely by the registration order.

## Alignment Guarantees

Every allocation is aligned to the natural alignment of its slab class. The four hot classes have natural alignments tied to their size: `0x60` and `0x70` are 8-byte aligned (the slab base is page-aligned and each carve is a multiple of 8); `0x68` and `0x78` retain the same 8-byte natural alignment because their trailing 8-byte member is a regular qword. Oversized allocations that escape to the large-allocation path are page-aligned by default, but a caller can request a stricter alignment (16, 32, or 64 bytes for vector types) by passing an explicit alignment to the `Allocate` call.

The slab-class selection algorithm is structurally simple: take the requested allocation size, round up to the next multiple of 8, look up the smallest slab class whose size is at least the rounded-up request *and* whose natural alignment satisfies the caller's required alignment, and carve from that class. The lookup is a four-entry compare-and-branch chain; the binary does not use a fancier data structure because the per-allocation cost has to beat the cost of the allocation itself.

There is one subtle alignment-related constraint. The `StorageUniquer`'s Level-2 hash table hashes on `BaseStorage*`, and the lower three bits of every `BaseStorage*` are used as a tag for the storage-kind discriminator. If the slab-class natural alignment were less than 8 bytes, the discriminator bits would overlap with the address bits and the hash would produce false collisions. The 8-byte natural alignment of every slab class is therefore not an optimisation — it is a correctness requirement for the storage tagging.

## Lifetime

`BumpPtrAllocator` has exactly one lifetime: it is freed all-at-once at context destruction. There is no per-object free path, no garbage collection cycle, no reference-counted release. The slabs are allocated forward over the lifetime of the `MLIRContext` and released in one pass when the context's destructor runs.

The consequence for the storage uniquer is that an interned `Type`, `Attribute`, `Location`, or `AffineExpr` is reachable as long as the `MLIRContext` is alive. The `ThreadSafeRefCountedBase` refcount on each storage object still tracks live references, but the refcount reaching zero does not free the storage — it only signals that the per-TypeID destructor table can run the payload deleter to release any heap-allocated state the storage owns (a `std::string` body, a non-inline `SmallVector` heap, etc.). The 808-byte / 96-byte / `sizeof()` portion of the storage object itself stays in its slab until the context dies.

The bytecode-reader IR-node arrays and the scheduler's schedule-node structs follow the same lifetime rule — they live inside the same arena and are freed in the same one-pass slab release. The dialect-conversion driver's `ConvertOpToLLVMPattern<T>` records also live in the arena because their construction happens during pass registration and they survive for the lifetime of the pass manager that owns them. A reimplementation that tried to free pattern records mid-pass would not just leak the slab — it would break every `PatternBenefit` lookup and every dispatch table that points back into the slab.

## MLIRContextImpl Arena Ownership

Everything above sits inside the `MLIRContextImpl` arena, which owns the `StorageUniquer` Level-1 bucket table. Each bucket can publish an 88-byte `StorageAllocator` containing a per-TypeID `pthread_rwlock_t`, live/tombstone counters, a bucket count, and a pointer to the Level-2 storage table. `MLIRContextImpl` retains every `StorageAllocator`, and each allocator retains every `BaseStorage` it hands out, so the arena lifetime is tied to a single `MLIRContext`. When the context dies, every interned `Type`, `Attribute`, `Location`, `Identifier`, `AffineExpr`, and pattern object allocated through this stack is reclaimed through the per-TypeID destructor table. The MLIR `BumpPtrAllocator` proper lives inside the same context as a separate slab list for operation records and trailing objects.

The concurrency story for this arena lives in [Threading and Synchronization](threading-and-synchronization.md): every per-TypeID `StorageAllocator` carries the `pthread_rwlock_t` that protects its Level-2 probe and insert, and the `MLIRContextImpl`'s diagnostic-printer mutex serialises the failure-path messages a slab-exhaustion event would produce.

## Reimplementation Notes

```text
allocate_storage(kind, payload_size):
    size = max(payload_size, 1)
    ptr = malloc_retry(size)
    initialize_storage_header(ptr, kind)
    return ptr

get_or_create_storage(context, type_id, key):
    allocator = context.storage_uniquer.lookup_or_publish_allocator(type_id)
    with allocator.write_lock_if_insert_needed(key):
        return allocator.lookup_or_insert(key)
```

Arena ownership is fundamental: individual MLIR storage records are never freed piecemeal during normal compilation. They are reclaimed when the owning `MLIRContext` is destroyed.
