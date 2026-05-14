# Threading and Synchronization

## Abstract

The `tileiras` binary links against `libpthread` and uses the standard POSIX threading primitives — `pthread_once_t`, `pthread_mutex_t`, `pthread_rwlock_t`, plus `pthread_key_create` for thread-local storage — together with compare-exchange and atomic add/sub operations on reference-count fields. Concurrency surfaces in four distinct layers: process-wide one-shot initialization of decoded data tables and cached TypeID singletons; thread-local caches that front every type and attribute uniquer probe; per-`MLIRContext` rwlock-protected hash tables that the TLS caches publish into; and the lock-free CAS fast path that publishes a freshly allocated `StorageAllocator` into the per-TypeID bucket array. Single-threaded builds collapse the same paths to plain loads and stores through LLVM's weak-threading gates.

This page is the canonical concurrency story for the whole `MLIRContext` storage stack. The allocator and refcount machinery it interacts with live elsewhere — see [Allocator BumpPtr and Slab Sizes](allocator-bumpptr-and-slab-sizes.md) for the arena and `StorageAllocator` shape that the per-TypeID `pthread_rwlock_t` below protects, and [Data Section Decryption](data-section-decryption.md) for the one `pthread_once` use that decodes a binary-time-encrypted pool rather than building runtime state.

## Primitive Families

Five POSIX primitives carry the concurrency contract; the rest of the binary's threading reduces to atomics on integer fields. Every primitive is paired with a weak-symbol fallback: when the linked libc lacks one of the `_pthread_*` family, the wrapper degenerates to a no-op and the surrounding code runs single-threaded.

| Primitive | Role inside `tileiras` |
|---|---|
| `pthread_mutex_t` | Affine-cluster Level-1 insertion mutex; data-section decode mutex; diagnostic-printer mutex on `MLIRContext`. |
| `pthread_rwlock_t` | Per-TypeID `StorageAllocator` lock — read mode protects the probe, write mode protects insert and resize. |
| `pthread_once_t` | One-shot guards for data-table decode, cached TypeID construction, and dialect / pass registration. |
| `pthread_key_create` / `pthread_getspecific` | TLS keys for the per-thread uniquer cache and the diagnostic-context pointer. |
| Atomic CAS, atomic add/sub | Refcount increments/decrements, `StorageAllocator` publish-or-free, lock-free probe of the Level-1 bucket array. |

The atomic operations compile down to `lock cmpxchg` and `lock xadd` on x86-64 in threaded builds. The weak-threading gate replaces them with plain `mov` plus `inc` / `dec` when the linker resolves `_pthread_key_create` to a null weak symbol — the binary checks that pointer once at startup, caches the result, and dispatches every later concurrency primitive against the cached flag.

## Weak-Symbol Single-Threaded Collapse

The same translation unit that compiles to a thread-safe `MLIRContext` also serves single-threaded builds. The collapse mechanism is a startup check that reads the weak symbol `_pthread_key_create`: if it resolves to `nullptr`, the binary is being run against a libc that does not link the threading library, and every later concurrency primitive in the storage stack short-circuits.

```c
static bool tileiras_threading_enabled(void) {
    // _pthread_key_create is a weak symbol. Its resolved address is the
    // canonical "do we have a real pthreads underneath us?" probe.
    return &_pthread_key_create != nullptr;
}
```

Once that probe returns false, the consequences cascade: the per-TypeID `pthread_rwlock_t` reads and writes become uncontended local loads and stores; the atomic CAS in the `StorageAllocator` publish path becomes a plain pointer write because no other thread can race; the refcount add/sub becomes ordinary integer arithmetic; the TLS cache becomes a single per-process cache. The `MLIRContext` runs end-to-end with no synchronisation overhead and the dialect-conversion driver finishes a kernel-build in roughly the same wall-clock time it would take on a single-threaded LLVM.

The opposite direction is also true: threaded builds never branch on the flag in the hot path. The wrappers compile their atomic and lock instructions unconditionally; the cost of a single uncontended `lock cmpxchg` is too small to justify the branch and the speculation cost. The flag is only read once at startup and at the few places where a destructor needs to know whether to call `pthread_*_destroy` or skip it.

## pthread_once one-shot gates

`pthread_once` serves as a process-wide "run exactly once, all other callers wait" guard in three structural roles. First, data-table decoding: PTX mnemonic and register-name pools decode lazily the first time the NVPTX printer asks for them. Second, cached TypeID construction: per-type StorageUniquer shims build their TypeID once, then future callers skip construction and go straight to lookup. Third, dialect and pass registration: dialect initialization and pass registration are once-gated so concurrent module creation sees a fully populated registry.

The Itanium ABI guard pair `__cxa_guard_acquire` / `__cxa_guard_release` handles smaller static-local byte guards. Its practical contract matches `pthread_once`: initialize once, publish only after construction completes, make later calls read-only.

The two guard families differ in observable behaviour under contention. `pthread_once` blocks every waiter on a futex until the running call completes; the waiters are then released together and each sees the published state. `__cxa_guard_acquire` busy-spins on the guard byte's low bit before falling back to a futex — the spin is short enough that uncontended cases (a single thread initialising a function-local static) never enter the kernel, but contended cases still degenerate to the same futex wait. The choice between them is driven by the size of the initialisation work: pool decoding and dialect registration go through `pthread_once` because the work is large enough that no waiter benefits from spinning; function-local statics go through the Itanium ABI guards because the work is short and the kernel call would dominate.

## Atomic Memory Ordering

The CAS in the `StorageAllocator` publish path uses sequentially-consistent memory ordering. A weaker ordering — release on the store, acquire on the load — would suffice for the storage-uniquer's correctness model, but the binary picks `seq_cst` because the cost of the stronger fence is small (an `mfence` on x86-64 the publishing thread issues once at first observation, never again) and the cost of getting the weaker ordering wrong would be a use-before-construct race that only manifests under specific weak-memory-model behaviours. The atomic add/sub on the refcount fields use `relaxed` ordering for the increment (which only matters for the count itself, not for any data it protects) and `acq_rel` ordering for the decrement that hits zero (which has to synchronise with the destructor's reads of the storage payload).

The lock-free probe of the Level-1 bucket array uses `acquire` ordering on the load: the probing thread has to see every store the publishing thread issued before its publishing CAS. The publishing thread's CAS is `seq_cst` (which is `acq_rel` plus a sequential consistency tail), so the load's `acquire` is sufficient — the bucket pointer's visibility implies the visibility of every store the publishing thread issued to the allocator's fields before the CAS.

## MLIRContextImpl Concurrency Layout

`MLIRContextImpl` owns the synchronization objects for type, attribute, and affine-expression interning. The interning machinery is a two-level hash table: Level 1 is a bucket array keyed by a `TypeID`, where each bucket holds a pointer to a `StorageAllocator`; Level 2 is the per-TypeID hash table that allocator owns, keyed by the parameterised storage key of each interned type or attribute. Type and attribute uniquers use 16-byte bucket slots plus size words; the affine map/expression path has its own mutex and state pointer because affine-cluster interning was bolted on after the original storage uniquer was generic.

| Field | Primitive | Protects |
|---|---|---|
| `affine_uniquer_mutex` | `pthread_mutex_t` | AffineMap, AffineExpr, and IntegerSet Level-1 insert path. |
| `affine_uniquer_state` | state pointer | Pointer to the affine-cluster `StorageUniquerImpl`. |
| `type_uniquer_buckets / size` | bucket pointer plus size | Per-context Type Level-1 bucket array. |
| `attr_uniquer_buckets / size` | bucket pointer plus size | Per-context Attribute Level-1 bucket array. |
| `diagnostic_printer_mutex` | `pthread_mutex_t` | Serialises diagnostic output across worker threads. |

Each per-TypeID `StorageAllocator` published into the Level-1 bucket array owns a `pthread_rwlock_t` for its Level-2 table. The lock has three operational modes: read mode for probes (any number of probes proceed concurrently), write mode for inserts and table resizes, and no-lock mode for the per-thread TLS cache that fronts the probe. The read-then-upgrade pattern is the core StorageUniquer concurrency contract: a probe takes the read lock and looks up the key, and if the key is absent the probe releases the read lock, takes the write lock, *re-probes* (because another writer may have inserted the same key between the two locks), and only then allocates.

## TLS Uniquer Cache

In front of every Level-2 probe sits a per-thread cache: a small hash table keyed by the parameterised storage key and valued by the `BaseStorage*` returned by the last successful probe. The cache is thread-local, accessed through a `pthread_key_create`-registered TLS key, and the read path needs no synchronisation because no other thread can see it. On x86-64 the access compiles to a single `%fs:`-segmented load — the `fs` segment register holds the TLS base for the current thread, and the compiler emits `mov %fs:offset, %reg` for the cache pointer probe.

The cache lives in two layers. The outer layer is a `pthread_getspecific(tls_key)` call that returns the per-thread cache header pointer; the inner layer is the cache header itself, which holds the hash-table buckets and the LRU eviction queue. The outer layer goes through libpthread's TLS mechanism the first time a thread touches the uniquer; subsequent accesses on the same thread short-circuit to the cached header pointer the compiler hoists into a register on entry to the hot probe function. The total cost of a cache hit on the steady-state path is one `mov` from the segment register, one compare, and one branch — comparable to a successful inline hash lookup against any unsynchronised in-process map.

The cache is filled from the global probe: a hit on the global table writes the `(key, BaseStorage*)` pair into the local cache, evicting the least-recently-used entry if the cache is full. A subsequent probe with the same key short-circuits to a single cache-hit lookup with no lock acquisition at all — which is the common case for the dialect-internal types and attributes a single compilation pass touches repeatedly.

The cache has one structural invariant: it caches only *positive* probes. A miss does not insert a tombstone, because the absence of an entry from the global table is racy — between the miss and a later probe, another thread can publish a matching entry, and a negative cache entry would shadow the global publication. The cache's eviction policy is approximate-LRU rather than exact-LRU: a hit promotes the entry to the front of the eviction list with one swap, and a full cache evicts the tail entry on each insert. Approximate-LRU is sufficient because the per-thread access pattern is heavily skewed — a single pass touches a small working set of types, and the working set fits in the cache's fixed-size table without eviction in practice.

## Affine Cluster Mutex

The affine cluster (AffineMap, AffineExpr, IntegerSet) uses a single `pthread_mutex_t` rather than the per-TypeID rwlock the type and attribute clusters use. The split has a structural reason: affine expressions cross-reference each other through structural sharing (an `AffineExpr` may be the sub-expression of many `AffineMap` instances), so the per-TypeID locking model would force a probe of an `AffineMap` to acquire the lock for every `AffineExpr` it referenced. The single mutex collapses that fan-out into one acquisition per probe.

The cost is that affine-cluster interning is the only place in the storage stack where a probe blocks a concurrent insert from a different thread. In practice the cost is negligible because affine interning is dominated by the dialect-conversion driver's affine-expression rewrites, which run on the function thread and rarely overlap.

## StorageUniquer Two-Level Concurrency

The full `StorageUniquer::getOrCreate(type_id, key)` flow threads the four layers — TLS cache, atomic CAS on the Level-1 bucket, rwlock-guarded Level-2 probe, rwlock-guarded Level-2 insert — in that order. The TLS cache hit is the common-case fast path; the lock-free CAS path is what makes the first time any thread touches a new `TypeID` lock-free; the rwlock read path is what scales probes for hot types across worker threads; the rwlock write path is the single serialisation point where new storage is allocated.

The CAS-publish-or-free idiom protects the Level-1 bucket array. When a probe finds a null entry for an unseen `TypeID`, the probing thread allocates a fresh `StorageAllocator`, then issues a compare-exchange against the bucket. The winner of the CAS has its allocator visible to every other thread; the loser frees its allocation and continues with the winner's allocator. No two threads ever share a half-initialised `StorageAllocator` — the allocator is fully constructed before the CAS that publishes it.

```text
get_or_create(type_id, key):
    # Layer 1: TLS cache (no lock, no atomic)
    cached = tls_cache.lookup(type_id, key)
    if cached != nullptr:
        return retain(cached)

    # Layer 2: atomic CAS on Level-1 bucket (lock-free publish)
    allocator = atomic_load(level1[type_id])
    if allocator == nullptr:
        candidate = allocate_storage_allocator(type_id)
        prev = compare_exchange(level1[type_id], nullptr, candidate)
        allocator = (prev == nullptr) ? candidate : (free(candidate), prev)

    # Layer 3: rwlock-guarded read probe
    with allocator.rwlock.read:
        existing = allocator.lookup(key)
        if existing:
            tls_cache.insert(type_id, key, existing)
            return retain(existing)

    # Layer 4: rwlock-guarded write insert with re-probe
    with allocator.rwlock.write:
        existing = allocator.lookup(key)        # re-probe, mandatory
        if existing:
            tls_cache.insert(type_id, key, existing)
            return retain(existing)
        fresh = allocator.insert_new_storage(key)
        tls_cache.insert(type_id, key, fresh)
        return fresh
```

The re-probe under the write lock is not optional. Without it, two racing inserts can pass through the read-lock probe simultaneously, both find the key absent, both upgrade to write mode in sequence, and both allocate a fresh storage object — leaving the table with two distinct `BaseStorage*` for the same key. Every later equality test on that type would compare unique-but-equal storage objects and return false, breaking the structural-equality contract every dialect relies on.

The TLS cache insert happens *after* the global probe in both the read-path hit case and the write-path insert case. Inserting the entry before the lock is released would still be correct under TLS isolation, but the post-lock insert keeps the hot path's lock window minimal — the cache write is a pair of stores against thread-local memory and adds no contention to the rwlock.

## ThreadSafeRefCountedBase

Every interned storage object follows the canonical `llvm::ThreadSafeRefCountedBase` shape: a strong count, a weak count, and an installed-in-cache marker, all `int32`. Strong increments fire when a storage object is handed to a caller through the `getOrCreate` return path; strong decrements run the payload deleter when the count reaches zero. Weak decrements run the final destructor when the weak count reaches zero. Threaded builds use atomic add/sub on each field; single-threaded builds collapse the same code to ordinary integer updates through LLVM's weak-threading gate.

The interaction with the storage-uniquer layers above is one-way. The refcount lives inside the `BaseStorage` the uniquer hands out, but the uniquer itself never reads or writes the refcount — that work is done by the higher-level `Type` / `Attribute` value wrappers as they enter and leave caller scope. The uniquer is responsible only for ensuring that two callers asking for the same key see the same `BaseStorage*`; the refcount machinery then ensures that the storage outlives the last caller that holds a reference.

## Cross-Pass Synchronisation

Most passes in the compiler are single-threaded over a given function, but the dialect-conversion driver can run multiple passes in parallel over different functions of the same module. Each function's pass execution touches the shared `MLIRContext`, which means every concurrent function-pass shares the same Level-1 bucket array, the same per-TypeID Level-2 tables, and the same TLS caches per thread. The four-layer storage-uniquer concurrency model is what makes that parallelism safe: TLS isolation hides the common-case probe from the rwlocks; the rwlock read mode lets many functions probe the same type concurrently; the rwlock write mode serialises the rare insert; the CAS-publish-or-free lock-free path handles the even rarer case of the first thread to touch a new `TypeID`.

The dialect-conversion driver also depends on the diagnostic-printer mutex on `MLIRContext`. When a parallel pass emits an `emitError` or `emitWarning`, the diagnostic engine acquires that mutex before writing to the active diagnostic stream — without it, two threads emitting concurrent diagnostics would interleave their output character-by-character. The mutex is held for the duration of one diagnostic emission only; it does not protect the diagnostic engine's internal state between emissions, which is single-threaded by construction.

## Reimplementation Notes

A reimplementation has to preserve four invariants together; any one of them in isolation is insufficient.

1. **TLS cache caches only positive probes.** A negative cache entry shadows global publication and breaks the structural-equality contract.
2. **The Level-1 CAS publishes a fully constructed allocator.** The allocator must be allocated, zero-filled, and have its rwlock initialised before the CAS issues; any thread that observes the bucket through `atomic_load` must see a usable allocator.
3. **The Level-2 write path re-probes after the read-to-write upgrade.** Without the re-probe, racing inserts create distinct storage objects for the same key and the dialect's equality contract breaks downstream.
4. **The weak-threading gate is checked once.** A per-call check would not be slower in the absolute, but the hot path's branch predictor pollution and the lost speculation slot make a startup-time check the correct trade.

A reimplementation that skips invariant (1) can corrupt user-visible compilation by giving distinct `Type` values for what should be the same type. A reimplementation that skips invariant (2) hits a use-after-construct race on the first multi-threaded probe of any new `TypeID`. A reimplementation that skips invariant (3) hits the same equality-corruption mode as (1), but only under load. A reimplementation that skips invariant (4) wastes cycles on the threaded hot path; the correctness model still holds.
