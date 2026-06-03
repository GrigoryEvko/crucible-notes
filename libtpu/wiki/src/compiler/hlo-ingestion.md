# HLO Ingestion

> *Addresses, build-id, and symbol names apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ; treat every VA as version-pinned.*

## Abstract

Every TPU program enters the compiler as **portable MLIR bytecode** — a StableHLO module (with CHLO and VHLO ops mixed in) that the framework bridge (JAX, the TF/XLA bridge, or PyTorch/XLA) serialized across the PjRt boundary. It does **not** enter as XLA HLO. The compiler's first act, `xla::CompilePhase0StablehloToHlo` (`0xf84de60`), is therefore not an optimization but a **format crossing**: it parses the bytecode into an in-memory `mlir::ModuleOp`, runs an ordered MLIR pass pipeline that legalizes CHLO→StableHLO→MHLO, then walks the MHLO module emitting an `xla::HloProto`, and finally parses that proto back into the `xla::HloModule`/`HloInstruction` graph the rest of XLA was written against. This page owns that crossing and the wire format on both sides of it.

The reader who knows LLVM should hold this analogy: Phase 0 is the *front-end parser plus the bitcode reader*, not a transform pass. There are three distinct representations in play, each with its own serialization. On the way in: StableHLO/CHLO/VHLO MLIR (the stable, versioned wire IR — the equivalent of LLVM bitcode with forward-compatibility guarantees). In the middle: the `xla.HloModuleProto` (the *flat, id-indexed* protobuf form of an HLO graph — XLA's own serialization, distinct from the MLIR bytecode). At the output: the live `HloModule` object graph. Phase 0 converts the first into the second (via an MLIR `PassManager` and `mlir::ConvertMlirHloToHlo`, `0x16a64920`), then deserializes the second into the third (via `xla::HloModule::CreateFromProto`, `0x1e5dbe60`). Only after that does the HLO optimizer (Phase 1, [compile-phases.md](compile-phases.md)) begin.

This page documents three things and links the rest. (1) The **StableHLO→HLO conversion** — the MLIR pass pipeline `xla::MlirToXlaComputation` (`0xf907d40`) builds and runs, the per-op `StablehloToHloOpConverter` patterns, and the CHLO/VHLO handling. (2) The **HLO proto schema** the front-end hands in — the `HloModuleProto`/`HloComputationProto`/`HloInstructionProto` id-graph, reconstructed from the binary's `protodesc_cold` descriptor pool. (3) The **compile entry** — how PjRt's phase-compile and the TF/XLA bridge's `CompileComputationToHlo` reach Phase 0. The *enumerated* HLO pass pipeline that runs *after* ingestion is on [compile-phases.md](compile-phases.md) and [hlo-pre-passes.md](hlo-pre-passes.md); the IR-layer stack overview is on [overview.md](overview.md).

For reimplementation, the ingestion contract is:

- **Three serializations, two conversions.** StableHLO/CHLO/VHLO bytecode → (legalize + `ConvertMlirHloToHlo`) → `HloModuleProto` → (`CreateFromProto`) → `HloModule`. A reimplementer who treats ingestion as one step will miss that the proto is a real intermediate the runtime can dump and cache.
- **The opcode is a string, not an enum.** On the HLO-proto wire, `HloInstructionProto.opcode` is `string opcode = 2`. There is no `xla.HloOpcode` proto enum. This is what makes the format forward-compatible.
- **`HloInstructionProto` is one ~83-field union.** Every op-specific attribute is an optional field on a single wide message; the opcode string selects which subset is meaningful.
- **The graph is a flat id-indexed DAG.** No nested instruction objects: data edges are `operand_ids` int64 references, call edges are `called_computation_ids` references, `root_id` names each computation's output.
- **CHLO and the StableHLO↔MHLO legalizers run inside Phase 0, before HLO exists.** A reimplementer who builds CHLO handlers into the HLO optimizer is at the wrong layer; CHLO is gone by the time Phase 1 sees the module.

| | |
|---|---|
| **Phase 0 entry** | `xla::CompilePhase0StablehloToHlo` @ `0xf84de60` |
| **Phase 0 I/O** | `(CompileOptions, absl::Span<const PjRtPartialProgramProto>, const PjRtTopologyDescription&)` → `StatusOr<vector<PjRtPartialProgramProto>>` |
| **Bytecode parse** | `xla::ParseMlirModuleString(string_view, mlir::MLIRContext&)` @ `0xf908580` |
| **Conversion driver** | `xla::MlirToXlaComputation` @ `0xf907d40`; `xla::ConvertStablehloToHlo(mlir::ModuleOp)` @ `0x16a3d200` |
| **MHLO → HloProto emit** | `mlir::ConvertMlirHloToHlo(ModuleOp, HloProto*, …)` @ `0x16a64920` |
| **Per-op converters** | `mlir::stablehlo::(anon)::StablehloToHloOpConverter<Op>` — 121 `matchAndRewrite` specializations |
| **CHLO legalizers** | `createChloLegalizeToStablehloPass`, `mlir::mhlo::createChloLegalizeToHighLevelMhloPass` (opts `getDefaultChloToHighLevelMhloOptions` @ `0x16ad78e0`) |
| **HLO proto parse** | `xla::HloModule::CreateFromProto(HloModuleProto const&, HloModuleConfig const&, …)` @ `0x1e5dbe60`; `CreateModuleConfigFromProto` @ `0x1e5e0480` |
| **Phase registry** | `xla::TpuCompiler::RegisterAllPhases` @ `0xf849ec0` |
| **HLO proto schema** | `protodesc_cold` (VA `0x0be8af30`): `hlo.proto` @ `0xc189a60`, `xla_data.proto` @ `0xc1b7e20`, `xla.proto` @ `0xc021470` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Compile Entry

### Purpose

Phase 0 is the head of the five-phase separate-compilation pipeline registered by `xla::TpuCompiler::RegisterAllPhases` (`0xf849ec0`). It is reached from two front-end surfaces, both of which hand in StableHLO MLIR, never raw HLO. A reimplementer must understand which surface produced the module because the surface dictates what is *already* in the bytecode (sharding dialect, dim-args, layout-mode attributes).

### Entry Point

```text
PjRt phase-compile  (PJRT_Api extension type 9 — see ../pjrt/ext-compile-phasecompile.md)
  │  serialized StableHLO module + CompileOptions  →  PjRtPartialProgramProto[]
  ▼
xla::CompilePhase0StablehloToHlo                                        0xf84de60
  ├─ xla::ParseMlirModuleString(view, MLIRContext&)                     0xf908580   parse bytecode → ModuleOp
  ├─ {GetArgLayoutModes / GetOutputLayoutModes}                                     read kArg/kOutLayoutModesAttr
  ├─ {GetArgMemoryKinds / GetOutputMemoryKinds}                                     read kArg/kOutMemorySpacesAttr
  ├─ xla::MlirToXlaComputation(ModuleOp, XlaComputation&, …, ChloOpts) 0xf907d40   the conversion (below)
  └─ → HloModuleProto (inside XlaComputation)  →  PjRtPartialProgramProto out

TF/XLA bridge alternative entry:
tensorflow::tpu::CompileComputationToHlo(                              0xf7cdba0
    TpuTopology, variant<MlirToHloArgs, FunctionToHloArgs>,
    CompileOnlyClient*, …) → XlaCompilationResult
  └─ MlirToHloArgs path also funnels through the StableHLO→HLO conversion
```

Two facts about the signature matter. First, Phase 0 takes a `Span<const PjRtPartialProgramProto>` and returns a vector of the same — the *partial-program* protocol that lets the runtime persist the post-ingestion module and resume at Phase 1 later (the basis of compilation caching; see [compile-phases.md](compile-phases.md)). Second, the `PjRtTopologyDescription&` argument means the topology (chip generation, mesh shape) is available *during* ingestion — relevant because layout-mode and memory-space attribute resolution (below) can be topology-aware.

> **NOTE — the bridge path and the PjRt path converge.** `tensorflow::tpu::CompileComputationToHlo` (`0xf7cdba0`) accepts a `variant<MlirToHloArgs, FunctionToHloArgs>`. The `MlirToHloArgs` arm carries a StableHLO module and routes through the same MLIR→HLO conversion machinery as the PjRt phase-compile path; the `FunctionToHloArgs` arm is the legacy TF GraphDef→XLA path. A reimplementation targeting modern JAX/PjRt only needs the StableHLO arm. [Confidence: CONFIRMED both arms exist; the FunctionToHloArgs internals were not traced — LOW on that legacy path.]

### The XLACallModule wrapper

The serialized module carried across the boundary is the payload of an **`XLACallModule`** op when it originates from JAX native serialization in a TensorFlow context. The op's documentation string (at `0x1898480`) records the versioning contract: minimum supported `version` is 2; from v2 the op carries StableHLO text *or* bytecode; v3 adds the `platforms` attribute; v4 adds StableHLO compatibility guarantees; v5 allows `stablehlo.custom_call`. This versioning is why VHLO (versioned HLO) ops appear in the import surface — they are the mechanism by which an older runtime can ingest a module a newer front-end produced.

---

## The StableHLO → HLO Conversion

### Purpose

This is the core of Phase 0: turn a `mlir::ModuleOp` holding StableHLO/CHLO/VHLO into an `xla::HloProto`. It is implemented as a conventional MLIR `PassManager` run followed by a single MHLO-walking emitter. Two related drivers exist — `xla::MlirToXlaComputation` (`0xf907d40`, the Phase-0 path, producing an `XlaComputation`) and `xla::(anonymous namespace)::ConvertStablehloToHloProtoInternal` (`0x16a3d400`, producing a bare `HloProto`). They share the *shape* of the pipeline (CHLO recompose → SymbolDCE → CHLO legalize → normalize → run → emit) but **do not share the exact pass set**: `MlirToXlaComputation` adds `StablehloComplexMathExpander` and runs the verifier at its default; `ConvertStablehloToHloProtoInternal` instead adds (conditionally) `StablehloTargetIndependentOptimization` + `StablehloSanitizeDiscardableAttributes` and explicitly calls `enableVerifier(false)`. Both end by walking the normalized module into a proto — `MlirToXlaComputation` routes through `ConvertStablehloToHloWithOptions` → `ConvertStablehloToHloInternal` (`0x16a3d220`) → `ConvertStablehloToHloProtoInternal`, so the proto emitter is shared even though the front pass chain differs. [Confidence: CONFIRMED both pipelines from the decompiled bodies.]

### Entry Point

```text
xla::MlirToXlaComputation(ModuleOp, XlaComputation&, bool, bool,        0xf907d40
                          ExecutableBuildOptions*,
                          mhlo::ChloLegalizeToHighLevelMhloPassOptions const&)
  └─ public wrappers:
       xla::ConvertStablehloToHlo(ModuleOp)                             0x16a3d200
       xla::ConvertStablehloToHloWithOptions(ModuleOp, bool, bool)      0x16a3d3a0
       xla::ConvertStablehloToHloProto(ModuleOp, HloProto*)             0x16a3d3c0
       xla::ConvertStablehloWithManyArgsToHloProto(…)                   0x16a3d7c0
```

### Algorithm

The conversion builds one `mlir::PassManager`, adds an ordered chain of MLIR passes (most nested under `func.func`), runs it, and then emits the proto. The pass chain below is recovered from the call targets in the decompiled body of `MlirToXlaComputation` (`0xf907d40`); the `ConvertStablehloToHloProtoInternal` (`0x16a3d400`) variant differs as noted under Purpose, above.

```c
function MlirToXlaComputation(module, out_computation, chlo_opts):   // 0xf907d40
    pm = PassManager(module.getContext(), "any")                     // mlir::PassManager (verifier left at default)

    // --- 0. Shardy fallback (only when GSPMD attrs/ops coexist with Shardy) ---
    if module has GSPMD attrs but Shardy is enabled:
        ExportShardyForGSPMD(module)                                 // disable Shardy, fall back to GSPMD propagation

    // --- 1. CHLO recompose, then SymbolDCE, then CHLO legalize ---
    pm.nest("func.func").addPass(stablehlo_ext::createChloRecomposeOpsPass())  // rebuild fused CHLO ops
    pm.addPass(createSymbolDCEPass())                                // drop unreferenced symbols (module-level)
    pm.nest("func.func").addPass(
        mhlo::createChloLegalizeToHighLevelMhloPass(chlo_opts))      // CHLO → high-level MHLO (top_k, erf, ragged…)
    pm.nest("func.func").addPass(
        stablehlo::createChloLegalizeToStablehloPass())              // remaining CHLO → StableHLO primitives

    // --- 2. StableHLO normalization ---
    pm.nest("func.func").addPass(
        stablehlo::createStablehloComplexMathExpanderPass())         // expand complex arithmetic
    pm.nest("func.func").addPass(
        stablehlo_ext::createSinkConstantsToControlFlowPass())       // push consts into while/case regions

    status = pm.run(module)                                          // BaseScopedDiagnosticHandler captures errors
    if !status.ok(): return status                                  // module now lives in MHLO + builtin dialects

    // --- 3. StableHLO → HloProto via the shared emitter (wraps ConvertMlirHloToHlo, 0x16a64920) ---
    hlo_proto = ConvertStablehloToHloWithOptions(module, …)          // → ConvertStablehloToHloProtoInternal → ConvertMlirHloToHlo
    out_computation = XlaComputation(hlo_proto.hlo_module())          // wrap proto in XlaComputation
    return out_computation
```

Two structural notes. The legalization is *staged top-down*: CHLO (the highest-level dialect, e.g. `chlo.top_k`, `chlo.erf`, `chlo.ragged_dot`) is recomposed and lowered first, partly into high-level MHLO ops (which have direct HLO equivalents) and partly into StableHLO primitives; then the StableHLO layer is normalized; then the whole thing is walked into proto. The `ConvertMlirHloToHlo` walk (reached through `ConvertStablehloToHloProtoInternal`) is where the actual MHLO-op → `HloInstructionProto` mapping happens — this is the boundary at which the program leaves MLIR and becomes an XLA HLO proto.

> **GOTCHA — verifier policy differs between the two drivers, and is *not* "on after every pass" in the proto path.** The proto-emitting driver `ConvertStablehloToHloProtoInternal` (`0x16a3d400`) explicitly calls `pm.enableVerifier(false)` — it does *not* re-verify between passes. `MlirToXlaComputation` (`0xf907d40`) constructs its `PassManager` without an explicit `enableVerifier` call (it inherits the MLIR default). Both drivers construct a `mlir::BaseScopedDiagnosticHandler`, which is what turns an MLIR diagnostic raised during `pm.run` into an `absl::Status` (via `ConsumeStatus`). A reimplementer should not assume per-pass verification is enabled on the ingestion path; the diagnostic handler — not the verifier — is the mechanism that surfaces a malformed module as a clean error.

### The per-op converter table

The StableHLO→MHLO op mapping is implemented by the templated pattern `mlir::stablehlo::(anonymous namespace)::StablehloToHloOpConverter<Op>`, one specialization per StableHLO op. 121 distinct `matchAndRewrite` specializations are present in the binary. Rather than dump all 121, the table describes the conversion *axes* — what the converter must do for each op category.

| Op category | Representative ops (verified specializations) | Conversion action |
|---|---|---|
| Elementwise unary | `AbsOp`, `CeilOp`, `CbrtOp`, `ClzOp`, `ConvertOp`, `CosineOp` | 1:1 to the MHLO/HLO op; copy result type |
| Elementwise binary | `AddOp`, `AndOp`, `DivOp`, `CompareOp`, `ComplexOp` | 1:1; `CompareOp` carries `comparison_direction`/`comparison_type` |
| Shape / data movement | `BroadcastOp`, `BroadcastInDimOp`, `ConcatenateOp`, `DynamicBroadcastInDimOp` | map `broadcast_dimensions` → `dimensions`; dynamic forms carry an extra shape operand |
| Reductions / windowed | `BatchNormTrainingOp`, `BatchNormInferenceOp`, `BatchNormGradOp` | carry `epsilon`, `feature_index`; expander runs later in HLO pre-passes |
| Matmul / conv | `DotOp`, `DotGeneralOp`, `ConvolutionOp`, `DynamicConvOp`, `CholeskyOp` | map dimension-number attrs → `dot_dimension_numbers` / `convolution_dimension_numbers`; carry `precision_config` |
| Control flow | `CaseOp`, `AsyncStartOp`, `AsyncDoneOp` | map region-bearing ops → `called_computation_ids` edges |
| Collectives | `AllGatherOp`, `AllReduceOp`, `AllToAllOp`, `CollectiveBroadcastOp`, `CollectivePermuteOp`, `CrossReplicaSumOp` | carry `channel_id`, replica grouping; `use_global_device_ids` |
| Gather / dynamic | `DynamicGatherOp`, `CreateTokenOp`, `AfterAllOp` | gather dim-numbers; token-typed ops produce `TOKEN` shapes |
| Custom | `CustomCallOp`, `CompositeOp` | preserve `custom_call_target`, `backend_config`, `api_version`; `CompositeOp` lowers to a decomposition call |

> **QUIRK — the 121 converter specializations are a *subset* of the ~182 StableHLO ops, because many StableHLO ops are identical to their MHLO counterpart and need no rewriter.** A reimplementation that builds a converter for every StableHLO op will write redundant identity rewriters; one that builds only the 121 and assumes the rest pass through unchanged is closer to libtpu's actual structure. The ops that *need* a converter are those whose attribute layout, region structure, or type semantics differ between the StableHLO and MHLO ODS definitions. [Confidence: CONFIRMED 121 specializations; the exact StableHLO/MHLO divergence per op was not individually audited — HIGH on the category mapping.]

### Related Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `xla::CompilePhase0StablehloToHlo` | `0xf84de60` | phase entry; parse + convert + repackage as partial program | CONFIRMED |
| `xla::ParseMlirModuleString` | `0xf908580` | StableHLO text/bytecode → `mlir::ModuleOp` | CONFIRMED |
| `xla::MlirToXlaComputation` | `0xf907d40` | the conversion driver (PassManager + emit) | CONFIRMED |
| `xla::ConvertStablehloToHlo` | `0x16a3d200` | thin wrapper, default options | CONFIRMED |
| `xla::ConvertStablehloToHloWithOptions` | `0x16a3d3a0` | wrapper exposing the two bool flags; tail-calls `ConvertStablehloToHloInternal` | CONFIRMED |
| `xla::(anon)::ConvertStablehloToHloInternal` | `0x16a3d220` | wraps `ConvertStablehloToHloProtoInternal`, returns `XlaComputation` | CONFIRMED |
| `xla::(anon)::ConvertStablehloToHloProtoInternal` | `0x16a3d400` | the real pass-pipeline + `ConvertMlirHloToHlo` emit (verifier disabled) | CONFIRMED |
| `xla::ConvertStablehloWithManyArgsToHloProto` | `0x16a3d7c0` | multi-argument-bundle variant | CONFIRMED |
| `mlir::ConvertMlirHloToHlo` | `0x16a64920` | MHLO module walk → `HloProto` | CONFIRMED |
| `mlir::mhlo::getDefaultChloToHighLevelMhloOptions` | `0x16ad78e0` | default CHLO-legalization options | CONFIRMED |
| `mlir::mhlo::StablehloLegalizeToHloPass::runOnOperation` | `0x16ae0320` | StableHLO→HLO pass (standalone) | CONFIRMED |
| `mlir::mhlo::ChloLegalizeToHloPass::runOnOperation` | `0x16adbd00` | CHLO→HLO pass (standalone) | CONFIRMED |

> **NOTE — `StablehloLegalizeToHloPass` (`0x16ae0320`) and the inline converter pipeline coexist.** The standalone `mlir::mhlo::StablehloLegalizeToHloPass` and `ChloLegalizeToHloPass` are registered passes (their full `…PassBase` vtables — `getName`, `getArgument`, `clonePass`, `getDependentDialects` — are present). The `MlirToXlaComputation` driver does *not* invoke them by name; it assembles its own `createChlo…` / `createStablehlo…` pass chain. Both routes produce the same legalization. The standalone passes exist for the reverse and round-trip paths (`HloLegalizeToStablehloPass`, `0x16adcea0`, runs at the *end* of the HLO pipeline to re-emit StableHLO for the MLIR descent — see [compile-phases.md](compile-phases.md)). A reimplementer should treat the inline chain as authoritative for ingestion.

---

## The HLO Proto Schema (the Wire Contract)

### Purpose

`ConvertMlirHloToHlo` emits an `xla.HloModuleProto`. This is the **stable serialization** of an HLO program — what `HloModule::ToProto()` produces, what `HloModule::CreateFromProto` parses, what `xla_dump_hlo_as_proto` writes, and the format in which the front-end's program is actually represented at the Phase-0/Phase-1 boundary. The schema is reconstructed field-by-field from the `protodesc_cold` descriptor pool embedded in the binary (section VA `0x0be8af30`, size `0x334180`); the three FileDescriptorProto records are `hlo.proto` (`0xc189a60`), `xla_data.proto` (`0xc1b7e20`), and `xla.proto` (`0xc021470`).

### The graph spine

```text
HloModuleProto
  ├─ string device_type = 21                 // "tpu"
  ├─ repeated HloComputationProto computations = 3
  │     ├─ string name = 1
  │     ├─ repeated HloInstructionProto instructions = 2   // FLAT list
  │     ├─ int64 id = 5
  │     └─ int64 root_id = 6                  // names the output instruction
  ├─ int64 entry_computation_id = 6
  ├─ ProgramShapeProto host_program_shape = 4 // entry signature
  ├─ HloScheduleProto schedule = 7            // per-computation id ordering
  ├─ HloInputOutputAliasProto input_output_alias = 8
  ├─ repeated bytes payloads = 22             // interned backend-config side-channel
  ├─ bool is_dynamic = 11                     // module has dynamic shapes
  ├─ OpSharding spmd_output_sharding = 12 / spmd_parameters_shardings = 14
  ├─ StackFrameIndexProto stack_frame_index = 17   // interned source provenance
  └─ FrontendAttributes frontend_attributes = 19
```

The program graph is a **flat instruction list with id edges**: there are no nested instruction objects. Every data edge is an int64 `operand_ids` (field 36) reference into the sibling instruction list; every call edge is an int64 `called_computation_ids` (field 38) reference into the module's computation list; `id` (field 35) is unique within a computation and `root_id` (field 6) names the output. This id-graph representation is why the proto survives serialization without pointer fixups — it is a DAG-by-index, not a tree.

### The universal instruction record

`HloInstructionProto` is a single message with ~83 declared fields running to field number 99 (parsed from the descriptor in `protodesc_cold`). Every op-specific attribute is its own optional field; the `opcode` string selects which subset is meaningful. The table below describes the *axes* of this union (the full field list is too wide to dump; these are the dimensions a reimplementer must reproduce).

| Field group | Representative fields (number) | Read by opcode(s) |
|---|---|---|
| Identity / edges | `name`(1), `opcode`(2), `shape`(3), `id`(35), `operand_ids`(36), `control_predecessor_ids`(37), `called_computation_ids`(38) | all |
| Leaf payloads | `literal`(8), `parameter_number`(9), `delta`(66), `distribution`(23), `rng_algorithm`(70) | constant, parameter, iota, rng, rng-bit-generator |
| Shape ops | `dimensions`(14), `slice_dimensions`(17), `dynamic_slice_sizes`(20), `padding_config`(21), `is_reverse`(94) | reshape, transpose, slice, pad, reverse, … |
| Matmul / conv | `dot_dimension_numbers`(30), `ragged_dot_dimension_numbers`(90), `convolution_dimension_numbers`(16), `window`(15), `feature_group_count`(50), `precision_config`(51), `conv_kind`(97) | dot, ragged-dot, convolution |
| Collectives | `channel_id`(26), `replica_groups`(49), oneof {`collective_device_list`(87), `iota_collective_device_list`(92), `mesh_axes_replica_group_list`(93)}, `use_global_device_ids`(71), `source_target_pairs`(52) | all-reduce, all-gather, all-to-all, collective-permute, … |
| Custom-call | `custom_call_target`(28), `backend_config`(43), `backend_config_payload`(99), `custom_call_api_version`(77), `output_operand_aliasing`(74) | custom-call (incl. `tpu_custom_call`) |
| Precision control | `result_accuracy`(91), `is_associative`(96), `exponent_bits`(18), `mantissa_bits`(19) | transcendentals, reduce-precision |
| Sharding | `sharding`(40), `domain_entry_sharding`(54), `domain_exit_sharding`(55) | any sharded op, domain |
| Provenance | `metadata`(7), `original_value`(88), `frontend_attributes`(68) | all |

> **QUIRK — `HloInstructionProto.opcode` is a string (`string opcode = 2`), not a proto enum.** An exhaustive scan of the entire `protodesc_cold` descriptor pool (≈770 embedded `.proto` files) finds **no `xla.HloOpcode` descriptor anywhere** — the substring `HloOpcode` does not appear once in the pool. The C++ `HloOpcode` enum is serialized through the `HloOpcodeString` ↔ `StringToHloOpcode` pair into a lowercase text mnemonic: `"add"`, `"dot"`, `"convolution"`, `"fusion"`, `"all-reduce"`, `"dynamic-update-slice"`, `"custom-call"`. This is the single most important serialization detail: it is *why* the format is forward/backward compatible across XLA versions — a new opcode is a new string, with no enum-number coordination between front-end and backend. A reimplementation that defines a numeric opcode enum on the wire will silently diverge from every real dumped module. [Confidence: CONFIRMED — definitive negative result from the descriptor pool.]

> **NOTE — `backend_config` has two encodings, and the new one interns.** The legacy `bytes backend_config = 43` is still present, but field 99 `backend_config_payload` (`xla.Payload`) is the new path: `Payload` is a oneof of `bytes value = 1` OR `int64 id = 2`, where the int64 id indexes into `HloModuleProto.payloads` (field 22, `repeated bytes`). This is an interning side-channel so duplicate backend configs are stored once per module. For TPU, `ConvertFrontendAttributesToBackendConfig` (the last HLO pass, see [compile-phases.md](compile-phases.md)) is what populates these just before the MLIR descent.

### Dynamic shapes and sharding in the proto

Dynamic shapes are encoded *structurally in `ShapeProto`*, not as a separate message: `is_dynamic_dimension`(6) is a `repeated bool` parallel to `dimensions`(3) (the dimension value is the *maximum* bound; the bool marks it runtime-variable), `HloModuleProto.is_dynamic`(11) is the module-level flag, and `LayoutProto.dynamic_shape_metadata_prefix_bytes`(15) reserves the runtime size-metadata prefix. The `DynamicPadder` pre-pass consumes these and emits static shapes plus masks.

Sharding is three coexisting layers, all present: classic tile-based `OpSharding` (`tile_assignment_dimensions`, explicit `tile_assignment_devices` *or* compact `iota_reshape_dims`+`iota_transpose_perm`); the Shardy bridge `NamedShardingProto` reachable from `OpSharding._named_sharding`(14) (mesh-relative `AxisRef` shardings, consumed by `ShardyXLA` when `use_shardy_partitioner=true`); and module-level `spmd_output_sharding`/`spmd_parameters_shardings`. Sharding flows in as `kCustomCall` markers (`"Sharding"`, `"SPMDFullToShardShape"`, `"SPMDShardToFullShape"`) and as `domain` ops bracketing uniform-sharding regions.

---

## The HLO Proto Parse (Proto → `HloModule`)

### Purpose

Once `ConvertMlirHloToHlo` has produced the `HloModuleProto`, the live `HloModule` object graph is reconstructed by `xla::HloModule::CreateFromProto`. This is the symmetric inverse of `ToProto()` and the point at which the id-indexed DAG becomes a pointer-linked `HloInstruction` graph. From here on, the rest of the compiler operates on `HloModule`, not on the proto.

### Entry Point

```text
xla::HloModule::CreateFromProto(HloModuleProto const&,                 0x1e5dbe60
                                HloModuleConfig const&, bool,
                                unique_ptr<CompilationEnvironments>,
                                bool, BufferAssignmentProto*)
  ├─ overload (HloModuleProto const&, HloModuleConfig const&,          0x1e5dbe20
  │            BufferAssignmentProto*, bool)
  └─ xla::HloModule::CreateFromProtoWithConfig(                        0x1e5e07e0
         HloModuleProtoWithConfig const&, …)

xla::HloModule::CreateModuleConfigFromProto(                           0x1e5e0480
    HloModuleProto const&, DebugOptions const&, ExecutionOptions const*)
  └─ builds the HloModuleConfig (entry layout, replica/partition counts,
     SPMD flags, MXU precision) that CreateFromProto consumes
```

### Algorithm

```c
function CreateFromProto(proto, config):                               // 0x1e5dbe60
    module = HloModule(proto.name(), config)
    // 1. Rebuild every computation, resolving the id-graph:
    for comp_proto in proto.computations():                            // flat list
        builder = HloComputation::Builder(comp_proto.name())
        id_to_instr = {}
        for instr_proto in comp_proto.instructions():                  // in id order
            instr = HloInstruction::CreateFromProto(instr_proto, id_to_instr,
                                                    computation_map)    // opcode string → typed op
            id_to_instr[instr_proto.id()] = instr                      // operand_ids resolve here
        comp = builder.Build(id_to_instr[comp_proto.root_id()])        // root_id names output
        module.AddComputation(comp, is_entry = (id == entry_id))
    // 2. Attach module-level tables:
    module.set_schedule(proto.schedule())                              // HloScheduleProto
    module.set_input_output_alias(proto.input_output_alias())
    module.set_frontend_attributes(proto.frontend_attributes())
    module.set_stack_frame_index(proto.stack_frame_index())           // interned provenance
    return module
```

`HloInstruction::CreateFromProto` is where the `opcode` *string* is mapped back to a C++ opcode via `StringToHloOpcode`, and where the union-field selection happens: a `"dot"` reads fields 30/51, a `"convolution"` reads 16/15/50, a `"custom-call"` reads 28/43/77. The `operand_ids` are resolved against the per-computation `id_to_instr` map built as instructions are created in id order — this is why the proto serializes instructions topologically by id.

> **GOTCHA — the `HloModuleConfig` is *not* in the `HloModuleProto`; it is reconstituted separately.** `HloModuleProto` carries the graph; `HloModuleConfigProto` (in `xla.proto`, the `HloModuleProtoWithConfig` pairing) carries the entry-computation *layout*, replica/partition counts, SPMD flags, `matrix_unit_operand_precision` (the MXU precision), `device_memory_size`, and the 290-field `DebugOptions`. `CreateModuleConfigFromProto` (`0x1e5e0480`) builds the config from the proto plus the runtime's `DebugOptions`/`ExecutionOptions`. A reimplementer who deserializes only `HloModuleProto` and defaults the config will get a module with no committed entry layout and default precision — the layout-assignment and MXU-precision decisions that Phase 1 depends on come from the *config*, not the graph proto.

---

## What Is Not on This Page

- **The HLO optimization pipeline that runs after ingestion** (pre-passes, sharding, layout, fusion, MSA, schedule) — see [compile-phases.md](compile-phases.md) and [hlo-pre-passes.md](hlo-pre-passes.md).
- **The IR-layer stack and the five-phase spine overview** — see [overview.md](overview.md).
- **The MLIR descent *out* of HLO** (`HloLegalizeToStablehloPass` and the MHLO→`tpu` lowering) — see [mhlo-xtile-tpu-lowering.md](mhlo-xtile-tpu-lowering.md).
- **The PjRt phase-compile C-ABI surface** (`PJRT_Api` extension type 9, options marshalling) — see [../pjrt/ext-compile-phasecompile.md](../pjrt/ext-compile-phasecompile.md).
- **The exact `HloOpcodeString` mnemonic table for this build.** The opcode set is serialized as strings; the descriptor pool (correctly) carries no enum. The precise ~200-entry mnemonic list must be lifted from the `HloOpcodeString` jump table in the binary's text/rodata; it was not enumerated here. [Confidence: the *category* bindings are CONFIRMED from the recovered attribute fields; the verbatim per-build mnemonic spellings are LOW.]

---

## Cross-References

- [overview.md](overview.md) — Part V orientation; the IR-layer stack and the five compile phases (Phase 0 is named there).
- [compile-phases.md](compile-phases.md) — the per-phase detail; Phase 1 (the HLO pass pipeline) is what runs on the `HloModule` this page produces.
- [hlo-pre-passes.md](hlo-pre-passes.md) — the front-of-pipeline HLO pre-pass set that first touches the ingested module (custom-call expanders, `DynamicPadder`, precision rewriters).
- [hlo-pass-registry.md](hlo-pass-registry.md) — the `HloPassInterface` class catalog these passes derive from.
- [mhlo-xtile-tpu-lowering.md](mhlo-xtile-tpu-lowering.md) — the reverse crossing: HLO back to StableHLO/MHLO and down to the `tpu` dialect (Phase 2a).
- [../pjrt/ext-compile-phasecompile.md](../pjrt/ext-compile-phasecompile.md) — the PjRt phase-compile entry that invokes `CompilePhase0StablehloToHlo`.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / Front-end and pipeline — [back to index](../index.md)
