# libtpu Internals — Reverse-Engineering Reference

> **Status**: index drafted (426 pages across 18 parts; 18 written) · **Primary binary**: `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` — 781,691,048 B, x86-64 ELF64 DYN, **not stripped**, build-id `89edbbe81c5b328a958fe628a9f2207d` · **Secondary**: `sdk.so` (94,732 functions)

## What this reference is

A reimplementation-grade reverse-engineering reference for **Google's `libtpu.so`** — the PJRT plugin that exposes Cloud TPU hardware to JAX, PyTorch/XLA, and TensorFlow. It is the functional equivalent of NVIDIA's `libcuda.so` + `libnvrtc.so` + the device-specific half of `nvcc`/`ptxas`, compressed into a single 745 MB monolithic shared object that statically links the entire XLA compiler, every TPU MLIR dialect, the per-generation LLVM backends, oneDNN, tcmalloc, Abseil, gRPC, protobuf, Eigen, the TPU runtime, the device-driver shim, and the ICI/DCN fabric stack.

Everything here was reconstructed **purely from static analysis of the binary** — `objdump`, `nm`, `readelf -rW`, raw byte reads, and `protoc --decode_raw` of carved descriptors. The binary ships unstripped (884,843 named functions), which is why reconstruction reached byte-exact / reimplementation grade across most of the surface.

## Why it is hard

- **884,843 functions** in the analysis database; 1,249,324 strings; ~52 GB of extracted IDA sidecars.
- **40,313 dispatch tables** (≈100× ptxas's 409), classified into 17 taxonomy classes.
- **160,566 RTTI entries** spanning `mlir::` (95k), `asic_sw::deepsea::` (77k), `xla::` (36k), `llvm::` (30k), `dnnl::` (44k), and ~150 smaller namespaces.
- **~2,900 static constructors** in `.init_array`; **1,069,006 relocations**.
- A trailing **25.8 MB zstd blob** past `.strtab` (the "4.1 MB dictionary blob" was a false positive — see Part I).
- Custom ELF sections (`google_malloc`, `malloc_hook`, `protodesc_cold`, `filewrapper_toc`, `__rseq_cs`, `__lcxx_override`).
- Six TPU silicon generations under a Google-internal codename ladder: `jellyfish → dragonfish → pufferfish → viperfish → ghostlite → 6acc60406`, each with its own ISA encoding, cost model, and HAL family.

## Two-tier C ABI

```
                JAX / PyTorch-XLA / TensorFlow
                          │
                          ▼  PJRT C-API (v0.103)
              ┌──────────────────────────────┐
              │   PJRT layer  (444 exports)  │   ← outer ABI  (Part II)
              │   GetPjrtApi @ 0xe6a83a0     │
              │   140-slot PJRT_Api struct   │
              │   17 extensions chained      │
              └──────────────┬───────────────┘
                             │  Tpu* C shim (~200 symbols)
              ┌──────────────────────────────┐
              │   libtpu runtime + compiler  │   ← inner ABI  (Part III)
              │   xla::jellyfish::*          │
              │   asic_sw::deepsea::*        │
              │   platforms_deepsea::*       │
              └──────────────────────────────┘
```

## How this reference is organized

The 18 parts follow the **data's own dependency chain**, not an alphabetical or importance order. Each part can be read assuming only the parts before it:

```
  silicon model ─► compiler passes ─► ISA encoding ─► cost model ─► scheduling
       (IV)             (V)              (VI)            (VII)         (VIII)
                                                                         │
   specialized engine (SparseCore, IX) ◄───────────────────────────────┘
       │
   on-chip memory & DMA (X) ─► runtime (XI) ─► distributed fabric (XII–XIV)
                                                  │
                              observability (XV) ─┴─► configuration (XVI)
```

The compiler back-end is deliberately factored along the canonical three-concern seam — **what instructions exist (VI)**, **what they cost (VII)**, **how to order and pack them (VIII)** — because in this binary the cost-model *data* is ~3× the volume of the scheduling *algorithms*, and conflating them produced a 50-page monster. SparseCore (IX) is kept whole rather than sliced across that seam: it is a self-contained engine a reader wants in one place.

### Status and evidence grade

Each page below carries a grade reflecting the maturity of its backing raw-findings file(s):

- `C` — **Confirmed / reimplementation-grade**: byte-anchored against objdump/nm/readelf or `protoc --decode_raw` of carved descriptors. The default for the `P-3-*` deep-dive corpus.
- `I` — **Inferred / synthesis**: wave-2 (`P-2-*`) foundations, forensic surveys (`W*`), per-gen-parametric claims, or connective overview pages.
- `O` — **Open**: not yet recovered; tracked in the [Open-Frontier Register](appendix/open-frontier-register.md).

Pages marked *(written)* already have content; all others are scaffolded stubs awaiting authoring.

### Parts at a glance

| Part | Title | Pages | Written | Depends on | Source domain |
|------|-------|------:|:-------:|-----------|---------------|
| 0 | Reference Apparatus | 9 | 1 | — | — |
| I | Binary Anatomy | 12 | 0 | 0 | forensics / dispatch / RTTI |
| II | Plugin Lifecycle & PJRT API | 23 | 0 | I | runtime / PJRT |
| III | Tpu C-Shim Layer | 10 | 0 | II | shim |
| IV | Silicon & Hardware Codename Model | 24 | 0 | — | silicon |
| V | Compiler — Lowering & Optimization Passes | 36 | 0 | IV | compiler |
| VI | TensorCore ISA & LLO Encoding | 42 | 0 | IV, V | ISA |
| VII | Cost & Latency Model | 41 | 0 | IV, VI | cost |
| VIII | Instruction Scheduling & Bundle Packing | 14 | 0 | VI, VII | cost / scheduling |
| IX | SparseCore & BarnaCore | 45 | 0 | IV, VI, VII | sparsecore |
| X | On-Chip Memory & DMA | 20 | 0 | IV | memory / DMA |
| XI | Runtime & Execution | 11 | 0 | II, VI, X | runtime |
| XII | Interconnect & Routing | 30 | 0 | IV | collectives / routing |
| XIII | On-Pod Collectives & Barriers | 30 | 0 | IX, XII | collectives |
| XIV | Megascale (Multi-Host / DCN) | 21 | 17 | XII, XIII | collectives / DCN |
| XV | Profiling & Telemetry | 22 | 0 | XI, XII | profiler |
| XVI | Configuration & Compile Knobs | 16 | 0 | V, VII | config |
| XVII | Appendices | 20 | 0 | all | cross-cutting |
| | **Total** | **426** | **18** | | |

### Per-generation navigation cross-index

The book is heavily per-generation. To trace one silicon generation end-to-end, follow its row:

| TpuVersion | Codename | Cloud / marketing | Family page | ISA bundle | MXU latency | Performance grid |
|-----------:|----------|-------------------|-------------|-----------|-------------|------------------|
| 0 | Jellyfish | TPU v2 | `targets/jxc-family.md` | `isa/bundle-jf-41b.md` | `cost/mxu-latency-jf-df.md` | `cost/performance-jf-df.md` |
| 1 | Dragonfish | TPU v3 | `targets/jxc-family.md` | `isa/bundle-df.md` | `cost/mxu-latency-jf-df.md` | `cost/performance-jf-df.md` |
| 2 | Pufferfish | TPU v4 | `targets/pxc-family.md` | `isa/bundle-pf-51b.md` | `cost/mxu-latency-pf.md` | `cost/performance-pf.md` |
| 3 | Viperfish | TPU v5 / v5e | `targets/vxc-family.md` | `isa/bundle-vf-64b.md` | `cost/mxu-latency-vf.md` | `cost/performance-vf.md` |
| 4 | Ghostlite | TPU v6e (Trillium) | `targets/gxc-family.md` | `isa/bundle-gl.md` | `cost/mxu-latency-gl.md` | `cost/performance-gl-ghperf.md` |
| 5 | 6acc60406 | TPU7x | `targets/gxc-family.md` | `isa/bundle-gf.md` | `cost/mxu-latency-gf.md` | `cost/performance-gf-ghperf.md` |

The one-page consolidated constants table is [Per-Gen Master Comparison Matrix](appendix/per-gen-comparison-matrix.md).

### Reading paths

- **Reimplement the cost model / scheduler** — IV (silicon constants) → VI (ISA) → VII (cost data) → VIII (scheduling).
- **Understand TPU-to-TPU collectives** — IV → XII (fabric + routing) → XIII (collective algorithms) → XIV (multi-host).
- **Parse a compiled program / bundle bytes** — VI (ISA encoding) → X (memory & DMA) → XI (runtime load/exec).
- **Write or debug a PJRT consumer** — II (PJRT API) → III (Tpu C-shim) → XI (execution) → XV (profiling).
- **Trace one TPU generation end-to-end** — use the per-gen cross-index above: family (IV) → bundle (VI) → MXU latency + perf grid (VII).
- **Debug a hang / deadlock** — XIII (barriers + SFLAG) → X (continuation-queue) → XII (VC-balance + routing).
- **Just get oriented** — 0 (Reference Apparatus, esp. the *Compile-Flow Walkthrough*) → I (Binary Anatomy) → IV (codename model).

## Conventions

- Function addresses are virtual addresses (`@0x…`); for `.lrodata`/`.rodata`, VA == file offset.
- Each page carries a **References** block: source binary path, the function/symbol VAs it cites, and the raw-findings file(s) it derives from.
- The `_src:_` tags in this index point at the internal raw-findings corpus (`P-2-*`, `P-3-*`, `W*`) that backs each page; the full page→file→VA mapping is the [Evidence-Anchor Index](appendix/evidence-anchor-index.md).

## The source corpus

This book is the promotion of a 412-file, ~33 MB raw-findings corpus (`libtpu/raw/`) into reader-facing pages: 322 byte-level deep dives (`P-3-*`), 14 wave-2 foundations (`P-2-*`), and ~70 forensic/substrate reports (`W*`, `p0-*`, `p1-*`).

---

# Master Index

## Part 0 — Reference Apparatus (9)

Orientation and connective tissue. Read the *Compile-Flow Walkthrough* first — it traces one matmul through every part and is the on-ramp to the whole book.

- `index.md` — **Landing / This Reference** · `I` *(written)*  
  What libtpu is, binary provenance, organization, per-gen cross-index, reading paths.
- `front/how-to-read.md` — **How to Read This Book** · `I`  
  Evidence grades, the dependency-flow rationale, the reading-path personas.
- `front/compile-flow-walkthrough.md` — **Compile-Flow Walkthrough** · `I`  
  One `dot` op traced HLO → MHLO → tpu → LLO → bundle bytes → execution, cross-referencing every part. _src: P-2-03, P-2-01_
- `methodology.md` — **Methodology** · `I`  
  Extraction pipeline, IDA sidecars, FLIRT, `protoc --decode_raw`, naming conventions. _src: W001, P-3-22_
- `subsystem-map.md` — **Subsystem Map** · `I`  
  The 13-domain dependency web and how the 18 parts cover it. _src: W001, G001–G003_
- `front/codename-cheatsheet.md` — **Codename Cheat-Sheet** · `C`  
  TpuVersion 0–5 ↔ codename ↔ chip-DID ↔ Cloud name on one card. _src: P-3-262_
- `glossary.md` — **Glossary** · `I`  
  LLO, MXU, XLU, EUP, SCS/TAC/TEC, MRB, SFLAG, ICI/DCN and the rest.
- `front/evidence-conventions.md` — **Evidence & Confidence Conventions** · `I`  
  The C/I/O grades and the anchor format used throughout. _src: W001_
- `bibliography.md` — **Bibliography** · `I`  
  External references; explicit note on what is *not* in the binary (Trillium/Ironwood are external names only).

## Part I — Binary Anatomy (12)

How the 745 MB ELF is laid out and navigated. Analysis and orientation only; the large enumerated catalogs live in Part XVII.

- `forensics/overview.md` — **Overview** · `I`  
  The two binaries, the section model, why it is this large. _src: W023, W027_
- `forensics/elf-anatomy.md` — **ELF Anatomy** · `C`  
  52 sections, segments, the VA==offset rule, `.lrodata`/`.rodata`/`.text` extents. _src: W023, W030, P-3-212_
- `forensics/two-binary-split.md` — **libtpu.so + sdk.so** · `C`  
  The 884,843-fn main object and the 94,732-fn sdk; symbol-population shape. _src: W001, W026_
- `forensics/custom-sections.md` — **Custom Sections** · `C`  
  google_malloc, protodesc_cold, filewrapper_toc, __rseq_cs, __lcxx_override. _src: P-2-07, W030_
- `forensics/embedded-library-atlas.md` — **Embedded-Library Atlas** · `C`  
  Vendored Abseil/protobuf/Eigen/oneDNN/tcmalloc/LLVM byte-accounting (FLIRT). _src: P-3-22, P-3-81, P-3-149_
- `forensics/llvm-mlir-manifest.md` — **LLVM/MLIR Version Manifest** · `C`  
  Embedded toolchain version + the component list. _src: P-3-118_
- `forensics/static-init.md` — **Static-Init Pipeline** · `I`  
  ~2,900 ctors, init ordering, the plugin-discovery hooks. _src: P-3-185_
- `forensics/trailing-zstd-blob.md` — **Trailing zstd Blob** · `C`  
  The 25.8 MB payload and the "4.1 MB dictionary blob" false-positive correction. _src: P-3-01, P-2-30_
- `forensics/dispatch-table-taxonomy.md` — **Dispatch-Table Taxonomy** · `C`  
  40,313 tables → 17 classes (MLIR Op-Model, UFB pools, libpfm4, dnnl/Xbyak…). _src: P-3-112, P-2-06_
- `forensics/rtti-vtable-census.md` — **RTTI ↔ Vtable Cross-Validation** · `C`  
  Every typeinfo mapped to its vtable; the namespace census. _src: P-3-265, P-3-127_
- `forensics/per-gen-function-dispatcher.md` — **Per-Generation Function Dispatcher** · `C`  
  The `util_registration::FunctionRegistry` dispatch engine. _src: P-3-270, P-3-394_
- `forensics/polymorphic-entry-points.md` — **Polymorphic Dispatch Entry Points** · `C`  
  The indirect-call sites + the thunk-table and top-vtable classes. _src: P-3-272, P-3-263, P-3-264_

## Part II — Plugin Lifecycle & PJRT API (23)

The outer ABI: how the plugin loads and the 140-slot PJRT_Api struct JAX/PyTorch consume.

#### Lifecycle
- `lifecycle/overview.md` — **Overview** · `I`  
  From `dlopen` to a usable client. _src: P-3-185_
- `lifecycle/elf-entry-and-init-proc.md` — **ELF Entry & init_proc** · `C`  
  `_init`, `.init_array`, the GOT/PLT bring-up. _src: P-3-185_
- `lifecycle/do-init-do-fini.md` — **do_init / do_fini** · `C`  
  Constructor/destructor ordering and global state. _src: P-3-185_
- `lifecycle/get-pjrt-api-thunk.md` — **GetPjrtApi Thunk & tpu_plugin Object** · `C`  
  `@0xe6a83a0` trampoline → `GetTpuPjrtApi` → 17 `__cxa_guard` blocks. _src: P-2-05, P-3-10_
- `lifecycle/tftpu-initialize-bootstrap.md` — **TfTpu_Initialize Bootstrap** · `I`  
  The initialize entry and option ingest. _src: P-3-185, P-3-70_
- `lifecycle/module-init-plugin-discovery.md` — **Module-Init & Plugin Discovery** · `C`  
  How the PJRT plugin is registered and found. _src: P-3-185_

#### PJRT_Api surface
- `pjrt/overview.md` — **Overview** · `I`  
  The C-API version, the extension-chain idea, `.lbss` storage. _src: P-3-10_
- `pjrt/api-vtable-reconstruction.md` — **PJRT_Api 140-Slot Reconstruction** · `C`  
  Every slot → libtpu impl VA, v0.103 schema, the `@0x227BA840` 1120-B table. _src: P-3-10, P-2-05_
- `pjrt/client-and-device.md` — **Client, Device & Topology** · `C`  
  `PJRT_Client_*`, device enumeration, addressable devices. _src: P-3-10, P-3-59_
- `pjrt/buffer-and-memory.md` — **Buffer ABI & Memory Layouts** · `C`  
  `PJRT_Buffer_*`, on-device layout, external refcounting. _src: P-3-126, P-3-10_
- `pjrt/executable-execution.md` — **Executable Loading & Execution** · `C`  
  compile → load → execute, serialization. _src: P-3-135, P-022_
- `pjrt/events-and-async.md` — **Events & Async Tracking** · `I`  
  `PJRT_Event` Await/OnReady/IsReady. _src: P-2-09, P-3-10_
- `pjrt/collectives-communicator.md` — **Collectives Communicator** · `C`  
  `CreateCommunicators`, cross-host handles. _src: P-3-69, P-3-10_
- `pjrt/dma-and-cross-host-recv.md` — **DMA Map & Cross-Host Receive** · `C`  
  The DMA-map slots + cross-host buffer receive. _src: P-3-10, P-3-471_
- `pjrt/callbacks.md` — **Callbacks & Pre-Fatal Hook** · `I`  
  Host-callback registration, the pre-fatal hook. _src: P-2-09_
- `pjrt/extension-chain.md` — **Extension Chain (17)** · `C`  
  The linked extension list and how it is walked. _src: P-3-106_
- `pjrt/ext-profiler.md` — **Extension: Profiler (type 1)** · `C`  
  `PLUGIN_Profiler_Api` 8 slots. _src: P-3-80_
- `pjrt/ext-topology-description.md` — **Extension: TopologyDescription (type 16)** · `C`  
  The TPU topology query. _src: P-3-59_
- `pjrt/ext-rawbuffer.md` — **Extension: RawBuffer (type 8)** · `C`  
  Raw device buffers. _src: P-3-126_
- `pjrt/ext-compile-phasecompile.md` — **Extension: Compile / PhaseCompile (type 9)** · `C`  
  The compile-options flow. _src: P-3-135_
- `pjrt/ext-remaining.md` — **Extensions: Layouts / Memories / Stream / FFI / …** · `C`  
  The remaining chain entries. _src: P-3-106_
- `pjrt/stream-executor-host-interpreter.md` — **StreamExecutor Host Interpreter** · `C`  
  The HloEvaluator host fallback. _src: P-3-134_
- `pjrt/stream-executor-pjrt-adapter.md` — **StreamExecutor → PJRT Adapter** · `C`  
  `xla::TpuClient` / the CommonPjRt framework. _src: P-3-190_

## Part III — Tpu C-Shim Layer (10)

The inner C ABI between PJRT and the runtime/compiler: the `Tpu*` C functions that wrap the C++ internals.

- `shim/overview.md` — **Overview** · `I`  
  The ~200-symbol Tpu* C surface and how PJRT calls it. _src: P-2-05, P-2-09_
- `shim/tpu-compiler-roster.md` — **TpuCompiler Roster** · `C`  
  The compile entry points. _src: P-3-135_
- `shim/tpu-executable-roster.md` — **TpuExecutable Roster** · `C`  
  Executable handle ops. _src: P-022_
- `shim/tpu-executor-roster.md` — **TpuExecutor Roster** · `C`  
  Stream/executor C functions. _src: P-3-190_
- `shim/tpu-transfer-manager.md` — **TpuTransferManager Roster** · `C`  
  The host↔device transfer C ABI. _src: P-3-190_
- `shim/tpu-program-roster.md` — **TpuProgram Roster** · `C`  
  Program object + serialization C ABI. _src: P-022_
- `shim/tpu-platform-and-topology.md` — **TpuPlatform & TpuNodeContext** · `C`  
  Platform init, node context. _src: P-3-59_
- `shim/tpu-topology.md` — **TpuTopology & TpuCoreLocation** · `C`  
  The topology/core-location C ABI. _src: P-3-59, P-3-278_
- `shim/tpu-embedding-engine.md` — **TpuEmbeddingEngine ABI** · `C`  
  The embedding/SparseCore C surface. _src: P-3-287_
- `shim/tpu-configuration-api.md` — **TpuConfigurationApi** · `C`  
  Runtime configuration entry points. _src: P-3-281_

## Part IV — Silicon & Hardware Codename Model (24)

The hardware the whole compiler is parameterized by. Read before V–VIII: the cost model, ISA, and MSA defaults all key off the per-codename constants defined here. **Canonical for: per-gen hardware constants** (referenced by VI, VII, IX).

#### Codename identity
- `targets/overview.md` — **Overview** · `I`  
  Six generations, three HAL families, the dual-enum trap. _src: P-3-262, P-2-08_
- `targets/tpu-version-codename-matrix.md` — **6-Codename Authoritative Reconciliation** · `C`  
  The 6-axis (enum / CHECK / chip_parts / PCI / namespace / marketing) cross-check; settles glc=v4, gfc=v5. _src: P-3-262, P-3-13, P-3-214, P-3-260_
- `targets/dual-enum-proto-vs-internal.md` — **Dual Enum (Proto vs Internal)** · `C`  
  `TpuVersionProto = internal + 1`; the "chip_parts v6" resolution. _src: P-3-262, P-2-08_
- `targets/pci-device-ids.md` — **PCI Device IDs** · `C`  
  Chip DIDs 0x00d1/0x00f2, header DIDs, rev-masks, IsGlc/IsGfc → device-type. _src: P-3-214, P-3-262_
- `targets/marketing-cloud-naming.md` — **Marketing / Cloud Naming** · `C`  
  v2…v6e/tpu7x; Trillium=v6e; Trillium/Ironwood NOT in the binary. _src: P-3-262_
- `targets/codename-superseded-labels.md` — **Superseded-Label Correction List** · `C`  
  The P-3-204 "v5p"/"Trillium" mislabels and the Ghostfish gloss. _src: P-3-262_

#### HAL families
- `targets/hal-families.md` — **HAL Families** · `C`  
  jxc/pxc/vxc factories; glc/gfc under gxc; per-family Register immediates. _src: P-3-196, P-2-08_
- `targets/hal-factory-override-matrix.md` — **HAL Factory Override Matrix** · `C`  
  Which methods each family overrides vs inherits. _src: P-3-05, P-2-35_
- `targets/tpuhal-class-hierarchy.md` — **TpuHal Class Hierarchy** · `C`  
  Only TpuHalHardware* exists; the {Jxc,Pxc,Vxc}HardwareImpl tree. _src: P-3-105_
- `targets/jxc-family.md` — **JXC Family (Jellyfish, Dragonfish)** · `C`  
  Identity, factory, PCI, cores. _src: P-3-187_
- `targets/pxc-family.md` — **PXC Family (Pufferfish)** · `C`  
  Identity, factory, BarnaCore binding. _src: P-3-195_
- `targets/vxc-family.md` — **VXC Family (Viperfish)** · `C`  
  Identity, std/lite variants, factory. _src: P-3-196_
- `targets/gxc-family.md` — **GXC Family (Ghostlite, 6acc60406)** · `C`  
  glc/gfc namespaces, the anonymous v5 codec, chip DIDs. _src: P-3-214, P-3-260_
- `targets/sub-core-taxonomy.md` — **Sub-Core Taxonomy (GFC/GLC/JXC/PXC/VFC/VLC)** · `C`  
  The sub-family encoder split. _src: P-3-262, P-2-08_

#### Per-codename hardware constants
- `targets/chip-parts-binarypb.md` — **chip_parts.binarypb Decode** · `C`  
  TpuChipPartsProto, the embedded v5/v7 blobs, the variant allow-list. _src: P-3-211, P-3-296, P-3-466_
- `targets/per-codename-hw-constants.md` — **Per-Codename Constant Table** · `C`  
  The master integer source consumed by the cost model and ISA. _src: P-3-288, P-3-251, P-3-358_
- `targets/tpu-topology-struct.md` — **TpuTopology Struct (Target+0x3b8)** · `C`  
  Per-codename chip geometry. _src: P-3-278_
- `targets/tpu-chip-config.md` — **TpuChipConfig** · `C`  
  LaneCount/SublaneCount/ChunksPerTile, VexMatrixWidth. _src: P-3-329, P-3-415_
- `targets/sparsecore-target-descriptor.md` — **SparseCoreTarget (Target+0x948)** · `C`  
  The per-codename MXU-contracting-depth map. _src: P-3-282, P-3-369_
- `targets/target-capability-bitfield.md` — **Target Capability Bitfield (Target+0x628)** · `C`  
  The 2 live capability bits across the whole struct. _src: P-3-445_
- `targets/kdevicetypeinfo-spec-constants.md` — **kDeviceTypeInfo Spec-Constants** · `C`  
  ~40 IEEE-754 doubles + the two DVFS frequency ladders. _src: P-3-420, P-3-429_
- `targets/accuracy-tables.md` — **Per-Gen Accuracy Tables** · `C`  
  Transcendental approximation accuracy driving precision decisions. _src: P-3-104_

#### Memory model primer (detailed allocators in Part X)
- `targets/memory-hierarchy.md` — **Memory Hierarchy** · `I`  
  HBM/VMEM/SMEM/CMEM/SFLAG tier model + the 17 MemorySpace values. _src: P-2-01, P-3-60_
- `targets/address-space-ids.md` — **Address-Space ID Table (AS0–AS9)** · `C`  
  Incl. the SparseCore fat-pointer AS7/8/9. _src: P-3-360, P-3-300, P-3-378_

## Part V — Compiler: Lowering & Optimization Passes (36)

The IR descent and the optimization passes. Silicon-parameterized (uses IV) but ISA-light: it lowers to LLO ops by name, not bits. Output is LLO IR; encoding is VI, cost/scheduling are VII/VIII.

#### Front-end and pipeline
- `compiler/overview.md` — **Overview** · `I`  
  `DeepseaCompilerBase::RunHloPasses`, compile phases 0–3. _src: P-2-03, P-3-09_
- `compiler/hlo-ingestion.md` — **HLO Ingestion** · `C`  
  StableHLO → HLO, the entry module. _src: P-002, P-3-90_
- `compiler/compile-phases.md` — **Compile Phases 0–3** · `C`  
  Phase0Stablehlo … Phase3Linking. _src: P-2-03_
- `compiler/compilation-cache.md` — **Compilation Cache** · `C`  
  Keys, fingerprints, the hit path. _src: P-022, P-3-135_
- `compiler/hlo-pass-registry.md` — **HLO Pass Registry** · `C`  
  The three pipeline containers + the xla_* flag-atlas binding. _src: P-3-267, P-3-193_
- `compiler/hlo-pre-passes.md` — **HLO Pre-Passes** · `C`  
  The passes that run before TPU lowering. _src: P-3-09_
- `compiler/sharding-propagation.md` — **Sharding Propagation** · `C`  
  GSPMD sharding inference. _src: P-3-43_
- `compiler/auto-sharding-spmd.md` — **Auto-Sharding / SPMD** · `C`  
  The partitioning passes. _src: P-3-43_
- `compiler/algebraic-simplifier.md` — **Algebraic Simplifier** · `I`  
  TPU-specific algebraic rewrites. _src: P-3-09, P-3-76_
- `compiler/dynamic-shape-support.md` — **Dynamic-Shape Support** · `C`  
  Bounded-dynamic model, dimension-size ops. _src: P-3-275_
- `compiler/optimization-barrier.md` — **Optimization Barrier** · `C`  
  Insertion, honouring, erasure. _src: P-3-274_
- `compiler/custom-call-lowering.md` — **Custom-Call Lowering & Registry** · `C`  
  The target catalog + the registration side. _src: P-3-51, P-3-202_

#### MLIR lowering chain
- `compiler/mhlo-xtile-tpu-lowering.md` — **MHLO → XTile → tpu** · `C`  
  The dialect-chain conversion. _src: P-3-125_
- `compiler/mosaic-overview.md` — **Mosaic Overview** · `C`  
  The kernel → tpu → LLO path. _src: P-3-141_
- `compiler/mosaic-layout-inference.md` — **Mosaic Layout Inference** · `C`  
  VectorLayoutInferer per-op rules. _src: P-3-255_
- `compiler/mosaic-vectorlayout.md` — **Mosaic VectorLayout** · `C`  
  The (sublane, lane) layout algebra. _src: P-3-236_
- `compiler/tpu-dialect-and-ops.md` — **The tpu MLIR Dialect** · `C`  
  Ops, attributes, the 157-op surface. _src: P-3-34_
- `compiler/tpu-to-llo-ods.md` — **tpu → LLO Lowering** · `C`  
  Per-op ODS operand/result/attr signatures, gain/staging registers. _src: P-3-229_
- `compiler/dialect-conversion-legalizer.md` — **DialectConversion Legalizer** · `C`  
  The depth-aware legalization cost. _src: P-3-283_
- `compiler/conversion-pattern-rewriter.md` — **ConversionPatternRewriter** · `C`  
  The rollback/rewrite-log engine, 1:N patterns. _src: P-3-291, P-3-377_
- `compiler/lower-to-mlo-dma-bridge.md` — **LowerToMlo DMA Bridge-Cast** · `C`  
  The two-stage DMA lowering. _src: P-3-299, P-3-385_
- `compiler/lower-to-sparsecore-llvm.md` — **LowerToSparseCoreLlvm** · `C`  
  Per-class rewrite bodies. _src: P-3-292, P-3-378_
- `compiler/sc-type-converter.md` — **SCTypeConverter** · `C`  
  The addr-space → `!llvm.ptr<addrspace>` map. _src: P-3-300, P-3-386_
- `compiler/mlir-op-model-contract.md` — **MLIR Op-Model Contract** · `C`  
  The 23-slot `Model<Op>` (6,050 instances). _src: P-3-276, P-3-367_
- `compiler/llvmtpu-intrinsic-catalog.md` — **LlvmTpu Intrinsic Catalog** · `C`  
  The 1,356 `tpu_*` backend intrinsics. _src: P-3-284, P-3-371_

#### Memory & layout optimization
- `compiler/msa-overview.md` — **MSA Overview** · `C`  
  The memory-space-assignment ILP pass. _src: P-3-38, P-3-220_
- `compiler/msa-allocate-segment.md` — **MSA AllocateSegment** · `C`  
  The allocation body + config proto. _src: P-3-220, P-3-311_
- `compiler/msa-per-version-defaults.md` — **MSA Per-Version Defaults** · `C`  
  Overlap ratios / outstanding-copy caps per gen. _src: P-3-243, P-3-351_
- `compiler/msa-reservation-hbm-policy.md` — **MSA Reservation & HBM Policy** · `C`  
  MsaReservationPolicy / HbmPolicy field dicts. _src: P-3-340, P-3-426_
- `compiler/layout-assignment.md` — **Layout Assignment** · `C`  
  FindMemoryMinimizingLayout weights + AddBackendConstraints. _src: P-3-68, P-3-227, P-3-313_

#### Fusion, dot/conv, tiling
- `compiler/fusion-patterns.md` — **Fusion Patterns** · `C`  
  The TPU-specific fusion class roster. _src: P-3-76_
- `compiler/fusion-cost-model.md` — **Fusion Cost Model** · `C`  
  Priority coefficients + the ShouldFuseImpl lambda set. _src: P-3-235_
- `compiler/dot-conv-mxu-lowering.md` — **Dot / Conv → MXU Lowering** · `C`  
  Tile-cost comparator + EmitFunctorEnum. _src: P-3-44, P-3-228_
- `compiler/raggeddot-convolution.md` — **RaggedDot → Windowed Convolution** · `C`  
  FromRaggedDot / DynamicSliceMaskedConv geometry. _src: P-3-339, P-3-346, P-3-354, P-3-440_
- `compiler/loop-tiling-unrolling.md` — **Loop Tiling & Unrolling** · `C`  
  TileKind rules + LoopConfig proto + the pipeline unroller. _src: P-3-88, P-3-237_
- `compiler/tpu-program-serialization.md` — **TpuProgram Serialization** · `C`  
  The final compiled-program container. _src: P-022, P-2-09_

## Part VI — TensorCore ISA & LLO Encoding (42)

The target representation: LLO IR and the per-generation VLIW bundle bit-layouts. Self-contained — read independently of the cost model. Bundle *packing* (LLO→bytes) is in VIII.

#### Foundations
- `isa/overview.md` — **Overview** · `I`  
  LLO IR: 462 opcodes, 17 memory spaces, the proto-descriptor source. _src: P-2-01, P-2-25_
- `isa/llo-opcode-enum.md` — **LloOpcode Enum (462)** · `C`  
  Categories: scalar / vector / EUP / reduction / MXU / transpose / DMA / sync / BarnaCore. _src: P-2-25, P-3-212_
- `isa/memory-space-enum.md` — **MemorySpace Enum (17)** · `C`  
  HBM/VMEM/SMEM/SFLAG/IMEM/CMEM/SC/HOST/PINNED. _src: P-2-01_
- `isa/bundle-model-overview.md` — **Bundle Model** · `I`  
  Per-gen sizes (41/51/64 B), slot counts, bundles-per-DMA-chunk. _src: P-2-04, P-2-34_
- `isa/instbits-master-db.md` — **InstBits Master DB** · `C`  
  The LLVM-MC per-opcode base bits; the default-all-zero / no-RELA finding. _src: P-3-212_
- `isa/instr-name-data.md` — **TPUInstrNameData / Descs / RegEncoding** · `C`  
  opcode→mnemonic, MCInstrDesc, the reg-encoding table. _src: P-3-212_
- `isa/llo-opcode-to-proto.md` — **LloOpcode ↔ Proto** · `C`  
  The 462-entry map + the inverse ProtoToLloOpcode. _src: P-3-212, P-3-07_
- `isa/mc-emitter.md` — **MC-Emitter (getBinaryCodeForInstr)** · `C`  
  The insertBits operand path + the HwMode select. _src: P-3-212_
- `isa/record-format.md` — **239-Bit Record Format** · `C`  
  The APInt record + per-operand insertBits(value, pos, width). _src: P-3-212_

#### Per-generation VLIW bundle
- `isa/bundle-jf-41b.md` — **Jellyfish 41-Byte Bundle** · `C`  
  The full slot map (EncodeBundleInternal). _src: P-3-203, P-3-384, P-3-470_
- `isa/bundle-df.md` — **Dragonfish Bundle** · `C`  
  The JF/DF shared 41-B layout deltas. _src: P-3-384, P-3-367_
- `isa/bundle-pf-51b.md` — **Pufferfish 51-Byte Bundle** · `C`  
  EncoderPf + the 5 shared load/store sub-encoders. _src: P-3-389, P-3-475_
- `isa/bundle-vf-64b.md` — **Viperfish 64-Byte Bundle** · `C`  
  Quad VALU, triple vload, ScalarSubBundle. _src: P-3-396, P-3-414_
- `isa/bundle-gl.md` — **Ghostlite Bundle** · `C`  
  The vector_misc slot; the glc encoder. _src: P-3-219, P-3-238_
- `isa/bundle-gf.md` — **6acc60406 Bundle** · `C`  
  The dedicated predicates slot; the gfc encoder. _src: P-3-219, P-3-238_

#### Per-slot encoding
- `isa/slot-mxu.md` — **MXU Slot** · `C`  
  matmul/matpush issue, latch fields, the per-gen MXU1 twin. _src: P-3-203, P-3-414, P-3-430_
- `isa/slot-vpu.md` — **VPU (Vector-ALU) Slot** · `C`  
  All generations. _src: P-3-204_
- `isa/slot-spu-scalar.md` — **SPU / Scalar Slot** · `C`  
  All generations. _src: P-3-205_
- `isa/slot-sequencer.md` — **Sequencer Slot** · `C`  
  branch/call/halt; the proto-bundle emitter path. _src: P-3-206, P-3-30_
- `isa/slot-memory-load.md` — **Memory-Load Slot** · `C`  
  All generations. _src: P-3-207_
- `isa/slot-memory-store.md` — **Memory-Store Slot** · `C`  
  All generations. _src: P-3-208_
- `isa/slot-predicate.md` — **Predicate-Register File** · `C`  
  The 7-bit field (4+1+2), count per gen. _src: P-3-209_
- `isa/slot-loop.md` — **Hardware Loop-Counter** · `C`  
  Encoding + count per gen. _src: P-3-210_
- `isa/slot-immediate.md` — **Immediate Slot** · `C`  
  Per-gen encoding-id → imm-slot bit position. _src: P-3-470_
- `isa/slot-eup-transcendental.md` — **EUP / Transcendental Slot** · `C`  
  VectorResult + VALU3 bit positions. _src: P-3-238, P-3-308_
- `isa/slot-matprep-iar-latch.md` — **Matprep / IAR / Latch** · `C`  
  Per-gen matprep WORD tables, the IAR bit-layout. _src: P-3-357, P-3-365, P-3-443_
- `isa/slot-vcreate-mask-mregister.md` — **vcreate_mask / M-Register** · `C`  
  End-inclusive range, per-gen field offsets, M0–M31. _src: P-3-464, P-3-469_
- `isa/slot-cmem-load-pf.md` — **cmem_load Slot (Pufferfish)** · `C`  
  The v4 constant-memory load path. _src: P-3-396_
- `isa/slot-sparsity-v5plus.md` — **Sparsity Slot (v5+)** · `O`  
  The structured-sparsity slot encoding. _src: #1092 (open)_

#### Encode / decode support
- `isa/v5plus-emitx-bit-positions.md` — **V5+ EmitX Absolute Bit Positions** · `C`  
  isa_emitter EmitX → BitCopy offsets (closes the InstBits gap). _src: P-3-219_
- `isa/isa-emitter-registry.md` — **IsaEmitter Registry** · `C`  
  The (TpuVersion, SequencerType) pair-key cell census. _src: P-3-402, P-3-488_
- `isa/decode-side-jf-pf.md` — **Decode-Side: JF / PF** · `C`  
  The disassembler inverse. _src: P-3-430_
- `isa/decode-side-vf-gxc.md` — **Decode-Side: VF / GXC** · `C`  
  The −20-bit twin decode. _src: P-3-419_
- `isa/nop-canonical.md` — **NOP / Unused-Slot Canonical Encoding** · `O`  
  Per-gen NOP templates. _src: #1096 (open)_
- `isa/tpumcimm-syimm32.md` — **TPUMCImm / SyImm32 Operand** · `C`  
  The MC immediate operand encoding + PatchOverlay. _src: P-3-465_
- `isa/archregno-numbering.md` — **ArchRegno Runtime Numbering** · `C`  
  ToArchRegno / InitRegisterNumbering per gen. _src: P-3-361, P-3-447_
- `isa/kisatable-data-sections.md` — **kIsaTable Data Sections** · `C`  
  The per-gen ISA-encoding split (no literal kIsaTable symbol). _src: P-3-07_
- `isa/sequencer-ops-per-gen.md` — **Sequencer Ops Per Gen × Type** · `C`  
  Control-flow op rosters. _src: P-3-30_
- `isa/resultfifo-archregister.md` — **ResultFifo & ArchRegister Enums** · `C`  
  25 result FIFOs + the 0x32-entry arch-register enum. _src: P-3-353, P-3-439_
- `isa/bias-quantization-helpers.md` — **Bias-Add & Quant/Dequant Helpers** · `C`  
  The TPU bias/quantization helper functions. _src: P-3-273_
- `isa/xlu-op-roster.md` — **XLU Op Roster** · `C`  
  Vsetperm/Vxpose/Vpermute/… opcode→factory table. _src: P-3-397, P-3-439, P-3-483_
- `isa/pack-unpack-precision.md` — **Pack/Unpack Precision** · `C`  
  VpackBf16 / VunpackCF32 bf16↔f32 conversion + segmented-reduce RPU. _src: P-3-404, P-3-490_

## Part VII — Cost & Latency Model (41)

What every instruction costs. The largest data surface in the binary (51 source files). Consumed by the schedulers in VIII; depends on the ISA (VI) and silicon constants (IV).

#### Core model
- `cost/overview.md` — **Overview** · `I`  
  The Performance / CycleTable / LatencyTable family architecture. _src: P-2-04, P-3-52_
- `cost/resource-enum.md` — **Resource Enum (23-slot)** · `C`  
  Names + SubsetOptions partition + TC-frequency wiring. _src: P-3-239_
- `cost/per-opcode-cycle-constants.md` — **Per-Opcode Cycle Constants** · `C`  
  Per-gen cycle-table dispatch. _src: P-3-52_
- `cost/normalized-computation-cost.md` — **NormalizedComputationCost** · `C`  
  opcode→weight switch + GetCyclesIfFused. _src: P-3-254, P-3-359_
- `cost/gethloresources-routing.md` — **GetHloResources Routing** · `C`  
  Per-op → ResourceVector sub-emitter routing. _src: P-3-285, P-3-372_
- `cost/tpu-hlo-cost-analysis.md` — **TpuHloCostAnalysis** · `C`  
  The flop-override surface. _src: P-3-285_
- `cost/bundle-aware-cost.md` — **Bundle-Aware Cost** · `C`  
  VLIW bundle-issue cost. _src: P-3-180_
- `cost/memory-bandwidth-latency-model.md` — **Memory Bandwidth & Latency Model** · `C`  
  The full cross-tier matrix per gen. _src: P-3-97_
- `cost/local-dma-bandwidth.md` — **LocalDmaBandwidth** · `C`  
  Per-gen matrix + the MemXfer-latency consumer. _src: P-3-304, P-3-382, P-3-390_

#### MXU latency (per-gen reservation matrices)
- `cost/mxu-latency-overview.md` — **MXU Latency Overview** · `C`  
  MxuResource enum + the reservation-matrix concept. _src: P-3-286, P-3-373_
- `cost/mxu-latency-jf-df.md` — **MXU Latency: JF / DF** · `C`  
  Oldest-gen reservation rows. _src: P-3-358, P-3-286_
- `cost/mxu-latency-pf.md` — **MXU Latency: PF** · `C`  
  Pufferfish reservation rows. _src: P-3-286_
- `cost/mxu-latency-vf.md` — **MXU Latency: VF** · `C`  
  Full Viperfish reservation matrix value-by-value. _src: P-3-443, P-3-451_
- `cost/mxu-latency-gl.md` — **MXU Latency: GL (Ghostlite)** · `C`  
  The GLM reservation rows. _src: P-3-286, P-3-407_
- `cost/mxu-latency-gf.md` — **MXU Latency: GF (6acc60406)** · `C`  
  res-remap 3/8 + fp8-fnuz. _src: P-3-316, P-3-321, P-3-402, P-3-450_
- `cost/matmul-mode-modifiers.md` — **MatmulMode & Modifiers** · `C`  
  16-ordinal naming, Matmul/MatpushModifier array<19> values. _src: P-3-247, P-3-290, P-3-435, P-3-450_
- `cost/mxu-opholdissues-stall.md` — **MxuOpHoldIssues Stall Recurrence** · `C`  
  The stall formula + the balancing gate. _src: P-3-294, P-3-380_

#### Performance grids (per-gen Instruction × Resource)
- `cost/performance-overview.md` — **Performance Family Overview** · `I`  
  The per-gen Performance<gen> variant model. _src: P-3-341_
- `cost/performance-jf-df.md` — **Performance: JF / DF** · `C`  
  Full latency array + I×R grid. _src: P-3-342, P-3-428, P-3-436_
- `cost/performance-pf.md` — **Performance: PF** · `C`  
  20-resource grid + the BarnaCore variant1. _src: P-3-341, P-3-427, P-3-429_
- `cost/performance-vf.md` — **Performance: VF** · `C`  
  The Viperfish grid. _src: P-3-341, P-3-427_
- `cost/performance-gl-ghperf.md` — **Performance: GL (GhPerf 476×31)** · `C`  
  The Ghostlite occupancy grid. _src: P-3-328, P-3-414, P-3-321_
- `cost/performance-gf-ghperf.md` — **Performance: GF (GhPerf 465×31)** · `C`  
  The 6acc60406 occupancy grid. _src: P-3-321, P-3-407, P-3-422_

#### CycleTable
- `cost/cycletable-family.md` — **CycleTable Family** · `C`  
  LatencyTable::Create(TpuVersion) factory dispatch. _src: P-3-388, P-3-474, P-3-480_
- `cost/jf-cycletable.md` — **JfCycleTable** · `C`  
  offsetLUT transcription + 7-column Resource naming. _src: P-3-350, P-3-436, P-3-459_
- `cost/vf-cycletable.md` — **VfCycleTable** · `C`  
  The 32-entry CT→(instr, res) dump + throughput bridge. _src: P-3-395, P-3-309, P-3-415_

#### EUP / transcendental latency
- `cost/eup-latency-overview.md` — **EUP Latency Overview** · `C`  
  The push→pop software-pipelining model. _src: P-3-308, P-3-394_
- `cost/eup-per-gen-integers.md` — **EUP Per-Gen Latency Integers** · `C`  
  PF/VF/GL push→pop integers. _src: P-3-326, P-3-412, P-3-420_
- `cost/eup-paynehanek.md` — **EUP Payne-Hanek Range Reduction** · `C`  
  The 2/π table. _src: P-3-326, P-3-412_
- `cost/eup-correction-coeffs.md` — **EUP Correction Coefficients** · `C`  
  Newton / VfastTwoSum per-function polynomials. _src: P-3-315, P-3-401_
- `cost/eup-lane-width-unpack.md` — **EUP Lane-Width / Unpack** · `C`  
  AluEpOpLowering unpack → compute → pack. _src: P-3-308, P-3-394_

#### XLU cost
- `cost/xlu-conflict-penalty.md` — **XLU Conflict-Penalty Table** · `C`  
  The non-MXU hazard table. _src: P-3-302, P-3-388_
- `cost/xlu-combine-sourcebus.md` — **XLU Combine / Source-Bus** · `C`  
  ComputeCombinablePairs + AssignSourceBus. _src: P-3-369, P-3-455, P-3-467_
- `cost/xlu-reemit-cost.md` — **XLU Reemit Cost** · `C`  
  Closed-form CyclesAddedByXluOperation + PerXluOperations. _src: P-3-390, P-3-476_
- `cost/xpose-reservation-latency.md` — **Transpose-Reservation Latency** · `C`  
  XposeXLUReservationLatency + VxposeMode. _src: P-3-310, P-3-396, P-3-403_

#### Conv / window cost
- `cost/window-description-cost.md` — **WindowDescription Byte-Cost** · `C`  
  The conv/DMA byte+throughput primitive. _src: P-3-309, P-3-301_
- `cost/convolution-cost-state.md` — **ConvolutionCostState** · `C`  
  Field map + VfCycleTable bridge. _src: P-3-301, P-3-293, P-3-387_
- `cost/reduce-window-pooling-cost.md` — **Reduce-Window / Pooling Cost** · `C`  
  RecordReduceWindowCycles. _src: P-3-301, P-3-254_

#### Misc cost
- `cost/learned-cost-model-client.md` — **Learned Cost-Model Client** · `C`  
  EmitterLearnedCostModelOptions + the wiring status. _src: P-3-57_
- `cost/cost-model-logging.md` — **Cost-Model Logging** · `C`  
  The impure AutoOr consumer + the float grammar. _src: P-3-325, P-3-411_
- `cost/iars-per-tensorcore.md` — **Consolidated Per-Gen Counts** · `C`  
  IarsPerTensorCore / mxu / xlu counts in one table. _src: P-3-373, P-3-459, P-3-474_

## Part VIII — Instruction Scheduling & Bundle Packing (14)

The algorithms that consume the cost model (VII) and emit ordered, packed bundles (VI). Smaller than VII by design — in this corpus the scheduling *algorithms* are a fraction of the cost *data*.

- `sched/overview.md` — **Overview** · `I`  
  Where scheduling sits between lowering and encoding. _src: P-3-221_
- `sched/latency-hiding-scheduler-core.md` — **LatencyHidingScheduler Core** · `C`  
  ScheduleComputation candidate loop + the async tracker. _src: P-3-221, P-3-312_
- `sched/lhs-post-layout-pre-fusion.md` — **LHS: post_layout_pre_fusion Variant** · `C`  
  The early scheduling variant. _src: P-3-31_
- `sched/lhs-post-layout.md` — **LHS: post_layout / final Variant** · `C`  
  The final scheduling variant. _src: P-3-96_
- `sched/lhs-ilp-variant.md` — **LHS: ILP Variant** · `C`  
  The two flag-gated code paths. _src: P-3-50_
- `sched/scheduler-resourcetype-model.md` — **ResourceType Taxonomy** · `C`  
  Per-resource model + AsyncTracker → core registry. _src: P-3-418, P-3-427, P-3-484_
- `sched/bundle-modulo-scheduling.md` — **Bundle Modulo Scheduling** · `C`  
  The II-search + software pipelining. _src: P-3-232_
- `sched/llo-bundle-packing.md` — **LLO → Bundle Packing** · `C`  
  The final-stage slot-assignment algorithm. _src: P-3-23_
- `sched/mxu-assignment-binpacker.md` — **MXU Assignment Bin-Packer** · `C`  
  AssignMxusForSequenceGroup. _src: P-3-302, P-3-388_
- `sched/latch-assignment-overrun.md` — **Latch Assignment & Overrun** · `C`  
  SetLatchIndices + the per-gen overrun handshake. _src: P-3-382, P-3-395, P-3-481_
- `sched/mxu-sequence-struct.md` — **MxuSequence / SequenceInfo** · `C`  
  The full record + set_mxu commit. _src: P-3-317, P-3-403_
- `sched/mrb-chain-allocator.md` — **MRB Chain Allocator** · `C`  
  The reservation-timeline algorithm + jitter model. _src: P-3-366, P-3-452_
- `sched/mrb-fifo-msr-placement.md` — **MRB FIFO / MSR Placement** · `C`  
  AllocateMrbEntriesAsFifo + BounceBetweenMsrs. _src: P-3-374, P-3-460_
- `sched/encoder-latch-serialization.md` — **Per-Gen Encoder Latch Serialization** · `C`  
  How latch fields serialize into the per-gen bundle. _src: P-3-403, P-3-489_

## Part IX — SparseCore & BarnaCore (45)

The embedding/sparse engine (SparseCore, v5+) and its retired predecessor (BarnaCore, v2–v4). Kept whole rather than sliced across the ISA/cost/scheduling axis. The collective-offload story lives in Part XIII.

#### SparseCore engines
- `sparsecore/overview.md` — **Overview** · `I`  
  SCS/TAC/TEC, the 2-sequencer (SCS+TEC) model. _src: P-2-02, P-3-32, P-3-311_
- `sparsecore/architecture.md` — **Architecture** · `C`  
  Engine roles + the embedding datapath. _src: P-3-58, P-3-287_
- `sparsecore/scs-engine.md` — **SCS (Scalar) Engine** · `C`  
  The scalar sequencer engine. _src: P-3-303, P-3-318_
- `sparsecore/tac-engine.md` — **TAC Engine** · `C`  
  The codec-only role. _src: P-3-311, P-3-303_
- `sparsecore/tec-engine.md` — **TEC (Vector) Engine** · `C`  
  The vector execution engine. _src: P-3-311, P-3-318_
- `sparsecore/bundle-slot-base-map.md` — **Per-Engine Bundle Slot-Base Map** · `C`  
  SCS/TAC/TEC byte offsets. _src: P-3-303, P-3-389_
- `sparsecore/region-to-sequencer-outliner.md` — **Region → Sequencer Outliner** · `C`  
  Partitions an SC computation into per-engine bundles. _src: P-3-303_
- `sparsecore/getsequencertype.md` — **getSequencerType** · `C`  
  Engine selection (SCS/TAC/TEC). _src: P-3-295, P-3-381_

#### SparseCore ISA
- `sparsecore/scalar-opcode-enum.md` — **Scalar Opcode Enum** · `C`  
  ScsScalarMisc / ScalarAlu0 / ScalarAlu1. _src: P-3-318, P-3-404_
- `sparsecore/vector-opcode-enum.md` — **Vector Opcode Enum** · `C`  
  VF 148-op / GF 257-op VectorAlu. _src: P-3-318, P-3-435_
- `sparsecore/oneslot-router.md` — **OneSlot Scalar Router** · `C`  
  ConsumeOneSlotInstruction jump table. _src: P-3-435, P-3-428_
- `sparsecore/vectorload-slot.md` — **VectorLoad Slot** · `C`  
  5-op field layout + the SourceOne seed enum. _src: P-3-330, P-3-416, P-3-440_
- `sparsecore/vectorstore-slot.md` — **VectorStore Slot** · `C`  
  The 33-entry type×mode scatter matrix. _src: P-3-323, P-3-409_
- `sparsecore/vectorextended-vex.md` — **VectorExtended / VEX** · `C`  
  The 53-op scan/sort/dedup family. _src: P-3-323, P-3-440_
- `sparsecore/vex-operand-port.md` — **VEX Operand-Port Binding** · `C`  
  FindAndEmitToUnusedPort (generation-specific). _src: P-3-446_
- `sparsecore/vex-mask-destport-subopcode.md` — **VEX Mask / Dest-Port / Sub-Opcode** · `C`  
  The bit0x104 mask field + the sub-opcode map. _src: P-3-457_
- `sparsecore/m-register-predicate.md` — **M-Register Predicate Word (M0–M31)** · `C`  
  Masked-scan inactive semantics. _src: P-3-464_
- `sparsecore/cbreg.md` — **CBREG Circular-Buffer Register** · `C`  
  Bit layout, addressing, wrap. _src: P-3-279_

#### SparseCore datapath (embeddings)
- `sparsecore/scan-datapath.md` — **Scan Datapath** · `C`  
  Mask consumption + ScanOp lowering. _src: P-3-473, P-3-559_
- `sparsecore/segmented-scan.md` — **Segmented Scan** · `C`  
  SegmentedScanOpLowering reduction_op switch. _src: P-3-426_
- `sparsecore/segmented-add-scan.md` — **Segmented-Add-Scan** · `C`  
  The newer-gen segment-reduce family. _src: P-3-423_
- `sparsecore/embedding-minibatching.md` — **Embedding Minibatching Decomposition** · `C`  
  The HLO layer above scan lowering. _src: P-3-433_
- `sparsecore/sample-combiner-emitter.md` — **SampleCombiner Emitter** · `C`  
  The inner-loop combiner emit. _src: P-3-444_
- `sparsecore/emit-valency-loop.md` — **EmitValencyLoop** · `C`  
  The per-sample valency loop. _src: P-3-456_
- `sparsecore/rank-and-permute-radixsort.md` — **RankAndPermute / RadixSort** · `C`  
  The sort/permute compute function. _src: P-3-474, P-3-560_
- `sparsecore/dedup-multiplicity.md` — **Dedup Multiplicity** · `C`  
  DuplicateCount→multiplicity + Uniquify inverse-permutation. _src: P-3-337, P-3-423_

#### SparseCore pointers & DMA
- `sparsecore/fat-pointers-as789.md` — **Fat Pointers (AS7/8/9)** · `C`  
  160/128/192-bit structured-pointer constructors. _src: P-3-360, P-3-446_
- `sparsecore/addrspacecast-isel.md` — **addrspacecast ISel** · `C`  
  The 16-cast from→to AS map. _src: P-3-368, P-3-454, P-3-461_
- `sparsecore/tile-id-cast.md` — **Tile-ID Cast** · `C`  
  On-tile 2-operand cast lowering. _src: P-3-446, P-3-469_
- `sparsecore/stream-gather-scatter.md` — **Stream Gather/Scatter** · `C`  
  The indirect-DMA descriptor format. _src: P-3-287, P-3-374_
- `sparsecore/indirect-vreg-stream.md` — **IndirectVregStream** · `C`  
  The VREG-loop form. _src: P-3-295, P-3-381_

#### SparseCore back-end
- `sparsecore/sc-backend-pipeline.md` — **SC Backend Pipeline** · `C`  
  RunPasses, all 12 passes, the MEGACORE barrier. _src: P-3-416_
- `sparsecore/sc-emitx-dispatcher.md` — **SC EmitX Dispatcher** · `C`  
  seq3/seq4/seq5 → EmitX jump tables. _src: P-3-410, P-3-417_
- `sparsecore/sc-core-selection.md` — **SC Core Selection** · `C`  
  SelectCores / GetAllowedCores policy. _src: P-3-391, P-3-477_
- `sparsecore/sc-queue-assignment-reservation.md` — **SC Queue Assignment & Reservation** · `C`  
  The resource→limit btree_map. _src: P-3-405, P-3-411, P-3-491_
- `sparsecore/getsparsecoreconfig.md` — **GetSparseCoreConfig** · `C`  
  The offload op-type enum source. _src: P-3-434_

#### SparseCore cross-cutting
- `sparsecore/sc-mxu-handshake.md` — **SC ↔ MXU Handshake** · `C`  
  The integration handshake. _src: P-3-58, P-3-125_
- `sparsecore/sparsecore-vs-neuron-matmultsparse.md` — **SparseCore vs Neuron MatmultSparse** · `I`  
  Cross-vendor comparison. _src: P-3-177_

#### BarnaCore (legacy v2–v4)
- `barnacore/overview.md` — **Overview** · `I`  
  The legacy embedding accelerator. _src: P-3-120, P-3-343_
- `barnacore/retirement.md` — **Retirement Evidence** · `C`  
  The BarnaCore → SparseCore transition. _src: P-3-120_
- `barnacore/bcs-scalar-isa.md` — **BCS Scalar0/Scalar1 ISA** · `C`  
  The 122-op control+memory ISA. _src: P-3-351, P-3-437_
- `barnacore/bcs-32byte-bundle.md` — **BCS 32-Byte Bundle** · `C`  
  InstBits_BarnaCorePxcHwMode + BcsMetadataAccessor. _src: P-3-359, P-3-445_
- `barnacore/merged-alu.md` — **Merged-ALU Bit Layout** · `C`  
  VectorResultDestination / BaseAddressEncoding. _src: P-3-378, P-3-464_
- `barnacore/jf-df-address-handler-bundle.md` — **JF/DF 16-Byte Address-Handler Bundle** · `C`  
  EncoderJf::EncodeBarnaCoreAddressHandler. _src: P-3-367, P-3-453_
- `barnacore/per-gen-perf-grids.md` — **Per-Gen BarnaCore Perf Grids** · `C`  
  PufferfishBarnaCorePerformance variant1. _src: P-3-343, P-3-429, P-3-334_

## Part X — On-Chip Memory & DMA (20)

The memory tiers' allocators and the DMA wire formats. The tier *model* is primed in IV; here are the allocator algorithms and descriptor byte layouts.

#### Memory tiers
- `memory/overview.md` — **Overview** · `I`  
  The five on-chip tiers + host memory. _src: P-3-97, P-2-01_
- `memory/hbm-allocator.md` — **HBM BestFit Allocator** · `C`  
  Coalescing rule + split/fragmentation policy. _src: P-3-24, P-3-216_
- `memory/hbm-dma-alignment.md` — **HBM DMA Alignment Contract** · `C`  
  The minimum-alignment rule. _src: P-3-24, P-3-157_
- `memory/vmem-allocator.md` — **VMEM Allocator** · `C`  
  Per-codename Config, alignment, MSA integration. _src: P-3-46_
- `memory/smem-scalar-memory.md` — **SMEM Scalar Memory** · `C`  
  Allocator, addressing, placement. _src: P-3-60_
- `memory/smem-register-window.md` — **SMEM Register-Window** · `C`  
  The mechanism + reconciliation with the SPU slot. _src: P-3-261, P-3-156_
- `memory/cmem-pool.md` — **CMEM Constant-Memory Pool** · `C`  
  Layout, allocator, placement (Pufferfish+). _src: P-3-71_
- `memory/sflag-protocol.md` — **SFLAG Sync-Flag Tier** · `C`  
  Allocator, Config, atomics, ordering. _src: P-3-89_
- `memory/tpu-buffer-layout.md` — **TpuBuffer Layout** · `C`  
  On-device buffer structure. _src: P-3-126_
- `memory/buffer-donation-aliasing.md` — **Buffer Donation & Aliasing** · `I`  
  DonateWithControlDependency. _src: P-2-09_
- `memory/on-device-compaction.md` — **On-Device Compaction** · `I`  
  The defrag path. _src: P-3-24_
- `memory/embedded-tcmalloc.md` — **Embedded tcmalloc** · `C`  
  Host-CPU allocator integration + sizing. _src: P-3-149_

#### DMA
- `dma/intra-chip-descriptor.md` — **Intra-Chip DMA Descriptor** · `C`  
  Format, tiling, tier-pair encoding. _src: P-3-157, P-3-155_
- `dma/tile-index-expansion.md` — **Tile-Index Expansion** · `C`  
  ExpandTiledMemRefs / expandTiledIndices algebra. _src: P-3-307, P-3-393_
- `dma/rolled-strided-general.md` — **Rolled / Strided / General Emitters** · `C`  
  issueRolled/Strided/General transfer bodies. _src: P-3-314, P-3-320, P-3-327_
- `dma/dma-parameters-selector.md` — **DmaParameters Selector** · `C`  
  Simple vs SingleStrided + dim-coalescing. _src: P-3-406, P-3-413_
- `dma/host-device-dma.md` — **Host↔Device DMA** · `C`  
  DeriveHostDmaTransfers + tags 6/7. _src: P-3-471_
- `dma/uhi-host-interface.md` — **UHI Host-Interface DMA** · `C`  
  The wire format + QueueId semantics. _src: P-3-477_
- `dma/oci-command-dma-id.md` — **OCI Command DMA-ID** · `C`  
  The 6 CmdDmaIdFromEntry helpers + the 3-header bands. _src: P-3-448, P-3-466_
- `dma/continuation-queue.md` — **Continuation Queue** · `C`  
  Memory model + runtime SFLAG protocol + the halt model. _src: P-3-452, P-3-461, P-3-468_

## Part XI — Runtime & Execution (11)

How a compiled program runs on a stream. Consumes the ISA (VI) and memory (X).

- `runtime/overview.md` — **Overview** · `I`  
  The execute path from PJRT down to the stream. _src: P-2-09, P-022_
- `runtime/execute-async-on-stream.md` — **ExecuteAsyncOnStream** · `C`  
  The core execution entry. _src: P-022, P-2-09_
- `runtime/load-program-enqueue.md` — **LoadProgramAndEnqueueToStream** · `C`  
  Program load + enqueue. _src: P-022_
- `runtime/stream-semantics.md` — **Stream Semantics & Dependencies** · `I`  
  Ordering, dependencies. _src: P-2-09_
- `runtime/infeed-outfeed.md` — **Infeed / Outfeed Queues** · `I`  
  The host-feed queues. _src: P-2-09_
- `runtime/host-callbacks.md` — **Host Callbacks** · `I`  
  Callback dispatch during execution. _src: P-2-09_
- `runtime/completion-loop.md` — **Completion Loop & AsyncTrackingEvent** · `I`  
  Completion tracking. _src: P-2-09_
- `runtime/allocator-integration.md` — **PJRT Client Allocator Integration** · `C`  
  Device-memory allocation flow. _src: P-3-199_
- `runtime/error-templates.md` — **Error/Status String Templates** · `C`  
  The printf-format + StrFormat catalog. _src: P-3-201_
- `runtime/hint-strings.md` — **User-Facing Hint Strings** · `C`  
  Actionable diagnostics (flag-suggestion / doc-link / capacity). _src: P-3-266_
- `runtime/internal-pass-names.md` — **Internal Pass-Name Catalog** · `C`  
  HLO + MLIR + pipeline phase names. _src: P-3-267_

## Part XII — Interconnect & Routing (30)

The physical fabric and how packets route across it. The geometric substrate (twisted torus) that on-pod collectives (XIII) build on.

#### ICI fabric
- `ici/overview.md` — **Overview** · `I`  
  The inter-chip interconnect model. _src: P-3-33, P-3-53_
- `ici/link-bringup.md` — **Link Bring-Up Sequence** · `C`  
  The link initialization sequence. _src: P-3-33_
- `ici/topology-discovery.md` — **Topology Discovery** · `C`  
  Master::DiscoverTopology end-to-end. _src: P-3-53_
- `ici/dma-descriptor.md` — **Cross-Chip DMA Descriptor** · `C`  
  The ICI DMA wire format. _src: P-3-156_
- `ici/all-reduce-primitive.md` — **ICI All-Reduce Primitive** · `C`  
  The step-generation primitive. _src: P-3-69, P-3-215_
- `ici/failure-recovery.md` — **Failure Modes & Recovery** · `C`  
  The recovery flow. _src: P-3-167_
- `ici/vc-balance-allocation.md` — **VC-Balance Allocation** · `C`  
  Deadlock-free virtual-channel allocation. _src: P-3-355, P-3-441_

#### Routing
- `routing/overview.md` — **Overview** · `I`  
  The route-generation → route-cache → emission pipeline. _src: P-3-224_
- `routing/randomized-toroidal-wildfirst.md` — **RandomizedToroidalWildFirstPaths** · `C`  
  The path generator. _src: P-3-224_
- `routing/route-table-generation.md` — **Route-Table Generation** · `C`  
  physmap + GetPhysicalToLogicalMapping3D. _src: P-3-347, P-3-433_
- `routing/get-static-path.md` — **GetStaticPath & Multipod** · `C`  
  Inter-pod route emission. _src: P-3-355, P-3-441_
- `routing/toroidal-route-cache.md` — **ToroidalRouteCache** · `C`  
  The 85-file binarypb decode + per-codename split. _src: P-3-253, P-3-355_
- `routing/route-cache-decompress.md` — **Route-Cache Decompress** · `C`  
  CompressedToroidalRouteCache proto→map. _src: P-3-371, P-3-457_
- `routing/route-cache-dedup.md` — **Route-Cache Dedup** · `C`  
  RouteCacheDeduplicator key + type dispatch. _src: P-3-371, P-3-457_
- `routing/route-cache-codec.md` — **Route-Cache Codec** · `C`  
  BitEncoder / DecodePathFromBits / TopologyRotationHelper. _src: P-3-376, P-3-386, P-3-462, P-3-472_
- `routing/create-routing-schedule.md` — **CreateRoutingSchedule Solver** · `C`  
  The priority-queue hop-assignment + PointerType enum. _src: P-3-455, P-3-463_
- `routing/net-router-pipeline.md` — **net_router Pipeline** · `C`  
  The software-pipeline callbacks + Transfer construction. _src: P-3-463, P-3-449_
- `routing/unicast-route-emission.md` — **Unicast Route Emission** · `C`  
  The layer above DmaDestinationRoutingTableEntryMapper. _src: P-3-347, P-3-449_
- `routing/get-distances.md` — **GetDistances** · `C`  
  The nK twisted-torus distance metric. _src: P-3-363, P-3-449_

#### Twisted torus geometry
- `twist/overview.md` — **Overview** · `I`  
  The twisted-torus topology and why it exists. _src: P-3-331_
- `twist/buildstrategy.md` — **TwistedTorusND::BuildStrategy** · `C`  
  Phase order + RingLocation construction. _src: P-3-331, P-3-417_
- `twist/twist-predicate-orientation.md` — **Twist Predicate & Orientation** · `C`  
  Orientation enum 4/5/6 negative-axis folding. _src: P-3-410, P-3-417_
- `twist/replica-group-2phase.md` — **2-Phase Replica-Group Construction** · `C`  
  The reduce-scatter / all-gather group construction. _src: P-3-338, P-3-424_
- `twist/shape-folds.md` — **Shape Folds** · `C`  
  K_K_2K / K_2K_2K / K_2K_NK twist-shape cases. _src: P-3-434, P-3-348_
- `twist/get-replica-pair-3d.md` — **GetReplicaPair3DOnTwistedTorus** · `C`  
  The coordinate fold. _src: P-3-348_
- `twist/megacore-even-odd.md` — **Megacore Even/Odd Split** · `C`  
  The split rationale. _src: P-3-348, P-3-434_
- `twist/get-tiebreak.md` — **GetTiebreak** · `C`  
  The literal-nK routing tiebreak. _src: P-3-356, P-3-442_
- `twist/sc-side-twist.md` — **SC-Side Twist** · `C`  
  GetPhase0/1Cores + EstimatePhysicalLinksUsed. _src: P-3-364, P-3-450_

#### ICR node-fabric
- `routing/icr-node-fabric-dma.md` — **ICR Node-Fabric DMA Bands** · `C`  
  trace_point_ids 48/50/51/91 timeline source. _src: P-3-460, P-3-466_
- `routing/nf-descriptor.md` — **nf_descriptor (27-field)** · `C`  
  The Node-Fabric DMA descriptor record. _src: P-3-439_

## Part XIII — On-Pod Collectives & Barriers (30)

How a collective is decomposed, offloaded, and synchronized over the fabric (XII). The SparseCore-offload path bridges to IX.

#### Collective algorithms
- `collectives/overview.md` — **Overview** · `I` *(stub exists)*  
  The strategy picker and the algorithm family. _src: P-3-69, P-3-319_
- `collectives/strategy-nd-picker.md` — **SelectNDStrategy** · `C`  
  The collective-algorithm picker + degraded-axis handling. _src: P-3-319, P-3-405_
- `collectives/binomial-recursive-doubling.md` — **Binomial / Recursive-Doubling** · `C`  
  The per-rank partner schedule. _src: P-3-438_
- `collectives/allreduce-hierarchical-pincer.md` — **AllReduce Hierarchical / Pincer** · `C`  
  The multi-phase 0x101 path + pincer fusion. _src: P-3-377, P-3-463, P-3-438_
- `collectives/allgather-nd-ring.md` — **AllGather ND-Ring** · `C`  
  GetShardIndex/GetOffset + the 2D/3D selector. _src: P-3-472, P-3-467_
- `collectives/alltoall-tables.md` — **AllToAll Tables** · `C`  
  GenerateAllToAllTables → ConstantMapper. _src: P-3-467_
- `collectives/reduce-scatter.md` — **ReduceScatter** · `C`  
  The reduce-scatter decomposition. _src: P-3-338, P-3-377_
- `collectives/constant-mapper.md` — **ConstantMapper** · `C`  
  Compile-time collective constant-pool tags + SMEM reads. _src: P-3-442_
- `collectives/degraded-axis.md` — **Degraded-Axis Ingest** · `C`  
  TpuDegradedAxesProto fault-tolerant path. _src: P-3-324, P-3-410_

#### SparseCore-offload collectives
- `collectives/sc-offload-config-builder.md` — **SC-Offload Config Builder** · `C`  
  ConstructConfigForCollectiveUniDirNDGroups. _src: P-3-372, P-3-458_
- `collectives/hierarchical-kind.md` — **HierarchicalKind** · `C`  
  AllGather/AllReduce/ReduceScatter OffloadConfig structs. _src: P-3-458_
- `collectives/tensor-split-ndplane.md` — **Tensor-Split / ND-Plane** · `C`  
  tensor_split_factor / NumScOffloadDevices + NDPlaneInfo. _src: P-3-379, P-3-465_
- `collectives/physical-core-placement.md` — **Physical-Core Placement** · `C`  
  physical_core_indices per-color mapping. _src: P-3-385, P-3-471_
- `collectives/sc-core-selection-offload.md` — **SC Core-Selection (Offload)** · `C`  
  The assignment cost + resource model. _src: P-3-391, P-3-398_
- `collectives/get-remote-memref.md` — **get_remote_memref** · `C`  
  Cross-chip address composition. _src: P-3-335, P-3-344, P-3-430_
- `collectives/start-remote-dma.md` — **StartRemoteDma** · `C`  
  The all-to-all producer + SubsliceToFullSliceGlobalCoreId. _src: P-3-352, P-3-438_

#### SFLAG & barriers
- `barrier/overview.md` — **Overview** · `I`  
  The sync-flag-based barrier model. _src: P-3-89, P-3-451_
- `barrier/special-purpose-sync-flags.md` — **SpecialPurposeSyncFlags** · `C`  
  The FromProto runtime sink + overlay semantics. _src: P-3-451, P-3-537_
- `barrier/per-codename-compiler-reserved.md` — **Per-Codename compiler_reserved SFLAG** · `C`  
  The literal {base, count} integers. _src: P-3-441_
- `barrier/barrier-coloring.md` — **BarrierColoring** · `C`  
  The greedy graph-coloring engine. _src: P-3-401, P-3-487_
- `barrier/barrier-to-sflag-binding.md` — **Barrier → SFLAG Number Binding** · `C`  
  The compiler-barrier → hardware-SFLAG number map. _src: P-3-409_
- `barrier/global-barrier-window.md` — **Global-Barrier SFLAG Window** · `C`  
  GetGlobalBarrierSyncFlagNumber consumers. _src: P-3-422_
- `barrier/replica-barrier.md` — **Replica (type-2) Barrier** · `C`  
  The REPLICA barrier lowering. _src: P-3-422_
- `barrier/tensorcore-barrier.md` — **TensorCore Barrier** · `C`  
  InitializeOnScs lookup-callback. _src: P-3-392, P-3-478_
- `barrier/tree-barrier-vsync.md` — **Tree-Barrier Vsync** · `C`  
  net_util actuation + InfoTable indexing. _src: P-3-425_
- `barrier/infer-barrier-config.md` — **InferBarrierConfig** · `C`  
  The per-gen SFLAG map source. _src: P-3-432, P-3-459_
- `barrier/remote-sflag-encoders.md` — **Per-Gen Remote-SFLAG Encoders** · `C`  
  GetRemoteSyncFlagEncoderRegistry + chip-id map. _src: P-3-447, P-3-453_

#### Higher-level
- `collectives/megacore-fusion.md` — **Megacore Fusion** · `I` *(stub exists)*  
  The megacore collective fusion. _src: P-3-424, P-3-348_
- `collectives/fp8-quantized-collective.md` — **FP8 Quantized Collective** · `C`  
  The quantized-collective dispatch path. _src: #1339_
- `collectives/spmd-link-count-cost.md` — **SPMD Link-Count Cost** · `C`  
  The link-count divisor + full collective cost-formula set. _src: P-3-312, P-3-398_

## Part XIV — Megascale (Multi-Host / DCN) (21)

The data-center-network layer above on-pod ICI: cross-host rendezvous, fleet metadata, and error aggregation. Seventeen of these pages are already written.

- `megascale/overview.md` — **Overview** · `I`  
  DCN vs ICI; what Megascale orchestrates. _src: P-3-70, P-3-176_
- `megascale/bootstrap/overview.md` — **Bootstrap: Overview** · `C` *(written)*  
  The rendezvous overview. _src: P-3-70_
- `megascale/bootstrap/coordinator-election.md` — **Bootstrap: Coordinator Election** · `C` *(written)*  
  The coordinator-election logic. _src: P-3-70_
- `megascale/bootstrap/worker-registration.md` — **Bootstrap: Worker Registration** · `C` *(written)*  
  Worker registration with the coordinator. _src: P-3-70_
- `megascale/bootstrap/topology-exchange.md` — **Bootstrap: Topology Exchange** · `C` *(written)*  
  The cross-host topology exchange. _src: P-3-70_
- `megascale/bootstrap/ici-handoff.md` — **Bootstrap: ICI Handoff** · `C` *(written)*  
  Handoff to the ICI fabric. _src: P-3-70_
- `megascale/bootstrap/convergence.md` — **Bootstrap: Convergence** · `C` *(written)*  
  Convergence detection. _src: P-3-70_
- `megascale/bootstrap/failure-handling.md` — **Bootstrap: Failure Handling** · `C` *(written)*  
  Bootstrap failure handling. _src: P-3-70_
- `megascale/bootstrap/tpunetd-relationship.md` — **Bootstrap: tpunetd Relationship** · `C` *(written)*  
  Relationship to the tpunetd daemon. _src: P-3-70, P-3-11_
- `megascale/fleet-metadata/overview.md` — **Fleet Metadata: Overview** · `C` *(written)*  
  The fleet-metadata schema overview. _src: P-3-176_
- `megascale/fleet-metadata/topology-model.md` — **Fleet: Topology Model** · `C` *(written)*  
  The fleet topology model. _src: P-3-176_
- `megascale/fleet-metadata/host-identity.md` — **Fleet: Host Identity** · `C` *(written)*  
  Host identity fields. _src: P-3-176_
- `megascale/fleet-metadata/global-addressing.md` — **Fleet: Global Addressing** · `C` *(written)*  
  Global addressing scheme. _src: P-3-176_
- `megascale/fleet-metadata/ici-vs-dcn.md` — **Fleet: ICI vs DCN** · `C` *(written)*  
  The ICI/DCN distinction. _src: P-3-176_
- `megascale/fleet-metadata/slice-shape.md` — **Fleet: Slice Shape** · `C` *(written)*  
  Slice-shape encoding. _src: P-3-176_
- `megascale/fleet-metadata/bootstrap-exchange.md` — **Fleet: Bootstrap Exchange** · `C` *(written)*  
  The bootstrap data exchange. _src: P-3-176_
- `megascale/fleet-metadata/barrier-error-usage.md` — **Fleet: Barrier & Error Usage** · `C` *(written)*  
  How fleet metadata feeds barriers/errors. _src: P-3-176_
- `megascale/fleet-metadata/field-decode.md` — **Fleet: Field Decode** · `C` *(written)*  
  Field-by-field decode. _src: P-3-176_
- `megascale/cross-host-barrier.md` — **Cross-Host Barrier** · `C`  
  The Megascale barrier primitive. _src: P-3-148_
- `megascale/error-aggregator.md` — **ErrorAggregator** · `C`  
  Wire format, scope, retention, dedup. _src: P-3-45, P-3-182_
- `megascale/tpunetd-protocol.md` — **tpunetd Protocol** · `C`  
  The daemon protocol. _src: P-3-11_

## Part XV — Profiling & Telemetry (22)

How libtpu emits XPlane traces and hardware telemetry. Per-generation trace payloads have distinct on-wire formats.

- `profiling/overview.md` — **Overview** · `I` *(stub exists)*  
  XPlane, the trace pipeline, the codec families. _src: P-2-32_
- `profiling/tpu-profiler-abi.md` — **TpuProfiler ABI** · `C`  
  The profiler C surface. _src: P-3-80_
- `profiling/pjrt-profiler-extension.md` — **PJRT_Profiler Extension** · `C`  
  PLUGIN_Profiler_Api. _src: P-3-80_
- `profiling/xplane-xstat-traceme.md` — **XPlane / XStat / TraceMe Emission** · `C`  
  The emit path. _src: P-2-32, P-3-408_
- `profiling/tpu-telemetry-proto.md` — **tpu_telemetry.proto** · `C`  
  Field-by-field decode. _src: P-3-133_
- `profiling/xevent-metadata-ids.md` — **XEvent Metadata IDs** · `C`  
  The profiler event catalog. _src: P-3-150_
- `profiling/xstat-metadata-ids.md` — **XStat Metadata IDs** · `C`  
  The stat/attribute catalog. _src: P-3-158_
- `profiling/trace-entries-coder.md` — **TraceEntriesCoder** · `C`  
  The fixed-width device-trace codec. _src: P-3-393_
- `profiling/riegeli-trace-container.md` — **riegeli Trace Container** · `C`  
  Framing + timebase clock-domain conversion. _src: P-3-485, P-3-399_
- `profiling/per-devicetype-struct.md` — **Per-DeviceType Profiler Struct** · `C`  
  The 0x448-byte master device table. _src: P-3-415_
- `profiling/kdevicetypeinfo-producer-readers.md` — **kDeviceTypeInfo Producer / Readers** · `C`  
  The roofline readers. _src: P-3-429_
- `profiling/tracepoints-master-registry.md` — **TracePoints Master Registry** · `C`  
  trace_point_id → {family, subscriber}. _src: P-3-413_
- `profiling/trace-entry-to-xevent.md` — **TraceEntry → XEvent/XStat** · `C`  
  The TpuXLineBuilder last hop. _src: P-3-408, P-3-494_
- `profiling/task-proto.md` — **Task Proto** · `C`  
  Device clock-rates + chip/host identity + GtcSpan offset. _src: P-3-406, P-3-492_
- `profiling/payload-jxc-legacy.md` — **Payload: jxc Legacy** · `C`  
  The 16-bit trace_point_id namespace. _src: P-3-421_
- `profiling/payload-vfc-vlc-gfc.md` — **Payload: vfc / vlc / gfc** · `C`  
  Per-gen payload field maps. _src: P-3-407, P-3-493_
- `profiling/payload-sc-band.md` — **Payload: SparseCore Band** · `C`  
  SCS/TEC/TAC profiler payloads. _src: P-3-412_
- `profiling/payload-uhi-oci-ici-dma.md` — **Payload: UHI/OCI/ICI/DMA** · `C`  
  The high-value trace-point bit-decodes. _src: P-3-400, P-3-486_
- `profiling/icr-dma-timeline-band.md` — **ICR DMA-Timeline Band** · `C`  
  The 48/50/51/91 rendering. _src: P-3-460, P-3-466_
- `profiling/jxc-dma-hbmmux-brnperf.md` — **jxc DMA / HbmMux / brn_perf** · `C`  
  The jellyfish DMA bands. _src: P-3-431_
- `profiling/v7x-perf-counters.md` — **v7x Perf-Counters** · `C`  
  The hardware-counter name resolver + firmware/DVFS telemetry. _src: P-3-436_
- `profiling/dma-endpoint-rendering.md` — **DMA Endpoint Rendering** · `C`  
  SrcMem/DstMem/Opcode enums + XEvent rendering. _src: P-3-466, P-3-454_

## Part XVI — Configuration & Compile Knobs (16)

Every flag, env var, and compile knob, and how they resolve. The TpuCompilationEnvironment is the 1,121-field master config object.

- `config/overview.md` — **Overview** · `I` *(stub exists)*  
  The flag/knob/env taxonomy. _src: P-3-281_
- `config/xla-flag-atlas.md` — **xla_* Flag Atlas** · `C`  
  The full option-name catalog. _src: P-3-193_
- `config/flag-families.md` — **Flag Families** · `C`  
  jf/pf/vf/gf/sc/msa/lhs prefixes. _src: P-3-193_
- `config/env-vars.md` — **Environment Variables** · `I`  
  The env-var catalog. _src: W005_
- `config/tpu-compilation-environment.md` — **TpuCompilationEnvironment (1121 fields)** · `C`  
  Overview + DefaultDebugOptions. _src: P-3-281, P-3-368_
- `config/tce-field-dictionary-a.md` — **TCE Field Dictionary (A)** · `C`  
  Fields part 1. _src: P-3-281_
- `config/tce-field-dictionary-b.md` — **TCE Field Dictionary (B)** · `C`  
  Fields part 2. _src: P-3-281_
- `config/tce-field-offsets-defaults.md` — **TCE Field-Offsets & Flag Defaults** · `C`  
  field#→offset + ABSL-flag defaults. _src: P-3-289, P-3-375, P-3-425_
- `config/debugoptions-proto.md` — **xla.DebugOptions (290 fields)** · `C`  
  The complete proto table. _src: P-3-280, P-3-346_
- `config/default-debugoptions.md` — **Default DebugOptions** · `C`  
  The effective defaults. _src: P-3-368_
- `config/autoproto-autoor-resolution.md` — **AutoProto / AutoOr Resolution** · `C`  
  The per-knob AUTO resolver bodies. _src: P-3-297, P-3-383_
- `config/autoor-parse-grammar.md` — **AutoOr Parse Grammar** · `C`  
  ParseAutoOrFromString (the XLA_FLAGS ingest). _src: P-3-305, P-3-391_
- `config/autoor-unparse.md` — **AutoOr Unparse** · `C`  
  AbslUnparseFlag reverse-text. _src: P-3-313, P-3-399_
- `config/autoproto-message-arms.md` — **AutoProto Message-Arms** · `C`  
  The 12 message-arm sub-message defaults + SET path. _src: P-3-306, P-3-333, P-3-419, P-3-392_
- `config/registry-mediated-flags.md` — **Registry-Mediated Flags** · `C`  
  enable_lem_scheduler / explicit_evict_memory_limit_kib. _src: P-3-332, P-3-418_
- `config/flag-prefix-dispatch.md` — **TpuVersion-Aware Flag-Prefix Dispatch** · `O`  
  The per-gen flag-prefix routing. _src: P-3-13, #1171 (open)_

## Part XVII — Appendices (20)

Reference tables, the source-traceability index, and the open-frontier register.

- `appendix/llo-opcode-table.md` — **LloOpcode Table (462)** · `C`  
  The full enum with categories. _src: P-2-25, P-3-212_
- `appendix/llvmtpu-intrinsic-table.md` — **LlvmTpu Intrinsic Table (1356)** · `C`  
  The full `tpu_*` intrinsic list. _src: P-3-284, P-3-371_
- `appendix/memory-space-table.md` — **MemorySpace Table (17)** · `C`  
  The full enumeration. _src: P-2-01_
- `appendix/dispatch-table-taxonomy-full.md` — **Dispatch-Table Taxonomy (full)** · `C`  
  All 17 classes + the 40,313-table TSV. _src: P-3-112, P-2-06_
- `appendix/filewrapper-toc-catalog.md` — **filewrapper_toc Catalog (61)** · `C`  
  Every embedded runtime resource. _src: P-3-98, P-2-07_
- `appendix/protodesc-cold-catalog.md` — **protodesc_cold Catalog (760)** · `C`  
  Every embedded FileDescriptorProto. _src: P-3-12, P-2-07_
- `appendix/rtti-namespace-census.md` — **RTTI Namespace Census** · `C`  
  The full 160,566-entry breakdown. _src: P-3-127, P-3-265_
- `appendix/reconstructed-proto-index.md` — **Reconstructed-Proto Index** · `C`  
  Every proto recovered from the descriptor pool. _src: P-3-90, P-3-12_
- `appendix/error-status-codes.md` — **Error / Status Codes** · `C`  
  The status-code catalog. _src: P-3-201_
- `appendix/flag-catalog-full.md` — **Flag Catalog (full TSV)** · `C`  
  The machine-readable flag list. _src: P-3-193_
- `appendix/symbol-namespace-index.md` — **Symbol Namespace Index** · `I`  
  The namespace population map. _src: W014, W003_
- `appendix/per-gen-comparison-matrix.md` — **Per-Gen Master Comparison Matrix** · `C`  
  bundle/lanes/MXU/XLU/IAR/SFLAG/EUP/DID, six gens, one page. _src: P-3-262, P-3-329, P-3-373, P-3-441_
- `appendix/evidence-anchor-index.md` — **Evidence-Anchor Index** · `I`  
  page → source-findings file → binary VA, the full traceability map. _src: (this corpus)_
- `appendix/source-corpus-map.md` — **Source-Corpus Map** · `I`  
  The P-2/P-3/W raw-findings file → part assignment. _src: W001, W034_
- `appendix/open-frontier-register.md` — **Open-Frontier Register** · `I`  
  What is NOT yet recovered (P-3-478..482; sparsity slot #1092; NOP canonical #1096; flag-prefix #1171). _src: P-3-477_
- `appendix/cross-reference-graph.md` — **Cross-Reference Dependency Graph** · `I`  
  The inter-page dependency web. _src: G001–G003_
- `appendix/binary-layout.md` — **Binary Layout Reference** · `C`  
  Segments, anchor symbols, scale vs ptxas/nvlink/cicc. _src: W023, W027_
- `appendix/methodology-deep.md` — **Methodology (Deep)** · `I`  
  Extraction pipeline, FLIRT, sidecar inventory. _src: W001, W031, P-3-22_
- `appendix/glossary-extended.md` — **Extended Glossary** · `I`  
  Every acronym + internal class name. _src: W014_
- `appendix/changelog.md` — **Changelog** · `I`  
  Book revision history vs binary version.

---

## Appendix highlights

Two appendix pages are the connective tissue that make this book auditable and the per-generation story coherent:

- **Evidence-Anchor Index** (`appendix/evidence-anchor-index.md`) — the full page → source-findings file (`P-2-*`/`P-3-*`/`W*`) → binary VA mapping. Every claim in the book is traceable back through this index to a specific function address and a specific raw-findings file. This is what separates a reimplementation reference from a blog post.
- **Per-Gen Master Comparison Matrix** (`appendix/per-gen-comparison-matrix.md`) — one page, six generations (Jellyfish/Dragonfish/Pufferfish/Viperfish/Ghostlite/6acc60406) × every per-generation constant: bundle size, lane/sublane count, MXU dimensions, XLU/IAR counts, SFLAG base/count, EUP push→pop latency, chip DID, PCI device IDs. Ties together the per-gen material otherwise distributed across Parts IV, VI, and VII.

## Open frontier

Pages graded `O` are not yet backed by a completed raw-findings file: the sparsity slot encoding (task #1092), per-gen NOP canonical encoding (#1096), TpuVersion-aware flag-prefix dispatch (#1171), and the cmem-load/sparsity edges feeding from P-3-478..482. The [Open-Frontier Register](appendix/open-frontier-register.md) tracks these. Everything else is backed by a `C` (confirmed) or `I` (inferred) raw-findings file.
