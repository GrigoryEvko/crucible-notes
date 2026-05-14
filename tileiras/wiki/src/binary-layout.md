# Program Layout

Tileiras is packaged as a standalone ELF executable with a large statically linked compiler stack inside it. The public fact that matters is not the address map of that executable; it is the way the executable is organized into compiler subsystems. This page gives a semantic map of those subsystems so the rest of the wiki has a shared vocabulary.

## Identity

| Property | Value |
| --- | --- |
| Tool role | CUDA Tile IR optimizing assembler |
| CUDA release | 13.1 |
| Toolkit banner | `Cuda compilation tools, release 13.1, V13.1.80` |
| LLVM lineage | Internal LLVM mainline snapshot identifying as `LLVM21.0.0git` |
| Input format | TileIR MLIR bytecode |
| Primary output | Host relocatable object containing compiled GPU code |
| Default output name | `elf.o` |
| Default GPU family | Blackwell-family target, normally `sm_100` |

Tileiras is not a C++ frontend. It does not parse CUDA C++, instantiate templates, or generate host stubs. It starts from serialized MLIR and lowers that IR into PTX and then into an assembled GPU payload.

## Subsystem Bands

The binary contains these major subsystems:

| Subsystem | Role |
| --- | --- |
| Driver and option handling | Parses command-line options, validates target configuration, resolves tool paths, and starts compilation. |
| TileIR bytecode reader | Reads the TileIR bytecode container, reconstructs MLIR modules, creates operations, and verifies the input dialect contract. |
| Dialect registry | Registers `cuda_tile`, `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, `cutlass`, `nvgpu`, `nvvm`, and LLVM dialect support. |
| Tile lowering pipeline | Runs dialect conversion, canonicalization, layout assignment, scheduling, async pipeline materialization, and target preparation. |
| TileAS scheduler | Computes staged schedules, resource constraints, and producer/consumer coordination values. |
| MLIR infrastructure | Provides operations, regions, types, attributes, storage uniquing, diagnostics, interfaces, rewrite patterns, and pass managers. |
| LLVM/NVVM lowering | Converts MLIR LLVM/NVVM dialects into an LLVM module with NVVM intrinsics and target attributes. |
| libdevice integration | Links device math bitcode, resolves reflection queries, inlines selected math functions, and cleans up unused bodies. |
| NVPTX backend | Optimizes LLVM IR, selects NVPTX instructions, verifies target-specific machine operations, and emits PTX text. |
| External tool harness | Invokes `ptxas` and, when requested, `nvdisasm` to assemble and inspect generated GPU code. |

## Runtime Flow

```text
process start
    -> parse CLI and create compile configuration
    -> read TileIR bytecode into an MLIR module
    -> register and verify TileIR dialects
    -> run TileIR and TileAS pass pipeline
    -> lower to LLVM/NVVM dialects
    -> materialize an LLVM module
    -> link libdevice and run LLVM/NVPTX passes
    -> emit PTX
    -> assemble with ptxas
    -> emit host relocatable object
```

This flow is the stable layout to keep in mind while reading detailed pages. The executable's physical section layout is an implementation detail; the compiler subsystem boundaries above are the parts that affect users and reimplementers.

## Data Lifetimes

Tileiras has three main data lifetimes:

| Lifetime | Data | Ends when |
| --- | --- | --- |
| Input lifetime | Raw bytecode buffer, command-line options, target configuration | The MLIR module and compile configuration have been built. |
| MLIR lifetime | Dialect operations, types, attributes, pass analyses, scheduler state | The module is translated to LLVM IR. |
| Backend lifetime | LLVM module, NVVM intrinsics, libdevice bodies, PTX text, cubin/object buffers | The final host object has been written. |

Keeping these lifetimes separate matters. For example, `cuda_tile` bytecode tags are not meaningful after dialect conversion, TileAS schedule analyses are not meaningful after LLVM materialization, and LLVM MachineIR details are not meaningful before instruction selection.

## Reimplementation Notes

A compatible implementation does not need to reproduce the executable's binary layout. It needs to reproduce the observable contracts:

- accepted bytecode structure and dialect schemas,
- command-line and target option behavior,
- pass ordering where it affects IR semantics,
- scheduler resource and dependence rules,
- lowering from TileIR dialects to NVVM/LLVM,
- libdevice link/reflect/inline behavior,
- NVPTX ABI and PTX emission conventions,
- subprocess behavior for `ptxas` and optional `nvdisasm`.

When a low-level implementation detail is important, the corresponding subsystem page describes it as an algorithm or invariant rather than as an address.
