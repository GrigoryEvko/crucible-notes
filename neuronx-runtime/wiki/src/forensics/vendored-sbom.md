# Vendored-Library SBOM

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB DYN x86-64, SONAME `libnrt.so.1`, NOT stripped, full DWARF v4.
>
> **Part II — Binary Anatomy & Forensics** / REFERENCE · **Evidence grade:** every version byte-pinned to a quoted token (`readelf -p .comment`, `strings -a`, `nm -C`, `readelf -V/-d`) re-run against this exact binary · [back to forensics index](overview.md)

## Abstract

`libnrt.so` statically vendors every heavy dependency it touches into its own `.text` — `readelf -d` lists `DT_NEEDED` only for `libgcc_s`/`libutil`/`librt`/`libpthread`/`libdl`/`libstdc++`/`libm`/`libc`/`ld-linux`; there is **no** NEEDED entry for protobuf, Abseil, simdjson, libarchive, zlib, or the Rust runtime. This page is the recovered **software bill of materials** for that vendored mass: a flat, version-pinned manifest of what got linked in, the exact evidence token that pins each version, and a confidence grade. It is the reference companion to the [forensics overview](overview.md), which establishes the first-party/vendored split by byte budget and function count; this page does not re-derive that split — it pins the *versions*.

The recovery method is the same one a maintainer would use to audit a closed binary: a static-link SBOM is reconstructible because the toolchains leave fingerprints behind. C/C++ libraries carry a compiled-in version macro the runtime checks itself (protobuf's `GOOGLE_PROTOBUF_VERSION 5026001`, libarchive's `ARCHIVE_VERSION_NUMBER 3008000`, zlib's `ZLIB_VERSION "1.3.1"`, simdjson's `SIMDJSON_VERSION 0.9.0`, Abseil's `ABSL_LTS_RELEASE_VERSION 20230802`). AWS's Brazil build system leaves package-cache paths in `.debug_str` (`.../packages/Protobuf/Protobuf-26.x.13.0/...`). Rust embeds the toolchain commit hash as a sysroot path (`/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb`) and each crate's `index.crates.io-.../<crate>-<ver>/src` source path. Every row below is anchored to one of those tokens, quoted verbatim, or explicitly tagged when the version is inferred rather than printed.

Two historical misreads are corrected in place. The protobuf runtime was once read as "v24 / `EDITION_2023`", confusing the Editions *feature ceiling* enum for a version number — the truth is **protobuf 5.26.1** (`5026001`). The Rust toolchain was once read as "1.89.0" — the truth is **rustc 1.91.1**, carried twice in the binary (the `.comment` string and the `/rustc/<hash>` path). Both corrections are detailed under [§3](#3-corrections).

For reimplementation, the contract this page establishes is:

- **Link, do not reverse.** Every row tagged VENDORED is upstream OSS at a pinned version. A reimplementer links that exact version from source; reversing protobuf's arena allocator or simdjson's SIMD kernels by hand is wasted effort. The arena allocator internals are documented (for verification, not reimplementation) in the [overview's protobuf note](overview.md#1-provenance-split--the-pie-as-a-table).
- **Pin to the byte.** A "uses protobuf" claim is useless; "protobuf 5.26.1 with Abseil LTS 20230802 and the bundled utf8_range" is buildable. Each version is the exact one the binary was compiled and linked against — mismatching it changes ABI (protobuf's `VerifyVersion` @`0x6fb690` *fatals* at startup on a version skew).
- **Know what is NOT vendored.** `libstdc++` is dynamic (`DT_NEEDED libstdc++.so.6`), not a static blob; the `std::`/`__gnu_cxx::` symbols defined in `.text` are header-only template instantiations emitted by GCC 14.2.1. The `neuron_rustime` sys_trace crate is first-party AWS Rust, not vendored. The `Kaena*` packages are AWS first-party, recorded here only as build-provenance context.

| | |
|---|---|
| **SBOM rows** | 14 native/runtime + 12 Rust crates + 3 first-party Kaena |
| **C/C++ toolchain** (`.comment`) | `GCC: (GNU) 14.2.1 20250110 (Red Hat 14.2.1-10)`; legacy C TUs GCC 7.3.1 / 8.5.0; `clang version 21.1.0-rc2` |
| **Rust toolchain** (`.comment`) | `rustc version 1.91.1 (ed61e7d7e 2025-11-07)` |
| **Protobuf version anchor** | `GOOGLE_PROTOBUF_VERSION 5026001` (= 5.26.1); `VerifyVersion` @`0x6fb690` |
| **Rust sysroot anchor** | `/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb` |
| **`libstdc++` linkage** | DYNAMIC (`DT_NEEDED libstdc++.so.6`); needs `GLIBCXX_3.4.22` / `CXXABI_1.3.11` |
| **`libnccom.so`** | sibling binary, NOT in this extract — out of SBOM scope (libnrt.so only) |

---

## 1. At a glance — the provenance triage

Before the version table, the three classes a reimplementer treats differently. The SBOM rows in [§2](#2-the-sbom-table) carry a Class column drawn from this triage.

| Class | What it means | How to treat it | Count |
|---|---|---|---|
| **VENDORED** | Upstream OSS statically linked into `.text` at a pinned version | Link the real library at that version; do not reverse | 5 native C/C++ + Rust std/core/alloc + 12 crates |
| **VENDORED (dynamic)** | `libstdc++` — header-only template instantiations + `.so.6` resolved at load | Reproduce structurally via any C++17 STL; the ABI floor is `GLIBCXX_3.4.22` | 1 |
| **VENDORED (AWS pkg)** | First-party-to-AWS but vendored from a separate Brazil package into libnrt | Out of OSS scope; record version for build provenance | `KaenaHal`, `KaenaDriverLib` |
| **GENERATED (AWS schema)** | `protoc`-emitted accessors for an AWS-authored `.proto` | Regenerate from the recovered schema; the wire format is first-party | `KaenaProfilerFormat` (`ntff.pb.cc` / `neuron_trace.pb.cc`) |

> **NOTE —** the `neuron_rustime` sys_trace crate (the AWS-authored Rust that produces NTFF trace events) is **first-party logic**, not a vendored crate, and is deliberately absent from the SBOM below. It carries no `index.crates.io/.../neuron_rustime-<ver>` path because it is built from the KaenaRuntime source root, not the cargo registry. Do not mistake it for vendored Rust; it is owned by [Part XIII — Profiling & Trace](../trace/overview.md).

---

## 2. The SBOM table

Three sub-tables: native C/C++ runtimes, Rust crates, and first-party Kaena packages. **Evidence** quotes the exact token recovered from this binary (macro value / source path / sysroot hash / `.rodata` literal). **Est. fn count** is the defined-text-symbol estimate from the forensics overview's DWARF/IDA partition, given so a reimplementer can size the dependency; it is not a version anchor. **Conf** grades the *version pin*, not the presence.

### 2.1 Native C/C++ runtimes (statically linked)

| Library | Version | Evidence (verbatim token in `libnrt.so`) | Est. fns | Class | Conf |
|---|---|---|---:|---|---|
| **protobuf** (C++ runtime) | **5.26.1** (= 26.1) | macro `"GOOGLE_PROTOBUF_VERSION 5026001"` (5 026 001 = 5.26.1); `VerifyVersion` @`0x6fb690`; path `".../Protobuf/Protobuf-26.x.13.0/.../stubs/common.cc"` | ~3,003 | VENDORED | CERTAIN |
| protobuf (headers) | 26.x (5.x scheme) | path `".../GoogleProtoBuf/GoogleProtoBuf-5.x.4041.0/.../map_field.h"` — same generation, second AWS package label (see [§3](#3-corrections)) | — | VENDORED | HIGH |
| protobuf Editions | `EDITION_2023` (max) — **not a version** | `"PROTOBUF_MAXIMUM_EDITION EDITION_2023"`; `"PROTOBUF_MINIMUM_EDITION EDITION_PROTO2"` | — | VENDORED | CERTAIN |
| **utf8_range** | bundled w/ protobuf 26.x | ships in protobuf `third_party/utf8_range`; no standalone version; uses `absl::lts_20230802::string_view` | — | VENDORED | HIGH |
| **Abseil** | **LTS 20230802** (patch 1) | macros `"ABSL_LTS_RELEASE_VERSION 20230802"` + `"ABSL_LTS_RELEASE_PATCH_LEVEL 1"`; ns `absl::lts_20230802::`; path `".../Abseil/Abseil-20230802.x.5134.0/..."` | ~1,177 | VENDORED | CERTAIN |
| **simdjson** | **0.9.0** | macro `"SIMDJSON_VERSION 0.9.0"`; per-µarch `simdjson::{haswell,westmere,fallback}::` kernels; src tree `Simdjson/third-party-src/...` | ~83 | VENDORED | CERTAIN |
| **libarchive** | **3.8.0dev** | macros `"ARCHIVE_VERSION_NUMBER 3008000"` + `ARCHIVE_VERSION_ONLY_STRING "3.8.0dev"` + `"libarchive 3.8.0dev"`; path `".../Libarchive/build/private/src/libarchive/archive_read.c"` | ~362 | VENDORED | CERTAIN |
| **zlib** | **1.3.1** | macro `ZLIB_VERSION "1.3.1"`; banner `" inflate 1.3.1 Copyright 1995-2024 Mark Adler "`; defined `inflate`/`inflateInit2_`/`crc32` | ~36 | VENDORED | CERTAIN |
| **libstdc++ / libsupc++** | GCC **14.2.1** (ABI source) | `.comment: "GCC: (GNU) 14.2.1 20250110 (Red Hat 14.2.1-10)"`; `DT_NEEDED libstdc++.so.6`; `readelf -V` version-needs floor `GLIBCXX_3.4.22` / `CXXABI_1.3.11` (`GLIBCXX_3.4.17..3.4.22`, `CXXABI_1.3.2..1.3.11`) | — | VENDORED (dynamic) | HIGH |

> **CORRECTION (F-3PVER / SRCMAP) —** an earlier source-map note read the absence of a protobuf-runtime DWARF compile-unit as the runtime not being statically linked. It **is** linked: `nm -C` lists thousands of defined (`t`) `google::protobuf::` text symbols including `SerialArena`/`ThreadSafeArena`/`ArenaStringPtr` and `VerifyVersion` @`0x6fb690`. The DWARF gap is a `-g`-less build of the runtime TUs (no line tables), not missing code. Abseil is *also* present — both, not alternatives. The arena allocator internals are dissected in [`overview`](overview.md#1-provenance-split--the-pie-as-a-table); the runtime backs the NTFF/NEFF protobuf schemas.

> **QUIRK —** the protobuf fn-count (~3,003 defined text symbols per the F-3PVER nm sweep) is larger than a naive `nm -C | rg -c 'google::protobuf'` of the demangled stream would lead you to trust — that grep returns 4,494 matching *lines*, inflated by multi-line demangles and Abseil-nested protobuf names. Ground any protobuf count claim on distinct defined-symbol bodies, not on a grep of the demangle stream. The version pin does not depend on the count — it is the `5026001` macro.

### 2.2 Rust crates (statically linked, rustc 1.91.1)

The Rust std/core/alloc libraries and every crate below are pinned by the `/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb` sysroot path plus, for each crate, the cargo source path `registry/src/index.crates.io-1949cf8c6b5b557f/<crate>-<ver>/src/...`. Every `<crate>-<ver>` token below was recovered verbatim from this binary.

| Crate | Version | Evidence (verbatim `<crate>-<ver>` token) | Role | Conf |
|---|---|---|---|---|
| **rustc** (std/core/alloc) | **1.91.1** (`ed61e7d7e`) | `.comment: "rustc version 1.91.1 (ed61e7d7e 2025-11-07)"`; `/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb/library/{std,core,alloc}/...` | toolchain + libstd | CERTAIN |
| serde_json | **1.0.145** | `"index.crates.io-1949cf8c6b5b557f/serde_json-1.0.145/src"` | JSON ser/de | HIGH |
| serde_core | **1.0.228** | `"index.crates.io-1949cf8c6b5b557f/serde_core-1.0.228/src"`; serde facade re-exports it (no separate `serde-1.x` path) | serde core traits | HIGH |
| hashbrown | **0.15.5** | `"hashbrown-0.15.5/src/raw/mod.rs"` | SwissTable hash map | HIGH |
| crossbeam-queue | **0.3.12** | `"crossbeam-queue-0.3.12"` | MPMC lock-free queue | HIGH |
| memchr | **2.7.5** | `"memchr-2.7.5/src/arch/all/packedpair/mod.rs"` | substring/byte search | HIGH |
| log | **0.4.28** | `"index.crates.io-1949cf8c6b5b557f/log-0.4.28"` | logging facade | HIGH |
| gimli | **0.32.0** | `"gimli-0.32.0/src/read/line.rs"` | DWARF reader (backtrace) | HIGH |
| addr2line | **0.25.0** | `"addr2line-0.25.0/src"` | addr→line symbolization | HIGH |
| object | **0.37.3** | `"object-0.37.3/src"` | ELF/object parsing (backtrace) | HIGH |
| miniz_oxide | **0.8.9** | `"miniz_oxide-0.8.9/src"` | pure-Rust deflate/inflate | HIGH |
| rustc-demangle | **0.1.26** | `"rustc-demangle-0.1.26/"` | Rust symbol demangling | HIGH |
| adler2 | **~2.0.x** (inferred) | symbols `adler2::adler32_slice` @`0x583c80`, `adler2::Adler32::write_slice` @`0x583cb0` — **no** `adler2-<ver>` cargo path emitted; version inferred from `miniz_oxide 0.8.9` dep tree | Adler-32 checksum | MEDIUM |

> **GOTCHA —** the Rust crate-version tokens appear in two forms in this binary, and only one is reliable as a *full* path. `log`, `serde_core`, and `serde_json` print the complete `registry/src/index.crates.io-1949cf8c6b5b557f/<crate>-<ver>` form; the rest (hashbrown, gimli, memchr, …) appear only as the bare `<crate>-<ver>/src/...` suffix in `.debug_str`. Both pin the version unambiguously — the `<crate>-<ver>` segment is the cargo-registry directory name and is byte-identical to the published version — but a grep that requires the `index.crates.io` prefix will miss nine of the twelve crates. `adler2` prints **neither** form; it is present by symbol only (@`0x583c80`), so its exact patch is the one MEDIUM row, pinned by inference from `miniz_oxide 0.8.9`'s lockfile dependency.

> **NOTE —** the Rust crates split by role: backtrace/symbolization (`gimli`, `addr2line`, `object`, `rustc-demangle`) for panic/crash reporting; `serde_json`+`serde_core` for JSON; `hashbrown` for hashing; `crossbeam-queue` for MPMC; `memchr` for substring scans; `log` for logging; `miniz_oxide`+`adler2` for deflate. They serve the first-party `neuron_rustime` sys_trace bridge — the Rust crate inventory exists *because* of that one AWS-authored crate, not the reverse.

### 2.3 First-party AWS Kaena packages (build-provenance context)

These are AWS-internal packages vendored from separate Brazil packages, recorded for provenance, not as third-party OSS. Versions are pinned by the Brazil package-cache path.

| Package | Version | Evidence (Brazil package path) | Role | Conf |
|---|---|---|---|---|
| **KaenaProfilerFormat** | **2.31.0.0** | `".../KaenaProfilerFormat/KaenaProfilerFormat-2.31.0.0/.../{ntff.pb.cc,neuron_trace.pb.cc}"` | generated NTFF/trace protobuf schema (GENERATED) | CERTAIN |
| **KaenaHal** | **2.31.0.0** | `".../KaenaHal/KaenaHal-2.31.0.0/..."` | hardware abstraction layer | CERTAIN |
| **KaenaDriverLib** | **2.27.4.0** | `".../KaenaDriverLib/KaenaDriverLib-2.27.4.0/..."` (+ `KaenaDriver-2.27.4.0`) | userspace driver shim (`ndl.c`) | CERTAIN |

> **NOTE —** three `*ArchHeaders` Brazil packages also appear in `.debug_str` — `CaymanArchHeaders-1.0.826.0`, `MarianaArchHeaders-1.0.1133.0`, `SundaArchHeaders-1.0.1851.0` — pinning the per-silicon arch-register header versions that the triplicated `cayman`/`mariana`/`sunda` instruction-block builders compile against. They are headers, not linked code, and carry no functions; recorded here only because they version-stamp the device targets the runtime was built for.

---

## 3. Corrections

Two cross-cell version disagreements were resolved against the binary itself; both corrections are recorded in place rather than silently edited.

### 3.1 protobuf "v24 / EDITION_2023" → 5.26.1

> **CORRECTION (F-3PVER, CONFLICT A) —** an earlier seed guessed protobuf "v24.x", reading the lone `"EDITION_2023"` static-init literal as a version number. `EDITION_2023` is **not** a library version — it is the protobuf *Editions* feature ceiling (`PROTOBUF_MAXIMUM_EDITION EDITION_2023`), a normal enum constant in any 26.x runtime naming the newest proto Edition the runtime accepts. The authoritative version literal is `"GOOGLE_PROTOBUF_VERSION 5026001"` (5 026 001 = **5.26.1** = "26.1"), the value `VerifyVersion` @`0x6fb690` compiles against and *fatals* on if the linked runtime disagrees (error strings `"This program was compiled with Protobuf C++ version "` / `", but the linked version is "`). The "5.x.4041.0" in the `GoogleProtoBuf-5.x.4041.0` header-package path is a *second* AWS Brazil package label (the header tree: `map_field.h`, `io/`, `stubs/`), distinct from the `Protobuf-26.x.13.0` runtime package — both use the post-21.0 C++ versioning scheme where 5.26 ≡ 26.1, so they are the **same** protobuf generation under two package labels (headers × runtime), not two versions. **Verdict: protobuf 5.26.1.**

### 3.2 Rust "1.89.0" → 1.91.1

> **CORRECTION (F-3PVER, CONFLICT B) —** an earlier "1.89.0" reading is rejected. The binary carries the rustc version **twice**, independently: the verbatim `.comment` string `"rustc version 1.91.1 (ed61e7d7e 2025-11-07)"` (also echoed in the clang-LLVM ident `"clang LLVM (rustc version 1.91.1 (ed61e7d7e 2025-11-07))"`), and the sysroot source-path commit hash `"/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb"`. The short hash `ed61e7d7e` in the `.comment` is the prefix of the long `/rustc/<hash>` path — two encodings of the same commit. **Verdict: rustc 1.91.1 (`ed61e7d7e`, 2025-11-07).**

---

## 4. Usage map — what each vendored library backs

A reimplementer should know *why* each library is present, to scope which subsystems pull which dependency. This is the consumer map, not a version anchor.

| Library | Backs | Pulls in |
|---|---|---|
| protobuf 5.26.1 | wire format for `KaenaProfilerFormat` (`ntff.pb.cc` / `neuron_trace.pb.cc` trace payloads) and NEFF/NTFF protobuf schemas; arena allocator + descriptor DB | Abseil, utf8_range |
| utf8_range | UTF-8 validation for protobuf 26.x string fields | Abseil `string_view` |
| Abseil 20230802 | C++ support runtime for protobuf and first-party C++: cord/status/strings/log/synchronization/time/swisstable | — |
| simdjson 0.9.0 | parses embedded JSON metadata in NEFF (`neff_json_parser`); dual µarch (westmere+haswell) selected by CPU dispatch | — |
| libarchive 3.8.0dev | NEFF container = gzip'd TAR; tar-format reader + gzip-filter reader (`archive_read_open_memory` path) | zlib |
| zlib 1.3.1 | `inflate` path backing libarchive's gzip filter; NEFF decompression | — |
| libstdc++ (GCC 14.2.1) | STL containers/strings — header-only template instantiations; `.so.6` is dynamic | — |
| Rust 1.91.1 + crates | `neuron_rustime` sys_trace: backtrace/symbolization (`gimli`/`addr2line`/`object`/`rustc-demangle`), JSON (`serde_json`/`serde_core`), hashing (`hashbrown`), MPMC (`crossbeam-queue`), substring (`memchr`), logging (`log`), deflate (`miniz_oxide`+`adler2`) | — |

---

## Cross-References

- [Overview and Heavy-Frame Census](overview.md) — the first-party/vendored byte split this SBOM versions; the protobuf-runtime static-link correction and arena allocator note
- [ELF Anatomy](elf-anatomy.md) — `DT_NEEDED` list, section/segment table, DWARF presence, and the `.comment`/`.debug_str` sections these version tokens live in
- [Symbol-Version Manifest](../appendix/symbol-versions.md) — the `GLIBCXX_3.4.x` / `CXXABI_1.3.x` ABI-floor needs that pin the dynamic libstdc++ requirement
