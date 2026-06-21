# Latency Model & Functional Units

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas instruction scheduler uses a static hardware performance model to estimate instruction latencies, functional unit occupancy, and pipeline conflicts. The model is architecture-parameterized: a static per-class descriptor table in `.rodata` (at `0x2297C00`) seeds it, and a family of table-construction helpers at `0x8E7300`–`0x8E9DC0` (generic grow-and-copy record appenders, fed per-architecture class data by their callers) assemble the runtime tables consumed by the scheduling engine. A separate 85 KB function (`sub_89FBA0`, SetOpcodeLatencies) assigns per-opcode scheduling classes that index into these tables. The combination produces a cost model that drives stall-count computation, priority scoring, and dual-issue pairing decisions.

| | |
|---|---|
| **Per-opcode classifier** | `sub_89FBA0` (85 KB) — assigns scheduling class per Ori opcode |
| **HW profile builder** | `sub_8E5CA0` (20 KB) — assembles scheduling control word tables |
| **Warp profile** | `sub_8E4400` (3.3 KB) — maps SM ID to warp/dispatch parameters |
| **Table-construction helpers** | `sub_8E7300`--`sub_8E97B0` — generic record appenders (caller supplies per-arch data) |
| **Latency query** | `sub_693BC0` (22 lines) — memory space classification |
| **Long-latency check** | `sub_8CCF80` (2.3 KB) — returns true if latency > 19 |
| **Resource model** | `sub_A08A00` (345 lines) — per-instruction FU cost computation |
| **Register query** | `sub_A08910` (39 lines) — operand register count/cost |
| **Stall update** | `sub_A09530` (91 lines) — per-instruction stall cycle accumulation |
| **FU class mapper** | `sub_8F0CD0` — maps (opcode, unit_name) to scheduling class |
| **FU unit query** | `sub_704D30` (14 KB) — maps SASS opcodes to functional unit IDs |
| **Cutlass detector** | `sub_8F47E0` — detects cutlass kernels for tuned scheduling |
| **Pipe class assigner** | `sub_13710B0` (7.1 KB) — SASS-level execution pipe assignment |

## Architecture of the Latency Model

The model has three layers:

```text
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

`sub_89FBA0` (85 KB, 2938 lines decompiled) is the largest function in the scheduling subsystem. It assigns each instruction a scheduling class — an integer that indexes into the per-architecture latency tables. The function operates as a massive switch on `*(instr+72) & 0xFFFFCFFF` (the Ori opcode with modifier bits masked out).

### Scheduling Descriptor Layout

Each instruction carries a scheduling descriptor at offsets 196–200 within the 296-byte Ori instruction object (not the SchedNode). The descriptor is a packed bit-field:

```text
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
| 1 | 0x01 | IMAD | 130 | — | Sub-class 0x10 (control flow path) |
| 2 | 0x02 | IMAD\_WIDE | 72 | 683 | FP-width: 131 (sub-class 0x20) |
| 3 | 0x03 | IADD3 | 72 | 683 | FP-width 3,5: 140 (sub-class 0x30) |
| 4 | 0x04 | BMSK | 72 | 683 | FP-width 4: 131 |
| 5 | 0x05 | SGXT | 72 | 683 | FP-width: 140 |
| 6 | 0x06 | LOP3 | 72 | 683 | Wide opcode 6: 140, pipe C (0x18000) |
| 7 | 0x07 | ISETP | 72 | 683 | type=19: class 52 |
| 8 | 0x08 | IABS | 2/3 | — | Flag set: 3, flag clear: 2 |
| 10 | 0x0A | SHF | 200 | 551 | Mercury: 694/700; grouped with 0xB,0x6C,0x95 |
| 14 | 0x0E | FMNMX | 5 | — | Conversion class |
| 33 | 0x21 | IDP | 75 | — | Integer dot product |
| 35 | 0x23 | I2I | 45 | — | Sub-class 0x20, pipe flag 0x06 |
| 37 | 0x25 | IMNMX | 143 | — | Sub-class 0x20, pipe flag 0x06 |
| 38 | 0x26 | POPC | 533 | — | Grouped with FLO,BREV,CCTL,etc. |
| 39 | 0x27 | FLO | 188 | — | Conditional on operand bits |
| 53 | 0x35 | BREV | 547 | — | Also I2IP (0x37) |

**FP32 / Half-precision / Miscellaneous ALU (pipe 1)**

| Ori | Hex | SASS mnemonic | Class | Wide class | Notes |
|---|---|---|---|---|---|
| 17 | 0x11 | FSEL | 10 | — | Simple class |
| 18 | 0x12 | FSETP | 11/12 | 544/769 | 4-way split on operand bits |
| 19 | 0x13 | MOV | (default) | — | No dedicated case; falls to `sub_A2D340` |
| 20 | 0x14 | SEL | (default) | — | No dedicated case; dynamic model |
| 23 | 0x17 | S2R | 20 | — | Grouped with 0x39 (BAR), 0x65 (GETLMEMBASE) |
| 24 | 0x18 | PRMT | 21 | 641 | Wide: 641 (Mercury); sub-class 0x20 |
| 26 | 0x1A | VOTE | 22 | — | — |
| 27 | 0x1B | CS2R\_32 | 27 | — | — |
| 28 | 0x1C | CS2R\_64 | 28 | 642 | Mercury: 642 |
| 32 | 0x20 | VABSDIFF4 | 33 | — | Also case 0x10F |
| 36 | 0x24 | I2F/PRMT | 96 | 586/707 | Wide: 586; Mercury: 707 |
| 40 | 0x28 | FCHK | 54 | — | Also FRND (0x2F) |
| 42 | 0x2A | MUFU | 548 | — | SFU pipe at HW level (quarter rate) |
| 58 | 0x3A | BAR | 64 | — | Barrier synchronization |
| 81 | 0x51 | NANOSLEEP | 142 | — | — |
| 119 | 0x77 | SHFL | 202 | — | Warp shuffle |
| 240 | 0xF0 | F2F | 99 | — | Pipe A (0x08000), flag 0x04 |
| 241 | 0xF1 | (sm82 FP) | 32 | — | — |

**FP64 (pipe 2)**

| Ori | Hex | SASS mnemonic | Class | Wide class | Notes |
|---|---|---|---|---|---|
| 122 | 0x7A | DFMA | 619 | — | Latency idx 0xE8 |
| 123 | 0x7B | DADD | 131 | — | Latency idx 0x03 |
| 124 | 0x7C | DMUL | 203 | — | — |
| 125 | 0x7D | DSETP | 190 | — | — |
| 201 | 0xC9 | DFMA (sm82+) | 97/98 | 175/688 | Wide: 175; Mercury: 688 |
| 209 | 0xD1 | (sm82 DP) | 592 | 713 | Mercury: 713 |
| 210 | 0xD2 | (sm82 DP) | 593 | 714 | Sub-class 0x30, flag 0x04 |
| 211 | 0xD3 | (sm82 DP) | 594 | — | — |

**Memory load/store (pipe 4, full rate)**

| Ori | Hex | SASS mnemonic | Class | Notes |
|---|---|---|---|---|
| 16 | 0x10 | ATOM | 7 | Flagged: 575; shared: 8; const: 9 |
| 60 | 0x3C | STG | 67/95 | 6-way split on same-src/flag/remat |
| 62 | 0x3E | LDL | 69/70 | Flag vs no-flag split |
| 63 | 0x3F | CALL | 71 | Sub-class 0x20, pipe flag 0x06 |
| 78 | 0x4E | LD (generic) | 125/126/127 | 3-way: remat/flag/no-flag |
| 89 | 0x59 | LDC | 170 | — |
| 94 | 0x5E | LDS | 29/30 | Reg-class-3 split |
| 95 | 0x5F | STS | 25 | Grouped with 0x5D |
| 96 | 0x60 | LDG | 183 | — |
| 98 | 0x62 | LDL (ext) | 526 | Mercury: 697 |
| 100 | 0x64 | LD (ext) | 648 | — |
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
| 270 | 0x10E | HMMA\_16 | 103/104 | — | Wide: 758, sub-class 0x40/0xC (idx 0xF7) |
| 279 | 0x117 | HMMA\_32 | 106/107 | — | Operand bit split |
| 280 | 0x118 | HMMA pair | 120/760 | 0xF9 | Wide: 760; type=6: 88 |
| 282 | 0x11A | IMMA | 121 | — | Sub-class 0x40, pipe B |
| 321 | 0x141 | WGMMA | 745 | 0xF1 | Warpgroup MMA |
| 322 | 0x142 | WGMMA (v3) | 744 | 0xF0 | Others fall to class 203 |
| 323 | 0x143 | BGMMA/QMMA | 765–767 | 0xFB | 3-way operand split |
| 324 | 0x144 | Tensor fence | 600 | 0xE6 | — |
| 325 | 0x145 | HMMA/BMMA | 759 | 0xF8 | Sub-class 0x40, pipe flag 0x0C |
| 327 | 0x147 | DP tensor | 757/761 | 0xF6/0xFA | Wide: 761; narrow: 757 |
| 329 | 0x149 | Tensor sync | 604 | 0xE7 | — |

**Collective / bulk operations (sm90+)**

| Ori | Hex | SASS mnemonic | Class | Latency idx | Notes |
|---|---|---|---|---|---|
| 317 | 0x13D | Collective | 747/750 | 0xF2/0xF5 | Variant-dependent |
| 318 | 0x13E | ACQBULK | 749 | 0xF4 | Bulk copy |
| 319 | 0x13F | RELBULK | 748 | 0xF3 | Bulk release |

The scheduling class IDs span a wide range (0–772+). Classes below 256 correspond to legacy instruction categories present since Volta; higher classes (526–769) represent newer types added for Ampere, Hopper, and Blackwell. Class 772 is a sentinel that triggers a fallback to the dynamic cost model via `sub_A2D340`.

### Latency Index Encoding

The low 9 bits of the descriptor at `a3+196` encode a latency index that maps directly into the per-architecture HW table. The index is formed by combining the descriptor's low byte with a pipe mask:

```c
latency_index = *(WORD*)(a3+196) & 0x1FF
```

Observed latency index values and their instruction classes. The table below covers both common instructions (low indices, where the index equals the scheduling class ID) and the tensor/collective range (0xE6–0xFB, assigned via explicit latency-index overrides in `sub_89FBA0`). The "model latency" column gives the scheduling cost from `per_sm_dependency_rules`; the "throughput^-1" column is the minimum issue interval between successive instructions of the same class on a single SM sub-partition.

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
| 16–19 | LSU | Global load variants | 21–23 / 20–23 / 21–56 | 19 | LDG.E, LDG.128 |
| 20 | ALU | Shared memory simple | 28 / 28 / 28 | 5 | LDS, STS |
| 21 | LSU | Constant memory | 22 / 22 / 22 | 2 | LDC |
| 35 | XU | Extended unit (fallback) | 52 / 52 / 52 | 21 | PRMT, BFE, BFI |
| 52 | LSU | Integer compare (narrow) | 13 / 48 / 46 | 19 | ISETP (narrow path) |
| 56–59 | SFU | Special function unit | 45–47 / 47 / 41–45 | 19 | MUFU (RCP, RSQ, SIN, COS) |
| 60–61 | LSU | Shared memory atomic | 13–48 / 48 / 46 | 19 | ATOMS, REDS |
| 66–67 | DFMA | FP64 multiply/FMA | 72 / 48–74 / 72 | 34 / 19–33 / 34 | DFMA, DMUL |
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
| 0xFB | 251 | 765–767 | 52 | BGMMA/QMMA |

For common instruction classes, the latency index stored at `a3+196` equals the scheduling class ID directly — no remapping is needed. Only the tensor/collective classes (0xE6–0xFB) use explicit latency-index overrides that differ from their scheduling class ID. The model latency value of 17 for ALU classes with throughput 0 corresponds to the lowest-latency fully-pipelined path (4-cycle register-to-register, encoded as cost 17 in the scheduling cost product). Classes with model latency 22 and throughput 2 represent the "default" short-latency profile used for most single-cycle pipe instructions. The jump from 42–52 to 65–72 marks the boundary between standard functional units and long-latency tensor/FP64 operations that require scoreboard barriers.

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

```text
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
| default | — | 35 | Fallback to extended unit |

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

```text
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

Each record in the HW table occupies 96 bytes (6 x 16-byte XMM slots). Records are stored in a growable array at `*(context+56)` with count at `*(context+64)` and capacity at `*(context+68)`. The array grows by 1.5x when full. Records are copied using three `_mm_loadu_si128` operations (offsets 0, 16, 32) plus manual field-by-field copy for offsets 48–95; the string at +48 is reference-cloned via `sub_714160` when the string-backed flag is set.

```text
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

Records are polymorphic — the type code at offset +0 selects the interpretation of fields +16..+31, +32..+47, and the sub-record format stored in the growable buffer at +80.

| Type | ASCII | Creator | Role |
|---|---|---|---|
| 1 | — | `sub_8E5CA0` | Root container (wraps entire HW table) |
| 23 | — | `sub_8E6B40` | Standard scheduling entry (latency + throughput + dual-issue) |
| 33 | `'!'` | `sub_8E5740` | Category header (begins a named section with string list) |
| 44 | `','` | `sub_8E8480` (generic appender) | Class-data table entry — caller supplies the per-architecture content |
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

Scheduling class 2 (predicate operations with flag clear — PSETP, PLOP3) provides a clean example because it uses the simplest record type (23, standard entry) and its pipe assignment is unambiguous. Below we decode both the 72-byte HW latency table entry from `sm_8x_shared` and its matching 40-byte dependency rule from the `sm_80` table.

**72-byte class descriptor** (at `0x2297C00 + 0 * 72 = 0x2297C00`). The exact on-disk
layout — verified by extracting all 256 records from `ptxas_rodata.bin` — is a DWORD
class id, a DWORD pad, two 8-byte pipe-mask vectors, then twelve DWORD params (total
`4 + 4 + 8 + 8 + 12·4 = 72`):

```text
Offset  Bytes                       Field           Decoded
------  -----                       -----           -------
 0..3   02 00 00 00                 class_id        2 (predicate ops, flag-clear)
 4..7   00 00 00 00                 reserved        0
 8..15  03 03 FF FF FF FF FF FF     pipe_masks_a    pipes 0+1 = 0x03 eligible; pipes 2–7 = 0xFF (N/A)
16..23  00 00 FF FF FF FF 00 00     pipe_masks_b    dual-issue eligibility vector
24..71  12 x DWORD                  sched_params    p0=0   (flags)
                                                    p1=4   throughput class (1/4 rate)
                                                    p2=0
                                                    p3=1
                                                    p4=0
                                                    p5=7   max stall cycles
                                                    p6=3
                                                    p7=2   class id (self-reference, == +0)
                                                    p8=1
                                                    p9=2
                                                    p10=3
                                                    p11=0
```

The `pipe_masks_a` value of `[3,3,0xFF,...]` means the instruction can issue on pipe 0 or pipe 1 (bitmask `0x03` = bits 0+1 set); pipes 2–7 carry `0xFF` = "not applicable." `pipe_masks_b` is the dual-issue eligibility vector. Of the twelve params, only three are named with confidence across the whole 256-entry table: **p1** is the throughput class (observed values `{0,1,2,4,132}`), **p5** is the max-stall cycle count (`{0..7}`, dominated by 3 and 7), and **p7** is the class id repeated as a self-reference (it equals `class_id` at +0 for every record). The remaining params are emitted as observed columns; their exact semantics are not asserted. The full 256-class dump (id 2..771) is in the repo at `decoded/ptxas-scheduling/sched_class_table.txt`.

**40-byte dependency rule** (from `sm_80` table, entry 0, matching unit\_id 2):

```text
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

```text
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

The `cost_product` at offset +16 is the scheduler's primary sorting key: higher values push the instruction later in the schedule. For this predicate class, 68 is a low cost — compare with FP64 ops (class 4, latency=42, throughput=15) where `cost_product = 42 * 15 = 630`, nearly 10x higher, reflecting the FP64 pipe's lower throughput.

#### End-to-End Latency Lookup Path

The complete path from "I have an instruction" to "its latency in cycles on this SM" involves four pointer dereferences and one vtable dispatch. The following pseudocode traces the exact chain, with byte offsets matching the binary.

```c
// resolve_latency(instr, sched_ctx) -> int
//   instr:     296-byte Ori instruction object
//   sched_ctx: scheduling context (carries SM backend + oracle)
//
// Step 1: Extract the scheduling class ID.
//   sub_89FBA0 already ran during instruction lowering and wrote
//   the class ID into the scheduling descriptor attached to instr.
//   The descriptor is an auxiliary object whose pointer lives at
//   instr+40; the class_id sits at descriptor+4.
//
//   descriptor = *(QWORD *)(instr + 40)       // SchedNode.desc_ptr
//   class_id   = *(DWORD *)(descriptor + 4)   // range 1..772+
//
// Step 2: Locate the per-SM HW latency table.
//   The SM backend is reachable from the scheduling context:
//
//   sm_backend = *(QWORD *)(*(QWORD *)(sched_ctx + 8) + 1584)
//   hw_table   = *(QWORD *)(sm_backend + 56)  // growable array base
//   table_count= *(DWORD *)(sm_backend + 64)   // number of 96-byte records
//
// Step 3: Index into the HW table to find the record.
//   Records are 96 bytes each. The scheduling class ID maps to an
//   index via the 9-bit latency_index stored at instr+196:
//
//   latency_idx = *(WORD *)(instr + 196) & 0x1FF
//   record      = hw_table + 96 * latency_idx  // 96-byte record pointer
//
//   Alternatively, when queried through the oracle vtable, the
//   record's base_latency is read directly:
//
//   base_latency = *(WORD *)(record + 20)       // cycles, from dependency rule
//
// Step 4: Query the latency through the oracle (sub_8BF3A0).
//   The scheduler does not read the record directly -- it dispatches
//   through a vtable method at *(*(oracle)+56).  The default
//   implementation (sub_8BF3A0) follows this priority chain:
//
//   desc = *(QWORD *)(instr + 40)              // scheduling descriptor
//   if *(BYTE *)(desc + 108) & 5 != 0:         // special-case flag set?
//       return *(DWORD *)(oracle + 92)          //   -> use oracle's default latency
//   override = *(INT16 *)(desc + 104)           // per-instruction override
//   if override != 0:
//       return override                         //   -> use the override
//   opcode = *(DWORD *)(instr + 72) & 0xFFFFCFFF
//   return *(DWORD *)(oracle + 4 * opcode + 744)  // -> per-opcode latency table
//
// Step 5 (optional): Long-latency classification (sub_8CCF80).
//   Returns true when the resolved latency exceeds 19 cycles.
//   For load/store (opcode 183): also checks memory space via
//   sub_693BC0; spaces 1,2,3,4,7,11,16 all qualify regardless.
//
//   is_long_latency = (resolve_latency(instr, sched_ctx) > 19)
```

The pointer chain in Step 2 (`sched_ctx+8` -> `+1584` -> `+56`) is the same indirection used by `sub_8CCF80` at its opening line: `v3 = *(*(*(a1+8) + 1584))`. The 96-byte record stride in Step 3 matches the `96LL * v15` expression in `sub_8E7300` line 98 (`v17 = (__m128i *)(v13 + 96LL * v15)`). The per-opcode table at oracle+744 in Step 4 is a 355-entry DWORD array (one slot per Ori opcode), populated during profile construction by the oracle constructor described next.

#### The Per-Opcode Scalar Latency Oracle (`sub_738E20`)

The `oracle+744` array that Step 4 reads is not a static table — it is filled at
construction time by the per-SM-family oracle constructor. `sub_738E20` is the
constructor for the **sm\_9x / Blackwell** family (its object is 4184 bytes). The
final loop of the constructor walks Ori opcodes `0..354` and writes each opcode's
latency into `*(this + 4*opcode + 744)`. The assignment is a plain `switch` on the
opcode id, transcribed from the decompiled body — this is the actual
latency model ptxas's own OCG scheduler consults:

```c
// sub_738E20 tail loop — fills the oracle+744 latency array, opcode 0..354.
// prop = *(config+776)  is the per-opcode property byte vector.
for (int op = 0; op < 355; ++op) {
    uint8_t p = prop[op];
    int lat;
    switch (op) {
        case 16:                                          lat = 300; break; // long global-memory
        case 17: case 42: case 53: case 55:
        case 91: case 183: case 195:                      lat = 24;  break; // SFU / slow-MIO
        case 38: case 59: case 60: case 62: case 67:
        case 78: case 79: case 106: case 162: case 180:
        case 182: case 192: case 194: case 199:
        case 215: case 221:                               lat = 13;  break; // medium / control / MIO
        case 88: case 89:                                 lat = 30;  break; // shared / atomic
        case 223: case 228:                               lat = 300; break; // long memory
        default:
            lat = (p & 0x40) ? oracle->default_mem        // = 300 (oracle+92 seed)
                             : 6;                          // default ALU baseline
    }
    oracle->lat[op] = lat;                                // this + 4*op + 744
    if (p & 0x02)                                          // decoupled / scoreboard-tracked
        oracle->scoreboard[op] = 5;                       // this + 4*op + 2212
}
```

The model collapses to **five bands**:

| Cycles | Band | Opcode ids (index into `oracle+744`) |
|---|---|---|
| 6   | default ALU (non-memory) | every opcode not listed below, property bit `0x40` clear |
| 13  | medium / control / MIO | 38, 59, 60, 62, 67, 78, 79, 106, 162, 180, 182, 192, 194, 199, 215, 221 |
| 24  | SFU / slow-MIO | 17, 42, 53, 55, 91, 183, 195 |
| 30  | shared / atomic | 88, 89 |
| 300 | long global-memory miss | 16, 223, 228, and any opcode with property bit `0x40` set |

The default seed at `oracle+92` is loaded from `xmmword_2029FE0 = {300,0,0,0}`, so an
unclassified *memory* opcode falls through to 300 cycles. Two of these bands are
architecturally-stable fixed points: the **6-cycle ALU baseline** (FXU
register-to-register, unchanged Volta→Hopper) and the **300-cycle global-memory miss**
(the unresolved-space scoreboard wait). The 13 / 24 / 30 bands are ptxas-internal OCG
scalars and need not equal any single hazard-matrix cell.

This five-band scalar oracle is the **fast per-opcode path** the OCG consults directly. It is
*not* the whole model: ptxas also ships a **richer per-class producer/consumer table** — the
40-byte dependency rules described in the next section — keyed by the scheduling class that
`sub_89FBA0` assigns. The two agree on anchors (ALU = 6, long-memory = 300) but the 13 / 24 / 30
oracle bands are OCG-internal scalars and need not equal any single dependency-rule cell. What
does **not** ship in byte-readable form is the build-time scheduling DSL itself (the source
`SM*.latencies_` description); it is compiled into the table-construction native code, so the
recoverable artifacts are the *shipped* tables it emits: this five-band scalar oracle, the 72-byte
sched-class descriptors (next section), and the 40-byte per-SM dependency rules.

The opcode→mnemonic naming must be resolved through the [Ori opcode table](../ir/instructions.md):
the index space here is the Ori opcode id (`*(instr+72) & 0xFFFFCFFF`), **not** the SASS
scheduling-class id (2..771) used by the descriptor table. The extracted oracle is archived in the
repo at `decoded/ptxas-scheduling/scalar_latency_oracle.txt` (band-corrected copy in
`decoded/ptxas-sched-full/scalar_latency_oracle.tsv`).

### The Full Per-SM Scheduling Model (three table families)

Beyond the scalar oracle, ptxas ships three `.rodata` table families that together form the
complete per-SM latency / dependency / scoreboard model. All three are indexed by the scheduling
class (or config index), and cover all 26 SMs the binary supports.

| Table | Record | Indexed by | Variants | Coverage |
|---|---|---|---|---|
| **Latency / sched-class descriptor** | 72 B | scheduling-class id | 3 (one per SM *family*) | sm_7x = 619, sm_8x = 256, sm_10x = 430 classes |
| **Dependency rule** | 40 B | scheduling-class id | 11 (per individual SM) | 10 distinct sets (sm_86 ≡ sm_90, byte-identical) |
| **Scoreboard config** | 88 B | config index | 7 | sm_100 uses ≤ 6 scoreboards; pre-Blackwell use 1 |

The latency descriptor tables are **shared per family**: `0x2297C00` (sm_8x — shared by
sm_80/86/89/90/90a), `0x226C880` (sm_10x — sm_100/103), `0x2245060` (sm_7x — sm_60/70/72/75).
Dependency-rule tables are **per-SM**.

#### Latency / sched-class descriptor (72 B)

```text
class_id (u32) · reserved (u32) · pipeA (8 B) · pipeB (8 B) · p0..p11 (12 × u32)
```

- `pipeA` = per-pipe eligibility byte vector (a `0xFF` byte = pipe N/A for that class); `pipeB` =
  the dual-issue eligibility vector.
- `p1` = throughput class (sm_8x: `{0,1,2,4,132}`; sm_7x / sm_10x add `{64,68,84,…}`).
- `p5` = max-stall cycles `{0..7}` (3 and 7 dominate).
- `p7` = the class id itself — a self-reference that equals `class_id` in **every** record of all
  three families (a structural invariant, verified).
- `p11` = always 0. `p0` is a packed flag word (high bit set in a few rows). `p2,p3,p4,p6,p8,p9,p10`
  are small enums emitted as observed columns; their individual semantics are not asserted.

#### Dependency rule (40 B) — the per-class producer/consumer cells

Ten `i32` fields, verified byte-exact against `.rodata`:

```text
unit_id · rule_type · latency · throughput_inv · barrier_latency · barrier_throughput
        · read_latency · write_latency · stall_cycles · issue_slots
```

| Field | Meaning |
|---|---|
| `rule_type` | `0/1/2` = active; **`4` = disabled / unit-absent** (always paired with `latency = 255`) |
| `latency` | producer→consumer pipeline latency; `255` = not-present sentinel |
| `throughput_inv` | inverse throughput (issue interval); `0` = fully pipelined |
| `barrier_latency` / `barrier_throughput` | dependency-barrier wait / interval for decoupled (scoreboard-tracked) ops; `-1` = none |
| `read_latency` / `write_latency` | explicit **RAW** / **WAW** operand-latency overrides; `-1` (0xFFFFFFFF) = "unset, use `latency`" |
| `stall_cycles` | static stall hint |
| `issue_slots` | dual-issue slot count |

`read_latency` / `write_latency` are populated for a minority of classes (e.g. sm_70 sets
`read_latency` on ~50 classes) and are the binary's explicit per-class RAW/WAW hazard cells — the
shipped equivalent of a producer×consumer matrix, but stored per scheduling class rather than as a
dense N×N grid.

#### Scoreboard config (88 B)

Seven `{scoreboard_id, threshold, mask}` triplets (3 × `i32` each) plus a `count` at `+84`:

- `mask == -1` (0xFFFFFFFF) = all-lanes / unconditional wait; small masks (`2,4,8,32`) =
  pipe-specific. `threshold` is the barrier-count cutoff (56 dominates).
- sm_100 uses up to **6** scoreboards per config (the Blackwell async dependency-barrier model);
  sm_80/86/89/90/90a/103 use a single triplet with a pipe mask. See
  [Scoreboards](scoreboards.md) for how the control-word generator installs these.

#### Per-SM coverage and the sm_90 / sm_90a split

- 3 latency tables cover all 26 SMs by family; 10 distinct dependency-rule sets cover 11 SMs
  (`sm_86` and `sm_90` are **byte-identical**).
- `sm_90a` enables every class (0 disabled); **`sm_90` disables 6 classes** — units
  41, 561, 562, 563, 566, 567, the WGMMA / async-MMA tensor classes (`rule_type = 4`,
  `latency = 255`). This is the entire mechanical difference between the sm_90 and sm_90a targets.
- Disabled-unit counts grow for restricted / older arches: sm_103 = 129, sm_75 = 173, sm_72 = 170,
  sm_70 = 146, sm_60 = 136, sm_80 = 19, sm_100 = 10.

A representative slice — sm_90 vs sm_90a dependency rules, an sm_8x sched-class descriptor sample,
and sm_100 vs sm_90 scoreboard configs — is in
[Per-SM Scheduling Sample](../reference/data/per-sm-scheduling-sample.md). The full per-SM TSVs
are in the repo at `decoded/ptxas-sched-full/`.

#### Opcode → pipeline → class → band (end-to-end)

```text
OriOpcode  --(opcode_pipeline_map)-->        pipeline_flags {0..4}
OriOpcode  --(sub_89FBA0 SetOpcodeLatencies)--> scheduling class_id (2..771)
class_id   --(latency_table_sm{fam})-->      pipe-eligibility + throughput class + max-stall
class_id   --(dependency_rules_<sm>)-->      latency / tput_inv / barrier / RAW(read) / WAW(write)
OriOpcode  --(scalar oracle via sub_738E20)--> flat latency band {6,13,24,30,300}
decoupled? --(oracle property bit 0x02)-->   scoreboard wait seed = 5 + per-SM scoreboard config
```

#### The hazard model (recovered from the OCG scheduler functions)

| Role | Function |
|---|---|
| Per-instruction latency query | `sub_8BF3A0` (reads `oracle+744`) |
| Long-latency predicate (> 19 cyc) | `sub_8CCF80` |
| Memory-space classifier | `sub_693BC0` |
| Resource / scoreboard occupancy | `sub_A08A00` (3 modes: 1 = issue, 2 = commit, 3 = revert) |
| Per-operand register cost | `sub_A08910` |
| Stall-cycle accumulation | `sub_A09530` (node+12, 9-bit `& 0x1FF`) |
| Oracle constructor (bands) | `sub_738E20` |
| HW-profile / table builder | `sub_8E5CA0` |
| Warp / dispatch profile | `sub_8E4400` |
| Cutlass barrier override (FNV-1a) | `sub_939370` |

- **Latency lookup** (`sub_8BF3A0`): for a node, if its operand flags (`*(op+108) & 5`) mark it a
  special form, return the default seed `oracle+92` (`{300,0,0,0}`); else if `*(op+104)` is
  non-zero use it directly; else index the flat scalar oracle at `oracle[744 + 4·(OriOpcode &
  0xFFFFCFFF)]`.
- **RAW handling**: the producer latency comes from the dependency-rule `latency` (or the per-class
  `read_latency` override when set ≠ −1); the consumer is held that many cycles. `sub_A08A00`
  mode-1 walks source operands (`sub_A08910`), adds each operand's cost into a per-functional-unit
  cumulative array `acc[4·unit_id]`, and sets the operand's register bit in the live scoreboard
  (`sub_BDBB80`).
- **WAW / WAR handling**: destination operands use `write_latency` (override) and the same
  scoreboard bitset; mode-3 (revert) clears bits via `sub_BDBC70`. The read/write split is exactly
  the `read_latency` / `write_latency` dependency-rule columns.
- **Long-latency gate**: `sub_8CCF80` returns true when the queried latency `> 19`, which decides
  whether an op is treated as latency-hiding material (memory / MMA) vs short ALU. The threshold 19
  is binary-fixed.
- **Cutlass tuning**: `sub_939370` FNV-1a-hashes the basic-block id (seed `0x811C9DC5`, prime
  `16777619`) against a per-kernel barrier table; a hit returns a packed `(stall_target,
  register_limit)` overriding default barrier insertion around MMA groups; a miss returns the
  sentinel `0x7FFFFFFF00000000`.

#### Sub-Record Formats in the Growable Buffer (+80)

Records with `string_backed_flag=1` carry variable-length sub-records in the growable buffer. The buffer header at `*(record+80)` is a 32-byte object: `{data_ptr, size (DWORD), capacity (DWORD), allocator_ptr}`.

**Type 59 (';') — Variant sub-records (20 bytes each):**

Created by `sub_8E5310` iterating the variant list at `config+536`:

```text
Sub-record layout (20 bytes):
  +0   DWORD   source_data       Variant source identifier
  +4   WORD    flags             Variant flags
  +6   WORD    zero              Reserved
  +8   DWORD   throughput_value  Throughput for this variant
  +12  DWORD   aux_value         Auxiliary parameter
  +16  DWORD   zero              Reserved
```

The main record additionally stores: `+16 = start_index` (from `config+544`), `+20 = record_index`, `+24 = back_ref` to previous category.

**Type 49 ('1') — Dimension sub-records (12 bytes each):**

Created by `sub_8E5530` traversing the BST at `config+592`:

```text
Sub-record layout (12 bytes):
  +0   WORD    node_flags        BST node flags (from node+38)
  +2   WORD    zero              Reserved
  +4   DWORD   node_value        BST node value (from node+32)
  +8   DWORD   node_child        BST node child pointer (low 32 bits of node+24)
```

**Type 44 (',') — class descriptor (16 bytes + packed bitmasks):**

Created by the generic appender `sub_8E8480` (and siblings) from caller-supplied class
data, followed by a call to `sub_8E3AD0` which appends packed bitmask DWORDs:

```text
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

### Table-Construction Helpers (`sub_8E7300`–`sub_8E97B0`)

The cluster of functions at `0x8E7300`–`0x8E97B0` is the table-construction family
invoked during scheduler init. An earlier draft of this page labelled each address
with a specific SM (`sub_8E7720` = "sm\_75 builder, 3.5 KB", `sub_8E8480` = "sm\_90
builder, 5.2 KB", …). **That per-SM attribution and the table-size column were an
inference and do not hold up against the decompiles.** Reading the two that were
spot-checked:

- `sub_8E7720` and `sub_8E8480` are **generic grow-and-copy record appenders**, not
  per-architecture builders. Each reads the growable array's `{base, count, capacity}`
  triple (`a1+56 / +64 / +68`), grows it by 1.5× when full (`count + ((count+1)>>1)`),
  copies the existing 96-byte records (three `_mm_loadu_si128` loads each, cloning the
  `+48` string field via `sub_714160`), and appends one new record. The only thing that
  differs is the record *type code* and the data the **caller** supplies: `sub_8E7720`
  stamps type `16` and copies the caller's `int`-pair (`a2[0]`, `a2[1]`); `sub_8E8480`
  stamps the `','` type-44 marker and `class_flags = 2`. Neither contains any SM
  constant, latency value, or intrinsic table size.

So these addresses are best understood as **shared append primitives** parameterized
entirely by their callers; the per-SM table sizes previously listed were derived from
inference, not from the function bodies, and have been removed. The actual
per-architecture content enters through the *callers* of these helpers (which pass the
SM-specific class data) and through the static base table at `0x2297C00` documented
above. Recovering a verified address-to-SM map would require tracing each caller's data
argument — that work is not yet done, and the page no longer claims it.

## Warp-Level Hardware Profile (sub\_8E4400)

`sub_8E4400` maps the SM architecture ID (a2) to warp-level dispatch parameters stored in a 36-byte structure:

### Architecture-to-Warp Mapping

| SM ID range | Warps per SM | Dispatch slots | Architecture era |
|---|---|---|---|
| <= 20479 | 4 | 96 | sm\_50 (Maxwell) |
| 20480–24575 | 6 | 176 | sm\_60 (Pascal) |
| 24576–28672 | 7 | 192 | sm\_70 (Volta) |
| 28673–32767 | 7 | 208 | sm\_75 (Turing) |
| 32768–36863 | 8 | 224 | sm\_80 (Ampere) |
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

These values model the effective issue width for different scheduling contexts. The tensor core modes (4–11) reflect warpgroup-level cooperative execution where multiple warp slots issue tensor instructions simultaneously.

## Memory Space Classification (sub\_693BC0)

`sub_693BC0` (22 lines) classifies the memory space of load/store instructions. It extracts the last source operand from the instruction, looks up the register descriptor, and calls `sub_91C840` to determine the memory space type. The function returns an integer code:

| Return value | Memory space | Typical latency range |
|---|---|---|
| 1 | Generic (resolved at runtime) | 20–200+ cycles |
| 2 | Local memory (per-thread stack) | 20–200 cycles |
| 3 | Shared memory | 20–30 cycles |
| 4 | Constant memory (cached) | 4–8 cycles |
| 7 | Constant bank (indexed) | 4–8 cycles |
| 11 | Surface memory | 200–500 cycles |
| 16 | Global memory (DRAM) | 200–500 cycles |

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

### Mode 2: Differential Cost (Speculative-Rollback)

Mode 2 computes the *net* resource change from last-use operand releases.  It uses
a speculative-rollback pattern: run the full operand scan to populate the output
vector, then undo every side effect on the persistent state so that only the
caller-visible output survives.

```c
function ResourceCost_Mode2(state, ctx, instr_data, bitmask, output, FU_vec):
    // --- snapshot persistent counters before mutation ---
    saved_add_count     = state.add_count        // state[0]
    saved_release_count = state.release_count     // state[1045]

    output[0..9] = 0                              // clear 10-element FU vector
    sched_node  = instr_data.sched_node           // *(instr_data+32)
    num_operands = instr_data.operand_count        // *(instr_data+80)
    pressure_9b  = sched_node.word_12 & 0x1FF     // 9-bit pressure field

    for i in 0 .. num_operands-1:
        operand = instr_data.operands[i]           // *(instr_data + 84 + 8*i)
        if operand_type(operand) != REGISTER: continue
        reg_id = operand & 0xFFFFFF
        if reg_id in {41,42,43,44}: continue       // skip sentinel registers
        desc = ctx.reg_table[reg_id]               // *(ctx+88)[reg_id]
        if desc.reg_class > 6: continue            // non-physical file

        if operand_sign_bit_set(operand):          // bit 31 = last-use flag
            // --- release path: operand is dead after this instruction ---
            if not IsLastUseEligible(instr_data, i): continue   // sub_A07C00
            (start, count, cost) = GetRegisterLatency(ctx, desc, &operand)
            for k in 0 .. count-1:
                hw_reg = start + k
                if bitmask_test(bitmask, hw_reg):   // already consumed
                    continue
                output[desc.reg_class] -= cost      // SUBTRACT from FU bucket
                bitmask_clear(bitmask, hw_reg)       // sub_BDBC70
                state.release_list[state.release_count++] = hw_reg
        else:
            // --- normal path: same as mode 0/1 ---
            if operand_aux_negative(operand): continue  // byte +6 check
            (start, count, cost) = GetRegisterLatency(ctx, desc, &operand)
            for k in 0 .. count-1:
                hw_reg = start + k
                if bitmask_test(bitmask, hw_reg): continue
                output[desc.reg_class] += cost      // ADD to FU bucket
                bitmask_set(bitmask, hw_reg)         // sub_BDBB80
                state.add_list[state.add_count++] = hw_reg

    // --- update pressure if new instruction exceeds current ---
    if output[6] < pressure_9b and pressure_9b != 0:
        output[6] = pressure_9b                     // *(FU_vec+24)

    // --- rollback: undo every bitmask mutation ---
    for j in saved_add_count .. state.add_count-1:
        bitmask_clear(bitmask, state.add_list[j+1])  // undo sets
    state.add_count = saved_add_count

    for j in saved_release_count .. state.release_count-1:
        bitmask_set(bitmask, state.release_list[j+1046])  // undo clears
    state.release_count = saved_release_count
    // output[0..9] retains the net delta; state is unchanged
```

The caller (`ComputeResourceCost`) accumulates `output[0..9]` into `slot[10..19]` --
the "release pressure" half of the 20-element resource vector.  Because the rollback
restores the bitmask to its pre-mode-2 state, mode 2 is side-effect-free on
`state` and `bitmask`; it only communicates through the output vector.

### Mode 3: Pressure Accumulation

Adds the instruction's previously computed pressure `a1[2071]` into the running total at `*(a5+24)`.

### Per-Operand Cost Computation

For each source operand, the function:
1. Checks operand type: `((operand >> 28) & 7) == 1` means register operand.
2. Skips operands with values 41–44 (special sentinel registers).
3. Looks up the register descriptor via `*(a1+88) + 8 * (operand & 0xFFFFFF)`.
4. Checks if register class `*(descriptor+64)` is <= 6 (physical register file).
5. Calls `sub_A08910` to get the register's latency and count:
   - Returns the starting register index
   - Outputs count (`*a4`) and cost-per-register (`*a5`)
6. Iterates over the register range, accumulating costs for registers not in the "already-consumed" bitmask at `*(a1+832)`.

The cost accumulation uses a 9-bit field in the instruction's scheduling word at offset +12, masked as `& 0x1FF`.

## Register Latency Query (sub\_A08910)

`sub_A08910` (39 lines) returns the register index and cost for a single operand:

```c
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
| Shared memory loads | `"LDS latency hiding"` | `sub_73A1D0` | 20–30 cycles |
| Global memory loads | `"LDG latency hiding"` | `sub_73A7F0` | 200–500 cycles |
| Extended 64-bit ops | `"Xu64 latency hiding"` | `sub_73ADF0` | 15–30 cycles |
| Anti-dependencies | `"Antidep latency hiding"` | (inline) | varies |

Each category reports: **Num** (count of operations), **Min** (minimum hidden cycles), **Max** (maximum hidden cycles), **Avg** (average hidden cycles). The pass also tracks MAC instruction utilization (`"MacInsts"`, `"MacReuses"`, `"TepidMacUtil"`) and resource busy time (`"LsuResBusy"`, `"Time"`, `"TepidTime"`).

This analysis runs after scheduling is complete and drives feedback for the Mac Loop scheduler, which handles fused multiply-accumulate loop bodies. Knob 443 gates the MAC instruction classification.

## Dual-Issue Rules

Dual-issue scheduling is split across three functions: `sub_8CF5D0` (CheckDualIssueEligibility, 3.5 KB) decides whether the pass runs at all and computes the per-function benefit score; `sub_8B77C0` (DualIssueScheduler, 15 KB) drives the per-slot pairing loop; `sub_8BDC40` (DualIssuePairing, 7.9 KB) is the partner-record helper that `sub_8B77C0` calls per candidate — it walks the candidate's dependency-rule bucket, applies the `pipe_masks_b` compatibility mask, checks for RAW/WAR conflicts against the already-committed slot 0 instruction, and either marks the pair as co-issued (writing into `pair_record+offset` at `slot_array[id*96]`) or returns 0 to signal the caller to try the next candidate.

### Eligibility Check

`sub_8CF5D0` returns 0 (no dual-issue) if:
- The target architecture does not support dual-issue (`sub_7DC0E0` returns false).
- Function flag bit 2 at `func+1368` is set (incompatible function).

When eligible, the function iterates basic blocks checking instruction pairs:
- `sub_A9CDE0(instr)`: returns true if instruction is dual-issuable (hot = global/texture).
- `sub_A9CF90(instr)`: returns true if instruction can pair with the next (cold = constant/shared).

The dual-issue benefit score is stored at `scheduler+328` and used by the priority function to bias toward instruction pairs that can co-issue.

### Dual-Issue Constraints

Dual-issue pairs must satisfy all four conditions simultaneously:

1. **Pipe compatibility**: the two instructions must target different functional units. Same-pipe pairs cannot dual-issue.
2. **No RAW dependency**: the pair must not have a read-after-write hazard on the same register in the same cycle.
3. **No pending barrier**: neither instruction may be waiting on a scoreboard barrier.
4. **Architecture gate**: dual-issue is a Maxwell-only feature — sm\_50, sm\_52, and sm\_53 (Tegra X1) all carry `target+1032` bit 7 set. The eligibility check returns false on every later family (sm\_60 Pascal onward clears the bit; Volta/Turing post-RA scheduling explicitly excludes dual-issue per [PostSchedule](post-schedule.md), and sm\_8x\_shared zeros `pipe_masks_b[0..1]` at the latency-table level).

For Maxwell, a dedicated register-budget adjustment (`DualIssueBudget`, invoked from `ComputeRegisterBudget` when `arch_id == 5`) trims the per-warp register target so paired co-issued instructions keep both dispatch slots busy without spilling. This budget delta is the only register-allocation pressure knob with a Maxwell-specific code path.

### Memory Space Classification for Pairing

`sub_A9CDE0` (IsHotMemory) and `sub_A9CF90` (IsColdMemory) classify LD/ST instructions into two non-overlapping categories that form the pairing basis. Both extract the last source operand's memory space via `sub_91C840`:

| Function | Opcode 183/288 (LD/ST) | Opcode 91/92 (TEX/SULD) | Memory space match |
|---|---|---|---|
| `sub_A9CDE0` (hot) | space == 6 (global), or space == 4 with addr\_mode bits 19:21 == 1 | operand low 3 bits == 7 | Global, texture |
| `sub_A9CF90` (cold) | space == 5 (shared), or space == 4 with addr\_mode bits 19:21 == 2 | `(operand & 1) == 0` and `((operand ^ 6) & 6) == 0` | Shared, constant |

The per-block classifier `sub_A9D140` marks each block with bit flags at `block+264`: bit 0 = has hot instructions, bit 1 = has cold instructions. If any block has both bits set (`(flags & 6) == 6`), dual-issue is disabled for the entire function — mixed hot/cold blocks defeat the benefit model.

### Pipe Compatibility Matrix

The `pipe_masks_b` field in the HW latency table encodes which pipe classes a scheduling class can pair with for dual-issue. Each byte is a bitmask over the 6 pipe classes. The matrix below summarizes the pairing rules as observed in `sm_7x_shared` (which carries the Maxwell-inherited dual-issue data; `sm_8x_shared` and `sm_10x_shared` set `pipe_masks_b[0..1]` to all-zero, disabling dual-issue at the table level).

| Pipe A (slot 0) | Pipe B (slot 1) | Can co-issue | Evidence |
|---|---|---|---|
| ALU (pipe 0) | FP32 (pipe 1) | YES | `pipe_masks_b` bit 1 set on ALU classes |
| ALU (pipe 0) | LSU (pipe 4) | YES | `pipe_masks_b` bit 2 set on ALU classes |
| ALU (pipe 0) | SFU (pipe 1b) | YES | SFU shares pipe B slot |
| FP32 (pipe 1) | ALU (pipe 0) | YES | Symmetric with row 1 |
| FP32 (pipe 1) | LSU (pipe 4) | YES | `pipe_masks_b` bit 2 set on FP32 classes |
| LSU (pipe 4) | ALU (pipe 0) | YES | `pipe_masks_b` bit 0 set on LSU classes |
| LSU (pipe 4) | FP32 (pipe 1) | YES | `pipe_masks_b` bit 1 set on LSU classes |
| FP64 (pipe 2) | any | NO | FP64 occupies both dispatch slots |
| Tensor (pipe 3) | any | NO | Tensor occupies both dispatch slots |
| Control (pipe 6) | any | NO | Branch/sync not dual-issuable |
| any | same pipe | NO | Same-pipe conflict |

The 337 scheduling classes with non-zero `pipe_masks_b` in `sm_7x_shared` use values 1–35 as a per-class pairing affinity code. The `dual_issue_flags` field at record offset +22 is populated from `pipe_masks_b[0:2]` during 96-byte record construction.

### Pairing Decision Pseudocode

```c
function CheckDualIssueEligibility(scheduler):            // sub_8CF5D0
    target = scheduler.func.target
    scheduler.dualIssueBenefit = 0                         // +328

    if target+1032 bit 7 clear:  return 0                  // arch gate
    if func+1368 bit 1 set:      return 0                  // incompatible function

    if not PerBlockClassify(target):  return 0             // sub_A9D140

    for each basic_block in func (reverse RPO):
        if block.flags & 3 != 3:  continue                 // need entry+exit edges
        hot_count = 0;  cold_count = 0

        for each instr in block:
            weight = (last_src_operand_byte & 7) + 1
            if IsHotMemory(target, func, instr):           // sub_A9CDE0
                hot_count += weight
            elif IsColdMemory(target, func, instr):        // sub_A9CF90
                cold_count += weight

        if hot_count > 0:
            score = min(hot_count, cold_count) + cold_total
            if score > scheduler.dualIssueBenefit:
                scheduler.dualIssueBenefit = score
                if score > target+616:  return 0           // exceeds arch limit
    return 1
```

The benefit score at `scheduler+328` biases the priority function toward co-issuable pairs, and gates selection of the dual-issue scheduling strategy (`sub_8B77C0`) when `scheduler+328 > 0` and `arch <= sm_52`.

## Stall Count Computation

The stall count determines how many cycles an instruction must wait before it can issue. Stalls are computed by `sub_8D3E20` (2.1 KB) and encoded by `sub_8F3130` (1.0 KB).

### Stall Encoding in Control Words

Each SASS instruction carries a stall count in its control word:
- Maximum stall: 16 cycles (capped by knobs 805 and 806).
- Minimum stall: 1 cycle (no zero-stall encoding exists).
- Default stall when no dependency: determined by the HW profile's pipeline depth.

The stall/barrier encoding pipeline (`sub_8D7760`, 41 KB) computes stalls by walking the dependency DAG backward from each instruction:

```c
function ComputeStallCycles(sched, instr):
    max_wait = 0
    for each predecessor of instr:
        distance = instr.cycle - pred.cycle
        latency = LookupLatency(sched, pred, instr)
        wait = latency - distance
        max_wait = max(max_wait, wait)
    return min(max_wait, MaxStallFromKnob(sched))
```

### LookupLatency Implementation (sub\_8BF3A0)

`sub_8BF3A0` resolves the edge latency from a predecessor via three priority-ordered sources:

```c
function LookupLatency(sched, pred, instr):           // sub_8BF3A0
    profile = *(sched+16)                              // scheduler profile object
    node    = *(pred+40)                               // SchedNode for predecessor
    // Path 1: barrier/scoreboard override
    if (*(node+108) & 0x05) != 0:                      // barrier (bit 0) or long-lat (bit 2)
        return *(profile+92)                           // default barrier latency (arch constant)
    // Path 2: pre-computed HW table latency (hot path)
    latency = *(int16*)(node+104)                      // from 96-byte record +20 (base_latency)
    if latency != 0:
        return latency                                 // e.g. 17=ALU, 42=FP64
    // Path 3: per-opcode fallback table
    opcode = *(pred+72) & 0xFFFFCFFF                   // Ori opcode, modifier bits masked
    return *(profile + 4*opcode + 744)                 // static 322+ entry table
```

Path 2 is the common case. Path 1 fires for scoreboard-tracked instructions (global loads, texture). Path 3 covers pseudo-ops that `sub_89FBA0` did not classify.

### MaxStallFromKnob Implementation (sub\_8D0640)

`sub_8D0640` initializes `sched+404` (maxStallCycles) during scheduler setup. The scheduling mode selects which knob applies:

```c
function MaxStallFromKnob(sched):                     // returns *(sched+404)
    mode = GetSchedulingMode(context)                  // 0=ILP, 1=ReduceReg, 2=DynBatch
    if context+507 != 0:                               // special "short stall" flag
        sched+404 = 6                                  // reduced cap for pressure-sensitive kernels
    else if mode != 2:                                 // ILP or ReduceReg
        sched+404 = min(*(context+508), 16)            // architecture default
        if KnobIsSet(ctx, 805):                        // knob 805 override
            sched+404 = min(GetKnobValue(ctx, 805), 16)
    else:                                              // DynBatch (mode 2)
        if KnobIsSet(ctx, 806):                        // knob 806 override
            sched+404 = min(GetKnobValue(ctx, 806), 16)
        else:
            sched+404 = 0                              // no stall cap (fall through)
    return *(sched+404)                                // ceiling for ComputeStallCycles
```

Knob 805 tunes ILP/ReduceReg; knob 806 tunes DynBatch. Both clamp to 16 (the 4-bit control word field). The ILP default comes from `context+508` (architecture pipeline depth minus 1).

The encoding function `sub_8F4140` packs the complete control word:

| Field | Encoder | Bits | Range |
|---|---|---|---|
| Stall count | `sub_8F3130` | 4 | 1–16 cycles |
| Yield hint | `sub_8F3650` | 1 | 0/1 |
| Read barrier | `sub_8F31F0` | 6 | 0–5 (barrier ID) |
| Write barrier | `sub_8F31F0` | 6 | 0–5 (barrier ID) |
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

```c
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

### Detection (sub\_8F47E0)

`sub_8F47E0` (12 lines) detects Cutlass GEMM kernels via `strstr(function_name, "cutlass")`, where the function name comes from the compilation unit's symbol table through `ctx->symtab->getName(ctx->func_id)`. The result propagates into `ctx+1381` bit 6 (`0x40`). This bit is set by the opcode visitor `sub_92C240` for both HMMA (case 77) and WMMA (case 50) opcodes — both flow through LABEL\_329, which ORs `0x40` unconditionally. The strstr result gates *downstream consumption* of the bit, not its setting.

### Flag Propagation into the Scheduler

`sub_94A020` (scheduling setup) reads the flag into the per-function scheduling context:

```c
sched->is_cutlass = 0                             // sched+440
if ctx->flags[1414] & 0x10:                       // matrix-instruction feature gate
    sched->is_cutlass = (ctx->flags[1381] & 0x40) != 0
```

The gate at `ctx+1414` bit 4 restricts the Cutlass path to architectures supporting matrix instructions. Two companion fields are set alongside: `sched+441` (knob 618) and `sched+442` (knob 629), providing alternative matrix scheduling modes independent of name detection.

### Barrier Worklist Builder (sub\_8F4820)

When Cutlass is detected, `sub_8F4820` builds a worklist of barrier insertion points for MMA-dominated basic blocks. It reads two hardware-profile fields:
- `profile+26136`: Cutlass detection mode (byte; `1` = override active)
- `profile+26144`: Cutlass stall count override (int; replacement value)

The function walks blocks in RPO order, finds the first and last instructions in each MMA/HMMA sequence (matched by `instr+164` slot ID equality), then scans between them for register operands whose source-position index falls below a per-opcode threshold:

| Opcode (masked) | Threshold | Notes |
|---|---|---|
| 22 (IMAD) | `sub_7E40E0(instr, 3)` | varies by modifier |
| 50 (WMMA) | `xmmword_21B2EC0[type*5]` | indexed by data type |
| 51 (HMMA), 110-111 | 3 | fixed |
| 77 (MMA) | `sub_7E36C0(2, flags...)` | varies by layout |
| 83 (BMMA) | `sub_7E3640(instr, 3)` | varies by shape |
| 112 | 4 | |
| 279 (STS) | 6 | shared memory store |
| 289 | 3 | |
| 297 (HMMA v2) | `sub_7E3790(instr, 3)` | varies by modifier |
| 352 (WGMMA) | `sub_7E3800(instr, 3)` | varies by modifier |

When a source operand falls below the threshold, `sub_93E9D0` inserts a `(block_id, priority)` pair into the worklist, with priority 1 (if knob 646 returns 2, indicating the low-latency fast path) or 3 (standard). Knob 646 can disable insertion entirely.

### Scheduling Score Adjustment (sub\_939370)

When `sched->is_cutlass` or `sched+441` is nonzero and the barrier worklist is populated (`sched+240 != 0`), the priority scoring functions (`sub_93FBE0`, `sub_93F130`, `sub_939220`) enter barrier-aware mode, restricted to scheduling modes 3, 5, and 6 (`sched+1504`).

`sub_939370` performs an FNV-1a hash lookup (seed `0x811C9DC5`, prime `16777619`) on the basic block ID (`a2+8`) against the barrier hash table at `sched+280..296`. A match returns a packed 64-bit `(stall_target, register_limit)` pair; a miss returns sentinel `0x7FFFFFFF00000000` (no override). The scoring function uses these values to adjust instruction priority by comparing current live-register pressure against the Cutlass-specific register limit and modifying stall insertion around MMA instruction groups.

### Iterative Rematerialization (sub\_913A30)

The Cutlass flag activates an iterative sink+remat path in Phase 28. When `ctx+1381 & 0x40` is set, `sub_913A30` runs the core engine `sub_911030` (2408 lines) in a convergence loop of up to knob 862 iterations (default 5). Each iteration calls `sub_8F5220` (init state), `sub_911030` (sink+remat), `sub_8F59C0` (convergence check), `sub_8F5AD0` (update state), and `sub_909A20` (propagate). Non-Cutlass functions receive a single-pass path via `sub_A0F020` instead. The remat cost model `sub_90B790` also relaxes eligibility for Cutlass: instructions with property bits 2-3 set (normally disqualifying) are permitted when `sub_8F47E0` returns true.

### Join Point: scheduling\_class + pipe\_class --> Final Control Word

The two classification systems operate at different pipeline stages and converge in the control word encoder. `sub_89FBA0` runs during IR-level scheduling to assign a `scheduling_class` (integer stored at `SchedNode+4`, range 2–772+). `sub_13710B0` runs during SASS encoding to assign a `pipe_class` (9-bit value in `*(WORD*)(a3+196) & 0x1FF`, range 0x00–0x141). On Blackwell (sm\_10x), `sub_7C4950` dispatches to `sub_89FBA0` for opcodes it handles natively and falls back to `sub_13710B0` for others, meaning some opcodes get both classifications while others get only `pipe_class`.

The two values are consumed at different points in the stall/barrier computation pipeline:

```c
function EmitControlWord(sched, instr):
    // --- Phase 1: scheduling_class drives latency and barrier selection ---
    // sub_8D7760 walks the dependency DAG backward from instr
    sched_class = *(SchedNode+4)            // assigned by sub_89FBA0
    for each predecessor:
        rule = per_sm_dependency_rules[sched_class]   // 40-byte record
        raw_lat = rule.latency              // RAW cycles (e.g., FFMA=17, LDG=42)
        barrier_lat = rule.barrier_latency  // when stall > 16, use scoreboard
        stall_cycles = max(stall_cycles, raw_lat - distance)

    // --- Phase 2: pipe_class drives encoding and reuse ---
    // sub_89FBA0 LABEL_3 (at 0x8A2119 equivalent) post-processes pipe_class
    pipe_class = *(WORD*)(a3+196) & 0x1FF   // assigned by sub_13710B0 or sub_89FBA0
    if pipe_class > 0xCD:                   // high classes skip reuse check
        goto finalize
    // sub_A2D340: test register reuse eligibility based on opcode type
    // opcodes 0x2B ('+'), 0x76 ('v'), 0x41 ('A'), 123, 304..322 bypass

    // --- Phase 3: merge into 21-bit control word ---
    stall_4bit = min(stall_cycles, MaxStallFromKnob(sched))  // 4 bits, 1..16
    barrier_3bit = AllocateBarrier(sched, instr)              // 3 bits, 0..5
    // If raw_lat > stall_max: assign a write barrier (barrier_3bit)
    // The barrier ID comes from a 6-slot pool, allocated by affinity to
    // the producer's scheduling_class (long-latency classes get priority)
    //
    // pipe_class determines the pipe mask, sub-class, and pipe flags that
    // feed into dual-issue pairing (same-pipe pairs cannot co-issue) and
    // resource vector accounting (which of the 10 FU counters to charge)
```

The critical asymmetry: `scheduling_class` controls *how long* to wait (latency lookup, barrier threshold), while `pipe_class` controls *where* the instruction executes (pipe mask, sub-class, reuse flags). For the 206 opcodes with specialized handlers in `sub_89FBA0`, both values are set in the same switch body — the function writes `*(v8+4) = <sched_class>` and `*(WORD*)(a3+196) = <pipe_class>` together, then falls through to LABEL\_3 for reuse-flag post-processing. The remaining 124 default-handler opcodes get only `scheduling_class` from `sub_89FBA0` (pipe\_class stays at 0xF8000 = all-pipes sentinel), and the SASS encoder `sub_13710B0` later overwrites the pipe\_class with a SASS-level value for the actual encoding.

## Execution Pipe Assignment (sub\_13710B0)

`sub_13710B0` (7.1 KB, 1,088 lines decompiled) is the SASS-backend execution pipe class assigner. It runs in the SASS encoding pipeline (address range 0x1370–0x139F) *after* instruction selection, register allocation, and the main scheduling pass are complete. Where `sub_89FBA0` assigns IR-level scheduling class IDs (2–772+) consumed by the priority and stall-computation passes, `sub_13710B0` writes SASS-level pipe class IDs (0x00–0x141) that control control-word encoding: stall counts, barrier assignments, and dual-issue pairing in the final binary.

### Descriptor Initialization

Before dispatching on the opcode, the function initializes the scheduling descriptor at `a3+196..202` to the "all-pipes" default:

```c
*(DWORD*)(a3+196) |= 0xF8000     // pipe mask = all (bits 15..19)
*(BYTE*)(a3+200)  |= 0x1F        // read barrier mask = all
*(WORD*)(a3+198)   = HIWORD | 0x1F0  // throughput class = max
*(WORD*)(a3+200)  |= 0x3E0       // write barrier mask = all
*(BYTE*)(a3+199)   = ... | 0x3E  // pipe flags = all set
```

Then it switches on `*(a2+72) & 0xFFFFCFFF` (the Ori opcode with modifier bits masked), writing a 9-bit pipe class into the low bits of `*(WORD*)(a3+196)` and optionally overriding the pipe mask, sub-class, and pipe flags.

### Pipe Mask Encoding

Bits 15–19 of `*(DWORD*)(a3+196)` select the execution pipe:

| Value | Pipe | Functional units | Resource vector indices |
|---|---|---|---|
| `0x08000` | Pipe A | ALU, integer, FP64, conversion | 0 (ALU), 2 (DFMA) |
| `0x10000` | Pipe B | FP32, tensor, SFU, half-precision | 1 (FMA), 3 (MMA), 8 (SFU) |
| `0x18000` | Pipe C | Memory, texture, wide FP64 | 4 (LSU), 5 (TEX) |
| `0xF8000` | All | Default sentinel (no constraint) | — |

### Sub-Class Encoding

Bits 4–7 of `*(WORD*)(a3+198)` encode the sub-class within the pipe:

| Value | Sub-class | Instruction category |
|---|---|---|
| `0x10` | Control flow | Branch, predicate, miscellaneous |
| `0x20` | Integer ALU | Conversion, barrier, integer ops |
| `0x30` | FP32 / SFU | Single-precision, half-precision |
| `0x40` | FP64 / Tensor | Double-precision wide, tensor core |

### Pipe Flags Encoding

Bits 1–5 of `*(BYTE*)(a3+199)` encode sub-unit affinity:

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
| 1 | 0x08 | — | 0x10 | IMAD | Always |
| 2–7 (wide) | 0x03 | B (0x10000) | 0x30 | IMAD\_WIDE, IADD3, etc. | `sub_7D6780` = true |
| 2–7 (wide, v6=6) | 0x03 | C (0x18000) | 0x40 | LOP3 (wide, FP64) | Opcode 6, wide |
| 2–7 (narrow) | 0x0C | A (0x08000) | — | IMAD, IADD3, etc. | Narrow, type != 19 |
| 2–7 (narrow, t=19) | 0x7B | — | — | IMAD (BF16/FP8 type) | Type 19 path |
| 8 (flag clear) | 0x33 | — | — | IABS (no guard) | Operand flag bit 0 |
| 8 (flag set) | 0x34 | — | — | IABS (guarded) | Operand flag bit 0 |
| 0x10 (flagged) | 0x68 | — | — | ATOM (flagged) | Operand bit 2 |
| 0x10 (mem=3) | 0x67 | — | — | ATOM (shared) | `sub_7DFFC0` = 3 |
| 0x10 (mem=4) | 0x69 | — | — | ATOM (constant) | `sub_7DFFC0` = 4 |
| 0x10 (other) | 0x66 | — | — | ATOM (global) | Default |
| 0x12 (no 0x400) | 0x3D | — | — | FADD (standard) | Operand bit 10 clear |
| 0x12 (0x400 set) | 0x78 | — | — | FADD (const-bank) | Operand bit 10 set |
| 0x17 (op1 reg6) | 0x37 | — | — | S2R (tensor reg, op1) | `*(desc+64)` = 6 |
| 0x17 (op2 reg6) | 0x36 | — | — | S2R (tensor reg, op2) | `*(desc+64)` = 6 |
| 0x17 (other) | 0x38 | — | — | S2R (standard) | Neither operand reg6 |
| 0x18 | 0x04 | A (0x08000) | 0x20 | FSETP | Always |
| 0x24 (wide) | 0x14 | B (0x10000) | 0x30 | PRMT (FP width) | `sub_7D6780` = true |
| 0x24 (narrow) | 0x11 | B (0x10000) | 0x30 | PRMT (integer) | `sub_7D6780` = false |
| 0x33 | 0x21 | A (0x08000) | 0x20 | IDP | Always; flags 0x06 |
| 0x3C (mem ops) | 0x2B–0x32 | — | — | STG variants | 6-way split on flags |
| 0x3E (mem ops) | 0x2D–0x2E | — | — | LDL variants | Flag / no-flag split |
| 0x42 | 0x5D | — | — | MUFU (SFU) | Always |
| 0x4D | 0x84–0x85 | B (0x10000) | 0x40 | WGMMA-class | Extended tensor fields |
| 0x4E (mem ops) | 0x2F–0x30 | — | — | LD (generic) | Flag / no-flag split |
| 0x66 | 0x09 | B (0x10000) | 0x30 | DEPBAR | Always; flags 0x08 |
| 0x82 / 130 (ext) | 0x17 | — | — | NANOTRAP (extended); `HSET2` in ROT13 | `sub_A9AB10` = true |
| 0x82 / 130 (ctrl) | 0x13 | all (0xF8000) | 0x10 | NANOTRAP (control); `HSET2` in ROT13 | vtable+640 |
| 0xC9–0xCA (wide) | 0x07 | A (0x08000) | — | DFMA, DADD (wide) | `sub_7D6780` = true |
| 0xD1 | 0x05 | A (0x08000) | 0x20 | DFMA | Always |
| 0xD2 | 0x0A | A (0x08000) | 0x30 | DFMA variant | Sub-class 0x30, flag 0x04 |
| 0xF0 | 0x0F | A (0x08000) | — | F2F | Flags 0x04 |
| 0x10E | 0x7E | B (0x10000) | — | HMMA\_16 | Flags 0x08 |
| 0x117 | 0x80 | B (0x10000) | 0x40 | HMMA\_32 | Tensor pipe; flags 0x0C |
| 0x11A | 0x81 | B (0x10000) | 0x40 | IMMA | Tensor pipe |
| default | 0x88 | — | — | (unrecognized) | Sentinel |

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

When `sub_7D6780` returns false (not wide) and `*(a2+76) == 19`, several instruction groups receive distinct pipe classes in the 0x7A–0x7D range:

| Ori opcode group | Standard class | Type-19 class | Likely type |
|---|---|---|---|
| 2–7 (narrow) | 0x0C | 0x7B | BF16 / FP8 |
| 0x6E–0x72 (narrow) | 0x0B | 0x7A | BF16 / FP8 |
| 0x8B–0x8C (narrow) | 0x0D | 0x7C | BF16 / FP8 |
| 0xC9–0xCA | 0x10/0x12 | 0x7D | BF16 / FP8 |

Type 19 likely corresponds to BF16 or FP8, which require different pipeline routing than standard FP16/FP32/FP64 types on Hopper and Blackwell architectures.

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_693BC0` | 22 lines | MemorySpaceClassify — return memory space code | HIGH |
| `sub_695530` | 606 lines | ComputeLatencies — per-BB latency computation | HIGH |
| `sub_704D30` | 14 KB | GetFunctionalUnit — SASS opcode to FU mapping | HIGH |
| `sub_73A1D0` | ~6 KB | LDSLatencyStats — shared memory latency stats | HIGH |
| `sub_73A7F0` | ~6 KB | LDGLatencyStats — global memory latency stats | HIGH |
| `sub_73ADF0` | 6.5 KB | XU64LatencyStats — extended unit latency stats | HIGH |
| `sub_73B360` | 28.7 KB | MacLoopSchedulingAnalytics — latency hiding report | HIGH |
| `sub_799860` | 2.9 KB | ClassifyInstructionLatency | HIGH |
| `sub_89FBA0` | 85 KB | **SetOpcodeLatencies** — per-opcode scheduling class | HIGH |
| `sub_8B5400` | 14 KB | ScheduleForLatency — latency-optimized scheduling | MEDIUM |
| `sub_8B77C0` | 15 KB | DualIssueScheduler — dual-issue scheduling engine | MEDIUM |
| `sub_8BDC40` | 7.9 KB | DualIssuePairing — instruction pair selection | MEDIUM |
| `sub_8C67A0` | 3.7 KB | ComputeResourceCost — per-instruction FU cost | HIGH |
| `sub_8C7290` | 5.1 KB | GetResourceVector — SSE-optimized copy | HIGH |
| `sub_8CCF80` | 2.3 KB | IsLongLatencyOp — latency > 19 check | HIGH |
| `sub_8CF5D0` | 3.5 KB | CheckDualIssueEligibility | HIGH |
| `sub_8D3E20` | 2.1 KB | ComputeStallCycles — required stall count | HIGH |
| `sub_8D7760` | 41 KB | StallAndBarrierInsertion — encode stalls/barriers | HIGH |
| `sub_8E3AD0` | — | CopyProfileEntries — finalize HW table | MEDIUM |
| `sub_8E4400` | 3.3 KB | **InitHWProfile\_Warp** — warp dispatch params | HIGH |
| `sub_8E4920` | 6.9 KB | BuildScoreboardEntries — scoreboard BST | HIGH |
| `sub_8E4D80` | 15 lines | StringRefCleanup — decref string in record copy | HIGH |
| `sub_8E4F20` | ~1.5 KB | EmitWeightEntry — supplementary weight record (type 'W') | HIGH |
| `sub_8E5310` | ~1.5 KB | EmitVariantSection — variant sub-records (type ';') | HIGH |
| `sub_8E5530` | ~1.5 KB | EmitDimensionEntries — dimension sub-records (type '1') | HIGH |
| `sub_8E5CA0` | 20 KB | **EmitScheduleOutput** — scheduling control words | HIGH |
| `sub_8E6760` | 2.9 KB | EmitGroupBoundary — group boundary marker | HIGH |
| `sub_8E6B40` | 2.9 KB | EmitSchedEntry — standard scheduling entry | HIGH |
| `sub_8E6D40` | 2.9 KB | EmitBarrierEntry — barrier/sync entry | HIGH |
| `sub_8E6F20` | 2.9 KB | EmitWaitEntry — wait dependency entry | HIGH |
| `sub_8E7110` | 2.9 KB | EmitScoreboardEntry — scoreboard entry | HIGH |
| `sub_8E7300` | 3.3 KB | Table-construction appender helper (caller-fed class data) | LOW |
| `sub_8E7540` | 2.9 KB | Table-construction appender helper | LOW |
| `sub_8E7720` | 3.5 KB | Generic grow-and-copy record appender (type 16) — **verified** | HIGH |
| `sub_8E7940` | 2.9 KB | Table-construction appender helper | LOW |
| `sub_8E7B40` | 3.3 KB | Table-construction appender helper | LOW |
| `sub_8E7D80` | 4.4 KB | Table-construction appender helper | LOW |
| `sub_8E8070` | 3.5 KB | Table-construction appender helper | LOW |
| `sub_8E8280` | 3.1 KB | Table-construction appender helper | LOW |
| `sub_8E8480` | 5.2 KB | Generic grow-and-copy record appender (type 44 `','`) — **verified** | HIGH |
| `sub_8E8780` | 4.6 KB | Table-construction appender helper | LOW |
| `sub_8E8A90` | 3.0 KB | Table-construction appender helper | LOW |
| `sub_8E8CB0` | 949 B | Table-construction appender helper (short) | LOW |
| `sub_8E8DB0` | 1.7 KB | Table-construction appender helper | LOW |
| `sub_8E8F60` | 618 B | Table-construction appender helper (short) | LOW |
| `sub_8E9000` | 2.9 KB | Table-construction appender helper | LOW |
| `sub_8E92E0` | 5.5 KB | Table-construction appender helper | LOW |
| `sub_8E97B0` | 8.8 KB | Table-construction appender helper (fallback path) | LOW |
| `sub_8E9DC0` | 4.8 KB | EmitLatencyEntry — HW table entry helper | HIGH |
| `sub_8EFA10` | 18 KB | EmitScheduleReport — statistics output | HIGH |
| `sub_8F0CD0` | 24 B | MapFUClassID — (opcode, name) to class | HIGH |
| `sub_8F1EB0` | 15 KB | EncodeScheduleWords — SASS control word output | HIGH |
| `sub_8F3130` | 1.0 KB | EncodeStallField | HIGH |
| `sub_8F31F0` | 6.1 KB | EncodeBarrierField | HIGH |
| `sub_8F3650` | 2.7 KB | EncodeYieldField | HIGH |
| `sub_8F3860` | 3.0 KB | EncodeScoreboardField | HIGH |
| `sub_8F4140` | 5.6 KB | EncodeFullControlWord | HIGH |
| `sub_8F47E0` | ~50 B | DetectCutlass — strstr for "cutlass" | CERTAIN |
| `sub_A08910` | 39 lines | GetRegisterLatency — operand cost query | HIGH |
| `sub_A08A00` | 345 lines | ResourceModel — 3-mode FU cost computation | HIGH |
| `sub_A09530` | 91 lines | UpdateStallCycles — per-instruction stall update | HIGH |
| `sub_A9CDE0` | — | IsHotMemory — global/texture classification | HIGH |
| `sub_A9CF90` | — | IsColdMemory — constant/shared classification | HIGH |
| `sub_13710B0` | 7.1 KB | **AssignPipeClass** — SASS-level pipe assignment | HIGH |
| `sub_1370F40` | ~500 B | CheckTensorFeature — gates tensor pipe classes | HIGH |
| `sub_7D6780` | ~100 B | IsWideType — true for FP64/wide types | HIGH |
| `sub_7DFFC0` | ~200 B | ClassifyMemAccess — 3=shared, 4=constant | HIGH |
| `sub_7E3640` | ~100 B | GetCustomPipe — 5-bit pipe sub-class | MEDIUM |
| `sub_91E7A0` | ~100 B | GetSrcEncoding — source operand encoding query | MEDIUM |
| `sub_91E860` | ~100 B | GetOperandType — operand type code | MEDIUM |
| `sub_A9AB10` | ~100 B | NeedsExtEncoding — extended encoding check | MEDIUM |

## Cross-References

- [Scheduler Overview](overview.md) — 3-phase architecture, HW profile table summary
- [Scheduling Algorithm](algorithm.md) — priority list scheduling, resource vector usage
- [Scoreboards & Barriers](scoreboards.md) — scoreboard encoding, dependency barriers
- [SASS Encoding](../codegen/encoding.md) — control word format in SASS binary
- [Targets Index](../targets/index.md) — SM architecture map and version codes
- [Knobs](../config/knobs.md) — scheduling knobs (740, 741, 805, 806, etc.)
