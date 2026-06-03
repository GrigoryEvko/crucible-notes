# Bibliography

> *Every page in this book is a reconstruction of `libtpu.so` from the freely-redistributed `libtpu-0.0.40-cp314` PyPI wheel (`manylinux_2_31_x86_64`): a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d`, reported runtime version `0.103`. This page records the artifact analyzed, the legal basis for analyzing it, the tools that read it, and the external standards and public references the reconstruction relies on. Another wheel will differ in every address.*

## Abstract

This is the references page. Every other page in the book makes claims about a single stripped-but-symbol-bearing shared object; this page records the apparatus those claims rest on. It is organized as five citation registers: the **provenance** of the exact analyzed artifact (down to byte counts and build-ids); the **legal basis** under which reverse engineering a publicly distributed binary for interoperability and research is lawful; the **tools** that performed the analysis; the external **standards and specifications** the binary's layout and contents conform to (so a reader can decode an Itanium-mangled symbol, a System V ABI relocation, or a protobuf descriptor without re-deriving the format); and the **public technical references** for the TPU/XLA/PJRT domain that frame what the binary *is*.

The book's central discipline is that nothing here was read from source. All analysis is from static reverse engineering of the compiled `libtpu.so` and `sdk.so` ELF objects using IDA Pro 9.x. No source code or any other restricted or copyrighted material was used — all findings derive solely from analysis of compiled binaries. The provenance section below is therefore written in the same pure-RE register as the rest of the book: sizes, build-ids, and section facts that anyone with the wheel and `readelf` can independently confirm. The legal, standards, and reference sections are ordinary citations — they point outward to public, stable documents and do not assert anything about the binary's internals.

The wheel is freely available on PyPI; Google publishes it as the official Cloud TPU PJRT plugin. Its public distribution and the interoperability/research purpose of this work are what make the analysis lawful — the same framing that applies to reverse-engineering any publicly distributed software toolchain. The authorities for that are cataloged under [Legal Basis](#legal-basis).

The contract for this page:

- **Provenance** — the exact wheel, the two ELF objects inside it, their sizes and build-ids, and where the wheel is publicly distributed, stated so the artifact can be re-acquired and re-verified bit-for-bit.
- **Legal basis** — the canonical reverse-engineering-for-interoperability authorities (US statute + case law, EU directive), cited precisely.
- **Tools, standards, references** — the disassembler and ELF tooling; the binary-format and IR standards the analysis decodes against; and the public domain references for TPU/XLA/PJRT.

| | |
|---|---|
| **Artifact** | `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl` |
| **Primary object** | `libtpu/libtpu.so` — 781,691,048 B, build-id `89edbbe81c5b328a958fe628a9f2207d` |
| **Secondary object** | `libtpu/sdk.so` — 22,541,240 B, build-id `4e9025466f71009fccb46a803806411c63744a0a` |
| **Runtime version** | `0.103` (reported); wheel version `0.0.40` |
| **Embedded toolchain** | LLVM/MLIR 23-dev (trunk), monorepo commit `8918319853fbdf9e6f6cb69e96848f913a22bc31` |
| **Analysis tool** | IDA Pro 9.x (Hex-Rays decompiler + FLIRT) |
| **Method** | Static reverse engineering of stripped-but-symbol-bearing x86-64 ELF — no source, no debugger, no running TPU |

---

## Provenance

The analyzed artifact is one PyPI wheel. Everything in the book is recovered from the two ELF shared objects it contains. This section fixes those objects by the properties that uniquely identify them, so a reader can re-acquire the wheel, extract it, and confirm every figure below with `readelf` and `ls`.

### The Artifact

The wheel filename encodes the full identity:

```text
libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl
   |     |       |     |          |
   |     |       |     |          +-- platform: manylinux_2_31, x86-64 (glibc >= 2.31)
   |     |       |     +-- ABI tag: cp314 (CPython 3.14 ABI)
   |     |       +-- Python tag: cp314 (CPython 3.14)
   |     +-- version: 0.0.40
   +-- distribution: libtpu (Google Cloud TPU PJRT plugin)
```

A wheel is a ZIP archive; extracting it yields a `libtpu/` package directory containing the two shared objects plus packaging metadata (`__init__.py`, `LICENSE`, `THIRD_PARTY_NOTICES.txt`, `SDK_THIRD_PARTY_NOTICES.txt`). The package `LICENSE` identifies the software as `Copyright [2026] Google LLC`, made available under the Google Cloud Platform agreement; the runtime version `0.103` is read from package metadata at import time, distinct from the `0.0.40` wheel version.

### Facts Table

Every figure here is directly observable: sizes from `ls -l` or `stat`, build-ids and ELF class from `readelf -n` / `readelf -h` / `file`.

| Property | `libtpu.so` | `sdk.so` | How confirmed | Confidence |
|---|---|---|---|---|
| Size on disk (bytes) | 781,691,048 | 22,541,240 | `ls -l` on extracted file | CERTAIN |
| ELF class | ELF64 LSB, x86-64 | ELF64 LSB, x86-64 | `readelf -h` / `file` | CERTAIN |
| ELF type | `ET_DYN` shared object | `ET_DYN` shared object | `readelf -h` | CERTAIN |
| OS/ABI | SYSV | GNU/Linux | `file` | CERTAIN |
| Build-id | `89edbbe81c5b328a958fe628a9f2207d` | `4e9025466f71009fccb46a803806411c63744a0a` | `readelf -n` (`NT_GNU_BUILD_ID`) | CERTAIN |
| Build-id format | md5/uuid (16 B) | sha1 (20 B) | `readelf -n` note length | CERTAIN |
| Symbol table | present (`.symtab` survives) | present | `file` reports "not stripped" | CERTAIN |
| Role | the compiler + runtime; "745 MB" object | the smaller SDK/host shim | size + [two-binary-split](forensics/two-binary-split.md) | HIGH |
| Embedded LLVM/MLIR | 23-dev trunk, commit `8918319853fbdf…` | — | `.rodata` literals; see [manifest](forensics/llvm-mlir-manifest.md) | HIGH |

> **NOTE (provenance) —** the two build-ids use different note formats: `libtpu.so` carries a 16-byte md5/uuid build-id, `sdk.so` carries a 20-byte sha1. `readelf -n` reports the note payload length (`0x10` vs `0x14`), which is itself the discriminator. This asymmetry is one of several signals that the two objects come off different build rules in the same release; the full argument is in [The Two-Binary Split](forensics/two-binary-split.md).

> **NOTE (RE discipline) —** every claim in this book derives **solely** from static analysis of these two compiled binaries. No source tree, no debugger session, no running TPU, and no Google-internal artifact entered the reconstruction. The provenance facts above are the ground truth a reader can independently reproduce with the wheel and standard binutils; everything else in the book is anchored back to addresses and offsets inside these same two objects. See [Evidence & Confidence Conventions](front/evidence-conventions.md) for the trust labels every page applies, and [Methodology](methodology.md) for the full pipeline.

### Reproducing the Artifact Identity

```bash
# Acquire (the wheel is freely distributed on PyPI)
pip download libtpu==0.0.40 --no-deps \
  --platform manylinux_2_31_x86_64 --python-version 3.14 \
  --only-binary=:all: -d .

# A wheel is a ZIP; extract it
unzip libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl -d extracted/

# Confirm the two objects, byte-for-byte
cd extracted/libtpu
ls -l libtpu.so sdk.so                 # 781691048 / 22541240
readelf -n libtpu.so | grep 'Build ID' # 89edbbe81c5b328a958fe628a9f2207d
readelf -n sdk.so    | grep 'Build ID' # 4e9025466f71009fccb46a803806411c63744a0a
```

Where the wheel is distributed: the Python Package Index (`pypi.org/project/libtpu/`), as the official Cloud TPU PJRT plugin Google ships for JAX/XLA on TPU. The package's third-party notices (`THIRD_PARTY_NOTICES.txt`, 731,537 B; `SDK_THIRD_PARTY_NOTICES.txt`, 103,306 B) enumerate the open-source components statically linked in — a published, authoritative corroboration of the embedded-library census in [Embedded-Library Atlas](forensics/embedded-library-atlas.md), used only as a cross-check, never as a source of internal behavior.

---

## Legal Basis

Reverse engineering a publicly distributed binary for interoperability and research is a long-settled, lawful activity in both US and EU law. This book relies on the following canonical authorities. They are cited here because the reconstruction's legitimacy is part of its provenance, not because the book asserts anything novel about them.

### United States — Statute

- **Digital Millennium Copyright Act, 17 U.S.C. § 1201(f) — "Reverse Engineering" exemption.** Permits circumventing technological protection measures, and developing the means to do so, for the sole purpose of identifying and analyzing the elements of a program necessary to achieve **interoperability** of an independently created program — to the extent such acts are permitted under copyright law. The provisions also permit sharing the information obtained for that interoperability purpose. The analysis here neither circumvents access controls (the wheel is distributed in the clear) nor redistributes the binary; § 1201(f) is the statutory backstop for the interoperability framing.

### United States — Case Law

- **Sega Enterprises Ltd. v. Accolade, Inc.**, 977 F.2d 1510 (9th Cir. 1992). Holds that **disassembly of a lawfully obtained copy** of a computer program is fair use when it is the only means of access to the unprotected functional elements (ideas, interfaces) of the program, and the copier has a legitimate reason — interoperability — for seeking that access. The foundational US authority that intermediate copying during disassembly does not infringe when its purpose is to study uncopyrightable functional aspects.

- **Sony Computer Entertainment, Inc. v. Connectix Corp.**, 203 F.3d 596 (9th Cir. 2000). Extends *Sega*: intermediate copying of the PlayStation BIOS during reverse engineering to produce an interoperable, independently written emulator is fair use, even where the resulting product competes with the original. Confirms that the transformative, interoperability-driven purpose controls, not whether the end product is commercially adverse.

### European Union

- **Directive 2009/24/EC of 23 April 2009 on the legal protection of computer programs (the "Software Directive").**
  - **Article 5(3)** — the lawful user of a program may, without authorization, observe, study, or test its functioning to determine the **ideas and principles** underlying any element of the program, while performing acts they are entitled to perform.
  - **Article 6** — **decompilation** is permitted, without the rightholder's authorization, where indispensable to obtain the information necessary to achieve the **interoperability** of an independently created program, subject to the stated conditions (performed by a licensee, the information not previously readily available, confined to the parts necessary for interoperability).

> **NOTE (scope) —** these authorities support analysis of functional/interoperability elements and ideas, not wholesale reproduction of protected expression. This book reproduces no source code and no copyrightable expression from the binary; it documents algorithms, data layouts, and interfaces recovered by analysis — precisely the unprotected functional elements *Sega*, *Connectix*, and Articles 5–6 contemplate. The wheel itself is freely and publicly distributed, so the "lawfully obtained copy" predicate is satisfied without any circumvention. The legal framing here mirrors the established CUDA-toolkit / publicly-distributed-software RE-for-research framing and applies equally to the libtpu wheel. See [Methodology § legal basis](methodology.md) for the in-pipeline statement.

---

## Tools

The reconstruction used a small, conventional static-analysis toolchain. No dynamic instrumentation, no emulation, and no TPU hardware were involved.

| Tool | Version | Role in this work |
|---|---|---|
| **IDA Pro** | 9.x | Primary disassembler and decompiler. Recovers code/data, the Hex-Rays C decompilation of each function, control-flow graphs, type information, and cross-references — the substrate every page is written from. |
| **Hex-Rays decompiler** | bundled with IDA 9.x | Produces the C-level pseudocode that the `### Algorithm` blocks throughout the book are distilled from. |
| **FLIRT** | bundled with IDA 9.x | Library-signature matching used to identify statically-linked third-party code (LLVM, Abseil, protobuf, …); feeds the [Embedded-Library Atlas](forensics/embedded-library-atlas.md). |
| **`readelf`** (GNU binutils) | system | ELF header, section/segment table, notes (`NT_GNU_BUILD_ID`), dynamic table, and symbol-table inspection. Source of every provenance fact above. |
| **`objdump`** (GNU binutils) | system | Cross-checking disassembly and section contents against IDA's recovery. |
| **`nm`** (GNU binutils) | system | Symbol enumeration / demangling cross-check against IDA's `.symtab` read. |
| **`strings`** | system | `.rodata` literal recovery (version pins, format markers, error text) at byte offsets — e.g. the LLVM/MLIR version literals. |
| **Sidecar-extraction pipeline** | project-internal | Serializes IDA's per-function output (disassembly, decompiled C, CFGs, types, xrefs) into machine-readable sidecars that the cross-validation passes consume; described in [Methodology](methodology.md). |

> **NOTE (tooling) —** IDA's symbol-table read is a hypothesis source, not ground truth. Because `libtpu.so` is not stripped, identifiers arrive demangled from `.symtab`; the book treats every such name as a claim to be confirmed against the decompiled body before it is graded above MEDIUM confidence. See [Methodology](methodology.md) for the adversarial cross-validation discipline.

---

## Standards & Specifications

The binary's layout and contents conform to a stack of public, stable standards. A reader does not need to re-derive name mangling, vtable layout, ELF relocation, or the protobuf wire format — these are the authoritative references the analysis decodes against. Each is cited precisely enough to locate.

### Binary Format & ABI

- **System V Application Binary Interface — AMD64 Architecture Processor Supplement** (the x86-64 psABI). The calling convention, register usage, stack layout, and the ELF-64 object format that `libtpu.so` and `sdk.so` are. Maintained at `gitlab.com/x86-psABI/x86-64-ABI`; the governing document for argument passing and the section/segment model the book reads. The large-code-model variant (`.lrodata`/`.lbss`) is specified here and is why the binary carries those sections.
- **Tool Interface Standard (TIS) — Executable and Linking Format (ELF) Specification, v1.2**, and the System V ABi gABI chapters 4–5. The ELF container: header, section header table, program header table, symbol table (`.symtab`/`.dynsym`), relocation types, and the note format (`NT_GNU_BUILD_ID`) from which the provenance build-ids are read.
- **Itanium C++ ABI** (`itanium-cxx-abi.github.io/cxx-abi/abi.html`). The cross-vendor C++ ABI that Linux toolchains implement: the **name-mangling** grammar (`_Z…`) that makes the `.symtab` identifiers demangle-able; the **vtable layout** (RTTI pointer at `vtable[-1]`, then the virtual function pointers); and the **RTTI / `type_info`** object layout (`__class_type_info`, `__si_class_type_info`, `__vmi_class_type_info` hierarchies). Every polymorphic-class and vtable claim in the forensics chapter is decoded against this document — see [RTTI/Vtable Census](forensics/rtti-vtable-census.md).
- **DWARF Debugging Information Format, v5** (`dwarfstd.org`). The standard for debug information. Relevant as a negative reference: the analyzed objects carry a `.symtab` but no full DWARF, which bounds what type information is recoverable and frames why structure layouts are reconstructed from access patterns rather than read from debug info.

### Serialization & IR Formats

- **Protocol Buffers — Encoding** (`protobuf.dev/programming-guides/encoding/`) and **`descriptor.proto`** (the self-describing schema language, `github.com/protocolbuffers/protobuf` → `src/google/protobuf/descriptor.proto`). The wire format (varints, tag = field-number ≪ 3 ∣ wire-type, length-delimited messages) and the descriptor schema that the embedded `FileDescriptorProto` blobs in `.rodata` are decoded with. The reconstructed-proto pages depend entirely on these two references.
- **LLVM Language Reference** (`llvm.org/docs/LangRef.html`) and **LLVM Bitcode File Format** (`llvm.org/docs/BitCodeFormat.html`). The IR and on-disk bitcode the embedded toolchain produces and parses; the vendored LLVM is **23-dev (trunk)**, monorepo commit `8918319853fbdf9e6f6cb69e96848f913a22bc31` — bounded from a sentinel-masked build, not read from a release banner (see [LLVM/MLIR Manifest](forensics/llvm-mlir-manifest.md)).
- **MLIR** — Language Reference, Bytecode format, and Dialect documentation (`mlir.llvm.org`). The multi-level IR infrastructure (Core IR, dialect registry, pass infra, bytecode reader/writer, conversion framework, LLVM-IR translation) compiled into `libtpu.so` alongside the TPU-specific dialects. Same monorepo commit as LLVM; there is no independent MLIR version number.
- **Zstandard (zstd) Compression Format** — RFC 8878 (`datatracker.ietf.org/doc/rfc8878/`) and `facebook/zstd`. The compression of the trailing data blob; see [Trailing Zstd Blob](forensics/trailing-zstd-blob.md).
- **Riegeli/records format** (`github.com/google/riegeli`). Google's record-oriented container format whose markers appear in the binary, layered over zstd framing.

> **NOTE (version anchoring) —** the embedded LLVM/MLIR major (**23-dev**) is the single inferred datum in the toolchain pin: it is bounded by the LLVM release-branch calendar and the build epoch, not read from a string (Google rewrites `LLVM_VERSION_MAJOR` to a `9999.0.0` sentinel). The exact reference is the **monorepo commit**, not a tagged release. A reimplementer targeting "LLVM 22" or "LLVM 23" tagged sources will see API drift. Full argument: [LLVM/MLIR Manifest § Version Pin](forensics/llvm-mlir-manifest.md).

---

## Public Technical References

These public references frame what the binary *is* — the host-side API it implements and the programming model it serves. They are documentation of the surrounding ecosystem, not of `libtpu.so`'s internals; the book uses them to name the concepts the reconstruction then grounds in addresses.

### PJRT — the plugin contract

- **PJRT C-API** (`github.com/openxla/xla` → `xla/pjrt/c/pjrt_c_api.h`). The stable C ABI that a PJRT plugin exports and a framework (JAX, XLA) calls. `libtpu.so` *is* a PJRT plugin; the entry-point surface the book documents is the binary's implementation of this header's function table. The version handshake, `PJRT_Client`/`PJRT_Buffer`/`PJRT_Executable` object model, and error convention are specified here.

### XLA / OpenXLA — the compiler the plugin embeds

- **OpenXLA / XLA** (`github.com/openxla/xla`, `openxla.org`). The accelerator-linear-algebra compiler whose TPU backend, HLO/StableHLO pipeline, and runtime are statically linked into `libtpu.so`. The reference for HLO semantics, the compilation pipeline shape, and the runtime concepts the forensics pages anchor to symbols.
- **StableHLO** (`github.com/openxla/stablehlo`). The portability layer / op-set spec that frames the high-level IR entering the embedded XLA compiler.

### TPU programming model

- **Cloud TPU System Architecture & Programming Model** (`cloud.google.com/tpu/docs`). The public description of the TPU execution model — the systolic MXU, vector/scalar units, memory spaces (HBM, VMEM/SMEM), and the SparseCore — that the LLO/Mosaic/SparseCore dialects in the binary target. Used to name memory spaces and execution units the [memory-space](appendix/memory-space-table.md) and dialect pages then ground in the binary.

### Supporting libraries

- **Abseil C++** (`abseil.io`, `github.com/abseil/abseil-cpp`). Google's base C++ library, statically linked throughout `libtpu.so` (`absl::` symbols, `Status`/`StatusOr`, `flat_hash_map`, `Span`, `Cord`). The reference for the container and status-handling idioms that pervade the recovered call graphs; pinned version is in the [Embedded-Library Atlas](forensics/embedded-library-atlas.md).
- **Eigen** (`eigen.tuxfamily.org`). Linear-algebra templates linked for CPU-side kernels (the bitcode-embedded math path, e.g. vectorized `tanh`).
- **Third-party notices** — `THIRD_PARTY_NOTICES.txt` (731,537 B) and `SDK_THIRD_PARTY_NOTICES.txt` (103,306 B), shipped inside the wheel. The authoritative published list of open-source components statically linked into the two objects, used only as an external cross-check on the embedded-library census.

---

## Cross-References

- [Methodology](methodology.md) — the full RE pipeline, the acquisition path, and the in-pipeline legal-basis statement this page formalizes
- [Evidence & Confidence Conventions](front/evidence-conventions.md) — the trust labels (CERTAIN/HIGH/MEDIUM/LOW) every page applies; the provenance table here is the CERTAIN floor
- [The Two-Binary Split](forensics/two-binary-split.md) — why `libtpu.so` and `sdk.so` are separate objects with different build-id formats
- [LLVM/MLIR Manifest](forensics/llvm-mlir-manifest.md) — the embedded LLVM 23-dev version pin and component census the standards section cites
- [Embedded-Library Atlas](forensics/embedded-library-atlas.md) — the statically-linked third-party census corroborated by the wheel's third-party notices
- [Source-Corpus Map](appendix/source-corpus-map.md) — the raw-findings file → part assignment underlying every page (sibling reference apparatus)
