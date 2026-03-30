# NVVM IR Node Layout

The NVVM frontend in cicc v13.0 uses a custom intermediate representation distinct from LLVM's native IR. Each IR node is a variable-length structure allocated from a bump allocator, with operands stored **backward** from the node header pointer. The node uniquing infrastructure lives in `sub_162D4F0` (49KB), which routes each opcode to a dedicated DenseMap inside the NVVM context object.

## Node Header Layout

The pointer `a1` returned from allocation points to the start of the fixed header. Operands are at negative offsets behind it.

| Offset | Size | Type | Field | Notes |
|--------|------|------|-------|-------|
| +0 | 1B | `uint8_t` | `opcode` | Switch key in `sub_162D4F0`; values 0x04..0x22+ |
| +2 | 2B | `uint16_t` | `subopcode` | Intrinsic ID; read for opcodes 0x1C, 0x1D, 0x1E |
| +4 | 4B | -- | (padding) | Not accessed directly |
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

## Opcode Dispatch Table

The uniquing function `sub_162D4F0` performs a byte-level switch on `*(_BYTE *)a1`. Each case extracts the tagged context pointer, dereferences it, then probes an opcode-specific DenseMap for a structurally identical node.

### Uniquing Opcode Dispatch (`sub_162D4F0`, 49KB)

The opcodes fall into two categories: "simple" opcodes that use sub-function tables at fixed stride, and "complex" opcodes that use dedicated DenseMap instances at individually-known offsets.

**Simple opcodes (0x04--0x15)** -- These 18 opcodes share a uniform dispatch pattern. Each routes to a sub-function table at a fixed byte offset within the context object, spaced 32 bytes apart:

| Opcode | Context Byte Offset | Semantic Category |
|--------|-------------------|-------------------|
| 0x04 | +496 | Type / value constant |
| 0x05 | +528 | Binary operation |
| 0x06 | +560 | (simple node) |
| 0x07 | +592 | (simple node) |
| 0x08 | +624 | (simple node) |
| 0x09 | +656 | Undef / poison |
| 0x0A | +688 | (simple node) |
| 0x0B | +720 | (simple node) |
| 0x0C | +752 | (simple node) |
| 0x0D | +784 | Integer constant |
| 0x0E | +816 | FP constant |
| 0x0F | +848 | Constant expression |
| 0x10 | -- | *Special*: uses DenseMap at qw[178] |
| 0x11 | +912 | (simple node) |
| 0x12 | +944 | (simple node) |
| 0x13 | +976 | Struct / aggregate type |
| 0x14 | +1008 | (simple node) |
| 0x15 | +1072 | (simple node) |

Each sub-function table entry at these offsets is a 32-byte structure containing the callback address and metadata for hash-table probing.

**Complex opcodes (0x16--0x22)** -- These opcodes each own a full DenseMap within the context object. Each DenseMap occupies 4 qwords at the indicated base, plus associated dword counters:

| Opcode | QWord Base | Byte Offset | DenseMap Dwords | Identified Semantic |
|--------|-----------|-------------|-----------------|---------------------|
| 0x16 | qw[130] | +1040 | dw[264..266] | Metadata node |
| 0x17 | -- | +1104 | -- | (simple-table path at +1104) |
| 0x18 | -- | +1136 | -- | Alloca (bitcode 0x18/0x58) |
| 0x19 | -- | -- | -- | Load |
| 0x1A | qw[146] | +1168 | dw[296..298] | Branch (br) |
| 0x1B | qw[150] | +1200 | dw[304..306] | Switch |
| 0x1C | qw[154] | +1232 | dw[312..314] | Invoke (reads subopcode) |
| 0x1D | qw[158] | +1264 | dw[320..322] | Unreachable / resume (reads subopcode) |
| 0x1E | qw[162] | +1296 | dw[328..330] | LandingPad (reads subopcode) |
| 0x1F | qw[166] | +1328 | dw[336..338] | Call instruction |
| 0x20 | -- | -- | -- | PHI node |
| 0x21 | -- | -- | -- | IndirectBr |
| 0x22 | qw[178] | +1424 | dw[360..362] | Special (extra_A = ptr) |

Opcodes 0x1C, 0x1D, and 0x1E read the `subopcode` field at `*(unsigned __int16 *)(a1 + 2)` as part of the hash key, because these node types require the intrinsic ID to distinguish structurally identical nodes with different semantic meaning.

### Hash Function

Every DenseMap in the uniquing tables uses the same hash:

```
hash(ptr) = (ptr >> 9) ^ (ptr >> 4)
```

Hash computation for multi-operand nodes (`sub_15B3480`) extends this by combining the hash of each operand pointer with a mixing step. The hash seed is the opcode byte, then each operand is folded in:

```
seed ^= hash(operand[i]) + 0x9E3779B9 + (seed << 6) + (seed >> 2);
```

Sentinel values: empty = `-8` (`0xFFFFFFFFFFFFFFF8`), tombstone = `-16` (`0xFFFFFFFFFFFFFFF0`).

### Node Erasure (`sub_1621740`, 14KB)

The mirror of insertion. Dispatches by the same opcode byte, finds the node in the corresponding DenseMap, overwrites the bucket with the tombstone sentinel (`-16`), and decrements `NumItems` while incrementing `NumTombstones`. When tombstone count exceeds `NumBuckets >> 3`, a rehash at the same capacity is triggered to reclaim tombstone slots.

## Bitcode Instruction Opcode Table

NVIDIA uses LLVM's standard instruction opcode numbering with minor adjustments. The bitcode reader `sub_166A310` / `sub_151B070` (`parseFunctionBody`, 60KB/123KB) dispatches on a contiguous range. The NVVM verifier `sub_2C80C90` confirms the mapping via its per-opcode validation switch:

| Opcode | Hex | LLVM Instruction | Verifier Checks |
|--------|-----|------------------|-----------------|
| 0x0B | 11 | `ret` | -- |
| 0x0E | 14 | `br` | -- |
| 0x0F | 15 | `switch` | -- |
| 0x15 | 21 | `invoke` | "invoke" unsupported via `sub_2C76F10` |
| 0x18 | 24 | `alloca` | Alignment <= 2^23; AS must be Generic |
| 0x19 | 25 | `load` | -- |
| 0x1A | 26 | `br` (cond) | Validates "Branch condition is not 'i1' type!" |
| 0x1B | 27 | `switch` (extended) | -- |
| 0x1C | 28 | `invoke` (extended) | -- |
| 0x1D | 29 | `unreachable` | -- |
| 0x1E | 30 | `resume` | -- |
| 0x1F | 31 | `call` | Pragma metadata validation |
| 0x20 | 32 | `phi` | -- |
| 0x21 | 33 | `indirectbr` | "indirectbr" unsupported |
| 0x22 | 34 | `call` (variant) | Validates callee type signature |
| 0x23 | 35 | `resume` (verifier) | "resume" unsupported |
| 0x23--0x34 | 35--52 | Binary ops (add/sub/mul/div/rem/shift/logic) | -- |
| 0x35--0x38 | 53--56 | Casts (trunc/zext/sext/fpcast) | -- |
| 0x3C | 60 | `alloca` | Alignment and address-space checks |
| 0x3D | 61 | `load` | Atomic loads rejected; tensor memory AS rejected |
| 0x3E | 62 | `store` | Atomic stores rejected; tensor memory AS rejected |
| 0x40 | 64 | `fence` | Only acq_rel/seq_cst in UnifiedNVVMIR mode |
| 0x41 | 65 | `cmpxchg` | Only i32/i64/i128; must be generic/global/shared AS |
| 0x42 | 66 | `atomicrmw` | Address space validation |
| 0x4F | 79 | `addrspacecast` | "Cannot cast non-generic to different non-generic" |
| 0x55 | 85 | Intrinsic call | Routes to `sub_2C7B6A0` (143KB verifier) |
| 0x58 | 88 | `alloca` (inalloca) | Same as 0x18 |
| 0x5F | 95 | `landingpad` | "landingpad" unsupported |

The binary opcodes in the 0x23--0x34 range follow LLVM's `BinaryOperator` numbering:

| Opcode | Hex | Operation | IRBuilder Helper |
|--------|-----|-----------|-----------------|
| 0x23 | 35 | `add` | -- |
| 0x24 | 36 | `fadd` | -- |
| 0x25 | 37 | `sub` | -- |
| 0x26 | 38 | `fsub` | -- |
| 0x27 | 39 | `mul` | -- |
| 0x28 | 40 | `fmul` | -- |
| 0x29 | 41 | `udiv` | -- |
| 0x2A | 42 | `sdiv` | -- |
| 0x2B | 43 | `fdiv` | -- |
| 0x2C | 44 | `urem` | -- |
| 0x2D | 45 | `srem` | -- |
| 0x2E | 46 | `frem` | -- |
| 0x2F | 47 | `shl` | -- |
| 0x30 | 48 | `lshr` | -- |
| 0x31 | 49 | `ashr` | -- |
| 0x32 | 50 | `and` | -- |
| 0x33 | 51 | `or` | -- |
| 0x34 | 52 | `xor` | -- |

### InstCombine Internal Opcode Table

The InstCombine mega-visitor `sub_10EE7A0` (405KB, the single largest function in cicc) uses a *different* opcode numbering -- the full LLVM `Instruction::getOpcode()` values rather than the bitcode record codes. These are accessed via `sub_987FE0` (getOpcode equivalent). Key ranges observed:

| Opcode Range | LLVM Instructions |
|-------------|-------------------|
| 0x0B | Ret |
| 0x0E | Br |
| 0x0F | Switch |
| 0x15 | Invoke |
| 0x1A | Unreachable |
| 0x3F | FNeg |
| 0x41--0x43 | Add, FAdd, Sub |
| 0x99 | GetElementPtr |
| 0xAA | Trunc |
| 0xAC--0xAE | ZExt, SExt, FPToUI |
| 0xB4--0xB5 | PtrToInt, IntToPtr |
| 0xCF--0xD2 | ICmp, FCmp, PHI, Call |
| 0xE3--0xEB | VAArg, ExtractElement, InsertElement, ShuffleVector, ExtractValue, InsertValue |
| 0x11A | Fence |
| 0x11D | AtomicCmpXchg |
| 0x125 | AtomicRMW |
| 0x134--0x174 | FPTrunc, FPExt, UIToFP, Alloca, Load, Store, FMul, UDiv, SDiv, ... |
| 0x17D--0x192 | BitCast, Freeze, LandingPad, CatchSwitch, CatchRet, CallBr, ... |
| 0x2551, 0x255F, 0x254D | NVIDIA custom intrinsic operations |

The NVIDIA custom opcodes (0x2551, 0x255F, 0x254D) are in a range far above standard LLVM and handle CUDA-specific operations (texture, surface, or warp-level ops encoded as custom IR nodes) that have no upstream LLVM equivalent.

## NVVM Context Object

The context object referenced by `context_ptr` is a large structure (**~3,656 bytes**, confirmed by the destructor `sub_B76CB0` at 97KB which tears down a `~3656-byte object`) containing uniquing tables for every NVVM opcode, plus type caches, metadata interning tables, and allocator state.

### Context Layout Overview

| Byte Offset | Size | Field | Description |
|-------------|------|-------|-------------|
| +0..+200 | 200B | Core state | Module pointer, allocator, flags |
| +200 | 8B | vtable_0 | Points to `unk_49ED3E0` |
| +224 | 8B | vtable_1 | Points to `unk_49ED440` |
| +248 | 8B | vtable_2 | Points to `unk_49ED4A0` |
| +272..+792 | 520B | Hash table array | 16 DenseMaps freed at stride 32 by destructor |
| +496..+1136 | 640B | Simple opcode tables | 18 sub-function tables, 32B each (opcodes 0x04..0x15) |
| +1040..+1424 | 384B | Complex opcode DenseMaps | Dedicated DenseMaps for opcodes 0x16..0x22 |
| +1424..+2800 | ~1376B | Extended tables | Additional hash tables, type caches, metadata maps |
| +2800..+3656 | ~856B | Allocator state | Bump allocator slabs, counters, statistics |

### Simple Opcode Table Region (+496..+1136)

The 18 entries for opcodes 0x04 through 0x15 (plus a few extras) are 32-byte structures at fixed offsets:

```
struct SimpleOpcodeTable {
    void  *buckets;       // +0:  heap-allocated bucket array
    int32  num_items;     // +8:  live entry count
    int32  num_tombstones; // +12: tombstone count
    int32  num_buckets;   // +16: always power-of-2
    int32  reserved;      // +20: padding
    void  *callback;      // +24: hash-insert function pointer (or NULL)
};
```

Byte offsets increase monotonically: +496, +528, +560, +592, +624, +656, +688, +720, +752, +784, +816, +848, +880, +912, +944, +976, +1008, +1072, +1104, +1136.

### Complex Opcode DenseMap Region (+1040..+1424)

Each DenseMap for a complex opcode occupies 4 qwords plus associated dword counters:

```
struct OpcodeUniqueMap {
    int64  num_entries;    // qw[N]:   includes tombstones
    void  *buckets;        // qw[N+1]: heap-allocated bucket array
    int32  num_items;      // dw[2*N + offset]: live entries
    int32  num_tombstones; // dw[2*N + offset + 1]: tombstone count
    int32  num_buckets;    // dw[2*N + offset + 2]: capacity (power-of-2)
};
```

Complete mapping:

| Opcode | qw Base | Byte Offset (qw) | dw Counters | Byte Offset (dw) |
|--------|---------|-------------------|-------------|-------------------|
| 0x16 | qw[130] | +1040 | dw[264..266] | +2112..+2120 |
| 0x1A | qw[146] | +1168 | dw[296..298] | +2368..+2376 |
| 0x1B | qw[150] | +1200 | dw[304..306] | +2432..+2440 |
| 0x1C | qw[154] | +1232 | dw[312..314] | +2496..+2504 |
| 0x1D | qw[158] | +1264 | dw[320..322] | +2560..+2568 |
| 0x1E | qw[162] | +1296 | dw[328..330] | +2624..+2632 |
| 0x1F | qw[166] | +1328 | dw[336..338] | +2688..+2696 |
| 0x10 | qw[178] | +1424 | dw[360..362] | +2880..+2888 |

### Destructor (`sub_1608300`, 90KB)

The context destructor confirms the layout by freeing resources in order:

1. Calls `j___libc_free_0` on bucket pointers at offsets +272 through +792 (stride 32) -- frees all 16 simple opcode hash tables.
2. Destroys sub-objects via `sub_16BD9D0`, `sub_1605960`, `sub_16060D0` -- these tear down the complex DenseMap instances and any heap-allocated overflow chains.
3. Releases vtable-referenced objects at offsets +200, +224, +248.

The separate LLVMContext destructor (`sub_B76CB0`, 97KB) frees 28+ hash tables from the full ~3,656-byte context structure, confirming that the uniquing tables are only part of the overall context.

### Type Tag System

The context object's hash tables also serve as uniquing tables for type nodes. The byte at offset +16 in each IR node encodes the type tag (distinct from the opcode byte at +0):

| Type Tag | Meaning | Notes |
|----------|---------|-------|
| 5 | Instruction / expression | Binary ops, comparisons |
| 8 | Constant aggregate | ConstantArray, ConstantStruct |
| 9 | Undef / poison | `UndefValue` |
| 13 | Integer constant | APInt at +24, bitwidth at +32 |
| 14 | FP constant | APFloat storage |
| 15 | Constant expression | ConstantExpr (GEP, cast, etc.) |
| 16 | Struct / aggregate type | Element list at +32 |
| 17 | MDTuple / metadata node | Metadata tuple |
| 37 | Comparison instruction | ICmp / FCmp predicate |

The type tag at +16 is used by InstCombine (`sub_1743DA0`) and many other passes to quickly classify nodes without reading the full opcode. The observed range is 5--75, considerably denser than standard LLVM's Value subclass IDs.

## Instruction Creation Helpers

NVIDIA's LLVM fork provides a set of instruction creation functions that allocate nodes from the bump allocator, insert them into the appropriate uniquing table, and update use-lists. These are the core IR mutation API:

### Primary Instruction Factories

| Address | Size | Signature | LLVM Equivalent |
|---------|------|-----------|-----------------|
| `sub_B504D0` | -- | `(opcode, op0, op1, state, 0, 0)` | `BinaryOperator::Create` / `IRBuilder::CreateBinOp` |
| `sub_B50640` | -- | `(val, state, 0, 0)` | Result-typed instruction / `CreateNeg` wrapper |
| `sub_B51BF0` | -- | `(inst, src, destTy, state, 0, 0)` | `IRBuilder::CreateZExtOrBitCast` |
| `sub_B51D30` | -- | `(opcode, src, destTy, state, 0, 0)` | `CmpInst::Create` / `IRBuilder::CreateCast` |
| `sub_B52190` | -- | `(...)` | `BitCastInst::Create` |
| `sub_B52260` | -- | `(...)` | `GetElementPtrInst::Create` (single-index) |
| `sub_B52500` | -- | `(...)` | `CastInst::Create` with predicate |
| `sub_B33D10` | -- | `(ctx, intrinsicID, args, numArgs, ...)` | `IRBuilder::CreateIntrinsicCall` |
| `sub_BD2DA0` | -- | `(80)` | `Instruction::Create` (allocates 80-byte IR node) |
| `sub_BD2C40` | -- | `(72, N)` | `Instruction::Create` (72-byte base, N operands) |

### Opcode Constants for Creation

These numeric opcode values are passed as the first argument to `sub_B504D0`:

| Value | Operation | Example |
|-------|-----------|---------|
| 13 | Sub | `sub_B504D0(13, a, b, ...)` |
| 15 | FNeg / FSub variant | `sub_B504D0(15, ...)` |
| 18 | SDiv | `sub_B504D0(18, ...)` |
| 21 | FMul | `sub_B504D0(21, ...)` |
| 25 | Or | `sub_B504D0(25, ...)` |
| 26 | And | `sub_B504D0(26, a, mask, ...)` |
| 28 | Xor | `sub_B504D0(28, ...)` |
| 29 | Add | `sub_B504D0(29, ...)` |
| 30 | Sub | `sub_B504D0(30, zero, operand)` |
| 32 | Shl | `sub_B504D0(32, ...)` |
| 33 | AShr | `sub_B504D0(33, ...)` |
| 38 | And (FP context) | `sub_B504D0(38, ...)` |
| 40 | ZExt (via sub_B51D30) | `sub_B51D30(40, source, resultType)` |
| 49 | CastInst | `sub_B51D30(49, src, destTy, ...)` |

### Node Builder / Cloner (`sub_16275A0`, 21KB)

The IR builder at `sub_16275A0` creates new nodes by cloning operand lists from a source node, using the tagged pointer Use-list encoding described above. It dispatches to three specialized constructors:

| Address | Role |
|---------|------|
| `sub_1627350` | Multi-operand node create (MDTuple::get equivalent). Takes `(ctx, operand_array, count, flag0, flag1)`. Called 463+ times from func-attrs and metadata passes. |
| `sub_15B9E00` | Binary node create. Fixed 2-operand layout, minimal header. |
| `sub_15C4420` | Variadic node create. Variable operand count, allocates backward operand storage. |

All three ultimately route through the uniquing function `sub_162D4F0` to deduplicate structurally-identical nodes.

### Infrastructure Functions

| Address | Call Count | Role |
|---------|------------|------|
| `sub_1623A60` | 349x | `IRBuilder::CreateBinOp` or SCEV type extension |
| `sub_1623210` | 337x | `IRBuilder::CreateUnaryOp` or SCEV use registration |
| `sub_15FB440` | 276x | Create node with 5 args: `(opcode, type, op1, op2, flags)` |
| `sub_161E7C0` | 463x | Node accessor / property query (most-called IR function) |
| `sub_164B780` | 336x | Use-chain linked list manipulation |
| `sub_1648A60` | 406x | Memory allocator: `(size, alignment)` |

## Allocation

NVVM IR nodes are allocated from a slab-based bump allocator:

- **Slab growth**: `4096 << (slab_index >> 7)` -- exponential, capped at 4TB.
- **Alignment**: 8 bytes (pointer aligned via `(ptr + 7) & ~7`).
- **Deallocation**: no individual free; entire slabs are released at once.
- **Overflow**: triggers a new slab via `malloc()`.

This is the standard LLVM BumpPtrAllocator pattern, consistent with how upstream LLVM manages IR node lifetimes. The lack of per-node deallocation means the NVVM frontend cannot reclaim memory for dead nodes until the entire context is destroyed.

## Cross-References

- [DenseMap / Hash Infrastructure](../infra/hash-infrastructure.md) -- universal hash function and DenseMap layout
- [DAG Node](./dag-node.md) -- SelectionDAG-level node layout (104-byte SDNode)
- [NVVM Container](./nvvm-container.md) -- the NVVMPassOptions/container that wraps the context
- [Bitcode I/O](../infra/bitcode-io.md) -- bitcode opcode encoding and parseFunctionBody
- [InstCombine](../llvm/instcombine.md) -- the 405KB mega-visitor that consumes these nodes
- [NVVM Verifier](../passes/nvvm-verify-deep.md) -- per-opcode validation rules

## Function Map

| Address | Size | Function |
|---------|------|----------|
| `sub_162D4F0` | 49KB | Node uniquing: lookup-or-insert, opcode dispatch |
| `sub_1621740` | 14KB | Node erase from uniquing tables (tombstone writer) |
| `sub_16275A0` | 21KB | IR builder / node cloner |
| `sub_1627350` | -- | Multi-operand node create (MDTuple::get) |
| `sub_15B9E00` | -- | Binary node create |
| `sub_15C4420` | -- | Variadic node create |
| `sub_15B3480` | -- | Hash computation for multi-operand nodes |
| `sub_1608300` | 90KB | Context destructor (frees 20+ hash tables) |
| `sub_B76CB0` | 97KB | LLVMContext destructor (~3,656-byte object) |
| `sub_B504D0` | -- | `BinaryOperator::Create` / `IRBuilder::CreateBinOp` |
| `sub_B50640` | -- | Result-typed instruction create / `CreateNeg` |
| `sub_B51BF0` | -- | `IRBuilder::CreateZExtOrBitCast` |
| `sub_B51D30` | -- | `CmpInst::Create` / `IRBuilder::CreateCast` |
| `sub_B52190` | -- | `BitCastInst::Create` |
| `sub_B52260` | -- | `GetElementPtrInst::Create` (single-index) |
| `sub_B52500` | -- | `CastInst::Create` with predicate |
| `sub_B33D10` | -- | `IRBuilder::CreateIntrinsicCall` |
| `sub_BD2DA0` | -- | `Instruction::Create` (80-byte allocation) |
| `sub_BD2C40` | -- | `Instruction::Create` (variable-size) |
| `sub_72C9A0` | -- | `create_empty_ir_node` (204 callers, EDG front-end) |
| `sub_1623A60` | -- | IR builder / node constructor (349x calls) |
| `sub_1623210` | -- | IR builder / node constructor variant (337x calls) |
| `sub_15FB440` | -- | Create node with 5 args (276x calls) |
| `sub_161E7C0` | -- | Node accessor / property query (463x calls) |
| `sub_166A310` | 60KB | `BitcodeReader::parseFunctionBody` (stock LLVM) |
| `sub_151B070` | 123KB | `parseFunctionBody` (two-phase compilation path) |
| `sub_9F2A40` | 185KB | `parseFunctionBody` (standalone libNVVM path) |
| `sub_10EE7A0` | 405KB | `InstCombinerImpl::visitInstruction` (full opcode switch) |
| `sub_F2CFA0` | -- | InstCombine master visit dispatcher |
| `sub_2C80C90` | 51KB | NVVMModuleVerifier (per-opcode validation) |
