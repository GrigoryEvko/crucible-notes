# neuronx-cc Internals

> *A reverse-engineering reference for `neuronx_cc` **2.24.5133.0+58f8de22** (the AWS Neuron compiler), reconstructed from static analysis of the cp310/cp311/cp312 wheels. Every address, offset, and symbol on these pages is pinned to that build.*

## What this wiki is

`neuronx_cc` is the ahead-of-time compiler that turns a machine-learning graph into a **NEFF** — the executable container the Neuron runtime (`libnrt.so`) loads onto Trainium / Inferentia NeuronCore devices. Its two front doors are XLA **HLO** (emitted by PyTorch-XLA or JAX through `libneuronpjrt`) and **NKI**, a Python kernel DSL that traces directly to the backend IR.

This book is the specification a competent systems engineer would need to **reimplement** that compiler. It documents the whole descent — HLO/StableHLO optimization, the `hlo2penguin` MLIR front half, the Penguin middle-end, NKI tracing and lowering, the BIR backend IR, the `libwalrus` backend (scheduling, register/memory allocation, per-engine code generation, multi-core linking), and the on-disk NEFF format — anchored at every step to the binary evidence that supports it.

Everything here derives from static analysis alone: demangled symbols, decompiled function bodies, `pybind11` and Cython string pools, embedded assert messages, and recovered `__FILE__` source paths inside the shipped binaries. There is no access to source. Where a claim is inferred rather than directly observed, it is labelled; see [Methodology & the Confidence Model](methodology.md).

## Start here

1. **[The Compile Pipeline at a Glance](front/pipeline.md)** — the IR descent from framework graph to NEFF, with the binary that owns each stage.
2. **[Binary Inventory & the .so Map](reference/binary-inventory.md)** — the tool ELFs, the eight `starfish/lib` shared objects, and the Cython module galaxy.
3. **[Methodology & the Confidence Model](methodology.md)** — what "binary-derived" means here, the four-tier confidence ladder, and what is provably *not* recoverable.
4. **[Glossary & Naming Conventions](glossary.md)** — NEFF, BIR, Penguin, KLR, walrus, pelican, the engines, the codenames.

## How the book is organized

The reference is fifteen parts. Part 0 (this apparatus) orients; Parts 1–14 descend the pipeline and then bottom out in cross-cutting references.

| Part | Subject | Directory |
|---|---|---|
| 0 | Reference apparatus — pipeline, methodology, inventory, glossary | — |
| 1 | Hardware & engine model (the arch object model, the six engines, LNC) | `arch/` |
| 2 | The Tonga ISA — the 64-byte bundle, access-pattern descriptors, per-engine encodings | `isa/` |
| 3 | Frontend, driver, flags & diagnostics | `frontend/` |
| 4 | `hlo-opt` + `hlo2penguin` — HLO/MLIR optimization and Penguin emission | `hlo-opt/` |
| 5 | Penguin IR & the middle-end (layout, tiling, fusion, scheduling, ISL glue) | `penguin/` |
| 6 | NKI — the Python kernel DSL, tracing, lowering, and the production kernel library | `nki/` |
| 7 | BIR, `libBIR`, the JSON wire format, `pelican::Expr`, and the simulator | `bir/` |
| 8 | The `libwalrus` backend — passes, allocators, schedulers, codegen, linker, verifiers, perf model | `walrus/` |
| 9 | Numeric semantics — dtypes, the cast engine, MX microscaling | `numerics/` |
| 10 | Activation & the piecewise-polynomial (PWP) function tables | `activation/` |
| 11 | Custom ops & the GPSIMD Xtensa CPUs | `customop/` |
| 12 | The NEFF container & packaging | `formats/` |
| 13 | Distribution, collectives & SPMD partitioning | `distribution/` |
| 14 | Appendices — opcode/dtype/error/symbol reference tables | `appendix/` |

> **NOTE —** pages land part-by-part. The navigation sidebar lists what is written; the full ~355-page plan is tracked separately, one task per page. A section that is not yet linked is planned, not missing.

## Companion wikis

- [`neuronx-runtime/wiki/`](../../neuronx-runtime/wiki/) — the runtime that loads and executes NEFFs.
- [`neuron-jax-stack/wiki/`](../../neuron-jax-stack/wiki/) — the `libneuronpjrt` → `neuronx_cc` subprocess contract.
- [`neuronx-misc/wiki/`](../../neuronx-misc/wiki/) — `torch_neuronx` / `jax_neuronx` framework bindings that drive compilation.
