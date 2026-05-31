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

```c
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

The encoding word packs a bitvector position and register-set identifier into 24 bits: `bit_offset | (((word_index_in_bv) | (4 * rbt_node_key)) << 6)`. This gives the register allocator a compact handle to identify which accumulator bank a register belongs to.

### Live Range Limit Check

`sub_ADAD60` performs two limit checks per instruction -- one after encoding source accumulators and one after encoding destination accumulators. Each compares the count of encoded entries against `maxActiveGmmaLiveRanges` stored at pass object offset +56:

```c
// After source accumulator encoding (tag 0x90000000):
src_count = pass->accumList.count;            // *(DWORD*)(a1+44) + 1
if (pass->maxActiveGmmaLiveRanges < src_count)
    emit_warning(0x1CEF, "GMMA sequence has too many active live ranges (%d), "
                 "reduce it to bring it under (%d)", src_count, maxActiveGmmaLiveRanges);

// After destination accumulator encoding (tag 0x10000000):
dst_count = pass->accumList.count - src_count;
if (pass->maxActiveGmmaLiveRanges < dst_count)
    emit_warning(0x1CEF, ...same message..., dst_count, maxActiveGmmaLiveRanges);
```

Warning `0x1CEF` (7407) fires independently for source and destination sets, enabling the compiler to identify which direction (input reuse vs. output fan-out) exceeds the hardware limit. The limit is architecture-dependent and reflects the number of accumulator register banks available to the tensor core pipeline.

### Per-Function Scan: sub_ADCA60

When the simple path is selected (mode byte at +26208 == 1 and sub-field at +26216 nonzero), `sub_ADCA60` performs a single linear scan over the function's basic blocks via the block index array at `codeObj+512`:

```c
for each BB index in codeObj->blockIndexArray[0 .. codeObj->blockCount-1]:
    bb = codeObj->bbArray[index]
    prev_wgmma_idx = -1
    for each instruction in bb->instrList:
        opcode = instr->opcode & 0xFFFFCFFF
        if opcode == 309 (wgmma.mma_async):
            if prev_wgmma_idx >= 0:
                // chain consecutive WGMMAs: check if same commit group
                if prev_instr->field_24 == instr->field_24:
                    check_commit_group_linkage(prev_instr, instr)
            if !pass->fastMode:
                encode_accumulator(instr)     // sub_ADAD60
            record(instr, &prev_wgmma_idx)
        elif opcode == 323 (commit_batch):
            record(instr, &prev_wgmma_idx)
            // extract pipeline depth from last operand flags
            depth = (operand_flags >> 2) & 0xF
            pass->maxPipelineDepth = max(pass->maxPipelineDepth, depth)
    // after scanning all instrs in this BB:
    if BB has accumulator-propagation flag (*(BYTE*)(bb+280) & 0x10):
        sub_ADBD30(pass, BB_index)   // propagate to successors
```

The per-BB FNV-1a hash table (`pass+184` through `pass+208`) stores accumulator records keyed by the basic block's unique tag at `bb+144`. Each record is a 48-byte node: `{next_ptr[8], bb_tag[4], padding[4], instr_list_ptr[8], instr_list_bound[8], ref_count[4], hash_value[4]}`.

### Cross-BB Propagation: sub_ADBD30

`sub_ADBD30` implements a worklist-driven forward propagation of accumulator liveness across basic block boundaries. The worklist is a dynamic array of 12-byte entries stored at pass object offsets +136 through +156:

| Offset | Type | Field |
|---|---|---|
| +16 | `void*[2]` | Per-program-point accumulator state array (16 bytes per slot) |
| +32 | `int` | High-water mark for state array |
| +48 | `RBTree*` | Visited-BB set (red-black tree, keyed by `bb_tag >> 8`) |
| +88 | `int` | `maxActiveGmmaLiveRanges` duplicate for cross-BB check |
| +96 | `HashTable` | Per-successor pending accumulator records (24-byte nodes) |
| +136 | `Allocator*` | Memory allocator for worklist entries |
| +144 | `void*` | Worklist buffer pointer |
| +152 | `int` | Worklist top index (stack pointer) |
| +156 | `int` | Worklist buffer capacity |
| +192 | `int` | Per-BB hash table entry count |
| +200 | `void*` | Per-BB hash table bucket array |
| +208 | `uint64` | Per-BB hash table bucket count |

The propagation algorithm:

```c
sub_ADBD30(pass, bb_index):
    push worklist entry: {bb_id=blockIndexArray[bb_index], cursor=-1, scope_bound=-1}
    while worklist is not empty:
        entry = worklist[top]
        if entry.cursor == -1:              // first visit to this BB
            bb = bbArray[entry.bb_id]
            entry.cursor = pass->stateHighWater + 1
            bb_tag = bb->field_144
            // FNV-1a lookup: hash bb_tag, probe pass->perBBHashTable
            record = hash_lookup(pass, bb_tag)
            if record found AND record has instruction list:
                // replay recorded WGMMA sequence into this BB
                for each recorded_instr in record->instrList:
                    if recorded_instr->opcode == 0x135 (wgmma.mma_async):
                        // grow state array, record instruction
                        find_scope_bound(pass, &scope_bound)  // sub_ACECD0
                        if scope_bound valid:
                            new_instr = sub_ADAD60(pass, recorded_instr, ...)
                            // update all state array refs from old to new
                    elif recorded_instr->opcode == 0x143 (commit_batch):
                        compute_scope_via_sub_AD9C20(pass, recorded_instr)
                        if cross-BB commit detected:
                            insert bb_tag into visited set
                            goto next worklist entry   // restart outer loop
            if record not found:
                insert bb_tag into per-BB hash table (new 48-byte node)
        else:
            // entry.cursor != -1: this BB was already partially processed
            pop and continue

        // after processing this BB, if scope_bound == -1:
        scope_bound = sub_ACECD0(pass)   // find nearest unprocessed commit
        clear pending successor hash table

        // propagate to all CFG successors
        for each successor edge in bb->successorList:
            succ_bb_id = successor->field_8
            succ_bb = bbArray[succ_bb_id]
            succ_tag = succ_bb->field_144
            // check visited set (RBTree at pass+48): key = succ_tag >> 8
            if succ_tag NOT in visited set:
                push worklist entry: {bb_id=succ_bb_id, cursor=-1, scope_bound=-1}
            else:
                // successor already has accumulator info
                // check if live range count exceeds limit
                pending_record = hash_lookup(pass->pendingHT, succ_tag)
                if pending_record.ref_count <= pass->maxActiveGmmaLiveRanges:
                    pending_record.ref_count++
                    enqueue {succ_bb_id | 0xFFFFFFFF00000000, scope_bound}
                    // into pass->worklist via sub_AD0A50
```

The key invariant: each BB is processed at most once per accumulator scope. The visited set (red-black tree at pass+48) prevents re-processing, while the pending hash table (pass+96) tracks how many accumulator live ranges cross into each successor, enforcing the hardware limit.

### Scope Bound Finder: sub_ACECD0

`sub_ACECD0` walks backward through the state array to find the nearest `commit_batch` (opcode 0x143) whose last operand has flags `& 3 == 0` (unprocessed commit). It returns the state array index of this commit, which becomes the scope boundary for subsequent propagation. If no unprocessed commit exists, it returns `-1`, indicating the accumulator scope extends to the function entry. If the current top-of-stack instruction is itself a `wgmma.mma_async` (opcode 0x135), the function returns immediately with the predecessor index, since the scope boundary is already known.

### Call Chain

```text
sub_AE5030  (2,967B -- SM gate, iteration over basic blocks)
  └─ sub_ADCA60  (3,643B -- per-function pipeline analysis)
       └─ sub_ADBD30  (3,364B -- per-block accumulator propagation)
       │    ├─ sub_ACECD0  (250B -- backward scope bound finder)
       │    ├─ sub_ACEC00  (230B -- dominance-aware scope validation)
       │    ├─ sub_AD0A50  (420B -- worklist push with 12-byte entry copy)
       │    ├─ sub_AD2830  -- state array growth
       │    ├─ sub_AD9C20  -- cross-BB commit scope computation
       │    └─ sub_7436F0  -- RBTree insert (visited-BB set)
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

```text
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

This 7,077-byte function is the heart of the GMMA pipeline. It takes the pass object (`a1`) and returns a packed 64-bit result: low 4 bits encode a serialization reason (0 = success, 1--10 = warning case from the table below), high 32 bits encode the function ID for diagnostic reporting. The algorithm has six sequential phases.

#### Phase 1: Initialization (decompiled lines 251--287)

Allocates two dynamic arrays, each with sentinel index `0xFFFFFFFF` (`-1`):
- **Wait list** (`v224`/`v225`, write cursor `v226`): collects `warpgroup.wait` injection points.
- **Arrive list** (`i`/`v228`, write cursor `v229`): collects `warpgroup.arrive` injection points.

Constructs two RB-tree-backed bitvector sets via `sub_661750` + `sub_768AB0`:
- `v238`: global accumulator register set (populated in phase 2, queried in phase 3).
- `v242`: per-stage register conflict scratch set (used for arrive-anchor validation).

Each tree node holds 4 x 64-bit words keyed by `reg_id >> 8` (register bank), giving 256 bits per node. Clears `field_56` (byte at `instr+56`) of every node in the code object's operand linked list at `codeObj+104`, resetting back-pointers from prior passes.

#### Phase 2: Global accumulator collection (lines 290--336)

Walks the entire function's instruction list from `codeObj+272` to the sentinel at `codeObj->field_35 + 8`. For each `wgmma.mma_async` (opcode 309 after masking `& 0xFFFFCFFF`):

1. Iterates the instruction's accumulator register operands via the `sub_ACC0A0`/`sub_AD50B0` iterator and inserts each register ID (masked `& 0xFFFFFF`) into the global set `v238`.
2. Checks four operand roles using `sub_7E3EF0(instr, role)`. The adjusted operand index is `operand_count - ((opcode >> 11) & 2) - 1` for the last accumulator slot. For each role, reads the flag byte at `instr + 84 + 8*adjusted_index + 4`:
   - **Bit 0 clear** (role 1, source A): calls `sub_ADA740` to collect accumulator regs into the tracking set.
   - **Bit 1 clear** (role 2, source B): same collection call.
   - **`sub_896530` true** (role 4, sparse metadata): collects if this is a sparse GMMA variant.
   - **Descriptor mismatch** (role 3): collects if the operand is not a predicate-true constant (`(operand ^ 0x70000000) & 0x70000000 != 0`).

#### Phase 3: Per-sequence stage walk (lines 337--1233)

Reads the sequence table at `codeObj->field_99`. If the table count is zero, skips directly to phase 5. Otherwise iterates each 4-byte entry (index into the BB array). For each sequence:

**3a. Resolve sequence extent.** Loads the start BB from `bbArray[seqTable[entry_index]]`. Follows the BB's first instruction's first register operand through `codeObj->field_37` (block index array) to find the containing region pointer `v205`. Resolves the end sentinel `v210`: if the terminal instruction is opcode 97 (`STG` in the 322-entry ROT13 SASS name table; used here as the Ori-IR control-flow boundary marker -- the actual SASS `BRA` is opcode 67), follows its target field at `instr+24`; otherwise follows the fall-through successor chain.

**3b. Per-BB instruction walk.** The inner `do`/`while` loop (line 384--1112) walks each instruction in the current BB via `instr = *(QWORD*)(instr+8)`. Per-BB state variables:

- `v36` (arrive anchor): pointer to the `warpgroup.arrive` pseudo-op for this stage, or null if none yet.
- `v38` (pending commit): last instruction before a `commit_batch` boundary; used to mark bit 2 on its last accumulator operand.
- `v37` (sequence index counter): monotonically increasing integer written to `instr->field_52` for each processed instruction.
- `v218` (cross-stage conflict flag): set to 1 when a `commit_batch` operand has bits `& 3` nonzero.
- `v211` (any-WGMMA-seen flag): set to 1 when any `wgmma.mma_async` is processed in this sequence.

Per-instruction dispatch on `opcode & 0xFFFFCFFF`:

- **271 or 32** (`warpgroup.wait` / `warpgroup.arrive`): reads the last source operand's flag byte (`& 2`). If set, the register is an in-flight accumulator -- records conflict. Otherwise probes the pass's visited-register RB-tree (`a1[84]`) by `reg_id >> 8`; if found and the bitmap bit is set, same conflict. Otherwise probes `bbArray[reg_id & 0xFFFFFF]` to check if the target BB is empty (extern call, state 1) or if `vreg+216 >= 0` and the callee is marked clobbering (`byte at ctx->field_43[vreg_idx]+57 != 0`, also state 1).
- **236** (function call): forces state 1 if `v206 == 0`. Resets `v36`, `v38`, `v207` to null.
- **309** (`wgmma.mma_async`): if `v36` is null, creates a `warpgroup.arrive` via `sub_ACBE60`, assigns `v37` as its sequence index, pushes to the arrive list. If `v38` is non-null, marks the previous commit's last accumulator operand with bit 2 (`|= 4`) and clears `v38`. Inserts the instruction into a per-accumulator-group FNV-1a hash table (48-byte nodes, key = `instr->field_16`, prime 16777619, basis 0x811C9DC5). The hash table auto-grows when `total_entries > bucket_count / 2`. Then runs **three conflict checks** against operand roles 1, 2, 4, 3 (see below). Sets `instr->field_52 = v37++`. For each register-type destination operand, sets `vreg->field_56 = instr` (back-pointer to defining WGMMA).
- **323** (`commit_batch`): if `v38` is non-null, marks its last accumulator operand with bit 2. Resets `v38 = 0`, records `v207 = instr`. If the commit's last source operand has flag bits `& 3` nonzero, sets `v218 = 1`; otherwise sets `v36 = instr` (commit becomes the new arrive anchor).
- **Other opcodes**: scans operands backward from last to find register-type-6 (accumulator class, `vreg+64 == 6`). For each, probes `v238`; on hit, checks `v218` and the target descriptor (`byte at ctx+208+72+26064 == 1` and `dword at +26072 != 0`): if both true, creates a `warpgroup.wait` via `sub_ACBF80` and pushes to the wait list. If `v36` is non-null, iterates its accumulator operands via `sub_ACC0A0` and probes the conflict set `v242` (via `sub_7554F0`); any conflict clears `v36`. Also increments per-register use counts at `a1[90]` via `sub_923B30`.

**Three conflict checks for opcode 309** (lines 898--1029). After inserting the WGMMA into the hash table:

1. **Source A/B** (roles 1 and 2, `sub_7E3EF0(instr, 1/2)`): for each register-type operand, follows `vreg->field_56` (the WGMMA back-pointer). If it exists, belongs to the same accumulator group (`def->field_24 == v36[6]`), and has a later sequence index (`def->field_52 > v36[13]`), a non-WGMMA instruction is defining a WGMMA input mid-stage: sets `v206 = 5`.
2. **Sparse metadata** (role 4, gated by `sub_896530`): same check as role 1/2, sets `v206 = 5`.
3. **Descriptor** (role 3, `sub_7E3EF0(instr, 3)`): follows the back-pointer, checks same group + later index, then verifies the defining instruction's opcode (masked `& 0xFFFFCFFF`) is NOT 309 -- a non-WGMMA defining a descriptor mid-stage sets `v206 = 7`.

#### Phase 4: Post-BB boundary handling (lines 1113--1233)

After exhausting a BB's instruction list: if `v38` is pending, marks its last accumulator operand with bit 2. If any WGMMA was seen (`v207 != 0`), updates the sequence table's per-sequence extent record via `sub_75FE80`/`sub_75FE60` (dominance-aware BB range merge using `codeObj->field_64`). Checks the successor BB's first instruction: if opcode 188 (`nop`/`wgmma.fence`) with a register-type-6 first operand whose low 2 bits encode fence flavor 1--3, injects a `warpgroup.wait`. Also injects a wait if the BB has the accumulator-propagation flag (`bb+280 bit 0`) and the successor starts with opcodes 93/94 (`sync` variants) followed by opcode 54 (`warpgroup.depbar`). Advances to the next BB by following branch/fall-through to `v210`.

#### Phase 5: Arrive/wait emission with diagnostics (lines 1242--1377)

Iterates the arrive list (`v228[0..v229]`). For each entry, checks the knob gate at `*(DWORD*)(pass+140)` (arrive knob). If nonzero, calls `sub_ACBCA0` to verify the arrive against the per-accumulator hash table at `pass+69`. If verification fails (`v237 == 0`), the arrive is unnecessary -- deletes it via `sub_9253C0(codeObj, instr, 1)`. Otherwise resolves the function name from BB info at `bb+200`, and if the instruction's BB has the compiler-generated flag (`bb+282 bit 3`), emits advisory `0x1D5F` (7519). If the flag is clear, sets `v206 = 10` (divergent-path arrive warning).

Same pattern for the wait list (`v225[0..v226]`): knob gate at `*(DWORD*)(pass+150)`, hash table at `pass+74`, advisory `0x1D5D` (7517), divergent-path sets `v206 = 9` (only if `v206` is still 0).

#### Phase 6: Finalization (lines 1379--1388)

Calls `sub_ADD8A0` (1,349 bytes) to rebuild WGMMA sequence metadata after all injections. Destroys the four temporary structures (two RB-tree sets via `sub_758B20`, two dynamic arrays via `sub_7578B0`). Returns `(v208 << 32) | (v206 & 0xF)`.

### Pipeline Stage State Machine

The fixup pass maintains a per-sequence error state in `v206`. The value is packed into the return word at line 1380: `return (function_id << 32) | (v206 & 0xF)`. A zero return means no serialization; any nonzero low nibble selects one of the 10 serialization warning codes (the value equals the warning table row). The state uses **first-error-wins** semantics: during the main instruction walk `v206` is only assigned when it was previously 0, so the first detected violation is the one reported. States 9 and 10 are exceptions -- they are set during the finalization pass and can overwrite a surviving 0.

Complete state table extracted from `sub_ADEB40`:

| `v206` | Warning | Meaning | Where set |
|--------|---------|---------|-----------|
| 0 | -- | No error; pipeline is well-formed | Initial value |
| 1 | `0x1D55` | Extern/opaque callee (BB with no instructions) or function call (opcode 236) within pipeline stage; also set when `vreg+216 >= 0` and the callee is marked clobbering (`byte at callee_bb+57 != 0`) | Lines 448--477 |
| 2 | `0x1D56` | Accumulator bitvector conflict while pipeline is already active from a preceding call boundary; conflict-bit lookup in the balanced BST (`bitmap[bank+4] >> bit & 1`) returns 1 for an arrive/wait register | Lines 420--430 |
| 5 | `0x1D59` | Non-WGMMA instruction defines WGMMA input register mid-stage; operand position 1/2/4 checked -- write in same BB (`def+24 == v36[6]`) and after the WGMMA (`def+52 > v36[13]`) | Lines 984--990 |
| 7 | `0x1D5B` | Non-WGMMA instruction defines accumulator register mid-stage; operand position 3 checked -- defining opcode (masked `& 0xFFFFCFFF`) is not 309 | Lines 1024--1028 |
| 9 | `0x1D5F` | Warpgroup.wait injection in non-suppressed divergent block; set during wait-list finalization when `sub_ACBCA0` passes but `bb+282 bit 3` is clear | Lines 1365--1368 |
| 10 | `0x1D60` | Warpgroup.arrive injection in non-suppressed divergent block; set during arrive-list finalization under same divergence check | Line 1302 |

**Transition rules during the main instruction walk:**

```c
for each instruction in pipeline-stage walk:
    opcode = instr->field_72 & 0xFFFFCFFF

    if opcode in {271 (arrive), 32 (wait)}:
        if accumulator_bitvector_conflict:
            if v206 == 0: v206 = 2, record function_id  // first-error-wins
            reset accum tracker, continue
        if target_BB empty or callee clobbering:
            if v206 == 0: v206 = 1, record function_id

    elif opcode == 236 (call):
        if v206 == 0: v206 = 1, record function_id
        reset accum tracker (v36=0, v38=0, v207=0), continue

    elif opcode == 309 (wgmma.mma_async):
        create warpgroup.arrive record if first in stage
        check input operands (pos 1,2,4) → conflict sets v206 = 5
        check accum operands (pos 3)     → conflict sets v206 = 7
        set v211 = 1 (stage-active flag)

    elif opcode == 323 (commit_batch):
        set bit 2 on last accumulator operand of pending commit
        record this instruction as pending commit (v38)

    else:
        scan operands against accumulator bitvector
```

**Back-to-back pipelines.** When `commit_batch` (323) is encountered, the pass records it in `v38`. If a subsequent `wgmma.mma_async` arrives while `v38` is non-null, bit 2 is set on the commit's last accumulator operand (`v38 + 8*idx + 88 |= 4`) and `v38` is cleared. This bit tells the injection pass not to insert a redundant `warpgroup.wait` between stages, allowing the hardware to overlap the previous commit's tensor-core work with the new stage's arrival.

**Function call mid-pipeline.** Opcode 236 immediately resets the accumulator tracker and sets `v206 = 1` (first-error-wins). The function_id is captured from the sequence descriptor (`v209[50]`). If the callee is known non-clobbering (via the `callee_bb+57` check), tracking resets without setting the error.

**Multi-sequence iteration.** The outer loop (lines 350--1240) iterates the sequence table at `context->field_99`. Each entry is an independent WGMMA sequence. `v206` persists across sequences: if the first sequence sets an error, subsequent sequences are still walked (injection points are collected) but the error code is locked.

**Divergent-path states (9, 10).** These are set exclusively during the finalization loops (lines 1242--1377), not during the main walk. When the compiler needs to inject `warpgroup.arrive` or `warpgroup.wait` but the injection point is in a divergent block and `sub_ACBCA0` does not suppress it, the pass records the error. These states can overwrite a surviving `v206 == 0`, which is the only exception to first-error-wins.

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

```text
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

### Validation Algorithm Details

#### sub_ADA7E0 -- pipeline consistency

Called with the orchestrator context (`a1`) and a packed argument `a2` whose low 32 bits are a flags word and high 32 bits a secondary limit. The algorithm iterates every WGMMA instruction in the pipeline hash table (stored at `a1[71]`, bucket count at `a1[72]`, enabled flag at `a1+140`).

For each WGMMA instruction `v20` in the table, the function resolves the instruction's accumulator peer set via `sub_AD5120`. When `v66` (flags) is zero, it takes the fast path: looks up the peer in the hash table using FNV-1a on bytes at `v20+16` (the register identity word) and copies the peer's accumulator register list into a local vector via SSE-optimized `memcpy`. It then re-calls `sub_AD5120` with the merged peer list. If `sub_AD5120` returns 0, the consistency check failed -- the function reads the basic block's function identifier from `*(ctx->field_0->bb_list[bb_array[instr+24]]+164) -> *(ctx->field_0->fn_table[fn_id]+200)` and returns error code 3 (packed with the function ID in the high 32 bits).

After each instruction, operands at offsets +80/+84 are scanned for register references with tag `(bits[31:28] & 7) == 1` and register type 6 (accumulator, from `*(reg_desc+64)`). These accumulator register IDs are inserted into a sorted set at `a1+93` via `sub_768AB0`.

#### sub_AE3D40 -- structural validation

Operates in verify mode (`a2=0`) or rebuild mode (`a2=1`). Allocates three sorted register sets (arrive-set at `v105`, wait-set at `v109`, input-set at `v113`) plus a stage metadata table via `sub_860D40`.

For each pipeline stage (iterated through the stage hash table at `a1+544`), the function walks every instruction in the stage's basic block range. Per instruction `v29`:

1. **Operand class 3** (accumulator source): if the tag bits `v29[21+2*pos] ^ 0x70000000` pass the non-zero test, registers are extracted and inserted into the input-set.
2. **Accumulator output**: the accumulator operand at `v29[20]` is resolved. If its low bit is set (`& 1`), the instruction produces an arrive-set entry; if bit 1 is set (`& 2`), a wait-set entry. Which bit is tested depends on the opcode class from `dword_229D3C0[opcode-11]`: class 0 and class 1 test arrive, class 3 tests wait.
3. **Operand class 4** (WGMMA input): registers are added to the input-set for later conflict detection.
4. **Operand classes 1 and 2**: register references are added to the arrive/wait sets respectively.

After all instructions in the stage, the function calls `sub_AD2140` to build a stage descriptor, then `sub_ACE3D0` to check structural consistency of the three sets against the basic block's pipeline metadata. `sub_ACE3D0` returns false if any set violates the pipeline protocol; the function then returns with low byte 0 (fail). In rebuild mode, it additionally calls `sub_ADB5E0` to construct fresh stage metadata and `sub_AD3410` to install it.

#### sub_AD8F90 -- secondary validation

Structurally parallel to `sub_AE3D40` but operates on the secondary pipeline table at `a1[73]` (bucket count at `a1+148`) instead of the primary at `a1+544`. Allocates two sorted sets (arrive at `v119`, wait at `v123`) and a stage metadata table via the same infrastructure.

Per stage, per instruction, the function enumerates operand 0 (accumulator output) and builds the arrive/wait sets using the same tag-based classification. In addition, for operand positions from `sub_7E3EF0(v48,1)` through `sub_7E3EF0(v48,4)`, it reads each operand, checks if its tag nibble `(bits[31:28] & 7) == 1` (virtual register), and adds matching entries to the arrive-set. This captures non-accumulator register references that participate in the secondary pipeline's arrival protocol.

In rebuild mode (`a2=1`), the function replaces the secondary pipeline table entries at `a1[73..81]` with the freshly computed versions. Verification mode calls `sub_ACE3D0` with the collected sets and returns low byte 0 on failure.

#### sub_AE17C0 -- late consistency

The largest validator (7,538 bytes). Called after metadata finalization. Performs three checks:

1. **Input register validation** (lines 383--387): calls `sub_AE0D20` twice -- once on the arrive annotation set (`a1+69`, offset +69 from context) and once on the wait annotation set (`a1+74`). `sub_AE0D20` verifies that every register in the pipeline's declared input set has a matching annotation. If either call returns false, error code 5 is returned (non-WGMMA defines input registers).

2. **Per-block instruction walk** (lines 537--660): iterates the WGMMA stage list at `*(ctx->field_792)`. For each stage entry, resolves the basic block range from `bb_start = reg_desc[instr.field_8+84] & 0xFFFFFF` through `bb_end` via the ordering array at `ctx+512`. Walks every instruction in the range, assigning sequence numbers at `instr+52`. For WGMMA opcodes (309 or 323 after masking), the instruction pointer is recorded into two arrays indexed by `instr+24` (basic block index): `v228[]` records the last WGMMA seen, `v231[]` records the first. Stage group IDs at `instr+264` matching `bb.field_144 == ctx->field_99[stage_id*4]` trigger insertion into the conflict register set via `sub_98CF00`. Commit-batch instructions (opcode `0x143` after `& 0xFFFFCFFF`) whose accumulator operand has `(flags & 3) == 2` and `(flags & 0x3C) == 0` are recorded via `sub_758060`.

3. **WGMMA accumulator conflict scan** (lines 662--912): iterates all instructions in basic-block linked-list order. For each `wgmma.mma_async` (opcode 309 masked): checks accumulator source operands via `sub_AD4CC0`/`sub_ACC800`; if the result set is non-empty (`!sub_ACC3A0`), returns error code 7 (non-WGMMA defines accumulators). Checks accumulator destination operands via `sub_AD4BE0`/`sub_ACBB60`; if non-empty, returns error code 6 (non-WGMMA reads accumulators). For `_warpgroup.commit_batch` (opcode 323): checks commit-source operands, and if a knob-gated diagnostic (`sub_ACBCA0`) indicates a conflict in the per-stage register set, walks the conflict set via `sub_ACC110` and `sub_ACC8B0` to verify every conflicting register, returning error code 6 if any accumulator conflict persists.

## Interaction with Register Allocation

The GMMA pipeline runs at phases 85/87, before register allocation (phase 101). The constraint flow between these passes is bidirectional: Phase 85 publishes accumulator live range data that the allocator consumes, and when the allocator cannot satisfy the resulting pressure, a feedback path triggers pipeline serialization.

### Forward path: Phase 85 to allocator

Phase 85 produces three artifacts consumed by the register allocator:

1. **Accumulator encoding tags.** `sub_ADAD60` encodes each WGMMA accumulator register set into a 24-bit packed word (bitvector position + register-set identifier, tagged `0x90000000` for source and `0x10000000` for destination). These tags are stored on instruction operands and later read by the interference builder (`sub_926A30`) to generate type-6 constraint nodes for VRs of register class 6 (Tensor/Acc).

2. **Sequence table at `context->field_99`.** Located at offset 792 from the compilation context base. An array of 4-byte entries (count in the first word), each indexing into the function table at `context->field_46`. The allocator's per-class iteration (`sub_9721C0`) reads this table to determine which functions contain WGMMA sequences and therefore have class-6 VRs requiring allocation.

3. **Inserted arrive/wait instructions.** Phase 87 injects `_warpgroup.arrive` (opcode 323) and `_warpgroup.wait` (opcode 271 masked) instructions that create artificial definition and use points for accumulator VRs. The standard liveness analysis at phase 101 entry treats these as ordinary def/use sites, extending the live ranges of class-6 VRs across the fence--wait interval. This is the primary mechanism by which WGMMA accumulator occupancy is communicated to the allocator -- the arrive/wait placement directly determines how many accumulator registers are simultaneously live at each program point.

### Allocator-side class-6 handling

The register allocator processes class 6 (Tensor/Acc) in the main per-class loop (`for class_id in 1..6`). Two class-6 specifics:

- **Early-out at `alloc+332`.** If `*(DWORD*)(alloc+332) == 2`, no class-6 VRs exist and allocation is skipped entirely. This guards non-WGMMA kernels from paying any tensor-register allocation cost.
- **Budget via `sub_9372B0`.** On allocation failure for class 6, the error message uses `sub_9372B0(alloc)` rather than `alloc.threshold + 1` to report the register count. This function accounts for uniform register reservations that reduce the effective class-6 budget. The pre-allocation pass (`sub_94A020`) also performs paired pre-assignment for class-6 operands: `find_dest_operand` scans for the first type-1 register with `vreg->reg_class == 6`, and `try_pair_preassign` links source and destination accumulator VRs with directional pairing constraints.

### Feedback path: "too many accumulators"

When the GMMA pipeline detects that accumulator pressure exceeds the hardware limit, two serialization codes fire:

- **`0x1D57` (7511):** "insufficient register resources for the wgmma pipeline." Triggered when the pipeline's internal register-set tracking (`sub_ADA7E0` / `sub_AE3D40` / `sub_AD8F90`) fails -- too many distinct accumulator banks are simultaneously active within the pipeline scope.
- **`0x1D58` (7512):** "insufficient register resources for the function." Triggered when total function register pressure (class-1 GPR + class-6 accumulator) exceeds the architecture limit after reserving the pipeline's accumulator banks. The live-range warning `0x1CEF` (`maxActiveGmmaLiveRanges` check at pass object +56) is the early detector for this condition.

Any serialization sets the flag `*(BYTE*)(context->field_0->field_1584 + 1920) = 1`, then calls `sub_AE47B0` to collapse the pipelined sequence into individual fence/mma/commit/wait groups per operation. After collapse, the arrive/wait instructions that Phase 87 inserted are removed, which shortens the class-6 live ranges and allows the allocator to succeed with lower register pressure -- at the cost of eliminating all tensor-core overlap.

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
