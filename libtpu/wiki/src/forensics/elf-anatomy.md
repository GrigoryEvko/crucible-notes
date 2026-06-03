# ELF Anatomy

> *All offsets, virtual addresses, and counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (plugin version 0.103, build-id `89edbbe81c5b328a958fe628a9f2207d`). Other builds will differ in every address.*

## Abstract

`libtpu.so` is a 745 MiB (`781,691,048`-byte) position-independent ELF64 shared object — `ET_DYN`, `EM_X86_64`, little-endian, System V ABI. It is the entire Google TPU PJRT plugin statically linked into one file: the XLA compiler, the MLIR/LLVM backend, the TPU runtime, gRPC, protobuf, Abseil, Eigen, and the device driver are all here. It carries no `DT_SONAME`, no `RPATH`/`RUNPATH`, no separate debug objects, and depends on only six bare system libraries. Despite its size it is a thoroughly ordinary clang/LLVM-built `.so`, just scaled up by three orders of magnitude.

The defining structural fact is that this object was built with the **x86-64 large code model**. That model splits read-only and BSS data into two tiers: the ordinary small-model `.rodata`/`.bss` reachable with 32-bit `%rip`-relative displacements, and the large-model `.lrodata`/`.lbss`/`.ldata` (the `l` flag, `SHF_X86_64_LARGE`) reached through 64-bit absolute addressing because their distance from `.text` exceeds ±2 GiB. `.lrodata` alone is 108 MiB. This is why the singleton tables that the runtime keys off — the PJRT API vtable among them — live in `.lbss` near vaddr `0x227ba840` rather than in `.bss`. A reimplementer who assumes a single `.rodata`/`.bss` pair will mis-map a third of the constant data.

This page is the authoritative section/segment/dynamic map of the binary. Every address cited anywhere else in this wiki sits inside one of the regions tabulated below; this page establishes which section owns which vaddr range, which `PT_LOAD` carries which permissions, and what the dynamic linker is told at load time. The deep dives — who the `.init_array` constructors are, and what the unusual `google_*`/`__rseq_cs`/`linkarr_*` sections hold — belong to the sibling pages noted at each table.

For reimplementation, the contract is:

- **The four-segment load image** — one `R E` text/rodata segment followed by three `RW` data segments, and the page-shift rule that breaks file-offset ≡ vaddr after the first segment.
- **The large-code-model section split** — `.rodata`/`.bss`/`.data` (small) versus `.lrodata`/`.lbss`/`.ldata` (large, `l`-flagged), and why the runtime's singletons land in the large tier.
- **The dynamic contract** — six `DT_NEEDED`, GNU-hash-only lookup, `.rela.dyn`/`.rela.plt` split with a million-plus `R_X86_64_RELATIVE` relocations, and the init/fini/preinit array anchors.

| | |
|---|---|
| **File size** | `781,691,048` bytes (~745 MiB) |
| **Class / data / type** | ELF64 · LSB (2's complement, little-endian) · `ET_DYN` |
| **Machine / OS-ABI** | `EM_X86_64` · UNIX System V (ABI v0) |
| **Entry point** | `0x0` (none — pure shared library) |
| **Program headers** | 11 entries, 56 bytes each, at file offset `64` |
| **Section headers** | 52 entries, 64 bytes each, at file offset `0x2e979ba8` |
| **`.shstrtab` index** | section `[50]` |
| **Build-id (NT_GNU_BUILD_ID)** | `89edbbe81c5b328a958fe628a9f2207d` (16 bytes) |
| **`DT_SONAME`** | none |
| **Code model** | x86-64 large (`.lrodata`/`.lbss`/`.ldata` present) |
| **`.text` extent** | `0xe63c000` … `0x21217484`, `0x12bdb484` bytes (~299 MiB) |

---

## ELF Header

The header is `readelf -h` verbatim. Every downstream address on this page and elsewhere in the wiki is a virtual address in the address space this header describes.

```text
Class:                             ELF64
Data:                              2's complement, little endian
Type:                              DYN (Shared object file)
Machine:                           Advanced Micro Devices X86-64
Entry point address:               0x0
Start of program headers:          64 (bytes into file)
Start of section headers:          781687720 (bytes into file)   # 0x2e979ba8
Size of program headers:           56  ·  Number of program headers:   11
Size of section headers:           64  ·  Number of section headers:   52
Section header string table index: 50
```

Three header facts shape everything below.

- **`Entry point = 0x0`.** This is a library, not an executable; there is no `_start`. Execution enters through the `DT_INIT`/`DT_INIT_ARRAY` machinery (the loader-driven constructor chain) and through exported entry symbols like `GetPjrtApi`. The entry-into-init mechanics are owned by [`lifecycle/elf-entry-and-init-proc.md`](../lifecycle/elf-entry-and-init-proc.md).
- **The section header table sits at the very end of the file** (offset `0x2e979ba8`, ~745 MiB in), after the colossal `.symtab`/`.strtab`. The section headers, the symbol table, and the string tables are *not* part of any `PT_LOAD`; they exist on disk but are never mapped at runtime.
- **52 sections including the `NULL` section `[0]`.** Tools that enumerate "real" sections report 51; both counts describe the same table.

> **NOTE —** the binary is `not_stripped` in the `.symtab` sense: a full `.symtab` of `1,232,970` entries plus an `0xab824de`-byte (~172 MiB) `.strtab` is present on disk. These give every internal function a name for static analysis, but they are non-`SHF_ALLOC` and contribute nothing to the runtime image. The runtime sees only the 741-entry `.dynsym`.

| | | Confidence |
|---|---|---|
| ELF class / endianness / type | ELF64 · LSB · `ET_DYN` | High |
| Machine | `EM_X86_64` (`Advanced Micro Devices X86-64`) | High |
| Entry point | `0x0` (no `_start`) | High |
| PHDR count / size | 11 × 56 bytes at offset `0x40` | High |
| SHDR count / size | 52 × 64 bytes at offset `0x2e979ba8` | High |
| `.shstrtab` index | `[50]` | High |

---

## Segment Table (Program Headers)

The runtime image is **four `PT_LOAD` segments**: one read-execute, three read-write. The remaining seven program headers are metadata views into the same bytes (`PHDR`, `TLS`, `DYNAMIC`, `GNU_RELRO`, `GNU_EH_FRAME`, `GNU_STACK`, `NOTE`). All alignments are 2 MiB (`0x200000`) — the build targets huge pages.

```text
Type           Offset     VirtAddr     FileSiz    MemSiz     Flg  Align
PHDR           0x000040   0x00000040   0x000268   0x000268   R    0x8
LOAD  [1]      0x000000   0x00000000   0x213f25d0 0x213f25d0 R E  0x200000
LOAD  [2]      0x213f25e0 0x215f25e0   0xa62bc0   0xa63a20   RW   0x200000
LOAD  [3]      0x21e551c0 0x222551c0   0x26e6a0   0x343a70   RW   0x200000
LOAD  [4]      0x22198c30 0x22798c30   0x021c00   0x0c6650   RW   0x200000
TLS            0x213f25e0 0x215f25e0   0x000110   0x000e78   R    0x20
DYNAMIC        0x21e48b40 0x22048b40   0x000210   0x000210   RW   0x8
GNU_RELRO      0x213f25e0 0x215f25e0   0xa62bc0   0xa63a20   R    0x1
GNU_EH_FRAME   0x0c2cc634 0x0c2cc634   0x6bd684   0x6bd684   R    0x4
GNU_STACK      0x000000   0x00000000   0x000000   0x000000   RW   0x0
NOTE           0x0002a8   0x000002a8   0x000020   0x000020   R    0x4
```

### The four load segments

| Seg | Perms | File offset | Vaddr | FileSiz | MemSiz | Holds | Confidence |
|---|---|---|---|---|---|---|---|
| LOAD 1 | `R E` | `0x0` | `0x0` | `0x213f25d0` (~534 MiB) | same | metadata, `.lrodata`, `.rodata`, EH tables, **all `.text*`**, `.plt` | High |
| LOAD 2 | `RW` | `0x213f25e0` | `0x215f25e0` | `0xa62bc0` (~10.4 MiB) | `0xa63a20` | TLS template, init/fini/preinit arrays, `.data.rel.ro`, `.dynamic`, `.got` | High |
| LOAD 3 | `RW` | `0x21e551c0` | `0x222551c0` | `0x26e6a0` (~2.5 MiB) | `0x343a70` | `.data`, the `google_*`/`__rseq_cs`/`linkarr_*` sections, `.got.plt`, `.bss` | High |
| LOAD 4 | `RW` | `0x22198c30` | `0x22798c30` | `0x021c00` | `0x0c6650` (~806 KiB) | `.ldata`, **`.lbss`**, `google_malloc_bss` (large-model writable) | High |

> **GOTCHA —** the file-offset ≡ vaddr identity holds **only for LOAD 1**, where both start at `0`. From LOAD 2 onward the linker page-shifts the vaddr ahead of the file offset by a growing 2 MiB step: vaddr − offset is `0x200000` for LOAD 2, `0x400000` for LOAD 3, `0x600000` for LOAD 4. A tool that computes `file_offset = vaddr` (correct inside `.text`) will read the wrong bytes for anything in the writable segments — including `.dynamic`, `.got`, and the `.lbss` singletons. Always translate through the owning `PT_LOAD`, never with a single global delta.

The three writable segments are split rather than merged because the large-code-model writable data (`.ldata`/`.lbss`) must stay in its own region: LOAD 4 carries exactly the `l`-flagged writable sections, kept apart from the small-model `.data`/`.bss` of LOAD 3.

### The non-LOAD program headers

- **`GNU_RELRO`** covers the entire LOAD-2 byte range (`0x215f25e0`, `0xa62bc0` bytes). After the dynamic linker applies relocations, this whole 10.4 MiB span — init arrays, `.data.rel.ro`, `.dynamic`, and `.got` — is re-mapped read-only. The `.got.plt` deliberately sits in LOAD 3, *outside* RELRO, so lazy PLT binding can still write resolved addresses.
- **`TLS`** points at the LOAD-2 head: `filesz 0x110` is the `.tdata` initialization image, `memsz 0xe78` adds `.tbss`. The thread-local block is tiny (3704 bytes) for a binary this size.
- **`DYNAMIC`** is the 528-byte (`0x210`) `.dynamic` array, also addressable as section `[34]`.
- **`GNU_EH_FRAME`** is the `.eh_frame_hdr` binary-search index at vaddr `0xc2cc634`, fronting the 30 MiB `.eh_frame`.
- **`GNU_STACK`** has zero size and `RW` (no `X`): non-executable stack, the modern default.
- **`NOTE`** is the 32-byte build-id note at `0x2a8`.

> **QUIRK —** the read-execute LOAD 1 spans `0x0`…`0x213f25d0` (~534 MiB) and physically contains far more constant data than code: `.lrodata` (108 MiB) and `.rodata` (58 MiB) and the EH tables (~38 MiB unwind) all live inside the executable segment because they are read-only, not because they are executable. `.text` is the largest tenant (~299 MiB) but the segment is not "the code segment" in the small-binary sense — it is "everything immutable".

---

## Section Table

52 section headers. The table below groups them by role and gives the address, file offset, size, and flags `readelf -S` reports. Flag key: `A` alloc, `X` execute, `W` write, `M` mergeable, `S` strings, `T` TLS, `I` info-link, `o` OS-processing, `l` `SHF_X86_64_LARGE`.

### Dynamic-linking metadata (LOAD 1 head)

| `[Nr]` Name | Type | Address | Offset | Size | Flg | Confidence |
|---|---|---|---|---|---|---|
| `[1] .note.gnu.build-id` | NOTE | `0x2a8` | `0x2a8` | `0x20` | `A` | High |
| `[2] .dynsym` | DYNSYM | `0x2c8` | `0x2c8` | `0x4578` | `A` | High |
| `[3] .gnu.version` | VERSYM | `0x4840` | `0x4840` | `0x5ca` | `A` | High |
| `[4] .gnu.version_d` | VERDEF | `0x4e0c` | `0x4e0c` | `0x38` | `A` | High |
| `[5] .gnu.version_r` | VERNEED | `0x4e44` | `0x4e44` | `0x270` | `A` | High |
| `[6] .gnu.hash` | GNU_HASH | `0x50b8` | `0x50b8` | `0x678` | `A` | High |
| `[7] .dynstr` | STRTAB | `0x5730` | `0x5730` | `0x3a3c` | `A` | High |
| `[8] .rela.dyn` | RELA | `0x9170` | `0x9170` | `0x1878c30` | `A` | High |
| `[9] .rela.plt` | RELA | `0x1881da0` | `0x1881da0` | `0x2c58` | `AI` | High |

### Read-only data (LOAD 1 body)

| `[Nr]` Name | Type | Address | Size | Flg | Holds | Confidence |
|---|---|---|---|---|---|---|
| `[10] .lrodata` | PROGBITS | `0x1884a00` | `0x6c0e7d0` (~108 MiB) | `AMSl` | large-model RO constants & merged strings | High |
| `[11] .rodata` | PROGBITS | `0x84a0000` | `0x39eaf28` (~58 MiB) | `AMSo` | small-model RO constants, 64 KiB-aligned | High |
| `[12] protodesc_cold` | PROGBITS | `0xbe8af30` | `0x334180` (~3.2 MiB) | `A` | cold protobuf descriptor tables | High |
| `[13] .gcc_except_table` | PROGBITS | `0xc1bf0b0` | `0x10d584` (~1.0 MiB) | `A` | LSDA exception tables | High |
| `[14] .eh_frame_hdr` | PROGBITS | `0xc2cc634` | `0x6bd684` (~6.7 MiB) | `A` | unwind search index | High |
| `[15] .eh_frame` | PROGBITS | `0xc989cb8` | `0x1cab86c` (~28.7 MiB) | `A` | DWARF CFI unwind records | High |

### Executable code (LOAD 1 tail)

| `[Nr]` Name | Type | Address | Size | Flg | Role | Confidence |
|---|---|---|---|---|---|---|
| `[16] .init` | PROGBITS | `0xe635524` | `0x17` | `AX` | `DT_INIT` legacy ctor stub | High |
| `[17] .fini` | PROGBITS | `0xe63553c` | `0x9` | `AX` | `DT_FINI` legacy dtor stub | High |
| `[18] .text.hot` | PROGBITS | `0xe635560` | `0x1e2e` | `AXo` | profile-hot functions | High |
| `[19] google_malloc` | PROGBITS | `0xe6373c0` | `0x46f2` | `AXo` | TCMalloc fast-path code | High |
| `[20] .text.split` | PROGBITS | `0xe63bab2` | `0x0` | `AXo` | empty placeholder section | High |
| `[21] .text` | PROGBITS | `0xe63c000` | `0x12bdb484` (~299 MiB) | `AX` | the main code body | High |
| `[22] .text.startup` | PROGBITS | `0x21217490` | `0x16a454` (~1.4 MiB) | `AX` | constructor/startup code | High |
| `[23] .text.unlikely` | PROGBITS | `0x21381900` | `0x68469` (~427 KiB) | `AX` | cold/unlikely paths | High |
| `[24] google_init_cold` | PROGBITS | `0x213e9d80` | `0x60f1` | `AX` | cold init code | High |
| `[25] malloc_hook` | PROGBITS | `0x213efe80` | `0x89e` | `AX` | allocator hook trampolines | High |
| `[26] __lcxx_override` | PROGBITS | `0x213f0720` | `0x105` | `AX` | libc++ `operator new`/`delete` overrides | High |
| `[27] .plt` | PROGBITS | `0x213f0830` | `0x1da0` | `AX` | procedure linkage table (473 slots) | High |

### Writable: TLS, init arrays, RELRO data (LOAD 2)

| `[Nr]` Name | Type | Address | Offset | Size | Flg | Confidence |
|---|---|---|---|---|---|---|
| `[28] .tdata` | PROGBITS | `0x215f25e0` | `0x213f25e0` | `0x110` | `WAT` | High |
| `[29] .tbss` | NOBITS | `0x215f26f0` | `0x213f26f0` | `0xd68` | `WAT` | High |
| `[30] .init_array` | INIT_ARRAY | `0x215f26f0` | `0x213f26f0` | `0x5aa0` (23200 B) | `WAo` | High |
| `[31] .fini_array` | FINI_ARRAY | `0x215f8190` | `0x213f8190` | `0x10` | `WA` | High |
| `[32] .data.rel.ro` | PROGBITS | `0x215f81a0` | `0x213f81a0` | `0xa50990` (~10.3 MiB) | `WA` | High |
| `[33] .preinit_array` | PREINIT_ARRAY | `0x22048b30` | `0x21e48b30` | `0x10` | `WA` | High |
| `[34] .dynamic` | DYNAMIC | `0x22048b40` | `0x21e48b40` | `0x210` | `WA` | High |
| `[35] .got` | PROGBITS | `0x22048d50` | `0x21e48d50` | `0xc450` | `WA` | High |
| `[36] .relro_padding` | NOBITS | `0x220551a0` | `0x21e551a0` | `0xe60` | `WA` | High |

> **NOTE —** `.init_array` is `0x5aa0` = 23,200 bytes at vaddr `0x215f26f0`, exactly matching `DT_INIT_ARRAY`/`DT_INIT_ARRAYSZ` below. At 8 bytes per pointer that is 2,900 constructor function pointers — an enormous static-initialization fan-out. The roster of which constructors these are (and the ordering hazards) is owned by [`forensics/static-init.md`](static-init.md); this page only fixes the array's location and extent.

### Writable: small-model data and the custom sections (LOAD 3)

| `[Nr]` Name | Type | Address | Offset | Size | Flg | Confidence |
|---|---|---|---|---|---|---|
| `[37] .data` | PROGBITS | `0x222551c0` | `0x21e551c0` | `0x26a5d8` (~2.5 MiB) | `WA` | High |
| `[38] filewrapper_toc` | PROGBITS | `0x224bf798` | `0x220bf798` | `0x1e8` | `WA` | High |
| `[39] __rseq_cs` | PROGBITS | `0x224bf980` | `0x220bf980` | `0x2260` | `WA` | High |
| `[40] __rseq_cs_ptr_array` | PROGBITS | `0x224c1be0` | `0x220c1be0` | `0x898` | `WA` | High |
| `[41] linkarr_upb_AllExts` | PROGBITS | `0x224c2480` | `0x220c2480` | `0x4a0` | `WAo` | High |
| `[42] pb_defaults` | PROGBITS | `0x224c2920` | `0x220c2920` | `0x18` | `WA` | High |
| `[43] google_malloc_data` | PROGBITS | `0x224c2938` | `0x220c2938` | `0x48` | `WA` | High |
| `[44] .got.plt` | PROGBITS | `0x224c2980` | `0x220c2980` | `0xee0` | `WA` | High |
| `[45] .bss` | NOBITS | `0x224c3880` | `0x220c3860` | `0xd53b0` (~853 KiB) | `WAo` | High |

The `filewrapper_toc`, `__rseq_cs`/`__rseq_cs_ptr_array` (restartable-sequences critical-section descriptors for TCMalloc), `linkarr_upb_AllExts` (upb proto-extension link array), `pb_defaults`, and `google_malloc_data` sections are non-standard, linker-script-placed sections. Their internal layout and purpose are the subject of [`forensics/custom-sections.md`](custom-sections.md); they are listed here only to fix their addresses in the global map.

### Writable: large-model data (LOAD 4)

| `[Nr]` Name | Type | Address | Offset | Size | Flg | Confidence |
|---|---|---|---|---|---|---|
| `[46] .ldata` | PROGBITS | `0x22798c30` | `0x22198c30` | `0x21c00` | `WAl` | High |
| `[47] .lbss` | NOBITS | `0x227ba840` | `0x221ba830` | `0x9f940` (~654 KiB) | `WAl` | High |
| `[48] google_malloc_bss` | NOBITS | `0x2285a180` | `0x221ba830` | `0x5100` | `WAl` | High |

> **QUIRK —** the runtime's PJRT API singleton lives in `.lbss`, near vaddr `0x227ba840` (the section base), not in `.bss`. Because the binary is large-code-model, any global whose address the compiler could not prove reachable with a 32-bit displacement was forced into the `l`-flagged large tier. For the dispatcher that hands out the PJRT function-pointer table this means the table is reached by 64-bit absolute load, and a reverse engineer tracing `GetPjrtApi` must map LOAD 4 (delta `0x600000`) to find the backing bytes. See [`lifecycle/get-pjrt-api-thunk.md`](../lifecycle/get-pjrt-api-thunk.md).

### Non-allocated tail (not in any segment)

| `[Nr]` Name | Type | Offset | Size | Confidence |
|---|---|---|---|---|
| `[49] .symtab` | SYMTAB | `0x221ba830` | `0x1c3cc50` (~28.4 MiB, 1,232,970 entries) | High |
| `[50] .shstrtab` | STRTAB | `0x23df7480` | `0x243` | High |
| `[51] .strtab` | STRTAB | `0x23df76c3` | `0xab824de` (~172 MiB) | High |

These three carry no `SHF_ALLOC` flag (address `0x0`): they exist on disk for static tooling and are never mapped. Together they account for roughly 200 MiB of the file — over a quarter of its on-disk size is symbol names that the loader never touches.

---

## Dynamic Section

The `.dynamic` array (section `[34]`, `PT_DYNAMIC`) is 33 entries / `0x210` bytes at vaddr `0x22048b40`. It is the complete instruction set the loader executes to wire the library up. `readelf -d`:

```text
 Tag           Type            Name/Value
 NEEDED         libm.so.6
 NEEDED         libpthread.so.0
 NEEDED         libdl.so.2
 NEEDED         librt.so.1
 NEEDED         libc.so.6
 NEEDED         ld-linux-x86-64.so.2
 RELA           0x9170          RELASZ  25660464 (bytes)   RELAENT 24
 RELACOUNT      1069006
 JMPREL         0x1881da0       PLTRELSZ 11352 (bytes)      PLTREL  RELA
 PLTGOT         0x224c2980
 SYMTAB         0x2c8           SYMENT  24
 STRTAB         0x5730          STRSZ   14908 (bytes)
 GNU_HASH       0x50b8
 PREINIT_ARRAY  0x22048b30      PREINIT_ARRAYSZ 16
 INIT_ARRAY     0x215f26f0      INIT_ARRAYSZ    23200
 FINI_ARRAY     0x215f8190      FINI_ARRAYSZ    16
 INIT           0xe635524
 FINI           0xe63553c
 VERSYM         0x4840
 VERDEF         0x4e0c          VERDEFNUM  2
 VERNEED        0x4e44          VERNEEDNUM 6
 NULL           0x0
```

### What the loader is told

| Aspect | Value | Implication | Confidence |
|---|---|---|---|
| `DT_NEEDED` × 6 | `libm`, `libpthread`, `libdl`, `librt`, `libc`, `ld-linux-x86-64` | Self-contained: no C++ runtime, no CUDA, no driver `.so`. `libstdc++`/`libgcc_s` are *not* needed — the C++ runtime is statically linked in. | High |
| `DT_SONAME` | absent | The object names itself only by path; nothing `dlopen`s it by SONAME. | High |
| `DT_RPATH`/`DT_RUNPATH` | absent | No embedded search path. | High |
| Symbol hash | `DT_GNU_HASH` only (`0x50b8`); no legacy `DT_HASH` | Loader must support GNU hash; a DT_HASH-only loader cannot resolve symbols here. | High |
| `DT_INIT` / `DT_FINI` | `0xe635524` / `0xe63553c` | Legacy single-function ctor/dtor stubs in `.init`/`.fini`. | High |
| `DT_INIT_ARRAY` / size | `0x215f26f0` / 23,200 B (2,900 ptrs) | The real constructor fan-out — see [static-init](static-init.md). | High |
| `DT_FINI_ARRAY` / size | `0x215f8190` / 16 B (2 ptrs) | Two destructors only. | High |
| `DT_PREINIT_ARRAY` / size | `0x22048b30` / 16 B (2 ptrs) | Runs *before* `.init_array` — rare in libraries. | High |
| `DT_VERSYM`/`VERDEF`/`VERNEED` | `0x4840` / `0x4e0c` / `0x4e44` | Symbol versioning active: 2 version definitions, 6 version-need records. | High |

> **QUIRK —** `DT_NEEDED` lists only six bare system libraries for a 745 MiB plugin. Everything else — the LLVM/MLIR compiler, XLA, gRPC, protobuf, Abseil, Eigen, the libstdc++ ABI, and the TPU driver — is statically archived into this one file. The dependency surface is deliberately minimal so the plugin drops into any manylinux_2_31 host without dragging in a transitive `.so` graph. A reimplementer who expects to find these as shared dependencies will find them as `.text` instead.

> **NOTE —** the dynamic-string table `DT_STRTAB`/`STRSZ` is only 14,908 bytes — it names just the 741 dynamic symbols and 6 libraries. It is unrelated to the 172 MiB `.strtab`, which names the 1.2 M static symbols and is never consulted at load time.

---

## Symbol and Relocation Surface

The runtime-visible symbol surface is tiny relative to the binary; the relocation surface is enormous because a 534 MiB PIE text segment full of absolute pointers must be rebased at load.

### Dynamic symbols

| Metric | Value | Confidence |
|---|---|---|
| `.dynsym` entries | 741 (size `0x4578` / 24) | High |
| of which imports (`UND`) | ~1,028 across the wheel; this object's `.dynsym` resolves to a few hundred undefined + a few hundred defined exports | Medium |
| Exported symbol families | `Tpu*` C API (`TpuExecutor`, `TpuCompiler`, `TpuProgram`, …), plus `GetPjrtApi`, `TfTpu_Initialize`, Abseil internal entry points | High |
| Symbol versions | `GLIBC_2.x` (libc/libm/librt), `VERS_1.0` (the object's own definition) | High |

The export surface is intentionally narrow: a host loads this library and calls a handful of C entry points — chiefly `GetPjrtApi` and the `TfTpu_*` bootstrap — and the entire compiler/runtime hides behind them as internal (`LOCAL`) symbols. The polymorphic dispatch behind those few exports is covered in [`forensics/polymorphic-entry-points.md`](polymorphic-entry-points.md) and [`lifecycle/get-pjrt-api-thunk.md`](../lifecycle/get-pjrt-api-thunk.md).

### Relocations

| Table | Address | Entries | Type mix | Confidence |
|---|---|---|---|---|
| `.rela.dyn` | `0x9170` | 1,069,186 (size `0x1878c30` / 24) | `R_X86_64_RELATIVE` dominates (`DT_RELACOUNT` = 1,069,006 ≈ 99.98%) | High |
| `.rela.plt` | `0x1881da0` | 473 (size `0x2c58` / 24, `PLTRELSZ` 11,352) | `R_X86_64_JUMP_SLOT` (PLT GOT entries) | High |

> **GOTCHA —** of the ~1.07 M dynamic relocations, all but ~180 are `R_X86_64_RELATIVE` — pure load-bias adds with no symbol reference. This is the cost of a 534 MiB position-independent text segment: every absolute pointer baked into `.data.rel.ro`, vtables, and jump tables must be rebased. The relocation table (`RELASZ` = 25,660,464 bytes ≈ 24.5 MiB) is itself larger than many complete shared libraries. A loader processes over a million relocations before the first constructor runs; this, more than `.init_array`, dominates `dlopen` latency for this plugin.

The 473 PLT slots are the lazily-bound calls into the six `DT_NEEDED` libraries (`malloc`, `pthread_*`, `dlopen`, math, etc.). `PLTGOT` = `0x224c2980` (section `[44] .got.plt`) sits in LOAD 3, outside `GNU_RELRO`, precisely so the lazy resolver can write resolved targets into it at runtime — the rest of the GOT (`[35] .got`) is RELRO-frozen.

> **CORRECTION (ELF-1) —** an earlier scratch fingerprint recorded this object as having `sections=51`. `readelf -h` reports `Number of section headers: 52`. Both are consistent: the 51 counts the meaningful sections, the 52 includes the mandatory `NULL` section `[0]`. This page uses the on-the-wire count of 52.

> **CORRECTION (ELF-2) —** the same scratch note listed `build_id=<none>` and `soname=<none>` for this object. `readelf -n` confirms `soname` is genuinely absent, but the build-id is present and equals `89edbbe81c5b328a958fe628a9f2207d` (the `<none>` was a tooling miss, not a real absence). The version-pin at the top of every wiki page relies on this build-id.

---

## Cross-References

- [Forensics Overview](overview.md) — the parallel high-level tour of the binary; this page is its byte-level substrate
- [Two-Binary Split](two-binary-split.md) — why the wheel ships `libtpu.so` plus a smaller `sdk.so`, and how their ELF shapes differ
- [Static Initialization](static-init.md) — owns the `.init_array` constructor roster and ordering; this page fixes only the array's location/size
- [Custom Sections](custom-sections.md) — deep dive on `google_*`, `__rseq_cs`, `linkarr_upb_AllExts`, `filewrapper_toc`, `pb_defaults` listed in the section table above
- [ELF Entry and Init Procedure](../lifecycle/elf-entry-and-init-proc.md) — how a library with entry `0x0` reaches first execution through `DT_PREINIT_ARRAY`/`DT_INIT`/`DT_INIT_ARRAY`
- [GetPjrtApi Thunk](../lifecycle/get-pjrt-api-thunk.md) — the exported entry whose backing singleton lives in the `.lbss` large-model region documented here
- [Lifecycle Overview](../lifecycle/overview.md) — the load-time sequence that consumes this page's dynamic section
