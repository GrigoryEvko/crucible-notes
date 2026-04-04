# Relocation Application Engine

The relocation application engine is the bit-level instruction patching core of nvlink's relocation pipeline. Its primary function `sub_468760` (14,322 bytes, 582 decompiled lines at `0x468760`) receives a descriptor-table entry for a given relocation type and a pointer into the section data buffer, then modifies specific bit fields of one or more 64-bit words to encode the resolved relocation value. Two helper functions handle the bit-field arithmetic: `sub_468670` (extraction) and `sub_4685B0` (insertion). A companion function `sub_46ADC0` (11,515 bytes at `0x46ADC0`) handles post-relocation output when `--preserve-relocs` is active, writing `.nv.resolvedrela` sections with remapped symbol indices and extracted bit-field addends.

This page documents the engine's internal structure in reimplementation-grade detail. For the surrounding pipeline context -- how the relocation linked list is walked, how symbol resolution feeds into the engine, and how the descriptor table is selected -- see [Relocation Phase](../pipeline/relocate.md).

## Key Facts

| Property | Value |
|---|---|
| Application engine | `sub_468760` at `0x468760` (14,322 bytes) |
| Bit-field extractor | `sub_468670` at `0x468670` (~240 bytes) |
| Bit-field writer | `sub_4685B0` at `0x4685B0` (~240 bytes) |
| Resolved-rela emitter | `sub_46ADC0` at `0x46ADC0` (11,515 bytes) |
| Called by | `sub_469D60` (apply\_relocations) during the "relocate" phase |
| CUDA descriptor table | `off_1D3DBE0` -- indexed by raw R\_CUDA type |
| Mercury descriptor table | `off_1D3CBE0` -- indexed by R\_MERCURY type minus `0x10000` |
| Descriptor entry size | 64 bytes (4 actions of 16 bytes each) |
| Maximum bit-field width | 128 bits (spans up to three 64-bit words) |

## Application Engine: sub_468760

### Signature

```c
int reloc_apply_engine(
    void*          descriptor_table,   // a1: off_1D3CBE0 or off_1D3DBE0
    uint32_t       reloc_type_index,   // a2: normalized type index into table
    bool           is_absolute,        // a3: 1 if symbol has absolute address
    uint64_t*      patch_ptr,          // a4: pointer into section data (instruction word)
    int64_t        extra_offset,       // a5: reloc_record->extra field
    int            section_offset,     // a6: addend / section base offset
    uint64_t       symbol_value,       // a7: resolved symbol address
    uint32_t       symbol_size,        // a8: symbol st_size
    uint32_t       section_type_delta, // a9: section_type - 0x6FFFFF84
    int64_t*       output_value        // a10: receives computed original value
);
// Returns 1 on success, 0 on unrecognized action type.
```

### Value Computation

Before the action loop begins, the engine computes the **relocation value** `v10`:

```c
uint64_t value = symbol_value;      // a7
if (is_absolute)
    value = symbol_value + extra_offset;  // a7 + a5
```

When `is_absolute` is false (the common case for relative/addend-based relocations), `value` starts as the raw symbol address. Each action type can further transform this value before writing it to the instruction word.

The engine also zeroes `*output_value` at entry. Actions that extract the existing bit-field from the instruction word store the extracted value there, making it available to the caller for preserve-relocs processing.

### Descriptor Table Layout

Each descriptor entry is located at `descriptor_table + (reloc_type_index << 6)`, yielding a 64-byte record. The first 12 bytes are a header (unused by the engine). The remaining 48 bytes hold up to three actions at offsets `+12`, `+28`, and `+44`, with a sentinel end marker at offset `+60`:

```
Offset   Field
------   -----
+0       (header, 12 bytes, skipped by engine)
+12      action[0].bit_offset     (uint32)
+16      action[0].bit_width      (uint32)
+20      action[0].action_type    (uint32)
+24      action[0].reserved       (uint32)
+28      action[1].bit_offset
+32      action[1].bit_width
+36      action[1].action_type
+40      action[1].reserved
+44      action[2].bit_offset
+48      action[2].bit_width
+52      action[2].action_type
+56      action[2].reserved
+60      sentinel (checked as end of action array)
```

The engine reads `v15` as a pointer that starts at offset `+12` (the first action) and advances by 4 `uint32` slots (16 bytes) per action. The loop terminates when `v15` reaches the sentinel at offset `+60` or when `action_type == 0`.

### Action Type Switch

The engine's core is a `while(2)` loop with a `switch` on `action_type`. Each case computes a value, optionally extracts the old field, and writes the new value into the target bit field. The following table summarizes all action codes:

| Code(s) | Name | Value computation | Notes |
|---|---|---|---|
| 0 | END | None | Advance to next action; terminate if at sentinel |
| 1, 0x12, 0x2E | ABS\_FULL | `value` (unchanged) | Standard absolute write. Special case: if `bit_offset==0` and `bit_width==64`, writes `value` directly to the 64-bit word instead of using bit-field logic |
| 6, 0x37 | ABS\_LO | Low 32 bits of `value` (`value & 0xFFFFFFFF`) | Extract low word; written through the general multi-word bit-field path |
| 7, 0x38 | ABS\_HI | High 32 bits of `value` (`value >> 32`) | Extract high word; written through the general multi-word bit-field path |
| 8 | PC\_REL\_SIZE | `extracted_old + symbol_size` | PC-relative plus symbol size; when `is_absolute`, uses `extra_offset + symbol_size` |
| 9 | SHIFTED\_2 | `value >> 2` | Right-shift by 2 for 4-byte-aligned addresses |
| 0xA | SEC\_TYPE\_LO | `section_type_delta & mask` where `mask = 255 >> (8 - bit_width)` | Encodes low bits of the section type offset |
| 0xB | SEC\_TYPE\_HI | `(section_type_delta >> 4) & mask` | Encodes high bits of the section type offset, shifted right by 4 |
| 0x10 | PC\_REL | `(int32_t)value - section_offset` | PC-relative: sign-extends value to 32-bit then subtracts the section offset |
| 0x13, 0x14 | CLEAR | Zero | Clears the target bit field to all-zeros |
| 0x16--0x1D, 0x2F--0x36 | MASKED\_SHIFT | `(value & mask_table[code-22]) >> shift_table[code-22]` | Table-driven mask-and-shift; 16 entries loaded from `xmmword_1D3F8E0`..`xmmword_1D3F930` as pairs of (mask, shift) indexed by `action_type - 22` |
| Other | ERROR | -- | Returns 0 (failure); caller emits `"unexpected NVRS"` |

### Actions 0x16--0x36: Table-Driven Masked Shift

Cases 0x16 through 0x1D and 0x2F through 0x36 share a single code path that uses two parallel lookup tables stored in SSE constants:

- **Mask table** (`v119[]`): 8 packed `uint64_t` values loaded from `xmmword_1D3F8E0`, `xmmword_1D3F8F0`, `xmmword_1D3F900`, `xmmword_1D3F910`. These are bitmasks applied to the relocation value before shifting.
- **Shift table** (`v118[]`): 8 packed `uint32_t` values loaded from `xmmword_1D3F920`, `xmmword_1D3F930`. These are right-shift amounts applied after masking.

The index into both tables is `action_type - 22`. The computation is:

```c
uint64_t mask  = mask_table[action_type - 22];
uint32_t shift = shift_table[action_type - 22];
value = (value & mask) >> shift;
```

This supports extraction of arbitrary byte lanes, half-words, or other sub-fields from a wide relocation value before writing to the target bit field. The 16 table slots cover the range of action codes 0x16--0x1D (8 codes) plus 0x2F--0x36 (8 codes), mapping to indices 0--7 and 25--32 respectively after subtracting 22. The gap between 0x1E and 0x2E is handled by other dedicated cases or falls to the default error path.

### Action 1/0x12/0x2E: Fast Path for Full-Width Writes

The most common action type has a fast path: when `bit_offset == 0` and `bit_width == 64`, the engine bypasses all bit-field logic and writes `value` directly to `*patch_ptr`:

```c
if (bit_offset == 0 && bit_width == 64) {
    if (!is_absolute) {
        *output_value = *patch_ptr;
        value += *patch_ptr;
    }
    *patch_ptr = value;
    return 1;
}
```

This handles the common case of a 64-bit absolute relocation targeting a full instruction word or data pointer. When `is_absolute` is false (relative mode), the existing word value is read first, added to the relocation value, and stored back -- the standard `S + A` (symbol + addend) pattern.

For narrower bit fields, the standard extraction/insertion path is taken.

## Bit-Field Extractor: sub_468670

The extractor reads an arbitrary bit field from an array of 64-bit words. It is called by the application engine in non-absolute mode to recover the existing instruction value before the relocation is applied.

### Signature

```c
int64_t bitfield_extract(
    uint64_t*  words,       // a1: pointer to instruction data
    int        bit_offset,  // a2: starting bit position
    int        bit_width    // a3: number of bits to extract
);
```

### Algorithm

1. **Word selection**: If `bit_offset >= 64`, advance the pointer by `bit_offset / 64` words and reduce `bit_offset` to `bit_offset % 64`.

2. **Single-word case**: If `bit_offset + bit_width <= 64`, extract inline:
   ```c
   return *words << (64 - (bit_offset + bit_width)) >> (64 - bit_width);
   ```
   This left-shifts to drop the higher bits above the field, then right-shifts to align the field to bit 0.

3. **Multi-word case**: If the field spans two or three 64-bit words, the function calls itself recursively:
   ```c
   low_part  = bitfield_extract(words, bit_offset, 64 - bit_offset);
   if (total_bits - 64 > 64) {
       // Spans 3 words (field > 128 bits impossible, but handles up to 192)
       mid_part  = bitfield_extract(words + 1, 0, 64);
       high_part = bitfield_extract(words + 2, 0, total_bits - 128);
   } else {
       // Spans 2 words
       high_part = words[1] << (128 - total_bits) >> (64 - (total_bits - 64));
   }
   return low_part | (high_part << (64 - bit_offset));
   ```

The recursion depth is bounded at 2 (for the three-word case) because GPU instructions are at most 128 bits wide, and the maximum practical field width is 128 bits. The two-word path is the common case: a field that straddles a 64-bit boundary within a 128-bit instruction.

## Bit-Field Writer: sub_4685B0

The writer inserts a value into an arbitrary bit field across one or more 64-bit words. It is the inverse of the extractor.

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

1. **Word selection**: Same normalization as the extractor -- advance pointer by `bit_offset / 64` words, reduce offset modulo 64.

2. **Multi-word case**: If `bit_offset + bit_width > 64`, the function iterates through intermediate words:
   ```c
   while (words != end_word) {
       *words = (*words & ~(-1ULL << bit_offset)) | (value << bit_offset);
       value >>= (64 - bit_offset);
       bit_offset = 0;
       words++;
   }
   remaining_width = adjusted_remaining;
   ```
   Each intermediate word receives the low bits of `value` starting at `bit_offset`, then `value` is shifted right by the consumed bits. After the loop, `bit_offset` resets to 0 for subsequent words.

3. **Final word**: The last (or only) word is patched using a read-modify-write with masks:
   ```c
   uint64_t mask = (-1ULL << (64 - bit_width)) >> (64 - (bit_offset + bit_width));
   *words = (*words & ~mask) | ((value << (64 - bit_width)) >> (64 - (bit_offset + bit_width)));
   ```
   This constructs a mask with `bit_width` ones positioned at `bit_offset`, clears those bits in the target word, and ORs in the value positioned at the same location.

### Mask Construction Detail

The mask formula `(-1ULL << (64 - W)) >> (64 - (O + W))` works as follows:
- `(-1ULL << (64 - W))` creates `W` ones in the top bits: e.g., for W=8, `0xFF00000000000000`
- `>> (64 - (O + W))` shifts the ones down so the lowest one lands at position O

The value insertion `(value << (64 - W)) >> (64 - (O + W))` performs the corresponding alignment of the value bits into the mask position.

## Application Engine Integration with Action Loop

For all action types except the full-width fast path, the application engine follows a common pattern after computing the value:

1. **Normalize bit\_offset**: If `bit_offset > 63`, compute `word_advance = bit_offset / 64` and `local_offset = bit_offset % 64`, advance `patch_ptr` by `word_advance`.

2. **Check span**: If `local_offset + bit_width <= 64`, it is a single-word patch. Otherwise, multi-word.

3. **Multi-word loop**: Call `sub_4685B0` for each intermediate 64-bit word, shifting `value` right by the consumed bits after each word.

4. **Final word**: Apply the mask-and-insert formula to the last word.

5. **Advance**: Move `v15` to the next action (16 bytes forward). If at the sentinel (`v100`), return 1. Otherwise continue the loop.

The engine inlines this pattern rather than always delegating to `sub_4685B0`. For the final word of a multi-word span, and for all single-word patches, the engine performs the read-modify-write directly in the switch body. `sub_4685B0` is called only for intermediate words in multi-word spans.

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

The first loop walks the relocation list at `ctx+376`. This is the main resolved-relocation list, containing entries that were applied during the relocation phase but retained for output. For each entry:

1. **ELF class check**: Reads `ctx+4` (ELF class byte) and `ctx+16` (link type). Class 1 = ELF32-style relocations; class 2 = RELA-style.

2. **Symbol addend resolution**: If the entry's symbol addend index (at record offset `+28`) is nonzero:
   - Calls `sub_444720` to remap the symbol index from internal to output numbering.
   - Calls `sub_440590` to look up the symbol record.
   - Validates that the symbol's resolved value (at symbol offset `+8`) is not `-1`. If it is, emits the fatal error `"symbol never allocated"`.
   - Adds the resolved symbol value to the record's addend (at record offset `+0`).

3. **Section lookup**: Calls `sub_442270` twice -- once for the target section (record offset `+24`), once for its parent section (at section offset `+44`).

4. **Offset validation**: The parent section's data size (at offset `+32`) must be nonzero and must be greater than the relocation's target offset (record offset `+0`). If the offset exceeds the size: `"relocation is past end of offset"`.

5. **Descriptor-driven bit-field extraction**: When `ctx+89` is set and the section type is not 4 (SHT\_RELA), the function selects the appropriate descriptor table (Mercury vs. CUDA, same dual-table logic as the main engine) and performs up to three rounds of bit-field extraction from the already-patched instruction data:
   - Reads the descriptor entry at the relocation type's offset.
   - For each of three field specifications at descriptor offsets `(+3,+4,+5)`, `(+7,+8,+9)`, and `(+11,+12,+13)` (in uint32 units): if the "present" flag (descriptor field `+5`, `+9`, or `+13`) is nonzero, extracts a bit field using `sub_468670` and accumulates it into the record's extra addend at offset `+16`.
   - This recovers the instruction-encoded portions of the relocation value after patching, so the output `.nv.resolvedrela` record carries the full resolved addend for re-application.

6. **Section data location**: Same chunk-list walk as the main engine. The section record at offset `+72` holds a linked list of data chunks. Each chunk node has: `[0]` next pointer, `[1]` data pointer. The data structure has: `[0]` buffer pointer, `[1]` base offset, `[3]` size. The function searches for the chunk containing the target offset. On failure: `"reloc address not found"`.

7. **Symbol index remapping**: Calls `sub_444720` to remap the relocation's symbol index (record offset `+12`) from internal numbering to output ELF `.symtab` numbering.

8. **Rela section creation**: Calls `sub_442760` to find or create the `.rela` output section for the target section. On failure: `"rela section never allocated"`.

9. **Output record writing**: The format depends on ELF class:
   - **RELA (class == 2)**: Calls `sub_4336B0` with a 24-byte record (8-byte offset, 8-byte info, 8-byte addend) at the record pointer, writing 8 bytes starting at record offset `+0` for a total of 24 bytes.
   - **REL (class != 2)**: Packs the symbol index and type into a compact format: `info = (sym_index << 8) + (type & 0xFF)`. Writes the offset (from record offset `+16`) into record offset `+8`. Calls `sub_4336B0` with 12 bytes at record offset `+4`.

### Secondary List: ctx+384

When `ctx+85` is set (preserve-relocs), a second loop processes the list at `ctx+384`. This list contains relocations that are candidates for the `.nv.resolvedrela` output. The selection criteria are:

1. The parent section's data size must be nonzero (section has allocated data).
2. The architecture flag check passes (Mercury vs. CUDA path).
3. The symbol must satisfy: section type == 1 (with bit `0x04` set in the section flags at offset `+8`), the symbol info low nibble == 13, and the symbol binding field (`+5`) masked with `0xE0` equals 64.

For qualifying entries, the function:
- Looks up the section name from the parent section record (at offset `+96`).
- Constructs the output section name by prepending `".nv.resolvedrela"` to the section name via `sprintf`.
- Calls `sub_4411D0` to find or create that section by name.
- Writes the relocation record in the same REL/RELA format as the primary list.

The section name is cached: if two consecutive relocations target the same section, the `sprintf` and `sub_4411D0` calls are skipped and the previous section index is reused.

## Worked Example: Patching a 24-bit Immediate at Bit Offset 20

Consider a SASS instruction word at `patch_ptr` with a 24-bit immediate field starting at bit 20. The descriptor action specifies `bit_offset=20`, `bit_width=24`, `action_type=1` (ABS\_FULL). The resolved symbol value is `0x1A2B3C`.

1. **Normalize**: `bit_offset=20 < 64`, so `words = patch_ptr`, `local_offset = 20`.

2. **Span check**: `20 + 24 = 44 <= 64`, single-word case.

3. **Extract old value** (if not absolute):
   ```
   old = *words << (64 - 44) >> (64 - 24)
       = *words << 20 >> 40
   ```
   This isolates the 24 bits at positions 20..43.

4. **Compute new value**: `value = symbol_value + old` (for addend relocation).

5. **Build mask**: `mask = (-1ULL << 40) >> (64 - 44) = (-1ULL << 40) >> 20 = 0x00000FFFFFF00000`.

6. **Write**: `*words = (*words & ~mask) | ((value << 40) >> 20)`.

For a 128-bit SASS instruction where a field straddles the 64-bit boundary (e.g., `bit_offset=56`, `bit_width=32`), the engine would:
- Write the low 8 bits (64 - 56 = 8) into the first word via `sub_4685B0`.
- Write the remaining 24 bits starting at offset 0 of the next word using the final-word mask formula.

## Error Conditions

| Error string | Severity | Source function | Condition |
|---|---|---|---|
| `"symbol never allocated"` | Fatal | `sub_46ADC0` | Symbol value is `-1` during resolved-rela emission |
| `"relocation is past end of offset"` | Fatal | `sub_46ADC0` | Relocation offset exceeds section data size |
| `"rela section never allocated"` | Fatal | `sub_46ADC0` | Could not find or create `.nv.resolvedrela` section |
| `"unexpected reloc"` | Fatal | `sub_46ADC0` | Relocation type is <= 0x10000 in Mercury mode (invalid normalization) |
| `"reloc address not found"` | Fatal | `sub_46ADC0` | Target offset not in any section data chunk |
| `"unexpected NVRS"` | Fatal | `sub_469D60` | `sub_468760` returned 0 (unrecognized action type) |

## Function Map

| Address | Size | Identity | Role |
|---|---|---|---|
| `0x468760` | 14,322 B | `reloc_apply_engine` | Descriptor-driven bit-field patching engine |
| `0x468670` | ~240 B | `bitfield_extract` | Extracts arbitrary bit field from instruction word(s) |
| `0x4685B0` | ~240 B | `bitfield_write` | Writes value into arbitrary bit field in instruction word(s) |
| `0x46ADC0` | 11,515 B | `emit_resolved_rela` | Writes `.nv.resolvedrela` sections for preserve-relocs |
| `0x469D60` | 26,578 B | `apply_relocations` | Main relocation phase; calls the engine |
| `0x445000` | 55,681 B | `finalize_elf` | Finalization phase; uses vtable instead of engine |
| `0x459640` | 16,109 B | `reloc_vtable_create` | Per-arch relocation handler vtable (used by finalization) |
| `0x444720` | ~2 KB | `sym_remap_index` | Remaps symbol index for output ELF numbering |
| `0x440590` | ~2 KB | `sym_idx_to_record` | Symbol index to record pointer accessor |
| `0x442270` | ~2 KB | `sec_idx_to_record` | Section index to record pointer accessor |
| `0x442760` | ~2 KB | `sec_find_or_create_rela` | Finds or creates `.rela` section for target |
| `0x4336B0` | ~2 KB | `section_write_data` | Writes bytes into section data buffer |
| `0x4411D0` | ~2 KB | `section_find_by_name` | Finds section by name string |
| `0x467460` | ~2 KB | `error_emit` | Variadic error emission |

## Cross-References

- [Relocation Phase](../pipeline/relocate.md) -- Pipeline context: linked-list walk, symbol resolution, descriptor table selection
- [Finalization Phase](../pipeline/finalize.md) -- Second relocation pass using per-arch vtable dispatch
- [R\_CUDA Relocations](r-cuda-relocations.md) -- CUDA-specific relocation type catalog
- [Unified Function Tables](../elf/uft.md) -- UFT/UDT structures referenced by unified relocations
- [Symbol Resolution](symbol-resolution.md) -- How symbols are resolved before relocation
- [Bindless Relocations](bindless-relocations.md) -- Bindless texture/surface relocation handling
