# Container Fingerprints

## Abstract

Three associative-container families dominate the tileiras binary and each leaves a distinct
constant fingerprint that survives stripping: LLVM `DenseMap` / `DenseSet` (pointer-keyed, sentinels
`-4096` / `-8192`, inline pointer hash `(p>>9)^(p>>4)`), an Abseil SwissTable variant in the
scheduler (fmix64 multiplier `0x9DDFEA08EB382D69`, HighMul64 intermediate `0xAE502812AA7333`,
secondary mixer `-348639895`), and `SmallVector` value blocks with packed inline-capacity markers
(`0x300000000`, `0x400000000`, `0x600000000` as single 64-bit stores). This page lists the verbatim
constants, the inline hash and probe expressions, the resize predicates, and the identification
procedure needed to recognise each family from a single fingerprint and reimplement it without
symbols.

## Fingerprint Summary

| Family | Primary fingerprint | Slot pitch | Secondary fingerprint |
|---|---|---|---|
| LLVM DenseMap / DenseSet | sentinels `0xFFFFFFFFFFFFF000` and `0xFFFFFFFFFFFFE000` at slot byte 0 | 16 B `{KeyTy*, ValueTy*}` | inline hash `(p>>9)^(p>>4)`, stride-1 linear probe |
| Abseil SwissTable (scheduler) | fmix64 multiplier `0x9DDFEA08EB382D69`, HighMul64 intermediate `0xAE502812AA7333`, secondary mixer `-348639895` | 16 B `{u64 op_handle, u32 depth, u32 pad}` | same -4096 / -8192 sentinels, H2 = `(h>>9)^(h>>4)` |
| SmallVector inline-cap marker | `0x300000000`, `0x400000000`, `0x600000000` at value-block offset +0 | 8 B header u64 | encodes `cap` in high 32 bits, `size=0` in low 32 bits |

Across the binary there are 47 distinct occurrences of the literal `0xFFFFFFFFFFFFF000` and 40 of `0xFFFFFFFFFFFFE000` that fit the DenseMap/SwissTable empty/tombstone slot pattern.

## LLVM DenseMap and DenseSet

Two sentinel pointer values mark slot state in every pointer-keyed DenseMap and DenseSet in Tileiras. Empty slots hold `0xFFFFFFFFFFFFF000` (the signed value `-4096`); tombstones hold `0xFFFFFFFFFFFFE000` (`-8192`). The empty/tombstone test reads only the first 8 bytes of the 16-byte slot; the companion value pointer is irrelevant.

Lookup runs LLVM's classical inline pointer hash followed by a stride-1 linear probe. The hash is open-coded at every call site rather than dispatched through a virtual table, so the same two shifts and one XOR appear over and over:

```c
size_t dense_map_index(const void *key, size_t cap_mask) {
    uintptr_t p = (uintptr_t)key;
    size_t   h = ((size_t)(p >> 9)) ^ ((size_t)(p >> 4));
    return h & cap_mask;
}

void *dense_map_find(DenseSlot *slots, size_t cap, const void *key) {
    size_t mask = cap - 1;
    size_t idx  = dense_map_index(key, mask);

    for (;;) {
        DenseSlot *s = &slots[idx];
        uintptr_t  k = (uintptr_t)s->key;

        if (k == 0xFFFFFFFFFFFFF000ULL) return NULL;             // empty: terminate
        if (k != 0xFFFFFFFFFFFFE000ULL && s->key == key) return s;
        idx = (idx + 1) & mask;                                   // stride-1 probe
    }
}
```

Two thresholds drive resize. Growth fires at 3/4 occupancy; in-place rehash to clear tombstones fires when free non-tombstone slots fall to 1/8 of capacity. Growth picks the next power of two of `2N-1` with a 64-slot floor, via the same five-round bit-fill shift sequence that appears verbatim in the binary:

```c
bool should_grow(size_t live, size_t cap)                       { return 4 * (live + 1) >= 3 * cap; }
bool should_rehash_in_place(size_t live, size_t tomb, size_t cap) {
    return cap - tomb - (live + 1) <= cap / 8;
}

size_t next_size(size_t cap) {
    size_t t = 2 * cap - 1;
    t |= t >> 1;
    t |= t >> 2;
    t |= t >> 4;
    t |= t >> 8;
    t |= t >> 16;
    ++t;
    return t < 64 ? 64 : t;
}
```

That five-round shift cascade with the trailing `++t` is itself a fingerprint. Any call site that materialises `2*cap - 1` and then runs the cascade is either DenseMap growth or its SwissTable cousin.

## Abseil SwissTable in the Scheduler

A second container family lives inside the scheduler. It reuses the -4096 / -8192 sentinels but layers in three constants that never appear in LLVM DenseMap. The fmix64 multiplier `0x9DDFEA08EB382D69` is the strongest single signature — the only 64-bit immediate of that exact value in the binary, followed by the second-round multiplier and a 33-bit XOR shift. The intermediate `0xAE502812AA7333` surfaces inside the HighMul64 used for size-class indexing, and the secondary mixer `-348639895` (`0xFFFFFFFFEB30AB69` as a sign-extended 64-bit immediate, `0xEB30AB69` as a 32-bit constant) is the fallback path when fmix64 is too expensive to inline.

Buckets span 16 bytes: a 64-bit operation handle, then a 32-bit depth field, then 32 bits of padding. The probe splits the mixed hash in two — H1 = `fmix64(k)` selects a bucket group, H2 = `(h>>9)^(h>>4)` matches inside the group. H2 matches the DenseMap inline hash, which is why bucket scans look textually similar in the decompilation even though their group selection is different.

Two scheduler-specific sentinel encodings reuse the bucket layout. A positive `0x7FFFFFFF` written into the depth slot means "operation already retired in this attempt, skip during retry"; a positive `4096` written into the same word as a tombstone or empty marker means "no inline value, this entry is a key-only intern". Both encodings live inside `sub_981D50` at lines 40-147 of the decompilation; that function is the central probe loop reused by every scheduler intern table.

The same growth and rehash predicates apply as for DenseMap, including the `next_size` cascade. The two families are distinguishable by the presence of the fmix64 multiplier and by the bucket-group probe rather than by their resize policy.

## SmallVector Inline-Capacity Markers

A `SmallVector` value block begins with an 8-byte header: high 32 bits encode the inline capacity, low 32 bits hold the current size. Empty construction with a small inline capacity writes the whole header as a single 64-bit store of a recognisable constant.

| Constant | Decoded meaning |
|---|---|
| `0x300000000` | inline capacity = 3, size = 0 |
| `0x400000000` | inline capacity = 4, size = 0 |
| `0x600000000` | inline capacity = 6, size = 0 |

These constants appear near allocator entry points such as `sub_44A8C20` whenever a pass-local `SmallVector` is initialized. They are unambiguous because no DenseMap or SwissTable slot encoding produces a value in the `0x100000000`-`0xFFF00000000` range with all-zero low 32 bits.

## Identification Procedure

A short procedure classifies any constant or call site:

- `(p >> 9) ^ (p >> 4)` near 16-byte slot accesses paired with `0xFFFFFFFFFFFFF000` and `0xFFFFFFFFFFFFE000` reads points to LLVM DenseMap or DenseSet.
- The immediate `0x9DDFEA08EB382D69` near the same -4096 / -8192 slot reads points to Abseil SwissTable — almost always inside scheduler code.
- A 64-bit constant of the form `0xN00000000` for small N near a `SmallVector` allocator such as `sub_44A8C20` is an inline-capacity marker, not a hash-table sentinel.

The shared resize predicates and the `next_size` cascade can be reproduced verbatim across both hash-table families. The SwissTable's fmix64 multiplier is the only constant that distinguishes its growth path from DenseMap's at the call-site level.

## Reimplementation Invariants

- Reserve two pointer values per pointer-keyed map for empty and tombstone, matching the -4096 / -8192 encoding if binary compatibility is desired.
- Open-code `(p >> 9) ^ (p >> 4)` for pointer-key hashing inside DenseMap-style maps.
- Use H1 = `fmix64(k)` for SwissTable group selection and H2 = `(h >> 9) ^ (h >> 4)` for in-group matching.
- Fire growth at `4 * (live + 1) >= 3 * cap`; fire in-place rehash at `cap - tomb - (live + 1) <= cap / 8`.
- Round growth to `next_pow2(2 * cap - 1)` with a 64-slot floor.
- Reserve `0x7FFFFFFF` in the SwissTable depth slot for the retry-dead marker and `4096` for key-only intern entries.
- Initialize empty inline `SmallVector` value blocks with a single 64-bit store of `cap << 32`.

## Consumers

The DenseMap family backs every uniquing table in the binary — the Level-1 / Level-2 buckets in
[Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md), the dual-width DenseMaps
embedded in `AsyncValueImpl` (see [AsyncValue and BLAKE3 Interning](asyncvalue-and-blake3-interning.md)),
the `OperationName *` fingerprint hashmap built by `FrozenRewritePatternSet`
([Pattern Vtables and Shapes](pattern-vtables-and-shapes.md)), and the interface entry arrays in
[Interface Vtables](interface-vtables.md). The SwissTable family is exclusive to the scheduler and
the IR intern tables consumed by the BLAKE3 driver.

## Cross-References

[Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md) describes the type and
attribute interning tables that sit on top of these container families. [Modulo Driver and
Chain](../scheduler/modulo-driver-or-chain.md) documents the scheduler control flow that consumes
the SwissTable intern tables described above. [AsyncValue and BLAKE3
Interning](asyncvalue-and-blake3-interning.md) describes the BLAKE3 digest path that feeds the
SwissTable family.
