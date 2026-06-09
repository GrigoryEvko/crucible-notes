# Hash Table and Collection Infrastructure

Every associative container in cicc v13.0 is built from the same handful of primitives: a pointer-hash DenseMap/DenseSet with quadratic probing, a wyhash-v4-family string hasher, and a SmallVector with inline buffer optimization. Before this page existed, the same hash table description was duplicated across 30+ wiki pages. This is the single source of truth. If you are reimplementing cicc's data structures, start here.

There are no NVIDIA-specific modifications to the DenseMap hashing or probing logic — cicc links the LLVM 20.0.0 implementation unmodified. The only NVIDIA-original hash infrastructure is the wyhash-v4 string hasher used for the builtin name table.

## DenseMap Layout

Two variants exist, distinguished by bucket stride. Both share the same 28-byte inline header, the same hash function, the same probing sequence, the same sentinel values, and the same growth policy. The header is always embedded directly inside a larger structure (context object, analysis result, pass state) — never heap-allocated on its own.

### Variant A — DenseSet (8 bytes/bucket)

| Offset | Size | Type | Field |
|--------|------|------|-------|
| +0 | 8 | `uint64_t` | `NumEntries` |
| +8 | 8 | `ptr` | `Buckets` (heap-allocated array) |
| +16 | 4 | `uint32_t` | `NumItems` (live entries) |
| +20 | 4 | `uint32_t` | `NumTombstones` |
| +24 | 4 | `uint32_t` | `NumBuckets` (always power of 2) |

Bucket array size: `NumBuckets * 8` bytes. Each bucket holds either a valid pointer, an empty sentinel, or a tombstone sentinel.

### Variant B — DenseMap (16 bytes/bucket)

Same 28-byte header. Each bucket holds a key-value pair at a 16-byte stride:

```c
v30 = (_QWORD *)(buckets + 16LL * slot);   // sub_163D530 line 561
*v30 = key;                                  // +0: key
v30[1] = value;                              // +8: value
```

Variant B is used by the SelectionDAG builder (context offsets +120 and +152), the NVVM IR node uniquing tables, and any subsystem that maps pointers to pointers.

### Where the Variants Appear

| Subsystem | Variant | Context offset | Purpose |
|-----------|---------|---------------|---------|
| NVVM IR uniquing (`sub_162D4F0`) | B (16B) | context qw[130..178] | Node deduplication per opcode |
| SelectionDAG builder (`sub_163D530`) | B (16B) | +120, +152 | Node mapping |
| SelectionDAG builder (`sub_163D530`) | A (8B) | +184 | Worklist set |
| Per-node analysis structures | A (8B) | +72 inside v381 | Visited set |
| CSSA PHI map (`sub_3720740`) | B (16B) | r15+0x60 | PHI-to-ID mapping |
| Coroutine spill tracking | B (16B) | +0x18 inline | Spill/reload tracking |
| Builtin name table | custom (12B stride) | context+480 | Name-to-ID with hash cache |

## Pointer Hash Function

Every DenseMap/DenseSet instance in cicc that uses pointer keys employs the same hash:

```text
hash(ptr) = (ptr >> 9) ^ (ptr >> 4)
```

This is LLVM's `DenseMapInfo<void*>::getHashValue`, unchanged. The right-shift by 4 discards the low bits that are always zero due to 8- or 16-byte alignment. The right-shift by 9 mixes in higher-order address bits to break up the stride patterns that arise from slab allocation (where consecutive objects are separated by a fixed power-of-two). The XOR combines these two views of the pointer into a single hash value that distributes well for both heap-allocated and slab-allocated objects.

Representative decompiled evidence (appears identically in dozens of functions):

```c
v9 = (v12 - 1) & (((unsigned int)v11 >> 9) ^ ((unsigned int)v11 >> 4));
```

### Integer-Key Hash Variant

A separate hash function is used for `DenseMap<unsigned, T>` instances (integer keys rather than pointers):

```text
hash(key) = key * 37
```

This is LLVM's `DenseMapInfo<unsigned>::getHashValue`. It appears in the instruction emitter (`sub_2E29BA0`), the two-address pass (`sub_1F4E3A0`), the vector legalization tables, and the SelectionDAG instruction selection cost table (`sub_3090F90`). Integer-key maps use a different sentinel pair: `0xFFFFFFFF` (empty) and `0xFFFFFFFE` (tombstone).

## wyhash v4 String Hasher — `sub_CBF760`

The NVVM builtin name table uses a separate, NVIDIA-original hash function for string keys. `sub_C92610` is a thin wrapper that tail-calls `sub_CBF760`. The function dispatches on input length into six code paths, each using different constant sets and mixing strategies:

### Length Dispatch Table

| Length | Strategy | Constants |
|--------|----------|-----------|
| 0 | Return constant | `0x2D06800538D394C2` |
| 1–3 | 3-byte read + XOR + multiply | seed `0x87275A9B`, mul `0xC2B2AE3D27D4EB4F`, avalanche `0x165667B19E3779F9` |
| 4–8 | 2x uint32 + combine + rotate | XOR `0xC73AB174C5ECD5A2`, mul `0x9FB21C651E98DF25` |
| 9–16 | 2x uint64 + 128-bit multiply | XOR `0x6782737BEA4239B9` / `0xAF56BC3B0996523A`, avalanche `0x165667919E3779F9` |
| 17–128 | Paired 16B reads from both ends | Per-pair constants, 128-bit multiplies, length mixed with `0x61C8864E7A143579` |
| 129–240 | Extended mixing | Delegates to `sub_CBF370` |
| 240+ | Bulk processing | Delegates to `sub_CBF100` |

### Pseudocode (length 1–3, the most common case for short builtins)

```rust
fn wyhash_short(data: &[u8], len: usize) -> u32 {
    let a = data[0] as u64;
    let b = data[len / 2] as u64;
    let c = data[len - 1] as u64;
    let combined = a | (b << 8) | (c << 16) | (len as u64) << 24;
    let mixed = combined ^ 0x87275A9B;
    let wide = mixed.wrapping_mul(0xC2B2AE3D27D4EB4F);
    let folded = wide ^ (wide >> 32);
    let result = folded.wrapping_mul(0x165667B19E3779F9);
    (result ^ (result >> 32)) as u32
}
```

### Pseudocode (length 17–128, covering most `__nvvm_*` names)

```rust
fn wyhash_medium(data: &[u8], len: usize) -> u32 {
    let pairs = [
        (0x1CAD21F72C81017C, 0xBE4BA423396CFEB8),  // pair 0
        (0x1F67B3B7A4A44072, 0xDB979083E96DD4DE),  // pair 1
        (0x2172FFCC7DD05A82, 0x78E5C0CC4EE679CB),  // pair 2
        // ... additional pairs for 64/96/128 thresholds
    ];
    let (mut v8, mut v10) = (0u64, 0u64);
    // read 16 bytes from front, 16 from back, mix with pair constants
    for i in 0..((len + 15) / 32) {
        let front = read_u128(&data[i * 16..]);
        let back  = read_u128(&data[len - (i + 1) * 16..]);
        (v8, v10) = mix_128(v8, v10, front, back, pairs[i]);
    }
    let combined = v8 ^ v10 ^ (len as u64 ^ 0x61C8864E7A143579);
    let result = 0x165667919E3779F9u64.wrapping_mul(combined ^ (combined >> 37));
    (result ^ (result >> 32)) as u32
}
```

The final return value is always a `uint32` — the high dword of the 64-bit result XORed with the low dword. Most NVVM builtin names are 8–35 bytes, hitting the optimal 4–8 and 9–16 and 17–128 paths.

## Probing Strategy

All DenseMap instances use quadratic probing with triangular-number increments:

```text
slot = hash & (capacity - 1)      // initial probe
step = 1
loop:
    if bucket[slot] == key   -> found
    if bucket[slot] == EMPTY -> not found (insert here)
    if bucket[slot] == TOMBSTONE -> record for reuse
    slot = (slot + step) & (capacity - 1)
    step++
```

The probe sequence for initial position `h` visits:

```text
h, h+1, h+3, h+6, h+10, h+15, h+21, ...
h + T(k) where T(k) = k*(k+1)/2   (triangular numbers)
```

This guarantees that for a power-of-2 table size `n`, all `n` slots are visited before any index repeats. The proof relies on the fact that the differences `T(k+1) - T(k) = k+1` produce all residues modulo `n` when `n` is a power of 2.

### Comparison Guard (Builtin Table)

The builtin name hash table (`sub_C92740`, `sub_C92860`) adds a triple comparison guard before performing the expensive `memcmp`:

1. **Cached hash equality**: `hash_cache[slot] == search_hash`
2. **Length equality**: `entry->length == search_length`
3. **Content equality**: `memcmp(search_data, entry->string_data, length) == 0`

The hash cache is stored in a separate array immediately after the bucket array and the end-of-table sentinel. This layout avoids polluting bucket cache lines with hash values that are only needed on collision.

### Probing Label: "Linear" vs "Quadratic"

Some analysis reports describe the probing as "linear" because the `step` variable increments by 1 each iteration. The actual probe *position* advances quadratically (by accumulating triangular numbers). Both descriptions refer to the same code. This page uses the technically precise term: quadratic probing with triangular numbers.

## Growth Policy

### Load Factor Threshold — 75%

After every successful insertion, the map checks whether to grow:

```c
if (4 * (NumItems + 1) >= 3 * NumBuckets)
    // load factor > 75% -> double capacity
    new_capacity = 2 * NumBuckets
```

### Tombstone Compaction — 12.5%

If the load factor is acceptable but tombstones have accumulated:

```c
elif (NumBuckets - NumTombstones - NumItems <= NumBuckets >> 3)
    // fewer than 12.5% of slots are truly empty
    // rehash at same capacity to clear tombstones
    new_capacity = NumBuckets
```

### Rehash Procedure — `sub_C929D0`

1. `calloc(new_capacity + 1, bucket_stride)` for the new array.
2. Write the end-of-table sentinel at position `new_capacity`.
3. For each live (non-empty, non-tombstone) entry in the old table, reinsert into the new table using quadratic probing.
4. Copy the cached hash (if the table has a hash cache).
5. Track the new position of a "current slot" pointer so the caller can continue using the entry it just inserted.
6. Free the old array.
7. Reset `NumTombstones` to 0.
8. Update `NumBuckets` to `new_capacity`.
9. Return the new position of the tracked slot.

### Capacity Constraints

- **Power of 2**: always. Enforced by the bit-smearing pattern: `x |= x>>1; x |= x>>2; x |= x>>4; ...; x += 1`.
- **Minimum**: 64 buckets for standard DenseMap instances. The builtin name table starts at 16 and grows through `16 -> 32 -> 64 -> 128 -> 256 -> 512 -> 1024` as its 770 entries are inserted.
- **Allocation**: `sub_22077B0` (operator `new[]`), freed via `j___libc_free_0`.

## Sentinel Values

Two sentinel families exist, distinguished by magnitude. Both are chosen to be impossible values for aligned pointers.

### NVVM-Layer Sentinels (small magnitude)

Used by the NVVM IR uniquing tables, the SelectionDAG builder maps, and the builtin name table:

| Role | Value | Hex | Why safe |
|------|-------|-----|----------|
| Empty | -8 | `0xFFFFFFFFFFFFFFF8` | Low 3 bits = `0b000` after masking, but no 8-byte-aligned pointer is this close to `(uint64_t)-1` |
| Tombstone | -16 | `0xFFFFFFFFFFFFFFF0` | Same reasoning, distinct from -8 |

The builtin name table also uses a value of `2` as an end-of-table sentinel placed at `bucket_array[capacity]`.

### LLVM-Layer Sentinels (large magnitude)

Used by the majority of LLVM pass infrastructure — SCEV, register coalescing, block placement, SLP vectorizer, StructurizeCFG, machine pipeliner, prolog-epilog, and others:

| Role | Value | Hex | Decimal |
|------|-------|-----|---------|
| Empty | `0xFFFFFFFFFFFFF000` | `-4096` | -4096 |
| Tombstone | `0xFFFFFFFFFFFFE000` | `-8192` | -8192 |

### Integer-Key Sentinels

Used by `DenseMap<unsigned, T>` instances (instruction emitter, two-address pass):

| Role | Value | Hex |
|------|-------|-----|
| Empty | `0xFFFFFFFF` | 32-bit all-ones |
| Tombstone | `0xFFFFFFFE` | 32-bit all-ones minus 1 |

### Which Sentinel Set to Expect

| Subsystem | Sentinel pair |
|-----------|---------------|
| NVVM IR uniquing, SelectionDAG builder | -8 / -16 |
| Builtin name table | -8 (tombstone), 0 (empty), 2 (end marker) |
| SCEV, block placement, SLP vectorizer | -4096 / -8192 |
| Register coalescing, machine pipeliner | -4096 / -8192 |
| StructurizeCFG, prolog-epilog | -4096 / -8192 |
| Instruction emitter, two-address | 0xFFFFFFFF / 0xFFFFFFFE |
| Coroutine spill tracking | 0xFFFFFFFFF000 / 0xFFFFFFFFE000 |
| CSSA PHI map | 0xFFFFFFFFF000 / 0xFFFFFFFFE000 |
| Debug verify | 0xFFFFFFFFF000 / 0xFFFFFFFFE000 |
| LazyCallGraph | 0xFFFFFFFFF000 / 0xFFFFFFFFE000 |

The -8/-16 pair appears exclusively in NVVM-layer (NVIDIA-original) code. The -4096/-8192 pair is the standard LLVM `DenseMapInfo<void*>` sentinel set. The difference is cosmetic — both pairs are safe for the same reasons — but it reveals code provenance: if you see -8/-16, the code was written or heavily modified by NVIDIA; if you see -4096/-8192, it is stock LLVM.

## SmallVector Pattern

SmallVector is the universal dynamic array throughout cicc, with two growth implementations:

### Layout

```text
[BeginPtr, Size:Count:Capacity, InlineData...]
```

| Offset | Size | Field |
|--------|------|-------|
| +0 | 8 | `data_ptr` (points to inline buffer initially, heap after growth) |
| +8 | 4 | `size` (live element count) |
| +12 | 4 | `capacity` (allocated slots) |
| +16 | N | Inline buffer (N = `InlineCapacity * element_size`) |

When `size == capacity` on insertion, the vector grows.

### Growth Functions

| Function | Address | Description |
|----------|---------|-------------|
| `SmallVector::grow` | `sub_C8D5F0` | Generic growth — copies elements, used for non-POD types |
| `SmallVectorBase::grow_pod` | `sub_C8D7D0` | POD-optimized growth — uses `realloc` when buffer is heap-allocated |
| `SmallVector::grow` (MIR) | `sub_16CD150` | Second copy in the MachineIR address range, identical logic |
| `SmallVector::grow` (extended) | `sub_C8E1E0` | Larger variant (11KB), handles edge cases |

### Growth Policy

The standard LLVM SmallVector growth: double the current capacity, with a minimum of 1. If the current buffer is the inline buffer, `malloc` a new heap buffer and `memcpy` the contents. If the buffer is already on the heap, `realloc` it (for POD types) or `malloc` + copy + `free` (for non-POD types).

```c
new_capacity = max(2 * old_capacity, required_capacity)
if (data_ptr == &inline_buffer)
    heap_buf = malloc(new_capacity * elem_size)
    memcpy(heap_buf, inline_buffer, size * elem_size)
else
    // POD: heap_buf = realloc(data_ptr, new_capacity * elem_size)
    // non-POD: heap_buf = malloc(...); copy; free(old)
data_ptr = heap_buf
capacity = new_capacity
```

### Common Inline Capacities

Observed across the codebase:

| Inline capacity | Element size | Total inline bytes | Typical use |
|----------------|-------------|-------------------|-------------|
| 2 | 8 | 16 | SCEV delinearization terms |
| 4 | 8 | 32 | LazyCallGraph SCC lists, basic block worklists |
| 8 | 8 | 64 | NVVMReflect call collection, PHI operand lists |
| 16 | 8 | 128 | AA evaluation pointer sets |
| 22 | 8 | 176 | Printf argument arrays (stack-allocated) |
| 8 | 56 | 448 | SROA slice descriptors |

## Builtin Name Table — Specialized Hash Table

The builtin name table at `context+480` is a specialized variant that does not use the standard DenseMap layout. It stores string entries rather than pointers, includes a parallel hash cache, and uses the wyhash function instead of the pointer hash.

### Table Structure (20 bytes)

| Offset | Size | Field |
|--------|------|-------|
| +0 | 8 | `bucket_array_ptr` |
| +8 | 4 | `capacity` (power of 2) |
| +12 | 4 | `count` (live entries) |
| +16 | 4 | `tombstone_count` |

### Memory Layout

```text
[0 .. 8*cap-1]                    bucket_array: cap QWORD pointers
[8*cap .. 8*cap+7]                sentinel: value 2 (end-of-table)
[8*cap+8 .. 8*cap+8+4*cap-1]     hash_cache: uint32 per slot
```

### String Entry (heap-allocated via `sub_C7D670`)

| Offset | Size | Field |
|--------|------|-------|
| +0 | 8 | `string_length` |
| +8 | 4 | `builtin_id` (set after insertion) |
| +16 | N+1 | Null-terminated string data |

Total allocation: `length + 17` bytes, 8-byte aligned. The string data offset (16) is stored at `hashtable+20` for use during comparison.

See [Builtins](../builtins/index.md) for the complete 770-entry builtin ID inventory.

## Usage Across the Compiler

### Subsystems Using DenseMap (pointer hash, -8/-16 sentinels)

- **NVVM IR uniquing** (`sub_162D4F0`): 8+ DenseMap instances in the NVVM context object, one per opcode range (0x04–0x1F). Tables at fixed qword-indexed offsets, spaced 32 bytes apart.
- **SelectionDAG builder** (`sub_163D530`): Three maps at context offsets +120, +152, +184. Map A and B are 16-byte-stride (key-value), Set C is 8-byte-stride (keys only).
- **Per-node analysis structures**: Embedded DenseSet at +72 within analysis objects created during DAG construction.
- **Memory space optimization** (`sub_1C6A6C0`): DenseMap-style tables for address space tracking.

### Subsystems Using DenseMap (pointer hash, -4096/-8192 sentinels)

- **SCEV** (`sub_F03CD0` and family): Expression caching, range computation, back-edge taken count.
- **Register coalescing** (`sub_1F2F8F0`): Already-coalesced set, equivalence class map.
- **Block placement** (`sub_2E3B720`): Chain membership, tail-merge candidates.
- **SLP vectorizer** (`sub_1ACCE50`): AllOps and Scalars hash tables (32-byte entries).
- **StructurizeCFG** (`sub_1B66CF0`): Flow-block mapping, region membership.
- **Machine pipeliner** (`sub_20C40D0`): Schedule stage tracking.
- **CSSA** (`sub_3720740`): PHI-to-ID mapping.
- **Debug/verify** (`sub_265D050`): Instruction validation tables.
- **LazyCallGraph** (`sub_D1A040`): Edge membership, SCC identity.

### Subsystems Using DenseMap (integer hash `key * 37`)

- **Instruction emitter** (`sub_2E1F350`): Opcode-to-constraint mapping. Sentinels: `0xFFFFFFFF` / `0xFFFFFFFE`.
- **Two-address pass** (`sub_1F4BFE0`): TiedOperandMap (56-byte entries, 4 inline). EqClassMap.
- **Vector legalization** (`sub_3302A00`): Type-split record mapping.
- **SelectionDAG isel** (`sub_3090F90`): Argument cost table.

### Subsystems Using wyhash (string keys)

- **Builtin name table** (`sub_90AEE0`): 770 NVVM/CUDA builtin names. Uses the specialized 20-byte table header with hash cache.
- This is the only known use of `sub_CBF760` in cicc.

## Key Functions

| Function | Address | Size | Role |
|----------|---------|------|------|
| DenseMap pointer hash | inline | — | `(ptr >> 9) ^ (ptr >> 4)` — always inlined |
| DenseMap integer hash | inline | — | `key * 37` — always inlined |
| wyhash v4 | `sub_CBF760` | ~4 KB | String hash, length-dispatched |
| wyhash wrapper | `sub_C92610` | tiny | Tail-calls `sub_CBF760` |
| Builtin insert-or-find | `sub_C92740` | ~2 KB | Quadratic probe with hash cache |
| Builtin find-only | `sub_C92860` | ~1 KB | Read-only variant of `sub_C92740` |
| Builtin rehash | `sub_C929D0` | ~1 KB | 75% load factor, tombstone compaction |
| Builtin table init | `sub_C92620` | tiny | Creates 16-bucket initial table |
| SmallVector::grow | `sub_C8D5F0` | ~2 KB | Generic element growth |
| SmallVectorBase::grow_pod | `sub_C8D7D0` | ~5 KB | POD-optimized realloc growth |
| SmallVector::grow (MIR) | `sub_16CD150` | ~2 KB | Duplicate in MachineIR range |
| SmallPtrSet::insertOrFind | `sub_C9A3C0` | ~16 KB | Small pointer set with growth |
| DenseMap grow (LLVM passes) | varies per pass | — | Each pass has its own inlined or outlined rehash |

## Cross-References

- [Builtins — Hash Table and ID Inventory](../builtins/index.md) — complete 770-entry builtin table with wyhash usage
- [DenseMap and Symbol Table Structures](../structs/symbol-table.md) — original page (now a subset of this one, kept for EDG node layout)
- [NVVM IR Node](../structs/ir-node.md) — NVVM context object with DenseMap uniquing tables
- [CSSA](../passes/cssa.md) — PHI hash map with -4096/-8192 sentinels
- [Register Coalescing](../llvm/register-coalescing.md) — integer-key and pointer-key hash map variants
- [SLP Vectorizer](../llvm/slp-vectorizer.md) — 32-byte-entry DenseMap with -4096/-8192 sentinels
- [SCEV](../llvm/scev.md) — SCEV expression caching with -4096/-8192 sentinels
- [Instruction Emitter](../llvm/instr-emitter.md) — integer-key hash with `key * 37`
