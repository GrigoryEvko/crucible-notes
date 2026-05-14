# Driver main() Entry

## Abstract

`tileiras` is a conventional LLVM-style compiler driver. The process entry
point parses `argv` against an option schema registered during static
initialization, reads the positional TileIR bytecode file, validates the
buffer and the requested target, runs the TileIR-to-object pipeline inside
a fresh `MLIRContext`, and writes the resulting host relocatable object —
default path `elf.o` — to disk. The artifact is a relocatable object, not
a raw PTX file or a standalone cubin.

The end-to-end contract is narrow. The input is an `argv` vector whose
first non-option positional element is a TileIR bytecode file (magic
`7f 54 69 6c 65 49 52 00`, version `13.1.x`, with at least the String and
Func sections present). The output is either a host relocatable object
written to `--output-file` and exit status `0`, or one of the five integer
error codes from
[Driver Program Handle](program-handle.md#public-error-codes) with a
verbatim diagnostic on stderr and no output file written. A failed compile
leaves the filesystem in its prior state — partial output never happens.

No lowering happens in the outer driver frame. The entry point is pure
orchestration: it owns option lifetime, error routing, file I/O, and the
call sequence into the TileIR compiler proper.

## Static-init Option Registration

Every command-line flag is an `llvm::cl::opt<T>` global constructed during
C++ static initialization, long before `main` runs. Each global registers
itself with the process-wide `cl::OptionRegistry` from its constructor, so by
the time `main` reaches `cl::ParseCommandLineOptions` the parser already
knows every flag, every alias, every default, and every `cl::values(...)`
mapping table. Option objects live for the entire process lifetime and are
not torn down by the driver; their storage belongs to the LLVM CommandLine
library.

The four enum-valued flags (`--gpu-name`, `--host-arch`, `--host-os`,
`--sanitize`) are template instantiations of `cl::opt<cl::ValuesClass>`
that share a single parser shape: a `cl::values(...)` table that maps each
accepted string to an `int32` code, plus a default integer. The driver
never sees the raw spelling — by the time the parser returns, each option
holds an integer the rest of the compiler can switch on.

## main()

`main` is pure orchestration. It runs the LLVM command-line parser, reads
the positional bytecode file, builds an `MLIRContext` with the dialects the
TileIR stack can parse or lower, dispatches to the compile entry, writes
the resulting object bytes, and returns one of the five public error codes
defined in [Driver Program Handle](program-handle.md#public-error-codes).

The compile dispatcher is structured so every failure path returns before
any artifact reaches disk. There is no partial-output mode and no rollback
logic — a failed compile leaves the filesystem in its prior state, with
diagnostics already on stderr.

```c
int main(int argc, char **argv) {
    cl::ParseCommandLineOptions(argc, argv,
        "tileiras: NVIDIA (R) Cuda Tile IR optimizing assembler\n");

    if (InputFile.empty()) {
        errs() << "error: no input file provided\n";
        return DRIVER_ERR_INPUT_MISSING;          // code 1
    }

    auto buffer = MemoryBuffer::getFile(InputFile);
    if (!buffer) {
        errs() << "error: cannot read '" << InputFile
               << "': " << buffer.getError().message() << "\n";
        return DRIVER_ERR_INPUT_READ;             // code 4
    }

    int err = validate_driver_options(buffer->getMemBufferRef());
    if (err != 0)
        return err;                                // codes 2, 3

    MLIRContext ctx;
    register_tileiras_dialects(ctx);

    OwningOpRef<ModuleOp> module = parseSourceString<ModuleOp>(
        buffer->getBuffer(), &ctx);
    if (!module) {
        errs() << "error: failed to parse IR bytecode\n";
        return DRIVER_ERR_COMPILE;                 // code 5
    }

    attach_target(*module, GpuName, HostArch, HostOs);

    PassManager pm(&ctx);
    build_tileir_pipeline(pm, make_pipeline_options());
    if (failed(pm.run(*module))) {
        errs() << "error: failed to compile Tile IR program\n";
        return DRIVER_ERR_COMPILE;                 // code 5
    }

    SmallString<0> object;
    raw_svector_ostream os(object);
    if (failed(emit_relocatable_object(*module, os))) {
        errs() << "error: failed to emit relocatable object\n";
        return DRIVER_ERR_COMPILE;                 // code 5
    }

    auto write_err = writeToOutput(OutputFile.empty() ? "elf.o" : OutputFile,
                                   object);
    return write_err ? DRIVER_ERR_OUTPUT_WRITE : 0;
}
```

The branches above are the real branches: every diagnostic the driver can
emit before reaching the pipeline maps to one of them. The compile path
itself produces only the generic compile-failure diagnostic — finer-grained
pipeline errors print from inside the pass that failed, through MLIR's
diagnostic engine, before control returns to `main`.

## Opt-level Dispatch

`-O` is an alias for `--opt-level`. The driver default is `3`, the accepted
range is `0..3`, and the validated integer is copied into the pipeline
options before the pass manager is built.

The embedded pass pipeline carries its own `opt-level` field with default
`2`, used when the pipeline runs outside the driver wrapper. The V2
pipeline carries a third field, `v2-opt-level`, defaulting to `0`. These
are three distinct axes — collapsing them produces silently different
behavior for integrators who embed the pipeline directly.

Full device debug carries one hard invariant: it cannot coexist with a
nonzero optimization level. With `--device-debug` set, the driver demands
`-O0`, and the NVVM option string then carries debug-preserving options
such as `-g`, `--dont-merge-basicblocks`, and `--return-at-end`. The
validator rejects the combination rather than silently degrading an
optimized build, because the user's intent — preserved control flow for a
debugger — is incompatible with the transforms `-O>0` would run.

## Diagnostics Before the Pipeline

Two diagnostics fire from `main` before the compile dispatcher even
constructs an MLIR context. A missing positional argument produces
`error: no input file provided` and returns the input-missing code. A
file that cannot be opened or mapped produces a diagnostic carrying both
the path and the operating-system error message and returns the read-error
code. The full validator — bytecode magic check, GPU support, optimization
range, debug/optimization compatibility — runs immediately after the file
is in memory and emits the verbatim diagnostics catalogued in
[Driver CLI Options](cli-options.md#validation-algorithm).

`--sanitize=memcheck` is the only sanitizer selector accepted. Setting it
appends the memcheck and tensor-memory access-check options to the
downstream tool configuration.

## MLIRContext and Dialect Registration

A single `MLIRContext` lives for the duration of one compile and owns every
op, type, and attribute the pipeline allocates. Before bytecode parsing
starts, the driver eagerly loads every dialect the TileIR stack can parse
or lower:

| Dialect family | Purpose |
| --- | --- |
| `cuda_tile` | Input TileIR operations and target metadata. |
| `nv_tileaa` | Tile-level analysis and allocation representation. |
| `nv_tileas` | Tile assembler scheduling and memory-operation representation. |
| `cute_nvgpu` | Cute/NVGPU atoms and Blackwell copy/MMA forms. |
| `cutlass` | CUTLASS-style scheduling and pipeline abstractions. |
| `gpu`, `llvm`, `nvvm` | Upstream lowering targets for host/device IR. |

Eager registration matters because MLIR bytecode references dialects by
name from its symbol table. If a dialect is not loaded by the time the
parser hits its first op, parsing fails with an unresolved-dialect
diagnostic. The driver therefore loads every dialect the pipeline could
need, and includes a late-registration fallback specifically for
`cuda_tile` to cover bytecode that references the dialect through a
form the eager path has not yet materialized.

After the module parses, the driver attaches the host/GPU target tuple
through a target-attribute setter on `builtin.module`. The pass manager is
then constructed against the module, and the pipeline is built with the
options object derived from the parsed command line.

## Teardown Semantics

Driver-owned cleanup is strictly local. File buffers, the MLIR context,
the parsed module, and any output bytes go out of scope when `main`
returns; LLVM's CommandLine library owns the `cl::opt` globals and tears
them down through static destructors. MLIR's dialect registry and uniqued
type/attribute storage are global runtime objects destroyed by their normal
destructors after `main` exits — they are not part of the driver phase
graph and not modeled as extra compile phases.

The distinction matters for a reimplementer building a long-lived embedding
of the driver. The driver should free exactly the resources it owns and
never manually destroy global dialect singletons owned by MLIR support
code. Doing so corrupts the global registry for any subsequent compile in
the same process.

## Related pages

[Driver Overview](overview.md) frames the surrounding pipeline from the
user's perspective; [Driver CLI Options](cli-options.md) catalogues every
option `main` consumes, including the validator error codes referenced
here; [Driver Program Handle](program-handle.md) documents the public
error-code numbering returned through `main`'s exit status.
