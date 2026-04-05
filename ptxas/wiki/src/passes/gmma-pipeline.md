# GMMA/WGMMA Pipeline

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

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
- `reg_type == 6` at `vreg+64` (tensor/accumulator register class)

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

Register type 6 (`vreg+64 == 6`) is the tensor/accumulator register class. The conflict check compares operand register IDs against the active accumulator bitvector using a balanced binary search tree (`v238` / `v148` in the decompilation). The tree is keyed by `register_id >> 8` (register bank) with a 64-bit bitmap per node tracking individual registers within the bank:

```c
bit_index = register_id & 0x3F;
bank_offset = (register_id >> 6) & 3;  // 0..3 for 4 64-bit words per node
is_conflict = (node->bitmap[bank_offset + 4] >> bit_index) & 1;
```

## Serialization Warnings

When the pipeline cannot be formed correctly, `sub_ACE480` (1,908 bytes) emits one of 10 distinct performance warnings. The function receives a packed 64-bit error code: the low 4 bits select the warning case (1--10) and the high 32 bits identify the function that triggered the failure. The function name is resolved via a vtable callback: `context->field_0->vtable[18]->method_1(context->field_0->vtable[18], function_id)`.

### Warning Emission Mechanism

Each warning is gated by a per-function flag at `context->field_208 + 72 + 26280`:

- **Byte == 1 with DWORD at +26288 nonzero:** Emit via `sub_895530` (direct diagnostic with source location). Falls back to `sub_7EEFA0` (format-to-buffer, no location) if the source location callback at `context->vtable + 48` is null.
- **Byte != 1 (default):** Emit via `sub_7FA2C0` (warning-once gate, keyed on hex code at `context + 154`). If the gate passes (first occurrence for this function), emits via `sub_895670` (diagnostic through `context->vtable + 128` callback). This prevents the same warning from being emitted multiple times for the same function.

All warnings use the prefix `"Potential Performance Loss: wgmma.mma_async instructions are serialized due to ..."`.

### Serialization Warning Table

| Case | Hex | Decimal | Message suffix | Source function |
|---|---|---|---|---|
| 1 | `0x1D55` | 7509 | `...the presence of Extern calls in the function '%s'` | `sub_ADEB40` |
| 2 | `0x1D56` | 7510 | `...wgmma pipeline crossing function boundary at a function call in the function '%s'` | `sub_ADEB40` |
| 3 | `0x1D57` | 7511 | `...insufficient register resources for the wgmma pipeline in the function '%s'` | `sub_ADA7E0`, orchestrator fallback |
| 4 | `0x1D58` | 7512 | `...insufficient register resources for the function '%s'` | orchestrator resource check |
| 5 | `0x1D59` | 7513 | `...non wgmma instructions defining input registers of a wgmma between start and end of the pipeline stage in the function '%s'` | `sub_ADEB40`, `sub_AE17C0` |
| 6 | `0x1D5A` | 7514 | `...non wgmma instructions reading accumulator registers of a wgmma between start and end of the pipeline stage in the function '%s'` | `sub_AE17C0` |
| 7 | `0x1D5B` | 7515 | `...non wgmma instructions defining accumulator registers of a wgmma between start and end of the pipeline stage in the function '%s'` | `sub_ADEB40`, `sub_AE17C0` |
| 8 | `0x1D5C` | 7516 | `...ill formed pipeline stage in the function '%s'` | `sub_AE3D40` structural check |
| 9 | `0x1D5E` | 7518 | `...program dependence on compiler-inserted WG.DP in divergent path in the function '%s'` | `sub_ADEB40` finalization |
| 10 | `0x1D60` | 7520 | `...program dependence on compiler-inserted WG.AR in divergent path in the function '%s'` | `sub_ADEB40` finalization |

Note: The hex codes are not contiguous. Codes `0x1D5D` (7517) and `0x1D5F` (7519) are advisory injection warnings, not serialization warnings (see below).

### Advisory Injection Warnings

During successful (non-serialized) pipeline fixup, `sub_ADEB40` emits advisory warnings when it injects warpgroup synchronization instructions. These are gated by knob check at `sub_ACBCA0` and the per-instruction flag at `bb_info + 282` bit 3:

| Hex | Decimal | Message |
|---|---|---|
| `0x1D5D` | 7517 | `"warpgroup.wait is injected in around line %d by compiler to allow use of registers defined by GMMA in function '%s'"` |
| `0x1D5F` | 7519 | `"warpgroup.arrive is injected in around line %d by compiler to allow use of registers in GMMA in function '%s'"` |

These are informational: they indicate the compiler successfully handled a register conflict by inserting synchronization, without falling back to serialization.

### Detailed Trigger Conditions

#### Case 1 (`0x1D55`): Extern calls prevent pipelining

**Trigger.** During the instruction walk in `sub_ADEB40`, a call instruction (Ori opcode 236) is encountered within a WGMMA pipeline stage, or an operand references a basic block with no instructions (opaque/extern function target). The compiler cannot verify that the callee preserves the accumulator register state.

**Detection code.** In `sub_ADEB40`: when `opcode == 236` (function call), or when a callee basic block's instruction pointer is null (`*(_QWORD*)v114 == 0`), `v206` is set to 1.

**Code pattern that causes it:**
```cuda
wgmma.fence;
extern_function_call();  // <-- triggers case 1
wgmma.mma_async ...;
wgmma.commit_group;
wgmma.wait_group;
```

**Fix.** Mark the callee as `__forceinline__` so the compiler can see its register usage. Move non-inlineable function calls outside the fence--wait region. Restructure the kernel so that no opaque calls occur between `wgmma.fence` and `wgmma.wait_group`.

#### Case 2 (`0x1D56`): Pipeline crosses function call boundary

**Trigger.** The bitvector conflict check finds a non-WGMMA instruction's register operand colliding with the active accumulator bitvector, at a point where the pipeline already has active state from a preceding call-boundary violation. Specifically, the register is looked up in the balanced binary tree (`node->bitmap[bank_offset + 4] >> bit_index`) and if the conflict bit is set while `v206` was already zero, it is promoted to case 2.

**Detection code.** In `sub_ADEB40` lines 418--426: after the accumulator bitvector lookup returns a match, `v206` is set to 2 (the first conflict after a call boundary was detected).

**Code pattern that causes it:**
```cuda
// Function A:
wgmma.fence;
wgmma.mma_async ...;
call function_B();  // pipeline spans across this call
wgmma.commit_group; // in function_B or after return
wgmma.wait_group;
```

**Fix.** Keep the entire fence--mma--commit--wait sequence within a single function. Do not split WGMMA pipeline stages across function boundaries.

#### Case 3 (`0x1D57`): Insufficient register resources for pipeline

**Trigger.** Three distinct paths produce this code:

1. `sub_ADA7E0` returns 3 when its internal call to `sub_AD5120()` fails (line 233). This function attempts to propagate accumulator tracking through the FNV-1a hash table, and failure means the pipeline's register sets cannot be simultaneously tracked.
2. `sub_AE3D40` (structural validation) returns with low byte 0, meaning `sub_ACE3D0()` rejected the pipeline structure. The orchestrator uses case 3 as the generic fallback (`v20 = 3` at line 66 of `sub_AE4F70`).
3. `sub_AD8F90` (secondary validation) returns with low byte 0 similarly.

**Code pattern that causes it:**
```cuda
// Too many concurrent accumulators
wgmma.fence;
wgmma.mma_async D0, ...;  // accum set 0
wgmma.mma_async D1, ...;  // accum set 1
wgmma.mma_async D2, ...;  // accum set 2
// ... many more with distinct accumulators
wgmma.commit_group;
wgmma.wait_group;
```

**Fix.** Reduce the number of concurrent WGMMA operations with distinct accumulator register sets. Split large tile computations into smaller stages with intervening waits. Reduce accumulator tile dimensions.

#### Case 4 (`0x1D58`): Insufficient register resources for function

**Trigger.** The function's overall register pressure (including non-WGMMA code) is too high. The WGMMA pipeline requires dedicated accumulator register banks, and if the function's total register demand exceeds what is available after reserving the pipeline's needs, serialization is triggered.

**Code pattern that causes it:**
```cuda
__global__ void kernel(...) {
    float local_array[256];     // high register pressure
    complex_computation(local_array);
    wgmma.fence;
    wgmma.mma_async ...;       // needs accumulator regs too
    wgmma.commit_group;
    wgmma.wait_group;
}
```

**Fix.** Reduce register usage in the kernel: use shared memory for large arrays, reduce live variable counts, split the kernel into smaller functions. Compile with `-maxrregcount` to force spilling of non-critical values.

#### Case 5 (`0x1D59`): Non-WGMMA defines input registers

**Trigger.** Two paths:

1. In `sub_ADEB40` (lines 960--990): for each non-WGMMA instruction within a pipeline stage, operand position 4 (WGMMA input operands) is checked. If a non-WGMMA instruction writes to a register that a WGMMA uses as matrix A or B input, and the write is in the same basic block (`v84+24 == v36[6]`) and after the WGMMA (`v84+52 > v36[13]`), the conflict is flagged.
2. In `sub_AE17C0` (lines 384--386): `sub_AE0D20()` validates the pipeline's input register sets against arrive/wait annotations. Failure at either the arrive set (offset +69) or wait set (offset +74) returns code 5.

**Code pattern that causes it:**
```cuda
wgmma.fence;
// desc_a = make_descriptor(smem_ptr);
wgmma.mma_async D, desc_a, desc_b;
desc_a = make_descriptor(smem_ptr + offset);  // <-- redefines input
wgmma.mma_async D, desc_a, desc_b;            // uses redefined input
wgmma.commit_group;
wgmma.wait_group;
```

**Fix.** Compute all WGMMA input values (descriptors, pointers) before `wgmma.fence`. Use separate register variables for distinct input values within a single pipeline stage. If different tiles need different descriptors, pre-compute them all before entering the pipeline.

#### Case 6 (`0x1D5A`): Non-WGMMA reads accumulators

**Trigger.** Detected only by `sub_AE17C0` (late consistency check), at two points:

1. Lines 707--741: for each WGMMA instruction, operand 0 (accumulator) is examined via `sub_AD4BE0`/`sub_ACBB60`. If the accumulator data set is non-empty (`!sub_ACC3A0`), a non-WGMMA instruction reads from an in-flight accumulator register.
2. Lines 870--885: same check in a per-basic-block iteration context.

**Code pattern that causes it:**
```cuda
wgmma.fence;
wgmma.mma_async D, A, B;
float val = D[0];              // <-- reads accumulator before wait
wgmma.commit_group;
wgmma.wait_group;
```

**Fix.** Move all reads of accumulator registers after `wgmma.wait_group`. The accumulator values are undefined until the wait completes. If the compiler cannot automatically insert a `warpgroup.wait` at the read point (e.g., divergent control flow), serialization occurs.

#### Case 7 (`0x1D5B`): Non-WGMMA defines accumulators

**Trigger.** Three paths:

1. In `sub_ADEB40` (lines 994--1028): for each non-WGMMA instruction, operand position 3 is checked. If the operand is a register (not immediate, tag != `0x70000000`), and it belongs to the same basic block and pipeline stage, and the defining instruction's opcode (after masking) is not 309 (wgmma.mma_async), the conflict is flagged.
2. In `sub_AE17C0` (lines 684--703): `sub_AD4CC0` checks WGMMA accumulator operands against the conflict set. If a match is found and the set is non-empty, code 7 is returned.
3. In `sub_AE17C0` (lines 1296--1302): a catch-all at the end of the late validation walk.

**Code pattern that causes it:**
```cuda
wgmma.fence;
D[0] = 0.0f;                   // <-- writes to accumulator
wgmma.mma_async D, A, B;       // D is accumulator
wgmma.commit_group;
wgmma.wait_group;
```

**Fix.** Initialize accumulators before `wgmma.fence`, or use the WGMMA `.useC` mode to let the hardware handle accumulator initialization. Never write to accumulator registers from non-WGMMA instructions inside a pipeline stage.

#### Case 8 (`0x1D5C`): Ill-formed pipeline stage

**Trigger.** `sub_AE3D40` (structural validation) detects that the fence/mma/commit/wait structure is malformed. The function walks the WGMMA sequence and checks structural properties via `sub_ACE3D0`. When the structure check fails (line 447), an error with low byte 0 is returned. The orchestrator maps structural failures to code 3 as fallback, but code 8 is emitted when `sub_ADEB40` detects the stage state machine in an inconsistent state.

**Code pattern that causes it:**
```cuda
wgmma.fence;
if (condition) {
    wgmma.mma_async D, A, B;
    wgmma.commit_group;        // commit only on one path
}
wgmma.wait_group;              // wait on all paths -- mismatch
```

**Fix.** Ensure each `wgmma.fence` is matched by exactly one `wgmma.commit_group` and one `wgmma.wait_group` on every control flow path. Keep pipeline stages in straight-line code. Do not use `goto`, early `return`, or conditional branches between fence and wait.

#### Case 9 (`0x1D5E`): WG.DP in divergent path

**Trigger.** During the finalization pass in `sub_ADEB40` (lines 1308--1370), the compiler iterates over `warpgroup.wait` injection points. For each injection, it checks the basic block's convergence flag at `bb_info + 282` bit 3. If bit 3 is NOT set (block is divergent) and `v206` was previously zero, `v206` is set to 9 with the function ID from the basic block at offset +200.

WG.DP = `WARPGROUP.DEPBAR` (dependency barrier), the SASS-level instruction that implements `warpgroup.wait`.

**Code pattern that causes it:**
```cuda
wgmma.fence;
wgmma.mma_async D, A, B;
wgmma.commit_group;
if (threadIdx.x < 64) {        // warp-divergent condition
    use(D[0]);                  // compiler needs WG.DP here, but path is divergent
}
wgmma.wait_group;
```

**Fix.** Ensure WGMMA pipeline stages execute in uniform (non-divergent) control flow. Move conditional logic outside the fence--wait region. Use predication instead of branching for minor variations within a stage.

#### Case 10 (`0x1D60`): WG.AR in divergent path

**Trigger.** During the finalization pass in `sub_ADEB40` (lines 1242--1306), the compiler iterates over `warpgroup.arrive` injection points. When the compiler needs to inject a `warpgroup.arrive` (to start a new pipeline stage after a conflict) but the injection point is in a divergent basic block, `v206` is set to 10. This occurs at line 1302 when a knob-gated diagnostic check at `sub_ACBCA0` indicates the injection is not suppressed but the block divergence prevents safe insertion.

WG.AR = `WARPGROUP.ARRIVE` (arrival barrier), the SASS-level instruction that synchronizes warpgroup warps before entering a pipeline stage.

**Code pattern that causes it:**
```cuda
if (threadIdx.x < 64) {        // divergent
    wgmma.fence;               // <-- compiler needs WG.AR, but divergent
    wgmma.mma_async D, A, B;
    wgmma.commit_group;
    wgmma.wait_group;
}
```

**Fix.** Same as case 9. Keep pipeline stage entry points (fences) and exit points (waits) in uniform control flow. All warps in the warpgroup must execute the same WGMMA pipeline structure.

### Orchestrator Error Code Flow

The orchestrator `sub_AE4F70` calls validation functions in sequence. Each returns a packed 64-bit value with the error code in the low bits and a function identifier in the high 32 bits:

```
sub_AE4F70
  │
  ├─ sub_ADEB40 (primary fixup)
  │    returns: 1, 2, 5, 7, 9, 10 in low 4 bits
  │    (0 = success)
  │
  ├─ sub_ADA7E0 (pipeline consistency)
  │    returns: 3 if FNV-1a accumulator tracking fails
  │    (0 = success)
  │
  ├─ sub_AE3D40 (structural validation)
  │    returns: low byte 1 = pass, low byte 0 = fail
  │    (orchestrator maps fail to case 3)
  │
  ├─ sub_AD8F90 (secondary validation)
  │    returns: low byte 1 = pass, low byte 0 = fail
  │    (orchestrator maps fail to case 3)
  │
  ├─ sub_AE4710 (finalize metadata) -- only on success
  │
  └─ sub_AE17C0 (late consistency)
       returns: 5, 6, 7 in low bits
       (0 = success)
```

Any nonzero result triggers the serialization path: `*(BYTE*)(context->field_0->field_1584 + 1920) = 1`, followed by `sub_ACE480` (warning emission) and `sub_AE47B0` (pipeline collapse).

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
| Accum reg\_type | 6 | `vreg+64` value for tensor/accumulator regs |
| Accum src tag | `0x90000000` | High nibble tag for source accumulator encoding |
| Accum dst tag | `0x10000000` | High nibble tag for destination accumulator encoding |
| FNV-1a prime | 16777619 | Hash function prime for register set lookup |
| FNV-1a offset | `0x811C9DC5` | Hash function offset basis |
| Live range warning | `0x1CEF` | Warning code for excessive live ranges |
| Serialization base | `0x1D55` | First serialization warning code (extern calls) |
| Serialization end | `0x1D60` | Last serialization warning code (WG.AR divergent) |
| Advisory wait inject | `0x1D5D` | Advisory: warpgroup.wait injected |
| Advisory arrive inject | `0x1D5F` | Advisory: warpgroup.arrive injected |

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
- [Register Model](../ir/registers.md) -- reg\_type 6 (tensor/accumulator, allocator class 6)
- [Register Allocator](../regalloc/overview.md) -- live range pressure from WGMMA accumulators
- [Mercury Encoder](../codegen/mercury.md) -- SASS encoding of WGMMA instructions
- [Uniform Register Optimization](uniform-regs.md) -- phase 86 between the two GMMA phases
- [Loop Passes](loop-passes.md) -- phase 88 LICM after GMMA fixup
- [Late Legalization](late-legalization.md) -- phase 93 catches ops exposed by GMMA passes
- [SM Architecture Map](../targets/index.md) -- SM 90+ architecture support
- [Knobs System](../config/knobs.md) -- diagnostic gating for injection warnings
