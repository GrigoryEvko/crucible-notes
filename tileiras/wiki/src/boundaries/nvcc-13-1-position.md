# Position in nvcc 13.1 Toolchain

## Abstract

CUDA 13.1 is the first toolkit release in which `nvcc` ships with two parallel device-code compilers in `bin/`. The legacy compiler `cicc` handles CUDA C++ source via the EDG 6.6 frontend and the NVVM bridge. A second compiler, `tileiras` (88 MB, build tag `release 13.1, V13.1.80, Build local.local.36836380_`), handles a new MLIR-bytecode input format that did not exist in any prior CUDA release. Both compilers link the same NVIDIA-internal LLVM 21.0.0git fork, share the same NVPTX backend, and emit PTX consumed by the same `ptxas`. What distinguishes them is the front edge of the pipeline: source language, IR shape, and dialect surface. This page locates `tileiras` inside the nvcc 13.1 toolchain, contrasts the two device-code paths end to end, and identifies which upstream MLIR DSLs can plausibly emit the bytecode `tileiras` consumes.

## Path A: cicc legacy (CUDA C++ source)

The classical CUDA device-compilation pipeline is unchanged from prior toolkits:

```text
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

```text
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

Selection visible in the `tileiras` driver is input-format-driven. The command line accepts one positional argument named `"<tile bytecode file>"`, and the public creation path expects one byte buffer containing valid TileIR bytecode. A null buffer returns error code 2 with the diagnostic `"null inputBuffer provided, expected valid bytecode buffer"`. A malformed buffer returns error code 3 with `"failed to parse IR bytecode"` or `"input does not correspond to Tile IR bytecode"`. If the byte stream appears to be ordinary upstream MLIR bytecode rather than TileIR bytecode, the diagnostic appends `" (it looks like MLIR bytecode instead)"`.

There is no C++ parsing path in `tileiras`: no EDG frontend, no `.int.c` emission, no CUDA C frontend, and no source-level kernel-launch lowering. The driver contract starts after source-language analysis has already happened.

The `nvcc` driver therefore routes work between the two compilers based on the input artifact rather than a runtime flag inside either tool. `.cu` translation units flow through `cudafe++` and into `cicc`; serialized TileIR bytecode flows directly into `tileiras`. No flag inside `tileiras` toggles between the two paths. A reimplementation of the nvcc driver layer should classify the input artifact before dispatch and should reject ambiguous bytecode early with the same diagnostics users see from `tileiras`.

## Invocation triggers

`nvcc` does not branch on a user-facing `--use-tile-ir` switch. The driver's choice is observable on the receiving end: tileiras requires one positional argument that begins with the TileIR bytecode magic, so the only way `nvcc` legitimately reaches the tileiras binary is to have a bytecode buffer in hand. Three concrete triggers explain where that buffer comes from:

1. **An MLIR-emitting frontend has run before nvcc receives the file.** A CUTLASS-on-MLIR pipeline, a CuTe-DSL JIT, or a Triton-for-CUDA backend writes a `.tileir`/`.ctir`/`.ctb` file with the `7f 54 69 6c 65 49 52 00` magic header and a `cuda_tile` payload. `nvcc` recognises the extension or the magic and routes the file to tileiras without invoking `cudafe++` or `cicc`.
2. **An ahead-of-time tooling step has produced the bytecode.** A library-level build (CUTLASS profiler, custom tile-program library) emits the bytecode artifact at install time; `nvcc` consumes it during the final assembly phase the same way it would consume a precompiled `.ptx` or `.cubin`.
3. **An integrator drives tileiras directly.** The tool accepts the bytecode path as its sole positional argument and writes the host ELF relocatable to `--output-file`. No nvcc wrapper is involved; the integrator owns process spawning, environment setup, and result handling. The `tileirasProgram*` C API is the in-process analogue of this path.

Nothing inside the `tileiras` binary changes between the three cases. The bytecode magic check, the GPU whitelist, the optimization-level validator, and the dialect cascade are identical regardless of caller.

## Argv shape that reaches tileiras

The driver-facing argv schema is fixed and small. A representative invocation that an nvcc dispatcher (or a reimplementation) constructs for a Blackwell datacenter target at `-O2` with line info looks like:

```bash
tileiras \
    --gpu-name=sm_100 \
    --opt-level=2 \
    --lineinfo \
    --host-arch=x86_64 \
    --host-os=linux \
    --output-file=/tmp/build/kernel.tileir.o \
    /tmp/build/kernel.tileir
```

Every token maps to one of the validated driver options catalogued in [Driver CLI Options](../driver/cli-options.md#driver-options). The positional argument is the single input file; there is no support for multiple bytecode buffers in one invocation, no `@response-file` expansion, and no environment-fed argv extension. A reimplementation of the nvcc dispatcher should construct argv per call rather than building a long-lived tileiras subprocess.

| Argv token | Origin in nvcc | Mandatory? |
|---|---|---|
| `--gpu-name=sm_<NN>` | nvcc's `-arch=sm_<NN>` parameter, validated against the SM whitelist before dispatch | yes |
| `--opt-level=<0..3>` | nvcc's `-O<0..3>` parameter, defaulting to `-O3` if unset | yes (defaulted) |
| `--lineinfo` | nvcc's `-lineinfo` switch | conditional |
| `--device-debug` | nvcc's `-G` switch; rejected unless `-O0` | conditional |
| `--sanitize=memcheck` | nvcc's `-Xcompiler-sanitize=memcheck` analogue | conditional |
| `--host-arch=<…>` | nvcc's host-architecture detection or `-target` shadow | yes (defaulted) |
| `--host-os=<…>` | nvcc's host-OS detection | yes (defaulted) |
| `--output-file=<path>` | nvcc-derived temporary path that nvcc later links into the fatbin | yes |
| `<bytecode path>` | The TileIR bytecode that triggered the dispatch | yes |

The driver rejects unrecognised tokens during command-line parsing, so an nvcc reimplementation must not splat its full argv into the tileiras call. Only the schema above is accepted.

## Environment inheritance

tileiras inherits the full process environment of its parent because the subprocess harness sets no `envp` override at spawn time. Three families of variables matter for an nvcc-orchestrated build:

- **Toolkit discovery.** `CUDA_ROOT`, `CUDA_HOME`, `CUDA_PATH` resolve the install root used to locate libdevice and `nvdisasm`. The driver-side resolver falls back to a `/proc/self/exe` walk; the NVVM-side resolver does not. The hazard is described in [Driver Env Vars and Runtime Gates](../driver/env-vars-and-runtime-gates.md#cuda-root-resolution); an nvcc wrapper should export `CUDA_ROOT` explicitly rather than relying on the executable-path fallback.
- **PATH for downstream subprocesses.** tileiras spawns `ptxas` and `nvdisasm` by basename; both resolve through the inherited `PATH`. An nvcc orchestrator must keep the CUDA `bin/` directory on `PATH` for the inherited environment, otherwise `ptxas` will fail with exit code `127` and the diagnostic `"Program could not be executed"`.
- **Tileiras-specific gates.** `MLIR_ENABLE_EVO`, `PTX_KNOBS_PATH`, `TILEIR_*`, `TILE_AS_DEBUG_*`. The full table lives in [Env Var and Runtime Gate Catalog](../reference/env-var-and-runtime-gate-catalog.md). nvcc forwards them verbatim because it never strips environment variables before spawning a tool subprocess.

What nvcc does *not* forward is anything specific to its own option surface. The driver does not understand `-Xcicc`, `-Xptxas`, `-Xcompiler` analogues; their bodies are not threaded into the tileiras argv. An nvcc reimplementation that wants to pass per-tool tuning to tileiras must translate the option into one of the validated flags listed above or into a `PTX_KNOBS_PATH` file consumed by ptxas downstream.

## Fallback behaviour on failure

There is no automatic fallback from tileiras to cicc inside the tileiras process. A failed compile returns one of the five public error codes catalogued in [Driver Program Handle](../driver/program-handle.md#public-error-codes) with a verbatim diagnostic on stderr; partial output never lands on disk. Exit-code semantics from the nvcc perspective:

| tileiras exit | Meaning | nvcc-side handling that makes sense |
|---|---|---|
| `0` | success; `--output-file` exists | proceed to nvlink/fatbinary |
| `1` | allocation failure | fatal; nvcc should report and abort the build |
| `2` | configuration rejected (bad GPU, opt-level, debug/opt combo) | fatal; the upstream emitter chose an unsupported target tuple |
| `3` | bytecode parse failure (including MLIR fall-through hint) | fatal; the upstream emitter produced incompatible bytecode |
| `4` | null handle / not-compiled (only reachable through the C API) | not visible from the CLI |
| `5` | compile failure inside the MLIR pipeline | fatal; surface tileiras stderr verbatim |

The cicc path is not a contingency for tileiras failures. cicc accepts CUDA C++ source, not TileIR bytecode; the two compilers see disjoint inputs and cannot substitute for each other. An nvcc driver that wanted source-level retry would have to re-run the upstream emitter, which is outside the toolkit. The conservative orchestration is therefore: dispatch once, propagate the exit code, leave retry policy to the user.

The `--output-file` invariant is worth restating because nvcc relies on it: tileiras either writes the full host relocatable object atomically or writes nothing at all. nvcc can safely treat the presence of the output path as proof of success without secondary checks.

## Shared downstream: ptxas

Both pipelines converge at `ptxas`. The PTX text from `cicc` and from `tileiras` is produced by the same NVPTX backend, the same SelectionDAG instruction selector, and the same NVIDIA NVVM pass roster: `NVVMReflect`, `NVVMPeepholeOptimizer`, `BaseAddressStrengthReduce`, `MemorySpaceOpt`, `DeadSyncElim`, `CommonBaseElim`, `NVVMIRVerifier`, `IPMSPPass`, `NVPTXSetFunctionLinkagesPass`, `SelectKernelsPass`, `KernelInfoPrinter`, and `NVVMAA`. From `ptxas`'s perspective, the upstream identity of the PTX is invisible. PTX-to-SASS-to-cubin assembly is the same regardless of which compiler emitted the PTX.

## Host code path is unrelated

Neither `cicc` nor `tileiras` handles host code. The host translation unit is preprocessed by `nvcc`, split by `cudafe++`, and handed to the system C++ compiler. `tileiras` accepts `--host-arch` (`x86_64`, `aarch64`, `arm64ec`) and `--host-os` (`linux`, `windows`) only because its output is a host ELF relocatable object: those flags select the host triple of the wrapper ELF, not a host compiler. Host-side C++ compilation is orchestrated by nvcc and is independent of which device-code compiler is in use; both paths emit artifacts the host linker later combines with the host object file.

## MLIR DSL frontends that emit tileiras-bound `.mlir-bc`

Tileiras's input is a serialized MLIR module whose top-level dialect is `cuda_tile`. Its dialect cascade covers `cuda_tile`, `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, and `cutlass`. This dialect surface tells the story of which upstream producers are intended to feed `tileiras`:

- **CUTLASS-on-MLIR** is the most direct match. The `cutlass` dialect carries `pipeline.{acquire, tail, commit, wait}`, `tile_scheduler.work_tile_info`, `block_striped.{reduce, load, store}`, and `seq_bar` - the exact pipeline-orchestration vocabulary CUTLASS uses for collective mainloops, persistent kernels, and stream-K schedulers.
- **CuTe-DSL frontends.** The `cute` dialect (~50 ops: `cute.local_tile`, `cute.local_partition`, `cute.tile_to_shape`, `cute.add_offset`, `cute.size`, `cute.cosize`, divide family) implements the CuTe layout algebra at MLIR-IR level. Any DSL that produces tile-by-tile descriptions of GPU work in CuTe terms can target this dialect.
- **Triton-for-CUDA-on-MLIR.** A Triton backend that targets the `cuda_tile` dialect (instead of, or in addition to, the existing `triton-gpu` lowering) would produce input `tileiras` accepts. The `cuda_tile.{if, select, xori, constant, atomic_cas_tko, entry, for, make_tensor_view, optimization_hints}` surface is general enough to host SPMD-tile programs.
- **Custom DSLs and JIT pipelines.** The bytecode contract is open: any caller that constructs a `builtin.module` with a `cuda_tile` payload, a valid `"nvopt<O0>"`/`"<O1>"`/`"<O2>"`/`"<O3>"` tier attribute, and dialect references confined to the registered cascade can serialize and feed `tileiras`. Schema versions 13.1/13.2 are recognized.

These producers are upstream of `tileiras` and outside the nvcc toolkit's `bin/` directory. The integration point is the bytecode file: the producer writes it; nvcc dispatches to `tileiras`; the rest of the build proceeds identically to a `cicc`-emitted artifact.

The producer-side contract — kernel-signature rules, the `tt.*` attribute namespace, operand-order conventions per op family, the AttrTag wire-format divergence from upstream MLIR, and the common emission mistakes a frontend must avoid — is documented in [Frontend Contract and Tile IR Emission](../topics/frontend-contract-and-tile-ir-emission.md).

## Side-by-side architectural diagram

```text
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

The important invariant is that the choice happens before either compiler starts. Once PTX has been produced, the downstream assembly path no longer needs to know whether the source was CUDA C++ or TileIR bytecode.
