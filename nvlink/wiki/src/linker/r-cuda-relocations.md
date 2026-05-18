# R_CUDA Relocations

nvlink defines 119 CUDA-specific ELF relocation types for the `EM_CUDA` (190) machine type. These types are stored in `.rela.*` sections of device ELF (cubin) files and are consumed by the relocation engine during the link phase. Each type encodes how a resolved symbol address is patched into the instruction stream or data section: the bit-field width, the bit-field position within the 64- or 128-bit instruction word, and the computation to perform (absolute, PC-relative, hi/lo split, etc.).

The types are organized into two global descriptor tables baked into the nvlink binary's `.rodata` segment. A validation/dispatch function at `sub_42F6C0` selects between them based on whether the relocation is a standard code/data relocation or a section-attribute relocation. The application engine at `sub_468760` reads 64-byte per-type descriptors from these tables and performs up to three sequential bit-field patching actions per relocation.

## Key Facts

| Property | Value |
|---|---|
| Machine type | `EM_CUDA` (190) |
| Total unique type names | 119 |
| Standard relocation table | `off_1D37600` (117 entries, index 0--116) |
| Attribute relocation table | `off_1D371E0` (65 entries, index 0--64) |
| Attribute type offset | `0x10000` (attribute type = standard type + 65536) |
| Descriptor size | 64 bytes per type (12-byte header + 3 actions x 16 bytes + 4-byte sentinel) |
| Validation function | `sub_42F6C0` at `0x42F6C0` |
| Architecture class function | `sub_42F8C0` at `0x42F8C0` |
| Max-type-for-class function | `sub_42F690` at `0x42F690` |
| Application engine | `sub_468760` at `0x468760` (14,322 bytes) |
| CUDA descriptor table | `off_1D3DBE0` (used by relocation engine) |
| Mercury descriptor table | `off_1D3CBE0` (Mercury types are CUDA types + `0x10000`) |

## Naming Convention

Every R_CUDA type name follows a systematic pattern:

```
R_CUDA_<category><bits>_<bitposition>
```

The components are:

- **Category**: the semantic class of the relocation (ABS, G, FUNC_DESC, TEX, etc.)
- **Bits**: the width of the relocated value in bits (8, 16, 19, 20, 21, 22, 24, 32, 47, 55, 56, 64, 128)
- **Bit position**: the starting bit offset within the instruction word where the value is inserted

For example, `R_CUDA_ABS32_20` means: patch bits [20:52) of the instruction word with a 32-bit absolute address. `R_CUDA_PCREL_IMM24_23` means: compute a PC-relative offset, take 24 bits, and insert starting at bit 23.

Some types use a compound suffix with `_HI` or `_LO` to indicate which half of a 32-bit value is being patched (high 16 bits or low 16 bits).

## Dual Descriptor Tables

### Standard Table (off_1D37600)

The standard relocation table at `off_1D37600` contains 117 entries (indices 0 through 116, validated against limit `0x75` = 117). Each entry is a pointer pair in the table: the first pointer is the type name string (e.g., `"R_CUDA_ABS32_20"`), and additional fields encode the relocation class and architecture compatibility.

The validation function `sub_42F6C0` checks:

```c
// Standard relocation path
if (!is_attribute) {
    if (type_index >= 117)       // limit 0x75
        error("unknown attribute");
    entry = &off_1D37600[2 * type_index];
    if (entry->arch_class > target_class)
        warning("Relocation %s not supported on %s", entry->name, class_name);
}
```

### Attribute Table (off_1D371E0)

The attribute relocation table at `off_1D371E0` contains 65 entries (indices 0 through 64, validated against limit `0x41` = 65). Attribute relocations are identified by having their type encoded with the `0x10000` offset -- when the relocation engine encounters a type >= `0x10000`, it subtracts `0x10000` and uses this table instead.

```c
// Attribute relocation path (type >= 0x10000)
type_index -= 0x10000;
if (type_index >= 65)            // limit 0x41
    error("unknown attribute");
entry = &off_1D371E0[2 * type_index];
```

Attribute relocations apply to `.nv.info.*` attribute sections rather than to instruction streams. A separate validation function at `sub_42F760` handles attribute-specific compatibility checking with a three-way dispatch based on the attribute usage field (`dword_1D37D68[4 * type + 1]`): value 0 = warning, value 1 = error, value 2 = silent ignore.

### Architecture Class System

The function at `sub_42F8C0` maps an SM version number to an architecture class used for relocation compatibility checking:

```c
int reloc_arch_class(int sm_version) {
    if (sm_version == 0)   return 0;   // invalid / unset
    if (sm_version <= 70)  return 1;   // Kepler through Volta (sm_30--sm_70)
    if (sm_version <= 72)  return 2;   // Volta extended (sm_72)
    if (sm_version >= 76)  return 5;   // Ampere+ (sm_80--sm_90+)
    return 3;                           // Turing (sm_75)
}
```

Each descriptor entry stores a minimum architecture class. The validation function compares the entry's class against the target to ensure the relocation type is supported on the architecture being linked. The five class names are stored in a string pointer array at `off_1D371A0` (indexed 0--4), used in error/warning messages.

The maximum valid relocation index varies by architecture class. The function `sub_42F690` scans backward from index 115 (the last non-special standard type) through the descriptor table, returning the first index whose architecture class is not 5 (the highest). This determines which types are valid for a given target.

## Descriptor Format

Each relocation type has a 64-byte descriptor in the application engine's table (`off_1D3DBE0` for CUDA, `off_1D3CBE0` for Mercury). The descriptor is divided into a 12-byte header followed by three 16-byte action slots and a 4-byte sentinel:

```
Descriptor (64 bytes total):
  +0   Header (12 bytes)
       +0   uint32_t  field_0;      // Used by resolved-rela emitter (sub_46ADC0)
       +4   uint32_t  field_1;      // Used by resolved-rela emitter
       +8   uint32_t  field_2;      // Used by resolved-rela emitter
  +12  Action 0 (16 bytes)
       +12  uint32_t  bit_offset;   // Starting bit position in instruction word
       +16  uint32_t  bit_width;    // Number of bits to patch
       +20  uint32_t  action_type;  // Operation code (see table below)
       +24  uint32_t  reserved;     // Padding / flags
  +28  Action 1 (16 bytes)
       +28  uint32_t  bit_offset;
       +32  uint32_t  bit_width;
       +36  uint32_t  action_type;
       +40  uint32_t  reserved;
  +44  Action 2 (16 bytes)
       +44  uint32_t  bit_offset;
       +48  uint32_t  bit_width;
       +52  uint32_t  action_type;
       +56  uint32_t  reserved;
  +60  Sentinel (4 bytes, marks end of action array)
```

The application engine (`sub_468760`) iterates the three action slots sequentially. Action type `0x00` terminates the sequence early (the engine skips to the next slot and terminates only when the slot pointer reaches the sentinel). The engine indexes into the descriptor table and positions its action pointer and sentinel:

```c
descriptor_base = table + (type_index << 6);  // type_index * 64
action_ptr = descriptor_base + 12;             // first action at byte offset +12
end_ptr    = descriptor_base + 60;             // sentinel at byte offset +60
```

The header fields at offsets +0, +4, +8 are not used by the application engine itself. They are consumed by the resolved-rela emitter (`sub_46ADC0`) during `--preserve-relocs` processing, where they specify up to three (present-flag, bit_offset, bit_width) triples for extracting the already-patched instruction fields back into addend records. The mapping is: descriptor `uint32` indices `[3,4,5]` = action 0's extraction spec, `[7,8,9]` = action 1's, `[11,12,13]` = action 2's -- where the third element of each triple is the "present" flag gating whether extraction occurs.

### Action Slot Processing Pseudocode

The core loop in `sub_468760` processes each action slot in order. The following pseudocode captures the complete dispatch:

```c
int reloc_apply_engine(
    void*      table,            // descriptor table base (off_1D3DBE0 or off_1D3CBE0)
    uint32_t   type_index,       // normalized relocation type index
    bool       is_absolute,      // true if symbol has absolute address
    uint64_t*  patch_ptr,        // pointer into section data (instruction words)
    int64_t    extra_offset,     // reloc record extra field
    int        section_offset,   // section base / PC address
    uint64_t   symbol_value,     // resolved symbol address (S)
    uint32_t   symbol_size,      // symbol st_size
    uint32_t   section_type,     // sh_type - 0x70000064 (SHT_CUDA_CONSTANT0 base)
    int64_t*   output_value      // receives extracted original value
) {
    // Compute initial relocation value
    uint64_t value = symbol_value;
    if (is_absolute)
        value = symbol_value + extra_offset;   // S + A

    uint8_t* desc = table + (type_index << 6);
    uint32_t* action = (uint32_t*)(desc + 12); // first action slot
    uint32_t* end    = (uint32_t*)(desc + 60); // sentinel
    *output_value = 0;

    while (action != end) {
        uint32_t bit_off  = action[0];
        uint32_t bit_wid  = action[1];
        uint32_t act_type = action[2];

        switch (act_type) {

        case 0x00: // END -- skip this slot, continue to next
            action += 4;
            break;

        case 0x01:  // ABS_FULL
        case 0x12:  // ABS_FULL (alias)
        case 0x2E:  // ABS_FULL (alias)
            // Fast path: full 64-bit word write
            if (bit_off == 0 && bit_wid == 64) {
                if (!is_absolute) {
                    *output_value = *patch_ptr;
                    value += *patch_ptr;
                }
                *patch_ptr = value;
                action += 4;
                break;
            }
            // Narrow field: extract old, add, write back
            if (!is_absolute) {
                int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
                value += old;
                *output_value = old;
            }
            action += 4;
            bitfield_write(patch_ptr, value, bit_off, bit_wid);
            break;

        case 0x06:  // ABS_LO -- low bits of (S + A)
        case 0x37:  // ABS_LO (alias)
            if (!is_absolute) {
                int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
                *output_value = old;
                value += old;
            }
            // Write low 64 bits of value
            bitfield_write(patch_ptr, (uint64_t)value, bit_off, bit_wid);
            action += 4;
            break;

        case 0x07:  // ABS_HI -- high 32 bits of (S + A)
        case 0x38:  // ABS_HI (alias)
            if (!is_absolute) {
                int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
                *output_value = old;
                value = (value >> 32) + old;
            } else {
                value = value >> 32;
            }
            bitfield_write(patch_ptr, value, bit_off, bit_wid);
            action += 4;
            break;

        case 0x08:  // ABS_SIZE -- overwrites running value with extra+size or extracted+size (no S)
            if (!is_absolute) {
                int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
                value = old + symbol_size;
                *output_value = old;
            } else {
                value = extra_offset + symbol_size;
            }
            bitfield_write(patch_ptr, value, bit_off, bit_wid);
            action += 4;
            break;

        case 0x09:  // SHIFTED_2 -- (S + A) >> 2
            value >>= 2;
            if (!is_absolute) {
                int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
                value += old;
                *output_value = old;
            }
            bitfield_write(patch_ptr, value, bit_off, bit_wid);
            action += 4;
            break;

        case 0x0A:  // SEC_TYPE_LO -- low nybble of section type delta
            value = section_type & (uint64_t)(0xFF >> (8 - bit_wid));
            if (!is_absolute)
                value += bitfield_extract(patch_ptr, bit_off, bit_wid);
            bitfield_write(patch_ptr, value, bit_off, bit_wid);
            action += 4;
            break;

        case 0x0B:  // SEC_TYPE_HI -- high nybble of section type delta
            value = (section_type >> 4) & (uint64_t)(0xFF >> (8 - bit_wid));
            if (!is_absolute)
                value += bitfield_extract(patch_ptr, bit_off, bit_wid);
            bitfield_write(patch_ptr, value, bit_off, bit_wid);
            action += 4;
            break;

        case 0x10:  // PC_REL -- (int32_t)(S + A) - PC
            if (!is_absolute) {
                int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
                value += old;
                *output_value = old;
            }
            value = (int32_t)value - section_offset;
            bitfield_write(patch_ptr, value, bit_off, bit_wid);
            action += 4;
            break;

        case 0x13:  // CLEAR -- zero the bit-field
        case 0x14:  // CLEAR (alias)
            bitfield_write(patch_ptr, 0, bit_off, bit_wid);
            action += 4;
            break;

        case 0x16: case 0x17: case 0x18: case 0x19:  // MASKED_SHIFT 0-3
        case 0x1A: case 0x1B: case 0x1C: case 0x1D:  // MASKED_SHIFT 4-7
        case 0x2F: case 0x30: case 0x31: case 0x32:  // MASKED_SHIFT 8-11
        case 0x33: case 0x34: case 0x35: case 0x36:  // MASKED_SHIFT 12-15
        {
            int idx = act_type - 22;
            if (!is_absolute) {
                int64_t old = bitfield_extract(patch_ptr, bit_off, bit_wid);
                value += old;
                *output_value = old;
            }
            value = (value & mask_table[idx]) >> shift_table[idx];
            bitfield_write(patch_ptr, value, bit_off, bit_wid);
            action += 4;
            break;
        }

        default:
            return 0;  // Unknown action -- caller emits "unexpected NVRS"
        }

        if (action == end)
            return 1;
    }
    return 1;
}
```

### Action Types

| Code | Name | Computation |
|---|---|---|
| `0x00` | **end** | Skip slot; terminate if at sentinel |
| `0x01` | **abs_full** | `S + A` -- full absolute, store all bits |
| `0x06` | **abs_lo** | `(S + A) & mask` -- low bits of absolute |
| `0x07` | **abs_hi** | `((S + A) >> 32) & mask` -- high 32 bits of absolute |
| `0x08` | **abs_size** | `extra + symbol_size` when `is_absolute`, else `extracted + symbol_size` -- overwrites the running value; no symbol address is mixed in |
| `0x09` | **abs_shifted** | `(S + A) >> 2` -- absolute right-shifted by 2 (4-byte aligned) |
| `0x0A` | **sec_type_lo** | `section_type & (0xFF >> (8 - width))` -- low nybble of section type |
| `0x0B` | **sec_type_hi** | `(section_type >> 4) & (0xFF >> (8 - width))` -- high nybble of section type |
| `0x10` | **pc_rel** | `(int32_t)(S + A) - PC` -- PC-relative offset |
| `0x12` | **abs_full** | Alias of `0x01` (same behavior) |
| `0x13` | **clear** | Zero the bit-field (write 0) |
| `0x14` | **clear** | Alias of `0x13` (same behavior) |
| `0x15` | **piece_cont** | Continuation slot for split-field types (`ABS55_16_34`, `ABS56_16_34`); pairs with a leading `0x09` slot to assemble a wide immediate from two bit ranges. Observed only in `slot1`. |
| `0x16`--`0x1D` | **masked_shift_0..7** | `(S + A) & mask_table[n] >> shift_table[n]` |
| `0x2E` | **abs_full** | Alias of `0x01` (same behavior, different encoding) |
| `0x2F`--`0x36` | **masked_shift_8..15** | `(S + A) & mask_table[n] >> shift_table[n]` |
| `0x37` | **abs_lo** | Alias of `0x06` |
| `0x38` | **abs_hi** | Alias of `0x07` |

The masked-shift actions (codes `0x16`--`0x1D` and `0x2F`--`0x36`) use a pair of SSE constant vectors loaded from `xmmword_1D3F8E0` through `xmmword_1D3F930`. These contain mask and shift values indexed by `(action_type - 22)`, enabling a single code path to handle 16 different extract-and-place patterns for multi-field instruction encodings.

### Bit-field Patching

The engine uses two helper functions for the actual bit manipulation:

- `sub_468670` (`bitfield_extract`): extracts the current value of a bit-field from the instruction word
- `sub_4685B0` (`bitfield_write`): splices a new value into a bit-field

Both handle fields that span 64-bit word boundaries. The instruction data is treated as an array of `uint64_t` words (little-endian). The general algorithms:

**Extraction** (`sub_468670`): Given a starting bit position and width, extract the field value:

```c
int64_t bitfield_extract(uint64_t* words, int bit_offset, int bit_width) {
    // Normalize: advance pointer past full 64-bit words
    if (bit_offset >= 64) {
        words += (bit_offset / 64);
        bit_offset = bit_offset % 64;
    }
    int end_bit = bit_offset + bit_width;

    // Single-word case: field fits within one uint64_t
    if (end_bit <= 64)
        return *words << (64 - end_bit) >> (64 - bit_width);

    // Multi-word case: recursive split across 64-bit boundary
    int64_t low = bitfield_extract(words, bit_offset, 64 - bit_offset);
    int64_t high;
    if (end_bit - 64 > 64) {
        // Three-word span (up to 192 bits, theoretical max)
        int64_t mid = bitfield_extract(words + 1, 0, 64);
        high = bitfield_extract(words + 2, 0, end_bit - 128);
    } else {
        // Two-word span (common case for 128-bit instructions)
        high = words[1] << (128 - end_bit) >> (64 - (end_bit - 64));
    }
    return low | (high << (64 - bit_offset));
}
```

**Insertion** (`sub_4685B0`): Given a value, starting bit position, and width, splice the value into the instruction words:

```c
void bitfield_write(uint64_t* words, uint64_t value, int bit_offset, int bit_width) {
    // Normalize: advance pointer past full 64-bit words
    if (bit_offset >= 64) {
        words += (bit_offset / 64);
        bit_offset = bit_offset % 64;
    }
    int end_bit = bit_offset + bit_width;

    // Multi-word case: iterate through intermediate words
    if (end_bit > 64) {
        uint64_t* end_word = words + ((end_bit - 65) / 64) + 1;
        int off = bit_offset;
        while (words != end_word) {
            // Preserve bits below bit_offset, fill above with value
            *words = (*words & ~(-1ULL << off)) | (value << off);
            value >>= (64 - off);
            off = 0;
            words++;
        }
        end_bit = end_bit - (((end_bit - 65) / 64) * 64) - 64;
        bit_width = end_bit;
    }

    // Final (or only) word: read-modify-write with constructed mask
    //   mask = bit_width ones positioned at [end_bit - bit_width, end_bit)
    uint64_t mask = (-1ULL << (64 - bit_width)) >> (64 - end_bit);
    *words = (*words & ~mask) | ((value << (64 - bit_width)) >> (64 - end_bit));
}
```

The mask formula `(-1ULL << (64 - W)) >> (64 - E)` where `E = offset + W` creates a window of `W` ones starting at bit position `E - W`. The value is aligned to the same position using an identical pair of shifts. This is a standard bit-field insertion idiom that avoids branching.

### Worked Example: R_CUDA_ABS32_26

`R_CUDA_ABS32_26` (standard table index 5) is a common relocation type that patches a 32-bit absolute address into a SASS instruction starting at bit 26. This section traces the complete path from relocation record to patched instruction.

**Scenario**: A `MOV` instruction at offset `0x100` in `.text` references a global symbol `_Z10my_kernelPi` resolved to address `0x0000_0000_DEAD_BEEF`. The `.rela.text` section contains:

```
Elf64_Rela {
    r_offset = 0x100,        // instruction offset within .text
    r_info   = (sym << 32) | 5,  // type = 5 (R_CUDA_ABS32_26)
    r_addend = 0              // no addend
}
```

**Step 1: Descriptor lookup.** The relocation engine selects the CUDA descriptor table at `off_1D3DBE0` and computes the descriptor address:

```
descriptor = off_1D3DBE0 + (5 << 6) = off_1D3DBE0 + 320
```

The 64-byte descriptor for type 5 contains (reconstructed from the type semantics):

```
Offset  Bytes (hex)                           Interpretation
------  -----------                           --------------
+0      xx xx xx xx xx xx xx xx xx xx xx xx   Header (12 bytes, used by sub_46ADC0)
+12     1A 00 00 00                           action[0].bit_offset  = 26
+16     20 00 00 00                           action[0].bit_width   = 32
+20     01 00 00 00                           action[0].action_type = 0x01 (ABS_FULL)
+24     00 00 00 00                           action[0].reserved    = 0
+28     00 00 00 00                           action[1].bit_offset  = 0
+32     00 00 00 00                           action[1].bit_width   = 0
+36     00 00 00 00                           action[1].action_type = 0x00 (END)
+40     00 00 00 00                           action[1].reserved    = 0
+44     00 00 00 00                           action[2].bit_offset  = 0
+48     00 00 00 00                           action[2].bit_width   = 0
+52     00 00 00 00                           action[2].action_type = 0x00 (END)
+56     00 00 00 00                           action[2].reserved    = 0
+60     xx xx xx xx                           Sentinel (4 bytes)
```

The engine positions its pointers: `action_ptr = descriptor + 12`, `end_ptr = descriptor + 60`.

**Step 2: Value computation.** The relocation is not absolute (`is_absolute = false`), so `value = symbol_value = 0xDEAD_BEEF`.

**Step 3: Action dispatch.** The engine reads action slot 0:
- `bit_offset = 26`
- `bit_width = 32`
- `action_type = 0x01` (ABS_FULL)

This is not the fast path (`bit_offset != 0 || bit_width != 64`), so the engine takes the narrow-field path.

**Step 4: Extract old value.** Since `is_absolute == false`, the engine extracts the existing 32-bit field from the instruction word. Assume the instruction word at `patch_ptr` is `0x0000_0000_0000_0000` (field pre-initialized to zero):

```
old = bitfield_extract(patch_ptr, 26, 32)
```

With `end_bit = 26 + 32 = 58 <= 64`, this is the single-word case:

```
old = *patch_ptr << (64 - 58) >> (64 - 32)
    = 0 << 6 >> 32
    = 0
```

The engine computes `value = value + old = 0xDEAD_BEEF + 0 = 0xDEAD_BEEF` and stores `*output_value = 0`.

**Step 5: Write new value.** The engine constructs a mask and writes the value into the instruction word. With `bit_offset = 26`, `bit_width = 32`, `end_bit = 58`:

```
mask = (-1ULL << (64 - 32)) >> (64 - 58)
     = 0xFFFF_FFFF_0000_0000 >> 6
     = 0x03FF_FFFF_FC00_0000

value_positioned = (0xDEAD_BEEF << (64 - 32)) >> (64 - 58)
                 = (0xDEAD_BEEF_0000_0000) >> 6
                 = 0x037A_B6FB_BC00_0000

*patch_ptr = (*patch_ptr & ~mask) | value_positioned
           = (0 & 0xFC00_0000_03FF_FFFF) | 0x037A_B6FB_BC00_0000
           = 0x037A_B6FB_BC00_0000
```

The 32 bits of `0xDEAD_BEEF` now occupy bits [26:58) of the instruction word:

```
Bit layout of patched instruction word:
  bits [63:58] = 0b000000       (unchanged)
  bits [57:26] = 0xDEAD_BEEF    (patched value)
  bits [25:0]  = 0x0000000      (unchanged)
```

**Step 6: Advance and terminate.** The action pointer advances by 16 bytes to action slot 1, which has `action_type = 0x00` (END). The engine skips to the next slot. The action pointer advances to offset +44 (action slot 2), which is also END. After advancing once more, `action_ptr == end_ptr` (offset +60), so the engine returns 1 (success).

**Multi-word variant**: If the relocation were `R_CUDA_ABS47_34` (47-bit field at bit offset 34), the field would span two 64-bit words: bits [34:64) in word 0 (30 bits) and bits [0:17) in word 1 (17 bits). The engine would call `bitfield_write` which iterates: first writing the low 30 bits into word 0 at offset 34, then shifting `value` right by 30 and writing the remaining 17 bits into word 1 at offset 0.

## Relocation Categories

The 119 types fall into 15 semantic categories based on their name prefix and purpose.

### No-op and Sentinel

| Index | Name | Description |
|---|---|---|
| 0 | `R_CUDA_NONE` | No relocation (placeholder / deleted) |
| 116 | `R_CUDA_NONE_LAST` | Sentinel marking end of valid type range |

### Full-Width Data Relocations

These apply to data sections (`.nv.global`, `.nv.constant*`, etc.) rather than instructions.

| Index | Name | Bits | Description |
|---|---|---|---|
| 1 | `R_CUDA_32` | 32 | 32-bit absolute address |
| 2 | `R_CUDA_32_HI` | 16 | Upper 16 bits of 32-bit address |
| 3 | `R_CUDA_32_LO` | 16 | Lower 16 bits of 32-bit address |
| 4 | `R_CUDA_64` | 64 | 64-bit absolute address |

### Byte-Level Relocations (R_CUDA_8_*)

Byte-granularity relocations for patching individual bytes within data structures, typically in descriptor tables or attribute sections.

| Index | Name | Byte offset | Description |
|---|---|---|---|
| 5 | `R_CUDA_8_0` | 0 | Byte at offset 0 |
| 6 | `R_CUDA_8_8` | 1 | Byte at offset 8 bits |
| 7 | `R_CUDA_8_16` | 2 | Byte at offset 16 bits |
| 8 | `R_CUDA_8_24` | 3 | Byte at offset 24 bits |
| 9 | `R_CUDA_8_32` | 4 | Byte at offset 32 bits |
| 10 | `R_CUDA_8_40` | 5 | Byte at offset 40 bits |
| 11 | `R_CUDA_8_48` | 6 | Byte at offset 48 bits |
| 12 | `R_CUDA_8_56` | 7 | Byte at offset 56 bits |

### Global Address Relocations (R_CUDA_G*)

Used for global memory address references. These are the primary relocations for `.nv.global` section symbols.

| Index | Name | Bits | Description |
|---|---|---|---|
| 13 | `R_CUDA_G32` | 32 | 32-bit global address |
| 14 | `R_CUDA_G64` | 64 | 64-bit global address |
| 15 | `R_CUDA_G8_0` | 8 | Global byte at offset 0 |
| 16 | `R_CUDA_G8_8` | 8 | Global byte at offset 8 |
| 17 | `R_CUDA_G8_16` | 8 | Global byte at offset 16 |
| 18 | `R_CUDA_G8_24` | 8 | Global byte at offset 24 |
| 19 | `R_CUDA_G8_32` | 8 | Global byte at offset 32 |
| 20 | `R_CUDA_G8_40` | 8 | Global byte at offset 40 |
| 21 | `R_CUDA_G8_48` | 8 | Global byte at offset 48 |
| 22 | `R_CUDA_G8_56` | 8 | Global byte at offset 56 |

### Absolute Instruction Relocations (R_CUDA_ABS*)

These patch absolute addresses into SASS instruction bit-fields. The first number is the bit-width of the value; the second is the starting bit position within the instruction word.

| Index | Name | Width | Bit pos | Description |
|---|---|---|---|---|
| 23 | `R_CUDA_ABS16_20` | 16 | 20 | 16-bit absolute at bit 20 |
| 24 | `R_CUDA_ABS16_23` | 16 | 23 | 16-bit absolute at bit 23 |
| 25 | `R_CUDA_ABS16_26` | 16 | 26 | 16-bit absolute at bit 26 |
| 26 | `R_CUDA_ABS16_32` | 16 | 32 | 16-bit absolute at bit 32 |
| 27 | `R_CUDA_ABS20_44` | 20 | 44 | 20-bit absolute at bit 44 |
| 28 | `R_CUDA_ABS24_20` | 24 | 20 | 24-bit absolute at bit 20 |
| 29 | `R_CUDA_ABS24_23` | 24 | 23 | 24-bit absolute at bit 23 |
| 30 | `R_CUDA_ABS24_26` | 24 | 26 | 24-bit absolute at bit 26 |
| 31 | `R_CUDA_ABS24_32` | 24 | 32 | 24-bit absolute at bit 32 |
| 32 | `R_CUDA_ABS24_40` | 24 | 40 | 24-bit absolute at bit 40 |
| 33 | `R_CUDA_ABS32_20` | 32 | 20 | 32-bit absolute at bit 20 |
| 34 | `R_CUDA_ABS32_23` | 32 | 23 | 32-bit absolute at bit 23 |
| 35 | `R_CUDA_ABS32_26` | 32 | 26 | 32-bit absolute at bit 26 |
| 36 | `R_CUDA_ABS32_32` | 32 | 32 | 32-bit absolute at bit 32 |
| 37 | `R_CUDA_ABS32_HI_20` | 16 | 20 | High 16 bits of 32-bit absolute at bit 20 |
| 38 | `R_CUDA_ABS32_HI_23` | 16 | 23 | High 16 bits of 32-bit absolute at bit 23 |
| 39 | `R_CUDA_ABS32_HI_26` | 16 | 26 | High 16 bits of 32-bit absolute at bit 26 |
| 40 | `R_CUDA_ABS32_HI_32` | 16 | 32 | High 16 bits of 32-bit absolute at bit 32 |
| 41 | `R_CUDA_ABS32_LO_20` | 16 | 20 | Low 16 bits of 32-bit absolute at bit 20 |
| 42 | `R_CUDA_ABS32_LO_23` | 16 | 23 | Low 16 bits of 32-bit absolute at bit 23 |
| 43 | `R_CUDA_ABS32_LO_26` | 16 | 26 | Low 16 bits of 32-bit absolute at bit 26 |
| 44 | `R_CUDA_ABS32_LO_32` | 16 | 32 | Low 16 bits of 32-bit absolute at bit 32 |
| 45 | `R_CUDA_ABS47_34` | 47 | 34 | 47-bit absolute at bit 34 (sm_75+ wide immediate) |
| 46 | `R_CUDA_ABS55_16_34` | 55 | 34 | 55-bit absolute at bit 34 (16-bit aligned) |
| 47 | `R_CUDA_ABS56_16_34` | 56 | 34 | 56-bit absolute at bit 34 (16-bit aligned) |

The HI/LO variants are used in instruction pairs where a 32-bit address is split across two instructions: one loads the upper 16 bits (`MOV32I` or `LUI`-like) and the other loads the lower 16 bits.

The ABS47, ABS55, and ABS56 types are newer additions (sm_75+/sm_90+) that exploit wider immediate fields in Turing and later ISA encodings.

### PC-Relative Relocations (R_CUDA_PCREL_*)

| Index | Name | Width | Bit pos | Description |
|---|---|---|---|---|
| 48 | `R_CUDA_PCREL_IMM24_23` | 24 | 23 | PC-relative 24-bit immediate at bit 23 |
| 49 | `R_CUDA_PCREL_IMM24_26` | 24 | 26 | PC-relative 24-bit immediate at bit 26 |

PC-relative relocations compute `(S + A) - PC` where `PC` is the address of the instruction being patched. These are used for branch instructions (`BRA`, `BRX`, `CALL`). The 24-bit width limits the branch offset to +/- 8M instructions (each instruction being 8 or 16 bytes depending on encoding).

### Function Descriptor Relocations (R_CUDA_FUNC_DESC_*)

These relocate references to function descriptor entries, used for indirect calls, virtual function tables, and device-side function pointers.

| Index | Name | Bits | Description |
|---|---|---|---|
| 50 | `R_CUDA_FUNC_DESC_32` | 32 | 32-bit function descriptor reference |
| 51 | `R_CUDA_FUNC_DESC_64` | 64 | 64-bit function descriptor reference |
| 52 | `R_CUDA_FUNC_DESC_8_0` | 8 | Descriptor byte at offset 0 |
| 53 | `R_CUDA_FUNC_DESC_8_8` | 8 | Descriptor byte at offset 8 |
| 54 | `R_CUDA_FUNC_DESC_8_16` | 8 | Descriptor byte at offset 16 |
| 55 | `R_CUDA_FUNC_DESC_8_24` | 8 | Descriptor byte at offset 24 |
| 56 | `R_CUDA_FUNC_DESC_8_32` | 8 | Descriptor byte at offset 32 |
| 57 | `R_CUDA_FUNC_DESC_8_40` | 8 | Descriptor byte at offset 40 |
| 58 | `R_CUDA_FUNC_DESC_8_48` | 8 | Descriptor byte at offset 48 |
| 59 | `R_CUDA_FUNC_DESC_8_56` | 8 | Descriptor byte at offset 56 |
| 60 | `R_CUDA_FUNC_DESC32_20` | 32 | 20 | 32-bit descriptor at instruction bit 20 |
| 61 | `R_CUDA_FUNC_DESC32_23` | 32 | 23 | 32-bit descriptor at instruction bit 23 |
| 62 | `R_CUDA_FUNC_DESC32_32` | 32 | 32 | 32-bit descriptor at instruction bit 32 |
| 63 | `R_CUDA_FUNC_DESC32_HI_20` | 16 | 20 | High 16 bits of descriptor at bit 20 |
| 64 | `R_CUDA_FUNC_DESC32_HI_23` | 16 | 23 | High 16 bits of descriptor at bit 23 |
| 65 | `R_CUDA_FUNC_DESC32_HI_32` | 16 | 32 | High 16 bits of descriptor at bit 32 |
| 66 | `R_CUDA_FUNC_DESC32_LO_20` | 16 | 20 | Low 16 bits of descriptor at bit 20 |
| 67 | `R_CUDA_FUNC_DESC32_LO_23` | 16 | 23 | Low 16 bits of descriptor at bit 23 |
| 68 | `R_CUDA_FUNC_DESC32_LO_32` | 16 | 32 | Low 16 bits of descriptor at bit 32 |

The byte-level variants (FUNC_DESC_8_*) are used for patching function descriptors in data sections rather than instruction immediate fields. The instruction variants (FUNC_DESC32_*) patch call instructions that embed the function descriptor index.

### Texture, Sampler, and Surface Relocations

These handle bindable resource references in SASS instructions.

| Index | Name | Description |
|---|---|---|
| 69 | `R_CUDA_TEX_HEADER_INDEX` | Texture header index (binds to .nv.tex.header) |
| 70 | `R_CUDA_TEX_SLOT` | Texture slot number |
| 71 | `R_CUDA_SAMP_HEADER_INDEX` | Sampler header index |
| 72 | `R_CUDA_SAMP_SLOT` | Sampler slot number |
| 73 | `R_CUDA_SAMP_HEADER_INDEX_0` | Sampler header index (variant 0) |
| 74 | `R_CUDA_SURF_HEADER_INDEX` | Surface header index |
| 75 | `R_CUDA_SURF_SLOT` | Surface slot number |
| 76 | `R_CUDA_SURF_HW_DESC` | Surface hardware descriptor |
| 77 | `R_CUDA_SURF_HW_SW_DESC` | Surface combined hardware/software descriptor |

These are resolved during linking by looking up the resource in the merged texture/sampler/surface header tables. The `HEADER_INDEX` types reference the global `.nv.tex.header` / `.nv.samp.header` / `.nv.surf.header` sections, while `SLOT` types reference the logical binding slot number.

### Constant Bank Relocations (R_CUDA_CONST_FIELD*)

Relocations for references into constant memory banks (`.nv.constant0`, `.nv.constant2`, etc.).

| Index | Name | Width | Bit pos | Description |
|---|---|---|---|---|
| 78 | `R_CUDA_CONST_FIELD19_20` | 19 | 20 | 19-bit constant offset at bit 20 |
| 79 | `R_CUDA_CONST_FIELD19_23` | 19 | 23 | 19-bit constant offset at bit 23 |
| 80 | `R_CUDA_CONST_FIELD19_26` | 19 | 26 | 19-bit constant offset at bit 26 |
| 81 | `R_CUDA_CONST_FIELD19_28` | 19 | 28 | 19-bit constant offset at bit 28 |
| 82 | `R_CUDA_CONST_FIELD19_40` | 19 | 40 | 19-bit constant offset at bit 40 |
| 83 | `R_CUDA_CONST_FIELD21_20` | 21 | 20 | 21-bit constant offset at bit 20 |
| 84 | `R_CUDA_CONST_FIELD21_23` | 21 | 23 | 21-bit constant offset at bit 23 |
| 85 | `R_CUDA_CONST_FIELD21_26` | 21 | 26 | 21-bit constant offset at bit 26 |
| 86 | `R_CUDA_CONST_FIELD21_38` | 21 | 38 | 21-bit constant offset at bit 38 |
| 87 | `R_CUDA_CONST_FIELD22_37` | 22 | 37 | 22-bit constant offset at bit 37 |

The 19-bit variants can address up to 512 KB of constant memory (19 bits * 4-byte aligned = 2 MB byte addressable, or 512 K dwords). The 21-bit and 22-bit variants (sm_75+) expand the addressable range for larger constant banks.

### Bindless Texture Relocations (R_CUDA_TEX_BINDLESSOFF* / R_CUDA_BINDLESSOFF*)

| Index | Name | Width | Bit pos | Description |
|---|---|---|---|---|
| 88 | `R_CUDA_TEX_BINDLESSOFF13_32` | 13 | 32 | Texture bindless offset at bit 32 |
| 89 | `R_CUDA_TEX_BINDLESSOFF13_41` | 13 | 41 | Texture bindless offset at bit 41 |
| 90 | `R_CUDA_TEX_BINDLESSOFF13_45` | 13 | 45 | Texture bindless offset at bit 45 |
| 91 | `R_CUDA_TEX_BINDLESSOFF13_47` | 13 | 47 | Texture bindless offset at bit 47 |
| 92 | `R_CUDA_TEX_SLOT9_49` | 9 | 49 | Texture slot 9-bit at bit 49 |
| 93 | `R_CUDA_BINDLESSOFF13_36` | 13 | 36 | Generic bindless offset at bit 36 |
| 94 | `R_CUDA_BINDLESSOFF14_40` | 14 | 40 | Generic bindless 14-bit offset at bit 40 |

Bindless texture relocations patch the bindless resource offset into texture sampling instructions. The 13-bit width supports up to 8192 unique textures per kernel launch. See [Bindless Relocations](bindless-relocations.md) for the full resolution pipeline.

### Unified Table Relocations (R_CUDA_UNIFIED_*)

Relocations for the Unified Descriptor Table (UDT) and Unified Function Table (UFT). These are the primary relocation types for CUDA Dynamic Parallelism and indirect function calls through the unified tables.

| Index | Name | Bits | Description |
|---|---|---|---|
| 95 | `R_CUDA_UNIFIED` | special | Unified table reference (generic) |
| 96 | `R_CUDA_UNIFIED_32` | 32 | 32-bit unified table offset |
| 97 | `R_CUDA_UNIFIED32_HI_32` | 16 | High 16 bits of unified table offset at bit 32 |
| 98 | `R_CUDA_UNIFIED32_LO_32` | 16 | Low 16 bits of unified table offset at bit 32 |
| 99 | `R_CUDA_UNIFIED_8_0` | 8 | Unified byte at offset 0 |
| 100 | `R_CUDA_UNIFIED_8_8` | 8 | Unified byte at offset 8 |
| 101 | `R_CUDA_UNIFIED_8_16` | 8 | Unified byte at offset 16 |
| 102 | `R_CUDA_UNIFIED_8_24` | 8 | Unified byte at offset 24 |
| 103 | `R_CUDA_UNIFIED_8_32` | 8 | Unified byte at offset 32 |
| 104 | `R_CUDA_UNIFIED_8_40` | 8 | Unified byte at offset 40 |
| 105 | `R_CUDA_UNIFIED_8_48` | 8 | Unified byte at offset 48 |
| 106 | `R_CUDA_UNIFIED_8_56` | 8 | Unified byte at offset 56 |

During the relocation phase, unified relocations (types 102--113 in the internal remapping) are translated to their base equivalents. Relocations targeting synthetic symbols (`__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT_CANONICAL`, `__UDT`, `__UFT`, `__UFT_END`, `__UDT_END`) are resolved to type 0 (no-op) because the unified table manager has already computed the final offsets.

### Instruction-Level Relocations (R_CUDA_INSTRUCTION*)

| Index | Name | Width | Description |
|---|---|---|---|
| 107 | `R_CUDA_INSTRUCTION64` | 64 | Full 64-bit instruction replacement |
| 108 | `R_CUDA_INSTRUCTION128` | 128 | Full 128-bit instruction replacement |

These are whole-instruction relocations that replace the entire instruction word. Used by the instruction-level optimization pass (peephole) and for instruction encoding conversions where the entire instruction must be rewritten (e.g., converting a 64-bit instruction to a 128-bit encoding or vice versa).

### Yield Relocations (R_CUDA_YIELD_*)

| Index | Name | Description |
|---|---|---|
| 109 | `R_CUDA_YIELD_OPCODE9_0` | Patch 9-bit opcode field at bit 0 for YIELD |
| 110 | `R_CUDA_YIELD_CLEAR_PRED4_87` | Clear 4-bit predicate field at bit 87 for YIELD |

YIELD relocations are used to convert YIELD instructions to NOP when forward-progress guarantees are required. The relocation engine checks the forward-progress-required flag (`ctx+94`) and suppresses the conversion if active, with the trace message: `"Ignoring the reloc to convert YIELD to NOP due to forward progress requirement."`

### Unused-Clear Relocations

| Index | Name | Width | Description |
|---|---|---|---|
| 111 | `R_CUDA_UNUSED_CLEAR32` | 32 | Clear 32 bits (write zeros) |
| 112 | `R_CUDA_UNUSED_CLEAR64` | 64 | Clear 64 bits (write zeros) |

These zero out fields in sections that are no longer needed after linking, such as placeholder entries in merged data sections.

### Miscellaneous Types

| Index | Name | Description |
|---|---|---|
| 113 | `R_CUDA_QUERY_DESC21_37` | 21-bit query descriptor offset at bit 37 |
| 114 | `R_CUDA_6_31` | 6-bit value at bit 31 |
| 115 | `R_CUDA_2_47` | 2-bit value at bit 47 |

The `QUERY_DESC` type is used for CUDA's query descriptor mechanism. The 6-bit and 2-bit types are narrow-field relocations for specific instruction encoding fields in newer architectures.

## Per-Architecture Vtable

In addition to the descriptor-table-driven application engine, nvlink maintains a per-architecture **relocation vtable** created by `sub_459640` (16,109 bytes). This 632-byte table (79 function pointer slots, 8 bytes each) contains architecture-specific handler functions for relocation types that require different patching behavior across GPU generations.

The vtable is populated based on the target SM range:

| SM Range | Architecture | Notes |
|---|---|---|
| 30--39 | Kepler | Shared "legacy" handler set |
| 50--59 | Maxwell | Adds additional instruction-field handlers |
| 60--69 | Pascal | Adds 60+ series handlers, wider field support |
| 70--72, 73--79 | Volta/Turing | New instruction format, `result[33]`/`result[34]` populated |
| 80--88, 89 | Ampere/Ada | Adds bindless handlers, new field variants |
| 90--99 | Hopper | Major differences in slots 10/11/28/50--53, new desc types |
| 100--103, 110--121 | Mercury (Blackwell+) | Distinct handler for slot 13, new constant field sizes |

The vtable is allocated via `sub_4307C0` (arena allocator) and the first 78 slots are populated. Slots that are not explicitly set remain NULL (zero), and the relocation engine skips NULL handlers. This is how unsupported relocation types are detected at runtime -- a NULL vtable entry for a required type triggers an error.

## Mercury vs CUDA Type Mapping

Mercury (sm >= 100) uses a parallel set of relocation types offset by `0x10000`. When the linker context's ELF class byte (offset `+7`) is `'A'` (0x41, indicating Mercury), the relocation engine subtracts `0x10000` from the type and uses the Mercury descriptor table (`off_1D3CBE0`) instead of the CUDA table (`off_1D3DBE0`).

```c
if (ctx->elf_class == 'A') {              // Mercury
    if (reloc_type <= 0x10000)
        fatal("unexpected reloc");          // should always be >= 0x10000
    reloc_type -= 0x10000;
    descriptor_table = off_1D3CBE0;         // Mercury table
} else {                                    // CUDA
    descriptor_table = off_1D3DBE0;         // CUDA table
}
```

Both tables have the same structure (64 bytes per entry) but different action encodings reflecting the different instruction formats between pre-Mercury (64-bit SASS) and Mercury (128-bit SASS) architectures.

## Validation Error Messages

The validation infrastructure produces these diagnostic messages:

| Source function | Message | Condition |
|---|---|---|
| `sub_42F6C0` | `"unknown attribute"` | Type index exceeds table bounds |
| `sub_42F6C0` | `"Relocation %s not supported on %s"` | Architecture class mismatch |
| `sub_42F760` | `"unknown attribute"` | Attribute type index > 96 |
| `sub_42F760` | `"Attribute %s not supported on %s"` | Attribute arch class mismatch |
| `sub_42F760` | `"unknown usage"` | Usage field has unrecognized value |
| `sub_42F850` | `"STO_CUDA_OBSCURE"` | Symbol with obscure storage class |
| `sub_469D60` | `"unexpected reloc"` | Mercury type found without Mercury context |

The error descriptors at `unk_2A5BAB0` (warning) and `unk_2A5BAC0` (error) control whether these diagnostics are warnings or fatal errors.

## Full Type Catalog

The complete list of 119 R_CUDA relocation types extracted from nvlink v13.0.88, sorted by name:

| # | Name | Type |
|---|---|---|
| 1 | `R_CUDA_2_47` | misc |
| 2 | `R_CUDA_32` | data |
| 3 | `R_CUDA_32_HI` | data |
| 4 | `R_CUDA_32_LO` | data |
| 5 | `R_CUDA_6_31` | misc |
| 6 | `R_CUDA_64` | data |
| 7 | `R_CUDA_8_0` | byte |
| 8 | `R_CUDA_8_16` | byte |
| 9 | `R_CUDA_8_24` | byte |
| 10 | `R_CUDA_8_32` | byte |
| 11 | `R_CUDA_8_40` | byte |
| 12 | `R_CUDA_8_48` | byte |
| 13 | `R_CUDA_8_56` | byte |
| 14 | `R_CUDA_8_8` | byte |
| 15 | `R_CUDA_ABS16_20` | abs-instr |
| 16 | `R_CUDA_ABS16_23` | abs-instr |
| 17 | `R_CUDA_ABS16_26` | abs-instr |
| 18 | `R_CUDA_ABS16_32` | abs-instr |
| 19 | `R_CUDA_ABS20_44` | abs-instr |
| 20 | `R_CUDA_ABS24_20` | abs-instr |
| 21 | `R_CUDA_ABS24_23` | abs-instr |
| 22 | `R_CUDA_ABS24_26` | abs-instr |
| 23 | `R_CUDA_ABS24_32` | abs-instr |
| 24 | `R_CUDA_ABS24_40` | abs-instr |
| 25 | `R_CUDA_ABS32_20` | abs-instr |
| 26 | `R_CUDA_ABS32_23` | abs-instr |
| 27 | `R_CUDA_ABS32_26` | abs-instr |
| 28 | `R_CUDA_ABS32_32` | abs-instr |
| 29 | `R_CUDA_ABS32_HI_20` | abs-instr |
| 30 | `R_CUDA_ABS32_HI_23` | abs-instr |
| 31 | `R_CUDA_ABS32_HI_26` | abs-instr |
| 32 | `R_CUDA_ABS32_HI_32` | abs-instr |
| 33 | `R_CUDA_ABS32_LO_20` | abs-instr |
| 34 | `R_CUDA_ABS32_LO_23` | abs-instr |
| 35 | `R_CUDA_ABS32_LO_26` | abs-instr |
| 36 | `R_CUDA_ABS32_LO_32` | abs-instr |
| 37 | `R_CUDA_ABS47_34` | abs-instr |
| 38 | `R_CUDA_ABS55_16_34` | abs-instr |
| 39 | `R_CUDA_ABS56_16_34` | abs-instr |
| 40 | `R_CUDA_BINDLESSOFF13_36` | bindless |
| 41 | `R_CUDA_BINDLESSOFF14_40` | bindless |
| 42 | `R_CUDA_CONST_FIELD19_20` | const |
| 43 | `R_CUDA_CONST_FIELD19_23` | const |
| 44 | `R_CUDA_CONST_FIELD19_26` | const |
| 45 | `R_CUDA_CONST_FIELD19_28` | const |
| 46 | `R_CUDA_CONST_FIELD19_40` | const |
| 47 | `R_CUDA_CONST_FIELD21_20` | const |
| 48 | `R_CUDA_CONST_FIELD21_23` | const |
| 49 | `R_CUDA_CONST_FIELD21_26` | const |
| 50 | `R_CUDA_CONST_FIELD21_38` | const |
| 51 | `R_CUDA_CONST_FIELD22_37` | const |
| 52 | `R_CUDA_FUNC_DESC_32` | func-desc |
| 53 | `R_CUDA_FUNC_DESC32_20` | func-desc |
| 54 | `R_CUDA_FUNC_DESC32_23` | func-desc |
| 55 | `R_CUDA_FUNC_DESC32_32` | func-desc |
| 56 | `R_CUDA_FUNC_DESC32_HI_20` | func-desc |
| 57 | `R_CUDA_FUNC_DESC32_HI_23` | func-desc |
| 58 | `R_CUDA_FUNC_DESC32_HI_32` | func-desc |
| 59 | `R_CUDA_FUNC_DESC32_LO_20` | func-desc |
| 60 | `R_CUDA_FUNC_DESC32_LO_23` | func-desc |
| 61 | `R_CUDA_FUNC_DESC32_LO_32` | func-desc |
| 62 | `R_CUDA_FUNC_DESC_64` | func-desc |
| 63 | `R_CUDA_FUNC_DESC_8_0` | func-desc |
| 64 | `R_CUDA_FUNC_DESC_8_16` | func-desc |
| 65 | `R_CUDA_FUNC_DESC_8_24` | func-desc |
| 66 | `R_CUDA_FUNC_DESC_8_32` | func-desc |
| 67 | `R_CUDA_FUNC_DESC_8_40` | func-desc |
| 68 | `R_CUDA_FUNC_DESC_8_48` | func-desc |
| 69 | `R_CUDA_FUNC_DESC_8_56` | func-desc |
| 70 | `R_CUDA_FUNC_DESC_8_8` | func-desc |
| 71 | `R_CUDA_G32` | global |
| 72 | `R_CUDA_G64` | global |
| 73 | `R_CUDA_G8_0` | global |
| 74 | `R_CUDA_G8_16` | global |
| 75 | `R_CUDA_G8_24` | global |
| 76 | `R_CUDA_G8_32` | global |
| 77 | `R_CUDA_G8_40` | global |
| 78 | `R_CUDA_G8_48` | global |
| 79 | `R_CUDA_G8_56` | global |
| 80 | `R_CUDA_G8_8` | global |
| 81 | `R_CUDA_INSTRUCTION128` | instr |
| 82 | `R_CUDA_INSTRUCTION64` | instr |
| 83 | `R_CUDA_NONE` | sentinel |
| 84 | `R_CUDA_NONE_LAST` | sentinel |
| 85 | `R_CUDA_PCREL_IMM24_23` | pc-rel |
| 86 | `R_CUDA_PCREL_IMM24_26` | pc-rel |
| 87 | `R_CUDA_QUERY_DESC21_37` | misc |
| 88 | `R_CUDA_SAMP_HEADER_INDEX` | sampler |
| 89 | `R_CUDA_SAMP_HEADER_INDEX_0` | sampler |
| 90 | `R_CUDA_SAMP_SLOT` | sampler |
| 91 | `R_CUDA_SURF_HEADER_INDEX` | surface |
| 92 | `R_CUDA_SURF_HW_DESC` | surface |
| 93 | `R_CUDA_SURF_HW_SW_DESC` | surface |
| 94 | `R_CUDA_SURF_SLOT` | surface |
| 95 | `R_CUDA_TEX_BINDLESSOFF13_32` | bindless |
| 96 | `R_CUDA_TEX_BINDLESSOFF13_41` | bindless |
| 97 | `R_CUDA_TEX_BINDLESSOFF13_45` | bindless |
| 98 | `R_CUDA_TEX_BINDLESSOFF13_47` | bindless |
| 99 | `R_CUDA_TEX_HEADER_INDEX` | texture |
| 100 | `R_CUDA_TEX_SLOT` | texture |
| 101 | `R_CUDA_TEX_SLOT9_49` | texture |
| 102 | `R_CUDA_UNIFIED` | unified |
| 103 | `R_CUDA_UNIFIED_32` | unified |
| 104 | `R_CUDA_UNIFIED32_HI_32` | unified |
| 105 | `R_CUDA_UNIFIED32_LO_32` | unified |
| 106 | `R_CUDA_UNIFIED_8_0` | unified |
| 107 | `R_CUDA_UNIFIED_8_16` | unified |
| 108 | `R_CUDA_UNIFIED_8_24` | unified |
| 109 | `R_CUDA_UNIFIED_8_32` | unified |
| 110 | `R_CUDA_UNIFIED_8_40` | unified |
| 111 | `R_CUDA_UNIFIED_8_48` | unified |
| 112 | `R_CUDA_UNIFIED_8_56` | unified |
| 113 | `R_CUDA_UNIFIED_8_8` | unified |
| 114 | `R_CUDA_UNUSED_CLEAR32` | clear |
| 115 | `R_CUDA_UNUSED_CLEAR64` | clear |
| 116 | `R_CUDA_YIELD_CLEAR_PRED4_87` | yield |
| 117 | `R_CUDA_YIELD_OPCODE9_0` | yield |
| 118 | `R_CUDA_INSTRUCTION64` | instr |
| 119 | `R_CUDA_INSTRUCTION128` | instr |

Note: The catalog contains 119 unique name strings as extracted from the binary. Some types appear in both the standard and attribute tables with the same name but different table indices and different descriptor actions. The total across both tables is 117 + 65 = 182 table slots, but many attribute slots share names with their standard counterparts.

## Tier Matrix

The 117 standard-table entries do not all light up on every architecture. Some types are baseline (present from Kepler), some appear with a specific ISA extension (Volta's `YIELD`, Turing's 21-bit constant fields, Hopper's 128-bit `INSTRUCTION128`, Blackwell's `ABS56`), and some are synthetic types translated away before the application engine ever indexes the descriptor table. The matrix below maps every `r_type` index to a pair:

- **`introduced_at`** -- the lowest SM tier at which the type was observed in the binary or its ISA usage is plausible. Tiers are SM ranges (`sm_30+` = Kepler-and-later baseline, `sm_50+` = Maxwell+, `sm_70+` = Volta+, `sm_75+` = Turing+, `sm_80+` = Ampere+, `sm_90+` = Hopper+, `sm_100+` = Blackwell/Mercury+).
- **`resolved_by`** -- the component that performs the patch:
  - **`nvlink`** -- standard relocation engine `sub_469D60` reads the descriptor and patches via `sub_468760`.
  - **`nvlink+bindless`** -- the bindless pass `sub_438DD0` rewrites the symbol target to `$NVLINKBINDLESSOFF_<name>` before `nvlink` applies the patch (see [Bindless Relocations](bindless-relocations.md)).
  - **`ptxas-runtime`** -- a synthetic type emitted by ptxas (`UNIFIED_*` family targeting `__UFT_*` / `__UDT_*` synthetic symbols); the unified-table manager translates these to `R_CUDA_NONE` (type 0) before relocation application, so the descriptor at this index is reached only by the resolved-rela emitter.
  - **`FNLZR`** -- on Mercury targets, the post-link finalizer `sub_4748F0` re-applies the same descriptor table (`off_1D3DBE0`, indexed by the CUDA type *without* the `0x10000` offset) when `--preserve-relocs` produced a `.nv.resolvedrela` section that survives capmerc emission. See [§ sm_100+ FNLZR-Deferred Path](bindless-relocations.md#sm_100-fnlzr-deferred-path).

Tier inferences are derived from three sources: (a) the architecture-class field in the standard table (`sub_42F6C0`/`sub_42F8C0`), which gives a coarse 5-bucket grouping; (b) name suffixes encoding bit positions that match known ISA encodings (`*_20` = Maxwell+, `*_34` = Turing+ wide immediate, `*_38` = Ampere+); (c) the per-architecture vtable (`sub_459640`) which populates handler slots conditionally. Confidence is marked **HIGH** where the architecture-class field directly classifies the entry, **MED** where the suffix gives a strong hint but no decompiled gate confirms it, and **LOW** where the inference rests solely on naming convention.

| Idx | Name | introduced_at | resolved_by | Confidence |
|----:|------|--------------:|-------------|:----------:|
| 0 | `R_CUDA_NONE` | sm_30+ | nvlink (no-op) | HIGH |
| 1 | `R_CUDA_32` | sm_30+ | nvlink | HIGH |
| 2 | `R_CUDA_64` | sm_30+ | nvlink | HIGH |
| 3 | `R_CUDA_G32` | sm_30+ | nvlink | HIGH |
| 4 | `R_CUDA_G64` | sm_30+ | nvlink | HIGH |
| 5 | `R_CUDA_ABS32_26` | sm_50+ | nvlink | HIGH |
| 6 | `R_CUDA_TEX_HEADER_INDEX` | sm_30+ | nvlink+bindless | HIGH |
| 7 | `R_CUDA_SAMP_HEADER_INDEX` | sm_30+ | nvlink+bindless | HIGH |
| 8 | `R_CUDA_SURF_HW_DESC` | sm_30+ | nvlink+bindless | HIGH |
| 9 | `R_CUDA_SURF_HW_SW_DESC` | sm_30+ | nvlink+bindless | HIGH |
| 10 | `R_CUDA_ABS32_LO_26` | sm_50+ | nvlink | HIGH |
| 11 | `R_CUDA_ABS32_HI_26` | sm_50+ | nvlink | HIGH |
| 12 | `R_CUDA_ABS32_23` | sm_50+ | nvlink | HIGH |
| 13 | `R_CUDA_ABS32_LO_23` | sm_50+ | nvlink | HIGH |
| 14 | `R_CUDA_ABS32_HI_23` | sm_50+ | nvlink | HIGH |
| 15 | `R_CUDA_ABS24_26` | sm_50+ | nvlink | HIGH |
| 16 | `R_CUDA_ABS24_23` | sm_50+ | nvlink | HIGH |
| 17 | `R_CUDA_ABS16_26` | sm_50+ | nvlink | HIGH |
| 18 | `R_CUDA_ABS16_23` | sm_50+ | nvlink | HIGH |
| 19 | `R_CUDA_TEX_SLOT` | sm_30+ | nvlink+bindless | HIGH |
| 20 | `R_CUDA_SAMP_SLOT` | sm_30+ | nvlink+bindless | HIGH |
| 21 | `R_CUDA_SURF_SLOT` | sm_30+ | nvlink+bindless | HIGH |
| 22 | `R_CUDA_TEX_BINDLESSOFF13_32` | sm_50+ | nvlink+bindless | HIGH |
| 23 | `R_CUDA_TEX_BINDLESSOFF13_47` | sm_75+ | nvlink+bindless | MED |
| 24 | `R_CUDA_CONST_FIELD19_28` | sm_50+ | nvlink | HIGH |
| 25 | `R_CUDA_CONST_FIELD19_23` | sm_50+ | nvlink | HIGH |
| 26 | `R_CUDA_TEX_SLOT9_49` | sm_75+ | nvlink+bindless | MED |
| 27 | `R_CUDA_6_31` | sm_75+ | nvlink | LOW |
| 28 | `R_CUDA_2_47` | sm_100+ | nvlink / FNLZR | LOW |
| 29 | `R_CUDA_TEX_BINDLESSOFF13_41` | sm_50+ | nvlink+bindless | MED |
| 30 | `R_CUDA_TEX_BINDLESSOFF13_45` | sm_75+ | nvlink+bindless | MED |
| 31 | `R_CUDA_FUNC_DESC32_23` | sm_35+ | nvlink | HIGH |
| 32 | `R_CUDA_FUNC_DESC32_LO_23` | sm_35+ | nvlink | HIGH |
| 33 | `R_CUDA_FUNC_DESC32_HI_23` | sm_35+ | nvlink | HIGH |
| 34 | `R_CUDA_FUNC_DESC_32` | sm_35+ | nvlink | HIGH |
| 35 | `R_CUDA_FUNC_DESC_64` | sm_35+ | nvlink | HIGH |
| 36 | `R_CUDA_CONST_FIELD21_26` | sm_75+ | nvlink | HIGH |
| 37 | `R_CUDA_QUERY_DESC21_37` | sm_75+ | nvlink | MED |
| 38 | `R_CUDA_CONST_FIELD19_26` | sm_50+ | nvlink | HIGH |
| 39 | `R_CUDA_CONST_FIELD21_23` | sm_75+ | nvlink | HIGH |
| 40 | `R_CUDA_PCREL_IMM24_26` | sm_50+ | nvlink | HIGH |
| 41 | `R_CUDA_PCREL_IMM24_23` | sm_50+ | nvlink | HIGH |
| 42 | `R_CUDA_ABS32_20` | sm_70+ | nvlink | HIGH |
| 43 | `R_CUDA_ABS32_LO_20` | sm_70+ | nvlink | HIGH |
| 44 | `R_CUDA_ABS32_HI_20` | sm_70+ | nvlink | HIGH |
| 45 | `R_CUDA_ABS24_20` | sm_70+ | nvlink | HIGH |
| 46 | `R_CUDA_ABS16_20` | sm_70+ | nvlink | HIGH |
| 47 | `R_CUDA_FUNC_DESC32_20` | sm_70+ | nvlink | HIGH |
| 48 | `R_CUDA_FUNC_DESC32_LO_20` | sm_70+ | nvlink | HIGH |
| 49 | `R_CUDA_FUNC_DESC32_HI_20` | sm_70+ | nvlink | HIGH |
| 50 | `R_CUDA_CONST_FIELD19_20` | sm_70+ | nvlink | HIGH |
| 51 | `R_CUDA_BINDLESSOFF13_36` | sm_75+ | nvlink+bindless | MED |
| 52 | `R_CUDA_SURF_HEADER_INDEX` | sm_30+ | nvlink+bindless | HIGH |
| 53 | `R_CUDA_INSTRUCTION64` | sm_75+ | nvlink | HIGH |
| 54 | `R_CUDA_CONST_FIELD21_20` | sm_75+ | nvlink | HIGH |
| 55 | `R_CUDA_ABS32_32` | sm_75+ | nvlink | HIGH |
| 56 | `R_CUDA_ABS32_LO_32` | sm_75+ | nvlink | HIGH |
| 57 | `R_CUDA_ABS32_HI_32` | sm_75+ | nvlink | HIGH |
| 58 | `R_CUDA_ABS47_34` | sm_75+ | nvlink | HIGH |
| 59 | `R_CUDA_ABS16_32` | sm_75+ | nvlink | HIGH |
| 60 | `R_CUDA_ABS24_32` | sm_75+ | nvlink | HIGH |
| 61 | `R_CUDA_FUNC_DESC32_32` | sm_75+ | nvlink | HIGH |
| 62 | `R_CUDA_FUNC_DESC32_LO_32` | sm_75+ | nvlink | HIGH |
| 63 | `R_CUDA_FUNC_DESC32_HI_32` | sm_75+ | nvlink | HIGH |
| 64 | `R_CUDA_CONST_FIELD19_40` | sm_80+ | nvlink | MED |
| 65 | `R_CUDA_BINDLESSOFF14_40` | sm_80+ | nvlink+bindless | MED |
| 66 | `R_CUDA_CONST_FIELD21_38` | sm_80+ | nvlink | MED |
| 67 | `R_CUDA_INSTRUCTION128` | sm_90+ | nvlink / FNLZR | HIGH |
| 68 | `R_CUDA_YIELD_OPCODE9_0` | sm_70+ | nvlink | HIGH |
| 69 | `R_CUDA_YIELD_CLEAR_PRED4_87` | sm_70+ | nvlink | HIGH |
| 70 | `R_CUDA_32_LO` | sm_30+ | nvlink | HIGH |
| 71 | `R_CUDA_32_HI` | sm_30+ | nvlink | HIGH |
| 72 | `R_CUDA_UNUSED_CLEAR32` | sm_75+ | nvlink (zero-fill) | MED |
| 73 | `R_CUDA_UNUSED_CLEAR64` | sm_75+ | nvlink (zero-fill) | MED |
| 74 | `R_CUDA_ABS24_40` | sm_80+ | nvlink | MED |
| 75 | `R_CUDA_ABS55_16_34` | sm_90+ | nvlink | HIGH |
| 76 | `R_CUDA_8_0` | sm_30+ | nvlink | HIGH |
| 77 | `R_CUDA_8_8` | sm_30+ | nvlink | HIGH |
| 78 | `R_CUDA_8_16` | sm_30+ | nvlink | HIGH |
| 79 | `R_CUDA_8_24` | sm_30+ | nvlink | HIGH |
| 80 | `R_CUDA_8_32` | sm_30+ | nvlink | HIGH |
| 81 | `R_CUDA_8_40` | sm_30+ | nvlink | HIGH |
| 82 | `R_CUDA_8_48` | sm_30+ | nvlink | HIGH |
| 83 | `R_CUDA_8_56` | sm_30+ | nvlink | HIGH |
| 84 | `R_CUDA_G8_0` | sm_30+ | nvlink | HIGH |
| 85 | `R_CUDA_G8_8` | sm_30+ | nvlink | HIGH |
| 86 | `R_CUDA_G8_16` | sm_30+ | nvlink | HIGH |
| 87 | `R_CUDA_G8_24` | sm_30+ | nvlink | HIGH |
| 88 | `R_CUDA_G8_32` | sm_30+ | nvlink | HIGH |
| 89 | `R_CUDA_G8_40` | sm_30+ | nvlink | HIGH |
| 90 | `R_CUDA_G8_48` | sm_30+ | nvlink | HIGH |
| 91 | `R_CUDA_G8_56` | sm_30+ | nvlink | HIGH |
| 92 | `R_CUDA_FUNC_DESC_8_0` | sm_35+ | nvlink | HIGH |
| 93 | `R_CUDA_FUNC_DESC_8_8` | sm_35+ | nvlink | HIGH |
| 94 | `R_CUDA_FUNC_DESC_8_16` | sm_35+ | nvlink | HIGH |
| 95 | `R_CUDA_FUNC_DESC_8_24` | sm_35+ | nvlink | HIGH |
| 96 | `R_CUDA_FUNC_DESC_8_32` | sm_35+ | nvlink | HIGH |
| 97 | `R_CUDA_FUNC_DESC_8_40` | sm_35+ | nvlink | HIGH |
| 98 | `R_CUDA_FUNC_DESC_8_48` | sm_35+ | nvlink | HIGH |
| 99 | `R_CUDA_FUNC_DESC_8_56` | sm_35+ | nvlink | HIGH |
| 100 | `R_CUDA_ABS20_44` | sm_80+ | nvlink | MED |
| 101 | `R_CUDA_SAMP_HEADER_INDEX_0` | sm_75+ | nvlink+bindless | MED |
| 102 | `R_CUDA_UNIFIED` | sm_75+ | ptxas-runtime | HIGH |
| 103 | `R_CUDA_UNIFIED_32` | sm_75+ | ptxas-runtime | HIGH |
| 104 | `R_CUDA_UNIFIED_8_0` | sm_75+ | ptxas-runtime | HIGH |
| 105 | `R_CUDA_UNIFIED_8_8` | sm_75+ | ptxas-runtime | HIGH |
| 106 | `R_CUDA_UNIFIED_8_16` | sm_75+ | ptxas-runtime | HIGH |
| 107 | `R_CUDA_UNIFIED_8_24` | sm_75+ | ptxas-runtime | HIGH |
| 108 | `R_CUDA_UNIFIED_8_32` | sm_75+ | ptxas-runtime | HIGH |
| 109 | `R_CUDA_UNIFIED_8_40` | sm_75+ | ptxas-runtime | HIGH |
| 110 | `R_CUDA_UNIFIED_8_48` | sm_75+ | ptxas-runtime | HIGH |
| 111 | `R_CUDA_UNIFIED_8_56` | sm_75+ | ptxas-runtime | HIGH |
| 112 | `R_CUDA_UNIFIED32_LO_32` | sm_75+ | ptxas-runtime | HIGH |
| 113 | `R_CUDA_UNIFIED32_HI_32` | sm_75+ | ptxas-runtime | HIGH |
| 114 | `R_CUDA_ABS56_16_34` | sm_100+ | nvlink / FNLZR | HIGH |
| 115 | `R_CUDA_CONST_FIELD22_37` | sm_100+ | nvlink / FNLZR | MED |
| 116 | `R_CUDA_NONE_LAST` | -- (sentinel) | nvlink (rejected) | HIGH |

### Tier-Grouping Summary

| Tier | Count | Defining feature | Example types |
|------|------:|------------------|---------------|
| sm_30+ (baseline) | 26 | Data, byte, global, texture/sampler/surface header & slot | `R_CUDA_32`, `R_CUDA_G64`, `R_CUDA_8_*`, `R_CUDA_TEX_HEADER_INDEX` |
| sm_35+ | 13 | Function-descriptor relocations (CUDA Dynamic Parallelism) | `R_CUDA_FUNC_DESC_*`, `R_CUDA_FUNC_DESC_8_*` |
| sm_50+ | 18 | 64-bit SASS bit-positions 23/26, 19-bit `CONST_FIELD`, PCREL, base bindless | `R_CUDA_ABS32_26`, `R_CUDA_CONST_FIELD19_23`, `R_CUDA_PCREL_IMM24_23` |
| sm_70+ (Volta) | 11 | Bit-position 20 family, `YIELD` opcode rewrite | `R_CUDA_ABS32_20`, `R_CUDA_YIELD_OPCODE9_0` |
| sm_75+ (Turing) | 33 | 21-bit `CONST_FIELD`, bit-position 32, `ABS47`, `UNIFIED_*`, `INSTRUCTION64` | `R_CUDA_ABS47_34`, `R_CUDA_UNIFIED_32`, `R_CUDA_CONST_FIELD21_*` |
| sm_80+ (Ampere) | 5 | Bit-position 38/40/44, 14-bit bindless | `R_CUDA_CONST_FIELD21_38`, `R_CUDA_BINDLESSOFF14_40`, `R_CUDA_ABS20_44` |
| sm_90+ (Hopper) | 2 | 55-bit immediate, 128-bit instruction word | `R_CUDA_ABS55_16_34`, `R_CUDA_INSTRUCTION128` |
| sm_100+ (Blackwell/Mercury) | 3 | 56-bit immediate, 22-bit `CONST_FIELD`, 2-bit narrow field | `R_CUDA_ABS56_16_34`, `R_CUDA_CONST_FIELD22_37`, `R_CUDA_2_47` |
| sentinel | 6 | `R_CUDA_NONE`, `R_CUDA_NONE_LAST`, four legacy entries with no live use observed | `R_CUDA_NONE`, `R_CUDA_NONE_LAST` |

The `sm_30+` and `sm_35+` baseline rows are anchored by the `arch_class == 1` bucket in `sub_42F8C0` (Kepler-through-Volta). The `sm_75+` group is corroborated by the `arch_class == 3` bucket (Turing-specific) plus the `arch_class == 5` boundary at `sm >= 76`, which separates Turing from Ampere. The four Blackwell-tier entries (`ABS56_16_34`, `CONST_FIELD22_37`, `2_47`, and the implied 22-bit/2-bit narrow-field family) appear at descriptor-table indices 114-115 -- the highest non-sentinel slots -- consistent with their being the last additions before the table was frozen at 117 entries.

> ⚡ **QUIRK -- `sm_30+` baseline contains 26 entries, but only 17 are reachable via class-1 vtable slots**
> The `sm_30+` rows in the matrix are inferred from name shape (data widths and texture/sampler/surface bindings predate the bit-position-encoded instruction relocs). However, the per-architecture vtable (`sub_459640`) leaves several slots NULL for Kepler-only targets, including the bindless-related entries (indices 22, 29, 30, 51). On a true Kepler target, attempting to apply `R_CUDA_TEX_BINDLESSOFF13_32` triggers the `"Relocation %s not supported on %s"` warning even though the descriptor itself decodes cleanly. The `introduced_at` column reflects *when the descriptor became legal*, not *when the type was first emitted by ptxas*. The downstream gate is the vtable, not the table.

> ⚡ **QUIRK -- `UNIFIED_*` types are dead by the time the application engine sees them**
> The 12 `R_CUDA_UNIFIED*` rows (indices 102-113) are marked `resolved_by = ptxas-runtime` because the unified-table manager rewrites every `UNIFIED_*` relocation targeting a synthetic symbol (`__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT_CANONICAL`, `__UDT`, `__UFT`, `__UFT_END`, `__UDT_END`) to type 0 (`R_CUDA_NONE`) before the relocation phase runs. The descriptors at indices 102-113 are still byte-decoded for the resolved-rela emitter (`sub_46ADC0`), but `sub_469D60` never indexes them through the live patch path. They are effectively "synthetic" types: real entries in the type space, real descriptors in the table, but no instruction stream is ever patched through them at link time.

> ⚡ **QUIRK -- Blackwell-tier entries share descriptor encoding with Hopper-tier entries**
> The three sm_100+ entries (`ABS56_16_34`, `CONST_FIELD22_37`, `2_47`) are not encoded with new action codes -- they reuse the same `slot0.action = 0x09` (`SHIFTED_2`, used for the low piece of wide split fields) and `slot1.action = 0x15` (decimal 21, the "second-piece" continuation action listed in [R_CUDA Relocation Catalog § `action` enum](../reference/r-cuda-catalog.md#action-enum-dword-at-byte-20)) that already serve the Hopper-era 55-bit and 21-bit types. The only difference is the descriptor's `bit_width` field: 56 instead of 55 (`R_CUDA_ABS56_16_34` decodes as `slot0=(16, 8, 9, 2); slot1=(34, 48, 21, 10)`, summing to 8 + 48 = 56), 22 instead of 21, 2 instead of 6. The continuation action `0x15` is distinct from the `MASKED_SHIFT` family at `0x16..0x1D` / `0x2F..0x36` -- it lives in the gap immediately below it. The application engine has no Mercury-specific code path -- it patches Mercury fields with the same dispatch as Hopper, and the FNLZR path inherits the same dispatch when re-applying preserved relocations after capmerc emission. See [§ sm_100+ FNLZR-Deferred Path](bindless-relocations.md#sm_100-fnlzr-deferred-path) for the consumer side.

## Cross-References

- [Relocation Phase](../pipeline/relocate.md) -- the pipeline stage that consumes these types
- [Finalization Phase](../pipeline/finalize.md) -- second-pass relocation application
- [Relocation Application Engine](relocation-engine.md) -- the bit-field patching engine
- [Bindless Relocations](bindless-relocations.md) -- bindless texture/surface resolution
- [Symbol Resolution](symbol-resolution.md) -- symbol resolution that feeds resolved addresses to relocation application
- [Section Merging](section-merging.md) -- merge phase that collects `.rela.*` sections from input objects
- [Binary Layout](../binary-layout.md) -- locations of descriptor tables in the nvlink binary

### Sibling Wiki

- **ptxas wiki**: [Relocations & Symbols](../../ptxas/output/relocations.html) -- how ptxas generates R\_CUDA and R\_MERCURY relocation entries during code emission (the producer side of what nvlink consumes)

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_42F6C0` validates standard table at `off_1D37600` (117 entries, limit 0x75) | HIGH | Decompiled `sub_42F6C0_0x42f6c0.c` line 26: `a1 >= 0x75`; line 25: `&off_1D37600` |
| `sub_42F6C0` validates attribute table at `off_1D371E0` (65 entries, limit 0x41) | HIGH | Decompiled line 17: `&off_1D371E0`; line 18: `a1 < 0x41` |
| Attribute type offset is 0x10000 | HIGH | Decompiled line 15: `a1 -= 0x10000` |
| `sub_42F8C0` arch class mapping: sm<=70 class 1, sm<=72 class 2, sm>=76 class 5, sm 73-75 class 3 | HIGH | Decompiled `sub_42F8C0_0x42f8c0.c`: `2*(a1>=76)+3` formula confirmed |
| `sub_42F6C0` emits `"unknown attribute"` on bounds violation | HIGH | Decompiled line 21: exact string literal |
| Five architecture class names at `off_1D371A0` | HIGH | Decompiled line 36: `&off_1D371A0 + v10` |
| 64-byte descriptor entries at `off_1D3DBE0` (CUDA) and `off_1D3CBE0` (Mercury) | HIGH | Decompiled `sub_468760`: `type_index << 6` indexing confirmed |
| Application engine `sub_468760` at 0x468760 with 10-parameter signature | HIGH | Decompiled file exists with matching signature |
| 119 unique R_CUDA type name strings extracted from binary | HIGH | All names extracted from `nvlink_strings.json`; complete catalog verified |
| `"STO_CUDA_OBSCURE"` emitted by `sub_42F850` | HIGH | String confirmed in `nvlink_strings.json` |
| Action types and aliases (0x01/0x12/0x2E, 0x06/0x37, 0x07/0x38) | HIGH | The 64-byte descriptor table at `off_1D3DBE0` was byte-decoded for all 117 entries; the resulting `slot0.action` distribution matches the `sub_468760` switch arms one-for-one. The full per-entry decode and action-to-name mapping appears in [R_CUDA Relocation Catalog § Descriptor Byte Layout](../reference/r-cuda-catalog.md#descriptor-byte-layout). |
| Per-architecture vtable with 79 slots from `sub_459640` | MEDIUM | Function exists; slot count inferred from 632-byte allocation (79 * 8) |
| YIELD relocation forward-progress check at ctx+94 | MEDIUM | Reconstructed from decompiled relocation phase analysis |
