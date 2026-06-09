# SelectionDAG & Instruction Selection

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** Target-independent DAG infrastructure: `llvm/lib/CodeGen/SelectionDAG/SelectionDAG.cpp`, `DAGCombiner.cpp`, `LegalizeDAG.cpp`, `LegalizeTypes.cpp`, `SelectionDAGBuilder.cpp`, `SelectionDAGISel.cpp`. NVPTX target: `llvm/lib/Target/NVPTX/NVPTXISelLowering.cpp`, `NVPTXISelDAGToDAG.cpp`, `NVPTXInstrInfo.td` (LLVM 20.0.0).
>
> **LLVM version note:** The target-independent SelectionDAG infrastructure at `0xF05000`--`0xF70000` appears to be stock LLVM 20 with no detectable NVIDIA modifications. All NVIDIA customization lives in the NVPTX target range (`0x3290000`--`0x35FFFFF`) via virtual dispatch through `NVPTXTargetLowering` and `NVPTXDAGToDAGISel`. The intrinsic lowering switch (`sub_33B0210`, 60KB) dispatches 785 cases over Intrinsic::ID values 0–0x310, far exceeding upstream NVPTX which covers approximately IDs 0–300.

CICC v13.0 contains a complete NVPTX SelectionDAG backend derived from LLVM 20.0.0, with substantial NVIDIA customizations for GPU-specific lowering, the PTX `.param`-space calling convention, tensor core intrinsic selection, and a 343KB intrinsic lowering mega-switch covering over 200 CUDA intrinsic IDs. The SelectionDAG pipeline converts LLVM IR into machine-level PTX instructions through four major phases: type legalization, operation legalization, DAG combining, and pattern-based instruction selection.

The NVPTX SelectionDAG backend spans roughly 4MB of code across two address ranges: `0xF05000`--`0xF70000` for the target-independent DAG infrastructure (combining, known-bits, node management) and `0x3290000`--`0x35FFFFF` for the NVPTX-specific lowering, instruction selection, and register allocation. The infrastructure range is stock LLVM with no detectable NVIDIA modifications; all NVIDIA customization lives in the latter range via target hooks and virtual dispatch.

| | |
|---|---|
| **LowerOperation dispatcher** | `sub_32E3060` (111KB, 3,626 lines) |
| **LowerCall (.param ABI)** | `sub_3040BF0` (88KB, 2,909 lines) |
| **Intrinsic lowering switch** | `sub_33B0210` (60 KB binary / 343 KB decompiled C, 9,518 lines, 785-case main switch over Intrinsic::ID 0–0x310) |
| **ISel::Select driver** | `sub_3090F90` (91KB, 2,828 lines) |
| **LegalizeTypes** | `sub_20019C0` (348KB, 10,739 lines) |
| **LegalizeOp dispatcher** | `sub_1FCE100` (91KB, ~100 opcodes) |
| **LegalizeOp action dispatch** | `sub_1FFB890` (137KB, 967 cases) |
| **DAG combiner visitor** | `sub_F20C20` (64KB) |
| **DAG combiner orchestrator** | `sub_F681E0` (65KB) |
| **DAGCombiner::combine (NVPTX)** | `sub_3425710` (142KB, "COVERED"/"INCLUDED" tracing) |
| **PerformDAGCombine (NVPTX)** | `sub_33C0CA0` (62KB) |
| **DAG combine: post-legalize** | `sub_32EC4F0` (92KB) |
| **computeKnownBits (NVPTX)** | `sub_33D4EF0` (114KB, 3,286 lines) |
| **Inline asm lowering** | `sub_2079C70` (83KB, 2,797 lines) |
| **Inline asm constraints (NVPTX)** | `sub_338BA40` (79KB) |
| **NVPTXTargetLowering init** | `sub_3056320` (45KB, constructor) |
| **Type legalization setup** | `sub_3314670` (73KB, table population) |
| **Upstream** | `lib/CodeGen/SelectionDAG/`, `lib/Target/NVPTX/NVPTXISelLowering.cpp` |

## Complexity

Let N = number of DAG nodes and E = number of edges (use-def relationships). The SelectionDAG pipeline runs eight sequential phases. SelectionDAGBuilder converts IR instructions to DAG nodes in O(I) where I = LLVM IR instruction count. Each DAG Combiner pass is worklist-driven: O(N) nodes are visited, each matched against pattern rules in O(1) via opcode dispatch; `ReplaceAllUsesWith` is O(U) per node where U = uses. The three combiner passes total O(3 * N * U_avg). Type legalization (`sub_20019C0`, 348KB) iterates until all types are legal — each iteration processes O(N) nodes, and convergence is guaranteed in O(T) iterations where T = max type-promotion depth (typically 2–3 for GPU types). Operation legalization (`sub_1FFB890`, 137KB) visits each node once: O(N). The action table lookup is O(1) via the 2D array at `TLI + 259 * VT + opcode + 2422`. ISel pattern matching (`sub_3090F90`, 91KB) visits each node once in topological order: O(N). Per-node matching is O(P) where P = number of patterns for that opcode, but NVPTX patterns are organized by opcode-indexed tables making this effectively O(1) for common opcodes. The DAG worklist uses `((addr >> 9) ^ (addr >> 4)) & (cap - 1)` hashing for O(1) amortized membership tests. Overall: O(I + N * U_avg * 3 + N * T + N) which simplifies to O(N * U_avg) in practice. The intrinsic lowering mega-switch (343KB, 200+ IDs) adds O(1) per intrinsic call via the jump table, not O(200).

## Pipeline Position

The SelectionDAG phases execute in a fixed sequence after SelectionDAGBuilder (`sub_2081F00`) converts LLVM IR into an initial DAG:

1. **SelectionDAGBuilder** — IR-to-DAG lowering, visitor dispatch at `sub_2065D30`
2. **DAG Combiner** (`sub_F681E0` / `sub_F20C20`) — initial algebraic simplification
3. **DAGTypeLegalizer** (`sub_20019C0`) — iterates to fixpoint until all types are legal; see [Type Legalization](type-legalization.md)
4. **DAG Combiner** — second pass after type legalization
5. **LegalizeDAG** (`sub_1FCE100` dispatcher, `sub_1FFB890` action engine) — legalizes operations on legal types
6. **DAG Combiner** — third pass after operation legalization
7. **NVPTXTargetLowering::PerformDAGCombine** (`sub_33C0CA0`) — NVPTX-specific post-legalize combines
8. **Instruction Selection** (`sub_3090F90`) — see [ISel Patterns](isel-patterns.md)

## Type Legalization

Type legalization (`sub_20019C0`) is the largest single function in the SelectionDAG pipeline at 348KB. Unlike upstream LLVM, which splits legalization across `LegalizeIntegerTypes.cpp`, `LegalizeFloatTypes.cpp`, and `LegalizeVectorTypes.cpp`, NVIDIA ships all type-legalization logic inlined into a single monolithic dispatch. This may be an LTO artifact or a deliberate choice for branch-prediction locality.

The master switch dispatches on approximately 50 ISD opcodes. Type legalization actions follow the standard LLVM model:

- **Promote** — widen small types to register width (e.g., `i8` to `i32`) via `ANY_EXTEND`/`ZERO_EXTEND`, perform the operation, then `TRUNCATE` the result.
- **Expand** — split wide types into halves (e.g., `i128` into two `i64` values) using shift-and-OR sequences.
- **Soften** — emulate unsupported FP types through integer libcall sequences.
- **Scalarize/Split Vector** — decompose illegal vector types into scalar element operations.

The legality table lives inside `NVPTXTargetLowering` at offset `+2422`, organized as a 2D array indexed by `259 * VT + opcode`. The 259-byte row stride accommodates LLVM's ~250 generic opcodes plus approximately 10 NVPTX target-specific opcodes. A secondary condition-code action table at offset `+18112` uses 4-bit packed nibbles indexed by `(VT_row + 15 * CC)`.

The SimpleVT type encoding appears as a recurring pattern throughout the function (at least 11 instances of the same bitwidth-to-VT mapping):

| SimpleVT | Type | SimpleVT | Type |
|---|---|---|---|
| 1 | `i1` | 7 | `i128` |
| 3 | `i8` | 8 | `f16` |
| 4 | `i16` | 9 | `f32` |
| 5 | `i32` | 10 | `f64` |
| 6 | `i64` | 14–109 | vector types |

The vector type range 14–109 maps fixed-width (14–55) and scalable (56–109) vector MVTs to their scalar element types through a ~100-case switch block that appears six times in the function body. The definitive `MVT::getSizeInBits()` mapping (confirmed at `sub_1FDDC20`) is:

| MVT Range | Bits | Description |
|---|---|---|
| 0, 1 | 0 | Other, Glue |
| 2 | 1 | `i1` |
| 3 | 8 | `i8` |
| 4, 8 | 16 | `i16`, `f16` |
| 5, 9 | 32 | `i32`, `f32` |
| 6, 10 | 64 | `i64`, `f64` |
| 7 | 128 | `i128` |
| 11 | 80 | `ppcf128` / `x87 f80` |
| 14–23 | varies | 2-element vectors |
| 24–109 | varies | 3+ element vectors |
| 111–114 | 0 | token, metadata, untyped |

Type legalization workers fan out from several dispatch functions:

| Dispatcher | Role | Size | Cases |
|---|---|---|---|
| `sub_201E5F0` | Promote/expand secondary dispatch | 81KB | 441 case labels, 6 switches |
| `sub_201BB90` | ExpandIntegerResult | 75KB | 632 case labels |
| `sub_2000100` | PromoteIntegerResult | 45KB | recursive self-calls |
| `sub_2029C10` | SplitVectorResult | 5KB (dispatcher) | ~190 cases |
| `sub_202E5A0` | SplitVectorOperand | 6KB (dispatcher) | ~157 cases |
| `sub_2036110` | ScalarizeVectorResult | dispatch | "Do not know how to scalarize..." |
| `sub_2035F80` | ScalarizeVectorOperand | dispatch | "Do not know how to scalarize..." |

For complete detail, see [Type Legalization](type-legalization.md).

## Operation Legalization

### LegalizeOp Dispatcher: `sub_1FCE100`

The top-level operation legalizer (`sub_1FCE100`, 91KB) is a massive switch on `SDNode::getOpcode()` (read as `*(uint16_t*)(node + 24)`) that dispatches approximately 100 ISD opcodes to dedicated per-opcode handler functions. The switch covers all major categories:

| Opcode | ISD Name | Handler | Size |
|---|---|---|---|
| 0x02 | `EntryToken` | `sub_1F823C0` | |
| 0x03–0x04 | `TokenFactor` | `sub_1F73660` | |
| 0x32 | `CopyFromReg` | `sub_1F78510` | |
| 0x33 | `CopyToReg` | `sub_1F987D0` | |
| 0x34 | `MERGE_VALUES` | `sub_1FC08F0` | |
| 0x35 | `ADD` | `sub_1FA8F90` | 31KB |
| 0x36 | `SUB` | `sub_1FAA420` | 26KB |
| 0x37 | `MUL` | `sub_1FAB9E0` | |
| 0x38 | `SDIV`/`UDIV` | `sub_1FABFF0` | |
| 0x39–0x3A | `SREM`/`UREM` | `sub_1F99DA0` | |
| 0x3B | `AND` | `sub_1FD2F20` | |
| 0x3C | `OR` | `sub_1FD2A20` | |
| 0x40 | `SHL` | `sub_1FA27D0` | |
| 0x41 | `SRA` | `sub_1FA2510` | |
| 0x42 | `SRL` | `sub_1F71080` | |
| 0x43 | `ROTL` | inline | builds opcode 65 target node |
| 0x44 | `ROTR` | `sub_1FA2D60` | |
| 0x47 | `CTLZ` | `sub_1FA7370` | |
| 0x49 | `CTPOP` | `sub_1FA2A00` | |
| 0x4A | `BSWAP` | inline | 16-bit width check |
| 0x4B | `BITREVERSE` | inline | |
| 0x4C | `SELECT` | `sub_1FAC480` | 78KB |
| 0x4D | `SELECT_CC` | `sub_1FAE680` | 87KB |
| 0x4E | `SETCC` | `sub_1FB04B0` | 26KB |
| 0x4F | `VSELECT` | `sub_1FCC170` | |
| 0x63 | `SIGN_EXTEND` | `sub_1F8D440` | 22KB |
| 0x65 | `ZERO_EXTEND` | `sub_1F74E80` | |
| 0x68 | `TRUNCATE` | `sub_1F912F0` | 77KB |
| 0x69 | `FP_ROUND` | `sub_1F97850` | 27KB |
| 0x6A | `FP_EXTEND` | `sub_1FC15C0` | 36KB |
| 0x6C | `BITCAST` | `sub_1F94350` | 22KB |
| 0x6D | `LOAD` | inline | alignment+memtype checks |
| 0x70 | `STORE` | `sub_1F766E0` | |
| 0x72–0x75 | `ATOMIC_FENCE`..`LOAD` | `sub_1FAA010` | |
| 0x76 | `ATOMIC_STORE` | `sub_1FBDC00` | 76KB |
| 0x77 | `ATOMIC_LOAD_ADD` | `sub_1FB1F30` | 37KB |
| 0x78 | `ATOMIC_LOAD_SUB` | `sub_1FBB600` | 44KB |
| 0x7A | `ATOMIC_LOAD_AND` | `sub_1FB8710` | 47KB |
| 0x7B | `ATOMIC_LOAD_OR` | `sub_1FBA730` | 24KB |
| 0x7C | `ATOMIC_LOAD_XOR` | `sub_1FB6C10` | 39KB |
| 0x86 | `INTRINSIC_WO_CHAIN` | `sub_1F9E480` | 47KB |
| 0x87 | `INTRINSIC_W_CHAIN` | `sub_1F9D3D0` | 26KB |
| 0x88 | `INTRINSIC_VOID` | `sub_1F9CFD0` | |
| 0x8E | `BUILD_VECTOR` | `sub_1FA3B00` | 26KB |
| 0x8F | `INSERT_VECTOR_ELT` | `sub_1FA4AC0` | 67KB |
| 0x90 | `EXTRACT_VECTOR_ELT` | `sub_1FA0CA0` | 20KB |
| 0x91 | `CONCAT_VECTORS` | `sub_1FB3BB0` | 65KB |
| 0x94 | `EXTRACT_SUBVECTOR` | `sub_1FB5FC0` | 19KB |
| 0x9A | `DYNAMIC_STACKALLOC` | `sub_1F8F600` | |
| 0x9E | `BR_CC` | `sub_1F8B6C0` | |

Opcodes not listed (0–1, 5–0x31, 0x3D–3F, 0x46, 0x48, 0x51–0x62, etc.) return immediately with code 0 (legal, no transformation needed).

### Action Dispatch Engine: `sub_1FFB890`

The operation legalization action engine (`sub_1FFB890`, 137KB) determines *what to do* for each DAG node based on the target's action table, then executes the chosen strategy. It reads the per-opcode action byte from `NVPTXTargetLowering + 2422` using the formula `*(uint8_t*)(TLI + 259 * VT + opcode + 2422)`:

| Action | Code | Behavior |
|---|---|---|
| Legal | 0 | Return immediately — node is natively supported |
| Custom | 1 | Call `NVPTXTargetLowering::LowerOperation` (vtable slot #164, offset `+1312`); if NULL returned, fall through to expand |
| Expand | 2 | Try `LegalizeTypes`, then `ExpandNode` (`sub_1FF6F70`) as fallback |
| LibCall | 3 | Call `ExpandNode` directly for libcall substitution |
| Promote | 4 | Find a larger legal type and rebuild the node |

The function contains 967 case labels dispatching on opcode. When `LowerOperation` returns NULL (the custom lowering cannot handle the node), the framework falls through to the expansion path. When it returns a different node, `ReplaceAllUsesWith` (`sub_1D44C70`) splices the replacement into the DAG and marks the old node as dead (tombstone value `-2` in the worklist hash set).

The promote path contains approximately 30 opcode-specific expansion strategies covering integer arithmetic, FP operations, vector operations, bitcasts, shifts, and NVPTX-specific operations. For FP promotion, the pattern is: `FP_EXTEND` both operands to the promoted type, apply the original operation, then `FP_ROUND` the result back.

Worklist management uses `sub_1FF5010` with a DenseSet-like structure. The hash function for SDNode pointers follows the standard LLVM pattern: `((addr >> 9) ^ (addr >> 4)) & (capacity - 1)`.

### Load/Store Legalization

The largest individual per-opcode handlers deal with memory operations:

| Handler | Opcode | Size | Behavior |
|---|---|---|---|
| `sub_1FC2C30` | LOAD (complex) | 70KB | Extending loads, vector loads, memory type conversion |
| `sub_1FC66B0` | Load/Store vectorization | 68KB | Offset-based coalescing with introsort (`sub_1F6CA30`) |
| `sub_1FC9570` | STORE legalization | 60KB | Alignment checks, store splitting, scatter sequences |

The load/store vectorization helper sorts operands by memory offset to detect coalescing opportunities, then creates vector load/store sequences when contiguous accesses are found. This is important for NVPTX because PTX supports `ld.v2`/`ld.v4`/`st.v2`/`st.v4` instructions that load/store 2 or 4 elements in a single transaction.

### Atomic Legalization

All atomic operations (`ATOMIC_STORE` through `ATOMIC_LOAD_XOR`, opcodes 0x72–0x7C) follow a shared structural pattern:

1. Check operation legality via `sub_1D16620` (`isAtomicStoreLegal` / `isOperationLegalOrCustom`)
2. If legal, emit the operation directly
3. If custom, call `NVPTXTargetLowering::LowerOperation` for scope-aware NVPTX atomics
4. Build atomic fence pairs around the operation when needed
5. Lower to target-specific NVPTX atomic operations with CTA/GPU/SYS scope

The `ATOMIC_LOAD_SUB` handler at `sub_1FBB600` converts subtraction to `atom.add` of the negated operand when the target lacks native `atom.sub`.

## NVPTX Custom Lowering: `sub_32E3060`

The `LowerOperation` dispatcher (`sub_32E3060`, 111KB) handles NVPTX-specific ISD opcode lowering. This is the second-largest function in the `0x32XXXXX` range. It operates through a multi-phase approach rather than a clean switch-on-opcode, with approximately 620 local variables and a `0x430`-byte stack frame.

The dispatcher is reached via vtable slot #164 (offset `+1312`) of the `NVPTXTargetLowering` object whenever the operation legalizer encounters action code 1 (Custom).

### Supported Opcodes

| Opcode | ISD Node | Lowering Strategy |
|---|---|---|
| 51 | `UNDEF` | Direct pass-through via `getNode(UNDEF)` |
| 156 | `BUILD_VECTOR` | Iterates operands, detects all-same, calls dedicated handler |
| 186 | `VECTOR_SHUFFLE` | Three-level approach by result count (1, 2, 3+) |
| 234 | `EXTRACT_VECTOR_ELT` | Three sub-paths: predicate check, direct sub-register, general extract |

Additionally, the function handles load/store lowering (`sub_32D2680`, 81KB companion), integer/FP operation legalization (`sub_32983B0`, 79KB), address space casts (`sub_32C3760`, 54KB), bitcast/conversion (`sub_32C7250`, 57KB), and conditional/select patterns (`sub_32BE8D0`, 54KB). These large helper functions are called from within `sub_32E3060`'s dispatch logic.

### BUILD_VECTOR Lowering

`BUILD_VECTOR` (opcode 156) lowering begins by iterating all operands to detect the all-same (splat) case. When all elements are the same value, the lowering produces a single scalar load followed by register-class-appropriate replication. When elements differ, it falls through to a per-element insert chain.

For NVPTX, `BUILD_VECTOR` is significant because PTX has no native vector construction instruction — vectors are built by storing elements into `.param` space and reloading as a vector type, or through register-pair packing for 2-element vectors.

### VECTOR_SHUFFLE Three-Level Lowering

Vector shuffle lowering (lines 2665–3055 of the decompilation) implements a three-level strategy based on the result element count:

**Level 1 — Single-result shuffle.** When the shuffle produces a single element, the lowering extracts the source element directly via `EXTRACT_VECTOR_ELT` and wraps it in a `BUILD_VECTOR` if needed. This avoids any actual shuffle machinery.

**Level 2 — Two-result shuffle.** The handler uses a two-phase identity/extract detection with `BitVector` tracking. Phase A scans the shuffle mask to identify which source elements map to which result positions. Phase B determines whether each result position is an identity (element already in the correct position in one of the source vectors) or requires extraction. Results that are identities are left in place; non-identity elements are extracted and inserted.

**Level 3 — General shuffle (3+ results).** Falls back to a `BUILD_VECTOR`-based reconstruction. Each result element is individually extracted from the appropriate source vector using `EXTRACT_VECTOR_ELT`, then all elements are combined via `BUILD_VECTOR`. For certain mask patterns, pairwise shuffle via `sub_32B2430` is attempted first as an optimization.

### EXTRACT_VECTOR_ELT Three Sub-Paths

`EXTRACT_VECTOR_ELT` (opcode 234) lowering takes one of three paths based on the extraction context:

1. **Predicate extraction.** When extracting from a vector of `i1` (predicates), the lowering produces a bitwise test on the packed predicate register. This is NVPTX-specific: PTX stores predicate vectors packed into integer registers.

2. **Direct sub-register extraction.** When the element index is a compile-time constant and the element aligns with a register boundary, the lowering generates a direct sub-register reference. This maps to PTX's `mov.b32` or `mov.b64` for extracting elements from packed register pairs.

3. **General extraction.** For non-constant indices or non-aligned elements, the lowering stores the entire vector to local memory, computes the byte offset from the index, and loads the element back. This generates `st.local` + `ld.local` sequences, which is expensive but handles all cases.

### Supporting NVPTX Lowering Functions

The custom lowering infrastructure at `0x3290000`--`0x32FFFFF` consists of approximately 13 large functions totaling ~850KB:

| Function | Size | Role |
|---|---|---|
| `sub_32E3060` | 111KB | Master LowerOperation dispatcher |
| `sub_32A1EF0` | 109KB | Custom type promotion for NVPTX types |
| `sub_32EC4F0` | 92KB | Post-legalize DAG combine |
| `sub_32FE970` | 88KB | Vector operation splitting/scalarization |
| `sub_32D2680` | 81KB | Load/store DAG lowering (address space, alignment) |
| `sub_32983B0` | 79KB | Integer/FP operation legalization |
| `sub_32B8A20` | 71KB | NVVM intrinsic lowering (tex/surf/special) |
| `sub_32CBCB0` | 57KB | Extended type legalization |
| `sub_32C7250` | 57KB | Bitcast/conversion lowering |
| `sub_32A9030` | 55KB | Vector operation lowering |
| `sub_32C3760` | 54KB | Address space cast / pointer lowering |
| `sub_32BE8D0` | 54KB | Conditional/select lowering |
| `sub_32B6540` | 50KB | Special register / intrinsic lowering |

Common helpers shared across all functions in this cluster:

| Range | Role |
|---|---|
| `sub_325Fxxx` | EVT/MVT type utilities |
| `sub_326xxxx` | DAG node creation (`getNode` variants) |
| `sub_327xxxx` | DAG memory node creation |
| `sub_328xxxx` | Target-specific node creation |
| `sub_33Exxxx` | NVPTX-specific node builders |
| `sub_33Fxxxx` | NVPTX instruction node helpers |
| `sub_340xxxx` | NVPTX constant/register node helpers |
| `sub_341xxxx` | NVPTX chain/glue node construction |

## The .param-Space Calling Convention

PTX does not use registers for argument passing. Instead, all arguments flow through `.param` memory space, a compiler-managed address space specifically for call sites. `LowerCall` (`sub_3040BF0`, 88KB) implements this convention by emitting a structured sequence of NVPTXISD custom DAG nodes.

### Call Sequence DAG Structure

```c
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

Each call increments a monotonic sequence counter at `NVPTXTargetLowering + 537024` (offset `134256 * 4`), used to match `CallSeqBegin`/`CallSeqEnd` pairs and generate unique `.param` variable names (e.g., `__param_0`, `__param_1`, etc.).

### Scalar Widening Rules

Scalar arguments narrower than 32 bits are widened to 32 bits; values between 32 and 64 bits are widened to 64 bits. This matches the PTX ABI requirement that `.param` scalars have a minimum 32-bit size:

| Source Width | Widened To | PTX Type |
|---|---|---|
| `i1` (1 bit) | `i32` (32 bit) | `.param .b32` |
| `i8` (8 bit) | `i32` (32 bit) | `.param .b32` |
| `i16` (16 bit) | `i32` (32 bit) | `.param .b32` |
| `i32` (32 bit) | `i32` (no change) | `.param .b32` |
| `i64` (64 bit) | `i64` (no change) | `.param .b64` |
| `f16` (16 bit) | `i32` (32 bit) | `.param .b32` |
| `f32` (32 bit) | `f32` (no change) | `.param .f32` |
| `f64` (64 bit) | `f64` (no change) | `.param .f64` |

### Vector Parameter Passing

Vector arguments use `StoreV1`/`StoreV2`/`StoreV4` (opcodes 571–573) mapping to PTX `st.param.b32`, `st.param.v2.b32`, `st.param.v4.b32` and their 64-bit variants. The element count determines the opcode:

| Opcode | Name | PTX | Description |
|---|---|---|---|
| 571 | `StoreV1` | `st.param.b32` / `.b64` | Single element store |
| 572 | `StoreV2` | `st.param.v2.b32` / `.v2.b64` | 2-element vector store |
| 573 | `StoreV4` | `st.param.v4.b32` / `.v4.b64` | 4-element vector store |

For byval struct arguments, the lowering decomposes the aggregate into chunks that fit the largest available vector store. An 80-byte struct, for example, might be lowered as five `StoreV4.b32` operations (5 x 4 x 4 = 80 bytes).

### NVPTXISD DAG Node Opcodes

The complete set of NVPTXISD opcodes used in call lowering:

| Opcode | Name | Role |
|---|---|---|
| 315 | `CallSeqBegin` | Marks start of call parameter setup (maps to ISD opcode) |
| 316 | `CallSeqEnd` | Outer end-of-call marker (maps to ISD opcode) |
| 505 | `DeclareParam` | Declares a byval `.param` aggregate parameter |
| 506 | `DeclareScalarParam` | Declares a scalar `.param` parameter with width+alignment |
| 508 | `DeclareRetScalarParam` | Declares the return value `.param` parameter |
| 510 | `CallDirect` | Direct call with prototype |
| 511 | `CallDirectNoProto` | Direct call without prototype (old-style C) |
| 512 | `CallIndirect` | Indirect call (function pointer) with prototype |
| 513 | `CallIndirectNoProto` | Indirect call without prototype |
| 514 | `CallStart` | The actual call instruction |
| 515 | `LoadRetParam` | Loads return value from `.param` space |
| 517 | `CallSeqEnd` (inner) | Inner end-of-call marker |
| 518 | `CallProto` | Call prototype declaration (type signature) |
| 571–573 | `StoreV1`/`V2`/`V4` | Stores to `.param` space |

### Four Call Flavors

Call dispatch is selected by prototype availability and call directness:

| Opcode | Name | When Used |
|---|---|---|
| 510 | `CallDirect` | Direct call to a named function with a known prototype |
| 511 | `CallDirectNoProto` | Direct call without prototype (K&R C style, rare in CUDA) |
| 512 | `CallIndirect` | Function pointer call with known prototype |
| 513 | `CallIndirectNoProto` | Function pointer call without prototype |

In CUDA code, `CallDirect` (510) dominates because the vast majority of device function calls are direct with full prototypes. `CallIndirect` (512) appears when calling through `__device__` function pointers. The no-prototype variants are legacy paths that may not be exercisable from CUDA C++ but are retained for C compatibility.

### Libcall Generation

When the lowering needs to synthesize a library call (e.g., for `__divdi3` software division), it attaches `"nvptx-libcall-callee"` metadata set to `"true"` on the callee. This metadata string was extracted from the binary at `sub_3040BF0`. The metadata tells later passes that the callee is a compiler-generated runtime helper rather than user code.

The primary helpers called from `LowerCall`:

| Helper | Role |
|---|---|
| `sub_302F170` | Parameter marshaling setup |
| `sub_3031480` | Argument type coercion |
| `sub_3031850` | Scalar widening |
| `sub_30351C0` | Struct decomposition for byval args |
| `sub_303E700` | Return value handling |

## DAG Combining

The DAG combiner runs three times during the SelectionDAG pipeline: once after initial DAG construction, once after type legalization, and once after operation legalization. The combiner consists of a target-independent framework and NVPTX-specific target hooks.

### Target-Independent Combiner Framework

The combiner orchestrator (`sub_F681E0`, 65KB) manages the worklist-driven iteration over all DAG nodes:

```c
function DAGCombine(dag):
    worklist = dag.allNodes()    // linked list iteration
    visited = SmallPtrSet()
    while worklist not empty:
        node = worklist.pop()
        if visited.count(node): continue
        visited.insert(node)     // sub_C8CA60 / sub_C8CC70
        result = visitNode(node) // sub_F20C20
        if result != node:
            ReplaceAllUsesWith(node, result) // sub_F162A0
            add users of result to worklist
            mark node dead
```

The worklist operates on the SDNode linked list. Nodes are processed via `sub_C8CA60` (SmallPtrSet::count for visited check) and `sub_C8CC70` (SmallPtrSet::insert with vector growth for worklist membership). The exclusion list at `this + 64` (with count at `this + 76`) prevents certain nodes from being visited.

Global flag `byte_4F8F8E8` enables verbose/debug tracing of the combining process.

### Visitor: `sub_F20C20`

The per-node combine visitor (`sub_F20C20`, 64KB) implements six sequential optimization phases for each node:

**Phase 1: Opcode-specific combine.** Calls `sub_100E380`, the target-independent combine dispatcher, which switches on the node's opcode and applies algebraic simplifications (e.g., `x + 0 -> x`, `x & -1 -> x`, `x * 1 -> x`). For NVPTX, this also invokes the target-specific combine hook via vtable dispatch.

**Phase 2: Known-bits narrowing.** For nodes with constant operands, the combiner builds APInt masks and calls `sub_11A3F30` (`computeKnownBits` / `SimplifyDemandedBits`) to narrow constants. When all high bits of a result are known-zero, the operation can be narrowed to a smaller type. Two global `cl::opt` flags gate this phase: `qword_4F8B3C8` controls strict-FP known-bits combining, and `qword_4F8B548` controls 2-operand reassociation.

**Phase 3: Operand type-narrowing loop.** For each operand, the combiner computes the legalized type, skips zero-constant operands, creates legalized replacements, and inserts `SIGN_EXTEND`/`TRUNCATE` cast nodes as needed. This handles the common case where an operation was originally on `i64` but only uses the low 32 bits.

**Phase 4: All-constant-operand fold.** Detects when every operand is a `ConstantSDNode` (opcode 17) and calls `sub_1028510` for full constant-fold evaluation. The constant check uses a 4x-unrolled loop for performance. The operand count is extracted via the `0x7FFFFFF` mask from the packed `SDNode` header.

**Phase 5: Division-by-constant strength reduction.** Replaces division by power-of-two constants with shift+mask sequences via APInt shift/mask computation. Division by non-power-of-two constants uses the magic-number reciprocal multiplication technique: `x / C` becomes `(x * M) >> shift` where `M` is the multiplicative inverse.

**Phase 6: Vector stride / reassociation patterns.** Attempts associative FP decomposition via `sub_F15980`, with fast-math flag propagation when both sub-results are known non-negative. This handles patterns like `(a + b) + c -> a + (b + c)` when `nsz` and `arcp` flags permit.

### ReplaceAllUsesWith: `sub_F162A0`

The combiner's RAUW implementation walks the use-list and hashes each user into a worklist map using the standard DenseMap infrastructure with LLVM-layer sentinels (-4096 / -8192). See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function and growth policy.

### Supporting Combine Functions

| Function | Size | Role |
|---|---|---|
| `sub_F0F270` | 25.5KB | Pattern matcher (STORE/BITCAST/CONSTANT) |
| `sub_F24210` | 34.6KB | DAG simplification pass |
| `sub_F2B940` | 29.8KB | Truncation/extension chain combines |
| `sub_F29CA0` | 26.9KB | Node morphing / operand updating |
| `sub_F27020` | 25KB | Specific operation combines |
| `sub_F2D1B0` | 22.2KB | Comparison combines |
| `sub_F2DD30` | 11.5KB | Shift combines |
| `sub_F62E00` | 46.7KB | Address/memory operation combines |
| `sub_F657D0` | 26.1KB | Vector operation combines |
| `sub_F6C1B0` | 15.7KB | TokenFactor chain management |

### SDNode Data Structure

The combiner manipulates SDNodes using these field offsets (reconstructed from access patterns throughout the combining code):

| Offset | Size | Field |
|---|---|---|
| -8 | 8 | Operand list pointer (when bit 6 of byte +7 is set) |
| 0 | 8 | First operand / use chain linked list |
| +4 | 4 | Packed: `NumOperands` (bits 0–26) &#124; `Flags` (bits 27–31) |
| +7 | 1 | Extra flags (bit 6 = has operand pointer at -8) |
| +8 | 8 | ValueType / MVT |
| +16 | 8 | Use chain (next user pointer, 0 if none) |
| +24 | 2 | Opcode (`uint16_t`) |
| +32 | 4 | Result type info |
| +36 | 4 | DebugLoc / location ID |
| +40 | 8 | Chain operand |
| +48 | 8 | Value pointer / type info |
| +72 | 4 | NumResults |
| +80 | 4 | Additional operand count / mask index |

Operand stride is 32 bytes. Access pattern: `node - 32 * (node[+4] & 0x7FFFFFF)` yields the first operand.

### NVPTX Target-Specific Combines: `sub_33C0CA0`

`NVPTXTargetLowering::PerformDAGCombine` (`sub_33C0CA0`, 62KB) provides NVPTX-specific algebraic optimizations. This function is called from the target-independent combiner framework via vtable dispatch. It receives an SDNode and returns either NULL (no transformation) or a replacement node.

The function calls `sub_2FE8D10` (13x), `sub_2FE6CC0` (12x), `sub_30070B0` (14x), and `sub_2D56A50` (9x), with 27 calls into `sub_B2D*/B2C*` for debug value builders.

A secondary NVPTX DAG combine function at `sub_32EC4F0` (92KB) handles post-legalize optimization, operating after the main legalization pass. It calls into the same shared DAG construction helpers (`sub_2FE3480`, `sub_2FE6750`, `sub_325F5D0`, `sub_3262090`).

The NVIDIA-side DAGCombiner at `sub_3425710` (142KB) includes debug tracing with "COVERED: " and "INCLUDED: " prefix strings, confirming it was built with NVIDIA's internal debug infrastructure. This function calls `sub_C8D5F0` (31x for type action checks), `sub_2E79000` (14x for value type access), and `sub_3423E80` (8x for combine helper dispatch).

## NVPTX Address Spaces

Address space constants appear throughout the SelectionDAG lowering. See [Address Spaces](../reference/address-spaces.md) for the master table and [SelectionDAG Address Space Encoding](../reference/address-spaces.md#selectiondag-address-space-encoding) for the backend-specific secondary encoding used in `.param` passing conventions.

In `LowerCall`, pointer arguments undergo `addrspacecast` to generic (AS 0) via `sub_33F2D30`. The pointer size for AS 5 follows a power-of-two encoding: sizes 1, 2, 4, 8, 16, 32, 64, 128 bytes map to codes 2, 3, 4, 5, 6, 7, 8, 9.

Address space handling permeates the entire lowering infrastructure. Functions `sub_33067C0` (74KB), `sub_331F6A0` (62KB), `sub_331C5B0` (60KB), and `sub_33D4EF0` (114KB) all contain address-space-aware logic for NVPTX memory operations, global address lowering, argument handling, and complex pattern matching respectively.

## Intrinsic Lowering

The intrinsic lowering mega-switch (`sub_33B0210`, 60KB / 61,601 bytes, 903 basic blocks) dispatches over 200 distinct NVPTX intrinsic IDs into DAG node construction. The main 785-case switch at `0x33b0358` covers intrinsic IDs 0–0x310 (0–784) contiguously; two secondary switches inside the function (at `0x33b118f` and `0x33baba1`) re-dispatch over the sub-ranges 333–372 and 308–355 for texture/surface and address-space variants. The function contains approximately 1,000 local variables and calls `sub_338B750` (getValue helper) 195 times, `sub_3406EB0` (getNode) 116 times, and `sub_337DC20` (setValue) 100 times.

Key intrinsic categories:

| Category | ID Range | Handler | Count |
|---|---|---|---|
| Math ops (rounding modes) | 2, 10, 12, 20, 21, 63, ... | `sub_33FA050` | ~20 |
| WMMA / MMA (tensor core) | 0xA4–0xA8, 0x194–0x1EC | `sub_33A64B0` | 95 |
| Texture sampling | 0x5D–0x8D | `sub_33A4350` | 50 |
| Surface read/write | 0x8E–0x90 | `sub_33A3180` | 3 |
| Warp shuffle | 0xD4, 0xD5, 0xDF, 0xE0 | `sub_33FAF80` | 4 |
| Vote intrinsics | 0xE1–0xE6 | `sub_339CDA0` / `sub_339E310` | 6 |
| Atomics | 0xEB–0xF8 | `sub_3405C90` / `sub_340AD50` | ~14 |
| cp.async / TMA | 0x175–0x17C | `sub_33AD3D0` | ~8 |
| MMA sm90+ (Hopper wgmma) | 0x183–0x191 | `sub_33AC8F0` | 15 |
| Texture/surface handle | 10578 | inline | `nvvm_texsurf_handle` |

The WMMA/MMA block is the largest single-handler group: 95 consecutive case labels (intrinsic IDs 404–492) all delegate to `sub_33A64B0`, covering `wmma.load`, `wmma.store`, `wmma.mma`, `mma.sync` (sm70+), `mma.sp` (sm80+), and `mma.f64` (sm90+). The warp shuffle intrinsics map to specific NVPTXISD opcodes: `__shfl_down_sync` to 277, `__shfl_up_sync` to 275, `__shfl_xor_sync` to 278, and `__shfl_sync` to 276.

Math intrinsics encode explicit rounding modes via an inner opcode table. For example, `ADD_RN` (round-to-nearest) maps to opcode 252, `ADD_RZ` (round-toward-zero) to 249, `ADD_RM` (round-toward-minus-infinity) to 245, and `ADD_RP` (round-toward-plus-infinity) to 270.

NVIDIA-specific intrinsic IDs include high-value entries: ID 10578 handles `nvvm_texsurf_handle`, IDs 8920/8937–8938 handle texture/surface operations. The overflow path at `sub_33A1E80` handles intrinsic IDs that fall outside the main switch range.

## NVPTX computeKnownBits

The NVPTX target provides a custom `computeKnownBitsForTargetNode` implementation (`sub_33D4EF0`, 114KB) that propagates bit-level information through 112 opcode cases in the SelectionDAG. This function calls `sub_969240` (SDNode accessor) 399 times and itself recursively 99 times. It supports demanded-bits pruning via an APInt mask parameter and caps recursion at depth 6 (matching LLVM's default `MaxRecursionDepth`).

Notable NVPTX-specific known-bits behaviors:

- **Memory operation type inference** (opcode 0x12A): Propagates known bits through load operations based on extension mode (zero-extend, sign-extend, any-extend) encoded in the node flags byte at bits `[2:3]`. Handles `ld.global.u32` vs `ld.global.s32` vs `ld.global.b32` distinctions.
- **Texture/surface fetch results** (opcodes 0x152–0x161): Sets known bits in the range `[elementSize..width]` based on the result type, encoding the known bit-width of texture fetch results.
- **Constant pool integration** (opcode 0x175): Uses LLVM's `ConstantRange` class to derive known bits from constant pool values, chaining `fromKnownBits` through `intersect` to `toKnownBits`.
- **Target fence** at opcode 499 (`ISD::BUILTIN_OP_END`): All opcodes above 499 delegate to the `TargetLowering` virtual method; below that, the generic ISD switch handles everything.

APInt values with width at most 64 bits use inline storage; wider values trigger heap allocation. The constant 0x40 (64) appears hundreds of times as the inline/heap branch condition.

The target-independent known-bits infrastructure at `0xF50000`--`0xF60000` includes:

| Function | Size | Role |
|---|---|---|
| `sub_F5A610` | 36.7KB | `computeKnownBits` for generic ISD opcodes (depth limit at `a4 == 48`) |
| `sub_F5F040` | 52.4KB | Extended known-bits with recursive expansion limit: `(v74-1)*v77 > qword_4F8BF28` |
| `sub_F5CD10` | 26.6KB | DAG combine using known-bits results |
| `sub_F54050` | 17.8KB | Known-bits for multi-result nodes |
| `sub_F54F50` | 10.7KB | Known-bits for vector operations |

Global `qword_4F8BF28` is a threshold that limits recursive known-bits expansion to prevent combinatorial blowup.

## Inline Assembly Lowering

Inline assembly lowering spans two locations in the binary: the target-independent `SelectionDAGBuilder::visitInlineAsm` at `sub_2079C70` (83KB) and the NVPTX-specific constraint handler at `sub_338BA40` (79KB).

### Target-Independent Framework: `sub_2079C70`

The inline assembly visitor (`sub_2079C70`, 83KB, 2,797 lines) lowers LLVM IR `asm` statements into `ISD::INLINEASM` (opcode 193) or `ISD::INLINEASM_BR` (opcode 51) DAG nodes. The function allocates an 8.4KB stack frame and processes operands in five phases:

1. **Initialization.** Parses the asm string and metadata. Looks up `"srcloc"` metadata on the asm instruction for error location reporting.

2. **Constraint pre-processing.** Each constraint string is parsed into a 248-byte record. Constraints are classified as: immediate (`'i'`, flag `0x20000`), memory (`'m'`, flag `0x30000`), or register (determined by target).

3. **Tied operand resolution.** Input operands tied to output operands (e.g., `"=r"` and `"0"`) are matched and validated for type compatibility. Diagnostic: `"inline asm not supported yet: don't know how to handle tied indirect register inputs"`.

4. **Per-operand lowering.** Each operand is lowered to an SDValue. Register operands go through `TargetLowering::getRegForInlineAsmConstraint()` (virtual dispatch). Diagnostics: `"couldn't allocate output register for constraint '"`, `"couldn't allocate input reg for constraint '"`.

5. **DAG node finalization.** All operands are assembled into an `INLINEASM` SDNode with chain and flag operands.

The function uses a 16-entry inline operand buffer (7,088 bytes on stack), reflecting the assumption that CUDA inline asm rarely exceeds 16 operands. Each operand working structure is 440 bytes. Overflow triggers heap reallocation via `sub_205BBA0`.

Diagnostic strings found in the binary:

| String | Condition |
|---|---|
| `"couldn't allocate output register for constraint '"` | Register constraint unsatisfiable |
| `"couldn't allocate input reg for constraint '"` | Input constraint unsatisfiable |
| `"Don't know how to handle indirect register inputs yet..."` | Indirect tied operand |
| `"inline asm error: This value type register class is not natively supported!"` | Unsupported type for register |
| `"invalid operand for inline asm constraint '"` | Generic operand mismatch |
| `"Indirect operand for inline asm not a pointer!"` | Non-pointer indirect operand |

### NVPTX Constraint Handler: `sub_338BA40`

The NVPTX-specific inline asm constraint handler (`sub_338BA40`, 79KB) is part of the `NVPTXTargetLowering` class. It processes constraint strings specific to the NVPTX backend:

- **Simplified constraint model.** NVPTX recognizes single-character `'i'` (immediate) and `'m'` (memory) constraints through `sub_2043C80`, avoiding the complex multi-character constraint tables used by x86/ARM backends.

- **Register class mapping.** The function maps MVT values to NVPTX register classes using a 544-case switch (confirmed at `sub_204AFD0`, 60KB): MVTs 0x18–0x20 map to `Int32Regs`, 0x21–0x28 to `Int64Regs`, 0x29–0x30 to `Float32Regs`, 0x31–0x36 to `Float64Regs`, 0x37 to `Int128Regs`, 0x56–0x64 to 2-element vector registers.

- **Convergent flag handling** (bit 5): Ensures barrier semantics are preserved for inline asm, checked via operand bundle attribute or function-level `convergent`.

- **Scalar-to-vector conversion.** String `"non-trivial scalar-to-vector conversion"` indicates that the handler attempts to pack scalar inline-asm results into vector register classes when the output constraint specifies a vector type.

Additional support at `sub_2046E60` emits `", possible invalid constraint for vector type"` when a vector type is used with an incompatible constraint.

## ISel Pattern Matching Driver

The instruction selection driver (`sub_3090F90`) manages the top-level selection loop rather than performing pattern matching directly. It builds a cost table for function arguments using a hash table with hash function `key * 37`, processes the topological worklist using a min-heap priority queue, and calls the actual pattern matcher (`sub_308FEE0`) for each node.

The driver maintains an iteration budget of `4 * numInstructions * maxBlockSize` to guard against infinite loops. When the budget is exceeded, selection terminates for the current function.

For complete ISel detail, see [ISel Pattern Matching & Instruction Selection](isel-patterns.md).

## NVPTXTargetLowering Initialization

The `NVPTXTargetLowering` constructor (`sub_3056320`, 45KB + `sub_3314670`, 73KB) populates the legalization action tables that drive all subsequent SelectionDAG processing. It calls `sub_302E500`, `sub_302F030`, `sub_3030230`, and `sub_3034720` to register legal/custom/expand actions for each `{ISD_opcode, MVT}` pair.

Key aspects of the initialization:

- **Subtarget-gated feature checks.** Offsets `+2843`, `+2584`, and `+2498` in the subtarget object encode SM-version-dependent feature availability. These control which types and operations are marked Legal vs. Custom vs. Expand.

- **Vector support.** NVPTX has limited native vector support. Most vector operations are marked Custom or Expand, forcing them through the custom lowering at `sub_32E3060`.

- **Atomic support.** The string `"vector atomics not supported on this architecture!"` at `sub_3048C30` confirms SM-version-gated vector atomic support, likely SM 90+ (Hopper) or SM 100+ (Blackwell).

- **Address space assertions.** AS values (generic=0, global=1, shared=3, const=4, local=5) are encoded directly into the legalization tables, with different legal operation sets per address space.

## What Upstream LLVM Gets Wrong for GPU

Upstream LLVM's SelectionDAG framework was designed for CPU ISAs where register classes overlap and share a unified physical register file. The NVPTX target breaks these assumptions at every level:

- **Upstream assumes register classes interfere with each other.** On x86, GR32 is a sub-register of GR64; allocating `eax` constrains `rax`. The interference graph, coalescing, and copy elimination infrastructure all assume overlapping classes. NVPTX has nine completely disjoint classes (`%r`, `%f`, `%fd`, `%p`, etc.) with zero cross-class interference. The DAG's register pressure tracking, copy coalescing hints, and class constraint propagation solve a problem that does not exist on this target.
- **Upstream assumes function calls are cheap register shuffles.** CPU calling conventions move arguments through registers (`rdi`, `rsi`, etc.) or a stack backed by L1 cache. NVPTX function calls go through the `.param` address space with explicit `DeclareParam`/`st.param`/`ld.param` sequences — O(n) memory operations per argument. The `LowerCall` function in cicc is 88KB (vs. upstream's few KB) because it must handle four call flavors, monotonic `.param` naming, and `"nvptx-libcall-callee"` metadata for synthesized calls.
- **Upstream assumes a small set of intrinsics.** Upstream NVPTX intrinsic lowering covers approximately IDs 0-300. CICC's intrinsic mega-switch at `sub_33B0210` (60KB) handles 785 contiguous Intrinsic::ID values 0–0x310, covering cp.async, TMA, WGMMA, and the full SM 90/100 tensor operation set; secondary high-ID dispatch for texture/surface attribute lookup lives in helpers such as `sub_247CB70` (cases up to 15839). The upstream framework's assumption that intrinsic lowering is a small switch case is off by more than 2x even before counting the auxiliary tables.
- **Upstream assumes vector types are natively supported.** CPU targets have native vector registers (XMM/YMM/ZMM, NEON Q-registers). NVPTX has no native vector registers — most vector operations are marked Custom or Expand, forcing them through 111KB of custom lowering at `sub_32E3060`. The "legalize then select" pipeline spends most of its time decomposing vectors that never should have been formed.
- **Upstream assumes known-bits propagation is a small target hook.** Upstream NVPTX's `computeKnownBitsForTargetNode` handles fewer than 20 opcodes. CICC's version at `sub_33D4EF0` (114KB, 112 opcode cases) propagates bits through texture fetches, address space loads, and NVPTX-specific operations — a 50x expansion that upstream's hook interface was never designed to support cleanly.

## Differences from Upstream LLVM

The NVPTX SelectionDAG backend in cicc v13.0 diverges from upstream LLVM NVPTX in several structural and behavioral ways. This section catalogs the known differences.

### Structural Divergences

**Monolithic type legalizer.** Upstream LLVM splits type legalization across four source files (`LegalizeIntegerTypes.cpp`, `LegalizeFloatTypes.cpp`, `LegalizeVectorTypes.cpp`, `LegalizeTypes.cpp`). In cicc, all four are collapsed into a single 348KB function (`sub_20019C0`), likely an LTO artifact. The behavioral result is identical, but the code layout makes the function nearly impossible to patch incrementally.

**Dual-address ISel infrastructure.** The NVPTX lowering code exists at two address ranges (`0x32XXXXX` and `0x33XXXXX`), with functions at `sub_32E3060` (LowerOperation) and `sub_3377410` (secondary dispatch) forming a two-level dispatch. Upstream NVPTX uses a single `LowerOperation` method. The binary has a secondary overflow path for intrinsic IDs that fall outside the main switch range.

**142KB NVPTX DAGCombiner.** The function `sub_3425710` includes "COVERED:" and "INCLUDED:" debug trace strings not present in any upstream LLVM release. This is NVIDIA internal instrumentation for tracking combine coverage during development.

**Two inline asm subsystems.** The target-independent `visitInlineAsm` at `sub_2079C70` (83KB) and the NVPTX-specific constraint handler at `sub_338BA40` (79KB) total 162KB. The upstream NVPTX inline asm support is approximately 200 lines of code. The cicc version is vastly more complex, likely handling NVIDIA-internal PTX inline asm patterns.

### Behavioral Divergences

**Calling convention.** Upstream LLVM NVPTX uses a simplified `LowerCall` that handles only the standard `.param` space protocol. CICC's `sub_3040BF0` (88KB) adds `"nvptx-libcall-callee"` metadata for synthesized libcalls, monotonic sequence counters for unique `.param` names, and four call flavors (with/without prototype x direct/indirect). The upstream has two flavors.

**Intrinsic count.** The cicc intrinsic lowering switch (`sub_33B0210`, 60KB) handles 785 contiguous Intrinsic::ID values 0–0x310 in its main dispatch, with dedicated handlers for cp.async/TMA and WGMMA instructions. Upstream LLVM's NVPTX intrinsic lowering covers approximately IDs 0–300. The extended range covers SM 90 (Hopper) and SM 100 (Blackwell) tensor operations.

**Vector shuffle lowering.** The three-level shuffle lowering (identity detection, BitVector tracking, BUILD_VECTOR fallback) is more sophisticated than upstream NVPTX, which typically scalarizes all shuffles unconditionally.

**Atomic scope awareness.** CICC's atomic lowering at `sub_3048C30` (86KB) supports CTA/GPU/SYS scope atomics with SM-version gating. Upstream LLVM NVPTX handles basic atomics but lacks the full scope hierarchy.

**Known-bits propagation.** The NVPTX `computeKnownBitsForTargetNode` at `sub_33D4EF0` (114KB, 112 opcode cases, 399 SDNode accesses, 99 recursive calls) is far more extensive than the upstream version, which typically handles fewer than 20 target-specific opcodes. The cicc version propagates bits through texture fetches, address space loads, and NVPTX-specific operations.

**PerformDAGCombine depth.** The NVPTX-specific combine at `sub_33C0CA0` (62KB) plus the post-legalize combine at `sub_32EC4F0` (92KB) total 154KB. Upstream `NVPTXISelLowering::PerformDAGCombine` is approximately 2KB.

**Address space 101.** CICC uses address space 101 as an alternative `.param` encoding (seen in `sub_33067C0`), which does not exist in upstream LLVM NVPTX. This may be an internal convention for distinguishing kernel `.param` from device-function `.param`.

### Unchanged from Upstream

The following components appear to be stock LLVM with no NVIDIA modifications:

- SelectionDAG core infrastructure at `0xF05000`--`0xF70000` (combining, known-bits, node management)
- DAG node hashing with `((a3 >> 4) ^ (a3 >> 9)) & (capacity - 1)` at `sub_F4CEE0`
- Constrained FP intrinsic lowering at `sub_F47010` (36KB, `"round.tonearest"`, `"fpexcept.ignore"`)
- `ReplaceAllUsesWith` implementation at `sub_F162A0`
- All SDNode creation, deduplication, and lifecycle management

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `SelectionDAGLegalize::LegalizeOp` dispatcher (~100 opcodes) | `sub_1FCE100` | 91KB | — |
| `SelectionDAGLegalize` action dispatch (967 cases) | `sub_1FFB890` | 137KB | — |
| Legalization worklist management | `sub_1FF5010` |  | — |
| `ExpandNode` fallback | `sub_1FF6F70` |  | — |
| `DAGCombiner::visitNode` (6-phase per-node combine) | `sub_F20C20` | 64KB | — |
| `DAGCombiner::combine` orchestrator (worklist management) | `sub_F681E0` | 65KB | — |
| `ReplaceAllUsesWith` (hash: `((id >> 9) ^ (id >> 4))`) | `sub_F162A0` |  | — |
| Combine pattern matcher (STORE/BITCAST/CONSTANT) | `sub_F0F270` | 25.5KB | — |
| Target-independent opcode-specific combine dispatcher | `sub_100E380` |  | — |
| All-constant-operand fold evaluation | `sub_1028510` |  | — |
| Vector stride / reassociation combine | `sub_F15980` |  | — |
| Generic `computeKnownBits` | `sub_F5A610` | 36.7KB | — |
| Extended known-bits (recursive expansion limit) | `sub_F5F040` | 52.4KB | — |
| `SelectionDAG::getNode` / CSE hash table | `sub_F4CEE0` | 41.3KB | — |
| DAG node builder (operand/result setup) | `sub_F49030` | 38.2KB | — |
| Constrained FP intrinsic lowering | `sub_F47010` | 36.4KB | — |
| `NVPTXTargetLowering::LowerOperation` dispatcher | `sub_32E3060` | 111KB | — |
| LowerOperation secondary dispatch (overflow) | `sub_3377410` | 75KB | — |
| NVPTX custom type promotion | `sub_32A1EF0` | 109KB | — |
| NVPTX post-legalize DAG combine | `sub_32EC4F0` | 92KB | — |
| NVPTX vector operation splitting | `sub_32FE970` | 88KB | — |
| NVPTX load/store lowering | `sub_32D2680` | 81KB | — |
| NVPTX integer/FP legalization | `sub_32983B0` | 79KB | — |
| NVPTX intrinsic lowering (tex/surf) | `sub_32B8A20` | 71KB | — |
| NVPTX vector operation lowering | `sub_32A9030` | 55KB | — |
| NVPTX addrspacecast / pointer lowering | `sub_32C3760` | 54KB | — |
| NVPTX conditional/select lowering | `sub_32BE8D0` | 54KB | — |
| NVPTX special register lowering | `sub_32B6540` | 50KB | — |
| `NVPTXTargetLowering::PerformDAGCombine` | `sub_33C0CA0` | 62KB | — |
| NVPTX DAGCombiner with "COVERED"/"INCLUDED" tracing | `sub_3425710` | 142KB | — |
| `NVPTXTargetLowering::LowerCall` | `sub_3040BF0` | 88KB | — |
| NVPTX atomic operation lowering | `sub_3048C30` | 86KB | — |
| `NVPTXTargetLowering` constructor (action setup) | `sub_3056320` | 45KB | — |
| Type legalization table population | `sub_3314670` | 73KB | — |
| Intrinsic lowering mega-switch | `sub_33B0210` | 343KB | — |
| NVPTX `computeKnownBitsForTargetNode` | `sub_33D4EF0` | 114KB | — |
| NVPTX inline asm constraint handler | `sub_338BA40` | 79KB | — |
| `SelectionDAGBuilder::visitInlineAsm` | `sub_2079C70` | 83KB | — |
| NVPTX `nvvm_texsurf_handle` lowering | `sub_2077400` | 20KB | — |
| NVPTX argument passing / type coercion | `sub_2072590` | 38KB | — |
| `NVPTXDAGToDAGISel::Select` driver | `sub_3090F90` | 91KB | — |
| Address space / memory operation support | `sub_33067C0` | 74KB | — |
| Global address lowering | `sub_331F6A0` | 62KB | — |
| Formal arguments / return lowering | `sub_3349730` | 82KB | — |
| Call lowering (`visitCall` / `LowerCallTo`) | `sub_332FEA0` | 79KB | — |

## Reimplementation Checklist

1. **NVPTXTargetLowering with legality tables.** Populate the 2D action table at offset +2422 (259-byte row stride, indexed by `259 * VT + opcode`) with per-SM-version legal/custom/expand/promote actions for all ISD opcodes and NVPTX-specific opcodes. Include the condition-code action table at offset +18112 and the SM-gated type legality rules (f16 on SM 53+, v2f16 on SM 70+, bf16 on SM 80+).
2. **LowerOperation dispatcher (111KB equivalent).** Implement the master `LowerOperation` switch dispatching ~3,626 lines of GPU-specific lowering for loads, stores, calls, atomics, vector operations, and address space casts, including the `.param`-space calling convention with DeclareParam/StoreV1-V4/LoadRetParam sequences.
3. **Intrinsic lowering mega-switch (60KB equivalent).** Build the intrinsic lowering function covering 200+ CUDA intrinsic IDs in a contiguous 0–0x310 dispatch, organized as a jump table with per-intrinsic lowering handlers for tensor core, warp, surface/texture, and math intrinsics.
4. **PerformDAGCombine for NVPTX.** Implement the NVPTX-specific DAG combines (62KB) that run after operation legalization, including load/store vectorization (offset-based coalescing with sorting for `ld.v2`/`ld.v4`/`st.v2`/`st.v4` detection), NVPTX-specific algebraic simplifications, and the "COVERED"/"INCLUDED" tracing infrastructure.
5. **ISel::Select pattern matching (91KB equivalent).** Implement the top-down instruction selection driver that visits DAG nodes in topological order, matching against NVPTX-specific patterns via opcode-indexed tables, with special handling for tensor core instructions, inline assembly constraints, and multi-result nodes.
6. **computeKnownBits for NVPTX (114KB).** Implement the NVPTX-specific known-bits analysis covering `ctaid`, `tid`, `ntid`, address space pointer width constraints, and GPU-specific intrinsic range information to enable downstream optimization.

## Cross-References

- [Type Legalization](type-legalization.md) — detailed 348KB monolith documentation
- [ISel Pattern Matching](isel-patterns.md) — instruction selection patterns and matching
- [Register Allocation](register-allocation.md) — follows ISel in the pipeline
- [Address Spaces](../reference/address-spaces.md) — consolidated AS reference
- [Register Classes](../reference/register-classes.md) — NVPTX register class definitions
- [NVPTX Opcodes](../reference/nvptx-opcodes.md) — MachineInstr opcode reference
- [NVPTXTargetMachine](../infra/nvptx-target.md) — target machine and TTI hooks
- [Emission](../pipeline/emission.md) — PTX emission from MachineInstrs
- [Tensor Core Intrinsics](../builtins/tensor-mma.md) — WMMA/MMA intrinsic detail
- [Surface/Texture Intrinsics](../builtins/surface-texture.md) — tex/surf lowering
