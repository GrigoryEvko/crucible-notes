# The Compile Pipeline at a Glance

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/driver/`). Other wheels differ; treat every address as version-pinned.*

## Abstract

A single `neuronx_cc` invocation is a **process pipeline**: the driver parses the compile arguments, builds an ordered list of *Job* names, and runs each Job in sequence — most of them by forking a standalone tool ELF and waiting on it. This page is the orientation map. It gives both views the rest of the book needs — the conceptual IR descent (HLO → Penguin → BIR → per-engine ISA → NEFF) and the *actual* job schedule the driver emits — and it marks where the naive reading of the two is wrong.

The job pipeline is assembled in one place: `CompileCommand.buildPipeline` (`0x619d0`, `CompileCommand.py:1146–1349`) concatenates three collector sub-lists — `collectFrontendPipeline` + `collectWalrusPipeline` + `collectBackendPipeline` — into a flat name list, which `JobRegistry.makePipeline` (`0xbb80`) turns into `Job` instances, and `runPipeline` (`0x7a3a0`) executes sequentially. The string list those three collectors return **is** the canonical compile order. There is no hidden scheduler; the pipeline shape is exactly what those three functions concatenate.

Two facts shape everything downstream and are easy to get wrong. First, the MLIR front half (`hlo2penguin`, emitted as the `HLOToTensorizer` job) runs **first** — before the `hlo-opt`-backed `Frontend` job and before the golden reference evaluator — so the "optimize HLO, then lower to Penguin" mental order does not match the process order. Second, `WalrusDriver` is a **single** Job whose ~18 backend stages (`lsa`, `coloring`, `pre_sched`, …) are forwarded as `--walrus-passes` to one `walrus_driver` process, not separate jobs.

| | |
|---|---|
| **Pipeline builder** | `CompileCommand.buildPipeline` @ `0x619d0` (py 1146–1349) |
| **Name → Job map** | `JobRegistry.makePipeline` @ `0xbb80` (py 84–92) |
| **Executor** | `CompileCommand.runPipeline` @ `0x7a3a0` → `Pipeline.runSingleInput` @ `0xe030` |
| **Default job order** | `HLOToTensorizer → InferGoldens → Frontend → StaticIOTranspose → WalrusDriver → Kelper → (NeffWrapper)` |
| **Front doors** | XLA HLO (PyTorch-XLA / JAX via `libneuronpjrt`) · NKI Python (`@nki.jit`) |
| **Wire output** | NEFF — a gzip-wrapped PAX tar of JSON + per-engine `.bin` members |

## The driver job pipeline

`buildPipeline` returns the concatenation of three collectors. The table is the default flow (no special flags, `--optlevel 2`); the **Gate** column says when each job is present. Job-name strings, tools, and gating py-lines are read from the decompiled `CompileCommand` / `Pipeline` / `JobRegistry` Cython modules.

| # | Job (name string) | Runs as | Gate | Owning part |
|---|---|---|---|---|
| F0 | `HLOToTensorizer` | `hlo2penguin` (forked) | always | [Part 4](../hlo-opt/) |
| F1 | `XLAInferGoldens` / `InferGoldens` | `xla_infergoldens` / in-proc | unless `--meta-module`; `XLA*` iff `framework==XLAInterface` | [3.16](../frontend/xla-infergoldens.md) |
| F2 | `Frontend` | `hlo-opt` (`--passes=…`) | always | [Part 4](../hlo-opt/) |
| F3 | `StaticIOTranspose` | in-process Python (`io_transposes.json`) | unless `--meta-module` | [3.15](../frontend/static-io-transpose.md) |
| W0 | `WalrusDriver` | `walrus_driver` → `libwalrus.so` | always | [Part 8](../walrus/) |
| B0 | `Backend` | emits `backend.bir` | legacy-standalone flow only | [Part 8](../walrus/) |
| B1 | `Kelper` | NEFF linker / writer (`calcNeffVersion`) | with the legacy codegen block | [Part 12](../formats/) |
| B2 | `NeffWrapper` | `hlo-neff-wrapper` (forked) | `--enable-internal-neff-wrapper` | [Part 12](../formats/) |

> **GOTCHA — `WalrusDriver` is one Job, not a chain.** `collectWalrusPipeline` (`0x29e00`, py 1655–1725) returns the single-element list `["WalrusDriver"]`. The ~18 stage names it appears to contain — `lsa`, `coloring`, `unroll`, `lower_ac`, `pre_sched`, `partitioner`, `bir_splitter`, `coloring_allocator` / `linear_scan_allocator`, `dma_optimization`, `anti_dependency_analyzer`, `vn_splitter`, `post_sched`, `tensorcopy_accel`, `dep_reduction`, `print_metrics`, and finally `birverifier` / `bir_sim` — are accumulated into `self.walrus_passes` and handed to the one `walrus_driver` process as `--walrus-passes`. They are *backend pass* names, not driver Jobs. See [Part 8](../walrus/).

> **GOTCHA — `--optlevel` does not add or remove top-level jobs.** It is a string (`"0".."3"`, default `"2"`) that tunes the `--walrus-passes` set and the `enable_internal_*` flags the collectors read. Only indirectly — when it flips `enable_internal_new_backend` or selects the modular / bir-linker flow — does it change which jobs appear. The Backend sub-list (B0–B2) is suppressed entirely in the modular, bir-linker, and legacy-standalone flows; in the common modular path `walrus_driver`'s output is consumed directly and `Backend`/`Kelper` never run.

## The IR descent

Underneath the job schedule, the program descends six IR levels. The mapping to jobs is not one-to-one — the heavy HLO→Penguin lowering and HLO optimization are split across the `HLOToTensorizer` (hlo2penguin) and `Frontend` (hlo-opt) jobs, and the exact division of HLO-level optimization between those two invocations is not fully resolved from the binaries (both carry `runXLAFrontend` / `runPenguin` entry strings). What *is* firm is the level sequence and the binary that owns each.

```text
  framework graph ──libneuronpjrt──►  XLA HLO  (HloModule protobuf)
                                          │
        ┌─────────────── hlo-opt / hlo2penguin (the F0+F2 jobs) ───────────────┐
        │  HLO / MHLO / StableHLO / CHLO  →  Penguin Python IR (.py emission)   │
        │  collectives→custom-call, layout, quantize legalize, NeuronOpFusion,  │
        │  TensorizerLegalization, PenguinizeFunctions, MhloToPythonPrinter     │
        └──────────────────────────────────────────────────────────────────────┘
                                          │
   Penguin IR  (penguin.* Cython galaxy)  ◄────── NKI @nki.jit traces in here directly
        │  middle-end: layout, tiling, fusion, scheduling; ISL glue over stock islpy
        │
        │   ┌─ beta3 (live):    BirCodeGenLoop.cpython  →  bir::Instruction directly
        ▼   └─ beta2 (dormant on re-trace): KLR → KlirToBirCodegen (in libwalrus)
   BIR  (libBIR.so; bir::Module→Function→BasicBlock→Instruction; 110-opcode enum)
        │
        │   WalrusDriver job → walrus_driver (708 KB shim) → libwalrus.so (65 MB)
        ▼
   Per-engine ISA  (PE / Pool / Activation / DVE / SP — 64-byte bundles)
        │   NeffPackager (in libwalrus) / Kelper / NeffWrapper
        ▼
   NEFF on disk
```

The six levels, the dialect each speaks, and the part that owns the full detail:

| IR level | Form | Owned by | Part |
|---|---|---|---|
| HLO | `HloModule` protobuf | `hlo-opt` `--passes` | [Part 4](../hlo-opt/) |
| MHLO / StableHLO / CHLO | MLIR dialects | `hlo2penguin` ingestion | [Part 4](../hlo-opt/) |
| Penguin IR | tile-and-loop SSA (Python+Cython) | `penguin.*`; `pelican::Expr` index algebra | [Part 5](../penguin/) |
| NKI | traced kernel → Penguin IR | `KernelBuilder.NeuronCodegen` (unstripped) | [Part 6](../nki/) |
| BIR | one inst per HW instruction | `libBIR.so`; `BirCodeGenLoop` ∥ `KlirToBirCodegen` | [Part 7](../bir/) |
| Per-engine ISA | 64-byte bundles | `CoreV{2,3,4}Gen` in `libwalrus` | [Part 2](../isa/) |

> **NOTE — two routes into BIR.** The live path in this build is **beta3**: `BirCodeGenLoop.cpython` lowers Penguin IR straight to `bir::Instruction`. The older **beta2** route through KLR and `KlirToBirCodegen` (in `libwalrus`) is retained but dormant for internal kernel re-tracing here. A reimplementer needs only beta3 for current behavior; beta2 is documented for completeness in [Part 7](../bir/). The two converge on the same `bir::` node.

## Execution & failure model

`runPipeline` (`0x7a3a0`, py 1513–1572) initializes `GlobalState`, `chdir`s into the work directory (logging `"Intermediate files stored in %s"`), and drives `Pipeline.runSingleInput` (`0xe030`), which runs each Job under a timer and `GlobalState` scope (`"Starting job %s"` / `"Finished job %s"`). Each native Job is a `subprocess.Popen(...).communicate()` over its tool ELF. The whole subcommand runs inside a `multiprocessing.Process` that `CommandDriver.run` (`0x1c510`) joins; the child's `exitcode` is routed through `handleError` (`0x30470`) to one of three fatal diagnostics — `[F134]` (abnormal termination), `[F137]` ("forcibly killed", a negative exitcode, typically SIGKILL/OOM), or `[F139]` (a second abnormal-termination variant). The precise exitcode→code thresholds are a derived branch (INFERRED); the ownership in `CommandDriver` is CONFIRMED.

## Two corrections to the common mental model

> **CORRECTION — the job order is not "optimize then lower".** A natural reading puts `hlo-opt` (HLO optimization) before `hlo2penguin` (HLO→Penguin lowering). The decompiled `collectFrontendPipeline` (py 1636–1648) emits them the other way: `HLOToTensorizer` (hlo2penguin) is F0, the golden evaluator is F1, and the `hlo-opt`-backed `Frontend` job is F2. Whatever HLO-level optimization the `Frontend` job performs, it is scheduled after the tensorizer job, not before it.

> **CORRECTION — `Watchpoint`, `SaveTemps`, and `BIRVerifier` are not collector jobs.** `Watchpoint` appears only as a name-equality special-case in `buildPipeline`'s post-`makePipeline` loop (`_Pyx_PyUnicode_Equals(name, "Watchpoint")` at `0x619d0`); none of the three collectors emit it, and the other two are debug/aux jobs wired by separate paths. A reimplementer building the pipeline from the three collectors will not — and should not — produce them.

## Cross-References

- [Binary Inventory & the .so Map](../reference/binary-inventory.md) — the tools and libraries each job runs, with sizes and roles.
- [Methodology & the Confidence Model](../methodology.md) — how the job order and the IR descent are evidenced, and what the confidence tags mean.
- [3.3 CompileCommand pipeline & the canonical job order](../frontend/compilecommand-pipeline.md) — `buildPipeline` / `runPipeline` in full *(planned)*.
- [3.4 JobRegistry & the sub-tool process model](../frontend/job-registry.md) — name→class resolution and the forked-tool model *(planned)*.
- [Worked Example A — a matmul end-to-end](worked-example-matmul.md) — this descent followed for one operator *(planned)*.
