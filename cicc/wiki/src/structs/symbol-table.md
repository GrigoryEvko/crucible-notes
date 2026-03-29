# DenseMap and Symbol Table Structures

Every hash table in cicc v13.0 uses the same LLVM DenseMap implementation. This structure serves as the universal symbol table, node uniquing table, and general-purpose associative container throughout the compiler. The implementation was recovered primarily from `sub_162D4F0` (NVVM IR uniquing) and `sub_163D530` (SelectionDAG builder).

## DenseMap Layout

Two variants exist, distinguished by bucket stride.

### Variant A: DenseMap\<ptr, void\> (set-like, 8B/bucket)

| Offset | Size | Type | Field |
|--------|------|------|-------|
| +0 | 8B | `uint64_t` | `NumEntries` (includes tombstones in some variants) |
| +8 | 8B | `ptr` | `Buckets` (heap-allocated array) |
| +16 | 4B | `uint32_t` | `NumItems` (live entries) |
| +20 | 4B | `uint32_t` | `NumTombstones` |
| +24 | 4B | `uint32_t` | `NumBuckets` (always power-of-2) |

Total header: **28 bytes**. Bucket array: `NumBuckets * 8` bytes on the heap.

### Variant B: DenseMap\<ptr, ptr\> (map, 16B/bucket)

Same 28-byte header layout. Bucket stride is 16 bytes (key + value pair). Confirmed by the decompiled access in `sub_163D530`:

```
v30 = (_QWORD *)(v28 + 16LL * v29);   // 16-byte bucket stride
*v30 = v11;                             // store key
v30[1] = v19;                           // store value
```

## Hash Function

Every DenseMap instance in cicc uses the same hash function:

```
hash(ptr) = (ptr >> 9) ^ (ptr >> 4)
```

This is a universal constant across all observed call sites: NVVM IR uniquing tables, SelectionDAG builder node maps, per-node analysis structures, and all other DenseMap/DenseSet instances.

## Collision Resolution

Open-addressing with **quadratic probing**:

```
bucket_idx = hash & (NumBuckets - 1);
step = 1;
while (bucket[idx] != key && bucket[idx] != EMPTY) {
    idx = (idx + step) & (NumBuckets - 1);
    step++;
}
```

## Sentinel Values

| Sentinel | Value | Hex |
|----------|-------|-----|
| EMPTY | -8 | `0xFFFFFFFFFFFFFFF8` |
| TOMBSTONE | -16 | `0xFFFFFFFFFFFFFFF0` |

These specific sentinel values are safe because all valid pointers in cicc are 8-byte aligned, so no real pointer can have the low 3 bits set to the patterns used by these sentinels.

## Growth Policy

The DenseMap grows when the load factor exceeds 75%:

```
if (4 * (NumItems + 1) >= 3 * NumBuckets)
    grow to 2 * NumBuckets
```

When the table has too many tombstones but is below the growth threshold, it rehashes in place:

```
if (NumBuckets - NumTombstones - NumItems <= NumBuckets >> 3)
    rehash at same size (clears tombstones)
```

Minimum bucket count: **64** (enforced by next-power-of-2 clamping).

## Allocation

Bucket arrays are heap-allocated via `sub_22077B0` (operator `new[]` equivalent) and freed via `j___libc_free_0` (`free()`). Arrays are always power-of-2 sized. The DenseMap header itself is typically embedded inline within a larger structure (context objects, analysis results, etc.).

## EDG Declaration / Type Node

The EDG frontend uses an intrusive linked-list structure for scope and declaration traversal. Partial layout recovered from `sub_163D530` lines 1456--1518:

| Offset | Size | Type | Field |
|--------|------|------|-------|
| +8 | 8B | `ptr` | `next_sibling` (linked list) |
| +16 | 1B | `uint8_t` | `node_kind` (values 25..34 are "interesting") |
| +20 | 4B | `uint32_t` | `operand_count` (low 28 bits: masked with `& 0xFFFFFFF`) |
| +23 | 1B | `uint8_t` | `flags` (bit 6 = 0x40 means "indirect operands") |
| +40 | 8B | `ptr` | `associated_ptr` (used for DenseMap lookup) |

The `node_kind` field is filtered by the expression `(unsigned __int8)(*(_BYTE *)(v43 + 16) - 25) <= 9`, selecting kinds 25 through 34 as targets for DAG construction.

### Operand Access

Operand stride is **24 bytes** (three QWORDs per operand). Access depends on the flags field:

- **Indirect** (flags & 0x40): operands at `*(_QWORD *)(node - 8)` (pointer to external array).
- **Inline** (flags & 0x40 == 0): operands at `(node - 24 * operand_count)` (stored backward, like NVVM IR nodes).

Each operand contains three 8-byte fields:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 8B | `operand_ptr[0]` (key value) |
| +8 | 8B | `operand_ptr[1]` |
| +16 | 8B | `operand_ptr[2]` |

## Usage Across the Compiler

DenseMap instances appear at these known locations:

- **NVVM context object**: 8+ tables for IR node uniquing (opcodes 0x10..0x1F), plus sub-function tables for opcodes 0x04..0x15.
- **SelectionDAG builder context**: Map A (+120), Map B (+152), Set C (+184) for node deduplication and worklist.
- **Per-node analysis**: embedded DenseSet at +72 inside analysis structures created during DAG construction.
- **Instruction constraint table**: the global `word_3F3E6C0` array is a flat table rather than a DenseMap, but the constraint emission functions use DenseMaps for lookup caching.

The consistency of the hash function, sentinel values, and growth policy across all instances confirms that cicc links a single DenseMap implementation from its bundled LLVM library, with no NVIDIA-specific modifications to the hashing or probing logic.
