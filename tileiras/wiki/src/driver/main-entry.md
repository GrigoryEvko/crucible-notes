# Driver main() Entry

## Abstract

The `tileiras` process entry point is a conventional LLVM-style compiler
driver. It builds the command-line option schema, parses `argv`, reads the
positional TileIR bytecode file into memory, creates a private program
handle, runs the compile dispatcher, writes the produced object bytes, and
tears down command-line storage. The default output path is `elf.o` —
the normal artifact is a host relocatable object, not a raw PTX file or a
standalone cubin.

**End-to-end contract.** In: an `argv` vector whose first non-option positional
element is a TileIR bytecode file (magic `7f 54 69 6c 65 49 52 00`, version
`13.1.x`, with at least the String and Func sections present). Out: either
a host relocatable object file written to `--output-file` (default `elf.o`)
and exit status `0`, or one of the five integer error codes from
[Driver Program Handle](program-handle.md#public-error-codes) with a verbatim
diagnostic on stderr and no output file written. A failed compile leaves
the filesystem untouched — partial output never happens.

No lowering happens in the outer driver frame. The entry point is pure
orchestration: it owns option lifetime, error routing, file I/O, and the
sequence that calls into the TileIR compiler proper.

## main() Flow

The body splits into four phases, with construction and destruction paired
so every option object and program resource is released in reverse order.

| Phase | Role |
| --- | --- |
| Initialize options | Register `--opt-level`, `-O`, `--lineinfo`, `--device-debug`, `-g`, input, output, target, and sanitizer options. |
| Validate input | Check the bytecode buffer, supported GPU, optimization range, and debug/optimization compatibility. |
| Compile | Register MLIR dialects, parse bytecode into `builtin.module`, run the pipeline, emit PTX, and assemble. |
| Cleanup | Release the program handle, output buffers, and command-line option storage. |

The sequence is simple enough to reimplement as a straight-line driver:

```c
int tileiras_main(int argc, char **argv) {
    DriverOptions opts = default_driver_options();
    int parse_status = parse_tileiras_options(argc, argv, &opts);
    if (parse_status != 0)
        return parse_status;

    if (opts.input_path == NULL)
        return report_error("no input file provided");

    ByteBuffer input = read_file(opts.input_path);
    if (!input.data)
        return report_input_read_error(opts.input_path);

    Program *program = NULL;
    int err = create_program(&program, &opts, input);
    if (err == 0)
        err = compile_program(program);
    if (err == 0)
        err = write_file(opts.output_path, program_output(program));

    release_program(program);
    destroy_driver_options(&opts);
    return err;
}
```

## Opt-level Dispatch

`-O` is an alias for `--opt-level`. Driver default is `3`, accepted range
`0..3`. The parsed integer is copied into the program configuration before
the program handle is allocated.

The embedded pass pipeline has its own option named `opt-level`, but its
default is `2` when the pipeline is built outside the driver wrapper. The V2
pipeline carries a separate `v2-opt-level` field that defaults to `0`. A
reimplementation should keep those fields distinct and only copy the driver
optimization level into the pipeline object when reproducing the command-line
driver's behavior.

Full device debug carries one hard invariant: it cannot be combined with a
nonzero optimization level. With `--device-debug` set, the driver expects
`-O0`, and the NVVM option string then carries debug-preserving options like
`-g`, `--dont-merge-basicblocks`, and `--return-at-end`.

## cl::opt Parse

The option parser uses LLVM command-line behavior and the banner:

```text
tileiras: NVIDIA (R) Cuda Tile IR optimizing assembler
```

After parsing, the positional input file is mandatory. A missing file makes
the driver report `error: no input file provided` and exit before
constructing the program handle. A file that cannot be read produces a
diagnostic carrying both the path and the operating-system error message.

The target selectors are fixed-value enums:

| Option | Values |
| --- | --- |
| `--gpu-name` | `sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121` |
| `--host-arch` | `x86_64`, `aarch64`, `arm64ec` |
| `--host-os` | `linux`, `windows` |
| `--sanitize` | `memcheck` |

`--sanitize=memcheck` is the only sanitizer selector recovered in the driver
surface. Setting it appends the memcheck and tensor-memory access-check
options to the downstream tool configuration.

## MLIRContext Setup

The compile dispatcher creates an `MLIRContext` and registers every dialect
the TileIR stack can parse or lower:

| Dialect family | Purpose |
| --- | --- |
| `cuda_tile` | Input TileIR operations and target metadata. |
| `nv_tileaa` | Tile-level analysis and allocation representation. |
| `nv_tileas` | Tile assembler scheduling and memory-operation representation. |
| `cute_nvgpu` | Cute/NVGPU atoms and Blackwell copy/MMA forms. |
| `cutlass` | CUTLASS-style scheduling and pipeline abstractions. |
| `gpu`, `llvm`, `nvvm` | Upstream lowering targets for host/device IR. |

The dispatcher then parses the bytecode into a `builtin.module`. A
late-registration fallback for `cuda_tile` covers the case where MLIR
bytecode references a dialect before the usual eager load path has made it
visible in the context. After parsing, the host/GPU target tuple is attached
to the module and the pipeline root is built for `builtin.module`.

```c
int compile_program(Program *program) {
    MLIRContext ctx;
    register_tileiras_dialects(&ctx);

    ModuleOp module = parse_tileir_bytecode(&ctx, program->input, program->opt_level);
    if (!module)
        return error("input does not correspond to Tile IR bytecode");

    attach_target(module, program->gpu, program->host_arch, program->host_os);

    PassManager pm = build_tileir_pipeline(program->pipeline_options);
    if (run_pipeline(pm, module) != SUCCESS)
        return error("failed to compile Tile IR program");

    program->output = assemble_module_to_object(module, program);
    program->has_output = true;
    return program->output.data ? 0 : error("failed to compile Tile IR program");
}
```

## Teardown Semantics

Driver-owned cleanup stays strictly local: option objects, aliases, file
buffers, the program handle, and any output bytes are released before
process exit. MLIR's dialect storage and uniqued type/attribute storage are
global runtime objects torn down by their normal runtime destructors after
`main` returns — not part of the driver phase graph, and not modeled as extra
compile phases.

The distinction matters for reimplementers. The driver should free exactly
the resources it owns and never try to manually destroy global dialect
singletons owned by MLIR support code.
