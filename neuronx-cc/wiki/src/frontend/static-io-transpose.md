# StaticIOTranspose and the io_transpose JSON Schema

> *All addresses, symbols, and string offsets on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel). Producer offsets are file offsets into `StaticIOTranspose.cpython-310-x86_64-linux-gnu.so`; consumer offsets are virtual addresses in `neuronxcc/starfish/bin/hlo-neff-wrapper`. Other versions will differ.*

## Abstract

When the compiler decides that an entry parameter must be laid out differently on-device than the user supplies it — a *static IO transpose* — that decision is recorded as a pair of small integer vectors per input: a `reshape` (the shape to fold the flat tensor into) and a `transpose` (the axis permutation to apply after). This page documents the on-disk wire format that carries those vectors (`io_transposes.json`), the partition-topology file that lets a transpose declared on one subgraph's input propagate to the user-facing input it aliases (`hlo_netlist.json`), and the two HLO frontend-attributes (`required_transpose`, `valid_inputs`) that the final wrapper stamps onto an `AwsNeuronNeff` custom-call so the runtime knows to apply them.

The contract spans three actors. The **producer of the map** is upstream of this page: the penguin `InsertIOTransposes` tonga pass computes the per-input reshape/transpose dict, and the `Frontend` driver job serializes one `io_transposes.json` array per subgraph directory (`sg0x/`). The **materialization stage** is the `StaticIOTranspose` driver job — an *in-process Cython* job, not a native ELF spawn — which `simplifyWeightTransform`s each spec (folding adjacent dims), in a verify pipeline physically transposes the input tensors to `transposed_inputs/value_*.npy`, re-serializes the simplified map, and symlinks the root transposed npys into every `sg*/` dir. The **consumer** is the native `hlo-neff-wrapper`, which parses the json array(s), combines them through the netlist's partition aliases, and emits the two custom-call attributes.

The format resembles a numpy `reshape`-then-`transpose` recipe stored as JSON, deduplicated and validated against a partition graph — the closest familiar analog is an XLA layout-assignment side-table, except it lives outside the HLO proto and is reconstituted into frontend-attributes only at the final wrapper stage. Both producer and consumer string-pools were recovered, and every JSON key below is anchored to a string literal in *both* sides; where the two agree the key is `CERTAIN`, and the one area neither side byte-resolves (the inner shape of `PartMap`) is flagged explicitly.

For reimplementation, the contract is:

- The `io_transposes.json` element schema: top-level **array**, each element `{input_name, reshape, transpose}`, ints capped at 6 inline dims (`SmallVector<long,6>`).
- The `hlo_netlist.json` partition schema: `PartMap` → `PartIOs` → `{Inputs, IntermediateIOs}`, plus a root `IntermediateIOs` forbidden-set.
- The producer's `simplifyWeightTransform` dim-folding rule and the verify-pipeline materialization (`transposed_inputs/`, `value_*.npy`, `inp-000.p`, the `sg*/` symlink fan-out).
- The consumer's single-vs-modular parse paths, the dedup + forbid-intermediate combine rule, alias propagation, and the two output frontend-attributes on `AwsNeuronNeff`.

| | |
|---|---|
| **Producer (Job)** | `StaticIOTranspose.cpython-310-x86_64-linux-gnu.so` (643,456 B, **not stripped, debug_info**, BuildID `35bf7e31a383f778`; source `neuronxcc/driver/jobs/StaticIOTranspose.py`) |
| **Consumer (ELF)** | `neuronxcc/starfish/bin/hlo-neff-wrapper` (BuildID `946053c80bdfb44a`, ~226 MB) |
| **Upstream map source** | penguin `InsertIOTransposes` tonga pass + `Frontend` job (writes the json array) |
| **Wire artifacts** | `io_transposes.json` (one array per `sg0x/`), `hlo_netlist.json` (one, top-level) |
| **In-memory map type** | `llvm::StringMap<std::pair<SmallVector<long,6>, SmallVector<long,6>>, MallocAllocator>` (input_name → (reshape, transpose)) |
| **Output attributes** | `valid_inputs` + `required_transpose` on a `kCustomCall` to target `"AwsNeuronNeff"` |
| **JSON library (consumer)** | `nlohmann::json_abi_v3_11_3`, `std::map`-backed (key order is sorted, not insertion) |

> **NOTE —** no sample `io_transposes.json` or `hlo_netlist.json` ships in the wheel (`fd` finds none). The schema below is reconstructed from the producer's `json.dump` call and const-pool, and from the consumer's `nlohmann` accessor sequence and error strings. The two sides agree on every key name, which is the strongest available evidence absent a captured instance.

---

## io_transposes.json — element schema

### Purpose

`io_transposes.json` is a flat **JSON array**. Each element declares, for one entry parameter, the reshape+permute the runtime must apply before feeding that input to the device. The consumer iterates the array and `try_emplace`s one StringMap entry per element keyed by `input_name`; the producer `json.dump`s the same list back out after simplification.

### Schema

Top-level type: **JSON array of objects**. Each element:

| Key (verbatim) | Type | Meaning | Required | Anchor / Confidence |
|---|---|---|---|---|
| `input_name` | string | Name of the entry parameter this transpose applies to; becomes the StringMap key. Must be a string. | **yes** | producer `__pyx_n_u_input_name`; consumer string `input_name` + `input_name not found in json`. **CERTAIN** |
| `reshape` | array&lt;int&gt; | Target shape to fold the flat tensor into **before** the permutation. Stored as `SmallVector<long,6>`. | **yes** | producer docstring `'reshape': […]`; consumer string `reshape` + `reshape not found in json`. **CERTAIN** |
| `transpose` | array&lt;int&gt; | Axis permutation applied **after** the reshape (numpy `transpose` axis order). Stored as `SmallVector<long,6>`. | **yes** | producer docstring `'transpose': […]`; consumer string `transpose` + `transpose not found in json`. **CERTAIN** |

```json
[
  { "input_name": "p0", "reshape": [3, 2048, 2, 128], "transpose": [0, 3, 2, 1] },
  { "input_name": "weight_7", "reshape": [768, 768], "transpose": [1, 0] }
]
```

The ints are JSON **numbers**, not strings — the consumer reads them with a `get<long>` whose type-tag check rejects non-numeric (`type must be number, but is `). Values are signed `long` in storage; in practice they are non-negative dim sizes (`reshape`) and axis indices (`transpose`).

> **QUIRK —** the top-level is an **array, not a dict keyed by `input_name`**. The key lives *inside* each element. A reimplementation that serializes `{ "p0": {reshape, transpose}, … }` will fail the consumer's array-iteration immediately — `parseIoTransposeFromJson` walks the json with an array iterator and does one `try_emplace(elem["input_name"])` per step.

### The 6-dim inline cap

Both `reshape` and `transpose` land in `SmallVector<long,6>` — six dims inline, spilling to heap beyond that. This cap is confirmed directly from the demangled consumer signature: the in-memory map is `StringMap<pair<SmallVector<long,6>, SmallVector<long,6>>, MallocAllocator>`. The producer's entire `simplifyWeightTransform` step exists to *keep the dim count at or below this cap* by folding adjacent dims (see below).

> **NOTE —** the cap is a soft optimization target, not a hard limit. `SmallVector<long,6>` tolerates `>6` elements by spilling to heap, so a 7-dim spec parses correctly. But `simplifyWeightTransform` is designed to avoid that, and the wider compiler's layout analysis is "much simpler with less dimensions" (producer docstring). Treat 6 as the intended ceiling.

---

## hlo_netlist.json — partition schema

### Purpose

`hlo_netlist.json` describes the partition/subgraph topology of the modular flow. The wrapper reads it to (a) build a forbidden set of cross-partition (intermediate) tensor names that may *not* carry a user-facing transpose, and (b) build an alias map so a transpose declared on one partition's input is propagated onto the user-facing input it corresponds to. It is consumed by `getIntermediateTensorNames` (`0x1e47a20`) and `createPartitionInputAliasMap` (`0x1e49920`) via `nlohmann::json::operator[]`.

### Schema

The json is `nlohmann::json_abi_v3_11_3` instantiated over `std::map` (ordered). The key paths recovered from the accessor sequence:

| Path (verbatim keys) | Type | Meaning | Anchor / Confidence |
|---|---|---|---|
| `PartMap` | array/object of partitions | Top-level partition map; iterated to build the alias map. | string `PartMap` @0x275930; read in `createPartitionInputAliasMap` `0x1e4992f`. **CERTAIN (key); MEDIUM (element shape — see GAP)** |
| `PartMap[*].PartIOs` | object | Per-partition IO descriptor. | string `PartIOs` @0x2438a7; `0x1e499a5`. **CERTAIN** |
| `PartMap[*].PartIOs.Inputs` | array&lt;string&gt; | Tensor names that are this partition's inputs (entry/user-facing on this partition). | string `Inputs` @0x25e58b; `0x1e49c8b`/`0x1e49ec5`. **CERTAIN** |
| `PartMap[*].PartIOs.IntermediateIOs` | array&lt;string&gt; | Tensor names that are intermediate (cross-partition edges, not user-facing). | string `IntermediateIOs` @0x2796c2; `0x1e499d4`. **CERTAIN** |
| (root) `IntermediateIOs` | array&lt;string&gt; | Global intermediate-tensor list; read by `getIntermediateTensorNames`, becomes the combine forbidden-set. | same string @0x2796c2; `0x1e47af3`/`b44`/`b5f`. **CERTAIN** |

```json
{
  "PartMap": [
    {
      "PartIOs": {
        "Inputs":          ["p0", "p1"],
        "IntermediateIOs": ["edge_3_to_5"]
      }
    }
  ],
  "IntermediateIOs": ["edge_3_to_5"]
}
```

All leaf values in `Inputs`/`IntermediateIOs` are plain tensor-name **strings** (extracted into a StringMap); the consumer's type guard is `type must be string, but is `. The two semantic uses:

- **Forbidden set.** `getIntermediateTensorNames(netlist)` collects the root `IntermediateIOs` into a name set. In `combineIoTransposes`, an io_transpose whose `input_name` is in that set is rejected (`NCC_IIOT002`, `Forbidden io_transpose found for input: `) — a transpose may not be requested on an internal cross-partition edge, only on a real entry parameter.
- **Alias map.** `createPartitionInputAliasMap(netlist)` walks `PartMap[*].PartIOs.{Inputs,IntermediateIOs}` to build a StringMap of partition-input aliases (which subgraph input corresponds to which user-facing input). `propagateThroughPartitionAliases` then copies a required transpose from the partition-internal input name onto the aliased user-facing input name in the accumulated map.

> **GAP — `PartMap` element shape not byte-resolved.** Only the keys `PartMap`/`PartIOs`/`Inputs`/`IntermediateIOs` are string-anchored. Whether `PartMap` is a JSON object keyed by partition-id or an array indexed by id, and whether each partition carries sibling fields (id, neff name, output list) beyond `PartIOs`, were **not** resolved — `createPartitionInputAliasMap` (3352 B) and `generateNewHloModule` (5089 B) exceeded the decompiler's per-function emit limit, leaving only prologues. The example above shows `PartMap` as an array because that is the simplest form consistent with the `operator[]`-then-iterate sequence, but **MEDIUM confidence** — a reimplementer should accept either form and key off `PartIOs`. (D-A12 §8 GAP 5.)

> **GAP — propagation direction inferred.** That `propagateThroughPartitionAliases` reads the alias map and rewrites the io_transpose StringMap is `HIGH`; the exact rule (intermediate→input vs input→input) is inferred from the `Inputs`+`IntermediateIOs` semantics plus the forbidden-set use, not line-traced. **MEDIUM.** (D-A12 §8 GAP 6.)

---

## Producer — StaticIOTranspose job

### Purpose

`StaticIOTranspose(Job)` is the in-process Cython driver job that *materializes* the io_transpose map. It does **not** compute the reshape/transpose — that map arrives already written (by `InsertIOTransposes` + `Frontend`). Its three jobs are: simplify each spec, physically dump transposed tensors (verify pipeline only), and fan the transposed npys out into every subgraph dir.

> **CORRECTION (D-A06) —** an earlier pass listed `StaticIOTranspose` as a native ELF spawned as a subprocess. It is in-process Cython: `StaticIOTranspose.cpython-310-…so` with `PyInit_StaticIOTranspose`, class `StaticIOTranspose(Job)`, methods compiled to `__pyx_pf_*` bodies. No subprocess is spawned by this stage.

### Entry Point

```text
StaticIOTranspose.run(self, in_states)                 ── 0xd9f0  per-state driver
  ├─ os.path.exists("io_transposes.json")              ── gate
  ├─ os.path.exists("inp-000.p")                        ── gate (value tensors present)
  ├─ json.load / np.load(pickle)                        ── read map + tensors
  ├─ dumpTransposedFiles(io_transposes, inp_dict, verify) ── 0x177c0
  │     └─ simplifyWeightTransform(io_transpose, orig_shape) ── 0x10490
  └─ link_root_npys_to_sg()                             ── 0x14c10  fan npys into sg dirs
```

### Algorithm — simplifyWeightTransform

```c
function simplifyWeightTransform(io_transpose, orig_shape):   // 0x10490
    // Fold dims that are adjacent in `transpose` AND were originally adjacent
    // in orig_shape, to reduce the dim count toward the 6-dim cap.
    reshape   = io_transpose['reshape']        // e.g. [3,16,128,2,2,64]
    transpose = io_transpose['transpose']      // e.g. [0,4,5,3,1,2]
    new_reshape, new_transpose = fold_consecutive(reshape, transpose, orig_shape)
    // guard: never merge dims that were not originally contiguous in orig_shape
    if inconsistent_merge:
        raise AssertionError("Illegal static io reshape from {} to {}"   // 0x1f160
                             .format(reshape, new_reshape))
    return { 'reshape': new_reshape, 'transpose': new_transpose }
```

The dim-folding rule is stated verbatim in the recovered docstring:

```text
orig_shape    = [6144, 256]
io_transpose  = { 'reshape': [3,16,128,2,2,64], 'transpose': [0,4,5,3,1,2] }   # before
# 16,128 and 2,64 are consecutive in the transpose -> fold:
io_transpose  = { 'reshape': [3,2048,2,128],     'transpose': [0,3,2,1]     }   # after
# "We look at the original shape to make sure we do not combine dimensions
#  that were not originally together."
```

> **GOTCHA —** the fold is gated on *original* adjacency, not just transpose adjacency. Two dims may be consecutive in the permuted order yet must NOT be merged if they were not contiguous in `orig_shape` — doing so silently corrupts the layout. The docstring closes with `FIXME: Will have to revisit this logic when we support static padding.`, so padded inputs are a known edge the fold does not yet handle.

### Algorithm — dumpTransposedFiles

```c
function dumpTransposedFiles(io_transposes, inp_dict, verify):   // 0x177c0
    // docstring: "Dumps transposed npy and pickle files into transposed_inputs
    //  directory. Create symlinks to the originals for tensors that were not
    //  transposed. We only need to dump npy files if we are in a verify pipeline."
    os.mkdir("transposed_inputs")                                  // transposed_inputs_dir
    for input_name, io_transpose in io_transposes:
        tensor = inp_dict[input_name]
        io_transpose = simplifyWeightTransform(io_transpose, tensor.shape)   // 0x10490
        if verify:                                                 // __pyx_n_u_verify
            t = np.ascontiguousarray(
                  np.transpose(np.reshape(tensor, io_transpose['reshape']),
                               io_transpose['transpose']))
            np.save("transposed_inputs/value_{}.npy".format(input_name), t)  // {}/value_{}.npy
    json.dump(io_transposes, open("io_transposes.json", "w"), ...)  // simplified map back out
    pickle.dump(inp_dict, open("{}/inp-000.p".format(...), "wb"))   // {}/inp-000.p
    for untransposed input:                                         // not in io_transposes
        os.symlink(original_npy, "{}/{}".format(cwd, name))         // {}/{}  join template
```

Confirmed strings: docstring lines (verbatim above), `ascontiguousarray`, `value_{}.npy` (`__pyx_kp_u_value__npy`), `inp-000.p` / `{}/inp-000.p`, `transposed_inputs`, `mkdir`, `symlink`, `verify`, `original_npy`. The npy materialization is **verify-pipeline only**; outside verify, the stage still re-serializes the simplified map and does the symlinks.

### Algorithm — link_root_npys_to_sg

```c
function link_root_npys_to_sg():                          // 0x14c10
    for npy in glob("transposed_inputs/*.npy"):           // *.npy  (npy_glob_str)
        for sgDir in glob("sg*/") + glob("nc*/sg*/"):     // __pyx_k_sg, __pyx_k_nc_sg
            dst = os.path.join(sgDir, os.path.basename(npy))   // {}/{}
            if os.path.lexists(dst): os.remove(dst)        // drop stale link
            os.symlink(os.path.realpath(npy), dst)
```

Every discovered subgraph dir (`sg*/` at top level, and `nc*/sg*/` under per-NeuronCore dirs) receives a symlink to each root transposed npy, so all subgraphs see the same physically-transposed tensors. A `*.npy.meta` sidecar (`__pyx_k_npy_meta` = `*.npy.meta`) pairs with each npy and is fanned out the same way.

### Function Map — producer

| Method | File offset | Role | Confidence |
|---|---|---|---|
| `StaticIOTranspose.__init__(self, parent_command)` | `0xc580` | trivial `Job` ctor; refs `parent_command` | CERTAIN |
| `StaticIOTranspose.run(self, in_states)` | `0xd9f0` | per-state driver; existence gates → load → `dumpTransposedFiles` → `link_root_npys_to_sg` | HIGH |
| `simplifyWeightTransform(io_transpose, orig_shape)` | `0x10490` | fold consecutive dims; raise `Illegal static io reshape from {} to {}` | HIGH |
| `dumpTransposedFiles(io_transposes, inp_dict, verify)` | `0x177c0` | write simplified map, dump/transpose npys (verify), pickle, symlink untransposed | HIGH |
| `link_root_npys_to_sg()` | `0x14c10` | symlink root `transposed_inputs/*.npy` into each `sg*/` / `nc*/sg*/` | HIGH |

---

## Consumer — hlo-neff-wrapper

### Purpose

`hlo-neff-wrapper` consumes the json array(s) plus the netlist and re-emits the input HLO as a single `AwsNeuronNeff` custom-call carrying the `required_transpose`/`valid_inputs` frontend-attributes. It has two entry paths — single and modular — that converge on the same `parseIoTransposeFromJson` element parser and `propagateThroughPartitionAliases` finisher.

### Entry Point

```text
single:  parseIoTranspose(StringRef)                       ── 0x1e57ec0  (815 B)
           openFile -> parseIoTransposeFromJson -> StringMap
           openFile(netlist) -> propagateThroughPartitionAliases

modular: parseModularIoTransposes()                        ── 0x1e58350  (4360 B)
           fileStreamToJson(netlist) -> getIntermediateTensorNames -> forbidden SmallSet<StringRef,8>
           for each "sg0x/io_transposes.json":   // log "Processing io transpose file: "
             fileStreamToJson -> parseIoTransposeFromJson(local)
             combineIoTransposes(acc, local, forbidden)     ── 0x1e50760  (3148 B)
           [>=2 files && no --netlist] -> NCC_IHNW001
           fileStreamToJson(netlist) -> propagateThroughPartitionAliases(netlist, acc)  ── 0x1e4a650

both ->  generateNewHloModule(hlo, neff, acc)              ── 0x1e5c990  (5089 B)
```

### Algorithm — parseIoTransposeFromJson

```c
function parseIoTransposeFromJson(json):                   // hilo::  0x1e4f450 (1827 B)
    for elem in json:                                      // nlohmann array iterator
        name = elem["input_name"]                          // else "input_name not found in json"
        reshape   = elem["reshape"].get<vector<long>>()    // else "reshape not found in json"
        transpose = elem["transpose"].get<vector<long>>()  // else "transpose not found in json"
        map.try_emplace(name, {SmallVector<long,6>(reshape),
                               SmallVector<long,6>(transpose)})
    return map  // StringMap<pair<SmallVector<long,6>,SmallVector<long,6>>, MallocAllocator>
```

### Algorithm — combineIoTransposes

```c
function combineIoTransposes(acc, local, forbidden):       // 0x1e50760 (3148 B)
    for (name, spec) in local:
        if name in acc:                                    // dedup
            error("Duplicate io_transpose found for input: " + name)   // NCC_IIOT001
        if name in forbidden:                              // cross-partition edge
            error("Forbidden io_transpose found for input: " + name)   // NCC_IIOT002
        acc.try_emplace(name, spec)
```

The `forbidden` set is `SmallSet<StringRef,8>` built by `getIntermediateTensorNames` from the netlist root `IntermediateIOs`. Two per-sg files declaring the same `input_name` is a hard error; so is any sg declaring a transpose on an internal edge.

### Algorithm — generateNewHloModule (output emission)

```c
function generateNewHloModule(module, neff, ioT):          // hilo::  0x1e5c990 (5089 B)
    processParameters(builder, ..., ioT, module)           // 0x1e5b1d0  reshape/shape cross-check
        // per param: declared reshape product must equal actual element count
        //   else "Reshape size mismatch for input: "       NCC_IHNW003
    call = builder.CreateCustomCall(... "AwsNeuronNeff" ...) // target string @0x219422
    attrs = FrontendAttributes()                            // Map<string,string>
    attrs["valid_inputs"]       = serialize(ioT.keys())     // string @0x20d2ab
    attrs["required_transpose"] = serialize(ioT.values())   // string @0x26237f
    call->set_frontend_attributes(attrs)
    // multi-output: CreateGetTupleElement + CreateTuple + MakeValidatedTupleShape
    // constant inputs: CreateConstant
```

### Output schema — frontend-attributes

`generateNewHloModule` turns the `input_name → (reshape, transpose)` StringMap into an `xla::FrontendAttributes` `Map<string,string>` set on the `AwsNeuronNeff` `kCustomCall`:

| Attribute (verbatim) | Built from | Anchor / Confidence |
|---|---|---|
| `valid_inputs` | the set of entry-param `input_name`s that carry an io_transpose (StringMap key set) | string @0x20d2ab, refby `generateNewHloModule`; emit `@0x1e5c9af`. **CERTAIN** |
| `required_transpose` | the reshape+transpose specs (serialized SmallVector pairs) for those inputs | string @0x26237f, refby `generateNewHloModule`; emit `@0x1e5c9ee`. **CERTAIN** |

CustomCall target literal `"AwsNeuronNeff"` is string @0x219422, also referenced by `generateNewHloModule`. If the StringMap is empty and there are no zero-sized params, the wrapper prints `There are no io transposes nor zero-sized parameters. Output will not be produced.` and emits nothing.

> **NOTE —** a *third*, adjacent frontend-attribute exists: `neff_input_names` (string @0x25271e), read by `getFrontendName`. It is not part of the io_transpose schema — it carries the parameter-name list for the neff and is mentioned here only so a reimplementer who sees it on the same custom-call does not mistake it for an io_transpose field.

### Function Map — consumer

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `parseIoTransposeFromJson` (`hilo::`) | `0x1e4f450` | 1827 | array-iterate; per elem read `input_name`/`reshape`/`transpose` → `try_emplace` | CERTAIN |
| `parseIoTranspose(StringRef)` | `0x1e57ec0` | 815 | single path: open → parse → propagate | HIGH |
| `parseModularIoTransposes()` | `0x1e58350` | 4360 | modular path: netlist → forbidden set; per-sg parse+combine; propagate | CERTAIN |
| `combineIoTransposes` | `0x1e50760` | 3148 | merge per-sg map; dedup (`NCC_IIOT001`) + forbid-intermediate (`NCC_IIOT002`) | CERTAIN |
| `getIntermediateTensorNames(json)` | `0x1e47a20` | 830 | root `IntermediateIOs` → forbidden name set | CERTAIN |
| `createPartitionInputAliasMap(json)` | `0x1e49920` | 3352 | `PartMap`→`PartIOs`→`Inputs`/`IntermediateIOs` → alias StringMap | CERTAIN |
| `propagateThroughPartitionAliases(json, map)` (`hilo::`) | `0x1e4a650` | 995 | propagate transpose across partition input-aliases | HIGH |
| `generateNewHloModule(...)` (`hilo::`) | `0x1e5c990` | 5089 | `CreateCustomCall("AwsNeuronNeff")` + `set_frontend_attributes` | HIGH |
| `processParameters(...)` | `0x1e5b1d0` | 4950 | per-param reshape/shape cross-check; `NCC_IHNW003` | HIGH |
| `generateNewHlo(hlo,neff,output,netlist)` (`hilo::`) | `0x1e5df00` | 3051 | top-level orchestrator | HIGH |

### Diagnostics crosswalk

| Code | Site | Message (verbatim) | Confidence |
|---|---|---|---|
| `NCC_IHNW001` | `parseModularIoTransposes` | `A netlist file is required at --netlist if multiple modular io_transposes are present` | CERTAIN |
| `NCC_IHNW002` | `parseModularIoTransposes` | `Failed to open io_transpose file: ` | CERTAIN |
| `NCC_IHNW003` | `processParameters` | `Reshape size mismatch for input: ` | HIGH |
| `NCC_IIOT001` | `combineIoTransposes` | `Duplicate io_transpose found for input: ` | CERTAIN |
| `NCC_IIOT002` | `combineIoTransposes` | `Forbidden io_transpose found for input: ` | CERTAIN |
| (producer) | `simplifyWeightTransform` | `Illegal static io reshape from {} to {}` (AssertionError) | HIGH |

---

## The sg0x directory convention

A *subgraph* (partition) of the modular flow lives in a directory named `sg<NN>` (zero-padded), nested under per-NeuronCore dirs `nc<N>`. The layout the wrapper expects is `nc*/sg*/io_transposes.json` (one io_transpose array per subgraph) plus a single top-level `hlo_netlist.json` linking them.

- **Producer globs** (StaticIOTranspose const-pool): `nc*/sg*/` (`__pyx_k_nc_sg` @0x1f7a0), `sg*/` (`__pyx_k_sg` @0x1f87f), `*.npy` (@0x1f859), `*.npy.meta` (@0x1f680), `{}/value_{}.npy` (@0x1f5a0), `{}/inp-000.p` (@0x1f610), `{}/{}` join template (@0x1f88a).
- **Consumer cl::opt help**: `modular-io-transposes` → `List of all the io_transpose.json files produced by modular flow. Can be found in the sg0x directories`; `netlist` (default `hlo_netlist.json`) → `hlo_netlist file produced by modular flow`. `parseModularIoTransposes` logs `Processing io transpose file: ` per file.
- The `--netlist` is what stitches the per-`sg0x` transposes together: `PartMap`/`PartIOs`/`Inputs`/`IntermediateIOs` say which subgraph input aliases which user-facing input, so a transpose declared in one subgraph's `io_transposes.json` is `propagateThroughPartitionAliases`'d to the model-level input. Hence `NCC_IHNW001` fires when ≥2 modular files are given without a netlist — without the topology, the per-sg transposes cannot be disambiguated.

---

## End-to-end flow

```text
penguin InsertIOTransposes pass  ──►  io_transpose map {input_name -> {reshape, transpose}}   [per partition]
        │
   Frontend job  writes  sg0x/io_transposes.json (JSON array)  +  hlo_netlist.json (PartMap/IntermediateIOs)
        │
   StaticIOTranspose job (in-process Cython):
     run -> simplifyWeightTransform (fold dims)
         -> dumpTransposedFiles (verify: transposed_inputs/value_*.npy; re-dump simplified json; symlink untransposed)
         -> link_root_npys_to_sg (fan npys into every sg*/ and nc*/sg*/)
        │
   hlo-neff-wrapper (native ELF; NeffWrapper job -> subprocess):
     --hlo --neff --output --netlist hlo_netlist.json
     ( --io_transposes <f>   XOR   --modular-io-transposes "sg00/io_transposes.json sg01/… " )
        │
     single  -> parseIoTranspose -> parseIoTransposeFromJson -> propagateThroughPartitionAliases
     modular -> parseModularIoTransposes -> getIntermediateTensorNames(forbidden)
                                          -> per-sg parseIoTransposeFromJson + combineIoTransposes(dedup/forbid)
                                          -> propagateThroughPartitionAliases
        │
     generateNewHloModule:
        processParameters (reshape/shape check -> NCC_IHNW003)
        CreateCustomCall("AwsNeuronNeff").set_frontend_attributes({ valid_inputs, required_transpose })
        -> GetTupleElement/Tuple/Constant wrap -> output HLO
```

The arc is: penguin computes the map → `Frontend` serializes the array → `StaticIOTranspose` simplifies + materializes → `hlo-neff-wrapper` re-imports, combines across partitions, and bakes the result into two HLO frontend-attributes on the `AwsNeuronNeff` custom-call. The JSON is the durable hand-off between the Python driver world and the native wrapper; the frontend-attributes are the durable hand-off into the runtime.

---

## Related Components

| Name | Relationship |
|---|---|
| penguin `InsertIOTransposes` (tonga pass) | upstream producer of the io_transpose map this page serializes |
| `Frontend` job | first writer of `sg0x/io_transposes.json` (the JSON array) and `hlo_netlist.json` |
| `NeffWrapper` job | driver job that spawns `hlo-neff-wrapper` as a subprocess with `--netlist`/`--io_transposes`/`--modular-io-transposes` |
| IOLayoutNormalization (Part 4) | the layout-analysis stage whose decisions the transposes encode |

## Cross-References

- [CompileCommand Pipeline](compilecommand-pipeline.md) — job ordering: where `Frontend`, `StaticIOTranspose`, and `NeffWrapper` sit
- [Front-End Pipeline](../front/pipeline.md) — driver/job model these jobs plug into
- [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — the on-chip layout the IO transpose normalizes toward
