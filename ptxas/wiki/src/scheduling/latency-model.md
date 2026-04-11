# Latency Model & Functional Units

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas instruction scheduler uses a static hardware performance model to estimate instruction latencies, functional unit occupancy, and pipeline conflicts. The model is architecture-parameterized: a family of 15+ profile-builder functions at 0x8E7300--0x8E9DC0 construct per-SM latency/throughput tables consumed by the scheduling engine. A separate 85 KB function (`sub_89FBA0`, SetOpcodeLatencies) assigns per-opcode scheduling classes that index into these tables. The combination produces a cost model that drives stall-count computation, priority scoring, and dual-issue pairing decisions.

| | |
|---|---|
| **Per-opcode classifier** | `sub_89FBA0` (85 KB) -- assigns scheduling class per Ori opcode |
| **HW profile builder** | `sub_8E5CA0` (20 KB) -- assembles scheduling control word tables |
| **Warp profile** | `sub_8E4400` (3.3 KB) -- maps SM ID to warp/dispatch parameters |
| **SM-specific tables** | `sub_8E7300`--`sub_8E97B0` -- 15 architecture-specific builders |
| **Latency query** | `sub_693BC0` (22 lines) -- memory space classification |
| **Long-latency check** | `sub_8CCF80` (2.3 KB) -- returns true if latency > 19 |
| **Resource model** | `sub_A08A00` (345 lines) -- per-instruction FU cost computation |
| **Register query** | `sub_A08910` (39 lines) -- operand register count/cost |
| **Stall update** | `sub_A09530` (91 lines) -- per-instruction stall cycle accumulation |
| **FU class mapper** | `sub_8F0CD0` -- maps (opcode, unit_name) to scheduling class |
| **FU unit query** | `sub_704D30` (14 KB) -- maps SASS opcodes to functional unit IDs |
| **Cutlass detector** | `sub_8F47E0` -- detects cutlass kernels for tuned scheduling |
| **Pipe class assigner** | `sub_13710B0` (7.1 KB) -- SASS-level execution pipe assignment |

## Architecture of the Latency Model

The model has three layers:

```
Layer 1: Per-Opcode Classification
  sub_89FBA0 reads each instruction's Ori opcode (field at instr+72,
  masked with 0xFFFFCFFF) and assigns:
    - Scheduling class ID (stored at descriptor+4, range 1..772+)
    - 9-bit latency index (low 9 bits of descriptor+196)
    - Execution pipe mask (bits 15..19 of descriptor+196..200)
    - Throughput class (bits in descriptor+198..199)

Layer 2: Architecture-Specific HW Tables
  sub_8E7300..sub_8E97B0 build per-SM latency/throughput tables as
  96-byte records in a growable array. Each record maps a scheduling
  class to its pipeline latency, scoreboard wait count, barrier stall
  cycles, and dual-issue compatibility flags.

Layer 3: Runtime Query
  The scheduling engine queries the model via:
    - sub_A08A00 for per-instruction resource costs (3 modes)
    - sub_A08910 for register operand latency
    - sub_693BC0 for memory space classification
    - sub_8CCF80 for long-latency detection (threshold: 19 cycles)
```

## Scheduling Class Assignment (sub\_89FBA0)

`sub_89FBA0` (85 KB, 2938 lines decompiled) is the largest function in the scheduling subsystem. It assigns each instruction a scheduling class -- an integer that indexes into the per-architecture latency tables. The function operates as a massive switch on `*(instr+72) & 0xFFFFCFFF` (the Ori opcode with modifier bits masked out).

### Scheduling Descriptor Layout

Each instruction carries a scheduling descriptor at offsets 196--200 within the 296-byte Ori instruction object (not the SchedNode). The descriptor is a packed bit-field:

```
Descriptor at a3+196 (DWORD, 32 bits):
  [8:0]   9-bit latency index -- indexes into HW latency table
  [14:9]  reserved
  [19:15] 5-bit execution pipe mask -- identifies functional unit
          0x08000 = pipe A (ALU)
          0x10000 = pipe B (FP/tensor)
          0x18000 = pipe C (memory/texture)
          0xF8000 = all pipes (default sentinel)

Descriptor at a3+198 (WORD, 16 bits):
  [3:0]   pipe sub-class within the execution pipe
          0x10 = sub-class 1 (control flow)
          0x20 = sub-class 2 (integer ALU)
          0x30 = sub-class 3 (FP32)
          0x40 = sub-class 4 (FP64 / wide ops)
  [8:4]   throughput class (5 bits)
          0x1F0 = maximum throughput (sentinel)

Descriptor at a3+199 (BYTE, high bits):
  [5:1]   additional pipe flags
          0x3E = all flags set (default)
          Specific values: 0x04 (ALU), 0x08 (SFU), 0x0A (FP64), 0x0C (tensor)

Descriptor at a3+200 (WORD, 16 bits):
  [4:0]   read barrier mask (5 bits, 0x1F)
  [9:5]   write barrier mask (5 bits, 0x3E0)
```

### Opcode-to-Class Mapping

The switch statement maps Ori opcodes to scheduling class IDs stored at `*(v8+4)`. The table below covers the top 50+ common CUDA opcodes organized by functional unit. Each row shows the default (narrow, non-Mercury) class; variant columns note the class under alternative width/architecture gates. All values extracted from `sub_89FBA0` case bodies.

**Integer ALU (pipe 0, full rate)**

| Ori | Hex | SASS mnemonic | Class | Wide class | Notes |
|---|---|---|---|---|---|
| 1 | 0x01 | IMAD | 130 | -- | Sub-class 0x10 (control flow path) |
| 2 | 0x02 | IMAD\_WIDE | 72 | 683 | FP-width: 131 (sub-class 0x20) |
| 3 | 0x03 | IADD3 | 72 | 683 | FP-width 3,5: 140 (sub-class 0x30) |
| 4 | 0x04 | BMSK | 72 | 683 | FP-width 4: 131 |
| 5 | 0x05 | SGXT | 72 | 683 | FP-width: 140 |
| 6 | 0x06 | LOP3 | 72 | 683 | Wide opcode 6: 140, pipe C (0x18000) |
| 7 | 0x07 | ISETP | 72 | 683 | type=19: class 52 |
| 8 | 0x08 | IABS | 2/3 | -- | Flag set: 3, flag clear: 2 |
| 10 | 0x0A | SHF | 200 | 551 | Mercury: 694/700; grouped with 0xB,0x6C,0x95 |
| 14 | 0x0E | FMNMX | 5 | -- | Conversion class |
| 33 | 0x21 | IDP | 75 | -- | Integer dot product |
| 35 | 0x23 | I2I | 45 | -- | Sub-class 0x20, pipe flag 0x06 |
| 37 | 0x25 | IMNMX | 143 | -- | Sub-class 0x20, pipe flag 0x06 |
| 38 | 0x26 | POPC | 533 | -- | Grouped with FLO,BREV,CCTL,etc. |
| 39 | 0x27 | FLO | 188 | -- | Conditional on operand bits |
| 53 | 0x35 | BREV | 547 | -- | Also I2IP (0x37) |

**FP32 / Half-precision / Miscellaneous ALU (pipe 1)**

| Ori | Hex | SASS mnemonic | Class | Wide class | Notes |
|---|---|---|---|---|---|
| 17 | 0x11 | FSEL | 10 | -- | Simple class |
| 18 | 0x12 | FSETP | 11/12 | 544/769 | 4-way split on operand bits |
| 19 | 0x13 | MOV | (default) | -- | No dedicated case; falls to `sub_A2D340` |
| 20 | 0x14 | SEL | (default) | -- | No dedicated case; dynamic model |
| 23 | 0x17 | S2R | 20 | -- | Grouped with 0x39 (BAR), 0x65 (GETLMEMBASE) |
| 24 | 0x18 | PRMT | 21 | 641 | Wide: 641 (Mercury); sub-class 0x20 |
| 26 | 0x1A | VOTE | 22 | -- | -- |
| 27 | 0x1B | CS2R\_32 | 27 | -- | -- |
| 28 | 0x1C | CS2R\_64 | 28 | 642 | Mercury: 642 |
| 32 | 0x20 | VABSDIFF4 | 33 | -- | Also case 0x10F |
| 36 | 0x24 | I2F/PRMT | 96 | 586/707 | Wide: 586; Mercury: 707 |
| 40 | 0x28 | FCHK | 54 | -- | Also FRND (0x2F) |
| 42 | 0x2A | MUFU | 548 | -- | SFU pipe at HW level (quarter rate) |
| 58 | 0x3A | BAR | 64 | -- | Barrier synchronization |
| 81 | 0x51 | NANOSLEEP | 142 | -- | -- |
| 119 | 0x77 | SHFL | 202 | -- | Warp shuffle |
| 240 | 0xF0 | F2F | 99 | -- | Pipe A (0x08000), flag 0x04 |
| 241 | 0xF1 | (sm82 FP) | 32 | -- | -- |

**FP64 (pipe 2)**

| Ori | Hex | SASS mnemonic | Class | Wide class | Notes |
|---|---|---|---|---|---|
| 122 | 0x7A | DFMA | 619 | -- | Latency idx 0xE8 |
| 123 | 0x7B | DADD | 131 | -- | Latency idx 0x03 |
| 124 | 0x7C | DMUL | 203 | -- | -- |
| 125 | 0x7D | DSETP | 190 | -- | -- |
| 201 | 0xC9 | DFMA (sm82+) | 97/98 | 175/688 | Wide: 175; Mercury: 688 |
| 209 | 0xD1 | (sm82 DP) | 592 | 713 | Mercury: 713 |
| 210 | 0xD2 | (sm82 DP) | 593 | 714 | Sub-class 0x30, flag 0x04 |
| 211 | 0xD3 | (sm82 DP) | 594 | -- | -- |

**Memory load/store (pipe 4, full rate)**

| Ori | Hex | SASS mnemonic | Class | Notes |
|---|---|---|---|---|
| 16 | 0x10 | ATOM | 7 | Flagged: 575; shared: 8; const: 9 |
| 60 | 0x3C | STG | 67/95 | 6-way split on same-src/flag/remat |
| 62 | 0x3E | LDL | 69/70 | Flag vs no-flag split |
| 63 | 0x3F | CALL | 71 | Sub-class 0x20, pipe flag 0x06 |
| 78 | 0x4E | LD (generic) | 125/126/127 | 3-way: remat/flag/no-flag |
| 89 | 0x59 | LDC | 170 | -- |
| 94 | 0x5E | LDS | 29/30 | Reg-class-3 split |
| 95 | 0x5F | STS | 25 | Grouped with 0x5D |
| 96 | 0x60 | LDG | 183 | -- |
| 98 | 0x62 | LDL (ext) | 526 | Mercury: 697 |
| 100 | 0x64 | LD (ext) | 648 | -- |
| 102 | 0x66 | ATOM (ext) | 197 | Mercury: 691 |
| 104 | 0x68 | RED | 198 | Mercury: 692 |
| 109 | 0x6D | CCTLL | 184 | mem=2: class 7 |

**Control flow (pipe 6)**

| Ori | Hex | SASS mnemonic | Class | Notes |
|---|---|---|---|---|
| 67 | 0x43 | BRA | 87 | Mercury: 670 |
| 79 | 0x4F | BSYNC | 128 | All pipes (0xF8000), sub-class 0x10 |
| 80 | 0x50 | MATCH | 129 | Sub-class 0x20, pipe flag 0x06 |
| 93 | 0x5D | OUT\_FINAL | 25 | Same class as STS |

**Tensor core (pipe 3)**

| Ori | Hex | SASS mnemonic | Class | Latency idx | Notes |
|---|---|---|---|---|---|
| 270 | 0x10E | HMMA\_16 | 103/104 | -- | Wide: 758, sub-class 0x40/0xC (idx 0xF7) |
| 279 | 0x117 | HMMA\_32 | 106/107 | -- | Operand bit split |
| 280 | 0x118 | HMMA pair | 120/760 | 0xF9 | Wide: 760; type=6: 88 |
| 282 | 0x11A | IMMA | 121 | -- | Sub-class 0x40, pipe B |
| 321 | 0x141 | WGMMA | 745 | 0xF1 | Warpgroup MMA |
| 322 | 0x142 | WGMMA (v3) | 744 | 0xF0 | Others fall to class 203 |
| 323 | 0x143 | BGMMA/QMMA | 765--767 | 0xFB | 3-way operand split |
| 324 | 0x144 | Tensor fence | 600 | 0xE6 | -- |
| 325 | 0x145 | HMMA/BMMA | 759 | 0xF8 | Sub-class 0x40, pipe flag 0x0C |
| 327 | 0x147 | DP tensor | 757/761 | 0xF6/0xFA | Wide: 761; narrow: 757 |
| 329 | 0x149 | Tensor sync | 604 | 0xE7 | -- |

**Collective / bulk operations (sm90+)**

| Ori | Hex | SASS mnemonic | Class | Latency idx | Notes |
|---|---|---|---|---|---|
| 317 | 0x13D | Collective | 747/750 | 0xF2/0xF5 | Variant-dependent |
| 318 | 0x13E | ACQBULK | 749 | 0xF4 | Bulk copy |
| 319 | 0x13F | RELBULK | 748 | 0xF3 | Bulk release |

The scheduling class IDs span a wide range (0--772+). Classes below 256 correspond to legacy instruction categories present since Volta; higher classes (526--769) represent newer types added for Ampere, Hopper, and Blackwell. Class 772 is a sentinel that triggers a fallback to the dynamic cost model via `sub_A2D340`.

### Latency Index Encoding

The low 9 bits of the descriptor at `a3+196` encode a latency index that maps directly into the per-architecture HW table. The index is formed by combining the descriptor's low byte with a pipe mask:

```
latency_index = *(WORD*)(a3+196) & 0x1FF
```

Observed latency index values and their instruction classes. The table below covers both common instructions (low indices, where the index equals the scheduling class ID) and the tensor/collective range (0xE6--0xFB, assigned via explicit latency-index overrides in `sub_89FBA0`). The "model latency" column gives the scheduling cost from `per_sm_dependency_rules`; the "throughput^-1" column is the minimum issue interval between successive instructions of the same class on a single SM sub-partition.

**Common instruction classes (index = scheduling class ID):**

| Sched class | Pipe | Instruction class | Model latency (sm80/sm90/sm100) | Throughput^-1 | SASS examples |
|---|---|---|---|---|---|
| 2 | ALU | Integer add/logic (narrow) | 17 / 17 / 17 | 0 (fully pipelined) | IADD3, LOP3, SHF |
| 3 | ALU | Predicate operations | 17 / 17 / 17 | 0 | ISETP, PSETP, PLOP3 |
| 4 | FMA | FP32 fused multiply-add | 42 / 42 / 42 | 15 | FFMA, FMUL |
| 5 | FMA | FP32 add/compare | 42 / 42 / 42 | 15 | FADD, FSETP, FMNMX |
| 10 | DFMA | FP64 conversion/misc | 42 / 42 / 42 | 15 | F2F, I2F (64-bit) |
| 11 | ALU | Predicate move (true) | 17 / 17 / 17 | 0 | MOV (predicate) |
| 15 | LSU | Global memory (default) | 255 / 22 / 22 | 35 / 2 / 2 | LDG, STG |
| 16--19 | LSU | Global load variants | 21--23 / 20--23 / 21--56 | 19 | LDG.E, LDG.128 |
| 20 | ALU | Shared memory simple | 28 / 28 / 28 | 5 | LDS, STS |
| 21 | LSU | Constant memory | 22 / 22 / 22 | 2 | LDC |
| 35 | XU | Extended unit (fallback) | 52 / 52 / 52 | 21 | PRMT, BFE, BFI |
| 52 | LSU | Integer compare (narrow) | 13 / 48 / 46 | 19 | ISETP (narrow path) |
| 56--59 | SFU | Special function unit | 45--47 / 47 / 41--45 | 19 | MUFU (RCP, RSQ, SIN, COS) |
| 60--61 | LSU | Shared memory atomic | 13--48 / 48 / 46 | 19 | ATOMS, REDS |
| 66--67 | DFMA | FP64 multiply/FMA | 72 / 48--74 / 72 | 34 / 19--33 / 34 | DFMA, DMUL |
| 72 | ALU | Integer ALU (standard) | 5 / 31 / 31 | 9 / 12 / 12 | IADD3, IMAD (standard) |
| 118 | MMA | Double-precision MMA | 15 / 14 / 15 | 19 | DMMA |
| 130 | BRA | Control flow (branch) | 22 / 22 / 22 | 2 | BRA, JMP, EXIT |
| 131 | ALU | Integer MAD | 22 / 22 / 22 | 2 | IMAD |
| 140 | FMA | FP32 operations | 22 / 22 / 22 | 2 | FADD, FFMA, FCHK |
| 200 | ALU | General ALU | 22 / 22 / 22 | 2 | IADD3, LOP3, IABS |
| 551 | ALU | Extended integer (wide enc) | 22 / 22 / 22 | 2 | IMAD.WIDE |
| 575 | FMA | Atomic with flag | 42 / 42 / 42 | 15 | ATOM (flag variant) |
| 683 | DFMA | Wide FP64 operations | 70 / 70 / 70 | 31 | DADD, DFMA (wide) |
| 694 | DFMA | Mercury extended integer | 70 / 70 / 70 | 31 | IMAD (Mercury wide) |
| 700 | DFMA | Mercury extended integer v2 | 70 / 70 / 70 | 31 | IMAD (Mercury alt) |

**Tensor and collective operations (explicit latency index override):**

| Index (hex) | Index (dec) | Sched class | Model latency (sm100) | Instruction class |
|---|---|---|---|---|
| 0xE6 | 230 | 600 | 42 | Tensor fence / sync |
| 0xE7 | 231 | 604 | 42 | Tensor synchronization |
| 0xF0 | 240 | 744 | 65 | WGMMA variant |
| 0xF1 | 241 | 745 | 65 | WGMMA primary |
| 0xF2 | 242 | 747 | 66 | Collective op (variant A) |
| 0xF3 | 243 | 748 | 66 | Bulk release (RELBULK) |
| 0xF4 | 244 | 749 | 66 | Bulk copy (ACQBULK) |
| 0xF5 | 245 | 750 | 66 | Collective op (variant B) |
| 0xF6 | 246 | 757 | 22 | DP tensor (narrow) |
| 0xF8 | 248 | 759 | 22 | Tensor core (HMMA/BMMA) |
| 0xFA | 250 | 761 | 22 | DP tensor (wide) |
| 0xFB | 251 | 765--767 | 52 | BGMMA/QMMA |

For common instruction classes, the latency index stored at `a3+196` equals the scheduling class ID directly -- no remapping is needed. Only the tensor/collective classes (0xE6--0xFB) use explicit latency-index overrides that differ from their scheduling class ID. The model latency value of 17 for ALU classes with throughput 0 corresponds to the lowest-latency fully-pipelined path (4-cycle register-to-register, encoded as cost 17 in the scheduling cost product). Classes with model latency 22 and throughput 2 represent the "default" short-latency profile used for most single-cycle pipe instructions. The jump from 42--52 to 65--72 marks the boundary between standard functional units and long-latency tensor/FP64 operations that require scoreboard barriers.

## Functional Unit Categories

The scheduler tracks 10 functional unit resource counters per basic block. Each counter corresponds to a hardware execution pipe on the SM.

### 10-Element Resource Vector

Resource tracking uses an 84-byte per-BB slot at `*(scheduler+672) + 84 * slot_index`:

| Index | Pipe name | Typical SASS instructions | Throughput (IPC) |
|---|---|---|---|
| 0 | Integer ALU (ALU) | IADD3, IMAD, ISETP, LOP3, SHF, IABS, POPC | 1 (full rate) |
| 1 | FP32 (FMA) | FADD, FFMA, FMUL, FSETP, FMNMX, FCHK | 1 (full rate) |
| 2 | FP64 (DFMA) | DADD, DFMA, DMUL, DSETP, DMNMX | 1/2 to 1/64 (SM-dependent) |
| 3 | Tensor core (MMA) | HMMA, IMMA, BMMA, BGMMA, WGMMA, QMMA | varies |
| 4 | Load/store (LSU) | LDG, STG, LDL, STL, LDS, STS, LDGSTS | 1 (full rate) |
| 5 | Texture (TEX) | TEX, TLD, TXQ, TLD4, TEXS | 1/2 to 1/4 |
| 6 | Control flow (BRA) | BRA, JMP, EXIT, RET, CALL, BRK, CONT | 1 |
| 7 | Shared memory (SMEM) | ATOMS, REDS, LDS, STS (atomic/reduce variants) | 1 |
| 8 | Special function (SFU) | MUFU (RCP, RSQ, SIN, COS, EX2, LG2) | 1/4 |
| 9 | Uniform datapath (UDP) | UPLOP3, UISETP, UIMAD, uniform operations | 1 |

The resource vector layout within each 84-byte slot:

```
Offset  Size       Content
 0..39  10 x int32  Current resource usage per FU (pipe 0..9)
40..79  10 x int32  Resource pressure delta (change from scheduling)
80..83  1 x int32   BB-entered flag and auxiliary state bits
```

### Functional Unit Class Mapping (sub\_8F0CD0)

A secondary mapper at `sub_8F0CD0` translates (opcode, unit-name-string) pairs to numeric scheduling class IDs for the stall/barrier encoding stage:

| Opcode | Unit string | Class ID | Meaning |
|---|---|---|---|
| 40 | `"LSU_T"` | 15 | Texture load/store unit |
| 40 | `"XU64"` | 35 | Extended unit (64-bit ops) |
| 39 | `"DMMA"` | 118 | Double-precision matrix multiply |
| 53 | `"DMMA"` | 118 | DMMA (alternate opcode) |
| default | -- | 35 | Fallback to extended unit |

The `"LSU_T"` and `"XU64"` string tags appear in the Mercury-era post-scheduling pipeline where the SASS encoder needs to distinguish sub-pipes within the load/store and extended-precision units.

### Functional Unit Query (sub\_704D30)

`sub_704D30` (14 KB) maps SASS opcode character codes to functional unit IDs for the Mercury encoder's latency model. The mapping uses single-character opcode identifiers:

| Char code | Decimal | FU ID | Unit |
|---|---|---|---|
| `'D'` (68) | 68 | 40 | FP64 unit |
| `'E'` (69) | 69 | 44 | Extended unit |
| `'F'` (70) | 70 | 48 | FP32 unit |
| `'J'` (74) | 74 | 52 | Integer unit |
| `'K'` (75) | 75 | 56 | Conversion unit |
| `'L'` (76) | 76 | 60 | Load/store unit |
| `'N'` (78) | 78 | 32 | Tensor unit |
| `'S'` (83) | 83 | 36 | Special function unit |

The function dispatches on `*(config+372) >> 12` (the SM architecture selector) to handle architecture-specific unit mapping variations (e.g., Kepler vs Volta).

## Per-Architecture HW Latency Tables

### Table Construction Pipeline

The HW latency tables are built during scheduler initialization by a chain of constructors:

```
sub_8E4400(profile, sm_id, sched_mode)     // Warp-level parameters
  |
  v
sub_8E5CA0(profile, table_ptr, table_size) // Assemble output array
  |
  +-- sub_8E6760()  // Group boundary markers
  +-- sub_8E6950()  // Barrier entries
  +-- sub_8E6B40()  // Standard scheduling entries
  +-- sub_8E6F20()  // Wait dependency entries
  +-- sub_8E7110()  // Scoreboard entries
  |
  v
sub_8E7300..sub_8E97B0(profile, ...)       // SM-specific table population
  |
  v
sub_8E3AD0(output, count, entries, ...)    // Copy into final profile
```

Each SM-specific function populates entries in the 96-byte-per-record output array. Records encode latency, throughput, pipe assignment, and barrier compatibility for each scheduling class.

### 96-Byte Schedule Record Format

Each record in the HW table occupies 96 bytes (6 x 16-byte XMM slots). Records are stored in a growable array at `*(context+56)` with count at `*(context+64)` and capacity at `*(context+68)`. The array grows by 1.5x when full. Records are copied using three `_mm_loadu_si128` operations (offsets 0, 16, 32) plus manual field-by-field copy for offsets 48--95; the string at +48 is reference-cloned via `sub_714160` when the string-backed flag is set.

```
Offset  Size   Field               Content
------  ----   -----               -------
 0..1   WORD   type_code           Record type (see type table below)
 2..3   WORD   (padding)           Zero
 4..7   DWORD  aux_size            Type-dependent:
                                     root (type 1): table_size
                                     barrier ('M'): 128 (fixed)
                                     wait/scoreboard ('5'/'6'): 36
                                     sched entry (23): 0
 8..15  8B     (reserved)          Zero
16..19  DWORD  cost_product        Scheduling cost (latency x throughput product)
                                     - Standard entry (23): a2 * a3
                                     - Category header ('!'): entry_count from config+528
                                     - Wait/scoreboard: 280 (fixed sentinel)
                                     - SM-specific (','): 4 * class_count
20..21  WORD   base_latency        Base latency in cycles (standard entries only)
22..23  WORD   dual_issue_flags    Dual-issue compatibility mask (standard entries only)
24..31  8B     (reserved)          Zero
32..39  QWORD  data_ptr            Pointer to type-specific data block:
                                     - Root: parent profile object
                                     - Wait/scoreboard: dependency tracking table
                                     - Barrier: barrier data array
                                     - Category headers: 0
40..47  QWORD  data_size           Byte count of data block at data_ptr:
                                     - Root: table_size; barrier: 128
                                     - Wait/scoreboard: 36; headers: 0
48      BYTE   inline_flag         0 = data_ptr/data_size carry raw data
                                   1 = this record uses the inline string buffer
49..63  15B    inline_str_buf      Inline NUL-terminated string (max 15 chars)
64..71  QWORD  parent_ptr          Back-pointer: SM-specific entries point to table
                                   root; category headers point to profile object
72..79  8B     (reserved)          Zero
80..87  QWORD  string_buf_ptr      Pointer to growable string buffer (32-byte header:
                                   data_ptr, size, capacity, allocator) for variable-
                                   length sub-records; self-references +48 when inline
88      BYTE   string_backed_flag  1 = record owns allocated string data at +80
                                   0 = no allocated string (uses inline or none)
89..95  7B     (padding)           Zero
```

#### Record Type Codes

Records are polymorphic -- the type code at offset +0 selects the interpretation of fields +16..+31, +32..+47, and the sub-record format stored in the growable buffer at +80.

| Type | ASCII | Creator | Role |
|---|---|---|---|
| 1 | -- | `sub_8E5CA0` | Root container (wraps entire HW table) |
| 23 | -- | `sub_8E6B40` | Standard scheduling entry (latency + throughput + dual-issue) |
| 33 | `'!'` | `sub_8E5740` | Category header (begins a named section with string list) |
| 44 | `','` | `sub_8E8480` et al. | SM-specific table entry (per-architecture class data) |
| 45 | `'-'` | `sub_8E5CA0` | Barrier section header (links 128-byte barrier table) |
| 49 | `'1'` | `sub_8E5530` | Dimension entries (contains 12-byte sub-records) |
| 53 | `'5'` | `sub_8E7110` | Scoreboard entry (dependency tracking, data\_size=36) |
| 54 | `'6'` | `sub_8E6F20` | Wait dependency entry (dependency table, data\_size=36) |
| 57 | `'9'` | `sub_8E5740` | Category footer (closes the section opened by type 33) |
| 59 | `';'` | `sub_8E5310` | Variant section (contains 20-byte sub-records) |
| 60 | `'<'` | `sub_8E6760` | Group boundary marker (separates scheduling groups) |
| 69 | `'E'` | `sub_8E6950` | Barrier entry (a2 = stall count in cost\_product field) |
| 77 | `'M'` | `sub_8E6D40` | Barrier/sync data entry (data\_ptr = barrier array, 128B) |
| 87 | `'W'` | `sub_8E4F20` | Supplementary weight entry (variable-length string data) |

#### Worked Example: unit\_id 2, sm\_80 (Predicate Operations)

Scheduling class 2 (predicate operations with flag clear -- PSETP, PLOP3) provides a clean example because it uses the simplest record type (23, standard entry) and its pipe assignment is unambiguous. Below we decode both the 72-byte HW latency table entry from `sm_8x_shared` and its matching 40-byte dependency rule from the `sm_80` table.

**72-byte HW latency entry** (at `0x2297C00 + 0 * 72 = 0x2297C00`):

```
Offset  Bytes                       Field           Decoded
------  -----                       -----           -------
 0..1   02 00                       unit_id         2 (predicate ops, flag-clear)
 2..3   00 00                       reserved        0
 4..11  03 03 FF FF FF FF FF FF     pipe_masks_a    pipes 0+1 active; pipes 2--7 = 0xFF (unused)
12..19  00 00 FF FF FF FF 00 00     pipe_masks_b    pipes 0+1 not dual-issue eligible;
                                                    pipes 2--5 = 0xFF (N/A); pipes 6--7 = 0
20..31  00 04 00 01 00 07 03 02     sched_params    [0]=0: no special flag
        01 02 03 00                                 [1]=4: throughput class (1/4 rate)
                                                    [2]=0: no read hazard override
                                                    [3]=1: write-after-read gap = 1
                                                    [4]=0: no scoreboard class
                                                    [5]=7: max stall cycles
                                                    [6]=3: barrier class
                                                    [7]=2: barrier stall weight
                                                    [8]=1: minimum issue distance
                                                    [9]=2: pipe contention group
                                                    [10]=3: resource class
                                                    [11]=0: reserved
```

The `pipe_masks_a` value of `[3,3,0xFF,...]` means the instruction can issue on pipe 0 or pipe 1 (bitmask `0x03` = bits 0+1 set). Pipes 2--7 carry `0xFF` = "not applicable." The `pipe_masks_b` zero entries for pipes 0--1 indicate this class is **not** eligible for dual-issue pairing on those pipes. `sched_params[5]=7` sets the maximum number of stall cycles the scheduler will model before it gives up and inserts a barrier.

**40-byte dependency rule** (from `sm_80` table, entry 0, matching unit\_id 2):

```
Offset  Bytes       Field               Decoded
------  -----       -----               -------
 0..1   02 00       unit_id             2 (must match latency entry)
 2      01          rule_type           1 = standard instruction rule
 3      11          latency             17 cycles (0x11) -- result-to-use delay
 4      00          throughput_inv      0 = full-rate (no throughput penalty)
 5      38          barrier_latency     56 cycles (0x38) -- latency when tracked via barrier
 6      08          barrier_throughput  8 = barrier occupies 1/8 of scoreboard bandwidth
 7..8   FF FF       read_latency        -1 (0xFF) = use default (no read-side override)
 9..10  FF FF       write_latency       -1 (0xFF) = use default (no write-side override)
11..12  00 00       stall_cycles        0 = no mandatory stall before next issue
13      01          issue_slots         1 = single-issue (occupies 1 dispatch slot)
14..39  00...       (padding)           Zero-filled to 40 bytes
```

**How this populates the 96-byte scheduling record.** When `sub_8E6B40` creates a type-23 record for this class, it fills the 96-byte structure as:

```
+0   type_code       = 23            (standard scheduling entry)
+4   aux_size        = 0             (standard entries carry no aux block)
+16  cost_product    = 17 * 4 = 68   (latency=17 x throughput_class=4 from sched_params[1])
+20  base_latency    = 17            (copied from dependency rule)
+22  dual_issue_flags= 0x0000        (pipe_masks_b[0..1] both zero -> not dual-issue eligible)
+32  data_ptr        = 0             (no sub-record data for type 23)
+40  data_size       = 0
+48  inline_flag     = 0
+88  string_backed   = 0
```

The `cost_product` at offset +16 is the scheduler's primary sorting key: higher values push the instruction later in the schedule. For this predicate class, 68 is a low cost -- compare with FP64 ops (class 4, latency=42, throughput=15) where `cost_product = 42 * 15 = 630`, nearly 10x higher, reflecting the FP64 pipe's lower throughput.

#### Sub-Record Formats in the Growable Buffer (+80)

Records with `string_backed_flag=1` carry variable-length sub-records in the growable buffer. The buffer header at `*(record+80)` is a 32-byte object: `{data_ptr, size (DWORD), capacity (DWORD), allocator_ptr}`.

**Type 59 (';') -- Variant sub-records (20 bytes each):**

Created by `sub_8E5310` iterating the variant list at `config+536`:

```
Sub-record layout (20 bytes):
  +0   DWORD   source_data       Variant source identifier
  +4   WORD    flags             Variant flags
  +6   WORD    zero              Reserved
  +8   DWORD   throughput_value  Throughput for this variant
  +12  DWORD   aux_value         Auxiliary parameter
  +16  DWORD   zero              Reserved
```

The main record additionally stores: `+16 = start_index` (from `config+544`), `+20 = record_index`, `+24 = back_ref` to previous category.

**Type 49 ('1') -- Dimension sub-records (12 bytes each):**

Created by `sub_8E5530` traversing the BST at `config+592`:

```
Sub-record layout (12 bytes):
  +0   WORD    node_flags        BST node flags (from node+38)
  +2   WORD    zero              Reserved
  +4   DWORD   node_value        BST node value (from node+32)
  +8   DWORD   node_child        BST node child pointer (low 32 bits of node+24)
```

**Type 44 (',') -- SM-specific class descriptor (16 bytes + packed bitmasks):**

Created by `sub_8E8480` and other SM-specific builders, followed by a call to `sub_8E3AD0` which appends packed bitmask DWORDs:

```
Initial 16-byte descriptor:
  +0   DWORD   class_flags = 2   Fixed flag value
  +4   WORD    zero              Reserved
  +8   QWORD   mask              Latency mask (0xFFFFFFFF00000000)

Followed by bitmask DWORDs (4 bytes each, one per 8 scheduling classes):
  Each DWORD encodes 4 bits per entry (4 entries x 4 properties):
    bit 4*i+0:  entry[i].field_0 != 1
    bit 4*i+1:  entry[i].field_4 != 1
    bit 4*i+2:  entry[i].field_8 != 1
    bit 4*i+3:  entry[i].field_12 != 1
  Source entries are 20 bytes apart in the input array.
```

#### Assembly Sequence

`sub_8E5CA0` orchestrates the complete table by emitting records in this order:

1. **Barrier header** (type `'-'`, conditional on `config+336`): links the 128-byte barrier data table at `config+272`.
2. **Root container** (type 1): `data_ptr = profile_object`, `data_size = table_size`.
3. **Category header + footer** (types `'!'` / `'9'`): emitted by `sub_8E5740`, which enumerates named sections from `config+520..528`.
4. **Variant section** (type `';'`): emitted by `sub_8E5310` if `config+544 != 0`.
5. **Supplementary weights** (type `'W'`): emitted by `sub_8E4F20` if `config+640 != -1`.
6. **Dimension entries** (type `'1'`): emitted by `sub_8E5530` if `config+608 > 0`.

After all records are appended, the function computes the total serialized size (with 16-byte alignment padding per data block), allocates the output buffer, and writes a 32-byte header per record into the linear output at `context+104`.

### Architecture Dispatch Table

| Address | SM | Architecture | Table size | Notes |
|---|---|---|---|---|
| `sub_8E7300` | sm\_70 | Volta | 3.3 KB | First Turing-era table format |
| `sub_8E7540` | sm\_72 | Xavier | 2.9 KB | Automotive Volta variant |
| `sub_8E7720` | sm\_75 | Turing | 3.5 KB | Added TensorFloat-32 |
| `sub_8E7940` | sm\_80 (base) | Ampere base | 2.9 KB | Shared base for sm\_80/86/87 |
| `sub_8E7B40` | sm\_80 | Ampere | 3.3 KB | Full Ampere with async copy |
| `sub_8E7D80` | sm\_86 | GA10x | 4.4 KB | Consumer Ampere |
| `sub_8E8070` | sm\_87 | Orin | 3.5 KB | Automotive Ampere |
| `sub_8E8280` | sm\_89 | Ada Lovelace | 3.1 KB | Added FP8 tensor ops |
| `sub_8E8480` | sm\_90 | Hopper | 5.2 KB | DPX, WGMMA, TMA |
| `sub_8E8780` | sm\_90a | Hopper accel. | 4.6 KB | WGMMA async extensions |
| `sub_8E8A90` | sm\_100 | Blackwell DC | 3.0 KB | 5th-gen tensor, TCGEN05 |
| `sub_8E8CB0` | sm\_100 (short) | Blackwell DC | 949 B | Supplementary table |
| `sub_8E8DB0` | sm\_103 | Blackwell Ultra | 1.7 KB | GB300 extensions |
| `sub_8E8F60` | sm\_103 (short) | Blackwell Ultra | 618 B | Supplementary table |
| `sub_8E9000` | sm\_120 | RTX 50xx | 2.9 KB | Consumer Blackwell |
| `sub_8E92E0` | sm\_120 (ext) | RTX 50xx | 5.5 KB | Extended consumer table |
| `sub_8E97B0` | universal | Fallback | 8.8 KB | Default for unknown SM |

sm\_90 (Hopper) has the second-largest combined table (5.2 + 4.6 KB including sm\_90a) reflecting the complexity of WGMMA, DPX, and TMA scheduling. sm\_120 extended (5.5 KB) is the single largest individual table, accommodating the consumer Blackwell feature set.

The "short" supplementary tables (sub\_8E8CB0 for sm\_100, sub\_8E8F60 for sm\_103) add entries for architecture-specific instructions not covered by the base table -- typically new tensor core variants and collective operations.

## Warp-Level Hardware Profile (sub\_8E4400)

`sub_8E4400` maps the SM architecture ID (a2) to warp-level dispatch parameters stored in a 36-byte structure:

### Architecture-to-Warp Mapping

| SM ID range | Warps per SM | Dispatch slots | Architecture era |
|---|---|---|---|
| <= 20479 | 4 | 96 | sm\_50 (Maxwell) |
| 20480--24575 | 6 | 176 | sm\_60 (Pascal) |
| 24576--28672 | 7 | 192 | sm\_70 (Volta) |
| 28673--32767 | 7 | 208 | sm\_75 (Turing) |
| 32768--36863 | 8 | 224 | sm\_80 (Ampere) |
| > 36863 | 16 | 240 | sm\_90+ (Hopper, Blackwell) |

The packed DWORD at offset +18 encodes (warps, sub-warp-count) as a 32-bit value. For example, `983055` (0x000F000F) = 15 warps in the low half and 15 in the high half, while `1048592` (0x00100010) = 16 warps for sm\_90+.

### Sub-Architecture Variants

Specific SM version IDs map to sub-architecture variant codes stored at offset +26:

| SM ID | Hex | Variant | Architecture |
|---|---|---|---|
| 8193 | 0x2001 | 2 | sm\_50 (Maxwell Titan X) |
| 20481 | 0x5001 | 2 | sm\_60 variant |
| 24576 | 0x6000 | 0 | sm\_70 (Volta base) |
| 28674 | 0x7002 | 2 | sm\_75 variant A |
| 28675 | 0x7003 | 3 | sm\_75 variant B |
| 28676 | 0x7004 | 4 | sm\_75 variant C |
| 28677 | 0x7005 | 5 | sm\_75 variant D |
| 32768 | 0x8000 | 0 | sm\_80 (Ampere base) |
| 36864 | 0x9000 | 0 | sm\_90 (Hopper base) |
| 36867 | 0x9003 | 3 | sm\_90 variant A |
| 36868 | 0x9004 | 4 | sm\_90 variant B (sm\_90a) |
| 36869 | 0x9005 | 5 | sm\_90 variant C |

### Pipeline Width (offset +24)

The scheduling mode parameter (a3) selects the pipeline width stored at offset +24. This value controls how many instructions the scheduler models as issuing per cycle:

| Mode | Value at +24 | Meaning |
|---|---|---|
| 1, 8, 9 | 1 | Single-issue |
| 3 | 4 | Quad-issue (tensor) |
| 4 | 5 | Penta-issue |
| 5 | 6 | Hexa-issue |
| 6 | 7 | Hepta-issue |
| 7 | 8 | Octa-issue |
| 10 | 9 | Nona-issue |
| 11 | 10 | Deca-issue |
| default | 2 | Dual-issue |

These values model the effective issue width for different scheduling contexts. The tensor core modes (4--11) reflect warpgroup-level cooperative execution where multiple warp slots issue tensor instructions simultaneously.

## Memory Space Classification (sub\_693BC0)

`sub_693BC0` (22 lines) classifies the memory space of load/store instructions. It extracts the last source operand from the instruction, looks up the register descriptor, and calls `sub_91C840` to determine the memory space type. The function returns an integer code:

| Return value | Memory space | Typical latency range |
|---|---|---|
| 1 | Generic (resolved at runtime) | 20--200+ cycles |
| 2 | Local memory (per-thread stack) | 20--200 cycles |
| 3 | Shared memory | 20--30 cycles |
| 4 | Constant memory (cached) | 4--8 cycles |
| 7 | Constant bank (indexed) | 4--8 cycles |
| 11 | Surface memory | 200--500 cycles |
| 16 | Global memory (DRAM) | 200--500 cycles |

The scheduler uses these values in the priority function (`sub_8C9320`) to distinguish "hot" (long-latency) memory operations from "cold" (short-latency) ones. Functions `sub_A9CDE0` classifies hot (global/texture) memory and `sub_A9CF90` classifies cold (constant/shared) memory.

### Long-Latency Detection (sub\_8CCF80)

`sub_8CCF80` checks if an instruction qualifies as "long-latency" for scheduling priority purposes. The function:

1. Verifies the target architecture supports dual-issue via `sub_7DC0E0`.
2. For opcode 183 (LD/ST variant): checks memory space via `sub_693BC0`. Memory spaces 4, 16, 2, 11, 3, 1, and 7 all qualify for long-latency classification.
3. For opcode 130 (`HSET2` in the ROT13 name table; used as a generic internal marker): queries via vtable+640 whether the instruction is recognized as long-latency.
4. Queries the scheduling oracle (`sub_8BF3A0`) for the instruction's estimated latency.
5. Returns `true` if the estimated latency exceeds **19 cycles**.

The threshold of 19 cycles is the boundary between "short-latency" instructions (ALU, FP32, shared memory) and "long-latency" instructions (global memory, texture, tensor core) that benefit from latency hiding through instruction reordering.

## Resource Cost Model (sub\_A08A00)

`sub_A08A00` (345 lines) computes per-instruction resource costs for the 10-element functional unit vector. It operates in three modes selected by parameter `a6`:

### Mode 0/1: Instruction Cost Initialization

Resets the instruction's resource tracking state:
- `a1[0]` = 0 (accumulated cost)
- `a1[1045]` = 0 (accumulated delta)
- `a1[2071]` = 0 (accumulated pressure)
- Byte at offset 8280 = 0 (flags)

Then computes per-operand resource contributions by iterating source operands (count at `a3+80`, starting at `a3+84`):

### Mode 2: Differential Cost

Computes the differential cost (new minus old):
```
v55 = a1[0]       // previous instruction cost
v56 = a1[1045]    // previous delta cost
```
Then runs the same operand iteration as mode 1 and subtracts the previous values.

### Mode 3: Pressure Accumulation

Adds the instruction's previously computed pressure `a1[2071]` into the running total at `*(a5+24)`.

### Per-Operand Cost Computation

For each source operand, the function:
1. Checks operand type: `((operand >> 28) & 7) == 1` means register operand.
2. Skips operands with values 41--44 (special sentinel registers).
3. Looks up the register descriptor via `*(a1+88) + 8 * (operand & 0xFFFFFF)`.
4. Checks if register class `*(descriptor+64)` is <= 6 (physical register file).
5. Calls `sub_A08910` to get the register's latency and count:
   - Returns the starting register index
   - Outputs count (`*a4`) and cost-per-register (`*a5`)
6. Iterates over the register range, accumulating costs for registers not in the "already-consumed" bitmask at `*(a1+832)`.

The cost accumulation uses a 9-bit field in the instruction's scheduling word at offset +12, masked as `& 0x1FF`.

## Register Latency Query (sub\_A08910)

`sub_A08910` (39 lines) returns the register index and cost for a single operand:

```
function GetRegisterLatency(context, reg_desc, operand, out_count, out_cost):
    pipeline_bits = (reg_desc.field_48 >> 20) & 3
    count = 1
    cost = (pipeline_bits == 3) ? 2 : 1
    *out_count = count
    *out_cost = cost

    if context.flags & 0x10:    // dual-register tracking mode
        return 2 * reg_desc.field_12     // doubled register index
    else:
        if context.flags & 0x08 and pipeline_bits != 1 and reg_desc.class == 6:
            *out_cost = 2 * cost          // double cost for wide registers
        return reg_desc.field_12          // register index
```

The pipeline bits extracted from `(reg_desc+48) >> 20` encode the register's pipeline affinity:
- Bits == 1: standard pipeline register
- Bits == 3: double-width register (costs 2 instead of 1)
- Other values: architecture-specific pipeline assignment

When dual-register tracking is active (context flag 0x10, controlled by knob 420), register indices are doubled to provide separate tracking for even/odd register halves.

## Latency Hiding Statistics

The post-scheduling analysis pass (`sub_73B360`, MacLoopSchedulingAnalytics, 28.7 KB) computes and reports latency hiding effectiveness for four categories of long-latency operations:

| Category | String identifier | Stat function | Typical latency |
|---|---|---|---|
| Shared memory loads | `"LDS latency hiding"` | `sub_73A1D0` | 20--30 cycles |
| Global memory loads | `"LDG latency hiding"` | `sub_73A7F0` | 200--500 cycles |
| Extended 64-bit ops | `"Xu64 latency hiding"` | `sub_73ADF0` | 15--30 cycles |
| Anti-dependencies | `"Antidep latency hiding"` | (inline) | varies |

Each category reports: **Num** (count of operations), **Min** (minimum hidden cycles), **Max** (maximum hidden cycles), **Avg** (average hidden cycles). The pass also tracks MAC instruction utilization (`"MacInsts"`, `"MacReuses"`, `"TepidMacUtil"`) and resource busy time (`"LsuResBusy"`, `"Time"`, `"TepidTime"`).

This analysis runs after scheduling is complete and drives feedback for the Mac Loop scheduler, which handles fused multiply-accumulate loop bodies. Knob 443 gates the MAC instruction classification.

## Dual-Issue Rules

Dual-issue scheduling is controlled by `sub_8CF5D0` (CheckDualIssueEligibility, 3.5 KB) and implemented by `sub_8B77C0` (DualIssueScheduler, 15 KB) with pairing logic in `sub_8BDC40` (7.9 KB).

### Eligibility Check

`sub_8CF5D0` returns 0 (no dual-issue) if:
- The target architecture does not support dual-issue (`sub_7DC0E0` returns false).
- Function flag bit 2 at `func+1368` is set (incompatible function).

When eligible, the function iterates basic blocks checking instruction pairs:
- `sub_A9CDE0(instr)`: returns true if instruction is dual-issuable (hot = global/texture).
- `sub_A9CF90(instr)`: returns true if instruction can pair with the next (cold = constant/shared).

The dual-issue benefit score is stored at `scheduler+328` and used by the priority function to bias toward instruction pairs that can co-issue.

### Dual-Issue Constraints

Dual-issue pairs must satisfy:
1. **Pipe compatibility**: the two instructions must target different functional units (e.g., ALU + FP32, or ALU + load/store). Same-pipe pairs cannot dual-issue.
2. **Register conflict**: the pair must not have RAW dependencies on the same register within the same cycle.
3. **Barrier compatibility**: neither instruction may be waiting on a scoreboard barrier.
4. **Architecture support**: dual-issue is primarily an sm\_50 (Maxwell) feature. Newer architectures (sm\_70+) use wider warp schedulers instead.

For sm\_50, a special register budget function adjusts the register allocation target to account for the reduced register pressure from dual-issue execution.

## Stall Count Computation

The stall count determines how many cycles an instruction must wait before it can issue. Stalls are computed by `sub_8D3E20` (2.1 KB) and encoded by `sub_8F3130` (1.0 KB).

### Stall Encoding in Control Words

Each SASS instruction carries a stall count in its control word:
- Maximum stall: 16 cycles (capped by knobs 805 and 806).
- Minimum stall: 1 cycle (no zero-stall encoding exists).
- Default stall when no dependency: determined by the HW profile's pipeline depth.

The stall/barrier encoding pipeline (`sub_8D7760`, 41 KB) computes stalls by walking the dependency DAG backward from each instruction:

```
function ComputeStallCycles(sched, instr):
    max_wait = 0
    for each predecessor of instr:
        distance = instr.cycle - pred.cycle
        latency = LookupLatency(sched, pred, instr)
        wait = latency - distance
        max_wait = max(max_wait, wait)
    return min(max_wait, MaxStallFromKnob(sched))
```

The encoding function `sub_8F4140` packs the complete control word:

| Field | Encoder | Bits | Range |
|---|---|---|---|
| Stall count | `sub_8F3130` | 4 | 1--16 cycles |
| Yield hint | `sub_8F3650` | 1 | 0/1 |
| Read barrier | `sub_8F31F0` | 6 | 0--5 (barrier ID) |
| Write barrier | `sub_8F31F0` | 6 | 0--5 (barrier ID) |
| Scoreboard wait | `sub_8F3860` | 6 | barrier wait mask |
| Reuse flags | (separate) | 4 | register reuse hints |

### Sentinel Values

The scheduling system uses several sentinel values:

| Value | Meaning |
|---|---|
| -1 (0xFFFFFFFF) | Unscheduled instruction position |
| 0x1869F (99999) | Infinite latency sentinel |
| 0xFFFFFFFF | Batch window sentinel (DynBatch) |

## Resource Cost Accumulation

`sub_8C67A0` (ComputeResourceCost, 3.7 KB) drives the per-instruction resource accounting. It calls the resource model `sub_A08A00` three times per instruction:

```
function ComputeResourceCost(sched, instr):
    slot = GetResourceSlot(sched, instr)
    slot.bb_entered |= 1

    // Phase 1: Instruction's own execution cost
    sub_A08A00(sched, instr, instr_data, output, slot, mode=1)
    // Accumulate: slot[0..9] += output[0..9]  (SSE _mm_add_epi32)

    // Phase 2: Operand release costs (for last-use operands)
    sub_A08A00(sched, instr, instr_data, output, slot, mode=2)
    // Accumulate delta: slot[10..19] += output[0..9]

    // Phase 3: Combined instruction + BB-level impact
    sub_A08A00(sched, instr, instr_data, output, slot, mode=3)
    // Accumulate pressure into slot[20]
```

The SSE-optimized accumulation uses `_mm_add_epi32` to add 4 resource counters at a time, processing the full 10-element vector in 3 SSE iterations (4 + 4 + 2).

## Cutlass-Specific Scheduling

`sub_8F47E0` detects NVIDIA cutlass GEMM kernels by calling `strstr(function_name, "cutlass")`. When detected, the scheduler activates hand-tuned scheduling parameters for matrix multiplication inner loops. This includes:
- Modified stall counts for the HMMA/WGMMA instruction sequences.
- Adjusted register pressure targets.
- Specific barrier placement patterns for double-buffered shared memory.

This reflects NVIDIA's investment in hand-tuning their cutlass library's scheduling behavior within ptxas itself.

## Execution Pipe Assignment (sub\_13710B0)

`sub_13710B0` (7.1 KB, 1,088 lines decompiled) is the SASS-backend execution pipe class assigner. It runs in the SASS encoding pipeline (address range 0x1370--0x139F) *after* instruction selection, register allocation, and the main scheduling pass are complete. Where `sub_89FBA0` assigns IR-level scheduling class IDs (2--772+) consumed by the priority and stall-computation passes, `sub_13710B0` writes SASS-level pipe class IDs (0x00--0x141) that control control-word encoding: stall counts, barrier assignments, and dual-issue pairing in the final binary.

### Descriptor Initialization

Before dispatching on the opcode, the function initializes the scheduling descriptor at `a3+196..202` to the "all-pipes" default:

```
*(DWORD*)(a3+196) |= 0xF8000     // pipe mask = all (bits 15..19)
*(BYTE*)(a3+200)  |= 0x1F        // read barrier mask = all
*(WORD*)(a3+198)   = HIWORD | 0x1F0  // throughput class = max
*(WORD*)(a3+200)  |= 0x3E0       // write barrier mask = all
*(BYTE*)(a3+199)   = ... | 0x3E  // pipe flags = all set
```

Then it switches on `*(a2+72) & 0xFFFFCFFF` (the Ori opcode with modifier bits masked), writing a 9-bit pipe class into the low bits of `*(WORD*)(a3+196)` and optionally overriding the pipe mask, sub-class, and pipe flags.

### Pipe Mask Encoding

Bits 15--19 of `*(DWORD*)(a3+196)` select the execution pipe:

| Value | Pipe | Functional units | Resource vector indices |
|---|---|---|---|
| `0x08000` | Pipe A | ALU, integer, FP64, conversion | 0 (ALU), 2 (DFMA) |
| `0x10000` | Pipe B | FP32, tensor, SFU, half-precision | 1 (FMA), 3 (MMA), 8 (SFU) |
| `0x18000` | Pipe C | Memory, texture, wide FP64 | 4 (LSU), 5 (TEX) |
| `0xF8000` | All | Default sentinel (no constraint) | -- |

### Sub-Class Encoding

Bits 4--7 of `*(WORD*)(a3+198)` encode the sub-class within the pipe:

| Value | Sub-class | Instruction category |
|---|---|---|
| `0x10` | Control flow | Branch, predicate, miscellaneous |
| `0x20` | Integer ALU | Conversion, barrier, integer ops |
| `0x30` | FP32 / SFU | Single-precision, half-precision |
| `0x40` | FP64 / Tensor | Double-precision wide, tensor core |

### Pipe Flags Encoding

Bits 1--5 of `*(BYTE*)(a3+199)` encode sub-unit affinity:

| Value | Meaning |
|---|---|
| `0x02` | Narrow ALU sub-unit |
| `0x04` | ALU (integer / conversion) |
| `0x06` | Load/store or wide ALU |
| `0x08` | SFU / half-precision pipe |
| `0x0A` | FP64 wide (double-precision) |
| `0x0C` | Tensor core pipe |
| `0x3E` | All flags set (default) |

### Opcode-to-Pipe-Class Mapping

The complete switch covers 80+ Ori opcodes. Representative mappings:

| Ori opcode | Pipe class | Pipe | Sub-class | SASS instruction | Decision logic |
|---|---|---|---|---|---|
| 1 | 0x08 | -- | 0x10 | IMAD | Always |
| 2--7 (wide) | 0x03 | B (0x10000) | 0x30 | IMAD\_WIDE, IADD3, etc. | `sub_7D6780` = true |
| 2--7 (wide, v6=6) | 0x03 | C (0x18000) | 0x40 | LOP3 (wide, FP64) | Opcode 6, wide |
| 2--7 (narrow) | 0x0C | A (0x08000) | -- | IMAD, IADD3, etc. | Narrow, type != 19 |
| 2--7 (narrow, t=19) | 0x7B | -- | -- | IMAD (BF16/FP8 type) | Type 19 path |
| 8 (flag clear) | 0x33 | -- | -- | IABS (no guard) | Operand flag bit 0 |
| 8 (flag set) | 0x34 | -- | -- | IABS (guarded) | Operand flag bit 0 |
| 0x10 (flagged) | 0x68 | -- | -- | ATOM (flagged) | Operand bit 2 |
| 0x10 (mem=3) | 0x67 | -- | -- | ATOM (shared) | `sub_7DFFC0` = 3 |
| 0x10 (mem=4) | 0x69 | -- | -- | ATOM (constant) | `sub_7DFFC0` = 4 |
| 0x10 (other) | 0x66 | -- | -- | ATOM (global) | Default |
| 0x12 (no 0x400) | 0x3D | -- | -- | FADD (standard) | Operand bit 10 clear |
| 0x12 (0x400 set) | 0x78 | -- | -- | FADD (const-bank) | Operand bit 10 set |
| 0x17 (op1 reg6) | 0x37 | -- | -- | S2R (tensor reg, op1) | `*(desc+64)` = 6 |
| 0x17 (op2 reg6) | 0x36 | -- | -- | S2R (tensor reg, op2) | `*(desc+64)` = 6 |
| 0x17 (other) | 0x38 | -- | -- | S2R (standard) | Neither operand reg6 |
| 0x18 | 0x04 | A (0x08000) | 0x20 | FSETP | Always |
| 0x24 (wide) | 0x14 | B (0x10000) | 0x30 | PRMT (FP width) | `sub_7D6780` = true |
| 0x24 (narrow) | 0x11 | B (0x10000) | 0x30 | PRMT (integer) | `sub_7D6780` = false |
| 0x33 | 0x21 | A (0x08000) | 0x20 | IDP | Always; flags 0x06 |
| 0x3C (mem ops) | 0x2B--0x32 | -- | -- | STG variants | 6-way split on flags |
| 0x3E (mem ops) | 0x2D--0x2E | -- | -- | LDL variants | Flag / no-flag split |
| 0x42 | 0x5D | -- | -- | MUFU (SFU) | Always |
| 0x4D | 0x84--0x85 | B (0x10000) | 0x40 | WGMMA-class | Extended tensor fields |
| 0x4E (mem ops) | 0x2F--0x30 | -- | -- | LD (generic) | Flag / no-flag split |
| 0x66 | 0x09 | B (0x10000) | 0x30 | DEPBAR | Always; flags 0x08 |
| 0x82 / 130 (ext) | 0x17 | -- | -- | NANOTRAP (extended); `HSET2` in ROT13 | `sub_A9AB10` = true |
| 0x82 / 130 (ctrl) | 0x13 | all (0xF8000) | 0x10 | NANOTRAP (control); `HSET2` in ROT13 | vtable+640 |
| 0xC9--0xCA (wide) | 0x07 | A (0x08000) | -- | DFMA, DADD (wide) | `sub_7D6780` = true |
| 0xD1 | 0x05 | A (0x08000) | 0x20 | DFMA | Always |
| 0xD2 | 0x0A | A (0x08000) | 0x30 | DFMA variant | Sub-class 0x30, flag 0x04 |
| 0xF0 | 0x0F | A (0x08000) | -- | F2F | Flags 0x04 |
| 0x10E | 0x7E | B (0x10000) | -- | HMMA\_16 | Flags 0x08 |
| 0x117 | 0x80 | B (0x10000) | 0x40 | HMMA\_32 | Tensor pipe; flags 0x0C |
| 0x11A | 0x81 | B (0x10000) | 0x40 | IMMA | Tensor pipe |
| default | 0x88 | -- | -- | (unrecognized) | Sentinel |

### Decision Axes

The function dispatches on three axes beyond the opcode:

1. **Data type width**: `sub_7D6780(*(a2+76))` returns true for wide types (FP64). Wide types route to pipe A or C with sub-class 0x30 or 0x40; narrow types route to pipe A with sub-class 0x20.

2. **Memory access classification**: `sub_7DFFC0(a2, code_obj)` returns a memory space code (3 = shared, 4 = constant). Used for ATOM (case 0x10) to split into 4 pipe classes by memory space.

3. **Operand register class**: `*(descriptor+64)` from the register descriptor. Class 6 (tensor/accumulator register file) triggers distinct pipe classes for S2R (case 0x17) and DFMA/DADD variants.

Additionally, two architectural gates control tensor instruction classes:
- `*(a1+25)` flag and `sub_1370F40` gate tensor-extended pipe classes. When disabled, tensor instructions fall through to class 0x141 (a sentinel).
- `vtable+3184` on the code object checks a feature gate for CALL instruction classification.

### Memory Instruction Pipe Variants

Load/store instructions (cases 0x3C, 0x3E, 0x4E) receive a 6-way pipe class split based on two properties:

| Property | Test method |
|---|---|
| Same-source vs different-source | `sub_91E7A0(a2, 0)` vs `sub_91E7A0(a2, 1)` |
| Has flag operand | `sub_91E860(code_obj, a2, i)` returns 8 |

| Variant | STG (0x3C) | LDL (0x3E) | LD (0x4E) |
|---|---|---|---|
| Same-src, no flag | 0x31 | (n/a) | (n/a) |
| Same-src, flagged | 0x32 | (n/a) | (n/a) |
| Diff-src, no flag | 0x2B | 0x2D | 0x2F |
| Diff-src, flagged | 0x2C | 0x2E | 0x30 |

This fine-grained split allows the SASS encoder to select different stall counts and barrier patterns depending on whether the load/store has a predicate guard and whether the source address register is shared with another operand.

### Type-19 Special Path

When `sub_7D6780` returns false (not wide) and `*(a2+76) == 19`, several instruction groups receive distinct pipe classes in the 0x7A--0x7D range:

| Ori opcode group | Standard class | Type-19 class | Likely type |
|---|---|---|---|
| 2--7 (narrow) | 0x0C | 0x7B | BF16 / FP8 |
| 0x6E--0x72 (narrow) | 0x0B | 0x7A | BF16 / FP8 |
| 0x8B--0x8C (narrow) | 0x0D | 0x7C | BF16 / FP8 |
| 0xC9--0xCA | 0x10/0x12 | 0x7D | BF16 / FP8 |

Type 19 likely corresponds to BF16 or FP8, which require different pipeline routing than standard FP16/FP32/FP64 types on Hopper and Blackwell architectures.

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_693BC0` | 22 lines | MemorySpaceClassify -- return memory space code | HIGH |
| `sub_695530` | 606 lines | ComputeLatencies -- per-BB latency computation | HIGH |
| `sub_704D30` | 14 KB | GetFunctionalUnit -- SASS opcode to FU mapping | HIGH |
| `sub_73A1D0` | ~6 KB | LDSLatencyStats -- shared memory latency stats | HIGH |
| `sub_73A7F0` | ~6 KB | LDGLatencyStats -- global memory latency stats | HIGH |
| `sub_73ADF0` | 6.5 KB | XU64LatencyStats -- extended unit latency stats | HIGH |
| `sub_73B360` | 28.7 KB | MacLoopSchedulingAnalytics -- latency hiding report | HIGH |
| `sub_799860` | 2.9 KB | ClassifyInstructionLatency | HIGH |
| `sub_89FBA0` | 85 KB | **SetOpcodeLatencies** -- per-opcode scheduling class | HIGH |
| `sub_8B5400` | 14 KB | ScheduleForLatency -- latency-optimized scheduling | MEDIUM |
| `sub_8B77C0` | 15 KB | DualIssueScheduler -- dual-issue scheduling engine | MEDIUM |
| `sub_8BDC40` | 7.9 KB | DualIssuePairing -- instruction pair selection | MEDIUM |
| `sub_8C67A0` | 3.7 KB | ComputeResourceCost -- per-instruction FU cost | HIGH |
| `sub_8C7290` | 5.1 KB | GetResourceVector -- SSE-optimized copy | HIGH |
| `sub_8CCF80` | 2.3 KB | IsLongLatencyOp -- latency > 19 check | HIGH |
| `sub_8CF5D0` | 3.5 KB | CheckDualIssueEligibility | HIGH |
| `sub_8D3E20` | 2.1 KB | ComputeStallCycles -- required stall count | HIGH |
| `sub_8D7760` | 41 KB | StallAndBarrierInsertion -- encode stalls/barriers | HIGH |
| `sub_8E3AD0` | -- | CopyProfileEntries -- finalize HW table | MEDIUM |
| `sub_8E4400` | 3.3 KB | **InitHWProfile\_Warp** -- warp dispatch params | HIGH |
| `sub_8E4920` | 6.9 KB | BuildScoreboardEntries -- scoreboard BST | HIGH |
| `sub_8E4D80` | 15 lines | StringRefCleanup -- decref string in record copy | HIGH |
| `sub_8E4F20` | ~1.5 KB | EmitWeightEntry -- supplementary weight record (type 'W') | HIGH |
| `sub_8E5310` | ~1.5 KB | EmitVariantSection -- variant sub-records (type ';') | HIGH |
| `sub_8E5530` | ~1.5 KB | EmitDimensionEntries -- dimension sub-records (type '1') | HIGH |
| `sub_8E5CA0` | 20 KB | **EmitScheduleOutput** -- scheduling control words | HIGH |
| `sub_8E6760` | 2.9 KB | EmitGroupBoundary -- group boundary marker | HIGH |
| `sub_8E6B40` | 2.9 KB | EmitSchedEntry -- standard scheduling entry | HIGH |
| `sub_8E6D40` | 2.9 KB | EmitBarrierEntry -- barrier/sync entry | HIGH |
| `sub_8E6F20` | 2.9 KB | EmitWaitEntry -- wait dependency entry | HIGH |
| `sub_8E7110` | 2.9 KB | EmitScoreboardEntry -- scoreboard entry | HIGH |
| `sub_8E7300` | 3.3 KB | HWTable\_sm70 -- Volta latency table | CERTAIN |
| `sub_8E7540` | 2.9 KB | HWTable\_sm72 -- Xavier latency table | CERTAIN |
| `sub_8E7720` | 3.5 KB | HWTable\_sm75 -- Turing latency table | CERTAIN |
| `sub_8E7940` | 2.9 KB | HWTable\_sm80\_base -- Ampere base table | CERTAIN |
| `sub_8E7B40` | 3.3 KB | HWTable\_sm80 -- Ampere full table | CERTAIN |
| `sub_8E7D80` | 4.4 KB | HWTable\_sm86 -- GA10x table | CERTAIN |
| `sub_8E8070` | 3.5 KB | HWTable\_sm87 -- Orin table | CERTAIN |
| `sub_8E8280` | 3.1 KB | HWTable\_sm89 -- Ada Lovelace table | CERTAIN |
| `sub_8E8480` | 5.2 KB | HWTable\_sm90 -- Hopper table | CERTAIN |
| `sub_8E8780` | 4.6 KB | HWTable\_sm90a -- Hopper accelerated table | CERTAIN |
| `sub_8E8A90` | 3.0 KB | HWTable\_sm100 -- Blackwell DC table | CERTAIN |
| `sub_8E8CB0` | 949 B | HWTable\_sm100\_short -- Blackwell supplementary | CERTAIN |
| `sub_8E8DB0` | 1.7 KB | HWTable\_sm103 -- Blackwell Ultra table | CERTAIN |
| `sub_8E8F60` | 618 B | HWTable\_sm103\_short -- BU supplementary | CERTAIN |
| `sub_8E9000` | 2.9 KB | HWTable\_sm120 -- RTX 50xx table | CERTAIN |
| `sub_8E92E0` | 5.5 KB | HWTable\_sm120\_ext -- RTX 50xx extended | CERTAIN |
| `sub_8E97B0` | 8.8 KB | HWTable\_universal -- fallback table | CERTAIN |
| `sub_8E9DC0` | 4.8 KB | EmitLatencyEntry -- HW table entry helper | HIGH |
| `sub_8EFA10` | 18 KB | EmitScheduleReport -- statistics output | HIGH |
| `sub_8F0CD0` | 24 B | MapFUClassID -- (opcode, name) to class | HIGH |
| `sub_8F1EB0` | 15 KB | EncodeScheduleWords -- SASS control word output | HIGH |
| `sub_8F3130` | 1.0 KB | EncodeStallField | HIGH |
| `sub_8F31F0` | 6.1 KB | EncodeBarrierField | HIGH |
| `sub_8F3650` | 2.7 KB | EncodeYieldField | HIGH |
| `sub_8F3860` | 3.0 KB | EncodeScoreboardField | HIGH |
| `sub_8F4140` | 5.6 KB | EncodeFullControlWord | HIGH |
| `sub_8F47E0` | ~50 B | DetectCutlass -- strstr for "cutlass" | CERTAIN |
| `sub_A08910` | 39 lines | GetRegisterLatency -- operand cost query | HIGH |
| `sub_A08A00` | 345 lines | ResourceModel -- 3-mode FU cost computation | HIGH |
| `sub_A09530` | 91 lines | UpdateStallCycles -- per-instruction stall update | HIGH |
| `sub_A9CDE0` | -- | IsHotMemory -- global/texture classification | HIGH |
| `sub_A9CF90` | -- | IsColdMemory -- constant/shared classification | HIGH |
| `sub_13710B0` | 7.1 KB | **AssignPipeClass** -- SASS-level pipe assignment | HIGH |
| `sub_1370F40` | ~500 B | CheckTensorFeature -- gates tensor pipe classes | HIGH |
| `sub_7D6780` | ~100 B | IsWideType -- true for FP64/wide types | HIGH |
| `sub_7DFFC0` | ~200 B | ClassifyMemAccess -- 3=shared, 4=constant | HIGH |
| `sub_7E3640` | ~100 B | GetCustomPipe -- 5-bit pipe sub-class | MEDIUM |
| `sub_91E7A0` | ~100 B | GetSrcEncoding -- source operand encoding query | MEDIUM |
| `sub_91E860` | ~100 B | GetOperandType -- operand type code | MEDIUM |
| `sub_A9AB10` | ~100 B | NeedsExtEncoding -- extended encoding check | MEDIUM |

## Cross-References

- [Scheduler Overview](overview.md) -- 3-phase architecture, HW profile table summary
- [Scheduling Algorithm](algorithm.md) -- priority list scheduling, resource vector usage
- [Scoreboards & Barriers](scoreboards.md) -- scoreboard encoding, dependency barriers
- [SASS Encoding](../codegen/encoding.md) -- control word format in SASS binary
- [Targets Index](../targets/index.md) -- SM architecture map and version codes
- [Knobs](../config/knobs.md) -- scheduling knobs (740, 741, 805, 806, etc.)
