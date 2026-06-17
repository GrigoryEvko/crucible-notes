# Methodology & the Confidence Model

> *Applies to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310/cp311/cp312 wheels). Every claim in this book is the product of static analysis of those artifacts.*

## Abstract

This wiki is reverse engineering, and its credibility rests on being explicit about how each fact was obtained and how certain it is. This page fixes the conventions every other page follows: what counts as evidence, how addresses and symbols are cited, the four-tier confidence ladder, and — just as important — the catalog of things that are provably *not* recoverable from the shipped binaries, so a reader never mistakes an honest gap for an oversight.

The reader to keep in mind is a systems engineer who knows compilers, SSA, and ISA encoding but has never opened these binaries, and who wants to **rebuild** the component. Pages are written to that bar: enough algorithm, layout, and rationale to reimplement, not a transcript of decompiler output.

## The evidence base

Everything derives from the three Python wheels and the binaries inside them. No source, no debug symbols beyond what shipped, no vendor documentation. The recoverable evidence channels, in rough order of strength:

| Channel | What it yields | Typical confidence |
|---|---|---|
| Demangled C++ symbols (`.dynsym`) | Class names, method names, signatures — `libwalrus.so` and the `lib*.so` retain demangled `CoreV*GenImpl::visitInst<Op>` etc. | High — names are authoritative |
| Decompiled function bodies | Control flow, struct offsets, store sequences, magic constants | High where the body is clean; lower in heavily-inlined code |
| `pybind11` / Cython string pools | Argument names, keyword names, field names, error literals | High — these are the real source identifiers |
| Embedded assert / error strings | Field names, legality conditions, recovered `__FILE__` source paths | High for what the string asserts |
| `*_info.so` data-model modules | op → struct-arm bindings, enum rosters | High |
| Cross-binary symbol matrix | Which binary owns a class; version parity across cp310/311/312 | High |

> **NOTE —** recovered identifier strings *are* binary evidence. A `pybind11` keyword like `act_func_set_id`, a Cython `pyx_n_s_` name, an assert literal, or a `__FILE__` path such as `neff_packager.cpp` is a byte sequence in the shipped binary, and citing it is no different from citing an offset. These are read off the artifact, not imported from any external tree.

### There is no DWARF

With one notable exception, the C++ binaries ship **stripped of debug info**. Addresses therefore come from disassembly of *named* functions: a symbol gives the entry point, and the body is read from there. For the big tools and `lib*.so`, the virtual address equals the file offset in `.text`/`.rodata` (the `.data` section carries a fixed delta and is called out where it matters).

> **QUIRK —** the exception is `KernelBuilder.cpython-3xx.so` (the NKI forward code generator), which ships **with full Cython debug info** — the most readable binary in the stack. Its DWARF line table maps every method to `KernelBuilder.py` line ranges. Pages in [Part 6](nki/) lean on this; pages elsewhere do not have the luxury.

## Citing evidence

- **Functions** are named by their demangled symbol where one exists (`bir::CastToNewDType`, `CoreV3GenImpl::visitInstMatmult`); by `sub_ADDR` only where the symbol was stripped. When a readable name is assigned to a `sub_`, the raw address is kept in a comment so a verifier can cross-check.
- **Addresses** are bare hex pinned to the build at the top of each page; the version is stated once, not repeated per address.
- **Offsets** into a struct or bundle are written `base+0xNN`.
- **Pseudocode** in `### Algorithm` blocks is annotated C that names the real function it models and cites offsets/labels in comments — never raw decompiler output with `v17`/`a3` as the only names.

## The confidence ladder

Reverse-engineered claims carry one of four tags. The same ladder appears as the **Confidence** column in function-map and struct-layout tables, and inline (e.g. `(INFERRED)`) in prose. A page with no confidence signal would read as uniformly authoritative, which is dishonest for this material.

| Tag | Meaning | Reimplementer should |
|---|---|---|
| **CONFIRMED** | Directly observed — a symbol, a decompiled store, an asserted field, a roster entry. | Trust verbatim. |
| **STRONG** | Multiple independent signals agree, but the decisive line was not read end-to-end. | Trust, spot-check the boundary. |
| **INFERRED** | Deduced from surrounding structure, naming, or analogy to a confirmed sibling. | Re-derive before relying on it. |
| **SPECULATIVE** | A plausible reading consistent with the evidence but unproven. | Treat as a hypothesis to test. |

These map onto the house style guide's `CERTAIN / HIGH / MEDIUM / LOW` one-to-one (CONFIRMED≈CERTAIN, STRONG≈HIGH, INFERRED≈MEDIUM, SPECULATIVE≈LOW). This book standardizes on the four-tier names above because the underlying analysis used them.

When later analysis overturns an earlier claim, the page carries an in-place correction note rather than a silent edit:

> **CORRECTION —** an example of the form. The original reading is stated, the corrected one replaces it, and the reason is given, so a reader who cached the old fact sees the change.

## What is *not* recoverable

These bounds were established during analysis and are honored throughout. Where a page touches one of them, it says so rather than guessing.

- **GPSIMD Xtensa instruction bodies.** The eight custom-op CPU ELFs are Xtensa, and the wheel ships **no Xtensa disassembler and no Xtensa toolchain**. The CPU *code* — instruction bodies, the `SUNDA_APB_BASE` numeric, the micro-register map — is not recoverable. The evidence channel for [Part 11](customop/) is rodata, strings, and cross-references only; the embedded SORT/TOPK builtins are documented to the extent their assert/rodata strings allow.
- **The profiler back end.** `perfetto` traces and the `.ntff` format do not ship here; their consumer is an out-of-wheel Go binary. What *is* in-wheel and documented: the `perf_sim.cpp` cost model, the `debug_info_pttf` trace tarball, `metricslibrary::MetricStore`, and the DMA/PGA metrics.
- **ISL internals.** The polyhedral layer is **stock `islpy ~=2023.1`** (a pip dependency, declared in `METADATA` and `THIRD-PARTY-LICENSES-ISL.txt`). The integer-set algebra, the Pluto-style scheduler, and the AST generator are off-the-shelf; only the Penguin⇄ISL *glue* is custom, and only the glue is documented ([Part 5](penguin/)).

## Confirmed negative results

Some of the most useful findings are absences — features a reimplementer might expect that this build does not have:

- **No stochastic rounding.** Only the upstream XLA `kStochasticConvert` string survives; the device path is round-to-nearest-even everywhere (the `CastToNewDType` engine and the simulator both confirm RNE). See [Part 9](numerics/).
- **No int8 device quantize.** INT8 uniform-quantize/dequantize legalization targets the XLA:CPU **golden** path (via oneDNN); the only low-precision quantize that reaches silicon is **MX-FP8 microscaling** (E8M0 block scale). The two are kept strictly separate in [Part 4](hlo-opt/) and [Part 9](numerics/).
- **No on-disk NEFF "magic".** NEFF is a gzip-wrapped PAX tar, not an ELF and not a bespoke container; the byte at offset 0 is the gzip magic. See [Part 12](formats/).

## Cross-References

- [The Compile Pipeline at a Glance](front/pipeline.md) — the stage map this confidence model annotates.
- [Binary Inventory & the .so Map](reference/binary-inventory.md) — the artifacts the evidence is read from.
- [Build & Version Provenance](reference/versions.md) — how the build is pinned and the cp310/311/312 parity argument.
- [Confidence Ledger](appendix/confidence-ledger.md) — the consolidated list of the book's weakest-evidence claims and structure-only pages.
