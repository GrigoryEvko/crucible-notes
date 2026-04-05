# The Ori Internal Representation

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Ori -- short for "Original IR" -- is ptxas's sole intermediate representation. It is a fully proprietary, SASS-level IR with virtual registers, its own CFG infrastructure, and a partial-SSA discipline. Ori has no relationship to LLVM IR: there is no LLVM Value hierarchy, no LLVM-style use-def chains, no SSA dominance-frontier construction. Every IR-level optimization pass in ptxas (prefixed `Ori` in the NamedPhases table: `OriCopyProp`, `OriSanitize`, `OriBranchOpt`, `OriLoopSimplification`, `OriStrengthReduce`, `OriDoPredication`, etc.) operates on this representation.

The key design decision that distinguishes Ori from PTX: **Ori uses SASS opcode names, not PTX mnemonics.** After the MercConverter pass (`sub_9F1A90`, 35KB) runs, every instruction carries the name of the hardware SASS instruction it will become -- `IMAD`, `FFMA`, `LDG`, `STG`, `BAR`, `BRA`, `EXIT`, etc. -- just with virtual (not physical) register operands. This means the optimizer already knows exactly which hardware functional unit each instruction will execute on, enabling accurate latency modeling and scheduling from the earliest optimization phases.

## Key Facts

| Property | Value |
|---|---|
| Name | Ori ("Original IR") |
| Heritage | Fully proprietary (not LLVM-based) |
| Level | SASS machine-level with virtual registers |
| SSA form | Partial -- constructed by phase 23, destroyed by phase 73 |
| Code Object size | ~1136 bytes per function (C++ object) |
| Code Object vtable | `0x21EE238` |
| Register files | 4: R (GPR), UR (uniform), P (predicate), UP (uniform predicate) |
| Operand kinds | 10 distinct types |
| CFG representation | FNV-1a hash maps for successor/backedge edges |
| Opcode encoding | ROT13 of real SASS mnemonic names |
| BB entry size | 40 bytes per basic block, contiguous array |
| Instruction linkage | Doubly-linked list within each basic block |

## Architecture Overview

```
  PTX source
      |
      v
  [Flex/Bison parser]          -- see pipeline/ptx-parser.md
      |
      v
  [PTX-to-Ori lowering]        -- see pipeline/ptx-to-ori.md
      |
      v
  +-------------------------------------------+
  |            Ori IR                          |
  |                                            |
  |  Code Object (per-function container)      |
  |    +-- Basic Block array (40B entries)     |
  |    |     +-- Instruction linked list       |
  |    |           +-- Packed operand array     |
  |    +-- CFG (FNV-1a hash map edges)         |
  |    +-- RPO array                           |
  |    +-- Register file arrays                |
  |    +-- Backedge map                        |
  +-------------------------------------------+
      |
      | 159 optimization phases (phases 0-158)
      |   phase 23: GenerateMovPhi (enter partial SSA)
      |   phase 73: ConvertAllMovPhiToMov (exit partial SSA)
      |
      v
  [Instruction selection]      -- see codegen/isel.md
      |
      v
  [Register allocation]        -- see regalloc/overview.md
      |
      v
  [Instruction scheduling]     -- see scheduling/overview.md
      |
      v
  [SASS binary encoding]       -- see codegen/encoding.md
```

## The Code Object

Every function under compilation is represented by a single Code Object -- a ~1136-byte C++ structure that owns all IR data for that function. The Code Object vtable is at `0x21EE238`. Its constructor is at `sub_A3B080`.

### Field Map

| Offset | Type | Field | Description |
|--------|------|-------|-------------|
| +24 | `u32` | `sm_version` | SM target (encoded: 12288=sm30, 20481=sm50, 36865=sm90) |
| +72 | `ptr` | `code_buf` | Output code object buffer |
| +88 | `ptr` | `reg_file` | Register descriptor array. `*(ctx+88)+8*regId` -> descriptor |
| +152 | `ptr` | `sym_table` | Symbol/constant lookup array |
| +272 | `ptr` | `instr_head` | Instruction linked-list head |
| +296 | `ptr` | `bb_array` | Basic block array pointer (40B per entry) |
| +304 | `u32` | `bb_index` | Basic block array count/current index |
| +312 | `ptr` | `options` | `OptionsManager*` for knob queries |
| +648 | `ptr` | `succ_map` | CFG successor edge hash table |
| +680 | `ptr` | `backedge_map` | CFG backedge hash table |
| +720 | `ptr` | `rpo_array` | Reverse post-order array (`int*`) |
| +768 | `ptr` | `const_sections` | Constant memory section array |
| +776 | `ptr` | `smem_sections` | Shared memory section array |
| +976 | `ptr` | `block_info` | Block info array (40 bytes per entry, contiguous) |
| +984 | `i32` | `num_blocks` | Number of basic blocks |
| +1584 | `ptr` | `sm_backend` | SM-specific architecture backend object (see [data-structures.md](./data-structures.md#sm-backend-object-at-1584)) |
| +1664 | `ptr` | `knob_container` | Knob container pointer (for `-knob` queries) |
| +1928 | `ptr` | `codegen_ctx` | Code object / code generation context |

### Register and Instruction Counts (SM Backend Object)

The register counts and instruction counts live in the **SM backend object** at `*(code_obj+1584)`, accessed via DWORD-indexed fields (not Code Object byte offsets). Earlier versions of this page incorrectly listed these as Code Object offsets +99, +102, +159, +335, +341 -- those are DWORD indices, making the actual byte offsets 396, 408, 636, 1340, and 1364 respectively within the SM backend.

| DWORD Index | Byte Offset | Type | Field | Description |
|-------------|-------------|------|-------|-------------|
| `[99]` | +396 | `u32` | `ur_count` | Uniform register (UR) count |
| `[102]` | +408 | `u32` | `r_alloc` | R-register count (allocated) |
| `[159]` | +636 | `u32` | `r_reserved` | R-register count (reserved) |
| `[335]` | +1340 | `u32` | `instr_hi` | Instruction count (upper bound) |
| `[341]` | +1364 | `u32` | `instr_lo` | Instruction count (lower bound) |

Register count formula (from `sub_A4B8F0`, where `v5 = *(_DWORD **)(ctx + 1584)`):

```
total_R_regs      = v5[159] + v5[102]   // reserved + allocated
instruction_count = v5[335] - v5[341]   // upper - lower
```

The stats emitter at `sub_A3A7E0` prints a detailed per-function profile:

```
# 142 instructions, 24 R-regs
# [inst=142] [texInst=0] [tepid=0] [rregs=24]
# [est latency = 87] [LSpillB=0]
# [Occupancy = 0.750000]
# [issue thru=0.888889] [fp thru=0.000000]
# [worstcaseLat=87.000000]
# [avgcaseLat=52.500000]
```

## Basic Blocks

Basic blocks are stored as 40-byte entries in a contiguous array at Code Object +976. The block count is at +984.

### Block Entry Layout (40 bytes)

| Offset | Type | Field |
|--------|------|-------|
| +0 | `ptr` | Head instruction pointer (first instruction in BB) |
| +8 | `ptr` | Instruction list link / tail |
| +28 | `i32` | `bix` -- block index (unique ID for CFG operations) |
| +32 | `u64` | Flags / padding |

Blocks are additionally accessible via a sub-block array at Code Object +368, indexed as `*(ctx+368) + 8*blockIndex`.

The debug dumper (`sub_BE21D0`) emits Graphviz DOT output for the CFG:

```
digraph f {
  node [fontname="Courier" ...]
  bix0 -> bix1
  bix0 -> bix3
  bix1 -> bix2
  bix2 -> bix1    // backedge (loop)
  bix2 -> bix3
}
```

## Control Flow Graph

The CFG uses FNV-1a hash maps to represent edges. Two separate hash tables exist at Code Object offsets +648 (successor edges) and +680 (backedge info).

### FNV-1a Hashing

All CFG hash lookups use the same parameters, confirmed across 50+ call sites:

| Parameter | Value |
|-----------|-------|
| Initial hash | `0x811C9DC5` |
| Prime | `16777619` (`0x01000193`) |
| Input | 4-byte block index, hashed byte-by-byte |

### Hash Map Structure

Each hash map uses chained hashing with 24-byte bucket entries:

```
Bucket (24 bytes):
  +0   node* head      // first node in chain
  +8   node* tail      // last node in chain
  +16  i32   count     // entries in this bucket

Full Node (64 bytes):
  +0   node* next      // chain link
  +8   i32   key       // block index
  +16  ptr   values    // successor/predecessor block list
  +32  sub-hash data   // embedded sub-table for multi-edge blocks
  +56  u32   hash      // cached FNV-1a hash

Simple Node (16 bytes):
  +0   node* next
  +8   i32   key
  +12  u32   hash
```

Growth policy: rehash when `total_elements > num_unique_keys` (load factor exceeds 1.0). Capacity doubles on each rehash.

### Key CFG Functions

| Address | Size | Function | Notes |
|---------|------|----------|-------|
| `sub_BDE150` | 9KB | `CFG::computeRPO` | Explicit DFS stack, assigns RPO numbers into +720 array |
| `sub_BDE8B0` | 2KB | `CFG::printEdges` | FNV-1a lookup, prints `"bix%d -> bix%d\n"` |
| `sub_BDEA50` | 4KB | `CFG::dumpRPOAndBackedges` | RPO + backedge debug dump |
| `sub_BE0690` | 54KB | `CFG::buildAndAnalyze` | Main CFG constructor: predecessors, successors, RPO, loop detection |
| `sub_BE21D0` | 1.4KB | `CFG::dumpDOT` | Graphviz DOT format output |
| `sub_BE2330` | 4KB | `CFG::computeDominators` | Post-build dominator/loop analysis with bitvector ops |

The RPO dump (`sub_BDEA50`) produces output like:

```
Showing RPO state for each basic block:
  bix0 -> RPONum: 0
  bix1 -> RPONum: 1
  bix2 -> RPONum: 3
  bix3 -> RPONum: 2
RPO traversal order: bix0, bix1, bix3, bix2
Showing backedge info:
  bix2 -> backedge's successor BB: 1
```

## Instructions

Instructions are C++ objects with a large vtable, linked into per-basic-block doubly-linked lists. Each instruction carries a unique integer ID, an opcode, and a packed operand array.

### Instruction Layout

| Offset | Type | Field | Description |
|--------|------|-------|-------------|
| +8 | varies | `reg_class` | Register class / encoding fields |
| +16 | `i32` | `id` | Unique instruction ID |
| +28 | `u32` | `opcode` | SASS opcode (lower 12 bits = base, bits 11-12 = modifier) |
| +36 | `u32` | `flags` | Flags (bits 19-21 = subtype) |
| +48 | `u8` | `special_flags` | Volatile/special (bit 5 = volatile) |
| +72 | `u32` | `opcode_info` | Opcode info (duplicate/extended field, confirmed 50+ sites) |
| +73 | `u8` | `instr_flags` | Per-instruction flag byte |
| +80 | `u32` | `operand_count` | Number of operands |
| +84 | `u32[]` | `operands` | Packed operand array (8 bytes per operand) |
| +160 | `ptr` | `enc_buf` | Encoding buffer pointer (post-selection) |
| +184 | `u32` | `enc_mode` | Encoding mode |
| +200 | `u64` | `imm_value` | Immediate value |

### Packed Operand Encoding

Each operand occupies 8 bytes in the operand array starting at instruction offset +84:

```
 31  30  29  28  27       24  23  22  21  20  19                  0
+---+---+---+---+-----------+---+---+---+---+---------------------+
|     type      |  modifier bits (8 bits)    |  index (20 bits)    |
+---+---+---+---+-----------+---+---+---+---+---------------------+
                 ^                            ^
                 bit 24: extended flag         bits 0-19: reg/sym index

type field (bits 28-30):
  1 = register operand      -> index into *(ctx+88) register file
  5 = symbol/const operand  -> index into *(ctx+152) symbol table
```

### Operand Word 1 (Upper 4 Bytes)

Each 8-byte operand slot has two DWORDs. Word 0 (documented above) carries type/modifier/index. Word 1 carries extended flags:

```
Word 1 (at instr + 84 + 8*i + 4):

 31  30  29  28  27  26  25  24  23                             0
+---+---+---+---+---+---+---+---+-------------------------------+
|     reserved / mod flags      |CB |      auxiliary data        |
+---+---+---+---+---+---+---+---+-------------------------------+
                             ^
                             bit 24: const-bank flag (CB)

Bits 25-31 (mask 0xFE000000): extended modifier flags
  When any bit is set, the operand has special semantics.
  Peephole matchers bail out early if (word1 & 0xFE000000) != 0.
  Bit 25 (0x2000000): operand reuse / negation extension
  Bit 26 (0x4000000): absolute-value modifier (|x|)

Bit 24 (mask 0x1000000): const-bank flag
  When set, indicates the source references a constant bank (c[N][offset]).
  The scheduler uses this to distinguish FADD (standard) from FADD (const-bank)
  for latency modeling (see scheduling/latency-model.md).

Bits 0-23: auxiliary data
  For symbol/const operands (type 5): constant bank number
  For predicate guards (type 6): predicate sense (true/false)
  For register operands (type 1): typically zero
```

Evidence: `sub_40848E` checks `(word1 & 0xFE000000) != 0` across all operands; `sub_405769` tests both `0x1000000` and `0x6000000` combinations; `sub_404AD0` verifies `(word1 & 0xFE000000) == 0` before allowing peephole transforms. Confirmed in 30+ decompiled functions (confidence 0.92).

### Extraction Pattern

Extraction pattern (appears in 50+ functions):

```c
uint32_t operand = *(uint32_t*)(instr + 84 + 8 * i);
int type    = (operand >> 28) & 7;
int index   = operand & 0xFFFFF;
int mods    = (operand >> 20) & 0xFF;

uint32_t word1 = *(uint32_t*)(instr + 84 + 8 * i + 4);
bool has_const_bank = (word1 & 0x1000000) != 0;
bool has_ext_mods   = (word1 & 0xFE000000) != 0;
```

### Opcode Constants

Selected confirmed opcodes (from multiple independent functions):

| Value | Instruction | Notes |
|-------|-------------|-------|
| 47 | NOP / barrier | |
| 72 | CALL / JMP | Function call or jump |
| 91 | ATOM | Atomic memory operation |
| 92 | RED | Reduction operation |
| 95 | STS | Store to shared memory (ROT13: `FGF`). Note: EXIT = opcode 77 (`RKVG`), RET = opcode 72 (`ERG`) |
| 155 | LD variant | Load instruction |
| 173 | ST variant | Store instruction |
| 183 | LD.E | Extended load (`& 0xFFFFCFFF` mask removes modifier bits) |
| 267 | ST variant | Store (`& 0xFFFFCFFF`) |
| 268 | LD variant | Load (`& 0xFFFFCFFF`) |
| 288 | ST.E | Extended store |

The `0xFFFFCFFF` mask (clear bits 12-13) strips modifier/suboperation bits from the opcode, yielding the base instruction class. This pattern appears in `InstructionClassifier`, `MBarrierDetector`, and `OperandLowering` code.

### ROT13 Opcode Names

All SASS opcode mnemonic strings stored in the binary are ROT13-encoded. The master table is initialized in `sub_BE7390` (`InstructionInfo` constructor) at offset 4184 of the InstructionInfo object, with 16-byte `{name, length}` entries. This is lightweight obfuscation -- not a security measure.

Selected decoded names (~200+ total, covering the full sm_70+ SASS ISA):

| ROT13 | Real | Category |
|-------|------|----------|
| `VZNQ` | `IMAD` | Integer multiply-add |
| `VNQQ3` | `IADD3` | 3-input integer add |
| `SSZN` | `FFMA` | FP fused multiply-add |
| `SNQQ` | `FADD` | FP add |
| `SZHY` | `FMUL` | FP multiply |
| `ZBI` | `MOV` | Move |
| `FRY` | `SEL` | Select |
| `YBC3` | `LOP3` | 3-input logic |
| `VFRGC` | `ISETP` | Integer set-predicate |
| `SFRGC` | `FSETP` | FP set-predicate |
| `YRN` | `LEA` | Load effective address |
| `FUS` | `SHF` | Shift / funnel shift |
| `ZHSH` | `MUFU` | Multi-function unit (SFU) |
| `YQT` | `LDG` | Load global |
| `FGT` | `STG` | Store global |
| `YQP` | `LDC` | Load constant |
| `YQY` | `LDL` | Load local |
| `YQF` | `LDS` | Load shared |
| `NGBZ` | `ATOM` | Atomic |
| `ONE` | `BAR` | Barrier |
| `OEN` | `BRA` | Branch |
| `PNYY` | `CALL` | Call |
| `ERG` | `RET` | Return |
| `RKVG` | `EXIT` | Exit |
| `GRK` | `TEX` | Texture |
| `ZRZONE` | `MEMBAR` | Memory barrier |
| `JNECFLAP` | `WARPSYNC` | Warp synchronize |
| `C2E` | `P2R` | Predicate to register |
| `E2C` | `R2P` | Register to predicate |
| `ABC` | `NOP` | No-op |
| `OFFL` | `BSSY` | Branch sync stack push |
| `OFLAP` | `BSYNC` | Branch sync |
| `QRCONE` | `DEPBAR` | Dependency barrier |

## Register Files

Ori maintains four distinct register files, mirroring the SASS hardware register model.

### Register File Summary

| File | Width | Range | Special | ABI type | Code Object offset |
|------|-------|-------|---------|----------|-------------------|
| R | 32-bit | R0 -- R255 | RZ (read-zero) | 2 | +102 (alloc), +159 (reserved) |
| UR | 32-bit | UR0 -- UR63 | URZ (read-zero) | 3 | +99 |
| P | 1-bit | P0 -- P6 | PT (always-true) | 5 | (tracked separately) |
| UP | 1-bit | UP0 -- UP6 | UPT (always-true) | -- | (tracked separately) |

**R registers** are the main 32-bit general-purpose registers. 64-bit values occupy consecutive pairs (e.g., R4:R5). The total R-register count for a function is `field[159] + field[102]` (reserved + allocated). Maximum is 255 usable registers (R0-R254); R255 is the hardware zero register RZ.

**UR registers** (sm_75+) are uniform registers shared across the warp. Every thread sees the same value. UR0-UR63 on supported architectures. The count is at Code Object +99.

**P registers** are 1-bit predicate registers used for conditional execution. P0-P6 are usable; PT is the hardwired always-true predicate (writes are discarded).

**UP registers** are the uniform variant of predicates, shared across the warp like UR.

### Register Descriptor

Each register is described by a descriptor in the register file array, accessed as `*(ctx+88) + 8*regId`:

| Offset | Type | Field |
|--------|------|-------|
| +8 | `u32` | Size / live range info |
| +12 | `u32` | Register number |
| +16 | `u32` | Register class (enum) |
| +20 | `u32` | Physical register name (assigned after regalloc) |
| +24 | `ptr` | Definition info (0 = undefined / uninitialized) |
| +36 | `u32` | Flags (bits 19-21 = subtype) |
| +48 | `u8` | Volatile/special flags (bit 5 = volatile marker) |
| +64 | `u32` | Register file type enum |
| +68 | `u32` | Physical register number (post-allocation) |

Register file type values at descriptor +64:

| Value | Meaning |
|-------|---------|
| 2 | General-purpose (R) |
| 3 | Uniform (UR) |
| 5 | Predicate (P) |
| 6 | General register (alternate classification) |
| 7 | Predicate (alternate classification) |
| 10 | Extended register pair (64-bit) |
| 11 | Extended register quad (128-bit) |

The register class name table at `off_21D2400` maps `reg_type` enum values to string names. The stat collector (`sub_A60B60`, 24KB) enumerates ~25 register sub-classes including R, P, B, UR, UP, UB, Tensor/Acc, SRZ, PT, RZ, and others. The allocator processes classes 0--6 (matching reg\_type values 0--6); barrier registers (reg\_type 9) are handled separately.

## Partial SSA

Ori does not maintain full SSA form at all times. Instead, it uses a bounded "partial SSA" window managed by two phases in the 159-phase optimization pipeline.

### Phase 23: GenerateMovPhi

Constructs phi-like `MovPhi` pseudo-instructions at CFG merge points. Inserted after loop unrolling (phase 22) and before pipelining (phase 24). This establishes partial SSA form -- not through LLVM-style dominance-frontier phi insertion, but through explicit `MovPhi` nodes that represent value merging at control-flow join points.

### Phase 73: ConvertAllMovPhiToMov

Destructs SSA form by lowering every `MovPhi` into a plain `MOV` instruction. Runs after sync instruction expansion (phase 72) and before uniform register conversion (phase 74). This is SSA destruction without the need for interference-graph-based coalescing -- the `MovPhi` nodes simply become copies.

### The SSA Window

The partial-SSA window spans phases 23 through 73, covering the bulk of the optimization pipeline:

```
Phase 23  GenerateMovPhi         <-- SSA construction
Phase 24  OriPipelining
Phase 25  StageAndFence
Phase 26  OriRemoveRedundantBarriers
Phase 29  GeneralOptimize
Phase 37  GeneralOptimizeMid
Phase 46  GeneralOptimizeMid2
Phase 49  GvnCse
Phase 50  OriReassociateAndCommon
Phase 54  OriDoRematEarly
Phase 58  GeneralOptimizeLate
Phase 63  OriDoPredication
Phase 65  GeneralOptimizeLate2
Phase 69  OriDoRemat
Phase 70  OriPropagateVaryingSecond
Phase 71  OptimizeSyncInstructions
Phase 72  LateExpandSyncInstructions
Phase 73  ConvertAllMovPhiToMov  <-- SSA destruction
```

All optimizations between these two phases can rely on the single-definition property of `MovPhi` nodes for reaching-definition analysis.

### MovPhi Instruction Format

A `MovPhi` is not a distinct opcode -- it reuses the MOV opcode (19) with a distinguishing flag in the instruction's auxiliary fields. Phase 73 (`ConvertAllMovPhiToMov`) converts MovPhi to plain MOV by clearing this flag, without changing the opcode value.

```
MovPhi operand layout:
  +72  opcode         = 19 (MOV)
  +76  opcode_aux     = flag distinguishing MovPhi from plain MOV
  +80  operand_count  = 2*N + 1  (variable, one destination + N source-predecessor pairs)

  operand[0]:           destination register (the merged value)
  operand[1], [2]:      {source_reg, predecessor_bix} for predecessor 0
  operand[3], [4]:      {source_reg, predecessor_bix} for predecessor 1
  ...
  operand[2*N-1], [2*N]: {source_reg, predecessor_bix} for predecessor N-1
```

This is the operational equivalent of an SSA phi node. For a CFG merge with two predecessors:

```
;; PTX-level CFG:            ;; Ori MovPhi:
;;   bix1 defines R7         ;;
;;   bix2 defines R9         ;;   MovPhi R3, R7, bix1, R9, bix2
;;   bix3 merges             ;;
;;   uses R3                 ;;   "if from bix1, R3 = R7; if from bix2, R3 = R9"
```

Phase 23 (`GenerateMovPhi`) inserts these at merge points where a register has different reaching definitions from different predecessors. Phase 73 destructor linearizes them: it inserts a `MOV R3, R7` at the end of bix1 and a `MOV R3, R9` at the end of bix2, then deletes the MovPhi.

## Operand Kinds

The IR supports 10 distinct operand kinds, identified through the register allocator verifier (`sub_A55D80`) and the instruction selection pattern matcher infrastructure.

| # | Kind | Description |
|---|------|-------------|
| 1 | R/UR register | General-purpose or uniform register operand |
| 2 | P/UP register | Predicate or uniform-predicate register operand |
| 3 | Any register | Wildcard -- matches any register class |
| 4 | Offset | Memory offset for address computation |
| 5 | Regular | Standard immediate or constant value |
| 6 | Predicated | Guard predicate controlling conditional execution |
| 7 | Remat | Rematerialization marker (value can be recomputed instead of spilled) |
| 8 | Spill-refill | Spill/refill pair marker for register allocator |
| 9 | R2P / P2R | Register-to-predicate or predicate-to-register conversion pair |
| 10 | Bit-spill | Single-bit spill (predicate register spill to GPR) |

The regalloc verifier (`sub_A55D80`, confidence 0.95) classifies 10 problem categories that map to these operand kinds:

1. Missing spill match for refill
2. Refill reads uninitialized memory
3. P2R-R2P pattern match failure
4. Bit-spill-refill pattern match failure
5. Previously defined operand now uninitialized
6. Extra post-regalloc definitions (mixed-size check)
7. Rematerialization problem
8. P2R-R2P base destroyed
9. Bit-spill-refill base destroyed
10. Definitions disappeared without new ones added

The pattern matcher infrastructure at `0xB7D000`--`0xBA9D00` (~390 functions) uses a separate classification for instruction selection:

| Function | Predicate |
|----------|-----------|
| `sub_B28E10` | `isRegOperand` |
| `sub_B28E20` | `isPredOperand` |
| `sub_B28E40` | `isImmOperand` |
| `sub_B28E80` | `isConstOperand` |
| `sub_B28E90` | `isUReg` |
| `sub_B28E00` | `getRegClass` (1023 = wildcard, 1 = GPR) |

## Ori vs. PTX

PTX is a virtual ISA -- a stable interface between the compiler frontend and the architecture-specific backend. Ori is the architecture-specific backend representation that replaces PTX opcodes with actual SASS instructions early in compilation.

| Aspect | PTX | Ori |
|--------|-----|-----|
| Opcode set | Virtual mnemonics (`add`, `mul`, `ld`, `st`) | SASS hardware opcodes (`IMAD`, `FFMA`, `LDG`, `STG`) |
| Register model | Unlimited virtual registers, typed | 4 hardware register files (R, UR, P, UP) with virtual numbering |
| SSA form | Not applicable (PTX is a linear ISA) | Partial SSA between phases 23 and 73 |
| CFG representation | Implicit (labels + branches) | Explicit hash-map-based CFG with RPO, backedges, dominators |
| Target dependence | Architecture-independent (forward-compatible) | Architecture-specific (per-SM instruction selection) |
| Conversion point | Input to ptxas | After MercConverter (`sub_9F1A90`) |

The MercConverter pass is the boundary: it transforms PTX-derived intermediate opcodes into SM-specific SASS opcodes by dispatching through a large opcode switch (`sub_9ED2D0`, 25KB). After MercConverter, the string `"After MercConverter"` appears in diagnostic output, and the IR is fully in SASS-opcode form. Each instruction then carries enough information for the scheduler to compute accurate latencies, throughputs, and functional-unit assignments.

## Worked Example: `add.f32` to FADD

This traces a single PTX instruction through the Ori representation, showing exactly how the opcode, operands, and register references are encoded in memory.

### PTX Input

```
add.f32 %f3, %f1, %f2
```

After MercConverter (`sub_9F1A90`), this becomes the Ori instruction:

```
FADD R3, R1, R2
```

The type qualifier `.f32` disappears -- the "F" in FADD encodes the float type. Register names `%f1`, `%f2`, `%f3` become virtual register IDs R1, R2, R3 in the R (GPR) register file.

### Instruction Object in Memory

FADD is opcode 12 in the ROT13 name table (ROT13: `SNQQ`, at `InstructionInfo+4184+16*12`). The 296-byte instruction object:

```
Offset  Value              Field
------  -----------------  ---------------------
+0      prev_ptr           Linked-list prev
+8      next_ptr           Linked-list next
+16     <id>               Unique instruction ID
+72     0x0000000C         opcode = 12 (FADD)
+80     0x00000003         operand_count = 3
+84     0x10000003         operand[0] word0: dst R3
+88     0x00000000         operand[0] word1: no ext flags
+92     0x10000001         operand[1] word0: src R1
+96     0x00000000         operand[1] word1: no ext flags
+100    0x10000002         operand[2] word0: src R2
+104    0x00000000         operand[2] word1: no ext flags
```

### Operand Decoding

Take operand[0] word0 = `0x10000003`:

```
  0x10000003 in binary:
    bit 31     = 0       (no sign/negate)
    bits 28-30 = 001     (type = 1 = register operand)
    bits 20-27 = 00000000 (no modifiers)
    bits 0-19  = 00003   (register index = 3)
```

The register index resolves through the register descriptor array:

```c
reg_desc = *(ptr*)(*(ptr*)(code_obj + 88) + 8 * 3);
// reg_desc + 64: reg_file_type = 2 (R / GPR file)
// reg_desc + 12: register number = 3
```

If the source operand were a constant-bank reference (e.g., `FADD R3, R1, c[0][0x10]`), operand[2] would have type=5 (symbol/constant) in word0 and the const-bank flag (`0x1000000`) set in word1. The scheduler distinguishes these two FADD variants for latency modeling: standard FADD gets throughput class `0x3D`, while const-bank FADD gets `0x78`.

## Memory Space Classification

Memory operands carry a space type enum, resolved by `sub_91C840` which maps the PTX-level space identifier to an internal category number. The full input enumeration (from complete decompilation of `sub_91C840`, confidence 0.98):

| Input | PTX Space | Internal Category | Notes |
|-------|-----------|-------------------|-------|
| 0 | (none) | -- | Unmapped, no memory space |
| 1 | Register / generic | 16 | Register file address |
| 2 | Code / function | 12 | Function address |
| 3 | (gap) | -- | Unmapped |
| 4 | `.shared` | 1 | Shared memory |
| 5 | `.const` | 3 | Constant memory |
| 6 | `.global` | 11 | Global memory |
| 7 | `.local` | 2 | Local memory |
| 8 | (gap) | -- | Unmapped |
| 9 | `.local` (variant) | 2 | Same as 7, alternate encoding |
| 10--11 | (gap) | -- | Unmapped |
| 12 | `.param` | 4 | Parameter memory |
| 13 | Generic (unqualified) | 0 | Generic address space |
| 14 | `.tex` | 8 | Texture memory |
| 15 | `.surf` | 17 | Surface memory |
| 16 | Spill space | 7 | Register spill/fill scratch |
| 17 | (gap) | -- | Unmapped |
| 18 | (instruction-dependent) | varies | Sub-classifies by opcode at `a2[1]` |
| 19 | `.uniform` | 15 | Uniform (sm_75+) |
| 20 | `.global` (extended) | 6 | Global, extended variant |
| 21 | `.const` (extended) | 5 | Constant, extended store-to-global path |
| 22 | `.const` (extended, alt) | 5 | Constant, alternate extended |
| 23 | `.surf` / tensor (ext) | 18 | Surface/tensor extended (sm_90+) |

Case 18 (`0x12`) uses a sub-switch on the opcode value at `a2[1]` to further classify: opcodes 7, 43, 45, 53 map to category 6 (global-like); opcode 111 and opcodes in the 183--199 range map to category 5 (constant-like); opcodes 54 and 189 map to category 9 (special).

The hot/cold classifier pair (`sub_A9CDE0` / `sub_A9CF90`) consumes the internal category to partition instructions for scheduling. Hot memory operations (global loads/stores, certain atomics -- category 11) have long latencies and benefit from aggressive scheduling; cold operations (constant loads -- category 3) have shorter latencies and are treated more conservatively.

## Related Pages

- [Instructions](./instructions.md) -- detailed instruction format and encoding
- [CFG](./cfg.md) -- basic block and control-flow-graph internals
- [Registers](./registers.md) -- register model, descriptor layout, allocation interface
- [Data Structures](./data-structures.md) -- hash tables, bitvectors, linked lists
- [Pipeline Overview](../pipeline/overview.md) -- where Ori sits in the full PTX-to-SASS flow
- [PTX-to-Ori Lowering](../pipeline/ptx-to-ori.md) -- how PTX becomes Ori
- [Optimizer](../pipeline/optimizer.md) -- the 159-phase optimization pipeline
- [Hash Tables and Bitvectors](../infra/hash-bitvector.md) -- FNV-1a maps and SSE2 bitvectors used by the CFG

## Key Functions

| Address | Size | Role | Confidence |
|---------|------|------|------------|
| `sub_A3B080` | -- | Code Object constructor; allocates ~1136-byte per-function IR container (vtable at `0x21EE238`) | 0.90 |
| `sub_A4B8F0` | -- | Register count formula: `total_R = v5[159] + v5[102]`, `instr_count = v5[335] - v5[341]` | 0.90 |
| `sub_A3A7E0` | -- | Stats emitter; prints per-function profile (instruction count, register count, occupancy, latency) | 0.90 |
| `sub_BE21D0` | 1.4KB | `CFG::dumpDOT`; emits Graphviz DOT output for the control flow graph | 0.92 |
| `sub_BDE150` | 9KB | `CFG::computeRPO`; explicit DFS stack, assigns reverse post-order numbers into Code Object +720 array | 0.92 |
| `sub_BDE8B0` | 2KB | `CFG::printEdges`; FNV-1a lookup, prints `"bix%d -> bix%d\n"` | 0.92 |
| `sub_BDEA50` | 4KB | `CFG::dumpRPOAndBackedges`; RPO traversal order + backedge debug dump | 0.92 |
| `sub_BE0690` | 54KB | `CFG::buildAndAnalyze`; main CFG constructor -- predecessors, successors, RPO, loop detection | 0.92 |
| `sub_BE2330` | 4KB | `CFG::computeDominators`; post-build dominator and loop analysis with bitvector operations | 0.92 |
| `sub_BE7390` | -- | `InstructionInfo` constructor; initializes 322-entry ROT13 opcode name table at object offset +4184 | 0.90 |
| `sub_9F1A90` | 35KB | MercConverter pass; transforms PTX-derived opcodes into SM-specific SASS opcodes | 0.92 |
| `sub_9ED2D0` | 25KB | Opcode switch inside MercConverter; dispatches per-opcode legalization | 0.90 |
| `sub_91C840` | -- | Memory space classifier; maps PTX-level space identifiers (0--23) to internal category numbers | 0.98 |
| `sub_A9CDE0` | -- | Hot/cold memory classifier (hot path); partitions instructions by memory category for scheduling | 0.85 |
| `sub_A9CF90` | -- | Hot/cold memory classifier (cold path); complement of `sub_A9CDE0` | 0.85 |
| `sub_A60B60` | 24KB | Register stat collector; enumerates ~25 register sub-classes (R, P, B, UR, UP, UB, Tensor/Acc, etc.) | 0.85 |
| `sub_A55D80` | -- | Register allocator verifier; classifies 10 operand-kind problem categories for regalloc validation | 0.95 |
| `sub_40848E` | -- | Operand extended-flag checker; tests `(word1 & 0xFE000000) != 0` across all operands | 0.85 |
| `sub_405769` | -- | Operand flag tester; tests `0x1000000` and `0x6000000` combinations in operand word 1 | 0.85 |
| `sub_404AD0` | -- | Peephole guard; verifies `(word1 & 0xFE000000) == 0` before allowing peephole transforms | 0.85 |
| `sub_B28E10` | -- | `isRegOperand`; ISel pattern matcher operand predicate | 0.90 |
| `sub_B28E20` | -- | `isPredOperand`; ISel pattern matcher operand predicate | 0.90 |
| `sub_B28E40` | -- | `isImmOperand`; ISel pattern matcher operand predicate | 0.90 |
| `sub_B28E80` | -- | `isConstOperand`; ISel pattern matcher operand predicate | 0.90 |
| `sub_B28E90` | -- | `isUReg`; ISel pattern matcher operand predicate | 0.90 |
| `sub_B28E00` | -- | `getRegClass`; returns register class (1023 = wildcard, 1 = GPR) | 0.90 |
