# Symbol Tables & Hash Maps

nvlink uses two distinct hash table implementations that serve different subsystems within the binary. The **linker hash table** (rooted at `sub_4489C0`) is a 112-byte open-addressing structure with bitmap-tracked slots, three hash modes, and chain-of-indices buckets terminated by `0xFFFFFFFF`. It handles all core linker lookups: symbols by name, options by key, and sections by name. The **FNV-1a hash table** (rooted at `sub_A4B5E0`) lives in the embedded ptxas code, uses standard FNV-1a hashing on keys of 4 or 8 bytes, stores nodes as linked-list chains within each bucket, and provides opcode-to-descriptor lookups for instruction selection. This page documents both implementations at reimplementation depth.

## Key Functions

### Linker Hash Table

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_448840` | `LinkerHash::init` | 560 B | Allocates 112-byte struct, bucket array, slot array, bitmap |
| `sub_4489C0` | `LinkerHash::create` | 112 B | Wraps init, detects mode from hash/compare function pointers |
| `sub_449A80` | `LinkerHash::lookup` | 592 B | Lookup by key, dispatches on mode, returns value or 0 |
| `sub_448E70` | `LinkerHash::insertOrUpdate` | 3,728 B | Insert key-value pair; returns old value on update; triggers resize |
| `sub_44E000` | `murmur3_string_hash` | 336 B | MurmurHash3-based string hash for mode 0 |
| `sub_44E180` | `strcmp_equal` | 16 B | String equality via `strcmp` for mode 0 |
| `sub_44E1C0` | `ptr_xor_shift_hash` | 32 B | `(a >> 11) ^ (a >> 8) ^ (a >> 5)` for mode 1 |
| `sub_44E1E0` | `ptr_equal` | 16 B | Pointer equality `a1 == a2` for mode 1 |
| `sub_44E120` | `identity_hash` | 8 B | Returns `a1` unchanged for mode 2 |
| `sub_44E130` | `int_equal` | 16 B | Integer equality `a1 == a2` for mode 2 |
| `sub_45CB00` | `ceil_log2` | ~48 B | Computes ceiling log2 for initial capacity |

### FNV-1a Hash Table (Embedded ptxas)

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_A4B5E0` | `FNVHash<u64>::rehash` | 420 B | Reallocates bucket array and redistributes all nodes |
| `sub_A4B770` | `FNVHash<u64>::insertOrUpdate` | 1,392 B | FNV-1a hash on 8-byte key, insert/update 168-byte node |
| `sub_A4C1D0` | `FNVHash<u32>::rehash` | ~400 B | Rehash variant for 32-bit key tables |
| `sub_A4C360` | `FNVHash<u32>::findOrCreate` | 576 B | Lookup/insert for 32-bit keys, 32-byte nodes |
| `sub_A4C7C0` | `FNVHash<u32>::findOrInsert` | 768 B | Iterator-returning variant for 32-bit key lookups |

## Linker Hash Table

### Struct Layout

`LinkerHash::init` (`sub_448840`) allocates a 112-byte structure from the arena allocator. The initial capacity is computed by `sub_45CB00` (ceiling log2): if the caller requests `n` slots, the actual capacity is `2^ceil_log2(n)`, rounded up to the next power of two. The load-factor threshold is set to `4 * capacity` entries before resize.

```
LinkerHash (112 bytes, 8-byte aligned)
=======================================================
Offset  Size  Field                Description
-------------------------------------------------------
  0      8    hash_fn              Hash function pointer (mode 0)
  8      8    compare_fn           Compare function pointer (mode 0)
 16      8    hash_fn_ctx          Hash function with context (mode 0, contextual path)
 24      8    compare_fn_ctx       Compare function with context (mode 0, contextual path)
 32      8    has_context           Non-zero if contextual hash/compare path is active
 40      4    bucket_mask          (bucket_count - 1); ANDed with hash for bucket index
 44      4    padding
 48      8    entry_count          Number of entries currently stored
 56      4    padding
 60      4    padding
 64      8    load_factor_limit    Resize threshold (initially 4 * bucket_count)
 72      4    resume_bitmap_idx    Bitmap scan resume position for next free slot
 76      4    entry_capacity       Allocated entry slots (grows by 2x)
 80      4    bitmap_capacity      Allocated bitmap DWORDs (grows by 2x)
 84      2    flags                Bit 0-1: entry array ownership
                                   Bit 2-3: bitmap array ownership
                                   Bit 4-7: mode (0=custom, 1=ptr-xor, 2=int-direct)
 86      2    padding
 88      8    entries              Pointer to 16-byte entry array (key, value pairs)
 96      8    bitmap               Pointer to DWORD bitmap tracking occupied slots
104      8    buckets              Pointer to bucket pointer array (bucket_count entries)
```

### Hash Modes

`LinkerHash::create` (`sub_4489C0`) wraps `LinkerHash::init` and then inspects the hash/compare function pointers to set optimized mode flags in the `flags` field at offset 84. The mode determines how `lookup` and `insertOrUpdate` compute bucket indices and compare keys.

| Mode | Bits [7:4] | Hash function | Compare | Used for |
|---|---|---|---|---|
| 0 (custom) | `0x00` | Caller-supplied function pointer at offset 0 or 16 | Caller-supplied at offset 8 or 24 | String keys (option names, section names, symbol names) |
| 1 (ptr-xor-shift) | `0x01` | `(key >> 11) ^ (key >> 8) ^ (key >> 5)` inlined | `key1 == key2` inlined | Pointer-keyed tables (address-to-object maps) |
| 2 (int-direct) | `0x02` | `key` used directly as hash | `key1 == key2` inlined | Integer-keyed tables (ID lookups) |

Mode detection in `sub_4489C0`:

```c
LinkerHash *LinkerHash_create(hash_fn_t hash, compare_fn_t compare, uint32_t hint) {
    LinkerHash *ht = LinkerHash_init(hint);     // sub_448840
    ht->hash_fn    = hash;
    ht->compare_fn = compare;

    // Detect integer-direct mode: identity hash + int equality
    if (compare == int_equal && hash == identity_hash) {
        ht->flags = (ht->flags & 0xF00F) | 0x0020;   // mode = 2
    }
    // Detect pointer-xor-shift mode: xor-shift hash + pointer equality
    if (compare == ptr_equal && hash == ptr_xor_shift_hash) {
        ht->flags = (ht->flags & 0xF00F) | 0x0010;   // mode = 1
    }
    // Otherwise mode bits stay 0x00 -> custom hash/compare via function pointers
    return ht;
}
```

### Entry Array

Entries are stored in a flat array of 16-byte key-value pairs:

```
Entry (16 bytes)
================
Offset  Size  Field
  0      8    key     (string pointer, integer, or address depending on mode)
  8      8    value   (associated data -- typically a pointer to a larger record)
```

The entry array is indexed by a global slot number. Free slots are tracked by a bitmap (one bit per slot). The bitmap is an array of `uint32_t` values where bit `i` in DWORD `j` corresponds to slot `32*j + i`. A set bit means the slot is occupied.

### Bucket Chains

Each bucket in the `buckets` array is either `NULL` (empty bucket) or a pointer to a dynamically allocated index chain. The chain is a variable-length array of `uint32_t` slot indices terminated by `0xFFFFFFFF`:

```
BucketChain (variable length)
=============================
Offset  Size  Field
  0      4    allocated_capacity   Max entries before realloc (not counting terminator)
  4      4    slot_index[0]        Index into the entry array
  8      4    slot_index[1]        ...
  ...
  N      4    0xFFFFFFFF           Terminator sentinel
```

When a bucket chain fills to capacity, it is reallocated with `2 * old_capacity + 2` slots. The old chain is freed and its contents copied into the new allocation.

### Lookup: sub\_449A80

The lookup function dispatches on the mode stored in `flags` bits [7:4]:

```c
void *LinkerHash_lookup(LinkerHash *ht, uint64_t key) {
    int mode = (ht->flags >> 4) & 0x0F;

    uint32_t hash_val;
    if (mode == 1) {
        // Pointer XOR-shift: inlined
        hash_val = (key >> 11) ^ (key >> 8) ^ (key >> 5);
    } else if (mode == 2) {
        // Integer direct: key IS the hash
        hash_val = (uint32_t)key;
    } else {
        // Custom: call through function pointer
        if (ht->has_context)
            hash_val = ht->hash_fn_ctx(key);
        else
            hash_val = ht->hash_fn(key);
    }

    uint32_t bucket_idx = ht->bucket_mask & hash_val;
    uint32_t *chain = ht->buckets[bucket_idx];
    if (!chain) return NULL;

    // Walk the chain, skipping the capacity header at chain[0]
    uint32_t *p = chain;
    while (1) {
        uint32_t slot = *++p;
        if (slot == 0xFFFFFFFF) break;

        Entry *e = &ht->entries[slot];
        if (mode == 0) {
            // Custom compare
            if (ht->has_context) {
                if (ht->compare_fn_ctx(e->key, key)) return e->value;
            } else {
                if (ht->compare_fn(e->key, key)) return e->value;
            }
        } else {
            // Modes 1 and 2: direct pointer/integer equality
            if (e->key == key) return e->value;
        }
    }
    return NULL;  // not found
}
```

### Insert/Update: sub\_448E70

`LinkerHash::insertOrUpdate` first performs a lookup using the same three-mode dispatch. If the key is found, the existing value is replaced and the old value is returned. If not found, a free slot is located via bitmap scanning, the entry is written, and the bucket chain is extended.

The free-slot search uses `_BitScanForward` (BSF instruction) on the inverted bitmap DWORDs. It resumes from the last-used bitmap DWORD index (`resume_bitmap_idx` at offset 72), wrapping around to DWORD 0 if no free slot is found in the upper range. If all bitmap DWORDs are exhausted, the bitmap array is doubled.

After insertion, the XOR-hash accumulator at offset 56 is updated (`entry_xor_hash ^= hash_val`) and the entry count at offset 48 is incremented. If `entry_count > load_factor_limit`, a full rehash is triggered:

1. A new bucket array is allocated with `2 * bucket_count + 1` buckets (making `bucket_mask` cover one more bit).
2. All old bucket chains are freed.
3. Every occupied entry (found via bitmap scan) is re-hashed and inserted into the new bucket array.

The resize doubles the capacity of bucket chains, the entry array, and the bitmap array as needed throughout this process. All allocations go through the arena allocator (`sub_4307C0`), with realloc handled by `sub_4313A0` when the array was arena-owned or by fresh allocation plus copy when the array was externally provided (tracked by flags bits 0-3).

### String Hash: MurmurHash3\_x86\_32

The custom string hash function at `sub_44E000` (336 bytes) is a faithful implementation of Austin Appleby's MurmurHash3\_x86\_32 algorithm, adapted for null-terminated C strings. Every constant, rotation amount, and mixing step matches the [reference implementation](https://github.com/aappleby/smhasher/blob/master/src/MurmurHash3.cpp) exactly. The seed is hardcoded to `0`.

#### Algorithm Constants

| Constant | Hex | Signed 32-bit | Role |
|---|---|---|---|
| c1 | `0xCC9E2D51` | `-862048943` | Body mix: first multiply on k |
| c2 | `0x1B873593` | `461845907` | Body mix: second multiply on k |
| m | `5` | `5` | Body mix: multiply on h |
| n | `0xE6546B64` | `-430675100` | Body mix: additive constant on h |
| fmix1 | `0x85EBCA6B` | `-2048144789` | Finalization: first multiply |
| fmix2 | `0xC2B2AE35` | `-1028477387` | Finalization: second multiply |

These are identical to the reference MurmurHash3\_x86\_32 constants. The rotation widths are 15 (on k) and 13 (on h).

#### Body: 4-byte Block Processing

The main loop reads 4 bytes at a time as a little-endian `uint32_t` and mixes them into the running hash `h`. Because the input is a null-terminated string (not a length-prefixed buffer), the loop condition checks all four bytes for NUL instead of comparing against a pre-computed block count:

```c
uint32_t murmur3_string_hash(const char *str) {
    uint32_t h = 0;          // seed = 0
    uint32_t len = 0;        // accumulated length for finalization

    // Body: process 4 bytes at a time
    while (str[0] && str[1] && str[2] && str[3]) {
        uint32_t k = *(uint32_t *)str;      // little-endian load
        k *= 0xCC9E2D51;                    // c1
        k = rotl32(k, 15);
        k *= 0x1B873593;                    // c2
        h ^= k;
        h = rotl32(h, 13);
        h = h * 5 + 0xE6546B64;            // 5h - 430675100
        len += 4;
        str += 4;
    }
```

The decompiled form at `sub_44E000` encodes the body as a `while(1)` loop with per-byte NUL checks after each 4-byte block, matching this logic exactly. The variable `v3` is `h`, `v2` tracks `len`, and `v7` is the advancing string pointer.

#### Tail: Remaining 1-3 Bytes

After the main loop exits (because one of the next four bytes is NUL), the function determines how many tail bytes remain (1, 2, or 3) by probing successive bytes. The tail bytes are packed into a `uint32_t` in little-endian order using a right-to-left byte-shift loop, then mixed with the same c1/c2 constants but without the rotate-13 or multiply-by-5 step:

```c
    // Tail: 1-3 remaining bytes
    int tail_len = 0;
    if (str[0]) { tail_len = 1;
        if (str[1]) { tail_len = 2;
            if (str[2]) tail_len = 3;
        }
    }
    if (tail_len > 0) {
        uint32_t k = 0;
        for (int i = tail_len - 1; i >= 0; --i)
            k = (k << 8) | str[i];          // pack little-endian
        len += tail_len;
        k *= 0xCC9E2D51;                    // c1
        k = rotl32(k, 15);
        k *= 0x1B873593;                    // c2
        h ^= k;
    }
```

In the decompiled code this appears as the `LABEL_4`/`LABEL_20` paths: `v1` holds `tail_len`, the `do { v4 = byte | (v4 << 8); }` loop packs the bytes, and the expression `461845907 * __ROL4__(-862048943 * v4, 15)` is the c1-rotate-c2 mix.

#### Finalization: fmix32

The finalization avalanche step is applied after XOR-ing in the total byte length. This is the standard `fmix32` from MurmurHash3, which ensures that every bit of the output depends on every bit of the input:

```c
    // Finalization: fmix32
    h ^= len;                               // fold in total length
    h ^= h >> 16;
    h *= 0x85EBCA6B;                        // fmix1
    h ^= h >> 13;
    h *= 0xC2B2AE35;                        // fmix2
    h ^= h >> 16;
    return h;
}
```

The decompiled code expresses `fmix32` as a single deeply nested return expression. Expanding `v2 = len`, `v3 = h` (pre-finalization):

```
t0 = v2 ^ v3                               // h ^= len
t1 = t0 ^ (t0 >> 16)                       // h ^= h >> 16
t2 = t1 * 0x85EBCA6B   (= -2048144789)     // h *= fmix1
t3 = t2 ^ (t2 >> 13)                       // h ^= h >> 13
t4 = t3 * 0xC2B2AE35   (= -1028477387)     // h *= fmix2
t5 = t4 ^ (t4 >> 16)                       // h ^= h >> 16  (return value)
```

All six steps match the reference fmix32 exactly.

#### Spec Compliance Verification

| Property | Reference MurmurHash3\_x86\_32 | nvlink `sub_44E000` | Match |
|---|---|---|---|
| Seed | Caller-supplied | Hardcoded `0` | Restricted |
| c1 | `0xCC9E2D51` | `0xCC9E2D51` | Yes |
| c2 | `0x1B873593` | `0x1B873593` | Yes |
| k rotation | `rotl32(k, 15)` | `__ROL4__(k, 15)` | Yes |
| h rotation | `rotl32(h, 13)` | `__ROL4__(h, 13)` | Yes |
| Body combine | `h = h * 5 + 0xE6546B64` | `5 * h - 430675100` | Yes (same value) |
| Tail packing | Little-endian byte accumulation | `(k << 8) \| byte` right-to-left loop | Yes |
| fmix multiply 1 | `0x85EBCA6B` | `-2048144789` (= `0x85EBCA6B`) | Yes |
| fmix multiply 2 | `0xC2B2AE35` | `-1028477387` (= `0xC2B2AE35`) | Yes |
| fmix shifts | `>> 16`, `>> 13`, `>> 16` | `>> 16`, `>> 13`, `>> 16` | Yes |
| Input model | `(key, len)` pair | Null-terminated string | Adapted |

The only deviation from the reference spec is the input model: standard MurmurHash3 takes a `(const void *key, int len, uint32_t seed)` triple, while nvlink's version walks a null-terminated string, accumulating `len` as a side effect. This means it cannot hash strings containing embedded NUL bytes, which is not a concern for linker symbol names. The seed is fixed at `0`, eliminating the caller parameter.

### Usage in the Linker

The linker hash table is the primary associative container throughout the linker-side code. Key use sites:

| Caller | Keys | Values | Mode |
|---|---|---|---|
| Option parser (`sub_42DFE0`) | Option name strings | Option record pointers | 0 (string) |
| Symbol table | Symbol name strings | Symbol record pointers | 0 (string) |
| Section name table (`0x466620`) | Section name strings | Section record pointers | 0 (string) |
| Address-to-object maps | Pointers/addresses | Object record pointers | 1 (ptr-xor) |
| ID lookup tables | Integer identifiers | Associated records | 2 (int-direct) |

The option parser creates two hash tables (one for long names, one for short names) via `sub_4489C0` during `sub_42DFE0`, with `sub_44E000`/`sub_44E180` as the hash/compare pair.

## FNV-1a Hash Table (Embedded ptxas)

### FNV-1a Algorithm

The embedded ptxas uses the standard FNV-1a (Fowler-Noll-Vo) hash for all its internal hash tables. The algorithm operates byte-by-byte on the key:

```c
uint32_t fnv1a_hash(const void *key, size_t len) {
    uint32_t h = 0x811C9DC5;    // FNV offset basis (2166136261)
    const uint8_t *p = key;
    for (size_t i = 0; i < len; i++) {
        h ^= p[i];              // XOR byte into low bits
        h *= 16777619;          // multiply by FNV prime (0x01000193)
    }
    return h;
}
```

In `sub_A4B770`, the FNV-1a is applied to an 8-byte `uint64_t` key. The decompiler unrolls this into 8 nested XOR-multiply operations:

```
h = 0x811C9DC5
h = (h ^ byte0) * 16777619
h = (h ^ byte1) * 16777619
h = (h ^ byte2) * 16777619
...
h = (h ^ byte7) * 16777619
```

For the `uint32_t` variants (`sub_A4C360`, `sub_A4C7C0`), only 4 bytes are processed.

### Table Struct Layout

The FNV hash table control structure lives at a fixed offset within a parent object (e.g., `NVInstrFormat` at offset +120). It is accessed through pointer arithmetic rather than having a standalone allocation:

```
FNVHashTable control block (32 bytes)
=====================================
Offset  Size  Field
  0      8    allocator_ref       Ref-counted pointer to arena allocator
  8      4    entry_count         Total entries stored across all buckets
 12      4    collision_count     Sum of (bucket_size - 1) across all non-empty buckets
 16      8    buckets             Pointer to bucket array
 24      8    bucket_count        Number of buckets (always a power of 2 due to 4x growth)
```

### Bucket Layout

Each bucket is a 24-byte structure containing a singly-linked list of nodes:

```
FNVBucket (24 bytes)
====================
Offset  Size  Field
  0      8    head                Pointer to first node (NULL if empty)
  8      8    tail                Pointer to last node (for O(1) append)
 16      4    count               Number of nodes in this bucket
 20      4    padding
```

Bucket index is computed as: `hash % bucket_count`. The use of modulus (not bitmask) is notable -- the bucket count is not required to be a power of two, although in practice the 4x rehash growth produces powers of two from the initial size of 8.

### Node Layout: uint64\_t Key Variant

For `sub_A4B770` (opcode-to-descriptor lookups), each node is a 168-byte allocation:

```
FNVNode<uint64_t> (168 bytes)
=============================
Offset  Size  Field
  0      8    next                Pointer to next node in chain (NULL = end)
  8      8    key                 uint64_t key (opcode ID)
 16    144    value_data          Copied from the 280-byte source record (first 144 bytes)
160      4    cached_hash         FNV-1a hash of the key (stored for rehash)
164      4    padding
```

The value data is copied in bulk using SSE `movdqu` instructions (16 bytes at a time) during both insert and update operations.

### Node Layout: uint32\_t Key Variant

For `sub_A4C360` and `sub_A4C7C0`, nodes are 32 bytes:

```
FNVNode<uint32_t> (32 bytes)
============================
Offset  Size  Field
  0      8    next                Pointer to next node in chain
  8      4    key                 uint32_t key
 12      4    padding
 16      8    value_ptr           Pointer to value (160-byte allocation via sub_4FDC30)
 24      4    cached_hash         FNV-1a hash of the key
 28      4    padding
```

### Insert/Update: sub\_A4B770

The insert path works as follows:

1. If the bucket array is `NULL` (table never used), call `rehash(table, 8)` to allocate the initial 8 buckets.
2. Compute FNV-1a hash of the 8-byte key.
3. Find the target bucket: `buckets[hash % bucket_count]`.
4. Walk the chain comparing `node->key == search_key`:
   - **Found**: Update all 144 bytes of value data in-place using SSE copies. Handle variable-length sub-arrays stored at offsets 40-56 by resizing if needed. Return pointer to updated node.
   - **Not found**: Continue to step 5.
5. Allocate a 168-byte node: first try the freelist at `*(allocator+8)`; if empty, call the allocator vtable to get fresh memory.
6. Initialize the node: set key, copy 144 bytes of value data, store the cached hash at offset 160.
7. Insert the node into the bucket chain (prepend to head, or set as both head and tail if bucket was empty).
8. Increment `bucket.count`, `table.collision_count`, and `table.entry_count`.
9. **Rehash check**: if `collision_count > entry_count` AND `entry_count > bucket_count / 2`, trigger `rehash(table, 4 * bucket_count)`.

### Rehash: sub\_A4B5E0

The rehash function reallocates the entire bucket array and redistributes all nodes:

```c
void FNVHash_rehash(FNVHashTable *table, uint64_t new_capacity) {
    // Reset collision counter
    table->collision_count = 0;

    // Allocate new bucket array: 24 bytes per bucket, zero-initialized
    FNVBucket *new_buckets = allocator_alloc(table->allocator, 24 * new_capacity);
    for (uint64_t i = 0; i < new_capacity; i++) {
        new_buckets[i] = (FNVBucket){NULL, NULL, 0};
    }

    // Redistribute all nodes from old buckets
    FNVBucket *old_buckets = table->buckets;
    uint64_t old_capacity = table->bucket_count;

    if (old_buckets && old_capacity > 0) {
        for (uint64_t i = 0; i < old_capacity; i++) {
            FNVNode *node = old_buckets[i].head;
            while (node) {
                FNVNode *next = node->next;

                // Remove from old bucket
                old_buckets[i].head = next;
                if (old_buckets[i].tail == node)
                    old_buckets[i].tail = NULL;
                old_buckets[i].count--;

                // Insert into new bucket using cached hash
                uint64_t new_idx = node->cached_hash % new_capacity;
                FNVBucket *dst = &new_buckets[new_idx];
                if (dst->head) {
                    node->next = dst->tail->next;
                    dst->tail->next = node;
                } else {
                    dst->head = node;
                    node->next = NULL;
                }
                dst->tail = node;
                int old_count = dst->count;
                table->collision_count += old_count;
                dst->count = old_count + 1;

                node = next;
            }
        }
        // Free old bucket array
        allocator_free(table->allocator, old_buckets);
    }

    table->buckets = new_buckets;
    table->bucket_count = new_capacity;
}
```

The rehash uses the `cached_hash` stored in each node (at offset +160 for 8-byte keys, +24 for 4-byte keys) to avoid recomputing the FNV-1a hash. The new bucket index is `cached_hash % new_capacity`.

### Load Factor Strategy

The FNV hash table uses a collision-count heuristic rather than a simple load-factor threshold:

| Condition | Action |
|---|---|
| `collision_count > entry_count` AND `entry_count > bucket_count / 2` | Rehash with `4 * bucket_count` |
| Initial allocation (buckets == NULL) | Allocate 8 buckets |

The collision count tracks the cumulative excess: each time a node is inserted into a bucket that already has `n` nodes, the collision count increases by `n`. This means the rehash triggers when the average chain length exceeds 2 (approximately), provided at least half the buckets are in use. The 4x growth factor is aggressive, designed to keep chains very short for the performance-critical opcode lookup path.

### Usage in Embedded ptxas

The FNV hash tables serve the instruction descriptor subsystem within the embedded ptxas:

| Parent struct | Offset | Key type | Value type | Lookup function |
|---|---|---|---|---|
| `NVInstrFormat` | +120 | `uint32_t` (opcode ID) | Opcode descriptor ptr | `sub_A49220` |
| `NVInstrFormat` | +152 | `uint32_t` (opcode ID) | Sub-opcode descriptor ptr | `sub_A492B0` |
| Instruction table builder | varies | `uint64_t` (compound key) | 168-byte descriptor record | `sub_A4B770` |

`sub_A49220` (`lookupOpcodeDesc`) is called from nearly every instruction-processing path in ptxas. It guards with `BUG()` if the table is empty (offset +136 == 0), then performs an FNV-1a hash on the opcode ID and walks the chain. `sub_A492B0` (`lookupSubopDesc`) uses a parallel table at a different offset and returns 0 on miss instead of asserting.

## Comparison of the Two Implementations

| Property | Linker Hash | FNV-1a Hash |
|---|---|---|
| Location in binary | `0x448840`-`0x449C40` | `0xA4B5E0`-`0xA4C7C0` |
| Struct size | 112 bytes | 32 bytes (control block) |
| Bucket structure | Pointer to index chain (variable-length uint32 array) | 24-byte struct with head/tail/count linked list |
| Chain type | Array of slot indices terminated by `0xFFFFFFFF` | Singly-linked list of node pointers |
| Hash algorithm | MurmurHash3 (strings), XOR-shift (pointers), identity (ints) | FNV-1a (all key types) |
| Key storage | 16-byte entry array (key + value, flat) | In-node (key at offset 8) |
| Value storage | 8-byte pointer in flat entry array | In-node (embedded 144 bytes) or pointer to external allocation |
| Free slot tracking | Bitmap with `_BitScanForward` | Freelist (singly-linked via node offset 0) |
| Resize trigger | `entry_count > load_factor_limit` (4x initial capacity) | `collision_count > entry_count` AND `entry_count > bucket_count/2` |
| Growth factor | 2x bucket count + 1 | 4x bucket count |
| Allocator | Arena allocator (`sub_4307C0`) | Ref-counted allocator with vtable |
| Thread safety | Not inherent (caller must synchronize) | Not inherent (caller must synchronize) |
