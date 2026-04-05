# Unified Function Tables

CUDA device code supports indirect function calls (function pointers) and virtual function dispatch on the GPU. The mechanism that makes this work at the linker level is the **Unified Function Table (UFT)** system -- a set of ELF sections that the compiler emits and the linker merges, reorders, and patches so that every indirect call site can jump through a table of known offsets at runtime. A parallel structure called the **Unified Data Table (UDT)** handles indirect data references (unified texture/surface descriptors). Both tables share the same UUID-based identification scheme and index file infrastructure.

The UFT system is the device-side equivalent of a PLT/GOT in a host ELF linker: the compiler emits stub functions that jump through a table entry, and the linker fills in the table at link time so each entry points to the real function body. The key difference is that CUDA devices do not support lazy binding -- all entries are resolved statically at link time.

> **Source evidence**: All structure layouts, algorithms, and constants in this page are derived from decompiled functions in the nvlink binary (v13.0.88). Field offsets and sizes are confirmed by `sh_entsize` parameters passed to section-creation functions and by pointer arithmetic in the reorder/merge loops.

## Key Facts

| Property | Value |
|---|---|
| Function table section | `.nv.uft` |
| Function table relocation section | `.nv.uft.rel` |
| Per-entry function table | `.nv.uft.entry` |
| Data table section | `.nv.udt` |
| Per-entry data table | `.nv.udt.entry` |
| Index file section | `.nv.uidx` |
| CLI option for index file | `--uidx-file` / `-uidx` |
| Reorder function | `sub_4637B0` (10,141 bytes at `0x4637B0`) |
| Setup function | `sub_463F70` (3,978 bytes at `0x463F70`) |
| Entry merge context | `sub_442820` (`elfw_merge_symbols`, 5,371 bytes at `0x442820`) |
| Property computation | `sub_451D80` (`compute_entry_properties`, 97,969 bytes at `0x451D80`) |
| Entry identification | 128-bit UUID per entry (`<%016llx,%016llx>` format) |
| Stub naming pattern | `__cuda_uf_stub_<function_name>` |
| Stub attribute | `.unified_func_stub` |
| Global offset symbol | `__UFT_OFFSET` |
| Global data offset symbol | `__UDT_OFFSET` |
| Canonical function symbol | `__UFT_CANONICAL` |
| Canonical data symbol | `__UDT_CANONICAL` |
| Table start symbol | `__UFT` / `__UDT` |
| Table end symbol | `__UFT_END` / `__UDT_END` |
| CUDA relocation types | `R_CUDA_UNIFIED` (12 variants: base, `_32`, `_8_0` through `_8_56`, `32_HI_32`, `32_LO_32`) |
| Mercury relocation types | `R_MERCURY_UNIFIED` (14 variants: base, `_32`, `_8_0` through `_8_56`, `32_HI`, `32_LO`) |
| Relocation type range (CUDA) | Types 102--113 |
| Relocation type range (Mercury) | Types 65586--65599 (0x10032--0x1003F) |

## Background: Indirect Calls on CUDA GPUs

When CUDA source code takes the address of a device function or uses a virtual method call, the compiler cannot resolve the target at compile time because the final code layout is not known until link time. The compiler instead:

1. Emits a **stub function** named `__cuda_uf_stub_<funcname>` with the `.unified_func_stub` attribute. The stub body is a single `_jcall` instruction that jumps to the real function.
2. Emits a **`.nv.uft.entry`** record containing a 128-bit UUID that uniquely identifies this indirect-call target.
3. Emits a **`.nv.uft.rel`** section alongside the function's `.text.<funcname>` section, containing relocation entries that reference `__UFT_OFFSET` to locate the jump table slot.
4. Marks the compilation unit as `"Using indirect function calls"`, which requires ABI mode (the error `"Indirect function call requires ABI"` is fatal if ABI is not enabled).

The PTX-level representation of a stub function is:

```
.func .attribute(.unified_func_stub) __cuda_uf_stub_myFunc( ) {
  _jcall myFunc;
}
```

The `_jcall` pseudo-instruction compiles to an indirect jump through the UFT slot assigned to `myFunc`.

At the CUDA source level, the conditions that trigger UFT generation are:
- Taking the address of a `__device__` function (`void (*fp)(int) = &myDeviceFunc;`)
- Virtual method calls on device-side objects (`obj->virtualMethod()`)
- `std::function`-like wrappers in device code

The diagnostic string `"Syscall compilation of Indirect function calls"` indicates a separate compilation path where indirect calls are lowered through a syscall mechanism rather than direct jumps, used in some debugging or compatibility scenarios.

## Pipeline Position

```
Merge Phase (sub_45E7D0, per-object)
  |
  +-- sub_442820: merge symbols including __cuda_uf_stub_* and .nv.uft sections
  |
  v
Layout Phase (sub_439830)
  |
  +-- Phases 1-9: globals, shared, constants, etc.
  +-- Phase 10: UFT/UDT setup
  |     |
  |     +-- sub_463F70: create/validate .nv.uft, .nv.udt, .nv.uft.entry,
  |     |               .nv.udt.entry sections
  |     +-- sub_4637B0: reorder UFT/UDT entries by UUID
  |     |
  v
Relocation Phase (sub_469D60)
  |
  +-- Unified relocation remapping (types 102-113 / 65586-65599)
  +-- Resolve __UFT_OFFSET, __UDT_OFFSET to actual section offsets
  +-- Patch jump slot addresses into .text sections
  |
  v
Finalization Phase (sub_445000)
  |
  +-- sub_451D80: compute_entry_properties (validates UFT, propagates
  |               register counts through indirect callees)
  v
Output ELF
```

## `.nv.uft` -- Function Jump Table

The `.nv.uft` section is a flat array of jump slots. Each slot is a fixed-size entry (architecture-dependent) that holds the address of a device function reachable via indirect call. The total section size equals `uftNumEntries * slot_size`.

### Section Properties

| Property | Value |
|---|---|
| `sh_type` | `0x7000000E` (NVIDIA custom: `SHT_NVIDIA_UFT`) |
| `sh_flags` | `0x6` (SHF_ALLOC \| SHF_WRITE) |
| `sh_addralign` | 128 |
| `sh_entsize` | 128 (the jump slot size for CUDA architectures) |

The section is created during Phase 10 of the layout phase (`sub_463F70`). The slot size (128 bytes) is the `sh_entsize` value passed when the section is first allocated in `sub_442820`. For Mercury (sm100+) architectures where `dpc_mode == 2`, the linker computes a supplementary padding size as `2 * arch_vtable->get_instruction_size()` (called through the arch dispatch vtable at offset +624). This padding is interleaved between jump slots to form a combined slot that holds both the jump instruction and its scheduling data.

### Validation

The linker validates that:

- The `.nv.uft.entry` section exists (fatal: `"missing nv.uft.entry"`)
- The number of jump slots matches the entry count (fatal: `"Number of .nv.uft jump slots != Number of entries in .nv.uft.entry"`). The check computes `section_size / sh_entsize` for both sections and compares them.
- If a UIDX file is provided, the window size matches the section size (fatal: `"size of uidx window != nv.uft"`)

The relationship between jump slots and entries is 1:1 -- each `.nv.uft.entry` record maps to exactly one jump slot in `.nv.uft`.

Validation pseudocode from `sub_463F70`:

```
uft_sec  = find_section(ctx, ".nv.uft")
if (!uft_sec) goto process_udt;

if (ctx->uidx == NULL)
    fatal("Unified symbols found but index file not specified.")

entry_sec = find_section(ctx, ".nv.uft.entry")
if (!entry_sec)
    fatal("missing nv.uft.entry")

uft_data    = get_section_data(ctx, uft_sec)
entry_data  = get_section_data(ctx, entry_sec)

uft_slots   = uft_data->sh_size / uft_data->sh_entsize
entry_count = entry_data->sh_size / entry_data->sh_entsize

if (uft_slots != entry_count)
    fatal("Number of .nv.uft jump slots != Number of entries in .nv.uft.entry")

uidx = ctx->uidx                         // offset +656 in the linker context
uidx_window = uidx->uft_window_size      // at uidx+16

if (verbose_level & 0x10)
    fprintf(stderr, "uftWindowSize = %llu\n.nv.uft section size = %llu\n",
            uidx_window, uft_data->sh_size)

if (uft_data->sh_size != uidx_window)
    fatal("size of uidx window != nv.uft")

uft_reorder_entries(ctx, uft_data, entry_data,
                    &uidx->uft_entries[0], uidx->uft_count, /*is_udt=*/0)
```

### Synthetic Symbols

The linker creates several synthetic symbols that reference the `.nv.uft` section. These are created on demand by `sub_162E070` (ptxas-side) the first time each symbol name is encountered, and by `sub_444A20` (linker-side classifier that returns true for any of the eight unified synthetic names):

| Symbol | Context field offset | Purpose |
|---|---|---|
| `__UFT_OFFSET` | `a1[3768]` | Offset within constant memory where the UFT base pointer resides; used by `_jcall` stubs |
| `__UDT_OFFSET` | `a1[3769]` | Same for UDT |
| `__UFT_CANONICAL` | `a1[3770]` | Canonical UFT symbol for deduplication during relocatable linking |
| `__UDT_CANONICAL` | `a1[3771]` | Same for UDT |
| `__UFT` | `a1[3772]` | Points to start of `.nv.uft` section |
| `__UDT` | `a1[3773]` | Points to start of `.nv.udt` section |
| `__UFT_END` | `a1[3774]` | Points to end (start + size) of `.nv.uft` section |
| `__UDT_END` | `a1[3775]` | Points to end of `.nv.udt` section |

All eight are created with 64-bit size, STB_GLOBAL binding, and property tag `106`. The creation function (`sub_162E070`) also conditionally registers a secondary ptxas-internal representation for these symbols when the LTO flag at `ctx->options + 1792` is set, routing through the ptxas temp section `<ptxOptTemps>`.

The classifier function `sub_444A20` at `0x444A20` (28 bytes) tests a symbol name against all eight names in a strcmp chain and returns `true` if the name matches any of them. The relocation phase uses this to identify unified synthetic symbols for special handling.

## `.nv.uft.rel` -- Per-Function Relocation Entries

Each device function that contains indirect call sites produces a `.nv.uft.rel` section alongside its `.text.<funcname>` section. This section is emitted by the LTO/ptxas backend function `sub_14075D0` (`ptx_emit_function_body`) during code generation.

The `.nv.uft.rel` section contains relocation entries that reference unified table slots. These relocations use the `R_CUDA_UNIFIED` family of relocation types (or `R_MERCURY_UNIFIED` on sm100+). The entries tell the linker which instruction operands need to be patched with the final UFT slot offset.

### Naming Convention

The naming depends on the input file type:

- **In relocatable objects (`ET_REL`, `e_type == 1`)**: The section is named `.nv.uft.rel.<funcname>`, where `<funcname>` is derived by stripping the `__cuda_uf_stub_` prefix from the stub symbol name. This per-function naming allows separate tracking of relocations for each indirect-call target.

- **In non-relocatable inputs**: The section name is simply `.nv.uft.rel`, and the association between a function and its UFT relocations is maintained through the relocation entries' symbol indices.

This naming split is handled by `sub_469230` at `0x469230`, which checks `e_type == 2` and whether the section name starts with `.nv.uft.rel`. When it detects a per-function `.nv.uft.rel.<funcname>` in a relocatable object, it strips the function suffix and redirects the relocation processing to the parent `.nv.uft` section:

```
if (e_type == ET_EXEC && starts_with(section_name, ".nv.uft.rel")):
    // Redirect to the base .nv.uft section for relocation processing
    section_name = ".nv.uft"
```

### Section Type

All `.nv.uft.rel` sections are created with `sh_type = 0x7000000E` (same as `.nv.uft` itself), `sh_flags = 6`, and the same 128-byte alignment and entry size as the main table. This ensures the relocation section is treated as part of the UFT family by the linker's section-type dispatch.

## `.nv.uft.entry` -- Per-Entry Table

The `.nv.uft.entry` section is a structured array where each record identifies one indirect-call target via its 128-bit UUID. The compiler assigns a unique UUID to each device function whose address is taken or which participates in virtual dispatch.

### Section Properties

| Property | Value |
|---|---|
| `sh_type` | `0x70000011` (NVIDIA custom: `SHT_NVIDIA_UFT_ENTRY`) |
| `sh_addralign` | 8 |
| `sh_entsize` | 32 |

### Entry Record Layout (32 bytes)

Each 32-byte entry record has the following layout, confirmed by pointer arithmetic in `sub_4637B0`, `sub_4633A0`, and `sub_464240`:

```
Offset  Size  Field         Description
------  ----  -----------   ------------------------------------------
 0x00     4   symidx        Symbol table index of the real function
                            (patched from virtual to real during merge)
 0x04     4   flags         Bit 31 (0x80000000): "unvisited" marker,
                            set during reorder, cleared when assigned
 0x08     8   offset        Byte offset of this entry's jump slot
                            within the .nv.uft section
 0x10     8   uuid_lo       Low 64 bits of the 128-bit UUID
 0x18     8   uuid_hi       High 64 bits of the 128-bit UUID
```

The `flags` field's bit 31 is used as a "not yet placed" marker during the reorder pass: `sub_4637B0` sets `entry->flags |= 0x80000000` for every entry before reordering, then clears it with `entry->flags &= ~0x80000000` once the entry has been assigned to its final slot. If an entry is encountered that already has bit 31 clear, the linker fatals with `"entry was already found?"`.

The `symidx` field initially holds the virtual symbol index from the input object's local symbol table. After merge, `sub_4633A0` patches it to the real symbol index in the merged symbol table. The verbose trace shows both:

```
Patching real symidx in UFT Entry with UUID 0x%llx-0x%llx
  Virtual symidx = %d
  Real symidx    = %d
```

### Entry Processing Pipeline

The linker processes entries through five stages:

**Stage 1: Addition** (`sub_464240` at `0x464240`). When a new indirect-call target is discovered, the linker calls this function to append an entry record:

```
sub_464240(ctx, entry_record):
    if (ctx->uft_entry_section_idx == 0):
        // First entry -- create the .nv.uft.entry section
        idx = create_section(ctx, ".nv.uft.entry",
                             sh_type=0x70000011, sh_flags=0,
                             sh_link=ctx->machine_arch,
                             sh_info=0, sh_addralign=8, sh_entsize=32)
        sym = get_symbol(ctx, idx)
        ctx->uft_entry_section_idx = to_section_index(ctx, sym)

    // Append to the linked list at ctx+480
    list_append(entry_record, &ctx->uft_entry_list)

    if (verbose & 0x1):
        fprintf(stderr, "Adding UFT Entry\n  uuid   = 0x%llx-0x%llx\n  offset = 0x%llx\n",
                entry_record->uuid_lo, entry_record->uuid_hi, entry_record->offset)
        fprintf(stderr, "  symidx = %d\n", entry_record->symidx)

    // Write 32 bytes into the section, aligned to 8-byte boundaries
    write_section_data(ctx, ctx->uft_entry_section_idx, entry_record, 8, 32)
```

**Stage 2: Merge** (`sub_45CF00` at `0x45CF00`). During ELF merge, `.nv.uft.entry` sections from each input object are processed:

```
sub_45CF00(ctx, input_obj, ...):
    entry_data = (is_64bit) ? load_64(obj, entry_header)
                            : load_32(obj, entry_header)
    num_entries = entry_data.size >> 5       // divide by 32 (entry size)

    if (verbose & 0x10):
        fprintf(stderr, "UFT Entry Merge\n  Number of UFT Entries in ET_REL is %d\n",
                num_entries)

    for each entry in entry_data (stride=32):
        // Remap virtual symidx through the symbol map from the input object
        entry->symidx = symbol_map[entry->symidx]
        entry->flags |= 6       // mark as merged
        real_sym = get_symbol(ctx, entry->symidx)
        entry->offset = real_sym->value    // copy the symbol's current offset

        if (verbose & 0x10):
            fprintf(stderr, "  er-symidx = %d\n  ew-symidx = %d\n  stub name = %s\n",
                    orig_symidx, entry->symidx, real_sym->name)
            fprintf(stderr, "  offset    = 0x%llx\n", entry->offset)
```

**Stage 3: Symbol index patching** (`sub_4633A0` at `0x4633A0`). After all objects are merged, this function iterates every entry in `.nv.uft.entry` and replaces virtual symbol indices with real ones:

```
sub_4633A0(ctx, uft_entry_section_idx):
    sec_data = get_section_data(ctx, uft_entry_section_idx)
    for each chunk in sec_data->chunk_list:
        raw = chunk->data
        num_entries = chunk->size >> 5     // entries are 32 bytes
        for i = 0 to num_entries-1:
            entry = &raw[i * 32]
            if (verbose & 0x1):
                fprintf(stderr, "Patching real symidx in UFT Entry with UUID 0x%llx-0x%llx\n",
                        entry->uuid_lo, entry->uuid_hi)
                fprintf(stderr, "  Virtual symidx = %d\n", entry->symidx)

            entry->symidx = remap_symbol_index(ctx, entry->symidx)

            if (verbose & 0x1):
                fprintf(stderr, "  Real symidx    = %d\n", entry->symidx)
```

The dispatch function `sub_464400` at `0x464400` calls `sub_4633A0` once for `.nv.uft.entry` and once for `.nv.udt.entry`.

**Stage 4: UUID validation and map building** (first half of `sub_4637B0`). See [Slot Assignment Algorithm](#slot-assignment-algorithm) below.

**Stage 5: Reorder** (second half of `sub_4637B0`). See [Slot Assignment Algorithm](#slot-assignment-algorithm) below.

### UUID Lookup

The function `sub_4637B0` builds a hash map from `uuid_lo XOR uuid_hi` to entry records. Lookup uses the trace `"get uft entry for <%016llx,%016llx>\n"`. If a UUID is not found in the map, the linker reports `"matching uuid not found"`. If two UUIDs XOR to the same key, `"uft map conflict: 0x%llx\n"` is reported and the conflicting entries are tracked in a separate linked list for linear-scan resolution.

### Stub Symbol Matching

During merge, the linker matches `__cuda_uf_stub_<name>` symbols to their corresponding UFT entries via `sub_463660` at `0x463660`:

```
sub_463660(ctx, stub_sym):
    func_name = stub_sym->name                 // e.g. "myFunc"
    prefix = <16-byte constant>                // "__cuda_uf_stub_" prefix
    lookup_name = prefix + func_name           // "__cuda_uf_stub_myFunc"
    candidate_idx = find_symbol_by_name(ctx, lookup_name)
    candidate = get_symbol(ctx, candidate_idx)

    // Check if the candidate's section index matches the stub's section
    if (candidate->section_idx == stub_sym->section_idx):
        return candidate       // fast path: name match is unambiguous

    // Slow path: name was not unique (multiple functions with same name
    // in different compilation units)
    if (verbose & 0x1):
        fprintf(stderr, "UFT symbol name %s not unique so search\n", candidate->name)

    // Linear scan through all symbols looking for a function symbol
    // (STT_FUNC, type nibble == 2) with matching section index and name hash
    for i = 1 to symbol_count(ctx):
        sym = get_symbol_by_index(ctx->symtab, i)
        if ((sym->st_info & 0xF) == 2            // STT_FUNC
            && sym->section_idx == stub_sym->section_idx
            && sym->name_hash == candidate->name_hash):
            return sym

    fatal("UFT stub match not found")
```

The prefix constant at `xmmword_1D3C1B0` is a 16-byte value that produces the `"__cuda_uf_stub_"` string when concatenated with the function name.

## Slot Assignment Algorithm

The reorder function `sub_4637B0` (10,141 bytes at `0x4637B0`) is the core of the UFT system. It takes the merged entry table, builds a UUID-to-entry hash map, then walks the UIDX-specified ordering to assign each entry to its final jump slot position. The same function handles both UFT and UDT reordering via the `is_udt` flag (parameter `a6`).

### Phase 1: Hash Map Construction

```
uft_reorder_entries(ctx, table_sec, entry_sec, uidx_entries, uidx_count, is_udt):

    // Determine if Mercury interleaving is needed
    if (ctx->dpc_mode == 2 && !is_udt):
        interleave = true
        padding_size = 2 * arch_vtable->get_instruction_size()
        expanded_size = table_sec->sh_size + uidx_count * padding_size
    else:
        interleave = false
        padding_size = 0
        expanded_size = 0

    // Create hash map: key -> entry_record
    hashmap = create_hashmap(key_cmp=sub_44E150, key_hash=sub_44E160, bucket_size=16)

    // Also maintain three parallel linked lists for conflict resolution:
    //   conflict_uuids_hi, conflict_uuids_lo, conflict_entries
    conflict_hi = NULL
    conflict_lo = NULL
    conflict_entries = NULL

    // Iterate all entry chunks in the entry section
    for each chunk in entry_sec->chunk_list:
        raw_data = chunk->data
        remaining = chunk->size
        ptr = raw_data

        while (remaining > 0):
            remaining -= 32                      // each entry is 32 bytes
            uuid_lo = *(uint64_t*)(ptr + 16)
            uuid_hi = *(uint64_t*)(ptr + 24)
            ptr->flags |= 0x80000000             // mark "unvisited"
            key = uuid_hi ^ uuid_lo              // XOR hash

            if (verbose & 0x2):
                fprintf(stderr, "map uid <%llx,%llx> to key=%llx\n",
                        uuid_lo, uuid_hi, key)

            if (hashmap_contains(hashmap, key)):
                // Hash collision -- check if it's a true UUID duplicate
                if (verbose & 0x2):
                    fprintf(stderr, "uft map conflict: 0x%llx\n", key)

                // Walk conflict lists to check for exact UUID match
                for each (hi_node, lo_node) in zip(conflict_hi, conflict_lo):
                    if (lo_node->value == uuid_hi && hi_node->value == uuid_lo):
                        fatal("duplicate ids in uft.entry")

                // Not a duplicate -- add to conflict linked lists
                list_prepend(uuid_lo, &conflict_hi)
                list_prepend(uuid_hi, &conflict_lo)
                list_prepend(ptr,     &conflict_entries)
            else:
                hashmap_insert(hashmap, key, ptr)

            ptr += 32
```

The XOR hash `uuid_hi ^ uuid_lo` reduces the 128-bit UUID to a 64-bit key for the hash map. Collisions (different UUIDs that XOR to the same 64-bit value) are handled by the conflict linked lists. True duplicates (identical UUIDs) are fatal.

### Phase 2: Slot Assignment

After the hash map is built, the function allocates a contiguous output buffer for the reordered table data, then iterates the UIDX entries in order:

```
    // Allocate output buffer matching the table section size
    output_buf = allocate(table_sec->sh_size)

    if (verbose & 0x10):
        fprintf(stderr, is_udt ? "Re-ordering UDT entries\n"
                                : "Re-ordering UFT entries\n")

    if (uidx_count == 0):
        goto finalize

    output_offset = 0
    max_written = 0

    for i = 0 to uidx_count-1:
        uidx_entry = &uidx_entries[i]       // 24 bytes per UIDX entry
        target_uuid_lo = uidx_entry->uuid_lo   // offset +0
        target_uuid_hi = uidx_entry->uuid_hi   // offset +8
        target_offset  = uidx_entry->offset    // offset +16

        if (verbose & 0x2):
            fprintf(stderr, "get uft entry for <%016llx,%016llx>\n",
                    target_uuid_lo, target_uuid_hi)

        // Try conflict list first (for entries with hash collisions)
        found_entry = NULL
        if (conflict_hi != NULL):
            for each (hi_node, lo_node, ent_node) in conflict lists:
                if (lo_node->value == target_uuid_hi
                    && hi_node->value == target_uuid_lo):
                    found_entry = ent_node->value
                    break

        if (found_entry == NULL):
            // Look up in hash map by XOR key
            key = target_uuid_lo ^ target_uuid_hi
            found_entry = hashmap_lookup(hashmap, key)
            if (found_entry == NULL):
                fatal("matching uuid not found")
            // Verify UUID matches (hash collision could give wrong entry)
            if (found_entry->uuid != target_uuid):
                fatal("matching uuid not found")

        if (found_entry->flags >= 0):          // bit 31 already clear
            fatal("entry was already found?")

        if (verbose & 0x10):
            fprintf(stderr, "  Index file UUID = 0x%llx-0x%llx\n",
                    target_uuid_lo, target_uuid_hi)
            fprintf(stderr, "  Mapped Entry:\n"
                    "    symidx          = %d\n"
                    "    orig-offset     = 0x%llx\n"
                    "    re-order offset = 0x%llx\n",
                    found_entry->symidx,
                    found_entry->offset,
                    target_offset)

        // Read the original jump slot data from the input section
        src_data = get_raw_data_at(ctx, table_sec, found_entry->offset)

        if (is_udt):
            // UDT: copy entry->sym->size bytes
            sym = get_symbol(ctx, found_entry->symidx)
            sym->value = target_offset
            slot_bytes = sym->size
            memcpy(&output_buf[target_offset], src_data, slot_bytes)
        else:
            // UFT: look up the real function symbol via sub_463660
            real_sym = find_matching_function(ctx, get_symbol(ctx, found_entry->symidx))
            real_sym->value = target_offset
            slot_bytes = table_sec->sh_entsize    // 128 bytes
            memcpy(&output_buf[target_offset], src_data, slot_bytes)

        // Update entry's offset to the new position
        found_entry->offset = target_offset
        end_pos = target_offset + slot_bytes
        if (end_pos > max_written):
            max_written = end_pos

        // Clear the "unvisited" marker
        found_entry->flags &= ~0x80000000
```

### Phase 3: Mercury Interleaving (DPC Mode 2)

For Mercury architectures (`ctx->dpc_mode == 2`) processing UFT (not UDT), the reordered table undergoes an additional interleaving step. Each 128-byte jump slot is expanded by inserting `padding_size` bytes of scheduling data after every 128 bytes of instruction data:

```
    if (interleave):
        // Allocate expanded buffer: uidx_count * (128 + padding_size)
        expanded_buf = allocate(expanded_size)

        // Interleave: copy 128 bytes of jump slot, then padding_size bytes of zeros
        src = output_buf
        dst = expanded_buf
        for i = 0 to uidx_count-1:
            memcpy(dst, src, 128)             // copy 8 x 16-byte xmm registers
            memcpy(dst + 128, zero_pad, padding_size)
            src += 128
            dst += 128 + padding_size

        // Replace original output buffer
        free(output_buf)
        output_buf = expanded_buf
        max_written = expanded_size
```

The copy loop uses eight consecutive 128-bit SSE loads/stores (`_mm_loadu_si128`) to copy each 128-byte slot, then `memcpy` for the padding region. This matches the pattern visible at lines 327-340 of the decompiled `sub_4637B0`.

### Phase 4: Finalization

```
    // Write the reordered data back to the section
    replace_section_data(ctx, table_sec, output_buf, max_written)

    // Clean up
    hashmap_destroy(hashmap)
    list_free(conflict_hi)
    list_free(conflict_lo)
    list_free(conflict_entries)
```

After finalization, the section's chunk list is cleared (`chunk_list = NULL`, `sh_size = 0`) and replaced with the output buffer via `sub_432B10`. This ensures subsequent phases see the reordered data.

## `.nv.udt` -- Unified Data Table

The `.nv.udt` section is the data counterpart to `.nv.uft`. It holds entries for indirect data references -- primarily unified texture and surface descriptors. When a kernel accesses a texture or surface through a handle rather than a statically bound slot, the handle indexes into the UDT.

The structure mirrors the UFT:

| Property | UFT equivalent | UDT equivalent |
|---|---|---|
| Main section | `.nv.uft` | `.nv.udt` |
| Per-entry section | `.nv.uft.entry` | `.nv.udt.entry` |
| Start symbol | `__UFT` | `__UDT` |
| End symbol | `__UFT_END` | `__UDT_END` |
| Offset symbol | `__UFT_OFFSET` | `__UDT_OFFSET` |
| Canonical symbol | `__UFT_CANONICAL` | `__UDT_CANONICAL` |
| Reorder trace | `"Re-ordering UFT entries\n"` | `"Re-ordering UDT entries\n"` |
| Entry add trace | `"Adding UFT Entry\n..."` | `"Adding UDT Entry\n..."` |
| UUID trace | `"uft uuid = ..."` | `"udt uuid = ..."` |

The UDT requires alignment: the trace `"udt size %lld needs aligning\n"` fires when the section size is not properly aligned, and the linker adjusts it before finalization.

The companion section `.nv.udt.entry` contains per-entry records structured identically to `.nv.uft.entry` -- each with a 128-bit UUID, an offset within `.nv.udt`, and a symbol index.

Validation follows the same pattern: `"missing nv.udt.entry"` for a missing per-entry section, `"size of uidx window != nv.udt"` for UIDX size mismatch.

## `.nv.uidx` -- Index File

The `.nv.uidx` section provides a pre-defined ordering for UFT and UDT entries. It is loaded from an external file specified via the `--uidx-file` / `-uidx` CLI option (stored in `qword_2A5F208`). The global variable description string is `"Path to uidx file."`.

The UIDX file serves as a cross-compilation-unit stable ordering: when multiple compilation units are linked, the UIDX file ensures that the same function always occupies the same table slot across different link invocations. This is critical for:

- **Separate compilation with indirect calls**: If a host-side CUDA program serializes function handles to device memory, the handles must remain stable across re-linking.
- **ABI stability**: Virtual function tables in device code rely on stable slot assignments.

### UIDX File Format

The UIDX file format is parsed by `sub_463490` at `0x463490`. The file has a fixed-size header followed by two arrays of 24-byte UUID/offset records.

#### Magic Number

The first 8 bytes must be the little-endian value `0x58444E495446557F`, which decodes as the ASCII string `\x7fUFTINDX`.

#### Header Layout (48 bytes)

```
Offset  Size  Field             Description
------  ----  ---------------   -------------------------------------------
 0x00     8   magic             0x58444E495446557F ("\x7fUFTINDX")
 0x08     4   version           File format version; version > 1 enables
                                an additional conflict-UUID block
 0x0C     4   (padding)
 0x10     8   uft_window_size   Expected size of the .nv.uft section
 0x18     8   uft_count         Number of UFT entries (N_uft)
 0x20     8   udt_window_size   Expected size of the .nv.udt section
 0x28     8   udt_count         Number of UDT entries (N_udt)
```

#### Entry Arrays

Immediately after the 48-byte header, the file contains two contiguous arrays:

1. **UFT entries** (N_uft entries, each 24 bytes):

```
Offset  Size  Field
------  ----  -----------
 0x00     8   uuid_lo       Low 64 bits of the 128-bit UUID
 0x08     8   uuid_hi       High 64 bits of the 128-bit UUID
 0x10     8   offset        Target byte offset within the .nv.uft section
```

2. **UDT entries** (N_udt entries, each 24 bytes): Same format, specifying `.nv.udt` offsets.

If `version > 1`, an additional block of `16 * N_uft` bytes appears between the header and the UFT entries array, containing supplementary conflict-resolution UUIDs.

#### Total File Size Validation

The parser validates the expected file size as:

```
expected_size = 48                              // header
              + 24 * (N_uft + N_udt)           // entry arrays
              + (version > 1 ? 16 * N_uft : 0) // optional conflict block
```

If the actual file size does not match, the error `"malformed uidx input"` is raised.

### UIDX Processing Pipeline

`sub_463490` processes the UIDX file in the following steps:

```
sub_463490(ctx, uidx_data, file_size):
    // Step 1: Magic validation
    if (*(uint64_t*)uidx_data != 0x58444E495446557F):
        fatal("not uidx input")

    // Step 2: Size validation
    N_uft = *(uint64_t*)(uidx_data + 24)
    N_udt = *(uint64_t*)(uidx_data + 40)
    conflict_block = (*(uint32_t*)(uidx_data + 8) > 1) ? 16 * N_uft : 0
    if (file_size != conflict_block + 24 * (N_uft + N_udt) + 48):
        fatal("malformed uidx input")

    // Step 3: Verbose dump of UFT entries
    if (verbose & 0x10):
        fprintf(stderr, "uftNumEntries=%llx, udtNumEntries=%llx\n", N_uft, N_udt)
        ptr = uidx_data + 48
        for i = 0 to N_uft-1:
            fprintf(stderr, "uft uuid = <%016llx,%016llx>, offset = %llx\n",
                    ptr[0], ptr[1], ptr[2])
            ptr += 24

    // Step 4: Verbose dump of UDT entries
    ptr = uidx_data + 48 + 24 * N_uft
    for i = 0 to N_udt-1:
        if (verbose & 0x10):
            fprintf(stderr, "udt uuid = <%016llx,%016llx>, offset = %llx\n",
                    ptr[0], ptr[1], ptr[2])
        ptr += 24

    // Step 5: Store the parsed UIDX pointer in the context
    ctx->uidx = uidx_data                  // offset +656
    create_section(ctx, ".nv.uidx", uidx_data, /*is_alloc=*/1, file_size)
```

### Window Size Validation

During `sub_463F70`, the UIDX window sizes are cross-checked against the actual section sizes:

- `uidx->uft_window_size` must equal `.nv.uft` section's `sh_size`
- `uidx->udt_window_size` must equal `.nv.udt` section's `sh_size`
- An `"invalid window size"` error fires for zero or negative window sizes

The UDT window validation happens after the UDT reorder call, not before it (unlike UFT where validation precedes the reorder call). This ordering difference is visible in `sub_463F70` at lines 134-136 of the decompiled source.

If unified symbols are detected but no UIDX file was specified, the linker warns: `"Unified symbols found but index file not specified."`.

## UFT Relocation Processing

The relocation phase (`sub_469D60`) handles UFT-related relocations through a dedicated remapping path. Unified relocation types are a distinct family within both the CUDA and Mercury relocation type systems:

### CUDA Unified Relocation Types (102--113)

| Type | Name | Bit pattern |
|---|---|---|
| 102 | `R_CUDA_UNIFIED` | Full 64-bit value |
| 103 | `R_CUDA_UNIFIED_32` | 32-bit value |
| 104 | `R_CUDA_UNIFIED_8_0` | Bits [7:0] |
| 105 | `R_CUDA_UNIFIED_8_8` | Bits [15:8] |
| 106 | `R_CUDA_UNIFIED_8_16` | Bits [23:16] |
| 107 | `R_CUDA_UNIFIED_8_24` | Bits [31:24] |
| 108 | `R_CUDA_UNIFIED_8_32` | Bits [39:32] |
| 109 | `R_CUDA_UNIFIED_8_40` | Bits [47:40] |
| 110 | `R_CUDA_UNIFIED_8_48` | Bits [55:48] |
| 111 | `R_CUDA_UNIFIED_8_56` | Bits [63:56] |
| 112 | `R_CUDA_UNIFIED32_HI_32` | High 32 bits of 64-bit address |
| 113 | `R_CUDA_UNIFIED32_LO_32` | Low 32 bits of 64-bit address |

### Mercury Unified Relocation Types (0x10032--0x1003F)

| Type | Name |
|---|---|
| 0x10032 | `R_MERCURY_UNIFIED` |
| 0x10033 | `R_MERCURY_UNIFIED_32` |
| 0x10034 | `R_MERCURY_UNIFIED_8_0` |
| 0x10035 | `R_MERCURY_UNIFIED_8_8` |
| 0x10036 | `R_MERCURY_UNIFIED_8_16` |
| 0x10037 | `R_MERCURY_UNIFIED_8_24` |
| 0x10038 | `R_MERCURY_UNIFIED_8_32` |
| 0x10039 | `R_MERCURY_UNIFIED_8_40` |
| 0x1003A | `R_MERCURY_UNIFIED_8_48` |
| 0x1003B | `R_MERCURY_UNIFIED_8_56` |
| 0x1003E | `R_MERCURY_UNIFIED32_LO` |
| 0x1003F | `R_MERCURY_UNIFIED32_HI` |

### Remapping Algorithm

During the relocation phase (`sub_469D60`), unified relocation types are processed through a multi-step pipeline:

**Step 1: Section type check.** The relocation phase first checks whether the target section has `sh_type == 0x7000000E` (the `.nv.uft` type). If so, the relocation's target address is adjusted by calling `sub_463660` (the stub symbol matcher) to resolve the real function symbol, and the `r_offset` field is set to the function's final value within the reordered table.

For Mercury DPC mode 2, the offset undergoes additional scaling: `offset += (offset >> 7) * 2 * get_instruction_size()` to account for the interleaved padding bytes.

**Step 2: Type translation.** Each unified relocation type is remapped to its base (non-unified) equivalent via a switch-case. The trace `"replace unified reloc %d with %d"` fires for each remapping. The exact mappings from the decompiled switch at offset `0x46A720`:

| Unified type | Value | Base type | Value | Notes |
|---|---|---|---|---|
| `R_CUDA_UNIFIED` | 102 | `R_CUDA_ABS` | 2 | Full 64-bit absolute |
| `R_CUDA_UNIFIED_32` | 103 | `R_CUDA_32` | 1 | 32-bit absolute |
| `R_CUDA_UNIFIED_8_0` | 104 | `R_CUDA_8_0` | 76 | Bits [7:0] |
| `R_CUDA_UNIFIED_8_8` | 105 | `R_CUDA_8_8` | 77 | Bits [15:8] |
| `R_CUDA_UNIFIED_8_16` | 106 | `R_CUDA_8_16` | 78 | Bits [23:16] |
| `R_CUDA_UNIFIED_8_24` | 107 | `R_CUDA_8_24` | 79 | Bits [31:24] |
| `R_CUDA_UNIFIED_8_32` | 108 | `R_CUDA_8_32` | 80 | Bits [39:32] |
| `R_CUDA_UNIFIED_8_40` | 109 | `R_CUDA_8_40` | 81 | Bits [47:40] |
| `R_CUDA_UNIFIED_8_48` | 110 | (same bucket as 8_40) | 81 | Bits [55:48] |
| `R_CUDA_UNIFIED_8_56` | 111 | (same bucket as 8_40) | 81 | Bits [63:56] |
| `R_CUDA_UNIFIED32_HI_32` | 112 | (hi-32 base) | 81 | High 32 bits |
| `R_CUDA_UNIFIED32_LO_32` | 113 | (lo-32 base) | 81 | Low 32 bits |

Note: Types 106-113 all map to the same base value (81) in the decompiled switch. This is because the byte-extraction relocations share a common fixup routine that reads the full 64-bit value and extracts the appropriate byte slice based on the original type encoded in the relocation addend.

**Step 3: Synthetic symbol resolution.** After type remapping, if the relocation symbol is one of the eight synthetic unified symbols, the relocation is resolved to type 0 (no-op):

```
// Check if the relocation targets a UFT/UDT synthetic symbol
sym_name = reloc->symbol->name
if (sym_name matches any of: __UFT_OFFSET, __UFT_CANONICAL, __UDT_OFFSET,
                              __UDT_CANONICAL, __UDT, __UFT, __UFT_END, __UDT_END):
    if (reloc_type != 0):
        if (verbose & 0x4):
            fprintf(stderr, "replace unified reloc %d with %d\n", reloc_type, 0)
        reloc_type = 0
        reloc->r_info = 0        // zero out both type and symbol
        reloc->symbol = get_symbol(ctx, 0)   // null symbol
```

The check for `__UFT_OFFSET` is performed by a 13-character `memcmp` loop (visible in the decompiled code at offset `0x46A860`), followed by similar checks for each of the remaining seven names. The trace `"ignore reloc on UFT_OFFSET\n"` fires specifically when an `__UFT_OFFSET` relocation is encountered in the secondary pass (after the UIDX-based reorder has already resolved the actual constant-memory offset).

**Step 4: Mercury extended mapping.** For Mercury relocations (types >= 0x10000), a parallel remapping table handles `R_MERCURY_UNIFIED` variants. The Mercury types occupy the range 0x10032--0x1003F and are remapped to their base `R_MERCURY_*` equivalents through a separate dispatch table at `off_1D3CBE0`.

In relocatable link mode (`-r`), the unified relocation remapping converts all 12 (CUDA) or 14 (Mercury) unified types to their equivalent base types before writing the output `.rela` sections. This allows downstream link steps to process them as ordinary relocations.

## PTX-Level Stub Generation

The function `sub_12AF8A0` at `0x12AF8A0` in the ptxas subsystem generates UFT stubs at the PTX level when the linker needs to create a new indirect-call trampoline (e.g., during LTO relinking). The function constructs a PTX source string and feeds it to the embedded PTX compiler:

```
sub_12AF8A0(func_name, ptx_context):
    // Build a PTX module string containing the stub
    buf = create_buffer(128, ptx_context)
    buf_printf(buf, "\t.version %s\n", ptx_context->version_string)
    buf_printf(buf, "\t.target  %s\n", ptx_context->target_string)
    buf_printf(buf, ".func .attribute(.unified_func_stub)  __cuda_uf_stub_%s( ) {\n"
                    " _jcall %s; }", func_name, func_name)

    // Compile the PTX string as if it were the file "<uft-stub>"
    ptx_context->compilation_mode = 2     // UFT stub compilation mode
    compile_ptx("<uft-stub>", buf, 0, ptx_context, 0, 0, 0, 0, 0)
    ptx_context->compilation_mode = 0     // restore normal mode
```

The `"<uft-stub>"` filename marker is used in diagnostic messages to identify code originating from synthesized UFT stubs rather than user source code.

## Relocation Emission from ptxas

The function `sub_161C810` in ptxas generates the relocation type mapping from ptxas internal relocation codes to the `R_CUDA_*` / `R_MERCURY_*` type numbers written into the output ELF. The relevant unified relocation mappings are:

| ptxas internal code | Condition | Emitted R_CUDA/R_MERCURY type |
|---|---|---|
| 66 (non-Mercury, non-unified) | `!is_unified` | `R_CUDA_ABS_32_LO` (56) |
| 66 (non-Mercury, unified) | `is_unified` | `R_CUDA_UNIFIED32_LO_32` (112) |
| 67 (non-Mercury, non-unified) | `!is_unified` | `R_CUDA_ABS_32_HI` (57) |
| 67 (non-Mercury, unified) | `is_unified` | `R_CUDA_UNIFIED32_HI_32` (113) |
| 81 (Mercury, non-unified) | `!is_unified` | `R_MERCURY_ABS_32_LO` (65541) |
| 81 (Mercury, unified) | `is_unified` | `R_MERCURY_UNIFIED32_LO` (65598) |
| 82 (Mercury, non-unified) | `!is_unified` | `R_MERCURY_ABS_32_HI` (65542) |
| 82 (Mercury, unified) | `is_unified` | `R_MERCURY_UNIFIED32_HI` (65599) |
| 93 | (always) | `R_MERCURY_UNIFIED` (65586) |
| 94 | (always, non-ABI) | Targets `__UFT_OFFSET`; type 68 |
| 103 | (always, non-ABI) | Targets `__UFT_OFFSET`; type 114 |

When `!ctx->is_abi && ctx->is_indirect_calls` is true and the ptxas internal code is 94 or 103, the relocation target symbol is forced to `__UFT_OFFSET` (looked up by name via `sub_4411B0`).

## Function Address Tables

The compiler also emits two auxiliary symbols related to indirect function calls:

| Symbol | Memory space | Purpose |
|---|---|---|
| `__funcAddrTab_c` | Constant memory (`.nv.constant`) | Table of function addresses in constant memory, read by indirect call dispatch logic |
| `__funcAddrTab_g` | Global memory | Table of function addresses in global memory, used when constant memory is full |

These tables are set up by `sub_162C8B0` (`setup_constant_space`) alongside constant bank initialization. They represent the runtime-accessible side of the UFT: while `.nv.uft` is the linker's internal representation of the jump table, `__funcAddrTab_c` / `__funcAddrTab_g` are the symbols visible to the generated SASS code at runtime.

The attribute `EIATTR_STUB_FUNCTION_KIND` in `.nv.info` sections marks functions that serve as UFT stubs, enabling the linker to distinguish stub trampoline functions from ordinary device functions during dead code elimination and register count propagation.

## Entry Merge Process

When multiple input objects contain `.nv.uft.entry` sections, the linker must merge them into a single unified table. The merge process involves two distinct functions:

### Symbol Assignment in `sub_442820` (`elfw_merge_symbols`)

The function `sub_442820` at `0x442820` (5,371 bytes) processes each symbol during ELF merge. When it encounters a symbol with the `__cuda_uf_stub_` prefix, it takes a special path:

```
sub_442820(ctx, sym_name, bind_flags, sym_idx):
    // Check if this is a UFT stub symbol
    if ((bind_flags & 0x14) == 0 && starts_with(sym_name, "__cuda_uf_stub_")):
        // Determine section assignment
        if (ctx->e_type == ET_REL):                // e_type at offset +16
            // Create a .nv.uft.rel section named after the real function
            func_name = sym_name + 15              // skip "__cuda_uf_stub_" prefix
            rel_section_name = ".nv.uft.rel." + func_name
        else:
            if (ctx->uft_section_idx == 0):        // first UFT symbol
                section_name = ".nv.uft"           // assign to main table section
            else:
                goto assign_to_existing
            // Create .nv.uft section: sh_type=0x7000000E, sh_flags=6,
            //   sh_addralign=128, sh_entsize=128
            uft_section = create_section(ctx, section_name, 0x7000000E, 6,
                                         ctx->machine_arch, sym_idx & 0xFFFFFF,
                                         128, 128)
            ctx->uft_section_idx = uft_section

    assign_to_existing:
        // Assign the symbol to the UFT section
        ...

    // After assignment, if section type is 0x7000000E and e_type != ET_REL:
    if (ctx->uft_section_idx != 0 && ctx->e_type != 1):
        propagate_section_properties(ctx, ctx->uft_section_idx)
```

The key insight is that in relocatable objects (`ET_REL`), each stub function gets its own `.nv.uft.rel.<funcname>` section for per-function relocation tracking. In executable/shared objects, all stubs share a single `.nv.uft` section.

### Per-Object Entry Merge in `sub_45CF00`

The function `sub_45CF00` at `0x45CF00` processes the `.nv.uft.entry` section from each input object during the merge phase. See [Entry Processing Pipeline, Stage 2](#entry-processing-pipeline) above for the detailed algorithm.

### Symbol Index Patching in `sub_4633A0` and `sub_464400`

After all objects are merged, `sub_464400` at `0x464400` dispatches to `sub_4633A0` to patch symbol indices in both `.nv.uft.entry` and `.nv.udt.entry`. See [Entry Processing Pipeline, Stage 3](#entry-processing-pipeline) above.

### Reorder in `sub_4637B0`

The UUID hash map construction, slot assignment, and Mercury interleaving are described in the [Slot Assignment Algorithm](#slot-assignment-algorithm) section above.

## Relationship to `compute_entry_properties`

The function `sub_451D80` (`compute_entry_properties`, the largest function in the 0x400000--0x470000 region at ~98KB) interacts with the UFT system during the property computation phase:

1. It locates the `.nv.uft` section. If not found, it reports `"nv.uft not found"` and skips UFT processing.
2. For each kernel entry point, it checks whether the entry uses indirect calls by examining the callgraph for UFT stub symbols.
3. If indirect calls are present, it propagates register counts through the indirect callees (since the actual callee is not statically known, the linker must assume the worst-case register count among all possible targets in the UFT).
4. It validates that `entry_info` and `entry_sym` are non-null (fatals: `"entry_info was null"`, `"entry_sym was null"`).

This register propagation through the UFT is critical for correctness: if a kernel calls a function pointer, and the target function uses 64 registers, then the kernel's register allocation must account for those 64 registers even though no static call edge exists.

## Error Conditions

| Error message | Severity | Condition |
|---|---|---|
| `"missing nv.uft.entry"` | Fatal | `.nv.uft` exists but `.nv.uft.entry` does not |
| `"missing nv.udt.entry"` | Fatal | `.nv.udt` exists but `.nv.udt.entry` does not |
| `"duplicate ids in uft.entry"` | Fatal | Two entries have the same 128-bit UUID |
| `"Number of .nv.uft jump slots != Number of entries in .nv.uft.entry"` | Fatal | Slot count / entry count mismatch |
| `"size of uidx window != nv.uft"` | Fatal | UIDX file window size does not match `.nv.uft` section size |
| `"size of uidx window != nv.udt"` | Fatal | UIDX file window size does not match `.nv.udt` section size |
| `"uft map conflict: 0x%llx"` | Error | Two UUID entries hash to the same key |
| `"matching uuid not found"` | Error | UUID from UIDX file not found in merged entries |
| `"UFT stub match not found"` | Fatal | No function symbol found matching a UFT stub |
| `"entry was already found?"` | Fatal | Entry's bit-31 flag already clear during reorder (duplicate slot assignment) |
| `"nv.uft not found"` | Warning | Expected `.nv.uft` section not present (non-fatal, skips UFT processing) |
| `"malformed uidx input"` | Fatal | UIDX file size does not match `48 + 24*(N_uft+N_udt) [+ 16*N_uft]` |
| `"not uidx input"` | Fatal | First 8 bytes are not `\x7fUFTINDX` (magic `0x58444E495446557F`) |
| `"invalid window size"` | Fatal | UIDX window size is zero or negative |
| `"Unified symbols found but index file not specified."` | Warning | Unified symbols present but no `--uidx-file` given |
| `"Indirect function call requires ABI"` | Fatal | Compilation unit uses indirect calls but was not compiled with ABI enabled |

## Function Map

| Address | Size | Identity | Role |
|---|---|---|---|
| `0x442820` | 5,371 B | `elfw_merge_symbols` | Merges symbols; creates `.nv.uft` / `.nv.uft.rel.<func>` sections for stubs |
| `0x444A20` | ~120 B | `is_unified_synthetic_symbol` | Returns true if name matches any of the 8 UFT/UDT synthetic symbols |
| `0x4633A0` | ~500 B | `patch_entry_symidx` | Iterates `.nv.uft.entry` / `.nv.udt.entry` records, patches virtual symidx to real |
| `0x463490` | ~400 B | `uidx_load_and_parse` | Loads and parses the UIDX index file; validates magic/size/entries |
| `0x463660` | ~400 B | `uft_find_matching_symbol` | Matches stub symbol to real function: name lookup then linear scan fallback |
| `0x4637B0` | 10,141 B | `uft_reorder_entries` | Builds UUID hash map, reorders UFT/UDT entries by UIDX, interleaves for Mercury |
| `0x463F70` | 3,978 B | `uft_setup_sections` | Creates/validates `.nv.uft`, `.nv.udt`, `.nv.uft.entry`, `.nv.udt.entry` |
| `0x464240` | ~200 B | `uft_add_entry` | Appends a new 32-byte entry record to `.nv.uft.entry` |
| `0x464400` | ~100 B | `uft_udt_patch_dispatch` | Dispatches `patch_entry_symidx` for both `.nv.uft.entry` and `.nv.udt.entry` |
| `0x469230` | ~600 B | `create_relocation_section` | Creates `.rela`/`.rel` sections; handles `.nv.uft.rel` name redirect for `ET_REL` |
| `0x451D80` | 97,969 B | `compute_entry_properties` | Validates UFT, propagates register counts through indirect callees |
| `0x45CF00` | ~500 B | `merge_uft_entries_from_obj` | Processes `.nv.uft.entry` from one input object during merge phase |
| `0x469D60` | 26,578 B | `apply_relocations` | Processes unified relocations, remaps types 102--113 / 0x10032--0x1003F |
| `0x12AF8A0` | ~200 B | `generate_ptx_uft_stub` | Synthesizes PTX stub source `.func .attribute(.unified_func_stub) ...` and compiles it |
| `0x14075D0` | 13,679 B | `ptx_emit_function_body` | Emits `.nv.uft.rel` sections during code generation |
| `0x161C810` | ~1,200 B | `ptxas_reloc_type_map` | Maps ptxas internal reloc codes to `R_CUDA_*` / `R_MERCURY_*` type numbers |
| `0x162C8B0` | (varies) | `setup_constant_space` | Creates `__funcAddrTab_c`/`__funcAddrTab_g` symbols |
| `0x162E070` | ~800 B | `setup_reserved_smem_and_texref` | Creates `__UFT_OFFSET`, `__UDT_OFFSET`, and other synthetic symbols on demand |

## Cross-References

- [Layout Phase](../pipeline/layout.md) -- Phase 10 invokes UFT setup
- [Relocation Phase](../pipeline/relocate.md) -- Unified relocation remapping
- [R_CUDA Relocations](../linker/r-cuda-relocations.md) -- Full unified type catalog
- [R_MERCURY Relocations](../mercury/r-mercury-relocations.md) -- Mercury unified type counterparts
- [Section Merging](../linker/section-merging.md) -- Merge of `.nv.uft.entry` sections
- [Bindless Relocations](../linker/bindless-relocations.md) -- Analogous system for texture/surface descriptors
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- Interaction with UFT stub reachability
- [CLI Options](../pipeline/cli-options.md) -- `--uidx-file` option documentation
