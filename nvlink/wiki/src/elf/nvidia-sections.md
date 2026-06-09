# NVIDIA Section Types

CUDA device ELF objects (cubins) use processor-specific ELF section types, flags, and naming conventions that extend the standard ELF format to describe GPU memory spaces, kernel metadata, constant banks, shared memory, unified descriptor/function tables, and debug information. This page catalogs every NVIDIA-specific section name identified in nvlink v13.0.88, organized by functional category, with section type codes, creation and processing function addresses, content format details, and the linker operations that consume them.

## Section Type Constants

CUDA defines custom section types in the `SHT_LOPROC`--`SHT_HIPROC` range (`0x70000000`--`0x7FFFFFFF`). nvlink uses these types internally after reclassifying input sections based on their names. The reclassification happens in `merge_elf` (`sub_45E7D0`): input sections arrive with standard ELF types (`SHT_PROGBITS`, `SHT_NOBITS`), and the linker maps them to CUDA-specific types for dispatch.

### NVIDIA CUDA-Specific Types

The table below lists every `sh_type` value actually passed to `section_create` (`sub_441AC0`). The symbolic constant names (`SHT_CUDA_*`) are synthetic — NVIDIA does not publish a public header with these identifiers — but the numeric values are verbatim from the decompiled function bodies.

| Type constant | Value (hex) | Value (dec) | Section(s) | Creation site |
|---|---|---|---|---|
| `SHT_CUDA_INFO` | `0x70000000` | 1,879,048,192 | `.nv.info`, `.nv.info.<func>` | `sub_4504B0:46`, `sub_4504B0:63` |
| `SHT_CUDA_CALLGRAPH` | `0x70000001` | 1,879,048,193 | `.nv.callgraph` | `sub_44D200:102` |
| `SHT_CUDA_PROTOTYPE` | `0x70000002` | 1,879,048,194 | `.nv.prototype` | `sub_44D9D0:25` |
| `SHT_CUDA_RESOLVED_RELA` | `0x70000003` | 1,879,048,195 | `.nv.resolvedrela` | `sub_469230:151` |
| `SHT_CUDA_METADATA` | `0x70000004` | 1,879,048,196 | `.nv.metadata` | `sub_43D6B0:31` |
| `SHT_CUDA_CONSTANT` | `0x70000006` | 1,879,048,198 | (generic placeholder) | Range check in `sub_441AC0:190` |
| `SHT_CUDA_GLOBAL` | `0x70000007` | 1,879,048,199 | `.nv.global` | `sub_436410:128`, `sub_439830:494` |
| `SHT_CUDA_GLOBAL_INIT` | `0x70000008` | 1,879,048,200 | `.nv.global.init` | `sub_436740:105` |
| `SHT_CUDA_LOCAL` | `0x70000009` | 1,879,048,201 | `.nv.local.<func>` | `sub_436310:31` |
| `SHT_CUDA_SHARED` | `0x7000000A` | 1,879,048,202 | `.nv.shared.<func>` | `sub_436A80:41` |
| `SHT_CUDA_RELOCINFO` | `0x7000000B` | 1,879,048,203 | `.nv.rel.action` | `sub_469D60:913` |
| `SHT_CUDA_UFT` | `0x7000000E` | 1,879,048,206 | `.nv.uft`, `.nv.uft.rel.<func>` | `sub_442820:73` |
| `SHT_CUDA_UFT_ENTRY` | `0x70000011` | 1,879,048,209 | `.nv.uft.entry` | `sub_4438F0:579`, `sub_464240:15` |
| `SHT_CUDA_UDT` | `0x70000012` | 1,879,048,210 | `.nv.udt` | `sub_436740:76`, `sub_436410:94` |
| `SHT_CUDA_UDT_ENTRY` | `0x70000014` | 1,879,048,212 | `.nv.udt.entry` | `sub_464320:15` |
| `SHT_CUDA_SHARED_RESERVED` | `0x70000015` | 1,879,048,213 | `.nv.reservedSmem*` | `sub_4379A0:49`, `sub_437BB0:70` |
| `SHT_CUDA_CONSTANT0`..`17` | `0x70000064`..`0x70000075` | 1,879,048,292..1,879,048,309 | `.nv.constant0`..`.nv.constant17` | Bank formula, range check in `sub_441AC0:190` |
| `SHT_CUDA_COMPAT` | `0x70000086` | 1,879,048,326 | `.nv.compat` | `sub_451BA0:64`, `sub_451920:113` |
| `SHT_CUDA_HOST` | `0x70000087` | 1,879,048,327 | `.nv.host` | `sub_435B60:110` |

**Value gaps:** Identifiers `0x70000005`, `0x7000000C`, `0x7000000D`, `0x7000000F`, `0x70000010`, `0x70000013` are not used in any observed `sub_441AC0` call site. The generic constant placeholder `0x70000006` appears in the range check `(sh_type - 0x70000064) <= 0x1A` at `sub_441AC0:190` but no section is ever created with that type as its final value. `.nv.compat` and `.nv.host` jump to the `0x70000086`--`0x70000087` block; the reason for this 109-value gap is not clear from the binary.

The constant bank type for bank N is `0x70000064 + N`. The bank number is parsed from the section name suffix by `strtol(name + 12, NULL, 10)`, so `.nv.constant0` maps to `0x70000064` and `.nv.constant17` to `0x70000075`. The range check `(sh_type - 0x70000064) <= 0x1A` accepts up to 27 possible bank types (0x70000064 through 0x7000007E), though only 18 (banks 0–17) have corresponding name strings in the binary.

### Section Flags

In addition to standard ELF flags (`SHF_WRITE`, `SHF_ALLOC`, `SHF_EXECINSTR`), CUDA uses the `SHF_MASKPROC` range:

| Flag | Value | Meaning |
|---|---|---|
| `SHF_CUDA_MERCURY` | `0x10000000` | Mercury-format section (sm100+). Bit 28 of `sh_flags`. Merge phase skips these sections and defers to FNLZR. |

The per-function flag `SHF_INFO_LINK` (`0x40`) is set on `.nv.info.<func>` sections to indicate that `sh_link` references the owning function's symbol table entry. The `sub_4504B0` creator passes `0x40` as the flags argument when creating per-function `.nv.info` sections (line 63) but passes `0` for the global `.nv.info` section (line 46).

---

## Section-by-Section Deep Dive: NVIDIA Custom Types

This section documents each of the five core NVIDIA-specific section types that use custom `sh_type` values in the `0x70000000`--`0x70000004` range. For each section: the exact creation path, the content format, the processing/reading functions, when it is created in the pipeline, and size characteristics.

### .nv.info — Kernel Metadata (SHT_CUDA_INFO, 0x70000000)

The `.nv.info` section is the most important NVIDIA ELF metadata section. It encodes per-kernel resource requirements as TLV (Type-Length-Value) records using the EIATTR (ELF Info ATTRibute) format. Without this metadata, the GPU driver cannot launch a kernel — it provides register counts, stack sizes, barrier counts, parameter layouts, and dozens of other resource descriptors.

| Property | Value |
|---|---|
| `sh_type` | `0x70000000` (1,879,048,192) |
| Section name (global) | `.nv.info` |
| Section name (per-function) | `.nv.info.<function_name>` |
| Creator function | `sub_4504B0` (72 decompiled lines) |
| Inline TLV parser (merge) | `sub_45E7D0` (case `sh_type == 0x70000000`, decompiled line ~1854 onward) |
| Inline TLV parser (finalize) | `sub_451D80` (`compute_entry_properties`, walks `elfw+392` list) |
| Debug dumper | `sub_4478F0` (prints `"nvinfo <fmt=%d,attr=%d,size=%d>, secidx=%d"`) |
| Property computer | `sub_451D80` (`compute_entry_properties`, 3,029 decompiled lines) |
| `sh_entsize` | 4 |
| `sh_addralign` | 0 |
| `sh_flags` | `0x00` (global) or `0x40` (`SHF_INFO_LINK`, per-function) |
| Record format | TLV: `[format:1][attr_code:1][size:2][payload:var]`, 4-byte aligned |
| Known attribute count | 97 EIATTR codes (0–96) in nvlink v13.0.88 |

**Creation path.** `sub_4504B0` (at `0x4504B0`) serves as the `.nv.info` section factory. It takes two arguments: the ELF context (`a1`) and a function section index (`a2`). The creation logic branches:

- **Global `.nv.info`** (`a2 == 0`): Calls `sub_4411D0` to look up an existing `.nv.info` section by name. If not found, calls `sub_441AC0(ctx, ".nv.info", 1879048192, 0, shndx, 0, 4, 0)` to create it (line 46). Flags are `0` — no `SHF_INFO_LINK`.

- **Per-function `.nv.info.<name>`** (`a2 != 0`): Resolves the function symbol name via `sub_440590` + `sub_440350`, then constructs the per-function section name via `sprintf(buf, "%s.%s", ".nv.info", func_name)`. Calls `sub_441AC0(ctx, buf, 1879048192, 0x40, shndx, func_idx, 4, 0)` (line 63). The `0x40` flag sets `SHF_INFO_LINK`, and the sixth argument (`func_idx`) becomes `sh_info`, linking the section to its owning function. After creation, `sub_4426D0` (line 66) registers the section-to-function association.

**Content format.** Each record is a 4-byte-aligned TLV entry:

```text
Offset  Size  Field
------  ----  -----
0x00    1     format      EIFMT byte (0x01=free, 0x02=value, 0x03=sized, 0x04=indexed)
0x01    1     attr_code   EIATTR type code (0x00–0x60)
0x02    2     size        Payload size in bytes (little-endian u16)
0x04    var   payload     Attribute data (padded to 4-byte alignment)
```

Format `0x04` (indexed) is the most common: it carries `[sym_index:4][value:4]` pairing a symbol with a property value. Format `0x01` (free) is used for variable-length data like parameter info structures and instruction-offset arrays.

**When created.** Global `.nv.info` is created during the merge phase when the first input `.nv.info` section is encountered. Per-function sections are created on demand as functions are merged. The output `.nv.info` sections are populated during the finalization phase by `compute_entry_properties` (`sub_451D80`), which walks every entry point and encodes its computed properties (propagated register counts, barrier counts, stack sizes, etc.) as EIATTR records.

**Processing path.** During merge, the inline TLV parser in `sub_45E7D0` (entered when `sh_type == 0x70000000`) reads incoming TLV records and dispatches on the attribute code. The linker extracts critical values:
- `EIATTR_REGCOUNT` (0x2F): Register count for weak-symbol resolution
- `EIATTR_MAX_STACK_SIZE` (0x23): Stack size for propagation through the call graph
- `EIATTR_MIN_STACK_SIZE` (0x12): Minimum stack for call-graph accumulation
- `EIATTR_CRS_STACK_SIZE` (0x1E): Call/return stack size

The parser logs each record as: `"nvinfo <fmt=%d,attr=%d,size=%d>, secidx=%d"`.

**Size characteristics.** A simple kernel's `.nv.info.<name>` section is typically 100–400 bytes. Complex kernels with many parameters, texture bindings, and barrier usage can reach 2–4 KB. The global `.nv.info` section is typically 20–100 bytes per compilation unit.

### .nv.callgraph — Call Edge Table (SHT_CUDA_CALLGRAPH, 0x70000001)

The `.nv.callgraph` section records caller-callee relationships between device functions. The linker uses this for dead code elimination, stack size propagation, and register count propagation from callees to callers.

| Property | Value |
|---|---|
| `sh_type` | `0x70000001` (1,879,048,193) |
| Section name | `.nv.callgraph` |
| Creator function | `sub_44D200` (`build_callgraph_section`, 368 decompiled lines) |
| Reader/fixup function | `sub_44CA40` (`fixup_callgraph`, 110 decompiled lines) |
| Dump function | `sub_44CE00` (outputs `"callgraph for sm_%d:"`) |
| DOT dump function | `sub_44CCF0` (outputs `"digraph callgraph {"`) |
| Consumer (DCE) | `sub_44AD40` (`dead_code_elimination`, 689 decompiled lines) |
| `sh_entsize` | 4 |
| `sh_addralign` | 8 |

**Creation path.** `sub_44D200` (`build_callgraph_section`) at address `0x44D200` creates the `.nv.callgraph` section in a single call: `sub_441AC0(ctx, ".nv.callgraph", 1879048193, 0, shndx, 0, 4, 8)` (line 102). The section is populated immediately after creation with a structured sequence of 8-byte edge records. The builder:

1. Emits a **sentinel entry** `{0, -1}` (0x00000000, 0xFFFFFFFF) as the first record.
2. If there are 2 or fewer functions, emits sentinel `{0, -2}` and `{0, -3}` immediately.
3. Otherwise, iterates all function pairs in the call graph: for each caller-callee edge, emits `{caller_symidx, callee_symidx}` as an 8-byte record via `sub_433760` (the data-copy primitive).
4. Emits separator sentinels `{0, -2}` and `{0, -3}` to delimit edge-list sections.
5. Iterates device-function export edges and function-pointer indirect-call targets, emitting additional 8-byte records.
6. Emits final sentinel `{0, -4}` to terminate the call graph.

**Content format.** A flat sequence of 8-byte records, each containing two 32-bit symbol indices:

```text
Offset  Size  Field
------  ----  -----
0x00    4     field_0    Symbol index (caller, or 0 for sentinel)
0x04    4     field_1    Symbol index (callee, or negative for sentinel)
```

Sentinel values partition the record stream into four logical sections:
- `{0, -1}`: Start of call graph
- Edge records: `{caller, callee}` pairs
- `{0, -2}`: End of direct call edges, start of device-function entries
- Device function records: `{func, export_id}` pairs
- `{0, -3}`: End of device functions, start of indirect-call targets
- Indirect-call records: `{func, target}` pairs
- `{0, -4}`: End of call graph

**When created.** Built during the finalization phase, after all functions have been merged and symbol indices have been assigned. The function is guarded by `"callgraph not complete"` assertions in multiple consumers, and `"adding function after callgraph completed"` errors are issued if a function section is created too late.

**Processing path.** The fixup function `sub_44CA40` remaps symbol indices after link-time symbol renumbering. It looks up `.nv.callgraph` by name via `sub_4411D0` (line 75) and walks the data nodes, calling `sub_444720` to translate each symbol index from input-local to output-global. If the section is not found when needed, it fatals with `"callgraph not found"`.

The dead code eliminator `sub_44AD40` consumes the call graph to determine reachability. For each unreachable function, it removes the `.text.<func>`, `.nv.info.<func>`, `.nv.local.<func>`, `.nv.shared.<func>`, and associated `.rela.*` sections, printing `"removed un-used section %s (%d)"` for each (8 distinct removal sites in the decompiled code).

**Diagnostic options.** The `--dump-callgraph` flag causes `sub_44CE00` to print the call graph to stderr. The `--dump-callgraph-no-demangle` variant skips C++ name demangling. The `--callgraph-file` flag writes the call graph in DOT format via `sub_44CCF0`.

**Size characteristics.** Proportional to the number of call edges: 8 bytes per edge plus 32 bytes of sentinels. A module with 100 functions and 200 edges produces approximately 1,632 bytes.

### .nv.prototype — Launch Prototype Descriptors (SHT_CUDA_PROTOTYPE, 0x70000002)

The `.nv.prototype` section describes the parameter layout and launch configuration for each `__global__` entry function. The CUDA driver reads this section to validate kernel launch parameters.

| Property | Value |
|---|---|
| `sh_type` | `0x70000002` (1,879,048,194) |
| Section name | `.nv.prototype` |
| Creator function | `sub_44D9D0` (`build_prototype_section`, 63 decompiled lines) |
| Reader/fixup function | `sub_44CBC0` (`fixup_prototype`, 54 decompiled lines) |
| `sh_entsize` | 4 |
| `sh_addralign` | 8 |

**Creation path.** `sub_44D9D0` at address `0x44D9D0` creates the section via `sub_441AC0(ctx, ".nv.prototype", 1879048194, 0, shndx, 0, 4, 8)` (line 25). After creation, it iterates all function entries in the function table (`a1+408`), starting from index 1 (index 0 is reserved). For each function entry where:
- The function has a non-zero prototype descriptor (field at offset +4)
- The function is not marked as an internal stub (byte at offset +50 is zero)

...it emits an 8-byte prototype record `{func_symidx, proto_descriptor}` via `sub_433760`.

**Content format.** A flat sequence of 8-byte records:

```text
Offset  Size  Field
------  ----  -----
0x00    4     func_symidx    Symbol index of the entry function
0x04    4     proto_desc     Prototype descriptor (parameter layout encoding)
```

The prototype descriptor is a compact encoding of the kernel's parameter count, sizes, and alignment requirements. It is opaque to the linker — nvlink copies it verbatim from input to output. The driver decodes it at kernel launch time to validate that the caller provides the correct number and types of arguments.

**When created.** Built during the finalization phase, after all functions have been merged. Like the callgraph, it requires that the function table is complete.

**Processing path.** The fixup function `sub_44CBC0` remaps symbol indices in the prototype section after symbol renumbering. It looks up `.nv.prototype` by name via `sub_4411D0` (line 23). For each record, it translates the function symbol index using the input-to-output mapping tables at `a1+456` (positive indices, ELF1) and `a1+464` (negative indices, ELF2). If the section does not exist (no entry functions), the fixup is silently skipped.

**Size characteristics.** 8 bytes per `__global__` entry function. A module with 10 entry kernels produces 80 bytes.

### .nv.resolvedrela — Preserved Relocations (SHT_CUDA_RESOLVED_RELA, 0x70000003)

The `.nv.resolvedrela` section preserves relocations after linking for driver-side patching. It is only created when the `--preserve-relocs` flag is specified (checked via the byte at `a1+85` in the ELF context).

| Property | Value |
|---|---|
| `sh_type` | `0x70000003` (1,879,048,195) |
| Section name pattern | `.nv.resolvedrela<section_name>` |
| Creator function | `sub_469230` (154 decompiled lines) |
| Emitter function | `sub_46ADC0` (`emit_resolved_relocations`, 406 decompiled lines) |
| `sh_entsize` | 4 or 8 (ELF32 vs ELF64) |
| `sh_addralign` | 12 or 24 (ELF32 vs ELF64) |
| `sh_flags` | `0x40` (`SHF_INFO_LINK`) |

**Creation path.** `sub_469230` at address `0x469230` is the relocation section creator. It handles three section types (REL, RELA, and resolved-RELA) in a single function. The creation of `.nv.resolvedrela` sections is conditional on `*(_BYTE *)(a1 + 85)` — the `--preserve-relocs` flag. When active, for each input relocation section, the function:

1. Constructs the resolved relocation section name via `sprintf(buf, "%s%s", ".nv.resolvedrela", original_section_name)`.
2. Calls `sub_441AC0(ctx, buf, 1879048195, 0x40, shndx, target_sec, entsize, addend_size)` (line 151).

The entry size and alignment depend on ELF class: for ELF64 (`a1+4 == 2`), `sh_entsize=8` and `sh_addralign=24`; for ELF32, `sh_entsize=4` and `sh_addralign=12`.

**Content format.** Standard `Elf32_Rela` or `Elf64_Rela` records (matching the ELF class), identical in format to `.rela.*` sections but containing relocations that have been partially or fully resolved by the linker. The driver must apply these relocations at load time to account for base address randomization and other runtime adjustments.

```text
Elf64_Rela:
  Offset  Size  Field
  ------  ----  -----
  0x00    8     r_offset     Byte offset within the target section
  0x08    8     r_info       Symbol index (upper 32) + relocation type (lower 32)
  0x10    8     r_addend     Addend value (pre-computed by linker)
```

**When created.** During the merge phase, when relocation sections are first encountered. The actual relocation records are emitted later by `sub_46ADC0` (`emit_resolved_relocations`) during the finalization phase. This function walks the linked relocation chain, translates section indices via `sub_444720`, resolves symbol addresses, and writes the resolved relocation entries. It includes validation: `"symbol never allocated"`, `"relocation is past end of offset"`, `"rela section never allocated"`, `"reloc address not found"`, and `"unexpected reloc"`.

**Size characteristics.** Directly proportional to the number of relocations in the linked binary. Each resolved relocation consumes 12 bytes (ELF32) or 24 bytes (ELF64). A kernel with 500 relocations produces ~12 KB.

### .nv.metadata — Module Metadata (SHT_CUDA_METADATA, 0x70000004)

The `.nv.metadata` section carries module-level metadata, primarily the `__nv_module_id` string that uniquely identifies the compilation unit for CUDA runtime registration.

| Property | Value |
|---|---|
| `sh_type` | `0x70000004` (1,879,048,196) |
| Section name | `.nv.metadata` |
| Creator function | `sub_43D6B0` (85 decompiled lines) |
| Consumer function | `sub_42A680` (`register_module_for_linking`) |
| Module ID extractor | `sub_46F0C0` (looks up `__nv_module_id` symbol in metadata) |
| `sh_entsize` | 4 |
| `sh_addralign` | 0 |

**Creation path.** `sub_43D6B0` at address `0x43D6B0` creates the `.nv.metadata` section lazily — only on first use. It checks if the metadata section index is already cached at `a1+232`:

```c
if (*(uint32_t *)(ctx + 232) == 0) {
    sec_idx = sub_441AC0(ctx, ".nv.metadata", 1879048196, 0, 0, 0, 4, 0);
    *(uint32_t *)(ctx + 232) = sec_idx;  // cache for reuse
}
```

The section is created with `sh_info=0` and `sh_link=0` (no associated function). After creation, the function:

1. Resolves the input symbol (`a3`) to the output string table via `sub_4405C0`.
2. Allocates a 12-byte metadata record: `{module_id_symbol:4, src_section_idx:4, alignment:4}`.
3. Builds a 40-byte data node with offset alignment to 4 bytes (`ALIGN_UP(current_offset, 4)`), attaches the 12-byte record as payload, and appends it to the section's data chain.
4. If additional raw data is provided (`a4 != 0`), appends it via `sub_433760`.

**Content format.** The section contains a linked list of 12-byte metadata records plus optional raw data blocks. Each metadata record:

```text
Offset  Size  Field
------  ----  -----
0x00    4     module_id_strtab_idx   Index into .strtab for the __nv_module_id string
0x04    4     source_section_idx     Original section index in the input ELF
0x08    4     alignment              Alignment constraint for this record
```

The `__nv_module_id` string is the compilation unit's unique identifier. During module registration (`sub_42A680`), the linker extracts this string by calling `sub_46F0C0`, which looks up the `__nv_module_id` symbol in the metadata section's associated data. If the string starts with `"def "`, it is treated as a definition record; otherwise, it is a reference that creates additional module-ID entries logged as `"extra module_id = %s"`.

**When created.** During the merge phase, when the first input `.nv.metadata` section is encountered by `register_module_for_linking` (`sub_42A680`). The section is populated incrementally as each input object contributes its module ID.

**Processing path.** `register_module_for_linking` allocates an 80-byte module registration record (zeroed), copies the input file path, and calls `sub_46F0C0` to extract the `__nv_module_id`. The module ID is used for:
- CUDA runtime registration (matching device code to host-side `__cudaRegisterFatBinary` calls)
- Incremental linking (identifying which modules need relinking)
- Debug information (mapping device functions to source compilation units)

**Size characteristics.** Small and fixed: 12 bytes per module ID record, plus the raw data contributed by each input object's `.nv.metadata` section. Typically 50–200 bytes per compilation unit.

---

## Code Sections

| Section name | sh_type | Description |
|---|---|---|
| `.text.<funcname>` | `SHT_PROGBITS` (`0x01`) | Machine code (SASS) for a single kernel or device function. Each function gets its own `.text.<name>` section, unlike host ELF which uses a monolithic `.text`. The function name is the mangled CUDA symbol. For Mercury targets (sm100+), the FNLZR replaces the Mercury instruction stream with final SASS. |

The linker processes `.text` sections during merge by copying them into the output ELF via `elfw_copy_section` (`sub_4411F0`). Dead code elimination (`sub_44AD40`) removes `.text` sections for unreachable functions, printing `"removed un-used section %s (%d)"` for each. The removal cascades to associated sections: `.nv.info.<func>`, `.nv.local.<func>`, `.nv.shared.<func>`, and their `.rela.*` counterparts — eight distinct removal sites in the decompiled dead-code-elimination function.

## Info and Metadata Sections

These sections carry structured metadata about kernels and the compilation unit.

| Section name | sh_type | Creator | Reader | Description |
|---|---|---|---|---|
| `.nv.info` | `0x70000000` | `sub_4504B0:46` | `sub_45E7D0` (merge, inline TLV parse), `sub_451D80` (finalize) | Global EIATTR metadata. Attributes apply to the entire module — CUDA API version, compatibility flags. |
| `.nv.info.<funcname>` | `0x70000000` | `sub_4504B0:63` | `sub_45E7D0`, `sub_451D80` | Per-kernel EIATTR metadata: register count, stack sizes, barriers, parameter info. `sh_link` references the owning function symbol. |
| `.nv.metadata` | `0x70000004` | `sub_43D6B0:31` | `sub_42A680`, `sub_46F0C0` | Module metadata: `__nv_module_id` string for CUDA registration. |
| `.nv.callgraph` | `0x70000001` | `sub_44D200:102` | `sub_44CA40`, `sub_44AD40` | Call edge table: caller-callee pairs for DCE and stack propagation. |
| `.nv.prototype` | `0x70000002` | `sub_44D9D0:25` | `sub_44CBC0` | Kernel launch prototype descriptors: parameter layout for driver validation. |
| `.nv.compat` | `0x70000086` | `sub_451BA0:64`, `sub_451920:113` | `sub_43E610`, `sub_45E7D0` | Compatibility attribute table checked by the driver at load time. Validation typo preserved: `"unknown .nv.compat attribute (%x) encoutered"`. |
| `.nv.rel.action` | `0x7000000B` | `sub_469D60:913` | `sub_469D60` | CUDA relocation action table. Multi-step relocation recipes for bindless textures and GPU-specific relocation patterns. |
| `.nv.resolvedrela` | `0x70000003` | `sub_469230:151` | `sub_46ADC0` | Resolved relocations preserved for driver-side patching (`--preserve-relocs`). |

### .nv.info Attribute Format

Each attribute record in a `.nv.info` section is encoded as a `(format, attribute_id, size)` triple followed by the payload. The linker logs these as:

```text
nvinfo <fmt=%d,attr=%d,size=%d>, secidx=%d
```

The `fmt` field encodes the payload format (EIFMT). The `attr` field is one of the `EIATTR_*` constants. nvlink v13.0.88 recognizes 97 distinct EIATTR constants (codes 0–96) — see the [NVIDIA Info Attributes](nv-info.md) page for the complete catalog.

### .nv.compat Attribute Format

The `.nv.compat` section contains compatibility attribute records processed during merge. Two functions create and validate these attributes:

- `sub_451BA0` (line 64): Creates `.nv.compat` with type `0x70000086` and emits attribute records for the standard compatibility path.
- `sub_451920` (line 113): Creates `.nv.compat` with type `0x70000086` for the alternate compatibility path.

Both functions share the same error diagnostic: `"unknown .nv.compat attribute (%x) encoutered with value %x."` — the typo `"encoutered"` is present in the binary at string address `0x1D3B1B8`. The merge function `sub_45E7D0` also validates `.nv.compat` attributes (line 1832) with the slightly different message `"unknown .nv.compat attribute (%x) encoutered."`.

#### EICOMPAT_ATTR Kinds

`.nv.compat` records use a parallel TLV namespace (`EICOMPAT_ATTR_*`) distinct from the EIATTR codes carried in `.nv.info`. The complete set of names extracted from the nvlink v13.0.88 binary:

| Kind | Category | Description (inferred from name) |
|---|---|---|
| `EICOMPAT_ATTR_ERROR` | Sentinel | First-slot sentinel (parallel to `EIATTR_ERROR` at 0x00). |
| `EICOMPAT_ATTR_PAD` | Sentinel | Padding record, skipped during parse. |
| `EICOMPAT_ATTR_CUDA_ACCELERATOR_TARGET` | Target | CUDA accelerator target identifier (compute capability / virtual arch). |
| `EICOMPAT_ATTR_ISA_CLASS` | Target | SASS ISA class for the input object; consumed when classifying inputs for Mercury vs pre-Mercury paths. |
| `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION` | Mercury | Mercury ISA major/minor revision; complements `EIATTR_MERCURY_ISA_VERSION` (code `0x5F` in `.nv.info`). |
| `EICOMPAT_ATTR_MERCURY_ISA_PATCH_VERSION` | Mercury | Mercury ISA patch revision. |
| `EICOMPAT_ATTR_INST_TCGEN05_MMA` | ISA feature | Object emits `tcgen05` MMA instructions (Blackwell tensor-core gen 5). |
| `EICOMPAT_ATTR_INST_TCGEN05_MMA_DEPRECATED` | ISA feature | Object emits deprecated `tcgen05` MMA encodings; driver may need to reject. |
| `EICOMPAT_ATTR_INST_TENSORMAP_V1` | ISA feature | Object uses v1 tensor-map descriptors. |
| `EICOMPAT_ATTR_CAN_FASTPATH_FINALIZE` | Pipeline | Object is eligible for the fast-path FNLZR (skips full finalization). |
| `EICOMPAT_ATTR_ENABLE_OPPORTUNISTIC_FINALIZATION` | Pipeline | Toggle for opportunistic Mercury finalization. |
| `EICOMPAT_ATTR_ERROR_LAST` | Sentinel | Last-slot sentinel marking end of catalog (parallels `EIATTR_ERROR_LAST` at 0x60). |

> ⚡ **QUIRK — EICOMPAT vs EIATTR namespaces**
> `.nv.compat` and `.nv.info` both carry TLV records but draw kind codes from disjoint namespaces (`EICOMPAT_ATTR_*` vs `EIATTR_*`). A parser keyed only on the EIATTR table will treat valid `.nv.compat` records as `"unknown attribute"` and silently misclassify Mercury target metadata. The two namespaces overlap conceptually for ISA versioning — `EIATTR_MERCURY_ISA_VERSION` lives in `.nv.info`, while `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION` / `..._PATCH_VERSION` live in `.nv.compat`. Confidence: MED (names are HIGH-confidence from the binary; semantic groupings are inferred from naming).

## Memory Space Sections

These sections represent the four GPU memory address spaces: global, local, shared, and constant.

### Global Memory

| Section name | sh_type | Description |
|---|---|---|
| `.nv.global` | `0x70000007` | Uninitialized global device memory. BSS-equivalent for `__device__` variables without initializers. Section type is `SHT_NOBITS` in input, reclassified to `SHT_CUDA_GLOBAL` by the linker. Multiple definitions of the same global are merged by `merge_overlapping_global_data` (`sub_432B10`), which validates byte-for-byte identity of overlapping regions. Created by `sub_436410:128`, `sub_439830:494`. |
| `.nv.global.init` | `0x70000008` | Initialized global device memory. Contains initial values for `__device__` variables with initializers. Carries `SHT_PROGBITS` data. Created by `sub_436740:105`. |
| `.nv.host` | `0x70000087` | Host-visible data section. Used for data that must be accessible from both host and device code paths. Created by `sub_435B60:110` with type `1879048327` (`0x70000087`). Merged by `merge_overlapping_host_data` (`sub_435B60`). |

### Local Memory (Per-Thread)

| Section name | sh_type | Description |
|---|---|---|
| `.nv.local.<funcname>` | `0x70000009` | Per-kernel local memory. Thread-private storage for register spills and local arrays. Each kernel function gets its own `.nv.local.<name>` section. Dead code elimination removes these when the parent function is unreachable. Input type is `SHT_NOBITS`, reclassified to `SHT_CUDA_LOCAL`. Created by `sub_436310:31`. Merged by `merge_overlapping_local_data` (`sub_437E20`). |

### Shared Memory (Per-CTA)

| Section name | sh_type | Description |
|---|---|---|
| `.nv.shared.<funcname>` | `0x7000000A` | Per-kernel shared memory. Cooperative storage shared among threads in a CTA (thread block). Each kernel's `__shared__` variables live in a separate section. The layout engine (`sub_439830`) performs overlap analysis via `shared_memory_optimizer` (`sub_436BD0`) to pack non-overlapping shared variables. Input type is `SHT_NOBITS`. Created by `sub_436A80:41`. |
| `.nv_debug.shared` | `SHT_NOBITS` | Debug-only shared memory. Present only in debug builds (`-g`). Provides additional shared memory for debug instrumentation. |

### Reserved Shared Memory

The compiler reserves fixed shared memory regions for hardware features (tensor core guards, memory barriers, TMEM allocation). These sections use the prefix `.nv.reservedSmem` and type `SHT_CUDA_SHARED_RESERVED` (`0x70000015`). Created by `sub_4379A0:49` and `sub_437BB0:70`.

| Section name | sh_type | Description |
|---|---|---|
| `.nv.reservedSmem` | `0x70000015` | Base reserved shared memory section. |
| `.nv.reservedSmem.begin` | `0x70000015` | Start address marker for reserved region. |
| `.nv.reservedSmem.end` | `0x70000015` | End address marker for reserved region. |
| `.nv.reservedSmem.cap` | `0x70000015` | Capacity limit of reserved region. |
| `.nv.reservedSmem.offset0` | `0x70000015` | First reserved offset slot. |
| `.nv.reservedSmem.offset1` | `0x70000015` | Second reserved offset slot. |

Associated symbols expose the reserved shared memory allocations to device code:

| Symbol | Description |
|---|---|
| `__nv_reservedSMEM_allocation_mask` | Bitmask controlling which reservation slots are active |
| `__nv_reservedSMEM_allocation_phase` | Phase counter for multi-phase allocation |
| `__nv_reservedSMEM_offset_0_alias` | Alias for offset slot 0 |
| `__nv_reservedSMEM_tcgen05_partition` | Tensor Core Gen05 partition offset (sm100+) |
| `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier` | TMEM allocation pipeline memory barrier |
| `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier_parity` | TMEM barrier parity toggle |
| `__nv_reservedSMEM_gb10b_war_var` | Blackwell workaround variable (sm100) |

The `--disable-smem-reservation` flag prevents the linker from generating reserved shared memory sections. The `--enable-extended-smem` flag extends the shared memory layout to support larger reservations.

## Constant Memory Sections

CUDA provides 18 constant memory banks (0–17), each mapped to a hardware constant cache slot accessible via the `LDC` (load constant) instruction.

### Numbered Banks

| Section name | sh_type | Bank | Description |
|---|---|---|---|
| `.nv.constant0` | `0x70000064` | 0 | Primary constant bank. Holds kernel parameters (`__constant__` variables) and driver-injected constants. This is the only bank guaranteed to be present. |
| `.nv.constant1` | `0x70000065` | 1 | General-purpose constant bank. |
| `.nv.constant2` | `0x70000066` | 2 | Compiler-generated (OCG) constants. |
| `.nv.constant3` | `0x70000067` | 3 | Bindless texture descriptors. |
| `.nv.constant4` | `0x70000068` | 4 | Reserved. |
| `.nv.constant5` | `0x70000069` | 5 | Reserved. |
| `.nv.constant6` | `0x7000006A` | 6 | Reserved. |
| `.nv.constant7` | `0x7000006B` | 7 | Reserved. |
| `.nv.constant8` | `0x7000006C` | 8 | Reserved. |
| `.nv.constant9` | `0x7000006D` | 9 | Reserved. |
| `.nv.constant10` | `0x7000006E` | 10 | Reserved. |
| `.nv.constant11` | `0x7000006F` | 11 | Reserved. |
| `.nv.constant12` | `0x70000070` | 12 | Reserved. |
| `.nv.constant13` | `0x70000071` | 13 | Reserved. |
| `.nv.constant14` | `0x70000072` | 14 | Reserved. |
| `.nv.constant15` | `0x70000073` | 15 | Reserved. |
| `.nv.constant16` | `0x70000074` | 16 | Reserved. |
| `.nv.constant17` | `0x70000075` | 17 | Highest constant bank. |

Constant banks are per-entry: the naming convention `<bank>.<funcname>` (e.g., `.nv.constant0.my_kernel`) creates entry-specific constant sections. The merge primitive `merge_constant_bank_data` (`sub_438640`) handles these, using the pattern `sprintf("%s.%s", bank_type_name, entry_name)`. It validates with the assertion `"bank SHT not CUDA_CONSTANT_?"`.

The constant deduplication pass `optimize_constant_dedup` (`sub_4339A0`) finds identical constant values across sections and aliases them, logging `"found duplicate value 0x%x, alias %s to %s"`.

### Named Constant Sections

Several constant banks have named aliases for specific purposes:

| Section name | Description |
|---|---|
| `.nv.constant.entry_params` | Kernel launch parameters. The driver writes actual argument values here before each kernel launch. Maps to constant bank 0. |
| `.nv.constant.driver` | Driver-injected constants. Contains values the driver sets at load time (grid dimensions, thread counts, etc.). |
| `.nv.constant.optimizer` | Compiler optimizer constants. Holds values generated by the optimizer (`__ocg_const`). Can be disabled with `--Xptxas --disable-optimizer-constants`. Overflow produces: `"Entry function '%s' uses too much data for compiler-generated constants"`. |
| `.nv.constant.user` | User-defined `__constant__` variables. The default bank for explicit constant memory declarations. |
| `.nv.constant.pic` | Position-independent code constants. Contains PIC trampoline data. Generated when PIC mode is active (`IsPIC`). |
| `.nv.constant.tools_data` | Profiling/debugging tool constants. Data injected by NVIDIA development tools (Nsight Compute, etc.). |
| `.nv.constant.entry_image_header_indices` | Image header index table. Maps entry points to their positions in the cubin image header array. |
| `.nv.ptx.const0.size` | Not a section per se but a metadata key encoding the size of constant bank 0 for the PTX compilation unit. Referenced during merge to validate constant bank sizing. |

## Unified Table Sections (UFT/UDT/UIDX)

The Unified Function Table (UFT) and Unified Descriptor Table (UDT) enable indirect calls and texture/surface access across compilation units. The UIDX (Unified Index) file is an external index that maps UUIDs to table slots.

| Section name | sh_type | Description |
|---|---|---|
| `.nv.uft` | `0x7000000E` | Unified Function Table. Jump slot array for cross-module indirect function calls. Each slot holds a branch instruction targeting the resolved function. Created by `sub_442820:73`. The linker validates: `"Number of .nv.uft jump slots != Number of entries in .nv.uft.entry"`. |
| `.nv.uft.entry` | `0x70000011` | UFT entry metadata. Maps each UFT slot to its UUID pair and target symbol. Created by `sub_4438F0:579`, `sub_464240:15`. Entries are 128-bit UUID pairs logged as `"uft uuid = <%016llx,%016llx>, offset = %llx"`. |
| `.nv.uft.rel` | `0x7000000E` | Per-kernel UFT relocation table. Shares the same `sh_type` as `.nv.uft` because the dispatch path at `sub_442820:61-73` constructs the per-kernel name via `sprintf("%s.%s", ".nv.uft.rel", a2+15)` and creates it with the same type `0x7000000E`. |
| `.nv.udt` | `0x70000012` | Unified Descriptor Table. Descriptor array for cross-module texture and surface access. Created by `sub_436740:76`, `sub_436410:94`. Aligned with: `"udt size %lld needs aligning"`. |
| `.nv.udt.entry` | `0x70000014` | UDT entry metadata. Maps each UDT slot to its UUID pair and target symbol, parallel to `.nv.uft.entry`. Created by `sub_464320:15`. |
| `.nv.uidx` | `SHT_PROGBITS` | Unified index table. Loaded from an external file specified by `--uidx-file`. Contains the pre-computed UUID-to-slot mapping. Validated with `"malformed uidx input"`, `"size of uidx window != nv.uft"`, `"size of uidx window != nv.udt"`. |

The UFT/UDT management functions (`sub_4637B0`, `sub_463F70`) reorder entries and resolve UUID-based lookups. The linker generates stub functions for unified calls using the template:

```ptx
.func .attribute(.unified_func_stub)  __cuda_uf_stub_<name>( ) { ... }
```

Unified table relocations use dedicated relocation types: `R_CUDA_UNIFIED`, `R_CUDA_UNIFIED_32`, `R_CUDA_UNIFIED_8_0` through `R_CUDA_UNIFIED_8_56`, and the Mercury equivalents `R_MERCURY_UNIFIED*`. The synthetic symbols `__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT_CANONICAL`, `__UFT_END`, and `__UDT_END` mark the table boundaries in the final ELF.

## Note Sections

Standard ELF `SHT_NOTE` sections carry CUDA compilation metadata consumed by the driver and runtime.

| Section name | sh_type | Description |
|---|---|---|
| `.note.nv.cuinfo` | `SHT_NOTE` (`0x07`) | CUDA compilation info. Contains key-value pairs describing the compilation (target architecture, CUDA version, compiler options that affect ABI). Created during `elfw_create` (`sub_4438F0`). |
| `.note.nv.cuver` | `SHT_NOTE` (`0x07`) | CUDA version stamp. Records the CUDA toolkit version used to compile the cubin. |
| `.note.nv.tkinfo` | `SHT_NOTE` (`0x07`) | Toolkit info. Extended toolkit metadata. Controllable with `--verbose-tkinfo`. |

All three are created at ELF wrapper initialization time and are always present in the output cubin.

## Texture and Surface Reference Sections

These carry descriptor size information for the texture/surface hardware units.

| Section name | sh_type | Description |
|---|---|---|
| `.nv.unified.texrefDescSize` | `SHT_PROGBITS` | Descriptor size for unified-mode texture references. Unified texture mode combines texture and sampler into a single descriptor. |
| `.nv.independent.texrefDescSize` | `SHT_PROGBITS` | Descriptor size for independent-mode texture references. Independent mode uses separate texture and sampler objects. |
| `.nv.independent.samplerrefDescSize` | `SHT_PROGBITS` | Descriptor size for independent-mode sampler references. |
| `.nv.surfrefDescSize` | `SHT_PROGBITS` | Descriptor size for surface references. |

The texture mode affects how `tex` and `suld`/`sust` instructions are lowered. The linker checks `"unexpected usage of non-unified surface descriptors"` when modes are mixed.

## Hash Relocation Sections (Incremental Linking)

These sections support incremental linking through hash-based relocation tracking. Each letter encodes the content type: **K**ey, **C**ode, **D**ata; and the scope: **E**xternal, **I**nternal. Processed by `hrk_section_process` (`sub_4AF3C0`) and `hrc_hrd_section_process` (`sub_4B02A0`).

| Section name | sh_type | Description |
|---|---|---|
| `.nvHRKE` | `SHT_PROGBITS` | Hash Relocation Key External — external key hash entries. |
| `.nvHRKI` | `SHT_PROGBITS` | Hash Relocation Key Internal — internal key hash entries. |
| `.nvHRCE` | `SHT_PROGBITS` | Hash Relocation Code External — external code hash entries. |
| `.nvHRCI` | `SHT_PROGBITS` | Hash Relocation Code Internal — internal code hash entries. |
| `.nvHRDE` | `SHT_PROGBITS` | Hash Relocation Data External — external data hash entries. |
| `.nvHRDI` | `SHT_PROGBITS` | Hash Relocation Data Internal — internal data hash entries. |

## Fatbin Sections (Host ELF)

These sections appear in the **host** ELF (not the device cubin) and contain embedded device code for lazy JIT compilation.

| Section name | Description |
|---|---|
| `.nvFatBinSegment` | Primary fat binary segment. Contains the `__cudaFatBinaryData` structure with embedded cubins for all target architectures. |
| `__nv_relfatbin` | Relocatable fat binary section. Contains position-dependent references into the fat binary that need host-side relocation. |
| `.nv_fatbin` | Fat binary data section. Raw fat binary payload referenced by `.nvFatBinSegment`. |

The linker generates a host linker script to ensure these sections are placed correctly:

```text
SECTIONS
{
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    __nv_relfatbin : { *(__nv_relfatbin) }
    .nv_fatbin : { *(.nv_fatbin) }
}
```

## Debug Sections

### Standard NVIDIA Debug Sections

| Section name | sh_type | Description |
|---|---|---|
| `.nv_debug_info_ptx` | `SHT_PROGBITS` | Embedded PTX source text for source-level debugging. |
| `.nv_debug_info_reg_sass` | `SHT_PROGBITS` | Per-instruction register liveness at the SASS level. Used by cuda-gdb for variable inspection at arbitrary breakpoints. |
| `.nv_debug_info_reg_type` | `SHT_PROGBITS` | Register type annotations associating data types with physical registers. |
| `.nv_debug_line_sass` | `SHT_PROGBITS` | SASS-level line number table. Constructed at runtime from prefix `.nv_debug_` + `line_sass`. |
| `.nv_debug.shared` | `SHT_NOBITS` | Debug-mode shared memory. Extra shared memory reserved for debug instrumentation when `-g` is active. |

### Standard DWARF Debug Sections

Cubins include standard DWARF sections (`.debug_abbrev`, `.debug_info`, `.debug_line`, `.debug_str`, etc.) with CUDA extensions. The linker validates their presence with `"skipping .debug_info section due to missing .debug_abbrev section"` and processes them during the merge phase. NVIDIA adds custom DWARF attributes such as `DW_AT_NV_general_flags`.

### Mercury Debug Sections

Mercury targets (sm100+) wrap their debug data in the `.nv.merc.*` namespace. These 19 sections (11 standard DWARF mirrors + 4 NVIDIA-specific + structural) are documented in full on the [Mercury ELF Sections](../mercury/elf-sections.md) page. The key difference is that Mercury debug sections carry the `0x10000000` flag in `sh_flags`, causing the merge phase to skip them and defer processing to FNLZR.

## Standard ELF Infrastructure Sections

Every cubin also contains standard ELF sections used by the linker infrastructure:

| Section name | sh_type | Description |
|---|---|---|
| `.symtab` | `SHT_SYMTAB` (`0x02`) | Symbol table. Contains both CUDA-specific and standard ELF symbols. |
| `.strtab` | `SHT_STRTAB` (`0x03`) | String table for symbol names. |
| `.shstrtab` | `SHT_STRTAB` (`0x03`) | Section header string table. Contains all section name strings. |
| `.rela.<secname>` | `SHT_RELA` (`0x04`) | Standard ELF relocation sections. Generated via `elfw_add_reloc_section` (`sub_441AC0`) using the pattern `.rela%s`. |
| `.rel.<secname>` | `SHT_REL` (`0x09`) | Standard ELF REL sections (without addend). Rejected by nvlink with `"unsupported REL section"`. |

## Section Name Dispatch in merge_elf

The `merge_elf` function (`sub_45E7D0`, 89,156 bytes) is the central section classifier. It uses `strncmp`-based prefix matching on section names to route each input section to the correct merge handler:

| Prefix match | Handler | Memory space |
|---|---|---|
| `.nv.global` (exact) | `merge_overlapping_global_data` (`sub_432B10`) | Global BSS |
| `.nv.global.init` | `merge_overlapping_data_variant` | Global initialized |
| `.nv.local.` | `merge_overlapping_local_data` (`sub_437E20`) | Per-thread local |
| `.nv.shared.` | overlap analysis + layout (`sub_436BD0`) | Per-CTA shared |
| `.nv.constant` | `merge_constant_bank_data` (`sub_438640`) | Constant banks |
| `.nv.info` | inline TLV parse in `sub_45E7D0` | Metadata |
| `.nv.compat` | compatibility check (`sub_451920`/`sub_451BA0`) | Compatibility |
| `.nv.host` | `merge_overlapping_host_data` (`sub_435B60`) | Host-visible |
| `.nv.merc.` | skip (deferred to FNLZR) | Mercury code/debug |
| `.text.` | `elfw_copy_section` (`sub_4411F0`) | Code |

All five overlap merge functions (`sub_432B10`, `sub_437E20`, `sub_4343C0`, `sub_434BC0`, `sub_435390`) share the same validation logic: they compare overlapping regions byte-for-byte and fatal on mismatch with `"overlapping non-identical data"`.

## Section Lifecycle

A section progresses through the following stages in the nvlink pipeline:

1. **Parse**: Input ELF sections are read by the ELF parser, producing section records with raw `sh_type` values.

2. **Classify**: `merge_elf` reclassifies sections based on name prefix, assigning CUDA-specific `sh_type` values.

3. **Create/Find**: The output section is found by name hash lookup or created by `section_create` (`sub_441AC0`), which allocates a 104-byte section record and registers it in both hash tables (by name at `ctx+296` and by index at `ctx+288`).

4. **Accumulate**: `section_data_copy` (`sub_433760`) appends data contributions to a linked list. No final layout yet — just a chain of (source_ptr, size, alignment) nodes.

5. **Layout**: The layout engine (`sub_439830`) calls `section_layout_engine` (`sub_4325A0`) to sort symbols by alignment and assign offsets within each section.

6. **Relocate**: `apply_relocations` (`sub_469D60`) resolves all relocations against the final section addresses.

7. **Finalize**: `finalize_elf` (`sub_445000`) applies final patches, generates `.nv.callgraph`, `.nv.prototype`, `.nv.resolvedrela` (if needed), and encodes computed `.nv.info` properties.

8. **Emit**: `write_elf_to_buffer` (`sub_45BF00`) serializes all sections into the output ELF, validating sizes with `"section size mismatch"`.

### Creation Timing Summary

| Pipeline phase | Sections created |
|---|---|
| Merge (Phase 5) | `.nv.info`, `.nv.info.<func>`, `.nv.metadata`, `.nv.compat`, `.nv.global`, `.nv.global.init`, `.nv.local.<func>`, `.nv.shared.<func>`, `.nv.constant*`, `.nv.host`, `.nv.udt`, `.nv.rel.action`, `.rela.*`, `.nv.resolvedrela*` |
| Layout (Phase 9) | `.nv.reservedSmem*`, `.nv.global` (additional via `sub_439830:494`) |
| Finalize (Phase 11–12) | `.nv.callgraph`, `.nv.prototype`, `.nv.uft`, `.nv.uft.entry`, `.nv.udt.entry`, `.note.nv.cuinfo`, `.note.nv.cuver`, `.note.nv.tkinfo` |

## Quick Reference: All Section Names

Complete alphabetical list of every NVIDIA-specific section name found in nvlink v13.0.88:

```text
.note.nv.cuinfo                         SHT_NOTE           Compilation info
.note.nv.cuver                          SHT_NOTE           CUDA version
.note.nv.tkinfo                         SHT_NOTE           Toolkit info
.nv.callgraph                           0x70000001         Call edge table
.nv.compat                              0x70000086         Compatibility attributes
.nv.constant0 .. .nv.constant17         0x70000064..75     Constant banks 0-17
.nv.constant.driver                     (bank alias)       Driver constants
.nv.constant.entry_image_header_indices (bank alias)       Image header indices
.nv.constant.entry_params               (bank alias)       Kernel parameters
.nv.constant.optimizer                  (bank alias)       Compiler-generated constants
.nv.constant.pic                        (bank alias)       PIC trampoline data
.nv.constant.tools_data                 (bank alias)       Tool-injected constants
.nv.constant.user                       (bank alias)       User __constant__ variables
.nv.global                              0x70000007         Global BSS
.nv.global.init                         0x70000008         Global initialized data
.nv.host                                0x70000087         Host-visible data
.nv.independent.samplerrefDescSize      SHT_PROGBITS       Sampler descriptor size
.nv.independent.texrefDescSize          SHT_PROGBITS       Texture descriptor size (indep)
.nv.info                                0x70000000         Global nvinfo attributes
.nv.info.<funcname>                     0x70000000         Per-kernel nvinfo attributes
.nv.local.<funcname>                    0x70000009         Per-kernel local memory
.nv.metadata                            0x70000004         Module metadata
.nv.merc.*                              (varies)           Mercury sections (19 total)
.nv.prototype                           0x70000002         Launch prototypes
.nv.ptx.const0.size                     (metadata)         Constant bank 0 size record
.nv.rel.action                          0x7000000B         Relocation action table
.nv.reservedSmem                        0x70000015         Reserved shared memory base
.nv.reservedSmem.begin                  0x70000015         Reserved region start
.nv.reservedSmem.cap                    0x70000015         Reserved region capacity
.nv.reservedSmem.end                    0x70000015         Reserved region end
.nv.reservedSmem.offset0                0x70000015         Reserved offset slot 0
.nv.reservedSmem.offset1                0x70000015         Reserved offset slot 1
.nv.resolvedrela                        0x70000003         Preserved relocations
.nv.shared.<funcname>                   0x7000000A         Per-kernel shared memory
.nv.surfrefDescSize                     SHT_PROGBITS       Surface descriptor size
.nv.udt                                 0x70000012         Unified Descriptor Table
.nv.udt.entry                           0x70000014         UDT entry metadata
.nv.uft                                 0x7000000E         Unified Function Table
.nv.uft.entry                           0x70000011         UFT entry metadata
.nv.uft.rel.<funcname>                  0x7000000E         Per-kernel UFT relocation slot
.nv.uidx                                SHT_PROGBITS       Unified index table
.nv.unified.texrefDescSize              SHT_PROGBITS       Texture descriptor size (unified)
.nvHRCE                                 SHT_PROGBITS       Hash reloc code external
.nvHRCI                                 SHT_PROGBITS       Hash reloc code internal
.nvHRDE                                 SHT_PROGBITS       Hash reloc data external
.nvHRDI                                 SHT_PROGBITS       Hash reloc data internal
.nvHRKE                                 SHT_PROGBITS       Hash reloc key external
.nvHRKI                                 SHT_PROGBITS       Hash reloc key internal
.nv_debug_info_ptx                      SHT_PROGBITS       Embedded PTX source
.nv_debug_info_reg_sass                 SHT_PROGBITS       SASS register liveness
.nv_debug_info_reg_type                 SHT_PROGBITS       Register type annotations
.nv_debug_line_sass                     SHT_PROGBITS       SASS line number table
.nv_debug.shared                        SHT_NOBITS         Debug shared memory
.nv_fatbin                              SHT_PROGBITS       Fat binary data (host ELF)
.nvFatBinSegment                        SHT_PROGBITS       Fat binary segment (host ELF)
__nv_relfatbin                          SHT_PROGBITS       Relocatable fatbin (host ELF)
.text.<funcname>                        SHT_PROGBITS       Kernel/function machine code
```

## Function Reference Table

All functions that create, read, or process NVIDIA-specific sections, with addresses and sizes:

| Function | Address | Decompiled lines | Role |
|---|---|---|---|
| `section_create` | `sub_441AC0` | 290 | Creates section records (104-byte allocation, dual hash-table registration) |
| `create_nvinfo_section` | `sub_4504B0` | 72 | Creates `.nv.info` / `.nv.info.<func>` sections |
| `build_callgraph_section` | `sub_44D200` | 368 | Creates and populates `.nv.callgraph` |
| `build_prototype_section` | `sub_44D9D0` | 63 | Creates and populates `.nv.prototype` |
| `create_reloc_section` | `sub_469230` | 154 | Creates `.rela.*`, `.rel.*`, and `.nv.resolvedrela*` sections |
| `create_metadata_section` | `sub_43D6B0` | 85 | Creates `.nv.metadata` section (lazy, cached at ctx+232) |
| `tokenize_quoted_arg` | `sub_44E590` | — | Recursive string tokenizer (handles `\`, `[`, `]`, `"` escapes). *Earlier misattribution as `parse_nvinfo_attribute` corrected: this function never touches `.nv.info` — the actual TLV parser is inline in `sub_45E7D0` (merge) and `sub_451D80` (finalize).* |
| `tokenize_string_list` | `sub_44E8B0` | 223 | String list tokenizer used by command-line / config parsing. *Earlier misattribution as `parse_nvinfo_section` corrected; see [.nv.info Metadata](nv-info.md).* |
| `compute_entry_properties` | `sub_451D80` | 3,029 | Walks the EIATTR linked list at `elfw+392`, computes per-entry-kernel properties (regcount, barriers, frame size, stack size, externs), and *appends* new EIATTR record nodes via `sub_450B70`/`sub_4644C0`. Does not write TLV bytes itself; the chunk-list payload pointers it sets up are later flushed by `sub_45BF00`. |
| `reloc_action_dispatcher` | `sub_468760` | — | Relocation-action engine / instruction-field bitpacker (14,322 bytes). *Earlier wiki revisions misattributed this as "nvinfo_encode"; it is unrelated to EIATTR TLV emission. See [.nv.info Metadata](nv-info.md#tlv-byte-emission) and [Relocation Engine](../linker/relocation-engine.md).* |
| `fixup_callgraph` | `sub_44CA40` | 110 | Remaps symbol indices in `.nv.callgraph` |
| `fixup_prototype` | `sub_44CBC0` | 54 | Remaps symbol indices in `.nv.prototype` |
| `emit_resolved_relocations` | `sub_46ADC0` | 406 | Writes resolved relocation entries |
| `register_module_for_linking` | `sub_42A680` | — | Reads `.nv.metadata` and extracts `__nv_module_id` |
| `extract_module_id` | `sub_46F0C0` | — | Looks up `__nv_module_id` symbol in metadata |
| `dead_code_elimination` | `sub_44AD40` | 689 | Consumes `.nv.callgraph`, removes unreachable sections |
| `merge_elf` | `sub_45E7D0` | — | Central section classifier (89,156 bytes) |
| `apply_relocations` | `sub_469D60` | — | Resolves relocations, creates `.nv.rel.action` at line 913 |
| `create_compat_section` (path A) | `sub_451BA0` | — | Creates `.nv.compat` at line 64 |
| `create_compat_section` (path B) | `sub_451920` | — | Creates `.nv.compat` at line 113 |
| `dump_callgraph` | `sub_44CE00` | — | Prints `"callgraph for sm_%d:"` to stderr |
| `dump_callgraph_dot` | `sub_44CCF0` | — | Outputs `"digraph callgraph {"` in DOT format |

## Cross-References

**Internal (nvlink wiki):**

- [Section Catalog](../reference/section-catalog.md) — Alphabetical reference catalog of all 109 section entries with `sh_type` hex values
- [.nv.info Metadata](nv-info.md) — EIATTR attribute format and the 97 attribute constants carried in `.nv.info` / `.nv.info.<funcname>` sections
- [Constant Banks](constant-banks.md) — Deep dive on `.nv.constant*` section numbering, dedup, the name-to-index table at `0x1D3A8E0`, and hardware size limits
- [Unified Function Tables](uft.md) — UFT/UDT section management (`.nv.uft`, `.nv.udt`, `.nv.uidx`)
- [Mercury ELF Sections](../mercury/elf-sections.md) — The 19 `.nv.merc.*` sections for Mercury targets (sm100+)
- [Section Merging](../linker/section-merging.md) — `merge_elf` name-prefix dispatch table that classifies input sections
- [Dead Code Elimination](../linker/dead-code-elimination.md) — How `.text.*` and associated `.nv.info.*` / `.nv.local.*` sections are removed
- [Device ELF Format](device-elf-format.md) — ELF header encoding and how `e_type` / `e_flags` relate to section emission
- [Linker Scripts](../infra/linker-scripts.md) — Host-side ELF sections (`.nvFatBinSegment`, `__nv_relfatbin`, `.nv_fatbin`) and the `SECTIONS` template
- [Program Headers](program-headers.md) — How sections are classified into PT\_LOAD segments via the internal flags bitmask
- [R\_CUDA Catalog](../reference/r-cuda-catalog.md) — Relocation types referencing these sections

**Sibling wikis:**

- [ptxas: Sections](../../ptxas/output/sections.html) — Section creation in ptxas: how `.text`, `.nv.info`, `.nv.constant*`, and debug sections are emitted
- [ptxas: EIATTR Reference](../../ptxas/reference/eiattr.html) — EIATTR attribute codes that populate `.nv.info` sections
- [ptxas: Debug Info](../../ptxas/output/debug-info.html) — How ptxas generates the NVIDIA debug sections (`.nv_debug_*`)

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| All `sh_type` hex values for NVIDIA-specific sections | HIGH | All 18 spot-checked values match the decompiled `sub_441AC0` call-site arguments (see P081 correction table in section-catalog.md) |
| `.nv.info` creation at sub_4504B0 lines 46 and 63 | HIGH | Decompiled code shows `sub_441AC0(ctx, ".nv.info", 1879048192, ...)` (line 46) and per-func variant with flags=0x40 (line 63) |
| `.nv.callgraph` 8-byte record format with sentinels -1/-2/-3/-4 | HIGH | Decompiled sub_44D200 shows exact sentinel values: `v11[1]=-1`, later `-2`, `-3`, `-4` |
| `.nv.prototype` 8-byte record format {symidx, proto_desc} | HIGH | Decompiled sub_44D9D0 shows `*v18 = *(_DWORD *)v13; *(v18+1) = *(v13+4)` — two 32-bit fields copied from function table entry |
| `.nv.resolvedrela` conditional on preserve-relocs flag at ctx+85 | HIGH | Decompiled sub_469230 shows `if (*(_BYTE *)(a1 + 85))` guard at lines 94, 108, 126, 140 |
| `.nv.metadata` lazy creation cached at ctx+232 | HIGH | Decompiled sub_43D6B0 shows `if (!*(DWORD*)(a1+232))` guard and cache store `*(DWORD*)(a1+232) = sec_idx` |
| `__nv_module_id` extraction via sub_46F0C0 | HIGH | Decompiled sub_46F0C0 shows string `"__nv_module_id"` at three call sites |
| Dead code elimination removes 8 section types per function | HIGH | Decompiled sub_44AD40 contains 8 distinct `"removed un-used section"` fprintf sites |
| Section record size = 104 bytes | HIGH | Decompiled sub_441AC0 line 101: `sub_4307C0(v14, 104)` |
| merge_elf at sub_45E7D0 (89,156 bytes) | HIGH | Decompiled file exists |
| `"overlapping non-identical data"` string | HIGH | String at 0x1D387D8 confirmed, xref to sub_432B10 |
| `"unknown .nv.compat attribute (%x) encoutered"` (typo) | HIGH | String at 0x1D3B1B8 confirmed (typo "encoutered" preserved) |
| Section lifecycle 8-stage pipeline | MEDIUM | Stages match observed function call order but reconstructed from multiple function call chains |
| `SHT_CUDA_CONSTANT` generic placeholder = 0x70000006 | MEDIUM | Referenced in range check `a3 == 1879048198` at sub_441AC0:190 but no section is ever created with this type |
| `SHF_CUDA_MERCURY` = 0x10000000 (bit 28) | MEDIUM | Referenced in Mercury section handling; not individually verified in decompiled bitmask |
| Fatbin section names (.nvFatBinSegment etc) | MEDIUM | Referenced in host-side linker path; not primary nvlink device-link path |
| Reserved shared memory symbol names | MEDIUM | Inferred from string table patterns; not all individually traced to specific code paths |
| Section creation timing (merge vs layout vs finalize) | MEDIUM | Inferred from function call chains; some sections may be created in multiple phases |
