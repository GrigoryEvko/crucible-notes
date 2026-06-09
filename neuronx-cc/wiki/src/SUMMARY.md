# Summary

[neuronx-cc Internals — AWS Neuron Compiler](index.md)

---

# Reference Apparatus

- [Methodology](methodology.md)
- [Subsystem Map](subsystem-map.md)
- [Glossary](glossary.md)
- [Versions](versions.md)
- [Binary Layout](reference/binary-layout.md)
- [Extraction Gaps](reference/extraction-gaps.md)

# Architecture

- [Compiler Pipeline Overview](arch/overview.md)
- [Process Topology (Driver, Daemon, Wrappers)](arch/process-topology.md)
- [Codename Decoder (Tonga/Sunda/Cayman/Mariana + CoreV1-V5)](arch/codename-decoder.md)
- [NeuronCore Generations (CoreV2Gen / CoreV3Gen / CoreV4Gen)](arch/core-generations.md)
- [Hardware Models (Boards, Devices, Pools, PSUM Bufs)](arch/hardware-models.md)
- [TPB Engines and LNC](arch/tpb-engines-lnc.md)

# HLO / MHLO / StableHLO Layer

- [Neuron HLO Custom Calls](hlo/neuron-custom-calls.md)
- [NeuronHloVerifier](hlo/neuron-hlo-verifier.md)
- [NeuronHloInstCombine](hlo/neuron-hlo-inst-combine.md)
- [Neuron Collective Combiners](hlo/collective-combiners.md)
- [Kernel Duplicate Remover](hlo/kernel-duplicate-remover.md)
- [Legalize Passes (CCOps, ArgMax)](hlo/legalize-passes.md)

# MLIR Front Half (hlo2penguin)

- [CHLO → StableHLO → MHLO Pipeline](mlir/dialect-pipeline.md)
- [CanonicalizeForTensorizer](mlir/canonicalize-for-tensorizer.md)
- [TensorizerLegalizationPass](mlir/tensorizer-legalization.md)
- [NeuronOpFusion (dot+logistic, elementwise)](mlir/neuron-op-fusion.md)
- [NeuronInstCombine and ControlDepTupleSimplifier](mlir/inst-combine.md)
- [PenguinizeFunctions](mlir/penguinize-functions.md)
- [MhloToPyPenguin / StableHLOToPyPenguin](mlir/mhlo-to-py-penguin.md)
- [Python Emission (MhloToPythonPrinter)](mlir/python-printer.md)

# BIR (Backend IR)

- [BIR Module and Function Model](bir/module-and-function.md)
- [Instruction Hierarchy (110 opcodes)](bir/inst-hierarchy.md)
- [Tensor Type System (SB / PSUM / DRAM / Block / Local / Weight)](bir/tensors.md)
- [Memory Locations and Access Patterns](bir/memory-locations.md)
- [Loop Axes and QuasiAffineExpr](bir/affine-indexing.md)
- [Hwm (Hardware Model) and Latency Tables](bir/hwm-model.md)
- [BIR JSON SerDe (schema version 0)](bir/json-serde.md)

# Walrus Backend Pipeline

- [BackendPassManager and Pass Registration](walrus/pass-manager.md)
- [Pass Inventory (60+ register_generator_*)](walrus/pass-inventory.md)
- [TBB-Parallel Forking (Module / Core / Subgraph)](walrus/forking.md)
- [Verifier and Hasher](walrus/verifier-hasher.md)
- [NKI Inlining (TranslateNKIASTToBIR)](walrus/nki-inlining.md)
- [Unrolling Strategies](walrus/unrolling.md)
- [Memory Analysis and OOMChecker](walrus/memory-analysis.md)
- [Coloring Allocators (SB, PSUM, DRAM, Reg)](walrus/coloring-allocators.md)
- [DMA Pipeline](walrus/dma-pipeline.md)
- [Synchronization (Semaphores, Queues)](walrus/sync.md)
- [Schedulers and Instruction Reordering](walrus/scheduling.md)
- [LNC Splitter, Verifier, Barriercheck](walrus/lnc-and-barriercheck.md)
- [Codegen (CoreV2Gen / CoreV3Gen / CoreV4Gen)](walrus/codegen.md)
- [BIR Linker and NEFF Packager](walrus/linker-and-packager.md)

# Simulators and Verifiers

- [BIRSimulator (birsim::TpbEngines)](sim/bir-simulator.md)
- [Performance Simulator (pwp_sim)](sim/perf-sim.md)
- [BIR Racecheck](sim/bir-racecheck.md)
- [LNC Barriercheck](sim/lnc-barriercheck.md)

# Tooling Binaries

- [hlo2penguin](tools/hlo2penguin.md)
- [hlo-opt](tools/hlo-opt.md)
- [hlo-neff-wrapper](tools/hlo-neff-wrapper.md)
- [xla_infergoldens](tools/xla-infergoldens.md)
- [snapshot-unpack](tools/snapshot-unpack.md)
- [walrus_driver](tools/walrus-driver.md)
- [walrus_bugpoint_driver](tools/walrus-bugpoint-driver.md)
- [Standalone Pass Binaries](tools/standalone-pass-binaries.md)

# NKI (Python Frontend)

- [Execution Model](nki/execution-model.md)
- [nl.* Language Surface (97 ops)](nki/nl-language-surface.md)
- [nisa.* ISA Surface (41 ops)](nki/nisa-isa-surface.md)
- [Memory Spaces (sbuf / psum / hbm)](nki/memory-spaces.md)
- [Kernel Decorators (jit / baremetal / benchmark / profile / simulate)](nki/decorators.md)
- [Framework Bridges (PyTorch / JAX / baremetal)](nki/framework-bridges.md)
- [SPMD Grid](nki/spmd-grid.md)
- [KernelBuilder (Lowering to Penguin IR)](nki/kernel-builder.md)
- [Trace Context](nki/trace-context.md)
- [Sema and Types](nki/sema-and-types.md)
- [Access and Indexing](nki/access-and-indexing.md)
- [FrameworkKernel and HLO Custom-Call](nki/framework-kernel-hlo.md)
- [Penguin IR Overview](nki/penguin-ir-overview.md)
- [IRBuilder](nki/ir-builder.md)
- [Frontend Compile Pipeline](nki/frontend-pipeline.md)
- [Pass Constructor](nki/pass-constructor.md)
- [Shared Transforms](nki/shared-transforms.md)
- [Target Hierarchy (Cayman ⟂ Tonga ← Sunda ← CoreV4)](nki/target-hierarchy.md)
- [Tonga Backend (trn1 / inf2)](nki/tonga-backend.md)
- [Sunda Backend (trn2)](nki/sunda-backend.md)
- [Cayman Backend (inf1)](nki/cayman-backend.md)
- [CoreV4 Backend (trn3pre / trn3)](nki/corev4-backend.md)
- [Backend Pass Catalog](nki/backend-pass-catalog.md)
- [TongaISAInst Layer](nki/tonga-isa-inst.md)
- [BirCodeGenLoop (Tensoriser → BIR)](nki/bir-codegen-loop.md)
- [NKI Codegen (Reverse Emission)](nki/nki-codegen.md)
- [Simulation (SimulateKernel)](nki/simulation.md)
- [Public Kernels (nki.kernels)](nki/public-kernels.md)
- [Private Kernels](nki/private-kernels.md)
- [Pre-Prod Kernels](nki/pre-prod-kernels.md)
- [nkilib (core / experimental)](nki/nkilib.md)

# Formats

- [NEFF File Format](formats/neff.md)
- [BIR JSON Wire Format](formats/bir-json.md)
- [KELF Container](formats/kelf.md)

# Standalone NKI Wheel (nki-0.3.0)

- [Wheel vs neuronx-cc-Bundled Comparison](nki-standalone/wheel-vs-cc.md)
- [Python Frontend](nki-standalone/python-frontend.md)
- [MLIR Dialect Surface](nki-standalone/mlir-dialect.md)
- [Backend Selection (mlir_tracer / simulator / kernel_builder)](nki-standalone/backends.md)
- [neuron_dtypes (bf16/fp4/fp8/fp32r)](nki-standalone/dtypes.md)
