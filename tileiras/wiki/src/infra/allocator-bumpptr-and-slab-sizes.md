# Allocator + BumpPtr + Slab Sizes

## Abstract

The `tileiras` binary leans on three intertwined allocator layers — a generic `malloc`-retry shim, a bump-pointer arena following LLVM's `BumpPtrAllocator::Allocate` contract, and a per-`MLIRContextImpl` lattice of fixed-size slab requests that fan out into the `StorageUniquer` hash-cons machinery. Together they explain why the dominant fixed-size allocations land on a small number of well-known C++ class sizes: each is the byte image of a published LLVM 18 / MLIR storage record, and every one reconciles against an upstream `sizeof()` in LLVM 18 (which the producer string `a2100git` independently dates the binary to, modulo the LLVM 21 development tag the bitcode loader reports).

The layers are documented in the order a `getOrCreate` call visits them: the `SDNode`-shaped `BumpPtrAllocator::Allocate` wrapper, the four pattern-object slab sizes, the 24/96-byte `Region` / `Block` strides, the custom MLIR allocation wrappers, and the per-`MLIRContextImpl` arena that owns all of the above. Cross-links: `infra/data-section-decryption.md` and the `StorageUniquer` page for the 88-byte `StorageAllocator` slot allocated atop this stack.

## BumpPtrAllocator allocate wrapper

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

## MLIRContextImpl arena ownership

Everything above sits inside the `MLIRContextImpl` arena, which owns the `StorageUniquer` Level-1 bucket table. Each bucket can publish an 88-byte `StorageAllocator` containing a per-TypeID `pthread_rwlock_t`, live/tombstone counters, a bucket count, and a pointer to the Level-2 storage table. `MLIRContextImpl` retains every `StorageAllocator`, and each allocator retains every `BaseStorage` it hands out, so the arena lifetime is tied to a single `MLIRContext`. When the context dies, every interned `Type`, `Attribute`, `Location`, `Identifier`, `AffineExpr`, and pattern object allocated through this stack is reclaimed through the per-TypeID destructor table. The MLIR `BumpPtrAllocator` proper lives inside the same context as a separate slab list for operation records and trailing objects.

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
