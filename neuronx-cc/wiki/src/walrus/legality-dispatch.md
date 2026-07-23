# Legality Dispatch — L0 (dormant) vs L1 (birverifier) vs L2 (wire)

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 share the `.text` logic). The legality DATA lives in `neuronxcc/starfish/lib/libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`); the live enforcement lives in `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331…`). For `.text`/`.rodata` the virtual address equals the file offset in both. `.data.rel.ro` / `.got` carry a small negative file-offset delta in libBIR (`-0x1000`) — do not `xxd` a vtable at its VA. Other wheels differ; treat every address as version-pinned.*

## Abstract

"Is operation X legal on engine E at architecture A?" is answered **three different times** in this compiler, by three independently-authored encodings, and — the headline — **only two of them run in this wheel**. A reimplementer who finds the obvious `bir::Instruction::isValidEngine` predicate in `libBIR.so` and wires their backend to call it will have reproduced a layer that the shipping product never invokes. The real gate is a conjunction of two libwalrus checks; the libBIR table is documentation-grade data with zero runtime consumers.

The three layers, and which is live:

- **L0 — the libBIR static `(ArchLevel × EngineType)` table.** 106 per-class `getValidEngines` maps, reached through `bir::Instruction::isValidEngine` (`0x2d6b10`). A pure two-level hash lookup: `arch ∈ outerMap ∧ engine ∈ innerSet[arch]`. **DORMANT.** `isValidEngine` is exported but called by *nothing* — zero internal libBIR callers, zero cross-binary importers, zero Python references.
- **L1 — the libwalrus `birverifier` per-op structural gate.** `birverifier::checkValidEngines` (`0x103a200`), driven by the `birverifier` BackendPass `Verifier::run` (`0xf9f120`). Each `visitInst<Op>` builds its legal-engine `std::vector<EngineType>` **inline** — re-encoded in the verifier source, *not* read from L0 — and the gate compares it against the instruction's assigned engine (`Inst+0x90`). **LIVE.**
- **L2 — the libwalrus wire validator.** `core_v4::is_valid_neuron_engine_instruction` (`0x1502120`), reached through `runSingleISACheck` and replayed in bulk by codegen's `flushISAChecks`. Validates the fully-packed 64-byte silicon bundle. **LIVE.**

The defended invariant is **L1 ∧ L2** — both in libwalrus, conjunctive (passing L1 does **not** imply passing L2), with no call edge between them or to L0. This page pins which layer runs, proves L0 is dead code, and documents the two libBIR-native *structural* predicates the verifier additionally drives across the L1/L2 boundary: the write-in-place aliasing engine (`validateWriteInPlace`) and the one concrete per-op `verify` in all of libBIR (`InstGPSIMDSB2SB::verify`).

For reimplementation, the contract is:

- The L0 lookup body and its **proof of dormancy** (the import-table and xref evidence) — so you know not to call it.
- The L1 `checkValidEngines` predicate: the wildcard rules, the inline `legalSet` construction, the TSP special-case, and the `NeuronAssertion 400` failure.
- The L1↔L2 bridge: the embedded `std::variant<…CoreVxGen>` the verifier constructs, and the `CodeGenMode` it constructs it with (corrected below).
- The two libBIR structural predicates the live path additionally uses: write-in-place validity and the GPSIMD-SB2SB special-casing.

| | |
|---|---|
| **L0 predicate (dormant)** | `bir::Instruction::isValidEngine(ArchLevel, EngineType)` @ `0x2d6b10` (libBIR) |
| **L0 accessor** | `getValidEngines` — virtual, vtable slot `+0x60`; 106 per-class overrides |
| **L1 predicate (live)** | `birverifier::checkValidEngines(Instruction&, vector<EngineType>)` @ `0x103a200` (libwalrus) |
| **L1 driver** | `neuronxcc::backend::Verifier::run(Module&)` @ `0xf9f120` (pass name `birverifier`) |
| **L1↔L2 bridge** | `birverifier::InstVisitor::initCodegen` @ `0xfc5d00` — embeds `CoreVxGen`, `CodeGenMode = 2` |
| **L2 predicate (live)** | `core_v4::is_valid_neuron_engine_instruction` @ `0x1502120`; selector `runSingleISACheck` |
| **Engine enum (L0/L1)** | `EngineType {0 Unassigned, 1 Pool, 2 Activation, 3 PE, 4 DMA, 5 DVE, 6 SP, 7 ALL}` |
| **Arch enum (L0/L1)** | `ArchLevel {10 inferentia, 20 sunda, 30 gen3, 40 core_v4, 50 core_v5}` |
| **Write-in-place gate** | `bir::validateWriteInPlace` @ `0x335170`; rule body `checkWriteInPlaceValidity` @ `0x204290` (libBIR) |
| **GPSIMD-SB2SB verify** | `bir::InstGPSIMDSB2SB::verify` @ `0x294e00` → `isCompatible` @ `0x2931d0` (libBIR) |

---

## L0 — the dormant libBIR `(ArchLevel × EngineType)` table

### Purpose

`bir::Instruction::isValidEngine` is the IR-level "may this op run on this engine at this arch?" predicate. It is structurally complete, exported, and answers a boolean — and it is never called. It is the clearest instance in the toolchain of a compile-time spec that survived into the shipped binary as pure data.

### The lookup body

`bir::Instruction::isValidEngine(ArchLevel a2, EngineType a3) const` (`0x2d6b10`, 215 bytes, `.dynsym` GLOBAL/DEFAULT, exported) is a two-level `std::unordered_map<ArchLevel, unordered_set<EngineType>>` walk. The map object is fetched through the virtual accessor `getValidEngines` at vtable slot `+0x60` — so *which* map is selected by the instruction's dynamic type, while *which cells* are tested are the caller-supplied `(arch, engine)` pair.

```c
// bir::Instruction::isValidEngine @0x2d6b10 — annotated from decompiled + disasm
bool isValidEngine(this, ArchLevel a2, EngineType a3):
    map = (*(vtable + 0x60))(this)            // getValidEngines() → .bss map object
    nOuter = map._M_bucket_count              // map+0x08
    head   = map._M_buckets[a2 % nOuter]      // open-hashing _Mod_range_hashing
    if (!head) return false                   // arch bucket empty → FALSE (no throw)
    node = head
    while (node.key != a2):                   // chain-walk on ArchLevel key (node+0x08)
        node = node._M_nxt
        if (!node || (a2 % nOuter) != (node.key % nOuter)):
            return false                      // ran off the bucket → FALSE
    set    = node.innerSet                    // node+0x10 (embedded unordered_set<EngineType>)
    nInner = node.innerBucketCount            // node+0x18
    b      = set._M_buckets[a3 % nInner]
    if (!b) return false                      // engine bucket empty → FALSE
    for (e = b; ; e = e._M_nxt):              // membership chain-walk
        if (e.value == a3) return true        // engine ∈ innerSet[arch] → TRUE
        if (!e._M_nxt || (a3 % nInner) != (e._M_nxt.value % nInner)):
            return false
```

`LEGALITY = (arch ∈ outerMap) ∧ (engine ∈ innerSet[arch])`. A not-found on **either** level returns `0` — no throw, no assertion, no diagnostic. The accessor is invoked twice (entry and after the arch match) with the second result discarded; `getValidEngines` is a side-effect-free GOT-cell load, so this is just the compiler not eliding a repeated inline of the trivial virtual.

*Anchors: 215-byte body; `0x2d6b26 call qword ptr [rax+60h]`, `0x2d6b32 div rsi` (the modulo), `0x2d6bdb mov eax,1 / ret`. Prologue at `0x2d6b10` = `41 55 41 89 d5 41 54 41 89 f4` = `push r13; mov r13d,edx`, saving the `EngineType a3`.*

The 106 `getValidEngines` overrides are virtual at vtable `+0x60`, proven by relocation rather than inference: `InstMatmult` vtable `@0x8fd9c0` → vptr `+0x10` → slot `+0x60` = VA `0x8fda30`, and `readelf -rW` shows `R_X86_64_64 0x8fda30 → InstMatmult::getValidEngines (0x32b600)`. Each override is an 8-byte stub returning its own `<Class>::validEngines_ptr` GOT cell; the `(arch, engine)` literals exist only in the static-initializer instruction stream (`sub_1D71A0 @0x1d71a0`, building all 105 concrete maps in `InstructionType` order), not as a parseable rodata table. The count of 106 holds four independent ways: 106 `getValidEngines` functions, 106 `validEngines` data symbols, 106 `_ptr` GOT cells, and 106 decompiled stubs.

> **NOTE — the base `validEngines` map is populated, not empty.** `sub_1D5BB0 @0x1d5bb0` builds `bir::Instruction::validEngines` with **all five** arch keys (10/20/30/40/50), including inferentia(10), which every *concrete*-op map omits. It is the permissive fallback a non-overriding op would inherit — but all 105 concrete ops do override `getValidEngines`, so it is never the selected map for a real op. That five keys are built is exact; the per-arch engine set was not byte-traced.

### Proof of dormancy

The dormancy claim is the central fact of this page and is verified three ways, all reproducible:

1. **Zero internal callers.** The libBIR `xrefs.json` has **no** `"to": "0x2d6b10"` entry. The only reference *mentioning* `0x2d6b10` is `{"from": "0x2d6b10", "to": "0x2d6b12"}` — an intra-function jump *out of* `isValidEngine` itself, not a call *into* it. A search for `"to": "0x2d6b10"` over the xref table returns nothing.
2. **Zero cross-binary importers.** No `.so` in the wheel lists `isValidEngine` or any `getValidEngines` as an undefined import. In particular `libwalrus.so` **does** import many `bir::` symbols — `listArchEngineInfos`, `ArchLevel2string`, `getEngineCount`, `GenericLowering::loweringChoices` are all present in its `native_imports.json` — but `isValidEngine` / `getValidEngines` are **absent**. The live backend reaches into libBIR for engine *metadata* and lowering, never for this legality predicate. Searching every libwalrus import table for `isValidEngine` or `getValidEngines` yields zero hits, while the four metadata imports above are all present.
3. **Zero Python references.** No `.py` references it.

So the libBIR `(ArchLevel × EngineType)` table is a self-contained compile-time spec whose live consumer is not present in this build. Whether some sibling artifact (a different `GenImpl`, a standalone bir tool, another wheel variant) calls it is out of scope — the claim is **wheel-local** to this cp310 wheel.

> **GOTCHA —** `call qword ptr [rax+60h]` *does* appear at two other libBIR sites — `checkWriteInPlaceValidity` (`0x204290`) and `isBasePartitionHardConstrained` (`0x313b00`) — but those are on **different object types** whose vtable slot `+0x60` is a different virtual, not `getValidEngines`. The vtable slot is exercised; the `isValidEngine` `(arch, engine)` *test* is not. Do not mistake a slot-`+0x60` call for L0 enforcement.

---

## L1 — the live `birverifier` engine gate

### Purpose

L1 is the symbolic, pre-codegen legality layer: it walks the BIR `Module` instruction-by-instruction and throws on the first structural violation. It is the libwalrus class `birverifier::InstVisitor`, a CRTP IR visitor that is the structural twin of the simulator's `birsim::InstVisitor` — same 110-arm switch on `InstructionType` (`Instruction+0x58`), same enter/leave hooks — except the verifier *checks* each op where the simulator *executes* it. The engine sub-check is `checkValidEngines`.

### The dispatch and the engine predicate

`bir::IRVisitor<birverifier::InstVisitor, void>::visit` (`0xfa26b0`) is a single switch on `Instruction+0x58` (the `InstructionType`, cases 0..109) tail-calling `visitInst<Op>`; the three structured-control-flow ops (105 Loop / 106 DynamicForLoop / 108 DoWhile) are intercepted in an outer switch and recurse the visitor over their region bodies. Every substantive `visitInst<Op>` runs, in order: `visitInstruction` (base common checks, `0x103c4f0`) → `checkMemType` (per-operand memspace) → `checkArchLevel(min, max)` (arch-version gate) → **`checkValidEngines`** (engine gate) → op-specific `check<X>` helpers.

`birverifier::checkValidEngines(bir::Instruction const& I, std::vector<bir::EngineType> legalSet)` (`0x103a200`, thunk `0x5f9df0`, body read in full):

```c
// birverifier::checkValidEngines @0x103a200 — annotated
void checkValidEngines(I, legalSet):
    eng = *(uint32*)(I + 0x90)                 // the instruction's ASSIGNED bir-engine
    // (a) Function-attribute fast path: if the Function carries a "skip-engine-check"
    //     bool attribute (attr-map key 0x18 = 24) set true → return early.
    if (Function.attrs.get(0x18) == true) return;
    // (b) instruction-side wildcard: engine ALL(7) or Unassigned(0) → unconstrained.
    if (eng == 7 || eng == 0) return;
    // (c) scan legalSet for eng (4-wide unrolled int32 compare). 7(ALL) in the set
    //     is itself a wildcard pass.
    if (eng in legalSet || 7 in legalSet) return;     // assigned engine ∈ legalSet → LEGAL
    // (d) TSP special-case: InstTensorScalarPtr (IT 29) delegates.
    if (*(uint*)(I + 0x88) == 29) return checkValidEnginesTSP(I, …);   // @0xfd2ff0
    // (e) NOT legal: build the "wrong engine" diagnostic and THROW.
    //     <stringstream of EngineType2string over each legalSet elem → "{DVE, Pool}">
    throw logging::NeuronAssertion<ErrorCode>(
        /*code*/ 400, /*named args*/ {validEngines, opcode, arch, engine},
        "Please open a support ticket … XLA_IR_DEBUG/XLA_HLO_DEBUG …")
        @ "…/walrus/verifier/src/inst_visitor.cpp:1368";
```

So `LEGALITY(I) = (I.engine ∈ {0, 7}) ∨ (I.engine ∈ legalSet) ∨ (7 ∈ legalSet) ∨ (Function has the skip attribute)`; otherwise `NeuronAssertion 400`. Sixty-two `visitInst<Op>` bodies call it directly.

*Anchors: prologue at `0x103a200` = `41 57 49 89 ff 41 56` = `push r15; mov r15,rdi; push r14`; the `EngineType2string` formatting and the `inst_visitor.cpp:1368` string.*

### The `legalSet` is re-encoded inline — *not* read from L0

This is the crux that separates L1 from L0. Each `visitInst<Op>` constructs its own `std::vector<EngineType>` inline and hands it to `checkValidEngines`; it does **not** consult `libBIR::validEngines`. Two byte-traced exemplars:

```c
visitInstPool   @0xfa6300:  tc_new(4); *p = 5;                 // → {DVE}
visitInstMemset @0xfa58a0:  tc_new(8); *p = 0x100000005;       // → {DVE=5, Pool=1}
                                                               //   (the 64-bit literal
                                                               //    packs two int32 EngineTypes
                                                               //    low→high = 5, 1)
```

The legal-engine set is literally a baked-in `int32[]` of `EngineType` values per opcode in the verifier `.text`. L0 and L1 therefore encode the **same fact** (op → legal engines) in two separate source files, kept in sync by convention, not by a shared lookup. They *can* drift, and nothing at runtime catches it because L0 is never consulted. The systematic L0-map ↔ L1-vector content diff across all 62 ops is a follow-up (the per-family deep-dives 8.42+, PLANNED) — the per-op L1 checks this layer dispatches to are documented in the per-op birverifier page (8.40, PLANNED). Both exemplars are byte-traced, and libwalrus does not import `getValidEngines` at all (see L0 dormancy #2).

The driver is the `birverifier` BackendPass: `Verifier::run` (`0xf9f120`) materialises a `birverifier::Config` from the Module's `PassOptions`, constructs the `InstVisitor`, runs `initCodegen` (the arch bridge, below), `enterModule` (module-scope pre-checks — `checkSbIO`, `checkFP8TypeConsistency`, `checkInstCount`, plus `EnumeratePrivateIndexes` assigning the error-map key), then walks `main` first and the remaining functions per-BB/per-instruction. The CLI flags `enableVerifier` / `enableVerifierForAll` / `enable-verifier-uninit-read-check` re-interpose this pass after nearly every other backend pass, so the L1 invariant is re-checked across the pipeline, not once. The `EngineType` space here is the **bir** enum (PE=3, Act=2, Pool=1, DVE=5, SP=6), distinct from the L2 wire enum.

### The L1 ↔ L2 bridge — and a corrected `CodeGenMode`

`birverifier::InstVisitor::initCodegen` (`0xfc5d00`) is the in-pass coupling between L1 and L2. It reads `Module+0xAC` (ArchLevel) and constructs an **embedded** `std::variant<monostate, CoreV2Gen, CoreV3Gen, CoreV4Gen>` at `InstVisitor+704` (discriminator tag at `+1600`), tagged by arch — so the verifier carries a live codegen `Generator` it can reach the L2 wire helpers through. The arch arms: `0x14`(sunda)→CoreV2Gen, `0x1E`(gen3)→CoreV3Gen, `0x28`(core_v4)→CoreV4Gen; inferentia(10)/core_v5(50) have **no** arm (no Generator built — consistent with libwalrus's wire layer being core_v2/3/4 only).

The 5th ctor argument is the `Generator::CodeGenMode`, and the immediate passed there is `2`:

> **GOTCHA — mode `2` is `GENERATE_ISACODE`, and the error string lists the modes in the opposite order.** The `CodeGenMode` ordinals are **`COLLECT_OPCODES = 0`, `RUN_ISA_CHECKS = 1`, `GENERATE_ISACODE = 2`**, fixed by the dispatch branch order. The human-readable error string lists them `GENERATE, RUN_ISA, COLLECT` — i.e. `2, 1, 0` — so reading the message top-down maps `2` onto the wrong name. `initCodegen` constructs each `CoreVxGen` with `mov $0x2,%r8d`, so the verifier's embedded Generator runs in GENERATE_ISACODE, not RUN_ISA_CHECKS. It is the *second* arch-select site in libwalrus; the production emit `Codegen::codegen @0x11d2c50` is the first, and drives only modes 0 and 1. See [the codegen driver's mode table](codegen-driver.md#the-codegenmode-tristate).
>
> *Anchors: `0xfc5d12 mov 0xac(%rsi),%eax` (arch read); `0xfc5d18/1d/26 cmp 0x28/0x1e/0x14`; `0xfc5d86` / `0xfc5dee` / `0xfc5e56` `mov $0x2,%r8d` immediately before the `CoreV4Gen` / `CoreV3Gen` / `CoreV2Gen` ctor `@plt` calls, whose symbols type the 5th param as `Generator::CodeGenMode`. The variant-tag write at `0xfc5df9 movb $0x2,0x640(%rbx)` is the discriminator at `+1600`, a separate datum.*

Note what mode 2 (`GENERATE_ISACODE`) implies functionally: per the codegen driver, GENERATE builds and `fwrite`s the bundle but does **not** enqueue it for deferred validation, and the end-of-walk `flushISAChecks` (gated `mode != 0`) finds an empty queue and is a no-op. The exact L2 wire-format helpers the L1 verifier reaches *through* this embedded Generator (vs. those run only by the real codegen emit pass) is the in-pass L1∧L2 coupling and is J/G12 strand scope. This is the L1→L2 path; its precise reach is [INFERRED], not line-traced here.

---

## L2 — the live wire validator (and the conjunction)

L2 is the silicon-legality layer: it validates the fully-packed 64-byte `NEURON_ISA_TPB_INST_UNION` against the ISA format rules for its wire engine and core version, through a per-arch `is_valid_*` cascade. Its entry is `core_v4::is_valid_neuron_engine_instruction` (`0x1502120`), selected per-arch by `runSingleISACheck` (V4 `0x1435010` / V3 `0x134c060` / V2 `0x1211cf0` = vtable slot 0 of the arch `Generator`), preceded by `neuron_isa_check_opcode_on_engine` (`0x1504700`). In production it is reached not inline but **after emit**: the codegen pass (mode `RUN_ISA_CHECKS = 1`) snapshots every emitted bundle into a queue and a single TBB-parallel `flushISAChecks` replays the queue through `runSingleISACheck` at module end — *validate-after-emit, fail-the-pass*. The driver mechanics are the sibling [codegen driver](codegen-driver.md) page; the `is_valid_*` cascade body is the L2 wire-validator page (8.41, PLANNED).

Three facts make L2 a **distinct, conjunctive** gate rather than a redundant copy of L1:

- **Engine-enum space differs.** L1 uses the bir `EngineType` (PE=3, Act=2, Pool=1, DVE=5, SP=4… SP=6). L2 uses the **wire** `NEURON_ISA_TPB_NEURON_ENGINE` enum (PE=0, ACT=1, POOL=2, DVE=3, SP=4); a `CoreV4GenImpl::convert()` remap (`sub_1433840`: 1→2, 2→1, 3→0, 5→3, 6→4) sits between the layers. A legality answer is remapped on the way from L1 to L2.
- **Arch space differs.** L1 keys on bir `ArchLevel` (10..50); L2 keys on **core version** (V2/V3/V4 — three separate validator families of 59/64/81 validators respectively, nm-counted).
- **What is checked differs.** L1 asks "is this op allowed on the engine it is *assigned to*, at this arch?" — a coarse op×engine×arch gate over the abstract IR. L2 asks "does the *fully-encoded 64-byte silicon word* satisfy the ISA format rules?" — dtype tags, access-pattern strides, PSUM/SBUF quadrant nibbles, fp8 dual-restrictions, partition reach — a much finer, post-codegen gate keyed on the packed word.

Because they are finer/coarser views, **passing L1 does not imply passing L2**: e.g. the dual-FP8 restriction (`s3_lw_dual_fp8_restrictions @0x14ac040`) is enforced *only* at L2, with no L1 equivalent. The single defended invariant is therefore **L1 ∧ L2** — both libwalrus, conjunctive — with L0 as documentation-grade data and the Python `NeuronVerifier` as a front-end fast-fail mirror. That L1 and L2 are independent encodings with no call edge is exact; the dual-FP8 example is cited from the L2 page rather than re-decoded here.

> **The Inferentia(10) asymmetry.** L0's base map carries arch 10, but every concrete-op L0 map omits it. Since L0 is dormant this has no runtime effect; it is a spec-level statement that "no concrete op is individually legal on inferentia." Inf1 support is realized elsewhere (a different `GenImpl` / wheel), consistent with libwalrus's wire layer being core_v2/3/4 only — and with `initCodegen` building no Generator arm for arch 10.

---

## The two libBIR structural predicates the live path drives

L0's engine table is dead, but two genuine libBIR-native *structural* legality bodies are live — invoked externally (zero libBIR internal callers; the consumer is libwalrus). They are not engine×arch gates; they are aliasing and shape rules.

### Write-in-place validity

`bir::validateWriteInPlace(MemoryLocation const& loc, vector<AccessPattern const*>* outConflicts)` (`0x335170`, exported via thunk `0x17c9c0`) is a full in-place aliasing engine: for a buffer an op writes in place, it pairs every `(writer, reader)` access-pattern and tests each pair through `checkWriteInPlaceValidity` (`0x204290`, 2185 B — the actual rule body). It returns false / fills `outConflicts`; it never throws — the consumer (libwalrus) raises the user-facing error. The rule body walks, in order:

```text
checkWriteInPlaceValidity(W, R):           // 0x204290  (1 = in-place LEGAL)
  STEP 1  if !MemoryLocation::overlaps(W.loc, R.loc):  return 1   // disjoint ⇒ trivially OK
  STEP 2  DRAM short-circuit (both type-8): same-MemLocSet equality vs set-overlap
  STEP 3  if !doAccessesOverlap(W, R):                 return 1   // AP windows disjoint ⇒ OK
  STEP 4  per-opcode (inst.IT @ inst+0x58):
            IT 0x35 CustomOp / 0x36 BIRKernel → trusted only if one literal buffer
            if isTransposeInstruction(inst):           return 0   // transpose ⇒ NEVER in-place
  STEP 5  address-equality invariant:
            if sameStart && sameDtype && sameNEP:  return sameRawPattern(W, R)
  STEP 6  PE-replication exception:
            eng = inst.engine_id (+0x90)
            if is2x2pAble(eng,arch) || is4x2pAble(eng,arch):  return 0  // 2-row/4-row replication
                                                                       //   ⇒ non-identity address map
  STEP 7  fallback:
            if readsFullInputBeforeCompute(inst):      return 1   // IT35 BNStatsAggregate
            if both SBUF: per-partition address-interval set test; collision ⇒ 0, else 1
```

The transpose ban (STEP 4) and the PE-replication ban (STEP 6) are the legality-relevant special cases: a transpose op may never write in place, and an op bound to an engine capable of 2-row/4-row partition-replication packing (`is2x2pAble` / `is4x2pAble`) is forbidden in-place because replication makes the per-partition address mapping non-identity, so "same start ⇒ alias-safe" is unsound. When the op's engine is `Unassigned(0)`, STEP 6 instead walks the op's `getValidEngines` candidates and bans in-place if *any* candidate is replication-capable — the one place the L0 *accessor* (vtable `+0x60`) is reached, though still not the `isValidEngine` predicate. Cross-references show only `validateWriteInPlace`'s own neighbourhood reaching the rule body — no other libBIR caller.

### GPSIMD-SB2SB — the one concrete per-op `verify` in libBIR

`bir::InstGPSIMDSB2SB::verify(unsigned long ctx, bool emitError)` (`0x294e00`) is the **only** concrete `::verify` body in all of libBIR (no base `Instruction::verify` exists; `nm` confirms a single `bir::*::verify(` symbol). It validates a thin gate then delegates the shape/context rules to `isCompatible` (`0x2931d0`):

```text
verify:        cnt = *(uint*)(this+0x90)        // engine_id / sub-mode field
               if (cnt > 1):  emitError ? assert@GPSIMDSB2SB.cpp:71 : return 0
               if (input-list empty || output-list empty):
                              emitError ? assert@:74 : return 0
               return isCompatible(ctx, module, ifmap, dst, emitError)

isCompatible:  C1 :21  ctx != 2                        ⇒ illegal
               C2 :25  Module+0xAC <= 29 (arch < gen3) ⇒ illegal   // GPSIMD-SB2SB is gen3+
               C3 :29  ifmap.dtype != dst.dtype        ⇒ illegal
               C4 :33  ifmap.NEP*step != dst.NEP*step  ⇒ illegal
               C5 :40  numDims ∉ {2,3,4,5}             ⇒ illegal
               C6 :43  either MemLoc.type != 16 (SB)   ⇒ illegal   // the "SB2SB" constraint
               C7 :46  AP.(vtable+0x40)() == true      ⇒ illegal   // non-plain-physical AP
               C8 :52  EffectiveBytesPerPartition > 1024 ⇒ illegal // 8-bit charged ×4, 16-bit ×2
```

`EffectiveBytesPerPartition` (`getEffectiveBytesPerPartition @0x291a50`) charges 8-bit dtypes ×4 and 16-bit ×2 against the 1024-byte-per-partition budget (the GPSIMD lane packs sub-32-bit elements). This is a GPSIMD-specific byte normalisation, not a generic BIR rule. `Module+0xAC` (C2) is the same ArchLevel field `initCodegen` and `checkArchLevel` read. *Anchors: `GPSIMDSB2SB.cpp:71/74` line strings; `MaxBytesPerPartition` at `.rodata 0x784a00` = 1024.* The `ctx==2` semantics is [INFERRED] to be a fixed GPSIMD config or AP-count constant; it is not pinned.

> **NOTE on the hardware model.** libBIR's `Hwm` class — the would-be address-bounds / partition-limit / latency model — is a pure abstract interface here: `Hwm::isValid(MemoryLocation&) @0x473240` is a 4-byte `return 1;` stub, and every sibling `Hwm` method (`getStartAddress`, the ~60 `getLatency(InstX)` overloads, …) asserts `"not implemented"`. The concrete per-arch `Hwm` (real SBUF/PSUM/DRAM bounds) is supplied by the consumer and registered via `Hwm::getSingletonMap` (`0x478960`, hash seed `0xC70F6907`), which is empty in libBIR. So address-legality, unlike write-in-place and GPSIMD-SB2SB legality, *is* fully deferred.

---

## What pins the five strongest claims

1. **L0 is dormant.** Three independent negatives: no `"to": "0x2d6b10"` anywhere in the libBIR xrefs (only a self-jump `from`); no `isValidEngine`/`getValidEngines` in any libwalrus or other `.so` import table, while four other `bir::` metadata imports *are* present; and no Python references. This is the best-defended claim on the page.
2. **L1 is the live engine gate and re-encodes its own `legalSet`.** `checkValidEngines @0x103a200` is byte-exact at the prologue (`41 57 49 89 ff 41 56`), two inline-vector exemplars are byte-traced (`{DVE}` and `{DVE, Pool}`), and libwalrus does not import L0's accessor. The full 62-op L0↔L1 content reconciliation is deferred, not done here.
3. **The verifier embeds a Generator in `CodeGenMode = 2` (GENERATE_ISACODE).** `initCodegen @0xfc5d00` reads the arch at `+0xAC` and issues three `mov $0x2,%r8d` immediately before the typed-`CodeGenMode` `CoreVxGen` ctor calls; the ordinals are cross-checked against the codegen-driver page.
4. **L1 ∧ L2 is conjunctive — passing L1 does not imply passing L2.** The enum and arch-space differences are exact. The dual-FP8-only-at-L2 example is cited from the L2 page rather than re-decoded here, and the *precise* set of L2 checks the verifier's embedded Generator reaches in-pass is [INFERRED], not line-traced.
5. **Write-in-place and GPSIMD-SB2SB legality logic live in libBIR; address-legality (`Hwm`) is deferred.** Both rule bodies were read, and `Hwm::isValid` is a 4-byte stub.

What this page does **not** pin: the `ctx==2` GPSIMD constant, which is [INFERRED]; the exact per-arch engine sets in the base `validEngines` map; and any NEFF or BIR-JSON fixture cross-check of a real emitted instruction against either live layer, which remains a standing gap.

---

## Related Components

| Name | Relationship |
|---|---|
| [Codegen Driver and the CodeGenMode Mechanism](codegen-driver.md) | The production emit pass (`Codegen::codegen`); owns `CodeGenMode`, `runSingleISACheck`, and the L2 `flushISAChecks` deferred validation this page references |
| Per-op birverifier checks (8.40, PLANNED) | The per-`visitInst<Op>` L1 checks `checkValidEngines` dispatches alongside |
| L2 wire validator (8.41, PLANNED) | The silicon-legality `is_valid_*` cascade `runSingleISACheck` enters |
| [Arch Object Model](../arch/arch-object-model.md) | The `Module+0xAC` ArchLevel discriminator (`0x14/0x1E/0x28`) every layer reads |
| [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md) | The wire `NEURON_ISA_TPB_NEURON_ENGINE` enum L2 keys on (vs the bir `EngineType` of L0/L1) |

## Cross-References

- [Codegen Driver and the CodeGenMode Mechanism](codegen-driver.md) — the `CodeGenMode` ordinals and the L2 validate-after-emit flush
- [Arch Object Model](../arch/arch-object-model.md) — the ArchLevel codes the L1↔L2 bridge selects on
- [The 64-Byte Instruction Bundle & Header Skeleton](../isa/instruction-bundle.md) — the packed word L2 validates
