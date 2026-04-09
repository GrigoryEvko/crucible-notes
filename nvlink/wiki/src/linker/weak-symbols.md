# Weak Symbol Handling

Weak symbols are a standard ELF mechanism that allows multiple translation units to define the same function without causing a "multiple definition" error. When the linker encounters duplicate weak definitions, it must choose one and discard the rest. nvlink extends this standard behavior with a CUDA-specific selection policy: rather than picking an arbitrary winner, it compares register pressure and PTX version metadata to select the definition most likely to produce efficient GPU code.

The entire weak symbol resolution logic lives in a single function -- `merge_weak_function` at `sub_45D180` -- which is called during Phase 3 of `merge_elf` (`sub_45E7D0`), before section iteration begins. At 26,816 bytes (~27 KB, 913 decompiled lines), it is the second-largest function in the merge subsystem after `merge_elf` itself.

## Key Facts

| Property | Value |
|---|---|
| Primary function | `sub_45D180` (`merge_weak_function`) |
| Address | `0x45D180` |
| Size | 26,816 bytes (~27 KB) |
| Decompiled lines | 913 |
| Called by | `sub_45E7D0` (`merge_elf`), Phase 3 symbol pass |
| Verbose flag | `ctx+64` bit 4 (`-v` verbose flag) |
| Recursive | Yes -- calls itself to resolve cross-referenced weak section symbols |

## When Weak Symbols Arise

In CUDA compilation, weak symbols are generated in several situations:

1. **Template instantiations**: Different translation units instantiating the same device template produce identical weak definitions. nvcc marks these `STB_WEAK` so the linker can collapse them.
2. **Inline functions in headers**: Device functions defined in headers (with `__device__` or `__host__ __device__`) that get compiled into multiple cubins produce weak definitions.
3. **libcudadevrt stubs**: Certain CUDA runtime support functions are compiled as weak so that user-provided implementations can override them.

The problem unique to GPU linking is that different compilation units may produce different register allocations for the same function body. A definition using 32 registers will cause the GPU to schedule fewer concurrent warps than one using 24 registers. nvlink's weak resolution policy attempts to select the definition with the lowest register pressure, maximizing occupancy.

## Invocation Context

During `merge_elf`, after the mapping tables are allocated (Phase 2) and before section iteration (Phase 4), the linker makes a dedicated symbol pass:

```c
// Phase 3: Weak function resolution
for (sym_idx = 0; sym_idx < merge_ctx->num_symbols; sym_idx++) {
    sym = get_symbol(merge_ctx, sym_idx);
    binding = sym->st_info >> 4;
    type    = sym->st_info & 0xF;

    if (type == STT_FUNC && binding == STB_WEAK) {  // binding == 2
        map_symbol_index[sym_idx] = merge_weak_function(
            ctx, input_elf, merge_ctx, sym_idx,
            sym->st_info, sym->st_shndx, ...
        );
    }
}
```

The result is stored in the `map_symbol_index` mapping table. Later phases use this mapping to translate symbol references from input-local to output-global indices. A separate `weak_processed` boolean array (one byte per symbol, at `merge_ctx+64`) tracks which symbols have already been resolved, preventing re-processing when sections are iterated in Phase 4.

## The Three Replacement Strategies

When `merge_weak_function` encounters a weak function symbol that already has a definition in the output ELF, it must decide whether to keep the existing definition or replace it. The decision follows a three-tier priority:

### Strategy 1: Register Count Comparison (Fewer Registers Wins)

The primary criterion. The linker extracts the register count for both the existing and incoming definitions and prefers the one using fewer registers:

```
Verbose trace: "replace weak function %s with weak that uses fewer registers"
```

Register counts are stored in the `.nv.info` section as EIATTR attribute code 47 (`0x2F`). This is a per-function property encoded as a 4-byte TLV record in the nvinfo format:

```
[04] [2F] [size:2] [sym_index:4] [reg_count:4]
```

The extraction process uses a two-tier lookup for each side (incoming and existing), trying a cached value first and falling back to a raw scan only if the cache is empty.

#### Incoming Register Count Extraction

The incoming definition's register count is carried inside the section header record that was copied from the input ELF during Phase 2. Specifically, it sits in the high byte of the fourth 32-bit word of the packed section header copy (the same structure copied as `v254`/`v255`/`n`/`v257` at function entry). In the decompiled code this is `HIBYTE(n[1])`, which corresponds to byte 3 of word 7 in the 10-word section header record -- the register count field that `cicc` / `ptxas` wrote into the `.nv.info` section and that the ELF loader cached into the section header structure.

If this cached byte is zero (indicating the register count was not populated during the initial section header copy), the function emits a verbose trace and falls back to scanning the raw `.nv.info` section data in the input ELF:

```
Verbose trace: "no new register count found for %s, checking .nv.info"
```

The fallback scan iterates every section in the input ELF, filtering for `SHT_CUDA_INFO` (`0x70000000`) sections with non-zero size. Within each qualifying section, it walks the TLV records looking for format `0x04` (indexed) with attribute code 47 (`0x2F`) whose symbol index matches the target function:

```c
// Incoming register count fallback -- scan input ELF .nv.info sections
// From decompiled sub_45D180, lines 783-833

uint32_t num_sections = elf64_section_count(input_elf);  // sub_448730
uint32_t new_reg_count = 0;

for (uint32_t sec_idx = 0; sec_idx < num_sections; sec_idx++) {
    Elf64_Shdr *shdr = elf64_section_by_index(input_elf, sec_idx);  // sub_448370
    if (shdr->sh_type != SHT_CUDA_INFO || shdr->sh_size == 0)
        continue;

    uint8_t *ptr = input_data + shdr->sh_offset;
    uint8_t *end = ptr + shdr->sh_size;

    while (ptr < end) {
        // Advance past non-indexed records: if format != 0x04,
        // skip in 4-byte steps until we find an indexed record
        // or exhaust the section
        if (*ptr != 0x04) {
            ptr += 4;
            continue;
        }

        // Indexed record found -- check attribute code
        uint32_t *payload = (uint32_t *)(ptr + 4);
        if (ptr[1] == 0x2F) {                    // EIATTR_REGCOUNT
            if (payload[0] == target_sym_idx) {   // symbol index match
                new_reg_count = payload[1];       // register count value
                goto have_new_reg_count;
            }
        }
        // Advance by header (4 bytes) + payload size
        ptr += 4 + *(uint16_t *)(ptr + 2);
    }
}

// If no section contained a matching record, emit fatal diagnostic
fatal_error("no such new reg count");
```

The non-indexed record skip loop deserves attention: when the format byte is not `0x04`, the code does not parse the size field. Instead it advances by a fixed 4 bytes, effectively treating the entire 4-byte header as a self-contained record. This works because non-indexed formats (`0x01`, `0x02`, `0x03`) that carry payload will have their header consumed in one step, then subsequent 4-byte steps will walk through the payload. It is an optimization that avoids branching on the size field for records the scan does not care about -- slightly wasteful in that it reads payload bytes as if they were headers, but correct because the scan only acts on records where `format == 0x04 && attr_code == 0x2F`.

#### Existing Register Count Extraction

The existing (already-in-output) definition's register count is read from byte +47 of its callgraph/nvinfo record in the output ELF, retrieved via `sub_442270` (`elfw_get_callgraph_entry`). This is the same byte that is written when processing EIATTR code 47 during the merge phase's `.nv.info` section handling. When the first definition of a weak function was merged, its register count attribute was parsed and stored at this offset.

```c
// Existing register count -- cached path
// From decompiled sub_45D180, lines 837-838

uint64_t callgraph_record = elfw_get_callgraph_entry(ctx, output_section_id);
uint32_t existing_reg_count = *(uint8_t *)(callgraph_record + 47);
```

If this byte is zero (the existing definition's register count was never cached -- possible if the first definition's `.nv.info` was malformed or empty), the function emits a verbose trace and falls back to scanning the output ELF's nvinfo linked list:

```
Verbose trace: "no original register count found for %s, checking .nv.info"
```

This fallback is structurally different from the incoming scan. Instead of walking raw section bytes in an input ELF, it iterates the output's nvinfo linked list (at `ctx+392`) using the list iterator API:

```c
// Existing register count fallback -- walk output nvinfo list
// From decompiled sub_45D180, lines 843-859

list_iterator_t it = list_begin(ctx->nvinfo_list);  // sub_464A80

while (!list_at_end(it)) {                           // sub_464A90
    nvinfo_entry_t *entry = list_deref(it);           // sub_464AC0

    if (entry->attr_code == 0x2F) {                   // EIATTR_REGCOUNT
        uint32_t *payload = entry->payload_ptr;        // offset +8
        if (payload[0] == output_symbol_id) {          // symbol index match
            existing_reg_count = payload[1];           // register count value
            break;
        }
    }

    it = list_next(it);                                // sub_464AA0
}

if (list_at_end(it))
    fatal_error("no such original reg count");
```

The key difference: incoming counts are extracted from raw ELF bytes in the input file; existing counts are extracted from the already-parsed nvinfo linked list in the output context. This asymmetry exists because the incoming definition has not yet been committed to the output -- its `.nv.info` data is still in the input ELF's raw section bytes. The existing definition's data was parsed and appended to the output nvinfo list when the first definition was merged.

#### Register Count Extraction Summary

| Side | Primary source | Fallback source | Fatal if missing |
|---|---|---|---|
| Incoming (new) | Section header record, high byte of word 7 | Raw `.nv.info` TLV scan of input ELF sections | Yes: `"no such new reg count"` |
| Existing (old) | Callgraph record byte +47 in output ELF | Output nvinfo linked list iterator scan | Yes: `"no such original reg count"` |

Both paths are fatal on failure -- if a weak function definition has no register count attribute at all, the linker aborts. This should never happen with well-formed cubins, since `ptxas` always emits EIATTR code 47 for every function.

#### The Comparison

Once both register counts are extracted, the comparison is a simple unsigned integer test:

```c
// From decompiled sub_45D180, lines 862-883

if (new_reg_count < existing_reg_count) {
    // Incoming definition uses fewer registers -- replace unconditionally
    if (verbose)
        fprintf(stderr,
            "replace weak function %s with weak that uses fewer registers\n",
            func_name);
    do_replace = true;

} else if (new_reg_count == existing_reg_count) {
    // Tie -- fall through to PTX version comparison (Strategy 2)
    do_replace = false;  // may be overridden by PTX check

} else {
    // Incoming uses more registers -- keep existing definition
    do_replace = false;
}
```

The strictly-less-than test (`new < existing`) means that when two definitions have the same register count, this strategy does not trigger replacement. Control falls through to Strategy 2.

### Strategy 2: PTX Version Comparison (Newer PTX Wins)

When register counts are equal (`new_reg_count == existing_reg_count`), the linker compares the PTX ISA versions of the two definitions as a tiebreaker. Each input file carries a PTX version at offset +8 in its input record (the `input_record_t` structure retrieved via `sub_464DB0` from the input list at `ctx+512`). The linker obtains both records:

```c
// From decompiled sub_45D180, lines 479-481

// Incoming definition's input record: last entry in the input list (current file)
input_record_t *incoming_record = list_get(ctx->input_list,
                                           list_count(ctx->input_list) - 1);

// Existing definition's input record: indexed by the symbol's stored input index
//   (offset +40 in the output symbol record, set when the symbol was first added)
input_record_t *existing_record = list_get(ctx->input_list,
                                           output_symbol->input_idx);
```

The PTX version comparison is then:

```c
// From decompiled sub_45D180, line 866

if (new_reg_count == existing_reg_count
    && incoming_record->ptx_version > existing_record->ptx_version) {
    if (verbose)
        fprintf(stderr,
            "replace weak function %s with weak from newer PTX\n", func_name);
    do_replace = true;
}
```

```
Verbose trace: "replace weak function %s with weak from newer PTX"
```

The comparison is strictly greater-than (`>`). If the incoming PTX version is equal to or less than the existing one, no replacement occurs. The rationale: a newer PTX compiler version may have produced better instruction scheduling or more compact code, even when register pressure happens to be identical.

### Strategy 3: First Definition Wins (Fallback)

If the incoming definition uses more registers than the existing one, or if both register count and PTX version are identical (or incoming PTX is older), the existing definition is kept. No verbose trace is emitted for this case -- the incoming definition is silently discarded, and the `do_replace` flag remains false.

#### Complete Decision Tree

```
merge_weak_function(incoming, existing):
    new_regs = extract_reg_count(incoming)     // cached byte, else input ELF scan
    old_regs = extract_reg_count(existing)     // callgraph byte +47, else output list scan

    if new_regs < old_regs:
        REPLACE  ("...with weak that uses fewer registers")
    elif new_regs == old_regs:
        new_ptx = incoming_input_record->ptx_version
        old_ptx = existing_input_record->ptx_version
        if new_ptx > old_ptx:
            REPLACE  ("...with weak from newer PTX")
        else:
            KEEP existing (silent)
    else:  // new_regs > old_regs
        KEEP existing (silent)
```

### Basic Replacement (Global-over-Weak)

A separate path handles the case where a `STB_GLOBAL` symbol replaces a `STB_WEAK` symbol. This follows standard ELF semantics -- a strong definition always overrides a weak one, with no comparison:

```
Verbose trace: "replace weak function %s"
```

This occurs when binding byte >> 4 is 1 (global) for the incoming symbol and 2 (weak) for the existing one. The replacement is unconditional.

## Post-Replacement Cleanup

When a weak function is replaced, the old definition's associated metadata must be removed from the output ELF. The function performs four distinct cleanup passes:

### 1. Relocation Removal

The function iterates the output's relocation list (linked list at `ctx+376`) and nullifies relocations targeting the replaced function's section:

```c
// Walk relocation list
for (reloc = ctx->reloc_list; reloc; reloc = reloc->next) {
    entry = reloc->data;
    target_section = entry->target_section_idx;  // offset +24

    if (target_section == old_rela_section || target_section == old_rel_section) {
        if (verbose)
            fwrite("remove weak reloc\n", 1, 0x12, stderr);
        entry->reloc_info = 0;  // nullify: offset +8 = 0
    }
}
```

The function looks up both `SHT_REL` (type 9) and `SHT_RELA` (type 4) sections associated with the old definition, using `sub_442760` to find the section by symbol index and type.

### 2. Debug Relocation Removal

Relocations targeting debug sections associated with the replaced function are also removed. Three debug section names are checked:

| Section name | Description |
|---|---|
| `.debug_line` | Standard DWARF line number tables |
| `.nv_debug_line_sass` | NVIDIA SASS-level line number tables |
| `.debug_frame` | DWARF call frame information |

For each debug section, the function looks up both `SHT_REL` and `SHT_RELA` variants and nullifies matching relocations:

```c
// For each debug section name
const char *debug_sections[] = {
    ".debug_line", ".nv_debug_line_sass", ".debug_frame"
};

for (int d = 0; d < 3; d++) {
    uint32_t sec_id   = find_section(ctx, debug_sections[d]);
    uint32_t rela_sec = find_reloc_section(ctx, sec_id, SHT_RELA);
    uint32_t rel_sec  = find_reloc_section(ctx, sec_id, SHT_REL);

    if (entry->target_section == rela_sec || entry->target_section == rel_sec) {
        if (verbose)
            fwrite("remove weak reloc from debug\n", 1, 0x1D, stderr);
        entry->reloc_info = 0;
    }
}
```

The section lookup uses `sub_4411D0` (find section by name) and `sub_442760` (find associated relocation section by symbol and type).

### 3. nvinfo Entry Removal

The function walks the output's nvinfo list (linked list at `ctx+392`) and clears entries belonging to the replaced function. Two kinds of entries are targeted:

**Direct nvinfo entries**: Entries whose function reference (offset +4 in the nvinfo record) matches the old function's section ID are zeroed:

```
Verbose trace: "remove weak nvinfo"
```

**Frame-size-class attributes**: Entries with specific EIATTR attribute codes that reference the replaced function symbol. The attribute codes are identified by a 64-bit bitmask `0x800800020000`, which has bits set at positions 17, 35, and 47:

| Bit | EIATTR code | Meaning |
|---|---|---|
| 17 | `0x11` | Frame size / stack information |
| 35 | `0x23` | CRS (call-return stack) stack size |
| 47 | `0x2F` | Register count (REGCOUNT) |

```c
uint64_t frame_size_mask = 0x800800020000ULL;

for (nvinfo = ctx->nvinfo_list; nvinfo; nvinfo = nvinfo->next) {
    entry = nvinfo->data;
    if (entry->function_ref == old_section_id) {
        if (verbose)
            fwrite("remove weak nvinfo\n", 1, 0x13, stderr);
        entry->attr_code = 0;  // nullify
    } else {
        uint8_t code = entry->attr_code;
        if (code <= 47 && bittest(frame_size_mask, code)) {
            uint32_t *payload = entry->payload_ptr;
            if (*payload == old_symbol_id) {
                if (verbose)
                    fwrite("remove weak frame_size\n", 1, 0x17, stderr);
                entry->attr_code = 0;  // nullify
            }
        }
    }
}
```

Setting `attr_code` to zero effectively marks the entry as dead. Later phases skip entries with a zero attribute code.

### 4. OCG Constant Section Removal

Each function may have an associated "OCG constant" section (offline code generation constants) named `<module>.<function>`, constructed by concatenating the module name and the function name with a dot separator:

```c
char *module_name = get_module_name(ctx);       // sub_4401F0
sprintf(buf, "%s.%s", module_name, func_name);  // e.g. "module0._Z3foov"
uint32_t ocg_sec = find_section(ctx, buf);       // sub_4411D0
```

If this section exists, its relocation list and size record are cleared:

```
Verbose trace: "remove weak ocg constants"
```

The function walks the OCG section's relocation chain (at record offset +72), calls `sub_431000` (arena\_free) on each relocation entry's data, then destroys the chain via `sub_464520` (list\_destroy). The section's size field (offset +32) is zeroed.

## Recursive Section Symbol Resolution

A notable feature of `merge_weak_function` is that it is **recursive**. When a weak function symbol references another section (via the EIATTR "common section" field at bits 24-47 of the packed section index), and that referenced section has not yet been mapped, the function calls itself to resolve it first:

```c
// If the function references a common section that hasn't been mapped yet
uint32_t common_sec_idx = (packed_field << 8) >> 8;  // extract 24-bit index
if (common_sec_idx != sym_idx && !map_section[common_sec_idx]) {
    // Recursively resolve the referenced section's symbol
    map_section[common_sec_idx] = merge_weak_function(
        ctx, input_elf, merge_ctx, common_sec_idx, ...
    );
}
```

This ensures that all dependent sections are properly mapped before the primary symbol's data is copied. The recursion depth is bounded by the input ELF's section count and the requirement that `(a7 & 0x100000000000) == 0` (a flag bit that prevents infinite recursion).

## Global-over-Weak Propagation Flag

When a weak function is part of a global-init section (indicated by flag bit `0x80000000000` in the packed argument), the function sets a propagation flag at byte +95 of the linker context (`ctx+95`). This flag signals to later phases that global-init semantics apply to the resolved symbol:

```c
if (packed_flags & 0x80000000000ULL) {
    *(uint8_t *)(ctx + 95) = 1;
}
```

Similarly, when a weak symbol being replaced has the "address taken" flag (bit 3 of the symbol's visibility byte at offset +5), this flag is propagated to the incoming replacement:

```c
// Preserve "address taken" flag across replacement
uint8_t old_flags = old_symbol_record[5];
if (packed_flags & 0x80000000000ULL) {
    old_symbol_record[5] = old_flags | 0x08;
} else if (old_flags & 0x08) {
    new_packed_flags |= 0x80000000000ULL;  // carry forward
}
```

This ensures that if any definition of a weak function had its address taken, the final resolved symbol retains that property -- critical for the callgraph analysis and dead code elimination that follow the merge phase.

## Interaction with Other Phases

### Merge Phase (Phase 4 -- Section Iteration)

After weak resolution completes, the section iteration pass checks the `weak_processed` array before processing any weak symbol. If `weak_processed[sym_idx]` is set, the symbol is skipped. The merge page documents this as: "If a weak symbol arrives that was already handled, verbose mode prints `weak %s already processed`."

For `.nv.info` sections, attributes with codes 17, 35, 47, and 59 are skipped entirely for symbols that have been through weak resolution, since the winning definition's nvinfo data was already committed.

### Relocation Phase

During the relocation phase (`sub_469D60`), weak function symbols with zero `st_value` trigger an alias chain walk. The relocation engine follows aliases to find the canonical definition, printing `"change alias reloc %s to %s"` in verbose mode. This is the runtime complement of the merge-time weak selection: the merge phase picks the winner, and the relocation phase ensures all references point to it.

### Dead Code Elimination

The DCE pass (`sub_44AD40`) respects weak resolution results. If a weak function was replaced and its sections zeroed out by the cleanup logic described above, DCE treats those sections as dead. The "address taken" flag propagation ensures that if any weak definition had its address taken, the surviving definition is not erroneously eliminated.

## Verbose Trace Summary

All verbose traces are gated behind `(ctx+64) & 0x10`, which corresponds to the `-v` command-line flag. The complete set of trace messages emitted by `merge_weak_function`:

| Message | Meaning |
|---|---|
| `replace weak function %s` | Global symbol replacing a weak definition |
| `replace weak function %s with weak from newer PTX` | Tie-breaking by PTX version |
| `replace weak function %s with weak that uses fewer registers` | Primary selection by register count |
| `no new register count found for %s, checking .nv.info` | Incoming definition missing cached reg count |
| `no original register count found for %s, checking .nv.info` | Existing definition missing cached reg count |
| `remove weak reloc` | Nullifying relocation for replaced function |
| `remove weak reloc from debug` | Nullifying debug-section relocation |
| `remove weak frame_size` | Nullifying frame-size nvinfo attribute |
| `remove weak nvinfo` | Nullifying general nvinfo entry |
| `remove weak ocg constants` | Clearing OCG constant section |

## Internal Function Reference

| Address | Name | Role |
|---|---|---|
| `sub_45D180` | `merge_weak_function` | Primary weak resolution (this page) |
| `sub_45E7D0` | `merge_elf` | Caller -- invokes during Phase 3 |
| `sub_440590` | `elfw_get_symbol_record` | Retrieves symbol record by output index |
| `sub_440350` | `elfw_get_section_for_symbol` | Finds section associated with a symbol |
| `sub_442270` | `elfw_get_callgraph_entry` | Retrieves callgraph/nvinfo record for a symbol |
| `sub_442760` | `elfw_find_reloc_section` | Finds relocation section by symbol and type |
| `sub_4411D0` | `elfw_find_section_by_name` | Looks up output section by name string |
| `sub_440740` | `elfw_add_symbol` | Creates new symbol in output ELF |
| `sub_442820` | `elfw_merge_symbols` | Merges symbol tables (sets binding/type) |
| `sub_440430` | `elfw_set_symbol_section` | Updates symbol's section assignment |
| `sub_432B10` | `merge_overlapping_global_data` | Copies section data with offset tracking |
| `sub_448750` | `elfw_find_symbol_by_name` | Resolves extended symbol index (0xFFFF) |
| `sub_4401F0` | `get_module_name` | Retrieves module name for OCG section naming |
| `sub_464BB0` | `list_count` | Returns count of entries in linked list |
| `sub_464DB0` | `list_get` | Returns Nth element from a linked list |
| `sub_464A80` | `list_begin` | Returns iterator pointing to first element |
| `sub_464A90` | `list_at_end` | Tests whether iterator has reached end |
| `sub_464AA0` | `list_next` | Advances iterator to next element |
| `sub_464AC0` | `list_deref` | Dereferences iterator to get element data |
| `sub_464520` | `list_destroy` | Destroys a linked list |
| `sub_448730` | `elf64_section_count` | Returns `e_shnum` from input ELF header |
| `sub_448370` | `elf64_section_by_index` | Returns section header by index from input ELF |
| `sub_431000` | `arena_free` | Frees arena-allocated memory |
| `sub_467460` | `diagnostic_emit` | Emits error/warning/info diagnostic |

## Cross-References

- [Symbol Resolution](symbol-resolution.md) -- symbol storage (positive/negative arrays) and name lookup used by weak resolution
- [Section Merging](section-merging.md) -- merge phase that invokes `merge_weak_function` during Phase 3
- [Dead Code Elimination](dead-code-elimination.md) -- runs after merge; removed sections no longer participate in weak resolution
- [Data Layout Optimization](data-layout-opt.md) -- OCG constant sections cleared during weak replacement feed into constant dedup
- [Merge Phase](../pipeline/merge.md) -- the parent pipeline phase that calls `merge_elf` and triggers weak resolution

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_45D180` at 0x45D180 is `merge_weak_function`, called from `sub_45E7D0` Phase 3 | HIGH | Decompiled `sub_45D180_0x45d180.c` exists with matching signature; called from merge_elf |
| Verbose trace `"replace weak function %s with weak that uses fewer registers"` | HIGH | Decompiled line 881: exact string literal confirmed |
| Verbose trace `"replace weak function %s with weak from newer PTX"` | HIGH | Decompiled line 869: exact string literal confirmed |
| Verbose trace `"replace weak function %s"` (global-over-weak) | HIGH | Decompiled line 570-571: exact string literal confirmed |
| `"no such new reg count"` fatal on missing incoming register count | HIGH | Decompiled line 834: exact string literal via `sub_467460` |
| `"no such original reg count"` fatal on missing existing register count | HIGH | Decompiled line 847: exact string literal via `sub_467460` |
| `"no original register count found for %s, checking .nv.info"` fallback trace | HIGH | Decompiled line 842: exact string literal confirmed |
| Strategy 1: `new_reg_count < existing_reg_count` triggers replacement | HIGH | Confirmed by decompiled decision tree; strictly-less-than test verified |
| Strategy 2: equal regs + `incoming_ptx > existing_ptx` triggers replacement | HIGH | Decompiled line 869 shows PTX version comparison path gated by equal register count |
| Strategy 3: first definition wins when incoming uses more registers | HIGH | Implicit in decompiled control flow: no replacement when `new >= existing` |
| EIATTR code 47 (0x2F) for register count in .nv.info TLV | HIGH | Decompiled scan loop checks `ptr[1] == 0x2F` for REGCOUNT attribute |
| Frame-size bitmask 0x800800020000 for EIATTR codes 17, 35, 47 | HIGH | String `"remove weak frame_size"` confirmed in `nvlink_strings.json`; bitmask reconstructed from decompiled code |
| Post-replacement cleanup: reloc removal, debug reloc removal, nvinfo removal, OCG removal | HIGH | All four cleanup verbose strings (`"remove weak reloc"`, `"remove weak nvinfo"`, `"remove weak ocg constants"`) confirmed in `nvlink_strings.json` |
| Existing reg count from callgraph record byte +47 | MEDIUM | Offset inferred from decompiled `sub_45D180` analysis; consistent with EIATTR code 47 storage |
| Recursive resolution of cross-referenced section symbols | MEDIUM | Recursion visible in decompiled call graph; bounded by section count and flag bit 0x100000000000 |
| Propagation flag at ctx+95 for global-init semantics | MEDIUM | Reconstructed from decompiled bit-manipulation; offset consistent with linker context layout |
