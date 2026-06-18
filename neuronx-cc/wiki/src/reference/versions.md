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
| **Standalone tool ELFs** | five distinct ~230 MB statically-linked tools (`hlo-opt`, `hlo2penguin`, `hlo-neff-wrapper`, `snapshot-unpack`, `xla_infergoldens`) — separate binaries, each hardlinked across the three wheels |
| **Primary backend library** | `libwalrus.so`, 64,973,024 bytes (cp310/cp311); 64,968,928 bytes (cp312) |

### Decoding `2.24.5133.0+58f8de22`

- `2.24` — the marketing/release line.
- `5133.0` — the build number within the line.
- `+58f8de22` — the PEP 440 *local version label*: an eight-hex-digit revision tag that uniquely identifies the source snapshot the wheel was built from. It is the most precise handle on the build and is the value to quote when reporting which artifact a finding came from.

> **NOTE —** `58f8de22` is the build's own identifier, recovered from the package metadata; it is unrelated to any ELF `NT_GNU_BUILD_ID`. Where a specific binary's GNU build-id matters (e.g. to distinguish cp310 from cp312), it is given on the page that needs it; the consolidated table lives in [Appendix 14.8](../appendix/build-id-version-table.md).

## The cp310 / cp311 / cp312 parity argument

Three wheels ship, one per CPython ABI. The book draws addresses from the **cp310** artifacts unless a page says otherwise. That is safe because the split is along a clean line:

- **The C++ binaries are Python-version-independent in their logic, but parity across the three wheels is not uniform — and the distinction matters for citing addresses.** The standalone **tool ELFs** in `bin/` *are* byte-identical across wheels: each is a single inode hardlinked into all three wheel directories (link-count 3), so a finding read from a tool holds verbatim for cp310/cp311/cp312. The **shared libraries** in `lib/` are *not* hardlinked and *not* byte-identical across wheels: `libwalrus.so` is the same *size* on cp310 and cp311 (64,973,024 bytes) but a different SHA-256 (`75cf23f5…` vs `49c63f9e…`), and cp312 is 4,096 bytes smaller (64,968,928). Same size with different bytes on cp310/cp311 indicates a per-wheel build stamp rather than a logic change, but a page must not claim a bit-identity it has not shown. Therefore: addresses on these pages are read from **cp310**; they carry to cp311 only after a per-symbol spot-check, and must be re-confirmed against cp312, where even the size differs.

### Per-binary build identifiers (cp310)

The GNU `NT_GNU_BUILD_ID` is the unambiguous per-binary anchor — independent of the package version label and distinct per wheel where the binary is rebuilt.

| Binary (cp310) | GNU build-id |
|---|---|
| `libwalrus.so` | `92b4d331a42d7e80bb839e03218d2b9b0c23c346` |
| `libBIR.so` | `a9b1ea38c47e579178b179fd445aa8edd593f206` |
| `libBIRSimulator.so` | `f1c6885f176c0d3c5d3a804b0f0b714ae7814ea6` |
| `hlo-opt` (tool) | `93dd8bd9bd4c697b` (8-byte) |

The shared-library build-ids differ on cp311/cp312 (the SHA differs); the consolidated cross-wheel build-id table is [Appendix 14.8](../appendix/build-id-version-table.md).
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
- [Build-ID / Version Table](../appendix/build-id-version-table.md) — the consolidated per-binary build identifiers.
