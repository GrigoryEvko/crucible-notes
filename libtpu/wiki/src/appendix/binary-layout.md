# Binary Layout Reference

> *All addresses on this page apply to `libtpu.so` build-id `89edbbe81c5b328a958fe628a9f2207d` (build tag `libtpu_lts_20260413_b_RC00`), from the `libtpu-0.0.40-cp314` wheel (781,691,048 bytes, 884,832 functions). Other builds will shift every boundary.*

## Abstract

`libtpu.so` is a 745 MiB statically-linked monolith: one shared object that absorbs the PJRT plugin entry layer, the full XLA compiler, the MLIR framework with three dozen dialects, the TPU instruction-set codecs, the oneDNN CPU fallback kernels, the profiling and collective subsystems, and the abseil/protobuf/LLVM/gRPC runtime — all link-time-merged into a single `.text`. A reverse-engineer who single-steps into this binary lands somewhere inside a flat 299.9 MiB instruction region with no module boundaries and no per-library segments. (This build does retain its full `.symtab`, so a symbol name is usually available — but a name alone does not tell you which of the link-merged subsystems you are in.) This page is the map that answers the only question that matters at that moment: **given an address, which subsystem am I in?**

The organizing observation is that the link order is not random. Functions from the same translation-unit cluster, and translation units from the same subsystem cluster into broad, contiguous *address bands* inside `.text`. The PJRT/runtime entry code sits at the very bottom of `.text` (the linker placed the exported-symbol translation units first); the MLIR dialect machinery fills the broad middle; the oneDNN CPU kernels form a tight island; and the TPU ISA codecs (`asic_sw::deepsea::{gxc,vxc,pxc}`) pile up densest in the top quarter. The bands overlap at their edges — a single namespace can have stragglers 200 MiB away from its centroid — so this map describes *density centroids and dominant occupants*, not hard partitions. Treat a band assignment as "most code here is X," verified by sampling, not as a guarantee that every byte is X.

This is the address-band navigation appendix. It complements but does not duplicate the ELF section table: [`forensics/elf-anatomy.md`](../forensics/elf-anatomy.md) owns the authoritative section header dump, and [`subsystem-map.md`](../subsystem-map.md) owns the subsystem-to-entry-point catalog. This page is the bridge between them — the VA-to-subsystem lookup that turns a raw address into a starting hypothesis.

For navigation, the contract is:

- The **section skeleton**: which sections are allocatable code, which are large-model read-only data, and where each begins. The address you hold is meaningless until you know which section it falls in.
- The **`.text` band partition**: the dominant namespace per ~15–30 MiB band, anchored by named exports and by the per-band namespace census.
- The **boundary anchors**: named symbols (`GetPjrtApi`, `GetLibtpuSdkApi`, `TpuExecutor_Init`, RTTI typeinfo strings) whose addresses pin band edges so the map can be re-verified against any future build.

| | |
|---|---|
| **Binary** | `libtpu.so`, build-id `89edbbe81c5b328a958fe628a9f2207d`, build tag `libtpu_lts_20260413_b_RC00` |
| **File size** | 781,691,048 B (745.5 MiB), 51 named sections (48 allocatable) |
| **Function count** | 884,832 total · 877,976 inside `.text` |
| **`.text` range** | `0x0e63c000` .. `0x21217484` (299.9 MiB) |
| **`.lrodata` range** | `0x01884a00` .. `0x084931d0` (108.1 MiB, large read-only data) |
| **`.rodata` range** | `0x084a0000` .. `0x0be8af28` (57.9 MiB) |
| **Lowest code addr** | `0x0e635524` (`.init`) |
| **Highest code addr** | `0x21217480` (end of `.text`) |
| **PJRT entry anchor** | `GetPjrtApi` @ `0x0e6a83a0` (low `.text`) |
| **SDK entry anchor** | `GetLibtpuSdkApi` @ `0x109028c0` (mid `.text`) |

---

## Section Skeleton

Before any band lookup, place the address in its section. The linker laid the file out in a strict ascending order — read-only data first, then a small ancillary-code cluster, then the giant `.text`, then the writable trailer. The full 51-row header dump lives in [`forensics/elf-anatomy.md`](../forensics/elf-anatomy.md); the rows below are the ones that bound the address bands.

| Section | VA Start | VA End | Size | Type | Holds |
|---|---|---|---|---|---|
| `.rela.dyn` | `0x00009170` | `0x01881da0` | 24.5 MiB | RELA | Dynamic relocations (RELRO) |
| `.lrodata` | `0x01884a00` | `0x084931d0` | 108.1 MiB | PROGBITS (`l`) | Large read-only data: vtables, typeinfo, jump tables, constant pools |
| `.rodata` | `0x084a0000` | `0x0be8af28` | 57.9 MiB | PROGBITS | Strings, small constants, format tables |
| `protodesc_cold` | `0x0be8af30` | `0x0c1bf0b0` | 3.2 MiB | PROGBITS | Cold protobuf descriptor tables |
| `.gcc_except_table` | `0x0c1bf0b0` | `0x0c2cc634` | 1.0 MiB | PROGBITS | C++ EH landing-pad tables |
| `.eh_frame_hdr` / `.eh_frame` | `0x0c2cc634` | `0x0e635524` | ~35 MiB | PROGBITS | Unwind tables (C++ EH; `.eh_frame` alone is ~28.7 MiB) |
| `.init` / `.text.hot` | `0x0e635524` | `0x0e63738e` | ~8 KiB | PROGBITS (X) | Init stub + 6 hot-promoted functions |
| `google_malloc` | `0x0e6373c0` | `0x0e63bab2` | 18.2 KiB | PROGBITS (X) | TCMalloc fast-path (72 functions) |
| **`.text`** | **`0x0e63c000`** | **`0x21217484`** | **299.9 MiB** | **PROGBITS (X)** | **All primary code — the band map below** |
| `.text.startup` | `0x21217490` | `0x213818e4` | 1.4 MiB | PROGBITS (X) | Static-initializer constructors (2,886 funcs) |
| `.text.unlikely` | `0x21381900` | `0x213e9d69` | 0.4 MiB | PROGBITS (X) | Cold/error-path code (2,798 funcs) |
| `google_init_cold` | `0x213e9d80` | `0x213efe71` | 24.8 KiB | PROGBITS (X) | Cold init (125 funcs) |
| `.plt` | `0x213f0830` | `0x213f25d0` | 7.5 KiB | PROGBITS (X) | PLT stubs for imported libc/libm/libdl symbols |
| `.data.rel.ro` | `0x215f81a0` | `0x22048b30` | 10.3 MiB | PROGBITS (W) | Relocated read-only: vtable pointers, RTTI graph |
| `.data` | `0x222551c0` | `0x224bf798` | 2.4 MiB | PROGBITS (W) | Initialized globals |
| `.bss` | `0x224c3880` | `0x22598c30` | 0.8 MiB | NOBITS | Zero-init globals |
| `.ldata` / `.lbss` | `0x22798c30` | `0x2285a180` | ~0.7 MiB | PROGBITS / NOBITS (`l`) | Large writable / large zero-init data |

> **NOTE —** the `l` (large) section flag on `.lrodata`, `.ldata`, and `.lbss` is the x86-64 large-code-model marker. These sections are placed outside the ±2 GiB signed-displacement window of `.text`, so the compiler addresses them with full 64-bit `movabs` rather than RIP-relative `lea`. When you see a `movabs` loading a constant from `0x018xxxxx`–`0x084xxxxx`, it is reaching into `.lrodata` — almost always a vtable, a typeinfo record, or a large dispatch/jump table.

> **GOTCHA —** `.text.startup` (`0x2121...`), `.text.unlikely`, and `google_init_cold` all sit *above* the main `.text` end (`0x21217484`), not interleaved with it. A static constructor that builds an MLIR dialect registry lives at `0x2122xxxx`, ~290 MiB away from the dialect's runtime methods in the `0x010x`–`0x012x` band. Do not assume an address near a function is in the same translation-unit cluster if it crosses into the startup/cold sections.

---

## The `.text` Address Bands

`.text` is partitioned below into ten bands (B0 a ~15 MiB entry band, B1–B9 ~30 MiB each). Each row gives the band's VA range, the section (always `.text` here), the dominant namespace/subsystem by function count, the approximate live-function count in the band, and a confidence. The dominant-occupant column is the per-band namespace census: the leading C++ namespace among the functions whose address falls in that band, sampled from the symbol table. "~funcs" is the count of recovered functions whose entry address lands in the band.

| Band | VA Range | Section | Dominant Subsystem / Namespace | ~Funcs |
|---|---|---|---|---|
| **B0** Runtime / PJRT entry | `0x0e63c000` .. `0x0f53a2a0` | `.text` | TPU runtime driver (`asic_sw::driver::deepsea`) + PJRT/`Tpu*` exports + MLIR op registration | ~40,000 |
| **B1** MLIR core + SPIR-V | `0x0f53a2a0` .. `0x11336000` | `.text` | MLIR framework (`mlir::RegisteredOperationName`, `mlir::spirv`), LLVM support | ~115,000 |
| **B2** XLA / SparseCore + MLIR | `0x11336000` .. `0x14030fc0` | `.text` | MLIR op machinery + `xla::tpu::sparse_core`, `mlir::linalg` | ~120,000 |
| **B3** MLIR dialects (dense) | `0x14030fc0` .. `0x15e2d500` | `.text` | `mlir::RegisteredOperationName` peak, `mlir::stablehlo`, Eigen tensor evaluators | ~92,000 |
| **B4** MLIR linalg/bufferization | `0x15e2d500` .. `0x17c29a40` | `.text` | `mlir::linalg`, `mlir::bufferization`, `mlir::stablehlo`, Eigen | ~99,000 |
| **B5** LLVM backend + MLIR | `0x17c29a40` .. `0x19a25f80` | `.text` | LLVM (`llvm`, `llvm::cl`, SelectionDAG), residual MLIR interfaces | ~65,000 |
| **B6** oneDNN CPU kernels | `0x19a25f80` .. `0x1b8224c0` | `.text` | `dnnl::impl::cpu` (CPU fallback JIT/reference kernels) | ~39,000 |
| **B7** STL/variant + driver | `0x1b8224c0` .. `0x1d61ea00` | `.text` | `std::__u` instantiations, `asic_sw::driver::deepsea`, residual oneDNN/MLIR | ~58,000 |
| **B8** TPU codecs (vxc/gxc) | `0x1d61ea00` .. `0x1f41af40` | `.text` | TPU ISA codecs `asic_sw::deepsea::{vxc,gxc,pxc}`, `xla` literals | ~128,000 |
| **B9** TPU codecs (gxc peak) | `0x1f41af40` .. `0x21217484` | `.text` | `asic_sw::deepsea::gxc` density peak, `pxc`/`vxc`, protobuf arena | ~146,000 |

> **QUIRK —** the namespace `asic_sw::deepsea::gxc` (the TPU "core-X" ISA codec family, ~60,700 functions in `.text`) appears across a ~197 MiB code span (`0x1391cd40` .. `0x1fe6f7a0`), but its *density* climbs toward the top of `.text`: ~13.7k functions land in B8 and ~46k in B9. A reimplementer who keys "codec land starts at X" off the first `gxc` function will be ~150 MiB too low. Use the **density centroid** (high `.text`, B8–B9), not the first occurrence.

> **NOTE —** the codec namespaces (`gxc`/`vxc`/`pxc`) cluster *ascending* with a density peak in the top quarter of `.text` (`~0x1d6xxxxx`–`0x21217484`); B8–B9 are the codec bands. The lower codec stragglers begin around `0x14xxxxxx`, far below where the cluster actually sits — key off the per-band census, not the first occurrence.

---

## Band Detail

Each band below names the anchor symbols that pin its edges and the most reliable landmarks for orientation. Anchors are real exported or RTTI symbols whose addresses survive in the `.symtab`; they are the re-verification points for checking this map against a future build.

### B0 — Runtime / PJRT Entry (`0x0e63c000`–`0x0f53a2a0`)

The bottom of `.text`. The linker front-loads the translation units that carry the library's exported `Tpu*` C API and the PJRT plugin surface, so this is where every externally-callable entry point lives. It is also where the `asic_sw::driver::deepsea` runtime/driver namespace is densest (~18,800 functions in the band) and where MLIR operation-registration constructors begin.

| Anchor Symbol | Address | Role |
|---|---|---|
| `GetPjrtApi` | `0x0e6a83a0` | PJRT plugin vtable accessor — the canonical entry point |
| `ConfigureDistributedTpuOp_DoWork` | `0x0e8cd400` | Distributed-TPU configuration op |
| `TpuExecutor_Init` | `0x0eab90c0` | TPU executor lifecycle |
| `TpuCompiler_New` | `0x0eabc4a0` | TPU compiler factory |

If you land between `0x0e6a0000` and `0x0eb00000`, you are almost certainly in the PJRT/executor lifecycle layer documented in [`subsystem-map.md`](../subsystem-map.md). The `Tpu*` exports are the only undecorated (non-mangled) names in this region, which makes them the fastest visual anchors in a disassembly listing.

### B1–B5 — The MLIR / XLA / LLVM Middle (`0x0f53a2a0`–`0x19a25f80`)

The broad central mass of `.text`, ~60% of all code. It is dominated by the MLIR framework: `mlir::RegisteredOperationName` alone spans `0x0ea2c120`–`0x1d8c90c0` and accounts for ~130,000 functions — the single largest namespace in the binary. These are the per-operation registration, verification, folding, and interface-dispatch methods generated by MLIR's ODS (Operation Definition Specification) for each of the ~36 dialects (`spirv`, `linalg`, `stablehlo`, `tosa`, `vector`, `bufferization`, `affine`, and the TPU-specific `tpu`, `llo`, `sparse_core` dialects — see the namespace census in [`forensics/llvm-mlir-manifest.md`](../forensics/llvm-mlir-manifest.md)).

Interleaved with MLIR are the XLA compiler proper (`xla::jellyfish`, `xla::tpu::sparse_core`, `xla::HloEvaluator`), the LLVM backend used for host/CPU codegen (`llvm`, `llvm::SelectionDAG`, `llvm::cl` command-line registration), and Eigen tensor-evaluator template instantiations. The sub-band tilt:

| Sub-band | Lean | Anchor namespace span |
|---|---|---|
| B1 `0x0f53...`–`0x1133...` | MLIR core + SPIR-V dialect | `mlir::spirv` `0x1126faa0`+ |
| B2 `0x1133...`–`0x1403...` | XLA SparseCore + MLIR | `xla::tpu::sparse_core` `0x0f858f20`+ |
| B3 `0x1403...`–`0x15e2...` | MLIR op-name peak + stableHLO | `mlir::stablehlo` `0x0eba7a60`+ |
| B4 `0x15e2...`–`0x17c2...` | MLIR linalg + bufferization | `mlir::linalg` `0x10a7f2e0`+ |
| B5 `0x17c2...`–`0x19a2...` | LLVM backend + residual MLIR | `llvm` cluster |

> **NOTE —** the `xla::megascale` (collectives), `xla::HloEvaluator`, and `tensorflow` namespaces are *scattered* across the entire middle rather than banded — their function spans run the full width of `.text` (e.g. `tensorflow` spans `0x0e63d5c0`–`0x20cccd80`). Do not expect a "collectives band"; collective and profiling code is sprinkled by translation-unit adjacency, not gathered. Use the [`subsystem-map.md`](../subsystem-map.md) entry-point table for these, not an address range.

### B6 — oneDNN CPU Kernels (`0x19a25f80`–`0x1b8224c0`)

A genuinely tight island. The `dnnl::impl::cpu` namespace clusters at `0x1a3cb2a0`–`0x1bf83d40` and dominates this band (~16,000 functions in B6, ~28,000 across its span). This is the Intel oneDNN (DNNL) CPU backend — the reference and JIT-generated convolution/matmul/reorder kernels XLA falls back to for host-side execution. Because oneDNN is a self-contained third-party library with little cross-call into the rest of libtpu, its translation units linked contiguously, producing the cleanest band boundary in the binary.

> **QUIRK —** the dense `dnnl::impl::cpu` cluster makes B6 the easiest band to recognize by content alone: heavy use of AVX-512 register-blocked inner loops, `std::__u::__function` trampolines (~6,200 in-band, the oneDNN kernel-dispatch closures), and almost no `mlir`/`xla` symbols. If a disassembly window is wall-to-wall vectorized GEMM micro-kernels, you are in B6.

### B7 — STL / Variant Glue + Driver (`0x1b8224c0`–`0x1d61ea00`)

A transitional band with no single dominant subsystem. It is led by `std::__u` STL template instantiations (`std::variant` visitors, `std::function` targets) and a second concentration of `asic_sw::driver::deepsea` driver code, with residual oneDNN and MLIR spillover. This band is where the binary transitions from "framework/library" code to "TPU-specific codec" code, and its mixed census reflects that. Confidence is MEDIUM: the leading occupant (`std::__u`, ~5,600) is glue, not a subsystem, so address-to-subsystem inference here is weaker than in the clean bands.

### B8–B9 — TPU ISA Codecs (`0x1d61ea00`–`0x21217484`)

The top quarter of `.text` and the heart of the TPU-specific machinery: the instruction-set codecs that encode/decode TPU bundles for the three core families — `vxc` (vector core), `gxc` (general/grid core), and `pxc` (processing core). These are the `asic_sw::deepsea::{vxc,gxc,pxc}` namespaces, and they are densest here: B8 holds ~20k `vxc` + ~14k `gxc` + ~1.5k `pxc`; B9 holds ~46k `gxc` (its peak) + ~4.9k `pxc` + ~4.4k `vxc`. The codec class hierarchy is template-heavy — `SparseCoreTecCodecBase`, `TensorCoreCodecBase`, and the `platforms_deepsea::jellyfish::isa::{Encoder,Decoder}Base` templates — which is why function counts explode here: every codec instantiation generates a full set of encode/decode/validate methods.

| Anchor (RTTI typeinfo string, `.rodata`) | Address | Pins |
|---|---|---|
| `asic_sw::deepsea::vxc::isa::TensorCoreCodecBase<…>` | `0x0406f1d8` | `vxc` TensorCore codec family |
| `asic_sw::deepsea::gxc::gfc::isa::TensorCoreCodecBase<…>` | `0x0406fd20` | `gxc` TensorCore codec family |
| `platforms_deepsea::jellyfish::isa::EncoderBase<…>` | `0x044fdc6e` | ISA encoder base template |
| `platforms_deepsea::jellyfish::isa::DecoderBase<…>` | `0x044fe6b9` | ISA decoder base template |
| `tpu::TpuCompactionIsaEmitterCodegen` | `0x0abe3e38` | ISA-emitter codegen RTTI |

> **NOTE —** the RTTI typeinfo *strings* above live in `.rodata`/`.lrodata` (low addresses, `0x04xxxxxx`–`0x0axxxxxx`), not in `.text`. They are the names of classes whose *methods* execute in B8–B9. The `.data.rel.ro` vtable pointer arrays (`0x215f81a0`+) wire the two together; see [`forensics/rtti-vtable-census.md`](../forensics/rtti-vtable-census.md) for the full RTTI graph. Use the typeinfo strings to confirm a band's identity, but expect the executing code 200+ MiB higher.

---

## Cross-Section Bands

A few subsystems span sections rather than living inside `.text`. They are listed here so an address outside `.text` still resolves.

| Region | VA Range | Section(s) | Subsystem |
|---|---|---|---|
| Vtable / typeinfo pool | `0x01884a00`–`0x084931d0` | `.lrodata` | RTTI typeinfo strings, vtable bodies, jump/dispatch tables (large code model) |
| String / constant pool | `0x084a0000`–`0x0be8af28` | `.rodata` | Log messages, op-name strings, flag tables, format strings |
| Protobuf descriptors | `0x0be8af30`–`0x0c1bf0b0` | `protodesc_cold` | Cold protobuf reflection metadata |
| Unwind tables | `0x0c2cc634`–`0x0e635524` | `.eh_frame*` | C++ exception unwind (~35 MiB) |
| Static constructors | `0x21217490`–`0x213818e4` | `.text.startup` | 2,886 init functions (dialect/flag/proto registration) |
| Cold / error paths | `0x21381900`–`0x213efe71` | `.text.unlikely`, `google_init_cold` | 2,923 cold-promoted functions |
| Relocated RO data | `0x215f81a0`–`0x22048b30` | `.data.rel.ro` | Vtable pointer arrays, RTTI base-class lists, the RTTI graph backbone |

> **GOTCHA —** the second ELF in the wheel, `sdk.so` (21.5 MiB, the Python `sdk` module), is a *separate* shared object with its own independent VA space. Do not confuse `sdk.so`'s addresses with `libtpu.so`'s — they overlap numerically but mean nothing across the boundary. Note the naming trap: the `GetLibtpuSdkApi` C-ABI export lives *inside `libtpu.so`* (at `0x109028c0`), not in `sdk.so` — `sdk.so` is a Python module that neither exports nor imports it. The two-binary split is documented in [`forensics/two-binary-split.md`](../forensics/two-binary-split.md). All addresses on *this* page are `libtpu.so` only.

---

## Re-Verification Recipe

To re-derive this map against a different `libtpu.so` build, the procedure that produced it:

```text
1. readelf -SW libtpu.so
     -> .text VA start/end, .lrodata/.rodata bounds, large-section flags
2. Histogram recovered function entry addresses into N equal .text sub-bands
     -> raw function-density profile (the ~Funcs column)
3. For each sub-band, census the leading demangled C++ namespace token
     -> dominant-subsystem column
4. Pin band edges with named anchors:
     readelf -sW libtpu.so | grep -E 'GetPjrtApi|GetLibtpuSdkApi|TpuExecutor_Init'
     readelf -sW libtpu.so | grep -iE 'CodecBase|EncoderBase|DecoderBase|IsaEmitter'
5. Cross-check density centroids vs first/last occurrence of each namespace
     -> distinguishes the cluster (centroid) from stragglers (span edges)
```

The invariants that should survive across builds (even as absolute addresses shift): PJRT/runtime is *lowest*, MLIR fills the *middle*, oneDNN is a *tight island* near the lower-third, and the TPU codecs cluster *highest*. The absolute boundaries on this page are build-specific; the *ordering* is structural and follows the link order of the subsystems.

---

## Cross-References

- [ELF Anatomy](../forensics/elf-anatomy.md) — owns the authoritative 51-row section header table; this page's section skeleton is the navigation subset
- [Forensics Overview](../forensics/overview.md) — the binary-shape orientation: size, symbol counts, stripped state
- [Custom Sections](../forensics/custom-sections.md) — `protodesc_cold`, `google_malloc`, `google_init_cold`, `linkarr_upb_AllExts` and the other non-standard sections
- [Subsystem Map](../subsystem-map.md) — owns the subsystem-to-entry-point catalog; use it for collectives/profiling code that is scattered rather than banded
- [LLVM/MLIR Manifest](../forensics/llvm-mlir-manifest.md) — the dialect census that fills bands B1–B5
- [Embedded Library Atlas](../forensics/embedded-library-atlas.md) — the third-party libraries (oneDNN, abseil, protobuf, gRPC) statically linked into `.text`
- [RTTI / Vtable Census](../forensics/rtti-vtable-census.md) — the `.data.rel.ro` vtable graph that wires `.lrodata` typeinfo to B8–B9 codec code
- [Two-Binary Split](../forensics/two-binary-split.md) — why `sdk.so` is a separate VA space from `libtpu.so`
- [Symbol Namespace Index](symbol-namespace-index.md) — the full namespace-to-count table underlying the per-band census
