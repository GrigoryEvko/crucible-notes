# Binary Build-ID / Version Table

> *Every identifier on this page is read directly from the three `neuronx_cc 2.24.5133.0+58f8de22` wheels (`cp310`, `cp311`, `cp312`, manylinux x86_64). Build-ids are the literal `readelf -n` `NT_GNU_BUILD_ID` notes; sizes are `stat`; SHA-256 is `sha256sum`. Nothing here is inferred from a decompiler.*

## Abstract

This appendix is the identification card for the whole book. Every other page cites a `sub_` address, a vtable slot, or a struct offset read from *one* of these binaries; a reader who wants to confirm they are looking at the same artifact needs the exact build identity, not just the package version. This page gives, for every binary the wiki documents: its GNU build-id, its size, and its cross-wheel parity status (byte-identical / same-size-different-bytes / per-wheel ABI variant). Together these let a reader verify the artifact and judge how far a cp310-derived finding generalizes.

The version string `2.24.5133.0+58f8de22` lives in package *metadata*, not in any ELF. The local-version label `58f8de22` identifies the source snapshot; it is **not** a build-id and appears in no `NT_GNU_BUILD_ID`. The per-binary GNU build-id is the unambiguous handle for *that ELF*, and it is the basis for the parity argument: a binary whose build-id (and SHA-256) is constant across the three wheels is provably the same artifact; one that varies is rebuilt per wheel and must be re-checked.

This page is the consolidated table promised by [§0.5 Build & Version Provenance](../reference/versions.md) and the binary atlas in [§0.4 Binary Inventory](../reference/binary-inventory.md). Those pages give the *argument*; this one gives the *raw identifiers* for all binaries at once.

## The version string and where it lives

| | |
|---|---|
| **Version string** | `2.24.5133.0+58f8de22` |
| **Build timestamp** | `Apr 08 2026, 21:07:10 UTC` |
| **Source** | `neuronxcc/version/__init__.py` (`__version__`, `__buildtime__`); also the wheel filename and `*.dist-info/METADATA` `Version:` field |
| **Wheel tag line** | `Tag: cp310-cp310-linux_x86_64` (per-wheel), `Generator: skbuild 0.19.0`, `Root-Is-Purelib: false` |
| **Decoding** | `2.24` release line · `5133.0` build number · `+58f8de22` PEP 440 local-version label = source-revision tag |

> **NOTE —** the version string is *not* embedded in `libwalrus.so` or any tool ELF — `strings … | rg '2\.24\.5133\|58f8de22'` returns nothing on the binaries. It is carried only in Python-level metadata. So "which version" is answered by the package; "which exact binary" is answered by the GNU build-id below.

> **QUIRK —** `58f8de22` *looks* like an ELF build-id (eight hex digits) but is unrelated to one. It is the PEP 440 local label. The real ELF build-ids are 8 bytes (16 hex) for the standalone tools and 20 bytes (40 hex) for the shared libraries — see below.

## The standalone tool ELFs (`starfish/bin/`)

These are large statically-linked tools forked by the driver as subprocesses ([§0.4](../reference/binary-inventory.md)). Their build-ids are **8-byte** (16 hex) — the truncated form emitted by the XLA/Bazel build, distinct from the 20-byte default used by the shared libraries. The five large tools are byte-identical across the three wheels; the four small Python-linked tools are rebuilt per wheel.

| Tool | Size (bytes) | GNU build-id (cp310) | Cross-wheel parity |
|---|---|---|---|
| `xla_infergoldens` | 234,006,296 | `e37e17b930af23aa` | **identical** — same build-id + SHA-256 on cp310/311/312 |
| `hlo2penguin` | 231,644,968 | `4ea91b0c9770f006` | **identical** across all three |
| `hlo-opt` | 228,962,280 | `93dd8bd9bd4c697b` | **identical** across all three (SHA `5c633e29…` verified equal) |
| `hlo-neff-wrapper` | 226,862,912 | `946053c80bdfb44a` | **identical** across all three |
| `snapshot-unpack` | 226,217,712 | `5eb26caa7b12ffc4` | **identical** across all three |
| `walrus_bugpoint_driver` | 35,925,984 | `98e94c710c499cfb280bd236b482aba294bc6c1f` | **per-wheel** (20-byte id; differs cp311/cp312) |
| `coloring_allocator_with_loop` | 3,768,896 | `987e77f5574b033e3828a268205bbc263d43e7bc` | **per-wheel** (differs cp311/cp312) |
| `full_unroll` | 2,924,576 | `88d5ca055f94f0e348ce15f63f2f87ff1cf567f6` | **per-wheel** (differs cp311/cp312) |
| `walrus_driver` | 708,864 | `d1c87759a9de86023eb1bc35d17dc8d1150b1926` | **per-wheel** (differs cp311/cp312) |

> **NOTE —** the five large tools carry an 8-byte build-id and are byte-identical across wheels; the four small tools carry a 20-byte build-id and are rebuilt per wheel. The 8-byte/20-byte split tracks the build path: the big tools are pure XLA/MLIR/LLVM C++ with no Python linkage, so they ship as one shared inode (link-count 3) across the three wheel directories. The four small drivers do link Python and are rebuilt per ABI.

### Per-wheel build-ids for the rebuilt tools

| Tool | cp310 | cp311 | cp312 |
|---|---|---|---|
| `walrus_driver` | `d1c87759a9de8602…150b1926` | `1fe7784af2e89983…c52fbabe` | `147f02ffbe5ca820…0b28a2ee` |
| `walrus_bugpoint_driver` | `98e94c710c499cfb…94bc6c1f` | `2a6b4af5c86fda88…2007c3f1` | `ec414ef6aeb3bc42…d7db6040` |
| `coloring_allocator_with_loop` | `987e77f5574b033e…3d43e7bc` | `fe82cd8a352de21e…6d373634` | `dff2fe26f17db71d…a24d9692` |
| `full_unroll` | `88d5ca055f94f0e3…1cf567f6` | `d4bedb368ad58e60…01c48eaa` | `f3f95f43d7abfb18…26f7fb12` |

---

## The C++ shared libraries (`starfish/lib/`)

These carry the backend logic the wiki documents most heavily. All carry a **20-byte** GNU build-id. Their *sizes* are constant across cp310/cp311/cp312 (one exception: `libwalrus.so` on cp312), but their *build-ids and SHA-256 differ per wheel* — they are rebuilt per wheel-build even though the C++ logic is Python-ABI-independent. Treat addresses as cp310-derived and spot-check before carrying to cp311/cp312.

| Library | Size cp310/cp311 (bytes) | Size cp312 (bytes) | GNU build-id (cp310) | Parity |
|---|---|---|---|---|
| `libwalrus.so` | 64,973,024 | **64,968,928** | `92b4d331a42d7e80bb839e03218d2b9b0c23c346` | per-wheel; cp312 also 4,096 B smaller |
| `libBIRSimulator.so` | 36,264,288 | 36,264,288 | `f1c6885f176c0d3c5d3a804b0f0b714ae7814ea6` | same size, per-wheel id |
| `liblogging.so` | 35,270,656 | 35,270,656 | `bb154e25bd9059c5f4c85cc0bb34bff383295054` | **byte-identical** (SHA `80f44b5f…` equal on all three) |
| `libLncBarriercheck.so` | 33,384,000 | 33,384,000 | `d0c41f5a1e2a1950fe69c49a91477b34f640eb2b` | same size, per-wheel id |
| `libBIR.so` | 9,549,368 | 9,549,368 | `a9b1ea38c47e579178b179fd445aa8edd593f206` | same size, per-wheel id (SHA differs) |
| `libBIRParserDumper.so` | 1,981,344 | 1,981,344 | `0783d8ed07f5a7223e176913883d16dfdcb0785c` | same size, per-wheel id |
| `libBIRRacecheck.so` | 919,328 | 919,328 | `4b6176662ca41ef130314409aa58846a881dbfa4` | same size, per-wheel id |

> **QUIRK —** `liblogging.so` is the one shared library that is **byte-identical** across all three wheels (verified by SHA-256), so its build-id is constant. Every other `lib/*.so` has a constant *size* on cp310/cp311 but a *different SHA-256 and build-id* — same source, separately-stamped builds. Do not read "same size" as "same bytes": e.g. `libBIR.so` is 9,549,368 bytes on every wheel yet has three distinct SHA-256 values (`2387d0d4…`, `51afccd0…`, `ae646014…`).

### Per-wheel build-ids for the shared libraries

| Library | cp310 | cp311 | cp312 |
|---|---|---|---|
| `libwalrus.so` | `92b4d331…0c23c346` | `70db95ec…85037138` | `aaac42d4…b972df21` |
| `libBIR.so` | `a9b1ea38…d593f206` | `9c8b08de…2bbff512` | `7fd92527…3974aad6` |
| `libBIRSimulator.so` | `f1c6885f…e7814ea6` | `eb1ccd96…24e70ea2` | `7f52d8a9…688d62f7` |
| `libBIRParserDumper.so` | `0783d8ed…dcb0785c` | `d8a77251…d15f48c0` | `d5fa70cf…06b9f36d` |
| `libBIRRacecheck.so` | `4b617666…881dbfa4` | `a493fb45…22ba9924` | `75796334…5c496436` |
| `liblogging.so` | `bb154e25…83295054` | `bb154e25…83295054` | `bb154e25…83295054` |
| `libLncBarriercheck.so` | `d0c41f5a…f640eb2b` | `8d6ad208…7911b003` | `1423437a…7423d7cd` |

---

## The Cython extension modules (`*.cpython-31x-*.so`)

These hold the Penguin middle-end and the NKI frontend. Unlike the C++ libraries, they link against the CPython ABI, so each ships as a genuinely **distinct per-ABI artifact**: build-id *and* size differ across the three wheels. There is no cross-version generality to claim for these — a cp310 finding must be re-confirmed on cp311/cp312. Sizes are listed because they are part of the per-wheel identity.

| Module (path under `neuronxcc/`) | Size cp310 | Size cp311 | Size cp312 |
|---|---|---|---|
| `nki/compiler/backends/neuron/KernelBuilder` | 14,588,400 | 17,327,800 | 16,823,688 |
| `starfish/penguin/targets/codegen/BirCodeGenLoop` | 11,139,256 | 13,761,672 | 13,916,872 |
| `starfish/penguin/targets/codegen/NkiCodegen` | 4,891,928 | 5,818,104 | 5,885,376 |
| `nki/isa/neuron_isa` | 3,303,784 | 3,950,632 | 3,795,816 |
| `logging/ErrorMessages` | 912,784 | 912,752 | 912,784 |
| `isa_tpb/sunda/neuron_isa` | 532,808 | 532,776 | 532,776 |

GNU build-ids (cp310, 20-byte):

| Module | GNU build-id (cp310) |
|---|---|
| `KernelBuilder` | `9eb1020ebbb2a46b230a16ada272daa71f3001bf` |
| `BirCodeGenLoop` | `cb19fae04f37f9a7ef7acc610b6d32d36ac816f1` |
| `NkiCodegen` | `4838bf1664497e3c833e32d01f742faca1104c27` |
| `nki/isa/neuron_isa` | `c5f6505d42902c94234c3cfba3e9004c1e14935f` |
| `isa_tpb/sunda/neuron_isa` | `95b91c3fd8cb6a6361f1aeef7b182491ceebd4f3` |
| `ErrorMessages` | `e3603eb1bd8b2d7e7372e62b8a79919be1aa1186` |

> **NOTE —** `ErrorMessages` and `isa_tpb/sunda/neuron_isa` differ by only 32 bytes between wheels (the build-stamp), but their build-ids are still distinct per ABI; they are not byte-identical. `KernelBuilder` and `BirCodeGenLoop` differ by megabytes — the cp310 build is materially smaller, so cp310 addresses do **not** transfer.

## The parity ladder (how far a finding generalizes)

Three tiers, strongest to weakest. This is the precise basis for the book's "cross-version general" claim:

1. **Byte-identical across all three wheels** — `hlo-opt`, `hlo2penguin`, `hlo-neff-wrapper`, `snapshot-unpack`, `xla_infergoldens` (the five large tool ELFs) and `liblogging.so`. A finding read here is exact for cp310/cp311/cp312 — the three SHA-256 digests are equal.
2. **Same source, per-wheel build stamp** — the rest of `lib/*.so` and the four small `bin/` drivers. Sizes mostly match but SHA-256 / build-id differ, so the logic carries while exact addresses stay **[INFERRED]** until a per-symbol spot-check on cp311/cp312.
3. **Distinct per-ABI artifact** — the Cython `*.cpython-31x-*.so`. Build-id and size differ; no address generality. A cp310 address is cp310-only unless re-extracted. **No cross-version claim.**

> **QUIRK —** the cp312 `libwalrus.so` is the sharpest reminder that tier 2 is not tier 1: it is 4,096 bytes (one page) *smaller* than cp310/cp311, so even backend addresses past that point can shift. Always confirm a `libwalrus.so` address against the cp312 build before quoting it as version-general.

## Verification provenance

- Build-ids: `readelf -n <file>` → `NT_GNU_BUILD_ID` note (8-byte for the five large tools, 20-byte elsewhere). Re-confirmed for `libwalrus.so` (`92b4d331…`), `hlo-opt` (`93dd8bd9bd4c697b`), and `libBIR.so` (`a9b1ea38…`).
- Sizes: `stat -c%s`. SHA-256: `sha256sum`. Byte-identity claims (tier 1) are SHA-256-verified equal across the three wheel copies; e.g. `hlo-opt` = `5c633e29…` and `liblogging.so` = `80f44b5f…` on all three.
- Version string: `neuronxcc/version/__init__.py` and `*.dist-info/METADATA`; confirmed *absent* from the binaries via `strings`.

See also: [§0.5 Build & Version Provenance](../reference/versions.md) (the parity argument and citation conventions) and [§0.4 Binary Inventory & the .so Map](../reference/binary-inventory.md) (what each binary contains).
