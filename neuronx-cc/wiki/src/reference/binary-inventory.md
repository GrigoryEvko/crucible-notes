# Binary Inventory & the .so Map

> *Sizes and paths are from the `neuronx_cc-2.24.5133.0+58f8de22-cp310` wheel. cp311/cp312 differ only as noted under [Version Provenance](versions.md).*

## Abstract

`neuronx_cc` is not one binary; it is a Python package that orchestrates a handful of large standalone tool ELFs, a set of eight C++ shared libraries under `starfish/lib`, and several hundred Cython extension modules that hold the Penguin middle-end and the NKI frontend. This page is the atlas: where the compiler's logic actually lives, so that when a later page says "in `libwalrus.so`" or "the `BirCodeGenLoop` module", the reader knows which artifact to open. Sizes are a rough proxy for how much logic each carries.

The package root is `neuronxcc/`. Three sub-trees matter: `starfish/` (the C++ compiler core — tools, libs, and the Cython `penguin` middle-end), `nki/` (the kernel DSL), and `driver/` (the Python/Cython command layer that drives everything).

## The standalone tool ELFs

These live in `neuronxcc/starfish/bin/` and are forked by the driver as subprocesses. The five largest are **five distinct ~230 MB statically-linked ELFs**, each with its own `main` — *not* one multi-call image. This is verifiable: they have distinct inodes and differing sizes (229–234 MB). They are similar in size because each statically links the same bulk of XLA / MLIR / LLVM. Each tool's link-count of 3 is **cross-wheel** sharing — the file is hardlinked into the cp310, cp311, and cp312 wheel directories — so a given tool is byte-identical across the three wheels, but the five tools are not copies of one another.

| Tool | Size | Role | Documented in |
|---|---|---|---|
| `xla_infergoldens` | 234 MB | Reference (golden) evaluator — runs HLO on a CPU oracle for numeric checking | [3.16](../frontend/xla-infergoldens.md) |
| `hlo2penguin` | 232 MB | MLIR front half: mhlo/stablehlo → Penguin Python IR | [Part 4](../hlo-opt/) |
| `hlo-opt` | 229 MB | HLO/MHLO/StableHLO optimizer; the `--passes` registry | [Part 4](../hlo-opt/) |
| `hlo-neff-wrapper` | 227 MB | Wraps backend output into the NEFF (the "Kelper" stage) | [12.x](../formats/) |
| `snapshot-unpack` | 226 MB | Unpacks a decomposed/snapshot compile input | [3.17](../frontend/snapshot-format.md) |
| `walrus_bugpoint_driver` | 36 MB | Delta-debugging driver over the backend | [Part 8](../walrus/) |
| `coloring_allocator_with_loop` | 3.8 MB | Standalone register/memory allocator (loop-aware) entry | [8.16](../walrus/coloring-allocator.md) |
| `full_unroll` | 2.9 MB | Standalone loop-unroll pass entry | [8.3](../walrus/unroll.md) |
| `walrus_driver` | 708 KB | **Thin shim** — argument marshalling; the backend itself is `libwalrus.so` | [3.7](../frontend/walrus-driver-cli.md) |

> **QUIRK —** `walrus_driver` is under a megabyte, but it is the entry point to the entire backend. It is a launcher: it parses the backend CLI, `dlopen`s `libwalrus.so`, and calls in. Do not look for scheduling or allocation logic in the driver — it is all in the 65 MB library.

Two Python tools also ship in `bin/`: `analyze_neff_artifacts.py` (a `json.load` + `os.stat` walk over an extracted NEFF directory) and `internal_nki_stubgen.py`.

## The `starfish/lib` shared libraries

Eight C++ `.so` carry the backend, the IR core, the simulators, and logging. They are loaded by `walrus_driver` and by the Cython modules.

| Library | Size | Owns | Documented in |
|---|---|---|---|
| `libwalrus.so` | 65 MB | **The backend**: ~150 passes, the dependence graph, the three schedulers, the SB/PSUM/DRAM/Reg allocators, loop optimization, LNC split, `lower_dma`, the `CoreV{2,3,4}Gen` per-engine code generators, the BIR linker, and `NeffPackager` | [Part 8](../walrus/), [Part 2](../isa/), [Part 12](../formats/) |
| `libBIRSimulator.so` | 36 MB | The BIR functional simulator (`birsim`), the whole-machine state model, and the DAP debugger | [7.34–7.41](../bir/) |
| `liblogging.so` | 35 MB | `NeuronLogger` and the Boost.Log sink stack | [3.18](../frontend/neuronlogger.md) |
| `libLncBarriercheck.so` | 33 MB | The inter-core (LNC) barrier verifier | [8.43](../walrus/barriercheck.md) |
| `libBIR.so` | 9.5 MB | **The BIR core**: `bir::Instruction` and the 110-opcode hierarchy, the Dtype tables, `CastToNewDType`, and the BIR-JSON SerDe | [Part 7](../bir/) |
| `libBIRParserDumper.so` | — | `parserdumper` / `bir_roundtrip` (BIR-JSON read-write round trip) | [8.44](../walrus/parserdumper.md) |
| `libBIRRacecheck.so` | — | The intra-core race checker (vector-clock + overlap geometry) | [8.42](../walrus/racecheck.md) |
| `libpwp_sim.so` | — | The piecewise-polynomial activation simulator (`evaluate_generic`) | [7.40](../bir/sim-activation-pwp.md), [Part 10](../activation/) |

## The Cython module galaxy

The Penguin middle-end and the NKI frontend ship as per-Python-version Cython extension modules (`*.cpython-3xx-x86_64-linux-gnu.so`). There are several hundred; the heaviest carry the most logic. A representative slice:

| Module (path under `neuronxcc/`) | Size | Owns |
|---|---|---|
| `starfish/penguin/simulation/Jit.so` | 20 MB | The Penguin simulation JIT |
| `nki/compiler/backends/neuron/KernelBuilder.so` | 15 MB | **NKI forward codegen** — `NeuronCodegen`; ships **with** Cython debug info |
| `starfish/penguin/targets/codegen/BirCodeGenLoop.so` | 11 MB | The beta3 Penguin → BIR driver |
| `starfish/penguin/targets/tonga/TongaISAInst.so` | 11 MB | The Tonga ISA instruction model (Python side) |
| `starfish/penguin/targets/transforms/TritiumFusionBase.so` | 10 MB | The Tritium fusion planner base |
| `starfish/penguin/transforms/LowerTensorOp.so` | 10 MB | High-level TensorOp → tiled-loop lowering |
| `starfish/penguin/ir/IRBuilder.so` | 8.5 MB | The Penguin IR builder |
| `starfish/penguin/ir/Intrinsics.so` | 8.3 MB | The Penguin intrinsic set |
| `nki/compiler/backends/neuron/sema.so` | 6.0 MB | NKI semantic legality engine |
| `starfish/penguin/ir/Access.so` | 6.0 MB | Access-pattern / tile-access model |
| `starfish/penguin/targets/codegen/NkiCodegen.so` | 4.9 MB | The Penguin IR → NKI-text **re-emit** printer |
| `nki/isa/neuron_isa.so` | 3.3 MB | The `nki.isa.*` intrinsic surface |
| `pelican.so` | 2.6 MB | `pelican::Expr` — the symbolic index/address algebra |
| `starfish/birpy/InstructionOpcodes.so` | 2.9 MB | The BIR opcode bindings (Python side) |
| `driver/commands/CompileCommand.so` | 2.5 MB | The top-level compile command |
| `driver/jobs/WalrusDriver.so`, `XLAInferGoldens.so` | 3.1 MB ea. | The Python job wrappers that fork the tools |

> **NOTE —** the two-codegen split is visible right here in the file list: `KernelBuilder.so` (`NeuronCodegen`) is the **forward** builder that turns traced NKI Python into Penguin IR, while `NkiCodegen.so` (`NkiCodegen`) is the **reverse** printer that re-emits NKI text from compiled Penguin IR. They are different classes in different files; do not conflate them. See [6.5.1](../nki/neuroncodegen-overview.md) and [6.5.9](../nki/nkicodegen.md).

## Reading the tree

- `starfish/` — the C++ compiler core. `bin/` (tools), `lib/` (the eight `.so`), and the Cython `penguin/` middle-end + `birpy/` bindings.
- `nki/` — the kernel DSL: `isa/` (intrinsics), `language/` (the `nl.*` ops), `compiler/backends/neuron/` (tracing, sema, codegen), and `_private_kernels/` / `_private/` (the production kernel library).
- `driver/` — the Python command layer: `commands/`, `jobs/`, `Arguments`, `CommandDriver`, `JobRegistry`.
- `hwm/`, `kra/`, `logging/` — the hardware model, the runtime-adjacent `kra`/`krt` libraries, and the `ErrorMessages` / `Assert` diagnostic modules.

## Cross-References

- [The Compile Pipeline at a Glance](../front/pipeline.md) — which of these binaries runs at each stage.
- [Build & Version Provenance](versions.md) — the cross-wheel hardlink finding and the cp310/311/312 parity argument.
- [Symbol & Offset Index](../appendix/symbol-offset-index.md) — per-binary symbol/address lookups.
