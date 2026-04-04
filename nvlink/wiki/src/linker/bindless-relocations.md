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

| Vtable offset | Query | Purpose |
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

| Decimal | Hex | Inferred name | Category |
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

Types that explicitly **do not** trigger bindless processing (skip to `LABEL_19`) despite being numerically adjacent: 26 (`0x1A`), 27 (`0x1B`), 28 (`0x1C`). These correspond to non-bindless relocation types (likely `R_CUDA_32`, `R_CUDA_64`, and `R_CUDA_CONST` or similar).

## The Bindless Handler (LABEL_32)

When a relocation matches a bindless type, the handler executes the following steps:

### Step 1: Symbol Lookup and Type Check

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

### Step 2: Create Synthetic Symbol

```
name = "$NVLINKBINDLESSOFF_" + sym->name
new_sym_idx = find_or_create_symbol(elfw, name)    // sub_4411B0

if verbose:
    fprintf(stderr, "change reloc symbol from %d to %d\n",
            sym->section_index, new_sym_idx)

free(name)    // arena_free via sub_431000
```

The `$NVLINKBINDLESSOFF_<name>` synthetic symbol acts as a proxy: instead of the relocation pointing at the original texture/surface symbol, it now points at the bindless offset table entry for that symbol. The function `sub_4411B0` either finds an existing symbol with that name (if a previous relocation already created it) or creates a new one.

### Step 3: Rewrite Relocation Target

```
reloc.info = (new_sym_idx << 32) | original_reloc_type
// i.e., keep the relocation type but change the symbol index
```

This is a 64-bit rewrite of the relocation info field. The upper 32 bits encode the symbol table index, the lower 32 bits encode the relocation type. After this rewrite, the relocation engine will resolve the address from the `$NVLINKBINDLESSOFF_` symbol rather than the original texture/surface symbol.

### Step 4: Build Section Bitmask

```
// byte_1D391A0 is a lookup table indexed by (sym_type - 10) * 4:
//   sym_type 10 (texture): bitmask bit 0  -> 0x01
//   sym_type 11 (sampler): bitmask bit 1  -> 0x02
//   sym_type 12 (surface): bitmask bits 0+1 -> 0x03

bitmask_byte = byte_1D391A0[4 * (sym_type - 10)]
section_bitmask[reloc.section_index] |= bitmask_byte
```

A VLA-sized bitmask (`alloca(num_sections + 1)` at function entry) tracks which sections contain references to each resource type. Each byte in the bitmask stores up to three flags:

| Bit | Meaning |
|---|---|
| 0 | Section references at least one bindless texture |
| 1 | Section references at least one bindless sampler |
| 0+1 | Section references at least one bindless surface (both bits set) |

### Step 5: Propagate Bitmask Through Callgraph

If the section containing the relocation is a function section (section flags bit 2 set), the bitmask is propagated to all callers:

```
section_record = get_section_by_index(elfw, reloc.section_index)
if section_record.flags & 0x04:   // is a function section
    if specific_constant_section:
        section_bitmask[specific_constant_idx] |= bitmask_byte
    else:
        // Walk callgraph edges to find all entry functions that call this function
        callgraph = sub_44C740(elfw, function_id)
        for each caller in callgraph:
            caller_sym = get_symbol_record(elfw, caller.sym_index)
            caller_section = get_section_index(elfw, caller_sym)
            section_bitmask[caller_section] |= bitmask_byte
```

This ensures that if a non-entry function references a bindless texture, the bitmask correctly marks the entry-function sections that transitively reach that reference.

## Post-Scan Section Pruning

After the relocation scan completes, the function iterates the list of per-entry constant bank sections (collected in `v94` during an earlier scan of the all-symbols vector at `elfw+360`):

```
for each constant_section in per_entry_list:
    if section_bitmask[constant_section.section_index] == 0:
        // No bindless references reach this section
        if verbose:
            fprintf(stderr, "no bindless ref in section %s\n",
                    constant_section->name)
        constant_section.data = NULL    // mark for removal
```

Sections that contain no bindless references are nulled out, preventing unnecessary allocation of bindless offset table space. This is a size optimization: without it, every per-entry constant section would receive a bindless offset table regardless of whether any kernel in that section actually uses bindless access.

## Per-Entry Bindless Allocation

After pruning, the function calls `sub_4325A0` (the section layout engine) to assign aligned offsets within the bindless constant bank. It then iterates the surviving per-entry sections and processes their descriptor lists:

```
for each surviving per_entry section:
    // Copy layout parameters from the parent section descriptor
    per_entry.alignment = parent.alignment
    per_entry.size      = parent.size

    for each descriptor in parent.descriptor_list (from a2[9]):
        sym = get_symbol_record(elfw, descriptor.symbol_index)
        bindless_sym = find_symbol(elfw, sym->name + 19)  // skip "$NVLINKBINDLESSOFF_" prefix
        section_idx = get_section_index(elfw, bindless_sym)

        // Validate the section matches
        if section_idx != 0 && section_idx != per_entry.section_index:
            skip    // descriptor belongs to a different section

        sym_type = bindless_sym->st_info & 0x0F
        switch (sym_type):
            case 10 (texture):
                reloc_type = arch_a_variant ? 65547 : 6
                if !(section_bitmask[per_entry.section_idx] & 0x01):
                    skip
                texture_count++

            case 11 (sampler):
                reloc_type = arch_a_variant ? 65548 : 7
                if !(section_bitmask[per_entry.section_idx] & 0x02):
                    skip
                sampler_count++

            case 12 (surface):
                if is_unified_surface_descriptor():
                    reloc_type = arch_a_variant ? 65549 : 52
                else:
                    // Check if entry uses non-standard surface offset
                    reloc_type = 8 or 9 depending on descriptor.offset
                if !(section_bitmask[per_entry.section_idx] & 0x03):
                    skip
                surface_count++

        emit_bindless_relocation(elfw, reloc_type, bindless_sym_index,
                                 per_entry.section_index, 0,
                                 descriptor.original_reloc)
```

The relocation types emitted during allocation differ from the input types. These are the **resolved** types used by the relocation application engine:

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

These limits are hard limits imposed by the GPU hardware. Exceeding them produces a fatal linker error via `sub_467460`.

## Resolve Bindless Type Symbols (`sub_43CDA0`)

This companion function counts bindless resources of a given type (texture, sampler, or surface) for a specific entry function. It is called during Phase 10 of the layout pass (resource counting).

The function has two major code paths depending on whether the architecture supports bindless:

### Path A: Bindless Supported (`vtable+296` returns true)

1. Look up the constant bank section type for the resource:
   - Texture/sampler (type 10 or 11): `vtable+304` (bindless texture bank type)
   - Surface (type 12): `vtable+312` (bindless surface bank type), with special handling for unified surfaces via `vtable+320`
2. Construct the section name `"<bank_type>.<entry_name>"` (e.g., `".nv.constant3.my_kernel"`)
3. Look up the section in the ELF via `sub_4411D0`
4. Scan the relocation list (`elfw+376`) counting relocations whose type matches the resolved bindless type code and whose target section matches the constructed section name
5. Return the count

For textures and samplers, the resolved type codes are 6 and 7 (or `0x1000B` and `0x1000C` for a-variant). For surfaces, the code is 8 (or 52/`0x1000D` for unified).

If the architecture has the "wide relocation" flag set (checked at `elfw+37 bit 1` or `elfw+49 bit 1` depending on 32/64-bit mode), the function first sorts the relocation list (`sub_4647D0` with comparator `sub_432810` or `sub_432840`) before counting. This sort groups relocations by section, enabling faster counting.

### Path B: Non-Bindless Architecture

When bindless is not supported, the function falls back to counting resource descriptors in the legacy resource lists (`elfw+392`, which holds the sampler/texture/surface descriptor records). It iterates the list, matching descriptors by type byte (at `descriptor+1`: value 2 indicates a resource descriptor) and section index, counting those whose symbol type matches the requested type.

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

## Descriptor List Layout (`sub_433310`)

Before `sub_438DD0` runs, the layout phase calls `sub_433310` three times (once each for textures at `elfw+424`, samplers at `elfw+432`, and surfaces at `elfw+440`). This function allocates constant-bank space for each descriptor:

1. For surface descriptors (type 12), perform a pre-scan of the sampler/resource list (`elfw+392`) to build an overlap set. Resource records with type byte 36 that reference sections marked as shared (`symbol flags bit 4 set`) are tracked to determine which sections share surface descriptors.

2. For each descriptor in the list:
   - Query the architecture vtable for the constant bank section type (`vtable+304`)
   - Find or create the section named after the bank type
   - Determine the descriptor size:
     - Texture/sampler: `vtable+440` bytes
     - Surface (unified): `vtable+448` bytes
     - Surface (non-unified, non-overlapping): `vtable+448 + vtable+352` bytes
   - Allocate a `$NVLINKBINDLESSOFF_<name>` symbol with that size
   - Allocate a zero-filled data buffer of descriptor size
   - Merge the data into the constant bank section via `sub_432B10`
   - Verbose: `"create $NVLINKBINDLESSOFF_<name>"`

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
| `"unexpected usage of non-unified surface descriptors"` | `sub_438DD0` | Fatal: non-unified surfaces on Mercury |
| `"unexpected bindless type"` | `sub_43CDA0` | Fatal: symbol type is not 10, 11, or 12 |
| `"symbol not found"` | `sub_43CDA0` | Fatal: bindless symbol missing from symbol table |
| `"too many %s in %s"` | `sub_438DD0` | Fatal: resource count exceeds hardware limit |

## Implementation Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `0x438DD0` | `process_bindless_references` | 12,779 B | Core: scan relocs, create synthetics, rewrite targets, emit new relocs |
| `0x43CDA0` | `resolve_bindless_type_symbols` | 6,937 B | Count bindless resources per entry, per type |
| `0x438CE0` | `emit_bindless_relocation` | ~240 B | Create and append a single relocation record to elfw+376 |
| `0x4324B0` | `bindless_target_setup` | ~1 KB | Create per-entry constant bank section for bindless |
| `0x433310` | `descriptor_list_layout` | ~1.5 KB | Allocate descriptor space in constant bank, create symbols |
| `0x4411B0` | `find_or_create_symbol` | (shared) | Look up symbol by name; create if absent |
| `0x440590` | `get_symbol_record` | (shared) | Retrieve symbol record by index |
| `0x440350` | `get_section_index` | (shared) | Get section index from a symbol record |
| `0x442270` | `get_section_by_rela_index` | (shared) | Get section record from relocation section index |
| `0x44C740` | `callgraph_get_callers` | (shared) | Walk callgraph edges for a function |
