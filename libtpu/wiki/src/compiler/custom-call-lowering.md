# Custom-Call Lowering & the Target Registry

> *All addresses, symbols, target strings, and proto names on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ.*

## Abstract

`HloOpcode::kCustomCall` is the single HLO opcode through which JAX, PyTorch/XLA, and Pallas inject behavior the core HLO set cannot express: Mosaic/Pallas device kernels, sharding and memory-placement markers, linear-algebra block primitives, host-offload, MegaScale collective metadata, and SDC instrumentation. The TPU compiler does **not** treat these uniformly — it dispatches on the instruction's `custom_call_target` **string**, a plain `std::string` matched by Swiss-table equality against a process-wide registry. This page is the specification for that dispatch layer.

The most important structural fact: `xla::jellyfish::CustomCallRegistration` is **not** a polymorphic base class. It is a holder of **five independent global registries**, each a `util_registration::FunctionRegistry<std::string, T>` for a different *facet* callback type (lowering emitter, can-fuse predicate, compilation properties, HLO cost analysis, SPMD partitioning visitor). All five are keyed by the same target string; a handler opts into whichever facets it needs at module-init time. There is no opaque encoding and no virtual table — the dispatch is `target_string → callback` lookup, repeated five times for five orthogonal questions the compiler asks about each custom-call.

For reimplementation, the contract this page fixes is:

- **The five-facet registry** — its five `Register*` entry points, their exact callback signatures, the `CompilationProperties` declarative-metadata struct, and the `google_init_module_*` seeding mechanism via `.init_array`.
- **The target catalog** — ~52 HIGH-confidence target strings across five categories, each with the handler/namespace constant that registers it and the lowering action it triggers.
- **The six lowering-action kinds** — pre-pass HLO rewrite, marker strip, emit-helper → LLO, Mosaic cached-MLIR-body, runtime/host-action passthrough, and SPMD-partition-only — and which targets take which.
- **The Mosaic import seam** — how `tpu_custom_call` (the escape hatch) carries a serialized `tpu`-dialect module in `backend_config.custom_call_config.mlir_module`, is parsed/cached as a `MosaicMlirCacheEntry`, and routed through `CustomCallEmitter::Emit`. (The downstream `tpu`-dialect pipeline is documented in [MHLO → XTile → tpu Lowering](mhlo-xtile-tpu-lowering.md) and [Mosaic Overview](mosaic-overview.md) — linked, not duplicated.)
- **The validation layers and error paths** — `HloVerifier` → `TpuHloSupportChecker` (where `__cudnn$…`/`__triton$…` targets are rejected) → `TpuCustomCallLegalizer`, and the recovered DCHECK/status strings each emits.

| | |
|---|---|
| **Dispatch layer** | `xla::jellyfish::CustomCallRegistration` (5 facet registries, source `…/jellyfish/custom_call_registration.h`) |
| **Key type** | `custom_call_target` : `std::string` (Swiss-table, default hash) |
| **Facet registries** | `RegisterLoweringEmitter` (3 overloads) · `RegisterCanFuse` · `RegisterCompilationProperties` · `RegisterHloCostAnalysis` · `RegisterSpmdPartitioningVisitor` |
| **Register entry points** | `RegisterLoweringEmitter` @ `0x10e8bf40`/`0x10c9de40`/`0x10e8f1c0` · `RegisterCanFuse` @ `0x10eb5680` · `RegisterCompilationProperties` @ `0x10f940e0` · `RegisterHloCostAnalysis` @ `0x111eee40` · `RegisterSpmdPartitioningVisitor` @ `0x14ba8000` |
| **Seeding** | `google_init_module_*()` per handler via `.init_array`, BSS-guarded |
| **Mosaic escape target** | `"tpu_custom_call"` → `CustomCallEmitter::Emit` @ `0x111ef740`, body via `GetCachedCustomCallBody` @ `0x13e31860` |
| **Mosaic cache** | `MosaicMlirCacheEntry`, keyed by `tsl::Fprint128`, stored on `HloModule` |
| **Validation** | `HloVerifier` → `xla::TpuHloSupportChecker` → `TpuCustomCallLegalizer::RunImpl` @ `0x11036080` |
| **Catalog size** | ~52 distinct HIGH-confidence target strings |
| **Confidence** | HIGH (symbol/string-anchored) unless a row or callout says otherwise |

---

## The Five-Facet Registry

`CustomCallRegistration` answers five orthogonal questions about a custom-call, and each question is its own registry. The decompiled symbol table confirms five distinct `FunctionRegistry<std::string, T>` instantiations (one `T` per facet), and the verbatim `Register*("Name", callback)` source-quotes survive in `.rodata` as macro-stringified DCHECK arguments, so the exact target strings and callback bodies are ground truth.

| Facet (registry) | Register entry point | Callback signature (recovered) | Question answered |
|---|---|---|---|
| Lowering emitter | `RegisterLoweringEmitter` ×3 | `StatusOr<OperandData>(HloInstruction*, LoweredGetter const&, LloRegion*, LloValue*, BackendConfigMap*, … context)` | How is this target emitted to LLO? |
| Can-fuse | `RegisterCanFuse` `0x10eb5680` | `bool(HloInstruction* producer, HloInstruction* consumer, Target const&, optional<FusionOptions>, optional<HloReachabilityMap const*>)` | May this op fuse with a neighbor? |
| Compilation properties | `RegisterCompilationProperties` `0x10f940e0` | `CompilationProperties(HloInstruction*)` | What declarative flags does this op carry? |
| HLO cost analysis | `RegisterHloCostAnalysis` `0x111eee40` | `Status(HloInstruction*, ShapeSizeFunction const&, HloCostAnalysis::Properties&)` | What are its FLOPs/bytes? |
| SPMD partitioning | `RegisterSpmdPartitioningVisitor` `0x14ba8000` | `Status(SpmdPartitioningVisitor*, HloInstruction*)` | How is it partitioned across shards? |

The `BackendConfigMap` in the emitter signature is recovered fully from the demangled `__policy_func` thunk: it is `absl::flat_hash_map<HloInstruction const*, std::unique_ptr<BackendConfig>>`. The full emitter prototype (the union of all three overloads' optional context tail) is:

```cpp
using LoweringEmitter = std::function<absl::StatusOr<OperandData>(
    const HloInstruction*        hlo,
    const LoweredGetter&         get_lowered,
    LloRegion*                   region,
    LloValue*                    output,
    BackendConfigMap*            backend_config_map,
    // optional context tail — present only in the wider overloads:
    const ProgramSharedRegistry*       registry        = nullptr,
    const LogicalTopologyInfo*         topology_info   = nullptr,
    sdc_reporter::SdcRegistrator*      sdc_registrator = nullptr,
    const llo_log::LogRecorder*        log_recorder    = nullptr,
    const HloInstruction*              original_hlo    = nullptr)>;
```

The three `RegisterLoweringEmitter` overloads differ **only** by how much of that context tail they accept; all three converge on the same `std::string`-keyed map. The narrowest form (no context) is used by simple ops like `PartialReduce`; the widest (with SDC/log/topology) by ops that touch the SDC checker or emit log records.

### `CompilationProperties` — the declarative metadata struct

Each handler may register a `RegisterCompilationProperties` callback returning a `CompilationProperties` aggregate. The recovered designated-initializer source-quotes pin the field set:

```cpp
struct CompilationProperties {
  bool has_communication            = false;  // carries ICI/DCN traffic
  bool supports_hlo_dedup           = false;  // HLO-level CSE-safe
  bool instruction_can_change_layout = true;  // layout assignment may relayout
  bool supports_internal_checksums  = false;  // SDC checker integration
  bool requires_mxu_assigner        = false;  // forces MXU register assignment
  bool check_fifos_are_empty        = false;  // must drain pipelines first
};
```

Recovered registrations (verbatim from `.rodata`) show how sparse these usually are — most set a single field:

| Target | Recovered `CompilationProperties` body |
|---|---|
| `kSharding`, `kPartialReduce`, `kDevicePlacement`, `kTopk`, `kTopkBatchMajorSmallK`, generic `target` | `{ .instruction_can_change_layout = false }` |
| `WindowPrefetchEmitter::kWindowPrefetch` | `{ .supports_hlo_dedup = true }` |
| `IciSdcTestEmitter::kKey` | `{ .has_communication = true, .supports_hlo_dedup = true }` |
| `SdcCheckerGetStatsEmitter::kKey`, `SdcCheckerReportSdcEventEmitter::kKey`, `SdcCheckerStartWithAlternativeCoresEmitter::kKey` | `{ .has_communication = false, .supports_hlo_dedup = true }` |

> **NOTE — `tpu_custom_call` cannot use a static `CompilationProperties`.** Unlike the fixed-target handlers above, the Mosaic escape has no compile-time-known properties — its behavior is the user's serialized MLIR. Its `RegisterCompilationProperties` callback therefore *queries the cached body at lookup time* to populate the struct dynamically. The recovered callback body (verbatim source-quote in `.rodata`) reads `has_communication` from the proto and queries `MosaicMlirCacheEntry::EmitsSdcChecksums` (→ `supports_internal_checksums`) and `MosaicMlirCacheEntry::RequiresMxuAssigner` (→ both `requires_mxu_assigner` and `check_fifos_are_empty`), with `supports_hlo_dedup = true` and `instruction_can_change_layout = true` fixed. See [The Mosaic Import Seam](#the-mosaic-import-seam).

### Registry seeding — `google_init_module_*`

Each handler ships a `google_init_module_<handler>()` function wired through the linker `.init_array` (not `__attribute__((constructor))`), each guarded by a `google_initializer_module_<handler>` BSS flag. On first touch these insert their `(target_string, callback)` pairs into the per-facet registries. Recovered initializers (addresses where extracted):

| Handler init function | Addr | Targets seeded |
|---|---|---|
| `google_init_module_custom_call_emitter` | `0x213ec9e0` | `tpu_custom_call` (Mosaic escape) |
| `google_init_module_alloc_handler` | `0x213ed4e0` | `AllocateBuffer` |
| `google_init_module_assume_handler` | `0x213ed5e0` | `AssumeGatherIndicesInBound` |
| `google_init_module_qr_handler` | `0x213edd80` | `QrDecompositionBlock`, `CompactWyHelper`, `InvertDiagBlocks*` |
| `google_init_module_resize_handler` | `0x213edee0` | `ResizeBilinear[Grad]`, `ResizeNearest[Grad]` |
| `google_init_module_topk_handler` | `0x213ee300` | `TopK`, `TopKWithUnique` |
| `google_init_module_x64_handler` | `0x213ee500` | `X64Combine`, `X64SplitLow/High` |
| `google_init_module_sliceid_handler` | `0x213ee260` | `SliceId` |
| `google_init_module_xla_llo_log_emitter` | `0x213ed920` | `kTpuLogCustomCallTarget` |
| `google_init_module_xla_sdc_checker_emitters` | `0x213ed220` | `xla-sdc-checker-get-stats`, `xla-sdc-checker-report-sdc-event`, `xla-sdc-checker-ici-sdc-test`, `xla-sdc-checker-start-with-alt-cores` |
| `megascale_custom_call_handler::google_init_module_…` | `0x213ed440` | `xla.megascale.provide_metadata` |

Plus per-emitter `google_init_module_*_emitter` initializers (`partial_reduce_emitter`, `window_prefetch_emitter`, `mosaic_broadcast_emitter`, `cholesky_emitter`, `qr_emitter`, `eigh_emitter`, `lu_emitter`, `invert_diag_blocks_emitters`, `compact_wy_emitter`, `padding_emitters`, `barrier_start_emitter`, `async_collective_{start,done}_emitter`, `barna_core_address_handler_emitter`, `topk_batch_major_small_k_emitter`). A reimplementation must guarantee the registry is fully populated before the HLO→LLO loop runs — the `.init_array` ordering achieves this at process start, before any compilation.

---

## Dispatch String Format

The `custom_call_target` field on `HloCustomCallInstruction` is a plain `std::string`; lookup is string-equality against the registry. Three naming conventions coexist:

| Convention | Meaning | Examples |
|---|---|---|
| **Bare CamelCase** | built-in TPU primitive lowerings | `Cholesky`, `QrDecompositionBlock`, `EighTpu`, `LuDecompositionBlock`, `TopK`, `TopKWithUnique`, `ResizeBilinear`, `Pin`, `Unpin`, `WindowPrefetch`, `AllocateBuffer`, `PadToStatic`, `SliceToDynamic`, `X128Combine`, `X64Combine`, `MaskAggregatorBlock`, `AssumeGatherIndicesInBound`, `MoveToHost`, `MoveToDevice`, `Sharding`, `SPMDFullToShardShape`, `BarrierStart`, `InspectSharding` |
| **lowercase_underscore** | generic dispatch containers | `tpu_custom_call` (Mosaic body in `backend_config`), `single_tpu_custom_call` (legalizer sentinel), `recover_custom_call` (debug-replay), `annotate_device_placement` (device-placement marker) |
| **Dotted namespace** | SDY / MegaScale escape | `xla.sdy.Sharding`, `xla.sdy.ShardingGroup`, `xla.sdy.FuncResultSharding`, `xla.sdy.GlobalToLocalShape`, `xla.sdy.LocalToGlobalShape`, `xla.sdy.PropagationBarrier`, `xla.megascale.provide_metadata` |

Two parsing rules are anchored:

- **`xla.sdy.` prefix detection** uses `hlo->custom_call_target().rfind("xla.sdy", 0)` (prefix test). Targets in this family round-trip through the Shardy importer.
- **`$`-prefixed names are reserved.** Recovered DCHECK (verbatim in decompiled output): `Invalid custom_call_target "%s": Call targets that start with '$' are reserved for internal use.` This is the gate that rejects cross-backend escapes like `__cudnn$convForward` and `__triton$…` — they pass the open-source `HloVerifier` but die in `TpuHloSupportChecker` (see [Validation Layers](#validation-layers-and-error-paths)).

---

## Custom-Call Target Catalog

~52 distinct HIGH-confidence target strings. Categorized; each row lists the namespace constant that registers it (recovered as a `k…` rodata anchor) and the lowering action. Action codes (A)–(F) are defined in [The Six Lowering Actions](#the-six-lowering-actions).

### Sharding markers

| Target | Registers via | Action / lowering |
|---|---|---|
| `Sharding` | `sharding_handler::kSharding` | (B) consumed by `ShardingPropagation`; bracket removed by `HloDomainRemover("sharding", …)` |
| `SPMDFullToShardShape` | (no handler; SpmdPartitioner) | (F) global→per-shard shape boundary; becomes a `Reshape` in the partitioned graph |
| `SPMDShardToFullShape` | (no handler; SpmdPartitioner) | (F) inverse; error if no sharding annotation present |

### SDY round-trip (Shardy)

| Target | Registers via | Action / lowering |
|---|---|---|
| `xla.sdy.Sharding` | `SdyCustomCallPattern` | rewritten to `mlir::sdy::ShardingConstraintOp` by `ImportSdyCustomCallsPass` |
| `xla.sdy.ShardingGroup` | `SdyCustomCallPattern` | → `mlir::sdy::ShardingGroupOp`; must have no uses after import |
| `xla.sdy.FuncResultSharding` | `getFuncResultSharding` | output-sharding carrier; stripped onto FuncOp result attr |
| `xla.sdy.GlobalToLocalShape` | `kGlobalToLocalShapeCallTargetName` | reshape boundary for `sdy::ManualComputationOp` round-trip |
| `xla.sdy.LocalToGlobalShape` | `kLocalToGlobalShapeCallTargetName` | inverse of above |
| `xla.sdy.PropagationBarrier` | PropagationBarrier importer | → `mlir::sdy::PropagationBarrierOp`; needs `allowed_direction` attr |
| `InspectSharding` | `RemoveInspectShardingCustomCall` | (B) removed unconditionally (JAX `inspect_array_sharding`); registered as a partitioner via `RegisterCustomCallPartitioner("InspectSharding")` |

### Memory placement

| Target | Registers via | Action / lowering |
|---|---|---|
| `MoveToHost` | `memory_annotations::kMoveToHostCustomCallTarget` | (B) `HostOffloader::HandleMoveToHostCustomCall` resets `memory_space`, deletes the call |
| `MoveToDevice` | `memory_annotations::kMoveToDeviceCustomCallTarget` | (B) inverse (`HandleMoveToDeviceCustomCall`) |
| `Pin` | `memory_annotations::kPinToDeviceCustomCallTarget` | (C) tensor→memref pin; "Pin custom_call should have a memref output" |
| `Pin` (SRAM) | `memory_annotations::kPinToDeviceSramCustomCallTarget` | (C) forces SRAM/VMEM placement |
| `Unpin` | memory_annotations_handler | (C) memref→tensor |
| `annotate_device_placement` | `memory_annotations::kDevicePlacement` / `device_placement_handler::kDevicePlacement` | forces device placement; `instruction_can_change_layout=false` (the `kDevicePlacement` constant resolves to the string `"annotate_device_placement"`) |

### Linear-algebra block primitives

| Target | Registers via | Action / lowering |
|---|---|---|
| `Cholesky` | `cholesky_handler::kCholesky` | (A) `TpuCholeskyExpander` (dot+TRSM) if pre-expanded; else (C) emit-helper |
| `QrDecompositionBlock` | `qr_handler::kQrDecompositionBlock` | (C) per-block QR (Givens), `emit_helper` |
| `CompactWyHelper` | `qr_handler::kCompactWyHelper` | (C) compact-WY builder for blocked QR |
| `InvertDiagBlocksLowerTriangular` | `qr_handler::k…LowerTriangular` | (C) triangular-solve expander helper |
| `InvertDiagBlocksUpperTriangular` | `qr_handler::k…UpperTriangular` | (C) upper-triangle variant |
| `EighTpu` | eigh_emitter | (C) block-Jacobi eigendecomposition, `emit_helper` |
| `LuDecompositionBlock` | lu_emitter | (C) per-block LU, `emit_helper` |
| `MaskAggregatorBlock` | anonymous emit_helper | (C) per-block mask aggregator (attention masking) |

### Sort / select / reduction

| Target | Registers via | Action / lowering |
|---|---|---|
| `TopK` | `topk_handler::kTopk` | (C) emit + (CanFuse=`true`) + cost + properties |
| `TopKWithUnique` | `topk_handler::kTopkWithUnique` | (C) same family |
| `TopKBatchMajorSmallK` | `topk_batch_major_small_k_handler::kTopkBatchMajorSmallK` | (C) specialised; CanFuse=`false` (recovered verbatim) |
| `ApproxTopK` | open-source XLA | verifier: even operand count, exactly 1 called_computation |
| `PartialReduce` | `partial_reduce_handler::kPartialReduce` | (C)+(F) `PartialReduceEmitter`; CanFuse=`true`; cost analysis registered |

### Memory / dynamic-shape / RNG / image

| Target | Registers via | Action / lowering |
|---|---|---|
| `AllocateBuffer` | `alloc_handler::kAllocateBuffer` | (C)+(F) static buffer reservation; memref result, no inputs |
| `WindowPrefetch` | `WindowPrefetchEmitter::kWindowPrefetch` | (C) `WindowPrefetchEmitter::Emit` @ `0x10f93f60`; `supports_hlo_dedup=true` |
| `PadToStatic` | `dynamic_padding_handler::kPadToStatic` | (C) `static_padding_emit_helper` |
| `SliceToDynamic` | `dynamic_padding_handler::kSliceToDynamic` | (C) `dynamic_padding_emit_helper` |
| `ResizeBilinear` / `Grad` | `resize_handler::kResizeBilinear[Grad]` | (C)+(F) custom HLO cost analysis per variant |
| `ResizeNearest` / `Grad` | `resize_handler::kResizeNearest[Grad]` | (C)+(F) `ResizeNearestHloCostAnalysis` |
| `RngBitGenerator` (HLO opcode, **not** a custom-call target) | `TpuRngBitGeneratorExpander` | (A) the `kRngBitGenerator` opcode is rewritten to Philox/ThreeFry HLO by the expander; listed here only because it is sometimes mistaken for a custom-call target — it never reaches the registry |

### Runtime intrinsics / precision / async / host / SDC

| Target | Registers via | Action / lowering |
|---|---|---|
| `DeviceId` | `deviceid_handler::kDeviceId` | (C) → `tpu.device_id` |
| `SliceId` | `sliceid_handler::kSliceId` | (C) returns slice id (MegaScale-aware) |
| `AssumeGatherIndicesInBound` | `assume_handler::k…` | (B) removed by `TpuAlgebraicSimplifier` after propagation; cost analysis registered |
| `X64Combine` | `x64_handler::kX64Combine` | (C) combine hi+lo of a 64-bit value |
| `X64SplitLow` / `X64SplitHigh` | `x64_handler::kX64Split{Low,High}` | (C)+(F) SPMD visitor so each shard owns its half |
| `X128Combine` | X128 emit helper | (C) 128-bit combine for `XPrecisionRewriter(kX128Precision)` |
| `PrepareAsyncCallStart` | `PrepareAsyncCallInserter::kPrepareAsyncCallStartTarget` | (C) → `tpu.async_start` |
| `PrepareAsyncCallDone` | `PrepareAsyncCallInserter::kPrepareAsyncCallDoneTarget` | (C) async completion marker |
| `kAsyncCollectiveStart` / `Done` | `async_collective_fusion_util::k…` | (C) async-collective pair; `emit_helper` |
| `kBarrierStart` | `async_barrier_util::kBarrierStart` | (C) barrier kickoff; `emit_helper` |
| `kDcnAllReduceStart` | DCN all-reduce kickoff | matched via `IsCustomCall("kDcnAllReduceStart")` |
| `HostExecute` | host_callback flow | (E) host execute; must have exactly one called computation |
| `xla.megascale.provide_metadata` | `runtime::kXlaMegaScaleCustomCallMetadataName` | (E) MegaScale collective metadata; `instruction_can_change_layout=false` |
| `xla-sdc-checker-start-with-alt-cores` | `SdcCheckerStartWithAlternativeCoresEmitter::kKey` | (C)/(E) SDC checker start |
| `xla-sdc-checker-ici-sdc-test` / `xla-sdc-checker-get-stats` / `xla-sdc-checker-report-sdc-event` | `IciSdcTestEmitter`/`SdcCheckerGetStatsEmitter`/`SdcCheckerReportSdcEventEmitter::kKey` | (C)/(E) SDC instrumentation |
| `kTpuLogCustomCallTarget` | `LogEmitter::kTpuLogCustomCallTarget` | (C) `OpEmitter::Emit<LogEmitter>` (recovered lambda) |

### The Mosaic escape

| Target | Registers via | Action / lowering |
|---|---|---|
| `tpu_custom_call` | `CustomCallEmitter` + `MosaicMlirCacheEntry` | (D) **the escape hatch** — `backend_config` carries the serialized `tpu`-dialect MLIR module; see below |

**Targets recognized but consumed silently** (rewritten into a CustomCall as an internal marker by another pass, then removed/expanded): `tf.XlaCallModule` (the MLIR-op-backed module-call carrier), `single_tpu_custom_call` (legalizer sentinel), `recover_custom_call` (debug-replay provenance). The keys `xla.sdy.in_shardings` / `xla.sdy.out_shardings` are **frontend-attribute names, not target strings**.

**Explicitly rejected** (never reach the registry): any `__<vendor>$…` target — filtered by `TpuHloSupportChecker` on the reserved `$` prefix.

---

## The Six Lowering Actions

Across all catalog targets, dispatch resolves to one of six action *kinds*. A single target may take more than one (e.g. `PartialReduce` is both (C) and (F)).

| Code | Action | When applied | Example targets |
|---|---|---|---|
| **(A)** | Rewrite to other HLO at pre-pass time | HLO PreOptimization | `Cholesky`→`TpuCholeskyExpander`, `RngBitGenerator`, Qr/Eigh/Lu/TriangularSolve/Fft opcodes |
| **(B)** | Strip marker after consumption | HLO mid-pipeline | `Sharding`→`ShardingPropagation`, `InspectSharding`→`RemoveInspectShardingCustomCall`, `MoveTo*`→`HostOffloader` |
| **(C)** | Lower to MLIR/LLO via emit-helper | HLO→LLO lowering loop | `TopK`, `PartialReduce`, `Resize*`, `AllocateBuffer`, `WindowPrefetch`, `PadToStatic`, `SliceToDynamic`, `DeviceId`, `SliceId`, `X64*`, the linalg blocks |
| **(D)** | Lower via cached MLIR module body | Mosaic path | `tpu_custom_call` (sole target) |
| **(E)** | Preserved to runtime as host-action marker | runtime emission | `xla.megascale.provide_metadata`, `HostExecute`, `xla-sdc-checker-*` |
| **(F)** | SPMD partition-only | SpmdPartitioner | `SPMDFullToShardShape`, `SPMDShardToFullShape`, `X64Split*`, `PartialReduce`, `Resize*`, `AllocateBuffer` |

### (A) Pre-pass rewrites

Targets in this class never reach the registry's emit-helper — an HLO pre-pass expands them into ordinary HLO first. (See [HLO Pre-Passes](hlo-pre-passes.md).)

| Target / opcode | Pre-pass class |
|---|---|
| `Cholesky` | `xla::TpuCholeskyExpander` |
| QR opcode | `xla::TpuQrExpander` |
| `Eigh` opcode | `xla::TpuEighExpander` |
| `Lu*` | `xla::LuDecompositionExpander` |
| `TriangularSolve` opcode | `xla::TpuTriangularSolveExpander` |
| `Fft` opcode | `xla::FftExpander` |
| `RngBitGenerator` | `xla::jellyfish::TpuRngBitGeneratorExpander` |
| `Gather` / `Scatter` | `xla::TpuGatherExpander` / `xla::TpuScatterExpander` |
| `RaggedAllToAll` | `xla::jellyfish::RaggedAllToAllExpander` |

`xla::jellyfish::MosaicFusion` also runs at this stage, but it is **not** triggered by a custom-call target — it lifts ordinary HLO sub-graphs into Mosaic-eligible kernels (emitting fresh `tpu_custom_call` instructions), so it has no entry in the registry catalog.

### (B) Marker strip

- `ShardingPropagation` consumes `Sharding`/`SPMDFullToShardShape`/`SPMDShardToFullShape`, emits `kDomain` brackets, then `HloDomainRemover("sharding", ApplyDomainSharding)` drops the brackets while retaining the sharding attribute ([Sharding Propagation](sharding-propagation.md)).
- `RemoveInspectShardingCustomCall::RunImpl` (`0x1278a040`) erases every `InspectSharding`, forwarding the operand.
- `HostOffloader::HandleMoveToHostCustomCall` (`0x110778a0`) / `HandleMoveToDeviceCustomCall` (`0x11078b00`) reset producer `memory_space` and delete the call.

### (C) Emit-helper path — the registry default

This is the common case: the HLO→LLO visitor sees a `kCustomCall`, looks up its target in the lowering-emitter registry, and invokes the callback, which emits `mlir::llo::*` (and helper `mlir::tpu::*`) ops into the `LloRegion` and returns the result `OperandData`. Most callbacks wrap a domain-specific `OpEmitter` subclass. Two recovered lambda bodies show the canonical shapes:

```cpp
// PartialReduce — narrow overload (no SDC/log context)
RegisterLoweringEmitter(partial_reduce_handler::kPartialReduce,
  [](const HloInstruction* hlo, const LoweredGetter& get_lowered,
     LloRegion* region, LloValue* output, BackendConfigMap* backend_config_map)
       -> absl::StatusOr<OperandData> {
    auto emitter = PartialReduceEmitter(hlo, get_lowered, region, output);
    TF_ASSIGN_OR_RETURN(auto return_value, emitter.Emit());
    return OperandData::CreateStaticOperandData(return_value, hlo->shape(), region);
  });

// LogEmitter — wide overload (uses log_recorder from the context tail)
RegisterLoweringEmitter(LogEmitter::kTpuLogCustomCallTarget,
  [](const HloInstruction* hlo, LoweredGetter get_lowered, LloRegion* llo_region,
     LloValue* output, BackendConfigMap* backend_config_map,
     const ProgramSharedRegistry* registry, const LogicalTopologyInfo* topology_info,
     sdc_reporter::SdcRegistrator* sdc_registrator, const llo_log::LogRecorder* log_recorder)
       -> absl::StatusOr<OperandData> {
    TF_ASSIGN_OR_RETURN(auto ret_val,
        OpEmitter::Emit<LogEmitter>(hlo, get_lowered, llo_region, log_recorder));
    return OperandData::CreateStaticOperandData(ret_val, hlo->shape(), llo_region);
  });
```

Both end identically (`OperandData::CreateStaticOperandData(value, hlo->shape(), region)`); the difference is only which slice of the context tail the emitter consumes.

### (E) Runtime / host-action targets

These survive HLO→LLO as a sentinel op, rewritten to a `HostCommand` (or `MegaScaleAction`) only at executable-emission time. They serialize as `Thunk::Kind::kCustomCall` entries via the shared upstream `xla::cpu/gpu::CustomCallThunkProto` (confirmed: `CustomCallThunkProto::_InternalSerialize`/`MergeImpl`/`ByteSizeLong` present). The TPU side reuses the proto but executes through `xla::megascale::runtime::CustomCallHandler` (`HandleCollectiveInput` `0x1cbd3a00`, `HandleCollectiveOutput` `0x1cbd4c20`, `AddPendingChain`/`GetPendingChain`/`ActiveGraph`), not the upstream thunk.

### (F) SPMD partition-only

For targets registered via `RegisterSpmdPartitioningVisitor`, the partitioner calls the registered visitor instead of its default handler. Recovered registrations (verbatim): `kAllocateBuffer`, `kPartialReduce`, `kResizeBilinear[Grad]`, `kResizeNearest[Grad]`, and a generic `target` (uniform partitioner). Two SPMD helpers also consume custom-calls: `TpuCustomCallShardingHelper` (implements `xla::CustomCallShardingHelper`: `InferShardingFromOperands` `0x1278bf80`, `PropagateUserSharding`, `IsCustomCallShardable`, `CanPropagateShardingToOperands`) and `TpuLogCustomCallPartitioner` (replicated-only `xla-log` sharding). See [Auto-Sharding & SPMD](auto-sharding-spmd.md).

---

## The Mosaic Import Seam

`tpu_custom_call` is the sole target taking action (D), and it is the principal use of the entire custom-call mechanism: it is how Pallas/Mosaic kernels — authored as `tpu`-dialect MLIR **outside** `libtpu` by the JAX frontend — enter the compiler. The `tpu` dialect is never *produced* by lowering general MHLO; it is only ever **imported** through this seam. (The downstream `tpu`→LLO pipeline is documented in [MHLO → XTile → tpu Lowering](mhlo-xtile-tpu-lowering.md) and [Mosaic Overview](mosaic-overview.md); this section covers only the custom-call entry and caching.)

A `tpu_custom_call` instruction carries:

- `custom_call_target` = `"tpu_custom_call"`
- `backend_config` = serialized `xla.jellyfish.BackendConfig` whose `custom_call_config` (`xla.jellyfish.CustomCallConfig`) holds the kernel.

The `CustomCallConfig` proto is confirmed present (`CustomCallConfig::{ByteSizeLong,Clear,CopyFrom,GetClassData}`, arena ctors, and the repeated nested submessages `CustomCallConfig_InputMemorySpaceColor` / `CustomCallConfig_OutputMemorySpaceColor` with their `RepeatedPtrFieldBase::Add` / `Arena::CopyConstruct` instantiations). Field set (names recovered; tag numbers not extracted — see [Confidence Summary](#confidence-summary)):

```protobuf
message CustomCallConfig {
  bytes  mlir_module;          // serialized tpu-dialect module (bytecode or textual)
  string host_mlir_module;     // host computation used for shape inference; "" if shapes static
  string kernel_name;          // recovered from the ",kernel_name=" descriptor fragment
  repeated InputMemorySpaceColor  input_memory_space_colors;   // per operand
  repeated OutputMemorySpaceColor output_memory_colors;        // per result (accessor confirmed; tuple-arity must match)
  bool   has_communication;    // survives async-fusion as a comm boundary if true
  int64  collective_id;        // required for tpu.get_barrier_semaphore in the body
  CustomCallCost cost_estimate;        // {flops, transcendentals, bytes_accessed} — proto type confirmed
}
```

> The `input_memory_space_colors` / `output_memory_colors` field-name pair is recovered from descriptor strings and `custom_call_config.output_memory_colors()` accessor call sites; the asymmetric naming (`_space_` on input only) is as observed in the binary, not a transcription artifact. The submessage types are `CustomCallConfig_InputMemorySpaceColor` / `CustomCallConfig_OutputMemorySpaceColor`. A standalone `metadata` field is **not** confirmed; metadata is attached via `SetCustomCallMetadata` on the instruction rather than a named proto field — that earlier claim is removed.

### Pipeline: HLO entry → cached body → LLO

1. **HLO entry.** The frontend emits one `kCustomCall("tpu_custom_call")` per `pl.kernel`, with `custom_call_config.mlir_module` filled.
2. **`MosaicFusion`** (`RunImpl` `0x10f12500`, wrapped in `HloPassFix`) lifts surrounding HLO sub-graphs into Mosaic-eligible kernels, to fixed point.
3. **`TpuCustomCallLegalizer::RunImpl`** (`0x11036080`) classifies each call (TensorCore vs SparseCore vs Megachip) via `ConfigureSparseCoreConfig` (`0x110355e0`) / `ConfigureMegachipParallelism` (`0x11035a20`); SparseCore kernels are offloaded in-place via `OffloadOneSparseCoreCustomCall` (`0x11035700`). See [Lower to SparseCore LLVM](lower-to-sparsecore-llvm.md).
4. **`TpuCustomCallMemorySpacePolicy::RunImpl`** (`0x110364a0`) assigns `input_memory_space_colors`/`output_memory_colors` via `RunHbmPolicy` (`0x11038120`) or `RunMsaReservationPolicy` (`0x110367c0`), driven by the `--xla_tpu_tpu_custom_call_memory_space_spec` proto. See [MSA Reservation / HBM Policy](msa-reservation-hbm-policy.md).
5. **`TpuCustomCallScopedVmemAdjuster::RunImpl`** (`0x1104de40`) runs a trial `BufferAssignment` (via the `absl::AnyInvocable<StatusOr<…BufferAssignment…>(HloModule*, Target const&)>` captured at construction) and rewrites the scoped-VMEM byte count to the actual requirement.
6. **HLO→LLO lowering** via `CustomCallEmitter::Emit` (`0x111ef740`):
   - `GetCustomCallAndConfig(HloInstruction const*)` (`0x111fe020`) extracts the `(HloCustomCallInstruction*, CustomCallConfig)` pair.
   - `GetCachedCustomCallBody(HloCustomCallInstruction const*, CustomCallConfig const&)` (`0x13e31860`) returns a `MosaicMlirCacheEntry`, parsing `config.mlir_module()` via `GetMlirModule(CustomCallConfig const&, MLIRContext&, bool)` (`0x13e31220`) → `ParseMlirModuleString` (`0x0f908580`) on a cache miss. The entry is stored on the `HloModule` (`HloModule::SetCacheEntry<MosaicMlirCacheEntry>`) keyed by `tsl::Fprint128`, so identical kernels parse once.
   - `SetupAsyncCollectiveLowering` (`0x111f7520`) runs when `has_communication=true`, fusing the call into an async collective.
   - `GetDeviceAssignment` (`0x111f4ee0`) resolves the device list from the parent computation.
7. **`MosaicMlirCacheEntry` introspection** — the cached entry exposes `HasAnyCoreType(Span<mlir::tpu::CoreType const>)`, `EmitsSdcChecksums`, `IsSparseCoreKernel`, `RequiresMxuAssigner`, `GetCacheKey` (`tsl::Fprint128`). These feed the dynamic `RegisterCompilationProperties` callback for `tpu_custom_call`.
8. The imported `tpu` module then runs the standard `tpu`-dialect pipeline (serde version upgrade, layout inference, `createLowerToLLOPass`) — documented in [Mosaic Overview](mosaic-overview.md).

The `MosaicMlirCacheEntry` constructor params are recovered exactly: `(std::string&, std::unique_ptr<mlir::MLIRContext>, mlir::OwningOpRef<mlir::ModuleOp>, MosaicMlirCacheEntry*)` — the parsed module plus its owning context, threaded into the cache.

> **GOTCHA — the cache key is the kernel bytecode, not the HLO instruction.** Because the key is a `tsl::Fprint128` over the MLIR body, two distinct `tpu_custom_call` instructions sharing a kernel collapse to one parse and one compiled body. A reimplementation that keys on the HLO instruction pointer instead will re-parse identical Pallas kernels and miss the dedup.

---

## Validation Layers and Error Paths

Three layers validate a custom-call, in pipeline order:

1. **`HloVerifier`** — generic shape/operand-count invariants for `kCustomCall` (operand count vs `operand_shapes_with_layout.size()`, layout presence). Runs after every pre-pass.
2. **`xla::TpuHloSupportChecker`** — the TPU acceptance test. Rejects any target **not** in the registry; this is where `__cudnn$convForward`, `__triton$…`, and other `$`-prefixed cross-backend escapes die.
3. **`TpuCustomCallLegalizer::RunImpl`** — semantic legalization (memory-space coloring sanity, SparseCore offload feasibility, megachip parallelism).

Representative recovered status/DCHECK strings (verbatim in `.rodata` / decompiled output), by category:

| Category | Anchored string(s) |
|---|---|
| Unknown target | `Attempting to lower unimplemented custom call %s` · `Custom call target %s is not implemented.` · `Unimplemented custom-call: %s` |
| Reserved name | `Invalid custom_call_target "%s": Call targets that start with '$' are reserved for internal use.` |
| Wrong arity | `Custom calls with target %s must have exactly one operand. %s has %d.` · `%s custom call must have no operands.` · `%s custom call must have an S32 scalar shape.` |
| Mosaic body | `Failed to parse custom call kernel: …` · `Custom call does not have a custom_call_config field.` · `Cannot lower tpu.get_barrier_semaphore op because a barrier config was not provided. Perhaps, collective_id was not set in the TPU custom call.` · `Custom call body is empty.` |
| Pallas output | `Pallas custom calls must have array or tuple output. Found: …` · `Pallas custom calls with tuple outputs must have exactly one output memory space color per tuple element.` |
| Scoped VMEM | `Failed to determine the scoped vmem requirement for one or more custom calls in module …` · `… is an unsupported memory space in TpuCustomCallScopedVmemAdjuster. …` |
| Pin / Unpin | `Pin custom_call should have a memref output` · `Unpin custom_call should have a tensor output` · `custom-call to Pin must have one output-to-operand aliasing` |
| AllocateBuffer | `CreateBuffer custom_call should have a memref result` · `custom-call to CreateBuffer can't have an operand` |
| SDY round-trip | `expected CustomCallOp with xla.sdy target name.` · `xla.sdy.ShardingGroup CustomCallOp should have no uses.` · `An SPMDShardToFullShape custom call found without a sharding annotation.` |
| ApproxTopK | `ApproxTopK takes an even number of operands.` · `ApproxTopK takes exactly 1 called_computation.` |
| Host execute | `Host execute custom call must have exactly one called computation.` |
| MegaScale | `MXLA custom call does not support collectives on multiple channels.` · `Host offloaded collective custom call '$0' only works in multi slice environment.` |
| Backend-config parse | `Unable to parse backend config for custom call: …` · `Failed to parse WindowPrefetch backend config.` · `Failed to register custom call partitioner for …` |

---

## Compile-Time Flags

| Flag (`FLAGS_xla_tpu_…`) | Effect |
|---|---|
| `tpu_custom_call_memory_space_spec` | serialized `TpuCustomCallMemorySpaceSpec` proto driving step 4 |
| `enable_tpu_custom_call_scoped_vmem_adjustments` | gates `TpuCustomCallScopedVmemAdjuster` |
| `custom_call_nop_return_token_vdelay` | delay cycles for nop-return-token calls |
| `mosaic_fusion` | gates the `MosaicFusion` pre-pass |
| `enable_mosaic_emitters` | gates the Mosaic emitter registry |
| `enable_async_collective_fusion_with_mosaic_custom_call` | gates `SetupAsyncCollectiveLowering` |
| `sdc_checker_{checksum,enable,timestamp,zero}_custom_call*` | SDC instrumentation of `tpu_custom_call` |
| `layer_scheduler_min_custom_call_flops` | scheduler FLOPs threshold |
| `llo_race_analysis_analyze_tpu_custom_calls` | LLO race analysis over custom-call bodies |

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Five-facet registry with the listed `Register*` entry points/signatures | `FunctionRegistry<std::string, CompilationProperties(HloInstruction const*)>::Register` + `RegisterCanFuse`/`RegisterSpmdPartitioningVisitor` demangled signatures; `Register*` source-quotes in `.rodata` | HIGH |
| `LoweringEmitter` callback signature incl. `BackendConfigMap = flat_hash_map<HloInstruction const*, unique_ptr<BackendConfig>>` | recovered from `__policy_func` thunk + verbatim lambda bodies | HIGH |
| `CompilationProperties` six-field struct + per-target values | designated-initializer source-quotes (`kSharding`, `WindowPrefetch`, SDC keys, …) | HIGH |
| ~52-target catalog with handler constants | `k…` rodata anchors + per-handler `google_init_module_*` symbols; each verbatim string confirmed present in the binary | HIGH |
| Mosaic import seam (`GetCachedCustomCallBody`/`GetMlirModule`/`MosaicMlirCacheEntry`/`CustomCallEmitter::Emit`) | all symbols present at listed addresses; `Fprint128`-keyed `HloModule::{Set,Get}CacheEntry<MosaicMlirCacheEntry>`; ctor param list recovered | HIGH |
| Three legalization passes + signatures | `TpuCustomCallLegalizer`/`MemorySpacePolicy`/`ScopedVmemAdjuster` `RunImpl` and helper signatures demangled; ctor `AnyInvocable<…BufferAssignment…>` recovered | HIGH |
| Error/DCHECK strings | reserved-`$` and unimplemented strings confirmed in decompiled output; remainder from `.rodata` quoted source | HIGH |
| `CustomCallConfig` proto **field tag numbers** | descriptor present (submessage names, accessors) but tag numbers not extracted from FileDescriptorProto | LOW |
| Per-handler emit-helper LLO bodies (exact op sequences) | handler→target mapping HIGH; the `OpEmitter` subclass LLO rewrites not decompiled per-op | LOW |
| `MosaicMlirCacheEntry::GetCacheKey` field composition | returns `tsl::Fprint128` over the body; whether `kernel_name`/`cost_estimate` participate not recovered | LOW |
| `xla.sdy.GlobalToLocalShape` per-axis reshape protocol | target-name constants HIGH; flatten/pack/relabel semantics not disassembled | LOW |

---

## Cross-References

- [The TPU Compiler (overview)](overview.md) — the five-phase spine; custom-call dispatch sits in the HLO→LLO lowering loop of the device path.
- [Compile Phases 0–3](compile-phases.md) — where the (A) pre-pass expanders and the (B) marker-strip passes run relative to the HLO→LLO emit loop.
- [HLO Pre-Passes](hlo-pre-passes.md) — the expander family (`TpuCholeskyExpander`, `TpuRngBitGeneratorExpander`, `MosaicFusion`, …) that consume action-(A) targets before the registry sees them.
- [MHLO → XTile → tpu Lowering](mhlo-xtile-tpu-lowering.md) — the seam that proves the `tpu` dialect is *imported* via `tpu_custom_call`, never produced by lowering MHLO; `GetMlirModuleOpFromCustomCall` / `RunMLIRPasses`.
- [Mosaic Overview](mosaic-overview.md) — the imported-kernel `tpu`-dialect pipeline (serde, layout inference, lower-to-LLO) that runs after the (D) cached-body extraction.
- [Lower to SparseCore LLVM](lower-to-sparsecore-llvm.md) — destination of `TpuCustomCallLegalizer`'s SparseCore offload (`OffloadOneSparseCoreCustomCall`).
- [The tpu MLIR Dialect](tpu-dialect-and-ops.md) — the dialect authored upstream and inlined by `CustomCallEmitter`.
- [Sharding Propagation](sharding-propagation.md) — consumer of the (B) sharding markers and the SDY round-trip targets.
- [Auto-Sharding & SPMD](auto-sharding-spmd.md) — the SpmdPartitioner that consumes action-(F) targets via `RegisterSpmdPartitioningVisitor` and `TpuCustomCallShardingHelper`.
- [MSA Reservation / HBM Policy](msa-reservation-hbm-policy.md) — the memory-space coloring policies invoked by `TpuCustomCallMemorySpacePolicy`.
- [Ragged-Dot & Convolution Lowering](raggeddot-convolution.md) — related jellyfish emitters for ops adjacent to the custom-call linalg blocks.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / Front-end and pipeline — [back to index](../index.md)
