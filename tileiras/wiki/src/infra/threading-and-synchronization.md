# Threading and Synchronization

## Abstract

The `tileiras` binary links against `libpthread` and uses the standard POSIX threading primitives — `pthread_once_t`, `pthread_mutex_t`, `pthread_rwlock_t` — together with compare-exchange and atomic add/sub operations on reference-count fields. Concurrency surfaces in three distinct layers: process-wide one-shot initialization of decoded data tables and cached TypeID singletons; per-`MLIRContext` locking of type, attribute, and affine-map uniquer state; and the lock-free publish fast path inside `mlir::detail::StorageUniquer::getOrCreate`. Single-threaded builds collapse the same paths to plain loads and stores through LLVM's weak-threading gates.

This page catalogues the primitive families the binary actually uses and the contracts they protect. The corresponding allocator, refcount, and hash-cons layers live elsewhere — see [Allocator BumpPtr and Slab Sizes](allocator-bumpptr-and-slab-sizes.md) for the arena and `StorageAllocator` shape that the per-TypeID `pthread_rwlock_t` below protects, and [Data Section Decryption](data-section-decryption.md) for the only `pthread_once` use that decodes a binary-time-encrypted pool rather than building runtime state.

## pthread_once one-shot gates

`pthread_once` serves as a process-wide "run exactly once, all other callers wait" guard in three structural roles. First, data-table decoding: PTX mnemonic and register-name pools decode lazily the first time the NVPTX printer asks for them. Second, cached TypeID construction: per-type StorageUniquer shims build their TypeID once, then future callers skip construction and go straight to lookup. Third, dialect and pass registration: dialect initialization and pass registration are once-gated so concurrent module creation sees a fully populated registry.

The Itanium ABI guard pair `__cxa_guard_acquire` / `__cxa_guard_release` handles smaller static-local byte guards. Its practical contract matches `pthread_once`: initialize once, publish only after construction completes, make later calls read-only.

## pthread_mutex_t and pthread_rwlock_t inside MLIRContextImpl

`MLIRContextImpl` owns the synchronization objects for type, attribute, and affine-expression interning. Type and attribute uniquers use 16-byte bucket slots plus size words; the affine map/expression path has its own mutex and state pointer.

| Field | Primitive | Protects |
|---|---|---|
| `affine_uniquer_mutex` | `pthread_mutex_t` | AffineMap, AffineExpr, and IntegerSet Level-1 insert path. |
| `affine_uniquer_state` | state pointer | Pointer to the affine-cluster `StorageUniquerImpl`. |
| `type_uniquer_buckets / size` | bucket pointer plus size | Per-context Type interning table. |
| `attr_uniquer_buckets / size` | bucket pointer plus size | Per-context Attribute interning table. |

Each per-TypeID `StorageAllocator` published into the Level-1 bucket array owns a `pthread_rwlock_t` for its Level-2 table. Probes take the read lock. Inserts release the read lock, take the write lock, then re-probe before allocating, because another writer may have inserted the same key between the two locks. That read-then-write structure is the core StorageUniquer concurrency contract.

## Atomic CAS in the StorageUniquer fast path

`StorageUniquer::getOrCreate` has one lock-free fast path: a compare-exchange publishes a freshly allocated `StorageAllocator` into the Level-1 bucket for an unseen `TypeID`. The loser of a CAS race frees its allocation and continues with the winner. No lock-free Level-2 insertion path exists; inner table insertions, resizes, and tombstone rehash decisions all serialize through the per-TypeID rwlock. The per-thread TLS cache that fronts the Level-2 probe is writer-private and needs neither CAS nor a lock.

## ThreadSafeRefCountedBase: three `int32` refcount fields

Every interned storage object follows the canonical `llvm::ThreadSafeRefCountedBase` shape: a strong count, a weak count, and an installed-in-cache marker. Strong increments fire when a storage object is handed to a caller; strong decrements run the payload deleter when the count reaches zero. Weak decrements run the final destructor when the weak count reaches zero. Threaded builds use atomic add/sub; single-threaded builds collapse the same code to ordinary integer updates through LLVM's weak-threading gate.

## Reimplementation Notes

```text
get_or_create(type_id, key):
    allocator = atomic_load(level1[type_id])
    if allocator == null:
        candidate = allocate_storage_allocator(type_id)
        allocator = compare_exchange_or_free_loser(level1[type_id], candidate)

    with allocator.read_lock:
        existing = allocator.lookup(key)
        if existing:
            return retain(existing)

    with allocator.write_lock:
        existing = allocator.lookup(key)
        if existing:
            return retain(existing)
        return allocator.insert_new_storage(key)
```

The re-probe under the write lock is mandatory. Without it, two racing inserts can create distinct storage objects for the same uniquing key.
