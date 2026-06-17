# ELF Anatomy

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`; `.git_hash` @`0xad41c0` = `"0b044f4ce917b633a70eb3d0bc460f34ac3da620"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB **DYN** x86-64, SONAME `libnrt.so.1`, RUNPATH `/opt/aws/neuron/lib`, NOT stripped, DWARF v4.
>
> **Part II — Binary Anatomy & Forensics** / DEEP reference · **Evidence grade:** every token below quoted from `readelf -h/-SW/-lW/-d/-n/-V/-rW/-p`, `readelf --dyn-syms`, `readelf --debug-dump=info` re-run against this exact binary. Sibling contrasts cite `libncfw.so` (same lib dir). · [back to forensics index](overview.md)

## Abstract

This page is the **ELF-level ground truth** that every citation elsewhere in the book rests on. When another page writes "`is_valid_neuron_instruction` @`0x2f2350`", that address is an *analysis VMA* only because of one fact established here — the runtime is laid out so **every loadable section's virtual address equals its file offset**. When the [SBOM](vendored-sbom.md) pins `protobuf 5.26.1` from a `.comment`/`.debug_str` token, this page is where those sections are shown to exist, how big they are, and which toolchain stamped them. The job here is to make the container legible: the section table, the symbol-version graph, the relocation model, and the toolchain census — each anchored to the exact `readelf` token that proves it.

Reading `libnrt.so` as an ELF is the familiar exercise of opening a large unstripped C++/Rust `DYN` object, with three Neuron-specific deviations from a textbook `gcc -shared` artifact. First, the linker emitted **non-standard `PROGBITS` sections** — `malloc_hook` (executable, @`0x7ce6e0`), `.git_hash`, `.nrt_brazil_version`, and `protodesc_cold` — that a generic ELF reader will not recognize but that carry the version provenance and a cold protobuf-descriptor pool. Second, the export surface is governed by a **three-node symbol-version graph** (`libnrt.so.1` BASE, `NRT_2.0.0`, `NRT_3.0.0`) so the public ABI is explicitly versioned, not flat. Third, the whole image is **identity-mapped** (VMA == file offset for all four `LOAD` segments), which is what makes byte-anchoring across the entire book sound. The disk object is 122,956,336 B (~117 MiB), but that mass is DWARF: `.debug_info` alone is `0x22f787d` (~36 MiB) and `.debug_str` is `0x1b63195` (~28 MiB); executable `.text` is only `0x790b19` (7,932,697 B ≈ 7.6 MiB).

The page is organized as five structural units, each a table plus its rationale: the **section layout** (§1), the **symbol-version nodes** (§2), the **`DT_NEEDED` / dynamic model** (§3), the **relocation histogram** (§4), and the **toolchain stamps** (§5). The version *numbers* of the vendored libraries are owned by the [SBOM](vendored-sbom.md) and are not duplicated here; this page owns the *structure* those tokens live in.

For reimplementation, the contract this page establishes is:

- **The identity-map invariant** — VMA == file offset for `.text`/`.rodata`/`.data`/`.data.rel.ro`/`.got` (every `LOAD` segment), so any `0x…` analysis address reads the same bytes whether interpreted as a VMA or a file offset. `.bss` is NOBITS (no file bytes). This is the load-bearing fact for every other page's addresses.
- **The export ABI graph** — 149 versioned exports across exactly two real nodes (`NRT_2.0.0`, `NRT_3.0.0`), the latter parented on the former, so a reimplementation's version script must reproduce *which* symbol lands in *which* node.
- **The dynamic linkage floor** — the 9 `DT_NEEDED` libraries and the symbol-version *needs* (`GLIBC_2.28`, `GLIBCXX_3.4.22`, `CXXABI_1.3.11`, `GCC_4.2.0`) that set the minimum runtime a host must provide.
- **The relocation model** — classic lazy-bind PLT (441 `JUMP_SLOT`), 7,295 `RELATIVE` (PIE base-relative), 250 `GLOB_DAT`, 357 absolute `R_X86_64_64`, 11 TLS `DTPMOD64`; **no IFUNC/IRELATIVE**.

| | |
|---|---|
| **Class / Type / Machine** | `ELF64` · `DYN (Shared object file)` · `Advanced Micro Devices X86-64` |
| **Entry point** | `0x0` (library — no `_start`; init via `DT_INIT`/`DT_INIT_ARRAY`) |
| **Program / section headers** | 10 `PHDR` · 46 `SHDR` (`shstrtab` index 45) |
| **BuildID[sha1]** | `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` (`.note.gnu.build-id` @`0x270`) |
| **SONAME / RUNPATH** | `libnrt.so.1` · `/opt/aws/neuron/lib` |
| **`.text` size** | `0x790b19` = 7,932,697 B @VMA/off `0x3dbc0` |
| **Symbol-version nodes** | BASE `libnrt.so.1`, `NRT_2.0.0` (141 exports), `NRT_3.0.0` (8 exports, parent `NRT_2.0.0`) |
| **`DT_NEEDED`** | 9 — `libgcc_s`/`libutil`/`librt`/`libpthread`/`libdl`/`libstdc++`/`libm`/`libc`/`ld-linux` |
| **Relocations** | 7,295 RELATIVE · 441 JUMP_SLOT · 357 R_X86_64_64 · 250 GLOB_DAT · 11 DTPMOD64 |
| **Toolchains** (`.comment`) | GCC 14.2.1, GCC 7.3.1, GCC 8.5.0, clang 21.1.0-rc2, rustc 1.91.1 |
| **DWARF** | v4, present in `.debug_{info,abbrev,line,str,loc,ranges,macro,aranges,frame}`; `.symtab` 25,623 entries |

> **NOTE — the VMA==file-offset rule, stated precisely for *this* binary.** `readelf -lW` shows all four `LOAD` segments with `Offset == VirtAddr == PhysAddr` (`0x0`, `0x3c000`, `0x7cf000`, `0xbeeaa0`), so **every PROGBITS section — including `.data` (@`0xc07e00`==off `0xc07e00`) — has VMA == file offset.** The only non-file-backed section is `.bss` (NOBITS, VMA `0xc37c40`, no file bytes); its section-header nominal offset `0xc37c20` is alignment bookkeeping, not a real file location, and the 0x20 gap is `.bss`'s 64-byte alignment, not an offset delta.
>
> **CORRECTION (ELF-ANATOMY vs F-STRINGS/overview) —** sibling pages and the source-string cell state "`.data`/`.bss` delta = `0x400000`". That figure is **not** true for `libnrt.so` — it is carried over from a *different* runtime image (the libtpu/Kaena-profiler sibling, where `.data` is not identity-mapped). For `libnrt.so.2.31.24.0` the delta is **zero** for all PROGBITS sections; `readelf -lW` proves the identity map. Cite the `0x400000` delta only against the binary it was measured on, never against `libnrt.so`.

---

## 1. Section layout

`libnrt.so` carries 46 sections. The reimplementation-relevant subset is below: the loadable code/data sections, the dynamic-linking metadata, the four **non-standard Neuron sections** a generic reader will not recognize, and the DWARF block (summarized, not enumerated). `VMA==off?` is the per-section verdict of the identity-map rule; sizes are the verbatim `Size` column from `readelf -SW`.

| Section | VMA | Off | Size | Type/Flg | Role | VMA==off? | Conf |
|---|---|---|---|---|---|---|---|
| `.note.gnu.build-id` | `0x270` | `0x270` | `0x24` | NOTE `A` | the `8bb57aba…` sha1 build-id (`readelf -n`) | yes | CERTAIN |
| `.gnu.hash` | `0x298` | `0x298` | `0x4e8` | GNU_HASH `A` | dynamic-symbol lookup (no SysV `.hash`) | yes | CERTAIN |
| `.dynsym` / `.dynstr` | `0x780` / `0x4f38` | = | `0x47b8` / `0x4d00` | DYNSYM/STRTAB `A` | 765 dynamic symbols + name pool | yes | CERTAIN |
| `.gnu.version` | `0x9c38` | `0x9c38` | `0x5fa` | VERSYM `A` | per-dynsym version index (2 B each) | yes | CERTAIN |
| `.gnu.version_d` | `0xa238` | `0xa238` | `0x5c` | VERDEF `A` | **3 version-def nodes** (§2) | yes | CERTAIN |
| `.gnu.version_r` | `0xa298` | `0xa298` | `0x340` | VERNEED `A` | **7 version-need files** (§3) | yes | CERTAIN |
| `.rela.dyn` | `0xa5d8` | `0xa5d8` | `0x2e5d8` | RELA `A` | RELATIVE/GLOB_DAT/64/DTPMOD relocs (§4) | yes | CERTAIN |
| `.rela.plt` | `0x38bb0` | `0x38bb0` | `0x2958` | RELA `AI` | **441 JUMP_SLOT** (`0x2958/24`) (§4) | yes | CERTAIN |
| `.init` | `0x3c000` | `0x3c000` | `0x1b` | PROGBITS `AX` | `_init` (`DT_INIT`) — [CRT/PLT](crt-plt.md) | yes | CERTAIN |
| `.plt` | `0x3c020` | `0x3c020` | `0x1ba0` | PROGBITS `AX` | PLT0 + 441 stubs (`0x1ba0/16 = 442`) | yes | CERTAIN |
| `.text` | `0x3dbc0` | `0x3dbc0` | `0x790b19` | PROGBITS `AX` | all code (7,932,697 B) | yes | CERTAIN |
| `malloc_hook` | `0x7ce6e0` | `0x7ce6e0` | `0x206` | **PROGBITS `AX`** | **non-standard** executable section (allocator-hook trampolines) | yes | HIGH |
| `.fini` | `0x7ce8e8` | `0x7ce8e8` | `0xd` | PROGBITS `AX` | `_fini` (`DT_FINI`) | yes | CERTAIN |
| `.rodata` | `0x7cf000` | `0x7cf000` | `0x3051b3` | PROGBITS `A` | ~3.0 MB literal pool — [String Domain](string-domain.md) | yes | CERTAIN |
| `.git_hash` | `0xad41c0` | `0xad41c0` | `0x29` | **PROGBITS `A`** | **non-standard** — full git hash `"0b044f4ce917b633…"` (41 B) | yes | CERTAIN |
| `.nrt_brazil_version` | `0xad41f0` | `0xad41f0` | `0xa` | **PROGBITS `A`** | **non-standard** — `"2.31.24.0"` (Brazil pkg version) | yes | CERTAIN |
| `protodesc_cold` | `0xad4200` | `0xad4200` | `0x64a8` | **PROGBITS `A`** | **non-standard** — cold protobuf FileDescriptor blob pool | yes | HIGH |
| `.eh_frame_hdr` / `.eh_frame` | `0xada6a8` / `0xafa708` | = | `0x2005c` / `0xd69b8` | PROGBITS `A` | CFI unwind tables (`.eh_frame` ~879 KiB) | yes | CERTAIN |
| `.gcc_except_table` | `0xbd10c0` | `0xbd10c0` | `0x1cbae` | PROGBITS `A` | LSDA — C++/Rust landing pads | yes | CERTAIN |
| `.tdata` / `.tbss` | `0xbeeaa0` / `0xbf2b08` | `0xbeeaa0` / — | `0x4068` / `0xbc` | PROGBITS/NOBITS `WAT` | TLS init image / TLS bss (`PT_TLS`) | `.tdata` yes | CERTAIN |
| `.init_array` / `.fini_array` | `0xbf2b08` / `0xbf2d70` | = | `0x268` / `0x10` | INIT/FINI_ARRAY `WA` | 77 ctors / 2 dtors — [Static-Init](static-init.md) | yes | CERTAIN |
| `.data.rel.ro` | `0xbf2d80` | `0xbf2d80` | `0x127c0` | PROGBITS `WA` | relro: vtables, const pointer tables | yes | CERTAIN |
| `.dynamic` | `0xc05540` | `0xc05540` | `0x280` | DYNAMIC `WA` | the dynamic array (§3) | yes | CERTAIN |
| `.got` / `.got.plt` | `0xc057c0` / `0xc06fe8` | = | `0x1810` / `0xde0` | PROGBITS `WA` | GOT (GLOB_DAT) / PLT GOT (3 reserved + 441) | yes | CERTAIN |
| `.data` | `0xc07e00` | `0xc07e00` | `0x2fe20` | PROGBITS `WA` | writable globals | **yes** | CERTAIN |
| `.bss` | `0xc37c40` | (NOBITS) | `0x7c6c0` | NOBITS `WA` | zero-init globals — **no file bytes** | N/A | CERTAIN |
| `.comment` | `0x0` | `0xc37c20` | `0xce` | PROGBITS `MS` | toolchain stamps (§5) — non-alloc | non-alloc | CERTAIN |
| `.debug_*` (9 sections) | `0x0` | `0xc3b728`+ | `~0x6.4M f` | PROGBITS | DWARF v4 — `.debug_info` `0x22f787d`, `.debug_str` `0x1b63195` | non-alloc | CERTAIN |
| `.symtab` / `.strtab` | `0x0` | `0x730c990`+ | `0x96228` | SYMTAB/STRTAB | 25,623 static symbols (NOT stripped) | non-alloc | CERTAIN |

> **QUIRK — the four non-standard `PROGBITS` sections are a Neuron build convention, not stock `gcc -shared`.** A generic linker emits none of `malloc_hook`, `.git_hash`, `.nrt_brazil_version`, `protodesc_cold`. They are placed by linker script / `__attribute__((section(...)))`: `.git_hash` (@`0xad41c0`, 41 B) and `.nrt_brazil_version` (@`0xad41f0`, 10 B) are the *authoritative* version provenance read by `nrt_infodump` (the package-name short hash `0b044f4ce` is the prefix of the full `.git_hash`). `malloc_hook` is `AX` (executable) — it holds allocator-interpose trampolines, not data. `protodesc_cold` is the cold-split half of the protobuf descriptor tables (the hot half lives in `.rodata`). A reimplementation that drops these sections still runs, but `nrt_infodump` version reporting and the allocator hook break.

> **NOTE — DWARF dominates the on-disk size; the *logic* is 7.6 MB.** Of the 117 MiB on disk, `.debug_info` (~36 MiB) + `.debug_str` (~28 MiB) + `.debug_loc` (~24 MiB) + `.debug_line` (~6.5 MiB) + `.debug_ranges` (~5.7 MiB) are ~100 MiB of DWARF v4. A reimplementer sizing the runtime should reason about the `0x790b19` `.text` budget, not the disk size. DWARF presence is what makes every other page's symbolization possible (`.symtab` carries 25,623 entries; the binary is explicitly `not stripped`).

---

## 2. Symbol-version nodes

The export ABI is governed by a **3-entry `.gnu.version_d` graph** (`readelf -V`, `VERDEFNUM 3`). Index 1 is the BASE filename node (`libnrt.so.1`, the SONAME, defining nothing); indexes 2 and 3 are the two real ABI levels. `NRT_3.0.0` declares `Cnt: 2` with `Parent 1: NRT_2.0.0` — it **inherits** the v2 node, so a v3 client resolves v2 symbols transparently. The export counts are from `readelf --dyn-syms` filtered on `@@<node>`.

| Node | Index | Flags / Parent | Exports | What lands here | Conf |
|---|---|---|---|---|---|
| `libnrt.so.1` | 1 | `BASE` | 0 | the SONAME filename node — names the library, defines no symbol | CERTAIN |
| `NRT_2.0.0` | 2 | none | **141** | the bulk public C API (`nrt_*`) + 4 LOCAL `std::` template instantiations versioned into v2 | CERTAIN |
| `NRT_3.0.0` | 3 | parent `NRT_2.0.0` | **8** | the async `nrta_*` v3 surface only | CERTAIN |

The 8 `NRT_3.0.0` exports are exactly the async ("`nrta_`") schedule/transfer API — `readelf -W --dyn-syms | grep '@@NRT_3.0.0'`:

```text
nrta_cc_prepare        @0x7c980 (3562 B)   nrta_cc_schedule       @0x7bd80 (1743 B)
nrta_execute_schedule  @0x7d8c0 (1605 B)   nrta_get_sequence      @0x7c450 ( 709 B)
nrta_is_completed      @0x7c720 ( 599 B)   nrta_tensor_copy       @0x7df10 (1315 B)
nrta_tensor_read       @0x7e440 (1315 B)   nrta_tensor_write      @0x7bb00 ( 639 B)
```

A representative slice of the 141 `@@NRT_2.0.0` GLOBAL exports — the public façade consumers link against — `nrt_async_sendrecv_*`, `nrt_all_gather`, `nrt_barrier`, `nrt_build_global_comm`, `nrt_cc_global_comm_init`, `nrt_close`, `nrt_allocate_tensor_set`, … (full list = the export surface owned by the ABI-equivalence pages).

> **QUIRK — four `LOCAL` `std::` instantiations carry a public version node.** `readelf --dyn-syms` shows entries like `_ZNSt…@@NRT_2.0.0` with binding `LOCAL` (@`0xc6a60`, `0xc6dc0`, `0xc6a90`, `0xc6bd0`). A symbol that is both `LOCAL` and version-tagged is unusual: the version script's global pattern swept these template bodies into the `NRT_2.0.0` node even though they are not part of the intended API. They are harmless (LOCAL = not externally bindable) but inflate a naive "count of `@@NRT_2.0.0` entries" from 141 to 145; the **149 figure** is the GLOBAL/WEAK export total (141 + 8). Ground any export-count claim on `GLOBAL|WEAK`, not on a raw `@@NRT_2.0.0` grep.

> **NOTE — the version graph is the ABI contract a port must reproduce.** Putting the async `nrta_*` calls behind `NRT_3.0.0` (parent `NRT_2.0.0`) lets the runtime add the async surface without breaking a binary linked only against v2: the parent edge means `NRT_3.0.0` is a strict superset. A reimplementation's linker version script must place the same 8 `nrta_*` symbols in v3 and everything else in v2, or symbol resolution against an existing client breaks. The graph is emitted from `.gnu.version_d` (`VERDEF` @`0xa238`) and indexed per-symbol by `.gnu.version` (`VERSYM` @`0x9c38`).

---

## 3. Dynamic linkage — `DT_NEEDED` and version needs

`readelf -d` lists **9 `DT_NEEDED`** entries and a `RUNPATH`. The NEEDED set is the floor a host must provide; the absence of protobuf / Abseil / simdjson / zlib / libarchive / the Rust runtime from this list is the structural proof that those are **statically vendored** (the [SBOM](vendored-sbom.md) pins their versions). Only `libstdc++.so.6` is a *dynamic* C++ dependency.

| `DT_NEEDED` | Role | Min version need (`readelf -V` `.gnu.version_r`) | Conf |
|---|---|---|---|
| `libgcc_s.so.1` | unwinder / compiler runtime | `GCC_3.0`, `GCC_3.3`, `GCC_4.2.0` | CERTAIN |
| `libutil.so.1` | (legacy `openpty`-family; thin) | — | CERTAIN |
| `librt.so.1` | POSIX realtime (timers/shm) | — | CERTAIN |
| `libpthread.so.0` | threads | `GLIBC_2.2.5`, `2.3.2`, `2.12` | CERTAIN |
| `libdl.so.2` | `dlopen`/`dlsym` (loads `libncfw`/`libnrtucode_extisa`) | `GLIBC_2.2.5` | CERTAIN |
| `libstdc++.so.6` | **dynamic** C++ STL/ABI | `GLIBCXX_3.4.22`, `CXXABI_1.3.11` (floor) | CERTAIN |
| `libm.so.6` | libm | `GLIBC_2.27`, `2.2.5` | CERTAIN |
| `libc.so.6` | glibc | `GLIBC_2.28` (max), down to `2.2.5` | CERTAIN |
| `ld-linux-x86-64.so.2` | the dynamic loader | `GLIBC_2.3` | CERTAIN |

Selected dynamic tags (`readelf -d`) that fix the linking model:

```text
INIT        0x3c000      FINI        0x7ce8e8       (DT_INIT / DT_FINI — see CRT/PLT)
INIT_ARRAY  0xbf2b08     INIT_ARRAYSZ 616           (77 ctors, 8 B each — see Static-Init)
FINI_ARRAY  0xbf2d70     FINI_ARRAYSZ  16           (2 dtors)
GNU_HASH    0x298        STRTAB 0x4f38  SYMTAB 0x780 (GNU hash only; no SysV .hash)
VERDEF      0xa238       VERDEFNUM 3                 (the 3 version-def nodes, §2)
VERNEED     0xa298       VERNEEDNUM 7                (7 version-need files, this §)
VERSYM      0x9c38       PLTREL RELA  PLTRELSZ 10584 (= 441×24 JUMP_SLOT — §4)
RUNPATH     /opt/aws/neuron/lib                      (DT_RUNPATH, not DT_RPATH)
RELACOUNT   7295                                     (= the R_X86_64_RELATIVE count, §4)
```

The **ABI floor** is the highest version-need across the 7 `.gnu.version_r` files: **`GLIBC_2.28`** (libc), **`GLIBCXX_3.4.22`** + **`CXXABI_1.3.11`** (libstdc++), **`GCC_4.2.0`** (libgcc_s). A host older than glibc 2.28 / a libstdc++ predating `GLIBCXX_3.4.22` cannot load this object. The `GLIBCXX`/`CXXABI` floor is owned in detail by the [Symbol-Version Manifest](../appendix/symbol-versions.md); it is reproduced here only as the link-time minimum.

> **NOTE — `DT_RUNPATH`, not `DT_RPATH`.** The runtime sets `RUNPATH /opt/aws/neuron/lib` (tag `0x1d`), which is searched *after* `LD_LIBRARY_PATH` (unlike the legacy `RPATH`, searched before). This is how the in-package `libstdc++` shim and the dlopen'd `libncfw.so` resolve without an absolute path. `RELACOUNT 7295` is a loader optimization tag: it tells `ld.so` the first 7,295 `.rela.dyn` entries are all `R_X86_64_RELATIVE` and can be applied in a tight loop without per-entry symbol lookup — and it matches the §4 RELATIVE count exactly.

---

## 4. Relocation model

The relocation histogram (`readelf -rW | grep -oE 'R_X86_64_[A-Z0-9_]+' | sort | uniq -c`) over `.rela.dyn` + `.rela.plt` is small and entirely classic — there are **no IFUNC/IRELATIVE resolvers** (0 `R_X86_64_IRELATIVE`), so the runtime does no CPU-dispatch via the loader; simdjson's per-µarch selection (per the [SBOM](vendored-sbom.md)) is done in-code, not via IFUNC.

| Type | Count | Section | Meaning | Conf |
|---|---|---|---|---|
| `R_X86_64_RELATIVE` | **7,295** | `.rela.dyn` | base-relative fixup `*(B+A)`; PIE load-bias rebase of internal pointers (matches `RELACOUNT`) | CERTAIN |
| `R_X86_64_JUMP_SLOT` | **441** | `.rela.plt` | lazy-bind PLT GOT slot; 1:1 with the 441 PLT stubs after PLT0 | CERTAIN |
| `R_X86_64_64` | **357** | `.rela.dyn` | absolute 64-bit `S+A`; symbol-bearing data pointers (vtable/typeinfo refs into externals) | CERTAIN |
| `R_X86_64_GLOB_DAT` | **250** | `.rela.dyn` | GOT-entry bind `S`; non-PLT external data/function pointers (`.got` @`0xc057c0`) | CERTAIN |
| `R_X86_64_DTPMOD64` | **11** | `.rela.dyn` | TLS module-id for general-dynamic `__tls_get_addr` access | CERTAIN |

The PLT is **classic lazy-binding**: `.plt` @`0x3c020` is `0x1ba0/16 = 442` entries = PLT0 + 441 stubs, exactly matching the 441 `JUMP_SLOT` relocs and `PLTRELSZ 10584 = 441×24`. There is no `.plt.sec` / CET second-PLT split. The mechanics (PLT0 → `_dl_runtime_resolve`, the 3 reserved `.got.plt` slots) are owned by [CRT / PLT](crt-plt.md); this page owns only the *counts* and the *model*.

> **NOTE — the relocation profile is that of a PIE-rebased monolith.** 7,295 RELATIVE (95% of `.rela.dyn`) is the signature of a position-independent object that internally references its own 117 MiB of vtables, descriptor tables, and string pointers: each is a `B+A` fixup applied once at load. The 250 GLOB_DAT + 357 R_X86_64_64 are the comparatively small external-pointer surface — consistent with a binary that statically vendors almost everything and imports only the 9 `DT_NEEDED` libraries' symbols. A reimplementation built `-fPIC -shared` with the same vendored set will produce a relocation histogram of the same *shape* (RELATIVE-dominated, IFUNC-free), though the exact counts depend on how many internal pointers the compiler materializes.

---

## 5. Toolchain stamps

The `.comment` section (`readelf -p .comment`, `MS` merge-strings, non-alloc) carries **five distinct producer stamps** — three GCC versions, rustc, and clang — confirming `libnrt.so` is a tri-language (C / C++ / Rust) artifact linked from objects built by four front-ends. Each stamp is quoted verbatim; the *what-it-built* attribution is corroborated by the `DW_AT_producer` census from `readelf --debug-dump=info` (counts = distinct producers across the 331 compile units).

| `.comment` token (verbatim) | Producer | What it built | DWARF `DW_AT_producer` corroboration | Conf |
|---|---|---|---|---|
| `GCC: (GNU) 14.2.1 20250110 (Red Hat 14.2.1-10)` | GCC 14.2.1 | C++17 TUs (KaenaRuntime C++, libstdc++ header-only instantiations) | **91** CUs `GNU C++17 14.2.1 … -ggdb3 -O2 -fPIC` | CERTAIN |
| `GCC: (GNU) 7.3.1 20180712 (Red Hat 7.3.1-17)` | GCC 7.3.1 | legacy C TUs | within the **119** `GNU C11` + **13** `GNU C99` CUs | HIGH |
| `GCC: (GNU) 8.5.0 20210514 (Red Hat 8.5.0-28)` | GCC 8.5.0 | legacy C TUs | within the `GNU C11`/`C99` CU set | HIGH |
| `rustc version 1.91.1 (ed61e7d7e 2025-11-07)` | rustc 1.91.1 | Rust `std`/`core`/`alloc` + crates + `neuron_rustime` | **13** CUs `clang LLVM (rustc version 1.91.1 (ed61e7d7e 2025-11-07))` | CERTAIN |
| `clang version 21.1.0-rc2` | clang 21.1.0-rc2 | some Neuron first-party C/C++ TUs | **1** CU `clang version 21.1.0-rc2` | HIGH |

The GCC 14.2.1 build flags are pinned by the DWARF producer string: `-mtune=generic -march=x86-64 -ggdb3 -O2 -fPIC` — generic x86-64 (no AVX baseline), full `-ggdb3` debug, `-O2`, position-independent. This is why the binary ships complete DWARF v4 and why the relocation profile is PIE-RELATIVE-dominated (§4).

> **QUIRK — rustc reports as `clang LLVM` in DWARF, as `rustc` in `.comment`.** The Rust CUs carry `DW_AT_producer = "clang LLVM (rustc version 1.91.1 (ed61e7d7e 2025-11-07))"` — because rustc's codegen backend *is* LLVM, the DWARF producer is stamped by the LLVM emitter while the version string is rustc's. The `.comment` stamp is the cleaner `rustc version 1.91.1 (ed61e7d7e 2025-11-07)`. Both encode the **same** commit `ed61e7d7e`, which also appears as the sysroot path `/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb` (the [SBOM](vendored-sbom.md) §3.2 correction rejects an earlier "1.89.0" misread on exactly this triple-anchored evidence). The three GCC stamps (14.2.1 / 7.3.1 / 8.5.0) are not a mistake either: the C++ mass is GCC 14, while two legacy C subtrees were compiled with the older 7.3.1 / 8.5.0 toolchains and linked in.

> **NOTE — the version *numbers* of vendored libraries are NOT on this page.** The `.comment`/`.debug_str` tokens that pin protobuf 5.26.1, Abseil LTS 20230802, simdjson 0.9.0, libarchive 3.8.0dev, zlib 1.3.1, and the 12 Rust crates are owned by the [Vendored-Library SBOM](vendored-sbom.md). This page establishes only that the `.comment` (toolchain) and `.debug_str` (source-path) sections *exist*, their geometry, and the *toolchain* stamps. The SBOM reads those same sections for the library pins.

---

## Sibling-binary contrast — `libncfw.so`

The same lib directory ships `libncfw.so` (615,640 B), the embedded-firmware image that `libnrt.so` `dlopen`s. It is a useful contrast: also `ELF64 DYN x86-64`, but with **none** of `libnrt`'s structure — no `NRT_*` version-def nodes (empty `.gnu.version_d`), a self-versioning SONAME `libncfw.so.2.31.1.0.cf13a49f` (version + hash *in the SONAME*, not in `.git_hash`/`.nrt_brazil_version` sections), a different build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, and a tiny relocation set (3 RELATIVE / 4 JUMP_SLOT / 4 GLOB_DAT). It is a thin loader-image, not a versioned ABI surface — which is exactly why `libnrt` (the public runtime) carries the full version graph and `libncfw` does not.

---

## Cross-References

- [Overview and Heavy-Frame Census](overview.md) — the first-party/vendored byte split that sits inside the `.text` this page measures; the section-table summary it links here for detail
- [Vendored-Library SBOM](vendored-sbom.md) — owns the *version numbers* read from the `.comment`/`.debug_str` sections whose *structure* this page establishes; the protobuf/Rust correction triples
- [CRT / PLT / Loader Surface](crt-plt.md) — the `_init`/`_fini`, PLT0 lazy-resolve mechanics, and the 441 PLT stubs whose `JUMP_SLOT` count this page pins
- [Symbol-Version Manifest](../appendix/symbol-versions.md) — the `GLIBCXX_3.4.22` / `CXXABI_1.3.11` / `GLIBC_2.28` ABI-floor needs this page summarizes as the link-time minimum
