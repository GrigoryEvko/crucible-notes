# The `kelf-N.json` Field Schema

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310). Producer addresses are from `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331…`; `.text`/`.rodata` VMA == file offset). The `bir::EngineType` enum is from `neuronxcc/starfish/lib/libBIR.so`. Reader addresses are from `neuronxcc/kra/NeffInfo.cpython-310-x86_64-linux-gnu.so` (Cython, with debug_info). These are **two distinct VA frames** — never mix a libwalrus address with a NeffInfo address. cp311/cp312 share the logic but not the exact addresses.*

## Abstract

`kelf-N.json` is a top-level member of the NEFF tar container — one file per compiled subgraph/virtual core (`kelf-0.json` ↔ `sg00`). The name suggests an obvious shape: a flat table mapping each datapath engine to the `{filename, offset, size}` of its instruction binary. **It is not that.** `kelf-N.json` is a *per-subgraph graph* encoded in the **nnvm / TVM `graph_runtime` JSON dialect** — the same dialect as the top-level `neff.json` — tagged `sg_coreV1` (subgraph-core schema V1) at schema **`revision` `0.5`**. Its structural keys are the TVM graph-runtime vocabulary (`arg_nodes`, `heads`, `node_row_ptr`, `attrs`, `dltype`, `storage_id`, the `list_*` typed-list wrappers, `metadata`, `target_arch`, `graphs`, `revision`), and the engine→binary mapping is expressed through a `__kelf` op-node whose `storage_id` / `func_name` machinery *indexes* the per-engine `.bin` members — it does **not** embed bare offset/size triples. The actual byte locations of each engine binary are delegated to the **NEFF tar BOM** plus the sibling `<Engine>.json` descriptors.

> **CORRECTION — `kelf-N.json` is NOT a flat `{engine → {filename, offset, size}}` map.** The S02 §299 named gap hypothesised that schema. The recovered writer key-pool (`libwalrus.so` `.rodata` `0x1c869f7…0x1c86b00`) contains **no** `offset`, `size`, or bare per-engine `filename` keys at all. What it contains is the TVM `graph_runtime` key vocabulary (`tvm_op`, `attrs`, `arg_nodes`, `heads`, `node_row_ptr`, `storage_id`, `dltype`, `list_shape`/`list_int`/`list_str`) plus the `sg_coreV1` version tag and `revision` `0.5`. `kelf-N.json` is therefore a per-subgraph TVM graph, and per-engine binary locations live in the **tar BOM** (`NeffFileWriter::addToBom` / `writeArchiveFile`) and the per-engine `<Engine>.json` descriptors — never inside `kelf-N.json` itself. *(Key-pool keys = CONFIRMED byte-verified; the delegation-to-BOM = STRONG by absence of any offset/size key.)*

For reimplementation, the contract is:

- **The writer** — `NeffPackager::writeNeffJson` @ `0x152c740` builds the `kelf-`+`to_string(i)`+`.json` filename by `std::string` append (no `printf`), and inserts a `__kelf` op-node into the graph (loop body `0x152e500…0x152e7f0`), as an `nlohmann::json` **ordered map** (key insertion order preserved).
- **The dialect** — TVM `graph_runtime` JSON, version tag `sg_coreV1`, `revision` `"0.5"`; the full key vocabulary is the byte-verified `.rodata` pool below.
- **The reader** — `NeffInfo.getKelf` @ `0xf640` reads the root member `kelf-0.json` (N hard-coded 0) and extracts a single key, `target`; `getArchType` @ `0x14110` wraps it. No tool in this wheel reads any other key out of `kelf-N.json`.
- **N** — per-subgraph / per-core index: one `kelf-N.json` per `bir::Module` in the writer's module vector; `kelf-0` ↔ `sg00`, tying to the [LNC per-core split](../arch/lnc-memory-model.md).

| | |
|---|---|
| **Container member** | `kelf-N.json` — top-level (root) NEFF tar member, fixed name `kelf-<N>.json` |
| **Dialect / version** | nnvm/TVM `graph_runtime` JSON · tag `sg_coreV1` · `revision` `"0.5"` |
| **Writer** | `NeffPackager::writeNeffJson` @ `0x152c740` (libwalrus.so) · `nlohmann::json` ordered_map |
| **`__kelf` node builder** | loop body `0x152e500…0x152e7f0` (libwalrus.so) |
| **Reader** | `NeffInfo.getKelf` @ `0xf640` → `json.loads(getNeffFile("kelf-0.json"))["target"]` |
| **Reader wrapper (arch)** | `NeffInfo.getArchType` @ `0x14110` (NeffInfo.so) |
| **Key the reader extracts** | `target` (`__pyx_n_u_target`) — the single accelerator-type field |
| **Engine enum** | `bir::EngineType2string` @ `0x47fa80` (libBIR.so) · datapath set `{PE, Pool, Activation, SP, DVE}` |
| **N index** | per-subgraph / per-core (`kelf-0` ↔ `sg00`); one per `bir::Module` |
| **Binary locations** | delegated to tar BOM + sibling `<Engine>.json` — **not** in `kelf-N.json` |

---

## 0. The Byte-Verified Key Pool

### Purpose

Every claim about the schema rests on one piece of evidence: the packed, null-terminated string blob in libwalrus `.rodata` that holds the entire `kelf`/`neff` TVM-graph key vocabulary. This is the writer's key table — the strings it hands to `nlohmann::json::operator[]` when building nodes. Recovering it verbatim refutes the flat-map premise and fixes the dialect.

### The Pool

The blob begins at VMA `0x1c869f7` (file offset `29911543`) and runs to `~0x1c86b00`. Read out as `\0`-delimited tokens, in on-disk order:

```text
kelf-0.json   output_names  is_param      may_alias    must_alias
sg_coreV1     __kelf        flatten_data  num_inputs   kelf-
tvm_op        attrs         arg_nodes     heads        list_shape
list_int      storage_id    list_str      dltype       node_row_ptr
signatures    metadata      target_arch   graphs       0.5
revision      ArchLevl > ArchLevel::Tonga …
```

Two facts fall straight out of this layout:

1. **The dialect is TVM `graph_runtime`.** `tvm_op`, `attrs`, `arg_nodes`, `heads`, `node_row_ptr`, `storage_id`, `dltype`, and the `list_shape`/`list_int`/`list_str` typed-list wrappers are the canonical nnvm/TVM graph-runtime serialization keys. No other JSON dialect uses this exact vocabulary. **[CONFIRMED — `\0KEY\0` byte adjacency in `libwalrus.so`.]**
2. **`revision` `"0.5"` is the schema version for this graph.** The literal `"0.5"` sits immediately before `revision` in the pool, adjacent to `graphs`/`target_arch`/`metadata`. **[CONFIRMED — string at pool offset, neighbour of `revision`.]**

The `sg_coreV1` tag (the subgraph-core schema-V1 version string) and the three `kelf` tokens (`kelf-0.json`, `kelf-`, `__kelf`) live in the same packed blob. The presence of `kelf-` (the concat prefix) *and* `kelf-0.json` (the reader's fixed name) in one pool is the writer/reader handshake made visible.

> **NOTE —** the pool is *shared* between `kelf-N.json` and `neff.json`: both are TVM graphs and both carry `sg_coreV1` / `__kelf` / `revision`. The `neff.json` `__kelf` *node* (op-type `kelf`, attrs `{func_name, flatten_data, num_inputs, kelf}`) is documented on the [NEFF/KELF `__kelf` node page](neff-kelf-node.md) (12.7), whose `attrs.kelf` string names the `kelf-<i>.json` *member* this page owns. The four-sidecar overview is in [NEFF JSON Sidecars](neff-json-sidecars.md).

---

## 1. The `kelf-N.json` Schema

### Structural Keys (TVM graph-runtime)

The graph-level keys, all CONFIRMED present as `\0KEY\0` in the writer pool:

| Key | Role | Confidence |
|---|---|---|
| `arg_nodes` | indices of the graph's input/parameter nodes | CONFIRMED (string) |
| `heads` | output node entries `[node_id, index, version]` | CONFIRMED (string) |
| `node_row_ptr` | CSR-style row pointer over node entries | CONFIRMED (string) |
| `attrs` | graph attribute table (holds the typed lists below) | CONFIRMED (string) |
| `dltype` | per-entry DLPack dtype list (under `attrs`, paired with `list_str`) | CONFIRMED (string) |
| `storage_id` | per-entry storage/buffer id (under `attrs`, paired with `list_int`) | CONFIRMED (string) |
| `list_shape` / `list_int` / `list_str` | TVM attr-table typed-list tag wrappers | CONFIRMED (strings) |
| `metadata` | graph metadata sub-object (holds `target_arch` / `graphs` / `revision`) | CONFIRMED (string) |
| `target_arch` | architecture-level string (e.g. gates `ArchLevl > ArchLevel::Tonga`) | CONFIRMED (string) |
| `graphs` | sub-graph list / container | CONFIRMED (string) |
| `revision` | schema revision; literal value `"0.5"` | CONFIRMED (string + value) |
| `name` | node / graph name | CONFIRMED (in-pool) |
| `signatures` | I/O signature table | CONFIRMED (string) |
| `output_names` / `is_param` / `may_alias` / `must_alias` | I/O + alias annotation keys (shared with neff/tensor_map vocabulary) | CONFIRMED (strings) |

### The `__kelf` Op-Node Attrs

The node that points at the per-engine binaries. Its attrs are CONFIRMED both as strings and as `to_string`-built values in the writer disasm (§2):

| Key | Role | Confidence |
|---|---|---|
| `__kelf` | the kelf op-node key (op-type `"kelf"`) | CONFIRMED (string `0x1c86a38`) |
| `tvm_op` | node op-type tag | CONFIRMED (string) |
| `func_name` | TVM function-name attr; the constant `"0"` in `neff.json`'s `__kelf` node (see CORRECTION) | CONFIRMED (string) |
| `flatten_data` | `"0"`/`"1"` flag (TVM standard attr) | CONFIRMED (string `0x1c86a3f`) |
| `num_inputs` | input count, built via `std::to_string` | CONFIRMED (string `0x1c86a4c` + `to_string` call) |

> **CORRECTION (Part-12 reconcile) —** the `neff.json` → `kelf-<i>.json` linkage is the `__kelf` node's **`attrs.kelf`** string (`"kelf-" + to_string(i) + ".json"`), **not** `func_name`. [12.7](neff-kelf-node.md) byte-resolves the `neff.json` `__kelf` node: `attrs.func_name == "0"` (a constant — the per-core kernel *is* the kelf, so the TVM function name is meaningless), and `attrs.kelf` carries the member name. The `func_name`/`storage_id` "indexes the per-engine `.bin`" framing above describes the TVM graph machinery *inside* `kelf-<i>.json`; the cross-member pointer from `neff.json` is `attrs.kelf`.

### The Key the Reader Extracts

| Key | Read by | Meaning | Confidence |
|---|---|---|---|
| `target` | `NeffInfo.getKelf` @ `0xf640` | accelerator/arch type for the whole NEFF | CONFIRMED (`__pyx_n_u_target`) |

The reader's docstring (verbatim from NeffInfo.so `.rodata`):

```text
Get the target type from kelf-0.json. Does not support mixed accelerator usage.
```

So `getKelf` returns exactly `kelf-0.json["target"]` and nothing else. The writer stores the arch as `target_arch` inside the `metadata` sub-object; the reader exposes it via a `target` lookup.

> **GOTCHA —** `target` (reader key, CONFIRMED) and `target_arch` (writer key, CONFIRMED) are not obviously the same JSON path. The reader's `target` is byte-pinned to `__pyx_n_u_target`; the writer's `target_arch` is byte-pinned to the `.rodata` pool. Whether `getKelf` reads a top-level `target` key or a nested `metadata.target_arch` — i.e. the exact JSON path — is **INFERRED** (no example `kelf-N.json` file is shipped in this wheel to settle it). A reimplementer should emit *both* shapes compatibly or confirm against a real `.neff`.

---

## 2. The Writer — `NeffPackager::writeNeffJson`

### Purpose

`writeNeffJson` is the libwalrus producer that emits the kelf filenames and the `__kelf` graph node. It is the same method that emits `neff.json` (the two graphs share the key pool), iterating the vector of `bir::Module`s — one module per subgraph.

### Symbol

```text
neuronxcc::backend::NeffPackager::writeNeffJson(
    NeffFileWriter&,
    std::vector<std::unique_ptr<bir::Module>>&)        @ 0x152c740   [nm-confirmed, demangled]
```

The JSON type throughout is `nlohmann::json_abi_v3_11_3` with `ordered_map` — **key insertion order is preserved** in the output, so a reimplementation must emit keys in the same order to byte-match. **[CONFIRMED — `nlohmann…ordered_map…` call targets in the disasm.]**

### The `__kelf` Node / Filename Builder

The loop body at `0x152e500…0x152e7f0` builds, per subgraph index `i`, the kelf filename and the `__kelf` node. Annotated from the disasm:

```c
// NeffPackager::writeNeffJson — per-subgraph loop body @ 0x152e500..0x152e7f0
// (libwalrus.so VA frame; json == nlohmann::json_abi_v3_11_3 ordered_map)
for (int i = 0; i < modules.size(); ++i) {
    std::string idx = std::to_string(i);              // call to_string @ 0x152e559

    // build the "__kelf" op-node as an nlohmann ordered_map
    json node = json::object();                       // operator new @ 0x152e5d0

    node["__kelf"]       = ...;                        // key "__kelf"        @ .rodata 0x1c86a38
    node["func_name"]    = ...;                        // node attr "func_name"
    // flatten_data default index string "0"
    node["flatten_data"] = "0";                        // key "flatten_data"  @ .rodata 0x1c86a3f

    std::string ninp = std::to_string(num_inputs);    // call to_string @ 0x152e6b9
    node["num_inputs"]   = ninp;                       // key "num_inputs"    @ .rodata 0x1c86a4c

    // build filename = "kelf-" + i + ".json"  — std::string APPEND, NOT printf
    std::string fname = "kelf-";                       // prefix @ .rodata 0x1c86a57
    std::string idx2  = std::to_string(i);            // call to_string @ 0x152e72f
    fname.append(idx2);                                // _M_append @ 0x152e771
    fname.append(".json");                             // suffix ".json" @ .rodata 0x1c869fd
    // graph-node op type literal "kelf" @ .rodata 0x1c86a3a

    graph["graphs"][...] = node;                       // insert into the TVM graph
}
```

Two CONFIRMED facts from this disasm:

1. **The filename is `"kelf-" + std::to_string(i) + ".json"`, built with `std::string::_M_append` (@ `0x152e771`) — there is no `%d` format string.** The `kelf-` prefix and `.json` suffix are separate `.rodata` literals. **[CONFIRMED.]**
2. **A node of op-type `"kelf"` (key `"__kelf"`) is inserted with attrs `{func_name, flatten_data, num_inputs}`**, the latter two built via `std::to_string`. **[CONFIRMED — `to_string` calls + `nlohmann operator[]` key-assignment.]**

### Sibling Producer Methods

For orientation, the `NeffPackager` roster around `writeNeffJson` (all nm-confirmed):

| Method | @ | Emits |
|---|---|---|
| `writeNeffJson` | `0x152c740` | `neff.json` incl. kelf filenames + `__kelf` nodes |
| `writeDefJson` | `0x152a0e0` | `sgNN/def.json` (var/dma/cc/ucode/fp8/features) |
| `run(std::vector<unique_ptr<bir::Module>>&)` | `0x15307e0` | top-level NEFF packaging entry |
| `run(bir::Module&)` | `0x151ec00` | single-module entry |

---

## 3. The N Index — Per-Subgraph / Per-Core

### What N Enumerates

`N` is the subgraph index `i` in `writeNeffJson`'s loop over the `std::vector<std::unique_ptr<bir::Module>>`. One `bir::Module` == one subgraph; the filename is `"kelf-"+to_string(i)+".json"`. The `sg_coreV1` tag reads "subgraph-core, schema V1": each `kelf-N` corresponds to one subgraph mapped to one virtual NeuronCore, so **`kelf-0` ↔ `sg00`**. **[STRONG — `to_string(i)` over the module vector + the `sg_coreV1` tag + the reader's `sg00`↔`kelf-0` pairing.]**

When a graph is split across LNC cores, each core's subgraph gets its own `kelf-N.json` (N ranges over the per-core subgraphs). The split itself — what each core owns privately vs. shares — is the subject of [the multi-core (LNC) memory model](../arch/lnc-memory-model.md).

> **GOTCHA —** the reader hard-codes `kelf-0.json` (N = 0) in `getKelf` and only inspects subgraph 0 for the `target` type — the docstring is explicit: *"Does not support mixed accelerator usage."* It assumes all subgraphs share one accelerator target. A reimplementation that emits divergent `target` values across `kelf-N` files for different cores would break this reader's single-target assumption.

---

## 4. The Engine → Binary Mapping (Delegated)

### The Datapath Engines

The `__kelf` node references the per-engine instruction binaries. The engines are the five **datapath** `bir::EngineType` values, byte-confirmed from two independent sources:

- `bir::EngineType2string` @ `0x47fa80` and `string2EngineType` @ `0x47fe60` (libBIR.so) — the full enum is `{PE, Pool, Activation, SP, DVE, DMA, All, Unassigned}` (plus `GPSIMD`, `Sync` in the parse table).
- `analyze_neff_artifacts.py:74` iterates `['Pool', 'SP', 'DVE', 'PE', 'Activation']` — the exact five that ship `.bin`/`.json` per subgraph.

| EngineType | Datapath? | On-disk member (under `sgNN/`) | Confidence |
|---|---|---|---|
| `PE` (Tensor) | yes | `PE.bin` + `PE.json` | CONFIRMED |
| `Pool` | yes | `Pool.bin` + `Pool.json` | CONFIRMED |
| `Activation` | yes | `Activation.bin` + `Activation.json` | CONFIRMED |
| `SP` (Vector) | yes | `SP.bin` + `SP.json` | CONFIRMED |
| `DVE` | yes | `DVE.bin` + `DVE.json` | CONFIRMED |
| `DMA` / `All` / `Unassigned` | no | — (non-datapath / sentinel) | CONFIRMED (enum) |

### How the Locations Are Found (Not From `kelf-N.json`)

Each engine's instruction stream is a **separate NEFF tar member** `sgNN/<Engine>.bin`, with a sibling `sgNN/<Engine>.json` carrying DMA/descriptor metadata. The kelf graph's nodes reference these via the TVM `storage_id` / `func_name` machinery — the `.bin` *member name* is the "filename"; per-engine offset/size live in:

- **the NEFF tar BOM** — assembled by `NeffFileWriter::addToBom` @ `0x153fb80` and `NeffFileWriter::writeArchiveFile` @ `0x153e030` (md5 over members; the tar member length gives each `.bin`'s size); and
- **the sibling `<Engine>.json`** — DMA descriptor layout (`dma[].desc[]` `from`/`to`/`from_sizes`/`to_sizes`), read by `analyze_neff_artifacts.py`.

**[engine list = CONFIRMED; `.bin`/`.json` member layout = STRONG; absence of literal offset/size keys inside `kelf-N.json` = STRONG — no such bare keys exist in the writer pool.]**

> **CORRECTION (recap) —** the `{filename, offset, size}` triple the S02 §299 premise asked for does **not** live inside `kelf-N.json`. The `filename` is the `.bin` member name (via `func_name`/`storage_id`); the `offset` is the tar member position (BOM); the `size` is the tar member length and/or the `<Engine>.json` descriptor extent. `kelf-N.json` indexes; it does not tabulate offsets.

---

## 5. The Reader — `NeffInfo.getKelf`

### Purpose

`NeffInfo` (`neuronxcc.kra.NeffInfo`, Cython, not stripped) is the tooling/test-infrastructure reader — *not* the on-device runtime parser (libnrt parses the NEFF at the binary level). Every getter shells out to `dd | tar` to extract a member, then `json.loads` it. The `kelf-N.json` reader is the simplest of all: it reads one member and one key.

### Symbol + Algorithm

```text
NeffInfo.getKelf      @ 0xf640    (__pyx_pw_9neuronxcc_3kra_8NeffInfo_8NeffInfo_17getKelf)
NeffInfo.getArchType  @ 0x14110   (wraps getKelf → arch codename)
```

```python
# NeffInfo.getKelf  @ 0xf640  (NeffInfo.so VA frame — NOT a libwalrus address)
def getKelf(self):
    # Get the target type from kelf-0.json. Does not support mixed accelerator usage.
    raw = self.getNeffFile("kelf-0.json")     # member name HARD-CODED, N = 0
    obj = json.loads(raw)                       # TVM graph_runtime JSON, rev "0.5"
    return obj["target"]                        # __pyx_n_u_target — the ONLY key read

# NeffInfo.getArchType  @ 0x14110 — surfaces getKelf's target as the arch codename
```

`getNeffFile` is the member extractor (`getKelf` funnels through it): it runs `dd if=<neff> bs=1024 skip=1 | tar [xvf|xvzf] - -O <member>`, stripping the 1024-byte `neff_header` before tar sees its first block. That access path — and the rest of the `NeffInfo` getter roster — is documented in [NEFF JSON Sidecars](neff-json-sidecars.md); the key point here is that **`kelf-0.json` is a root member** (not under `sgNN/`, unlike `sgNN/def.json`), and the reader extracts only `target`.

> **NOTE —** `analyze_neff_artifacts.py` (the compiler-side analyzer) reads the *sibling* per-engine artifacts directly (`sgNN/{Pool,SP,DVE,PE,Activation}.{json,bin}`, `sgNN/def.json`) but **never** reads `kelf-N.json` itself. So in the entire shipped Python surface, the only field ever extracted from `kelf-N.json` is `target`. The full graph (nodes, `storage_id`, `node_row_ptr`) is consumed only by the on-device runtime, which is not present in this compiler-only wheel.

---

## 6. Re-Verification Ceiling

What is byte-pinned vs. inferred:

| Claim | Status | Evidence |
|---|---|---|
| Key pool = TVM `graph_runtime` vocabulary | CONFIRMED | `\0KEY\0` blob `0x1c869f7…0x1c86b00` |
| `revision` value `"0.5"` | CONFIRMED | literal adjacent to `revision` in pool |
| `sg_coreV1` tag | CONFIRMED | string in pool |
| Filename = `"kelf-"+to_string(i)+".json"`, no printf | CONFIRMED | disasm `0x152e72f`/`0x152e771` |
| `__kelf` node attrs `{func_name, flatten_data, num_inputs}` | CONFIRMED | `to_string` calls + key strings |
| `writeNeffJson` @ `0x152c740` | CONFIRMED | nm demangle |
| `getKelf` reads `kelf-0.json["target"]` | CONFIRMED | docstring + `__pyx_n_u_target` + `kelf-0.json` string |
| Datapath engines `{PE,Pool,Activation,SP,DVE}` | CONFIRMED | `EngineType2string` + `analyze_neff_artifacts.py:74` |
| Not a flat engine→offset map | STRONG | no offset/size/filename key in writer pool |
| Per-engine `.bin`/`.json` member layout | STRONG | analyze_neff + tar-member semantics |
| N = per-subgraph/per-core | STRONG | `to_string(i)` over module vector + `sg00`↔`kelf-0` |
| `target` vs `metadata.target_arch` exact JSON path | INFERRED | no example `kelf-N.json` shipped |
| Runtime walk `kelf-N.json` → `<Engine>.bin` | INFERRED | no runtime binary in this wheel |

The two structural inferences (`target` JSON path; runtime traversal) would each need a shipped example `.neff`'s `kelf-N.json` or the `aws-neuronx-runtime` binary to settle — neither is in this compiler-only wheel.

---

## Related Components

| Name | Relationship |
|---|---|
| [NEFF JSON Sidecars](neff-json-sidecars.md) | the four-sidecar overview + the `dd\|tar` access path `getKelf` funnels through |
| [The multi-core (LNC) memory model](../arch/lnc-memory-model.md) | the per-core split that produces one subgraph (one `kelf-N.json`) per core |
| [NEFF/KELF `__kelf` node](neff-kelf-node.md) (12.7) | the `neff.json` `op:"__kelf"` node whose `attrs.kelf` string names this `kelf-<i>.json` member |
