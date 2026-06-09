# Extended Symbol Resolution

This page documents `sub_4411F0`, the symbol-to-output-section resolver invoked during the section copy phase of merge. Where [Symbol Resolution](symbol-resolution.md) covers storage and the 0xFFFF virtual-section indirection, and [Symbol Addition](symbol-addition.md) covers the insertion side, this companion focuses on the read path that translates an input-local symbol index into an output section index, walking every layer of indirection nvlink keeps for that purpose. The resolver is also the home of the `"found multiple section symbols for %s"` linear-scan fallback that fires when input sections have been folded into a single output section by [Section Merging](section-merging.md).

## `sub_4411F0` Overview

`sub_4411F0` (`elfw_copy_section` per the sweep, but functionally an extended symbol-to-section resolver) is a 482-line function that resolves a symbol index to its owning output section index, handling all layers of indirection. It is the most complex resolution function in the symbol subsystem:

```c
// sub_4411F0 -- resolve symbol to output section index (extended)
uint32_t resolve_symbol_to_section(elfw* ctx, int sym_index) {
    // Step 1: Get symbol record
    symbol_record* sym = elfw_get_symbol(ctx, sym_index);   // +344/+352
    uint16_t shndx = sym->st_shndx;
    
    // Step 2: Resolve extended section index if virtual
    if (shndx == 0xFFFF)
        shndx = resolve_extended_section(ctx, sym);
    
    // Step 3: Early exit for SHN_UNDEF
    if (shndx == 0)
        return 0;
    
    // Step 4: Validate via section descriptor array
    // (includes virtual section indirection at +368, +472)
    if (ctx->virtual_section_flag) {            // byte at +82
        uint32_t virt_entry = ctx->virt_map[shndx];    // +472
        if (virt_entry != 0) {
            uint32_t real_idx = ctx->virt_table[virt_entry];  // +368
            if (real_idx != shndx)
                fatal("secidx not virtual");
        }
    }
    
    // Step 5: Look up section name from section descriptor
    section_record* sec = dyn_array_get(ctx->sections, shndx);  // +360
    const char* sec_name = sec->name;           // offset +96 in section record
    if (!sec)
        fatal("section not found");
    
    // Step 6: Look up the section name in the output hash map
    int out_sec_idx = hash_map_lookup(ctx->name_map, sec_name);
    
    // Step 7: If found, remap through old-to-new tables
    if (ctx->pos_remap && out_sec_idx != 0) {
        out_sec_idx = remap_symbol_index(ctx, out_sec_idx);
    }
    
    // Step 8: Resolve the output section index through extended arrays
    uint16_t out_shndx = resolve_section_index_for(ctx, out_sec_idx);
    
    // Step 9: Compare source and output section indices
    // If they match, return the signed symbol index directly
    // If they differ, scan all positive symbols for a section symbol
    // (type == STT_SECTION, info & 0xF == 3) whose section matches
    if (source_shndx != out_shndx) {
        if (verbose)
            fprintf(stderr, "found multiple section symbols for %s\n", sec_name);
        for (int i = 0; i < dyn_array_count(ctx->pos_symbols); i++) {
            symbol_record* candidate = dyn_array_get(ctx->pos_symbols, i);
            uint16_t cand_sec = resolve_extended_section(ctx, candidate);
            uint16_t orig_sec = resolve_extended_section(ctx, sym);
            if (cand_sec == orig_sec) {
                // Skip: same section as the input symbol
                continue;
            }
            if ((candidate->st_info & 0xF) == STT_SECTION)
                return candidate->sym_index;     // offset +24
        }
    }
    
    return out_sec_idx;
}
```

This function is called from `merge_elf` (`sub_45E7D0`) during the section copy phase. When copying a section from an input ELF to the output ELF, relocations reference symbols by their input-local indices. This function translates those references to output-global indices by walking through: symbol record lookup, extended section resolution, remap table translation, name-based hash map lookup in the output, and finally a linear scan for section symbols when the section index has changed (indicating the section was merged into a different output section).

## Linear-Scan Fallback

The linear scan at the end (iterating all positive symbols) is a fallback for the case where multiple input sections map to the same output section. The diagnostic `"found multiple section symbols for %s"` is printed when this occurs, and the function returns the `sym_index` of the first `STT_SECTION` symbol whose resolved section matches the source symbol's resolved section.

> ⚡ **QUIRK — O(N) per relocation in the fallback**
> When section merging has collapsed several input sections into one output section, every relocation that targets such a section pays a full linear scan of `pos_symbols` to find the surviving section symbol. The scan re-invokes `resolve_extended_section` for both the candidate and the original on every iteration, so the worst case is O(N) per relocation against an N-symbol positive array. The fast path (`source_shndx == out_shndx`) short-circuits this entirely, which is why most relocations never see it.

## Cross-References

- [Symbol Resolution](symbol-resolution.md) — `sub_440350` and the 0xFFFF virtual-section indirection that this resolver chases through
- [Symbol Addition](symbol-addition.md) — populates the `pos_symbols`/`neg_symbols`/`merged_symbol_array` structures that this resolver reads
- [Section Merging](section-merging.md) — creates the multi-input-to-one-output mapping that drives the linear-scan fallback
- [Relocation Application Engine](relocation-engine.md) — the primary caller for input-to-output index translation
- [Merge Phase](../pipeline/merge.md) — `sub_45E7D0` section-copy loop that invokes this resolver
