# Glossary

This glossary defines the public terms used throughout the tileiras wiki. It focuses on behavior, data models, dialects, passes, and target concepts. Detailed operation rosters live in their dialect pages.

## Core Tools

| Term | Meaning |
| --- | --- |
| `tileiras` | CUDA TileIR optimizing assembler. It consumes TileIR MLIR bytecode and produces a host object containing compiled GPU code. See [Driver Overview](driver/overview.md). |
| `ptxas` | NVIDIA assembler invoked after PTX emission to produce the final GPU binary payload. See [ptxas Handoff Protocol](boundaries/ptxas-handoff-protocol.md). |
| `nvdisasm` | NVIDIA disassembler optionally invoked to produce annotated SASS output. |
| `cicc` | CUDA C++ device compiler. It shares the LLVM/NVPTX backend family with tileiras but starts from CUDA frontend output, not TileIR bytecode. See [cicc Comparison](boundaries/cicc-comparison.md). |
| TileIR | NVIDIA's MLIR-based tile program representation consumed by tileiras. The serialized bytecode form carries `builtin.module` containers whose `gpu.module` payloads are expressed in the `cuda_tile` dialect; passing through the full lowering cascade it becomes `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, `cutlass`, `nvgpu`, `nvvm`, and finally `llvm`. See [Pipeline Overview](pipeline/overview.md). |
| TileAS | The pass family and dialect-family name covering scheduling, layout, async pipeline, CTA cluster, and buffer-management work over `nv_tileas` IR. The CLI prefix and option names use the lowercase form (`tileas-*`); prose uses TileAS. See [TileAS Pass Families](passes/tileas/scheduling-glue.md). |

## Dialects

Each dialect occupies one layer of the lowering pipeline. The early dialects preserve tile semantics, the middle dialects make layout and scheduling explicit, and the late dialects bridge to NVVM and LLVM.

| Term | Meaning |
| --- | --- |
| `cuda_tile` | Public input dialect for tile programs. It describes tile arithmetic, memory, control flow, tokens, tensor views, and kernel entries. The dialect is the only public surface — the rest of the cascade is NVIDIA-private. See [cuda_tile Overview](dialects/cuda_tile/overview.md). |
| `nv_tileaa` | Alias-aware internal dialect below `cuda_tile`. It introduces explicit memory references, pointer provenance, tokens, queues, and reuse markers so later passes can reason about aliasing without re-deriving it. See [nv_tileaa Overview](dialects/nv_tileaa/overview.md). |
| `nv_tileas` | Operational async-scheduling dialect. It represents producer/consumer pipelines, TMA-ready memory operations, layout conversion, and scheduled regions. The TileAS pass family runs on this dialect. See [nv_tileas Overview](dialects/nv_tileas/overview.md). |
| `cute` | Target-neutral layout algebra dialect derived from CuTe concepts: shape, stride, layout, tile, coord, swizzle, and tiled atom descriptors. Used to express layout transformations and tile partitioning. See [cute Overview](dialects/cute/overview.md). |
| `cute_nvgpu` | NVIDIA architecture atom dialect for MMA, WGMMA, TMA, TMEM, ldmatrix, stmatrix, and target-specific copy operations. Each atom is parameterised by SM tier (SM70..SM120). See [cute_nvgpu Overview](dialects/cute_nvgpu/overview.md). |
| `cutlass` | CUTLASS pipeline dialect for tile schedulers, sequence barriers, pipeline roles, block-striped operations, and persistent kernel structure. Models the CUTLASS programming-model abstractions as MLIR ops. See [cutlass Overview](dialects/cutlass/overview.md). |
| `nvgpu` | Stock MLIR NVIDIA GPU bridge dialect used before NVVM conversion. Acts as an intermediate between high-level GPU intent and concrete NVVM intrinsics. See [nvgpu Overview](dialects/nvgpu/overview.md). |
| `nvvm` | MLIR dialect representing NVVM/PTX-facing intrinsics and target operations before LLVM IR materialization. See [NVVM Overview](dialects/nvvm/overview.md). |
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
| Stage | Logical software-pipeline stage assigned by the TileAS scheduler. Operations in stage `k` start `k` iterations of the prologue ahead of the steady-state. |
| Order | Deterministic tie-breaker within a stage. Together with stage, it forms the `(stage, order)` pair downstream materialization consumes. |
| Initiation interval (`II`) | Number of cycles between starts of successive software-pipeline iterations. The minimum II respects both data-dependence and resource constraints. |
| RRT | Resource Reservation Table. A bitset table with one row per cycle modulo the candidate II, where each row is a bitset of resource classes. Used to test whether an operation can occupy a candidate modulo cycle. See [Resource Constraint Builder and RRT](scheduler/resource-constraint-builder-and-rrt.md). |
| Resource footprint | Per-operation resource occupancy over one or more cycles. The scheduler reads it before probing an RRT slot. |
| `ScheduleAnalysis` | Preserved MLIR analysis carrying the fixed schedule from `TileASGenerateSchedule` to `TileASMaterializeSchedule`. The two-pass split is what lets the scheduler decide once and the materializer apply once. |
| MaterializeSchedule | The TileAS pass that consumes the cached `ScheduleAnalysis` and emits `Pipe_` / `Mutex_` SSA values along with the `cute_nvgpu.arch.agent_switch` partitioning at warp-specialized boundaries. See [Async/Pipeline Family](passes/tileas/async-pipeline-family.md). |
| `Pipe_` | Concrete producer/consumer coordination value emitted after schedule placement. Models a depth-`d` ring buffer with bounded slack between producer and consumer stages. See [Pipe and Mutex Value Layout](scheduler/pipe-mutex-value-layout.md). |
| `Mutex_` | Concrete mutual-exclusion coordination value emitted after schedule placement. Models a zero-slack serialization edge — iteration `i` of the protected region must complete before iteration `i+1` starts. |
| `Schedule::solve` | Materialization algorithm that groups producers and consumers into `Pipe_` values after placement is fixed. See [Schedule::solve and Cost Evaluators](scheduler/schedule-solve-and-cost-evaluators.md). |
| VLIW | Very Long Instruction Word. Used in the scheduler context to describe how multiple operations get bundled into a single issue slot — the modulo scheduler emits VLIW-style packed schedules when the target pipeline has multiple parallel function units. |

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
| Agent switch | Operation that selects producer or consumer agent regions under warp specialization. The `nv_tileas.async.pipeline.agent_switch` op is the IR-visible form. |
| AWS | Agent-Warp-Specialized. The dispatch mode `MaterializeSchedule` selects when distinct producer and consumer agents are partitioned across warps; the alternative AUS (Agent-Unspecialized) has a single SIMT agent owning both. The `nv_tile.aws.*` attribute family threads scheduling keys back into the AsyncValue headers. |

## GPU Architecture Terms

| Term | Meaning |
| --- | --- |
| SM | Streaming multiprocessor generation or target family, such as `sm_90` or `sm_100`. Tileiras targets the Blackwell family: `sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121`. |
| CTA | Cooperative thread array, often called a thread block. |
| Cluster | Group of CTAs that can use cluster-level synchronization and memory features. Hopper introduced 2-CTA clusters; Blackwell extends to 4-CTA. |
| DSMEM | Distributed shared memory. The cross-CTA shared-memory mechanism in clusters: each cluster member can address shared memory of every peer through `nvvm.mapa` plus `llvm.addrspacecast`. The DSMEM handshake protocol pairs `nvvm.cluster.arrive` and `nvvm.cluster.wait` with optional fences. See [DSMEM Handshake and Cluster Barrier](topics/dsmem-handshake-and-cluster-barrier.md). |
| TMA | Tensor Memory Accelerator, used for bulk tensor-memory transfers. Hopper and Blackwell support TMA via the `cp.async.bulk.tensor` family driven by 128-byte descriptors. |
| TMEM | Tensor memory used by Blackwell-era tensor-core operations. A separate memory space (`addrspace 4`) with its own access primitives. |
| S2T copy | Shared-to-tensor-memory copy. Blackwell-specific transfer from SMEM to TMEM, used to stage `tcgen05.mma` operands. The `cute_nvgpu.atom.copy_make_s2t_copy_op` family models it. |
| WGMMA | Warpgroup matrix multiply-accumulate, introduced for Hopper-era tensor cores. Issued by a four-warp warp group cooperatively against an SMEM-resident B descriptor and a register or SMEM A descriptor. See [WGMMA Emission Protocol](topics/wgmma-emission-protocol.md). |
| UMMA | Unified MMA family used by Blackwell tensor-memory operations. Issued through `tcgen05.mma` with accumulator and operands in TMEM. |
| IMMA | Integer matrix multiply-accumulate. The PTX instruction family for integer MMA tiles; appears in mixed-precision MMA paths alongside the floating-point families. |
| GMMA descriptor | Synonym for SMEM descriptor in the WGMMA context. The 64-bit shared-memory descriptor that encodes the SMEM base address (low 14 bits, in 16-byte units) plus leading and stride byte offsets pinning the 2D tile shape into shared memory. WGMMA operand B is always an SMEM descriptor; operand A is either a register fragment or an SMEM descriptor. |
| SMEM descriptor | See GMMA descriptor. |
| f8E8M0FNU | 8-bit floating-point variant used as the scale-factor type in block-scaled MMA. Encodes a pure exponent (no mantissa, no sign), giving microscale factors a wide dynamic range from a single byte. |
| Microscale | Block-scaled MMA where each tile of operand data carries a small shared scale factor (typically f8E8M0FNU). Allows narrow operand types (FP4 and FP8 mantissa) to express a wide effective dynamic range. |
| `collector::a` | The `tcgen05.mma` accumulator-mode parameter selecting how the accumulator participates: `use` reads and writes, `fill` writes only (zero-init equivalent), `discard` writes only with no read dependency. The kind-word verifier at `sub_1AD26A0` packs this into the same bitfield as `cta_group`. |
| `tcgen05` | Blackwell tensor-memory instruction family exposed through NVVM/NVPTX lowering. Covers `tcgen05.mma`, `tcgen05.cp`, `tcgen05.commit`, and the synchronizing primitives. See [tcgen05 Tensor Memory Model](topics/tcgen05-tensor-memory-model.md). |
| `ldmatrix` | Instruction family that loads matrix fragments from shared memory into registers. The companion to `cp.async`/`cp.async.bulk` for SMEM-to-RF staging. |
| `stmatrix` | Store-side companion for matrix-fragment movement. |
| `cp.async` | Ampere asynchronous copy family. |
| `cp.async.bulk.tensor` | Hopper/Blackwell bulk tensor-memory copy family used by TMA. |
| mbarrier | Memory barrier object held in shared memory. Used by TMA, async copy, and the producer/consumer handshake to coordinate work across warps. See [mbarrier State Machine](topics/mbarrier-state-machine.md). |

## Backend Terms

| Term | Meaning |
| --- | --- |
| NVVM intrinsic | LLVM intrinsic in the `llvm.nvvm.*` family. |
| LLVM module | LLVM IR representation produced after MLIR lowering. |
| MachineIR | LLVM target-specific machine representation after instruction selection. |
| Parameter space | PTX address space used for kernel parameters. |
| Address space | Memory-space classification such as generic, global, shared, constant, local, or parameter. |
| libdevice | NVIDIA device math bitcode library linked into modules that call `__nv_*` math functions. See [libdevice Overview](libdevice/overview.md). |
| `__nvvm_reflect` | Compile-time configuration query used by libdevice and NVVM support code. The reflect pass replaces `__nvvm_reflect("name")` calls with the resolved integer value at compile time. See [NVVMReflect Mechanism](libdevice/nvvm-reflect-mechanism.md). |
| `__grid_constant__` | Kernel-parameter attribute indicating a value that is constant per grid launch. The TMA descriptor pass uses it to mark TMA descriptors passed by kernel parameter, so codegen can place the descriptor into a read-only constant slot without proving constancy from scratch. |
| Inline PTX | LLVM inline assembly carrying PTX text and operand constraints. |
| Descriptor | Generic name for a structured operand passed to a hardware primitive. Each architecture family has its own descriptor type: TMA descriptors are 128-byte records for `cp.async.bulk.tensor`; GMMA/SMEM descriptors are 64-bit records for WGMMA. |
| Intrinsic | A function-like name that lowers to one or a few target instructions rather than a regular call. PTX intrinsics surface in MLIR as `nvvm.*` ops. |
| Pass | An MLIR transformation that runs on an operation kind (`builtin.module`, `gpu.module`, `nv_tileaa.func`, etc.). Tileiras runs about fifty passes per device module at `-O3`. See [Full Pass List by Opt Level](pipeline/full-pass-list-by-opt-level.md). |
| Dialect | An MLIR namespace owning a set of operations, types, attributes, and interfaces. Tileiras registers nine dialects across the lowering cascade plus upstream MLIR dialects (`arith`, `math`, `scf`, `builtin`, etc.). |
| NCL | NVPTX Common Library — the family of `nv-*` and `nvptx-*` helper passes that perform common-base elimination, dead-sync elimination, kernel attribute stamping, and other NVPTX-specific cleanups in the backend. |

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
