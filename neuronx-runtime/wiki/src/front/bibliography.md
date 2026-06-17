# Bibliography & External References

> **Pinned to:** `libnrt.so.2.31.24.0` (package `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, BuildID[sha1] `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`) · sibling collectives binary `libnccom.so.2.31.24` (package `aws-neuronx-collectives 2.31.24.0-1a31ba186`, BuildID[sha1] `9c00176c081788c9435d27d11bb40e92495463f0`) · GPSIMD microcode `libnrtucode_extisa.so`.
>
> **Toolchains** (from `.comment`): `GCC: (GNU) 14.2.1 20250110 (Red Hat 14.2.1-10)` · `rustc version 1.91.1 (ed61e7d7e 2025-11-07)` · `clang version 21.1.0-rc2`.
>
> **Part 0 — Front Matter** / REFERENCE · this is the public-upstream-pointer companion to [forensics/vendored-sbom](../forensics/vendored-sbom.md) (which carries the per-binary embedding evidence).

## Abstract

A reimplementer of the Neuron runtime does not start from a blank page. Most of the heavy lifting inside `libnrt.so` and its siblings is **public open-source code, statically vendored** — protobuf parses the trace wire format, simdjson parses the NEFF JSON metadata, libarchive unpacks the NEFF container, Abseil backs the C++ Swiss-table maps, and a Rust 1.91.1 toolchain plus a dozen registry crates drive the `neuron_rustime` symbolization path. The collectives library `libnccom.so` is a fork of NVIDIA NCCL; the GPSIMD pool microcode emits the Cadence Tensilica Vision-Q7 vector ISA. None of these is something a reimplementer should reverse by hand — each is a known upstream that should be *linked at the pinned version*, leaving the reverse-engineering effort for the genuinely first-party AWS logic. This page is the directory of those upstreams: each entry pairs a public homepage/repository with a one-line reason a reimplementer cares.

Every version on this page is fingerprinted from the shipped binaries themselves — a compiled-in version macro the runtime checks (`GOOGLE_PROTOBUF_VERSION 5026001`), an AWS Brazil package-cache path in `.debug_str` (`.../Protobuf/Protobuf-26.x.13.0/...`), a Rust sysroot commit hash (`/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb`), or a cargo registry source path (`.../serde_json-1.0.145/src`). This page does **not** re-derive that evidence: the per-binary, per-token embedding proof — which `readelf -p`/`strings`/`nm` token pins each row, and which two historical misreads were corrected (protobuf "v24", Rust "1.89") — lives in [forensics/vendored-sbom](../forensics/vendored-sbom.md). Treat this page as the *public-pointer view* and that page as the *embedding-evidence view*; the version numbers are identical by construction, and a `> **NOTE —**` at the foot states the single source of version truth.

## Vendored OSS upstreams

Each row is a public upstream statically linked into `libnrt.so` at the exact version fingerprinted from the binary (see [vendored-sbom §2](../forensics/vendored-sbom.md#2-the-sbom-table) for the per-row token). The **why** is the concrete job that upstream does inside the runtime — the reason a reimplementer links it rather than rewrites it.

| Project | Version | Role in runtime | Public link | Why it matters |
|---|---|---|---|---|
| **protobuf** (C++ runtime) | **5.26.1** (= 26.1) | Wire format for the `KaenaProfilerFormat` trace/profiling payloads (`ntff.pb.cc` / `neuron_trace.pb.cc`); arena allocator + descriptor DB | <https://github.com/protocolbuffers/protobuf> | The NTFF/trace `.pb` wire format is protobuf 26.x — regenerate accessors from the schema and link this exact runtime; `VerifyVersion` fatals on a version skew. |
| **utf8_range** | bundled with protobuf 26.x | UTF-8 validation of protobuf string fields | (in protobuf `third_party/utf8_range`) | No standalone version — ships inside protobuf 5.26.1; track it as a protobuf component, not a separate dependency. |
| **Abseil** | **LTS 20230802** | C++ support runtime *for* protobuf and first-party C++: cord/status/strings/log/synchronization/time/swisstable | <https://github.com/abseil/abseil-cpp> | The Swiss-table hash maps and the `absl::lts_20230802::` string/status types — protobuf 26.x pulls this LTS; ABI-coupled, so the LTS tag must match. |
| **simdjson** | **0.9.0** | Parses the embedded JSON metadata in a NEFF (`neff_json_parser`) | <https://github.com/simdjson/simdjson> | NEFF carries JSON metadata; simdjson's runtime arch dispatch (haswell/westmere/fallback) decodes it. Reproduce the parse against this version's DOM API. |
| **libarchive** | **3.8.0dev** | NEFF container reader: the NEFF is a gzip'd TAR — tar format + gzip filter | <https://github.com/libarchive/libarchive> | NEFF unpack *is* libarchive's gzip-tar reader; the container layout is libarchive's, not a custom format. Link 3.8.0dev, do not write a TAR reader. |
| **zlib** | **1.3.1** | `inflate` path backing the libarchive gzip filter; NEFF decompression | <https://zlib.net> | The deflate stream inside the NEFF gzip is decoded by zlib 1.3.1's `inflate`; it sits under libarchive. |
| **libstdc++** | GCC **14.2.1** (ABI source) | STL containers/strings | <https://gcc.gnu.org/onlinedocs/libstdc++/> | **Dynamically linked** (`DT_NEEDED libstdc++.so.6`), not a static blob — the `std::`/`__gnu_cxx::` symbols in `.text` are header-only template instantiations. Reproduce via any C++17 STL with the `GLIBCXX_3.4.22` ABI floor. |
| **Rust toolchain** (std/core/alloc) | **1.91.1** (`ed61e7d7e`) | The `neuron_rustime` side: backtrace/symbolization, JSON, hashing, MPMC, logging | <https://github.com/rust-lang/rust> | The Rust runtime half is built with this exact toolchain (commit `ed61e7d7e`, 2025-11-07); the crates below are its dependency tree. |
| serde_json | **1.0.145** | JSON serialize/deserialize on the Rust side | <https://crates.io/crates/serde_json> | The Rust trace/config JSON path (facade re-exports `serde_core` 1.0.228). |
| hashbrown | **0.15.5** | Swiss-table hash maps in Rust (`std::collections::HashMap` backend) | <https://crates.io/crates/hashbrown> | The Rust-side map implementation; pairs with Abseil's swisstable on the C++ side. |
| crossbeam-queue | **0.3.12** | Lock-free MPMC queue | <https://crates.io/crates/crossbeam-queue> | The concurrent ring used by the Rust runtime's event/work plumbing. |
| memchr | **2.7.5** | SIMD substring/byte search | <https://crates.io/crates/memchr> | Fast scanning in the Rust parsing/symbolization paths. |
| log | **0.4.28** | Rust logging facade | <https://crates.io/crates/log> | The Rust logger bridge the runtime emits through. |
| gimli | **0.32.0** | DWARF reader (backtrace symbolization) | <https://crates.io/crates/gimli> | Reads the binary's own DWARF for Rust backtraces. |
| addr2line | **0.25.0** | Address → source-line resolution | <https://crates.io/crates/addr2line> | Sits atop gimli/object for symbolized stack frames. |
| object | **0.37.3** | ELF/object-file parsing | <https://crates.io/crates/object> | Parses the ELF for the addr2line/gimli backtrace stack. |
| miniz_oxide | **0.8.9** | Pure-Rust DEFLATE (inflate/deflate) | <https://crates.io/crates/miniz_oxide> | The Rust-side compression path (distinct from the C zlib under libarchive). |
| adler2 | ~2.0.x (inferred) | Adler-32 checksum for miniz_oxide | <https://crates.io/crates/adler2> | miniz_oxide 0.8.9's checksum dependency — version inferred from the dep tree (MED), not an emitted cargo path. |
| rustc-demangle | **0.1.26** | Rust symbol demangling | <https://crates.io/crates/rustc-demangle> | Demangles Rust symbols in the backtrace output. |

> **NOTE —** the `neuron_rustime` sys_trace crate is **first-party AWS Rust**, not a vendored registry crate, and is deliberately absent above (no `index.crates.io/.../neuron_rustime-<ver>` path). It is owned by the trace/profiling part of the book, not by this bibliography.

> **GOTCHA —** `protobuf 26.1` and `protobuf 5.26.1` are the same library. Post-21.0 protobuf C++ uses the `5.x` versioning scheme, so `5.26` == `26.1`. The two AWS Brazil package labels (`Protobuf-26.x.13.0` runtime, `GoogleProtoBuf-5.x.4041.0` headers) are one generation under two names — not two protobuf versions. The `EDITION_2023` token a reader may meet is the protobuf Editions *feature ceiling* enum, **not** a version (a misread it caused is corrected in [vendored-sbom §3](../forensics/vendored-sbom.md#3-corrections)).

---

## Upstream forks

Two upstreams ship as **forks**, not pristine library links: the runtime carries AWS modifications, so a reimplementer needs the upstream base *and* an understanding of where the fork diverges. The divergence detail lives in the `nccom/` chapter; this section names the public base and the reason it was forked.

| Upstream | Fork base | Role | Public link | Why the fork diverges |
|---|---|---|---|---|
| **NCCL** | NCCL **2.31.24** (`+nrt2.0`; AWS source root `KaenaNCCL`) | The base of `libnccom.so` — the multi-node collectives library | <https://github.com/NVIDIA/nccl> | The fork hard-wires the collective to **RING** by construction: only one `ncclTopoGraph` (the ring template, `pattern=4`) is ever built, and the double-binary-tree builders (`ncclGetBtree`/`ncclGetDtree`) are retained as faithful ports but are **dead code** — zero callers, zero relocations. The cost model (`getAlgoInfo`/`ncclTopoTuneModel`) is removed entirely; topology is Neuron pod/UltraServer/KangaRing, not NCCL's tree/collNet. See [nccom/algorithm-tree](../nccom/algorithm-tree.md). |
| **aws-ofi-nccl** | the OFI/EFA transport plugin | The `net` plugin that carries NCCL traffic over AWS EFA via libfabric (OFI) | <https://github.com/aws/aws-ofi-nccl> | NCCL's pluggable `ncclNet` transport is supplied by the OFI plugin so collectives ride EFA/libfabric rather than IB/sockets — the inter-node transport a reimplementer must wire to reach AWS fabric. See [nccom/net-plugin](../nccom/net-plugin.md) and [nccom/transport-efa](../nccom/transport-efa.md). |

> **QUIRK —** the NCCL base is a *ring-only* fork. A reimplementer who ports upstream NCCL's algorithm chooser will diverge immediately: there is no runtime algorithm/protocol selection in `libnccom` (the `"LL/LL128/Simple/Tree/Ring"` name table at `.rodata 0x8be50` is referenced by no instruction — dead data). Protocol selection happens *outside* the library, in the Neuron compiler / NRT execution plan.

---

## ISA lineage

The GPSIMD pool microcode (`libnrtucode_extisa.so`) emits a **custom Tensilica TIE** ISA. Its lineage is a public Cadence configurable core plus a specific toolchain version; the `.tie` config is what makes the per-instruction encoding decode.

| Component | Identity | Role | Public reference | Why it matters |
|---|---|---|---|---|
| **Cadence Tensilica Vision-Q7** (Xtensa) IVP | Xtensa24 / XEA3, `Xm_ncore2gp` "Cairo" config, Customer ID 19270 (AWS); 512-bit SIMD (`simd16=0x20`), `vq7_isa=1`, dual/quad MAC, GatherScatter | The base ISA the `ivp_*` ("Instruction Vision Processing") GPSIMD vector intrinsics belong to | <https://www.cadence.com/en_US/home/tools/ip/tensilica-ip/vision-q7-dsp.html> | The 285 `ivp_*` mnemonics the pool kernels emit are a subset of the Vision-Q7 ISA's 1065 `IVP_*` ops; recognizing the ISA as stock Vision-Q7 means the datapath (vec/wvec/vbool/valign/gvr regfiles, FLIX bundles) is documented, not bespoke. See [gpsimd/xtensa-vision-q7](../gpsimd/xtensa-vision-q7.md). |
| **XtensaTools-14.09** | the Xtensa SDK version the toolchain ships (`xt-objdump` disassembler) | Decodes the carved Q7 ELF32 blobs into `ivp_*` bundles | (Cadence Tensilica Software Tools, version 14.09) | The runnable `xt-objdump` from this toolchain version is what disassembles the FLIX bundles; a reimplementer needs the matching tools version to decode the blobs. See [gpsimd/xtensa-toolchain](../gpsimd/xtensa-toolchain.md). |
| **the `.tie` config** | `libtie-core.so` / `libisa-core.so` / `core.xparm` (the AWS Vision-Q7 TIE description) | Defines the per-mnemonic opcode bit-fields (`OPCODEDEF`/`FIELDDEF`/`ENCODING`) | (Tensilica Instruction Extension description, shipped with the GPSIMD tools) | Without the TIE config the custom `IVP_*` ops are opaque opcodes; *with* it, every instruction decodes to exact `{fld[HI:LO]==const}` bit-fields and operand lanes. The `.tie` is what turns the ISA from a name list into a decodable encoding. See [gpsimd/xtensa-toolchain](../gpsimd/xtensa-toolchain.md). |

> **NOTE —** the in-blob proof that this is Vision-Q7 is the surviving TIE type `_TIE_xt_ivp32_xb_vec2Nx8U` (the 2N×8-bit vector register class) in the `cptc_decode_impl` instantiations, plus the `core.xparm` Vision flags (`vq7_isa=1`, `simd16=0x20`). The sequencer-core Xtensa TIE in `libncfw.so` is a *different* Xtensa config (MAC16 space, not Vision-IVP32) and is documented separately.

---

## Companion wikis

The Neuron stack is documented across four sibling books. This runtime wiki owns the on-device runtime (`libnrt.so`), collectives (`libnccom.so`), and GPSIMD microcode; the rest is owned by the companions below. Consult them at the boundary edges where this book hands off.

| Companion wiki | What it covers | When to consult it |
|---|---|---|
| **neuronx-cc** | The Neuron compiler: HLO/MLIR/BIR lowering, the NEFF producer, and the execution-plan / protocol decisions that this runtime *consumes* | When you need *why* a NEFF or collective plan looks the way it does — the runtime executes plans the compiler emits (e.g. collective protocol is chosen here, not in `libnccom`). |
| **neuron-platform** | The kernel driver, device topology, and the host/device platform layer | When tracing below the runtime: the driver ioctl surface, device memory, and the `KaenaDriver` boundary the runtime calls into. |
| **neuronx-gpsimd** | The GPSIMD tooling and the full Vision-Q7 TIE ISA reference (the `.tie`, `ncore2gp`, the complete `IVP_*` OPCODEDEF set) | When you need the *complete* ISA encoding beyond the 285 emitted ops this book catalogs — the authoritative TIE-XML reference lives there. |
| **neuronx-distributed** | The distributed-training framework that drives the collectives from above | When you need the caller's view of `libnccom`: how collective ops are issued from the training stack into the runtime. |

---

> **NOTE —** every version on this page is **fingerprinted from the shipped binaries**, never from a vendor source tree. The single source of version truth — which `readelf`/`strings`/`nm` token pins each row, the confidence grade, and the two corrected misreads (protobuf "v24", Rust "1.89") — is [forensics/vendored-sbom](../forensics/vendored-sbom.md). If a version on this page and that page ever disagree, the SBOM page (the embedding-evidence view) wins; this page is the public-pointer view and must track it.

## Cross-References

- [forensics/vendored-sbom](../forensics/vendored-sbom.md) — the per-binary embedding evidence: which token pins each version, with confidence grades (the source of version truth)
- [nccom/algorithm-tree](../nccom/algorithm-tree.md) — the NCCL fork's retained-but-dead tree builders; proof the algorithm is ring-only
- [nccom/net-plugin](../nccom/net-plugin.md) — the aws-ofi-nccl EFA/OFI transport plugin
- [gpsimd/xtensa-toolchain](../gpsimd/xtensa-toolchain.md) — the XtensaTools-14.09 toolchain and the `.tie` config that decodes the Vision-Q7 ISA
- [gpsimd/xtensa-vision-q7](../gpsimd/xtensa-vision-q7.md) — the Vision-Q7 ISA identification and regfile/FLIX model
- [appendix/symbol-versions](../appendix/symbol-versions.md) — the ELF symbol-version manifest (`GLIBCXX_*` / `CXXABI_*` floors) backing the libstdc++ ABI row
