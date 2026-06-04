# Mosaic Overview

> *All addresses, symbols, op-name strings, dump-stage names, and error strings on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ.*

## Abstract

Mosaic is the tiled-tensor MLIR kernel compiler reached through one HLO opcode — `kCustomCall("tpu_custom_call")` — and it is the **only** producer of the `mlir::tpu` dialect on the TPU device path. A Pallas `@pl.kernel` (or a hand-written Mosaic kernel) is lowered, *outside* `libtpu`, into a `tpu`-dialect MLIR module; that module is serialized, embedded in the custom-call's backend config, and shipped across the PjRt boundary inside the HLO program. When the compiler reaches the custom-call at HLO→LLO emit time, `libtpu` parses the embedded module, runs a version-migration round-trip, and drives it through a 16-stage `tpu`-dialect pass pipeline down to LLO (or, for SparseCore kernels, to LLVM-TPU intrinsics). General HLO never becomes `tpu` ops — it is emitted straight to LLO by ~3225 `xla::jellyfish::*Emitter` classes ([MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md)). Mosaic is therefore a *second IR producer feeding the same target dialect*, not a parallel back end: it skips the entire HLO optimizer and rejoins the main descent at the `tpu` dialect ([The tpu MLIR Dialect](tpu-dialect-and-ops.md)).

This is the orientation page for the Mosaic backend. It fixes what Mosaic is, the import/serde seam that brings a `tpu` module into the compiler, the `CustomCallEmitter::Emit` driver that glues the imported module to the surrounding HLO program, the 16-stage `RunMLIRPasses` pipeline (TensorCore and SparseCore arms), and a map of the Mosaic sub-pages. It does **not** reproduce the layout algebra — the `VectorLayout` `(sublane, lane)` value type lives on [Mosaic VectorLayout](mosaic-vectorlayout.md) and the per-op layout-inference rules on [Mosaic Layout Inference](mosaic-layout-inference.md); both are linked, not duplicated.

For reimplementation, the orientation contract is:

- **Mosaic is the only `tpu`-dialect origin.** The binary has **zero** `*ToTpuPass`/`MhloToTpu`/`StablehloToTpu` conversion passes; the `tpu` dialect is *imported*, authored upstream, never lowered from MHLO. A reimplementer must not build an MHLO→`tpu` legalizer that does not exist.
- **The import + serde seam.** `GetMlirModuleOpFromCustomCall` → `GetCachedCustomCallBody` (CityHash128-keyed, one parse per unique kernel) → `GetMlirModule` → `ParseModule` + a single `MosaicSerdePass` deserialize/upgrade. The serde is a per-op **version-migration** engine, not a generic codec.
- **The driver entry.** `CustomCallEmitter::Emit` validates the kernel `main`'s ABI against the HLO operands, binds memory-space colors and windows, selects the entry func by core type, runs the pipeline, and stitches the resulting LLO into the parent region.
- **The 16-stage pipeline.** `RunMLIRPasses` runs `simplify → infer-memref-layout → canonicalize-mosaic → infer-vector-layout → apply-vector-layout → … → lower-to-llo`, with a SparseCore branch that diverts to `lower-to-mlo` → LLVM-TPU instead.
- **The sub-page map.** Where the layout algebra, the layout-inference rules, the tpu→LLO descent, and the SparseCore lowering each live.

| | |
|---|---|
| **What Mosaic is** | the tiled-tensor MLIR kernel compiler; the only producer of `mlir::tpu` on the TPU path, via `tpu_custom_call` import |
| **Entry opcode / target** | `HloOpcode::kCustomCall`, `custom_call_target = "tpu_custom_call"` |
| **Driver entry** | `xla::jellyfish::(anon)::CustomCallEmitter::Emit` @ `0x111ef740` (3657 lines; source `…/mosaic/python/custom_call_emitter.cc`) |
| **Import seam** | `mlir_utils::GetMlirModuleOpFromCustomCall` @ `0x13e327a0` → `GetCachedCustomCallBody` @ `0x13e31860` → `GetMlirModule` @ `0x13e31220` |
| **Serde engine** | `mlir::tpu::MosaicSerdePass::runOnOperation` @ `0x145307a0`; `jaxlib::mosaic::RunSerde` @ `0x14533b20`; module attr `stable_mosaic.version`, default version 11 |
| **Pipeline driver** | `xla::jellyfish::RunMLIRPasses` @ `0x111fefa0` (source `…/mosaic/python/mosaic_passes.cc`); 16 stages, dumps `post-<stage>` |
| **Entry-func selection** | `mlir::tpu::GetFuncWithCoreType` @ `0x14aa61a0` (matches `tpu.core_type`, falls back to `@main`) |
| **Mem-space bridge** | `MemorySpaceToColor` @ `0x1d6ffb80` / `ColorToMemorySpace` @ `0x1d6ffb00` |
| **TensorCore handoff** | `mlir::tpu::createLowerToLLOPass` @ `0x11203ba0` → `llo::createEliminateLLOExtensionsPass` @ `0x13e668a0` |
| **SparseCore handoff** | `mlir::tpu::createLowerToMloPass` @ `0x1322adc0` → `sparse_core::CreateLowerToSparseCoreLlvmPass` @ `0x135667c0` |
| **MHLO→`tpu` passes in binary** | **none** (0 `*ToTpuPass`/`MhloToTpu`/`StablehloToTpu` symbols) |
| **Confidence** | HIGH (symbol/string-anchored) unless a row or callout says otherwise |

---

## What Mosaic Is, and Why It Is a Side Channel

Mosaic is not a stage inside the HLO optimizer; it is an *escape hatch*. The general TPU compute path lowers HLO to LLO directly through ~3225 `xla::jellyfish::*Emitter` classes (`DmaEmitter`, `FusionEmitter`, `ReduceEmitter`, `GatherEmitter`, …) calling into `LloRegionBuilder` (3037 referencing functions) — no MLIR `tpu` dialect is ever produced for general compute. The `tpu` dialect exists in the binary **only** because Pallas/Mosaic kernels are authored in it upstream and imported. Two independent observations pin this down:

- **No conversion pass produces `tpu`.** The function table contains **zero** symbols matching `*ToTpuPass`, `*ToTPUDialect`, `MhloToTpu`, `HloToTpu`, `StablehloToTpu`, `LegalizeToTpu`, `LowerMhloToTpu`. (The only `ConvertToTpu*` hits are `ConvertToTpuCompilationCacheGroupEntryProto` and `ConvertToTpuCoreProgram` — runtime/SparseCore-ISA, unrelated.)
- **The general path bypasses `tpu` entirely.** Each `*Emitter` is a per-HLO-opcode visitor that builds `llo.*` ops straight into an `LloRegion`.

So the device path is two trees that converge at the `tpu` dialect only on the Mosaic arm:

```text
TREE A1 — general TPU compute
  HLO ──[97-row pre-passes]──► HLO ──[~3225 jellyfish *Emitter + LloRegionBuilder]──► LLO ──► bundles
        (never becomes tpu-dialect ops)

TREE A2 — Pallas/Mosaic kernels  ◄── THIS PAGE
  HLO kCustomCall("tpu_custom_call") carrying a SERIALIZED tpu-dialect module
        ──[import + serde]──► tpu-dialect IR
        ──[RunMLIRPasses 16-stage pipeline]──► LLO (TensorCore) | LLVM-TPU (SparseCore)
```

Mosaic kernels skip the HLO optimizer (no algebraic simplification, layout assignment, fusion, or MSA on the kernel body) because they arrive *already* as `tpu` ops carrying their own tiling. They converge with the optimized path at the `tpu` dialect and share the identical tpu→LLO→bundle descent from there.

> **GOTCHA — do not build an MHLO→`tpu` legalizer.** The convergence point the [compiler overview](overview.md) draws at Level 2 (`tpu` dialect) is fed by exactly one producer: the Mosaic custom-call import. General HLO bypasses `tpu` and goes straight to LLO. A reimplementation that allocates effort to an MHLO/StableHLO→`tpu` conversion-pattern set is building a stage the production compiler does not contain. See [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md) for the full two-tree evidence.

> **NOTE — "TLP"/"MLO" are IR-layer names, not dialects.** The `tpu` dialect's physical role as the "TPU-Level Program" container is realized only for the imported Mosaic kernel. "Mlo" (mid-level ops) is the SparseCore intermediate *inside* the `tpu`/`sparse_core` lowering (see [The SparseCore Arm](#the-sparsecore-arm)), not a top-level IR.

---

## The Mosaic Kernel Embedding

A Pallas `@pl.kernel` is compiled by the JAX frontend (outside `libtpu`) into a `tpu`-dialect module, run through `MosaicSerdePass` in **serialize** mode (downgrade to a pinned version, set the module's `stable_mosaic.version` attr), and embedded into one HLO instruction:

```text
HloOpcode::kCustomCall
  custom_call_target = "tpu_custom_call"
  backend_config     = serialized xla::jellyfish::CustomCallConfig proto:
      mlir_module           (an absl::Cord; the serialized tpu module)
      serialization_format  (must equal 1)
      input_memory_colors / output_memory_colors
      has_communication, collective_id, cost_estimate, metadata
```

Four HLO pre-passes prepare the call before lowering ([Custom-Call Lowering](custom-call-lowering.md)): `MosaicFusion` (fuse the surrounding HLO into operands) → `TpuCustomCallLegalizer` (TensorCore/SparseCore/Megachip classification) → `TpuCustomCallMemorySpacePolicy` (fill the memory colors) → `TpuCustomCallScopedVmemAdjuster` (size scoped VMEM). At HLO→LLO emit time the registered emitter for the `"tpu_custom_call"` target is `CustomCallEmitter::Emit`.

---

## Import and the MosaicSerde Round-Trip

The import is a thin caching wrapper that parses the embedded module exactly once per unique kernel and runs a deserialize/upgrade pass.

```text
GetMlirModuleOpFromCustomCall (0x13e327a0)
  └─ GetCachedCustomCallBody (0x13e31860)
        1. validate body present (else "Custom call body is empty.")
        2. key = farmhash::CityHash128WithSeed(Cord→string(mlir_module))   // 128-bit
        3. HloModule::GetCacheEntry<MosaicMlirCacheEntry>(module, key)
              HIT  → reuse the parsed ModuleOp across every duplicate kernel
              MISS → build it:
                       ctx = new mlir::MLIRContext
                       LoadDialects(ctx)                      // 0x13e32140
                       GetMlirModule(config, ctx, verify)     // 0x13e31220
                       cache a MosaicMlirCacheEntry (OWNS ctx + ModuleOp)
        4. fingerprint-collision guard → fatal on bcmp mismatch
              ("Kernel body fingerprint collision detected for key: …")
```

`LoadDialects` (`0x13e32140`) builds the kernel's *self-contained* MLIRContext (it does not share the host compiler's): it loads `stablehlo`, `arith`, `func`, `memref`, `scf`, `vector`, **`mlir::tpu::TPUDialect`**, `cf`, **`mlir::llo::LLODialect`**, `mlir::sparse_core::ScDialect` (registered as `"sc_tpu"`), plus `RegisterNonSparseCoreDialects` and `func::registerAllExtensions`, then calls `allowUnregisteredDialects(true)` — required by the serde's serialize side and to let unknown ops survive a round-trip.

`GetMlirModule` (`0x13e31220`) parses and deserializes:

1. `ParseModule` (`0x13e30dc0`) wraps `mlir::parseSourceString` — it consumes **both** textual MLIR and MLIR bytecode through one entry — under a `StatusScopedDiagnosticHandler`, then `constructContainerOpForParserIfNecessary<ModuleOp>`. Failure: `"Failed to parse the Mosaic module: <diag>"`.
2. If `serialization_format == 1`, run one `MosaicSerdePass(serialize=false)` in a `PassManager` (dump stage `"deserialization"`); any other value → `"Unsupported serialization format: <N>"`.

### MosaicSerdePass is a version-migration engine

`MosaicSerdePass::runOnOperation` (`0x145307a0`) is **not** a generic bytecode codec — it is a per-op version upgrade/downgrade engine. It runs in two modes: `serialize=false` (deserialize/**upgrade**, used on import) and `serialize=true` (serialize/**downgrade**, used when writing the kernel into the persistent compilation cache). It lazily builds two `StringMap`s keyed by op-name (`upgrade_rules`, `downgrade_rules`), dispatched by `jaxlib::mosaic::RunSerde` (`0x14533b20`).

`RunSerde` reads the module's `stable_mosaic.version` IntegerAttr, bounds-checks it (`"Unsupported version: expected <= <cur> but got <stored>"`), removes the attr, then walks every op applying the matching migration lambda. Eight ops carry per-version rules:

| op (key) | schema evolution the rule encodes |
|---|---|
| `tpu.enqueue_dma` | v2 added the operand-segment-sizes form; v4 added `priority`; v8 added `strict_ordering` |
| `tpu.wait_dma2` | v7 added `device_id`/`core_id` segments |
| `tpu.dynamic_gather` | v5: singular `dimension` → multi-dim `dimensions` |
| `tpu.iota` | v6: singular `dimension` → multi-dim `dimensions` |
| `tpu.sem_signal` | v2 added `core_id` |
| `vector.multi_reduction` | v3: `reduction_dims` ArrayAttr-of-IntegerAttr → DenseArrayAttr<i64> |
| `tpu.store` | v11 added the fused store-and-accumulate `add` flag |
| `arith.constant` | v10 generalized vector-of-i1 constants (pre-v10 must be splat) |

The current dialect version is **11**. An out-of-tree compiler reading a JAX-emitted persistent cache must replay the matching downgrade lambdas down to its pinned version. The string constants resolve to `kMangledDialect = "stable_mosaic"` (the bytecode dialect tag, 13 chars) and `kVersionAttrName = "stable_mosaic.version"`. The per-version field-migration bodies are documented on [Mosaic VectorLayout](mosaic-vectorlayout.md) alongside the layout serialization.

> **NOTE — the bytecode magic is upstream MLIR; the TPU-specific part is the version attr + the 8 rules.** Parsing handles standard MLIR text/bytecode; the only TPU-specific deserialization work is reading/removing `stable_mosaic.version` and replaying the eight per-op upgrade lambdas.

---

## The Driver: CustomCallEmitter::Emit

`CustomCallEmitter::Emit` (`0x111ef740`, 3657 lines) is the registered LLO emitter for `"tpu_custom_call"`. It connects the imported `tpu` module to the surrounding HLO program and drives the whole pipeline. Recovered call structure and verbatim error strings:

1. **Fetch & validate config** — `"Custom call does not have a custom call config."` / `"Failed to retrieve the config. Error: %s"`.
2. **Import the body** — `GetCachedCustomCallBody` → cached `tpu` `ModuleOp`. Re-entry guard: `"Trying to lower the same Mosaic kernel more than once"`.
3. **Core-type gate** (TensorCore vs SparseCore) from the cache entry's core-type set: `"Cannot lower a Mosaic kernel targeting SparseCore on a device without SparseCore"`, `"Cannot lower a Mosaic kernel targeting SparseCore to TensorCore."`, `"Mosaic is not supported on this hardware version."`.
4. **Validate the kernel `main` ABI** (the HLO↔kernel contract):

   ```text
   main.getNumArguments() == iteration_bounds + num_inputs + num_outputs
                             + num_scratch + has_communication
   main.getNumResults()   == 0          (all outputs are by-ref memrefs)
   main.getRegion().hasOneBlock()
   ```

5. **Bind operands/outputs** — element-type allow-list (`float8e5m2`, `float8e4m3fn`, `float8e4m3b11fnuz`, `float8e8m0fnu`, `float4e2m1fn`, `bfloat16`, 32-bit), per-operand memory-space (`"Some arguments are missing their memory space attribute"`), memory-space→color via `MemorySpaceToColor`, layout verify via `VerifyAndMaybeUpdateShapeLayoutForMosaic` (`"Missing layout for Mosaic kernel operand %d"` / `"Failed to verify layout for Mosaic kernel operand "`), scratch placement (`"Scratch memref allocation only supported for vmem, smem and semaphore_mem."`), and semaphore-liveness on exit (`"Semaphore (scratch argument %d) has a nonzero value upon exit … Make sure every DMA is awaited …"`).
6. **Windowing** — `PipelineEmitter` (39 references in `Emit`) windows each operand/output into the iteration grid, reading the `window_params`, `iteration_bounds`, and `dimension_semantics` attrs off `main` (`"Wrong number of window_params: expected %d, got %d"`, `"iteration_bounds should be a DenseI64ArrayAttr"`). The window-descriptor math (grid flatten, double-buffering, dynamic-offset DMA, window cycle cost) is recovered on [Mosaic VectorLayout](mosaic-vectorlayout.md).
7. **Select the entry func** — `GetFuncWithCoreType(module, core_type)` (`0x14aa61a0`) returns the `func.func` whose `tpu.core_type` matches the device, falling back to `@main` (`"No function with tpu.core_type = %v nor a main function found"`).
8. **Run the pipeline** — `RunMLIRPasses(...)` lowers the module's `main` to LLO in-place (next section).
9. **Megacore split** — `MegacoreAdjuster` (4 references) splits the work across the two TensorCores of a megacore chip.
10. **Stitch into LLO** — the lowered body is emitted into the parent `LloRegion` via `LloRegionBuilder` (`AllocateScopedVmem`/`Smem`, `EnqueueDmaLocalInGranules`, …); post-emit cleanup `"post-finalize-llo-post-emitter"`. Returns the LLO SSA value(s) for the kernel outputs.

> **NOTE — the driver entry is `CustomCallEmitter::Emit`, not `MosaicEmitter`.** `MosaicEmitter` (`EmitWindow` @ `0xfaadcc0`) is a small anonymous-namespace **window-emit helper** — a sibling of `MosaicBroadcast` and `PipelineEmitter::OperandWindow`, used *inside* the windowing step. The class that actually imports the module, validates the ABI, runs the pipeline, and stitches LLO is `CustomCallEmitter::Emit` (`0x111ef740`). "MosaicEmitter" is a loose name for the Mosaic side door; the driver symbol is `CustomCallEmitter::Emit`.

### How the imported module connects to HLO

The connection is by ABI on `main` plus a memory-space-color bridge:

| Surrounding HLO concept | `tpu`-module `main` binding |
|---|---|
| `kCustomCall` operand *i* (HLO buffer) | a `memref` arg (tiled layout + `tpu.memory_space` attr); color from `config.input_memory_colors[i]` |
| `kCustomCall` result | a by-ref output `memref` arg (`num_results == 0`) |
| grid / pipeline dims | `iteration_bounds` (`DenseI64ArrayAttr`) on `main` |
| parallel vs reduction grid dims | `dimension_semantics` attr |
| per-operand window/tiling | `window_params` attr (one per memref arg) |
| scoped scratch (vmem/smem/sem) | extra `main` args after the outputs |
| collective barrier | `has_communication` arg (+ `config.collective_id`) |

`MemorySpaceToColor` (`0x1d6ffb80`) maps the mosaic-tpu memref memory-space enum onto the integer "color" the host `BufferAssignment` uses, so an imported kernel's inputs/scratch land in the right physical memory. The mosaic-tpu MemorySpace enum (`vmem`/`smem`/`hbm`/`cmem`/`semaphore_mem`/`vmem_shared`/`host`) and the jellyfish color table values are documented on [Mosaic VectorLayout](mosaic-vectorlayout.md).

---

## The tpu-Dialect Pass Pipeline: RunMLIRPasses

`RunMLIRPasses` (`0x111fefa0`, source `…/mosaic/python/mosaic_passes.cc`) is the actual `tpu`-dialect pipeline. Each pass runs via `RunPass` (`0x14514d60`), which wraps the `PassManager` in `RunAndCaptureDiagnostics` and dumps MLIR as `post-<stage>` (or `post-<stage>-failed`). The TensorCore pipeline, in execution order with create-functions (all `mlir::tpu::` unless noted):

| # | stage (dump name) | create-function / action |
|---|---|---|
| 0 | `original` | `DumpMlir`; setup: `GetFuncWithCoreType`, `GetMosaicHardwareGeneration(target)`; guard `"Should not run HLO passes without layout passes"` |
| 1 | `deserialize`/diagnostics | `RunAndCaptureDiagnostics` |
| 2 | `simplify` | `$_4` canonicalizer → `InferMemorySpaces` (dump `post-infer-memref-space`) |
| 3 | `hlo-conversion` *(iff `run_hlo_passes`)* | nest `func.func`: `stablehlo::createStablehloLegalizeToLinalgPass` + `LinalgVectorizationPass` |
| 4 | `infer-memref-layout` | `createInferMemRefLayoutPass` (`0x132c0f00`) (+ `-simplify`) |
| 5 | `assert-insertion` | `createDebugAssertInsertionPass` |
| 6 | `canonicalize-operations` | `createCanonicalizeOperationsPass` |
| 7 | `pre-canon-optimization` | `createPreCanonicalizationOptimizationPass` |
| 8 | `canonicalize-mosaic` | `createCanonicalizeMosaicPass` (`0x132a2ac0`) (+ `-simplify`) |
| 9 | `tiling-propagation` | `createTilingPropagationPass` |
| 10 | `infer-vector-layout` | `createInferVectorLayoutPass` (`0x132c2c20`) |
| 11 | `relayout-insertion` | `createRelayoutInsertionPass` |
| 12 | `apply-vector-layout` | `createApplyVectorLayoutPass` (`0x1325cda0`) (+ `-simplify`) |
| 13 | `logical-to-physical-device-id` | `createLogicalToPhysicalDeviceIdPass` |
| 14 | `lower-to-llo` | nest `func.func`: `createLowerToLLOPass(target)` (`0x11203ba0`) |
| 15 | `eliminate-llo-extensions` | nest `func.func`: `llo::createEliminateLLOExtensionsPass` (`0x13e668a0`) |
| 16 | `finalize-llo` | `$_4` canonicalizer |

After `finalize-llo` the module body is pure `llo.*` (+ `scf`/`func`/`memref` structural ops) and control returns to `CustomCallEmitter::Emit` for the `LloRegionBuilder` stitch. The `-simplify` sub-stages run the `$_4` canonicalize/CSE lambda after the three layout passes. IR printing is gated by VLOG; the verifier by `IsMosaicVerificationEnabled`.

The layout heart of the pipeline is stages 10–12. `infer-vector-layout` annotates every op with `in_layout`/`out_layout` attributes (the per-op inference rules — [Mosaic Layout Inference](mosaic-layout-inference.md)); `relayout-insertion` inserts explicit `tpu.relayout` wherever a consumer's required layout differs from a producer's, so that `apply-vector-layout` (`applyLayoutFunc` @ `0x1325cc80` → per-op `applyLayoutOp`) materializes each logical `vector<NxMxT>` into hardware-native vreg tiles via a 49-entry op-name→rule dispatch table. The `VectorLayout` value type, the textual grammar, the per-vreg packing math, and the relayout driver are all on [Mosaic VectorLayout](mosaic-vectorlayout.md).

> **GOTCHA — `canonicalize-mosaic` runs `allow_unverified=!a12`.** `createCanonicalizeMosaicPass` takes a `gen` + `IsMosaicCompatibilityModeEnabled` + an `allow_unverified` flag derived from the verification gate. A reimplementer must not assume the canonicalizer always re-verifies; in compatibility mode it tolerates unverified IR so older kernels survive.

> **NOTE — four of the 22 inventoried `tpu` passes are not in the TensorCore arm.** `CanonicalizeMemorySpace` and `ConvertIntegerMemrefs` belong to other paths, `LowerToMlo` to the SparseCore branch below, and `MosaicSymbolicShapeRefinement` runs earlier as a hybrid HLO+MLIR pass. Searching this 16-stage list for them will not find them.

---

## TensorCore vs SparseCore: the Two Lowering Arms

The pipeline forks after `logical-to-physical-device-id` on the entry func's `tpu.core_type`.

### The TensorCore arm (stages 14–16)

`createLowerToLLOPass(target)` (`0x11203ba0`) runs the full `applyFullConversion` of `tpu.*` → `llo.*` (242 patterns — [tpu → LLO Lowering](tpu-to-llo-ods.md)); `createEliminateLLOExtensionsPass` (`0x13e668a0`) expands LLO "extension" convenience-macro ops into base LLO ops; `finalize-llo` canonicalizes. After this, every `vector` value is a native vreg shape that maps 1:1 onto an LLO `VregType`, and the body is stitched into the parent `LloRegion`. Downstream, [TpuProgram Serialization](tpu-program-serialization.md) and the [scheduling back end](../sched/overview.md) pack bundles.

### The SparseCore arm

A kernel whose `main` carries a SparseCore `tpu.core_type` is **not** lowered to LLO. Instead the `tpu` ops convert to the SparseCore "Mlo" (mid-level-ops) stage and then to LLVM-TPU intrinsics:

```text
tpu (SparseCore core_type) module
  → [mosaic_sc::InferVectorLayoutPass + ApplyVectorLayoutPass]      (vreg layout; only op: mosaic_sc.relayout)
  → mlir::tpu::createLowerToMloPass(Target, LowerToMloPassContext)  (0x1322adc0)
        converts tpu.* (incl. SparseCore-only tpu.barrier, tpu.all_reduce,
        tpu.enqueue_indirect_dma, tpu.fetch_and_add_sync, tpu.scan,
        tpu.wait_indirect_dma) into mlo/sparse_core/llvm ops
  → MloModuleVerifier
  → sparse_core::CreateLowerToSparseCoreLlvmPass(Target, …)         (0x135667c0)  → mlir::LLVM / LlvmTpu
  → sparse_core::CreateLlvmIntToPtrSafetyPass(Target)               (int↔ptr safety legalize)
  → mlir-translate -mlir-to-llvmir → per-gen SparseCore ISA
```

The SparseCore Mosaic dialect (`mosaic_sc`) is intentionally tiny — one op (`mosaic_sc.relayout`) plus the two layout passes — so the Mosaic vreg-layout phase can run for SparseCore before the `tpu → mlo → LlvmTpu` lowering. Detail: [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md), [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md), [SCTypeConverter](sc-type-converter.md), and the [SparseCore Part](../sparsecore/overview.md).

---

## The Complete Mosaic → tpu → LLO Chain

```text
JAX/Pallas frontend (out of libtpu)
  @pl.kernel → tpu-dialect module → MosaicSerde SERIALIZE (downgrade, set stable_mosaic.version)
             → embedded in HloCustomCall("tpu_custom_call").backend_config.mlir_module
─────────────────────────────── libtpu.so ───────────────────────────────
HLO pre-passes: MosaicFusion → TpuCustomCallLegalizer
                → TpuCustomCallMemorySpacePolicy → TpuCustomCallScopedVmemAdjuster
   │  at HLO→LLO emit time, target = "tpu_custom_call"
   ▼
CustomCallEmitter::Emit (0x111ef740)
   │  GetMlirModuleOpFromCustomCall (0x13e327a0)
   │    └ GetCachedCustomCallBody (0x13e31860): CityHash128 key; on miss
   │        new MLIRContext → LoadDialects → GetMlirModule (parse + MosaicSerde deserialize)
   │  validate main ABI; bind memref colors (MemorySpaceToColor); set windows (PipelineEmitter)
   │  GetFuncWithCoreType (0x14aa61a0) → pick entry func
   │        ▼  RunMLIRPasses (0x111fefa0)
   │   simplify → infer-memref-layout → canonicalize-mosaic → tiling-propagation
   │   → infer-vector-layout → relayout-insertion → apply-vector-layout
   │   → logical-to-physical-device-id
   │   ├── TensorCore: → lower-to-llo (242 patterns) → eliminate-llo-extensions → finalize-llo
   │   └── SparseCore: → (mosaic_sc layout) → lower-to-mlo → MloModuleVerifier
   │                   → lower-to-sparsecore-llvm → llvm-int-to-ptr-safety ⇒ LLVM-TPU
   │  MegacoreAdjuster (split across 2 TensorCores)
   │  stitch LLO into parent LloRegion via LloRegionBuilder
   ▼
LLO instruction stream → bundle packer → ISA bundles   (SparseCore: LLVM IR → per-gen SC ISA)
```

---

## Map of the Mosaic Sub-Pages

| topic | page | owns |
|---|---|---|
| the layout atom | [Mosaic VectorLayout](mosaic-vectorlayout.md) | the `(sublane, lane)` `VectorLayout` struct + invariants, textual grammar, `tilesPerVreg`/`tileArrayShape`, the `ImplicitDim` model, the `relayout` driver, the 49-entry `applyLayoutOp` dispatch table, the 8 serde migration lambdas, the `MemorySpaceToColor` table values, and the `PipelineEmitter` window math |
| the layout solver | [Mosaic Layout Inference](mosaic-layout-inference.md) | the per-op `VectorLayoutInferer` rules that *choose* each op's `in_layout`/`out_layout` (the producer of the attrs `applyLayoutOp` consumes) |
| the target dialect | [The tpu MLIR Dialect](tpu-dialect-and-ops.md) | the `tpu` dialect's ops, attributes, and op-model contract |
| TensorCore handoff | [tpu → LLO Lowering](tpu-to-llo-ods.md) | the `createLowerToLLOPass` 242-pattern descent into LLO |
| SparseCore handoff | [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md), [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md), [SCTypeConverter](sc-type-converter.md) | the `tpu → mlo → LlvmTpu` SparseCore lowering |
| the dispatch layer | [Custom-Call Lowering](custom-call-lowering.md) | the `tpu_custom_call` target registration + the four HLO pre-passes |

---

## Confidence Summary

| Claim | Evidence |
|---|---|
| Mosaic (`tpu_custom_call` import) is the only `tpu`-dialect producer | 0 `*ToTpuPass`/`MhloToTpu`/`StablehloToTpu` symbols; ~3225 jellyfish `*Emitter` + 3037 `LloRegionBuilder` refs for the general path |
| Import seam = `GetMlirModuleOpFromCustomCall` → `GetCachedCustomCallBody` → `GetMlirModule`/`ParseModule`/`LoadDialects` | `0x13e327a0`, `0x13e31860`, `0x13e31220`, `0x13e30dc0`, `0x13e32140` decompiled; CityHash128 key + `MosaicMlirCacheEntry` cache |
| Serde is a per-op version-migration engine; 8 ops; default v=11; attr `stable_mosaic.version` | `MosaicSerdePass::runOnOperation` `0x145307a0`, `RunSerde` `0x14533b20`; `kMangledDialect`/`kVersionAttrName` from `.data` relocs |
| Driver entry is `CustomCallEmitter::Emit` (3657 lines), calling `GetCachedCustomCallBody`/`GetFuncWithCoreType`/`MemorySpaceToColor`/`PipelineEmitter`/`MegacoreAdjuster`/`RunMLIRPasses` | `0x111ef740` decompiled; 1/1/2/39/4/1 references confirmed; verbatim ABI/memory/window/semaphore error strings |
| `MosaicEmitter` is a window-emit helper, not the driver | `MosaicEmitter::EmitWindow` `0xfaadcc0`, sibling of `MosaicBroadcast`/`PipelineEmitter::OperandWindow`; not called as the lowering driver |
| 16-stage `RunMLIRPasses` pipeline with the listed stage names + create-functions | `0x111fefa0` (source `mosaic_passes.cc`); `createInferMemRefLayoutPass` `0x132c0f00`, `createCanonicalizeMosaicPass` `0x132a2ac0`, `createInferVectorLayoutPass` `0x132c2c20`, `createApplyVectorLayoutPass` `0x1325cda0`, `createLowerToLLOPass` `0x11203ba0`, `createEliminateLLOExtensionsPass` `0x13e668a0` |
| Entry-func selection by `tpu.core_type` via `GetFuncWithCoreType` | `0x14aa61a0`; error strings `"No function with tpu.core_type = %v …"` |
| SparseCore arm = `mosaic_sc` layout → `createLowerToMloPass` → `MloModuleVerifier` → `CreateLowerToSparseCoreLlvmPass` → LLVM-TPU | `createLowerToMloPass` `0x1322adc0`, `CreateLowerToSparseCoreLlvmPass` `0x135667c0`, `mosaic_sc::createInferVectorLayoutPass` `0x132ecf60` |
| Exact `hlo-conversion`/megacore/large-2nd-minor arm gating per TPU generation | call sequence recovered; per-gen flag selection not fully traced |

---

## Cross-References

- [The TPU Compiler (overview)](overview.md) — the five-phase spine; this page details its "Mosaic side channel" section.
- [MHLO → XTile → tpu Lowering](mhlo-xtile-tpu-lowering.md) — the two-tree evidence and the proof that no MHLO→`tpu` pass exists; the general path's jellyfish `*Emitter` route.
- [Custom-Call Lowering & the Target Registry](custom-call-lowering.md) — the `tpu_custom_call` target registration and the four HLO pre-passes that prepare the embedding.
- [The tpu MLIR Dialect](tpu-dialect-and-ops.md) — the target dialect that Mosaic imports and the pipeline lowers.
- [Mosaic VectorLayout](mosaic-vectorlayout.md) — the `(sublane, lane)` layout algebra, the `applyLayoutOp` rule table, the serde migration lambdas, and the window-descriptor math (linked, not duplicated here).
- [Mosaic Layout Inference](mosaic-layout-inference.md) — the per-op inference rules that produce the `in_layout`/`out_layout` attrs.
- [tpu → LLO Lowering](tpu-to-llo-ods.md) — the TensorCore `createLowerToLLOPass` descent.
- [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md) · [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) · [SCTypeConverter](sc-type-converter.md) — the SparseCore lowering arm.
- [TpuProgram Serialization](tpu-program-serialization.md) — how the lowered LLO is packed and serialized.
- [Part VIII — Scheduling](../sched/overview.md) · [SparseCore Overview](../sparsecore/overview.md) — the back ends the two arms hand off to.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
