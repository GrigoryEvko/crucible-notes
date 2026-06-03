# Binary Forensics Overview

> *All offsets, addresses, section names, and counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel: a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d`, reported runtime version `0.103`. Other wheels will differ in every address.*

## Abstract

`libtpu.so` is the Google TPU PJRT plugin: a single statically-linked shared object that packs an MLIR/XLA compiler, a TPU runtime, a device driver, and most of their third-party dependencies into one 745 MiB blob. It is the kind of artifact that defeats casual inspection — `readelf -S` alone returns 52 sections, the symbol table holds over 1.2 million entries, and the code section is larger than many entire toolchains. This page is the map for the rest of the Binary Forensics part: it establishes the verified top-level shape of the file so that every deeper page can reference a shared frame instead of re-deriving the headline numbers.

The shape is unusual in three ways that drive the rest of the section. First, the object is **not stripped** — it ships a full `.symtab` (1,233,710 entries) and a 171 MiB `.strtab`, so the disassembler recovers real C++ symbol names for the overwhelming majority of functions rather than `sub_`-only placeholders. Second, the linker used **function/data section splitting** aggressively: alongside the conventional `.text`/`.rodata`/`.data` there is a parallel `.text.hot` / `.text.startup` / `.text.unlikely` / `.text.split` family and a `.lrodata` / `.ldata` / `.lbss` "large" family for objects past the small-code-model 2 GiB reach. Third, the build embeds **named non-standard sections** — `google_malloc`, `malloc_hook`, `protodesc_cold`, `filewrapper_toc`, `__rseq_cs`, `linkarr_upb_AllExts` and more — that betray the internal allocator, the protobuf descriptor pool, an embedded file-wrapper table-of-contents, and restartable-sequence metadata.

This page presents the at-a-glance binary statistics, the ELF section/segment census, the function population shape, and a coarse "capsule atlas" of the major embedded regions, then forward-links each of those to the deep page that owns it. Treat the numbers here as the canonical anchors; treat any claim about *contents* of a region (e.g. what the trailing bytes decode to, what a dispatch table means) as the property of the page that owns that region.

For reimplementation — or, more realistically, for re-deriving this analysis against a different wheel — the contract is:

- **The container shape.** 52 sections, 11 program headers, four `PT_LOAD` segments, where code, read-only, and writable data live, and how the split-section families partition each.
- **The function population.** ~884.8 K disassembler-recovered functions, ~99.66 % carrying real symbol names; how to reproduce the named/anonymous split and the namespace concentration.
- **The two-binary fact.** The wheel ships `libtpu.so` (this file) and a much smaller `sdk.so` in the same directory; the analysis covers both, and several runtime concepts only make sense across the pair.
- **The embedded-blob inventory.** Which large regions are code, which are descriptor pools, which are compressed payloads — and which deep page reverse-engineers each.

| | |
|---|---|
| **File** | `libtpu/libtpu.so` (in the `cp314` manylinux wheel) |
| **Size** | 781,691,048 bytes (745.5 MiB) |
| **Type** | ELF64 LSB shared object (`DYN`), x86-64, version 1 (SYSV) |
| **Build-id** | `89edbbe81c5b328a958fe628a9f2207d` (NT_GNU_BUILD_ID, md5/uuid form) |
| **Reported version** | `0.103` (from package metadata; see correction below) |
| **Stripped?** | **No** — full `.symtab` present (1,233,710 entries) |
| **Sections** | 52 (`e_shnum`); `.shstrtab` index 50 |
| **Program headers** | 11 (`e_phnum`); 4 × `PT_LOAD` |
| **Section headers at** | offset `0x2e979ba8` (781,687,720) — runs exactly to EOF |
| **Entry point** | `0x0` (none — a library, driven through `.init_array`) |
| **Disassembled functions** | 884,832 (IDA), 881,784 named / 3,048 anonymous |
| **Sibling object** | `sdk.so` — 22,541,240 bytes, 94,732 functions |

> **CORRECTION (FOR-01) —** the `0.103` version is taken from the package/runtime metadata reported by the surrounding tooling, **not** observed as a literal `0.103` ASCII string in the binary on a plain byte scan. The build-id, file size, section count, and segment count below were all confirmed directly against the binary with `readelf`/`stat`; the version string was not, and is carried here as reported-but-unverified. A reimplementer should pin to the build-id, which is unambiguous.

---

## Container Shape at a Glance

The file is a normal `DYN` object with an abnormal size distribution. Nearly half the on-disk bytes are code; most of the rest is read-only constant data and the (unstripped) symbol/string tables. The four largest sections account for the overwhelming majority of the file.

| Section | Size | Share | Role | Confidence |
|---|---|---|---|---|
| `.text` | 299.9 MiB (314,422,404 B) | ~40 % | Primary executable code | CERTAIN |
| `.strtab` | 171.5 MiB (179,840,222 B) | ~23 % | Symbol-name strings (unstripped) | CERTAIN |
| `.lrodata` | 108.1 MiB (113,305,552 B) | ~14 % | Large-model read-only data | CERTAIN |
| `.rodata` | 57.9 MiB (60,731,176 B) | ~8 % | Read-only constants, vtables, RTTI | CERTAIN |
| `.eh_frame` | 28.7 MiB (30,062,700 B) | ~4 % | DWARF CFI unwind tables | CERTAIN |
| `.symtab` | 28.2 MiB (29,609,040 B) | ~4 % | 1,233,710 symbol entries | CERTAIN |
| `.rela.dyn` | 24.5 MiB (25,660,464 B) | ~3 % | Dynamic relocations | CERTAIN |
| (remaining 45 sections) | ~38 MiB | ~5 % | unwind hdr, got, data, custom | HIGH |

> **NOTE —** `.text` alone (299.9 MiB) is larger than a typical full LLVM `libLLVM.so`. The single biggest *lever* on reasoning about this file is that the code is one giant section in the **small/medium code model** with a "large" overflow family (`.lrodata`/`.ldata`/`.lbss`) for objects whose displacement would not fit a 32-bit `RIP`-relative reference. The split-section discipline below is what keeps a binary this size linkable at all.

> **GOTCHA —** the on-disk byte distribution is not the runtime memory distribution. `.symtab` and `.strtab` (≈ 200 MiB combined) are **not** part of any `PT_LOAD` segment — they are debug/link metadata that the loader never maps. A reimplementer estimating resident set from file size will be off by roughly that 200 MiB; size the load segments instead (§ELF anatomy).

---

## ELF Section and Segment Census

`readelf -hSl` confirms the headline structure. The section table places 52 entries; the program header table places 11 entries, of which four are `PT_LOAD`.

```text
ELF Header (readelf -h, abbreviated)
  Type:                   DYN (Shared object file)
  Machine:                Advanced Micro Devices X86-64
  Entry point address:    0x0
  Start of program headers:   64
  Start of section headers:   781687720   (0x2e979ba8)
  Number of program headers:  11
  Number of section headers:  52
  Section header string table index: 50
```

The four loadable segments partition the address space into the conventional R-E / RW / RW / RW shape, with one twist: the "large" data family gets its own `PT_LOAD`.

```text
Program headers (PT_LOAD only)            FileSiz     MemSiz   Flg
  LOAD  off 0x00000000  vaddr 0x00000000  0x213f25d0  0x213f25d0  R E   code + all read-only
  LOAD  off 0x213f25e0  vaddr 0x215f25e0  0x00a62bc0  0x00a63a20  RW    relro: got, init_array, data.rel.ro
  LOAD  off 0x21e551c0  vaddr 0x222551c0  0x0026e6a0  0x00343a70  RW    .data, .bss, custom data sections
  LOAD  off 0x22198c30  vaddr 0x22798c30  0x00021c00  0x000c6650  RW    .ldata / .lbss (large model)
```

The R-E segment swallows everything from the build-id note through `.plt` — including all read-only data (`.lrodata`, `.rodata`, `protodesc_cold`, `.gcc_except_table`, the EH frames) and the entire code-section family. The remaining three RW segments split along the standard RELRO / data / large-data lines.

### The code-section family

The compiler emitted code into eight distinct sections rather than one, a hot/cold/startup partition the linker uses to improve locality:

| Section | Size | Meaning | Confidence |
|---|---|---|---|
| `.text` | 299.9 MiB | Main code body | CERTAIN |
| `.text.startup` | 1.45 MiB | Static-init / constructor code (runs once) | HIGH |
| `.text.unlikely` | 427 KiB | Cold paths (error/abort/slow) | HIGH |
| `.text.hot` | 7.7 KiB | Profile-hot inner loops | HIGH |
| `google_init_cold` | 24.8 KiB | Cold init for the embedded allocator | MEDIUM |
| `google_malloc` | 18.2 KiB | Embedded malloc implementation (TCMalloc-style) | MEDIUM |
| `malloc_hook` | 2.2 KiB | Allocation hook trampolines | MEDIUM |
| `__lcxx_override` | 261 B | libc++ operator-new/delete overrides | MEDIUM |

> **QUIRK —** the allocator is not a dependency loaded at runtime; it is **welded into the object** as named sections (`google_malloc`, `malloc_hook`, `google_malloc_data`, `google_malloc_bss`, `google_init_cold`). A reimplementer who assumes `libtpu.so` calls the system `malloc` will mis-model its heap behavior; the binary overrides `operator new`/`delete` via `__lcxx_override` and routes through its own arena. This is the standard fingerprint of a statically-linked Google TCMalloc.

The full per-section walk — flags, alignment, the `.text.split` zero-length marker, the `.lrodata`/`.ldata`/`.lbss` large family, and how the address space is carved — is the subject of [ELF Anatomy](elf-anatomy.md).

---

## Function Population

The disassembler recovered **884,832 functions** from `libtpu.so`. Because the object is unstripped, the recovery is symbol-driven rather than purely heuristic, so the named fraction is far higher than a stripped binary of this size would yield.

| Metric | Value | Confidence |
|---|---|---|
| Total functions | 884,832 | CERTAIN |
| Carry a real symbol name | 881,784 (99.66 %) | CERTAIN |
| Anonymous (`sub_` only, no symbol) | 3,048 (0.34 %) | CERTAIN |
| C++ name successfully demangled | 822,847 (93.0 %) | HIGH |
| Thunks | 750 | HIGH |
| Leaf functions (no callees) | 326,941 (37 %) | HIGH |
| Median function size | 87 bytes | HIGH |
| 95th-percentile function size | 1,214 bytes | HIGH |

The population is dominated by a handful of C++ namespaces, which is the clearest single signal of what this binary *is*:

| Namespace prefix | Functions | What it is | Confidence |
|---|---|---|---|
| `mlir::RegisteredOperationName` | 19,171 | MLIR op registration (per-op machinery) | HIGH |
| `asic_sw::driver` | 18,834 | TPU device driver / low-level ASIC software | HIGH |
| `mlir::TF` | 9,125 | TensorFlow MLIR dialect | HIGH |
| `std::__u` | 5,449 | libc++ (`__u` inline namespace) | HIGH |
| `mlir::detail` | 5,389 | MLIR internals | HIGH |
| `tensorflow::(anon)` | 2,514 | TensorFlow translation-unit-local | MEDIUM |
| `platforms_deepsea::jellyfish` | 2,505 | TPU platform / codegen ("jellyfish") | MEDIUM |

> **NOTE —** the disassembler's per-function `addr_name` field is *always* the address form (`sub_E635524`); the real symbol lives in a separate `name` field. The named/anonymous split above is computed by comparing the two: a function is "anonymous" only when its `name` collapses back to the `sub_` form. Do not read a `sub_`-prefixed name as evidence the function is unnamed — for 99.66 % of this binary there is a real mangled symbol behind it.

> **CORRECTION (FOR-02) —** scratch counts of the function population floated several near-but-unequal totals (e.g. an 884,843 manifest figure). The authoritative count from the disassembler's own function sidecar is **884,832**; the small deltas are accounting differences between the manifest's expected count and the records actually materialized. Where this page needs one number, it uses 884,832.

The named/anonymous mechanics, the namespace concentration, and how 326 K leaf functions interact with the call-graph are developed in [Per-Gen Function Dispatcher](per-gen-function-dispatcher.md) (the per-generation entry fan-out) and the population framing carried forward into [RTTI / Vtable Census](rtti-vtable-census.md).

---

## Capsule Atlas — Major Embedded Regions

Beyond the standard ELF sections, the file carries several large *capsules* — self-contained embedded regions whose contents are a different kind of artifact than ordinary compiled code. The atlas below is coarse: it names each region, locates it, and points to the page that reverse-engineers its interior. It is a routing table, not an analysis.

| Capsule | Where | Nature | Owning page | Confidence |
|---|---|---|---|---|
| Code body | `.text` (`0xe63c000`–`0x21217484`) | 300 MiB of x86-64 | [ELF Anatomy](elf-anatomy.md) | CERTAIN |
| Large read-only data | `.lrodata` (108 MiB) | Constant pools past 32-bit reach | [ELF Anatomy](elf-anatomy.md) | CERTAIN |
| Protobuf descriptor pool | `protodesc_cold` (3.4 MiB) | Serialized `FileDescriptorProto`s | [Custom Sections](custom-sections.md) | HIGH |
| File-wrapper TOC | `filewrapper_toc` (488 B) | Index into embedded file blobs | [Custom Sections](custom-sections.md) | MEDIUM |
| upb extension registry | `linkarr_upb_AllExts` (1.2 KiB) | Link-array of upb extensions | [Custom Sections](custom-sections.md) | MEDIUM |
| Restartable-sequence metadata | `__rseq_cs`, `__rseq_cs_ptr_array` | Per-CPU `rseq` critical sections | [Custom Sections](custom-sections.md) | MEDIUM |
| Embedded allocator | `google_malloc` + data/bss | Statically-linked TCMalloc | [ELF Anatomy](elf-anatomy.md) | MEDIUM |
| Dispatch / vtable tables | `.rodata`, `.data.rel.ro` | C++ vtables, RTTI, jump tables | [Dispatch-Table Taxonomy](dispatch-table-taxonomy.md), [RTTI/Vtable Census](rtti-vtable-census.md) | HIGH |
| LLVM/MLIR manifest | `.rodata` string pools | Pass names, dialect/op tables | [LLVM/MLIR Manifest](llvm-mlir-manifest.md) | MEDIUM |
| Trailing compressed blob | end-of-file region | Suspected zstd payload | [Trailing zstd Blob](trailing-zstd-blob.md) | LOW |

> **GOTCHA —** the section header table ends *exactly* at EOF: `e_shoff` (781,687,720) + 52 × 64 bytes = 781,691,048 = file size. There is therefore **no naive trailing data after the section headers**. A plain ASCII/byte scan for the zstd frame magic (`28 b5 2f fd`) returned zero hits at this layer. Any "trailing zstd blob" is consequently either inside a section (e.g. an embedded payload within `.lrodata`/`.rodata`) or absent in this build — the claim is carried at **LOW** confidence and is owned, with the proper search, by [Trailing zstd Blob](trailing-zstd-blob.md). This page does not assert the blob exists; it routes the question.

---

## The Two-Binary Split

The wheel directory holds two ELF objects, not one. Both were disassembled; the analysis treats them as a pair because the runtime/driver split spans them.

| Object | Size | Functions | Needed libs | Role | Confidence |
|---|---|---|---|---|---|
| `libtpu.so` | 745.5 MiB | 884,832 | libm, libpthread, libdl, librt, libc, ld | PJRT plugin: compiler + runtime + driver | CERTAIN |
| `sdk.so` | 21.5 MiB | 94,732 | libstdc++, libgcc_s, libpthread, libm, libc, ld | SDK / support library | HIGH |

Two details distinguish them. `libtpu.so` links **no external `libstdc++`** — its C++ runtime is statically embedded (consistent with the welded allocator and the `std::__u` / libc++ namespace seen in the function population). `sdk.so`, by contrast, dynamically needs `libstdc++.so.6` and presents protobuf/absl-heavy namespaces (`google::protobuf`, `absl::lts_*`, `libtpu::sdk`, `tpu::monitoring`). The full provenance — why there are two objects, what each owns, and how symbols flow between them — is the subject of [Two-Binary Split](two-binary-split.md).

---

## Forensics Part Roadmap

This overview is the index to the Binary Forensics part. Each region or structural fact named above is reverse-engineered in depth on a sibling page; read them in roughly this order.

```text
overview (this page)  ── container shape, population, capsule routing
  ├─ elf-anatomy ─────── sections, segments, code-model, split families
  ├─ two-binary-split ── libtpu.so vs sdk.so, symbol flow
  ├─ static-init ─────── .init_array / .preinit_array / .text.startup order
  ├─ custom-sections ─── protodesc_cold, filewrapper_toc, __rseq_cs, upb
  ├─ embedded-library-atlas ── third-party libs welded in (absl, protobuf, MLIR…)
  ├─ dispatch-table-taxonomy ── jump/dispatch table families and their shapes
  ├─ rtti-vtable-census ─ C++ class hierarchy from RTTI + vtables
  ├─ polymorphic-entry-points ── the virtual entry surface of the plugin
  ├─ per-gen-function-dispatcher ── per-TPU-generation code selection
  ├─ llvm-mlir-manifest ── recovered pass / dialect / op inventory
  └─ trailing-zstd-blob ── the suspected compressed payload (LOW confidence)
```

- [ELF Anatomy](elf-anatomy.md) — the full section/segment walk; code model; the hot/cold/large split families.
- [Two-Binary Split](two-binary-split.md) — `libtpu.so` and `sdk.so` as a pair; what each owns and how they link.
- [Static Initialization](static-init.md) — `.preinit_array` / `.init_array` (5,792 B of pointers) and `.text.startup`; constructor ordering.
- [Custom Sections](custom-sections.md) — the named non-standard sections: protobuf descriptor pool, file-wrapper TOC, upb extension link-array, `rseq` metadata.
- [Embedded-Library Atlas](embedded-library-atlas.md) — the statically-linked third-party libraries (libc++, absl, protobuf, MLIR, TensorFlow) identified from the namespace census.
- [Dispatch-Table Taxonomy](dispatch-table-taxonomy.md) — the jump-table and dispatch-table families in `.rodata` / `.data.rel.ro` (33,016 switch constructs reported).
- [RTTI / Vtable Census](rtti-vtable-census.md) — reconstructing the C++ class hierarchy from RTTI records and vtable layout.
- [Polymorphic Entry Points](polymorphic-entry-points.md) — the virtual call surface that the runtime dispatches through.
- [Per-Gen Function Dispatcher](per-gen-function-dispatcher.md) — how the binary selects code paths per TPU generation.
- [LLVM/MLIR Manifest](llvm-mlir-manifest.md) — the recovered pass, dialect, and op inventory from the MLIR namespaces.
- [Trailing zstd Blob](trailing-zstd-blob.md) — the suspected compressed payload; this page only routes the question (see GOTCHA above).

---

## Cross-References

- [ELF Anatomy](elf-anatomy.md) — the section/segment detail this page summarizes.
- [Two-Binary Split](two-binary-split.md) — the `libtpu.so` / `sdk.so` pairing introduced here.
- [Custom Sections](custom-sections.md) — the named capsules in the atlas above.
- [Trailing zstd Blob](trailing-zstd-blob.md) — owner of the LOW-confidence trailing-payload claim.
- [Per-Gen Function Dispatcher](per-gen-function-dispatcher.md) — develops the function-population framing.
- [Lifecycle Overview](../lifecycle/overview.md) — how the statically-initialized plugin is driven once loaded.
