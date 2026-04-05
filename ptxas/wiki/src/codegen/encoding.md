# SASS Instruction Encoding

The SASS instruction encoder is the single largest subsystem in ptxas by function count. It translates the internal Ori IR instruction representation into packed binary SASS machine code for a specific SM target. The encoder comprises approximately 4,000 template-generated handler functions dispatched through function-pointer tables indexed by opcode, plus six massive switch-dispatch megafunctions that route field-level queries by instruction category. The core encoding primitive is a single 216-byte bitfield-insert function (`sub_7B9B80`) called from 18,347 sites throughout the binary. NVIDIA internally names this pipeline phase "Ori Phase Encoding" within the Mercury assembler backend.

| | |
|---|---|
| **Pipeline phase** | OriPhaseEncoding (within Mercury) |
| **Core bitfield packer** | `sub_7B9B80` (216 bytes, 18,347 callers) |
| **Encoding buffer** | 1280 bits = 20 QWORDs at `a1+544` |
| **Instruction widths** | 64-bit (format 1), 128-bit (format 2), 256-bit (format 8) |
| **Opcode hierarchy** | 3-level: major (9 bits) / minor (8 bits) / sub-opcode (7 bits) |
| **SM100 encoder count** | ~1,086 encode functions + ~97 decode functions |
| **SM100 opcode categories** | 370 (case values 0x0 through 0x171) |
| **SM100 major opcodes** | 102 unique values |
| **Bitfield accessor primitives** | 2,095 functions (mostly under 200 bytes) |
| **Confirmed strings** | `"AdvancedPhaseOriPhaseEncoding"`, `"MercEncodeAndDecode"`, `"After EncodeAndDecode"`, `"ENCODING"` |

## Encoding Buffer Layout

Every encoder operates on an instruction encoding context object passed as `a1`. The primary encoding target is a 1280-bit (160-byte, 20 QWORD) buffer at offset `a1+544`. The bitfield packer `sub_7B9B80` writes individual fields into this buffer by iterating in 64-bit chunks:

```c
// sub_7B9B80(a1, bit_offset, bit_width, value)
// Insert `value` into bits [bit_offset .. bit_offset+bit_width) of the encoding buffer
void bitfield_insert(int64_t a1, int bit_offset, int bit_width, uint64_t value) {
    uint64_t mask = (1ULL << bit_width) - 1;
    value &= mask;
    int pos = bit_offset;
    while (pos < 1280) {
        int qword_idx = pos >> 6;           // which QWORD
        int bit_in_qword = pos & 63;        // bit position within QWORD
        *(uint64_t*)(a1 + 8 * qword_idx + 544) |= value << bit_in_qword;
        // Handle fields that cross a QWORD boundary
        if (bit_in_qword + bit_width > 64) {
            int overflow = bit_in_qword + bit_width - 64;
            *(uint64_t*)(a1 + 8 * (qword_idx + 1) + 544) |= value >> (64 - bit_in_qword);
        }
        break;  // single insertion (loop structure handles interleaved format)
    }
}
```

The encoding context object has this layout:

| Offset | Size | Content |
|--------|------|---------|
| `+0x000` | 8B | vtable / allocator pointer |
| `+0x008` | 16B | Format descriptor (xmmword constant from rodata) |
| `+0x010` | 4B | Bitfield position base index |
| `+0x018` | 120B | Register class maps (3 arrays of 10 DWORDs: source classes, dest classes, widths) |
| `+0x090` | 4B | Operand count (`a1+144`) |
| `+0x094`--`+0x110` | | Explicit operand mapping table (pairs of index + bit position) |
| `+0x194` | 32B | Extended operand attributes (from xmmword tables) |
| `+0x1D4`--`+0x214` | 64B | Constant buffer slot table (16 DWORD slots, cleared to 0xFF by `sub_7B9D30`) |
| `+0x214` | 4B | Constant buffer slot counter (`a1+532`) |
| `+0x218` | 8B | Encoding validation context pointer (`a1+536`) |
| `+0x220` | 8B | Instruction bits [63:0] (`a1+544`) |
| `+0x228` | 8B | Instruction bits [127:64] (`a1+552`) |
| `+0x230`+ | | Additional encoding space (up to 1280 bits total) |

## Instruction Word Format

SASS instructions use a 3-level opcode hierarchy packed into the first 32 bits of the encoding buffer. The format code in bits [0:3] determines instruction width:

```
128-bit instruction word:
  bits [0:3]     = 0x2       (format code: 128-bit)
  bits [4:6]     = 0x0       (scheduling group slot 0)
  bits [8:16]    = MAJOR     (9-bit major opcode, 0x00-0x171)
  bits [17:24]   = MINOR     (8-bit minor opcode / variant)
  bits [25:31]   = SUBOP     (7-bit sub-opcode / format ID)
  bits [48+]     = MODIFIERS (format-dependent modifier fields)
  bits [132:134] = 0x0       (extended opcode flag, at offset 0x84)

64-bit instruction word:
  bits [0:3]     = 0x1       (format code: 64-bit)
  bits [4:6]     = 0x0       (scheduling group slot 0)
  bits [8:16]    = MAJOR     (9-bit major opcode)
  bits [17:24]   = MINOR     (8-bit minor opcode)
  bits [25:31]   = SUBOP     (7-bit sub-opcode)
  (no bit 132 extended flag -- only 5 initial sub_7B9B80 calls)

256-bit instruction word:
  bits [0:3]     = 0x8       (format code: 256-bit)
  (used for IMAD.WIDE variants with 16 constant-bank operand slots)
```

The 128-bit format uses 6 initial `sub_7B9B80` calls (including one at offset 0x84 for the extended flag). The 64-bit format uses only 5 (no 0x84 call). This is the reliable way to distinguish the two during analysis.

The maximum variant value observed is 0x2F (47 decimal), so each major opcode can have up to 48 sub-operations, though most have far fewer.

## Encoder Template

Every encoding handler function follows an identical 10-phase template. The only differences between the approximately 1,086 encoder functions for SM100 are the specific constant values and which modifier-encoding helpers are called. This is textbook C++ template/macro expansion:

```c
int64_t encode_OPCODE_VARIANT(int64_t a1, int64_t a2) {
    // a1 = instruction encoding context (output)
    // a2 = Ori IR instruction node (input)

    // Phase 1: Set instruction format header
    sub_7B9B80(a1, 0, 4, FORMAT_CODE);     // bits[0:3] = 1 (64b) / 2 (128b) / 8 (256b)
    sub_7B9B80(a1, 4, 3, 0);              // bits[4:6] = sched group slot 0
    sub_7B9B80(a1, 0x84, 3, 0);           // bits[132:134] = extended flag (128-bit only)
    sub_7B9B80(a1, 8, 9, OPCODE_CLASS);   // bits[8:16] = major opcode
    sub_7B9B80(a1, 0x11, 8, OPCODE_MINOR); // bits[17:24] = minor opcode / variant
    sub_7B9B80(a1, 0x19, 7, FORMAT_ID);   // bits[25:31] = sub-opcode / format ID

    // Phase 2: Load operand format descriptor
    *(xmmword*)(a1 + 8) = xmmword_23FXXXX; // 128-bit format field layout from rodata
    // Copy 3 arrays of 10 DWORDs into a1+24..a1+140 (slot sizes, types, flags)

    // Phase 3: Set operand count and modifier table
    *(int*)(a1 + 144) = NUM_SOURCE_OPERANDS;
    *(xmmword*)(a1 + 404) = xmmword_YYYYYYY; // modifier descriptor table

    // Phase 4: Initialize encoding context
    sub_7B9D30(a1);         // clear constant buffer slot table (memset +468, 0xFF, 64)
    sub_7B9D60(a1, a2, 0);  // encode reuse flags + guard predicate

    // Phase 5: Encode primary opcode ID
    void* ctx = *(void**)(a1 + 536);
    int opcode = sub_10BFxxx(*(a2+32) + 32 * *(a2+40));  // extract from IR operand
    int encoded = sub_10B6180(ctx, opcode);                // map through lookup table
    sub_7B9B80(a1, 8 * *(a1+16), 1, encoded);             // insert at computed position

    // Phase 6: Encode source operands (variable number and types)
    sub_7BC030(a1, a2, 0, 0x60);  // register operand 0 at bit offset 0x60
    sub_7BC030(a1, a2, 1, 0x70);  // register operand 1 at bit offset 0x70
    sub_7BCF00(a1, a2, 2, 0x88);  // immediate operand 2 at bit offset 0x88
    sub_7BC5C0(a1, a2, 3, 0x98);  // predicate operand 3 at bit offset 0x98

    // Phase 7: Encode instruction-specific modifiers
    int mod_val = sub_10B96A0(a2);              // read modifier from IR node
    int enc_mod = sub_10B3680(ctx, mod_val);    // validate and map
    *(int64_t*)(a1+544) |= ((int64_t)enc_mod << 55) & 0x180000000000000LL;

    // Phase 8: Encode explicit operand mapping (source operands with data offsets)
    *(int*)(a1 + 148) = operand_index;
    *(int*)(a1 + 152) = 8 * bit_position;
}
```

## Operand Type Encoders

Four type-specific helper functions encode operands into the instruction word. Each reads the operand descriptor from the IR instruction's operand table at `*(a2+32) + 32*operand_index`.

### Register Operand Encoder -- `sub_7BC030`

814 bytes, 6,147 callers. Encodes a general-purpose register (R0-R255, UR0-UR63):

```c
// sub_7BC030(insn, ir_insn, operand_index, bit_offset)
void encode_register(int64_t a1, int64_t a2, int op_idx, int bit_off) {
    if (op_idx >= *(int*)(a2 + 92))  // check operand count
        return;
    void* operand = *(void**)(a2 + 32) + 32 * op_idx;
    int reg_type_raw = *(int*)(operand + 20);

    // Map register file type to 4-bit encoding:
    //   1->0, 2->1, 3->2, 4->3, 5->4, 6->5, 7->6, 8->7,
    //   16->8, 32->9, 64->10, 128->11
    int reg_type = map_regfile(reg_type_raw);
    int reg_num = *(int*)(operand + 4);  // signed register number

    sub_7B9B80(a1, bit_off, 1, 1);           // 1-bit presence flag
    sub_7B9B80(a1, bit_off + 1, 4, reg_type); // 4-bit register type
    sub_7B9B80(a1, bit_off + 6, 10, reg_num); // 10-bit register number
}
```

The register file type encoding maps raw operand type codes to a 4-bit hardware register file selector. The 12 supported values (1 through 128 in powers of 2) cover GPR, uniform registers, predicate registers, special registers, and extended register files.

### Immediate / Constant-Buffer Encoder -- `sub_7BCF00`

856 bytes, 1,657 callers. Encodes immediate values and constant memory references (`c[bank][offset]`):

```c
// sub_7BCF00(insn, ir_insn, operand_index, bit_offset)
void encode_immediate(int64_t a1, int64_t a2, int op_idx, int bit_off) {
    void* operand = *(void**)(a2 + 32) + 32 * op_idx;
    int type = *(uint8_t*)operand;

    if (type == 14 || type == 15 || type == 16) {
        // Predicate-typed immediate: store to constant buffer slot table
        *(void**)(a1 + 468 + 8 * *(int*)(a1 + 532)) = operand + 8;
        *(int*)(a1 + 532) += 1;
    }
    sub_7B9B80(a1, bit_off, 1, 1);               // presence flag
    sub_7B9B80(a1, bit_off + 11, 5, *(int*)(operand + 4)); // 5-bit value
}
```

### Predicate Encoder -- `sub_7BC5C0`

416 bytes, 1,449 callers. Encodes predicate register operands (PT, P0-P6):

```c
// sub_7BC5C0(insn, ir_insn, operand_index, bit_offset)
void encode_predicate(int64_t a1, int64_t a2, int op_idx, int bit_off) {
    void* operand = *(void**)(a2 + 32) + 32 * op_idx;
    sub_7B9B80(a1, bit_off, 2, pred_type);       // 2-bit predicate type
    sub_7B9B80(a1, bit_off + 3, 3, pred_cond);   // 3-bit condition code
    sub_7B9B80(a1, bit_off + 8, 8, pred_value);  // 8-bit predicate value
}
```

### Uniform Register Encoder -- `sub_7BC360`

Used for uniform registers (UR0-UR63) and source operands with alternative bitfield layouts. 126 calls in the SM100 encoding range. Likely handles the UR register file which has a separate encoding namespace from the main GPR file.

## Instruction Format Groups

The encoder functions are organized into 16 format groups, identified by the xmmword constant loaded at `a1+8`. Each xmmword holds the field layout descriptor for that instruction format. The groups divide into two categories:

### 128-bit Formats (11 groups)

| Format Descriptor | Format ID | Encoder Count | Description |
|---|---|---|---|
| `xmmword_23F1DF8` | 0x03 | 145 | General ALU/memory -- most common format |
| `xmmword_23F29A8` | 0x19 | 117 | Extended format for complex instructions |
| `xmmword_23F21B0` | 0x0A | 99 | Multi-source ALU operations |
| `xmmword_23F2678` | 0x13 | 27 | Tensor/extended ALU |
| `xmmword_23F2018` | 0x07 | 9 | Miscellaneous ALU |
| `xmmword_23F2348` | 0x0D | 6 | Specialized ALU |
| `xmmword_23F2EF8` | 0x23 | 5 | Extended variant |
| `xmmword_23F2810` | 0x16 | 4 | Bulk data / DMA |
| `xmmword_23F2128` | 0x09 | 2 | Rare format |
| `xmmword_23F2DE8` | 0x21 | 2 | Rare extended |
| `xmmword_23F25F0` | 0x12 | 2 | Rare format |

### 64-bit Formats (5 groups)

| Format Descriptor | Encoder Count | Description |
|---|---|---|
| `xmmword_23F1F08` | 113 | Short-form general -- widest opcode coverage (27 classes) |
| `xmmword_23F1D70` | 41 | Short-form 4-operand |
| `xmmword_23F1F90` | 11 | Short-form variant |
| `xmmword_23F2238` | 8 | Short-form variant |
| `xmmword_23F2C50` | 1 | Minimal format |

The 128-bit group (format code 2) encodes long-form SASS instructions (ALU, load/store, texture, tensor core). The 64-bit group (format code 1) encodes short-form instructions (simple moves, branches, barriers, NOP-like control). Two additional functions use format code 8 (256-bit) for IMAD.WIDE variants with 16 constant-bank operand slots.

## Instruction Format Group Catalog

### Format Descriptor Architecture

Each format group is defined by a 128-bit xmmword constant stored in rodata at addresses 0x23F1xxx--0x23F2xxx. This descriptor is loaded via SSE into the encoding context at `a1+8`:

```c
*(__m128i *)(a1 + 8) = _mm_loadu_si128(&xmmword_23F29A8);
```

Immediately following each xmmword in rodata are three arrays of 10 DWORDs that define the operand slot layout. The encoder copies these into the context object at `a1+24` through `a1+140`:

| Rodata Array | Context Offset | Content |
|---|---|---|
| `dword_XXXXX8[10]` | `a1+24` .. `a1+60` | Operand slot sizes (bits per slot) |
| `dword_XXXXE0[10]` | `a1+64` .. `a1+100` | Operand slot types (register class selector) |
| `dword_XXXXX8[10]` | `a1+104` .. `a1+140` | Operand slot flags (encoding mode flags) |

Observed slot-size values: `10` = register (10-bit number + overhead), `12` = register with type, `17` = immediate/cbuf, `-1` = unused. Slot-type values: `28` = register-type, `0` = basic, `-1` = unused. Slot-flag values: `0` = default, `2` = secondary (uniform/extended), `-1` = unused.

The copy uses SSE aligned loads for 16-byte chunks and scalar DWORD stores for remainders. The alignment check visible in every decompiled encoder (`if (a1 + 120 <= dword_XXXXX8 || a1 + 24 >= &dword_XXXXX8)`) is a compiler-generated overlap guard for the `memcpy`-like bulk copy.

### Bitfield Packer Detail -- `sub_7B9B80`

The core encoding primitive. 216 bytes compiled, 18,347 callers total. Inserts an arbitrary-width bitfield into the 1280-bit buffer at `a1+544`:

```c
// sub_7B9B80(a1, bit_offset, bit_width, value)
// Reconstructed algorithm from decompiled code:
__int64 bitfield_insert(__int64 a1, uint32_t bit_offset, int bit_width, uint64_t value) {
    uint32_t end = bit_offset + bit_width;
    uint32_t neg_base = -64 - bit_offset;  // pre-computed right-shift amount
    uint32_t pos = 0;
    do {
        while (1) {
            uint32_t chunk_end = pos + 64;
            if (bit_offset > pos + 63 || end <= pos) goto next;  // no overlap

            uint32_t start = (bit_offset >= pos) ? bit_offset : pos;
            uint32_t stop  = (end <= chunk_end) ? end : chunk_end;
            int width = stop - start;
            int shift_right = (chunk_end + neg_base < 0) ? 0 : chunk_end + neg_base;
            int bit_in_qword = start & 0x3F;
            __int64 *qword = (__int64*)(a1 + 8 * (start >> 6) + 544);
            uint64_t shifted = value >> shift_right;

            if (width == 64)
                *qword |= shifted << bit_in_qword;
            else
                *qword |= (shifted & ~(-1ULL << width)) << bit_in_qword;
        next:
            pos = chunk_end;
            if (chunk_end == 1280) return pos;
        }
    } while (pos != 1280);
    return pos;
}
```

Key properties:
- Handles cross-QWORD-boundary fields: a 9-bit opcode starting at bit 59 writes 5 bits to QWORD 0 and 4 bits to QWORD 1
- Loop terminates at bit position 1280 (20 QWORDs), hard ceiling
- For typical field widths (1--9 bits), executes 1--2 iterations
- Called 8--12 times per encoder function (average ~10)
- The 256-bit format encoders call it with wider fields (up to 32 bits for data values)

### 128-bit Format 0x03 -- General ALU/Memory (145 encoders)

The most populous format group. Handles the bread-and-butter ALU and memory instructions.

| Property | Value |
|---|---|
| **Descriptor** | `xmmword_23F1DF8` |
| **Format ID** | 0x03 (bits[25:31]) |
| **Slot arrays** | `dword_23F1E08`, `dword_23F1E30`, `dword_23F1E40` |
| **Operand slots** | 2--7 per instruction |
| **Typical pattern** | 3 reg + 1 imm + 1 pred (5 slots) |
| **Modifier fields** | 4--8 per instruction |

Opcode classes (29): 0x08, 0x0B, 0x0F, 0x10, 0x16, 0x17, 0x19, 0x1A, 0x1B, 0x20, 0x22, 0x25, 0x28, 0x2A, 0x2B, 0x30, 0x32, 0x34, 0x35, 0x36, 0x37, 0x38, 0x3B, 0x41, 0x45, 0x4A, 0x4B, 0x5B, 0x67.

### 128-bit Format 0x19 -- Extended Complex (117 encoders)

Second most common. Used for instructions with rich modifier fields or unusual operand configurations.

| Property | Value |
|---|---|
| **Descriptor** | `xmmword_23F29A8` |
| **Format ID** | 0x19 (bits[25:31]) |
| **Slot arrays** | `dword_23F29B8`, `dword_23F29E0`, `dword_23F2A08` |
| **Operand slots** | 3--6 per instruction |
| **Modifier fields** | 5--8 per instruction |

Opcode classes (8): 0x0F, 0x10, 0x1A, 0x1B, 0x22, 0x38, 0x4D, 0x5E. Notable concentration: opcode 0x1B has 41 variants in this format alone (tensor/MMA family); opcode 0x5E has 26 variants. The load/store family (0x38) uses this format for 7 of its 16 variants -- the ones with extended addressing modes.

### 128-bit Format 0x0A -- Multi-Source ALU (99 encoders)

Designed for instructions with 4--7 source operands. Heavily weighted toward rich ALU operations.

| Property | Value |
|---|---|
| **Descriptor** | `xmmword_23F21B0` |
| **Format ID** | 0x0A (bits[25:31]) |
| **Operand slots** | 4--7 per instruction |
| **Typical pattern** | 4 reg + 1 imm + 1 pred |

Opcode classes (10): 0x10, 0x16, 0x17, 0x20, 0x25, 0x28, 0x2A, 0x45, 0x4B, 0x67. Opcode 0x2A dominates with 30 variants; opcode 0x25 has 18.

### 128-bit Format 0x13 -- Tensor/Extended ALU (27 encoders)

Contains the most complex encoders in the binary. Opcode 0x5A variant 0x02 (`sub_D89C90`, 2015 bytes) has 18 modifier fields -- the maximum observed.

| Property | Value |
|---|---|
| **Descriptor** | `xmmword_23F2678` |
| **Format ID** | 0x13 (bits[25:31]) |
| **Slot arrays** | `dword_23F2688`, `dword_23F26B0`, `dword_23F26D8` |
| **Operand slots** | 4--7 per instruction |
| **Modifier fields** | 8--18 per instruction |

Opcode classes (7): 0x10, 0x16, 0x17, 0x1A, 0x41, 0x5A, 0x67.

### 128-bit Formats 0x07, 0x0D, 0x23, 0x16, 0x09, 0x21, 0x12 -- Rare Formats (35 encoders combined)

| Descriptor | Format ID | Encoders | Opcode Classes |
|---|---|---|---|
| `xmmword_23F2018` | 0x07 | 9 | 0x0F, 0x10 |
| `xmmword_23F2348` | 0x0D | 6 | 0x0F, 0x16, 0x67 |
| `xmmword_23F2EF8` | 0x23 | 5 | 0x10 |
| `xmmword_23F2810` | 0x16 | 4 | 0x4B (bulk/DMA) |
| `xmmword_23F2128` | 0x09 | 2 | -- |
| `xmmword_23F2DE8` | 0x21 | 2 | -- |
| `xmmword_23F25F0` | 0x12 | 2 | 0x4B |

Format 0x16 and 0x12 share opcode class 0x4B, suggesting they encode different addressing-mode variants of the same bulk-data instruction.

### 64-bit Format A (`xmmword_23F1F08`) -- Short-Form General (113 encoders)

Widest opcode coverage of any single format. Covers 27 distinct opcode classes with few variants each -- the simple, common instructions.

| Property | Value |
|---|---|
| **Descriptor** | `xmmword_23F1F08` |
| **Operand slots** | 0--3 per instruction |
| **Register offsets** | 0x40, 0x50, 0x60, 0x70 |

Opcode classes (27): 0x00--0x09, 0x0A--0x0F, 0x10, 0x11, 0x12, 0x14, 0x16, 0x1B, 0x1C, 0x20, 0x21, 0x23, 0x25. Many of these are NOP/control, simple moves, and compact branches.

### 64-bit Format B (`xmmword_23F1D70`) -- Short-Form 4-Operand (41 encoders)

Bimodal operand count: either 0 operands (control instructions) or 4 operands (compact arithmetic with all-register sources).

Opcode classes: 0x00--0x09, 0x10, 0x12, 0x14--0x1E, 0x26, 0x28, 0x2A.

### 64-bit Formats C, D, E -- Specialized Short Forms (20 encoders combined)

| Descriptor | Encoders | Notes |
|---|---|---|
| `xmmword_23F1F90` | 11 | Short-form variant C |
| `xmmword_23F2238` | 8 | Short-form variant D |
| `xmmword_23F2C50` | 1 | Minimal format, single encoder; also appears in 128-bit category with 0 uses |

### Distinguishing 64-bit vs 128-bit Encoders

The 128-bit format sets the extended opcode flag at bit offset 0x84, which the 64-bit format does not:

```
128-bit (6 initial sub_7B9B80 calls):
  sub_7B9B80(a1, 0,    4, 2)     // format code = 2
  sub_7B9B80(a1, 4,    3, 0)     // sched group slot
  sub_7B9B80(a1, 0x84, 3, 0)    // extended flag at bit 132  <-- PRESENT
  sub_7B9B80(a1, 8,    9, MAJ)   // major opcode
  sub_7B9B80(a1, 0x11, 8, MIN)   // minor opcode
  sub_7B9B80(a1, 0x19, 7, FID)   // format ID

64-bit (5 initial sub_7B9B80 calls):
  sub_7B9B80(a1, 0,    4, 1)     // format code = 1
  sub_7B9B80(a1, 4,    3, 0)     // sched group slot
                                  // NO 0x84 call           <-- ABSENT
  sub_7B9B80(a1, 8,    9, MAJ)   // major opcode
  sub_7B9B80(a1, 0x11, 8, MIN)   // minor opcode
  sub_7B9B80(a1, 0x19, 7, FID)   // format ID
```

The 256-bit format (format code 8) is used by exactly 2 encoders for IMAD.WIDE (major 0x59, minor 0x02 and 0x03), each with 16 constant-buffer operand slots encoded via `sub_7BCF00`.

## Dispatch Tables -- The Six Megafunctions

Six switch-dispatch megafunctions in the 0x10C0B20--0x10E32E0 range form the central routing logic of the instruction codec. All six switch on the opcode category at `*(WORD*)(a1+12)` with up to 370 cases (0x0 through 0x171), each containing sub-switches on field ID:

| Function | Size | Decompiled Lines | Callers | Purpose |
|---|---|---|---|---|
| `sub_10C0B20` | 180 KB | 9,231 | 3,109 | **setField** -- write a value into a named field |
| `sub_10D5E60` | 197 KB | 6,491 | 961 | **getFieldOffset** -- return bit-offset of a named field |
| `sub_10E32E0` | 187 KB | 6,240 | 72 | **hasField** -- boolean: does this instruction have field X? |
| `sub_10CCD80` | 142 KB | 7,581 | 4 | **setFieldDefault** -- write hardcoded default for a field |
| `sub_10CAD70` | 68 KB | 1,864 | 74 | **getOperandFieldOffset** -- bit-offset of a per-operand field |
| `sub_10C7690` | 65 KB | 2,313 | 288 | **setOperandField** -- write a per-operand field value |

`sub_10C0B20` (setField) is one of the most-called functions in the entire ptxas binary at 3,109 call sites. It writes field values using `sub_AF80xx` writer stubs and contains jump-out targets (`0xAF43F0`, `0xAF44C0`, `0xAF4550`) for bit-manipulation operations that cross word boundaries.

`sub_10D5E60` (getFieldOffset) returns `extractor(a1+48, bit_position) + base_offset` for each field, where `base_offset` is a field-specific constant (observed values: +125, +790, +1278, etc.). Returns `0xFFFFFFFF` when the queried field does not exist in the given instruction category.

`sub_10CAD70` (getOperandFieldOffset) operates on per-operand packed records at `*(QWORD*)(a1+32) + 32*operand_index + 24`. The field IDs it handles include: 1 (register class), 2 (operand type), 7, 8, 12 (operand size), 13 (address mode), 14, 15, 19, 24, 25, 26, 27, 29.

Dead cases (opcode categories without the queried field) share a common pattern: cases 3, 0x11, 0x24, 0x26, 0x2D, 0x75, 0x78, 0x84, 0x8C-0x9F, 0xA1-0xBA, 0xBE-0x16F return `0xFFFFFFFF` or false.

## Bitfield Accessor Library

The 0x10B0000--0x10BF2C0 range contains 2,095 machine-generated bitfield read/write primitives for the 192-bit packed instruction format. These are the building blocks that the six megafunctions call:

- 1,661 functions under 200 bytes: pure getters/setters for individual fields
- 412 functions between 200-500 bytes: multi-field accessors
- 22 functions above 500 bytes: complex accessors with validation

Seven core extractors handle all bitfield reads:

| Function | Width | Storage Format |
|---|---|---|
| `sub_10B28E0` | 1-bit | 192-bit (3x QWORD) |
| `sub_10B2860` | 2-bit | 192-bit |
| `sub_10B27E0` | 3-bit | 192-bit |
| `sub_10B2760` | 4-bit | 192-bit |
| `sub_10B26E0` | 5-bit | 192-bit |
| `sub_10B2650` | 2-bit | 32-bit array |
| `sub_10B25C0` | 3-bit | 32-bit array |

The 192-bit format (3 QWORDs = 24 bytes, stored at `a1+48`) handles boundary crossing: if a bitfield spans a QWORD boundary, the extractor combines partial reads from adjacent words. The 32-bit-array format is used for sub-fields that are naturally DWORD-aligned.

A typical accessor is trivially simple:

```c
// sub_10BEF80 (140 bytes)
int get_field_X(int64_t a1) {
    return (*(uint32_t*)(a1 + 24) & 3) + 51;  // extract 2-bit field, add base
}
```

## Modifier Encoding

After operands are encoded, each handler packs instruction-specific modifier fields into the bits at `a1+544` (primary word) and `a1+552` (extended word). The pattern is:

1. Read modifier value from IR node via a property extractor (`sub_10B9xxx` family)
2. Validate and map through an encoding lookup table (`sub_10B3xxx`/`sub_10B4xxx` family)
3. OR the result into the packed word at a shifted/masked position

The most commonly used modifier-encoding functions:

| Function | Callers | Bits | Likely Meaning |
|---|---|---|---|
| `sub_10B6180` | 8,091 | 1 | Boolean flag (.S, .STRONG, .SAT, etc.) |
| `sub_10B6160` | 2,205 | 1 | Boolean flag (.NEG, .ABS, etc.) |
| `sub_10B6140` | 1,645 | 1 | Boolean flag variant |
| `sub_10B2D90` | 538 | 2 | Data type, rounding mode |
| `sub_10B5580` | 475 | 5 | Shift amount, cache policy |
| `sub_10B44E0` | 416 | 2 | Addressing mode |
| `sub_10B6220` | 363 | 3 | Register bank, cache level |
| `sub_10B4650` | 330 | 4 | Type qualifier, address mode |
| `sub_10B47F0` | 243 | 4 | Type qualifier variant |
| `sub_10B2F00` | 151 | 3 | 3-bit modifier field |
| `sub_10B2F20` | 101 | 4 | 4-bit modifier field |

Modifier fields per instruction range from 0 (simple control instructions) to 18 (the most complex encoder, `sub_D89C90` for opcode class 0x5A). The average is approximately 6 modifier fields per encoder. Bit positions in `a1+544` concentrate in bits 48-63; bit positions in `a1+552` concentrate in bits 0-11.

## Decoder Functions

97 decoder functions in the 0xEB3040--0xED0FE0 range reverse the encoding: they extract operand information from packed SASS bitfields back into Ori IR representation. The decoder entry point is `sub_EB3040`, a dispatcher that performs binary search on the instruction type word (`*(a2+12)`, `*(a2+14)`, `*(a2+15)`) against a table at `off_22E6380`. For instruction types 120/121, it falls through to the generic decoder `sub_7BFAE0`.

The decoder template mirrors the encoder but in reverse:

```c
void decode_OPCODE(int64_t a1, int64_t a2) {
    // 1. Set output instruction type
    *(uint16_t*)(a2 + 12) = INSTR_TYPE_ID;

    // 2. Load operand format table (same xmmword constants as encoder)
    // 3. Set operand count
    *(int*)(a1 + 144) = NUM_OPERANDS;

    // 4. Decode operands using type-specific decoders
    sub_7BD3C0(a1, a2, 0, 0x50, 2);   // GPR register (type=2)
    sub_7BE090(a1, a2, 1, 0x60, 3);   // predicate register (type=3)
    sub_7BD650(a1, a2, 2, 0x70, 10);  // extended register (type=10)

    // 5. Extract control bits (reuse flags, stall counts, yield hints)
    sub_7BD260(a1, a2);

    // 6. Translate encoded values back to IR references
    int reg = sub_AF7DF0(*(void**)(a1+536), extracted_bits);
    sub_B056B0(dest_ptr, reg);
    int pred = sub_AF7200(*(void**)(a1+536), pred_bits);
    sub_AFA380(a2, pred);

    // 7. Extract modifier bitfields (reverse of encoder phase 7)
    sub_AF53B0(*(void**)(a1+536), *(a1+550) & mask);
    sub_AFCEB0();  // commit extracted value
}
```

Decoder operand count distribution: 6 two-operand, 18 three-operand, 22 four-operand, 16 five-operand, 22 six-operand, 12 eight-operand decoders.

## Opcode ID Extractors

Over 100 small functions in the 0x10BF000--0x10C0C00 range serve as opcode discriminators. Each maps an IR instruction node to an opcode ID by reading fields from the operand table. The most-used extractors:

| Function | Encoder Users | Major Opcode Family |
|---|---|---|
| `sub_10BF440` | 48 | Generic (most common) |
| `sub_10BF230` | 45 | Generic |
| `sub_10BF590` | 43 | Generic |
| `sub_10BFA90` | 30 | 0x59 (IMAD variants) |
| `sub_10BFD30` | 26 | 0xFD family |
| `sub_10BFFA0` | 25 | 0x4F family |
| `sub_10BF580` | 23 | 0x29 (IADD/IADD3) |
| `sub_10BF680` | 16 | 0x38 (load/store) |
| `sub_10C0AF0` | 14 | 0xDF (WGMMA) |

89 distinct opcode reader functions cover all instruction families.

## Per-SM Architecture Encoding

The encoding system is replicated per SM target. Each SM architecture has its own set of encoder/decoder functions with different xmmword opcode constants. The SM100 (Blackwell datacenter) implementation spans these address ranges:

| Range | Functions | Layer |
|---|---|---|
| 0xD27000--0xDFC000 | 592 | Encoder stubs (p1.12) |
| 0xDFC000--0xEB2AE0 | 494 | Encoder stubs continuation (p1.13) |
| 0xEB3040--0xED0FE0 | 97 | Decoder functions (p1.13) |
| 0x107B1E0--0x10AD700 | 641 | Encoder stubs continuation (p1.16) |
| 0x10ADD30--0x10AFF80 | 78 | Instruction lifecycle & scheduling |
| 0x10B0000--0x10BF2C0 | 2,095 | Bitfield accessor library (p1.16) |
| 0x10C0B20--0x10E32E0 | 184 | Dispatch table megafunctions (p1.16) |
| 0x10EE900--0x1134160 | ~400 | Binary encoders: IR fields to bits (p1.16) |
| 0x1134160--0x114F380 | ~132 | High-level encode path (p1.16) |

The total SM100 codec spans roughly 2.5 MB of binary code across approximately 4,700 functions (including the shared bitfield accessor library).

Other SM targets (SM75 Turing, SM80 Ampere, SM86 Ada, SM89 Lovelace, SM90a Hopper, SM103 Blackwell Ultra, SM120 consumer Blackwell) have parallel encoder populations in the p1.14, p1.15, p1.17--p1.22 address ranges, each with matched xmmword constants for their architecture-specific instruction set.

## Operand Encoding Patterns

The 576 encoder functions in the p1.12 range use 52 distinct operand encoding patterns. The most common:

| Pattern (reg, imm, pred) | Count | Description |
|---|---|---|
| 3 reg + 1 pred | 88 | Standard 3-source with predicate |
| 2 reg + 1 pred | 57 | Binary op with predicate |
| 3 reg only | 43 | Ternary ALU, no predicate/immediate |
| 3 reg + 1 imm + 1 pred | 42 | MAD-class with immediate + predicate |
| 2 reg only | 40 | Simple binary |
| 3 reg + 1 imm | 25 | Ternary with immediate |
| 1 reg + 1 pred | 22 | Unary with predicate |
| 4 reg + 1 imm | 21 | Quaternary with immediate |
| 4 reg only | 20 | Quaternary register-only |

Register operand bit offsets are format-dependent:
- 64-bit format: 0x40, 0x50, 0x60, 0x70
- 128-bit format: 0x60, 0x70, 0x88, 0x98, 0xA8

## Major Opcode Summary (SM100)

102 unique major opcodes were identified across 494 encoding variants (p1.13 range alone). Opcode-to-mnemonic mapping is inferred from operand patterns and opcode density; exact mnemonic assignment requires correlation with ROT13-obfuscated instruction names found elsewhere in the binary.

### Memory / Load-Store

| Major | Variants | Likely SASS Mnemonics |
|---|---|---|
| 0x38 | 16 | LDG, STG, LDS, STS |
| 0x60 | 2 | Extended load |
| 0x70--0x72 | 9 | Load groups A/B/C |
| 0xA4--0xA6 | 12 | Load/store with addressing modes |
| 0xAD | 9 | Memory extended |
| 0x1E | 4 | ATOM, ATOMS |
| 0x99, 0xA2 | 2 | Extended atomics |
| 0x39 | 2 | REDUX (reduction) |

### Integer Arithmetic

| Major | Variants | Likely SASS Mnemonics |
|---|---|---|
| 0x59 | 30 | IMAD, IMAD.HI, IMAD.WIDE, ISCADD |
| 0x29 | 24 | IADD3, IADD3.64, IADD32I |
| 0x4F | 25 | Extended integer operations |
| 0x3B | 10 | Integer MUL/MAD extended |

### Floating Point

| Major | Variants | Likely SASS Mnemonics |
|---|---|---|
| 0x3A | 1 | Float operation |
| 0x3E--0x40 | 4 | FFMA, FFMA variants |
| 0x43--0x44 | 2 | Float MUL/MAD |
| 0x4A | 4 | FADD, FMUL, FFMA forms |
| 0x49 | 6 | HFMA2, HADD2, HMUL2 |
| 0x5C | 6 | HFMA2 variants |
| 0x5F | 2 | Half-float extended |

### Tensor Core / WGMMA

| Major | Variants | Likely SASS Mnemonics |
|---|---|---|
| 0xA8--0xA9 | 16 | Tensor core A/B (WGMMA, HMMA) |
| 0xAB--0xAC | 12 | Tensor core C/D |
| 0xAE--0xB0 | 30 | Tensor core E/F/G |
| 0xB1--0xB3 | 15 | Tensor core H/I/J |
| 0xDF | 14 | WGMMA dispatch (main family) |
| 0x12 | 4 | Matrix operations |
| 0x54 | 6 | Extended matrix |

### Control Flow

| Major | Variants | Likely SASS Mnemonics |
|---|---|---|
| 0x18 | 10 | BRA, SSY, CAL, EXIT, RET, BREAK, CONT |
| 0x19 | 5 | Control flow group B |
| 0x7D | 2 | YIELD, control |
| 0x24 | 2 | BAR, barrier/sync |
| 0xCF | 3 | BARRIER |
| 0xD4 | 2 | BARRIER B |
| 0x33 | 2 | DEPBAR |

### Comparison / Predicate

| Major | Variants | Likely SASS Mnemonics |
|---|---|---|
| 0x0D | 10 | ISETP, FSETP, DSETP |
| 0x17 | 8 | PSETP, PLOP3 |
| 0x95 | 6 | Comparison variants |

### Data Movement / Conversion

| Major | Variants | Likely SASS Mnemonics |
|---|---|---|
| 0x61 | 5 | MOV, MOV.64, MOV32I |
| 0x46, 0x66, 0x45 | 3 | MOV variants |
| 0x56 | 6 | F2I, I2F, F2F type conversions |
| 0x62 | 6 | Type conversion group 2 |
| 0x10 | 4 | SEL (conditional select) |
| 0x1B | 3 | PRMT (permute) |

## Instruction Object Lifecycle

The instruction object constructor `sub_10AFF80` (11 KB, 3 callers: `sub_6F0A30`, `sub_6F52F0`, `sub_9EE390`) takes 32 parameters and builds a ~900-byte instruction-level object:

- 13 sub-object allocations via vtable allocator (`vtable+24`)
- 4 linked-list structures for instruction chaining
- 2 string buffers for instruction name and alternate name (via `strlen` + `memcpy`)
- Architecture descriptor via `sub_B19110(arch_id)` at offset +408
- Hash table using FNV-1a (seed `0x811C9DC5`, prime `16777619`) for instruction record lookup

The instruction unlink-and-recycle functions (`sub_10ADF90`, `sub_10AE190`) remove an instruction node from a doubly-linked list (head/tail at `a1+48/56`), update the count at `a1+64`, free operand attachments via vtable call, and return the node to a free-list at `a1+72`. The maximum instruction count per list is 16,383 (checked by `sub_10AE7C0`).

## Encoding Pipeline Layers

The full encoding pipeline operates in three layers, from high-level IR to binary output:

**Layer 1: High-level encode (0x1134160--0x114F380, ~132 functions)**
Populates full IR records before low-level packing. Uses `sub_9B3C20(a1, a2, slot, type, mode, width, reg_id)` for register operands and `sub_9B3D60` for immediates. Handles 255->1023 sentinel translation for "don't care" register values. Sets opcode/modifier fields via `sub_AFA910`/`sub_AFA930`. Applies conditional fixups: e.g., if `opcode==2038 && subopcode==2257`, sets `operand_slot+84 = 5`.

**Layer 2: Binary encoders (0x10EE900--0x1134160, ~400 functions)**
Reads operand fields from IR via `sub_10BDxxx` extractors, transforms through `sub_10Bxxx` lookup tables, and packs results into the 128-bit output word at `*(QWORD*)(a1+40)`:

```c
// Typical pattern (sub_10F91D0):
int v6 = sub_10BF170(operand_addr);       // extract register class
int v7 = sub_10B6180(lookup_table, v6);    // translate to encoding value
*(uint64_t*)(a1 + 40) |= ((uint64_t)v7 << 15);  // pack at bit position 15
```

Includes a register pair encoder (`sub_112CDA0`, 8.9 KB) that maps 40 register pair combinations (R0/R1, R2/R3, ... R78/R79) to packed output values at 0x2000000 intervals.

**Layer 3: Template encoder stubs (0xD27000--0xEB2AE0, ~1,086 functions)**
The lowest-level stubs that directly write the encoding buffer via `sub_7B9B80`. These are the functions described by the encoder template above.

## Variant/Sub-opcode Distribution

The variant field (bits[17:24], 8 bits) has a distribution that peaks at variant 0x05 with 128 functions, suggesting this is the default or most common variant (possibly `.F32` type or the unmodified form):

| Variant | Count | Variant | Count |
|---------|-------|---------|-------|
| 0x00 | 21 | 0x08 | 13 |
| 0x01 | 25 | 0x09 | 14 |
| 0x02 | 62 | 0x0A | 10 |
| 0x03 | 24 | 0x0B | 19 |
| 0x04 | 20 | 0x0C | 14 |
| **0x05** | **128** | 0x0D | 9 |
| 0x06 | 30 | 0x0E | 11 |
| 0x07 | 10 | 0x0F--0x2F | decreasing |

Maximum observed variant value is 0x2F (47), giving up to 48 sub-operations per major opcode.

## Function Map

| Address | Size | Callers | Identity |
|---------|------|---------|----------|
| `sub_7B9B80` | 216 B | 18,347 | **bitfield_insert** -- core packer into 1280-bit buffer |
| `sub_7B9D30` | 38 B | 2,408 | **clear_cbuf_slots** -- memset(a1+468, 0xFF, 64) |
| `sub_7B9D60` | 408 B | 2,408 | **encode_reuse_predicate** -- reuse flags + guard predicate |
| `sub_7BC030` | 814 B | 6,147 | **encode_register** -- GPR operand encoder |
| `sub_7BC360` | ~500 B | 126 | **encode_uniform_register** -- UR operand encoder |
| `sub_7BC5C0` | 416 B | 1,449 | **encode_predicate** -- predicate operand encoder |
| `sub_7BCF00` | 856 B | 1,657 | **encode_immediate** -- immediate/cbuf operand encoder |
| `sub_7BD260` | ~300 B | 96 | **decode_finalize** -- extract control bits |
| `sub_7BD3C0` | ~500 B | 286 | **decode_register** -- GPR operand decoder |
| `sub_7BD650` | ~400 B | 115 | **decode_register_alt** -- destination register decoder |
| `sub_7BE090` | ~400 B | 50 | **decode_predicate** -- predicate operand decoder |
| `sub_10B6180` | 21 B | 8,091 | **encode_bool_field** -- 1-bit opcode-to-control mapping |
| `sub_10B6160` | 21 B | 2,205 | **encode_bool_field_B** -- 1-bit flag variant |
| `sub_10B6140` | 21 B | 1,645 | **encode_bool_field_C** -- 1-bit flag variant |
| `sub_10AFF80` | 11 KB | 3 | **instruction_constructor** -- 32-param object builder |
| `sub_10ADF90` | 2.2 KB | 357 | **instruction_unlink** -- linked-list remove + recycle |
| `sub_10B0BE0` | 6.5 KB | -- | **hash_table_insert_64** -- FNV-1a, 8-byte key, 4x resize |
| `sub_10B1C30` | 3.9 KB | -- | **hash_table_insert_32** -- FNV-1a, 4-byte key |
| `sub_10C0B20` | 180 KB | 3,109 | **setField** -- field value writer dispatch |
| `sub_10D5E60` | 197 KB | 961 | **getFieldOffset** -- field bit-position lookup dispatch |
| `sub_10E32E0` | 187 KB | 72 | **hasField** -- field existence query dispatch |
| `sub_10CCD80` | 142 KB | 4 | **setFieldDefault** -- default value writer dispatch |
| `sub_10CAD70` | 68 KB | 74 | **getOperandFieldOffset** -- per-operand field offset dispatch |
| `sub_10C7690` | 65 KB | 288 | **setOperandField** -- per-operand field writer dispatch |
| `sub_AF7DF0` | -- | 7,355 | **encoded_to_ir_register** -- hardware reg to IR translation |
| `sub_AF7200` | -- | 552 | **encoded_to_ir_predicate** -- hardware pred to IR translation |
| `sub_EB3040` | 1.9 KB | -- | **decode_dispatcher** -- binary search on instruction type |
| `sub_112CDA0` | 8.9 KB | -- | **register_pair_encoder** -- 40-pair mapping via if-chain |

## Cross-References

- [Mercury Encoder](mercury.md) -- the assembler backend that invokes the encoding phase
- [Capsule Mercury & Finalization](capmerc.md) -- post-encoding finalization
- [SASS Opcode Catalog](../reference/sass-opcodes.md) -- full mnemonic table
- [Instruction Selection](isel.md) -- the preceding pipeline phase
- [Blackwell (SM 100-121)](../targets/blackwell.md) -- SM100 architecture details
- [IR Instructions & Opcodes](../ir/instructions.md) -- the Ori IR instruction format consumed by the encoder
