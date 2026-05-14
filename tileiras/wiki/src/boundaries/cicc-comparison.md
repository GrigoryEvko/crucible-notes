# Boundaries: tileiras vs cicc

## Abstract

The `tileiras` and `cicc` binaries shipped inside CUDA Toolkit 13.x are siblings. They live in the same `bin/` directory, are both invoked by `nvcc`, and both emit PTX that is handed to the same `ptxas`. What differs is the front edge of the pipeline: `cicc` accepts CUDA C++ source and rides an EDG-driven NVVM bridge into the NVPTX backend; `tileiras` accepts MLIR bytecode and rides a 53-pass MLIR pipeline driver into the same NVPTX backend. This page assumes the reader already knows `cicc` and documents what is shared, what is reinvented, and what `cicc` carries that `tileiras` jettisoned.

## Premise

Tileiras and cicc are sibling tools in CUDA 13.1's device-compilation toolchain. They link the same NVIDIA-internal LLVM 21.0.0git fork, expose the same MC subsystem identity, and carry the same NVVM/NVPTX pass family names. Cicc 13.0 carries the same family one minor revision earlier; cicc 13.1 tracks tileiras's LLVM snapshot.

`cicc` is a CUDA-C++-to-PTX compiler. Its three major subsystems are an EDG 6.6 frontend, an NVVM bridge, and an LLVM NVPTX backend. Together they implement a full source-to-PTX flow with standalone and libNVVM-shaped dispatch. The compiler parses C++, lowers through EDG IL, emits the `.int.c`/`.device.c`/`.stub.c` split artifacts, optimizes through NVIDIA's NVVM pass family, and runs the NVPTX backend.

`tileiras` is an optimizing assembler in the literal MLIR sense: it consumes a serialized representation of an already-lowered tile program, finishes lowering to a hardware-near IR, and emits a deployable artifact. Input is MLIR bytecode — the on-disk encoding of a `builtin.module` containing a `cuda_tile` payload — not source. There is **no C++ parser**, **no EDG frontend**, **no `.int.c` emission**, **no constexpr evaluator**. Tileiras is also explicitly **not** a `cudafe++` replacement: cudafe++ does C++ source-to-source rewrite (kernel-launch lowering, host/device split), while tileiras only consumes bytecode and emits a host ELF (`elf.o` by default).

## Pass-by-pass overlap matrix

The clean way to read the shared surface is to split it into two layers.

**Layer A — MLIR / IR-frontend.** No equivalent in cicc. Cicc has no MLIR; its frontend is EDG 6.6 emitting C, then a hand-written EDG-IL-to-LLVM-IR translator inside the NVVM bridge.

**Layer B — NVVM-IR / NVPTX-backend.** Shared. Pass names, command-line keys, diagnostic strings, and pass-info constructor shapes match byte-for-byte across the two binaries.

| Layer | Subsystem | tileiras | cicc | Status |
|---|---|---|---|---|
| A | C++ parser | absent | EDG 6.6 frontend | cicc-only |
| A | constexpr evaluator | absent | EDG tree-walker | cicc-only |
| A | `.int.c`/`.device.c`/`.stub.c` triple | absent | EDG backend output | cicc-only |
| A | EDG IL → LLVM IR | absent | source-language IR generation | cicc-only |
| A | MLIR bytecode reader | present | absent | tileiras-only |
| A | 9-dialect cascade + dialect registration | present | absent | tileiras-only |
| A | TileAS pass family | present | absent | tileiras-only |
| A | MLIR `PassManager` constructor | 53-pass pipeline | absent | tileiras-only |
| A | MODSBuilder | cost-based modulo scheduler | absent | tileiras-only |
| A | TileIR pipeline driver | register, configure, run MLIR lowering | absent | tileiras-only |
| A | Pipeline option registrar | compact typed option table | broad `cl::opt` surface | different shape |
| A | OptiX IR generation | absent | `--emit-optix-ir` path | cicc-only |
| A | Wizard mode / fast-compile tier | absent | present | cicc-only |
| B | NVVMReflect family | present | present | shared |
| B | NVVM Peephole Optimizer | present | present | shared |
| B | BaseAddressStrengthReduce | present | present | shared |
| B | MemorySpaceOpt | present | present | shared |
| B | DeadSyncElim | present | present | shared |
| B | CommonBaseElim | present | present | shared |
| B | NVVMIRVerifier | present | present | shared |
| B | IPMSPPass | present | present | shared |
| B | NVPTXSetFunctionLinkagesPass | present | present | shared |
| B | SelectKernelsPass | present | present | shared |
| B | KernelInfoPrinter | present | present | shared |
| B | NVVMAA | present | present | shared |
| B | nvvm-reflect-pp | present | present | shared |
| B | NVPTX SelectionDAG | present | present | shared |
| B | NVPTX instruction printer | present | present | shared |
| B | `PassBuilder::registerAllPasses` | present | present | shared |
| B | libdevice bitcode | embedded once | embedded twice in the two cicc paths | shared content |
| B | ptxas subprocess | launched by tileiras | launched by the nvcc/cicc path | both shell out |

The pattern is simple: above NVVM-IR everything is rewritten; below NVVM-IR almost everything is shared.

## Shared NVPTX backend evidence

When the `cuda_tile` MLIR module finishes its descent through the 9-dialect cascade and reaches the `llvm`/`nvvm` dialect, tileiras hands the resulting LLVM module to a NVPTX backend from the same NVIDIA-internal fork that cicc links. The pass roster, command-line keys, diagnostics, and analysis names line up across the two tools.

| Pass | Public key or surface | Role |
|---|---|---|
| NVVM Peephole Optimizer | `nvvm-peephole-optimizer` | Performs NVVM-specific instruction and intrinsic cleanups before codegen. |
| BaseAddressStrengthReduce | internal debug type | Rewrites address arithmetic into forms that are cheaper for NVPTX selection. |
| MemorySpaceOpt | `-mllvm` knob family | Normalizes memory-space casts and address-space information. |
| DeadSyncElim | `-nvvm-dead-sync-elim` | Removes synchronization operations proven unnecessary. |
| CommonBaseElim | SCEV-driven transform | Deduplicates related GEP/base-address computations. |
| NVVMIRVerifier | verifier diagnostics | Rejects invalid NVVM IR shapes before NVPTX lowering. |
| IPMSPPass | `ipmsp` | Interprocedural module-specialization support. |
| NVPTXSetFunctionLinkagesPass | `check-kernel-functions` | Sets and validates kernel linkage state. |
| SelectKernelsPass | `select-kernels` | Restricts compilation to selected kernel sets or ranges. |
| KernelInfoPrinter | `kernel-info` | Emits kernel metadata for downstream consumers. |
| NVVMAA | `nvvm-aa` | NVIDIA alias analysis for NVVM/NVPTX transforms. |
| NVVMReflect | `nvvm-reflect`, `nvvm-reflect-pp` | Resolves `__nvvm_reflect` queries from reflection metadata. |

Two CLI knob families confirm the shared backend contract at the user-visible layer. The `nvvm-reflect-` option family installs the same enable and key/value override behavior in both tools, and the kernel-selection family accepts the same kernel-list, kernel-range, IPMSP dump, and clone-control options.

Crucial scoping note: these passes are **not** invoked by tileiras's own MLIR `PassManager`. They run one level down, after tileiras's LLVM-dialect output is materialized as an `llvm::Module` and handed to the embedded NVPTX backend. The MLIR layer produces valid-shape NVVM-dialect IR; the LLVM layer applies the shared NVPTX pass family unchanged.

## Tileiras-only inventions

Above the NVVM-IR boundary, tileiras introduces an MLIR-shaped front-end with no analogue in cicc. None of the following symbols, dialects, or pass mnemonics appear in the cicc binary.

| Subsystem | Description |
|---|---|
| MLIR bytecode reader | Project-private MLIR bytecode I/O with Tile versioning, frozen op/type/attribute tags, and `cuda_tile` schema support. |
| TileIR top-level driver | Compile-and-serialize path that registers dialects, registers pipeline options, and runs lowering. |
| 9-dialect cascade | `cuda_tile` → `nv_tileaa` → `nv_tileas` (+ `cute`, `cute_nvgpu`, `cutlass`) → `nvgpu` → `nvvm` → `llvm`. |
| MLIR-pipeline driver | Builds the `mlir::PassManager` for O0/O1/O2; the tier is decoded from bytecode attributes such as `"nvopt<O2>"`. |
| TileAS family | Removes dead args, resolves agent boundaries, schedules async work, materializes layouts, plans CTA mapping, and inserts OCG knobs. |
| MODSBuilder | Cost-based modulo scheduler used in O2 after schedule generation and after GPU-op conversion. |
| `cute` dialect | CuTe layout algebra: local tiling, partitioning, shape arithmetic, size/cosize, and divide helpers. |
| `cute_nvgpu` dialect | SM70-SM120 atoms for TMA, tensor memory, GMMA/UMMA descriptors, warp-uniform values, and WGMMA. |
| `cutlass` dialect | Pipeline acquire/commit/wait, tile-scheduler records, block-striped operations, and sequence barriers. |
| `cuda_tile` dialect | Public control, entry, tensor-view, atomic, selection, constant, and optimization-hint surface. |
| `nv_tileaa` / `nv_tileas` | Alias-aware typed-pointer/token/view layer plus assembler-near schedules, layouts, execution units, tiled loads/stores, and dot operations. |
| Pipeline option registrar | Compact typed table for integer, unsigned, boolean, enum, and string options. |
| `nvdisasm -c` shell-out | Optional SASS disassembly pass that appends a disassembly section to the emitted host object. |

Three pieces deserve a closer look. First, dialect registration has no analogue in cicc, which builds its IR directly in LLVM-IR shape. Second, the MLIR `PassManager` uses nested operation pass managers, function adapters, and the canonicalizer/CSE/SymbolDCE cleanup trio; cicc's pass manager is a conventional LLVM function/module pipeline. Third, the optimization tier comes from an attribute embedded in the TileIR bytecode, while cicc uses the conventional `-O0`/`-O1`/`-O2`/`-O3` driver flag family.

## cicc-only baggage tileiras dropped

Cicc's bulk comes from features tileiras explicitly does not need. The following are visible in the cicc binary and entirely absent from tileiras.

| Dropped subsystem | cicc responsibility | Why tileiras drops it |
|---|---|---|
| EDG 6.6 frontend | C++ parsing, type checking, templates, constexpr, and CUDA source diagnostics. | input is MLIR bytecode, not C++ |
| `.int.c` / `.device.c` / `.stub.c` emission | EDG backend source splitting and host/device artifact generation. | emits host ELF directly |
| OptiX IR generation | Optional OptiX IR output stage. | no OptiX path |
| Wizard mode | cicc-internal experimental mode. | absent |
| Fast-compile tiers | Multiple compile-tier knobs. | only the TileIR optimization tier applies |
| NVVMPassOptions struct | Large shared knob block for the cicc NVVM pipeline. | consolidated into a compact typed option table |
| Dual Path A / Path B dispatch | Two frontend/IR-generation paths for standalone and libNVVM-shaped usage. | one bytecode-to-object path |
| Broad `cl::opt` registry | Large standalone compiler option surface. | small driver surface plus TileIR pipeline options |
| NVVM builtin resolution table | Source-level builtin name and overload resolution. | resolution happens upstream |
| constexpr evaluator | EDG tree-walking interpreter. | C++ template/constexpr evaluation happens upstream |
| C++ template cleanup | Synthesized source-language runtime cleanup. | no synthesized C++ runtime |
| `-nvvm-version=nvvm-latest`/`nvvm70` switch | Path selector for older cicc modes. | absent |
| LibNVVM API entry points | Library-facing API surface. | not a libNVVM client |

Tileiras is 88 MB despite carrying a full MLIR runtime, a 9-dialect cascade, the CuTe/CUTLASS pipeline op surface, a cost-based modulo scheduler, and the TileAS pass family, because it leaves the 3.2 MB EDG, the dual-path duplication, the 1,689-option registry, the 4 KB NVVMPassOptions struct, and the OptiX path behind. Cicc 13.0's 60 MB skew toward EDG and dual-path overhead; tileiras's 88 MB skew toward the MLIR/dialect surface and the TileAS family.

## Architectural sketch (side-by-side)

```
                cicc                                          tileiras
                ────                                          ────────
  CUDA C++ source (.cu / .ci / .i)                  MLIR bytecode (.ctir / .ctb)
              │                                                    │
              ▼                                                    ▼
   ┌─────────────────────┐                            ┌──────────────────────┐
   │  EDG 6.6 frontend   │                            │  MLIR bytecode       │
   │  parser, constexpr  │                            │  reader              │
   │   parser, constexpr │                            └──────────┬───────────┘
   │  evaluator          │                                       │
   └──────────┬──────────┘                                       ▼
              │ .int.c / .device.c / .stub.c       ┌────────────────────────┐
              ▼                                    │   cuda_tile dialect    │
   ┌─────────────────────┐                         └──────────┬─────────────┘
   │  IRGEN: EDG IL →    │                                    ▼
   │  LLVM IR translator │                         ┌────────────────────────┐
   │  standalone/libNVVM │                         │   nv_tileaa dialect    │
   │  shaped paths       │                         └──────────┬─────────────┘
   └──────────┬──────────┘                                    ▼
              │                                    ┌────────────────────────┐
              ▼                                    │   nv_tileas dialect    │
   ┌─────────────────────┐                         │   + cute               │
   │  LNK + libdevice    │                         │   + cute_nvgpu         │
   │  (456 KB embedded)  │                         │   + cutlass            │
   └──────────┬──────────┘                         │  TileAS 16 passes      │
              │                                    │  MODSBuilder           │
              ▼                                    │  53-pass MLIR pipeline │
   ┌─────────────────────┐                         └──────────┬─────────────┘
   │  OPT: NVVM passes   │                                    ▼
   │  35 NVIDIA-custom + │                         ┌────────────────────────┐
   │  standard LLVM      │                         │   mlir::nvgpu          │
   │  NVVM pipeline      │                         └──────────┬─────────────┘
   └──────────┬──────────┘                                    ▼
              │                                    ┌────────────────────────┐
              │ (no MLIR layer)                    │   nvvm dialect         │
              │                                    └──────────┬─────────────┘
              │                                               ▼
              │                                    ┌────────────────────────┐
              │                                    │   llvm dialect         │
              │                                    └──────────┬─────────────┘
              │                                               │
              └───────────────────┬───────────────────────────┘
                                  │
                                  ▼  (CONVERGENCE — same NVPTX backend)
              ┌────────────────────────────────────────────────────────┐
              │  NVPTX backend (LLVM 21.0.0git internal fork)         │
              │  ─ nvvm-peephole-optimizer / BaseAddressStrengthReduce│
              │  ─ MemorySpaceOpt / DeadSyncElim / CommonBaseElim     │
              │  ─ NVVMIRVerifier / IPMSP / NVVMAA                    │
              │  ─ NVPTXSetFunctionLinkagesPass / SelectKernelsPass   │
              │  ─ KernelInfoPrinter / NVVMReflect / nvvm-reflect-pp  │
              │  ─ NVPTX SelectionDAG ISel / NVPTXInstPrinter         │
              └────────────────────────────┬───────────────────────────┘
                                           │
                                           ▼
                                       PTX text
                                           │
                                           ▼
                              ┌──────────────────────────┐
                              │  ptxas (subprocess)      │
                              │  PTX → SASS              │
                              └────────────┬─────────────┘
                                           │
                                           ▼
                                    cicc: .ptx       tileiras: elf.o
                                                     (with optional
                                                      nvdisasm -c
                                                      SASS section)
```

The two pipelines converge at the moment the LLVM module is materialized for the NVPTX backend, and from that point forward they share the same code — passes, ISel, register allocation, scheduling, asm-printer.

## Cross-link recommendations

Everything tileiras inherits unchanged from the LLVM 21 fork is documented in the cicc wiki, and those pages are reusable verbatim for the tileiras NVPTX backend.

- **NVPTX backend internals** — see cicc `pipeline/codegen.md` and `pipeline/emission.md`. Same SelectionDAG, same `NVPTXTargetLowering`, same 19 MMA shapes x 11 data types, and same instruction-printer surface.
- **NVVMReflect mechanism** — see cicc reflect docs. Same `__nvvm_reflect`/`__nvvm_reflect_ocl` rewrite, same `nvvm.reflection` module-flag table, same `nvvm-reflect-add` parser.
- **libdevice** — same ~456 KB bitcode payload. Tileiras embeds it once (no Path A / Path B duplication).
- **NVVM Peephole / BaseAddressStrengthReduce** — same pre-codegen cleanup and address-strength-reduction roles.
- **MemorySpaceOpt** — same address-space normalization and memory-space cleanup behavior.
- **DeadSyncElim** — same synchronization-elimination pass.
- **NVVMIRVerifier** — same verifier role before backend lowering.
- **IPMSP / SelectKernels / KernelInfo / NVPTXSetFunctionLinkages / NVVMAA / nvvm-reflect-pp** — same backend registration family.

For everything *above* the NVVM-IR boundary, the cicc wiki has nothing to offer; refer to the tileiras-internal pages: [cuda_tile Overview](../dialects/cuda_tile/overview.md), [cute Overview](../dialects/cute/overview.md), [cute_nvgpu Overview](../dialects/cute_nvgpu/overview.md), [cutlass Overview](../dialects/cutlass/overview.md), [nv_tileaa Overview](../dialects/nv_tileaa/overview.md), [nv_tileas Overview](../dialects/nv_tileas/overview.md), the [TileAS Pass Families](../passes/tileas/scheduling-glue.md) series, [Full Pass List by Opt Level](../pipeline/full-pass-list-by-opt-level.md), [Modulo Scheduler and Rau](../scheduler/modulo-scheduler-and-rau.md), [CLI Options](../driver/cli-options.md), and [MLIR Bytecode Format](../bytecode/mlir-bc-format.md).

## Reimplementation Notes

Model the two tools as two different producers for the same downstream backend shape:

```text
cicc:
    input: CUDA C++ source or preprocessed CUDA source
    frontend: EDG and NVVM bridge
    handoff: LLVM/NVVM module
    backend: shared NVPTX backend
    output: PTX for ptxas

tileiras:
    input: TileIR MLIR bytecode
    frontend: MLIR dialect cascade and TileAS passes
    handoff: LLVM/NVVM module
    backend: shared NVPTX backend
    output: host object that carries ptxas output
```

This split is the key design constraint. Above the LLVM/NVVM handoff, reuse between the two tools is mostly conceptual. Below that handoff, the pass names, reflection behavior, libdevice payload, and PTX emission semantics should be treated as one shared backend contract.
