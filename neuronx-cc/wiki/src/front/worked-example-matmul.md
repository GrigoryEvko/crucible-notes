# Worked Example A — a matmul end-to-end

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). Front-half addresses are VAs in the native ELF tools `hlo2penguin` / `hlo-opt` (`.rodata = VA−0x200000`, `.text = VA−0x201000`); the Penguin-IR and BIR codegen live in the Cython modules `BirCodeGenLoop.cpython-310…so` / `KernelBuilder.cpython-310…so`; the back-half encoders and packager are in `neuronxcc/starfish/lib/libwalrus.so` (`.text` base `0x62d660`, `.rodata` base `0x1c72000`, VA == file-offset). Other wheels differ; treat every address as version-pinned.*

## Abstract

This page follows **one program** — a single dense matmul — from the `dot` an ML framework hands across the PjRt boundary down to the 64-byte PE bundle bytes that land in the `PE.bin` member of the output NEFF. It is the orientation map for the whole compiler: a narrative spine that names each IR level, the data structure it lives in, the one function that drives the descent at that hop, and the deep page that owns the full detail. Read it first; then dive into the page each stage links to. The shape we trace is concrete throughout:

```text
  lhs : bf16 [128, 512]     (M=128 rows, K=512 contraction)
  rhs : bf16 [512, 256]     (K=512 contraction, N=256 cols)
  out : fp32 [128, 256]     (M×N, fp32 because PSUM is always fp32)
```

The descent is **not** the textbook MLIR cascade `HLO → MHLO → tpu → LLO` that a TPU reader might expect. neuronx-cc is a **process pipeline** ([Compile Pipeline](pipeline.md)): a Python driver forks a sequence of native tools and Cython jobs, and the IR crosses six levels — `HLO → MHLO → Penguin → BIR → per-engine ISA → NEFF`. Two facts about the *order* are the most common things to get wrong, and this page is built around correcting them. First, the MLIR front-half (`hlo2penguin`, the `HLOToTensorizer` job) runs **first** — it ingests the `dot`, runs the HLO passes, lowers MHLO→Penguin, and emits `penguin.py` — all **before** the `hlo-opt`-backed `Frontend` job. So Hop 3 (lowering) precedes Hop 2 (the second HLO-opt job) in wall-clock job order, even though the IR-level story reads "optimize, then lower." Second, `WalrusDriver` is a **single** Job whose ~18 backend stages — schedule, allocate, codegen, pack — are forwarded as `--walrus-passes` to one `walrus_driver` process, not separate jobs.

The reference frame is XLA-on-LLVM for the front half and a TPU-class systolic array for the back half. If you know XLA's `kDot`, MHLO's `dot_general`, and a weight-stationary MXU, you already own the endpoints; the genuinely Neuron-specific descent is the middle — the Penguin tile-and-loop IR, the `bir::InstMatmult` two-phase encoding, and the `CoreV{2,3,4}Gen` 64-byte bundle packer. Silicon-specific choices (SBUF/PSUM geometry, bundle width, accumulate semantics) enter late and are documented in the Part-1 architecture pages this walkthrough links to.

For reimplementation, the orientation contract is:

- The **eight hops** and the single function/symbol that drives each: `ConvertHloToMlirHlo` (`0x22175d0`) → `LayoutAssignment::AssignLayouts` (`0x29b4f50`, hlo-opt) → `MhloToPythonPrinter::printDotOp` (`0x20c5af0`) → `MatMulOp` (Penguin) via `DAGTiler` → `BirCodeGenLoop.codegenMatMulOp` (`0x14c7b0`) → `PreSched` + `coloring_allocator_sb/psum` (libwalrus) → `CoreV2GenImpl::visitInstMatmult` (`0x126a850`) → `NeffPackager::run` (`0x15307e0`).
- The **job-order inversion**: Hop 1 (HLO ingest) and Hop 3 (MHLO→Penguin) both happen inside the **first** job (`HLOToTensorizer`/`hlo2penguin`); the **second** HLO-opt job (`Frontend`/`hlo-opt`) is a separate invocation, and the exact split of HLO-level work between the two is **not fully resolved** from the binaries (the HLO-pass code is statically linked into *both* tools).
- The **two matmul-node spellings**: the HLO-facing Penguin op is `TensorContractOp` (from `mhlo.dot`); the tile-level, ISA-bound op that codegen consumes is `MatMulOp` (from `nisa.nc_matmul`). They are different classes at different layers — conflating them is the single most common Penguin-side error.

| | |
|---|---|
| **Front-door input** | XLA `HloModule` (proto or StableHLO/MLIR text) across the PjRt boundary (`libneuronpjrt`) |
| **Wire output** | a gzip-wrapped PAX tar (NEFF) whose `PE.bin` member is raw 64-byte PE bundles in program order |
| **Job pipeline builder** | `CompileCommand.buildPipeline` @ `0x619d0` ([Compile Pipeline](pipeline.md)) |
| **Default job order** | `HLOToTensorizer → InferGoldens → Frontend → StaticIOTranspose → WalrusDriver → Kelper → (NeffWrapper)` |
| **The matmul we trace** | one `bf16 × bf16 → f32` dot, contracting `K = 512` |
| **PE array** | 128 partitions (`K`) × 128 columns (`N`), fp32 PSUM accumulator ([PE Engine](../arch/pe-engine.md)) |

---

## Stage map at a glance

The eight hops our matmul passes through, the data structure it lives in at each, the one function/symbol that drives the descent there, the owning binary, and the page that owns the full story. **Job** marks which driver Job the hop runs inside (F0 = `HLOToTensorizer`/`hlo2penguin`, F2 = `Frontend`/`hlo-opt`, W0 = `WalrusDriver`/`walrus_driver`).

| # | IR level / data structure | Driving symbol (entry VA) | Binary | Job | Owning deep page |
|---|---|---|---|---|---|
| 1 | `HloInstruction` opcode `kDot` → `mhlo.dot_general` | `xla::ConvertHloToMlirHlo` (`0x22175d0`) | hlo2penguin | F0 | [Part 4](../hlo-opt/) |
| 2 | HLO + `Layout.minor_to_major` (sharding/layout) | `LayoutAssignment::AssignLayouts` (`0x29b4f50`); `IOLayoutNormalization::Run` (`0x1ed4aa0`) | hlo-opt | F2 | [Part 4](../hlo-opt/) |
| 3 | `mhlo.dot_general` → Penguin `DotOp` (`penguin.py`) | `MhloToPythonPrinter::printDotOp` (`0x20c5af0`) | hlo2penguin | F0 | [Part 4](../hlo-opt/) · [Part 5](../penguin/) |
| 4 | Penguin `MatMulOp` + tiled loop nest (PAG layout) | `DAGTiler` / `PGTiling` (Cython) | penguin `.so` | — | [Part 5](../penguin/) |
| 5 | `bir::InstMatmult` (operands, AccessPattern, fp32 PSUM) | `BirCodeGenLoop.codegenMatMulOp` (`0x14c7b0`) | BirCodeGenLoop `.so` | — | [Part 7](../bir/) |
| 6 | scheduled + SBUF/PSUM-allocated BIR | `PreSched` (`run_pre_sched` `0xca1490`); `…::SB_Allocator` (`0xa97750`) / `PSUM_Allocator` (`0xad9970`) | libwalrus | W0 | [Part 8](../walrus/) · [SBUF/PSUM](../arch/sbuf-psum-geometry.md) |
| 7 | 64-byte `NEURON_ISA_TPB_S3D3_MM_STRUCT` bundle pair | `CoreV2GenImpl::visitInstMatmult` (`0x126a850`) | libwalrus | W0 | [2.10 PE encoding](../isa/pe-matmul-encoding.md) · [PE Engine](../arch/pe-engine.md) |
| 8 | `PE.bin` tar member of the NEFF | `NeffPackager::run` (`0x15307e0`); `NeffFileWriter::writeArchiveFile` (`0x153e030`) | libwalrus | W0 | [Part 12](../formats/) |

> **GOTCHA — the wall-clock job order is not the IR-descent order.** Rows are numbered by IR level (the order a reimplementer thinks in), but the *process* runs Hop 1 → **Hop 3** → Hop 2 → Hops 4–8. The native `hlo2penguin` tool (Job F0) ingests the `kDot`, runs the HLO passes, lowers MHLO all the way to the `penguin.py` `DotOp`, and exits — *before* the `Frontend`/`hlo-opt` job (F2) is even forked. So Hop 2 below is described where it belongs in the IR story (layout, on HLO), but it executes in a **second, separate** HLO-opt invocation. The two corrections that follow this section make the consequence precise.

---

## Stage 1 — HLO `dot` ingestion: `kDot` → `mhlo.dot_general`

Our matmul enters as an XLA `HloInstruction` of opcode **`kDot`**, carried in an `xla::HloModule` (a protobuf, or StableHLO/MLIR text), handed to the compiler by the framework bridge through `libneuronpjrt`. This is the first job in the pipeline, the `HLOToTensorizer` Job, which forks the native tool **`hlo2penguin`** (`runHlo2Tensorizer`: *"Run hlo2penguin to produce penguin.py"*). The dot's `bf16` operands and contracting-dimension numbers ride the `HloInstruction`'s `DotDimensionNumbers`.

The native `main` (`0x1edc880`, `hilo/hlo2penguin/main.cc`) loads the module (`parseHloProtoInput` `0x21bebe0`, or the text path `runStableHLOFrontendPipeline` `0x1ee0ca0` → `xla::ConvertStablehloToHlo` → `CreateHloModuleFromProto` `0x21be0a0`), then drives `hilo::HiloFlatCompile` (`0x1efc7d0`) → `HiloCompileInternal` (`0x1efc4d0`) → `hilo::ConvertHloToMLIR` (`0x1efc1c0`). That last function runs the HLO pass set (`hilo::RunHloPasses` `0x1efa160`) on the `HloModule`, then calls the upstream OpenXLA importer **`xla::ConvertHloToMlirHlo` (`0x22175d0`)**, which translates the `kDot` into an `mhlo.dot_general` op inside a fresh `mlir::ModuleOp`. After this hop the dot is an MHLO op, not an XLA `HloInstruction`; everything from here to Hop 3 operates on the MHLO module. Every symbol named above has a matching `.asm` disasm sidecar in the `hlo2penguin` IDA set.

> **NOTE — `dot_general`, not a bespoke matmul op.** The importer emits the standard `mhlo.dot_general` (a plain rank-2 `mhlo.dot` is the degenerate single-contracting-dim case). There is no Neuron-private matmul op at the MHLO level — the divergence starts at Hop 3, where the Python printer maps `dot_general` onto a Penguin contraction node.

→ [HLO Ingestion / Part 4](../hlo-opt/) · job model: [Compile Pipeline](pipeline.md)

---

## Stage 2 — Layout & sharding: where the data lands

A `dot_general` says nothing about the physical `minor_to_major` order of its operands or which device holds which shard. **Sharding** runs first: a single-device matmul is `Replicated`, so for our example it is a no-op, but this is where a multi-chip matmul would gain its collectives ([collectives, Part 4](../hlo-opt/)). **Layout assignment** then decides physical layouts. The driver is the stock XLA engine `xla::LayoutAssignment::AssignLayouts` (`0x29b4f50`) with its constraint helpers `AddBackendConstraints` (`0x2995f70`), `SetOperandLayout` (`0x29a6450`), `SetInstructionLayout` (`0x29aa810`); the three Neuron-specific layout passes then force the canonical **descending / major-to-minor** order (`minor_to_major {rank-1,…,1,0}`): `IOLayoutNormalization::Run` (`0x1ed4aa0`, early), `EnsureDescendingLayoutInRoot::Run` (`0x1f84f40`, late), and `RelaxCollectivesLayoutConstraint` (`0x20025e0`, all-reduce only) ([layout passes, Part 4](../hlo-opt/)). For our matmul this just pins a 2-D `NC` descending layout on the LHS, RHS, and result — the layout that lets the systolic array stream operands without a transpose.

This hop is numbered 2 because layout is an HLO-level concern, but it executes in the `Frontend` Job (F2, `hlo-opt --passes=…`), forked **after** the `hlo2penguin` tool (F0) has already emitted `penguin.py`. Complicating the attribution further, the driving HLO-pass code — `IOLayoutNormalization` included — is statically linked into *both* tools: `IOLayoutNormalization::Run` sits at `0x1ed4aa0` in `hlo-opt` and at a different address, `0x1fb0370`, in `hlo2penguin`, the same source compiled twice.

> **NOTE — which job assigns the layout is INFERRED.** The layout machinery itself is unambiguous: stock `LayoutAssignment` plus the three Neuron passes. But the per-tool `--passes` subset is chosen at runtime by the Cython Jobs `HLOToTensorizer.so` / `Frontend.so`, which carry no IDA decompile, so the F0-vs-F2 split was not byte-traced. This is the same unresolved division [Compile Pipeline](pipeline.md) flags.

→ [Layout Assignment / Part 4](../hlo-opt/) · [Sharding & SPMD / Part 4](../hlo-opt/)

---

## Stage 3 — MHLO → Penguin: the `dot_general` becomes a `DotOp` in `penguin.py`

This is where the program leaves the XLA/MHLO world and enters Penguin, and it happens **inside the same `hlo2penguin` tool** as Hop 1. The MLIR pass pipeline is built *imperatively* (not from a textual pipeline string) by `hilo::registerMHLOPasses` (`0x1ee12f0`) — a flat ~32-step `addPass`/`addNestedPass` spine. Our `dot_general` flows through `CanonicalizeForTensorizer::runOnOperation` (`0x20869d0`, 8 sub-rewriters — none touch a clean dot), `TensorizerLegalizationPass::runOnOperation` (`0x2197940`, which names outputs via a `neuron.symName` ArrayAttr and inserts an `mhlo.CopyOp` only if an output aliases an input/constant), and `PenguinizeIO` (`createPenguinizeIOPass` `0x2089330`), and finally reaches the **terminal Python emitter** `MhloToPyPenguin::runOnOperation` (`0x20a9870`).

The terminal printer is the descent's hinge. `MhloToPythonPrinter::printOperation` matches the op's MLIR `TypeID` against `mhlo::DotGeneralOp` and dispatches to `print<mhlo::DotGeneralOp>` (`0x20c6d60`), which calls the shared emitter **`MhloToPythonPrinter::printDotOp` (`0x20c5af0`)**. `printDotOp` writes one Python statement into `penguin.py` constructing a Penguin contraction op with `srcs=[lhs, rhs]`, the destination, and the contracting/batch dimension lists lifted from the `dot_general`; `printDotOp` and both `print<…>` specializations have matching `.asm` sidecars. The emitted high-level Penguin node is **`TensorContractOp`** — the HLO-facing contraction class, with the `contract_axes` / `lhs_free_axes` / `rhs_free_axes` / `batch_mm_axes` axis model and a `swap_operands` stationary/streaming flag ([Penguin IR model](../penguin/)).

> **GOTCHA — `printDotOp` builds a `TensorContractOp`, not yet a `MatMulOp`.** The two Penguin matmul spellings are different classes at different layers. `TensorContractOp` (this hop) is the whole-tensor, HLO-facing node. The tile-level, ISA-bound **`MatMulOp`** that BIR codegen consumes (Hop 5) is produced *later*, by the tiling middle-end (Hop 4) and the NKI `nc_matmul` builder. A reimplementer who expects `printDotOp` to emit the ISA-bound op will be off by the entire middle-end.

→ [MhloToPyPenguin / Part 4](../hlo-opt/) · [Penguin IR model / Part 5](../penguin/)

---

## Stage 4 — Penguin `MatMulOp`: tiling and PAG layout

`penguin.py` re-enters the Python/Cython middle-end as Penguin IR, where the whole-tensor `TensorContractOp` is **tiled** into TPB-sized sub-computations and lowered to the ISA-bound `MatMulOp`. The tiler is the `DAGTiler` family in `PGTilingHelpers.py`, sequenced by `PGTiling` inside `PGLayoutTilingPipeline` (`LayoutTilingPipeline.py`) and analyzed per partition-group by `PGAnalysisForTiling.py` ([U02 tensorizer](../penguin/)). For a matmul the worker cuts two axis classes: `cutParAxes` cuts the **partition (P) axis to ≤ 128** — our `K = 512` contraction maps onto the 128 SBUF/PE rows, so it is split into **4 partition-block loop iterations** — and `cutFreeAxes` sizes the free tile so the result fits one PSUM bank (512 fp32 per bank), with the contraction axis protected by `is_tile_axis_all_in_contract` so it is *accumulated*, not spilled. The output is a `TiledDAG(loop_axes, partition_axes ≤ 128, free_axes)` and the PG (partition-group / "PAG") relation graph that the loop generator walks.

The tiled node is **`MatMulOp`** (with siblings `MatMulMXOp`, `MatMulSparseOp`), emitted by the NKI `KernelBuilder` from `nisa.nc_matmul(dst, stationary, moving)` and carrying `stationary`, `moving`, `perf_mode`, `tile_position`, `tile_size`, and `outputs=[psum]`. For our `[128,512]·[512,256]` the tiler produces a loop nest of `MatMulOp(stationary=weights, moving=ifmap, outputs=[psum])` calls, each contracting one 128-row partition block, with the `N = 256` free dimension spanning two 128-column tiles. These Penguin transforms live in Cython `.so` modules under `penguin/targets/transforms/`; the class names (`DAGTiler`, `PGTiling`, `MatMulOp`, `TensorContractOp`) and the cut helpers come from `__pyx` qualnames and verbatim docstrings, but the tiler's internal cut arithmetic is INFERRED from the worked examples — Cython routes attribute names through the module-state struct, which blocks a byte-traced call list.

> **NOTE — the partition axis is the contraction, and it never exceeds 128.** The PE array has 128 rows, so the `K = 512` reduction is *defined* to span four array passes; the tiler's `cutParAxes` is what materializes that "4" as a loop bound. This is the same 128-partition rule the [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md) page documents from the allocator side.

→ [Penguin tiling / Part 5](../penguin/) · [NKI `nc_matmul` / Part 6](../nki/)

---

## Stage 5 — BIR `InstMatmult`: operands, AccessPattern, and the fp32 PSUM accumulate

The tiled Penguin `MatMulOp` lowers to the backend IR node **`bir::InstMatmult`** by the **beta3** path: `BirCodeGenLoop.cpython-310…so` (docstring: *"Generate Backend IR from tensoriser IR at the TongaISAInst level"*). The per-instruction codegen is **`BirCodeGenLoop.codegenMatMulOp`** (`__pyx_pw` symbol, body @ `0x14c7b0`, ~27.8 KB — the largest codegen in the module). It builds three operand `MemoryLocation`s and binds them positionally, matching [PE Engine §Operand Roles](../arch/pe-engine.md):

```c
// BirCodeGenLoop.codegenMatMulOp  @ 0x14c7b0  — beta3 Penguin MatMulOp → bir::InstMatmult [IT8]
moving   = createMemLoc(op.moving)         // getArgument(0) — ifmap, streamed through the array
stationary = transformNeuronWeightTensor(op.stationary)   // getArgument(1) — weights, latched (rank-2 assert)
psum     = transformNeuronPSUMTensor(op.outputs[0])       // getOutput(0)  — fp32 PSUM destination
inst = birpy.Instruction("Matmult")        // addInstToBir: set_name, set_engine[PE=3], addInstruction
attach AccessPattern(partition_ap, free_aps) to each operand   // sNdM AccessPattern, libBIR Argument
inst.perf_mode = None                      // InstMatmultBase+0x2D0 — plain bf16, no DoubleRow fold
inst.is_accumulate = ...                   // recorded; START/STOP NOT finalized here (see below)
```

`InstMatmult` is IR-type **IT8**; its default engine is `EngineType::PE` (`= 3`, `getDefaultEngine` @ libBIR `0x3e2cb0`). Each operand's shape and strides are carried by an `AccessPattern` — the partition axis (`Pattern[0]`, the contraction) folds into a base-partition term rather than a loop word, and the free dimensions iterate `Pattern[1..3]` ([E12 AccessPattern](../bir/)). The accumulator is **always fp32 in PSUM regardless of the `bf16` inputs** — `PSUMLegalization::widen_psum` widens any sub-4-byte matmul destination to a 4-byte PSUM cell.

> **GOTCHA — codegen records the accumulate intent but does *not* set the START/STOP flags.** `codegenMatMulOp` notes `is_accumulate` and leaves the PSUM-bank command flags to a later legalizer. That legalizer is `LegalizeMatmulAccumulationGroups` (registry order 94, `::run` @ `0x16e4120`): it groups matmuls by PSUM destination, clusters physically overlapping banks via `find_overlapped_psum_zero_region_groups` (`0x16dee50`), then sets **head → `setCalcStart`** (zero the bank on the first of the four `K`-tiles) and **tail → `setCalcStop`** (drain into fp32 PSUM on the fourth). The flag byte itself (`{bit0 START, bit1 STOP, bit2 ACCUMULATE}`) is not written until the encoder (Hop 7). A reimplementer who emits the calc bits during BIR codegen will fight the legalizer that owns them. See [PE Engine §Accumulate Chain](../arch/pe-engine.md).

> **NOTE — two routes into `bir::InstMatmult`, one node.** The live build uses **beta3** (`BirCodeGenLoop` → `birpy.Instruction("Matmult")`, emitting BIR directly — it does *not* pass through KLR). The older **beta2** route (`KlirToBirCodegen` in libwalrus, via KLR) is a parallel front-end onto the same IT8 node, dormant for internal re-tracing in this build ([Part 7](../bir/)).

→ [BIR Instruction Hierarchy / Part 7](../bir/) · [PSUM accumulation / Part 8](../walrus/) · [PE Engine](../arch/pe-engine.md)

---

## Stage 6 — Walrus: schedule, then SBUF/PSUM allocation

The BIR module enters the `WalrusDriver` Job — a single process (`walrus_driver` → `libwalrus.so`) that runs ~18 backend stages as `--walrus-passes`. Two of them place our matmul in space and time. **Scheduling** is `PreSched` (registry pass `pre_sched`, factory `0x3e00cc5`; real body `run_pre_sched` @ `0xca1490`), the first, dependency-only list scheduler: it builds a `MemoryLocation` DAG from the BIR producer→consumer edges, computes a unit-weight critical-path priority (`compute_depths` fwd `0xc8f610` / bwd `0xc8f270`, plus a "fringe" term), and emits a linear order — no hardware-latency cost model at this stage ([pre_sched, Part 8](../walrus/)).

**Allocation** then colors tensors into the two on-chip memories. The SBUF allocator is `neuronxcc::backend::ColoringAllocatorWithLoop::Rep::SB_Allocator::SB_Allocator` (`0xa97750`, pass `coloring_allocator_sb`), which reads the per-partition SBUF byte budget and colors each tensor as a `partitions × bytes` rectangle. The PSUM allocator is the sibling `…::Rep::PSUM_Allocator::PSUM_Allocator` (`0xad9970`, pass `coloring_allocator_psum`), a 1-D bank-interval colorer. For our `fp32 [128,256]` result, the per-partition footprint is `256 × 4 = 1024 B`, and `getPsumBankNum` (`0x1088bd0`) computes `divideCeil(1024, 2048) = 1` PSUM bank — `getPsumBankNum` is `divideCeil(bytes, getPsumBankSize())`, and `getPsumBankSize` is 2048 for gen2+. The four `K`-tile sub-matmuls all accumulate into that **one** bank, which is exactly why the accumulation-group legalizer (Hop 5) keeps the group inside a single pow2-aligned bank range. The full SBUF/PSUM geometry — 128 partitions, 8 banks × 2048 B (gen2+) — is on the [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md) page.

> **NOTE — the `0xa97750` / `0xad9970` symbols are the *nested* `ColoringAllocatorWithLoop::Rep` constructors,** not the free-standing `neuronxcc::backend::SB_Allocator` / `PSUM_Allocator` classes (whose `allocate` bodies are at `0x9d1b10` / `0xa3ad90`). Cite the nested qualified name to be unambiguous. The "4 array passes for `K=512`" count is INFERRED (geometry-consistent: 512 / 128 = 4), reached through the array-row rule and the accumulate-group legalizer rather than a single byte-pinned literal.

→ [PreSched / Part 8](../walrus/) · [SBUF/PSUM allocators / Part 8](../walrus/) · [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md)

---

## Stage 7 — `CoreV{2,3,4}Gen`: the 64-byte PE bundle pair

The `codegen` walrus pass (registry pass `codegen`, name-string `0x3e03d40`) lowers each `bir::InstMatmult` to its wire form by dispatching to the per-generation encoder. For gen2 (Sunda/Trainium) that is **`CoreV2GenImpl::visitInstMatmult` (`0x126a850`)** — the gen3 sibling is `CoreV3GenImpl::visitInstMatmult` (`0x1368750`). The encoder is a weight-tile-cache orchestrator holding a `std::map<pair<u8,u8>, WeightTileState>` RB-tree (CoreV2 `this+704`). On a cache **miss** (or tiling change) it emits the systolic two-phase pair — `generateLoadWeights` (`0x1258500`, op 1: stage the stationary weight tile into the array latches) then `generateMatMul` (`0x1248650`, op 2: stream the moving ifmap, drain to PSUM). On a **hit** (same weights tile still warm) it skips the load and emits `MatMul` only. Because our example is `bf16` inputs into an `fp32` PSUM accumulator — *not* an `fp32` matmul — it takes this normal single-pass, cache-using path, **not** the fp32 two-FMA two-pass reload ([PE Engine §Two-Phase Protocol](../arch/pe-engine.md)).

Each phase fills a 64-byte `neuronxcc::core_v2::NEURON_ISA_TPB_S3D3_MM_STRUCT`: the buffer is zero-initialized, then the control band is written — dtype at `+0x20`, perf-mode at `+0x21`, the moving-ifmap `TENSOR3D` AP at `+0x10`, the PSUM-dst AP at `+0x30`, the row/col tile-group bytes at `+0x2C`/`+0x2D`, and the **accumulate byte at `+0x2B`** (`or [r15+0x2B],1` START @ `0x124963d`, `or …,2` STOP @ `0x1249664`, `or …,4` ACCUMULATE). The four `K`-tile matmuls are sequenced START → (interior) → STOP across this byte, realizing the single-bank accumulation chain from Hop 5/6. The struct is then committed with **`fwrite(buf, 1, 0x40, bin)` (`0x1249699`)** — written directly into the PE engine's `.bin` stream. The addresses match those on the [PE Engine](../arch/pe-engine.md) page. The bit-exact field layout of the 64-byte bundle is [2.10 PE matmul encoding](../isa/pe-matmul-encoding.md).

→ [2.10 PE matmul encoding](../isa/pe-matmul-encoding.md) · [PE Engine](../arch/pe-engine.md)

---

## Stage 8 — the NEFF `PE.bin` member

The `fwrite` at Hop 7 has already streamed the PE bundles, in program order, into a per-engine `.bin` file. The final walrus pass, `neff_packager` (registry pass `neff_packager`), assembles those files into the output NEFF. The driver is **`NeffPackager::run(vector<unique_ptr<bir::Module>>&)` (`0x15307e0`)** (cite the vector overload — there is also a `Module&` overload at `0x151ec00`). It builds the Bill-Of-Materials (`writePackageFile` `0x15200e0`), emits `def.json` (`writeDefJson` `0x152a0e0`) whose `["definition"]["pe"]["_instr"]` token resolves to the on-disk basename, and hands the BOM map to **`NeffFileWriter::writeArchiveFile` (`0x153e030`)**, which writes the container with libarchive (`archive_write_set_format_pax` + `archive_write_add_filter_gzip`).

The result is the crucial structural fact about a NEFF: **it is not an ELF.** It is a libarchive **PAX-format tar stream**, gzip-wrapped by default — its leading bytes are the gzip magic `0x1F 0x8B` (or the tar `ustar` magic at offset `0x101` with `--uncompressed`), never an ELF magic and never a bespoke "NEFF" magic. `nm` shows the complete libarchive tar API and not one ELF-writer symbol. Its "sections" are tar **members**: the per-engine instruction streams `PE.bin`, `Pool.bin`, `Activation.bin`, `SP.bin`, `DVE.bin` (raw 0x40-byte bundles in program order; DMA descriptors fold into the issuing compute engine's `.bin`, so there is no `DMA.bin`). The basename `PE` comes from `bir::EngineInfo2string` (TitleCase); the `def.json` token `pe` is the lowercase formatter spelling — the BOM maps the on-disk `PE.bin` path to its in-tar member name. The `neff_header` POD (version 2, UUID, engine bitmap) is **not** a leading record — it lives inside the `info.json` member. After this hop, our matmul is 64-byte PE bundles inside `PE.bin` inside a gzipped tar, ready for the runtime to extract and feed to the PE engine's instruction queue — the descent is complete.

→ [NEFF container / Part 12](../formats/) · [Kelf / Part 12](../formats/)

---

## Two things the common mental model gets wrong

> **GOTCHA — the front half is "ingest + lower" in one tool, then a *second* optimize job.** The natural reading puts HLO optimization (`hlo-opt`) before HLO→Penguin lowering (`hlo2penguin`). The decompiled `collectFrontendPipeline` emits them the other way round: `HLOToTensorizer` (`hlo2penguin`) is **F0** and lowers the dot all the way to `penguin.py`; the `hlo-opt`-backed `Frontend` job is **F2**, a separate later invocation. Hops 1 and 3 are both F0; Hop 2 is F2. Because the same HLO-pass code is statically linked into both tools, every F0-vs-F2 attribution on this page is INFERRED.

> **GOTCHA — `MatMulOp` and `TensorContractOp` are different Penguin nodes.** `printDotOp` (Hop 3) emits the whole-tensor, HLO-facing `TensorContractOp`. The tile-level, ISA-bound `MatMulOp` that BIR codegen consumes (Hop 5) is produced later, by the tiling middle-end (Hop 4) and the NKI `nc_matmul` builder. "MatMulOp" is the right name at the codegen hop and the wrong name before tiling; the two converge only once the tiler has run.

## Reading the rest of the book from here

| If you want… | Start at |
|---|---|
| The driver Jobs and the canonical pipeline order | [Compile Pipeline](pipeline.md) |
| The HLO→MHLO→Penguin lowering in full | [Part 4](../hlo-opt/) |
| The Penguin tile-and-loop IR and the tiler | [Part 5](../penguin/) |
| The NKI front door into Penguin | [Part 6](../nki/) |
| `bir::InstMatmult` as a `bir::Instruction` node | [Part 7](../bir/) |
| The walrus schedule/allocate/codegen passes | [Part 8](../walrus/) |
| The systolic-array datapath and accumulate semantics | [PE Engine](../arch/pe-engine.md) |
| The SBUF/PSUM address spaces and bank model | [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md) |
| The bit-exact 64-byte PE bundle layout | [2.10 PE matmul encoding](../isa/pe-matmul-encoding.md) |
| The NEFF tar/JSON wire format | [Part 12](../formats/) |
| How a claim's confidence is graded | [Methodology](../methodology.md) |

## Cross-References

- [The Compile Pipeline at a Glance](pipeline.md) — the Job order and IR descent this page follows for one operator; do not contradict its F0→F1→F2 sequence.
- [HLO-opt / Part 4](../hlo-opt/) — Hops 1–3; `kDot` import, layout, and the MHLO→Penguin printer.
- [Penguin / Part 5](../penguin/) — Hop 4; the `TensorContractOp` → tiled `MatMulOp` middle-end.
- [NKI / Part 6](../nki/) — the `nc_matmul` builder that emits the tile-level `MatMulOp`.
- [BIR / Part 7](../bir/) — Hop 5; `bir::InstMatmult` (IT8), its operands and AccessPattern.
- [Walrus / Part 8](../walrus/) — Hop 6; `PreSched`, the SBUF/PSUM allocators, and the accumulate-group legalizer.
- [PE Engine — the Systolic Matmul Array](../arch/pe-engine.md) — Hop 7; the two-phase protocol, weight cache, and fp32 PSUM accumulate the encoder realizes.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — Hop 6; the 128-partition SBUF rectangle and the 2048-byte PSUM bank our result occupies.
- [2.10 PE Matmul Encoding](../isa/pe-matmul-encoding.md) — Hop 7; the bit-level 64-byte bundle this page describes functionally.
- [Formats / Part 12](../formats/) — Hop 8; the NEFF PAX-tar container and the `PE.bin` member.
- [Methodology & the Confidence Model](../methodology.md) — the grounding ladder used throughout.
