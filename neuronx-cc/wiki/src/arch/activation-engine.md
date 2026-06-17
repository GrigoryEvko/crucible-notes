# Activation Engine — Datapath and the LUT-Load Mechanism

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). Functions live in `libwalrus.so` (build-id `92b4d331…`; `.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset), `libBIR.so` (build-id `a9b1ea38…`, the `bir::` IR + enums), and `libBIRSimulator.so` (the `birsim::` reference model). Addresses are cp310; other wheels differ — see [Build & Version Provenance](../reference/versions.md).*

## Abstract

The **Activation engine** is Trainium's scalar/elementwise unit — `EngineType 2` (external name **"Activation"**, also surfaced as **"Scalar"**). It is the sibling of the [PE matmul array](pe-engine.md): where the PE array does `K`-reductions into fp32 PSUM, the Activation engine does the *pointwise* leg — an affine pre-scale, a transcendental function (`sigmoid`, `gelu`, `exp`, …), an optional bias add, and an optional channel-wise accumulation — in one pass per element. The transcendentals are not hardwired silicon; they are **piecewise-polynomial lookup tables** that must be *loaded* onto the engine before use. This page is the engine and its load mechanism: how a function set is selected, how `LoadActFuncSet` swaps it onto the on-chip LUT bank, and how the bias/accumulator datapath is steered. The polynomial math and the bkt/ctrl blob byte-format belong to [Part 10 — the PWP model](../activation/) and are cross-referenced, not duplicated.

The defining constraint is **single-set residency**. The engine's LUT bank holds exactly **one** activation-function-set at a time — a `(bkt_bin, ctrl_bin)` image modelled as `PWPSim::AFTable`. A function set is a bundle of co-resident functions (a "hero" transcendental plus ~13 always-on cheap funcs; full roster in [Part 10](../activation/)). To compute `gelu` you must have loaded the *unique* set whose `act` map contains `gelu`; that load also brings in every other function in the set, and evicts the previous set. The compiler (`lower_act`) therefore runs a **set-cover** so that a run of activations that share one set reloads the LUT only once. The `LoadActFuncSet` instruction is the wire face of that reload: it carries exactly one operand-specific field — an integer **set index** — and **no LUT source address**, because the table images are engine/ROM-resident and the index merely selects which one becomes active.

For reimplementation, the contract is:

- **The set-index encoding** — `LoadActFuncSet` (BIR `InstLoadActFuncSet`, IT6) carries one field: `act_func_set_id` (BIR-JSON key) / `act_tbl_sel` (silicon-encoder name), at struct `inst+0xF0`, range-checked, stamped into bundle byte `+0x23`. The index is the **zero-based array position** of the set in `act_info.json`'s `act_func_sets[]`.
- **The name→index resolution** — a `std::map<string, unsigned int>` inside `AllActSetName2ActInfo` maps a set name to its array order; `generateInstLoadActFuncSet` reads the int and stores it.
- **The wire opcode** — `0x23` "ActivationTableLoad" (gen-invariant), plus a separate scheduling pseudo `0xC6` "PseudoLoadActFuncSet".
- **The co-residency limit** — exactly **one** set resident at a time (the `AFTable` map has one active key per Activation engine); the load is the residency gate.
- **The `dynamic_pwp` gate** — a `ModuleAttribute "neff_feature_dynamic_pwp"` + NEFF feature bit `0x40` that tells the runtime the LUT sets are loaded dynamically (via these ops) rather than from a static ROM.
- **The bias/accumulator datapath** — `out = act_func(scale·x + bias)`, with the three affine operands (`scale`, `bias`, `alpha`) all fp32, plus an optional channel accumulator steered by `EngineAccumulationType` and drained by `InstActivationReadAccumulator`.

| | |
|---|---|
| **Engine** | `EngineType 2` — external name "Activation" / "Scalar" |
| **Load op** | `bir::InstLoadActFuncSet` (InstructionType 6, "IT6") |
| **Load encoder** | `CoreV2GenImpl::visitInstLoadActFuncSet` @ `0x1250b30` (gen-invariant — no V3/V4 override) |
| **Wire opcode** | `0x23` "ActivationTableLoad"; pseudo `0xC6` "PseudoLoadActFuncSet" |
| **Set-index field** | `act_func_set_id` (JSON) / `act_tbl_sel` (wire) @ `inst+0xF0` → bundle `+0x23`; default `-1` (`0xFFFFFFFF`) |
| **Index source** | array order of `act_func_sets[]` in `act_info.json`; resolved by `std::map<string,unsigned int>` in `AllActSetName2ActInfo` |
| **Index minter** | `LowerPWPImpl::generateInstLoadActFuncSet` @ `0x1156510` |
| **Set-cover** | `LowerPWPImpl::calculateBestSets` @ `0x11597e0` over the `<29>` LUT-driven funcs |
| **Co-residency** | **one** set resident at a time — `std::unordered_map<string, PWPSim::AFTable>` |
| **Dynamic gate** | `ModuleAttribute "neff_feature_dynamic_pwp"` + NEFF feature bit `0x40` (`NeffPackager::writeNEFFFeatures` @ `0x15294b0`); global `enableDynamicActTable` |
| **Activation op** | `bir::InstActivation` (IT4, opcode `0x21` "Activate"); `getIfmap`/`getBiases`/`getSummation`/`getDst` |
| **Accumulator drain** | `bir::InstActivationReadAccumulator` / `InstReadActivationAccumulator` (opcode `0x24`) |
| **Load latency model** | **none** — `Hwm::getLatencyExec(InstLoadActFuncSet, EngineType)` is an `__assert_fail` stub; no arch override |

---

## The Engine Datapath — Affine, Function, Accumulate

### Purpose

The Activation engine is a streaming elementwise ALU with a transcendental LUT in the middle. One `InstActivation` (the IT4 op, opcode `0x21` "Activate") computes, per element of the moving ifmap, a three-stage pipeline: an **affine pre-op** (`scale·x + bias`), the **function evaluation** through the resident LUT, and an optional **channel-wise accumulation** of the result. This is the same shape as a TPU's "vector unit" activation stage, with the transcendental approximated by a piecewise polynomial instead of a hardwired CORDIC.

### The Three Affine Operands

The IR node's operand accessors (libBIR, demangled from `InstActivation` symbols) name the datapath inputs exactly:

| Accessor | Role | Wire / dtype | Confidence |
|---|---|---|---|
| `InstActivation::getIfmap` | the moving input tensor `x` (SBUF-resident AP) | tensor AP | CONFIRMED |
| `InstActivation::getBiases` | the per-channel **bias** addend | fp32 immediate/tensor | CONFIRMED |
| `InstActivation::getDst` | the result destination | tensor AP | CONFIRMED |
| `InstActivation::getSummation` | optional **accumulator** output (`hasSummation` gates it) | accumulator region | CONFIRMED |
| `scale` (wire key `"scale"`) | the per-channel **multiplier** | fp32 immediate | CONFIRMED |
| `alpha` (wire key `"alpha"`) | the parametric-func **alpha** (e.g. leaky-relu slope) | fp32 immediate | CONFIRMED |

> **NOTE — bias, scale, and alpha are *always fp32*, regardless of the ifmap dtype.** The reference model asserts it directly: `imm_value.getType() == Dtype::float32 && "activation function only support float32 bias/scale/alpha"` (`libBIRSimulator`). A reimplementer feeding a bf16 ifmap still passes fp32 scalars for the affine pre-op; the engine widens the product internally. This mirrors the PE array's fp32-PSUM rule — the *accumulation/affine* arithmetic is full-precision even when the streamed data is narrow.

### Algorithm

```c
// per-element datapath, modelled after birsim::InstVisitor::visitInstActivation
// (the IT4 / opcode 0x21 "Activate" path)
function activation_datapath(InstActivation &I):
    func   = func_code(I)                 // bundle+0x23: silicon LUT code (= pwp neuron_id)
    table  = currently_resident_set()     // the AFTable last installed by LoadActFuncSet
    scale  = I.scale                      // fp32 immediate ("scale")
    bias   = I.getBiases()                // fp32 per-channel addend
    alpha  = I.alpha                      // fp32 parametric-func slope ("alpha")

    for each element x in I.getIfmap():
        pre  = scale * x + bias           // affine pre-op (fp32)
        y    = eval_pwp(table, func, pre, alpha)   // piecewise-poly LUT eval — see Part 10
        write(I.getDst(), y)              // pointwise result
        if I.hasSummation():              // optional channel accumulation leg
            accumulate(I.getSummation(), y, I.getAcc())   // EngineAccumulationType command
```

> **GOTCHA — the function is *not* named in the Activation bundle by set; only by code.** `InstActivation::readFieldsFromJson` does **not** read the `act_func_set_id` key — the Activation op carries no set index at all. It names its function purely by the silicon **LUT code** at bundle `+0x23` (e.g. `Sigmoid → 0x05`, `Exp → 0x07`, the `ActivationFunctionType → uint8` func-remap, which equals the pwp `neuron_id`; see [§ Addressing a Function](#addressing-a-function-within-the-resident-set)). The engine resolves that code against **whatever set is currently resident**. The contract — "the right set is resident before each Activation" — is a *compiler* guarantee enforced by inserting `LoadActFuncSet` ops, not a property of the Activation bundle. A reimplementer who emits an Activation without first guaranteeing its set is loaded produces a silently-wrong result keyed against the previous set's tables.

### The Accumulator Command

The summation leg is steered by `EngineAccumulationType` (the `bir::` enum behind `getAcc()`), the same accumulator vocabulary the DVE/TensorScalar engines use. Its commands:

| Value | Name | Meaning | Confidence |
|---|---|---|---|
| `0` | `Idle` | no accumulation — plain pointwise write | INFERRED (ordinal) |
| `1` | `ZeroAccumulate` | zero the accumulator region, then write (head of a channel reduction) | STRONG (name CONFIRMED, ordinal INFERRED) |
| `2` | `AddAccumulate` | add into the existing accumulator region (interior) | STRONG (name CONFIRMED, ordinal INFERRED) |
| `3` | `LoadAccumulate` | preload the accumulator from an fp32 immediate, then add | STRONG (name CONFIRMED, ordinal INFERRED) |

> **NOTE — `LoadAccumulate` requires an fp32 immediate seed.** The simulator enforces a cluster of guards: `"an instruction that does a LoadAccumulate must provide an immediate value to load"`, `"immediate value for LoadAccumulate must be an fp32"`, and `"imm_ptr for LoadAccumulate must have the same number of partitions as the src/dst"`. The drain side carries `"Cannot accumulate to nothing"`. The enum *names* are CONFIRMED from the simulator strings and `EngineAccumulationType::LoadAccumulate` usage; the exact integer **ordinals** `{0,1,2,3}` are INFERRED from the conventional ordering (the typed enum is reconstructed only via the `EngineAccumulationType2string` switch, not statically recovered as a value table) and from the sibling [PE engine page](pe-engine.md#the-fp32-psum-accumulator)'s citation of the same enum.

The accumulated channel sums are read out by a *separate* op — `InstActivationReadAccumulator` (alias `InstReadActivationAccumulator`), wire opcode **`0x24`** "ActivationReadAccumulator" — which drains the accumulator region into a tensor, exactly as the PE array's STOP flag drains a PSUM bank. Both accumulator-read ops bind `validEngines = {Activation}` only.

> **QUIRK — two near-identical accumulator-read ops coexist.** `bir::InstReadActivationAccumulator` (IT5) and `bir::InstActivationReadAccumulator` (IT101) are distinct IR types with parallel `createFromJson`/`evalFieldsInto`/`getValidEngines` symbols. They are the legacy (`ReadActivation…`) and renamed (`ActivationRead…`) spellings of the same accumulator-drain; both target opcode `0x24`. A reimplementer should treat them as one mechanism with two BIR node names.

---

## LoadActFuncSet — the LUT-Load Instruction

### Purpose

`bir::InstLoadActFuncSet` (InstructionType 6, "IT6") is the *only* instruction that changes which activation-function-set is resident on the engine. It is minted by the compiler (never present in input BIR), carries one field (the set index), and lowers to wire opcode `0x23`. It does **not** carry a LUT source address: the `(bkt_bin, ctrl_bin)` table images are engine/ROM-resident, and the set index selects which one becomes active.

### Entry Point

```text
LowerAct::run @ 0x114e280                         ── the lower_act pass driver (L28)
  └─ LowerPWPImpl::fillAllActInfos @ 0x115af90    ── parse act_info.json → AllActSetName2ActInfo
  └─ LowerPWPImpl::calculateBestSets @ 0x11597e0  ── set-cover over the <29> LUT funcs
  └─ LowerPWPImpl::addLoadInstructionBefore @ 0x1155620
       └─ LowerPWPImpl::generateInstLoadActFuncSet @ 0x1156510   ── mint the IT6, stamp index
   ... later, in the backend codegen ...
  CoreV2GenImpl::visitInstLoadActFuncSet @ 0x1250b30   ── encode IT6 → 64-byte bundle, opcode 0x23
```

> **NOTE — the IDA `decompiled/*.c` bodies for these symbols at `0x5f….` are PLT-thunk stubs.** The real bodies live at the high VAs above (`generateInstLoadActFuncSet @ 0x1156510`, thunk @ `0x5f33c0`; `calculateBestSets @ 0x11597e0`, thunk @ `0x5f2090`; `visitInstLoadActFuncSet @ 0x1250b30`, thunk @ `0x60beb0`). All addresses on this page are the high-VA real bodies, read straight from the `.so`.

### The Wire Bundle

`LoadActFuncSet` encodes to a 64-byte bundle (the `std::array<uint8,64>` the backend stamps). Only three bytes are non-zero — there is no operand, no access pattern, and no LUT source address:

| Offset | Field | Source | Confidence |
|---|---|---|---|
| `0x00` | opcode `0x23` | `setupHeader(src=0x23)` — stamped at all 3 codegen arms | CONFIRMED |
| `0x01` | `inst_word_len = 0x10` (16 dwords = 64 B) | `setupHeader` | CONFIRMED |
| `0x23` | `act_func_set_id` / `act_tbl_sel` — the **set index**, range-checked int→byte | from `inst+0xF0`; field name `"instr.act_tbl_sel"` in rodata | CONFIRMED |

The remaining 60 bytes are zero. A guard at `inst+0x110` (`"fill_reg"`) must be `0` (`test al; jne →out_of_range`): the load cannot be a register-addressed fill.

> **QUIRK — the set-index and the activation func-code occupy the *same* bundle byte `+0x23`.** When the bundle is a `LoadActFuncSet`, byte `+0x23` is the raw set index (no remap). When it is an `Activation`, byte `+0x23` is the function code (the 31-entry func-remap output). Two ops, one wire byte, complementary roles: the load *installs* a set; the activation *references a function within* the resident set. A reimplementer decoding a bundle must dispatch on the opcode byte before interpreting `+0x23`.

The opcode `0x23` is independently corroborated by the ISA validity checker `core_v{2,3,4}::dbg_is_valid_acttableld` (which does `cmp …, 0x23` on the opcode byte) and by `enum_variant_string_opcode`, which names opcode `0x23` "ActivationTableLoad" identically across all three generations — see [The ISA Opcode Names](#the-isa-opcode-names).

### The Encoder

```c
// CoreV2GenImpl::visitInstLoadActFuncSet @ 0x1250b30  (gen-invariant: V3/V4 reuse this body)
function visitInstLoadActFuncSet(InstLoadActFuncSet &I):
    bundle = zero_array<uint8>(64)
    setupHeader(bundle, /*opcode*/ 0x23, /*word_len*/ 0x10)   // @ 0x1172120
    idx = I.field[0xF0]                          // the act_func_set_id (already range-checked)
    bundle[0x23] = (uint8) idx                   // setter @ 0x124e710 — the set index byte
    // fill_reg guard at I+0x110 must be 0 (test al; jne out_of_range)
    emit(bundle)                                 // fwrite 0x40 bytes @ 0x1250d07 / 0x1250d8b
```

> **CORRECTION — `LoadActFuncSet` is gen-invariant; there is *no* per-generation override.** Earlier strand-J notes might lead a reader to expect a `CoreV3GenImpl`/`CoreV4GenImpl` override (as exists for `Activate2` at opcode `0x25`, gen4-only). There is none: gen4's visitor reuses the `CoreV2` body verbatim, exactly as plain `Activation` (opcode `0x21`) does. Only `Activate2` (`0x25`) has a gen4 override on the Activation engine; the LUT load and the plain activation are single-implementation across generations.

---

## The Set-Index Encoding — Name → Integer

### Purpose

The value stamped into `inst+0xF0` (→ bundle `+0x23`) is the **zero-based array position** of the chosen set in `act_info.json`'s `act_func_sets[]` array. There is no separate ID space: index `0` is the first set in the JSON (`exp_and_others`), index `20` the last on Trainium (`derivative_gelu_apprx_sigmoid_and_others`). The whole roster — 21 sets on `pwp_bin_trainium`, 14 on `pwp_bin_with_ln` — is owned by [Part 10](../activation/); this page owns only the *selection* mechanism.

### Algorithm

```c
// LowerPWPImpl::generateInstLoadActFuncSet @ 0x1156510 (real body; thunk @ 0x5f33c0)
function generateInstLoadActFuncSet(set_name):
    // AllActSetName2ActInfo is an llvm::MapVector< string, ActFuncSetInfo, std::map<string,unsigned int> >
    // The underlying std::map<string,unsigned int> IS the name→index dictionary;
    // the SmallVector<pair<string,ActFuncSetInfo>> preserves act_func_sets[] array order,
    // so index == JSON array position.
    assert AllActSetName2ActInfo.count(set_name) > 0     // @0x1d4ff60: the set NAME must exist
    idx = AllActSetName2ActInfo.map[set_name]            // _Map_base::operator[] @ 0x1156881
                                                         //   → ebx = the set index (int)
    assert (int) AllActSetName2ActInfo.size() > idx      // @0x1d50040: index in range
    inst = new InstLoadActFuncSet("<name>-PWP")          // "-PWP" suffix names the minted load
    inst.field[0xF0] = idx                               // mov [rcx+0xf0], ebx @ 0x11568b6
    inst.field[0x110] = 0                                // fill_reg = 0 (the encoder guard)
    return inst
```

The dictionary is built once, while parsing the JSON, by `fillAllActInfos` (@ `0x115af90`): it reads the top-level keys `"act_func_sets"` and `"pwp_file_keys"`, and per-set `"name"`, incrementing an index counter for each set element — so the integer index is literally the loop counter, i.e. the array order.

| Structure | What it is | Confidence |
|---|---|---|
| `AllActSetName2ActInfo` | `llvm::MapVector<string, ActFuncSetInfo, std::map<string,unsigned int>, …>` — name→index dict + ordered vector | CONFIRMED |
| `act_func_set_id` | BIR-JSON wire key; field at `inst+0xF0` | CONFIRMED |
| `act_tbl_sel` | silicon-encoder field name (`"instr.act_tbl_sel"`) for the same datum | CONFIRMED |
| `GlobalActFuncSetId` | the int read out of the map, range-checked against the set count | CONFIRMED |

> **GOTCHA — the index is `act_info.json` array order, not a stable enum.** Because the index is array position, the *same* logical set can carry a *different* `act_func_set_id` on `trainium` vs `with_ln` (the rosters differ — 21 vs 14 sets, with renames; see [Part 10](../activation/)). A reimplementer must resolve the set *name* against the *target's own* `act_info.json` order, never hardcode an index. The asserts `count(ActSetName) > 0` and `size() > GlobalActFuncSetId` are the runtime's own defense against a stale index.

### Addressing a Function within the Resident Set

A subsequent `Activation` names its function by a silicon **LUT code** at bundle `+0x23`, produced by the 31-entry `ActivationFunctionType → uint8` func-remap (rodata `@0x1df57a0`). These codes equal the pwp `neuron_id`:

```text
Identity→0x01  Relu→0x02  Lrelu→0x03  Prelu→0x04  Sigmoid→0x05  Tanh→0x06
Exp→0x07  Sqrt→0x08  Softplus→0x09  Ln→0x0A  Sin→0x13  Erf→0x15  Silu→0x17
Cos→0x1C  Rsqrt→0x1D  Square→0x1E  Gelu→0x1F  Copy→0x80  Reciprocal→0x81 …
```

There is **no per-set slot index** — the func code is the per-function address key into the resident set's `bkt/ctrl` tables, and the engine resolves it against whichever set `LoadActFuncSet` last installed.

---

## Single-Set Residency — the AFTable Model

### Purpose

The Activation engine has **one** active LUT bank. The compiler/simulator models the resident set as `PWPSim::AFTable` ("Activation-Function Table"), keyed by set name in a `std::unordered_map<string, PWPSim::AFTable>` (the `_Hashtable<…basic_string…, pair<…AFTable>…>` symbol in libwalrus). A function set is a `(bkt_bin, ctrl_bin)` pair — the `bkt` blob is the piecewise-poly coefficient bank, the `ctrl` blob the region/breakpoint table (byte layout = [Part 10](../activation/)). Each set co-loads its hero transcendental(s) plus the always-on cheap functions.

### The Co-Residency Limit

The limit is the headline: **one set resident at a time.** `LoadActFuncSet` carries no LUT base address; the set index selects which named `AFTable` becomes the single active bank, evicting the prior one. This is why:

- To compute function `X`, you must load the *unique* set whose roster contains `X`; that load also makes every other function in that set available, and unloads the previous set.
- You cannot mix two heroes from different sets without a second `LoadActFuncSet`.
- The compiler runs a **set-cover** to minimise reloads (next section).

> **QUIRK — the engine's physical LUT-bank SRAM size is *not* in these binaries.** The co-residency limit is "one *set*", not a byte capacity the encoder checks. The per-set `bkt`/`ctrl` blob sizes (Part 10: `bkt` ≈ 8–46 KB, `ctrl` ≈ 0.5–8 KB) bound the bank from below, but the absolute SRAM size is a hardware spec absent from the compiler. The debug-dump string `" PWP tables, tried to load "` (in `LowerPWPImpl::dumpToFile`) traces the *loaded-set sequence*, not a capacity error — do not mistake it for an overflow check.

### The Set-Cover and Load Insertion

Three Activation-engine passes form the LUT-residency pipeline. Together they pay the (expensive but un-modelled) refill cost *structurally* — fewest loads, hoisted early, globally deduplicated:

| Pass | Function | Role | Confidence |
|---|---|---|---|
| `lower_act` (L28) | `LowerPWPImpl::calculateBestSets` @ `0x11597e0` | **set-cover** over the `std::array<pair<string,ActivationFunctionType>,29>` of LUT-driven funcs — pick the minimal sequence of resident sets so set-sharing activations don't each reload | STRONG |
| | `addLoadInstructionBefore` @ `0x1155620` → `generateInstLoadActFuncSet` @ `0x1156510` | mint the IT6 before the first activation needing a new set | CONFIRMED |
| `optimize_prefetch_act_control` (L29) | `OptimizePrefetchActLoadImpl` @ `0x11656e6` | **hoist** the load earlier to hide the refill behind upstream compute | CONFIRMED |
| `optimize_act_control` (L30) | `OptimizeActControl` @ `0x11603b0` / `…Impl::enterBasicBlock` @ `0x11618a0` | **global dedup** — eliminate a load whose set a dominating earlier load already made resident | STRONG |

> **NOTE — the prefetch pass bound-checks the index against a per-engine set count.** `optimize_prefetch_act_control` validates `Eng2UsedActTables->count(actTblLoad->getEngineInfo()) > 0 && (int)Eng2UsedActTables->lookup(actTblLoad->getEngineInfo()).size() > actFuncSetId` — `Eng2UsedActTables` is a `DenseMap<EngineInfo, vector<…>>` and `act_func_set_id` is bounded against the per-Activation-engine set count. The global dedup tracks the resident set with a `0xFFFFFFFF` ("no active set") sentinel — which is the same value as the struct default of `inst+0xF0` (`toJson` emits `act_func_set_id` only when the field `≠ -1`).

> **GOTCHA — the LUT load has *no* cycle-cost model in this build.** `Hwm::getLatency(InstLoadActFuncSet)` and `Hwm::getLatencyExec(InstLoadActFuncSet, EngineType)` are `__assert_fail` stubs (`"…getLatencyExec( const InstLoadActFuncSet &, bir::EngineType ) not implemented"`), and **no** arch subclass (`TrainiumHwm`, `CoreV4Hwm`) overrides them — only `InstActivation` itself is given a latency. The scheduler therefore cannot charge a typed cycle cost for a reload (calling the typed `getLatency` would assert); it amortises the load *structurally* via the set-cover + prefetch + dedup trio, treating it as a fixed/near-zero slot. A reimplementer modelling the perf-sim must not look for a `LoadActFuncSet` latency — there isn't one.

---

## The ISA Opcode Names

The `enum_variant_string_opcode(int, char*, int)` ISA-name switch (one body per `core_vN`) names the Activation-engine opcodes. They are gen-invariant unless noted:

| Opcode | Name (core_v2 == core_v4) | BIR op | Confidence |
|---|---|---|---|
| `0x21` | `Activate` | `InstActivation` (IT4, plain) | CONFIRMED |
| `0x22` | `ActivateQuantize` | act + quantize fused | CONFIRMED |
| `0x23` | `ActivationTableLoad` | **`InstLoadActFuncSet` (IT6)** — the LUT load | CONFIRMED |
| `0x24` | `ActivationReadAccumulator` | `InstReadActivationAccumulator` / `InstActivationReadAccumulator` | CONFIRMED |
| `0x25` | `Activate2` (core_v4 ONLY; core_v2 = ∅) | `InstActivation` (IT4, `is_activate2`) | CONFIRMED |
| `0x30` | `Exponential` | `InstExponential` (DVE, hardwired) | CONFIRMED |
| `0xC6` | `PseudoLoadActFuncSet` (both gens) | scheduling pseudo | CONFIRMED (name) |

> **NOTE — `0xC6` "PseudoLoadActFuncSet" is a real ISA-named pseudo, lowered to `0x23`.** Both rodata strings exist (`"PSEUDO_LOAD_ACT_FUNC_SET"` and `"PseudoLoadActFuncSet"`), and `0xC6` is referenced by all three `core_vN` `enum_variant_string_opcode` bodies. The most likely role: the real load is the `0x23` "ActivationTableLoad"; `0xC6` is the placeholder the scheduler/prefetch passes manipulate (a pseudo so the resident-set bookkeeping survives hoisting/dedup without consuming a real engine cycle), later materialised to `0x23`. The exact `0xC6 → 0x23` lowering site is INFERRED.

---

## The Dynamic-PWP Gate

### Purpose

Dynamic-PWP is the mode in which LUT sets are loaded *at runtime* (via the `LoadActFuncSet` ops the compiler inserts) rather than from a single pre-baked static ROM image. When active, the compiler emits the set-cover load ops *and* advertises the capability to the runtime through the NEFF, so the runtime expects dynamic table loads.

### The NEFF Feature

`NeffPackager::writeNEFFFeatures` @ `0x15294b0` walks a series of `Module::getAttribute(ModuleAttribute)` tests, each setting a bit in a feature bitmask. The dynamic-PWP branch:

```c
// NeffPackager::writeNEFFFeatures @ 0x15294b0 (dynamic_pwp arm)
if (Module::getAttribute(ModuleAttribute::dynamic_pwp) is set):     // call @ 0x15295c6
    neff_features.push_back("neff_feature_dynamic_pwp")             // string @ 0x1c8695a
    feature_mask |= 0x40                                            // or r14, 0x40 @ 0x1529c5d
```

| Facet | Value | Confidence |
|---|---|---|
| NEFF feature bit | `0x40` | CONFIRMED |
| Feature string | `"neff_feature_dynamic_pwp"` | CONFIRMED |
| Module attribute | `ModuleAttribute "neff_feature_dynamic_pwp"` (registered in `ModuleAttribute2string`) | CONFIRMED |
| Internal toggle | global `enableDynamicActTable` (.bss); literal `"DynamicActTable"` | STRONG |
| Wire key | `"dynamic_act_table"` (BIR-JSON) | CONFIRMED |

> **NOTE — there are two layers to the gate.** The **module attribute** `neff_feature_dynamic_pwp` (set by the front-end/HLO lowering) drives the NEFF feature bit and tells the *runtime* to expect dynamic loads. A separate **internal global** `enableDynamicActTable` (with the literal `"DynamicActTable"`, read at the queue-allocation/codegen layer) selects static-vs-dynamic act-table *mode* inside the compiler: static mode = a single legacy pre-loaded ROM set (no per-op `LoadActFuncSet`); dynamic mode = the set-cover path that emits the `LoadActFuncSet` ops. The exact upstream producer that *sets* the attribute / global — i.e. the precise static↔dynamic decision rule — is upstream of these binaries (HLO/front-end) and is the one SPECULATIVE seam on this page.

---

## Gaps and Confidence

- **`EngineAccumulationType` ordinals — INFERRED.** The member *names* (`Idle`, `ZeroAccumulate`, `AddAccumulate`, `LoadAccumulate`) are CONFIRMED from simulator strings; the integer values `{0,1,2,3}` are inferred from conventional ordering and the sibling PE page, because the typed enum is reconstructed only via its `2string`/`string2…` switch, not recovered as a static value table.
- **The bkt/ctrl blob byte layout — out of scope (Part 10).** This page establishes *selection + addressing*; the physical packing of functions inside the loaded image is owned by [Part 10](../activation/).
- **The static↔dynamic decision rule — SPECULATIVE.** The role of `enableDynamicActTable` / `neff_feature_dynamic_pwp` is CONFIRMED; the upstream pass that *sets* them, and the precise rule, are not in these binaries.
- **`0xC6 → 0x23` materialisation site — INFERRED.** That `PseudoLoadActFuncSet` is a real ISA-named pseudo is CONFIRMED; where it lowers to the real `0x23` load is inferred from the prefetch/dedup linkage.
- **Physical LUT-bank SRAM size — not in these binaries.** A hardware spec; bounded from below only by the per-set blob sizes.

## Cross-References

- [PE Engine — the Systolic Matmul Array](pe-engine.md) — the sibling engine; the fp32-PSUM accumulator and the `EngineAccumulationType` enum this page reuses for the channel accumulator.
- [PWP Activation Model — sets, bkt/ctrl blobs, FindActInfo](../activation/) — Part 10: the piecewise-polynomial math, the 21/14-set roster, and the blob byte-format this page selects but does not decode.
- [ISA: Activation Encoding](../isa/activation-encoding.md) — Part 2.11: the bit-level 64-byte bundle layout of `Activate`/`ActivationTableLoad`/`ActivationReadAccumulator` this page describes functionally.
- [BIR Instruction Hierarchy](../bir/) — Part 7: `InstActivation` / `InstLoadActFuncSet` / `InstActivationReadAccumulator` as `bir::Instruction` nodes, their `getBiases`/`getSummation`/`getAcc` accessors and `validEngines`.
- [SBUF / PSUM Bank Geometry](sbuf-psum-geometry.md) — the SBUF the Activation ifmap streams from and the memory the accumulator drains to.
