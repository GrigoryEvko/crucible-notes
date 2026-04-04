# SM100 Blackwell

The SM100 architecture (Blackwell, datacenter) represents the largest single-ISA definition embedded in nvlink v13.0.88. The instruction encoding infrastructure spans approximately 8 MB of `.text` across four major regions, defines 3,200+ encoding/decoding functions, 118 instruction families organized under 3 major opcodes, and introduces SM100-specific features including tcgen05 tensor intrinsics, unified tensor core (UTC) instructions, and a new ROT13-obfuscated mnemonic set internally codenamed "MERCURY" (`ZREPHEL` in ROT13).

This page documents the complete ISA encoding layer as reconstructed from the embedded ptxas backend, covering the 128-bit instruction word format, the opcode space partitioning, the encoder/decoder/descriptor infrastructure, and all SM100-specific instruction families.

## Sub-variant Matrix

nvlink registers three SM100 sub-variants through the architecture dispatch at `sub_15C0CE0`:

| Sub-variant | ELF Flag | Description |
|---|---|---|
| `sm_100` | Base | Full Blackwell datacenter ISA |
| `sm_100a` | Accelerated | Enables all SM100 features including tcgen05 MMA |
| `sm_100f` | Forward-compatible | Feature-subset for forward binary compatibility |

All three sub-variants share the same encoding tables. The `a` suffix enables the full feature set; the `f` suffix restricts to the forward-compatible subset. The architecture dispatch registers 7 callbacks per sub-variant (nv.info emitter, resource usage table, instruction encoding table, compute capability array, perf-stats handler, cpf_optx handler, codegen options).

## Instruction Word Format

All SM100 SASS instructions are 128 bits (16 bytes) wide. The instruction word is stored as two 64-bit halves at offsets `+544` (bits 0-63) and `+552` (bits 64-127) in the internal instruction representation structure.

### Fixed Opcode Fields (Bits 0-31)

The first 32 bits encode the instruction identity through five fields set by the first five calls to the bitfield insertion primitive `sub_4C28B0`:

```
127                              64 63                                0
+----------------------------------+----------------------------------+
|     Modifier / Operand Fields    |  Fmt2 | Mod | SubOp | Minor | EF | MajOp |
+----------------------------------+----------------------------------+

Bit Range    Width   Name              Description
---------    -----   ----              -----------
  [3:0]      4 bits  Major Opcode      Instruction format class (1, 2, or 3)
  [6:4]      3 bits  Encoding Format    Sub-format / encoding variant (usually 0)
  [16:8]     9 bits  Minor Opcode      Instruction family (118 unique values)
  [24:17]    8 bits  Sub-Opcode        Specific instruction within family
  [31:25]    7 bits  Modifier           Data type, addressing mode, operation variant
```

### Major Opcode Distribution

| Major Opcode | Class | Encoder Count | Percentage | Description |
|---|---|---|---|---|
| 1 | ALU/Scalar | 558 | 37.2% | Integer arithmetic, float arithmetic, conversion, comparison, bitfield, shift, move, special register |
| 2 | Vector/Memory/Control | 977 | 62.7% | Memory load/store, texture, tensor core, control flow, barrier, predicate, warp shuffle, async copy |
| 3 | Special | 2 | 0.1% | Half-precision extended format (HSETP2 wide variant only) |

### Encoding Formats

| Format | Encoder Count | Typical Usage |
|---|---|---|
| Format 1 | 308 | Simpler register-register forms, 3-operand ALU, branches |
| Format 2 | 1,227 | Extended forms with immediates, predicated variants, memory ops |
| Format 3 | 1 | Wide format -- only `HSETP2` (opcode 0x6F sub 0x04, half-precision paired comparison) |

### Operand Field Encoding

Bits 32-127 encode operand references, modifier flags, and immediates. The layout varies by instruction class but follows consistent patterns:

| Bit Range | Typical Content |
|---|---|
| 48-53 | Source operand modifiers (negate, absolute value, swizzle) |
| 54-57 | Rounding mode, conversion type |
| 58-63 | Destination modifiers, saturation |
| 64-79 | Extended opcode / sub-function qualifiers (word 1) |
| 80-127 | Instruction-specific operand fields, immediates |
| 134-132 (offset 0x84) | Format2 field -- secondary format indicator (3 bits) |

### Register Operand Types

The register encoder `sub_4C4D60` writes a structured operand field at the specified bit offset:

| Field | Width | Values |
|---|---|---|
| is_output | 1 bit | 0=source, 1=destination |
| register type | 4 bits | 0=GPR, 2=uniform, 3=pair, 4=quad, 5=predicate, 7=barrier, 8=special, 10=64-bit, 11=128-bit |
| register number | 10 bits | 0-1023 |

The decoder `sub_4C60F0` uses a register class parameter: 2=GPR, 3=predicate, 9=constant buffer reference, 10=uniform register.

## Encoding Infrastructure

The SM100 ISA definition is split across four binary regions, totaling ~8 MB of template-instantiated functions:

### Region Map

| Address Range | Size | Content | Functions |
|---|---|---|---|
| `0x620000`--`0x84DD70` | 2.2 MB | SM100+ SASS encoders (table 1) | 1,537 |
| `0x84DD70`--`0xA48290` | 1.7 MB | InstrDesc initializers | 1,613 |
| `0xDA0000`--`0xE436D0` | 660 KB | SASS encoders (table 2) | 438 |
| `0xE43DC0`--`0xF15A50` | 840 KB | SASS decoders | 648 |

Combined totals: **1,975 encoder functions**, **648 decoder functions**, **1,613 descriptor initializers** = **4,236 template-instantiated functions**.

### Encoder Architecture

Every encoder follows an identical structural template:

```
__int64 __fastcall encode_XXX(__int64 buf, __int64 ir_instr)
{
    sub_4C2A60(buf);                          // Initialize encoding buffer
    sub_4C28B0(buf, 0, 4, major_opcode);      // Set bits[3:0] = major
    sub_4C28B0(buf, 4, 3, 0);                 // Set bits[6:4] = format ext
    sub_4C28B0(buf, 8, 9, minor_opcode);      // Set bits[16:8] = minor
    sub_4C28B0(buf, 17, 8, sub_opcode);       // Set bits[24:17] = sub
    sub_4C28B0(buf, 25, 7, modifier);         // Set bits[31:25] = modifier
    sub_4C2A90(buf, ir_instr, pred_idx);      // Encode predicate guard
    sub_4C4D60(buf, ir_instr, 0, 0x50);       // Encode register operand 0
    sub_4C4D60(buf, ir_instr, 1, 0x60);       // Encode register operand 1
    sub_4C52F0(buf, ir_instr, 2, 0x70);       // Encode immediate operand
    sub_4C5C30(buf, ir_instr, 3, 0x80);       // Encode predicate operand
    // ... instruction-specific modifier encoding via sub_A4xxxx/sub_A5xxxx ...
    sub_A50D10(lookup_table, opcode_value);    // Primary opcode mapping
}
```

#### Core Infrastructure Functions

| Address | Signature | Usage | Description |
|---|---|---|---|
| `sub_4C28B0` | `(buf, bit_offset, width, value)` | All encoders | Bitfield insertion into 128-bit instruction word |
| `sub_4C2A60` | `(buf)` | All encoders | Clear operand remap table (offsets 468-531, 16 DWORD slots), reset operand counter at offset 532 |
| `sub_4C2A90` | `(buf, ir, variant)` | All encoders | Encode predicate register + 5-bit scheduling field; variant 0=standard, 1=extended immediate |
| `sub_4C4D60` | `(buf, ir, op_idx, offset)` | 1,964/1,975 | Register operand encoder: 1-bit output flag + 4-bit type + 10-bit register number |
| `sub_4C52F0` | `(buf, ir, op_idx, offset)` | 715/1,975 | Constant/immediate operand encoder: 5-bit type + register number |
| `sub_4C5C30` | `(buf, ir, op_idx, offset)` | 875/1,975 | Predicate/special operand encoder with operand remapping |

#### Modifier Encoding Helpers

| Address | References | Modifier |
|---|---|---|
| `sub_A50D10` | 1,975 (all) | Primary opcode mapping through lookup table |
| `sub_A50CF0` | 267 | Secondary/auxiliary opcode bits |
| `sub_A4D6A0` | 86 | Rounding mode |
| `sub_A4D920` | 81 | Type conversion |
| `sub_A4F1C0` | 75 | Destination negate |
| `sub_A4F210` | 75 | Absolute value |
| `sub_A4DDD0` | 65 | Saturation |
| `sub_A4F120` | 66 | Source negate |
| `sub_A4D7D0` | 43 | Data type size |

### Decoder Architecture

Decoders mirror encoders -- each unpacks a 128-bit instruction word into the internal IR:

```
__int64 __fastcall decode_XXX(__int64 buf, __int64 ir_out)
{
    sub_4C5F90(buf, ir_out);                       // Finalize init
    sub_4C60F0(buf, ir_out, 0, 0x50, 2);           // Decode register (GPR)
    sub_4C60F0(buf, ir_out, 1, 0x60, 10);          // Decode register (uniform)
    sub_4C6380(buf, ir_out, 2, 0x70, 9);           // Decode constant buffer
    sub_4C6DC0(buf, ir_out, 3, 0x80, 3);           // Decode predicate
    sub_50C790(lookup, bit_val);                    // Read 1-bit flag
    sub_50BBA0(lookup, val);                        // Read 5-bit predicate field
    // ... field extraction via sub_50xxxx/sub_51xxxx ...
}
```

| Address | Usage | Description |
|---|---|---|
| `sub_4C5F90` | All 648 decoders | Finalize instruction decoding |
| `sub_4C60F0` | 634/648 | Decode register operand with class parameter |
| `sub_4C6380` | 288/648 | Decode constant buffer operand |
| `sub_4C6DC0` | 321/648 | Decode predicate/barrier operand |
| `sub_50C790` | All 648 | Read 1-bit field from encoded instruction (universal) |
| `sub_50C770` | 188/648 | Read multi-bit flag field |
| `sub_50BBA0` | 101/648 | Read 5-bit predicate register field |

### Dispatch Functions

Two functions serve as the entry points for the entire encoding/decoding pipeline:

**Encoder dispatch** (`sub_E43C20`, 92 lines): Reads the instruction opcode from `*(a2+12)` as a 16-bit value. Special-cases opcodes 120-121 (control flow) to `sub_4C8810`. Otherwise performs binary search in the 24-byte dispatch table at `off_1EA4380`. Each table entry contains `{opcode_byte0, opcode_byte1, encode_func_ptr, this_offset}`. Supports vtable-aware dispatch (checks LSB of function pointer).

**Decoder dispatch** (`sub_EFE6C0`, 93 lines): Reads the 128-bit instruction word from `a1[68]` (offset 544). Extracts the format field `(bits>>4)&7` and immediate offset `16*(bits&0xF)`. Delegates format 2/3 to `sub_4CB100`. Extracts the 9-bit major opcode `(bits>>8)&0x1FF`. For format 1, adds 121 to create a separate opcode space. Binary search in dispatch table at `off_1E957E0`. Calls `sub_A49B50` to finalize.

### InstrDesc Initializers

The 1,613 descriptor initializers at `0x84DD70`--`0xA48290` populate instruction metadata objects defining:

- Operand count and types (register, immediate, memory, predicate)
- Instruction properties (flags, latencies, execution unit assignment)
- Scheduling hints and resource requirements

Each descriptor writes a class ID to `*(a2+12)`. The class distribution spans 56 unique IDs (0-103), with ID 18 being the largest (78 descriptors, integer arithmetic class).

## Complete Instruction Set Map

The following tables enumerate all 118 instruction families across the three major opcodes. Within each family, sub-opcodes define specific instruction variants (register-register, register-immediate, predicated, etc.).

### Major Opcode 1: ALU/Scalar (558 encoders)

| Minor | Family | Variants | Operand Range | Description |
|---|---|---|---|---|
| 0x01 | ALU-MISC-A | 2 | R1-2 | Miscellaneous ALU type A |
| 0x02 | ALU-MISC-B | 7 | R2-3, I0-1 | Miscellaneous ALU type B |
| 0x03 | ALU-MISC-C | 1 | R2 | Single-variant ALU |
| 0x04 | INT-SHIFT | 6 | R2-3, I0-1 | Integer shift operations |
| 0x06 | INT-ARITH | 12 | R2-3, I0-1 | Integer arithmetic (ADD, SUB) |
| 0x08 | INT-MUL | 3 | R2-3 | Integer multiply |
| 0x09 | INT-MAD-EXT | 26 | R2-4, I0-1 | Integer multiply-add extended -- largest family in major 1 |
| 0x0A | FLOAT-CVT | 3 | R2-3 | Float conversion |
| 0x0C | CVT | 1 | R2 | Type conversion |
| 0x0D | INT-MAD | 10 | R2-3, I0-1 | Integer multiply-add |
| 0x0E | INT-WIDE | 6 | R2-3, I0-1 | Integer wide operations |
| 0x0F | INT-CMP | 2 | R2-3 | Integer comparison |
| 0x10 | IMM-LOAD | 4 | R2-3, I1 | Immediate value load |
| 0x12 | BIT-OP | 4 | R2-3 | Bit manipulation |
| 0x17 | SHIFT-EXT | 8 | R2-4, I0-1 | Extended shift operations |
| 0x18 | ISCADD/LEA | 10 | R2-4, I0-1, P0-1 | Integer scale-add / load effective address |
| 0x19 | SHIFT-WIDE | 5 | R2-3, I0-1 | Wide shift variants |
| 0x1A | CONST-LOAD | 4 | R2-3, I0-1 | Constant buffer load |
| 0x1B | INT-ABS | 3 | R2-3 | Integer absolute value |
| 0x1C | INT-NEG | 1 | R2 | Integer negate |
| 0x1D | INT-MIN/MAX | 1 | R2 | Integer minimum/maximum |
| 0x1E | BIT-EXT | 4 | R2-3, I0-1 | Bit field extract/insert |
| 0x22 | INT-SPECIAL | 1 | R2 | Special integer operation |
| 0x24 | INT-DIV | 2 | R2-3 | Integer divide helpers |
| 0x27 | LD-GLOBAL-128 | 2 | I4 | 128-bit global memory load (immediate-only form) |
| 0x33 | UNIFORM-ALU | 2 | R2-3 | Uniform register ALU |
| 0x39 | WARP-VOTE | 2 | R2-3, P1 | Warp voting operations |

### Major Opcode 2: Vector/Memory/Control (977 encoders)

| Minor | Family | Variants | Operand Range | Description |
|---|---|---|---|---|
| 0x27 | P-LD-128 | 2 | R3, I1, P1 | Predicated 128-bit load |
| 0x29 | ATOM-SHARED | 24 | R2-3, I1-2, P0-1 | Shared memory atomic operations |
| 0x3B | TEXTURE-SURF | 10 | R4-5, I1, P0-1 | Texture/surface operations |
| 0x3E | EXIT/BRK | 2 | R5, P1, M1 | Exit and break (7 operands each) |
| 0x3F | CONT | 1 | R5, P1, M1 | Continue (7 operands) |
| 0x40 | LONGJMP | 1 | R5, P1, M1 | Long jump |
| 0x42 | PREBRK | 1 | R5, P1, M1 | Pre-break |
| 0x43 | PCNT | 1 | R5, P1, M1 | Pre-continue |
| 0x44 | PRET | 1 | R5, P1, M1 | Pre-return |
| 0x45 | SSY | 1 | R5, P1, M1 | Set synchronization point |
| 0x46 | CAL | 1 | R5, P1, M1 | Function call |
| 0x49 | ATOM-GLOBAL | 6 | R2-3, I1-2, P0-1 | Global memory atomic operations |
| 0x4A | REDUCE | 4 | R2-3, I0-1 | Reduction operations |
| 0x4C | MEM-FENCE | 4 | R2-3, I0-1, P0-1 | Memory fence instructions |
| 0x4E | CORE-VEC | 63+ | R3-4, I1, P1 | Core vector ALU -- **largest family** |
| 0x4F | CORE-VEC-EXT | 25 | R2-4, I0-2, P0-1 | Extended core vector ALU |
| 0x52 | INT-SET-PRED | 6 | R2-3, I0-1 | Integer set predicate |
| 0x54 | LOAD-SHARED-EXT | 6 | R3, P1 | Extended shared memory load |
| 0x56 | WARP-BARRIER | 4+ | R1-2, P0-1 | Warp-level barrier operations |
| 0x59 | MMA-TENSOR | 30 | R4-7, I0-1, P0-2 | Tensor core MMA -- 2nd largest family |
| 0x5C | LOAD-GLOBAL | 6 | R3-4, P0-1 | Global memory load |
| 0x61 | STORE-GLOBAL | 5 | R1-3, I1-4, P0-2 | Global memory store |
| 0x62 | STORE-SHARED | 6 | R2-3, P0-2 | Shared memory store |
| 0x6B | TEXTURE-LOAD | 4 | R4-7, I1, P0-1 | Texture load operations |
| 0x6D | SURFACE-OP | 5 | R4-7, I1, P0-1 | Surface load/store |
| 0x70 | ASYNC-COPY | 4 | R2-3, I1, P0-1 | Asynchronous memory copy |
| 0x71 | WARP-GROUP | 3 | R2-3, P0-1 | Warp group operations |
| 0x7E | BARRIER-OP | 6 | R1-2, P0-1 | Barrier operations |
| 0x81 | UNIFORM-PRED | 4 | R0-1, I1, P0-1 | Uniform predicate operations |
| 0x84 | CONTROL-FLOW | 5 | R0-3, I0-1, P0-1 | Branch, call, return |
| 0x94 | TMA-ACCESS | 4 | R3-4, P0-1 | Tensor memory accelerator access |
| 0x95 | DEPBAR | 6 | R0-2, P0-1 | Dependency barrier |
| 0xA4 | HMMA-A | 5 | R5-6, I1, P0-1 | Half-precision MMA type A |
| 0xA5 | HMMA-B | 4 | R4-5, I0-1, P0-1 | Half-precision MMA type B |
| 0xA6 | IMMA | 3 | R4-5, P0-1 | Integer MMA |
| 0xA8 | MMA-F16xF16 | 8 | R5-6, I1, P1-2 | FP16 x FP16 matrix multiply |
| 0xA9 | MMA-MIXED | 8 | R4-5, I0-1, P0-1 | Mixed-precision MMA |
| 0xAB | MMA-INT | 4 | R4-5, P0-1 | Integer matrix multiply-accumulate |
| 0xAC | MMA-INT-V | 8 | R4-5, P0-1 | Integer MMA variant |
| 0xAD | LOAD-UNIFORM | 9 | R1-2, P0-1 | Uniform register load |
| 0xAE | MMA-TF32xTF32 | 10 | R5-6, I1, P1-2 | TF32 x TF32 MMA |
| 0xAF | MMA-F64xF64 | 10 | R5-6, I1, P1-2 | FP64 x FP64 MMA |
| 0xB0 | MMA-REDUCED | 10 | R4-6, I0-1, P1-2 | Reduced-precision MMA |
| 0xB1 | MMA-SPARSE | 5 | R4-5, P1-2 | Sparse MMA |
| 0xB2 | MMA-WIDE | 5 | R5-6, I1, P1-2 | Wide MMA |
| 0xB3 | MMA-SPECIAL | 5 | R4, P1 | Special MMA variant |
| 0xCF | PRED-LOGIC | 3 | R1-2, P0-1 | Predicate logic operations |
| 0xD4 | SELECT | 2 | R1-2, P0-1 | Conditional select / cmov |
| 0xDF | IMM-MOV | 14 | R1-2, I1-3, P0-1 | Immediate move -- most immediate-rich family |
| 0xE1 | STORE-EXT | 4 | R1-3, I1-3, P0-2 | Extended store operations |
| 0xEE | WARP-SHUFFLE | 3 | R2-3, P0-1 | Warp shuffle operations |

### Major Opcode 3: Special (1 encoder)

| Minor | Family | Variants | Operands | Description |
|---|---|---|---|---|
| 0x6F | HSETP2-WIDE | 1 | R4, I1, P2 | Half-precision set predicate, paired comparison -- the only format 3 instruction in the entire ISA |

## Instruction Families by Functional Class

The 118 instruction families group naturally into functional classes:

### Integer Arithmetic (13 families, ~110 encoders)

Integer arithmetic dominates the ALU opcode space. The key families:

- **IADD3** (minor 0x005 in the 0xDA region): 3-input integer add with carry. 26 encoding variants covering register-register, register-immediate, and different data widths. All variants include 1 immediate + 1 predicate + 3-4 register operands.
- **IMAD** (minors 0x016, 0x017): Integer multiply-add in standard (0x016, 7 variants) and wide (0x017, 6 variants) forms.
- **ISCADD/LEA** (minor 0x18): Integer scale-add / load effective address. 10 variants. Used extensively in address computation.
- **ALU-MISC** (minor 0x012 in 0xDA region): The largest single class with 63 distinct encodings. Includes BFE, BFI, FLO, POPC, LEA, PRMT, and many more.

### Floating-Point Arithmetic (5 families, ~30 encoders)

- **FADD** (minor 0x007): 11 variants covering FP32 add, multiply, FMA.
- **HADD2/HFMA2** (minors 0x06A, 0x06D): Half-precision packed arithmetic. 5 and 4 variants respectively. HFMA2 uses 2 predicate operands.
- **HSETP2** (minor 0x06F): Half-precision set predicate with paired output. 7 variants including the sole format 3 instruction.
- **DFMA** (minor 0x020): Double-precision FMA with 4 variants.

### Memory Operations (15 families, ~90 encoders)

- **LDG** (minor 0x025): Global memory load. 9 variants with up to 4 register operands.
- **STG** (minor 0x02E): Global memory store. 8 variants with up to 4 immediates.
- **LDS/STS** (minors 0x00D, 0x0E3, 0x0EC): Shared memory load and store. 6 variants each.
- **LDL** (minor 0x067): Local memory / stack. 13 variants with 2-4 immediates for complex addressing.
- **LDC** (minor 0x060): Constant buffer load. 6 variants, all with 4 immediate operands.
- **Async Copy** (minor 0x0B8): 6 variants for asynchronous data movement.

### Texture and Surface (5 families, ~30 encoders)

- **TEX** (minor 0x05A): Texture fetch. 13 variants. The most operand-rich instructions in the entire ISA: up to **7 register + 1 immediate + 1 predicate** operands. The largest encoder function (`sub_DC6680`, 8,794 bytes, 302 lines) belongs to this family.
- **TXQ** (minor 0x08B): Texture query. 4 variants with 4-7 register operands.
- **SUST** (minor 0x0CE): Surface store. 4 variants.
- **Texture/Surface load** (minor 0x03B): 6 variants.

### Tensor Core / MMA (16 families, ~120 encoders)

The tensor core families constitute the second-largest functional class:

| Family | Minor(s) | Variants | Description |
|---|---|---|---|
| HMMA-A/B | 0xA4, 0xA5 | 9 | Half-precision matrix multiply-accumulate |
| MMA-F16xF16 | 0xA8 | 6 | FP16 x FP16 with 5-6 register operands |
| MMA-MIXED | 0xA9 | 6 | Mixed-precision MMA |
| IMMA | 0xA6, 0xAB | 6 | Integer MMA |
| IMMA-V | 0xAC | 6 | Integer MMA variant |
| MMA-TF32 | 0xAE | 6 | TF32 x TF32 matrix multiply |
| MMA-F64 | 0xAF | 6 | FP64 x FP64 double-precision MMA |
| MMA-REDUCED | 0xB0 | 6 | Reduced-precision MMA |
| MMA-SPARSE | 0xB1 | 3 | Sparsity-aware MMA |
| MMA-WIDE | 0xB2 | 3 | Wide accumulator MMA |
| MMA-SPECIAL | 0xB3 | 1 | Special MMA variant |
| TENSOR-LD | 0xB4 | 2 | Tensor memory load |
| TENSOR-ST | 0xB6 | 2 | Tensor memory store |
| TMA | 0x09F | 6 | Tensor Memory Accelerator operations |
| WGMMA | 0x0C2 | 2 | Warp Group MMA (Blackwell-new) |

### Control Flow (10 families, ~25 encoders)

- **BRA** (minor 0x034): Branch with 3 variants (conditional, unconditional, indirect).
- **EXIT/BRK** (minor 0x3E): Exit/break with 7-operand encoding (5 registers + 1 predicate + 1 memory).
- **CALL/RET** (minors 0x079, 0x07B): Function call and return.
- **SSY/CAL/PCNT/PRET** (minors 0x42-0x46): Structured control flow primitives, each with 7 operands.
- **YIELD/NANOSLEEP** (minor 0x07C): Thread yield with 2 variants.

### Synchronization (6 families, ~25 encoders)

- **BAR.SYNC** (minor 0x085): Barrier synchronization.
- **WARP-BARRIER** (minor 0x56): Warp-level barrier with 4+ variants.
- **DEPBAR** (minor 0x096): Dependency barrier for scoreboard management.
- **BARRIER-OP** (minor 0x0A7): Extended barrier operations.
- **FENCE** (minor 0x08D): Memory fence with 4 variants.

## SM100-Specific Instructions

### ROT13 Mnemonic Table

The SM100 opcode table constructor at `sub_1782540` (111,076 bytes, 3,227 lines) initializes ~400+ instruction mnemonics using ROT13 encoding. The ROT13 prefix `ZREPHEL` decodes to `MERCURY`, the internal codename for Blackwell. Key SM100-specific mnemonics decoded from the binary:

| ROT13 | Decoded | Description |
|---|---|---|
| `OZZN` | `BMMA` | Block matrix multiply-accumulate |
| `QZZN` | `DMMA` | Dense matrix multiply-accumulate |
| `DZZN` | `QMMA` | Quantized matrix multiply-accumulate |
| `GPTRA05` | `TCGEN05` | Tensor Core Generation 5 intrinsic |
| `HGPONE` | `UTCBAR` | Unified Tensor Core barrier |
| `HGPPC` | `UTCPC` | Unified Tensor Core program counter |
| `HGPUZZN` | `UTCHMMA` | Unified Tensor Core half-precision MMA |
| `ZKEZN.FC` | `MXQMA.SP` | Mixed-precision quantized MMA, sparse variant |
| `FLAPF` | `SYNCS` | Synchronization primitives (Blackwell-specific) |
| `NPDOHYX` | `ACQBULK` | Acquire bulk (barrier operation) |
| `NPDFUZVAVG` | `ACQSHMINIT` | Acquire shared memory init |
| `NY2C` | `AL2P` | Attribute to parameter (legacy compat) |
| `NEEVIRF` | `ARRIVES` | Barrier arrival notification |
| `NGR` | `AST` | Attribute store |
| `NGBZ` | `ATOM` | Atomic operation |
| `NGBZT` | `ATOMG` | Atomic global |
| `SNQQ` | `FADD` | Float add |
| `SZHY` | `FMUL` | Float multiply |
| `VZNQ` | `IMAD` | Integer multiply-add |
| `VZNQ_JVQR` | `IMAD_WIDE` | Integer multiply-add wide |
| `VNQQ3` | `IADD3` | 3-input integer add |
| `OZFX` | `BMSK` | Bit mask |
| `FTKG` | `SGXT` | Sign extend |
| `YBC3` | `LOP3` | 3-input logic operation |
| `VFRGC` | `ISETP` | Integer set predicate |
| `PPGY` | `CCTL` | Cache control |
| `OFLAP` | `BSYNC` | Block synchronization |
| `SRAPR` | `FENCE` | Memory fence |
| `REEONE` | `ERRBAR` | Error barrier |

### tcgen05 Intrinsics

SM100 introduces the tcgen05 (Tensor Core Generation 5) subsystem with dedicated PTX-level intrinsics. The code generation infrastructure resides at `0x16E0000`--`0x16E3AB0` and includes:

**Instruction type classifier** (`sub_16E0A70`, 17,302 bytes, 322 lines): Chains ~50 type-check predicates against the instruction object to classify tcgen05 MMA variants into integer type IDs 1-54. Types 1-8 map to base types, 18-29 to extended variants. Classification keys include modifiers: `_expand16bit`, `_pack16bit`, `_maxabs`, `_minabs`, `_fused`, `_blockscale`, `_ashift`.

**Guardrails codegen** (`sub_16E1DB0`, 10,365 bytes, 325 lines): Generates boundary-checking code for tcgen05 tensor operations. Emits inline PTX with string templates:
- `__cuda__sm10x_tcgen05_guardrails_in_physical_bounds_` -- validates tensor memory addresses
- `__cuda__sm10x_tcgen05_guardrails_are_columns_allocated_` -- validates column allocation

**Tensor memory address computation** (`sub_16E2410`): Emits `add.u32 %s, %s, %s;` for tmem address calculation. References `__cuda_sm_100_tcgen05_tmem_addr`.

**Address base conversion** (`sub_16E2610`): Emits `cvt.u32.u64` for 64-bit to 32-bit address conversion for tmem operations.

**MMA operand setup** (four functions at `0x16E27D0`--`0x16E2D70`): Configure tensor memory for:
- `__cuda_sm10x_tcgen05_mma_tmemD` -- destination tensor memory
- `__cuda_sm10x_tcgen05_mma_tmemA` -- source A tensor memory
- `__cuda_sm10x_tcgen05_mma_scaleTmemA` -- scale tensor for matrix A
- `__cuda_sm10x_tcgen05_mma_scaleTmemB` -- scale tensor for matrix B

**Arguments type mapper** (`sub_16E3AB0`, 18,623 bytes, 337 lines): Maps arrays of tcgen05 instruction arguments to numeric type IDs. Iterates over the argument array (base offset 796 stores count, result at offset 944), calling the same type-check chain as the instruction classifier.

### Decoder Opcode Classes for Blackwell-New Instructions

The decoder region (`0xE43DC0`--`0xF15A50`) reveals Blackwell-specific instruction classes through high opcode IDs (>300) not present in earlier architectures:

| Decoder Opcode | Decoders | Description |
|---|---|---|
| 356 | 15 | Uniform compute extensions (new register file operations) |
| 357 | 6 | New barrier primitives |
| 358 | 6 | New synchronization primitives |
| 368 | 10 | Asynchronous copy/compute operations |
| 289-299 | ~20 | TMA (Tensor Memory Accelerator) operations |

## SM100 Compiler Backend

The SM100-specific compiler backend extends from `0x1782540` through `0x17B9300` and includes:

### Opcode Table Constructor

`sub_1782540` (111,076 bytes, 3,227 lines) is the SM100 opcode table constructor. It calls the parent class constructor `sub_19B11F0` and initializes all SM100 instruction mnemonics with name + length at offset +11,360. The vtable is at `off_2415E98`. This is over 3x larger than the SM70 equivalent (`sub_1769B50`, 24,230 bytes) reflecting the expanded ISA.

### Instruction Property Initializer

`sub_17884A0` (44,713 bytes, 1,603 lines) sets latencies, throughputs, and execution unit assignments for every SM100 opcode. Companion to the opcode table constructor.

### Scheduling Table

`sub_178AA00` (35,422 bytes, 1,205 lines) initializes the SM100-specific scheduling resource tables defining per-instruction throughputs and latencies for each execution unit type (ALU, SFU, LDST, TEX, MMA).

### SM100-Specific Optimization Passes

| Address | Size | Lines | Function |
|---|---|---|---|
| `sub_179BD10` | 16,544 B | 649 | SM100 peephole optimizer |
| `sub_179E620` | 10,854 B | 455 | SM100 instruction combiner |
| `sub_179EF10` | 9,214 B | 352 | SM100 pattern matcher |
| `sub_179F6D0` | 7,078 B | 294 | SM100 dead code handler |
| `sub_17A2130` | 33,823 B | 1,065 | SM100 instruction legalization (main) |
| `sub_17A7A40` | 17,754 B | 601 | SM100 type legalization |
| `sub_17A8610` | 29,094 B | 889 | SM100 lowering pass |
| `sub_17AB9D0` | 36,177 B | 1,221 | SM100 instruction selection |
| `sub_17ADA40` | 35,411 B | 1,270 | SM100 complex instruction selection |
| `sub_17B04A0` | 23,212 B | 871 | SM100 operand folding |

### Master Encoder Functions

Two monumental encoding functions handle the final SASS binary emission:

- `sub_17F2670` (156,611 bytes, 4,858 lines): The **master instruction encoder** -- the largest function in the entire nvlink binary. Dispatches to individual encoding routines for all SASS instruction types. ~640 local variables, 0x2C8-byte stack frame.
- `sub_17F9AE0` (61,531 bytes, 2,150 lines): Secondary encoder for less common instruction types.

## Format Descriptor Tables

Each instruction references a 16-byte (128-bit) format descriptor loaded via SSE instructions. These descriptors define the operand layout template:

### Encoder-Side Descriptors

| Address | Encoders | Usage |
|---|---|---|
| `xmmword_1E30DA0` | 166 | Default/common format |
| `xmmword_1E30E30` | 54 | Integer arithmetic formats |
| `xmmword_1E30DC0` | 34 | Memory operation formats |
| `xmmword_1E30DB0` | 19 | Comparison/predicate formats |
| `xmmword_1E30EF0` | 14 | MOV/immediate formats |
| `xmmword_1E30F10` | 16 | Integer extended formats |
| `xmmword_1E30E50` | 12 | Integer multiply formats |
| `xmmword_1E30EA0` | 10 | Bitfield/logic formats |

### Decoder-Side Descriptors

| Address | Decoders | Usage |
|---|---|---|
| `xmmword_1F46278` | 125 | Common 64-bit instruction format |
| `xmmword_1F46388` | 106 | Standard 3-operand format |
| `xmmword_1F46AF8` | 99 | Extended operand format |
| `xmmword_1F46630` | 84 | Memory instruction format |
| `xmmword_1F46E28` | 72 | Wide instruction format |
| `xmmword_1F461F0` | 64 | Predicate instruction format |

The universal operand descriptor at `xmmword_1F460E0`/`xmmword_1F460F0` (32 bytes) is referenced by all 4,236 encoder+decoder+descriptor functions -- it defines the register class mapping for the SM100 architecture.

## Internal Data Structures

### Instruction Representation Object (~560 bytes)

The `a1` parameter across all encoder/decoder functions points to:

```
Offset  Size   Description
------  ----   -----------
  0       4    Flags / instruction ID
  4       4    Scheduling control bits
  8      16    Format descriptor (128-bit SSE copy from xmmword table)
 12       4    Operand count metadata field 1
 16       4    Operand count metadata field 2
 24-60   40    Operand register indices (10 x 4 bytes)
 64-100  40    Operand type/class (10 x 4 bytes, -1 = unused)
104-140  40    Operand modifier flags (10 x 4 bytes)
144       4    Active operand count
148       4    Immediate encoding offset 1
152       4    Immediate encoding offset 2
156-276 120    Reserved / operand extension data
276-404 128    Decoded operand output buffer
404-468  64    Modifier flag output buffer
452       4    Modifier count field 1
456       4    Modifier count field 2
468-532  64    Operand remap table (16 DWORD slots)
532       4    Operand remap counter
536       8    Pointer to encoding/decoding lookup table
544       8    Instruction word 0 (bits 0-63 of 128-bit SASS)
552       8    Instruction word 1 (bits 64-127 of 128-bit SASS)
556       4    Decoded immediate / offset value
```

### Encoder Dispatch Table Entry (24 bytes)

```
Offset  Size  Description
------  ----  -----------
  0       1   Opcode byte 0 (minor opcode low)
  1       1   Opcode byte 1 (minor opcode high / sub-opcode)
  8       8   Function pointer to encoder (LSB=1 indicates vtable indirection)
 16       8   this-pointer offset adjustment
```

## Statistical Summary

| Metric | Value |
|---|---|
| Total encoding functions | 1,975 |
| Total decoding functions | 648 |
| Total descriptor initializers | 1,613 |
| **Total template instantiations** | **4,236** |
| Unique instruction families | 118 |
| Unique descriptor class IDs | 56 (ranging 0-103) |
| Largest encoder | `sub_DC6680` TEX (8,794 bytes, 302 lines) |
| Smallest encoder | ~4,661 bytes, 167 lines |
| Only format 3 instruction | HSETP2 (minor 0x6F, sub 0x04) |
| Max register operands | 7 (texture fetch) |
| Max immediate operands | 4 (constant buffer load, local memory) |
| Max predicate operands | 2 (half-precision comparison, store ops) |
| SM100 opcode table size | 111,076 bytes / 3,227 lines |
| SM70 opcode table size | 24,230 bytes / 733 lines |
| SM100-to-SM70 ISA size ratio | ~4.6x |
