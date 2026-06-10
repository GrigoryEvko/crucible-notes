# tileiras vs cudafe++ (Non-Relationship)

## Abstract

A common misconception about CUDA Toolkit 13.1 is that the new `tileiras` binary is a successor or replacement for `cudafe++`. It is not. The two tools share a parent driver (`nvcc`), a vendor (NVIDIA), and a problem domain (CUDA), but their inputs, outputs, internal architectures, and roles in the build graph have zero overlap. This page documents that non-relationship explicitly.

## What tileiras does NOT have

The cleanest way to state the boundary is to enumerate, point by point, each cudafe++ subsystem that is absent from tileiras. The absence is architectural, not just cosmetic: `tileiras` starts from serialized MLIR bytecode, so every source-language responsibility that belongs to `cudafe++` has already happened upstream or does not apply.

- **No EDG frontend.** cudafe++ is built around the Edison Design Group C++ Front End v6.6: lexer, parser, type system, template instantiation engine, overload resolver, and constexpr interpreter. tileiras has none of that machinery. Its bulk comes from the MLIR runtime, TileIR dialect libraries, and the LLVM 21 NVPTX backend.
- **No C++ parser.** tileiras has no recursive-descent C++ parser, token kind table, operator-precedence engine, or Itanium ABI name mangler. Its inbound surface is the MLIR bytecode reader, which decodes a serialized `builtin.module` whose ops, types, and attributes have already been resolved upstream. tileiras enters at bytecode, not source text.
- **No `.int.c` emission.** cudafe++ is a C++ source-to-source translator; one of its jobs is writing the transformed host-side `.int.c` output. tileiras emits no C source. Its terminal output is a host ELF object, with PTX as the intermediate textual artifact handed to `ptxas`.
- **No host stubs.** cudafe++ generates `__wrapper__device_stub_<kernel>()` host-side forwarding functions, the `.nvHRKI`/`.nvHRDE`/`.nvHRCE` ELF host-reference arrays, the `__cudaRegisterFatBinary` / `__cudaRegisterFunction` registration table, and the CRC32-derived module ID. tileiras is device-only. No kernel-launch lowering, no host-side stub synthesis, no fat-binary registration boilerplate.
- **No lambda machinery.** cudafe++ injects template wrappers (`__nv_dl_wrapper_t`, `__nv_hdl_wrapper_t`, `__nv_hdl_create_wrapper_t`) to carry extended `__device__` and `__host__ __device__` lambdas across the host/device boundary, driven by 1024-bit capture-count bitmasks. tileiras has no concept of a lambda or a capture. Whatever upstream tool produces the bytecode has already lowered any C++ lambda away by the time tileiras sees it.
- **No template instantiation.** cudafe++ runs a full C++ template instantiation worklist with deduction, partial specialization, SFINAE, and constexpr evaluation. tileiras has no template engine — no instantiation queue, no template parameter binding table, no constexpr tree-walker. Template specialization is a source-language concept that does not exist in MLIR bytecode.

## Why people might confuse them

The confusion is structural rather than semantic. Both binaries live in the same `bin/` directory of a CUDA Toolkit installation. Both are stripped, statically linked NVIDIA-internal ELF binaries. Both are invoked transparently by `nvcc`. Both bear the word "CUDA" in their public framing. Both deal with device-side work. None of those surface similarities reflect any internal overlap. The two tools operate at completely different levels of the pipeline — cudafe++ in the source-translation layer, tileiras at the device-IR-to-PTX layer — and never see each other's outputs.

## What cudafe++ actually does

cudafe++ is the CUDA C++ source-to-source translator. It accepts a `.cu` translation unit, runs the EDG 6.6 C++ frontend over it, separates device code from host code via execution-space attributes (`__device__`, `__host__`, `__global__`), and produces two outputs: an EDG IL stream consumed by `cicc`, and a transformed `.int.c` file consumed by the system C++ compiler (gcc, clang, or MSVC). cudafe++ is not a compiler in the conventional sense — it never emits PTX, never emits cubin, and never emits machine code. It is a frontend that splits a CUDA translation unit and hands the two tracks to different downstream tools.

## Redirect

This wiki documents tileiras only. For cudafe++ documentation — its EDG frontend internals, the 5-pass IL finalization, the 85-entry-kind IL graph, the `.int.c` emission format, the CUDA execution-space bitfield, lambda wrapper template injection, the 276-flag CLI surface, and the 3,795-entry diagnostic table — see the separate cudafe++ wiki at `crucible-notes/cudafe++/wiki/`.

## Boundary table

The four NVIDIA device-toolchain binaries, their inputs, outputs, and roles:

| tool | input | output | role |
|---|---|---|---|
| cudafe++ | `.cu` source (CUDA C++) | `.int.c` (transformed C/C++ host source) + EDG IL stream | C++ source-to-source translator; host/device split |
| cicc | `.cu` / `.i` / EDG IL | PTX text | CUDA-to-PTX compiler (EDG 6.6 + NVVM bridge + LLVM NVPTX backend) |
| tileiras | MLIR bytecode (`cuda_tile` dialect) | host ELF (`elf.o`) wrapping PTX (and optional SASS section) | MLIR-to-PTX optimizing assembler (53-pass MLIR pipeline + shared NVPTX backend) |
| ptxas | PTX text | SASS / cubin | PTX-to-SASS assembler |

cudafe++ is the gate at the source boundary; cicc is the conventional source-language compile path; tileiras is the optimizing-assembler path for tile-shaped kernels expressed in MLIR; ptxas is the final SASS encoder. tileiras and cudafe++ sit at opposite ends of this chain and never interact.

## Reimplementation Notes

Do not model `tileiras` as a cudafe++ mode. A clean driver should keep the responsibilities separate:

```text
cudafe++:
    input: CUDA C++ source
    work: split host and device code, lower launches, emit host-side transformed source
    output: host-side source plus device-side compiler input

tileiras:
    input: TileIR MLIR bytecode
    work: verify bytecode schema, run MLIR/NVVM/NVPTX lowering, invoke ptxas
    output: host ELF object carrying the generated device code
```

The only shared orchestration point is `nvcc`, which chooses which downstream compiler to run. The tools themselves should remain independent in any faithful reconstruction.
