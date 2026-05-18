# Relocation Application Engine

The relocation application engine is the bit-level instruction patching core of nvlink's relocation pipeline. It comprises three tightly-coupled layers: the main relocation loop (`sub_469D60`), the descriptor-driven action dispatcher (`sub_468760`), and two bit-field helper functions (`sub_468670` for extraction and `sub_4685B0` for insertion). Together they transform unresolved relocation records into patched instruction words and data, encoding resolved symbol addresses into the non-byte-aligned bit fields that GPU instructions use for immediates, offsets, and function descriptors.

This page documents all three layers in reimplementation-grade detail. For the surrounding pipeline context -- how the relocation phase fits between layout and finalization, the 10-step resolution algorithm, alias chain resolution, dead function filtering, and unified relocation remapping -- see [Relocation Phase](../pipeline/relocate.md). For the catalog of relocation type names and their descriptor table entries, see [R\_CUDA Relocations](r-cuda-relocations.md).

## Key Facts

| Property | Value |
|---|---|
| Main relocation loop | `sub_469D60` at `0x469D60` (26,578 bytes, 985 lines) |
| Action dispatcher | `sub_468760` at `0x468760` (14,322 bytes, 582 lines) |
| Bit-field extractor | `sub_468670` at `0x468670` (~240 bytes) |
| Bit-field writer | `sub_4685B0` at `0x4685B0` (~240 bytes) |
| Resolved-rela emitter | `sub_46ADC0` at `0x46ADC0` (11,515 bytes) |
| CUDA descriptor table | `off_1D3DBE0` -- indexed by raw R\_CUDA type |
| Mercury descriptor table | `off_1D3CBE0` -- indexed by R\_MERCURY type minus `0x10000` |
| Descriptor entry size | 64 bytes (12-byte header + 3 x 16-byte action slots + 4-byte sentinel) |
| Maximum bit-field width | 128 bits (spans up to three 64-bit words) |
| SSE optimization | `_mm_loadu_si128` for 128-bit relocation record loading |

## Architecture Overview

```
sub_469D60 (apply_relocations)                       MAIN LOOP
 |
 |  for each reloc_node in linked list at ctx+376:
 |    1. Load 32-byte relocation record via _mm_loadu_si128 (2x 128-bit)
 |    2. Resolve addend symbol (sub_440590)
 |    3. Select descriptor table (Mercury vs CUDA)
 |    4. Resolve target symbol, get section record
 |    5. Handle special cases (aliases, dead funcs, unified remapping)
 |    6. Walk section data chunks to find patch_ptr
 |    7. Call sub_468760 (action dispatcher)
 |    8. Unlink node from list, optionally preserve for resolved-rela
 |
 +---> sub_468760 (action dispatcher)                ACTION LOOP
        |
        |  descriptor = table + (type << 6)
        |  for each action in descriptor[+12..+60]:
        |    switch (action_type):
        |      case 1/0x12/0x2E: ABS_FULL
        |      case 6/0x37:      ABS_LO
        |      case 7/0x38:      ABS_HI
        |      case 8:           ABS_SIZE
        |      case 9:           SHIFTED_2
        |      case 0xA:         SEC_TYPE_LO
        |      case 0xB:         SEC_TYPE_HI
        |      case 0x10:        PC_REL
        |      case 0x13/0x14:   CLEAR
        |      case 0x16-0x1D,0x2F-0x36: MASKED_SHIFT
        |
        +---> sub_468670 (extract old value)         BIT-FIELD READ
        +---> sub_4685B0 (write new value)           BIT-FIELD WRITE
```

## Main Relocation Loop: sub_469D60

### Signature

```c
char apply_relocations(
    void*               ctx,          // a1: linker context object
    pthread_mutexattr_t* mutex_attr   // a2: mutex attributes (passed through)
);
```

### Relocation Record Format

Each relocation is stored as a 32-byte record accessed through a singly-linked list at `ctx+376`. Each list node is a 16-byte pair: `[next_ptr, record_ptr]`. The 32-byte record is loaded as two 128-bit SSE values via `_mm_loadu_si128`:

```c
// At line 236-237 of sub_469D60:
v162 = _mm_loadu_si128(v5);        // bytes [0:16]  of reloc record
v163 = _mm_loadu_si128(v5 + 1);    // bytes [16:32] of reloc record
```

The record layout:

```
Offset  Size    Field             Access in decompiled code
------  ------  ----------------  ---------------------------------
+0      int64   addend            v5->m128i_i64[0]
+8      int32   reloc_type        (uint32_t)(v5->m128i_i64[1])
+12     int32   symbol_index      SHIDWORD(v5->m128i_i64[1])  (v5->m128i_i32[3])
+16     int64   extra             v5[1].m128i_i64[0]
+24     uint32  section_idx       v5[1].m128i_u32[2]
+28     uint32  sym_addend_idx    v5[1].m128i_i32[3]
```

The `v5[1]` `__m128i` covers bytes [16:32] of the record, so `v5[1].m128i_i64[0]` reads bytes [16:24] (the `extra` slot used for in-instruction addend accumulation), `v5[1].m128i_u32[2]` reads bytes [24:28], and `v5[1].m128i_i32[3]` reads bytes [28:32]. The writer-side emitter `sub_46ADC0` confirms this layout: it loads `section_idx` from `*(_DWORD *)(rec+24)` and `sym_addend_idx` from `*(_DWORD *)(rec+28)`, and accumulates extracted bit-field values into `*(_QWORD *)(rec+16)` -- the same byte ranges accessed through the `__m128i` view in the apply engine.

The SSE `_mm_loadu_si128` loads are an optimization: rather than reading individual fields with scalar loads, the entire 32-byte record is loaded in two unaligned 128-bit operations, which the CPU can handle efficiently. The decompiler shows the record stored in `__m128i` typed variables (`v5` is an `__m128i*`), with fields accessed through the union members `.m128i_i32[]`, `.m128i_u32[]`, and `.m128i_i64[]`.

### Loop Structure Pseudocode

```c
void apply_relocations(linker_ctx* ctx, pthread_mutexattr_t* mutex_attr) {
    // Initialize output buffers
    int64_t  output_value = 0;
    __m128i  record_lo, record_hi;
    uint16_t link_type = *(uint16_t*)(ctx + 16);

    if (!*(uint8_t*)(ctx + 81))
        sub_44DB00(ctx);              // pre-pass initialization

    reloc_node* node = *(reloc_node**)(ctx + 376);
    reloc_node* prev = NULL;

    while (node != NULL) {
        reloc_record* rec = node->record;

        // ---- Step 1: Addend symbol resolution ----
        uint32_t sym_addend_idx = rec->sym_addend_idx;   // offset +28
        if (sym_addend_idx != 0) {
            symbol_rec* sym = sub_440590(ctx, sym_addend_idx);
            rec->addend += *(int64_t*)(sym + 8);
        }

        // ---- Step 2: Architecture-dependent table selection ----
        uint32_t reloc_type = (uint32_t)rec->reloc_info;
        uint32_t flags_mask = (*(uint8_t*)(ctx + 7) == 'A') ? 1 : 0x80000000;
        uint32_t adjusted_type;
        void** descriptor_table;

        if (flags_mask & *(uint32_t*)(ctx + 48)) {
            // Mercury path
            descriptor_table = &off_1D3CBE0;
            if (reloc_type != 0) {
                if (reloc_type <= 0x10000)
                    error("unexpected reloc");
                adjusted_type = reloc_type - 0x10000;
            } else {
                adjusted_type = 0;
            }
        } else {
            // CUDA path
            descriptor_table = &off_1D3DBE0;
            adjusted_type = reloc_type;
        }

        // ---- Step 3: Symbol resolution ----
        symbol_rec* target_sym = sub_440590(ctx, rec->symbol_index);
        int sym_section = sub_440350(ctx, target_sym);

        // ---- Step 4: Section lookup ----
        section_rec* sec = sub_442270(ctx, rec->section_idx);
        section_rec* parent = sub_442270(ctx, sec->parent_idx);  // +44

        // ---- Step 5: Special handling ----
        // (UFT/UDT magic section 0x7000000E = SHT_CUDA_UFT, alias chains, dead func filtering,
        //  unified relocation remapping -- see pipeline/relocate.md for full detail)

        // ---- Step 6: Descriptor compatibility check ----
        // Validate PC-relative branch is within same section:
        if (descriptor_table[5 * adjusted_type] == 16 &&
            rec->section_idx != parent->output_idx)
            error("PC relative branch address should be in the same section");

        // ---- Step 7: Locate patch address in section data ----
        chunk_node* chunk = *(chunk_node**)(parent + 72);
        uint64_t target_offset = rec->addend;
        uint64_t* patch_ptr = NULL;

        while (chunk) {
            chunk_data* data = chunk->data;
            if (target_offset >= data->base) {
                uint64_t delta = target_offset - data->base;
                if (delta < data->size) {
                    patch_ptr = data->buffer + delta;
                    break;
                }
            }
            chunk = chunk->next;
        }
        if (!patch_ptr)
            error("reloc address not found");

        // ---- Step 8: Verbose trace ----
        if (*(uint8_t*)(ctx + 64) & 4)
            fprintf(stderr, "resolve reloc %d for sym=%d+%lld at "
                    "<section=%d,offset=%llx>\n",
                    reloc_type, rec->symbol_index,
                    rec->extra, rec->section_idx, rec->addend);

        // ---- Step 9: Apply via action dispatcher ----
        bool is_absolute = (sec->sh_type == 4);  // SHT_RELA
        int success = sub_468760(
            descriptor_table,           // a1: table base
            adjusted_type,              // a2: type index
            is_absolute,                // a3: absolute flag
            patch_ptr,                  // a4: instruction word pointer
            rec->extra,                 // a5: extra offset field
            rec->addend,                // a6: addend / section offset
            *(uint64_t*)(target_sym + 8),  // a7: resolved symbol value
            *(uint32_t*)(target_sym + 28), // a8: symbol st_size
            parent->sh_type - 0x70000064,  // a9: sh_type - SHT_CUDA_CONSTANT0 (constant-bank delta)
            &output_value               // a10: receives extracted old value
        );

        if (!success)
            error("unexpected NVRS");

        // ---- Step 10: Unlink node and optionally preserve ----
        reloc_node* next = node->next;
        if (prev)
            prev->next = next;
        else
            *(reloc_node**)(ctx + 376) = next;

        // Preserve-relocs path (--preserve-relocs, byte at ctx+85)
        if (*(uint8_t*)(ctx + 85) &&
            ((target_sym->st_bind & 3) != 1 ||     // not STB_LOCAL
             (sym_section != 0 && parent_has_data)))
        {
            if (sec->sh_type != 4)     // not SHT_RELA
                rec->extra = output_value;
            sub_4644C0(rec, ctx + 384);  // append to preserve-relocs list
        } else {
            sub_431000(node->record);    // free record
        }
        sub_431000(node);                // free list node

        node = prev ? *prev->next_field : *(reloc_node**)(ctx + 376);
    }

    // ---- Post-loop: .nv.rel.action emission (non-Mercury, link_type == 2) ----
    // ... (see section below)
}
```

### SSE Relocation Record Loading

The use of `_mm_loadu_si128` at line 236-237 of the decompiled code is a deliberate optimization. Instead of 8 separate 32-bit loads (or 4 separate 64-bit loads) to access the 32-byte relocation record, the compiler (or original author) uses two 128-bit unaligned loads:

```c
v162 = _mm_loadu_si128(v5);        // load bytes [0:15]  into xmm register
v163 = _mm_loadu_si128(v5 + 1);    // load bytes [16:31] into xmm register
```

The `_mm_loadu_si128` intrinsic generates a single `MOVDQU` instruction on x86-64, which loads 16 bytes from an unaligned address into an XMM register. This is cache-friendly (two cache-line-width loads vs. many scattered accesses) and avoids potential store-forwarding stalls. The individual fields are then extracted from the `__m128i` values using union member access (`.m128i_i32[n]`, `.m128i_i64[n]`).

### Symbol Resolution During Relocation

Symbol resolution in `sub_469D60` is a multi-step process:

1. **Addend symbol resolution** (`sym_addend_idx` at record offset +28): If nonzero, `sub_440590(ctx, sym_addend_idx)` returns the symbol record, and `*(int64_t*)(sym + 8)` (the resolved symbol value) is added to the record's addend at offset +0. This implements the `S + A` (symbol + addend) pattern.

2. **Target symbol resolution** (`symbol_index` at record offset +12): `sub_440590(ctx, SHIDWORD(rec->reloc_info))` returns the target symbol record. The symbol's resolved address at offset +8 becomes the `a7` (symbol\_value) argument to `sub_468760`.

3. **Section-relative vs absolute**: The `is_absolute` flag (parameter `a3` to the action dispatcher) is derived from whether the target section type equals `SHT_RELA` (type 4). When `is_absolute == true`, the engine computes `value = symbol_value + extra_offset` and writes it directly. When false (the common case), the engine first extracts the existing bit-field value from the instruction word, adds it to the symbol value, and writes back the sum -- implementing addend-based relocation.

4. **Alias chain traversal**: When a target symbol is a weak function (`STT_FUNC` = 2) with an unresolved value (offset +8 is zero), the function follows the alias chain via `sub_440350` to find the canonical definition. The verbose trace `"change alias reloc %s to %s\n"` is emitted when this occurs.

### Section Data Chunk Walk

Section data in nvlink is not stored as a flat buffer. Instead, each section record has a linked list of data chunks at offset +72. Each chunk node has the structure:

```
chunk_node:
    [0]  next pointer     (chunk_node* or NULL)
    [1]  data descriptor  (chunk_data*)

chunk_data:
    [0]  buffer pointer   (void*)
    [1]  base offset      (uint64_t) -- starting offset within section
    [3]  size             (uint64_t) -- number of bytes in this chunk
```

The relocation loop walks this list linearly to find the chunk containing `target_offset`:

```c
while (chunk) {
    chunk_data* data = chunk->data;
    if (target_offset >= data->base) {
        uint64_t delta = target_offset - data->base;
        if (delta < data->size) {
            patch_ptr = (uint64_t*)(data->buffer + delta);
            break;
        }
    }
    chunk = chunk->next;
}
```

The `patch_ptr` is then passed directly to the action dispatcher, which reads and modifies the instruction word(s) at that address.

## Descriptor Table Lookup

### Table Selection

The descriptor table is selected based on the architecture flag at `ctx+7`:

| Architecture | Flag byte | Descriptor table | Relocation type normalization |
|---|---|---|---|
| Mercury (SM100+) | `'A'` (0x41) | `off_1D3CBE0` | `type -= 0x10000` (Mercury types are CUDA types + 65536) |
| CUDA (pre-Mercury) | Other | `off_1D3DBE0` | `type` used directly |

### Descriptor Entry Structure

Each descriptor entry is located at `table_base + (reloc_type_index << 6)`, yielding a 64-byte record. The `<< 6` shift is equivalent to multiplying by 64 (the entry size). The 64-byte layout is:

```
Byte offset  Size    Field
-----------  ------  -----------------------------------------------
+0           12 B    Header (3 x uint32: flags, mode, reserved)
                     Used by sub_46ADC0 for preserve-relocs extraction;
                     field at +5 (in uint32 units, i.e. +20 bytes) holds
                     the descriptor mode (16 = PC-relative)
+12          16 B    action[0]  {bit_offset, bit_width, action_type, reserved}
+28          16 B    action[1]  {bit_offset, bit_width, action_type, reserved}
+44          16 B    action[2]  {bit_offset, bit_width, action_type, reserved}
+60          4 B     Sentinel   (address stored in v100, marks end of action array)
```

Each action slot is 16 bytes = 4 x `uint32_t`:

```c
struct reloc_action {
    uint32_t bit_offset;    // starting bit position in instruction word
    uint32_t bit_width;     // number of bits to patch
    uint32_t action_type;   // operation code (0 = END/skip)
    uint32_t reserved;      // unused / flags
};
```

### Descriptor Header and Preserve-Relocs Extraction

The 12-byte header at the start of each descriptor entry is not used by the action dispatcher (`sub_468760`), but it is consumed by the resolved-rela emitter (`sub_46ADC0`). During preserve-relocs processing, `sub_46ADC0` reads three field specifications from the header at uint32 offsets (+3,+4,+5), (+7,+8,+9), and (+11,+12,+13), each encoding a bit-field extraction recipe for recovering the instruction-encoded portions of the relocation value after patching.

## Action Dispatcher: sub_468760

### Signature

```c
int reloc_apply_engine(
    void*       descriptor_table,    // a1: off_1D3CBE0 or off_1D3DBE0
    uint32_t    reloc_type_index,    // a2: normalized type index into table
    bool        is_absolute,         // a3: 1 if symbol has absolute address
    uint64_t*   patch_ptr,           // a4: pointer into section data
    int64_t     extra_offset,        // a5: reloc_record->extra field
    int         section_offset,      // a6: addend / section base offset
    uint64_t    symbol_value,        // a7: resolved symbol address
    uint32_t    symbol_size,         // a8: symbol st_size
    uint32_t    section_type_delta,  // a9: sh_type - 0x70000064 (SHT_CUDA_CONSTANT0)
    int64_t*    output_value         // a10: receives extracted old value
);
// Returns 1 on success, 0 on unrecognized action type.
```

### Initialization and Value Computation

Before the action loop begins, the engine performs three setup operations:

```c
// Line 122-128 of decompiled sub_468760:
uint64_t value = symbol_value;           // a7 -> v10
if (is_absolute)
    value = symbol_value + extra_offset; // a7 + a5
*output_value = 0;                       // zero the output

// Pre-load SSE constants for masked-shift actions:
__m128i mask0 = _mm_load_si128(&xmmword_1D3F8E0);  // mask table [0:1]
__m128i mask1 = _mm_load_si128(&xmmword_1D3F8F0);  // mask table [2:3]
__m128i mask2 = _mm_load_si128(&xmmword_1D3F900);  // mask table [4:5]
__m128i mask3 = _mm_load_si128(&xmmword_1D3F910);  // mask table [6:7]

// Compute descriptor entry pointer and action boundaries:
uint64_t desc_entry = table_base + ((uint64_t)reloc_type_index << 6);
uint32_t* action_ptr = (uint32_t*)(desc_entry + 12);   // first action
uint32_t* sentinel   = (uint32_t*)(desc_entry + 60);   // end marker
```

The four SSE constant loads (`_mm_load_si128`) happen once at function entry. They load 64 bytes of mask data into local storage, avoiding repeated memory access during the masked-shift action cases. The shift table (`xmmword_1D3F920` and `xmmword_1D3F930`) is loaded on-demand inside the masked-shift case body.

### Action Loop Pseudocode

```c
while (true) {
    uint32_t action_type = action_ptr[2];    // v15[2] = action_type

    switch (action_type) {
    case 0:  // END
        action_ptr += 4;                     // advance to next 16-byte slot
        if (action_ptr == sentinel) return 1;
        continue;

    case 1: case 0x12: case 0x2E: {          // ABS_FULL
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];

        // Fast path: full 64-bit word write
        if (bit_off == 0 && bit_wid == 64) {
            if (!is_absolute) {
                *output_value = *patch_ptr;
                value += *patch_ptr;
            }
            *patch_ptr = value;
            action_ptr += 4;
            if (action_ptr == sentinel) return 1;
            continue;
        }

        // General path: bit-field write
        if (!is_absolute) {
            int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
            value += old;
            *output_value = old;
        }
        action_ptr += 4;
        bitfield_write(patch_ptr, value, bit_off, bit_wid);
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 6: case 0x37: {                     // ABS_LO (low 32 bits)
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];
        uint32_t lo = (uint32_t)value;       // truncate to low 32 bits

        if (!is_absolute) {
            int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
            *output_value = old;
            lo = (uint32_t)value + old;
        }
        write_bitfield_inline(patch_ptr, lo, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 7: case 0x38: {                     // ABS_HI (high 32 bits)
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];
        uint32_t hi = (uint32_t)(value >> 32);  // HIDWORD

        if (!is_absolute) {
            int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
            *output_value = old;
            hi = (uint32_t)(value >> 32) + old;
        }
        write_bitfield_inline(patch_ptr, hi, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 8: {                                // ABS_SIZE
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];

        // Note: case 8 OVERWRITES the running value (symbol_value [+ extra]).
        // No symbol_value is propagated into the result -- the patched field
        // receives either extra_offset+size or extracted_old+size only.
        if (is_absolute) {
            value = extra_offset + symbol_size;
        } else {
            int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
            value = old + symbol_size;
            *output_value = old;
        }
        write_bitfield_inline(patch_ptr, value, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 9: {                                // SHIFTED_2 (>> 2)
        value >>= 2;                        // byte offset to DWORD offset
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];

        if (!is_absolute) {
            int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
            value += old;
            *output_value = old;
        }
        write_bitfield_inline(patch_ptr, value, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 0xA: {                              // SEC_TYPE_LO
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];
        value = section_type_delta & (uint64_t)(255 >> (8 - bit_wid));

        if (!is_absolute)
            value += bitfield_extract(patch_ptr, bit_off, bit_wid);
        write_bitfield_inline(patch_ptr, value, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 0xB: {                              // SEC_TYPE_HI
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];
        value = ((uint64_t)section_type_delta >> 4) & (255 >> (8 - bit_wid));

        if (!is_absolute)
            value += bitfield_extract(patch_ptr, bit_off, bit_wid);
        write_bitfield_inline(patch_ptr, value, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 0x10: {                             // PC_REL
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];

        if (!is_absolute) {
            int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
            value += old;
            *output_value = old;
        }
        uint64_t pc_value = (int32_t)value - section_offset;
        write_bitfield_inline(patch_ptr, pc_value, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 0x13: case 0x14: {                  // CLEAR
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];
        // Write all zeros -- no extraction, no value computation
        write_bitfield_zero(patch_ptr, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    case 0x16: case 0x17: case 0x18: case 0x19:
    case 0x1A: case 0x1B: case 0x1C: case 0x1D:
    case 0x2F: case 0x30: case 0x31: case 0x32:
    case 0x33: case 0x34: case 0x35: case 0x36: {  // MASKED_SHIFT
        uint32_t idx = action_type - 22;
        uint32_t bit_off = action_ptr[0];
        uint32_t bit_wid = action_ptr[1];

        // Load shift table on demand
        uint32_t shift_table[8];
        _mm_store_si128(&shift_table[0], _mm_load_si128(&xmmword_1D3F920));
        _mm_store_si128(&shift_table[4], _mm_load_si128(&xmmword_1D3F930));

        // mask_table was pre-loaded at function entry
        uint64_t mask  = mask_table[idx];
        uint32_t shift = shift_table[idx];

        if (!is_absolute) {
            int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
            value += old;
            *output_value = old;
        }
        value = (mask & value) >> shift;
        write_bitfield_inline(patch_ptr, value, bit_off, bit_wid);
        action_ptr += 4;
        if (action_ptr == sentinel) return 1;
        continue;
    }

    default:
        return 0;  // unrecognized action type -> caller emits "unexpected NVRS"
    }
}
```

### Complete Action Code Table

The following table enumerates every action code handled by the switch statement in `sub_468760`:

| Code | Hex | Name | Value computation | Notes |
|---|---|---|---|---|
| 0 | 0x00 | END | None | Advance to next slot; terminate if at sentinel |
| 1 | 0x01 | ABS\_FULL | `value` (unchanged) | Standard absolute write. Fast path when `bit_offset==0 && bit_width==64`: direct 64-bit word write bypassing all bit-field logic |
| 6 | 0x06 | ABS\_LO | `(uint32_t)value` | Low 32 bits of the relocation value |
| 7 | 0x07 | ABS\_HI | `(uint32_t)(value >> 32)` | High 32 bits of the relocation value |
| 8 | 0x08 | ABS\_SIZE | `extracted_old + symbol_size` | When `is_absolute`: `extra_offset + symbol_size`. Overwrites the running value; no symbol address is mixed in. Used for relocations against array/sized symbols where the patched field must carry the symbol's byte size combined with an existing addend |
| 9 | 0x09 | SHIFTED\_2 | `value >> 2` | Right-shift by 2 for 4-byte-aligned (DWORD) addresses. Converts byte offsets to DWORD indices |
| 10 | 0x0A | SEC\_TYPE\_LO | `section_type_delta & (255 >> (8 - bit_width))` | Low bits of the section type offset, masked to fit bit\_width |
| 11 | 0x0B | SEC\_TYPE\_HI | `(section_type_delta >> 4) & (255 >> (8 - bit_width))` | High bits of the section type offset, shifted right by 4 then masked |
| 16 | 0x10 | PC\_REL | `(int32_t)value - section_offset` | PC-relative: sign-extends value to 32-bit, subtracts the section offset (argument a6) |
| 18 | 0x12 | ABS\_FULL\_ALT1 | Same as code 1 | Alternate ABS\_FULL code; shares identical case body |
| 19 | 0x13 | CLEAR | Zero | Clears the target bit field to all-zeros |
| 20 | 0x14 | CLEAR\_ALT | Zero | Alternate CLEAR code; shares identical case body with 0x13 |
| 22 | 0x16 | MASKED\_SHIFT\_0 | `(value & mask_table[0]) >> shift_table[0]` | Table-driven mask-and-shift, index 0 |
| 23 | 0x17 | MASKED\_SHIFT\_1 | `(value & mask_table[1]) >> shift_table[1]` | Table-driven mask-and-shift, index 1 |
| 24 | 0x18 | MASKED\_SHIFT\_2 | `(value & mask_table[2]) >> shift_table[2]` | Table-driven mask-and-shift, index 2 |
| 25 | 0x19 | MASKED\_SHIFT\_3 | `(value & mask_table[3]) >> shift_table[3]` | Table-driven mask-and-shift, index 3 |
| 26 | 0x1A | MASKED\_SHIFT\_4 | `(value & mask_table[4]) >> shift_table[4]` | Table-driven mask-and-shift, index 4 |
| 27 | 0x1B | MASKED\_SHIFT\_5 | `(value & mask_table[5]) >> shift_table[5]` | Table-driven mask-and-shift, index 5 |
| 28 | 0x1C | MASKED\_SHIFT\_6 | `(value & mask_table[6]) >> shift_table[6]` | Table-driven mask-and-shift, index 6 |
| 29 | 0x1D | MASKED\_SHIFT\_7 | `(value & mask_table[7]) >> shift_table[7]` | Table-driven mask-and-shift, index 7 |
| 46 | 0x2E | ABS\_FULL\_ALT2 | Same as code 1 | Second alternate ABS\_FULL code |
| 47 | 0x2F | MASKED\_SHIFT\_25 | `(value & mask_table[25]) >> shift_table[25]` | Table-driven mask-and-shift, index 25 |
| 48 | 0x30 | MASKED\_SHIFT\_26 | `(value & mask_table[26]) >> shift_table[26]` | Table-driven mask-and-shift, index 26 |
| 49 | 0x31 | MASKED\_SHIFT\_27 | `(value & mask_table[27]) >> shift_table[27]` | Table-driven mask-and-shift, index 27 |
| 50 | 0x32 | MASKED\_SHIFT\_28 | `(value & mask_table[28]) >> shift_table[28]` | Table-driven mask-and-shift, index 28 |
| 51 | 0x33 | MASKED\_SHIFT\_29 | `(value & mask_table[29]) >> shift_table[29]` | Table-driven mask-and-shift, index 29 |
| 52 | 0x34 | MASKED\_SHIFT\_30 | `(value & mask_table[30]) >> shift_table[30]` | Table-driven mask-and-shift, index 30 |
| 53 | 0x35 | MASKED\_SHIFT\_31 | `(value & mask_table[31]) >> shift_table[31]` | Table-driven mask-and-shift, index 31 |
| 54 | 0x36 | MASKED\_SHIFT\_32 | `(value & mask_table[32]) >> shift_table[32]` | Table-driven mask-and-shift, index 32 |
| 55 | 0x37 | ABS\_LO\_ALT | Same as code 6 | Alternate ABS\_LO code; shares case body |
| 56 | 0x38 | ABS\_HI\_ALT | Same as code 7 | Alternate ABS\_HI code; shares case body |

Action codes not in this table (0x02-0x05, 0x0C-0x0F, 0x11, 0x15, 0x1E-0x2D, 0x39+) fall to the `default` case and return 0 (failure).

### The 3-Slot Action Model

Each 64-byte descriptor entry supports up to 3 active action slots (at offsets +12, +28, +44), bounded by a 4-byte sentinel at offset +60. This is a critical design choice: a single R\_CUDA relocation type can perform up to 3 sequential bit-field modifications to the same instruction word. This enables complex multi-field relocations like:

- **HI/LO pairs within a single instruction**: Action[0] writes the low 16 bits at one bit position, action[1] writes the high 16 bits at another position, all in one relocation record.
- **Section type encoding**: Action[0] writes SEC\_TYPE\_LO, action[1] writes SEC\_TYPE\_HI to different bit positions.
- **Mixed clear-and-write**: Action[0] clears one field, action[1] writes the value to another field.

The iteration logic:

```c
// v15 starts at desc_entry + 12 (first action)
// v100 = desc_entry + 60 (sentinel)
// Each iteration: v15 += 4 (advance by 4 uint32s = 16 bytes)
// Loop terminates when v15 == v100 OR action_type == 0 (END)
```

Most CUDA relocation types use only action[0] with action[1].action\_type == 0 (END). Multi-action types include certain 128-bit instruction relocations and the section-type encoding pairs.

### Actions 0x16--0x36: Table-Driven Masked Shift

These 16 action codes share a single code path that uses two SSE-loaded lookup tables:

- **Mask table** (`v119[]`): 8 `uint64_t` values (64 bytes total) from `xmmword_1D3F8E0` through `xmmword_1D3F910`. These are AND-masks applied to the relocation value before shifting.
- **Shift table** (`v118[]`): 8 `uint32_t` values (32 bytes total) from `xmmword_1D3F920` and `xmmword_1D3F930`. These are right-shift amounts.

The index into both tables is `action_type - 22`. The computation:

```c
uint32_t idx = action_type - 22;
uint64_t mask  = mask_table[idx];     // from v119
uint32_t shift = shift_table[idx];    // from v118
value = (value & mask) >> shift;
```

The 16 action codes map to table indices as follows:

| Action code range | Decimal range | Table index range |
|---|---|---|
| 0x16 -- 0x1D | 22 -- 29 | 0 -- 7 |
| 0x2F -- 0x36 | 47 -- 54 | 25 -- 32 |

The gap between indices 8 and 24 corresponds to the action codes 0x1E through 0x2E. Action code 0x2E is handled by the ABS\_FULL case, and codes 0x1E through 0x2D fall to the default error path. The mask table has 8 `uint64_t` entries (indices 0--7) covering the first group; the second group (indices 25--32) accesses into the same 64-byte memory region at a higher offset, reading from the pre-loaded SSE vectors.

This table-driven approach eliminates the need for 16 separate switch cases, each with a hardcoded mask and shift. It supports extraction of arbitrary byte lanes, half-words, and sub-fields from the relocation value -- for example, extracting bits [16:24) of a 64-bit address and placing them into an 8-bit instruction field.

### Action 1/0x12/0x2E: Fast Path for Full-Width Writes

The most common action type has an optimized fast path. When `bit_offset == 0` and `bit_width == 64`, the engine bypasses all bit-field logic and writes `value` directly:

```c
if (bit_offset == 0 && bit_width == 64) {
    if (!is_absolute) {
        *output_value = *patch_ptr;   // save old value for preserve-relocs
        value += *patch_ptr;          // S + A pattern
    }
    *patch_ptr = value;
    // advance and return
}
```

This handles 64-bit absolute relocations targeting full data pointers (e.g., `R_CUDA_ABS64_0` patching a function pointer in a `.nv.global` data section). No masking, no shifting, just a direct load-add-store.

For narrower bit fields (the far more common case for instruction patching), the engine follows the general extraction/insertion path using the bit-field helpers.

### Inlined Bit-Field Write Pattern

For all action types except the full-width fast path, the engine performs the bit-field write inline rather than always calling `sub_4685B0`. The pattern, visible in every case body, is:

```c
// 1. Normalize bit_offset to find the target word
uint64_t* words = patch_ptr;
int local_offset = bit_offset;
if (bit_offset > 63) {
    local_offset = (bit_offset - 64) & 0x3F;
    words = &patch_ptr[((bit_offset - 64) >> 6) + 1];
}

// 2. Check if field spans multiple 64-bit words
int total_bits = local_offset + bit_width;
if (total_bits <= 64) {
    // Single-word case: inline read-modify-write
    uint64_t mask = (-1ULL << (64 - bit_width)) >> (64 - total_bits);
    *words = (*words & ~mask) | (value << (64 - bit_width) >> (64 - total_bits));
} else {
    // Multi-word case: loop through intermediate words using sub_4685B0
    int num_intermediate = ((total_bits - 65) >> 6);
    uint64_t* end_word = &words[num_intermediate + 1];
    int off = local_offset;
    do {
        sub_4685B0(words, value, off, 64 - off);
        value >>= (64 - off);
        off = 0;
        words++;
    } while (words != end_word);
    // Adjust total_bits and bit_width for final word
    total_bits = total_bits - (num_intermediate << 6) - 64;
    bit_width = total_bits;
    // Final word: inline read-modify-write as above
    *words = (*words & ~mask) | (value << (64 - bit_width) >> (64 - total_bits));
}
```

`sub_4685B0` is called only for intermediate words in multi-word spans. The final word is always patched inline.

## Bit-Field Extractor: sub_468670

### Signature

```c
int64_t bitfield_extract(
    uint64_t*  words,       // a1: pointer to instruction data
    int        bit_offset,  // a2: starting bit position
    int        bit_width    // a3: number of bits to extract
);
```

### Algorithm

```c
int64_t bitfield_extract(uint64_t* words, int bit_offset, int bit_width) {
    // 1. Word normalization: advance pointer if offset >= 64
    if (bit_offset > 63) {
        uint32_t excess = bit_offset - 64;
        bit_offset = excess & 0x3F;       // reduce modulo 64
        words += (excess >> 6) + 1;       // advance by (excess/64 + 1) words
    }

    int total = bit_width + bit_offset;

    // 2. Single-word case
    if (total <= 64) {
        return *words << (64 - total) >> (64 - bit_width);
    }

    // 3. Multi-word case (recursive)
    int64_t low_part = bitfield_extract(words, bit_offset, 64 - bit_offset);

    int64_t mid_or_high;
    if (total - 64 > 64) {
        // Spans 3 words (very rare: field > 64 bits crossing two boundaries)
        mid_or_high = bitfield_extract(words + 1, 0, 64);
        bitfield_extract(words + 2, 0, total - 128);  // return value unused
    } else {
        // Spans 2 words (common: 128-bit instruction with field crossing boundary)
        mid_or_high = words[1] << (128 - total) >> (64 - (total - 64));
    }

    return low_part | (mid_or_high << (64 - bit_offset));
}
```

The single-word extraction formula `*words << (64 - total) >> (64 - bit_width)` works by:
1. Left-shifting to push the bits above the field off the top of the 64-bit register
2. Right-shifting to align the field to bit 0

The recursion depth is bounded at 2 (for the three-word case). In practice, GPU instructions are at most 128 bits wide, so the two-word path (field straddling one 64-bit boundary) is the only multi-word case encountered.

## Bit-Field Writer: sub_4685B0

### Signature

```c
void bitfield_write(
    uint64_t*  words,       // a1: pointer to instruction data
    uint64_t   value,       // a2: value to write
    int        bit_offset,  // a3: starting bit position
    int        bit_width    // a4: number of bits to write
);
```

### Algorithm

```c
void bitfield_write(uint64_t* words, uint64_t value, int bit_offset, int bit_width) {
    // 1. Word normalization
    if (bit_offset > 63) {
        uint32_t excess = bit_offset - 64;
        bit_offset = excess & 0x3F;
        words += (excess >> 6) + 1;
    }

    uint32_t total = bit_width + bit_offset;

    // 2. Multi-word loop (intermediate words)
    if (total > 64) {
        uint64_t* end_word = &words[((total - 65) >> 6) + 1];
        do {
            // Clear bits above bit_offset, OR in value shifted to position
            uint64_t keep_mask = ~(-1LL << bit_offset);
            *words = (*words & keep_mask) | (value << bit_offset);
            value >>= (64 - bit_offset);
            bit_offset = 0;
            words++;
        } while (words != end_word);
        // Adjust for remaining bits
        total = total - (((total - 65) >> 6) << 6) - 64;
        bit_width = total;
    }

    // 3. Final (or only) word: masked read-modify-write
    uint64_t mask = (-1ULL << (64 - bit_width)) >> (64 - total);
    *words = (*words & ~mask) | (value << (64 - bit_width) >> (64 - total));
}
```

### Mask Construction Detail

The mask formula `(-1ULL << (64 - W)) >> (64 - (O + W))` constructs a window of `W` ones starting at bit position `O`:

```
Step 1: (-1ULL << (64 - W))   -> W ones in the top bits
        Example (W=8): 0xFF00_0000_0000_0000

Step 2: >> (64 - (O + W))     -> shift the window down to start at bit O
        Example (O=20, W=8):   0x0000_000F_F000_0000
        (8 ones at positions 20..27)
```

The value insertion `(value << (64 - W)) >> (64 - (O + W))` performs the same alignment:

```
Step 1: (value << (64 - W))   -> left-align value in the register
Step 2: >> (64 - (O + W))     -> shift down to the target position
```

This two-step approach ensures that only the lower `W` bits of `value` are placed, and they land exactly at the position defined by the mask.

## .nv.rel.action Section Emission

At the end of `sub_469D60` (lines 884--985 in the decompiled output), after all relocations have been applied, the function generates a `.nv.rel.action` section for non-Mercury relocatable links (`link_type == 2`). This section encodes the relocation descriptor actions in a compact format that downstream tools can use to re-apply relocations.

The emission logic:

```c
if (link_type == 2 && !mercury_flag) {
    int max_type = sub_42F690();        // get max relocation type for this arch
    if (max_type != 116) {              // not at maximum (all types present)
        // Create header record: 8-byte entry containing the starting type index
        header = sub_4307C0(section, 8);
        header->type_index = max_type;
        sub_4644C0(header, ctx + 480);

        // Create action records: 8 bytes per (116 - max_type) entries
        int count = 116 - max_type;
        action_buf = sub_4307C0(section, 8 * count);
        memset(action_buf, 0, 8 * count);
        sub_4644C0(action_buf, ctx + 480);

        // Emit section
        section_idx = sub_441AC0(ctx, ".nv.rel.action", 0x7000000B, 0, 0, 0, 8, 8);
        sub_4343C0(ctx, section_idx, 0, header, 0, 8, 8);

        // Iterate descriptor table entries, convert to compact format
        uint8_t* out = action_buf;
        uint32_t* desc = (uint32_t*)(max_type * 64 + 30661612);
        int skipped = 0;

        while (true) {
            int action_type = desc[2];      // action_type at +8 in action slot
            if (action_type == 0) {
                skipped++;
                goto next;
            }
            if (action_type == 21 || action_type == 9) {
                out[0] = 0;
                out[1] = 2 * (action_type == 9);
            } else if (action_type == 1 || (action_type - 22) <= 7) {
                out[0] = (action_type == 1) ? 0 : (action_type - 22);
                out[1] = 0;                 // no shift flag
            } else if ((action_type - 30) <= 7) {
                out[0] = 1;                 // group 1
                out[1] = 0;
            } else if ((action_type - 38) <= 7) {
                out[0] = 2;                 // group 2
                out[1] = 0;
            } else if ((action_type - 46) <= 10) {
                out[0] = 9;                 // group 9
                out[1] = 0;
            } else if (action_type == 3) {
                out[0] = 3;
                out[1] = 0;
            } else {
                out[0] = 0;
                out[1] = 0;
            }
            // Copy remaining descriptor fields
            out[2] = desc[3];               // reserved field
            out[3] = desc[1];               // bit_width
            out[4] = desc[0];               // bit_offset
            out[5] = desc[7];               // action[1].reserved
            out[6] = desc[5];               // action[1].action_type
            out[7] = desc[4];               // action[1].bit_offset
        next:
            out += 8;
            desc += 16;                     // advance by 64 bytes (16 uint32s)
            if (out == end_of_buffer)
                break;
        }
        // Write compacted data, excluding skipped entries
        sub_4343C0(ctx, section_idx, 0, action_buf, 8, 8, 8 * (count - skipped));
    }
}
```

The `.nv.rel.action` section uses `sh_type = 0x7000000B` (`SHT_CUDA_RELOCINFO`). It provides a compact 8-byte-per-type representation of the relocation actions, allowing the CUDA runtime or downstream linker to interpret and re-apply relocations without access to the full 64-byte descriptor table.

## Resolved-Rela Emitter: sub_46ADC0

### Signature

```c
void emit_resolved_relocations(
    void*  linker_ctx,   // a1: linker context
    void*  a2,           // a2: unused / mutex attrs
    void*  a3,           // a3: passed through to sub_442270
    int    a4,           // a4: passed through
    int    a5,           // a5: passed through
    int    a6            // a6: passed through
);
```

### Overview

This function walks two linked lists and writes relocation records into output `.nv.resolvedrela` sections. It is called from the finalization phase when `--preserve-relocs` is active (byte at `ctx+85` nonzero), producing relocations that a downstream linker or the CUDA runtime can re-apply at load time.

### Primary List: ctx+376

The first loop walks the relocation list at `ctx+376`, processing entries that were applied during the relocation phase but retained for output. For each entry:

1. **ELF class check**: Reads `ctx+4` (ELF class byte) and `ctx+16` (link type). Class 1 = ELF32-style relocations; class 2 = RELA-style.

2. **Symbol-addend fold into `r_offset`** (only when link type at `ctx+16` is `1`, i.e. ELF32-style record handling): If `*(uint32_t*)(rec+28)` (sym_addend_idx) is nonzero, calls `sub_444720` to remap the symbol index, then `sub_440590` to look up the symbol record. Validates that the resolved value at `sym+8` is not `-1` (fatal: `"symbol never allocated"`). The resolved value is then added to `*(uint64_t*)(rec+0)` -- the field that becomes the on-disk `r_offset`, **not** the `r_addend`. This is the writer-side `S + A` accumulation: any sub-symbol displacement encoded by the assembler is folded into the relocation target offset before emission.

3. **Section lookup**: Calls `sub_442270` for the target section and its parent section (at section offset `+44`).

4. **Offset validation**: The parent section's data size (at offset `+32`) must be nonzero and must exceed the relocation's target offset (fatal: `"relocation is past end of offset"`).

5. **Descriptor-driven bit-field extraction** (writer-side addend recovery): When `*(uint8_t*)(ctx+89)` is set and the *target* section's type (`sec+4`, where `sec = sub_442270(ctx, rec->section_idx)`) is not `4` (SHT_RELA -- skipping records that already point at a `.rela`-class section), the function selects the descriptor table by architecture (`off_1D3CBE0` for Mercury, `off_1D3DBE0` for CUDA) and performs up to three rounds of `sub_468670` bit-field extraction. For each of three field specifications at descriptor uint32 indices (+3,+4,+5), (+7,+8,+9), and (+11,+12,+13): if the "present" flag (the third uint32 of each triple) is nonzero, the extractor reads `bit_width` bits starting at `bit_offset` from the section data at the relocation site and *adds* the result to `*(uint64_t*)(rec+16)` -- the `extra` slot that becomes the on-disk `r_addend`. This is the writer-side inverse of the apply engine's bit-field write: it recovers the in-instruction addend so a downstream linker or runtime can re-apply the same patch sequence without needing the section bytes. The Mercury branch additionally subtracts `0x10000` from the relocation type before indexing the descriptor table, and validates that the type is above `0x10000` (`"unexpected reloc"`).

6. **Section data location**: Same chunk-list walk as the main engine. The section record at offset `+72` holds a linked list of data chunks. On failure: `"reloc address not found"`.

7. **Symbol index remapping**: Calls `sub_444720` to remap the relocation's symbol index from internal to output ELF `.symtab` numbering.

8. **Rela section creation**: Calls `sub_442760` to find or create the `.rela` output section. On failure: `"rela section never allocated"`.

9. **Output record writing**: For RELA (class == 2): 24-byte record `{offset, info, addend}`. For REL (class != 2): packs `info = (sym_index << 8) + (type & 0xFF)` and writes 12 bytes.

### Secondary List: ctx+384

A second loop processes `ctx+384`. **It runs only when `*(uint8_t*)(ctx+85)` (the `--preserve-relocs` flag) is set** -- there is no entry to this loop on a non-preserve link. Selection criteria, all of which must hold for a record to be emitted:

- Parent section data size (`parent+32`) is nonzero
- Architecture flag check: `(*(uint8_t*)(ctx+7) == 'A' ? 1 : 0x80000000) & *(uint32_t*)(ctx+48)` is nonzero (the Mercury / non-Mercury arch family is live)
- Parent section type (`parent+4`) is `1` (`SHT_PROGBITS`) -- code/data sections only, not synthetic ELF sections
- Parent section flags low byte (`parent+8`) has bit `0x4` set (`SHF_EXECINSTR`) -- text sections only
- Symbol `st_info` low nibble (`sym+4 & 0xF`) is `0xD` (CUDA-extended symbol type, indices texture/surface/bindless)
- Symbol `st_info` high three bits (`sym+5 & 0xE0`) equal `0x40` (binding `STB_CUDA_*` value 2 in the high field)

For qualifying entries, the function constructs the output section name by prepending `".nv.resolvedrela"` to the parent section name (the parent's `sec+96` name string), calls `sub_4411D0` to find or create that section, and writes the relocation record via `sub_4336B0` using the same RELA / REL slab format as the primary loop (24 bytes for ELF64, 12 bytes for ELF32, alignment 8 / 4). Section names are cached across iterations: if consecutive relocations target the same parent section, the `sprintf` and lookup are skipped (`v83 == v20` short-circuit).

Note that the primary loop at `ctx+376` runs **unconditionally** (no `ctx+85` guard) -- the preserve-relocs flag only gates the secondary `.nv.resolvedrela` emission. For a relocatable (`-r`) link with `--preserve-relocs` absent, the primary loop still emits standard `.rela.<section>` entries for all surviving records; only the texture/surface-specific `.nv.resolvedrela.*` sections are suppressed.

## Error Conditions

| Error string | Severity | Source function | Condition |
|---|---|---|---|
| `"unexpected reloc"` | Fatal | `sub_469D60` | Relocation type nonzero but <= 0x10000 in Mercury mode |
| `"reloc address not found"` | Fatal | `sub_469D60`, `sub_46ADC0` | Target offset not in any section data chunk |
| `"unexpected NVRS"` | Fatal | `sub_469D60` | `sub_468760` returned 0 (unrecognized action type) |
| `"PC relative branch address should be in the same section"` | Fatal | `sub_469D60` | PC-relative relocation crosses section boundary |
| `"symbol never allocated"` | Fatal | `sub_46ADC0` | Symbol value is `-1` during resolved-rela emission |
| `"relocation is past end of offset"` | Fatal | `sub_46ADC0` | Relocation offset exceeds section data size |
| `"rela section never allocated"` | Fatal | `sub_46ADC0` | Could not find or create `.nv.resolvedrela` section |

## Diagnostic Traces

All traces are gated by `(ctx->verbose_flags & 4) != 0` (bit 2 of the debug flags at `ctx+64`):

| Trace string | When emitted |
|---|---|
| `"resolve reloc %d for sym=%d+%lld at <section=%d,offset=%llx>\n"` | Per-relocation resolution trace |
| `"change alias reloc %s to %s\n"` | Weak alias chain followed to canonical symbol |
| `"ignore reloc on dead func %s\n"` | Relocation dropped because target function is dead |
| `"replace unified reloc %d with %d\n"` | Unified table relocation type remapped to base |
| `"ignore reloc on UFT_OFFSET\n"` | UFT\_OFFSET relocation dropped when UDT mode inactive |
| `"Ignoring the reloc to convert YIELD to NOP due to forward progress requirement.\n"` | YIELD conversion suppressed |

## Function Map

| Address | Size | Identity | Role |
|---|---|---|---|
| `0x469D60` | 26,578 B | `apply_relocations` | Main relocation phase; iterates linked list, calls engine |
| `0x468760` | 14,322 B | `reloc_apply_engine` | Descriptor-driven action dispatcher; 31 action opcodes across 11 case-body branches |
| `0x468670` | ~240 B | `bitfield_extract` | Extracts arbitrary bit field from instruction word(s) |
| `0x4685B0` | ~240 B | `bitfield_write` | Writes value into arbitrary bit field in instruction word(s) |
| `0x46ADC0` | 11,515 B | `emit_resolved_rela` | Writes `.nv.resolvedrela` sections for preserve-relocs |
| `0x445000` | 55,681 B | `finalize_elf` | Finalization phase; second relocation pass using vtable |
| `0x459640` | 16,109 B | `reloc_vtable_create` | Per-arch relocation handler vtable (used by finalization) |
| `0x42F690` | ~256 B | `reloc_max_type` | Returns maximum relocation type for current architecture |
| `0x42F6C0` | ~512 B | `reloc_validate` | Validates relocation type and selects descriptor table |
| `0x444720` | ~2 KB | `sym_remap_index` | Remaps symbol index for output ELF numbering |
| `0x440590` | ~2 KB | `sym_idx_to_record` | Symbol index to record pointer accessor |
| `0x440350` | ~2 KB | `sym_get_section` | Gets section index containing a symbol |
| `0x442270` | ~2 KB | `sec_idx_to_record` | Section index to record pointer accessor |
| `0x442760` | ~2 KB | `sec_find_or_create_rela` | Finds or creates `.rela` section for target |
| `0x4336B0` | ~2 KB | `section_write_data` | Writes bytes into section data buffer |
| `0x4411D0` | ~2 KB | `section_find_by_name` | Finds section by name string |
| `0x441AC0` | ~2 KB | `section_create` | Creates a new section with given attributes |
| `0x4343C0` | ~2 KB | `section_append_data` | Appends data to section buffer at offset |
| `0x463660` | ~2 KB | `uft_get_offset` | UFT/UDT offset resolver |
| `0x4644C0` | ~1 KB | `list_append` | Appends node to singly-linked list |
| `0x431000` | ~1 KB | `arena_free` | Frees an arena-allocated block |
| `0x467460` | ~2 KB | `error_emit` | Variadic error emission |

## Cross-References

- [Relocation Phase](../pipeline/relocate.md) -- Pipeline context: the 10-step resolution algorithm, alias chains, dead function filtering, unified remapping, and 3 worked examples showing complete before/after hex dumps
- [Finalization Phase](../pipeline/finalize.md) -- Second relocation pass using per-arch vtable dispatch
- [R\_CUDA Relocations](r-cuda-relocations.md) -- CUDA-specific relocation type catalog with all 119 type names and their descriptor entries
- [Unified Function Tables](../elf/uft.md) -- UFT/UDT structures referenced by unified relocations
- [Symbol Resolution](symbol-resolution.md) -- How symbols are resolved before relocation
- [Bindless Relocations](bindless-relocations.md) -- Bindless texture/surface relocation handling using the `.nv.rel.action` section

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_468760` at 0x468760, 10-parameter signature | **HIGH** | Decompiled `sub_468760_0x468760.c` confirms address and 10-parameter function signature |
| `sub_469D60` at 0x469D60, 26,578 bytes | **HIGH** | Decompiled `sub_469D60_0x469d60.c` confirms address, 985 lines |
| `sub_468670` (bitfield\_extract) at 0x468670 with recursive multi-word support | **HIGH** | Decompiled `sub_468670_0x468670.c`: recursive calls at lines 24, 29-30; shift pattern `<< (64 - v5) >> (64 - a3)` confirmed |
| `sub_4685B0` (bitfield\_write) uses mask `(-1ULL << (64-W)) >> (64-(O+W))` | **HIGH** | Decompiled `sub_4685B0_0x4685b0.c` line 35-36: exact mask formula confirmed |
| Relocation record loaded via `_mm_loadu_si128` (2x 128-bit) | **HIGH** | Decompiled `sub_469D60` lines 236-237: `v162 = _mm_loadu_si128(v5)`, `v163 = _mm_loadu_si128(v5 + 1)` |
| Descriptor table indexed by `type_index << 6` (64 bytes per entry) | **HIGH** | Decompiled `sub_468760` line 126: `v12 = a1 + ((unsigned __int64)a2 << 6)` |
| Action pointer starts at descriptor+12, sentinel at +60 | **HIGH** | Decompiled `sub_468760` lines 130-132: `v15 = (unsigned int *)(v12 + 12)`, `v100 = (unsigned int *)(v12 + 60)` |
| Each action is 16 bytes (4 x uint32), advance by `v15 += 4` | **HIGH** | All case bodies in `sub_468760` advance with `v15 += 4` |
| Fast path: `bit_offset==0 && bit_width==64` for direct word write | **HIGH** | Decompiled line 145: `if ( *v15 || v19 != 64 )` guards the fast path |
| CUDA table at `off_1D3DBE0`, Mercury table at `off_1D3CBE0` | **HIGH** | Decompiled `sub_469D60` lines 202, 214: both addresses confirmed |
| Mercury type normalization: `type -= 0x10000` | **HIGH** | Decompiled `sub_469D60` line 203: `v148 = v9 - 0x10000` |
| 31 action codes in switch statement (11 distinct case bodies, 5 alias values) | **HIGH** | Exhaustive enumeration from decompiled `sub_468760` switch cases: {0,1,6,7,8,9,A,B,10,12,13,14,16-1D,2E,2F-36,37,38} |
| Masked-shift tables at `xmmword_1D3F8E0`--`xmmword_1D3F930` | **HIGH** | Decompiled `sub_468760` lines 123-131, 518-526: all six SSE vector addresses confirmed |
| `.nv.rel.action` section with sh\_type `0x7000000B` | **HIGH** | Decompiled `sub_469D60` line 913: `sub_441AC0(v2, ".nv.rel.action", 1879048203, ...)` where 1879048203 = 0x7000000B |
| Chunk-list walk for section data at section record offset +72 | **HIGH** | Decompiled `sub_469D60` line 522: `v107 = *(_QWORD **)(v53 + 72)` |
| `"unexpected NVRS"` on engine failure | **HIGH** | Decompiled `sub_469D60` line 436: `sub_467460(dword_2A5B990, "unexpected NVRS")` |
| `"reloc address not found"` error | **HIGH** | Decompiled `sub_469D60` line 547: `sub_467460(dword_2A5B990, "reloc address not found")` |
| `"PC relative branch address should be in the same section"` | **HIGH** | Decompiled `sub_469D60` line 410 |
| Preserve-relocs list at `ctx+384` | **MEDIUM** | Decompiled `sub_469D60` line 454: `sub_4644C0((__int64)v5, (pthread_mutexattr_t *)(v2 + 384))` |
| Resolved-rela emitter dual-list processing (ctx+376, ctx+384) | **MEDIUM** | Inferred from `sub_46ADC0` decompiled analysis; offsets consistent with linker context |
| Action type names (ABS\_FULL, ABS\_LO, etc.) | **MEDIUM** | Names are editorial labels based on observed behavior; the binary does not contain symbolic names for action codes |
| `.nv.rel.action` compact format (8 bytes per type) | **MEDIUM** | Reconstructed from decompiled `sub_469D60` lines 913--979; field assignments inferred from pointer arithmetic |
