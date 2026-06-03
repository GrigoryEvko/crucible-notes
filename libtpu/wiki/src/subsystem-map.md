# Subsystem Map

> *All addresses on this page apply to `libtpu.so` v0.103 from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ. This is the root-level map for the whole wiki; every Part links back here.*

## Abstract

`libtpu.so` is a single 745 MiB shared object that is the entire Google TPU software stack below the framework line: a PJRT plugin, the XLA→TPU compiler, every per-generation instruction encoder, the cost model and schedulers, the on-device runtime, the ICI/Megascale collective fabric, and the profiler — all statically linked into one ELF. There is no separate compiler binary, no separate runtime daemon inside the process; the framework loads this `.so`, calls one exported symbol, and receives a function-pointer table that fans out to all of it. This page is the architectural index: it names the major subsystems, gives the entry point and address band of each, and routes to the wiki Part that documents it in depth.

The shape is best understood as **one ABI surface over nine cooperating subsystems**. The ABI surface is the PJRT C plugin (`GetPjrtApi`, a 140-slot struct plus an extension chain) layered on top of a second, internal C-ABI shim (the `TfTpu` per-class roster) that decouples the framework-facing plugin from the implementation. Behind those two surfaces sit the compiler (HLO down to per-gen instruction bundles), the ISA encoders that turn the compiler's last IR into bytes, the cost model and schedulers the compiler consults, the runtime that loads and executes the resulting executables, the collective/network fabric the runtime drives for multi-chip programs, the profiler that observes all of it, and the memory subsystem every layer allocates from. The familiar reference frame is **XLA**: this is the XLA:TPU backend and PJRT:TPU plugin, compiled to a single object and reverse-engineered from the binary alone.

Because everything is one binary, the most useful navigation aid after the subsystem names is the **address band**. The `.text` segment runs `0xe63c000`–`0x21217484` (~300 MiB of code) and is not randomly laid out: the PJRT/lifecycle entry points cluster low (`0xe6a…`–`0xf8…`), the compiler and MLIR lowering occupy the broad middle, the ISA encoders and codec factories sit higher (`0x1e8…`), and the collective/profiler trace machinery is higher still (`0xf3…`–`0xf5…` for trace factories, with route/fabric code spread across the upper bands). Mutable singletons — most notably the PJRT API table — live in `.lbss` (`0x227ba840`+). The at-a-glance table below pins each subsystem to a band; the per-subsystem sections expand each one.

For navigation, the contract of this page is:

- **The nine subsystems and their boundaries** — what each owns, and where one ends and the next begins.
- **The headline entry point of each** — a confirmed address you can seek to in the binary.
- **The address band each occupies** — so a function found at an arbitrary address can be attributed to a subsystem.
- **The owning wiki Part** — every subsystem links to its `overview.md` and that Part's detail pages.

| | |
|---|---|
| **Binary** | `libtpu.so`, 745 MiB (781,691,048 B), unstripped, single ELF |
| **Plugin entry** | `GetPjrtApi` @ `0xe6a83a0` (thunk) |
| **PJRT builder** | `pjrt::tpu_plugin::GetTpuPjrtApi` @ `0xe6aa440` (lazy, returns the singleton) |
| **PJRT struct factory** | `pjrt::CreatePjrtApi` @ `0xf874160` (140 slots, `struct_size = 1120`) |
| **PJRT singleton** | `GetTpuPjrtApi()::pjrt_api` @ `0x227ba840` (start of `.lbss`) |
| **Shim bootstrap** | `TfTpu_Initialize` @ `0xe6f54a0` |
| **ISA codec factory** | `tpu::TpuCodec::Create(TpuVersion)` @ `0x1e835fa0` |
| **Profiler codec factory** | `xprof::tpu::GetTraceCodec` @ `0xf5a2900` |
| **`.text` band** | `0xe63c000` – `0x21217484` (all entry points above live here) |
| **Static init** | `.init_array` `0x215f26f0`–`0x215f8190` (~2,900 constructors); orchestrated from `init_proc` @ `0xe635524` |

## Subsystems at a Glance

The nine functional subsystems, each pinned to its headline entry point, its rough `.text` band, and the wiki Part that owns it. Confidence reflects how directly the entry point and boundary were observed in the binary, not the maturity of the owning Part.

| Subsystem | Job | Main entry point (addr) | Address band | Owning Part | Confidence |
|---|---|---|---|---|---|
| **PJRT plugin surface** | Framework-facing C ABI: client/device/buffer/executable/event, 140-slot `PJRT_Api` + extension chain | `GetPjrtApi` @ `0xe6a83a0` → `GetTpuPjrtApi` @ `0xe6aa440` → `CreatePjrtApi` @ `0xf874160` | `0xe6a…`–`0xf87…` (low `.text`) | [`pjrt/`](pjrt/overview.md) | CERTAIN |
| **TfTpu C-API shim** | Internal C ABI between plugin and implementation; per-class roster of function pointers | `TfTpu_Initialize` @ `0xe6f54a0` | `0xe6f…`–`0xe8d…` | [`shim/`](shim/overview.md) | HIGH |
| **XLA→TPU compiler** | HLO → MHLO → Mosaic/`tpu` dialect → LLO → bundles; the pass pipeline | compiler pass registry / `RunHloPasses` driver (mid-`.text`) | `0x10e…`–`0x1c0…` (broad middle) | [`compiler/`](compiler/overview.md) | HIGH |
| **Per-gen ISA + encoders** | TensorCore / SparseCore / BarnaCore instruction emitters; LLO → bundle bytes | `tpu::TpuCodec::Create(TpuVersion)` @ `0x1e835fa0` | `0x1e8…`–`0x20f…` | [`isa/`](isa/overview.md) | HIGH |
| **Cost model + schedulers** | Per-gen latency tables, `LatencyHidingScheduler`, MSA (memory-space assignment) | `XlaTpuMemorySpaceAssignment*` knobs; LHS / MSA drivers | `0x14…`–`0x1d…` (interleaved) | [`cost/`](cost/overview.md), [`sched/`](sched/overview.md) | MEDIUM |
| **Runtime** | Executable load/execute, streams, infeed/outfeed, allocators, completion loop | PJRT `Execute` → stream-executor adapter (low-mid `.text`) | `0xf8…`–`0x13…` | [`runtime/`](runtime/overview.md) | HIGH |
| **Collectives + network** | ICI fabric, Megascale control plane, route-table generation | ICI/Megascale slice-config factories | `0x130…`–`0x1f8…` (upper `.text`) | [`collectives/`](collectives/overview.md), [`ici/`](ici/overview.md), [`megascale/`](megascale/overview.md), [`routing/`](routing/overview.md) | MEDIUM |
| **Profiling** | Per-core trace codec → XPlane/XStat → XSpace; TraceMe | `xprof::tpu::GetTraceCodec` @ `0xf5a2900` | `0xf3…`–`0xf5…` (trace factories) | [`profiling/`](profiling/overview.md) | HIGH |
| **Memory** | HBM / VMEM / SMEM / CMEM / SFLAG pools and allocators; embedded tcmalloc | HBM allocator + tcmalloc (`MallocExtension_Internal_*` @ `0xe639600`) | `0xe63…` (tcmalloc) + per-pool sites | [`memory/`](memory/overview.md) | MEDIUM |

> **NOTE —** the bands overlap because the binary is not partitioned by subsystem at link time. A band is the *center of mass* of a subsystem's code, useful for attributing an arbitrary address to a probable owner; it is not a hard boundary. When in doubt, resolve the symbol and read its owning Part. The [forensics](forensics/overview.md) Part documents how the bands were derived.

## Data Flow

The end-to-end path from an XLA HLO module to executing TPU bundles, with the collective fabric and profiler wrapped around the core compile-then-execute spine. Read top-to-bottom for the compile path, then the runtime loop at the bottom; collectives and profiling are the side rails.

```text
                       framework (JAX / TF / PyTorch-XLA)
                                     |
                                     v
        +===========================================================+
        |  PJRT PLUGIN SURFACE          pjrt/overview.md            |
        |  GetPjrtApi@0xe6a83a0 -> GetTpuPjrtApi@0xe6aa440          |
        |          -> CreatePjrtApi@0xf874160 (140 slots)          |
        |  [ singleton pjrt_api @ .lbss 0x227ba840 ]               |
        +===========================================================+
                                     |  Compile(args)        Execute(args)
                                     v                            |
        +===========================================================+   |
        |  TfTpu C-API SHIM             shim/overview.md            |   |
        |  TfTpu_Initialize@0xe6f54a0  -> per-class roster fanout   |   |
        +===========================================================+   |
                                     |                              |
   HLO module --------------------->  v                             |
        +===========================================================+   |
        |  XLA->TPU COMPILER           compiler/overview.md         |   |
        |   HLO  -> [pre-passes, simplifier, sharding, fusion]      |   |
        |        -> MHLO -> Mosaic / `tpu` dialect (xtile)          |   |
        |        -> LLO (low-level op enum)                         |   |
        |        |          ^                  ^                    |   |
        |        |   consults |          consults |                |   |
        |        |   cost/overview.md     sched/overview.md         |   |
        |        |   (latency tables)     (LHS, MSA, bundle pack)   |   |
        |        v                                                  |   |
        +===========================================================+   |
                  | LLO instructions                                    |
                  v                                                      |
        +===========================================================+   |
        |  PER-GEN ISA + ENCODERS      isa/overview.md              |   |
        |  TpuCodec::Create(TpuVersion)@0x1e835fa0                  |   |
        |  TensorCore / SparseCore / BarnaCore emitters            |   |
        |        -> packed instruction BUNDLES (.df/.gf/.jf/...)    |   |
        +===========================================================+   |
                  | executable (bundles + metadata)                     |
                  v                                                      v
        +===========================================================================+
        |  RUNTIME                                       runtime/overview.md        |
        |  load executable -> streams -> infeed/outfeed -> execute -> completion    |
        |  allocators draw from  MEMORY  (memory/overview.md):                      |
        |       HBM | VMEM | SMEM | CMEM | SFLAG                                    |
        +===========================================================================+
             ^                                  |                          |
             | drives                           | emits trace events       |
             v                                  v                          v
   +===========================+   +=========================================+
   | COLLECTIVES + NETWORK     |   | PROFILING                               |
   | collectives|ici|megascale |   | GetTraceCodec@0xf5a2900                  |
   | routing/overview.md       |   | per-core codec -> XPlane/XStat -> XSpace |
   | ICI fabric, route tables, |   | profiling/overview.md                   |
   | Megascale control plane   |   +=========================================+
   +===========================+
```

> **QUIRK —** the compiler does **not** call the ISA encoders directly through a function pointer it stores; it lowers to the LLO opcode enum, and a separate codec factory (`TpuCodec::Create`) materializes the per-generation emitter selected by `TpuVersion`. The generation is therefore a *late* binding: the same lowered LLO can be encoded for different TPU generations by handing a different `TpuVersion` to the factory. A reimplementation that fuses lowering and encoding into one pass loses this seam. See [`isa/isa-emitter-registry.md`](isa/isa-emitter-registry.md).

---

## PJRT Plugin Surface

The framework's only required entry point. `GetPjrtApi` @ `0xe6a83a0` is a thin thunk that tail-calls `pjrt::tpu_plugin::GetTpuPjrtApi` @ `0xe6aa440`, a lazy builder that constructs the `PJRT_Api` struct once and caches it in the static-local singleton `pjrt_api` @ `0x227ba840` (the first object in `.lbss`). The struct itself is assembled by `pjrt::CreatePjrtApi` @ `0xf874160`, which fills a **140-slot** `struct_size = 1120` record (5 scalar header fields + 135 function pointers) and then walks an **extension chain** of optional capability blocks (layouts, raw-buffer, profiler, Megascale, cross-host-transfers, and others) linked through `PJRT_Extension_Base` next-pointers. This is the standard PJRT C ABI: client/device/memory enumeration, buffer lifecycle (`BufferFromHostBuffer` → donate → `Execute` → delete), async events (`PJRT_Event` with `Await`/`OnReady`/`IsReady`), and executable compile/execute.

> **GOTCHA —** there is **no FFI extension** in this build. User custom-call FFI is unsupported; the only custom-op path is through the `tpu_executable` extension's compilation-environment hook. A reimplementer wiring an FFI slot will find no consumer for it.

Owned by **Part `pjrt/`**. Start at [`pjrt/overview.md`](pjrt/overview.md); the 140-slot reconstruction is in [`pjrt/api-vtable-reconstruction.md`](pjrt/api-vtable-reconstruction.md) and the optional blocks in [`pjrt/extension-chain.md`](pjrt/extension-chain.md). The bootstrap path (ELF entry → `init_proc` → thunk) is in [`lifecycle/`](lifecycle/overview.md).

---

## TfTpu C-API Shim

A second C ABI sits *behind* the PJRT surface, between the plugin and the implementation. `TfTpu_Initialize` @ `0xe6f54a0` bootstraps it: it brings up the embedded runtime and populates a set of **per-class rosters** — flat structs of function pointers, one per implementation class (compiler, executor, transfer manager, topology, program, executable, configuration). The PJRT plugin calls into these rosters rather than calling implementation methods directly, which is what lets the framework-facing ABI stay stable while the implementation behind it changes. This is the classic TensorFlow `TfTpu_*` "stream-executor C API" pattern, here statically linked instead of `dlopen`-ed.

The rosters are not exposed as a flat list of `TfTpu_*` exports (only a handful exist as named symbols); they are vtable-like structs filled in during initialization. Each roster maps to one detail page under the Part.

Owned by **Part `shim/`**. Start at [`shim/overview.md`](shim/overview.md); the per-class rosters are [`shim/tpu-compiler-roster.md`](shim/tpu-compiler-roster.md), [`shim/tpu-executor-roster.md`](shim/tpu-executor-roster.md), [`shim/tpu-transfer-manager.md`](shim/tpu-transfer-manager.md), [`shim/tpu-program-roster.md`](shim/tpu-program-roster.md), [`shim/tpu-executable-roster.md`](shim/tpu-executable-roster.md), and [`shim/tpu-configuration-api.md`](shim/tpu-configuration-api.md). The initialization sequence is in [`lifecycle/tftpu-initialize-bootstrap.md`](lifecycle/tftpu-initialize-bootstrap.md).

---

## XLA→TPU Compiler

The largest subsystem by code volume, spanning the broad middle of `.text`. It ingests an XLA HLO module and lowers it through a sequence of IRs: HLO pre-passes and the algebraic simplifier clean up the graph; sharding propagation and auto-sharding (SPMD) distribute it; fusion forms kernels; the graph is then raised into **MHLO** and lowered into the **Mosaic / `tpu` dialect** (the "xtile" tiling representation), then down to the **LLO** low-level op enum that the ISA encoders consume. The pipeline is a registry of HLO passes (the "walrus"/pass-pipeline driver) plus an MLIR conversion stack with pattern rewriters and a dialect-conversion legalizer. SparseCore programs take a parallel lowering branch into SparseCore LLVM.

This is the XLA:TPU backend. The reference frame is LLVM/MLIR dialect conversion: a reimplementer should think in terms of pass managers, conversion patterns, and legalization targets, not a monolithic translator.

Owned by **Part `compiler/`**. Start at [`compiler/overview.md`](compiler/overview.md). Key pages: [`compiler/compile-phases.md`](compiler/compile-phases.md), [`compiler/hlo-pass-registry.md`](compiler/hlo-pass-registry.md), [`compiler/mosaic-overview.md`](compiler/mosaic-overview.md), [`compiler/tpu-dialect-and-ops.md`](compiler/tpu-dialect-and-ops.md), [`compiler/tpu-to-llo-ods.md`](compiler/tpu-to-llo-ods.md), and the MSA pages [`compiler/msa-overview.md`](compiler/msa-overview.md).

---

## Per-Gen ISA + Encoders

Where the compiler's last IR becomes bytes. The factory `tpu::TpuCodec::Create(tpu::TpuVersion)` @ `0x1e835fa0` selects a per-generation codec keyed on `TpuVersion`, and that codec drives the instruction emitters for up to three engine types: **TensorCore** (the matrix engine, on every generation), **SparseCore** (the embedding/gather engine, Viperfish onward), and **BarnaCore** (the earlier embedding coprocessor, Jellyfish/Dragonfish/Pufferfish only — retired and replaced by SparseCore at the Pufferfish→Viperfish boundary). Each generation has its own bundle width and slot layout — `.df`, `.gf`, `.gl`, `.jf` (41-byte), `.pf` (51-byte), `.vf` (64-byte, also Ghostlite and 6acc60406) — and the encoder packs the LLO instruction stream into those fixed-width bundles. The instruction-bit master DB and the slot encoders (MXU, VPU, EUP, sequencer, memory load/store, predicate, immediate) live in this band.

> **QUIRK —** the per-gen codec is the *encoder* registry, parallel to but distinct from the *cost-model* per-gen tables. The codec decides how bits are laid out; the cost model decides how many cycles a bundle takes. Both are keyed on the same `TpuVersion`, but they are separate data tables in separate bands. See [`isa/bundle-model-overview.md`](isa/bundle-model-overview.md) and [`cost/overview.md`](cost/overview.md).

Owned by **Part `isa/`** (with **`sparsecore/`** and **`barnacore/`** for the two non-TensorCore engines). Start at [`isa/overview.md`](isa/overview.md); the encoder registry is [`isa/isa-emitter-registry.md`](isa/isa-emitter-registry.md), the LLO opcode enum is [`isa/llo-opcode-enum.md`](isa/llo-opcode-enum.md). SparseCore: [`sparsecore/overview.md`](sparsecore/overview.md). BarnaCore: [`barnacore/overview.md`](barnacore/overview.md).

---

## Cost Model + Schedulers

The compiler consults two cooperating subsystems while lowering. The **cost model** is a family of per-generation latency tables (MXU latency, EUP latency, per-opcode cycle constants, memory bandwidth/latency) plus an HLO cost analyzer that estimates resource use; `GetHloResources` routing maps each HLO op to a resource vector. The **schedulers** consume those costs: the `LatencyHidingScheduler` (LHS) orders instructions to overlap compute with memory/collective latency, **MSA** (memory-space assignment) places tensors across HBM/VMEM, and a bundle modulo-scheduler packs the per-gen slots. Flag strings such as `xla_tpu_memory_space_assignment_while_execution_count` and the per-gen `xla_{vf,gf}_max_vmem_used_by_memory_space_assignment` confirm the MSA knob surface; the LHS has an ILP variant and pre-/post-layout passes.

The reference frame is the standard XLA `LatencyHidingScheduler` and `MemorySpaceAssignment`, plus modulo scheduling for the VLIW bundle packing. These are keyed per generation because the latency tables differ across TPU versions.

Owned by **Parts `cost/` and `sched/`**. Start at [`cost/overview.md`](cost/overview.md) and [`sched/overview.md`](sched/overview.md). Key pages: [`cost/mxu-latency-overview.md`](cost/mxu-latency-overview.md), [`cost/tpu-hlo-cost-analysis.md`](cost/tpu-hlo-cost-analysis.md), [`sched/latency-hiding-scheduler-core.md`](sched/latency-hiding-scheduler-core.md), [`sched/bundle-modulo-scheduling.md`](sched/bundle-modulo-scheduling.md).

---

## Runtime

The execution half of the plugin. Once an executable (bundles plus metadata) exists, the runtime loads it, sets up **streams**, wires **infeed/outfeed** queues, runs the program, and drives a **completion loop** that signals the PJRT events. Buffer lifecycle is async: `BufferFromHostBuffer` produces a buffer with a `ReadyEvent`; `DonateWithControlDependency` enables in-place reuse; external references are tracked with `Increase/DecreaseExternalReferenceCount`. Multi-stream execution creates an async-tracking event per device. The runtime draws all device memory from the memory subsystem's allocators.

The reference frame is XLA's stream-executor plus the PJRT execution model. The host-side adapter (stream-executor → PJRT) and the host interpreter are documented as their own pages.

Owned by **Part `runtime/`**. Start at [`runtime/overview.md`](runtime/overview.md). Key pages: [`runtime/load-program-enqueue.md`](runtime/load-program-enqueue.md), [`runtime/execute-async-on-stream.md`](runtime/execute-async-on-stream.md), [`runtime/infeed-outfeed.md`](runtime/infeed-outfeed.md), [`runtime/completion-loop.md`](runtime/completion-loop.md), [`runtime/allocator-integration.md`](runtime/allocator-integration.md). The executable-execution PJRT slot is [`pjrt/executable-execution.md`](pjrt/executable-execution.md).

---

## Collectives + Network

Multi-chip and multi-host programs run a collective fabric the runtime drives. Three layers stack here. **ICI** (Inter-Chip Interconnect) is the on-pod toroidal fabric: link bring-up, DMA descriptors, topology discovery, virtual-channel balancing, and a fault/resiliency model that tolerates ICI link faults only for 3D Superpod slices under resiliency mode. **Routing** generates the route tables (randomized toroidal wildfirst, route-cache codec with dedup/decompress) that map logical collective patterns onto physical links. **Megascale** is the cross-host control plane above ICI: a DCN (Data Center Network) topology, a per-host control handshake, an error aggregator, and a `tpunetd` gRPC client. The **collectives** layer implements the algorithms themselves (all-reduce hierarchical pincer, all-gather ND ring, reduce-scatter, all-to-all), including FP8-quantized variants and SparseCore-offloaded paths.

Owned by **Parts `collectives/`, `ici/`, `megascale/`, `routing/`** (with **`dma/`** for the descriptor formats). Start at [`collectives/overview.md`](collectives/overview.md), [`ici/overview.md`](ici/overview.md), [`megascale/overview.md`](megascale/overview.md), [`routing/overview.md`](routing/overview.md). DMA descriptors: [`dma/intra-chip-descriptor.md`](dma/intra-chip-descriptor.md).

---

## Profiling

An observation subsystem wrapped around everything. `xprof::tpu::GetTraceCodec(asic_sw::DeviceIdentifiers, int)` @ `0xf5a2900` is the factory that selects a **per-chip-family trace codec** — five fixed-width `TraceEntry` schemas (`pxc`, `vfc`, `vlc`, `gfc`, `glc`), plus the legacy `jxc` `PerformanceTraceEntry` path, each decoded by its own templated codec. The decoded trace entries are converted into Google XProf's **XPlane / XStat / XSpace** proto schema and surfaced through the `TpuProfiler_*` C ABI (`Create`/`Start`/`Stop`/`CollectData`) and the `PJRT_Profiler` extension. `TraceMe` scopes and per-core trace controls (the `xprof::tpu::AddObjectsForKind` / `Add*TraceControls` factories in the `0xf34…` band) seed the trace limits.

> **NOTE —** the codec is *per sub-core type*, not per chip. A reimplementer must instantiate the right `TraceEntry` codec for each core class in the topology; one codec does not decode another core's trace stream.

Owned by **Part `profiling/`**. Start at [`profiling/overview.md`](profiling/overview.md). Key pages: [`profiling/tpu-profiler-abi.md`](profiling/tpu-profiler-abi.md), [`profiling/trace-entries-coder.md`](profiling/trace-entries-coder.md), [`profiling/xplane-xstat-traceme.md`](profiling/xplane-xstat-traceme.md), [`profiling/pjrt-profiler-extension.md`](profiling/pjrt-profiler-extension.md).

---

## Memory

Every layer above allocates from a small set of physically distinct memory spaces, each with its own allocator and protocol. **HBM** is the large off-chip pool (the HBM allocator with DMA-alignment rules and a reservation policy MSA respects); **VMEM** is the vector-engine scratchpad; **SMEM** is scalar memory plus the register window; **CMEM** is a separate constant/coefficient pool; **SFLAG** is the synchronization-flag space used to coordinate engines. The host-side allocator is the **embedded tcmalloc** (its own `google_malloc` ELF section at `0xe6373c0`–`0xe63bab2`, with `MallocExtension_Internal_GetExperiments` @ `0xe639600` as a stable anchor) — distinct from the on-device pools.

> **GOTCHA —** the memory-space enum is shared between the compiler (MSA places tensors into spaces) and the ISA (memory-load/store slots encode the space). The same integer means the same physical space in both subsystems; treating them as independent namespaces will mis-encode every load. See [`isa/memory-space-enum.md`](isa/memory-space-enum.md).

Owned by **Part `memory/`**. Start at [`memory/overview.md`](memory/overview.md). Key pages: [`memory/hbm-allocator.md`](memory/hbm-allocator.md), [`memory/vmem-allocator.md`](memory/vmem-allocator.md), [`memory/smem-scalar-memory.md`](memory/smem-scalar-memory.md), [`memory/cmem-pool.md`](memory/cmem-pool.md), [`memory/sflag-protocol.md`](memory/sflag-protocol.md), [`memory/embedded-tcmalloc.md`](memory/embedded-tcmalloc.md).

---

## How the Pieces Bind

Three binding mechanisms hold the nine subsystems together, and each is the seam a reimplementer must reproduce:

| Seam | Mechanism | Where documented |
|---|---|---|
| Framework ↔ plugin | The 140-slot `PJRT_Api` struct + extension chain, built once and cached in `.lbss` | [`pjrt/api-vtable-reconstruction.md`](pjrt/api-vtable-reconstruction.md) |
| Plugin ↔ implementation | The `TfTpu` per-class rosters, filled by `TfTpu_Initialize` | [`shim/overview.md`](shim/overview.md) |
| Compiler ↔ ISA | The LLO opcode enum + the late `TpuCodec::Create(TpuVersion)` binding | [`isa/llo-opcode-to-proto.md`](isa/llo-opcode-to-proto.md) |
| Compiler ↔ cost/sched | The HLO resource model + per-gen latency tables | [`cost/gethloresources-routing.md`](cost/gethloresources-routing.md) |
| All layers ↔ memory | The shared memory-space enum + per-pool allocators | [`isa/memory-space-enum.md`](isa/memory-space-enum.md) |
| Process startup | `.init_array` (~2,900 ctors) → `init_proc` → plugin discovery | [`lifecycle/elf-entry-and-init-proc.md`](lifecycle/elf-entry-and-init-proc.md) |

The single most important structural fact is that **the version (`TpuVersion`) is the master key**. It is the dimension along which the ISA encoders, the cost-model latency tables, the schedulers' resource models, and several memory defaults all fan out. A reimplementation that hardcodes one generation will silently mis-encode, mis-cost, and mis-schedule the others; the per-gen dispatch is the architecture, not an afterthought. The [forensics](forensics/overview.md) Part documents the per-gen dispatch machinery directly.

## Cross-References

- [`pjrt/overview.md`](pjrt/overview.md) — the framework-facing C ABI surface; the 140-slot table and extension chain
- [`shim/overview.md`](shim/overview.md) — the internal `TfTpu` roster ABI between plugin and implementation
- [`compiler/overview.md`](compiler/overview.md) — HLO → MHLO → Mosaic/`tpu` → LLO lowering and the pass pipeline
- [`isa/overview.md`](isa/overview.md) — per-gen instruction bundles and the encoder registry; the codec factory
- [`sparsecore/overview.md`](sparsecore/overview.md) — the SparseCore engine and its parallel lowering branch
- [`barnacore/overview.md`](barnacore/overview.md) — the scalar control core ISA
- [`cost/overview.md`](cost/overview.md) — per-gen latency tables and the HLO cost analyzer
- [`sched/overview.md`](sched/overview.md) — LatencyHidingScheduler, MSA, bundle modulo scheduling
- [`runtime/overview.md`](runtime/overview.md) — executable load/execute, streams, infeed/outfeed, completion loop
- [`collectives/overview.md`](collectives/overview.md) — collective algorithms and SparseCore offload
- [`ici/overview.md`](ici/overview.md) — the on-pod toroidal interconnect fabric
- [`megascale/overview.md`](megascale/overview.md) — the cross-host control plane and `tpunetd` client
- [`routing/overview.md`](routing/overview.md) — route-table generation and the route-cache codec
- [`dma/host-device-dma.md`](dma/host-device-dma.md) — DMA descriptor formats used by runtime and fabric
- [`profiling/overview.md`](profiling/overview.md) — per-core trace codec → XPlane/XStat → XSpace
- [`memory/overview.md`](memory/overview.md) — HBM/VMEM/SMEM/CMEM/SFLAG pools and allocators
- [`config/overview.md`](config/overview.md) — XLA flags, DebugOptions, and the TPU compilation environment
- [`lifecycle/overview.md`](lifecycle/overview.md) — ELF entry, static init, plugin discovery, `TfTpu_Initialize` bootstrap
- [`forensics/overview.md`](forensics/overview.md) — ELF anatomy, address-band derivation, per-gen dispatch, embedded-library atlas
- [back to index](index.md) — Part 0 — Reference Apparatus
