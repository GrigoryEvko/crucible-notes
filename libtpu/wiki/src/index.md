# libtpu Internals — Reverse-Engineering Reference

> **Status**: 426 pages across 18 parts · **Primary binary**: `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` — 781,691,048 B, x86-64 ELF64 DYN, **not stripped**, build-id `89edbbe81c5b328a958fe628a9f2207d` · **Secondary**: `sdk.so` (94,732 functions)

## What this reference is

A reimplementation-grade reverse-engineering reference for **Google's `libtpu.so`** — the PJRT plugin that exposes Cloud TPU hardware to JAX, PyTorch/XLA, and TensorFlow. It is the functional equivalent of NVIDIA's `libcuda.so` + `libnvrtc.so` + the device-specific half of `nvcc`/`ptxas`, compressed into a single 745 MB monolithic shared object that statically links the entire XLA compiler, every TPU MLIR dialect, the per-generation LLVM backends, oneDNN, tcmalloc, Abseil, gRPC, protobuf, Eigen, the TPU runtime, the device-driver shim, and the ICI/DCN fabric stack.

Everything here was reconstructed **purely from static analysis of the binary** — `objdump`, `nm`, `readelf -rW`, raw byte reads, and `protoc --decode_raw` of carved descriptors. The binary ships unstripped — 884,832 disassembler-recovered functions, 881,784 of them (99.66 %) carrying a real symbol name — which is why reconstruction reached byte-exact / reimplementation grade across most of the surface.

## Why it is hard

- **884,832 functions** in the analysis database (the per-function artifact directories hold a slightly higher 884,843 files, counting thunk/alias/data-stub entries; function *counts* cite 884,832, see [Binary Forensics Overview](forensics/overview.md)); 1,249,324 strings; ~52 GB of extracted IDA sidecars.
- **40,313 dispatch tables** (≈100× ptxas's 409), classified into 19 taxonomy classes.
- **160,351 RTTI records** (`_ZTI` 60,457 · `_ZTV` 39,244 · `_ZTS` 60,650 · 2), the 60,457 typeinfos led by `mlir::` (13,091), `asic_sw::` (11,379), `tensorflow::` (3,108), `xla::` (3,036), `llvm::` (2,940), with `dnnl::` / `std::` / `grpc_core::` and a long vendored tail behind them.
- **~2,900 static constructors** in `.init_array`; **1,069,659 relocations** (of which 1,069,006 are `R_X86_64_RELATIVE`).
- The section-header table ends **exactly at EOF** — there is **no** trailing payload past it. A `zstd` magic immediate that appears inside `.text` is an inline constant, not a stored compression frame; see [Trailing zstd Blob](forensics/trailing-zstd-blob.md).
- Custom ELF sections (`google_malloc`, `malloc_hook`, `protodesc_cold`, `filewrapper_toc`, `__rseq_cs`, `__lcxx_override`).
- Six TPU silicon generations under a Google-internal codename ladder: `jellyfish → dragonfish → pufferfish → viperfish → ghostlite → 6acc60406`, each with its own ISA encoding, cost model, and HAL family.

## Two-tier C ABI

```text
                JAX / PyTorch-XLA / TensorFlow
                          │
                          ▼  PJRT C-API (v0.103)
              ┌──────────────────────────────┐
              │   PJRT layer (outer C-API)   │   ← outer ABI  (Part II)
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

```text
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

Each page below carries a grade reflecting how directly its claims are anchored in the binary:

- `C` — **Confirmed / reimplementation-grade**: byte-anchored against objdump/nm/readelf or `protoc --decode_raw` of carved descriptors. The default for the byte-level deep-dive pages.
- `I` — **Inferred / synthesis**: foundational, forensic-survey, per-gen-parametric, or connective overview pages.
- `O` — **Open**: not yet recovered; tracked in the [Open-Frontier Register](appendix/open-frontier-register.md).

The evidence grade above (`C`/`I`/`O`) is the per-page label that matters. An `O` (open) page flags a specific not-yet-recovered detail, tracked in the [Open-Frontier Register](appendix/open-frontier-register.md).

### Parts at a glance

The **Open** column counts the pages still carrying an `O` (not-yet-recovered detail) grade.

| Part | Title | Pages | Open | Depends on | Source domain |
|------|-------|------:|:----:|-----------|---------------|
| 0 | Reference Apparatus | 9 | 0 | — | — |
| I | Binary Anatomy | 12 | 0 | 0 | forensics / dispatch / RTTI |
| II | Plugin Lifecycle & PJRT API | 23 | 0 | I | runtime / PJRT |
| III | Tpu C-Shim Layer | 10 | 0 | II | shim |
| IV | Silicon & Hardware Codename Model | 24 | 0 | — | silicon |
| V | Compiler — Lowering & Optimization Passes | 36 | 0 | IV | compiler |
| VI | TensorCore ISA & LLO Encoding | 42 | 2 | IV, V | ISA |
| VII | Cost & Latency Model | 41 | 0 | IV, VI | cost |
| VIII | Instruction Scheduling & Bundle Packing | 14 | 0 | VI, VII | cost / scheduling |
| IX | SparseCore & BarnaCore | 45 | 0 | IV, VI, VII | sparsecore |
| X | On-Chip Memory & DMA | 20 | 0 | IV | memory / DMA |
| XI | Runtime & Execution | 11 | 0 | II, VI, X | runtime |
| XII | Interconnect & Routing | 30 | 0 | IV | collectives / routing |
| XIII | On-Pod Collectives & Barriers | 30 | 0 | IX, XII | collectives |
| XIV | Megascale (Multi-Host / DCN) | 21 | 0 | XII, XIII | collectives / DCN |
| XV | Profiling & Telemetry | 22 | 0 | XI, XII | profiler |
| XVI | Configuration & Compile Knobs | 16 | 1 | V, VII | config |
| XVII | Appendices | 20 | 0 | all | cross-cutting |
| | **Total** | **426** | **3** | | |

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

- Function addresses are virtual addresses (`@0x…`); for `.text`/`.rodata`/`.lrodata`, VA == file offset.
- Each page carries a **References** block: the source binary and the function/symbol virtual addresses it cites.

> **NOTE —** the VA == file-offset rule holds only for `.text`/`.rodata`/`.lrodata`. For `.data` the file offset is VA − `0x400000`, and for `.data.rel.ro` it is VA − `0x200000`; seeking with `xxd`/`objdump` at the raw VA for a struct that resides in those sections reads the wrong bytes. The full section map is in [ELF Anatomy](forensics/elf-anatomy.md).
- The full page→symbol→VA mapping that backs every claim is the [Evidence-Anchor Index](appendix/evidence-anchor-index.md); the input set those addresses resolve against is catalogued in the [Source-Corpus Map](appendix/source-corpus-map.md).

## The source corpus

Every page in this book is derived from static analysis of `libtpu.so` — its symbol table, disassembly, and decompilation. The complete input set that the analysis ran against, down to the byte, is inventoried in the [Source-Corpus Map](appendix/source-corpus-map.md); the methodology that produced and consumed it is described in [Methodology (Deep)](appendix/methodology-deep.md).

---

## Master Index

## Part 0 — Reference Apparatus (9)

Orientation and connective tissue. Read the *Compile-Flow Walkthrough* first — it traces one matmul through every part and is the on-ramp to the whole book.

- `index.md` — **Landing / This Reference** · `I`  
  What libtpu is, binary provenance, organization, per-gen cross-index, reading paths.
- `front/how-to-read.md` — **How to Read This Book** · `I`  
  Evidence grades, the dependency-flow rationale, the reading-path personas.
- `front/compile-flow-walkthrough.md` — **Compile-Flow Walkthrough** · `I`  
  One `dot` op traced HLO → MHLO → tpu → LLO → bundle bytes → execution, cross-referencing every part. _src: P-2-03, P-2-01_
- `methodology.md` — **Methodology** · `I`  
  Extraction pipeline, IDA sidecars, FLIRT, `protoc --decode_raw`, naming conventions. _src: W001_
- `subsystem-map.md` — **Subsystem Map** · `I`  
  The 13-domain dependency web and how the 18 parts cover it. _src: W001, G001–G003_
- `front/codename-cheatsheet.md` — **Codename Cheat-Sheet** · `C`  
  TpuVersion 0–5 ↔ codename ↔ chip-DID ↔ Cloud name on one card.
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
  52 sections, segments, the VA==offset rule, `.lrodata`/`.rodata`/`.text` extents. _src: W023, W030_
- `forensics/two-binary-split.md` — **libtpu.so + sdk.so** · `C`  
  The 884,832-fn main object and the 94,732-fn sdk; symbol-population shape. _src: W001, W026_
- `forensics/custom-sections.md` — **Custom Sections** · `C`  
  google_malloc, protodesc_cold, filewrapper_toc, __rseq_cs, __lcxx_override. _src: P-2-07, W030_
- `forensics/embedded-library-atlas.md` — **Embedded-Library Atlas** · `C`  
  Vendored Abseil/protobuf/Eigen/oneDNN/tcmalloc/LLVM byte-accounting (FLIRT).
- `forensics/llvm-mlir-manifest.md` — **LLVM/MLIR Version Manifest** · `C`  
  Embedded toolchain version + the component list.
- `forensics/static-init.md` — **Static-Init Pipeline** · `I`  
  ~2,900 ctors, init ordering, the plugin-discovery hooks.
- `forensics/trailing-zstd-blob.md` — **Trailing zstd Blob** · `C`  
  Why no trailing payload exists: the "4.1 MB dictionary blob" false-positive correction (ZSTD-01). _src: P-2-30_
- `forensics/dispatch-table-taxonomy.md` — **Dispatch-Table Taxonomy** · `C`  
  40,313 tables → 19 classes (MLIR Op-Model, UFB pools, libpfm4, dnnl/Xbyak…). _src: P-2-06_
- `forensics/rtti-vtable-census.md` — **RTTI ↔ Vtable Cross-Validation** · `C`  
  Every typeinfo mapped to its vtable; the namespace census.
- `forensics/per-gen-function-dispatcher.md` — **Per-Generation Function Dispatcher** · `C`  
  The `util_registration::FunctionRegistry` dispatch engine.
- `forensics/polymorphic-entry-points.md` — **Polymorphic Dispatch Entry Points** · `C`  
  The indirect-call sites + the thunk-table and top-vtable classes.

## Part II — Plugin Lifecycle & PJRT API (23)

The outer ABI: how the plugin loads and the 140-slot PJRT_Api struct JAX/PyTorch consume.

### Lifecycle
- `lifecycle/overview.md` — **Overview** · `I`  
  From `dlopen` to a usable client.
- `lifecycle/elf-entry-and-init-proc.md` — **ELF Entry & init_proc** · `C`  
  `_init`, `.init_array`, the GOT/PLT bring-up.
- `lifecycle/do-init-do-fini.md` — **do_init / do_fini** · `C`  
  Constructor/destructor ordering and global state.
- `lifecycle/get-pjrt-api-thunk.md` — **GetPjrtApi Thunk & tpu_plugin Object** · `C`  
  `@0xe6a83a0` trampoline → `GetTpuPjrtApi` → 17 `__cxa_guard` blocks. _src: P-2-05_
- `lifecycle/tftpu-initialize-bootstrap.md` — **TfTpu_Initialize Bootstrap** · `I`  
  The initialize entry and option ingest.
- `lifecycle/module-init-plugin-discovery.md` — **Module-Init & Plugin Discovery** · `C`  
  How the PJRT plugin is registered and found.

### PJRT_Api surface
- `pjrt/overview.md` — **Overview** · `I`  
  The C-API version, the extension-chain idea, `.lbss` storage.
- `pjrt/api-vtable-reconstruction.md` — **PJRT_Api 140-Slot Reconstruction** · `C`  
  Every slot → libtpu impl VA, v0.103 schema, the `@0x227BA840` 1120-B table. _src: P-2-05_
- `pjrt/client-and-device.md` — **Client, Device & Topology** · `C`  
  `PJRT_Client_*`, device enumeration, addressable devices.
- `pjrt/buffer-and-memory.md` — **Buffer ABI & Memory Layouts** · `C`  
  `PJRT_Buffer_*`, on-device layout, external refcounting.
- `pjrt/executable-execution.md` — **Executable Loading & Execution** · `C`  
  compile → load → execute, serialization. _src: P-022_
- `pjrt/events-and-async.md` — **Events & Async Tracking** · `I`  
  `PJRT_Event` Await/OnReady/IsReady. _src: P-2-09_
- `pjrt/collectives-communicator.md` — **Collectives Communicator** · `C`  
  `CreateCommunicators`, cross-host handles.
- `pjrt/dma-and-cross-host-recv.md` — **DMA Map & Cross-Host Receive** · `C`  
  The DMA-map slots + cross-host buffer receive.
- `pjrt/callbacks.md` — **Callbacks & Pre-Fatal Hook** · `I`  
  Host-callback registration, the pre-fatal hook. _src: P-2-09_
- `pjrt/extension-chain.md` — **Extension Chain (17)** · `C`  
  The linked extension list and how it is walked.
- `pjrt/ext-profiler.md` — **Extension: Profiler (type 1)** · `C`  
  `PLUGIN_Profiler_Api` 8 slots.
- `pjrt/ext-topology-description.md` — **Extension: TopologyDescription (type 16)** · `C`  
  The TPU topology query.
- `pjrt/ext-rawbuffer.md` — **Extension: RawBuffer (type 8)** · `C`  
  Raw device buffers.
- `pjrt/ext-compile-phasecompile.md` — **Extension: Compile / PhaseCompile (type 9)** · `C`  
  The compile-options flow.
- `pjrt/ext-remaining.md` — **Extensions: Layouts / Memories / Stream / FFI / …** · `C`  
  The remaining chain entries.
- `pjrt/stream-executor-host-interpreter.md` — **StreamExecutor Host Interpreter** · `C`  
  The HloEvaluator host fallback.
- `pjrt/stream-executor-pjrt-adapter.md` — **StreamExecutor → PJRT Adapter** · `C`  
  `xla::TpuClient` / the CommonPjRt framework.

## Part III — Tpu C-Shim Layer (10)

The inner C ABI between PJRT and the runtime/compiler: the `Tpu*` C functions that wrap the C++ internals.

- `shim/overview.md` — **Overview** · `I`  
  The ~200-symbol Tpu* C surface and how PJRT calls it. _src: P-2-05, P-2-09_
- `shim/tpu-compiler-roster.md` — **TpuCompiler Roster** · `C`  
  The compile entry points.
- `shim/tpu-executable-roster.md` — **TpuExecutable Roster** · `C`  
  Executable handle ops. _src: P-022_
- `shim/tpu-executor-roster.md` — **TpuExecutor Roster** · `C`  
  Stream/executor C functions.
- `shim/tpu-transfer-manager.md` — **TpuTransferManager Roster** · `C`  
  The host↔device transfer C ABI.
- `shim/tpu-program-roster.md` — **TpuProgram Roster** · `C`  
  Program object + serialization C ABI. _src: P-022_
- `shim/tpu-platform-and-topology.md` — **TpuPlatform & TpuNodeContext** · `C`  
  Platform init, node context.
- `shim/tpu-topology.md` — **TpuTopology & TpuCoreLocation** · `C`  
  The topology/core-location C ABI.
- `shim/tpu-embedding-engine.md` — **TpuEmbeddingEngine ABI** · `C`  
  The embedding/SparseCore C surface.
- `shim/tpu-configuration-api.md` — **TpuConfigurationApi** · `C`  
  Runtime configuration entry points.

## Part IV — Silicon & Hardware Codename Model (24)

The hardware the whole compiler is parameterized by. Read before V–VIII: the cost model, ISA, and MSA defaults all key off the per-codename constants defined here. **Canonical for: per-gen hardware constants** (referenced by VI, VII, IX).

### Codename identity
- `targets/overview.md` — **Overview** · `I`  
  Six generations, three HAL families, the dual-enum trap. _src: P-2-08_
- `targets/tpu-version-codename-matrix.md` — **6-Codename Authoritative Reconciliation** · `C`  
  The 6-axis (enum / CHECK / chip_parts / PCI / namespace / marketing) cross-check; settles glc=v4, gfc=v5.
- `targets/dual-enum-proto-vs-internal.md` — **Dual Enum (Proto vs Internal)** · `C`  
  `TpuVersionProto = internal + 1`; the "chip_parts v6" resolution. _src: P-2-08_
- `targets/pci-device-ids.md` — **PCI Device IDs** · `C`  
  Chip DIDs 0x00d1/0x00f2, header DIDs, rev-masks, IsGlc/IsGfc → device-type.
- `targets/marketing-cloud-naming.md` — **Marketing / Cloud Naming** · `C`  
  v2…v6e/tpu7x; Trillium=v6e; Trillium/Ironwood NOT in the binary.
- `targets/codename-superseded-labels.md` — **Superseded-Label Correction List** · `C`  
  The "v5p"/"Trillium" mislabels and the Ghostfish gloss.

### HAL families
- `targets/hal-families.md` — **HAL Families** · `C`  
  jxc/pxc/vxc factories; glc/gfc under gxc; per-family Register immediates. _src: P-2-08_
- `targets/hal-factory-override-matrix.md` — **HAL Factory Override Matrix** · `C`  
  Which methods each family overrides vs inherits. _src: P-2-35_
- `targets/tpuhal-class-hierarchy.md` — **TpuHal Class Hierarchy** · `C`  
  Only TpuHalHardware* exists; the {Jxc,Pxc,Vxc}HardwareImpl tree.
- `targets/jxc-family.md` — **JXC Family (Jellyfish, Dragonfish)** · `C`  
  Identity, factory, PCI, cores.
- `targets/pxc-family.md` — **PXC Family (Pufferfish)** · `C`  
  Identity, factory, BarnaCore binding.
- `targets/vxc-family.md` — **VXC Family (Viperfish)** · `C`  
  Identity, std/lite variants, factory.
- `targets/gxc-family.md` — **GXC Family (Ghostlite, 6acc60406)** · `C`  
  glc/gfc namespaces, the anonymous v5 codec, chip DIDs.
- `targets/sub-core-taxonomy.md` — **Sub-Core Taxonomy (GFC/GLC/JXC/PXC/VFC/VLC)** · `C`  
  The sub-family encoder split. _src: P-2-08_

### Per-codename hardware constants
- `targets/chip-parts-binarypb.md` — **chip_parts.binarypb Decode** · `C`  
  TpuChipPartsProto, the embedded v5/v7 blobs, the variant allow-list.
- `targets/per-codename-hw-constants.md` — **Per-Codename Constant Table** · `C`  
  The master integer source consumed by the cost model and ISA.
- `targets/tpu-topology-struct.md` — **TpuTopology Struct (Target+0x3b8)** · `C`  
  Per-codename chip geometry.
- `targets/tpu-chip-config.md` — **TpuChipConfig** · `C`  
  LaneCount/SublaneCount/ChunksPerTile, VexMatrixWidth.
- `targets/sparsecore-target-descriptor.md` — **SparseCoreTarget (Target+0x948)** · `C`  
  The per-codename MXU-contracting-depth map.
- `targets/target-capability-bitfield.md` — **Target Capability Bitfield (Target+0x628)** · `C`  
  The 2 live capability bits across the whole struct.
- `targets/kdevicetypeinfo-spec-constants.md` — **kDeviceTypeInfo Spec-Constants** · `C`  
  ~40 IEEE-754 doubles + the two DVFS frequency ladders.
- `targets/accuracy-tables.md` — **Per-Gen Accuracy Tables** · `C`  
  Transcendental approximation accuracy driving precision decisions.

### Memory model primer (detailed allocators in Part X)
- `targets/memory-hierarchy.md` — **Memory Hierarchy** · `I`  
  HBM/VMEM/SMEM/CMEM/SFLAG tier model + the 17 MemorySpace values. _src: P-2-01_
- `targets/address-space-ids.md` — **Address-Space ID Table (AS0–AS9)** · `C`  
  Incl. the SparseCore fat-pointer AS7/8/9.

## Part V — Compiler: Lowering & Optimization Passes (36)

The IR descent and the optimization passes. Silicon-parameterized (uses IV) but ISA-light: it lowers to LLO ops by name, not bits. Output is LLO IR; encoding is VI, cost/scheduling are VII/VIII.

### Front-end and pipeline
- `compiler/overview.md` — **Overview** · `I`  
  `DeepseaCompilerBase::RunHloPasses`, compile phases 0–3. _src: P-2-03_
- `compiler/hlo-ingestion.md` — **HLO Ingestion** · `C`  
  StableHLO → HLO, the entry module. _src: P-002_
- `compiler/compile-phases.md` — **Compile Phases 0–3** · `C`  
  Phase0Stablehlo … Phase3Linking. _src: P-2-03_
- `compiler/compilation-cache.md` — **Compilation Cache** · `C`  
  Keys, fingerprints, the hit path. _src: P-022_
- `compiler/hlo-pass-registry.md` — **HLO Pass Registry** · `C`  
  The three pipeline containers + the xla_* flag-atlas binding.
- `compiler/hlo-pre-passes.md` — **HLO Pre-Passes** · `C`  
  The passes that run before TPU lowering.
- `compiler/sharding-propagation.md` — **Sharding Propagation** · `C`  
  GSPMD sharding inference.
- `compiler/auto-sharding-spmd.md` — **Auto-Sharding / SPMD** · `C`  
  The partitioning passes.
- `compiler/algebraic-simplifier.md` — **Algebraic Simplifier** · `I`  
  TPU-specific algebraic rewrites.
- `compiler/dynamic-shape-support.md` — **Dynamic-Shape Support** · `C`  
  Bounded-dynamic model, dimension-size ops.
- `compiler/optimization-barrier.md` — **Optimization Barrier** · `C`  
  Insertion, honouring, erasure.
- `compiler/custom-call-lowering.md` — **Custom-Call Lowering & Registry** · `C`  
  The target catalog + the registration side.

### MLIR lowering chain
- `compiler/mhlo-xtile-tpu-lowering.md` — **MHLO → XTile → tpu** · `C`  
  The dialect-chain conversion.
- `compiler/mosaic-overview.md` — **Mosaic Overview** · `C`  
  The kernel → tpu → LLO path.
- `compiler/mosaic-layout-inference.md` — **Mosaic Layout Inference** · `C`  
  VectorLayoutInferer per-op rules.
- `compiler/mosaic-vectorlayout.md` — **Mosaic VectorLayout** · `C`  
  The (sublane, lane) layout algebra.
- `compiler/tpu-dialect-and-ops.md` — **The tpu MLIR Dialect** · `C`  
  Ops, attributes, the 157-op surface.
- `compiler/tpu-to-llo-ods.md` — **tpu → LLO Lowering** · `C`  
  Per-op ODS operand/result/attr signatures, gain/staging registers.
- `compiler/dialect-conversion-legalizer.md` — **DialectConversion Legalizer** · `C`  
  The depth-aware legalization cost.
- `compiler/conversion-pattern-rewriter.md` — **ConversionPatternRewriter** · `C`  
  The rollback/rewrite-log engine, 1:N patterns.
- `compiler/lower-to-mlo-dma-bridge.md` — **LowerToMlo DMA Bridge-Cast** · `C`  
  The two-stage DMA lowering.
- `compiler/lower-to-sparsecore-llvm.md` — **LowerToSparseCoreLlvm** · `C`  
  Per-class rewrite bodies.
- `compiler/sc-type-converter.md` — **SCTypeConverter** · `C`  
  The addr-space → `!llvm.ptr<addrspace>` map.
- `compiler/mlir-op-model-contract.md` — **MLIR Op-Model Contract** · `C`  
  The 23-slot `Model<Op>` (6,050 instances).
- `compiler/llvmtpu-intrinsic-catalog.md` — **LlvmTpu Intrinsic Catalog** · `C`  
  The 1,356 `tpu_*` backend intrinsics.

### Memory & layout optimization
- `compiler/msa-overview.md` — **MSA Overview** · `C`  
  The memory-space-assignment ILP pass.
- `compiler/msa-allocate-segment.md` — **MSA AllocateSegment** · `C`  
  The allocation body + config proto.
- `compiler/msa-per-version-defaults.md` — **MSA Per-Version Defaults** · `C`  
  Overlap ratios / outstanding-copy caps per gen.
- `compiler/msa-reservation-hbm-policy.md` — **MSA Reservation & HBM Policy** · `C`  
  MsaReservationPolicy / HbmPolicy field dicts.
- `compiler/layout-assignment.md` — **Layout Assignment** · `C`  
  FindMemoryMinimizingLayout weights + AddBackendConstraints.

### Fusion, dot/conv, tiling
- `compiler/fusion-patterns.md` — **Fusion Patterns** · `C`  
  The TPU-specific fusion class roster.
- `compiler/fusion-cost-model.md` — **Fusion Cost Model** · `C`  
  Priority coefficients + the ShouldFuseImpl lambda set.
- `compiler/dot-conv-mxu-lowering.md` — **Dot / Conv → MXU Lowering** · `C`  
  Tile-cost comparator + EmitFunctorEnum.
- `compiler/raggeddot-convolution.md` — **RaggedDot → Windowed Convolution** · `C`  
  FromRaggedDot / DynamicSliceMaskedConv geometry.
- `compiler/loop-tiling-unrolling.md` — **Loop Tiling & Unrolling** · `C`  
  TileKind rules + LoopConfig proto + the pipeline unroller.
- `compiler/tpu-program-serialization.md` — **TpuProgram Serialization** · `C`  
  The final compiled-program container. _src: P-022, P-2-09_

## Part VI — TensorCore ISA & LLO Encoding (42)

The target representation: LLO IR and the per-generation VLIW bundle bit-layouts. Self-contained — read independently of the cost model. Bundle *packing* (LLO→bytes) is in VIII.

### Foundations
- `isa/overview.md` — **Overview** · `I`  
  LLO IR: 462 opcodes, 17 memory spaces, the proto-descriptor source. _src: P-2-01, P-2-25_
- `isa/llo-opcode-enum.md` — **LloOpcode Enum (462)** · `C`  
  Categories: scalar / vector / EUP / reduction / MXU / transpose / DMA / sync / BarnaCore. _src: P-2-25_
- `isa/memory-space-enum.md` — **MemorySpace Enum (17)** · `C`  
  HBM/VMEM/SMEM/SFLAG/IMEM/CMEM/SC/HOST/PINNED. _src: P-2-01_
- `isa/bundle-model-overview.md` — **Bundle Model** · `I`  
  Per-gen sizes (41/51/64 B), slot counts, bundles-per-DMA-chunk. _src: P-2-04, P-2-34_
- `isa/instbits-master-db.md` — **InstBits Master DB** · `C`  
  The LLVM-MC per-opcode base bits; the default-all-zero / no-RELA finding.
- `isa/instr-name-data.md` — **TPUInstrNameData / Descs / RegEncoding** · `C`  
  opcode→mnemonic, MCInstrDesc, the reg-encoding table.
- `isa/llo-opcode-to-proto.md` — **LloOpcode ↔ Proto** · `C`  
  The 462-entry map + the inverse ProtoToLloOpcode.
- `isa/mc-emitter.md` — **MC-Emitter (getBinaryCodeForInstr)** · `C`  
  The insertBits operand path + the HwMode select.
- `isa/record-format.md` — **239-Bit Record Format** · `C`  
  The APInt record + per-operand insertBits(value, pos, width).

### Per-generation VLIW bundle
- `isa/bundle-jf-41b.md` — **Jellyfish 41-Byte Bundle** · `C`  
  The full slot map (EncodeBundleInternal).
- `isa/bundle-df.md` — **Dragonfish Bundle** · `C`  
  The JF/DF shared 41-B layout deltas.
- `isa/bundle-pf-51b.md` — **Pufferfish 51-Byte Bundle** · `C`  
  EncoderPf + the 5 shared load/store sub-encoders.
- `isa/bundle-vf-64b.md` — **Viperfish 64-Byte Bundle** · `C`  
  Quad VALU, triple vload, ScalarSubBundle.
- `isa/bundle-gl.md` — **Ghostlite Bundle** · `C`  
  The vector_misc slot; the glc encoder.
- `isa/bundle-gf.md` — **6acc60406 Bundle** · `C`  
  The dedicated predicates slot; the gfc encoder.

### Per-slot encoding
- `isa/slot-mxu.md` — **MXU Slot** · `C`  
  matmul/matpush issue, latch fields, the per-gen MXU1 twin.
- `isa/slot-vpu.md` — **VPU (Vector-ALU) Slot** · `C`  
  All generations.
- `isa/slot-spu-scalar.md` — **SPU / Scalar Slot** · `C`  
  All generations.
- `isa/slot-sequencer.md` — **Sequencer Slot** · `C`  
  branch/call/halt; the proto-bundle emitter path.
- `isa/slot-memory-load.md` — **Memory-Load Slot** · `C`  
  All generations.
- `isa/slot-memory-store.md` — **Memory-Store Slot** · `C`  
  All generations.
- `isa/slot-predicate.md` — **Predicate-Register File** · `C`  
  The 7-bit field (4+1+2), count per gen.
- `isa/slot-loop.md` — **Hardware Loop-Counter** · `C`  
  Encoding + count per gen.
- `isa/slot-immediate.md` — **Immediate Slot** · `C`  
  Per-gen encoding-id → imm-slot bit position.
- `isa/slot-eup-transcendental.md` — **EUP / Transcendental Slot** · `C`  
  VectorResult + VALU3 bit positions.
- `isa/slot-matprep-iar-latch.md` — **Matprep / IAR / Latch** · `C`  
  Per-gen matprep WORD tables, the IAR bit-layout.
- `isa/slot-vcreate-mask-mregister.md` — **vcreate_mask / M-Register** · `C`  
  End-inclusive range, per-gen field offsets, M0–M31.
- `isa/slot-cmem-load-pf.md` — **cmem_load Slot (Pufferfish)** · `C`  
  The v4 constant-memory load path.
- `isa/slot-sparsity-v5plus.md` — **Sparsity Slot (v5+)** · `O`  
  The structured-sparsity slot encoding. _src: #1092 (open)_

### Encode / decode support
- `isa/v5plus-emitx-bit-positions.md` — **V5+ EmitX Absolute Bit Positions** · `C`  
  isa_emitter EmitX → BitCopy offsets (closes the InstBits gap).
- `isa/isa-emitter-registry.md` — **IsaEmitter Registry** · `C`  
  The (TpuVersion, SequencerType) pair-key cell census.
- `isa/decode-side-jf-pf.md` — **Decode-Side: JF / PF** · `C`  
  The disassembler inverse.
- `isa/decode-side-vf-gxc.md` — **Decode-Side: VF / GXC** · `C`  
  The −20-bit twin decode.
- `isa/nop-canonical.md` — **NOP / Unused-Slot Canonical Encoding** · `O`  
  Per-gen NOP templates. _src: #1096 (open)_
- `isa/tpumcimm-syimm32.md` — **TPUMCImm / SyImm32 Operand** · `C`  
  The MC immediate operand encoding + PatchOverlay.
- `isa/archregno-numbering.md` — **ArchRegno Runtime Numbering** · `C`  
  ToArchRegno / InitRegisterNumbering per gen.
- `isa/kisatable-data-sections.md` — **kIsaTable Data Sections** · `C`  
  The per-gen ISA-encoding split (no literal kIsaTable symbol).
- `isa/sequencer-ops-per-gen.md` — **Sequencer Ops Per Gen × Type** · `C`  
  Control-flow op rosters.
- `isa/resultfifo-archregister.md` — **ResultFifo & ArchRegister Enums** · `C`  
  25 result FIFOs + the 0x32-entry arch-register enum.
- `isa/bias-quantization-helpers.md` — **Bias-Add & Quant/Dequant Helpers** · `C`  
  The TPU bias/quantization helper functions.
- `isa/xlu-op-roster.md` — **XLU Op Roster** · `C`  
  Vsetperm/Vxpose/Vpermute/… opcode→factory table.
- `isa/pack-unpack-precision.md` — **Pack/Unpack Precision** · `C`  
  VpackBf16 / VunpackCF32 bf16↔f32 conversion + segmented-reduce RPU.

## Part VII — Cost & Latency Model (41)

What every instruction costs. The largest data surface in the binary (51 source files). Consumed by the schedulers in VIII; depends on the ISA (VI) and silicon constants (IV).

### Core model
- `cost/overview.md` — **Overview** · `I`  
  The Performance / CycleTable / LatencyTable family architecture. _src: P-2-04_
- `cost/resource-enum.md` — **Resource Enum (23-slot)** · `C`  
  Names + SubsetOptions partition + TC-frequency wiring.
- `cost/per-opcode-cycle-constants.md` — **Per-Opcode Cycle Constants** · `C`  
  Per-gen cycle-table dispatch.
- `cost/normalized-computation-cost.md` — **NormalizedComputationCost** · `C`  
  opcode→weight switch + GetCyclesIfFused.
- `cost/gethloresources-routing.md` — **GetHloResources Routing** · `C`  
  Per-op → ResourceVector sub-emitter routing.
- `cost/tpu-hlo-cost-analysis.md` — **TpuHloCostAnalysis** · `C`  
  The flop-override surface.
- `cost/bundle-aware-cost.md` — **Bundle-Aware Cost** · `C`  
  VLIW bundle-issue cost.
- `cost/memory-bandwidth-latency-model.md` — **Memory Bandwidth & Latency Model** · `C`  
  The full cross-tier matrix per gen.
- `cost/local-dma-bandwidth.md` — **LocalDmaBandwidth** · `C`  
  Per-gen matrix + the MemXfer-latency consumer.

### MXU latency (per-gen reservation matrices)
- `cost/mxu-latency-overview.md` — **MXU Latency Overview** · `C`  
  MxuResource enum + the reservation-matrix concept.
- `cost/mxu-latency-jf-df.md` — **MXU Latency: JF / DF** · `C`  
  Oldest-gen reservation rows.
- `cost/mxu-latency-pf.md` — **MXU Latency: PF** · `C`  
  Pufferfish reservation rows.
- `cost/mxu-latency-vf.md` — **MXU Latency: VF** · `C`  
  Full Viperfish reservation matrix value-by-value.
- `cost/mxu-latency-gl.md` — **MXU Latency: GL (Ghostlite)** · `C`  
  The GLM reservation rows.
- `cost/mxu-latency-gf.md` — **MXU Latency: GF (6acc60406)** · `C`  
  res-remap 3/8 + fp8-fnuz.
- `cost/matmul-mode-modifiers.md` — **MatmulMode & Modifiers** · `C`  
  16-ordinal naming, Matmul/MatpushModifier array<19> values.
- `cost/mxu-opholdissues-stall.md` — **MxuOpHoldIssues Stall Recurrence** · `C`  
  The stall formula + the balancing gate.

### Performance grids (per-gen Instruction × Resource)
- `cost/performance-overview.md` — **Performance Family Overview** · `I`  
  The per-gen Performance<gen> variant model.
- `cost/performance-jf-df.md` — **Performance: JF / DF** · `C`  
  Full latency array + I×R grid.
- `cost/performance-pf.md` — **Performance: PF** · `C`  
  20-resource grid + the BarnaCore variant1.
- `cost/performance-vf.md` — **Performance: VF** · `C`  
  The Viperfish grid.
- `cost/performance-gl-ghperf.md` — **Performance: GL (GhPerf 476×31)** · `C`  
  The Ghostlite occupancy grid.
- `cost/performance-gf-ghperf.md` — **Performance: GF (GhPerf 465×31)** · `C`  
  The 6acc60406 occupancy grid.

### CycleTable
- `cost/cycletable-family.md` — **CycleTable Family** · `C`  
  LatencyTable::Create(TpuVersion) factory dispatch.
- `cost/jf-cycletable.md` — **JfCycleTable** · `C`  
  offsetLUT transcription + 7-column Resource naming.
- `cost/vf-cycletable.md` — **VfCycleTable** · `C`  
  The 32-entry CT→(instr, res) dump + throughput bridge.

### EUP / transcendental latency
- `cost/eup-latency-overview.md` — **EUP Latency Overview** · `C`  
  The push→pop software-pipelining model.
- `cost/eup-per-gen-integers.md` — **EUP Per-Gen Latency Integers** · `C`  
  PF/VF/GL push→pop integers.
- `cost/eup-paynehanek.md` — **EUP Payne-Hanek Range Reduction** · `C`  
  The 2/π table.
- `cost/eup-correction-coeffs.md` — **EUP Correction Coefficients** · `C`  
  Newton / VfastTwoSum per-function polynomials.
- `cost/eup-lane-width-unpack.md` — **EUP Lane-Width / Unpack** · `C`  
  AluEpOpLowering unpack → compute → pack.

### XLU cost
- `cost/xlu-conflict-penalty.md` — **XLU Conflict-Penalty Table** · `C`  
  The non-MXU hazard table.
- `cost/xlu-combine-sourcebus.md` — **XLU Combine / Source-Bus** · `C`  
  ComputeCombinablePairs + AssignSourceBus.
- `cost/xlu-reemit-cost.md` — **XLU Reemit Cost** · `C`  
  Closed-form CyclesAddedByXluOperation + PerXluOperations.
- `cost/xpose-reservation-latency.md` — **Transpose-Reservation Latency** · `C`  
  XposeXLUReservationLatency + VxposeMode.

### Conv / window cost
- `cost/window-description-cost.md` — **WindowDescription Byte-Cost** · `C`  
  The conv/DMA byte+throughput primitive.
- `cost/convolution-cost-state.md` — **ConvolutionCostState** · `C`  
  Field map + VfCycleTable bridge.
- `cost/reduce-window-pooling-cost.md` — **Reduce-Window / Pooling Cost** · `C`  
  RecordReduceWindowCycles.

### Misc cost
- `cost/learned-cost-model-client.md` — **Learned Cost-Model Client** · `C`  
  EmitterLearnedCostModelOptions + the wiring status.
- `cost/cost-model-logging.md` — **Cost-Model Logging** · `C`  
  The impure AutoOr consumer + the float grammar.
- `cost/iars-per-tensorcore.md` — **Consolidated Per-Gen Counts** · `C`  
  IarsPerTensorCore / mxu / xlu counts in one table.

## Part VIII — Instruction Scheduling & Bundle Packing (14)

The algorithms that consume the cost model (VII) and emit ordered, packed bundles (VI). Smaller than VII by design — in this corpus the scheduling *algorithms* are a fraction of the cost *data*.

- `sched/overview.md` — **Overview** · `I`  
  Where scheduling sits between lowering and encoding.
- `sched/latency-hiding-scheduler-core.md` — **LatencyHidingScheduler Core** · `C`  
  ScheduleComputation candidate loop + the async tracker.
- `sched/lhs-post-layout-pre-fusion.md` — **LHS: post_layout_pre_fusion Variant** · `C`  
  The early scheduling variant.
- `sched/lhs-post-layout.md` — **LHS: post_layout / final Variant** · `C`  
  The final scheduling variant.
- `sched/lhs-ilp-variant.md` — **LHS: ILP Variant** · `C`  
  The two flag-gated code paths.
- `sched/scheduler-resourcetype-model.md` — **ResourceType Taxonomy** · `C`  
  Per-resource model + AsyncTracker → core registry.
- `sched/bundle-modulo-scheduling.md` — **Bundle Modulo Scheduling** · `C`  
  The II-search + software pipelining.
- `sched/llo-bundle-packing.md` — **LLO → Bundle Packing** · `C`  
  The final-stage slot-assignment algorithm.
- `sched/mxu-assignment-binpacker.md` — **MXU Assignment Bin-Packer** · `C`  
  AssignMxusForSequenceGroup.
- `sched/latch-assignment-overrun.md` — **Latch Assignment & Overrun** · `C`  
  SetLatchIndices + the per-gen overrun handshake.
- `sched/mxu-sequence-struct.md` — **MxuSequence / SequenceInfo** · `C`  
  The full record + set_mxu commit.
- `sched/mrb-chain-allocator.md` — **MRB Chain Allocator** · `C`  
  The reservation-timeline algorithm + jitter model.
- `sched/mrb-fifo-msr-placement.md` — **MRB FIFO / MSR Placement** · `C`  
  AllocateMrbEntriesAsFifo + BounceBetweenMsrs.
- `sched/encoder-latch-serialization.md` — **Per-Gen Encoder Latch Serialization** · `C`  
  How latch fields serialize into the per-gen bundle.

## Part IX — SparseCore & BarnaCore (45)

The embedding/sparse engine (SparseCore, v5+) and its retired predecessor (BarnaCore, v2–v4). Kept whole rather than sliced across the ISA/cost/scheduling axis. The collective-offload story lives in Part XIII.

### SparseCore engines
- `sparsecore/overview.md` — **Overview** · `I`  
  SCS/TAC/TEC, the 2-sequencer (SCS+TEC) model. _src: P-2-02_
- `sparsecore/architecture.md` — **Architecture** · `C`  
  Engine roles + the embedding datapath.
- `sparsecore/scs-engine.md` — **SCS (Scalar) Engine** · `C`  
  The scalar sequencer engine.
- `sparsecore/tac-engine.md` — **TAC Engine** · `C`  
  The codec-only role.
- `sparsecore/tec-engine.md` — **TEC (Vector) Engine** · `C`  
  The vector execution engine.
- `sparsecore/bundle-slot-base-map.md` — **Per-Engine Bundle Slot-Base Map** · `C`  
  SCS/TAC/TEC byte offsets.
- `sparsecore/region-to-sequencer-outliner.md` — **Region → Sequencer Outliner** · `C`  
  Partitions an SC computation into per-engine bundles.
- `sparsecore/getsequencertype.md` — **getSequencerType** · `C`  
  Engine selection (SCS/TAC/TEC).

### SparseCore ISA
- `sparsecore/scalar-opcode-enum.md` — **Scalar Opcode Enum** · `C`  
  ScsScalarMisc / ScalarAlu0 / ScalarAlu1.
- `sparsecore/vector-opcode-enum.md` — **Vector Opcode Enum** · `C`  
  VF 148-op / GF 257-op VectorAlu.
- `sparsecore/oneslot-router.md` — **OneSlot Scalar Router** · `C`  
  ConsumeOneSlotInstruction jump table.
- `sparsecore/vectorload-slot.md` — **VectorLoad Slot** · `C`  
  5-op field layout + the SourceOne seed enum.
- `sparsecore/vectorstore-slot.md` — **VectorStore Slot** · `C`  
  The 33-entry type×mode scatter matrix.
- `sparsecore/vectorextended-vex.md` — **VectorExtended / VEX** · `C`  
  The 53-op scan/sort/dedup family.
- `sparsecore/vex-operand-port.md` — **VEX Operand-Port Binding** · `C`  
  FindAndEmitToUnusedPort (generation-specific).
- `sparsecore/vex-mask-destport-subopcode.md` — **VEX Mask / Dest-Port / Sub-Opcode** · `C`  
  The bit0x104 mask field + the sub-opcode map.
- `sparsecore/m-register-predicate.md` — **M-Register Predicate Word (M0–M31)** · `C`  
  Masked-scan inactive semantics.
- `sparsecore/cbreg.md` — **CBREG Circular-Buffer Register** · `C`  
  Bit layout, addressing, wrap.

### SparseCore datapath (embeddings)
- `sparsecore/scan-datapath.md` — **Scan Datapath** · `C`  
  Mask consumption + ScanOp lowering.
- `sparsecore/segmented-scan.md` — **Segmented Scan** · `C`  
  SegmentedScanOpLowering reduction_op switch.
- `sparsecore/segmented-add-scan.md` — **Segmented-Add-Scan** · `C`  
  The newer-gen segment-reduce family.
- `sparsecore/embedding-minibatching.md` — **Embedding Minibatching Decomposition** · `C`  
  The HLO layer above scan lowering.
- `sparsecore/sample-combiner-emitter.md` — **SampleCombiner Emitter** · `C`  
  The inner-loop combiner emit.
- `sparsecore/emit-valency-loop.md` — **EmitValencyLoop** · `C`  
  The per-sample valency loop.
- `sparsecore/rank-and-permute-radixsort.md` — **RankAndPermute / RadixSort** · `C`  
  The sort/permute compute function.
- `sparsecore/dedup-multiplicity.md` — **Dedup Multiplicity** · `C`  
  DuplicateCount→multiplicity + Uniquify inverse-permutation.

### SparseCore pointers & DMA
- `sparsecore/fat-pointers-as789.md` — **Fat Pointers (AS7/8/9)** · `C`  
  160/128/192-bit structured-pointer constructors.
- `sparsecore/addrspacecast-isel.md` — **addrspacecast ISel** · `C`  
  The 16-cast from→to AS map.
- `sparsecore/tile-id-cast.md` — **Tile-ID Cast** · `C`  
  On-tile 2-operand cast lowering.
- `sparsecore/stream-gather-scatter.md` — **Stream Gather/Scatter** · `C`  
  The indirect-DMA descriptor format.
- `sparsecore/indirect-vreg-stream.md` — **IndirectVregStream** · `C`  
  The VREG-loop form.

### SparseCore back-end
- `sparsecore/sc-backend-pipeline.md` — **SC Backend Pipeline** · `C`  
  RunPasses, all 12 passes, the MEGACORE barrier.
- `sparsecore/sc-emitx-dispatcher.md` — **SC EmitX Dispatcher** · `C`  
  seq3/seq4/seq5 → EmitX jump tables.
- `sparsecore/sc-core-selection.md` — **SC Core Selection** · `C`  
  SelectCores / GetAllowedCores policy.
- `sparsecore/sc-queue-assignment-reservation.md` — **SC Queue Assignment & Reservation** · `C`  
  The resource→limit btree_map.
- `sparsecore/getsparsecoreconfig.md` — **GetSparseCoreConfig** · `C`  
  The offload op-type enum source.

### SparseCore cross-cutting
- `sparsecore/sc-mxu-handshake.md` — **SC ↔ MXU Handshake** · `C`  
  The integration handshake.
- `sparsecore/sparsecore-vs-neuron-matmultsparse.md` — **SparseCore vs Neuron MatmultSparse** · `I`  
  Cross-vendor comparison.

### BarnaCore (legacy v2–v4)
- `barnacore/overview.md` — **Overview** · `I`  
  The legacy embedding accelerator.
- `barnacore/retirement.md` — **Retirement Evidence** · `C`  
  The BarnaCore → SparseCore transition.
- `barnacore/bcs-scalar-isa.md` — **BCS Scalar0/Scalar1 ISA** · `C`  
  The 122-op control+memory ISA.
- `barnacore/bcs-32byte-bundle.md` — **BCS 32-Byte Bundle** · `C`  
  InstBits_BarnaCorePxcHwMode + BcsMetadataAccessor.
- `barnacore/merged-alu.md` — **Merged-ALU Bit Layout** · `C`  
  VectorResultDestination / BaseAddressEncoding.
- `barnacore/jf-df-address-handler-bundle.md` — **JF/DF 16-Byte Address-Handler Bundle** · `C`  
  EncoderJf::EncodeBarnaCoreAddressHandler.
- `barnacore/per-gen-perf-grids.md` — **Per-Gen BarnaCore Perf Grids** · `C`  
  PufferfishBarnaCorePerformance variant1.

## Part X — On-Chip Memory & DMA (20)

The memory tiers' allocators and the DMA wire formats. The tier *model* is primed in IV; here are the allocator algorithms and descriptor byte layouts.

### Memory tiers
- `memory/overview.md` — **Overview** · `I`  
  The five on-chip tiers + host memory. _src: P-2-01_
- `memory/hbm-allocator.md` — **HBM BestFit Allocator** · `C`  
  Coalescing rule + split/fragmentation policy.
- `memory/hbm-dma-alignment.md` — **HBM DMA Alignment Contract** · `C`  
  The minimum-alignment rule.
- `memory/vmem-allocator.md` — **VMEM Allocator** · `C`  
  Per-codename Config, alignment, MSA integration.
- `memory/smem-scalar-memory.md` — **SMEM Scalar Memory** · `C`  
  Allocator, addressing, placement.
- `memory/smem-register-window.md` — **SMEM Register-Window** · `C`  
  The mechanism + reconciliation with the SPU slot.
- `memory/cmem-pool.md` — **CMEM Constant-Memory Pool** · `C`  
  Layout, allocator, placement (Pufferfish+).
- `memory/sflag-protocol.md` — **SFLAG Sync-Flag Tier** · `C`  
  Allocator, Config, atomics, ordering.
- `memory/tpu-buffer-layout.md` — **TpuBuffer Layout** · `C`  
  On-device buffer structure.
- `memory/buffer-donation-aliasing.md` — **Buffer Donation & Aliasing** · `I`  
  DonateWithControlDependency. _src: P-2-09_
- `memory/on-device-compaction.md` — **On-Device Compaction** · `I`  
  The defrag path.
- `memory/embedded-tcmalloc.md` — **Embedded tcmalloc** · `C`  
  Host-CPU allocator integration + sizing.

### DMA
- `dma/intra-chip-descriptor.md` — **Intra-Chip DMA Descriptor** · `C`  
  Format, tiling, tier-pair encoding.
- `dma/tile-index-expansion.md` — **Tile-Index Expansion** · `C`  
  ExpandTiledMemRefs / expandTiledIndices algebra.
- `dma/rolled-strided-general.md` — **Rolled / Strided / General Emitters** · `C`  
  issueRolled/Strided/General transfer bodies.
- `dma/dma-parameters-selector.md` — **DmaParameters Selector** · `C`  
  Simple vs SingleStrided + dim-coalescing.
- `dma/host-device-dma.md` — **Host↔Device DMA** · `C`  
  DeriveHostDmaTransfers + tags 6/7.
- `dma/uhi-host-interface.md` — **UHI Host-Interface DMA** · `C`  
  The wire format + QueueId semantics.
- `dma/oci-command-dma-id.md` — **OCI Command DMA-ID** · `C`  
  The 6 CmdDmaIdFromEntry helpers + the 3-header bands.
- `dma/continuation-queue.md` — **Continuation Queue** · `C`  
  Memory model + runtime SFLAG protocol + the halt model.

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
  Device-memory allocation flow.
- `runtime/error-templates.md` — **Error/Status String Templates** · `C`  
  The printf-format + StrFormat catalog.
- `runtime/hint-strings.md` — **User-Facing Hint Strings** · `C`  
  Actionable diagnostics (flag-suggestion / doc-link / capacity).
- `runtime/internal-pass-names.md` — **Internal Pass-Name Catalog** · `C`  
  HLO + MLIR + pipeline phase names.

## Part XII — Interconnect & Routing (30)

The physical fabric and how packets route across it. The geometric substrate (twisted torus) that on-pod collectives (XIII) build on.

### ICI fabric
- `ici/overview.md` — **Overview** · `I`  
  The inter-chip interconnect model.
- `ici/link-bringup.md` — **Link Bring-Up Sequence** · `C`  
  The link initialization sequence.
- `ici/topology-discovery.md` — **Topology Discovery** · `C`  
  Master::DiscoverTopology end-to-end.
- `ici/dma-descriptor.md` — **Cross-Chip DMA Descriptor** · `C`  
  The ICI DMA wire format.
- `ici/all-reduce-primitive.md` — **ICI All-Reduce Primitive** · `C`  
  The step-generation primitive.
- `ici/failure-recovery.md` — **Failure Modes & Recovery** · `C`  
  The recovery flow.
- `ici/vc-balance-allocation.md` — **VC-Balance Allocation** · `C`  
  Deadlock-free virtual-channel allocation.

### Routing
- `routing/overview.md` — **Overview** · `I`  
  The route-generation → route-cache → emission pipeline.
- `routing/randomized-toroidal-wildfirst.md` — **RandomizedToroidalWildFirstPaths** · `C`  
  The path generator.
- `routing/route-table-generation.md` — **Route-Table Generation** · `C`  
  physmap + GetPhysicalToLogicalMapping3D.
- `routing/get-static-path.md` — **GetStaticPath & Multipod** · `C`  
  Inter-pod route emission.
- `routing/toroidal-route-cache.md` — **ToroidalRouteCache** · `C`  
  The 85-file binarypb decode + per-codename split.
- `routing/route-cache-decompress.md` — **Route-Cache Decompress** · `C`  
  CompressedToroidalRouteCache proto→map.
- `routing/route-cache-dedup.md` — **Route-Cache Dedup** · `C`  
  RouteCacheDeduplicator key + type dispatch.
- `routing/route-cache-codec.md` — **Route-Cache Codec** · `C`  
  BitEncoder / DecodePathFromBits / TopologyRotationHelper.
- `routing/create-routing-schedule.md` — **CreateRoutingSchedule Solver** · `C`  
  The priority-queue hop-assignment + PointerType enum.
- `routing/net-router-pipeline.md` — **net_router Pipeline** · `C`  
  The software-pipeline callbacks + Transfer construction.
- `routing/unicast-route-emission.md` — **Unicast Route Emission** · `C`  
  The layer above DmaDestinationRoutingTableEntryMapper.
- `routing/get-distances.md` — **GetDistances** · `C`  
  The nK twisted-torus distance metric.

### Twisted torus geometry
- `twist/overview.md` — **Overview** · `I`  
  The twisted-torus topology and why it exists.
- `twist/buildstrategy.md` — **TwistedTorusND::BuildStrategy** · `C`  
  Phase order + RingLocation construction.
- `twist/twist-predicate-orientation.md` — **Twist Predicate & Orientation** · `C`  
  Orientation enum 4/5/6 negative-axis folding.
- `twist/replica-group-2phase.md` — **2-Phase Replica-Group Construction** · `C`  
  The reduce-scatter / all-gather group construction.
- `twist/shape-folds.md` — **Shape Folds** · `C`  
  K_K_2K / K_2K_2K / K_2K_NK twist-shape cases.
- `twist/get-replica-pair-3d.md` — **GetReplicaPair3DOnTwistedTorus** · `C`  
  The coordinate fold.
- `twist/megacore-even-odd.md` — **Megacore Even/Odd Split** · `C`  
  The split rationale.
- `twist/get-tiebreak.md` — **GetTiebreak** · `C`  
  The literal-nK routing tiebreak.
- `twist/sc-side-twist.md` — **SC-Side Twist** · `C`  
  GetPhase0/1Cores + EstimatePhysicalLinksUsed.

### ICR node-fabric
- `routing/icr-node-fabric-dma.md` — **ICR Node-Fabric DMA Bands** · `C`  
  trace_point_ids 48/50/51/91 timeline source.
- `routing/nf-descriptor.md` — **nf_descriptor (27-field)** · `C`  
  The Node-Fabric DMA descriptor record.

## Part XIII — On-Pod Collectives & Barriers (30)

How a collective is decomposed, offloaded, and synchronized over the fabric (XII). The SparseCore-offload path bridges to IX.

### Collective algorithms
- `collectives/overview.md` — **Overview** · `I`  
  The strategy picker and the algorithm family.
- `collectives/strategy-nd-picker.md` — **SelectNDStrategy** · `C`  
  The collective-algorithm picker + degraded-axis handling.
- `collectives/binomial-recursive-doubling.md` — **Binomial / Recursive-Doubling** · `C`  
  The per-rank partner schedule.
- `collectives/allreduce-hierarchical-pincer.md` — **AllReduce Hierarchical / Pincer** · `C`  
  The multi-phase 0x101 path + pincer fusion.
- `collectives/allgather-nd-ring.md` — **AllGather ND-Ring** · `C`  
  GetShardIndex/GetOffset + the 2D/3D selector.
- `collectives/alltoall-tables.md` — **AllToAll Tables** · `C`  
  GenerateAllToAllTables → ConstantMapper.
- `collectives/reduce-scatter.md` — **ReduceScatter** · `C`  
  The reduce-scatter decomposition.
- `collectives/constant-mapper.md` — **ConstantMapper** · `C`  
  Compile-time collective constant-pool tags + SMEM reads.
- `collectives/degraded-axis.md` — **Degraded-Axis Ingest** · `C`  
  TpuDegradedAxesProto fault-tolerant path.

### SparseCore-offload collectives
- `collectives/sc-offload-config-builder.md` — **SC-Offload Config Builder** · `C`  
  ConstructConfigForCollectiveUniDirNDGroups.
- `collectives/hierarchical-kind.md` — **HierarchicalKind** · `C`  
  AllGather/AllReduce/ReduceScatter OffloadConfig structs.
- `collectives/tensor-split-ndplane.md` — **Tensor-Split / ND-Plane** · `C`  
  tensor_split_factor / NumScOffloadDevices + NDPlaneInfo.
- `collectives/physical-core-placement.md` — **Physical-Core Placement** · `C`  
  physical_core_indices per-color mapping.
- `collectives/sc-core-selection-offload.md` — **SC Core-Selection (Offload)** · `C`  
  The assignment cost + resource model.
- `collectives/get-remote-memref.md` — **get_remote_memref** · `C`  
  Cross-chip address composition.
- `collectives/start-remote-dma.md` — **StartRemoteDma** · `C`  
  The all-to-all producer + SubsliceToFullSliceGlobalCoreId.

### SFLAG & barriers
- `barrier/overview.md` — **Overview** · `I`  
  The sync-flag-based barrier model.
- `barrier/special-purpose-sync-flags.md` — **SpecialPurposeSyncFlags** · `C`  
  The FromProto runtime sink + overlay semantics.
- `barrier/per-codename-compiler-reserved.md` — **Per-Codename compiler_reserved SFLAG** · `C`  
  The literal {base, count} integers.
- `barrier/barrier-coloring.md` — **BarrierColoring** · `C`  
  The greedy graph-coloring engine.
- `barrier/barrier-to-sflag-binding.md` — **Barrier → SFLAG Number Binding** · `C`  
  The compiler-barrier → hardware-SFLAG number map.
- `barrier/global-barrier-window.md` — **Global-Barrier SFLAG Window** · `C`  
  GetGlobalBarrierSyncFlagNumber consumers.
- `barrier/replica-barrier.md` — **Replica (type-2) Barrier** · `C`  
  The REPLICA barrier lowering.
- `barrier/tensorcore-barrier.md` — **TensorCore Barrier** · `C`  
  InitializeOnScs lookup-callback.
- `barrier/tree-barrier-vsync.md` — **Tree-Barrier Vsync** · `C`  
  net_util actuation + InfoTable indexing.
- `barrier/infer-barrier-config.md` — **InferBarrierConfig** · `C`  
  The per-gen SFLAG map source.
- `barrier/remote-sflag-encoders.md` — **Per-Gen Remote-SFLAG Encoders** · `C`  
  GetRemoteSyncFlagEncoderRegistry + chip-id map.

### Higher-level
- `collectives/megacore-fusion.md` — **Megacore Fusion** · `I`  
  The megacore collective fusion.
- `collectives/fp8-quantized-collective.md` — **FP8 Quantized Collective** · `C`  
  The quantized-collective dispatch path. _src: #1339_
- `collectives/spmd-link-count-cost.md` — **SPMD Link-Count Cost** · `C`  
  The link-count divisor + full collective cost-formula set.

## Part XIV — Megascale (Multi-Host / DCN) (21)

The data-center-network layer above on-pod ICI: cross-host rendezvous, fleet metadata, and error aggregation.

- `megascale/overview.md` — **Overview** · `I`  
  DCN vs ICI; what Megascale orchestrates.
- `megascale/bootstrap/overview.md` — **Bootstrap: Overview** · `C`  
  The rendezvous overview.
- `megascale/bootstrap/coordinator-election.md` — **Bootstrap: Coordinator Election** · `C`  
  The coordinator-election logic.
- `megascale/bootstrap/worker-registration.md` — **Bootstrap: Worker Registration** · `C`  
  Worker registration with the coordinator.
- `megascale/bootstrap/topology-exchange.md` — **Bootstrap: Topology Exchange** · `C`  
  The cross-host topology exchange.
- `megascale/bootstrap/ici-handoff.md` — **Bootstrap: ICI Handoff** · `C`  
  Handoff to the ICI fabric.
- `megascale/bootstrap/convergence.md` — **Bootstrap: Convergence** · `C`  
  Convergence detection.
- `megascale/bootstrap/failure-handling.md` — **Bootstrap: Failure Handling** · `C`  
  Bootstrap failure handling.
- `megascale/bootstrap/tpunetd-relationship.md` — **Bootstrap: tpunetd Relationship** · `C`  
  Relationship to the tpunetd daemon.
- `megascale/fleet-metadata/overview.md` — **Fleet Metadata: Overview** · `C`  
  The fleet-metadata schema overview.
- `megascale/fleet-metadata/topology-model.md` — **Fleet: Topology Model** · `C`  
  The fleet topology model.
- `megascale/fleet-metadata/host-identity.md` — **Fleet: Host Identity** · `C`  
  Host identity fields.
- `megascale/fleet-metadata/global-addressing.md` — **Fleet: Global Addressing** · `C`  
  Global addressing scheme.
- `megascale/fleet-metadata/ici-vs-dcn.md` — **Fleet: ICI vs DCN** · `C`  
  The ICI/DCN distinction.
- `megascale/fleet-metadata/slice-shape.md` — **Fleet: Slice Shape** · `C`  
  Slice-shape encoding.
- `megascale/fleet-metadata/bootstrap-exchange.md` — **Fleet: Bootstrap Exchange** · `C`  
  The bootstrap data exchange.
- `megascale/fleet-metadata/barrier-error-usage.md` — **Fleet: Barrier & Error Usage** · `C`  
  How fleet metadata feeds barriers/errors.
- `megascale/fleet-metadata/field-decode.md` — **Fleet: Field Decode** · `C`  
  Field-by-field decode.
- `megascale/cross-host-barrier.md` — **Cross-Host Barrier** · `C`  
  The Megascale barrier primitive.
- `megascale/error-aggregator.md` — **ErrorAggregator** · `C`  
  Wire format, scope, retention, dedup.
- `megascale/tpunetd-protocol.md` — **tpunetd Protocol** · `C`  
  The daemon protocol.

## Part XV — Profiling & Telemetry (22)

How libtpu emits XPlane traces and hardware telemetry. Per-generation trace payloads have distinct on-wire formats.

- `profiling/overview.md` — **Overview** · `I`  
  XPlane, the trace pipeline, the codec families. _src: P-2-32_
- `profiling/tpu-profiler-abi.md` — **TpuProfiler ABI** · `C`  
  The profiler C surface.
- `profiling/pjrt-profiler-extension.md` — **PJRT_Profiler Extension** · `C`  
  PLUGIN_Profiler_Api.
- `profiling/xplane-xstat-traceme.md` — **XPlane / XStat / TraceMe Emission** · `C`  
  The emit path. _src: P-2-32_
- `profiling/tpu-telemetry-proto.md` — **tpu_telemetry.proto** · `C`  
  Field-by-field decode.
- `profiling/xevent-metadata-ids.md` — **XEvent Metadata IDs** · `C`  
  The profiler event catalog.
- `profiling/xstat-metadata-ids.md` — **XStat Metadata IDs** · `C`  
  The stat/attribute catalog.
- `profiling/trace-entries-coder.md` — **TraceEntriesCoder** · `C`  
  The fixed-width device-trace codec.
- `profiling/riegeli-trace-container.md` — **riegeli Trace Container** · `C`  
  Framing + timebase clock-domain conversion.
- `profiling/per-devicetype-struct.md` — **Per-DeviceType Profiler Struct** · `C`  
  The 0x448-byte master device table.
- `profiling/kdevicetypeinfo-producer-readers.md` — **kDeviceTypeInfo Producer / Readers** · `C`  
  The roofline readers.
- `profiling/tracepoints-master-registry.md` — **TracePoints Master Registry** · `C`  
  trace_point_id → {family, subscriber}.
- `profiling/trace-entry-to-xevent.md` — **TraceEntry → XEvent/XStat** · `C`  
  The TpuXLineBuilder last hop.
- `profiling/task-proto.md` — **Task Proto** · `C`  
  Device clock-rates + chip/host identity + GtcSpan offset.
- `profiling/payload-jxc-legacy.md` — **Payload: jxc Legacy** · `C`  
  The 16-bit trace_point_id namespace.
- `profiling/payload-vfc-vlc-gfc.md` — **Payload: vfc / vlc / gfc** · `C`  
  Per-gen payload field maps.
- `profiling/payload-sc-band.md` — **Payload: SparseCore Band** · `C`  
  SCS/TEC/TAC profiler payloads.
- `profiling/payload-uhi-oci-ici-dma.md` — **Payload: UHI/OCI/ICI/DMA** · `C`  
  The high-value trace-point bit-decodes.
- `profiling/icr-dma-timeline-band.md` — **ICR DMA-Timeline Band** · `C`  
  The 48/50/51/91 rendering.
- `profiling/jxc-dma-hbmmux-brnperf.md` — **jxc DMA / HbmMux / brn_perf** · `C`  
  The jellyfish DMA bands.
- `profiling/v7x-perf-counters.md` — **v7x Perf-Counters** · `C`  
  The hardware-counter name resolver + firmware/DVFS telemetry.
- `profiling/dma-endpoint-rendering.md` — **DMA Endpoint Rendering** · `C`  
  SrcMem/DstMem/Opcode enums + XEvent rendering.

## Part XVI — Configuration & Compile Knobs (16)

Every flag, env var, and compile knob, and how they resolve. The TpuCompilationEnvironment is the 1,121-field master config object.

- `config/overview.md` — **Overview** · `I`  
  The flag/knob/env taxonomy.
- `config/xla-flag-atlas.md` — **xla_* Flag Atlas** · `C`  
  The full option-name catalog.
- `config/flag-families.md` — **Flag Families** · `C`  
  jf/pf/vf/gf/sc/msa/lhs prefixes.
- `config/env-vars.md` — **Environment Variables** · `I`  
  The env-var catalog. _src: W005_
- `config/tpu-compilation-environment.md` — **TpuCompilationEnvironment (1121 fields)** · `C`  
  Overview + DefaultDebugOptions.
- `config/tce-field-dictionary-a.md` — **TCE Field Dictionary (A)** · `C`  
  Fields part 1.
- `config/tce-field-dictionary-b.md` — **TCE Field Dictionary (B)** · `C`  
  Fields part 2.
- `config/tce-field-offsets-defaults.md` — **TCE Field-Offsets & Flag Defaults** · `C`  
  field#→offset + ABSL-flag defaults.
- `config/debugoptions-proto.md` — **xla.DebugOptions (290 fields)** · `C`  
  The complete proto table.
- `config/default-debugoptions.md` — **Default DebugOptions** · `C`  
  The effective defaults.
- `config/autoproto-autoor-resolution.md` — **AutoProto / AutoOr Resolution** · `C`  
  The per-knob AUTO resolver bodies.
- `config/autoor-parse-grammar.md` — **AutoOr Parse Grammar** · `C`  
  ParseAutoOrFromString (the XLA_FLAGS ingest).
- `config/autoor-unparse.md` — **AutoOr Unparse** · `C`  
  AbslUnparseFlag reverse-text.
- `config/autoproto-message-arms.md` — **AutoProto Message-Arms** · `C`  
  The 12 message-arm sub-message defaults + SET path.
- `config/registry-mediated-flags.md` — **Registry-Mediated Flags** · `C`  
  enable_lem_scheduler / explicit_evict_memory_limit_kib.
- `config/flag-prefix-dispatch.md` — **TpuVersion-Aware Flag-Prefix Dispatch** · `O`  
  The per-gen flag-prefix routing. _src: #1171 (open)_

## Part XVII — Appendices (20)

Reference tables, the source-traceability index, and the open-frontier register.

- `appendix/llo-opcode-table.md` — **LloOpcode Table (462)** · `C`  
  The full enum with categories. _src: P-2-25_
- `appendix/llvmtpu-intrinsic-table.md` — **LlvmTpu Intrinsic Table (1356)** · `C`  
  The full `tpu_*` intrinsic list.
- `appendix/memory-space-table.md` — **MemorySpace Table (17)** · `C`  
  The full enumeration. _src: P-2-01_
- `appendix/dispatch-table-taxonomy-full.md` — **Dispatch-Table Taxonomy (full)** · `C`  
  All 19 classes + the 40,313-table TSV. _src: P-2-06_
- `appendix/filewrapper-toc-catalog.md` — **filewrapper_toc Catalog (61)** · `C`  
  Every embedded runtime resource. _src: P-2-07_
- `appendix/protodesc-cold-catalog.md` — **protodesc_cold Catalog (760)** · `C`  
  Every embedded FileDescriptorProto. _src: P-2-07_
- `appendix/rtti-namespace-census.md` — **RTTI Namespace Census** · `C`  
  The full 160,351-entry breakdown.
- `appendix/reconstructed-proto-index.md` — **Reconstructed-Proto Index** · `C`  
  Every proto recovered from the descriptor pool.
- `appendix/error-status-codes.md` — **Error / Status Codes** · `C`  
  The status-code catalog.
- `appendix/flag-catalog-full.md` — **Flag Catalog (full TSV)** · `C`  
  The machine-readable flag list.
- `appendix/symbol-namespace-index.md` — **Symbol Namespace Index** · `I`  
  The namespace population map. _src: W014, W003_
- `appendix/per-gen-comparison-matrix.md` — **Per-Gen Master Comparison Matrix** · `C`  
  bundle/lanes/MXU/XLU/IAR/SFLAG/EUP/DID, six gens, one page.
- `appendix/evidence-anchor-index.md` — **Evidence-Anchor Index** · `I`  
  page → source-findings file → binary VA, the full traceability map. _src: (this corpus)_
- `appendix/source-corpus-map.md` — **Source-Corpus Map** · `I`  
  The P-2/P-3/W raw-findings file → part assignment. _src: W001, W034_
- `appendix/open-frontier-register.md` — **Open-Frontier Register** · `I`  
  What is NOT yet recovered (cmem-load/sparsity edges; sparsity slot #1092; NOP canonical #1096; flag-prefix #1171).
- `appendix/cross-reference-graph.md` — **Cross-Reference Dependency Graph** · `I`  
  The inter-page dependency web. _src: G001–G003_
- `appendix/binary-layout.md` — **Binary Layout Reference** · `C`  
  Segments, anchor symbols, scale vs ptxas/nvlink/cicc. _src: W023, W027_
- `appendix/methodology-deep.md` — **Methodology (Deep)** · `I`  
  Extraction pipeline, FLIRT, sidecar inventory. _src: W001, W031_
- `appendix/glossary-extended.md` — **Extended Glossary** · `I`  
  Every acronym + internal class name. _src: W014_
- `appendix/changelog.md` — **Changelog** · `I`  
  Book revision history vs binary version.

---

## Appendix highlights

Two appendix pages are the connective tissue that make this book auditable and the per-generation story coherent:

- **Evidence-Anchor Index** (`appendix/evidence-anchor-index.md`) — the full page → source-findings file (`P-2-*`/`P-3-*`/`W*`) → binary VA mapping. Every claim in the book is traceable back through this index to a specific function address and a specific raw-findings file. This is what separates a reimplementation reference from a blog post.
- **Per-Gen Master Comparison Matrix** (`appendix/per-gen-comparison-matrix.md`) — one page, six generations (Jellyfish/Dragonfish/Pufferfish/Viperfish/Ghostlite/6acc60406) × every per-generation constant: bundle size, lane/sublane count, MXU dimensions, XLU/IAR counts, per-tier memory (VMEM/SMEM/SFLAG/CMEM/HBM), accelerator-core type, and the cost-model class trio (LatencyTable/CycleTable/Performance) that carries the per-gen EUP push→pop latency. Ties together the per-gen material otherwise distributed across Parts IV, VI, and VII.

## Open frontier

Pages graded `O` are not yet backed by a completed raw-findings file: the sparsity slot encoding (task #1092), per-gen NOP canonical encoding (#1096), TpuVersion-aware flag-prefix dispatch (#1171), and the cmem-load/sparsity edges. The [Open-Frontier Register](appendix/open-frontier-register.md) tracks these. Everything else is backed by a `C` (confirmed) or `I` (inferred) raw-findings file.
