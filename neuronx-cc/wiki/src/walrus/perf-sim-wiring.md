# perf_sim Pass Wiring & the `debug_info_pttf` Trace Tarball

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 share the same `libwalrus.so` byte-for-byte). The producer lives in `neuronxcc/starfish/lib/libwalrus.so`. For `.text` (`0x62d660+`) and `.rodata` (`0x1c72000+`) the virtual address equals the file offset (every string VA below was found by `grep -abo` over the extracted `_rodata.bin` + base `0x1c72000`); `.data` carries a `0x400000` VMA↔fileoff delta, and the `0x5e…/0x60…/0x62…` aliases are `.plt` thunks / ICF duplicates — every body cited here is the real function above `0x62d660` (e.g. `getFullDebugFn` lives at `0x11fc070`, not the `0x60dda0` thunk frame). Other wheels differ; treat every address as version-pinned.*

## Abstract

The backend's analytical performance simulator (`perf_sim`) is wired into the walrus pass pipeline at **three** slots and produces two distinct artifacts: a single scalar (`PostSchedEstLatency`, `BackendMetricType` 42) that becomes the autotuner reward, and a set of per-basic-block **Chrome/Catapult trace-event JSON files** that are gathered, gzip-tarred as `file.perf-sim`, and embedded into the NEFF as member **`debug_info_pttf`** ("Perf-sim Trace Tar File"). This page recovers the *wiring* and the *packaging* — the cost model itself (the `bir::Hwm` oracle, the `SimEvent` timeline, `get_overall_end`) is the subject of the in-flight `walrus/perfsim-cost-model.md` sibling (Part 8.45) and is referenced, not re-derived, here.

The recovery target is reimplementation grade: a reader should be able to rebuild the three factory registrations and their pipeline ordinals, the per-BB double invocation (why `perf_sim` runs the simulator *twice per block*), the byte-exact Catapult JSON schema (every format-string fragment + the eleven engine lanes), and the `collectTraceFiles → createTarball → debug_info_pttf` packaging chain (libarchive PAX+gzip, the `perf_sim\..*\.json` discovery regex, the `IRLayer 0` member name).

| | |
|---|---|
| **Pass class** | `neuronxcc::backend::PerfSimPass` (one class, three registrations) |
| **Constructor** | `PerfSimPass::PerfSimPass(PassOptions const&, std::string const&)` @ `0x166fc20` (104-byte object) |
| **Driver** | `PerfSimPass::run(bir::Module&)` @ `0x166d380` |
| **Factory — `perf_sim`** | `register_generator_perf_sim__` invoke @ `0x166ffd0` (BSS flag `0x3e05412`) |
| **Factory — `perf_sim_at_end`** | `register_generator_perf_sim_at_end__` invoke @ `0x1670100` (BSS flag `0x3e05411`) |
| **Factory — `perf_sim_package_pass`** | `register_generator_perf_sim_package_pass__` (BSS flag `0x3e05414`) |
| **Pipeline ordinals** | order **88** (pre-RA), **99** (post-RA), **106** (`perf_sim_at_end`) — driver ordinals, STRONG |
| **Trace serializer (run A)** | `PerfSim::dump_chrome_trace(std::string)` @ `0x165dc90` |
| **Trace serializer (run B)** | `PerfSim::dump_trace(std::string, Module&, Function&, BasicBlock&)` @ `0x1661090` |
| **Reward metric** | `addMetric(42 …)` @ `0x1742020`; `BackendMetricType2String(42)` = `"backend::PostSchedEstLatency"` @ `0x173b490` |
| **Package pass (real entry)** | `PerfSimPackagePass::run(vector<unique_ptr<Module>>&)` @ `0x1672530` |
| **Trace discovery** | `PerfSimPackagePass::collectTraceFiles(path const&, vector<path>&)` @ `0x1671d40` |
| **Tar builder** | `PerfSimPackagePass::createTarball(vector<path> const&, path const&)` @ `0x1670ed0` |
| **Tar filename** | `"file.perf-sim"` (`0x1c88e6f`) |
| **NEFF member name** | `DebugInfoWriter::getFullDebugFn(IRLayer=0, …)` @ `0x11fc070` → `"debug_info_pttf"` (`0x1c84377`) |
| **Enable gate** | `cl::opt "enable-perf-sim"` (`0x1dc3923`) |
| **Source TU (asserts)** | `…/neuronxcc/walrus/perf_sim/src/perf_sim.cpp` (`0x1d8336e`) |

The string evidence is direct: `grep -abo` over `_rodata.bin` resolves the three pass-name literals (`perf_sim` `0x1dc6ef1`, `perf_sim_at_end` `0x1dc6f7e`, `perf_sim_package_pass` `0x1dc6f8e`), the discovery regex `perf_sim\..*\.json` (`0x1c88e2e`), the tar name `file.perf-sim` (`0x1c88e6f`), the NEFF member family `debug_info_{pttf,hlo,penguin,backend,asm}` (`0x1c84377`+), and every Catapult JSON format fragment (`0x1c88b70`+).

> **CORRECTION (vs backing report D-H26):** that report cited the `enable-perf-sim` CLI string at VA `0x1dc4823`. The literal `enable-perf-sim` actually sits at **`0x1dc3923`** in this build (`grep -abo` over `_rodata.bin`); `0x1dc4823` does not hold that string. Address corrected here.

---

## 1. Three factories, one class, three pipeline slots

`perf_sim` is registered through the same `GeneratorRegistration` mechanism every backend pass uses: a `std::function<unique_ptr<BackendPass>(PassOptions const&)>` whose `_M_invoke` thunk constructs the concrete pass. **Three** registrations all instantiate the *identical* `PerfSimPass` class — the only difference is the name string handed to the constructor. [CONFIRMED — both invoke bodies read firsthand.]

```c
// register_generator_perf_sim__  _M_invoke  @0x166ffd0  (CONFIRMED)
unique_ptr<BackendPass> perf_sim_factory(PassOptions const &opts) {
    std::string name;
    sub_166BAC0(&name, "perf_sim", "");        // .rodata "perf_sim" @0x1dc6ef1
    PerfSimPass *p = (PerfSimPass *)tc_new(104); // 104-byte PerfSimPass object
    PerfSimPass::PerfSimPass(p, opts, name);     // ctor @0x166fc20
    return p;
}

// register_generator_perf_sim_at_end__  _M_invoke  @0x1670100  (CONFIRMED)
// BYTE-IDENTICAL except the literal:
//    sub_166BAC0(&name, "perf_sim_at_end", "");  // .rodata @0x1dc6f7e
```

The constructor stores the name at `this+1` (a `std::string`), builds a `logging::Logger` at `this+5` (its channel is the demangled `neuronxcc::backend::PerfSimPass` with the `neuronxcc::backend::` prefix stripped), and stashes the `PassOptions*` at `this[12]`. The name flows into the trace-file prefix (§4) and the log channel, so the three instances are byte-identical `PerfSimPass` objects placed at three different points in the driver's pass list.

**The pipeline ordinals 88 / 99 / 106 are the Python `WalrusDriver` default-pipeline ordinals, not constants stored in `libwalrus.so`.** The library stores pass *names*; the ordering is built by the driver's pass list. The bracketing is STRONG from the neighbour pass names:

| Ordinal | Pass name | Position | Gate | Purpose |
|---|---|---|---|---|
| **88** | `perf_sim` | after `prefetch_scheduling_before_sched` (87), before load/compute split + `post_sched` | `--enable-perf-sim` | **post-first-schedule / pre-RA** estimate — measures the schedule `pre_sched`+prefetch produced, before register allocation / DMA-engine binding |
| **99** | `perf_sim` | after `post_sched` (92) + `arch_legalize` (93) / `legalize_mm_accumulation_groups` (94) / `insert_ptcom` (95) / `expand_scheduling_units` (96) | `--enable-perf-sim` | **post-final-schedule / post-RA** estimate — the schedule actually handed to codegen |
| **106** | `perf_sim_at_end` | final read-out after the mid-level pipeline | always (no separate CLI flag) | pure read-out; emits the `perf_sim_at_end_trace.*` family only |

Orders 88 and 99 write the **same** `MetricStore` key (`type=42`, `module_name`, agg-name), so **99 overwrites 88** under last-wins aggregation: the published reward is the post-RA number, and the order-88 write is the pre-RA snapshot read by intervening native consumers (`separate_load_and_compute` / `dep_opt`). There is **no fixed-point iteration** inside walrus — "twice" is literally the count of pipeline slots that share a key. [STRONG — single key, two writes; ordinals STRONG from neighbour names, INFERRED on the exact integers since they are driver-side.]

> **No separate `perf-sim-at-end` CLI string.** `grep -abo perf-sim-at-end` over `_rodata.bin` returns nothing; the order-106 instance is selected by the driver appending the `perf_sim_at_end`-named pass to the list, not by a library-resident `cl::opt`. (D-H26 named a `--perf-sim-at-end` flag; the gating is the registered pass name, not a string in this binary.)

---

## 2. `PerfSimPass::run` — the per-BB double invocation

`run(PerfSimPass*, bir::Module&)` @ `0x166d380` walks every function → every basic block and, **per block, runs the simulator twice**. The two runs differ in exactly one constructor argument: whether anti-dependency (WAR/WAW) edges are modeled as stalls. [CONFIRMED — both `PerfSim` ctor sites and both `perf_estimation` sites read firsthand.]

```c
// PerfSimPass::run(this, bir::Module &mod)   @0x166d380   (CONFIRMED)
int modular_flow_runs =
    get_modular_flow_runs(this,
        *(uint32_t *)(*((uint64_t *)mod_field + 12) + 420));  // module field +420 = flow descriptor

for (Function &fn : mod) {
  for (BasicBlock &bb : fn) {

    // ---- INVOCATION A : WITH anti-dependencies (the PUBLISHED metric) ----
    PerfSim simA(/*cycle_accurate=*/1, /*bw_scale=*/0.0f, /*b3=*/0,
                 /*model_antideps=*/1, logger);          // ctor @0x165a7f0, a5 = 1
    uint v189 = PerfSim::perf_estimation(&simA, bb, /*cycle_accurate=*/1);
    LOG_INFO(": Estimated latency (ns), for BasicBlock <bb>");   // .rodata @0x1c…

    uint64_t estimate = modular_flow_runs * v189;        // per-BB metric value
    LOG_INFO("Time-aware simulation time: <estimate>");  // perf_sim.cpp:758, str @0x1c77636
    addMetric(42 /*PostSchedEstLatency*/, getName(), estimate, /*agg*/…);  // §3

    PerfSim::dump_chrome_trace(&simA, "<mod>.<fn>.<bb>.json");  // run-A trace, @0x165dc90

    // ---- INVOCATION B : WITHOUT anti-dependencies (DIAGNOSTIC, not published) ----
    PerfSim simB(/*cycle_accurate=*/1, /*bw_scale=*/0.0f, /*b3=*/0,
                 /*model_antideps=*/0, logger);          // ctor @0x165a7f0, a5 = 0
    uint v10 = PerfSim::perf_estimation(&simB, bb, 1);
    LOG_INFO(": Estimated latency without anti-dependencies (ns), for BasicBlock <bb>");
    PerfSim::dump_trace(&simB, "<mod>.<fn>.<bb>.json", mod, fn);  // run-B trace, @0x1661090

    // ~PerfSim(simB); ~PerfSim(simA);
  }
}
```

The `PerfSim` constructor signature `PerfSim(bool cycle_accurate, float bw_scale, bool b3, bool model_antideps, logging::Logger&)` @ `0x165a7f0` is CONFIRMED; defaults are `this+1 = 500` (fixed pipeline-overhead cycles), `this+44 = 1.0f` (HBM-bandwidth scale = `a3`), `this+12 = 20` (log severity). Run A is the conservative estimate (anti-deps honored) that becomes `PostSchedEstLatency`; run B is the optimistic lower bound logged for diagnostics. [CONFIRMED.]

**Why the double invocation matters for a reimplementer:** run A is the *metric* path (it feeds `addMetric(42)` and the tarred trace via `dump_chrome_trace`); run B exists purely to print the no-anti-dependency floor and to emit the richer dependency-graph trace via `dump_trace`. The two traces share one filename stem `<mod>.<fn>.<bb>.json`, and **both** consume the same BIR accessors (`InstructionType2string`, `getInputShapes`, `getInputAccessPatterns`, `EdgeKind2string` — confirmed in both `0x165dc90` and `0x1661090`). The per-module total handed to the metric is `PostSchedEstLatency = modular_flow_runs * v189`, i.e. estimated cycles per inference run. `get_modular_flow_runs` @ `0x109c360` reads the module's flow descriptor at field `+420`; its exact trip-count derivation is a cost-model concern (deferred to 8.45). [CONFIRMED on the multiply + `addMetric`.]

---

## 3. The reward scalar — `addMetric(42)` → the global `MetricStore`

The published number is one entry in a process-global singleton. `addMetric` @ `0x1742020` builds a three-string key and inserts into a TBB `concurrent_hash_map`:

```c
// neuronxcc::backend::addMetric(BackendMetricType type, const string &module_name,
//                               variant<float,uint,long,ulong> value,
//                               const MetricAggregationType &agg)   @0x1742020 (CONFIRMED)
tuple<string,string,string> key = {
    BackendMetricType2String(type),   // 42 → "backend::PostSchedEstLatency"  @0x173b490
    module_name,
    agg_name                          // 0→"Sum" 1→"Average" 2→"Custom" 3→"None"
};
size_t h = llvm::hash_combine(key);
MetricStore::getGlobalMetricStore().instance_.insert(key, value);  // tbb concurrent_hash_map
```

The `PerfSimPass::run` call site passes `type = 42`, `value = modular_flow_runs * v189` (a `ulong`), and the aggregation index `2` (the `"Custom"` arm = last-wins/replace), so the order-99 write replaces the order-88 write under one key.

> **Naming nuance:** `BackendMetricType2String(42)` returns the *prefixed* string **`"backend::PostSchedEstLatency"`** (decoded from `case 42:` @ `0x173b490`), not the bare `PostSchedEstLatency`. The key actually stored is `("backend::PostSchedEstLatency", module, agg-name)`.

`PostSchedEstLatency` is index 42 of the 54-entry `BackendMetricType` enum (0..53). **`perf_sim` produces only type 42** — the per-engine instruction counts (`NumPEInstructions` 50, `NumPoolInstructions` 48, `NumActivationInstructions` 49, `NumDMAInstructions` 51, `NumDVEInstructions` 52, `NumSPInstructions` 53) and the DRAM/DMA size counters are co-located in the same store but emitted by `PostSched` / codegen / the allocators, not by `perf_sim`. The full enum and the 44-site producer map are the subject of the cost-model / metrics siblings; here the relevant fact is the single key and the last-wins overwrite.

This store is the autotuner reward surface: it is serialized via `NeffFileWriter::addMetricMetadata` (NEFF metric-metadata) and read back cross-process via `MetricStore::readNestedMetricStoreFromFile`; the parent autotuner process reads exactly the type-42 entry as its `nc_latency` reward. [STRONG — reward identity; the autotuner-side parse is Python territory, named not opened.]

---

## 4. The Catapult trace-event schema (run A: `dump_chrome_trace`)

`PerfSim::dump_chrome_trace(std::string output_name)` @ `0x165dc90` writes one file in the **legacy Chrome/Catapult "JSON Array Format"** — a bare top-level array `[ … ]`, *not* the `{"traceEvents":[…]}` object form. The serializer is a single pass: an open bracket, eleven `"ph":"M"` thread-name metadata events (one per engine lane, written once as a header), then one `"ph":"X"` complete-duration event per `SimEvent`, then a close bracket. Every literal below was decoded byte-exact from `_rodata.bin`. [CONFIRMED — full body + every fragment.]

### 4.1 File structure

```
[                                                          // 0x1c7ddf6, then endl
{"name":"thread_name","ph":"M","pid":0,"tid":0,"args":{"name":"0. Unassigned"}},   // 0x1d83870
{"name":"thread_name","ph":"M","pid":0,"tid":1,"args":{"name":"4. POOL"}},         // 0x1d838c8
{"name":"thread_name","ph":"M","pid":0,"tid":2,"args":{"name":"7. ACT"}},          // 0x1d83918
{"name":"thread_name","ph":"M","pid":0,"tid":3,"args":{"name":"5. PE"}},           // 0x1d83968
{"name":"thread_name","ph":"M","pid":0,"tid":4,"args":{"name":"1. DMA"}},          // 0x1d839b8
{"name":"thread_name","ph":"M","pid":0,"tid":5,"args":{"name":"6. DVE"}},          // 0x1d83a08
{"name":"thread_name","ph":"M","pid":0,"tid":6,"args":{"name":"3. SP"}},           // 0x1d83a58
{"name":"thread_name","ph":"M","pid":0,"tid":7,"args":{"name":"2.0 CCStream0"}},   // 0x1d83aa8
{"name":"thread_name","ph":"M","pid":0,"tid":8,"args":{"name":"2.1 CCStream1"}},   // 0x1d83b00
{"name":"thread_name","ph":"M","pid":0,"tid":9,"args":{"name":"2.2 CCStream2"}},   // 0x1d83b58
{"name":"thread_name","ph":"M","pid":0,"tid":10,"args":{"name":"2.3 CCStream3"}}   // 0x1d83bb0 (no trailing comma)
,<event>,<event>,…                                         // one "ph":"X" record per SimEvent
]                                                          // close array + endl
```

The `tid` lane is the simulator's engine timeline; the `N. ` numeric prefix on each name is the hardware engine id (`0` Unassigned, `1` DMA, `2.x` collective-compute streams, `3` SP, `4` POOL, `5` PE, `6` DVE, `7` ACT). The eleven lanes match the `all_timelines` (`vector<vector<SimEvent>>`) walked by the simulator and the `SimEngineId` enum (`CollectiveComputeStream0..3`, `NumSimEngineIds`).

### 4.2 Per-event record (`"ph":"X"` Complete event)

The serializer interleaves these `.rodata` fragments with stream-inserted values, walking `all_timelines[engine]` (outer) and the `SimEvent` list per engine (inner; 56-byte stride, `event.inst = *(inst*)(ev+16)`, `event.start = +0`, `event.duration = +8`):

| Field | Value source | Fragment VA |
|---|---|---|
| `{"name":"<NAME>"` | BIR instruction name (or `"null"`) | `0x1c88b70` |
| `","ph":"X","ts":<TS>` | `ph:"X"` (Complete), `TS` = event start (sim ns/cycles, uint) | `0x1c88b7a` |
| `,"dur":<DUR>` | event duration (uint) | `0x1c88b8b` |
| `,"pid":0,"tid":<TID>` | `pid` always 0; `TID` = engine lane id | `0x1c88b93` |
| `,"args":{"bir_id":<BID>` | `bir::Instruction` id (uint) | `0x1c88ba3` |
| `,"bir_name":"<BNAME>"` | BIR instruction name | `0x1c88bb6` |
| `","opcode":"<OP>"` | `bir::InstructionType2string(inst)` | `0x1c88bc4` |
| `","nki_source_location":"<LOC>"` | `OpDebugInfo` `getFileName():getLineNo()` (empty if no DebugInfo) | `0x1c88bd1` |
| `","input_shapes":"<ISH>"` | `getInputShapes()` | `0x1c88beb` |
| `","output_shapes":"<OSH>"` | `getOutputShapes()` | `0x1c88bfe` |
| `","input_access_patterns":"<IAP>"` | `getInputAccessPatterns()` | `0x1c88c12` |
| `","output_access_patterns":"<OAP>"` | `getOutputAccessPatterns()` | `0x1c88c2e` |
| `","inst_dump":"<DUMP>"` | `bir::operator<<` text of the instruction | `0x1c88c4b` |
| `", "deps":[ <DEPS> ]` | predecessor list (see below) | `0x1c88c5b` |
| `}}` | close `args` + event | `0x1c88c9a` |

Each **dep** element (one per scheduling predecessor whose simulated event exists):

```c
{ … ,"edge_type":"<EK>"          // 0x1c88c7d ; EK = bir::EdgeKind2string(edge)
     ,"end_time":<ET> }          // 0x1c88c8c ; ET = predecessor event end_time (uint)
```

Predecessors are walked, `get_event(pred)` is asserted non-null (`pred_event != nullptr`, `perf_sim.cpp:816`), cross-engine deps carry an `"Engine"`+id tag, and the list is de-duplicated, sorted, and comma-separated (first dep has no leading comma). The `inst == nullptr` placeholder branch emits a degenerate `{"bir_id":…}` record (`0x1c88bab`) named `"null"` / `"dma_transfer_inst"` (`0x1c88c9d`) with `ts`/`dur`/`tid` only (string `"Null instruction in trace"` @ `0x1c88caf`).

> `ph:"X"` = Complete (duration) event; `ph:"M"` = Metadata. The file loads directly in `chrome://tracing` / Perfetto. `ts`/`dur` are **simulated** cycles/ns from the cost model, **not** measured hardware time. [STRONG — Chrome Trace Event Format identification from the `ph` literals.]

### 4.3 Filename families

All three are produced by the same serializer; the caller's pass name picks the prefix:

| Prefix (`.rodata`) | Emitter | Trigger |
|---|---|---|
| `perf_sim.` (`0x1c88da7`) | `PerfSimPass::run` named `perf_sim` (orders 88/99) | `--enable-perf-sim` |
| `perf_sim_at_end_trace.` (`0x1c88d90`) | `PerfSimPass::run` named `perf_sim_at_end` (order 106) | always-at-end |
| `ps_trace.` (`0x1c77653`) | `post_scheduler::schedule` @ `0xc36010` (embedded sim) | `--dump-postsched-trace` (`0x1c776da`) |

The filename is built by `<pass_name> + "." + Module::getName() + "." + … + ".json"`, then sanitized by a `std::regex` that replaces illegal path characters. **Only the `perf_sim.*.json` family is tarred** (§5) — the regex in §5 requires a literal `perf_sim.` substring, which `perf_sim_at_end_trace.*` and `ps_trace.*` do not contain. [STRONG — regex literal + `regex_search` semantics.]

---

## 5. `collectTraceFiles → createTarball → file.perf-sim`

`PerfSimPackagePass` is a fourth registration (`register_generator_perf_sim_package_pass__`, BSS flag `0x3e05414`). Its `run(bir::Module&)` @ `0x1670c40` is a stub (`"… should not be called"`); the real entry is `run(vector<unique_ptr<Module>>&)` @ `0x1672530`, which reads the artifacts directory member, discovers traces, and tars them. [CONFIRMED — full run-vector decompile.]

```c
// PerfSimPackagePass::run(vector<unique_ptr<bir::Module>> &mods)  @0x1672530  (CONFIRMED)
path dir = path(this->artifacts_dir);                  // this+8 std::string member
LOG_INFO("Searching for perf_sim trace files in: " + dir);  // 0x1d840a8

vector<path> files;
if (collectTraceFiles(dir, files)) {                   // @0x1671d40 — true iff non-empty
    path out("file.perf-sim");                         // 0x1c88e6f
    if (createTarball(files, out))                     // @0x1670ed0
        LOG_INFO("Successfully created file.perf-sim"); // 0x1c88ec3
    else
        LOG_ERROR("Failed to create perf sim package"); // run() error return string
} else {
    LOG_INFO("No perf_sim trace files found. Skipping tarball creation.");  // 0x1d840d0
}
```

### 5.1 Discovery — `collectTraceFiles`

```c
// PerfSimPackagePass::collectTraceFiles(path const &dir, vector<path> &out)  @0x1671d40 (CONFIRMED)
std::regex re("perf_sim\\..*\\.json");                  // literal @0x1c88e2e
if (!boost::filesystem::exists(dir)) {
    LOG_ERROR("Directory does not exist: " + dir);      // 0x1c88e41
    return false;
}
for (auto &e : boost::filesystem::directory_iterator(dir)) {   // NON-recursive
    if (e.type() != regular_file) continue;             // type==2 only
    if (std::regex_search(e.path().filename(), re)) {    // SEARCH, not match
        out.push_back(e.path());                         // _M_realloc_insert
        LOG_INFO("Found trace file: " + e.path());       // 0x1c88e5c
    }
}
return out.begin() != out.end();                         // → "Found N perf_sim trace file(s)" 0x1c88e7d
```

`regex_search` (`__regex_algo_impl`) matches the pattern *anywhere* in the filename; because the pattern needs the literal `perf_sim.` substring, only the base `perf_sim.<…>.json` files qualify. No recursion, no sort, no compression here — pure discovery.

### 5.2 Packaging — `createTarball` (libarchive PAX + gzip)

`createTarball` uses **libarchive directly** (not the system `tar`): PAX (POSIX.1-2001) format with a gzip filter, copying each file in 8 KiB chunks. The output `file.perf-sim` therefore has `.tar.gz` semantics. [CONFIRMED — full decompile @ `0x1670ed0`.]

```c
// PerfSimPackagePass::createTarball(vector<path> const &files, path const &out)  @0x1670ed0 (CONFIRMED)
archive *a = archive_write_new();
archive_write_set_format_pax(a);                 // PAX tar
archive_write_add_filter_gzip(a);                // gzip filter
archive_write_set_filter_option(a, …);  x2       // gzip level / options
if (archive_write_open_filename(a, out)) {       // out = "file.perf-sim"
    LOG_ERROR("Failed to open archive file: " + archive_error_string(a));  // 0x1c88dba
    archive_write_free(a); return false;
}
for (path &p : files) {
    struct stat st;
    if (stat(p, &st)) { LOG_ERROR("Failed to stat file: " + p); return false; }   // 0x1c88dd8
    archive_entry *ent = archive_entry_new();
    archive_entry_copy_stat(ent, &st);
    archive_entry_set_pathname(ent, p);
    archive_entry_set_uname(ent, ""); archive_entry_set_gname(ent, "");
    archive_entry_set_mtime/ctime/atime(ent, …);
    FILE *f = fopen(p, "rb");
    if (!f) { LOG_ERROR("Failed to open file: " + p); archive_entry_free(ent); return false; }  // 0x1c88dee
    archive_write_header(a, ent);
    char buf[0x2000];                            // 8 KiB
    while (size_t n = fread(buf, 1, 0x2000, f))
        archive_write_data(a, buf, n);
    fclose(f); archive_entry_free(ent);
    LOG_INFO("Added to tarball: " + p);          // 0x1c88e04
}
archive_write_close(a); archive_write_free(a);
return true;
```

`libwalrus.so`'s job ends at producing the `file.perf-sim` file on disk in the compile artifacts directory; the only xref that constructs the `"file.perf-sim"` literal is this run-vector. The NEFF embedding is downstream (§6). [CONFIRMED — single xref.]

---

## 6. The `debug_info_pttf` NEFF member

The NEFF member *name* is produced by `DebugInfoWriter::getFullDebugFn(IRLayer layer, bir::EngineType eng, std::string ext)` @ `0x11fc070` — a switch on the IR layer that the `debuginfo-writer.md` sibling documents in full for the other four layers. Layer **0** is the perf-sim trace tar. [CONFIRMED — full switch read.]

```c
// DebugInfoWriter::getFullDebugFn(IRLayer layer, EngineType eng, string ext)  @0x11fc070 (CONFIRMED)
switch (layer) {
  case 0: base = "debug_info_pttf";    break;   // 0x1c84377  ← Perf-sim Trace Tar File
  case 1: base = "debug_info_hlo";     break;   // 0x1c84387
  case 2: base = "debug_info_penguin"; break;   // 0x1c84396
  case 3: base = "debug_info_backend"; break;   // 0x1c843a9
  case 4: base = "debug_info_asm";     break;   // 0x1c843bc
  default: base = "unknown";
}
// full member = base + (eng != 0 ? EngineType2string(eng) : "") + counterSuffix + "." + ext;
```

For the whole-module perf-sim tarball, `eng = 0` and there is no counter suffix, so the member is simply **`debug_info_pttf`**. The five `debug_info_*` member names are a contiguous `.rodata` block at `0x1c84377`, and `getFullDebugFn` is the only producer of all five. The actual append of `file.perf-sim`'s bytes into the NEFF container is performed by the NEFF packager (Part 12 — NEFF Container, planned), using this member-name scheme; `libwalrus.so` contributes the *name* (via `getFullDebugFn`) and the *payload* (via `createTarball`), but not the container write. [STRONG — member name + `IRLayer` enum CONFIRMED here; the container append is downstream.]

> **`pttf` = "Perf-sim Trace Tar File".** The expansion is inferred from the artifact chain (perf-sim → trace JSON → tar), not a literal in the binary; the `debug_info_pttf` member string itself is CONFIRMED at `0x1c84377`. [INFERRED on the acronym expansion only.]

---

## 7. Contrast with the runtime `.ntff`

`debug_info_pttf` is the compiler's *prediction*; the runtime `.ntff` (Neuron Trace File Format) is the device's *measurement*. The NKI entrypoints page ([`../nki/entrypoints.md`](../nki/entrypoints.md)) covers the runtime trace surface; the two are designed to be diffed.

| `debug_info_pttf` (this page) | `.ntff` (runtime) |
|---|---|
| **Compile-time**, inside `libwalrus.so` | **Runtime**, on-device profiler |
| Perf-SIM *simulated* timeline (modeled `ts`/`dur`) | *Measured* hardware timestamps |
| Chrome JSON-array trace, gzip-tarred as `file.perf-sim` | Binary `.ntff` artifact |
| Embedded **inside the NEFF** as member `debug_info_pttf` | Separate on-disk artifact captured at exec |
| Always available whenever perf-sim ran | Requires profiling enabled at runtime |

Both describe the same engine lanes (DMA / PE / POOL / ACT / SP / DVE / CC-streams); the pttf is the compiler's prediction and the `.ntff` is the device's measurement, used together to validate the perf model.

---

## 8. Confidence ledger

**CONFIRMED (full decompile or byte-exact string/disasm):**

- The three factory `_M_invoke` bodies (`0x166ffd0` `perf_sim`, `0x1670100` `perf_sim_at_end`) — both `tc_new(104)` the same `PerfSimPass` class, differ only in the name literal.
- The per-BB double invocation in `run` @ `0x166d380`: `PerfSim(…,0,1,…)` (a5=1, anti-deps) vs `PerfSim(…,0,0,…)` (a5=0), both `perf_estimation`, the `modular_flow_runs * v189` multiply, the two log strings.
- `addMetric(42, …)` @ `0x1742020`; `BackendMetricType2String(42)` = `"backend::PostSchedEstLatency"` @ `0x173b490`.
- The full Catapult schema: the array brackets, all 11 thread-name metadata lanes (byte-exact at `0x1d83870`+), and every per-event format fragment (`0x1c88b70`+) including `deps`/`edge_type`/`end_time`.
- `collectTraceFiles` @ `0x1671d40` (regex `perf_sim\..*\.json` @ `0x1c88e2e`, dir-exists check, `directory_iterator`, `regex_search`, `push_back`, return-non-empty).
- `createTarball` @ `0x1670ed0` (libarchive `set_format_pax` + `add_filter_gzip` + 8 KiB `fread`/`archive_write_data` loop) and the run-vector chain `collectTraceFiles → "file.perf-sim" → createTarball → "Successfully created"`.
- `getFullDebugFn` @ `0x11fc070` `IRLayer 0 → "debug_info_pttf"` (and 1..4 → hlo/penguin/backend/asm).
- Pass-name literals, the `enable-perf-sim` option string @ `0x1dc3923`, the `dump-postsched-trace` flag @ `0x1c776da`, the `perf_sim.cpp` source path @ `0x1d8336e`.

**STRONG (logically forced):** only `perf_sim.*.json` (not `perf_sim_at_end_trace.*` / `ps_trace.*`) is tarred (from the regex literal + `regex_search`); `file.perf-sim` becomes NEFF member `debug_info_pttf` via the `DebugInfoWriter` scheme (member name + `IRLayer` enum CONFIRMED here; the container append is in the NEFF packager).

**INFERRED (flagged):** the integer ordinals 88/99/106 are driver-side, not constants in `libwalrus.so` (the bracketing is STRONG from neighbour pass names); the `pttf` acronym expansion; the exact `ext` on the pttf member name (the `getFullDebugFn` `ext` arg is set by the caller, not pinned to a literal in this lib); whether `perf_sim_at_end` traces are *also* embedded under a different member (not evidenced — only `perf_sim.*.json` is shown to be tarred).

**Re-verify ceiling:** the wiring (factories, double invocation, addMetric key) and the packaging (regex → libarchive → member name) are firsthand and unlikely to be wrong. The weakest links are the *driver-side ordinals* (cannot be cross-checked against a numeric constant in this binary) and the *NEFF container append* (lives in the packager, not `libwalrus.so`); both are tagged accordingly and neither is fabricated.
