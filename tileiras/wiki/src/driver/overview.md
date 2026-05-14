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

## Compilation Contract

One translation unit per process invocation. The input is a TileIR bytecode
buffer (magic `7f 54 69 6c 65 49 52 00`, version `13.1.x`); the output is a
host relocatable object byte buffer that the private program handle owns
until the driver writes it to disk. Exit status is `0` on success or one of
the five error codes documented in
[Driver Program Handle](program-handle.md#public-error-codes); no partial
output is ever written.

A stream that looks like upstream MLIR bytecode — magic `06 03 80 0a 4d 4c 49 52`,
the same MLIR-bytecode framing prefix followed by `"\nMLIR"` instead of
`"Tile\0"` — is rejected with a distinct diagnostic so the user can route the
input to the right tool.

```c
int compile_tileir_file(const char *input_path,
                        const char *output_path,
                        const DriverOptions *options) {
    ByteBuffer input = read_file(input_path);
    if (!input.data)
        return report_read_error(input_path);

    const char *cuda_root = resolve_cuda_root(options->argv0);

    Program *program = NULL;
    int err = program_create(&program, input, options, cuda_root);
    if (err != 0)
        return err;

    err = program_compile(program);
    if (err == 0)
        err = write_file(output_path ? output_path : "elf.o", program_output(program));

    program_release(program);
    return err;
}
```

Validation rejects null buffers, non-TileIR bytecode, unsupported GPUs,
optimization levels above `3`, and `--device-debug` with any nonzero
optimization level. The bytecode-mismatch diagnostic also detects a stream
that looks like generic MLIR bytecode and explains that TileIR bytecode is
required.

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
  register LLVM command-line options
  parse argv
  read positional TileIR bytecode file
  resolve CUDA toolkit root
  validate options and allocate program handle
  create an MLIR context
  register TileIR, Cute, CUTLASS, GPU, LLVM, and NVVM dialects
  parse bytecode into builtin.module
  build the TileIR pass pipeline for the requested optimization level
  lower to NVVM and LLVM
  serialize PTX text
  invoke ptxas with PTX passed as an argv string
  optionally write cubin to a temporary file and run nvdisasm -c
  store object bytes on the program handle
  write output bytes to disk
  release the handle and command-line storage
```

The only external tools on the default path are CUDA toolkit binaries.
`ptxas` receives PTX through `--input-as-string` and returns assembled cubin
bytes on stdout. The SASS dump path writes that cubin to a temporary file,
runs the configured disassembler command, then removes the temporary file if
the driver created it.

## Failure Model

Every failure is reported before the driver tears down its private session
state. The user-visible categories that matter:

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

Errors are terminal for the current invocation by design. The driver never
attempts partial output recovery after a pipeline or assembler failure.
