# Custom Sections

> *All addresses on this page apply to `libtpu.so` version 0.103 from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other builds will differ. Every section name, address, size, and flag set below was confirmed against `readelf -SW` and `readelf -x`; raw-note figures that disagreed were corrected in place.*

## Abstract

A textbook x86-64 shared object has a dozen well-known sections — `.text`, `.rodata`, `.data`, `.bss`, `.dynsym`, `.eh_frame` — and a reverse-engineer can predict every one of them. `libtpu.so` has **52** section headers, and roughly a third of them are *not* in that textbook. Some are mandatory artifacts of how the binary was compiled (an LLVM/clang large-code-model build splits rodata, data, and bss into `.lrodata`/`.ldata`/`.lbss` because the small-data 2 GiB window cannot hold a 745 MiB image). Others are linker-array sections invented by Google's internal libraries — `google_malloc`, `__rseq_cs`, `protodesc_cold`, `linkarr_upb_AllExts`, `__lcxx_override`, `filewrapper_toc` — each a contiguous run of objects or thunks that a startup routine or runtime walks by its `__start_`/`__stop_` encapsulation symbols. A reverse-engineer who assumes these are padding or junk will miss the tcmalloc per-CPU fast path, the protobuf descriptor pool, and the PJRT API singleton.

This page is the annotated *subset*: it does not re-tabulate all 52 sections (that is the job of the [ELF anatomy](elf-anatomy.md) page, which owns the authoritative full table, the `PT_LOAD` segment map, and the dynamic array). Here we take only the notable and the genuinely weird sections and answer one question per section: *what lives in it, and why does it exist*. The reframe to the familiar: think of this as the difference between an LLVM/lld large-code-model link of a monolithic Abseil/protobuf/MLIR/XLA blob, versus a small ordinary `.so` — the extra sections are the cost of monolithic static linking at a scale where the 32-bit `R_X86_64_PC32` displacement no longer reaches.

For reimplementation / analysis, the contract is:

- **The large-code-model split** — why `.lrodata` (108 MiB), `.ldata`, and `.lbss` exist as siblings of `.rodata`/`.data`/`.bss`, how the `l` (large) flag and the high `0x1884a00`+ load addresses signal `-mcmodel=large`, and that the PJRT API vtable singleton lives in `.lbss`.
- **The descriptor / proto data** — `protodesc_cold` (the protobuf descriptor pool), `linkarr_upb_AllExts` (upb extension registrars), `pb_defaults`.
- **The runtime / allocator sections** — `google_malloc`, `google_malloc_data`, `google_malloc_bss`, `__rseq_cs`, `__rseq_cs_ptr_array`, `malloc_hook` — the tcmalloc per-CPU restartable-sequence machinery.
- **Unwinding and notes** — `.eh_frame`/`.eh_frame_hdr`/`.gcc_except_table`, and the `.note.gnu.build-id` that fixes the version.
- **The genuine oddities** — `__lcxx_override`, `filewrapper_toc`, `.text.split` (zero bytes), and the fact that the binary carries a full `.symtab` and is **not** classically stripped.

| | |
|---|---|
| **Section header count** | 52 (header index 50 = `.shstrtab`) |
| **File size** | 781,691,048 bytes (~745 MiB) |
| **Code model** | LLVM/clang `-mcmodel=large` (sections flagged `l`) |
| **Largest notable section** | `.lrodata` — `0x6c0e7d0` (108.1 MiB), flags `AMSl` |
| **PJRT API singleton** | `_ZZN4pjrt10tpu_plugin13GetTpuPjrtApiEvE8pjrt_api` @ `0x227ba840` (`.lbss`, 1120 B) |
| **build-id** | `89edbbe81c5b328a958fe628a9f2207d` (`.note.gnu.build-id`, 32 B) |
| **Stripped?** | No — `.symtab` present (1,232,970 symbols, 28.2 MiB) |

The notable sections, at a glance (textbook `.text`/`.rodata`/`.data`/`.dynsym`/`.plt`/`.got` are omitted — see [ELF anatomy](elf-anatomy.md)):

| Section | VAddr | Size | Flags | Contents | Confidence |
|---|---|---|---|---|---|
| `[10] .lrodata` | `0x1884a00` | `0x6c0e7d0` (108.1 MiB) | `AMSl` | Large-model read-only data: const tables, string pools, vtables | CERTAIN |
| `[12] protodesc_cold` | `0xbe8af30` | `0x334180` (3.2 MiB) | `A` | protobuf `descriptor_table_protodef_*` + `TableStruct_*::offsets` | CERTAIN |
| `[13] .gcc_except_table` | `0xc1bf0b0` | `0x10d584` (1.05 MiB) | `A` | LSDA tables for C++ exception landing pads | CERTAIN |
| `[14] .eh_frame_hdr` | `0xc2cc634` | `0x6bd684` (6.74 MiB) | `A` | Binary-search index into `.eh_frame` | CERTAIN |
| `[15] .eh_frame` | `0xc989cb8` | `0x1cab86c` (28.7 MiB) | `A` | DWARF CFI unwind descriptors | CERTAIN |
| `[19] google_malloc` | `0xe6373c0` | `0x46f2` (18.1 KiB) | `AXo` | tcmalloc per-CPU rseq thunks + check-fail helpers | CERTAIN |
| `[20] .text.split` | `0xe63bab2` | `0x0` (0 B) | `AXo` | Empty split-text marker section | CERTAIN |
| `[24] google_init_cold` | `0x213e9d80` | `0x60f1` (24.2 KiB) | `AX` | Cold-path static-init code | HIGH |
| `[25] malloc_hook` | `0x213efe80` | `0x89e` (2.2 KiB) | `AX` | Abseil `LowLevelAlloc` allocation hooks | CERTAIN |
| `[26] __lcxx_override` | `0x213f0720` | `0x105` (261 B) | `AX` | Overridden libc++ `operator new`/`delete` thunks | CERTAIN |
| `[30] .init_array` | `0x215f26f0` | `0x5aa0` (23.2 KiB) | `WAo` | 2900 constructor pointers (census → static-init) | CERTAIN |
| `[31] .fini_array` | `0x215f8190` | `0x10` (16 B) | `WA` | 2 destructor pointers | CERTAIN |
| `[33] .preinit_array` | `0x22048b30` | `0x10` (16 B) | `WA` | 2 pre-init pointers (rare in a library) | CERTAIN |
| `[38] filewrapper_toc` | `0x224bf798` | `0x1e8` (488 B) | `WA` | Embedded-file table-of-contents (zero-filled on disk) | MEDIUM |
| `[39] __rseq_cs` | `0x224bf980` | `0x2260` (8.8 KiB) | `WA` | tcmalloc restartable-sequence critical-section descriptors | CERTAIN |
| `[40] __rseq_cs_ptr_array` | `0x224c1be0` | `0x898` (2.2 KiB) | `WA` | 275 pointers into `__rseq_cs` | HIGH |
| `[41] linkarr_upb_AllExts` | `0x224c2480` | `0x4a0` (1.2 KiB) | `WAo` | 37 × 32 B upb proto-extension registrars | CERTAIN |
| `[42] pb_defaults` | `0x224c2920` | `0x18` (24 B) | `WA` | protobuf C++-feature default-instance pointer | HIGH |
| `[43] google_malloc_data` | `0x224c2938` | `0x48` (72 B) | `WA` | tcmalloc writable globals | HIGH |
| `[46] .ldata` | `0x22798c30` | `0x21c00` (135 KiB) | `WAl` | Large-model writable data | CERTAIN |
| `[47] .lbss` | `0x227ba840` | `0x9f940` (638 KiB) | `WAl` | Large-model zero-init data — **PJRT API singleton** | CERTAIN |
| `[48] google_malloc_bss` | `0x2285a180` | `0x5100` (20.4 KiB) | `WAl` | tcmalloc large-model zero-init globals | HIGH |
| `[1] .note.gnu.build-id` | `0x2a8` | `0x20` (32 B) | `A` | GNU build-id note (version anchor) | CERTAIN |

> **NOTE —** the `Flags` column uses `readelf`'s letters: `A` alloc, `W` write, `X` execute, `M` merge, `S` strings, `l` large (`SHF_X86_64_LARGE`), `o` OS-specific (`SHF_GNU_RETAIN` / link-order). The `l` flag on `.lrodata`/`.ldata`/`.lbss`/`google_malloc_bss` is the unambiguous large-code-model fingerprint.

---

## Large-Code-Model Sections

### Why they exist

The x86-64 small/medium code models assume code and data live within a ±2 GiB window reachable by a 32-bit signed `RIP`-relative displacement (`R_X86_64_PC32`). `libtpu.so` is a 745 MiB image whose `.text` alone is `0x12bdb484` ≈ 313 MiB at vaddr `0xe63c000`; the moment rodata and code together exceed the 2 GiB displacement budget, the compiler must emit 64-bit addressing for "large" objects. clang/LLVM's `-mcmodel=large` answer is to *segregate* the large objects into their own sections, flagged `SHF_X86_64_LARGE` (`l`), and place them past the small-data window. That is precisely the layout here: `.rodata` (the small, mergeable read-only data) sits at `0x84a0000`, while the *large* read-only data is a separate `.lrodata` placed earlier at `0x1884a00` and the large writable data is `.ldata`/`.lbss` placed last, above `0x22000000`.

> **QUIRK —** the presence of *both* `.rodata` and `.lrodata` (and `.data`/`.ldata`, `.bss`/`.lbss`) is not a duplication bug — it is the defining signature of an LLVM `-mcmodel=large` build. A small-model `.so` has no `.l*` siblings. A reverse-engineer who only greps for `.rodata` will miss 108 MiB of the most important constant data in the binary.

### .lrodata — the 108 MiB constant store

```text
  [10] .lrodata          PROGBITS  0000000001884a00 1884a00 6c0e7d0 00 AMSl  0   0 16
```

`0x6c0e7d0` = **113,305,552 bytes (108.1 MiB)** — larger than `.rodata` (57.9 MiB) by nearly 2×, and by far the largest non-code, non-symtab section in the file. Flags `AMSl`: allocated, mergeable (`M`), string-containing (`S`), large (`l`), with 16-byte alignment. This is where the bulk of read-only program data lives: MLIR/LLVM static op tables, demangled type-name string pools, RTTI type-info records, and the read-only halves of vtables. The `M`+`S` flags mean the linker merged duplicate string constants here; the 16-byte alignment is the large-section default. Because it is mergeable read-only, it maps into the first executable+read `PT_LOAD` and is shared across processes.

### .ldata and .lbss — large writable data

```text
  [46] .ldata            PROGBITS  0000000022798c30 22198c30 021c00 00 WAl  0   0 16
  [47] .lbss             NOBITS    00000000227ba840 221ba830 09f940 00 WAl  0   0 64
```

`.ldata` is `0x21c00` (135 KiB) of initialized large writable data; `.lbss` is `0x9f940` (638 KiB) of zero-init large writable data (a `NOBITS` section — it occupies no file bytes, only a memory reservation). Both carry the `l` flag.

The most important resident of `.lbss` is the **PJRT plugin API table singleton**. The symbol table places a 1120-byte LOCAL `OBJECT` at the exact start of the section:

```text
  1448: 00000000227ba840  1120 OBJECT  LOCAL  DEFAULT  47 _ZZN4pjrt10tpu_plugin13GetTpuPjrtApiEvE8pjrt_api
```

Demangled, that is `pjrt::tpu_plugin::GetTpuPjrtApi()::pjrt_api` — the static `PJRT_Api` struct (function-pointer table) that the exported `GetPjrtApi` entry point (`0xe6a83a0`, 5 bytes, in `.text`) hands back to the framework. Its Meyers-singleton guard variable lives separately in `.bss`:

```text
  1447: 00000000224c3f90     8 OBJECT  LOCAL  DEFAULT  45 _ZGVZN4pjrt10tpu_plugin13GetTpuPjrtApiEvE8pjrt_api
```

> **NOTE —** the single most-called-into object in the whole plugin — the PJRT dispatch table — sits in `.lbss` precisely *because* it is a large-model build. In a small-model `.so` it would be an ordinary `.bss` object. The `l`-flagged placement is purely an addressing-model consequence, not a security or layout choice. The runtime construction of this table is a `.init_array` constructor; see [static-init](static-init.md).

---

## Descriptor / Proto Data Sections

### protodesc_cold — the descriptor pool

```text
  [12] protodesc_cold    PROGBITS  000000000be8af30 be8af30 334180 00   A  0   0 16
```

`0x334180` = **3,359,104 bytes (3.2 MiB)**, read-only (`A`). This is the protobuf C++ runtime's *cold* descriptor data — the serialized `.proto` schemas that `descriptor_table_*` initializers feed into the global descriptor pool at startup. The symbol table confirms the contents directly:

```text
  1620: 000000000be8af80 497 OBJECT LOCAL .. _ZL37descriptor_table_protodef_zzRDQFgX_23
  1642: 000000000be8af30  76 OBJECT LOCAL .. _ZN23TableStruct_zzRDQFgX_237offsetsE
  2506: 000000000be8b210 461 OBJECT LOCAL .. _ZL37descriptor_table_protodef_eguDetDQC_5
```

Each `descriptor_table_protodef_*` blob is a serialized `FileDescriptorProto`; each `TableStruct_*::offsets` array maps message fields to their in-memory offsets. The `_cold` suffix is the section-placement hint clang attaches to data touched only during one-time descriptor registration — grouping it keeps the hot `.rodata` cache-dense. The proto filenames are obfuscated (`zzRDQFgX_23`, `eguDetDQC_5`), a build-system artifact, not encryption.

> **GOTCHA —** `protodesc_cold` is *not* code despite its placement near the executable region — it has flags `A` only, no `X`. The "cold" naming convention (also seen in `google_init_cold`, `.text.unlikely`) is clang's profile/heuristic partitioning of rarely-executed bytes, applied here to data. Do not assume a `*_cold` section is executable.

### linkarr_upb_AllExts — upb extension registrars

```text
  [41] linkarr_upb_AllExts PROGBITS 00000000224c2480 220c2480 0004a0 00 WAo  0   0 16
```

`0x4a0` = 1184 bytes = **37 entries of 32 bytes**, bracketed by the encapsulation symbols `__start_linkarr_upb_AllExts` (`0x224c2480`) and `__stop_linkarr_upb_AllExts` (`0x224c2920`). This is a *linker array*: each translation unit that defines a upb (micro-protobuf) proto extension drops a 32-byte registrar record into this named section, and a startup routine iterates `[__start, __stop)` to register every extension with the global registry. The contents are named:

```text
  1161430: 00000000224c2480 32 OBJECT LOCAL .. envoy_annotations_disallowed_by_default_ext
  1161431: 00000000224c24a0 32 OBJECT LOCAL .. envoy_annotations_deprecated_at_minor_version_ext
```

The `o` flag (`SHF_GNU_RETAIN`) keeps these records even though no symbol references them directly — they are reached only by the `__start_`/`__stop_` sweep, so the linker must be told not to garbage-collect them.

### pb_defaults

```text
  [42] pb_defaults       PROGBITS  00000000224c2920 220c2920 000018 00  WA  0   0  8
```

24 bytes. A single small writable table the protobuf C++ runtime uses to hold the `CppFeatures` default-instance pointer and edition-defaults offset. Hexdump shows it is mostly zero with a `0x18` length field — it is populated at static-init time.

---

## Runtime / Allocator Sections

The binary statically links Google's tcmalloc, and tcmalloc's per-CPU fast path is built on the Linux **restartable sequences** (`rseq`) kernel ABI. That mechanism needs three cooperating sections — code thunks, critical-section descriptors, and a pointer array — plus writable globals.

### google_malloc — tcmalloc rseq thunks

```text
  [19] google_malloc     PROGBITS  000000000e6373c0 e6373c0 0046f2 00 AXo  0   0 64
```

`0x46f2` = 18,162 bytes of *executable* code (`AX`), retained (`o`), 64-byte aligned, bracketed by `__start_google_malloc`/`__stop_google_malloc`. This is the per-CPU allocator hot path — the restartable-sequence functions the kernel may abort and restart on preemption:

```text
  1228043: 000000000e637480 33 FUNC LOCAL .. RseqFunction_PerCpuCmpxchg64
  1228044: 000000000e637440 42 FUNC LOCAL .. RseqFunction_PerCpuTryLock
  1228047: 000000000e6374c0 43 FUNC LOCAL .. RseqFunction_PerCpuCmpxchgCheck64
```

These live in their own retained section because the `__rseq_cs` descriptors (below) point at exact start/commit/abort instruction addresses inside them; the section keeps them contiguous and prevents the linker from reordering or eliding the carefully-laid-out commit windows.

### __rseq_cs and __rseq_cs_ptr_array

```text
  [39] __rseq_cs           PROGBITS 00000000224bf980 220bf980 002260 00 WA  0   0 32
  [40] __rseq_cs_ptr_array PROGBITS 00000000224c1be0 220c1be0 000898 00 WA  0   0  8
```

`__rseq_cs` is `0x2260` (8800 bytes) of 32-byte `struct rseq_cs` descriptors — each one a `{version, flags, start_ip, post_commit_offset, abort_ip}` record telling the kernel "if this thread is preempted while its instruction pointer is inside `[start_ip, start_ip+post_commit_offset)`, jump to `abort_ip`." `__rseq_cs_ptr_array` is `0x898` = **275 eight-byte pointers** into `__rseq_cs`, the indirection table tcmalloc registers with the kernel. The `start_ip`/`abort_ip` fields point into `google_malloc`. The hexdump shows the descriptor `flags`/`version` fields and offsets (`0x1a`, `0x16`, …) inline.

> **QUIRK —** `__rseq_cs` being writable (`WA`) is required: the kernel `rseq(2)` registration writes back into these descriptors, and the loader relocates the embedded instruction-pointer fields. A reimplementer treating them as const will fault at registration time.

### malloc_hook, google_malloc_data, google_malloc_bss

```text
  [25] malloc_hook        PROGBITS 00000000213efe80 213efe80 00089e 00 AX  0   0 32
  [43] google_malloc_data PROGBITS 00000000224c2938 220c2938 000048 00 WA  0   0  8
  [48] google_malloc_bss  NOBITS   000000002285a180 221ba830 005100 00 WAl 0   0 16
```

`malloc_hook` (`0x89e`, executable) holds Abseil's low-level allocator entry points — `absl::base_internal::LowLevelAlloc::Alloc`/`Free`/`AllocWithArena` — the bootstrap allocator used before tcmalloc is initialized, bracketed by `__start_malloc_hook`/`__stop_malloc_hook`. `google_malloc_data` (72 bytes, writable) and `google_malloc_bss` (`0x5100` = 20.4 KiB, zero-init, large-flagged) hold tcmalloc's mutable globals — per-size-class freelist heads, sampling state, the central cache. `google_malloc_bss` carries the `l` flag, so even the allocator's BSS got pushed into the large-model region.

### google_init_cold

```text
  [24] google_init_cold  PROGBITS  00000000213e9d80 213e9d80 0060f1 00 AX  0   0 32
```

`0x60f1` = 24,817 bytes of executable cold-path initialization code — the rarely-taken branches of static constructors, partitioned out of `.text.startup` by clang so the common-case init path stays cache-dense. Companion to `.text.unlikely`.

---

## Unwinding and Note Sections

### .eh_frame / .eh_frame_hdr / .gcc_except_table

```text
  [13] .gcc_except_table PROGBITS 000000000c1bf0b0 c1bf0b0 10d584 00 A  0   0  4
  [14] .eh_frame_hdr     PROGBITS 000000000c2cc634 c2cc634 6bd684 00 A  0   0  4
  [15] .eh_frame         PROGBITS 000000000c989cb8 c989cb8 1cab86c 00 A  0   0  8
```

`.eh_frame` is **28.7 MiB** of DWARF Call Frame Information — one FDE per function describing how to restore the stack during exception propagation and stack unwinding. `.eh_frame_hdr` (6.74 MiB) is the sorted binary-search index the C++ personality routine uses to locate the FDE for a faulting PC in `O(log n)` rather than scanning. `.gcc_except_table` (1.05 MiB) holds the LSDA (Language-Specific Data Area) tables — the per-function maps from call-site PC ranges to landing-pad addresses and catch-type filters.

The sheer size (a combined 36.5 MiB) reflects ~1 million functions, all compiled with exceptions enabled. Note the section is named `.gcc_except_table` even though the toolchain is clang/LLVM — that name is the platform ABI convention, not evidence of GCC.

> **NOTE —** these three sections are textbook in *kind* but extraordinary in *size*, and they are the backbone of the binary's RTTI/exception machinery. A reimplementer stripping exceptions would shed ~5% of the file. The unwinder type is `X86_64_UNWIND` for the `PT_GNU_EH_FRAME` segment that wraps `.eh_frame_hdr`.

### .note.gnu.build-id

```text
  [1] .note.gnu.build-id NOTE  00000000000002a8 0002a8 000020 00 A  0   0  4
```

The 32-byte GNU note that anchors this entire wiki to one build. Its layout is the standard `Elf_Nhdr`: `namesz=4` (`"GNU\0"`), `descsz=0x10`, `type=3` (`NT_GNU_BUILD_ID`), followed by the 16-byte hash:

```text
  0x000002a8 04000000 10000000 03000000 474e5500  ............GNU.
  0x000002b8 89edbbe8 1c5b328a 958fe628 a9f2207d  .....[2....(.. }
```

→ build-id `89edbbe81c5b328a958fe628a9f2207d`. This is the first allocated section in the file (vaddr `0x2a8`, right after the program headers) and the canonical version key cited in every page's version-pin blockquote.

---

## Genuine Oddities

### __lcxx_override — overridden global new/delete

```text
  [26] __lcxx_override   PROGBITS  00000000213f0720 213f0720 000105 00 AX  0   0 32
```

261 bytes of executable code holding the *replacement* global allocation operators that libc++ (the `__lcxx` prefix) routes through tcmalloc instead of the default malloc:

```text
  1231661: 00000000213f0720  69 FUNC LOCAL .. _Znwm                 // operator new(size_t)
  1231663: 00000000213f07a0 104 FUNC LOCAL .. _ZnwmSt11align_val_t  // operator new(size_t, align_val_t)
  1231665: 00000000213f0780   5 FUNC LOCAL .. _Znam                 // operator new[](size_t)
  1231669: 00000000213f0820   5 FUNC LOCAL .. _ZnamSt11align_val_t  // operator new[](size_t, align_val_t)
```

Isolating the operator-new/delete overrides into a named section lets the link guarantee they win over the libc++ defaults (interposition ordering) and lets the runtime verify the override is present. A reverse-engineer who sees `__lcxx_override` should immediately read it as "this binary replaces the global allocator" — every C++ `new` in the entire 745 MiB image funnels through these four thunks into tcmalloc.

### filewrapper_toc

```text
  [38] filewrapper_toc   PROGBITS  00000000224bf798 220bf798 0001e8 00 WA  0   0  8
```

488 bytes, writable, zero-filled on disk (the hexdump is all `00`). This is the table-of-contents for Google's `file_wrapper`/embedded-file mechanism — a registry of resources compiled into the binary, populated at static-init by relocations rather than carrying initialized bytes on disk. With no non-zero on-disk content the exact schema is not recoverable from bytes alone; its purpose is inferred from the symbol name and the `WA` + zero-fill pattern (MEDIUM confidence on the precise record format).

### .text.split — the empty section

```text
  [20] .text.split       PROGBITS  000000000e63bab2 e63bab2 000000 00 AXo  0   0  1
```

A **zero-byte** executable section sitting between `google_malloc` and `.text`. It is the marker/anchor clang's machine-function-splitting pass emits to delimit the split-text region; in this build the split cold-code landed elsewhere (`.text.unlikely`, `google_init_cold`), leaving this section empty but present. It is harmless, but a reverse-engineer scanning the section table should not be alarmed by a 0-byte `PROGBITS X` entry — it carries no code.

### Not stripped — the full .symtab

```text
  [49] .symtab           SYMTAB  0000000000000000 221ba830 1c3cc50 18  51 1232970  8
  [51] .strtab           STRTAB  0000000000000000 23df76c3 ab824de 00   0   0  1
```

Despite being a production plugin, `libtpu.so` ships with its **full `.symtab`** — 1,232,970 symbols (28.2 MiB) plus a 171.5 MiB `.strtab` of their mangled names — *in addition to* the load-time `.dynsym` (1118 symbols). It is therefore **not classically stripped**: every local function and object carries a readable mangled name, which is why this wiki can resolve `descriptor_table_protodef_*`, `RseqFunction_PerCpuCmpxchg64`, and the PJRT singleton by name rather than by address alone.

> **QUIRK —** the `.symtab` + `.strtab` pair is *non-allocated* (vaddr `0x0`, no `A` flag) — it costs zero runtime memory but ~200 MiB of file size. Google ships it for crash-symbolication. For a reverse-engineer this is an enormous gift: the binary is effectively self-documenting. Run `strip --strip-debug` and 200 MiB and all local names vanish, leaving only the 1118 `.dynsym` exports.

> **CORRECTION (W030-1) —** prior scratch notes tallied the section header count as part of a "sections: 88" aggregate and listed a `.tm_clone_table` of size 0. The aggregate counted two ELF objects (`libtpu.so` + `sdk.so`) together; `libtpu.so` alone has exactly **52** section headers per `readelf -h`/`readelf -S`, and the `.tm_clone_table` and `.tdata`/`.tbss` rows in that aggregate belong partly to the sibling `sdk.so`. This page's tables describe `libtpu.so` only; figures were re-derived directly from `readelf -SW libtpu.so`.

> **CORRECTION (W030-2) —** the `.lbss` size was re-measured as `0x9f940` = 653,632 bytes (638 KiB), not the rounded value implied by earlier notes; `.ldata` is `0x21c00` = 138,240 bytes (135 KiB). The PJRT singleton sits at the section's exact base `0x227ba840`, confirmed by the `.symtab` entry, not merely "near" it.

---

## Cross-References

- [ELF Anatomy](elf-anatomy.md) — owns the authoritative full 52-section table, the `PT_LOAD` segment map, and the dynamic array; this page is the annotated subset of *notable* sections.
- [Static Initialization](static-init.md) — owns the `.init_array` census (2900 constructor slots); this page only fixes the array's location and notes the PJRT singleton it builds.
- [Trailing zstd Blob](trailing-zstd-blob.md) — owns the question of any embedded/trailing carved payload; this page covers only named ELF sections.
- [Binary Forensics Overview](overview.md) — the map of the whole forensics part and where each section's deep dive lives.
- [Two-Binary Split](two-binary-split.md) — why `libtpu.so` and `sdk.so` are separate objects (the source of the `sections: 88` aggregate the corrections above untangle).
