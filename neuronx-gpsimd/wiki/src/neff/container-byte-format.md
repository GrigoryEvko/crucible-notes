# NEFF Container Byte Format

> **Scope.** This page opens the **NEFF Part**. NEFF — the *Neuron Executable File
> Format* — is the on-disk model container produced by `neuronx-cc` and consumed by
> `libnrt.so`'s `nrt_load` path. This page reverses it **from the loader side**, byte by
> byte: (1) the fixed **1024-byte header** `neff_header_t`; (2) the inner **gzip-pax-tar**
> archive walk into `neff_t::files`; (3) the **section → exact `libnrt` parser** map keyed
> off `sgNN/def.json`; (4) the **`feature_bits` forward-compat trapdoor**; (5) how a
> **GPSIMD custom-op's device code** rides in as a *ucode library* and is placed into the
> Vision-Q7 IMEM via the Pool engine. Every other page in `neff/` inherits the framing set
> here.
>
> The single binary of record is `libnrt.so` (`aws-neuronx-runtime-lib 2.31.24.0`, host x86-64,
> debug-info + RTTI intact). All offsets that follow are **file offsets into that ELF**, with
> the convention that `.text`/`.rodata` have **VMA == file offset** and — confirmed per
> `readelf -SW` — this build's `.data` *also* has **VMA == file offset** (`.data` VMA `0xc07e00`
> == file offset `0xc07e00`; no delta), so `.data`-resident tables like `NEURON_ENG_NAMES`
> may be dereferenced directly. The container is dissected against a **real embedded NEFF**: a
> complete model is baked into this binary's `.data` at file offset **`0xC07E20`** (the 1024-B
> header) immediately followed by its gzip stream at **`0xC08220`** — a test fixture this page
> carves end-to-end.

**Tag legend.** Each claim carries `[CONF × PROV]`: confidence `HIGH/MED/LOW` × provenance
`OBSERVED` (bytes/struct/string read from the binary or the carved fixture), `INFERRED`
(decompiled control flow / RTTI), `CARRIED` (cited from a sibling page). v2–v4 facts are
byte-grounded; v5 / MAVERICK is header-OBSERVED only and called out where it appears.

---

## 0. The two-level container, at a glance  `[HIGH × OBSERVED]`

A NEFF is a flat C-struct header glued to an in-memory filesystem:

```
file off 0x000   [ 1024-byte neff_header_t                              ]
file off 0x400   [ data_size bytes of inner archive (== header.data_size) ]
```

The header's `header_size` field **is** `0x400` in every observed artifact, and the inner
archive begins at exactly that offset. The inner archive is selected by `pkg_version`
(header `+0x000`):

| `pkg_version` | inner archive | integrity digest |
|:---:|:---|:---|
| `2` (current) | **gzip**-compressed POSIX-pax **tar** | **MD5** (16 B, in `hash[0:16]`) |
| `1` (legacy)  | raw POSIX-pax **tar**                 | **SHA-256** (32 B, full `hash[]`) |

There is **no literal magic number**. NEFF is identified *structurally*: `header_size` (`+0x08`)
sane and `< file_size`; `data_size` (`+0x10`) `<= file_size − 0x400`; `neff_version_major`
(`+0x18`) `<= 2`; and (for `pkg2`) a `1f 8b` gzip stream sitting at offset `0x400`. The header
validators that enforce this are `neff_get_header_from_buffer` @`0x4ca2c0` and `neff_parse`
@`0x4ca3f0`.

> **GOTCHA.** `data_size` is the size of the **compressed** payload, not the inflated tar. For
> the carved fixture `data_size = 0x6AD` (1709 B) of gzip that inflates to **20 480 B** of tar.
> Treat the gzip filter as sitting *in front of* the tar: `libnrt` stacks
> `archive_read_support_filter_gzip` over `archive_read_support_format_tar` and hands it the raw
> `data[data_size]` window.

---

## 1. The 1024-byte header — `neff_header_t`  `[HIGH × OBSERVED]`

IDA ordinal **5896**, size **1024**. Every offset below is exact (`libnrt.so_structures.json`)
and was re-read byte-for-byte out of the embedded fixture at `0xC07E20`:

| off | C type | name | role / observed value in the fixture |
|---:|:---|:---|:---|
| `+0x000` | `u64` | `pkg_version`        | `2` → gzip-tar + MD5; `1` → raw-tar + SHA-256 |
| `+0x008` | `u64` | `header_size`        | `0x400`; validated `< file_size` |
| `+0x010` | `u64` | `data_size`          | inner-archive **compressed** byte count; `0x6AD` |
| `+0x018` | `u64` | `neff_version_major` | hard ceiling **2** (`> 2` ⇒ refuse) — fixture `2` |
| `+0x020` | `u64` | `neff_version_minor` | reported in the version-refusal message |
| `+0x028` | `u8[128]` | `neff_build_version` | `"2.0.21884.0%kaena-tools/develop@6c66f4b"` |
| `+0x0A8` | `u32` | `unused_0`           | producer compression flag (`1` = gzip) — fixture `1` |
| `+0x0AC` | `u8[32]` | `hash`            | `pkg2`: MD5 in first 16 B; `pkg1`: full SHA-256 |
| `+0x0CC` | `u8[16]` | `uuid`            | RFC-4122 model UUID — `5618 26fb 125f 4bb8 …` |
| `+0x0DC` | `char[256]` | `name`         | model/test name — fixture `"x"` |
| `+0x1DC` | `u32` | `requested_tpb_count` | TPBs requested (`1` = single core) |
| `+0x1E0` | `u8[64]` | `tpb_per_node`    | per-NUMA-node TPB counts (all zero in fixture) |
| `+0x220` | `u64` | `feature_bits`       | forward-compat mask (§4) — fixture `0` |
| `+0x228` | `u32` | `vnc_size`           | virtual-NeuronCore size (`1` single, `2` dual-fuse) |
| `+0x22C` | `u8[468]` | `pad`            | reserved |
| `+0x400` | `u8[data_size]` | `data`     | inner archive payload (gzip stream begins here) |

Raw fixture bytes confirming the leading qwords (file offset `0xC07E20`):

```
00c07e20: 02000000 00000000   pkg_version        = 2
00c07e28: 00040000 00000000   header_size        = 0x400
00c07e30: ad060000 00000000   data_size          = 0x6AD
00c07e38: 02000000 00000000   neff_version_major = 2
00c07e48: "2.0.21884.0%kaena-tools/develop@6c66f4b"   (neff_build_version)
00c07ec8: 01000000           unused_0            = 1   (gzip)
00c07ecc: 61b3cab2 4369b326 a2b3ee40 a744c746   hash[0:16] = MD5
00c07ffc: 01000000           requested_tpb_count = 1
00c08040: 00000000 00000000   feature_bits        = 0
00c08048: 01000000           vnc_size            = 1
00c08220: 1f8b0800 …          inner gzip stream   (== 0xC07E20 + 0x400)
```

> **NOTE — `neff_build_version` is your most precise build fingerprint.** It is a free-form ASCII
> string `"<ver>%<branch>@<git-sha>"`. The fixture's `2.0.21884.0%kaena-tools/develop@6c66f4b`
> identifies the exact compiler commit. Prefer this over the numeric `(major,minor)` pair, which is
> a coarse compat gate, not an identity.

### 1.1 Header validation — `neff_get_header_from_buffer` @`0x4ca2c0`  `[HIGH × OBSERVED]`

This is the cheap front gate, returning the header pointer or `NULL`. Annotated from the disasm:

```c
// 0x4ca2c0  neff_header_t* neff_get_header_from_buffer(void* buf, size_t size)
if (buf == NULL)            { nlog("Invalid NEFF buffer"@0x8497b6); return NULL; }       // 0x4ca2c7
if (size <= 0x3FF)         { nlog("Invalid number of NEFF bytes received %lx"@0x82d2d0); // 0x4ca2cc
                             return NULL; }
if (hdr->header_size /*+8*/ >= size)                                                      // 0x4ca2d5
                           { nlog("Invalid NEFF file size hdr sz: %lx, file sz: %lx"@0x82d300);
                             return NULL; }
return (neff_header_t*)buf;                                                               // 0x4ca2de
```

All three error strings were read at their cited `.rodata` addresses, logged via `nlog_write`
@`0x224d40` under subsystem tag `"NEFF"` @`0x8441d9`.

---

## 2. Version gate + integrity — inside `neff_parse` @`0x4ca3f0`  `[HIGH × OBSERVED]`

`neff_parse` calls `neff_get_header_from_buffer`, then drives the version ladder. The relevant
disasm at `neff_parse+0x148`:

```c
v = hdr->neff_version_major;          // mov 0x18(%rax),%rax
if (v > 2) goto reject_version;       // cmp $0x2,%rax ; ja  0x4cb0b8   (4ca53c)
if (v == 2) goto feature_trapdoor;    // je  0x4ca790                   (4ca546)
// v in {0,1}: fall through to data-size + tar walk
if ((file_size - 0x400) < hdr->data_size)   // 4ca550
    reject("Invalid NEFF data size(%lx vs %lx)"@0x82d498);
```

`reject_version` formats `"NEFF version: %lu.%lu is not supported, accepted version range:
%u.x-%u.x"` (@`0x82d360`) with the floor/ceiling = `0..2`.

**Integrity** (only when the caller passes the verify flag): the fixture is `pkg2`, so the loader
runs `MD5_Init/Update/Final` over `data[data_size]` and compares 16 B against `hash[0:16]`;
mismatch ⇒ `"MD5 mismatch!"` (@`0x849833`). The `pkg1` path runs `sha256_*` over the same window
and compares the full 32 B; mismatch ⇒ `"SHA256 mismatch!"` (@`0x84980b`). Both assert `len > 0`
in `neff.cpp` before hashing.

> The integrity digest is **unkeyed** (plain MD5 / SHA-256 over the compressed payload). It detects
> *corruption*, **not** tampering — NEFF carries no signature or MAC.

This was verified directly: computing `MD5(data[0xC08220 : 0xC08220+0x6AD])` reproduces the header
field byte-for-byte:

```
header.hash[0:16]      = 61b3cab2 4369b326 a2b3ee40 a744c746
MD5(data[data_size])   = 61b3cab2 4369b326 a2b3ee40 a744c746   ✓ exact
```

---

## 3. The inner tar walk → `neff_t::files`  `[HIGH × OBSERVED]`

After the version/feature gate, `neff_parse` builds the in-memory filesystem. The libarchive
pipeline (at `neff_parse+0x19b`):

```c
a = archive_read_new();                                  // 0x4ce790
archive_read_support_format_tar(a);                      // 0x4da0c0
archive_read_support_filter_gzip(a);                     // 0x4d1f40   (gzip for pkg2)
archive_read_open_memory(a, hdr + 0x400, hdr->data_size);// 0x4d1810   (no temp file)

while (archive_read_next_header(a, &e) != ARCHIVE_EOF) { // 0x4e3ba0
    if (archive_entry_filetype(e) == 0x4000) continue;   // AE_IFDIR: skip dirs   (cmp $0x4000)
    p = archive_entry_pathname(e);                        // 0x4cc050
    name = std::string(p);
    if (name.rfind("./") == 0) name.erase(0, 2);          // strip leading "./"   (rfind "./"=0x849869)
    if (name endswith 18-byte "wavegraph-bin.json")       // memcmp last 18 B vs 0x84986c
        { archive_read_data_skip(a); continue; }          // skip debug wavegraph IR
    sz = archive_entry_size(e);  if (sz < 0) error;
    buf = malloc(sz);  archive_read_data(a, buf, sz);     // private copy out of the stream
    files[name] = { buf, sz };                            // std::map<string,pair<void*,long>>
}
```

> **CORRECTION (vs DX-NEFF-01 §2).** The 18-byte suffix the loader skips is **`wavegraph-bin.json`**,
> *not* `"checksum"`. The comparison anchor is the byte string at `0x84986c` (`"wavegraph-bin.json"`,
> exactly 18 chars, terminated at `byte_84987e`); `neff_parse` does `memcmp(name_tail,
> 0x84987e − 0x12, 18)`. So the per-graph **debug wavegraph IR** is the entry dropped at load — the
> sibling [concrete-carve](concrete-carve.md) and [container-capstone](container-capstone.md) pages
> must use this corrected suffix. (`def.json` still *references* `wavegraph-bin.json` under
> `debug_info.wavegraph`; in production builds the file is simply absent.)

`neff_t` (ordinal 5894, size 48) is just `{ std::map<std::string, std::pair<void*, long>> files; }`.
The O(log n) name lookup that every later "load file X" goes through is `neff_get_file_content`
@`0x4cb670`: it returns `value.first` (buffer) and `value.second` (size) from the map node
(payload at `node+0x40` / `node+0x48`).

### 3.1 The carved fixture's tar — 19 members  `[HIGH × OBSERVED]`

Inflating the fixture's gzip (`0xC08220`, `0x6AD` B) yields a 20 480-B POSIX tar with **19**
members (the lone `sg00` directory entry is dropped by the `AE_IFDIR` skip, leaving 18 files in
`neff_t::files`):

| member | size | role |
|:---|---:|:---|
| `kelf-a.json`       | 129 B | top-level kelf descriptor (graphs[], version, target) |
| `neff.json`         | 980 B | TVM/NNVM host graph manifest (nodes / `tvm_op` / `__kelf`) |
| `sg00/` *(dir)*     | 0 B   | sub-graph marker — **skipped** (`AE_IFDIR`) |
| `sg00/def.json`     | 1421 B | sub-graph manifest (var table, dma_queue, engines, …) — §5 |
| `sg00/pe.json`      | 63 B  | PE-array engine descriptor (`{dma, instr, name}`) |
| `sg00/pe.bin`       | **64 B** | PE sequencer program — one 64-byte slot |
| `sg00/pe.asm`       | 153 B | human-readable disasm of `pe.bin` (debug) |
| `sg00/act.json`     | 182 B | Activation-engine descriptor + tables |
| `sg00/act.bin`      | 0 B   | empty engine (no Activation program) |
| `sg00/act.asm`      | 0 B   | — |
| `sg00/dve.json`     | 65 B  | Data-Vector-engine descriptor |
| `sg00/dve.bin`/`.asm` | 0 B | empty |
| `sg00/sp.json`      | 63 B  | Sync/Scalar-sequencer descriptor |
| `sg00/sp.bin`/`.asm` | 0 B  | empty |
| `sg00/pool.json`    | 1042 B | Pool/DMA-router descriptor (2 DMA queues) |
| `sg00/pool.bin`     | **192 B** | Pool sequencer program — three 64-byte slots |
| `sg00/pool.asm`     | 126 B | disasm of `pool.bin` |

`kelf-a.json` (verbatim):

```json
{ "graphs": [ { "definition": "sg00/def.json", "name": "sg00" } ],
  "version": "0.5", "target": "*" }
```

`target: "*"` = architecture-neutral (already lowered). The host `neff.json` wraps the kelf as a
single `tvm_op` node `sg_tonga0` with `attrs.func_name = "__kelf"` and `attrs.kelf =
"kelf-a.json"` — the host-graph → kelf hand-off. (`tonga` is the V1 codename; the node name is
producer cosmetic, not an arch selector.)

---

## 4. The `feature_bits` forward-compat trapdoor  `[HIGH × OBSERVED]`

When `neff_version_major == 2`, `neff_parse` takes the trapdoor branch at `0x4ca790`:

```c
// 0x4ca790
mask = 0x7FFFFFFFF8000000;                          // movabs $0x7ffffffff8000000
if ((hdr->feature_bits /*+0x220*/ & mask) != 0)     // and 0x220(%r15),%rax ; je continue
    reject("This NEFF (version: %lu.%lu, features: 0x%lx) has been compiled by a newer "
           "version of Neuron compiler. Features supported by this Neuron Runtime: 0x%lx. "
           "Please update the aws-neuronx-runtime-lib package…"@0x82d3b0,
           major, minor, feature_bits, /*supported=*/ 0x7FFFFFF);   // push $0x7ffffff
// else: feature_bits all within bits 0..26 → proceed
```

The constant `0x7FFFFFFFF8000000` is the **negative** of the supported space: bits **0..26** are
the currently-defined feature space (the message prints `0x7FFFFFF` = `(1<<27)-1` as "supported"),
and **any of bits 27..62 set** triggers a clean refusal. The MSB (bit 63) is *outside* the mask, so
it is not itself a trapdoor bit.

> This is the format's only forward-compat mechanism and it is deliberately conservative: an
> *unknown* future feature bit causes a **clean refusal with an actionable message**, never a
> silent mis-execution. A reimplementer adding a new compiler feature must (a) claim a bit in
> `0..26` *and* (b) ship the matching runtime support, or the runtime will reject the NEFF. The
> fixture's `feature_bits == 0` ⇒ vanilla NEFF, no optional features.

---

## 5. Section → parser map — `kelf_load_from_neff` @`0x4c0870`  `[HIGH × OBSERVED]`

For each `graph` in `kelf-a.json`, the runtime calls `kelf_load_from_neff(neff, graph_name,
def_path, &mla_resources)`. It loads `sgNN/def.json` via **simdjson** and consumes the keys in a
fixed order; this is the canonical *section → exact `libnrt` parser* mapping the sibling pages
build on:

| `def.json` key | `libnrt` parser / sink | sibling page |
|:---|:---|:---|
| `name` | graph-name string | — |
| `var { }` | **`parse_one_variable`** @`0x4b36b0` → `mem_ref`[ ] into `mla_resources.MI` | [metaneff-io-abi](metaneff-io-abi.md) |
| *(post-`var`)* dense-`var_id` check | `max(var_id)+1 == map.size` invariant | [metaneff-io-abi](metaneff-io-abi.md) |
| *(post-`var`)* `num_outputs > 0` | "no Outputs" reject | — |
| `dma_queue { }` | **`parse_one_dma_ring`** @`0x4b5f80` → `mla_resources.DI.dma_queues` | [relocation-weights](relocation-weights.md) |
| `replica_groups [ ]` | `parse_replica_groups` (collectives topology) | — |
| `src_target_pairs [ ]` | `parse_src_target_pairs` (collectives routing) | — |
| *(if `arch_type == 2`)* default Q7 lib | `ucode_get_q7_lib` @`0x2265a0` → `ext_isa_ucode_lib_def` | §6 |
| `ucode_lib` *(→ file)* | **`parse_one_ucode_lib`** @`0x4b1610` | [seq-microcode](seq-microcode.md) / §6 |
| `runtime_statebuffer_reservation [ ]` | `parse_sb_carveouts` (SB carveouts) | [version-compat](version-compat.md) |
| `cc_stream` / `num_streams` | `CCSTMI.num_streams` | — |
| *(fp8 block)* `n_config` | `parse_fp8_conversion_config` | — |
| per-engine `<eng>.json: dma[]` | **`parse_one_dma_block`** @`0x4bc620` | [relocation-weights](relocation-weights.md) |
| per-engine `<eng>.json: instr→.bin` | **`parse_one_engine_instr`** @`0x4b7e30` → `load_bin_file` @`0x4ae500` | [seq-microcode](seq-microcode.md) |
| *(engine absent)* | empty placeholder + WARN `"Engine %s not found in NEFF %s, using empty placeholder"`@`0x82d050` | — |

> **GOTCHA — two distinct `arch` numberings.** The `arch_type` tested above is the **software/HAL
> ordinal** `al_hal_tpb_arch_type_t` (`SUNDA = 2`, `CAYMAN = 3`, `MARIANA = 4`, `NUM = 5`), *not*
> the hardware `arch_id` codename byte (`0x05` SUNDA / `0x0c` CAYMAN / `0x14` MARIANA / `0x1c`
> MARIANA_PLUS) carried elsewhere in the wiki. `arch_type == 2` ⇒ inject the default Q7 ExtISA lib
> (§6); `arch_type == 3` is the legacy-NEFF guard below. Do not conflate the two scales.

**Legacy-NEFF guard.** If `runtime_statebuffer_reservation` is missing/corrupt **and**
`arch_type == 3` **and** env `NEURON_RT_ALLOW_LEGACY_NEFF` is not set, the loader rejects with
`"Missing or corrupted runtime_statebuffer_reservation field in def.json and
NEURON_RT_ALLOW_LEGACY_NEFF=1 is not set…"` (@`0x82cf70`).

### 5.1 The five engine names — `al_hal_tpb_get_tpb_eng_names` @`0x44bd00`  `[HIGH × OBSERVED]`

```c
// 0x44bd00  the whole function:
const char** al_hal_tpb_get_tpb_eng_names() { return &NEURON_ENG_NAMES; }  // lea 0xc09600
```

`NEURON_ENG_NAMES` @`0xc09600` (`.data`, VMA==file off) is five `char*` indexed by
`al_hal_tpb_eng_type`:

| idx | `al_hal_tpb_eng_type` | string ptr | name |
|---:|:---|:---|:---|
| 0 | `AL_HAL_TPB_ENG_PE`   | `0x8495bc` | `pe`   |
| 1 | `AL_HAL_TPB_ENG_ACT`  | `0x849865` | `act`  |
| 2 | `AL_HAL_TPB_ENG_POOL` | `0x841143` | `pool` |
| 3 | `AL_HAL_TPB_ENG_DVE`  | `0x847bf3` | `dve`  |
| 4 | `AL_HAL_TPB_ENG_SP`   | `0x8454bd` | `sp`   |

`MAX_ENG = 5` for SUNDA/CAYMAN/MARIANA alike. These names are exactly the `sg00/{pe,act,pool,dve,
sp}.{json,bin,asm}` filenames in the tar — the array order (`POOL` before `DVE`) is the file
naming convention. Each engine's `<eng>.bin` is fetched by `parse_one_engine_instr` →
`load_bin_file` @`0x4ae500` → `neff_get_file_content` (a `malloc`+`memcpy` private copy), stored as
`instr_set { uint8_t* buffer; uint32_t size; }` (size 16) in `mla_resources.INS.instr_sets`.

> The `<eng>.bin` is **not** Vision-Q7 / Xtensa code. It is a **TPB sequencer** instruction stream —
> the on-chip engine microcode — in **64-byte slots** (proven in §7). The byte-level ISA decode of
> these slots is the sibling [seq-microcode](seq-microcode.md) page; here we only fix the framing:
> 16-bit LE lead opcode per slot, one slot = 64 B.

---

## 6. GPSIMD custom-op: device-code embedding & placement  `[HIGH × OBSERVED/INFERRED]`

A GPSIMD custom-op's device code rides in the NEFF as a **ucode library**. There are two injection
paths, both terminating in the same `ucode_lib` struct.

**(A) Default Q7 library — baked into the runtime, *not* the NEFF.** On `arch_type == 2`,
`kelf_load_from_neff` calls `ucode_get_q7_lib` @`0x2265a0` *before* any per-NEFF lib:

```c
// 0x2265a0  ucode_get_q7_lib(out):
a = al_hal_tpb_get_arch_type();          // 2,3,4 = SUNDA,CAYMAN,MARIANA
i = a - 2;  if (i > 2) return 2;         // sub $0x2 ; cmp $0x2 ; ja fail
lib_id = CSWTCH_113[i];                  // table @0x86ada8 = { 6, 13, 21 }
return ucode_lib_get_ext_isa(lib_id, NRTUCODE_FLAVORS_DEFAULT/*=0*/, out);  // call *ucode_lib_get_ext_isa
```

The default Extended-ISA library is resolved from the **sibling shared object
`libnrtucode_extisa.so`** via the imported `ucode_lib_get_ext_isa`. It is registered under the JSON
key `ext_isa_ucode_lib_def` (@`0x84963b`) with `flags = 6`, capped at **18** ExtISA ops
(`"Number of ExtISA Op trying to load %lu exceeds the maximum %u"`@`0x82cea0`); zero functions ⇒
`"No default Extended Isa functions found… will lead to hangs."` (@`0x82ce38`).

> **NOTE.** `CSWTCH.113` @`0x86ada8` = `{ 6, 13, 21 }` (LE `u32`, indexed by `arch_type − 2`) is a
> **third** numbering distinct from both `arch_type` (2/3/4) and the hardware `arch_id`
> (`0x05`/`0x0c`/`0x14`): these are **ExtISA library identifiers** handed to
> `ucode_lib_get_ext_isa`. (DX-NEFF-01 cited "`CSWTCH_94`"; the live symbol in this build is
> `CSWTCH.113`.)

**(B) NEFF-supplied library.** `def.json` key `ucode_lib` names a JSON manifest inside the tar
(e.g. `ucode_lib.json`). The loader logs `"Loading ucode libs specified in ucode_lib.json from the
NEFF. This interface is not yet entirely stable - FUTURE API CHANGES ARE POSSIBLE!"` (@`0x82cee0`)
and runs `parse_one_ucode_lib` @`0x4b1610` per element.

### 6.1 `parse_one_ucode_lib` — the custom-op decoder  `[HIGH × OBSERVED]`

Decompiled key handling, with the byte-exact opcode binding (`@0x4b1610`):

```c
// keys read with simdjson at_key:
"library"     → load_bin_file(neff, path) → neff_get_file_content → malloc+memcpy a private
                copy of the raw device-code bytes out of the tar map.  (missing ⇒ fail)
"version"     → parse_version_string; checked against NRT ucode version "1.21.1.0" (@0x84fe68,
                inlined @0x4b1c08). mismatch ⇒ "Mismatch between GPSIMD lib version and NRT
                version!" (@0x82c240); missing ⇒ "Missing GPSIMD lib version!".
"opcode"      → uint64. flags = (opcode == -123) ? 0 : 6;       // v31=0; if(first!=-123) v31=6
                // -123 == 0x85 == 133 marks a BUILT-IN/registered op (flags 0); any other opcode
                // ⇒ a USER ExtISA custom-op (flags 6).
"cpu_id"      → uint64; assert cpu_id == 0   (kelf2kbin.cpp:0x691, "cpu_id == 0" @0x848ff0)
"total_cpus"  → uint64; reject if (n > 1 && n != 8):
                "UCode Library %s has invalid number of total cpus %u" (@0x82c310).
                // 1 ⇒ one Vision-Q7 core; 8 ⇒ all eight (the ncore2gp cluster).
duplicate name ⇒ "UCode Library %s has already been added" (@0x82c2e8).
"functions"[] → parse_one_ucode_lib_function @0x4b1180 per element.
```

The in-memory `ucode_lib` (**size 0x50 / 80**, confirmed by `operator new(0x50u)`):

| off | type | field | binding |
|---:|:---|:---|:---|
| `+0x00` | `std::string` | `name` | library name (map key) |
| `+0x20` | `void*` | `content` | raw device-code bytes from the tar (`library`) |
| `+0x28` | `u64` | `content_size` | |
| `+0x30` | `u32` | `flags` | `0` = built-in (opcode `0x85`); `6` = user ExtISA |
| `+0x34` | `u8` | `cpu_id` | `0` |
| `+0x35` | `u8` | `total_cpus` | `1` or `8` |
| `+0x38` | `std::vector` | `functions` | `function_entry`[ ] |

`function_entry` (size 40): `+0x00 std::string name`, `+0x20 u8 opcode`, `+0x21 u8 sub_opcode` —
`parse_one_ucode_lib_function` @`0x4b1180` reads keys `"opcode"` (required) and `"sub_opcode"`
(optional) and packs them as one `WORD` at `+0x20`. Every custom-op function lands in
`ucode_lib.functions`, keyed by `(opcode, sub_opcode)`.

### 6.2 On-device placement  `[MED × INFERRED]`

`ucode_lib.content` is a **Vision-Q7 (Cairo NX, core `ncore2gp`) device program**. The host hands
it to the `nrtucode` layer (in `libnrtucode_*`):

- `nrtucode_get_memory_image` (string @`0x84439b`) builds the device memory image;
- `nrtucode_ll_get_load_sequence` / `nrtucode_ll_get_unload_sequence` (@`0x84454b`) emit the
  load/unload **instruction sequences** that DMA the image into the GPSIMD core's IMEM.

Core kinds enumerate **eight** getters — four codenames × two host engines:
`NRTUCODE_CORE_{SUNDA,CAYMAN,MARIANA,MARIANA_PLUS}_NX_{POOL,DVE}`. The runtime drives the cores
through **Pool stdio queues** (`tpb->pooling_q7_nrtucode_core[0] != NULL` @`0x80eee8`; `"Failed to
alloc memory… Pool stdio queue. Neuron core %u, GPSIMD core %u"` @`0x815b10`; `"GPSIMD stdio queue
overflowed…"` @`0x815ed8`; `"Failed to copy GPSIMD stdio block info table to device memory"`
@`0x815dd0`). Per-engine HAL init is `aws_hal_q7_ucode_eng_init` @`0x451080` (asserts the HAL
vtable slot `kaena_khal.khal_q7.ucode_eng_init`; arch variants `_sunda` / `_cayman` / `_mariana`),
sourced from `KaenaHal-2.31.0.0/.../common/q7/aws_hal_q7.c`.

> **End-to-end:** NEFF tar `library` bytes → `ucode_lib.content` (host) → `nrtucode` memory image →
> `nrtucode_ll_get_load_sequence` DMA into GPSIMD Q7 IMEM → dispatched **by opcode** through the
> **Pool** engine's stdio queues. The per-`(gen × engine)` firmware images this references are the
> same blobs catalogued in the [images Part](../images/image-catalog-index.md).
>
> **CORRECTION (vs DX-NEFF-01 §6).** The GPSIMD cores are **not** Pool-only: this build exposes both
> `…_NX_POOL` *and* `…_NX_DVE` getters for all four codenames. The *host driver* path documented
> here (stdio queues, `pooling_q7_nrtucode_core`) is the **POOL** attachment; the **DVE** attachment
> is a parallel host engine. The single-core (`total_cpus == 1`) case targets one Vision-Q7 core;
> `total_cpus == 8` targets the eight-core `ncore2gp` cluster.

---

## 7. Engine `.bin` = 64-byte-slot sequencer microcode  `[HIGH × OBSERVED]`

The fixture grounds the slot geometry that §8 (relocation) independently confirms. `pe.bin` is a
single 64-byte slot; its first 32 bytes:

```
pe.bin (64 B, one slot):
  00: c8 10 00 00   LE lead opcode = 0x10C8
  04: 04 0a 13 16   (predicate / semaphore operands)
  08: 00 00 00 00   04 0a 00 00
  10: 03 00 00 00   = input_tensor_id  = 3
  14: 04 00 00 00   = output_tensor_id = 4
  18: 20 00 00 00   = num_elements     = 32
  1c: 00 00 00 00
```

The matching `pe.asm` (debug disasm of the *same* bytes):

```
PSEUDO_TRIGGER_COLLECTIVE $S[10]>0 $S[22]++@complete ctype=ALL_REDUCE
  input_tensor_id=3 output_tensor_id=4 num_elements=32 dtype=fp32 op=ADD group_id=0;
```

`pool.bin` is **192 B = three 64-byte slots**, lead opcode `0x10C1`, and at `+0x0C` of slot 0 it
embeds the ASCII queue name `q_gradient_in` — the Pool engine's DMA-trigger program, cross-linked
to the `pool.json` `dma_queue` entries. `act.bin` / `dve.bin` / `sp.bin` are **0 bytes** (empty
engines get a 0-byte `.bin`; the loader installs an empty placeholder `instr_set` + WARN). So: a
16-bit LE opcode leads each 64-byte slot, and the `.asm` is the debug-only disassembly of the
identical bytes. The full opcode/operand-field decode is [seq-microcode](seq-microcode.md).

---

## 8. Relocation framing — 64-byte slots, confirmed independently  `[HIGH × OBSERVED]`

Engine streams are compiled against a *device IP* space and relocated into the per-model *NEFF IP*
space at load — the NEFF analog of ELF dynamic relocations. The IP-walk `get_neff_ip` @`0x2faee0`
strides a `kbin_patch_location_t[ ]` (size **16**, confirmed by `shl $0x4` indexing):

```c
// kbin_patch_location_t : +0 u32 offset ; +4 u32 count ; +8 kbin_patch_type_t type ; +12 kbin_patch_section_t section
for (each entry, matching section at +0xC):                 // cmp 0xc(%rax),%ebp
   switch (type) {                                           // mov 0x8(%rax),%r12d
     case KBIN_PATCH_TYPE_MODIFY /*1*/: ip += 0x40;          break;   // single 64-B slot
     case KBIN_PATCH_TYPE_DELETE /*2*/: ip += 0x40 * count;  break;   // count slots
     case KBIN_PATCH_TYPE_INSERT /*0*/: ip += (count << 6);  break;   // count*64
     default: assert("Invalid patch type");                           // 0x2fafad
   }
// remaining span:  (end - ip) >> 6   →  number of 64-B slots
```

The `0x40` / `<<6` strides (`add $0x40,%r8`; `shl $0x6,%esi`; trailing `shr $0x6`) **prove TPB
sequencer instructions are 64-byte slots** — exactly matching `pe.bin` (64 = 1 slot) and `pool.bin`
(192 = 3 slots). The patch enums:

- `kbin_patch_type_t`: `INSERT = 0`, `MODIFY = 1`, `DELETE = 2`
- `kbin_patch_section_t`: `PREAMBLE = 0`, `POSTAMBLE = 1`, `MAIN = 2`, **`FUNCATION = 3`** *(symbol spelled with the producer's typo — kept verbatim)*

`kbin_patch_device_ip_to_neff_ip` @`0x2fb3a0` indexes the per-section table at
`model + 0x18A + section*16` (`lea 0x18a(%rcx)`, then `add $0x10` per section), maps a device IP
into the MAIN (then FUNCTION) instruction buffer, and reports `"Failed to find section %d in the
patch list"` (@`0x813d98`) / `"device_ip 0x%lx is out of bounds of main and function buffer"`
(@`0x813e00`). `kbl_model_get_kbin_patch_info` @`0x3076d0` copies, per engine, `kbin_patch_info.
eng_patch[i]` (`kbin_eng_patch_t`, size 16: `count`, `array_count`, `locations*`) for the
device-side builder. The full subsystem is [relocation-weights](relocation-weights.md).

---

## 9. SB carveouts — `runtime_statebuffer_reservation`  `[HIGH × OBSERVED]`

`parse_sb_carveouts` loops the array. The **only** accepted `type` string is the 8-character
literal **`evtaccel`** — compared as a qword `0x6C65636361747665` (`"evtaccel"` little-endian) ⇒
`KBIN_SB_CARVEOUT_TYPE_EVTACCEL (1)`; anything else ⇒ `"Invalid runtime_statebuffer_reservation
type %s"` (@`0x82d020`). The `sb_carveout` struct (size 40): `+0x00 type`, `+0x08 offset`,
`+0x10 size`, `+0x18 start_partition`, `+0x20 num_partitions`. These reserve on-chip State-Buffer
regions so the runtime's own allocator stays out of compiler-claimed SB space.

> **CORRECTION (vs DX-NEFF-01 §7).** The carveout type literal is **`evtaccel`** (8 chars, with the
> `t`), matching both the qword `0x6C65636361747665` and the enum `KBIN_SB_CARVEOUT_TYPE_EVTACCEL`
> — *not* `"evaccel"` (7 chars). An 8-byte qword compare only makes sense for an 8-byte string. The
> fixture's `runtime_statebuffer_reservation` is `[]` (no carveouts).

---

## 10. Producer ↔ consumer recap  `[HIGH × OBSERVED / INFERRED]`

**Consumer (`libnrt.so`):**

```
nrt_load → nrt_load_util → neff_parse @0x4ca3f0
    neff_get_header_from_buffer @0x4ca2c0   (1024-B header, version ≤ 2, feature_bits gate)
    MD5/SHA-256 verify (over compressed data[data_size])
    archive_read_* tar walk → neff_t::files     (gzip+pax, in-memory, no temp files)
  → kelf_load_from_neff @0x4c0870  (per graph in kelf-a.json)
      simdjson(def.json) → parse_one_variable / parse_one_dma_ring / parse_replica_groups /
      parse_src_target_pairs / ucode_get_q7_lib / parse_one_ucode_lib / parse_sb_carveouts /
      parse_fp8_conversion_config / per-engine: parse_one_dma_block + parse_one_engine_instr
  → kbin build + kbin_patch_device_ip_to_neff_ip (relocate)
  → nrtucode memory-image + load-sequence for GPSIMD Q7 libraries (Pool / DVE engine).
```

**Producer (`libwalrus.so` in `neuronx-cc`, `[INFERRED]`):** `NeffPackager::writePackageFile`
emits `neff.json` + `def.json`, `NeffFileWriter::initializeNeffHeader` fills the 1024-B header,
`writeArchiveFile` gzips the tar with an incremental MD5 — the exact mirror of the consumer above.
The packager round-trip is detailed in [version-compat](version-compat.md); the byte-for-byte
re-carve of this fixture is [concrete-carve](concrete-carve.md); the consolidated container view is
[container-capstone](container-capstone.md). The host `nrt_load` call spine is
[runtime/callgraph-spine](../runtime/callgraph-spine.md) and the loader surface is
[runtime/libnrt-surface](../runtime/libnrt-surface.md).

---

## Key design points  `[HIGH]`

1. **Two-level, self-contained, no magic.** 1024-B C-struct header + in-memory gzip-pax-tar
   (libarchive, `read_open_memory` — zero temp files). Identified structurally, not by a magic
   number.
2. **`feature_bits` bits 27..62 are a forward-compat trapdoor** (mask `0x7FFFFFFFF8000000`): clean
   refusal vs silent mis-execution; bits 0..26 are the defined feature space.
3. **Integrity is unkeyed** (MD5 `pkg2` / SHA-256 `pkg1` over the *compressed* payload) — corruption
   detection, not authentication.
4. **Engine `.bin` = 64-byte-slot TPB sequencer microcode** (not Xtensa), one per engine
   `{pe,act,pool,dve,sp}`; missing engines ship a 0-byte `.bin` + placeholder.
5. **GPSIMD custom-op device code = a ucode library:** raw bytes under `library`, `opcode 0x85`
   (`-123`) marks built-in vs user ExtISA (`flags 0` vs `6`), `total_cpus ∈ {1,8}` targets one or
   all eight Vision-Q7 cores; placed via the `nrtucode` memory image + load sequence, dispatched
   through the **Pool** (or DVE) engine. The default ExtISA lib lives in `libnrtucode_extisa.so`,
   **not** the NEFF.
6. **Relocation = `kbin_patch_location_t` lists** (`INSERT/MODIFY/DELETE` × `PREAMBLE/POSTAMBLE/
   MAIN/FUNCATION`) per engine, applied at load by `get_neff_ip` / `kbin_patch_device_ip_to_neff_ip`.
7. **SB carveouts** (`evtaccel` / `EVTACCEL`) fence State-Buffer regions away from the runtime
   allocator.

---

### Corrections logged vs DX-NEFF-01 (for sibling consistency)

| § | DX-NEFF-01 claim | binary truth (this page) |
|:---:|:---|:---|
| §2 | skipped 18-byte suffix = `"checksum"` | **`wavegraph-bin.json`** (`memcmp` vs `0x84986c`) |
| §6 | default-lib table = `CSWTCH_94` | live symbol = **`CSWTCH.113`** @`0x86ada8` = `{6,13,21}` |
| §6 | GPSIMD cores Pool-only | also **`…_NX_DVE`** getters for all four codenames |
| §7 | carveout type = `"evaccel"` (7 ch) | **`evtaccel`** (8 ch) = qword `0x6C65636361747665` |
| §10 | section enum `FUNCTION` | symbol is spelled **`FUNCATION`** (producer typo, kept verbatim) |
