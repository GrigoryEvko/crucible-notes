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

Every page in this book is derived from static analysis of `libtpu.so` — its symbol table, disassembly, and decompilation. The complete input set that the analysis ran against, down to the byte, is inventoried in the [Source-Corpus Map](appendix/source-corpus-map.md); the methodology that produced and consumed it is described in [Methodology](methodology.md).

> This page preserves the narrative overview. For the complete content index, see [libtpu Internals Index](index.md).
