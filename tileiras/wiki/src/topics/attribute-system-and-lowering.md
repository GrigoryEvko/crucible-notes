# Attribute System and Lowering

## Abstract

Compiler attributes carry the semantic context that IR-graph structure alone cannot express: kernel launch shape, fast-math flags, pipeline staging, scheduling hints, memory ordering, layout descriptors. Every lowering stage in tileiras has a deliberate policy for each attribute family — preserve it under a renamed key for the new dialect, consume it and drop the carrier after the analysis that needed it finished, or synthesize a fresh attribute from inferred facts. A reimplementation that drops the wrong attribute at the wrong stage emits PTX that compiles and runs but produces the wrong answer; the bytes survive ptxas because nothing it sees is malformed.

This page is the canonical reference for the attribute system as a whole. The per-stage lowering pages document where individual attributes flow; the dialect type-and-attribute pages document the per-attribute parse contract and verifier. This page documents the cross-cutting policy that ties them together: what each lowering stage does to each attribute family, which transitions are intentionally lossy, and which silent drops are wrong-output bugs waiting to be introduced.

## Attribute carriers

MLIR exposes five places an attribute can live. Tileiras uses all five, and the decision of which carrier to use is part of the attribute's contract — moving an attribute from one carrier to another changes who reads it, when it is read, and what happens when the carrier disappears under a rewrite.

| Carrier | Storage | Lifetime | Primary readers |
|---|---|---|---|
| Op attribute dictionary | `DictionaryAttr` on the op header, or inherent `Properties` storage for ops that declared an inherent attribute slot | Bound to the op; survives clones unless the op is rewritten away | Verifiers, fold rules, conversion patterns, the AsmPrinter |
| Type-storage parameters | Fields inside the type's `TypeStorage` derivative, uniqued through the context `StorageUniquer` | Bound to the type identity; outlives every op that uses the type | Type-equality checks, type converters, every walker that keys on type |
| Function-level named-attribute dictionary | `func.func` (or `llvm.func`) operation header | Bound to the function symbol; survives function-level clones | Function-boundary lowering, LLVM function-attribute emission, the PTX directive emitter |
| Module-level dictionary | `builtin.module` operation header | Bound to the module; survives across passes that do not rewrite the module shell | Pipeline driver, target-attachment pass, options-mapping pass |
| NVVM properties blob | Per-op compact slot table at `Operation*+64`, slots stride 8 bytes | Bound to the op like an inherent attribute, but the slots are positional, not keyed | The NVVM-to-LLVM dispatcher arms documented in [Properties Blob and Attr Parsers](../dialects/nvvm/properties-blob-and-attr-parsers.md) |

The carrier decision matters because the rules for who can read a carrier differ. An op-attribute dictionary entry is keyed by string; a passes that consumes the op can fetch it through `getAttr("name")`. A type-storage parameter is positional; only code that knows the type's storage class can read it. A function-level attribute is read by a different set of passes than an op-level attribute carrying the same name. Moving an attribute from op-level to function-level — for example, when a kernel-spec entry on the function summarises a per-op annotation — changes the answer to "which pass owns this attribute now?".

## Lifecycle of a kernel attribute

A concrete attribute makes the policy concrete. The frontend hint `tt.num_warps = 4` — a Triton-style annotation requesting four warps per CTA — flows through every lowering stage in tileiras, changing carrier and key as it travels. The end result is the PTX directive `.reqntid 128, 1, 1` in the kernel's `.entry` header.

Stage 0 (frontend input). A Triton-style producer emits `cuda_tile` bytecode with the kernel-spec hint attached to the module:

```text
module attributes { tt.num_warps = 4 : i32 } {
  cuda_tile.entry @gemm(...) { ... }
}
```

Stage 1 (`ConvertCudaTileToTileAA`). The pass walks `cuda_tile.module` operations and lowers their bodies, but the module-level dictionary entry passes through verbatim. The `nv_tileaa` dialect declares the same string key as a legal attribute on its enclosing module, so the conversion target does not reject it. The lifecycle here is "preserve, do not rename".

Stage 2 (`ConvertTileAAToTileAS`, kernel-spec attach). The `attachKernelSpecAttributes` step folds the frontend hint into the function-level `nv_tileaa.kernel_spec` attribute. The bytes `num_warps = 4` become one field of a structured kernel-spec record on the function. The lifecycle is "consume and synthesize" — the module-level `tt.num_warps` is read once and a function-level `nv_tileaa.kernel_spec` is written.

Stage 3 (TileAS scheduling and layout). The scheduler reads `kernel_spec.num_warps = 4` to size the warp partitioning and resource pools. The agent-switch pass reads `nv_tileas.num_warps = 4` (a per-agent mirror written by `OptimizeExecutionUnitMapping`) to round each agent's starting warp to its group size. The lifecycle is "read to act, do not rewrite".

Stage 4 (`ConvertTileFuncToLLVM`). The function-boundary lowering reads `nv_tileaa.kernel_spec` and writes `nvvm.reqntid = 128 : i32` onto the rewritten `func.func`, derived from `32 * num_warps = 32 * 4 = 128`. The lifecycle is "consume and synthesize"; the kernel-spec attribute remains for downstream readers, but the `nvvm.reqntid` carrier is what the PTX emitter consumes next.

Stage 5 (PTX directive emission). The kernel-directive emitter walks the LLVM function's `nvvm.*` attribute set in the fixed order documented in [Host Launch and ptxas Knobs](../driver/host-launch-and-ptxas-knobs.md) and emits `.reqntid 128, 1, 1` into the `.entry` header. The lifecycle is "read and project to PTX".

The full trace is six carriers in five stages: module dictionary → function-level kernel-spec → scheduler-internal pool sizing → function-level `nvvm.*` → PTX directive → cubin metadata consumed by the CUDA driver at launch time. Each transition has a different rule, and each transition is owned by exactly one pass.

## Attribute categories

The attribute system breaks into nine functional families. Each family has its own carrier policy, its own set of readers, and its own per-stage rewrite rules.

| Family | Representative attributes | Carrier | Primary readers |
|---|---|---|---|
| Launch shape | `nvvm.reqntid`, `nvvm.maxntid`, `nvvm.cluster_dim`, `nvvm.maxclusterrank`, `nvvm.minctasm`, `nvvm.maxnreg`, `nvvm.blocksareclusters`, `nvvm.explicitcluster`, `nvvm.grid_constant` | Function-level dictionary | Kernel-directive emitter, NVVM IR verifier |
| Compute capability | `nv_tileaa.compute_capability`, `nv_tileaa.target_spec`, `nv_tileas.compute_capability`, `nvvm.target` | Module-level dictionary; `nvvm.target` is a type-storage parameter on a type-encoded target attribute | Target-attachment pass, SM-gated rewriter guards |
| Kernel spec | `nv_tileaa.kernel_spec`, `nv_tileas.num_warps`, `nv_tileas.workspace_global_offset` | Function-level dictionary | Scheduler, agent-switch builder, function-boundary lowering |
| Fast-math | `fastmath = "contract"`, `fastmath = "nnan"`, `fastmath = "ninf"`, `fastmath = "nsz"`, `fastmath = "arcp"`, `fastmath = "afn"`, `fastmath = "reassoc"` | Op-level inherent attribute on arithmetic and MMA ops | Arith folder, instruction selector, intrinsic-rewrite pattern |
| Memory ordering | `mem_semantic` (relaxed / acquire / release / acq_rel / sc), `mem_scope` (cta / cluster / gpu / sys), `mbar_scope`, `mbar_space` | Op-level inherent attribute on memory ops; later an NVVM properties slot | Memory-op verifier, NVVM dispatcher arm A, LLVM atomic emitter |
| Cache policy | `cache_modifier` (`.ca`/`.cg`/`.cs`/`.cv`), `eviction_policy`, `l2_prefetch`, `cache_eviction_priority` | Op-level inherent attribute | Memory-op selectors, ptxas directive emitter |
| Layout / shape | cuTe layout descriptors, `DenseI32ArrayAttr` tile shape on `partition_view`, `mma_layout`, `wgmma_layout` | Type-storage parameter (layout-on-view), op-level attribute (layout-on-MMA) | Layout-assignment pass, atom builders, MMA intrinsic selector |
| Pipeline staging | `pipeline_stage`, `num_stages`, `nv_tileas.persistent`, `tileas.schedule.constraint.*` | Op-level discardable attribute on async-pipeline ops; some live in inherent properties when the op definition reserved a slot | Modulo scheduler, MaterializeAsync, schedule-constraint parser |
| Assumption / debug | `div_by`, `bounded`, `same_elements` (assumption predicates on `cuda_tile.assume`); `di_loc`, `di_compile_unit`, `di_file`, `di_lexical_block`, `di_subprogram` (debug info) | Op-level attribute | Optimizer, debug-info translator |

The kernel-spec family is the central pivot. Frontend hints land as kernel-spec fields, the scheduler reads kernel-spec fields, function-boundary lowering reads kernel-spec fields and writes `nvvm.*` attributes from them, the PTX emitter walks the `nvvm.*` set in a fixed order — every interesting per-kernel decision passes through the kernel-spec at least once.

## Per-stage attribute rules

Each lowering stage has a published rule for each attribute family. The table below is the policy matrix every conversion pattern must respect: which attributes the stage is allowed to drop, which it must preserve (renamed under the new dialect's prefix), which it must synthesize from inferred or read facts, and which it reads to drive its own rewriting decisions without modifying.

| Stage | Drops | Preserves and renames | Synthesizes | Reads to act |
|---|---|---|---|---|
| Frontend → `cuda_tile` (bytecode input) | (none) | (none) | (none) | (none — this is the input contract) |
| `ConvertCudaTileToTileAA` | (none) | All op-attribute dictionaries flow through the `TypeConverter` and emerge on the rewritten `nv_tileaa` ops; `fastmath` carries verbatim on arithmetic ops | (none) | Compute capability from the pass option to specialise type conversion |
| `ConvertTileAAToTileAS` | (none at this stage; downstream passes drop intermediate analysis attrs) | Per-op CopyAtom and ReduceAtom witnesses ride verbatim onto the new `nv_tileas` ops; layout attributes carry through | `nv_tileaa.kernel_spec` on the function from frontend hints; SM-gated rewrites consult it through the attached attribute | `nv_tileaa.compute_capability` for SM100 block-scaled MMA admission |
| TileAS scheduling and layout (D07-D22) | Scheduler-internal intermediate attrs after `MaterializeSchedule` consumes them | `pipeline_stage`, `nv_tileas.num_warps`, schedule-constraint attrs survive into materialization | `pipeline_stage` integer on each producer/consumer region, `nv_tileas.num_warps` mirror on agent-switch ops, `agent_strides` array on `agent_switch` | `kernel_spec.num_warps`, `kernel_spec.num_ctas`, schedule-constraint attrs |
| `ConvertTileFuncToLLVM` | `nv_tileaa.kernel_spec` field-by-field (the function-level dictionary entry stays; its readers move) | `nv_tileaa.compute_capability`, `nv_tileaa.target_spec`; `nv_tileaa.grid_constant` argument attributes are renamed and migrated onto the LLVM-typed arguments by the downstream `CuteKernelToNvvmRewrite` pass | `nvvm.reqntid` from `32 * numWarps`; `nvvm.cluster_dim` when `targetSM > 89 && clusterProduct > 1`; `nvvm.blocksareclusters` under the same predicate; `nvvm.minctasm = 1`; `nvvm.maxnreg` from per-SM occupancy table when `nv_tileaa.occupancy` is set; `cute.kernel` unit marker (renamed to `nvvm.kernel` only in the downstream pass) | `nv_tileaa.kernel_spec` field accessors |
| `ConvertTileASToLLVM` body conversion | Async-token operand types collapse to `i32` carriers; some carrier-only attrs disappear with their ops | `nvvm.*` properties attributes on lowered ops survive into the NVVM dispatcher slots described in [Properties Blob and Attr Parsers](../dialects/nvvm/properties-blob-and-attr-parsers.md) | NVVM properties slots from the lowered op's MLIR attribute dictionary; `mem_semantic` becomes a Pattern-A enum slot at `+64`, `mem_scope` becomes a Pattern-A enum slot at `+72` | `cute.kernel` marker, CopyAtom and ReduceAtom witnesses |
| Companion `cute*`-to-LLVM lowering | CuTe-internal layout-algebra attributes after descriptor materialization | Tile-shape attributes survive into the emitted descriptor constants | TMA descriptor constants from cuTe layout attributes | Layout attributes, `compute_capability` for atom selection |
| `ConvertNVGPUAndGPUToNVVM` | `gpu.kernel` after rewriting to `nvvm.kernel` | `nvvm.*` family unchanged | (none beyond what the rewrite emits) | `gpu.kernel`, `gpu.module` target attribute |
| `AttachNVVMTarget` | (none) | Compute-capability and target-spec data folded into `#nvvm.target` | `#nvvm.target` attribute on the `gpu.module` with chip, features, link-files, flags | `nv_tileaa.compute_capability`, `nv_tileaa.target_spec` |
| MLIR-to-LLVM translation | The `nvvm.*` markers that have no LLVM-IR counterpart (e.g. `nvvm.kernel` is emitted as a calling-convention attribute, not as a metadata node) | All function-level `nvvm.*` directive carriers become LLVM function attributes named `nvvm-reqntid`, `nvvm-cluster-dim`, etc., or NVVM annotation tuples on the legacy path | LLVM function attributes; debug-info intrinsics | All carrier-only `nvvm.*` attributes |
| NVPTX MIR | Most function-level attributes outside the directive-bearing ones | `nvvm-reqntid`, `nvvm-cluster-dim`, `nvvm-maxnreg`, `nvvm-minctasm`, `nvvm-grid-constant`, `nvvm-maxclusterrank`, `nvvm-blocksareclusters` carry through as function attributes the AsmPrinter reads | `NVPTXISD` pseudo-opcodes for grid-constant arguments and TMA descriptor materialization | `nvvm.kernel` (entry vs func split), per-arg `nvvm.grid_constant` |
| AsmPrinter (MIR → PTX) | (none at emission time) | (none — this is the projection step) | PTX directives: `.entry`, `.maxntid`, `.reqntid`, `.minnctapersm`, `.maxnreg`, `.explicitcluster`, `.reqnctapercluster`, `.maxclusterrank`, `.blocksareclusters` | Every directive-bearing function attribute |

The two stages that synthesize the most are `ConvertTileFuncToLLVM` and `AttachNVVMTarget`. Function-boundary conversion is where frontend hints, scheduler analysis, and kernel-spec fields collapse into the small set of `nvvm.*` attributes the AsmPrinter will eventually project to PTX. Target attachment is where the per-module `compute_capability` and `target_spec` strings become the single resolved `#nvvm.target` attribute that drives every SM-gated decision downstream.

## Intentional drops and silent miscompiles

Not every attribute drop is a bug. The pipeline deliberately drops attributes once their consumer has read them, and the carrier serves no purpose after that point. Distinguishing intentional drops from accidental drops is the central correctness concern for any reimplementation.

Intentional drops:

- Scheduler-internal intermediate attributes are dropped after `MaterializeSchedule` consumes them. They exist only to communicate analysis state from one scheduler subpass to the next, and they would clutter the IR if left behind. The drop is correct because no downstream pass reads them.
- `fastmath` attributes on an op's output value disappear when the op is rewritten as an intrinsic that re-encodes the same flags. The intrinsic's argument list carries the flags forward (typically as a `fastmathflags` LLVM operand bundle), so the original attribute carrier is redundant.
- `cute.kernel` is renamed to `nvvm.kernel` by the downstream `CuteKernelToNvvmRewrite` pass; the original marker disappears once the rename runs. The two-step rename exists because the rewriter also lifts `cute_nvgpu.grid_constant` argument attributes to `nvvm.grid_constant`, and that lift needs the LLVM-typed function arguments the function-boundary pass has just produced.
- Per-op `mem_semantic` and `mem_scope` op-attribute entries fold into NVVM properties slots during `ConvertTileASToLLVM`. The op-attribute carrier vanishes, but the value survives at a positional slot the NVVM dispatcher reads.

Silent-miscompile drops to avoid:

- Dropping `mem_semantic` on a memory op during lowering produces a load or store with weaker ordering than the source requested. The NVVM dispatcher picks the relaxed-ordering arm by default, and the resulting PTX validates cleanly under ptxas — there is no diagnostic to surface the missing fact.
- Dropping `mem_scope` on a cluster-scope atomic produces a CTA-scope atomic on Hopper hardware. The two opcodes both exist and both pass the NVVM IR verifier; the cluster invariant is not checked.
- Dropping `nv_tileaa.compute_capability` before `AttachNVVMTarget` runs produces a `#nvvm.target` attribute with the default chip, not the requested one. The NVVM IR verifier accepts the target because the chip string is legal; the cubin compiles for the wrong SM and runs in degraded mode (or crashes on unsupported instructions).
- Dropping `nv_tileaa.kernel_spec` before function-boundary conversion produces a kernel without launch-bound directives. The function compiles as a `.func` instead of a `.entry`, and the resulting cubin exposes no kernel for the driver to launch.
- Dropping `nvvm.grid_constant` on a TMA descriptor argument produces a kernel that copies the descriptor through parameter memory on every launch instead of materializing it once. ptxas accepts the result; the kernel runs but at degraded performance.
- Dropping `fastmath` on an `mmaf` op that the frontend marked `contract` produces an MMA emission that refuses fused-multiply-add formation. The PTX is correct under IEEE-754, slower than the user requested, and the diagnostic surface is empty.

A reimplementation should treat any attribute drop that is not on the intentional list as a candidate bug. The pass-level verifier catches structural mismatches but does not see semantic drops; the NVVM IR verifier sees structural target-violation but does not see semantic miscompiles. The attribute-drop policy is the producer-side discipline that fills that gap.

## NVVM properties blob

The NVVM properties blob is the dialect-specific compact carrier that sits below the standard MLIR attribute-dictionary surface. Every `nvvm.*` op that carries inline-data attributes gets a uniform Properties record bump-allocated next to its `Operation*` header, with attribute slots starting at byte `+64` and striding 8 bytes apart. The five access patterns (A through E) cover every per-op attribute family in the dialect — enum payloads, optional enums, unit attributes, integer attributes, and array attributes.

The blob is positional, not keyed. A slot's meaning is fixed by the dispatcher arm for that op mnemonic; a reimplementation that gets the slot ordering wrong reads the wrong attribute even when the data is present. The full slot tables for each op family, plus the 67-element enum-attr registrar chain that backs the parsers, are documented in [Properties Blob and Attr Parsers](../dialects/nvvm/properties-blob-and-attr-parsers.md).

Three properties of the blob matter for the cross-stage attribute system. First, the blob is the terminal carrier for memory-ordering and cache-modifier attributes; once an op reaches the NVVM dispatcher, its op-attribute dictionary has been collapsed into the slot table. Second, the blob is inherent storage, not discardable, so cloning an op preserves the slot values verbatim. Third, the slot ordering is the canonical reference for how the lowering pass maps op-attribute keys to NVVM Properties positions — getting the ordering right is exactly the constraint that a hand-written pattern set must satisfy.

## Cross-references

Per-stage attribute movement is documented in [Lowering Overview](../lowering/overview.md), [cuda_tile to nv_tileaa](../lowering/cuda-tile-to-tileaa.md), [nv_tileaa to nv_tileas](../lowering/tileaa-to-tileas.md), and [nv_tileas to LLVM](../lowering/tileas-to-llvm.md). [Host Launch and ptxas Knobs](../driver/host-launch-and-ptxas-knobs.md) documents the launch-shape directive emitter and the per-directive policy. [Properties Blob and Attr Parsers](../dialects/nvvm/properties-blob-and-attr-parsers.md) documents the NVVM properties carrier in detail. [cuda_tile Types and Attrs](../dialects/cuda_tile/types-and-attrs.md), [nv_tileaa Types, Attrs, Verifiers](../dialects/nv_tileaa/types-attrs-verifiers.md), and [nv_tileas Types](../dialects/nv_tileas/types.md) document the per-dialect parse contract and verifier for each attribute. [Schedule Constraint Attributes](../scheduler/schedule-constraint-attributes.md) covers the nine scheduler-constraint attribute strings the modulo scheduler reads. [GPU Execution Model](gpu-execution-model.md) is the canonical reference for the launch-shape directives at runtime. [DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) walks a representative kernel through every stage and shows the attribute movement in context.
