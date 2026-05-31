# Layout Phase

The layout phase (`sub_439830`, 65,776 bytes at `0x439830`) is the single largest phase in the linker core after merge. It assigns addresses to every data item across all NVIDIA GPU memory spaces -- global memory, shared memory (global, local, extern, and reserved), and constant banks -- merges overlapping data, deduplicates constants, resolves extern shared memory fixups, and lays out per-kernel constant sections. The function is called from `main()` immediately after the merge phase completes and before relocation application.

Despite the sweep identifying this function as "shared\_memory\_layout," the decompiled code reveals it is actually the **unified layout engine** for the entire device ELF: it handles global data, all four shared memory categories, constant bank layout, constant deduplication, OCG (object-code-generator) constant optimization, bindless resource allocation, reserved shared memory symbols, UFT section setup, and texture/sampler/surface resource counting. The name in the binary's debug trace is simply `"layout"`.

## Position in the Pipeline

```text
Merge Phase (sub_45E7D0, per-object)
  |
  v
Layout Phase (sub_439830)         <-- this page
  |
  +-- Global data merging (.nv.global)
  +-- Shared memory allocation (global, local, extern, reserved)
  +-- Constant bank layout & dedup
  +-- Bindless resource processing
  +-- UFT section setup
  |
  v
Relocation Phase (sub_469D60)
```

The function receives a single argument: the elfw (ELF wrapper) object that holds the merged output state. It returns after all addresses have been assigned and all layout optimizations have been applied.

## Function Signature

```c
// sub_439830 -- unified layout engine
// Address: 0x439830
// Size: 65,776 bytes (~2,173 lines of decompiled pseudocode)
// ~400 local variables
//
// a1: elfw* -- the output ELF wrapper object
// Returns: result of last operation (discarded by caller)
size_t __fastcall sub_439830(__int64 a1);
```

Key fields accessed on the elfw object `a1`:

| Offset | Type | Description |
|---|---|---|
| `+7` | `byte` | Architecture class (65 = `'A'` for a-variant) |
| `+16` | `int16` | Link mode (1 = relocatable, 0xFF00 = special) |
| `+48` | `dword` | Architecture flags bitmask |
| `+64` | `byte` | Debug/verbose flags (bit 1 = verbose layout trace) |
| `+80` | `byte` | EWP (early-write-pass) mode flag |
| `+81` | `byte` | Skip-global-init flag |
| `+83` | `byte` | Optimize-data-layout flag |
| `+85` | `byte` | No-shared-layout flag |
| `+87` | `byte` | Reserve-null-pointer flag |
| `+90` | `byte` | No-opt flag |
| `+91` | `byte` | Force OCG constant optimization flag |
| `+97` | `byte` | Merge-constants flag |
| `+100` | `byte` | Extended shared memory mode |
| `+216` | `dword` | Section index: `.nv.global.init` |
| `+220` | `dword` | Section index: secondary global init |
| `+228` | `dword` | Section index: `.nv.shared` (global shared) |
| `+248` | `dword` | Section index: `.nv.reservedSmem` |
| `+256` | `list*` | Per-entry shared memory section list |
| `+264` | `list*` | Extern shared memory section list |
| `+272` | `list*` | Per-entry constant bank section list |
| `+280` | `list*` | Per-entry local data section list |
| `+344` | `vec*` | Positive symbol array (symbols with index >= 0) |
| `+352` | `vec*` | Negative symbol array (symbols with index < 0) |
| `+360` | `vec*` | Section array (all section records) |
| `+376` | `list*` | Relocation record list |
| `+424` | `list*` | Texture descriptor list |
| `+432` | `list*` | Sampler descriptor list |
| `+440` | `list*` | Surface descriptor list |
| `+448` | `list*` | Global variable pending-merge list |
| `+488` | `vtable*` | Architecture profile vtable pointer |
| `+504` | `dword` | Syscall-const-offset value |
| `+512` | `vec*` | EWP function metadata vector |
| `+568` | `dword` | Specific constant section index |
| `+576` | `htab*` | Overlapped-constant dedup hash table |

## High-Level Execution Flow

The function proceeds through ten sequential phases. Each phase handles one memory space or optimization pass. The order is critical because later phases depend on addresses assigned by earlier ones.

### Phase 1: Global Data Merge (`.nv.global`)

```c
if (elfw->pending_globals != NULL):
    section = find_or_create_section(".nv.global", SHT_CUDA_GLOBAL=0x70000007)
    walk symbol linked list at section+72 to find tail
    for each pending global in elfw+448:
        move symbol into .nv.global section (sub_440430)
        copy data into section buffer (sub_433760)
        advance to next
```

The function `sub_433760` is the core data-copy primitive. It allocates a 40-byte data node, computes the aligned offset (`current_size` rounded up to the symbol's alignment), stores the data pointer and size, and appends the node to the section's symbol linked list. The section's total size (`section+32`) is updated to `aligned_offset + data_size`.

If `.nv.global` does not yet exist, it is created via `sub_441AC0` with section type `SHT_CUDA_GLOBAL` (0x70000007), flags 3 (ALLOC|WRITE), and alignment 1.

### Phase 2: Bindless Resource Processing

If the architecture supports bindless resources (checked via the vtable at `elfw+488`, offset 296), the layout phase processes texture, sampler, and surface descriptors:

1. **Specific constant section**: If `elfw+568` has a section index, call `sub_4324B0` to set up that section as the bindless data target.
2. **Fallback scan**: Otherwise, iterate all symbols looking for entry-point functions (symbol type check: bit 4 set and binding != LOCAL), and call `sub_4324B0` on each matching section.
3. **Descriptor list layout**: Call `sub_433310` three times to lay out the texture list (`elfw+424`), sampler list (`elfw+432`), and surface list (`elfw+440`).
4. **Per-entry bindless section processing**: For each entry in the constant bank list (`elfw+272`), if the section type matches the bindless constant bank type (queried via vtable offset 304 or 312), call `sub_438DD0` to process bindless relocations. This function creates synthetic `$NVLINKBINDLESSOFF_<name>` symbols and rewrites relocation targets.

This phase is skipped entirely in relocatable link mode (`link_mode == 1`).

### Phase 3: Global Init Section Layout

```c
if (elfw+216 has a section index):
    section = get_section_header(elfw, index)
    sub_4325A0(elfw, section, 0)    // lay out at offset 0

if (elfw+220 has a section index):
    section = get_section_header(elfw, index)
    sub_4325A0(elfw, section, 0)    // lay out at offset 0
```

The function `sub_4325A0` is the **general-purpose section layout engine**. It sorts the section's symbol list (via `sub_4647D0` with comparator `sub_432440`), then iterates symbols computing aligned offsets:

```c
for each symbol in section's symbol list:
    if symbol has explicit alignment:
        offset = roundup(current_offset, alignment)
    else if symbol has a size:
        align = min(size, 8)
        offset = roundup(current_offset, align)
    else:
        assert(no_opt_mode)  // "should only reach here with no opt"
    symbol.value = offset
    section_record.value = offset
    current_offset = offset + symbol.size
    // verbose: "variable %s at offset %d"
```

### Phase 4: Global Shared Memory Layout

This is the most complex shared memory phase. It handles `.nv.shared` and related per-entry shared memory sections.

**Step 4a: Build overlap sets.** Create a set container (via `sub_465020`), then scan the relocation list (`elfw+376`) for relocations that reference entry-point function sections. For each such relocation, add its function ID to the overlap set via `sub_4644C0`.

**Step 4b: Lay out global shared.** If there is a global shared section (`elfw+228`):

- **Relocatable mode** (`link_mode == 1`): Walk the section's symbol list directly, assigning offsets from their pre-existing positions. For each symbol, set `section_record.value = symbol.offset` and track the maximum offset as the section's running size.

- **No-opt mode** (`elfw+90` set): Call `sub_4325A0` directly for a simple linear layout.

- **Optimized mode** (default): Call `sub_436BD0` (the shared memory optimizer, 15,711 bytes). This function builds an **interference graph** between shared memory variables based on which kernel entry points reference them. Variables that are never live simultaneously in any kernel can share the same address, reducing total shared memory consumption. The optimizer prints `"global shared %s only used in entry %d"` and `"remove unused global shared %s"` during verbose operation.

**Step 4c: Per-entry shared memory layout.** Iterate the per-entry shared section list (`elfw+256`). For each entry:

1. Determine which entry function the shared section belongs to (via `sub_440590` to get the section record, then extract the function index).
2. Check if the entry function reaches global shared memory using the overlap set (`sub_4655C0`). If not, the entry's shared offset starts fresh rather than after the global shared region.
3. If the entry reaches global shared, its shared sections start after the global shared region's high-water mark.
4. In relocatable mode: walk symbols directly assigning pre-existing offsets.
5. Otherwise: if the entry is not from an EWP (early-write-pass) function, call `sub_4325A0` to lay out the section.
6. Track the maximum shared memory address across all entries.

Verbose output includes `"esh %s has offset %d"`, `"shared entry %s:"`, `"entry %s does not reach global shared"`, and `"shared entry %s from EWP ignored"`.

### Phase 5: Extern Shared Memory Fixups

Extern shared variables (`__shared__ extern`) have no static size -- their offset is determined at link time based on the total non-extern shared memory consumed. This phase resolves those addresses.

There are two distinct code paths:

**Path A: No-opt mode** (`elfw+90` set). All extern shared variables are placed at the same offset (after global shared). The maximum alignment across all extern shared sections (minimum 16 bytes) is computed, the base offset is aligned accordingly, and every extern shared symbol receives that aligned offset. The per-entry shared sections also get their base offset updated.

**Path B: Optimized mode** (default, and the larger code path). This handles per-entry extern shared placement with overlap analysis:

1. **Count entries**: Call `sub_464740` to count the number of extern shared entries.
2. **Allocate per-entry overlap sets**: Create one overlap set per extern shared entry.
3. **Populate sets**: Scan the relocation list. For each relocation that targets an extern shared section with zero data (i.e., truly extern), identify which entry function it belongs to and add a mapping from the extern shared symbol to each reachable entry's set.
4. **Enforce minimum alignment**: For every extern shared section record, if alignment is less than 16, force it to 16.
5. **Iterative placement loop** (`do { ... } while (v422)`):
   - For each extern shared section, scan its overlap set to find the maximum offset of any overlapping shared region.
   - Compute the extern shared offset: `max_overlap + 16 - (max_overlap % 16)`, with special handling for the reserve-null-pointer flag.
   - If the computed offset changed from the previous iteration, set a "changed" flag and iterate again.
   - This fixed-point iteration converges because offsets can only grow.
   - Verbose: `"extern shared variable %s at offset %lld"`.
6. **Overlapping set merging**: If the EWP flag (`elfw+80`) is set, additional logic detects and merges overlapping extern shared sets. It prints `"overlapping sets %d and %d"` and rewrites relocation targets. The format `"reloc of extern shared %d replaced with symbol %d"` appears in the verbose trace.
7. **Final extern layout**: For each merged set, iterate its overlap set members, query their shared section sizes via `sub_436A80`, and assign final offsets. Verbose: `"extern shared instance %d at offset %lld"`.

### Phase 6: Shared Memory Cleanup and Reserved Shared

After the extern shared pass, if neither EWP nor relocatable mode is active and the `no-shared-layout` flag is clear, the global shared section is cleaned up: its data nodes are freed, the symbol list is cleared, and the section index at `elfw+228` is zeroed.

**Reserved shared memory** (`elfw+248`): Three special symbols are looked up and assigned architecture-specific values from the profile vtable:

| Symbol name | Vtable offset | Meaning |
|---|---|---|
| `.nv.reservedSmem.begin` | +600 | Start address of GPU-reserved shared memory |
| `.nv.reservedSmem.cap` | +584 | Capacity of reserved shared region |
| `.nv.reservedSmem.offset0` | +608 | Base offset for reserved shared |

If the architecture is not an a-variant (i.e., `elfw+7 != 'A'`), and reserved shared sections exist, the reserved shared symbols are offset by the `reservedSmem.offset0` value. Verbose: `"shared variable %s updated offset to %lld"`.

### Phase 7: Local Data Section Layout

```c
for each entry in per-entry local data list (elfw+280):
    // verbose: "local entry %s:"
    section = get_section_header(elfw, entry_index)
    sub_4325A0(elfw, section, 0)
```

Each per-entry local data section is laid out independently starting at offset 0 using the standard layout engine.

### Phase 8: Overlapped Constant Replacement

If the overlapped-constant hash table (`elfw+576`) is populated (from the merge phase's constant dedup analysis), this phase rewrites relocations that target constants which were identified as overlapping with larger constants:

```c
for each relocation in elfw+376:
    dup_sym = lookup(overlap_table, reloc.symbol)
    if found:
        offset_delta = original_sym.value - dup_sym.value
        rewrite reloc target to dup_sym
        if offset_delta < 0:
            fatal("overlapped offset < full offset?")
        if offset_delta > 0:
            reloc.addend += offset_delta
            // also rewrite rela section index if needed
        // verbose: "replace overlapped constant %d with %d, offset %d"
```

### Phase 9: Per-Entry Constant Bank Layout

This phase handles the CUDA constant memory banks (`.nv.constant0`, `.nv.constant2`, etc.) that are split per kernel entry point. It contains four sub-paths:

**Sub-path 9a: OCG (object-code-generator) constant sections.** For each entry in the per-entry constant list (`elfw+272`), if the section type matches the OCG constant bank type (vtable offset 136), the section is laid out per entry function:

1. Walk the callgraph edges (`sub_44C740`) to find which entry functions reference this constant section.
2. For each reachable entry, create a per-entry constant section named `"<bank_type>.<entry_name>"` (e.g., `".nv.constant0.my_kernel"`).
3. Copy the constant data into the per-entry section using `sub_432B10` (overlapping data merge).
4. Track the maximum constant section size across all entries.
5. Align the per-entry copies to the maximum alignment.
6. Copy relocations that reference the original OCG section into the per-entry copies, adjusting offsets.
7. Verbose: `"OCG constant section %d reaches entry %d(%s)"`, `"need new ocg section %s"`, `"new ocg %s offset = %lld in %d"`, `"reset ocg constant reloc offset from %lld to %lld"`.
8. After creating all per-entry copies, the original shared OCG section's symbol list is freed and its data zeroed.

**Sub-path 9b: Non-OCG constant sections.** If the constant bank type matches the standard constant type (vtable offset 144 -- typically `.nv.constant0`), the section is laid out using `sub_4325A0`, with an optional `syscall-const-offset` value from `elfw+504`. Verbose: `"constant entry %s:"`.

**Sub-path 9c: Constant section merging.** If the used-set filter is active (`elfw+97` set by `--kernels-used` / `--variables-used`) and `elfw+80` (debug_flag) is clear, the function creates a `TEMP_MERGED_CONSTANTS` temporary section, calls `sub_4339A0` (the constant deduplication engine, 13,199 bytes) to merge all constant data into it, then replaces the original section's contents. This function identifies duplicate 32-bit and 64-bit values and aliases symbols. Verbose: `"layout and merge section %s"`, `"found duplicate value 0x%x, alias %s to %s"`.

**Sub-path 9d: OCG constant optimization.** If any OCG constant section exceeds the architecture's constant bank size limit (vtable offset 32), or if the `force-ocg-optimization` flag is set, the function enters the OCG constant optimization path:

1. Create `TEMP_OCG_CONSTANTS` temporary section.
2. Build a per-entry set of referenced constants.
3. For each OCG constant section with data, call `sub_4339A0` to deduplicate.
4. If the optimized size is within the bank size limit: replace all OCG constant section contents with the merged version. Copy the merged symbol list to each per-entry section.
5. If the optimized size still exceeds the limit: discard the optimization attempt. Verbose: `"ocg const optimization didn't help so give up"`.
6. Verbose: `"optimize OCG constants for %s, old size = %lld"`, `"new OCG constant size = %lld"`.

### Phase 10: Resource Counting and UFT Setup

**Resource counting.** If the architecture does not support bindless resources (vtable offset 296 returns false), allocate a counting structure (`sub_432740`) and call `sub_432870` three times to count textures, samplers, and surfaces against their respective architecture limits (vtable offsets 40, 48, 56).

**UFT section setup.** Call `sub_463F70` to create and validate the Unified Function Table sections (`.nv.uft`, `.nv.udt`, `.nv.uft.entry`, `.nv.udt.entry`). This function validates that the UFT entry window size matches the UFT section size and aligns the UDT section as needed.

This phase is skipped entirely in relocatable link mode.

## Key Sub-Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `0x4325A0` | `section_layout_engine` | ~1.4 KB | Sort symbols by alignment, assign offsets with padding |
| `0x433760` | `section_data_copy` | ~600 B | Allocate 40-byte data node, compute aligned offset, append to section |
| `0x436BD0` | `shared_memory_optimizer` | 15,711 B | Build interference graph, group non-overlapping shared vars |
| `0x432B10` | `merge_overlapping_global` | 11,683 B | Validate and merge overlapping data in `.nv.global` |
| `0x4339A0` | `constant_dedup` | 13,199 B | Find duplicate 32/64-bit constants, alias symbols |
| `0x438DD0` | `process_bindless_refs` | 12,779 B | Handle bindless texture/surface relocations |
| `0x4376D0` | `extern_shared_set_builder` | ~2 KB | Build overlap sets for extern shared placement |
| `0x436A80` | `extern_shared_section_lookup` | ~1 KB | Find per-entry shared section for a given entry function |
| `0x44DB00` | `global_data_fixup` | ~2 KB | Pre-layout global data initialization |
| `0x433310` | `descriptor_list_layout` | ~1 KB | Lay out texture/sampler/surface descriptor lists |
| `0x4324B0` | `bindless_target_setup` | ~1 KB | Set up constant section as bindless data target |
| `0x463F70` | `uft_setup_sections` | 3,978 B | Create/validate `.nv.uft`/`.nv.udt` sections |
| `0x432740` | `resource_counter_create` | ~1 KB | Allocate resource counting structure |
| `0x432870` | `resource_counter_check` | ~2 KB | Count resources against architecture limits |

## The Section Layout Engine (`sub_4325A0`)

This is the workhorse function called repeatedly throughout the layout phase. It implements a sorted linear allocator:

```c
sub_4325A0(elfw, section, initial_offset):
    assert(section != NULL, "section not found")

    if not extended_smem_mode or not arch_supports(section.type):
        sort section.symbol_list by alignment (sub_4647D0)

    current = initial_offset
    for each symbol in section.symbol_list:
        sym_record = get_sym_record(elfw, symbol.sym_index)
        alignment = symbol.alignment   // from symbol.offset+16

        if alignment > 0:
            current = roundup(current, alignment)
        elif symbol.size > 0:
            natural_align = min(symbol.size, 8)
            current = roundup(current, natural_align)
        else:
            assert(no_opt_mode)

        sym_record.value = current
        symbol.value = current
        // verbose: "variable %s at offset %d"
        current += symbol.size

    section.total_size = current
    return current
```

## The Data Copy Primitive (`sub_433760`)

This function appends a data contribution to a section:

```c
sub_433760(elfw, section_index, source_sym, alignment, data_ptr):
    section = get_section_header(elfw, section_index)
    if section is NULL: return

    if alignment > section.alignment:
        section.alignment = alignment

    node = arena_alloc(40)    // 40-byte data node
    node.source_sym = source_sym
    node.data_ptr = 0
    node.alignment = alignment
    node.data = data_ptr
    node.size = 0

    // compute aligned offset within section
    current_size = section.size
    remainder = current_size % alignment
    if remainder:
        current_size = current_size + alignment - remainder

    node.offset = current_size
    section.size = current_size + data_size

    // append to section's linked list (tail pointer at section+80)
    if section.symbol_list is empty:
        prepend(node, section.symbol_list)
        section.tail = section.symbol_list
    else:
        assert(section.tail != NULL, "tail data node not found")
        append_after(node, section.tail)
        section.tail = node
```

## Shared Memory Optimization (`sub_436BD0`)

The shared memory optimizer is a graph-coloring-based algorithm that reduces total shared memory consumption by aliasing non-overlapping variables. Two shared memory variables can share the same address if no kernel entry point uses both simultaneously.

The algorithm:

1. For each shared variable, determine which entry functions reference it (via callgraph traversal).
2. Build an interference graph: two variables interfere if any entry references both.
3. Variables that are only referenced by a single entry are candidates for entry-local placement. Verbose: `"global shared %s only used in entry %d"`.
4. Unreferenced variables are removed entirely. Verbose: `"remove unused global shared %s"`.
5. Non-interfering variables are grouped, and the group's address is the maximum of the individual requirements. Verbose: `"allocate to group %d"`.

## Constant Deduplication (`sub_4339A0`)

The constant deduplication engine finds identical constant values and merges them:

1. Create a temporary section `TEMP_MERGED_CONSTANTS` or `TEMP_USER_DATA`.
2. Iterate all symbols in the constant section.
3. For each symbol, check if an identical value already exists in the dedup hash table (`sub_448E70`).
4. If a duplicate 32-bit value is found: alias the symbol. Verbose: `"found duplicate value 0x%x, alias %s to %s"`.
5. If a duplicate 64-bit value is found: alias the symbol. Verbose: `"found duplicate 64bit value 0x%llx, alias %s to %s"`.
6. Unused constants (determined by `sub_43FB70` reachability check) are marked as dead: `sym.binding = LOCAL`. Verbose: `"remove unused constant %s"`.
7. After dedup, the original section's symbol list is replaced with the merged version.
8. Relocations targeting the original section are rewritten to point into the merged section. Verbose: `"change reloc in section %d, offset from %lld to %lld"`.

## Verbose Trace Strings

When the verbose layout flag is set (`elfw+64, bit 1`), the function emits extensive trace output to stderr. These strings are the primary evidence for understanding the layout logic:

| String | Phase | Meaning |
|---|---|---|
| `"global shared:"` | 4b | Header before global shared layout |
| `"esh %s has offset %d"` | 4c | Per-entry shared variable with pre-existing offset |
| `"entry %s does not reach global shared"` | 4c | Entry function not in global shared overlap set |
| `"shared entry %s:"` | 4c | Header before per-entry shared layout |
| `"shared entry %s from EWP ignored"` | 4c | Skip EWP entry's shared section |
| `"extern shared"` | 5 | Header before extern shared processing |
| `"extern shared variable %s at offset %lld"` | 5b | Extern shared address assigned |
| `"overlapping sets %d and %d"` | 5b | Two extern shared sets merged |
| `"reloc of extern shared %d replaced with symbol %d"` | 5b | Relocation rewrite for extern shared |
| `"extern shared instance %d at offset %lld"` | 5b | Final extern shared instance placement |
| `"global reserved shared:"` | 6 | Header before reserved shared layout |
| `"shared variable %s updated offset to %lld"` | 6 | Reserved shared offset adjustment |
| `"local entry %s:"` | 7 | Header before per-entry local layout |
| `"replace overlapped constant %d with %d, offset %d"` | 8 | Constant overlap rewrite |
| `"ocg const bank %s, size=%lld:"` | 9a | OCG constant section header |
| `"OCG constant section %d reaches entry %d(%s)"` | 9a | OCG section reachability |
| `"need new ocg section %s"` | 9a | Creating per-entry OCG section |
| `"new ocg %s offset = %lld in %d"` | 9a | Per-entry OCG offset assignment |
| `"reset ocg constant reloc offset from %lld to %lld"` | 9a | OCG relocation rewrite |
| `"constant entry %s:"` | 9b | Non-OCG constant layout header |
| `"layout and merge section %s"` | 9c | Constant merge begin |
| `"optimize space in %s (%d)"` | 9d | Constant dedup begin |
| `"local constant %s at offset %lld"` | 9d | Local constant placement |
| `"constant %s at offset %lld"` | 9d | Global constant placement |
| `"remove unused constant %s"` | 9d | Dead constant elimination |
| `"no symbol for reloc section %d at offset %lld?"` | 9d | Relocation target lookup failure |
| `"change reloc in section %d, offset from %lld to %lld"` | 9d | Relocation offset rewrite |
| `"optimize OCG constants for %s, old size = %lld"` | 9d | OCG optimization begin |
| `"new OCG constant size = %lld"` | 9d | OCG optimization result |
| `"ocg const optimization didn't help so give up"` | 9d | OCG optimization abandoned |

## Relocatable Link Mode

When `link_mode == 1` (relocatable link, `-r` flag), the layout phase operates in a simplified mode:

- Global shared layout directly copies pre-existing offsets from symbols rather than computing new ones.
- Per-entry shared sections are laid out the same way.
- Reserved shared sections copy offsets directly.
- The extern shared optimization path is entirely skipped.
- OCG constant optimization is skipped.
- Resource counting and UFT setup are skipped.
- Constant deduplication is skipped.

This mode preserves the address assignments from the individual input objects so the output can be linked again later.

## Architecture-Dependent Behavior

The layout phase queries the architecture profile vtable (`elfw+488`) at numerous points:

| Vtable offset | Query | Effect on layout |
|---|---|---|
| +32 | `max_constant_bank_size()` | Triggers OCG constant optimization when exceeded |
| +40 | `max_textures()` | Texture resource count limit |
| +48 | `max_samplers()` | Sampler resource count limit |
| +56 | `max_surfaces()` | Surface resource count limit |
| +136 | `ocg_constant_type()` | Section type for OCG constant banks |
| +144 | `standard_constant_type()` | Section type for `.nv.constant0` |
| +192 | `constant_section_name_id()` | Generates constant section name string |
| +200 | `supports_extended_smem()` | Controls extended shared memory layout |
| +208 | `is_constant_type()` | Validates section type is a constant bank |
| +296 | `supports_bindless()` | Enables/disables bindless resource processing |
| +304 | `bindless_texture_type()` | Section type for bindless texture bank |
| +312 | `bindless_surface_type()` | Section type for bindless surface bank |
| +584 | `reserved_smem_capacity()` | Value for `.nv.reservedSmem.cap` |
| +600 | `reserved_smem_begin()` | Value for `.nv.reservedSmem.begin` |
| +608 | `reserved_smem_offset0()` | Value for `.nv.reservedSmem.offset0` |

The a-variant architectures (sm\_90a, sm\_100a, sm\_103a) are detected via `elfw+7 == 'A'` and gate certain constant-bank and reserved-shared behaviors.

## Data Flow Summary

```text
Input state (after merge):
  .nv.global           -- merged global data, possibly with pending entries
  .nv.shared.*         -- per-entry shared memory sections, sizes known
  extern shared        -- zero-size placeholders awaiting address assignment
  .nv.constant*        -- constant bank data, possibly with duplicates
  .nv.reservedSmem.*   -- symbols awaiting architecture-specific values
  relocations          -- unresolved references to shared/constant/global

Output state (after layout):
  .nv.global           -- all globals merged, overlaps validated
  .nv.shared.*         -- all shared vars assigned addresses, optimized
  extern shared        -- addresses resolved to post-non-extern offsets
  .nv.constant*        -- deduped, per-entry copies created, unused removed
  .nv.reservedSmem.*   -- filled with arch-specific values
  .nv.uft/.nv.udt     -- UFT sections created and validated
  relocations          -- targets rewritten for constant overlap/merge
```

## Worked Example: Section Layout Computation

This example traces a single invocation of `sub_439830` on a small merged ELF with two kernels and demonstrates exactly how each byte offset is produced. All arithmetic matches the decompiled control flow of `sub_4325A0` (section layout engine) and `sub_433760` (data-copy primitive).

### Setup: Input Merged ELF

After the merge phase (`sub_45E7D0`), the `elfw` object holds the following sections. Section sizes are the totals accumulated during merge; the per-symbol offsets *within* each section are what the layout phase is about to compute or re-compute.

| Section | Type | Size | Align | Flags | Purpose |
|---|---|---|---|---|---|
| `.text.kernel_a` | `SHT_PROGBITS` | 2048 B | 128 | AX | SASS for `kernel_a` |
| `.text.kernel_b` | `SHT_PROGBITS` | 1024 B | 128 | AX | SASS for `kernel_b` |
| `.nv.constant0.kernel_a` | `SHT_PROGBITS` (cbank0) | 256 B | 4 | A | Driver param bank (kernel_a) |
| `.nv.constant0.kernel_b` | `SHT_PROGBITS` (cbank0) | 128 B | 4 | A | Driver param bank (kernel_b) |
| `.nv.constant2` | `SHT_PROGBITS` (cbank2) | 4096 B | 16 | A | User const bank, shared between both kernels |
| `.nv.shared.kernel_a` | `SHT_NOBITS` (smem) | 128 B | 16 | AW | Per-entry shared for `kernel_a` |
| `.nv.shared` | `SHT_NOBITS` (smem) | -- | 8 | AW | Two globals: `g_tmp` (64 B, align 8) and `g_hist` (96 B, align 16) |

Both kernels contain `R_CUDA_ABS32_LO_20` relocations into `.nv.shared` (global shared variables `g_tmp` and `g_hist`) and `R_CUDA_CONST_FIELD` relocations into `.nv.constant2`. `.nv.constant0.kernel_a` contains three symbols and `.nv.constant0.kernel_b` contains two, both split out during merge (Phase 9a's OCG path).

The relevant `elfw` field state at the start of layout:

```text
elfw+90  (no_opt_flag)     = 0     -- optimizer enabled
elfw+80  (EWP flag)        = 0     -- no early-write-pass
elfw+16  (link_mode)       = 0     -- final link, not -r
elfw+228 (.nv.shared idx)  = 14    -- global shared exists
elfw+256 (per-entry smem)  = list{.nv.shared.kernel_a}
elfw+272 (per-entry cbank) = list{.nv.constant0.kernel_a, .nv.constant0.kernel_b, .nv.constant2}
```

### Step 1: Per-Section Symbol Offsets (`sub_4325A0`)

The layout engine is called on each data-bearing section. `.text.*` sections are **not** touched by `sub_439830` -- they hold fully resolved SASS after merge and have stable internal layout. Only shared/constant/global data sections have their symbol offsets reassigned.

#### Step 1a: `.nv.constant0.kernel_a` (Phase 9b)

Symbol list after merge (in insertion order):

| Symbol | Size | Align |
|---|---|---|
| `__cudaparm_kernel_a_ptr_out` | 8 | 8 |
| `__cudaparm_kernel_a_count` | 4 | 4 |
| `__cudaparm_kernel_a_scale` | 4 | 4 |
| `__cudaparm_kernel_a_bias` | 4 | 4 |
| `__cudaparm_kernel_a_mode` | 4 | 4 |

`sub_4325A0` first calls `sub_4647D0(section+72, sub_432440)` to stable-sort by alignment (decreasing). The list becomes `{ptr_out, count, scale, bias, mode}` (unchanged here -- insertion was already sorted). It then walks the list:

```text
current = 0
ptr_out: align=8   -> roundup(0,8)=0    -> value=0,  current=0+8=8
count:   align=4   -> roundup(8,4)=8    -> value=8,  current=8+4=12
scale:   align=4   -> roundup(12,4)=12  -> value=12, current=12+4=16
bias:    align=4   -> roundup(16,4)=16  -> value=16, current=16+4=20
mode:    align=4   -> roundup(20,4)=20  -> value=20, current=20+4=24
```

Stored back: `section+32 (total_size) = 24`. Verbose trace: `variable __cudaparm_kernel_a_ptr_out at offset 0` etc. The driver will later prepend the built-in 0x160-byte `gridDim/blockDim/...` header at runtime, so these user-visible offsets start at 0.

#### Step 1b: `.nv.constant0.kernel_b` (Phase 9b)

Symbol list:

| Symbol | Size | Align |
|---|---|---|
| `__cudaparm_kernel_b_ptr_in` | 8 | 8 |
| `__cudaparm_kernel_b_ptr_out` | 8 | 8 |
| `__cudaparm_kernel_b_n` | 4 | 4 |

Walk:

```text
current = 0
ptr_in:  align=8 -> value=0,  current=8
ptr_out: align=8 -> value=8,  current=16
n:       align=4 -> value=16, current=20
```

`section+32 = 20`. Note: the section's internal size is the tight 20, but the section's own `sh_addralign` will be promoted to 8 (the max symbol alignment seen) by the line `if (v11 > *(_QWORD *)(a2 + 48)) *(_QWORD *)(a2 + 48) = v11;` in `sub_4325A0`.

#### Step 1c: `.nv.constant2` (Phase 9b, shared user cbank)

This section is referenced by **both** kernels, so it is laid out as a single unified bank. Symbols (post-merge, after any dedup from Phase 9c):

| Symbol | Size | Align |
|---|---|---|
| `lookup_table` | 2048 | 16 |
| `coeffs` | 1024 | 16 |
| `weights` | 768 | 16 |
| `masks` | 128 | 8 |
| `thresholds` | 16 | 4 |

Walk:

```text
current = 0
lookup_table: align=16 -> roundup(0,16)=0       -> value=0,    current=2048
coeffs:       align=16 -> roundup(2048,16)=2048 -> value=2048, current=3072
weights:      align=16 -> roundup(3072,16)=3072 -> value=3072, current=3840
masks:        align=8  -> roundup(3840,8)=3840  -> value=3840, current=3968
thresholds:   align=4  -> roundup(3968,4)=3968  -> value=3968, current=3984
```

`section+32 = 3984`. The section occupies bytes `[0, 3984)` within cbank2; the remaining `4096 - 3984 = 112` bytes are unused padding at the bank's tail. The hardware cbank2 window for a kernel is a fixed 64 KB slice of constant memory; only the bytes covered by actual symbols are materialized in the output ELF.

Verbose trace for this section:

```text
constant entry .nv.constant2:
variable lookup_table at offset 0
variable coeffs at offset 2048
variable weights at offset 3072
variable masks at offset 3840
variable thresholds at offset 3968
```

### Step 2: Shared Memory Overlap Graph (Phase 4)

Both kernels reference the globals in `.nv.shared`. The shared memory optimizer (`sub_436BD0`) determines which variables interfere.

**Step 2a: scan relocations and build reachability sets.** `sub_439830` walks `elfw+376` (the relocation list) and for each relocation that targets a `.nv.shared` symbol, identifies which entry function owns the referencing `.text.*` section. This produces:

```text
g_tmp   -> {kernel_a, kernel_b}   (both kernels call a helper that touches g_tmp)
g_hist  -> {kernel_a}             (only kernel_a updates the histogram)
s_local -> {kernel_a}             (from .nv.shared.kernel_a, per-entry)
```

**Step 2b: build interference graph.** Two shared vars interfere iff their entry-sets overlap:

```text
              g_tmp    g_hist   s_local
    g_tmp     --       X        X       (both share kernel_a)
    g_hist    X        --       X       (both live in kernel_a)
    s_local   X        X        --
```

An edge means "cannot share an address." In ASCII:

```text
           +--------+
           | g_tmp  |
           +--------+
            /      \
           /        \
          /          \
   +--------+    +---------+
   | g_hist |----| s_local |
   +--------+    +---------+
```

Every pair interferes because `kernel_a` touches all three. Result: no aliasing is possible; each variable needs its own offset. Verbose trace: no `"global shared %s only used in entry %d"` lines are emitted (each var has more than one user or is already entry-local).

**Step 2c: lay out `.nv.shared` globals.** `sub_4325A0` is called (via the optimized path in `sub_436BD0` which reduces to the plain layout here since no merging is possible):

```text
current = 0
g_hist: align=16 -> roundup(0,16)=0   -> value=0,  current=96
g_tmp:  align=8  -> roundup(96,8)=96  -> value=96, current=160
```

Sort pulled `g_hist` ahead of `g_tmp` (higher alignment first). `section+32 = 160`. Global shared high-water mark = 160.

**Step 2d: lay out `.nv.shared.kernel_a` (Phase 4c).** `kernel_a` reaches the global shared region (its relocation set contains `g_tmp` and `g_hist`), so the per-entry layout starts **after** the global high-water mark. `sub_436BD0` calls `sub_4325A0` with `initial_offset = roundup(160, per_entry_align)`.

Per-entry symbols in `.nv.shared.kernel_a`:

| Symbol | Size | Align |
|---|---|---|
| `s_local` (shared tile) | 128 | 16 |

```text
initial = 160
s_local: align=16 -> roundup(160,16)=160 -> value=160, current=160+128=288
```

`section+32 = 288`. Verbose: `shared entry .nv.shared.kernel_a:` then `variable s_local at offset 160`.

**Step 2e: `kernel_b` has no per-entry `.nv.shared.kernel_b`.** Its shared memory requirement is just the global region it reaches (`g_tmp` at offset 96). If `kernel_b` did **not** reach any global shared variable, the verbose trace would emit `entry kernel_b does not reach global shared` and its hypothetical per-entry section would start at offset 0 instead of 160 -- this is the key optimization: non-overlapping kernels can reuse the low shared addresses.

Final per-kernel shared memory footprints:

| Kernel | Shared high-water | Composition |
|---|---|---|
| `kernel_a` | 288 B | `g_hist[0..96) + g_tmp[96..160) + s_local[160..288)` |
| `kernel_b` | 160 B | `g_hist[0..96) + g_tmp[96..160)` (g_hist addr exists but is dead for b) |

### Step 3: Constant Bank Layout Summary

After Phase 9 completes, the final per-bank picture is:

```text
cbank0 / kernel_a:
    +0    +---------------------+
          | __cudaparm_ptr_out  |  (8 B)
    +8    +---------------------+
          | __cudaparm_count    |  (4 B)
    +12   +---------------------+
          | __cudaparm_scale    |  (4 B)
    +16   +---------------------+
          | __cudaparm_bias     |  (4 B)
    +20   +---------------------+
          | __cudaparm_mode     |  (4 B)
    +24   +---------------------+
    ...   (driver fills rest)

cbank0 / kernel_b:
    +0    +---------------------+
          | __cudaparm_ptr_in   |  (8 B)
    +8    +---------------------+
          | __cudaparm_ptr_out  |  (8 B)
    +16   +---------------------+
          | __cudaparm_n        |  (4 B)
    +20   +---------------------+

cbank2 (shared by both kernels):
    +0    +---------------------+
          | lookup_table        |  (2048 B, align 16)
    +2048 +---------------------+
          | coeffs              |  (1024 B, align 16)
    +3072 +---------------------+
          | weights             |  (768 B, align 16)
    +3840 +---------------------+
          | masks               |  (128 B, align 8)
    +3968 +---------------------+
          | thresholds          |  (16 B, align 4)
    +3984 +---------------------+
          | (padding up to 4096)|
    +4096 +---------------------+
```

Both kernels see the **same** cbank2 addresses -- that is the whole point of a non-per-entry constant bank. In contrast, cbank0 is split per entry because the parameter layout differs between kernels.

### Step 4: Final File `sh_offset` Table

The layout phase only assigns symbol offsets *within* sections; file offsets (`sh_offset` in each section header) are materialized later during the output phase (`sub_452010` / writer sub-pipeline), which walks the section list and places each section sequentially in the ELF file respecting `sh_addralign`.

For the merged ELF described above, assuming a standard 64-byte ELF64 header and no program headers (typical for a `.cubin` fragment before fatbinary wrap), the file layout is:

| # | Section | `sh_type` | `sh_addralign` | `sh_size` | Raw write start | `sh_offset` (aligned) | `sh_offset + sh_size` |
|---|---|---|---|---|---|---|---|
| 0 | `NULL` | `SHT_NULL` | 0 | 0 | -- | 0 | 0 |
| 1 | `.text.kernel_a` | `PROGBITS` | 128 | 2048 | 64 | **128** | 2176 |
| 2 | `.text.kernel_b` | `PROGBITS` | 128 | 1024 | 2176 | **2176** | 3200 |
| 3 | `.nv.constant0.kernel_a` | `PROGBITS` | 8 | 24 | 3200 | **3200** | 3224 |
| 4 | `.nv.constant0.kernel_b` | `PROGBITS` | 8 | 20 | 3224 | **3224** | 3244 |
| 5 | `.nv.constant2` | `PROGBITS` | 16 | 3984 | 3244 | **3248** | 7232 |
| 6 | `.nv.shared` | `NOBITS` | 16 | 160 | 7232 | **7232** | 7232 |
| 7 | `.nv.shared.kernel_a` | `NOBITS` | 16 | 288 | 7232 | **7232** | 7232 |
| 8 | `.shstrtab` | `STRTAB` | 1 | ~120 | 7232 | **7232** | 7352 |
| 9 | `.symtab` | `SYMTAB` | 8 | ... | 7352 | **7360** | ... |
| 10 | `.strtab` | `STRTAB` | 1 | ... | ... | ... | ... |

Alignment padding events to notice:

- Section 1 (`.text.kernel_a`) aligns from raw 64 up to 128: **64 bytes of zero padding** immediately after the ELF header.
- Section 2 (`.text.kernel_b`) is already 128-aligned at 2176 (since 2048 was a multiple of 128): **no padding**.
- Section 3 (`.nv.constant0.kernel_a`) at 3200 is 8-aligned already: **no padding**.
- Section 4 (`.nv.constant0.kernel_b`) at 3224 is 8-aligned: **no padding**.
- Section 5 (`.nv.constant2`) at 3244 must align to 16: **4 bytes of padding** (3244 -> 3248).
- Sections 6 and 7 are `SHT_NOBITS`: they occupy zero bytes in the file, so their `sh_offset` is set to the current write position but the cursor does **not** advance past them. Both record `sh_offset = 7232`. The same file-vs-memory asymmetry surfaces at program-header level when the cubin is executable -- NOBITS sections in the code segment add to `p_memsz` (via `code_nobits_sz`) without touching `p_filesz`; see [Program Headers: First Pass](../elf/program-headers.md#first-pass-compute-segment-extents).
- Section 9 (`.symtab`) aligns 7352 -> 7360: **8 bytes of padding**.

The section content cursor therefore advances: `64 -> 128 -> 2176 -> 3200 -> 3224 -> 3244 -> 3248 -> 7232` (data sections done) `-> 7352 -> 7360 -> ...` (metadata sections). Total file padding: `64 + 4 + 8 = 76` bytes.

### Step 5: Order of Offset Assignment

For reference, the exact order in which `sub_439830` assigns offsets for this example is:

1. **Phase 1** (global data): no `.nv.global` pending, skipped.
2. **Phase 3** (global init): no `.nv.global.init`, skipped.
3. **Phase 4b** (global shared): `g_hist=0`, `g_tmp=96`. Section size = 160.
4. **Phase 4c** (per-entry shared): `.nv.shared.kernel_a.s_local = 160`. Section size = 288.
5. **Phase 5** (extern shared): no extern shared in this example, skipped.
6. **Phase 6** (reserved shared): no `.nv.reservedSmem` used, skipped.
7. **Phase 7** (local data): no `.nv.local.*`, skipped.
8. **Phase 8** (overlapped constant replacement): dedup table empty, skipped.
9. **Phase 9a** (OCG split): cbank0 was already per-entry, no split needed.
10. **Phase 9b** (non-OCG constants): `.nv.constant0.kernel_a` -> symbols at 0/8/12/16/20 (size 24). `.nv.constant0.kernel_b` -> symbols at 0/8/16 (size 20). `.nv.constant2` -> symbols at 0/2048/3072/3840/3968 (size 3984).
11. **Phase 9c** (constant merge): `merge-constants` flag was clear, skipped.
12. **Phase 9d** (OCG size optimization): cbank0 sizes (24 and 20) are well under the 64 KB limit, no optimization.
13. **Phase 10** (resource counting + UFT): textures/samplers/surfaces = 0; `sub_463F70` creates empty `.nv.uft`/`.nv.udt`.

At this point `sub_439830` returns and control passes to the relocation phase (`sub_469D60`), which uses the offsets assigned above to patch `R_CUDA_ABS32_LO_20` (for `g_tmp`/`g_hist`/`s_local`) and `R_CUDA_CONST_FIELD` (for `lookup_table`/`coeffs`/etc.) into the already-finalized `.text.*` bytes.

## Cross-References

- [Pipeline Overview](overview.md) -- layout phase in the context of the full 14-phase pipeline
- [Merge Phase](merge.md) -- preceding phase that produces the merged elfw consumed by layout
- [Relocation Phase](relocate.md) -- succeeding phase that patches instruction/data bytes against addresses assigned here
- [Section Merging](../linker/section-merging.md) -- how sections are merged during Phase 9 before layout assigns addresses
- [Data Layout Optimization](../linker/data-layout-opt.md) -- the constant deduplication and overlap merge sub-algorithms called from layout
- [Bindless Relocations](../linker/bindless-relocations.md) -- Phase 2 bindless resource processing detail (`sub_438DD0`)
- [Unified Function Tables](../elf/uft.md) -- Phase 10 UFT/UDT section creation and validation (`sub_463F70`)
- [Constant Banks](../elf/constant-banks.md) -- `.nv.constant0`/`.nv.constant2` layout and R\_CUDA\_CONST\_FIELD relocations
- [Program Headers](../elf/program-headers.md) -- how laid-out sections map to ELF program headers in the output
- [Architecture Profiles](../targets/arch-profiles.md) -- the vtable at `elfw+488` queried throughout layout for architecture-specific limits
- [ELF Writer Structure](../structs/elf-writer.md) -- the elfw data structure that layout mutates
- [Section Record](../structs/section-record.md) -- per-section metadata records updated during layout
- [Symbol Record](../structs/symbol-record.md) -- per-symbol records whose `value` field is assigned during layout
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- callgraph reachability analysis that determines which symbols are live for layout

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_439830` at `0x439830`, 65,776 bytes, ~2,173 lines | **HIGH** | `stat -c%s` = 65,776; `wc -l` = 2,173 |
| Single argument `(elfw*)` signature | **HIGH** | Decompiled: `size_t __fastcall sub_439830(__int64 a1)` |
| `"global shared %s only used in entry %d"` | **HIGH** | String at `0x1d389a8` in `nvlink_strings.json` |
| `"remove unused global shared %s"` | **HIGH** | String at `0x1d389d0` in `nvlink_strings.json` |
| `"variable %s at offset %d"` | **HIGH** | String at `0x1d38739` in `nvlink_strings.json` |
| `"esh %s has offset %d"` | **HIGH** | String at `0x1d38bcb` in `nvlink_strings.json` |
| `"entry %s does not reach global shared"` | **HIGH** | String at `0x1d38da8` in `nvlink_strings.json` |
| `"shared entry %s:"` | **HIGH** | String at `0x1d38be1` in `nvlink_strings.json` |
| `"extern shared variable %s at offset %lld"` | **HIGH** | String found in `nvlink_strings.json` (partial match at `0x1d38a80`) |
| `"overlapping sets %d and %d"` | **HIGH** | String at `0x1d38c02` in `nvlink_strings.json` |
| `"reloc of extern shared %d replaced with symbol %d"` | **HIGH** | String at `0x1d38e28` in `nvlink_strings.json` |
| `"local entry %s:"` | **HIGH** | String at `0x1d38c7c` in `nvlink_strings.json` |
| `"constant entry %s:"` | **HIGH** | String at `0x1d38c8d` in `nvlink_strings.json` |
| `"found duplicate value 0x%x, alias %s to %s"` | **HIGH** | String at `0x1d38888` in `nvlink_strings.json` |
| `"remove unused constant %s"` | **HIGH** | String at `0x1d38cf9` in `nvlink_strings.json` |
| `"layout and merge section %s"` | **HIGH** | String at `0x1d38d46` in `nvlink_strings.json` |
| `"optimize space in %s (%d)"` | **HIGH** | String at `0x1d38ccf` in `nvlink_strings.json` |
| `"need new ocg section %s"` | **HIGH** | String at `0x1d38ca1` in `nvlink_strings.json` |
| `"new OCG constant size = %lld"` | **HIGH** | String at `0x1d38d8a` in `nvlink_strings.json` |
| `"ocg const optimization didn't help so give up"` | **HIGH** | String at `0x1d39058` in `nvlink_strings.json` |
| `"reset ocg constant reloc offset from %lld to %lld"` | **HIGH** | String at `0x1d38f60` in `nvlink_strings.json` |
| `"should only reach here with no opt"` assertion | **HIGH** | String at `0x1d38758` in `nvlink_strings.json` |
| `"tail data node not found"` assertion | **HIGH** | String at `0x1d38839` in `nvlink_strings.json` |
| `"shared variable %s updated offset to %lld"` | **HIGH** | String found in `nvlink_strings.json` |
| `TEMP_MERGED_CONSTANTS` / `TEMP_OCG_CONSTANTS` temp sections | **HIGH** | Strings at `0x1d38d30` and `0x1d38d77` in `nvlink_strings.json` |
| `.nv.reservedSmem.begin` / `.cap` / `.offset0` symbols | **HIGH** | `".nv.reservedSmem.begin"` at `0x1d38c37` in `nvlink_strings.json` |
| `.nv.global.init` section reference | **HIGH** | String at `0x1d38940` in `nvlink_strings.json` |
| `.nv.uft` / `.nv.udt` sections | **HIGH** | Strings at `0x1d39f74` and `0x1d38924` in `nvlink_strings.json` |
| `sub_436BD0` (shared memory optimizer), 15,711 B | **HIGH** | `stat -c%s` = 15,711 bytes |
| `sub_4339A0` (constant dedup), 13,199 B | **HIGH** | `stat -c%s` = 13,199 bytes |
| `sub_438DD0` (process_bindless_refs), 12,779 B | **HIGH** | `stat -c%s` = 12,779 bytes |
| `sub_463F70` (uft_setup_sections), 3,978 B | **HIGH** | `stat -c%s` = 3,978 bytes |
| All 14 function addresses in the key sub-functions table | **HIGH** | All verified to exist in `decompiled/` directory |
| 10-phase sequential execution order | **MEDIUM** | Phase ordering matches decompiled control flow in `sub_439830`; sub-phase boundaries are editorial grouping |
| `elfw` field offsets (+7, +16, +48, +64, +80, +81, etc.) | **MEDIUM** | Consistent with decompiled pointer arithmetic; individual offsets are inferred from `a1 + N` patterns |
| Architecture vtable offsets (+32, +40, +48, +136, +296, etc.) | **MEDIUM** | Offsets inferred from decompiled indirect calls `*(fn*)(a1 + 488 + N)`; consistent across the codebase |
| Interference graph algorithm for shared memory optimization | **MEDIUM** | Algorithmic description inferred from control flow of `sub_436BD0`; graph-coloring terminology is editorial |
