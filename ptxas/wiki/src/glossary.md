# Glossary

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Quick-reference for terms used throughout this wiki. Each entry links to the primary page with full details.

| Term | Definition |
|---|---|
| **Barrier** | Hardware synchronization primitive that blocks threads until a condition is met. PTXAS inserts and optimizes barriers via dedicated passes. See [Synchronization & Barriers](./passes/sync-barriers.md). |
| **BMMA** | Binary Matrix Multiply-Accumulate — tensor core operation on 1-bit inputs. Part of the WMMA/GMMA family. See [Tensor Core Intrinsics](./intrinsics/tensor.md). |
| **BSSY** | Barrier Set Synchronization — SASS instruction that sets a convergence barrier for divergent control flow. Paired with BSYNC. See [Scoreboards & Dependency Barriers](./scheduling/scoreboards.md). |
| **BSYNC** | Barrier Synchronize — SASS instruction that waits on a convergence barrier set by BSSY. See [Scoreboards & Dependency Barriers](./scheduling/scoreboards.md). |
| **Capmerc** | Capsule Mercury — an ELF section (`.nv.capmerc`) embedding a secondary Mercury-encoded representation of the kernel for debug metadata and binary patching. See [Capsule Mercury & Finalization](./codegen/capmerc.md). |
| **CGA** | Cooperative Grid Array — Hopper+ hardware grouping of thread blocks that can synchronize and share distributed shared memory. See [Ada & Hopper](./targets/ada-hopper.md). |
| **Convergence** | The point where divergent warp threads rejoin a common execution path, marked by BSSY/BSYNC pairs in SASS. See [Predication](./passes/predication.md). |
| **Cubin** | CUDA Binary — the ELF-based output format produced by ptxas, containing `.text` (SASS), `.nv.info`, `.nv.constant0`, and other NVIDIA-specific sections. See [ELF/Cubin Output](./pipeline/output.md). |
| **DAG** | Directed Acyclic Graph — the core data structure within Ori IR basic blocks; instructions form a DAG of def-use edges rather than a flat list. See [IR Overview & Design](./ir/overview.md). |
| **DEPBAR** | Dependency Barrier — SASS instruction (`DEPBAR`) that stalls until a scoreboard counter reaches a threshold, enforcing producer-consumer ordering. See [Scoreboards & Dependency Barriers](./scheduling/scoreboards.md). |
| **Divergence** | When threads within a warp take different control-flow paths, requiring the hardware to serialize execution. PTXAS manages divergence through predication and BSSY/BSYNC insertion. See [Predication](./passes/predication.md). |
| **DMMA** | Double-precision Matrix Multiply-Accumulate — FP64 tensor core operation available on sm_80+. See [Tensor Core Intrinsics](./intrinsics/tensor.md). |
| **DynBatch** | Dynamic Batch — one of the instruction scheduler's two modes (alongside ReduceReg), which batches independent instructions to maximize ILP. See [Scheduler Architecture](./scheduling/overview.md). |
| **EIATTR** | Extended Info Attributes — per-kernel metadata in `.nv.info` sections: tag-length-value records carrying register counts, barrier usage, shared memory sizes, and other properties consumed by the CUDA runtime and driver. See [EIATTR Attribute Catalog](./reference/eiattr.md). |
| **ELFW** | PTXAS's custom ELF writer (`sub_1C9F280`, 97 KB) — a bespoke emitter that builds CUBIN files with NVIDIA-specific sections, relocations, and symbol conventions. See [Custom ELF Emitter](./output/elf-emitter.md). |
| **Fatpoint** | The register allocation algorithm used by ptxas. A fatpoint is a program point annotated with the set of simultaneously live virtual registers; the allocator maps these sets to physical registers. See [Fatpoint Algorithm](./regalloc/algorithm.md). |
| **HMMA** | Half-precision Matrix Multiply-Accumulate — FP16 tensor core operation, the original WMMA instruction class from Volta/Turing. See [Tensor Core Intrinsics](./intrinsics/tensor.md). |
| **IMMA** | Integer Matrix Multiply-Accumulate — INT8/INT4 tensor core operation. See [Tensor Core Intrinsics](./intrinsics/tensor.md). |
| **Knob** | An internal tuning parameter (1,294 total) stored as a ROT13-obfuscated string in the binary, read from environment variables or INI-format knob files. Controls per-pass thresholds, feature toggles, and scheduler behavior. See [Knobs System](./config/knobs.md). |
| **MEMBAR** | Memory Barrier — SASS instruction that enforces memory ordering across threads, CTAs, or the GPU. See [Synchronization & Barriers](./passes/sync-barriers.md). |
| **MercConverter** | The subsystem that converts abstract Ori IR instructions into Mercury-compatible instruction objects for SASS encoding. Part of instruction selection. See [Instruction Selection](./codegen/isel.md). |
| **Mercury** | The SASS binary encoder subsystem. Converts abstract instruction objects into 128-bit packed machine words via ~4,000 per-variant handler functions. See [Mercury Encoder](./codegen/mercury.md). |
| **MovPhi** | A pseudo-instruction in the Ori IR that represents SSA phi-node moves — parallel copies resolved during register allocation and out-of-SSA conversion. See [IR Overview & Design](./ir/overview.md). |
| **NvOptRecipe** | NVIDIA Optimization Recipe — a predefined sequence of optimization phases selected by optimization level. The PhaseManager reads the recipe to determine which phases run and in what order. See [Optimization Levels](./config/opt-levels.md). |
| **Occupancy** | The ratio of active warps to the maximum warps a streaming multiprocessor can support, determined by register count, shared memory usage, and barrier count. Higher occupancy helps hide memory latency. See [Allocator Architecture](./regalloc/overview.md). |
| **OCG** | Optimizing Code Generator — NVIDIA's internal name for the ptxas optimization and codegen pipeline (the 159-phase core). Appears in knob prefixes and timing strings. See [Optimization Pipeline](./pipeline/optimizer.md). |
| **Opex** | Operand Expansion — a late pipeline stage that expands abstract operands into concrete SASS encoding fields (virtual registers, immediates, address modes to bit patterns). See [SASS Code Generation](./pipeline/codegen.md). |
| **Ori IR** | PTXAS's internal intermediate representation — basic blocks containing an instruction DAG with typed virtual registers. Named after recovered debug strings; not an acronym. See [IR Overview & Design](./ir/overview.md). |
| **PhaseManager** | The infrastructure class (`sub_C62720`) that drives the 159-phase optimization pipeline: a factory, vtable dispatch, execute/isNoOp/getName interface. See [Phase Manager Infrastructure](./passes/phase-manager.md). |
| **Pipeline progress counter** | A hardware counter (Hopper+) that tracks the stage of an asynchronous pipeline operation, used by `cp.async` and TMA to overlap compute with memory transfers. See [Ada & Hopper](./targets/ada-hopper.md). |
| **PTX** | Parallel Thread Execution — NVIDIA's virtual ISA for GPU compute. The textual input format consumed by ptxas. See [PTX Instruction Table](./reference/ptx-instructions.md). |
| **QMMA** | Quarter-precision Matrix Multiply-Accumulate — FP8 (E4M3/E5M2) tensor core operation available on Hopper+. See [Tensor Core Intrinsics](./intrinsics/tensor.md). |
| **Register pressure** | The number of live virtual registers at a program point relative to the physical register file capacity. High pressure causes spilling. See [Allocator Architecture](./regalloc/overview.md). |
| **Remat** | Rematerialization — recomputing a value instead of spilling and reloading it, trading ALU cycles for register file pressure reduction. See [Rematerialization](./passes/rematerialization.md). |
| **ROT13** | The trivial Caesar cipher (rotate-13) used to obfuscate all 1,294 knob name strings in the ptxas binary. Decoded at lookup time by `GetKnobIndex`. See [Knobs System](./config/knobs.md). |
| **SASS** | Shader Assembly — NVIDIA's native GPU machine code. The binary output produced by ptxas, encoded as 128-bit instruction words. See [SASS Opcode Catalog](./reference/sass-opcodes.md). |
| **Scoreboard** | A hardware dependency-tracking mechanism (6 barriers on pre-Hopper, more on Hopper+) that enforces ordering between long-latency producers and their consumers. Managed by DEPBAR instructions. See [Scoreboards & Dependency Barriers](./scheduling/scoreboards.md). |
| **sm\_backend** | The per-architecture codegen backend selected by `--gpu-name`. Each SM family (Turing, Ampere, Ada, Hopper, Blackwell) has distinct encoding tables, latency profiles, and feature gates. See [SM Architecture Map](./targets/index.md). |
| **Spill** | Storing a live register value to local memory when the allocator cannot fit all live values into the physical register file. Spills degrade performance significantly on GPUs. See [Spilling](./regalloc/spilling.md). |
| **tcgen05** | Fifth-generation tensor core instruction set on Blackwell (sm_100+). Replaces WMMA/GMMA with a new ISA for matrix operations. See [TCGen05](./targets/tcgen05.md). |
| **TMA** | Tensor Memory Accelerator — Hopper+ hardware unit that performs bulk asynchronous copies between global and shared memory with address generation offloaded from the SM. See [Ada & Hopper](./targets/ada-hopper.md). |
| **UFT** | Uniform Function Table — a data structure in the CUBIN that maps function indices to code offsets, used by the driver for indirect call dispatch. See [ELF/Cubin Output](./pipeline/output.md). |
| **UDT** | Uniform Data Table — a companion to UFT that maps data indices to constant bank offsets within the CUBIN. See [ELF/Cubin Output](./pipeline/output.md). |
| **Warpgroup** | A Hopper+ scheduling unit consisting of 4 warps (128 threads) that execute WGMMA and other warpgroup-level instructions collectively. See [Ada & Hopper](./targets/ada-hopper.md). |
| **WGMMA** | Warpgroup Matrix Multiply-Accumulate — Hopper+ tensor core instruction that operates at warpgroup granularity (4 warps), supporting asynchronous execution with pipeline progress counters. See [GMMA/WGMMA Pipeline](./passes/gmma-pipeline.md). |
