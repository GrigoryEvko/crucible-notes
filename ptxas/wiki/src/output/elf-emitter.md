# Custom ELF Emitter

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas builds its ELF/cubin output without libelf or any external ELF library. The entire ELF construction pipeline is a custom implementation spread across approximately 20 functions in the `0x1C99`--`0x1CD1` address range, totaling roughly 300 KB of binary code. At the center is a 672-byte in-memory object called the "ELF world" (`ELFW`), which owns all sections, symbols, and string tables. The emitter writes standard ELF headers with NVIDIA extensions: `EM_CUDA` (`0xBE` / 190) as the machine type, NVIDIA-specific section types (`SHT_CUDA_INFO` = `0x70000000` for `.nv.info`, `SHT_CUDA_CALLGRAPH` = `0x70000064` for `.nv.callgraph`, `SHT_CUDA_COMPAT` = `0x70000086` for `.nv.compat`), and CUDA-specific ELF flags encoding the SM architecture version. The design handles both 32-bit and 64-bit ELF classes, with the class byte at ELF offset 4 set to `'3'` (32-bit) or `'A'` (64-bit). Finalization is a single-pass algorithm that orders sections into 8 priority buckets, assigns file offsets with alignment, and handles the ELF extended section index mechanism (`SHN_XINDEX`) when the section count exceeds 65,280.

| | |
|---|---|
| **ELFW constructor** | `sub_1CB53A0` (3,480 bytes, 672-byte object) |
| **Section creator** | `sub_1CB3570` (1,963 bytes, 44 callers) |
| **Text section creator** | `sub_1CB42D0` (SHT_PROGBITS, SHF_ALLOC\|SHF_EXECINSTR) |
| **Symbol table builder** | `sub_1CB68D0` (9,578 bytes, ~1,700 lines) |
| **Master ELF emitter** | `sub_1C9F280` (15,263 bytes, 97 KB decompiled) |
| **Section layout calculator** | `sub_1C9DC60` (5,663 bytes, 29 KB decompiled) |
| **Symbol fixup** | `sub_1CB2CA0` (2,038 bytes) |
| **Section index remap** | `sub_1C99BB0` (4,900 bytes) |
| **ELF structure dumper** | `sub_1CB91C0` (2,668 bytes) |
| **File serializer** | `sub_1CD13A0` (2,541 bytes) |
| **Cubin entry point** | `sub_612DE0` (47 KB, called from `sub_446240`) |
| **ELF machine type** | `EM_CUDA` = `0xBE` (190) |
| **CUDA section type (`.nv.info`)** | `SHT_CUDA_INFO` = `0x70000000` |
| **CUDA section type (`.nv.callgraph`)** | `SHT_CUDA_CALLGRAPH` = `0x70000064` |
| **CUDA section type (`.nv.compat`)** | `SHT_CUDA_COMPAT` = `0x70000086` |
| **ELF magic** | `0x464C457F` (`\x7fELF`) |
| **Memory pool** | `"elfw memory space"` (4,096-byte initial) |

## Architecture

```text
sub_446240 (real main -- compilation driver)
  |
  v
sub_612DE0 (47KB -- cubin generation entry)
  |  Parses: deviceDebug, lineInfo, optLevel, IsCompute, IsPIC
  |  Sets up setjmp/longjmp error recovery
  |  Handles recursive self-call for nested finalization
  |
  +-- sub_1CB53A0 -------- ELFW constructor (672-byte object)
  |     |  Creates "elfw memory space" pool (4096 initial)
  |     |  Writes ELF magic 0x464C457F into header
  |     |  Sets e_machine = 0xBE (EM_CUDA)
  |     |  Sets EI_CLASS = 32-bit ('3') or 64-bit ('A')
  |     |  Creates 7 standard sections:
  |     |    .shstrtab, .strtab, .symtab, .symtab_shndx,
  |     |    .note.nv.tkinfo, .note.nv.cuinfo, .nv.uft.entry
  |     v
  +-- sub_1CB3570 x N ---- Section creator (44 call sites)
  |     |  Creates section: name, type, flags, link, info, align, entsize
  |     |  Auto-creates .rela/.rel companion for executable sections
  |     v
  +-- sub_1CB42D0 --------- .text.<funcname> section creator
  |     |  SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR
  |     |  One section per kernel/function
  |     v
  +-- sub_1CB68D0 --------- Symbol table builder (~1700 lines)
  |     |  Iterates internal symbol list
  |     |  Filters deleted symbols
  |     |  Handles __cuda_syscall special symbol
  |     |  Manages SHN_XINDEX overflow (>= SHN_LORESERVE)
  |     |  Builds .symtab_shndx extended index table
  |     v
  +-- sub_1CB2CA0 --------- Symbol fixup
  |     |  Renumbers symbols after section deletion
  |     |  Creates missing section symbols
  |     v
  +-- sub_1C99BB0 --------- Section index remap
  |     |  Reindexes sections after dead elimination
  |     |  Remaps .symtab_shndx / .nv.merc.symtab_shndx
  |     v
  +-- sub_1C9DC60 --------- Section layout calculator (29KB)
  |     |  Computes section offsets and sizes
  |     |  Skips .nv.constant0, .nv.reservedSmem
  |     |  Handles .debug_line special padding
  |     v
  +-- sub_1C9F280 --------- Master ELF emitter (97KB -- largest function)
  |     |  Copies ELF header (64 bytes via SSE loadu)
  |     |  Iterates sections via sub_1CB9FF0 / sub_1CB9C40
  |     |  Skips virtual sections (flag & 4)
  |     |  Patches ELF flags (SM version, ELFCLASS32/64)
  |     |  Handles program headers
  |     |  Embeds Mercury capsule if capmerc mode
  |     |  Processes debug sections
  |     v
  +-- sub_1CD13A0 --------- File serializer
        |  Iterates sections, writes with alignment padding
        |  Validates sizes: "section size mismatch"
        |  Handles 32-bit and 64-bit ELF formats
        v
      OUTPUT: .cubin / .o file on disk
```

## ELFW Object -- `sub_1CB53A0`

The ELFW constructor allocates and initializes a 672-byte object that serves as the central data structure for the entire ELF construction pipeline. Every section, symbol, and string table lives under this object. The constructor is called exactly once per compilation unit.

### Construction Sequence

```c
// sub_1CB53A0 -- ELFW constructor (simplified)
void* elfw_init(int elf_class, int sm_version) {
    // 1. Allocate 672-byte ELFW object from pool allocator
    void* elfw = sub_424070(672);

    // 2. Create dedicated memory pool
    sub_4258D0("elfw memory space", 0, 4096);

    // 3. Write ELF header
    *(uint32_t*)(elfw + 0) = 0x464C457F;   // e_ident[EI_MAG0..3] = "\x7fELF"
    *(uint8_t*)(elfw + 4)  = (elf_class == 64) ? 'A' : '3';  // EI_CLASS
    *(uint16_t*)(elfw + MACHINE_OFF) = 0xBE; // e_machine = EM_CUDA (190)

    // 4. Initialize section/symbol/string containers
    init_section_table(elfw);
    init_symbol_table(elfw);
    init_string_table(elfw);

    // 5. Create 7 mandatory sections
    add_section(elfw, ".shstrtab",        SHT_STRTAB,   0, ...);
    add_section(elfw, ".strtab",          SHT_STRTAB,   0, ...);
    add_section(elfw, ".symtab",          SHT_SYMTAB,   0, ...);
    add_section(elfw, ".symtab_shndx",    SHT_SYMTAB_SHNDX, 0, ...);
    add_section(elfw, ".note.nv.tkinfo",  SHT_NOTE,     0, ...);
    add_section(elfw, ".note.nv.cuinfo",  SHT_NOTE,     0, ...);
    add_section(elfw, ".nv.uft.entry",    SHT_PROGBITS, 0, ...);

    return elfw;
}
```

The ELFW object stores:
- The ELF header (first 64 bytes for 64-bit class, 52 for 32-bit)
- A section table (dynamic array of section descriptors)
- A symbol table (dynamic array of symbol entries)
- String tables for section names (`.shstrtab`) and symbol names (`.strtab`)
- Metadata for relocation processing and section ordering

### ELFW Object Layout (672 bytes)

The 672-byte ELFW object divides into 13 regions. Offsets 0--63 overlay a standard ELF header (whose internal layout depends on ELF class). All pointer-sized fields are 8 bytes (the allocator returns 8-byte-aligned memory). The `v17` variable in the decompilation is a `uint64_t*`, so `v17[N]` = byte offset `N * 8`.

#### Region 1 -- ELF Header Embed (bytes 0--63)

The ELF header is stored inline at the start of the ELFW object. Field positions within it vary by class (32-bit vs 64-bit), matching the standard `Elf32_Ehdr` / `Elf64_Ehdr` layout, except that `EI_CLASS` and `EI_OSABI` use non-standard CUDA values.

| Offset | Size | Name | Evidence |
|---|---|---|---|
| 0 | 4B | `e_ident[EI_MAG0..3]` | `*(_DWORD*)v17 = 0x464C457F` |
| 4 | 1B | `e_ident[EI_CLASS]` | `(v11 != 0) + 1`: 1 = ELFCLASS32, 2 = ELFCLASS64 |
| 5 | 1B | `e_ident[EI_DATA]` | Hardcoded 1 (little-endian) |
| 6 | 1B | `e_ident[EI_VERSION]` | Hardcoded 1 (EV_CURRENT) |
| 7 | 1B | `e_ident[EI_OSABI]` | `0x41` ('A') for 64-bit cubin, `0x33` ('3') for 32-bit |
| 8 | 1B | `e_ident[EI_ABIVERSION]` | Constructor parameter `a3` |
| 16 | 2B | `e_type` | Constructor parameter `a1` (cast to uint16) |
| 18 | 2B | `e_machine` | Hardcoded `0x00BE` (EM_CUDA = 190) |
| 62 | 2B | `e_shstrndx` | `*(_WORD*)(v17 + 31)` -- set to `.shstrtab` section index |

For 32-bit class (`EI_CLASS = 1`):

| Offset | Size | Name | Dumper accessor |
|---|---|---|---|
| 20 | 4B | `e_version` | `*(_DWORD*)(v17 + 5)` |
| 28 | 4B | `e_phoff` | `*(_DWORD*)(a1 + 28)` |
| 32 | 4B | `e_shoff` | `*(_DWORD*)(a1 + 32)` |
| 36 | 4B | `e_flags` | `*(_DWORD*)(a1 + 36)` -- dumper prints `"flags=%x"` |
| 44 | 2B | `e_phnum` | `*(uint16*)(a1 + 44)` -- dumper prints `"phnum"` |
| 48 | 2B | `e_shnum` | `*(uint16*)(a1 + 48)` -- dumper prints `"shnum"` |

For 64-bit class (`EI_CLASS = 2`):

| Offset | Size | Name | Dumper accessor |
|---|---|---|---|
| 32 | 8B | `e_phoff` | `*(_QWORD*)(a1 + 32)` -- dumper prints `"phoff=%llx"` |
| 40 | 8B | `e_shoff` | `*(_QWORD*)(a1 + 40)` -- dumper prints `"shoff=%llx"` |
| 48 | 4B | `e_flags` | `*(_DWORD*)(a1 + 48)` -- dumper prints `"flags=%x"` |
| 56 | 2B | `e_phnum` | `*(uint16*)(a1 + 56)` -- dumper prints `"phnum"` |
| 60 | 2B | `e_shnum` | `*(uint16*)(a1 + 60)` -- dumper prints `"shnum"` |

#### Region 2 -- Metadata and Flags (bytes 64--107)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 64 | 1B | `debugMode` | `a8` parameter: `deviceDebug` |
| 68 | 4B | `compilationFlags` | `rawOptions & 0x70000` -- preserved option bits 16--18 |
| 72 | 4B | `smVersion` | `a4` parameter: SM architecture number (e.g., 100 for Blackwell) |
| 76 | 4B | `rawOptions` | Full options bitmask `a9`, possibly OR'd with `0x80000` for relocatable |
| 80 | 1B | `lineInfoMode` | `a6` parameter: `lineInfo` |
| 82 | 1B | `is32bit` | Dumper gate: controls 32-bit vs 64-bit format in `sub_1CB91C0` |
| 83 | 1B | `hasSymbolRemap` | Set to `*(WORD*)(v17 + 42) != 0` |
| 84 | 1B | `flag_relocatable` | `rawOptions & 1` |
| 85 | 1B | `flag_executable` | `(rawOptions & 2) != 0` |
| 86 | 1B | `flag_PIC` | `(rawOptions & 0x200) != 0` -- position-independent code |
| 87 | 1B | `flag_bit2` | `(rawOptions & 4) != 0` |
| 88 | 1B | `flag_bit3` | `(rawOptions & 8) != 0` |
| 89 | 1B | `flag_relocOrBit4` | `(rawOptions >> 4) & 1`, forced to 1 if relocatable mode |
| 90 | 1B | `flag_bit5` | `(rawOptions & 0x20) != 0` |
| 91 | 1B | `flag_bit14` | `(rawOptions & 0x4000) != 0` |
| 92 | 1B | `flag_bit6` | `(rawOptions & 0x40) != 0` |
| 93 | 1B | `flag_byte1_bit0` | `BYTE1(rawOptions) & 1` -- bit 8 of options |
| 94 | 1B | `flag_archGuard` | `(a5 > 0x45) & (rawOptions >> 7)` -- arch-gated feature |
| 96 | 1B | `flag_bit11` | `(rawOptions & 0x800) != 0` |
| 99 | 1B | `flag_notBit12` | `((rawOptions >> 12) ^ 1) & 1` -- inverted bit 12 |
| 100 | 1B | `flag_bit13` | `(rawOptions & 0x2000) != 0` |
| 101 | 1B | `highClass` | `(a9 & 0x8000) != 0` -- selects 64-bit header variant with wider ELF fields |

#### Region 3 -- Inline String Tables (bytes 108--171)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 108 | 32B | `shstrtab` | Section header string table, initialized via `sub_1CB0530(v17 + 108, 1000)` |
| 140 | 32B | `strtab` | Symbol name string table, initialized via `sub_1CB0530(v17 + 140, 2000)` |

These are 32-byte inline structures (not heap pointers). `sub_1CB0530` initializes them with the given initial capacity (1000 and 2000 bytes respectively). The `.shstrtab` is also referenced by `sub_1CA6650` during `.note.nv.cuinfo` attribute injection.

#### Region 4 -- Section Index Cache (bytes 196--215)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 200 | 2B | `strtabSecIdx` | `.strtab` section index -- `*(_WORD*)(v17 + 101) = v54` |
| 202 | 2B | `symtabSecIdx` | `.symtab` section index -- `*(_WORD*)(v17 + 102) = v56` |
| 204 | 2B | `xindexSecIdx` | `.symtab_shndx` section index -- `*(_WORD*)(v17 + 103)` |
| 206 | 2B | `cuinfoSecIdx` | `.note.nv.cuinfo` section index -- `*(_WORD*)(v17 + 104)` |
| 208 | 2B | `tkinfoSecIdx` | `.note.nv.tkinfo` section index -- `*(_WORD*)(v17 + 105)` |

These cached indices avoid repeated linear scans of the section table when cross-referencing sections (e.g., `.symtab`'s `sh_link` must point to `.strtab`).

#### Region 5 -- Sorted Maps and Counters (bytes 288--327)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 288 | 8B | `sortedMap_A` | Red-black tree via `sub_425CA0`, initial capacity 512 |
| 296 | 8B | `sortedMap_B` | Red-black tree via `sub_425CA0`, initial capacity 512 |
| 304 | 4B | `mapCount` | Counter for sorted maps, cleared to 0 |
| 312 | 8B | `countPair` | Packed `0x100000000` = high DWORD 1, low DWORD 0 |
| 320 | 4B | `activeFlag` | Set to 1 during initialization |

#### Region 6 -- Section and Symbol Containers (bytes 344--419)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 344 | 8B | `sectionList_A` | Indexed vector (cap=64) via `sub_1CD2F90`. Dumper: `sub_1CD3060(*(a1+344))` returns symbol count |
| 352 | 8B | `sectionList_B` | Indexed vector (cap=64). Dumper: `sub_1CD3060(*(a1+352))` returns secondary count |
| 360 | 8B | `sectionList_C` | Indexed vector (cap=64). Dumper iterates `*(a1+360)` for section dump loop |
| 368 | 8B | `secIndexMap` | Virtual-to-real section index map. Dumper: `*(a1+368) + 4*v11` for reverse lookup |
| 376 | 8B | `relocList` | Linked list head for relocations. Dumper: `for (k = *(a1+376); k; k = *k)` |
| 392 | 8B | `nvinfoList` | Linked list head for `.nv.info` entries. Dumper: `for (j = *(a1+392); j; j = *j)` |
| 408 | 8B | `auxVector` | Indexed vector (cap=32) via `sub_1CD2F90` |
| 416 | 4B | `auxCount` | Counter for `auxVector`, cleared to 0 |

`sub_1CD2F90` creates an indexed vector (growable array with count/capacity tracking). `sub_1CD3060` returns the element count; `sub_1CD31F0` returns the element at the current iteration index.

#### Region 7 -- Deletion Remap Tables (bytes 456--479)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 456 | 8B | `symDeleteMap` | Symbol index remap table (positive indices). Dumper: `*(a1+456) + 4*idx` |
| 464 | 8B | `symDeleteMapNeg` | Symbol index remap table (negative indices). Dumper: `*(a1+464) + 4*(-idx)` |
| 472 | 8B | `secDeleteMap` | Section index remap table. Dumper: `*(a1+472) + 4*idx` |

After dead code elimination deletes sections and symbols, these tables map old indices to new indices. The negative-index variant handles symbols stored with inverted sign conventions (a ptxas-internal encoding for unresolved forward references).

#### Region 8 -- Architecture State (bytes 488--495)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 488 | 8B | `archState` | Architecture descriptor pointer. Initialized via `sub_1CD04F0` (relocatable) or `sub_1CCEEE0` (non-relocatable). Fatal error `"couldn't initialize arch state"` on failure |

#### Region 9 -- Name Sets and Input Tracking (bytes 496--519)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 496 | 8B | `sectionNameSet` | Sorted set of well-known section name strings. Populated from static table `off_2403A60` (22 entries ending at `dword_2403B70`) |
| 512 | 8B | `inputFileList` | Indexed vector (cap=8). First entry is a 16-byte descriptor: `{ptr="<input>", arch_version, ...}` |

#### Region 10 -- Hash Maps (bytes 520--567)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 520 | 8B | `hashMap_A` | Hash map via `sub_42D150`, initial capacity 16 |
| 528 | 8B | `hashMap_B` | Hash map (cap=16) |
| 536 | 8B | `hashMap_C` | Hash map (cap=16) |
| 544 | 8B | `hashMap_D` | Hash map (cap=16) |
| 552 | 8B | `hashMap_E` | Hash map (cap=16) |
| 560 | 8B | `hashMap_F` | Hash map (cap=16) |

Six hash maps initialized identically with `sub_42D150(sub_427630, sub_4277B0, 0x10)`. The two function pointers are the hash function and equality comparator. These maps serve section/symbol lookups during the construction pipeline. The specific role of each map (by-name, by-type, etc.) requires tracing callers of the hash map accessors.

#### Region 11 -- Extended Index and Miscellaneous (bytes 576--607)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 576 | 8B | `smallSortedSet` | Sorted set via `sub_425CA0`, element size 8 |
| 592 | 8B | `symtabShndxVec` | `.symtab_shndx` data vector. Dumper: `sub_1CD31F0(*(a1+592))` for SHN_XINDEX resolution |
| 600 | 8B | `mercSymtabShndx` | `.nv.merc.symtab_shndx` data vector. Dumper: `*(a1+600)` for Mercury SHN_XINDEX |

#### Region 12 -- Memory Pool and Tail (bytes 608--671)

| Offset | Size | Name | Purpose |
|---|---|---|---|
| 608 | 8B | `memoryPool` | `"elfw memory space"` pool pointer. Only set when `(a9 & 0x400) != 0` |
| 616 | 8B | `memoryPoolCursor` | Pool allocation cursor |
| 624 | 4B | `elfFormatVersion` | `sub_1C97990()` return value -- ELF format version from global config |
| 664 | 8B | `tailSentinel` | `v17[83] = 0` -- zeroed during init, marks end of object |

#### Visual Layout

```text
 ELFW Object (672 bytes = 0x2A0)
 +---------+------+--------------------------------------------------+
 |  0x000  | 64B  | ELF Header Embed (Elf32_Ehdr or Elf64_Ehdr)     |
 +---------+------+--------------------------------------------------+
 |  0x040  | 44B  | Metadata + Option Flags (debugMode, smVersion,   |
 |         |      |   rawOptions, 16 boolean flags)                  |
 +---------+------+--------------------------------------------------+
 |  0x06C  | 64B  | Inline String Tables                             |
 |         |      |   +0x06C: shstrtab (32B, cap=1000)               |
 |         |      |   +0x08C: strtab  (32B, cap=2000)                |
 +---------+------+--------------------------------------------------+
 |  0x0AC  | 36B  | Section Index Cache + Padding                    |
 |         |      |   5 x uint16 section indices (.strtab, .symtab,  |
 |         |      |   .symtab_shndx, .cuinfo, .tkinfo)               |
 +---------+------+--------------------------------------------------+
 |  0x120  | 40B  | Sorted Maps + Counters                           |
 +---------+------+--------------------------------------------------+
 |  0x158  | 76B  | Section/Symbol Containers                        |
 |         |      |   3 indexed vectors (sections), index map,        |
 |         |      |   relocation list, nvinfo list, aux vector        |
 +---------+------+--------------------------------------------------+
 |  0x1C8  | 24B  | Deletion Remap Tables (sym+, sym-, sec)          |
 +---------+------+--------------------------------------------------+
 |  0x1E8  |  8B  | Architecture State Pointer                       |
 +---------+------+--------------------------------------------------+
 |  0x1F0  | 24B  | Name Sets + Input Tracking                       |
 +---------+------+--------------------------------------------------+
 |  0x208  | 48B  | Six Hash Maps (16-entry initial)                 |
 +---------+------+--------------------------------------------------+
 |  0x240  | 32B  | Extended Index Vectors + Small Sorted Set         |
 +---------+------+--------------------------------------------------+
 |  0x260  | 64B  | Memory Pool + Format Version + Tail              |
 +---------+------+--------------------------------------------------+
```

### ELF Class Selection

The ELF class byte at offset 4 determines 32-bit vs 64-bit output format. ptxas uses non-standard values:

| EI_CLASS byte | Standard ELF | ptxas meaning |
|---|---|---|
| `'3'` (0x33) | n/a | 32-bit CUDA ELF |
| `'A'` (0x41) | n/a | 64-bit CUDA ELF |

Standard ELF uses 1 (ELFCLASS32) and 2 (ELFCLASS64). The non-standard values `'3'` and `'A'` are a CUDA-specific convention that identifies the binary as a cubin rather than a generic ELF. The CUDA driver recognizes these values during cubin loading.

## Section Creator -- `sub_1CB3570`

The generic section creation function, called from 44 sites throughout the ELF construction pipeline. It accepts the full set of ELF section header parameters and optionally creates a companion relocation section.

```c
// sub_1CB3570 -- add section to ELFW (simplified)
int add_section(void* elfw, const char* name, uint32_t type, uint64_t flags,
                uint32_t link, uint32_t info, uint64_t align, uint64_t entsize) {
    // 1. Add name to .shstrtab, get string table offset
    int name_idx = strtab_add(elfw->shstrtab, name);

    // 2. Allocate section descriptor, fill ELF section header fields
    section_t* sec = alloc_section(elfw);
    sec->sh_name    = name_idx;
    sec->sh_type    = type;
    sec->sh_flags   = flags;
    sec->sh_link    = link;
    sec->sh_info    = info;
    sec->sh_addralign = align;
    sec->sh_entsize = entsize;

    // 3. For executable sections, auto-create relocation companion
    if (flags & SHF_EXECINSTR) {
        char rela_name[256];
        snprintf(rela_name, sizeof(rela_name), ".rela%s", name);
        // -- or ".rel%s" depending on ELF class --
        section_t* rela = alloc_section(elfw);
        rela->sh_type = SHT_RELA;  // or SHT_REL
        rela->sh_link = symtab_index;
        rela->sh_info = sec->index;
    }

    return sec->index;
}
```

The assertion `"adding function section after callgraph completed"` fires if a section is added after the call graph analysis phase has already run. This enforces the ordering constraint: all `.text.<funcname>` sections must exist before dead code elimination and call graph construction begin.

## Text Section Creator -- `sub_1CB42D0`

Creates a per-function code section with the naming convention `.text.<funcname>`:

| Field | Value |
|---|---|
| `sh_type` | `SHT_PROGBITS` (1) |
| `sh_flags` | `SHF_ALLOC \| SHF_EXECINSTR` (0x6) |
| Section name | `.text.<funcname>` |
| Companion | `.rela.text.<funcname>` (auto-created) |

Each kernel entry point and each device function gets its own `.text` section. This per-function section layout enables the linker (`nvlink`) to perform function-level dead code elimination and allows the CUDA driver to load individual kernels.

## Symbol Table Builder -- `sub_1CB68D0`

The largest function in the ELFW subsystem at 9,578 bytes (approximately 1,700 decompiled lines). It constructs the `.symtab` section from the internal symbol representation, handling several CUDA-specific concerns.

### Processing Steps

1. **Iterate internal symbol list** -- walks the ELFW symbol container
2. **Filter deleted symbols** -- skips entries marked deleted, emits `"reference to deleted symbol"` warning (12 occurrences of this check in the function)
3. **Handle `__cuda_syscall`** -- special-cases the CUDA syscall dispatcher symbol, which serves as the entry point for device-side system calls (vprintf, malloc, `__assertfail`, etc.)
4. **Compute symbol values/sizes** -- resolves virtual addresses from section offsets
5. **Create section symbols** -- ensures every section has a corresponding `STT_SECTION` symbol
6. **Handle SHN_XINDEX overflow** -- when the section index exceeds `SHN_LORESERVE` (0xFF00 = 65,280), the symbol's `st_shndx` field is set to `SHN_XINDEX` (0xFFFF) and the real index is stored in the `.symtab_shndx` table
7. **Build `.symtab_shndx`** -- populates the extended section index table for overflow cases

### Error Messages

| String | Condition |
|---|---|
| `"reference to deleted symbol"` | Symbol was deleted but still referenced (12 checks) |
| `"ignore symbol %s in unused section"` | Symbol in dead-eliminated section |
| `"missing sec strtab"` | String table not initialized |
| `"missing std sections"` | Standard sections (.shstrtab, .strtab, .symtab) missing |
| `"overflow number of sections %d"` | Section count exceeds ELF limits |

### CUDA Syscall Functions

The `__cuda_syscall` symbol is the dispatcher for device-side system calls. The known syscall functions referenced throughout the ptxas binary:

| Syscall | Purpose |
|---|---|
| `vprintf` | Device-side formatted output |
| `malloc` | Device-side dynamic memory allocation |
| `free` | Device-side memory deallocation |
| `__assertfail` | Device assertion failure handler |
| `__profile` | Profiling counter increment |
| `cnpGetParameterBuffer` | Cooperative launch parameter access |

These are compiled as indirect calls through the `__cuda_syscall` dispatch mechanism. The symbol `__cuda_syscall_32f3056bbb` (observed in binary strings) is a hash-mangled variant used for linking.

## Section Layout Calculator -- `sub_1C9DC60`

Computes file offsets and virtual addresses for all sections in the ELF. This is a multi-pass algorithm that respects alignment constraints and handles several special cases.

### Special Section Handling

| Section | Treatment |
|---|---|
| `.nv.constant0` | Skipped (handled separately by OCG constant bank allocation) |
| `.nv.reservedSmem` | Skipped (shared memory layout computed by master allocator `sub_1CABD60`) |
| `.debug_line` | Receives special alignment padding for DWARF line table requirements |

The layout calculator assigns offsets in section-table order, which itself is determined by the 8-bucket priority sort performed during finalization.

## ELF Finalization -- `sub_1C9F280`

The master ELF emitter at 15,263 binary bytes (97 KB decompiled) is the single largest function in the post-codegen address range. It assembles the complete ELF output from the ELFW internal representation.

### Execution Flow

1. **Copy ELF header** -- 64 bytes transferred via SSE `loadu` (128-bit unaligned loads) for performance
2. **Iterate sections** -- uses accessor pair `sub_1CB9FF0` (section count) / `sub_1CB9C40` (get section by index)
3. **Skip virtual sections** -- sections with `flag & 4` set have no file data (metadata-only)
4. **Filter `.nv.constant0`** -- detected via `strstr(".nv.constant0")`, handled by separate constant bank logic
5. **Copy section headers** -- SIMD-width stride memcpy of section header entries
6. **Patch ELF flags** -- mask `0x7FFFBFFF` clears CUDA-specific flag bits, then sets SM version and relocatable/executable mode
7. **Emit program headers** -- creates PT_LOAD segments for loadable sections
8. **Build symbol table** -- delegates to `sub_1CB68D0`
9. **Resolve section indices** -- handles cross-references between sections
10. **Embed Mercury capsule** -- if capmerc mode, embeds the `.nv.merc.*` sections
11. **Process debug sections** -- maps `.debug_info`, `.debug_line`, `.debug_frame` sections
12. **Error recovery** -- uses `_setjmp` for non-local error exit on fatal corruption

### Section Ordering -- 8 Priority Buckets

During finalization, sections are sorted into 8 priority buckets that determine their order in the output ELF. The bucket assignment ensures the correct layout for the CUDA driver's section scanner. Lookup proceeds against the well-known section name set seeded from the static table at `off_2403A60` (22 entries, terminator `dword_2403B70`); names that do not match a well-known entry are bucketed by section kind (`sh_type`, `sh_flags`, and the NVIDIA `SHT_LOPROC` type code).

| Bucket | Contents (full enumeration) |
|---|---|
| 0 (highest) | ELF header pseudo-section, `.shstrtab` |
| 1 | `.strtab`, `.symtab`, `.symtab_shndx`, `.nv.merc.symtab_shndx` |
| 2 | `.note.nv.tkinfo`, `.note.nv.cuinfo`, `.note.nv.cuver` |
| 3 | `.text.<funcname>` (code sections, `SHF_ALLOC \| SHF_EXECINSTR`) |
| 4 | Data / NVIDIA-info-but-not-EIATTR: `.nv.constant0.<func>`, `.nv.constant{1..17}`, `.nv.constant.{entry_params, driver, optimizer, user, pic, tools_data, entry_image_header_indices}`, `.nv.shared.<func>`, `.nv.local.<func>`, `.nv.global`, `.nv.global.init`, `.nv.reservedSmem`, `.nv.callgraph` (`SHT_CUDA_CALLGRAPH`), `.nv.prototype`, `.nv.compat` (type `0x70000086`), `.nv.uft`, `.nv.uft.entry` |
| 5 | Relocations: `.rela.*`, `.rel.*`, `.nv.rel.action`, `.nv.uft.rel`, `.nv.merc.rela` |
| 6 | EIATTR: `.nv.info`, `.nv.info.<funcname>` (`SHT_CUDA_INFO`, type `0x70000000`) |
| 7 (lowest) | Debug / Mercury: `.debug_info`, `.debug_line`, `.debug_frame`, `.debug_abbrev`, `.debug_aranges`, `.debug_loc`, `.debug_macinfo`, `.debug_pubnames`, `.debug_pubtypes`, `.debug_ranges`, `.debug_str`, `.nv.merc.debug_*`, `.nv.merc.nv_debug_line_sass`, `.nv.merc.nv_debug_info_reg_sass`, `.nv.merc.nv_debug_info_reg_type`, `.nv.merc.nv_debug_ptx_txt`, `.nv.merc.nv.shared.reserved.<func>`, generic `.nv.merc.*` |

Three section names in bucket 4 are sort-ordered alongside data sections but **skipped by the offset-assignment walk** in `sub_1C9DC60`: `.nv.constant0.<func>` (file offset assigned by the OCG constant-bank allocator) and `.nv.reservedSmem` (offset assigned by the shared-memory master allocator `sub_1CABD60`). Virtual sections (`flags & 4`) are likewise skipped -- they carry only metadata.

### Offset Assignment and Alignment

Each section's file offset is aligned to its `sh_addralign` value. The algorithm walks the sorted section list, advancing a running offset counter with alignment padding:

```c
uint64_t offset = elf_header_size;
for (int i = 0; i < section_count; i++) {
    section_t* sec = sorted_sections[i];
    if (sec->flags & 4)               continue;   // virtual section -- no file data
    if (is_nv_constant0(sec))         continue;   // OCG constant-bank allocator assigns sh_offset
    if (is_nv_reservedSmem(sec))      continue;   // sub_1CABD60 shared-memory allocator assigns sh_offset
    if (sec->sh_addralign > 1)
        offset = (offset + sec->sh_addralign - 1) & ~(sec->sh_addralign - 1);
    sec->sh_offset = offset;
    offset += sec->sh_size;
}
```

### Extended Section Index Handling (SHN_XINDEX)

When the total section count exceeds 65,280 (`SHN_LORESERVE` = 0xFF00), standard ELF `e_shnum` cannot hold the value. The emitter activates the SHN_XINDEX mechanism:

1. Sets `e_shnum = 0` in the ELF header
2. Stores the real section count in `sh_size` of section header index 0 (the null section)
3. Sets `e_shstrndx = SHN_XINDEX` (0xFFFF)
4. Stores the real `.shstrtab` index in `sh_link` of section header index 0

This is the standard ELF extension for large section counts, and it is necessary for large CUDA programs with many kernels (each kernel generates at minimum a `.text`, `.rela.text`, `.nv.info`, and `.nv.constant0` section).

## Cubin Generation Entry -- `sub_612DE0`

The top-level ELF/cubin generation function at 47 KB. Called from the compilation driver `sub_446240` after all per-kernel OCG passes complete. This function orchestrates the entire output pipeline.

### Key Behaviors

- **Option parsing** -- reads compilation flags: `deviceDebug`, `lineInfo`, `optLevel`, `IsCompute`, `IsPIC`
- **Fastpath optimization** -- `"Finalizer fastpath optimization"` string indicates a fast path for cross-target finalization when no complex linking is needed
- **Version embedding** -- writes `"Cuda compilation tools, release 13.0, V13.0.88"` and build ID `"Build cuda_13.0.r13.0/compiler.36424714_0"` into the cubin
- **Error recovery** -- establishes its own `setjmp`/`longjmp` frame independent of the top-level driver's
- **Recursive self-call** -- handles nested finalization for scenarios where the output pipeline must invoke itself (e.g., generating both a primary cubin and an embedded Mercury cubin simultaneously)

## Symbol Fixup -- `sub_1CB2CA0`

Adjusts symbol indices after dead code elimination removes sections from the ELFW. When sections are deleted, all symbol references to those sections become stale and must be renumbered.

```text
For each section in ELFW:
  If section lacks a STT_SECTION symbol:
    Create one
  If section has multiple STT_SECTION symbols:
    Emit "found multiple section symbols for %s"
  Renumber all symbol st_shndx values to match new section indices
```

Called from 4 sites, indicating it runs at multiple points during the output pipeline (after dead function elimination, after mercury section cloning, etc.).

## Section Index Remap -- `sub_1C99BB0`

Handles the `.symtab_shndx` and `.nv.merc.symtab_shndx` extended index tables when section indices change. This is the companion to `sub_1CB2CA0`: while that function fixes symbol `st_shndx` fields, this one fixes the extended index tables that hold the real indices when SHN_XINDEX is in use.

## ELF Structure Dumper -- `sub_1CB91C0`

Debug-mode function that prints a formatted dump of the ELFW internal state. Triggered by internal debug flags, not by any user-visible CLI option.

Output format:

```text
elfw structure:
header: size=%d type=%d abiv=%d, flags=%x
  shnum=%d shoff=%d
  phnum=%d phoff=%d

section <v/r>:
  [idx] name  type  flags  offset  size  link  info  align  entsize

symbol <v/r>:
  [idx] name  value  size  bind  type  shndx
```

The `<v/r>` suffix indicates virtual (`v`) or real (`r`) mode, corresponding to whether the dump shows the in-memory intermediate state or the final file-ready values. Both 32-bit and 64-bit format strings are present.

## File Serializer -- `sub_1CD13A0`

The final step: writes the assembled ELF binary to disk. Called from 2 sites (main cubin and Mercury capsule cubin).

### Validation Checks

| Check | Error String |
|---|---|
| Section data size negative | `"Negative size encountered"` |
| Computed size != declared size | `"section size mismatch"` |
| Write failure | `"writing file"` (logged 12 times across write operations) |

The serializer handles both 32-bit and 64-bit ELF formats, adjusting section header entry sizes (40 bytes for 32-bit, 64 bytes for 64-bit) and symbol table entry sizes (16 bytes for 32-bit, 24 bytes for 64-bit).

## ELF Header Layout

The ELF header written by the ELFW constructor follows the standard ELF format with CUDA-specific overrides:

| Offset | Size | Field | Value |
|---|---|---|---|
| 0x00 | 4B | `e_ident[EI_MAG0..3]` | `0x7F 'E' 'L' 'F'` (magic `0x464C457F`) |
| 0x04 | 1B | `e_ident[EI_CLASS]` | `0x33` ('3', 32-bit) or `0x41` ('A', 64-bit) |
| 0x05 | 1B | `e_ident[EI_DATA]` | `0x01` (little-endian) |
| 0x06 | 1B | `e_ident[EI_VERSION]` | `0x01` (EV_CURRENT) |
| 0x07 | 1B | `e_ident[EI_OSABI]` | CUDA ABI version |
| 0x12 | 2B | `e_machine` | `0x00BE` (EM_CUDA = 190) |
| 0x14 | 4B | `e_version` | `0x00000001` |
| 0x24 | 4B | `e_flags` | SM version + CUDA flags (masked via `0x7FFFBFFF`) |

The `e_flags` field encodes the target SM architecture (e.g., `sm_100` for Blackwell) and several CUDA-specific flags including relocatable object mode vs executable mode. The mask `0x7FFFBFFF` clears bits 14 and 31, which are reserved CUDA control bits.

## NVIDIA-Specific Section Types

Beyond standard ELF section types, the emitter uses NVIDIA-defined types in the `SHT_LOPROC`--`SHT_HIPROC` range. See [Section Catalog](sections.md#nvidia-specific-section-types) for the authoritative table; the constants below are repeated here for the emitter context:

| Type Constant | Value | Section |
|---|---|---|
| `SHT_CUDA_INFO` | `0x70000000` | `.nv.info`, `.nv.info.*` -- global and per-entry EIATTR attributes |
| `SHT_CUDA_CALLGRAPH` | `0x70000064` | `.nv.callgraph` -- inter-function call edges (relocatable mode) |
| `SHT_CUDA_COMPAT` | `0x70000086` | `.nv.compat` -- forward-compatibility attributes |

The magic constant `1879048292` (`0x70000064`) appears in the emitter decompilation as a range-check endpoint for CUDA-specific section types -- `sub_1CB3570` treats the range `0x70000064`--`0x7000007E` plus `0x70000006` as receiving special relocatable-mode handling. The `.nv.info` section creator `sub_1CC7FB0` passes `1879048192` (`0x70000000`) for the type field.

## Cross-References

- [Section Catalog & EIATTR](sections.md) -- complete inventory of section types and EIATTR attributes
- [Relocations & Symbols](relocations.md) -- relocation resolution and UFT/UDT management
- [Debug Information](debug-info.md) -- DWARF generation and `.debug_*` section handling
- [Mercury Encoder](../codegen/mercury.md) -- Mercury/capmerc encoding that feeds into the ELF emitter
- [Pipeline Overview](../pipeline/overview.md) -- where the ELF phase fits in the compilation pipeline
- [Memory Pool Allocator](../infra/memory-pools.md) -- the `sub_424070` pool allocator used by ELFW

## Function Map

| Address | Size (native) | Size (decomp) | Callers | Callees | Purpose |
|---|---|---|---|---|---|
| `sub_1CB53A0` | 3,480 B | 13 KB | 1 | 25 | ELFW constructor (672-byte object) |
| `sub_1CB3570` | 1,963 B | 10 KB | 44 | 13 | Section creator (.rela/.rel auto-create) |
| `sub_1CB42D0` | -- | -- | -- | -- | .text.\<funcname\> section creator |
| `sub_1CB68D0` | 9,578 B | 49 KB | 1 | 36 | Symbol table builder (.symtab) |
| `sub_1CB2CA0` | 2,038 B | 8 KB | 4 | 11 | Symbol fixup (post-deletion renumbering) |
| `sub_1C99BB0` | 4,900 B | 25 KB | 1 | 18 | Section index remap (.symtab_shndx) |
| `sub_1C9DC60` | 5,663 B | 29 KB | 1 | 24 | Section layout calculator |
| `sub_1C9F280` | 15,263 B | 97 KB | 1 | 42 | Master ELF emitter (assembles complete output) |
| `sub_1CB91C0` | 2,668 B | 13 KB | 1 | 5 | ELF structure dumper (debug) |
| `sub_1CD13A0` | 2,541 B | 11 KB | 2 | 6 | File serializer (final write to disk) |
| `sub_612DE0` | ~12,000 B | 47 KB | 1 | -- | Cubin generation entry point |
| `sub_1CB9FF0` | -- | -- | -- | -- | Section count accessor |
| `sub_1CB9C40` | -- | -- | -- | -- | Get section by index |
