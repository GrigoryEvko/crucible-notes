# Binary Layout

> *All addresses, offsets, and section sizes on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, git `0b044f4ce`, **BuildID[sha1] `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`**), with the four sibling binaries pinned by their own build-ids in §4. ELF64 LSB `DYN` x86-64, **not stripped**, **DWARF v4**. The disk image is **122,956,336 B (~117 MiB)**. Every value below is the verbatim `readelf -SW` / `readelf -lW` column for the exact file; other versions will differ.*

## Abstract

This is the **byte-level ground** that every address-bearing claim elsewhere in the book stands on. When a runtime page writes "`kaena_khal` @`0xcaeb80` (.bss)" or "`ucode_func_symbols` @`0xbf2ea0` (.data.rel.ro)", the address is usable as both a virtual address and a file offset only because of one fact established here: for `libnrt.so` **every loadable section's virtual address equals its file offset** — the image is identity-mapped across all four `PT_LOAD` segments, and the writable `LOAD` segment's `Offset == VirtAddr == 0xbeeaa0` (delta **zero**). `readelf -lW` proves it; `readelf -SW` corroborates section by section. The page leads with the libnrt section table, states the delta-zero invariant as a NOTE with the in-place correction it requires, maps where the major data tables physically live, breaks down the ~117 MiB budget, and closes with a cross-binary section profile of the other four images.

The mass of `libnrt.so` is **debug information, not logic**. Executable `.text` is `0x790b19` = 7,932,697 B (~7.6 MiB) and the whole writable image (`.data.rel.ro` + `.got` + `.data` + `.bss`) is under ~0.9 MiB; the remaining ~108 MiB is DWARF v4 (`.debug_info`, `.debug_str`, `.debug_loc`, `.debug_line`, `.debug_ranges`) plus `.symtab`/`.strtab`. A reimplementer should size the runtime against the `0x790b19` `.text` budget, never the disk size. The structural anatomy — symbol-version graph, relocation model, toolchain census — is owned by [ELF Anatomy](../forensics/elf-anatomy.md); this page owns the **geometry** (which section sits at which VMA, how big, identity-mapped or not) and the **table-location map** (which forensic table lives in which section band).

| | |
|---|---|
| **File / SONAME** | `libnrt.so.2.31.24.0` → `libnrt.so.1` · `DYN` x86-64, not stripped |
| **BuildID[sha1]** | `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` (`.note.gnu.build-id` @`0x270`) |
| **Disk size** | `122,956,336` B (~117 MiB) — ~108 MiB of it DWARF v4 |
| **`.text`** | VMA/off `0x3dbc0`, size `0x790b19` (7,932,697 B) — all code |
| **`.rodata`** | VMA/off `0x7cf000`, size `0x3051b3` (~3.0 MiB) — literal/const pool |
| **`.data.rel.ro`** | VMA/off `0xbf2d80`, size `0x127c0` — RELRO ptr tables + C++ vtables/RTTI |
| **`.data`** | VMA/off `0xc07e00`, size `0x2fe20` — writable globals + `_table_` statics |
| **`.bss`** | VMA `0xc37c40`, size `0x7c6c0` — **NOBITS** (no file bytes) |
| **Identity map** | RW `LOAD` `Offset == VirtAddr == 0xbeeaa0` → **delta zero, all sections** |

---

## 1. libnrt.so section geometry

The table below is the reimplementation-relevant subset of the 46-section table, quoted from `readelf -SW`. `VMA == off?` is the per-section verdict of the identity-map rule; the `.bss` row carries no file bytes (`NOBITS`), so its nominal section-header offset (`0xc37c20`) is alignment bookkeeping, not a real file location.

| Section | VMA | File offset | Size | Flags | Contents | Confidence |
|---|---|---|---|---|---|---|
| `.init_array` | `0xbf2b08` | `0xbf2b08` | `0x268` | `WA` | 77 C++/Rust ctors (`0x268/8`), run via `DT_INIT_ARRAY` | CERTAIN |
| `.fini_array` | `0xbf2d70` | `0xbf2d70` | `0x10` | `WA` | 2 dtors | CERTAIN |
| `.data.rel.ro` | `0xbf2d80` | `0xbf2d80` | `0x127c0` | `WA` (RELRO) | RELRO const-ptr tables: C++ vtables/RTTI, per-arch fn-ptr seed arrays, `ucode_func_symbols` | CERTAIN |
| `.got` | `0xc057c0` | `0xc057c0` | `0x1810` | `WA` | GOT (`GLOB_DAT` slots) | CERTAIN |
| `.got.plt` | `0xc06fe8` | `0xc06fe8` | `0xde0` | `WA` | PLT GOT (3 reserved + 441 stubs) | CERTAIN |
| `.data` | `0xc07e00` | `0xc07e00` | `0x2fe20` | `WA` | initialized writable globals; proto `descriptor_table_*` objects; const dispatch/alloc tables | CERTAIN |
| `.bss` | `0xc37c40` | (NOBITS) | `0x7c6c0` | `WA` | zero-init singletons, large per-engine state arrays, protobuf `_table_` parse statics | CERTAIN |
| `.text` | `0x3dbc0` | `0x3dbc0` | `0x790b19` | `AX` | all executable code (7,932,697 B); compiler switch/jump tables are `.text`-embedded | CERTAIN |
| `.rodata` | `0x7cf000` | `0x7cf000` | `0x3051b3` | `A` | literals, `CSWTCH.*` value arrays, `data_type_sizes`, `sync_events`, NQ-id maps | CERTAIN |

> **NOTE — VMA == file offset holds for ALL libnrt.so sections (delta zero).** `readelf -lW` shows the writable `LOAD` segment as `LOAD 0xbeeaa0 0x0000000000beeaa0 0x0000000000beeaa0 0x049180 0x0c5860 RW 0x1000` — `Offset == VirtAddr == PhysAddr == 0xbeeaa0`, delta **zero**. All four `LOAD` segments are identity-mapped (`Offset == VirtAddr` at `0x0`, `0x3c000`, `0x7cf000`, `0xbeeaa0`), so `.text`/`.rodata`/`.data.rel.ro`/`.got`/`.data` all read the same bytes whether an address is treated as a VMA or a file offset. `.bss` is `NOBITS` — it occupies no file bytes; its `0xc37c20` header offset is the 64-byte-alignment slot before `.comment`, and the `0x20` gap to its `0xc37c40` VMA is `.bss` alignment, not an offset delta.

> **CORRECTION (BINARY-LAYOUT vs source notes) —** an earlier draft imported a `+0x400000` `.data` VMA→file-offset delta into the libnrt section geometry. That figure is **false for `libnrt.so`** — it is a fact about a *different* image (the libtpu / Kaena-profiler binary, where `.data` is not identity-mapped). For `libnrt.so.2.31.24.0` the delta is **zero** for every loadable section, proven by the `readelf -lW` RW-segment line above and by `readelf -SW` showing `.data` at `Address 0xc07e00 / Off c07e00` — identical. Read `.data`/`.data.rel.ro` globals at their VMA directly. This matches the in-place corrections already carried on [runtime/overview](../runtime/overview.md) and [runtime/arch-csr-offsets](../runtime/arch-csr-offsets.md).

---

## 2. Where the major data tables live

The forensic catalogs ([Dispatch Tables](../forensics/dispatch-tables.md), [Globals Atlas](../forensics/globals-atlas.md)) enumerate the individual tables; this map answers the geometry question — **which section band a given table class occupies**, so a reimplementer knows whether a structure is static-in-file (`.data.rel.ro`/`.data`/`.rodata`) or runtime-filled (`.bss`). The single most important consequence: the named C dispatch tables (`kaena_khal`, `tdrv_arch_ops`, the `csr_regions_v*`) live in `.bss` and are **empty in the file** — their contents are reconstructable only from the `register_*`/`*_init` store sequences, never from a static hexdump.

| Table class | Section | VMA band | Role | Confidence |
|---|---|---|---|---|
| Compiler switch / jump tables | `.text` | within `0x3dbc0`..`0x7ce6d9` | opcode→handler & per-arch decode switches (4× 256-case ISA-opcode tables, status/name stringifiers); jump-target arrays inlined in `.text` | HIGH |
| C++ vtables + RTTI (`_ZTV`/`_ZTI`/`_ZTS`) | `.data.rel.ro` | `0xbf2d80`..`0xc05540` | RELRO virtual tables and typeinfo for the vendored protobuf/Abseil/Rust classes and first-party C++ types | HIGH |
| Per-arch fn-ptr seed arrays | `.data.rel.ro` | `0xbf3108` (mariana) · `0xbf32c8` (cayman) · `0xbf3488` (sunda) · `0xbf3698`/`0xbf3778` (tdrv seed) | source pointer arrays copied into the `.bss` op-tables by `tdrv_arch_register_*` / `kaena_khal_register_funcs_v*` | HIGH |
| `ucode_func_symbols` dlsym table | `.data.rel.ro` | `0xbf2ea0` (size `0x1e0`) | 30 `{void**, const char*}` bindings; loader `dlsym`s each name from `libnrtucode_extisa.so` into a `.bss` slot block | HIGH |
| Const dispatch / DMA alloc tables | `.data` | `0xc08900`+ (`nrt_init_state_strings` @`0xc08900`; `v2/v3_queue_bundle_alloc_table` @`0xc08bc0`/`0xc08d40`) | static const lookup tables that survive RELRO (string ptr arrays, queue-bundle alloc maps) | MED |
| protobuf `descriptor_table_*` objects | `.data` | `0xc0b6a0`+ (`…_ntff_2eproto` @`0xc0b6a0`) | FileDescriptor table objects for the ntff / neuron_trace / google internal protos | HIGH |
| ntff `_table_` parse statics | `.bss` | `0xc37c40`..`0xcb4300` | 69 `TcParseTableBase` parse tables (38 ntff) + `file_level_metadata_ntff_2eproto` @`0xcafe00`; zero-filled, populated at ctor time | HIGH |
| Named C HAL dispatch tables | `.bss` | `kaena_khal` @`0xcaeb80` (`0x7a8`) · `tdrv_arch_ops` @`0xc97180` (`0x1e8`) | per-arch device-HAL vtables — **empty in file**, filled by `register_*`/`*_ops_init` at bring-up | HIGH |
| Writable singletons + state arrays | `.bss` | `nrt_config` @`0xc5c480` · `csr_regions_v2/3/4` @`0xc97380`/`0xc97680`/`0xc9f480` · big xu arrays `0xc37c60`..`0xc88ae0` | runtime config, CSR region tables, per-engine execution-unit/worker state | HIGH |

> **GOTCHA — the device-HAL dispatch tables are `.bss`, not `.data`.** A reimplementer who tries to read `kaena_khal` (@`0xcaeb80`) or `tdrv_arch_ops` (@`0xc97180`) from a static file dump gets zeros. These are `NOBITS` slots installed at device bring-up — `kaena_khal_register_funcs_v{2,3,4}` and `tdrv_arch_register_{sunda,cayman,mariana}` copy pointers from the `.data.rel.ro` seed arrays (`0xbf3108`/`0xbf32c8`/`0xbf3488`/`0xbf3698`/`0xbf3778`) plus direct symbol stores. The *table layout* is recovered from those register functions, not from the binary's data sections.

---

## 3. Size budget

The ~117 MiB on disk decomposes into a tiny code/data core and a dominant DWARF block. The loadable core a reimplementation must reproduce — code plus all writable state — is well under 9 MiB; the rest is debug metadata and the literal pool.

| Region | Section(s) | Size | Share of 122,956,336 B |
|---|---|---|---|
| Executable code | `.text` (+ `.init`/`.plt`/`malloc_hook`/`.fini`) | `0x790b19` ≈ 7.6 MiB | ~6.5% |
| Const / literal pool | `.rodata` | `0x3051b3` ≈ 3.0 MiB | ~2.6% |
| RELRO const-ptr tables | `.data.rel.ro` | `0x127c0` ≈ 75.7 KiB | ~0.06% |
| GOT | `.got` + `.got.plt` | `0x1810` + `0xde0` ≈ 9.9 KiB | <0.01% |
| Initialized writable | `.data` | `0x2fe20` ≈ 196 KiB | ~0.16% |
| Zero-init writable | `.bss` (NOBITS — runtime RAM, 0 file bytes) | `0x7c6c0` ≈ 497 KiB | 0% on disk |
| Unwind / LSDA | `.eh_frame{,_hdr}` + `.gcc_except_table` | ~1.0 MiB | ~0.9% |
| **DWARF v4** | `.debug_{info,str,loc,line,ranges,…}` | **~108 MiB** | **~92%** |
| Static symbols | `.symtab` + `.strtab` | `0x96228` ≈ 600 KiB (25,623 syms) | ~0.5% |

> **NOTE — strip the binary and ~92% evaporates.** `.debug_info` alone is `0x22f787d` (~36 MiB) and `.debug_str` is `0x1b63195` (~28 MiB) (sizes from [ELF Anatomy §1](../forensics/elf-anatomy.md)). The runtime's actual footprint is the ~7.6 MiB `.text` plus ~497 KiB of `.bss` allocated at load. The full DWARF is why every other page in this book can symbolize addresses to function names; it is not part of the runtime's working set.

---

## 4. Cross-binary section profile

The runtime ships alongside four other images. Their geometries diverge from libnrt in two reimplementation-relevant ways: **none carries the `NRT_*` version graph**, and **only libnccom.so carries DWARF** — the firmware and microcode images are debug-stripped. A second divergence: unlike libnrt's delta-zero identity map, the firmware/microcode/collectives images carry a small `+0x1000` shift between their RW-data VMA and file offset (their writable `LOAD` is page-aligned with a one-page bias), so `.data` reads in those binaries must use the section-header `Off` column, not the VMA. The kernel driver ships as **DKMS source only** — no prebuilt `.ko` is in the package, so its section profile is N/A.

| Binary | SONAME | BuildID[sha1] | `.text` | `.rodata` | `.data` | DWARF? | Confidence |
|---|---|---|---|---|---|---|---|
| `libnrt.so.2.31.24.0` | `libnrt.so.1` | `8bb57aba…102e` | `0x790b19` @`0x3dbc0` | `0x3051b3` @`0x7cf000` | `0x2fe20` @`0xc07e00` (delta 0) | **yes** (v4) | CERTAIN |
| `libncfw.so` | `libncfw.so.2.31.1.0.cf13a49f` | `a98f8e1c…0db5` | `0x63991` @`0x10c0` | `0x2c8e4` @`0x65000` | `0x8` @ VMA `0x95020`/Off `0x94020` | **no** | CERTAIN |
| `libnrtucode_extisa.so` | `libnrtucode_extisa.so` | `7bb03bc4…5e44` | `0x7dc2` @`0x3180` | `0x923968` @`0xb000` | `0xff8` @ VMA `0x9350a0`/Off `0x9340a0` | **no** | CERTAIN |
| `libnccom.so.2.31.24` | `libnccom.so.2` | `9c00176c…63f0` | `0x6b987` @`0x1ea80` | `0xc5f5` @`0x8b000` | `0x8830` @ VMA `0xa86a0`/Off `0xa76a0` | **yes** (v4) | CERTAIN |
| `neuron.ko` | — (DKMS module `neuron`, pkg `aws-neuronx 2.27.4.0`) | N/A (built on host install) | N/A — source-only | N/A | N/A | N/A | CERTAIN |

> **QUIRK — the SONAME carries the version+hash for `libncfw`, the sections carry it for `libnrt`.** `libncfw.so` self-versions in its SONAME (`libncfw.so.2.31.1.0.cf13a49f` — version + the `cf13a49f` hash inline) and has an *empty* `.gnu.version_d`; `libnrt.so` instead stamps its version into the non-standard `.git_hash`/`.nrt_brazil_version` `PROGBITS` sections and exposes a 3-node `NRT_*` version graph. The firmware image is a thin `dlopen`'d loader (615,640 B, `.text` `0x63991`), not a versioned ABI surface — so it needs neither the version graph nor DWARF.

> **NOTE — `libnrtucode_extisa.so` is almost all `.rodata`.** Its `.rodata` is `0x923968` (~9.2 MiB) against a `.text` of only `0x7dc2` (~31 KiB) — the image is a microcode/ISA blob store with a thin code shell, the dlsym target whose 30 `nrtucode_*` entry points libnrt binds via `ucode_func_symbols` (§2). The collectives image `libnccom.so` is the opposite balance — `0x6b987` `.text` of real NCCL-fork transport/algorithm code with a small `.rodata`.

---

## Cross-References

- [ELF Anatomy](../forensics/elf-anatomy.md) — the structural anatomy this page's geometry sits inside: the 46-section table, the `NRT_*` symbol-version graph, the `DT_NEEDED`/relocation model, and the toolchain census
- [Globals Atlas](../forensics/globals-atlas.md) — the writable-globals inventory whose `.data`/`.bss` singletons this page locates by section band (`kaena_khal`, `nrt_config`, `ngc`, `vcores`, the xu state arrays)
- [Dispatch Tables](../forensics/dispatch-tables.md) — the jump-table / vtable / fn-ptr-table catalog whose entries this page maps to their physical section (the `.bss` HAL tables, the `.data.rel.ro` seeds, the `.data` proto descriptors)
- [Vendored-Library SBOM](../forensics/vendored-sbom.md) — the version pins for the statically-linked protobuf / Abseil / Rust / libarchive whose vtables and `descriptor_table_*` objects fill `.data.rel.ro` and `.data`
- [Arch CSR Offsets](../runtime/arch-csr-offsets.md) — a runtime consumer of the delta-zero rule: it reads per-arch register magics from `.rodata`/`.data` tables at their VMA directly, and carries the same `+0x400000`-is-not-libnrt correction
