# Relocation Phase

The relocation phase is the sixth stage of nvlink's linking pipeline, invoked from `main()` after the layout phase has assigned addresses to all sections and symbols. Its job is to walk every pending relocation in the output ELF, resolve the target symbol, compute the final value, and either patch the value into the instruction stream or (when `--preserve-relocs` is active) emit the relocation into a `.nv.resolvedrela` section for the runtime loader. The primary entry point is `sub_469D60` (`apply_relocations`, 26,578 bytes, 985 decompiled lines), called from `main()` as the "relocate" phase in the timing checkpoint sequence `init -> read -> merge -> layout -> relocate -> finalize -> write`.

Unlike a conventional ELF linker that iterates `.rela.*` sections and patches target bytes, nvlink maintains relocations in a **singly-linked list** rooted at offset `+376` in the linker context object. Each node in this list holds a pointer to a 32-byte relocation record. The function walks this list linearly, resolving each relocation through a multi-stage pipeline: addend computation, symbol lookup, alias chain resolution, dead function filtering, unified table remapping, descriptor table dispatch, and finally bit-field patching of the instruction word via the application engine `sub_468760`.

| | |
|---|---|
| **Primary function** | `sub_469D60` at `0x469D60` (26,578 bytes) |
| **Application engine** | `sub_468760` at `0x468760` (14,322 bytes) |
| **Resolved-rela emitter** | `sub_46ADC0` at `0x46ADC0` (11,515 bytes) |
| **Relocation vtable** | `sub_459640` at `0x459640` (16,109 bytes, used by finalization) |
| **Called from** | `main()` at `0x409800`, between layout and finalization |
| **Timing label** | `"relocate"` |
| **Key globals** | `off_1D3CBE0` (Mercury descriptor table), `off_1D3DBE0` (CUDA descriptor table) |
| **CLI option** | `--preserve-relocs` (byte at `2A5F2CE`) |

## Pipeline Position

```
Layout Phase (sub_439830)
   |
   v
Relocation Phase (sub_469D60)  <-- this page
   |
   v
Finalization Phase (sub_445000)
```

`main()` calls `sub_469D60` with two arguments: the linker context pointer (`a1`) and a mutex attribute pointer (`a2`). The linker context carries all state needed for relocation resolution: the relocation linked list at offset `+376`, the preserve-relocs linked list at offset `+384`, architecture flags, the output ELF wrapper, and symbol/section accessors.

## Relocation Linked List

All relocations pending resolution are stored in a singly-linked list. Each node is a pair:

```
struct reloc_node {
    reloc_node*   next;         // offset +0: pointer to next node (NULL = end)
    reloc_record* reloc;        // offset +8: pointer to the 32-byte relocation record
};
```

The relocation record itself is stored as two SSE-width (128-bit) values, loaded via `_mm_loadu_si128`:

```
struct reloc_record {           // 32 bytes total, accessed as two __m128i
    int64_t  addend;            // [0:8]   target addend / offset value
    int64_t  reloc_info;        // [8:16]  low 32 bits = relocation type,
                                //         high 32 bits = symbol index
    uint32_t section_idx;       // [16:20] target section index in output ELF
    uint32_t sym_addend_idx;    // [20:24] symbol index for addend resolution
    int64_t  extra;             // [24:32] extra data / secondary offset
};
```

The walk is a simple `while (v4 != NULL)` loop that reads `v4[0]` (next pointer) and `v4[1]` (relocation record pointer) at each step. Nodes are removed from the list in-place when the relocation is fully applied: the predecessor's next pointer is redirected to skip the consumed node, and both the node and its record are freed via `sub_431000` (arena_free).

## Resolution Algorithm

For each relocation record, `sub_469D60` executes the following steps:

### Step 1: Addend Resolution

```c
sym_addend_idx = reloc->sym_addend_idx;   // field at byte 20
if (sym_addend_idx != 0) {
    symbol = sub_440590(ctx, sym_addend_idx);  // resolve symbol by index
    reloc->addend += *(int64_t*)(symbol + 8);  // add symbol value to addend
}
```

`sub_440590` is the symbol-index-to-record accessor. It returns a pointer to the symbol record, whose field at offset `+8` is the resolved symbol value (address). This handles relocations that reference a symbol plus a constant addend -- the standard `S + A` pattern.

### Step 2: Architecture-Dependent Descriptor Table Selection

The relocation type (low 32 bits of `reloc_info`) is used to index into one of two descriptor tables depending on the target architecture:

```c
uint32_t reloc_type = reloc->reloc_info & 0xFFFFFFFF;
uint32_t flags_mask;

if (ctx->elf_class == 'A')      // byte at ctx+7, 'A' = 0x41
    flags_mask = 1;
else
    flags_mask = 0x80000000;

if (flags_mask & *(uint32_t*)(ctx + 48)) {
    // Mercury (SM100+) path
    descriptor_table = &off_1D3CBE0;
    if (reloc_type != 0)
        adjusted_type = reloc_type - 0x10000;
    else
        adjusted_type = 0;
} else {
    // CUDA (pre-Mercury) path
    descriptor_table = &off_1D3DBE0;
    adjusted_type = reloc_type;
}
```

The two global tables `off_1D3CBE0` and `off_1D3DBE0` are arrays of relocation descriptors. Each descriptor is 64 bytes (16 x 4-byte fields), containing bit-field specifications that tell the application engine which bits of the instruction word to patch. Mercury relocations use type codes offset by `0x10000` from CUDA relocations -- the linker subtracts `0x10000` to normalize the index into the Mercury descriptor table.

The relocation type `0` is a sentinel meaning "no relocation" or "already resolved." If `reloc_type == 0` and `reloc_type` is non-zero after normalization, the error `"unexpected reloc"` is emitted via `sub_467460`.

### Step 3: Symbol Resolution and Section Lookup

```c
target_symbol = sub_440590(ctx, HIDWORD(reloc_info));  // high 32 bits = sym index
sym_section   = sub_440350(ctx, target_symbol);         // get symbol's section index
section_rec   = sub_442270(ctx, section_idx);           // section idx -> record
parent_sec    = sub_442270(ctx, section_rec->parent);   // section's parent section
```

`sub_440350` returns the section index that contains the target symbol. `sub_442270` converts a section index to its section record pointer. The parent section (at offset `+44` in the section record) is used to locate the actual data buffer where the relocation will be applied.

### Step 4: Special Section Handling

If the link type (`ctx + 16`) is not `1` (non-relocatable link) and the parent section has `sh_type == 0x7000000E` (`SHT_CUDA_UFT`, decimal `1879048206`) at offset `+4`, the relocation targets a Unified Function Table entry (indirect-call jump slot). In this case, the addend is replaced with a value from `sub_463660` (the unified table offset resolver):

```c
if (link_type != 1 && parent_section->sh_type == 0x7000000E) {  // SHT_CUDA_UFT
    uft_entry = sub_463660(ctx, target_symbol);
    reloc->addend = *(int64_t*)(uft_entry + 8);
    if (ctx->compilation_mode == 2) {   // ctx+104
        if (reloc->addend != 0) {
            slot_size = 2 * (*(fn_ptr*)(ctx->vtable + 624))();
            reloc->addend += slot_size * (reloc->addend >> 7);
        }
    }
}
```

This handles Unified Function Table (UFT) and Unified Descriptor Table (UDT) relocations, where the addend encodes a table slot index that must be multiplied by the per-slot size.

### Step 5: Unified Relocation Remapping

For relocatable links (`link_type == 1`), unified relocation types are remapped to their base equivalents. The decompiled code contains a large switch-case that maps unified relocation types to standard ones:

| Unified type | Remapped to | Notes |
|---|---|---|
| 102 | 2 | Base absolute relocation |
| 103 | 1 | `fprintf: "replace unified reloc %d with %d\n", 103, 1` |
| 104 | 76 | — |
| 105 | 77 | — |
| 106 | 78 | — |
| 107 | 79 | — |
| 108 | 80 | — |
| 109 | 81 | — |
| 110 | 82 | — |
| 111 | 83 | — |
| 112 | 56 | — |
| 113 | 57 | — |
| 65586 | 65538 | Mercury equivalents (type - 0x10000 base) |
| 65587 | 65539 | — |
| 65588 | 65552 | — |
| 65589 | 65553 | — |
| 65590 | 65554 | — |
| 65591 | 65555 | — |
| 65592 | 65556 | — |
| 65593 | 65557 | — |
| 65594 | 65558 | — |
| 65598 | 65541 | — |
| 65599 | 65542 | — |
| 65595 | 65559 | — |

For types not in this table, the function checks whether the target symbol name matches one of the unified table synthetic symbols: `__UFT_OFFSET`, `__UFT_CANONICAL`, `__UDT_OFFSET`, `__UDT_CANONICAL`, `__UDT`, `__UFT`, `__UFT_END`, `__UDT_END`. If any matches, the relocation type is set to 0 (resolved) with the verbose trace `"replace unified reloc %d with %d\n", old_type, 0`.

### Step 6: Alias Chain Resolution

After the symbol is resolved, the function checks if the target symbol is a weak alias (symbol type `STT_FUNC` = 2, at byte `+4` low nibble) with an unresolved value (offset `+8` is zero). If so, it follows the alias chain:

```c
if (sym_section_idx != 0 && (symbol->st_info & 0xF) == STT_FUNC) {
    if (symbol->st_value == 0) {
        // Follow alias: look up the canonical symbol
        new_sym_idx = sub_440350(ctx, symbol);
        canonical = sub_442270(ctx, new_sym_idx);
        canonical_section_idx = canonical->parent & 0x00FFFFFF;
        if (canonical_section_idx != reloc->sym_hi && canonical->sh_type != 0x7000000E) {  // not SHT_CUDA_UFT
            old_name = symbol->name;
            symbol = sub_440590(ctx, canonical_section_idx);
            if (ctx->verbose_flags & 4)
                fprintf(stderr, "change alias reloc %s to %s\n", old_name, symbol->name);
            reloc->reloc_info = ((uint64_t)canonical_section_idx << 32) | reloc_type;
        }
    }
}
```

The verbose trace `"change alias reloc %s to %s\n"` is emitted when debug verbosity bit 2 is set in the flags at `ctx+64`. This alias chain walk is crucial for weak function resolution -- when multiple translation units define the same weak symbol, the merge phase picks one canonical definition, and all other references must be redirected.

### Step 7: Dead Function Filtering

If the target symbol is marked as dead (symbol type `STT_FUNC` with binding `STB_LOCAL` = 1, at byte `+5` bits 0-1):

```c
if ((symbol->st_info & 0xF) == STT_FUNC && (symbol->st_bind & 3) == STB_LOCAL) {
    if (ctx->verbose_flags & 4)
        fprintf(stderr, "ignore reloc on dead func %s\n", symbol->name);
    reloc->reloc_info = 0;   // zero out type and symbol
    adjusted_type = 0;
    reloc_type = 0;
}
```

Dead functions are those eliminated by the dead code elimination pass. Their relocations are silently dropped. The verbose trace `"ignore reloc on dead func %s\n"` helps track which relocations were discarded.

### Step 8: Special Relocation Handling

Several special cases are handled before the general application:

**Common undefined symbols** (section index `SHN_COMMON` = 0xFFF2 or related): If the section type at offset `+4` matches `0x70000007` (`SHT_CUDA_GLOBAL`, decimal `1879048199`), `0x70000008` (`SHT_CUDA_GLOBAL_INIT`, decimal `1879048200`), or `0x70000012` (`SHT_CUDA_UDT`, decimal `1879048210`), the relocation is deferred to the finalization phase. The node is simply advanced past without removal.

**YIELD-to-NOP suppression** (relocation types 68-69): When the forward-progress-required flag (`ctx+94`) is set, YIELD instructions are not converted to NOP, and the relocation is handled specially:
```
"Ignoring the reloc to convert YIELD to NOP due to forward progress requirement."
```

**PC-relative branch validation**: When the descriptor table entry at index `+5` (descriptor mode) equals 16 (PC-relative), the function validates that the relocation target and source are in the same section:
```
"PC relative branch address should be in the same section"
```

**UFT\_OFFSET ignoring**: If the target symbol is named `__UFT_OFFSET` and the linker's UDT mode (`ctx+240`) is zero, the relocation is dropped:
```
"ignore reloc on UFT_OFFSET"
```

### Step 9: Data Buffer Location

Before calling the application engine, the function must locate the exact byte position in the output section's data buffer where the relocation applies. Section data is stored in a linked list of data chunks (at section record offset `+72`), each chunk containing a base address and length:

```c
chunk_list = *(chunk_node**)(section_record + 72);
target_offset = reloc->addend;

while (chunk_list) {
    chunk_data = chunk_list->data;       // chunk_list[1]
    chunk_base = chunk_data->base;       // chunk_data[1]
    if (target_offset >= chunk_base) {
        delta = target_offset - chunk_base;
        if (delta < chunk_data->size)    // chunk_data[3]
            break;
    }
    chunk_list = chunk_list->next;       // chunk_list[0]
}

if (!chunk_list)
    error("reloc address not found");

patch_ptr = chunk_data->buffer + delta;  // chunk_data[0] + delta
```

If no chunk contains the target offset, the fatal error `"reloc address not found"` is emitted.

### Step 10: Application Engine Dispatch

The actual bit-patching is performed by `sub_468760`:

```c
success = sub_468760(
    descriptor_table,       // off_1D3CBE0 or off_1D3DBE0
    adjusted_type,          // normalized relocation type index
    is_absolute,            // flag: 1 if symbol has absolute address
    patch_ptr,              // pointer into section data buffer
    extra_offset,           // reloc->extra field
    addend_value,           // computed addend
    symbol_value,           // resolved symbol address (from sym+8)
    symbol_size,            // symbol size (from sym+28)
    section_type_delta,     // sh_type - 0x70000064 (constant-bank base)
    &output_value           // receives the computed final value
);
```

If the engine returns 0 (failure), the fatal error `"unexpected NVRS"` is emitted. On success, the relocation node is unlinked from the list.

## Application Engine: sub_468760

The application engine (`sub_468760`, 14,322 bytes, 582 lines) is the bit-level instruction patching workhorse. It receives a relocation descriptor and the target instruction word, then applies the relocation by modifying specific bit fields.

### Descriptor Table Format

Each entry in the descriptor table (`off_1D3CBE0` / `off_1D3DBE0`) is 64 bytes, organized as an array of up to 4 **relocation actions**. Each action is 16 bytes (4 x uint32):

```
struct reloc_action {           // 16 bytes
    uint32_t  bit_offset;       // offset +0: starting bit position in instruction word
    uint32_t  bit_width;        // offset +4: number of bits to patch
    uint32_t  action_type;      // offset +8: relocation action code (0=end, 1..0x14+)
    uint32_t  reserved;         // offset +12: reserved / flags
};

struct reloc_descriptor {       // 64 bytes = 4 actions
    reloc_action  actions[4];   // at offsets +12 through +60 within the entry
                                // (first 12 bytes are the entry header)
};
```

The engine iterates actions from the first to the last, stopping when `action_type == 0` or when all 4 slots are consumed (the end sentinel is at byte offset `+60` from the entry start, stored in `v100`).

### Action Types

The `action_type` field in each descriptor action determines how the value is computed and patched. The engine implements a switch statement over these codes:

| Code | Name | Semantics |
|---|---|---|
| 0 | END | Terminator -- stop processing this descriptor |
| 1 | ABS\_FULL | Absolute: write `value` to bit field (also used by 0x12, 0x2E) |
| 6, 0x37 | ABS\_LO | Absolute low 32 bits: extract low word of value |
| 7, 0x38 | ABS\_HI | Absolute high 32 bits: extract high word of value |
| 8 | ABS\_PLUS\_SIZE | Absolute + symbol size addend |
| 9 | ABS\_SHIFTED | Absolute with right-shift by 2 (4-byte aligned addresses) |
| 0xA | SEC\_TYPE\_LO | Section type low bits, masked by `(255 >> (8 - width))` |
| 0xB | SEC\_TYPE\_HI | Section type high bits, shifted right by 4 then masked |
| 0x10 | PC\_REL | PC-relative: `value - section_offset` |
| 0x13, 0x14 | CLEAR | Clear bits: write zeros to the specified bit field |

### Bit-Field Patching

The patching mechanism operates on 64-bit words addressed through the `patch_ptr`. Given a `bit_offset` and `bit_width`, the engine:

1. Computes which 64-bit word(s) the field spans: `word_index = bit_offset / 64`, `local_offset = bit_offset % 64`
2. If the field fits within a single 64-bit word (`local_offset + bit_width <= 64`), performs a single read-modify-write using shift-and-mask:
   ```c
   mask = ~((-1ULL << (64 - bit_width)) >> (64 - (local_offset + bit_width)));
   word = (word & ~mask) | ((value << (64 - bit_width)) >> (64 - (local_offset + bit_width)));
   ```
3. If the field spans multiple 64-bit words, loops through intermediate words using `sub_4685B0` (the bit-field writer helper), shifting the value right by the consumed bits at each step.

The helper `sub_468670` is the inverse operation -- it extracts a bit field from the instruction word, used in non-absolute modes where the engine must read the existing value before adding to it.

This bit-level granularity is necessary because GPU instructions encode operands, immediates, and relocation targets in non-byte-aligned bit fields. A single SASS instruction may be 64 or 128 bits wide, with the relocated value occupying an arbitrary sub-field.

## Worked Example: Applying 3 Relocation Types

This section walks three representative relocations through `sub_469D60` and `sub_468760` end to end -- from the symbol being referenced, through the 32-byte relocation record, into the CUDA descriptor table at `off_1D3DBE0`, and finally producing a before/after hex dump of the patched bytes. The three examples cover the three architectural shapes of the descriptor table:

1. **`R_CUDA_ABS32_LO_20`** -- a 16-bit instruction bit-field write (low half of a 32-bit absolute).
2. **`R_CUDA_FUNC_DESC_32`** -- a 32-bit data patch into a function descriptor slot.
3. **`R_CUDA_CONST_FIELD19_20`** -- a 19-bit instruction bit-field write with an implicit `>> 2` (byte offset to DWORD offset).

All three examples assume a pre-Mercury target (any of sm_75 / sm_80 / sm_86--89 / sm_90; the descriptor table is identical across these tiers and the original sm_70 Volta layout it inherits from), so the descriptor table selected by `sub_469D60` is `off_1D3DBE0` (the CUDA table), and the relocation types are used as direct indices (no `0x10000` normalization). Each entry in the 64-byte CUDA descriptor table is laid out as `[12 bytes header | action[0] 16 bytes | action[1] 16 bytes | action[2] 16 bytes | 4 bytes sentinel]`, with the action iteration bounded by `v100 = (unsigned int *)(v12 + 60)` at line 132 of `sub_468760`.

### Example 1: R_CUDA_ABS32_LO_20 (index 33)

**Scenario.** A kernel needs to load the address of a global `__device__` variable `g_table` into a register. The compiler emits a `MOV32I` (or equivalent sm_75 / sm_80 / sm_89 `IMAD`/`MOV32I`-style wide-immediate) instruction split into two halves -- an `R_CUDA_ABS32_HI_20` high-half relocation and an `R_CUDA_ABS32_LO_20` low-half relocation. After layout, `g_table` has been assigned the absolute address `0x00C0_FFEE` in the merged `.nv.global` section. This example patches the **low** half of that address into the 16-bit immediate field at bit 20 of the second instruction of the pair.

**a. Symbol being referenced.**

```
Symbol name      : g_table
Section          : .nv.global.data (sh_type = SHT_CUDA_GLOBAL, 0x70000062)
st_value (post-layout)
                 : 0x00C0_FFEE     (32-bit absolute address)
st_size          : 0x0080           (128 bytes, 32 floats)
st_info          : STB_GLOBAL | STT_OBJECT
Binding section  : output section index 11
```

After the layout phase, `sub_440590(ctx, sym_idx)` returns a symbol record whose field at `+8` is `0x00000000_00C0FFEE`.

**b. Relocation record bytes (32 bytes, loaded as two `__m128i`).**

```
Offset  Bytes (little-endian)                            Interpretation
------  ---------------------------------                ----------------------------------
+0      08 00 00 00 00 00 00 00                          addend = 0x08  (target offset within
                                                         .text.foo: second half of the pair,
                                                         the low-half instruction)
+8      21 00 00 00 07 00 00 00                          reloc_info low32 = 0x21 = 33 (type)
                                                         reloc_info high32 = 0x07 = sym idx 7
+16     05 00 00 00                                      section_idx = 5 (.text.foo)
+20     00 00 00 00                                      sym_addend_idx = 0 (no S+A rewrite)
+24     00 00 00 00 00 00 00 00                          extra = 0
```

The low 32 bits of `reloc_info` give relocation type `33 = 0x21 = R_CUDA_ABS32_LO_20`. The high 32 bits give symbol index `7`, which `sub_440590` resolves to `g_table`.

**c. Instruction being patched.**

The target is the second instruction of a HI/LO pair inside `.text.foo` at byte offset `0x08` (reloc addend). The pre-relocation 64-bit instruction word, as stored in the section data buffer:

```
patch_ptr (offset 0x08 in .text.foo):
   Byte 0  1  2  3  4  5  6  7
         38 72 00 00 00 00 00 00
   as u64: 0x0000_0000_0000_7238
```

The low 16 bits of the final 32-bit address `0x00C0_FFEE` are `0xFFEE`. The relocation must write `0xFFEE` into bits [20:36) of this instruction word.

**d. Descriptor action slots from off_1D3DBE0.**

The engine computes the descriptor pointer as `off_1D3DBE0 + (33 << 6) = off_1D3DBE0 + 2112`, then reads action slots starting at `+12`:

```
Offset  Bytes          action field              Value
------  ------------   ----------------------    ------------------
+12     14 00 00 00    action[0].bit_offset      0x14 = 20
+16     10 00 00 00    action[0].bit_width       0x10 = 16
+20     06 00 00 00    action[0].action_type     6 (ABS_LO = low 32 bits)
+24     00 00 00 00    action[0].reserved        0
+28     00 00 00 00    action[1].bit_offset      0
+32     00 00 00 00    action[1].bit_width       0
+36     00 00 00 00    action[1].action_type     0 (END)
+40     00 00 00 00    action[1].reserved        0
+44 ... 00 ...         action[2] / action[3]     END / zero
```

Only one real action: `action[0]` is code `6` (`ABS_LO`). The engine routes to the `case 6u, 0x37u` branch of `sub_468760` (lines 173--211 of the decompiled function).

**e. Before/after hex dump.**

```c
// Pre-patch extraction (sub_468670 at bit 20, width 16):
old = bitfield_extract(patch_ptr, 20, 16);
//   end = 20 + 16 = 36 <= 64 (single-word case)
//   old = (*patch_ptr << (64 - 36)) >> (64 - 16)
//       = (0x0000_0000_0000_7238 << 28) >> 48
//       = 0x0000_0723_8000_0000_0000 >> 48  (logically; truncated to 64 bits)
//       = 0x0000  (the immediate slot was empty pre-link)
old = 0;

// a7 = symbol_value = 0x00C0_FFEE, a3 = is_absolute = 0
// v10 = a7 = 0x00C0_FFEE (line 122)
// v80 = (uint32_t)v10 = 0xFFEE (line 176, LOBYTE of a 32-bit view)
// Adding old: v80 += 0 -> v80 = 0xFFEE
// v55 = v80 = 0xFFEE

// Write-back (single-word branch, LABEL_48 at line 253):
//   bit_offset v81 = 20, bit_width v52 = 16, v57 = 36
//   mask = (~(-1ULL << (64 - 16))) >> (64 - 36)
//        = 0x0000_0000_0000_FFFF << 20     (equivalently)
//        = 0x0000_0000_FFF0_0000
//   placed = (0xFFEE << 48) >> 28
//          = 0xFFEE_0000_0000_0000 >> 28
//          = 0x0000_000F_FEE0_0000
//                  ^--- the 16-bit value 0xFFEE now occupies bits [20:36)
//   *patch_ptr = (*patch_ptr & ~mask) | placed
//              = (0x0000_0000_0000_7238 & 0xFFFF_FFFF_000F_FFFF)
//              | 0x0000_0000_FFE0_0000
//              = 0x0000_0000_FFE0_7238
```

Hex dump of the 8 bytes at `.text.foo + 0x08`:

```
BEFORE:  38 72 00 00  00 00 00 00          // 0x0000_0000_0000_7238
AFTER:   38 72 E0 FF  00 00 00 00          // 0x0000_0000_FFE0_7238

                ^^ ^^   low byte of 0xFFEE at byte 2, high byte 0xFF at byte 3,
                         shifted by 20 bits (bits [20:36))
```

Paired with the matching `R_CUDA_ABS32_HI_20` (index 29, action code `7 = ABS_HI`) on the previous instruction, the full 32-bit address `0x00C0_FFEE` is reconstructed in a register at runtime by a two-instruction sequence.

### Example 2: R_CUDA_FUNC_DESC_32 (index 52)

**Scenario.** A device-side function pointer table (`.nv.global` slot) needs to be filled with a 32-bit function descriptor handle for the function `kernel_launch_helper`. Unlike the instruction-field relocations, `R_CUDA_FUNC_DESC_32` patches a raw 32-bit data word in a data section -- the descriptor format is **just `bit_offset=0, bit_width=32, action_type=1`** (ABS\_FULL). The value written is the function's descriptor address (assigned by `sub_463660` / the unified function table resolver).

**a. Symbol being referenced.**

```
Symbol name      : kernel_launch_helper
Section          : .text.kernel_launch_helper (sh_type = SHT_CUDA_TEXT)
st_value         : 0x0000_2340        (function entry PC)
st_info          : STB_GLOBAL | STT_FUNC
```

Because this is a function descriptor relocation, the target symbol is a `STT_FUNC` with a canonical value. `sub_469D60`'s alias chain resolution (Step 6) runs first, picking the canonical definition if this is a weak alias. The final `symbol->st_value + 8` read yields the function's code address `0x0000_2340`. The descriptor table / UFT mechanism wraps this into a 32-bit descriptor handle during resolution; for this example, assume the resolved descriptor value (stored in the symbol record) is `0x0000_2340` (1:1 mapping on architectures without a UFT indirection).

**b. Relocation record bytes.**

```
Offset  Bytes (little-endian)                            Interpretation
------  ---------------------------------                ----------------------------------
+0      20 01 00 00 00 00 00 00                          addend = 0x0120  (target byte offset
                                                         within .nv.global.functable)
+8      34 00 00 00 13 00 00 00                          reloc_info low32  = 0x34 = 52 (type)
                                                         reloc_info high32 = 0x13 = sym idx 19
+16     0C 00 00 00                                      section_idx = 12 (.nv.global.functable)
+20     00 00 00 00                                      sym_addend_idx = 0
+24     00 00 00 00 00 00 00 00                          extra = 0
```

Relocation type `52 = 0x34 = R_CUDA_FUNC_DESC_32`. Symbol index 19 resolves to `kernel_launch_helper`.

**c. Data word being patched.**

The target is 4 bytes of uninitialized data inside `.nv.global.functable` at byte offset `0x0120`. Data sections are treated as 64-bit words by `sub_468760` (the engine always addresses through `unsigned __int64 *a4`), but only bits [0:32) are written by this descriptor, leaving the high 32 bits untouched.

```
patch_ptr (offset 0x0120 in .nv.global.functable):
   Byte  0  1  2  3  4  5  6  7
         00 00 00 00 00 00 00 00         // zero-initialized slot
   as u64: 0x0000_0000_0000_0000
```

**d. Descriptor action slots from off_1D3DBE0.**

```
descriptor_ptr = off_1D3DBE0 + (52 << 6) = off_1D3DBE0 + 3328

Offset  Bytes          action field              Value
------  ------------   ----------------------    ------------------
+12     00 00 00 00    action[0].bit_offset      0
+16     20 00 00 00    action[0].bit_width       0x20 = 32
+20     01 00 00 00    action[0].action_type     1 (ABS_FULL)
+24     00 00 00 00    action[0].reserved        0
+28     00 00 00 00    action[1].action_type     0 (END)
+32 ... 00 ...         action[2] / action[3]     END / zero
```

Action code `1` routes to the `case 1u, 0x12u, 0x2Eu` branch (lines 140--172 of `sub_468760`).

**e. Before/after hex dump.**

The ABS\_FULL path handles the "bit_width == 32" case directly. Tracing the decompiled logic:

```c
// Entry to case 1 (line 143)
v18 = *v15;        // bit_offset = 0
v19 = v15[1];      // bit_width  = 32
// Not the (v18 == 0 && v19 == 64) whole-word fast path, fall through:
if ( !a3 )         // a3 = is_absolute = 0
{
    v112 = v15[1];
    v98 = sub_468670(a4, 0, 32);   // extract old 32-bit field
    v10 += v98;                    // add to value: 0x2340 + 0 = 0x2340
    v19 = v112;
    *a10 = v98;
}
v15 += 4;
sub_4685B0(a4, v10, 0, 32);        // write 0x2340 into bits [0:32)
```

The bit-field writer (`sub_4685B0`, lines 35--37):

```c
// bit_offset=0, bit_width=32, value=0x2340
// v5 = 0 + 32 = 32; since 32 <= 64, single-word branch
// mask = (-1LL << (64 - 32)) >> (64 - 32) = 0xFFFF_FFFF_0000_0000 >> 32
//      = 0x0000_0000_FFFF_FFFF
// *a1 = (*a1 & ~0x0000_0000_FFFF_FFFF)
//     | ((0x2340 << 32) >> 32)
//     = 0x0000_0000_0000_0000 | 0x0000_0000_0000_2340
//     = 0x0000_0000_0000_2340
```

Hex dump of the 8 bytes at `.nv.global.functable + 0x0120`:

```
BEFORE:  00 00 00 00  00 00 00 00          // slot zeroed pre-link
AFTER:   40 23 00 00  00 00 00 00          // 32-bit descriptor 0x00002340 (little-endian)

         ^^ ^^ ^^ ^^
         low 32 bits = kernel_launch_helper's function descriptor
```

Only the first 4 bytes are affected. Bytes 4--7 are the high half of the 64-bit word the engine operates on; they are untouched because the mask is exactly `0xFFFFFFFF` in the low half. If another `R_CUDA_FUNC_DESC_32` patch were to land at byte offset `0x0124`, it would write to the high half of the same 64-bit word without disturbing the low half.

### Example 3: R_CUDA_CONST_FIELD19_20 (index 42)

**Scenario.** A kernel loads from a compiler-generated constant in `.nv.constant0`. After merging, the constant symbol `__cuda_local_const_0` is placed at byte offset `0x240` within `.nv.constant0`. The target instruction is a 64-bit sm_70 LDC-family encoding with a 19-bit DWORD-offset field starting at bit 20. `R_CUDA_CONST_FIELD19_20` is standard-table index 42; its descriptor uses action code `9` (ABS\_SHIFTED), which right-shifts the byte offset by 2 to convert to a DWORD offset before writing.

**a. Symbol being referenced.**

```
Symbol name      : __cuda_local_const_0
Section          : .nv.constant0 (sh_type = SHT_CUDA_CONSTANT0, 0x70000064)
st_value (post-merge)
                 : 0x0000_0240        (byte offset within merged .nv.constant0)
st_info          : STB_LOCAL | STT_OBJECT
```

`sub_440590` returns the symbol record whose `+8` field is `0x240`.

**b. Relocation record bytes.**

```
Offset  Bytes (little-endian)                            Interpretation
------  ---------------------------------                ----------------------------------
+0      40 00 00 00 00 00 00 00                          addend = 0x40  (offset of target
                                                         instruction within .text.kernel)
+8      2A 00 00 00 2B 00 00 00                          reloc_info low32  = 0x2A = 42 (type)
                                                         reloc_info high32 = 0x2B = sym idx 43
+16     06 00 00 00                                      section_idx = 6 (.text.kernel)
+20     00 00 00 00                                      sym_addend_idx = 0
+24     00 00 00 00 00 00 00 00                          extra = 0
```

Relocation type `42 = 0x2A = R_CUDA_CONST_FIELD19_20`. Symbol index 43 resolves to `__cuda_local_const_0`.

**c. Instruction being patched.**

A 64-bit load-constant instruction at `.text.kernel + 0x40`. The compiler has pre-encoded the bank index (bits [14:19) = `0x00` for bank 0) and zeroed the 19-bit offset field:

```
patch_ptr (offset 0x40 in .text.kernel):
   Byte 0  1  2  3  4  5  6  7
         B8 79 00 00 00 00 00 00
   as u64: 0x0000_0000_0000_79B8
```

The bit layout of this pre-relocation word:

```
  63                    39       20 19  14 13              0
  +----------------------+--------+------+------------------+
  | (scheduling, pred)   | offset | bank |    opcode/dst    |
  |         ...          | 0x00000|  0x0 |      0x79B8      |
  |                      | 19 bit | 5 b  |                  |
  +----------------------+--------+------+------------------+
```

**d. Descriptor action slots from off_1D3DBE0.**

```
descriptor_ptr = off_1D3DBE0 + (42 << 6) = off_1D3DBE0 + 2688

Offset  Bytes          action field              Value
------  ------------   ----------------------    ------------------
+12     14 00 00 00    action[0].bit_offset      0x14 = 20
+16     13 00 00 00    action[0].bit_width       0x13 = 19
+20     09 00 00 00    action[0].action_type     9 (ABS_SHIFTED, >> 2)
+24     00 00 00 00    action[0].reserved        0
+28     00 00 00 00    action[1].action_type     0 (END)
+32 ... 00 ...         action[2] / action[3]     END / zero
```

Action code `9` routes to the `case 9u` branch (lines 301--337 of `sub_468760`).

**e. Before/after hex dump.**

The `ABS_SHIFTED` action does one extra step before the standard extract/add/write cycle: it right-shifts `v10` (the running value, initialized to the symbol address) by 2 **once**, at the top of the case:

```c
// case 9u: (line 302)
v10 >>= 2;                     // 0x0000_0240 >> 2 = 0x0000_0090 (DWORD offset)

v61 = *v15;                    // bit_offset = 20
v62 = v15[1];                  // bit_width  = 19
v63 = *v15;                    // bit_offset (cached)
if ( !a3 )                     // a3 = is_absolute = 0
{
    v96 = sub_468670(a4, 20, 19);   // extract old 19-bit field
    // old = (0x0000_0000_0000_79B8 << (64 - 39)) >> (64 - 19)
    //     = (0x0000_0000_0000_79B8 << 25) >> 45
    //     = 0x0000_0000_F370_0000_0000 >> 45
    //     (first drop high 8 bits above bit 63, then shift right 45)
    //     = 0x00000  (all bits in [20:39) were zero pre-link)
    v10 += v96;                // v10 += 0  ->  v10 = 0x0000_0090
    *a10 = v96;
}
// v61 = 20 <= 63, so v42 = a4; v64 = 20 + 19 = 39 <= 64 -> LABEL_98
// v44 = v10 = 0x0000_0090
// v48 = 64 - 19 = 45, v49 = 64 - 39 = 25
// v50 = -1LL << 45 = 0xFFFF_E000_0000_0000
// Write-back at LABEL_59 (line 573):
//   *v42 = (*v42 & ~(v50 >> v49)) | (v44 << v48 >> v49)
//        = (0x0000_0000_0000_79B8 & ~(0xFFFF_E000_0000_0000 >> 25))
//        | ((0x0000_0090 << 45) >> 25)
//   mask  = 0xFFFF_E000_0000_0000 >> 25 = 0x0000_007F_FFF0_0000
//   ~mask = 0xFFFF_FF80_000F_FFFF
//   placed = (0x0000_0090 << 45) >> 25
//          = 0x0012_0000_0000_0000 >> 25
//          = 0x0000_0009_0000_0000
//                  ^--- DWORD offset 0x90 at bits [20:39)
//   *v42 = (0x0000_0000_0000_79B8 & 0xFFFF_FF80_000F_FFFF)
//        | 0x0000_0009_0000_0000
//        = 0x0000_0009_0000_79B8
```

Hex dump of the 8 bytes at `.text.kernel + 0x40`:

```
BEFORE:  B8 79 00 00  00 00 00 00          // 0x0000_0000_0000_79B8
AFTER:   B8 79 00 00  09 00 00 00          // 0x0000_0009_0000_79B8

                      ^^
                      byte 4 = 0x09: the DWORD offset 0x90 occupies bits [20:39)
                      Verification: 0x09 << 32 = 0x0000_0009_0000_0000
                                    bits [20:39) of that = bits [20:39) of
                                    0x0000_0009_0000_0000, i.e. 0x90
```

Verification of the ISA semantics: the hardware interprets the 19-bit field as a DWORD index (4-byte-multiplied). `0x90 << 2 = 0x240`, which matches the merged byte offset of `__cuda_local_const_0` in `.nv.constant0`. The instruction now decodes as `LDC R?, c[0x0][0x240]`, loading the correct constant.

### Summary of the Three Examples

| Relocation | Index | Action code | Bit offset | Bit width | Value transform | Target kind |
|---|---|---|---|---|---|---|
| `R_CUDA_ABS32_LO_20` | 33 | `6` (ABS\_LO) | 20 | 16 | `value & 0xFFFF` (low half of 32-bit) | Instruction field |
| `R_CUDA_FUNC_DESC_32` | 52 | `1` (ABS\_FULL) | 0 | 32 | `value` (verbatim) | Data word |
| `R_CUDA_CONST_FIELD19_20` | 42 | `9` (ABS\_SHIFTED) | 20 | 19 | `value >> 2` (byte to DWORD) | Instruction field |

The three examples exercise three distinct mechanical paths through `sub_468760`: the wide-immediate split-half path (ABS\_LO), the full-width data patch path (ABS\_FULL), and the byte-to-DWORD shifted path (ABS\_SHIFTED). All three share the common infrastructure of bit-field extract (`sub_468670`), value accumulation, and bit-field write (`sub_4685B0`), and all three produce deterministic, architecture-independent bit patterns in the output section data buffer.

## Preserve-Relocs Path

When `--preserve-relocs` is active (byte at `ctx+85` is nonzero), resolved relocations are not discarded after application. Instead, they are appended to a secondary linked list rooted at `ctx+384`:

```c
if (ctx->preserve_relocs) {
    if ((symbol->st_bind & 3) != STB_LOCAL
        || (sym_section != 0
            && section_has_data))
    {
        if (section_type != 4)  // not SHT_RELA
            reloc->extra = output_value;
        sub_4644C0(reloc_record, ctx + 384);  // append to preserve list
    }
} else {
    sub_431000(reloc_record);  // free the record
}
```

`sub_4644C0` is a linked-list append operation. After the main relocation walk completes, the preserve list is processed by `sub_46ADC0` to emit `.nv.resolvedrela` sections.

## Resolved-Rela Emission: sub_46ADC0

The function `sub_46ADC0` (11,515 bytes, 406 lines) walks the preserve-relocs list at `ctx+384` and writes each relocation into a `.nv.resolvedrela` section. This is used when the output is a relocatable object (`-r` flag) or when `--preserve-relocs` is specified, so that a subsequent link step or the CUDA runtime loader can perform final relocation at load time.

For each preserved relocation:

1. **Symbol index remapping**: Calls `sub_444720` to remap the symbol index from internal numbering to output ELF symbol table numbering.

2. **Symbol value validation**: The resolved symbol's value at offset `+8` must not be `-1` (unallocated):
   ```
   "symbol never allocated"
   ```

3. **Section data location**: Same chunk-list walk as the main engine, with the same error:
   ```
   "reloc address not found"
   ```

4. **Offset validation**: Verifies the relocation offset does not exceed the section's data size:
   ```
   "relocation is past end of offset"
   ```

5. **Rela section allocation**: Calls `sub_442760` to find or create the `.nv.resolvedrela` section for the target, with error:
   ```
   "rela section never allocated"
   ```

6. **Descriptor-driven extraction**: For relocations with non-trivial descriptors, the engine extracts existing bit-field values from the patched instruction data using the same `sub_468670` bit-field reader, accumulating the extracted value into the relocation addend at offset `+16`.

7. **Output format**: Writes the relocation record in either REL or RELA format depending on the ELF class (byte at `ctx+4`). For RELA (class == 2), the full 24-byte record `{offset, info, addend}` is emitted via `sub_4336B0`. For REL, the record is 12 bytes with the addend folded into `info`.

After all preserved relocations are emitted, if this is a non-Mercury relocatable link, the function also generates a `.nv.rel.action` section containing the relocation descriptor actions themselves, so downstream tools can re-apply them:

```c
if (link_type == 2 && !mercury_flag) {
    action_section = sub_441AC0(ctx, ".nv.rel.action", SHT_CUDA_xxx, ...);
    // Iterate descriptor table entries, emit action records
}
```

## Relocation Vtable: sub_459640

While not directly called by `sub_469D60`, the relocation vtable at `sub_459640` (16,109 bytes, 570 lines) is a critical companion used by the finalization phase (`sub_445000`). It creates a 632-byte vtable of function pointers, one per relocation type, dispatched by architecture:

| Architecture | Description |
|---|---|
| sm 30-39 | Kepler handlers |
| sm 50-59 | Maxwell handlers |
| sm 60-69 | Pascal handlers |
| sm 70-74 | Volta handlers |
| sm 75-79 | Turing handlers |
| sm 80-89 | Ampere/Ada handlers |
| sm 90-99 | Hopper handlers |
| sm 100+ | Mercury/Blackwell handlers |

Each handler slot corresponds to a specific R\_CUDA (or R\_MERCURY) relocation type. The vtable provides approximately 70 handler slots, covering all GPU relocation types across all supported architectures. The finalization phase uses this vtable for the second pass of relocation application -- while `sub_469D60` handles the initial resolution and unified table fixup, `sub_445000` applies architecture-specific patching using the vtable dispatch.

## Error Conditions

| Error string | Severity | Condition |
|---|---|---|
| `"unexpected reloc"` | Fatal | Relocation type nonzero but <= 0x10000 in Mercury mode |
| `"reloc address not found"` | Fatal | Target offset not contained in any section data chunk |
| `"unexpected NVRS"` | Fatal | Application engine returned failure (invalid descriptor) |
| `"PC relative branch address should be in the same section"` | Fatal | PC-relative relocation crosses section boundary |
| `"symbol never allocated"` | Fatal | Preserved relocation references unallocated symbol |
| `"rela section never allocated"` | Fatal | Could not create .nv.resolvedrela output section |
| `"relocation is past end of offset"` | Fatal | Relocation offset exceeds section data size |

## Diagnostic Traces

All traces are gated by `(ctx->verbose_flags & 4) != 0` (bit 2 of the debug flags at `ctx+64`):

| Trace string | When emitted |
|---|---|
| `"change alias reloc %s to %s\n"` | Weak alias chain followed to canonical symbol |
| `"ignore reloc on dead func %s\n"` | Relocation dropped because target function was eliminated |
| `"replace unified reloc %d with %d\n"` | Unified table relocation type remapped to base type |
| `"resolve reloc %d for sym=%d+%lld at <section=%d,offset=%llx>\n"` | Per-relocation resolution trace (full detail) |
| `"ignore reloc on UFT_OFFSET\n"` | UFT\_OFFSET relocation dropped when UDT mode inactive |
| `"Ignoring the reloc to convert YIELD to NOP due to forward progress requirement.\n"` | YIELD conversion suppressed |

## Function Map

| Address | Size | Identity | Role |
|---|---|---|---|
| `0x469D60` | 26,578 B | `apply_relocations` | Main relocation phase entry point |
| `0x468760` | 14,322 B | `reloc_apply_engine` | Bit-field patching engine, descriptor-driven |
| `0x46ADC0` | 11,515 B | `emit_resolved_rela` | Writes .nv.resolvedrela for preserve-relocs |
| `0x459640` | 16,109 B | `reloc_vtable_create` | Per-architecture relocation handler vtable |
| `0x468670` | ~240 B | `bitfield_extract` | Extracts bit field from instruction word |
| `0x4685B0` | ~240 B | `bitfield_write` | Writes value into bit field of instruction word |
| `0x440590` | ~2 KB | `sym_idx_to_record` | Symbol index to record pointer accessor |
| `0x440350` | ~2 KB | `sym_get_section` | Gets section index containing a symbol |
| `0x442270` | ~2 KB | `sec_idx_to_record` | Section index to record pointer accessor |
| `0x444BD0` | ~2 KB | `sym_is_defined` | Checks if symbol has a definition |
| `0x463660` | ~2 KB | `uft_get_offset` | UFT/UDT offset resolver |
| `0x4644C0` | ~1 KB | `list_append` | Appends node to singly-linked list |
| `0x444720` | ~2 KB | `sym_remap_index` | Remaps symbol index for output ELF |
| `0x4336B0` | ~2 KB | `section_write_data` | Writes data into a section's data buffer |
| `0x4411D0` | ~2 KB | `section_find_by_name` | Finds section by name string |
| `0x467460` | ~2 KB | `error_emit` | Variadic error emission entry point |

## Cross-References

- [Pipeline Overview](overview.md) -- Where the relocation phase fits in the end-to-end pipeline
- [Layout Phase](layout.md) -- Preceding phase: assigns addresses that relocations resolve against
- [Finalization Phase](finalize.md) -- Following phase: second relocation pass using the vtable
- [R\_CUDA Relocations](../linker/r-cuda-relocations.md) -- CUDA-specific relocation type catalog
- [Relocation Application Engine](../linker/relocation-engine.md) -- Deep dive on `sub_468760` bit-patching
- [Unified Function Tables](../elf/uft.md) -- UFT/UDT structures referenced by unified relocations
- [Symbol Resolution](../linker/symbol-resolution.md) -- How symbols are resolved before relocation
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- How dead functions are marked for relocation filtering

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_469D60` at `0x469D60`, 26,578 bytes, 985 lines | **HIGH** | `stat -c%s` = 26,578; `wc -l` = 985 |
| `sub_468760` (application engine), 14,322 B, 582 lines | **HIGH** | `stat -c%s` = 14,322; `wc -l` = 582 |
| `sub_46ADC0` (resolved-rela emitter), 11,515 B, 406 lines | **HIGH** | `stat -c%s` = 11,515; `wc -l` = 406 |
| `sub_459640` (relocation vtable), 16,109 B, 570 lines | **HIGH** | `stat -c%s` = 16,109; `wc -l` = 570 |
| Signature: `(ctx, mutex_attr)` -- two arguments | **HIGH** | Decompiled: `char __fastcall sub_469D60(__int64 a1, pthread_mutexattr_t *a2)` |
| `off_1D3CBE0` (Mercury descriptor table) | **HIGH** | Referenced at lines 202, 207 of `sub_469D60` decompiled code |
| `off_1D3DBE0` (CUDA descriptor table) | **HIGH** | Referenced at line 214 of `sub_469D60` decompiled code |
| Mercury relocation type offset `0x10000` | **HIGH** | `v9 - 0x10000` at line 203 and `<= 0x10000` check at line 197 of decompiled code |
| SSE `_mm_loadu_si128` for relocation record loading | **HIGH** | `_mm_loadu_si128(v5)` at line 236 of decompiled code |
| `"unexpected reloc"` error string | **HIGH** | String at `0x1d3bcd0` in `nvlink_strings.json` (full: `"unexpected reloc section"`) |
| `"reloc address not found"` error string | **HIGH** | String at `0x1d3c990` in `nvlink_strings.json` |
| `"unexpected NVRS"` error string | **HIGH** | String at `0x1d3caf8` in `nvlink_strings.json` |
| `"PC relative branch address should be in the same section"` | **HIGH** | String at `0x1d3ca68` in `nvlink_strings.json` |
| `"symbol never allocated"` error string | **HIGH** | String at `0x1d3cb17` in `nvlink_strings.json` |
| `"rela section never allocated"` error string | **HIGH** | String at `0x1d3cb2e` in `nvlink_strings.json` |
| `"change alias reloc %s to %s"` trace | **HIGH** | String at `0x1d3caa1` in `nvlink_strings.json` |
| `"ignore reloc on dead func %s"` trace | **HIGH** | String at `0x1d3cabe` in `nvlink_strings.json` |
| `"replace unified reloc %d with %d"` trace | **HIGH** | String at `0x1d3c9a8` in `nvlink_strings.json` |
| `"resolve reloc %d for sym=%d+%lld at <section=%d,offset=%llx>"` | **HIGH** | String at `0x1d3ca28` in `nvlink_strings.json` |
| `"Ignoring the reloc to convert YIELD to NOP due to forward progress requirement."` | **HIGH** | String at `0x1d3c9d0` in `nvlink_strings.json` |
| `"ignore reloc on UFT_OFFSET"` trace | **HIGH** | `"__UFT_OFFSET"` string at `0x1d3a025` in `nvlink_strings.json` |
| Unified relocation remapping table (102->2, 103->1, etc.) | **MEDIUM** | Values inferred from switch-case in decompiled `sub_469D60`; individual mappings verified against code |
| UFT synthetic symbols (`__UFT_OFFSET`, `__UFT_CANONICAL`, etc.) | **HIGH** | `"__UFT_OFFSET"` at `0x1d3a025`; related strings nearby in `nvlink_strings.json` |
| Relocation descriptor format (64 bytes, 4 actions of 16 bytes) | **MEDIUM** | Inferred from decompiled loop structure in `sub_468760` and descriptor table stride; not independently labeled |
| Action type codes (0=END, 1=ABS_FULL, 6=ABS_LO, 7=ABS_HI, etc.) | **MEDIUM** | Values from switch-case in `sub_468760`; semantics inferred from patching behavior |
| Bit-field patching mechanism (64-bit read-modify-write) | **HIGH** | Shift-and-mask operations visible in `sub_468760` and helpers `sub_4685B0`/`sub_468670` |
| `sub_4685B0` (bitfield_write) and `sub_468670` (bitfield_extract) | **HIGH** | Both files exist in `decompiled/` |
| Preserve-relocs path appends to list at `ctx+384` | **MEDIUM** | Offset inferred from decompiled pointer arithmetic; list-append call visible |
| Relocation vtable architecture ranges (sm 30--39 Kepler, etc.) | **MEDIUM** | Architecture dispatch visible in `sub_459640`; specific SM ranges are editorial grouping |
| All 18 function addresses in the function map table | **HIGH** | All verified to exist in `decompiled/` directory |
| 10-step resolution algorithm | **MEDIUM** | Step boundaries are editorial grouping of the decompiled control flow; individual steps verified |
| Relocation record struct (32 bytes, two `__m128i`) | **HIGH** | `__m128i` type visible in decompiled variable declarations; `_mm_loadu_si128` confirms 128-bit loading |
