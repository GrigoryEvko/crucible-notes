# Hash Tables & Bitvectors

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas contains two independent hash map implementations and a dedicated bitvector library. These three container abstractions underpin nearly every subsystem in the compiler -- from the PTX parser's symbol tables to the optimizer's liveness analysis to the code generator's instruction deduplication cache. This page documents their binary-level layouts, hash algorithms, SIMD acceleration, and usage patterns.

## Overview

| Container | Object Size | Hash Algorithm | Address Range | Callers |
|-----------|-------------|----------------|---------------|---------|
| General hash map | 112 bytes | MurmurHash3 (strings), pointer-shift, or identity | `0x425B20`--`0x42D850` | 2800+ (`sub_426150`) |
| CFG hash map | 40 bytes (header) | FNV-1a (32-bit) | `0xBDED20`--`0xBDFB10` | ~80 |
| Bitvector | 20 bytes (header) | N/A | `0xBDBA60`--`0xBDE150` | 500+ |

The general hash map is a self-contained 112-byte object used for string-keyed lookups (intrinsic tables, PTX directive dispatch, ELF section names), pointer-keyed caches (instruction deduplication), and integer-keyed registries (opcode tables). The CFG hash map is a separate, purpose-built implementation for graph edge storage with embedded sub-hash tables for multi-edge blocks. The bitvector library provides 17+ SSE2-accelerated operations optimized for the iterative dataflow workloads that dominate ptxas compile time.

---

## General Hash Map (112 bytes)

### Construction

The constructor `sub_425CA0` takes three arguments: a hash function pointer, a compare function pointer, and an initial capacity hint. It delegates allocation to `sub_425B20`, which allocates the 112-byte map object, a bucket pointer array, an entries array, and a used-bits bitmap.

```c
HashMap* HashMap_create(HashFn hash, CmpFn compare, uint32_t capacity_hint);
// sub_425CA0(hash_fn, cmp_fn, initial_capacity)
```

After allocation, the constructor detects two well-known function-pointer pairs and sets fast-path mode flags in the flags field at offset +84 (bits 4-7):

| Mode | Bits 4-7 | Hash Function | Compare Function | Key Semantics |
|------|----------|---------------|------------------|---------------|
| 0 (custom) | `0x00` | User-supplied | User-supplied | String or arbitrary (MurmurHash3 typical) |
| 1 (pointer) | `0x10` | `sub_4277F0` | `sub_427810` | Raw pointer identity |
| 2 (integer) | `0x20` | `sub_427750` | `sub_427760` | 32/64-bit integer identity |

Mode detection is a compile-time optimization: the insert and lookup fast paths test the mode bits and branch directly to specialized hash/compare code, avoiding the overhead of indirect function calls through the hash/compare pointers.

### Object Layout (112 bytes)

```
GeneralHashMap (112 bytes, allocated by sub_425B20)
  +0    ptr     hash_fn          // Hash function pointer (or NULL for mode 1/2)
  +8    ptr     compare_fn       // Compare function pointer
  +16   ptr     (reserved)       // Additional function pointer slot
  +24   ptr     (reserved)       // Additional function pointer slot
  +32   u32     has_custom_cmp   // Non-zero if custom compare supplied
  +40   u32     bucket_mask      // (bucket_count - 1); power-of-2 mask
  +48   u32     entry_count      // Number of occupied entries
  +52   u32     (padding)
  +56   u32     (reserved)
  +60   u32     (reserved)
  +64   u32     load_threshold   // entry_count threshold triggering resize
  +68   u32     (reserved)
  +72   u32     first_free       // Free-slot tracking index
  +76   u32     entry_capacity   // Allocated capacity of entries array
  +80   u32     bitmap_capacity  // Capacity of used-bits bitmap (in uint32_t words)
  +84   u16     flags            // Bits 4-7: hash mode (0=custom, 1=pointer, 2=integer)
                                 // Bits 0-1: bitmap/entry state flags
  +86   u16     (padding)
  +88   ptr     entries          // Array of 16-byte entries [key (8B), value (8B)]
  +96   ptr     used_bitmap      // Bitmap tracking which entry slots are occupied
  +104  ptr     bucket_array     // Array of pointers to index lists per bucket
```

### Entry Format (16 bytes)

Each entry in the entries array at +88 is 16 bytes:

```
Entry (16 bytes, at entries + 16 * index)
  +0    ptr/u64   key            // Key (string pointer, raw pointer, or integer)
  +8    ptr/u64   value          // Associated value
```

Bucket chains are stored as arrays of `uint32_t` indices terminated by sentinel value `0xFFFFFFFF` (`-1`). Each bucket pointer at `bucket_array[hash & bucket_mask]` points to a null-terminated index list. Collision resolution is open addressing with index chaining -- not linked-list chaining.

### Hash Functions

**Mode 0 (Custom / String) -- MurmurHash3:**

String-keyed maps use `sub_427630` (273 bytes, 73 callers), which implements MurmurHash3_x86_32 with the standard mixing constants:

```c
uint32_t MurmurHash3_x86_32(const char* key, int len) {
    // sub_427630
    uint32_t h = seed;
    const uint32_t c1 = 0xCC9E2D51;  // -862048943
    const uint32_t c2 = 0x1B873593;  //  461845907

    // Body: process 4 bytes at a time
    for (int i = 0; i < len / 4; i++) {
        uint32_t k = ((uint32_t*)key)[i];
        k *= c1;
        k = ROTL32(k, 15);
        k *= c2;
        h ^= k;
        h = ROTL32(h, 13);
        h = h * 5 - 430675100;  // 0xE6546B64
    }

    // Tail: remaining 1-3 bytes
    // ... standard MurmurHash3 tail handling ...

    // Finalization
    h ^= len;
    h ^= h >> 16;
    h *= 0x85EBCA6B;  // -2048144789
    h ^= h >> 13;
    h *= 0xC2B2AE35;  // -1028477387
    h ^= h >> 16;
    return h;
}
```

**Mode 1 (Pointer):**

Pointer-keyed maps use a shift-XOR hash that destroys the alignment pattern common in heap pointers:

```c
uint32_t pointer_hash(void* key) {
    uintptr_t p = (uintptr_t)key;
    return (p >> 11) ^ (p >> 8) ^ (p >> 5);
}
```

The right-shifts at 5, 8, and 11 bits fold the significant bits of a 64-bit heap address into a 32-bit range. The compare function (`sub_427810`) performs raw pointer equality.

**Mode 2 (Integer):**

Integer-keyed maps use the key value directly as the hash (identity hash). The compare function (`sub_427760`) performs integer equality. Bucket selection is `key & bucket_mask`.

### Insert Operation (`sub_426150`)

`sub_426150` (2534 bytes, 2800 callers) is the most heavily used hash map function. Its signature:

```c
void* HashMap_put(HashMap* map, void* key, void* value);
// Returns: previous value if key existed, NULL if new insertion
```

Algorithm:

1. **Hash computation.** Branch on mode bits at +84 to select hash function.
2. **Bucket lookup.** Compute `bucket = hash & *(map+40)`. Load index list from `bucket_array[bucket]`.
3. **Key scan.** Walk the index list. For each index `i` where `i != 0xFFFFFFFF`, compare `entries[i].key` with the target key using the mode-specific compare.
4. **Hit:** Swap `entries[i].value` with the new value. Return the old value.
5. **Miss:** Find a free slot via the used-bits bitmap at +96. Set `entries[free].key = key`, `entries[free].value = value`. Append the free index to the bucket's index list. Increment `entry_count` at +48.
6. **Resize check.** If `entry_count > load_threshold`, double the capacity: allocate new entries array (`2 * old_capacity`) and new bucket array, rehash all entries.

### Lookup Operation (`sub_426D60`)

`sub_426D60` (345 bytes, 422 callers) is the read-only lookup:

```c
void* HashMap_get(HashMap* map, void* key);
// Returns: value if found, NULL (0) if not found
```

Same hash and scan logic as insert, but no modification path. The three mode fast paths avoid indirect calls on the hot path.

### Contains Operation (`sub_426EC0`)

`sub_426EC0` (349 bytes, 29 callers) returns `1` if the key exists, `0` otherwise. Nearly identical to lookup but returns a boolean rather than the value.

### Resize Policy

The resize doubles capacity when `entry_count` exceeds `load_threshold`. The threshold is set to `4 * bucket_count` during initialization (from `sub_425B20`: `v6[8] = 4 << log2(capacity)`), meaning the default load factor limit is approximately 4.0 entries per bucket. Initial bucket count is rounded up to the next power of two from the capacity hint (minimum 1). The constructor at `sub_425B20` calls `sub_42BDF0` to compute `ceil(log2(capacity))`.

### Destructor (`sub_425D20`)

`sub_425D20` (121 bytes, 63 callers) frees the entries array, used-bits bitmap, bucket array, and the 112-byte map object itself. All four allocations are returned to the pool allocator.

### Iteration

Hash map iteration (used by `sub_425DB0`, 9 callers) walks the entries array linearly, testing the used-bits bitmap to identify occupied slots. There is no guaranteed iteration order -- the order depends on insertion history and resize operations.

### Usage Examples

| Subsystem | Hash Mode | Key Type | Purpose |
|-----------|-----------|----------|---------|
| Intrinsic table (`sub_5AB660`) | Custom (MurmurHash3) | String | 608 intrinsic name lookups (capacity 0x80) |
| PTX opcode dispatch | Custom (MurmurHash3) | String | Named PTX opcodes at `ctx+808` |
| SM version backends | Custom (MurmurHash3) | String | `sm_XX`, `compute_XX`, `lto_XX` registries |
| Instruction dedup (`sub_737760`) | Pointer | Pointer | Avoid re-encoding identical instructions |
| Opcode hash tables | Integer | Integer | Fast opcode-to-handler dispatch |
| File offset cache | Custom | String | Cache file offsets for `#line` directives |
| Symbol table (`sub_621480`) | Custom (MurmurHash3) | String | Named symbol lookups at `ctx+30016` |
| Per-function disable list | Custom (FNV-1a) | String | Function-specific pass disable at `ctx+120->+1128` |

---

## CFG Hash Map (Graph Edges)

The CFG edge hash map is a completely separate implementation from the general hash map, located in the address range `0xBDED20`--`0xBDFB10`. It is designed specifically for graph edge storage -- mapping block indices to successor/predecessor sets -- and has a different object layout, hash function, and collision resolution strategy.

### Object Layout

```
CFGHashMap (40 bytes header)
  +0    ptr     first_free_node   // Free list for node recycling
  +8    ptr     node_arena        // Pool allocator for new nodes
  +16   ptr     bucket_array      // Array of 24-byte bucket headers
  +24   u64     num_buckets       // Power of two, initial = 8
  +32   i32     total_elements    // Total entries across all buckets
  +36   i32     num_unique_keys   // Distinct keys inserted
```

Two distinct hash map configurations exist for different node sizes:

### Full Node (64 bytes) -- Successor Edge Map

Used by `sub_BDED20` (12KB) for the successor edge hash map at Code Object +648. Each node represents a block's successor edge set with an optional sub-hash table for multi-successor blocks:

```
FullNode (64 bytes)
  +0    ptr     next              // Chain link within bucket
  +8    i32     key               // Block index (bix)
  +12   i32     value_info        // Edge count or flags
  +16   ptr     value_array       // Pointer to sub-array of successor indices
  +24   i32     value_count       // Number of successors in sub-array
  +28   i32     (padding)
  +32   ptr     sub_hash_data     // Embedded sub-hash for multi-edge blocks
  +40   u64     sub_hash_size     // Sub-hash capacity
  +48   u64     (reserved)
  +56   u32     cached_hash       // Cached FNV-1a hash of key
  +60   u32     (padding)
```

### Simple Node (16 bytes) -- Backedge Set

Used by `sub_BDF480` (10KB) for the backedge hash map at Code Object +680. Each node is a minimal key-hash pair for set membership testing:

```
SimpleNode (16 bytes)
  +0    ptr     next              // Chain link within bucket
  +8    i32     key               // Block index
  +12   u32     cached_hash       // Cached hash
```

### Bucket Header (24 bytes)

Both full and simple node maps use the same bucket header:

```
Bucket (24 bytes)
  +0    ptr     head              // First node in collision chain
  +8    ptr     tail              // Last node in collision chain
  +16   i32     count             // Number of nodes in this bucket
  +20   i32     (padding)
```

### FNV-1a Hash Function

All CFG hash lookups use FNV-1a (32-bit) with standard parameters, confirmed across 50+ call sites:

```c
uint32_t fnv1a_hash_u32(uint32_t key) {
    uint32_t hash = 0x811C9DC5;                    // FNV offset basis
    hash = 16777619 * (hash ^ (key & 0xFF));       // byte 0
    hash = 16777619 * (hash ^ ((key >> 8) & 0xFF));  // byte 1
    hash = 16777619 * (hash ^ ((key >> 16) & 0xFF)); // byte 2
    hash = 16777619 * (hash ^ ((key >> 24) & 0xFF)); // byte 3
    return hash;
}
```

Bucket selection: `bucket = hash & (num_buckets - 1)`.

This appears inline in the decompiled code as:

```c
v9 = 16777619 * (HIBYTE(*a3)
   ^ (16777619 * ((unsigned __int8)BYTE2(*a3)
   ^ (16777619 * (BYTE1(v8)
   ^ (16777619 * ((unsigned __int8)v8 ^ 0x811C9DC5)))))));
v11 = v9 & (a2[3] - 1);   // bucket index
```

### Resize Policy

The CFG hash map resizes when `total_elements > num_unique_keys` (load factor > 1.0). New capacity is `4 * old_bucket_count`. Both `sub_BDED20` and `sub_BDF480` allocate a new 192-byte bucket array (8 buckets * 24 bytes/bucket = 192) during initial creation, then redistribute nodes during resize.

The resize algorithm:

1. Allocate new bucket array (192 bytes for 8 buckets).
2. Walk all old buckets, removing each node from old chains.
3. Re-hash each node: `new_bucket = cached_hash & (new_num_buckets - 1)`.
4. Insert at head or tail of new bucket chain.
5. Free old bucket array via pool allocator (vtable dispatch at allocator `+32` for free).

### Edge Map Operations

**Insert/Find (`sub_BDED20`, `sub_BDF480`):**
1. Compute FNV-1a hash of the block index key.
2. Index into bucket array: `bucket = hash & (num_buckets - 1)`.
3. Walk the collision chain comparing keys.
4. If found: return existing node.
5. If not found: allocate node from arena, initialize, insert into bucket, increment counters.

**Erase (`sub_BDE6C0`, 3KB):**
Removes an entry from the successor edge map and recursively erases successor edges for blocks that become unreachable (single-predecessor blocks whose only predecessor was removed). This recursive cleanup maintains CFG consistency during block deletion.

**Print (`sub_BDE8B0`, 2KB):**
Iterates a block's successors and prints `"\tbix%d -> bix%d\n"` for each edge. Used by the CFG debug dump infrastructure.

### Multi-Edge Sub-Hash

For blocks with many successors (switch statements, computed branches), the full node at `+32` contains a pointer to an embedded sub-hash table. This sub-hash maps successor block indices within a single node's edge set, enabling O(1) lookup of whether a specific successor edge exists. The sub-hash uses the same 24-byte bucket structure as the outer hash map.

### Storage Locations

| Code Object Offset | Field | Map Type | Node Size |
|---------------------|-------|----------|-----------|
| +648 | `succ_map` | Successor edges | 64 bytes (full) |
| +680 | `backedge_map` | Backedge set | 16 bytes (simple) |

---

## Bitvector Library

The bitvector library at `0xBDBA60`--`0xBDE150` is the most performance-critical infrastructure in ptxas. It supports 17+ operations, all SSE2-accelerated with manual alignment handling. The library is the backbone of liveness analysis (6 dedicated phases), dominance computation, register interference detection, and dead code elimination.

### Object Layout (20 bytes)

```c
struct BitVector {          // 20 bytes
    uint32_t* data;         // +0:  pointer to word array (heap-allocated)
    int32_t   word_count;   // +8:  number of 32-bit words in use
    int32_t   capacity;     // +12: allocated words (>= word_count)
    int32_t   bit_count;    // +16: number of valid bits
};
```

Word count derivation: `word_count = (bit_count + 31) >> 5`.

Memory is allocated through the pool allocator (vtable dispatch at allocator `+24` for alloc, `+32` for free). The `allocate` function (`sub_BDBA60`) implements grow-only semantics: if the new word count exceeds the current capacity, the old buffer is freed and a new one allocated. The buffer is never shrunk.

### Bit Addressing

Individual bits are addressed by word index and bit position within the word:

```c
// Set bit i:  data[i >> 5] |= (1 << (i & 0x1F))
// Clear bit i: data[i >> 5] &= ~(1 << (i & 0x1F))
// Test bit i:  (data[i >> 5] >> (i & 0x1F)) & 1
```

### SSE2 Acceleration Pattern

All bulk operations follow a three-phase structure:

1. **Alignment prologue.** Process scalar words until the destination pointer is 16-byte aligned. The alignment count is `(-(uintptr_t)(dst_ptr) >> 2) & 3`, yielding 0-3 scalar iterations.

2. **SSE2 main loop.** Process 4 words (128 bits) per iteration using `_mm_load_si128` (aligned destination), `_mm_loadu_si128` (potentially unaligned source), and the appropriate SSE2 intrinsic (`_mm_or_si128`, `_mm_and_si128`, `_mm_andnot_si128`, `_mm_xor_si128`). The loop count is `(remaining_words) >> 2`.

3. **Scalar epilogue.** Process remaining 0-6 words individually. The decompiler shows an unrolled epilogue handling up to 6 trailing words with sequential if-chains rather than a loop.

Example from `sub_BDCF40` (`orIfChanged`):

```c
// Prologue: align dst
int align = (-(uintptr_t)(dst) >> 2) & 3;
for (int i = 0; i < align && i < word_count; i++)
    dst[i] |= src[i];

// SSE2 loop: 4 words per iteration
int sse_iters = (word_count - align) >> 2;
for (int i = 0; i < sse_iters; i++) {
    __m128i d = _mm_load_si128(&dst[aligned_offset + 4*i]);
    __m128i s = _mm_loadu_si128(&src[aligned_offset + 4*i]);
    _mm_store_si128(&dst[aligned_offset + 4*i], _mm_or_si128(d, s));
}

// Epilogue: 0-6 remaining words
```

### Operation Catalog

#### Allocation and Lifecycle

| Address | Operation | Signature | Description |
|---------|-----------|-----------|-------------|
| `sub_BDBA60` | `allocate` | `(bv*, alloc*, num_bits)` | Grow-only allocation. Sets `bit_count`, recomputes `word_count`, reallocates if capacity insufficient. |
| `sub_BDDC00` | `clear` | `(bv*, start_bit)` | Zeroes all words from `start_bit >> 5` to end. When `start_bit = 0`, equivalent to `memset(data, 0, ...)`. |
| `sub_BDCA60` | `operator=` | `(dst*, src*)` | Copy assignment. Reallocates dst if capacity insufficient, then memcpy. |

#### Bit Manipulation

| Address | Operation | Signature | Description |
|---------|-----------|-----------|-------------|
| `sub_BDBFB0` | `setBit` | `(bv*, bit_index)` | `data[i>>5] \|= (1 << (i&31))` |
| `sub_BDC0E0` | `clearBit` | `(bv*, bit_index)` | `data[i>>5] &= ~(1 << (i&31))` |
| `sub_BDC200` | `testBit` | `(bv*, bit_index) -> bool` | `(data[i>>5] >> (i&31)) & 1` |

#### Bulk Boolean Operations (SSE2)

| Address | Operation | Signature | SSE2 Intrinsic | Description |
|---------|-----------|-----------|----------------|-------------|
| `sub_BDCDE0` | `operator\|=` | `(dst*, src*)` | `_mm_or_si128` | `dst \|= src` |
| `sub_BDC5F0` | `operator&=` | `(dst*, src*)` | `_mm_and_si128` | `dst &= src`; zeroes dst tail beyond src length |
| `sub_BDDAA0` | `operator^=` | `(dst*, src*)` | `_mm_xor_si128` | `dst ^= src` |

#### Fixed-Point Detection (IfChanged variants)

These return `1` if any bit changed, `0` if the result is identical to the previous state. They are the core of iterative dataflow convergence detection.

| Address | Operation | Signature | Description |
|---------|-----------|-----------|-------------|
| `sub_BDCF40` | `orIfChanged` | `(dst*, src*) -> bool` | Scans for `(~dst & src) != 0` first, then applies `dst \|= src` from the first differing word. Returns whether any bit was newly set. |
| `sub_BDC790` | `andIfChanged` | `(dst*, src*) -> bool` | Scans for `(~src & dst) != 0` first, then applies `dst &= src`. Zeroes trailing dst words beyond src. Returns whether any bit was cleared. |

The `IfChanged` early-exit optimization: the function first scans word-by-word for the first position where a change would occur (`~dst & src` for OR, `~src & dst` for AND). If no such position exists, it returns 0 immediately without touching memory. If found, it applies the operation only from that position forward, reducing cache pollution when most blocks have already converged.

#### Three-Input Operations

| Address | Operation | Signature | Description |
|---------|-----------|-----------|-------------|
| `sub_BDC3F0` | `assignAND` | `(dst*, a*, b*)` | `dst = a & b` (SSE2 `_mm_and_si128`) |
| `sub_BDD8C0` | `assignANDNOT` | `(dst*, a*, b*)` | `dst = a & ~b` (SSE2 `_mm_andnot_si128`) |
| `sub_BDCC20` | `assignOR` | `(dst*, a*, b*)` | `dst = a \| b` (SSE2 `_mm_or_si128`) |
| `sub_BDD140` | `orWithAND` | `(dst*, a*, b*)` | `dst \|= a & b` (SSE2 `_mm_and_si128` + `_mm_or_si128`) |

#### Liveness-Specific Fused Operations

The most important bitvector operations for compiler performance are the fused transfer functions used by liveness analysis:

| Address | Operation | Signature | Description |
|---------|-----------|-----------|-------------|
| `sub_BDD300` | `orWithAndNot` | `(dst*, gen*, in*, kill*)` | `dst \|= gen \| (in & ~kill)` |
| `sub_BDD560` | `orWithAndNotIfChanged` | `(dst*, gen*, in*, kill*) -> bool` | Same as above + returns change flag |

These implement the liveness transfer function `LiveIn(B) = gen(B) | (LiveOut(B) - kill(B))` in a single SIMD pass, avoiding materialization of intermediate bitvectors.

**`orWithAndNot` inner loop (`sub_BDD300`):**

```c
// SSE2: process 4 words per iteration
*(__m128i*)(dst + offset) = _mm_or_si128(
    _mm_or_si128(
        _mm_loadu_si128(gen + offset),      // gen
        *(__m128i*)(dst + offset)),          // existing dst
    _mm_andnot_si128(
        _mm_loadu_si128(kill + offset),     // ~kill
        _mm_loadu_si128(in + offset)));     // in & ~kill
```

**`orWithAndNotIfChanged` (`sub_BDD560`):**

This function first scans for any word position where the new value would differ from the current dst:

```c
// Phase 1: scan for first change
for (int i = 0; i < word_count; i++) {
    uint32_t new_bits = gen[i] | (in[i] & ~kill[i]);
    if (~dst[i] & new_bits)
        goto apply_from_i;  // Found change at position i
}
return 0;  // No change -- already at fixed point

// Phase 2: apply from first change position
apply_from_i:
    // SSE2 loop: dst[j] |= gen[j] | (in[j] & ~kill[j])
    // ... (same three-phase SSE2 pattern) ...
    return 1;
```

This two-phase design is critical for liveness analysis performance: in the late iterations of the fixed-point solver, most blocks have already converged and the scan returns 0 without writing memory.

#### Query Operations

| Address | Operation | Signature | Description |
|---------|-----------|-----------|-------------|
| `sub_BDDD40` | `popcount` | `(bv*) -> int` | Count set bits. Uses `__popcountdi2` intrinsic per word. |
| `sub_BDBFB0` | `findLastSetWord` | `(bv*) -> int` | Scans from high word to low, returns index of last non-zero word. Returns `0xFFFFFFFF` if all zero. |
| `sub_BDDC00` | `nextSetBit` | `(bv*, start) -> int` | Find next set bit at or after `start`. Uses `tzcnt` (trailing zero count) instruction for intra-word scanning. Returns `0xFFFFFFFF` if none found. |
| `sub_BDDCB0` | `prevSetBit` | `(bv*, start) -> int` | Find previous set bit at or before `start`. Uses `bsr` (bit scan reverse). Returns `0xFFFFFFFF` if none found. |

#### Extract Operation

| Address | Operation | Signature | Description |
|---------|-----------|-----------|-------------|
| `sub_BDBD60` | `extractBits` | `(bv*, out[], start, end)` | Extract bit range `[start, end]` into output array. Handles cross-word boundaries with shift-and-mask. Supports extraction spans > 32 bits by filling multiple output words. |

The extract function handles three cases:
1. **Same word:** Both start and end are in the same 32-bit word. Single mask-and-shift.
2. **Small span (<=32 bits, two words):** Combine partial words with shift and OR.
3. **Large span (>32 bits):** Loop over full words with optional shift for non-aligned start.

### Bitvector Subset Test

The `isSubsetOf` operation tests whether bitvector A is a subset of B, i.e. `(A & ~B) == 0`. This is used by dominance queries (checking if block X is dominated by block Y). The test at `sub_1245740` referenced in the copy-prop-CSE pass performs a single-bit indexed test against the dominator bitvector: `testBit(dom_set[block], dominator_id)`.

---

## Usage Across Subsystems

### Liveness Analysis

The iterative fixed-point solver runs in reverse post-order, computing LiveIn and LiveOut bitvectors per basic block:

```
LiveOut(B) |= LiveIn(S)                    -- for each successor S: orIfChanged
LiveIn(B)  = gen(B) | (LiveOut(B) - kill(B))  -- orWithAndNotIfChanged
```

Six dedicated phases (10, 16, 19, 33, 61, 84) perform liveness + DCE. The `orWithAndNotIfChanged` fusion and early-exit optimization minimize cache traffic in later iterations when most sets have stabilized. Bitvectors for liveness are stored at Code Object +832 (R registers) and +856 (UR registers).

### Dominance

The iterative dominator computation (`sub_BE2330`, 4KB) uses bitvector intersection:

```
dom[entry] = {entry}
dom[b] = {b} union (intersection of dom[p] for all predecessors p)
```

Each block's dominator set is a bitvector indexed by block ID. Convergence uses `andIfChanged` to detect when the intersection no longer removes any dominators.

### Register Allocation

The interference graph builder (`sub_926A30`, 155KB decompiled) uses bitvector intersection to detect live range overlaps. Two registers interfere if their liveness bitvectors have overlapping set bits at any program point. The allocator at `sub_9721C0` explicitly rebuilds liveness before allocation.

### GVN-CSE

The GVN-CSE pass (phase 49) uses the general hash map with FNV-1a hashing for the value numbering table. Each hash entry is 24 bytes: `[next_ptr (8B), key (8B), value/metadata (8B)]` with chained collision resolution. The hash incorporates opcode, type, and recursively resolved value numbers of all operands.

### CFG Analysis

The CFG builder (`sub_BE0690`, 54KB) populates successor and backedge hash maps during phase 3 (`AnalyzeControlFlow`). RPO computation (`sub_BDE150`, 9KB) reads from the successor map. The DOT dumper (`sub_BE21D0`) iterates edge sets for visualization.

---

## Key Function Table

### General Hash Map

| Address | Size | Identity | Callers | Confidence |
|---------|------|----------|---------|------------|
| `sub_425B20` | 0.5KB | `HashMap::allocate` -- allocates 112-byte map + arrays | 127 (via `sub_425CA0`) | HIGH |
| `sub_425CA0` | 114B | `HashMap::create` -- constructor with hash/compare fn ptrs | 127 | HIGH |
| `sub_425D20` | 121B | `HashMap::destroy` -- frees all internal arrays + map | 63 | MEDIUM |
| `sub_425DB0` | 292B | `HashMap::clear` / iterator init | 9 | MEDIUM |
| `sub_426150` | 2.5KB | `HashMap::put` -- insert or update key/value pair | 2800 | HIGH |
| `sub_426D60` | 345B | `HashMap::get` -- lookup key, return value or 0 | 422 | HIGH |
| `sub_426EC0` | 349B | `HashMap::contains` -- test key existence | 29 | HIGH |
| `sub_427630` | 273B | `MurmurHash3_x86_32` -- string hash | 73 | HIGH |
| `sub_427750` | -- | Integer hash function (identity) | -- | HIGH |
| `sub_4277F0` | -- | Pointer hash function (shift-XOR) | -- | HIGH |
| `sub_42D850` | 2.5KB | `HashMap::insertSet` -- set-mode insert with auto-resize | 282 | HIGH |

### CFG Hash Map

| Address | Size | Identity | Confidence |
|---------|------|----------|------------|
| `sub_BDED20` | 12KB | `CFGHashMap::insertOrFind` -- full 64-byte node | HIGH |
| `sub_BDF480` | 10KB | `CFGHashMap::insertOrFind_simple` -- 16-byte node | HIGH |
| `sub_BDE6C0` | 3KB | `CFGHashMap::erase` -- recursive edge removal | MEDIUM |
| `sub_BDE8B0` | 2KB | `CFGHashMap::printEdges` -- `"bix%d -> bix%d"` | HIGH |
| `sub_BDEA50` | 4KB | `CFGHashMap::dumpRPOAndBackedges` -- debug dump | HIGH |
| `sub_BDFB10` | 24KB | `CFGHashMap::buildBlockMap` -- block array init, RPO resize | MEDIUM |
| `sub_BDDDF0` | ~2KB | `CFGHashMap::destroyAll` -- walk and free all nodes | MEDIUM |

### Bitvector Library

| Address | Size | Identity | Confidence |
|---------|------|----------|------------|
| `sub_BDBA60` | ~120B | `BitVector::allocate` | HIGH (0.90) |
| `sub_BDBFB0` | ~120B | `BitVector::findLastSetWord` | HIGH (0.90) |
| `sub_BDBD60` | ~370B | `BitVector::extractBits` | HIGH (0.88) |
| `sub_BDBFB0` | ~120B | `BitVector::setBit` | HIGH (0.90) |
| `sub_BDC0E0` | ~120B | `BitVector::clearBit` | HIGH (0.90) |
| `sub_BDC200` | ~140B | `BitVector::testBit` | HIGH (0.90) |
| `sub_BDC3F0` | ~520B | `BitVector::assignAND` | HIGH (0.90) |
| `sub_BDC5F0` | ~480B | `BitVector::operator&=` | HIGH (0.95) |
| `sub_BDC790` | ~800B | `BitVector::andIfChanged` | HIGH (0.95) |
| `sub_BDCA60` | ~280B | `BitVector::operator=` (copy) | MEDIUM (0.85) |
| `sub_BDCC20` | ~320B | `BitVector::assignOR` | HIGH (0.90) |
| `sub_BDCDE0` | ~400B | `BitVector::operator\|=` | HIGH (0.95) |
| `sub_BDCF40` | ~560B | `BitVector::orIfChanged` | HIGH (0.95) |
| `sub_BDD140` | ~480B | `BitVector::orWithAND` | HIGH (0.90) |
| `sub_BDD300` | ~490B | `BitVector::orWithAndNot` | HIGH (0.92) |
| `sub_BDD560` | ~650B | `BitVector::orWithAndNotIfChanged` | HIGH (0.92) |
| `sub_BDD8C0` | ~320B | `BitVector::assignANDNOT` | HIGH (0.88) |
| `sub_BDDAA0` | ~400B | `BitVector::operator^=` | HIGH (0.95) |
| `sub_BDDC00` | ~200B | `BitVector::nextSetBit` / `clear` | HIGH (0.90) |
| `sub_BDDCB0` | ~180B | `BitVector::prevSetBit` | MEDIUM (0.80) |
| `sub_BDDD40` | ~160B | `BitVector::popcount` | MEDIUM (0.80) |

---

## Design Notes

**Why two hash map implementations?** The general hash map is optimized for string and pointer lookups with three mode-specific fast paths that avoid indirect calls. The CFG hash map is optimized for integer-keyed graph edge storage with chained nodes and embedded sub-hashes for multi-edge blocks. The general map uses open addressing with index arrays; the CFG map uses linked-list chaining with explicit node allocation. These are different design tradeoffs for different access patterns.

**Why 32-bit bitvector words (not 64-bit)?** The SSE2 instructions operate on 128-bit vectors regardless of word size, so 32-bit and 64-bit words yield the same SIMD throughput. The 32-bit choice minimizes wasted bits in the trailing word when `bit_count` is not a multiple of 64, and simplifies the alignment prologue calculation (`(-(ptr >> 2)) & 3` vs `(-(ptr >> 3)) & 1`). It also matches the `tzcnt`/`bsr` instruction operand size on x86, avoiding the need for 64-bit variants in the bit-scan operations.

**Why `orWithAndNotIfChanged` as a single function?** This fusion eliminates three intermediate bitvectors that would be needed if the liveness transfer function were decomposed into separate AND-NOT, OR, and change-detection operations. On a function with 200 registers and 50 basic blocks, this saves ~60KB of intermediate allocations per iteration. More importantly, the two-phase scan-then-apply design avoids cache writes for converged blocks, which is the common case in later iterations.

## Cross-References

- [Data Structure Layouts](../ir/data-structures.md) -- Compilation context, Code Object field map, pool allocator
- [Basic Blocks & CFG](../ir/cfg.md) -- CFG edge hash maps, RPO computation, backedge detection
- [Liveness Analysis](../passes/liveness.md) -- Bitvector usage in iterative dataflow, SSE2 loop details
- [Copy Propagation & CSE](../passes/copy-prop-cse.md) -- GVN value table hash map, FNV-1a in expression hashing
- [Memory Pool Allocator](./memory-pools.md) -- Pool allocator used by both hash maps and bitvectors
