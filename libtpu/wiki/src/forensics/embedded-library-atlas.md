# Embedded-Library Atlas

> *All byte offsets, sizes, and version strings on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (release marker `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes on disk). Other wheels will differ.*

## Abstract

`libtpu.so` is a single statically-linked PJRT plugin that carries an entire compiler, runtime, and ML stack inside one ELF. Its dynamic dependency list is six entries — `libm`, `libpthread`, `libdl`, `librt`, `libc`, and `ld-linux-x86-64` — with **no `libstdc++` and no `libgcc_s`**. Everything else, from the C++ standard library through LLVM/MLIR down to the `crc32` table, is compiled in. This page is the bill of materials: the authoritative inventory of which third-party libraries are embedded, the binary evidence that each is present, the version where the binary pins one, and the rough code footprint each occupies.

Unusually for a shipped artifact, this binary is **not stripped**. Its `.symtab` retains roughly 1.2 million defined, Itanium-mangled symbols. That changes the forensic method entirely: classic FLIRT signature matching — the technique you reach for against a stripped binary — is unnecessary here, because every function already announces its owning namespace (`absl::`, `mlir::`, `re2::`, `tcmalloc::`) in plain text. The atlas is therefore built by namespace bucketing over the demangled symbol pool, corroborated against `third_party/...` source-path literals baked into `.rodata` and against version-bearing banners. Three classes of evidence appear throughout: a **namespace marker** (a demangled symbol prefix, present at a file offset), a **version string** (a banner or path literal that pins a release), and a **symbol-family count** (how many distinct names a library contributes, from the IDA names sidecar). Where a raw count is cited, it is the number of symbol-table names whose text contains that family fragment.

The structure below leads with an at-a-glance bill of materials, then splits the detail into four families: the Google C++ core (Abseil, protobuf, tcmalloc, riegeli, RE2, farmhash), the math/ML layer (Eigen, oneDNN), the compression/crypto layer (zlib, zstd, Brotli, snappy, BoringSSL), and miscellany (ICU, gRPC, RE2's sibling utilities). LLVM and MLIR are the largest single occupants by a wide margin; they have their own page and are summarized here only as line items — see [LLVM/MLIR Manifest](llvm-mlir-manifest.md) for the commit pin and dialect inventory.

For a reimplementer the contract is:

- **The dependency posture** — what is dynamically linked (six libraries) versus statically embedded (everything else, including the C++ runtime), and how to confirm it from `DT_NEEDED`.
- **The detection method per library** — which namespace marker, path literal, or banner proves presence, and at which file offset, so the inventory can be reproduced and re-verified.
- **The version pins that the binary actually fixes** (oneDNN v3.3, ICU 78, protobuf editions, LLVM trunk commit) versus the ones it only floors (Abseil LTS, Eigen 3.4.x, gRPC), and why each falls where it does.

| | |
|---|---|
| **Binary** | `libtpu.so`, 781,691,048 bytes, ELF64 DYN x86-64 |
| **Build-id** | `89edbbe81c5b328a958fe628a9f2207d` (`.note.gnu.build-id`) |
| **Release marker** | `libtpu_lts_20260413_b_RC00` @ file offset `0x84A0E80` (139,071,104) |
| **Dynamic deps** | 6: `libm`, `libpthread`, `libdl`, `librt`, `libc`, `ld-linux-x86-64` |
| **Stripped?** | **No** — `.symtab` retains ~1.2 M mangled symbols; classification is by namespace, not FLIRT |
| **Toolchain** | `clang version 9999.0.0 (a70419505471…)` @ `0xAF59226`; Bazel `r4rca-2026.04.04-1 (mainline @894239244)` @ `0x84A12A0` |
| **Largest occupants** | LLVM + MLIR (see sibling page), then Abseil, oneDNN, libc++, Eigen |

---

## Bill of Materials

The table is the authoritative inventory. **Version evidence** is the strongest pin the binary supports: a path literal or banner where one exists, a feature-floor where only behavior is observable. **Footprint** is the order-of-magnitude share of code-plus-rodata symbol bytes the library's namespace owns — a coarse size signal, not a linker map. **Confidence** reflects how directly presence and version are observed: `CERTAIN` = namespace plus version string both present; `HIGH` = namespace family present, version inferred from features; `MEDIUM` = present but version unresolved.

The footprint scale, for orientation: **huge** ≳ 25 MB, **large** 5–25 MB, **medium** 1–5 MB, **small** 0.1–1 MB, **trace** < 0.1 MB.

| Library | Version evidence | Footprint | Confidence |
|---|---|---|---|
| **LLVM** (see [sibling](llvm-mlir-manifest.md)) | `LLVM version g3_____-trunk 8918319853fbdf…` @ `0xB1FA070` — trunk commit, no upstream tag | huge (~84 MB) | CERTAIN |
| **MLIR** (see [sibling](llvm-mlir-manifest.md)) | bundled with same LLVM trunk; `mlir::` namespace | huge (~72 MB) | CERTAIN |
| **Abseil** | `absl::log_internal::SetTimeZone` @ `0x8714E5B`; `absl::AnyInvocable` @ `0x188C785` → **≥ LTS 20240722**, ceiling open | huge (~29 MB) | HIGH |
| **Intel oneDNN (DNNL)** | path `third_party/intel_dnnl/v3_3/src/common` @ `0x85BEAE8` → **v3.3** | large (~25 MB) | CERTAIN |
| **libc++** | `std::__u::` ABI tag — google3 libc++, matches the LLVM commit | large (~18 MB) | HIGH |
| **Eigen** | `Eigen` namespace, 28,702 names; `Eigen::bfloat16` tensor ABI → **3.4.x branch**, exact tag open | large (~12 MB) | HIGH |
| **protobuf** | `EDITION_2024` @ `0x5EE8C1E`, `EDITION_2026` @ `0x5EE8C31`; `descriptor_table_protodef` @ `0x23DF7736` → **v32+ editions-era** | medium (~8 MB) | HIGH |
| **gRPC** | `grpc_core::` @ `0x188C4DE`; `chaotic_good` transport → google3-tip (no tagged release), **≥ v1.66 dev** | medium (~3 MB) | HIGH |
| **BoringSSL** | `BoringSSL` banner @ `0x857D84C`; `bssl::` / `EVP_`/`SSL_`/`X509_` C symbols | medium (~1.3 MB) | CERTAIN |
| **Brotli** | `BrotliDecoderDecompress` @ `0xA2869A1`; static-dictionary tables → ≥ 1.1, reached through riegeli | small (~0.85 MB) | HIGH |
| **zstd** | `ZSTD_compressBound` @ `0x270D623C`, `ZSTD_createDCtx` @ `0x8716BDF`; bundled as `third_party/zippy/` | small (~0.6 MB) | CERTAIN |
| **ICU** | data file `icudt78` @ `0x83FE04C`; namespace `icu_78` → **ICU 78** | small (~0.5 MB) | CERTAIN |
| **riegeli** | path `third_party/riegeli/` @ `0x85D7066`; `riegeli::` namespace | small (~0.3 MB) | CERTAIN |
| **snappy** | `snappy::` namespace @ `0xA1251E5` | trace (~18 KB) | CERTAIN |
| **RE2** | `re2::` @ `0x86679B9`, `re2::RegexpFlag`, `third_party/re2` | small (~0.12 MB) | CERTAIN |
| **zlib** | `zlib_rs` Rust-port symbols @ `0x852F25E`; `deflate`/`inflate`/`crc32` C symbols | small (~0.17 MB) | CERTAIN |
| **Xbyak** (oneDNN JIT) | `Xbyak` namespace @ `0xB38846D` | trace (~90 KB) | CERTAIN |
| **tcmalloc** | `tcmalloc::` namespace; custom `google_malloc` ELF section | trace (~55 KB code, allocator-only) | CERTAIN |
| **farmhash** | `farmhash` family (`farmhashna`/`uo`/`te`/`xo`/`mk`/`cc`) @ `0xFDCE579` | trace (~7 KB) | CERTAIN |

> **NOTE —** the footprint numbers are share of *named symbol bytes*, not segment sizes. They rank libraries correctly relative to one another but should not be read as exact `.text` contributions; a TU-local helper inside Abseil that lost its namespace prefix is counted in the anonymous residue, not against Abseil. Treat the scale buckets, not the parenthetical figures, as the reliable signal.

> **QUIRK —** the absence of `libstdc++.so.6` and `libgcc_s.so.1` from `DT_NEEDED` is the single most consequential fact on this page. The standard C++ runtime is statically linked as google3's libc++ build, identifiable by the `__u` inline-namespace ABI tag on every `std::` symbol (`std::__u::basic_string`, `std::__u::vector`). A reimplementer who assumes a system `libstdc++` is loaded will mismatch the string and exception ABIs. The companion `sdk.so`, by contrast, *does* declare `libstdc++.so.6` — the two binaries make opposite linking choices; see [Two-Binary Split](two-binary-split.md).

---

## Google C++ Core

These are the libraries that form the foundation every other google3 component is built on: collections, strings, status propagation, logging, the allocator, and the serialization stack. They are pervasive — Abseil alone contributes more named symbols than any non-LLVM library.

### Abseil

Abseil is the substrate. Its `absl::` namespace appears across 109,189 symbol-table names (`nm -C libtpu.so | grep -c 'absl::'`), the largest non-LLVM/MLIR family in the binary, spanning `absl::Status`/`StatusOr`, `absl::Cord`, `absl::flat_hash_map`, the flags library, and the structured-logging stack.

Version is **floored but not ceilinged**. The presence of `absl::log_internal::SetTimeZone` at offset `0x8714E5B` (141,643,355) and `absl::AnyInvocable` at `0x188C785` (25,741,189) places the build at **LTS 20240722 or later** — both are features that landed in or after that cut. No `ABSL_LTS_RELEASE_VERSION` or `ABSL_OPTION_*` literal survives in `.rodata`, so the upper bound cannot be pinned from strings alone. Closing it would require a per-symbol diff against tagged Abseil trees (for example, checking whether the SwissTable container internals match the LTS 20250127 reorganization).

```text
absl::log_internal::SetTimeZone          @ 0x8714E5B   →  ≥ LTS 20240722
absl::AnyInvocable                       @ 0x188C785   →  ≥ LTS 20240722
absl:: (symbol family)                   109,189 names →  pervasive
```

### protobuf

Protobuf is present in its modern **editions** era. The enum literals `EDITION_2024` (offset `0x5EE8C1E`, 99,519,518) and `EDITION_2026` (`0x5EE8C31`, 99,519,537) sit adjacent in the `.rodata` enum-name pool, and the generated descriptor machinery shows up as `descriptor_table_protodef_*` symbols (first at `0x23DF7736`, 601,847,606) plus `TableStruct_*` reflection metadata. The `EDITION_2024`-production / `EDITION_2026`-declared mix is consistent with protobuf **v32 or later** (editions support shipped in v32). The exact minor version is not surfaced as a banner; pinning it would require matching the serialized descriptor-pool wire format against a candidate tree.

> **NOTE —** the 760 serialized `FileDescriptorProto` records live in a dedicated `protodesc_cold` ELF section, isolated from the rest of `.rodata` by the build. That isolation is a compile-time hint that the descriptor pool is cold-path data; it is documented under [Custom Sections](custom-sections.md) rather than duplicated here.

### tcmalloc

The allocator is google3's TCMalloc, not the system `malloc`. Two pieces of evidence are decisive: the `tcmalloc::` / `tcmalloc_internal::` namespace family (107 names; 110 for the bare `tcmalloc` fragment), and a set of **custom ELF sections the build emits specifically for it** — `google_malloc` (the allocator code body), `google_malloc_bss` (its `.bss` state), `malloc_hook` (the `MallocHook` lifecycle thunks), and `__rseq_cs` (restartable-sequence critical-section metadata for the per-CPU caches). The `__rseq_cs` section is the giveaway for the rseq-accelerated per-CPU cache design of post-2024 TCMalloc. The named code footprint is small (~55 KB) because most of the allocator is a compact hot path; the section-level isolation, not the byte count, is what marks it.

### riegeli, RE2, farmhash

Three smaller google3 staples round out the core:

- **riegeli** — the record-IO format. Path literal `third_party/riegeli/` at `0x85D7066` (140,341,350) and the `riegeli::` namespace confirm it. Riegeli is the consumer that pulls in Brotli (below) as a block codec.
- **RE2** — the regex engine. Namespace `re2::` at `0x86679B9` (140,933,561), plus the internal `re2::RegexpFlag` symbol and the `third_party/re2` path. About 18,722 mangled names carry the `re2` fragment (`nm libtpu.so | grep -c re2`), though that count includes generated automaton tables, not just engine code; the demangled `re2::` namespace itself accounts for only 605 names.
- **farmhash** — the non-cryptographic hash. The full sub-variant family is present (`farmhashna`, `farmhashuo`, `farmhashte`, `farmhashxo`, `farmhashmk`, `farmhashcc`, starting at `0xFDCE579`), which is decisive: a coincidental string would not reproduce all six dispatch variants. Footprint is a handful of functions (~7 KB).

---

## Math / ML Layer

This is the numerical core that XLA and the TPU runtime lean on: dense linear algebra and tensor expression templates from Eigen, and the x86 JIT-kernel library oneDNN for the host-side fallback paths.

### Eigen

Eigen contributes 28,702 symbol-table names (`nm libtpu.so | grep -c Eigen`) — the third-largest C++ family after Abseil and the LLVM/MLIR pair — almost entirely template instantiations of the unsupported `Tensor` module (`Eigen::Tensor`, `ThreadPoolDevice`, the async executor). The presence of `Eigen::bfloat16` as a first-class scalar (rather than only the legacy `Eigen::half`) places the build on the **3.4.x branch or later**. No `EIGEN_WORLD_VERSION` / `EIGEN_MAJOR_VERSION` macro literal survives in `.rodata`, so 3.4.0 cannot be distinguished from a 3.4-series snapshot by string evidence; the pin is to the branch, not the tag.

> **GOTCHA —** Eigen's footprint is almost all template bloat. The 29 K names do not mean 29 K distinct functions of engine logic — they are per-type, per-device instantiations of the same handful of expression templates. A reimplementer sizing the "real" Eigen surface should count the distinct algorithm bodies, not the symbol names.

### Intel oneDNN (DNNL)

oneDNN is the one math library this binary pins **exactly**. The source-path literal `third_party/intel_dnnl/v3_3/src/common` at offset `0x85BEAE8` (140,241,640) fixes the version at **v3.3** with no inference required — the version is encoded directly in the vendored path. oneDNN brings its own JIT assembler, **Xbyak** (namespace at `0xB38846D`), which it uses to emit CPU kernels at runtime; Xbyak's presence is therefore a consequence of oneDNN, not an independent dependency. The combined footprint is large (~25 MB of named symbols), reflecting the breadth of primitive implementations (convolution, matmul, normalization) oneDNN ships.

---

## Compression / Crypto Layer

Four compression codecs and one TLS/crypto stack are embedded. The codec choices map to distinct consumers: zstd for the wheel-internal payload and runtime block compression, Brotli as riegeli's record codec, snappy and zlib for legacy compatibility paths, and BoringSSL for transport security.

### zstd

zstd is present and used in two roles. The runtime-codec role shows up as the full `ZSTD_*` C API — `ZSTD_compressBound` at offset `0x270D623C` (655,188,540), `ZSTD_createDCtx` at `0x8716BDF`, plus the `ZSTD_CCtx`/`ZSTD_c*` context machinery — bundled through google3's `third_party/zippy/` wrapper. The second role, decompressing the wheel-internal trailing blob, is the subject of its own page and is not duplicated here.

> **NOTE —** no `ZSTD_versionNumber` banner or `1.5.x` literal survives in `.rodata`, so the exact zstd release is not pinned from strings — only the API surface is confirmed. The two zstd usages (runtime codec vs. trailing payload) are documented separately; for the trailing-blob story see [Trailing zstd Blob](trailing-zstd-blob.md).

### Brotli and snappy

- **Brotli** is reached through riegeli as a record-block codec. `BrotliDecoderDecompress` at `0xA2869A1` (170,420,641) and the embedded static-dictionary tables (the large `.rodata` arrays Brotli ships) confirm it; the static-dictionary form points at Brotli ≥ 1.1. There are 16 `BrotliDecoder*` names in the symbol table — a thin, decode-leaning surface consistent with read-path use.
- **snappy** is present as the `snappy::` namespace at `0xA1251E5` (168,972,773) — a trace footprint (~18 KB), a legacy fast-compression path.

### zlib

zlib is present in an unusual form: the primary implementation is the **Rust port** `libz-rs`, whose mangled symbols carry the `zlib_rs` fragment (first at `0x852F25E`, 139,653,726), alongside the classic C `deflate`/`inflate`/`crc32` entry points and the `crc_braid_table` lookup. This is the only place a Rust crate provides a C-ABI compression primitive in the binary; it coexists with (rather than replaces) the C zlib symbols.

### BoringSSL

The crypto and TLS stack is **BoringSSL**, not system OpenSSL. The literal `BoringSSL` banner appears at offset `0x857D84C` (139,974,732), and the C symbol families `EVP_*`, `SSL_*`, `X509_*`, `BN_*`, `RSA_*`, `AES_*`, `SHA256_*`, `ED25519_*`, `X25519_*` are all present (9 names carry the `BoringSSL` fragment directly; the algorithm families are far larger). It is vendored under `third_party/openssl/`, the conventional google3 location for the BoringSSL fork. BoringSSL is the transport-security provider behind the embedded gRPC stack.

---

## Miscellany

### ICU

International Components for Unicode is present at a pinned version: the data-file marker `icudt78` at offset `0x83FE04C` (138,403,916) and the versioned C++ namespace `icu_78` together fix it at **ICU 78** (`icudt78` is the ICU 78 data-bundle naming convention, and `icu_78::` is the ICU 78 namespace-versioning macro expansion). ICU pulls in its own trie-based property tables (`propsTrie`, `ucase_props`, `ubidi_props`, `norm2_nfc_data`) as `.rodata` blobs.

### gRPC

gRPC is embedded as google3-tip, not a tagged release. The `grpc_core::` namespace (first at `0x188C4DE`, 25,740,510) and the presence of the **`chaotic_good`** transport — which exists only in gRPC trunk and no tagged release through v1.71 — place it on a development branch **≥ v1.66 dev**. The `grpc_version_string` symbol exists but its build-time `gRPC C++ x.y.z` literal is replaced by Google's internal release tagging, so no upstream semver can be read off. gRPC's TLS is provided by the BoringSSL stack above; its serialization by the protobuf stack.

> **CORRECTION (BOM-1) —** an earlier candidate list, drawn from the wheel's `THIRD_PARTY_NOTICES.txt`, suggested a **double-conversion** dependency (the float↔string library). A direct probe of the binary for `double_conversion` / `double-conversion` markers returned nothing in `.rodata` or the symbol table. The library may be present only as inlined `absl::str_format` internals with no surviving namespace marker, or absent from this build entirely. Listing it as a confirmed embedded library would be unsupported, so it is omitted from the bill of materials. Trust the binary: no marker, no entry.

> **NOTE —** the wheel's `THIRD_PARTY_NOTICES.txt` (731,537 bytes) names many more components than this atlas lists. The notices file is a *legal superset*: it covers everything that could be linked across the build configuration, including components compiled out of this particular artifact. Every library in the bill of materials above was re-derived from binary evidence independently of the notices file; a name appearing only in `THIRD_PARTY_NOTICES.txt`, with no namespace marker or path literal in the binary, is not counted as present.

---

## Reproduction

The inventory is reproducible without specialized tooling, precisely because the binary is unstripped. The method, end to end:

```bash
# 1. Confirm the dependency posture — six entries, no libstdc++/libgcc.
readelf -d libtpu.so | grep NEEDED

# 2. Pin the build identity (version-bearing banners, with offsets).
grep -aboE 'LLVM version g3[_]+-trunk [0-9a-f]{40}' libtpu.so
grep -aboE 'libtpu_lts_[0-9]+_b_RC[0-9]+'           libtpu.so

# 3. Per-library presence: namespace marker / path literal / banner, at an offset.
grep -aboE 'third_party/intel_dnnl/v3_3/src/common' libtpu.so   # oneDNN v3.3
grep -aboE 'icudt78'                                libtpu.so   # ICU 78
grep -aboE 'EDITION_2024|EDITION_2026'              libtpu.so   # protobuf editions
grep -aboE 'BoringSSL|ZSTD_compressBound|re2::'     libtpu.so   # crypto / codec / regex

# 4. Symbol-family counts: how many names a library contributes.
#    Bucket the demangled symbol pool by namespace prefix.
nm -SC --defined-only libtpu.so | grep -c 'absl::'
```

Each row of the bill of materials corresponds to one such probe; the file offsets cited above are the literal results, so an independent verifier lands on the same byte.

> **QUIRK —** because the binary is unstripped, the temptation is to *trust the names absolutely* and skip the path-literal corroboration. Resist it for version claims. A namespace like `re2::` proves the library is linked but says nothing about its version; only a path literal (`third_party/intel_dnnl/v3_3/...`) or a data-file marker (`icudt78`) pins a release. Presence and version are two different claims with two different evidence grades — the Confidence column tracks the weaker of the two.

## Cross-References

- [LLVM/MLIR Manifest](llvm-mlir-manifest.md) — owns the two largest occupants (LLVM trunk `8918319853fbdf…`, MLIR dialects); this atlas lists them only as line items
- [Binary Forensics Overview](overview.md) — top-level map of the forensic effort and where this atlas sits in it
- [Two-Binary Split](two-binary-split.md) — `sdk.so` *does* declare `libstdc++.so.6`; the opposite linking choice from the libc++ that `libtpu.so` embeds
- [Trailing zstd Blob](trailing-zstd-blob.md) — the wheel-internal zstd payload; this page records zstd's presence, that page tells its story
- [Custom Sections](custom-sections.md) — `google_malloc`, `protodesc_cold`, `__rseq_cs` and the other build-emitted sections that isolate tcmalloc and the protobuf descriptor pool
- [ELF Anatomy](elf-anatomy.md) — section-size accounting that frames the per-library footprint figures
