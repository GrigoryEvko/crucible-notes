# The neff.json `__kelf` Subgraph Node

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 share the `.text` logic). The producer is `neuronxcc::backend::NeffPackager::writeNeffJson` in `neuronxcc/starfish/lib/libwalrus.so`. The ELF is stripped of `.symtab` but retains the full `.dynsym`, so the method maps to an address via `nm -DC libwalrus.so`. For `.text` and `.rodata` the virtual address equals the file offset (the producer body lives at `0x152c740..0x15307e0`; its value strings cluster in `.rodata` at `0x1c86a03..0x1c86a5d`). Other wheels differ; treat every address as version-pinned.*

## Abstract

`neff.json` is the **TVM / NNVM graph-runtime JSON** member of a NEFF — `nodes / arg_nodes / heads / attrs / node_row_ptr` wrapped in a thin Kaena envelope (see [NEFF JSON Sidecars](neff-json-sidecars.md) for the v0.5 overview). This page covers exactly one element of `graphs[].definition.nodes[]`: the node with **`op = "__kelf"`**. There is one such node per virtual NeuronCore, and its single job is to *name* that core's `kelf-<i>.json` member (the per-core engine BOM decoded in the kelf-json page, in flight) through its `attrs.kelf` string. It is the one and only edge from the graph member to the per-core kelf.

The node is emitted by `NeffPackager::writeNeffJson` @`0x152c740`, called once from `NeffPackager::run(vector<unique_ptr<Module>>&)` @`0x15307e0`, which asserts `modules.size() == vnc_nc_count` — so the count of `__kelf` nodes equals the virtual-core count. [CONFIRMED — `nm -DC` resolves both symbols at those addresses.]

This page also nails two negatives that earlier sketches got wrong: **`sgLnk` is not a `neff.json` key** (it is the `bir_linker` per-core symlink directory), and there is **no `size` key** on the node. Both were checked against the disassembly, not just asserted — see the [Corrections](#corrections).

## The node, field by field

For each virtual NeuronCore `i` (each `bir::Module` in the input vector), `writeNeffJson` appends one node to `graph["nodes"]`:

```json
{
  "op"   : "__kelf",
  "name" : "sg_coreV1<i>",
  "inputs" : [ [nodeId, idx, 0], ... ],
  "attrs"  : {
    "func_name"    : "0",
    "flatten_data" : "0",
    "num_inputs"   : "<n>",
    "kelf"         : "kelf-<i>.json",
    "num_outputs"  : "<n>",
    "tvm_op"       : "sg_coreV1<i>"
  },
  "output_names" : [ "<out0>", ... ]
}
```

| Key | Value | Source | Tag |
| --- | --- | --- | --- |
| `op` | `"__kelf"` | value-string ctor on `aKelf` @`.rodata 0x1c86a38` | CONFIRMED |
| `name` | `"sg_coreV1<i>"` | `to_string(i)` then prepend tag `"sg_coreV1"` (`sub_96C8E0` @`0x96c8e0`) | CONFIRMED |
| `inputs` | `[[nodeId, idx, 0], ...]` | TVM entry-refs into the IO `null` nodes; key @`0x152ebc1` | CONFIRMED (key) / INFERRED (triple layout) |
| `attrs.func_name` | `"0"` | literal `"0"` @`0x152e657` — constant (the kelf *is* the implementation) | CONFIRMED |
| `attrs.flatten_data` | `"0"` | `to_string(0)` @`0x152e6b9` | CONFIRMED |
| `attrs.num_inputs` | `to_string(numIn)` | `to_string` → key `"num_inputs"` @`0x152e6d6` | CONFIRMED (key) / INFERRED (counter) |
| `attrs.kelf` | `"kelf-<i>.json"` ★ | `"kelf-"` (`0x1c86a57`) ++ `to_string(i)` ++ `".json"` (`0x1c7da3f`); key @`0x152e7df` | CONFIRMED |
| `attrs.num_outputs` | `to_string(numOut)` | key `"num_outputs"` @`0x152ea08` | CONFIRMED (key) / INFERRED (counter) |
| `attrs.tvm_op` | `"sg_coreV1<i>"` | the node name re-stamped; key @`0x152ea7f` | CONFIRMED |
| `output_names` | `["<out0>", ...]` | key @`0x152ebfb` | CONFIRMED |

Every `attrs.*` value except `func_name` is a `to_string(...)` of an integer, so the JSON carries **strings, not numbers** — `"flatten_data": "0"`, `"num_inputs": "4"`, and so on. `func_name` is the literal `"0"` (the TVM "function name" is meaningless here because the per-core kernel is the kelf, not a TVM function). [CONFIRMED — all are string ctors / `to_string` in the `0x152e6xx..0x152eaxx` region.]

The disassembly of `writeNeffJson` references each of `__kelf` (`0x1c86a38`), `sg_coreV1` (`0x1c86a2e`), `kelf-` (`0x1c86a57`), `flatten_data` (`0x1c86a3f`), `tvm_op` (`0x1c86a5d`), and `output_names` (`0x1c86a03`) by a `lea ...(%rip)` whose resolved target lands exactly on that `.rodata` string. [CONFIRMED — RIP-relative target resolution over the `0x152c740..0x15307e0` body.]

## `sg_coreV1<i>` — naming and enumeration

The node name decodes as `sg` (subgraph) + `coreV1` (the fixed subgraph-kind tag, "core, version 1") + `<i>` (the decimal core index, appended **with no separator**):

```c
// node name construction
std::string name = std::to_string(i);   // "0", "1", ... "10", ...
sub_96C8E0(&name, "sg_coreV1");          // _M_replace(pos=0, len=0, "sg_coreV1") — PREPEND
// result: "sg_coreV1" ++ to_string(i)
```

`sub_96C8E0` @`0x96c8e0` is a `std::string::_M_replace(0, 0, ...)` prepend helper, preceded by the `to_string`. So:

- node `0` → `"sg_coreV10"`
- node `1` → `"sg_coreV11"`
- node `9` → `"sg_coreV19"`
- node `10` → `"sg_coreV110"`

[CONFIRMED — `sg_coreV10` does **not** appear as a baked literal in the binary (`rg -c -a "sg_coreV10"` returns 0), proving the name is assembled at runtime from the tag `sg_coreV1` (which *is* a literal) plus the index, exactly as above.]

The same string is written **twice** — as `name` and as `attrs.tvm_op` — so a runtime can index the per-core subgraph by either field. [CONFIRMED.]

**Enumeration.** `NeffPackager::run(vector<…>&)` @`0x15307e0` asserts `modules.size() == options.vnc_nc_count` and calls `writeNeffJson(writer, modules)` once. Therefore:

```
N __kelf nodes  ==  N "sg_coreV1<i>" names  ==  N modules  ==  vnc_nc_count
```

— exactly one subgraph entry per virtual NeuronCore. A single-core NEFF has one `"sg_coreV10"` node. The consumer side (`NeffInfo::getNumNc` / `getNumNeuronCores`) counts these `sg_coreV1` entries to recover the core count. [CONFIRMED — `run`'s `.rodata` assert + the per-module write loop.]

## `attrs.kelf` → `kelf-<i>.json` (the bridge to the kelf member)

`attrs["kelf"]` is the **only** pointer from `neff.json` to the per-core kelf member:

```c
// attrs["kelf"] construction inside writeNeffJson
std::string kelf = std::to_string(i);    // core index
sub_96C8E0(&kelf, "kelf-");               // prepend "kelf-"   (.rodata 0x1c86a57)
kelf.append(".json");                     // append ".json"    (.rodata 0x1c7da3f)
// → "kelf-" ++ to_string(i) ++ ".json"
node["attrs"]["kelf"] = kelf;             // operator[]("kelf") @0x152e7df
```

The resolution chain at load time:

```
neff.json  __kelf node i :  attrs.kelf == "kelf-<i>.json"
        ── name lookup ──►  kelf-<i>.json   (the per-core engine BOM; see kelf-json page)
        ─────────────────►  {PE,Pool,Activation,SP,DVE}.bin  (the per-engine streams)
```

The runtime reads `neff.json`, and for each `sg_coreV1<i>` node opens the `attrs.kelf` member (`kelf-<i>.json`) co-located in that core's directory leaf, then loads the per-engine `.bin` streams that kelf indexes. The `__kelf` node *names* the kelf; it never carries the engine layout itself. [CONFIRMED — `"kelf-"` / `".json"` / key `"kelf"` literals at the cited addresses; the kelf member's own decode is covered by the kelf-json page (12.6, in flight).]

## What `inputs` points at — the IO `null` nodes

Before the `__kelf` nodes, `writeNeffJson` walks the `bir` Function named `"main"` (`getFunctionByName("main")`) and emits one TVM `"null"` variable node per input/output `MemoryLocation`:

```json
{ "op":"null", "name":"<tensor>", "inputs":[], "output_names":["<tensor>"],
  "attrs":{ "is_param":"0", "id":"<id>", "dtype":"<Dtype2string>",
            "shape":[d0,d1,...], "may_alias":[...], "must_alias":[...] } }
```

The `__kelf` node's `inputs` (and the graph's `arg_nodes` / `heads`) reference these `null` nodes by TVM entry-index — that is how a per-core subgraph binds to the program's IO tensors. The per-element triple layout `[node_id, index, version]` is the standard TVM convention and matches the int-vector appends in the loop, but the individual values were not traced symbol-by-symbol. [CONFIRMED — the `null`-node key/value set; INFERRED — the entry-triple numerics.] The full graph tail (`attrs.dltype/storage_id/shape` via TVM's `list_str/list_int/list_shape` tags) and the Kaena envelope (`graphs[]`, `version:"0.5"`, `target`, `revision`) are documented in [NEFF JSON Sidecars](neff-json-sidecars.md).

## Producer pseudocode (the `__kelf` emit loop)

```c
// neuronxcc::backend::NeffPackager::writeNeffJson @0x152c740
//   one __kelf subgraph-entry node per virtual core (per module)
for (int i = 0; i < mods.size(); ++i) {            // == vnc_nc_count
    json k = ordered{};
    k["name"]   = std::string("sg_coreV1") + std::to_string(i);  // 0x1c86a2e
    k["op"]     = "__kelf";                                       // 0x1c86a38
    k["attrs"]  = ordered{
        {"func_name",    "0"},                                   // 0x152e657
        {"flatten_data", std::to_string(0)},                     // 0x152e6b9
        {"num_inputs",   std::to_string(numIn)},                 // key 0x152e6d6
        {"kelf",         std::string("kelf-") + std::to_string(i) + ".json"}, // ★ key 0x152e7df
        {"num_outputs",  std::to_string(numOut)},                // key 0x152ea08
        {"tvm_op",       k["name"]} };                           // key 0x152ea7f
    k["inputs"]       = /* entry-refs into the IO null nodes */;  // key 0x152ebc1
    k["output_names"] = /* subgraph output tensor names */;       // key 0x152ebfb
    nodes.push_back(k);
}
```

A skip-guard at the top of the function returns early without writing `neff.json` if `kelf-0.json` is already a packaged member (`LOG("Skipping neff.json generation since it already exists")`) — so some NEFFs ship the kelf member with no graph member, and consumers must tolerate the absence. [CONFIRMED — the `"Skipping neff.json"` and `kelf-0.json` literals are both present in the binary.]

## Corrections

> **CORRECTION — `sgLnk` is NOT a `neff.json` key.** It is the `bir_linker` per-core **symlink directory leaf** (`nc<core>/sg<sub>/sgLnk/`), produced by `BirLinker::setupLinkDir` @`0x15d8590` → `createLinkDir` @`0x15c7910`, which symlinks each core's `{PE,Pool,Activation,SP,DVE}.{bin,json}`, `def.json`, `tensor_map.json`, and `kelf-<i>.json` into that leaf (see [bir_linker](bir-linker.md)). The `sgLnk` token *does* occur in the binary (4 times), but only in the `.rodata` region of the linker (`0x1c87d3e`, `0x1dc604a`), and **no `lea` in `writeNeffJson` targets it**. The inter-core "link tree" is the filesystem layout plus the `attrs.kelf` name references — not an edge list inside `neff.json`. [CONFIRMED — RIP-target scan of the producer body finds no reference to either `sgLnk` string.]

> **CORRECTION — there is NO `size` key on the `__kelf` node.** Earlier notes speculated a `"size"` field holding the kelf byte length; that is wrong. The kelf byte size is recorded inside `kelf-<i>.json`'s own BOM, never in `neff.json`. No `lea` in `writeNeffJson` targets a standalone `"size\0"` `.rodata` string. [CONFIRMED for the resolved RIP-targets — see [Re-verification ceiling](#re-verification-ceiling).]

A related framing fix: the per-node key set is the standard TVM `{op, name, inputs, attrs, output_names}` with `attrs = {func_name, flatten_data, num_inputs, kelf, num_outputs, tvm_op}`. There is no `"subgraph":"0"` key and no bare core-id key — the only `"0"`-valued fields are `attrs.func_name` and `attrs.flatten_data`. The schema version is `"0.5"` (the graph member), distinct from `def.json`'s `"0.6"`; do not conflate the two. [CONFIRMED.]

## Re-verification ceiling

- **CONFIRMED at the byte level:** the producer symbol `writeNeffJson` @`0x152c740` and its caller `NeffPackager::run` @`0x15307e0`; the value strings `__kelf` / `sg_coreV1` / `kelf-` / `.json` / `flatten_data` / `tvm_op` / `func_name` / `output_names` at their cited `.rodata` offsets, each reached by a resolved RIP-relative `lea` inside the producer body; the runtime construction of `sg_coreV1<i>` (the literal `sg_coreV10` is absent, proving the prepend mechanism); the skip-guard message.
- **STRONG (negatives):** `sgLnk` and a standalone `"size\0"` are not referenced by the producer. Caveat: `objdump` auto-resolved 66 of the 301 `lea` instructions in the body; the remaining ~235 RIP displacements were not individually resolved, so this is "no reference among resolved targets" rather than an exhaustive sweep. Both negatives are independently consistent with the report's full `operator[]`/value-string inventory of the function, and the `sgLnk` strings physically reside in the unrelated linker `.rodata` region.
- **INFERRED:** the per-element layout of `inputs`/`heads`/`node_row_ptr` entry-triples (`[node_id, index, version]`, TVM-standard; structurally confirmed by the int-vector appends) and which exact BIR getter feeds each `num_inputs`/`num_outputs`/`flatten_data` counter (values are `to_string` of the subgraph's IO counts).
