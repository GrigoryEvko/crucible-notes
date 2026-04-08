# Bindless Relocations

Bindless texture and surface references in CUDA allow kernels to access texture/surface objects through handles stored in constant memory rather than through fixed hardware slots. The nvlink device linker implements bindless support through a dedicated relocation processing pass (`sub_438DD0`, 12,779 bytes at `0x438DD0`) that runs during the layout phase. This function scans all relocations for bindless types, creates synthetic `$NVLINKBINDLESSOFF_<name>` symbols, rewrites relocation symbol indices to point at these synthetic symbols, builds a per-section bitmask tracking which sections contain bindless references, and then allocates constant-bank space for the bindless offset tables. A companion function (`sub_43CDA0`, 6,937 bytes at `0x43CDA0`) resolves bindless symbol types and counts resources per entry function.

## Background: Bindless vs. Bound Texture Access

Traditional (bound) CUDA texture access assigns each texture/surface to a fixed hardware slot, with per-architecture limits on slot count (queried via vtable offsets `+40`, `+48`, `+56` for textures, samplers, and surfaces respectively). Bindless access eliminates this fixed-slot limitation: the compiler emits a handle-based reference that the linker resolves to an offset within a constant memory bank dedicated to bindless descriptors.

The CUDA source-level mechanism works as follows:

1. **PTX frontend** (`sub_12B9660`): The ptxas compiler creates `$BINDLESS$<module>$<texture>$<sampler>` or `$BINDLESS$<module>$<surface>` symbols for each bindless texture/surface/sampler triple.
2. **Merge phase**: These symbols and their relocations are merged into the output ELF during `merge_elf` (`sub_45E7D0`).
3. **Layout phase**: `sub_438DD0` (this page) processes the merged relocations, creating `$NVLINKBINDLESSOFF_<name>` symbols and assigning them offsets within a dedicated constant bank section.
4. **Relocation application**: The standard relocation engine patches the final addresses.

The architecture profile vtable controls whether bindless is supported at all:

| Vtable offset | Query | Description |
|---|---|---|
| `+296` | `supports_bindless()` | Gate: returns nonzero if bindless mode is active |
| `+304` | `bindless_texture_type()` | Returns the ELF section type for the bindless texture constant bank |
| `+312` | `bindless_surface_type()` | Returns the ELF section type for the bindless surface constant bank |
| `+320` | (unnamed) | Returns nonzero if unified surface descriptors are in use |
| `+440` | (unnamed) | Size of a texture/sampler descriptor entry |
| `+448` | (unnamed) | Base size for a surface descriptor entry |
| `+352` | (unnamed) | Additional surface descriptor offset for non-unified mode |

The constant memory bank holding bindless descriptors is identified by `sw-bindless-tex-surf-table-bank` (from `sub_16257C0` in the ptxas memory-space classifier).

## Position in the Pipeline

```
Merge Phase (sub_45E7D0, per-object)
  |
  v
Layout Phase (sub_439830)
  |
  +-- Phase 1: Global data merge
  +-- Phase 2: Bindless resource processing     <-- this page
  |     |
  |     +-- sub_4324B0: set up constant bank target section
  |     +-- sub_433310: lay out descriptor lists (tex/sampler/surface)
  |     +-- sub_438DD0: process bindless relocations (core)
  |     +-- sub_43CDA0: resolve bindless symbol types (called from Phase 10)
  |     |
  +-- Phases 3-9: shared memory, constants, etc.
  +-- Phase 10: Resource counting & UFT setup
  |
  v
Relocation Phase (sub_469D60)
```

Phase 2 is gated by `vtable+296` (`supports_bindless()`) and is skipped entirely in relocatable link mode (`elfw+16 == 1`).

## Function Signatures

### `sub_438DD0` -- `process_bindless_references`

```c
// Address: 0x438DD0
// Size: 12,779 bytes (~451 lines decompiled)
//
// a1: elfw*       -- the output ELF wrapper
// a2: section_desc* -- per-entry constant bank section descriptor
//                      (from the per-entry constant list at elfw+272)
//
// Returns: pointer to the updated section descriptor
_QWORD* __fastcall process_bindless_references(__int64 a1, _QWORD* a2);
```

### `sub_43CDA0` -- `resolve_bindless_type_symbols`

```c
// Address: 0x43CDA0
// Size: 6,937 bytes (~292 lines decompiled)
//
// a1: elfw*         -- the output ELF wrapper
// a2: entry_index   -- entry function index (0 = no specific entry)
// a3: symbol_type   -- bindless symbol STT type (10=texture, 11=sampler, 12=surface)
//
// Returns: count of matching resources for this entry
__int64 __fastcall resolve_bindless_type_symbols(__int64 a1, int a2, unsigned __int8 a3);
```

### `sub_438CE0` -- `emit_bindless_relocation`

```c
// Address: 0x438CE0
// Size: ~240 bytes
//
// a1: elfw*           -- the output ELF wrapper
// a2: reloc_type      -- relocation type code
// a3: symbol_index    -- target symbol index in symtab
// a4: section_index   -- target section index
// a5: addend_flag     -- additional relocation flag
// a6: original_reloc  -- original relocation data pointer
//
// Creates a new relocation record and appends it to elfw+376 (relocation list)
__int64 __fastcall emit_bindless_relocation(
    __int64 a1, unsigned int a2, unsigned int a3,
    unsigned int a4, int a5, __int64 a6);
```

### `sub_4324B0` -- `bindless_target_setup`

```c
// Address: 0x4324B0
// Size: ~1 KB
//
// a1: elfw*          -- the output ELF wrapper
// a2: symbol_record  -- the entry-point symbol record
//
// Creates a per-entry constant bank section of the bindless texture type
// (section type from vtable+304), named "<bank_type>.<entry_name>".
// Appends the new section index to the per-entry constant list (elfw+272).
__int64 __fastcall bindless_target_setup(__int64 a1, __int64 a2);
```

### `sub_433310` -- `descriptor_list_layout`

```c
// Address: 0x433310
// Size: ~1.5 KB
//
// a1: elfw*              -- the output ELF wrapper
// a2: descriptor_list*   -- linked list of texture/sampler/surface descriptors
//                           (from elfw+424, +432, or +440)
//
// For each descriptor in the list:
//   1. Look up the bindless texture constant bank section (vtable+304)
//   2. Create $NVLINKBINDLESSOFF_<name> symbol in that section
//   3. Allocate zero-filled data of descriptor size (vtable+440 or +448)
//   4. Merge data into the constant bank via sub_432B10
__int64 __fastcall descriptor_list_layout(__int64 a1, _QWORD* a2);
```

## Relocation Type Classification

The core logic of `sub_438DD0` iterates the global relocation list (`elfw+376`) and classifies each relocation record by its type code (lower 32 bits of the 8-byte relocation info field at `record+8`). The following types trigger bindless processing (reach the `LABEL_32` handler):

| Decimal | Hex | Inferred name | Type |
|---|---|---|---|
| 5 | `0x05` | `R_CUDA_ABS32_20` | 32-bit absolute |
| 12 | `0x0C` | `R_CUDA_ABS32_HI20` | 32-bit absolute high |
| 17 | `0x11` | `R_CUDA_TEX_HEADER_INDEX` | Texture header index |
| 18 | `0x12` | `R_CUDA_TEX_HEADER_INDEX_HI` | Texture header index high |
| 22 | `0x16` | `R_CUDA_BINDLESSOFF13_22` | Bindless offset (13-bit, slot 22) |
| 23 | `0x17` | `R_CUDA_BINDLESSOFF13_23` | Bindless offset (13-bit, slot 23) |
| 24 | `0x18` | `R_CUDA_BINDLESSOFF13_24` | Bindless offset (13-bit, slot 24) |
| 25 | `0x19` | `R_CUDA_BINDLESSOFF13_25` | Bindless offset (13-bit, slot 25) |
| 29 | `0x1D` | `R_CUDA_BINDLESSOFF13_29` | Bindless offset (13-bit, slot 29) |
| 30 | `0x1E` | `R_CUDA_BINDLESSOFF13_30` | Bindless offset (13-bit, slot 30) |
| 38 | `0x26` | `R_CUDA_BINDLESSOFF14_38` | Bindless offset (14-bit, slot 38) |
| 39 | `0x27` | `R_CUDA_BINDLESSOFF14_39` | Bindless offset (14-bit, slot 39) |
| 42 | `0x2A` | `R_CUDA_BINDLESSOFF14_42` | Bindless offset (14-bit, slot 42) |
| 46 | `0x2E` | `R_CUDA_ABS36_LO20` | 36-bit absolute low (Volta+) |
| 50 | `0x32` | `R_CUDA_ABS36_HI16` | 36-bit absolute high (Volta+) |
| 51 | `0x33` | `R_CUDA_ABS36_20` | 36-bit absolute (Volta+) |
| 54 | `0x36` | `R_CUDA_BINDLESSOFF14_54` | Bindless offset (14-bit, slot 54) |
| 55 | `0x37` | `R_CUDA_BINDLESSOFF14_55` | Bindless offset (14-bit, slot 55) |
| 59 | `0x3B` | `R_CUDA_BINDLESSOFF_59` | Bindless offset (extended) |
| 64 | `0x40` | `R_CUDA_BINDLESSOFF_64` | Bindless offset (Ampere+) |
| 65 | `0x41` | `R_CUDA_BINDLESSOFF_65` | Bindless offset (Ampere+) |
| 66 | `0x42` | `R_CUDA_BINDLESSOFF_66` | Bindless offset (Ampere+) |
| 115 | `0x73` | `R_CUDA_BINDLESSOFF_115` | Bindless offset (Hopper+) |
| 65539 | `0x10003` | `R_MERCURY_BINDLESS_3` | Mercury bindless (Blackwell+) |
| 65540 | `0x10004` | `R_MERCURY_BINDLESS_4` | Mercury bindless (Blackwell+) |

The types span four generations of encoding evolution:

- **Base types** (5, 12): Generic absolute relocations that happen to reference bindless symbols. These predate the dedicated bindless relocation types.
- **Legacy 13-bit** (17-18, 22-25, 29-30): Compact bindless offset encoding with 13-bit fields, used on pre-Volta architectures.
- **14-bit extended** (38-39, 42, 54-55, 59): Wider offset encoding added for Volta (sm70+) constant bank expansions.
- **36-bit absolute** (46, 50-51): Wider address relocations for Volta+ extended addressing.
- **Ampere+** (64-66): Relocation types added for sm80+ instruction encodings.
- **Hopper+** (115): Added for sm90 Hopper architecture.
- **Mercury** (65539-65540): Range `0x10000+` is the Mercury (sm100+ / Blackwell) relocation namespace, with bindless variants at offsets 3 and 4.

Types that explicitly **do not** trigger bindless processing (skip to `LABEL_19`) despite being numerically adjacent: 26 (`0x1A`), 27 (`0x1B`), 28 (`0x1C`). These correspond to non-bindless relocation types (`R_CUDA_32`, `R_CUDA_64`, and a third type in the same numeric range -- see the [R_CUDA Catalog](../reference/r-cuda-catalog.md) for exact type assignments).

## Complete Pseudocode: `process_bindless_references` (`sub_438DD0`)

The following pseudocode reconstructs the full algorithm from the decompiled binary. Variable names are inferred from usage context; the structure matches the decompiled code at `0x438DD0` line-for-line.

```c
// sub_438DD0 -- process_bindless_references
// a1: elfw*          -- output ELF wrapper
// a2: section_desc*  -- per-entry constant bank section descriptor
//
// Returns: pointer to the updated section descriptor

section_desc* process_bindless_references(elfw* elf, section_desc* parent_desc)
{
    int num_sections  = elf->section_count;               // elfw+584
    int parent_shtype = parent_desc->sh_type;             // a2[1] (32-bit at offset 4)

    // ---- Phase A: Detect architecture variant ----
    //
    // The byte at elfw+7 encodes the relocation namespace.
    // 'A' (0x41) = Mercury / Blackwell (sm100+): use 0x10000+ reloc types.
    // Anything else = legacy namespace.

    bool is_mercury = (elf->arch_byte == 'A');            // elfw+7 == 0x41

    // Mercury uses bit 0 of elfw+48 for the "wide reloc" flag;
    // non-Mercury uses bit 31 (0x80000000) of elfw+48.
    int arch_flags_mask = is_mercury ? 0x01 : 0x80000000;
    int wide_reloc_flag = elf->arch_flags & arch_flags_mask;  // elfw+48 & mask

    // ---- Phase B: Collect per-entry constant bank sections ----
    //
    // Walk the global section vector (elfw+360) and collect sections whose
    // sh_type matches parent_shtype AND that have non-empty data or relocations.
    // These are the per-entry constant bank sections that might need bindless
    // offset tables.

    linked_list per_entry_list = NULL;                    // v94
    uint64_t max_size  = 0;                               // v93
    uint32_t max_align = 1;                               // v90

    section_vec* all_sections = elf->section_vec;         // elfw+360
    for (uint64_t i = 0; i < vec_count(all_sections); i++)
    {
        section_record* sec = vec_get(all_sections, i);

        // Match on sh_type: must equal the parent constant bank type
        // The constant is either 0x70000006 (single value) or falls in
        // the range [0x70000084, 0x7000009E] (per-entry variants).
        if (sec->sh_type != 0x70000006
            && ((sec->sh_type - 0x70000084) > 0x1A || parent_shtype != sec->sh_type))
            continue;

        uint32_t link_idx = sec->sh_link;                 // offset +44
        if (!link_idx) continue;

        section_record* linked = vec_get(all_sections, link_idx);
        if (linked->data == NULL && linked->reloc_list == NULL)
            continue;

        // Append sec to per_entry_list
        list_append(sec, &per_entry_list);

        // Track maximum alignment and size across all per-entry sections
        if (sec->sh_addralign > max_align)
            max_align = sec->sh_addralign;
        if (sec->sh_size > max_size)
            max_size = sec->sh_size;
    }

    // ---- Phase C: Allocate per-section bitmask ----
    //
    // Stack-allocated byte array, one byte per section + 1.
    // Each byte tracks which bindless resource types are referenced
    // by relocations targeting that section.

    uint8_t bitmask[num_sections + 1];                    // alloca on stack
    memset(bitmask, 0, num_sections + 1);

    // ---- Phase D: Resolve specific constant section ----
    //
    // elfw+568 holds a "specific entry function" symbol index. If nonzero,
    // all bitmask propagation goes to that single section instead of walking
    // the callgraph. This path is used in single-entry compilation mode.

    uint32_t specific_section_idx = 0;
    uint32_t specific_sym = elf->specific_entry_sym;      // elfw+568
    if (specific_sym != 0) {
        sym_record* sym = get_symbol_record(elf, specific_sym);
        specific_section_idx = get_section_index(elf, sym);
    }

    // ---- Phase E: Main relocation scan ----
    //
    // Walk the global relocation linked list (elfw+376). For each relocation,
    // extract the type code from the lower 32 bits of the info field and
    // dispatch through the type classification logic.

    reloc_node* reloc = elf->reloc_list;                  // elfw+376 (linked list head)
    while (reloc != NULL)
    {
        reloc_record* rec = reloc->data;                  // reloc->next_ptr[1]
        uint64_t info = rec->r_info;                      // offset +8 in reloc record
        uint32_t reloc_type = (uint32_t)info;             // low 32 bits
        uint32_t sym_idx    = (uint32_t)(info >> 32);     // high 32 bits

        // ---- Type classification (maps to decompiled switch cascade) ----
        //
        // Bindless types jump to the handler; all others skip (goto next).
        //
        // The classification is a series of range checks. Expressed as a
        // decision tree matching the binary's branch structure:

        bool is_bindless = false;
        switch (reloc_type) {
            // Base absolute types that can reference bindless symbols
            case 5:                                       // R_CUDA_ABS32_20
            case 12:                                      // R_CUDA_ABS32_HI20

            // Texture header index
            case 17: case 18:                             // R_CUDA_TEX_HEADER_INDEX{,_HI}

            // Legacy 13-bit bindless offsets
            case 22: case 23: case 24: case 25:           // R_CUDA_BINDLESSOFF13_22..25
            // Note: 26, 27, 28 are EXCLUDED (non-bindless)
            case 29: case 30:                             // R_CUDA_BINDLESSOFF13_29..30

            // 14-bit extended (Volta+)
            case 38: case 39:                             // R_CUDA_BINDLESSOFF14_38..39
            case 42:                                      // R_CUDA_BINDLESSOFF14_42

            // 36-bit absolute (Volta+)
            case 46:                                      // R_CUDA_ABS36_LO20
            case 50: case 51:                             // R_CUDA_ABS36_{HI16,20}

            // More 14-bit extended
            case 54: case 55:                             // R_CUDA_BINDLESSOFF14_54..55
            case 59:                                      // R_CUDA_BINDLESSOFF_59

            // Ampere+ (sm80)
            case 64: case 65: case 66:                    // R_CUDA_BINDLESSOFF_64..66

            // Hopper (sm90)
            case 115:                                     // R_CUDA_BINDLESSOFF_115

            // Mercury / Blackwell (sm100+): 0x10000+ namespace
            case 65539: case 65540:                       // R_MERCURY_BINDLESS_3..4
                is_bindless = true;
                break;

            default:
                is_bindless = false;
        }

        if (!is_bindless) {
            reloc = reloc->next;
            continue;
        }

        // ---- LABEL_32: Bindless handler ----

        // Step 1: Verify the target symbol is a texture/sampler/surface
        sym_record* sym = get_symbol_record(elf, sym_idx);    // sub_440590
        uint8_t sym_type = sym->st_info & 0x0F;

        if ((sym_type - 10) > 2) {
            // Not texture(10), sampler(11), or surface(12) -- skip
            reloc = reloc->next;
            continue;
        }

        // Step 2: Build the synthetic symbol name
        const char* orig_name = sym->name;                    // sym+32
        size_t name_len = strlen(orig_name);
        char* synth_name = arena_alloc(name_len + 20);        // sub_4307C0
        sprintf(synth_name, "$NVLINKBINDLESSOFF_%s", orig_name);

        // Step 3: Find or create the synthetic symbol in the output symtab
        uint32_t new_sym_idx = find_or_create_symbol(elf, synth_name);  // sub_4411B0

        if (elf->verbose) {                                   // elfw+64 bit 1
            fprintf(stderr, "change reloc symbol from %d to %d\n",
                    sym->section_index, new_sym_idx);
        }
        arena_free(synth_name);                               // sub_431000

        // Step 4: Rewrite the relocation info field
        //
        // Keep the original relocation type in bits [31:0].
        // Replace the symbol index in bits [63:32] with the new synthetic symbol.
        rec->r_info = ((uint64_t)new_sym_idx << 32) | (uint32_t)reloc_type;

        // Step 5: Compute the bitmask byte from the resource type
        //
        // byte_1D391A0 is a compile-time lookup table at address 0x1D391A0:
        //   byte_1D391A0[0]  = 0x01   (texture:  bit 0)
        //   byte_1D391A0[4]  = 0x02   (sampler:  bit 1)
        //   byte_1D391A0[8]  = 0x03   (surface:  bits 0+1)
        //
        // Indexed as: byte_1D391A0[4 * (sym_type - 10)]

        uint8_t bitmask_byte = 0;
        if ((sym_type - 10) <= 2)
            bitmask_byte = BITMASK_LUT[sym_type - 10];       // byte_1D391A0

        // Step 6: Mark the relocation's target section in the bitmask
        uint32_t reloc_section = rec->rela_section_index;     // rec+24
        uint32_t resolved_sec = get_section_by_rela(elf, reloc_section)->sh_link;
        bitmask[resolved_sec] |= bitmask_byte;

        // Step 7: Propagate through callgraph (if the section is a function)
        section_record* sec_rec = get_section_by_rela(elf, resolved_sec);
        if (!(sec_rec->flags & 0x04)) {
            // Not a function section -- no propagation needed
            reloc = reloc->next;
            continue;
        }

        if (specific_section_idx != 0) {
            // Single-entry mode: propagate directly to the known entry section
            bitmask[specific_section_idx] |= bitmask_byte;
        } else {
            // Multi-entry mode: walk the callgraph to find all callers
            //
            // The function ID is extracted from the section's sh_link field,
            // sign-extended from 24 bits: (sh_link << 8) >> 8
            int func_id = (int)(sec_rec->sh_link << 8) >> 8;

            // sub_44C740 returns the head of a linked list of caller records.
            // Each record has: { next_ptr, padding, caller_sym_index }
            caller_node* caller = callgraph_get_callers(elf, func_id);
            while (caller != NULL) {
                sym_record* caller_sym = get_symbol_record(elf, caller->sym_index);
                uint32_t caller_sec = get_section_index(elf, caller_sym);
                bitmask[caller_sec] |= bitmask_byte;
                caller = caller->next;
            }
        }

        reloc = reloc->next;
    }
    // ---- End of relocation scan (Phase E) ----

    // ... Phases F-I follow (section pruning, allocation, emission, cleanup)
}
```

### Phase E Detail: The Bitmask Lookup Table

The global array `byte_1D391A0` at binary address `0x1D391A0` is a 12-byte lookup table. It is indexed by `4 * (sym_type - 10)`, where the stride of 4 is an artifact of the compiler aligning each entry to a 4-byte boundary. The effective mapping is:

| Index expression | `sym_type` | Bitmask byte | Binary representation |
|---|---|---|---|
| `byte_1D391A0[0]` | 10 (texture) | `0x01` | `0000_0001` |
| `byte_1D391A0[4]` | 11 (sampler) | `0x02` | `0000_0010` |
| `byte_1D391A0[8]` | 12 (surface) | `0x03` | `0000_0011` |

Surface references set both bits because a surface object conceptually contains both a texture descriptor (for reads) and a separate surface descriptor (for writes). The linker must allocate space in both the texture and sampler/surface descriptor tables for a surface, hence both bits are set.

### Phase E Detail: Callgraph Propagation Walk

The function `sub_44C740` (`callgraph_get_callers`) works as follows:

```c
// sub_44C740 -- callgraph_get_callers
caller_node* callgraph_get_callers(elfw* elf, int func_id)
{
    sym_record* sym = get_symbol_record(elf, func_id);

    // elfw+408 holds the callgraph vector, indexed by sym->callgraph_idx (offset +28)
    callgraph_entry* entry = vec_get(elf->callgraph_vec, sym->callgraph_idx);

    // Sanity check: callgraph must be fully built
    if (!elf->callgraph_complete)                         // elfw+81
        fatal("callgraph not complete");

    if (entry)
        return entry->caller_list;                        // entry+40: linked list head
    else
        return NULL;
}
```

Each `caller_node` in the linked list has this layout:

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `next` | Pointer to next caller node (NULL = end of list) |
| 8 | 4 | `sym_index` | Symbol table index of the calling function |

The propagation loop ORs the bitmask byte into each caller's section entry. This means: if device function `helper()` uses a bindless texture, and kernels `A()` and `B()` both call `helper()`, then both `section_of_A` and `section_of_B` get bit 0 set in the bitmask. This transitive propagation is essential because the per-entry constant bank must be allocated for every kernel that *transitively* reaches a bindless reference, even through multiple levels of function calls.

### The Bindless Handler (LABEL_32) -- Step-by-Step Summary

When a relocation matches a bindless type, the handler executes five steps:

**Step 1: Symbol Lookup and Type Check**

```
sym = get_symbol_record(elfw, reloc.symbol_index)    // sub_440590
sym_type = sym->st_info & 0x0F

if (sym_type - 10) > 2:    // not texture(10), sampler(11), or surface(12)
    skip this relocation    // goto LABEL_19
```

The `st_info & 0x0F` low nibble encodes the CUDA-specific symbol type:

| Value | `STT_*` type | GPU resource |
|---|---|---|
| 10 | `STT_CUDA_TEXTURE` | Texture reference |
| 11 | `STT_CUDA_SAMPLER` | Sampler state |
| 12 | `STT_CUDA_SURFACE` | Surface reference |

Only relocations targeting these three symbol types proceed to bindless processing.

**Step 2: Create Synthetic Symbol**

```
name_len = strlen(sym->name)
synth_name = arena_alloc(name_len + 20)               // sub_4307C0
sprintf(synth_name, "$NVLINKBINDLESSOFF_%s", sym->name)
new_sym_idx = find_or_create_symbol(elfw, synth_name)  // sub_4411B0

if verbose:
    fprintf(stderr, "change reloc symbol from %d to %d\n",
            sym->section_index, new_sym_idx)

arena_free(synth_name)                                 // sub_431000
```

The allocation size is `strlen + 20` (19 bytes for the prefix + 1 for the NUL terminator). The function `sub_4411B0` either finds an existing symbol with that name (if a previous relocation already created it) or creates a new one. The arena allocator at `sub_4307C0` obtains memory from the per-module arena (located via `sub_44F410`), and the corresponding `sub_431000` returns it.

**Step 3: Rewrite Relocation Target**

```
reloc.info = (new_sym_idx << 32) | original_reloc_type
```

This is a 64-bit rewrite of the relocation info field. The upper 32 bits encode the symbol table index, the lower 32 bits encode the relocation type. After this rewrite, the relocation engine will resolve the address from the `$NVLINKBINDLESSOFF_` symbol rather than the original texture/surface symbol.

**Step 4: Build Section Bitmask**

```
bitmask_byte = byte_1D391A0[4 * (sym_type - 10)]
section_bitmask[reloc.section_index] |= bitmask_byte
```

A VLA-sized bitmask (`alloca(num_sections + 1)` at function entry) tracks which sections contain references to each resource type. Each byte in the bitmask stores up to three flags:

| Bit | Meaning |
|---|---|
| 0 | Section references at least one bindless texture |
| 1 | Section references at least one bindless sampler |
| 0+1 | Section references at least one bindless surface (both bits set) |

**Step 5: Propagate Bitmask Through Callgraph**

If the section containing the relocation is a function section (section flags bit 2 set), the bitmask is propagated to all callers:

```
section_record = get_section_by_rela(elfw, reloc.section_index)
if section_record.flags & 0x04:   // is a function section
    if specific_constant_section:
        section_bitmask[specific_constant_idx] |= bitmask_byte
    else:
        func_id = (section_record.sh_link << 8) >> 8   // sign-extend 24-bit
        caller_list = callgraph_get_callers(elfw, func_id)
        for each caller in caller_list:
            caller_sym = get_symbol_record(elfw, caller.sym_index)
            caller_section = get_section_index(elfw, caller_sym)
            section_bitmask[caller_section] |= bitmask_byte
```

This ensures that if a non-entry function references a bindless texture, the bitmask correctly marks the entry-function sections that transitively reach that reference.

## Complete Pseudocode: Post-Scan Phases (F through I)

After the relocation scan (Phase E) completes, `sub_438DD0` continues with four more phases.

### Phase F: Section Pruning

```c
    // ---- Phase F: Prune sections with no bindless references ----
    //
    // Walk the per-entry list collected in Phase B. Any section whose
    // bitmask byte is still zero has no bindless references (direct or
    // transitive) and can be pruned.

    for (list_node* node = per_entry_list; node != NULL; node = node->next)
    {
        section_record* sec = node->data;
        uint32_t sec_idx = sec->sh_link;                  // offset +44

        if (bitmask[sec_idx] == 0) {
            if (elf->verbose)
                fprintf(stderr, "no bindless ref in section %s\n", sec->name);

            // NULL out the data pointer -- marks this node as dead.
            // The section still exists in the ELF, but won't receive a
            // bindless offset table. The node remains in the list but is
            // skipped during Phase H.
            node->data = NULL;
        }
    }
```

This is a pure size optimization. Without it, every per-entry constant section would be allocated a bindless offset table, wasting constant memory on kernels that never perform bindless access.

### Phase G: Section Layout

```c
    // ---- Phase G: Compute aligned layout for the constant bank ----
    //
    // sub_4325A0 assigns byte offsets to each descriptor symbol within
    // the constant bank section. It walks the sorted symbol list,
    // aligning each entry to its required alignment and advancing
    // the running offset.
    //
    // The total_size is padded up to the maximum alignment boundary:
    //   if (max_size % max_align != 0)
    //       total_size = max_align + max_size - (max_size % max_align)
    //   else
    //       total_size = max_size

    uint64_t remainder = max_size % max_align;
    uint32_t total_size = (remainder != 0)
        ? max_align + max_size - remainder
        : max_size;

    layout_section(elf, parent_desc, total_size, max_align);  // sub_4325A0
```

The layout engine (`sub_4325A0`) iterates the section's symbol list in sorted order, assigning each symbol an aligned offset:

```c
// sub_4325A0 -- layout_section (simplified)
void layout_section(elfw* elf, section_desc* desc, uint32_t init_offset, int align)
{
    // Sort symbols by alignment (descending) via sub_4647D0 + sub_432440
    sort(desc->symbol_list, compare_by_alignment);

    uint32_t offset = init_offset;
    for (list_node* n = desc->symbol_list; n; n = n->next)
    {
        sym_layout_record* rec = n->data;
        uint64_t sym_align = rec->alignment;              // rec+16

        if (sym_align != 0) {
            // Align up: offset = ceil(offset / sym_align) * sym_align
            if (offset % sym_align != 0)
                offset = offset + sym_align - (offset % sym_align);
        } else {
            // Zero-size symbol: use natural alignment (min 8)
            uint64_t sz = rec->size;
            uint64_t nat_align = (sz <= 8) ? sz : 8;
            if (offset % nat_align != 0)
                offset = offset + nat_align - (offset % nat_align);
        }

        // Assign offset to both the layout record and the symbol
        sym_record* sym = get_symbol_record(elf, rec->sym_index);
        sym->st_value = offset;
        rec->assigned_offset = offset;

        if (elf->verbose)
            fprintf(stderr, "variable %s at offset %d\n", sym->name, offset);

        offset += rec->size;
    }
    desc->sh_size = offset;
}
```

### Phase H: Per-Entry Relocation Emission

```c
    // ---- Phase H: Emit resolved bindless relocations ----
    //
    // For each surviving per-entry section, walk the parent's descriptor
    // list and emit a resolved relocation (type 6/7/8/9/52 or Mercury
    // equivalents) that tells the relocation engine where to write the
    // descriptor offset.

    // Precompute the resolved relocation type codes
    uint32_t tex_reloc_type     = is_mercury ? 65547 : 6;     // 0x1000B : 0x06
    uint32_t sampler_reloc_type = is_mercury ? 65548 : 7;     // 0x1000C : 0x07

    for (list_node* entry_node = per_entry_list; entry_node; )
    {
        section_record* entry_sec = entry_node->data;
        if (entry_sec == NULL) {
            // Pruned in Phase F -- skip
            entry_node = entry_node->next;
            continue;
        }

        // Copy layout parameters from the parent section
        entry_sec->sh_addralign = parent_desc->alignment;     // a2[6] -> sec+48
        entry_sec->sh_size      = parent_desc->size;          // a2[4] -> sec+32

        // Counters for resource limit checking
        uint32_t texture_count = 0;
        uint32_t sampler_count = 0;
        uint32_t surface_count = 0;

        // Walk the parent's descriptor list (from parent_desc+72, i.e., a2[9])
        for (list_node* desc_node = parent_desc->descriptor_list; desc_node; desc_node = desc_node->next)
        {
            descriptor_record* desc = desc_node->data;

            // The descriptor's symbol was created during sub_433310 with the
            // $NVLINKBINDLESSOFF_ prefix. Strip the prefix (+19) to find the
            // original texture/sampler/surface symbol.
            sym_record* desc_sym = get_symbol_record(elf, desc->sym_index);
            uint32_t bindless_sym_idx = find_or_create_symbol(elf, desc_sym->name + 19);
            sym_record* bindless_sym  = get_symbol_record(elf, bindless_sym_idx);
            uint32_t bindless_sec     = get_section_index(elf, bindless_sym);

            // Validate: descriptor must belong to this per-entry section
            // (or to section 0, which means "unassigned")
            uint32_t entry_sec_idx = entry_sec->sh_link;
            if (bindless_sec != 0 && entry_sec_idx != bindless_sec)
                continue;   // belongs to a different per-entry section

            // Determine the resolved relocation type based on the resource type
            uint8_t res_type = bindless_sym->st_info & 0x0F;
            uint32_t emit_type = 0;

            switch (res_type) {
                case 10:  // texture
                    emit_type = tex_reloc_type;
                    if (!(bitmask[entry_sec_idx] & 0x01))
                        continue;                          // section has no texture refs
                    texture_count++;
                    break;

                case 11:  // sampler
                    emit_type = sampler_reloc_type;
                    if (!(bitmask[entry_sec_idx] & 0x02))
                        continue;                          // section has no sampler refs
                    sampler_count++;
                    break;

                case 12:  // surface
                    if (is_unified_surface()) {            // vtable+320
                        emit_type = is_mercury ? 65549 : 52;
                    } else {
                        if (is_mercury)
                            fatal("unexpected usage of non-unified surface descriptors");

                        // Compare descriptor's offset against the base surface
                        // descriptor size from the architecture vtable (+448).
                        // If they differ, the descriptor needs an addend adjustment.
                        uint64_t desc_offset = desc->offset;   // desc+24
                        uint64_t base_size   = vtable_surface_base_size();
                        emit_type = (desc_offset != base_size) ? 9 : 8;
                    }
                    if (!(bitmask[entry_sec_idx] & 0x03))
                        continue;                          // section has no surface refs
                    surface_count++;
                    break;

                default:
                    emit_type = 0;                         // unreachable if data is valid
                    break;
            }

            // Emit the resolved relocation
            // Parameters: (elf, type, sym_idx, target_section, addend_flag, orig_reloc)
            emit_bindless_relocation(elf,
                emit_type,
                bindless_sym_idx,
                entry_sec->section_index,                  // offset +64
                0,                                         // no addend
                desc->original_reloc);                     // desc+8
        }

        // ---- Phase I: Resource limit checking ----

        const char* type_name = get_section_type_name(entry_sec->sh_type);

        if (texture_count > max_textures()) {              // vtable+40
            char* entry_name = get_entry_name(elf, entry_sec->section_index);
            // Error format: "%d exceeds the maximum number of %s (%zu) for '%s'"
            fatal_limit(max_textures(), "textures", strlen(type_name) + strlen(entry_name) + 1);
        }

        if (sampler_count > max_samplers()) {              // vtable+48
            char* entry_name = get_entry_name(elf, entry_sec->section_index);
            fatal_limit(max_samplers(), "samplers", strlen(type_name) + strlen(entry_name) + 1);
        }

        if (surface_count > max_surfaces()) {              // vtable+56
            char* entry_name = get_entry_name(elf, entry_sec->section_index);
            fatal_limit(max_surfaces(), "surfaces", strlen(type_name) + strlen(entry_name) + 1);
        }

        entry_node = entry_node->next;
    }
```

### Phase J: Cleanup

```c
    // ---- Phase J: Free temporary data structures ----

    // Free the per-entry linked list collected in Phase B
    list_free(per_entry_list);                            // sub_464520

    // Free each descriptor record in the parent's descriptor list,
    // then free the list itself and zero out the pointers
    for (list_node* n = parent_desc->descriptor_list; n; n = n->next)
        arena_free(n->data);                              // sub_431000

    list_free(parent_desc->descriptor_list);              // sub_464520

    parent_desc->size = 0;                                // a2[4] = 0
    parent_desc->descriptor_list = NULL;                  // a2[9] = 0
    parent_desc->descriptor_tail = NULL;                  // a2[10] = 0

    return parent_desc;
}
```

## Resolved Relocation Types

The relocation types emitted during Phase H differ from the input types. These are the **resolved** types consumed by the relocation application engine (`sub_469D60`):

| Type | Hex | A-variant | Meaning |
|---|---|---|---|
| 6 | `0x06` | No | Apply texture descriptor offset |
| 7 | `0x07` | No | Apply sampler descriptor offset |
| 8 | `0x08` | No | Apply surface descriptor offset (standard) |
| 9 | `0x09` | No | Apply surface descriptor offset (with addend) |
| 52 | `0x34` | No | Apply unified surface descriptor offset |
| 65547 | `0x1000B` | Yes | Mercury texture descriptor offset |
| 65548 | `0x1000C` | Yes | Mercury sampler descriptor offset |
| 65549 | `0x1000D` | Yes | Mercury unified surface descriptor offset |

A-variant (`elfw+7 == 'A'`) indicates Mercury / Blackwell (sm100+) architectures, which use the `0x10000+` relocation namespace.

The distinction between types 8 and 9 for surfaces is determined by comparing the descriptor's offset field (`desc+24`) against the architecture's base surface descriptor size (`vtable+448`). If they match, type 8 is emitted (standard offset application). If they differ, type 9 is emitted, signaling that the relocation engine must apply an addend adjustment to account for non-standard surface descriptor layouts.

## Resource Limit Checking

After emitting relocations for each per-entry section, the function checks whether the resource counts exceed architecture limits:

```
entry_name = get_section_type_name(per_entry.type)

if texture_count > max_textures():        // vtable+40
    error("too many %s in %s", "textures", entry_name)

if sampler_count > max_samplers():        // vtable+48
    error("too many %s in %s", "samplers", entry_name)

if surface_count > max_surfaces():        // vtable+56
    error("too many %s in %s", "surfaces", entry_name)
```

These limits are hard limits imposed by the GPU hardware. Exceeding them produces a fatal linker error via `sub_467460`. The error message format string at address `0x2A5BA40` produces output like: `"128 exceeds the maximum number of textures (128) for '.nv.constant3.my_kernel'"`. The entry name is constructed by concatenating the section type name (from `sub_4401F0`) with the per-entry section name (from `sub_4402D0`).

## Complete Pseudocode: `resolve_bindless_type_symbols` (`sub_43CDA0`)

This companion function counts bindless resources of a given type (texture, sampler, or surface) for a specific entry function. It is called during Phase 10 of the layout pass (resource counting and UFT setup).

```c
// sub_43CDA0 -- resolve_bindless_type_symbols
// a1: elfw*          -- the output ELF wrapper
// a2: entry_index    -- entry function index (0 = no specific entry)
// a3: symbol_type    -- bindless symbol STT type (10=texture, 11=sampler, 12=surface)
//
// Returns: count of matching bindless resources for this entry

uint32_t resolve_bindless_type_symbols(elfw* elf, int entry_index, uint8_t sym_type)
{
    // Look up the entry function symbol
    uint32_t entry_sym_id = lookup_entry_symbol();        // sub_444720
    sym_record* entry_sym = get_symbol_record(elf, entry_sym_id);

    if (entry_index == 0)
        return 0;                                         // no entry -- nothing to count

    if (entry_sym == NULL)
        fatal("symbol not found");

    // ---- Gate: Does the architecture support bindless? ----
    if (!supports_bindless())                             // vtable+296
        goto path_B;

    // ===============================================
    // PATH A: Bindless-supported architecture
    // ===============================================

    int resolved_type;
    bool needs_sort = false;
    uint32_t bank_section_type;

    if (sym_type >= 10 && sym_type <= 11) {
        // Texture or sampler
        bank_section_type = bindless_texture_bank_type(); // vtable+304

        // Check if the architecture has the "wide relocation" flag.
        // This flag indicates relocations need to be sorted before counting
        // to enable efficient section-grouped iteration.
        if (elf->compact_mode && elf->elf_class == 1) {  // elfw+82, elfw+4
            if (elf->wide_reloc_flag_32 & 0x02)          // elfw+37 bit 1
                needs_sort = true;
        } else {
            if (elf->wide_reloc_flag_64 & 0x02)          // elfw+49 bit 1
                needs_sort = true;
        }

        if (needs_sort)
            sort_reloc_list(&elf->reloc_list, comparator);  // sub_4647D0

        // Resolved type: 6 for texture, 7 for sampler
        resolved_type = (sym_type != 10) ? 7 : 6;        // (a3 != 10) + 6

    } else if (sym_type == 12) {
        // Surface
        resolved_type = 8;
        bank_section_type = bindless_surface_bank_type(); // vtable+312

        if (is_unified_surface()) {                       // vtable+320
            resolved_type = 52;                           // unified surface
            needs_sort = false;                           // no sort needed for surfaces
        }

    } else {
        fatal("unexpected bindless type");
        resolved_type = 0;
        bank_section_type = 0;
    }

    // Construct the per-entry section name: "<bank_type_name>.<entry_name>"
    const char* bank_name  = get_section_type_name(bank_section_type);
    const char* entry_name = entry_sym->name;

    char section_name[strlen(bank_name) + strlen(entry_name) + 16];
    sprintf(section_name, "%s.%s", bank_name, entry_name);

    // Look up the section by name
    uint32_t target_sec_idx = find_section_by_name(elf, section_name);
    if (target_sec_idx == 0)
        return 0;                                         // section doesn't exist

    // Resolve the target section's sh_link to get the actual section index
    // used in relocation records
    uint32_t target_shlink = elf->section_map[target_sec_idx];  // elfw+472

    // ---- Count matching relocations ----
    //
    // Walk the relocation list and count entries that match both:
    //   (1) the resolved relocation type, AND
    //   (2) the target section index
    //
    // For textures and samplers (sym_type 10 or 11), there is an additional
    // deduplication step when the sort flag is active: consecutive relocations
    // with the same symbol index are counted as one. This handles the case
    // where multiple instructions reference the same texture/sampler.

    reloc_node* reloc = elf->reloc_list;
    uint32_t count = 0;

    if (sym_type <= 11) {
        // Texture or sampler counting
        if (needs_sort) {
            // SORTED path: deduplicate by symbol index
            uint32_t prev_sym = 0;

            while (reloc != NULL) {
                reloc_record* rec = reloc->data;
                uint32_t rec_type, rec_sec;

                // Extract type and section depending on ELF class
                if (elf->compact_mode && elf->elf_class == 1) {
                    rec_type = rec->compact_type;         // rec+4 (byte)
                    rec_sec  = get_section_by_rela(elf, rec->rela_idx)->compact_link;
                } else {
                    rec_type = rec->r_type;               // rec+8 (32-bit)
                    rec_sec  = get_section_by_rela(elf, rec->rela_idx)->sh_link;
                }

                if (rec_type == resolved_type && rec_sec == target_shlink) {
                    // Extract symbol index from the info field
                    uint32_t cur_sym;
                    if (elf->compact_mode && elf->elf_class == 1)
                        cur_sym = rec->compact_type >> 8;  // rec+4 bits [31:8]
                    else
                        cur_sym = (uint32_t)(rec->r_info >> 32);

                    // Deduplicate: only count if symbol differs from previous
                    if (cur_sym != prev_sym) {
                        count++;
                        prev_sym = cur_sym;
                    }
                }
                reloc = reloc->next;
            }

        } else {
            // UNSORTED path: simple count (no dedup)
            while (reloc != NULL) {
                reloc_record* rec = reloc->data;
                uint32_t rec_type, rec_sec;

                if (elf->compact_mode && elf->elf_class == 1) {
                    rec_type = rec->compact_type;
                    rec_sec  = get_section_by_rela(elf, rec->rela_idx)->compact_link;
                } else {
                    rec_type = rec->r_type;
                    rec_sec  = get_section_by_rela(elf, rec->rela_idx)->sh_link;
                }

                if (rec_type == resolved_type && rec_sec == target_shlink)
                    count++;

                reloc = reloc->next;
            }
        }

    } else {
        // Surface counting (sym_type == 12)
        //
        // Surfaces use the same logic as the unsorted texture/sampler path
        // but with the surface-specific resolved type (8 or 52).

        if (needs_sort) {
            // Sorted: filter by section first, then by type
            while (reloc != NULL) {
                reloc_record* rec = reloc->data;
                uint32_t rec_type, rec_sec;

                if (elf->compact_mode && elf->elf_class == 1) {
                    rec_type = rec->compact_type;
                    rec_sec  = get_section_by_rela(elf, rec->rela_idx)->compact_link;
                } else {
                    rec_type = rec->r_type;
                    rec_sec  = get_section_by_rela(elf, rec->rela_idx)->sh_link;
                }

                if (rec_sec == target_shlink && rec_type == resolved_type)
                    count++;

                reloc = reloc->next;
            }
        } else {
            // Unsorted: same logic
            while (reloc != NULL) {
                reloc_record* rec = reloc->data;
                uint32_t rec_type, rec_sec;

                if (elf->compact_mode && elf->elf_class == 1) {
                    rec_type = rec->compact_type;
                    rec_sec  = get_section_by_rela(elf, rec->rela_idx)->compact_link;
                } else {
                    rec_type = rec->r_type;
                    rec_sec  = get_section_by_rela(elf, rec->rela_idx)->sh_link;
                }

                if (rec_type == resolved_type && rec_sec == target_shlink)
                    count++;

                reloc = reloc->next;
            }
        }
    }

    return count;

    // ===============================================
    // PATH B: Non-bindless architecture (legacy)
    // ===============================================
path_B:
    // Resolve the entry function's section index
    uint32_t entry_sec;
    if (elf->compact_mode && elf->elf_class == 1)
        entry_sec = entry_sym->compact_shndx;             // sym+14 (16-bit)
    else
        entry_sec = get_section_index(elf, entry_sym);

    if (entry_sec == 0xFFFF)                              // SHN_COMMON / extended
        entry_sec = vec_get(elf->extended_shndx, entry_sym_id);

    // Walk the legacy resource descriptor list (elfw+392)
    // This list contains sampler/texture/surface records from bound-mode access.
    uint32_t max_slot = 0;

    for (list_node* node = elf->resource_list; node; node = node->next)
    {
        descriptor_record* desc = node->data;

        // Filter: only type-2 records (resource descriptors)
        if (desc->type_byte != 2)                         // desc+1
            continue;

        // Match section index
        section_record* desc_sec_rec = get_section_by_rela(elf, desc->section_idx);
        uint32_t desc_sec;
        if (elf->compact_mode && elf->elf_class == 1)
            desc_sec = desc_sec_rec->compact_link;
        else
            desc_sec = desc_sec_rec->sh_link;

        if (entry_sec != desc_sec)
            continue;

        // Match symbol type
        uint32_t* sym_ref = desc->sym_ref;                // desc+8 (pointer to [sym_idx, slot_id])
        sym_record* ref_sym = get_symbol_record(elf, sym_ref[0]);

        uint8_t ref_type;
        if (elf->compact_mode && elf->elf_class == 1)
            ref_type = ref_sym->compact_info & 0x0F;
        else
            ref_type = ref_sym->st_info & 0x0F;

        if (ref_type != sym_type)
            continue;

        // Track the maximum slot index (slot_id is at sym_ref[1])
        uint32_t slot = sym_ref[1];
        if (slot >= max_slot)
            max_slot = slot + 1;
    }

    return max_slot;
}
```

### Key Differences Between Path A and Path B

| Aspect | Path A (bindless) | Path B (legacy bound) |
|---|---|---|
| Data source | Relocation list (`elfw+376`) | Resource descriptor list (`elfw+392`) |
| What is counted | Resolved relocations of type 6/7/8/52 | Descriptor records with matching `STT_*` type |
| Return value | Number of distinct relocations | Highest slot index + 1 |
| Section matching | By constructed section name (`"<bank>.<entry>"`) | By section index from descriptor's `sh_link` |
| Deduplication | Symbol-based dedup for textures/samplers when sorted | None (slot IDs are already unique) |
| Sort optimization | Yes, via `sub_4647D0` when wide-reloc flag is set | No sorting needed |

### Relocation List Sorting

When the "wide relocation" flag is set (`elfw+37 bit 1` for 32-bit ELF, `elfw+49 bit 1` for 64-bit), the function sorts the relocation list before counting. Two comparators are used depending on ELF class:

- `sub_432810`: Comparator for compact (32-bit) relocation records. Sorts by section index (from `rela_section+28`), then by relocation type.
- `sub_432840`: Comparator for standard (64-bit) relocation records. Sorts by section index (from `rela_section+44`), then by relocation type (`rec+8`).

Sorting groups all relocations for the same section together, which enables the deduplication logic for textures and samplers: consecutive relocations targeting the same symbol within the same section are counted as one. This deduplication reflects the fact that multiple instructions may reference the same texture object, but only one descriptor slot is needed.

## The `$NVLINKBINDLESSOFF_` Naming Convention

The synthetic symbol name follows a strict pattern:

```
$NVLINKBINDLESSOFF_ + <original_symbol_name>
```

For example, if the input cubin contains a texture symbol named `my_texture`, the linker creates `$NVLINKBINDLESSOFF_my_texture`. This symbol is:

- Added to the output ELF's symbol table via `sub_4411B0` (find-or-create)
- Assigned to the bindless constant bank section
- Given section type 13 (`STT_SECTION` or CUDA-specific type), binding 0 (`STB_LOCAL`), flags 129
- Sized according to the descriptor entry size from the vtable (`+440` for textures/samplers, `+448` for surfaces)
- Zero-initialized (the descriptor data is filled later by the CUDA runtime)

The name prefix `$NVLINKBINDLESSOFF_` is exactly 19 characters, which is used by `sub_43CDA0` when it strips the prefix to find the original symbol name (`sym->name + 19`).

## Surface Descriptor Variants

Surface handling has an additional complexity compared to textures and samplers. The function checks `vtable+320` to determine whether **unified surface descriptors** are in use:

- **Unified mode** (default on newer architectures): All surfaces use a single descriptor format. The relocation type is 52 (or `0x1000D` for Mercury). This is the common path.
- **Non-unified mode**: Surfaces may use different descriptor layouts depending on the surface type. If non-unified surfaces are encountered on an a-variant (Mercury) architecture, the linker emits a fatal error: `"unexpected usage of non-unified surface descriptors"`. On pre-Mercury architectures, the function checks the descriptor's offset field against the architecture's base surface offset (`vtable+448`) and selects type 8 or 9 accordingly.

## Complete Pseudocode: `emit_bindless_relocation` (`sub_438CE0`)

This small helper creates a single relocation record and appends it to the global relocation list.

```c
// sub_438CE0 -- emit_bindless_relocation
// a1: elfw*           -- output ELF wrapper
// a2: reloc_type      -- resolved relocation type (6, 7, 8, 9, 52, or Mercury variants)
// a3: symbol_index    -- index of the $NVLINKBINDLESSOFF_ symbol in the symtab
// a4: section_index   -- target section index (the per-entry constant bank section)
// a5: addend_flag     -- additional flag stored at record+28
// a6: original_reloc  -- pointer to the original relocation data (copied as-is)

void emit_bindless_relocation(elfw* elf, uint32_t type, uint32_t sym_idx,
                               uint32_t sec_idx, int addend_flag, void* orig_reloc)
{
    // Construct the relocation section name from the target section name.
    // For RELA-style relocations (elfw+89 set): ".rela<section_name>"
    // For REL-style relocations: ".rel<section_name>"
    const char* sec_name = get_section_name(elf, sec_idx);      // sub_4402D0
    char rela_name[strlen(sec_name) + 21];                      // alloca
    if (elf->is_rela)                                           // elfw+89
        sprintf(rela_name, ".rela%s", sec_name);
    else
        sprintf(rela_name, ".rel%s", sec_name);

    // Look up or create the relocation section
    uint32_t rela_sec_idx = find_section_by_name(elf, rela_name);  // sub_4411D0

    // Allocate a 32-byte relocation record from the module arena
    reloc_record* rec = arena_alloc(32);                        // sub_4307C0

    // Fill the record:
    //   [0:8]   = original relocation data (e.g., the r_offset from the input reloc)
    //   [8:16]  = r_info: (sym_idx << 32) | type
    //   [16:24] = r_addend: 0
    //   [24:28] = rela section index
    //   [28:32] = addend flag
    rec->original_data = *(uint64_t*)orig_reloc;                // rec+0
    rec->r_info        = ((uint64_t)sym_idx << 32) | type;      // rec+8
    rec->r_addend      = 0;                                     // rec+16
    rec->rela_sec_idx  = rela_sec_idx;                          // rec+24
    rec->addend_flag   = addend_flag;                           // rec+28

    // Append to the global relocation list
    list_append(rec, &elf->reloc_list);                         // sub_4644C0 -> elfw+376
}
```

The relocation record layout (32 bytes):

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `original_data` | Copied from the input relocation (preserves `r_offset`) |
| 8 | 8 | `r_info` | `(symbol_index << 32) \| relocation_type` |
| 16 | 8 | `r_addend` | Always 0 for bindless relocations |
| 24 | 4 | `rela_sec_idx` | Index of the `.rela*` section this reloc belongs to |
| 28 | 4 | `addend_flag` | Extra flag (always 0 in the bindless emission path) |

## Descriptor List Layout (`sub_433310`)

Before `sub_438DD0` runs, the layout phase calls `sub_433310` three times (once each for textures at `elfw+424`, samplers at `elfw+432`, and surfaces at `elfw+440`). This function allocates constant-bank space for each descriptor.

```c
// sub_433310 -- descriptor_list_layout
// a1: elfw*              -- output ELF wrapper
// a2: descriptor_list*   -- linked list of descriptors (from elfw+424/432/440)

void descriptor_list_layout(elfw* elf, descriptor_node* list)
{
    // Create a hash set for tracking shared-surface overlap sections
    hash_set* overlap_set = hashset_create(256);          // sub_465020

    if (list == NULL)
        goto cleanup;

    // ---- Surface pre-scan ----
    // If the first descriptor is a surface (type 12), scan the resource list
    // (elfw+392) to identify shared sections. Records with type byte 36 (0x24)
    // that reference shared symbols (flag bit 4) are tracked.
    descriptor_record* first_desc = list->data;
    if ((first_desc->st_info & 0x0F) == 12) {            // surface type
        for (list_node* n = elf->resource_list; n; n = n->next) {
            resource_record* res = n->data;
            if (res->type_byte != 36)                     // res+1
                continue;

            // Look up the section that this resource references
            section_record* res_sec = get_section_by_rela(elf, res->section_idx);
            uint32_t func_id = (res_sec->sh_link << 8) >> 8;  // sign-extend 24-bit

            // Check if the referenced function's symbol has the shared flag
            sym_record* func_sym = get_symbol_record(elf, func_id);
            if (func_sym->st_other & 0x10) {              // shared flag at sym+5 bit 4
                hashset_insert(overlap_set, func_id);
            }
            // Mark this record as processed by clearing its type
            res->type_byte = 0;
        }
    }

    // ---- Main descriptor allocation loop ----
    size_t desc_size = 0;
    while (list != NULL) {
        // Get the bindless constant bank section type from the arch vtable
        uint32_t bank_type = bindless_texture_bank_type();    // vtable+304
        const char* bank_name = get_section_type_name(bank_type);

        // Find or create the constant bank section
        uint32_t bank_sec_idx = find_section_by_name(elf, bank_name);
        if (bank_sec_idx == 0) {
            // Create the section with type = bank_type, flags = SHF_ALLOC(2),
            // alignment = 4, link = (bank_type - 0x70000084)
            uint32_t sym_idx = create_section(elf, bank_name, bank_type, 2, 0, 0,
                                               4, bank_type - 0x70000084);
            sym_record* sec_sym = get_symbol_record(elf, sym_idx);
            bank_sec_idx = get_section_index(elf, sec_sym);
            list_append(bank_sec_idx, &elf->per_entry_const_list);  // elfw+272
        }

        // Determine descriptor size based on resource type
        descriptor_record* desc = list->data;
        uint8_t res_type = desc->st_info & 0x0F;

        switch (res_type) {
            case 10:  // texture -- fall through
            case 11:  // sampler
                desc_size = vtable_tex_sampler_desc_size();    // vtable+440
                break;

            case 12:  // surface
                // Check if this is a shared surface using the overlap set
                uint32_t sec_idx = get_section_index(elf, list->data);
                section_record* sec = get_section_by_rela(elf, sec_idx);
                uint32_t func_id = (sec->sh_link << 8) >> 8;

                if (is_unified_surface()                       // vtable+320
                    || !hashset_has_entries(overlap_set)
                    || (func_id != 0 && !hashset_contains(overlap_set, func_id)))
                {
                    // Unified or non-overlapping: base size only
                    desc_size = vtable_surface_base_size();     // vtable+448
                } else {
                    // Non-unified, overlapping: base + extra offset
                    desc_size = vtable_surface_base_size()      // vtable+448
                              + vtable_surface_extra_offset();  // vtable+352
                }
                break;
        }

        // Build the $NVLINKBINDLESSOFF_ symbol name
        const char* orig_name = desc->name;                    // desc+32
        char* synth_name = arena_alloc(strlen(orig_name) + 20);
        sprintf(synth_name, "$NVLINKBINDLESSOFF_%s", orig_name);

        if (elf->verbose)
            fprintf(stderr, "create %s\n", synth_name);

        // Create the symbol in the output symtab:
        //   type=13, binding=0, flags=129, section=bank_sec_idx, alignment=4
        uint32_t sym_idx = create_symbol(elf, synth_name, 13, 0, 129,
                                          bank_sec_idx, -1, 4, desc_size);
        arena_free(synth_name);

        // Allocate a zero-filled data buffer of desc_size bytes
        void* data = arena_alloc(desc_size);
        memset(data, 0, desc_size);

        // Append data to the pending merge list (elfw+480)
        list_append(data, &elf->pending_merge_list);

        // Merge the data into the constant bank section
        // sub_432B10 handles overlap detection and offset assignment
        merge_into_section(elf, bank_sec_idx, sym_idx, data, -1, 4, desc_size);

        list = list->next;
    }

cleanup:
    hashset_destroy(overlap_set);                          // sub_4650A0
}
```

The descriptor size values are architecture-dependent. Typical values observed across architectures:

| Architecture | `vtable+440` (tex/sampler) | `vtable+448` (surface base) | `vtable+352` (surface extra) |
|---|---|---|---|
| sm50-sm75 | 4 bytes | 4 bytes | 4 bytes |
| sm80-sm90 | 4 bytes | 4 bytes | 4 bytes |
| sm100+ (Mercury) | 4 bytes | 4 bytes | N/A (unified only) |

## Worked Example: Two Kernels with Shared and Private Textures

Consider a CUDA program with two kernels and one device function:

```c
// Input: two cubins merged into one link job

// cubin A defines:
//   kernel_A (entry)   -- calls helper()
//   helper() (device)  -- uses tex_shared (texture), samp_shared (sampler)

// cubin B defines:
//   kernel_B (entry)   -- uses tex_private (texture), surf_unified (surface)
```

### Initial State After Merge

After the merge phase, the output ELF contains:

| Symbol index | Name | `st_info & 0xF` | Section |
|---|---|---|---|
| 5 | `tex_shared` | 10 (texture) | `.nv.constant3` |
| 6 | `samp_shared` | 11 (sampler) | `.nv.constant3` |
| 7 | `tex_private` | 10 (texture) | `.nv.constant3` |
| 8 | `surf_unified` | 12 (surface) | `.nv.constant3` |
| 10 | `kernel_A` | 2 (func) | `.text.kernel_A` |
| 11 | `kernel_B` | 2 (func) | `.text.kernel_B` |
| 12 | `helper` | 2 (func) | `.text.helper` |

The relocation list (`elfw+376`) contains:

| Reloc # | Type | Symbol | Target section | Description |
|---|---|---|---|---|
| R1 | 22 (`R_CUDA_BINDLESSOFF13_22`) | 5 (`tex_shared`) | `.text.helper` | helper uses tex_shared |
| R2 | 23 (`R_CUDA_BINDLESSOFF13_23`) | 6 (`samp_shared`) | `.text.helper` | helper uses samp_shared |
| R3 | 22 (`R_CUDA_BINDLESSOFF13_22`) | 7 (`tex_private`) | `.text.kernel_B` | kernel_B uses tex_private |
| R4 | 42 (`R_CUDA_BINDLESSOFF14_42`) | 8 (`surf_unified`) | `.text.kernel_B` | kernel_B uses surf_unified |

The callgraph records: `kernel_A` calls `helper`.

### Phase B: Collect Per-Entry Sections

The function finds two per-entry constant bank sections:

- Section 20: `.nv.constant3.kernel_A` (sh_type matches parent)
- Section 21: `.nv.constant3.kernel_B` (sh_type matches parent)

### Phase C-D: Bitmask Initialization

```
bitmask[] = all zeros, one byte per section
specific_section_idx = 0  (multi-entry mode)
```

### Phase E: Relocation Scan

**Processing R1** (type 22, sym 5 = `tex_shared`):
- Type 22 matches bindless -> proceed
- sym_type = 10 (texture) -> proceed
- Create `$NVLINKBINDLESSOFF_tex_shared` -> new sym_idx = 15
- Rewrite R1.info = `(15 << 32) | 22`
- bitmask_byte = `0x01` (texture)
- Target section = `.text.helper` -> section index 25
- `bitmask[25] |= 0x01` -> bitmask[25] = 0x01
- Section 25 has flags & 0x04 (function section) -> propagate
- callgraph_get_callers(helper) returns: `[{sym_index: 10}]` (kernel_A)
- kernel_A is in section 20 -> `bitmask[20] |= 0x01` -> bitmask[20] = 0x01

**Processing R2** (type 23, sym 6 = `samp_shared`):
- Type 23 matches bindless -> proceed
- sym_type = 11 (sampler) -> proceed
- Create `$NVLINKBINDLESSOFF_samp_shared` -> new sym_idx = 16
- Rewrite R2.info = `(16 << 32) | 23`
- bitmask_byte = `0x02` (sampler)
- Target section = `.text.helper` -> section index 25
- `bitmask[25] |= 0x02` -> bitmask[25] = 0x03
- Propagate to callers of helper:
  - `bitmask[20] |= 0x02` -> bitmask[20] = 0x03

**Processing R3** (type 22, sym 7 = `tex_private`):
- Type 22 matches bindless -> proceed
- sym_type = 10 (texture) -> proceed
- Create `$NVLINKBINDLESSOFF_tex_private` -> new sym_idx = 17
- Rewrite R3.info = `(17 << 32) | 22`
- bitmask_byte = `0x01` (texture)
- Target section = `.text.kernel_B` -> section index 21
- `bitmask[21] |= 0x01` -> bitmask[21] = 0x01
- Section 21 is a function section -> propagate
- callgraph_get_callers(kernel_B) returns NULL (kernel_B is an entry point, has no callers)

**Processing R4** (type 42, sym 8 = `surf_unified`):
- Type 42 matches bindless -> proceed
- sym_type = 12 (surface) -> proceed
- Create `$NVLINKBINDLESSOFF_surf_unified` -> new sym_idx = 18
- Rewrite R4.info = `(18 << 32) | 42`
- bitmask_byte = `0x03` (surface = both bits)
- Target section = `.text.kernel_B` -> section index 21
- `bitmask[21] |= 0x03` -> bitmask[21] = 0x03

### Bitmask State After Phase E

| Section index | Section name | Bitmask | Has texture | Has sampler | Has surface |
|---|---|---|---|---|---|
| 20 | `.nv.constant3.kernel_A` | `0x03` | yes (via helper) | yes (via helper) | no |
| 21 | `.nv.constant3.kernel_B` | `0x03` | yes | no (bit set from surface) | yes |
| 25 | `.text.helper` | `0x03` | yes | yes | no |

### Phase F: Section Pruning

- Section 20: bitmask[20] = 0x03 != 0 -> keep
- Section 21: bitmask[21] = 0x03 != 0 -> keep

(If there were a `kernel_C` with no bindless references, its constant section would be pruned here.)

### Phase G: Section Layout

`sub_4325A0` assigns offsets within the constant bank to each `$NVLINKBINDLESSOFF_*` symbol:

```
$NVLINKBINDLESSOFF_tex_shared   -> offset 0,   size 4
$NVLINKBINDLESSOFF_samp_shared  -> offset 4,   size 4
$NVLINKBINDLESSOFF_tex_private  -> offset 8,   size 4
$NVLINKBINDLESSOFF_surf_unified -> offset 12,  size 4
```

### Phase H: Relocation Emission

**For section 20 (kernel_A):**

Walk the parent descriptor list. For each descriptor, check if it belongs to section 20 and if the bitmask allows it:

| Descriptor | sym_type | Check | Emit type | Count |
|---|---|---|---|---|
| `tex_shared` | 10 | bitmask[20] & 0x01 = yes | 6 (texture) | texture_count = 1 |
| `samp_shared` | 11 | bitmask[20] & 0x02 = yes | 7 (sampler) | sampler_count = 1 |
| `tex_private` | 10 | section mismatch -> skip | -- | -- |
| `surf_unified` | 12 | section mismatch -> skip | -- | -- |

Emitted relocations for kernel_A:
- `emit_bindless_relocation(elf, 6, 15, section_20, 0, R1.orig)`
- `emit_bindless_relocation(elf, 7, 16, section_20, 0, R2.orig)`

**For section 21 (kernel_B):**

| Descriptor | sym_type | Check | Emit type | Count |
|---|---|---|---|---|
| `tex_shared` | 10 | section mismatch -> skip | -- | -- |
| `samp_shared` | 11 | section mismatch -> skip | -- | -- |
| `tex_private` | 10 | bitmask[21] & 0x01 = yes | 6 (texture) | texture_count = 1 |
| `surf_unified` | 12 | bitmask[21] & 0x03 = yes | 52 (unified surf) | surface_count = 1 |

Emitted relocations for kernel_B:
- `emit_bindless_relocation(elf, 6, 17, section_21, 0, R3.orig)`
- `emit_bindless_relocation(elf, 52, 18, section_21, 0, R4.orig)`

### Phase I: Resource Limit Checking

For kernel_A: texture_count=1, sampler_count=1, surface_count=0 -- all within limits.
For kernel_B: texture_count=1, sampler_count=0, surface_count=1 -- all within limits.

### Final Relocation List

After `sub_438DD0` completes, the relocation list contains both the rewritten input relocations and the newly emitted resolved relocations:

| Reloc | Type | Symbol | Target section | Description |
|---|---|---|---|---|
| R1' | 22 | 15 (`$NVLINKBINDLESSOFF_tex_shared`) | `.text.helper` | Instruction patch |
| R2' | 23 | 16 (`$NVLINKBINDLESSOFF_samp_shared`) | `.text.helper` | Instruction patch |
| R3' | 22 | 17 (`$NVLINKBINDLESSOFF_tex_private`) | `.text.kernel_B` | Instruction patch |
| R4' | 42 | 18 (`$NVLINKBINDLESSOFF_surf_unified`) | `.text.kernel_B` | Instruction patch |
| R5 | 6 | 15 (`$NVLINKBINDLESSOFF_tex_shared`) | `.nv.constant3.kernel_A` | Descriptor offset |
| R6 | 7 | 16 (`$NVLINKBINDLESSOFF_samp_shared`) | `.nv.constant3.kernel_A` | Descriptor offset |
| R7 | 6 | 17 (`$NVLINKBINDLESSOFF_tex_private`) | `.nv.constant3.kernel_B` | Descriptor offset |
| R8 | 52 | 18 (`$NVLINKBINDLESSOFF_surf_unified`) | `.nv.constant3.kernel_B` | Descriptor offset |

R1'-R4' are the original relocations with rewritten symbol targets. These cause the relocation engine to patch the instruction immediates with the offset of the descriptor within the constant bank.

R5-R8 are the newly emitted resolved relocations. These cause the relocation engine to write the actual descriptor data (texture header index, sampler state, surface descriptor) into the per-entry constant bank section at the offset assigned to each `$NVLINKBINDLESSOFF_*` symbol.

## Interaction with Other Passes

### Pre-Requisites

- The **merge phase** must have already collected all texture/sampler/surface symbols and their relocations into the output ELF. The descriptor lists at `elfw+424`, `+432`, `+440` are populated during merge.
- The **callgraph** must be built (`sub_44D200`), because bindless bitmask propagation walks callgraph edges via `sub_44C740`.

### Post-Conditions

- All bindless relocations have been rewritten to target `$NVLINKBINDLESSOFF_` symbols.
- Per-entry constant bank sections have been allocated with correct sizes and alignments.
- The relocation list contains new bindless-type relocations (types 6-9, 52, or Mercury equivalents) that the relocation engine will apply during the relocate phase.
- Sections without bindless references have been pruned from the per-entry list.

### Debug Trace Strings

With verbose mode enabled (`elfw+64 bit 1` set, corresponding to `-v` on the command line):

| String | Source | Meaning |
|---|---|---|
| `"change reloc symbol from %d to %d"` | `sub_438DD0` | Relocation target rewritten to synthetic symbol |
| `"no bindless ref in section %s"` | `sub_438DD0` | Per-entry section pruned (no bindless refs) |
| `"create $NVLINKBINDLESSOFF_%s"` | `sub_433310` | Synthetic symbol created during descriptor layout |
| `"variable %s at offset %d"` | `sub_4325A0` | Descriptor symbol assigned offset in constant bank |
| `"unexpected usage of non-unified surface descriptors"` | `sub_438DD0` | Fatal: non-unified surfaces on Mercury |
| `"unexpected bindless type"` | `sub_43CDA0` | Fatal: symbol type is not 10, 11, or 12 |
| `"symbol not found"` | `sub_43CDA0` | Fatal: bindless symbol missing from symbol table |
| `"too many %s in %s"` | `sub_438DD0` | Fatal: resource count exceeds hardware limit |
| `"callgraph not complete"` | `sub_44C740` | Fatal: callgraph propagation attempted before build |

## Implementation Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `0x438DD0` | `process_bindless_references` | 12,779 B | Core: scan relocs, create synthetics, rewrite targets, emit new relocs |
| `0x43CDA0` | `resolve_bindless_type_symbols` | 6,937 B | Count bindless resources per entry, per type |
| `0x438CE0` | `emit_bindless_relocation` | ~240 B | Create and append a single relocation record to elfw+376 |
| `0x4325A0` | `layout_section` | ~500 B | Assign aligned byte offsets to symbols within a section |
| `0x4324B0` | `bindless_target_setup` | ~1 KB | Create per-entry constant bank section for bindless |
| `0x433310` | `descriptor_list_layout` | ~1.5 KB | Allocate descriptor space in constant bank, create symbols |
| `0x432B10` | `merge_into_section` | ~2 KB | Merge data buffer into section with overlap detection |
| `0x4411B0` | `find_or_create_symbol` | (shared) | Look up symbol by name; create if absent |
| `0x440590` | `get_symbol_record` | (shared) | Retrieve symbol record by index |
| `0x440350` | `get_section_index` | (shared) | Get section index from a symbol record |
| `0x442270` | `get_section_by_rela_index` | (shared) | Get section record from relocation section index |
| `0x44C740` | `callgraph_get_callers` | (shared) | Walk callgraph edges for a function |
| `0x4647D0` | `sort_linked_list` | (shared) | Merge-sort a linked list with comparator function |
| `0x432810` | `reloc_compare_compact` | (shared) | Comparator: compact relocs by section then type |
| `0x432840` | `reloc_compare_standard` | (shared) | Comparator: standard relocs by section then type |

## Cross-References

- [R\_CUDA Relocations](r-cuda-relocations.md) -- relocation type catalog including bindless types (R\_CUDA\_TEX\_\*, R\_CUDA\_SURF\_\*)
- [Relocation Application Engine](relocation-engine.md) -- bit-field patching engine that applies bindless relocations after this pass
- [Section Merging](section-merging.md) -- merge phase that populates the descriptor lists consumed here
- [Symbol Resolution](symbol-resolution.md) -- symbol lookup infrastructure used by `find_or_create_symbol`
- [Data Layout Optimization](data-layout-opt.md) -- constant bank deduplication that runs on the same constant sections
- [Layout Phase](../pipeline/layout.md) -- parent phase that orchestrates bindless processing (Phase 2)
- [Relocation Phase](../pipeline/relocate.md) -- downstream phase that applies the rewritten relocations

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_438DD0` at 0x438DD0 processes bindless relocations (12,779 bytes) | HIGH | Decompiled `sub_438DD0_0x438dd0.c` exists; line 182: `sprintf(v88, "$NVLINKBINDLESSOFF_%s", ...)` confirmed |
| Synthetic symbol naming: `$NVLINKBINDLESSOFF_<name>` | HIGH | String `"$NVLINKBINDLESSOFF_%s"` confirmed in decompiled code and `nvlink_strings.json` |
| `"no bindless ref in section %s"` diagnostic | HIGH | Decompiled `sub_438DD0` line 303: exact format string |
| Mercury detection via `elfw+7 == 0x41` ('A') | HIGH | Consistent with Mercury relocation namespace detection across all linker functions |
| Bindless relocation type classification (types 5, 12, 17-18, 22-25, 29-30, 38-39, 42, 46, 50-51, 54-55, 59, 64-66, 115, 65539-65540) | HIGH | Type dispatch reconstructed from decompiled `sub_438DD0` switch/if-else chain |
| Architecture vtable offsets +296 (supports_bindless), +304 (bindless_texture_type), +312 (bindless_surface_type) | MEDIUM | Vtable offsets inferred from decompiled function pointer calls; consistent across multiple bindless functions |
| `sub_43CDA0` (6,937 bytes) resolves bindless type symbols per entry | MEDIUM | Decompiled file `sub_43CDA0_0x43cda0.c` exists; function purpose inferred from calling context |
| Per-section bitmask for tracking bindless-containing sections | MEDIUM | Reconstructed from Phase C of `sub_438DD0` pseudocode analysis |
| Descriptor sizes from vtable offsets +440 (texture) and +448 (surface) | MEDIUM | Inferred from decompiled function calls; vtable layout consistent with architecture profile |
| `sub_4324B0` creates per-entry constant bank section | LOW | Function exists but not individually verified; role inferred from calling context in layout phase |
