# GMMA/WGMMA Pipeline

The GMMA pipeline handles warpgroup matrix multiply-accumulate (WGMMA) instructions introduced with SM 90 (Hopper). Two dedicated compiler phases -- `OriPropagateGmma` (phase 85) and `FixupGmmaSequence` (phase 87) -- transform the IR to satisfy the hardware's strict pipelining requirements for asynchronous tensor-core operations. These are the only passes in ptxas whose sole purpose is WGMMA instruction handling.

WGMMA operates at warpgroup granularity (4 warps executing in lockstep). The hardware requires a specific sequencing protocol: `wgmma.fence` to open a pipeline stage, a sequence of `wgmma.mma_async` operations that share accumulator registers, `wgmma.commit_group` to close the stage, and `wgmma.wait_group` to synchronize on completion. Between the fence and wait, strict constraints govern which registers can be touched by non-WGMMA instructions. Violating these constraints forces the compiler to serialize the WGMMA pipeline, destroying throughput.

| | |
|---|---|
| **Pipeline phases** | 85 (`OriPropagateGmma`), 87 (`FixupGmmaSequence`) |
| **Target architectures** | SM 90+ (Hopper, Blackwell) |
| **Phase 85 entry** | `sub_AE5030` (2,967 bytes) -- outer driver, SM gate check |
| **Phase 85 core** | `sub_ADAD60` (2,170 bytes) -- accumulator propagation per instruction |
| **Phase 87 entry** | `sub_AE4F70` (182 bytes) -- sequencing orchestrator |
| **Phase 87 core** | `sub_ADEB40` (7,077 bytes) -- sequence fixup, warpgroup inject |
| **Serialization warnings** | `sub_ACE480` (1,908 bytes) -- 10 distinct warning codes |
| **Pipeline validation** | `sub_AE3D40` (2,511 bytes) -- sequence structural check |
| **Accumulator collect** | `sub_ADA740` (146 bytes) -- gathers accumulator register set |
| **Live range propagation** | `sub_ADBD30` (3,364 bytes) -- per-basic-block propagation |
| **Phase name strings** | `0x22BCB13` (`OriPropagateGmma`), `0x22BCB40` (`FixupGmmaSequence`) |

## Hardware Background

### Warpgroup Execution Model

A warpgroup consists of 4 consecutive warps (128 threads). WGMMA instructions execute cooperatively across all 4 warps, with each warp contributing a slice of the matrix operation. The hardware tensor core pipeline is decoupled from the main pipeline: `wgmma.mma_async` dispatches work to the tensor core and returns immediately, while the accumulator registers remain in-flight until a `wgmma.wait_group` completes.

The PTX-level instructions that constitute a WGMMA pipeline stage:

| PTX Instruction | Ori Opcode | Role |
|---|---|---|
| `wgmma.fence` | (via handler `sub_4DA380`) | Opens a pipeline stage; prevents reordering across the fence |
| `wgmma.mma_async` | 309 | Dispatches an asynchronous matrix multiply-accumulate |
| `wgmma.commit_group` | (via handler `sub_4DA4B0`) | Closes the current pipeline stage |
| `wgmma.wait_group` | (via handler `sub_4DA5E0`) | Waits for N committed groups to complete |
| `_warpgroup.arrive` | 323 | Compiler-inserted warpgroup synchronization (arrive) |
| `_warpgroup.wait` | 271 (masked `& 0xFFFFCFFF`) | Compiler-inserted warpgroup synchronization (wait) |
| `_warpgroup.commit_batch` | | Compiler-inserted commit batch |

The `_warpgroup.*` instructions (prefixed with underscore) are compiler-internal pseudo-operations inserted by ptxas, not directly written by the programmer. They map to SASS `WARPGROUP.ARRIVE`, `WARPGROUP.WAIT`, and `WARPGROUP.DEPBAR` instructions.

### Accumulator Register Constraints

WGMMA accumulator registers are the output (D) operands of `wgmma.mma_async`. While a pipeline stage is open (between fence and wait), strict rules apply:

1. **No non-WGMMA definitions of accumulator registers.** Another instruction cannot write to a register that a WGMMA in the current stage uses as an accumulator.
2. **No non-WGMMA reads of accumulator registers.** Another instruction cannot read from an accumulator register between the producing WGMMA and the completing wait.
3. **No non-WGMMA definitions of WGMMA input registers.** The A and B matrix input registers (including descriptor registers) must not be redefined by non-WGMMA instructions within the stage.

Violation of any constraint forces serialization -- the compiler collapses the pipeline to issue one WGMMA at a time with individual fence/commit/wait per operation.

### Sparse GMMA

The binary contains support for sparse GMMA variants (structured sparsity). The string `"Sparse GMMA with "` at `0x1D0B430` appears in `sub_494210` (2,276 bytes), which handles sparse matrix metadata validation. Sparse WGMMA uses an additional metadata operand encoding the 2:4 or other sparsity pattern.

## Phase 85: OriPropagateGmma

### Purpose

Phase 85 propagates WGMMA accumulator register liveness information through the IR. For each `wgmma.mma_async` instruction (Ori opcode 309), it identifies the accumulator register set and builds a compact encoding that downstream passes use to track which registers are "in-flight" at each program point. This information is consumed by phase 87 to determine where `warpgroup.arrive` and `warpgroup.wait` instructions must be injected.

### SM Gate

The outer driver `sub_AE5030` checks the target architecture before proceeding. At offset +1381 of the compilation context, a flag indicates whether the target supports WGMMA. The check at the function entry:

```
if (*(char*)(context + 1381) >= 0)  // bit 7 clear = no WGMMA support
    return;
```

An additional mode check reads from the target descriptor at offset 26208 (within a 72-byte sub-structure at the descriptor's offset 72):
- Value 0: no WGMMA support -- skip entirely
- Value 1 with sub-field at 26216 nonzero: use the simple single-function path (`sub_ADCA60`)
- Otherwise: use the full pipeline analysis path

### Accumulator Register Encoding

The core function `sub_ADAD60` processes each `wgmma.mma_async` instruction and encodes its accumulator register set into a packed 32-bit word. The encoding uses the FNV-1a hash (prime 16777619, offset basis 0x811C9DC5) for register-set lookup in a hash table:

```c
hash = 16777619 * (HIBYTE(reg_id) ^
       (16777619 * (BYTE2(reg_id) ^
       (16777619 * (BYTE1(reg_id) ^
       (16777619 * ((uint8_t)reg_id ^ 0x811C9DC5)))))));
```

Accumulator entries are stored with a type tag in the high nibble:
- `0x90000000 | (encoded_accum & 0xFFFFFF)` -- source accumulator register set
- `0x10000000 | (encoded_accum & 0xFFFFFF)` -- destination accumulator register set

### Live Range Limit Check

After accumulator propagation, the pass checks whether the number of active GMMA live ranges exceeds the hardware limit. The limit is stored at offset 56 of the pass object (field `*(DWORD*)(a1 + 56)` = `maxActiveGmmaLiveRanges`). If exceeded, a diagnostic is emitted:

```
"GMMA sequence has too many active live ranges (%d), reduce it to bring it under (%d)"
```

This diagnostic uses warning code `0x1CEF` (7407). The limit is architecture-dependent and reflects the number of accumulator register banks available to the tensor core pipeline.

### Call Chain

```
sub_AE5030  (2,967B -- SM gate, iteration over basic blocks)
  └─ sub_ADCA60  (3,643B -- per-function pipeline analysis)
       └─ sub_ADBD30  (3,364B -- per-block accumulator propagation)
            └─ sub_ADAD60  (2,170B -- per-instruction accumulator encoding)
                 ├─ sub_AD4500  -- hash table lookup for register set
                 ├─ sub_AD4940  -- hash table insert/update
                 ├─ sub_AD6280  -- register set cache insert
                 ├─ sub_AD8E50  -- instruction iterator setup
                 ├─ sub_AD0C50  -- begin accumulator iteration
                 ├─ sub_AD3EA0  -- advance accumulator iterator
                 ├─ sub_AD1FA0  -- advance to next accumulator slot
                 ├─ sub_75A670  -- grow dynamic array (accumulator list)
                 └─ sub_895530  -- emit diagnostic warning
```

### Accumulator Collection Helper

`sub_ADA740` (146 bytes) collects the set of registers that are accumulators for a given instruction. It iterates over an instruction's operands, checking:
- Operand type tag `(operand >> 28) & 7 == 1` (register operand)
- Not an immediate-flagged operand (`(byte_flag & 1) == 0`)
- Register class `== 6` (tensor/accumulator register class)

Matching registers are added to a bitvector-like set via `sub_768AB0`.

## Phase 87: FixupGmmaSequence

### Purpose

Phase 87 is the critical legalization pass. It analyzes WGMMA instruction sequences, verifies that the hardware pipeline constraints are satisfied, and inserts `warpgroup.arrive` / `warpgroup.wait` instructions where registers used by non-WGMMA instructions conflict with in-flight WGMMA accumulators. If the pipeline cannot be formed correctly, it triggers serialization and emits performance warnings.

### Orchestrator: sub_AE4F70

The 182-byte wrapper orchestrates the complete fixup sequence:

```
sub_AE4F70 (FixupGmmaSequence orchestrator)
  │
  ├─ [1] sub_ADEB40  -- primary sequence fixup (inject arrive/wait)
  ├─ [2] sub_ADA7E0  -- verify pipeline consistency
  ├─ [3] sub_AE3D40  -- structural validation of sequences
  ├─ [4] sub_AD8F90  -- secondary validation pass
  ├─ [5] sub_AE4710  -- finalize sequence metadata
  ├─ [6] sub_AE17C0  -- late pipeline consistency check
  │
  └─ On failure at any step:
       ├─ Set serialization flag: *(BYTE*)(context + 1920) = 1
       ├─ sub_ACE480  -- emit serialization warning
       └─ sub_AE47B0  -- serialize the WGMMA pipeline (fallback)
```

The return value encodes the failure reason in the low 32 bits and a function identifier in the high 32 bits, which `sub_ACE480` uses to select the appropriate warning message.

### Primary Fixup: sub_ADEB40

This 7,077-byte function is the heart of the GMMA pipeline. Its logic:

**1. Initialization.** Allocates two dynamic arrays (`v224`/`v225` for warpgroup.wait insertion points, `i`/`v228` for warpgroup.arrive insertion points) and initializes them with sentinel values (`0xFFFFFFFF`).

**2. First pass -- identify WGMMA sequences.** Iterates over all instructions in the function's code list. For each instruction with opcode 309 (`wgmma.mma_async`):

- Collects the instruction's accumulator register set via `sub_ACC0A0` / `sub_AD50B0` iterator pattern
- Checks whether each of the instruction's operands (positions 1--4) has already been marked with arrival/wait flags
- For unmarked operands, calls `sub_ADA740` to collect accumulator registers and add them to the tracking set

The pass checks operand flag bits at `instruction + 84 + 8*operand_index + 4`:
- Bit 0 (`& 1`): operand has been processed for arrive
- Bit 1 (`& 2`): operand has been processed for wait
- Bit 2 (`& 4`): operand requires a warpgroup.arrive/wait boundary

**3. Second pass -- walk pipeline stages.** For each WGMMA sequence identified in the compilation context's sequence table (`context->field_99`), the pass walks forward through basic blocks:

- Tracks the current pipeline stage state (`v206`: 0=initial, 1=arrived, 2=committed)
- When encountering a `wgmma.mma_async` (opcode 309), records it as part of the current stage
- When encountering a `_warpgroup.commit_batch` (opcode 323), marks the stage boundary and sets bit 2 on the last accumulator operand
- When encountering an `arrive` (opcode 271 masked) or `wait` (opcode 32 masked), updates the pipeline state
- When encountering a function call (opcode 236), forces a pipeline break

For non-WGMMA instructions within a stage, checks whether their register operands conflict with the active accumulator set by querying the bitvector (the balanced binary tree at `v238`). If a conflict is found, the instruction needs a `warpgroup.arrive` or `warpgroup.wait` to be injected before it.

**4. Injection.** Creates new instructions:
- `sub_ACBE60` creates `warpgroup.arrive` pseudo-instructions
- `sub_ACBF80` creates `warpgroup.wait` pseudo-instructions

These are added to the arrival/wait lists and later inserted into the code.

**5. Commit pass.** After analysis, iterates over the collected injection points:
- For each `warpgroup.arrive` insertion, checks whether the injection needs a diagnostic via `sub_ACBCA0` (knob-gated)
- Emits advisory warning `0x1D5F` (7519): `"warpgroup.arrive is injected in around line %d by compiler to allow use of registers in GMMA in function '%s'"`
- For each `warpgroup.wait` insertion, emits advisory warning `0x1D5D` (7517): `"warpgroup.wait is injected in around line %d by compiler to allow use of registers defined by GMMA in function '%s'"`

**6. Finalization.** Calls `sub_ADD8A0` (1,349 bytes) to rebuild the WGMMA sequence metadata after injection.

### Pipeline Stage State Machine

The fixup pass maintains a state machine as it walks through instructions within a WGMMA sequence:

```
          ┌──────────────┐
          │  state = 0   │  (initial / outside pipeline)
          │  no active   │
          │  stage       │
          └──────┬───────┘
                 │  encounter wgmma.mma_async
                 ▼
          ┌──────────────┐
          │  state = 1   │  (in pipeline stage, arrived)
          │  tracking    │
          │  accumulators│
          └──────┬───────┘
                 │  encounter commit_batch
                 ▼
          ┌──────────────┐
          │  state = 2   │  (committed, waiting)
          │  accumulators│
          │  in-flight   │
          └──────┬───────┘
                 │  encounter wait or stage end
                 ▼
          ┌──────────────┐
          │  state = 0   │  (back to initial)
          └──────────────┘

  At any state, encountering a function call (opcode 236)
  or a conflicting register use forces:
    → inject warpgroup.arrive/wait
    → potentially serialize the pipeline
```

### Register Conflict Detection

Register class 6 is the tensor/accumulator register class. The conflict check compares operand register IDs against the active accumulator bitvector using a balanced binary search tree (`v238` / `v148` in the decompilation). The tree is keyed by `register_id >> 8` (register bank) with a 64-bit bitmap per node tracking individual registers within the bank:

```c
bit_index = register_id & 0x3F;
bank_offset = (register_id >> 6) & 3;  // 0..3 for 4 64-bit words per node
is_conflict = (node->bitmap[bank_offset + 4] >> bit_index) & 1;
```

## Serialization Warnings

When the pipeline cannot be formed correctly, `sub_ACE480` emits one of 10 distinct performance warnings. Each has a unique diagnostic code and is gated by a per-function warning flag at `context->field_208 + 72 + 26280`:

| Code | Hex | Condition |
|---|---|---|
| 1 | `0x1D55` | Extern (external function) calls in the function prevent pipelining |
| 2 | `0x1D56` | WGMMA pipeline crosses a function call boundary |
| 3 | `0x1D57` | Insufficient register resources for the WGMMA pipeline |
| 4 | `0x1D58` | Insufficient register resources for the function overall |
| 5 | `0x1D59` | Non-WGMMA instructions define input registers within a pipeline stage |
| 6 | `0x1D5A` | Non-WGMMA instructions read accumulator registers within a pipeline stage |
| 7 | `0x1D5B` | Non-WGMMA instructions define accumulator registers within a pipeline stage |
| 8 | `0x1D5C` | Ill-formed pipeline stage structure |
| 9 | `0x1D5D` | Program dependence on compiler-inserted `WG.DP` in divergent path |
| 10 | `0x1D5E` | Program dependence on compiler-inserted `WG.AR` in divergent path |

The warnings are in the "Potential Performance Loss" category. Each warning message includes the function name via a callback through `context->field_0->vtable[18]->method_1(context->field_0->vtable[18], function_id)`.

The serialization fallback function `sub_AE47B0` replaces the pipelined WGMMA sequence with individual fence/mma/commit/wait groups per operation, which is functionally correct but eliminates all overlap between tensor core operations.

## Interaction with Register Allocation

The GMMA pipeline runs at phases 85/87, before register allocation (phase 101). This is by design -- the pass operates on virtual registers and needs to:

1. Track accumulator live ranges before physical register assignment constrains placement
2. Insert warpgroup.arrive/wait with freedom to position them optimally
3. Propagate accumulator liveness to inform the register allocator about the extended live ranges that WGMMA creates

The live range limit check (warning code `0x1CEF`) directly impacts register allocation: if too many WGMMA accumulators are simultaneously live, the register allocator will not have enough physical registers, and the pipeline must be serialized.

Phase 86 (`InsertPseudoUseDefForConvUR`) runs between the two GMMA phases. It inserts pseudo use/def instructions for uniform register conversion, which must account for the accumulator regions identified by phase 85.

Phase 88 (`OriHoistInvariantsLate3`) runs immediately after phase 87, exploiting the now-explicit pipeline boundaries as LICM barriers.

## PTX Instruction Handlers

The PTX-to-Ori lowering registers four WGMMA-related handlers in `sub_5D4190`:

| PTX Mnemonic | Handler | Size |
|---|---|---|
| `wgmma.mma_async` | `sub_50AC70` | 1,282 bytes |
| `wgmma.fence` | `sub_4DA380` | 295 bytes |
| `wgmma.commit_group` | `sub_4DA4B0` | 295 bytes |
| `wgmma.wait_group` | `sub_4DA5E0` | 311 bytes |

The `wgmma.mma_async` handler is the largest, handling the complex operand encoding (matrix dimensions, data types, layout, scale factors, descriptor format). The fence/commit/wait handlers are thin wrappers producing single Ori instructions.

The internal warpgroup synchronization instructions (`_warpgroup.arrive`, `_warpgroup.wait`, `_warpgroup.commit_batch`) are registered separately as `_mma.warpgroup`-prefixed handlers at `0x466000`--`0x467900` (approximately 36 small ~96-byte handler functions covering the various warpgroup synchronization variants).

## SASS Output

The Ori WGMMA instructions are encoded to the following SASS opcodes by the Mercury encoder:

| Ori Instruction | SASS Opcode | Description |
|---|---|---|
| `wgmma.mma_async` | `WGMMA.MMA_ASYNC` | Asynchronous warpgroup matrix multiply |
| `wgmma.fence` | `WGMMA.FENCE` | Pipeline fence |
| `wgmma.commit_group` | `WGMMA.COMMIT_GROUP` | Commit current group |
| `wgmma.wait_group N` | `WGMMA.WAIT_GROUP N` | Wait for N groups |
| `_warpgroup.arrive` | `WARPSYNC` / `BAR.ARRIVE` | Warpgroup arrival barrier |
| `_warpgroup.wait` | `WARPSYNC` / `BAR.WAIT` | Warpgroup wait barrier |
| `_warpgroup.commit_batch` | `DEPBAR` variant | Warpgroup dependency barrier |

The Mercury encoder at `sub_62E890` (118 KB) handles the SASS-level encoding of warpgroup operations, referenced by strings `"warpgroup-arrive"`, `"warpgroup-wait"`, and `"warpgroup-commit_batch"` used as internal Mercury instruction tags.

## Key Constants

| Constant | Value | Meaning |
|---|---|---|
| WGMMA opcode | 309 | Ori opcode for `wgmma.mma_async` |
| Arrive opcode (masked) | 271 | `opcode & 0xFFFFCFFF` for `_warpgroup.arrive/wait` |
| Commit opcode | 323 | Ori opcode for `_warpgroup.commit_batch` |
| Call opcode | 236 | Forces pipeline break |
| Accum register class | 6 | Register class ID for tensor/accumulator regs |
| Accum src tag | `0x90000000` | High nibble tag for source accumulator encoding |
| Accum dst tag | `0x10000000` | High nibble tag for destination accumulator encoding |
| FNV-1a prime | 16777619 | Hash function prime for register set lookup |
| FNV-1a offset | `0x811C9DC5` | Hash function offset basis |
| Live range warning | `0x1CEF` | Warning code for excessive live ranges |
| Serialization base | `0x1D55` | Base warning code for serialization reasons |

## Key Function Table

| Address | Size | Name / Role |
|---|---|---|
| `0xAE5030` | 2,967 | Phase 85 outer driver (SM gate, BB iteration) |
| `0xADCA60` | 3,643 | Phase 85 per-function pipeline analysis |
| `0xADBD30` | 3,364 | Phase 85 per-block accumulator propagation |
| `0xADAD60` | 2,170 | Phase 85 per-instruction accumulator encoding |
| `0xADA740` | 146 | Accumulator register collector |
| `0xAE4F70` | 182 | Phase 87 orchestrator |
| `0xADEB40` | 7,077 | Phase 87 primary sequence fixup |
| `0xADB5E0` | 1,867 | Phase 87 sequence metadata builder |
| `0xADD8A0` | 1,349 | Phase 87 post-injection metadata rebuild |
| `0xAE3D40` | 2,511 | Sequence structural validation |
| `0xAD8F90` | 2,924 | Secondary validation pass |
| `0xAE17C0` | 7,538 | Late pipeline consistency check |
| `0xAE47B0` | 1,975 | Serialization fallback (collapse pipeline) |
| `0xACE480` | 1,908 | Serialization warning emitter (10 codes) |
| `0xACBE60` | 279 | Create warpgroup.arrive instruction |
| `0xACBF80` | 279 | Create warpgroup.wait instruction |
| `0xACBCA0` | 191 | Knob-gated injection diagnostic check |
| `0x50AC70` | 1,282 | PTX handler: `wgmma.mma_async` |
| `0x4DA380` | 295 | PTX handler: `wgmma.fence` |
| `0x4DA4B0` | 295 | PTX handler: `wgmma.commit_group` |
| `0x4DA5E0` | 311 | PTX handler: `wgmma.wait_group` |
| `0x494210` | 2,276 | Sparse GMMA validation |
| `0x62E890` | 118,150 | Mercury encoder for warpgroup SASS ops |

## Cross-References

- [Pass Inventory](index.md) -- phases 85, 87 in the 159-phase table
- [Synchronization & Barriers](sync-barriers.md) -- warpgroup barriers, `DEPBAR` generation
- [Register Model](../ir/registers.md) -- register class 6 (tensor/accumulator)
- [Register Allocator](../regalloc/overview.md) -- live range pressure from WGMMA accumulators
- [Mercury Encoder](../codegen/mercury.md) -- SASS encoding of WGMMA instructions
- [Uniform Register Optimization](uniform-regs.md) -- phase 86 between the two GMMA phases
- [Loop Passes](loop-passes.md) -- phase 88 LICM after GMMA fixup
- [Late Legalization](late-legalization.md) -- phase 93 catches ops exposed by GMMA passes
- [SM Architecture Map](../targets/index.md) -- SM 90+ architecture support
- [Knobs System](../config/knobs.md) -- diagnostic gating for injection warnings
