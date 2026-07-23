# Glossary & Naming Conventions

> *Terms as used in `neuronx_cc` 2.24. Where a name is the compiler's own (a symbol, a module, a codename), that is noted; where it is this book's shorthand, that is noted too.*

## Abstract

The Neuron stack carries a dense, partly-internal vocabulary: a single data structure may be called one thing in a Python module, another in a C++ symbol, and a third in an error string. This page is the dictionary. It also fixes the naming conventions the rest of the book follows, so that "BIR", "Penguin", "KLR", and "the engines" mean exactly one thing throughout.

## Pipeline stages & IRs

| Term | Meaning |
|---|---|
| **HLO** | XLA's High-Level Optimizer IR — the `HloModule` protobuf that enters from PyTorch-XLA / JAX. |
| **MHLO / StableHLO / CHLO** | The MLIR dialects `hlo2penguin` ingests; StableHLO is the portable form, MHLO the in-memory XLA dialect, CHLO the "client" high-level ops. |
| **Penguin IR** | The mid-level, tile-and-loop SSA representation optimized by the middle-end. A Python object graph backed by Cython modules under `starfish/penguin/`. The internal codename for the tensorizer/mid-level layer. |
| **Tensorizer** | The transform layer that tiles tensor ops into loop nests over hardware tiles; the source of names like `CanonicalizeForTensorizer`. |
| **BIR** | Backend IR (`libBIR.so`, `bir::*`). One `bir::Instruction` per Tonga hardware instruction; a 110-entry opcode enum. The level `walrus` operates on and the JSON wire format serializes. |
| **KLR** | The "KLIR"/beta2 intermediate carried between the NKI tracer and the backend, lowered by `KlirToBirCodegen`. Retained but dormant for internal re-trace in this build (the live path is beta3). |
| **NEFF** | Neuron Executable File Format — the compiler's output container. A gzip-wrapped PAX tar of JSON + per-engine `.bin` members; **not** an ELF. |
| **kelf** | A TVM/nnvm `graph_runtime`-dialect JSON (`kelf-N.json`) embedded in the NEFF describing a subgraph. |

## Tools & libraries

| Term | Meaning |
|---|---|
| **hlo-opt** | The HLO/MLIR optimizer tool ELF; runs the `--passes` pipeline. |
| **hlo2penguin** | The MLIR front-half tool ELF; emits Penguin Python IR. |
| **walrus / libwalrus** | The backend. `walrus_driver` is the thin launcher; `libwalrus.so` is the logic. |
| **pelican** | `pelican::Expr` — the symbolic integer/address expression library underpinning affine indexing and dynamic shapes. |
| **birsim** | The BIR functional simulator (`libBIRSimulator.so`). |
| **PerfSim** | The cycle/cost simulator (`perf_sim.cpp` in `libwalrus`) backed by the `bir::Hwm` latency oracle. |
| **PWP** | Piecewise-polynomial — the activation-function approximation scheme (`bkt.bin` / `ctrl.bin` tables, `libpwp_sim.so`). |
| **NKI** | Neuron Kernel Interface — the Python kernel DSL (`nki.*`) that traces directly to Penguin IR. |

## Hardware: engines & memory

| Term | Meaning |
|---|---|
| **TPB** | Tensor Processing Block — the NeuronCore compute complex containing the engines. |
| **PE** | The Processing Element array — the systolic matmul engine. |
| **Pool** | The pooling/reduction engine. **GPSIMD** is an external alias of Pool (see below). |
| **Activation (Act)** | The activation engine; evaluates PWP functions via on-chip LUTs loaded by `LoadActFuncSet`. |
| **DVE** | The vector/data engine, driven by an on-device microcode table. |
| **SP** | The control processor — the TPB's sequencer; runs register-lane ops, branches, sync. |
| **GPSIMD** | **Two distinct things** sharing a name: (1) the *external alias* of the internal **Pool** engine, surfaced as `InstGPSIMDSB2SB` (cross-core SB-to-SB); (2) the **8-core Xtensa custom-op CPU cluster** that runs user custom ops. They are unrelated units; see [11.9](custom-ops/two-gpsimd-reconciliation.md). |
| **SBUF** | State Buffer — the on-core scratchpad SRAM; **replicated** per logical neuron core. |
| **PSUM** | Partial-Sum buffer — the matmul accumulator banks; replicated per core. |
| **DRAM / HBM** | Off-core high-bandwidth memory; **partitioned** across logical cores (contrast SBUF/PSUM). |
| **LNC** | Logical NeuronCore — the multi-core split of a physical NeuronCore; the backend splits the BIR graph across LNCs (`lnc_splitter`) and re-links per-core streams (`bir_linker`). |
| **VNC** | The cross-core link layer (`vnc_remote_addr_map`, `vnc_link`) that wires LNC cores together. |
| **DGE** | Dynamic Generation Engine — the descriptor-generation level for dynamic-offset DMA. |

## Codenames & generations

The compiler names targets two ways — a silicon codename and an ISA-generation codename — and the mapping is decoded in [1.02](arch/codename-taxonomy.md).

| Term | Meaning |
|---|---|
| **Tonga / Sunda / Cayman / Mariana** | Per-silicon cost-model / target codenames (four chronological generations). |
| **CoreV2Gen / CoreV3Gen / CoreV4Gen** | The three ISA-generation code generators in `libwalrus` (`CoreV{2,3,4}GenImpl::visitInst<Op>`). |
| **CoreV1 / CoreV5** | Vestigial generations present in enums but not actively targeted by this build; see [1.03](arch/vestigial-generations.md). |
| **trn1 / inf2 / trn2 / inf1 / trn3** | The user-facing instance/target arguments that select a backend. |

## Numerics

| Term | Meaning |
|---|---|
| **MX / MX-FP8** | OCP Microscaling — the block-scaled low-precision format (E8M0 per-block scale over FP8 elements). The **only** low-precision quantize that reaches silicon. |
| **E8M0** | The 8-bit exponent-only block-scale type used by MX. |
| **RNE** | Round-to-nearest-even — the device rounding mode everywhere (there is no stochastic rounding in this build). |
| **x4-packing** | The 4-elements-per-word packing of sub-byte dtypes in BIR's dtype model. |
| **golden** | The reference (CPU oracle) numeric path, via `xla_infergoldens` / oneDNN; distinct from the device path (e.g. int8 quantize is golden-only). |

## This book's conventions

- **`sub_ADDR`** — a function whose symbol was stripped, named by address. A readable name, when assigned, keeps the `sub_ADDR` in a comment.
- **`base+0xNN`** — a byte offset into a struct or instruction bundle.
- **CERTAIN / HIGH / MEDIUM / LOW** — the confidence ladder, used in a table's Confidence column; see [Methodology](methodology.md). Certainty is the default, so prose marks only doubt, with `[INFERRED]` / `[SPECULATIVE]` / `[UNRESOLVED]`.
- **"binary-derived"** — every claim is from static analysis of the pinned build; recovered identifier strings count as binary evidence.

## Cross-References

- [The Compile Pipeline at a Glance](front/pipeline.md) — the stages these terms name.
- [Methodology & the Confidence Model](methodology.md) — the confidence vocabulary.
- [Codename ↔ device ↔ generation taxonomy](arch/codename-taxonomy.md) — the full codename decode *(planned)*.
