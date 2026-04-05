# Instructions & Opcodes

This page documents the Ori IR instruction representation: in-memory layout, opcode encoding, operand model, instruction flags, creation/iteration APIs, the master descriptor table, and opcode categories. All offsets are from `ptxas v13.0.88` (37.7 MB stripped x86-64 ELF).

## Instruction Object Layout

Every Ori instruction is a 296-byte C++ object allocated from the Code Object's arena. Instructions are linked into per-basic-block doubly-linked lists via pointers at offsets +0 and +8. The allocator at `sub_7DD010` allocates exactly 296 bytes per instruction and zeroes the object before populating it.

### Memory Layout (296 bytes)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +0 | 8 | `ptr` | `prev` | Previous instruction in BB linked list (`nullptr` for head) |
| +8 | 8 | `ptr` | `next` | Next instruction in BB linked list (`nullptr` for tail) |
| +16 | 4 | `i32` | `id` | Unique instruction ID (monotonically increasing within function) |
| +20 | 4 | `i32` | `ref_count` | Reference/use count (incremented by `sub_7E6090`) |
| +24 | 4 | `i32` | `bb_index` | Basic block index (`bix`) this instruction belongs to |
| +28 | 4 | `u32` | `reserved_28` | Reserved / padding |
| +32 | 4 | `u32` | `control_word` | Scheduling control word (stall cycles, yield, etc.) |
| +36 | 4 | `u32` | `flags_36` | Instruction flags (bits 19-21 = subtype, see below) |
| +40 | 8 | `ptr` | `sched_slot` | Scheduling state pointer |
| +48 | 8 | `u64` | `flag_bits` | Extended flag bits (bit 5 = volatile, bit 27 = reuse) |
| +56 | 8 | `ptr` | `def_instr` | Defining instruction (for SSA def-use chains) |
| +64 | 8 | `ptr` | `reserved_64` | Reserved / register class info |
| +72 | 4 | `u32` | `opcode` | Full opcode word (lower 12 bits = base opcode, bits 12-13 = modifier) |
| +76 | 4 | `u32` | `opcode_aux` | Auxiliary opcode data (sub-operation, comparison predicate) |
| +80 | 4 | `u32` | `operand_count` | Total number of operands (destinations + sources) |
| +84 | var | `u32[N*2]` | `operands[]` | Packed operand array (8 bytes per operand slot) |
| +88 | 4 | `u32` | `operands[0].extra` | High word of first operand slot |
| +100 | 1 | `u8` | `type_flags` | Data type / modifier flags (bits 0-2 = data type code) |
| +104 | 4 | `u32` | `reserved_104` | Reserved |
| +112 | 8 | `ptr` | `use_chain` | Use chain linked list head (for CSE) |
| +120 | 8 | `ptr` | `reserved_120` | Reserved |
| +136 | 4 | `i32` | `reserved_136` | Reserved |
| +160 | 8 | `ptr` | `enc_buf` | Encoding buffer pointer (populated during code generation) |
| +168 | 8 | `ptr` | `reserved_168` | Reserved |
| +184 | 4 | `u32` | `enc_mode` | Encoding mode selector |
| +200 | 8 | `u64` | `imm_value` | Immediate value (for instructions with constant operands) |
| +208 | 16 | `xmm` | `sched_params` | Scheduling parameters (loaded via `_mm_load_si128`) |
| +240 | 4 | `u32` | `reserved_240` | Reserved |
| +244 | 1 | `u8` | `reserved_244` | Reserved |
| +248 | 8 | `i64` | `sentinel_248` | Initialized to `-1` (0xFFFFFFFFFFFFFFFF) |
| +256 | 8 | `i64` | `sentinel_256` | Initialized to `0xFFFFFFFF` |
| +264 | 8 | `i64` | `bb_ref` | Basic block reference / block index storage |
| +272 | 8 | `i64` | `reserved_272` | Reserved |
| +280 | 16 | `u128` | `reserved_280` | Zeroed on creation |

### Linked-List Pointers

Instructions form a doubly-linked list within each basic block. The Code Object stores the global list head at offset +272 and tail at offset +280:

```
Code Object +272  -->  head instruction (prev = nullptr)
                            |
                            v  (+8 = next)
                       instruction 2
                            |
                            v
                       instruction 3
                            |
                            v  ...
Code Object +280  -->  tail instruction (next = nullptr)
```

The linked-list traversal pattern appears in hundreds of functions throughout ptxas:

```c
// Forward iteration over all instructions
for (instr = *(ptr*)(code_obj + 272); instr != nullptr; instr = *(ptr*)(instr + 8)) {
    uint32_t opcode = *(uint32_t*)(instr + 72);
    uint32_t num_ops = *(uint32_t*)(instr + 80);
    // process instruction...
}
```

## Opcode Encoding

The opcode field at offset +72 is a 32-bit word with a structured layout.

### Opcode Word Format

```
 31              16  15  14  13  12  11            0
+------------------+---+---+---+---+---------------+
|    upper flags   |   |   | M | M |  base opcode  |
+------------------+---+---+---+---+---------------+
                            ^   ^
                            |   bit 12: modifier bit 0
                            bit 13: modifier bit 1

M = modifier bits (stripped by the 0xFFFFCFFF mask)
base opcode = 12-bit instruction class identifier (0-4095)
```

The mask `0xFFFFCFFF` (clear bits 12-13) is used throughout `InstructionClassifier`, `MBarrierDetector`, `OperandLowering`, and many other subsystems to extract the base instruction class, stripping sub-operation modifier bits:

```c
uint32_t raw_opcode = *(uint32_t*)(instr + 72);
uint32_t base_opcode = raw_opcode & 0xFFFFCFFF;
```

Additionally, bit 11 is sometimes used in operand count calculations:

```c
// Effective operand count adjustment (appears in 50+ functions)
int adj = (*(uint32_t*)(instr + 72) >> 11) & 2;  // 0 or 2
int dst_count = *(uint32_t*)(instr + 80) - adj;
```

### Canonical Opcode Reference

The opcode value stored at instruction+72 is the same index into the ROT13 name table at `InstructionInfo+4184`. There is a single numbering system -- the ROT13 table index IS the runtime opcode. This was verified by tracing `sub_BEBAC0` (getName), which computes `InstructionInfo + 4184 + 16 * opcode` with no remapping.

The following table lists frequently-referenced opcodes from decompiled code, with their canonical SASS mnemonic names from the ROT13 table. Each opcode appears in 10+ decompiled functions reading `*(instr+72)`.

| Base Opcode | SASS Mnemonic | Category | Reference Count |
|-------------|---------------|----------|-----------------|
| 0 | `ERRBAR` | Error barrier (internal) | Sentinel in scheduler |
| 1 | `IMAD` | Integer multiply-add | 100+ functions |
| 7 | `ISETP` | Integer set-predicate | `sub_7E0030` switch |
| 18 | `FSETP` | FP set-predicate | `sub_7E0030` switch |
| 19 | `MOV` | Move | 80+ functions |
| 23 | `PLOP3` | Predicate 3-input logic | `sub_7E0030` case 23 |
| 25 | `NOP` | No-op | Scheduling, peephole |
| 52 | `AL2P_INDEXED` | BB boundary pseudo-opcode | `sub_6820B0`, 100+ |
| 54 | `BMOV_B` | Barrier move (B) | `sub_7E6090` case 54 |
| 61 | `BAR` | Barrier synchronization | Sync passes |
| 67 | `BRA` | Branch | `sub_74ED70`, CFG builders |
| 71 | `CALL` | Function call | `sub_7B81D0`, ABI, spill |
| 72 | `RET` | Return | `sub_74ED70` (with 67) |
| 77 | `EXIT` | Exit thread | `sub_7E4150`, CFG sinks |
| 93 | `OUT_FINAL` | Tessellation output (final) | `sub_734AD0`, 25+ |
| 94 | `LDS` | Load shared | `sub_7E0650` case 94 |
| 95 | `STS` | Store shared | `sub_7E0030`, 40+ |
| 96 | `LDG` | Load global | Memory analysis |
| 97 | `STG` | Store global | `sub_6820B0`, 30+ |
| 102 | `ATOM` | Atomic | Encoding switch |
| 104 | `RED` | Reduction | Encoding switch |
| 111 | `MEMBAR` | Memory barrier | Sync passes |
| 119 | `SHFL` | Warp shuffle | `sub_7E0030` case 119 |
| 122 | `DFMA` | Double FP fused mul-add | `sub_7E0030` case 122 |
| 130 | `HSET2` | Half-precision set (packed) | 20+ functions |
| 135 | `INTRINSIC` | Compiler intrinsic (pseudo) | ISel, lowering |
| 137 | `SM73_FIRST` | SM gen boundary (real instr) | Strength reduction |
| 183 | sm_82+ opcode | Extended mem operation | `& 0xFFFFCFFF` mask |

**Important caveats:**

1. **Opcode 52** (`AL2P_INDEXED` in name table) is universally used as a **basic block delimiter** in 100+ decompiled functions. The SASS mnemonic name may be vestigial; no decompiled code uses it for attribute-to-patch operations.

2. **SM boundary markers** (136=`SM70_LAST`, 137=`SM73_FIRST`, etc.) have marker names in the ROT13 table but are valid runtime opcodes. Instructions with these opcode values exist in the IR and are processed by optimization passes (e.g., strength reduction operates on opcode 137).

3. **Earlier versions of this page** had a "Selected Opcode Values" table that assigned incorrect SASS mnemonics based on behavioral inference rather than the ROT13 name table. Those labels (93=BRA/CALL, 95=EXIT, 97=CALL/label, 130=MOV) were wrong. The correct labels are: 93=`OUT_FINAL`, 95=`STS`, 97=`STG`, 130=`HSET2`. Branch/call/exit are at 67=`BRA`, 71=`CALL`, 77=`EXIT`.

### Opcode Ranges by SM Generation

The ROT13 opcode name table in `sub_BE7390` (`InstructionInfo` constructor) includes explicit SM generation boundary markers:

| Marker Opcode | Decoded Name | Meaning |
|---------------|--------------|---------|
| 136 | `SM70_LAST` | Last sm_70 (Volta) opcode |
| 137 | `SM73_FIRST` | First sm_73 (Volta+) opcode |
| 171 | `SM73_LAST` | Last sm_73 opcode |
| 172 | `SM82_FIRST` | First sm_82 (Ampere) opcode |
| 193 | `SM82_LAST` | Last sm_82 opcode |
| 194 | `SM86_FIRST` | First sm_86 (Ampere+) opcode |
| 199 | `SM86_LAST` | Last sm_86 opcode |
| 200 | `SM89_FIRST` | First sm_89 (Ada) opcode |
| 205 | `SM89_LAST` | Last sm_89 opcode |
| 206 | `SM90_FIRST` | First sm_90 (Hopper) opcode |
| 252 | `SM90_LAST` | Last sm_90 opcode |
| 253 | `SM100_FIRST` | First sm_100 (Blackwell) opcode |
| 280 | `SM100_LAST` | Last sm_100 opcode |
| 281 | `SM104_FIRST` | First sm_104 (Blackwell Ultra) opcode |
| 320 | `SM104_LAST` | Last sm_104 opcode |
| 321 | `LAST` | Sentinel (end of table) |

This gives a clear partitioning: opcodes 0-136 are the base sm_70+ ISA, 137-171 extend to sm_73, and so on up through sm_104. Each SM generation only adds opcodes; no base opcodes are removed.

## Operand Model

### Packed Operand Encoding

Each operand occupies 8 bytes (two 32-bit words) in the operand array starting at instruction offset +84. The first word carries the type, modifier bits, and index. The second word carries additional data (extended flags, immediate bits, etc.).

```
Word 0 (at instr + 84 + 8*i):

 31  30  29  28  27  26  25  24  23  22  21  20  19                  0
+---+---+---+---+---+---+---+---+---+---+---+---+---------------------+
| S |  type(3) |       modifier (8 bits)        |    index (20 bits)   |
+---+---+---+---+---+---+---+---+---+---+---+---+---------------------+
  ^   ^                                           ^
  |   bits 28-30: operand type                    bits 0-19: register/symbol index
  bit 31: sign/negative flag (S)

Word 1 (at instr + 88 + 8*i):

 31                                                                  0
+--------------------------------------------------------------------+
|               extended data / immediate bits / flags                |
+--------------------------------------------------------------------+
```

### Operand Type Field (bits 28-30)

| Value | Type | Index Meaning |
|-------|------|---------------|
| 0 | Unused / padding | — |
| 1 | Register | Index into `*(code_obj+88) + 8*index` register descriptor array |
| 2 | Predicate register | Index into predicate register file |
| 3 | Uniform register | UR file index |
| 4 | Address/offset | Memory offset value |
| 5 | Symbol/constant | Index into `*(code_obj+152)` symbol table |
| 6 | Predicate guard | Guard predicate controlling conditional execution |
| 7 | Immediate | Encoded immediate value |

### Operand Extraction Pattern

This exact extraction pattern appears in 50+ functions across scheduling, regalloc, encoding, and optimization passes:

```c
uint32_t operand_word = *(uint32_t*)(instr + 84 + 8 * i);

int  type   = (operand_word >> 28) & 7;     // bits 28-30
int  index  = operand_word & 0xFFFFF;        // bits 0-19 (also seen as 0xFFFFFF)
int  mods   = (operand_word >> 20) & 0xFF;   // bits 20-27
bool is_neg = (operand_word >> 31) & 1;      // bit 31

// Register operand check (most common pattern)
if (type == 1) {
    reg_descriptor = *(ptr*)(*(ptr*)(code_obj + 88) + 8 * index);
    reg_file_type  = *(uint32_t*)(reg_descriptor + 64);
    reg_number     = *(uint32_t*)(reg_descriptor + 12);
}
```

Some functions use a 24-bit index mask (`& 0xFFFFFF`) instead of 20-bit, packing additional modifier bits into the upper nibble of the index field.

### Operand Classification Predicates

Small predicate functions at `0xB28E00`-`0xB28E90` provide the instruction selection interface for operand queries:

| Address | Function | Logic |
|---------|----------|-------|
| `sub_B28E00` | `getRegClass` | Returns register class; 1023 = wildcard, 1 = GPR |
| `sub_B28E10` | `isRegOperand` | `(word >> 28) & 7 == 1` |
| `sub_B28E20` | `isPredOperand` | `(word >> 28) & 7 == 2` |
| `sub_B28E40` | `isImmOperand` | `(word >> 28) & 7 == 7` |
| `sub_B28E80` | `isConstOperand` | `(word >> 28) & 7 == 5` |
| `sub_B28E90` | `isUReg` | `(word >> 28) & 7 == 3` |

### Destination vs. Source Operand Split

Destinations come first in the operand array, followed by sources. The boundary is computed from the `operand_count` field and the modifier bits in the opcode:

```c
uint32_t total_ops = *(uint32_t*)(instr + 80);
int adj = (*(uint32_t*)(instr + 72) >> 11) & 2;  // 0 or 2
int first_src_index = total_ops - adj;             // or total_ops + ~adj + 1
// Destinations: operands[0 .. first_src_index-1]
// Sources:      operands[first_src_index .. total_ops-1]
```

For most instructions, `adj = 0` and the split point equals `operand_count`. Instructions with bit 11 set in the opcode word shift the split by 2, indicating 2 extra destination operands (e.g., predicated compare-and-swap operations that write both a result register and a predicate).

### Predicate Guard Operand

The last operand (at index `operand_count - 1`) can be a predicate guard (type 6) controlling conditional execution. The guard predicate check in `sub_7E0E80`:

```c
bool has_pred_guard(instr) {
    int last_idx = *(uint32_t*)(instr + 80) + ~((*(uint32_t*)(instr + 72) >> 11) & 2);
    uint32_t last_op = *(uint32_t*)(instr + 84 + 8 * last_idx);
    return ((last_op & 0xF) - 2) < 7;  // type bits in low nibble
}
```

## Instruction Flags and Modifiers

### Opcode Modifier Bits (offset +72, bits 12-13)

Bits 12-13 of the opcode word encode sub-operation modifiers. The `0xFFFFCFFF` mask strips them to yield the base opcode. Common uses:

| Modifier | Meaning |
|----------|---------|
| 0 | Default operation |
| 1 | `.HI` or alternate form |
| 2 | `.WIDE` or extended form |
| 3 | Reserved / architecture-specific |

### Extended Flag Bits (offset +48)

The 64-bit flag word at offset +48 accumulates flags throughout the compilation pipeline:

| Bit | Hex Mask | Flag | Set By |
|-----|----------|------|--------|
| 6 | `0x40` | Live-out | `sub_7E6090` (def-use builder) |
| 16 | `0x10000` | Has single def | `sub_7E6090` |
| 25 | `0x2000000` | Has prior use | `sub_7E6090` |
| 27 | `0x8000000` | Same-block def | `sub_7E6090` |
| 33 | `0x200000000` | Source-only ref | `sub_7E6090` |

### Control Word (offset +32)

The control word encodes scheduling metadata added by the instruction scheduler. It is initialized to zero and populated during scheduling (phases ~150+):

- Stall cycles (how many cycles to wait before issuing the next instruction)
- Yield hint (whether the warp scheduler should yield after this instruction)
- Dependency barrier assignments
- Reuse flags (register reuse hints for the hardware register file cache)

The stall cycle field is checked during scoreboard computation at `sub_A08910`. The control word format is the same as the SASS encoding control field.

### Data Type Flags (offset +100)

The byte at offset +100 encodes the instruction's data type in its low 3 bits:

```c
uint8_t type_code = *(uint8_t*)(instr + 100) & 7;
```

These correspond to SASS data type suffixes (`.F32`, `.F64`, `.U32`, `.S32`, `.F16`, `.B32`, etc.). The exact encoding is architecture-specific and queried through the `InstructionInfo` descriptor table.

## ROT13 Opcode Name Table

All SASS opcode mnemonic strings in the binary are ROT13-encoded. This is lightweight obfuscation, not a security measure. The `InstructionInfo` constructor at `sub_BE7390` populates a name table at object offset +4184 with 16-byte `{char* name, uint64_t length}` entries.

### Table Structure

```
InstructionInfo object:
  +0       vtable pointer (off_233ADC0)
  +8       parent pointer
  ...
  +4184    opcode_names[0].name_ptr    -> "REEONE"   (ROT13 of ERRBAR)
  +4192    opcode_names[0].length      -> 6
  +4200    opcode_names[1].name_ptr    -> "VZNQ"     (ROT13 of IMAD)
  +4208    opcode_names[1].length      -> 4
  ...
  +9320    opcode_names[321].name_ptr  -> "YNFG"     (ROT13 of LAST)
  +9328    opcode_names[321].length    -> 4
  +9336    qmemcpy from unk_22B2320, 0x508 bytes (~80 more entries)
  +10624   (end of name table area)
```

Total: 322 explicitly initialized entries (indices 0-321) plus ~80 additional entries copied in bulk from a static data block, giving approximately 402 named opcodes.

### Full Decoded Opcode Table (Base ISA, sm_70+)

| Idx | ROT13 | SASS | Category |
|-----|-------|------|----------|
| 0 | `REEONE` | `ERRBAR` | Error barrier (internal) |
| 1 | `VZNQ` | `IMAD` | Integer multiply-add |
| 2 | `VZNQ_JVQR` | `IMAD_WIDE` | Integer multiply-add wide |
| 3 | `VNQQ3` | `IADD3` | 3-input integer add |
| 4 | `OZFX` | `BMSK` | Bit mask |
| 5 | `FTKG` | `SGXT` | Sign extend |
| 6 | `YBC3` | `LOP3` | 3-input logic |
| 7 | `VFRGC` | `ISETP` | Integer set-predicate |
| 8 | `VNOF` | `IABS` | Integer absolute value |
| 9 | `YRN` | `LEA` | Load effective address |
| 10 | `FUS` | `SHF` | Funnel shift |
| 11 | `SSZN` | `FFMA` | FP fused multiply-add |
| 12 | `SNQQ` | `FADD` | FP add |
| 13 | `SZHY` | `FMUL` | FP multiply |
| 14 | `SZAZK` | `FMNMX` | FP min/max |
| 15 | `SFJMNQQ` | `FSWZADD` | FP swizzle add |
| 16 | `SFRG` | `FSET` | FP set |
| 17 | `SFRY` | `FSEL` | FP select |
| 18 | `SFRGC` | `FSETP` | FP set-predicate |
| 19 | `ZBI` | `MOV` | Move |
| 20 | `FRY` | `SEL` | Select |
| 21 | `C2E` | `P2R` | Predicate to register |
| 22 | `E2C` | `R2P` | Register to predicate |
| 23 | `CYBC3` | `PLOP3` | Predicate 3-input logic |
| 24 | `CEZG` | `PRMT` | Byte permute |
| 25 | `ABC` | `NOP` | No-op |
| 26 | `IBGR` | `VOTE` | Warp vote |
| 27 | `PF2E_32` | `CS2R_32` | Control/status to register (32-bit) |
| 28 | `PF2E_64` | `CS2R_64` | Control/status to register (64-bit) |
| 29 | `CZGEVT` | `PMTRIG` | Performance monitor trigger |
| 30 | `CFZGRFG` | `PSMTEST` | PSM test |
| 31 | `INOFQVSS` | `VABSDIFF` | Vector absolute difference |
| 32 | `INOFQVSS4` | `VABSDIFF4` | Vector absolute difference (4-way) |
| 33 | `VQC` | `IDP` | Integer dot product |
| 34 | `VQR` | `IDE` | Integer dot expand |
| 35 | `V2V` | `I2I` | Integer to integer conversion |
| 36 | `V2VC` | `I2IP` | Integer to integer (packed) |
| 37 | `VZAZK` | `IMNMX` | Integer min/max |
| 38 | `CBCP` | `POPC` | Population count |
| 39 | `SYB` | `FLO` | Find leading one |
| 40 | `SPUX` | `FCHK` | FP check (NaN/Inf) |
| 41 | `VCN` | `IPA` | Interpolate attribute |
| 42 | `ZHSH` | `MUFU` | Multi-function unit (SFU) |
| 43 | `S2S` | `F2F` | Float to float conversion |
| 44 | `S2S_K` | `F2F_X` | Float to float (extended) |
| 45 | `S2V` | `F2I` | Float to integer |
| 46 | `S2V_K` | `F2I_X` | Float to integer (extended) |
| 47 | `V2S` | `I2F` | Integer to float |
| 48 | `V2S_K` | `I2F_X` | Integer to float (extended) |
| 49 | `SEAQ` | `FRND` | FP round |
| 50 | `SEAQ_K` | `FRND_X` | FP round (extended) |
| 51 | `NY2C` | `AL2P` | Attribute to patch |
| 52 | `NY2C_VAQRKRQ` | `AL2P_INDEXED` | Attribute to patch (indexed) |
| 53 | `OERI` | `BREV` | Bit reverse |
| 54 | `OZBI_O` | `BMOV_B` | Barrier move (B) |
| 55 | `OZBI_E` | `BMOV_R` | Barrier move (R) |
| 56 | `OZBI` | `BMOV` | Barrier move |
| 57 | `F2E` | `S2R` | Special register to register |
| 58 | `O2E` | `B2R` | Barrier to register |
| 59 | `E2O` | `R2B` | Register to barrier |
| 60 | `YRCP` | `LEPC` | Load effective PC |
| 61 | `ONE` | `BAR` | Barrier synchronization |
| 62 | `ONE_VAQRKRQ` | `BAR_INDEXED` | Barrier (indexed) |
| 63 | `FRGPGNVQ` | `SETCTAID` | Set CTA ID |
| 64 | `FRGYZRZONFR` | `SETLMEMBASE` | Set local memory base |
| 65 | `TRGYZRZONFR` | `GETLMEMBASE` | Get local memory base |
| 66 | `QRCONE` | `DEPBAR` | Dependency barrier |
| 67 | `OEN` | `BRA` | Branch |
| 68 | `OEK` | `BRX` | Branch indirect |
| 69 | `WZC` | `JMP` | Jump |
| 70 | `WZK` | `JMX` | Jump indirect |
| 71 | `PNYY` | `CALL` | Function call |
| 72 | `ERG` | `RET` | Return |
| 73 | `OFFL` | `BSSY` | Branch sync stack push |
| 74 | `OERNX` | `BREAK` | Break |
| 75 | `OCG` | `BPT` | Breakpoint trap |
| 76 | `XVYY` | `KILL` | Kill thread |
| 77 | `RKVG` | `EXIT` | Exit |
| 78 | `EGG` | `RTT` | Return to trap handler |
| 79 | `OFLAP` | `BSYNC` | Branch sync |
| 80 | `ZNGPU` | `MATCH` | Warp match |
| 81 | `ANABFYRRC` | `NANOSLEEP` | Nanosleep |
| 82 | `ANABGENC` | `NANOTRAP` | Nano trap |
| 83 | `GRK` | `TEX` | Texture fetch |
| 84 | `GYQ` | `TLD` | Texture load |
| 85 | `GYQ4` | `TLD4` | Texture load 4 |
| 86 | `GZZY` | `TMML` | Texture mip-map level |
| 87 | `GKQ` | `TXD` | Texture fetch with derivatives |
| 88 | `GKD` | `TXQ` | Texture query |
| 89 | `YQP` | `LDC` | Load constant |
| 90 | `NYQ` | `ALD` | Attribute load |
| 91 | `NFG` | `AST` | Attribute store |
| 92 | `BHG` | `OUT` | Tessellation output |
| 93 | `BHG_SVANY` | `OUT_FINAL` | Tessellation output (final) |
| 94 | `YQF` | `LDS` | Load shared |
| 95 | `FGF` | `STS` | Store shared |
| 96 | `YQT` | `LDG` | Load global |
| 97 | `FGT` | `STG` | Store global |
| 98 | `YQY` | `LDL` | Load local |
| 99 | `FGY` | `STL` | Store local |
| 100 | `YQ` | `LD` | Load (generic) |
| 101 | `FG` | `ST` | Store (generic) |
| 102 | `NGBZ` | `ATOM` | Atomic |
| 103 | `NGBZT` | `ATOMG` | Atomic global |
| 104 | `ERQ` | `RED` | Reduction |
| 105 | `NGBZF` | `ATOMS` | Atomic shared |
| 106 | `DFCP` | `QSPC` | Query space |
| 107 | `PPGY_AB_FO` | `CCTL_NO_SB` | Cache control (no scoreboard) |
| 108 | `PPGY` | `CCTL` | Cache control |
| 109 | `PPGYY` | `CCTLL` | Cache control (L2) |
| 110 | `PPGYG` | `CCTLT` | Cache control (texture) |
| 111 | `ZRZONE` | `MEMBAR` | Memory barrier |
| 112 | `FHYQ` | `SULD` | Surface load |
| 113 | `FHFG` | `SUST` | Surface store |
| 114 | `FHNGBZ` | `SUATOM` | Surface atomic |
| 115 | `FHERQ` | `SURED` | Surface reduction |
| 116 | `CVKYQ` | `PIXLD` | Pixel load |
| 117 | `VFOREQ` | `ISBERD` | Indexed set binding for redirect |
| 118 | `VFORJE` | `ISBEWR` | Indexed set binding for write |
| 119 | `FUSY` | `SHFL` | Warp shuffle |
| 120 | `JNECFLAP` | `WARPSYNC` | Warp synchronize |
| 121 | `ZVRYQ` | `MYELD` | Yield (internal) |
| 122 | `QSZN` | `DFMA` | Double FP fused multiply-add |
| 123 | `QNQQ` | `DADD` | Double FP add |
| 124 | `QZHY` | `DMUL` | Double FP multiply |
| 125 | `QFRGC` | `DSETP` | Double FP set-predicate |
| 126 | `UNQQ2` | `HADD2` | Half-precision add (packed) |
| 127 | `UNQQ2_S32` | `HADD2_F32` | Half-precision add (F32 accum) |
| 128 | `USZN2` | `HFMA2` | Half FP fused multiply-add (packed) |
| 129 | `UZHY2` | `HMUL2` | Half-precision multiply (packed) |
| 130 | `UFRG2` | `HSET2` | Half-precision set (packed) |
| 131 | `UFRGC2` | `HSETP2` | Half-precision set-predicate (packed) |
| 132 | `UZZN_16` | `HMMA_16` | Half MMA (16-wide) |
| 133 | `UZZN_32` | `HMMA_32` | Half MMA (32-wide) |
| 134 | `VZZN` | `IMMA` | Integer MMA |
| 135 | `VAGEVAFVP` | `INTRINSIC` | Compiler intrinsic (pseudo) |

### Opcode Categories

The ~400 opcodes group into these functional categories:

**Integer ALU (14 opcodes):** `IMAD`, `IMAD_WIDE`, `IADD3`, `IADD`, `IMNMX`, `IABS`, `BMSK`, `SGXT`, `LOP3`, `ISETP`, `LEA`, `SHF`, `POPC`, `FLO`, `BREV`, `IDP`, `IDE`, `PRMT`

**FP32 ALU (9 opcodes):** `FFMA`, `FADD`, `FMUL`, `FMNMX`, `FSWZADD`, `FSET`, `FSEL`, `FSETP`, `FCHK`

**FP64 ALU (4 opcodes):** `DFMA`, `DADD`, `DMUL`, `DSETP`

**FP16 Packed (6 opcodes):** `HADD2`, `HADD2_F32`, `HFMA2`, `HMUL2`, `HSET2`, `HSETP2`

**Conversion (12 opcodes):** `F2F`, `F2I`, `I2F`, `I2I`, `F2FP`, `F2IP`, `I2FP`, `I2IP`, `FRND`, and their `_X` extended variants

**Data Movement (6 opcodes):** `MOV`, `UMOV`, `MOVM`, `SEL`, `USEL`, `PRMT`

**Special Function (1 opcode):** `MUFU` (sin, cos, rsqrt, rcp, etc.)

**Predicate (4 opcodes):** `PLOP3`, `P2R`, `R2P`, `VOTE`

**Memory -- Global (4 opcodes):** `LDG`, `STG`, `LD`, `ST`

**Memory -- Shared (4 opcodes):** `LDS`, `STS`, `LDSM`, `STSM`

**Memory -- Local (2 opcodes):** `LDL`, `STL`

**Memory -- Constant (2 opcodes):** `LDC`, `LDCU`

**Atomic/Reduction (6 opcodes):** `ATOM`, `ATOMG`, `ATOMS`, `RED`, `REDUX`, `REDAS`

**Texture (6 opcodes):** `TEX`, `TLD`, `TLD4`, `TMML`, `TXD`, `TXQ`

**Surface (4 opcodes):** `SULD`, `SUST`, `SUATOM`, `SURED`

**Control Flow (12 opcodes):** `BRA`, `BRX`, `JMP`, `JMX`, `CALL`, `RET`, `EXIT`, `BREAK`, `BSSY`, `BSYNC`, `KILL`, `BPT`

**Synchronization (6 opcodes):** `BAR`, `BAR_INDEXED`, `DEPBAR`, `MEMBAR`, `WARPSYNC`, `NANOSLEEP`

**Tensor Core / MMA (25+ opcodes):** `HMMA_*`, `IMMA_*`, `BMMA_*`, `DMMA`, `GMMA`, `QMMA_*`, `OMMA_*`, and their sparse (`_SP_`) variants

**Uniform Register (30+ opcodes):** All `U`-prefixed variants (`UIMAD`, `UIADD3`, `UMOV`, `USEL`, `ULOP3`, `ULEPC`, etc.) that operate on uniform registers shared across the warp

**Blackwell sm_100+ (28 opcodes):** `ACQBLK`, `CGABAR_*`, `CREATEPOLICY`, `ELECT`, `ENDCOLLECTIVE`, `FENCE_G/S/T`, `LDTM`, `STTM`, `MEMSET`, `ACQSHMINIT`, `UTCBAR_*`, `UTCMMA_*`, `UTCSHIFT_*`, `UTCCP_*`, `TCATOMSWS`, `TCLDSWS`, `TCSTSWS`, `VIRTCOUNT`, `UGETNEXTWORKID`, `FADD2`, `FFMA2`, `FMUL2`, `FMNMX3`, `CREDUX`, `QFMA4`, `QADD4`, `QMUL4`, `WARPGROUP`

## Instruction Descriptor Table

The `InstructionInfo` class at `sub_BE7390` (inheriting from the base class at `sub_738E20`) provides a per-opcode descriptor table consulted by every pass in the compiler. The base class constructor `sub_738E20` initializes an 11,240+ byte object.

### InstructionInfo Object Layout

| Offset | Content |
|--------|---------|
| +0 | Vtable pointer (`off_233ADC0` in derived, `off_21DB6E8` / `off_21B4790` in base) |
| +8 | Parent context pointer |
| +92 | Scheduling parameters (16 bytes, XMM loaded) |
| +108 | Descriptor index sentinel |
| +112 | Descriptor index sentinel |
| +168 | Opcode-to-descriptor mapping array pointer |
| +176 | Mapping array count |
| +180 | Mapping array capacity |
| +184 | Packed descriptor flags (`0x4000000000`) |
| +744 | Extended descriptor data start |
| +2156 | Secondary descriptor area |
| +2212 | Tertiary descriptor area |
| +3624 | Quaternary descriptor area |
| +3728 | SM version / opcode count (from architecture query) |
| +4048 | Constant: 2 |
| +4056 | Constant: 10 |
| +4112 | Per-opcode property array pointer |
| +4120 | Per-opcode property count |
| +4132 | Constant: 7 (initial), set to 8 post-init |
| +4136 | Reference-counted descriptor block |
| +4176 | Initialization complete flag |
| +4184 | ROT13 opcode name table start (16 bytes per entry) |
| +10624 | Per-opcode descriptor array (allocated as 10,288 bytes) |
| +10648 | Additional config value |
| +10656 | Descriptor block pointer |
| +11200 | Sentinel: -2 |
| +11208 | Architecture-specific handler |

### Per-Opcode Descriptor Block

At offset +10624, a 10,288-byte block is allocated and split into three sections:

```c
// From sub_BE7390, lines 686-694:
block = allocate(10288);
block[0]    = 0;             // section 0 header
block[641]  = 0;             // section 1 header (at 641 * 8 = 5128 bytes)
block[1284] = parent_ctx;    // section 2: back-pointer (at 1284 * 8 = 10272 bytes)
block[1285] = instr_info;    // section 2: self-pointer
memset(&block[1] ... block[640], 0, 5128 bytes);  // zero section 0
```

This gives `10288 / 8 = 1286` QWORD entries. With two 641-entry sections (one per half), this yields **641 descriptor slots** per section, close to the "1,141 instruction descriptors" mentioned in other analyses (the remaining ~500 may come from the architecture-specific sub-tables built by `sub_896D50`).

### Architecture-Specific Sub-Tables

The architecture-specific mnemonic table initializer at `sub_896D50` (21KB) is called from `sub_7A4650`. It builds extended instruction property tables including:

- Latency values per functional unit
- Throughput (instructions per cycle)
- Port masks (which execution units can handle each opcode)
- Encoding class identifiers
- Register class requirements per operand position

These are accessed through virtual dispatch on the `InstructionInfo` vtable, with 40+ tiny property accessor stubs at `0x859F80`-`0x85A5F0` and `0x868500`-`0x869700`.

## Instruction Creation

### Allocation: `sub_7DD010`

The primary instruction allocator at `sub_7DD010` (called from pass code that needs to create new instructions):

1. Allocates 296 bytes from the Code Object's arena allocator (`vtable+16`, size 296)
2. Zeroes the entire 296-byte object
3. Initializes sentinel fields: offset +248 = -1, +256 = 0xFFFFFFFF, +264 and +272 = 0xFFFFFFFF00000000
4. Loads scheduling parameter defaults from `xmmword_2027620` into offset +208
5. Appends the new instruction to the Code Object's instruction index array at +368 (resizable, 1.5x growth policy)
6. Assigns a unique instruction index: `*(instr + 264) = index`
7. Invalidates cached analysis (RPO at +792)

The instruction is created unlinked -- it is not yet in any basic block's linked list.

### Linking: `sub_925510` (Insert Before)

`sub_925510` inserts instruction `a2` before instruction `a3` in the doubly-linked list of Code Object `a1`:

```c
void InsertBefore(CodeObject* ctx, Instr* instr, Instr* before) {
    // 1. Check if instruction removal impacts scheduling state
    if (IsScheduleRelevant(instr, ctx))
        UpdateScheduleState(ctx, instr);

    // 2. Notify observers
    NotifyObservers(ctx->observer_chain + 1952, instr);

    // 3. Unlink from current position
    if (instr->prev) {
        instr->prev->next = instr->next;
        if (instr->next)
            instr->next->prev = instr->prev;
        else
            ctx->tail = instr->prev;   // was tail
    } else {
        ctx->head = instr->next;        // was head
        instr->next->prev = nullptr;
    }

    // 4. Insert before target
    instr->next = before;
    instr->bb_index = before->bb_index;
    instr->prev = before->prev;
    if (before->prev)
        before->prev->next = instr;
    if (before == ctx->head)
        ctx->head = instr;
    before->prev = instr;

    // 5. Post-insert bookkeeping
    PostInsertUpdate(ctx, instr);
}
```

### Removal: `sub_9253C0`

`sub_9253C0` (634 callers) removes an instruction from its linked list:

1. Checks if the instruction affects scheduling state (same check as insert)
2. Notifies the observer chain at Code Object +1952
3. Unlinks from the doubly-linked list (updating head/tail pointers at +272/+280)
4. Optionally updates the instruction map at Code Object +1136 (if `a3` flag is set)
5. Handles debug info cleanup if the debug flag at byte +1421 bit 5 is set

### Instruction Removal Check: `sub_7E0030`

Before removing an instruction (`sub_7E0030`, called from both `sub_9253C0` and `sub_925510`), the compiler checks whether the removal is legal. This function examines:

- Whether the instruction is an `STS` (store shared, base opcode 95) with specific operand count and data type patterns (operand_count - adj == 5 with data type codes 1, 2, or 4 prevent removal)
- Whether a target-specific scheduler hook (vtable offset 2128 on the scheduler context at Code Object +1584) vetoes the removal
- Whether the instruction is a `PLOP3` (predicate logic, opcode 23) writing to a special register (register file type 9 at descriptor +64)
- Whether the dead-code check (`sub_7DF3A0`) clears the instruction, excluding opcodes 93 (`OUT_FINAL`), 124 (`DMUL`), and 248 (SM90+ opcode) which have required side effects
- Whether the opcode class has a "must keep" flag in the per-opcode property array at Code Object +776 (`byte[4*opcode + 2] & 4`)

## Instruction Iteration

### Forward Walk

The standard forward walk over a basic block's instructions:

```c
// code_obj->head is at +272, tail at +280
instr_ptr instr = *(ptr*)(code_obj + 272);
while (instr) {
    // process instruction
    instr = *(ptr*)(instr + 8);  // next
}
```

### Reverse Walk

```c
instr_ptr instr = *(ptr*)(code_obj + 280);  // tail
while (instr) {
    // process instruction
    instr = *(ptr*)(instr + 0);  // prev
}
```

### Block-Scoped Iteration

When iterating within a specific basic block (used by scheduling, regalloc, and peephole passes), the block's head instruction pointer at block_entry +0 is the starting point, and iteration continues until the next block boundary (opcode 52, named `AL2P_INDEXED` in the ROT13 table but universally used as a BB delimiter pseudo-opcode) or the list tail:

```c
// Block info at code_obj+976, 40 bytes per block
ptr block_head = *(ptr*)(*(ptr*)(code_obj + 976) + 40 * block_index);
for (instr = block_head; instr != nullptr; instr = *(ptr*)(instr + 8)) {
    uint32_t op = *(uint32_t*)(instr + 72) & 0xFFFFCFFF;
    if (op == 52)  // BB boundary
        break;
    // process instruction
}
```

### Def-Use Chain Iterator: `sub_7E6090`

The complex def-use chain builder `sub_7E6090` (650 lines decompiled) is the core instruction analysis function. Called from `sub_8E3A80` and numerous optimization passes, it:

1. Walks all instructions in program order
2. For each register operand (type == 1 via `(word >> 28) & 7`), updates the register descriptor's def/use counts at offsets +20 and +24
3. Builds use chains via linked list nodes allocated from the arena (16-byte nodes with `{next, instruction_ptr}`)
4. Sets flag bits in register descriptors (+48) for live-out, same-block-def, has-prior-use, and source-only-ref
5. Tracks the single-definition instruction at register descriptor +56
6. Handles CSE matching: compares operand arrays of instructions with matching opcode, operand count, and auxiliary data to detect redundant computations
7. Takes parameter `a5` as a bitmask of register file types to process (bit per register class)

## Key Function Reference

| Address | Size | Function | Description |
|---------|------|----------|-------------|
| `sub_7DD010` | 1.3KB | `Instruction::create` | Allocate and initialize 296-byte instruction |
| `sub_7E0030` | 3.6KB | `Instruction::canRemove` | Check if instruction removal is legal |
| `sub_7E0650` | 0.7KB | `Instruction::hasPredGuard` | Check if instruction has predicate guard |
| `sub_7E0E80` | 0.1KB | `Instruction::lastOpIsPred` | Quick predicate-guard check on last operand |
| `sub_7E6090` | 10KB | `DefUseChain::build` | Build def-use chains for all instructions |
| `sub_7DDCA0` | 0.2KB | `Observer::notify` | Walk observer chain and notify |
| `sub_9253C0` | 0.5KB | `Instruction::remove` | Remove instruction from linked list (634 callers) |
| `sub_925510` | 0.5KB | `Instruction::insertBefore` | Insert instruction before another (13 callers) |
| `sub_917A60` | 6.8KB | `InstrInfo::getRegClass` | Opcode-to-register-class mapping (221 callers) |
| `sub_91A0F0` | 5.6KB | `InstrInfo::resolveRegClass` | Resolve operand register class with constraints |
| `sub_9314F0` | 0.4KB | `RegClass::query` | Register class query (1,547 callers) |
| `sub_738E20` | 10KB | `InstrDescTable::init` | Base instruction descriptor table constructor |
| `sub_BE7390` | 16KB | `InstructionInfo::init` | InstructionInfo constructor (ROT13 table + descriptors) |
| `sub_896D50` | 21KB | `InstrMnemTable::init` | Architecture-specific mnemonic table initializer |
| `sub_6D9690` | 94KB | `Instruction::encode` | Master SASS instruction encoder |
| `sub_B28E00` | varies | `isReg/isPred/isImm` | Operand type predicates (isel infrastructure) |

## Related Pages

- [Ori IR Overview](./overview.md) -- Code Object, basic blocks, CFG, register files
- [Registers](./registers.md) -- Register descriptor layout, register file types
- [CFG](./cfg.md) -- Basic block structure, control-flow graph
- [Data Structures](./data-structures.md) -- Hash tables, bitvectors, linked lists
- [Peephole Optimization](../codegen/peephole.md) -- Instruction rewriting passes
- [SASS Encoding](../codegen/encoding.md) -- How Ori instructions become SASS binary
- [Instruction Selection](../codegen/isel.md) -- Pattern matching for instruction selection
- [Scheduling](../scheduling/overview.md) -- 3-phase instruction scheduler
