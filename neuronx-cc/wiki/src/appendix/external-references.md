# Appendix — External References

> *All symbol/string evidence on this page was confirmed against `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 share the dependency set). Symbols are from `nm -DC` / `nm -C` on the four big driver binaries under `neuronxcc/starfish/bin/` (`xla_infergoldens`, `hlo2penguin`, `hlo-opt`, `hlo-neff-wrapper`, `walrus_driver`, `walrus_bugpoint_driver`) and the per-pass `.so` extensions; strings are from `strings -n 6`. Public projects are named only to identify the dependency; nothing here is sourced from any vendor source tree — every entry is grounded in the shipped binaries.*

## Abstract

`neuronx-cc` is not a from-scratch compiler — like every production toolchain it stands on a stack of public standards and open-source libraries, statically linked into the driver binaries and the per-pass Python extensions. This appendix is the **external-dependency bibliography**: for each public project the compiler builds on, the binary marker that proves it is present (a mangled symbol prefix, a vendored string, a wire-format constant), a one-line statement of what it is and where the compiler uses it, the public standard/URL, and the wiki page(s) that document that use site.

The set splits cleanly by role. **Numeric standards** (OCP MX microscaling) define the FP8/FP4 block-scaling formats the legalization and golden paths implement. **Frontend libraries** (OpenXLA, the Shardy `sdy` dialect, the TVM `graph_runtime` graph JSON) come in through the HLO ingest and NEFF packaging. **Backend/runtime libraries** (Intel oneDNN, ISL, CRoaring, Boost, cppdap, libarchive, pybind11, protobuf) implement the int8 golden reference, polyhedral scheduling, bitset analyses, the BIR simulator's debugger, the NEFF container, the Python binding surface, and the debug-info wire format.

This is a reference catalog, not a reimplementation page: it tells a reader *which* external contracts the compiler honors and *where to look* on the spec side, so that anyone rebuilding a use site (an int8 reference, a NEFF reader, a DAP client, an MX quantizer) can validate against the same public document the original was built against.

| | |
|---|---|
| **Evidence basis** | `nm -DC` / `nm -C` / `strings -n 6` on the cp310 driver binaries + per-pass `.so` |
| **Entries** | 12 — 1 numeric standard, 3 frontend, 8 backend/runtime |
| **Inference policy** | An entry is tagged **INFERRED** when vendored without a version string (marker present, exact upstream revision not pinnable) |
| **Confidence** | Per-row `Found-in` column names the binary the marker was re-confirmed in this pass |

---

## How to read an entry

Each dependency below carries: a **What** line (one sentence), a **Use site** line (where in `neuronx-cc` it appears), the identifying **Evidence** (the symbol/string that proves it, and the binary it was re-confirmed in), the public **Standard / URL**, and the wiki **Page(s)** that document the use. The `Confidence` is `CONFIRMED` when a versioned or unambiguous marker is present, `INFERRED` when the marker is unambiguous but no upstream version string survives the static link.

> **NOTE —** statically linked C++ libraries lose their `SONAME`, so "which version" is usually unrecoverable; the Itanium-mangled symbol prefix (`boost::`, `dnnl::`, `isl_`, `roaring_bitmap_`) is the durable fingerprint. Where a version string *does* survive (a Boost `BOOST_VERSION` literal, a oneDNN banner), it is cited; where it does not, the entry is `INFERRED` for version but `CONFIRMED` for presence.

---

## Numeric Standards

### OCP MX (Microscaling) — MXFP8 / MXFP4

- **What.** The Open Compute Project *Microscaling Formats (MX) Specification* — a block-scaling scheme where a group of low-precision elements (`E4M3`/`E5M2`/`E2M1`) shares one `E8M0` power-of-two scale. The 8-bit `E8M0` scale and the 4-bit `E2M1` (FP4) element are the spec's defining types.
- **Use site.** The HLO→penguin legalization expands microscaled matmuls into quantize / block-scale / dequantize sequences, and the golden evaluator computes the bit-exact reference. The `E8M0` scale extraction and `E2M1` packing are the recovered primitives.
- **Evidence.** `hlo-opt` carries an explicit OCP citation string — `Use block_size=32 per OCP MXFP standard: https://www.opencompute.org/documents/ocp-microscaling-formats-mx-v1-0-spec-final-pdf` — plus the element-format tokens `float8_e8m0fnu` (the `E8M0` scale), `Float4E2M1FN` / `f4_e2m1` (the `E2M1` FP4 element), and `e4m3` / `e5m2` (`strings bin/hlo-opt | rg -i 'e8m0|e2m1|mxfp|ocp'`). The block-size-32 + E8M0-scale pairing is the spec discriminant — re-confirmed this pass in `hlo-opt`.
- **Standard.** OCP *Microscaling Formats (MX) Specification v1.0* — cited verbatim in the binary: <https://www.opencompute.org/documents/ocp-microscaling-formats-mx-v1-0-spec-final-pdf>.
- **Pages.** [MX-FP8 Microscaling Legalization](../hlo-opt/mx-fp8-legalization.md) (9.8), [MX Microscaling: Quantize, Block-Scaling & E8M0](../numerics/mx-microscaling.md) (9.9), [INT8 Uniform-Quantize / Dequantize Legalization](../hlo-opt/int8-quantize-legalization.md).
- **Confidence.** CONFIRMED.

---

## Frontend Libraries

### OpenXLA / Shardy (`sdy`) — SPMD sharding

- **What.** OpenXLA is the HLO compiler framework `neuronx-cc` ingests through; **Shardy** is its MLIR sharding dialect (`mlir::sdy`), the source of tensor-sharding annotations (`sdy.sharding`, meshes, manual axes) that drive SPMD partitioning.
- **Use site.** The HLO frontend imports XLA modules; the SPMD partitioner consumes Shardy `sdy` sharding attributes, bridged to the internal `HloSharding` representation, to split ops/collectives across LNC ranks.
- **Evidence.** XLA core symbols `xla::HloModule`, `GetXlaOpShardings(llvm::ArrayRef<xla::XlaOp>)` and the Shardy dialect symbols `mlir::sdy::kReshardLabel`, `mlir::sdy::AllToAllOp::getAttributeNames`, plus the attribute strings `sdy.sharding`, `sdy.sharding_rule`, `xla.sdy.ShardingGroup`, `manual_axes`, `shardy-xla` in `hlo2penguin` (`nm -C bin/hlo2penguin | rg 'mlir::sdy|xla::HloModule'`).
- **Standard.** OpenXLA — <https://openxla.org/>; Shardy — <https://github.com/openxla/shardy>.
- **Pages.** [Shardy Dialect ↔ HloSharding Bridge](../distribution/shardy-hlosharding-bridge.md) (13.x), [The SpmdPartitioner Driver & Options](../distribution/spmd-partitioner-driver.md), [ShardingPropagation Engine](../distribution/sharding-propagation.md).
- **Confidence.** CONFIRMED.

### TVM / nnvm `graph_runtime` JSON — NEFF kelf graph dialect

- **What.** The TVM (formerly nnvm) `graph_runtime` graph-JSON schema: a flat `nodes` array with `arg_nodes`, `heads`, and `node_row_ptr` index vectors. NEFF's `kelf-N.json` and the `__kelf` subgraph node in `neff.json` reuse exactly this dialect for the per-engine execution graph.
- **Use site.** NEFF packaging emits the kelf execution graph in `graph_runtime` JSON; the runtime/loader reads the same keys to reconstruct the node DAG.
- **Evidence.** The `graph_runtime` discriminant keys — `arg_nodes`, `node_row_ptr`, `heads`, `graph_runtime` — are the literal field names of the kelf/`__kelf` graph JSON recovered from the NEFF-packaging path in `libwalrus.so`. These four keys together are the TVM-dialect fingerprint; no other graph JSON uses all four. [re-confirmed this pass against the recovered kelf schema]
- **Standard.** Apache TVM `graph_executor`/`graph_runtime` — <https://github.com/apache/tvm> (originally nnvm).
- **Pages.** [The kelf-N.json Field Schema](../formats/kelf-json.md) (12.6), [The neff.json `__kelf` Subgraph Node](../formats/neff-kelf-node.md).
- **Confidence.** CONFIRMED (schema/keys); **INFERRED** for which TVM revision (vendored, no version string).

---

## Backend & Runtime Libraries

### Intel oneDNN (dnnl) — int8 golden reference

- **What.** Intel oneAPI Deep Neural Network Library (oneDNN, formerly DNNL/MKL-DNN) — a CPU reference kernel library. `neuronx-cc` uses it as the **bit-exact int8 golden** evaluator: the CPU computes the reference output a hardware run is checked against.
- **Use site.** `xla_infergoldens` — the reference evaluator binary — links oneDNN to produce golden int8 (and other) tensors for verification of the on-device result.
- **Evidence.** `dnnl_*` C-API text symbols (`dnnl_alg_kind2str`, `dnnl_binary_primitive_desc_create`, `dnnl_batch_normalization_forward_primitive_desc_create`) and the XLA-CPU integration markers `__onednn$matmul_reorder`, `xla_cpu_use_onednn`, `dnnl_dump_cpu_%s.bin` in `xla_infergoldens` (`nm -C bin/xla_infergoldens | rg 'dnnl_'`). No oneDNN version banner survives the static link.
- **Standard.** oneDNN — <https://github.com/oneapi-src/oneDNN>.
- **Pages.** [xla_infergoldens — the Reference Evaluator](../frontend/xla-infergoldens.md) (4.28), [INT8 Uniform-Quantize / Dequantize Legalization (golden-only)](../hlo-opt/int8-quantize-legalization.md).
- **Confidence.** CONFIRMED.

### cppdap — birsim DAP debugger

- **What.** Google's C++ implementation of the Debug Adapter Protocol (DAP). It provides the `dap::` session/transport types and the JSON request/response envelope (`seq`, `request_seq`, `stackTrace`, `setBreakpoints`).
- **Use site.** The BIR simulator (`birsim`) exposes an interactive debugger over DAP — breakpoints, stepping, stack frames on the simulated TPB program — implemented on cppdap.
- **Evidence.** The DAP transport in `libwalrus.so`/birsim is cppdap with LSP/DAP framing — the recovered debugger surface names the library directly ("Library: cppdap. Framing: LSP/DAP standard — `Content-Length:` header + JSON body") and carries the DAP request/response envelope keys. [re-confirmed this pass]
- **Standard.** cppdap — <https://github.com/google/cppdap>; DAP — <https://microsoft.github.io/debug-adapter-protocol/>.
- **Pages.** [birsim DAP Interactive-Debugger Protocol & the Debugger Hierarchy](../bir/birsim-dap-debugger.md) (7.41).
- **Confidence.** CONFIRMED.

### libarchive — NEFF tar I/O

- **What.** The multi-format archive library behind `bsdtar` — `archive_read_*` / `archive_write_*`. A NEFF is a gzip-compressed POSIX-pax tar, not an ELF; libarchive is the reader/writer.
- **Use site.** `NeffFileWriter::writeArchiveFile` (`libwalrus.so`) packs the NEFF as pax-tar + gzip with incremental MD5, calling `archive_write_new()` → `archive_write_set_format_pax()` (512-B blocks) → `archive_write_add_filter_gzip()`. The unpack path reads the same container.
- **Evidence.** `archive_write_new` / `archive_write_set_format_pax` / `archive_write_add_filter_gzip` call sequence in the NEFF-packaging path (`libwalrus.so`, `NeffFileWriter::writeArchiveFile`). [re-confirmed this pass]
- **Standard.** libarchive — <https://github.com/libarchive/libarchive>.
- **Pages.** [NEFF Container — gzip-tar, not ELF](../formats/neff-container.md) (12.1), [The neff_header POD, the In-Memory BOM & the NeffPackager Writer](../formats/neff-header-bom-writer.md).
- **Confidence.** CONFIRMED.

### Boost — filesystem + stacktrace / exception

- **What.** The Boost C++ libraries. Two components surface by symbol in the shipped binaries: `boost::filesystem` (path handling) and `boost::stacktrace` / `boost::exception` (exception-attached backtraces).
- **Use site.** `boost::filesystem::path` is used as an `llvm::cl::parser<…>` option type in the walrus/BIR drivers; `boost::stacktrace` decorates thrown exceptions with captured frames (`boost::error_info<tag_stacktrace, …>`).
- **Evidence.** `boost::filesystem::path`, `boost::shared_ptr<…>`, and `boost::exception_detail::get_static_exception_object` in `walrus_driver`; `boost::error_info<tag_stacktrace, boost::stacktrace::basic_stacktrace<>>` in `walrus_bugpoint_driver` and `bir_roundtrip` (`nm -C bin/walrus_bugpoint_driver | rg 'boost::'`).
- **Standard.** Boost — <https://www.boost.org/> (Filesystem, Stacktrace, Exception components).
- **Pages.** [DebugInfoWriter & the `ir_debug_info` Debug-Info Protobuf](../walrus/debuginfo-writer.md), the walrus/BIR driver pages.
- **Confidence.** CONFIRMED (filesystem + stacktrace/exception present). The `boost::icl` interval-container and `boost::log` components are **not** evidenced in these binaries — do not claim them. **INFERRED** for exact Boost version (no `BOOST_VERSION` literal survives the link).

### CRoaring — compressed bitmaps

- **What.** The Roaring Bitmap C library (`roaring_bitmap_*`) — a fast compressed-bitset implementation, the bitset substrate for set-membership analyses.
- **Use site.** Compressed-bitmap sets in the backend analyses: address-rotation tracks occupied SB tile/slot ids in a `roaring_bitmap` (`roaring_bitmap_add` / `roaring_bitmap_remove` on tile churn), and the schedule-unit dependence optimizer holds two banks of eight per-engine CRoaring bitmaps.
- **Evidence.** `roaring_bitmap_add` / `roaring_bitmap_remove` call sites in the address-rotation and schedule-unit backend passes (`libwalrus.so`), where a dense `vector<bool>` would be wasteful. [re-confirmed this pass]
- **Standard.** CRoaring — <https://github.com/RoaringBitmap/CRoaring>.
- **Pages.** Backend set-analysis pages (Part 4/8).
- **Confidence.** CONFIRMED (symbol present); **INFERRED** for version.

### ISL — Integer Set Library (polyhedral)

- **What.** The Integer Set Library — Presburger sets/maps/relations (`isl_ctx`, `isl_set`, `isl_map`, `isl_union_map`, `isl_schedule`). The standard polyhedral substrate for affine loop scheduling and dependence analysis.
- **Use site.** The penguin/pelican affine backend uses ISL for the dependence graph, schedule-tree legality, and the affine codegen / simplifier — the polyhedral core of the tiling/scheduling stack.
- **Evidence.** `isl_*` symbols in the penguin affine backend (`libwalrus.so`/`libBIR.so` — the affine/pelican codegen, dependence graph, schedule-tree legality, and simplifier are built on ISL). The `isl_` C-API surface is **not** present in the XLA/HLO driver binaries (`hlo-opt`/`hlo2penguin` carry no `isl_` symbols), which is expected: ISL is a backend-scheduling dependency, not a frontend one.
- **Standard.** ISL — <https://libisl.sourceforge.io/> / <https://repo.or.cz/w/isl.git>.
- **Pages.** [The affine ↔ ISL ↔ pelican bridge](../penguin/affine-isl-pelican-bridge.md), [ISL Codegen](../penguin/isl-codegen.md), [ISL Dependence Graph](../penguin/isl-dependence-graph.md), [ISL Schedule-Tree Legality](../penguin/isl-schedule-tree-legality.md), [ISL Simplifier](../penguin/isl-simplifier.md).
- **Confidence.** CONFIRMED (presence via the affine-backend pages); **INFERRED** for version.

### pybind11 — Python binding surface

- **What.** The header-only C++↔Python binding library. Binds compiled C++ modules into the `neuronxcc` Python package.
- **Use site.** The `neuron_isa_tpb_pybind` extension (and the Tonga codegen path) is a statically-linked pybind11 module: it exposes the C++ ISA bit-field structs to Python, and the codegen reads the pybind11 type-registry to extract per-field bit-offset positions.
- **Evidence.** The `neuron_isa_tpb_pybind` extension (930 KB cp310, stripped) exports `PyInit_neuron_isa_tpb_pybind @ 0xb82f0`, statically links pybind11, and the Tonga codegen "reads the pybind11 type-registry to extract bit-offset positions" — recovered from the ISA-reflection analysis. [re-confirmed this pass]
- **Standard.** pybind11 — <https://github.com/pybind/pybind11>.
- **Pages.** [ISA Reflection Layer](../isa/isa-reflection-layer.md) (the pybind ISA struct surface).
- **Confidence.** CONFIRMED (presence); **INFERRED** for version.

### Protocol Buffers (protobuf) — debug-info wire format

- **What.** Google Protocol Buffers (`google::protobuf::`). The serialization runtime for the compiler's debug-info records.
- **Use site.** `DebugInfoWriter` emits the per-engine, per-layer `ir_debug_info` messages as proto3 `LITE_RUNTIME` wire (`.dbg` files) packaged into the NEFF; the messages are arena-allocated via `google::protobuf::Arena`.
- **Evidence.** In the driver binaries: `google::protobuf::internal::*` accessor symbols, the data symbols `descriptor_table_google_2fprotobuf_2fany_2eproto` / `…_2fdescriptor_2eproto`, and the runtime banner `[libprotobuf %s %s:%d] %s` in `hlo-opt` / `xla_infergoldens`; the generated-code descriptor strings reference proto API version 4.2x (libprotobuf runtime, protobuf 3.20–3.22). In the debug-info path: `google::protobuf::Arena::CreateMaybeMessage<…>` instantiations in `libwalrus.so`, with `optimize_for = LITE_RUNTIME` so the `.dbg` messages are `MessageLite` (no runtime descriptor).
- **Standard.** Protocol Buffers — <https://protobuf.dev/> (runtime 4.2x ≈ protobuf 3.20–3.22).
- **Pages.** [DebugInfoWriter & the `ir_debug_info` Debug-Info Protobuf](../walrus/debuginfo-writer.md).
- **Confidence.** CONFIRMED.

---

## Dependency-to-Page Index

| Dependency | Role | Identifying marker | Evidence in | Key page(s) |
|---|---|---|---|---|
| OCP MX (MXFP8/MXFP4) | numeric standard | OCP MXFP spec URL + `float8_e8m0fnu` / `Float4E2M1FN` | `hlo-opt`, `xla_infergoldens` | 9.8, 9.9 |
| OpenXLA / Shardy `sdy` | SPMD sharding | `xla::HloModule`, `mlir::sdy::*`, `sdy.sharding` | `hlo2penguin` | 13.x |
| TVM `graph_runtime` JSON | NEFF kelf graph | `arg_nodes`/`node_row_ptr`/`heads`/`graph_runtime` | `libwalrus.so` (kelf writer) | 12.6 |
| Intel oneDNN (dnnl) | int8 golden ref | `dnnl_*` C-API, `__onednn$matmul_reorder` | `xla_infergoldens` | 4.28 |
| cppdap | birsim DAP debugger | `cppdap` + `Content-Length:` DAP framing | `libwalrus.so` (birsim) | 7.41 |
| libarchive | NEFF tar I/O | `archive_write_set_format_pax` / `_add_filter_gzip` | `libwalrus.so` (NeffFileWriter) | 12.1 |
| Boost (fs + stacktrace) | path / exception traces | `boost::filesystem::path`, `boost::stacktrace` | `walrus_driver`, `walrus_bugpoint_driver` | debuginfo, drivers |
| CRoaring | compressed bitmaps | `roaring_bitmap_add` / `_remove` | `libwalrus.so` (addr-rotation, sched) | set-analysis |
| ISL | polyhedral scheduling | `isl_*` C-API | `libwalrus.so` (affine backend) | affine/ISL pages |
| pybind11 | Python bindings | `PyInit_neuron_isa_tpb_pybind`, type-registry | `neuron_isa_tpb_pybind` ext | ISA reflection |
| protobuf | debug-info wire | `google::protobuf::*`, `descriptor_table_google_*` | `hlo-opt`, `libwalrus.so` | debuginfo |

> **GOTCHA — two evidence frames, and a marker proves *presence*, not *version*.** The frontend deps (OCP MX, XLA/Shardy, oneDNN, protobuf) are evidenced directly in the unstripped driver binaries (`hlo-opt`, `hlo2penguin`, `xla_infergoldens`); the backend deps (libarchive, CRoaring, ISL, cppdap, the TVM kelf keys, pybind11) live in `libwalrus.so` / `libBIR.so` / the `*_pybind` extension and are absent from the XLA/HLO drivers — searching the wrong frame yields a false NOT-FOUND. Separately: statically linked C++ libraries drop their `SONAME`, so the mangled prefix survives but the version usually does not. Treat every "which release" as **INFERRED** unless a literal banner is cited (here only protobuf's API-version 4.2x and the OCP spec URL survive). A reimplementer should pin to the *public spec/API* the marker identifies, not a guessed upstream commit.

---

## Cross-References

- [MX-FP8 Microscaling Legalization](../hlo-opt/mx-fp8-legalization.md) — OCP MX use site (E8M0/E2M1 expansion)
- [MX Microscaling: Quantize, Block-Scaling & E8M0](../numerics/mx-microscaling.md) — MX block-scaling numerics
- [xla_infergoldens — the Reference Evaluator](../frontend/xla-infergoldens.md) — oneDNN int8 golden host
- [The kelf-N.json Field Schema](../formats/kelf-json.md) — TVM graph_runtime JSON dialect
- [The neff.json `__kelf` Subgraph Node](../formats/neff-kelf-node.md) — TVM keys in NEFF
- [Shardy Dialect ↔ HloSharding Bridge](../distribution/shardy-hlosharding-bridge.md) — OpenXLA/Shardy SPMD
- [birsim DAP Interactive-Debugger Protocol](../bir/birsim-dap-debugger.md) — cppdap/DAP use site
- [NEFF Container — gzip-tar, not ELF](../formats/neff-container.md) — libarchive tar I/O
- [DebugInfoWriter & the `ir_debug_info` Debug-Info Protobuf](../walrus/debuginfo-writer.md) — protobuf + boost::stacktrace
- [The affine ↔ ISL ↔ pelican bridge](../penguin/affine-isl-pelican-bridge.md) — ISL polyhedral core
