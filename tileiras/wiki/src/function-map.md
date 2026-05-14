# Subsystem Function Map

## Abstract

Tileiras is a single executable, but it behaves as a stack of cooperating subsystems with a fairly rigid call-graph shape. This page is the function map for that stack at the role level: for each subsystem, the conceptual entry points it exposes, the callers it answers to, the callees it dispatches into, and the canonical wiki page where the mechanism is described in depth. The page is meant to answer the question "if I want to find where X happens in this compiler, where do I look?" without requiring a reader to know any internal address.

## Call-Graph Shape at the Subsystem Level

The driver is the only entry. It builds a compile configuration, opens the input file, and calls into the bytecode reader. The reader returns an MLIR module rooted in `cuda_tile`. The driver hands that module, together with the configuration, to the pipeline builder. The pipeline builder asks the dialect registry for the operations and types it will see, asks the lowering subsystem for the conversion patterns it will use, and asks the scheduler for the analysis passes it must register. Pass execution then walks the registered passes in order: dialect-to-dialect lowering rewrites operations in place; the scheduler runs as an embedded phase that produces a `ScheduleAnalysis` without changing the IR; subsequent materialization passes consume that analysis to emit pipe and mutex operations. After the IR reaches the LLVM/NVVM dialects, the codegen subsystem translates it to an `llvm::Module`, links libdevice bitcode, runs the LLVM IR pipeline, and hands the result to the NVPTX backend. The backend produces PTX text. The driver then invokes `ptxas` as a subprocess, captures its object output, and writes the final host relocatable.

In short: driver -> bytecode -> dialects (via the registry) -> lowering -> scheduler (inside lowering) -> codegen -> libdevice link -> NVPTX backend -> ptxas. The MLIR infrastructure subsystem is orthogonal to that chain; every layer above calls into it for operations, types, attributes, regions, contexts, diagnostics, pattern rewriting, and pass management.

## Driver

The driver owns process-level behavior: command-line parsing, target validation, CUDA installation discovery, output naming, subprocess invocation, and error reporting.

| Conceptual entry point | Role |
| --- | --- |
| `parse_command_line` | Read argv into a structured compile configuration; reject malformed flags. |
| `resolve_cuda_installation` | Locate the CUDA toolkit root, the libdevice bitcode, and the ptxas binary. |
| `validate_target` | Check that the requested GPU target is supported by this build of the assembler. |
| `open_input_module` | Map the input file and hand its buffer to the bytecode reader. |
| `run_compilation` | Drive the pipeline against the parsed module and the configuration. |
| `invoke_ptxas` | Spawn ptxas with the derived option set and stream its output. |
| `emit_host_object` | Wrap the cubin or PTX payload into the host relocatable. |
| `report_error` | Format a subprocess or pipeline failure as a user-facing diagnostic. |

Callers: process entry. Callees: bytecode reader, pipeline builder, codegen, subprocess harness. See [Driver Overview](driver/overview.md), [CLI Options](driver/cli-options.md), [Subprocess Harness](driver/subprocess-harness.md).

## Bytecode Reader

The reader owns the on-disk TileIR format. It validates the container, reconstructs the string and attribute tables, and rebuilds MLIR operations and regions in memory.

| Conceptual entry point | Role |
| --- | --- |
| `read_container_header` | Check the bytecode magic and version, locate the section table. |
| `read_string_section` | Restore the interned string pool from its compressed form. |
| `read_dialect_section` | Resolve each referenced dialect against the dialect registry. |
| `read_type_and_attribute_sections` | Reconstruct uniqued type and attribute values. |
| `read_operation_section` | Walk the operation stream, building regions and blocks. |
| `verify_module` | Invoke the MLIR verifier on the reconstructed module. |

Callers: driver `open_input_module`. Callees: dialect registry, MLIR infrastructure (storage uniquer, operation builder). See [MLIR Bytecode Format](bytecode/mlir-bc-format.md).

## Dialect Stack

The dialect subsystem registers operations, types, attributes, interfaces, verifiers, folders, and printers for every dialect the compiler understands. Lookup goes through `OperationName` and `RegisteredOperationName`, which is the bridge between a mnemonic and its implementation.

| Dialect | Conceptual role | Page |
| --- | --- | --- |
| `cuda_tile` | Public tile input dialect; what the bytecode actually contains. | [cuda_tile](dialects/cuda_tile/) |
| `nv_tileaa` | Alias-aware memory, token, queue, and pointer layer. | [nv_tileaa](dialects/nv_tileaa/) |
| `nv_tileas` | Operationally-scheduled async memory and TMA layer. | [nv_tileas](dialects/nv_tileas/) |
| `cute` | Target-neutral layout algebra. | [cute](dialects/cute/) |
| `cute_nvgpu` | NVIDIA-architecture atom registry. | [cute_nvgpu](dialects/cute_nvgpu/) |
| `cutlass` | CUTLASS pipeline and tile-scheduler abstractions. | [cutlass](dialects/cutlass/) |
| `nvgpu` | Stock MLIR GPU bridge layer. | [nvgpu](dialects/nvgpu/) |
| `nvvm` | PTX-facing intrinsic dialect. | [nvvm](dialects/nvvm/) |

Each dialect exposes the same role-level entry points: `register_operations`, `register_types`, `register_attributes`, `register_interfaces`, and per-operation `verify` / `fold` / `print` / `parse` hooks. Callers: bytecode reader (to resolve mnemonics on load), lowering (to identify source-dialect operations), MLIR infrastructure (to build operations and unique types).

## Lowering

The lowering subsystem owns dialect-to-dialect conversion. It is a set of pattern populations driven by `ConversionTarget` and `DialectConversion`. Each lowering page describes the patterns for one source-to-target hop.

| Conceptual entry point | Role |
| --- | --- |
| `populate_<src>_to_<tgt>_patterns` | Register the rewrite patterns for one conversion edge. |
| `configure_conversion_target` | Mark legal and illegal operations for the current hop. |
| `build_type_converter` | Wire source-dialect types to target-dialect types and addr-space rules. |
| `apply_full_conversion` | Run the pattern set to fixpoint or fail with a diagnostic. |
| `apply_partial_conversion` | Run a target-bounded conversion where some operations remain. |

Conversion edges, in order along the main path: `cuda_tile` -> `nv_tileaa` -> `nv_tileas` -> LLVM/NVGPU -> NVVM. See [Lowering Overview](lowering/overview.md) and the per-edge pages under `lowering/`.

## Scheduler

The scheduler is a phase inside lowering. It does not modify operations; it computes a `ScheduleAnalysis` (stage, order, and resource placement per operation) that later materialization passes consume.

| Conceptual entry point | Role |
| --- | --- |
| `build_constraints` | Walk the loop region, build dependence and resource constraints. |
| `compute_mii_bounds` | Compute resource, recurrence, fine-density, and dependence lower bounds on `II`. |
| `place_groups` | Choose an `II` and place operation groups into the resource reservation table. |
| `solve` | Run the linear schedule solve over a fixed placement. |
| `materialize_pipes_and_mutexes` | Emit `Pipe_` and `Mutex_` values once placement is fixed. |

Callers: the TileAA-to-TileAS conversion edge, plus the warp-specialize pipelines. Callees: MLIR infrastructure (analysis manager, attribute storage). See [Scheduler Overview](scheduler/overview.md) and [Modulo Scheduler](scheduler/modulo-scheduler-and-rau.md).

## Codegen and NVPTX Backend

Codegen is the boundary between MLIR and the LLVM toolchain. It produces an `llvm::Module`, links libdevice, runs the LLVM IR pipeline, then hands control to the NVPTX backend, which selects instructions and prints PTX.

| Conceptual entry point | Role |
| --- | --- |
| `translate_module_to_llvm` | Convert the LLVM/NVVM-dialect module into an in-memory `llvm::Module`. |
| `link_libdevice` | Resolve libdevice math calls against the bundled bitcode. |
| `run_llvm_passes` | Run the LLVM IR pipeline (NVVM reflection, address-space opt, arg lowering, etc.). |
| `select_nvptx_instructions` | Match LLVM IR against the NVPTX matcher table to produce MachineIR. |
| `verify_nvptx_machine_ir` | Run NVPTX-specific machine verifiers before emission. |
| `emit_ptx_text` | Print the final PTX module. |

Callers: pipeline driver, after lowering reaches LLVM dialect. Callees: libdevice subsystem, MLIR translation infrastructure. See [Codegen Overview](codegen/overview.md), [NVPTX Backend Passes](nvptx-passes/overview.md).

## libdevice

libdevice is the bundled bitcode that supplies math intrinsics. It is loaded once per compilation and integrated by the LLVM IR pipeline.

| Conceptual entry point | Role |
| --- | --- |
| `load_libdevice_bitcode` | Parse the bundled `libdevice.*.bc` into an `llvm::Module`. |
| `resolve_nvvm_reflect` | Replace `__nvvm_reflect` calls with the compile-time reflection answer. |
| `inline_selected_math` | Inline the math functions whose bodies are pulled into the kernel module. |
| `prune_unused_bodies` | Drop libdevice functions that survived as unused declarations. |

Callers: codegen during the LLVM IR pipeline. See [libdevice Overview](libdevice/overview.md), [NVVM Reflect Mechanism](libdevice/nvvm-reflect-mechanism.md).

## MLIR Infrastructure

The infrastructure is the shared runtime model every other subsystem depends on.

| Conceptual entry point | Role |
| --- | --- |
| `MLIRContext::getOrLoadDialect` | Look up or create a dialect in the current context. |
| `StorageUniquer::getOrCreate` | Intern a type or attribute storage object. |
| `OperationName::resolve` | Bind a mnemonic to its registered operation hook table. |
| `OpBuilder::create` | Build a new operation with operands, results, attributes, and regions. |
| `PatternApplicator::matchAndRewrite` | Drive pattern matching against an operation. |
| `PassManager::run` | Execute the configured pass pipeline against a module. |
| `Diagnostic::emit` | Surface an error or warning attached to a location. |

Callers: every other subsystem in this map. See [MLIR Infrastructure Overview](mlir-infra/overview.md), [Storage Uniquer](mlir-infra/storage-uniquer-and-context-impl.md), [Operation Layout](mlir-infra/operation-layout.md).

## How to Use This Map

Locate the behavior you want to understand by role: input parsing belongs to the driver and bytecode reader; semantic transformation belongs to lowering; placement decisions belong to the scheduler; libdevice math belongs to libdevice; PTX selection and emission belong to codegen and the NVPTX backend. The "page" link in each subsystem section is the canonical entry; child pages drill into one specific mechanism per page.
