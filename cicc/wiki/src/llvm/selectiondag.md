# SelectionDAG & Instruction Selection

CICC v13.0 contains a complete NVPTX SelectionDAG backend derived from LLVM 7.x, with substantial NVIDIA customizations for GPU-specific lowering, the PTX `.param`-space calling convention, tensor core intrinsic selection, and a 343KB intrinsic lowering mega-switch covering over 200 CUDA intrinsic IDs. The SelectionDAG pipeline converts LLVM IR into machine-level PTX instructions through four major phases: type legalization, operation legalization, DAG combining, and pattern-based instruction selection.

| | |
|---|---|
| **LowerOperation dispatcher** | `sub_32E3060` (111KB, 3,626 lines) |
| **LowerCall (.param ABI)** | `sub_3040BF0` (88KB, 2,909 lines) |
| **Intrinsic lowering switch** | `sub_33B0210` (343KB, 9,518 lines) |
| **ISel::Select driver** | `sub_3090F90` (91KB, 2,828 lines) |
| **LegalizeTypes** | `sub_20019C0` (348KB, 10,739 lines) |
| **LegalizeOp** | `sub_1FFB890` (169KB) |
| **DAG combiner visitor** | `sub_F20C20` (~64KB) |
| **computeKnownBits (NVPTX)** | `sub_33D4EF0` (114KB, 3,286 lines) |
| **Inline asm lowering** | `sub_2079C70` (83KB, 2,797 lines) |

## Type Legalization

Type legalization (`sub_20019C0`) is the largest single function in the SelectionDAG pipeline at 348KB. Unlike upstream LLVM, which splits legalization across `LegalizeIntegerTypes.cpp`, `LegalizeFloatTypes.cpp`, and `LegalizeVectorTypes.cpp`, NVIDIA ships all type-legalization logic inlined into a single monolithic dispatch. This may be an LTO artifact or a deliberate choice for branch-prediction locality.

The master switch dispatches on approximately 50 ISD opcodes. Type legalization actions follow the standard LLVM model:

- **Promote** -- widen small types to register width (e.g., `i8` to `i32`) via `ANY_EXTEND`/`ZERO_EXTEND`, perform the operation, then `TRUNCATE` the result.
- **Expand** -- split wide types into halves (e.g., `i128` into two `i64` values) using shift-and-OR sequences.
- **Soften** -- emulate unsupported FP types through integer libcall sequences.
- **Scalarize/Split Vector** -- decompose illegal vector types into scalar element operations.

The legality table lives inside `NVPTXTargetLowering` at offset `+2422`, organized as a 2D array indexed by `259 * VT + opcode`. The 259-byte row stride accommodates LLVM's ~250 generic opcodes plus approximately 10 NVPTX target-specific opcodes. A secondary condition-code action table at offset `+18112` uses 4-bit packed nibbles indexed by `(VT_row + 15 * CC)`.

The SimpleVT type encoding appears as a recurring pattern throughout the function (at least 11 instances of the same bitwidth-to-VT mapping):

| SimpleVT | Type | SimpleVT | Type |
|---|---|---|---|
| 1 | `i1` | 7 | `i128` |
| 3 | `i8` | 8 | `f16` |
| 4 | `i16` | 9 | `f32` |
| 5 | `i32` | 10 | `f64` |
| 6 | `i64` | 14--109 | vector types |

The vector type range 14--109 maps fixed-width (14--55) and scalable (56--109) vector MVTs to their scalar element types through a ~100-case switch block that appears six times in the function body.

## Operation Legalization

Operation legalization (`sub_1FFB890`, 169KB) processes each DAG node through a per-opcode action lookup and dispatches to one of five paths:

| Action | Code | Behavior |
|---|---|---|
| Legal | 0 | Return immediately -- node is natively supported |
| Custom | 1 | Call `NVPTXTargetLowering::LowerOperation` (vtable slot #164, offset `+1312`) |
| Expand | 2 | Try `LegalizeTypes`, then `ExpandNode` (`sub_1FF6F70`) as fallback |
| LibCall | 3 | Call `ExpandNode` directly for libcall substitution |
| Promote | 4 | Find a larger legal type and rebuild the node |

The custom lowering path dispatches through the NVPTX target's virtual method table at offset `+1312`. When `LowerOperation` returns NULL, the framework falls through to expansion. When it returns a different node, `ReplaceAllUsesWith` splices the replacement into the DAG and marks the old node as dead (tombstone value `-2` in the worklist hash set).

The promote path contains approximately 30 opcode-specific expansion strategies covering integer arithmetic, FP operations, vector operations, bitcasts, shifts, and NVPTX-specific operations. For FP promotion, the pattern is: `FP_EXTEND` both operands to the promoted type, apply the original operation, then `FP_ROUND` the result back.

## DAG Combining

The DAG combiner visitor (`sub_F20C20`) implements LLVM's standard per-node combine framework with several optimization phases executed sequentially:

1. **Opcode-specific combine** via `sub_100E380` -- the target-independent dispatcher.
2. **Known-bits narrowing** for constant nodes -- builds APInt masks and calls `sub_11A3F30` (computeKnownBits / SimplifyDemandedBits) to narrow constants.
3. **Operand type-narrowing loop** -- for each operand, computes the legalized type, skips zero-constant operands, creates legalized replacements, and inserts `SIGN_EXTEND`/`TRUNCATE` cast nodes as needed.
4. **All-constant-operand fold** -- detects when every operand is a constant and calls `sub_1028510` for full constant-fold evaluation. Uses a 4x-unrolled loop for the constant check.
5. **Division-by-constant strength reduction** -- replaces division by power-of-two constants with shift+mask sequences via APInt shift/mask computation.
6. **Vector stride / reassociation patterns** -- attempts associative FP decomposition via `sub_F15980`, with fast-math flag propagation when both sub-results are known non-negative.

The combiner's `ReplaceAllUsesWith` implementation (`sub_F162A0`) walks the use-list and hashes each user into a worklist map using open-addressing with hash function `((id >> 9) ^ (id >> 4)) & (size - 1)`. The worklist grows when load factor exceeds 75%.

Two global `cl::opt` flags gate specific combine paths: `qword_4F8B3C8` controls strict-FP known-bits combining, and `qword_4F8B548` controls 2-operand reassociation.

## NVPTX Custom Lowering

The `LowerOperation` dispatcher (`sub_32E3060`) handles NVPTX-specific ISD opcode lowering through a multi-phase approach rather than a clean switch-on-opcode. It is one of the largest functions analyzed, with approximately 620 local variables and a 0x430-byte stack frame.

The dispatch covers these key ISD opcodes:

| Opcode | ISD Node | Lowering Strategy |
|---|---|---|
| 51 | `UNDEF` | Direct pass-through via `getNode(UNDEF)` |
| 156 | `BUILD_VECTOR` | Iterates operands, detects all-same, calls dedicated handler |
| 186 | `VECTOR_SHUFFLE` | Three-level approach by result count (1, 2, 3+) |
| 234 | `EXTRACT_VECTOR_ELT` | Three sub-paths: predicate check, direct sub-register, general extract |

Vector shuffle lowering is the most complex section (lines 2665--3055), implementing a multi-level approach: Level 1 uses direct extract/insert for single results; Level 2 uses two-phase identity/extract detection with BitVector tracking; Level 3 falls back to general `BUILD_VECTOR`-based lowering with pairwise shuffle via `sub_32B2430`.

## The .param-Space Calling Convention

PTX does not use registers for argument passing. Instead, all arguments flow through `.param` memory space, a compiler-managed address space specifically for call sites. `LowerCall` (`sub_3040BF0`) implements this convention by emitting a structured sequence of NVPTXISD custom DAG nodes:

```
CallSeqBegin(315, seq_id, 0)
  DeclareScalarParam(506, align=4, idx=0, size=32)   // scalar arg
  DeclareParam(505, align=4, idx=1, size=N)           // struct arg (byval)
    StoreV1(571, ...)                                  // 8 bytes at a time
    StoreV2(572, ...)                                  // or 2-element vector
  DeclareRetScalarParam(508, 1, 32, 0)                // return decl
  CallProto(518, callee, ...)
  CallStart(514, ...)                                  // actual call
  LoadRetParam(515, 1, 0, ...)                         // load return value
  CallSeqEnd(517, ...)
CallSeqEnd_Outer(316, ...)
```

Each call increments a monotonic sequence counter at `NVPTXTargetLowering + 537024` (offset `134256 * 4`), used to match `CallSeqBegin`/`CallSeqEnd` pairs.

Scalar arguments narrower than 32 bits are widened to 32 bits; values between 32 and 64 bits are widened to 64 bits. This matches the PTX ABI requirement that `.param` scalars have a minimum 32-bit size. Vector arguments use `StoreV1`/`StoreV2`/`StoreV4` (opcodes 571--573) mapping to PTX `st.param.b32`, `st.param.v2.b32`, `st.param.v4.b32` and their 64-bit variants. The element count determines the opcode: 1 element uses V1, 2 uses V2, 4 uses V4.

Four call flavors exist, selected by prototype availability and call directness:

| Opcode | Name | Description |
|---|---|---|
| 510 | `CallDirect` | Direct call with prototype |
| 511 | `CallDirectNoProto` | Direct call without prototype (old-style C) |
| 512 | `CallIndirect` | Indirect call (function pointer) with prototype |
| 513 | `CallIndirectNoProto` | Indirect call without prototype |

## NVPTX Address Spaces

Address space constants appear throughout the SelectionDAG lowering:

| AS# | Name | Usage |
|---|---|---|
| 0 | `generic` | Default address space; unqualified pointers |
| 1 | `global` | `.global` memory |
| 5 | `local` | `.local` memory -- stack allocations, pointer casts in `LowerCall` |
| 6 | (unnamed) | Likely `.const` or another special space |
| 7 | `param` | `.param` memory -- used extensively in call lowering |

In `LowerCall`, pointer arguments undergo `addrspacecast` to generic (AS 0) via `sub_33F2D30`. The pointer size for AS 5 follows a power-of-two encoding: sizes 1, 2, 4, 8, 16, 32, 64, 128 bytes map to codes 2, 3, 4, 5, 6, 7, 8, 9.

## Intrinsic Lowering

The intrinsic lowering mega-switch (`sub_33B0210`, 343KB) dispatches over 200 distinct NVPTX intrinsic IDs into DAG node construction. The switch covers intrinsic IDs 0--0x310 in the main body, with high-ID ranges for texture/surface operations extending to ID 14196 (0x3774). The function contains approximately 1,000 local variables.

Key intrinsic categories:

| Category | ID Range | Handler | Count |
|---|---|---|---|
| Math ops (rounding modes) | 2, 10, 12, 20, 21, 63, ... | `sub_33FA050` | ~20 |
| WMMA / MMA (tensor core) | 0xA4--0xA8, 0x194--0x1EC | `sub_33A64B0` | 95 |
| Texture sampling | 0x5D--0x8D | `sub_33A4350` | 50 |
| Surface read/write | 0x8E--0x90 | `sub_33A3180` | 3 |
| Warp shuffle | 0xD4, 0xD5, 0xDF, 0xE0 | `sub_33FAF80` | 4 |
| Vote intrinsics | 0xE1--0xE6 | `sub_339CDA0` / `sub_339E310` | 6 |
| Atomics | 0xEB--0xF8 | `sub_3405C90` / `sub_340AD50` | ~14 |
| cp.async / TMA | 0x175--0x17C | `sub_33AD3D0` | ~8 |
| MMA sm90+ (Hopper wgmma) | 0x183--0x191 | `sub_33AC8F0` | 15 |

The WMMA/MMA block is the largest single-handler group: 95 consecutive case labels (intrinsic IDs 404--492) all delegate to `sub_33A64B0`, covering `wmma.load`, `wmma.store`, `wmma.mma`, `mma.sync` (sm70+), `mma.sp` (sm80+), and `mma.f64` (sm90+). The warp shuffle intrinsics map to specific NVPTXISD opcodes: `__shfl_down_sync` to 277, `__shfl_up_sync` to 275, `__shfl_xor_sync` to 278, and `__shfl_sync` to 276.

Math intrinsics encode explicit rounding modes via an inner opcode table. For example, `ADD_RN` (round-to-nearest) maps to opcode 252, `ADD_RZ` (round-toward-zero) to 249, `ADD_RM` (round-toward-minus-infinity) to 245, and `ADD_RP` (round-toward-plus-infinity) to 270.

## NVPTX computeKnownBits

The NVPTX target provides a custom `computeKnownBitsForTargetNode` implementation (`sub_33D4EF0`, 114KB) that propagates bit-level information through 112 opcode cases in the SelectionDAG. This function supports demanded-bits pruning via an APInt mask parameter and caps recursion at depth 6 (matching LLVM's default `MaxRecursionDepth`).

Notable NVPTX-specific known-bits behaviors:

- **Memory operation type inference** (opcode 0x12A): Propagates known bits through load operations based on extension mode (zero-extend, sign-extend, any-extend) encoded in the node flags byte at bits `[2:3]`. Handles `ld.global.u32` vs `ld.global.s32` vs `ld.global.b32` distinctions.
- **Texture/surface fetch results** (opcodes 0x152--0x161): Sets known bits in the range `[elementSize..width]` based on the result type, encoding the known bit-width of texture fetch results.
- **Constant pool integration** (opcode 0x175): Uses LLVM's `ConstantRange` class to derive known bits from constant pool values, chaining `fromKnownBits` through `intersect` to `toKnownBits`.
- **Target fence** at opcode 499 (`ISD::BUILTIN_OP_END`): All opcodes above 499 delegate to the `TargetLowering` virtual method; below that, the generic ISD switch handles everything.

APInt values with width at most 64 bits use inline storage; wider values trigger heap allocation. The constant 0x40 (64) appears hundreds of times as the inline/heap branch condition.

## Inline Assembly Lowering

The inline assembly visitor (`sub_2079C70`, 83KB) lowers LLVM IR `asm` statements into `ISD::INLINEASM` (opcode 193) or `ISD::INLINEASM_BR` (opcode 51) DAG nodes. The function allocates an 8.4KB stack frame and parses constraints through `NVPTXTargetLowering`'s virtual table.

NVPTX-specific extensions to the standard LLVM inline asm framework:

- **Convergent flag** (bit 5): Ensures barrier semantics are preserved for inline asm, checked via operand bundle attribute or function-level `convergent`.
- **Simplified constraint handling**: NVPTX directly recognizes single-character `'i'` (immediate, flag `0x20000`) and `'m'` (memory, flag `0x30000`) constraints through `sub_2043C80`, avoiding the complex multi-character constraint tables used by x86/ARM backends.
- **Stack-allocated operand buffer**: 16-entry inline capacity (7,088 bytes on stack) reflects NVIDIA's assumption that CUDA inline asm rarely exceeds 16 operands. Overflow triggers heap reallocation via `sub_205BBA0`.

Each parsed constraint occupies a 248-byte record; each operand working structure is 440 bytes. The function processes operands in five phases: initialization, constraint pre-processing, tied operand resolution, per-operand lowering, and DAG node finalization.

## ISel Pattern Matching Driver

The instruction selection driver (`sub_3090F90`) manages the top-level selection loop rather than performing pattern matching directly. It builds a cost table for function arguments using a hash table with hash function `key * 37`, processes the topological worklist using a min-heap priority queue, and calls the actual pattern matcher (`sub_308FEE0`) for each node.

The driver maintains an iteration budget of `4 * numInstructions * maxBlockSize` to guard against infinite loops. When the budget is exceeded, selection terminates for the current function.
