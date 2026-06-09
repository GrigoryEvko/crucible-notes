# Capsule Mercury Format

Capsule Mercury (capmerc) is the default binary output format for SM100+ targets in nvlink v13.0.88. It replaces the traditional cubin (CUDA binary) format with a two-layer ELF structure: the outer ELF is a standard CUDA device ELF with `e_machine = 190` (`EM_CUDA`), but it carries an inner set of `.nv.merc.*` sections that encode Mercury-native instruction streams, Mercury-specific relocations, debug information, and ISA metadata. The CUDA driver reconstitutes finalized SASS from these Mercury sections at load time (or the linker can do it ahead of time via `--self-check`), enabling the driver to apply architecture-specific fixups and optimizations that were impossible with the flat SASS cubin model.

The name "Capsule Mercury" comes from the encapsulation metaphor: Mercury-format data is encapsulated inside a CUDA ELF container. The "capmerc" abbreviation appears consistently in CLI flags, output filenames, and internal string references.

## Key Facts

| Property | Value |
|---|---|
| CLI selection | `--binary-kind=capmerc` (default for sm100+) |
| Other valid kinds | `mercury` (legacy), `sass` (flat SASS) |
| Option parser | `sub_4AC380` at `0x4AC380` (9,967 bytes, 429 lines) |
| ELF type | `0xFF00` (`ET_LOPROC` — processor-specific, distinct from `ET_EXEC=2`) |
| Default output name | `capmerc.cubin` (vs `sass.cubin` for SASS kind) |
| Activation flag | `byte_2A5F222 = 1` when arch > sm\_99 |
| capmerc-specific flag | `byte_2A5F225 = 1` |
| Self-check flag | `--self-check` via `sub_4AC380` |
| FNLZR entry | `sub_4275C0` (post-link) / `sub_4748F0` (engine) |
| SASS reconstitution | `sub_5207A0` (18,673 bytes, 784 lines) |
| Fatbin member type | `16` (distinct from cubin=2, PTX=1, NVVM=8) |

## Binary Kind Selection

The `--binary-kind` flag is registered in `sub_4AC380` with the help text:

```text
Specify the type of target ELF binary kind. Default on sm100+ is capmerc
```

Three values are accepted: `mercury`, `capmerc`, and `sass`. The `capmerc` value is the default when `byte_2A5F222` is set (architecture > sm\_99). The `mercury` value produces a legacy Mercury ELF without the capsule wrapping. The `sass` value produces a traditional flat SASS cubin identical to pre-Blackwell output.

Internally, option parsing in `sub_427AE0` sets `byte_2A5F222 = 1` and `byte_2A5F225 = 1` for any architecture with SM > 99. These globals gate every Mercury-specific code path in the linker. When `byte_2A5F225` is set, the output path in `main()` serializes to memory (via `sub_45C950`) rather than directly to file (via `sub_45C920`), because the FNLZR post-link transform requires a complete in-memory ELF image.

### Related CLI Options

| Option | Help text | Function |
|---|---|---|
| `--binary-kind` | Target ELF binary kind | Selects mercury/capmerc/sass |
| `--cap-merc` | Generate Capsule Mercury | Boolean shortcut for `--binary-kind=capmerc`; the parser order in `sub_4AC380` registers `--cap-merc` *after* `--binary-kind`, so a later `--binary-kind=sass` overrides an earlier `--cap-merc` and an explicit `--cap-merc` cannot un-set a prior `--binary-kind=sass` |
| `--self-check` | Self check for capsule mercury (capmerc) | Validates round-trip reconstitution |
| `--out-sass` | Output reconstituted SASS | Writes SASS from capmerc only via `--self-check` |
| `--fastpath-off` | Turns off the fast-path finalization optimization | Disables cross-family fast finalization |
| `--opportunistic-finalization-lvl` | Opportunistic finalization level (0-3) | Controls finalization scope |
| `--asatentrypatch` | Compile patch as at entry fragment | Entry-patch compilation mode |

## Capmerc vs Traditional Cubin

A traditional cubin (SASS kind) is a fully resolved ELF: every `.text.*` section contains final machine code with all relocations applied, all instruction encodings fixed, and all control-flow addresses resolved. The driver loads it and maps it directly to GPU memory.

A capmerc binary defers the final instruction encoding step. Instead of storing finalized SASS, each function's `.text.*` section is accompanied by a set of `.nv.merc.*` sections that carry:

1. **Mercury intermediate code** — instruction streams in Mercury's scheduling-friendly representation, where instructions carry symbolic operand references and control-flow metadata rather than fully resolved bit-level encodings.

2. **Mercury relocations** — `R_MERCURY_*` relocation types (see [R\_MERCURY Relocations](r-mercury-relocations.md)) that the driver's finalizer resolves at load time. These are distinct from `R_CUDA_*` relocations and use a dedicated relocation table (`.nv.merc.rela`).

3. **Mercury debug sections** — DWARF-like debug data under the `.nv.merc.*` namespace, including line tables, abbreviation tables, and register-level debug info.

4. **Mercury ISA metadata** — `EIATTR_MERCURY_ISA_VERSION` and `EIATTR_MERCURY_FINALIZER_OPTIONS` attributes that tell the driver which Mercury ISA version the code targets and what finalizer options were used at compile time.

5. **Compatibility attributes** — `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION`, `EICOMPAT_ATTR_MERCURY_ISA_PATCH_VERSION`, and `EICOMPAT_ATTR_CAN_FASTPATH_FINALIZE` for driver-side version matching.

The deferred encoding enables the driver to:

- Apply architecture-variant-specific instruction scheduling (sm\_100 vs sm\_100a vs sm\_100f).
- Insert or remove NOPs based on final instruction addresses (control-flow-dependent padding).
- Apply warp-level optimizations that require knowledge of the final binary layout.
- Support forward compatibility via the "f" architecture variants (sm\_100f, sm\_103f, etc.).

### Structural Comparison

| Aspect | Traditional cubin (sass) | Capsule Mercury (capmerc) |
|---|---|---|
| ELF type | `ET_EXEC` (2) | `0xFF00` |
| Text sections | Final SASS bytes | Mercury intermediate + SASS stub |
| Relocations | `R_CUDA_*` (resolved) | `R_CUDA_*` (resolved) + `R_MERCURY_*` (deferred) |
| Debug sections | `.debug_*` | `.debug_*` + `.nv.merc.debug_*` |
| Output filename | `sass.cubin` | `capmerc.cubin` |
| Driver finalization | None | Required (reconstitutes SASS) |
| Forward compatible | No | Yes (via "f" variants) |
| Fatbin member type | default (cubin) | `16` (mercury/capmerc) |

## Mercury Sections (`.nv.merc.*`)

The inner Mercury payload is distributed across multiple ELF sections with the `.nv.merc` prefix. These sections are created by the compiler backend (ptxas/cicc) and preserved through linking by nvlink's merge phase. During merge (`sub_45E7D0`), Mercury sections are identified and either copied or skipped depending on the linking mode — the string `"skip mercury section %i"` appears when a Mercury section is not relevant to the current merge pass.

All `.nv.merc.*` sections carry the **SHF\_NV\_MERC** flag: bit 28 of `sh_flags` (`0x10000000`). This NVIDIA extension flag serves two purposes: (1) fast O(1) rejection of non-merc sections during classification, and (2) namespace separation during section index remapping (`sub_1C99BB0`). The finalizer uses this flag to identify which sections require relocation patching during off-target finalization.

### Section Catalog

| Section name | Description |
|---|---|
| `.nv.merc` | Mercury code payload (per-function) |
| `.nv.merc.rela` | Mercury-specific relocation table |
| `.nv.merc.symtab_shndx` | Extended symbol section indices for Mercury |
| `.nv.merc.debug_abbrev` | DWARF abbreviation table (Mercury) |
| `.nv.merc.debug_aranges` | DWARF address ranges (Mercury) |
| `.nv.merc.debug_frame` | DWARF call frame info (Mercury) |
| `.nv.merc.debug_info` | DWARF debug info entries (Mercury) |
| `.nv.merc.debug_loc` | DWARF location lists (Mercury) |
| `.nv.merc.debug_macinfo` | DWARF macro info (Mercury) |
| `.nv.merc.debug_pubnames` | DWARF public names (Mercury) |
| `.nv.merc.debug_pubtypes` | DWARF public types (Mercury) |
| `.nv.merc.debug_ranges` | DWARF ranges (Mercury) |
| `.nv.merc.debug_str` | DWARF string table (Mercury) |
| `.nv.merc.debug_line` | DWARF line number table (Mercury) |
| `.nv.merc.nv_debug_line_sass` | NVIDIA SASS-level line debug |
| `.nv.merc.nv_debug_info_reg_sass` | NVIDIA SASS register debug info |
| `.nv.merc.nv_debug_info_reg_type` | NVIDIA register type debug info |
| `.nv.merc.nv_debug_ptx_txt` | PTX source text for debug |
| `.nv.merc.nv.shared.reserved.` | Reserved shared memory (Mercury) |

The Mercury code sections use a naming convention of `.nv.merc.` followed by a function-relative suffix. The section `.nv.merc` without further qualification is the prefix used during section lookup in the finalization pipeline (`sub_4748F0` references `.nv.merc.` at four distinct call sites).

### Section Name Construction

Mercury section names are constructed by two helper functions:

- `sub_1CEC4C0` — generic name constructor. Prepends `".nv.merc"` to the original section name: `sprintf(buf, "%s%s", ".nv.merc", original_name)`. Allocates `strlen(name) + 9` bytes (8 for prefix + NUL).

- `sub_1CEC660` — constant bank name constructor. Maps `.nv.constant*` sections to Mercury equivalents with bank-type suffixes. Extracts the bank character from offset 12 of the section name, computes the bank type as `char + 0x70000034` (SHT\_LOPROC+52), then matches against six known bank types:

| Bank type | Suffix | vtable offset |
|---|---|---|
| Entry image header indices | `.entry_image_header_indices` | +72, +304 |
| Driver | `.driver` | +144 |
| Optimizer | `.optimizer` | +136 |
| User | `.user` | +192 |
| PIC | `.pic` | +168 |
| Tools data | `.tools_data` | +152 |

The composite name is built by `sub_1CEC570`: e.g., `".nv.merc" + ".nv.constant" + ".user" + bank_name`.

Relocation section names are constructed in `sub_1CF72E0` as: `sprintf(buf, "%s%s%s", ".nv.merc", ".rela", name+8)` — the +8 strips the `".nv.merc"` prefix so that `".nv.merc.debug_info"` becomes `".nv.merc.rela.debug_info"`.

### Section Classifier — `sub_1CED0E0`

The 9,262-byte classifier at `0x1CED0E0` identifies `.nv.merc.*` sections using a two-stage guard-then-waterfall algorithm identical to the ptxas classifier at `sub_1C98C60` (see [ptxas: Capsule Mercury & Finalization](../../../../ptxas/wiki/src/codegen/capmerc.md) for the full algorithm description).

**Stage 1: `sh_type` range check with bitmask `0x5D05`.** The section's `sh_type` is tested against two processor-specific ranges:

| Range | sh\_type span | Qualifying types |
|---|---|---|
| A | `0x70000006`..`0x70000014` | Filtered by bitmask `0x5D05` (7 specific types) |
| B | `0x70000064`..`0x7000007E` | All accepted (memory-space data) |
| Special | `1` (SHT\_PROGBITS) | Accepted (capmerc descriptors, string tables) |

Bitmask `0x5D05` = binary `0101_1101_0000_0101` selects: SHT\_LOPROC+6 (bit 0), +8 (bit 2), +14 (bit 8), +16 (bit 10), +17 (bit 11), +18 (bit 12), +20 (bit 14).

**Stage 2: Name-based disambiguation.** When `SHF_NV_MERC` (`0x10000000`) is set in `sh_flags`, the classifier performs sequential `strcmp()` against 15+ section names, returning 1 on first match. The check order is: `.nv.merc.debug_abbrev`, `.nv.merc.debug_aranges`, `.nv.merc.debug_frame`, `.nv.merc.debug_info`, `.nv.merc.debug_loc`, `.nv.merc.debug_macinfo`, `.nv.merc.debug_pubnames`, `.nv.merc.debug_pubtypes`, `.nv.merc.debug_ranges`, `.nv.merc.debug_str`, `.nv.merc.nv_debug_info_reg_type`, and more.

A companion classifier `sub_1CED7C0` (6,757 bytes) performs the same algorithm for non-merc debug sections (`.debug_*`), using the same `0x5D05` bitmask.

### sh\_type Map for Mercury Sections

| sh\_type | Hex | Section types |
|---|---|---|
| 1 | `0x00000001` | `.nv.capmerc<func>`, `.nv.merc.debug_abbrev`, `.nv.merc.debug_str`, `.nv.merc.nv_debug_ptx_txt` |
| 4 | `0x00000004` | `.nv.merc.rela*` (SHT\_RELA) |
| 18 | `0x00000012` | `.nv.merc.symtab_shndx` (SHT\_SYMTAB\_SHNDX) |
| SHT\_LOPROC+6 | `0x70000006` | `.nv.merc.<memory-space>` clones |
| SHT\_LOPROC+8 | `0x70000008` | `.nv.merc.nv.shared.reserved` |
| SHT\_LOPROC+12 | `0x7000000C` | Mercury data sections (during output serialization) |
| SHT\_LOPROC+13 | `0x7000000D` | Mercury debug sections (during output serialization) |
| SHT\_LOPROC+14 | `0x7000000E` | `.nv.merc.debug_line` |
| SHT\_LOPROC+16 | `0x70000010` | `.nv.merc.debug_frame` |
| SHT\_LOPROC+17 | `0x70000011` | `.nv.merc.debug_info` |
| SHT\_LOPROC+18 | `0x70000012` | `.nv.merc.nv_debug_line_sass` |
| SHT\_LOPROC+20 | `0x70000014` | `.nv.merc.debug_loc`, `.nv.merc.debug_ranges`, `.nv.merc.nv_debug_info_reg_*` |
| SHT\_LOPROC+100..+126 | `0x70000064`..`0x7000007E` | Memory-space variant sections (constant banks, shared, local, global) |

The `.nv.merc.*` debug sections reuse the same `sh_type` values as their non-merc counterparts. The `SHF_NV_MERC` flag (`0x10000000`) in `sh_flags` is the distinguishing marker.

### Capsule Descriptor Layout

The per-function `.nv.capmerc<funcname>` section contains a 328-byte capsule descriptor. For the full byte-level layout including all 7 field groups (Identity, SASS Data, Relocation Infrastructure, Function Metadata, Code Generation Parameters, Constant Bank Info, KNOBS Embedding), marker stream TLV format, and sub-byte relocation design, see [ptxas: Capsule Mercury & Finalization](../../../../ptxas/wiki/src/codegen/capmerc.md) which documents the descriptor format at the compiler-output level. nvlink reads and preserves these descriptors through the linking pipeline without modification.

## Production Pipeline

The capmerc output pipeline in `main()` follows a distinct path from traditional cubin serialization.

### Step 1: ELF Construction and Finalization

The standard linking pipeline runs identically for both sass and capmerc output: input parsing, merge (`sub_45E7D0`), shared memory layout (`sub_439830`), relocation application (`sub_469D60`), and ELF finalization (`sub_445000`). The finalization phase handles the `0xFF00` ELF type with special-case logic for virtual-to-physical section index remapping and Mercury-specific symbol binding rules (see [Finalization Phase](../pipeline/finalize.md)).

### Step 2: Serialize to Memory (sub\_45C950)

Instead of writing directly to a file, the Mercury path:

1. Computes total ELF size via `sub_45C980`.
2. Allocates a contiguous memory buffer from the arena.
3. Calls `sub_45C950` which uses the mode-4 (memcpy) polymorphic writer to serialize the complete ELF into the buffer.

This produces an `"in-memory-ELF-image"` — the string used as the FNLZR input identifier.

### Step 3: FNLZR Post-Link Transform (sub\_4275C0 -> sub\_4748F0)

The FNLZR (Finalizer) operates on the serialized ELF byte buffer. `sub_4275C0` is a 3,989-byte dispatch function that:

1. Builds a 160-byte configuration struct from global flags:
   - `byte_2A5F222` (Mercury mode)
   - `byte_2A5F225` (capmerc mode, controls config word at offset +24: value 4 or 5)
   - `byte_2A5F310` (shared flag)
   - TODO: identify true byte for `capmerc.secondary_flag` — previous wiki revisions cited `byte_2A5F210`, but `byte_2A5F210` is the `--disable-smem-reservation` CLI byte (feeds `merge_flags` bit 12) and has no documented capmerc/Mercury use
   - `byte_2A5F224`, `byte_2A5F223` (additional binary properties)
   - `byte_2A5F2A9` (mode selector)

2. Logs to stderr when verbose: `"FNLZR: Input ELF: %s"`, `"FNLZR: Post-Link Mode"`, `"FNLZR: Flags [ %u | %u ]"`, `"FNLZR: Starting %s"`, `"FNLZR: Ending %s"`.

3. Calls `sub_4748F0` (48,730 bytes, 1,830 lines), the top-level link-and-finalize engine with 25 parameters. This function:
   - Parses the binary-kind specification to determine `mercury`, `capmerc`, or `sass` output.
   - Processes finalization options: `"cap-merc"`, `"self-check"`, `"out-sass"`, `"fastpath-off"`, `"opportunistic-finalization-lvl"`.
   - Calls `sub_471700` (78,516 bytes), the main finalization orchestrator that compiles Mercury intermediate code to final SASS instruction encodings.
   - Manages Hash Relocation sections (`.nvHRKE`, `.nvHRKI`, `.nvHRCE`, `.nvHRCI`, `.nvHRDE`, `.nvHRDI`) for incremental linking support.

Two FNLZR modes exist:

| Mode | Config | When |
|---|---|---|
| Pre-Link (`a5=0`) | Used on individual input objects | During input processing |
| Post-Link (`a5=1`) | Used on the final linked output | After ELF serialization |

### FNLZR Configuration Struct (160 bytes)

`sub_4275C0` builds a 160-byte configuration struct (`v28[0..19]`, with bytes 8-159 zeroed via `memset(&v28[1], 0, 0x98)`) before calling `sub_4748F0`:

| Offset | Size | Field | Source |
|---|---|---|---|
| +0 | 8 | `output_elf_ptr` | Set by `sub_4748F0` on return |
| +24 | 4 | `mode_selector` | `4 + (byte_2A5F310 != 0)` or `5` when `byte_2A5F310 && !byte_2A5F2A9` |
| +28 | 1 | `shared_flag` | `byte_2A5F310 != 0` |
| +31 | 1 | `secondary_flag` | TODO: identify true byte for `capmerc.secondary_flag`; `byte_2A5F210` was previously listed but is the `--disable-smem-reservation` CLI byte (feeds `merge_flags` bit 12), not a Mercury-subsystem flag |
| +64 | 4 | `(unknown)` | Set to `3` in some paths |
| +104 | 1 | `mercury_mode` | `1` when Mercury active (not capmerc) |
| +105 | 1 | `capmerc_mode` | `1` when `byte_2A5F225` set |
| +106 | 1 | `always_1` | Always set to `1` |
| +107 | 1 | `sm_gt_72` | `byte_2A5F224 != 0` |
| +108 | 1 | `sm_gt_99_variant` | `byte_2A5F223 != 0` |

The mode\_selector at +24 controls finalization behavior: 4 = standard finalization, 5 = shared-mode finalization (when `byte_2A5F310` is active). The FNLZR logs the two flag bytes as `"FNLZR: Flags [ %u | %u ]"` where the first value is the mercury\_mode flag and the second is the capmerc\_mode flag.

The post-link mode is the one that produces the final capmerc binary. Pre-link mode runs earlier in the pipeline on individual cubin inputs that need Mercury-level transformation before merging.

### Step 4: Write Finalized Binary

After FNLZR returns, `main()` writes the transformed buffer to the output file via `fwrite()`.

### Mercury Section Emission Order

During ELF output serialization (`sub_1CEE030`), Mercury sections are emitted in five passes:

1. **Data sections** — `.nv.merc.*` memory-space clones (constant banks, shared, local, global). Written with `sh_type = 0x7000000C` (SHT\_LOPROC+12) and `sh_flags = 0x10000000` (SHF\_NV\_MERC). Section headers are copied via SSE-optimized `_mm_loadu_si128` operations.

2. **Debug sections** — `.nv.merc.debug_*` and `.nv.merc.nv_debug_*`. Written with `sh_type = 0x7000000D` (SHT\_LOPROC+13) and `sh_flags = 0x10000000`. Output offset is aligned to each section's `sh_addralign` value.

3. **Relocation sections** — `.nv.merc.rela*`. Written with `sh_type = 0x70000064` (SHT\_LOPROC+100) in Mercury mode or `1` (SHT\_PROGBITS) in some variants. `sh_flags = 0x42`.

4. **Remaining sections** — any additional Mercury sections from the 280-entry section list.

5. **Extended section index** — `.nv.merc.symtab_shndx` (sh\_type = 18, SHT\_SYMTAB\_SHNDX). Created only when section indices exceed `0xFF00`. When a symbol references a section with index > `0xFF00`, the symbol table entry stores `0xFFFF` and the actual index is recorded in this extended section index table.

### Output Filename

The output filename is selected in `main()` based on architecture and binary-kind flags:

| Condition | Filename |
|---|---|
| arch <= 0x63 | `cubin` (standard) |
| arch > 0x63, SASS mode | `sass.cubin` |
| arch > 0x63, capmerc mode (`byte_2A5F222`) | `capmerc.cubin` |
| arch > 0x63, mercury mode (`byte_2A5F225`) | `merc.cubin` (computed as `"capmerc.cubin" + 3`) |

### Fastpath Optimization

The finalizer supports a fastpath for "off-target" finalization, logged as:

```text
[Finalizer] fastpath optimization applied for off-target %u -> %u finalization
```

This occurs when the driver's target architecture differs from the compilation architecture but belongs to the same decade family (e.g., sm\_100 -> sm\_103, since `100/10 == 103/10`). The fastpath avoids a full re-finalization by applying only the delta between the two ISA variants. The `--fastpath-off` flag disables this optimization.

### Opportunistic Finalization

The `--opportunistic-finalization-lvl` option controls when off-target finalization is attempted. The CLI help string at `0x1D41EE2` documents only four values:

```text
Specify the opportunistic finalization level. 0=default,
1=no opportunistic finalization, 2=intra family finalization only,
or 3=intra and inter family finalization
```

| Level | Behavior | Source |
|---|---|---|
| 0 | Default (driver decides) | help text |
| 1 | No opportunistic finalization | help text |
| 2 | Intra-family finalization only | help text |
| 3 | Intra and inter family finalization | help text |
| 4 | Undocumented; parser accepts but help text omits | parser only |

The option parser (`sub_4AC380`) range-checks the integer with `value > 4` (rejecting 5 and above), so values 0..4 reach the option store while the help string only advertises 0..3. Level 4 is reachable through the CLI but has no documented semantics; its observable effect is the value written into `EICOMPAT_ATTR_ENABLE_OPPORTUNISTIC_FINALIZATION`, which the driver interprets. Treat level 4 as an undocumented escape hatch rather than a stable interface.

## Architecture Compatibility

The finalization compatibility checking functions (`sub_4709E0`, `sub_470DA0`) determine whether a capmerc binary can be finalized for a target architecture. The checks use:

1. **Internal architecture remapping**: 104 -> 120, 130 -> 107, 101 -> 110 (mapping internal variant codes to canonical family representatives).

2. **Decade-family matching**: Two architectures are in the same family if `arch1/10 == arch2/10`. For example, sm\_100 and sm\_103 are both in the "10x" decade.

3. **Capability bitmask matching**: Each architecture has a capability bitmask:
   - `sm_100` (code 'd'/100) = bit 0 (value 1)
   - `sm_103` (code 'g'/103) = bit 3 (value 8)
   - `sm_110` (code 'n'/110) = bit 1 (value 2)
   - `sm_121` (code 'y'/121) = bit 6 (value 64)

4. **Version bounds**: Finalization version > `0x101` returns error code 25 (version too high). The finalization class (byte at offset +3 of the arch info struct) has values 0-4, dispatched through `dword_1D40660[]`.

5. **Environment override**: The `CAN_FINALIZE_DEBUG` environment variable enables debug logging of compatibility decisions.

### Architecture Compatibility Return Codes

`sub_4709E0` returns a numeric error code indicating the compatibility result:

| Return | Meaning |
|---|---|
| 0 | Compatible — finalization allowed |
| 24 | NULL architecture profile (`a1 == NULL`) |
| 25 | ISA version too high (`> 0x101`) |
| 26 | Incompatible finalization class (general) |
| 27 | Finalization class 4 restriction |
| 28 | Finalization class 3 restriction (only allows sm\_100 -> sm\_102/sm\_103 with specific bit checks, or sm\_120 -> sm\_121) |
| 29 | Finalization class 2 restriction (same-decade only, source must be < target) |
| 30 | Unknown finalization class (byte not in range 0-4) |

The finalization class at offset +3 of the arch info struct controls the rules. Class 0 is the most restrictive (no cross-arch if special flag set, no sm\_110 involvement). Class 4 is the most permissive (allows cross-family including sm\_110 and sm\_121). Classes 1-3 require the source SM to be less than the target SM. The special flag at offset +4 further restricts class 1 (returns error 26 if set) and relaxes classes 2-3 (returns error 26 for classes 0-1 if special flag is set).

## Self-Check Mechanism

The `--self-check` flag triggers a validation pass where the linker reconstitutes SASS from the capmerc binary and compares it against expected output. Three sections are validated independently:

| Check | Error string |
|---|---|
| Text section | `"Self check for capsule mercury text section failed"` |
| Debug section | `"Self check for capsule mercury debug section failed"` |
| Relocation section | `"Self check for capsule mercury relocation section failed"` |

A more detailed failure message references the internal Jira page:

```text
Failure of '%s' section in self-check for capsule mercury.
See the Jira confluence page 'MERCSW-125' for more information
that includes some debugging steps.
```

The `--out-sass` option works only through self-check mode. Its help text states:

```text
Generate output of capmerc based reconstituted sass only through -self-check
```

The reconstitution itself is performed by `sub_5207A0` (18,673 bytes, 784 lines), an instruction opcode dispatch table that routes opcode case IDs (1..49+) to the encoding handler `sub_A49120` with opcode dispatch IDs (827..875+). This function is part of the reconstitution pipeline that decodes Mercury intermediate sections and re-encodes them as flat SASS instruction bytes using the instruction encoding engine at `sub_4C7D10`.

## Mercury Uplift

The error string `"Invalid elf provided for mercury uplift."` reveals a conversion mechanism called "mercury uplift" — the process of converting a traditional SASS cubin into a capmerc binary. When an input cubin is detected as SASS-only (fatbin member type is default cubin rather than 16), the linker can "uplift" it by wrapping the SASS content in Mercury sections. This operation validates that the input ELF is structurally sound before attempting the conversion.

The `MercGenerateSassUCode` string at `0x2443D02` names the internal pass that generates Mercury microcode from SASS input, forming the core of the uplift pipeline. The companion target-fixup stage `PostFixForMercTargets` (string at `0x2443C44`, master-table entry at `0x24443C0`) runs as the first Mercury-specific phase after register allocation; uplift relies on it to apply Mercury-only instruction rewrites before SASS UCode emission. See [Mercury Compiler Passes](compiler-passes.md) for the full phase table.

## Capmerc Binary String Anchors

The following nine strings together fully characterise the capmerc surface in the linker binary; every claim on this page is traceable to one of them.

| Address | String | Xref function | Role |
|---|---|---|---|
| `0x1D33FA9` | `capmerc.cubin` | `main` | Default output filename in capmerc mode |
| `0x1D41D03` | `mercury,capmerc,sass` | `sub_4AC380` (`0x4AC55C`) | `--binary-kind` choice list |
| `0x1D41D94` | `<mercury\|capmerc\|sass>` | `sub_4AC380` | `--binary-kind` argument hint |
| `0x1D41E78` | `Specify the type of target ELF binary kind. Default on sm100+ is capmerc` | `sub_4AC380` | Help text confirming sm100+ default |
| `0x1D41ED9` | `Generate Capsule Mercury` | `sub_4AC380` | `--cap-merc` help text |
| `0x1D41EF8` | `Self check for capsule mercury (capmerc)` | `sub_4AC380` | `--self-check` help text |
| `0x1D41F2A` | `Generate output of capmerc based reconstituted sass only through -self-check` | `sub_4AC380` | `--out-sass` help text |
| `0x2458F38` / `0x2458F70` / `0x2458FA8` | `Self check for capsule mercury text/debug/relocation section failed` | self-check verifier | Per-section self-check failure |
| `0x1F44288` | `Failure of '%s' section in self-check for capsule mercury. See the Jira confluence page 'MERCSW-125' for more information...` | self-check verifier | MERCSW-125 Jira anchor |

## FNLZR Internals

The FNLZR finalization orchestrator (`sub_471700`, 78,516 bytes) is the largest function in the finalization subsystem. It:

1. Allocates a 656-byte compilation unit descriptor with a vtable at `off_1D49C58`.
2. Copies a 256-byte architecture profile via SSE-accelerated `_mm_loadu_si128` operations.
3. Parses key-value options from the configuration: `"deviceDebug"`, `"lineInfo"`, `"optLevel"`, `"IsCompute"` (True/False), `"IsPIC"` (True/False).
4. Builds compiler flags and concatenates with existing flags at `v4+48`.
5. Sets up the compilation unit: arch version at `v4+28`, PIC flag at `v4+32`, debug flags at `v4+36`/`+40`, optimization level at `v4+104`.
6. Allocates a 648-byte section builder (offsets 448+) and initializes it via `sub_4F5880`.
7. Creates sections including `.nv.merc.`, `.nvFatBinSegment`, `__nv_relfatbin`, `.nv_fatbin`, `.note.nv.tkinfo`, `.symtab`.
8. Uses `"Final memory space"` as the arena name for finalization allocations.
9. Outputs the finalized ELF with version string `"Cuda compilation tools, release 13.0, V13.0.88"`.

## JIT Finalization Path

A separate JIT finalization entry exists at `sub_52DD50` (781 bytes; `0x52DD50`..`0x52E05C`), the JIT-specific wrapper that owns all five `FNLZR: ... JIT` diagnostic strings (verified xrefs at `0x52DDE1`, `0x52DE07`, `0x52DFB0`, `0x52DFFB`, `0x52E049`). `sub_52DD50` delegates to the same `sub_4748F0` engine used by the AOT path. The byte-adjacent function `sub_52E060` (11,802 bytes; starts at `0x52E060`, immediately after `sub_52DD50` ends at `0x52E05C`) is **not** part of the finalizer — it is the embedded nvJIT API option parser (the entry point installed at `qword_2A77DD0` by `sub_4FFC30` and dispatched by `sub_4BE350`/`sub_4BDB90`; see [PTX Input Handling](../input/ptx-input.md)). Its decompilation contains no `FNLZR`/JIT/ptxas-internal strings; instead it owns `"nvJIT API"`, `"JIT API Command Line Options"`, `"in-memory-ELF-image"`, and the toolchain version banner, plus a 23-entry option-handler dispatch table at `0x1df8ce0..0x1df8d90` whose every slot lands back inside itself. The 23 paired `_setjmp` buffers are per-option longjmp targets for option-parse failure (not C++ exception machinery). The `sub_52DD50` wrapper handles the CUDA driver's JIT finalization path with its own logging:

```text
FNLZR: JIT Path
FNLZR: preLink Mode
FNLZR: postLink Mode
FNLZR: Flags [ %u | %u ]
FNLZR: Starting JIT
FNLZR: Ending JIT
```

The JIT wrapper shares the underlying finalization orchestrator (`sub_471700`) with the ahead-of-time path but adds JIT-specific framing for on-the-fly architecture selection and runtime option injection.

## Error Conditions

| Error string | Trigger |
|---|---|
| `"Invalid elf provided for mercury uplift."` | Input ELF is malformed for uplift conversion |
| `"Self check for capsule mercury text section failed"` | Self-check text mismatch |
| `"Self check for capsule mercury debug section failed"` | Self-check debug mismatch |
| `"Self check for capsule mercury relocation section failed"` | Self-check relocation mismatch |
| `"SASS generation failed"` | Reconstitution engine failure |
| `"the elf arch is not compatible with finalizer arch"` | Architecture mismatch during finalization |
| `"conflicting options provided for finalizer"` | Contradictory FNLZR options |
| `"Failed to create finalizer thread"` | Thread creation failure in parallel finalization |
| `"Param struct passed to finalizer is Nil"` | NULL parameter to finalizer entry |
| `"Internal FNLZR error '%s'"` | Generic finalizer error with detail string |
| `"Cannot target %s when input '%s' is SASS"` | Attempting capmerc output from SASS-only input |
| `"skip mercury section %i"` | Verbose message during merge skipping Mercury sections |
| `"cubin not an elf?"` | Input cubin fails ELF magic validation |
| `"cubin not a device elf?"` | Input ELF has wrong `e_machine` |

## Function Map

| Address | Size | Identity | Role |
|---|---|---|---|
| `sub_4AC380` | 9,967 B | nvlink_options_define_table | Binary-kind option registration |
| `sub_4AD420` | 12,265 B | nvlink_options_postprocess | Option validation and defaults |
| `sub_4275C0` | 3,989 B | fnlzr_post_link | FNLZR dispatch (pre-link / post-link) |
| `sub_4748F0` | 48,730 B | nvlink_link_and_finalize_entry | Top-level FNLZR engine (25 params) |
| `sub_471700` | 78,516 B | nvlink_finalize_object | Main finalization orchestrator |
| `sub_4709E0` | 2,609 B | can_finalize_architecture_check | Architecture compatibility check |
| `sub_470DA0` | 2,074 B | can_finalize_with_capability_mask | Capability bitmask check |
| `sub_5207A0` | 18,673 B | capmerc_reconstitute_sass | SASS reconstitution from Mercury |
| `sub_52DD50` | 781 B | finalizer_jit_entry | JIT-path wrapper (owns all `FNLZR: ... JIT` strings); delegates to `sub_4748F0` |
| `sub_52E060` | 11,802 B | nvjit_api_option_parser | Embedded nvJIT API option parser — byte-adjacent to `sub_52DD50` but unrelated to finalization; installed at `qword_2A77DD0` by `sub_4FFC30` (documented in [PTX Input Handling](../input/ptx-input.md)) |
| `sub_45C950` | ~1 KB | write_elf_to_memory | Serialize ELF to buffer (Mercury path) |
| `sub_45C980` | ~1 KB | compute_elf_size | Compute serialized ELF byte count |
| `sub_45BF00` | 13,258 B | serialize_elf | Core ELF serialization engine |
| `sub_42AF40` | 11,143 B | extract_and_process_fatbin_member | Fatbin extraction (type 16 = capmerc) |
| `sub_1CED0E0` | 9,262 B | ELF_EmitDebugSections | Mercury debug section emitter |
| `sub_1CED7C0` | 6,757 B | ELF_EmitSASSDebugSections | Mercury SASS debug section emitter |
| `sub_1CEC390` | ~500 B | classify_shared_reservation | Identify `.nv.shared.reserved` and `.nv.merc.nv.shared.reserved` sections |
| `sub_1CEC4C0` | ~200 B | merc_section_name_construct | Prepend `".nv.merc"` to section names |
| `sub_1CEC570` | ~250 B | merc_composite_name_construct | Build composite `.nv.merc.*` names with multiple parts |
| `sub_1CEC660` | ~400 B | merc_constant_bank_section_map | Map `.nv.constant*` to `.nv.merc` equivalents with bank suffixes |
| `sub_1CEF5B0` | 22,867 B | ELF_ProcessRelocations | Mercury relocation processing |
| `sub_1CF1690` | 16,049 B | ELF_EmitRelocationTable | Mercury relocation table emitter |
| `sub_1CF72E0` | ~3 KB | emit_merc_rela_sections | Construct and emit `.nv.merc.rela*` sections |
| `sub_1CF3720` | ~10 KB | process_merc_symtab_shndx | Handle `.nv.merc.symtab_shndx` mapping |
| `sub_1CF7F30` | ~5 KB | emit_merc_rela_companion | Emit companion relocation sections |

## Global Variables

| Address | Type | Name | Description |
|---|---|---|---|
| `byte_2A5F222` | byte | mercury_mode | Set when arch > sm\_99 |
| `byte_2A5F225` | byte | capmerc_mode | Set alongside mercury_mode |
| `byte_2A5F224` | byte | sm_gt_72 | Set when arch > sm\_72 |
| `byte_2A5F229` | byte | ewp_detected | Mercury/EWP input detected (e\_type == 0xFF00) |
| `dword_2A5F314` | dword | arch_code | Target architecture numeric code |
| `dword_2A5F308` | dword | fnlzr_verbose | FNLZR verbose output flags |
| `byte_2A5F310` | byte | shared_flag | Controls FNLZR config word (+24: 4 or 5) |

## Cross-References

### nvlink Internal
- [R\_MERCURY Relocations](r-mercury-relocations.md) — full catalog of Mercury relocation types
- [Mercury ELF Sections](elf-sections.md) — detailed `.nv.merc.*` section layout
- [FNLZR Post-Link](fnlzr.md) — FNLZR binary rewriter internals
- [Mercury Overview](overview.md) — high-level Mercury architecture
- [Finalization Phase](../pipeline/finalize.md) — ELF finalization including 0xFF00 type handling
- [Output Phase](../pipeline/output.md) — Mercury output path details
- [Architecture Profiles](../targets/arch-profiles.md) — SM100+ architecture database
- [Fatbin Extraction](../input/fatbin-extraction.md) — fatbin member type 16 handling
- [CLI Options](../pipeline/cli-options.md) — `--binary-kind` and related flags

### Sibling Wikis
- [ptxas: Capsule Mercury & Finalization](../../../../ptxas/wiki/src/codegen/capmerc.md) — standalone ptxas capmerc format (Mercury section binary layouts, 328-byte capsule descriptor layout, sh\_type map, classifier algorithm with `0x5D05` bitmask, marker stream TLV format, rela entry format, sub-byte relocation design, finalization levels)
- [ptxas: Mercury Encoder Pipeline](../../../../ptxas/wiki/src/codegen/mercury.md) — standalone ptxas Mercury encode/decode pipeline (phases 113–122)

## Confidence Assessment

| Claim | Rating | Evidence |
|---|---|---|
| `--binary-kind=capmerc` is default for sm100+ | **HIGH** | String `"mercury,capmerc,sass"` at `0x1D41D03` verified. Option parser `sub_4AC380` decompiled (9,967 bytes, 429 lines). |
| ELF type `0xFF00` for capmerc | **HIGH** | Decompiled from `sub_4275C0` and `sub_4748F0`: ELF subtype check `elf_subtype == 0xFF00`. |
| `sub_4AC380` option parser (9,967 bytes, 429 lines) | **HIGH** | Decompiled file `sub_4AC380_0x4ac380.c` exists and confirms size/line count. |
| `byte_2A5F222` = Mercury mode, `byte_2A5F225` = capmerc mode | **HIGH** | Both globals referenced in decompiled `sub_4AC380` and `sub_4275C0`. |
| `sub_45C950` serialize-to-memory path | **HIGH** | Function called at `main` line 1462. `"in-memory-ELF-image"` string at `0x1D3236D` verified. `sub_45C980` computes size, buffer allocated, then `sub_45C950(buffer, elf)` serializes. |
| FNLZR dispatch `sub_4275C0` (3,989 bytes) | **HIGH** | Decompiled file exists. Size and parameter count verified. All call sites confirmed. |
| FNLZR engine `sub_4748F0` (48,730 bytes, 1,830 lines, 25 params) | **HIGH** | Decompiled file exists. Size, line count, and parameter count verified. |
| Finalization orchestrator `sub_471700` (78,516 bytes) | **HIGH** | Decompiled file exists. vtable `off_1D49C58`, 256-byte profile, 656-byte CU confirmed. |
| `sub_5207A0` SASS reconstitution (18,673 bytes) | **MEDIUM** | Function exists at stated address. Size from function bounds. Role inferred from call context and string proximity. |
| Fatbin member type 16 = capmerc | **MEDIUM** | Inferred from decompiled `sub_42AF40` fatbin extraction logic. No direct string evidence for the numeric value 16. |
| Self-check validates text/debug/relocation independently | **HIGH** | Three distinct error strings verified at `0x2458F38`, `0x2458F70`, `0x2458FA8`. MERCSW-125 reference at `0x1F44288`. |
| `--self-check`, `--out-sass`, `--fastpath-off` CLI options | **HIGH** | All option strings verified in `nvlink_strings.json`. Help text strings confirmed. |
| Opportunistic finalization levels 0–4 | **MEDIUM** | `EICOMPAT_ATTR_ENABLE_OPPORTUNISTIC_FINALIZATION` verified at `0x245EED8`. Parser accepts 0–4 (rejects > 4). Level 4 semantics undocumented. Levels 0–3 semantics partially inferred from code paths. |
| Decade-family matching (`arch1/10 == arch2/10`) | **HIGH** | Integer division comparison verified in decompiled `sub_4709E0`. |
| Version ceiling `> 0x101` returns error 25 | **HIGH** | Verified from decompiled `sub_4748F0` Phase 2. |
| `"Failed to create finalizer thread"` at `0x2458EC0` | **HIGH** | Verified in `nvlink_strings.json`. Confirms thread-based finalization. |
| JIT wrapper `sub_52DD50` (781 bytes) owns `FNLZR: ... JIT` strings | **HIGH** | All five JIT diagnostic strings (`"FNLZR: JIT Path"`, `"FNLZR: preLink Mode"`, `"FNLZR: postLink Mode"`, `"FNLZR: Ending JIT"`, `"FNLZR: Starting JIT"`) carry xrefs into `sub_52DD50` at `0x52DDE1`, `0x52DE07`, `0x52DFB0`, `0x52DFFB`, and `0x52E049` respectively (verified in `nvlink_strings.json`). |
| Byte-adjacent `sub_52E060` is **not** a finalizer function | **HIGH** | `sub_52E060` decompilation contains zero `FNLZR`/JIT/ptxas-internal strings. It owns `"nvJIT API"`, `"JIT API Command Line Options"`, `"in-memory-ELF-image"`, and the toolchain version banner (call at line 1345 to `sub_443730`). 23-entry option-handler dispatch table at `0x1df8ce0..0x1df8d90` lands every slot back inside `sub_52E060`; the matching 23 `_setjmp` buffers are per-option longjmp error-recovery targets, not C++ EH. Single caller chain: installer `sub_4FFC30` writes `qword_2A77DD0 = sub_52E060`, dispatcher `sub_4BE350` reads and calls through that pointer (see [PTX Input Handling](../input/ptx-input.md) and [Wave-17C correction](../input/ptx-input.md)). |
| Section catalog: 19 `.nv.merc.*` names | **HIGH** | All 19 section name strings verified at addresses `0x24582E8`--`0x2458D00`. Xrefs to emitter functions confirmed. |
| `"skip mercury section %i"` at `0x1D3BCB7` | **HIGH** | String verified at exact address with xref to `0x45F624`. |
| Hash Relocation sections `.nvHRKE`/`.nvHRKI`/`.nvHRCE`/`.nvHRCI`/`.nvHRDE`/`.nvHRDI` | **MEDIUM** | Section names inferred from decompiled code. Not individually verified in string scan. |
| `"SASS generation failed"` error string | **MEDIUM** | Not individually verified in `nvlink_strings.json` scan. May exist at an unchecked address. |
| SHF\_NV\_MERC = `0x10000000` (bit 28 of sh\_flags) | **HIGH** | Verified in 10+ decompiled locations: `sub_45E7D0` line 1583 (`v140 & 0x10000000`), `sub_1CED0E0` line 47 (`*((_QWORD *)a2 + 1) & 0x10000000`), `sub_1CEE030` line 322/370 (`0x10000000` written to sh\_flags). |
| Bitmask `0x5D05` for sh\_type classification | **HIGH** | Constant `23813` (`0x5D05`) verified in `sub_1CED0E0`, `sub_1CED7C0`, `sub_1CEF5B0`, and `sub_1CF1690`. Same bitmask used by ptxas classifier. |
| Section name constructors `sub_1CEC4C0`, `sub_1CEC660` | **HIGH** | Decompiled files verified. `sub_1CEC4C0` line 31: `sprintf(v10, "%s%s", ".nv.merc", v15)`. `sub_1CEC660` line 52: `sub_1CEC570(".nv.merc", ...)`. |
| Architecture compatibility return codes 0/24–30 | **HIGH** | All return paths verified in decompiled `sub_4709E0` (149 lines). Error code 25 at line 50-52, 26 at line 57, 28 at line 132, 29 at line 92, 30 at line 102/130. |
| FNLZR config struct 160-byte layout | **HIGH** | Decompiled `sub_4275C0` line 88: `memset(&v28[1], 0, 0x98)` (152 bytes + 8 for v28[0] = 160 total). All field assignments verified at lines 89-121. |
| Emission sh\_types `0x7000000C` and `0x7000000D` | **HIGH** | Verified in `sub_1CEE030` line 318: `v15->m128i_i32[1] = 1879048204` (= `0x7000000C`) and line 364: `*(_DWORD *)(v33 + 4) = 1879048205` (= `0x7000000D`). |
| `.nv.merc.rela` name construction (`name+8` stripping) | **HIGH** | Verified in `sub_1CF72E0` line 358: `sprintf(buf, "%s%s%s", ".nv.merc", ".rela", (const char *)(v60 + 8))`. |
