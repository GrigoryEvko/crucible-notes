# SASS Instruction Encoding

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

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

Every encoder operates on an instruction encoding context object passed as `a1`. The primary encoding target is a 1280-bit (160-byte, 20 QWORD) buffer at offset `a1+544` (hex `0x220`). The bitfield packer `sub_7B9B80` writes individual fields into this buffer by scanning all 20 QWORDs in a single pass, OR-ing the relevant slice of the value into each overlapping QWORD. See [Bitfield Packer Detail](#bitfield-packer-detail----sub_7b9b80) for the full reconstructed algorithm.

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

The core encoding primitive. 216 bytes compiled, 18,347 callers total. Inserts an arbitrary-width bitfield into the 1280-bit buffer at `a1+0x220` (decimal 544). Reconstructed from decompiled `sub_7B9B80` and verified against disassembly:

```c
// sub_7B9B80(a1, bit_offset, bit_width, value)
// Canonical algorithm -- single source of truth.
//
// Scans all 20 QWORDs (0..1280 in steps of 64).  For each QWORD that
// overlaps [bit_offset, bit_offset+bit_width), extracts the relevant
// slice of `value` and OR-s it in.  The `neg_base` trick lets the
// compiler compute the right-shift amount with a single ADD + CMOVS.
uint32_t bitfield_insert(int64_t a1, uint32_t bit_offset, int bit_width, uint64_t value) {
    uint32_t end      = bit_offset + bit_width;   // asm: add edx, esi
    uint32_t neg_base = -64 - bit_offset;          // asm: mov r9d,0xFFFFFFC0; sub r9d,esi
    uint32_t pos      = 0;                         // asm: xor eax, eax
    do {
        uint32_t chunk_end = pos + 64;                         // lea r8d,[rax+0x40]
        if (bit_offset > pos + 63 || end <= pos) goto next;    // ja / jbe to skip

        uint32_t start = (bit_offset >= pos) ? bit_offset : pos;  // cmovnb eax, esi
        uint32_t stop  = (end <= chunk_end)  ? end : chunk_end;   // cmovbe ebp, edx
        int width       = stop - start;                            // sub ebp, eax
        int shift_right = neg_base + chunk_end;    // = pos - bit_offset
        if (shift_right < 0) shift_right = 0;      // cmovs ecx, r10d (r10d=0)
        int bit_in_qw  = start & 0x3F;             // and r12d, 0x3F
        int64_t *qword = (int64_t *)(a1 + 8 * (start >> 6) + 0x220);
        uint64_t slice  = value >> shift_right;     // shr r13, cl

        if (width == 64)
            *qword |= slice << bit_in_qw;                              // full-QWORD path
        else
            *qword |= (slice & ~(-1ULL << width)) << bit_in_qw;       // masked path
    next:
        pos = chunk_end;
    } while (pos != 1280);                          // cmp r8d, 0x500
    return pos;
}
```

Key properties:
- Handles cross-QWORD-boundary fields: a 9-bit opcode starting at bit 59 writes 5 bits to QWORD 0 and 4 bits to QWORD 1.
- `neg_base + chunk_end` simplifies to `pos - bit_offset` -- the number of value bits already consumed by earlier chunks. Clamped to 0 for the first overlapping chunk (where `pos <= bit_offset`).
- Loop terminates at bit position 1280 (20 QWORDs), hard ceiling.
- For typical field widths (1--9 bits), only 1--2 iterations touch the OR path; the rest hit the skip.
- Called 8--12 times per encoder function (average ~10).
- The 256-bit format encoders call it with wider fields (up to 32 bits for data values).
- Buffer offset 0x220 confirmed in disassembly: `mov r15, [r14+220h]` / `mov [r14+220h], rax`.

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

### Routing structure

All four instruction-level dispatchers (setField, getFieldOffset, hasField, setFieldDefault) share identical structure: a primary `switch(*(WORD*)(a1+12))` on the opcode category (0x0--0x171, 370 slots), where each live case contains a sub-switch on field ID `a2`. Of these 370 slots, 248 are live and 122 are dead (returning `0xFFFFFFFF`, `false`, or silently returning).

The two operand-level dispatchers (getOperandFieldOffset, setOperandField) use the same primary switch but extend to category 0x174 (373 slots, 278 with handlers). They add an `a2` (operand index) parameter and access per-operand records at `*(QWORD*)(a1+32) + 32*operand_index + 24`, sub-switching on field ID `a3` over the range 1--30.

### setField shared write paths

`sub_10C0B20` (setField, 3,109 call sites) delegates individual field writes to `sub_AF80xx` writer stubs but routes 347 cases through four shared tail labels that perform boundary-crossing bit-manipulation directly on the 192-bit word at `a1+48`:

| Label | Goto count | Field width | JUMPOUT target | Boundary logic |
|---|---|---|---|---|
| LABEL_3941 | 36 | 1-bit | (inline) | Single-QWORD OR with mask `1 << (bit & 0x3F)` |
| LABEL_3923 | 36 | 4-bit | `0xAF44A0` | Spans at `(bit+3)>>6`; partial writes to adjacent QWORDs |
| LABEL_3929 | 110 | 3-bit | `0xAF44A0` | Spans at `(bit+2)>>6`; same combiner as 4-bit |
| LABEL_3935 | 165 | 2-bit | `0xAF4550` | Spans at `(bit+1)>>6`; partial write to next word |

Each label receives three pre-loaded values: `v_offset` (value minus base_offset), `v_ptr` (`a1+48`), and `v_bit` (bit position). The JUMPOUT targets are hit when source and destination QWORD indices match (no boundary crossing), which is the fast path handled as a single-word mask-OR.

### getFieldOffset and hasField

`sub_10D5E60` (getFieldOffset, 961 callers) returns `extractor(a1+48, bit_position) + base_offset` for each field. Observed base_offset constants: +92, +125, +131, +788, +790, +1278, +1488, +1796, +1942, +2353, +2416, +2476. Returns `0xFFFFFFFF` when the queried field does not exist.

`sub_10E32E0` (hasField, 72 callers) is structurally identical but returns `extractor(...) != 0` as a boolean. The dead-case list is exactly the same 122 categories in both functions.

### setFieldDefault

`sub_10CCD80` (setFieldDefault, 4 callers) mirrors setField but replaces the caller-supplied `a3` with hardcoded defaults. Example: category 0 / field 242 calls `sub_AF8010(a1, 1278)` where setField calls `sub_AF8010(a1, a3)`. The default values correspond to `base_offset` constants from getFieldOffset -- the default encoding is the base offset itself (the zero/neutral position).

### Category richness

Field count per category varies dramatically (median: 5, max: 47):

| Category | Fields | Likely instruction class |
|---|---|---|
| 0x12 | 47 | Complex ALU (IMAD variants) |
| 0x63 | 45 | Memory (LD/ST with addressing modes) |
| 0x23 | 43 | Conversion (I2F/F2I with rounding) |
| 0x5A | 39 | Texture (TEX with sampler modes) |
| 0x68 | 35 | Surface (SULD/SUST) |
| 0x59 | 34 | Texture (TLD4 variants) |

Categories 0x0--0xA are minimal (1--4 fields), handling pseudo-ops or simple control flow.

### Dead-case bitmask (122 categories)

The 122 dead categories are identical across getFieldOffset, hasField, and setFieldDefault. setField uses `default: return` instead of an explicit dead-case block. The dead cases cluster in two dense zones:

- **Mid-range 0x8C--0xBA:** 28 of 47 slots dead (60%) -- reserved or arch-specific categories not active in the analyzed binary.
- **High range 0x12E--0x16F:** 44 of 66 slots dead (67%) -- Blackwell/sm_100+ categories not yet populated.
- **Sparse isolates below 0x80:** only 7 dead (0x3, 0x11, 0x24, 0x26, 0x2D, 0x75, 0x78).

The operand-level dispatchers have no explicit dead-case block; unknown categories fall through to `default: return 0xFFFFFFFF`.

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

### Validate-and-Map Step

Every modifier-encoding function follows one of two structural variants. Both take `(ctx, ir_enum_value)` and return a packed encoding integer, or -1 (0xFFFFFFFF) on invalid input.

**Variant A -- inline boolean** (used by `sub_10B6180`, `sub_10B6160`, `sub_10B6140`, `sub_10B2D90`, `sub_10B44E0`; covers ~12,000 call sites). For 2-value modifiers where the lookup table would contain just `{0, 1}`:

```c
// Reconstructed from sub_10B6180 (base=52), sub_10B6160 (base=49),
// sub_10B6140 (base=46), sub_10B2D90 (base=317), sub_10B44E0 (base=1852).
int modifier_encode_bool(int64_t ctx, int ir_val) {
    if (ir_val == BASE)       return 0;
    if (ir_val == BASE + 1)   return 1;
    return -1;  // invalid -- caller treats as encoding error
}
// Note: the decompiler emits "2 * (ir_val == BASE+1) - 1" which equals
// 1 when true, -1 (0xFFFFFFFF unsigned) when false -- same semantics.
```

**Variant B -- table lookup** (used by `sub_10B4650`, `sub_10B47F0`, `sub_10B2F00`, `sub_10B2F20`, `sub_10B5580`, `sub_10B6220`; covers ~1,800 call sites). Indexes into one of 40 lookup arrays in the `modifier_value_tables` region (0x22FCD20--0x22FD580, 2144 bytes total):

```c
// Reconstructed from sub_10B4650 (base=1899, table=identity_5 @ 0x22FD480, N=5),
// sub_10B47F0 (base=1957, table=split_block @ 0x22FCFF0, N=5),
// sub_10B6220 (base=71, table=rounding_mode_023 @ 0x22FCD20, N=3).
int modifier_encode_table(int64_t ctx, int ir_val,
                          int BASE, const int32_t *TABLE, int N) {
    unsigned idx = (unsigned)(ir_val - BASE);
    if (idx >= N)
        return -1;               // out of range
    int encoded = TABLE[idx];
    if (encoded == -1)
        return -1;               // gap entry -- reserved/invalid enum slot
    return encoded;
}
```

The 40 tables divide into three categories: 11 identity maps (output equals index -- used for pass-through encodings like `identity_5`, `tristate`, `quaternary`), 4 byte-width tables (for wide enum spaces up to 256 entries, e.g. `identity_byte_256`), and 25 non-trivial remapping tables (e.g. `rounding_mode_023` maps indices 0,1,2 to encoding bits 0,2,3 -- skipping the unused encoding value 1). Tables with `has_invalid_gaps: true` (3 of 40) contain -1 sentinel entries at positions corresponding to IR enum values that were removed or reserved; the lookup returns -1 for those positions, which the caller propagates as an encoding error.

Modifier fields per instruction range from 0 (simple control instructions) to 18 (the most complex encoder, `sub_D89C90` for opcode class 0x5A). The average is approximately 6 modifier fields per encoder. Bit positions in `a1+544` concentrate in bits 48-63; bit positions in `a1+552` concentrate in bits 0-11.

## Physical Register Encoding

The SASS instruction encoder uses a two-stage pipeline to convert abstract virtual registers into hardware register fields in the final instruction word. The first stage (Ori encoding, described above in "Register Operand Encoder") packs register type and number into operand slots within the 1280-bit encoding buffer. The second stage (SASS emission) maps the compiler's abstract `(register_class, sub_index)` pair into an 8-bit hardware register number and writes it into the final 128-bit instruction word. This second stage is implemented by the register-class encoding tables at address range 0x1B4C000--0x1B76000 (Zone A of the emission backend).

### Class-to-Hardware Formula

`sub_1B6B250` (2965 bytes, 254 callers, 0 callees) is a fully unrolled lookup table that implements the mapping:

```
hardware_reg = register_class * 32 + sub_index
```

The function takes two integer arguments `(a1, a2)` where `a1` is the register class (0--5) and `a2` is the sub-register index within that class. It is compiled as a deeply nested if-chain covering all 156 valid `(class, index)` combinations. The decompiler output is 495 lines of cascading conditionals, but every return value satisfies the formula `a1 * 32 + a2` exactly:

```c
// sub_1B6B250 -- reconstructed from decompiled lookup table
__int64 register_class_to_hardware(int reg_class, int sub_index) {
    // Returns reg_class * 32 + sub_index for all valid inputs.
    // Valid classes: 0, 1, 2, 3, 4, 5
    // Valid sub-indices: 1..15, 17..27 (index 0 and 16 excluded)
    // Returns 0 for any unmatched input (fallthrough).
}
```

The guard wrapper `sub_1B73060` (19 bytes, 483 callers) short-circuits the no-register case:

```c
// sub_1B73060 -- guard wrapper
__int64 encode_register_guarded(__int64 ctx, int reg_class, int sub_index) {
    if (reg_class | sub_index)
        return register_class_to_hardware(reg_class, sub_index);
    else
        return 0;  // no register
}
```

### Per-Class Hardware Number Ranges

Each class occupies a 32-number stride in the hardware register namespace. Within each stride, indices 1--15 and 17--27 are populated (26 registers per class). Index 0 maps to the no-register sentinel via the guard wrapper. Index 16 is absent from the lookup table -- a gap in every class.

| Class | a1 | Hardware Range | Populated Indices | Gap | Likely Register File |
|-------|---:|---------------|-------------------|-----|---------------------|
| 0 | 0 | 0--27 | 1--15, 17--27 | 16 | R (GPR primary) |
| 1 | 1 | 32--59 | 1--15, 17--27 | 48 | R (GPR secondary) |
| 2 | 2 | 64--91 | 1--15, 17--27 | 80 | P (predicate) |
| 3 | 3 | 96--123 | 1--15, 17--27 | 112 | UR (uniform GPR) |
| 4 | 4 | 128--155 | 1--15, 17--27 | 144 | UR (uniform ext) |
| 5 | 5 | 160--187 | 1--15, 17--27 | 176 | P/UP (uniform pred) |

Hardware numbers 28--31 (and the corresponding padding in each class) are unused, providing alignment to 32-register boundaries. The maximum hardware register number produced by the table is 187 (class 5, index 27). The 8-bit encoding field can represent 0--255, so values 188--255 are reserved.

#### Why Index 16 Is Excluded

The index-16 gap is not speculation -- it is an explicit, deliberate skip in the decompiled lookup table. In `sub_1B6B250`, the if-chain tests `a2 == 15` (line 192, returning `class*32+15`) and then jumps directly to `a2 == 17` (line 204, returning `class*32+17`). No conditional for `a2 == 16` exists anywhere in the 495-line function body. The gap is identically present in the extended encoder `sub_1B6EA20` (7194 bytes), which adds modifier support but covers the same `(class, index)` domain. The same applies to all five additional encoding variants (`sub_1B6D590`, `sub_1B70640`, `sub_1B71AD0`, `sub_1B748F0`, `sub_1B76100`).

The architectural reason is a half-bank boundary in the register file hardware. Each 32-entry class stride divides into two 16-entry halves:

```
Lower half:  indices  0 -- 15   (hw numbers  N*32+0  through  N*32+15)
Upper half:  indices 16 -- 31   (hw numbers  N*32+16 through  N*32+31)
```

Index 0 is consumed by the no-register sentinel (the guard wrapper `sub_1B73060` returns 0 when `reg_class | sub_index == 0`). Index 16 = `0b10000` is the exact position where bit 4 of the hardware register number transitions from 0 to 1, which is the boundary between the two physical register file half-banks. In the split bitfield writer (`sub_1B72F60`), the low 5 bits of the hardware number go to instruction bits [109:105]. For index 16, those 5 bits are `10000` -- exactly the half-bank select bit with all intra-bank address bits zero. The hardware reserves this slot as the half-bank boundary marker.

The parallel with architectural zero registers reinforces this interpretation. In the NVIDIA register file, hardware register 0 is RZ (read-zero, write-discard) for GPRs and PT (always-true) for predicates. Slot 16 is the analogous reserved position at the top of each half-bank -- it is not exposed as a named architectural register but the hardware treats it as a bank-select boundary that cannot hold allocatable state.

Concrete evidence: the 6 excluded hardware numbers are 16, 48, 80, 112, 144, and 176. In binary these are `0b0_10000`, `0b01_10000`, `0b10_10000`, `0b11_10000`, `0b100_10000`, `0b101_10000` -- every one has bits [4:0] = `10000` and bits [7:5] identifying the class. The encoder never produces any of these values, and the guard wrapper ensures that `(0,0)` also returns 0 (the RZ sentinel) rather than entering the table. The combined effect: indices 0 and 16 in each class are reserved, indices 28--31 are unused padding, and the 26 allocatable slots per class are 1--15 and 17--27.

### Split Bitfield Writer

`sub_1B72F60` (32 bytes, 483 callers) writes the 8-bit hardware register number into the SASS instruction word. The encoding is split across two non-contiguous bitfields within a single DWORD:

```c
// sub_1B72F60 -- register field writer (decompiled verbatim)
__int64 write_register_field(__int64 a1, int encoded_reg) {
    __int64 buf = *(_QWORD *)(a1 + 112);   // instruction encoding buffer
    __int64 result = *(_DWORD *)(buf + 12)  // DWORD at byte offset 12
                   | ((_WORD)encoded_reg << 9) & 0x3E00u;     // low 5 bits -> [13:9]
    *(_DWORD *)(buf + 12) = result
                   | (encoded_reg << 21) & 0x1C000000;        // high 3 bits -> [28:26]
    return result;
}
```

Bit-level layout within the DWORD at `*(instruction_buffer + 12)`:

```
DWORD bits:  31 30 29 28 27 26 25 24 23 22 21 20 19 18 17 16 15 14 13 12 11 10  9  8  7 ..  0
                      [  h2:h0  ]                                      [ l4:l3:l2:l1:l0 ]
                      hw[7:5]                                          hw[4:0]
```

The DWORD at byte offset 12 covers bits [127:96] of the 128-bit instruction word. In full instruction coordinates:

| Field | DWORD Bits | Instruction Bits | Width | Content |
|-------|-----------|------------------|-------|---------|
| Low | [13:9] | [109:105] | 5 bits | `hardware_reg[4:0]` |
| High | [28:26] | [124:122] | 3 bits | `hardware_reg[7:5]` |

The 12-bit gap between instruction bits [121] and [110] is occupied by other instruction fields (modifiers, flags, secondary operand encodings). This split-field design is common in GPU ISAs where instruction bits are at a premium and different fields must be routed to different functional unit inputs.

`sub_1B72FE0` (32 bytes, 104 callers) is byte-identical to `sub_1B72F60` but occupies a different vtable slot, used by a secondary operand encoding path.

### Extended Register Encoder

`sub_1B6EA20` (7194 bytes, 25 callers) extends the base encoding with operand modifier support. It takes 5 parameters:

```c
// sub_1B6EA20 -- register encoding with modifiers
__int64 encode_register_with_modifiers(
    int reg_class,      // a1: register class (0-5)
    int sub_index,      // a2: sub-register index
    int negation,       // a3: .NEG modifier flag
    int abs_value,      // a4: |.ABS| modifier flag
    int type_modifier   // a5: type cast modifier
);
```

When all modifier flags are zero (`a3 | a4 | a5 == 0`), the function returns the same value as `sub_1B6B250` -- the base `class * 32 + index` result. When modifiers are present, the function continues into extended encoding logic that packs modifier bits alongside the register number. The guard wrapper `sub_1B748C0` (35 bytes, 104 callers) provides the same no-register short-circuit for the extended variant.

Additional encoding variants for different operand positions include `sub_1B6D590`, `sub_1B70640`, `sub_1B71AD0`, `sub_1B748F0`, and `sub_1B76100` (5264--6106 bytes each, 2--49 callers each). All share the same nested-if structural pattern and operate on the same class/index domain.

### Encoding Pipeline Summary

The complete register encoding pipeline from virtual register to instruction bits:

```
Virtual Register (vreg+64 = reg_type, vreg+68 = physical_reg)
  |
  v
[Ori Encoder -- sub_7BC030, 6147 callers]
  Reads: operand+20 (reg_type_raw), operand+4 (reg_num)
  Writes: 1-bit presence + 4-bit type + 10-bit number into 1280-bit buffer
  |
  v
[SASS Emission -- sub_1B6B250 via sub_1B73060, 483 callers]
  Input: (register_class, sub_index)
  Formula: hardware_reg = class * 32 + sub_index
  Output: 8-bit hardware register number (0-187)
  |
  v
[Bitfield Writer -- sub_1B72F60, 483 callers]
  Input: 8-bit hardware register number
  Output: split across instruction bits [109:105] and [124:122]
```

### Zone A Function Map

| Function | Size | Callers | Role | Confidence |
|----------|-----:|--------:|------|-----------|
| `sub_1B6B250` | 2,965 B | 254 | Core `class*32+index` lookup table | HIGH |
| `sub_1B6EA20` | 7,194 B | 25 | Extended encoding with modifier bits | HIGH |
| `sub_1B73060` | 19 B | 483 | Guard wrapper for `sub_1B6B250` | CERTAIN |
| `sub_1B748C0` | 35 B | 104 | Guard wrapper for `sub_1B70640` | CERTAIN |
| `sub_1B72F60` | 32 B | 483 | Split bitfield register writer | HIGH |
| `sub_1B72FE0` | 32 B | 104 | Identical writer (different vtable slot) | HIGH |
| `sub_1B73080` | 6,106 B | 88 | 3-operand register encoding (class, index, modifier) | HIGH |
| `sub_1B6D590` | 5,264 B | varies | Register encoding variant (operand position A) | HIGH |
| `sub_1B70640` | varies | varies | Register encoding variant (operand position B) | HIGH |
| `sub_1B71AD0` | varies | varies | Register encoding variant (operand position C) | HIGH |
| `sub_1B748F0` | varies | varies | Register encoding variant (operand position D) | HIGH |
| `sub_1B76100` | varies | varies | Register encoding variant (operand position E) | HIGH |

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

## Per-SM Instruction Format Descriptors

316 instruction format descriptor functions at 0x1732170--0x17A9B70 form the shared, architecture-neutral instruction pattern database. Unlike the per-SM encoder stubs (replicated per architecture at separate address ranges), these descriptors are a single set of functions that describe every SASS opcode variant's encoding geometry: bitfield layout, operand slot configuration, and modifier schema. They are invoked exclusively through virtual dispatch (zero static callers) from the ISel passes (`sub_A4BC60`, `sub_A4D3F0`) via the FNV-1a hash-based instruction matcher at `sub_1731440`.

### Descriptor Template

Every descriptor function initializes an Encoding Context object through a fixed 4-phase sequence:

```c
// Phase 1: Opcode header (5 calls for 64-bit, 6 for 128-bit)
sub_7B9B80(a1, 0,    4, FORMAT_CODE);   // bits[3:0]     format: 1=64b, 2=128b
sub_7B9B80(a1, 4,    3, 0);             // bits[6:4]     sched group slot
sub_7B9B80(a1, 0x84, 3, 0);             // bits[134:132] ext flag (128-bit ONLY)
sub_7B9B80(a1, 8,    9, MAJOR_OP);      // bits[16:8]    9-bit major opcode
sub_7B9B80(a1, 0x11, 8, MINOR_OP);      // bits[24:17]   8-bit minor opcode
sub_7B9B80(a1, 0x19, 7, FORMAT_ID);     // bits[31:25]   7-bit format ID

// Phase 2: Format layout descriptor (Tier 1) -- selects operand geometry
*(__m128i*)(a1 + 8) = xmmword_23FXXXX;  // 128-bit format template from rodata
// + bulk copy of 3 x 10 DWORD arrays into a1+24..a1+140

// Phase 3: Architecture modifier table (Tier 2) -- selects per-SM encoding
*(__m128i*)(a1 + 404) = xmmword_YYYYYYY;  // per-SM modifier constants
*(DWORD*)(a1 + 420) = VAL1;               // explicit modifier overrides
*(DWORD*)(a1 + 424) = VAL2;

// Phase 4: Operand count + standard encoding tail
*(DWORD*)(a1 + 144) = NUM_OPERANDS;       // 0--7
sub_7B9D30(a1);                            // clear constant buffer table
sub_7B9D60(a1, a2, 0);                     // encode reuse + guard predicate
// Then: opcode extraction, register encoding, modifier field packing
```

### Two-Tier xmmword Architecture

Each descriptor loads two classes of xmmword constants that together fully specify the instruction encoding:

**Tier 1 (at `a1+8`): Format Layout Descriptor.** Selects the instruction format -- operand slot sizes, types, and field layout. These are the 16 format groups documented in the "Instruction Format Group Catalog" section above. Addresses in the `0x23F1xxx`--`0x23F2xxx` rodata range.

**Tier 2 (at `a1+404`): Architecture Modifier Table.** Selects per-SM encoding variations for the same format layout. Two instructions with the same Tier 1 descriptor but targeting different architectures use different Tier 2 constants. Addresses span three rodata ranges:

| Rodata Range | Group | Functions | Paired With |
|---|---|---|---|
| `0x202A280`--`0x202A2B0` | A | ~40 | `202A290` or `202A2A0`+`202A2B0` at `a1+420` |
| `0x22F1B30`--`0x22F1B50` | B/C | ~8 | None (single 16B block) |
| `0x22F1BA0`--`0x22F1BB0` | D | ~3 | None |
| `0x22F1AA0`--`0x22F1AE0` | E | ~3 | (observed in SM100 encoder range) |
| `0x22F1C20`--`0x22F1C30` | F | ~2 | Paired at `a1+404`/`a1+420` |
| `0x23B2DE0` | G | 4 | None (rare/specialized) |

### SM Generation Mapping

The Tier 2 modifier groups correspond to GPU architecture generations. The mapping is inferred from operand table sizes (larger = newer), function counts per group (fewer = newer/specialized), and cross-reference with the per-SM encoder stubs at known address ranges:

| Modifier Address | Probable SM Range | ISA Family | Confidence |
|---|---|---|---|
| `0x202A280`--`0x202A2B0` | sm_50--sm_75 | Maxwell / Pascal / Volta / Turing | MEDIUM |
| `0x22F1B30`--`0x22F1B50` | sm_80--sm_86 | Ampere / Ada | MEDIUM |
| `0x22F1BA0`--`0x22F1BB0` | sm_89--sm_90a | Lovelace / Hopper | MEDIUM |
| `0x22F1AA0`--`0x22F1AE0` | sm_100+ | Blackwell datacenter | MEDIUM |
| `0x22F1C20`--`0x22F1C30` | sm_103 / sm_120 | Blackwell Ultra / consumer | LOW |
| `0x23B2DE0` | Cross-arch | Specialized / rare instructions | LOW |

The progression from `0x202A` to `0x22F1` to `0x23B2` in rodata address space mirrors the SM generation ordering. Group A (Maxwell--Turing) is the most populous, consistent with the longest-supported ISA family. Groups E and F have the fewest functions, consistent with the newest architectures that introduce fewer format changes.

### Cross-SM Dispatch Table Comparison

Five per-SM handler dispatch tables at `0x22E7AD0`--`0x23B99D0` (72,000 bytes each, 24 bytes per entry) map `(format_id << 8) | minor_opcode` dispatch keys to encoder stub handler addresses. The tables share a common core of 492 opcodes while differing in total population, handler multiplicity, and per-opcode encoder routing.

| Property | SM50--7x | SM75 | SM80--8x | SM86--89 | SM100 |
|---|---|---|---|---|---|
| Dispatch table VA | `0x22E7AD0` | `0x2348FB0` | `0x238C9B0` | `0x23A8090` | `0x236E160` |
| Total entries | 1,484 | 1,613 | 1,896 | 1,641 | 1,808 |
| Unique dispatch opcodes | 512 | 535 | 535 | 535 | 634 |
| Unique handler functions | 1,472 | 1,606 | 1,888 | 1,634 | 1,797 |
| Avg entry multiplicity | 2.90 | 3.01 | 3.54 | 3.07 | 2.85 |
| Max entry multiplicity | 37 | 41 | 55 | 42 | 40 |
| Handler VA range | `0xC69`--`0xEB2` | `0xC69`--`0x1C0B` | `0xC69`--`0x180B` | `0xC69`--`0x18F0` | `0xC69`--`0x1C07` |

**Opcode set evolution.** SM75, SM80--8x, and SM86--89 all share an identical 535-opcode set: the SM50 baseline plus 41 additions minus 18 removals. SM100 (Blackwell) diverges substantially with 100 new exclusive opcodes spanning 13 format IDs (heaviest in format_id 3 with 16 new opcodes, format_id 5 with 15, and format_id 7 with 13). The 17 SM50-only opcodes cluster in format IDs 17 (7 opcodes), 37 (7 opcodes), 31 (2), and 36 (1) -- all absent from every later generation.

**Handler routing divergence.** Among the 492 common opcodes, only 236 (48%) route to identical handler sets across all five tables. The remaining 256 (52%) have at least one SM generation providing extra encoder stubs, typically architecture-specific variants for the same logical instruction. SM80--8x is the most divergent with 175 opcodes carrying extra handlers (consistent with its highest entry count of 1,896 and max multiplicity of 55). All tables share a common handler base address at `0xC693D0`, with SM-specific encoder stubs extending into disjoint address ranges: SM50 at `0xC69`--`0xEB2`, SM75/80/86 adding stubs in `0x174`--`0x18F`, and SM100 adding a block at `0x144`--`0x150`.

**Format ID presence/absence across generations:**

| Format ID | SM50--7x | SM75+ | SM100 | Notes |
|---|---|---|---|---|
| 17 | 23 entries | 0 | 0 | Removed after Maxwell--Volta |
| 31 | 2 entries | 0 | 0 | Removed after Maxwell--Volta |
| 36 | 1 entry | 0 | 0 | Removed after Maxwell--Volta |
| 37 | 13 entries | 2--4 | 0 | Shrinking; gone in Blackwell |
| 27 | 0 | 0 | 1 entry | Blackwell-only |
| 32 | 0 | 0 | 1 entry | Blackwell-only |

The largest cross-generation entry count swings occur in format IDs 3 (284 to 379, +33%), 5 (322 to 449 peak at SM80, +39%), and 25 (199 to 295 peak at SM80, +48%), reflecting expanding ALU and load/store encoding variant coverage. SM80--8x consistently peaks across nearly all format IDs, suggesting Ampere carries the broadest per-opcode variant coverage (more encoding paths per logical instruction) despite sharing the same 535-opcode ISA as SM75 and SM86.

### Format Code Distribution

| Format Code | Instruction Width | Descriptor Count | sub_7B9B80 Header Calls | Notes |
|---|---|---|---|---|
| 1 | 64-bit | ~120 | 5 (no `0x84` call) | Simple moves, branches, barriers, NOP-like control |
| 2 | 128-bit | ~194 | 6 (includes `0x84`) | ALU, load/store, texture, tensor core |
| 8 | 256-bit | 2 | Extended | IMAD.WIDE with 16 constant-bank slots |

### Descriptor-Initialized Context Fields

The format descriptor writes these fields into the Encoding Context object. All offsets are decimal:

| Offset | Size | Initialized By | Content |
|---|---|---|---|
| `+8` | 16B | Phase 2 (Tier 1 xmmword) | Format layout descriptor |
| `+24`--`+60` | 40B | Phase 2 (bulk copy) | Operand slot sizes (10 DWORDs) |
| `+64`--`+100` | 40B | Phase 2 (bulk copy) | Operand slot types (10 DWORDs) |
| `+104`--`+140` | 40B | Phase 2 (bulk copy) | Operand slot flags (10 DWORDs) |
| `+144` | 4B | Phase 4 | Operand count (0--7) |
| `+404` | 16B | Phase 3 (Tier 2 xmmword) | Architecture modifier table |
| `+420` | 4B | Phase 3 (scalar) | Architecture modifier field 1 |
| `+424` | 4B | Phase 3 (scalar) | Architecture modifier field 2 |

### Pipeline Position

The format descriptors bridge ISel pattern matching and per-SM encoding:

```
ISel Pattern Matcher (sub_1731440, FNV-1a hash on *(a2+12))
  |
  v  (virtual dispatch via vtable)
Format Descriptor (one of 316 at 0x1732170--0x17A9B70)
  Writes: a1+0..a1+144   (format layout + operand geometry)
  Writes: a1+404..a1+424  (architecture modifier table)
  |
  v  (encoding context passed down)
Per-SM Encoder Stub (e.g. 0xD27xxx for SM100)
  Reads: format context from descriptor
  Writes: a1+544..a1+703  (1280-bit encoding buffer)
```

### Representative Examples

**`sub_1732170` -- 64-bit float conversion (single-dest):**

| Field | Value | Meaning |
|---|---|---|
| Format code | 1 | 64-bit instruction |
| Major opcode | 0x0C | Float conversion family |
| Minor opcode | 0x0D | Variant D |
| Format ID | 5 | Short-form general (23F1F08) |
| Tier 1 | `xmmword_23F1F08` | Short-form general, 27 opcode classes |
| Tier 2 | `xmmword_22F1B30` | Group B (Ampere/Ada) |
| Operand count | 3 | Register operands at 0x50, 0x60, 0x70 |
| Modifier fields | 12 | Spanning `a1+544` and `a1+552` |

**`sub_1740200` -- 128-bit IMAD.WIDE (dual-dest):**

| Field | Value | Meaning |
|---|---|---|
| Format code | 2 | 128-bit instruction |
| Major opcode | 0x23 | IMAD.WIDE family |
| Minor opcode | 0x12 | Variant with modifier 0x13 |
| Format ID | 0x13 | Tensor/extended ALU (23F2678) |
| Tier 1 | `xmmword_23F2678` | Extended ALU, 7 opcode classes |
| Tier 2 | `xmmword_202A280` | Group A (Maxwell--Turing) |
| Dual-dest | Yes | `0x84` field present, set to 0 |

**`sub_1732E90` -- 128-bit extended complex:**

| Field | Value | Meaning |
|---|---|---|
| Format code | 2 | 128-bit instruction |
| Major opcode | 0x0C | Float conversion family |
| Minor opcode | 0x0C | Same as major (self-referencing variant) |
| Format ID | 0x19 | Extended complex (23F29A8) |
| Tier 1 | `xmmword_23F29A8` | Extended complex, 8 opcode classes |
| Tier 2 | `xmmword_22F1B30` | Group B (Ampere/Ada) |

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

### Operand Bit-Offset Selection Algorithm

The encoder selects operand bit offsets based on the instruction format code
(1=64-bit, 2=128-bit), not by computing them from the format descriptor's
`slot_sizes` array at runtime. Each of the ~1,086 encoder stubs contains
hardcoded offset constants passed as the fourth argument to `sub_7BC030`
(register encoder), `sub_7BC5C0` (immediate encoder), or `sub_7BCF00`
(predicate encoder).

The bit layout that produces these offsets:

```
64-bit instruction (format code 1):

  bits[0:31]   fixed header (format code, sched slot, major/minor/format ID)
  bits[32:63]  modifier zone (flags, immediates -- accessed via a1+544 shifts)
  -------------------------------------------------------
  bit 0x40 (64)  = operand slot 0        --|
  bit 0x50 (80)  = operand slot 1          |  4 slots, 16 bits each
  bit 0x60 (96)  = operand slot 2          |  stride = 0x10
  bit 0x70 (112) = operand slot 3        --|


128-bit instruction (format code 2):

  bits[0:31]   fixed header (same layout as 64-bit)
  bits[32:95]  modifier/immediate zone (64 bits)
  -------------------------------------------------------
  bit 0x60 (96)  = operand slot 0        --|  2 slots, stride = 0x10
  bit 0x70 (112) = operand slot 1        --|
  bits[128:135]  extended modifier zone   -- 8-bit gap (includes bit 0x84)
  bit 0x88 (136) = operand slot 2        --|
  bit 0x98 (152) = operand slot 3          |  3 slots, stride = 0x10
  bit 0xA8 (168) = operand slot 4        --|
```

Each operand slot is 16 bits wide: 1-bit presence flag, 4-bit register-file
type (via the 12-entry regfile map in `sub_7BC030`), and 10-bit register
number. The 128-bit gap between slots 1 and 2 (0x70 to 0x88 = 24 bits)
reserves bits[128:135] for the extended opcode flag written by
`sub_7B9B80(a1, 0x84, 3, 0)` in every 128-bit encoder header.

Binary evidence -- representative encoder call sequences:

```c
// sub_C7B170: 64-bit, 4-register (xmmword_23F1D70, slot_sizes[0]=8)
sub_7BC030(a1, a2, 0, 0x40u);   // operand 0 -> bit 64
sub_7BC030(a1, a2, 1, 0x50u);   // operand 1 -> bit 80
sub_7BC030(a1, a2, 2, 0x60u);   // operand 2 -> bit 96
sub_7BC030(a1, a2, 3, 0x70u);   // operand 3 -> bit 112

// sub_C86C00: 128-bit, 6-operand mixed (xmmword_23F1DF8, slot_sizes=[10,17])
sub_7BC5C0(a1, a2, 0, 0x50u);   // immediate  -> bit 80 (modifier zone)
sub_7BC030(a1, a2, 1, 0x60u);   // register 1 -> bit 96
sub_7BC030(a1, a2, 2, 0x70u);   // register 2 -> bit 112
sub_7BC030(a1, a2, 3, 0x88u);   // register 3 -> bit 136 (after gap)
sub_7BCF00(a1, a2, 4, 0x98u);   // predicate  -> bit 152
sub_7BC030(a1, a2, 5, 0xA8u);   // register 5 -> bit 168
```

The `slot_sizes` array in the format descriptor partitions the *modifier zone*
between operand slots, not the operand positions themselves. The expression
`8 * (slot_sizes[i] + base_index) + 8` seen in some encoders (e.g.,
`sub_7B9B80(a1, 8*(*a1_28 + *a1_12)+8, 8, 0)` in `sub_C86C00`) computes
modifier-field bit positions relative to a slot boundary, where `base_index`
is `ctx.xmmword[1]` at `a1+12` and `slot_sizes[i]` is at `a1+24+4*i`.

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

```
// Generalized Layer 2 encoder pipeline (sub_10F91D0 pattern):
//
// State:  a1+32 = lookup_tables_base,  a1+40 = ptr to 128-bit output word
//         a1+12 = default_reg (sentinel replacement)
//         a2+32 = operand_array_base,  a2+40 = operand_slot_index
//
// Step 1 -- OR the format descriptor mask into the output word:
output_128[0..15] |= format_descriptor_xmmword   // e.g. xmmword_231C170

// Step 2 -- For each field (register, modifier, flag), repeat:
//   (a) extract:   raw = sub_10BFxxx(operand_addr)     // field-specific extractor
//   (b) translate: enc = sub_10Bxxx(tables_base, raw)   // lookup table remapping
//   (c) pack:      output_word[N] |= (enc << shift) & mask
//
// Concrete fields from sub_10F91D0 (4-operand instruction):
//   word[0] |= (reg_class_enc    << 15)  & 0x8000              // bit  15
//   word[0] |= (operand[0].type  << 12)  & 0x7000              // bits 12-14
//   word[0] |= (reg_src0         << 16)  & 0xFF0000            // bits 16-23
//   word[0] |= (reg_src1         << 24)                        // bits 24-31
//   word[0] |= (reg_src2         << 32)  & 0xFF_00000000       // bits 32-39
//   word[0] |= (abs_src2_flag    << 62)  & 0x4000000000000000  // bit  62
//   word[0] |= (neg_src2_flag    << 63)                        // bit  63
//   word[1] |= (reg_dst                )                       // bits 64-71
//   word[1] |= (abs_src0         <<  8)  & 0x100               // bit  72
//   word[1] |= (flag_src0        <<  9)  & 0x200               // bit  73
//   word[1] |= (flag_dst         << 10)  & 0x400               // bit  74
//   word[1] |= (abs_dst          << 11)  & 0x800               // bit  75
//   word[1] |= (modifier_c       << 13)  & 0x2000              // bit  77
//   word[1] |= (modifier_b       << 14)                        // bits 78-79
//   word[1] |= (modifier_a       << 18)  & 0x40000             // bit  82
//   word[1] |= (neg_src1         << 19)  & 0x80000             // bit  83
//   word[1] |= (neg_src2_hi      << 20)  & 0x100000            // bit  84

// Step 3 -- Sentinel substitution (applied per register field):
//   if (reg_id == 1023) reg_id = default_reg;    // 0x3FF = "don't care"
```

Register pair encoder (`sub_112CDA0`): maps even/odd register pairs to a 6-bit index packed at bits [25:30] of `word[0]`. The 40 valid combinations and their encoding:

```
// pair_index = reg_lo / 2,  where reg_lo in {0,2,4,...,78} and reg_hi = reg_lo+1
// Validation: both regs present AND reg_hi == reg_lo + 1 (consecutive pair)
// Encoding:   word[0] |= (pair_index << 25)
//
// pair_index  regs       packed value        pair_index  regs       packed value
//     0       R0 /R1     0x00000000              20      R40/R41    0x28000000
//     1       R2 /R3     0x02000000              21      R42/R43    0x2A000000
//     2       R4 /R5     0x04000000              22      R44/R45    0x2C000000
//     3       R6 /R7     0x06000000              23      R46/R47    0x2E000000
//     ...     ...        (+0x2000000)             ...     ...        (+0x2000000)
//    15       R30/R31    0x1E000000              35      R70/R71    0x46000000
//    16       R32/R33    0x20000000              36      R72/R73    0x48000000
//    19       R38/R39    0x26000000              39      R78/R79    0x4E000000
// Fallback: if no pair matches, pair_index = 0 (R0/R1 encoding)
```

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

## SASS Emission Backend

The final stage of the encoding pipeline operates at the instruction-word level: 11 per-instruction-form bitfield packers at addresses 0x1B79940--0x1B9C220 take a pre-decoded instruction descriptor and pack all fields into a 128-bit SASS instruction word. These functions sit at Level 2 of a 4-level emission hierarchy:

```
Level 0: SM-target dispatch    (0xC4DF70, 0xC53330, 0xC54090, 0xC59610, 0xC5ABE0, 0xC5B5C0)
Level 1: Emission orchestrators (Zone C: 0x1BA0000-0x1BE5000, ~150 functions)
Level 2: Per-form bit packers   (Zone B: 0x1B79940-0x1B9C220, 11 functions, THIS SECTION)
Level 3: Register class encoders (Zone A: 0x1B4C000-0x1B76000, ~40 functions)
```

Each function has exactly 1 caller and 0 callees (pure bitfield packing, no external calls). Sizes range from 6836 to 6980 bytes of compiled code. All 11 share an identical combinator body (verified: same 453 `LABEL_xxx` targets, same 75 unique OR-mask constants, same max comparison value of 27). They differ only in two things: the opcode base constant, and the prologue field-packing sequence.

### Input / Output Interface

```c
int *__fastcall emit_instruction_form_X(int *a1) {
    // a1 = pre-decoded instruction descriptor (array of 32-bit ints)
    // Returns: pointer to output buffer (also accessible at *((_QWORD*)a1 + 14))
    int *result = *((_QWORD *)a1 + 14);  // output = 128-bit instruction word
    // result[0] = instruction bits [31:0]   (opcode base, guard pred, sched group)
    // result[1] = instruction bits [63:32]  (register operand fields, modifiers)
    // result[2] = instruction bits [95:64]  (immediate/offset, auxiliary fields)
    // result[3] = instruction bits [127:96] (predicate control, combinator encoding)
}
```

The input struct `a1` is a flat array of pre-extracted instruction fields. Fields `a1[0]` through `a1[3]` carry common header values; `a1[4]` through `a1[15]` carry instruction-specific operand data (which indices are used depends on the instruction form).

### Phase 1: Prologue -- Opcode Base and Field Packing

Every function begins with the same template, parameterized by different constants:

```c
// 1. Load output buffer pointer
result = *((_QWORD *)a1 + 14);

// 2. OR opcode base into result[0] -- unique 12-bit constant per function
*result |= OPCODE_BASE;  // e.g., 0xA1E, 0x81B, 0x803

// 3. Pack guard predicate: bits [14:12] of result[0]
*result |= ((unsigned short)a1[1] << 12) & 0x7000;

// 4. Pack scheduling group: bits [16:15] of result[0]
*result |= (unsigned short)((unsigned short)a1[2] << 15);

// 5. Pack predicate encoding: bits [25:20] of result[3]
result[3] |= (a1[3] << 20) & 0x3F00000;

// 6. Pack instruction-specific operand fields (VARIES PER FUNCTION)
//    Each function packs a different set of a1[6..15] fields into
//    result[0], result[1], result[2] using different shifts and masks.

// 7. Set base combinator mask: bits [17:14] of result[3] = 0x3F
result[3] |= 0xFC000;
```

The prologue is the sole source of variation between functions. The field-packing differs in which `a1[]` indices are used, which shift amounts are applied, and which `result[]` DWORDs are targeted.

### The 11 Functions and Their Opcode Bases

| Function | Size | Opcode Base | Family | Caller Chain |
|----------|-----:|-------------|--------|-------------|
| `sub_1B79940` | 6,900 B | `0xA1B` | 0xAxx | `sub_1BA5340` via `sub_C4DF70` |
| `sub_1B7B440` | 6,868 B | `0x81B` | 0x8xx | `sub_1BA5340` via `sub_C4DF70` |
| `sub_1B87740` | 6,852 B | `0x238` | 0x2xx | `sub_1BA8D80` via `sub_C53330` |
| `sub_1B89350` | 6,836 B | `0x213` | 0x2xx | `sub_1BA8E80` via `sub_C54090` |
| `sub_1B8FFE0` | 6,852 B | `0x202` | 0x2xx | `sub_1BA8D80` via `sub_C53330` |
| `sub_1B92590` | 6,868 B | `0x803` | 0x8xx | `sub_1BACB10` (direct) |
| `sub_1B94390` | 6,964 B | `0x21D` | 0x2xx | `sub_1BACDC0` via `sub_C59610` |
| `sub_1B95ED0` | 6,980 B | `0xA1E` | 0xAxx | `sub_1BACDC0` via `sub_C59610` |
| `sub_1B985B0` | 6,852 B | `0x804` | 0x8xx | `sub_1BAD6D0` (direct) |
| `sub_1B9A430` | 6,884 B | `0x807` | 0x8xx | `sub_1BB1110` via `sub_C5ABE0` |
| `sub_1B9C220` | 6,884 B | `0x81A` | 0x8xx | `sub_1BAD920` via `sub_C5B5C0` |

The opcode bases cluster into three families by high nibble:
- **0x2xx** (4 functions): bases 0x202, 0x213, 0x21D, 0x238
- **0x8xx** (5 functions): bases 0x803, 0x804, 0x807, 0x81A, 0x81B
- **0xAxx** (2 functions): bases 0xA1B, 0xA1E

### Phase 2: Combinator -- 3-Axis Predicate Encoding into result[3]

After the prologue, all 11 functions execute an identical ~1900-line decision tree. This combinator reads three integer values from the input struct and produces a single 32-bit mask that is ORed into `result[3]`.

The three axes are:
- **axis0** = `a1[0]`: instruction class selector, values 0..5 (6 values)
- **axis1** = `a1[4]`: slot/form index, values 1..27 (26 populated, gap at 16)
- **axis2** = `a1[N]`: sub-mode flag, values 0 or 1 (N varies per function -- a1[8], a1[9], a1[10], a1[11], or a1[15])

The combinator exits immediately if all three axes are zero (`!(axis0 | axis1 | axis2)`). Otherwise it walks a nested decision tree that tests axis0 values (0 through 5), axis1 values (1 through 27), and axis2 values (0 or 1), and ORs the appropriate mask into `result[3]`:

```c
// Reconstructed combinator logic (pseudocode):
if (axis0 == 0 && axis1 == 0 && axis2 == 0) return;

// For axis0 values 1-5 combined with axis1 values 1-15:
// result[3] |= prefix_for_axis0 | 0xFC000 | (axis1 << 9)
//
// For axis1 values 17-27 combined with axis2:
// result[3] |= base_mask_for_axis1  (if axis2 == 0)
// result[3] |= extended_mask_for_axis1  (if axis2 == 1)
```

### Combinator Mask Encoding

The 75 unique masks in the FC/FD series decompose as:

```
result[3] bit layout for combinator-generated fields:
  bits [17:14] = 0x3F  (always set by prologue base 0xFC000)
  bits [13:9]  = slot_index  (5-bit, derived from axis1, values 1-27)
  bits [28:26] = axis0 prefix encoding (3-bit, for axis0 values 1-5)
```

The 5 prefix values correspond to axis0 encodings 1-5:

| axis0 Value | Prefix OR'd | Prefix Bits [28:26] |
|-------------|-------------|---------------------|
| 0 | `0x00000` | 000 (no prefix) |
| 1 | `0x40xxxxx` | 001 |
| 2 | `0x80xxxxx` | 010 |
| 3 | `0xC0xxxxx` | 011 |
| 4 | `0x100xxxxx` | 100 |
| 5 | `0x140xxxxx` | 101 |

Combined with 15 slot values (axis1 = 1..15), this produces 5 x 15 = 75 masks in the `0xFC200`--`0x140FCE00` range.

For axis1 values 17--27, the masks shift into the `0xFE200`--`0xFF600` range. These slots use only the "no prefix" and "prefix 0x100" variants (axis0 values 0 and 4), and the axis2 flag selects between the two. This gives an additional 12 unique masks for the high-slot range (11 base + 11 extended, minus shared ones, equals 12 unique).

### Why the Combinator Exists

The combinator encodes an architecture-independent mapping from a 3-dimensional instruction property coordinate to a hardware-specific bitfield pattern in the predicate/control section of the 128-bit instruction word. This section (bits [127:96]) controls:
- Guard predicate assignment (bits [25:20] from prologue)
- Scheduling mode (bits [17:14] base + combinator overlay)
- Instruction form variant (bits [13:9] from combinator)
- Predicate class / condition code routing (bits [28:26] from combinator)

The identical combinator across all 11 functions confirms that this is not an opcode-specific encoding but rather a cross-cutting encoding for predicate/scheduling state that applies uniformly to all instruction forms.

### Equivalent Lookup Table

The entire 2000-line decision tree can be replaced by a flat table of 6 x 28 x 3 = 504 entries:

```c
// Equivalent reconstruction:
static const uint32_t combinator_table[6][28][3] = { ... };
// Access: result[3] |= combinator_table[axis0][axis1][axis2];
// Table size: 504 * 4 = 2,016 bytes (vs ~6,800 bytes of code per function)
```

The compiler chose a decision tree over a table lookup, likely because the C++ source used nested switch/case statements (or if/else chains with early return), and the optimizer did not convert this to a table at -O2.

### Zone B Function Map (Emission Cluster)

| Address | Size | Opcode Base | Caller | Confidence |
|---------|-----:|-------------|--------|-----------|
| `sub_1B79940` | 6,900 B | `0xA1B` | `sub_1BA5340` | HIGH |
| `sub_1B7B440` | 6,868 B | `0x81B` | `sub_1BA5340` | HIGH |
| `sub_1B87740` | 6,852 B | `0x238` | `sub_1BA8D80` | HIGH |
| `sub_1B89350` | 6,836 B | `0x213` | `sub_1BA8E80` | HIGH |
| `sub_1B8FFE0` | 6,852 B | `0x202` | `sub_1BA8D80` | HIGH |
| `sub_1B92590` | 6,868 B | `0x803` | `sub_1BACB10` | HIGH |
| `sub_1B94390` | 6,964 B | `0x21D` | `sub_1BACDC0` | HIGH |
| `sub_1B95ED0` | 6,980 B | `0xA1E` | `sub_1BACDC0` | HIGH |
| `sub_1B985B0` | 6,852 B | `0x804` | `sub_1BAD6D0` | HIGH |
| `sub_1B9A430` | 6,884 B | `0x807` | `sub_1BB1110` | HIGH |
| `sub_1B9C220` | 6,884 B | `0x81A` | `sub_1BAD920` | HIGH |

## SM89/90 Codec Layer

SM89 (Ada Lovelace) and SM90 (Hopper) share a pre-encoding instruction reordering layer absent from SM100 (Blackwell). This layer sits above the three-layer encoding pipeline: it manipulates Mercury IR instruction lists to optimize instruction interleaving before the encoding stubs pack bitfields. The entire cluster spans addresses 0x1226E80--0x1233D70, roughly 261 KB of compiled code across 18 functions.

### Call Chain

```
sub_C60910 / sub_C5FEF0   SM-target dispatch (Level 0, 0xC5xxxx range)
  |
  v
sub_1233D70 (6 KB)         Orchestrator: guards on knob 487 and O-level > 1,
  |                        sets up cost-function parameters, calls A then B
  |
  +-> sub_122AD60 (112 KB)  Pass A: classify instructions + reorder within blocks
  +-> sub_122F650 (105 KB)  Pass B: scheduling-aware emission ordering across blocks
  +-> sub_A112C0            Post-pass finalization
```

The orchestrator `sub_1233D70` is called only when optimization level exceeds 2 (`sub_7DDB50(ctx) > 1`). It reads floating-point cost weights from the target descriptor via knob offsets +7200, +7560, +7128, +7272 and passes them through to both passes. Default base weights are `1.8, -0.8, 3.2, -2.2`.

### Pass A: Instruction Classification and Reordering (`sub_122AD60`)

4,118 decompiled lines. Traverses every instruction in each basic block and sorts them into 4 linked-list queues by instruction category:

| Category | Return Code | Instruction Type | Queue Role |
|----------|:-----------:|:----------------:|------------|
| Branch / control-flow | 0 | type 9 (BRA, EXIT, RET, ...) | Held at block boundaries |
| Load | 1 | type 12 (LDG, LDS, ...) | Scheduled early for latency hiding |
| Store | 2 | type 5 (STG, STS, ...) | Deferred to maximize distance from load |
| General ALU | 4 | type 4 (IADD, FFMA, ...) | Interleaved between memory ops |
| Uncategorized | 3 | other / missing info | Treated as general |

The classifier is `sub_1228670` (30 lines), which reads the instruction scheduling class via `sub_7E2FE0` and returns 0--4. A companion predicate `sub_1228EF0` (38 lines) returns 0 for types 9, 5, and 12 (the "special" categories), 1 for everything else.

After classification, Pass A performs register-class-aware instruction motion: it uses `sub_91BF30` (register class builder), `sub_91E390` (class query), and `sub_91E610` (class intersection) to verify that moving an instruction does not violate register-class constraints. Instructions that pass the check have their operand flags updated at `+48` (bit 0x40 = "moved" marker) and `+96` (copy-chain tracking).

The reordering step `sub_122AA30` (186 lines) performs the final within-block interleaving. `sub_1227D90` (522 lines) handles the actual linked-list surgery: unlink an instruction from its current position and reinsert it at a new location.

### Pass B: Scheduling-Aware Emission Ordering (`sub_122F650`)

3,917 decompiled lines. Takes the classified instruction lists from Pass A and determines the emission order that optimizes scheduling. Operates on 8 bitvector arrays allocated via the `sub_BDxxxx` bitvector library:

| Bitvector | Purpose |
|-----------|---------|
| v521 | Main liveness set (all instructions) |
| v523 | Load-group register liveness |
| v525 | Store-group register liveness |
| v527 | ALU-group register liveness |
| v529 | Control-flow register liveness |
| v531 | Cross-block interference set |
| v533 | Scheduling priority set |
| v535 | Secondary interference set |

Each bitvector is sized to the function's total register count (`*(ctx+224)`). Pass B iterates through instructions, populates the bitvectors with defined-register information via `sub_BDBC70` (set bit), then merges category-specific vectors into the main set via `sub_BDC5F0` (union) in an order determined by the dependency analysis.

The single switch at line 2578 dispatches on the instruction category:

```
case 4 (ALU):     merge load + store + ALU vectors into main
case 3 (branch):  merge load vector only
case 0 (uncategorized): merge store vector only
case 2 (load):    merge ALU vector only
case 1 (store):   no merge (control-flow kept separate)
```

Knob-derived flags control reordering aggressiveness:
- Knob at target offset +7416 (index ~103): enable load reordering
- Knob at target offset +7488: enable general reordering
- All reordering disabled when `*(ctx+1584)+372 == 12288` (specific regalloc config)

Pass B also maintains a red-black tree structure for the emission schedule, with standard left/right/parent pointers at node offsets 0, 8, 16.

### Differences from SM100

| Aspect | SM89/90 | SM100 (Blackwell) |
|--------|---------|-------------------|
| Pre-encode reordering | Present (sub_122AD60 + sub_122F650) | Absent -- scheduling integrated into own pass |
| Instruction classification | 5-category scheme (branch/load/store/ALU/other) | 370-category opcode dispatch via megafunctions |
| Cost model | Floating-point heuristic (4 tunable weights) | Table-driven via hardware profile records |
| Liveness tracking | 8 bitvectors per block | Handled in scheduling pass, not in encoding |
| Knob control | Knobs 103, 106, 218, 230, 487, 501 | Different knob set for Blackwell scheduler |
| Register class validation | sub_91BF30/sub_91E390 per-move check | Per-instruction class check at encoding time |
| Binary encoder calls | None -- IR-level manipulation only | sub_7B9B80 (18,347 callers) |

The SM89/90 pair operates entirely at the Mercury IR level and produces no packed instruction bits. It rewrites the instruction linked lists in each basic block to optimize scheduling, after which the standard encoding pipeline (Layers 1--3) runs on the reordered sequence. SM100 Blackwell does not need this layer because its scheduling infrastructure (documented in scheduling/algorithm.md) already integrates instruction ordering into the scheduling pass itself.

### SM89/90 Codec Function Map

| Address | Size | Lines | Identity | Confidence |
|---------|-----:|------:|----------|-----------|
| `sub_1233D70` | 6 KB | 321 | **sm89_orchestrator** -- guards, cost params, calls A+B | HIGH |
| `sub_122AD60` | 112 KB | 4,118 | **sm89_classify_reorder** -- instruction classification + block reordering | HIGH |
| `sub_122F650` | 105 KB | 3,917 | **sm89_emission_order** -- scheduling-aware emission ordering | HIGH |
| `sub_122AA30` | ~3 KB | 186 | **local_reorder** -- within-block instruction interleaving | HIGH |
| `sub_1227D90` | ~9 KB | 522 | **instruction_reinsert** -- unlink + reinsert at new position | HIGH |
| `sub_122F1E0` | ~6 KB | 330 | **scheduling_heuristic** -- cost-function comparison for emission order | MEDIUM |
| `sub_1228670` | ~0.5 KB | 30 | **instruction_classify** -- 5-category classifier (returns 0--4) | CERTAIN |
| `sub_1228EF0` | ~0.5 KB | 38 | **is_special** -- predicate: types 9/5/12 return false | CERTAIN |
| `sub_1226E80` | ~0.3 KB | 22 | **list_prepend** -- insert instruction at list head | CERTAIN |
| `sub_1226EB0` | ~5 KB | 274 | **instruction_finalize** -- post-reorder operand fixup | HIGH |
| `sub_1227820` | ~1 KB | 77 | **operand_offset_update** -- adjust operand offsets after move | HIGH |
| `sub_1227B60` | ~0.5 KB | 31 | **motion_check** -- can instruction move to new position? | HIGH |
| `sub_1228FA0` | ~2 KB | 100 | **regclass_propagate** -- propagate register class after move | HIGH |
| `sub_12292B0` | ~0.5 KB | 38 | **queue_init_A** -- initialize classification queue | HIGH |
| `sub_1229330` | ~0.5 KB | 38 | **queue_init_B** -- initialize classification queue | HIGH |
| `sub_1229BD0` | ~2 KB | 107 | **tree_rebalance** -- red-black tree rebalance | MEDIUM |
| `sub_122A050` | ~1 KB | 77 | **pre_pass_init** -- initialize pass A state object | HIGH |
| `sub_122A1A0` | ~2 KB | 139 | **block_resize** -- resize bitvector for new block count | HIGH |

## Function Map

| Address | Size | Callers | Identity | Confidence |
|---------|------|---------|----------|---|
| `sub_7B9B80` | 216 B | 18,347 | **bitfield_insert** -- core packer into 1280-bit buffer | CERTAIN |
| `sub_7B9D30` | 38 B | 2,408 | **clear_cbuf_slots** -- memset(a1+468, 0xFF, 64) | HIGH |
| `sub_7B9D60` | 408 B | 2,408 | **encode_reuse_predicate** -- reuse flags + guard predicate | HIGH |
| `sub_7BC030` | 814 B | 6,147 | **encode_register** -- GPR operand encoder | HIGH |
| `sub_7BC360` | ~500 B | 126 | **encode_uniform_register** -- UR operand encoder | HIGH |
| `sub_7BC5C0` | 416 B | 1,449 | **encode_predicate** -- predicate operand encoder | HIGH |
| `sub_7BCF00` | 856 B | 1,657 | **encode_immediate** -- immediate/cbuf operand encoder | HIGH |
| `sub_7BD260` | ~300 B | 96 | **decode_finalize** -- extract control bits | HIGH |
| `sub_7BD3C0` | ~500 B | 286 | **decode_register** -- GPR operand decoder | HIGH |
| `sub_7BD650` | ~400 B | 115 | **decode_register_alt** -- destination register decoder | HIGH |
| `sub_7BE090` | ~400 B | 50 | **decode_predicate** -- predicate operand decoder | HIGH |
| `sub_10B6180` | 21 B | 8,091 | **encode_bool_field** -- 1-bit opcode-to-control mapping | HIGH |
| `sub_10B6160` | 21 B | 2,205 | **encode_bool_field_B** -- 1-bit flag variant | HIGH |
| `sub_10B6140` | 21 B | 1,645 | **encode_bool_field_C** -- 1-bit flag variant | HIGH |
| `sub_10AFF80` | 11 KB | 3 | **instruction_constructor** -- 32-param object builder | HIGH |
| `sub_10ADF90` | 2.2 KB | 357 | **instruction_unlink** -- linked-list remove + recycle | HIGH |
| `sub_10B0BE0` | 6.5 KB | -- | **hash_table_insert_64** -- FNV-1a, 8-byte key, 4x resize | HIGH |
| `sub_10B1C30` | 3.9 KB | -- | **hash_table_insert_32** -- FNV-1a, 4-byte key | HIGH |
| `sub_10C0B20` | 180 KB | 3,109 | **setField** -- field value writer dispatch | HIGH |
| `sub_10D5E60` | 197 KB | 961 | **getFieldOffset** -- field bit-position lookup dispatch | HIGH |
| `sub_10E32E0` | 187 KB | 72 | **hasField** -- field existence query dispatch | HIGH |
| `sub_10CCD80` | 142 KB | 4 | **setFieldDefault** -- default value writer dispatch | MEDIUM |
| `sub_10CAD70` | 68 KB | 74 | **getOperandFieldOffset** -- per-operand field offset dispatch | HIGH |
| `sub_10C7690` | 65 KB | 288 | **setOperandField** -- per-operand field writer dispatch | HIGH |
| `sub_AF7DF0` | -- | 7,355 | **encoded_to_ir_register** -- hardware reg to IR translation | HIGH |
| `sub_AF7200` | -- | 552 | **encoded_to_ir_predicate** -- hardware pred to IR translation | HIGH |
| `sub_EB3040` | 1.9 KB | -- | **decode_dispatcher** -- binary search on instruction type | HIGH |
| `sub_112CDA0` | 8.9 KB | -- | **register_pair_encoder** -- 40-pair mapping via if-chain | HIGH |

## Cross-References

- [Mercury Encoder](mercury.md) -- the assembler backend that invokes the encoding phase
- [Capsule Mercury & Finalization](capmerc.md) -- post-encoding finalization
- [SASS Opcode Catalog](../reference/sass-opcodes.md) -- full mnemonic table
- [Instruction Selection](isel.md) -- the preceding pipeline phase
- [Blackwell (SM 100-121)](../targets/blackwell.md) -- SM100 architecture details
- [IR Instructions & Opcodes](../ir/instructions.md) -- the Ori IR instruction format consumed by the encoder
