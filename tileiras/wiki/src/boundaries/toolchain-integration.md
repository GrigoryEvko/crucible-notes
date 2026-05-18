# Toolchain Integration

## Abstract

The other pages in this section describe individual handoffs: tileiras versus cicc, tileiras versus cudafe++, the ptxas subprocess protocol, the place tileiras occupies in nvcc 13.1. This page joins those handoffs into a single end-to-end story for build engineers and integrators. It catalogues file formats at every stage, documents how the subprocess control flow nests, traces environment-variable inheritance from nvcc down to ptxas, and reconstructs three worked invocations so the reader can map their own build against the toolchain.

The goal is operational. A reimplementation of the nvcc dispatcher, an MLIR-emitting frontend that needs to feed tileiras directly, or a build system that wants to invoke tileiras as part of a custom packaging pipeline should be able to read this page and produce a correct invocation with no further reverse engineering.

## Position in the CUDA toolchain

```
        ┌─────────────────────────────────┐
        │  Source-level inputs (any form) │
        └──────────────┬──────────────────┘
                       │
        ┌──────────────┴───────────────┐
        │                              │
   .cu CUDA C++ source            MLIR-emitting frontend
        │                         (CUTLASS-on-MLIR, CuTe-DSL,
        │                          Triton-for-CUDA, custom)
        │                              │
        ▼                              ▼
  ┌───────────┐                  ┌──────────────────────┐
  │ cudafe++  │                  │ TileIR bytecode      │
  │ (host /   │                  │ (.tileir / .ctir /   │
  │  device   │                  │  .ctb;               │
  │  split)   │                  │  magic 7F 54 69 6C   │
  └─────┬─────┘                  │  65 49 52 00)        │
        │                        └──────────┬───────────┘
        │ host code                         │
        ▼                                   ▼
  system C++ compiler           ┌──────────────────────┐
  (gcc/clang/MSVC)              │ tileiras             │
        │                       │ (53-pass MLIR        │
        │                       │  pipeline, NVPTX     │
        │ device code           │  backend, ptxas      │
        ▼                       │  subprocess)         │
  ┌───────────┐                 └──────────┬───────────┘
  │ cicc      │                            │
  │ (EDG +    │           PTX text         │ PTX text emitted in-process
  │  NVVM)    │           (.ptx)           │
  └─────┬─────┘                            │
        │ PTX text                         │
        ▼                                  ▼
  ┌─────────────────────────────────────────────────┐
  │ ptxas (PTX → SASS, embedded in cubin)           │
  └──────────────────────┬──────────────────────────┘
                         │
                         ▼
                ┌──────────────────┐
                │ cubin / SASS     │
                └────────┬─────────┘
                         │
                         ▼
                ┌──────────────────┐
                │ nvlink           │
                │ (multi-cubin     │
                │  resolution)     │
                └────────┬─────────┘
                         │
                         ▼
                ┌──────────────────┐
                │ fatbinary +      │
                │ host linker      │
                └────────┬─────────┘
                         │
                         ▼
                    final binary
```

cicc and tileiras are sibling device-code compilers. They share the NVPTX backend below the LLVM-dialect handoff but accept disjoint inputs and never see each other's outputs. The convergence point is ptxas: both compilers hand PTX text to the same `ptxas` binary, and the rest of the build (cubin assembly, nvlink resolution, fatbinary embedding, host linking) is indistinguishable.

## File formats at each handoff

| Stage | Input | Output | Format reference |
|---|---|---|---|
| frontend → tileiras | TileIR MLIR bytecode | (none; tileiras receives) | [MLIR Bytecode Format](../bytecode/mlir-bc-format.md) |
| cudafe++ → cicc | CUDA C++ source + EDG IL | (none; cicc receives) | EDG IL — see cudafe++ wiki |
| cicc → ptxas | (none; cicc produces) | PTX text (`.ptx`) | PTX ISA reference manual |
| tileiras → ptxas | (none; tileiras produces) | PTX text (passed via `--input-as-string`) | PTX ISA reference manual; [ptxas Handoff Protocol](ptxas-handoff-protocol.md#ptx-text-protocol) |
| ptxas → cubin | PTX text | ELF cubin with `.text.<kernel>` SASS sections | CUDA Binary Utilities documentation |
| tileiras → nvlink/host linker | (none; tileiras produces) | Host ELF relocatable wrapping the cubin payload | [Driver main() Entry](../driver/main-entry.md#main); ELF specification |
| nvlink → fatbinary | Multiple cubins per arch | Multi-arch fatbin section | CUDA documentation |

Three format details matter operationally. First, TileIR bytecode begins with the 8-byte magic `7F 54 69 6C 65 49 52 00` (`"\x7fTileIR\0"`), distinguishing it from upstream MLIR bytecode whose magic is `ML\xefR`. The tileiras driver's parse failure on a non-TileIR input appends the hint `" (it looks like MLIR bytecode instead)"` to the error message, documented in [Driver Program Handle](../driver/program-handle.md#public-error-codes). Second, tileiras passes PTX to ptxas inline via `--input-as-string`, not through a temporary file; this bounds the maximum kernel PTX size to the platform `MAX_ARG_STRLEN`. Third, tileiras's terminal output is a host ELF relocatable object, not a raw cubin — the cubin produced by ptxas is embedded in the ELF along with an optional `.nvdisasm` SASS-text section.

## Subprocess control flow

The harness is two-level. Top level: nvcc (or an integrator) spawns tileiras. Bottom level: tileiras spawns ptxas and optionally nvdisasm.

```
nvcc (parent)
│
│ posix_spawn(tileiras, argv, envp, file_actions)
│ wait4() with optional alarm-based timeout
│
└── tileiras (child of nvcc, parent of ptxas)
    │
    │ posix_spawn(ptxas, argv_with_PTX_inline, envp_inherited, file_actions)
    │ wait4() with timeout enforced via SIGALRM
    │ stdout + stderr merged via dup2 into one accumulator
    │
    └── ptxas (child of tileiras)
        │
        │ writes assembled cubin to stdout
        │ writes diagnostics to stderr (merged at parent)
        │ exits with shell-style status code
        │
        └── (no further children for the PTX-to-SASS stage)
```

Both levels use `posix_spawn` as the fast path and fall back to `fork`+`exec` only when the caller requests `setsid` or process resource limits — see [Subprocess Harness](../driver/subprocess-harness.md#posix-subprocess-launcher) for the launcher contract. Timeouts ride on `SIGALRM`; the parent installs a temporary handler, arms `alarm(seconds)`, calls `wait4`, and on `EINTR` sends `SIGKILL` to the child before reaping.

The control-flow model has one important property: tileiras's process lifetime brackets ptxas's. If nvcc kills tileiras, the active ptxas child is orphaned and reparented to PID 1 with no further cleanup. An nvcc orchestrator that wants reliable cancellation should kill the entire process group rather than the tileiras leader alone; the easiest path is to spawn tileiras with `setsid` so the harness can `killpg` the resulting session.

## Environment-variable inheritance

The subprocess harness sets no explicit `envp` override at spawn time, so tileiras inherits the full nvcc environment, and ptxas inherits the full tileiras environment in turn. The chain is therefore:

```
nvcc environment
   └── tileiras environment (inherited)
          └── ptxas environment (inherited)
```

Variables that tileiras itself consumes are catalogued in [Env Var and Runtime Gate Catalog](../reference/env-var-and-runtime-gate-catalog.md). The high-impact subset for toolchain integration is:

- **Toolkit discovery.** `CUDA_ROOT`, `CUDA_HOME`, `CUDA_PATH`. Two resolvers inside tileiras walk this chain; one falls back to `/proc/self/exe`, the other does not. The hazard is documented in [Driver Env Vars and Runtime Gates](../driver/env-vars-and-runtime-gates.md#cuda-root-resolution); production builds should export `CUDA_ROOT` explicitly.
- **Subprocess discovery.** `PATH`. tileiras spawns ptxas and nvdisasm by basename; both need the CUDA `bin/` directory on `PATH`.
- **ptxas knob forwarding.** `MLIR_ENABLE_EVO` and `PTX_KNOBS_PATH`. AND-gated; setting only one is silently ignored. When both are set, tileiras appends `--knobs-file=<path>` to the ptxas argv. The knob-file grammar belongs to ptxas — see [ptxas Handoff Protocol](ptxas-handoff-protocol.md#knob-file-format).
- **TMA and swizzle policy.** `TILEIR_DELAY_TMA_STORE_WAIT`, `TILEIR_PREFER_TMA_FOR_LOAD_STORE`, `TILEIR_ALWAYS_SWIZZLE`. Pass-internal gates that affect codegen choices.
- **Debug.** `TILEIR_DEBUG_DUMP_BC`, `TILEIR_DEBUG_DUMP_LLVM`, `TILE_AS_DEBUG_UNLIMITED_SMEM`, `TILE_AS_DEBUG_VERBOSE`. Diagnostic switches; presence-only or string-equality against `"1"` depending on the variable.

ptxas reads its own environment variables (notably `PTXAS_KNOBS_DEFAULTS`) that tileiras does not interpret. An nvcc orchestrator must keep ptxas-specific variables in the parent environment for inheritance to work; tileiras does not synthesise them.

## Error propagation

Errors travel from the innermost child back to the outermost parent. Each level transforms the failure differently:

1. **ptxas → tileiras.** ptxas exits with a non-zero status and writes a diagnostic to stderr. tileiras's harness captures both the exit code and the merged stdout/stderr buffer. The diagnostic is forwarded verbatim through the driver's diagnostic callback; the tileiras driver returns exit code `5` (compile failure) from `tileirasProgramCompile`. tileiras does not retry, does not rewrite the diagnostic, and does not produce a partial output file. The exit-code table is in [ptxas Handoff Protocol](ptxas-handoff-protocol.md#exit-code-interpretation).
2. **tileiras → nvcc.** tileiras exits with one of the five public error codes from [Driver Program Handle](../driver/program-handle.md#public-error-codes). nvcc observes the exit code and the on-disk presence (or absence) of the `--output-file` path. A successful tileiras invocation leaves a complete relocatable object on disk; a failed invocation leaves nothing. nvcc cannot retry by falling back to cicc — the two compilers consume disjoint inputs.
3. **nvcc → user.** nvcc translates the tileiras exit code into one of its own driver-level messages and exits the build. The verbatim tileiras stderr (which itself may contain verbatim ptxas stderr) is preserved through the chain so the user can diagnose PTX-level issues.

The conservative rule for a reimplementation is to surface the deepest diagnostic without rewriting it. ptxas knows the most about why PTX was rejected; rewriting its message into "tileiras compile failed" or "nvcc subprocess failure" loses information that the user needs. The harness's merge-stderr-into-stdout optimisation makes verbatim forwarding cheap because the diagnostic arrives as one contiguous buffer.

## Worked invocations

### Release build of a CUTLASS-on-MLIR kernel

The upstream frontend has emitted `kernel.tileir` containing a `cuda_tile` payload targeted at sm_100. The user runs:

```bash
nvcc --gpu-architecture=sm_100 kernel.tileir -O2 -o app
```

The nvcc dispatcher classifies the input as TileIR bytecode (magic check) and constructs the tileiras invocation:

```bash
tileiras \
    --gpu-name=sm_100 \
    --opt-level=2 \
    --host-arch=x86_64 \
    --host-os=linux \
    --output-file=/tmp/nvcc-12345/kernel.tileir.o \
    /tmp/nvcc-12345/kernel.tileir
```

tileiras parses the bytecode, runs the 53-pass MLIR pipeline, emits PTX text into a heap buffer, and spawns ptxas as:

```bash
ptxas \
    -arch sm_100 \
    --opt-level 2 \
    --input-as-string '<PTX text inline>'
```

ptxas writes the assembled cubin to stdout. tileiras captures the bytes, embeds them in a host ELF relocatable along with an optional `.nvdisasm`-produced SASS section, and writes `/tmp/nvcc-12345/kernel.tileir.o`. nvcc picks up the file, links it with the host translation units through the host linker, and produces `app`.

### Debug build with line info

The user runs:

```bash
nvcc -G -lineinfo --gpu-architecture=sm_100 kernel.tileir -o app
```

nvcc translates `-G` to `--device-debug` and adds `--lineinfo`. The validator in tileiras rejects `--device-debug` unless `--opt-level=0`, so nvcc must dispatch:

```bash
tileiras \
    --gpu-name=sm_100 \
    --opt-level=0 \
    --device-debug \
    --lineinfo \
    --host-arch=x86_64 \
    --host-os=linux \
    --output-file=/tmp/nvcc-67890/kernel.tileir.o \
    /tmp/nvcc-67890/kernel.tileir
```

The downstream ptxas call is constructed at the same `--opt-level 0`, which suppresses most code transformations in ptxas. Debug-info preservation in the cubin is handled by the lowering pipeline; tileiras emits the appropriate `nvvm.*` debug attributes and PTX `.dwarf` directives during pipeline execution, not at the ptxas argv layer.

A user who omits the `-O0` part of the combination (for example by mixing `-G` with a project-wide `-O3` default) triggers tileiras's validator with the diagnostic `"optimized debugging is not supported, change optimization level to 0 or disable full debug info"` and exit code `2`. nvcc sees the exit code, surfaces the message, and aborts.

### Direct integrator invocation

An integrator building a custom packaging pipeline bypasses nvcc and drives tileiras directly:

```bash
MLIR_ENABLE_EVO=1 \
PTX_KNOBS_PATH=/etc/myproject/ptxas-knobs.cfg \
CUDA_ROOT=/opt/cuda-13.1 \
tileiras \
    --gpu-name=sm_103 \
    --opt-level=3 \
    --output-file=build/kernel.o \
    src/kernel.tileir
```

The two environment variables are AND-gated; both are required to forward `--knobs-file=/etc/myproject/ptxas-knobs.cfg` to ptxas. `CUDA_ROOT` is exported explicitly because the integrator does not want to rely on the `/proc/self/exe` fallback for libdevice resolution. The integrator owns process lifecycle, exit-code interpretation, and downstream linking; tileiras is treated as a one-shot transform with one input file and one output file.

## Cross-references

- [Position in nvcc 13.1 Toolchain](nvcc-13-1-position.md) covers the dispatch decision and argv contract from nvcc's side.
- [ptxas Handoff Protocol](ptxas-handoff-protocol.md) covers the tileiras-to-ptxas subprocess in depth, including the knob-file format.
- [tileiras vs cicc](cicc-comparison.md) catalogues the capability split between the two device-code compilers and the migration trajectory.
- [tileiras vs cudafe++ (Non-Relationship)](cudafe-non-relationship.md) clarifies that tileiras is not a cudafe++ replacement.
- [Driver main() Entry](../driver/main-entry.md) describes how the tileiras process orchestrates the compile internally.
- [Driver CLI Options](../driver/cli-options.md) enumerates every option in the tileiras argv schema.
- [Subprocess Harness](../driver/subprocess-harness.md) documents the POSIX launcher shared by every external tool invocation.
- [Env Var and Runtime Gate Catalog](../reference/env-var-and-runtime-gate-catalog.md) lists every environment variable the binary reads.
- [Driver Program Handle](../driver/program-handle.md) defines the public error codes returned through the tileiras exit status.
- [MLIR Bytecode Format](../bytecode/mlir-bc-format.md) specifies the TileIR bytecode shape that triggers the dispatch.
