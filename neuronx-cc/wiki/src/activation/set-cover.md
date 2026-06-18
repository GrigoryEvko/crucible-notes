# Activation Set-Cover, `calculateBestSets`, and Prefetch

> *All addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 `libwalrus.so`, BuildID `92b4d331…`). cp311/cp312 share the same bodies; the `.so` is stripped, so symbols are recovered from the demangled label table (`objdump -d`) and the rodata string pool.*

## Abstract

The Activation engine has **one** live LUT bank: a `LoadActFuncSet` (IT6) installs an entire pre-baked function-set — a hero transcendental plus its co-resident cheap functions — and a later `Activation` (IT4) names a function *inside whatever set is currently resident* ([1.10 Activation Engine](../arch/activation-engine.md), [10.6 LoadActFuncSet](loadactfuncset.md)). The front-end never authors a `LoadActFuncSet`; the `lower_act` pass (L28) synthesises the **minimal** sequence of loads so that the right set is resident before each Activation, and two follow-on passes hide and dedup those loads. This page documents that three-pass residency pipeline and, in particular, byte-traces the cover heuristic inside `LowerPWPImpl::calculateBestSets@0x11597e0` — the one piece [10.6 §4](loadactfuncset.md) defers here for its precise (greedy, non-optimal) structure.

The headline result: the cover is **greedy, not optimal**. `calculateBestSets` makes a **single forward pass** over the candidate `ActFuncSets[]` list, assigning each Activation the first remaining set that covers its function, clearing the covered functions from a running bit-set, and asserting at the end that the candidate list was either *exhausted* or had *remaining sets for the last group*. There is no nested search, no backtracking, no minimisation over the power-set of sets — it is a classic first-fit greedy set-cover. Two structural budgets cap the result: at most **8 resident func-sets per engine** (`"the number of activation tables must be <= 8"`@0x1d50118, asserted in the load-minting path), and **one func-set family per engine** (`"Engine2UsedActSetNames.size() <= 1"`).

The page is organised as: the residency contract (§1), the data structures the cover walks (§2), the greedy cover algorithm as annotated pseudocode (§3), the load-minting budget asserts (§4), the prefetch hoist (§5), and the global dedup (§6), closing with a confidence ledger (§7).

For reimplementation, the contract is:

- The **greedy first-fit cover**: for each Activation, scan candidate sets, take the first that still covers the function, record `(inst → set index)`, clear covered functions, never reconsider.
- The **bit-set bookkeeping** (`vector<uint64>` membership mask, `0xFFFFFFFF`-fill / clear-on-cover) and the per-instruction `MapVector<Instruction*,int>` + unique-index `set<int>` it produces.
- The two **residency budgets** (`≤ 8` resident sets, `≤ 1` family per engine) and where they are checked.
- The **prefetch hoist** (a stack-machine that floats each load to its earliest legal point) and its per-engine index bound.
- The **global dedup** (drop a load whose set a dominating earlier load already made resident, tracked by the `0xFFFFFFFF` "no active set" sentinel).

| | |
|---|---|
| **Pass** | `lower_act` (L28) → `optimize_prefetch_act_control` (L29) → `optimize_act_control` (L30) |
| **Set-cover** | `LowerPWPImpl::calculateBestSets@0x11597e0` (thunk `@0x5f2090` → GOT `off_3DCE830`) |
| **IR scan** | `LowerPWPImpl::visitInstruction@0x1151110` (opcode at `inst+0x58`, `ActivationFunctionType` at `inst+0x90`) |
| **Load minting** | `LowerPWPImpl::addLoadInstructionBefore(bir::Instruction&, ActivationFunctionType)@0x1155620` → `generateInstLoadActFuncSet@0x1156510` |
| **Prefetch hoist** | `OptimizePrefetchActLoadImpl::enterBasicBlock@0x11651b0` / `visitInstruction@0x11653f0`; `resetStack@0x11650a0` |
| **Global dedup** | `OptimizeActControl::run@0x11603b0` → `OptimizeActControlImpl::enterBasicBlock@0x11618a0` |
| **Residency budget** | `≤ 8` resident sets/engine (`@0x1d50118`); `≤ 1` family/engine (`Engine2UsedActSetNames.size() <= 1`) |
| **"No active set" sentinel** | `0xFFFFFFFF` (= `-1`, the `toJson` default of `inst+0xF0`) |
| **Cover result type** | `MapVector<bir::Instruction*, int>` (inst→set index) + `std::set<unsigned>` (unique chosen indices) |

> **NOTE —** This page owns the **scheduling** half of the LUT story: *which* sets to load, *how many*, and *where*. The **wire/encoding** half (the IT6 bundle byte `+0x23`, the `act_func_set_id` index, the `dynamic_pwp` gate) is [10.6](loadactfuncset.md); the **catalog** of sets (21 for `trainium`, 14 for `with_ln`) is [10.2 Activation Function Catalog](act-function-catalog.md); the `bkt.bin`/`ctrl.bin` byte format of a set is [10.4](bkt-ctrl-blob.md). The `lower_act` shell that constructs `LowerPWPImpl` is the [8.6 engine-lowering](../walrus/engine-lowering-set.md) page.

---

## 1. The residency contract

A `LoadActFuncSet` makes a named set resident; an `Activation` references a function *inside* it. Both key on the same wire byte `+0x23`, but the Activation op carries **no** set index — `InstActivation::readFieldsFromJson` never reads `"act_func_set_id"` ([10.6 §1](loadactfuncset.md)). The contract is therefore *implicit residency*: the engine resolves an Activation's func code against whatever set the most recent `LoadActFuncSet` installed. Emitting an Activation without first guaranteeing its set is resident produces a silently-wrong result keyed against the previous set's tables.

`lower_act` enforces that contract. Two front-end-shape asserts guard it: `"InstLoadActFuncSet should not exist before lower activate"`@0x1d4f580 (the loads are *synthesised*, never authored) and `"Module has no Activation Function Tables set up for this InstLoadActFuncSet"` (the catalog must be parsed first, by `fillAllActInfos@0x115af90`). The amortisation argument is the whole point: a load installs an *entire set* — its hero function plus a fixed tail of cheap co-resident 1-piece funcs ([10.4 §1](bkt-ctrl-blob.md)) — so a refill happens once per resident-set *epoch*, not once per Activation. The cover's job is to make those epochs as few and as long as possible.

> **GOTCHA — the engine bank is single, but the budget is `≤ 8`.** Only one set is *active* at any instant (the `unordered_map<string, PWPSim::AFTable>` models a single resident image), yet a single kernel may *reference* up to 8 distinct sets across its lifetime — the `≤ 8` budget (§4) bounds how many the kernel keeps in play, not how many are simultaneously banked. A reimplementer must not conflate "one live bank" with "one set per kernel."

---

## 2. What the cover walks

`calculateBestSets` operates on three structures, all built before it runs:

| Structure | Type (recovered) | Role |
|---|---|---|
| Instruction list | `vector<bir::Instruction*>` at `this+0xE8 … this+0xF0` | the Activations needing a func, in program order |
| Candidate sets | the parsed `act_func_sets[]` (`fillAllActInfos`, keyed `pwp_file_keys`/`type`) | each set's covered `ActivationFunctionType` membership |
| Needs bit-set | `vector<uint64>` at `this-relative −0x90 … −0x80` (frame), `0xFFFFFFFF`-filled | per-set "still-needed" mask, cleared as funcs get covered |

The Activation's requested function is read from the instruction at offset `inst+0x120` **only when** the opcode discriminator at `inst+0x58` equals `4` (the `Activation` IT) — `cmpl $0x4,0x58(%rax); … mov 0x120(%rax),%edi`@0x1159986. Non-Activation ops in the list contribute `func = 0`. Each candidate set is a record of stride `0x88` (`add $0x88,%r9`@0x11599bc) whose membership is probed through a red-black-tree walk (`0x1159a10`–`0x1159a5a`, comparing the requested `func` at node offset `+0x20`) — i.e. each set stores its functions as an ordered `std::map`/`set` keyed by `ActivationFunctionType`.

The cover *produces* two results:

- a `MapVector<bir::Instruction*, int>` (`try_emplace@0x619240`, calls at `0x1159cc4`/`0x1159dcd`/`0x115a2ef`) — the **per-Activation chosen set index**, stored at `this+0xA0`;
- a `std::set<unsigned>` (`_Rb_tree::_M_insert_unique@0x60f460`/`0x5ef890`, calls at `0x1159de3`/`0x115a302`) — the **distinct set indices** chosen, the input to load-minting.

The "best set index" register is initialised to `0xFFFFFFFF` (`mov $0xffffffff,%r13d`@0x1159d17) — the sentinel meaning "no set chosen yet," reused as the `LoadActFuncSet`'s null `act_func_set_id` ([10.6 §3.3](loadactfuncset.md)).

---

## 3. The greedy cover — `calculateBestSets@0x11597e0`

The function first asserts a non-empty work list (`reportError(empty, "must have at least one Activation instruction")`@0x1159875, guarding `this+0xE8 == this+0xF0`), then runs the greedy pass. The two trailing string asserts — `"must have remaining ActFuncSets for the last …"`@0x1d50568 and `"must have exhausted all ActFuncSets"`@0x1d50540 — bracket the candidate iteration and are the structural proof that the cover is a *single forward sweep* of the candidate list, terminating either by running out of sets or by covering the last group. There is no power-set enumeration and no re-scan.

> **CORRECTION — the `"must have remaining ActFuncSets …"` string starts at `0x1d50568`, not `0x1d50563`.** An earlier draft pinned this rodata string to `0x1d50563`; that offset lands inside the null padding *after* the preceding `"…ActFuncSets"` (bytes `0x1d50560..67` are `65 74 73 00 00 00 00 00` = `"ets\0\0\0\0\0"`). The C-string actually begins at `0x1d50568` (`6d 75 73 74` = `"must"`), verified by `objdump -s -j .rodata`. The string content and the single-forward-sweep conclusion are unchanged. (CONFIRMED — byte dump of `.rodata` at `0x1d50540..0x1d50590`.)

```c
// LowerPWPImpl::calculateBestSets @ 0x11597e0  (greedy first-fit set-cover)
// this+0xE8..0xF0 : vector<Instruction*> acts      — Activations needing a func
// this+0x90        : array of candidate ActFuncSet records (stride 0x88)
// this+0x98        : numCandidateSets (read into ebx @0x11598b2)
// this+0xA0        : MapVector<Instruction*,int> instToSet  (the result map)
void LowerPWPImpl::calculateBestSets() {
    reportError(acts.empty(), "must have at least one Activation instruction");   // @0x1159875

    uint32_t numSets = this->numCandidateSets;                                    // @0x11598b2
    // 'needs' is a per-set bitmask vector, all-ones at start: every set is still
    // a live candidate for every Activation.  Width = ceil(nActs / 64) words.
    vector<uint64_t> needs;                                                       // frame −0x90..−0x80
    memset(needs.data, 0xFF, needs.bytewidth);                                    // @0x1159e3c/0x1159e6c/0x1159f34/0x115a14b

    set<unsigned> chosen;                                                         // unique chosen indices

    for (Instruction* inst : acts) {                                             // outer loop @0x1159970
        int func = (inst->opcode == 4 /*Activation*/) ? inst->funcType : 0;       // @0x1159986

        // ── inner greedy pick: first candidate set still covering this func ──
        int best = -1 /*0xFFFFFFFF*/;                                             // @0x1159d17
        for (uint32_t s = 0, rem = numSets; s < numSets; ++s) {                   // @0x11599cc
            if (!bit_test(needs, s)) continue;        // set already retired       @0x11599e7
            if (!candidateSet[s].covers(func)) {      // rb-tree membership probe  @0x1159a10
                bit_clear(needs, s);                  // retire set for good       @0x1159a60
                if (--rem == 0) break;                                            // @0x1159a69
                continue;
            }
            best = s;                                 // FIRST fit wins
            break;
        }

        if (best < 0)                                                            // no set covers func
            reportError(true, "Invalid activation function found in instruction"); // @0x1d4ff90 / 0x1159d5b

        instToSet.try_emplace(inst, best);            // record (inst → set)       @0x1159dcd
        chosen.insert(best);                          // accumulate unique sets    @0x1159de3

        // termination invariants the binary asserts at the tail of the sweep:
        //   "must have remaining ActFuncSets for the last ..."  @0x1d50568
        //   "must have exhausted all ActFuncSets"               @0x1d50540
    }
    // 'chosen' (a std::set<unsigned>) is the minimal cover handed to load-minting.
}
```

The crucial detail is `best = s; break;` — the inner loop takes the **first** still-live set that covers the function, not the set that covers the *most* outstanding functions. Combined with the single non-backtracking outer pass, this is a **greedy first-fit** cover. It is cheap (`O(nActs · numSets · log|set|)`) and produces a valid — but not provably minimal — cover. For the shipped catalogs (≤21 sets, ≤8 budget) the gap between greedy and optimal is immaterial; the heuristic trades the NP-hardness of exact set-cover for a linear sweep.

> **CORRECTION — this is *not* an exact/optimal minimiser.** An earlier framing might read "minimal sequence" as "fewest possible loads, optimally chosen." The byte trace contradicts that: there is no objective function evaluated over the power-set of sets, no comparison of candidate covers, and no backtracking. "Minimal" here means "greedily fewest by first-fit," which can be strictly worse than optimal on an adversarial membership matrix. The name `calculateBestSets` is aspirational, not a proof of optimality. **[STRONG — control-flow structure, the first-fit `break`, and the single-pass termination asserts are all anchored; the exact tie-break among equally-covering sets is the program order of `act_func_sets[]`, inferred from the forward scan rather than independently proven.]**

> **NOTE — invalid-function is a hard error, not a fallback.** If no shipped set covers a requested `ActivationFunctionType`, the cover calls `reportError("Invalid activation function found in instruction")`@0x1d4ff90 and aborts. There is no synthesise-a-new-set path: the set catalog (`act_info.json`, parsed by `fillAllActInfos`) is closed, and LUT-driven funcs that belong to no set cannot be lowered. This is why the catalog ([10.2](act-function-catalog.md)) and the cover are co-dependent.

---

## 4. Load minting and the residency budgets

The unique set indices in `chosen` drive `addLoadInstructionBefore(bir::Instruction&, ActivationFunctionType)@0x1155620`, which inserts one IT6 before the first Activation that needs a not-yet-resident set and calls `generateInstLoadActFuncSet@0x1156510` to mint the bundle, stamping the chosen index into `inst+0xF0` ([10.6 §3](loadactfuncset.md)). The signature carrying an `ActivationFunctionType` confirms the load is tagged with its *triggering* function, used downstream for dedup.

Two budget asserts in the minting path cap the residency model (both strings present in `.rodata`, referenced from the `generateInstLoadActFuncSet@0x1156510` body):

| Assert string | Address | Meaning |
|---|---|---|
| `"the number of activation tables must be <= 8"` | `0x1d50118` | **at most 8 resident func-sets per Activation engine** — distinct from the `act_func_set_id` catalog range (0..20); the index numbers the shipped catalog, the `≤ 8` caps how many one kernel keeps live |
| `"Engine2UsedActSetNames.size() <= 1"` | rodata | **one func-set *family* per engine** (the CoreV4 variant tightens this to `== 0` — `"For CoreV4-, Engine2UsedActSetNames must be emtpy after visting Module"`) |

> **QUIRK — the `≤ 8` budget is enforced at *mint* time, not in the cover.** `calculateBestSets` itself never checks `chosen.size() ≤ 8`; the bound is an assert in the load-minting path. So the greedy cover can in principle select more than 8 distinct sets and the budget assert would fire — but for the closed catalog (a hero-per-set layout where most Activations share a few sets) `chosen` stays well under 8 in practice. A reimplementer should still enforce the budget *during* cover to fail gracefully rather than assert.

---

## 5. The prefetch hoist — `optimize_prefetch_act_control` (L29)

The LUT refill has **no cost model** in this build (the `bir::Hwm::getLatencyExec(InstLoadActFuncSet, …)` methods are `__assert_fail` stubs — [1.10](../arch/activation-engine.md), [10.6 §7](loadactfuncset.md)). The scheduler therefore cannot charge a typed cycle cost for a reload; it hides the (physically real) refill *structurally* by floating each `LoadActFuncSet` to its earliest legal program point, behind upstream compute.

`OptimizePrefetchActLoadImpl::enterBasicBlock@0x11651b0` opens each block by calling `resetStack@0x11650a0` (a stack-machine of pending loads), then `visitInstruction@0x11653f0` walks the block and relocates each load. The hoist consults `Eng2UsedActTables` — a `DenseMap<bir::EngineInfo, vector<set<ActivationFunctionType>>>` (the `doFind@0x60e410` and the `vector<set<ActivationFunctionType>>` destructor `@0x60aec0` calls at `0x11654e6`/`0x1165637` confirm the value type) — and bound-checks the index against the per-engine set count:

```text
assert("Eng2UsedActTables->count(actTblLoad->getEngineInfo()) > 0 &&
        (int)Eng2UsedActTables->lookup(actTblLoad->getEngineInfo()).size() > actFuncSetId")
```

i.e. the engine must have a known set-list and the load's index must be in range for *that* engine. The hoist is ordering-aware: a config knob `"ForcePrefetchFollowIncomingOrder"`@0x15fb5f forces the hoisted loads to keep their original relative order rather than be freely reordered — a determinism/ correctness valve for when two loads' earliest points would otherwise cross.

> **NOTE — there is a *separate* generator-level prefetch scheduler.** The symbol pool also carries `register_generator_prefetch_scheduling_before_sched` / `…_after_sched` and a `PrefetchScheudling` [sic] class (with `cc_dma_alignment`). That is the **DMA** prefetch scheduler for the generator, a *different* mechanism from `optimize_prefetch_act_control` (the *act-table-load* hoist documented here). A reimplementer must not merge them: this page's hoist moves IT6 `LoadActFuncSet` ops; the generator scheduler reorders DMA queue work.

---

## 6. The global dedup — `optimize_act_control` (L30)

The final pass eliminates redundant reloads that survive the local cover and the hoist. `OptimizeActControl::run@0x11603b0` drives `OptimizeActControlImpl::enterBasicBlock@0x11618a0`, which scans IT6 ops (`inst+0x50 == 0x6`), tracks the **currently-resident set** across basic blocks using the `0xFFFFFFFF` "no active set" sentinel, and **drops** any load whose set a dominating earlier load already made resident — a global redundant-reload elimination. The sentinel here is exactly the `toJson` default of `inst+0xF0`: a load is "no-op" if it re-installs the set that is already banked.

This closes the trio:

```text
L28  lower_act                       calculateBestSets   ── greedy first-fit cover  (which sets, fewest)
L29  optimize_prefetch_act_control   enterBasicBlock     ── hoist loads earliest    (hide the refill)
L30  optimize_act_control            enterBasicBlock     ── global dedup            (drop redundant reloads)
```

> **GOTCHA — dedup is dominator-scoped, cover is program-order.** The cover (§3) assigns sets in straight program order and is oblivious to control flow; the dedup (§6) is the pass that understands *dominance* — it removes a reload only when an earlier load on every path already left the set resident. Splitting "pick the sets" from "remove the redundant loads" is deliberate: the cover stays a cheap linear sweep, and the (more expensive) dominance reasoning is confined to one pass.

---

## 7. Confidence ledger

**CONFIRMED** (anchored byte-exact to the stripped `.so`):
- `calculateBestSets@0x11597e0` (label resolves exactly; thunk `0x5f2090` → GOT `off_3DCE830`); the first-fit inner `break`, the per-set bit-set clear-on-cover (`not; and; mov`@0x1159a60), the single-pass outer loop, and the `MapVector`/`set` result builders.
- The greedy-vs-optimal verdict: no power-set search, no backtracking; the `"must have remaining ActFuncSets …"`@0x1d50568 / `"must have exhausted all ActFuncSets"`@0x1d50540 termination asserts.
- The `≤ 8` budget string `@0x1d50118` and the `Engine2UsedActSetNames.size() <= 1` (CoreV4 `== 0`) family bound.
- The prefetch hoist entry `@0x11651b0` calling `resetStack@0x11650a0`; `Eng2UsedActTables` as `DenseMap<EngineInfo, vector<set<ActivationFunctionType>>>`; the `ForcePrefetchFollowIncomingOrder`@0x15fb5f knob.
- The `0xFFFFFFFF` "no active set" sentinel shared by cover, load default, and dedup.

**STRONG** (symbol/structure grounded, one inference step):
- The tie-break among equally-covering sets is `act_func_sets[]` program order (inferred from the forward scan).
- The `≤ 8` budget is enforced at mint time, not inside the cover.

**INFERRED / SPECULATIVE:**
- The exact membership-probe node layout (rb-tree at set-record `+0x60`, key at node `+0x20`) is read from the walk shape, not from a typed struct dump.
- Whether two distinct Activation engines can each carry their own ≤8/≤1 budget independently (the per-engine keying of `Eng2UsedActTables`/`Engine2UsedActSetNames` implies yes; not exercised on a multi-engine module here).

**Evidence:** D-H38 (engine lowering), D-T05 (PWP control). Cross-refs: [10.6 LoadActFuncSet](loadactfuncset.md) (wire/encoding half), [10.2 Activation Function Catalog](act-function-catalog.md) (the set roster), [10.4 bkt/ctrl blob](bkt-ctrl-blob.md) (set byte format), [8.6 engine-lowering](../walrus/engine-lowering-set.md) (the `lower_act` shell), [1.10 Activation Engine](../arch/activation-engine.md) (datapath + the no-latency finding).
