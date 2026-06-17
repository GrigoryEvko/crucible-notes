# NEFF Metadata Schema (simdjson-DOM Consumed Keys)

> *All addresses, offsets, sizes, and key strings on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA. The metadata parser is statically-linked **simdjson 0.9.0** (binary string `"SIMDJSON_VERSION 0.9.0"`); there is **no** protobuf in the NEFF metadata path. Source TUs: `/opt/workspace/KaenaRuntime/{kelf/neff.cpp, kelf/kelf.cpp, kelf/kelf2kbin.cpp}`. Other versions will differ.*
> *Evidence grade: **Confirmed (string- and callsite-anchored)** — every consumed key is a `.rodata` literal at the cited address; every parse function and simdjson DOM primitive is an `nm`/IDA-confirmed symbol; LE-ASCII string-compare constants are decoded byte-exact. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

The NEFF metadata — the model description the loader turns into compiled per-engine programs — is **JSON**, parsed by the statically-linked **simdjson 0.9.0 DOM** parser. It is *not* protobuf, not FlatBuffers, and not a custom binary table. The loader reaches the JSON through the container's member table ([NEFF Container](container.md)): four manifest tiers, parsed in order, each naming the next by a string value rather than an index — `neff.json` (a TVM-relay graph) → the `__kelf` descriptor (legacy `kelf-a.json`) → per-subgraph `def.json` → raw `<eng>.bin` / `.npy` payloads. Every manifest is opened by `simdjson::dom::parser::parse_into_document` and walked field-by-field with `at_key` / `operator[]` plus the typed getters `get_string` / `get_uint64` / `get_int64` / `get_double` / `get_array`. (Abseil/protobuf symbols *do* exist binary-wide — the `"Illegal jstype for int64…"` descriptor strings, for example — but they belong to an unrelated embedded telemetry component; the NEFF parsers call **only** `simdjson::dom::*`.)

The single fact a reimplementer must internalize is that **the loader reads a strict subset of each manifest's keys** — the *consumed* key set, not the producer's full schema. A manifest field the runtime never looks up is invisible to `libnrt.so` and out of scope here. This page enumerates, tier by tier, exactly which keys the loader's `at_key` calls reference, the type each is read as, the loader struct/field each populates, the parse callsite that reads it, and the wire-meaning of the discriminator string values — decoded from the LE-ASCII integer compares the binary actually performs (`"in"` = `0x6e69`, `"dyna"`+`"mic"` for the `dynamic_dma` path, `"evtaccel"` = `0x6c65636361747665`), not guessed from field names. The producer-side schema (what `neuronx-cc` emits, including keys the runtime ignores) is canonically owned by the compiler project and is **not** re-derived here.

A structural subtlety the page pins down: the two JSON tiers use two *different* simdjson lookup primitives. `parse_neff_json` (the TVM-relay tier) dispatches through a local `operator[](const char*)` clone (`@0xe49b0`); the `def.json` tier (`parse_one_variable`, `parse_one_dma_ring`, …) dispatches through the generic `at_key(string_view)` (`@0x49ced0`) and the typed getters. Both are the same simdjson DOM; the clone is a constprop specialization, not a separate parser. The keys are read, the loader structs are populated, and the metadata never touches device memory — the whole parse runs in stages S0–S3, `IOCTL`-free ([Load Pipeline](load-pipeline.md)).

For reimplementation, the contract is:

- **The four-tier manifest chain** and its discriminators — `neff.json`'s `attrs.func_name=="__kelf"` node, that node's `attrs.kelf` string naming the KELF descriptor, the KELF `graphs[].definition` paths naming each `def.json`, and the `def.json` payload keys naming `<eng>.bin` / `.npy` blobs.
- **The consumed-key set per tier** — every key the loader's `at_key`/`operator[]` calls reference, with its JSON type, its destination loader field, and its parse callsite — and the explicit boundary that producer-only keys are out of scope.
- **The discriminator-string → enum maps**, decoded from the binary's LE-ASCII compares: dtype string → `nrt_dtype_t`; `target` → `al_hal_tpb_arch_type_t`; var `type` → `kbin_mr_type_t`; dma_queue `owner` → `kbin_dma_ring_type`; desc `op` → `kbin_dma_desc_op`; carveout `type`=`"evtaccel"`.
- **The parse-and-dispatch flow** — open DOM → look up keys → populate `io_node_info_t` (tier 1) and `mla_resources` (tier 3) — as annotated C pseudocode citing symbol+addr.

| | |
|---|---|
| **Parser** | simdjson **0.9.0** DOM (`parse_into_document` `@0xe4b50`); 3 CPU tiers (haswell/westmere/fallback) |
| **Tier-1 parser** | `parse_neff_json` `@0xe1d10` → `io_node_info_t` (144 B) input/output maps |
| **Tier-2 parser** | `kelf::kelf::load` `@0x497dc0` (version/target/graphs) |
| **Tier-3 parser** | `kelf_load_from_neff` `@0x4c0870` (def.json top-level demux) → `mla_resources` (440 B) |
| **Tier-1 field lookup** | `element::operator[](const char*)` `@0xe49b0` (constprop clone) |
| **Tier-3 field lookup** | `element::at_key(string_view)` `@0x49ced0` + typed getters (`@0x4c5660`…) |
| **Typed getters** | `get_string` `@0x4c5660` · `get_uint64` `@0x4c56c0` · `get_int64` `@0x4c5730` · `get_double` `@0x4c57a0` · `get_array` `@0x4c5860` |
| **KELF discriminator** | `attrs.func_name == "__kelf"` (`.rodata @0x83fd8d`) |
| **Format** | JSON, **not** protobuf — every NEFF parser calls only `simdjson::dom::*` |

---

## 1. The Four-Tier Manifest Chain

### Purpose

The loader does not parse "the NEFF metadata" as one document. It parses **four manifest tiers**, each a separate JSON (or binary) member of the container, each naming the next by a *string value* embedded in its own DOM — never by a member index or a table offset. A reimplementer must walk the chain in order, because every tier hands the next its file name. The chain is the spine; the per-tier key tables (§3–§7) are the ribs.

### The chain

```text
TIER 1  neff.json            parse_neff_json @0xe1d10
        TVM-relay graph: I/O tensor list (dtype/shape/size per EID) +
        the node whose attrs.func_name=="__kelf" names the KELF member.
          │  attrs.kelf (string)  ──────────────────────────────────┐
          ▼                                                          │
TIER 2  <kelf member>         kelf::kelf::load @0x497dc0   ◀─────────┘
        (legacy name kelf-a.json) version / target / graphs[].
          │  graphs[].definition (string per subgraph)  ────────────┐
          ▼                                                          │
TIER 3  sg<N>/def.json        kelf_load_from_neff @0x4c0870 ◀────────┘
        kbin-ingest: var / dma_queue / dma / replica_groups /
        src_target_pairs / ucode_lib / sb_reservation / fp8 / num_streams.
          │  "instr" / "file_name" (string payload names)  ─────────┐
          ▼                                                          │
TIER 4  <eng>.bin, *.npy      load_bin_file @0x4ae500 / numpy_load @0x4cb810
        Raw headerless byte blobs (instruction streams, weights). Not JSON.
```

### The two DOM lookup primitives

> **NOTE —** the tiers split on *which* simdjson lookup primitive they use, and a reimplementer cross-reading the disassembly will see two different field-lookup functions. `parse_neff_json` (tier 1) reads keys through `simdjson::dom::element::operator[](const char*)` (`@0xe49b0`, a constprop clone local to the TVM-relay parse), after `simdjson::dom::parser::parse_into_document` (`@0xe4b50`). The `def.json` tier (tier 3 — `parse_one_variable`, `parse_one_dma_ring`, `parse_one_dma_block`, …) reads keys through the generic `simdjson::dom::element::at_key(string_view)` (`@0x49ced0`) plus the typed getters `get_string` (`@0x4c5660`) / `get_uint64` (`@0x4c56c0`) / `get_int64` (`@0x4c5730`) / `get_double` (`@0x4c57a0`) / `get_array` (`@0x4c5860`). Both are the same simdjson 0.9.0 DOM and produce identical semantics — `operator[]` is the throwing wrapper over `at_key`. The split is an artifact of which TU was compiled with the clone; it is **not** two parsers.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `simdjson::dom::parser::parse_into_document` | `0xe4b50` | parse a member buffer into a DOM document | HIGH |
| `parse_neff_json` | `0xe1d10` | tier-1 TVM-relay graph parse → IO maps + `__kelf` node | HIGH |
| `kelf::kelf::load` | `0x497dc0` | tier-2 KELF descriptor: version/target/graphs[] | HIGH |
| `kelf_load_from_neff` | `0x4c0870` | tier-3 def.json top-level demux → `mla_resources` | HIGH |
| `json_parse_load_elements` | `0x4af3e0` | open+`parse_into_document` a sub-json member (tier 3) | HIGH |
| `element::operator[](const char*)` | `0xe49b0` | tier-1 field lookup (constprop clone) | HIGH |
| `element::at_key(string_view)` | `0x49ced0` | tier-3 field lookup | HIGH |
| `load_bin_file` / `numpy_load` | `0x4ae500` / `0x4cb810` | tier-4 raw `.bin` / `.npy` payload fetch | HIGH |

---

## 2. Reading the Consumed-Key Tables

The §3–§7 tables list, per tier, **only the keys the loader's `at_key`/`operator[]` calls reference** — the consumed set. Columns:

- **json key** — the `.rodata` literal the loader passes to `at_key`/`operator[]`.
- **type** — the JSON type the loader reads it as (the typed getter it calls).
- **loader use / dest** — the loader struct field or behavior the value drives.
- **callsite** — the parse function (addr) whose DOM walk reads the key.
- **Conf.** — confidence; `HIGH` = key string + getter both code-confirmed, `MED` = branch present but not line-walked.

> **GOTCHA —** absence from these tables means "the runtime does not read it", **not** "the producer does not emit it". `neuronx-cc` emits a richer schema; keys it writes that no `at_key` call references are invisible to `libnrt.so` and deliberately omitted. For the canonical producer schema, see the [neuronx-cc companion wiki](../../neuronx-cc/wiki/) — do not infer producer keys from this consumer list.

---

## 3. Tier 1 — `neff.json` (TVM-Relay Graph)

### Purpose

`neff.json` is a TVM-relay graph, inherited from the compiler's IR — `nodes` / `arg_nodes` / `heads` / `node_row_ptr` / `attrs`, exactly the TVM graph-runtime manifest shape. The loader reads it for two things: the **I/O tensor descriptors** (per input/output EID: dtype, shape, byte size) and the **`__kelf` discriminator** that names the tier-2 KELF member. The per-tensor record is `io_node_info_t` (144 B), keyed by tensor name into two `std::map`s (the inputs map and the outputs map). The dtype/shape source-of-truth is the flat `attrs.dltype` / `attrs.shape` arrays, indexed by EID via `node_row_ptr`.

### Consumed keys

| json key | type | loader use / dest | callsite | Conf. |
|---|---|---|---|---|
| `nodes` | array | graph nodes; per node reads `name`, `attrs` | `parse_neff_json` `@0xe1d10` | HIGH |
| `arg_nodes` | array\<uint\> | indices into `nodes[]` that are graph **inputs** (entry-var EIDs) | `parse_neff_json` | HIGH |
| `heads` | array | graph **outputs**; each `[node_id, out_idx, …]` TVM head tuple (`node_id` indexed into `nodes[]`, bounds-checked) | `parse_neff_json` | HIGH |
| `node_row_ptr` | array\<uint\> | EID row-pointer: node index → EID base, to index the flat `dltype`/`shape` tables | `parse_neff_json` | HIGH |
| `attrs` | object | graph attrs (dtype/shape tables + the `kelf` marker) | `parse_neff_json` | HIGH |
| `name` | string | per-node tensor/op name (the IO-map key) | `parse_neff_json` | HIGH |
| `attrs.dltype` | array | TVM `[storage_tag, array<string>]`: per-EID dtype **strings**; must be a JSON array (`'['`/`0x5B` validated) | `parse_neff_json` | HIGH |
| `attrs.shape` | array | TVM `[storage_tag, array<array<int>>]`: per-EID shape arrays | `parse_neff_json` | HIGH |
| `attrs.kelf` | string | on the KELF node only: the literal `"__kelf"` (`.rodata @0x83fd8d`) — marks the subgraph-carrying node | `parse_neff_json` | HIGH |
| `attrs.func_name` | string | on the KELF node: names the tier-2 KELF member, returned via `char** kelf_name_out` (`"Found KELF node: %s"`) | `parse_neff_json` | HIGH |
| `attrs.output_names` | array\<string\> | output tensor names → the output `io_node_info_t` map | `parse_neff_json` | HIGH |

`.rodata` anchors: `arg_nodes` `@0x83fd24` · `node_row_ptr` `@0x83fd34` · `dltype` `@0x83fd47` · `func_name` `@0x83fd83` · `__kelf` `@0x83fd8d` · `output_names` `@0x83fdc2`.

### The `io_node_info_t` record (144 B, ordinal 2948)

`extract_tensor_info` (`@0xe1710`, inlines `str_to_dtype` + `calc_tensor_size`) fills one record per tensor name:

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `size` | `+0x00` | `size_t` | `product(shape) × dtype_size`; overflow-checked (`calc_tensor_size`) |
| `dtype` | `+0x08` | `nrt_dtype_t` (4 B) | element dtype (§8 map) |
| `shape` | `+0x0C` | `uint32[32]` | up to 32 dims (`"Too many dimensions (%lu)"`) |
| `ndim` | `+0x8C` | `uint32` | valid dim count |

### Algorithm

```c
// parse_neff_json @0xe1d10 — TVM-relay graph -> IO maps + __kelf member name.
// a3 = inputs map<string,io_node_info_t>, a4 = outputs map.  Field lookup via operator[] @0xe49b0.
function parse_neff_json(json_str, out kelf_name, out inputs, out outputs):
    parser.parse_into_document(doc, json_str.data, json_str.size)   // 0xe4b50
    root = doc.root()

    nodes        = root["nodes"]                  // 0xe49b0 operator[] (the at_key wrapper)
    arg_nodes    = root["arg_nodes"]
    heads        = root["heads"]
    node_row_ptr = root["node_row_ptr"]
    attrs        = root["attrs"]
    if any missing: reject "Missing required fields in NEFF JSON"

    dltype = attrs["dltype"]                       // must be a JSON array: HIBYTE=='[' (0x5B)
    shape  = attrs["shape"]                         //   "Invalid dltype data format" / "Invalid shape data format"
    if !dltype || !shape: reject "Missing dltype or shape in attrs"

    // inputs: each arg_nodes[i] -> nodes[idx]; EID via node_row_ptr
    for each idx in arg_nodes:
        eid  = node_row_ptr[idx]                   // bounds: "EID %u out of bounds for dtypes/shapes"
        name = nodes[idx]["name"]
        extract_tensor_info(eid, name, dltype, shape, &info)   // 0xe1710 (str_to_dtype + calc_tensor_size inlined)
        inputs[name] = info

    // outputs: heads tuples (node_id bounds-checked) + attrs.output_names
    for each [node_id, out_idx, …] in heads:
        if node_id >= nodes.size(): reject "Output node ID %lu out of bounds"
        name = nodes[node_id]["name"]              // cross-indexed with attrs["output_names"]
        extract_tensor_info(out_eid, name, dltype, shape, &info)
        outputs[name] = info

    // KELF discriminator: the node whose attrs.func_name == "__kelf"
    for each node in nodes:
        if node["attrs"]["func_name"] == "__kelf":           // .rodata 0x83fd8d
            k = node["attrs"]["kelf"]                          // names the tier-2 member
            if k is missing:  reject "KELF node found but missing 'kelf' attribute"
            if k not a string: reject "KELF attribute is not a string"
            *kelf_name = strdup(k); log "Found KELF node: %s"
            break
    if no KELF node: reject "No KELF node found in NEFF JSON"
```

> **QUIRK —** the KELF discriminator is a *graph-node attribute*, not a top-level field. The executable is wherever the node with `attrs.func_name == "__kelf"` points its `attrs.kelf` string — there is no top-level `"kelf"` key. A reimplementer that scans the top-level object for a `kelf` member will miss it; the marker is one level deep, inside the `attrs` of one specific node. (HIGH — `__kelf` literal `@0x83fd8d`; the discriminator loop is the only consumer of `func_name`.)

---

## 4. Tier 2 — the KELF Descriptor (`kelf-a.json`)

### Purpose

The tier-2 member (named by tier-1's `attrs.kelf`; legacy file name `kelf-a.json`) is the subgraph index: a `version`, a compile `target` arch, and a `graphs[]` array each pointing at a `def.json` path. `kelf::kelf::load` (`@0x497dc0`) reads all three; the `target` gates whether this runtime can run the NEFF on the present hardware.

### Consumed keys

| json key | type | loader use / dest | callsite | Conf. |
|---|---|---|---|---|
| `version` | string | `"MAJOR.MINOR-suffix"`; `parse_version` `@0x4970f0` → `{major,minor,suffix}` (`"KELF version: %u.%u-%s"`) | `kelf::kelf::load` `@0x497dc0` | HIGH |
| `target` | string | compile-target arch; `parse_target` `@0x497c10` → `al_hal_tpb_arch_type_t` (§8) | `kelf::kelf::load` | HIGH |
| `graphs` | array | subgraph list; `load_graphs` `@0x4978b0` → `load_graph` `@0x497640` each (`"mlaop dir %s, has %ld graphs"`) | `kelf::kelf::load` | HIGH |
| `graphs[].name` | string | subgraph name | `kelf::kelf::load_graph` `@0x497640` | HIGH |
| `graphs[].definition` | string | path to the `sg<N>/def.json` (the tier-3 member) | `kelf::kelf::load_graph` | HIGH |

`.rodata` anchors: `sunda` `@0x848978` · `cayman` `@0x84897e` · `mariana` `@0x848985` · `definition` `@0x848a2d`.

> **GOTCHA —** a `target` mismatch is **not** silent. `parse_target` maps the string to an arch id (§8) and gates it against the running hardware; a mismatch rejects the load with `"Loading a NEFF compiled for a different arch…"` and the `NEURON_RT_ALLOW_LEGACY_NEFF=1` escape hint. `nlog_set_error_cause(NEFF_ARCH_INCOMPAT)` is the **only** error cause this path sets. The wildcard `"*"` defers to `al_hal_tpb_get_arch_type()` (match the present hardware). A reimplementer must carry the same arch gate or it will accept a NEFF whose ISA the device cannot execute.

---

## 5. Tier 3 — `def.json` (KBIN-Ingest, the Bulk Schema)

### Purpose

`def.json` is the per-subgraph kbin-ingest manifest — the bulk of the consumed schema. `kelf_load_from_neff` (`@0x4c0870`) is the top-level demux: it `at_key`-dispatches on each top-level key and routes to a `parse_one_*` handler (several of which — `parse_sb_carveouts`, `parse_fp8_conversion_config` — are *inlined* into it, having no standalone symbol). Each handler populates one region of `mla_resources` (440 B), the parser working set later lowered to `kbin` by `gen_kbin` ([kelf2kbin](kelf2kbin.md)).

### Top-level consumed keys (dispatch order)

| json key | type | loader use / dest (`mla_resources` region) | callsite | Conf. |
|---|---|---|---|---|
| `var` | array\<obj\> | **required** (`"…no Outputs"` if absent); each → `parse_one_variable` → `mem_ref` (the tensor schema, §6) | `parse_one_variable` `@0x4b36b0` | HIGH |
| `check_var_ids` | flag | sanity gate (`"Unexpected var_ids set! max=%u, size=%lu"`) | `kelf_load_from_neff` `@0x4c0870` | HIGH |
| `dma_queue` | array\<obj\> | each → `parse_one_dma_ring` → DMA-ring schema (`mla_resources.DI`, §6) | `parse_one_dma_ring` `@0x4b5f80` | HIGH |
| `replica_groups` | array | collectives replica groups → `parse_replica_groups` (`mla_resources.RGI`) | `parse_replica_groups` `@0x4b24b0` | HIGH |
| `src_target_pairs` | array | collectives src/target pairs → `parse_src_target_pairs` (`mla_resources.SRCTGTPI`) | `parse_src_target_pairs` `@0x4b27f0` | HIGH |
| `ext_isa_ucode_lib_def` | object | default Extended-ISA (Q7/GPSIMD) lib def; sub-keys `functions`, `add_lib` (max 18 ops) | `kelf_load_from_neff` | HIGH |
| `ucode_lib` | array\<obj\> | GPSIMD ucode libs → `parse_one_ucode_lib` (`mla_resources.UCODE_LIBS`, §6) | `parse_one_ucode_lib` `@0x4b1610` | HIGH |
| `runtime_statebuffer_reservation` | array\<obj\> | SB carveouts → `parse_sb_carveouts` (inlined; `mla_resources.SB_CARVE`, §7) | `kelf_load_from_neff` | HIGH |
| `num_streams` | uint | CC stream count → `mla_resources.CCSTMI` | `kelf_load_from_neff` | HIGH |
| `dma` | array\<obj\> | DMA descriptor **blocks** → `parse_one_dma_block` (§6) | `parse_one_dma_block` `@0x4bc620` | HIGH |
| `fp8_conversion_config` | object | FP8 cast config → `parse_fp8_conversion_config` (inlined; `mla_resources.FP8_CONV_CFG`, §7) | `kelf_load_from_neff` | HIGH |

`.rodata` anchors: `dma_queue` `@0x849631` · `num_streams` `@0x8496c5` · `ext_isa_ucode_lib_def` `@0x84963b`.

> **NOTE —** `runtime_statebuffer_reservation` is **conditionally required**. Absent on a `cayman` (v3) target without `NEURON_RT_ALLOW_LEGACY_NEFF=1`, `kelf_load_from_neff` FATALs (`"Missing or corrupted runtime_statebuffer_reservation field…"`). On other targets it is optional. A reimplementer must couple this key's presence requirement to the tier-2 `target` value, not treat it as universally optional.

### Algorithm — the def.json demux

```c
// kelf_load_from_neff @0x4c0870 — def.json top-level dispatch. Field lookup via at_key @0x49ced0.
// Opens the member with json_parse_load_elements @0x4af3e0 (parse_into_document).
function kelf_load_from_neff(neff, sg_dir, def_json_name, out mla_res):
    json_parse_load_elements(neff, sg_dir, def_json_name, parser, root)   // 0x4af3e0

    if e = root.at_key("var"):                  // REQUIRED
        for each v in e.get_array():            // 0x4c5860
            parse_one_variable(neff, mla_res, sg_dir, name(v), v)   // 0x4b36b0  -> mem_ref (§6)
    else: reject  // subgraph has no outputs

    if e = root.at_key("check_var_ids"): gate_var_ids(mla_res, e)
    if e = root.at_key("dma_queue"):
        for q in e.get_array(): parse_one_dma_ring(mla_res, qname(q), q)     // 0x4b5f80
    if e = root.at_key("replica_groups"):     parse_replica_groups(mla_res, e)    // 0x4b24b0
    if e = root.at_key("src_target_pairs"):   parse_src_target_pairs(mla_res, e)  // 0x4b27f0
    if e = root.at_key("ext_isa_ucode_lib_def"): parse_ext_isa_lib(mla_res, e)    // inlined; max 18 ops
    if e = root.at_key("ucode_lib"):
        for u in e.get_array(): parse_one_ucode_lib(neff, mla_res, sg_dir, u)     // 0x4b1610
    if e = root.at_key("runtime_statebuffer_reservation"):
        parse_sb_carveouts(mla_res, e)        // inlined; type=="evtaccel" only (§7)
    else if target==cayman && !ALLOW_LEGACY_NEFF: FATAL  // "Missing or corrupted runtime_statebuffer_reservation…"
    if e = root.at_key("num_streams"):  mla_res.CCSTMI = e.get_uint64()           // 0x4c56c0
    if e = root.at_key("dma"):
        for b in e.get_array(): parse_one_dma_block(mla_res, b)                   // 0x4bc620 -> parse_one_desc_ap
    if e = root.at_key("fp8_conversion_config"): parse_fp8_conversion_config(mla_res, e)   // inlined (§7)

    // per-engine instruction sets (pe/act/dve/sp/pool[,q7]) handled by parse_one_engine_instr @0x4b7e30
    // -> "instr" string -> load_bin_file @0x4ae500 (tier 4)
```

### `mla_resources` (440 B, ordinal 2763) — the def.json → struct map

The top-level keys map 1:1 onto `mla_resources` regions (the parser working set; offsets verbatim from `structures.json`):

| Region | Offset | Fed by def.json key |
|---|---|---|
| `DI` dma_info | `+0` | `dma_queue` |
| `MI` mem_ref_info | `+48` | `var` |
| `INS` instructions | `+192` | per-engine `instr` |
| `RGI` replica_group_info | `+280` | `replica_groups` |
| `SRCTGTPI` | `+304` | `src_target_pairs` |
| `UCODE_LIBS` | `+336` | `ucode_lib` / `ext_isa_ucode_lib_def` |
| `SB_CARVE` sb_carveouts | `+384` | `runtime_statebuffer_reservation` |
| `CCSTMI` cc_streams_info | `+408` | `num_streams` |
| `FP8_CONV_CFG` | `+412` | `fp8_conversion_config` |
| `num_inputs` / `num_outputs` | `+432` / `+436` | `var` type `input`/`output` counts |

---

## 6. Tier 3 — Per-Entry Sub-Schemas

The array entries under `var` / `dma_queue` / `dma` / `ucode_lib` each have their own consumed-key set. The discriminator-string values (var `type`, dma_queue `owner`, desc `op`) are decoded from the LE-ASCII integer compares the binary performs, then mapped to the wire enum.

### `var` entry — `parse_one_variable` `@0x4b36b0` (the tensor schema)

| json key | type | loader use / dest | Conf. |
|---|---|---|---|
| `type` | string | **required**; selects the `mem_ref` subtype (table below); `"Failed to parse, key: type"` | HIGH |
| `var_id` | uint64 | **required**; unique variable id; `"missing <var_id>"` | HIGH |
| `alignment` | uint | optional; must be power-of-2 (`"alignment provided not power of 2. alignment=%u"`) | HIGH |
| `dtype` | string | optional; element dtype (kbin_dtype; FP8/FP4 set) | HIGH |
| `shape` | array | optional; dim list (`'['` validated) | HIGH |
| `size` | uint | required for `input`/`output`/`tmp-buf`/`virtual`/`pointer` (`==8`) | HIGH |
| `backing_variable_off` / `backing_buf` | uint / string | `virtual` only (`.rodata @0x8490a8`) | HIGH |
| `referenced_var_id` | uint | `pointer` only (`.rodata @0x8490dd`) | HIGH |
| `list` | array\<uint\> | `list` only (`"Failed to parse, key: list"`) | HIGH |
| `file_name` | string | `file` only → `.npy`→`numpy_load`, else `load_weight_bin` | HIGH |
| `prefix` / `pipe` | string / int | device-print extras (only `file`/`tmp-buf`/`virtual`) | HIGH |

The `type` string → `kbin_mr_type` map (the discriminator; full mem_ref tree owned by [memory-planning](memory-planning.md)):

| `type` value (`.rodata`) | → `kbin_mr_type` | extra keys |
|---|---|---|
| `state-buffer` (`@0x84906a`) | `MR_SB(1)` | — (SB-resident) |
| `input` | `MR_INPUT(5)` | `size`; `++num_inputs` |
| `output` | `MR_OUTPUT(6)` | `size`; `++num_outputs` |
| `tmp-buf` (`@0x849055`) | `MR_TMP_BUF(4)` | `size` |
| `virtual` | `MR_VIRTUAL_TMP_BUF(8)` | `size`, `backing_variable_off`, `backing_buf` |
| `pointer` | `MR_PTR(7)` | `size`(==8), `referenced_var_id` |
| `list` | `MR_LIST(9)` | `list` |
| `file` | weight/const loader | `file_name` → `numpy_load`/`load_weight_bin` |
| `remote`/`shared`/`sharded`/`main`/`dge-table` | `MR_REMOTE(11)`/etc. | branch present, per-key set not line-walked (MED) |

### `dma_queue` entry — `parse_one_dma_ring` `@0x4b5f80` (DMA ring schema)

| json key | type | loader use / dest | Conf. |
|---|---|---|---|
| `owner` | string | **required**; queue role → `kbin_dma_ring_type` (table below); `"Invalid owner '%s' for DMA queue %s"` | HIGH |
| `num_queues` | uint | queue-set count; not allowed for `in`/`out`/`embedding_update` owners | HIGH |
| `queue_instances` | array | per-instance ring instances (`@0x849139`) | HIGH |
| `semaphore` / `semaphore_set` | uint / array | completion semaphore(s) (`@0x849149`) | HIGH |
| `pinned` | flag | pinned ring (`@0x849127`) | HIGH |
| `dge` | flag | HW DGE queue (`"NEFF requested %u HW DGE queues…"`) | HIGH |
| `dynamic_dma` | obj/flag | dynamic DMA (v2+); requires `indirect_memcpy`, scratch addr/size, `SB_scratch_partition_size` (`@0x8491a0`) | HIGH |
| `indirect_memcpy` | bool | indirect-memcpy ring (`@0x8491ba`) | HIGH |
| `embedding_update_sync_semaphore` | — | required for `embedding_update` owner (`@0x846857`) | HIGH |

The `owner` → `kbin_dma_ring_type` map (decoded from the LE-ASCII compares in `parse_one_dma_ring`: `"in"` = `0x6e69` / `28265`; the `"dyna"`+`"mic"` constants `0x616e7964` / `0x63696d61` drive the `dynamic_dma` path):

| `owner` value | → ring type | note |
|---|---|---|
| `in` | `KBIN_DMA_RING_TYPE_IN(7)` | no queue-set instances |
| `out` | `KBIN_DMA_RING_TYPE_OUT(8)` | no queue-set instances |
| `data` | `DATA(6)` / `INDIRECT_MEMCPY(9)` if `indirect_memcpy==true` | |
| `act_load` | act-table load | `num_queues` must == 1; unique in NEFF |
| `embedding_update` | `EMBEDDING_UPDATE(15)` | requires per-queue unique sems |
| `dynamic_dma`(path) | forces `data` + dynamic scratch | v2+ arch only |

### `dma` block — `parse_one_dma_block` `@0x4bc620` + `parse_one_desc_ap` `@0x4bb0d0`

| json key | type | loader use / dest | Conf. |
|---|---|---|---|
| `queue` / `instance_name` | string | target queue + ring-instance name | HIGH |
| `id` | uint | block id | HIGH |
| `#comment` / `comment` | string | diagnostic (`"invalid dma block…"`) | HIGH |
| `priority_class` | uint | scheduling priority (`@0x849519`) | HIGH |
| `function_start` / `section_start_desc` | flag | block markers (`@0x8494f7` / `@0x849506`) | HIGH |
| `remote_semaphores` | array | remote sems for the block (`@0x849607`) | HIGH |
| `desc` | array\<obj\> | descriptors → `parse_one_desc_ap` (per descriptor) | HIGH |
| `op` | string | desc operation → `kbin_dma_desc_op` (table below); `"Unknown dma operation: %s"` | HIGH |
| `from` / `from_arr` / `to` | array / obj | source/dest access-patterns (CCE/FMA require array; bounded by `aws_hal_get_sdma_max_fma_sources`) | HIGH |
| `sizes` / `steps` | arrays | access-pattern dims (must match length; `"Size cannot be 0"`) | MED |
| `_dtype` | string | CCE element dtype (kbin_dtype/FP8 map); `"Invalid cce desc ap without dtype"` | HIGH |
| `scale` / `scale_dtype` | number / string | FMA scale; `scale_dtype` must be `"float32"` (`@0x849575`) | HIGH |
| `constant` / `constant_dtype` | — | min/max operand | HIGH |
| `cast` | flag | type-casting copy | HIGH |
| `transpose_op` / `transpose_shape` / `transpose_element_size` / `num_tiling_dimensions` | — | transpose params (`@0x849528`…) | HIGH |

The `op` → `kbin_dma_desc_op` map: `copy`(or absent)=`COPY(0)` · `fma`=`FMA(1)` · `add`=`ADD(2)` · `min`=`MIN(3)` · `max`=`MAX(4)` · `transpose`/`transpose_op`=`TRANSPOSE(5)`. (`dma_ring_instance::add_desc` takes a `kbin_dma_desc_op` plus `kbin_dma_desc_transpose_info` / `kbin_dma_desc_cce_info` structs — confirmed in its signature; full access-pattern micro-schema is a deep-dive candidate.)

### `ucode_lib` entry — `parse_one_ucode_lib` `@0x4b1610`

| json key | type | loader use / dest | Conf. |
|---|---|---|---|
| `library` | string | **required**; lib file (`load_bin_file`); `@0x848fa2` | HIGH |
| `ulib_to_ucode_version` | string | **required**; `"X.Y.Z.W"` (`parse_version_string` `@0x4ade60`); checked vs NRT (ref `"1.21.1.0"`) | HIGH |
| `opcode` | uint | **required**; base opcode (`@0x848f7d`) | HIGH |
| `cpu_id` | uint | target cpu id (asserts `==0`; `@0x848fde`) | HIGH |
| `functions` | array\<obj\> | each → `parse_one_ucode_lib_function` `@0x4b1180`: `{name, opcode, sub_opcode}` (`@0x848f98`) | HIGH |
| `add_lib` | flag | add to lib set (`"UCode Library %s has already been added"`) | HIGH |

---

## 7. Tier 3 — Carveouts and FP8 Config

### `runtime_statebuffer_reservation` — `parse_sb_carveouts` (inlined)

Array of carveout objects → `sb_carveout` (40 B):

| json key | type | loader use / dest | Conf. |
|---|---|---|---|
| `type` | string | **required**; only `"evtaccel"` accepted → `KBIN_SB_CARVEOUT_TYPE_EVTACCEL(1)`; `"Invalid runtime_statebuffer_reservation type %s"` | HIGH |
| `offset` / `size` | uint | SB byte offset / carveout byte size | HIGH |
| `base_partition` / `partitions` | uint | starting SB partition / partition count | HIGH |

> **NOTE —** the carveout `type` is matched by an **8-byte LE compare against `0x6C65636361747665`**, which is `"evtaccel"` little-endian — the only accepted value. The decoded magic *is* the human-readable string; the loader does a single 64-bit equality, not a `strcmp`. A reimplementer emitting any other `type` string hits the reject path. → `sb_carveout {type@0, offset@8, size@16, start_partition@24, num_partitions@32}`. (HIGH — magic decoded byte-exact.)

### `fp8_conversion_config` — `parse_fp8_conversion_config` (inlined)

| json key | type | loader use / dest | Conf. |
|---|---|---|---|
| `fp8_range_extension` | bool/uint | enable FP8 range extension → `kbin_fp8_conv_cfg_t+0` | HIGH |
| `emax_fp8_e4m3` / `emax_fp8_e5m2` | uint | max exponent E4M3 / E5M2 → `+4` / `+8` | HIGH |
| `ocp_sat` | bool | OCP saturation mode → `+12` | HIGH |
| `fp8_sis` | bool | scale-in-software → `+13` | HIGH |
| `fp8_sns` | bool | scale-not-stochastic → `+14` | HIGH |

Each field is runtime-overridable by env (`NEURON_RT_DBG_FP8_RANGE_EXT` / `_EMAX_E4M3` / `_EMAX_E5M2` / `NEURON_RT_DBG_OCP_FP8_SAT` / `_FP8_SIS` / `_FP8_SNS`) → `kbin_fp8_conv_cfg_t` (16 B), wrapped in `fp8_conversion_info` (20 B) `{initialized@0, cfg@4}`.

---

## 8. Discriminator-String Decode Tables

The three core string→enum maps the loader resolves by LE-ASCII integer compare (not `strcmp`). These are the wire-meaning of the discriminator strings; the integer constants are decoded byte-exact from the compare sites.

### dtype string → `nrt_dtype_t` (tier 1; `str_to_dtype` inlined in `extract_tensor_info` `@0xe1710`)

| dtype string | → `nrt_dtype_t` | LE-ASCII anchor |
|---|---|---|
| `float32` | `10` FLOAT32 | `"floa"`+`"t32"` (`1634692198`/`842232929`) |
| `float16` | `7` FLOAT16 | `"floa"`+`"at16"` |
| `bfloat16` | `6` BFLOAT16 | `0x363174616F6C6662` `"bfloat16"` |
| `int8` | `2` INT8 | `947154537` `"int8"` |
| `int16`/`int32`/`int64` | `4`/`8`/`12` | `"int1"`+`'6'` / `"int3"`+`'2'` / `"int6"`+`'4'` |
| `uint8` | `3` UINT8 | `"uint"`+`'8'` |
| `uint16`/`uint32`/`uint64` | `5`/`9`/`1` | `"uint"`+`"16"`/`"32"`/`"64"` |
| *(else)* | `0` UNKNOWN | `"Unknown dtype: %s, defaulting to UNKNOWN"` (1 byte/elem) |

> **NOTE —** the tier-1 `str_to_dtype` matches the **smaller** set above (the I/O tensor dtypes). The def.json side (`var`/`desc` `dtype`/`_dtype`) uses a separate `kbin_dtype` map that additionally recognizes `float32r`, `float8e3/e4/e5`, `float8_e4m3fn` (`.rodata @0x848e90`), `float8_e5m2`, `float4_e2m1fn`, and their `_x4` variants. Whether every FP8/FP4 string is reachable from a def.json key is **MED** — the strings are present near `parse_instr`, but the full `kbin_dtype` matcher table (`@0x4c50f0`/`0x4c7100`) was not enumerated this pass. The byte sizes of the FP4/FP8 dtypes are owned by [the dtype System](dtype-system.md).

### `target` string → `al_hal_tpb_arch_type_t` (tier 2; `parse_target` `@0x497c10`)

| `target` value | → arch type | id |
|---|---|---|
| `sunda` / `v2` (`@0x848978`) | `AL_HAL_TPB_ARCH_TYPE_SUNDA` | 2 |
| `cayman` / `v3` (`@0x84897e`) | `AL_HAL_TPB_ARCH_TYPE_CAYMAN` | 3 |
| `mariana` / `v4` (`@0x848985`) | `AL_HAL_TPB_ARCH_TYPE_MARIANA` | 4 |
| `*` | `al_hal_tpb_get_arch_type()` (present HW) | — |

### `owner` string → `kbin_dma_ring_type` — see §6 (decoded: `"in"`=`0x6e69`, `dynamic_dma` via `"dyna"`/`"mic"`).

---

## 9. Tier 4 — Payloads Referenced by Name

`def.json` string values name raw binary blobs the loader fetches from the container member table (not JSON):

| reference | from key | loader | note |
|---|---|---|---|
| `<eng>.bin` | per-engine `instr` (string) | `parse_instr` `@0x4aefe0` → `load_bin_file` `@0x4ae500` | size-capped (`"Instruction size %zu for %s exceeds maximum…"`) |
| `<name>.bin` | var `file_name` | `load_bin_file` `@0x4ae500` | `"<sg_dir>/<name>.bin"` RB-tree lookup |
| `<name>.npy` | var `file_name` ending `.npy` | `numpy_load` `@0x4cb810` | NumPy v1: magic word `0x4e93` (`"\x93N"`); `"NUMPY %s bad header %x != 0x4e93"` |

These are byte blobs, not parsed by simdjson; the loader copies them out of `neff_t::files` by pathname. Their interior layout (instruction encoding, weight tensor format) is owned by the section-taxonomy and kbin-structs siblings.

---

## CORRECTION

> **CORRECTION (W2-NEFF-PROTO) —** the NEFF metadata is **JSON parsed by simdjson 0.9.0 DOM**, *not* protobuf. Any earlier scaffold or sibling that implied a protobuf metadata path (e.g. reading the binary-wide `"Illegal jstype for int64…"` / `"jstype is only allowed on…"` descriptor strings `@0x8312e0`/`0x8313f8` as NEFF-related) is wrong: those strings belong to an **unrelated** embedded component (telemetry/grpc), and every NEFF metadata parser — `parse_neff_json` `@0xe1d10`, `kelf::kelf::load` `@0x497dc0`, `kelf_load_from_neff` `@0x4c0870`, and the whole `parse_one_*` family — calls **only** `simdjson::dom::*` primitives (`parse_into_document` `@0xe4b50`, `at_key` `@0x49ced0`, `operator[]` `@0xe49b0`, the typed getters `@0x4c5660`…). There is no protobuf descriptor, no `.proto`, and no FlatBuffers in the NEFF ingest path. (HIGH — callgraph-confirmed: zero protobuf callees in any tier-1/2/3 parser; simdjson is the sole JSON engine.)

---

## Related Components

| Name | Relationship |
|---|---|
| `parse_neff_json` (`@0xe1d10`) | tier-1 TVM-relay parse; the `__kelf` discriminator + `io_node_info_t` IO maps |
| `kelf::kelf::load` (`@0x497dc0`) | tier-2 KELF descriptor: `version`/`target`/`graphs[]` |
| `kelf_load_from_neff` (`@0x4c0870`) | tier-3 `def.json` top-level demux → `mla_resources` |
| `parse_one_variable` (`@0x4b36b0`) | the `var` tensor sub-schema → the `mem_ref` tree |
| simdjson 0.9.0 DOM | the sole JSON engine; `parse_into_document` + `at_key`/`operator[]` + typed getters |

## Cross-References

- [neuronx-cc companion wiki](../../neuronx-cc/wiki/) — the **canonical producer-side** NEFF schema (every key `neuronx-cc` emits, including those the runtime ignores); this page derives only the consumer subset, do not re-derive the producer schema from it
- [NEFF Container (gzip-tar, No-Magic Header)](container.md) — where these JSON members come from: the in-memory tar walk into `neff_t::files` and the manifest router
- [The Load Pipeline (Parse → Build → Stage → Relocate)](load-pipeline.md) — where the metadata parse (stages S0–S3, `IOCTL`-free) sits in the full model load
- [Static Memory Planning (mem_ref)](memory-planning.md) — the `var` `type` → `kbin_mr_type` tree and the build→plan→resolve chain the tensor schema feeds
- [The dtype System](dtype-system.md) — `nrt_dtype_t` / `kbin_dtype` byte sizes and the FP8/FP4 dtype-string set referenced here
- [kelf2kbin: JSON → KBIN Lowering](kelf2kbin.md) — how the `mla_resources` working set this page populates is lowered to the 424-byte `kbin`
- [Overview: the Model-File Journey](overview.md) — the high-level NEFF → KBIN → device map this schema feeds
- [back to index](../index.md)
