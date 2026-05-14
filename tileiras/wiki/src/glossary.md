# Glossary

This glossary defines the public terms used throughout the tileiras wiki. It focuses on behavior, data models, dialects, passes, and target concepts. Detailed operation rosters live in their dialect pages.

## Core Tools

| Term | Meaning |
| --- | --- |
| `tileiras` | CUDA Tile IR optimizing assembler. It consumes TileIR MLIR bytecode and produces a host object containing compiled GPU code. |
| `ptxas` | NVIDIA assembler invoked after PTX emission to produce the final GPU binary payload. |
| `nvdisasm` | NVIDIA disassembler optionally invoked to produce annotated SASS output. |
| `cicc` | CUDA C++ device compiler. It shares the LLVM/NVPTX backend family with tileiras but starts from CUDA frontend output, not TileIR bytecode. |
| TileIR | NVIDIA's MLIR-based tile program representation consumed by tileiras. |

## Dialects

| Term | Meaning |
| --- | --- |
| `cuda_tile` | Public input dialect for tile programs. It describes tile arithmetic, memory, control flow, tokens, tensor views, and kernel entries. |
| `nv_tileaa` | Alias-aware internal dialect below `cuda_tile`. It introduces explicit memory references, pointer provenance, tokens, queues, and reuse markers. |
| `nv_tileas` | Operational async-scheduling dialect. It represents producer/consumer pipelines, TMA-ready memory operations, layout conversion, and scheduled regions. |
| `cute` | Target-neutral layout algebra dialect derived from CuTe concepts: shape, stride, layout, tile, coord, swizzle, and tiled atom descriptors. |
| `cute_nvgpu` | NVIDIA architecture atom dialect for MMA, WGMMA, TMA, TMEM, ldmatrix, stmatrix, and target-specific copy operations. |
| `cutlass` | CUTLASS pipeline dialect for tile schedulers, sequence barriers, pipeline roles, block-striped operations, and persistent kernel structure. |
| `nvgpu` | Stock MLIR NVIDIA GPU bridge dialect used before NVVM conversion. |
| `nvvm` | MLIR dialect representing NVVM/PTX-facing intrinsics and target operations before LLVM IR materialization. |
| `llvm` | MLIR LLVM dialect used as the last MLIR form before creating an LLVM module. |

## Tile and Layout Terms

| Term | Meaning |
| --- | --- |
| Tile | A logical block of tensor data operated on as a unit. |
| Shape | Extents of a tile, tensor view, or coordinate tuple. |
| Stride | Offset step associated with each coordinate dimension. |
| Layout | Mapping from logical coordinates to physical offsets, usually shape plus stride and optional swizzle. |
| Swizzle | Bit permutation used to match hardware layout requirements or avoid memory-bank conflicts. |
| Coord | Coordinate value used to index a shape or layout. |
| View | Pointer or memref plus shape, stride, element type, and memory-space metadata. |
| Tensor view | High-level view of a tensor region with shape and stride semantics. |
| Partition view | View that partitions a tensor or tile among program dimensions, lanes, warps, or agents. |
| Atom | A hardware-sized operation descriptor such as a copy atom, MMA atom, or TMA atom. |

## Scheduling Terms

| Term | Meaning |
| --- | --- |
| Stage | Logical software-pipeline stage assigned by the TileAS scheduler. |
| Order | Deterministic tie-breaker within a stage. Together with stage, it forms `(stage, order)`. |
| Initiation interval (`II`) | Number of cycles between starts of successive software-pipeline iterations. |
| RRT | Resource Reservation Table. A bitset table used to test whether an operation can occupy a candidate modulo cycle. |
| Resource footprint | Per-operation resource occupancy over one or more cycles. |
| `ScheduleAnalysis` | Preserved analysis carrying the fixed schedule from generation to materialization. |
| `Pipe_` | Concrete producer/consumer coordination value emitted after schedule placement. |
| `Mutex_` | Concrete mutual-exclusion coordination value emitted after schedule placement. |
| `Schedule::solve` | Materialization algorithm that groups producers and consumers into `Pipe_` values after placement is fixed. |

## Async Pipeline Terms

| Term | Meaning |
| --- | --- |
| Producer | Agent or region that fills a pipeline stage. |
| Consumer | Agent or region that waits for and reads a produced pipeline stage. |
| Pipeline stage | Rotating buffer slot shared by producer and consumer agents. |
| Producer acquire | Operation that grants producer ownership of a stage. |
| Producer commit | Operation that publishes a filled stage to consumers. |
| Consumer wait | Operation that waits for a committed stage. |
| Consumer release | Operation that returns a consumed stage to the pipeline. |
| Pipeline iterator | SSA value identifying the current rotating stage. |
| Agent switch | Operation that selects producer or consumer agent regions under warp specialization. |

## GPU Architecture Terms

| Term | Meaning |
| --- | --- |
| SM | Streaming multiprocessor generation or target family, such as `sm_90` or `sm_100`. |
| CTA | Cooperative thread array, often called a thread block. |
| Cluster | Group of CTAs that can use cluster-level synchronization and memory features. |
| TMA | Tensor Memory Accelerator, used for bulk tensor-memory transfers. |
| TMEM | Tensor memory used by Blackwell-era tensor-core operations. |
| WGMMA | Warpgroup matrix multiply-accumulate, introduced for Hopper-era tensor cores. |
| UMMA | Unified MMA family used by Blackwell tensor-memory operations. |
| `tcgen05` | Blackwell tensor-memory instruction family exposed through NVVM/NVPTX lowering. |
| `ldmatrix` | Instruction family that loads matrix fragments from shared memory into registers. |
| `stmatrix` | Store-side companion for matrix-fragment movement. |
| `cp.async` | Ampere asynchronous copy family. |
| `cp.async.bulk.tensor` | Hopper/Blackwell bulk tensor-memory copy family used by TMA. |

## Backend Terms

| Term | Meaning |
| --- | --- |
| NVVM intrinsic | LLVM intrinsic in the `llvm.nvvm.*` family. |
| LLVM module | LLVM IR representation produced after MLIR lowering. |
| MachineIR | LLVM target-specific machine representation after instruction selection. |
| Parameter space | PTX address space used for kernel parameters. |
| Address space | Memory-space classification such as generic, global, shared, constant, local, or parameter. |
| libdevice | NVIDIA device math bitcode library linked into modules that call `__nv_*` math functions. |
| `__nvvm_reflect` | Compile-time configuration query used by libdevice and NVVM support code. |
| Inline PTX | LLVM inline assembly carrying PTX text and operand constraints. |

## Common Options and Environment

| Term | Meaning |
| --- | --- |
| `--gpu-name` | Driver target GPU option. |
| `--host-arch` | Host architecture option used when producing the host object. |
| `--host-os` | Host operating-system option used when producing the host object. |
| `--opt-level` / `-O` | Optimization level controlling the pass pipeline. |
| `--lineinfo` | Requests line-number information when input debug information exists. |
| `--device-debug` / `-g` | Requests device debug information when input debug information exists. |
| `--sanitize` | Enables supported sanitizer mode. |
| `CUDA_ROOT`, `CUDA_HOME`, `CUDA_PATH` | Environment variables used to locate CUDA tools when needed. |

## Reading Notes

Operation names are written in backticks, for example `nv_tileas.async.pipeline.produce_one`. Dialect names are also written in backticks. Pseudocode uses C-like syntax but is descriptive rather than ABI-exact unless the page explicitly says otherwise.
