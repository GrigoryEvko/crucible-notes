# Glossary

> *All names and addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; wheel/`METADATA`/`__init__` version `0.0.40`). The binary is **not** stripped: every C++ symbol survives as a demangled name, so a "term is grounded" claim here means the literal string or symbol is present in the binary's name/string tables. Other builds will differ.*

## Abstract

This is the canonical definition source for every acronym, codename, and term the rest of this wiki uses. It exists so that no other page has to re-define `MXU`, `SCS`, `Mosaic`, or `gfc` in passing — they link here, and here each term gets a tight one-to-three-sentence definition plus a pointer to the deep page that owns it. A reader who lands mid-graph on a page about, say, the SparseCore tile-execute sequencer can resolve `TEC`, `Spmem`, and `vex` here in seconds and then follow the link to the page that documents the mechanism.

The entries are grouped by domain — **Silicon & Codenames**, **Compute Units**, **Memory Spaces**, **Compiler & IR**, **ABI & Runtime**, **Collectives & Network**, **Profiling**, and **Reverse-Engineering Terms** — and within each group they read as a definition list. Every term that is a concrete binary fact (a codec namespace, a sequencer ordinal, a C-API struct) was re-confirmed against the binary's name and string tables before being defined; terms that are *external* facts (the marketing name "Trillium", the conventional acronym "PSUM") or that are *inferred* carry an explicit `(inferred)` or `(not in binary)` tag rather than a fabricated anchor.

Two structural traps recur often enough to be worth stating before the body: a single TPU generation wears at least seven names spread across **three numerically-disjoint integer axes** (`TpuVersion`, `DeviceType`, `TpuVersionProto`), and the SparseCore sequencer enum exists in **two off-by-one numberings** (codec-template vs proto/runtime). Both are defined below and flagged where a reimplementer is most likely to index the wrong table.

For navigation, the contract is:

- **Every binary-grounded term cites its grounding** — a symbol, a namespace, an ELF section, or the owning deep page.
- **Uncertain or external terms are flagged** with `(inferred)`, `(external)`, or `(not in binary)`; they are never given a fake address.
- **The two recurring traps** (the three-axis codename numbering and the two-numbering sequencer enum) get a callout where they live.

| | |
|---|---|
| **Plugin** | `libtpu.so` — Google TPU PJRT plugin, wheel version `0.0.40` (build-id `89edbbe81c5b328a958fe628a9f2207d`), ~745 MB |
| **Companion** | `sdk.so` — proto/debug-event split-off; see [Two-Binary Split](forensics/two-binary-split.md) |
| **Codec axis** | `tpu::TpuVersion` `0..5` (dense) — owns the codename binding |
| **Profiler axis** | `xprof::DeviceType` `1..13` (sparse) — *different numbering* |
| **ISA level** | `LLO` (Low-Level Optimizer) — the per-bundle target IR |
| **Canonical codename card** | [Codename Cheat-Sheet](front/codename-cheatsheet.md) |

---

## Silicon & Codenames

Three orthogonal naming systems describe one chip: the **codec/ISA codename** (a two-or-three-letter tag baked into namespaces), the **fish codename** (a marketing-adjacent internal name in `.rodata`), and the **public Cloud name** (`v2`…`tpu7x`). The codec codenames nest two levels deep under `asic_sw::driver::deepsea::` — a *family* tag (`jxc`/`pxc`/`vxc`/`gxc`) then a fetch/load *sub-core* tag. All counts below are confirmed hits in the name table unless tagged.

| Term | Definition |
|---|---|
| **Jellyfish / `jxc`** | First-generation codec family; covers Jellyfish (`TpuVersion` 0, "TPU v2") and Dragonfish (`TpuVersion` 1, "TPU v3"), which share one fused codec with no fetch/load split. Namespace `asic_sw::driver::deepsea::jxc::*` (7,495 hits). Owned by [JXC Family](targets/jxc-family.md). |
| **Dragonfish** | Internal codename for "TPU v3" — `TpuVersion` 1, still inside the `jxc` family. Literal `dragonfish` is in `.rodata`. |
| **Pufferfish / `pxc` / `pfc`** | Codec family for "TPU v4"; `pxc::pfc` is the fetch sub-core (16,999 hits). `TpuVersion` 2. Owned by [PXC Family](targets/pxc-family.md). |
| **Puffylite / `plc`** | The v4-class *lite* variant; the load sub-core `pxc::plc` (2,174 hits). Has its own profiler `DeviceType` (8) but **no own `TpuVersion`** — it folds into Pufferfish (`TpuVersion` 2). |
| **Viperfish / `vxc` / `vfc`** | Codec family for "TPU v5"; `vxc::vfc` fetch sub-core (35,203 hits). `TpuVersion` 3. Owned by [VXC Family](targets/vxc-family.md). |
| **Viperfish-lite / `vlc`** | The v5-class lite variant; load sub-core `vxc::vlc` (4,223 hits). `DeviceType` 11, folds into Viperfish (`TpuVersion` 3). |
| **Ghostlite / `glc`** | `gxc` family, load sub-core `gxc::glc` (126,927 hits). `TpuVersion` 4, `DeviceType` 13, Cloud `v6e`. Ships a **named** codec class `tpu::TpuCodecGhostlite`. |
| **`gfc` / `6acc60406` / Ironwood** | `gxc` family, fetch sub-core `gxc::gfc` (126,696 hits). `TpuVersion` 5, `DeviceType` 12, display string "TPU7x". The chip's only *internal* name in the binary is the obfuscated tag `6acc60406`; "Ironwood" (the v7x marketing name) and "Ghostfish" are **not in the binary** (external names). Do not confuse with "Trillium", which is the prior v6e/Ghostlite generation. |
| **`TpuVersion`** | Internal dense `0..5` codec/HAL axis; `TpuVersionToString` indexes a 6-pointer table at `off_22011BF0`. This is the axis the codec, HAL, and ISA emitter switch on. |
| **`DeviceType`** | Profiler's sparse `1..13` axis (`xprof::DeviceType`); assigned per-PCI-tuple by `DeviceTypeFromDeviceIdentifiers`. The eight TPU silicon families land on `{3,5,7,8,10,11,12,13}`. |
| **`TpuVersionProto`** | The protobuf wire enum `1..6`; `internal = proto − 1`. This is why the embedded `6acc60406_chip_parts.binarypb` carries `version = 6` for internal `TpuVersion` 5. |
| **HAL** | Hardware Abstraction Layer — the `TpuHal{Jxc,Pxc,Vxc}HardwareFactory` classes. Note `gxc` (Ghostlite + `gfc`) registers into the **shared** `Vxc` factory; there is no `TpuHalGxc`. |
| **BarnaCore / `bcs` / `brn`** | A separate scalar sub-core present in the early `jxc` family (`jxc::bcs`, `jxc::brn`); a 32-byte-bundle scalar ISA distinct from the TensorCore. Owned by [BarnaCore](barnacore/overview.md). |

> **GOTCHA — `TpuVersion` ≠ `DeviceType`.** These are two enums maintained by two subsystems and were never meant to align. `TpuVersion` 4 is Ghostlite; `DeviceType` 4 is not any TPU generation. The `DeviceType` numbering is also chronologically inverted at the top: Ghostlite (older, v6e) = `13`, `6acc60406` (newer, v7x) = `12`. A reimplementer who indexes the profiler's per-device table with a compiler `TpuVersion` ordinal reads the wrong row. Resolve through [Codename Cheat-Sheet](front/codename-cheatsheet.md).

> **NOTE — "Trillium", "Ironwood", "Ghostfish" are not in the binary.** The literal strings have zero occurrences. The `gxc::gfc` abbreviation *plausibly* stands for a "Ghostfish"-style fish name, but that name is **not present** — cite `6acc60406` (internal) or `TPU7x` (display) instead. The Trillium = `v6e` = Ghostlite identity is correct but is an *external* (Cloud-documentation) fact.

---

## Compute Units

The TensorCore is the dense-math engine; SparseCore is a separate co-processor for embedding/gather workloads, each with its own sequencer-typed ISA. Acronyms here name the per-slot functional units a bundle's slots target.

| Term | Definition |
|---|---|
| **TensorCore / TC** | The dense matrix/vector core — the primary compute engine driving the MXU, VPU, and XLU. `TensorCore` appears 447,417 times in the name table; sequencer-type ordinal 0 (codec-template). Cross-cuts [ISA Overview](isa/overview.md). |
| **MXU** | Matrix-multiply Unit — the systolic array doing the dense `dot`/`conv` contraction. Symbol family `…::Mxu` (476 hits). Owned by [MXU Slot](isa/slot-mxu.md); allocation in [MXU Assignment Bin-Packer](sched/mxu-assignment-binpacker.md). |
| **VPU** | Vector Processing Unit — the SIMD lane engine for elementwise vector ops. `…::Vpu` (522 hits). Owned by [VPU Slot](isa/slot-vpu.md). |
| **XLU** | Cross-Lane Unit — the lane-shuffle/reduction unit (`CrossLane*`, `…::Xlu`, 442 hits) that moves data *between* VPU lanes (transpose, reduce, broadcast). Owned by [XLU Op Roster](isa/xlu-op-roster.md). |
| **EUP** | Extended Unary Processor / transcendental unit — computes `exp`, `log`, `rsqrt`, and friends. Symbol family `…::Eup` (1,025 hits). Owned by [EUP Transcendental Slot](isa/slot-eup-transcendental.md). |
| **SPU** | Scalar Processing Unit — the per-bundle scalar ALU/address engine. `SPU` appears (20 name hits); the bundle's scalar slot. Owned by [SPU Scalar Slot](isa/slot-spu-scalar.md). |
| **SparseCore / SC** | The embedding/sparse co-processor; namespace `SparseCore*` (127,401 hits). A separate ISA with three sequencer types (SCS/TAC/TEC). Owned by [SparseCore Architecture](sparsecore/architecture.md). |
| **SCS** | SparseCore **S**calar sequencer — the control/scalar engine. Codec-template sequencer ordinal **3** (proto/runtime **4**). Symbol family `SparseCoreScs*` (6,425 hits). Owned by [SCS Engine](sparsecore/scs-engine.md). |
| **TAC** | SparseCore **T**ile-**A**ccess sequencer — drives gather/scatter tile addressing. Ordinal **4** (proto **5**). `SparseCoreTac*` (5,966 hits). Owned by [TAC Engine](sparsecore/tac-engine.md). **Dropped on `gfc`/v7x.** |
| **TEC** | SparseCore **T**ile-**E**xecute sequencer — runs the per-tile compute. Ordinal **5** (proto **6**). `SparseCoreTec*` (72,530 hits). Owned by [TEC Engine](sparsecore/tec-engine.md). |
| **VEX** | Vector-Extended unit inside SparseCore — the SparseCore vector datapath (mask/dest-port/sub-opcode encoded). Owned by [VectorExtended (vex)](sparsecore/vectorextended-vex.md). |

> **GOTCHA — 6acc60406 (`gfc`/v7x) ships SCS + TEC only, no TAC.** Confirmed in the binary: `gfc::isa::SparseCoreScs*` (1,139 hits) and `gfc::isa::SparseCoreTec*` (17,193 hits) are present, but `gfc::isa::SparseCoreTac*` has **zero** hits. Viperfish (`vfc`) and Ghostlite (`glc`) carry all three sequencers; v7x drops the tile-access engine. A reimplementation that assumes a uniform SparseCore triad across all SparseCore-bearing generations emits a TAC codec the v7x hardware has no sequencer for. See [GetSequencerType](sparsecore/getsequencertype.md).

> **GOTCHA — the SparseCore sequencer enum has two off-by-one numberings.** The codec template instantiates `{SCS=3, TAC=4, TEC=5}`; the proto/runtime form is one higher, `{SCS=4, TAC=5, TEC=6}`. `TpuSequencerTypeFromProto` is the literal `internal = proto − 1` switch. Mixing the two silently encodes for the wrong engine (or for the reserved slot 2/3). See [Sequencer Ops Per Gen](isa/sequencer-ops-per-gen.md).

---

## Memory Spaces

A TPU program addresses several distinct, non-coherent memory spaces, each with its own allocator and addressing rules. The space is part of the LLO memory-space enum (see [Memory-Space Enum](isa/memory-space-enum.md)); names below are the binary's own.

| Term | Definition |
|---|---|
| **HBM** | High-Bandwidth Memory — off-chip DRAM, the largest tier; holds program inputs/outputs and spilled state. Symbol family `…Hbm…` (27,346 hits). Owned by [HBM Allocator](memory/hbm-allocator.md). |
| **VMEM** | Vector Memory — on-chip scratchpad for vector tiles feeding the VPU/MXU. `…Vmem…` (8,284 hits). Owned by [VMEM Allocator](memory/vmem-allocator.md). |
| **SMEM** | Scalar Memory — on-chip scalar scratchpad / register window for the SPU. `…Smem…` (7,319 hits). Owned by [SMEM Scalar Memory](memory/smem-scalar-memory.md). |
| **CMEM** | Common Memory — a shared on-chip pool (cross-core / staging). `…Cmem…` (1,344 hits). Owned by [CMEM Pool](memory/cmem-pool.md). |
| **Spmem** | SparseCore-private scratch memory (`Spmem`, 13,122 hits) — the buffer space `AllocateDefaultSpmemWindow` carves for SparseCore lowering. Distinct from VMEM/SMEM. Documented under [SparseCore](sparsecore/overview.md). |
| **PSUM** | Partial-Sum accumulator memory — the MXU's result-accumulation banks. The *acronym* `PSUM` is **not in the binary**; the grounded name is `PartialSum*` (979 name hits). `(external acronym)` — use `PartialSum` when grepping. |
| **SFLAG** | Sync-Flag memory — the small flag/semaphore space used for cross-engine synchronization. Grounded as `SyncFlag*` (9,882 hits) / `Sflag` (1,158 hits). Owned by [SFLAG Protocol](memory/sflag-protocol.md). |
| **MRB** | Matrix-Result Buffer — the FIFO/buffer holding MXU outputs before write-back; symbol family `…::Mrb…` (82 hits). Owned by [MRB Chain Allocator](sched/mrb-chain-allocator.md) and [MRB FIFO/MSR Placement](sched/mrb-fifo-msr-placement.md). |

> **NOTE — PSUM is a convention, PartialSum is the binary.** The wiki uses "PSUM" because it is the standard TPU term for the MXU accumulator banks, but the literal string `PSUM` does not appear; every binary anchor is to `PartialSum`. Do not search for `PSUM` and conclude the feature is absent.

---

## Compiler & IR

The compiler ingests a high-level graph (HLO / StableHLO), lowers it through MLIR dialects (MHLO, the TPU dialect, Mosaic) to the per-bundle LLO target IR, schedules bundles, and assigns memory. Names below are the dialect/pass terms a compiler-pages reader meets.

| Term | Definition |
|---|---|
| **HLO** | High-Level Optimizer IR — XLA's classic op-graph IR; the compiler's ingestion form. Owned by [HLO Ingestion](compiler/hlo-ingestion.md). |
| **MHLO** | The MLIR dialect mirror of HLO — the entry MLIR dialect for the TPU lowering pipeline. See [MHLO→XTile/TPU Lowering](compiler/mhlo-xtile-tpu-lowering.md). |
| **StableHLO** | The version-stable MLIR HLO dialect used for JAX native serialization; the binary carries the `StableHLO module` op-doc string (71 string hits). The portable interchange form ahead of MHLO. |
| **Mosaic** | The TPU tiling/vector-layout MLIR framework that maps tensor ops onto VMEM tiles and lane layouts (`Mosaic*`, 514 hits). Owned by [Mosaic Overview](compiler/mosaic-overview.md); layout inference in [Mosaic Layout Inference](compiler/mosaic-layout-inference.md). |
| **TPU dialect** | The MLIR dialect of TPU-native ops sitting below Mosaic, lowered to LLO via ODS-generated patterns. See [TPU Dialect and Ops](compiler/tpu-dialect-and-ops.md) and [TPU→LLO ODS](compiler/tpu-to-llo-ods.md). |
| **LLO** | Low-Level Optimizer IR — the per-bundle target IR the ISA emitter consumes; the opcode enum and proto form live in `Llo*` (25,333 hits). Owned by [LLO Opcode Enum](isa/llo-opcode-enum.md). |
| **the bundle** | One VLIW instruction word — a fixed-width pack of per-unit slots (MXU/VPU/XLU/EUP/SPU/sequencer/memory). Width is per-generation (41-bit JF, 51-bit PF, 64-bit VF, etc.). Owned by [Bundle Model Overview](isa/bundle-model-overview.md). |
| **MSA** | Memory-Space Assignment — the pass that places buffers across HBM/VMEM/etc. and reserves segments. Grounded as `MemorySpaceAssignment*` (544 hits). Owned by [MSA Overview](compiler/msa-overview.md). |
| **LatencyHidingScheduler / LHS** | The bundle scheduler that overlaps compute with DMA/memory latency (`LatencyHiding*`, 363 hits). Owned by [Latency-Hiding Scheduler Core](sched/latency-hiding-scheduler-core.md). |
| **TpuCodec** | The per-`TpuVersion` codec object that owns ISA encode/decode; built by the 6-case switch `tpu::TpuCodec::Create(TpuVersion)`. Symbol family `TpuCodec*` (128 hits). The codec-template that instantiates per-sequencer codecs (and the SCS/TAC/TEC ordinals) hangs off this. |
| **SPMD / sharding** | Single-Program-Multiple-Data partitioning — the auto-sharding pass that splits a program across cores; `SpmdPartitioner*` (596 hits), `ShardingPropagation*` (143 hits). Owned by [Auto-Sharding SPMD](compiler/auto-sharding-spmd.md) and [Sharding Propagation](compiler/sharding-propagation.md). |
| **autotune** | The cache that memoizes best-found kernel configs (`Autotune*`, 610 hits; `autotune_results`, 8 hits). Cache mode is `READ`/`UPDATE`/`UNSPECIFIED`; invalidation is the user's responsibility. See [Fusion Cost Model](compiler/fusion-cost-model.md). |
| **SymbolicTile / IndexingMap** | The tiling cost-model primitives: a `SymbolicTile` is an `IndexingMap` triple (offset-map, size-map, stride-map) describing how a tile maps to a tensor (`SymbolicTile*` 272, `IndexingMap*` 8,014 hits). Used by the loop-tiling and fusion passes. |
| **addrspacecast** | The MLIR/LLVM address-space cast op (`addrspacecast*`, 1,578 hits) — the lowering that re-typed a pointer between memory spaces (notably the SparseCore fat-pointer spaces). See [AddrSpaceCast ISel](sparsecore/addrspacecast-isel.md) and [Fat Pointers AS7/8/9](sparsecore/fat-pointers-as789.md). |
| **walrus** | An IR/compiler term carried in upstream context but with **zero occurrences** in this binary (name and string tables, case-insensitive). `(not in binary)` — do not anchor any claim to it; if a feature was attributed to "walrus", re-derive it from a grounded symbol before citing. |

> **CORRECTION (GLOSS-1) — "walrus" is not present in `libtpu.so`.** Prior scratch context listed `walrus` among the IR/compiler terms. A case-insensitive search of both the name and string tables returns nothing. It is retained here only as a flagged absence so later pages do not silently treat it as binary-grounded.

---

## ABI & Runtime

The plugin's external surface is the PJRT C-API, layered over the older `TfTpu` C-API. Runtime moves data in and out of the device via infeed/outfeed and executes async on streams.

| Term | Definition |
|---|---|
| **PJRT** | Portable JAX/XLA Runtime — the C-ABI plugin contract the framework calls. The plugin's struct family `PJRT_Client` / `PJRT_Buffer` / `PJRT_Executable` / `PJRT_Device` / `PJRT_Event` is all present. Owned by [PJRT Overview](pjrt/overview.md). |
| **`PJRT_Api` / `GetPjrtApi`** | The top-level dispatch vtable struct and its entry-point getter (`GetPjrtApi`, 1 hit) the framework loads to reach every PJRT function. Reconstructed in [API Vtable Reconstruction](pjrt/api-vtable-reconstruction.md). |
| **TfTpu C-API** | The older TensorFlow-TPU C-API (`TfTpu_Initialize`, etc., 117 `TfTpu` hits) that PJRT layers over; still the substrate for executable execution. See [Stream-Executor / PJRT Adapter](pjrt/stream-executor-pjrt-adapter.md). |
| **StreamExecutor** | The XLA device-abstraction layer (`StreamExecutor*`, 787 hits) PJRT wraps; manages streams, allocations, and async events on the TPU. See [Stream-Executor / Host Interpreter](pjrt/stream-executor-host-interpreter.md). |
| **buffer lifecycle** | The PJRT device-buffer path: `BufferFromHostBuffer` (upload) → `DonateWithControlDependency` (alias-into-execute) → `Execute` → `Delete`. External lifetime is ref-counted via `Increase/DecreaseExternalReferenceCount`. Owned by [Buffer and Memory](pjrt/buffer-and-memory.md). |
| **AsyncValue / ReadyEvent** | The async-completion primitives: a `PJRT_Event` (`Await`/`OnReady`/`IsReady`) wraps an `AsyncValue` (5,088 hits); a buffer's `ReadyEvent` signals when its contents are valid. Owned by [Events and Async](pjrt/events-and-async.md). |
| **custom-call** | The `tpu_custom_call` HLO escape hatch for hand-written kernels (`CustomCall*`, 4,319 hits). The plugin has **no user FFI extension** — custom ops route only through `tpu_custom_call` registration. See [Custom-Call Lowering](compiler/custom-call-lowering.md). |
| **DmaMap / cross-host receive** | The PJRT Client-extension surface (`DmaMap`/`DmaUnmap`, 584 hits) that pins host memory for direct cross-host buffer transfers. Owned by [DMA and Cross-Host Recv](pjrt/dma-and-cross-host-recv.md). |
| **infeed / outfeed** | The host↔device streaming channels: *infeed* pushes inputs to the device queue, *outfeed* drains outputs (`Infeed*` 4,025 / `Outfeed*` 3,323 name hits). Owned by [Infeed/Outfeed](runtime/infeed-outfeed.md). |
| **ICI** | Inter-Chip Interconnect — the on-pod chip-to-chip fabric (`ICI`, 1,527 string hits). Carries collective traffic inside a slice/pod. Owned by [ICI Overview](ici/overview.md). |
| **Megascale** | The cross-host control plane above ICI (`xla::megascale::runtime::*`); coordinates multi-slice/multi-pod execution over the DCN. Owned by [Megascale Overview](megascale/overview.md). |
| **the trace codec** | The profiler component that serializes per-core trace entries; the codec *alternative* is selected by raw PCI identity (not by `TpuVersion`/`DeviceType`). See [Riegeli Trace Container](profiling/riegeli-trace-container.md). |

---

## Collectives & Network

Collective communication (all-reduce, all-gather, reduce-scatter) runs over ICI inside a pod and over the DCN across hosts. Topology terms below describe the fabric a collective is scheduled onto.

| Term | Definition |
|---|---|
| **DCN** | Data-Center Network — the inter-host network above ICI (`DCN`, 472 string hits); `DCNTopology` is its topology proto. The transport Megascale coordinates over. |
| **superpod / pod** | A topology tier: chips form a pod (toroidal ICI mesh), pods form a superpod (`HierarchicalTopology`). 3D-Superpod resiliency mode tolerates sub-cube ICI link faults. See [Routing](routing/overview.md). |
| **Megacore fusion** | A collective-lowering optimization that fuses the collective with adjacent compute across the two cores of a chip. Owned by [Megacore Fusion](collectives/megacore-fusion.md). |
| **polarity** | An ICI link-assignment attribute (a polarity state machine assigns link directions during topology bring-up). See [Link Bring-up](ici/link-bringup.md). |

---

## Profiling

The profiler emits the Google XProf schema — a proto-defined event tree (`XSpace` → `XPlane` → `XEvent` + `XStat`) — with per-core trace dispatchers, one per sub-core type. Terms below name that schema.

| Term | Definition |
|---|---|
| **XPlane** | A timeline plane in the XProf schema — one device or host gets one plane (`XPlane*`, 1,679 name hits). Owned by [XPlane / XStat / TraceMe](profiling/xplane-xstat-traceme.md). |
| **XEvent** | A single timed event on an XPlane (`XEvent*`, 377 hits) — a span with start/duration and a metadata id. |
| **XStat** | A typed key/value statistic attached to an XEvent or XPlane (`XStat*`, 244 hits). |
| **XSpace** | The top-level container holding all XPlanes for one capture (`XSpace*`, 332 hits). |
| **TraceMe** | The instrumentation primitive that emits an XEvent for a scoped region (`TraceMe*`, 1,121 hits). |
| **CoreDispatcher** | The per-sub-core trace-entry decoder; the binary instantiates one per codec family (`gfc`, `glc`, `jxc`, `pxc`, `vfc`, `vlc`). Owned by [Trace Entries Coder](profiling/trace-entries-coder.md). |
| **TpuProfiler ABI** | The C-ABI profiler surface (`TpuProfiler_Create`/`Start`/`Stop`/`CollectData`). Owned by [TPU Profiler ABI](profiling/tpu-profiler-abi.md). |

---

## Reverse-Engineering Terms

These are the static-analysis terms the wiki uses to describe *how* a fact was recovered from the binary, not facts about the TPU itself.

| Term | Definition |
|---|---|
| **vtable** | The C++ virtual-method dispatch table; emitted as `_ZTV…` symbols (39,244 hits). Because the binary is unstripped, a vtable address resolves to the class it belongs to. See [RTTI/Vtable Census](forensics/rtti-vtable-census.md). |
| **RTTI / typeinfo** | Run-Time Type Information — the `_ZTI…` type-info records (60,457 hits) and the `typeinfo` name strings (141,445 hits) the compiler emits per polymorphic class; the primary lever for naming otherwise-anonymous objects. See [RTTI/Vtable Census](forensics/rtti-vtable-census.md). |
| **`.lrodata` / `.lbss`** | Large-code-model ELF sections (both present in this binary). `.lrodata` holds large read-only data (the codename tables, proto descriptors) and `.lbss` the large zero-init data, addressed with 64-bit relocations rather than the ±2 GB small model. See [ELF Anatomy](forensics/elf-anatomy.md). |
| **FLIRT** | Fast Library Identification and Recognition Technology — IDA's library-function fingerprinting, used to label the statically-linked Abseil/protobuf/tcmalloc/dnnl code so it can be excluded from TPU-specific analysis. Embedded libraries catalogued in [Embedded-Library Atlas](forensics/embedded-library-atlas.md). |
| **sidecar** | A machine-readable export of the disassembly (the `_names.json`, `_strings.json`, `_segments.json`, callgraph, and ctree files) that the wiki greps instead of re-running the decompiler. Every count on this page is a sidecar hit. |
| **dispatch table** | A compiled jump/pointer table (a `switch` or a per-`TpuVersion` function-pointer array) the binary indexes at run time; the per-generation ones are the spine of the codename derivation. See [Dispatch-Table Taxonomy](forensics/dispatch-table-taxonomy.md) and [Per-Gen Function Dispatcher](forensics/per-gen-function-dispatcher.md). |
| **the two-binary split** | The wheel ships `libtpu.so` (the plugin) and a companion `sdk.so` (proto/debug-event definitions); many `libtpu.sdk.*` symbols live in the latter. See [Two-Binary Split](forensics/two-binary-split.md). |

---

## Cross-References

- [Codename Cheat-Sheet](front/codename-cheatsheet.md) — the canonical card binding every codename across all three integer axes; come here first for any `jxc`/`glc`/`DeviceType` question
- [Subsystem Map](subsystem-map.md) — the top-level layout these terms are grouped by
- [ISA Overview](isa/overview.md) · [Memory-Space Enum](isa/memory-space-enum.md) · [Bundle Model Overview](isa/bundle-model-overview.md) — owns the compute-unit, memory-space, and bundle terms
- [Memory Overview](memory/overview.md) — owns HBM/VMEM/SMEM/CMEM/SFLAG/MRB
- [SparseCore Overview](sparsecore/overview.md) · [GetSequencerType](sparsecore/getsequencertype.md) — owns SCS/TAC/TEC and the two-numbering trap
- [Compiler Overview](compiler/overview.md) · [Mosaic Overview](compiler/mosaic-overview.md) · [MSA Overview](compiler/msa-overview.md) — owns HLO/MHLO/StableHLO/Mosaic/LLO/MSA
- [PJRT Overview](pjrt/overview.md) · [API Vtable Reconstruction](pjrt/api-vtable-reconstruction.md) — owns the PJRT/TfTpu ABI terms
- [Collectives Overview](collectives/overview.md) · [ICI Overview](ici/overview.md) · [Megascale Overview](megascale/overview.md) — owns ICI/DCN/Megascale/superpod
- [Profiling Overview](profiling/overview.md) · [XPlane / XStat / TraceMe](profiling/xplane-xstat-traceme.md) — owns the XProf schema terms
- [Forensics Overview](forensics/overview.md) · [RTTI/Vtable Census](forensics/rtti-vtable-census.md) · [ELF Anatomy](forensics/elf-anatomy.md) — owns the reverse-engineering vocabulary
