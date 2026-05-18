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
| SM (Streaming Multiprocessor) | The basic GPU compute unit. Each SM owns a register file, a shared-memory bank, warp schedulers, and one or more tensor-core pipelines. Targets are named by SM tier: tileiras emits for the Blackwell family (`sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121`). See [GPU Execution Model](topics/gpu-execution-model.md). |
| CTA (Cooperative Thread Array) | The PTX-level name for a thread block. A CTA contains 1 to 1024 threads grouped into warps; threads in the same CTA share an SMEM allocation and can synchronize through CTA-local barriers. See [GPU Execution Model](topics/gpu-execution-model.md). |
| Warp | 32 threads executing in SIMT lockstep on the same SM. The warp is the unit of instruction issue, divergence, and most synchronization primitives. See [GPU Execution Model](topics/gpu-execution-model.md). |
| Warp-group | Four contiguous warps, 128 threads. The unit of cooperation for WGMMA on Hopper and for several Blackwell tensor-memory operations. See [WGMMA Emission Protocol](topics/wgmma-emission-protocol.md). |
| Cluster | A SM90-introduced grouping of 1-8 CTAs that share distributed shared memory and can use cluster-scope barriers. Hopper introduced 2-CTA clusters; Blackwell extends to 4-CTA. See [Cluster Sync and DSMEM Handshake](topics/cluster-sync-and-dsmem-handshake.md). |
| Grid | The whole kernel launch — a 1D/2D/3D array of CTAs scheduled together by the driver. |
| Register file | The per-SM bank of 32-bit registers, partitioned among resident warps. Tileiras's register-pressure heuristics and the modulo scheduler both reason about this resource. |
| SMEM (Shared Memory) | Per-CTA on-chip memory. Around 228 KB usable per SM on H100-class parts; bandwidth on the order of tens of TB/s. Used for tiles, mbarriers, and TMA staging. |
| GMEM (Global Memory) | Device-wide off-chip DRAM. Tens to hundreds of GB on data-center parts. Accessed through `ld.global`, `cp.async`, or TMA. |
| DSMEM (Distributed Shared Memory) | Cross-CTA shared memory inside a cluster: each cluster member can address shared memory of every peer through `nvvm.mapa` plus `llvm.addrspacecast`. The handshake pairs `nvvm.cluster.arrive` and `nvvm.cluster.wait` with optional fences. See [Cluster Sync and DSMEM Handshake](topics/cluster-sync-and-dsmem-handshake.md). |
| TMEM (Tensor Memory) | SM100+ on-chip memory used as the operand and accumulator store for `tcgen05.mma`. A separate address space (`addrspace 4`) with its own load/store and copy primitives. See [tcgen05 Tensor Memory Model](topics/tcgen05-tensor-memory-model.md). |
| TMA (Tensor Memory Accelerator) | SM90+ async bulk tensor-copy engine. Driven by 128-byte tensormap descriptors and the `cp.async.bulk.tensor` family. See [TMA TensorMap and `cp.async.bulk`](codegen/tma-tensormap-and-cp-async-bulk.md). |
| S2T copy | Shared-to-tensor-memory copy. Blackwell-specific transfer from SMEM to TMEM, used to stage `tcgen05.mma` operands. The `cute_nvgpu.atom.copy_make_s2t_copy_op` family models it. |
| WGMMA | Warp-group matrix multiply-accumulate, introduced for Hopper tensor cores. Issued by a 128-thread warp group cooperatively against an SMEM-resident B descriptor and a register or SMEM A descriptor. See [WGMMA Emission Protocol](topics/wgmma-emission-protocol.md). |
| UMMA | Unified MMA family used by Blackwell tensor-memory operations. Issued through `tcgen05.mma` with accumulator and operands in TMEM. |
| IMMA | Integer matrix multiply-accumulate. The PTX instruction family for integer MMA tiles; appears in mixed-precision MMA paths alongside the floating-point families. |
| GMMA descriptor | Synonym for SMEM descriptor in the WGMMA context. The 64-bit shared-memory descriptor that encodes the SMEM base address (low 14 bits, in 16-byte units) plus leading and stride byte offsets pinning the 2D tile shape into shared memory. WGMMA operand B is always an SMEM descriptor; operand A is either a register fragment or an SMEM descriptor. |
| SMEM descriptor | See GMMA descriptor. |
| f8E8M0FNU | 8-bit floating-point variant used as the scale-factor type in block-scaled MMA. Encodes a pure exponent (no mantissa, no sign), giving microscale factors a wide dynamic range from a single byte. See also `e8m0` under [Math and Precision](#math-and-precision). |
| Microscale | Block-scaled MMA where each tile of operand data carries a small shared scale factor (typically f8E8M0FNU). Allows narrow operand types (FP4 and FP8 mantissa) to express a wide effective dynamic range. See [Fast-Math and Numerical Precision](topics/fast-math-and-numerical-precision.md). |
| `collector::a` | The `tcgen05.mma` accumulator-mode parameter selecting how the accumulator participates: `use` reads and writes, `fill` writes only (zero-init equivalent), `discard` writes only with no read dependency. The kind-word verifier at `sub_1AD26A0` packs this into the same bitfield as `cta_group`. |
| `tcgen05` | Blackwell tensor-memory instruction family exposed through NVVM/NVPTX lowering. Covers `tcgen05.mma`, `tcgen05.cp`, `tcgen05.commit`, and the synchronizing primitives. See [tcgen05 Tensor Memory Model](topics/tcgen05-tensor-memory-model.md). |
| `mma.sync` | Warp-cooperative matrix multiply-accumulate on SM70 through SM89. Operands and accumulator live in registers; the whole warp issues the operation together. Superseded by WGMMA on Hopper and `tcgen05.mma` on Blackwell, but still emitted for older targets. |
| `ldmatrix` | Synchronous instruction family that loads matrix fragments from shared memory into per-thread registers shaped for `mma.sync`/WGMMA consumption. The SMEM-to-RF companion to `cp.async`/`cp.async.bulk`. |
| `stmatrix` | Synchronous matrix-fragment store from registers back to shared memory. The store-side counterpart to `ldmatrix`. |
| `cp.async` | Ampere (SM80+) asynchronous global-to-shared copy family. Decouples the load issue from the data-ready point through commit-and-wait groups. |
| `cp.async.bulk` | SM90+ bulk async copy family covering both tensor and non-tensor variants. The tensor variant is the TMA path; the non-tensor variant carries plain byte ranges. |
| `cp.async.bulk.tensor` | Hopper/Blackwell bulk tensor-memory copy family used by TMA, driven by tensormap descriptors. |
| mbarrier | Transactional barrier object held in shared memory. Used by TMA, async copy, and the producer/consumer handshake to coordinate arrivals and byte-count transactions across warps. See [mbarrier State Machine](topics/mbarrier-state-machine.md). |
| NamedBarrier (`bar.sync N`) | The CTA-local barrier pool indexed by a small integer (0-15). Distinct from mbarriers: `bar.sync` is a hardware-implemented synchronous barrier with no transactional state, used for sub-CTA synchronization at warp-specialized boundaries. |

## PTX and SASS

| Term | Meaning |
| --- | --- |
| PTX | NVIDIA's virtual ISA and target-independent intermediate representation. Tileiras emits PTX text that ptxas then translates to a concrete SM's SASS. See [ptxas Handoff Protocol](boundaries/ptxas-handoff-protocol.md). |
| SASS | NVIDIA's hardware ISA, generated by ptxas from PTX and specific to one SM tier. Tileiras itself does not emit SASS; it relies on ptxas for instruction selection at that level. See [PTX Version and Target Selection](topics/ptx-version-and-target-selection.md). |
| State space | PTX's address-space designation on a load/store or pointer: `global`, `shared`, `local`, `constant`, `param`, or the unspecified `generic`. State spaces map to MLIR memory spaces and to LLVM address spaces in the NVVM target. See [AddrSpace Vote Lattice](topics/addrspace-vote-lattice.md). |
| Inline PTX | LLVM inline assembly carrying PTX text and operand constraints. Tileiras emits inline PTX for primitives the NVVM intrinsics layer does not cover directly. |

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
| Descriptor | Generic name for a structured operand passed to a hardware primitive. Each architecture family has its own descriptor type: TMA descriptors are 128-byte records for `cp.async.bulk.tensor`; GMMA/SMEM descriptors are 64-bit records for WGMMA. |
| Intrinsic | A function-like name that lowers to one or a few target instructions rather than a regular call. PTX intrinsics surface in MLIR as `nvvm.*` ops. |
| Pass | An MLIR transformation that runs on an operation kind (`builtin.module`, `gpu.module`, `nv_tileaa.func`, etc.). Tileiras runs about fifty passes per device module at `-O3`. See [Full Pass List by Opt Level](pipeline/full-pass-list-by-opt-level.md). |
| Dialect | An MLIR namespace owning a set of operations, types, attributes, and interfaces. Tileiras registers nine dialects across the lowering cascade plus upstream MLIR dialects (`arith`, `math`, `scf`, `builtin`, etc.). |
| NCL | NVPTX Common Library — the family of `nv-*` and `nvptx-*` helper passes that perform common-base elimination, dead-sync elimination, kernel attribute stamping, and other NVPTX-specific cleanups in the backend. |

## MLIR Infrastructure

| Term | Meaning |
| --- | --- |
| MLIR (Multi-Level IR) | The LLVM-project IR-of-IRs framework that hosts tileiras's whole lowering cascade. Dialects, operations, types, attributes, and passes are all MLIR concepts. See [Architecture Evolution and Design Decisions](topics/architecture-evolution-and-design-decisions.md). |
| Operation | An instruction-level IR node in MLIR. Carries operands, results, attributes, regions, a source location, and an `OperationName`. The whole MLIR program is a tree of operations. See [Operation Layout](mlir-infra/operation-layout.md). |
| Attribute | Compile-time-known data attached to an operation: integers, strings, types, dictionaries, dialect-defined records, etc. Attributes are uniqued in the `MLIRContext`. See [Attribute System and Lowering](topics/attribute-system-and-lowering.md). |
| Type | An MLIR value's type. Types are uniqued through the `StorageUniquer` per context and carry a `TypeID` plus optional dialect-defined storage. See [Storage Uniquer and ContextImpl](mlir-infra/storage-uniquer-and-context-impl.md). |
| Region | A container of basic blocks living inside an operation. Functions, loops, branches, and structured constructs each own one or more regions. |
| OperationName | The per-op-kind runtime identity that every concrete operation refers to. Holds the dialect pointer, the operation's TypeID, its interface table, and folding/verification hooks. See [Operation Layout](mlir-infra/operation-layout.md). |
| TypeID | Per-class runtime identity assigned by MLIR's `TypeID` machinery. Used to key attribute storage, type storage, interface dispatch, and pass IDs. RTTI is disabled in LLVM/MLIR, so `TypeID` plays the role that `typeid` would in standard C++. See [TypeID Sentinels and Anchors](mlir-infra/typeid-sentinels-and-anchors.md). |
| TableGen | LLVM's declarative DSL (extension `.td`) for describing instructions, registers, intrinsics, and other compiler tables. A backend reads the `.td` files and emits C++ headers and tables at build time. |
| ODS (Operation Definition Specification) | The MLIR-specific use of TableGen. Each dialect's operations, types, attributes, and interfaces are declared in `.td` files; `mlir-tblgen` emits the C++ classes and definitions consumed by the dialect implementation. |

## Math and Precision

| Term | Meaning |
| --- | --- |
| FP32 / f32 | IEEE 754 single-precision binary32. 1 sign + 8 exponent + 23 mantissa bits. The reference precision for tile arithmetic that is not explicitly narrowed. |
| FP16 / f16 / half | IEEE 754 half-precision binary16. 1 + 5 + 10 bits. Common as MMA operand and accumulator on pre-Hopper tensor cores. |
| BF16 / bf16 | brain-float-16. 1 + 8 + 7 bits. Same exponent range as FP32 but only 7-bit mantissa; the standard low-precision training format on Hopper and Blackwell tensor cores. |
| FP8 (e4m3, e5m2) | 8-bit floating-point types from the OFP8 family. `e4m3` has 4 exponent + 3 mantissa bits (used for forward activations and weights), `e5m2` has 5 + 2 (wider range, used for gradients). MMA operand type on SM89+. |
| FP4 (e2m1) | 4-bit floating-point type with 2 exponent + 1 mantissa bit. Used as MMA operand in Blackwell block-scaled MMA. |
| e8m0 | 8-bit exponent, 0-bit mantissa, no sign. Used as the per-block scale factor in MX-FP block formats. In MLIR this is `f8E8M0FNU`. |
| Block-scaled FP | An MX-FP-style format: a block of N narrow values (FP4 or FP8 mantissa) plus a shared `e8m0` scale factor. Lets narrow operands cover a wide effective dynamic range. See [Fast-Math and Numerical Precision](topics/fast-math-and-numerical-precision.md). |
| FTZ (Flush to Zero) | Hardware option that flushes subnormal inputs and results to signed zero. Controlled per-module through NVVM-Reflect, per-call through libdevice fast variants, and per-instruction through PTX rounding modifiers. |
| Denormal / Subnormal | An IEEE 754 number with the implicit leading 1 absent, allowing magnitudes below the smallest normal at the cost of reduced relative precision. GPU pipelines often FTZ them for throughput. |
| FMA (Fused Multiply-Add) | The operation `a*b + c` computed with a single rounding step. Lower error and higher throughput than separate multiply and add. See [Fast-Math and Numerical Precision](topics/fast-math-and-numerical-precision.md). |
| Fast-math flags | The LLVM IR flag set carried on floating-point ops: `nnan` (no NaNs), `ninf` (no Infs), `nsz` (no signed zero), `arcp` (allow reciprocal), `contract` (allow FMA contraction), `afn` (approximate function), `reassoc` (allow reassociation). Tileiras propagates these through NVVM lowering. |

## Reverse Engineering and Binary

| Term | Meaning |
| --- | --- |
| ELF (Executable and Linkable Format) | The standard Linux binary format. Both the tileiras driver shared object and ptxas's input/output use ELF containers. |
| Stripped | A binary with its symbol table removed. Tileiras ships stripped, which is why the wiki refers to internal routines by `sub_ADDR` instead of source names. See [Binary Anatomy and RE Methodology](topics/binary-anatomy-and-re-methodology.md). |
| `sub_ADDR` | IDA Pro's auto-generated name for an unnamed function at virtual address `ADDR` (hex). The wiki uses this convention to cite specific routines in the stripped binary. |
| IDA Pro | The commercial disassembler and decompiler used to recover tileiras's behavior from its stripped shared object. See [Binary Anatomy and RE Methodology](topics/binary-anatomy-and-re-methodology.md). |
| vtable | The per-class table of virtual-function pointers a C++ object carries when it has virtual methods. The wiki cites vtable layouts when discussing dialect interfaces, pass classes, and pattern rewriters. |
| RTTI (Run-Time Type Information) | The standard-C++ mechanism for runtime type identification via `typeid`/`dynamic_cast`. LLVM and MLIR disable RTTI for code size; tileiras uses MLIR's `TypeID` machinery instead. See [TypeID Sentinels and Anchors](mlir-infra/typeid-sentinels-and-anchors.md). |

## CUDA Toolchain

| Term | Meaning |
| --- | --- |
| nvcc | The top-level CUDA compiler driver. Invokes the host compiler, cudafe++, cicc/tileiras, ptxas, fatbinary, and the host linker. See [nvcc 13.1 Position](boundaries/nvcc-13-1-position.md). |
| ptxas | The PTX → SASS assembler. Receives PTX text from tileiras and emits a cubin for one SM target. See [ptxas Handoff Protocol](boundaries/ptxas-handoff-protocol.md). |
| cudafe++ | NVIDIA's CUDA C++ frontend. Splits a CUDA source file into host and device translation units before either side is compiled. See [cudafe Non-Relationship](boundaries/cudafe-non-relationship.md). |
| cicc | The older LLVM-based device compiler that lowers CUDA C++ device IR to PTX. Shares the NVPTX backend family with tileiras but starts from cudafe++ output rather than TileIR bytecode. See [cicc Comparison](boundaries/cicc-comparison.md). |
| libdevice | NVIDIA's device-side math bitcode library, linked into device modules that call `__nv_*` math functions. Configured through NVVM-Reflect at link time. See [libdevice Overview](libdevice/overview.md). |
| NVVM | NVIDIA's variant of LLVM IR for device code. Tileiras's final MLIR form lowers into NVVM-flavored LLVM IR, which is then translated to PTX. |
| NVVM-Reflect | The mechanism that resolves environment-style integer queries (`__CUDA_FTZ`, `__CUDA_PREC_SQRT`, SM version, etc.) into compile-time constants, controlling which libdevice variants survive optimization. See [NVVMReflect Mechanism](libdevice/nvvm-reflect-mechanism.md). |
| Fatbin | A container format holding multiple cubin and/or PTX images for different SM targets in one file. Produced by `fatbinary` and consumed by the CUDA runtime for JIT or load-time selection. |
| Cubin | A compiled CUDA binary for one SM target, produced by ptxas. The unit packaged into a fatbin. |

## Scheduler Coordination Values

| Term | Meaning |
| --- | --- |
| AsyncValue | The umbrella value family the TileAS scheduler emits to model async coordination resources after placement. `Pipe_` and `Mutex_` are the two concrete shapes; both are interned and fingerprinted (Blake3) so identical synchronization patterns share storage. See [AsyncValue and Blake3 Interning](mlir-infra/asyncvalue-and-blake3-interning.md). |
| `Pipe_` | A depth-`d` producer/consumer ring buffer with bounded slack between producer and consumer stages. Emitted by `Schedule::solve` after placement. See [Pipe and Mutex Value Layout](scheduler/pipe-mutex-value-layout.md). |
| `Mutex_` | A zero-slack mutual-exclusion edge between successive iterations of a protected region. Iteration `i` must complete before iteration `i+1` starts. See [Pipe and Mutex Value Layout](scheduler/pipe-mutex-value-layout.md). |
| Rau scheduling | The Rau 1994 modulo-scheduling algorithm: search an initiation interval, place each operation into a cycle modulo II, and respect both recurrence and resource constraints. Tileiras's `TileASGenerateSchedule` is a Rau-style placement engine. See [Modulo Scheduler and Rau](scheduler/modulo-scheduler-and-rau.md). |
| RRT (Resource Reservation Table) | A per-cycle bitset table indexed modulo the candidate II, where each row records which resource classes are occupied. The scheduler probes the RRT before committing an operation to a cycle. See [Resource Constraint Builder and RRT](scheduler/resource-constraint-builder-and-rrt.md). |
| Modulo Initiation Interval (II) | The number of cycles between starts of successive software-pipeline iterations under a modulo schedule. Smaller II raises throughput; the scheduler searches upward from the maximum of the resource, recurrence, and dependence lower bounds. See [Modulo Scheduler and Rau](scheduler/modulo-scheduler-and-rau.md). |

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
