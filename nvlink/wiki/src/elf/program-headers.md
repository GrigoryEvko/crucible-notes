# Program Headers

Program headers in CUDA device ELFs describe loadable segments that the GPU driver maps into device memory. Unlike CPU ELFs where program headers drive virtual memory setup and dynamic linking, CUDA device ELFs use a minimal program header table that serves a narrower purpose: telling the CUDA runtime which byte ranges in the cubin file correspond to code (SASS instructions) versus data (constant banks, global initializers). The program header table is only emitted for executable ELFs (`e_type == ET_EXEC`); relocatable objects (`-r` mode) have no program headers. All `p_vaddr` fields are zero — the CUDA runtime uses `p_offset` for segment location and `p_flags` for segment classification.

The program header table is written by `sub_45BAA0` (5,657 bytes at `0x45BAA0`), called as the final step of ELF serialization from within `sub_45BF00`. The table is appended **after** the section header table, at the very end of the file. This placement is unusual compared to standard ELF practice where program headers typically sit near the beginning of the file, immediately after the ELF header. CUDA device ELFs place them last because the GPU driver parses section headers directly for detailed metadata access, and the program headers serve as a secondary, coarse-grained segment map.

## Key Facts

| Property | Value |
|---|---|
| Emitter function | `sub_45BAA0` at `0x45BAA0` |
| Emitter size | 5,657 bytes (228 lines) |
| Called from | `sub_45BF00` (ELF serialization engine), line 530 |
| Condition | `e_type == ET_EXEC` (value 2) and executable flag clear |
| ELF32 Phdr size | 32 bytes per entry |
| ELF64 Phdr size | 56 bytes per entry |
| Max entries | 4 (PT_PHDR + up to 2 PT_LOAD for data/code + 1 PT_LOAD for phdr table) |
| Max table size | 128 bytes (ELF32) or 224 bytes (ELF64) |
| Alignment | `p_align = 8` for all segments (ELF64), `p_align = 4` for all segments (ELF32) |
| Helper function | `sub_438BB0` at `0x438BB0` — align-up helper |
| Error handler | `sub_467460` with `"writing file"` on write failure |

## When Program Headers Are Emitted

Program headers are only emitted when three conditions are all satisfied:

1. **ELF type is ET_EXEC** (`elfw+16 == 2`): Relocatable links (`-r` flag) produce `ET_REL` objects that have no program headers.

2. **Executable flag is clear**: For certain ABI variants (when `elfw+7 == 'A'`, the NVIDIA a-variant), bit 0 of `e_flags` is checked; otherwise bit 31 (`0x80000000`) is checked. If the checked bit is set, program headers are suppressed. This flag indicates a "stub" or "partial" executable that should not have a loadable segment map (e.g., intermediate Mercury executables before FNLZR post-link).

3. **Section count is non-zero**: The ordered section array at `elfw+368` must contain sections to iterate.

When these conditions are not met, the serialization function (`sub_45BF00`) returns immediately after writing section headers, skipping the `sub_45BAA0` call entirely.

The size computation function `sub_45C980` reserves space for program headers using the same conditions:

```c
// sub_45C980 -- reserves program header space at end of ELF
// ELF32: adds 128 bytes (4 * 32-byte phdrs)
// ELF64: adds 224 bytes (4 * 56-byte phdrs)
if (e_type == ET_EXEC && !exec_flag_set)
    total_size += (elf_class == 2) ? 224 : 128;
```

## phnum Calculation

The number of program headers (written into `e_phnum` in the ELF header) is computed by `sub_45BF00` before calling `sub_45BAA0`. The logic scans all ordered sections to detect the presence of code and data segments:

```c
// Inside sub_45BF00, before calling sub_45BAA0
uint64_t code_base = 0;   // first code section's sh_addr
uint64_t data_base = 0;   // first data section's sh_addr

for (int i = 0; i < section_count; i++) {
    section_record *sec = get_ordered_section(elfw, i);
    uint32_t flags = sec->internal_flags;   // offset +8 in section record

    if (flags & 0x1) {     // code section (SHF_EXECINSTR equivalent)
        if (code_base == 0)
            code_base = sec->sh_addr;
    }
    else if (flags & 0x2) { // data section (writable/loadable data)
        if (data_base == 0)
            data_base = sec->sh_addr;
    }
}

// Compute phnum
uint16_t phnum;
if (code_base == 0 && data_base == 0)
    phnum = 2;                              // PT_PHDR + PT_LOAD(phdr)
else if (code_base != 0 && data_base != 0)
    phnum = 4;                              // PT_PHDR + PT_LOAD(data) + PT_LOAD(code) + PT_LOAD(phdr)
else
    phnum = 3;                              // PT_PHDR + PT_LOAD(one type) + PT_LOAD(phdr)
```

The result is:

| Code sections present | Data sections present | phnum |
|---|---|---|
| No | No | 2 |
| Yes | No | 3 |
| No | Yes | 3 |
| Yes | Yes | 4 |

After computing `phnum`, the value is written into the ELF header:

- **ELF32**: `e_phnum` at `elfw+44`, `e_phoff` = `e_shoff + e_shnum * e_shentsize` (appended after section headers)
- **ELF64**: `e_phnum` at `elfw+56`, `e_phoff` = `e_shoff + e_shnum * e_shentsize`

## Internal Section Flag Classification

The internal section record (not the raw ELF `sh_flags`) maintains a classification bitmask at offset `+8` that categorizes each section for program header assignment:

| Bit | Meaning | Typical sections | PT_LOAD `p_flags` |
|---|---|---|---|
| `0x1` | Code (executable instructions) | `.text.*` (SASS function bodies) | `6` (PF_R \| PF_W) |
| `0x2` | Data (loadable data/metadata) | `.nv.constant*`, `.nv.global*` | `5` (PF_R \| PF_X) |
| Neither | Non-loadable | `.nv.info*`, `.strtab`, `.symtab`, `.shstrtab` | Not in any PT_LOAD |

Within the code-section category, the emitter additionally checks whether a section is a "no-data" section that should not contribute to the segment's file size. This check uses the NVIDIA section type bitmask test:

```c
bool is_nobits(uint32_t sh_type) {
    if (sh_type == SHT_NOBITS)       // type 8
        return true;
    uint32_t idx = sh_type - 0x70000007;
    if (idx <= 14)
        return (0x400D >> idx) & 1;  // matches types at offsets 0, 2, 3, 14
    return false;
}
```

The base value `0x70000007` in the decompiled code appears as the decimal literal `1879048199`. The bitmask `0x400D` (`0100 0000 0000 1101` in binary) selects these NVIDIA section types as no-data:

| Offset from `0x70000007` | Type value | Name |
|---|---|---|
| 0 | `0x70000007` | SHT_CUDA_GLOBAL (uninitialized global device memory) |
| 2 | `0x70000009` | SHT_CUDA_LOCAL (per-kernel local memory) |
| 3 | `0x7000000A` | SHT_CUDA_SHARED (per-CTA shared memory) |
| 14 | `0x70000015` | SHT_CUDA_SHARED_RESERVED (compiler-reserved shared memory) |

Plus `SHT_NOBITS` (type 8) as a separate check. All five types represent GPU memory spaces that have addresses but carry no file-backed data — their `sh_size` defines a memory reservation only. Note that `SHT_CUDA_GLOBAL_INIT` (`0x70000008`, offset 1) is deliberately excluded because it carries initialized data that must be present in the file.

## Segment Construction

`sub_45BAA0` constructs the entire program header table on the stack as a single contiguous array, then writes it in one call to the polymorphic writer (`sub_45B6D0`). The function takes six parameters:

```c
void write_program_headers(
    elf_writer *writer,       // a1: polymorphic write target
    elfw       *elf,          // a2: ELF wrapper object
    uint32_t    section_count,// a3: number of ordered sections (e_shnum)
    int         is_64bit,     // a4: 1 if ELF64, 0 if ELF32
    uint64_t    data_base,    // a5: first data section base address (0 if none)
    uint64_t    code_base     // a6: first code section base address (0 if none)
);
```

### First Pass: Compute Segment Extents

Before constructing program header entries, the function iterates all ordered sections to compute the extent (offset + total size) of each segment:

```c
uint64_t code_offset = 0;     // tracks end of code address range relative to code_base
uint64_t code_nobits_sz = 0;  // accumulated aligned size of NOBITS code sections
uint64_t data_end = 0;        // end of data address range relative to data_base

for (int i = 0; i < section_count; i++) {
    section_record *sec = get_ordered_section(elfw, i);

    if (sec->internal_flags & 0x1) {        // code section
        if (is_nobits(sec->sh_type)) {
            // NOBITS code section -- accumulate aligned memory-only size,
            // record start offset (not end) since no file data
            code_nobits_sz = align_up(code_nobits_sz, sec->sh_addralign);
            code_offset = sec->sh_addr - code_base;
            code_nobits_sz += sec->sh_size;
        } else {
            // PROGBITS code section -- record end of address range
            code_offset = sec->sh_addr + sec->sh_size - code_base;
        }
    }
    else if (sec->internal_flags & 0x2) {   // data section
        data_end = sec->sh_addr + sec->sh_size - data_base;
    }
}
```

In practice, code sections (`.text.*`) are always `SHT_PROGBITS`, so `code_nobits_sz` remains 0 and `code_offset` equals the total file extent of the code segment. The NOBITS path exists defensively for hypothetical NOBITS code sections (e.g., if `SHT_CUDA_SHARED` sections ever had flag bit 0x1 set).

The `align_up` helper (`sub_438BB0`) computes:

```c
uint64_t align_up(uint64_t value, uint64_t alignment) {
    if (value % alignment == 0)
        return value;
    return value + alignment - (value % alignment);
}
```

### Program Header Table Layout (ELF64)

For 64-bit ELFs, the table is constructed as an array of 56-byte `Elf64_Phdr` structures. The entries are built in a fixed order, with optional entries skipped when their base address is zero:

**Entry 0: PT_PHDR (always present)**

```c
Elf64_Phdr {
    p_type   = 6    (PT_PHDR)
    p_flags  = 5    (PF_R | PF_X)
    p_offset = e_phoff
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = phnum * 56
    p_memsz  = phnum * 56
    p_align  = 8
}
```

This self-referential segment points to the program header table itself. Its file offset equals `e_phoff` (which is computed as `e_shoff + e_shnum * e_shentsize`), pointing past the section header table to where the program headers are physically written. The `p_vaddr` field is 0 (left zeroed by `memset`) because CUDA device ELFs do not use virtual address mappings in program headers — see the [p_vaddr section](#p_vaddr-and-the-segment-address-model) below.

**Entry 1: PT_LOAD for data (only if `data_base != 0`)**

```c
Elf64_Phdr {
    p_type   = 1    (PT_LOAD)
    p_flags  = 5    (PF_R | PF_X)
    p_offset = data_base
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = data_end
    p_memsz  = data_end
    p_align  = 8
}
```

Data sections are marked with `PF_R | PF_X` (5). On a GPU, these flags do not carry the same semantics as on a CPU — `PF_X` does not mean "executable" in the CPU sense. The CUDA runtime interprets these flags to distinguish read-only metadata (constant banks, global init data) from mutable code segments. `p_filesz` and `p_memsz` are both set to `data_end` because every section reaching the data branch is `SHT_PROGBITS` (constant banks, `.nv.global.init`, `.nv.host`); NOBITS device-memory sections (`.nv.global`, `.nv.local.*`, `.nv.shared.*`) carry internal flags 0 and never enter this segment, which is why no `data_nobits_sz` accumulator exists.

**Entry 2: PT_LOAD for code (only if `code_base != 0`)**

```c
Elf64_Phdr {
    p_type   = 1    (PT_LOAD)
    p_flags  = 6    (PF_R | PF_W)
    p_offset = code_base
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = code_offset
    p_memsz  = code_offset + code_nobits_sz
    p_align  = 8
}
```

Code sections are marked with `PF_R | PF_W` (6). The write permission reflects the reality that the GPU driver or FNLZR post-link transform may need to patch SASS instruction encodings (control-flow metadata, scheduling bits, instruction addresses) at load time.

The `p_filesz` and `p_memsz` fields differ structurally: `p_filesz` receives `code_offset` (the end of the address range for file-backed `SHT_PROGBITS` sections), while `p_memsz` receives `code_offset + code_nobits_sz` (adding the aligned, accumulated size of any `is_nobits()`-classified sections so the loader reserves device memory the file does not back). In practice, all code sections (`.text.*`) are `SHT_PROGBITS`, so `code_nobits_sz` is 0 and `p_filesz == p_memsz`. The data branch performs no equivalent split: `p_filesz = p_memsz = data_end` unconditionally, which relies on NOBITS variables (`.nv.global`, `.nv.local.*`, `.nv.shared.*`) never being classified into the data segment — see [Section-to-Segment Mapping](#section-to-segment-mapping) below.

**Entry 3: PT_LOAD for program header table (always present)**

```c
Elf64_Phdr {
    p_type   = 1    (PT_LOAD)
    p_flags  = 5    (PF_R | PF_X)
    p_offset = e_phoff
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = phnum * 56
    p_memsz  = phnum * 56
    p_align  = 8
}
```

The final entry always covers the program header table itself, making the same byte range loadable that the PT_PHDR entry describes. Both entry 0 (PT_PHDR) and this entry point to the identical file range: `e_phoff` through `e_phoff + phnum * 56`. The PT_PHDR provides standard ELF self-referential semantics, while this PT_LOAD entry marks the range as explicitly loadable. The `p_filesz` and `p_memsz` are both `phnum * sizeof(Elf64_Phdr)` (56 bytes each).

Note: The section header table (`e_shoff`, `e_shnum * 64` bytes) is NOT covered by any program header. The CUDA driver accesses section headers through direct `e_shoff`/`e_shnum` parsing, not through the program header table.

### Program Header Table Layout (ELF32)

For 32-bit ELFs, the table uses 32-byte `Elf32_Phdr` structures with 32-bit fields. The standard `Elf32_Phdr` field order differs from `Elf64_Phdr`:

```c
// ELF32: p_flags comes after the address/size fields
typedef struct {
    Elf32_Word  p_type;     // +0
    Elf32_Off   p_offset;   // +4
    Elf32_Addr  p_vaddr;    // +8
    Elf32_Addr  p_paddr;    // +12
    Elf32_Word  p_filesz;   // +16
    Elf32_Word  p_memsz;    // +20
    Elf32_Word  p_flags;    // +24
    Elf32_Word  p_align;    // +28
} Elf32_Phdr;               // 32 bytes total

// ELF64: p_flags moves to after p_type
typedef struct {
    Elf64_Word  p_type;     // +0
    Elf64_Word  p_flags;    // +4
    Elf64_Off   p_offset;   // +8
    Elf64_Addr  p_vaddr;    // +16
    Elf64_Addr  p_paddr;    // +24
    Elf64_Xword p_filesz;   // +32
    Elf64_Xword p_memsz;    // +40
    Elf64_Xword p_align;    // +48
} Elf64_Phdr;               // 56 bytes total
```

The entry semantics are identical to the ELF64 case, only the field widths and `p_align` value (4 instead of 8) differ:

**ELF32 PT_PHDR:**

```c
Elf32_Phdr {
    p_type   = 6
    p_offset = e_phoff       // 32-bit: elfw+28
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = phnum * 32
    p_memsz  = phnum * 32
    p_flags  = 5
    p_align  = 4
}
```

**ELF32 PT_LOAD segments** follow the same pattern with `p_flags = 5` for data, `p_flags = 6` for code, `p_vaddr = 0` for all entries, and `p_align = 4`. The final PT_LOAD entry covers the program header table with `p_filesz = phnum * 32`.

## p\_vaddr and the Segment Address Model

All program header `p_vaddr` and `p_paddr` fields are **zero** in CUDA device ELFs. The entire program header array is zeroed by `memset` before construction, and no code path writes a non-zero value to any `p_vaddr` or `p_paddr` position. Only `p_type`, `p_flags`, `p_offset`, `p_filesz`, `p_memsz`, and `p_align` are explicitly set.

This differs from CPU ELF conventions where `p_vaddr` is the process virtual address the loader maps the segment to. In CUDA device ELFs, GPU virtual addresses are assigned by the driver at kernel launch time — the cubin file does not dictate mapping addresses. The CUDA runtime locates segment data using `p_offset` alone and ignores `p_vaddr`. Section `sh_addr` values (which equal file offsets in CUDA device ELFs) provide per-section addressing but are not propagated into program header virtual addresses.

## Segment Flag Inversion

The assignment of `p_flags` values in CUDA device ELFs is inverted from CPU ELF conventions:

| Segment type | CPU ELF convention | CUDA device ELF |
|---|---|---|
| Executable code | `PF_R \| PF_X` (5) | `PF_R \| PF_W` (6) |
| Read-only data | `PF_R` (4) | `PF_R \| PF_X` (5) |
| Read-write data | `PF_R \| PF_W` (6) | (not used) |

The rationale:

- **Code gets PF_R\|PF_W (6)**: SASS instructions may be patched by the driver or FNLZR at load time. The Mercury post-link transform (`sub_4275C0`) rewrites instruction scheduling metadata, control-flow annotations, and branch target addresses after the full binary is available. The write permission signals that code bytes are mutable up until dispatch.

- **Data/metadata gets PF_R\|PF_X (5)**: Constant banks and metadata sections are immutable once loaded. The `PF_X` bit does not mean "executable" — it is repurposed as a flag indicating the segment contains structured metadata that the CUDA runtime should parse (section headers, constant initializers) rather than instruction bytes.

## PT\_NOTE Absence

nvlink produces **no PT_NOTE segments**. Although `.note.nv.cuinfo` and `.note.nv.tkinfo` sections exist as `SHT_NOTE` sections in the section header table, they are not covered by any program header. The CUDA driver accesses these note sections through section header parsing (`e_shoff` + index lookup), not through the program header table. The entire sub_45BAA0 function contains no reference to PT_NOTE (type 4).

## Section-to-Segment Mapping

The mapping from sections to segments is determined by the internal flags bitmask at offset `+8` of each section record, NOT by standard ELF `sh_flags`:

| Internal flag | Segment | Covered sections |
|---|---|---|
| Bit 0x1 (code) | PT_LOAD flags=6 | `.text.*` (SASS function bodies) |
| Bit 0x2 (data) | PT_LOAD flags=5 | `.nv.constant0.*`, `.nv.constant1.*`, `.nv.global.init`, `.nv.host` |
| Neither | No segment | `.nv.info*`, `.nv.callgraph`, `.nv.prototype`, `.nv.metadata`, `.strtab`, `.symtab`, `.shstrtab`, `.note.*`, `.debug_*`, `.rel*`, `.rela*` |
| N/A | PT_LOAD flags=5 | Program header table itself (always) |
| N/A | PT_PHDR | Program header table itself (always) |

NOBITS sections (`SHT_NOBITS`, `SHT_CUDA_GLOBAL`, `SHT_CUDA_LOCAL`, `SHT_CUDA_SHARED`, `SHT_CUDA_SHARED_RESERVED`) carry a `sh_size` that reserves memory but is not backed by file bytes — the data emitter in `sub_45C950` skips them entirely (see [Output Phase 6: NOBITS skip](../pipeline/output.md#phase-6-section-data)). When such a section is in the code segment (internal flag bit 0x1), `sub_45BAA0` accumulates its aligned `sh_size` into `code_nobits_sz` so `p_memsz = code_offset + code_nobits_sz` exceeds `p_filesz = code_offset` by exactly that reservation. The data branch has no symmetric accumulator, so NOBITS variables are kept out of the data segment by classification: `.nv.global` (`SHT_CUDA_GLOBAL`), `.nv.local.*`, `.nv.shared.*`, and `.nv.reservedSmem*` all receive internal flags 0 (neither code nor data), so they appear in the section header table but in no `PT_LOAD`. Only `.nv.global.init` (`SHT_PROGBITS`) and the constant banks reach the data segment, keeping `p_filesz == p_memsz` for it. The NOBITS branch on the code path therefore remains defensive against future reclassification rather than active in any current build.

## Mercury and Stub Executables

For Mercury targets (sm >= 100), the program header emission path has an additional gate. The executable-flag check (`e_flags & 0x80000000` or `e_flags & 1` depending on the ABI variant) can suppress program header emission entirely:

- **Pre-FNLZR Mercury executables**: These are intermediate ELFs produced before the FNLZR post-link pass. They have `e_type == ET_EXEC` but carry the executable flag bit set, indicating they are not yet finalized. The serialization function checks this flag and skips `sub_45BAA0`. The FNLZR pass will consume the ELF, rewrite it, and the final output may have its own segment structure.

- **Capmerc (capsule Mercury) targets**: Use ABI variant `'A'` where the flag check is on bit 0 rather than bit 31 of `e_flags`.

- **Standard (non-Mercury) executables**: sm < 100 targets always emit program headers for `ET_EXEC` ELFs.

The size computation function `sub_45C980` mirrors this logic exactly, so the pre-allocated buffer (used by the Mercury serialize-to-memory path) includes or excludes the 128/224 bytes based on the same condition.

### Mercury vs SASS Program Header Format

When program headers ARE emitted (post-FNLZR Mercury executables or standard SASS executables), the segment structure is **identical**. There is no Mercury-specific segment type, flag value, or alignment. The sub_45BAA0 function contains no architecture-specific code paths — it constructs the same PT_PHDR + PT_LOAD entries regardless of target SM version. The only difference is the emission gate: SASS always emits for `ET_EXEC`; Mercury conditionally suppresses based on the `e_flags` finalization bit.

## File Layout Position

The program header table occupies the final bytes of the output cubin file. The overall file layout order is:

```text
Offset 0:                    ELF header (52 or 64 bytes)
                             Padding (1 byte)
                             .shstrtab data (section name strings)
                             .strtab data (symbol name strings)
                             Padding to alignment
                             Symbol table (.symtab) entries
                             Section data (in section-order)
                             Padding to e_shoff
e_shoff:                     Section header table (e_shnum * shdr_size)
e_phoff:                     Program header table (phnum * phdr_size)   <-- HERE
                             EOF
```

Where `e_phoff = e_shoff + e_shnum * e_shentsize`. This tail placement means the ELF header's `e_phoff` field points past the section header table, and `e_phentsize` is 32 (ELF32) or 56 (ELF64). The section header table is NOT covered by any program header; only the program header table itself is described by both the PT_PHDR and the last PT_LOAD entry.

## Practical Examples

### Typical sm\_90 cubin (ELF64, 3 kernels, constants + code)

```text
e_type    = ET_EXEC (2)
e_shoff   = 0x5000
e_shnum   = 18
e_phoff   = 0x5000 + 18*64 = 0x5480
e_phnum   = 4
e_phentsize = 56

Phdr[0]: PT_PHDR   flags=5  offset=0x5480  vaddr=0  filesz=4*56=224   align=8  [phdr table]
Phdr[1]: PT_LOAD   flags=5  offset=0x2000  vaddr=0  filesz=0x800      align=8  [constants]
Phdr[2]: PT_LOAD   flags=6  offset=0x3000  vaddr=0  filesz=0x1800     align=8  [.text.*]
Phdr[3]: PT_LOAD   flags=5  offset=0x5480  vaddr=0  filesz=224        align=8  [phdr table]
```

Note: Phdr[0] and Phdr[3] describe the same byte range (the program header table at `e_phoff`). Phdr[0] is PT_PHDR (type 6), Phdr[3] is PT_LOAD (type 1).

### Minimal cubin (ELF64, no loadable sections)

```text
e_type    = ET_EXEC (2)
e_shoff   = 0x200
e_shnum   = 5
e_phoff   = 0x200 + 5*64 = 0x340
e_phnum   = 2
e_phentsize = 56

Phdr[0]: PT_PHDR   flags=5  offset=0x340  vaddr=0  filesz=2*56=112  align=8  [phdr table]
Phdr[1]: PT_LOAD   flags=5  offset=0x340  vaddr=0  filesz=112        align=8  [phdr table]
```

### Relocatable object (no program headers)

```text
e_type    = ET_REL (1)
e_phnum   = 0
e_phoff   = 0
e_phentsize = 0
```

### Worked Example: sm\_89 cubin with 2 kernels (ELF64)

A concrete example showing the complete computation for a simple executable with two kernels and one constant bank:

```text
Sections (ordered by section index):
  [ 0] NULL              addr=0x0000  size=0x0000  flags=0x0  (neither)
  [ 1] .shstrtab         addr=0x0041  size=0x0120  flags=0x0  (neither)
  [ 2] .strtab           addr=0x0161  size=0x0060  flags=0x0  (neither)
  [ 3] .symtab           addr=0x01C8  size=0x00C0  flags=0x0  (neither)
  [ 4] .nv.info          addr=0x0288  size=0x0030  flags=0x0  (neither)
  [ 5] .nv.info.kernA    addr=0x02B8  size=0x0050  flags=0x0  (neither)
  [ 6] .nv.info.kernB    addr=0x0308  size=0x0050  flags=0x0  (neither)
  [ 7] .nv.constant0.kernA addr=0x0380 size=0x0100 flags=0x2  (data)
  [ 8] .nv.constant0.kernB addr=0x0480 size=0x0080 flags=0x2  (data)
  [ 9] .text.kernA       addr=0x0500  size=0x0600  flags=0x1  (code)
  [10] .text.kernB       addr=0x0B00  size=0x0300  flags=0x1  (code)
  [11] .nv.callgraph     addr=0x0E00  size=0x0018  flags=0x0  (neither)
  [12] .note.nv.cuinfo   addr=0x0E18  size=0x0010  flags=0x0  (neither)

Step 1: phnum computation (in sub_45BF00)
  code_base = 0x0500 (first section with flag bit 0x1 = section 9)
  data_base = 0x0380 (first section with flag bit 0x2 = section 7)
  Both nonzero -> phnum = 4

Step 2: e_phoff computation
  e_shoff = 0x0E28  (end of section data, aligned)
  e_shnum = 13
  e_shentsize = 64
  e_phoff = 0x0E28 + 13 * 64 = 0x0E28 + 0x340 = 0x1168

Step 3: First pass in sub_45BAA0
  Data sections (flag bit 0x2):
    Section 7: data_end = 0x0380 + 0x0100 - 0x0380 = 0x0100
    Section 8: data_end = 0x0480 + 0x0080 - 0x0380 = 0x0180

  Code sections (flag bit 0x1, all PROGBITS):
    Section 9:  code_offset = 0x0500 + 0x0600 - 0x0500 = 0x0600
    Section 10: code_offset = 0x0B00 + 0x0300 - 0x0500 = 0x0900
    code_nobits_sz = 0 (no NOBITS code sections)

Step 4: Program header construction
  Phdr[0]: PT_PHDR  (type=6, flags=5)
    p_offset = 0x1168 (e_phoff)
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = 4 * 56 = 224 (0xE0)
    p_memsz  = 224
    p_align  = 8

  Phdr[1]: PT_LOAD  (type=1, flags=5, data segment)
    p_offset = 0x0380 (data_base)
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = 0x0180 (data_end)
    p_memsz  = 0x0180
    p_align  = 8

  Phdr[2]: PT_LOAD  (type=1, flags=6, code segment)
    p_offset = 0x0500 (code_base)
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = 0x0900 (code_offset)
    p_memsz  = 0x0900 (code_offset + 0 = code_offset)
    p_align  = 8

  Phdr[3]: PT_LOAD  (type=1, flags=5, phdr table)
    p_offset = 0x1168 (e_phoff)
    p_vaddr  = 0
    p_paddr  = 0
    p_filesz = 224 (phnum * 56)
    p_memsz  = 224
    p_align  = 8

Total file size = 0x1168 + 224 = 0x1248
  (matches sub_45C980: e_shoff + shdr_table_size + 224)
```

## Decompiled Code Annotation

The core logic of `sub_45BAA0` is reproduced here with annotations explaining each block:

```c
// sub_45BAA0 -- write_program_headers
// a1 = writer, a2 = elfw, a3 = section_count, a4 = is_64bit
// a5 = data_base_addr, a6 = code_base_addr

// Phase 1: Iterate sections to compute segment extents
if (section_count > 0) {
    for (int i = 0; i < section_count; i++) {
        sec = get_ordered_section(elfw, section_order[i]);

        if (is_64bit) {
            // 64-bit section record: +4 (type), +8 (flags), +24 (addr), +32 (size), +48 (align)
            if (sec->flags_64 & 0x1) {           // code section
                if (is_nobits(sec->sh_type)) {
                    // NOBITS: accumulate aligned memory-only size
                    code_nobits_sz = align_up(code_nobits_sz, sec->sh_addralign_64);
                    code_offset = sec->sh_addr_64 - code_base;
                    code_nobits_sz += sec->sh_size_64;
                } else {
                    // PROGBITS: record end of address range
                    code_offset = sec->sh_addr_64 + sec->sh_size_64 - code_base;
                }
            } else if (sec->flags_64 & 0x2) {    // data section
                data_end = sec->sh_addr_64 + sec->sh_size_64 - data_base;
            }
        } else {
            // 32-bit section record: +4 (type), +8 (flags), +16 (addr), +20 (size), +32 (align)
            // Same logic with uint32_t fields
        }
    }
}

// Phase 2: Construct program header table on stack
if (is_64bit) {
    // 56-byte entries, up to 4 entries = 224 bytes max
    Elf64_Phdr phdrs[4];
    memset(phdrs, 0, sizeof(phdrs));    // zeroes all p_vaddr and p_paddr
    uint64_t e_phoff = *(uint64_t*)(elfw + 32);  // pre-computed by sub_45BF00
    uint16_t phnum   = *(uint16_t*)(elfw + 56);
    int idx = 0;

    // PT_PHDR -- self-reference to program header table
    phdrs[0].p_type  = PT_PHDR;    // 6
    phdrs[0].p_flags = PF_R|PF_X;  // 5
    phdrs[0].p_offset = e_phoff;   // points past section headers
    // p_vaddr = 0 (left zeroed)
    phdrs[0].p_filesz = phnum * 56;
    phdrs[0].p_memsz  = phnum * 56;
    phdrs[0].p_align  = 8;
    idx = 1;

    // PT_LOAD for data (if present)
    if (data_base != 0) {
        phdrs[idx].p_type  = PT_LOAD;     // 1
        phdrs[idx].p_flags = PF_R|PF_X;   // 5
        phdrs[idx].p_offset = data_base;
        // p_vaddr = 0 (left zeroed)
        phdrs[idx].p_filesz = data_end;
        phdrs[idx].p_memsz  = data_end;
        phdrs[idx].p_align  = 8;
        idx++;
    }

    // PT_LOAD for code (if present)
    if (code_base != 0) {
        phdrs[idx].p_type  = PT_LOAD;     // 1
        phdrs[idx].p_flags = PF_R|PF_W;   // 6
        phdrs[idx].p_offset = code_base;
        // p_vaddr = 0 (left zeroed)
        phdrs[idx].p_filesz = code_offset;
        phdrs[idx].p_memsz  = code_offset + code_nobits_sz;
        phdrs[idx].p_align  = 8;
        idx++;
    }

    // PT_LOAD for program header table (always present)
    phdrs[idx].p_type  = PT_LOAD;     // 1
    phdrs[idx].p_flags = PF_R|PF_X;   // 5
    phdrs[idx].p_offset = e_phoff;    // same as PT_PHDR entry
    // p_vaddr = 0 (left zeroed)
    phdrs[idx].p_filesz = phnum * 56; // same as PT_PHDR entry
    phdrs[idx].p_memsz  = phnum * 56;
    phdrs[idx].p_align  = 8;

    // Write entire table
    size_t table_size = phnum * 56;
    if (elf_write(writer, phdrs, table_size) != table_size)
        fatal_error("writing file");
} else {
    // 32-bit path: identical logic with 32-byte Elf32_Phdr, p_align=4,
    // e_phoff at elfw+28, phnum at elfw+44
}
```

## GPU Memory Mapping Implications

The program header `p_flags` values have specific implications for how the CUDA driver maps segments into GPU memory:

1. **PF_R\|PF_W segments (code)**: The driver allocates GPU memory with write access enabled. For pre-sm100 architectures, the SASS binary is copied directly and may receive minor patches (NOP slide insertion for debugging, instruction cache management). For Mercury targets (sm >= 100), the FNLZR post-link pass has already finalized instruction encodings before the cubin is written, but the write flag is retained for driver-side compatibility and potential future patching needs.

2. **PF_R\|PF_X segments (data/metadata and phdr table)**: The driver maps data segments as read-only. Constant bank data (`nv.constant0.*`) is loaded into constant cache hardware. The phdr table PT_LOAD (also PF_R\|PF_X) makes the program headers themselves loadable, though the runtime accesses them through `e_phoff` rather than segment mapping.

3. **Segments not covered by any PT_LOAD**: The section header table, `.nv.info.*`, `.strtab`, `.symtab`, `.shstrtab`, `.rel`/`.rela` sections, and DWARF debug sections are not part of any loadable segment. The driver accesses the section header table through `e_shoff`/`e_shnum` parsing and does not create GPU memory mappings for metadata sections.

## Function Reference

| Address | Name | Role |
|---|---|---|
| `0x45BAA0` | `write_program_headers` | Constructs and writes program header table |
| `0x45BF00` | `serialize_elf` | Calls `write_program_headers` as final step |
| `0x45C980` | `compute_elf_size` | Reserves 128/224 bytes for program header table |
| `0x438BB0` | `align_up` | Rounds value up to next multiple of alignment |
| `0x45B6D0` | `elf_write` | Polymorphic write dispatcher (writes the table) |
| `0x464DB0` | `list_get` | Retrieves section from ordered section list |
| `0x464BB0` | `list_count` | Returns count of entries in ordered list |
| `0x467460` | `fatal_error` | Reports fatal write errors |

## Cross-References

**Internal (nvlink wiki):**

- [Device ELF Format](device-elf-format.md) — ELF header fields (`e_phoff`, `e_phnum`, `e_phentsize`) and `e_flags` encoding that gates program header emission
- [ELF Serialization](serialization.md) — Phase 10 writes the program header table as the final step of the serialize engine
- [NVIDIA Section Types](nvidia-sections.md) — Section type constants and the `SHT_NOBITS`/`SHT_CUDA_*` classification used by the `is_nobits` bitmask
- [ELF Writer](../structs/elf-writer.md) — The 672-byte `elfw` struct and 40-byte polymorphic writer context that `sub_45BAA0` writes through
- [Layout Phase](../pipeline/layout.md) — Section address assignment that determines `code_base` and `data_base` inputs to program header construction
- [Output Writing](../pipeline/output.md) — Pipeline dispatch deciding between file and memory serialization paths
- [Mercury FNLZR](../mercury/fnlzr.md) — Pre-FNLZR stub executables that suppress program header emission via `e_flags` gating
- [Capsule Mercury Format](../mercury/capmerc-format.md) — ABI variant `'A'` where the flag check uses bit 0 instead of bit 31

**Sibling wikis:**

- [ptxas: ELF Emitter](../../ptxas/output/elf-emitter.html) — ptxas-side ELF emission that produces the input cubins consumed by nvlink
- [ptxas: Sections](../../ptxas/output/sections.html) — Section creation in ptxas that establishes the section types nvlink classifies for program headers

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| Emitter sub_45BAA0 (5,657 bytes) | HIGH | Decompiled file sub_45BAA0_0x45baa0.c exists; 6-parameter signature confirmed |
| Called from sub_45BF00 (serialization engine) | HIGH | sub_45BF00 decompiled file exists; call at line 530 confirmed |
| "writing file" error string | HIGH | String confirmed in nvlink_strings.json, xref to sub_45BAA0 |
| phnum = 2/3/4 based on code_base/data_base presence | HIGH | Verified in sub_45BF00 decompiled code; conditional phnum computation matches |
| ELF64 Phdr = 56 bytes, ELF32 Phdr = 32 bytes | HIGH | Standard ELF specification; confirmed by size constants 56 and 32 in decompiled code |
| PT_PHDR (type 6) always first entry | HIGH | Constant `0x500000006` at line 139 of sub_45BAA0: type=6, flags=5 |
| PF_R\|PF_W (6) for code, PF_R\|PF_X (5) for data | HIGH | Constants `0x600000001` (code) and `0x500000001` (data) confirmed |
| All p_vaddr = 0 | HIGH | memset zeroing at line 133/181; no subsequent write to any p_vaddr position |
| PT_PHDR p_offset = e_phoff (not e_shoff) | HIGH | Line 135: `v25 = *(a2+32)` reads e_phoff; line 137: stored as p_offset |
| Last PT_LOAD covers phdr table (not section headers) | HIGH | Line 171: `v53[v32] = v25` (e_phoff); line 173: `v53[v32+3] = v26` (phnum*56) |
| NOBITS bitmask base = 0x70000007 (not 0x70000008) | HIGH | Line 73: `v14 = v13 - 1879048199` where 1879048199 = 0x70000007 |
| NOBITS types: GLOBAL, LOCAL, SHARED, SHARED_RESERVED | HIGH | Bitmask 0x400D at offsets 0,2,3,14 from 0x70000007 = types 7,9,A,15 |
| Code p_filesz = code_offset, p_memsz = code_offset + code_nobits_sz | HIGH | Line 161: `v53[v30+3]=v10`; line 164: `v53[v30+4]=v9+v10` |
| p_align = 8 (ELF64) / 4 (ELF32) | HIGH | Alignment constants visible at lines 136/165/174 and 209/213/223 |
| Program headers placed after section headers (tail position) | HIGH | e_phoff = e_shoff + shdr_table_size computed in sub_45BF00 line 258 |
| sub_45C980 reserves 224 bytes (ELF64) / 128 bytes (ELF32) | HIGH | Decompiled sub_45C980: constants 224 (line 36) and 128 (line 19) |
| Condition: e_type == ET_EXEC (2) required | HIGH | sub_45BF00 line 529: `*(_WORD *)(v3 + 16) == 2` |
| e_flags bit 31 (0x80000000) suppresses phdrs | HIGH | sub_45C980 line 27: `v5 = 0x80000000` |
| ABI 'A' (0x41) uses bit 0 instead of bit 31 | HIGH | sub_45C980 line 28-29: `if (*(_BYTE *)(a1 + 7) == 65) v5 = 1` |
| align_up helper sub_438BB0 | HIGH | Decompiled file confirms: `value + alignment - (value % alignment)` |
| Segment flag inversion (code=RW, data=RX) | HIGH | Constants 0x600000001 (code) and 0x500000001 (data) confirmed |
| Mercury vs SASS: identical format when emitted | HIGH | sub_45BAA0 has no arch-specific code paths; only the emission gate differs |
| No PT_NOTE segments | HIGH | No reference to type 4 anywhere in sub_45BAA0 or sub_45BF00 |
