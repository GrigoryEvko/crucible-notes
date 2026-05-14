# Tileiras - MLIR-Based Optimizing Assembler

Tileiras is NVIDIA's CUDA Tile IR optimizing assembler, shipped with CUDA 13.1 as a separate compiler binary. It consumes serialized MLIR bytecode for a tile program, lowers that program through NVIDIA tile dialects and NVPTX code generation, invokes the assembler toolchain, and writes a host relocatable object containing the compiled GPU payload.

The useful way to think about tileiras is not as a C++ compiler and not as a replacement for `cudafe++`. Tileiras starts after a frontend has already described the GPU work in MLIR. Its job is to make that tile-level program executable on Blackwell-family GPUs.

This wiki is written for two practical readers:

- If you use or integrate tileiras, the driver, option, bytecode, and subprocess pages explain what inputs the tool accepts, which target modes are valid, which external tools must be available, and how failures should be interpreted.
- If you are reimplementing compatible tooling, the subsystem pages describe observable contracts: bytecode structure, dialect schemas, pass ordering, scheduler invariants, lowering decisions, diagnostics, and pseudocode-level algorithms.

## At a Glance

| Item | Value |
|---|---|
| Program role | MLIR bytecode to host ELF relocatable with embedded GPU code |
| CUDA release | 13.1, toolkit build `V13.1.80` |
| LLVM lineage | Internal LLVM main-branch snapshot identifying as `LLVM21.0.0git` |
| Default GPU target | `sm_100` |
| Accepted driver targets | `sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121` |
| Default output | `elf.o` |
| Main input language | Binary MLIR bytecode carrying `cuda_tile` programs |
| Main output path | Tile dialects -> NVVM/LLVM -> NVPTX -> `ptxas` -> host object |

## What Tileiras Does

Tileiras is an optimizing assembler in the MLIR sense. It accepts an already-formed module, validates that the module uses the dialect versions it understands, runs a target-specific lowering pipeline, schedules and legalizes tile operations, emits PTX through the NVPTX backend, and delegates final machine-code assembly to `ptxas`.

The input is not CUDA C++, and tileiras does not perform preprocessing, C++ parsing, EDG lowering, template instantiation, or host-stub generation. Those responsibilities belong to other CUDA tools. Tileiras is the compiler for a lower-level tile IR surface.

The broad flow is:

```text
tileiras bytecode
    -> parse builtin.module
    -> load cuda_tile, nv_tileaa, nv_tileas, cute, cute_nvgpu, cutlass
    -> lower tile program toward LLVM and NVVM
    -> run TileAS scheduling, layout, TMA, pipeline, and cluster passes
    -> run NVPTX code generation
    -> run ptxas
    -> optionally run nvdisasm -c for annotated disassembly payloads
    -> emit host ELF relocatable
```

## Public Contract

For integration work, treat tileiras as a narrow bytecode-to-object compiler.

1. Produce MLIR bytecode for a `builtin.module` whose dialect tables match the CUDA 13.1 tile dialect schema.
2. Select one of the supported Blackwell-family targets.
3. Provide host, optimization, debug, line-info, output, and sanitizer options through the driver interface.
4. Ensure `ptxas` is available. Some configurations also require `nvdisasm` because the compile pipeline shells out to it.
5. Consume the produced object file, normally `elf.o`, as a host relocatable carrying the device payload.

The driver has a deliberately small option surface compared with `nvcc` or `cicc`: target GPU, host architecture, host OS, optimization level, line info, device debug, sanitizer mode, and output path. Most of the complexity is inside the bytecode reader and pass pipeline, not in command-line dispatch.

## Compiler Model

Tileiras lowers across nine dialect layers. The early dialects preserve tile semantics; the middle dialects make layout, memory, and scheduling explicit; the late dialects bridge into NVVM and LLVM.

| Dialect | Role |
|---|---|
| `cuda_tile` | Public bytecode-facing tile program surface: blocks, tiles, async operations, atomics, and high-level tensor actions. |
| `nv_tileaa` | Alias-aware layer with typed pointer, token, and view operations. It makes memory-space and aliasing facts explicit enough for later rewriting. |
| `nv_tileas` | Assembler-near layer for schedules, layouts, execution units, TMA descriptors, pipeline state, and resource decisions. |
| `cute` | Layout algebra and tile decomposition primitives. |
| `cute_nvgpu` | NVIDIA GPU atom layer: MMA atoms, TMA, WGMMA, tcgen05, LDSM/STSM, and cluster-specific operations. |
| `cutlass` | Pipeline, scheduler, sequence-barrier, and block-striped primitives reused from the CUTLASS programming model. |
| `mlir::nvgpu` | Generic NVIDIA GPU bridge dialect used before NVVM lowering. |
| `NVVM` | LLVM IR with NVPTX intrinsics and NVIDIA memory-space semantics. |
| `llvm` | Final LLVM IR representation consumed by the NVPTX backend. |

The central reimplementation point is that every stage has a structural contract. The bytecode reader must recognize the same dialect and operation tags. The pass manager must preserve the same invariants. The scheduler must obey the same resource and dependency model. The NVPTX lowering must emit the same param-space and memory-space conventions expected by `ptxas`.

## End-to-End Algorithm

The top-level compiler can be modeled as this pipeline:

```c
TileirasResult compile_tileiras(ByteBuffer input, TileirasConfig cfg) {
    validate_config(cfg);

    if (!is_tileiras_bytecode(input)) {
        if (looks_like_plain_mlir_bytecode(input))
            return error("failed to parse IR bytecode (it looks like MLIR bytecode instead)");
        return error("failed to parse IR bytecode");
    }

    MLIRContext ctx = create_context();
    register_tileiras_dialects(&ctx);

    Module module = parse_tileiras_bytecode(&ctx, input);
    verify_module_contract(module, cfg.gpu);

    PassManager pm = build_tileiras_pipeline(cfg);
    pm.run(module);

    LLVMModule llvm = lower_to_llvm_and_nvvm(module, cfg);
    PTXText ptx = emit_nvptx(llvm, cfg);
    Cubin sass = run_ptxas(ptx, cfg);

    Optional<Disassembly> disasm = none();
    if (cfg.requires_disassembly_payload)
        disasm = run_nvdisasm_c(sass, cfg);

    return assemble_host_object(sass, disasm, cfg.output_file);
}
```

The overview intentionally keeps this algorithm coarse. The detailed pages define the bytecode grammar, pass families, scheduler resource model, NVVM lowering, call-lowering ABI, and code-emission helpers at reimplementation depth.

## Position in CUDA 13.1

In CUDA 13.1, tileiras is best understood as a sibling device compiler to `cicc`, not as a child of it. Both paths eventually produce PTX and rely on the same downstream assembler, but they start from different frontends:

```text
CUDA C++ source path:
    CUDA C++ -> cudafe++ / cicc -> LLVM/NVVM -> PTX -> ptxas

Tile IR path:
    MLIR bytecode -> tileiras -> LLVM/NVVM -> PTX -> ptxas
```

That distinction matters for debugging. If tileiras rejects a program, the failure is normally in bytecode schema, dialect verification, tile lowering, scheduling, NVVM conversion, or PTX assembly. It is not a C++ frontend failure.

## Practical Reading Paths

For tool users and integrators, start with:

- [Driver Overview](driver/overview.md)
- [CLI Options](driver/cli-options.md)
- [Host Launch and ptxas Knobs](driver/host-launch-and-ptxas-knobs.md)
- [Subprocess Harness](driver/subprocess-harness.md)
- [Position in nvcc 13.1](boundaries/nvcc-13-1-position.md)

For bytecode producers, read:

- [MLIR Bytecode Format](bytecode/mlir-bc-format.md)
- [Dialect Reader/Writer Status](bytecode/dialect-readers-status.md)
- [cuda_tile Overview](dialects/cuda_tile/overview.md)
- [cuda_tile Op Roster](dialects/cuda_tile/op-roster.md)
- [TypeID Sentinel Table](reference/typeid-sentinel-table.md)

For reimplementation work, read in pipeline order:

- [Pipeline Overview](pipeline/overview.md)
- [cuda_tile](dialects/cuda_tile/overview.md), [nv_tileaa](dialects/nv_tileaa/overview.md), and [nv_tileas](dialects/nv_tileas/overview.md)
- [TileAS Pass Families](passes/tileas/scheduling-glue.md)
- [Scheduler Overview](scheduler/overview.md)
- [Lowering Overview](lowering/overview.md)
- [Codegen Overview](codegen/overview.md)
- [NVPTX Passes](nvptx-passes/overview.md)

Reference catalogs such as the function map, opcode rosters, and sentinel tables are intentionally denser. They are for lookup and audit work; the subsystem pages are the narrative documentation.

## Documentation Style

Public pages describe behavior first. Internal recovery anchors, binary offsets, and raw analysis notes are treated as authoring evidence, not as the reader-facing API. When a recovered implementation detail matters for compatibility, the page names the semantic role first and gives pseudocode or a data-structure contract before any low-level identifier.

Code blocks use C-like pseudocode for algorithms and explicit tables for externally visible contracts. The goal is that a reader can both operate tileiras and build a compatible implementation without having to reverse the prose back into an algorithm.
