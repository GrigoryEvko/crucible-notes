# Merge Phase

The merge phase is the heart of nvlink's linking pipeline. After all input files have been read, parsed, and (if needed) JIT-compiled, the linker iterates over every input cubin and merges its sections, symbols, and relocations into a single output ELF. This phase is implemented almost entirely by a single function -- `merge_elf` at `sub_45E7D0` -- which at 89 KB (2,838 lines of decompiled pseudocode, 450+ local variables) is the largest function in the linker core. It is called once per input object, in the order the objects appear on the command line (or were extracted from fatbins/archives).

The merge phase sits between the input-reading loop and the shared-memory layout phase. In `main()`, the timing checkpoint `sub_4279C0("merge")` is emitted immediately before the merge loop begins, and another checkpoint fires when the loop completes.

## Key Facts

| Property | Value |
|---|---|
| Primary function | `sub_45E7D0` (`merge_elf`) |
| Address | `0x45E7D0` |
| Size | 89,156 bytes (~89 KB) |
| Decompiled lines | 2,838 |
| Local variables | 450+ |
| Callees | 222 distinct functions |
| Called by | `main()` in a per-object loop |
| Timing checkpoint | `sub_4279C0("merge")` before/after the merge loop |
| Verbose diagnostic flag | `ctx+64` bit 4 (the `-v` verbose flag) |
| Thread safety | Uses `pthread_mutex` for concurrent merge in split-compile mode |

## Invocation Context

In `main()`, the merge loop looks approximately like this:

```c
sub_4279C0("merge");                     // timing: start merge phase

for (obj = input_list_head; obj; obj = obj->next) {
    if (is_cudadevrt(obj))
        continue;                        // skip cudadevrt in certain LTO modes
    int err = sub_45E7D0(ctx, obj->elf_data, obj->name, ...);
    if (err)
        sub_467460(&fatal_error, "merge_elf failed", ...);
}

sub_4279C0("merge");                     // timing: end merge phase
```

Before merge begins, `main()` has already:
1. Parsed all CLI options (`sub_427AE0`)
2. Created the output ELF wrapper via `sub_4438F0` (elfw\_create) with sections `.note.nv.cuinfo`, `.note.nv.tkinfo`, `.shstrtab`, `.strtab`, `.symtab`
3. Read, identified, and loaded all input files into memory
4. Run LTO compilation (if `-lto` was specified), converting NVVM IR to cubin objects
5. Appended all resulting objects to the input list

### cudadevrt Handling

The CUDA device runtime library (`libcudadevrt`) receives special treatment. When full LTO is active (all translation units have IR), `main()` prints `"LTO on everything so remove libcudadevrt from list"` and skips the cudadevrt object entirely -- the LTO compilation subsumes its functionality. The detection uses `sub_4448C0`, which iterates the output ELF's symbol table looking for undefined function symbols. If all function symbols are resolved (or are `__cuda_syscall` intrinsics, or match the syscall constant table checked by `sub_449BE0`), cudadevrt is deemed unnecessary.

### Verbose-Keep Command Reconstruction

When the `-vkeep` (verbose-keep) flag is set (`byte_2A5F29B`), nvlink prints the equivalent command that would reproduce the merge step. This appears in the fatbin extraction code (`sub_42AF40`) as:

```
nvlink -extract <fatbin> -m<bits> -arch=<arch> -o <output>
```

and for LTO modules:

```
nvlink -lto-add-module <file>.nvvm
```

This is useful for debugging: running nvlink with `-vkeep` emits a complete trace of every operation, including the exact command lines for embedded ptxas invocations.

## merge\_elf Internal Architecture

`sub_45E7D0` processes a single input cubin ELF. It builds temporary mapping tables, then iterates the input's sections and symbols to merge them into the output ELF. The function has five major phases:

### Phase 1: Input ELF Header Parsing

The function begins by extracting the ELF file header, symbol table header, and string table from the input object. It supports both 32-bit and 64-bit CUDA ELF formats:

```c
// Allocate merge context (16-byte record + 80-byte section map context)
merge_record = arena_alloc(16);   // linked into ctx->input_list
merge_ctx    = arena_alloc(80);   // maps, counters, flags

merge_ctx->is_64bit = (obj->class == ELFCLASS64);  // byte at offset 56

if (merge_ctx->is_64bit) {
    efh = sub_448360(input_elf);           // get ELF64 file header
    symsec = sub_4484F0(input_elf, SHT_SYMTAB);  // find .symtab
    strsec = sub_448370(input_elf, symsec->sh_link);
    merge_ctx->num_sections = sub_448730(efh);     // e_shnum
    merge_ctx->num_symbols  = symsec->sh_size / symsec->sh_entsize;
} else {
    efh = sub_46B590(input_elf);           // get ELF32 file header
    symsec = sub_46B700(input_elf, SHT_SYMTAB);
    strsec = sub_46B5A0(input_elf, symsec->e32_link);
    merge_ctx->num_sections = sub_46B810(efh);
    merge_ctx->num_symbols  = symsec->e32_size / symsec->e32_entsize;
}

// Pointers into the raw input buffer
merge_ctx->strings  = input_elf + strsec->sh_offset;
merge_ctx->symbols  = input_elf + symsec->sh_offset;
merge_ctx->sections = input_elf + efh->e_shoff;

// EWP (extended warp) flag: e_type == 0xFF00
merge_ctx->is_ewp = (efh->e_type == 0xFF00);
```

Fatal errors `"efh not found"`, `"symsec not found"`, and `"strsec not found"` are emitted via `sub_467460` if any of these lookups fail.

The ISA version is extracted from the ELF header (`sub_43E3C0` / `sub_43E420`) and the minimum ISA version across all inputs is tracked at `ctx+200`. A flag at `ctx+94` is set if any input has ISA version > 0x45 (69), indicating a post-Volta architecture.

### Phase 2: Mapping Table Allocation

Four mapping arrays are allocated -- these translate input-local indices to output-global indices:

| Array | Element size | Count | Description |
|---|---|---|---|
| `map_symbol_index` | 4 bytes (uint32) | num\_symbols + 1 | Input symbol index -> output symbol index |
| `map_section_index` | 4 bytes (uint32) | num\_sections + 1 | Input section index -> output section index |
| `map_section_offset` | 8 bytes (uint64) | num\_sections + 1 | Base offset within merged output section |
| `weak_processed` | 1 byte (bool) | num\_symbols + 1 | Tracks whether a weak symbol has been resolved |

All arrays are zero-initialized. Zero in a mapping slot means "not yet mapped" or "section was skipped."

### Phase 3: Symbol Pass (Weak Resolution)

Before processing sections, `merge_elf` makes a first pass over all symbols to handle **weak function definitions**. This is a dedicated pre-pass -- it runs to completion before Phase 4 section iteration begins. The loop walks the input ELF's symbol table and calls `sub_45D180` (`merge_weak_function`, 26 KB) for every symbol whose low nibble of `st_info` equals 2 (the `STB_WEAK` binding code):

```c
// merge_elf Phase 3 (decompiled lines 762-800)
for (sym_idx = 0; sym_idx < merge_ctx->num_symbols; sym_idx++) {
    // Read symbol entry -- layout depends on 64-bit vs 32-bit ELF
    if (merge_ctx->is_64bit) {
        sym = (ELF64_Sym *)(merge_ctx->symbols + 24 * sym_idx);
        // 64-bit sym: [st_name:4][st_info:1][st_other:1][st_shndx:2][st_value:8][st_size:8]
    } else {
        sym = (ELF32_Sym *)(merge_ctx->symbols + 16 * sym_idx);
        // 32-bit sym: [st_name:4][st_value:4][st_size:4][st_info:1][st_other:1][st_shndx:2]
    }

    if ((sym->st_info & 0xF) == 2) {   // STB_WEAK binding
        map_symbol_index[sym_idx] = merge_weak_function(
            ctx, input_elf, merge_ctx, sym_idx,
            sym->st_info,       // byte: low nibble = type, high nibble = binding
            sym->st_other,      // visibility byte
            sym->st_shndx,      // section index (packed with flags in upper bits)
            sym->st_value,      // 64-bit value / 32-bit value+size pair
            sym->st_size
        );
    }
}
```

Note the filter condition: `(st_info & 0xF) == 2` checks only the low nibble (symbol binding), not the high nibble (symbol type). This means the weak pass processes all weak symbols regardless of whether they are `STT_FUNC`, `STT_OBJECT`, or `STT_SECTION` -- though in practice CUDA cubins only emit weak function symbols (`STT_FUNC`, type 2).

The return value of `merge_weak_function` is stored directly into `map_symbol_index[sym_idx]`, providing the output-global symbol index that replaces this input-local index in all subsequent translation.

#### merge\_weak\_function Control Flow

`merge_weak_function` (`sub_45D180`) is the second-largest function in the merge subsystem at 26,816 bytes (913 decompiled lines, 235+ local variables). It implements a complex decision tree that handles first-seen symbols, global-over-weak replacement, and weak-over-weak comparison. The complete control flow:

```
merge_weak_function(ctx, input_elf, merge_ctx, sym_idx, st_info, ...)
│
├── Extract symbol name from input string table
├── Set global-init flag at ctx+95 if packed_flags & 0x80000000000
│
├── Lookup symbol name in output ELF (sub_4411B0)
│   │
│   ├── NOT FOUND in output (first occurrence of this symbol name)
│   │   ├── Check if map_symbol_index[sym_idx] already set
│   │   │   └── If set: return existing mapping (already resolved by recursion)
│   │   │
│   │   ├── Has section index (st_shndx != 0)?
│   │   │   ├── YES: Look up section in map_section_index
│   │   │   │   ├── If section already mapped: use existing output section
│   │   │   │   └── If not mapped AND references a "common section" (bits 24-47):
│   │   │   │       └── RECURSIVE CALL to merge_weak_function for the common section
│   │   │   │           (guarded by flag bit 0x100000000000 to prevent infinite recursion)
│   │   │   │
│   │   │   ├── Create output symbol via sub_440740 (elfw_add_symbol)
│   │   │   ├── Set input_index field to (list_count(ctx+512) - 1)
│   │   │   ├── Copy section data via sub_432B10 (merge_overlapping_global_data)
│   │   │   ├── Update callgraph record: ISA bits at +8, register count at +47
│   │   │   └── Return new output symbol index
│   │   │
│   │   └── NO section (st_shndx == 0): create undefined weak symbol
│   │       ├── Create via sub_440740, set input_index
│   │       └── Return new output symbol index
│   │
│   └── FOUND in output (duplicate definition exists)
│       │
│       ├── Check for visibility conflict (BYTE5 ^ existing_byte5) & 0x10
│       │   └── If mismatched: emit diagnostic via unk_2A5B8F0
│       │
│       ├── Existing symbol has binding 0 (STB_LOCAL)?
│       │   └── Treat as first-occurrence (same path as NOT FOUND + section)
│       │
│       ├── map_symbol_index[sym_idx] already set?
│       │   └── Return existing mapping (already resolved)
│       │
│       ├── Read existing symbol record (sub_440590)
│       ├── Fetch input list entries for PTX version comparison:
│       │   │   v244 = list_entry(ctx+512, list_count - 1)  // incoming input
│       │   │   v243 = list_entry(ctx+512, existing.input_index)  // existing
│       │
│       ├── Verify existing binding is STB_WEAK (binding >> 4 == 2)
│       │   └── If NOT weak: emit unk_2A5BA00 error (type conflict)
│       │
│       ├── Check existing section assignment (sub_440350)
│       │   │
│       │   ├── Existing has NO section: assign section, copy data, set ISA/regcount
│       │   │   └── Propagate address-taken flag (bit 3 of st_other)
│       │   │
│       │   └── Existing HAS section: enter replacement decision tree
│       │       │
│       │       ├── INCOMING is STB_GLOBAL (binding >> 4 == 1)?
│       │       │   ├── EXISTING is also STB_GLOBAL?
│       │       │   │   └── Emit multiple-definition error (unk_2A5BA10)
│       │       │   │       then fall through to check if existing is weak
│       │       │   ├── EXISTING is STB_WEAK?
│       │       │   │   └── GOTO LABEL_82: UNCONDITIONAL REPLACEMENT
│       │       │   │       Verbose: "replace weak function %s"
│       │       │   │       (standard ELF global-overrides-weak semantics)
│       │       │   └── Otherwise: skip to LABEL_63 (keep existing)
│       │       │
│       │       ├── INCOMING is STB_WEAK (binding >> 4 == 2)?
│       │       │   └── Enter three-tier comparison (see below)
│       │       │
│       │       └── Other binding: skip to LABEL_63
│       │
│       └── LABEL_63: Map section index, return output symbol index
```

#### The Three-Tier Weak-over-Weak Decision Tree

When both the incoming and existing definitions are `STB_WEAK`, the linker applies CUDA's register-aware selection policy. This is the core of `merge_weak_function` (decompiled lines 774-906):

```
WEAK-OVER-WEAK COMPARISON:
│
├── Step 1: Extract register counts
│   │
│   ├── INCOMING register count (v242):
│   │   ├── Read from HIDWORD(n[1]) -- extracted from input symbol's packed field
│   │   │   (this is the high byte of the 64-bit section header word, encoding
│   │   │    the register count cached by ptxas in the ELF metadata)
│   │   │
│   │   └── If v242 == 0 (no cached register count):
│   │       ├── Verbose: "no new register count found for %s, checking .nv.info"
│   │       ├── Scan ALL SHT_CUDA_INFO (0x70000000) sections in input ELF:
│   │       │   for each section with sh_type == 0x70000000 and sh_size > 0:
│   │       │       walk TLV records at 4-byte granularity:
│   │       │           if format_byte != 0x04: skip 4 bytes (non-indexed record)
│   │       │           if format_byte == 0x04 AND attr_code == 0x2F (47):
│   │       │               read sym_index from payload[0]
│   │       │               if sym_index == target: reg_count = payload[1]; FOUND
│   │       │           else: advance by 4 + payload_size
│   │       └── If NOT FOUND: fatal_error("no such new reg count")
│   │
│   └── EXISTING register count (v250[0]):
│       ├── Read from callgraph/nvinfo record byte at offset +47 (via sub_442270)
│       │   (this was populated when the first definition was merged)
│       │
│       └── If v250[0] == 0 (no cached register count):
│           ├── Verbose: "no original register count found for %s, checking .nv.info"
│           ├── Scan output's nvinfo linked list (ctx+392):
│           │   for each entry in the list (via sub_464A80 iterator):
│           │       if entry.attr_code == 47 (0x2F):
│           │           if entry.payload[0] == existing_symbol_index:
│           │               reg_count = entry.payload[1]; FOUND
│           └── If NOT FOUND: fatal_error("no such original reg count")
│
├── Step 2: Compare register counts
│   │
│   ├── new_reg_count < existing_reg_count?
│   │   └── YES: REPLACE (fewer registers = higher occupancy)
│   │       Verbose: "replace weak function %s with weak that uses fewer registers"
│   │       → GOTO LABEL_82 (replacement + cleanup)
│   │
│   ├── new_reg_count == existing_reg_count?
│   │   └── Fall through to PTX version comparison
│   │
│   └── new_reg_count > existing_reg_count?
│       └── KEEP existing, set v114 = 0
│           → GOTO LABEL_173 (mark weak_processed, return existing)
│
├── Step 3: Compare PTX versions (only when register counts are equal)
│   │
│   │   // v244 = incoming input record, v243 = existing input record
│   │   // PTX version is at offset +8 in each input record
│   │
│   ├── incoming_ptx_version > existing_ptx_version?
│   │   └── YES: REPLACE (newer PTX compiler = better code quality)
│   │       Verbose: "replace weak function %s with weak from newer PTX"
│   │       → GOTO LABEL_82 (replacement + cleanup)
│   │
│   └── incoming_ptx_version <= existing_ptx_version?
│       └── KEEP existing (first definition wins)
│           → GOTO LABEL_173
│
└── Step 4: Address-taken flag propagation (always, before returning)
    │
    ├── If incoming has address-taken (packed_flags & 0x80000000000):
    │   └── Set bit 3 of existing symbol's st_other: ptr[5] |= 0x08
    │
    └── If existing has address-taken (ptr[5] & 0x08) but incoming does not:
        └── Propagate to incoming: BYTE5(a7) |= 0x08
```

#### LABEL\_82: The Replacement Path

When the decision tree selects the incoming definition as the winner (via register count, PTX version, or global-over-weak), control flows to LABEL\_82 (decompiled lines 562-767). This path performs:

1. **Section data swap**: Looks up the existing definition's section via `sub_440350`, retrieves its callgraph record via `sub_442270`, then destroys the old section data (releases relocation chain at record+72, zeroes size at record+32) and copies the incoming section data via `sub_432B10`.

2. **ISA and register count update**: Updates the callgraph record's ISA bits at offset +8 (masked with `0xF80FFFFF | (isa_bits << 20) & 0x7F00000`) and the register count byte at offset +47.

3. **Symbol record update**: Overwrites the existing symbol's `st_value` and `st_size` with the incoming values. Sets the `input_index` field (offset +40) to `list_count(ctx+512) - 1`.

4. **Four cleanup passes** (see below).

#### Post-Replacement Cleanup Passes

When a weak function is replaced at LABEL\_82, four cleanup passes remove the old definition's metadata from the output ELF:

**Pass 1 -- Relocation nullification** (lines 608-624): Finds the old function's `SHT_REL` (type 9) and `SHT_RELA` (type 4) sections via `sub_442760`, then walks the output's relocation linked list at `ctx+376`. For each relocation entry whose `target_section_idx` (offset +24) matches either section, the `reloc_info` field (offset +8) is zeroed. Verbose: `"remove weak reloc"`.

**Pass 2 -- Debug relocation removal** (lines 625-663): For each of three debug section names (`.debug_line`, `.nv_debug_line_sass`, `.debug_frame`), looks up the section's associated `SHT_REL` and `SHT_RELA` sections. Relocations targeting these are nullified with the same zero-write technique. Verbose: `"remove weak reloc from debug"`.

**Pass 3 -- nvinfo entry removal** (lines 671-734): Walks the output's nvinfo linked list at `ctx+392`. Two sub-cases:
- **Direct match**: If the entry's function reference (offset +4) matches the old section ID, its `attr_code` (offset +1) is zeroed. Verbose: `"remove weak nvinfo"`.
- **Frame-size-class match**: If the entry's `attr_code` is <= 47 and the 64-bit bitmask `0x800800020000` has the corresponding bit set (true for codes 17, 35, 47 -- FRAME\_SIZE, CRS\_STACK\_SIZE, REGCOUNT), and the payload's first DWORD matches the old symbol index, the `attr_code` is zeroed. Verbose: `"remove weak frame_size"`.

**Pass 4 -- OCG constant section removal** (lines 736-766): Constructs the OCG section name as `<module>.<function>` using `sprintf`, where the module name comes from `sub_4401F0` (reading from the vtable at `ctx+488` offset 136). If `sub_4411D0` finds this section in the output, its callgraph record's size (offset +32) is zeroed, all relocation chain entries are freed via `sub_431000`, and the chain pointer (offset +72) is cleared via `sub_464520`. Verbose: `"remove weak ocg constants"`.

#### weak\_processed Flag: Bridging Phase 3 and Phase 4

The result of Phase 3 feeds directly into Phase 4 through two mechanisms:

1. **map\_symbol\_index**: The output symbol index returned by `merge_weak_function` is stored in `map_symbol_index[sym_idx]`. Phase 4 uses this to translate symbol references when copying sections, relocations, and nvinfo entries.

2. **weak\_processed array** (`merge_ctx+64`, stored as `v19[8]` in the decompiled code): A boolean byte array indexed by the input symbol index. Phase 4 sets `weak_processed[sym_idx] = 1` when processing a weak symbol during section iteration (decompiled line 1406). Before processing any weak symbol's constant bank data, Phase 4 checks this array -- if the byte is set, it emits `"weak %s already processed"` (line 1076) and skips to the next symbol. The check at line 1177 also prevents section-index mapping for already-processed symbols: `if (st_shndx != 0 && st_shndx != 0xFFF2 && !weak_processed[sym_idx])`.

This two-array system ensures that: (a) Phase 3 resolves all weak conflicts and records winners in `map_symbol_index`, and (b) Phase 4 does not re-process or duplicate data for symbols that Phase 3 already handled. The `.nv.info` merge in Phase 5 also respects `weak_processed` -- EIATTR attribute codes 17 (`FRAME_SIZE`), 35 (`CRS_STACK_SIZE`), 47 (`REGCOUNT`), and 59 are silently skipped for weak-processed symbols.

### Phase 4: Section Iteration

The second symbol/section pass is the bulk of the function. It iterates every symbol in the input ELF and processes it according to its section type and binding:

```c
for (sym_idx = 0; sym_idx < merge_ctx->num_symbols; sym_idx++) {
    sym = get_symbol(merge_ctx, sym_idx);
    name = merge_ctx->strings + sym->st_name;

    // Classify the section type
    sh_type = get_section_type(sym->st_shndx);

    // Reclassify SHT_NOBITS (8) by section name prefix
    if (sh_type == SHT_NOBITS) {
        if (memcmp(name, ".nv.global", 10) == 0)
            sh_type = SHT_CUDA_GLOBAL;          // 0x70000007
        else if (memcmp(name, ".nv.shared.", 11) == 0)
            sh_type = SHT_CUDA_SHARED;          // 0x7000000A
        else if (memcmp(name, ".nv.shared.reserved.", 20) == 0)
            sh_type = SHT_CUDA_SHARED_RESERVED; // 0x70000015
        else if (memcmp(name, ".nv.local.", 10) == 0)
            sh_type = SHT_CUDA_LOCAL;           // 0x70000009
    }
    // SHT_PROGBITS (1) with .nv.constant prefix -> bank number
    if (sh_type == SHT_PROGBITS && memcmp(name, ".nv.constant", 12) == 0)
        sh_type = strtol(name + 12, NULL, 10) + 0x70000064;
    // SHT_PROGBITS with .nv.global.init prefix
    if (sh_type == SHT_PROGBITS && memcmp(name, ".nv.global.init", 15) == 0)
        sh_type = SHT_CUDA_GLOBAL_INIT;        // 0x70000008

    // Dispatch by section type and symbol binding...
}
```

#### CUDA Section Type Constants

The merge function uses NVIDIA's proprietary ELF section types (`SHT_LOPROC` = `0x70000000` and up):

| Constant (decimal) | Hex | Symbolic name | Section pattern |
|---|---|---|---|
| 1879048192 | `0x70000000` | `SHT_CUDA_INFO` | `.nv.info`, `.nv.info.*` |
| 1879048198 | `0x70000006` | `SHT_CUDA_CONSTANT` | `.nv.constant` (base) |
| 1879048199 | `0x70000007` | `SHT_CUDA_GLOBAL` | `.nv.global` |
| 1879048200 | `0x70000008` | `SHT_CUDA_GLOBAL_INIT` | `.nv.global.init` |
| 1879048201 | `0x70000009` | `SHT_CUDA_LOCAL` | `.nv.local.*` |
| 1879048202 | `0x7000000A` | `SHT_CUDA_SHARED` | `.nv.shared.*` |
| 1879048203 | `0x7000000B` | `SHT_CUDA_RELOCINFO` | `.nv.rel.action` |
| 1879048206 | `0x7000000E` | `SHT_CUDA_UFT` | `.nv.uft` |
| 1879048209 | `0x70000011` | `SHT_CUDA_UFT_ENTRY` | `.nv.uft.entry` |
| 1879048210 | `0x70000012` | `SHT_CUDA_UDT` | `.nv.udt` |
| 1879048212 | `0x70000014` | `SHT_CUDA_UDT_ENTRY` | `.nv.udt.entry` |
| 1879048213 | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | `.nv.shared.reserved.*` |
| 1879048292 | `0x70000064` | `SHT_CUDA_CONSTANT0` | `.nv.constant0` (bank 0) |
| 1879048293+ | `0x70000065`+ | `SHT_CUDA_CONSTANT_N` | `.nv.constantN` (bank N = type - 0x70000064) |
| 1879048326 | `0x70000086` | `SHT_CUDA_COMPAT` | `.nv.compat` |

Constant bank numbers are encoded as `SHT_CUDA_CONSTANT0 + bank_number`. The bank number is parsed from the section name suffix: `strtol(name + 12, NULL, 10)`.

#### Section Dispatch Logic

Each symbol/section pair is handled according to its type and binding. The major cases:

**Local symbols (STB\_LOCAL, binding byte >> 4 == 0)**:

Sections with no section index (`st_shndx == 0` or `SHN_UNDEF`) and section name `.nv.ptx.const0.size` are looked up in the output ELF. If already present, the sizes are compared and conflicts are diagnosed.

Sections with data (`sh_size > 0`) and a known NVIDIA section name are created in the output via `sub_440BE0` (elfw\_add\_section\_with\_data). If the section name matches a known `.nv.*` pattern (checked by `sub_444AD0`), special flags are applied.

**Global symbols (STB\_GLOBAL, binding byte >> 4 == 1)**:

The section is looked up in the output ELF by name via `sub_4411B0`. If it does not exist, a new section is created. If it already exists, the linker checks for:

- **Size conflicts**: If the existing section has a different size, a diagnostic is emitted via `sub_467460`. Global init data (`.nv.global.init`) replacing a common symbol prints `"global.init replaces common for %s"`.
- **Common symbols** (section index `SHN_COMMON` = `0xFFF2`): The larger size wins. `"increase size of common %s"` is printed when the common grows.
- **Multiple definition errors**: `sub_467460` with the `unk_2A5B9D0` error record (multiple definitions).

**Weak symbols (STB\_WEAK, binding byte >> 4 == 2)**:

Already resolved in Phase 3. The `weak_processed` flag prevents re-processing. If a weak symbol arrives that was already handled, verbose mode prints `"weak %s already processed"`.

**Shared memory sections** (binding `0x40`, or `.nv.shared.*` prefix):

These are allocated as BSS (no data) via `sub_437BB0`. The function takes the section flags, alignment, and size, and creates a placeholder in the output. Shared memory layout happens later in `sub_439830`.

**Reserved shared memory sections** (binding `0xA0`, or `__nv_reservedSMEM_*`):

Special SMEM reservations for hardware features. The function detects:

- `__nv_reservedSMEM_tcgen05_partition` -- tcgen05 tensor core partition (priority 2)
- `__nv_reservedSMEM_allocation_phase` -- allocation phase (priority 1)
- `__nv_reservedSMEM_allocation_mask` -- allocation mask (priority 1)
- `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier` -- tmem pipeline mbarrier (priority 1)
- `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier_parity` -- tmem parity (priority 1)

A conflict check at `ctx+664` ensures all input objects agree on the reserved SMEM partition type. Mismatches produce a fatal error through `sub_467460` with `unk_2A5B8C0`.

### Phase 5: Section Header (`.nv.info`) and Relocation Processing

After processing symbols, the function iterates the input's section headers directly. Three major section types receive special handling:

#### `.nv.info` Sections (`SHT_CUDA_INFO`, `0x70000000`)

The `.nv.info` section contains CUDA-specific per-function metadata: register counts, stack sizes, max thread counts, barrier counts, CRS stack sizes, parameter sizes, and more. Each entry is a compact TLV (type-length-value) record.

The merge function processes `.nv.info` by iterating entries and translating symbol/section indices through the mapping tables. The attribute byte (offset +1 in each 4-byte record) determines the type:

| Attribute | Code | Action |
|---|---|---|
| Frame size, reg count, min stack, etc. | 2, 6, 7, 8, 9, 18, 19, 20, 23, 38, 69 | Translate `sym_index` through `map_symbol_index` |
| CRS stack size | 10 | Translate symbol index, resolve section mapping |
| Sampler/surface array | 15 | Translate array of symbol indices |
| Weak-related attributes | 17, 35, 47, 59 | Skip if weak symbol already processed |
| Max thread count | 55 | Validate against `ctx->maxrregcount` limit (`ctx+628`) |
| Per-function reloc set | 79 | Translate and detect SMEM partition conflicts |

Each translated entry is added to the output via `sub_4508F0`.

For entries referencing weak symbols that have already been processed (checked via `weak_processed` array), the entry is silently skipped -- the winning weak definition's `.nv.info` data takes precedence.

#### `.nv.compat` Sections (`SHT_CUDA_COMPAT`, `0x70000086`)

Compatibility attribute sections are parsed byte-by-byte. Each attribute has a type code and a value. The merge function calls `sub_451920` or `sub_451BA0` to register each attribute in the output context. Missing attributes are filled with defaults:

| Attribute code | Default | Setter |
|---|---|---|
| 2 (ISA\_CLASS) | 0 | `sub_451920(ctx, 2, 0)` |
| 3 | 3 | `sub_451920(ctx, 3, 3)` |
| 5 | 0 | `sub_451920(ctx, 5, 0)` |
| 6 | 1 | `sub_451920(ctx, 6, 1)` |
| 7 | 0x100 | `sub_451BA0(ctx, 7, 0x100)` |

Unknown attribute codes produce verbose trace: `"unknown .nv.compat attribute (%x) encoutered."` (note: the typo "encoutered" is in the binary).

An ISA\_CLASS validation check fires for Mercury (sm100+) with ISA version > 0x7F: the linker issues an error via `unk_2A5B900`.

#### Relocation Sections (`SHT_REL` / `SHT_RELA`, types 4 and 9)

Relocation entries are copied from the input and translated:

1. The section index is looked up in `map_section_index` to find the output section.
2. The symbol index is translated through `map_symbol_index`.
3. For `SHT_RELA` (type 4), addends are also present; for `SHT_REL` (type 9), they are not.
4. Relocations referencing weak symbols that were already processed are skipped, with verbose trace: `"weak sym %d already relocated"`, `"remove reloc for subsequent weak %d"`.
5. Debug-related sections (`.debug_line`, `.nv_debug_line_sass`, `.debug_frame`) are checked specially -- weak relocations into these are silently dropped rather than producing errors.

The actual relocation entry is added to the output via `sub_469790` (SHT\_RELA) or `sub_4698A0` (SHT\_REL). A section copy helper `sub_469230` maps the relocation target section.

#### `.nv.callgraph` Sections (`0x70000001`)

Callgraph entries are 8-byte records. A leading record with a zero caller carries a type marker in its high 32 bits (`-1`, `-2`, `-3`, `-4`); subsequent records inherit that type until the next marker. Each non-marker record's symbol index (caller, low 32 bits) is translated through `map_symbol_index`, and the trailing 32 bits encode either a callee section index or a name-string offset depending on the active type. The four edge builders are:

- type `-1` -- `sub_44B9F0(ctx, caller_sec, callee_sec)` -- appends to the caller node's `callee_list` (entry offset `+16`), keyed by callee section id; this is the standard direct-call edge.
- type `-2` -- `sub_44BA60(ctx, sec, name_attr)` -- interns `name_attr` into the string table at `ctx+288` (via `sub_4405C0`) and stores the resulting **string-table offset** at entry offset `+4` (i.e. the symbol's interned name token, not a callgraph index), then stamps `address_taken=1` at offset `+50`. This name-key is later matched against `-3` alt_call entries (in `sub_44C030`) to resolve by-name address-taken references back to this node.
- type `-3` -- `sub_44BAA0(ctx, sec, name_attr)` -- appends to the caller's `alt_call_list` (entry offset `+8`), keyed by interned string index; this carries indirect or external-by-name callees.
- type `-4` -- `sub_44BF90(ctx, caller_sec, callee_sec)` -- appends to the caller's `fn_ptr_edge_list` (entry offset `+32`) and stamps `address_taken=1` on the callee node; this is the function-pointer / address-taken edge.

Entries referencing weak-processed symbols are skipped.

#### `.nv.callgraph.info` Sections (`0x70000002`)

Per-function callgraph information. Each entry references a function symbol and a name string. The function calls `sub_44BAE0` to register the info, with validation through `sub_4447B0`. Failures produce a fatal error via `sub_467460` with `unk_2A5B9C0`.

#### Debug Sections (`SHT_CUDA_FUNCDATA`, `0x70000004`)

Variable-length records containing function debug metadata. Each record has a symbol index, a string pointer, and an array of key-value pairs. String entries (key type 3) are translated via `sub_43D690` (debug string interning), and the record is registered via `sub_43D6B0`.

### Phase 6: Cleanup and Diagnostic Dump

After all sections are processed, merge\_elf performs cleanup:

1. **Verbose diagnostic dump** (if `-v` flag set at `ctx+64` bit 4):
   - Prints `map_section_index[%d] = %d, offset = %lld` for every mapped section
   - Prints `map_symbol_index[%d] = %d` for every mapped symbol
   - Calls `sub_4478F0` (elfw\_dump\_structure) to dump the entire output ELF state

2. **Memory cleanup**: Frees all four mapping arrays and the linked list of deferred-init data via `sub_431000` (arena\_free) and `sub_464520` (list\_destroy).

## Phase Order Inside Merge

Before walking through a concrete example, it helps to enumerate the sub-steps `merge_elf` performs for each input object, in execution order. Each input cubin is processed by a single top-level call to `sub_45E7D0` that traverses this list:

1. **Register input in driver list** (`sub_464C30` on `ctx+512`) -- append the input record so later callers can recover the source cubin name, PTX version, ISA bits, and input index. The input's list position becomes its `input_index` for all symbols created from this cubin.
2. **Parse ELF header** -- `sub_448360`/`sub_46B590` to get the file header, `sub_4484F0`/`sub_46B700` to find `.symtab`, `sub_448370`/`sub_46B5A0` to find the linked string table. Compute `num_sections`, `num_symbols`, `is_64bit`, `is_ewp`, and cache pointers to the raw section/symbol/string buffers inside the 80-byte merge context.
3. **Allocate four mapping arrays** (lines 706-752) via `sub_4307C0` -- `map_symbol_index`, `map_section_index`, `map_section_offset`, `weak_processed`, all zero-initialised.
4. **Compute ISA/Mercury flag masks** (lines 753-761) -- `v535` (input compatibility flag) and `v537` (output compatibility flag) gate Mercury section skipping later.
5. **Weak pre-pass** (lines 762-800) -- iterate symbols; for every entry whose `(st_info & 0xF) == 2`, call `sub_45D180` (`merge_weak_function`) and store the returned output symbol index in `map_symbol_index[sym_idx]`.
6. **Main symbol/section pass** (lines 801-1600+) -- iterate symbols again; dispatch each by `st_info` binding (local/global/weak), by `st_other & 0xE0` flags (0x40 = shared, 0x80 = constant, 0xA0 = reserved SMEM), and by reclassified section type. This is where the bulk of section creation (`sub_441AC0`), symbol creation (`sub_440740`/`sub_442CA0`), and constant-bank data merge (`sub_438640`) happens.
7. **Section header pass** -- walk the input's section headers directly to handle `.nv.info` (`SHT_CUDA_INFO`, 0x70000000), `.nv.compat` (`SHT_CUDA_COMPAT`, 0x70000086), `.nv.callgraph` (0x70000001), `.nv.callgraph.info` (0x70000002), debug funcdata (0x70000004), and relocation sections (`SHT_REL`, `SHT_RELA`). Each entry is translated through `map_symbol_index` and `map_section_index` and then appended to the output via `sub_4508F0`, `sub_44B9F0`, `sub_469790`, or `sub_4698A0`.
8. **Verbose diagnostic dump** (conditional on `-v`) -- print the mapping arrays and call `sub_4478F0` (`elfw_dump_structure`).
9. **Cleanup** -- free the four mapping arrays and any deferred linked lists via `sub_431000` (`arena_free`) and `sub_464520` (`list_destroy`). Return to `main()`'s merge loop for the next input.

When `main()` advances to the next input, the output ELF state (`ctx`) now contains the cumulative symbols, sections, relocations, and metadata contributed by every previous object. The next call to `sub_45E7D0` sees that state and merges on top of it.

## Worked Example: Merging Two Cubin Inputs

This section walks through merging two small cubins on the nvlink command line:

```
nvlink -arch=sm_90 input1.cubin input2.cubin -o merged.cubin
```

### Inputs

**`input1.cubin`** (ELF64, `e_machine = EM_CUDA`, `e_flags` encoding sm\_90):

| Input section | Type | Size | Content |
|---|---|---|---|
| `[1] .text.kernel_a` | `SHT_PROGBITS` (1) | 384 B | SASS for `kernel_a` |
| `[2] .rela.text.kernel_a` | `SHT_RELA` (4) | 48 B | 2 relocations (call target = `device_fn`, constant load) |
| `[3] .nv.info` | `0x70000000` | 92 B | EIATTR records for `kernel_a`: REGCOUNT=24, MAX\_THREADS=256, FRAME\_SIZE=0 |
| `[4] .nv.info.kernel_a` | `0x70000000` | 32 B | Per-function nvinfo with PARAM\_CBANK descriptor |
| `[5] .nv.constant0.kernel_a` | `SHT_PROGBITS` (1) | 352 B | Kernel parameter bank (bank 0, kernel-local) |
| `[6] .nv.callgraph` | `0x70000001` | 16 B | One edge: `kernel_a` -> `device_fn` |
| `[7] .symtab` | 2 | 120 B | 5 symbols |
| `[8] .strtab` | 3 | 64 B | Name strings |

**`input1` symbol table**:

| idx | Name | `st_info` | `st_shndx` | Binding |
|---|---|---|---|---|
| 0 | (null) | 0x00 | 0 | -- |
| 1 | `.text.kernel_a` | 0x03 | 1 | `STT_SECTION`, `STB_LOCAL` |
| 2 | `.nv.constant0.kernel_a` | 0x03 | 5 | `STT_SECTION`, `STB_LOCAL` |
| 3 | `kernel_a` | 0x12 | 1 | `STT_FUNC`, `STB_GLOBAL` |
| 4 | `device_fn` | 0x10 | **0** | `STT_NOTYPE`, `STB_GLOBAL`, **undefined** |

The relocation at `[2] .rela.text.kernel_a + 0x00` is `R_CUDA_ABS32_LO_20` with `r_sym = 4` (the undefined `device_fn` reference) and `r_addend = 0`.

**`input2.cubin`** (ELF64, sm\_90):

| Input section | Type | Size | Content |
|---|---|---|---|
| `[1] .text.device_fn` | `SHT_PROGBITS` (1) | 256 B | SASS for `device_fn` |
| `[2] .nv.constant2` | `SHT_PROGBITS` (1) | 64 B | Module-level constant data (bank 2, shared) |
| `[3] .nv.info` | `0x70000000` | 60 B | REGCOUNT=16 for `device_fn`, FRAME\_SIZE=0 |
| `[4] .nv.info.device_fn` | `0x70000000` | 16 B | Per-function nvinfo |
| `[5] .symtab` | 2 | 96 B | 4 symbols |
| `[6] .strtab` | 3 | 48 B | Name strings |

**`input2` symbol table**:

| idx | Name | `st_info` | `st_shndx` | Binding |
|---|---|---|---|---|
| 0 | (null) | 0x00 | 0 | -- |
| 1 | `.text.device_fn` | 0x03 | 1 | `STT_SECTION`, `STB_LOCAL` |
| 2 | `.nv.constant2` | 0x03 | 2 | `STT_SECTION`, `STB_LOCAL` |
| 3 | `device_fn` | 0x12 | 1 | `STT_FUNC`, `STB_GLOBAL` |
| 4 | `const_data` | 0x11 | 2 | `STT_OBJECT`, `STB_GLOBAL` (value = 0 into `.nv.constant2`, size = 64) |

### Initial Output ELF State (before any merge)

`main()` has already called `sub_4438F0` (`elfw_create`) to build an empty output wrapper. It contains only the skeleton:

| Output section | Type | Comment |
|---|---|---|
| `[0]` (null) | 0 | ELF null section |
| `[1] .note.nv.cuinfo` | `SHT_NOTE` (7) | Empty, to be filled in output phase |
| `[2] .note.nv.tkinfo` | `SHT_NOTE` (7) | Empty |
| `[3] .shstrtab` | `SHT_STRTAB` (3) | Section name string table |
| `[4] .strtab` | `SHT_STRTAB` (3) | Symbol name string table |
| `[5] .symtab` | `SHT_SYMTAB` (2) | Contains the null symbol (idx 0) |

Both symbol hash maps (`ctx+288`, `ctx+296`) are empty. The driver list at `ctx+512` is empty. The merge loop begins.

### Merge Step 1: Processing `input1.cubin`

`main()` calls `sub_45E7D0(ctx, input1_elf_data, "input1.cubin", ...)`.

**1a. Register input** (`sub_464C30` on `ctx+512`): Append a new input record with name `"input1.cubin"`, ISA bits for sm\_90, PTX version from `.nv.info`. This becomes input index 0; any symbol created from this input will get `input_index = 0` written at offset +40 of its symbol record.

**1b. Parse ELF header** (`sub_448360`, `sub_4484F0`, `sub_448370`): `num_sections = 9` (including null), `num_symbols = 5`, `is_64bit = 1`, `is_ewp = 0`.

**1c. Allocate mapping arrays** (lines 706-752): `map_symbol_index[0..5]`, `map_section_index[0..9]`, `map_section_offset[0..9]`, `weak_processed[0..5]` -- all zero.

**1d. Weak pre-pass** (lines 762-800): No symbol has `(st_info & 0xF) == 2`. The loop makes no calls to `sub_45D180`. `map_symbol_index` remains all zero.

**1e. Main symbol/section pass**. The loop iterates `sym_idx = 0..4`:

- **`sym_idx = 0`** (null symbol): `st_shndx == 0` and not a matching section name. Falls through the dispatch and is skipped. `map_symbol_index[0] = 0`.

- **`sym_idx = 1`** (`.text.kernel_a`, `STT_SECTION`, `STB_LOCAL`, `st_shndx = 1`):
  - Looks up section name `.text.kernel_a` in the output via `sub_4411D0` -- **not found** (returns 0).
  - Falls into the `sub_444AD0` branch (NVIDIA-pattern section name check). `.text.*` passes the check, so the section is created with `sh_type = 13` (a nvlink-internal code for text sections).
  - Calls `sub_440BE0` (`elfw_add_section_with_data`) which internally:
    1. Allocates a 104-byte section record via `sub_441AC0` (`section_create`), registers it in `ctx+360`, assigns it output section index **6** (next slot after the 5 skeleton sections), and registers the name in the hash map at `ctx+296`.
    2. Allocates a 48-byte symbol record for `.text.kernel_a` via the local-symbol path, appending to the positive array at `ctx+344`. Assigns positive symbol index **1** (first local after the null symbol). Registers the name in `ctx+288`.
  - Records `map_section_index[1] = 6` via the write-back at `sub_45CD30` / line 1114.
  - Records `map_symbol_index[1] = 1` (positive symbol).
  - Also copies the input section's data contribution via `sub_432B10` (`merge_overlapping_global_data`), which creates a 40-byte data node pointing at the 384 bytes of SASS, offset 0, and appends it to the section record's `data_list_head` (at section+72). `map_section_offset[1] = 0`.

- **`sym_idx = 2`** (`.nv.constant0.kernel_a`, `STT_SECTION`, `STB_LOCAL`, `st_shndx = 5`):
  - The output section name is looked up -- not found. The name matches the `.nv.constant0` prefix check at lines 1006-1007 and the type is reclassified to `0x70000064` (`SHT_CUDA_CONSTANT0 + 0`).
  - The full name `.nv.constant0.kernel_a` is actually interpreted as a **kernel-local** constant bank because the `.kernel_a` suffix is non-numeric. The dispatch at lines 1082-1105 calls `sub_438640` (`merge_constant_bank_data`) to merge the 352-byte parameter bank into the kernel-local constant bank namespace.
  - `map_section_index[5] = 7` (new output section index for `.nv.constant0.kernel_a`).
  - `map_symbol_index[2] = 2` (positive symbol, section symbol).

- **`sym_idx = 3`** (`kernel_a`, `STT_FUNC`, `STB_GLOBAL`, `st_shndx = 1`):
  - `sub_4411B0(ctx, "kernel_a")` returns 0 (not in output). No duplicate; proceeds to add.
  - The section containing `kernel_a` has already been mapped in the previous iterations: `map_section_index[1] = 6`.
  - Calls `sub_442CA0` (`elfw_add_function_symbol`) with name `"kernel_a"`, binding=1, section=6, value=0, size=384:
    1. Allocates a 48-byte symbol record, sets `st_info = 0x12` (global function), `st_shndx = 6`, `st_value = 0`, `st_size = 384`, `input_index = 0` (via `sub_464BB0(ctx+512) - 1`).
    2. Appends to the negative array at `ctx+352`. Assigns signed symbol index **-1**.
    3. Registers the name in `ctx+288` pointing at -1.
    4. Registers in the callgraph at `ctx+408` via `sub_44B940`.
    5. Calls `sub_442820` (`elfw_merge_symbols`) to handle any UFT stub merge (none in this case).
  - Records `map_symbol_index[3] = -1`.

- **`sym_idx = 4`** (`device_fn`, undefined, `STB_GLOBAL`, `st_shndx = 0`):
  - The section index is zero. The dispatch takes the `LABEL_142`/`LABEL_70` path for undefined symbols.
  - `sub_4411B0(ctx, "device_fn")` returns 0 (not yet in output).
  - `sub_440BE0` is called with `sh_type = 13` and `st_shndx = 0`, creating a 48-byte symbol record with `st_info = 0x10` (global, notype), `st_shndx = 0`, `st_value = 0`, `st_size = 0`, `input_index = 0`.
  - Appended to the negative array at `ctx+352` as signed index **-2**.
  - Registered in the name hash map at `ctx+288` pointing at -2.
  - Records `map_symbol_index[4] = -2`.

At this point the mapping arrays for input1 look like:

```
map_symbol_index:  [0, 1, 2, -1, -2]
map_section_index: [0, 6, 0, 0, 0, 7, 0, 0, 0]   // only sections 1 and 5 were mapped
map_section_offset:[0, 0, 0, 0, 0, 0, 0, 0, 0]
weak_processed:    [0, 0, 0, 0, 0]
```

**1f. Section header pass**. The loop walks input1's section headers:

- **`[3] .nv.info`** (`SHT_CUDA_INFO`): Iterate TLV records. Each record's symbol-index field is translated through `map_symbol_index`. The REGCOUNT entry for `kernel_a` (attribute code 47) arrives with input symbol index 3 and gets translated to output symbol index -1 (the `kernel_a` we just added). The entry is appended to the output's nvinfo list via `sub_4508F0`.

- **`[4] .nv.info.kernel_a`**: Same treatment, but this is a per-function nvinfo section. A new output section `.nv.info.kernel_a` is created via `sub_4504B0` (or `sub_441AC0` directly), and entries are copied in.

- **`[2] .rela.text.kernel_a`** (`SHT_RELA`): Two relocation entries:
  1. `r_sym = 4` (`device_fn`) -> translated to **-2**; `r_type = R_CUDA_ABS32_LO_20`; `r_offset = 0x00`; `r_addend = 0`. The target section index is `map_section_index[1] = 6` (the output `.text.kernel_a`). The entry is added via `sub_469790` (`reloc_add_rela`) with symbol index -2 and target section 6. **This is where the extern reference in input1 becomes a live relocation pointing at the undefined symbol record -- it will be re-resolved in step 2 when input2 provides the definition.**
  2. Second relocation is internal to input1 (loads from `.nv.constant0.kernel_a`) -- `r_sym = 2` translates to positive symbol index 2, target section index 6. Added to the output.

- **`[6] .nv.callgraph`**: The single 8-byte edge `(kernel_a, device_fn)` is translated to `(-1, -2)` and registered via `sub_44B9F0`. Even though `device_fn` is undefined at this moment, the callgraph records the edge as a signed symbol-index pair; the referenced record will later be overwritten in place when input2 defines `device_fn`, so the edge remains valid.

**1g. Cleanup**. Mapping arrays are freed; `sub_45E7D0` returns 0 to `main()`.

**Output ELF state after input1**:

| Output | Name | Type | Size | Source |
|---|---|---|---|---|
| sec[6] | `.text.kernel_a` | `0x0D` (text) | 384 | input1[1] |
| sec[7] | `.nv.constant0.kernel_a` | `0x70000064` | 352 | input1[5] |
| sec[8] | `.rela.text.kernel_a` | `SHT_RELA` | 48 | input1[2] (auto-created by `sub_441AC0`) |
| sec[9] | `.nv.info` | `0x70000000` | -- | input1[3] contributions |
| sec[10] | `.nv.info.kernel_a` | `0x70000000` | 32 | input1[4] |

Symbol table:

| Signed idx | Name | Binding | Section | Value |
|---|---|---|---|---|
| +1 | `.text.kernel_a` | LOCAL | 6 | 0 |
| +2 | `.nv.constant0.kernel_a` | LOCAL | 7 | 0 |
| -1 | `kernel_a` | GLOBAL FUNC | 6 | 0 (size 384) |
| -2 | `device_fn` | GLOBAL, **undefined** | 0 | 0 |

The relocation at `.rela.text.kernel_a + 0x00` now references output symbol -2. If the linker stopped here, the undefined reference would be an error.

### Merge Step 2: Processing `input2.cubin`

`main()` calls `sub_45E7D0(ctx, input2_elf_data, "input2.cubin", ...)`. The output ELF state is now non-empty.

**2a. Register input**: Append `"input2.cubin"` to `ctx+512`. Input index becomes **1**.

**2b. Parse ELF header**: `num_sections = 7`, `num_symbols = 5`.

**2c. Allocate mapping arrays**: Fresh arrays, all zero, of appropriate size for input2.

**2d. Weak pre-pass**: No weak symbols in input2. Skipped.

**2e. Main symbol/section pass**:

- **`sym_idx = 0`** (null): Skipped.

- **`sym_idx = 1`** (`.text.device_fn`, `STT_SECTION`, `STB_LOCAL`, `st_shndx = 1`):
  - `sub_4411D0(ctx, ".text.device_fn")` returns 0 (new name).
  - Created as output section index **11** via `sub_441AC0`. Data is merged into it via `sub_432B10`.
  - A positive symbol `.text.device_fn` is added at index **3** (next free positive slot). Name registered in `ctx+288`.
  - `map_section_index[1] = 11`, `map_symbol_index[1] = 3`.

- **`sym_idx = 2`** (`.nv.constant2`, `STT_SECTION`, `STB_LOCAL`, `st_shndx = 2`):
  - The section name is just `.nv.constant2` (no `.kernel` suffix). It matches the `.nv.constant` prefix at lines 1006-1007 and the strtol parse at line 1001 yields `bank = 2`. Reclassified to `sh_type = 0x70000064 + 2 = 0x70000066` (`SHT_CUDA_CONSTANT2`).
  - This is a **module-level** constant bank (no per-kernel suffix). `sub_4411D0(ctx, ".nv.constant2")` returns 0.
  - Created as output section index **12** via `sub_441AC0`. Data is merged via `sub_432B10` -- 64 bytes appended to a fresh data node.
  - Positive symbol `.nv.constant2` added at index **4**.
  - `map_section_index[2] = 12`, `map_symbol_index[2] = 4`.

- **`sym_idx = 3`** (`device_fn`, `STT_FUNC`, `STB_GLOBAL`, `st_shndx = 1`):
  - `sub_4411B0(ctx, "device_fn")` returns **-2** (found! from input1's undefined reference).
  - The existing record is fetched via `sub_440590(ctx, -2)`. It has `st_shndx = 0` (undefined) and `st_info = 0x10` (global notype).
  - Control flow at line 1037 detects that `st_info >> 4 == 1` (existing is global) and enters the replacement path at `LABEL_267` (line 1253).
  - Visibility conflict check (`v246 = ^` at line 1258) passes -- both have default visibility.
  - `sub_440350(ctx, existing)` returns the existing `st_shndx = 0` (not `SHN_COMMON`). The branch at line 1271 falls through to `LABEL_335` with `v250 = 0` (existing size) and `v251 = 0` (new size -- `n` is still 0 because `device_fn`'s `st_size` has not yet been read into `n` on the re-entry path).
  - At line 1289, `v250 == v251` (both zero) so jumps to `LABEL_323`. At line 1346, `v110 = 1` (new section index) so the code proceeds to `sub_442270` for the existing section (0) followed by `LABEL_325`.
  - At `LABEL_325` (line 1358), the code resolves the existing symbol's section via `sub_440350`. The result is 0 (undefined). The branch at line 1361 takes the "existing has no section" path.
  - The section data copy path at line 1384 (`sub_4377B0` = `add_data_to_existing_section`) is invoked with the new section index mapped to output 11 (`map_section_index[1] = 11`) and the symbol record at signed index -2 is updated in place:
    - `st_shndx` is overwritten with the output section index containing `.text.device_fn` (11).
    - `st_value = 0`, `st_size = 256`.
    - `st_info` is updated to `0x12` (global function).
    - `input_index` at offset +40 is updated to **1** (the new input's position in `ctx+512`).
  - **This is the resolution step**: the previously undefined symbol record at signed index -2 now has a section and a size. All relocations in the output that referenced -2 (including the `R_CUDA_ABS32_LO_20` from input1's `.rela.text.kernel_a`) automatically become live, because relocations store symbol indices, not pointers to copies.
  - `map_symbol_index[3] = -2` (the reused existing slot). The verbose trace would emit nothing special -- this is the normal global-defines-undefined path, not a weak or duplicate case.

- **`sym_idx = 4`** (`const_data`, `STT_OBJECT`, `STB_GLOBAL`, `st_shndx = 2`):
  - `sub_4411B0(ctx, "const_data")` returns 0 (new).
  - The containing section has already been mapped: `map_section_index[2] = 12`.
  - `sub_440740` (`elfw_add_symbol`) is called with binding=1, type=1 (object), section=12, value=0, size=64.
  - Negative symbol slot **-3** is assigned (next free after -1 and -2).
  - `map_symbol_index[4] = -3`.

Mapping arrays for input2:

```
map_symbol_index:  [0, 3, 4, -2, -3]
map_section_index: [0, 11, 12, 0, 0, 0, 0]
map_section_offset:[0, 0, 0, 0, 0, 0, 0]
weak_processed:    [0, 0, 0, 0, 0]
```

**2f. Section header pass** for input2:

- **`[3] .nv.info`** (`SHT_CUDA_INFO`): The REGCOUNT entry for `device_fn` arrives with input symbol index 3, translated through `map_symbol_index[3] = -2` to output signed index **-2**. The entry is appended to the output's nvinfo list. The `.nv.info` section now contains two REGCOUNT entries: `{-1: 24}` (from input1 kernel\_a) and `{-2: 16}` (from input2 device\_fn).

- **`[4] .nv.info.device_fn`**: A new per-function nvinfo section `.nv.info.device_fn` is created in the output as section index **13** via `sub_4504B0`.

- No relocation sections in input2, no callgraph.

**2g. Cleanup**. Return to `main()`.

### Final Merged ELF State

After both inputs have been processed, the output ELF contains:

| Output idx | Name | Type | Size | Origin |
|---|---|---|---|---|
| 0 | (null) | 0 | 0 | skeleton |
| 1 | `.note.nv.cuinfo` | 7 | -- | skeleton |
| 2 | `.note.nv.tkinfo` | 7 | -- | skeleton |
| 3 | `.shstrtab` | 3 | -- | skeleton |
| 4 | `.strtab` | 3 | -- | skeleton |
| 5 | `.symtab` | 2 | -- | skeleton |
| 6 | `.text.kernel_a` | 0x0D | 384 | input1[1] |
| 7 | `.nv.constant0.kernel_a` | 0x70000064 | 352 | input1[5] |
| 8 | `.rela.text.kernel_a` | 4 | 48 | input1[2] |
| 9 | `.nv.info` | 0x70000000 | merged | input1[3] + input2[3] |
| 10 | `.nv.info.kernel_a` | 0x70000000 | 32 | input1[4] |
| 11 | `.text.device_fn` | 0x0D | 256 | input2[1] |
| 12 | `.nv.constant2` | 0x70000066 | 64 | input2[2] |
| 13 | `.nv.info.device_fn` | 0x70000000 | 16 | input2[4] |

**Final symbol table** (positive slots concatenated first, then negatives in reverse per ELF ordering):

| Signed idx | Name | Binding/Type | Section | Value | Size | `input_index` |
|---|---|---|---|---|---|---|
| +1 | `.text.kernel_a` | LOCAL SECTION | 6 | 0 | 0 | 0 |
| +2 | `.nv.constant0.kernel_a` | LOCAL SECTION | 7 | 0 | 0 | 0 |
| +3 | `.text.device_fn` | LOCAL SECTION | 11 | 0 | 0 | 1 |
| +4 | `.nv.constant2` | LOCAL SECTION | 12 | 0 | 0 | 1 |
| -1 | `kernel_a` | GLOBAL FUNC | 6 | 0 | 384 | 0 |
| -2 | `device_fn` | GLOBAL FUNC | 11 | 0 | 256 | **1 (updated)** |
| -3 | `const_data` | GLOBAL OBJECT | 12 | 0 | 64 | 1 |

**Relocation resolution**: The entry in `.rela.text.kernel_a` at offset 0 still carries symbol index **-2**. Because `sub_442CA0`/`sub_440BE0` updated the negative-array entry at slot 2 in place during input2 processing, the relocation now effectively points at:

- symbol name: `device_fn`
- section: **11** (`.text.device_fn`)
- value: 0

When the later relocation application phase (during output ELF serialization) walks the relocation list and patches the SASS at `.text.kernel_a + 0x00`, it resolves the target address using the updated symbol record -- turning input1's extern call into a live branch to the input2-provided `device_fn` body. No extra pass is needed to "fix up" the relocation; the in-place symbol update makes it automatic.

### Key Observations from the Example

1. **Signed symbol indices are stable**: Once an undefined symbol is added (as -2 for `device_fn`), its slot persists. When input2 defines the symbol, the **same** slot is updated in place -- relocations and callgraph edges referring to -2 never need to be rewritten.

2. **Sections are never renumbered**: Output section indices are assigned at creation time and never change during the merge loop. The fact that `.text.kernel_a` lands at output index 6 while `.text.device_fn` lands at 11 is a direct consequence of command-line order and the intervening auto-created sections (`.rela.text.kernel_a`, `.nv.info`, `.nv.info.kernel_a`).

3. **Mapping tables are per-input and disposable**: `map_symbol_index` and `map_section_index` exist only for the duration of one `sub_45E7D0` call. They are freed at the end of each call. The permanent state lives in the output `ctx` (sections at +360, positive symbols at +344, negative symbols at +352, hash maps at +288/+296).

4. **Auto-created relocation sections**: `sub_441AC0` creates `.rela.text.kernel_a` as output section 8 automatically when `.text.kernel_a` is added (driven by the callgraph-aware reloc-section creation at `sub_441AC0` line ~60). This is why the output has more sections than either input individually.

5. **`.nv.info` accumulates**: Per-kernel REGCOUNT and FRAME\_SIZE entries from every input are appended to the same merged `.nv.info` section. Translation through `map_symbol_index` ensures that the `sym_index` fields in TLV records always refer to the correct output-global symbol after merge.

6. **Module-level vs kernel-local constants**: `.nv.constant2` (module-level, from input2) and `.nv.constant0.kernel_a` (kernel-local, from input1) follow different dispatch paths -- the former goes through the plain section-add path at line 1117 and the latter through `sub_438640` at line 1082. Both end up as distinct output sections with `sh_type` values derived from `strtol(name + 12, NULL, 10) + 0x70000064`.

## Mercury Section Skipping

For Mercury targets (sm100+), certain sections are conditionally skipped during merge. When both the input cubin and the output context agree on flags (checked via the `v477 = v535 && v537` condition, which tests flag bits at `ctx+48` and the input ELF header), sections with the `0x10000000` flag in their `sh_flags` field are skipped:

```c
if (is_mercury_compatible && (section_flags & 0x10000000) != 0) {
    fprintf(stderr, "skip mercury section %i\n", section_idx);
    continue;
}
```

This allows Mercury-specific sections to be deferred to the FNLZR post-link transformation phase.

## Section Mapping Helper: `sub_45CD30`

`sub_45CD30` creates or finds the output section corresponding to an input section. If the section does not yet exist in the output (checked by `sub_4411D0`), it creates it via `sub_441AC0` (elfw\_add\_reloc\_section). It also handles the special case of duplicate parameter banks on weak entry points: `"duplicate param bank on weak entry %s"`.

For sections that are not one of the special CUDA types (global, global\_init, shared, local, constant banks), the function delegates to `sub_432B10` (merge\_overlapping\_global\_data) which sets up the section offset tracking.

## Timing

The merge phase is bracketed by calls to `sub_4279C0`, which is the linker's timing checkpoint function:

```c
// sub_4279C0 implementation
void timing_checkpoint(const char *phase_name) {
    if (timing_initialized) {
        sub_45CCE0(&timer_state);  // stop timer
        fprintf(stderr, "%s time: %f\n", phase_name, elapsed);
    } else {
        timing_initialized = true;
    }
    sub_45CCD0(&timer_state);      // start timer
}
```

The global timer state is at `unk_2A5F1B0`, and the initialized flag is `byte_2A5F1C0`. This produces output like:

```
merge time: 0.042000
```

## Error Conditions

| Error string | Condition | Severity |
|---|---|---|
| `"efh not found"` | Input ELF has no file header | Fatal |
| `"symsec not found"` | Input ELF has no `.symtab` | Fatal |
| `"strsec not found"` | Input ELF has no string table linked from `.symtab` | Fatal |
| `"section not mapped"` | Relocation references unmapped section | Fatal |
| `"unexpected reloc section"` | Relocation section has unexpected type | Fatal |
| `"merge_elf failed"` | Return value from `sub_45E7D0` is nonzero | Fatal (in `main()`) |
| `"reference to deleted symbol"` | Symbol was removed (weak resolution) but still referenced | Fatal |
| Multiple definition errors via `unk_2A5B9D0` | Two strong definitions of the same symbol | Fatal |
| `unk_2A5BA10` error | Section type conflict between existing and new definition | Fatal |
| `unk_2A5B950` error | Size conflict: `.nv.global.init` smaller than existing common | Fatal |
| `unk_2A5B920` error | `maxrregcount` value in `.nv.info` exceeds configured limit | Fatal |
| `unk_2A5B8C0` error | Reserved SMEM partition type conflict between objects | Fatal |
| `unk_2A5B910` error | `.nv.compat` section missing or invalid | Fatal |
| `unk_2A5B900` error | ISA\_CLASS mismatch for Mercury target | Fatal |
| `unk_2A5B9C0` error | Callgraph info validation failure | Fatal |
| `"weak %s already processed"` | Diagnostic (verbose only) -- not an error | Info |
| `"global.init replaces common for %s"` | Diagnostic (verbose only) | Info |
| `"increase size of common %s"` | Diagnostic (verbose only) | Info |
| `"skip mercury section %i"` | Mercury section deferred to FNLZR | Info |
| `"unknown .nv.compat attribute (%x) encoutered."` | Unknown compat attribute (typo in binary) | Info |

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x45E7D0` | `merge_elf` | 89,156 B | Core merge function -- processes one input cubin |
| `0x45D180` | `merge_weak_function` | 26,816 B | Weak symbol resolution (register count / PTX version) |
| `0x45CD30` | `section_map_or_create` | ~1,200 B | Find/create output section for input section index |
| `0x45E3C0` | `copy_section_with_relocs` | ~1,000 B | Copy section data + relocation entries |
| `0x45CF00` | `copy_relocinfo_section` | ~1,000 B | Copy SHT\_CUDA\_RELOCINFO sections |
| `0x4411F0` | `elfw_copy_section` | 12,184 B | Deep copy of section between ELF wrappers |
| `0x442820` | `elfw_merge_symbols` | 5,371 B | Merge symbols including UFT stubs |
| `0x440740` | `elfw_add_section` | 5,410 B | Add new section to output ELF |
| `0x440BE0` | `elfw_add_section_with_data` | 7,034 B | Add section with initial data payload |
| `0x442CA0` | `elfw_add_symbol` | 7,159 B | Add symbol to output symbol table |
| `0x4411B0` | `elfw_find_section_by_name` | ~500 B | Lookup section by name in output ELF |
| `0x4411D0` | `elfw_find_or_create_section` | ~800 B | Combined find-or-create for section index |
| `0x438640` | `merge_constant_bank_data` | 4,043 B | Merge data into constant memory banks |
| `0x437BB0` | `add_shared_section` | ~2,000 B | Create shared memory placeholder section |
| `0x4377B0` | `add_data_to_existing_section` | ~2,000 B | Append data to an existing output section |
| `0x4379A0` | `add_reserved_smem_section` | ~2,000 B | Create reserved shared memory section |
| `0x4448C0` | `check_undefined_functions` | ~2,000 B | Check if all function symbols are defined |
| `0x4649E0` | `list_reverse` | ~100 B | Reverse a singly-linked list in place |
| `0x4279C0` | `timing_checkpoint` | ~200 B | Emit phase timing to stderr |
| `0x45CCD0` | `timer_start` | ~100 B | Start high-resolution timer |
| `0x45CCE0` | `timer_stop` | ~100 B | Stop timer and compute elapsed |
| `0x4508F0` | `nvinfo_add_entry` | ~500 B | Add translated `.nv.info` entry to output |
| `0x4504B0` | `nvinfo_get_or_create` | ~500 B | Get or create `.nv.info` section for a function |
| `0x451920` | `compat_set_byte_attr` | ~200 B | Set byte-sized `.nv.compat` attribute |
| `0x451BA0` | `compat_set_word_attr` | ~200 B | Set word-sized `.nv.compat` attribute |
| `0x469230` | `reloc_map_section` | ~500 B | Map relocation target to output section |
| `0x469790` | `reloc_add_rela` | ~500 B | Add SHT\_RELA entry to output |
| `0x4698A0` | `reloc_add_rel` | ~500 B | Add SHT\_REL entry to output |
| `0x467460` | `fatal_error` | ~500 B | Emit fatal diagnostic and (usually) abort |

## Cross-References

- [Pipeline Overview](overview.md) -- merge phase in context of the full pipeline
- [Input File Loop](input-loop.md) -- how input objects are collected before merge
- [Weak Symbols](../linker/weak-symbols.md) -- detailed weak resolution policy
- [Section Merging](../linker/section-merging.md) -- section-level merge mechanics
- [Symbol Resolution](../linker/symbol-resolution.md) -- global/weak/local symbol resolution
- [NVIDIA Sections](../elf/nvidia-sections.md) -- `.nv.*` section catalog with type codes
- [`.nv.info`](../elf/nv-info.md) -- per-function metadata format
- [Layout Phase](layout.md) -- next pipeline phase after merge
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- callgraph-based DCE after merge

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `merge_elf` at `0x45E7D0`, 89,156 bytes, 2,838 lines, 450+ locals | **HIGH** | Verified: `decompiled/sub_45E7D0_0x45e7d0.c` exists with exactly 2,838 lines |
| `merge_weak_function` at `0x45D180`, 26,816 bytes, 913 lines | **HIGH** | Verified: `decompiled/sub_45D180_0x45d180.c` exists with exactly 913 lines |
| 222 distinct callees from `merge_elf` | **MEDIUM** | Plausible from function size; not independently counted |
| `pthread_mutex` for concurrent merge in split-compile mode | **MEDIUM** | Mutex functions verified in decompiled/ (`j_.pthread_mutex_lock_0x44f9d0.c`, etc.) |
| Error strings: `"efh not found"`, `"symsec not found"`, `"strsec not found"` | **HIGH** | Strings confirmed in `sub_45E7D0` decompiled output (9 total matches for these patterns) |
| `"merge_elf failed"` in main | **HIGH** | Found at line 1590 of `main_0x409800.c` |
| Timing checkpoint `"merge"` via `sub_4279C0` | **HIGH** | `sub_4279C0` verified in decompiled/ |
| Six-phase internal architecture (header, maps, weak pass, section iter, nvinfo, cleanup) | **HIGH** | Structural match from 2,838-line decompiled code; phase boundaries match described line ranges |
| Four mapping arrays (symbol, section, offset, weak_processed) | **MEDIUM** | Allocation pattern visible in `sub_45E7D0`; array sizes inferred from decompiled code |
| Weak resolution: register count comparison, PTX version tiebreaker | **HIGH** | Strings `"replace weak function"`, `"no new register count"`, `"no original register count"` verified in binary strings; EIATTR code 0x2F (47) for regcount confirmed |
| CUDA section type constants (0x70000000, 0x70000007, etc.) | **HIGH** | Hex constants visible in `sub_45E7D0` decompiled code; match ELF spec for `SHT_LOPROC` |
| Constant bank encoding: `strtol(name + 12, NULL, 10) + 0x70000064` | **MEDIUM** | Arithmetic visible in decompiled code; exact offset confirmed |
| `"unknown .nv.compat attribute (%x) encoutered."` typo in binary | **HIGH** | Exact misspelling `"encoutered"` confirmed in binary string table |
| `.nv.callgraph` entries as 8-byte (caller, callee) records | **MEDIUM** | Inferred from `sub_44B9F0`/`sub_44BA60` call patterns; record size not independently measured |
| Mercury section skipping (`sh_flags & 0x10000000`) | **MEDIUM** | Flag constant visible in decompiled code; skip behavior matches Mercury architecture requirements |
| Post-replacement cleanup: 4 passes (reloc, debug reloc, nvinfo, OCG constants) | **MEDIUM** | Structural match from decompiled LABEL_82 region; pass count inferred from code blocks |
| All function addresses in Function Map table (30+ entries) | **HIGH** | All verified to exist as files in `decompiled/` directory |
