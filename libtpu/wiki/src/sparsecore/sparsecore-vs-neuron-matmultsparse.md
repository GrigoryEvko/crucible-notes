# SparseCore vs Neuron MatmultSparse

> *Every TPU-side address, symbol, and literal on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`). Other versions differ. The AWS Neuron side is described at the architectural level only — Neuron lives in a different binary and carries no addresses on this page.*

## Abstract

Two vendors ship something called "sparse matmul," and the names collide in a way that invites the wrong conclusion. Google's TPU has **SparseCore** — an on-die coprocessor that gathers embedding rows out of HBM and feeds them to the TensorCore's dense MXU. AWS's Neuron compiler has **MatmultSparse** — a single instruction-set opcode that runs a structured-sparse dense matmul on the *same* PE systolic array as its dense matmul. This page settles whether they are the same kind of thing. They are not: they sit in **different architectural categories**, accelerate **orthogonal meanings of "sparse"**, and relieve **different bottlenecks**. The naming collision is misleading, and the true functional analogues cross over (TPU SparseCore's analogue is Neuron's `IndirectLoad` / `Gather` DMA family, not MatmultSparse; Neuron MatmultSparse's analogue is NVIDIA's structured-2:4 Sparse Tensor Cores, not SparseCore).

This is an *orientation / comparison* page, not a reimplementation page for either subsystem. The TPU side is owned in full by the sibling pages of Part IX — [SparseCore Overview](overview.md), [Architecture](architecture.md), [SC ↔ MXU Handshake](sc-mxu-handshake.md), [Stream Gather/Scatter](stream-gather-scatter.md), [Dedup Multiplicity](dedup-multiplicity.md), [SampleCombiner Emitter](sample-combiner-emitter.md) — and every TPU claim here is anchored to a symbol or address in `libtpu.so` already established on those pages. The Neuron side is the contrast: it is reconstructed from public Neuron architecture (the PE/State-Buffer/PSUM model, the 2:4 structured-sparse pattern, the `InstMatmultSparse` opcode story) and is presented at the architectural level only. No Neuron address is asserted, because no Neuron binary was analyzed for this wiki.

The page is structured as an **equivalence map** (the four structural stages where the two paths line up — embedding gather, sparse-index dedup, reduce-by-segment, dense-engine handoff) followed by the **divergences** (engine ownership, the meaning of "sparse," the data path, the cost-model units) and a closing verdict on where each model wins. Read it after the [SparseCore Overview](overview.md) and the [SC ↔ MXU Handshake](sc-mxu-handshake.md), which establish the TPU gather→reduce→MXU path this page compares against.

For orientation, the contract is:

- **The category, not the name, is the headline.** TPU SparseCore is a *dedicated peer coprocessor* (its own ISA, its own memory model, its own compiler pipeline); Neuron MatmultSparse is an *instruction-set mode* of one matmul engine. Comparing them as "two sparse-matmul implementations" mis-frames both.
- **"Sparse" means two unrelated things.** On the TPU it is *access-pattern* sparsity — arbitrary index-driven gather of rows from a multi-GB table, no fixed ratio. On Neuron it is *arithmetic* sparsity — a structured N:M (≈2:4) zero-mask baked into the weight matrix at compile time, a fixed ~50% column skip.
- **The bottleneck each relieves is different.** TPU SparseCore relieves HBM *random-access bandwidth* (keeping the MXU fed); Neuron MatmultSparse relieves *MAC throughput* (halving the matmul's exec cycles). One is a memory feature, the other a compute feature, and they do not even share units.
- **They do overlap structurally.** Both ultimately feed a 128×128-class systolic array; both use a side "tag/index" stream to drive a transformation; both are compile-time-planned and runtime-executed; both fuse a reduction/accumulation. The overlap is real but it is the *shape* of a gather-then-matmul pipeline, not equivalence of the sparse mechanism.

| | |
|---|---|
| **TPU artifact** | SparseCore — on-die coprocessor (SCS/TAC/TEC fabric), peer of the TensorCore |
| **TPU "sparse matmul" entry point** | `XlaSparseDenseMatmulOp` (+ Grad / CSR / optimizer-fused / minibatch variants) — confirmed in decompile |
| **TPU sparse meaning** | Arbitrary indexed gather/scatter (access-pattern sparsity); no fixed ratio |
| **TPU dense handoff** | `tpu_dma_hbm_to_vmem_sc_general` (16 operands) → TC VMEM → MXU; reduces over `Target::MxuContractingSize` |
| **TPU MXU sparse mode** | `Target::MxuSparseContractingSize` = **0 on every gen** — the MXU is dense-only; no structured-sparse skip |
| **Neuron artifact** | MatmultSparse — BIR opcode 9, runs on the PE systolic array (same engine as dense matmul) |
| **Neuron sparse meaning** | Structured N:M (≈2:4) weight-matrix mask (arithmetic sparsity); fixed ~50% column skip |
| **Equivalence verdict** | **NOT equivalent** — different architectural categories; the true analogues cross over |
| **Confidence** | TPU rows CONFIRMED (decompile-anchored). Neuron rows ARCHITECTURAL (no binary analyzed here) |

> **NOTE — provenance discipline on this page.** Every TPU claim is anchored to a `libtpu.so` symbol or address that one of the sibling pages already verifies. Every Neuron claim is an architecture-level statement with **no address**, because Neuron's compiler binary was not analyzed for this wiki. Where a Neuron mechanism is described concretely (opcode numbers, the three-instruction lowering, the `>> 1` cycle halving), treat it as *the public/architectural Neuron model* — the contrast that sharpens the TPU picture — not as recovered binary evidence. The two columns of every table below carry different epistemic weight, and the Confidence column says so.

---

## The Two Things Being Compared

### TPU SparseCore — a coprocessor that gathers

SparseCore is a separate piece of silicon, not a mode of the matmul engine. A SC-bearing chip carries a fixed **4:1 SC:TC ratio** (`xla::jellyfish::lowering_util::SparseCoreCountPerTensorCore` @ `0x1c6cb760`, which asserts `sparse_core_count % tensor_core_count == 0` and returns the integer 4 on every SC gen — see [SC ↔ MXU Handshake](sc-mxu-handshake.md#partition-assignment--which-sc-feeds-which-mxu)). Each SC is itself split into three VLIW sub-engines selected by `tpu::TpuSequencerType` — SCS (scalar/addressing, type 3), TAC (tile-access/DMA, type 4, removed on Trillium), TEC (vector compute, type 5) — established in [SparseCore Overview](overview.md#the-three-engine-classes).

What it accelerates is the **memory-access side** of sparse computation: the DLRM / recommender embedding-lookup pattern, where the cost is random HBM access to a multi-GB table, not arithmetic. Its defining primitive is the in-HBM atomic floating-point scatter-add (`STREAM_OPCODE_SCATTER_FLOAT_ADD`, paired with the DMA destination opcode `DMA_DEST_OPCODE_READ_AND_ADD`), which the MXU cannot do — see [Stream Gather/Scatter](stream-gather-scatter.md). SparseCore does **not** do the dense matmul. After it has gathered and reduced the embedding rows into a tile, the TensorCore's MXU consumes that tile and runs the dense MLP matmul. SC is the *feeder*; the MXU is the *consumer*.

### Neuron MatmultSparse — an opcode that skips zeros

MatmultSparse, by the public Neuron architecture, is one opcode among many in a single unified instruction stream (BIR), not a separate engine. It runs a structured-sparse matmul on the regular PE systolic array — the *same* array that runs the dense matmul. Its parent is the same abstract matmul base as the dense matmul, and its default engine is the PE engine, identical to dense. The extra state over a dense matmul is just a reference to a sparse mask: the instruction is "dense matmul plus a tag." Codegen lowers it to a short prologue-plus-matmul sequence on the PE — load the per-weight-tile sparsity tag, load the (compressed, zero-stripped) weight tile, run the matmul using the tag to skip the zeroed columns.

What it accelerates is the **arithmetic side** of a structured-sparse dense GEMM: a weight matrix pruned to a structured N:M pattern (most commonly 2:4 — two nonzeros out of every four), where the array skips the zero columns and the matmul completes in fewer cycles. There is no gather of arbitrary rows from HBM; the operands are already resident in the on-core State Buffer. Neuron handles embeddings — the workload TPU SparseCore exists for — through *ordinary indirect-load / gather DMA opcodes*, an entirely separate mechanism from MatmultSparse.

> **QUIRK — the names point at opposite halves of the same pipeline.** "SparseCore" and "MatmultSparse" both contain "sparse," and both live near a dense matmul, so the obvious reading is that they are competing implementations of one feature. They are not. SparseCore accelerates the *gather* that produces the matmul's input; MatmultSparse accelerates the *matmul* itself. A reimplementer who maps one onto the other will reproduce the wrong half of the system. The functional pairing crosses the naming: TPU SparseCore ≈ Neuron's gather/indirect-load DMA ops; Neuron MatmultSparse ≈ a structured-sparse *MXU mode* — which the TPU MXU does not have (`MxuSparseContractingSize` = 0).

---

## The Equivalence Map — Four Stages That Line Up

Despite the category mismatch, a recommender/embedding pipeline and a structured-sparse GEMM share the same four-stage skeleton: a side stream drives a data transformation, duplicate work is collapsed, a reduction is fused, and the result is handed to a dense systolic array. The two vendors implement each stage in a structurally analogous place. This is the genuine overlap — and naming each stage's TPU and Neuron realization is the fastest way to see exactly where the equivalence holds and where it breaks.

```text
STAGE            TPU SparseCore (coprocessor)            Neuron MatmultSparse (opcode mode)
─────            ────────────────────────────            ──────────────────────────────────
1 side-stream    index stream drives gather addresses    sparsity tag drives column-skip
   load          (TAC/TEC stream-start prologue)         (LoadTags prologue instruction)

2 dedup /        TEC DuplicateCountFloat / Uniquify       (no on-device dedup — the N:M mask
   collapse      collapse repeated embedding ids          is static, baked at compile time)

3 reduce /       TEC segmented reduce-by-sample           PSUM accumulate (CalcStart/Stop/Accu);
   accumulate    (+ in-HBM SCATTER_FLOAT_ADD on bwd)      same accumulate flags as dense matmul

4 dense          SC tile -> VMEM -> MXU systolic array    PE systolic array runs the sparse
   handoff       (separate engine, SFLAG handshake)      matmul itself (no engine crossing)
```

### Stage 1 — the side stream that drives the transformation

Both paths carry a side channel that is not the bulk data: it *steers* the bulk data. On the TPU, that side channel is the **index stream** — a stream of integer row indices that the SC stream engine turns into gather addresses `HBM[table_base + index_i * row_stride] → TILE_SPMEM` ([Stream Gather/Scatter](stream-gather-scatter.md)). On Neuron, the side channel is the **sparsity tag** — a per-weight-tile record of which columns survived pruning, loaded by a dedicated prologue instruction before the matmul runs. Both have a dedicated prologue to load the side data (TPU: the TAC/TEC stream-start; Neuron: the LoadTags opcode). The equivalence is real at this level of abstraction — *a tag/index stream drives a data transformation* — but the transformation it drives is opposite: TPU's stream picks *which rows to fetch from memory*; Neuron's tag picks *which columns to skip in the array*.

### Stage 2 — dedup / collapse of repeated work

Embedding lookups have duplicate indices: the same table row is requested by many samples in a batch, and gathering it once and broadcasting is cheaper than gathering it repeatedly. The TPU collapses duplicates on-device in the TEC, via the dedup/multiplicity primitives — `DuplicateCountFloat` (confirmed in the decompile as a TEC vector-extended op) counts repeats and the uniquify path emits each distinct id once with its multiplicity ([Dedup Multiplicity](dedup-multiplicity.md)). **Neuron has no analogue at this stage**, because its sparsity is *static*: the N:M mask was fixed at compile time when the model was pruned, so there is no runtime stream of indices to dedup. This is the first place the equivalence breaks — the TPU does runtime structure discovery on its index stream; Neuron's structure was decided before the program was built.

### Stage 3 — the fused reduction / accumulation

Both fuse a reduction into the path rather than materializing an intermediate and reducing in a separate pass. On the TPU, the TEC runs a **segmented reduce** — sum / weighted-sum / max over each sample's gathered rows ([SampleCombiner Emitter](sample-combiner-emitter.md)) — and on the backward pass fuses the gradient reduction into the in-HBM `STREAM_OPCODE_SCATTER_FLOAT_ADD`. On Neuron, the matmul accumulates into PSUM with the same `CalcStart` / `CalcStop` / `CalcAccu` flags the dense matmul uses; the structured-sparse matmul reuses the dense accumulation datapath wholesale. Both also carry an atomic-accumulate primitive in their broader toolkit (TPU: `SCATTER_FLOAT_ADD` into HBM; Neuron: a separate indirect-save-accumulate op). The structural match is genuine — *the reduction is fused, not a separate pass* — but the reduction is over different axes: TPU reduces *gathered rows per sample*; Neuron reduces *over the matmul's contracting dimension*.

### Stage 4 — the handoff to the dense engine

Both pipelines end at a 128×128-class systolic array, and this is the strongest structural equivalence. On the TPU the handoff *crosses an engine boundary*: the SC-produced tile lands in the TensorCore's VMEM via the one direct route (`tpu_dma_hbm_to_vmem_sc_general`, 16 operands, confirmed in the decompile), an SFLAG sync flag gates the MXU latch, and the MXU reduces over the dense `Target::MxuContractingSize` (128 base, 256 on the Ghostlite class) — see [SC ↔ MXU Handshake](sc-mxu-handshake.md#the-contracting-depth-binding). On Neuron there is *no engine crossing*: the sparse matmul **is** the PE-array run, the operands are already in the State Buffer, the output accumulates into PSUM. The shared fact is that a systolic array is the terminal consumer in both. The divergence is whether reaching it costs an inter-engine DMA + sync handshake (TPU) or nothing (Neuron, same engine).

> **GOTCHA — the TPU MXU never sees "sparse."** On the TPU, by the time the gathered/reduced tile reaches the MXU it is an ordinary dense activation tile; the sparsity was fully resolved upstream by the SC gather. `Target::MxuSparseContractingSize` (@ `0x1d4900c0`) is **0 on every generation** — no `Target` subclass overrides it — so the MXU runs the ordinary dense contracting datapath. There is no sparse-MXU latch on the embedding feed. This is the structural opposite of Neuron MatmultSparse, where the sparsity lives *inside* the matmul engine as a column-skip. A reimplementer must not look for a structured-sparse mode in the TPU MXU; on the TPU, sparsity is an upstream-coprocessor concern, end of story.

---

## The Divergences — Where They Are Not the Same

### Engine ownership

The headline divergence. TPU SparseCore is a separate silicon block: four SCs per TC, its own three-engine VLIW fabric, its own register files and bundle formats (`SparseCore{Scs,Tac,Tec}CodecBase` per generation, [SparseCore Overview](overview.md)). Neuron MatmultSparse runs on the *same* PE systolic array as the dense matmul — by the public Neuron cost model, the sparse and dense matmuls share the identical exec-latency function (it is reachable under both opcodes' names), which is the cleanest possible proof that there is one datapath, not two. On the TPU side the equivalent proof is the *opposite*: there is a whole separate engine, gated by `Target::SupportsSparseCore` (false on the pre-SparseCore Jellyfish/Dragonfish/Pufferfish gens — see [SC ↔ MXU Handshake](sc-mxu-handshake.md#partition-assignment--which-sc-feeds-which-mxu)), and the SC↔TC boundary is an explicit DMA + SFLAG handshake.

### The meaning of "sparse"

| Axis | TPU SparseCore | Neuron MatmultSparse |
|---|---|---|
| Sparsity lives in | the **access pattern** (which rows are read) | the **weight matrix** (which entries are zero) |
| Structure | unstructured — arbitrary indices, any order, duplicates | structured N:M (≈2:4) — fixed pattern within groups |
| Ratio | none — a window pulls 1 row or thousands, data-dependent | fixed ~50% column reduction |
| When decided | at runtime (indices arrive per minibatch) | at compile time (pruning bakes the mask into the weights) |
| HLO / format | CSR input (`XlaSparseDenseMatmulWithCsrInputOp`) describing which rows per sample | compressed (zero-stripped) weight tile + a position tag |

These are orthogonal meanings. TPU sparsity is *data-movement* sparsity — the matrix is dense, the *access* is sparse. Neuron sparsity is *arithmetic* sparsity — the access is dense, the *matrix* is sparse. The TPU's "sparse-dense matmul" name literally parses as "sparse (embedding) *lookup* feeding a dense matmul"; Neuron's "matmul-sparse" parses as "matmul whose *operand* is sparse." They use the same word for opposite properties.

> **QUIRK — the TPU has no fixed sparsity ratio, by design.** A reimplementer coming from the NVIDIA 2:4 world expects a fixed N:M ratio and a compressed-storage format. The TPU SparseCore has neither: the `VariableWindowAllocationEstimator` (@ `0x13ca2200`) does not encode any ratio — it checks an *inequality* (does this lookup window's variable-size id set fit in TILE_SPMEM?) and mini-batches if it does not ([embedding minibatching](embedding-minibatching.md)). The "sparsity" is the variable, data-dependent window size, not a structured mask. Hard-coding a 2:4-style ratio into a SparseCore reimplementation is a category error.

### The data path

The TPU data path is a **multi-hop pipeline through a separate engine, then over HBM/VMEM to the MXU**, with sync-flag handshakes at the engine boundary:

```text
indices ─► SCS (address calc) ─► TAC/TEC stream-gather
        ─► HBM[table + idx*stride] ─► TILE_SPMEM (per-tile SC SRAM)
        ─► TEC vector reduce (sum / weighted-sum / max)
        ─► TILE_SPMEM ─► (HBM) ─► VMEM ─► MXU latch ─► dense matmul ─► PSUM
```

The canonical SC→MXU fastpath is `SC TEC → TILE_SPMEM → HBM → VMEM → MXU`; there is exactly one direct SC→VMEM DMA route (`tpu_dma_hbm_to_vmem_sc_general`) and one TC→SC route, and everything else transits HBM ([SC ↔ MXU Handshake](sc-mxu-handshake.md#the-three-handoff-channels)). The pipeline is double-buffered — SC produces tile N while the MXU consumes tile N−1, coordinated by a two-flag SFLAG handshake.

The Neuron data path, by contrast, **never leaves the PE engine**. By the public architecture, the compressed weights, the tag, and the activations are all resident in the on-core State Buffer and must share a base partition; the matmul runs in the PE array and accumulates into PSUM. There is no HBM gather, no inter-engine DMA, no sync-flag handshake — the entire transformation is a prologue instruction plus a column-skip inside one engine.

### The cost-model units

The two performance models do not share units, which is the most concrete consequence of the category difference.

| | TPU SparseCore | Neuron MatmultSparse |
|---|---|---|
| Model type | **memory** model | **compute** model |
| Limiting resource | HBM random-access bandwidth (credit-arbitrated) | PE-array cycles |
| Metric | bytes/sec of random HBM access | MAC cycles, halved (~2× FLOPS) |
| Speedup type | latency-hiding (keep the MXU fed) | ~2× arithmetic on the matmul |
| Knobs | `GetSparseCoreHbmBandwidthAdjustmentFactor`, DMA credit / throttle | a single `>> 1` cycle halving on the exec phase |

On the TPU there is **no FLOPS speedup** — SC does little arithmetic; its job is to keep the MXU from stalling on HBM. The cost model is per-table HBM-usage estimates and a fractional-bandwidth share (`GetSparseCoreHbmBandwidthAdjustmentFactor`, referenced from `RunMemorySpaceAssignment`), with a starvation timeout (`FLAGS_xla_tpu_debug_sc_sflag_wait_timeout_ms`) catching the case where SC fails to produce a tile before the MXU needs it ([SC ↔ MXU Handshake](sc-mxu-handshake.md#bandwidth-and-back-pressure-at-the-boundary)). On Neuron the entire performance story is the structured-sparse cycle halving — a sparse matmul costs half the exec cycles of a dense matmul with the same output shape, modulo a fixed tag-load overhead that amortizes for large contracting depth. One model is bytes/sec; the other is MAC cycles. They are not comparable.

---

## Per-Generation Availability

### TPU SparseCore presence (CONFIRMED — `libtpu.so`)

SparseCore presence and the engine roster are read directly from the codec-class family namespaces and the `SparseCore<Engine>*` decompiled function counts ([SparseCore Overview](overview.md#per-generation-presence)):

| TPU gen | Codename | SparseCore | Engines | Notes |
|---|---|:--:|---|---|
| v3 / v4 | Jellyfish / Pufferfish | **NO** | — | BarnaCore era; `Target::SupportsSparseCore` = false |
| v5e | Viperfish | **YES** | SCS + TAC + TEC | first three-engine split |
| v5p | Ghostlite | **YES** | SCS + TAC + TEC | widened vector set; `MxuContractingSize` 256 |
| v6e | Trillium | **YES** | SCS + TEC (no TAC) | TAC folded into SCS+TEC; 0 `gfc::*SparseCoreTac*` symbols |

### Neuron MatmultSparse availability (ARCHITECTURAL — no binary analyzed)

By the public Neuron architecture, MatmultSparse is *not* present on the earliest inference-only generation (that arch's matmul is dense-only) and appears from the first training generation onward, inherited across the later cores as a PE-engine feature. This column carries **no addresses** and is included only as the contrast to the TPU column; treat the specifics as the architectural Neuron model, not recovered evidence.

| Neuron arch class | MatmultSparse | Notes |
|---|:--:|---|
| inference-gen-0 | **NO** | matmul is dense-only on this arch |
| training-gen (introduced) | **YES** | first arch with the sparse matmul opcode |
| later training gens | **YES** | inherited as a PE-engine feature; orthogonal microscaling/FP-format modes added separately |

> **NOTE — the TPU has no MatmultSparse analogue at all.** The closest TPU peer of Neuron MatmultSparse is *not* SparseCore — it is the dense MXU's own dtype/precision modes (e.g. the `MxuContractingSizeIsDoubled` 4-bit packed-nibble path, [SC ↔ MXU Handshake](sc-mxu-handshake.md#the-contracting-depth-binding)). And `Target::MxuSparseContractingSize` = 0 on every gen means the TPU MXU has *no* structured-sparse column-skip mode. The working conclusion is that the TPU handles all sparsity on the *memory* side (SparseCore) and runs a strictly dense MXU, while Neuron handles structured sparsity on the *compute* side and uses ordinary DMA for the memory side. The vendors split the sparse problem along opposite seams.

---

## The Verdict — Not Equivalent

The two are **not equivalent**; they occupy different architectural categories.

- **TPU SparseCore** is a *dedicated coprocessor* — a peer engine (4 SC : 1 TC) with its own ISA, memory model, compiler pipeline, and programming model — that accelerates the **gather/scatter memory pattern** of embedding lookups.
- **Neuron MatmultSparse** is an *instruction-set mode* of the existing matmul engine — one opcode on the same PE array as the dense matmul — that accelerates the **arithmetic** of a structured-sparse dense GEMM.

Stated three ways: (1) *Engine ownership* — SparseCore is separate silicon; MatmultSparse is the same array as dense matmul. (2) *Meaning of "sparse"* — TPU = the access pattern (gather indices); Neuron = the weight-matrix mask (structured N:M zeros). (3) *Bottleneck relieved* — TPU relieves HBM random-access bandwidth; Neuron relieves MAC throughput. The functional analogues cross over: TPU SparseCore ≈ Neuron's indirect-load / gather / scatter-accumulate DMA family; Neuron MatmultSparse ≈ NVIDIA's structured-2:4 Sparse Tensor Cores. The shared word "sparse" pairs the wrong things.

### Where each wins

| Workload | Winner | Why |
|---|---|---|
| Recommender / DLRM with multi-GB embedding tables | **TPU SparseCore** | purpose-built for random HBM gather + in-HBM atomic FP scatter-add; the MXU cannot do this at all |
| Pruned (2:4 structured-sparse) transformer / CNN weights | **Neuron MatmultSparse** | ~2× MAC-cycle reduction on a compute-bound GEMM; no equivalent on the dense-only TPU MXU |
| Dense matmul (no sparsity) | tie — both run their dense systolic array | sparsity machinery is bypassed on both |
| Embedding lookup *on Neuron* | Neuron's indirect-load / gather DMA, **not** MatmultSparse | MatmultSparse has no gather; embeddings go through ordinary DMA |
| Structured-sparse matmul *on TPU* | **no native support** | `MxuSparseContractingSize` = 0; the TPU MXU is dense-only |

> **NOTE — what is and is not recoverable here.** The TPU side of every claim above is anchored in `libtpu.so` (the `XlaSparseDenseMatmul*` family, the `tpu_dma_*_sc_*` routes, `SparseCoreCountPerTensorCore` @ `0x1c6cb760`, `MxuSparseContractingSize` @ `0x1d4900c0`, `DuplicateCountFloat`, `VariableWindowAllocationEstimator` @ `0x13ca2200`). The Neuron side is *architectural contrast only* — opcode numbers, the three-instruction lowering, the `>> 1` cycle model — drawn from the public Neuron architecture, not from any binary analyzed for this wiki, and therefore carries no addresses. The headline conclusion (different categories, crossed-over analogues) is robust to the Neuron details, because it rests on the *structural* facts of each system, not on either side's exact bit layout.

---

## Cross-References

- [SparseCore Overview](overview.md) — the TPU side in full: the SCS/TAC/TEC fabric, per-gen presence, the embedding data path.
- [Architecture](architecture.md) — SC geometry, the four-tier memory model, and the embedding datapath in depth.
- [SC ↔ MXU Handshake](sc-mxu-handshake.md) — the gather→VMEM→MXU boundary, the SFLAG sync, and the `MxuSparseContractingSize` = 0 fact this page leans on.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the indirect-DMA descriptor and the `STREAM_OPCODE_*` set (including the in-HBM atomic scatter-add).
- [Dedup Multiplicity](dedup-multiplicity.md) — the TEC `DuplicateCountFloat` / uniquify path that collapses repeated embedding ids (Stage 2 of the equivalence map).
- [SampleCombiner Emitter](sample-combiner-emitter.md) — the per-sample segmented reduce (Stage 3 of the equivalence map).
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore cross-cutting — [back to index](../index.md)
