# NVVM IR Node Layout

The NVVM frontend in cicc v13.0 uses a custom intermediate representation distinct from LLVM's native IR. Each IR node is a variable-length structure allocated from a bump allocator, with operands stored **backward** from the node header pointer. The node uniquing infrastructure lives in `sub_162D4F0` (49KB), which routes each opcode to a dedicated DenseMap inside the NVVM context object.

## Node Header Layout

The pointer `a1` returned from allocation points to the start of the fixed header. Operands are at negative offsets behind it.

| Offset | Size | Type | Field | Notes |
|--------|------|------|-------|-------|
| +0 | 1B | `uint8_t` | `opcode` | Switch key in `sub_162D4F0`; values 0x04..0x22+ |
| +2 | 2B | `uint16_t` | `subopcode` | Intrinsic ID; read for opcodes 0x1C, 0x1D, 0x1E |
| +4 | 4B | — | (padding) | Not accessed directly |
| +8 | 4B | `uint32_t` | `num_operands` | Controls operand access range |
| +16 | 8B | `tagged_ptr` | `context_ptr` | Low 3 bits are tag; mask with `& ~7` for pointer |
| +24 | 8B | varies | `extra_A` | DWORD for opcodes 0x1A/0x1B; pointer for 0x10/0x22 |
| +28 | 4B | `uint32_t` | `extra_B` | Present for opcode 0x1B |
| +32 | 8B | varies | `extra_C` | Present for opcode 0x10 |
| +40 | 1B | `uint8_t` | `extra_flag` | Present for opcode 0x10 |

Minimum header size is 24 bytes. Total node allocation: `24 + 8 * num_operands` bytes minimum, though opcode-specific extra fields extend the header region for certain node types.

## Operand Storage

Operands are stored as 8-byte QWORD pointers at negative offsets from the header. The stride is exactly 8 bytes per operand. Access follows this pattern (decompiled from `sub_162D4F0`):

```
operand[k] = *(_QWORD *)(a1 + 8 * (k - num_ops))
```

For a node with `num_operands = 3`:
- `operand[0]` is at `a1 - 24`
- `operand[1]` is at `a1 - 16`
- `operand[2]` is at `a1 - 8`

A 2-operand node occupies 40 bytes total (16 operand bytes + 24 header bytes). A node with opcode 0x1B and 5 operands requires approximately 88 bytes (40 operand bytes + ~48 header bytes including extra fields).

## Tagged Pointer Semantics

The `context_ptr` at offset +16 uses low-bit tagging to encode indirection:

- **Bits [2:0] = 0**: pointer is a direct reference to the context object.
- **Bit [2] = 1**: pointer is an indirect reference (pointer-to-pointer).

The decompiled dereferencing pattern:

```
v = *(a1 + 16) & 0xFFFFFFFFFFFFFFF8;  // mask off tag bits
if (*(a1 + 16) & 4)                    // bit 2 set = indirect
    v = *v;                             // one extra dereference
```

This technique saves a field by encoding the indirection flag inside the pointer itself, relying on 8-byte alignment guarantees.

## NVVM Context Object

The context object referenced by `context_ptr` is a large structure (~2,800+ bytes) containing uniquing tables for every NVVM opcode. Each table is an LLVM-style DenseMap (see [DenseMap](./symbol-table.md)) at fixed qword-indexed offsets.

| Opcode | QWord Base | Description |
|--------|-----------|-------------|
| 0x04..0x15 | byte offsets +496..+1136 | Simple opcode tables, spaced 32B apart |
| 0x16 | qw[130] | DenseMap at qwords 130..131, dwords 264..266 |
| 0x1A | qw[146] | DenseMap at qwords 146..147, dwords 296..298 |
| 0x1B | qw[150] | DenseMap at qwords 150..151, dwords 304..306 |
| 0x1C | qw[154] | DenseMap at qwords 154..155, dwords 312..314 |
| 0x1D | qw[158] | DenseMap at qwords 158..159, dwords 320..322 |
| 0x1E | qw[162] | DenseMap at qwords 162..163, dwords 328..330 |
| 0x1F | qw[166] | DenseMap at qwords 166..167, dwords 336..338 |
| 0x10 | qw[178] | DenseMap at qwords 178..179, dwords 360..362 |

Each DenseMap occupies 4 qwords: `NumEntries`, `Buckets` pointer, then two dwords for `NumItems` and `NumTombstones`. Estimated total context size: at least 1,336 bytes for the uniquing tables alone, plus the sub-function table regions for simple opcodes.

## Allocation

NVVM IR nodes are allocated from a slab-based bump allocator:

- **Slab growth**: `4096 << (slab_index >> 7)` -- exponential, capped at 4TB.
- **Alignment**: 8 bytes (pointer aligned via `(ptr + 7) & ~7`).
- **Deallocation**: no individual free; entire slabs are released at once.
- **Overflow**: triggers a new slab via `malloc()`.

This is the standard LLVM BumpPtrAllocator pattern, consistent with how upstream LLVM manages IR node lifetimes. The lack of per-node deallocation means the NVVM frontend cannot reclaim memory for dead nodes until the entire context is destroyed.

## Opcode Dispatch

The uniquing function `sub_162D4F0` performs a byte-level switch on `*(_BYTE *)a1`:

- Opcodes 0x04 through 0x15 dispatch to sub-functions at 32-byte-spaced offsets within the context.
- Opcodes 0x16 through 0x22+ each have a dedicated DenseMap for deduplication.
- Each case computes a hash over the node's operands and extra fields, then probes the corresponding table to find or insert a unique node.

The hash function used universally across all DenseMap instances in cicc is `(ptr >> 9) ^ (ptr >> 4)`. Sentinel values are `-8` (empty) and `-16` (tombstone).
