# Compile-Flow Walkthrough

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (wheel version `0.0.40`; the runtime-reported `0.103` is not statically verifiable in the binary, so the build-id is the unambiguous anchor: `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped — full C++ symbols; `.text` VA == file offset). Other wheel versions will differ; treat every VA as version-pinned.*

## Abstract

This page follows **one program** — a single `bf16` matmul — from the StableHLO module a framework hands across the PjRt boundary down to the packed VLIW bundle bytes a TPU TensorCore issues. It is the orientation map for the compiler: a narrative spine that names each IR level, the IR/dialect it speaks, the few passes that matter at that level, and the deep page that owns the full detail. Read it first; then dive into the page each stage links to.

The descent is **not** the textbook MLIR cascade `HLO → MHLO → tpu → LLO`. There are two distinct lowering trees inside this one shared object, and only one of them is the TPU device path. The **device path** (the one our matmul takes) is `HLO → [≈97 HLO pre-passes] → HLO → [layout / fusion / scheduling / memory] → HLO → [jellyfish per-op Emitters via LloRegionBuilder] → LLO → [bundle packer] → per-gen bundle bytes`. General tensor IR is lowered **directly to LLO** by hundreds of C++ `*Emitter` classes — it never becomes `tpu`-dialect MLIR. The `mlir::tpu` (Mosaic) dialect and the XTile dialect both ship in the binary but sit off the device path: `tpu` only arrives as a serialized MLIR module imported from a Pallas/Mosaic `kCustomCall`, and XTile is the XLA CPU/GPU tiled-fusion codegen dialect, never reached by the matmul lowering. The most common map of this compiler is wrong on exactly this point; the per-stage sections below mark where.

The reference frame is XLA-on-LLVM. If you know XLA's HLO optimization pipeline, layout assignment, the `LatencyHidingScheduler`, and `MemorySpaceAssignment` (MSA), you already know four of the seven stages — they are the open-source passes, run with a TPU `Target` and interleaved with ~137 TPU-private `xla::jellyfish::*` passes. The genuinely TPU-specific descent is the last two stages: the HLO→LLO emitter wall and the per-generation bundle packer. LLO is the TPU's low-level VLIW IR — flat `LloInstruction`s, one per machine op, the last IR above the wire. Silicon-specific choices (which MXU data-format, which native vreg shape, how many bytes per bundle) enter late, almost all of them threaded through one `xla::jellyfish::Target` object that every stage consults.

For reimplementation, the orientation contract is:

- The **seven IR levels** and the single function entry that drives each: `CompilePhase0StablehloToHlo` (`0xf84de60`) → `RunHloPasses` (`0x1093a420`) → `LayoutAssignment` (`0x169bf440`) → `LatencyHidingScheduler` (`0x136321a0`) → `RunMemorySpaceAssignment` (`0x12fc3080`) → jellyfish `*Emitter` / `LloRegionBuilder` → `BundlePacker` (`0x13b206a0`).
- The **two-tree split**: device path (HLO → LLO direct emit) vs. the bundled-but-off-path XTile (CPU/GPU) and Mosaic-`tpu` (Pallas-import) trees.
- The **`Target` as the silicon switch**: data-format, native vreg shape, and bundle width are all per-generation values read from one `Target` argument, not branched on a global.

| | |
|---|---|
| **Front-door input** | StableHLO MLIR bytecode (CHLO/VHLO mixed) across the PjRt boundary |
| **Wire output** | per-gen VLIW bundle bytes — JF **41 B** / PF **51 B** / VF·GL **64 B** |
| **Monolithic driver** | `DeepseaCompilerBase::RunHloPasses` @ `0x1093a420` |
| **Separate-compile phases** | `phase0_stablehlo_to_hlo` · `phase1_hlo_opts` · `phase2a_tlp_lowering` · `phase2b_deduped_lowering` · `phase3_linking` |
| **Silicon switch** | `xla::jellyfish::Target const&` — threaded into every stage |
| **The matmul we trace** | one `bf16 × bf16 → f32` `kDot`, contracting one dim |

---

## Stage map at a glance

The seven IR levels our matmul passes through, the dialect each speaks, the key passes there, and the page that owns the full story. **Tree** marks whether the level is on the TPU device path (D) or one of the bundled off-path trees (X = XLA CPU/GPU XTile, M = Mosaic-`tpu` Pallas import).

| # | IR level | Dialect / form | Key passes (entry VA) | Tree | Owning deep page |
|---|----------|----------------|------------------------|------|------------------|
| 0 | Front-door bytecode | StableHLO + CHLO/VHLO | `CompilePhase0StablehloToHlo` (`0xf84de60`) | D | [HLO Ingestion](../compiler/hlo-ingestion.md) |
| 1 | HLO pre-passes | XLA HLO | `RunHloPasses` (`0x1093a420`); `TpuHloSupportChecker` (`0x11071480`) | D | [HLO Pre-Passes](../compiler/hlo-pre-passes.md) · [Compile Phases](../compiler/compile-phases.md) |
| 2 | Layout / sharding | XLA HLO + layouts | `LayoutAssignment` (`0x169bf440`); `TpuLayoutAssignment` (`0x110ace00`) | D | [Layout Assignment](../compiler/layout-assignment.md) · [Sharding Propagation](../compiler/sharding-propagation.md) |
| 3 | Fusion + scheduling | XLA HLO (scheduled) | `LatencyHidingScheduler` (`0x136321a0`); `TpuInstructionFusion` | D | [LHS Core](../sched/latency-hiding-scheduler-core.md) · [Fusion Patterns](../compiler/fusion-patterns.md) |
| 4 | Memory-space assignment | XLA HLO + memspace | `RunMemorySpaceAssignment` (`0x12fc3080`) | D | [MSA Overview](../compiler/msa-overview.md) |
| 5 | HLO → LLO emit | LLO (VLIW IR) | jellyfish `*Emitter` via `LloRegionBuilder`; `MatrixMultiplyAccumulateFunctor::operator()` (`0x1310cd80`) | D | [Dot/Conv → MXU Lowering](../compiler/dot-conv-mxu-lowering.md) · [LLO Opcode Enum](../isa/llo-opcode-enum.md) |
| 6 | Bundle packing | `vector<Bundle>` | `BundlePacker::runOnMachineFunction` (`0x13b206a0`) | D | [LLO Bundle Packing](../sched/llo-bundle-packing.md) |
| 7 | Bundle bytes | per-gen wire word | `Encoder<gen>::EncodeBundleInternal` | D | [Bundle Model](../isa/bundle-model-overview.md) |
| — | Mosaic `tpu` import | `mlir::tpu` MLIR | `createLowerToLLOPass` (`0x11203ba0`); imported via `GetMlirModuleOpFromCustomCall` (`0x13e327a0`) | M | [tpu → LLO ODS](../compiler/tpu-to-llo-ods.md) · [Mosaic Overview](../compiler/mosaic-overview.md) |
| — | XLA XTile codegen | `xla::xtile` MLIR | `StablehloLowerToXtilePass` (`0x15060560`) | X | [MHLO/XTile/tpu Lowering](../compiler/mhlo-xtile-tpu-lowering.md) |

> **GOTCHA —** rows 0–7 are the path the traced matmul actually walks. The last two rows are *bundled but off-path*: a plain `kDot` never visits the `tpu` dialect or XTile. The `tpu` dialect appears only for Pallas/Mosaic kernels the framework hands in as serialized MLIR; XTile is the XLA CPU/GPU backend's tiled-fusion codegen, depending only on the LLVM/CPU dialect set. Confusing either for "the TPU lowering of MHLO" is the single most common error in reading this compiler.

---

## Stage 0 — Front door: StableHLO bytecode → HLO

Our matmul does **not** enter as XLA HLO. It enters as **portable MLIR bytecode** — a StableHLO module, with CHLO and VHLO ops mixed in, that the framework bridge (JAX, the TF/XLA bridge, or PyTorch/XLA) serialized across the PjRt boundary. The dot is a `stablehlo.dot_general` with contracting-dimension numbers and a `bf16` operand element type.

The compiler's first act is a **format crossing**, not an optimization. `xla::CompilePhase0StablehloToHlo` (`0xf84de60`) parses the bytecode into an in-memory `mlir::ModuleOp`, runs an ordered MLIR pipeline that legalizes CHLO→StableHLO→MHLO (the `createChloLegalize*` / `createStablehloLegalizeToHlo` passes), walks the MHLO module emitting an `xla::HloProto`, then parses that proto back into the `xla::HloModule` / `HloInstruction` graph the rest of XLA is written against. After this stage our `stablehlo.dot_general` is an `HloInstruction` of opcode `kDot`. Everything downstream of here, up to Stage 5, operates on that HLO graph — MHLO/StableHLO/VHLO exist only at this boundary and as the JAX-bridge wire format.

The deep page owns the bytecode format on both sides, the CHLO/VHLO version-skew handling, and the proto round-trip.

→ [HLO Ingestion](../compiler/hlo-ingestion.md) · framing of the descent: [How to Read This Book](how-to-read.md)

---

## Stage 1 — HLO pre-passes: the ≈97-row scrub

The HLO graph now runs the long pre-lowering pipeline, built and dispatched by `xla::jellyfish::DeepseaCompilerBase::RunHloPasses` (`0x1093a420`) — the function behind the public C ABI export and behind `xla::CompilePhase1HloOptimizations` (`0xf84ee00`) in the separate-compilation flow. The pipeline container is the ordinary `xla::HloPassPipeline`; there is no TPU-private pipeline class. Roughly 97 passes run in six conceptual phases, each pass derived directly from `xla::HloPassInterface`, with `MaybeAddInvariantCheckers` (`0x10944600`) re-validating structure (HloVerifier, scheduling-annotation legality, cycle detection) after **every** pass.

Most of these passes ignore our matmul — they expand custom-calls (`TpuCholeskyExpander`, `TpuQrExpander`, RNG, gather/scatter), decompose dynamic shapes (`DynamicPadder`), and canonicalize. Two touch it directly. `xla::jellyfish::XPrecisionRewriter` is the one that would fire if the dot carried an `x6` / `x9` / `x128` high-precision annotation, splitting it into a 2-, 3-, or 8-step accumulation chain of lower-precision dots; a plain `bf16` dot passes through untouched. And the canonical acceptance gate, `xla::TpuHloSupportChecker::RunImpl` (`0x11071480`, pass name `tpu_hlo_support_checker`), walks every computation and validates every result `Shape` via `ShapeUtil::ValidateShapeWithOptionalLayout` — it never mutates the module, it only rejects HLO the TPU backend cannot lower. Our `bf16` dot passes the gate. Of the ≈97 passes, ~33 are explicitly TPU-private (`xla::jellyfish::*`); the rest are open-source XLA run with a TPU `Target`.

→ [HLO Pre-Passes](../compiler/hlo-pre-passes.md) · [Compile Phases](../compiler/compile-phases.md) · [HLO Pass Registry](../compiler/hlo-pass-registry.md) · [Algebraic Simplifier](../compiler/algebraic-simplifier.md)

---

## Stage 2 — Layout & sharding: where the data lands

Sharding runs first (between the two halves of the pipeline): `xla::ShardingPropagation` for manual annotations, or `xla::TpuAutoSharding` + `xla::sdy::ShardyXLA` for the auto / Shardy flows, followed by `xla::jellyfish::TpuSpmdPartitioner` for the per-partition rewrite. Our single-device matmul is `Replicated`, so sharding is a no-op for it, but this is where a multi-chip matmul would gain its collectives.

Then **layout assignment** decides physical minor-to-major orders for every array. The driver is the open-source `xla::LayoutAssignment::RunImpl` (`0x169bf440`), specialized for TPU by `xla::jellyfish::TpuLayoutAssignment::RunImpl` (`0x110ace00`), which adds TPU-specific constraints — including indices-layout constraints for gather/scatter and the tiling rules the MXU prefers. For our matmul this is a pivotal decision: the layout chosen for the LHS, RHS, and result determines whether the systolic array can stream operands without a transpose, and the tiling assignment (`TpuTilingAssignment`, `WindowConfigAssignment`) fixes the tile shape the MXU emitter will later loop over. This stage is the first place a silicon-specific choice — the native tile geometry — enters, read from the `Target`.

→ [Layout Assignment](../compiler/layout-assignment.md) · [Sharding Propagation](../compiler/sharding-propagation.md) · [Auto-Sharding / SPMD](../compiler/auto-sharding-spmd.md)

---

## Stage 3 — Fusion & scheduling: the order of work

With layouts fixed, fusion groups producer/consumer ops into fusion computations. The TPU's main fusion pass is `xla::jellyfish::TpuInstructionFusion` (pass name `tpu_fusion`), which drives a priority-fusion queue across the "Pre main fusion" / "Main fusion" / "Post main fusion" pipeline phases. If our matmul is followed by an elementwise bias-add or activation, fusion folds them into one fusion node so the emitter can keep the result in vregs instead of round-tripping through memory.

Scheduling then linearizes the graph. The canonical pass is `xla::LatencyHidingScheduler::RunImpl` (`0x136321a0`, name `latency-hiding-scheduler`) — the same XLA LHS used elsewhere, run here with a TPU cost model so it can overlap our matmul's MXU work with the DMAs that feed it and drain it. An ILP-flavored variant and a per-layer scheduler (`LatencyHidingLayerScheduler`) exist for the cases where greedy list scheduling under-laps; the scheduler stage runs the `async_scheduling` `HloPassPipeline` (the byte-verified pipeline name carrying the LHS itself), after the base memory-scheduling pass. The schedule produced here is what the much-later [bundle packer](../sched/llo-bundle-packing.md) consumes IR order from — the two are different granularities (HLO instruction order vs. VLIW slot fill) of the same "what runs when" question.

> **NOTE —** the macro scheduler (LHS, this stage) and the micro scheduler (the per-region greedy bundle packer at Stage 6) are independent. LHS reorders HLO instructions to hide latency across the whole computation; the packer locally fills VLIW slots within one region in IR order. Neither is modulo scheduling — inner-loop software pipelining is a third, separate path.

→ [LatencyHidingScheduler Core](../sched/latency-hiding-scheduler-core.md) · [LHS ILP Variant](../sched/lhs-ilp-variant.md) · [Fusion Patterns](../compiler/fusion-patterns.md) · [Fusion Cost Model](../compiler/fusion-cost-model.md)

---

## Stage 4 — Memory-space assignment: VMEM vs. HBM

A scheduled HLO graph still says nothing about *where* each buffer lives. Memory-space assignment (MSA) places every value in HBM, VMEM, or CMEM and inserts the copies/prefetches that move data into the fast on-chip scratch before it is needed. The TPU driver is `xla::jellyfish::RunMemorySpaceAssignment` (`0x12fc3080`), which configures and runs the open-source engine `xla::memory_space_assignment::MemorySpaceAssignment::Run` (`0x1dc2e200`) plus its best-fit repacker. For our matmul, MSA is what decides that the LHS and RHS tiles are prefetched into VMEM so the MXU never stalls on HBM latency, and it sizes the VMEM working set against the per-generation budget read from the `Target`. The per-version default budgets and the HBM reservation policy live on their own pages.

→ [MSA Overview](../compiler/msa-overview.md) · [MSA Per-Version Defaults](../compiler/msa-per-version-defaults.md) · [MSA Reservation / HBM Policy](../compiler/msa-reservation-hbm-policy.md) · [VMEM Allocator](../memory/vmem-allocator.md)

---

## Stage 5 — HLO → LLO: the emitter wall (and where MHLO does *not* go)

This is the genuinely TPU-specific descent and the stage most likely to be mis-described. The scheduled, laid-out, memory-assigned HLO graph is lowered **straight to LLO** — the TPU's low-level VLIW IR — by hundreds of C++ `*Emitter` classes, each owning one HLO op family and building LLO into a region via `xla::jellyfish::LloRegionBuilder`. There is **no MHLO→`tpu`-dialect conversion pass** for general programs; the `tpu` MLIR dialect is produced only by the Pallas/Mosaic frontend and imported separately (see the callout below).

Our `bf16` matmul reaches the systolic array through one descent shared with convolution: an upstream pass rewrites `kDot` into `kConvolution`, so a single lowering serves both. A per-window tile-cost comparator picks the systolic tiling, an emission-strategy dispatch (`GetEmitFunctorFromEmitFunctorEnum` `0x130e8de0`, a 19-case `switch`) selects one of the MXU strategies, and the codegen body `xla::jellyfish::MatrixMultiplyAccumulateFunctor::operator()` (`0x1310cd80`, which takes an `LloRegionBuilder`) produces a tiled loop nest of LLO matmul ops. The matmul atom is a strict 3-or-4-instruction sequence per native chunk — `llo.vmatprep` → `llo.vmatmul` → (optional pack) → `llo.vmatres` (the `kVectorMatprep*` / `kVectorMatmul` / `kVectorMatres` opcode band). The data-format choice (`bf16×bf16→f32` vs. an `f8` or `int8` variant) and the MXU register-bank assignment are selected here from the operand element types and the `Target` — the second place silicon-specific behavior enters, now at instruction granularity. Elementwise ops fused onto the matmul lower through the scalar/vector LLO atom tables; LLO-level allocation (`LloAllocation`, an interval-tree live-range allocator) and region analysis run as part of this stage.

> **NOTE —** the device-path HLO matmul emitter (`MatrixMultiplyAccumulateFunctor::operator()` `0x1310cd80`, building into an `LloRegionBuilder`) and the Mosaic-path atom emitters `mlir::tpu::(anonymous namespace)::EmitMatmuls` (`0x11241460`) / `EmitLatches` (`0x112403c0`) both produce the same `kVectorMatprep*`/`kVectorMatmul`/`kVectorMatres` band, but they are *different* entries reached from *different* inputs. `EmitMatmuls`/`EmitLatches` take an `mlir::ConversionPatternRewriter` and are called only from the `mlir::tpu::MatmulOp` / `MatmulAccLhsOp` conversion patterns inside `createLowerToLLOPass` (`0x11203ba0`) — i.e. the off-path Mosaic `tpu`→LLO import below, not our `bf16` HLO matmul. The two descents converge on the same LLO atom at the LLO level; they do not share a function.

> **GOTCHA — the `tpu` and XTile dialects are off this path.** General MHLO is **never** lowered to `tpu` MLIR. The `mlir::tpu` (Mosaic) dialect only enters as a serialized MLIR module extracted from a Pallas/Mosaic `kCustomCall` by `xla::jellyfish::mlir_utils::GetMlirModuleOpFromCustomCall` (`0x13e327a0`); that module is canonicalized, has its vector layout inferred (`createInferVectorLayoutPass` `0x132c2c20`, `createApplyVectorLayoutPass` `0x1325cda0`), then lowered to LLO by `mlir::tpu::createLowerToLLOPass` (`0x11203ba0`) — joining our matmul's stream at the LLO level. Separately, the `xla::xtile` dialect (`StablehloLowerToXtilePass` `0x15060560`) is the XLA **CPU/GPU** tiled-fusion codegen, depending only on the LLVM/CPU dialect set; it is bundled into `libtpu.so` but the jellyfish TPU lowering never calls it.

→ [Dot/Conv → MXU Lowering](../compiler/dot-conv-mxu-lowering.md) · [LLO Opcode Enum](../isa/llo-opcode-enum.md) · [Slot — MXU](../isa/slot-mxu.md) · Mosaic-import path: [tpu → LLO ODS](../compiler/tpu-to-llo-ods.md) · [Mosaic Overview](../compiler/mosaic-overview.md) · XTile (off-path): [MHLO/XTile/tpu Lowering](../compiler/mhlo-xtile-tpu-lowering.md)

---

## Stage 6 — Bundle packing: LLO ops → VLIW slots

The flat list of `LloInstruction`s — our matmul is now a loop nest of `vmatprep`/`vmatmul`/`vmatres` plus its prefetch DMAs and any fused elementwise ops — is packed into fixed-width VLIW bundles by `BundlePacker::runOnMachineFunction` (`0x13b206a0`, the LLVM-backend `tpu-bundle-packer` pass). The algorithm is **forward greedy list scheduling**: walk LLO ops in IR order, compute each op's earliest legal bundle from its read-after-write dependencies, ask a per-generation `SlotTracker` for the first bundle whose remaining capacity admits the op's `BundleRequirement`, append empty bundles when none fits, and commit. Each generation has a different slot grid — number of MXU slots, vector source ports, immediate slots, predicate fields — so the same LLO stream packs into a different number of bundles per silicon. Empty slots are stamped with the `kNeverExecute = 31` predicate so an absent slot is a defined no-op. The output is a `vector<Bundle>` of typed sub-fields.

→ [LLO Bundle Packing](../sched/llo-bundle-packing.md) · [Bundle Modulo Scheduling](../sched/bundle-modulo-scheduling.md) · [Scheduler Resource Model](../sched/scheduler-resourcetype-model.md) · [Bundle-Aware Cost](../cost/bundle-aware-cost.md)

---

## Stage 7 — Bundle bytes: the per-generation wire word

Each typed `Bundle` is serialized to raw bytes by the per-generation encoder. This is the last place silicon matters, and it matters most: the **bundle width itself is a per-generation constant**, returned inline from a codec-metadata table keyed on `(TpuVersion, TpuSequencerType)`.

| Generation (codename) | Bundle width | Codec-metadata anchor |
|---|---|---|
| Jellyfish (≤ v3) | **41 B / 328 bit** | `JellyfishCodecMetadata::BundleSizeBytes` @ `0x1ecf7460` → 41 |
| Pufferfish (v4) | **51 B / 408 bit** | `EncoderPfTensorCore::BundleSizeBytes` @ `0x1d227740` → 0x33 |
| Viperfish (TPU v5 / v5e / v5p) | **64 B / 512 bit** | `ViperfishCodecMetadata::BundleSizeBytes` @ `0x1ee71320` → 64 |
| Ghostlite (TPU v6 lite / v6e) | **64 B / 512 bit** | `GhostliteCodecMetadata::BundleSizeBytes` @ `0x1eeb7640` → 0x40 |

The encoders themselves diverge in mechanism, not just width. Jellyfish is a *direct-pack* encoder: `EncoderJf::EncodeBundleInternal` builds a 53-byte scratch struct with `shl`/`and`/`or` arithmetic and strips the first 12 bytes (output byte N == struct byte 0x0C+N). Pufferfish and every V5+ generation instead `memset` a zero buffer and write each field with the shared bit-packing primitive `BitCopy(dst, dst_bit, src, src_bit, nbits)` (`0x1fa0a900`), so a field's absolute bundle bit *is* its `BitCopy` argument. The same 64-byte width is shared by Viperfish, Ghostlite, and `6acc60406` (the three V5+ generations), but the slot *bit layout* differs (Ghostlite widens opcodes 7→8 bits and shifts the scalar/sequencer region +3 bits). Our matmul's `vmatmul` op lands in a `VectorExtended`/MXU slot whose exact bit position is one of these per-gen maps.

After this stage the program is a sequence of bundle bytes ready for the TensorCore to fetch and issue — the descent is complete.

→ [Bundle Model Overview](../isa/bundle-model-overview.md) · [Jellyfish 41-B Bundle](../isa/bundle-jf-41b.md) · [Pufferfish 51-B Bundle](../isa/bundle-pf-51b.md) · [Viperfish 64-B Bundle](../isa/bundle-vf-64b.md) · [Ghostlite Bundle](../isa/bundle-gl.md) · [MC-Emitter](../isa/mc-emitter.md) · codename glossary: [Codename Cheat-Sheet](codename-cheatsheet.md)

---

## Reading the rest of the book from here

Each stage above is a single paragraph over a deep page; this section is the index of where to go for the full algorithm.

| If you want… | Start at |
|---|---|
| The exact ≈97-pass order and per-pass rewrites | [HLO Pre-Passes](../compiler/hlo-pre-passes.md) |
| The phase taxonomy (`phaseN_*`, monolithic vs. separate compile) | [Compile Phases](../compiler/compile-phases.md) |
| How a layout is chosen and the MXU tiling constraint | [Layout Assignment](../compiler/layout-assignment.md) |
| The latency cost model the scheduler prices with | [LatencyHidingScheduler Core](../sched/latency-hiding-scheduler-core.md) · [Cost Overview](../cost/overview.md) |
| Where buffers live and how prefetch is inserted | [MSA Overview](../compiler/msa-overview.md) |
| The full matmul → MXU emission and the 19 strategies | [Dot/Conv → MXU Lowering](../compiler/dot-conv-mxu-lowering.md) |
| The LLO opcode space | [LLO Opcode Enum](../isa/llo-opcode-enum.md) |
| The Mosaic/Pallas `tpu`-dialect import path | [tpu → LLO ODS](../compiler/tpu-to-llo-ods.md) · [Mosaic Overview](../compiler/mosaic-overview.md) |
| The bit-exact bundle wire formats | [Bundle Model Overview](../isa/bundle-model-overview.md) and the per-gen bundle pages |
| What "Jellyfish / Pufferfish / Viperfish / Ghostlite" map to | [Codename Cheat-Sheet](codename-cheatsheet.md) |

## Cross-References

- [HLO Ingestion](../compiler/hlo-ingestion.md) — Stage 0; the StableHLO→HLO format crossing this page opens on
- [HLO Pre-Passes](../compiler/hlo-pre-passes.md) — Stage 1; the full ≈97-row pre-lowering pipeline
- [Compile Phases](../compiler/compile-phases.md) — the monolithic vs. separate-compilation phase taxonomy behind every stage
- [Layout Assignment](../compiler/layout-assignment.md) — Stage 2; physical layout + MXU tiling constraints
- [LatencyHidingScheduler Core](../sched/latency-hiding-scheduler-core.md) — Stage 3; the macro scheduler
- [Fusion Patterns](../compiler/fusion-patterns.md) — Stage 3; `TpuInstructionFusion` and the fusion buckets
- [MSA Overview](../compiler/msa-overview.md) — Stage 4; HBM/VMEM/CMEM placement
- [Dot/Conv → MXU Lowering](../compiler/dot-conv-mxu-lowering.md) — Stage 5; the matmul emitter wall in full
- [LLO Opcode Enum](../isa/llo-opcode-enum.md) — the LLO IR our matmul becomes at Stage 5
- [tpu → LLO ODS](../compiler/tpu-to-llo-ods.md) — the off-path Mosaic `tpu`-dialect import that rejoins at LLO
- [MHLO/XTile/tpu Lowering](../compiler/mhlo-xtile-tpu-lowering.md) — the off-path XTile (CPU/GPU) tree and the two-tree split
- [LLO Bundle Packing](../sched/llo-bundle-packing.md) — Stage 6; greedy slot-fill list scheduling
- [Bundle Model Overview](../isa/bundle-model-overview.md) — Stage 7; the per-gen wire-format model
- [Codename Cheat-Sheet](codename-cheatsheet.md) — the generation names this page uses for silicon-specific behavior
- [How to Read This Book](how-to-read.md) — evidence conventions and the confidence labels used throughout
- [Glossary](../glossary.md) — definitions of the IR and unit terms (LLO, MXU, VMEM, bundle) this walkthrough leans on
- [Subsystem Map](../subsystem-map.md) — where the compiler stage traced here sits among the nine subsystems
