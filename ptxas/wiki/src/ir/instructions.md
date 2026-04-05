# Instructions & Opcodes

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

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
  +9336    encoding_category_map[0..321]  (322 x int32, from unk_22B2320)
  +10624   (end of encoding category map)
```

Total: 322 named opcodes (indices 0-321). The 0x508 bytes at +9336 are **not** additional name entries -- they are a 322-element `int32` array mapping each opcode index to an encoding category number (see [Encoding Category Map](#encoding-category-map) below).

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

The `InstructionInfo` class at `sub_BE7390` (inheriting from the base class at `sub_738E20`) provides a per-opcode descriptor table consulted by every pass in the compiler. The derived constructor calls the base class constructor `sub_738E20`, then populates the ROT13 name table, allocates the per-opcode descriptor block, and queries SM-specific configuration knobs. The resulting object is ~11,240 bytes inline plus a 10,288-byte dynamically allocated descriptor block.

### Construction Sequence

`sub_BE7390(this, parent_context)` executes in this order:

1. **Base class init** (`sub_738E20`): sets vtable, stores parent pointer, allocates the opcode-to-descriptor mapping array (512 bytes, 64 QWORD slots), zeroes all four descriptor data areas (+744..+3624), queries SM version and stores at +3728, allocates per-opcode property array (`4 * sm_opcode_count` bytes at +4112), allocates a reference-counted descriptor block (24 bytes at +4136), queries knobs 812/867/822/493 for configuration. Sets `+4132 = 8` and `+4176 = 0` (init incomplete).
2. **Override vtable**: `+0 = off_233ADC0` (derived vtable).
3. **Populate ROT13 name table**: 322 inline entries (indices 0-321) at offsets +4184..+9328, each 16 bytes (`{char* name_ptr, u64 length}`).
4. **Bulk-copy encoding category map**: `qmemcpy(+9336, unk_22B2320, 0x508)` -- 322-entry `int32` array (1288 bytes) mapping opcode index to encoding category number. The source table varies by arch constructor (see below).
5. **Initialize post-table fields**: zero offsets +10624..+10680.
6. **Store sentinels**: `+11200 = -2`, `+11224 = 0xFFFFFFFF`.
7. **Set constants**: `+4048 = 2`, `+4056 = 10`, `+3733 = 1`.
8. **Descriptor defaults** (`sub_1370BD0`): populates scheduling templates and operand defaults at +192..+704.
9. **Override property mode**: `+4132 = 7` (overwriting base class's 8).
10. **Allocate descriptor block**: 10,288 bytes via the MemoryManager, partitioned into 3 sections.
11. **Query SM-specific config**: reads `parent->+1664->+72->+55080` and stores result at +10648.

### InstructionInfo Object Layout

The complete byte-level field map, derived from `sub_BE7390` (derived constructor), `sub_738E20` (base constructor), and `sub_1370BD0` (descriptor defaults init).

#### Region 1: Vtable, Parent, and Core Identity (+0 to +91)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +0 | 8 | `ptr` | `vtable` | `off_233ADC0` (derived); base chain: `off_21DB6E8` / `off_21B4790` |
| +8 | 8 | `ptr` | `parent_ctx` | Parent compilation context pointer |
| +44 | 8 | `u64` | `operand_counts` | Packed pair `0x100000001`: lo=1 dst, hi=1 src (base default) |

#### Region 2: Scheduling Defaults and Flags (+92 to +159)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +92 | 16 | `xmm` | `sched_defaults` | Scheduling parameter defaults (loaded from `xmmword_2029FE0`) |
| +108 | 4 | `i32` | `desc_idx_a` | Descriptor index sentinel = 0 |
| +112 | 4 | `i32` | `desc_idx_b` | Descriptor index sentinel = -1 (`0xFFFFFFFF`) |
| +116 | 1 | `u8` | `flag_116` | = 0 |
| +117 | 1 | `u8` | `flag_117` | = 0 |
| +118 | 1 | `u8` | `flag_118` | = 1 |
| +120 | 3 | `u8[3]` | `flags_120` | All = 0 |
| +136 | 4 | `i32` | `sentinel_136` | = -1 (`0xFFFFFFFF`) |
| +148 | 8 | `u64` | `reserved_148` | = 0 |

#### Region 3: Opcode-to-Descriptor Mapping (+160 to +191)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +160 | 8 | `ptr` | `mapping_allocator` | MemoryManager used for mapping array |
| +168 | 8 | `ptr` | `mapping_array` | Dynamically allocated QWORD array (initial: 512 bytes, 64 entries) |
| +176 | 4 | `i32` | `mapping_count` | Current entry count (initially 63) |
| +180 | 4 | `i32` | `mapping_capacity` | Current capacity (initially 64) |
| +184 | 8 | `u64` | `packed_flags` | = `0x4000000000` (bit 38: descriptor config flag) |

#### Region 4: Descriptor Defaults (+192 to +704, set by `sub_1370BD0`)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +192 | 8 | `u64` | `default_operand_cfg` | Packed `0x200000002`: lo=2, hi=2 |
| +200 | 4 | `u32` | `default_dst_count` | = 4 |
| +208 | 4 | `u32` | `default_modifier` | = 2 |
| +216 | 16 | `xmm` | `sched_template_a` | Scheduling template (from `xmmword_233B1E0`) |
| +240 | 4 | `u32` | `default_operand_w` | = 4 |
| +448 | 8 | `u64` | `section_marker_448` | = 1 |
| +456 | 4 | `u32` | `section_id_456` | = 2 |
| +464 | 4 | `u32` | `section_id_464` | = 3 |
| +472 | 16 | `xmm` | `sched_template_b` | Scheduling template (from `xmmword_233B1F0`) |
| +496 | 4 | `u32` | `default_value_496` | = 5 |

Gaps within +204..+447 and +500..+695 are zero-initialized by `sub_1370BD0`.

#### Region 5: Primary Descriptor Data (+744 to +2155)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +744 | 8 | `u64` | `desc_data_start` | Primary area header = 0 |
| +752..+2155 | 1404 | `u8[]` | `desc_data` | Zero-initialized per-opcode descriptor records |

#### Region 6: Secondary Descriptor Area (+2156 to +2211)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +2156 | 8 | `u64` | `secondary_header` | = 0 |
| +2164..+2211 | 48 | `u8[]` | `secondary_data` | Zero-initialized |

#### Region 7: Tertiary Descriptor Area (+2212 to +3623)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +2212 | 8 | `u64` | `tertiary_header` | = 0 |
| +2220..+3623 | 1404 | `u8[]` | `tertiary_data` | Zero-initialized |
| +2372 | 4 | `u32` | `desc_record_type_a` | = 4 (set by derived constructor) |
| +2400 | 4 | `u32` | `desc_record_type_b` | = 4 (set by derived constructor) |

#### Region 8: Quaternary Descriptor Area and Target Config (+3624 to +3735)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +3624 | 8 | `u64` | `quaternary_header` | = 0 |
| +3640..+3664 | 32 | `u64[4]` | `quat_ptrs` | All = 0 |
| +3672 | 1 | `u8` | `is_sm75_plus` | = 1 if SM ID >= 16389, else 0 |
| +3673 | 1 | `u8` | `target_flag_bit6` | Bit 6 of `*(target+1080)` |
| +3674 | 1 | `u8` | `target_flag_bit7` | Bit 7 of `*(target+1080)` |
| +3675..+3682 | 8 | `u8[8]` | `zero_pad` | All = 0 |
| +3684 | 32 | `u128[2]` | `zero_pad_3684` | = 0 |
| +3716..+3717 | 2 | `u8[2]` | `flags_3716` | = 0 |
| +3720 | 4 | `u32` | `value_3720` | = 0 |
| +3724 | 1 | `u8` | `flag_3724` | = 1 |
| +3725 | 1 | `u8` | `flag_3725` | = 0 |
| +3728 | 4 | `u32` | `sm_opcode_count` | SM version / total opcode count from arch query |
| +3732 | 1 | `u8` | `knob_812_flag` | Knob 812 derived flag |
| +3733 | 1 | `u8` | `derived_flag` | = 1 (set by derived constructor; base leaves at 0) |

#### Region 9: Scheduling Configuration (+4016 to +4111)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +4016 | 16 | `u128` | `sched_config_a` | = 0 |
| +4032 | 8 | `u64` | `sched_config_b` | = 0 |
| +4040 | 16 | `xmm` | `sched_constants` | Loaded from `xmmword_21B4EE0` |
| +4048 | 4 | `u32` | `constant_2` | = 2 (derived overrides base default 0) |
| +4056 | 4 | `u32` | `constant_10` | = 10 (derived overrides base default `0x7FFFFFFF`) |
| +4060..+4064 | 8 | `u32[2]` | `zero_pad` | = 0 |
| +4072 | 8 | `u64` | `sched_ptr` | = 0 |
| +4080 | 8 | `u64` | `sched_ext` | = 0 |
| +4088 | 1 | `u8` | `flag_4088` | = 0 |
| +4089 | 1 | `u8` | `knob_867_flag` | = 1 if knob absent; = `(knob_value == 1)` otherwise |
| +4090 | 1 | `u8` | `flag_4090` | = 0 |
| +4092 | 4 | `u32` | `knob_822_value` | Default 7; overridden by knob 822 |
| +4096 | 4 | `u32` | `knob_493_value` | Default 5; overridden by knob 493 |

#### Region 10: Per-Opcode Property Array (+4112 to +4183)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +4112 | 8 | `ptr` | `property_array` | Allocated: `4 * sm_opcode_count` bytes; 4 bytes per opcode |
| +4120 | 4 | `u32` | `property_count` | = `4 * !hasExtendedPredicates` (0 or 4) |
| +4124 | 4 | `u32` | `property_aux` | = 0 |
| +4128 | 1 | `u8` | `property_init_flag` | = 1 |
| +4132 | 4 | `u32` | `property_mode` | Base sets 8, derived overwrites to 7 |
| +4136 | 8 | `ptr` | `ref_counted_block` | 24-byte block: `[refcount=2, data=0, allocator_ptr]` |
| +4144..+4160 | 24 | `u64[3]` | `rc_aux` | All = 0 |
| +4176 | 1 | `u8` | `init_complete` | = 0 initially; set to 1 after full initialization |

#### Region 11: ROT13 Opcode Name Table (+4184 to +10623)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +4184 | 5152 | `struct[322]` | `opcode_names[0..321]` | 322 inline entries, each 16 bytes: `{char* name, u64 len}` |
| +9336 | 1288 | `int32[322]` | `encoding_category_map[0..321]` | Per-opcode encoding category; bulk-copied from arch-specific static table (see below) |

Total: 322 named opcodes. Index `N` name is at offset `4184 + 16*N`. The `getName` accessor at `sub_BEBAC0` computes `this + 4184 + 16 * opcode` directly. Encoding category for opcode `N` is at `+9336 + 4*N`.

##### Encoding Category Map

The 1288-byte block at +9336 is a 322-element `int32` array that maps each opcode index to an **encoding category** number. The SASS mnemonic lookup function (`sub_1377C60`) uses this to resolve a `(mnemonic, arch)` pair to a binary encoding format descriptor.

**Arch-specific source tables:**

| Constructor | Source Table | Content |
|---|---|---|
| `sub_7A5D10` (base) | `unk_21C0E00` | Identity map: `map[i] = i` for all `i` in 0..321 |
| `sub_7C5410` | `unk_21C3600` | Arch-remapped: some entries differ from identity |
| `sub_BE7390` | `unk_22B2320` | Arch-remapped: some entries differ from identity |

The base constructor uses a pure identity map where opcode N maps to encoding category N. Arch-specific constructors override selected entries so the same mnemonic at different opcode indices can map to different encoding formats. For example, DMMA at opcode index 180 maps to encoding category 434 on one arch, while DMMA at opcode index 215 maps to encoding category 515 on another.

**Reader: `sub_1377C60` (SASS mnemonic lookup)**

```c
// After matching mnemonic string v11 to opcode index v18 via ROT13 comparison:
v84 = *(_DWORD *)(a1 + 4 * v18 + 9336);  // encoding_category_map[v18]
// v84 is then FNV-1a hashed together with arch discriminator v16,
// and looked up in the hash table at *(a1 + 10672) to find the
// encoding format descriptor for this (category, arch) pair.
```

The hash table at `+10672` stores entries of the form `{encoding_category, arch_code, format_value}`, keyed by FNV-1a of `(encoding_category, arch_discriminator)`. This is the central mechanism that maps a SASS mnemonic string plus target architecture to the correct binary encoding format.

#### Region 12: Descriptor Block Control (+10624 to +10687)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +10624 | 8 | `u64` | `block_ctrl_a` | = 0 |
| +10632 | 8 | `u64` | `block_ctrl_b` | = 0 |
| +10648 | 4 | `u32` | `arch_config` | SM-specific config from `target+55080/55088` |
| +10656 | 8 | `ptr` | `descriptor_block` | Pointer to allocated 10,288-byte per-opcode descriptor block |
| +10664 | 8 | `ptr` | `block_allocator` | MemoryManager that allocated the descriptor block |
| +10672 | 8 | `ptr` | `encoding_lookup_table` | Hash table for `(encoding_category, arch)` -> format descriptor lookup; read by `sub_1377C60` |
| +10680 | 8 | `u64` | `block_aux_b` | = 0 |

#### Region 13: Sentinels and Architecture Handler (+11200 to +11240)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +11200 | 4 | `i32` | `sentinel` | = -2 (`0xFFFFFFFE`) |
| +11208 | 8 | `ptr` | `arch_handler` | = `parent_ctx->+16` (MemoryManager) |
| +11216 | 8 | `u64` | `zero_11216` | = 0 |
| +11224 | 8 | `u64` | `sentinel_11224` | = `0xFFFFFFFF` |
| +11232 | 1 | `u8` | `flag_11232` | = 0 |
| +11236 | 4 | `u32` | `zero_11236` | = 0 |

### Per-Opcode Descriptor Block (10,288 bytes)

Allocated by the derived constructor and stored at `+10656`. The block is `10288 / 8 = 1286` QWORD entries, partitioned into three sections:

```
+--------------------+  block + 0
| Section 0 header   |  QWORD[0] = 0
+--------------------+  block + 8
| Section 0 payload  |  QWORD[1..640]  = all zero (memset)
| (640 slots)        |  Per-opcode descriptors for opcodes 0..639
+--------------------+  block + 5128
| Section 1 header   |  QWORD[641] = 0
+--------------------+  block + 5136
| Section 1 payload  |  QWORD[642..1283]  (NOT explicitly zeroed)
| (642 slots)        |  Modifier-variant descriptors (opcode | 0x1000, etc.)
+--------------------+  block + 10272
| Section 2 (16B)    |  QWORD[1284] = parent_ctx  (back-pointer)
|                    |  QWORD[1285] = instr_info   (self back-pointer)
+--------------------+  block + 10288
```

**Section 0** (5,128 bytes): 641 QWORD slots. Only the payload (slots 1..640, 5,120 bytes) is explicitly zeroed. Each slot corresponds to a base opcode index. With 402 named opcodes, ~240 slots remain spare.

**Section 1** (5,144 bytes): 643 QWORD slots. The header is zeroed but the payload is NOT explicitly zeroed -- it relies on the arena allocator's default behavior or lazy initialization during opcode registration. Likely stores modifier-variant descriptors (e.g., entries for `opcode | 0x1000` when bits 12-13 carry sub-operation modifiers).

**Section 2** (16 bytes): Two back-pointers for navigating from the descriptor block back to its owning objects (parent compilation context and the InstructionInfo instance).

### Architecture-Specific Sub-Tables (sub_896D50, 26,888 bytes)

The architecture-specific extended property object is NOT stored inside InstructionInfo. It is lazily allocated by `sub_7A4650`, which gates on `target+372 == 0x8000` (sm_80 / Ampere targets). The allocation is 26,888 bytes, constructed by `sub_896D50(block, parent_context)`.

#### sub_896D50 Object Layout

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| +0 | 8 | `ptr` | `vtable` | `off_21DADF8` |
| +8 | 8 | `ptr` | `parent_ctx` | From construction parameter |
| +40 | 8 | `ptr` | `allocator_base` | MemoryManager from `parent->+16` |

**Property Array A** (at sub-object +56):

| Sub-offset | Field | Description |
|------------|-------|-------------|
| +56 | `ptr` | Array pointer: 64 bytes per entry, 772 entries (49,408 bytes allocated) |
| +64 | `i32` | Count = 771 |
| +68 | `i32` | Capacity = 772 |

Each 64-byte entry: bytes [0..11] initialized to `0xFF` (pipeline-unassigned sentinel), bytes [12..63] zeroed. Stores latency, throughput, port mask, and register class requirements per opcode.

**Property Array B** (at sub-object +80):

| Sub-offset | Field | Description |
|------------|-------|-------------|
| +80 | `ptr` | Array pointer: 36 bytes per entry, 772 entries (27,792 bytes allocated) |
| +88 | `i32` | Count = 771 |
| +92 | `i32` | Capacity = 772 |

Each 36-byte entry: all zeroed. Stores encoding class, format identifiers, operand encoding rules.

**Property Array C** (at sub-object +176):

| Sub-offset | Field | Description |
|------------|-------|-------------|
| +176 | `ptr` | Array pointer: 16 bytes per entry, 35 entries (560 bytes allocated) |
| +184 | `i32` | Count = 34 |
| +188 | `i32` | Capacity = 35 |

Each 16-byte entry: zeroed. Stores functional unit properties for major FU categories.

**Property Array D** (at sub-object +200):

| Sub-offset | Field | Description |
|------------|-------|-------------|
| +200 | `ptr` | Array pointer: 16 bytes per entry, 35 entries (560 bytes allocated) |
| +208 | `i32` | Count = 34 |

Parallel table for alternate functional unit configurations.

**Dimension Table** (at sub-object +472):

| Sub-offset | Field | Description |
|------------|-------|-------------|
| +472 | `ptr` | 168-byte block: `[count=40, entries[0..39]]`, 4 bytes per entry, zero-initialized |

**Alphabetical SASS Name Table** (at sub-object +11360):

Starting at offset +11360, `sub_896D50` populates an alphabetically sorted ROT13 name table using the same `{char*, u64}` format. Unlike the InstructionInfo name table (indexed by opcode), this table is sorted by decoded mnemonic name and includes modifier variants:

- `OZZN.168128` (BMMA.168128)
- `PPGY.P.YQP.VINYY` (CCTL.C.LDC.IVALL)
- `VZNQ.JVQR.ERNQ.NO` (IMAD.WIDE.READ.AB)
- `VZZN.FC.{168128.*|16864.*8.*8}` (IMMA.SP.{...} -- regex patterns for variant matching)

This table is used for SASS assembly parsing and opcode-to-encoding resolution, where a single base opcode may map to multiple encoding variants distinguished by modifier suffixes.

**Knob-derived fields:**

| Sub-offset | Field | Source |
|------------|-------|--------|
| +108 | `i32` | Knob 803 value (instruction scheduling latency override) |
| +468 | `u8` | = 0 |
| +469 | `u8` | = 1 |
| +470 | `u8` | = 1 |

#### Accessor Stubs

40+ tiny vtable accessor stubs at `0x859F80`-`0x85A5F0` and `0x868500`-`0x869700` provide virtual dispatch access to per-opcode properties. Typical pattern:

```c
int getLatency(ArchSpecificInfo* this, int opcode) {
    return *(int*)(this->property_array_a + 64 * opcode + latency_offset);
}
```

## PTX Text-Generation Operand Accessor API

The PTX text generation subsystem (instruction pretty-printer, dispatcher at `sub_5D4190`) converts Ori IR instructions into PTX assembly text. The ~580 formatter functions at `0x4DA340`-`0x5A9FFF` query a PTX instruction context object through a stable API of 48 small accessor helpers concentrated at `0x707000`-`0x710FFF`.

### PTX Instruction Context Object

The accessor functions do NOT operate on the 296-byte Ori IR instruction directly. They take a **PTX instruction context object** (~2500+ bytes) that contains pre-decoded fields for text generation. The raw Ori instruction is accessible at `*(context + 1096)`. Each formatter receives this context as argument `a1` and a pool allocator table as argument `a2`.

Partial field map of the PTX instruction context (offsets used by accessors):

| Offset | Size | Type | Field | Accessed By |
|--------|------|------|-------|-------------|
| +544 | 8 | `ptr` | `predicate_ptr` | `has_predicate`, `get_opcode_string` |
| +564 | 4 | `u32` | `saturation_code` | `get_saturation_mode` (== 12 means saturate) |
| +596 | 4 | `u32` | `field_operand_count` | `get_field_a`..`get_field_d` |
| +600 | 1 | `u8` | `flag_byte_a` | Bit 0: precision, bit 6: addressing, bit 7: addr_mode |
| +604 | 1 | `u8` | `rounding_mode` | Bits 0-2: rounding mode code (3 bits) |
| +605 | 1 | `u8` | `scale_byte` | Bits 4-7: scale code (4 bits, 16 entries) |
| +609 | 1 | `u8` | `base_addr_byte` | Bits 2-3: base address mode (2 bits, 4 entries) |
| +611 | 1 | `u8` | `param_flags` | Bits 4-5: parameter variant selector |
| +615 | 1 | `u8` | `ftz_byte` | Bits 6-7: FTZ flag code (2 bits, 4 entries) |
| +620 | 1 | `u8` | `variant_index` | Variant string lookup index (8 bits, 256 entries) |
| +627 | 1 | `u8` | `flag_byte_b` | Bits 0-1: extended_op, 2-3: flag_b, 4-5: modifier/variant |
| +640 | 4 | `i32` | `precision_code` | Index into precision string table |
| +648 | var | `ptr[]` | `operand_names` | Per-operand name string pointer array (8B per slot) |
| +800 | 4 | `u32` | `operand_count` | Number of operands for comparison/count accessors |
| +816 | var | `ptr[]` | `reg_operands` | Register operand pointer array (8B per slot) |
| +944 | var | `u32[]` | `operand_types` | Per-operand type code array (4B per slot) |
| +1024 | var | `ptr[]` | `src_part0` | Source part 0 pointer array (8B per slot) |
| +1264 | var | `ptr[]` | `src_part1` | Source part 1 pointer array (8B per slot) |
| +1504 | var | `ptr[]` | `data_types_0` | Data type array, part 0 (8B per slot) |
| +1744 | var | `ptr[]` | `data_types_1` | Data type array, part 1 (8B per slot) |
| +1984 | var | `u32[]` | `target_sm` | Target SM version array (4B per slot) |
| +2120 | 8 | `ptr` | `opcode_name` | Opcode mnemonic string pointer |
| +2488 | 8 | `ptr` | `string_intern` | String interning table for modifier deduplication |

### Accessor Catalog

#### Tier 1: Core Accessors (>200 callers)

Used by nearly every formatter function. These are the fundamental building blocks of PTX text generation.

| Address | Name | Size | Callers | Signature | Logic |
|---------|------|------|---------|-----------|-------|
| `sub_710860` | `getDataType` | 39B | 2953 | `(ctx, idx, part) -> u8` | `part ? **(ctx+1744+8*idx) & 0x3F : **(ctx+1504+8*idx) & 0x3F` |
| `sub_70B910` | `getSrcPart0` | 12B | 1656 | `(ctx, idx) -> ptr` | `*(ctx + 8*idx + 1024)` |
| `sub_70B8E0` | `getRegOperand` | 12B | 1449 | `(ctx, idx) -> ptr` | `*(ctx + 8*idx + 816)` |
| `sub_70B920` | `getSrcPart1` | 12B | 1296 | `(ctx, idx) -> ptr` | `*(ctx + 8*idx + 1264)` |
| `sub_70B700` | `hasPredicate` | 14B | 946 | `(ctx) -> bool` | `*(ctx + 544) != 0` |
| `sub_70B780` | `getPredicateName` | 151B | 514 | `(ctx, pool) -> str` | Allocates `"@" + opcode_name`; inserts `"!"` if negated |
| `sub_70CA60` | `getOperandType` | 11B | 480 | `(ctx, idx) -> u32` | `*(ctx + 4*idx + 944)` |
| `sub_70B710` | `getOpcodeString` | 111B | 348 | `(ctx, pool) -> str` | Allocates `"@" + *(ctx+2120)` from arena pool |
| `sub_70FA00` | `getTargetSM` | 10B | 286 | `(ctx, idx) -> u32` | `*(ctx + 4*idx + 1984)` |

#### Tier 2: Modifier and Property Accessors (10-200 callers)

Used by instruction-class families (memory ops, float ops, texture ops, etc.).

| Address | Name | Size | Callers | Signature | Logic |
|---------|------|------|---------|-----------|-------|
| `sub_70CA70` | `getTypeSuffix` | 427B | 191 | `(ctx, pool) -> str` | Iterates `*(ctx+796)` type codes; looks up in `off_2032300[]` with interning |
| `sub_70CD20` | `getOperandOffset` | 122B | 158 | `(ctx, idx) -> str` | `off_2032300[*(ctx+4*idx+944)]`; resolves via string interning for codes <= 0x39 |
| `sub_707CE0` | `getAddressOperand` | 22B | 93 | `(ctx) -> str` | `off_2033DE0[*(ctx+600) >> 7]` |
| `sub_70B930` | `getOperandCount` | 7B | 68 | `(ctx) -> u32` | `*(ctx + 800)` |
| `sub_70B4C0` | `getBaseAddress` | 22B | 46 | `(ctx) -> str` | `off_2032700[(*(ctx+609) >> 2) & 3]` |
| `sub_709A10` | `getVariantString` | 73B | 46 | `(ctx) -> str` | `off_2033060[*(ctx+620)]` resolved via string interning |
| `sub_70B6E0` | `hasPredicate_v2` | 14B | 42 | `(ctx) -> bool` | `*(ctx + 544) != 0` (identical body to `hasPredicate`) |
| `sub_709760` | `getComparisonOp` | 127B | 21 | `(ctx, pool) -> str` | Iterates `*(ctx+800)` operand names from +648 array with `" , "` separator |
| `sub_709FE0` | `getRoundingMode` | 11B | 17 | `(ctx) -> u8` | `*(ctx + 604) & 7` |
| `sub_70A500` | `getSaturationMode` | 13B | 15 | `(ctx) -> bool` | `*(ctx + 564) == 12` |
| `sub_709910` | `getVariantCount` | 14B | 13 | `(ctx) -> u8` | `(*(ctx+627) >> 4) & 3` |
| `sub_708E40` | `getExtendedOperand` | 29B | 10 | `(ctx, idx) -> str` | `off_2033720[(*(ctx+627) >> (idx==1 ? 0 : 2)) & 3]` |

#### Tier 3: Instruction-Class-Specific Accessors (<10 callers)

Used by specific instruction families (MMA/tensor, texture, guardrail formatters).

| Address | Name | Size | Callers | Signature | Purpose |
|---------|------|------|---------|-----------|---------|
| `sub_70FA10` | `checkTargetSM` | 66B | 7 | `(ctx, idx, str) -> bool` | `sscanf(str, "sm_%d")` then compare to `*(ctx+1984+4*idx)` |
| `sub_70C890` | `getOperandDetail` | ~300B | varies | `(ctx, pool, maxlen, type) -> str` | Complex: hex parse, fallback to `sub_707380`, type-dispatch |
| `sub_70A810` | `getScaleString` | 22B | varies | `(ctx) -> str` | `off_2032BA0[(*(ctx+605) >> 4) & 0xF]` |
| `sub_70B3F0` | `getFtzFlag` | 22B | varies | `(ctx) -> str` | `off_20327C0[(*(ctx+615) >> 6) & 3]` |
| `sub_707530` | `getPrecisionString` | 12B | varies | `(ctx) -> str` | `off_2033FA0[*(ctx+640)]` |
| `sub_707C60` | `getAddressingMode` | 12B | varies | `(ctx) -> bool` | `(*(ctx+600) & 0x40) != 0` |
| `sub_707C80` | `getScopeString` | 22B | varies | `(ctx) -> str` | `off_2033E00[(*(ctx+600) & 0x40) != 0]` |
| `sub_7075E0` | `getLayoutString` | 22B | varies | `(ctx) -> str` | `off_2033EE0[*(ctx+600) & 1]` -- WMMA/TCGEN05 |
| `sub_707BE0` | `getShapeString` | 22B | varies | `(ctx) -> str` | `off_2033E30[(*(ctx+600) & 4) != 0]` -- WMMA/TCGEN05 |
| `sub_7075C0` | `getInstrFlagA` | 7B | varies | `(ctx) -> u8` | `*(ctx+600) & 1` -- WMMA/rsqrt |
| `sub_707BC0` | `getInstrFlagB` | varies | varies | `(ctx) -> varies` | Secondary flag accessor -- WMMA/rsqrt |
| `sub_70D3B0` | `getFieldA` | 91B | 2 | `(ctx) -> str` | Returns `".transA"` if operand count matches MMA shape |
| `sub_70D410` | `getFieldB` | 99B | 2 | `(ctx) -> str` | Returns `".transB"` (symmetric with `getFieldA`) |
| `sub_70D480` | `getFieldC` | 91B | 2 | `(ctx) -> str` | MMA field C modifier string |
| `sub_70D4E0` | `getFieldD` | 91B | 2 | `(ctx) -> str` | MMA field D modifier string |
| `sub_70D360` | `getModifier` | 76B | 1 | `(ctx, pool) -> str` | Reads operand at index 3 or 5 depending on byte 627 |
| `sub_70D2F0` | `getImmediate` | 107B | 1 | `(ctx, pool) -> str` | Reads operand at +672, conditionally appends second value |
| `sub_70FCB0` | `getParamA` | varies | varies | `(ctx) -> u64` | Dispatch on `(*(ctx+611) & 0x30)`: selects guardrail constant |
| `sub_70FCF0` | `getParamB` | varies | varies | `(ctx) -> u64` | Similar dispatch on different bit field |
| `sub_70E670` | `getParamC` | varies | varies | `(ctx) -> u64` | Third parameter accessor |

### Static String Tables

The accessor functions perform table-driven lookups using static string pointer arrays in `.rodata`. Each table is indexed by a small bit-field extracted from the context object:

| Table Address | Entries | Indexed By | Content |
|---------------|---------|------------|---------|
| `off_2032300` | >57 | Operand type code | Type suffix strings (`.f32`, `.u16`, `.b64`, etc.) |
| `off_2032700` | 4 | `(ctx+609 >> 2) & 3` | Base address mode strings |
| `off_20327C0` | 4 | `(ctx+615 >> 6) & 3` | FTZ flag strings (empty, `.ftz`, etc.) |
| `off_2032BA0` | 16 | `(ctx+605 >> 4) & 0xF` | Scale modifier strings |
| `off_2033060` | 256 | `ctx+620` | Variant name strings |
| `off_2033720` | 4 | `(ctx+627 >> N) & 3` | Extended operand strings |
| `off_2033DE0` | 2 | `ctx+600 >> 7` | Address operand strings |
| `off_2033E00` | 2 | `(ctx+600 & 0x40) != 0` | Scope strings (`.cta`, `.gpu`, etc.) |
| `off_2033E30` | 2 | `(ctx+600 & 4) != 0` | Shape strings -- WMMA/TCGEN05 |
| `off_2033EE0` | 2 | `ctx+600 & 1` | Layout strings -- WMMA/TCGEN05 |
| `off_2033FA0` | indexed by int | `ctx+640` | Precision strings for texture ops |

### Architectural Notes

1. **String interning**: String-returning accessors for type codes <= 0x39 go through a string interning table at `*(ctx+2488)`. The pattern is: look up a candidate string from the static table, then pass it through `sub_426D60` (hash lookup) or `sub_7072A0` (insert-and-return). This deduplicates PTX modifier strings across the entire text generation pass.

2. **Pool allocation**: Accessors that construct new strings (prefixing `"@"`, joining with separators) receive a pool allocator parameter. They allocate from the formatter's 50KB temp buffer via `sub_4280C0` (get pool) -> `sub_424070` (alloc from pool) -> `sub_42BDB0` (abort on failure).

3. **Duplicate functions**: `sub_70B700` (`hasPredicate`, 946 callers) and `sub_70B6E0` (`hasPredicate_v2`, 42 callers) have bytewise-identical bodies. Both return `*(a1+544) != 0`. These are likely methods in different classes (base and derived, or two sibling classes) that were not merged by the linker because they have distinct mangled names.

4. **MMA/tensor accessors**: `getFieldA` through `getFieldD`, `getLayoutString`, and `getShapeString` are used exclusively by WMMA, HMMA, and TCGEN05 instruction formatters. They decode matrix operation modifiers (`.transA`, `.transB`, `.row`, `.col`) from compressed bit fields.

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
- Whether a target-specific scheduler hook (vtable offset 2128 on the SM backend at compilation context +1584) vetoes the removal
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

## Instruction Lowering Handler -- `sub_65D640` (48 KB)

The central PTX-to-Ori instruction lowering handler lives at `sub_65D640`. It is installed at vtable offset +32 in the ISel Phase 1 dispatch table (`sub_660CE0`) and called through the vtable for every PTX instruction during lowering.

**Signature:** `int64 sub_65D640(context*, bb_ref, ptx_node*, ori_instr*)`

The function reads the PTX opcode from `*(*(ptx_node+32)+8)` and dispatches through a ~60-case switch. An entry gate (`sub_44AC80`) diverts certain opcode types to an alternate handler (`sub_656600`). The function calls `sub_A2FD90` (operand setter) 59 times to populate Ori operands on the resulting instructions.

### Opcode Case Map

| Case(s) | PTX family | Handler | Description |
|---|---|---|---|
| 5 | `prmt` (byte permute) | inline | Decodes 8-bit per-byte channel mask, sets 2 operands |
| 6 | `prmt` (extended) | inline | Two-operand permute with address computation via `sub_6294E0` |
| 10 | `mov` (special) | inline | Clears immediate flag for float type 109 |
| 12 | (delegated) | `sub_659F90` | -- |
| 13 | multi-operand expansion | inline | Expands via `sub_62E840`, resolves type 87 (address) and 97 (register) operands |
| 17, 18, 24 | `mov`/`cvt` variants | `sub_652FA0` | -- |
| 19, 20, 23 | surface ops | inline | ~200 lines: multi-register data, `sub_6273E0` operand classification, up to 4 data regs + address |
| 34, 35 | load/store | inline | Optional address resolution gated on `(ptx_node+61 & 0xC)` |
| 45, 238 | conversion | inline | Rewrites operand type to 20 (integer), binds address via `sub_6294E0` |
| 68, 71 | register indirect rewrite | inline | Checks operand size == 8, rewrites descriptor to type 110 |
| 81 | instruction expansion | inline | Creates IADD3 (opcode 38) with constant 0, reg class 12 |
| 82 | instruction expansion | inline | Rewrites to opcode 162 with IADD3 operand |
| 84 | load expansion | inline | Creates IADD3 with offset, flags 0x2000 |
| 85 | operand reorder | inline | 3-operand shuffle |
| 87 | reg class adjustment | inline | Table lookup at `dword_2026C60`, swaps operands 1/2, sets opcode 150 |
| 88 | matrix config | inline | MMA dimension table at `dword_2026C48`, sets fields 179/180 |
| 104 | 4-wide load | inline | Creates 4-operand instruction, address binding via `sub_6294E0` |
| 110 | (delegated) | `sub_652610` | -- |
| 123 | **generic addressing** | inline | Converts flat-to-specific addresses; SM-version-dependent multi-instruction sequences |
| 124, 125 | **cvta / isspacep** | inline | Address space conversion; creates CVTA opcode 538/539 on SM > 0x1A |
| 130 | instruction fusion | inline | Fuses instruction if operand count is not 3 or 4 |
| 165 | (delegated) | `sub_65BF40` | -- |
| 175--178 | **texture addr_mode** | inline | Resolves `.addr_mode_0/1/2` attributes from texture descriptor |
| 179 | atomic address mode | inline | Classifies atomic op type, creates SEL + ATOM sequence |
| 180 | (delegated) | `sub_65CE90` | -- |
| 181, 182 | (delegated) | `sub_64FF20` | -- |
| 183 | conditional atomic | inline | State space 0x20: rewrites to opcode 71 with mask 0xFF01010101 |
| 184--190 | surface/texture lowering | inline | Handles SULD/SUST/SURED (opcodes 449-456); SM-dependent operand resolution |
| 197, 198 | call site lowering | inline | Same-module vs cross-module call dispatch |
| 201--204, 208--211 | wide load/store | inline | `.v2`/`.v4` multi-element operations with IADD3 offset computation |
| 206, 207, 212, 213 | 3-op wide load/store | inline | 3-operand variants of wide memory operations |
| 221, 222 | TMA operations | inline | Sets field 197 with value 365/366 |

### Addressing Mode Types

ptxas handles four distinct addressing mode categories during instruction lowering, all resolved by `sub_65D640`:

#### 1. Texture Addressing Modes (per-dimension)

Cases 175--178 resolve `.addr_mode_0`, `.addr_mode_1`, `.addr_mode_2` attributes from texture descriptors. These are the PTX `txq` query targets.

The function walks the texture descriptor's attribute linked list at `*(descriptor+16)+24`, comparing each attribute name string:

```c
// Pseudocode for cases 175-178:
addr_mode_0 = addr_mode_1 = addr_mode_2 = 0;
found = false;
for (node = attr_list_head; node != NULL; node = *node) {
    name = *(node[1] + 16);    // attribute name string
    value = *(*(node[1] + 24) + 16);  // integer value
    if (strcmp(name, "addr_mode_0") == 0)  { addr_mode_0 = value; found = true; }
    else if (strcmp(name, "addr_mode_1") == 0)  { addr_mode_1 = value; found = true; }
    else if (strcmp(name, "addr_mode_2") == 0)  { addr_mode_2 = value; found = true; }
}
```

For 2D textures (state space byte & 0xB0 == 0x20), the function checks `addr_mode_0 == addr_mode_1`. For 3D textures (0x30), it checks all three equal. If modes are uniform (all equal), the instruction gets a single addressing mode flag (field 91 = 1 for clamp_to_border). If modes differ, it delegates to `sub_64FC90` for a multi-instruction lowering that handles per-dimension mode selection.

#### 2. Generic-to-Specific Address Conversion (case 123)

Converts flat/generic pointers to specific memory space pointers. The address space ID from `*(ptx_node+40)` selects the conversion strategy:

| Space ID | Memory space | Strategy |
|---|---|---|
| 4 | shared | `sub_654A90` (direct conversion) |
| 5 | combined | OR of global + shared + local conversions |
| 6 | local | `sub_64F7A0` with register pair 101/102 |
| 7 | generic (flat) | SM-dependent: `sub_654FB0` (SM <= 0x1A) or SHR/AND extraction + SEL mux (SM > 0x10) |
| 8 | global | `sub_64F7A0` with register pair 98/99 |

For generic space on older architectures (SM <= 0x1A with feature flag via `sub_61AF90`), a simpler single-instruction path is used. On newer architectures, a multi-instruction sequence extracts the space tag from the upper address bits.

#### 3. Address Space Conversion (cases 124--125, cvta/isspacep)

The `cvta` (Convert Address) and `isspacep` (Is Space Predicate) instructions convert between generic and specific address spaces. For global space (type 8) on SM > 0x1A, the handler creates CVTA with opcode 538 (isspacep) or 539 (cvta) and sets register class 7 with width 4 or 16 bytes.

#### 4. Memory Addressing Modes (implicit)

Memory addressing modes for load/store/atomic instructions are not enumerated as named constants. Instead, they emerge from the operand construction patterns in cases 19--23, 34--35, 81--84, 104, 201--213:

| Pattern | PTX syntax | Ori representation |
|---|---|---|
| Register indirect | `[%rd1]` | Operand type 87 from `sub_629E40` |
| Register + offset | `[%rd1+16]` | Register operand + immediate via `sub_6273E0` |
| Constant bank | `c[2][0x100]` | Constant operand via `sub_620320` (type 12) |
| Immediate address | `.local` space | Constant value via `sub_620320` |
| Base + index | `[%rd1], %r2` | Two-operand form |

### ISel Phase 1 Dispatch Vtable

`sub_660CE0` constructs a 17-slot vtable at context offset +3784 for the ISel Phase 1 instruction handlers:

| Offset | Handler | Size | Role |
|---|---|---|---|
| +0 | `sub_650840` | -- | Primary handler |
| +8 | `sub_64EEB0` | -- | Operand handler |
| +16 | `sub_64F270` | -- | Type handler |
| +24 | `sub_6575D0` | 49 KB | Register-class-to-opcode dispatch |
| +32 | `sub_65D640` | 48 KB | **Instruction lowering (this function)** |
| +40 | `sub_64EDD0` | -- | Auxiliary handler |
| +128 | `sub_64EEC0` | -- | Lowering helper |

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
| `sub_65D640` | 48KB | `InstrLowering::handle` | PTX-to-Ori instruction lowering handler (60+ opcode cases, addressing mode resolution) |
| `sub_660CE0` | 0.3KB | `InstrLowering::initVtable` | Constructs ISel Phase 1 dispatch vtable (17 slots) |
| `sub_6575D0` | 49KB | `RegClassOpcodeDispatch::handle` | Register-class-to-opcode dispatch (vtable +24 sibling) |
| `sub_6D9690` | 94KB | `Instruction::encode` | Master SASS instruction encoder |
| `sub_B28E00` | varies | `isReg/isPred/isImm` | Operand type predicates (isel infrastructure) |
| `sub_5D4190` | 12.9KB | `PTXFormatter::dispatch` | PTX text generation dispatcher (580 formatters) |
| `sub_710860` | 39B | `PTXCtx::getDataType` | Data type accessor (2,953 callers) |
| `sub_70B8E0` | 12B | `PTXCtx::getRegOperand` | Register operand accessor (1,449 callers) |
| `sub_70B910` | 12B | `PTXCtx::getSrcPart0` | Source part 0 accessor (1,656 callers) |
| `sub_70B700` | 14B | `PTXCtx::hasPredicate` | Predicate presence check (946 callers) |
| `sub_70CA60` | 11B | `PTXCtx::getOperandType` | Operand type code accessor (480 callers) |
| `sub_70B710` | 111B | `PTXCtx::getOpcodeString` | Opcode string with "@" prefix (348 callers) |
| `sub_70FA00` | 10B | `PTXCtx::getTargetSM` | Target SM version accessor (286 callers) |

## Related Pages

- [Ori IR Overview](./overview.md) -- Code Object, basic blocks, CFG, register files
- [Registers](./registers.md) -- Register descriptor layout, register file types
- [CFG](./cfg.md) -- Basic block structure, control-flow graph
- [Data Structures](./data-structures.md) -- Hash tables, bitvectors, linked lists
- [Peephole Optimization](../codegen/peephole.md) -- Instruction rewriting passes
- [SASS Encoding](../codegen/encoding.md) -- How Ori instructions become SASS binary
- [Instruction Selection](../codegen/isel.md) -- Pattern matching for instruction selection
- [PTX-to-Ori Pipeline](../pipeline/ptx-to-ori.md) -- Full lowering pipeline context for `sub_65D640`
- [Scheduling](../scheduling/overview.md) -- 3-phase instruction scheduler
