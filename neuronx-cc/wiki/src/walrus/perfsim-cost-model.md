# PerfSim Cost Model — the `bir::Hwm` oracle and the cycle simulator

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The cost model and the simulator live in `neuronxcc/starfish/lib/libwalrus.so`; the abstract `bir::Hwm` base lives in `libBIR.so`. For `.text`/`.rodata` the virtual address equals the file offset; `.data` carries a +0x400000 delta. Other wheels differ — treat every address as version-pinned.*

## Abstract

After the backend has scheduled a basic block, it needs a number: *how many cycles will this block take on the target chip?* That number drives spill decisions, anti-dependency pruning, and — one layer up — the layout/fusion autotuners that recompile the model thousands of times. `neuronx_cc` produces it with a two-stage estimator wholly contained in `libwalrus.so`:

- **`bir::Hwm`** — a per-architecture **analytic latency oracle**. An abstract base in `libBIR.so` declares a per-opcode `getLatency(InstX)` family; every method on the base is a pure virtual that aborts with a `"…not implemented"` assertion. Three concrete subclasses in `libwalrus.so` supply the actual cycle formulas, one per Trainium generation. `getLatency(Inst)` returns a scalar `readInit + exec + writeDrain`.
- **`PerfSim`** — a per-basic-block **event-driven Wavegraph simulator**. It pre-bakes the oracle's per-instruction latencies into a flat `cost[]` array, then walks the scheduled BIR in program order, placing each instruction as one `SimEvent` on exactly one of **twelve** per-engine timelines. Cross-engine semaphore stalls, PSUM-bank serialization, engine-band coupling, and a finite DMA-descriptor-ring model fold into each instruction's start time. The block's cost is the **critical path** = the longest of the twelve engine timelines.

`PerfSimPass` (the pipeline driver, owned by [8.46](#cross-references)) multiplies that per-block critical path by the modular-flow trip count and writes it to the metric store as `backend::PostSchedEstLatency`. This page documents the two stages a reimplementer must rebuild: the oracle interface and its three concrete number-tables, and the simulator's event model, timeline array, DMA-trigger contract, and the alternate learned (`MLInstructionLatencyModel`) oracle.

For reimplementation, the contract is:

- The **pure-virtual `bir::Hwm` base** (the vtable of `getLatency*` slots that each abort) and the **three concrete implementations** — `TrainiumHwm` / `Gen3Hwm` / `CoreV4Hwm` — keyed by codename through `bir::Hwm::getSingleton`.
- The **shared 100-cycle-unit helper machinery** (`getLatencyHelper` / `getExecLatencyHelper` / `getOverheadLatencyHelper`) and the per-engine frequency divisor tables.
- The **`SimEvent` (56 B)** record, the **`all_timelines[12]`** array of per-engine timelines, and `add_instruction`'s start-time computation.
- The **DMA-trigger contract**: how an `InstDMATrigger` reserves a descriptor in one of thirteen DMA queues and stalls on the head transfer's issue time.
- The **`MLInstructionLatencyModel`** learned oracle that replaces `Hwm` when `--ml-model-directory` is set.

| | |
|---|---|
| **Abstract oracle base** | `bir::Hwm` (`libBIR.so`); `Hwm.cpp` asserts `getLatency( const InstX & ) not implemented` |
| **Singleton registry** | `bir::Hwm::getSingleton(std::string const&)` @ `0x61d680`; `getSingletonMap` @ `0x5f7ce0` |
| **Concrete oracles** | `TrainiumHwm` ctor `0x1853f40` / `Gen3Hwm` `0x185b370` / `CoreV4Hwm` `0x18606c0` |
| **Oracle vtables** | `_ZTV11TrainiumHwm` @ `0x3da48b0` · `_ZTV7Gen3Hwm` @ `0x3da5770` · `_ZTV9CoreV4Hwm` @ `0x3da6630` (vptr = symbol + 0x10) |
| **Simulator** | `PerfSim` ctor `0x165a7f0`; `perf_estimation` `0x1663f60`; `add_instruction` `0x165fa30` |
| **Event record** | `SimEvent` = 56 B `{start, duration, inst, std::string label}`; `get_event` @ `0x1657810` strides `56*index` |
| **Timeline array** | `all_timelines[12]` = `std::vector<std::vector<SimEvent>>` @ `PerfSim+96`; `SimEngineId` 0..6, 8..0xB, count 0xC |
| **DMA model** | `dma_queues[13]` @ `PerfSim+16`; trigger assert `perf_sim.cpp:654` @ `0x1d83c80` |
| **Critical path** | `get_overall_end` @ `0x1657c50` = max over 12 timelines of `last.start + last.duration` |
| **Learned oracle** | `MLInstructionLatencyModel` ctor `0x183a940`, `getLatency` `0x1833950`; per-opcode `MLSingleOpcodeLatencyModel::forward` `0x182efe0` |
| **Output metric** | `backend::PostSchedEstLatency` string @ `0x1c8a094` (`BackendMetricType` 42 = 0x2A) |

---

## Layer 1 — `bir::Hwm`, the per-instruction latency oracle

### The abstract base is pure-virtual

`bir::Hwm` is declared in `libBIR.so`. Its compiled body is a wall of abort stubs: every per-opcode `getLatency`, the helpers, and the latency-table accessors are pure virtuals that `__assert_fail` when reached on the base. The assert strings are recoverable verbatim from `libBIR.so`, all carrying the `…/neuronxcc/walrus/ir/lib/IR/Hwm.cpp` source path **[CONFIRMED — `strings libBIR.so`]**:

```text
false && "getLatencyHelper not implemented"
false && "getLatencyMemset( unsigned NumElementsPerPartition ) not implemented"
false && "getLatency( const InstLoop & ) not implemented"
false && "getLatency( const InstGenericCopy & ) not implemented"
false && "getSBtoDRAMLatency not implemented"
… (one per opcode / helper)
```

The base therefore carries **no numbers** — it is an interface. Each opcode's cost is a vtable slot; a chip generation supplies its formulas by overriding the slots it cares about, and any opcode a subclass does *not* override hits the base abort and is excluded from cost accounting (control-flow ops — `InstLoop`, `InstDoWhile`, `Return`, barriers — are scheduled on the event model below, not cycle-costed). The base typeinfo is `_ZTIN3bir3HwmE` @ `0x3fd76d8` in `libwalrus.so` **[CONFIRMED — names table]**.

> **CONFIRMED — exactly three concrete subclasses.** `nm`/IDA names of `libwalrus.so` contain `_ZTV`/`_ZTI`/`_ZTS` (vtable/typeinfo/typename) triples for precisely **`TrainiumHwm`, `Gen3Hwm`, `CoreV4Hwm`** and no others. There is no separate `SundaHwm` or `CaymanHwm` class — "Sunda"/"Cayman" are the codenames *served by* `Gen3Hwm`. Each subclass also exposes a `registerSingleton()` (`0x7e4c90` / `0x7e4f80` / `0x7e5270`) that installs it into the registry below.

| Concrete oracle | Codename / generation | ctor | vtable (`_ZTV…`) | vptr (= symbol + 0x10) |
|---|---|---|---|---|
| `TrainiumHwm` | Tonga / trn1 | `0x1853f40` | `0x3da48b0` | `0x3da48c0` |
| `Gen3Hwm` | Sunda · Cayman / trn2 | `0x185b370` | `0x3da5770` | `0x3da5780` |
| `CoreV4Hwm` | CoreV4 · Mariana / trn3 | `0x18606c0` | `0x3da6630` | `0x3da6640` |

> **CORRECTION (vs D-G14 §0).** The backing report cited the Trainium/Gen3 vtables as `off_3DA48C0` / `off_3DA61C0`. The `_ZTV` **symbol** bases are `0x3da48b0` / `0x3da5770`; `0x3da48c0` is the *vptr* (symbol + 0x10, past the offset-to-top and typeinfo header). The report's `0x3DA61C0` for Gen3 does not match the symbol table (`_ZTV7Gen3Hwm` @ `0x3da5770`); use the symbol-table values above. This is the standard two-VA / vptr-base artifact — the formula bodies the report cites are unaffected.

### The singleton registry

A consumer never constructs a `Hwm` directly. `bir::Hwm::getSingleton(std::string const&)` @ `0x61d680` is keyed by codename (`"Trainium"` / `"Gen3"` / `"CoreV4"`), looks the instance up in the map built by `getSingletonMap` @ `0x5f7ce0`, and returns the per-arch oracle. `PerfSim` resolves the singleton once and pre-bakes every instruction's latency into `cost[]` (see [Layer 2](#layer-2-perfsim-the-cycle-simulator)) — there is **no virtual dispatch in the hot timeline walk**.

### `getLatency` composes three phases

Every per-opcode `getLatency(InstX)` sums three vtable-dispatched phases, verified firsthand from `TrainiumHwm::getLatency(InstMatmult)` @ `0x1852af0` **[CONFIRMED — D-G14]**:

```c
// bir::Hwm::getLatency(InstX) — the three-phase composition.  [CONFIRMED 0x1852af0]
// Per-opcode the three phase slots sit 880 bytes apart in the vtable (readInit/exec/
// writeDrain for ONE opcode), and adjacent opcodes are +8 within a phase band.
uint64 getLatency(const InstX &inst) {
    EngineType eng = *(uint *)((char*)&inst + 144);   // Inst+0x90 = bir::EngineType
    uint64 r = readInit_slot(inst, eng);              // vtable readInit band
    r += exec_slot(inst, eng);                        // vtable exec band
    if (writeDrain_slot != abort_stub)                // most opcodes: drain is 0
        r += writeDrain_slot(inst, eng);
    return r;                                          // = readInit + exec + writeDrain
}
```

The phase slots are dispatched through three vtable bands separated by **880 bytes** (the readInit / exec / writeDrain bands), with adjacent opcodes `+8` apart inside a band. For most opcodes `writeDrain` is the base abort stub and contributes 0 — the per-op cost is just `readInit + exec`.

### The shared 100-cycle-unit machinery

Three helpers do the arithmetic for nearly every opcode. They share a "model-cycle" unit: the constant **100** scaled by a per-engine *frequency divisor* (an elements-per-100-model-cycles throughput number, **not** silicon MHz). Verified firsthand from the `TrainiumHwm` bodies **[CONFIRMED — D-G14]**:

```c
// getLatencyHelper @0x1853140  [TrainiumHwm vtable+88]
// NEP = max over (args ∪ outputs that are PhysicalAccessPattern) of
//       AccessPattern::getNumElementsPerPartition(p).  Sign-defensive: if NEP<0,
//       NEP_float = 2*(NEP&1 | NEP>>1).  freq defaults to getEngineFrequency(eng).
uint64 getLatencyHelper(double base, const Inst &i, double freq, ?, double mult) {
    double NEP = max_NEP_over_access_patterns(i);
    return (uint64) floor( (NEP * mult + base) * 100.0 / freq );
}
// getExecLatencyHelper @0x1853640  [vtable+96]  = getLatencyHelper with base = 0
uint64 getExecLatencyHelper(const Inst &i, double freq, ?, double mult)
    { return (uint64) floor( max_NEP(i) * mult * 100.0 / freq ); }
// getOverheadLatencyHelper @0x1853090  [vtable+184]  — constant cost, no NEP scan
uint64 getOverheadLatencyHelper(double base, double freq, ...)
    { return (uint64) floor( base * 100.0 / freq ); }
```

**Engine-frequency divisor table** (`getEngineFrequency`, verified firsthand). The branch tests the internal `bir::EngineType` ordinal: the data/systolic band (`engine ∈ [1,3]`) uses the datapath divisor, the SP/scalar branch (`engine == 5`) uses the SP divisor, and the remaining ordinals abort (they are never the engine on a cost-modeled instruction):

| Engine band | Tonga (`TrainiumHwm`) | Sunda (`Gen3Hwm`) | CoreV4 (`CoreV4Hwm`) | anchor |
|---|---|---|---|---|
| data band `[1..3]` | 140 (`0x8C`) | 120 (`0x78`) | 120 (`0x78`) | `0x1851b30` / `0x1858090` / `0x185e910` |
| SP band (`==5`) | 112 (`0x70`) | 96 (`0x60`) | 120 (`0x78`) | same |
| else | abort | abort | abort | tailcall |

CoreV4 is unified (120 everywhere). These divisors are the throughput knob: a higher divisor means more elements consumed per 100 model-cycles, i.e. lower latency per element.

### Representative concrete formulas

The standalone K-only estimators (used by pre-schedule passes that have a tile's K dimension but no access patterns) bypass the NEP scan and are verified byte-for-byte **[CONFIRMED — D-G14]**:

```c
// TrainiumHwm::getLatencyMatmult(uint K)  @0x1851800  (Gen3 0x1857c30, CoreV4 0x185e4d0)
//   = 20 * (5*(K>>1) + 1000) / freq   ==   (K/2 + 200) * 100 / freq
//   5 cyc per K-pair, K/2 pairs (PE consumes 2 K-elems/array-cycle), 1000 fixed overhead.
//   freq = 140 Tonga | 120 Gen3 | 120 CoreV4.
// TrainiumHwm::getLatencyMemset(uint N)   @0x1851830
//   = (100*N + 5000) / freq            (runs on SP → SP-band divisor 112/96/120)
```

The **DMA / HBM-bandwidth exec** formula (`getLatencyExec(InstDMACopy)` @ `0x1856240` Tonga, `0x185d590` Gen3) converts elements to bytes through a 20-entry dtype-size table and divides by an effective HBM-bandwidth divisor:

```c
// standard path:  exec = ((P+15)>>4) * (dt[d_idx] * NEP) / DIV
//                 DIV  = 17 (0x11) Tonga | 23 (0x17) Gen3    (Gen3 ≈35% faster)
// 1-byte-element fast path (dtype-bits == 8):
//                 exec = NEP * dt[d_idx] * P / DIV8
//                 DIV8 = 272 (0x110) Tonga | 368 (0x170) Gen3
// d_idx = *(uint*)(AP+48) (dtype field), bounds-checked <= 0x13 → llvm_unreachable.
// (P+15)>>4 = the partition-aligned 16-row tile count.
```

The **DGE descriptor-generation overhead** is added to the fixed `1300`-cycle DMA `readInit`. The DGE-type field at `Inst+248` selects: Tonga `NONE→0 / SWDGE→500 / HWDGE→abort` (HW descriptor generation is unsupported on trn1); Gen3 routes each through `getOverheadLatencyHelper` with base `0/700/500`.

The **CoreV4 activation** cost (`getLatency(InstActivation)` @ `0x185fb10`) is `readInit + exec`, where `readInit = getActivationInitialCycles * 100 / 120` with init `= 222` (a 16-bit dtype arg present) / `172` (32-bit) / `100` (default), and `exec = getExecLatencyHelper(freq=120, mult=1)` scaled by a per-element throughput that halves for some dtypes.

The dtype-size table is three bit-identical 20-qword arrays (Tonga `0x1E1B0C0`, Gen3 `0x1E1B1C0`, CoreV4 `0x1E1B2C0`): `[1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8]` (byte sizes of the 20 BIR dtype codes), consumed by every byte-rate exec formula.

> The full per-opcode formula catalogue (TensorTensor / Select / GenericCopy / CollectiveCompute / Rand / IndirectLoad and the `mult ∈ {1.0, 2.0, 3.0}` dtype selectors) is exhaustively tabulated in the D-G14 backing pass; the items above are the structurally representative ones a reimplementer needs to rebuild the helper machinery.

---

## Layer 2 — `PerfSim`, the cycle simulator

`PerfSim` is a per-basic-block, single-forward-pass simulator. It never reorders — it consumes the schedule the upstream `pre_sched`/`post_sched` produced and **measures** it. The model is greedy-as-late-as-required: each instruction starts at the maximum of its dependency-ready time, its engine's free time, its PSUM-bank free time, its paired-band free time, and (for DMA triggers) its descriptor-queue free time.

### The `SimEngineId` enum and `all_timelines[12]`

The simulator keys everything off a twelve-valued engine identifier. The switch in `simEngineId2string` @ `0x1657410` has cases **0,1,2,3,4,5,6, then 8,9,0xA,0xB,0xC** — a deliberate **gap at 7** **[CONFIRMED — decompiled body]**:

```c
enum class SimEngineId : uint32 {           // simEngineId2string @0x1657410 switch
    Unassigned               = 0,           // "Unassigned"
    Pool                     = 1,           // "Pool"
    Activation               = 2,           // "Activation"
    PE                       = 3,           // "PE"   (the systolic matmult array)
    DMA                      = 4,           // "DMA"
    DVE                      = 5,           // "DVE"
    SP                       = 6,           // "SP"   (sync / GPSIMD scalar proc)
    /* 7 : NO STRING — synthetic FULL-BARRIER sentinel (see determine_start_time) */
    CollectiveComputeStream0 = 8,           // "CollectiveComputeStream0"
    CollectiveComputeStream1 = 9,           // "CollectiveComputeStream1"
    CollectiveComputeStream2 = 0xA,         // "CollectiveComputeStream2"
    CollectiveComputeStream3 = 0xB,         // "CollectiveComputeStream3"
    NumSimEngineIds          = 0xC,         // "NumSimEngineIds"  (== 12, the array size)
};
```

`is_collective_compute_sim_engine_id(id) == ((unsigned)(id - 8) <= 3)` — the string `is_collective_compute_sim_engine_id(engine_id)` appears verbatim in the assertion at `perf_sim.cpp:96` **[CONFIRMED — strings]**.

> **CONFIRMED — `all_timelines` has twelve elements.** `PerfSim::init` @ `0x1663850` allocates `all_timelines` (a `std::vector<std::vector<SimEvent>>`) sized to **12** = `NumSimEngineIds`, the vector handle at `PerfSim+96`. `get_overall_end` @ `0x1657c50` iterates exactly these twelve inner vectors (`this+96`, inner-handle stride 24). The DMA-queue vector is a separate, **13**-element array (queue ids 0..12) at `PerfSim+16`.

The instruction → engine map lives only in `get_timeline` @ `0x1657730` / `get_sim_engine_id` @ `0x1664d90`; everything else in the simulator is engine-id-agnostic. The decision is on the BIR opcode (`Inst+0x58`), with a quirk worth pinning:

```c
// get_timeline @0x1657730 — opcode → all_timelines slot (begin = PerfSim+96, stride 24).
//   opcode 18                  -> slot 4  (DMA)
//   opcode in byte_1E02420 (the 10 "engine-typed" ops):
//       opcode == 67           -> slot = EngineType field (Inst+0x90)   // ONLY 67 routes by engine
//       else                   -> slot 4  (DMA)
//   opcode 47                  -> slot 8  (CollectiveComputeStream0)
//   opcode in {48,49,50}       -> CC stream 8 + (Inst+0x140 <= 3 ? that : ...) or DMA (local form)
//   default                    -> slot = EngineType field (Inst+0x90)   // Pool/Act/PE/DVE/SP
```

> **CORRECTION (adopted from D-AD01 verification pass).** The `byte_1E02420` engine-typed-opcode set is `{19, 22, 32, 41, 42, 43, 44, 45, 46, 67}` — verified by `xxd @0x1E02420` (bytes `==1` at indices 0,3,13,22,23,24,25,26,27,48). The D-AD01 *draft* had listed `{…40…68}`; the truth is `41` and `67`. Among the ten, **only opcode 67 (`InstDMATrigger`) routes by the `EngineType` field**; the other nine land on the DMA timeline. The final `default` fallthrough routes by `EngineType`, **not** to DMA. Do not assume opcode == engine in this build.

### `SimEvent` is 56 bytes

Each instruction becomes exactly one `SimEvent`. The record is **56 bytes** — confirmed firsthand: `get_event` @ `0x1657810` returns `*get_timeline(inst) + 56*index`, and every timeline realloc copy loop strides 56 **[CONFIRMED — decompiled `get_event`: `… + 56LL * index`]**:

```c
struct SimEvent {                 // sizeof == 56 (0x38)   [CONFIRMED 0x1657810]
    uint64            start_time; // [+0]  cycle this op begins
    uint64            duration;   // [+8]  == cost (cycles); end = start + duration
    bir::Instruction *inst;       // [+16] the op
    std::string       label;      // [+24] SSO std::string (24 + 32 = 56);
                                  //       append sets it empty (ptr 0, inline buf)
};
// get_timeline_end(tl) @0x1657c30 = tl.empty()? 0 : tl.back().start + tl.back().duration
// get_event(I)         @0x1657810 = &get_timeline(I)[ timeline_event_indices[I] ]
//                                 =  base + 56 * index   (the literal 56LL multiply)
```

A timeline is `std::vector<SimEvent>` (24-byte handle); `all_timelines` is `std::vector<std::vector<SimEvent>>` sized 12. The `timeline_event_indices` `DenseMap<const Inst*, uint64>` at `PerfSim+208` lets any instruction's event be found O(1).

### `PerfSim` object layout (the fields the algorithm reads)

Triangulated from the ctor (`0x165a7f0`), `init`, and every field read in `add_instruction` / `determine_start_time` / `get_cost` **[mix of CONFIRMED / STRONG — see D-AD01 V8]**:

| off | field | role |
|---|---|---|
| +16 | `dma_queues.begin` | `std::vector<SimDMAQueue>` (88 B/elem), **13** elems |
| +40 | `dword[10]` | CC-bandwidth selector (init writes `max(Inst+0x140)`; read as arch class — see open item) |
| +48 | `dword[12]` arch_version | `= Module[+0xAC]`; `<= 29` gates the Pool↔DVE band coupling |
| +56 | `qword[7]` = **1300** | DMA dispatch-overlap window (`xmmword_1DD7AC0` lo) |
| +64 | `qword[8]` = **100** | compute dispatch-overlap window (`xmmword_1DD7AC0` hi) |
| +72 | `cost.begin` | `std::vector<uint64> cost[]`; index = `Inst+0x4C` (`dword[19]`) |
| +96 | `all_timelines.begin` | `std::vector<std::vector<SimEvent>>`, **12** elems |
| +120 | `per_engine_psum_writer_end[12]` | per-engine PSUM writer end times (read by `determine_start_time`) |
| +144 | `psum_bank_dispatch_map` | `unordered_map<bank, next_dispatch>` — the cross-engine PSUM semaphore surrogate |
| +160 | `psum_writer_chain` head | fallback linked list when the bank vector is empty |
| +208 | `timeline_event_indices` | `DenseMap<const Inst*, uint64>` (event index within its timeline) |
| +232 | `saved_timeline_sizes[12]` | `save_state`/`rollback_state` checkpoint (speculative-scheduling undo) |
| +276 | `float[69]` = ctor `a3` | collective-overlap weight A |
| +280 | `float[70]` = `a4 ? a3 : 0.25` | collective-overlap weight B |
| +285 | `include_anti_deps` = ctor `a5` | gate in `inst_ready_time`: honor anti-dep (kind-2) edges iff set |

`xmmword_1DD7AC0` @ `0x1DD7AC0` = `14 05 00 00 … 64 00 00 00 …` = `{1300, 100}` (two uint64) **[CONFIRMED — `xxd`]**.

### `add_instruction` — the scheduling core

`add_instruction(Inst *I, uint64 floor, SimEngineId override, Inst *dma_xfer)` @ `0x165fa30` places one instruction. In execution order **[CONFIRMED — D-AD01 §3 / V4, all 1068 lines re-read]**:

```c
void add_instruction(Inst *I, uint64 floor, SimEngineId override, Inst *xfer) {
    auto &tl  = override ? all_timelines[override] : *get_timeline(I);
    uint64 ready = inst_ready_time(I);                  // dependency stall (below)
    SimEngineId e = override ? override : get_sim_engine_id(I);
    uint64 start  = determine_start_time(ready, e,      // engine/PSUM/band serialization
                                         has_reader_banks, has_writer_banks, writer_banks);
    start = max(start, floor);                          // external floor a3
    uint64 cost = get_cost(I);                          // cost[I->cost_id] (/bw if collective)

    if (I->opcode == 48 /*InstCollectiveCompute*/)      // bandwidth-share reflow
        reflow_overlapping_collectives(tl, start, cost);

    if (I->opcode == 67 /*InstDMATrigger*/) {           // === THE DMA-TRIGGER CONTRACT ===
        assert(xfer != nullptr);                        // perf_sim.cpp:654, NeuronAssertion 1889
        SimDMAQueue &q = dma_queues[ get_dma_queue_id(xfer, e) ];
        if (q.size() >= q.max_depth) {                  // ring full → wait for a slot
            Inst *head = q.front().second;              // pair.second = the transfer inst
            start = max(start, get_event_next_dispatch(get_event(head)));
            q.pop_front();                              // advance the descriptor ring
        }
        q.push_back({ I /*trigger*/, xfer /*transfer*/ });
    }

    tl.push_back(SimEvent{ start, cost, I, /*label*/"" });
    timeline_event_indices[I] = tl.size() - 1;
    if (cost) {                                         // advance PSUM-bank dispatch times
        if (has_writer_banks) per_engine_psum_writer_end[e] = get_timeline_end(tl);
        uint64 nd = get_timeline_next_dispatch(tl);
        for (bank : reader_writer_banks(I)) psum_bank_dispatch_map[bank] = nd;
    }
}
```

The DMA-trigger assertion is real: the strings `PerfSim::add_instruction requires the DMA transfer instruction as an argument when adding a DMA trigger.` and `…/perf_sim.cpp:654` are both present in `libwalrus.so` (`perf_sim.cpp:654` @ `0x1d83c80`) **[CONFIRMED — strings]**. The queue element is a `std::pair<trigger_inst, transfer_inst>`: the trigger is zero-cost and fast, the transfer lands later, and the simulator tracks both halves through the queue and the `inst_ready_time` recursion below.

### Dependency stall and the asynchronous-DMA semantics

`inst_ready_time` @ `0x16578c0` walks the producer-edge list and returns the max over producers of each producer's completion time — except a DMA-trigger producer contributes its **issue** time, and the simulator recurses into the paired transfer **[CONFIRMED — D-AD01 §9 / V7]**:

```c
uint64 inst_ready_time(Inst *I) {                       // 0x16578c0
    uint64 ready = 0; int region = I->region;
    for (edge : I->uses) {
        if (!(this->include_anti_deps || (edge.kind != 2))) continue;  // a5 gate on kind-2
        Inst *p = edge.producer;
        if (p->region != region || p->opcode == 105 /*Loop barrier*/) continue;
        SimEvent *ev = get_event(p);  if (!ev) continue;
        uint64 t = ev->start_time;
        if (p->opcode != 67)            t += ev->duration;             // true completion
        if ((edge.kind & 7) == 1) {                                    // trigger/queue dep
            t = get_event_next_dispatch(ev);                           // ISSUE time, not end
            if ((I->opcode == 48 || get_sim_engine_id(I) == 1) && p->opcode == 48)
                t = min(t, inst_ready_time(ev->inst /*the xfer inst*/));// recurse into transfer
        }
        ready = max(ready, t);
    }
    return ready;
}
```

`get_event_next_dispatch` @ `0x1657650` is the **issue-throughput vs latency** distinction: an engine can issue its *next* op before the current op completes, by an overlap window — **100 cycles for compute engines, 1300 for DMA**, zero for CC streams and `InstDMATrigger` **[CONFIRMED — D-AD01 V3; note the draft had these swapped]**:

```c
uint64 get_event_next_dispatch(SimEvent &ev) {          // 0x1657650
    uint64 end = ev.start_time + ev.duration;
    if (ev.duration <= 100) return end;                 // small ops: no overlap
    SimEngineId eng = get_sim_engine_id(ev.inst);
    if (eng == 4 /*DMA*/)      return ev.duration < 1300 ? end : end - 1300;
    if (eng >= 8 && eng <= 11) return end;              // CC streams fully serial
    return (ev.inst->opcode == 67) ? end : end - 100;   // compute pipelines by 100
}
```

### Engine-parallelism and serialization

`determine_start_time` @ `0x165ad90` folds every serialization source into the start time **[CONFIRMED — D-AD01 §6 / V5]**:

```c
uint64 determine_start_time(uint64 ready, SimEngineId e,
                            bool has_reader, bool has_writer, vec<uint64> &wbanks) {
    uint64 t;
    if (e == 7) {                                       // synthetic FULL-BARRIER sentinel
        t = 0;                                          // = max over {Act,DVE,PE,Pool,SP}
        for (int sub : {2, 5, 3, 1, 6})                 // = xmmword_1DD7AD0 {2,5,3,1} ++ {6}
            t = max(t, get_timeline_next_dispatch(all_timelines[sub]));
    } else
        t = get_timeline_next_dispatch(all_timelines[e]);
    uint64 start = max(ready, t);                        // can't start before engine is free
    if (has_writer) {                                    // PSUM-bank serialization
        if (!wbanks.empty()) for (b : wbanks) start = max(start, psum_bank_dispatch_map[b]);
        else                 for (n : psum_writer_chain) start = max(start, n.time);
    }
    if (arch_version <= 29) {                            // engine-band coupling regime
        if (e == 5) start = max(start, get_timeline_next_dispatch(all_timelines[1])); // DVE waits Pool
        if (e == 1) start = max(start, get_timeline_next_dispatch(all_timelines[5])); // Pool waits DVE
        if (e == 2) start = max(start, per_engine_psum_writer_end[5],                 // Act waits DVE,
                                       per_engine_psum_writer_end[1]);                 //   Pool ends
    }
    return start;
}
```

`xmmword_1DD7AD0` @ `0x1DD7AD0` = `02 00 00 00 05 00 00 00 03 00 00 00 01 00 00 00` = int32 `{2,5,3,1}` = `{Activation, DVE, PE, Pool}`; `determine_start_time` appends a literal `6` (SP) → the engine-7 barrier set is `{Act, DVE, PE, Pool, SP}` **[CONFIRMED — `xxd`]**. Pool (1) and DVE (5) **mutually serialize** on `arch_version <= 29` (they share an issue port); Activation additionally clamps to the Pool/DVE writer-ends. Concurrency is *emergent*: two ops on different engines whose maxes don't reference each other proceed in parallel.

### Cost readout and the collective bandwidth divide

`get_cost` @ `0x165ba70` is the L1→L2 bridge — a flat array index, with a runtime divide only for collectives:

```c
uint64 get_cost(Inst *I) {                              // 0x165ba70
    uint64 base = cost[ I->cost_id ];                   // cost_id = Inst+0x4C
    if (I->opcode != 48) return base;                   // regular op: just the L1 latency
    double bw = get_collective_compute_stream_bandwidth(get_sim_engine_id(I));
    return (uint64)((double)base / bw);                 // CC: cost scaled by 1/bandwidth
}
```

`get_collective_compute_stream_bandwidth` @ `0x165b000` (asserts `eng ∈ 8..11`, `perf_sim.cpp:96`) returns the per-arch HBM-share fraction **[CONFIRMED — D-AD01 §8 / V6]**:

```c
// arch 0 (Trainium): 1.0 for all CC streams
// arch 1 (Gen3):     eng8 -> 0.75, eng9 -> 0.25  (streams 10/11 → fatal :108/:121)
// arch 2 (CoreV4):   flt_1E02410[eng-8] = { 0.5, 0.25, 0.25, 0.0 }
// arch 3+:           -1.0 (unmodelled sentinel)
```

`flt_1E02410` @ `0x1E02410` = `00 00 00 3f · 00 00 80 3e · 00 00 80 3e · 00 00 00 00` = `{0.5, 0.25, 0.25, 0.0}` **[CONFIRMED — `xxd`]**. A 0.25-bandwidth stream therefore costs 4× its nominal latency — it shares the HBM link four ways.

> **OPEN ITEM (honestly flagged, INFERRED selector).** The field the bandwidth switch reads is `PerfSim+40`, but `init` *also* writes `+40` with `max(Inst+0x140)` (the CC stream-id) over the block's collective ops. The switch **values** (`1.0` / `{0.75,0.25}` / `{0.5,0.25,0.25,0}` / `-1`) unmistakably match the three Hwm arch families, so the arch interpretation is kept; the precise selector semantics of `+40` is the one unresolved offset in the reconstruction. The genuine `arch_version` is at `+48` (`= Module[+0xAC]`).

### The DMA-queue model

`get_dma_queue_id` @ `0x16588e0` maps a DMA/collective instruction to one of 13 lanes by opcode + DMA-kind (`Inst+0xF8`) + triggering engine + tensor direction. The two lanes with finite descriptor depth are seeded by `init`: queue 10 (`max_depth = 4`) and queue 12 (`max_depth = 1`); all others default to 0 **[CONFIRMED — D-AD01 V12]**. `is_dma_queue_ready` @ `0x16590a0` is a finite-descriptor-ring, **issue-throughput** check (not a full-completion barrier): a DMA issues when its queue is under-full, or when the oldest in-flight descriptor has reached its next-dispatch time.

### Critical path and the program-order walk

`perf_estimation` @ `0x1663f60` drives the whole thing **[CONFIRMED — D-AD01 §10 / V10]**:

```c
uint64 perf_estimation(BasicBlock *bb, bool fresh) {    // 0x1663f60
    if (fresh) { init(bb);                              // alloc 12 timelines, 13 dma_queues
                 hwm_cost_setup(bb); }                  // TBB-parallel L1 oracle → cost[]
    for (Inst *I : bb->instructions /*program order*/) {
        if (I->opcode == 105 /*InstLoop*/) {
            assert(I->loop.isSchedulingUnit());         // perf_sim.cpp:488
            for (BasicBlock *body : I->loop.blocks())
                perf_estimation(body, /*fresh=*/false); // recurse on the SAME timelines
        } else
            add_instruction(I, 0, /*override*/0, /*xfer*/0);
    }
    return get_overall_end();                           // the BB critical path
}
uint64 get_overall_end() {                              // 0x1657c50
    uint64 r = 0;
    for (auto &tl : all_timelines /*12*/)               // iterate this+96, stride 24
        r = max(r, get_timeline_end(tl));               // last.start + last.duration
    return r;                                           // MAX, not SUM — parallelism captured
}
```

`hwm_cost_setup` @ `0x1658610` is the L1→L2 bridge: a one-time, TBB-parallel pass that runs the `Hwm` oracle for every instruction and scatters the results into `cost[]`, so the serial timeline walk does **no virtual dispatch**. Loop bodies are timed by recursion on the same timelines, so a loop's contribution is its per-iteration critical path; the `×trip-count` multiply to a whole-program estimate happens one layer up in `PerfSimPass::run` ([8.46](#cross-references)), which writes the result as `backend::PostSchedEstLatency` (string @ `0x1c8a094`, `BackendMetricType` 42).

`PerfSimPass` runs `perf_estimation` **twice** — two `PerfSim` objects differing only in ctor arg `a5` → `include_anti_deps` (`PerfSim+285`): `a5=1` honors anti-dependency edges (the conservative/real schedule), `a5=0` ignores them (the ideal schedule). Their ratio is the anti-dependency scheduling slack.

---

## Layer 3 — the learned oracle (`MLInstructionLatencyModel`)

When the global `--ml-model-directory` string is non-empty, `PerfSim` constructs an `MLInstructionLatencyModel` (ctor @ `0x183a940`, `getLatency` @ `0x1833950`) and `hwm_cost_setup` uses its prediction *in place of* `Hwm::getLatency`, logging `Using ML latency … instead of getLatency …` on divergence. The learned oracle is a per-opcode regression **[CONFIRMED — symbols; MED on internal structure]**:

- `MLInstructionLatencyModel::getLatency` extracts a feature vector and dispatches to a per-opcode `MLSingleOpcodeLatencyModel` (a `std::map` keyed by `InstructionType` name).
- `MLSingleOpcodeLatencyModel::forward` @ `0x182efe0` is the per-opcode regression. Its helpers — `serialize_feature` @ `0x18318d0`, `serialize_access_pattern` @ `0x1830dc0`, `get_one_hot_from_feature_enums` @ `0x182ded0`, `get_embedding_from_serialization` @ `0x182e2f0` — show the model is **JSON-backed** (the symbols carry `nlohmann::json` parameter types): the weights are loaded from a JSON bundle and indexed by one-hot/embedding feature buckets, returning the estimate in the same 100-cycle units. Weight files are matched by the regex `^latency_models_([0-9]+).*\.npy`.

The same bundle is consumed by the `TimeAware` scheduler (`InstCostMode == File`, `file_cost_setup` @ `0x165bb60`), so when both are enabled the perf-sim estimate is self-consistent with the schedule it measures. The internal regression form (linear vs MLP, the per-feature bucketization) is **INFERRED** from the symbol shapes — not byte-decoded.

---

## Cross-references

- **[8.11 — `post_sched` and the Three Schedulers](post-sched-schedulers.md)** — the `TimeAware` scheduler that *consumes* this cost model (and shares the learned bundle via `file_cost_setup`).
- **[8.18 — SBUF Allocator: Spill-Cost, Simplify, Select & Spill-Code](sbuf-spill-select.md)** — the spill-cost consumer that reads `Hwm` latencies to price candidate spills.
- **8.46 — `perf_sim` harness wiring + `pttf` trace** *(in flight)* — `PerfSimPass`/`PerfSimPackagePass`, the `×modular_flow_runs` multiply, the `addMetric(42, …)` sink, and the Chrome/Perfetto trace emission. This page owns the cost model and the simulator core; the harness that drives them and bundles their traces is documented there.

## Verification ceiling

Independently re-confirmed against `libwalrus.so` / `libBIR.so` this pass: the **three** concrete `Hwm` vtables/typeinfos (and the absence of a fourth); the abstract base's `Hwm.cpp` `"…not implemented"` aborts in `libBIR.so`; the four constant tables by `xxd` (`xmmword_1DD7AC0` = `{1300,100}`, `xmmword_1DD7AD0` = `{2,5,3,1}`, `flt_1E02410` = `{0.5,0.25,0.25,0}`, `byte_1E02420` engine-typed set `{19,22,32,41..46,67}`); the `SimEvent` 56-byte stride (`get_event`'s literal `56*index`); the `SimEngineId` switch with its gap at 7 and `NumSimEngineIds` = 0xC; the twelve-timeline iteration in `get_overall_end`; and the DMA-trigger assert string at `perf_sim.cpp:654` / `0x1c8a094` `backend::PostSchedEstLatency`. The per-opcode formula constants (helpers, DMA divisors, DGE overheads, activation init) are adopted from the firsthand D-G14 pass and spot-checked at the symbol level. The single unresolved offset is the `PerfSim+40` CC-bandwidth selector (arch-class vs max-CC-stream-id aliasing) — flagged INFERRED above. The `MLInstructionLatencyModel` regression internals are confirmed to exist (symbols, JSON parameter types) but **not** byte-decoded — MED.
