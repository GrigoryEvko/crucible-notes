# filewrapper_toc Catalog

> *All addresses on this page apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

libtpu.so embeds a self-contained set of runtime resource files — topology
tables, per-silicon firmware bootloaders, accuracy LUTs, CSR/error catalogs,
and precomputed ICI route caches — and reaches them through an in-process
`embed://` virtual filesystem rather than the host filesystem. The directory
of that filesystem is a single named ELF section, **`filewrapper_toc`**, which
holds a flat array of pointers to fixed-size file descriptors. This appendix
catalogs that section exhaustively: its location, the descriptor record format,
and the full enumerated table of all 61 embedded resources with name, content
type, byte size, and data address.

The mechanism resembles a minimal in-memory `tar`/`cpio` index with
content-addressed dedup. Each descriptor is a 40-byte record holding three
relocated/scalar fields — `name`, `data`, `size` — plus a 16-byte content
fingerprint used for cache keying. There is no per-entry compression flag: the
codec is implied by the filename suffix (`.binarypb` = raw serialized protobuf,
`.br` = Brotli, `.compressed` = a `CompressedToroidalRouteCache` wrapper,
`.txtpb`/`.txt`/`.tmpl` = text, and one TZif zoneinfo blob). The section itself
is `WA` (writable, allocated) and reads as all-zero on disk — its 61 pointers
are materialized at load time by `R_X86_64_RELATIVE` relocations, as is each
descriptor's `name` and `data` field.

> **NOTE —** libtpu carries **two** independent embedded-binary systems that
> are easy to conflate. This page documents the **MemFile / `embed://`
> registry** indexed by `filewrapper_toc` (61 entries, ~5.5 MiB). It is *not*
> the much larger pool of `(anonymous namespace)::filewrapper_*`
> `ToroidalRouteCache` symbols in `.lrodata` (~60 MB) that the slice-builder
> references directly by symbol. Ten of those route caches are *also*
> re-registered through this TOC under short basenames (entries 27–29, 53–59);
> the rest never appear here.

For reimplementation, the contract is:

- **The section layout:** `filewrapper_toc` is `entry_count` × 8-byte pointers, each populated by an `R_X86_64_RELATIVE` reloc, each pointing at a 40-byte descriptor in `.data.rel.ro`.
- **The descriptor record:** `{ const char* name; const char* data; uint64_t size; uint8_t fingerprint[16]; }`, 40 bytes, with `name`/`data` themselves relocated.
- **The codec-by-suffix rule:** the struct has no codec field; the loader picks the decompressor from the filename extension.
- **The `embed://` URI binding:** each resource is reachable as `embed://<memfile_name>/<filename>`, registered by a `<name>_memfile_embed_internal_create()` initializer.

| | |
|---|---|
| **Section** | `filewrapper_toc` — ELF section index 38, flags `WA` |
| **Section VA / file offset** | `0x224bf798` / `0x220bf798` |
| **Section size** | `0x1e8` = 488 bytes |
| **Entry count** | 61 (`488 / 8`), confirmed by 61 `R_X86_64_RELATIVE` relocs |
| **Pointer array stride** | 8 bytes (one pointer per entry) |
| **Descriptor record size** | 40 bytes (`0x28`) |
| **Descriptor location** | `.data.rel.ro` (VA `0x215f81a0`, file `0x213f81a0`, Δ `0x200000`) |
| **Name strings** | `.rodata` (VA `0x84a0000`), merged NUL-terminated pool |
| **Data blobs** | `.lrodata` (VA `0x1884a00`) and `.rodata` |
| **Total embedded payload** | 5,767,739 bytes (~5.50 MiB), 51 distinct filenames |
| **Registration anchor** | `_ZL7toc_ptr` @ `0x224bf918`; `*_memfile_embed_internal_create()` set |

---

## Section Layout and Record Format

### The pointer array

`filewrapper_toc` is a writable section holding nothing but a 61-element array
of 64-bit pointers. On disk every byte is zero; the pointers are filled by the
dynamic loader from `R_X86_64_RELATIVE` relocations, one per slot:

```text
filewrapper_toc @ 0x224bf798 (488 B)
  slot 0  @ 0x224bf798  --reloc-->  desc 0x215fb0b0
  slot 1  @ 0x224bf7a0  --reloc-->  desc 0x215fc950
  slot 2  @ 0x224bf7a8  --reloc-->  desc 0x217935a0
  ...
  slot 60 @ 0x224bf978  --reloc-->  desc 0x2203ff90
```

The count is doubly confirmed: `0x1e8 / 8 = 61`, and exactly 61 relocations
target the range `[0x224bf798, 0x224bf798+0x1e8)`. The `_ZL7toc_ptr` symbol at
`0x224bf918` sits inside this section (slot ~48), and is the symbol the
registration functions reference when walking the TOC.

> **QUIRK —** the section being `WA` and zero-on-disk means a static hex dump
> of `filewrapper_toc` shows nothing useful. The directory does not exist until
> the loader applies relocations. Any reimplementation that mmaps the section
> and reads pointers without first resolving `R_X86_64_RELATIVE` will see 61
> null pointers.

### The descriptor record

Every TOC pointer lands on a 40-byte descriptor in `.data.rel.ro`. The layout
is proven by relocation analysis: `+0x00` and `+0x08` are themselves
`R_X86_64_RELATIVE` relocs (so they are *pointers*, not inline data), `+0x10`
is a raw little-endian `uint64`, and `+0x18` is 16 bytes of inline hash.

```c
struct FileWrapperEntry {            // .data.rel.ro, 32-byte aligned
    const char* name;                // +0x00  -> .rodata, NUL-terminated (merged pool)
    const char* data;                // +0x08  -> .lrodata / .rodata blob
    uint64_t    size;                // +0x10  byte length of data
    uint8_t     fingerprint[16];     // +0x18  128-bit content hash
};                                   // sizeof == 0x28 (40 bytes)
```

For entry 0, the descriptor at `0x215fb0b0` resolves to: `name -> 0x8798cc4`
(`"pjrt_tpu_topo_desc_name_map.txtpb"`), `data -> 0x189a3f0`, `size = 403`,
`fingerprint = 9454b768359f7edd1f156f3c40827bc8`. Reading `name` and `size`
back from the binary reproduces the cataloged values exactly.

> **GOTCHA —** the **descriptors are not packed at 40-byte stride in
> `.data.rel.ro`**. Consecutive descriptors are spaced by whatever the linker
> chose (e.g. the bootloader run at entries 5–16 is 0x50 apart, not 0x28). The
> 40-byte figure is the *record size* — the span the loader reads per
> descriptor — not the array pitch. There is no descriptor array to stride
> over; the only array is the pointer table in `filewrapper_toc`. Reach each
> descriptor through its TOC pointer, never by adding 40 to the previous one.

### Codec selection by suffix

The struct carries no compression-type field, so the loader infers the codec
from the filename extension. The observed suffix→codec map:

| Suffix | Codec | Decoder |
|---|---|---|
| `.binarypb` | raw serialized protobuf, stored verbatim | direct `ParseFromArray` |
| `.br` | raw Brotli stream (no container magic) | bundled Brotli runtime |
| `.compressed` | `CompressedToroidalRouteCache` wrapper proto (`format` + `data`) | `Decompress(CompressedToroidalRouteCache)` @ `sub_20B63320` |
| `.txtpb` / `.txt` / `.tmpl` | ASCII / text-proto / HTML-JS template | text, no decode |
| `America/Los_Angeles` (no suffix) | IANA TZif zoneinfo | tz parser |

The `.br` blobs begin with no recognizable magic (entry 25 starts `55 8b 5b…`)
because they are bare Brotli, not a framed container. The `.compressed` blobs
begin with a protobuf tag for `format` (`08 02` ⇒ field 1 = 2 ⇒ Brotli payload)
or jump straight to the `data` field (`7a` ⇒ field 15) when `format` is the
default — see the route-cache section below.

### The `embed://` URI binding

Each resource is published into the in-process MemFile namespace under
`embed://<memfile_name>/<filename>`. The memfile name is the registration
group, the filename the descriptor's `name`. Verbatim URI prefixes recovered
from `.rodata` include:

```text
embed://tpu_chip_parts/                 (e.g. .../6acc60406_tensornode_chip_parts.binarypb)
embed://tpu_chip_config/                (chip_configs lookups)
embed://current_tpu_runtime_abi_memfile/current_tpu_runtime_abi.binarypb
embed://deepsea_compiler_hash_memfile/deepsea_compiler_hash.txt
embed://accuracy_table_                 (per-codename accuracy LUTs)
embed://{vfc,gfc,glc,vlc}_architectural_resources_memfile/architectural_resources.binarypb
embed://{vfc,gfc,glc,vlc}_pf_csrs_memfile/<chip>_pf_csrs.br
embed://{vfc,gfc,glc,vlc}_error_collector_error_sources_brotli_memfile/<chip>_error_collector_error_sources.br
```

Registration is performed by a family of nullary
`<name>_memfile_embed_internal_create()` functions (e.g.
`tpu_chip_parts_memfile_embed_internal_create()` @ `sub_20B37240`), driven from
`_GLOBAL__sub_I_*_memfile_embed_internal_builtin.cc` static initializers, plus
four `*_pf_register_memfile_register_translator.cc` translators for the `pf`
("platform-firmware") CSR maps. Each writes its descriptor pointer into the TOC
via `_ZL7toc_ptr`.

> **QUIRK —** the `embed://tpu_chip_parts/<codename>` paths are built at runtime
> by concatenating the prefix with a filename literal; the full path is never a
> single string in `.rodata`. All nine `<codename>_chip_parts.binarypb`
> filenames *do* exist as `.rodata` literals (jellyfish, pufferfish,
> pufferfish_lite, viperfish, viperfish_lite, ghostlite, dragonfish,
> 6acc60406, plus the 6acc60406 tensornode variant), but only the
> **6acc60406 tensornode** chip_parts is actually embedded (entry 48). The
> other eight are lookup keys with no backing blob in this build — older
> silicon is described by the chip_configs entries (39–47) instead.

---

## Full Entry Catalog

All 61 entries, recovered directly from the binary by walking the TOC
pointers, resolving each descriptor's `name`/`data` relocations, and reading
`size`. `data VA` is the address of the resource blob (in `.lrodata` or
`.rodata`). The codec column applies the suffix→codec rule above. Every row was
read back from the live binary; the catalog matches the section byte-for-byte,
so the whole table is `CERTAIN` unless a cell is an interpretation of content
(those carry their own lower confidence in the detail sections).

| #  | data VA      |      size | codec  | content (proto / type)                         | name |
|----|--------------|----------:|--------|------------------------------------------------|------|
|  0 | `0x189a3f0`  |       403 | text   | PJRT topology name-map (text-proto)            | `pjrt_tpu_topo_desc_name_map.txtpb` |
|  1 | `0x189a590`  |    94,037 | raw    | platform/topology config (9 generations)       | `tpu_platform_configs.binarypb` |
|  2 | `0x1c8df30`  |       412 | raw    | jellyfish BarnaCore constant params            | `barna_core_constant_param_jfc.binarypb` |
|  3 | `0x344d280`  |     1,124 | text   | LLO-IR HTML visualizer footer (JS template)    | `llo_ir_footer.tmpl` |
|  4 | `0x3451120`  |       171 | raw    | SparseCore embedding-loop kernel registry      | `embeddings_loop_configs_legacy.binarypb` |
|  5 | `0x3fad570`  |    12,480 | raw    | TpuCoreProgramProto (TC bootloader)            | `continuation_bootloader_program_tensorcore_dragonfish_default.binarypb` |
|  6 | `0x3fb0640`  |    12,480 | raw    | TpuCoreProgramProto (TC bootloader)            | `continuation_bootloader_program_tensorcore_jellyfish_default.binarypb` |
|  7 | `0x3fb3710`  |    14,016 | raw    | TpuCoreProgramProto (TC bootloader)            | `continuation_bootloader_program_tensorcore_pufferfish_default.binarypb` |
|  8 | `0x3fb6de0`  |    13,512 | raw    | TpuCoreProgramProto (TC bootloader)            | `continuation_bootloader_program_tensorcore_pufferfish_lite_default.binarypb` |
|  9 | `0x3fba2b0`  |    26,828 | raw    | TpuCoreProgramProto (TC bootloader)            | `continuation_bootloader_program_tensorcore_viperfish_lite_default.binarypb` |
| 10 | `0x3fc0b80`  |    40,648 | raw    | TpuCoreProgramProto (TC bootloader)            | `continuation_bootloader_program_tensorcore_viperfish_inference.binarypb` |
| 11 | `0x3ff21a0`  |    64,730 | raw    | TpuCoreProgramProto (TC, megachip tccontrol)   | `continuation_bootloader_program_tensorcore_smoothie_viperfish_megachip_tccontrol.binarypb` |
| 12 | `0x4021420`  |     1,718 | raw    | TpuCoreProgramProto (SC bootloader)            | `continuation_bootloader_program_sparsecore_viperfish_inference.binarypb` |
| 13 | `0x4024400`  |    34,504 | raw    | TpuCoreProgramProto (TC bootloader)            | `continuation_bootloader_program_tensorcore_ghostlite_inference.binarypb` |
| 14 | `0x4045f50`  |     1,718 | raw    | TpuCoreProgramProto (SC bootloader)            | `continuation_bootloader_program_sparsecore_ghostlite_inference.binarypb` |
| 15 | `0x40481b0`  |    41,178 | raw    | TpuCoreProgramProto (TC, megachip tccontrol)   | `continuation_bootloader_program_tensorcore_6acc60406_megachip_tccontrol.binarypb` |
| 16 | `0x4066430`  |     1,672 | raw    | TpuCoreProgramProto (SC, megachip tccontrol)   | `continuation_bootloader_program_sparsecore_6acc60406_megachip_tccontrol.binarypb` |
| 17 | `0x4067890`  |     1,334 | raw    | runtime ABI table (abi version 158)            | `current_tpu_runtime_abi.binarypb` |
| 18 | `0xb430db0`  |        32 | text   | compiler build hash (32 hex chars)             | `deepsea_compiler_hash.txt` |
| 19 | `0x40703d0`  |   181,284 | raw    | accuracy/transcendental table                  | `accuracy_table_dragonfish.binarypb` |
| 20 | `0x409c800`  |   180,617 | raw    | accuracy/transcendental table                  | `accuracy_table_ghostlite.binarypb` |
| 21 | `0x40c8990`  |   181,284 | raw    | accuracy/transcendental table                  | `accuracy_table_jellyfish.binarypb` |
| 22 | `0x40f4dc0`  |   181,284 | raw    | accuracy/transcendental table                  | `accuracy_table_pufferfish.binarypb` |
| 23 | `0x41211f0`  |   181,139 | raw    | accuracy/transcendental table                  | `accuracy_table_viperfish.binarypb` |
| 24 | `0x44cd160`  |    13,489 | raw    | arch resource-ID table (vfc / viperfish-fc)    | `architectural_resources.binarypb` |
| 25 | `0x44d0620`  |     8,136 | brotli | per-chip CSR map (vfc)                          | `vfc_pf_csrs.br` |
| 26 | `0x44d25f0`  |   177,789 | brotli | error-source catalog (vfc)                      | `vfc_error_collector_error_sources.br` |
| 27 | `0x44ff0d0`  |   171,623 | ctrc   | CompressedToroidalRouteCache 8x8x8             | `8x8x8.binarypb.compressed` |
| 28 | `0x50386d0`  |   724,766 | ctrc   | CompressedToroidalRouteCache 8x16x16 twisted    | `8x16x16_twisted.binarypb.compressed` |
| 29 | `0x5336180`  |   339,942 | ctrc   | CompressedToroidalRouteCache 8x8x16 twisted     | `8x8x16_twisted.binarypb.compressed` |
| 30 | `0x5a21f00`  |   253,765 | brotli | error-source catalog (gfc / 6acc60406)          | `gfc_error_collector_error_sources.br` |
| 31 | `0x5a5fe50`  |     4,916 | brotli | per-chip CSR map (gfc / 6acc60406)              | `gfc_pf_csrs.br` |
| 32 | `0x5ce8d10`  |     5,936 | raw    | arch resource-ID table (gfc / 6acc60406)        | `architectural_resources.binarypb` |
| 33 | `0x5e41bf0`  |     7,317 | raw    | arch resource-ID table (glc / ghostlite)        | `architectural_resources.binarypb` |
| 34 | `0x5e43890`  |     5,485 | brotli | per-chip CSR map (glc / ghostlite)              | `glc_pf_csrs.br` |
| 35 | `0x5e44e00`  |   115,260 | brotli | error-source catalog (glc / ghostlite)          | `glc_error_collector_error_sources.br` |
| 36 | `0x5ea8ec0`  |     3,043 | raw    | arch resource-ID table (vlc / viperfish-lite)   | `architectural_resources.binarypb` |
| 37 | `0xbd7a320`  |       856 | brotli | per-chip CSR map (vlc / viperfish-lite)         | `vlc_pf_csrs.br` |
| 38 | `0x5ea9ab0`  |    28,847 | brotli | error-source catalog (vlc / viperfish-lite)     | `vlc_error_collector_error_sources.br` |
| 39 | `0x5f01460`  |     1,309 | raw    | chip_configs (default)                          | `6acc60406_tensornode_chip_configs_default.binarypb` |
| 40 | `0x5f02fa0`  |     1,481 | raw    | chip_configs (inference)                        | `6acc60406_chip_configs_inference.binarypb` |
| 41 | `0x5f03ff0`  |     1,190 | raw    | chip_configs (legacy)                           | `6acc60406_tensornode_chip_configs_legacy.binarypb` |
| 42 | `0xbdf0830`  |       870 | raw    | chip_configs (legacy_dense)                     | `pufferfish_chip_configs_legacy_dense.binarypb` |
| 43 | `0xbdf0ba0`  |       985 | raw    | chip_configs (megacore, glp emulation)          | `viperfish_glp_emulation_chip_configs_megacore.binarypb` |
| 44 | `0xbdf19a0`  |       819 | raw    | chip_configs (megacore_dense)                   | `pufferfish_chip_configs_megacore_dense.binarypb` |
| 45 | `0xbdf1ce0`  |       786 | raw    | chip_configs (megacore_inference)               | `pufferfish_chip_configs_megacore_inference.binarypb` |
| 46 | `0x5f05b70`  |     1,458 | raw    | chip_configs (megacore)                         | `viperfish_chip_configs_megacore.binarypb` |
| 47 | `0x5f07af0`  |     1,174 | raw    | chip_configs (legacy_sparse_core)               | `6acc60406_tensornode_chip_configs_legacy_sparse_core.binarypb` |
| 48 | `0xbdf29a0`  |       504 | raw    | **TpuChipPartsProto (version 6)**               | `6acc60406_tensornode_chip_parts.binarypb` |
| 49 | `0x5f08e80`  |    68,780 | raw    | ICI resiliency route table (fault dim z)        | `cache_ici_resiliency_6acc60406_fault_dim_z.binarypb` |
| 50 | `0x5f3c060`  |     4,009 | raw    | ICI resiliency route table (fault dim z)        | `cache_ici_resiliency_pufferfish_fault_dim_z.binarypb` |
| 51 | `0x5f3f640`  |    34,504 | raw    | ICI resiliency route table (fault dim z)        | `cache_ici_resiliency_viperfish_fault_dim_z.binarypb` |
| 52 | `0xbe62e50`  |       134 | raw    | twisted-torus route-cache **index**             | `cache_twisted_torus_all.binarypb` |
| 53 | `0x5f59080`  |     5,230 | ctrc   | CompressedToroidalRouteCache 8x8x16 twisted     | `8x8x16_twisted.binarypb.compressed` |
| 54 | `0x6482ae0`  |   173,801 | ctrc   | CompressedToroidalRouteCache 8x8x8             | `8x8x8.binarypb.compressed` |
| 55 | `0x67f17c0`  |   702,686 | ctrc   | CompressedToroidalRouteCache 8x16x16 twisted    | `8x16x16_twisted.binarypb.compressed` |
| 56 | `0x68c3bb0`  |   367,927 | ctrc   | CompressedToroidalRouteCache 8x8x16 twisted     | `8x8x16_twisted.binarypb.compressed` |
| 57 | `0x6a92250`  |   176,905 | ctrc   | CompressedToroidalRouteCache 8x8x8             | `8x8x8.binarypb.compressed` |
| 58 | `0x783ac00`  |   722,765 | ctrc   | CompressedToroidalRouteCache 8x16x16 twisted    | `8x16x16_twisted.binarypb.compressed` |
| 59 | `0x7bac5b0`  |   367,795 | ctrc   | CompressedToroidalRouteCache 8x8x16 twisted     | `8x8x16_twisted.binarypb.compressed` |
| 60 | `0x845b240`  |     2,852 | tzif   | IANA zoneinfo (Pacific time, TZif2)             | `America/Los_Angeles` |

> **NOTE —** the 16-byte fingerprint at descriptor `+0x18` is omitted from the
> table (it is one value per row and adds no reimplementation signal at the
> directory level). It is read back per-entry from `.data.rel.ro` and is the
> content hash the MemFile layer uses for dedup / cache-keying; entry 0's is
> `9454b768359f7edd1f156f3c40827bc8`.

---

## Resource Groups

The 61 entries fall into a small number of content families. The grouping below
is by *content* (verified from the blob's leading bytes / protobuf structure),
not just by name. Counts and sizes are `CERTAIN`; the interior semantics of
each proto are `HIGH`/`MEDIUM` as marked.

### Topology and platform metadata (entries 0–4, 17, 18, 52, 60)

The singleton metadata files. `tpu_platform_configs.binarypb` (entry 1, 94 KB)
is the master topology table; its bytes begin `0a 74 0a 05 74 70 75 76…` —
field 1 (length 0x74) wrapping field 1 (length 5) = the string `"tpuv2"`,
confirming a repeated per-generation record. Nine generations are present
(`tpuv2, tpuv3, tpuv4, tpuv4lite, tpuv5, tpuv5e, tpuv5lite, tpuv6e, tpu7x`),
each carrying named mesh shapes with packed `[x,y]` dims and a wrap/twist
descriptor (HIGH confidence on the generation list, MEDIUM on the inner shape
schema).

`current_tpu_runtime_abi.binarypb` (entry 17) leads with ABI version 158.
`deepsea_compiler_hash.txt` (entry 18) is 32 ASCII hex chars —
`4c38e01d56f528ed66ff9991fcbf2be3` — the compiler build stamp used for cache
keying (read back verbatim). `pjrt_tpu_topo_desc_name_map.txtpb` (entry 0) is a
text-proto name map. `barna_core_constant_param_jfc.binarypb` (entry 2) and
`embeddings_loop_configs_legacy.binarypb` (entry 4) are small jellyfish
BarnaCore / SparseCore kernel-registry protos. `llo_ir_footer.tmpl` (entry 3)
is the HTML/JS footer for the LLO-IR dump visualizer — text, not a proto.
`America/Los_Angeles` (entry 60) starts with the `TZif2` magic — an IANA
zoneinfo blob embedded for profiler/log timestamp formatting.

### Continuation bootloaders (entries 5–16)

Twelve `tpu.TpuCoreProgramProto` blobs — per-silicon TensorCore (TC) and
SparseCore (SC) bootloader / resume programs. Coverage: dragonfish, jellyfish,
pufferfish, pufferfish_lite, viperfish_lite, viperfish (inference +
`smoothie_viperfish_megachip_tccontrol`), ghostlite (TC + SC), 6acc60406
(TC + SC megachip). The SparseCore variants (entries 12, 14, 16) are ~1.7 KB
each; the TensorCore programs range 12 KB–65 KB. The inner ABI proto pins to
runtime ABI version 158, matching entry 17 (HIGH confidence on the proto type
and codenames, MEDIUM on the inner field tree).

### Accuracy tables (entries 19–23)

Five `accuracy_table_<codename>.binarypb` blobs (~181 KB each) for dragonfish,
ghostlite, jellyfish, pufferfish, viperfish. The near-identical sizes imply a
shared schema with per-chip coefficient values — almost certainly the
minimax/polynomial coefficient tables that drive bf16/fp8 transcendental
(exp/log/rsqrt/tanh) accuracy emulation (table identity CERTAIN, coefficient
interpretation MEDIUM). No `accuracy_table_6acc60406` is present — the newest
silicon reuses a generic path.

### Architectural resources, CSR maps, error catalogs (entries 24–26, 30–38)

Per-chip-family resource descriptors. `architectural_resources.binarypb`
appears four times (entries 24, 32, 33, 36) under distinct memfile names
(`vfc_`/`gfc_`/`glc_`/`vlc_architectural_resources_memfile`) — the resource-ID
universe for viperfish-fc, 6acc60406-gfc, ghostlite-glc, viperfish-lite-vlc.
The `*_pf_csrs.br` (entries 25, 31, 34, 37) are **Brotli-compressed** CSR maps;
`*_error_collector_error_sources.br` (entries 26, 30, 35, 38) are Brotli error
catalogs. The `.br` blobs are bare Brotli streams — entry 25 begins `55 8b 5b…`
with no container magic. `pf` = the platform-firmware register translator
(`*_pf_register_memfile_register_translator.cc`).

> **NOTE —** the four `architectural_resources.binarypb` entries share a
> filename but are *distinct blobs* at distinct data VAs and distinct sizes
> (13,489 / 5,936 / 7,317 / 3,043 B). The `embed://` namespace disambiguates
> them by memfile-name prefix, not by filename. A reimplementation keyed only
> on basename would collide four resources into one.

### chip_configs and chip_parts (entries 39–48)

Nine `*_chip_configs.binarypb` (entries 39–47) are per-codename / per-mode
core+memory layout descriptors. No static PCI vendor/device IDs appear —
consistent with the chip_parts-discovery design — instead they enumerate memory
regions (32 KiB tiles, ~2.97 GiB HBM windows). Modes seen: default, inference,
legacy, legacy_dense, megacore, megacore_dense, megacore_inference,
legacy_sparse_core, glp_emulation_megacore. Codenames: 6acc60406, pufferfish,
viperfish.

Entry 48, `6acc60406_tensornode_chip_parts.binarypb` (504 B), is the **only**
embedded `chip_parts` blob — a `TpuChipPartsProto`. Its bytes begin `08 06` =
field 1 (`version`) varint **6**, confirming the "discover via chip_parts"
design for the newest silicon. Decoded structure (HIGH confidence — read via
protobuf `--decode_raw`):

```text
version 6 ; driver_abi_version 1
cores[0] type=TENSOR_CORE(1) count=1
  reg banks id1x32, id2x64, id3x14, id4x16 ; mem 64K/256K/4K/128K ; clock 1900
cores[1] type=SPARSE_CORE(3) count=2
  mem 2K/128K/2M ; clock 1750 ; dma_misc {32,4,64,4}
shared_memories type=1 count=1
  byte_size 3,187,671,040 (~2.97 GiB HBM) ; clock 7200 ; bw_slot ~3.686e12
dma_requirements host_align=32 device_align=32 granule=32
  sync_flag_granule=32 max_single_host_dma=34,359,738,368 (32 GiB)
misc {has_extra_done_bit, host_sync_flag_async, supports_count_dones}
```

> **QUIRK —** `6acc60406` is the `tpu7x` generation. It is the *only* codename
> that ships a real chip_parts blob; the eight older codenames have only the
> filename literal as an `embed://tpu_chip_parts/` key, and resolve to
> chip_configs + static tables instead. A reimplementation must not assume
> every codename has a chip_parts resource — eight of nine lookups fall through
> in this build.

### Route caches (entries 27–29, 49–51, 52, 53–59)

The precomputed all-reduce / ICI route tables. There are three sub-kinds:

- **Twisted-torus index** — `cache_twisted_torus_all.binarypb` (entry 52, 134 B). Its bytes begin `08 01 12 0f 08 04 10 04…` — a list of `{x,y,z,fault,orientation}` tuples that map a topology shape to its route-cache file (shapes 4x4x8 … 16x16x32). This is the lookup table the loader reads first.
- **Compressed route caches** — `8x8x8.binarypb.compressed`, `8x8x16_twisted.binarypb.compressed`, `8x16x16_twisted.binarypb.compressed` (entries 27–29 and 53–59, ten blobs). Each is a `CompressedToroidalRouteCache` wrapper: `{ int32 format = 1; bytes data = 15; }`. Entry 27 begins `08 02` (`format = 2` ⇒ Brotli payload); entry 53 begins `7a` (field 15 directly) because it omits the default `format`. Decompression is `Decompress(CompressedToroidalRouteCache)` @ `sub_20B63320`, which on `format == 2` wraps the `data` Cord in a Brotli reader and parses the result as a `ToroidalRouteCache`.
- **ICI resiliency tables** — `cache_ici_resiliency_<codename>_fault_dim_z.binarypb` (entries 49–51): ICI-link-failure fallback route tables for 6acc60406 (68,780 B), pufferfish (4,009 B), viperfish (34,504 B).

> **GOTCHA —** each route-cache *shape* appears twice in the TOC (e.g.
> `8x8x8.binarypb.compressed` at entries 27, 54, 57 with sizes 171,623 /
> 173,801 / 176,905 B). These are **distinct blobs** — different fault-count
> instances of the same shape, re-registered under the same basename. The ten
> `.compressed` entries alone are 3,753,440 B (~3.58 MiB), the bulk of the TOC payload. As
> with `architectural_resources.binarypb`, do not dedup on basename: the data
> VAs and sizes differ.

---

## Cross-References

- [Custom Sections](../forensics/custom-sections.md) — owns the existence of the `filewrapper_toc` section in the ELF section table; this page is the catalog of what it indexes.
- [protodesc_cold Catalog](protodesc-cold-catalog.md) — sibling appendix; the `CompressedToroidalRouteCache` / `TpuChipPartsProto` / `TpuCoreProgramProto` schemas referenced here are FileDescriptorProtos in that catalog.
- [Reconstructed Proto Index](reconstructed-proto-index.md) — sibling appendix; the message types that decode these `.binarypb` blobs.
- [Trailing zstd Blob](../forensics/trailing-zstd-blob.md) — the *other* large embedded payload (25.8 MB, trailing the section table); distinct from the MemFile registry documented here.
- [Forensics Overview](../forensics/overview.md) — the binary's section/segment map and the two-embedded-systems distinction.
