# MetricStore and the Cost-Stats Reference

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical Cython rebuilds, BuildID `92b4d331a42d7e80bb839e03218d2b9b0c23c346`). Everything here lives in `neuronxcc/starfish/lib/libwalrus.so` (64,973,024 B, `.symtab` stripped, full `.dynsym` retained). For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset — every string VA below was confirmed by `grep -abo` over the binary + base. The `0x5e9020`–`0x62d650` band is `.plt` thunks: a function that resolves to a low `0x5e…/0x61…` address is the thunk frame, and the real body lives high (e.g. `readNestedMetricStoreFromFile` is `0x8586a0`, not its `0x61a7e0` thunk alias). `.data` carries a +0x400000 VMA↔fileoffset delta. Other wheels differ — treat every address as version-pinned.*

## Abstract

The walrus backend keeps **one** quantitative counter store: a process-global
singleton named `neuronxcc::metricslibrary::MetricStore`. Every backend pass that
wants to publish a number — instruction counts, DMA byte volumes, DRAM
high-water-marks, and most importantly the perf-sim cycle estimate
`PostSchedEstLatency` — calls one writer, `backend::addMetric` (`0x1742020`),
which upserts into a `tbb::detail::d2::concurrent_hash_map`. The map's value type
is a four-alternative `std::variant<float, unsigned int, long, unsigned long>` and
its key is a three-string `std::tuple` — the **TripleHash** — compared by
`neuronxcc::metricslibrary::TripleHashCompare`. This store is the cross-stage and
cross-process reward surface the layout/fusion autotuners read back: lower
`PostSchedEstLatency` is a better schedule.

The shape is deliberately simple and worth pinning before any reimplementation.
The store does **no** in-memory aggregation: `addMetric` is an unconditional
overwrite of the value slot; the `MetricAggregationType` (Sum/Average/Custom/None)
is only baked into the key *as a string*, so the chosen fold is the *caller's* or
the *reader's* responsibility. The on-disk surface is nlohmann-JSON
(`json_abi_v3_11_3`), and there are three live JSON files plus one static
descriptor: `global_metric_store.json` is the full nested dump written out at
packaging time; `tensorizer_metric_store.json` is read *back* by the backend
driver to seed the store from an earlier stage; `hlo_metrics.json` is a frontend
per-HLO file relayed verbatim into the NEFF as `hlo_stats.json`. Round-tripping a
metric through the JSON reader is *lossy*: every value is narrowed to the variant's
`float` alternative and the aggregation is reset to the literal `"None"`.

This page documents the container types, the TripleHash key recipe and its hash,
the 54-entry `BackendMetricType` enum (decoded firsthand from the dispatcher jump
table — index 42 is `PostSchedEstLatency`), the serialize/deserialize JSON schema,
the four sinks, and the consolidated cost-model constant table that travels with
the store. The perf-sim *algorithm* that produces metric 42 (the `bir::Hwm`
per-instruction latency oracle, the `get_overall_end` critical path, the
`get_modular_flow_runs` multiplier) is a sibling deliverable (8.45
`perfsim-cost-model`, in-flight) and is referenced — not re-derived — here.

For reimplementation, the contract is:

- The container: a TBB `concurrent_hash_map<tuple<3×string>, variant<float,uint32,int64,uint64>, TripleHashCompare>` behind an inlined Meyers singleton.
- The TripleHash key: the exact three members (aggregation, name, metric-type-string), and the `llvm::hash_combine` order `(type, name, agg)` that on-disk keys interoperate with.
- The 54-entry `BackendMetricType` enum, stringified with the `backend::` prefix, index 42 = `PostSchedEstLatency`.
- The JSON schema (flat vs 3-level nested), the `"Count"`-based Average fold, and the *lossy* read-back (float-narrowing + agg→`"None"`).
- The four sinks and which direction each flows (out / in / relay / descriptor).

| | |
|---|---|
| **Singleton** | `MetricStore::getGlobalMetricStore()` — fully inlined; guard `0x3dfd900`, `instance_` `0x3dfd920` |
| **Writer** | `backend::addMetric` `0x1742020`; compile-time sibling `addCompileTimeMetric` `0x1740d70` |
| **Key→string** | `backend::BackendMetricType2String` `0x173b490` (dispatcher), jump table `0x1e04ef0` (54 × int32) |
| **Value type** | `std::variant<float, unsigned int, long, unsigned long>` (alt 0..3) |
| **Key type** | `std::tuple<std::string, std::string, std::string>` / `TripleHashCompare` |
| **Serialize** | `metricslibrary::serialize[abi:cxx11](bool nested)` `0x1537b80` → nlohmann `json_abi_v3_11_3` |
| **Read-back** | `MetricStore::readNestedMetricStoreFromFile(istream&)` `0x8586a0` → `deserializeNested` `0x84eef0` |
| **NEFF descriptor** | `NeffFileWriter::addMetricMetadata()` `0x153fe40` (BOM tag `metric-metadata` @ VA `0x1dd78a0`) |

---

## The Container

### Purpose

`MetricStore` is a thin wrapper whose object size is dominated by one TBB
concurrent hash map. It is a process-global singleton so that in a multi-NeuronCore
compile every pass writes to one shared table; the per-core separation is carried
by the *name* string in the key (almost always `bir::Module::getName()`), not by
separate store instances.

### Value type — the variant

The value type is confirmed from the mangled template instantiation in `.dynsym`
(`nm -DC`): `std::variant<float, unsigned int, long, unsigned long>`. The
`addMetric` mangled name encodes it as `St7variantIJfjlmEE` (`f`=float, `j`=uint,
`l`=long, `m`=ulong), and the four `serialize` visitor lambdas
(`__gen_vtable_impl … integer_sequence<0,1,2,3>` at `0x15318b0`–`0x15319c0`) confirm
the arity is exactly four.

| Variant index | C++ type | Width | JSON type on dump |
|---|---|---|---|
| 0 | `float` | 4 B | real |
| 1 | `unsigned int` | 4 B (uint32) | integer |
| 2 | `long` | 8 B (int64) | integer |
| 3 | `unsigned long` | 8 B (uint64) | integer |

In the libstdc++ layout the variant is an 8-byte payload plus a 1-byte
active-alternative index. Inside a TBB bucket node the payload lands at `node+0x70`
(qword index 14) and the index byte at `node+0x78` (byte 120). The store side of
`addMetric` (`0x1742020`, sidecar lines 967–968) is exactly:

```c
*((uint64_t*)node + 14) = payload;     // node+0x70 — the 8-byte variant value
*((uint8_t *)node + 120) = index;      // node+0x78 — the active-alternative byte
```

> **GOTCHA —** this is an *unconditional overwrite* once the `(type,name,agg)` key
> is found or inserted. There is no in-place Sum/Average. A reimplementation that
> tries to "add into" the existing slot is wrong: the running fold (where one
> exists) happens later, in `serialize`'s `"Count"` path or in the consumer, not
> here. The `MetricAggregationType` argument only ever becomes a *string in the
> key*.

### Key type — the TripleHash

The key is `std::tuple<std::string, std::string, std::string>`, compared by
`neuronxcc::metricslibrary::TripleHashCompare` and hashed with
`llvm::hash_combine<std::string,std::string,std::string>`. The three members and
the *order in which they are combined* are confirmed from the `addMetric` body
(`0x1742020`, sidecar): line 229 is

```c
hash = llvm::hash_combine<string,string,string>(s1 /*=TYPE*/,
                                                name /*=NAME*/,
                                                agg  /*=AGG*/);
```

where `s1 = BackendMetricType2String(type)`, `name` is the producer's name
argument, and `agg` is the aggregation literal. The three tuple slots in the
bucket node, however, are stored in a *different* order than the hash-combine
argument order:

| Tuple slot | Bucket-node offset | Member | Source |
|---|---|---|---|
| `#0` | `node+0x10` | **aggregation** string | `MetricAggregationType` → `"Sum"`/`"Average"`/`"Custom"`/`"None"` |
| `#1` | `node+0x30` | **name** string | the producer's identifier (usually `bir::Module::getName()`) |
| `#2` | `node+0x50` | **metric-type** string | `BackendMetricType2String(type)` = `"backend::<Type>"` |

So the conceptual key is **`(aggregation, name, metric-type)`** while the hash is
combined as **`(type, name, agg)`**. A reimplementation must reproduce the combine
order, not the slot order, to land in the same bucket as an on-disk key.

> **QUIRK —** the aggregation is part of the key, which means the *same* counter
> written once with `Sum` and once with `Average` lands in *two* buckets, not one.
> This matters only at write time; the JSON read-back collapses the distinction by
> forcing the aggregation to `"None"` (see [Read-back](#read-back--lossy-merge)).

### The storage map

```text
tbb::detail::d2::concurrent_hash_map<
    std::tuple<std::string, std::string, std::string>,        // key
    std::variant<float, unsigned int, long, unsigned long>,   // value
    neuronxcc::metricslibrary::TripleHashCompare,             // HashCompare
    tbb::detail::d1::tbb_allocator<std::pair<const tuple, variant>> >
```

This is an Intel TBB concurrent hash map: thread-safe, one `spin_rw_mutex`
(`rw_scoped_lock`) per accessor, split-ordered bucket addressing. The
`_mm_pause`/`InterlockedCompareExchange` spin loops in `addMetric` are the TBB
accessor lock acquisition; the `rw_scoped_lock<spin_rw_mutex>::release` call right
after the value store (sidecar tail) drops it. `rehash_bucket`, `rehash`,
`internal_copy`, and `clear` are all instantiated for this exact type and present
in `.dynsym` (e.g. `internal_copy` `0x10e1760`, `clear` `0x10e0080`,
`rehash_bucket` `0x84eaa0`).

### The singleton

`MetricStore::getGlobalMetricStore()` is a Meyers singleton over a function-local
static. There is **no out-of-line symbol** for it — only the guard variable
`0x3dfd900` and the `instance_` storage `0x3dfd920` survive in `.dynsym` — which
confirms it is fully *inlined* into every caller. The first call inside `addMetric`
zero-initializes the embedded `concurrent_hash_map` (segment-table header + 64
segment-pointer slots) and registers `~MetricStore` (`0x81b7a0`) via
`__cxa_atexit`. The destructor just tears the map down.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `backend::addMetric` | `0x1742020` | The one writer: build TripleHash, hash, upsert | CERTAIN |
| `backend::addCompileTimeMetric` | `0x1740d70` | Sibling writer into the same store | CERTAIN |
| `backend::BackendMetricType2String` | `0x173b490` | 54-arm enum → `"backend::<Type>"` | CERTAIN |
| `metricslibrary::serialize[cxx11](bool)` | `0x1537b80` | Snapshot store → nlohmann json (flat/nested) | CERTAIN |
| `MetricStore::readNestedMetricStoreFromFile` | `0x8586a0` | istream → json → `deserializeNested` | CERTAIN |
| `metricslibrary::deserializeNested` | `0x84eef0` | json leaf → tuple key → upsert into global store | CERTAIN |
| `metricslibrary::getMetricMetadataName` | `0x10dff40` | Store iterator → `pair(name, type)` (drops agg) | HIGH |
| `MetricStore::~MetricStore` | `0x81b7a0` | Map teardown | CERTAIN |
| `NeffFileWriter::addMetricMetadata` | `0x153fe40` | Walk store → NEFF `metric-metadata` BOM entry | HIGH |

---

## The `BackendMetricType` Enum

### Purpose

`BackendMetricType` is the master inventory of every quantitative counter the
walrus backend can publish. Its `…2String` form is the metric-type member of the
TripleHash, so these names are literally the inner JSON keys on disk.

### Encoding

`BackendMetricType2String` (`0x173b490`) is a jump-table dispatcher:
`cmp $0x35,%ebx` (`0x35` = 53 → 54 arms, indices 0..53), `ja` to the default
(`llvm_unreachable`), `lea 0x6c9a3d(%rip),%rdx` loading the table base
**`0x1e04ef0`** (54 × int32 RIP-relative arm offsets), then `jmp *%rax`. Each arm is
`lea <str>,%rsi` loading a `.rodata` string and tail-calling the string builder.
Decoding all 54 table entries firsthand this pass yields every index→string map
below. **Every name carries the `backend::` prefix in `.rodata`**, so the on-disk
metric-type strings are literally `"backend::PostSchedEstLatency"` etc.

> **NOTE —** the enum is co-located in *one* store but produced by *many* passes
> (see [Producers](#producers--store--consumers)). perf-sim contributes only index
> 42; the per-engine `Num*Instructions` (47..53) come from `post_sched`/codegen, the
> DMA-size block (9..26) from the report passes, the `Dram*` family (30..36) and
> renumber counts (29/30/37) from the allocators.

The 54 entries, each anchored to its `.rodata` string VA (all decoded from the
jump table @ `0x1e04ef0`):

| Idx | Name (`backend::`-prefixed) | strVA | Emitted by |
|---|---|---|---|
| 0 | `PostUnrollInstructionCount` | `0x1d95c60` | FullUnroll InstProfiler |
| 1 | `FullUnrollInstructionCount` | `0x1d95c88` | FullUnroll InstProfiler |
| 2 | `TotalSplitSbNodesCount` | `0x1d95cb0` | VN-Splitter / memloc_split |
| 3 | `CanSplitSbNodesCount` | `0x1c89f65` | VN-Splitter (eligible) |
| 4 | `VNSplitterDeadNodesCount` | `0x1d95cd0` | `VNSplitter::runTransform` `0xd5a3d0` |
| 5 | `ShrunkNodesCount` | `0x1c89f83` | `ShrinkDN::run` |
| 6 | `ShrinkDnSavings` | `0x1c89f9d` | `ShrinkDN::run` |
| 7 | `mlBPCGSize` | `0x1c89fb6` | ML back-prop coalesce-group size |
| 8 | `agRGCGSize` | `0x1c89fca` | accum-group reduce coalesce size |
| 9–26 | `{Local,Shared}{In,Out}{Load,Save,Copy}{Total,Average}DMASize` | `0x1d40818`–`0x1d40ac0` (24 B stride) | `ReportStats`/`DataMoveReporter` |
| 27 | `PreGcaDMAAccesses` | `0x1c89fde` | allocator / GCA DMA pass |
| 28 | `PostGcaDMAAccesses` | `0x1c89ff9` | allocator / GCA DMA pass |
| 29 | `PsumRenumberLocationsCount` | `0x1d95cf8` | PSUM allocator renumber |
| 30 | `DramRenumberLocationsCount` | `0x1d95d20` | DRAM allocator renumber |
| 31 | `DramSpillSpace` | `0x1c8a015` | DRAM spill bytes |
| 32 | `DramAlignedSpillSpace` | `0x1d95d48` | aligned DRAM spill bytes |
| 33 | `DramLocalHWM` | `0x1c828c8` | DRAM local high-water-mark |
| 34 | `DramLocalTotalSize` | `0x1c828de` | DRAM local total |
| 35 | `DramSharedHWM` | `0x1c828fa` | DRAM shared (collective) HWM |
| 36 | `DramSharedTotalSize` | `0x1c82911` | DRAM shared total |
| 37 | `SbRenumberLocationsCount` | `0x1d95d68` | SB allocator renumber |
| 38 | `SplitSelectCount` | `0x1c8a02d` | `LowerSelectPass::run` `0xbef6b0` |
| 39 | `TSPtoACTCount` | `0x1c8a047` | `PeepholeOpts::run` `0xbea260` |
| 40 | `COPYtoACTCount` | `0x1c8a05e` | `PeepholeOpts::run` |
| 41 | `RECIPROCALtoACTCount` | `0x1c8a076` | `PeepholeOpts::run` |
| **42** | **`PostSchedEstLatency`** | **`0x1c8a094`** | **`PerfSimPass::run` `0x166d380` + `post_scheduler::schedule` (inline) — the autotuner reward** |
| 43 | `TotalAllocatedDramSize` | `0x1d95d90` | `HBMUsage::getModelUsage` `0x16b81a0` |
| 44 | `TotalRuntimeDramSize` | `0x1c8a0b1` | `HBMUsage::getModelUsage` |
| 45 | `TotalCodeSize` | `0x1c8a0cf` | codegen CoreV2/V3GenImpl |
| 46 | `TotalDescriptorSize` | `0x1c8a0e6` | `HBMUsage::getDescUsage` `0x16b8710` |
| 47 | `NumUnassignedInstructions` | `0x1d95db0` | `post_sched` (engine==Unassigned) |
| 48 | `NumPoolInstructions` | `0x1c8a103` | `post_sched`/codegen |
| 49 | `NumActivationInstructions` | `0x1d95dd8` | `post_sched`/codegen |
| 50 | `NumPEInstructions` | `0x1c8a120` | `post_sched`/codegen |
| 51 | `NumDMAInstructions` | `0x1c8a13b` | `post_sched`/codegen |
| 52 | `NumDVEInstructions` | `0x1c8a157` | `post_sched`/codegen |
| 53 | `NumSPInstructions` | `0x1c8a173` | `post_sched`/codegen |

Indices 47 + 48..53 are the seven `bir::EngineType` buckets
(Unassigned/Pool/Activation/PE/DMA/DVE/SP). The DMA-size block 9..26 is the cross
product `{Local,Shared} × {In,Out} × {Load,Save,Copy} × {Total,Average}` at a
regular 24-byte string stride (`0x1d40818` + 24·k), which is why the table folds it
to one row rather than 18.

> **QUIRK —** the master flag `-stats` and the `print-data-type-stats` /
> `print-mem-transfer-stats` / `print-large-tensor-stats` `cl::opt`s do **not** gate
> *population* of the store — the store is always written. They gate only what the
> report passes *print* in prose. A reimplementation that conditions `addMetric` on
> `-stats` diverges from the binary.

### The aggregation enum

`MetricAggregationType` is decoded by a `std::string::compare` switch inside
`addMetric` (sidecar lines 184–204) that emits the agg literal into key slot `#0`:

| Value | Literal | strVA |
|---|---|---|
| 0 | `"Sum"` | `0x1c869b5` |
| 1 | `"Average"` | `0x1c869b9` |
| 2 | `"Custom"` | `0x1c869c1` |
| 3 | `"None"` | (literal `"None"` @ `0x1c84ce7`) |
| else | `llvm_unreachable("Unknown MetricAggregationType", ".../metrics.hpp")` | `0x1c869c8` |

The first three literals are contiguous in `.rodata` immediately before the
`"Unknown MetricAggregationType"` diagnostic, all under
`/usr/local/include/neuronxcc/metrics.hpp` (the one path string present in the
binary).

---

## Serialize / Deserialize — the JSON Schema

### serialize

`metricslibrary::serialize[abi:cxx11](bool nested)` (`0x1537b80`) returns an
nlohmann `json_abi_v3_11_3` (the `std::map`-ordered alternative, so keys come out
lexicographically sorted on disk). The algorithm, reconstructed from the inlined
nlohmann calls:

```c
function serialize(bool nested):                       // 0x1537b80
    store = getGlobalMetricStore();                    // inlined singleton
    snap  = store.internal_copy();                     // line 244 — lock-free snapshot
    for node in snap.buckets:                           // split-ordered bucket walk
        agg  = node.tuple[0];                           // node+0x10
        name = node.tuple[1];                           // node+0x30
        type = node.tuple[2];                           // node+0x50 = "backend::<MetricType>"
        v    = visit_to_json(node.value);               // variant index → json number
        kind = decode_agg(agg);                         // std::string::compare → 0/1/2/3
        if (!nested):                                    // FLAT layout (lines 346-636)
            json[name][type] = v;                        //   2 levels: NAME / "backend::<Type>"
        else:                                            // NESTED layout (lines 640-1047)
            (parent, child) = split(type, "::");         //   "backend" / "<MetricType>"
            slot = &json[name][parent][child];           //   3 levels
            if (kind == Average):                        //   line 429.. running mean
                cnt = &json[name][parent]["Count"];
                *slot = (*slot * *cnt + v) / (*cnt + 1);
                *cnt += 1;
            else if (kind == Sum):
                *slot += v;
            else:                                        //   Custom / None
                *slot = v;                               //   last-wins overwrite
    snap.clear();                                        // line 1083
    return json;
```

The nested form (`nested == true`) is the canonical `global_metric_store.json`
shape — a 3-level object:

```json
{
  "<module-name>": {
    "backend": {
      "PostSchedEstLatency": 123456.0,
      "TotalCodeSize": 98304,
      "NumPEInstructions": 512,
      "Count": 3
    }
  }
}
```

The value is a raw JSON number whose JSON type mirrors the variant alternative
(`float` → real; the integer alternatives → integer). The `"Count"` sibling field
appears only inside groups written with `Average` aggregation, so that repeated
serialize/merge passes converge to the true mean rather than the last sample.

> **NOTE —** the `"::"`-split of the type string (`"backend::PostSchedEstLatency"`
> → `"backend"` / `"PostSchedEstLatency"`) is what turns the flat two-level layout
> into the nested three-level one. The split is on the *first* `"::"`; `"backend"`
> is always the parent because every enum string is `backend::`-prefixed.

### Read-back — lossy merge

`MetricStore::readNestedMetricStoreFromFile(std::istream&)` (`0x8586a0`) is a thin
wrapper: it runs the nlohmann lexer/parser over the stream (the float lexer reads
`localeconv()->decimal_point`, confirmed in the body) and then calls
`deserializeNested` (sidecar line 103). Crucially, the `MetricStore*` `this` is
**not** the destination — `deserializeNested` writes straight into the *global*
singleton. So "read from file" is "merge file contents into the process-global
store".

`metricslibrary::deserializeNested(json)` (`0x84eef0`) walks the nested json and
re-inserts every leaf, rebuilding the TripleHash key:

```c
function deserializeNested(json):                      // 0x84eef0
    for (name, backend_obj) in json.items():
        for (child, leaf) in backend_obj["backend"].items():
            type = "backend"; type += "::"; type += child;   // lines 1041-1042
            agg  = "None";                                     // line 1107 — RESET
            // value coercion: EVERY number leaf → float (variant index 0)
            switch (leaf.type):
                case int:   v = (float)(int64) leaf;           // line 950
                case uint:  v = (float)(uint8/…) leaf;          // line 944
                case float: v =          (float) leaf;
                default:    throw "type must be number, but is <T>";
            hash = hash_combine<string,string,string>(type, name, agg);  // line 1168
            global_store.upsert(tuple(agg, name, type), variant<float>(v)); // line 1367
```

This makes read-back **lossy in two ways**, both confirmed in the body:

1. **Integer narrowing** — `*(float*)slot = v39` (line 999) stores every leaf as
   the variant's `float` alternative regardless of the original alternative, so a
   `uint64`/`int64` value that exceeded 24 bits loses precision.
2. **Aggregation erasure** — the agg is forced to `"None"` (line 1107), so after a
   round trip only `(type, name)` distinguish entries; the original Sum/Average
   intent is gone.

For the autotuner this is fine: the consumer only needs the numeric value to
compare tuning candidates, not the fold or the exact integer.

> **GOTCHA —** because read-back forces `agg="None"`, a value written with
> `Sum`/`Average` and then round-tripped through the JSON reader lands in a
> *different* bucket than the original. A reimplementation that keys reads off the
> writer's aggregation will miss the read-back entry. Key reads off `(type, name)`
> only, the way `getMetricMetadataName` (`0x10dff40`, which returns `pair(name,
> type)` and *drops* the aggregation) does.

---

## The Four Sinks

The store touches four on-disk artifacts. Three carry live values, one is a static
descriptor; only two are produced by `metricslibrary`. Output-directory paths are
joined with the run dir via the `std::filesystem` `operator/=` helper, and
existence is gated by a `std::filesystem::exists`/`status` probe.

| File (string VA) | Direction | Producer / Reader | Schema |
|---|---|---|---|
| `global_metric_store.json` (`0x1c86b36`) | **OUT** | `NeffPackager::run` `0x15307e0` → `serialize(nested=true)` → ofstream | nested 3-level |
| `tensorizer_metric_store.json` (`0x1dc9ec9`) | **IN** | `run_backend_driver` `0x8113b0` → `readNestedMetricStoreFromFile` → merge into global store | nested 3-level |
| `hlo_metrics.json` (`0x1c86555`) | **RELAY** | `NeffPackager::writePackageFile` `0x15200e0` → added to NEFF BOM as `hlo_stats.json` (`0x1c86566`) | frontend per-HLO (XLA) |
| `metric-metadata` BOM tag (`0x1dd78a0`) | **DESCRIPTOR** | `NeffFileWriter::addMetricMetadata` `0x153fe40` | static units/descriptions |

- **`global_metric_store.json`** — the canonical full dump of every
  `addMetric` made in the run (`PostSchedEstLatency`, `Num*Instructions`,
  `Dram*`, `*DMASize`, split counts, …), written at packaging time after
  `writePackageFile`.
- **`tensorizer_metric_store.json`** — the cross-stage read-back leg. The backend
  driver checks for this file, and if present opens it as an `ifstream` and merges
  it into the global store *before* the backend passes run, so backend passes and
  the autotuner can read tensorizer-stage measurements. Same nested schema as the
  out file (it is read by the nested reader).
- **`hlo_metrics.json`** — *not* produced by `metricslibrary`; it is the
  HLO/frontend stage's per-HLO metrics file. `writePackageFile` relays it into the
  NEFF under the in-NEFF name `hlo_stats.json`.
- **`metric-metadata`** — the BOM tag (string present at VA `0x1dd78a0`) under
  which `addMetricMetadata` records the static descriptor of metric meanings
  (units/descriptions). This is metric *schema*, not live values.

> **CORRECTION (AD03/K20) —** backing reports D-AD03 §C.4 and D-K20 §0 cite the
> descriptor filename as `MetricMetadata.json`. That exact filename is **not** a
> string literal in `libwalrus.so` — `rg -a -o -i metricmetadata` returns only the
> symbol `addMetricMetadataEv` (×2), not a `.json` path. What *is* confirmed in the
> binary is the BOM **tag** `metric-metadata` (VA `0x1dd78a0`) and the
> `NeffFileWriter::addMetricMetadata()` symbol (`0x153fe40`). The on-disk filename
> the descriptor is written under is **INFERRED** (likely constructed at runtime or
> living in a sibling module), not directly observable here. The three live JSON
> filenames (`global_metric_store.json`, `tensorizer_metric_store.json`,
> `hlo_metrics.json`/`hlo_stats.json`) *are* present as literals and are CONFIRMED.

---

## Producers → Store → Consumers

### Producers

Every producer passes `name = bir::Module::getName()`, a `BackendMetricType`, the
measured value (as a variant), and a `MetricAggregationType`, then calls the single
`addMetric` thunk. The 44 call sites map (symbols confirmed `nm -DC`):

```text
PerfSimPass::run        0x166d380  → 42 PostSchedEstLatency  (the perf-sim write)
post_scheduler::schedule 0xc36010  → 42 (INLINE — embedded TimeAwareScheduler PerfSim)
post_sched / codegen               → 47..53 per-engine Num*Instructions, 45/46 sizes
ColoringAllocator::Rep::allocate 0x98f830 (~20 calls) → 29,31,32,37,38 SB/PSUM stats
DRAM_Allocator::allocate 0xa6cb20  → 30,33-36 Dram*
VNSplitter::runTransform 0xd5a3d0  → 2,3,4 split-node counts
ShrinkDN::run                      → 5,6 ShrunkNodes / ShrinkDnSavings
PeepholeOpts::run 0xbea260         → 39,40,41 *toACT
LowerSelectPass::run 0xbef6b0      → 38 SplitSelectCount
FullUnroll::InstProfiler::print    → 0,1 unroll counts
HBMUsage::getModelUsage 0x16b81a0  → 43,44; getDescUsage 0x16b8710 → 46
ReportStats / DataMoveReporter     → 9..26 DMA sizes
```

Two passes write metric 42: the standalone `PerfSimPass` *and*
`post_scheduler::schedule` (which embeds a `TimeAwareScheduler` PerfSim and emits
the same `modular_flow_runs * get_overall_end` scalar inline). Because they share
the one key `(type=42, module, agg)`, later writes overwrite earlier ones —
last-wins — so the published value is the final post-RA estimate. The producer
wiring and the pre-RA/post-RA pipeline slots are documented in the perf-sim wiring
sibling.

### Store → consumers

The store is a *passive* singleton — there is no in-process pub/sub. Two read
paths exist:

1. **Cross-stage file read-back** — `run_backend_driver` merges
   `tensorizer_metric_store.json` into the global store, so an earlier stage's
   metrics become available to the backend and the autotuner.
2. **In-memory iteration** — `getMetricMetadataName` (`0x10dff40`) returns
   `pair(name, type)` per node (dropping the aggregation), the lookup key a consumer
   sees; `NeffFileWriter::addMetricMetadata` walks the store to build the NEFF
   `metric-metadata` entry.

### The autotuner loop

```text
PerfSim / VNSplitter / allocators  (produce)
        ▼ addMetric (upsert)
global MetricStore  (tbb concurrent_hash_map, singleton)
        ▼ serialize(nested=true) → global_metric_store.json   (OUT)
        ▲ readNestedMetricStoreFromFile ← tensorizer_metric_store.json (IN)
        ▼ NeffFileWriter::addMetricMetadata → NEFF metric-metadata
PGA / MCTS layout autotuner reads PostSchedEstLatency (42)
        ▼ picks best split/fusion/layout candidate (lower cycles → higher reward)
re-runs passes → next iteration produces a new PostSchedEstLatency
```

The file legs let the loop survive across process and stage boundaries: the parent
autotuner spawns a child compile, reads metric 42 from the serialized surface as
the `nc_latency` reward, and recompiles. The reward *arithmetic* (UCT, beam search)
lives in the PGA-feedback sibling (8.49, planned); the metric plumbing — store, key,
value, serialize — is CONFIRMED here.

---

## Cost-Stats Constant Reference

The store is the *quantitative* channel of the backend's observability surface; the
constants that the cost model (perf-sim and the spill/loop allocators) reads to
*produce* those metrics are a separate, consolidated reference. They are
reproduced here as the companion constant table to the store inventory above — the
metric values in the store are computed from these. All re-verified firsthand
against the binary; per-mechanism derivations live in the cited siblings.

| Constant | Value | Where (addr / sym) | Controls | Confidence |
|---|---|---|---|---|
| Spill weight (in-loop) | `getLatency(DRAM=8)` | `find_costs` `0x9e4713` (gated by `isLoopBody` `0x9e45c8`) | DRAM round-trip latency for a use inside a loop body | CONFIRMED |
| Spill weight (non-loop) | `getLatency(SB=16)` | `find_costs` `0x9e4df9`/`0x9e58d3`/`0x9e616f` | on-chip SBUF spill/reload latency | CONFIRMED |
| MemoryType selector | DRAM=8, SB=16, PSUM=0x20 | binary `isLoopBody` DenseSet test | loop/non-loop tier — *not* a K^depth power | CONFIRMED |
| Un-spillable sentinel | `+inf` `0x7FF0000000000000` | qword `0x1DBCEB8` | cost slot when pinned / never-used | CONFIRMED |
| Finite/inf compare bound | `DBL_MAX` `0x7FEFFFFFFFFFFFFF` | qword `0x1dbce10` | spillability threshold (compare only, never stored) | CONFIRMED |
| Chaitin metric | `argmin(cost / degree²)` | `WithLoop` simplify `0xab58c0` (`imul edx,edx`) | spill-candidate selection (degree *squared*) | CONFIRMED |
| Loop-opt distance threshold | 8 | `cl::opt` static-init `0x7cf85d`, cell `0x3dff780` | fusion-candidate window in BB order | CONFIRMED |
| Loop tile factor | `GCD(partExtent(l1), partExtent(l2))` if >1 | inline GCD `0xb93b08`; `"[LOOP TILING] by N"` `0xb94ebb` | tiling factor — not a fixed const | STRONG |
| Fusion profit | `2 × Σ_sharedTensors (liveN × elemSize)` | `constructFusionProfitGraph` `0xba1375` (`addss xmm0,xmm0`) | FPG edge weight | CONFIRMED |
| `loop_opt_sb_size` / `loop_opt_psum_size` | 0 (derive from arch) | `cl::opt` `0x3dffa80` / `0x3dff9c0` | fused working-set budget | CONFIRMED |
| enable-loop-distribution / -fusion | true / true | bool `0x3dff900` / `0x3dff840` | greedy fusion is default | CONFIRMED |
| Full-unroll instruction ceiling | `> 24,999,999` (`0x17D783F`) throws | `instruction_limit_check` (full_unroll) | 25M-instruction hard limit | CONFIRMED |
| `MaxFactor` (split granularity) | 16 (`0x10`) | `getLargestFactorWithThreshold` (full_unroll) | storage/engine split — *distinct* from the GCD tile factor | CONFIRMED |
| Perf-sim model-cycle unit | 100 | `getLatencyHelper` `0x1853140` | `floor((NEP·mult+base)·100/freq)` divisor numerator | CONFIRMED |
| Engine freq (Tonga / Gen3 / CoreV4) | 140,112 / 120,96 / 120 | `getEngineFrequency` `0x1851b30`/`0x1858090`/`0x185e910` | throughput divisors (not MHz) | CONFIRMED |
| Matmult standalone | `(K/2 + 200)·100/freq` | `getLatencyMatmult` `0x1851800` (`0x3e8`=1000) | matmul latency | CONFIRMED |
| DMA readInit / exec divisor | `1300 + DGE` / Tonga 17, Gen3 23 | `getLatency(InstDMACopy)` `0x1855850` / `getLatencyExec` `0x1856240` | DMA cost | CONFIRMED |
| dtype-size table | `[1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8]` | `0x1E1B0C0`/`0x1E1B1C0`/`0x1E1B2C0` | per-dtype byte widths | CONFIRMED |
| PerfSim fixed overhead / bw scale | 500 cycles / `1.0f` (`0x3F800000`) | PerfSim ctor `0x165a7f0` (this+1 / this+44) | pipeline overhead, HBM bw scale | STRONG |
| `longest_path` depth cap | `999` (`0x3e7`) | `post_scheduler::longest_path` `0xc183e0` | recursion-depth memo | CONFIRMED |
| `PostSchedEstLatency` formula | `get_modular_flow_runs(mod+420) × get_overall_end` | `PerfSimPass::run` `0x166d380` L802 | the metric-42 value itself | CONFIRMED |

> **NOTE —** the spill weights are `bir::Hwm` *cost-oracle* cycle counts, not
> allocator-resident float literals — `find_costs` (`0x9e3ff0`–`0x9e6200`) contains
> zero `mulsd`/`divsd`, i.e. no hard-coded float weights. The "loop multiplier" is a
> binary in-loop⇒DRAM-tier switch via `isLoopBody`, **not** a `K^depth` power. The
> `+inf` sentinel is the *stored* un-spillable value; `DBL_MAX` is only the
> finite-vs-infinite *compare* bound.

> **CORRECTION (K20) —** the `DagPart` `POracle::addMetric(PMetric&)` (`0xd13070`) /
> `PMetric` system is a **separate** partition-explorer cost store — it is *not* the
> `BackendMetricType` `MetricStore`, does *not* feed `-stats`, and must not be
> conflated with the singleton documented on this page.

---

## Reimplementation Notes / Caveats

- `serialize`'s JSON construction is inlined nlohmann-json (`json_abi_v3_11_3`,
  `std::map`-ordered → keys sorted lexicographically on disk). The per-leaf logic
  (`operator[]` chains, the variant `to_json` visit, the `"Count"` averaging) is
  reconstructed from the inlined calls; treat the exact dump formatting (indent,
  separators) as nlohmann `dump()` defaults with the caller-passed indent.
- The variant alternative on *write* is whatever the producer passed
  (float/uint32/int64/uint64); on *read-back* `deserializeNested` narrows everything
  to `float` (index 0). A reimplementation that preserves integer alternatives
  through the JSON reader will *not* be byte-compatible with this binary.
- `TripleHashCompare`: the hash is `llvm::hash_combine` over the three strings (not
  `std::hash`); equality is three `memcmp`s. Reproduce the combine order
  `(type, name, agg)` to interoperate with on-disk keys — though after a round trip
  `agg="None"`, so only `(type, name)` distinguish entries.
- The store is a process-global singleton; in a multi-NeuronCore compile the *name*
  (module name) is what separates per-core metrics within the one shared map.

### Verification ceiling

Decoded firsthand and CONFIRMED this pass against `libwalrus.so`: the 54-entry
jump table (`0x1e04ef0`, all 54 index→strVA decoded, index 42 = `backend::PostSchedEstLatency`
at `0x1c8a094`); the variant arity 4 and `node+0x70`/`node+0x78` store offsets; the
`hash_combine` order and three tuple members; the three live JSON filenames as
`.rodata` literals; the agg switch literals; `getGlobalMetricStore` inlining
(guard/`instance_` only, no out-of-line body). **Not** directly observable, marked
INFERRED on this page: the descriptor on-disk filename `MetricMetadata.json` (only
the BOM tag `metric-metadata` and the `addMetricMetadata` symbol are present); the
exact `get_modular_flow_runs` (mod+420) trip-count formula (logger-heavy body,
deferred to the perf-sim cost-model sibling); and the per-callsite enum immediate
for all ~20 ColoringAllocator `addMetric` sites (STRONG from the name↔pass map, not
individually transcribed).

---

## Related Components

| Name | Relationship |
|---|---|
| `PerfSimPass` / `post_scheduler::schedule` | Producers of metric 42 `PostSchedEstLatency` |
| `BackendMetricType2String` `0x173b490` | The enum→string dispatcher feeding the key |
| `NeffFileWriter::addMetricMetadata` `0x153fe40` | Walks the store → NEFF `metric-metadata` descriptor |
| `run_backend_driver` `0x8113b0` | Reads `tensorizer_metric_store.json` back into the store |
| `NeffPackager::run` `0x15307e0` | Writes `global_metric_store.json` at packaging |

## Cross-References

- [perf_sim Pass Wiring](perf-sim-wiring.md) — the pass that produces metric 42, the orders-88/99/106 slots, and the autotuner-feedback path
- [DMAMetrics and PerformanceProfiler](dmametrics-profiler.md) — the sibling per-load metrics surface (8.48) feeding the DMA-size block 9..26
- [post_sched and the Three Schedulers](post-sched-schedulers.md) — the inline metric-42 writer and the per-engine `Num*Instructions` counts
- [BackendPass Registry](backendpass-registry.md) — how the producer passes are registered and ordered
- [VN-Splitter and Shrink-ML](vnsplitter-shrink.md) — producer of split-node counts 2/3/4 and shrink counts 5/6
- [The DRAM Allocator](dram-allocator.md) — producer of the `Dram*` family 30..36
- [The PSUM Allocator](psum-allocator.md) — producer of renumber counts 29/37
- The perf-sim cost-model page (8.45, in-flight) owns the `bir::Hwm` latency oracle and the `PostSchedEstLatency` topo-walk that fills metric 42
- The PGA-feedback page (8.49, planned) owns the autotuner reward arithmetic that reads `PostSchedEstLatency` back
