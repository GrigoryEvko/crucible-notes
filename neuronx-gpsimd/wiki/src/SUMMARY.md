# Summary

[neuronx-gpsimd Internals — The Vision-Q7 GPSIMD Reimplementation Reference](index.md)

---

# Part 0 — Reference Apparatus

- [How to Read This Guide](reference/how-to-read.md)
- [The Confidence & Walls Model](reference/confidence-model.md)
- [Methodology — How This Was Reverse-Engineered](reference/methodology.md)
- [FLIX Bundle-Decoding Methodology](reference/flix-decoding.md)
- [The Corpus, Tiers & Binary Inventory](reference/corpus-inventory.md)
- [Toolchain Inventory & Versions](reference/toolchain-versions.md)
- [Codename ↔ Generation Cross-Walk](reference/codename-crosswalk.md)
- [The Do-Not-Repeat / Correction Ledger](reference/correction-ledger.md)
- [Master Glossary](glossary.md)

# Part 1 — Orientation

- [What GPSIMD Is — the one-screen map](orientation/what-gpsimd-is.md)
- [The Seven Faces of the One Machine](orientation/seven-faces.md)
- [Keystone Facts Reimplementers Get Wrong](orientation/keystone-facts.md)
- [A Custom Op, End to End](orientation/customop-end-to-end.md)
- [A Collective, End to End](orientation/collective-end-to-end.md)
- [The Gen-Invariance Thesis](orientation/gen-invariance.md)
- [The Reimplementation Verdict & Open-Questions Map](orientation/verdict-and-open-questions.md)

# Part 2 — Q7 Core & ISA Foundations

- [Core Identity & Configuration](isa/core/identity-config.md)
- [Config-Grounded Microarch Reference Sheet](isa/core/config-reference-sheet.md)
- [The Canonical ISA Decode Model (libisa-core)](isa/core/libisa-decode-model.md)
- [The FLIX VLIW Encoding (14 format / 46 slot)](isa/core/flix-encoding.md)
- [The Eight Register Files](isa/core/register-files.md)
- [The Floating-Point Sub-ISA (FCR/FSR view)](isa/core/fp-sub-isa.md)
- [The TIE Database & Four Independent ISA Sources](isa/core/tie-database.md)
- [The libisa Table Schema & Codec ABI](isa/core/libisa-table-schema.md)
- [ctype / coproc / funcUnit / bypass Tables](isa/core/ctype-coproc-funcunit.md)
- [ISA Coverage & the 1534/1607/12642 Tally](isa/core/coverage-tally.md)

# Part 3 — Per-Instruction ISA Reference

- [ISA Reference — Template & 30-Batch Partition](isa/ref/template-and-partition.md)
- [ISA Batch 01 — Vector ALU (int/compare/logic core)](isa/ref/b01-vec-alu-int.md)
- [ISA Batch 02 — Vector ALU (fp16/fp32 slice)](isa/ref/b02-vec-alu-fp.md)
- [ISA Batch 03 — Vector ALU (int/B-variant/flag/predicated)](isa/ref/b03-vec-alu-rest.md)
- [ISA Batch 04 — Integer MAC Matrix (signed)](isa/ref/b04-mac-integer.md)
- [ISA Batch 05 — MAC (mixed-sign/complex/wide-acc)](isa/ref/b05-mac-mixed.md)
- [ISA Batch 06 — Vector Loads + valign priming](isa/ref/b06-loads.md)
- [ISA Batch 07 — Vector Stores](isa/ref/b07-stores.md)
- [ISA Batch 08 — Cross-Lane Reduce](isa/ref/b08-reduce.md)
- [ISA Batch 09 — Vector Move / regfile bridge](isa/ref/b09-vec-mov.md)
- [ISA Batch 10 — wvec Pack (wide→narrow readout)](isa/ref/b10-wvec-pack.md)
- [ISA Batch 11 — vbool ALU / predicate](isa/ref/b11-vbool-alu.md)
- [ISA Batch 12 — Vector Shift / Rotate / Normalize](isa/ref/b12-shift.md)
- [ISA Batch 13 — fp32 Convert (sp_cvt)](isa/ref/b13-sp-cvt.md)
- [ISA Batch 14 — fp16 Transcendental Seeds (hp_lookup)](isa/ref/b14-hp-lookup.md)
- [ISA Batch 15 — fp32 Transcendental Seeds (sp_lookup)](isa/ref/b15-sp-lookup.md)
- [ISA Batch 16 — Vector Replicate / Extract (vec_rep)](isa/ref/b16-vec-rep.md)
- [ISA Batch 17 — fp32 Fused Multiply-Add (spfma)](isa/ref/b17-spfma.md)
- [ISA Batch 18 — fp16 Fused Multiply-Add (hp_fma)](isa/ref/b18-hp-fma.md)
- [ISA Batch 19 — SuperGather Scatter/Gather](isa/ref/b19-scatter-gather.md)
- [ISA Batch 20 — fp16 Convert (hp_cvt)](isa/ref/b20-hp-cvt.md)
- [ISA Batch 21 — Select / Shuffle / Compress](isa/ref/b21-select-shuffle.md)
- [ISA Batch 22 — Unpack / wvec Move](isa/ref/b22-unpack-wvec-mov.md)
- [ISA Batch 23 — Vector Integer Divide](isa/ref/b23-divide.md)
- [ISA Batch 24 — Histogram / Squeeze / QLI / FCR-FSR](isa/ref/b24-composite.md)
- [ISA Batch 25 — base-Xtensa scalar arith/logic/shift](isa/ref/b25-xt-core.md)
- [ISA Batch 26 — base-Xtensa ld/st/branch/density/MUL32/div](isa/ref/b26-xt-ctrl.md)
- [ISA Batch 27 — base-Xtensa System / SR / RegWindow / Sync](isa/ref/b27-xt-system.md)
- [ISA Batch 28 — base-Xtensa exc-dispatch/bool/loop/minmax](isa/ref/b28-xt-exc.md)
- [ISA Batch 29 — base-Xtensa debug/timer/cache/MMU/atomic](isa/ref/b29-xt-system2.md)
- [ISA Batch 30 — Appendix P (pseudo/fence) + Final Coverage](isa/ref/b30-appendix-p.md)
- [Formal Semantics I — arith / MAC / load-store / gather](isa/semantics/group-semantics-i.md)
- [Formal Semantics II — predicate / convert-fp / valign-reduce / control](isa/semantics/group-semantics-ii.md)
- [Formal Semantics — Coverage Ledger](isa/semantics/coverage-ledger.md)
- [The Complete Formal ISA-Semantics Model](isa/semantics/formal-isa-model.md)

# Part 4 — Microarchitecture & Timing (14)
- [Pipeline Timing Model](uarch/pipeline-timing.md)
- [FLIX Co-Issue Matrix + Cache/Local-Memory Timing](uarch/co-issue-matrix.md)
- [Register-File Port Model + Bypass Network](uarch/regfile-ports.md)
- [Boot / Reset Sequence + Startup Config](uarch/boot-reset.md)
- [Local-Memory / System-Bus / LSU Model](uarch/lsu-memory.md)
- [Atomic + Memory-Ordering Model](uarch/atomics-ordering.md)
- [The SIMD Compute-Datapath](uarch/simd-datapath.md)
- [The VFPU / IEEE-754 Exception Model](uarch/vfpu-ieee.md)
- [Activation + Transcendental Table Engine](uarch/activation-transcendental-tables.md)
- [The NCFW Scalar-LX Management Core](uarch/ncfw-lx-core.md)
- [Clock / Reset / Power Domains](uarch/clock-reset-power.md)
- [NX1.1.4 Revision Markers](uarch/rev-markers.md)
- [Microarchitecture Synthesis](uarch/microarch-synthesis.md)
- [Per-Engine Firmware Depth (PE/SP/TOP_SP/ACT)](uarch/per-engine-depth.md)

# Part 5 — Device Firmware & Kernel Catalog (85)
- [SEQ Boot / Entry Path](firmware/seq/boot.md)
- [SEQ Main FSM Loop](firmware/seq/main-loop.md)
- [SEQ Fetch + PC-Redirect Front-End](firmware/seq/fetch-pc-redirect.md)
- [SEQ IRAM Instruction Cache / Overlay](firmware/seq/iram-cache.md)
- [SEQ Branch + Prefetch-Hint](firmware/seq/branch-prefetch.md)
- [SEQ PC-Bounds Enforcement + Host API](firmware/seq/pc-bounds.md)
- [SEQ Decode / Dispatch Hub](firmware/seq/dispatch-hub.md)
- [SEQ Run-State Machine](firmware/seq/run-state.md)
- [SEQ Error-Handler / Fault Reporting](firmware/seq/error-handler.md)
- [SEQ Surprises / IRQ Poll](firmware/seq/surprises-irq.md)
- [SEQ Uarch Register Model + Single-Step Debugger](firmware/seq/uarch-debugger.md)
- [SEQ SoC Window Manager](firmware/seq/soc-window-manager.md)
- [HW-Decode vs Sunda Dual Fetch](firmware/seq/dual-fetch.md)
- [POOL Engine Main Dispatch Loop](firmware/pool/pool-dispatch.md)
- [POOL Extended-Opcode (0xF0) Dispatch](firmware/pool/pool-ext-0xf0.md)
- [External-Lib Loader (device side)](firmware/pool/external-lib-loader.md)
- [External-Lib Prelink Validation + NUM_POOL_CORES](firmware/pool/prelink-validation.md)
- [kernel_info_table Binary Layout](firmware/pool/kernel-info-table.md)
- [iDMA / Legacy DMA (IRAM cache-fill)](firmware/dge/idma-legacy-dma.md)
- [DGE Setup + Context Init](firmware/dge/dge-setup.md)
- [DGE 3-Backend Selector](firmware/dge/dge-backend-selector.md)
- [DGE Reshape Engine](firmware/dge/dge-reshape.md)
- [DGE Descriptor-Emit Path](firmware/dge/dge-emit.md)
- [DGE Error Notifications](firmware/dge/dge-errors.md)
- [The Opcode Catalog Ledger (140 real opcodes)](firmware/kernels/opcode-catalog-ledger.md)
- [On-Device Virtual File-I/O Manager](firmware/kernels/file-io-manager.md)
- [The Unified Datatype Model](firmware/kernels/dtype-model.md)
- [RNG — Xorwow Software Path](firmware/kernels/rng-xorwow-sw.md)
- [RNG — Xorwow TIE Hardware Path](firmware/kernels/rng-xorwow-tie.md)
- [RNG — LFSR + rand_algo Dispatch Tree](firmware/kernels/rng-lfsr-dispatch.md)
- [Rand2 (user random-tensor op)](firmware/kernels/rand2.md)
- [SB2SB Remote-Copy Collective Kernel](firmware/kernels/sb2sb-remote-copy.md)
- [RDMA Descriptor Gen/Start (TX-RX protocol)](firmware/kernels/rdma-desc-gen-start.md)
- [The CPTC Compressed-Tensor Codec Family](firmware/kernels/cptc-codec.md)
- [BatchNormalize — Forward Statistics](firmware/kernels/batchnorm-forward.md)
- [BatchNormalize — Back-Prop](firmware/kernels/batchnorm-backprop.md)
- [BatchNormalize — GradAccum](firmware/kernels/batchnorm-gradaccum.md)
- [BatchNormalize — ParamLoad](firmware/kernels/batchnorm-paramload.md)
- [Dropout](firmware/kernels/dropout.md)
- [Exponential (the EXP transform)](firmware/kernels/exponential.md)
- [ConvLutLoad (legacy LUT-load path of 0xe4)](firmware/kernels/convlutload.md)
- [DVE Search/Select Cluster (FindIndex8/Max8/MatchReplace8)](firmware/kernels/search-cluster.md)
- [CrossLaneReduce](firmware/kernels/cross-lane-reduce.md)
- [RangeSelect](firmware/kernels/rangeselect.md)
- [Sort / DECODE_SORT](firmware/kernels/sort.md)
- [Tensor-Tensor Elementwise Arith + the ALU-OP Table](firmware/kernels/tensor-tensor.md)
- [Tensor-Scalar + Tensor-Scalar-PTR](firmware/kernels/tensor-scalar.md)
- [Tensor-Reduce (cross-partition)](firmware/kernels/tensor-reduce.md)
- [TensorTensorScan](firmware/kernels/tensor-tensor-scan.md)
- [Scalar-Tensor-Tensor](firmware/kernels/scalar-tensor-tensor.md)
- [TensorScalarCacheCumulative](firmware/kernels/ts-cache-cumulative.md)
- [TensorScalarCacheReduce](firmware/kernels/ts-cache-reduce.md)
- [TensorScalarSelect](firmware/kernels/ts-select.md)
- [TensorScalarImmLd (Arith/Bitvec)](firmware/kernels/ts-immld.md)
- [TensorScalarPtrMulti (Arith/Bitvec)](firmware/kernels/ts-ptrmulti.md)
- [StreamTranspose (DVE datapath transpose)](firmware/kernels/stream-transpose.md)
- [SparsityCompress / SparsityCompressTag](firmware/kernels/sparsity-compress-tag.md)
- [CastPredicated](firmware/kernels/castpredicated.md)
- [CopyPredicatedReduce](firmware/kernels/copypredicatedreduce.md)
- [TensorDequantize](firmware/kernels/tensor-dequantize.md)
- [MX (Microscaling) Dequant Compute Paths](firmware/kernels/mx-dequant.md)
- [TensorLoad](firmware/kernels/tensorload.md)
- [TensorStore](firmware/kernels/tensorstore.md)
- [PE Matrix-Multiply Path (LdWeight/Matmul/ManageSeed)](firmware/kernels/pe-matmul.md)
- [Extended Tensor-Tensor Arith (0xF0 variant)](firmware/kernels/ext-tensor-tensor-arith.md)
- [Tensor-Tensor 64-bit Path](firmware/kernels/tensor-tensor-64bit.md)
- [decode_pool (the "Pool" kernel disambiguation)](firmware/kernels/decode-pool.md)
- [avg_pool / max_pool](firmware/kernels/avg-max-pool.md)
- [Iota / sequence-index generator](firmware/kernels/iota.md)
- [Cast and Copy](firmware/kernels/cast-copy.md)
- [NonzeroWithCount](firmware/kernels/nonzero-with-count.md)
- [The Indirection Engine (gather/scatter/embedding)](firmware/kernels/indirection-gather.md)
- [Activate + the PWL Application Mechanism](firmware/kernels/activate-pwl.md)
- [GetSequenceBounds](firmware/kernels/get-sequence-bounds.md)
- [Move (general-purpose register move + dtype gate)](firmware/kernels/move-dtype.md)
- [The ALU-Op Datapath + Dtype Matrix](firmware/kernels/alu-op-matrix.md)
- [AffineSelect (TensorScalarAffineSelect)](firmware/kernels/affineselect.md)
- [CopyPredicatedScalar](firmware/kernels/copypredicatedscalar.md)
- [TensorCumulative](firmware/kernels/tensorcumulative.md)
- [DVE State Read-Back (DveReadAccumulator/Indices)](firmware/kernels/dve-read-state.md)
- [SUNDA-only Dual / Deprecated TensorScalarPtr](firmware/kernels/sunda-dual-tensorscalarptr.md)
- [POOL/ACT Compute Gap-Cluster](firmware/kernels/pool-compute-misc.md)
- [RNG Seed-State Opcodes (0x77/0x78)](firmware/kernels/rng-seed-state-ops.md)
- [DMA / Transpose Opcode Cluster](firmware/kernels/dma-transpose-opcode-cluster.md)
- [Gen-Bracket Extremes — MAVERICK INT_WIDE + SUNDA BF16](firmware/kernels/intwide-bf16-extremes.md)

<!-- ===========================================================================
ROADMAP — Parts 1–16 (401 leaf pages). Journey order: a reimplementer reads top
to bottom to rebuild a Vision-Q7-compatible GPSIMD engine. Each Part below is a
committed roadmap; as a page ships, its author MOVES that line out of the comment
into a live `# Part N — Title` section + `- [Title](path.md)` link, in order.
Per-page tasks: harness #588–#998. Do NOT bulk-uncomment — wire one page at a time.

# Part 6 — Firmware Images & Generations (35)
## Per-(gen × engine) images (28)
- images/extisa-inventory.md            — EXTISA Q7 SO-Blob Inventory + Blob→Getter Map
- images/image-catalog-index.md         — Firmware-Image Accessor Index
- images/cayman-act.md                  — CAYMAN × ACT image
- images/cayman-dve.md                  — CAYMAN × DVE image
- images/cayman-pe.md                   — CAYMAN × PE image
- images/cayman-pool.md                 — CAYMAN × POOL image (dual-dispatch)
- images/cayman-sp.md                   — CAYMAN × SP image (+ SP-vs-TOP_SP)
- images/mariana-act.md                 — MARIANA × ACT image (cross-gen diff vs Cayman)
- images/mariana-dve.md                 — MARIANA × DVE image
- images/mariana-pe.md                  — MARIANA × PE image
- images/mariana-pool.md                — MARIANA × POOL image (dual-core)
- images/mariana-sp.md                  — MARIANA × SP image
- images/mariana-plus-act.md            — MARIANA_PLUS × ACT image
- images/mariana-plus-dve.md            — MARIANA_PLUS × DVE image
- images/mariana-plus-pe.md             — MARIANA_PLUS × PE image
- images/mariana-plus-pool.md           — MARIANA_PLUS × POOL image (dual-core)
- images/mariana-plus-sp.md             — MARIANA_PLUS × SP image
- images/maverick-act.md                — MAVERICK × ACT image (the ACT→DVE fold)
- images/maverick-dve.md                — MAVERICK × DVE image (absorbs ACT)
- images/maverick-pe.md                 — MAVERICK × PE image
- images/maverick-pool.md               — MAVERICK × POOL image (dual-core)
- images/maverick-sp.md                 — MAVERICK × SP image
- images/sunda-pool.md                  — SUNDA × POOL image (the v2 baseline)
- images/sunda-sp-remaining.md          — SUNDA × SP + remaining NX engines
- images/sunda-arch5-extisa.md          — SUNDA arch5 EXTISA ELF
- images/prof-cam-table-formats.md      — PROF_CAM / PROF_TABLE Blob Formats
- images/cross-gen-kernel-info-matrix.md — Cross-Gen kernel_info_table / Opcode Matrix
- images/firmware-image-catalog.md      — Firmware-Image Catalog Capstone
## Generations (7)
- generations/codename-generation-map.md  — Codename ↔ Generation Map
- generations/sunda-v2-baseline.md         — SUNDA v2 Baseline Topology
- generations/mariana-plus-delta.md        — MARIANA_PLUS (v4+) Generation Delta
- generations/maverick-profile.md          — MAVERICK (v5) Profile + Maximal-Observed Carve
- generations/arch-isa-header-diff.md       — Cross-Generation Arch-ISA Header Diff
- generations/cross-gen-opcode-diff.md      — Cross-Generation Opcode-Table Diff + TONGA
- generations/master-capability-matrix.md   — Master Per-Generation Capability Matrix

# Part 7 — Custom-Op ABI (21)
- abi/q7ptrtype.md                — Q7PtrType + Lazy Translation
- abi/tensor-object-chain.md      — The at::Tensor Object Chain
- abi/tensor-accessor.md          — The Retargeted TensorAccessor
- abi/coherency-enforcer.md       — The CoherencyEnforcer
- abi/tensorstream-tcm.md         — TensorStream + TCM Staging
- abi/scalartype-dtype-rosetta.md — ScalarType ↔ DTYPE Rosetta
- abi/customop-marshalling.md     — customop_* Marshalling Entries
- abi/build-custom-op-codegen.md  — build_custom_op.py Codegen
- abi/stack-switch.md             — Stack-Switch Dispatch
- abi/neuron-translate-windows.md — neuron_translate Window Family
- abi/device-allocators.md        — Device Memory Allocators
- abi/data-transfer-backends.md   — Data-Transfer Backends
- abi/multicore-spmd.md           — The Multicore API (8-core SPMD)
- abi/build-flow.md               — Build → Compile → Link → Strip → Package Flow
- abi/lsp-elf.md                  — LSP Linker Specs + ELF Layout
- abi/flexlm-licensing.md         — FlexLM Licensing Gate
- abi/device-abi-reference.md     — The Device-Side Custom-Op ABI Reference
- abi/complete-customop-abi.md    — The Complete Custom-Op ABI (libneuroncustomop.a)
- abi/q7-elf-vaddr.md             — The Q7 ELF VADDR + Per-Core Memory Model
- abi/abi-synthesis.md            — End-to-End ABI Synthesis
- abi/programming-model.md        — The Custom-Op Programming Model (overview)

# Part 8 — Host Runtime (28)
- runtime/libnrt-surface.md                   — The libnrt Surface Map (GPSIMD lens)
- runtime/nrtucode-bringup.md                 — The nrtucode Subsystem + Device Bring-Up
- runtime/aws-hal-q7.md                        — The aws_hal_q7_* HAL
- runtime/hw-decode-cam-programming.md         — HW-Decode CAM-Table Programming
- runtime/execute-time-dispatch.md             — Execute-Time GPSIMD Custom-Op Dispatch
- runtime/spmd-teardown.md                     — The 8-Core SPMD Execution Model + Teardown
- runtime/lifecycle-error-model.md             — Host Model Lifecycle + Error-Handling Model
- runtime/multimodel-context-dmem.md           — Multi-Model / Context Tree + dmem Allocator
- runtime/runtime-synthesis.md                 — The libnrt Runtime Synthesis
- runtime/public-api-table.md                  — The nrt Host API Surface Reference
- runtime/public-vs-internal-partition.md      — Public-vs-Internal API Partition + Versioned Symbols
- runtime/ucode-relocation-consumer.md         — The UCODE Relocation / Prelink Engine (runtime consumer)
- runtime/concurrency-primitives.md            — Host-Runtime Concurrency Primitives
- runtime/host-device-descriptor-handoff.md    — Host↔Device Descriptor Handoff (runtime side)
- runtime/reachability-cuts.md                 — Crypto / SQLite / Codec Reachability Cuts
- runtime/object-model-graph.md                — The nrtucode Object Model Graph
- runtime/callgraph-spine.md                   — Runtime End-to-End Call-Graph Spine
- runtime/nrtucode-context.md                  — nrtucode_context_t + Lifecycle
- runtime/nrtucode-core.md                     — nrtucode_core_t Struct + Introspection/Boot
- runtime/nrtucode-logging-allocator.md        — nrtucode Logging + Leak-Tracking Allocator
- runtime/dge-host-api.md                      — The DGE Host-Private API (priority/mailbox/PC-bounds)
- runtime/nrtucode-ll-create.md                — nrtucode_ll_create / destroy / name / size
- runtime/opcode-to-lib-resolver.md            — Opcode-Set → Library Resolver
- runtime/nrtucode-ll-load-unload.md           — ll Load / Unload Sequence Generators
- runtime/nrtucode-opset.md                    — Opset Create / Add-Instruction / Query
- runtime/version-extisa-getters.md            — Version + Ext-ISA Getters
- runtime/image-hwdecode-resolvers.md          — Image + HW-Decode Resolvers
- runtime/prelinker-ucpl.md                    — The Host Prelinker — UCPL / Segment Loader / R_XTENSA / Staging

# Part 9 — DMA / Descriptors / Memory (12)
- dma/descriptor-model.md               — The DMA / Descriptor / Memory Subsystem
- dma/gather-scatter-descriptors.md     — Gather/Scatter + Gather-Transpose Descriptors
- dma/rdma-cross-die.md                 — RDMA Cross-Die SBUF→SBUF P2P
- dma/cce-in-transfer.md                — CCE (Compute-DMA) In-Transfer Compute
- dma/dge-builder-qos.md                — DGE Descriptor-Builder + SDMA QoS/Arbitration
- dma/sbuf-psum-banks.md                — On-Chip State-Buffer (SBUF) + PSUM Bank Model
- dma/udma-hw-engine.md                 — The al_udma Hardware DMA Engine
- dma/dge-microop-encoding.md           — DGE Micro-Op Encoding (byte-level)
- dma/data-movement-reference.md        — Consolidated Data-Movement + Collectives Reference
- dma/descriptor-ring-field-tables.md   — Descriptor + Ring Field-Table Reference
- dma/sdma-windows-apb.md               — SDMA Address Windows + APB Chain
- dma/onchip-working-memory.md          — On-Chip Working-Memory Regions (SBUF/PSUM/scratch)

# Part 10 — Collectives & NCFW (28)
## Collective ops (14)
- collectives/ops/trigger-collective.md        — TriggerCollective (0xC8)
- collectives/ops/trigger-collective2-ext.md   — TriggerCollective2 + Ext (0xD9/0xDA)
- collectives/ops/all-reduce.md                — ALL_REDUCE
- collectives/ops/s3d3-collective.md           — S3D3 Collective (SB2SB, 0xBF)
- collectives/ops/rank-id.md                   — PseudoCurProcessingRankID (0xDB)
- collectives/ops/core-barrier.md              — PSEUDO_CORE_BARRIER (0xD8)
- collectives/ops/sync-barrier.md              — PSEUDO_SYNC_BARRIER (0xD5)
- collectives/ops/dma-barrier.md               — PSEUDO_DMABARRIER (0xC3)
- collectives/ops/sendrecv.md                  — SENDRECV (point-to-point, 0xCB)
- collectives/ops/rdma-gather-pseudo-ops.md    — RDMA/DMA Collective Pseudo-Ops (DIRECT2D/GATHER_XPOSE/INDIRECT1D)
- collectives/ops/collective-enums.md          — Collective-Type + cc_op Enum Reference
- collectives/ops/top-sp-lowering.md           — TOP_SP Collective Lowering
- collectives/ops/xrp-host-dsp-messaging.md    — XRP Host↔DSP Messaging Transport
- collectives/ops/architecture-synthesis.md    — The Unified Collective-Communication Architecture
## NCFW (14)
- collectives/ncfw/ncfw-iram-images.md             — NCFW IRAM Images + Host Selector
- collectives/ncfw/ncfw-dram-ctx-log.md            — NCFW DRAM Images + ctx_log Decoder
- collectives/ncfw/main-dispatch-loop.md           — NCFW Main Dispatch Loop
- collectives/ncfw/ring-kangaring.md               — Ring + Kangaring Collective
- collectives/ncfw/mesh-collective.md              — Mesh Collective
- collectives/ncfw/hierarchical-collective.md      — Hierarchical Collective
- collectives/ncfw/pring-descriptors.md            — pring (Persistent DMA Descriptor Ring)
- collectives/ncfw/neff-device-barrier.md          — NEFF Device Barrier
- collectives/ncfw/neff-host-barrier.md            — NEFF Host Barrier + Step-Config Sequencing
- collectives/ncfw/dma-reprogram-apb-bcast.md      — NCFW DMA Reprogram + APB Broadcast + Alloc Bitmap
- collectives/ncfw/spad-ccop-tsync.md              — NCFW spad-ctrl cc_op Table + tsync
- collectives/ncfw/cust3-doorbell-thunks.md        — NCFW CUST3 DMA Doorbell Thunks
- collectives/ncfw/ring-protocol-config-command.md — NCFW Ring Send/Wait + Config Schema + Host Command
- collectives/ncfw/lx-isa-naming-archid-synthesis.md — NCFW LX-ISA / DMA-Naming / arch_id-Diff / Orchestration Synthesis

# Part 11 — NEFF Container Format (11)
- neff/container-byte-format.md     — NEFF Container Byte Format
- neff/metaneff-io-abi.md           — metaneff Protobuf + var/mem_ref Device I/O ABI
- neff/seq-microcode.md             — Per-Engine TPB-Sequencer Microcode (64-byte word)
- neff/relocation-weights.md        — Relocation / Patch Subsystem + Weight Layout
- neff/assembly-pipeline.md         — Per-Engine Instruction-Block Assembly Pipeline
- neff/version-compat.md            — NEFF Version / Compatibility Model
- neff/format-reference.md          — The NEFF Format Reference (synthesis)
- neff/concrete-carve.md            — A Concrete NEFF, Carved Byte-by-Byte
- neff/neff-elf-relationship.md     — The NEFF ↔ ELF Relationship
- neff/container-capstone.md        — The NEFF Byte-Level Container Capstone
- neff/ntff-trace-parse-state.md    — ntff Trace Protobuf + simdjson NEFF Parse State

# Part 12 — Compiler Seam (12)
- compiler/compiler-map.md                  — The GPSIMD-Relevant Compiler Map + emit_*→opcode
- compiler/sundaisel.md                     — SundaISel Deep-Dive
- compiler/mx-path.md                       — The MX Microscaling Path (end-to-end)
- compiler/mx-device-bodies.md              — Byte-Decode of the MX Device Bodies
- compiler/tiling-memory-scheduling.md      — Tiling / Memory-Planning / Scheduling Backend
- compiler/bir-inst-roster.md               — The Penguin BIR Instruction Set + BIR→ISA Map
- compiler/fused-cc-lowering.md             — Fused-CC / nkilib Kernel Lowering (worked)
- compiler/opt-sync-insertion.md            — Optimization + Inter-Engine Sync Insertion
- compiler/collective-loadtime-rewrite.md   — The Collective NRT-Load-Time Rewrite (byte-level)
- compiler/dtype-engine-fanin-synthesis.md  — Dtype/Engine/Gen Fan-In + CC-Lane Synthesis
- compiler/nki-frontend.md                  — The NKI Frontend + Reference Simulator
- compiler/crossref-neuronxcc.md            — Compiler Cross-Reference to the neuronx-cc Wiki

# Part 13 — Control Plane: CSR / Address / Interrupt / Security (58)
## Address map (17)
- control/address/soc-master-map.md            — Cayman SoC Top-Level Address Map (master)
- control/address/addr-decode.md               — SoC Physical-Address Bitfield Layout (64-die mesh)
- control/address/pcie-bars.md                 — Host-Side PCIe BAR Address Map
- control/address/preproc-cc.md                — PREPROC / Compute-Cluster (CC) Address Subtree
- control/address/tpb-pool.md                  — TPB_0 POOL-Engine Address Subtree (8-core)
- control/address/evt-sem-regions.md           — TPB Event/Semaphore Regions (EVT_SEM)
- control/address/block-schema-xref.md         — The Block → Schema Cross-Reference
- control/address/pkl-db.md                    — al_address_map_db.pkl — Load + Schema + Top Blocks
- control/address/pkl-tpb-subtree.md           — pkl TPB-Engine Subtree
- control/address/pkl-dma-subtree.md           — pkl DMA-Engine Subtree
- control/address/pkl-hbm-subtree.md           — pkl HBM / Memory Subtree
- control/address/pkl-pcie-d2d-fabric.md       — pkl PCIe / D2D / Fabric Subtree
- control/address/pkl-intc-sprot-security.md   — pkl INTC / SPROT Security Subtree
- control/address/pkl-topsp-coverage.md        — pkl TOP_SP / Sync / MISC Subtree + Coverage Tally
- control/address/soc-q7-translation-windows.md — The SoC ↔ Q7 Translation Windows
- control/address/lsp-sram-window-map.md       — The LSP SRAM Window Map (build-time view)
- control/address/unified-soc-memory-map.md    — The Unified GPSIMD / Cayman SoC Memory Map
## CSR (20)
- control/csr/tpb-xt-local-reg.md       — CSR — tpb_xt_local_reg
- control/csr/xtensa-q7.md              — CSR — Xtensa Q7 Debug/Trace/PMU/OCD
- control/csr/xtensa-nx.md              — CSR — Xtensa NX (NX-vs-Q7 diff)
- control/csr/tpb.md                    — CSR — tpb (top-level cluster control)
- control/csr/pe-array-sequencer.md     — CSR — PE Array Sequencer (host-visible)
- control/csr/notific-queue.md          — CSR — NOTIFIC Queue
- control/csr/udma-m2s.md               — CSR — UDMA M2S
- control/csr/udma-s2m.md               — CSR — UDMA S2M
- control/csr/udma-gen-tdma.md          — CSR — UDMA_GEN + GEN_EX + TDMA_MODEL
- control/csr/intc-4group.md            — CSR — INTC 4-Group (no_msix + msix)
- control/csr/intc-1group-apintc.md     — CSR — INTC 1-Group + ap_intc (IOFIC)
- control/csr/qos-prot.md               — CSR — qos_prot (FIS QoS / AXI NTS)
- control/csr/remapper.md               — CSR — amzn_remapper / user_remapper
- control/csr/nsm.md                    — CSR — nsm (AXI Network-Security Monitor)
- control/csr/qos-pmu-hostvisible.md    — CSR — qos_pmu + qos_host_visible
- control/csr/rdm-top-sp.md             — CSR — rdm_model + top_sp
- control/csr/hbm-d2d-pcie-blocks.md    — CSR — HBM / D2D / PCIe Blocks
- control/csr/fis-errtrig-spad.md       — CSR — FIS Control + errtrig + spad
- control/csr/tpb-subblocks.md          — CSR — TPB Sub-Blocks (SBUF/events/act-table/DGE)
- control/csr/notific-sdma-residual.md  — CSR — NOTIFIC Variants + SDMA Residual + Coverage Ledger
## Interrupts (13)
- control/interrupt/sdma-triggers.md               — SDMA Trigger Set
- control/interrupt/io-fabric-triggers.md          — IO-Fabric Source Map
- control/interrupt/pcie-hbm-tpb-d2d-triggers.md   — PCIe / HBM / TPB / D2D Sources
- control/interrupt/peb-cc-topsp-triggers.md       — PEB Apex / CC / TOP_SP Sources
- control/interrupt/schema-atlas.md                — Trigger-YAML Schema Atlas + Source Map
- control/interrupt/physical-intc-instances.md     — Physical INTC Instance Map
- control/interrupt/errtrig-fis-routing.md         — errtrig / FIS Error Routing
- control/interrupt/abort-scandump-clockstop.md    — Abort / Scan-Dump / Clock-Stop Control
- control/interrupt/q7-surprises-binding.md        — INTC → Q7 Firmware "Surprises" Binding
- control/interrupt/nsm-flow-unified.md            — NSM Fault → Isolation → IRQ + Unified Synthesis
- control/interrupt/xea3-interrupt-architecture.md — The XEA3 Interrupt / Exception Architecture
- control/interrupt/handler-bodies.md              — The Interrupt/Exception Handler Bodies
- control/interrupt/device-host-notification.md    — Device→Host Interrupt / Notification Path + Sync/Events
## Security (8)
- control/security/boot-fault-overview.md          — Boot / Fault Chain Overview
- control/security/soc-fabric-perimeter.md         — The SoC-Fabric Perimeter
- control/security/boot-arming-fault-recovery.md   — Boot-Arming + Device Fault Recovery
- control/security/profiling-trace-debug-gating.md — Profiling / Trace / Debug + Access Gating
- control/security/reachability-isolation.md       — Custom-Op Reachability / Isolation Model
- control/security/trust-chain-threat-model.md     — Firmware Trust Chain + Threat Model
- control/security/side-channel-leakage.md         — Information-Leakage / Side-Channel Surface
- control/security/security-synthesis.md           — SEC-Lane Synthesis (boot→attest→fault)

# Part 14 — ISS as Executable Oracle (20)
- iss/cas-core-surface.md           — libcas-core Surface + ISS Plugin ABI
- iss/cas-arith-sem.md              — cas Vector-Arithmetic Semantics
- iss/cas-mac-fmac.md              — cas/fiss MAC / 2×-FMAC Semantics
- iss/cas-load-store.md            — cas/fiss Load/Store (LSU) Semantics
- iss/cas-supergather.md           — cas/fiss SuperGather Semantics
- iss/cas-predicate-boolean.md     — cas/fiss Predicate / Boolean (vbool) Semantics
- iss/cas-convert-pack-fp.md       — cas/fiss Convert / Pack / FP Semantics
- iss/cas-valign-shuffle-reduce.md — cas/fiss VALIGN / Shuffle-Select / Reduce
- iss/fiss-surface-exceptions.md   — libfiss-base Surface + Exception Model
- iss/fiss-slotfill-f0-f3.md       — fiss Slotfill — FLIX Formats F0–F3
- iss/fiss-slotfill-f4-f11.md      — fiss Slotfill — FLIX Formats F4/F6/F7/F11
- iss/fiss-slotfill-n0-n2.md       — fiss Slotfill — Narrow Formats N0/N1/N2
- iss/fiss-datapath-oracle.md      — fiss Datapath — the 864-Leaf Value Oracle
- iss/cas-timing-model.md          — libcas-core — The Cycle/Pipeline Timing Model
- iss/libtie-core-msem.md          — libtie-core + libtie-Xtensa-msem (TIE Runtime)
- iss/libctype-cstub.md            — libctype — CSTUB Custom-Type Functions
- iss/ref-vs-production-diff.md    — ref-vs-production ISS Variant Diff
- iss/iss-semantic-synthesis.md    — The ISS Semantic-Model Synthesis
- iss/runnable-iss-infra.md        — Running the ISS — Invocation / Config / Memory / Trace / Perf
- iss/iss-oracle-synthesis.md      — ISS Introspection / Single-Step / Fault / SystemC + Oracle Synthesis

# Part 15 — Validation & Verification (10)
- validation/four-oracle-method.md          — The 4-Oracle Bit-Exact Differential Method
- validation/fp-soft-float.md               — VAL — fp16/fp32 Soft-Float Family
- validation/mac-multiply.md                — VAL — MAC / Multiply Family
- validation/convert-pack-cast.md           — VAL — Convert / Pack / Cast Family
- validation/reduce-shift-shuffle.md        — VAL — Reduce / Shift / Shuffle-Select Family
- validation/gather-scatter.md              — VAL — Gather / Scatter (SuperGather) Family
- validation/predicate-classify.md          — VAL — Predicate / Classify / Compare Family
- validation/transcendental-seed.md         — VAL — fp Transcendental Seed/Refine Family
- validation/regfile-bridge-divergence.md   — VAL — Regfile-Bridge / Accumulator-Readout + Divergence Catalog
- validation/capstone-matrix.md             — VAL — Residual Closures + the Per-Family Pass/Fail Capstone

# Part 16 — Appendices (15)
- appendix/struct-census-overview.md          — Struct Census Overview
- appendix/struct-host-runtime-layouts.md     — Host-Runtime Struct Layouts (field-exact)
- appendix/struct-device-firmware-globals.md  — Device-Firmware Global Structs (field-exact)
- appendix/struct-exec-state-census.md        — Host Execution-State Structs + Census Close
- appendix/opcode-kernel-engine-matrix.md     — The Opcode ↔ Kernel ↔ Engine Matrix
- appendix/isa-encoding-appendix.md           — The Master ISA Encoding Appendix
- appendix/csr-field-table-index.md           — The CSR Field-Table Index
- appendix/codename-crosswalk-table.md        — The Codename ↔ NC-ver ↔ coretype ↔ arch_id Cross-Walk
- appendix/open-questions-register.md         — The Open-Questions Register
- appendix/coverage-ledger.md                 — The Coverage Ledger
- appendix/do-not-repeat-full-ledger.md       — The Full Do-Not-Repeat / Correction Ledger
- appendix/bibliography-source-binaries.md    — Bibliography of Source Binaries
- appendix/abbreviations-index.md             — Abbreviations & Symbol Index
- appendix/changelog-provenance.md            — Provenance & Methodology Changelog
- appendix/reimplementation-checklist.md      — The Reimplementation Checklist

LEGACY SOURCE (superseded by the Parts above — kept on disk for authors to mine,
NOT linked): src/topics/{overview,toolchain,build-pipeline,programming-model,
aten-abi,wrapper-anatomy,memory-model,multicore,stack-switching,lsp-elf,licensing}.md
→ content destinations: overview→Part 1; aten-abi/wrapper-anatomy/programming-model/
build-pipeline/lsp-elf/licensing→Part 7; memory-model/multicore/stack-switching→Part 7/8.
=========================================================================== -->
