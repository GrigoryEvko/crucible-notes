# Neuron Dialect Registry and Extension Ops

> *All addresses on this page apply to `hlo2penguin` from neuronx-cc 2.24.5133.0+58f8de22 (cp310). `.rodata` VA = file offset; `.text` VA = file offset − 0x1000. Other builds will differ.*

## Abstract

`hlo2penguin` is the front-half MLIR driver of the Neuron compiler: it imports an XLA module (MHLO / StableHLO), runs the `hilo` pass pipeline ([4.32](hlo2penguin-mlir-pipeline.md)), and re-emits textual Python targeting `neuronxcc.starfish.penguin.ir`. A reimplementer's first instinct is to look for a **Neuron MLIR dialect** — a `neuron::NeuronDialect` registering `neuron.*` ops, types, and attributes. **That dialect does not exist.** `hlo2penguin` defines zero custom dialect ops, types, attrs, or interfaces. Every `hilo::*` class in RTTI is a `mlir::PassWrapper<…>` or a `RewritePattern`; `hilo` is a C++ pass *namespace* ("HIgh-Level-Optimizer"), never a dialect.

The MLIRContext eagerly loads exactly **three** upstream dialects — `mhlo`, `stablehlo`, `func` — in `hilo::initializeMLIRContext` @0x1ee0500. The remaining ~30 dialects in the statically-linked roster (chlo, tensor, arith, quant, shape, sdy, plus a dead XLA-GPU cluster of gpu/NVVM/ROCDL/linalg/vector/scf/affine) arrive lazily as dependent dialects of the importer and conversion passes, or are linked but never reached on the Trainium path. The genuine "Neuron op set" is therefore **data, not C++ op classes**: a vocabulary of 26 `AwsNeuron*` `custom_call_target` strings carried on upstream `{mhlo,stablehlo}.custom_call` ops, a `FusionKind`/`CompositeKind` string-attr vocabulary, and 9 `neuron.*` discardable/inherent attributes.

This page documents the registry entry point, enumerates the real Neuron extension vocabulary, and delivers the provenance verdict for the eight `tensor.g2s.*` strings — which look like Neuron tile/im2col ops but are LLVM/NVPTX intrinsic-rename fragments from the linked-but-dead GPU backend.

For reimplementation, the contract is:

- The eager-load entry point: which dialects load up-front, in what order, and which are lazy/dependent.
- The Neuron extension vocabulary as **strings on upstream ops**, not as a custom dialect — and the parser/verifier that reads them.
- The g2s provenance rule: how to tell a Neuron extension op from a linked-in GPU intrinsic fragment that shares the `tensor.` lexeme.

| | |
|---|---|
| **Registry entry point** | `hilo::initializeMLIRContext(mlir::MLIRContext&)` @0x1ee0500 (`.cold` clone @0x1ee04d3) |
| **Called from** | `main` @0x1edc8e7 (D-A01 `main` @0x1edc880) |
| **Eager-loaded dialects** | 3 — `mhlo` (1st), `stablehlo` (2nd), `func` (3rd) |
| **Linked dialect roster** | 33 distinct `*Dialect` typeinfo classes (RTTI); none Neuron-authored |
| **Neuron op set** | 26 `AwsNeuron*` `custom_call_target` strings (data on upstream `custom_call`) |
| **Neuron attr set** | 9 `neuron.*` attributes (StringAttr/ArrayAttr, no custom storage class) |
| **g2s verdict** | LLVM/NVPTX `cp.async.bulk.tensor.g2s.*` intrinsic-upgrade fragments — **NOT Neuron** |

---

## The Dialect Registry

### Purpose

`initializeMLIRContext` is the one function that decides what IR `hlo2penguin` can parse before any pass runs. It is the analog of an LLVM `PassRegistry` plus a `DialectRegistry`, collapsed into three eager `getOrLoadDialect` calls. Anything not loaded here is loaded later as a *dependent dialect* declared by a conversion pass, exactly as upstream MLIR does it.

### Entry Point

```text
main @0x1edc880
  └─ initializeMLIRContext(ctx) @0x1ee0500          ── eager dialect load
       ├─ ctx.getOrLoadDialect<mhlo::MhloDialect>()         @0x1ee053e  (1st)
       ├─ ctx.getOrLoadDialect<stablehlo::StablehloDialect>() @0x1ee0574 (2nd)
       └─ ctx.getOrLoadDialect<func::FuncDialect>()         @0x1ee0599  (3rd)
```

### Algorithm

```c
function initializeMLIRContext(MLIRContext& ctx):     // hilo::initializeMLIRContext @0x1ee0500
    // 69-line body; EXACTLY three getOrLoadDialect call-sites, no loop,
    // no registerAllDialects, no appendDialectRegistry.
    ctx.getOrLoadDialect<mhlo::MhloDialect>()         // @0x1ee053e — legacy HLO IR
    ctx.getOrLoadDialect<stablehlo::StablehloDialect>() // @0x1ee0574 — newer HLO IR
    ctx.getOrLoadDialect<func::FuncDialect>()         // @0x1ee0599 — func/return/call container
    // lazy TypeID guard-vars + FallbackTypeIDResolver::registerImplicitTypeID
    // for mhlo/stablehlo, guarded once each.
    return
```

Each `getOrLoadDialect<X>` is the templated MLIR call `MLIRContext::getOrLoadDialect(StringRef, TypeID, function_ref<unique_ptr<Dialect>()>)`; the per-dialect constructing lambda is present in `names.json` as `getOrLoadDialect<mlir::mhlo::MhloDialect>(void)::{lambda(void)#1}` (and the `stablehlo`/`func` twins). These three lambdas are the only eager-load evidence; everything else is pulled in transitively.

> **GOTCHA —** there is no flat "registered dialect list" to drive a reimplementation off of. A prior pass (S2-03 §2) presented a flat 12-dialect "registered" list with no load-order distinction. **CORRECTION (D-C21) —** `initializeMLIRContext` eagerly loads exactly 3 (`mhlo`→`stablehlo`→`func`, in that disasm order); the rest arrive lazily. A reimplementer that eager-loads all 30 will load a dead GPU cluster that never executes on Trainium.

### Dialect Roster

The linked binary carries 33 distinct `*Dialect` typeinfo classes (RTTI: typeinfo + typeinfo-name + vtable triples). They split into three load tiers. The table describes the *axes* of that roster, not 33 rows of upstream MLIR trivia.

| Load tier | Dialects | Origin | Neuron-authored? |
|---|---|---|---|
| **Eager** (in `initializeMLIRContext`) | `mhlo`, `stablehlo`, `func` | upstream MHLO / StableHLO / MLIR | NO |
| **Transitive / dependent** (declared by importer + conversion passes) | `chlo`, `tensor`, `arith`, `quant`, `shape`, `sdy` (Shardy), `cf`, `builtin` | upstream MLIR / XLA | NO |
| **Linked-but-dead** (statically linked XLA-GPU/CPU cluster, off the Trainium path) | `gpu`, `NVVM`, `ROCDL`, `linalg`, `vector`, `memref`, `math`, `scf`, `affine`, `LLVM`, `pdl`/`pdl_interp`, `ub`, `complex`, `DLTI`, `sparse_tensor`, `xla`/`xla::cpu`/`xla::gpu` | upstream MLIR + XLA codegen | NO |

> **NOTE —** RTTI confirms **33** distinct `*Dialect` typeinfo classes in this build; an earlier pass cited 31. Re-grounded count is 33 (`rg -o '[A-Za-z_]*DialectE' rtti.json | sort -u`). The discrepancy is two additional upstream dialects, none Neuron-authored — the verdict (zero Neuron dialects) is unchanged. **CONFIRMED.**

> **QUIRK —** the `gpu`/`NVVM`/`ROCDL`/`xla::gpu`/`linalg`/`vector`/`scf` cluster is the statically-linked XLA-GPU MLIR pipeline. ~10,000 `nvvm|NVVM|XlaGpu|xla::gpu` functions are in the binary but **off the Trainium code path** — they exist because `hlo2penguin` links the whole XLA MLIR registration TU set. This cluster is the sole origin of the g2s strings (§g2s). There is **no** `hilo::*Dialect`, `neuron::*Dialect`, `tonga::*Dialect`, or `penguin::*Dialect` typeinfo anywhere. `tonga` appears as a namespace *string* only (it is the downstream backend dialect, not loaded here — [4.33](hlo-to-native-kernel-lowering.md)).

---

## The Real Neuron Extension Ops

Because `hilo` registers no dialect ops, the de-facto Neuron op set rides as **data on upstream ops**: `custom_call_target` strings on `{mhlo,stablehlo}.custom_call`, fusion/composite kind strings, and `neuron.*` attributes. A `RewritePattern`-based parser (`NeuronHloVerifier` @0x... string `NeuronHloVerifier`, and the per-target lowering patterns) reads these strings and lowers them.

### The AwsNeuron* custom_call_target Vocabulary (26)

These are the literal `custom_call_target` strings the Neuron front-end recognizes on an upstream `custom_call` op. Each names a fused/native Neuron primitive that a downstream pass lowers to a Penguin/NKI kernel. Verbatim from `strings.json` (`rg -o 'AwsNeuron[A-Za-z0-9]+' | sort -u`, minus the two `ModuleMarker` delimiters):

| Category | Targets |
|---|---|
| **Control / infra** | `AwsNeuronControlDep`, `AwsNeuronExit`, `AwsNeuronZeroSizedOp`, `AwsNeuronDevicePrint`, `AwsNeuronLNCShardingConstraint`, `AwsNeuronTransferWithStaticRing` |
| **Custom kernel** | `AwsNeuronCustomOp`, `AwsNeuronCustomNativeKernel`, `AwsNeuronMLPNKI` |
| **Matmul / collective** | `AwsNeuronCollectiveMatmul`, `AwsNeuronIntMatmult` |
| **Norm** | `AwsNeuronRmsNorm`, `AwsNeuronRmsNormBackward` |
| **Activation** | `AwsNeuronSoftmax`, `AwsNeuronSoftmaxBackward`, `AwsNeuronGelu`, `AwsNeuronGeluApprxTanh`, `AwsNeuronGeluBackward`, `AwsNeuronSilu`, `AwsNeuronSiluBackward`, `AwsNeuronErf` |
| **Reduction / select** | `AwsNeuronArgMax`, `AwsNeuronArgMin`, `AwsNeuronTopK` |
| **Stochastic** | `AwsNeuronDropout`, `AwsNeuronDropoutMaskV1` |

Plus two **phase-delimiter markers** (not lowerable ops): `AwsNeuronModuleMarkerStart-{Forward,Backward}` @0x39dc10 / 0x3cd1d0 and `AwsNeuronModuleMarkerEnd-*`. The static-analysis marker class string `NeuronTensorOp` @0x809294-region and verifier class `NeuronHloVerifier` are also present.

> **NOTE —** the roster grounded here is 28 distinct `AwsNeuron*` strings; subtracting the 2 `ModuleMarker` delimiter families gives the **26** genuine `custom_call_target` ops. **CONFIRMED** (`sort -u` on `strings.json`). Per-target lowering lives in the `hilo::Convert*` / `hilo::Legalize*` rewrite passes ([4.32](hlo2penguin-mlir-pipeline.md)).

### The FusionKind / CompositeKind Vocabulary

Two inherent string-attrs carry fusion intent:

| Attr key | Addr | Carrier op | Values (sample) |
|---|---|---|---|
| `FusionKind` | 0x266931 | `mhlo.fusion` (inherent) | `DotLogistic`, `MulRedSqrt`, `ScheduleFusion`, `Elementwise`, `Expm1`, `Log1p`, `DotSoftmax` |
| `CompositeKind` | 0x21a28c | `stablehlo.composite` (Neuron inherent) | + default `FusedComposite`; `ResizeNearest`, `ResizeBilinear` |

`CompositeKind` @0x21a28c is a **Neuron-added** inherent attr on `stablehlo.composite`, distinct from upstream `composite.name` / `composite.version` / `composite.attributes`. Default composite name literal `composite.Fused` is also present.

### The neuron.* Attribute Set (9)

Discardable/inherent attributes that ride on upstream ops. There is **no** custom Attr storage class (`hilo::*Attr` RTTI = empty), so these are plain `StringAttr`/`ArrayAttr`:

| Attr | Role |
|---|---|
| `neuron.symName` | output-name `ArrayAttr<SymbolRef>` |
| `neuron.actual_shape` | zero-rewrite / 0-sized real shape |
| `neuron.CrossPassTensor` | tensor live across pass boundaries |
| `neuron.non_local` | non-local placement marker |
| `neuron.transposable` | transpose-eligibility hint |
| `neuron.groupID` | fusion / sharding group id |
| `neuron.remat.value` | rematerialization value marker |
| `neuron.no_opt` | opt-barrier marker |
| `neuron.readthedocs` | (doc-link string; non-IR) |

Plus module/op string attrs `has_control_deps` and `stream_id`.

> **QUIRK —** every "Neuron op" is upstream-op + string-attr, parsed by a `RewritePattern`. RTTI query `hilo::*(Op|Type|Attr|Dialect|Interface)` returns the **empty set** — `rg -o '4hilo[0-9]+[A-Za-z]*(Op|Type|Attr|Dialect|Interface)E' rtti.json` matches nothing across 233 `hilo`-prefixed RTTI records. Every `hilo::*` class is a `PassWrapper` (`NeuronOpFusion`, `NeuronInstCombine`, `ScheduleFusion`, `FusionToComposite`, `LegalizeAlias/Scatter/SRA`, `ConvertCustomCallToAllReducePass`, …) or a `RewritePattern` (`SimplifyReduceSliceReducePattern`, `CollectiveBroadcastToAllGatherPattern`). **CONFIRMED.**

---

## g2s Mnemonic Provenance Verdict

The eight strings `tensor.g2s.tile.{1d..5d}` and `tensor.g2s.im2col.{3d,4d,5d}` look exactly like a Neuron tile/im2col extension op family. **They are not.** They are LLVM/NVPTX intrinsic-AutoUpgrade name fragments — not Neuron ops, not MLIR ops of any dialect.

### The Two String Families

Each g2s mnemonic exists as two separately-deduplicated `.rodata` literals:

| Family | Form | `.rodata` | Referenced by |
|---|---|---|---|
| **BARE** | `tensor.g2s.tile.1d` … `tensor.g2s.im2col.5d` | low (0x21ccf2 … 0x281b02) | `upgradeIntrinsicFunction1` only, refs:1 each |
| **FULL** | `llvm.nvvm.cp.async.bulk.tensor.g2s.tile.1d` … | high (0xcb0f79 … 0xcb10ac) | none, refs:0 — `IntrinsicNameTable` data read by `Intrinsic::getName` |

The bare string `tensor.g2s.tile.1d` @0x228f6d is referenced from exactly one site: `0x8495e3a`, inside `upgradeIntrinsicFunction1(llvm::Function*, llvm::Function*&, bool)` (mangled `_ZL25upgradeIntrinsicFunction1PN4llvm8FunctionERS1_b`, @0x8493f00). The full string `llvm.nvvm.cp.async.bulk.tensor.g2s.tile.1d` @0xcb1000 has zero code refs — it is LLVM intrinsic-name-table data.

### The Verdict

```c
// inside upgradeIntrinsicFunction1 @0x8493f00 (LLVM AutoUpgrade.cpp::UpgradeIntrinsicFunction1)
function upgradeIntrinsicFunction1(F, NewFn, CanUpgradeDebugIntrinsicsToRecords):
    name = F->getName()                      // @0x8493f2c
    name.starts_with("llvm.nvvm.")           // earlier StringRef::starts_with strips prefix
    name.starts_with("cp.async.bulk.")       // strips TMA prefix → residual suffix
    // length-then-memcmp string-switch over NVVM intrinsic suffixes:
    if memcmp(suffix, "tensor.g2s.im2col.3d", 0x14) == 0: …   // @0x8495d9a
    if memcmp(suffix, "tensor.g2s.im2col.4d", 0x14) == 0: …   // @0x8495dcd
    if memcmp(suffix, "tensor.g2s.tile.1d", …) == 0: …        // xref 0x8495e3a
    // SIBLING comparands in the SAME switch (unmistakably LLVM/NVVM auto-upgrade):
    //   "shared.cta.to.cluster", "cttz.", "ctlz.", "fabs.", "add.f64.p",
    //   "inc.32.p", "dec.32.p", "atomic.", "sha256sig0", ".old"
```

The bare g2s strings are the `memcmp` comparands inside `upgradeIntrinsicFunction1`'s NVVM string-switch — the deprecated-intrinsic **renamer**, not a verifier or a Neuron parser. Their siblings (`cttz.`, `fabs.`, `atomic.`, `.old`, …) are the verbatim body of LLVM `llvm/lib/IR/AutoUpgrade.cpp::UpgradeIntrinsicFunction1`.

| Mnemonic | Verdict | Evidence (CONFIRMED) |
|---|---|---|
| `tensor.g2s.tile.1d` | LLVM/NVPTX intrinsic-upgrade fragment | bare @0x228f6d, ref-from 0x8495e3a; full nvvm @0xcb1000 |
| `tensor.g2s.tile.{2d..5d}` | LLVM/NVPTX | bare @0x281b02/0x22ce76/0x26cfe1/0x21ccf2; full @0xcb102b… |
| `tensor.g2s.im2col.3d` | LLVM/NVPTX | bare @0x279889, memcmp @0x8495d9a (len 0x14); full @0xcb0f79 |
| `tensor.g2s.im2col.{4d,5d}` | LLVM/NVPTX | bare @0x26cff4/0x23cfcb, memcmp @0x8495dcd/0x8495e00 |

These are Hopper TMA (`cp.async.bulk.tensor`) global-to-shared (**g2s**) tile/im2col-mode intrinsics from the statically-linked LLVM NVPTX backend (dependency of the linked XLA-GPU code), targeting NVIDIA SM90+. The `g2s` prefix = **global-to-shared**, the TMA copy direction in NVVM's `cp.async.bulk.tensor.g2s.*` intrinsic family. They are never read by any mhlo/stablehlo/tensor op-parser, never emitted by any `hilo` pass, and never reach the Trainium/Penguin path. **There is no Neuron `tensor.g2s.*` extension op.** Provenance **CONFIRMED**.

> **CORRECTION (D-C21) —** S2-03 §2 listed `tensor.g2s.tile.{1d..5d}` / `tensor.g2s.im2col.{3d,4d,5d}` as "Neuron-extension tile/im2col ops." They are LLVM/NVPTX intrinsic-rename fragments referenced only by `upgradeIntrinsicFunction1` @0x8493f00. The real Trainium conv-as-matmul / im2col / tiling lowering lives **downstream** in the Penguin Python / Tensorizer backend, not in `hlo2penguin`'s MLIR layer (`hlo2penguin`'s terminal output is textual Python targeting `neuronxcc.starfish.penguin.ir`).

> **GOTCHA —** the `tensor.` lexeme is a coincidence. The upstream `mlir::tensor` dialect (`tensor.empty`, `tensor.reshape`, `tensor.extract_slice`) is real and loaded transitively, but is unrelated to the g2s strings — the g2s strings merely contain the substring `tensor.g2s` because NVVM's `cp.async.bulk.tensor.g2s.*` does. A reimplementer must not register g2s ops in a `tensor`-like Neuron dialect.

---

## Adversarial Self-Verify

Five strongest claims, re-challenged against the binary:

1. **"`initializeMLIRContext` eager-loads exactly 3 dialects."** — `names.json` has exactly three `getOrLoadDialect<X>::{lambda(void)#1}` instantiations: `Mhlo`, `Stablehlo`, `Func`. No fourth eager lambda. **CONFIRMED.**
2. **"No Neuron dialect/op/type/attr exists."** — `rg -o '4hilo[0-9]+[A-Za-z]*(Op|Type|Attr|Dialect|Interface)E' rtti.json` = empty; 233 `hilo` RTTI records are all Pass/Pattern. **CONFIRMED.**
3. **"g2s strings referenced only by `upgradeIntrinsicFunction1`."** — `strings.json` record for `tensor.g2s.tile.1d` has `referenced_by:[{from:0x8495e3a, func:_ZL25upgradeIntrinsicFunction1…}]`, refs:1; full nvvm @0xcb1000 refs:0. **CONFIRMED.**
4. **"26 AwsNeuron custom_call targets."** — `sort -u` gives 28 distinct `AwsNeuron*` strings; minus 2 `ModuleMarker` delimiter families = 26 targets. **CONFIRMED** (count re-derived, not copied).
5. **"33 `*Dialect` typeinfo classes."** — `rg -o '[A-Za-z_]*DialectE' rtti.json | sort -u | wc -l` = 33. Backing said 31; **CORRECTED to 33** here (two extra upstream dialects). The Neuron-dialect count (zero) is unchanged. **CONFIRMED / corrected.**

No fabricated anchors. The non-cold `initializeMLIRContext` ea is `null` in `functions.json` (only the `.cold` clone @0x1ee04d3 resolves cleanly); the 0x1ee0500 entry and the three `getOrLoadDialect` call-site addresses (0x1ee053e/0x1ee0574/0x1ee0599) are taken from the backing disasm and are **STRONG** (not independently re-disassembled here).

---

## Related Components

| Name | Relationship |
|---|---|
| `mlir::MLIRContext::getOrLoadDialect` | the upstream MLIR call the registry uses; eager-load primitive |
| `upgradeIntrinsicFunction1` @0x8493f00 | LLVM AutoUpgrade renamer; sole referrer of the bare g2s strings |
| `NeuronHloVerifier` | RTTI verifier class that reads the `AwsNeuron*` / `neuron.*` vocabulary |
| Penguin Python / Tensorizer (Strand U) | downstream home of the real tiling/im2col/conv→matmul lowering |

## Cross-References

- [hlo2penguin MLIR Pipeline Order & Entry Flow](hlo2penguin-mlir-pipeline.md) — 4.32, the `hilo` pass sequence that consumes this vocabulary
- [HLO → Native / NKI Kernel Lowering](hlo-to-native-kernel-lowering.md) — 4.33, how the `AwsNeuron*` custom-call vocabulary lowers to native/NKI kernels
