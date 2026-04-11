# Capsule Mercury & Finalization

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Capsule Mercury ("capmerc") is a packaging format that wraps Mercury-encoded instruction streams with relocation metadata, debug information, and a snapshot of compilation knobs, enabling deferred finalization for a target SM that may differ from the original compilation target. Where standard Mercury produces a fully-resolved SASS binary bound to a single SM, capmerc produces an intermediate ELF object that a downstream tool (the driver or linker) can finalize into native SASS at load time. This is the default output format for all SM 100+ targets (Blackwell, Jetson Thor, consumer RTX 50-series). The capmerc data lives in `.nv.capmerc<funcname>` per-function ELF sections alongside 21 types of `.nv.merc.*` auxiliary sections carrying cloned debug data, memory-space metadata, and Mercury-specific relocations. Finalization can be "opportunistic" -- the same capmerc object may be finalized for different SMs within or across architectural families, controlled by `--opportunistic-finalization-lvl`.

| | |
|---|---|
| **Output modes** | `mercury` (SM 75--99 default), `capmerc` (SM 100+ default), `sass` (explicit only) |
| **CLI parser** | `sub_703AB0` (10KB, ParsePtxasOptions) |
| **Auto-enable** | SM arch > 99 sets `*(context + offset+81) = 1` |
| **Mercury mode flag** | `*(DWORD*)(context+385) == 2` (shared with Mercury) |
| **Capsule descriptor** | 328-byte object, one per function (`sub_1C9C300`) |
| **Merc section classifier** | `sub_1C98C60` (9KB, 15 `.nv.merc.*` names) |
| **Master ELF emitter** | `sub_1C9F280` (97KB, orchestrates full CUBIN output) |
| **Self-check verifier** | `sub_720F00` (64KB Flex lexer) + `sub_729540` (35KB comparator) |
| **Off-target checker** | `sub_60F290` (compatibility validation) |
| **Kernel finalizer** | `sub_612DE0` (47KB, fastpath optimization) |

## Output Mode Selection

The ptxas CLI (`sub_703AB0`) registers three binary-kind options plus related flags:

| Option | String literal | Purpose |
|---|---|---|
| `--binary-kind` | `"mercury,capmerc,sass"` | Select output format |
| `--cap-merc` | `"Generate Capsule Mercury"` | Force capmerc regardless of SM |
| `--self-check` | `"Self check for capsule mercury (capmerc)"` | Roundtrip verification |
| `--out-sass` | `"Generate output of capmerc based reconstituted sass"` | Dump reconstituted SASS |
| `--opportunistic-finalization-lvl` | (in finalization logic) | Finalization aggressiveness |

When `--binary-kind` is not specified, the default is determined by SM version:

```c
// Pseudocode from sub_703AB0 + auto-enable logic
if (sm_version > 99) {
    *(context + offset + 81) = 1;  // capmerc auto-enabled
    binary_kind = CAPMERC;
} else if (sm_version >= 75) {
    binary_kind = MERCURY;
} else {
    binary_kind = SASS;  // legacy direct encoding
}
```

The Mercury mode flag `*(DWORD*)(context+385) == 2` is shared between Mercury and capmerc -- both use the identical Mercury encoder pipeline (phases 117--122). The capmerc distinction is purely at the ELF emission level: capmerc wraps the phase-122 SASS output in a capsule descriptor with relocation metadata instead of emitting it directly as a `.text` section.

## Capsule Mercury ELF Structure

A capmerc-mode compilation produces a CUBIN ELF with two layers of content: standard CUBIN sections (`.text.<func>`, `.nv.constant0`, `.nv.info.<func>`, etc.) and a parallel set of `.nv.merc.*` sections carrying the metadata needed for deferred finalization.

```
CUBIN ELF (capmerc mode)
├── Standard sections
│   ├── .shstrtab, .strtab, .symtab, .symtab_shndx
│   ├── .text.<funcname>               (SASS binary, possibly partial)
│   ├── .nv.constant0.<funcname>       (constant bank data)
│   ├── .nv.shared.<funcname>          (shared memory layout)
│   ├── .nv.info.<funcname>            (EIATTR attributes)
│   ├── .note.nv.tkinfo, .note.nv.cuinfo
│   └── .nv.uft.entry                 (unified function table)
│
├── Per-function capsule descriptor
│   └── .nv.capmerc<funcname>          (328-byte descriptor + payload)
│
└── Mercury auxiliary sections (21 types)
    ├── .nv.merc.debug_abbrev          (DWARF abbreviation table)
    ├── .nv.merc.debug_aranges         (DWARF address ranges)
    ├── .nv.merc.debug_frame           (DWARF frame info)
    ├── .nv.merc.debug_info            (DWARF info)
    ├── .nv.merc.debug_line            (DWARF line table)
    ├── .nv.merc.debug_loc             (DWARF locations)
    ├── .nv.merc.debug_macinfo         (DWARF macro info)
    ├── .nv.merc.debug_pubnames        (DWARF public names)
    ├── .nv.merc.debug_pubtypes        (DWARF public types)
    ├── .nv.merc.debug_ranges          (DWARF ranges)
    ├── .nv.merc.debug_str             (DWARF string table)
    ├── .nv.merc.nv_debug_ptx_txt      (embedded PTX source text)
    ├── .nv.merc.nv_debug_line_sass    (SASS-level line table)
    ├── .nv.merc.nv_debug_info_reg_sass (register allocation info)
    ├── .nv.merc.nv_debug_info_reg_type (register type info)
    ├── .nv.merc.symtab_shndx          (extended section index table)
    ├── .nv.merc.nv.shared.reserved    (shared memory reservation)
    ├── .nv.merc.rela                  (Mercury relocations)
    ├── .nv.merc.rela<secname>         (per-section relocation tables)
    └── .nv.merc.<memory-space>        (cloned constant/global/local/shared)
```

### Capsule Descriptor -- `sub_1C9C300`

Each function produces a `.nv.capmerc<funcname>` section constructed by `sub_1C9C300` (24KB, 3816 bytes binary). This function processes `.nv.capmerc` and `.merc` markers, embeds KNOBS data (compilation configuration snapshot), manages constant bank replication, and creates the per-function descriptor.

The descriptor is a 328-byte object containing:
- Mercury-encoded instruction stream for the function
- R_MERCURY_* relocation entries that must be patched during finalization
- KNOBS block -- a serialized snapshot of all knob values affecting code generation, optimization level, target parameters, and feature flags
- References to the `.nv.merc.*` auxiliary sections
- Function-level metadata: register counts, barrier counts, shared memory usage

The KNOBS embedding allows the finalizer to reproduce exact compilation settings without the original command-line arguments. This is critical for off-target finalization where the finalizer runs in a different context (e.g., the CUDA driver at application load time).

#### Construction Algorithm (`sub_1C9C300`)

```
function BuildCapsuleDescriptor(func_markers, context):
    if func_markers == NULL or func_markers.active == 0:
        return 0                                  // nothing to emit

    // ── Phase 1: allocate and zero-fill 328-byte descriptor ────────
    desc = allocate(328)                          // sub_424070(allocator, 328)
    memset(desc, 0, 328)

    // ── Phase 2: compute sampling_mode from section type ───────────
    section_type = resolve_section(context, func_markers.section_id).type
    if section_type != 1:
        flag = 0
        if section_type - 0x70000006 <= 14:       // SHT_LOPROC range check
            flag = (0x5D05 >> (section_type - 6)) & 1
        if (section_type - 0x70000064 <= 26) or flag:
            desc.sampling_mode = (section.flags >> 2) & (section_type == 0x7000000E)
    desc.section_index = func_markers.section_id

    // ── Phase 3: initialize container fields ───────────────────────
    desc.rela_list_a      = new_vector(elem_size=8)
    desc.rela_list_b      = new_vector(elem_size=8)
    desc.reloc_symbol_list= new_vector(elem_size=8)
    desc.reloc_index_set  = new_sorted_set(cmp=sub_427750, elem=0x20)
    desc.per_reloc_data   = new_sorted_set(cmp=sub_427750, elem=0x20)
    desc.reloc_payload_map= new_sorted_set(cmp=sub_427750, elem=0x20)
    desc.kv_pair_list     = new_vector(elem_size=8)
    desc.knobs_pair_list  = new_vector(elem_size=8)
    desc.min_sm_version   = 256                   // sentinel = no constraint
    if profile != NULL:
        desc.min_sm_version = profile.sm_version  // from *(profile + 6)

    // ── Phase 4: resolve rela, constant bank, text sections ────────
    if func_markers.rela_a_id:  copy_rela(func_markers, context, &desc.rela_list_a)
    if func_markers.rela_b_id:  copy_rela(func_markers, context, &desc.rela_list_b)
    if func_markers.const_id:
        sec = resolve_section(context, func_markers.const_id)
        desc.const_bank_section_index = func_markers.const_id
        // walk constant bank data range → populate reloc containers
    if func_markers.text_id:
        desc.text_section_offset = get_offset(context, resolve(func_markers.text_id))
        desc.text_rela_section_index = func_markers.text_id

    desc.sass_data_offset = get_offset(context, section)
    desc.sass_data_size   = section.size
    desc.func_name_ptr    = get_name(context, section)
    desc.section_name_ptr = ".nv.capmerc" + func_name
    desc.section_alignment= 16

    // ── Phase 5: scan symbol table for weak binding ────────────────
    for i in 0 .. symbol_count(context):
        sym = get_symbol(context, i)
        if sym.type == STT_SECTION and sym.section == desc.section_index:
            if (sym.value == 0 or (sym.value & 0x7F == 0 and sym.info & 4)):
                if desc.weak_symbol_desc == 0:
                    desc.weak_symbol_index = i
                    desc.weak_symbol_desc  = sym

    // ── Phase 6: parse marker stream ───────────────────────────────
    cursor = stream_start;  end = stream_start + stream_length
    while cursor < end:
        type = cursor[0];  sub = cursor[1]
        switch type:
          case 2:   // 4-byte boolean flags
            if sub == 21: desc.has_exit  = 1
            if sub == 22: desc.has_crs   = 1
            if sub == 74: desc.sampling_mode = cursor[2]
            cursor += 4

          case 3:   // 4-byte short-value markers
            payload = *(WORD*)(cursor+2)
            if sub == 27: desc.desc_version       = payload
            if sub == 73: desc.stack_frame_size    = payload
            if sub == 88: desc.{uses_atomics|shared|global}[payload] = 1
            if sub == 95: desc.min_sm_version = payload; desc.has_crs_depth = 1
            cursor += 4

          case 4:   // variable-length data markers
            size = *(WORD*)(cursor+2);  data = cursor + 4;  next = data + size
            switch sub:
              10: desc.has_global_refs = 1
              25: desc.has_shared_refs = 1
              47: desc.instr_format_version = *(DWORD*)data
              50: desc.register_count       = *(DWORD*)data
              54: desc.has_texture_refs     = 1
              67: desc.barrier_info_size = size; desc.barrier_info_data = data
              23,69: max_reg = *(WORD*)(data+6)     // register pressure
                     desc.max_register_count = max(desc.max_register_count,
                                                   max_reg + shift)
              28,40,49,70,71,87:                    // reloc symbol indices
                     for j in 0 .. size/4:
                         reloc_index_set.insert(*(DWORD*)(data + 4*j))
              46,68: for j in 0 .. size/8:          // 8-byte reloc entries
                         reloc_index_set.insert(*(DWORD*)(data + 8*j))
              52:    while data < next:              // per-symbol reloc records
                         sym_id = *data; dep_count = data[2]
                         reloc_index_set.insert(sym_id)
                         if dep_count:
                             buf = new_vector(dep_count)
                             reloc_payload_map.insert(sym_id, buf)
                             for k in 0 .. dep_count:
                                 per_reloc_data.insert(data[3+k])
                                 buf.append(data[3+k])
                         data += 3 + dep_count
              57:    for j in 0 .. size/16:         // 16-byte reloc records
                         reloc_index_set.insert(*(DWORD*)(data + 16*j))
              64:    for j in 0 .. size/12:         // 12-byte reloc records
                         reloc_index_set.insert(*(DWORD*)(data + 12*j))
              72:    target_type = *(DWORD*)data     // KNOBS section binding
                     for s in context.knobs_sections:
                         if s.type == target_type:
                             desc.knobs_section_desc = alloc_64B_subobj()
                             desc.knobs_section_desc.index  = s.id
                             desc.knobs_section_desc.offset = get_offset(s)
                             desc.knobs_section_desc.size   = s.size
                             desc.knobs_section_desc.name   = get_name(s)
                             break
              85:    parse_reloc_subtypes(data, size, reloc_index_set)
              90:    blob = alloc_copy(data, size)   // KNOBS KV block
                     checksum = crc32_init(0x123456)
                     crc32_update(checksum, blob, size)
                     while offset < size - 3:
                         name_off = *(WORD*)(rec-4); val_off = *(WORD*)(rec-2)
                         entry = {key=strdup(name), value=strdup(val)}
                         if key == "KNOBS": desc.knobs_pair_list.append(entry)
                         else:              desc.kv_pair_list.append(entry)
                         offset += name_off + 4 + val_off
                     free(blob)
            cursor = next

          default:  cursor += 4                    // unknown type: skip

    // ── Phase 7: allocate companion .merc descriptor ───────────────
    if merc_mirror_active:
        merc = allocate(328);  memset(merc, 0, 328)
        merc.section_index    = desc.section_index
        merc.sass_data_offset = memcpy_alloc(desc.sass_data_offset,
                                             desc.sass_data_size)
        merc.sass_data_size   = desc.sass_data_size
        merc.section_flags    = section.elf_flags  // from original ELF header
        merc.section_name_ptr = ".merc" + func_name
        context.merc_strtab_size += strlen(merc.section_name_ptr) + 1
        merc_section_list.append(merc)

    return 0
```

### Capsule Descriptor Layout (328 bytes)

The descriptor is heap-allocated via `sub_424070(allocator, 328)` and zero-filled before field initialization. The constructor also creates a companion `.merc<funcname>` descriptor (same 328-byte layout) when merc section mirroring is active.

```
         Capsule Descriptor (328 bytes = 0x148)
         ======================================

         Group 1: Identity
         ┌─────────┬──────┬────────────────────────────────────────┐
   0x000 │ WORD    │  2B  │ desc_version                           │
   0x002 │ WORD    │  2B  │ instr_format_version                   │
   0x004 │ DWORD   │  4B  │ section_index                          │
   0x008 │ DWORD   │  4B  │ weak_symbol_index                      │
   0x00C │ --      │  4B  │ (padding)                              │
         └─────────┴──────┴────────────────────────────────────────┘

         Group 2: SASS Data
         ┌─────────┬──────┬────────────────────────────────────────┐
   0x010 │ QWORD   │  8B  │ weak_symbol_desc                       │
   0x018 │ QWORD   │  8B  │ sass_data_offset                       │
   0x020 │ DWORD   │  4B  │ sass_data_size                         │
   0x024 │ --      │  4B  │ (padding)                              │
   0x028 │ QWORD   │  8B  │ func_name_ptr                          │
         └─────────┴──────┴────────────────────────────────────────┘

         Group 3: Relocation Infrastructure
         ┌─────────┬──────┬────────────────────────────────────────┐
   0x030 │ QWORD   │  8B  │ rela_list_a (vector)                   │
   0x038 │ QWORD   │  8B  │ rela_list_b (vector)                   │
   0x040 │ QWORD   │  8B  │ reloc_symbol_list (vector)             │
   0x048 │ QWORD   │  8B  │ aux_rela_list (vector)                 │
   0x050 │ QWORD   │  8B  │ debug_rela_list (vector)               │
   0x058 │ QWORD   │  8B  │ text_section_offset                    │
   0x060 │ QWORD   │  8B  │ reloc_index_set (sorted container)     │
   0x068 │ QWORD   │  8B  │ per_reloc_data_set (sorted container)  │
   0x070 │ BYTE    │  1B  │ sampling_mode                          │
   0x071 │ --      │  7B  │ (padding)                              │
   0x078 │ QWORD   │  8B  │ reloc_payload_map (sorted container)   │
         └─────────┴──────┴────────────────────────────────────────┘

   0x080 │ --      │ 32B  │ (reserved, not written by constructor)  │
         └─────────┴──────┴────────────────────────────────────────┘

         Group 4: Function Metadata
         ┌─────────┬──────┬────────────────────────────────────────┐
   0x0A0 │ QWORD   │  8B  │ section_flags                          │
   0x0A8 │ DWORD   │  4B  │ max_register_count                     │
   0x0AC │ DWORD   │  4B  │ extra_section_index                    │
   0x0B0 │ BYTE    │  1B  │ has_global_refs                        │
   0x0B1 │ BYTE    │  1B  │ has_shared_refs                        │
   0x0B2 │ BYTE    │  1B  │ has_exit                               │
   0x0B3 │ BYTE    │  1B  │ has_crs                                │
   0x0B4 │ BYTE    │  1B  │ uses_atomics                           │
   0x0B5 │ BYTE    │  1B  │ uses_shared_atomics                    │
   0x0B6 │ BYTE    │  1B  │ uses_global_atomics                    │
   0x0B7 │ BYTE    │  1B  │ has_texture_refs                       │
   0x0B8 │ --      │ 24B  │ (padding)                              │
         └─────────┴──────┴────────────────────────────────────────┘

         Group 5: Code Generation Parameters
         ┌─────────┬──────┬────────────────────────────────────────┐
   0x0D0 │ QWORD   │  8B  │ knobs_section_desc_ptr → 64B sub-obj  │
         │         │      │   +0x00 DWORD: knobs_section_index     │
         │         │      │   +0x08 QWORD: knobs_section_offset    │
         │         │      │   +0x10 DWORD: knobs_section_size      │
         │         │      │   +0x18 QWORD: knobs_section_name_ptr  │
   0x0D8 │ DWORD   │  4B  │ stack_frame_size                       │
   0x0DC │ --      │  4B  │ (padding)                              │
   0x0E0 │ DWORD   │  4B  │ register_count                         │
   0x0E4 │ --      │  4B  │ (padding)                              │
   0x0E8 │ DWORD   │  4B  │ barrier_info_size                      │
   0x0EC │ --      │  4B  │ (padding)                              │
   0x0F0 │ QWORD   │  8B  │ barrier_info_data_ptr                  │
   0x0F8 │ --      │  8B  │ (reserved)                             │
         └─────────┴──────┴────────────────────────────────────────┘

         Group 6: Constant Bank & Section Info
         ┌─────────┬──────┬────────────────────────────────────────┐
   0x100 │ QWORD   │  8B  │ const_bank_offset                      │
   0x108 │ DWORD   │  4B  │ const_bank_size                        │
   0x10C │ --      │  4B  │ (padding)                              │
   0x110 │ QWORD   │  8B  │ section_name_ptr (".nv.capmerc<func>") │
   0x118 │ QWORD   │  8B  │ section_alignment (default 16)         │
   0x120 │ DWORD   │  4B  │ const_bank_section_index               │
   0x124 │ --      │  4B  │ (padding)                              │
   0x128 │ DWORD   │  4B  │ text_section_index                     │
   0x12C │ DWORD   │  4B  │ text_rela_section_index                │
         └─────────┴──────┴────────────────────────────────────────┘

         Group 7: KNOBS Embedding
         ┌─────────┬──────┬────────────────────────────────────────┐
   0x130 │ QWORD   │  8B  │ kv_pair_list (vector)                  │
   0x138 │ QWORD   │  8B  │ knobs_pair_list (vector)               │
   0x140 │ WORD    │  2B  │ min_sm_version (default 256 = sentinel) │
   0x142 │ BYTE    │  1B  │ has_crs_depth                          │
   0x143 │ --      │  5B  │ (padding to 0x148)                     │
         └─────────┴──────┴────────────────────────────────────────┘
```

Key design observations:

**Flag byte block (+0x0B0 to +0x0B7).** Eight single-byte flags capture function characteristics that determine which R_MERCURY_* relocation patches the finalizer must apply. The flags are set by type-2, type-3, and type-4 markers in the capmerc stream. Each flag is a boolean (0 or 1), never a bitfield.

**KNOBS indirection (+0x0D0).** The KNOBS data does not live inline in the descriptor. Instead, +0x0D0 points to a separately allocated 64-byte sub-object carrying the ELF coordinates (section index, file offset, size, and name pointer) of the KNOBS section. This allows the KNOBS data to reside in a dedicated ELF section while the descriptor references it by position. The KNOBS pair list at +0x138 and the generic key-value list at +0x130 store the parsed key-value pairs from marker type 90 data blocks; the "KNOBS" string literal serves as the discriminator between the two lists.

**Dual-descriptor pattern.** When the merc section mirror is active, the constructor allocates a second 328-byte object for the `.merc<funcname>` companion section. This companion receives a copy of the SASS data (not a pointer -- an actual `memcpy` of `sass_data_size` bytes), the function name with a `.merc` prefix, and the section flags from the original ELF section header at +0x0A0. The companion's weak_symbol_index (+0x008) is always zero.

**Relocation containers.** The three sorted containers at +0x060, +0x068, and +0x078 (created via `sub_425CA0` with comparator pair `sub_427750`/`sub_427760` and element size 0x20 = 32 bytes) form a three-level relocation index. The reloc_index_set stores symbol indices that appear in relocations. The per_reloc_data_set stores per-symbol relocation metadata. The reloc_payload_map associates symbol indices with the actual payload data that the finalizer patches into instruction bytes. These are populated by marker sub-types 10, 23, 25, 28, 40, 46, 49, 52, 57, 64, 68, 70, 71, 85, and 87.

**min_sm_version sentinel.** The default value 256 (0x100) at +0x140 acts as a sentinel meaning "no minimum SM constraint." When a target profile is available at construction time, the profile's SM version overwrites this field. Marker sub-type 95 can further override it when CRS depth information constrains the minimum SM.

### Capmerc Marker Stream Format

The constructor parses a compact binary marker stream embedded in the capmerc section data. Each marker begins with a type byte followed by a sub-type byte:

| Type | Size | Format | Description |
|---|---|---|---|
| 2 | 4 bytes fixed | `[02] [sub] [00] [00]` | Boolean flag markers |
| 3 | 4 bytes fixed | `[03] [sub] [WORD payload]` | Short value markers |
| 4 | variable | `[04] [sub] [WORD size] [payload...]` | Variable-length data markers |

Selected marker sub-types and the descriptor fields they populate:

| Sub | Type | Descriptor Field | Purpose |
|---|---|---|---|
| 10 | 4 | +0x0B0 `has_global_refs` | Function accesses global memory |
| 21 | 2 | +0x0B2 `has_exit` | Function contains EXIT instruction |
| 22 | 2 | +0x0B3 `has_crs` | Function uses call return stack |
| 23 | 4 | +0x0A8 `max_register_count` | Register pressure (with max tracking) |
| 25 | 4 | +0x0B1 `has_shared_refs` | Function accesses shared memory |
| 27 | 3 | +0x000 `desc_version` | Descriptor format version stamp |
| 47 | 4 | +0x002 `instr_format_version` | Instruction encoding format version |
| 50 | 4 | +0x0E0 `register_count` | Allocated register count |
| 54 | 4 | +0x0B7 `has_texture_refs` | Function uses texture/sampler units |
| 67 | 4 | +0x0E8, +0x0F0 `barrier_info` | Barrier count and data |
| 72 | 4 | +0x0D0 `knobs_section_desc_ptr` | KNOBS section ELF binding |
| 73 | 3 | +0x0D8 `stack_frame_size` | Per-thread stack frame bytes |
| 74 | 2 | +0x070 `sampling_mode` | Interpolation/sampling mode |
| 88 | 3 | +0x0B4/B5/B6 | Atomic usage (plain/shared/global) |
| 90 | 4 | +0x138 `knobs_pair_list` | KNOBS key-value data block |
| 95 | 3 | +0x140, +0x142 | Min SM version + CRS depth flag |

## .nv.merc.* Section Builder Pipeline

Four functions cooperate to construct the `.nv.merc.*` section namespace:

```
sub_1C9F280 (97KB, Master ELF emitter)
  │
  ├─ sub_1C9B110 (23KB) ── Mercury capsule builder
  │   Creates .nv.merc namespace, reads symtab entry count,
  │   allocates mapping arrays, duplicates sections into merc space
  │
  ├─ sub_1CA2E40 (18KB) ── Mercury section cloner
  │   Iterates all sections, clones constant/global/shared/local
  │   into .nv.merc.* namespace, creates .nv.merc.rela sections,
  │   handles .nv.global.init and .nv.shared.reserved
  │
  ├─ sub_1C9C300 (24KB) ── Capsule descriptor processor
  │   Processes .nv.capmerc and .merc markers, embeds KNOBS,
  │   handles constant bank replication and rela duplication
  │
  ├─ sub_1CA3A90 (45KB) ── Section merger
  │   Merge/combine pass for sections with both merc and non-merc
  │   copies; processes .nv.constant bank sections, handles section
  │   linking and rela association
  │
  └─ sub_1C99BB0 (25KB) ── Section index remap
      Reindexes sections after dead elimination, handles
      .symtab_shndx / .nv.merc.symtab_shndx mapping
```

The section classifiers `sub_1C9D1F0` (16KB) and `sub_1C98C60` (9KB) map section names to internal type IDs. The former handles both SASS and merc debug section variants; the latter is merc-specific and recognizes all 15 `.nv.merc.debug_*` names.

## R_MERCURY_* Relocation Types

Capsule Mercury defines its own relocation type namespace for references within `.nv.merc.rela` sections and the capsule descriptor. These are distinct from standard CUDA ELF relocations (R_NV_32, etc.) and are processed during finalization rather than at link time.

| Type | Description |
|---|---|
| `R_MERCURY_ABS64` | 64-bit absolute address |
| `R_MERCURY_ABS32` | 32-bit absolute address |
| `R_MERCURY_ABS16` | 16-bit absolute address |
| `R_MERCURY_PROG_REL` | PC-relative reference |
| `R_MERCURY_8_0` | Sub-byte patch: bits [7:0] of target word |
| `R_MERCURY_8_8` | Sub-byte patch: bits [15:8] |
| `R_MERCURY_8_16` | Sub-byte patch: bits [23:16] |
| `R_MERCURY_8_24` | Sub-byte patch: bits [31:24] |
| `R_MERCURY_8_32` | Sub-byte patch: bits [39:32] |
| `R_MERCURY_8_40` | Sub-byte patch: bits [47:40] |
| `R_MERCURY_8_48` | Sub-byte patch: bits [55:48] |
| `R_MERCURY_8_56` | Sub-byte patch: bits [63:56] |
| `R_MERCURY_FUNC_DESC` | Function descriptor reference |
| `R_MERCURY_UNIFIED` | Unified address space reference |
| `R_MERCURY_TEX_HEADER_INDEX` | Texture header table index |
| `R_MERCURY_SAMP_HEADER_INDEX` | Sampler header table index |
| `R_MERCURY_SURF_HEADER_INDEX` | Surface header table index |

### Sub-Byte Relocation Design

The eight `R_MERCURY_8_*` types enable patching individual bytes within a 64-bit instruction word. Mercury instruction encodings pack multiple fields into single 8-byte QWORDs (the 1280-bit instruction buffer at `a1+544` is organized as 20 QWORDs). During finalization for a different SM, only certain bit-fields within an instruction word may need updating -- for example, the opcode variant bits or register class encoding -- while neighboring fields remain unchanged. The sub-byte types let the finalizer patch exactly one byte at a specific position within the word without a read-modify-write cycle on the entire QWORD.

### Relocation Resolution Algorithm -- `sub_1CD48C0`

The master resolver (22KB, 17 callees) walks the relocation linked list at `elfw+376` and applies five major stages per entry. Reconstructed pseudocode from the decompiled binary:

```
resolve_relocations(elfw):
    for each rela in linked_list(elfw+376):
        sym    = lookup_symbol(elfw, rela.r_sym)
        r_type = rela.r_info_lo            // low 32 bits of r_info

        // ---- Stage 1: Mercury vs CUDA table selection ----
        test_bit = (elfw.ei_class == 'A') ? 1 : 0x80000000
        is_mercury = (test_bit & elfw.flags) != 0
        if is_mercury:
            if r_type != 0 and r_type <= 0x10000:
                fatal("unexpected reloc")   // must be 0 or >0x10000
            table = &off_2407B60            // R_MERCURY table (65 entries)
            index = r_type - 0x10000        // strip Mercury namespace prefix
        else:
            table = &off_2408B60            // R_CUDA table (117 entries)
            index = r_type

        // ---- Stage 2: R_MERCURY_UNIFIED downgrade (compat path) ----
        //   When elfw+656 == 0 (no Mercury backend) and link_mode != 1,
        //   rewrite Mercury type codes to CUDA equivalents:
        //     103 -> 1    (UNIFIED -> ABS32)
        //     102 -> 2,  104..106 -> 76..78,  107..113 -> 79..83,56,57
        //   Also rewrites __UFT_OFFSET/__UDT_OFFSET symbols: type -> 0,
        //   sym replaced with symbol 0 (null section).

        // ---- Stage 3: Alias redirection ----
        sec = lookup_section(elfw, sym.st_shndx)
        parent = lookup_section(elfw, sec.sh_link)
        if sym.st_info_type == STT_SECTION and sym.st_value == 0:
            alias_target = sec.sh_link_target  // low 24 bits of sh_link
            if alias_target != rela.r_sym and parent.sh_type != 0x7000000E:
                // Redirect: resolve alias chain to true section
                debug("change alias reloc %s to %s\n", sym.name, new.name)
                rela.r_sym = alias_target
                sym = lookup_symbol(elfw, alias_target)

        // ---- Stage 4: Dead-function skip ----
        if sym.st_info_type == STT_FUNC:           // type 0xD
            shndx = get_shndx(elfw, sym)
            if shndx == 0 or shndx == 0xFFF2:      // SHN_UNDEF / SHN_ABS
                if sym.st_other_visibility == STV_DEFAULT:
                    unlink(rela); continue           // drop reloc silently
            if sym.st_info_bind == STB_LOCAL:        // binding == 1
                debug("ignore reloc on dead func %s\n", sym.name)
                rela.r_info_lo = 0; index = 0        // zero out type

        // ---- Stage 5: YIELD-to-NOP guard ----
        //   For r_type 68..69 (YIELD relocs), if forward_progress flag
        //   (elfw+94) is set, skip patching (preserve YIELD as-is):
        desc = table[index]
        if desc.patch_mode in {2, 3} and r_type in {68, 69}:
            if elfw.forward_progress:
                debug("Ignoring reloc to convert YIELD to NOP...")
                rela.r_info_lo = 0; index = 0

        // ---- Dispatch to bit-field patcher ----
        target_buf = find_patch_address(section, rela.r_offset)
        debug("resolve reloc %d sym=%d+%lld at <section=%d,offset=%llx>")
        if desc.patch_mode == 16:   // PC-relative
            assert rela.r_sec_idx == target_section  // same-section branch
        sub_1CD34E0(table, index, is_rel, target_buf,
                    sym.st_value, rela.r_offset, rela.r_addend,
                    sym.st_size, section.sh_type - 0x70000064)
```

The sub-byte `R_MERCURY_8_N` types (ordinals 4--11) use patch mode 6 in `sub_1CD34E0`, which extracts byte `N/8` from the computed value and writes it to the target offset without touching adjacent bytes. This avoids a read-modify-write on the full QWORD -- the descriptor's `bit_start` field encodes the byte position (0, 8, 16, ... 56) and `bit_width` is always 8.

## Mercury Section Binary Layouts

### Section Classifier Algorithm -- `sub_1C98C60`

The 9KB classifier uses a two-stage guard-then-waterfall pattern to identify `.nv.merc.*` sections from their ELF section headers.

**Stage 1: sh_type range check (fast rejection).** The section's `sh_type` is tested against two NVIDIA processor-specific ranges:

| Range | sh_type span | Decimal | Qualifying types |
|---|---|---|---|
| A | `0x70000006`..`0x70000014` | SHT_LOPROC+6..+20 | Filtered by bitmask `0x5D05` |
| B | `0x70000064`..`0x7000007E` | SHT_LOPROC+100..+126 | All accepted (memory-space data) |
| Special | `1` | SHT_PROGBITS | Accepted (generic debug data) |

Within Range A, the bitmask `0x5D05` (binary `0101_1101_0000_0101`) selects seven specific types:

| Bit | sh_type | Hex | Section types |
|---|---|---|---|
| 0 | SHT_LOPROC+6 | `0x70000006` | Memory-space clones |
| 2 | SHT_LOPROC+8 | `0x70000008` | `.nv.merc.nv.shared.reserved` |
| 8 | SHT_LOPROC+14 | `0x7000000E` | `.nv.merc.debug_line` |
| 10 | SHT_LOPROC+16 | `0x70000010` | `.nv.merc.debug_frame` |
| 11 | SHT_LOPROC+17 | `0x70000011` | `.nv.merc.debug_info` |
| 12 | SHT_LOPROC+18 | `0x70000012` | `.nv.merc.nv_debug_line_sass` |
| 14 | SHT_LOPROC+20 | `0x70000014` | `.nv.merc.debug_loc`, `.nv.merc.debug_ranges`, `.nv.merc.nv_debug_info_reg_*` |

**Stage 2: Name-based disambiguation (expensive path).** When `sh_flags` bit 28 (`0x10000000`, `SHF_NV_MERC`) is set, the classifier calls `sub_1CB9E50()` to retrieve the section name and performs sequential `strcmp()` against 15 names, returning 1 on the first match. The check order matches the declaration order in the ELF structure table above. `sub_4279D0` is used for `.nv.merc.nv_debug_ptx_txt` as a prefix match rather than exact match.

### SHF_NV_MERC Flag (`0x10000000`)

Bit 28 of `sh_flags` is an NVIDIA extension: **SHF_NV_MERC**. All `.nv.merc.*` sections carry this flag. It serves two purposes:

1. **Fast filtering** -- the classifier checks this bit before string comparisons, giving O(1) rejection for the common case of non-merc sections.
2. **Namespace separation** -- during section index remapping (`sub_1C99BB0`), sections with `SHF_NV_MERC` are remapped into a separate merc section index space. The finalizer uses this flag to identify which sections require relocation patching during off-target finalization.

### `.nv.capmerc<funcname>` -- Capsule Data Layout

The per-function capsule section contains the full marker stream, SASS data, KNOBS block, and optionally replicated constant bank data. The section is created by `sub_1C9C300`.

ELF section header:

| Field | Value |
|---|---|
| sh_type | `1` (SHT_PROGBITS) |
| sh_flags | `0x10000000` (SHF_NV_MERC) |
| sh_addralign | 16 |

Section data is organized as four consecutive regions:

```
         .nv.capmerc<funcname> Section Data
         ====================================

         ┌──────────────────────────────────────────────────────┐
         │ Marker Stream     (variable length)                  │
         │   Repeating TLV records:                             │
         │     [type:1B] [sub:1B] [payload:varies]              │
         │                                                      │
         │   Type 2: 4 bytes total   [02] [sub] [00 00]        │
         │     Boolean flags (has_exit, has_crs, sampling_mode) │
         │                                                      │
         │   Type 3: 4 bytes total   [03] [sub] [WORD:value]   │
         │     Short values (desc_version, stack_frame_size,    │
         │     atomic flags, min_sm_version)                    │
         │                                                      │
         │   Type 4: variable        [04] [sub] [WORD:size] ..  │
         │     Variable-length blocks (register counts, KNOBS   │
         │     data, barrier info, relocation payloads)         │
         │                                                      │
         │   Terminal marker: sub-type 95 (min_sm + CRS depth)  │
         ├──────────────────────────────────────────────────────┤
         │ SASS Data Block   (sass_data_size bytes)             │
         │   Mercury-encoded instruction bytes identical to     │
         │   what .text.<func> would contain for the compile    │
         │   target; byte-for-byte match with phase 122 output  │
         ├──────────────────────────────────────────────────────┤
         │ KNOBS Block       (knobs_section_size bytes)         │
         │   Serialized key-value pairs from marker sub-type 90 │
         │   "KNOBS" tag separates knob pairs from generic KV   │
         │   Contains: optimization level, target parameters,   │
         │   feature flags, all codegen-affecting knob values   │
         ├──────────────────────────────────────────────────────┤
         │ Constant Bank Data (const_bank_size bytes, optional) │
         │   Replicated .nv.constant0 data for deferred binding │
         │   Only present when the function references constant │
         │   bank data that the finalizer may need to patch     │
         └──────────────────────────────────────────────────────┘
```

### `.nv.merc.debug_info` -- Cloned DWARF Debug Info

The cloner (`sub_1CA2E40`) produces a byte-for-byte copy of the source `.debug_info` section, placed into the merc namespace with modified ELF section header properties.

ELF section header:

| Field | Value |
|---|---|
| sh_type | `0x70000011` (SHT_LOPROC + 17) |
| sh_flags | `0x10000000` (SHF_NV_MERC) |
| sh_addralign | 1 |

Section data is standard DWARF `.debug_info` format:

```
         .nv.merc.debug_info Section Data
         ==================================

         ┌──────────────────────────────────────────────────────┐
         │ Compilation Unit Header                              │
         │   +0x00  unit_length    : 4B (DWARF-32) or 12B (-64)│
         │   +0x04  version        : 2B (typically DWARF 4)    │
         │   +0x06  debug_abbrev_offset : 4B → .nv.merc.debug_abbrev │
         │   +0x0A  address_size   : 1B (8 for 64-bit GPU)    │
         ├──────────────────────────────────────────────────────┤
         │ DIE Tree (Debug Information Entries)                  │
         │   Sequence of entries, each:                         │
         │     abbrev_code  : ULEB128                           │
         │     attributes   : per abbreviation definition       │
         │                                                      │
         │   Cross-section references (via relocations):        │
         │     DW_FORM_strp     → .nv.merc.debug_str            │
         │     DW_FORM_ref_addr → .nv.merc.debug_info           │
         │     DW_FORM_sec_offset → .nv.merc.debug_line etc.    │
         └──────────────────────────────────────────────────────┘
```

The critical difference from standard `.debug_info`: all cross-section offset references point to other `.nv.merc.*` sections, not the original `.debug_*` sections. The `.nv.merc.rela.debug_info` relocation table handles rebinding these offsets during finalization.

### `.nv.merc.rela` / `.nv.merc.rela<secname>` -- Mercury Relocations

Mercury relocation sections use standard `Elf64_Rela` on-disk format (24 bytes per entry) but encode Mercury-specific relocation types with a `0x10000` offset in the type field.

ELF section header:

| Field | Value |
|---|---|
| sh_type | `4` (SHT_RELA) |
| sh_flags | `0x10000000` (SHF_NV_MERC) |
| sh_addralign | 8 |
| sh_entsize | 24 |
| sh_link | symtab section index |
| sh_info | target section index |

Section names are constructed by `sub_1C980F0` as `".nv.merc.rela"` + suffix (e.g., `".nv.merc.rela.debug_info"`).

On-disk entry layout (standard `Elf64_Rela`, 24 bytes):

```
         .nv.merc.rela Entry (24 bytes on disk)
         ========================================

         ┌─────────┬──────┬────────────────────────────────────────────┐
   0x00  │ QWORD   │  8B  │ r_offset — byte position in target section │
   0x08  │ DWORD   │  4B  │ r_type — relocation type                   │
         │         │      │   Standard: 1=R_NV_ABS64, etc.             │
         │         │      │   Mercury:  r_type > 0x10000               │
         │         │      │   Decoded:  r_type - 0x10000 → R_MERCURY_* │
   0x0C  │ DWORD   │  4B  │ r_sym — symbol table index                 │
   0x10  │ QWORD   │  8B  │ r_addend — signed addend value             │
         └─────────┴──────┴────────────────────────────────────────────┘
```

During resolution (`sub_1CD48C0`), the 24-byte on-disk entries are loaded into a 32-byte in-memory representation that adds two section index fields:

```
         In-Memory Relocation Entry (32 bytes)
         =======================================

         ┌─────────┬──────┬────────────────────────────────────────────┐
   0x00  │ QWORD   │  8B  │ r_offset — byte position in target section │
   0x08  │ DWORD   │  4B  │ r_type — relocation type                   │
   0x0C  │ DWORD   │  4B  │ r_sym — symbol table index                 │
   0x10  │ QWORD   │  8B  │ r_addend — signed addend value             │
   0x18  │ DWORD   │  4B  │ r_sec_idx — target section index           │
   0x1C  │ DWORD   │  4B  │ r_addend_sec — addend section index        │
         └─────────┴──────┴────────────────────────────────────────────┘
```

The extra 8 bytes enable cross-section targeting: `r_sec_idx` identifies which section `r_offset` is relative to, and `r_addend_sec` identifies the section contributing the addend base address. When `r_addend_sec != 0`, the resolver adds that section's load address to `r_offset` before patching.

The resolver's Mercury/CUDA table selection and five-stage dispatch algorithm are documented in [Relocation Resolution Algorithm](#relocation-resolution-algorithm----sub_1cd48c0) above.

### Complete sh_type Map

| sh_type | Hex | Section types |
|---|---|---|
| 1 | `0x00000001` | `.nv.capmerc<func>`, `.nv.merc.debug_abbrev` (PROGBITS variant), `.nv.merc.debug_str`, `.nv.merc.nv_debug_ptx_txt` |
| 4 | `0x00000004` | `.nv.merc.rela*` (SHT_RELA) |
| SHT_LOPROC+6 | `0x70000006` | `.nv.merc.<memory-space>` clones |
| SHT_LOPROC+8 | `0x70000008` | `.nv.merc.nv.shared.reserved` |
| SHT_LOPROC+14 | `0x7000000E` | `.nv.merc.debug_line` |
| SHT_LOPROC+16 | `0x70000010` | `.nv.merc.debug_frame` |
| SHT_LOPROC+17 | `0x70000011` | `.nv.merc.debug_info` |
| SHT_LOPROC+18 | `0x70000012` | `.nv.merc.nv_debug_line_sass` |
| SHT_LOPROC+20 | `0x70000014` | `.nv.merc.debug_loc`, `.nv.merc.debug_ranges`, `.nv.merc.nv_debug_info_reg_sass`, `.nv.merc.nv_debug_info_reg_type` |
| SHT_LOPROC+100..+126 | `0x70000064`..`0x7000007E` | Memory-space variant sections (constant banks, shared, local, global) |

The `.nv.merc.*` debug sections reuse the same `sh_type` values as their non-merc counterparts (`.debug_info` uses `0x70000011` in both namespaces). The `SHF_NV_MERC` flag (`0x10000000`) in `sh_flags` is the distinguishing marker.

## Self-Check Mechanism

The `--self-check` flag activates a roundtrip verification that validates the capmerc encoding by reconstituting SASS from the capsule data and comparing it against the original:

```
Phase 122 output (SASS) ──────────────────────────> reference SASS
         │
         └─ capmerc packaging ─> .nv.capmerc<func>
                                        │
                                        └─ reconstitute ─> reconstituted SASS
                                                                │
                                                    section-by-section compare
                                                                │
                                                      pass / fail (error 17/18/19)
```

The reconstitution pipeline uses `sub_720F00` (64KB), a Flex-generated SASS text lexer with thread-safety support (pthread_mutexattr_t), to parse the reconstituted instruction stream. `sub_729540` (35KB) performs the actual section-by-section comparison.

Three error codes signal specific self-check failures:

| Error code | Meaning |
|---|---|
| 17 | Section content mismatch (instruction bytes differ) |
| 18 | Section count mismatch (missing or extra sections) |
| 19 | Section metadata mismatch (size, alignment, or flags differ) |

These error codes trigger `longjmp`-based error recovery in the master ELF emitter (`sub_1C9F280`), which uses `_setjmp` at its entry point for non-local error handling.

The `--out-sass` flag causes ptxas to dump the reconstituted SASS to a file, useful for debugging self-check failures by manual comparison with the original SASS output.

## Opportunistic Finalization

The `--opportunistic-finalization-lvl` flag controls how aggressively capmerc binaries may be finalized for a target SM different from the compilation target:

| Level | Name | Behavior |
|---|---|---|
| 0 | default | Standard finalization for the compile target only |
| 1 | none | No finalization; output stays as capmerc (deferred to driver) |
| 2 | intra-family | Finalize for any SM within the same architectural family |
| 3 | intra+inter | Finalize across SM families |

Level 2 allows a capmerc binary compiled for sm_100 (datacenter Blackwell) to be finalized for sm_103 (Blackwell Ultra / GB300) without recompilation. Level 3 extends this across families -- for example, sm_100 capmerc finalized for sm_120 (consumer RTX 50-series).

The key constraint is instruction encoding compatibility: the sub-byte R_MERCURY_8_* relocations can patch SM-specific encoding bits, but the overall instruction format and register file layout must be compatible between source and target.

## Off-Target Finalization

Off-target finalization converts a capmerc binary compiled for SM X into native SASS for SM Y. The compatibility checker `sub_60F290` gates this with a two-phase test: family normalization followed by capability bitmask validation.

### Compatibility checker (`sub_60F290`)

```
fn can_finalize_offtarget(ctx, source_sm, target_sm, cross_family_ok) -> bool:
    // Phase 1: normalize SM number to family code (ASCII-range integer)
    family_of(sm) :=
        104 -> 120    // sm_104 alias -> Blackwell-x family ('x')
        130 -> 107    // sm_130       -> family 'k'
        101 -> 110    // sm_101       -> family 'n'
        _   -> sm     // sm_100=100('d'), sm_103=103('g'), sm_121=121('y'), etc.
    src_fam = family_of(source_sm)
    tgt_fam = family_of(target_sm)

    // Phase 2: family match -- special routing for Blackwell cross-aliases
    // sm_101/sm_130 targeting sm_104 (all normalize to Blackwell base) pass directly
    if src_fam == tgt_fam:
        result = cross_family_ok
    else:
        result = cross_family_ok AND (src_fam == tgt_fam)   // redundant, always false
    if !result:
        // fallback: check capability descriptor presence at ctx+24
        result = cross_family_ok AND (ctx.cap_word_present != 0)
        if !result: return false

    // Phase 3: target capability bitmask (ctx+16 -> dereferenced DWORD*)
    cap_flags = *ctx.cap_flags_ptr
    if cap_flags == NULL: return false
    required_bits = { 100 -> 0x01,    // sm_100 datacenter Blackwell
                      103 -> 0x08,    // sm_103 Blackwell Ultra / GB300
                      110 -> 0x02,    // sm_110 Jetson Thor
                      121 -> 0x40 }   // sm_121 DGX Spark
    if tgt_fam not in required_bits: return false
    return (cap_flags & required_bits[tgt_fam]) == required_bits[tgt_fam]
```

The `CAN_FINALIZE_DEBUG` environment variable enables verbose tracing of the decision (read via `getenv`/`strtol` at entry; the parsed value gates logging in callers, not the decision itself).

### Kernel finalizer fastpath (`sub_612DE0`)

When the check passes, the finalizer patches the capsule in-place rather than re-running phases 117--122. On success ptxas emits:

```
"[Finalizer] fastpath optimization applied for off-target %u -> %u finalization"
```

The 5-step fastpath sequence (lines 830--879 of the decompilation):

```
fn finalizer_fastpath(capsule_buf, source_sm, target_sm, out_ptr, out_size):
    // 1. Parse capsule -- extract .text byte count from capsule descriptor
    text_size = get_capsule_text_size(capsule_buf)          // sub_1CAFC60
    output = allocate_zeroed(text_size)                     // sub_424070
    memcpy(output, capsule_buf, text_size)

    // 2. Validate -- source SM extracted from capsule header field at +48:
    //    format 'A' (0x41): sm = dword_48 >> 8   (16-bit field)
    //    other:              sm = dword_48 & 0xFF (8-bit field)
    //    sub_60F290 already returned true before entering this path

    // 3. Patch target SM into capsule header
    hdr = parse_capsule_header(output)                      // sub_1CB9C30
    if hdr.format == 'A':
        hdr.dword_48 = (target_sm << 8) | (hdr.dword_48 & 0xFF0000FF)
    else:
        hdr.dword_48 = target_sm | (hdr.dword_48 & 0xFFFFFF00)

    // 4. Emit patched .text -- output replaces the .text section content
    *out_ptr  = output
    *out_size = text_size
    // R_MERCURY_* relocations resolved by relocation engine against
    // target SM encoding tables (see Relocation Resolution Algorithm)

    // 5. Falls through to standard EIATTR builder for target SM
    //    (register counts, barrier counts, max stack size rewritten)
    goto cleanup    // LABEL_20 -- skip full 122-phase compilation
```

If the fastpath fires, execution jumps directly to `LABEL_20` (cleanup epilogue), bypassing the full compilation setup: mutex acquisition, per-kernel state initialization, the 122-phase pipeline, and ELF section construction. The full path is only taken when `sub_60F290` returns false or when `--opportunistic-finalization-lvl` is 0.

## Pipeline Integration

Capmerc does not modify the Mercury encoder pipeline (phases 113--122). The instruction encoding, pseudo-instruction expansion, WAR hazard resolution, operation expansion (opex), and SASS microcode emission all execute identically regardless of output mode. The divergence happens after phase 122 completes:

| Mode | Post-Pipeline Behavior |
|---|---|
| Mercury | Phase 122 SASS output written directly to `.text.<func>` ELF section |
| Capmerc | Phase 122 output wrapped in 328-byte capsule descriptor; `.nv.merc.*` sections cloned; R_MERCURY_* relocations emitted; KNOBS data embedded |
| SASS | Phase 122 output written as raw SASS binary (no ELF wrapper) |

The master ELF emitter `sub_1C9F280` (97KB) orchestrates the post-pipeline divergence:

```c
// Simplified from sub_1C9F280
void EmitELF(context) {
    // Common: copy ELF header (64 bytes via SSE loadu)
    memcpy(output, &elf_header, 64);

    // Common: iterate sections, build section headers
    for (int i = 0; i < section_count; i++) {
        if (section[i].flags & 4) continue;  // skip virtual sections
        // ... copy section data, patch headers ...
    }

    if (is_capmerc_mode) {
        sub_1C9B110(ctx);   // create .nv.merc namespace
        sub_1CA2E40(ctx);   // clone sections into merc space
        sub_1C9C300(ctx);   // build capsule descriptors + KNOBS
        sub_1CA3A90(ctx);   // merge merc/non-merc section copies
    }

    // Common: remap section indices, build symbol table
    sub_1C99BB0(ctx);       // section index remap
    sub_1CB68D0(ctx);       // build .symtab

    // Common: resolve relocations
    sub_1CD48C0(ctx);       // relocation resolver (handles R_MERCURY_*)

    // Common: finalize and write
    sub_1CD13A0(ctx);       // serialize to file
}
```

## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_1C9F280` | 97KB | Master ELF emitter (orchestrates full CUBIN output) |
| `sub_1CA3A90` | 45KB | Section merger / combined section emitter |
| `sub_1CB68D0` | 49KB | Symbol table builder (handles merc section references) |
| `sub_1C99BB0` | 25KB | Section index remap (`.symtab_shndx` / `.nv.merc.symtab_shndx`) |
| `sub_1C9C300` | 24KB | Capsule descriptor processor (328-byte object, KNOBS embed) |
| `sub_1C9B110` | 23KB | Mercury capsule builder (creates `.nv.merc` namespace) |
| `sub_1CD48C0` | 22KB | Master relocation resolver (R_MERCURY_* + standard) |
| `sub_1CA2E40` | 18KB | Mercury section cloner |
| `sub_1C9D1F0` | 16KB | Debug section classifier (SASS + merc variants) |
| `sub_1C98C60` | 9KB | Mercury debug section classifier (15 section names) |
| `sub_720F00` | 64KB | Flex SASS text lexer (self-check reconstitution) |
| `sub_729540` | 35KB | SASS assembly verification (self-check comparator) |
| `sub_703AB0` | 10KB | Binary-kind CLI parser |
| `sub_612DE0` | 47KB | Kernel finalizer / ELF builder (fastpath optimization) |
| `sub_60F290` | -- | Off-target compatibility checker |
| `sub_1CD13A0` | 11KB | ELF serialization (final file writer) |

## Cross-References

- [Mercury Encoder Pipeline](./mercury.md) -- phases 113--122, the upstream encoding that capmerc wraps
- [SASS Instruction Encoding](./encoding.md) -- bit-level encoding format and 1280-bit instruction buffer
- [Code Generation Overview](./overview.md) -- high-level codegen pipeline context
- [Knobs System](../config/knobs.md) -- knob infrastructure that KNOBS embedding serializes
- [Phase Manager](../passes/phase-manager.md) -- 159-phase pipeline infrastructure
- [SM Architecture Map](../targets/index.md) -- SM version numbers and family groupings
