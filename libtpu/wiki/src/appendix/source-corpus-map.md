# Source-Corpus Map

> *Every figure on this page is the provenance manifest for `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl`. The analyzed payload is two ELF64 shared objects: `libtpu.so` (781,691,048 bytes, build-id `89edbbe81c5b328a958fe628a9f2207d`, wheel `0.0.40`) and `sdk.so` (22,541,240 bytes, build-id `4e9025466f71009fccb46a803806411c63744a0a`). Other wheel builds rename the package and rehash every build-id.*

## Abstract

This appendix is the corpus manifest: a byte-exact inventory of *everything* that was analyzed to build this wiki, so any claim on any other page can be traced back to the artifact it came from. It answers one question — **what exactly is "the binary"?** — at four levels: the PyPI wheel and its unpacked file tree, the two ELF objects inside it, the resources those ELF objects embed in named sections, and the IDA-Pro-derived sidecar corpus that the static analysis actually ran against. Nothing here is reverse-engineered behavior; it is the ground truth of the input set.

The distinction that governs the whole wiki is established on [Two-Binary Split](../forensics/two-binary-split.md): the wheel ships **two independent link units**, not one. `libtpu.so` is the PJRT TPU plugin (a versioned C-ABI shared object); `sdk.so` is a CPython 3.14 extension exporting `PyInit_sdk`. Neither lists the other in `DT_NEEDED`; neither imports a symbol the other defines. They share a directory and a wheel, and nothing else. Every "function count" in the wiki is therefore per-object, never summed — the combined ~979k figure that appears in early notes is an artifact of adding two unrelated IDA databases.

The page is organized as four catalogs, each with a Confidence column on every factual table: the **wheel file tree** (path → type → size), the **ELF objects** (header facts and build-ids confirmed with `readelf`), the **embedded resources** (the `filewrapper_toc` and `protodesc_cold` sections, catalogued in full on their own appendix pages), and the **IDA sidecar corpus** (the per-function decompilation/disasm/graph trees plus the database-wide JSON sidecars). All counts were confirmed directly against the filesystem; where a prior note disagreed with the bytes on disk, a `> **CORRECTION —**` records the resolution.

| | |
|---|---|
| **Wheel** | `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl` |
| **Distribution** | `libtpu` 0.0.40 (PyPI), `Requires-Python: >=3.11`, tag `cp314-cp314-manylinux_2_31_x86_64` |
| **Analyzed objects** | `libtpu.so` (745 MiB) + `sdk.so` (21.5 MiB) |
| **libtpu.so build-id** | `89edbbe81c5b328a958fe628a9f2207d` (GNU, 16 bytes) |
| **sdk.so build-id** | `4e9025466f71009fccb46a803806411c63744a0a` (GNU, 20 bytes) |
| **libtpu.so functions (IDA)** | 884,832 (records; 884,843 artifact files — see CORPUS-2) |
| **sdk.so functions (IDA)** | 94,732 |
| **Embedded schema descriptors** | 760 `FileDescriptorProto` blobs in `protodesc_cold` |
| **Embedded virtual files** | 61 entries in the `filewrapper_toc` registry |

---

## The Wheel and Its File Tree

### Purpose

The corpus root is a single binary wheel downloaded from PyPI. Unpacking it (a wheel is a ZIP) yields one Python package directory, `libtpu/`, plus the `.dist-info` metadata directory the installer reads. Everything the wiki analyzes lives under `libtpu/`; the `.dist-info/` directory is provenance, not payload.

### File Tree

The full unpacked tree, every file with its exact byte size. Sizes are from `stat`; the `.dist-info/RECORD` SHA-256 digests (below) independently pin each file's content.

```text
libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/
├── libtpu/
│   ├── libtpu.so                       781,691,048 B   ← PJRT TPU plugin (analyzed)
│   ├── sdk.so                           22,541,240 B   ← CPython 3.14 extension (analyzed)
│   ├── __init__.py                           1,131 B   ← path-config shim
│   ├── LICENSE                                  229 B
│   ├── THIRD_PARTY_NOTICES.txt              731,537 B
│   └── SDK_THIRD_PARTY_NOTICES.txt         103,306 B
└── libtpu-0.0.40.dist-info/
    ├── METADATA                              1,186 B
    ├── RECORD                                  787 B
    ├── WHEEL                                    113 B
    └── top_level.txt                             7 B
```

| Path | Type | Size (B) | Role |
|---|---|---:|---|
| `libtpu/libtpu.so` | ELF64 shared object | 781,691,048 | PJRT plugin — the primary analysis target |
| `libtpu/sdk.so` | ELF64 shared object | 22,541,240 | CPython extension `PyInit_sdk` — secondary target |
| `libtpu/__init__.py` | Python source | 1,131 | Sets `TPU_LIBRARY_PATH` to the bundled `libtpu.so` on import |
| `libtpu/LICENSE` | Text | 229 | Google Cloud Platform terms reference (`Copyright [2026] Google LLC`) |
| `libtpu/THIRD_PARTY_NOTICES.txt` | Text | 731,537 | OSS attributions vendored into `libtpu.so` |
| `libtpu/SDK_THIRD_PARTY_NOTICES.txt` | Text | 103,306 | OSS attributions vendored into `sdk.so` |
| `libtpu-0.0.40.dist-info/METADATA` | Wheel metadata | 1,186 | Name/version/summary, `Requires-Python: >=3.11` |
| `libtpu-0.0.40.dist-info/RECORD` | Wheel manifest | 787 | Per-file SHA-256 + size; the content-integrity oracle |
| `libtpu-0.0.40.dist-info/WHEEL` | Wheel header | 113 | `Generator: setuptools (82.0.1)`, `Tag: cp314-cp314-manylinux_2_31_x86_64`, `Root-Is-Purelib: false` |
| `libtpu-0.0.40.dist-info/top_level.txt` | Text | 7 | Single line: `libtpu` |

> **CORRECTION (CORPUS-1) —** earlier scratch inventories described the package as carrying only `libtpu.so`, `sdk.so`, and `__init__.py` under `libtpu/`. The unpacked tree carries **three additional payload files** — `LICENSE` (229 B), `THIRD_PARTY_NOTICES.txt` (731,537 B), and `SDK_THIRD_PARTY_NOTICES.txt` (103,306 B) — all listed in `RECORD`. They are attribution text, not code, but they are part of the shipped package and are recorded here for completeness.

### The `RECORD` integrity oracle

`RECORD` is the authoritative content manifest: one CSV line per file, `path,sha256=<urlsafe-b64-digest>,size`. The two analyzed objects pin as:

```text
libtpu/libtpu.so,sha256=RW59bm-rG1hnBr5kZ8Z6PtjVdu9bZhcR6_TMMHhK4DM,781691048
libtpu/sdk.so,sha256=aSTl9uVX4PFjRzpBtpaqLJU964lNqu2Iacx3mhIMMTI,22541240
```

These are the wheel-relative digests (base64url of the SHA-256). The IDA pipeline computed its own hex SHA-256 over the same files: `libtpu.so` → `456e7d6e6fab1b586706be6467c67a3ed8d576ef5b661711ebf4cc30784ae033`, `sdk.so` → `6924e5f6e557e0f163473a41b696aa2c953deb894daaed8869cc779a120c3132`. Either digest uniquely identifies the analyzed bytes; the build-id is the more convenient short handle.

> **NOTE —** `__init__.py` is the only executable Python in the wheel. Its sole job is `configure_library_path()`, which sets the `TPU_LIBRARY_PATH` environment variable to the absolute path of the bundled `libtpu.so` unless it is already set. JAX/PyTorch/TF then `dlopen` that path. The Python layer carries no TPU logic — it is a one-file locator shim.

---

## ELF Objects

### Purpose

The two `.so` files are the actual reverse-engineering subjects. Their ELF headers, build-ids, and section/segment shapes are the hard anchors the rest of the wiki cites. All facts below come from `readelf -h` / `readelf -n` run directly on the extracted files.

### Header Facts

| Fact | `libtpu.so` | `sdk.so` |
|---|---|---|
| File size | 781,691,048 B (745 MiB) | 22,541,240 B (21.5 MiB) |
| Class / endianness | ELF64 / little-endian | ELF64 / little-endian |
| OS/ABI | UNIX – System V | UNIX – GNU |
| Type | `DYN` (shared object) | `DYN` (shared object) |
| Machine | x86-64 | x86-64 |
| Entry point | `0x0` (library, no `_start`) | `0x0` |
| Program headers | 11 | 9 |
| Section headers | 52 | 38 |
| Build-id (GNU note) | `89edbbe81c5b328a958fe628a9f2207d` | `4e9025466f71009fccb46a803806411c63744a0a` |

> **GOTCHA —** the two objects differ in `OS/ABI` (`SYSV` vs `GNU`) and in build-id **length** — `libtpu.so` carries a 16-byte (128-bit) build-id, `sdk.so` a 20-byte (160-bit) one. This is independent corroboration of the two-binary-split thesis: they were produced by different link configurations, not a single linker invocation. Pin to the full build-id, never to a truncated prefix that could collide.

> **GOTCHA —** the wheel is colloquially called a "stripped 745 MB plugin," but neither object is stripped. Both retain a full `.symtab` — `1,233,710` symbol-table entries in `libtpu.so` (1,232,970 local + 740 global) with a ~172 MiB `.strtab` — which is exactly why IDA recovers ~884k *named* functions instead of `sub_` blanks. The `.symtab` is non-`SHF_ALLOC` (it never loads at runtime; the runtime sees only the 741-entry `.dynsym`), but it is present on disk and is what makes deep static analysis possible. Analysis depth here is governed by the surviving `.symtab`, not by the small `.dynsym`. See [ELF Anatomy](../forensics/elf-anatomy.md) for the full section/segment tables.

### Roles

The two objects play categorically different roles, summarized here and detailed on [Two-Binary Split](../forensics/two-binary-split.md):

| Object | ABI surface | Linkage | What it is |
|---|---|---|---|
| `libtpu.so` | 226-entry C-ABI (218 `@@VERS_1.0` versioned + 8 linker-set bounds; `GetPjrtApi` family) | No `DT_NEEDED` on `sdk.so` | The TPU compiler + runtime: XLA/HLO, ICI collectives, the deepsea ISA backends |
| `sdk.so` | `PyInit_sdk` (one CPython init export) | No `DT_NEEDED` on `libtpu.so` | A CPython 3.14 extension module for direct TPU/SDK interaction |

---

## Embedded Resources

### Purpose

`libtpu.so` is not just code: it embeds two distinct in-binary data registries that other appendix pages catalog in full. They are recorded here so the corpus manifest names every resource pool the wiki draws on. Both live in named ELF sections and are reachable only through the symbol table and `.init_array` constructors — neither section is self-describing.

### `protodesc_cold` — the protobuf schema pool

A read-only `PROGBITS` section (`0xbe8af30`–`0xc1bf0b0`, ~3.2 MiB) holding the serialized `google.protobuf.FileDescriptorProto` for **every** `.proto` schema statically linked into the plugin — one blob per compiled `.proto`.

| Property | Value |
|---|---|
| Section | `protodesc_cold`, header `[12]`, flags `A` (alloc, read-only) |
| Descriptor count | **760** (`760` `descriptor_table_protodef_*` blobs ↔ `760` `descriptor_table_*` registrars) |
| Distinct `.proto` path strings | 769 (the 9-string excess are import-only dependencies) |
| First blob | `descriptor_table_protodef_zzRDQFgX_23` @ `0xbe8af80` (`pjrt_tpu_topo_desc_name_mapping.proto`) |
| Registration | `descriptor_table_*` structs walked by a `_GLOBAL__sub_I_` ctor in `.init_array` at static-init |

The 760 schemas span the XLA/HLO compiler, the deepsea TPU ISA for five chip families, the runtime topology and program format, the XPlane/xprof profiler, Megascale collectives, and PJRT distributed coordination. The full per-root, per-domain taxonomy is in the [protodesc_cold Catalog](protodesc-cold-catalog.md).

### `filewrapper_toc` — the embedded-file registry

A writable (`WA`) section, ELF section index 38, holding a pointer table to an embedded virtual filesystem — the runtime's bundled data files (precompiled assets, configuration blobs) materialized in memory rather than on disk.

| Property | Value |
|---|---|
| Section | `filewrapper_toc`, ELF section index 38, flags `WA` |
| Layout | `entry_count` × 8-byte pointers, each an `R_X86_64_RELATIVE` reloc into a 40-byte descriptor in `.data.rel.ro` |
| Entry count | **61** entries (~5.5 MiB of indexed payload) |
| Table anchor | `filewrapper_toc` @ `0x224bf798` (488 B array) |
| Registration anchor | `_ZL7toc_ptr` @ `0x224bf918`, set by `*_memfile_embed_internal_create()` |

> **NOTE —** do not confuse the 61-entry `filewrapper_toc` *registry* with the much larger pool of `(anonymous namespace)::filewrapper_*` symbols elsewhere in the binary. The registry is the indexed embedded filesystem; the larger symbol pool is unrelated wrapper machinery. The full catalog of the 61 entries is in the [filewrapper_toc Catalog](filewrapper-toc-catalog.md).

The existence of both sections in the ELF section table — and the static-init registration mechanism that populates them — is owned by [Custom Sections](../forensics/custom-sections.md). A third embedded resource, a trailing zstd-compressed blob carved past the last ELF section, is documented on [Trailing zstd Blob](../forensics/trailing-zstd-blob.md); the binwalk pass over `libtpu.so` carved exactly that one file.

---

## IDA Sidecar Corpus

### Purpose

The static analysis did not run on the raw `.so` bytes alone. An IDA Pro batch pass produced, for each object, an `.i64` database plus a large fan-out of per-function and per-database sidecar files (decompiled C, disassembly, control-flow graphs, and database-wide JSON exports of names/xrefs/strings/structures). The wiki's function-level claims are anchored against these sidecars; this section inventories them so any address citation can be traced to the file that backs it.

### Per-object coverage

Both objects were processed to full per-function coverage. The IDA run manifest records the function count and processing mode per target:

| Object | Functions | Mode | Per-function trees | `.i64` database | binwalk |
|---|---:|---|---|---|---|
| `libtpu.so` | 884,832 | fast | context + decompiled + disasm + graphs | yes | 1 file carved (trailing blob) |
| `sdk.so` | 94,732 | full | context + decompiled + disasm + graphs | yes | pending |

> **CORRECTION (CORPUS-2) —** two distinct counts are in play for `libtpu.so` and must not be conflated. The **function-record count** — the `length` of the `functions` sidecar, and the figure every other page cites as a "function count" — is **884,832**. The **per-function artifact-file count** in the `context/`, `decompiled/`, and `disasm/` trees is **884,843**, exactly 11 higher: a handful of thunk/alias/data-stub entries receive an artifact file without being booked as a full function record. Cite **884,832** for any function *count* (matching [Binary Layout](binary-layout.md), [Evidence-Anchor Index](evidence-anchor-index.md), and [Methodology (Deep)](methodology-deep.md)); cite **884,843** only for artifact-file *coverage*. `sdk.so` is **94,732**.

> **GOTCHA —** the IDA *mode* labels are counter-intuitive. `libtpu.so` — the 745 MiB primary target — ran in `fast` mode; the small `sdk.so` ran in `full` mode. "Full" vs "fast" governs decompiler thoroughness per function, not coverage breadth: both objects reached 100% function coverage (zero `canonical_deficits`). A reimplementer reading a decompiled `libtpu.so` body should treat marginal decompiler artifacts as expected for the fast pass, and cross-check against the disasm tree.

### Per-function trees (`libtpu.so`)

For each function, four artifact trees are emitted; the `context/`, `decompiled/`, and `disasm/` trees hold **884,843** files each — 11 more than the 884,832 function records (see CORPUS-2). The trees are enormous and exist only as analysis scaffolding; they are never read whole.

| Tree | Files | Total bytes | Contents |
|---|---:|---:|---|
| `context/` | 884,843 | ~10.06 GB | Per-function context bundle (signature, callers/callees, locals) |
| `decompiled/` | 884,843 | ~2.62 GB | Hex-Rays pseudo-C per function |
| `disasm/` | 884,843 | ~6.66 GB | x86-64 disassembly per function |
| `graphs/` | 1,769,686 | ~11.94 GB | Per-function CFG — two files each (`.dot` + `.json`) |

> **NOTE —** `graphs/` holds exactly two files per function (a `.dot` and a `.json` rendering of the same CFG), which is why its file count is `2 × 884,843 = 1,769,686`. The other three trees are one file per function.

### Database-wide JSON sidecars (`libtpu.so`)

Alongside the per-function trees, IDA emits a fixed set of whole-database exports — one file per category — plus sharded `ctree`/`split` exports cut by address window (the `off<N>_lim<M>` naming). The single-file sidecars are the practical entry points for cross-database queries.

| Sidecar | Role |
|---|---|
| `names.json` / `functions.json` / `function_addresses.json` | Symbol → address maps (the naming spine the wiki cites) |
| `callgraph.json` (~1.8 GB) / `callgraph.dot` | Function-level call graph — the "who calls X" oracle |
| `xrefs.json` (~39 GB) | Global code+data cross-reference graph — streamed, never loaded whole |
| `strings.json` | Recovered string literals |
| `rtti.json` | RTTI / typeinfo records |
| `structures.json` / `enums.json` / `prototypes.json` | Recovered type information |
| `imports.json` / `native_imports.json` / `native_exports.json` | Dynamic import/export surface |
| `segments.json` / `entries.json` / `fixups.json` / `frames.json` | Segment map, entry points, relocations, stack frames |
| `data_tables.json` / `switches.json` / `tryblks.json` | Jump tables, switch dispatch, exception try-blocks |
| `comments.json` (~496 MB) | Per-address auto-comments |
| `metadata.json` / `problems.json` | Run metadata, decompiler problem log |
| `ctree_*` (97 shards, ~2.87 GB) | AST (ctree) exports cut by address window |
| `split_*` (~192 files) | Per-window completion/metadata pairs |
| `decompilation_failures_*` (19 files) | Per-window lists of functions Hex-Rays could not decompile |

> **GOTCHA —** `xrefs.json` is the single largest artifact in the family — at ~39 GB it is roughly 50× the size of `libtpu.so` itself. It is the global cross-reference graph and is sliced by address, never loaded whole; the `callgraph.json` (~1.8 GB) is its function-level projection and is the artifact actually used for call-relationship queries. Treat any "X is referenced from Y" claim as a slice of `xrefs`, not a whole-file scan.

The `sdk.so` sidecar set mirrors this layout at 1/9th the scale (94,732 functions), with one consolidated `ctree` sidecar rather than 97 shards. The methodology — how these sidecars were generated and consumed — is documented on [Methodology (Deep)](methodology-deep.md).

---

## Cross-References

- [Two-Binary Split](../forensics/two-binary-split.md) — establishes that the wheel ships two independent link units; owns the per-object ABI/linkage evidence summarized here.
- [Forensics Overview](../forensics/overview.md) — the top-level orientation to `libtpu.so`'s size, section model, and version provenance.
- [ELF Anatomy](../forensics/elf-anatomy.md) — full section/segment tables, the `.symtab`/`.dynsym` split, and the LOAD-segment vaddr/offset translation.
- [Custom Sections](../forensics/custom-sections.md) — owns the `filewrapper_toc` and `protodesc_cold` section headers and their static-init registration.
- [Trailing zstd Blob](../forensics/trailing-zstd-blob.md) — the one file binwalk carves past the last ELF section.
- [filewrapper_toc Catalog](filewrapper-toc-catalog.md) — the 61 embedded virtual files indexed by `filewrapper_toc`.
- [protodesc_cold Catalog](protodesc-cold-catalog.md) — the 760 `FileDescriptorProto` schemas in `protodesc_cold`.
- [Methodology (Deep)](methodology-deep.md) — how the IDA sidecar corpus was produced and consumed.
- [Binary Layout](binary-layout.md) — the address-band map of `libtpu.so` that the per-function trees index.
