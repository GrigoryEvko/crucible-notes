# SelectionDAG Node Structure

The SelectionDAG (SDNode) is the central data structure in cicc's code generation backend. Nodes represent operations in the target-independent DAG before instruction selection lowers them to machine instructions. The DAG builder (`sub_2081F00`, 267KB) converts LLVM IR into an initial DAG by visiting each IR instruction through a dispatch chain rooted at `sub_2065D30`. Nodes are deduplicated via a CSE hash table (`sub_F4CEE0`, 41KB) and allocated from a bump allocator embedded in the builder context object. The complete SelectionDAG pipeline then runs type legalization, operation legalization, DAG combining, and instruction selection over this graph before emitting PTX machine instructions.

## SDNode Layout (104 Bytes, Two Views)

Every SDNode is allocated as exactly 104 bytes, hardcoded in `sub_163D530`. After allocation, all fields are zeroed. Two complementary views of the layout have been recovered: the "allocator view" from the zeroing pattern in `sub_163D530`, and the "accessor view" from field access patterns across the combiner (`sub_F20C20`), legalization (`sub_1FFB890`), and known-bits engine (`sub_33D4EF0`).

### Allocator View (from `sub_163D530`)

The raw 104 bytes are zeroed via a combination of qword and dword stores:

```
qw[0..5] = 0, dw[6] = 0, qw[8..10] = 0, dw[11] = 0, byte[96] = 0
```

The statistics counter at context offset +96 is incremented by 104 for every allocation: `*(_QWORD *)(v4 + 96) += 104LL`.

### Accessor View (Composite from Combiner, Legalizer, KnownBits)

The following table reconciles field accesses across `sub_F20C20` (DAG combiner visitor), `sub_1FFB890` (LegalizeOp), `sub_33D4EF0` (computeKnownBits, 114KB), and `sub_1FCE100` (LegalizeOp dispatcher):

| Offset | Size | Type | Field | Evidence |
|--------|------|------|-------|----------|
| +0 | 8B | `SDNode*` | `chain_next` / first operand value | D03: `*(qword*)(N+0)` used as first operand in single-operand patterns |
| +4 | 4B | `uint32_t` | `NumOperands_packed` | D03: `*(dword*)(N+4) & 0x7FFFFFF` = NumOperands (low 27 bits); bits 27--30 = flags; bit 30 (0x40 in byte +7) = hasChainOps |
| +7 | 1B | `uint8_t` | `node_flags_byte` | D03: bit 4 = hasDebugLoc; bit 6 = hasChainPtr (operand list at `N-8`) |
| +8 | 8B | `SDVTList*` | `VTList` / ValueType pointer | D03: `*(qword*)(N+8)` = result value type descriptor; D05: read for MVT extraction |
| +16 | 8B | `SDUse*` | `UseList` | D03: head of use-def chain (doubly-linked list) |
| +24 | 4B | `uint16_t` | `opcode` | D02: `*(uint16_t*)(node+24)` = SDNode::getOpcode(); D05: `*(a3+24)` switched upon |
| +28 | 4B | `uint32_t` | `opcode_flags` | D05: `*(a3+28)` = sub-flags (nsw/nuw/exact bits) |
| +32 | 8B | `SDUse*` | `operand_list` | D02: `*(node+32)` = pointer to first operand SDUse; operand stride = 40 bytes |
| +33 | 1B | `uint8_t` | `extension_mode` | D05: `*(a3+33)` bits[2:3] = load extension mode (0=none, 1=zext, 2=sext, 3=zext) |
| +40 | 8B | `ptr` | `value_list` / operand[0] type | D02: `*(node+40)` = SDValue type info; D01: result type descriptor |
| +48 | 8B | `EVT` | `result_VT` | D05: `*(a3+48)` = result VT list, 16-byte entries `{u16 MVT, pad, u64 ext}` |
| +60 | 4B | `uint32_t` | `num_values` | D02: number of result values |
| +64 | 4B | `uint32_t` | `flags` / `num_operands_alt` | D05: `*(a3+64)` = operand count (alternate access path in KnownBits) |
| +72 | 8B | `SDValue` | `chain_operand` / result EVT | D03: `*(qword*)(N+72)` = result value type; D01: chain operand for memory ops |
| +80 | 8B | `ptr` | `metadata` / mem operand | D01: `*(node+80)` = predicate for CAS; extra metadata |
| +88 | 4B | `uint32_t` | `address_space` / ordering | D01: `*(node+88)` = memory operand / address-space descriptor |
| +96 | 8B | `uint64_t` | `immediate_value` | D05: `*(a3+96)` = constant value for ConstantSDNode (width <= 64) |
| +104 | 8B | `ptr` | `extended_data` | D05: `*(a3+104)` = second immediate, type info for wide constants |
| +112 | 8B | `ptr` | `mem_chain` / alignment | D05: `*(a3+112)` = MemSDNode chain / alignment info |

**Note on dual access patterns.** The combiner accesses opcodes at `N+24` as a 4-byte field with flags, while the legalizer reads `*(uint16_t*)(node+24)` for a clean 16-bit opcode. The KnownBits engine (`sub_33D4EF0`) accesses fields at offsets up to `+112`, confirming that ConstantSDNode and MemSDNode subclasses extend beyond the base 104-byte allocation. These extended nodes are allocated via `sub_BD2DA0` (80 bytes for lightweight variants) or `sub_22077B0` (128 bytes for MemSDNode), while the base SDNode remains 104 bytes.

### Operand Storage

Operands are stored in a contiguous array of `SDUse` structures. Two storage modes exist:

**Mode A -- backward inline** (common for small operand counts). Operands are stored *before* the node in memory, growing toward lower addresses:

```
operand[i] = *(qword*)(N + 32*(i - NumOps))
// or equivalently: N - 32*NumOps = first operand address
```

This 32-byte operand stride is confirmed across `sub_F3D570`, `sub_F20C20`, and `sub_F5A610`.

**Mode B -- indirect pointer** (when `node_flags_byte` bit 6 is set). An 8-byte pointer at `N-8` points to a separately allocated operand array:

```
if (*(byte*)(N+7) & 0x40):
    operand_base = *(qword*)(N - 8)
```

The `SDUse` structure (each operand slot) has a 40-byte stride in the legalizer view (`sub_1FFB890`) and a 32-byte stride in the combiner view. The 40-byte stride includes use-chain forward/backward pointers:

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| +0 | 8B | `Val` | Pointer to the SDNode this use points to |
| +8 | 4B | `ResNo` | Result number within the pointed-to node |
| +16 | 8B | `Next` | Next SDUse in the use-list of the defining node |
| +24 | 8B | `Prev` | Previous SDUse (for doubly-linked list) |
| +32 | 8B | `User` | Back-pointer to the node that owns this operand |

Use-list traversal functions: `sub_B43C20` (add to use list), `sub_B43D60` (remove from use list).

## SDValue

An SDValue is a lightweight `{SDNode*, unsigned ResNo}` pair identifying a specific result of a specific DAG node. In the decompiled code, SDValues appear as 16-byte pairs at various points:

```
struct SDValue {
    SDNode *Node;     // +0: pointer to the defining node
    uint32_t ResNo;   // +8: which result of that node (0-based)
};
```

SDValues are passed by value in registers (packed into `__m128i` in many decompiled signatures) and stored in operand arrays. The `SDUse` structure wraps an SDValue with use-chain linkage for the def-use graph.

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

Three DenseMap/DenseSet instances are embedded inline in the context for node deduplication and worklist tracking. All use the standard DenseMap infrastructure with NVVM-layer sentinels (-8 / -16); see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function, probing strategy, and growth policy.

**Map A (CSE node mapping)** at offsets +120..+148:

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

## DAG Builder Algorithm (SelectionDAGBuilder)

The SelectionDAGBuilder converts LLVM IR to an initial SelectionDAG. The main entry is `sub_2081F00` (267KB, ~9,000 lines), with the visit dispatcher at `sub_2065D30` (25KB). The builder processes one basic block at a time, walking the IR instruction list and emitting corresponding SDNode subgraphs.

### Entry and Dispatch

```
sub_2081F00(SelectionDAGBuilder *this, BasicBlock *BB):
    // this+552 = SelectionDAG pointer
    // this+560 = DataLayout pointer
    // Walk BB instruction list via linked list at BB+40/+48
    for each instruction I in BB:
        sub_2065D30(this, I)     // main visit dispatch
```

The visit dispatcher (`sub_2065D30`) contains a DenseMap for node deduplication (hash function: `(key >> 9) ^ (key >> 4)`). It switches on the IR opcode and delegates to per-instruction visitors:

| IR Instruction | Visitor Function | Size | Notes |
|----------------|------------------|------|-------|
| Binary ops | `sub_206E5B0`--`sub_206F0D0` | 2.3KB each | 8 identical template instantiations for different ISD opcodes |
| Call | `sub_208CF60` | 56KB | Calls `sub_20C7CE0` (NVPTX ComputeCalleeInfo) |
| Load | `sub_209B000` | 15KB | Chains via `sub_2051C20` |
| Store | `sub_2090780` | 14KB | Alignment, volatile, chain tokens |
| Switch/Br | `sub_20912B0` | 18KB | Jump tables, range checks |
| PHI | `sub_20920A0` | 13KB | Block ordering, vreg setup |
| GEP | `sub_209FCA0` | 13KB | Recursive address building |
| Intrinsic | `sub_208C8A0` | 9KB | Dispatches to intrinsic handlers |
| Debug | `sub_208C270` | 7KB | Debug value/location handling |
| Inline Asm | `sub_2079C70` | 83KB | Full constraint parsing |
| NVVM Tex/Surf | `sub_2077400` | 20KB | `"nvvm_texsurf_handle"` metadata, **NVIDIA custom** |
| NVVM Args | `sub_2072590` | 38KB | CUDA argument coercion, **NVIDIA custom** |

### Chain Management

Every memory-touching SDNode carries a *chain* operand (token type) that enforces memory ordering. The chain is a linked sequence of token-typed SDValues threading through all memory operations in program order.

**Chain creation.** The builder maintains a "current chain" (`PendingChain`) that is updated after every memory operation. When a load or store is emitted, the current chain becomes its chain input, and the node's token result becomes the new current chain.

**TokenFactor merging.** When multiple independent memory operations can be reordered (e.g., independent loads), the builder creates a `TokenFactor` (opcode 2/55 depending on context) node that merges multiple chains into one:

```
// sub_F429C0: merge node creation
TokenFactor = getNode(ISD::TokenFactor, dl, MVT::Other, chains[])
```

Chain handling utilities in the builder:
- `sub_20993A0` (11KB) -- chain/token helper for load/store sequences
- `sub_2098400` -- chain token node creator
- `sub_20989A0` -- memory scheduling chain builder
- `sub_F6C1B0` (16KB) -- chain management in combining, uses `sub_B46970` (isTokenFactor)

**Glue (flag) chains.** Certain node pairs must be scheduled adjacently (e.g., CopyToReg + CALL). These use a "glue" value type (MVT::Glue) as an additional operand/result. The call lowering in `sub_3040BF0` threads glue through the entire call sequence: `CallSeqBegin -> DeclareParam* -> Store* -> CallProto -> CallStart -> LoadRetParam* -> CallSeqEnd`.

### Per-Node Analysis Structure

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

## CSE (Common Subexpression Elimination) Hash Table

The `getNode()` family of functions deduplicates SDNodes via a CSE hash table. The primary implementation is `sub_F4CEE0` (41KB):

```
sub_F4CEE0(SelectionDAG *DAG, unsigned Opcode, SDVTList VTs, SDValue *Ops, unsigned NumOps):
    // 1. Compute profile hash via sub_F4B360 (SDNode::Profile)
    //    Hash combines: opcode, VTs, all operand node pointers
    // 2. Lookup in CSE hash table:
    //    hash = ((profile >> 4) ^ (profile >> 9)) & (capacity - 1)
    //    Quadratic probing: step 1, 2, 3, ...
    //    Sentinels: -4096 (empty), -8192 (tombstone)
    // 3. If found: return existing node
    // 4. If not found:
    //    Allocate via sub_BD2C40 (bump allocator)
    //    Initialize via sub_B44260 (SDNode constructor)
    //    Insert into hash table
    //    Add to AllNodes list (global sentinel: qword_4F81430)
    //    Return new node
```

Node builder variants handle different operand counts:
- `sub_F49030` (38KB) -- complex node construction with operand/result type setup
- `sub_F429C0` (34KB) -- merge/TokenFactor/indexed node creation
- `sub_F44160` (22KB) -- CSE rebuild after modification
- `sub_F40FD0` (16KB) -- node construction with chain initialization

The AllNodes list (`qword_4F81430`) is a doubly-linked intrusive list of all SDNodes in the current DAG, used for iteration during combining and legalization passes.

## NVPTX-Specific Node Types (NVPTXISD)

NVPTX target-specific ISD opcodes begin at `ISD::BUILTIN_OP_END` = 0x1DC9 (confirmed by `sub_2095B00` delegation threshold for `getTargetNodeName()`). In the decompiled code, target opcodes are referenced by small integers (the NVPTXISD enum value minus `BUILTIN_OP_END`). The following table consolidates all NVPTXISD opcodes discovered across `sub_3040BF0`, `sub_32E3060`, `sub_33B0210`, and the legalization infrastructure:

### Call ABI Nodes

| Opcode | Name | Operands | Description |
|--------|------|----------|-------------|
| 315 | `CallSeqBegin` | chain, seqId, frameSize | Mark start of call frame |
| 316 | `CallSeqEnd_Outer` | chain, ... | Outer call-sequence-end wrapper |
| 505 | `DeclareParam` | chain, align, idx, size | Declare `.param` (byval/aggregate) |
| 506 | `DeclareScalarParam` | chain, align, idx, size | Declare `.param` (scalar, widened) |
| 507 | `DeclareRetParam` | chain, ... | Declare `.param` for return (byval callee) |
| 508 | `DeclareRetScalarParam` | chain, ... | Declare `.param` for return (scalar callee) |
| 510 | `CallDirect` | chain, callee, ... | Direct call (callee not extern) |
| 511 | `CallDirectNoProto` | chain, callee, ... | Direct call without prototype |
| 512 | `CallIndirect` | chain, ptr, ... | Indirect call via function pointer |
| 513 | `CallIndirectNoProto` | chain, ptr, ... | Indirect call without prototype |
| 514 | `CallStart` | chain, ... | Actual call instruction emission |
| 515 | `LoadRetParam` | chain, offset | Load return value from `.param` (not last) |
| 516 | `LoadRetParamLast` | chain, offset | Load last return value from `.param` |
| 517 | `CallSeqEnd` | chain, seqId, ... | End of call sequence (inner chain) |
| 518 | `CallProto` | chain, paramCount | Declare call prototype (`.callprototype`) |
| 521 | `DeclareRetParam_Ext` | chain, ... | Declare `.param` for return (extended path) |
| 527 | `StoreCalleeRetAddr` | chain, ... | Store callee return address in `.param` |
| 528 | `StoreRetValToParam` | chain, ... | Store return value to `.param` (return path) |

### Memory / Vector Nodes

| Opcode | Name | Operands | Description |
|--------|------|----------|-------------|
| 568 | `LoadV1` | chain, ptr, offset | Load 1-element from `.param` (scalar return) |
| 569 | `LoadV2` | chain, ptr, offset | Load 2-element vector from `.param` |
| 570 | `LoadV4` | chain, ptr, offset | Load 4-element vector from `.param` |
| 571 | `StoreV1` | chain, val, ptr, offset | Store 1-element to `.param` (`st.param`) |
| 572 | `StoreV2` | chain, val, ptr, offset | Store 2-element vector to `.param` |
| 573 | `StoreV4` | chain, val, ptr, offset | Store 4-element vector to `.param` |

### Math / Rounding-Mode Nodes

| Opcode | Name | Description |
|--------|------|-------------|
| 245 | `ADD_RM` | Add, round toward -inf |
| 246 | `SQRT_RP` | Sqrt, round toward +inf |
| 248 | `SQRT_RZ` | Sqrt, round toward zero |
| 249 | `ADD_RZ` | Add, round toward zero |
| 250 | `DIV_RZ` | Div, round toward zero |
| 251 | `MUL_RN` | Mul, round to nearest |
| 252 | `ADD_RN` | Add, round to nearest |
| 253 | `FMA_RN` | FMA, round to nearest |
| 254 | `SQRT_RM` | Sqrt, round toward -inf |
| 255 | `MUL_RZ` | Mul, round toward zero |
| 256 | `DIV_RM` | Div, round toward -inf |
| 267 | `FMA_RZ` | FMA, round toward zero |
| 268 | `DIV_RN` | Div, round to nearest |
| 269 | `DIV_RP` | Div, round toward +inf |
| 270 | `ADD_RP` | Add, round toward +inf |
| 271 | `FMA_RM` | FMA, round toward -inf |
| 272 | `MUL_RP` | Mul, round toward +inf |
| 273 | `FMA_RP` | FMA, round toward +inf |
| 274 | `MUL_RM` | Mul, round toward -inf |

### Address Space / Miscellaneous Nodes

| Opcode | Name | Description |
|--------|------|-------------|
| 22 | `TargetAddr` | Target address computation |
| 24 | `Wrapper` | Global address wrapping |
| 149 | `ATOMIC_LOAD` | Atomic load with scope |
| 152 | `SELECT_CC` | Ternary select on condition code |
| 154 | `SQRT_RN` | Sqrt, round to nearest |
| 189 | `MoveParam` | Read thread index / special register |
| 193--196 | `MIN/MAX` | Integer min/max variants |
| 197 | `CTPOP` | Population count |
| 198--204 | `ConstPool*` | Constant pool variants by size |
| 208 | `CMPXCHG` | Compare-and-exchange atomic |
| 230 | `DeclareLocal` | Declare local `.param` / address of param |
| 233--234 | `AddrSpaceCast` | Bidirectional address space cast pair |
| 287--290 | `Barrier/Fence` | Memory barrier/fence variants |
| 310 | `Annotation` | Annotation metadata node |
| 321 | `StackRestore` | Restore stack pointer |
| 322 | `StackAlloc` | Dynamic stack allocation |
| 330 | `FunctionAddr` | Function address |
| 335 | `BinaryArith` | Generic binary arithmetic |
| 371 | `DynAreaOffset` | Dynamic alloca offset |
| 499 | `ConditionalBranch` | Conditional branch with chain |

### Atomic Opcodes (from `sub_20BED60`)

| Opcode Range | Operation | Widths |
|--------------|-----------|--------|
| 294--297 | `atom.add` | f32/f64/i32/i64 |
| 302--305 | `atom.min` | s32/s64/u32/u64 |
| 314--317 | `atom.max` | s32/s64/u32/u64 |
| 462 | `atom.cas` | generic |

## DAG Legalization Flow

After the initial DAG is built, three legalization phases transform it into a form the NVPTX backend can select:

### Phase 1: Type Legalization (`sub_20019C0`, 348KB)

The DAGTypeLegalizer iterates to fixpoint. For each node, it reads the result/operand types and checks the legality table at `TLI + 259 * VT + opcode + 2422`. If illegal, it applies one of: promote, expand, soften, scalarize, or split-vector. The worklist iterates until no node has an illegal type.

NVPTX legal vector types are extremely limited (only v2f16, v2bf16, v2i16, v4i8 -- all packing into 32-bit registers via `Int32HalfRegs`). This means virtually all LLVM-IR vector operations pass through the split/scalarize paths.

Type legalization workers:
- `sub_201E5F0` (81KB) -- promote/expand secondary dispatch (441 case labels, 6 switches)
- `sub_201BB90` (75KB) -- ExpandIntegerResult (632 case labels)
- `sub_2029C10` -- SplitVectorResult dispatcher (reads opcode at `node+24`)
- `sub_202E5A0` -- SplitVectorOperand dispatcher
- `sub_2036110` -- ScalarizeVectorResult
- `sub_2035F80` -- ScalarizeVectorOperand

### Phase 2: Operation Legalization (`sub_1FFB890`, 169KB)

After types are legal, the operation legalizer checks whether each *operation* at its now-legal type is supported. The action lookup:

```
action = *(uint8_t*)(TLI + 259*VT + opcode + 2422)
```

Actions dispatch through a five-way switch:

| Action | Code | Behavior |
|--------|------|----------|
| Legal | 0 | Return immediately |
| Custom | 1 | Call `TLI->LowerOperation()` via vtable slot #164 (offset 1312) |
| Expand | 2 | Try `sub_20019C0` (LegalizeTypes), then `sub_1FF6F70` (ExpandNode) |
| LibCall | 3 | Call `sub_1FF6F70` directly |
| Promote | 4 | Find next legal type, rebuild at promoted type |

Custom lowering invokes `NVPTXTargetLowering::LowerOperation()` (`sub_32E3060`, 111KB) through the vtable. This is where all NVPTX-specific operation lowering happens: `BUILD_VECTOR` splat detection, `VECTOR_SHUFFLE` three-level lowering, `EXTRACT_VECTOR_ELT` three-path dispatch, and the `.param`-space calling convention.

Additional action tables:
- Second table at `TLI + opcode + 2681` -- for BSWAP/CTLZ/CTTZ/BITREVERSE (opcodes 43--45, 199)
- Third table at `TLI + opcode + 3976` -- for FSINCOS (opcode 211)
- Fourth table at `TLI + 18112` -- packed nibble format for FP_TO_SINT/FP_TO_UINT/SELECT_CC, indexed by `(VT_id >> 3) + 15 * condcode_type`

### Phase 3: DAG Combining (Three Passes)

DAG combining runs after each legalization phase. The orchestrator (`sub_F681E0`, 65KB) manages a worklist of SDNodes and calls the per-node visitor (`sub_F20C20`, 64KB) for each. The visitor implements a six-phase combine algorithm:

1. **Opcode-specific combine** via `sub_100E380` -- target-independent pattern matching
2. **Known-bits narrowing** -- for constants, calls `sub_11A3F30` (computeKnownBits/SimplifyDemandedBits) and narrows if fewer bits demanded
3. **Operand type-narrowing loop** -- walks all operands, promotes/truncates to legal types, creates `SIGN_EXTEND`/`TRUNCATE` casts
4. **All-constant-operand fold** -- 4x-unrolled check via `sub_1028510` (ConstantFold)
5. **Division-by-constant strength reduction** -- shift+mask replacement for power-of-2 divisors
6. **Vector stride / reassociation** -- `sub_F15770` (shift-fold), `sub_F17ED0` (stride patterns)

**NVPTX-specific combines** run as a post-legalize pass:
- `sub_33C0CA0` (62KB) -- `PerformDAGCombine`, the NVPTX target hook
- `sub_32EC4F0` (92KB) -- post-legalize combine
- `sub_3425710` (142KB) -- the NVIDIA DAGCombiner with internal `"COVERED"`/`"INCLUDED"` debug tracing strings (not present in upstream LLVM)

The worklist uses the same DenseMap infrastructure as the builder context, with the hash at `DAG+2072` (capacity at `DAG+2088`, count at `DAG+2080`). Node replacement goes through `sub_F162A0` (CombineTo/ReplaceAllUsesWith), which walks the use-list, hashes each user into the worklist map, then calls `sub_BD84D0` for the actual use-chain splice.

## Bump Allocator

The builder context uses a slab-based bump allocator identical to the one used for NVVM IR nodes:

- **Slab growth**: `4096 << (slab_index >> 7)` -- exponential, capped at 4TB.
- **Alignment**: 8 bytes.
- **No per-node free**: entire slabs are released when the DAG is destroyed.
- **Overflow**: allocates a new slab via `malloc()`.

Since every base SDNode is exactly 104 bytes (13 qwords), a single 4096-byte initial slab holds approximately 39 nodes before overflow triggers slab growth. Extended node types (ConstantSDNode, MemSDNode) may be larger and are allocated via separate paths:
- `sub_BD2C40` -- standard SDNode allocation (bump allocator)
- `sub_BD2DA0` -- SDNode allocation variant (80 bytes, for lightweight nodes)
- `sub_22077B0` -- `operator new[]` (128 bytes, for MemSDNode with chain/alignment fields)

## Basic Block Iteration

The builder iterates over the function's basic blocks via a linked list rooted at `a2 + 72` (the function parameter). Each list node embeds the data pointer at offset -24 from the node:

```
bb_data = node_ptr - 24
```

Within each basic block, instructions are iterated via an inner list:
- Inner list sentinel at `bb_data + 40`
- Inner list head at `bb_data + 48`

This matches the LLVM `ilist` intrusive linked list pattern where the list hook is embedded at a fixed offset within the contained object.

## Differences from Upstream LLVM

| Area | NVIDIA (cicc v13.0) | Upstream LLVM 20.0 |
|------|---------------------|---------------------|
| Type legalizer structure | Single 348KB monolithic function (`sub_20019C0`) | Split across 4 files (`LegalizeIntegerTypes.cpp`, etc.) |
| NVIDIA DAGCombiner | 142KB `sub_3425710` with `"COVERED"`/`"INCLUDED"` internal tracing | No equivalent; target combines via `PerformDAGCombine` hook only |
| computeKnownBits | 114KB `sub_33D4EF0`, covers 112+ ISD opcodes including NVPTX target nodes | ~30 opcodes in generic `computeKnownBits`, target extends via hook |
| Inline asm | 162KB total (`sub_2079C70` + `sub_338BA40`) | ~200 lines per target |
| Intrinsic lowering | 343KB switch covering 200+ intrinsic IDs up to 14196 | ~300 standard intrinsic IDs |
| Address spaces | AS 101 (param alt), AS 7 (`.param`), CTA/GPU/SYS scope atomics | No AS 101; no scope atomics |
| Libcall metadata | `"nvptx-libcall-callee"` metadata for custom libcall routing | Not present |
| Legal vector types | Only v2f16, v2bf16, v2i16, v4i8 (packed into 32-bit registers) | Varies by target; typically much wider vectors |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| SelectionDAG builder context init | `sub_163D530` | 73KB | Allocator, DenseMaps, BB iteration |
| SelectionDAGBuilder::visit | `sub_2081F00` | 267KB | IR-to-DAG main lowering |
| SelectionDAGBuilder visit dispatch | `sub_2065D30` | 25KB | Per-instruction routing |
| visitCall | `sub_208CF60` | 56KB | Call lowering into DAG |
| visitLoad | `sub_209B000` | 15KB | Load chain emission |
| visitStore | `sub_2090780` | 14KB | Store alignment/chain |
| visitSwitch/Br | `sub_20912B0` | 18KB | Control flow lowering |
| visitPHI | `sub_20920A0` | 13KB | PHI node handling |
| visitGEP | `sub_209FCA0` | 13KB | Address computation |
| visitInlineAsm | `sub_2079C70` | 83KB | Inline asm constraint parsing |
| visitNVVMTexSurf | `sub_2077400` | 20KB | NVIDIA tex/surf handle lowering |
| NVPTX argument coercion | `sub_2072590` | 38KB | CUDA kernel argument lowering |
| getNode / CSE hash table | `sub_F4CEE0` | 41KB | Node deduplication |
| SelectionDAG node builder | `sub_F49030` | 38KB | Complex node construction |
| Merge/TokenFactor creation | `sub_F429C0` | 34KB | Chain merging, indexed nodes |
| DAG combiner orchestrator | `sub_F681E0` | 65KB | Worklist management |
| DAG combiner visitor | `sub_F20C20` | 64KB | Per-node combine algorithm |
| combine() opcode dispatch | `sub_100E380` | -- | Target-independent combines |
| CombineTo / RAUW | `sub_F162A0` | -- | Use-chain replacement + worklist push |
| SDNode allocation | `sub_BD2C40` | -- | Bump allocator |
| SDNode constructor | `sub_B44260` | -- | Initialization |
| SDUse add to use list | `sub_B43C20` | -- | Use-chain linkage |
| SDUse remove from use list | `sub_B43D60` | -- | Use-chain unlinkage |
| ReplaceAllUsesWith | `sub_BD84D0` | -- | Raw use-chain splice |
| transferDbgValues | `sub_BD6B90` | -- | Debug info transfer |
| setOperand | `sub_B91C10` | -- | Operand mutation |
| replaceOperand | `sub_B99FD0` | -- | Single operand swap |
| DAGTypeLegalizer::run | `sub_20019C0` | 348KB | Type legalization master dispatch |
| LegalizeOp | `sub_1FFB890` | 169KB | Operation legalization |
| ExpandNode | `sub_1FF6F70` | -- | Full node expansion fallback |
| NVPTXTargetLowering::LowerOperation | `sub_32E3060` | 111KB | NVPTX custom operation lowering |
| NVPTXTargetLowering::LowerCall | `sub_3040BF0` | 88KB | `.param` calling convention |
| Intrinsic lowering switch | `sub_33B0210` | 343KB | 200+ CUDA intrinsic IDs |
| PerformDAGCombine (NVPTX) | `sub_33C0CA0` | 62KB | Post-legalize NVPTX combines |
| NVIDIA DAGCombiner | `sub_3425710` | 142KB | NVIDIA-specific combine engine |
| computeKnownBits (NVPTX) | `sub_33D4EF0` | 114KB | 112-opcode known-bits transfer |
| ISel::Select driver | `sub_3090F90` | 91KB | Pattern matching entry |
| getOperationName | `sub_2095B00` | 35KB | ISD opcode -> string mapping |

## Cross-References

- [SelectionDAG & Instruction Selection](../llvm/selectiondag.md) -- pipeline overview, NVPTX lowering, combine detail
- [Type Legalization](../llvm/type-legalization.md) -- 348KB type legalizer deep-dive
- [ISel Patterns](../llvm/isel-patterns.md) -- instruction selection pattern database
- [Register Classes](../reference/register-classes.md) -- NVPTX register class constraints
- [Address Spaces](../reference/address-spaces.md) -- address space encoding
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- universal DenseMap documentation
- [IR Node Structure](ir-node.md) -- NVVM IR node layout (pre-SelectionDAG)
- [Pattern Database](pattern-db.md) -- ISel pattern constraint classes
