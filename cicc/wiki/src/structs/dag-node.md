# SelectionDAG Node Structure

The SelectionDAG (SDNode) is the central data structure in cicc's code generation backend. Nodes represent operations in the target-independent DAG before instruction selection lowers them to machine instructions. The DAG builder lives in `sub_163D530` (73KB), which allocates nodes from a bump allocator embedded in a builder context object.

## SDNode Layout (104 Bytes)

Every SDNode is allocated as exactly 104 bytes, hardcoded in `sub_163D530`. After allocation, all fields are zeroed. The layout was recovered from the zeroing pattern and subsequent field accesses:

| Offset | Size | Type | Field | Notes |
|--------|------|------|-------|-------|
| +0 | 8B | `ptr` | `chain_next` | Linked list / next pointer |
| +8 | 8B | `ptr` | `use_list` | Head of use-def chain |
| +16 | 8B | `ptr` | `operand_list` | Pointer to operand array / result type |
| +24 | 4B | `uint32_t` | `opcode_flags` | Opcode in low bits, flags in high bits |
| +32 | 8B | `ptr` | (unknown) | Possibly node ID or ordering |
| +40 | 8B | `ptr` | (unknown) | Possibly type list pointer |
| +48 | 8B | `ptr` | (unknown) | Possibly debug/metadata |
| +56 | 4B | `uint32_t` | (unknown) | Possibly num_operands |
| +64 | 8B | `ptr` | `debug_loc` | Source location information |
| +72 | 8B | `ptr` | (unknown) | Possibly IR value reference |
| +80 | 8B | `ptr` | (unknown) | Possibly morphed-from node |
| +88 | 4B | `uint32_t` | (unknown) | Possibly node ordering |
| +96 | 1B | `uint8_t` | `flag_byte` | Misc boolean flags |

The raw 104 bytes are zeroed via a combination of qword and dword stores:

```
qw[0..5] = 0, dw[6] = 0, qw[8..10] = 0, dw[11] = 0, byte[96] = 0
```

The statistics counter at context offset +96 is incremented by 104 for every allocation: `*(_QWORD *)(v4 + 96) += 104LL`.

## SelectionDAG Builder Context

The builder context is the `a1`/`v4` parameter to `sub_163D530`. It holds the function being compiled, target information, the bump allocator state, and several DenseMaps for node deduplication.

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| +0 | 8B | `func_ptr` | The LLVM function being compiled (a2) |
| +8 | 8B | `target_ptr` | Target machine info (a4) |
| +16 | 8B | `alloc_cursor` | Bump allocator current position |
| +24 | 8B | `alloc_end` | Bump allocator end boundary |
| +32 | 8B | `slab_array` | Pointer to array of slab pointers |
| +40 | 4B | `slab_index` | Current slab number (dword) |
| +44 | 4B | `slab_capacity` | Max slabs in array (dword) |
| +48 | var | `inline_slab` | Start of first allocation region |
| +80 | 8B | `bb_list_head` | Basic block list sentinel (points to +96) |
| +88 | 8B | `bb_list_count` | Number of basic blocks (init 0) |

### Embedded DenseMaps

Three DenseMap/DenseSet instances are embedded inline in the context for node deduplication and worklist tracking:

**Map A (node mapping)** at offsets +120..+148:

| Offset | Size | Field |
|--------|------|-------|
| +120 | 8B | `NumEntries` |
| +128 | 8B | `Buckets` pointer |
| +136 | 4B | `NumItems` |
| +140 | 4B | `NumTombstones` |
| +144 | 4B | `NumBuckets` |

**Map B (secondary set)** at offsets +152..+176, same layout.

**Set C (worklist)** at offsets +184..+208, same layout.

Total minimum context size: **212 bytes**.

Map A uses 16-byte bucket stride (key + value pairs), confirmed by the decompiled access pattern:

```
v30 = (_QWORD *)(v28 + 16LL * v29);   // 16-byte stride
*v30 = v11;                             // key
v30[1] = v19;                           // value
```

## Per-Node Analysis Structure

During DAG construction, `sub_163D530` creates per-node analysis objects (accessed via `v381`) with the following layout:

| Offset | Size | Field |
|--------|------|-------|
| +8 | 8B | `array_ptr` | Pointer to pointer array |
| +16 | 4B | `array_count` | Live entries (dword) |
| +24 | 4B | `array_capacity` | Allocated size (dword) |
| +72 | 8B | `set.Buckets` | Embedded DenseSet |
| +80 | 4B | `set.NumItems` | |
| +84 | 4B | `set.NumTombstones` | |
| +88 | 4B | `set.NumBuckets` | |

Operations: `sub_163BE40(v381, ptr)` inserts into the +8 array; `sub_163BBF0(context, key)` looks up the analysis structure for a node in the context's DenseMap.

## Bump Allocator

The builder context uses a slab-based bump allocator identical to the one used for NVVM IR nodes:

- **Slab growth**: `4096 << (slab_index >> 7)` -- exponential, capped at 4TB.
- **Alignment**: 8 bytes.
- **No per-node free**: entire slabs are released when the DAG is destroyed.
- **Overflow**: allocates a new slab via `malloc()`.

Since every SDNode is exactly 104 bytes (13 qwords), a single 4096-byte initial slab holds approximately 39 nodes before overflow triggers slab growth.

## Basic Block Iteration

The builder iterates over the function's basic blocks via a linked list rooted at `a2 + 72` (the function parameter). Each list node embeds the data pointer at offset -24 from the node:

```
bb_data = node_ptr - 24
```

Within each basic block, instructions are iterated via an inner list:
- Inner list sentinel at `bb_data + 40`
- Inner list head at `bb_data + 48`

This matches the LLVM `ilist` intrusive linked list pattern where the list hook is embedded at a fixed offset within the contained object.
