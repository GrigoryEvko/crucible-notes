# neuronx-gpsimd Internals — Custom-Op SDK

> **Status**: scaffolding · **Source packages**: `aws-neuronx-gpsimd-customop-lib_0.21.2.0` + `aws-neuronx-gpsimd-tools_0.21.0.0`

## What this wiki is

GPSIMD is AWS Neuron's branding for a **Tensilica Xtensa Q7-derived per-core compute engine** that lives inside each NeuronCore device alongside the main TPB sequencer. The "GPSIMD custom-op SDK" lets users compile C++ kernels into per-Xtensa-core ELF objects which are dispatched by NeuronCore work scheduling and called from PyTorch via ATen.

## Key surprise

Wave-2 N5 corrected wave-1's assumption: **GPSIMD is NOT a custom NVIDIA-style ISA**. The `xt-clang++` toolchain shipped in the .deb (`--xtensa-core=ncore2gp`, `-mcoproc`, `-mlongcalls`, linked against `libxmem`, `libhal`, `libc++-e`) plus the Tensilica copyright on LSP files (`Customer ID=15949; Build=0xa0ff9; Copyright (c) 2012 by Tensilica Inc.`) make this unambiguous — Q7 is a Tensilica Xtensa LX derivative.

## SDK build flow

```
User C++ kernel
   func(at::Tensor input, at::Tensor output) { ... }
          │
          │  build_custom_op.py
          │  - _create_wrapper() generates marshalling shim
          │  - _compile_sources() invokes xt-clang++ × 8 cores
          │  - _link() with per-CPU LSP spec files
          │  - _strip() outputs <name>_cpuN.stripped.so × 8
          ▼
8 Xtensa ELF objects (one per per-core, sized for that core's
   IRAM/DRAM, linked against ncore2gp LSP)
          │
          │  Compiled into NEFF by neuronx-cc as a custom op
          │  (referenced by InstCustomOp in BIR JSON)
          ▼
At runtime: NeuronCore TPB schedules custom-op kernel onto
   GPSIMD subcores via DMA + semaphore signalling
```

## Notable findings

- **8 cores per device**: `lsp_fll_load_cpu0` .. `lsp_fll_load_cpu7` link scripts plus `lsp_fll_load_cpu_single` for single-core ops. `NUM_CPUS = 8` in build script.
- **FlexLM licensed**: build script unconditionally sets `LM_LICENSE_FILE = /opt/aws/neuron/gpsimd/tools/licenses/amzn_vq7_us_582883.out`. Compilation **requires** both the binary and the license file.
- **32-char function name limit**: hard-erros above that, likely Xtensa symbol-table or PTBL constraint.
- **Split address space**: `neuron_hbm_allocate()` returns a 64-bit SoC address; `neuron_dataram_allocate()` returns a 32-bit local pointer; `neuron_translate()` maps between them via 16/64 MB windows.
- **"C10" is NOT PyTorch c10**: it's a sysroot bundle (zlib, bzip2, OpenSSL 1.1, libffi, sqlite3, custom libc10.a). PyTorch ATen integration enters via `neuron/wrapper_api.h`.
- **Custom-op marshalling**: `<fn>_wrapper()` calls `customop_setup(true)`, fetches args via `customop_next_tensor()`/`_next_int()`/etc., invokes user kernel, returns via `customop_return_tensor()`.
- **Optional stack switching**: `<fn>_stack_switch()` routes through `switch_stack_or_call_wrapper()` which can allocate an HBM stack up to 4 MB (`MAX_STACK_SIZE = 0x400000`) and `asm("j switchBack")` to return.

## Where to start

1. **[Custom-Op Programming Model](topics/programming-model.md)** — the user-facing C++ kernel API
2. **[Toolchain Inventory](topics/toolchain.md)** — `xt-clang++`, `xt-pkg-loadlib`, `xt-ld`, etc.
3. **[Build Pipeline Walkthrough](topics/build-pipeline.md)** — `compile() → _compile_sources() → _create_wrapper() → _link() → _strip()`
4. **[ATen ↔ Custom-Op Marshalling ABI](topics/aten-abi.md)** — `wrapper_api.h` interface
5. **[Memory Model](topics/memory-model.md)** — HBM vs DataRAM, translation windows

## Companion wikis

- [`neuronx-cc/wiki/`](../../neuronx-cc/wiki/) — InstCustomOp in BIR JSON; how the compiler embeds GPSIMD kernels into NEFFs
- [`neuronx-runtime/wiki/`](../../neuronx-runtime/wiki/) — the runtime that dispatches custom-op kernels onto GPSIMD cores
