# Summary

[Tileiras Internals](index.md)

---

# Reference Apparatus

- [Program Layout](binary-layout.md)
- [Methodology](methodology.md)
- [Subsystem Map](function-map.md)
- [Version Tracking](VERSIONS.md)
- [Glossary](glossary.md)
- [Reading Map](bibliography.md)

# Driver

- [Overview](driver/overview.md)
- [Main Entry](driver/main-entry.md)
- [Program Handle](driver/program-handle.md)
- [CLI Options](driver/cli-options.md)
- [Env Vars and Runtime Gates](driver/env-vars-and-runtime-gates.md)
- [Host Launch and ptxas Knobs](driver/host-launch-and-ptxas-knobs.md)
- [Subprocess Harness](driver/subprocess-harness.md)
- [TILEIR_CALLBACKS ABI](driver/tileir-callbacks-abi.md)

# Bytecode

- [MLIR Bytecode Format](bytecode/mlir-bc-format.md)
- [Dialect Reader/Writer Status](bytecode/dialect-readers-status.md)
- [AsmPrinter Status](bytecode/asm-printer-status.md)

# Dialects

- [cuda_tile — Overview](dialects/cuda_tile/overview.md)
  - [Op Roster](dialects/cuda_tile/op-roster.md)
  - [Types and Attrs](dialects/cuda_tile/types-and-attrs.md)
  - [Verifiers](dialects/cuda_tile/verifiers.md)
  - [Canonicalizers and Folds](dialects/cuda_tile/canonicalizers-and-folds.md)
  - [AsmPrinter](dialects/cuda_tile/asm-printer.md)
  - [Bytecode](dialects/cuda_tile/bytecode.md)
- [nv_tileaa — Overview](dialects/nv_tileaa/overview.md)
  - [Op Roster](dialects/nv_tileaa/op-roster.md)
  - [Types, Attrs, Verifiers](dialects/nv_tileaa/types-attrs-verifiers.md)
  - [Folds, Canonicalizers, Tokens](dialects/nv_tileaa/folds-canonicalizers-tokens.md)
- [nv_tileas — Overview](dialects/nv_tileas/overview.md)
  - [Op Roster and Builders](dialects/nv_tileas/op-roster-and-builders.md)
  - [Types](dialects/nv_tileas/types.md)
  - [Verifiers](dialects/nv_tileas/verifiers.md)
  - [Folds and Mem Consistency](dialects/nv_tileas/folds-and-mem-consistency.md)
- [cute — Overview](dialects/cute/overview.md)
  - [Layout Algebra and Descriptor Grammar](dialects/cute/layout-algebra-and-descriptor-grammar.md)
  - [Tile and Divide Ops](dialects/cute/tile-and-divide-ops.md)
  - [Atom Builders and Desugar](dialects/cute/atom-builders-and-desugar.md)
  - [Verifiers](dialects/cute/verifiers.md)
- [cute_nvgpu — Overview](dialects/cute_nvgpu/overview.md)
  - [SM-Tier Roster and Copy Atom Registry](dialects/cute_nvgpu/sm-tier-roster-and-copy-atom-registry.md)
  - [MMA Atoms SM70-120](dialects/cute_nvgpu/mma-atoms-sm70-120.md)
  - [TMA Atoms](dialects/cute_nvgpu/tma-atoms.md)
  - [Mode-Pattern Verifiers](dialects/cute_nvgpu/mode-pattern-verifiers.md)
  - [AsmPrinter and Mnemonic Hash](dialects/cute_nvgpu/asm-printer-and-mnemonic-hash.md)
- [cutlass — Overview](dialects/cutlass/overview.md)
  - [Pipeline and Tile Scheduler](dialects/cutlass/pipeline-and-tile-scheduler.md)
  - [Seq-Bar and Block-Striped](dialects/cutlass/seq-bar-and-block-striped.md)
  - [MODS Async Dispatch](dialects/cutlass/mods-async-dispatch.md)
- [nvgpu — Overview](dialects/nvgpu/overview.md)
- [NVVM — Overview](dialects/nvvm/overview.md)
  - [Properties Blob and Attr Parsers](dialects/nvvm/properties-blob-and-attr-parsers.md)
  - [WMMA Ops](dialects/nvvm/wmma-ops.md)
  - [WGMMA Ops](dialects/nvvm/wgmma-ops.md)
  - [TMA Ops](dialects/nvvm/tma-ops.md)
  - [tcgen05 Ops](dialects/nvvm/tcgen05-ops.md)
  - [mbarrier Ops](dialects/nvvm/mbarrier-ops.md)
  - [Cluster Ops](dialects/nvvm/cluster-ops.md)

# Pipeline

- [Overview](pipeline/overview.md)
- [Driver and Opt Levels](pipeline/driver-and-opt-levels.md)
- [Pass Manager Internals](pipeline/pass-manager-internals.md)
- [Invariants and Verifiers](pipeline/invariants-and-verifiers.md)
- [Options Mapping](pipeline/options-mapping.md)
- [Instrumentation and Action Handler](pipeline/instrumentation-and-action-handler.md)
- [Full Pass List by Opt Level](pipeline/full-pass-list-by-opt-level.md)
- [PassBuilder Mega-Registry](pipeline/passbuilder-mega-registry.md)

# TileAS Passes

- [Async/Pipeline Family](passes/tileas/async-pipeline-family.md)
- [Layout and Buffer Family](passes/tileas/layout-and-buffer-family.md)
- [TMA and Memops Family](passes/tileas/tma-and-memops-family.md)
- [CTA Cluster Family](passes/tileas/cta-cluster-family.md)
- [Scheduling Glue](passes/tileas/scheduling-glue.md)

# Scheduler

- [Overview](scheduler/overview.md)
- [Serial vs Cost-Based Generators](scheduler/serial-vs-cost-based-generators.md)
- [Resource Constraint Builder and RRT](scheduler/resource-constraint-builder-and-rrt.md)
- [Modulo Scheduler and Rau](scheduler/modulo-scheduler-and-rau.md)
- [Modulo Driver and 4-Arm OR-Chain](scheduler/modulo-driver-or-chain.md)
- [Schedule Constraint Attributes](scheduler/schedule-constraint-attributes.md)
- [Schedule::solve and Cost Evaluators](scheduler/schedule-solve-and-cost-evaluators.md)
- [Blackwell Pipeline 15-Slot Model](scheduler/blackwell-pipeline-15-slot-model.md)
- [Pipe_ and Mutex_ Value-Header Layout](scheduler/pipe-mutex-value-layout.md)
- [Buffer Assignment and Named-Barrier Binding](scheduler/buffer-assignment-and-mbarriers.md)

# Lowering

- [Overview](lowering/overview.md)
- [cuda_tile to tileaa](lowering/cuda-tile-to-tileaa.md)
- [tileaa to tileas](lowering/tileaa-to-tileas.md)
- [tileas to LLVM](lowering/tileas-to-llvm.md)
- [cute and cute_nvgpu to LLVM](lowering/cute-and-cute_nvgpu-to-llvm.md)
- [nvgpu and gpu to NVVM](lowering/nvgpu-and-gpu-to-nvvm.md)
- [Target and Debuginfo](lowering/target-and-debuginfo.md)
- [Pattern Set and Type Converter](lowering/pattern-set-and-typeconverter.md)

# Codegen (NVPTX Backend)

- [Overview](codegen/overview.md)
- [NVPTX Bring-up and Target Init](codegen/nvptx-bring-up-and-target-init.md)
- [NVPTX Subtarget and Feature Matrix](codegen/nvptx-subtarget-and-feature-matrix.md)
- [NVPTX Target Lowering, Call and Args](codegen/nvptx-target-lowering-call-and-args.md)
- [ISelDAG and MatcherTable](codegen/iseldag-and-matchertable.md)
- [AsmPrinter Monster and Windows](codegen/asm-printer-monster-and-windows.md)
- [Per-SM Emission Templates](codegen/per-sm-emission-templates.md)
- [Atomic, Warp, Sreg, Fence](codegen/atomic-warp-sreg-fence.md)
- [TMA, Tensormap and cp.async.bulk](codegen/tma-tensormap-and-cp-async-bulk.md)
- [tcgen05, WGMMA, mbarrier, Cluster](codegen/tcgen05-wgmma-mbarrier-cluster.md)
- [ldmatrix, stmatrix and Register Class Vtables](codegen/ldmatrix-stmatrix-and-register-class-vtables.md)

# NVPTX Custom Passes

- [Overview](nvptx-passes/overview.md)
- [Peephole MIR and Image Handles](nvptx-passes/peephole-mir-and-image-handles.md)
- [Lower-Args, Aggr, Struct](nvptx-passes/lower-args-and-aggr-and-struct.md)
- [MemorySpaceOpt and process-restrict](nvptx-passes/memory-space-opt-and-process-restrict.md)
- [Printf Lowering and vprintf](nvptx-passes/printf-lowering-and-vprintf.md)
- [DeadSyncElim and CommonBaseElim](nvptx-passes/dead-sync-elim-and-common-base.md)
- [NVVMIRVerifier](nvptx-passes/nvvm-ir-verifier.md)
- [Kernel, CDP, Inline, Pretreat](nvptx-passes/kernel-cdp-inline-pretreat.md)

# Libdevice

- [Overview](libdevice/overview.md)
- [NVVMReflect Mechanism](libdevice/nvvm-reflect-mechanism.md)
- [Intrinsic ID Switch and Name Table](libdevice/intrinsic-id-switch-and-name-table.md)
- [Math Pass Pipeline and Crosswalk](libdevice/math-pass-pipeline-and-crosswalk.md)

# MLIR Infra

- [Overview](mlir-infra/overview.md)
- [Operation Layout](mlir-infra/operation-layout.md)
- [StorageUniquer and Context Impl](mlir-infra/storage-uniquer-and-context-impl.md)
- [Pattern Vtables and Shapes](mlir-infra/pattern-vtables-and-shapes.md)
- [Interface Vtables](mlir-infra/interface-vtables.md)
- [TypeID Construction Idioms](mlir-infra/typeid-idioms.md)
- [TypeID Sentinels and Anchors](mlir-infra/typeid-sentinels-and-anchors.md)
- [Container Fingerprints](mlir-infra/container-fingerprints.md)
- [Diagnostic ABI and Helpers](mlir-infra/diagnostic-abi-and-helpers.md)
- [AsyncValue and BLAKE3 Interning](mlir-infra/asyncvalue-and-blake3-interning.md)
- [Pass-Failure Handshake](mlir-infra/pass-failure-handshake.md)

# Cross-cutting Infra

- [Data Section Decryption](infra/data-section-decryption.md)
- [Binary Vtable Banks and Static Ctors](infra/binary-vtable-banks-and-static-ctors.md)
- [Threading and Synchronization](infra/threading-and-synchronization.md)
- [Allocator BumpPtr and Slab Sizes](infra/allocator-bumpptr-and-slab-sizes.md)
- [Twine, StringRef, format](infra/twine-stringref-and-format.md)
- [Diagnostic Helpers](infra/diagnostic-helpers.md)
- [GlobalValue Flag Bits](infra/global-value-flag-bits.md)

# Specialized Topics

- [GPU Execution Model](topics/gpu-execution-model.md)
  - [Memory Hierarchy and Data Flow](topics/memory-hierarchy-and-dataflow.md)
  - [AddrSpace Vote Lattice](topics/addrspace-vote-lattice.md)
  - [Concurrency and Sync Semantics](topics/concurrency-and-sync-semantics.md)
  - [Cluster Sync and DSMEM Handshake](topics/cluster-sync-and-dsmem-handshake.md)
  - [mbarrier State Machine](topics/mbarrier-state-machine.md)
- [Tensor Core Emission](topics/wgmma-emission-protocol.md)
  - [Blackwell 2-CTA/4-CTA MMA](topics/blackwell-2cta-and-4cta-mma.md)
  - [tcgen05 Tensor Memory Model](topics/tcgen05-tensor-memory-model.md)
  - [tcgen05.mma Walkthrough](topics/tcgen05-mma-walkthrough.md)
  - [Matmul Progression by SM](topics/matmul-progression-by-sm.md)
  - [TMA Load Walkthrough](topics/tma-load-walkthrough.md)
- [DSL to PTX End-to-End](topics/dsl-to-ptx-end-to-end.md)
  - [Frontend Contract and Tile IR Emission](topics/frontend-contract-and-tile-ir-emission.md)
  - [Attribute System and Lowering](topics/attribute-system-and-lowering.md)
  - [PTX Version and Target Selection](topics/ptx-version-and-target-selection.md)
  - [cuda_tile Simplifier Walker](topics/cuda-tile-simplifier-walker.md)
  - [Force-Inline and Specialize Callees](topics/force-inline-and-specialize-callees.md)
  - [LoopIdiom, Vectorize, Divergent](topics/loop-idiom-vectorize-divergent.md)
  - [LowerMatrix and mfadd](topics/lower-matrix-mfadd.md)
- [Performance and Cost Model](topics/performance-and-cost-model.md)
  - [Fast-Math and Numerical Precision](topics/fast-math-and-numerical-precision.md)
- [Correctness Layers](topics/correctness-layers.md)
  - [Error Handling and Diagnostics](topics/error-handling-and-diagnostics.md)
  - [Debugging and Introspection](topics/debugging-and-introspection.md)
  - [Troubleshooting and Known Issues](topics/troubleshooting-and-known-issues.md)
  - [Testing and Observability](topics/testing-and-observability.md)
- [Architecture Evolution and Design Decisions](topics/architecture-evolution-and-design-decisions.md)
  - [Common Compiler Patterns and Idioms](topics/common-compiler-patterns-and-idioms.md)
  - [Binary Anatomy and RE Methodology](topics/binary-anatomy-and-re-methodology.md)
- [Frequently Asked Questions](topics/frequently-asked-questions.md)

# OSS Cross-Reference

- [OSS Comparison Overview](oss/overview.md)
- [cuda_tile Tree Mapping](oss/cuda-tile-tree-mapping.md)
- [.td Files Delta](oss/td-files-delta.md)
- [Transforms, FuseFMA, SynthDbg](oss/transforms-fusefma-synthdbg.md)

# Boundaries

- [cicc Comparison](boundaries/cicc-comparison.md)
- [ptxas Handoff Protocol](boundaries/ptxas-handoff-protocol.md)
- [cudafe++ Non-Relationship](boundaries/cudafe-non-relationship.md)
- [Position in nvcc 13.1](boundaries/nvcc-13-1-position.md)
- [Toolchain Integration](boundaries/toolchain-integration.md)

# Reference Catalogs

- [cl::opt Full Catalog](reference/cl-opt-full-catalog.md)
- [Env Var and Runtime Gate Catalog](reference/env-var-and-runtime-gate-catalog.md)
- [TypeID Sentinel Table](reference/typeid-sentinel-table.md)
- [Op Mnemonic Master Table](reference/op-mnemonic-master-table.md)
- [LLVM Fingerprint Table](reference/llvm-fingerprint-table.md)
- [Wire-Format Constants](reference/wire-format-constants.md)
- [String Evidence and Confidence Policy](reference/string-evidence-and-confidence-policy.md)
