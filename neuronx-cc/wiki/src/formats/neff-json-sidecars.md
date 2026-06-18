# NEFF JSON Sidecars

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310). Producer addresses are from `neuronxcc/starfish/lib/libwalrus.so` (dynamic symbol table, not stripped); consumer addresses are from `neuronxcc/kra/NeffInfo.cpython-310-x86_64-linux-gnu.so` (Cython, with debug_info). Two distinct VA frames — never mix them. Other versions and other Python ABIs (cp311/cp312) will differ.*

## Abstract

A compiled `.neff` is not a monolithic blob: it is a libarchive **PAX tar** container — optionally gzip-compressed — prefixed by a 1024-byte binary `neff_header` region. Inside that tar sit a small family of human-readable JSON members that together describe the graph the runtime must execute: which Neuron cores exist, where each tensor lives in DRAM/SBUF, how the input/output buffers alias, what collective-compute world size is required, and what compiler feature flags are set. This page documents the **four schema-bearing sidecars** — `info.json`, `neff.json` (v0.5), `def.json` (v0.6), and `tensor_map.json` — and the **Cython reader** (`neuronxcc.kra.NeffInfo`) that extracts and parses them.

The reader is the surprising part. `NeffInfo` does **not** link libarchive and walk the tar in C++. It is a Cython module that **shells out to `dd | tar`**, skipping the 1024-byte header with `bs=1024 skip=1`, lists the members, selects one by a **Python regex over the sorted member listing** (`re.findall` against a `file_rgx`), extracts it with `tar … -O <member>`, and feeds the bytes to `json.loads`. This regex-by-name selection is what lets a caller reach `sg00/def.json` or `sgLnk/sg00/tensor_map.json` without knowing the core index — the index is absorbed into a `sg\d+/…` pattern. The producer side is the mirror image: a C++ `NeffPackager` in libwalrus builds each member with `nlohmann::json` and writes the tar with libarchive.

The schemas relate to siblings as follows. `info.json` carries the POD that seeds the binary `neff_header` (cross-referenced to the [NEFF-header / BOM-writer page](neff-header-bom-writer.md), 12.2). `neff.json` is the subgraph manifest whose nodes are `__kelf`-backed (the `__kelf` node itself is the subject of the [NEFF/KELF-node page](neff-kelf-node.md), 12.7); each node points at a `kelf-<N>.json` (the [KELF-JSON page](kelf-json.md), 12.6). `def.json`'s `CCInfo` sub-table and its `replica_groups` structure are revisited from the collectives angle in Part 13. Those four are mentioned here for orientation; this page owns the four sidecar schemas, the `dd|tar` access path, and the `NeffInfo` accessor roster.

For reimplementation, the contract is:

- **The container access path** — PAX tar with a 1024-byte header skip, member selection by name-regex (`dd bs=1024 skip=1 | tar [xvf|xvzf] - -O <rgx-matched member>`), not by offset.
- **The four member schemas** — `info.json` (header oracle), `neff.json` v0.5 (the `sg_coreV1` `__kelf` subgraph table), `def.json` v0.6 (the var-table + DMA-queue table + CC/ucode/fp8/features sub-tables), and `tensor_map.json` (`inputs` + `alias_map` + `must_alias`).
- **The accessor roster** — the `NeffInfo` getters, what member each reads, and what dict path each returns; in particular `getArchType` (from `kelf-0.json`) and `getCCRankWorldSize` (the world-size max over `replica_groups` / `src_target_pairs` / `#rank_world_size`).

| | |
|---|---|
| **Container** | libarchive PAX tar (`+gzip` optional), prefixed by 1024-byte `neff_header` |
| **Reader** | `neuronxcc.kra.NeffInfo.NeffInfo` (Cython, `__pyx_*`), shells to `dd`/`tar`, `json.loads` |
| **Reader member-selection** | `findInNeff` @ `0x19b10` — `file_rgx` via `re.findall` over sorted `tar tf` listing |
| **Producer** | `neuronxcc::backend::NeffPackager` / `NeffFileWriter` in libwalrus.so (`nlohmann::json` + libarchive) |
| **`info.json` consumer** | `NeffFileWriter::findInfoJson` @ `0x153ca20` → `initializeNeffHeader` @ `0x1540a00` |
| **`neff.json` version / writer** | `0.5` (string @ libwalrus `+594113`) · `writeNeffJson` @ `0x152c740` |
| **`def.json` version / writer** | `0.6` (string @ libwalrus `+594084`, adjacent to `definition`) · `writeDefJson` @ `0x152a0e0` |
| **`tensor_map.json` keys** | `inputs`, `alias_map`, `must_alias`, `input_name`, `output_name`, `name` |

---

## 0. Container Model and the `dd|tar` Access Path

### Purpose

Everything else on this page presupposes that the JSON members can be *found*. The `.neff` is a PAX tar archive whose first 1024 bytes are not a tar block at all — they are the binary `neff_header` POD (`neff_header + 8 == 0x400 == 1024`, the header-region size field). A naïve `tar tf file.neff` therefore fails: tar sees garbage where it expects its first 512-byte block header. The reader works around this by `dd`-skipping exactly one 1024-byte block before piping into tar.

### Entry Point

```text
NeffInfo.parseFile (0x16e40)           ── open + cache neff path
  └─ NeffInfo.findInNeff (0x19b10)     ── member access by regex
       ├─ subprocess.check_output("dd if=<neff> bs=1024 skip=1 | tar tf - | sort")   ── list
       └─ subprocess.check_output("dd if=<neff> bs=1024 skip=1 | tar xvf - -O <member>")  ── extract
```

### Algorithm

`findInNeff` is the single member-access primitive every getter funnels through. It lists the members, regex-matches one name, then extracts it — choosing the plain vs. gzip tar flags by whether the archive is compressed.

```c
// __pyx_pw_..._findInNeff @ 0x19b10  (NeffInfo.so VA frame)
function findInNeff(neff_path, file_rgx, gzipped):
    // 1. list members, skipping the 1024-byte binary header region
    if gzipped:
        listing = check_output("dd if=" + neff_path +     // literal "dd if="
                               " bs=1024 skip=1 | tar tzf - | sort")
    else:
        listing = check_output("dd if=" + neff_path +
                               " bs=1024 skip=1 | tar tf - | sort")
    members = listing.decode("utf_8").split()

    // 2. select the member by NAME-REGEX, not by offset (re.findall)
    matches = []
    for m in members:
        if re.findall(file_rgx, m):                       // symbols: re, findall, file_rgx, match
            matches.append(m)
    member = matches[0]                                   // sg-index varies; rgx absorbs it

    // 3. extract that one member to stdout (-O), discard tar chatter on stderr
    if gzipped:
        data = check_output("dd if=" + neff_path +
                            " bs=1024 skip=1 | tar xvzf - -O " + member,
                            stderr=DEVNULL)
    else:
        data = check_output("dd if=" + neff_path +
                            " bs=1024 skip=1 | tar xvf - -O " + member,
                            stderr=DEVNULL)
    return data                                           // bytes → json.loads in callers
```

The four command templates are present verbatim as `__pyx_kp_u_*` string constants in NeffInfo.so:

```text
 bs=1024 skip=1 | tar tf - | sort
 bs=1024 skip=1 | tar tzf - | sort
 bs=1024 skip=1 | tar xvf - -O 
 bs=1024 skip=1 | tar xvzf - -O 
dd if=
```

### Member-Name Patterns

The member names the reader knows how to resolve, as literal string constants in NeffInfo.so. Note that some have two on-disk layouts (top-level vs. per-core, and `sgLnk/` vs. flat `sg<N>/`); the regex form is what tolerates both.

| Member | Literal pattern(s) | Layout | Confidence |
|---|---|---|---|
| KELF index | `kelf-0.json`, `kelf-<N>.json` | top-level, fixed name (+ per-core variant) | CONFIRMED |
| Per-core definition | `sg00/def.json`, `sg\d+/def.json` | per-core; regex-resolved | CONFIRMED |
| Top metadata | `info.json` | top-level | CONFIRMED |
| Subgraph table | `neff.json` | top-level | CONFIRMED |
| Tensor map | `sgLnk/sg00/tensor_map.json`, `sg\d+/tensor_map.json` | per-core, two layouts | CONFIRMED |

> **GOTCHA —** `tensor_map.json` appears under **both** `sgLnk/sg00/` and a flat `sg<N>/`. A reimplementation that hardcodes `sg00/tensor_map.json` will miss it on the other layout. The reader's defence is the regex `sg\d+/tensor_map.json` — callers must select by pattern, never by a fixed path.

> **QUIRK —** the reader is Cython (`__pyx_*` symbols), and the tar parsing is done by **external `dd` and `tar` processes**, not by an in-process libarchive call. The C++ libarchive code in this toolchain is on the **producer** side (`NeffFileWriter`), not here. So the reader's correctness depends on `dd`/`tar` being on `PATH` and on the GNU `tar -O` semantics — a detail a pure-C++ reimplementer would never guess from the schema alone.

---

## 1. The `NeffInfo` Accessor Layer

### Purpose

`neuronxcc.kra.NeffInfo.NeffInfo` is the **tooling / test-infrastructure** reader (kra = "Kaena Runtime API" surface). It is *not* the on-device runtime parser — libnrt parses the NEFF at the protobuf/binary level; `NeffInfo` is the JSON-sidecar view used by the compiler's own tools (e.g. `analyze_neff_artifacts.py`). Every getter is the same three-line shape: regex-extract a member, `json.loads` it, traverse the dict. The module imports `os, re, subprocess, json, numpy(np), functools (lru_cache/reduce), gc`; member extraction is memoized with `@lru_cache`.

### Accessor Roster

All wrapper addresses are in the NeffInfo.so VA frame (`__pyx_pw_9neuronxcc_3kra_8NeffInfo_8NeffInfo_*`). Every qualname appears as a `neuronxcc.kra.NeffInfo.NeffInfo.<m>` string and as a `__pyx_k_<m>` constant.

| Accessor | Wrapper @ | Member read | Returns | Confidence |
|---|---|---|---|---|
| `parseFile` | `0x16e40` | (opens neff, caches path) | self | CONFIRMED |
| `findInNeff` | `0x19b10` | (the `dd|tar` primitive) | raw member bytes | CONFIRMED |
| `getKelf` | `0xf640` | `kelf-0.json` | raw text/bytes | CONFIRMED |
| `getInfoJson` | `0x100c0` | `info.json` | dict | CONFIRMED |
| `getNeffJson` | `0xf100` | `neff.json` | dict | CONFIRMED |
| `getDefJson` | `0xfb80` | `sg\d+/def.json` | dict | CONFIRMED |
| `getNeffFile` | `0x10600` | — | neff path/handle | CONFIRMED |
| `getTensorMapJson` | `0x16260` | `sg\d+/tensor_map.json` | dict / raw json | CONFIRMED |
| `getTensorMap` | `0x11640` | `tensor_map.json` | parsed I/O map | CONFIRMED |
| `getAliasInfo` | `0x18480` | `tensor_map.json` | `{input_name → output_name}` pairs | CONFIRMED |
| `hasMustAlias` | `0x150c0` | `tensor_map.json` | bool | CONFIRMED |
| `getArchType` | `0x14110` | `kelf-0.json` | arch codename str | CONFIRMED |
| `getCCRankWorldSize` | `0x1ade0` | def.json CC metadata | int (world size) | CONFIRMED |
| `hasCC` | `0x14700` | (CC presence) | bool | CONFIRMED |
| `getNumNc` | `0x13660` | `neff.json` | core count | CONFIRMED |
| `getNumNeuronCores` | `0x11d20` | `neff.json` | core count | CONFIRMED |

### Algorithm

The common getter mechanics, recovered from the import/symbol set (`json`, `loads`, `get`, `items`, `lru_cache`):

```c
// shared shape of getDefJson/getInfoJson/getNeffJson/getTensorMapJson @ their wrappers
function getDefJson(self):                 // 0xfb80
    text = findInNeff(self.neff, "sg\\d+/def.json", self.gzipped)   // lru_cache memoized
    obj  = json.loads(text)                 // nlohmann-produced JSON
    return obj                              // caller indexes obj["var"], obj["dma"], …
```

### Error and Guard Strings

These `__pyx_kp_*` literals are the reader's refusal conditions — each one is a place a reimplementation must reproduce the same rejection:

| String | Raised by | Meaning |
|---|---|---|
| `NEFF does not exist.` | construction | missing `.neff` path |
| `NEFF does not have CC op` | `getCCRankWorldSize` guard (via `hasCC`) | no CollectiveCompute present |
| `1 input aliasing to multiple outputs is not supported` | `getAliasInfo` | `alias_map` must be 1:1 from each input |
| `Empty replica_groups found` / `Empty src_target_pairs found` | `getCCRankWorldSize` | malformed CC metadata |
| `Empty topology found at index {}, possible MPMD neff file …` | `getCCRankWorldSize` | MPMD reject (empty inner topology) |
| `Inconsistent worker count across topologies: {} vs {}. Possible MPMD …` | `getCCRankWorldSize` | MPMD reject (inconsistent world size) |

> **NOTE —** MPMD = multi-program-multiple-data. `getCCRankWorldSize` explicitly *refuses* MPMD NEFFs; the error suggests `--stopAfter=compile` as the workaround. A reimplementer who silently averages or maxes inconsistent topologies would diverge from this reader's contract.

---

## 2. `info.json` — The Header Oracle

### Purpose

`info.json` is the top-level metadata member whose fields seed the **binary** `neff_header`. The producer's `NeffFileWriter::findInfoJson` @ `0x153ca20` locates it as `<writerBaseDir>/info.json`, and `initializeNeffHeader` @ `0x1540a00` reads it field-by-field into the header POD. `getInfoJson` @ `0x100c0` is the reader-side surface. The on-disk POD that this JSON feeds — and the writer that emits both — is the subject of the [NEFF-header / BOM-writer page](neff-header-bom-writer.md) (12.2); here we document only what `info.json` *carries*.

### Schema

| JSON field | Type | Header destination | Confidence |
|---|---|---|---|
| version **major** | uint | `neff_header + 168` | CONFIRMED |
| version **minor** | uint | `neff_header + 476` | CONFIRMED |
| NEFF **filename** | str (≤255) | `neff_header + 220` (`strncpy` bound `0xFF`) | CONFIRMED |
| **engine-present** bool array | array&lt;bool&gt; | `neff_header + 480…` (one byte / engine slot) | CONFIRMED |

The engine-present array is iterated as an `nlohmann` `array<bool>` and stored one byte per engine slot — the per-engine "has instructions" bitmap. Header constants **not** sourced from `info.json` (set by `initializeNeffHeader` directly): `+0 = 2` (NEFF format version), `+8 = 0x400` (1024-byte header region), `+204` a 16-byte RFC-4122 v4 UUID, `+544` CCRankWorldSize, `+552` arch-mode word.

> **GOTCHA —** there is an **override branch** (writer offset `+264 ≠ 0`) that skips `info.json` entirely: it hardcodes filename `"file.neff"`, `major=1, minor=1`, and zeroes the engine array. A reimplementer who assumes `info.json` is always authoritative will mis-handle this path. The literal `info.json` appears as a string only in the *reader* (NeffInfo.so); in libwalrus the path is built by concatenation in `findInfoJson`, so it is not a standalone `.rodata` string there.

---

## 3. `neff.json` — The Subgraph Manifest (v0.5)

### Purpose

`neff.json` is the top-level manifest the runtime walks to find each core's KELF. It is produced by `writeNeffJson` @ `0x152c740` (over the vector of `bir::Module`s) as an `nlohmann` ordered map. `getNeffJson` @ `0xf100` surfaces it; `getNumNc` / `getNumNeuronCores` count its subgraph entries. The schema **version is `0.5`** — the string literal sits in libwalrus `.rodata` at `+594113`, immediately adjacent to the cluster `target_arch` / `graphs` / `revision`, which are this file's top-level structural keys.

### Schema

```json
{
  "revision":    "...",
  "target_arch": "...",
  "0.5":         "...",
  "graphs": {
    "main": { ... },
    "sg_coreV1<i>": {
      "op":          "__kelf",
      "subgraph":    "0",
      "<core-id>":   "<coreIdx>",
      "kelf-<N>.json": "...",
      "size":        <decimal kelf byte size>,
      "tvm_op":      "sg_coreV1<i>"
    }
  }
}
```

| Field | Where | Meaning | Confidence |
|---|---|---|---|
| `revision` / `target_arch` / `0.5` | top-level | revision, arch tag, schema version | CONFIRMED (strings) |
| `graphs` | top-level | the subgraph map (root `"main"` + per-core nodes) | CONFIRMED (string) |
| `sg_coreV1<i>` | per virtual NeuronCore | one node per core, `i = to_string(coreIdx)` | CONFIRMED (`sg_coreV1` string) |
| `op = "__kelf"` | node | kelf-backed subgraph kind | CONFIRMED (`__kelf` string) |
| `subgraph = "0"` | node | subgraph id within the core | STRONG |
| `kelf-<N>.json` | node | reference to the per-core KELF index | CONFIRMED |
| `size` | node | kelf byte size (decimal) | STRONG |
| `tvm_op` | node | tvm op tag = the `sg_coreV1` node name | CONFIRMED (`tvm_op` string) |

> **NOTE —** the `sg_coreV1` nodes reference both the per-core `kelf-<N>.json` *and* the per-core `def.json` (`getArtifactAbsPath + "def.json"`). The on-disk per-engine basenames are TitleCase (`PE.bin`, `Pool.bin`, `Activation.bin`, `SP.bin`, `DVE.bin`) while `def.json` indexes lowercase engine ids — an intentional engine-name duality.

> **QUIRK —** `neff.json` generation is **skipped** when `kelf-0.json` already exists: the packager logs `Skipping neff.json generation since it already exists` and short-circuits (the KELF already encodes the engine layout). A reimplementation that unconditionally emits `neff.json` will not match the reference packager's output set.

The `__kelf` node's internal structure is the subject of the [NEFF/KELF `__kelf` node page](neff-kelf-node.md) (12.7); the `kelf-<N>.json` schema it points at is covered by the [KELF JSON schema page](kelf-json.md) (12.6).

---

## 4. `def.json` — The Per-Core Definition (v0.6)

### Purpose

`def.json` is the per-core definition member: variable/tensor placement, DMA-queue table, collective-comms info, ucode-lib entries, FP8 config, and the feature flags. It is produced by `writeDefJson` @ `0x152a0e0`, path `getArtifactAbsPath(module) + "def.json"`, as an `nlohmann` `std::map`. The **schema version is `0.6`** — string at libwalrus `+594084`, directly adjacent to the top-level key `definition`. `getDefJson` @ `0xfb80` surfaces the whole dict.

### Top-Level Schema

| Key | Value | Confidence |
|---|---|---|
| `definition` | module name | CONFIRMED (string) |
| `0.6` | schema version | CONFIRMED (string @ `+594084`) |
| arch fields | `ArchModel`-derived (`ArchModel + 84` always; `+96` when `archLevel ≤ 49`) | STRONG |
| `var` | the variable / tensor table (§4.1) | CONFIRMED |
| `dma` | the DMA-queue table (§4.2) | CONFIRMED |
| CC info | `replica_groups` / `src_target_pairs` / `#rank_world_size` (§5) | CONFIRMED |
| ucode | `{name, opcode, sub_opcode}` entries | CONFIRMED |
| fp8 | `FP8ConvConfig[0]/[1]` | CONFIRMED |
| `neff_features` | feature-flag string array (§4.3); key is plural `"neff_features"` @ `.rodata 0x1c86984` (see [12.5](neff-feature-flags.md)) | CONFIRMED (string) |

The producer sub-tables run in order; each is a separately-symboled `NeffPackager` method:

| Sub-table | Writer @ | Confidence |
|---|---|---|
| DMA-queue definitions | `writeDMAQueueDefinitions` @ `0x1524d10` | CONFIRMED |
| Variable definitions | `writeVarDefinitions` @ `0x1526530` | CONFIRMED |
| CC info | `writeCCInfo` @ `0x1523af0` | CONFIRMED |
| ucode-lib definitions | `writeUCodeLibDefinitions` @ `0x1522d60` | CONFIRMED |
| FP8 config | `writeFP8Config` @ `0x1521e70` | CONFIRMED |
| NEFF features | `writeNEFFFeatures` @ `0x15294b0` | CONFIRMED |

### 4.1 The `var` Table

The variable table (`writeVarDefinitions`) is the heart of `def.json`. Each entry binds a name to a location-class and placement. The `type` enum and the virtual-var sub-keys are cross-confirmed by `analyze_neff_artifacts.py`, which iterates `data['var']` and branches on `v['type']`.

| Per-var key | Meaning | Confidence |
|---|---|---|
| `var_id` | numeric var id | STRONG |
| `name` | tensor name | CONFIRMED (string) |
| `size` | byte size | CONFIRMED (`v['size']` read in analyze_neff) |
| `file` | backing const `.bin` filename | STRONG |
| `pointer` | placement pointer | CONFIRMED (string) |
| `dge-table` / `DGETable` | DGE descriptor-table reference | CONFIRMED (strings) |
| `CCGrp` | collective-comms group id | CONFIRMED (string) |
| `type` | location-class enum (below) | CONFIRMED |

The `type` location-class enum (string literals in libwalrus, read in analyze_neff):

```text
state-buffer | local | shared | remote | tmp-buf | virtual | input | output | file
```

A `virtual` var additionally carries one of three virtual-memory styles (verbatim from `analyze_neff_artifacts.py`):

```c
// analyze_neff_artifacts.py — virtual-var placement decode
if v['type'] == 'virtual':
    if   'fabric_path'  in v: style = v['fabric_path']    // main / alt  — two areas in virt
    elif 'backing_buf'  in v: style = v['backing_buf']    // main / sharded
    else:                     style = 'main'              // everything in main
    extent = v['backing_variable_off'] + v['size']        // offset into backing area + size
```

| Virtual-var key | Meaning | Confidence |
|---|---|---|
| `fabric_path` | virt area name (`main`/`alt`) | CONFIRMED (analyze_neff + libwalrus string) |
| `backing_buf` | virt area name (`main`/`sharded`) | CONFIRMED |
| `backing_variable_off` | offset into the backing area | CONFIRMED |

Const-file de-dup is gated by `enableConstFileDedup`; the packager logs `Const File de-dup saved <KB> KB`. The table asserts `tensors in Call table must be physical tensors`.

> **CORRECTION (D-S02 §6 / §2) —** the backing report lists the analyze-time `type`-iteration set as `{'tmp-buf','virtual','input','output','file'}`. In the shipped `analyze_neff_artifacts.py` the size-accumulation loop iterates only `['tmp-buf', 'virtual', 'input', 'output']`, and `'file'` is handled in a **separate** branch (`if v['type'] == 'file': self.const_size += v['size']`). The full *emitted* enum (nine classes incl. `state-buffer`/`local`/`shared`/`remote`) is still correct — the correction is only about which subset that one analyzer loop walks.

### 4.2 The DMA-Queue Table

`writeDMAQueueDefinitions` @ `0x1524d10` emits the per-queue table. Queue-name family (string literals): `act_load`, `embedding_update`, `data`, `in`/`out`, `dynamic`, `DynamicDMAScratchLoc_set`; each carries a queue id, semaphore id, and direction. Asserts (verbatim):

```text
one NEFF can only use up to 11x16 number of DMA queues
Pinned weights queue should have no semaphore id
SW DGE must be on GPSIMD engine
```

> **NOTE —** the per-engine DMA-*descriptor* JSONs (`PE.json`, `Pool.json`, `Activation.json`, `SP.json`, `DVE.json`) are adjacent tar members but are **not** part of `def.json` — they are read separately by `analyze_neff_artifacts.py` (`data['dma']` → `desc['from'/'to'/'from_sizes'/'to_sizes'/'from_arr']`, `descArr['queue'/'instance_name']`). They are noted here only so a reimplementer does not conflate the queue *table* in `def.json` with the descriptor *streams*.

### 4.3 The `features` Array and 64-bit Bitmask

`writeNEFFFeatures` @ `0x15294b0` reads `bir::Module::getAttribute()` and emits **two** parallel views: a human-readable `"neff_features"` string array into `def.json` (the key is plural — see [12.5](neff-feature-flags.md)), and a 64-bit feature **bitmask** into the binary header (`neff_header+192` `feature_flags`). The named subset that reaches `def.json` is **11** flags; the header mask carries up to ~21 bits (the full bit-by-bit catalog, ordinals, and the `vnc`/`remote_sem` procedural-bit correction are owned by [12.5 — NEFF Feature Flags](neff-feature-flags.md)). The abbreviated bit↔name map (from decompile):

| Bit | Name | Bit | Name |
|---|---|---|---|
| `0x000002` | `collective_has_offset` | `0x000100` | `…indirect_memcpy_bound_check` |
| `0x000004` | `neff_feature_custom_ops` | `0x000200` | `neff_feature_DMA_desc_higher_dim` |
| `0x000008` | `neff_coalesced_ccops` | `0x000400` | `neff_feature_vnc` (`vnc_nc_count>1`) |
| `0x000010` | `neff_queue_set_instances` | `0x000800` | DVE perf mode (`archLevel>20 & !disable`) |
| `0x000020` | `neff_has_functions` | `0x004000` | `neff_feature_remote_sem` (InstDMABlock scan) |
| `0x000040` | `neff_feature_dynamic_pwp` | `0x2000000` | `neff_feature_large_tensor_support` (ord 0x16); `0x1000000` is the unnamed `archLevel>39` companion (both bump def-schema ver ≥ 2 — see [12.5](neff-feature-flags.md)) |
| `0x000080` | `neff_feature_indirect_memcpy_32b_sem_wait` | | |

> **QUIRK —** the high attribute bits (`0x1000000`/`0x2000000`) set the bitmask bit but carry **no** human-readable name in the `"features"` array — so the string array and the bitmask are not 1:1. A reimplementer cannot reconstruct the full 64-bit mask from the string array alone; the bitmask is authoritative.

---

## 5. `getArchType` and `getCCRankWorldSize` — Derived Accessors

These two getters are the most algorithmically interesting in the roster: one resolves the single target arch, the other reconstructs the collective world size from up to three independent sources.

### `getArchType` — Target Arch from `kelf-0.json`

`getArchType` @ `0x14110` reads `kelf-0.json` (via `getKelf`) and returns the single target codename. The verbatim docstring (NeffInfo.so `.rodata`):

```text
Get the target type from kelf-0.json. Does not support mixed accelerator usage.
```

"Does not support mixed accelerator" means it asserts **one** arch for the whole NEFF. The returned value is a codename from the M14 ArchLevel family:

| ArchLevel | Codename | Generation |
|---|---|---|
| 10 | inferentia (tonga) | Inf1 / gen1 / CoreV1 |
| 20 | sunda | Trn1 / Inf2 / gen2 / CoreV2 |
| 30 | cayman | Trn2 / gen3 / CoreV3 |
| 40 | core_v4 (mariana) | Trn3 / gen4 / CoreV4 |
| 50 | core_v5 | Trn4 / gen5 / CoreV5 |

### `getCCRankWorldSize` — Collective World Size

`getCCRankWorldSize` @ `0x1ade0` computes the world size from CC metadata that `writeCCInfo` @ `0x1523af0` emitted into `def.json`. Verbatim docstring:

```text
Returns the total number of workers/cores needed for CollectiveCompute operation.
This method determines the world size by examining CC metadata:
- replica_groups: 3D structure defining CC topology
     - Outermost dim: Unique CC topologies.
     - Middle: Number of groups per CC topology.
     - Inner: rank size per CC.
- src_target_pairs: Collective permute operations
- #rank_world_size: Explicit world size specification
Returns 1 as minimum to match XLAInferGoldens and BIRSim behavior.
When multiple sources are present, returns the maximum world size found.
```

```c
// __pyx_pw_..._getCCRankWorldSize @ 0x1ade0  (uses internal genexpr generators @ 0x10cd0 / 0x11190)
function getCCRankWorldSize(self):
    if not self.hasCC():                          // guard → "NEFF does not have CC op"
        raise CCError
    cc = self.getDefJson()[<cc keys>]

    // (a) replica_groups: 3D list [topology][group][rank]
    ws_rg = 0
    for topo_idx, topo in enumerate(cc["replica_groups"]):
        if topo is empty:                         // "Empty topology found at index {}"  (MPMD reject)
            raise MPMDError
        rank_size = max(len(group) for group in topo)      // inner = rank size
        if ws_rg and rank_size != ws_rg:          // "Inconsistent worker count …"  (MPMD reject)
            raise MPMDError
        ws_rg = rank_size

    // (b) src_target_pairs: collective-permute span
    ws_pairs = derive_from(cc["src_target_pairs"])         // "Empty src_target_pairs found" guard

    // (c) explicit override
    ws_expl  = cc.get("#rank_world_size", 0)               // literal key "#rank_world_size"

    return max(ws_rg, ws_pairs, ws_expl, 1)                // 1 = floor (XLAInferGoldens / BIRSim)
```

The CC metadata keys (all `__pyx_n_s_*` / `__pyx_kp_u_*` constants in NeffInfo.so): `replica_groups`, `replica_groups_list`, `src_target_pairs`, `#rank_world_size`, `topology`, `topology_idx`, `first_topology`, `num_groups`, `world_size`, `pairs_world_size`, `explicit_world_size`, `total_workers`, `current_total_workers`, `num_rank_hint`, `num_tpb`. This world-size value also feeds the runtime header at `neff_header + 544` (CCRankWorldSize). The collective `replica_groups` / `src_target_pairs` structure is revisited from the collectives angle in Part 13.

---

## 6. `tensor_map.json` — The I/O Binding Surface

### Purpose

`tensor_map.json` is the wire-level binding surface: it maps each NEFF input/output tensor name to its placement and records the input↔output alias relation. It is produced **per-core by the bir_linker/codegen**, not by the packager (the packager only collects it via `getArtifactAbsPath + "tensor_map.json"`). It is threaded by reference through later passes — dead-code elimination prunes deleted-MemoryLocation names from it; memreserve loads it; both gated by `bir::needNoTensorMap()`. Reader surfaces: `getTensorMap` @ `0x11640` (parsed), `getTensorMapJson` @ `0x16260` (raw), `getAliasInfo` @ `0x18480`, `hasMustAlias` @ `0x150c0`.

### Schema

| Key | Where | Meaning | Confidence |
|---|---|---|---|
| `inputs` | top-level | list of input tensor descriptors | CONFIRMED (`__pyx_n_s_inputs`/`__pyx_n_u_inputs`) |
| `outputs` | top-level | list of output tensor descriptors | INFERRED (see CORRECTION) |
| `alias_map` | top-level | input↔output aliasing table | CONFIRMED (`__pyx_n_s_alias_map`) |
| `must_alias` | alias entry / flag | must-alias (in-place) vs may-alias | CONFIRMED (`__pyx_n_u_must_alias`) |
| `input_name` | alias entry | the donated input tensor name | CONFIRMED |
| `output_name` | alias entry | the aliased output tensor name | CONFIRMED |
| `name` | per-tensor | tensor name field | CONFIRMED (`__pyx_n_s_name`) |

> **CORRECTION (D-S02 §2) —** the backing report lists `"outputs"` as a CONFIRMED top-level key. In NeffInfo.so cp310 there **is** a string constant for `inputs` (`__pyx_n_s_inputs` / `__pyx_n_u_inputs`) but **no** standalone `outputs` constant — only `output_name` (the alias sub-key) exists. So the reader is observed to iterate `inputs` and `alias_map`; the `outputs` top-level list is inferred from the producer's intent (and from the `1 input aliasing to multiple outputs …` assertion, which names "outputs" only in prose), not anchored to a reader string. Tagged INFERRED above.

### Per-Tensor Descriptor

The per-tensor binding tuple — `name → {shape, dtype, DRAM-address, engine/queue}` — is the documented intent of `getTensorMap`. The placement values originate from `bir::MemoryLocation` (MemoryType → `{DRAM8, SB16, PSUM32}`, MemoryAddressSpace, Dtype). `getTensorMap` indexes `inputs` (and outputs) entries by `name` then reads the placement sub-keys; the exact JSON key spelling for `shape`/`dtype`/`address` is produced by the linker and is **not** re-stated as literals inside NeffInfo — the reader simply traverses them. Tagged STRONG/INFERRED for the sub-key spelling.

### Alias Relation

```c
// getAliasInfo @ 0x18480
function getAliasInfo(self):
    tmap = self.getTensorMap()
    pairs = {}
    for entry in tmap["alias_map"]:
        in_name  = entry["input_name"]
        out_name = entry["output_name"]
        if in_name in pairs:
            raise "1 input aliasing to multiple outputs is not supported"   // map must be 1:1
        pairs[in_name] = out_name
    return pairs

// hasMustAlias @ 0x150c0 → true iff any entry has must_alias set
```

The alias relation originates as `xla::HloInputOutputAliasConfig` (output_index → `{param#, param_index, kind}`; AliasKind `0=kMayAlias`, `1=kMustAlias`), surfaces as MHLO `mhlo.ArgResultAliasAttr.mustAlias` (`"may_alias"`/`"must_alias"`), and is lowered into `tensor_map.json`'s `alias_map` + `must_alias`. When `hasMustAlias` is true the runtime **must** place the output in the donated input's buffer (in-place) — a hard constraint, not a hint.

---

## 7. Producer → Consumer Arc

```text
bir_linker (setupLinkDir → sgLnk tree)  +  codegen (per-engine .bin, PE.json…, kelf-N.json, tensor_map.json)
        │
        ▼
NeffPackager  (libwalrus.so)
   writeNeffJson  0x152c740 → neff.json (v0.5)
   writeDefJson   0x152a0e0 → def.json (v0.6) { var, dma, cc, ucode, fp8, features }
   collects kelf-*/tensor_map/info  + global_metric_store.json / icMetadata.json
        │
        ▼
NeffFileWriter  (libwalrus.so)
   findInfoJson         0x153ca20 → reads info.json
   initializeNeffHeader 0x1540a00 → seeds binary neff_header
   writeArchiveFile               → libarchive PAX tar (+gzip);  MD5 "IR signature"
        │
        ▼  file.neff
        ▼
NeffInfo  (kra, Cython)   parseFile → findInNeff (dd|tar) → json.loads →
   getDefJson / getInfoJson / getNeffJson / getKelf / getTensorMap[Json] /
   getAliasInfo / getArchType / getCCRankWorldSize / hasCC / getNumNeuronCores
   analyze_neff_artifacts.py reads def.json["var"] + per-engine *.json + *.bin
```

> **CORRECTION (D-J34 §5) —** the NEFF integrity hash is **MD5** ("IR signature"), not SHA-1.

---

## Related Components

| Name | Relationship |
|---|---|
| [NEFF header / BOM writer](neff-header-bom-writer.md) (12.2) | `info.json` is the POD source for the binary `neff_header` |
| [KELF JSON schema](kelf-json.md) (12.6) | `kelf-<N>.json` referenced by every `neff.json` `__kelf` node; `getArchType` reads `kelf-0.json` |
| [NEFF/KELF `__kelf` node](neff-kelf-node.md) (12.7) | the internal structure of the `neff.json` `op:"__kelf"` subgraph node |
| Part 13 — CCInfo / replica_groups (planned) | the collective angle on `def.json`'s `replica_groups` / `src_target_pairs` |

## Cross-References

- [NEFF header + BOM writer](neff-header-bom-writer.md) (12.2) — the `info.json`-fed binary `neff_header` POD and the BOM/MD5-signature writer.
- [KELF JSON schema](kelf-json.md) (12.6) — the `kelf-<N>.json` member every `__kelf` node names; source of `getArchType`'s `target`.
- [NEFF/KELF `__kelf` node](neff-kelf-node.md) (12.7) — the `neff.json` `op:"__kelf"` subgraph node and its `attrs.kelf` bridge.

*(Part 13's CCInfo / `replica_groups` deep-dive is planned and intentionally unlinked until published.)*
