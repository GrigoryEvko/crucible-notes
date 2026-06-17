# Summary

[neuronx-cc Internals](index.md)

---

# Part 0 — Reference Apparatus

- [The Compile Pipeline at a Glance](front/pipeline.md)
- [Worked Example A — a matmul end-to-end](front/worked-example-matmul.md)
- [Worked Example B — a flash-attention end-to-end](front/worked-example-flash-attention.md)
- [Methodology & the Confidence Model](methodology.md)
- [Binary Inventory & the .so Map](reference/binary-inventory.md)
- [Build & Version Provenance](reference/versions.md)
- [Glossary & Naming Conventions](glossary.md)

# Part 1 — Hardware & Engine Model

- [The Arch Object Model (getArchModel → Board/Device/Core)](arch/arch-object-model.md)
- [Codename ↔ Device ↔ Generation Taxonomy](arch/codename-taxonomy.md)
- [Vestigial Generations — CoreV1 (Inferentia) & CoreV5](arch/vestigial-generations.md)
- [Per-Generation Hardware-Constant Matrix](arch/hardware-constant-matrix.md)
- [SBUF / PSUM Bank Geometry](arch/sbuf-psum-geometry.md)
- [DRAM / HBM Geometry & the DRAM Split](arch/dram-hbm-geometry.md)
- [The multi-core (LNC) memory model](arch/lnc-memory-model.md)
- [PE Engine — the Systolic Matmul Array](arch/pe-engine.md)
- [Activation Engine — Datapath and the LUT-Load Mechanism](arch/activation-engine.md)
- [Pool Engine — Windowed Pooling and the Reduce Leg](arch/pool-engine.md)
- [DVE Engine — Microcode-Table Architecture](arch/dve-engine.md)
- [GPSIMD Engine — the Pool-Alias Cross-Core SB2SB Mover](arch/gpsimd-engine.md)
- [SP Engine — the TPB Control Processor](arch/sp-engine.md)
- [Execution & Sync Model — Semaphores & Barriers](arch/execution-sync-model.md)

# Part 2 — The Tonga ISA

- [The 64-Byte Instruction Bundle & Header Skeleton](isa/instruction-bundle.md)
- [ADDR4 — the 32-Bit Address Word](isa/addr4.md)
- [TENSOR1D / 2D / 3D Descriptors — the 4+4N Rule](isa/tensor-descriptors.md)
- [TENSOR4D / MEM_PATTERN4D — the Spill Descriptor](isa/tensor4d-mempattern4d.md)
- [MEM_PATTERN2D / 3D — the DST/PSUM Role](isa/mempattern-2d-3d.md)
- [MXMEM_PATTERN1D — MX Data + E8M0 Scale](isa/mxmem-pattern1d.md)
- [Indirect-Gather Descriptors — INDIRECT16B / 20B / MXINDIRECT16B](isa/indirect-descriptors.md)
- [Access-Pattern Encoder Dispatch](isa/ap-encoder-dispatch.md)
- [NEURON_ISA_TPB Struct-Family Capstone (the .h)](isa/neuron-isa-tpb-capstone.md)
- [PE Matmul Encoding — Dense / Sparse / MX & Quantize](isa/pe-matmul-encoding.md)
- [BatchNorm-Family Encoding](isa/batchnorm-encoding.md)
- [Pool / TensorReduce / Reciprocal / Iota Encoding](isa/pool-reduce-encoding.md)
- [Activation Encoding](isa/activation-encoding.md)
- [TensorScalar / Cumulative / ScalarTensorTensor / Exp Encoding](isa/tensorscalar-encoding.md)
- [TensorTensor / Copy / Cast / Select / Memset / MoveShape Encoding](isa/tensortensor-encoding.md)
- [RNG-Family Encoding](isa/rng-encoding.md)
- [DVE Datamove / Misc Encoding — Shuffle / Transpose / Gather / Dropout / IndirectCopy / RangeSelect](isa/dve-datamove-encoding.md)
- [DVE Search & Datamove Encoding — Max8 / FindIndex8 / MatchReplace / Nonzero](isa/dve-search-encoding.md)
- [SP Register-Lane + TensorLoad/Save Encoding](isa/sp-register-encoding.md)
- [SP Sync / Branch / Control Encoding](isa/sp-sync-encoding.md)
- [DMA-Family Encoding & Descriptors](isa/dma-encoding.md)
- [Collective / GPSIMD / CustomOp Encoding](isa/collective-customop-encoding.md)
- [ISA Numeric Enum-Ordinal Tables](isa/isa-enum-ordinals.md)
- [The ISA Datamodel Reflection Layer](isa/isa-reflection-layer.md)
- [DVE On-Device Microcode: opcode_table](isa/dve-opcode-table.md)
- [DVE On-Device Microcode: control_table](isa/dve-control-table.md)
- [DVE On-Device Microcode: datapath_table](isa/dve-datapath-table.md)
- [DVE 0xF1/0xF2 Engine-Migration Reconcile](isa/dve-engine-migration.md)

# Part 3 — Frontend, Driver & Diagnostics

- [The neuronx-cc Command Dispatcher & Subcommand Model](frontend/command-dispatcher.md)
- [The Two-Parser Architecture](frontend/two-parser-architecture.md)
- [CompileCommand Pipeline & the Canonical Job Order](frontend/compilecommand-pipeline.md)
- [JobRegistry & the Sub-Tool Process Model](frontend/job-registry.md)
- [Sub-Tool argv Construction & Replay](frontend/subtool-argv.md)
- [hlo2penguin Entry & the Native cl::opt Surface](frontend/hlo2penguin-entry.md)
- [walrus_driver Backend CLI & Pass Vocabulary](frontend/walrus-driver-cli.md)
- [CompileCommand Flag Catalog — the 147-Flag Surface](frontend/flag-catalog.md)
- [Flag Visibility Taxonomy (ArgKind)](frontend/flag-visibility-argkind.md)
- [The Opt-Level Planes — -O0..-O3 and the Three Hidden Dials](frontend/opt-level-planes.md)
- [Environment-Variable Catalog & the NEURON_CC_FLAGS Boundary](frontend/env-var-catalog.md)
- [Defaults & Config-File Model — the Absence of a Config File](frontend/defaults-config-model.md)
- [Framework Bindings — JAX, PyTorch-XLA & Custom-Call Targets](frontend/framework-bindings.md)
- [Frontend Precision-Flag Marshalling](frontend/precision-flag-marshalling.md)
- [StaticIOTranspose & the io_transpose JSON Schema](frontend/static-io-transpose.md)
- [xla_infergoldens — the Reference Evaluator](frontend/xla-infergoldens.md)
- [Snapshot / Decomposed Input Format](frontend/snapshot-input-format.md)
- [NeuronLogger — the C++ Logging Singleton & Boost.Log Sinks](frontend/neuronlogger.md)
- [Python Logging Façade & Log-Level Mapping](frontend/python-logging-facade.md)
- [Diagnostic & Error-Code Catalog — Four Systems](frontend/diagnostic-error-catalog.md)
- [Optional Client/Daemon IPC Server](frontend/client-daemon-ipc.md)

# Part 4 — hlo-opt + hlo2penguin

- [The hlo-opt Pass Registry (the --passes Table)](hlo-opt/pass-registry.md)
- [HLO/mhlo/stablehlo Ingestion & the Stock-vs-Neuron Boundary](hlo-opt/hlo-ingestion-boundary.md)
- [Collectives → Custom-Call Forward Conversion](hlo-opt/collectives-to-customcall.md)
- [Collective Stream-ID & Channel-ID Family](hlo-opt/collective-stream-channel-id.md)
- [AllReduce/ReduceScatter/AllGather Combiners & Threshold Model](hlo-opt/collective-combiners.md)
- [Flip-Collective OpExpander Family](hlo-opt/flip-collective-opexpander.md)
- [AllReduce→ReduceScatter & DynamicSlice Rewrites](hlo-opt/allreduce-dynslice-rewrites.md)
- [CollectivePermute → AllGather Lowering](hlo-opt/collectivepermute-to-allgather.md)
- [Looped-Einsum → Collective-Matmul Fusion](hlo-opt/looped-einsum-collective-matmul.md)
- [Integer All-Reduce & Scalar-Reduce Decomposition](hlo-opt/int-scalar-reduce-decomposition.md)
- [While-Loop Unroll & All-Gather Trip-Count Rewrite](hlo-opt/whileloop-unroll-tripcount.md)
- [Boundary Markers & Layer-Cut Analysis](hlo-opt/boundary-markers-layer-cut.md)
- [While-Loop Collective Code-Motion](hlo-opt/whileloop-collective-codemotion.md)
- [CC-Op Decompose & Legalize Family](hlo-opt/ccops-decompose-legalize.md)
- [Control-Dependency Reification (HLO→MLIR→Penguin)](hlo-opt/control-dep-reification.md)
- [Layout Passes](hlo-opt/layout-passes.md)
- [Input/Output Alias Family](hlo-opt/io-alias-family.md)
- [Concatenation Optimizations](hlo-opt/concat-optimizations.md)
- [DUS/DS Simplifier & DynamicSlice Mover](hlo-opt/dus-ds-simplifier.md)
- [Duplicate-Parameter & Kernel Dedup Passes](hlo-opt/dedup-passes.md)
- [InstCombine Peephole Passes](hlo-opt/instcombine-peephole.md)
- [HLO Misc & Cleanup Sweep](hlo-opt/hlo-misc-cleanup-sweep.md)

<!-- Roadmap: pages below land part-by-part as they are written.
     The full 355-page plan is tracked in the task board (one task per page).
     Section headers are kept here so the book's shape is visible from day one.

# Part 1 — Hardware & Engine Model            (arch/)        14 pages
# Part 2 — The Tonga ISA                        (isa/)         28 pages
# Part 3 — Frontend, Driver & Diagnostics       (frontend/)    21 pages
# Part 4 — hlo-opt + hlo2penguin                (hlo-opt/)     45 pages
# Part 5 — Penguin IR & Middle-End              (penguin/)     27 pages
# Part 6 — NKI Kernel DSL                        (nki/)         61 pages
# Part 7 — BIR, libBIR & the Simulator          (bir/)         41 pages
# Part 8 — The libwalrus Backend                (walrus/)      52 pages
# Part 9 — Numeric Semantics                     (numerics/)    10 pages
# Part 10 — Activation & PWP                     (activation/)   7 pages
# Part 11 — Custom Ops & GPSIMD                  (customop/)    10 pages
# Part 12 — NEFF Container & Packaging           (formats/)      8 pages
# Part 13 — Distribution & Collectives           (distribution/) 11 pages
# Part 14 — Appendices                           (appendix/)    12 pages
-->
