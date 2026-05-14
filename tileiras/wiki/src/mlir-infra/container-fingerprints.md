# Container Fingerprints

## Abstract

Three associative-container families dominate the tileiras binary and each leaves a distinct
constant fingerprint that survives stripping: LLVM `DenseMap` / `DenseSet` (pointer-keyed, sentinel
pointers `0xFFFFFFFFFFFFF000` and `0xFFFFFFFFFFFFE000` in the slot key word, inline pointer hash
`(p>>9)^(p>>4)`), an Abseil SwissTable variant in the scheduler (full fmix64 finalizer with
multipliers `0xFF51AFD7ED558CCD` and `0xC4CEB9FE1A85EC53`, size-class constant
`0x9DDFEA08EB382D69`, 16-byte control-byte groups with sentinels `0x80`, `0xFE`, `0xFF`), and
`SmallVector` value blocks with packed inline-capacity markers (`0x300000000`, `0x400000000`,
`0x600000000` as single 64-bit stores). This page lists the verbatim constants, the inline hash and
probe expressions, the resize predicates, and the identification procedure needed to recognise each
family from a single fingerprint and reimplement it without symbols.

## Fingerprint Summary

| Family | Primary fingerprint | Slot pitch | Probe shape |
|---|---|---|---|
| LLVM DenseMap / DenseSet | sentinel pointers `0xFFFFFFFFFFFFF000` (empty) and `0xFFFFFFFFFFFFE000` (tombstone) in slot key word | 16 B `{KeyTy*, ValueTy*}` | inline pointer hash `(p>>9)^(p>>4)`, stride-1 linear probe over key slots |
| Abseil SwissTable (scheduler) | fmix64 multiplier `0x9DDFEA08EB382D69`, HighMul64 intermediate `0xAE502812AA7333`, full fmix64 finalizer (three xor-shifts) | 16 B control-byte group plus 16 entry slots | H1 picks group (high 57 bits of hash), H2 tag (low 7 bits) SIMD-matched inside 16-byte control group |
| SmallVector inline-cap marker | `0x300000000`, `0x400000000`, `0x600000000` written as a single 64-bit store at value-block offset +0 | 8 B header u64 | encodes `cap` in high 32 bits, `size = 0` in low 32 bits |

Across the binary there are 47 distinct occurrences of the literal `0xFFFFFFFFFFFFF000` and 40 of `0xFFFFFFFFFFFFE000` that fit the DenseMap empty/tombstone slot pattern.

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

A second container family lives inside the scheduler. It uses the standard Abseil SwissTable shape — a 16-byte control-byte group followed by 16 entry slots — and is identifiable by the fmix64 multiplier `0x9DDFEA08EB382D69`, the strongest single signature in the binary because no other call site uses that exact immediate.

### fmix64 Finalizer

The hash a SwissTable indexes with is the output of the fmix64 finalizer, derived from the input pointer or key by three successive xor-shift-multiply rounds:

```c
uint64_t fmix64(uint64_t h) {
    h ^= h >> 33;
    h *= 0xFF51AFD7ED558CCDULL;   // K1
    h ^= h >> 33;
    h *= 0xC4CEB9FE1A85EC53ULL;   // K2
    h ^= h >> 33;
    return h;
}
```

The `0x9DDFEA08EB382D69` constant is the size-class multiplier used in the HighMul64 step that picks a group index from the finalized hash; the intermediate `0xAE502812AA7333` surfaces inside that HighMul64 computation as the high-half of the 128-bit product. The two-round xor-shift-multiply is the diagnostic — any function that runs the fmix64 finalizer and follows it with a HighMul64-style group selection is a SwissTable probe.

### H1 / H2 Split

The finalized hash splits into two halves that play distinct roles. H1 (the high 57 bits) selects a group; H2 (the low 7 bits) tags the entry inside the group. The control-byte group is 16 bytes wide and holds one control byte per entry slot. A probe loads the group, broadcasts H2 across a 16-byte SIMD register, compares for byte-equality against the control bytes in one instruction, and walks only the slots whose control byte matches H2.

```c
size_t swiss_h1(uint64_t hash) { return hash >> 7; }              // group selector
uint8_t swiss_h2(uint64_t hash) { return (uint8_t)(hash & 0x7F); } // 7-bit per-slot tag

Entry *swiss_find(const SwissTable *t, const void *key) {
    uint64_t hash = fmix64((uint64_t)key);
    size_t group = swiss_h1(hash) % t->n_groups;
    uint8_t tag  = swiss_h2(hash);

    while (true) {
        __m128i ctrl = _mm_loadu_si128((const __m128i *)t->ctrl[group]);
        __m128i match = _mm_cmpeq_epi8(ctrl, _mm_set1_epi8(tag));
        uint32_t mask = (uint32_t)_mm_movemask_epi8(match);

        while (mask) {
            int i = __builtin_ctz(mask);                       // first matching slot
            Entry *e = &t->slots[group * 16 + i];
            if (e->key == key) return e;
            mask &= mask - 1;
        }

        // No live match in this group. If any slot is kEmpty, the key is absent.
        __m128i empties = _mm_cmpeq_epi8(ctrl, _mm_set1_epi8((char)0x80));
        if (_mm_movemask_epi8(empties)) return NULL;

        group = (group + 1) % t->n_groups;                     // probe next group
    }
}
```

The H2 tag turns membership into a single-instruction byte compare; only the slots whose tag matches are read further. That is why SwissTable finds a present key in expected O(1) regardless of group occupancy — the SIMD scan handles all 16 slots in one step.

### Control-Byte Sentinels

Four control-byte values encode slot state. The top bit distinguishes occupied slots (top bit clear, byte holds H2) from special slots (top bit set, byte encodes a state).

| Sentinel | Value | Meaning |
|---|---|---|
| `kEmpty` | `0x80` | slot has never been used; probe terminates here |
| `kDeleted` | `0xFE` | slot was occupied and erased; probe continues past it |
| `kSentinel` | `0xFF` | end-of-table guard; never matched as an entry |
| occupied | `0x00..0x7F` | H2 tag of the entry currently in this slot |

The probe terminates on `kEmpty` because no later insertion could have written past an empty cell; on `kDeleted` it continues so it can still find live entries inserted before the deletion. The `kSentinel` byte sits one past the last valid slot and lets the SIMD scan run without a separate bounds check.

### Growth and Rehash

SwissTable's growth policy is structurally similar to DenseMap's but expressed against the number of empty control slots rather than against tombstone count. Growth fires when the number of remaining empty slots drops below `(7 * capacity) / 16`; rehash-in-place to clear `kDeleted` slots fires when the tombstone fraction exceeds an analogous threshold. The same power-of-two `next_size` cascade from the DenseMap section applies.

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

- `(p >> 9) ^ (p >> 4)` near 16-byte slot accesses paired with `0xFFFFFFFFFFFFF000` (empty) and `0xFFFFFFFFFFFFE000` (tombstone) pointer reads points to LLVM DenseMap or DenseSet.
- The immediate `0x9DDFEA08EB382D69` followed by an xor-shift-multiply cascade, paired with a SIMD compare against an immediate that broadcasts a single byte (`pshufb` / `pcmpeqb` against H2), points to Abseil SwissTable. Control bytes `0x80`, `0xFE`, `0xFF` appearing in the same probe loop are the sentinel signature.
- A 64-bit constant of the form `0xN00000000` for small `N` near a `SmallVector` allocator is an inline-capacity marker, not a hash-table sentinel.

The two hash-table families are distinguishable by sentinel domain — DenseMap stores sentinels as pointer values in the key slot, SwissTable stores them as bytes in a separate control-byte group. The SwissTable's fmix64 multiplier is the strongest single-call-site signature.

## Consumers

The DenseMap family backs every uniquing table in the binary — the Level-1 / Level-2 buckets in
[Storage Uniquer and Context Impl — Two-Level Intern Table](storage-uniquer-and-context-impl.md#two-level-intern-table), the dual-width DenseMaps
embedded in `AsyncValueImpl` (see [AsyncValue and BLAKE3 Interning — AsyncValueImpl Header](asyncvalue-and-blake3-interning.md#asyncvalueimpl-header)),
the `OperationName *` fingerprint hashmap built by `FrozenRewritePatternSet`
([Pattern Vtables and Shapes — Pattern Application Drivers](pattern-vtables-and-shapes.md#pattern-application-drivers)), and the interface entry arrays in
[Interface Vtables — InterfaceMap Layout](interface-vtables.md#interfacemap-layout). The SwissTable family is exclusive to the scheduler and
the IR intern tables consumed by the BLAKE3 driver.

## Cross-References

[Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md) describes the type and
attribute interning tables that sit on top of these container families. [Modulo Driver — Per-Attempt SwissTable
Scratches](../scheduler/modulo-driver-or-chain.md#per-attempt-swisstable-scratches) documents the scheduler control flow that consumes
the SwissTable intern tables described above. [AsyncValue and BLAKE3
Interning](asyncvalue-and-blake3-interning.md) describes the BLAKE3 digest path that feeds the
SwissTable family.
