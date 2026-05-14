# Driver Overview

## Abstract

`tileiras` is NVIDIA's TileIR optimizing assembler. It takes a TileIR
bytecode module, lowers it through the TileIR and NVVM pipeline, emits PTX,
invokes `ptxas`, and writes a host relocatable object. It is not a CUDA C++
front-end — no EDG, no cudafe, no host stub synthesis, no CUDA source parser
lives in this tool. Those stages must already have produced the TileIR
bytecode this driver consumes.

From the command line the driver behaves like a compact LLVM-style compiler:

```text
tileiras [driver options] <tileir-bytecode>
    -> parse TileIR bytecode as an MLIR builtin.module
    -> run TileIR, NVVM, and NVPTX lowering
    -> serialize PTX
    -> assemble PTX with ptxas
    -> optionally dump SASS through nvdisasm -c
    -> write a host relocatable object, default elf.o
```

The public contract stays deliberately small. Users select the GPU
architecture, host architecture, host OS, optimization/debug mode, optional
memcheck instrumentation, CUDA toolkit root, and output file. The large pass
inventory hiding behind that surface lives in the pipeline section of the
wiki.

## What the driver does

One translation unit per process invocation. The input is a TileIR
bytecode buffer (magic `7f 54 69 6c 65 49 52 00`, version `13.1.x`); the
output is a host relocatable object the driver writes to `--output-file`
or, by default, `elf.o`. Exit status is `0` on success or one of the five
error codes documented in
[Driver Program Handle](program-handle.md#public-error-codes); no partial
output is ever written.

The driver distinguishes TileIR bytecode from generic upstream MLIR
bytecode at the magic-number level. A stream that opens with the MLIR
framing prefix `06 03 80 0a 4d 4c 49 52` and the `"\nMLIR"` payload tag —
rather than the TileIR `"Tile\0"` tag in the same slot — is rejected with
a separate diagnostic that names MLIR bytecode explicitly, so the user
can route the input to the right tool instead of guessing whether a
parser failure means a corrupt file.

Validation runs before any pipeline construction. It rejects null buffers,
non-TileIR bytecode, unsupported GPU names, optimization levels above `3`,
and `--device-debug` paired with any nonzero optimization level. The
verbatim diagnostic strings and their error codes live in
[Driver CLI Options](cli-options.md#validation-algorithm).

## Supported Targets

| Surface | Accepted values | Default / effect |
| --- | --- | --- |
| `--gpu-name` | `sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121` | Defaults to `sm_100`. |
| `--host-arch` | `x86_64`, `aarch64`, `arm64ec` | Selects the host triple fragment. |
| `--host-os` | `linux`, `windows` | Selects the object and triple OS fragment. |
| `--sanitize` | `memcheck` | Adds TileIR memcheck instrumentation when present. |
| `--opt-level` / `-O` | `0`, `1`, `2`, `3` | Driver default is `3`. |
| `--lineinfo` | boolean | Emits line information without full device debug. |
| `--device-debug` / `-g` | boolean | Requires `-O0`; enables full device debug mode. |
| `--output-file` / `-o` | path | Defaults to `elf.o`. |

The target set is Blackwell-oriented. A clean-room implementation should
treat unsupported SM names as hard validation errors rather than silently
remap them to the closest known architecture.

## Driver Flow

The compile path is linear and has no user-visible subcommands:

```text
main
  parse argv against the cl::opt registry
  read positional TileIR bytecode file
  resolve CUDA toolkit root
  validate buffer, target, optimization level
  create an MLIRContext and register dialects
  parse bytecode into builtin.module
  attach host/GPU target tuple
  build the TileIR pass pipeline for the requested optimization level
  lower to NVVM and LLVM
  serialize PTX text
  invoke ptxas with PTX passed as --input-as-string
  optionally write cubin to a temporary file and run nvdisasm -c
  write the relocatable object bytes to disk
```

The only external tools on the default path are CUDA toolkit binaries.
`ptxas` receives PTX through `--input-as-string` and returns assembled
cubin bytes on stdout. The SASS dump path writes that cubin to a temporary
file, runs the configured disassembler command, and removes the temporary
file when the driver created it.

## Failure Model

Every failure prints a diagnostic and returns a nonzero exit status; the
driver never writes a partial output file. The user-visible categories are:

| Category | Typical trigger |
| --- | --- |
| Input missing | No positional TileIR bytecode file was provided. |
| Read failure | The input file cannot be opened or mapped. |
| Bytecode mismatch | The buffer is not TileIR bytecode. |
| Unsupported target | `--gpu-name`, `--host-arch`, or `--host-os` is outside the accepted set. |
| Invalid options | `--opt-level > 3` or `--device-debug` with nonzero optimization. |
| Toolkit failure | CUDA root cannot be resolved for an operation that requires the toolkit. |
| Compile failure | MLIR parsing, pass execution, PTX emission, or ptxas failed. |
| Dump failure | The configured SASS dump command failed or could not be executed. |

Errors are terminal for the current invocation by design. The driver makes
no attempt at partial output recovery after a pipeline or assembler failure
— a fresh invocation with corrected input is always cheaper than guessing
how much of a half-finished artifact is trustworthy.

## Related pages

[Driver main() Entry](main-entry.md) walks the entry-point code path in
detail; [Driver CLI Options](cli-options.md) catalogues every option and
its validator; [Driver Program Handle](program-handle.md) defines the
public error-code numbering; [Host Launch ABI and ptxas Knobs](host-launch-and-ptxas-knobs.md)
covers the kernel-launch metadata the driver emits into the produced
object.
