# Position in nvcc 13.1 Toolchain

## Abstract

CUDA 13.1 is the first toolkit release in which `nvcc` ships with two parallel device-code compilers in `bin/`. The legacy compiler `cicc` handles CUDA C++ source via the EDG 6.6 frontend and the NVVM bridge. A second compiler, `tileiras` (88 MB, build tag `release 13.1, V13.1.80, Build local.local.36836380_`), handles a new MLIR-bytecode input format that did not exist in any prior CUDA release. Both compilers link the same NVIDIA-internal LLVM 21.0.0git fork, share the same NVPTX backend, and emit PTX consumed by the same `ptxas`. What distinguishes them is the front edge of the pipeline: source language, IR shape, and dialect surface. This page locates `tileiras` inside the nvcc 13.1 toolchain, contrasts the two device-code paths end to end, and identifies which upstream MLIR DSLs can plausibly emit the bytecode `tileiras` consumes.

## Path A: cicc legacy (CUDA C++ source)

The classical CUDA device-compilation pipeline is unchanged from prior toolkits:

```
.cu source
  |
  v
cudafe++ (EDG frontend, host/device split, kernel-launch lowering)
  |
  v
.int.c / .device.c / .stub.c  (transformed C with CUDA extensions stripped)
  |
  v
cicc  (C/EDG-IL -> NVVM IR -> NVPTX backend -> PTX text)
  |
  v
PTX text
  |
  v
ptxas  (PTX -> SASS)
  |
  v
cubin  (or fatbin section, embedded by fatbinary/nvlink/nvcc)
```

Inside `cicc`, EDG parses CUDA C++, evaluates constexpr expressions, and produces the split artifacts that the rest of the classic CUDA pipeline expects. The NVVM bridge translates the device side into LLVM IR, runs the NVIDIA NVVM pass family, and hands the module to the NVPTX backend. The observable compiler product at this stage is PTX text.

## Path B: tileiras new (MLIR bytecode)

The MLIR-rooted pipeline is structurally distinct above the LLVM IR layer:

```
MLIR DSL frontend (CUTLASS-on-MLIR, custom DSL, etc.)
  |
  v
.mlir-bc  (MLIR bytecode containing a builtin.module with a cuda_tile payload)
  |
  v
tileiras  (MLIR -> 9-dialect cascade -> NVVM dialect -> llvm dialect -> NVPTX backend -> PTX text -> elf.o)
  |
  v
PTX text  (materialized internally; ptxas is invoked as a subprocess)
  |
  v
ptxas  (PTX -> SASS, embedded in elf.o)
  |
  v
elf.o  (host ELF relocatable carrying the SASS payload)
```

Inside `tileiras`, the MLIR bytecode reader parses the input into a `builtin.module`. The driver registers the `cuda_tile` target, loads the `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, `cutlass`, `nvgpu`, `nvvm`, and `llvm` dialect families, and builds a 53-pass MLIR pipeline that lowers the module to the LLVM dialect. Below the NVVM-IR boundary the same NVPTX backend used by `cicc` produces PTX. The driver then invokes `ptxas`, embeds the resulting SASS into a host ELF object, and writes the result to `--output-file` (default `elf.o`).

## Driver invocation: how nvcc chooses which compiler

Selection visible in the `tileiras` driver is input-format-driven. The command line accepts one positional argument named `"<tile bytecode file>"`, and the public creation path expects one byte buffer containing valid Tile IR bytecode. A null buffer returns error code 2 with the diagnostic `"null inputBuffer provided, expected valid bytecode buffer"`. A malformed buffer returns error code 3 with `"failed to parse IR bytecode"` or `"input does not correspond to Tile IR bytecode"`. If the byte stream appears to be ordinary upstream MLIR bytecode rather than Tile IR bytecode, the diagnostic appends `" (it looks like MLIR bytecode instead)"`.

There is no C++ parsing path in `tileiras`: no EDG frontend, no `.int.c` emission, no CUDA C frontend, and no source-level kernel-launch lowering. The driver contract starts after source-language analysis has already happened.

The `nvcc` driver therefore routes work between the two compilers based on the input artifact rather than a runtime flag inside either tool. `.cu` translation units flow through `cudafe++` and into `cicc`; serialized Tile IR bytecode flows directly into `tileiras`. No flag inside `tileiras` toggles between the two paths. A reimplementation of the nvcc driver layer should classify the input artifact before dispatch and should reject ambiguous bytecode early with the same diagnostics users see from `tileiras`.

## Shared downstream: ptxas

Both pipelines converge at `ptxas`. The PTX text from `cicc` and from `tileiras` is produced by the same NVPTX backend, the same SelectionDAG instruction selector, and the same NVIDIA NVVM pass roster: `NVVMReflect`, `NVVMPeepholeOptimizer`, `BaseAddressStrengthReduce`, `MemorySpaceOpt`, `DeadSyncElim`, `CommonBaseElim`, `NVVMIRVerifier`, `IPMSPPass`, `NVPTXSetFunctionLinkagesPass`, `SelectKernelsPass`, `KernelInfoPrinter`, and `NVVMAA`. From `ptxas`'s perspective, the upstream identity of the PTX is invisible. PTX-to-SASS-to-cubin assembly is the same regardless of which compiler emitted the PTX.

## Host code path is unrelated

Neither `cicc` nor `tileiras` handles host code. The host translation unit is preprocessed by `nvcc`, split by `cudafe++`, and handed to the system C++ compiler. `tileiras` accepts `--host-arch` (`x86_64`, `aarch64`, `arm64ec`) and `--host-os` (`linux`, `windows`) only because its output is a host ELF relocatable object: those flags select the host triple of the wrapper ELF, not a host compiler. Host-side C++ compilation is orchestrated by nvcc and is independent of which device-code compiler is in use; both paths emit artifacts the host linker later combines with the host object file.

## MLIR DSL frontends that emit tileiras-bound `.mlir-bc`

Tileiras's input is a serialized MLIR module whose top-level dialect is `cuda_tile`. Its dialect cascade covers `cuda_tile`, `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, and `cutlass`. This dialect surface tells the story of which upstream producers are intended to feed `tileiras`:

- **CUTLASS-on-MLIR** is the most direct match. The `cutlass` dialect carries `pipeline.{acquire, tail, commit, wait}`, `tile_scheduler.work_tile_info`, `block_striped.{reduce, load, store}`, and `seq_bar` - the exact pipeline-orchestration vocabulary CUTLASS uses for collective mainloops, persistent kernels, and stream-K schedulers.
- **CuTe-DSL frontends.** The `cute` dialect (~50 ops: `cute.local_tile`, `cute.local_partition`, `cute.tile_to_shape`, `cute.add_offset`, `cute.size`, `cute.cosize`, divide family) implements the CuTe layout algebra at MLIR-IR level. Any DSL that produces tile-by-tile descriptions of GPU work in CuTe terms can target this dialect.
- **Triton-for-CUDA-on-MLIR.** A Triton backend that targets the `cuda_tile` dialect (instead of, or in addition to, the existing `triton-gpu` lowering) would produce input `tileiras` accepts. The `cuda_tile.{if, select, xori, constant, atomic_cas_tko, entry, for, make_tensor_view, optimization_hints}` surface is general enough to host SPMD-tile programs.
- **Custom DSLs and JIT pipelines.** The bytecode contract is open: any caller that constructs a `builtin.module` with a `cuda_tile` payload, a valid `"nvopt<O0>"`/`"<O1>"`/`"<O2>"` tier attribute, and dialect references confined to the registered cascade can serialize and feed `tileiras`. Schema versions 13.1/13.2 are recognized.

These producers are upstream of `tileiras` and outside the nvcc toolkit's `bin/` directory. The integration point is the bytecode file: the producer writes it; nvcc dispatches to `tileiras`; the rest of the build proceeds identically to a `cicc`-emitted artifact.

## Side-by-side architectural diagram

```
                Path A: cicc legacy                                  Path B: tileiras new
                -------------------                                  --------------------
  .cu source                                                MLIR DSL frontend (CUTLASS-on-MLIR /
       |                                                              CuTe DSL / Triton / custom)
       v                                                              |
  cudafe++  (EDG frontend, host/device split,                         v
             kernel-launch lowering)                            .mlir-bc  (cuda_tile bytecode)
       |                                                              |
       v                                                              v
  .int.c / .device.c / .stub.c                                  tileiras
       |                                                              |
       v                                              MLIR bytecode reader
  cicc                                                                |
   - EDG IL -> LLVM IR translator                                     v
   - NVVM bridge (~4 MB)                                       cuda_tile dialect
   - 35 NVIDIA-custom NVVM passes                                     |
                                                                      v
                                                              nv_tileaa / nv_tileas / cute /
                                                              cute_nvgpu / cutlass dialects
                                                              + 16-pass TileAS family
                                                              + MODSBuilder modulo scheduler
                                                              + 53-pass mlir::PassManager
                                                              (53-pass pipeline)
                                                                      |
                                                                      v
                                                                  nvgpu dialect
                                                                      |
                                                                      v
                                                                  nvvm dialect
                                                                      |
                                                                      v
                                                                  llvm dialect
       |                                                              |
       +---------------------+----------------------------------------+
                             |  CONVERGENCE: same NVPTX backend (LLVM 21.0.0git fork)
                             v
                ----------------------------------
                NVPTX backend
                  - NVVMReflect / nvvm-reflect-pp
                  - NVVMPeepholeOptimizer
                  - BaseAddressStrengthReduce
                  - MemorySpaceOpt / DeadSyncElim / CommonBaseElim
                  - NVVMIRVerifier / IPMSPPass / NVVMAA
                  - NVPTXSetFunctionLinkagesPass / SelectKernelsPass
                  - KernelInfoPrinter
                  - NVPTX SelectionDAG ISel
                  - NVPTX instruction printer
                ----------------------------------
                             |
                             v
                          PTX text
                             |
                             v
                          ptxas (PTX -> SASS)
                             |
                             v
            ----------------------------------------
            cicc path:           tileiras path:
            cubin / .ptx         elf.o (host ELF
                                 wrapping SASS, with
                                 optional nvdisasm -c
                                 disassembly section)
```

The diagram mirrors the architectural reality: the two pipelines diverge above the LLVM IR layer and converge at the NVPTX backend.

## Reimplementation Notes

For a driver reimplementation, treat `tileiras` as a separate device-code compiler selected by artifact type:

```text
if input.kind == "cuda-cpp-source":
    run cudafe++ to split host and device work
    run cicc on the device-side artifact
elif input.kind == "tileir-bytecode":
    run tileiras on the bytecode buffer
else:
    reject the input before invoking either compiler
```

The important invariant is that the choice happens before either compiler starts. Once PTX has been produced, the downstream assembly path no longer needs to know whether the source was CUDA C++ or Tile IR bytecode.
