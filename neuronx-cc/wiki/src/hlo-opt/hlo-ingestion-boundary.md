# HLO / mhlo / stablehlo Ingestion & the Stock-vs-Neuron Boundary

> *All addresses, symbol counts, and offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical — treat names, offsets, and constants as stable, virtual addresses as cp310-specific). The HLO pass corpus lives in `neuronxcc/starfish/bin/hlo-opt` (ELF, BuildID `93dd8bd9bd4c697b`, 229 MB, **not stripped**). The MLIR-side import/simplify passes live in `neuronxcc/starfish/bin/hlo2penguin` (BuildID `4ea91b0c9770f006`). Both binaries carry a full local symtab — `nm -C` resolves 369,744 names on `hlo-opt` — so every claim below is anchored to a demangled symbol, a verbatim string, or a section offset.*

## Abstract

The Neuron front-half compiler is **mostly upstream**. `hlo-opt` and `hlo2penguin` statically link the whole of XLA, MHLO, StableHLO, and LLVM at the `lts_20230802` Abseil snapshot; the great majority of the passes that run on a model are stock optimizers — `AlgebraicSimplifier`, `HloDCE`, `BatchNormExpander`, `SpmdPartitioner`, `ShardingPropagation` — that AWS did not write and does not maintain. Layered on top of that imported middle is a comparatively thin band of **Neuron-authored** passes that recognize Neuron custom-calls, coalesce collectives for the specific NeuronLink topology, reify control dependencies, and pattern-match the operators the device can lower. This page draws the line between the two so that every subsequent Part-4 page can state, for any pass it documents, whether that pass *is* or *isn't* Neuron's own code.

The line is not a judgment call — it is **encoded in the C++ namespace mangling and the source-path strings baked into the binary**. Neuron's HLO passes live in the `xla::hilo::` namespace (2,042 demangled symbol-lines on `hlo-opt`); stock XLA passes live in the bare `xla::` namespace (51,551 lines). Neuron's translation units are tagged `hilo/hlo_passes/<Pass>.cc` (76 distinct files); stock XLA's are tagged `xla/hlo/transforms/<area>/<pass>.cc`. Neuron's emitted ops are `AwsNeuron*` custom-calls; stock ops are the standard `HloOpcode` enum. Any one of these four markers classifies a pass; together they are redundant and mutually corroborating. A reimplementer who wants to know "did AWS write this, or is it free upstream code I can read in the open XLA tree?" applies the algorithm in [§4](#4-the-classification-algorithm) and gets a definite answer.

A caution governs every address on this page. `hlo-opt` is a 229 MB statically-linked executable whose load layout shifts the section offsets: for `.text` the virtual address exceeds the raw file offset by **0x201000**, and for `.rodata` by **0x200000** (verified in [§5](#5-the-binary-offset-caveat)). IDA's JSON sidecars report *virtual* addresses; a raw `xxd`/`objdump --start-address` against the on-disk file must subtract the per-section delta first. Get this wrong and a correct symbol lands in the wrong function — the single most common error when reading this binary.

For reimplementation, the contract this page establishes is:

- The **four classification markers** (`xla::hilo::` namespace, `Neuron` class prefix, `hilo/hlo_passes/*.cc` source path, `AwsNeuron*` custom-call) and the precedence among them when they appear to disagree.
- The **stock-middle / Neuron-edge** split: which broad pass families are imported wholesale (algebraic simplification, DCE, SPMD, sharding, expanders, the StableHLO↔HLO bridge) versus authored at AWS (collective combiners for NeuronLink, custom-call legalization, control-dep preservation, FS-pattern matching).
- The **ingestion artifacts**: that a `stochastic-convert` op (and the whole stock opcode vocabulary) is *recognized at the input boundary* by the linked HLO/StableHLO parser even when there is no device lowering for it, and what that means for classifying a pass that touches such an op.
- The **`.text`/`.rodata` offset deltas**, so addresses cited from the IDA JSON resolve to the right bytes on disk.

| | |
|---|---|
| **HLO pass binary** | `neuronxcc/starfish/bin/hlo-opt` — BuildID `93dd8bd9bd4c697b`, not stripped |
| **MLIR import/emit binary** | `neuronxcc/starfish/bin/hlo2penguin` — BuildID `4ea91b0c9770f006` |
| **Neuron namespace** | `xla::hilo::` — 2,042 symbol-lines (`nm -C \| rg -c 'xla::hilo::'`) |
| **Neuron class-prefix subset** | `xla::hilo::Neuron*` — 637 lines, ≥28 distinct classes |
| **Stock XLA namespace** | bare `xla::` — 51,551 lines; `xla::spmd::` (1,218), `mlir::mhlo::` (11,194), `mlir::stablehlo::` (12,736) |
| **Neuron source-path marker** | `hilo/hlo_passes/*.cc` — 76 distinct files; also `hilo/tools/` |
| **Stock source-path marker** | `xla/hlo/transforms/*.cc`, `xla/service/*.cc` |
| **Neuron emitted-op marker** | `AwsNeuron*` custom-call targets (`AwsNeuronControlDep`, `AwsNeuronGelu`, …) |
| **Pass registry (Neuron)** | `xla::hilo::RegisterHloPass` @ `0x1ebc3f0`; `GetHloPassRegistry` @ `0x1ebc570`; `hloPassRegistry` (BSS) @ `0x9a38ee0` |
| **Upstream snapshot pin** | Abseil `lts_20230802` (visible in every stock symbol's `flat_hash_set` type) |
| **`.text` VMA − fileoff** | `0x201000` (VMA `0x1e6e960`, fileoff `0x1c6d960`) |
| **`.rodata` VMA − fileoff** | `0x200000` (VMA `0x20c940`, fileoff `0xc940`) |

---

## 1. Why the boundary matters

Treat the two front-half binaries as **XLA distributions with a Neuron delta**. `hlo-opt` is the XLA HLO optimizer compiled into a tool; `hlo2penguin` is the same plus an MLIR (MHLO/StableHLO) import path and a Python-"Penguin"-IR emitter ([4.32](hlo2penguin-mlir-pipeline.md)). Everything XLA ships — the HLO opcode enum, the literal evaluator, the shape verifier, the algebraic simplifier, the SPMD partitioner, the StableHLO dialect and its bridge to HLO — is *inside* these binaries, statically linked. Reverse-engineering the *whole* of either binary would mostly re-document open-source XLA. The interesting, closed, Neuron-specific behavior is the delta, and the delta is small relative to the imported mass: 2,042 `xla::hilo::` lines against 51,551 bare-`xla::` lines is roughly a 1-in-25 ratio of Neuron-authored to stock symbol surface.

The practical consequence for the wiki: a Part-4 page documenting `NeuronAllReduceCombiner` is documenting genuinely closed AWS code (no open reference exists). A page documenting `AlgebraicSimplifier` or `SpmdPartitioner` is documenting *stock XLA that happens to be linked in* — the algorithm is in the public XLA tree, and the page's job is only to confirm it is present, identify the version pin, and note any Neuron-specific scheduling of it. The two demand different treatment, and conflating them produces two errors: crediting AWS with upstream work, or burying a genuine Neuron innovation under "it's just XLA." The marker test below removes the guesswork.

> **NOTE —** "Neuron-authored" here means *resides in the `xla::hilo::` namespace / `hilo/` source tree* — i.e. AWS's own first-party compiler code, distinct from the upstream XLA they vendored. It does **not** mean "novel algorithm." Some `xla::hilo::` passes are thin Neuron wrappers that call straight into a stock `xla::` helper (the combiners reuse `xla::AllReduceCombiner` key logic). The namespace tells you *who owns the translation unit*, which is exactly the provenance question Part 4 must answer; whether the contained algorithm is original is a separate, per-pass judgment.

---

## 2. The imported middle (stock XLA / MHLO / StableHLO)

### What is linked, and how to recognize it

The stock corpus occupies the bare `xla::`, `mlir::mhlo::`, `mlir::stablehlo::`, and `xla::spmd::` namespaces. It is recognizable three ways, any of which suffices:

1. **Bare `xla::<Pass>`** mangling — no `hilo` infix. `nm -C hlo-opt | rg '(^| )xla::AlgebraicSimplifier'` returns 477 lines (the count is dominated by `AlgebraicSimplifierVisitor` template instantiations); the same query with `xla::hilo::AlgebraicSimplifier` returns **0**. The pass is in `xla::`, not `xla::hilo::`. This holds for every stock pass sampled (see table).
2. **Stock source path** — the `Run`/`name` bodies are tagged `xla/hlo/transforms/<area>/<pass>.cc` or `xla/service/<pass>.cc`, e.g. `xla/hlo/transforms/expanders/stochastic_convert_decomposer.cc`, `xla/hlo/transforms/expanders/qr_expander.cc`. Contrast the Neuron marker `hilo/hlo_passes/*.cc` ([§3](#3-the-neuron-edge-authored-passes)).
3. **Abseil `lts_20230802` type tag** — every stock `Run(HloModule*, const flat_hash_set<…>&)` signature carries `absl::lts_20230802::flat_hash_set<…>` in its mangling (confirmed on `xla::StochasticConvertDecomposer::Run` @ `0x27629b0`). That LTS tag is the upstream snapshot the whole tree was vendored from; it is the single most reliable "this is imported XLA" fingerprint because it brands the *type system*, not the symbol name.

### Representative stock families

| Stock pass / family | Namespace | Symbol-line count (`nm -C`) | Notes |
|---|---|---|---|
| `AlgebraicSimplifier` | `xla::` | 477 | the canonical peephole simplifier (count dominated by `AlgebraicSimplifierVisitor` instantiations) |
| `HloDCE` | `xla::` | 28 | dead-code elimination |
| `HloCSE` | `xla::` | 20 | common-subexpression elimination |
| `TupleSimplifier` | `xla::` | 15 | stock tuple cleanup (distinct from the Neuron MLIR `NeuronControlDepTupleSimplifier`, [§3](#3-the-neuron-edge-authored-passes)) |
| `WhileLoopSimplifier` | `xla::` | 16 | loop cleanup |
| `BatchNormExpander` | `xla::` | 14 | drives `--expand-batch-norm-training` |
| `ZeroSizedHloElimination` | `xla::` | 10 | empty-tensor removal |
| `FlattenCallGraph` | `xla::` | 17 | call-graph flattening |
| `StochasticConvertDecomposer` | `xla::` | 10 | the `stochastic-convert` expander ([§6](#6-ingestion-artifact-recognized--lowerable)) |
| `ShardingPropagation` | `xla::` | 75 | SPMD sharding inference |
| `SpmdPartitioner` | `xla::spmd::` | (family) | stock SPMD partitioner; `StatefulRngSpmdPartitioner` also present |

> **GOTCHA —** SPMD is **stock**, and this trips people because the *driver* exposes a `--spmd` flag ("Run additional optimizations only applied to SPMD models", [4.1](pass-registry.md)). The flag is Neuron's; the *partitioner it gates* is `xla::spmd::SpmdPartitioner` / `xla::spmd::StatefulRngSpmdPartitioner`, pure upstream XLA (Part 13). A Neuron flag toggling a stock pass is the common shape — do not let a Neuron-named knob promote the pass it controls to "Neuron-authored." Classify the **pass body's namespace**, not the flag that schedules it.

### The MHLO / StableHLO dialects and the bridge

`hlo2penguin` ingests both raw XLA `HloModule` protos *and* MLIR text in the MHLO or StableHLO dialects ([4.1](pass-registry.md): `parseHloProtoInput` vs `parseSourceFile` + `runStableHLOFrontendPipeline` + `ConvertStablehloToHlo`). The dialects themselves are stock: `mlir::mhlo::` (11,194 lines) and `mlir::stablehlo::` (12,736 lines) are the open MHLO/StableHLO models, and the StableHLO→HLO bridge `xla::ConvertStablehloToHlo` plus the surrounding `*StablehloToHlo*`/`*HloToMhlo*` converters total 1,151 demangled symbol-lines — all bare-namespace upstream. The Neuron delta on the MLIR side is *not* the dialects but a small set of `hilo::`-namespaced MLIR passes that run after import (next section).

---

## 3. The Neuron edge (authored passes)

### The four shapes of a Neuron-authored unit

Neuron's own code shows up in four mangling/string shapes. Internalize these — they are the whole classification basis:

1. **`xla::hilo::Neuron<Name>`** — the explicit Neuron-prefixed HLO passes. 637 symbol-lines; ≥28 distinct classes. Enumerated below.
2. **`xla::hilo::<Name>` without the `Neuron` prefix** — *also Neuron-authored*. The `xla::hilo::` namespace is the owner; the `Neuron` prefix is a naming convention applied to *some but not all* of its passes. `PreserveControlDeps` ([4.15](control-dep-reification.md)) and `ConvertFSPatternsToCC` are `xla::hilo::` with no `Neuron` prefix yet are unmistakably Neuron code (they emit `AwsNeuron*` custom-calls / match Neuron-customer patterns). **This is the single most important subtlety on the page** — the prefix is a hint, the namespace is the rule.
3. **`hilo::<Name>` (lowercase `hilo`, MLIR side, in `hlo2penguin`)** — the MHLO/StableHLO-level Neuron passes: `hilo::NeuronControlDepTupleSimplifier`, `hilo::isControlDep`, `hilo::runStableHLOFrontendPipeline`. These do not appear in `hlo-opt` at all (the HLO/MLIR split, [§7](#7-worked-classification-table)).
4. **`AwsNeuron*` custom-call targets** — the *ops* Neuron emits and recognizes. Not passes, but a corroborating signal: a pass that creates or matches `AwsNeuronControlDep`, `AwsNeuronGelu`, `AwsNeuronSoftmax`, etc. is Neuron's by construction (stock XLA has no concept of these).

### The `xla::hilo::Neuron*` class roster (28 of 637 lines)

`nm -C hlo-opt | rg -o 'xla::hilo::Neuron[A-Za-z0-9_]+' | sort -u`:

```text
NeuronAddBoundaryMarker            NeuronLoopedEinsumReplacer
NeuronAllGatherCombiner            NeuronLoopedEinsumTokenReplacer
NeuronAllReduceCombineKey          NeuronMoveAllGatherWhileLoop
NeuronAllReduceCombiner            NeuronMoveReduceScatterWhileLoop
NeuronCollectivePermuteToAllGather NeuronPreprocessKernelDuplicateRemover
NeuronCollectiveStreamIdInjector   NeuronReduceScatterCombiner
NeuronCombiner                     NeuronRepeatedDusToConcat
NeuronDefaultMetadata              NeuronRewriteAllGatherTripCount
NeuronDuplicateParameterAllGatherRemover  NeuronSliceOfConcatOptimizer
NeuronDynamicSliceMover            NeuronTokenCollectiveThreadingRepeated
NeuronDynamicUpdateSliceDynamicSliceSimplifier  NeuronUniqueChannelIdEnforcer
NeuronFlipAllGatherDynamicSlice    NeuronWhileLoopAllReduceCodeMotion
NeuronFlipReduceConvertAdd         NeuronWhileLoopUnroller
NeuronHloInstCombine               NeuronIntMatmulDowncast
```

The roster is dominated by **collective** rewriting (combiners, all-gather/reduce-scatter motion, channel-id enforcement, stream-id injection) — exactly the work a custom interconnect (NeuronLink) demands and that stock XLA's generic collective passes do not cover. The non-`Neuron`-prefixed `xla::hilo::` cohort (sampled: `AddMustAliases`, `ConvertFSPatternsToCC`, `DecomposeIntAllReduce`, `FlipAllGatherConvert`, `GroupedDusToConcat`, `IOLayoutNormalization`, `PreserveControlDeps`) brings the total to 2,042 lines and is *equally* Neuron-owned.

### The `hilo/` source-path roster (76 files)

`strings -a hlo-opt | rg -o 'hilo/hlo_passes/[A-Za-z0-9_]+\.cc' | sort -u` yields **76 distinct** translation units (`hilo/tools/` is the other `hilo/` subtree). Sample:

```text
hilo/hlo_passes/AddAlias.cc                 hilo/hlo_passes/FuseReduceScatter.cc
hilo/hlo_passes/BatchNormTrainingUpcast.cc  hilo/hlo_passes/HiloConditionalToSelect.cc
hilo/hlo_passes/BoundaryMarkerRemoval.cc    hilo/hlo_passes/HloPassManager.cc
hilo/hlo_passes/CollectiveTokenRemoval.cc   hilo/hlo_passes/InjectNumericalErrors.cc
hilo/hlo_passes/ConvertCollectivesToCustomCall.cc  hilo/hlo_passes/InlineWeights.cc
hilo/hlo_passes/ConvertFSPatternsToCC.cc    hilo/hlo_passes/IOLayoutNormalization.cc
hilo/hlo_passes/DecomposeAttention.cc       hilo/hlo_passes/LegalizeAwsNeuronArgMax.cc
hilo/hlo_passes/DecomposeCCOps.cc           hilo/hlo_passes/PreserveControlDeps.cc
hilo/hlo_passes/EmitOffloadedDropout.cc     …  (76 total)
```

The `hilo/hlo_passes/PreserveControlDeps.cc` tag is the clinching cross-check on the "namespace, not prefix" rule from §3: `PreserveControlDeps` has no `Neuron` prefix, yet its source file lives in `hilo/hlo_passes/`, and its `setSourceLine 0x31` (=49) is readable directly off the rodata string `hilo/hlo_passes/PreserveControlDeps.cc` @ VMA `0x2b8088`. Two independent markers — namespace and source path — agree.

### The `AwsNeuron*` emitted-op roster

`strings -a hlo-opt | rg -o '\bAwsNeuron[A-Za-z]+' | sort -u` (selection):

```text
AwsNeuronArgMax        AwsNeuronControlDep      AwsNeuronGelu
AwsNeuronArgMin        AwsNeuronDevicePrint     AwsNeuronGeluBackward
AwsNeuronCollectiveMatmul  AwsNeuronDropout     AwsNeuronRmsNorm
AwsNeuronCustomNativeKernel AwsNeuronErf        AwsNeuronSilu
AwsNeuronModuleMarkerStart/End  AwsNeuronSoftmax/Backward  AwsNeuronTopK
```

These are the `custom_call_target` strings Neuron passes plant into the HLO graph; `AwsNeuronControlDep` (target len `0x13`=19) is the control-dep reification op ([4.15](control-dep-reification.md)). A pass that constructs one of these is Neuron's. (One string, `AwsNeuronAllReduAwsNeuronReduceSfficient`, is a rodata-adjacency artifact of two overlapping literals, not a real target — a reminder that raw `strings` can splice neighbors; trust the demangled symbol over the spliced string when they conflict.)

---

## 4. The classification algorithm

Given a candidate pass (by class name, by the `Run`/`name` symbol, or by the source-path string at its body), decide stock vs Neuron:

```c
// Returns STOCK | NEURON | NEEDS_REVIEW for an hlo-opt / hlo2penguin pass.
// Inputs available from the not-stripped binary: demangled symbol (nm -C),
// the source-path .cc string at the pass body, the custom_call_target it emits.
classify(pass):
    sym = demangled_symbol(pass.Run_or_name)      // e.g. "xla::hilo::NeuronAllReduceCombiner::Run"
    src = source_path_string(pass.body)            // e.g. "hilo/hlo_passes/FuseReduceScatter.cc" or
                                                   //      "xla/hlo/transforms/expanders/qr_expander.cc"

    // --- RULE 1: namespace is authoritative ---------------------------------
    if sym contains "xla::hilo::"  or  sym matches /(^| )hilo::/ :   // upper- or lower-case hilo
        return NEURON            // owner is AWS's first-party tree; prefix "Neuron" is optional

    // --- RULE 2: stock namespaces -------------------------------------------
    if sym starts_with "xla::"      and not contains "hilo"  : return STOCK   // bare xla::
    if sym starts_with "xla::spmd::"                          : return STOCK   // SPMD is upstream (Part 13)
    if sym starts_with "mlir::mhlo::" or "mlir::stablehlo::"  : return STOCK   // the dialects themselves

    // --- RULE 3: source-path corroboration (symbol absent/ambiguous) --------
    if src starts_with "hilo/"                                : return NEURON
    if src starts_with "xla/" or "mlir/" or "stablehlo/"     : return STOCK

    // --- RULE 4: emitted-op corroboration -----------------------------------
    if pass emits/matches custom_call_target ~ /^AwsNeuron/   : return NEURON

    // --- RULE 5: the absl LTS tag is a *type* fingerprint of imported XLA ----
    if sym's Run signature carries "absl::lts_20230802::flat_hash_set" : return STOCK   // weak

    return NEEDS_REVIEW          // e.g. a Neuron flag wrapping a stock pass: classify the BODY
```

**Precedence and conflicts.** Rules are tried in order; Rule 1 (namespace) wins over everything. The deliberate ordering encodes three facts learned from the binary:

- A pass can be `xla::hilo::` *without* a `Neuron` prefix (`PreserveControlDeps`) → Rule 1 still tags it NEURON. **Never gate on the `Neuron` prefix alone.**
- A Neuron *flag* (`--spmd`, `--expand-batch-norm-training`) can schedule a *stock* pass → the flag does not appear in `classify`; only the pass **body's** namespace does. This is the `NEEDS_REVIEW` escape: when handed "the pass behind `--spmd`," resolve it to `xla::spmd::SpmdPartitioner` first, then classify → STOCK.
- The absl `lts_20230802` tag (Rule 5) is a *type-system* fingerprint that brands imported XLA even where a symbol name is otherwise ambiguous; it is weak because Neuron's `xla::hilo::` passes *also* take `absl::lts_20230802::flat_hash_set` arguments (they share the linked Abseil). Use it only to confirm STOCK, never to override a `hilo` namespace match.

> **QUIRK —** the lowercase `hilo::` (MLIR) and the `xla::hilo::` (HLO) namespaces are *both* Neuron and *both* matched by Rule 1, but they live in **different binaries**: the HLO `xla::hilo::` passes are in `hlo-opt`; the MLIR `hilo::` passes (`NeuronControlDepTupleSimplifier`, `runStableHLOFrontendPipeline`) are in `hlo2penguin`. `nm -C hlo-opt | rg NeuronControlDepTupleSimplifier` → **0 hits**. The binary a symbol lives in is itself a provenance/stage signal (HLO-level vs MLIR-level), orthogonal to stock-vs-Neuron.

---

## 5. The binary-offset caveat

`hlo-opt` is statically linked and large enough that its loadable segments do not start at file offset 0 of their section, so **virtual address ≠ file offset**. From `readelf -SW hlo-opt`:

```text
[11] .rodata  PROGBITS  VMA 0x000000000020c940  fileoff 0x00c940   →  VMA − off = 0x200000
[16] .text    PROGBITS  VMA 0x0000000001e6e960  fileoff 0x1c6d960   →  VMA − off = 0x201000
[30] .data    PROGBITS  VMA 0x0000000009a27c00  fileoff 0x9824c00   →  VMA − off = 0x203000
```

The IDA JSON sidecars (`*_functions.json`, `*_strings.json`, `*_xrefs.json`) and every address in the backing reports report **virtual addresses**. To read raw bytes off the on-disk file you must subtract the per-section delta first:

```text
file_offset(.text  VA)  =  VA − 0x201000      # e.g. PreserveControlDeps::Run VA 0x1f5c110 → off 0x1d5b110
file_offset(.rodata VA) =  VA − 0x200000      # e.g. "AwsNeuronControlDep" VA 0x26e82c → off 0x6e82c
file_offset(.data   VA) =  VA − 0x203000
```

> **GOTCHA — VMA does not equal file offset in `hlo-opt`.** The convenient rule "for `.text`/`.rodata` the VMA equals the file offset" holds for some smaller Neuron libraries (`libwalrus.so`, [2.x](../arch/sbuf-psum-geometry.md)) and is **false here**. The two deltas differ from each other and neither is zero: `raw fileoff = VA − 0x201000` for `.text`, `VA − 0x200000` for `.rodata`. Applying the wrong delta — or zero — lands a correct symbol in the wrong function, which is the single most common error reading this binary. Resolve through the IDA JSON (VA-keyed) and convert only when you must touch raw bytes.

---

## 6. Ingestion artifact: recognized ≠ lowerable

A subtlety the ingestion boundary forces: because the **whole** stock XLA opcode vocabulary is statically linked, the HLO/StableHLO *parser* will **accept** ops that the Neuron device has **no lowering for**. Recognition at the input boundary is not the same as device support. The canonical example, fully worked in [the numeric negative-results page](../numerics/numeric-negative-results.md):

- `stochastic-convert` is an exact-match line in `hlo-opt`'s `HloOpcodeString`/`StringToHloOpcode` table (`strings | rg -x 'stochastic-convert'` → 1 hit). The text parser accepts it.
- The full stock machinery is present and bare-`xla::`: `xla::HloEvaluator::HandleStochasticConvert`, `xla::ShapeInference::InferStochasticConvertShape`, `xla::HloInstruction::CreateStochasticConvert`, and the expander `xla::StochasticConvertDecomposer::Run` @ `0x27629b0` (source `xla/hlo/transforms/expanders/stochastic_convert_decomposer.cc`).
- There is **no** Neuron handler: `nm -C hlo2penguin | rg penguin | rg -ci stochastic` → 0; no `xla::hilo::` / `penguin::` StochasticConvert visitor exists. The stock decomposer rewrites the op into deterministic arithmetic before device lowering, or the op errors as un-lowerable.

The classification lesson: **a pass or handler being present in the binary does not make it part of the Neuron device path.** `HandleStochasticConvert` classifies STOCK (bare `xla::`, `HloEvaluator` is the reference interpreter) and is *dead with respect to the device* — a textbook case where the namespace marker correctly separates "linked-in upstream scaffolding" from "Neuron-authored device behavior." The same reasoning applies to the LLVM `denormal-fp-math` attributes and the NVPTX/AMDGPU `ftz` intrinsics that ride in with the linked LLVM ([numeric negative results](../numerics/numeric-negative-results.md)): present in `hlo-opt`/`hlo2penguin`, bare-namespace, and irrelevant to NeuronCore numerics.

> **NOTE —** when a Part-4 page documents a pass that *touches* a stock op (e.g. a Neuron combiner that consumes an `all-reduce`), classify the **pass**, not the op. The op may be stock `HloOpcode::kAllReduce`; the combiner that rewrites it is `xla::hilo::NeuronAllReduceCombiner` → NEURON. Provenance attaches to the transformation, not the data it transforms.

---

## 7. Worked classification table

Applying [§4](#4-the-classification-algorithm) to representative passes/handlers. "Marker hit" names the rule that fires. All symbol/string evidence re-verified by `nm -C` / `strings -a` against the cp310 binary.

| Pass / symbol | Binary | Namespace / source / op | classify() | Marker hit |
|---|---|---|---|---|
| `NeuronAllReduceCombiner::Run` | hlo-opt | `xla::hilo::Neuron…` | **NEURON** | R1 namespace + R1 prefix |
| `NeuronCollectiveStreamIdInjector` | hlo-opt | `xla::hilo::Neuron…` | **NEURON** | R1 |
| `PreserveControlDeps::Run` | hlo-opt | `xla::hilo::` (no prefix); src `hilo/hlo_passes/PreserveControlDeps.cc`; emits `AwsNeuronControlDep` | **NEURON** | R1 namespace (prefix absent) + R3 src + R4 op |
| `ConvertFSPatternsToCC` | hlo-opt | `xla::hilo::`; src `hilo/hlo_passes/ConvertFSPatternsToCC.cc` | **NEURON** | R1 + R3 |
| `LegalizeAwsNeuronArgMax` | hlo-opt | src `hilo/hlo_passes/LegalizeAwsNeuronArgMax.cc`; `AwsNeuronArgMax` | **NEURON** | R3 + R4 |
| `NeuronControlDepTupleSimplifier::runOnOperation` | **hlo2penguin** | `hilo::` (MLIR); arg `flatten-control-dep-tuple-operands` | **NEURON** | R1 (lowercase `hilo::`) |
| `AlgebraicSimplifier::Run` | hlo-opt | bare `xla::` | **STOCK** | R2 |
| `HloDCE` / `HloCSE` / `TupleSimplifier` | hlo-opt | bare `xla::` | **STOCK** | R2 |
| `BatchNormExpander` (gated by `--expand-batch-norm-training`) | hlo-opt | bare `xla::`; flag is Neuron | **STOCK** | R2 (classify body, not flag) |
| `SpmdPartitioner` (gated by `--spmd`) | hlo-opt | `xla::spmd::` | **STOCK** | R2 (Part 13) |
| `ShardingPropagation::Run` | hlo-opt | bare `xla::` | **STOCK** | R2 |
| `StochasticConvertDecomposer::Run` | hlo-opt | bare `xla::`; src `xla/hlo/transforms/expanders/…` | **STOCK** | R2 + R3 ([§6](#6-ingestion-artifact-recognized--lowerable)) |
| `HloEvaluator::HandleStochasticConvert` | hlo-opt | bare `xla::`; reference interpreter, device-dead | **STOCK** | R2 (recognized ≠ lowerable) |
| `ConvertStablehloToHlo` | hlo2penguin | bare `xla::`; the StableHLO→HLO bridge | **STOCK** | R2 |
| `mlir::mhlo::*` / `mlir::stablehlo::*` ops | hlo2penguin | the dialects themselves | **STOCK** | R2 |

---

## 8. Evidence summary

The five strongest claims, re-checked against the binary; unprovable parts tagged.

- **Neuron HLO passes are `xla::hilo::`; stock are bare `xla::`.** `nm -C hlo-opt | rg -c 'xla::hilo::'` = 2,042; `rg -c '\bxla::'` = 51,551. Sampled stock passes (`AlgebraicSimplifier`, `HloDCE`, `SpmdPartitioner`, …) all return 0 under `xla::hilo::<Pass>` and non-zero under `xla::<Pass>` / `xla::spmd::<Pass>`. The split is exact across every sample: no stock pass inside `xla::hilo::`, no Neuron pass in bare `xla::`.

- **The `Neuron` prefix is a hint; the `xla::hilo::` namespace is the rule.** `PreserveControlDeps` and `ConvertFSPatternsToCC` demangle as `xla::hilo::PreserveControlDeps` / `xla::hilo::ConvertFSPatternsToCC` — `xla::hilo::` with no `Neuron` prefix — and both carry `hilo/hlo_passes/*.cc` source strings. Two independent markers agree, so gating on the prefix alone would mis-classify these as non-Neuron.

- **SPMD is stock, despite the Neuron `--spmd` flag.** `nm -C hlo-opt | rg SpmdPartitioner` resolves to `xla::spmd::SpmdPartitioner` / `xla::spmd::StatefulRngSpmdPartitioner`; `xla::spmd::` symbol-lines total 1,218; there are zero `xla::hilo::*Spmd*`. The `--spmd` flag is a driver `cl::opt` ([4.1](pass-registry.md)): the flag is Neuron, the pass body is upstream.

- **`.text` VMA−fileoff = `0x201000`, `.rodata` = `0x200000`.** From `readelf -SW hlo-opt`: `.text` VMA `0x1e6e960` / off `0x1c6d960`; `.rodata` VMA `0x20c940` / off `0xc940`.

- **`stochastic-convert` is recognized at ingest but has no Neuron device lowering.** `strings hlo-opt | rg -x 'stochastic-convert'` returns one hit (the opcode-table line); `xla::StochasticConvertDecomposer::Run` is present @ `0x27629b0` under bare `xla::`; `nm -C hlo2penguin | rg penguin | rg -ci stochastic` returns 0. What happens to such an op — decomposed away, or errored as unlowerable at the visitor — is **[INFERRED]** ([numeric negative results](../numerics/numeric-negative-results.md)): the default-action string `"<op> cannot be lowered."` is present, but the opcode→default path is deduced from the absence of a case rather than traced.

> **GOTCHA — these counts are symbol *lines*, not passes.** The 2,042 / 51,551 / 637 figures are `nm -C` demangled symbol-line counts, and one class yields many lines: ctor, dtor, `Run`, `name`, vtable, typeinfo, template instantiations. They are sound as namespace-surface proportions and as the basis for the marker test, but "637" is not "637 Neuron passes" — the distinct `xla::hilo::Neuron*` class count is ≥28 ([§3](#3-the-neuron-edge-authored-passes)) and the distinct `hilo/hlo_passes/*.cc` file count is 76. For a per-pass census see [4.1 the pass registry](pass-registry.md).

---

## 9. Cross-references

- [4.1 — hlo-opt pass registry](pass-registry.md): the per-pass census and registration order; `xla::hilo::RegisterHloPass` @ `0x1ebc3f0`, `GetHloPassRegistry` @ `0x1ebc570`, `hloPassRegistry` (BSS) @ `0x9a38ee0`. Every row there is classifiable by this page's algorithm.
- [4.32 — hlo2penguin MLIR Pipeline Order & Entry Flow](hlo2penguin-mlir-pipeline.md): the MLIR import path (`parseSourceFile` / `runStableHLOFrontendPipeline` / `ConvertStablehloToHlo`) and the Penguin emitter that consume the post-classification graph.
- [front/control-deps — PreserveControlDeps & the control-dep custom-call](control-dep-reification.md): the worked example of an `xla::hilo::`-namespace, no-`Neuron`-prefix, `AwsNeuron*`-emitting Neuron pass, and the `hilo::` MLIR-side simplifier in `hlo2penguin`.
- [numeric/negative-results — recognized-but-unlowerable](../numerics/numeric-negative-results.md): the full stochastic-convert / `denormal-fp-math` / `ftz` worked cases for "linked-in upstream scaffolding ≠ Neuron device path."
- **Part 13 — SPMD**: the stock `xla::spmd::SpmdPartitioner` documented as upstream.
