# Build & Version Provenance

> *This entire wiki is pinned to one build. This page records exactly which one, and the parity argument that lets cp310-derived findings stand for cp311/cp312.*

## Abstract

Reverse-engineering claims are only as good as their version pin. Every address, offset, and symbol elsewhere in the book is read from a single build of `neuronx_cc`, and this page fixes its identity, decodes the version string, and establishes why the three Python-version wheels can be treated as one artifact for the C++ core. It also gives the conventions for *how* a page is allowed to cite a build, which keeps the whole book internally consistent.

## The pinned build

| | |
|---|---|
| **Package** | `neuronx_cc` (the AWS Neuron compiler) |
| **Version string** | `2.24.5133.0+58f8de22` |
| **Wheels analyzed** | `cp310-cp310`, `cp311-cp311`, `cp312-cp312` (manylinux x86_64) |
| **Local-version tag** | `58f8de22` — the source-revision identifier carried as the PEP 440 local segment |
| **Primary tool image** | the ~230 MB multi-call ELF (`hlo-opt` / `hlo2penguin` / `hlo-neff-wrapper` / `snapshot-unpack` / `xla_infergoldens`) |
| **Primary backend library** | `libwalrus.so`, 64,973,024 bytes (cp310/cp311); 64,968,928 bytes (cp312) |

### Decoding `2.24.5133.0+58f8de22`

- `2.24` — the marketing/release line.
- `5133.0` — the build number within the line.
- `+58f8de22` — the PEP 440 *local version label*: an eight-hex-digit revision tag that uniquely identifies the source snapshot the wheel was built from. It is the most precise handle on the build and is the value to quote when reporting which artifact a finding came from.

> **NOTE —** `58f8de22` is the build's own identifier, recovered from the package metadata; it is unrelated to any ELF `NT_GNU_BUILD_ID`. Where a specific binary's GNU build-id matters (e.g. to distinguish cp310 from cp312), it is given on the page that needs it; the consolidated table lives in [Appendix 14.8](../appendix/build-id-table.md) *(planned)*.

## The cp310 / cp311 / cp312 parity argument

Three wheels ship, one per CPython ABI. The book draws addresses from the **cp310** artifacts unless a page says otherwise. That is safe because the split is along a clean line:

- **The C++ tool ELFs and `starfish/lib/*.so` are Python-version-independent.** They contain no CPython API and are byte-identical across the three wheels — in the extracted tree the five big tools appear as **hardlinks** (link-count 3), one inode shared by cp310/cp311/cp312. `libwalrus.so` is identical between cp310 and cp311 and differs from cp312 by ~4 KB (a build-stamp-level delta, not a logic change). So any finding read from `libwalrus.so`, `libBIR.so`, the simulators, or the tool ELFs holds for all three wheels.
- **The Cython `*.cpython-3xx-*.so` modules are per-ABI.** They embed the CPython C-API for their version and therefore differ in size and layout across cp310/311/312 (e.g. `KernelBuilder` is 14.6 MB on cp310 vs 17.3 MB on cp311). The *logic* is the same Cython source compiled three ways; the symbol and string evidence a page cites from one ABI is present in the others, but a raw offset is ABI-specific. Pages that cite Cython-module offsets state the ABI.

> **QUIRK —** the size ordering of the Cython modules is not monotone in Python version (cp311's `KernelBuilder` is larger than cp312's). This reflects compiler/codegen differences between the CPython toolchains used to build each wheel, not a difference in the compiler's behavior. Treat module size as an artifact property, not a feature signal.

## How pages cite a build

- A page that cites addresses opens with a version-pin blockquote naming this build. The version is stated once, at the top, never repeated per address.
- A claim read from a Python-independent binary (tools, `lib*.so`) needs no ABI qualifier — it holds across wheels.
- A claim read from a Cython module names the ABI (`cp310`) when it cites a raw offset; symbol- and string-level claims are ABI-agnostic and need no qualifier.
- When two wheels disagree on a value, the disagreement is reported in place with both numbers, not silently resolved to one.

## Cross-References

- [Binary Inventory & the .so Map](binary-inventory.md) — the artifacts this provenance applies to, with sizes.
- [Methodology & the Confidence Model](../methodology.md) — how a pinned build underpins every confidence claim.
- [Build-ID / Version Table](../appendix/build-id-table.md) — the consolidated per-binary build identifiers *(planned)*.
