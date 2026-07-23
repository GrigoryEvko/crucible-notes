# The hlo-opt Pass Registry

> *All addresses on this page apply to the `hlo-opt` standalone ELF shipped in **neuronx_cc 2.24.5133.0+58f8de22** (cp310 wheel: `neuronxcc/starfish/bin/hlo-opt`, BuildID xxHash `93dd8bd9bd4c697b`, **not stripped**). The cp311/cp312 builds carry the same registry; addresses are bit-identical across the three CPython variants of this version. Other versions will differ.*

## Abstract

`hlo-opt` is the Neuron compiler's standalone HLO optimizer — the XLA-side analogue of LLVM's `opt`. It reads an HLO module, runs a name-selected subset of the HLO-level rewrite/legalization passes, and writes the transformed module back out. Like `opt`, every pass it can run lives in a single global registry keyed by a short string name, and the command line selects from that registry by name. This page documents that registry: the one function that fills it (`xla::hilo::RegisterHiloHloPasses`), the `llvm::StringMap` it fills, the **112 passes** it registers, and the stock-XLA `DebugOptions` flag mechanism that turns a `--passes` / `--skip-pass` name list into the actual run-list.

The shape will be familiar to anyone who has read LLVM's `PassRegistry` or XLA's `HloPassPipeline`: a managed-static container, a registration function that runs once at startup, and a name → factory binding so the driver can instantiate a pass by string. The Neuron specifics are (a) the registration is one flat 8.9 KB function of 112 near-identical blocks rather than per-pass static initializers, (b) the registry value is a `std::function` factory (not a singleton pass), and (c) the key for each pass is *not* a literal in the registration code — it is whatever the constructed pass's `name()` virtual returns, read back at registration time. That last point is the central gotcha and the reason the 112 keys had to be recovered from each pass's vtable rather than from the registrar.

This is the index page for Part 4: every other 4.x page documents one or more passes that appear as a row in the table below. The table is the canonical name→class→entry map; the rest of the page explains how a row is bound and how `--passes`/`--skip-pass` consume it.

For reimplementation, the contract is:

- **The registry container and its key derivation** — an `llvm::StringMap<std::function<unique_ptr<HloPassInterface>()>>` whose key for each entry is the C-string returned by `pass->name()` (vtable slot vptr+0x10), read at registration time, not a literal in the registrar.
- **The registration loop** — 112 identical "build a `std::function` factory, call `RegisterHloPass`" blocks, executed once from a managed-static guard.
- **The selection algorithm** — the stock-XLA `xla_enable_hlo_passes_only` / `xla_disable_hlo_passes` / `xla_disable_all_hlo_passes` `DebugOptions` triad, exact-name matched against the registry keys, plumbed as a `flat_hash_set<string_view>` into `HloPassPipeline::RunPassesInternal`.
- **The Neuron-vs-stock split** — which classes are `xla::hilo::Neuron*` (authored for this compiler) versus stock `xla::` passes reused unchanged.

| | |
|---|---|
| **Registrar** | `xla::hilo::RegisterHiloHloPasses()` @ `0x1e72270` (weak, 8895 B) |
| **Key-binding helper** | `xla::hilo::RegisterHloPass(const std::function<…>&)` @ `0x1ebc3f0` (382 B) |
| **Registry accessor** | `xla::hilo::GetHloPassRegistry()` @ `0x1ebc570` |
| **Registry storage** | `xla::hilo::hloPassRegistry` @ `0x9a38ee0` (`.bss`, managed-static) |
| **Container type** | `llvm::StringMap<std::function<std::unique_ptr<xla::HloPassInterface>()>>` |
| **Pass count** | **112** (= 112 `RegisterHloPass` call sites in the registrar) |
| **Key for each pass** | C-string from `pass->name()` (vtable vptr+0x10) |
| **Selection flags** | `xla_enable_hlo_passes_only` (=`--passes`), `xla_disable_hlo_passes` (=`--skip-pass`), `xla_disable_all_hlo_passes` |
| **Pipeline driver** | `xla::HloPassPipeline::RunPassesInternal<HloModule>` (consumes the disable set as `flat_hash_set<string_view>`) |
| **OpExpander shared Run** | `xla::OpExpanderPass::Run` @ `0x29f0bb0` (25 of 112 passes share it) |

---

## 1. How a Registry Key Is Bound

### Purpose

The registry maps a `--passes` name to a factory that constructs the pass. The subtle part is where the name comes from: the registrar never writes the name string. It builds a factory, hands it to `RegisterHloPass`, and `RegisterHloPass` *constructs a throw-away instance just to ask it its name*, then uses that name as the map key.

### Entry Point

```text
_GLOBAL__sub_I_… (managed-static guard)
  └─ xla::hilo::RegisterHiloHloPasses (0x1e72270, 8895 B)   ── 112 identical blocks
       └─ ×112  xla::hilo::RegisterHloPass (0x1ebc3f0, 382 B)  ── binds one key
            ├─ call (*factory)()              ── construct temp pass
            ├─ call [vptr+0x10] == pass->name()  ── StringRef {data,len}
            ├─ llvm::StringMapImpl::hash(StringRef)
            └─ llvm::StringMapImpl::LookupBucketFor(StringRef) ── insert into hloPassRegistry
```

### Algorithm

Each of the 112 blocks in the registrar is the same shape — assemble a `std::function` factory on the stack, call `RegisterHloPass`, then destroy the temporary:

```c
// one of 112 blocks inside RegisterHiloHloPasses (0x1e72270)
function RegisterOneBlock():
    var_28 = &Register<Pass>_M_invoke;     // the factory lambda (_M_invoke ptr)
    var_30 = &Register<Pass>_M_manager;    // std::function manager (copy/destroy)
    *(__m128*)&var_40 = 0;                 // zero the std::function inline buffer
    RegisterHloPass(/* &std::function */ &var_40);   // -> 0x1ebc3f0
    if (var_30) (*var_30)(&var_40, DESTROY);         // tear down the temporary
```

`RegisterHloPass` does the real work — it derives the key from the constructed pass and inserts the factory:

```c
function RegisterHloPass(factory):                  // 0x1ebc3f0
    pass = (*factory.invoke)();                      // 0x1ebc416 — construct temp pass
    vptr = *(void**)pass;
    name = ((StringRef(*)(void*))vptr[2])(pass);     // 0x1ebc437 — call [vptr+0x10] = pass->name()
                                                     //   returns StringRef{ rax=data, rdx=len }
    h      = llvm::StringMapImpl::hash(name);         // 0x1ebc446
    bucket = registry.LookupBucketFor(name, h);       // 0x1ebc456 -> hloPassRegistry @ 0x9a38ee0
    bucket.value = factory;                           // store the std::function factory
    // (temp pass destroyed on return)
```

> **QUIRK —** the registry key is *not* visible in the registrar. The block at `0x1e72270` only stores a factory `_M_invoke` pointer; the human-readable `--passes` key is produced later, when `RegisterHloPass` calls `pass->name()` on a freshly built instance. To recover the 112 keys you must follow each factory to its `<Class>` ctor, find the class vtable, read the `name()` function at **vptr+0x10**, and dereference the returned `StringRef`. The registrar symbol name (`RegisterXxx`) is *not* the key either — e.g. `RegisterNeuronAllGatherCombiner` registers the key `all-gather-combiner`.

### The `name()` literal form

Every `<Class>::name()` is the same 11-byte stub returning a `{data,size}` `StringRef` by value in `{edx,eax}`:

```asm
b8 <len32>      mov  eax, <StringRef.size>      ; length
ba <ptr32>      mov  edx, offset <const char[]> ; data pointer
c3              ret
```

`RegisterHloPass` consumes that pair at `0x1ebc437`. All 112 declared lengths equal `strlen(name)` (length integrity check passes for every row). Verified verbatim against the binary for, among others, `dce` (len 3 @ `0x913dc30`), `lower-to-nki-kernels` (len 20 @ `0x1f3f690`), `aws_neuron_default_metadata` (len 27 @ `0x1f964c0`), and the 59-char `aws_neuron_rewrite_all_reduce_dynamic_slice_multiple_groups` (@ `0x2003870`).

> **NOTE —** `hloPassRegistry` (@ `0x9a38ee0`, `.bss`) is a managed static initialized through a `_GLOBAL__sub_I_` guard. It is read once by `GetHloPassRegistry` (@ `0x1ebc570`), which has exactly **one** call site in the ELF — the driver path that resolves a requested name to its factory. `RegisterHiloHloPasses` likewise has exactly one call site, the startup initializer. Both counts come from a call-target scan of the disassembly.

---

## 2. The 112-Row Master Pass Index

This is the canonical table every Part-4 page slots into. Columns:

- **#** — index in registration order (1-based). The `StringMap` is unordered; this order is the registrar's emission order and serves as the default-pipeline proxy (the actual run-list is a driver-supplied name subset — see [§3](#3---passes----skip-pass-selection)).
- **PassName** — the verbatim `name()` C-string = the `--passes`/`--skip-pass` key. Byte-exact.
- **Class** — the constructed class (from the `_ZTV…` vtable demangle).
- **Vtable** — `_ZTV<Class>` address (vptr = this + 0x10).
- **Entry** — primary body: `Run` (vptr+0x18) for own-`Run` passes; for OpExpander passes (shared base `Run` @ `0x29f0bb0`) this is the per-pass `InstructionMatchesPattern` (vptr+0x28), the real per-pass entry.
- **Kind** — `HloPass` (own `Run`) vs `OpExpander` (overrides `InstructionMatchesPattern`/`ExpandInstruction`, shares the base `Run`). **25** of 112 are OpExpander.
- **Src** — `N` = Neuron-authored (`xla::hilo::Neuron*` or a Neuron-only `xla::hilo::*` / `xla::*` class), `S` = stock XLA class reused unchanged. See [§4](#4-stock-vs-neuron-split) for the rule.

All rows below were recovered by the method in [§1](#1-how-a-registry-key-is-bound): the name string and its length are read from the binary and length-validated, and vtable, factory and registration order are disassembly-verified.

| # | PassName | Class | Vtable | Entry | Kind | Src |
|---|---|---|---|---|---|---|
| 1 | `tuple-simplifier` | `xla::TupleSimplifier` | `0x486f60` | `0x2b37160` | HloPass | S |
| 2 | `call-inliner` | `xla::CallInliner` | `0xd26d90` | `0x9135790` | HloPass | S |
| 3 | `dce` | `xla::HloDCE` | `0xd26ec8` | `0x913fb70` | HloPass | S |
| 4 | `stub` | `xla::Stub` | `0x4105a8` | `0x1f68b20` | HloPass | N |
| 5 | `legalize-intrinsics` | `xla::LegalizeIntrinsics` | `0x40d368` | `0x1ef4080` | HloPass | N |
| 6 | `legalize-aws-neuron-arg-max` | `xla::LegalizeAwsNeuronArgMax` | `0x40cef8` | `0x1eecd30` | HloPass | N |
| 7 | `legalize-softmax` | `xla::LegalizeSoftmax` | `0x40e2d0` | `0x1f00120` | HloPass | N |
| 8 | `legalize-topk` | `xla::LegalizeTopK` | `0x40e350` | `0x1f01260` | HloPass | N |
| 9 | `legalize-quantize-mx` | `xla::LegalizeQuantizeMX` | `0x40e168` | `0x1efc4f0` | HloPass | N |
| 10 | `legalize-scaled-matmul` | `xla::LegalizeScaledMatmul` | `0x40e248` | `0x1efe1a0` | HloPass | N |
| 11 | `convert-collectives-to-custom-call` | `xla::ConvertCollectivesToCustomCall` | `0x408ad0` | `0x1e90dc0` | HloPass | N |
| 12 | `legalize-cpu-cc-ops` | `xla::LegalizeCpuCCOps` | `0x40d108` | `0x1ef2e90` | HloPass | N |
| 13 | `legalize-compare` | `xla::LegalizeCompare` | `0x40d080` | `0x1ef25f0` | HloPass | N |
| 14 | `remove-passthrough-outputs` | `xla::RemovePassthroughOutputs` | `0x4102c0` | `0x1f60f40` | HloPass | N |
| 15 | `remove-gradiant-outputs` | `xla::RemoveGradiantOutputs` | `0x410218` | `0x1f5fdb0` | HloPass | N |
| 16 | `partition-embeding-ops` | `xla::PartitionEmbedingOps` | `0x40ffd0` | `0x1f59390` | HloPass | N |
| 17 | `eliminate-redundant-compare` | `xla::EliminateRedundantCompare` | `0x409530` | `0x1ea1510` | HloPass | N |
| 18 | `fuse-send-recv` | `xla::FuseSendRecv` | `0x4098a0` | `0x1ead8c0` | HloPass | N |
| 19 | `lower-argminmax-custom-call` | `xla::LowerArgMinMaxCustomCall` | `0x40e5c8` | `0x1f06c60` | HloPass | N |
| 20 | `config-lowering` | `xla::ConfigLowering` | `0x4088d0` | `0x1e90020` | HloPass | N |
| 21 | `metadata-naming` | `xla::MetadataNaming` | `0x40fc30` | `0x1f484e0` | HloPass | N |
| 22 | `hilo-conditional-to-select` | `xla::HiloConditionalToSelect` | `0x409938` | `0x1eb0dd0` | HloPass | N |
| 23 | `io-layout-normalization` | `xla::IOLayoutNormalization` | `0x40b8e8` | `0x1ed4aa0` | HloPass | N |
| 24 | `replace-minimum-constant` | `xla::ReplaceMinimumConstant` | `0x410398` | `0x1f62990` | HloPass | N |
| 25 | `unroll-while-loop` | `xla::UnrollWhileLoop` | `0x4107e0` | `0x1f733a0` | HloPass | N |
| 26 | `decompose-cc-ops` | `xla::DecomposeCCOps` | `0x409150` | `0x1e9f650` | HloPass | N |
| 27 | `simplify-concat` | `xla::SimplifyConcat` | `0x410530` | `0x1f68690` | HloPass | N |
| 28 | `fuse-reduce-scatter` | `xla::FuseReduceScatter` | `0x409818` | `0x1eace30` | HloPass | N |
| 29 | `opt-barrier-removal` | `xla::OptBarrierRemoval` | `0x40fe88` | `0x1f56240` | HloPass | N |
| 30 | `canonicalize-boundary-marker` | `xla::CanonicalizeBoundaryMarker` | `0x408640` | `0x1e8bfa0` | HloPass | N |
| 31 | `boundary-marker-removal` | `xla::BoundaryMarkerRemoval` | `0x4085a8` | `0x1e8b1a0` | HloPass | N |
| 32 | `trivial-cc-removal` | `xla::TrivialCCRemoval` | `0x4106c8` | `0x1f71e20` | HloPass | N |
| 33 | `collective-token-removal` | `xla::CollectiveTokenRemoval` | `0x408848` | `0x1e8e9e0` | HloPass | N |
| 34 | `unpack-nested-aws-ntwsr` | `xla::UnpackNestedAWSNTWSR` | `0x410758` | `0x1f72790` | HloPass | N |
| 35 | `transform-variadic-reduce` | `xla::TransformVariadicReduce` | `0x410638` | `0x1f71950` | HloPass | N |
| 36 | `emit-offloaded-dropout` | `xla::EmitOffloadedDropout` | `0x4095b8` | `0x1ea3b80` | HloPass | N |
| 37 | `lower-to-custom-native-kernel` | `xla::LowerToCustomNativeKernel` | `0x40f170` | `0x1f3eeb0` | HloPass | N |
| 38 | `decompose-attention` | `xla::DecomposeAttention` | `0x408c88` | `0x1e9d160` | HloPass | N |
| 39 | `inline-weights` | `xla::InlineWeights` | `0x40cd48` | `0x1ee8c60` | HloPass | N |
| 40 | `replace-rng` | `xla::ReplaceRng` | `0x410420` | `0x1f63280` | HloPass | N |
| 41 | `native-to-custom-softmax-dx` | `xla::NativeToCustomSoftmaxDx` | `0x40fd58` | `0x1f4c530` | HloPass | N |
| 42 | `native-to-custom-softmax` | `xla::NativeToCustomSoftmax` | `0x40fcb8` | `0x1f491b0` | HloPass | N |
| 43 | `add-logit-output` | `xla::AddLogitOutput` | `0x408338` | `0x1e87070` | HloPass | N |
| 44 | `remove-aliases` | `xla::hilo::RemoveAliases` | `0x410188` | `0x1f5df70` | HloPass | N |
| 45 | `add-must-aliases` | `xla::hilo::AddMustAliases` | `0x3ffb38` | `0x1e7ea90` | HloPass | N |
| 46 | `add-may-aliases` | `xla::hilo::AddMayAliases` | `0x3ffb88` | `0x1e7eac0` | HloPass | N |
| 47 | `flip-must-aliases` | `xla::hilo::FlipMustAliases` | `0x409708` | `0x1ea66c0` | HloPass | N |
| 48 | `flip-may-aliases` | `xla::hilo::FlipMayAliases` | `0x409758` | `0x1ea66f0` | HloPass | N |
| 49 | `legalize-types-for-infergoldens` | `xla::hilo::LegalizeTypesForInfergoldens` | `0x40e450` | `0x1f03ea0` | HloPass | N |
| 50 | `legalize-ccops-for-tensorizer` | `xla::hilo::LegalizeCCOpsForTensorizer` | `0x40d000` | `0x1ef12e0` | HloPass | N |
| 51 | `batch-norm-training-upcast` | `xla::BatchNormTrainingUpcast` | `0x408520` | `0x1e898e0` | HloPass | N |
| 52 | `inject-numerical-errors` | `xla::InjectNumericalErrors` | `0x40bb98` | `0x1ed9780` | HloPass | N |
| 53 | `preprocess-kernels-for-simulation` | `xla::PreprocessKernelsForSimulation` | `0x410060` | `0x1f5a5d0` | HloPass | N |
| 54 | `convert-inputs-to-optimal-shape` | `xla::ConvertInputsToOptimalShape` | `0x408c00` | `0x1e97850` | HloPass | N |
| 55 | `dynamic-slice-transpose` | `xla::DynamicSliceTranspose` | `0x4092a8` | `0x1ea02d0` | OpExpander | N |
| 56 | `lower-to-nki-kernels` | `xla::LowerToNKIKernelCC` | `0x40fba8` | `0x1f48300` | HloPass | N |
| 57 | `convert-fs-patterns-to-cc` | `xla::hilo::ConvertFSPatternsToCC` | `0x408b60` | `0x1e95b20` | HloPass | N |
| 58 | `collective-stream-id-checker` | `xla::CollectiveStreamIdChecker` | `0x4086e0` | `0x1e8c800` | HloPass | N |
| 59 | `rewrite-module-dtype` | `xla::RewriteModuleDtype` | `0x4104a8` | `0x1f644e0` | HloPass | N |
| 60 | `upcast-all-to-fp32` | `xla::UpcastAllToFP32` | `0x410870` | `0x1f74640` | HloPass | N |
| 61 | `preserve-control-deps` | `xla::hilo::PreserveControlDeps` | `0x410100` | `0x1f5c110` | HloPass | N |
| 62 | `neuron-hlo-inst-comb` | `xla::hilo::NeuronHloInstCombine` | `0x40fe00` | `0x1f55000` | HloPass | N |
| 63 | `neuron_looped_einsum_replacer` | `xla::hilo::NeuronLoopedEinsumReplacer` | `0x411d68` | `0x1fab000` | OpExpander | N |
| 64 | `neuron_looped_einsum_token_replacer` | `xla::hilo::NeuronLoopedEinsumTokenReplacer` | `0x411dc8` | `0x1faaef0` | OpExpander | N |
| 65 | `aws_neuron_default_metadata` | `xla::hilo::NeuronDefaultMetadata` | `0x4118c0` | `0x1f964d0` | OpExpander | N |
| 66 | `aws_neuron_common_instruction_elimination` | `xla::hilo::CommonInstructionElimination` | `0x410908` | `0x1f74f20` | HloPass | N |
| 67 | `aws_neuron_resolve_self_comparison` | `xla::hilo::ResolveSelfComparison` | `0x413080` | `0x20028c0` | OpExpander | N |
| 68 | `neuron-int-matmul-downcast` | `xla::hilo::NeuronIntMatmulDowncast` | `0x411c60` | `0x1fa9f10` | OpExpander | N |
| 69 | `legalize-device-assignment` | `xla::hilo::DeviceAssignmentLegalization` | `0x410ee0` | `0x1f7c570` | HloPass | N |
| 70 | `inject-prints` | `xla::hilo::InjectPrints` | `0x40bc28` | `0x1edaa10` | HloPass | N |
| 71 | `aws_neuron_ensure_descending_layout_in_root` | `xla::hilo::EnsureDescendingLayoutInRoot` | `0x411388` | `0x1f84f40` | HloPass | N |
| 72 | `aws_neuron_alias_to_must_alias` | `xla::hilo::AliasToMustAlias` | `0x4135a0` | `0x200a250` | HloPass | N |
| 73 | `aws_neuron_buffer_donation_to_alias` | `xla::hilo::BufferDonationToAlias` | `0x4135f0` | `0x200d0b0` | HloPass | N |
| 74 | `aws_neuron_decompose_scalar_reduce` | `xla::hilo::DecomposeScalarReduce` | `0x410d00` | `0x1f78bd0` | OpExpander | N |
| 75 | `aws_neuron_relax_collectives_layout_constraint` | `xla::hilo::RelaxCollectivesLayoutConstraint` | `0x412f88` | `0x20025e0` | OpExpander | N |
| 76 | `neuron-preprocess-kernel-duplicate-remover` | `xla::hilo::NeuronPreprocessKernelDuplicateRemover` | `0x411fd0` | `0x1fbff50` | HloPass | N |
| 77 | `all-gather-combiner` | `xla::hilo::NeuronAllGatherCombiner` | `0x4114a0` | `0x1f8add0` | HloPass | N |
| 78 | `reduce-scatter-combiner` | `xla::hilo::NeuronReduceScatterCombiner` | `0x412150` | `0x1fc49c0` | HloPass | N |
| 79 | `all-reduce-combiner` | `xla::AllReduceCombiner` | `0x413808` | `0x2014480` | HloPass | S |
| 80 | `neuron-dus-ds-index-simplifier` | `xla::hilo::NeuronDynamicUpdateSliceDynamicSliceSimplifier` | `0x411998` | `0x1f9a940` | HloPass | N |
| 81 | `aws_neuron_collective_stream_id_injector` | `xla::hilo::NeuronCollectiveStreamIdInjector` | `0x411808` | `0x1f951a0` | HloPass | N |
| 82 | `neuron_unique_channel_id_enforcer` | `xla::hilo::NeuronUniqueChannelIdEnforcer` | `0x412968` | `0x1fecb40` | HloPass | N |
| 83 | `neuron-rematerialize-large-broadcast` | `xla::hilo::RematerializeLargeBroadcast` | `0x412308` | `0x1fc8930` | HloPass | N |
| 84 | `neuron-token-threading-repeated` | `xla::hilo::NeuronTokenCollectiveThreadingRepeated` | `0x412840` | `0x1feb8c0` | HloPass | N |
| 85 | `token_threading_analyzer` | `xla::hilo::TokenThreadingAnalyzer` | `0x4136c0` | `0x200fc10` | HloPass | N |
| 86 | `collective-permute-to-all-gather` | `xla::hilo::NeuronCollectivePermuteToAllGather` | `0x411708` | `0x1f931d0` | HloPass | N |
| 87 | `dynamic-slice-mover` | `xla::hilo::NeuronDynamicSliceMover` | `0x411a60` | `0x1fa5730` | HloPass | N |
| 88 | `aws_neuron_decompose_int_all_reduce` | `xla::hilo::DecomposeIntAllReduce` | `0x410c00` | `0x1f77eb0` | OpExpander | N |
| 89 | `aws_neuron_delete_permute` | `xla::hilo::DeletePermute` | `0x410d98` | `0x1f79470` | OpExpander | N |
| 90 | `aws_neuron_constant_slice_clamp_simplifier` | `xla::hilo::ConstantSliceClampSimplifier` | `0x410a08` | `0x1f76970` | OpExpander | N |
| 91 | `neuron-rematerialize-large-allgather` | `xla::hilo::RematerializeLargeAllGather` | `0x412268` | `0x1fc6ea0` | HloPass | N |
| 92 | `aws_neuron_flip_all_gather_dynamic_slice` | `xla::hilo::NeuronFlipAllGatherDynamicSlice` | `0x411b08` | `0x1fa7c80` | OpExpander | N |
| 93 | `rewrite_scatter_partition` | `xla::hilo::RewriteScatterPartition` | `0x413460` | `0x2009240` | OpExpander | N |
| 94 | `aws_neuron_rewrite_collective_permute` | `xla::hilo::RewriteCollectivePermute` | `0x4133a0` | `0x2006930` | OpExpander | N |
| 95 | `aws_neuron_rewrite_all_gather_reduce` | `xla::hilo::RewriteAllGatherReduce` | `0x413120` | `0x20032f0` | OpExpander | N |
| 96 | `aws_neuron_flip_all_gather_convert` | `xla::hilo::FlipAllGatherConvert` | `0x411140` | `0x1f81670` | OpExpander | N |
| 97 | `aws_neuron_flip_all_gather_reshape` | `xla::hilo::FlipAllGatherReshape` | `0x4111a0` | `0x1f82510` | OpExpander | N |
| 98 | `aws_neuron_flip_all_gathers_binary` | `xla::hilo::FlipAllGathersBinary` | `0x411240` | `0x1f83370` | OpExpander | N |
| 99 | `aws_neuron_rewrite_all_reduce_dynamic_slice` | `xla::hilo::RewriteAllReduceDynamicSlice` | `0x413290` | `0x2004520` | OpExpander | N |
| 100 | `aws_neuron_rewrite_all_reduce_dynamic_slice_multiple_groups` | `xla::hilo::RewriteAllReduceDynamicSliceMultipleGroups` | `0x4132f0` | `0x2004ae0` | OpExpander | N |
| 101 | `neuron-while-loop-all-reduce-code-motion` | `xla::hilo::NeuronWhileLoopAllReduceCodeMotion` | `0x412a88` | `0x1ff39f0` | HloPass | N |
| 102 | `aws_neuron_flip_reduce_convert_add` | `xla::hilo::NeuronFlipReduceConvertAdd` | `0x411bc0` | `0x1fa8c50` | OpExpander | N |
| 103 | `neuron_move_reduce_scatter_while_loop` | `xla::hilo::NeuronMoveReduceScatterWhileLoop` | `0x411f28` | `0x1fbf1a0` | HloPass | N |
| 104 | `aws_neuron_flip_reduce_scatter_transpose` | `xla::hilo::FlipReduceScatterTranspose` | `0x4112e0` | `0x1f838f0` | OpExpander | N |
| 105 | `neuron_move_all_gather_while_loop` | `xla::hilo::NeuronMoveAllGatherWhileLoop` | `0x411e88` | `0x1fb85c0` | HloPass | N |
| 106 | `neuron_all_gather_duplicate_remover` | `xla::hilo::NeuronDuplicateParameterAllGatherRemover` | `0x4115b0` | `0x1f8e890` | HloPass | N |
| 107 | `slice-of-concat-optimizer` | `xla::hilo::NeuronSliceOfConcatOptimizer` | `0x4125e8` | `0x1fdae30` | HloPass | N |
| 108 | `neuron_repeated_dus_to_concat` | `xla::hilo::NeuronRepeatedDusToConcat` | `0x4123a0` | `0x1fd6b10` | HloPass | N |
| 109 | `aws_neuron_constant_slice_convert_canonicalizer` | `xla::hilo::ConstantSliceConvertCanonicalizer` | `0x410b08` | `0x1f76c90` | OpExpander | N |
| 110 | `aws_neuron_dynamic_slice_reshape_canonicalizer` | `xla::hilo::DynamicSliceReshapeCanonicalizer` | `0x411048` | `0x1f80fa0` | OpExpander | N |
| 111 | `aws_neuron_rewrite_all_gather_trip_count` | `xla::hilo::NeuronRewriteAllGatherTripCount` | `0x4124e8` | `0x1fd7300` | OpExpander | N |
| 112 | `while_loop_unroller` | `xla::hilo::NeuronWhileLoopUnroller` | `0x412e48` | `0x20013a0` | HloPass | N |

> **NOTE —** the **Entry** column is the per-pass body, not the `name()` stub. For the 87 own-`Run` passes it is the `Run` slot (vptr+0x18). For the 25 OpExpander passes it is `InstructionMatchesPattern` (vptr+0x28); their `Run` slot all points at the shared `xla::OpExpanderPass::Run` @ `0x29f0bb0`, so `Run` is useless as a per-pass discriminator. The non-virtual `ExpandInstruction` body for each OpExpander is reachable from its matcher but is not exposed in the vtable; those addresses are **not traced here** (out of registry scope).

> **GOTCHA —** the class name and the key name diverge for the Neuron collective passes. `name()` keys drop both the namespace and (for several) the `Neuron` prefix: `xla::hilo::NeuronAllGatherCombiner` → `all-gather-combiner` (#77), `xla::hilo::DeviceAssignmentLegalization` → the verb-first `legalize-device-assignment` (#69), `xla::hilo::NeuronCollectivePermuteToAllGather` → `collective-permute-to-all-gather` (#86). A reimplementer must read the key off `name()`, never off the class symbol.

---

## 3. `--passes` / `--skip-pass` Selection

### Purpose

The registry is a superset. The driver narrows it to a run-list. `hlo-opt` does this with the **stock XLA `DebugOptions` pass-filtering triad** — there is no Neuron-specific selection engine; the registry keys are matched directly against the comma-separated name lists in `DebugOptions`. The `--passes` / `--skip-pass` CLI flags are surface names for these `DebugOptions` fields.

### The selection flags

| DebugOptions field | CLI surface | Semantics (verbatim from the binary) | Confidence |
|---|---|---|---|
| `xla_enable_hlo_passes_only` | `--passes` | "Comma-separated list of hlo passes to be **enabled**. These names must exactly match the passes' names; no whitespace around commas. The unspecified passes are all disabled." | CERTAIN |
| `xla_disable_hlo_passes` | `--skip-pass` | "Comma-separated list of hlo passes to be **disabled**. These names must exactly match the passes' names; no whitespace around commas." | CERTAIN |
| `xla_disable_all_hlo_passes` | (`--disable-all-passes`) | "*All* passes disabled by `--xla_disable_all_hlo_passes`." | CERTAIN |

> **QUIRK —** "enable-only" is **whitelist** semantics, not "also enable". `xla_enable_hlo_passes_only` makes the listed names the *entire* run-set and disables everything else; an empty list with this flag set disables all passes. `xla_disable_hlo_passes` is the **blacklist**: run everything except the named passes. Setting both is legal — disable wins on conflict (a name in both lists does not run). This is exactly stock XLA `HloPassPipeline` filter semantics; Neuron added no new arbitration.

### Algorithm

The run-time path turns the flag lists into a `flat_hash_set<string_view>` of disabled names and threads it through the pipeline. Names are matched by exact string equality against each pass's `name()` (the same StringRef used as the registry key):

```c
// xla::HloPassPipeline::RunPassesInternal<HloModule>(module, debug_opts, disabled_set)
function RunPassesInternal(module, opts, disabled):
    if opts.xla_disable_all_hlo_passes:                 // "*All* passes disabled…"
        return module                                   // run nothing
    enable_only = split(opts.xla_enable_hlo_passes_only)  // whitelist (may be empty)
    disable     = split(opts.xla_disable_hlo_passes)      // blacklist
    for pass in pipeline_passes:                         // driver-ordered subset of the 112
        nm = pass->name()                               // exact key, == registry key
        if enable_only nonempty and nm not in enable_only:
            log "Skipping pass: " nm                     // diag string present in binary
            continue
        if nm in disable or nm in disabled:
            log "Skipping pass " nm
            continue
        Run(pass, module, disabled)                      // OpExpander passes get disabled as the run-arg set
```

### Considerations

- **Names must match exactly.** Both lists are exact-match against `name()` — no prefix, no fuzzy, no namespace. The diagnostics `Passes enabled by --xla_enable_hlo_passes_only: ` and `Passes disabled by --xla_disable_hlo_passes: ` are emitted at pipeline build so a typo'd name silently does nothing (it neither enables nor disables a real pass). Validate names against the [§2](#2-the-112-row-master-pass-index) table.
- **Where the run-list / order comes from.** The registry is unordered. The *executed* pipeline is a driver-supplied ordered name subset assembled outside `hlo-opt`'s registrar (in the Neuron pipeline-construction code / Python frontend), then filtered by the flags above. The [§2](#2-the-112-row-master-pass-index) registration order is the strong default-pipeline proxy but is **not** itself the run order. (Run-order construction not traced on this page.)
- **The disabled set is also a Run argument.** `OpExpanderPass::Run` and the per-pass `Run` slots take the `flat_hash_set<string_view>` of disabled execution-pass names as a parameter (stock XLA signature), so sub-pipelines inside a pass honor the same blacklist.

---

## 4. Stock-vs-Neuron Split

### The rule

Of the 112 registered passes, **109 are Neuron-authored** and **3 are stock XLA classes reused unchanged**:

| Pass | Class | Why stock |
|---|---|---|
| #1 `tuple-simplifier` | `xla::TupleSimplifier` | unmodified stock XLA simplifier |
| #2 `call-inliner` | `xla::CallInliner` | unmodified stock XLA inliner |
| #3 `dce` | `xla::HloDCE` | unmodified stock XLA dead-code elimination |

All three are plain `xla::` (no `hilo`) classes whose vtables sit in the high `0xd2…`/`0x48…` ranges shared with the rest of the bundled XLA runtime, and their `Run` bodies are the large stock implementations (`HloDCE::Run` @ `0x913fb70`, `CallInliner::Run` @ `0x9135790`).

### The near-miss: `all-reduce-combiner`

Row #79 `all-reduce-combiner` looks like a fourth stock pass — its class `xla::AllReduceCombiner` is plain `xla::` with no `hilo` — but it is the stock combiner *driven by a Neuron combine-key*. Its factory is constructed with Neuron-specific combiner parameters, and its real entry is `RunWithKeyCombiner`, to which Neuron supplies the `CombineKey` callback. The **Src** column therefore marks it `N` (Neuron-configured) rather than pure stock.

The two *true* Neuron collective combiners are different animals again: #77 `all-gather-combiner` (`xla::hilo::NeuronAllGatherCombiner`) and #78 `reduce-scatter-combiner` (`xla::hilo::NeuronReduceScatterCombiner`) are Neuron subclasses with their own `Run`.

> **GOTCHA — a plain `xla::` class name does not mean a stock pass.** Provenance has to be read from the factory's constructor arguments and entry point, not from the namespace: #79 is stock-classed but Neuron-parameterised, and many `xla::`-namespace passes are wholly Neuron-authored (see below).

### Namespace topology

- **`xla::` (no `hilo`)** — orders 1–43, 51–56, 58–60, 79. These are the legalize/convert/fuse/lowering passes plus the three stock passes and the stock-class combiner.
- **`xla::hilo::`** — orders 44–50, 57, 61–78, 80–112. The alias machinery, the Neuron collective transforms, the rematerializers, the while-loop motion passes, and all 25 OpExpander pattern rewriters.

> **NOTE —** the namespace does not perfectly track "Neuron vs stock": many `xla::`-namespace passes (e.g. #4 `xla::Stub`, #5 `xla::LegalizeIntrinsics`, #60 `xla::UpcastAllToFP32`) are Neuron-authored despite lacking the `hilo` namespace — their source paths are all under `hilo/hlo_passes/` (e.g. `hilo/hlo_passes/Stub.cc`, `hilo/hlo_passes/LegalizeQuantizeMX.cc`, recoverable as `.cc` path strings in the ELF). Treat the **Src** column in [§2](#2-the-112-row-master-pass-index), not the namespace, as the authority on provenance.

### Factory shapes

Most factories (105 of 112) construct via a trivial/default ctor that stores the vtable pointer inline (`mov qword ptr [rax], offset off_<ZTV+0x10>`). **7** use an explicit ctor `call` because they take constructor arguments read from the registrar's config struct (`[rbx+0xDA0]` string, `[rbx+0xE90]`/`[rbx+0xF48]` ints):

- #1 `TupleSimplifier(bool)`
- #52 `InjectNumericalErrors()`
- #77 `NeuronAllGatherCombiner(long,long,bool,bool)` — four config fields
- #78 `NeuronReduceScatterCombiner(…)`
- #79 `AllReduceCombiner(long,long)` — Neuron combine-key supplied separately
- #86 `NeuronCollectivePermuteToAllGather(std::string const&,int,int)` — reads string + two ints
- #105 `NeuronMoveAllGatherWhileLoop(…)`

The combiner-threshold / replica-count values in that registrar config struct are **not reconstructed here** (out of registry scope).

---

## 5. Evidence summary and limits

### Evidence summary

- **The count of 112.** An objdump of the `RegisterHiloHloPasses` body (`0x1e72270`–`0x1e744af`) contains exactly 112 `call` instructions whose rel32 target is `RegisterHloPass` (`0x1ebc3f0`).
- **The registrar's identity.** `nm` resolves `_ZN3xla4hilo21RegisterHiloHloPassesEv` (weak) at `0x1e72270`, with a single call site in the startup guard.
- **The container and its key.** `RegisterHloPass` @ `0x1ebc3f0` calls the factory, then `[vptr+0x10]` (= `name()`), then `StringMapImpl::hash` / `LookupBucketFor`, inserting into `hloPassRegistry` @ `0x9a38ee0`. The `name()` stub form (`mov eax,len; mov edx,offset str; ret`) was read directly for multiple rows.
- **The keys and classes.** Every key in [§2](#2-the-112-row-master-pass-index) is a verbatim `name()` literal recovered from a real vtable — none is reconstructed from a class name. A 12-key string sample (`tuple-simplifier`, `call-inliner`, `legalize-quantize-mx`, `lower-to-nki-kernels`, `neuron-hlo-inst-comb`, `aws_neuron_default_metadata`, `neuron-preprocess-kernel-duplicate-remover`, `aws_neuron_rewrite_all_reduce_dynamic_slice_multiple_groups`, `while_loop_unroller`, `upcast-all-to-fp32`, `all-gather-combiner`, `aws_neuron_rewrite_all_gather_trip_count`) was located in `strings`, and eight sampled vtables (`TupleSimplifier` `0x486f60`, `HloDCE` `0xd26ec8`, `LegalizeQuantizeMX` `0x40e168`, `Stub` `0x4105a8`, `RemoveAliases` `0x410188`, `ResolveSelfComparison` `0x413080`, `NeuronLoopedEinsumReplacer` `0x411d68`, `NeuronReduceScatterCombiner` `0x412150`) match the table.
- **Stock-XLA selection.** The strings `xla_enable_hlo_passes_only` ("…unspecified passes are all disabled"), `xla_disable_hlo_passes` and `xla_disable_all_hlo_passes`, the diagnostics `Passes enabled by --xla_enable_hlo_passes_only: ` / `Passes disabled by --xla_disable_hlo_passes: ` / `*All* passes disabled by --xla_disable_all_hlo_passes.` / `Skipping pass: `, and the `HloPassPipeline::RunPassesInternal<HloModule>(…, flat_hash_set<string_view> const&)` symbol are all present in the ELF.

### Limits of this reading

The table is a full disassembly-derived enumeration, but only the eight vtables listed above were re-derived independently as a spot-check; the remaining rows rest on the single enumeration pass.

Two things sit outside what was traced. The CLI spellings `--passes` / `--skip-pass` are mapped to their `DebugOptions` backing fields above, but the argv-parsing glue that binds them — which option library performs `--passes` → `xla_enable_hlo_passes_only` — was not followed, so the spelling is [INFERRED] while the field semantics are read from the binary. And the **run order** is not here at all: the registry is unordered, and the executed pipeline is an ordered name subset assembled outside `hlo-opt`'s registrar. The registration order in [§2](#2-the-112-row-master-pass-index) is a good proxy for the default pipeline but is not itself the run order. The combiner-threshold and replica-count values in the registrar's config struct are likewise out of scope.

---

## Related Components

| Component | Relationship |
|---|---|
| `xla::HloPassPipeline` | Consumes registry factories; applies the enable/disable filter; runs the ordered subset |
| `xla::OpExpanderPass::Run` (`0x29f0bb0`) | Shared `Run` for the 25 OpExpander passes; they override `InstructionMatchesPattern` |
| `xla::HloPassInterface` | Base class; `name()` (vptr+0x10) is the registry key source, `Run` (vptr+0x18) the body |
| Neuron pipeline constructor | Supplies the actual ordered run-list (outside `hlo-opt`'s registrar) |

## Cross-References

- [walrus-driver-cli](../frontend/walrus-driver-cli.md) — the frontend pass vocabulary and how the driver assembles the `--passes` list this registry resolves
- Every Part-4 (4.x) page — each documents one or more rows of the [§2](#2-the-112-row-master-pass-index) table in depth
- Part-14 pass catalog — the cross-tool index that this hlo-opt registry feeds
