# Storage Uniquer and Context Implementation

## Abstract

Every uniqued value in TileIR — every Type, Attribute, Location, Identifier, AffineExpr, AffineMap, and IntegerSet — is interned through a single 9 630-byte gateway. The function lives at `sub_4497E40`, 534 basic blocks of mostly duplicated insert paths, and it is reached from more than 700 call sites, approximately one per registered uniqued class. Calling it twice with the same `(MLIRContextImpl*, TypeID, hash, equality)` tuple returns the same `BaseStorage*`. Calling it with a fresh key allocates a 32-byte `ThreadSafeRefCountedBase`-shaped storage object, publishes it into the right hash table, and returns the new canonical pointer.

What follows is the algorithm at reimplementation grade: the two-level hash table behind uniquing, the per-class allocator that owns Level-2, the compare-and-swap that publishes Level-2 into Level-1, the refcount transitions on storage objects, the thread-local cache that skips every lock on the common case, and the lock order that keeps the slow path safe under MLIR's full thread-safe context.

## Two-Level Intern Table

Two hash tables stack: Level-1 keys on a TypeID singleton — the address of a per-class sentinel in `.data.rel.ro` such as `&unk_5B37828` for `cuda_tile::TileType` or `&unk_5B377F0` for a representative attribute class — and stores a pointer to that class's `StorageAllocator`, an 88-byte structure that owns Level-2. Level-2 keys on the caller-supplied 32-bit hash plus a caller-supplied equality predicate; its values are the `BaseStorage*` objects returned to user code.

| Level | Key | Hash input | Value | Container |
| --- | --- | --- | --- | --- |
| 1 | TypeID sentinel pointer (`a2`) | sentinel address | `StorageAllocator*` | per-Context bucket array |
| 2 | structural key blob (`a5`) | caller hash (`a3`) | `BaseStorage*` | per-class bucket array |

Both levels run the same machinery. One hash family — the canonical LLVM `DenseMapInfo<void*>::getHashValue` seed `((uintptr_t)key >> 9) ^ ((uintptr_t)key >> 4)`. One collision strategy — stride-1 linear probing, bucket count kept a power of two so the index is `(N - 1) & h`. One sentinel pair at 16-byte slot pitch: `0xFFFFFFFFFFFFF000` (`(void*)-4096`) marks EMPTY, `0xFFFFFFFFFFFFE000` (`(void*)-8192`) marks TOMBSTONE.

The probe seed and the sentinel pair together are a hard fingerprint for upstream LLVM `DenseMap` and MLIR's `StorageUniquer`. Sharding does the rest of the work: the 16-byte slot pitch combined with per-TypeID Level-2 tables — `cuda_tile`, `nv_tileas`, `nv_tileaa`, `cute`, `cute_nvgpu`, `cutlass`, `nvvm`, `llvm`, `builtin`, `func`, `arith`, `scf`, `vector`, `memref`, `cf`, `math`, `pdl`, `pdl_interp`, plus roughly 30 attributes per dense dialect — keeps probe chains short even on enormous IRs.

## MLIRContextImpl Layout

The Level-1 array lives inside `MLIRContextImpl`, a 576-byte (`0x240`) object allocated by `sub_445EDD0`. Its first qword is the vtable pointer `&off_5A2CA80`, which is the central anchor for "this is an MLIRContext" in the binary. The fields read by the uniquer are:

```c
struct MLIRContextImpl {                              /* 0x240 bytes */
    /*+0x000*/ void              **vtable;            /* off_5A2CA80 */
    /*+0x010*/ atomic_uint64_t     context_id;
    /*+0x040*/ DialectTable       *registered_dialects;
    /*+0x080*/ OpRegistry         *registered_op_table;
    /*+0x110*/ AttributeRegistry  *registered_attr_table;
    /*+0x180*/ TypeRegistry       *registered_type_table;
    /*+0x1B0*/ Level1Slot         *type_uniquer_buckets;
    /*+0x1C0*/ uint32_t            type_uniquer_size;     /* power of two, >= 64 */
    /*+0x260*/ Level1Slot         *attr_uniquer_buckets;
    /*+0x270*/ uint32_t            attr_uniquer_size;
    /*+0x278*/ pthread_mutex_t     allocator_mutex;       /* 40 B, *ctx + 632 */
    /*+0x2B0*/ AffineUniquerState *affine_uniquer_state;  /* *ctx + 688 */
    /* ... diagnostic handler chain, interface tables, dialect hooks ... */
};

struct Level1Slot {                                   /* 16 B */
    /*+0x00*/ TypeID  *type_id;       /* the sentinel address, or EMPTY/TOMBSTONE */
    /*+0x08*/ StorageAllocator *impl; /* Level-2 handle, CAS target */
};
```

Five helper routines reach into `MLIRContextImpl` — callers inline them or share them. `sub_445B3C0` is the `insertOrLookup<ImmortalStorage<ArrayRef<Storage*>>>` shim that other passes use to dedup-then-intern small pointer arrays. `sub_447FBB0` is the 1242-line `walk_impl` that drives the operation walker. `sub_4458150` is the 4-way unrolled tail of an LLVM `DenseSet::find` used on hot dispatch paths. `sub_445F520` and `sub_4461BA0` are `getRegisteredType` and `getRegisteredAttribute`; both consult the context allocator mutex when they need to publish a new descriptor.

Do not confuse the allocator mutex at `*ctx + 632` with the diagnostic-handler mutex earlier in the structure. The allocator mutex guards Level-1 mutation alone: live-count and tombstone bookkeeping, the resize-in-place dance, and the narrow window where a thread has decided a TypeID has no Level-2 slot and is about to publish one.

## StorageAllocator Layout

One `StorageAllocator` per registered class: 88 bytes holding the Level-2 bucket array, its free list, its load-factor counters, and the rwlock that synchronises Level-2 readers and writers.

```c
struct StorageAllocator {                             /* 0x58 bytes */
    /*+0x00*/ StorageEntry  *buckets;       /* open-addressed table, 16 B slots */
    /*+0x08*/ void          *freelist;      /* zeroed on resize */
    /*+0x10*/ uint32_t       live_count;
    /*+0x14*/ uint32_t       tombstone_count;
    /*+0x18*/ uint32_t       bucket_count;  /* power of two, >= 64 */
    /*+0x1C*/ uint32_t       resize_threshold;
    /*+0x20*/ pthread_rwlock_t lock;        /* 56 B, Level-2 readers/writer */
};

struct StorageEntry {                                 /* 16 B */
    /*+0x00*/ uint32_t  hash_key;           /* caller-supplied a3 */
    /*+0x04*/ uint32_t  pad0;
    /*+0x08*/ uint64_t  ptr_or_sentinel;    /* BaseStorage*, -4096 EMPTY, -8192 TOMBSTONE */
};
```

`sub_44A8C20(0x58)` allocates the allocator and zeroes it before publish. `sub_45603F0(16 * N, 8)` allocates its buckets, then a tight loop walks the buffer at 16-byte stride writing `hash_key = 0` and `ptr_or_sentinel = -4096`. Insert-path specialisation duplicates the same loop body more than ten times across `sub_4497E40`; the literal `-4096` appears 47 times in the body, `-8192` 40 times.

## BaseStorage Layout

A uniqued storage object is 32 bytes of `ThreadSafeRefCountedBase` with the MLIR storage payload tacked onto the same block. Its vtable is the global `off_59A4108`.

```c
struct BaseStorage {                                  /* 0x20 bytes */
    /*+0x00*/ void   **vtable;        /* off_59A4108 */
    /*+0x08*/ int32_t  strong_count;  /* init 1 — owned-by-uniquer */
    /*+0x0C*/ int32_t  weak_count;    /* init 1 — handed to caller */
    /*+0x10*/ void    *payload;       /* class-specific, zero at init */
    /*+0x18*/ uint8_t  flags;         /* bit 0 = "owned by uniquer" */
    /*+0x19*/ uint8_t  pad[7];
};
```

Vtable slot 2 is the deleter, slot 3 the full destructor. The deleter fires when `strong_count` drops to zero through `_InterlockedExchangeAdd(&strong, -1)`, dispatched as `(*(**vtable + 16))(obj)`; the destructor fires when `weak_count` drops to zero, dispatched as `(*(**vtable + 24))(obj)`. Both counts initialise to 1 because the uniquer holds the strong reference (it caches the object) and hands the caller a weak reference through the bucket entry. The `flags` byte at `+0x18` is set to 1 right after a successful insert — the "installed in cache" marker that prevents the deleter from running while the publish is still in flight.

## The getOrCreate Gateway

The full signature of the gateway is:

```c
__int64 sub_4497E40(
    MLIRContextImpl    **uniquer_pp,    /* a1 */
    TypeID              *type_id,       /* a2 — sentinel pointer */
    uint32_t             hash,          /* a3 — precomputed 32-bit key hash */
    bool (*equals)(uintptr_t, uintptr_t, uintptr_t),
                                        /* a4 — equality predicate */
    void                *key_ctx,       /* a5 — KeyTy pointer / equality context */
    void                *alloc_ctx,     /* a6 — opaque, forwarded to ctor */
    __m128i              pack);         /* a7 — pack.lo = ctor*, pack.hi = KeyTy blob */
```

The `__m128i` at `a7` is loaded into a single SSE register on entry and split at the two construction sites: the low qword is a pointer to the storage constructor and the high qword is the key blob handed to that constructor. This packing matches upstream `mlir::detail::StorageUniquer::getOrCreate<Storage>(KeyTy)`, where the StorageAllocator and KeyTy are forwarded to `Storage::construct`.

The full algorithm, with the duplicated insert bodies collapsed into a single representative path:

```c
BaseStorage *get_or_create(MLIRContextImpl **uniquer_pp,
                           TypeID *tid,
                           uint32_t hash,
                           equals_fn equals,
                           void *key_ctx,
                           void *alloc_ctx,
                           __m128i pack)
{
    MLIRContextImpl *U = *uniquer_pp;
    uint32_t N1 = U->type_uniquer_size;

    /* ---------- Level-1 probe: TypeID -> StorageAllocator ---------- */
    if (N1 == 0) {
        grow_level1(U, /*new_count=*/64);                       /* min bucket count is 64 */
        N1 = U->type_uniquer_size;
    }

    uint32_t h1   = ((uintptr_t)tid >> 9) ^ ((uintptr_t)tid >> 4);
    uint32_t mask = N1 - 1;
    Level1Slot *buckets = U->type_uniquer_buckets;
    Level1Slot *tomb    = NULL;
    uint32_t step = 1;
    uint32_t idx  = mask & h1;

    for (;;) {
        Level1Slot *s = &buckets[idx];
        if (s->type_id == tid)               break;             /* hit */
        if ((uintptr_t)s->type_id == -4096)  goto l1_insert;    /* EMPTY */
        if ((uintptr_t)s->type_id == -8192 && !tomb) tomb = s;  /* first tombstone wins */
        idx = mask & (idx + step);
        ++step;
    }

    StorageAllocator *impl = buckets[idx].impl;
    goto l2_entry;

l1_insert:
    /* Load-factor 3/4 trigger and 1/8 tombstone-density trigger.
     * On grow, next-pow2(2N - 1) via the inline 5-round bit-fill,
     * clamped to a minimum of 64. On rehash-in-place, same size. */
    pthread_mutex_lock(&U->allocator_mutex);

    uint32_t live = ++U->live_count;
    if (4 * live >= 3 * N1) {
        uint32_t new_N = next_pow2(2 * N1 - 1);
        if (new_N < 64) new_N = 64;
        rehash_level1(U, new_N);
    } else if (N1 - U->tombstone_count - live <= N1 / 8) {
        rehash_level1(U, N1);                                   /* same size, drops tombs */
    }

    Level1Slot *seat = tomb ? tomb : &buckets[idx];
    seat->type_id    = tid;

    StorageAllocator *fresh = sub_44A8C20(0x58);                /* 88-byte calloc */
    memset(fresh, 0, 0x58);

    /* CAS-publish the StorageAllocator. If another thread won the race,
     * free the loser and use the winner. The CAS happens with the allocator
     * mutex held; the mutex protects bookkeeping, the CAS protects publish. */
    StorageAllocator *winner = (StorageAllocator *)
        _InterlockedCompareExchange64(&seat->impl, (int64_t)fresh, 0);

    impl = winner ? winner : fresh;
    if (winner) {
        sub_4560420(fresh->buckets, 16 * fresh->bucket_count, 8);
        free(fresh);
    }

    pthread_mutex_unlock(&U->allocator_mutex);

l2_entry:
    /* ---------- TLS cache fast path ---------- */
    if (tls_cache_hit(key_ctx, impl, hash, &result)) {
        return result;                                          /* no locks, no atomics */
    }

    /* ---------- Level-2 probe under per-class rwlock ---------- */
    pthread_rwlock_rdlock(&impl->lock);
    BaseStorage *hit = level2_probe_read(impl, hash, equals, key_ctx);
    if (hit) {
        pthread_rwlock_unlock(&impl->lock);
        tls_cache_install(key_ctx, impl, hash, hit);
        return hit;
    }
    pthread_rwlock_unlock(&impl->lock);

    /* Upgrade to write and re-probe; another thread may have inserted. */
    pthread_rwlock_wrlock(&impl->lock);
    hit = level2_probe_read(impl, hash, equals, key_ctx);
    if (hit) {
        pthread_rwlock_unlock(&impl->lock);
        tls_cache_install(key_ctx, impl, hash, hit);
        return hit;
    }

    /* Resize Level-2 with the same load-factor and tombstone-density
     * triggers as Level-1. Reuse the inline next-pow2 bit-fill. */
    if (4 * (impl->live_count + 1) >= 3 * impl->bucket_count) {
        rehash_level2(impl, next_pow2(2 * impl->bucket_count - 1));
    } else if (impl->bucket_count - impl->tombstone_count - impl->live_count
               <= impl->bucket_count / 8) {
        rehash_level2(impl, impl->bucket_count);
    }

    /* Construct the storage object via the caller's ctor callback.
     * In thread-safe contexts the allocator argument is the per-thread
     * sub-allocator returned by sub_4496E20; in single-threaded mode it
     * is the context itself. */
    void *allocator_arg = thread_safe(U)
                          ? sub_4496E20(uniquer_pp, alloc_ctx)
                          : (void *)U;

    typedef BaseStorage *(*ctor_fn)(void *key, void *alloc, void *ctx);
    ctor_fn ctor = (ctor_fn)((__m128i_u64 *)&pack)[0];
    void   *key  = (void *)((__m128i_u64 *)&pack)[1];

    BaseStorage *storage = ctor(key, allocator_arg, alloc_ctx);

    /* Initialise the refcount header. The uniquer holds the strong ref
     * (it caches the object); the caller is handed the weak ref. */
    storage->vtable        = &off_59A4108;
    storage->strong_count  = 1;
    storage->weak_count    = 1;
    storage->payload       = NULL;
    storage->flags         = 1;                                 /* installed in cache */

    StorageEntry *seat2 = level2_seat_for(impl, hash);
    seat2->hash_key         = hash;
    seat2->ptr_or_sentinel  = (uint64_t)storage;

    pthread_rwlock_unlock(&impl->lock);
    tls_cache_install(key_ctx, impl, hash, storage);
    return storage;
}
```

The body is enormous because the inner insert is duplicated for every combination of `{Level-1 resize / no-resize} × {Level-2 resize / no-resize} × {mutex / rwlock / single-threaded}`. The pseudocode collapses those into one normal form; the binary carries nine specialisations of the same insert block, each tuned for one combination of locks held and resize state.

## Sentinels and the Inline next-pow2

The EMPTY and TOMBSTONE sentinels are the same constants at both levels and across every duplicated probe body:

```c
#define DENSE_EMPTY     ((void *)-4096)   /* 0xFFFFFFFFFFFFF000 */
#define DENSE_TOMBSTONE ((void *)-8192)   /* 0xFFFFFFFFFFFFE000 */
```

`-4096` and `-8192` are deliberate choices: both are page-aligned, both stand out against any heap pointer, and both compare cheaply against sign-extended 32-bit immediates. The same pair shows up in `sub_117BB70`, an unrelated `DenseMap` rehash body with 80-byte slots; the slot pitch differs because `sub_117BB70` inlines its full keys while `sub_4497E40` stores only pointers and a 32-bit hash.

The next-power-of-two routine is expanded inline at every grow site:

```c
uint32_t next_pow2(uint32_t x) {
    x |= x >> 1;
    x |= x >> 2;
    x |= x >> 4;
    x |= x >> 8;
    x |= x >> 16;
    return x + 1;
}
```

Any result smaller than `0x40` is clamped to 64. The minimum bucket count after any allocation is therefore always 64.

## Resize Policy

Two independent triggers govern resize, applied identically at both levels:

| Trigger | Condition | Action |
| --- | --- | --- |
| Load-factor | `4 * (live + 1) >= 3 * N` | grow to `next_pow2(2*N - 1)`, min 64 |
| Tombstone density | `N - tombstones - (live + 1) <= N / 8` | rehash in place at the same size |

Load-factor resize keeps the probe chain expected-constant. Tombstone-density resize stops a delete-heavy workload from accumulating an unbounded chain of dead slots that linear probing must scan through. A reimplementation that follows true `DenseMap` semantics — never delete, only allocate — exercises the load-factor trigger almost exclusively, because storage objects are immutable and only freed when the whole context dies.

## Compare-And-Swap on Level-1 Publish

A single `_InterlockedCompareExchange64(&seat->impl, fresh, 0)` installs the new `StorageAllocator` into Level-1. The CAS races every other thread allocating the same TypeID for the first time: both see EMPTY at Level-1, both call `sub_44A8C20(0x58)`, and both arrive at the CAS with a private allocator in hand. The winner installs its allocator and proceeds to Level-2; the loser sees the winner's allocator in the CAS return value, frees its own through `sub_4560420` and `free`, and proceeds against the winner.

This pattern is correct because Level-1 entries are write-once. Once a `StorageAllocator` is published into a Level-1 slot, the entry never changes — the TypeID is permanent and the allocator outlives the context. Level-2 is mutated forever, and that is why Level-2 is guarded by the per-class rwlock instead of CAS.

The CAS is wrapped in a broader region protected by the allocator mutex at `*ctx + 632`. The mutex is held while bookkeeping `live_count` and `tombstone_count`, while resizing Level-1, and across the CAS itself. The CAS is the synchronisation primitive that publishes the allocator; the mutex is the synchronisation primitive that keeps Level-1's metadata consistent. They are complementary, not redundant.

## Single-Threaded Collapse

Single-threaded builds dissolve the entire locking apparatus into plain loads and stores. The trick is a weak-symbol probe of `&_pthread_key_create`: glibc resolves it to a non-zero address when libpthread is loaded and to NULL otherwise.

That same probe gates every atomic op in `sub_4497E40`. `pthread_mutex_lock` / `_unlock` and `pthread_rwlock_rdlock` / `wrlock` all resolve to no-ops; `_InterlockedCompareExchange64` collapses to a plain pointer store followed by a pointer load. The binary carries both expansions side-by-side, switched by a load of the weak symbol. The same gate guards every `_InterlockedExchangeAdd` on the strong and weak refcounts.

This is why a single-threaded `cicc` invocation pays zero synchronisation cost for uniquing. The fast path really is a hash lookup and a pointer load — nothing else.

## Thread-Local Cache

Both locks vanish from the fast path through a thread-local cache rooted at `%fs:-584`. The cache holds the four most recently looked-up `(KeyTy, StorageAllocator*)` pairs; a hit returns the interned pointer with no atomic ops and no locks at all.

```c
struct TlsCache {                                     /* at %fs:-592 */
    /*-592*/ bool      initialised;
    /*-584*/ uint32_t  header;          /* bucket_count_cache << 1 | tombstone_bit */
    /*-580*/ uint32_t  tombstone_count;
    /*-576*/ void     *cache_storage;   /* inline 4-slot array */
    /*-568*/ uint32_t  live_count;
    /*-560*/ CacheRow  rows[4];         /* 40 B each */
};
```

On first use the cache registers a thread-exit destructor with `sub_44A7D30(sub_44933E0, &tls[-584])`, the moral equivalent of `pthread_key_create(&key, sub_44933E0)`. The destructor walks the four cache rows on thread exit and decrements the weak refcount of each cached storage object so that the uniquer's strong references are correctly accounted.

The cache keys on `(KeyTy, StorageAllocator*)` rather than `(KeyTy, TypeID)` because the Level-1 CAS publish happens once per class and the resulting allocator pointer is stable for the life of the context. Caching on the allocator skips Level-1 entirely on every subsequent hit.

## Refcount Transitions

Refcount transitions on `BaseStorage` go through `_InterlockedExchangeAdd`, treated as fetch-and-add since it returns the pre-update value. Both counters share the qword at `+0x08` but are accessed as 32-bit subfields, so an atomic on either counter leaves the other undisturbed.

| Transition | Atomic | Trigger |
| --- | --- | --- |
| Strong increment | `_InterlockedExchangeAdd(&strong, +1)` | hand-off to caller after insert |
| Strong decrement | `_InterlockedExchangeAdd(&strong, -1)` | uniquer evicts cached entry |
| Weak increment | `_InterlockedExchangeAdd(&weak, +1)` | caller stores a weak handle |
| Weak decrement | `_InterlockedExchangeAdd(&weak, -1)` | weak handle drops |

When a strong decrement returns 1 (pre-decrement), the deleter at `vtable[2]` fires via `(*(**vtable + 16))(obj)`. When a weak decrement returns 1, the destructor at `vtable[3]` fires via `(*(**vtable + 24))(obj)`. The `flags` byte at `+0x18` is the "owned by uniquer" marker that prevents the deleter from running while the storage is mid-publish — the byte is set to 1 only after the Level-2 bucket has been written with the storage pointer, so an in-flight insert is never reachable from another thread before its refcount transitions become valid.

## Lock Order and Concurrency Model

Three lock domains are held during a complete `get_or_create`, and the gateway always acquires them in the same order:

| Order | Lock | Scope | Protects |
| --- | --- | --- | --- |
| 1 | TLS cache | per-thread | local 4-slot cache, no synchronisation needed |
| 2 | `allocator_mutex` | per-context | Level-1 bookkeeping and CAS publish window |
| 3 | `StorageAllocator::lock` | per-class | Level-2 buckets, refcount transitions |

The allocator mutex is held only on the slow path. The fast path — TLS hit or warm Level-1 plus warm Level-2 — never acquires it. Concurrent uniquers of different TypeIDs share no state once their Level-1 entries are published; they race only at Level-2 within their own class. Concurrent uniquers of the same TypeID synchronise through the per-class rwlock: readers probe under the rdlock, and a miss upgrades to wrlock with a mandatory re-probe to catch a competing insert.

The rwlock upgrade is not atomic — the gateway explicitly drops the read lock before requesting the write lock, and the re-probe under wrlock is what makes the design correct. A simple loop that holds the read lock and asks for the write lock would deadlock against another thread doing the same thing.

## Caller Shape

Each of the 700+ callers is a tiny shim of roughly 1 KB. The shim's only job is to compute the 32-bit key hash, pack the constructor pointer and key blob into the `__m128i`, and tail-call `sub_4497E40` with the right TypeID sentinel. A representative pattern, derived from five canonical shims (`sub_6156C0`, `sub_6180E0`, `sub_618360`, `sub_6185E0`, `sub_61E800`):

```c
BaseStorage *get_or_create_TileType(MLIRContextImpl *ctx, Shape shape, ElementType elt) {
    KeyTy key = pack_key(shape, elt);
    uint32_t hash = ((uintptr_t)&key >> 9) ^ ((uintptr_t)&key >> 4);
    /* hash is then mixed with the structural bytes of the key */
    hash = mix_key_bytes(hash, &key, sizeof key);

    __m128i pack;
    pack.lo = (uint64_t)&TileTypeStorage_construct;
    pack.hi = (uint64_t)&key;

    return sub_4497E40(&ctx, &unk_5B37828 /* TileType TypeID */,
                       hash, &TileTypeStorage_equals, &key, ctx, pack);
}
```

The TypeID sentinel address is hard-coded per shim because the address is the identity. The constructor is a small helper that allocates 32 bytes via the StorageAllocator's bump-pointer allocator (separate from `StorageAllocator::buckets` — that is the hash table, not the storage region), copies the key's structural bytes into the payload, and returns the pointer to be installed in Level-2.

## Interaction with the Rest of MLIR

`sub_4497E40` is the shared backbone for every uniqued value in TileIR. The Type system uniques `IntegerType`, `FloatType`, `MemRefType`, `cuda_tile::TileType`, `nv_tileaa::TokenType`, `cute::LayoutType`, and so on. The Attribute system uniques `StringAttr`, `ArrayAttr`, `DictionaryAttr`, plus per-dialect dense attribute classes. The Location system uniques `FileLineColLoc`, `NameLoc`, `CallSiteLoc`, and `FusedLoc`. `Identifier` is a small wrapper around `StringAttr` that short-circuits to the same uniquer. AffineExpr, AffineMap, and IntegerSet each have their own TypeIDs and their own Level-2 tables but share the gateway. The internal DAG uniquers for the `cuda_tile` block and region trees also reach the gateway, transitively, through `sub_445B3C0`.

A reimplementation can choose a different table representation, but the contract is fixed: identity for uniqued objects is pointer equality, storage objects are immutable after publication, and the allocator that owns Level-2 outlives every storage object it allocates. Anything that breaks one of those invariants breaks every map, set, and pattern matcher that keys on Type or Attribute identity.

## Reimplementation Invariants

- Make uniqued storage immutable after publication, including any cached hash.
- Key Level-1 on the TypeID sentinel address, not on its contents.
- Use one hash family for both levels; the `(p>>9)^(p>>4)` seed has the right spread for `.data.rel.ro` pointers.
- Preserve the EMPTY/TOMBSTONE sentinel pair so that probes do not need an auxiliary bitmap.
- Keep the load-factor trigger at 3/4 and the tombstone-density trigger at 1/8.
- CAS-publish Level-2 allocators into Level-1; never mutate a published Level-1 entry.
- Re-probe Level-2 after rwlock upgrade; the upgrade is not atomic.
- Gate every atomic and every lock on a single threading switch so that single-threaded builds pay no synchronisation cost.
- Use a thread-local cache keyed on `(KeyTy, StorageAllocator*)`, not on `(KeyTy, TypeID)`.
- Initialise `BaseStorage` strong and weak counts to 1, and set the "owned by uniquer" flag only after the Level-2 bucket is written.

## How to Recognize in a Binary

The gateway `sub_4497E40` is identifiable from any of the following independent fingerprints:

- The combination of the EMPTY sentinel `0xFFFFFFFFFFFFF000` (47 occurrences) and the TOMBSTONE
  sentinel `0xFFFFFFFFFFFFE000` (40 occurrences) at 16-byte slot pitch is the strongest signal. The
  pair is unambiguous because both values are at the top of the unmapped address range and never
  collide with heap pointers.
- The inline pointer hash `((uintptr_t)k >> 9) ^ ((uintptr_t)k >> 4)` appears at every Level-1
  probe entry and at every Level-2 caller-supplied-hash mixer site. A function that materialises
  this two-shift XOR over a pointer-shaped operand is part of the uniquer family.
- The `__m128i` calling convention with `pack.lo = ctor*` and `pack.hi = key_blob*` distinguishes
  `sub_4497E40` from any other variadic interner. Callers visibly pack two pointers into an SSE
  register before the call; the gateway splits them at the two construction sites.
- The 88-byte (`0x58`) `sub_44A8C20(0x58)` allocation immediately followed by a per-class rwlock
  initialiser is the `StorageAllocator` constructor — the Level-2 owner allocated by the
  gateway's L1-insert path.

The single qword at `*ctx + 632` that is held under `pthread_mutex_lock` during Level-1 mutation
distinguishes the allocator mutex from the diagnostic-handler mutex (earlier in the structure) and
from the per-class rwlock (later, inside each `StorageAllocator`). Verifiers that audit lock order
key on the offset rather than on the lock value.

## Consumers

Every uniqued value in TileIR is produced by a caller of this gateway. The 700+ shims sit one per
registered class — each `cuda_tile`, `nv_tileas`, `nv_tileaa`, `cute`, `cute_nvgpu`, `cutlass`,
`nvvm`, `llvm`, `builtin`, `func`, `arith`, `scf`, `vector`, `memref` Type and Attribute class owns
one. The walker in [Operation Layout](operation-layout.md) reads the resulting `OperationName`
sentinels at `+0x40` for kind dispatch; the pattern application drivers in
[Pattern Vtables and Shapes](pattern-vtables-and-shapes.md) read them through the frozen
fingerprint map. The TypeID sentinel bands documented in
[TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md) are the Level-1 keys this gateway
hashes on.

## Cross-References

[Type Identity Anchors](typeid-sentinels-and-anchors.md) documents how TypeID sentinel addresses are assigned to dialects, operations, types, attributes, and interfaces. [MLIR Infrastructure Overview](overview.md) is the entry point for the rest of the substrate. [Operation Layout](operation-layout.md) describes how uniqued types and attributes are referenced from operations. [Container Fingerprints](container-fingerprints.md) catalogues the other DenseMap- and DenseSet-shaped tables in the binary that share the same probe seed and sentinel constants.
