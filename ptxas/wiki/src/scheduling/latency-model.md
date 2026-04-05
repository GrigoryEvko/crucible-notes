# Latency Model & Functional Units

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

The switch statement maps Ori opcodes to scheduling class IDs. These IDs are stored at `*(v8+4)` where `v8` is a pointer to the instruction's extended scheduling record. Representative mappings:

| Ori opcode | Scheduling class | Execution pipe | Description |
|---|---|---|---|
| 1 | 130 | sub-class 1 (0x10) | Control flow (BRA, JMP) |
| 2--7 (wide) | 683 | sub-class 4 (0x40), pipe 0xA | Wide FP64 operations |
| 2--7 (narrow, type 19) | 52 | sub-class 2 (0x20) | Integer ALU (narrow) |
| 2--7 (narrow, other) | 72 | sub-class 2 (0x20) | Integer ALU (standard) |
| 3, 5 (medium) | 140 | sub-class 3 (0x30) | FP32 operations |
| 4 (medium) | 131 | sub-class 2 (0x20) | Integer MAD |
| 6 (wide) | 140 | sub-class 4 (0x40), pipe 0xA | FP64 pair operations |
| 8 (flag bit set) | 3 | default | Predicate operations (true) |
| 8 (flag clear) | 2 | default | Predicate operations (false) |
| 0xA, 0xB, 0x6C, 0x95 | 200 | sub-class 2 (0x20) | Integer compare/logic |
| 0xA (extended) | 551 | default | Extended integer (wide encoding) |
| 0xA (extended, Mercury) | 694/700 | default | Mercury-era extended integer |
| 0xE | 5 | default | Conversion operations |
| 0x10 (atomic) | 575 | default | Atomic with flag |
| 0x10 (global) | varies | sub-class 4 (0x40) | Global memory load/store |
| 0x141 | 745 | latency 0xF1 | WGMMA (warpgroup MMA) |
| 0x142 (variant 3) | 744 | latency 0xF0 | WGMMA variant |
| 0x143 | 765--767 | latency 0xFB | BGMMA/QMMA tensor variants |
| 0x144 | 600 | latency 0xE6 | Tensor fence |
| 0x145, 0x146 | 759 | sub-class 4, pipe 0xC | Tensor core (HMMA/BMMA) |
| 0x147, 0x148 (wide) | 761 | latency 0xFA | Double-precision tensor (wide) |
| 0x147, 0x148 (narrow) | 757 | latency 0xF6 | Double-precision tensor (narrow) |
| 0x149 | 604 | latency 0xE7 | Tensor synchronization |
| 0x13E | 749 | latency 0xF4 | Bulk copy (ACQBULK) |
| 0x13F | 748 | latency 0xF3 | Bulk release (RELBULK) |
| 0x13D (variant) | 747/750 | latency 0xF2/0xF5 | Collective operations |

The scheduling class IDs span a wide range (2--772+). Classes below 256 correspond to legacy instruction categories; higher classes (551, 575, 600, 683, 694, 700, 744--767) represent newer instruction types added for Hopper and Blackwell architectures.

### Latency Index Encoding

The low 9 bits of the descriptor at `a3+196` encode a latency index that maps directly into the per-architecture HW table. The index is formed by combining the descriptor's low byte with a pipe mask:

```
latency_index = *(WORD*)(a3+196) & 0x1FF
```

Observed latency index values and their instruction classes:

| Index (hex) | Index (dec) | Instruction class |
|---|---|---|
| 0xE6 | 230 | Tensor fence / sync |
| 0xE7 | 231 | Tensor synchronization |
| 0xF0 | 240 | WGMMA variant |
| 0xF1 | 241 | WGMMA primary |
| 0xF2 | 242 | Collective op (variant A) |
| 0xF3 | 243 | Bulk release |
| 0xF4 | 244 | Bulk copy |
| 0xF5 | 245 | Collective op (variant B) |
| 0xF6 | 246 | DP tensor (narrow) |
| 0xF8 | 248 | Tensor core (HMMA/BMMA) |
| 0xFA | 250 | DP tensor (wide) |
| 0xFB | 251 | BGMMA/QMMA |

The highest index values (0xE6--0xFB) correspond to tensor and collective operations -- the most complex instructions with the longest and most architecture-variable latencies.

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

Each record in the HW table occupies 96 bytes (6 x 16-byte XMM slots):

```
Offset  Size  Content
 0..15  16B   Header: record type (WORD at +0), flags, size fields
16..31  16B   Latency/throughput data
32..39  8B    Pointer to parent record or scheduling class
40..47  8B    Size/count field (e.g., 128 for barrier entry)
48      1B    Active flag
88      1B    String-backed flag (1 = has allocated string name)
80..87  8B    Pointer to name string (if string-backed)
```

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

| Address | Size | Identity |
|---|---|---|
| `sub_693BC0` | 22 lines | MemorySpaceClassify -- return memory space code |
| `sub_695530` | 606 lines | ComputeLatencies -- per-BB latency computation |
| `sub_704D30` | 14 KB | GetFunctionalUnit -- SASS opcode to FU mapping |
| `sub_73A1D0` | ~6 KB | LDSLatencyStats -- shared memory latency stats |
| `sub_73A7F0` | ~6 KB | LDGLatencyStats -- global memory latency stats |
| `sub_73ADF0` | 6.5 KB | XU64LatencyStats -- extended unit latency stats |
| `sub_73B360` | 28.7 KB | MacLoopSchedulingAnalytics -- latency hiding report |
| `sub_799860` | 2.9 KB | ClassifyInstructionLatency |
| `sub_89FBA0` | 85 KB | **SetOpcodeLatencies** -- per-opcode scheduling class |
| `sub_8B5400` | 14 KB | ScheduleForLatency -- latency-optimized scheduling |
| `sub_8B77C0` | 15 KB | DualIssueScheduler -- dual-issue scheduling engine |
| `sub_8BDC40` | 7.9 KB | DualIssuePairing -- instruction pair selection |
| `sub_8C67A0` | 3.7 KB | ComputeResourceCost -- per-instruction FU cost |
| `sub_8C7290` | 5.1 KB | GetResourceVector -- SSE-optimized copy |
| `sub_8CCF80` | 2.3 KB | IsLongLatencyOp -- latency > 19 check |
| `sub_8CF5D0` | 3.5 KB | CheckDualIssueEligibility |
| `sub_8D3E20` | 2.1 KB | ComputeStallCycles -- required stall count |
| `sub_8D7760` | 41 KB | StallAndBarrierInsertion -- encode stalls/barriers |
| `sub_8E3AD0` | -- | CopyProfileEntries -- finalize HW table |
| `sub_8E4400` | 3.3 KB | **InitHWProfile\_Warp** -- warp dispatch params |
| `sub_8E4920` | 6.9 KB | BuildScoreboardEntries -- scoreboard BST |
| `sub_8E5CA0` | 20 KB | **EmitScheduleOutput** -- scheduling control words |
| `sub_8E6760` | 2.9 KB | EmitGroupBoundary -- group boundary marker |
| `sub_8E6B40` | 2.9 KB | EmitSchedEntry -- standard scheduling entry |
| `sub_8E6D40` | 2.9 KB | EmitBarrierEntry -- barrier/sync entry |
| `sub_8E6F20` | 2.9 KB | EmitWaitEntry -- wait dependency entry |
| `sub_8E7110` | 2.9 KB | EmitScoreboardEntry -- scoreboard entry |
| `sub_8E7300` | 3.3 KB | HWTable\_sm70 -- Volta latency table |
| `sub_8E7540` | 2.9 KB | HWTable\_sm72 -- Xavier latency table |
| `sub_8E7720` | 3.5 KB | HWTable\_sm75 -- Turing latency table |
| `sub_8E7940` | 2.9 KB | HWTable\_sm80\_base -- Ampere base table |
| `sub_8E7B40` | 3.3 KB | HWTable\_sm80 -- Ampere full table |
| `sub_8E7D80` | 4.4 KB | HWTable\_sm86 -- GA10x table |
| `sub_8E8070` | 3.5 KB | HWTable\_sm87 -- Orin table |
| `sub_8E8280` | 3.1 KB | HWTable\_sm89 -- Ada Lovelace table |
| `sub_8E8480` | 5.2 KB | HWTable\_sm90 -- Hopper table |
| `sub_8E8780` | 4.6 KB | HWTable\_sm90a -- Hopper accelerated table |
| `sub_8E8A90` | 3.0 KB | HWTable\_sm100 -- Blackwell DC table |
| `sub_8E8CB0` | 949 B | HWTable\_sm100\_short -- Blackwell supplementary |
| `sub_8E8DB0` | 1.7 KB | HWTable\_sm103 -- Blackwell Ultra table |
| `sub_8E8F60` | 618 B | HWTable\_sm103\_short -- BU supplementary |
| `sub_8E9000` | 2.9 KB | HWTable\_sm120 -- RTX 50xx table |
| `sub_8E92E0` | 5.5 KB | HWTable\_sm120\_ext -- RTX 50xx extended |
| `sub_8E97B0` | 8.8 KB | HWTable\_universal -- fallback table |
| `sub_8E9DC0` | 4.8 KB | EmitLatencyEntry -- HW table entry helper |
| `sub_8EFA10` | 18 KB | EmitScheduleReport -- statistics output |
| `sub_8F0CD0` | 24 B | MapFUClassID -- (opcode, name) to class |
| `sub_8F1EB0` | 15 KB | EncodeScheduleWords -- SASS control word output |
| `sub_8F3130` | 1.0 KB | EncodeStallField |
| `sub_8F31F0` | 6.1 KB | EncodeBarrierField |
| `sub_8F3650` | 2.7 KB | EncodeYieldField |
| `sub_8F3860` | 3.0 KB | EncodeScoreboardField |
| `sub_8F4140` | 5.6 KB | EncodeFullControlWord |
| `sub_8F47E0` | ~50 B | DetectCutlass -- strstr for "cutlass" |
| `sub_A08910` | 39 lines | GetRegisterLatency -- operand cost query |
| `sub_A08A00` | 345 lines | ResourceModel -- 3-mode FU cost computation |
| `sub_A09530` | 91 lines | UpdateStallCycles -- per-instruction stall update |
| `sub_A9CDE0` | -- | IsHotMemory -- global/texture classification |
| `sub_A9CF90` | -- | IsColdMemory -- constant/shared classification |
| `sub_13710B0` | 7.1 KB | **AssignPipeClass** -- SASS-level pipe assignment |
| `sub_1370F40` | ~500 B | CheckTensorFeature -- gates tensor pipe classes |
| `sub_7D6780` | ~100 B | IsWideType -- true for FP64/wide types |
| `sub_7DFFC0` | ~200 B | ClassifyMemAccess -- 3=shared, 4=constant |
| `sub_7E3640` | ~100 B | GetCustomPipe -- 5-bit pipe sub-class |
| `sub_91E7A0` | ~100 B | GetSrcEncoding -- source operand encoding query |
| `sub_91E860` | ~100 B | GetOperandType -- operand type code |
| `sub_A9AB10` | ~100 B | NeedsExtEncoding -- extended encoding check |

## Cross-References

- [Scheduler Overview](overview.md) -- 3-phase architecture, HW profile table summary
- [Scheduling Algorithm](algorithm.md) -- priority list scheduling, resource vector usage
- [Scoreboards & Barriers](scoreboards.md) -- scoreboard encoding, dependency barriers
- [SASS Encoding](../codegen/encoding.md) -- control word format in SASS binary
- [Targets Index](../targets/index.md) -- SM architecture map and version codes
- [Knobs](../config/knobs.md) -- scheduling knobs (740, 741, 805, 806, etc.)
