# DMA Engine Binding — `assign_trigger_engine` and `assign_hwdge_engine`

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311 exports are byte-identical, cp312 VAs drift but enum ordinals and field offsets are ABI-stable). Both passes live in `neuronxcc/starfish/lib/libwalrus.so` (`.text` base `0x62d660`, `.rodata` base `0x1c72000`, both VA == file offset; `.data` adds a `+0x400000` delta). The real pass bodies sit high (`> 0x1179xxx`); the `0x5e9020`–`0x62d650` band is `.plt` thunks, so a symbol that decompiles to an 8-line forwarder is a thunk, not the body. `bir::isHWDGEDMAWithEngineSet` / `bir::isIODMA` / the `bir::InstructionType` and `DGEType`/`EngineType` enums live in `libBIR.so`. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

A neuron DMA is never "run by a DMA engine" in isolation. A **compute** engine (Pool, Activation, PE, DVE, or SP) issues the DMA *trigger*, and — for a hardware-descriptor-generation (HW-DGE) DMA — a compute engine also *builds the descriptor list on device*. Two adjacent backend passes pin both bindings into the per-`InstDMA` engine field (`+0x90` engine-id, `+0x94` sub-id). `assign_trigger_engine` (pass order **110**) chooses which compute engine *triggers* each Load / Save / DMACopy; `assign_hwdge_engine` (pass order **123**) makes the engine of an HW-DGE DMA a *legal descriptor-gen engine*. Pass **124** (`alloc_queues`) then turns each `(engine, QoS)` pair into one DMA queue, and the CoreV2 encoder serializes the trigger op onto the engine these passes chose.

The headline of pass 110 is the **trigger-follows-producer** rule: a save DMA is triggered by the engine that *produced its source data*, so the engine that just finished writing the source tile fires the DMA and no cross-engine synchronization is needed before it triggers. Collective-compute (CC) DMAs are the special case — a DMA feeding or draining a collective op is triggered by the *same engine that runs the collective*, so the DMA and the CC op share an engine queue. The headline of pass 123 is the **descriptor-gen engine selection**: the hardware exposes several engines that can emit HW-DGE descriptors, and the pass either keeps an already-legal engine, deterministically picks SP-or-Activation by current engine class on older silicon (arch ≤ 49), or **round-robin load-balances** across a target-supplied `ValidEngines` vector on `core_v5` (arch ≥ 50).

Neither pass decides *SWDGE vs HWDGE*. That tag (`DGEType @InstDMA+0xF8`) is computed upstream by a pure classifier (`getDGEInstructionType`) and persisted by the DMA-optimization / dynamic-DMA passes; pass 123 only *gates* on `DGEType == HWDGE` and assigns the engine. The engine write it performs is precisely what flips `libBIR`'s `isHWDGEDMAWithEngineSet` predicate from false to true — there is no separate `EngineSet` container; the "engine set" **is** the `+0x90` dword.

For reimplementation, the contract is:

- The `InstDMA` engine-binding field layout: `+0x90` engine-id (low u32 = `EngineType`), `+0x94` sub-id, `+0xF8` `DGEType`, `+0x38` DMA-kind dword — the four fields the two passes read and write.
- The pass-110 selection policy: re-home only DMAs currently on the abstract `EngineType::DMA` (4); Load → CC-op engine, Save → latest-producer engine; then the IO-queue-limiting tail that coalesces non-HWDGE IO DMAs onto SP when more than one HW-DGE engine is in play.
- The pass-123 worker: the `DGEType==HWDGE` gate, the dedup-against-`ValidEngines` fast path, and the two engine-selection arms (deterministic SP/Act for arch ≤ 49 with PE rejected; round-robin for `core_v5`), plus the hard `ValidEngines.size() >= 2` invariant.

| | |
|---|---|
| **Pass 110** | `assign_trigger_engine` — orchestrator `AssignTriggerEngine::run(vector&)` `@0x117be10` (564 lines) |
| **Pass 123** | `assign_hwdge_engine` — orchestrator `AssignHWDGEEngine::run(vector&)` `@0x117fe00`; worker `assignEngine` `@0x1180360` (114 lines) |
| **Pass order** | 110 (`assign_trigger_engine`) · 123 (`assign_hwdge_engine`) · 124 (`alloc_queues`) |
| **Engine field** | `InstDMA+0x90` engine-id (low u32 = `EngineType`) · `+0x94` sub-id · `+0xF8` `DGEType` · `+0x38` DMA-kind |
| **`EngineType`** | `{0 Unassigned, 1 Pool, 2 Activation, 3 PE, 4 DMA, 5 DVE, 6 SP, 7 ALL}` (D-D03; abstract `DMA=4` is the lowering default these passes re-home) |
| **`DGEType`** | `{0 None, 1 SWDGE, 2 HWDGE, 3 Unassigned}` — gate value for both passes is `HWDGE = 2` |
| **DMA family** | `byte_1DF3660` selector table (`.rodata`, 49 entries, index = IT−19) → `IT ∈ {18,19,22,32,41-46,67}` |

The two source-path strings baked into the bodies — `neuronxcc/walrus/assign_trigger_engine/src/assign_trigger_engine.cpp` and `neuronxcc/walrus/assign_hwdge_engine/src/assign_hwdge_engine.cpp` — anchor every function on this page; both were read out of `libwalrus.so` directly. CONFIRMED.

---

## The shared field layout these passes pin

Both passes operate on one `InstDMA` structure and touch four fields. Nailing the layout once makes every algorithm below readable.

| Field | Offset | Decoded as | Meaning | Confidence |
|---|---|---|---|---|
| Instruction type | `+0x58` | `*((uint*)inst + 22)` | `bir::InstructionType` — selects the DMA family (D-D01) | CERTAIN |
| DMA-kind | `+0x38` | `inst[14]` (dword) | DMA-kind classification; `== 2` enables the dedup-vs-`ValidEngines` fast path in pass 123 | HIGH |
| Engine-id | `+0x90` | `inst[36]`, written as a **qword** | The engine handle; **low u32 = `EngineType` ordinal**. The trigger engine (pass 110) and the HW-DGE descriptor-gen engine (pass 123) both land here | CERTAIN |
| Engine sub-id | `+0x94` | `inst[37]` | Engine sub-id companion (the high half of the `+0x90` qword) | HIGH |
| `DGEType` | `+0xF8` | `inst[62]` (dword) | `{None0, SWDGE1, HWDGE2, Unassigned3}` — the **assign gate** for pass 123 and the HWDGE counter in pass 110's IO-limit check | CERTAIN |

> **NOTE —** `+0x90` is written as a 64-bit store but consumed as a 32-bit `EngineType` in the predicate path. `libBIR`'s `isHWDGEDMAWithEngineSet` tests `(*(u32*)(inst+0x90) & ~4) != 0` — i.e. "the engine field holds a value other than the bare `DMA=4` bit." Because pass 123 always writes a *real* compute engine (SP/Act/round-robin, none of which is 4 alone), that write is exactly what makes the predicate true. The "engine set" is not a separate `std::set`/`SmallVector` member; it is this dword.

### The DMA-family selector table `byte_1DF3660`

Three of the pass-110 leaf visitors (`AssignSaves`, `CheckLimitIOQueues`, `LimitIOQueues`) gate on the *same* idiom: a single-byte lookup table at `.rodata` VA `0x1DF3660`, indexed by `IT − 19`, plus an `IT == 18` (`InstDMA` base) special-case.

```c
// the DMA-family gate shared by AssignSaves / CheckLimitIOQueues / LimitIOQueues
IT = *((uint*)inst + 22);                       // InstDMA+0x58 (D-D01)
is_dma_family = (IT == 18)                       // InstDMA base
             || ((uint)(IT - 19) <= 0x30         // index in [0, 48]
                 && byte_1DF3660[IT - 19] != 0);  // .rodata @0x1DF3660
```

Read directly off `.rodata` (49 bytes at file offset `0x1DF3660`, which equals the VA):

```text
01 00 00 01 00 00 00 00 00 00 00 00 00 01 00 00
00 00 00 00 00 00 01 01 01 01 01 01 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
01
nonzero indices (= IT−19): {0, 3, 13, 22, 23, 24, 25, 26, 27, 48}
→ IT set: {19 Load, 22 Save, 32 DMACopy, 41,42,43,44,45,46 (collective/DMA variants), 67 DMATrigger}
```

With the `IT == 18` special-case this is exactly the D-D01 DMA family `{18,19,22,32,41-46,67}`. CONFIRMED — table bytes read out of the binary; the family reproduces D-D01 with no discrepancy.

---

## Pass 110 — `assign_trigger_engine`

### Purpose

Pick the **compute** engine that triggers each DMA. Lowering leaves DMAs on the abstract `EngineType::DMA` (4); this pass re-homes them onto a real compute engine following dataflow, so the trigger fires from the engine that owns the relevant data and no extra cross-engine barrier is needed.

### Entry Point

```text
AssignTriggerEngine::run(vector<unique_ptr<Module>>&)   0x117be10  ── orchestrator (564 ln)
  per BB, per instruction:
    AssignIdsAndCcLocs::visitInstruction                0x117a610  ── build scheduled-ID map + CC storage maps
      └─ sub_117A0F0                                    0x117a0f0  ── IT==48 (CollectiveCompute): build CC-result / CC-arg maps
    AssignSaves::visitInstruction                       0x117bd50  ── DMA-engine dispatch (gate inst+0x90 == 4)
      ├─ tryAssignToCCEngine                            0x117a060  ── CC-engine path  → sub_1179CF0 (0x1179cf0)
      └─ assignSaveToProducerEngine                     0x117b960  ── PRODUCER path (tail-jump)
      ·  assignLoadToConsumerEngine                     0x117cd00  ── consumer path — DEAD (0 callers in this build)
  after the walk (IO-queue-limit tail):
    CheckLimitIOQueues::visitInstruction                0x117a090  ── count distinct HWDGE engines into a set
    CheckLimitIOQueues::shouldLimitIOQueues()           0x1179ca0  ── return (set.size() > 1)
    LimitIOQueues::visitInstruction                     0x1179cb0  ── force non-HWDGE IO DMAs → SP(6)
```

### Algorithm

The orchestrator walks each module's functions, constructing two sub-visitors *per instruction*. `AssignIdsAndCcLocs` builds the prerequisites; `AssignSaves` does the engine selection. Two counters accumulate moved-DMA totals streamed into the diagnostics `"Moved <n> DMA instructions to CC's engines."` and `"Assigned trigger engine for <n> DMA instructions. Moved …"` (both strings verbatim in `.rodata`).

```c
function AssignTriggerEngine_run(modules):              // 0x117be10
    for M in modules:
        for BB in M.functions().blocks():
            for inst in BB:
                AssignIdsAndCcLocs(inst, &ccResultMap, &ccArgMap)  // 0x117a7c0 ctor
                AssignSaves(inst)                                  // 0x117bdc0 ctor — runs visit
        // accumulate moved-to-CC (v80) and assigned (v81) counts
    // IO-queue-limit tail (lines 421-527):
    if (unk_3E03CD8                                  // global force/debug flag (GAP G3)
        || CheckLimitIOQueues().shouldLimitIOQueues())
        LimitIOQueues().visit(modules);              // logs "Limiting IO queue to SP only"
```

#### Stage 1: build the scheduled-ID and CC maps (`AssignIdsAndCcLocs`)

For *every* instruction, `visitInstruction` `@0x117a610` inserts `(inst → ID)` into a `DenseMap<Instruction const*, unsigned long>`, where `ID` is a monotonically-increasing per-walk counter (visitor `+0x8`, bumped on each insert). This ID **is the scheduled order**, and the producer/consumer selection later picks "latest producer before me" / "earliest consumer after me" by comparing these IDs.

```c
function AssignIdsAndCcLocs_visit(inst):                // 0x117a610
    idMap[inst] = counter++;                            // visitor+0x8; ID = scheduled order
    if (inst[+0x58] == 48)                              // IT == InstCollectiveCompute (D-D09)
        record_cc_locations(inst);                      // sub_117A0F0
```

`record_cc_locations` `@0x117a0f0` populates two `DenseMap<StorageBase const*, Instruction const*>` maps: a **CC-result** map (each CC op's output `StorageBase` → the CC inst) and a **CC-argument** map (each CC op's input-arg `StorageBase` → the CC inst). These are exactly the lookups `tryAssignToCCEngine` performs.

#### Stage 2: the trigger-engine dispatch (`AssignSaves`)

```c
function AssignSaves_visit(inst):                       // 0x117bd50
    IT = inst[+0x58];
    if (is_dma_family(IT))                               // byte_1DF3660: 18,19,22,32,41-46,67
        if (inst[+0x90] == 4)                            // current engine == abstract DMA(4)
            if (!tryAssignToCCEngine(inst))             // call 0x117a060
                assignSaveToProducerEngine(inst);       // tail-jump 0x117b960
```

> **QUIRK —** only DMAs *currently bound to the abstract `EngineType::DMA` (4)* are re-homed. A DMA that already carries a concrete engine (e.g. one a prior pass placed) is left alone. The `cmp DWORD PTR [rsi+0x90], 0x4` / `jne` at `0x117bd75` is the entire filter — get this wrong and you will stomp engines another pass deliberately chose. CONFIRMED from disassembly: the dispatch is exactly one `call tryAssignToCCEngine` then a tail-`jmp assignSaveToProducerEngine`.

#### Stage 3a: the CC-engine path (`tryAssignToCCEngine` — the load case)

```c
function tryAssignToCCEngine(inst):                     // 0x117a060 → sub_1179CF0
    if (inst[+0x58] != 19 && inst[+0x58] != 32)         // Load(19) || DMACopy(32) only
        return 0;
    for sb in storage_bases_of(inst.accessPatterns):
        if (cc = ccResultMap.find(sb)) or (cc = ccArgMap.find(sb)):
            inst[+0x90] = cc[+0x90];                     // DMA trigger engine = the CC op's engine
            ++movedToCcCount;                            // visitor+0x18
            return 1;
    return 0;                                            // no CC storage touched
```

This is the **CC-engine special case**: a DMA that moves data into or out of a collective-compute op is triggered by the *same engine that runs the collective*, so the DMA and the CC op share an engine queue and execute coherently.

#### Stage 3b: the producer path (`assignSaveToProducerEngine` — the default)

```c
function assignSaveToProducerEngine(inst):              // 0x117b960
    if (inst[+0x58] != 32 && inst[+0x58] != 22)         // DMACopy(32) || Save(22)
        return;
    // direction filter: an SB/PSUM→DRAM "save" that is NOT a real external output
    if (srcStorage.memType != 8                          // src NOT DRAM (read via +216 / +0x110 tag)
        && dstStorage.memType == 8                       // dst IS DRAM
        && !bir::isTensorKindOutput(dstStorage)):        // and not a true graph output
        ownID = idMap.at(inst);
        writers = getWritersFromBB(srcStorage, inst.parentBB);   // BB-local defs of the source
        best = -inf; engCand = ?; subCand = ?;
        for w in writers:
            id = idMap.at(w);                            // "has not assigned ID yet" → reportError (cpp:147)
            if (ownID > id && id >= best):               // LATEST writer strictly before me
                best = id; engCand = w[+0x90]; subCand = w[+0x94];
        inst[+0x90] = engCand; inst[+0x94] = subCand;    // trigger engine = data producer
        ++assignedCount;                                 // visitor+0x1C
```

This is the **trigger-follows-producer** rule, the pass's headline. A save DMA is triggered by the engine that wrote its source tile last (latest scheduled ID strictly before the DMA's own), so the producing engine fires the DMA the moment its write completes. If a candidate writer has no ID yet, the pass raises `bir::reportError("… has not assigned ID yet")` (`assign_trigger_engine.cpp:147`) — an invariant violation, not a recoverable case.

> **QUIRK —** the symmetric **load→consumer** arm, `assignLoadToConsumerEngine` `@0x117cd00`, is *compiled but dead* in this build. It mirrors the producer logic (src DRAM, dst not DRAM, dst not `TensorKindInput`, pick the *earliest consumer strictly after* the load), but it has **zero call sites** — disassembly shows `AssignSaves::visitInstruction` tail-jumps *only* to `assignSaveToProducerEngine`, and the only two textual references to `117cd00` in the whole `.text` are the symbol label and its own header. The live policy is therefore Load → CC-engine (Stage 3a) and Save → producer-engine (Stage 3b). The source clearly intends a symmetric save↔producer / load↔consumer scheme; only those two arms are reachable here. CONFIRMED (0 callers).

#### Stage 4: IO-queue limiting (the trigger-pass tail)

After the per-module walk, a coalescing step can collapse IO DMAs onto a single engine. `CheckLimitIOQueues` counts the distinct engines used by HWDGE DMAs; if more than one is in play (`shouldLimitIOQueues` returns `set.size() > 1`), `LimitIOQueues` re-pins every *non*-HWDGE IO DMA onto SP.

```c
function CheckLimitIOQueues_visit(inst):                // 0x117a090
    if (is_dma_family(inst[+0x58]) && inst[+0xF8] == 2) // DGEType == HWDGE
        engineSet.insert(inst[+0x90]);                  // distinct HWDGE engine-ids

function shouldLimitIOQueues():                         // 0x1179ca0
    return engineSet.size() > 1;                        // cmp [this+0x28],1 ; seta

function LimitIOQueues_visit(inst):                     // 0x1179cb0
    if (!bir::isHWDGEDMAWithEngineSet(inst)             // NOT an HWDGE-with-engine DMA
        && bir::isIODMA(inst))                          // IS an input/output DMA
        inst[+0x90] = 6;                                // EngineType::SP — "SP only"
```

> **GOTCHA —** the coalescing leaves HWDGE-with-engine DMAs *untouched* — they already carry a deliberate descriptor-gen engine (set by pass 123, which runs later in the pipeline; the engine these DMAs end up on is decided there). Only the non-HWDGE IO traffic is forced onto SP, so all the loose IO collapses onto one queue ("Limiting IO queue to SP only"). A reimplementation that also re-pins the HWDGE DMAs would defeat pass 123's load-balancing. CONFIRMED — `LimitIOQueues::visitInstruction` calls `isHWDGEDMAWithEngineSet` (PLT `0x5fd980`) then `isIODMA` (PLT `0x5ea8f0`) then `mov QWORD PTR [rbx+0x90], 6`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `AssignTriggerEngine::run(vector&)` | `0x117be10` | Orchestrator; per-inst sub-visitor construction + IO-limit tail | CERTAIN |
| `AssignIdsAndCcLocs::visitInstruction` | `0x117a610` | Scheduled-ID map insert; IT 48 → CC maps | CERTAIN |
| `sub_117A0F0` | `0x117a0f0` | Build CC-result / CC-arg storage→inst maps | HIGH |
| `AssignSaves::visitInstruction` | `0x117bd50` | DMA-engine dispatch (gate `+0x90 == 4`) | CERTAIN |
| `AssignSaves::tryAssignToCCEngine` | `0x117a060` | CC-engine path (Load/DMACopy) | CERTAIN |
| `AssignSaves::assignSaveToProducerEngine` | `0x117b960` | Producer path — latest writer before own ID | CERTAIN |
| `AssignSaves::assignLoadToConsumerEngine` | `0x117cd00` | Consumer path — **DEAD, 0 callers** | CERTAIN |
| `CheckLimitIOQueues::visitInstruction` | `0x117a090` | Insert HWDGE engine-ids into a set | CERTAIN |
| `CheckLimitIOQueues::shouldLimitIOQueues` | `0x1179ca0` | `set.size() > 1` | CERTAIN |
| `LimitIOQueues::visitInstruction` | `0x1179cb0` | Force non-HWDGE IO DMA → SP(6) | CERTAIN |

---

## Pass 123 — `assign_hwdge_engine`

### Purpose

For DMAs already tagged `DGEType == HWDGE`, make the engine a **legal HW-DGE descriptor-generation engine**. The device exposes several engines that can build descriptor lists; this pass either keeps an already-valid one, or assigns one — deterministically on older silicon, by round-robin load-balancing on `core_v5`. The write it performs is the second half of `isHWDGEDMAWithEngineSet` (the engine-set conjunct), so it is the point at which a HWDGE DMA becomes *fully bound*.

### Entry Point

```text
AssignHWDGEEngine::run(vector<unique_ptr<Module>>&)     0x117fe00  ── orchestrator (builds ValidEngines)
  AssignHWDGEEngineImpl::AssignHWDGEEngineImpl(...)      0x1181340  ── ctor: copy vec, assert size>=2, store arch, run visit
    IRVisitor<AssignHWDGEEngineImpl>::visit              0x1180680  ── BB/inst recursion (539 ln)
      AssignHWDGEEngineImpl::assignEngine(InstDMA&)      0x1180360  ── WORKER (114 ln)
```

### Algorithm

#### Stage 1: build `ValidEngines` and run the visitor (orchestrator + ctor)

```c
function AssignHWDGEEngine_run(modules):                 // 0x117fe00
    arch = modules[0][+0xAC];                            // Module ArchLevel (D-D03)
    if (arch <= 49):                                     // cmp [rax+0xac], 0x31  @0x117fe44
        ValidEngines = [ EngineInfo{type=6 /*SP*/}, EngineInfo{type=2 /*Activation*/} ];
        // injected literally: mov QWORD [rax], 6 ; mov QWORD [rax-8], 2   @0x1180060/0x1180073
    else:                                                // core_v5 (arch >= 50)
        ValidEngines = target_supplied_engine_set();     // config-driven (GAP G4)
    for M in modules:
        impl = AssignHWDGEEngineImpl(logger, M, ValidEngines);  // 0x1181340

function AssignHWDGEEngineImpl_ctor(logger, M, ValidEngines):   // 0x1181340
    copy ValidEngines into Impl[+0x8 .. +0x10] (capacity Impl+0x18);
    assert(ValidEngines.size() >= 2)                     // else FATAL NeuronAssertion (cpp:22)
        // "Number of valid engines for HWDGE must be greater than or equal to 2"
    Impl[+0x24] = M[+0xAC];                              // arch (mov eax,[rbx+0xac]; mov [rcx+0x24],eax)
    visit(M.getFunction("main"));                        // "main" first
    for F in M.functions(): if F != main: visit(F);
```

> **GOTCHA —** `ValidEngines.size() >= 2` is a hard `NeuronAssertion` (`assign_hwdge_engine.cpp:22`, resolution `CONTACT_SUPPORT`), not a soft check. The arch ≤ 49 path satisfies it by injecting the two-entry `[SP, Act]` literal; a `core_v5` target whose config supplies fewer than two HW-DGE engines is a fatal misconfiguration. A reimplementation must guarantee the vector has ≥ 2 entries before the visitor runs. CONFIRMED — `mov QWORD [rax],6` / `mov QWORD [rax-8],2` injection and the assert string both read out of the binary.

#### Stage 2: per-instruction dispatch (`IRVisitor::visit`)

The visitor recurses Function → BB → Instruction (node-tag `+0x50`: 105 func, 106/108 nested regions). For each leaf instruction with `IT ∈ {19 Load, 22 Save, 32 DMACopy}` it calls `assignEngine`. Any other opcode in `[0,32]` triggers `llvm_unreachable("Unknown Instruction type encountered!")`; opcodes `33..0x7F` are skipped.

#### Stage 3: the HW-DGE engine worker (`assignEngine`)

```c
function assignEngine(impl, dma):                        // 0x1180360
    if (dma[+0xF8] != 2) return;                         // DGEType != HWDGE(2) → SKIP   (the assign gate)

    if (dma[+0x38] == 2) {                               // DMA-kind == 2: dedup against ValidEngines
        for ei in impl.ValidEngines:                     // scan [Impl+0x8 .. Impl+0x10)
            if (ei matches dma[+0x90]/[+0x94])            // already on a valid HW-DGE engine
                return;                                   // keep it
    }
LABEL_3:                                                  // current engine NOT in ValidEngines
    if (impl[+0x24] <= 49) {                              // gen3 / core_v4  (cmp [r15+0x24],0x31)
        cur = dma[+0x90];                                 // current engine class (EngineType)
        bir::reportError("InstDMA cannot be on PE engine");   // PE(3) is rejected for HW-DGE
        if (cur == 4 || cur <= 1)                         // DMA(4) / Unassigned(0) / Pool(1)
            dma[+0x90] = ValidEngines[0];                 //   → SP(6)
        else if (cur == 2 || cur == 5)                    // Activation(2) / DVE(5)
            dma[+0x90] = ValidEngines[1];                 //   → Activation(2)
    } else {                                              // core_v5 (arch >= 50)
        dma[+0x90] = ValidEngines[ (impl[+0x20]++) % ValidEngines.size() ];   // ROUND-ROBIN
    }
```

> **QUIRK —** the `core_v5` arm is the load-balancer: a single counter at `Impl+0x20` is post-incremented and taken modulo `ValidEngines.size()`, so successive HW-DGE DMAs cycle across the available descriptor-gen engines. CONFIRMED from disassembly: `mov eax,[r15+0x20]` (counter) → `mov rax,[rsi+rdx*8]` (`ValidEngines[idx]`) → `mov QWORD PTR [rbx+0x90],rax`. The `arch <= 49` arm is *deterministic by current engine class* and always rejects PE(3) — the `cmp r12d,0x3` / `cmp r12d,0x4` / `cmp r12d,0x1` ladder and the `"InstDMA cannot be on PE engine"` string are both present.

The three policy outcomes:

- **Non-HWDGE DMAs** (None / SWDGE / Unassigned) are untouched — they keep the trigger engine pass 110 gave them, and SWDGE descriptors are software-emitted, not device-generated.
- **A HWDGE DMA already on a `ValidEngines` engine** keeps it (the `+0x38 == 2` dedup fast path).
- **Otherwise** it moves to a legal descriptor-gen engine: deterministically SP-or-Activation (arch ≤ 49, by current engine class, PE rejected) or round-robin across the per-target `ValidEngines` (`core_v5`).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `AssignHWDGEEngine::run(vector&)` | `0x117fe00` | Orchestrator; builds `ValidEngines` (`[SP,Act]` for arch ≤ 49) | CERTAIN |
| `AssignHWDGEEngineImpl::AssignHWDGEEngineImpl` | `0x1181340` | ctor: copy vec, assert `size>=2`, store arch @`+0x24`, run visit | CERTAIN |
| `AssignHWDGEEngineImpl::assignEngine` | `0x1180360` | Worker: gate `+0xF8==2`; dedup; SP/Act-or-round-robin; write `+0x90` | CERTAIN |
| `IRVisitor<AssignHWDGEEngineImpl>::visit` | `0x1180680` | BB/inst recursion; dispatch IT ∈ {19,22,32} | HIGH |

---

## SWDGE vs HWDGE — where the tag is decided (not here)

Neither pass writes the `DGEType` tag at `+0xF8`; both only *read* it. The tag is owned by `libBIR` and resolved upstream.

`getDGEInstructionType` `@0x1097330` is a **pure classifier**: it returns a `std::set<DGEType>` by value and never mutates the instruction. Its arms (NX-151):

| Instruction shape | Candidate `DGEType` set |
|---|---|
| transpose + `IsHWDGETransposeAPShapeSizeOk` | `{Unassigned, SWDGE}` — transpose is SWDGE, never HWDGE |
| copy / SR / cast baseline | `{HWDGE}`; **+ `{SWDGE}` when `Module.arch > 29`** |
| scalar dynamic-offset | `{Unassigned, HWDGE}` |
| default | `{Unassigned}` |

The *persisted* choice is written by `libBIR setDGEType`, called from the DMA-optimization / dynamic-DMA passes (callgraph callers of `getDGEInstructionType`: `DMAOptimization::DMACopy_insertion` / `::beneficial_to_copy`, `DynamicDMAScanImpl::visitInstruction`, `DynamicDMACleanUp::run`, and the verifier's `checkDMAInDLOC`). By the time pass 123 runs, `+0xF8` is fixed; `assignEngine`'s `dma[+0xF8] != 2` gate simply skips any DMA the classifier did not mark HWDGE.

This is consistent with the verifier's transpose rule: `checkDMATranspose` enforces `getDgeType() == Unassigned || == SWDGE` for transposes, so a transpose DMA reaching pass 123 with `DGEType != HWDGE` is skipped by the gate and never given an HW-DGE engine. The two facts agree — pass 123 finalizes only the **engine-set half** of the routing (`isHWDGEDMAWithEngineSet`'s second conjunct), never the tag.

```c
// libBIR isHWDGEDMAWithEngineSet @0x30f240 — what pass 123's +0x90 write satisfies
bool isHWDGEDMAWithEngineSet(inst):
    return (inst[+0xF8] == 2)                   // DGEType == HWDGE  (cmp [rdi+0xf8],2)
        && ((inst[+0x90] & ~4) != 0);           // engine other than bare DMA bit
                                                 //   (test [rdi+0x90],0xfffffffb ; setne)
```

> **NOTE —** `~4 == 0xfffffffb`. The predicate is true precisely when the engine field holds something other than the lone `DMA=4` bit. Since pass 123 always writes SP(6) / Act(2) / a `core_v5` engine — none equal to 4 alone — the write flips this predicate, and that is the signal downstream passes use to treat the DMA as fully bound. CONFIRMED byte-exact from `libBIR.so`.

---

## Codegen handoff

After 110 and 123 the per-DMA binding lives in `InstDMA+0x90` (engine), `+0x94` (sub), `+0xF8` (`DGEType`). The downstream consumers:

| Consumer | Pass / address | What it reads | Confidence |
|---|---|---|---|
| `alloc_queues` | order 124 | Allocates one DMA queue per `(engine, QoS)` slot from the `+0x90` engine the two passes pinned | HIGH |
| `DynamicDMACleanUp::run` / `LimitIOQueues` | — | Gate on `isHWDGEDMAWithEngineSet`: HWDGE-with-engine DMAs are exempt from SP-only IO coalescing | HIGH |
| `CoreV2GenImpl::generateDynamicDMA` | `0x1276b10` | Emits the DMA descriptor (QoS byte[12] bits[0:2]); the engine binding selects which engine-stream the descriptor list / trigger op is encoded into | HIGH |
| `checkDMATrigger` (verifier) | `0xfef270` | Cross-checks trigger-engine (`+0x90`) == queue-engine (`queue+0xAC`) | HIGH |

So 110 fixes the trigger engine, 123 fixes the HW-DGE descriptor-gen engine (and the engine-set predicate), 124 turns `(engine, QoS)` into queues, and CoreV2 encodes them. The verifier's `"Trigger engine does not match queue's engine"` gate is the final consistency check on what these two passes wrote.

---

## Gaps and Uncertainty

- **`InstDMA+0x38` DMA-kind enum (LOW).** The `== 2` case (enabling the dedup-vs-`ValidEngines` search) is the only value exercised here; the full enum is not transcribed. Likely a copy/IO/CCE kind tag — a Strand-D follow-up on `+0x38`.
- **`getWritersFromBB` / `getReadersFromBB` internals (MED).** The templated BB-local def/use scans that feed producer/consumer selection were not transcribed end-to-end. The *selection arithmetic* (latest-before / earliest-after by scheduled ID) is certain from the call sites; the scan helper internals are not.
- **`unk_3E03CD8` force flag (LOW).** The global that force-enables `LimitIOQueues` regardless of `shouldLimitIOQueues` is likely an env/option byte; its origin is not pinned.
- **`core_v5` `ValidEngines` contents (MED).** For arch ≥ 50 the vector is target-supplied (chip-parts / target-datamodel), not a `libwalrus` literal. The round-robin *behavior* is certain; the concrete per-gen engine set is config-driven and not enumerated here.

Every address, offset, enum ordinal, and rodata byte cited at CERTAIN was read directly off `libwalrus.so` / `libBIR.so` (cp310) and cross-checked against a real instruction. The pass-110 selection policy (Load→CC, Save→producer; consumer arm dead), the pass-123 worker (DGEType gate, SP/Act-or-round-robin, PE reject, `size>=2` assert), the `[SP=6, Act=2]` injection, the DMA-family table, and the `isHWDGEDMAWithEngineSet` predicate are all CONFIRMED at the binary level.

---

## Related Passes

| Pass | Name | Relationship |
|---|---|---|
| Upstream | `DMAOptimization` / `DynamicDMAScan` / `DynamicDMACleanUp` | Call `getDGEInstructionType` + `setDGEType` — fix the `+0xF8` tag these passes gate on |
| 110 | `assign_trigger_engine` | Picks the trigger (compute) engine — this page |
| 123 | `assign_hwdge_engine` | Picks the HW-DGE descriptor-gen engine — this page |
| 124 | `alloc_queues` | Maps the `(engine, QoS)` the two passes pinned into DMA queues |
| Codegen | `CoreV2GenImpl::generateDynamicDMA` | Encodes the DMA descriptor onto the bound engine's stream |

## Cross-References

- [DGE Level Selection and the `dynamic_dma_*` Pass Family](../penguin/dge-level-dynamic-dma.md) — the front-end DGE level / dynamic-DMA selection feeding the `DGEType` tag these passes gate on
- [SP Engine — the TPB Control Processor](../arch/sp-engine.md) — the SP engine and its `DMATrigger` mechanism (the SP=6 target of the IO-queue coalescing and the arch ≤ 49 default)
- [ISA Enum Ordinals](../isa/isa-enum-ordinals.md) — the `EngineType` / `DGEType` / `InstructionType` ordinals referenced throughout
- [Backend Pass Registry](backendpass-registry.md) — the `register_generator_<name>__` factory mechanism and pass ordering
- [Engine Lowering Set](engine-lowering-set.md) — the abstract `EngineType::DMA` (4) default that pass 110 re-homes
- DMA Materialization (`lower_dma`, pass 8.27) — the descriptor blocks pass 123's engine assignment feeds (page in flight)
