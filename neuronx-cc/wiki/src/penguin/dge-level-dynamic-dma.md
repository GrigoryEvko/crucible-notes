# DGE Level Selection and the `dynamic_dma_*` Pass Family

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core). The route predicate, the three `dynamic_dma_*` passes, and `assign_hwdge_engine` live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset). The persisted `bir::DGEType` enum and `DGEType2string` live in `libBIR.so`. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

A *dynamic-offset DMA* is one whose source or destination access pattern (AP) is not a compile-time constant — the partition/free offsets are runtime values held in registers, materialized at execution time. Walrus cannot emit such a DMA as a plain static descriptor; it must instead choose a **descriptor-generation engine** (DGE) and a scheme for producing the descriptor stream at run time. This page documents how that choice is made and how it is staged, stamped, and verified across the backend pipeline.

Two enums with deceptively similar vocabulary drive the whole subsystem, and conflating them is the single biggest trap here. **`DGELevels`** is the user/pass *configuration* set — the `cl::list<DGELevels>` parsed from `--dge-levels`, telling the compiler what classes of DGE DMA it is *allowed* to generate (`{spill_reload, scalar_dynamic_offset, vector_dynamic_offsets, dynamic_size, dst_reduce, transpose}`). **`bir::DGEType`** is the per-instruction *result* tag actually written into each `bir::InstDMA` at offset `+0xF8`: `{None=0, SWDGE=1, HWDGE=2, Unassigned=3}` — the route. The central predicate `getDGEInstructionType` reads the AP flavour, consults the enabled `DGELevels`, gates `HWDGE` on `ArchLevel > 29`, and returns a primary route plus the set of routes the DMA is *eligible* for. Three passes then act on that decision: `dynamic_dma_scan` stamps the route onto every still-`Unassigned` DMA, `dynamic_dma_setup` reserves the SB scratch buffer the descriptor lowering stages into, and `dynamic_dma_cleanup` re-validates the route and engine binding after allocation and codegen.

For reimplementation, the contract is:

- The **two enums** (`DGELevels` config vs `bir::DGEType` route) and the `+0xF8` field that persists the route.
- **`getDGEInstructionType`** — the route predicate: AP-flavour dispatch (transpose / dynamic-offset / copy-cast), the `count(DGELevels::…)` checks, the `sub_1096280` descriptor-feasibility test, and the `ArchLevel > 29` HWDGE gate.
- The **three passes** and **where each sits** in the pipeline, plus how the route feeds (and is re-checked against) `assign_hwdge_engine`.

| | |
|---|---|
| **Route predicate** | `getDGEInstructionType` `@0x1097330` |
| **Descriptor-fit helper** | `sub_1096280` (SW-vs-HW feasibility) `@0x1096280` |
| **Route field** | `bir::InstDMA + 0xF8` (4-byte `int`), values `{None0, SWDGE1, HWDGE2, Unassigned3}` |
| **Config option** | `--dge-levels` → `cl::list<DGELevels>`, set in `PassOptions.enable_dge_levels` (`std::set<DGELevels>`) |
| **Scan / setup / cleanup** | `DynamicDMAScan::run @0xd03c40` · `DynamicDMASetupImpl::visit @0xd05440` · `DynamicDMACleanUp::run @0xd02680` |
| **HWDGE engine binder** | `AssignHWDGEEngineImpl::assignEngine @0x1180360` (gates on `DGEType==2`) |
| **HWDGE arch gate** | `ArchLevel (Module+0xAC) > 29` (`cmp …,0x1d`) — Cayman/Trn2 (30) and up |
| **Descriptor consumer** | `generateDynamicDMA(InstDMA&, DMAQoSClass) @0x1276b10` (lowering) |

> **GOTCHA — `DGELevels` is not `bir::DGEType`.** They share words (`ScalarDynamicOffset`, `VectorDynamicOffsets` are `DGELevels`; `SWDGE`/`HWDGE` are `DGEType`) but live in different binaries and serve different roles. A `DGELevels` is an *AP-offset flavour the compiler is allowed to handle*; a `bir::DGEType` is the *SW-vs-HW descriptor-generation route* chosen for one DMA. The scalar-offset flavour maps to the `SWDGE` route; the vector-offset / xbar-transpose flavours map to the arch-gated `HWDGE` route. Throughout `getDGEInstructionType` the *set keys and firstOrdinal it returns are `bir::DGEType`*, while the predicates it tests (`isScalarDynamicOffsetAP`, …) are `DGELevels` flavour checks.

---

## The Two Enums

### `DGELevels` — the configuration set

`DGELevels` is an `llvm::cl::list<DGELevels>` registered as the `--dge-levels` option (the `cl::list<DGELevels, bool, cl::parser<DGELevels>>` template instantiation is byte-present: `_ZN4llvm2cl4listI9DGELevels…` symbols, and the option banner string is `"Available DGE Levels:"`). The `cl::ValuesClass` value/description table pairs six literals with help text, in this order (rodata value-table order, `0x1dc3583..0x1dc36ed`):

| Ordinal | Literal | Description |
|---|---|---|
| 0 | `spill_reload` | "Spill/Reload DMA on DGE" |
| 1 | `scalar_dynamic_offset` | "DGE with scalar dynamic offset" |
| 2 | `vector_dynamic_offsets` | "DGE with vector of dynamic offsets" |
| 3 | `dynamic_size` | "DGE with Dynamic Transfer Size with Fixed Upper Bound" |
| 4 | `dst_reduce` | "DGE with an accumulate operation (add/min/max) performed on the destination" |
| 5 | `transpose` | "allow DGE DMAs to do xbar transposes" |

> **CORRECTION (D-Z02) —** an earlier survey of the driver CLI ([`--dge-levels`](../frontend/walrus-driver-cli.md), tagged MED there) reported the literal→description pairing shifted by one (an "IO DMA on DGE" first line). The rodata value-table read directly here is unambiguous: each literal is immediately followed by *its own* description string, giving the table above. The CLI page already flags its own pairing as lower-confidence "as laid out"; this page is authoritative on the pairing. *(CONFIRMED — value-table order; the literal NAMES `spill_reload … transpose` are independently CONFIRMED in the CLI page.)*

The chosen set is resolved into `PassOptions` as `enable_dge_levels`, a `std::set<DGELevels>` — the rodata name for the resolved set is `"ChosenDGELevels"` (`@0x15ffb8`). Every DMA query is a `CurDGELevels->count(DGELevels::Level) > 0` red-black-tree lookup; an assertion that mentions one such call by name is `"options.enable_dge_levels.count(DGELevels::Transpose) > 0"` (`@0x1dca130`). *(CONFIRMED — set name string, member-name assertion strings.)*

> **NOTE — clEnumValN ints are INFERRED.** Only the literal/description *table* is byte-readable; the actual `int` value each `clEnumValN` binds is the C++ constructor argument, not stored as a contiguous LUT. The ordinal column above is the value-table *index*, which the `cl::parser` returns from `OptionInfo+40` (`parse() @0x7eea40`). The member *names* `ScalarDynamicOffset`, `VectorDynamicOffsets`, `Transpose` are CONFIRMED (they appear by `DGELevels::` in assertion strings); the numeric value of each is INFERRED to match table order.

### `bir::DGEType` — the per-DMA route tag

The route is a persisted libBIR enum stored as a 4-byte `int` at `bir::InstDMA + 0xF8`:

| Value | Name | Meaning |
|---|---|---|
| 0 | `None` | Static DMA — no descriptor generation |
| 1 | `SWDGE` | Software descriptor generation (a descriptor program runs on the triggering compute engine) |
| 2 | `HWDGE` | Hardware descriptor generation (a dedicated SP/Activation engine runs the generator) |
| 3 | `Unassigned` | Sentinel — pre-scan; the value `dynamic_dma_scan` looks for |

These four ordinals are CONFIRMED byte-exact two ways: by `libBIR`'s `DGEType2string`/`string2DGEType` (`HWDGE\0 → 2`, `SWDGE\0 → 1`, `None\0 → 0`, `Unassigned\0 → 3`), and by the consolidated ISA enum catalogue, which lists `DGEType {0:None, 1:SWDGE, 2:HWDGE, 3:Unassigned}` (libBIR `@0x400dc0`) as an identity wire enum that *selects the descriptor-gen engine, not an opcode* (see [ISA Enum Ordinals](../isa/isa-enum-ordinals.md) and [DMA Encoding](../isa/dma-encoding.md)). *(CONFIRMED — `DGEType2string` table + two cross-checked sibling pages from the same build.)*

> **QUIRK — the SW-vs-HW route is the engine selector, not an opcode.** A `SWDGE` and an `HWDGE` DMA of the same shape carry the *same* DMA-family opcode; the `DGEType` field is what decides whether a descriptor program is generated in software on the compute engine, or by a dedicated hardware DGE on SP/Activation. The downstream encoder ([DMA Encoding](../isa/dma-encoding.md)) reads this field, not a distinct instruction.

---

## `getDGEInstructionType` — the Route Predicate

### Purpose

`getDGEInstructionType` is the one function that decides, for a single instruction, whether it is a DGE DMA and which route(s) it qualifies for. It is called by the scan (to stamp the route) and again by the cleanup (to re-validate it). Everything downstream — whether the DMA even reaches the HWDGE engine binder — flows from its return value.

### Entry Point

```text
DynamicDMAScanImpl::visitInstruction  @0xd05ba0    ── stamps the route
  └─ getDGEInstructionType            @0x1097330    ── THE route predicate
       ├─ bir::isTransposeDMA / isVectorDynamicOffsetDMA   ── AP-flavour probes (libBIR, PLT)
       ├─ IsHWDGETransposeAPShapeSizeOk  @0x1093990         ── xbar-transpose feasibility
       └─ sub_1096280                  @0x1096280            ── SW-vs-HW descriptor fit (copy/cast)
            └─ bir::getMaxDMADescElements                    ── HW per-descriptor element cap
```

### Algorithm

```c
// getDGEInstructionType  @0x1097330
//   returns { int firstOrdinal /*bir::DGEType*/, std::set<bir::DGEType> eligible }
pair<DGEType,set<DGEType>>
getDGEInstructionType(const Instruction& I,
                      const set<DGELevels>* CurDGELevels,
                      bool considerDescNum   /*b1*/,
                      bool considerMinElems  /*b2*/):

    // (1) EARLY-OUT to {None, {None}} — not a DGE candidate.
    opcode = I[88];                               // I + 0x58
    if opcode > 0x20 || !DMA_OPCODE_MASK[opcode]  // non-DMA opcode
       || CurDGELevels->empty()                   // a3+40 == 0: nothing enabled
       || I.has_no_AP_args():
        return { None(0), {None} };               // writes @0x10973c6 / 0x10981f2

    in0  = I.getArgument<AccessPattern>(0);
    out0 = I.getOutput<AccessPattern>(0);
    if in0.isTensorIndirect() || out0.isTensorIndirect():
        NeuronAssertion("DMA Instruction cannot have TensorIndirect AP",
                        utils.cpp:440, id 1855);

    CurArch = I.getModule()->ArchLevel;           // Module + 0xAC (172)
    bool hwOk = (CurArch > 29);                   // cmp CurArch, 0x1d  → HWDGE gate

    // (2) TRANSPOSE branch — xbar transpose DGE.
    if bir::isTransposeDMA(I):
        bool xposeLevel = CurDGELevels->count(DGELevels::Transpose) > 0;  // key==5/6
        if xposeLevel && !isVectorDynamicOffsetDMA(I):
            if IsHWDGETransposeAPShapeSizeOk(in0, out0, xposeOps, CurArch):  // @0x1093990
                return { HWDGE(2), {None, HWDGE} };   // 0x1097ff9: firstOrdinal=2
            else:
                return { None(0), {None} };           // shape too big → static
        if isVectorDynamicOffsetDMA(I):
            assert IsDynamicTransposeAPShapeSizeOk(in0,out0,xpose,CurArch);  // utils.cpp:450
            // fall through to the dynamic-offset branch

    // (3) DYNAMIC-OFFSET branch — runtime partition/free offsets.
    if in0.isDynamicAP() || out0.isDynamicAP():     // vfn slot +64 == isDynamicAP
        // HARD INVARIANT: scalar or vector offset DGE must be enabled.
        if !( CurDGELevels->count(DGELevels::ScalarDynamicOffset) > 0 ||
              CurDGELevels->count(DGELevels::VectorDynamicOffsets) > 0 ):
            NeuronAssertion("DMA Instruction with dynamic AP must have "
                            "Scalar/Vector Dynamic offset enabled",
                            utils.cpp:464, id 1856);
        // dispatch on the AP offset flavour
        if   in0.isScalarDynamicOffsetAP() || out0.isScalarDynamicOffsetAP():  // vfn +88
             ;  // scalar-offset flavour
        elif in0.isVectorDynamicOffsetAP() || out0.isVectorDynamicOffsetAP():  // vfn +72
             ;  // vector-offset flavour
        else:
             llvm_unreachable("DGE should be of type Scalar or Vector. "
                              "This code should never be reached!");           // utils.cpp
        set s = { SWDGE };                          // 0x1097947: firstOrdinal=1
        if hwOk: s.insert(HWDGE);                    // arch>29 → {SWDGE,HWDGE}
        return { SWDGE(1), s };

    // (4) COPY / CAST family — feasibility-tested, not flavour-tested.
    if isIOCopyDMA(I) || isSRCopyDMA(I) || isIOCastDMA(I) || isSRCastDMA(I):
        // CCE-aware: scan descendants for opcode 33/48 (CollectiveCompute)
        // to decide whether the copy can legally stay a DGE DMA.   [STRONG]
        bool fits_sw = sub_1096280(in0, out0, /*cand*/SWDGE, considerDescNum, considerMinElems);
        bool fits_hw = hwOk && sub_1096280(in0, out0, /*cand*/HWDGE, considerDescNum, considerMinElems);
        set s = {};
        if fits_sw: s.insert(SWDGE);
        if fits_hw: s.insert(HWDGE);
        if s.empty(): return { None(0), {None} };
        return { /*first present*/ first(s), s };

    // (5) DEFAULT — nothing matched.
    return { None(0), {None} };
```

The byte-exact `firstOrdinal` writes pin the four return arms: `None(0)` at `0x10973c6`/`0x10981f2`, `SWDGE(1)` at `0x1097947`, `HWDGE(2)` (transpose arm) at `0x1097ff9`; the eligible-set keys are written as `{0}`, `{1}` (+`{2}` when `arch>29`), `{0,2}` (`0x200000000`), `{0,1}` (`0x100000000`). *(CONFIRMED — decompiled body, NX-162 byte-exact writes.)*

> **QUIRK — `firstOrdinal` is itself a `bir::DGEType`.** The pair's `.first` is not an index into the set; it is the *primary route* the scan stamps (`None`/`SWDGE`/`HWDGE`). The `.second` set is the *eligibility envelope* — the routes the AP still satisfies — which the cleanup pass later checks the stamped route against. A dynamic-offset DMA on Cayman (ArchLevel 30) returns `firstOrdinal=SWDGE`, set `{SWDGE,HWDGE}`: SW is the default route, HW is an offered alternative that a later pass may promote to.

### The HWDGE arch gate

`HWDGE` is added to the eligible set only when `CurArch > 29` (`cmp CurArch, 0x1d`). The `ArchLevel` is read once from `Module + 0xAC` (172). `SWDGE` is offered on any qualifying arch.

> **NOTE — what "gen3+" means numerically.** The backing analysis labels the `>29` set "gen3(30)/core_v4(40)/core_v5(50)". Against the CONFIRMED `ArchLevel` mapping in [Target Abstraction](target-abstraction.md) — `{10→Inf1/Tonga, 20→Trn1/Sunda, 30→Trn2/Cayman, 40→Trn3/CoreV4, 50→CoreV5}` — the gate `>29` (i.e. `≥30`) admits **Cayman/Trn2 (30) and up**, and excludes Inferentia (10) and Sunda/Trn1 (20). On those two, the `HWDGE` candidate is never added, so all DGE on them is `SWDGE`. The same `cmp …,0x1d` (29) generation boundary is observed independently in the GPSIMD latency selector ([GPSIMD Engine](../arch/gpsimd-engine.md)), corroborating 29 as the gen boundary constant in this build. *(CONFIRMED gate constant; codename mapping cross-strand.)*

### `sub_1096280` — descriptor-count feasibility (SW-vs-HW fit)

For the copy/cast family, the route is decided not by an AP flavour bit but by whether the DMA's element count *fits* as a SW or HW descriptor program. `sub_1096280` (`@0x1096280`) is that test:

```c
// sub_1096280  @0x1096280  — descriptor-fit feasibility
bool descFits(const AccessPattern& in, const AccessPattern& out,
              unsigned candDGEType /*1=SWDGE, 2=HWDGE*/,
              bool checkDescNum /*b1/a4*/, bool checkMinElems /*b2/a5*/):

    assert(in.actualPattern.size() == out.actualPattern.size());   // utils.cpp:150
    for i: assert(in.actualPattern[i].getNum()
                  == out.actualPattern[i].getNum());               // utils.cpp:153

    MaxDMADescElements = bir::getMaxDMADescElements(I);            // HW per-descriptor cap

    // require partition-contiguous APs; else canonicalize and compare
    if !partitionContiguous(in, out):
        canonIn = canonicalize(in); canonOut = canonicalize(out);
        if canonIn.dims != canonOut.dims:
            reportError("DMA Copy in/out canonical dimensions must match");
            reportError("strided Free dim AP should be at least 2D");

    // which dim's split is checked depends on the candidate route
    elemsPerPartition = (candDGEType==SWDGE) ? splitAlongDim0(in)   // SW: dim-0 split
                                             : perElement(in);      // HW: per-element/dim

    numDesc = ceil(elems / MaxDMADescElements);
    if elems % MaxDMADescElements != 0: return false;              // exact division required
    if !neuronxcc::backend::isDescNumOk(numDesc, elemsPerPartition, candDGEType):
        return false;

    if checkMinElems:                                              // a5
        if sub_1092B10(elemsPerPartition) < qword_3E01C58:        // per-arch min-element floor
            return false;        // tiny DMAs are NOT promoted to DGE
    return true;
```

The two trailing bools threaded from `getDGEInstructionType` are `b1 = considerDescNum` (use the `getMaxDMADescElements` per-descriptor cap) and `b2 = considerMinElems` (also enforce the `qword_3E01C58` per-arch minimum-element floor, so a copy too small to be worth a descriptor program stays static). The exact-division requirement (`elems % MaxDMADescElements == 0`) means a copy whose element count is not a clean multiple of the per-descriptor cap simply does not fit and falls back to `None`. *(STRONG — decompiled; `isDescNumOk`/`getMaxDMADescElements` resolved through libBIR PLT, semantics from call sites + the two `utils.cpp` size-match assertions.)*

### `_validateOnlyOneOf…DynamicOffset…`

A dynamic AP must carry *exactly one* of the scalar/vector offset flags. `_validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided` (`@0xf140a0`) enforces it:

```c
// _validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided  @0xf140a0
void validate(shared_ptr<klr::BirAccessPattern> ap):
    if ap->vectorOffsetFlag(+88) == ap->scalarOffsetFlag(+64):   // both set OR neither set
        reportError();   // illegal: a dynamic AP must have exactly one offset flavour
```

The runtime strings that mirror this constraint — `"Logic Fault: dynamic access should have vector offset."`, `"tensor_copy only supports scalar_offset for dynamic access, not vector_offset."`, `"When using vector offset, the only supported indirectDim is 0"` — confirm that scalar and vector offsets are mutually exclusive per AP, and that `tensor_copy` lowering only accepts the scalar flavour for dynamic access. *(CONFIRMED — decompiled body + runtime assertion strings.)*

---

## `dynamic_dma_scan` — Stamp the Route

### Purpose

`dynamic_dma_scan` is a pure annotator: it visits every DMA-family instruction and, if the instruction's route field is still `Unassigned(3)`, computes `getDGEInstructionType` and writes the primary route into `+0xF8`. It creates no instructions and never overwrites an already-routed DMA.

### Algorithm

```c
// DynamicDMAScan::run(Module&)  @0xd03c40
run(Module& M):
    pushLogAttr("module_name", M.name());
    Function* main = M.getFunctionByName("main");
    for Function* f in [main, ...rest]:               // main first, then the others
        CFG cfg; cfg.build(*f);
        loopBodies = cfg.getLoopBodies();             // hash-set of BBs inside loops
        for BasicBlock& BB in *f:
            bool isInLoop = loopBodies.contains(&BB);  // per-BB flag
            for Instruction& I in BB:
                DynamicDMAScanImpl{logger, isInLoop}.visit(I);
    return passResultString;

// DynamicDMAScanImpl::visitInstruction(Instruction& I)  @0xd05ba0
visit(Instruction& I):
    opcode = I[88];                                    // I + 0x58
    if opcode == 32 /*InstDMACopy*/:
        // probe args for a transpose-AP (kind <= 5), then forbid dst-reduce here
        assert !bir::isDstReduceDGE(&I);               // dynamic_dma.cpp:124
    // DMA-family gate (same table the cleanup uses):
    if !( opcode == 18 ||
          (opcode-19 <= 0x30 && DMAtable[opcode-19]) ):  // off_1DE6420, 0x31 bytes
        return;                                          // not a DMA-family inst
    // THE STAMP — only Unassigned DMAs are routed:
    if I.DGEType /*I+0xF8*/ == Unassigned(3):
        auto r = getDGEInstructionType(I, CurDGELevels, b1, b2);
        I.DGEType = r.first;                            // None/SWDGE/HWDGE; set is discarded
    // else: already routed → leave it untouched (idempotent).
```

The DMA-family gate reads a 0x31-byte table `off_1DE6420` indexed by `opcode-19`, plus the explicit `opcode == 18` case; the table sets the DMA / DMA-like opcodes `{18,19,22,32,41,42,43,44,45,46,67}`. The `!isDstReduceDGE` assertion (`dynamic_dma.cpp:124`) is the scan's refusal to route a destination-reduce DMA through DGE here — `dst_reduce` is a separate `DGELevel` handled elsewhere in the lowering. *(CONFIRMED — decompiled bodies; the `!isDstReduceDGE(&I)` and `dynamic_dma.cpp:124` strings pin the assert.)*

> **QUIRK — the `Unassigned` guard makes the scan idempotent and re-runnable.** Because the stamp only fires on `+0xF8 == 3`, the pass can run twice (it does — once pre-allocation, once post; see [Pipeline Placement](#pipeline-placement)) and the second pass routes only DMAs that were *introduced after* the first (spill/reload clones, SSA re-clones), leaving every already-routed DMA alone. A reimplementation that unconditionally re-stamps would clobber routes the allocator depends on.

---

## `dynamic_dma_setup` — Reserve the Descriptor Scratch

### Purpose

The descriptor-generation lowering needs a place in state-buffer (SB) memory to *stage* the descriptors it synthesizes at run time before issuing the DMA. `dynamic_dma_setup` inserts exactly one named SB-space scratch `MemoryLocation`, `"DynamicDMAScratchLoc"`, and marks it allocated. It is a pre-linker pass (single function per module).

### Algorithm

```c
// DynamicDMASetupImpl::visit(Module&)  @0xd05440  (driver: DynamicDMASetup::run @0xd02370)
visit(Module& M):
    // (1) pre-linker invariant: exactly one function.
    assert M.num_functions() == 1;        // "this pass should run in pre-linker phase…"
                                          //  dynamic_dma.cpp:39 / 44
    Function* f = M.getFunctionByName("main");

    // (2) create the SB-space scratch location.
    MemoryLocationSet desc = { ptr, 0x400000000 };   // high dword 4 == SB memory-SPACE tag
    MemoryLocation* scratch =
        f->addMemoryLocationWithMemoryLocationSet("DynamicDMAScratchLoc", desc);
    this->scratchLoc /*this+16*/ = scratch;

    // (3) reserve it.
    scratch->setAllocated(true);
    scratch->byte(+579) = 1;                          // mark live/reserved
    scratch->memLocationSet(+352)->buildTensorId2MemLoc();   // index by tensor id
```

The high dword `4` in the `MemoryLocationSet` is the memory-space tag selecting SB (state-buffer) space for the scratch. The location it creates, `"DynamicDMAScratchLoc"`, is the buffer `generateDynamicDMA` (`@0x1276b10`, the descriptor lowering consumer in [DMA Encoding](../isa/dma-encoding.md)) stages dynamically-generated descriptors into. The driver-facing size of this region is the `--dynamic-dma-scratch-size-per-partition` option ("The SB scratch size per partition for Dynamic DMA", CONFIRMED in [walrus-driver-cli](../frontend/walrus-driver-cli.md)). *(CONFIRMED — decompiled body; `"DynamicDMAScratchLoc"` and the pre-linker assertion strings present; consumer cross-referenced.)*

---

## `dynamic_dma_cleanup` — Validate Route + Engine

### Purpose

Despite its name, `dynamic_dma_cleanup` is not a memory teardown — it is the post-lowering **consistency verifier** for the DGE routing and the HWDGE engine binding, plus the feature-flag stripper. It runs after allocation/codegen, flat across every instruction, and performs three checks per DMA. The `DynamicDMAScratchLoc` reservation from setup is freed by the normal allocator lifecycle, not here.

### Algorithm

```c
// DynamicDMACleanUp::run(Module&)  @0xd02680
run(Module& M):
    bool dgeEnabled = (M.config(+12)->byte(+472) != 0);   // module DGE feature flag
    for Instruction& I in flat(M):                        // getFirstWithin/getNextInstFlat/…
        if !isDMAFamily(I): continue;                     // same opcode gate as the scan

        // (A) FEATURE GUARD — strip routes if DGE is off at Walrus.
        if (I.DGEType == SWDGE || I.DGEType == HWDGE) && !dgeEnabled:
            reportError("Input uses DGE but DGE is not enabled at Walrus");  // ~dynamic_dma.cpp:143
        if !dgeEnabled:
            I.DGEType = None(0);                           // strip the mark entirely

        // (B) ROUTE-VALIDITY — the stamped route must still be eligible.
        if I.hasDynamicAP() /*AP-kind==1*/ && I.DGEType != Unassigned(3):
            set validDGETypes = getDGEInstructionType(I, ...).second;     // recompute envelope
            assert validDGETypes.find(I.DGEType) != validDGETypes.end();  // dynamic_dma.cpp:145
            // "validDGETypes.find(target_dge_type) != validDGETypes.end()"

        // (C) ENGINE-BINDING — HWDGE DMAs must be on SP or Activation.
        CurEngine = I[0x90] & 0xFFFFFFFB;                 // engine field, bit2 stripped
        if bir::isHWDGEDMAWithEngineSet(I) && (CurEngine & ~4) != 2:
            assert CurEngine == EngineType::SP || CurEngine == EngineType::Activation;
            // dynamic_dma.cpp:172, error id 1698
```

Check (B) is the central invariant of the whole subsystem: the route stamped by the scan must *still* be among the AP's eligible routes after every intervening lowering and allocation transform — if a pass mutated the AP such that the stamped `DGEType` no longer fits, the build fails loudly rather than emitting a wrong descriptor. Check (C) hands off to / agrees with `assign_hwdge_engine`: `isHWDGEDMAWithEngineSet(I)` is `(I.DGEType == HWDGE(2)) && ((I.engine@+0x90 & ~4) != 0)`, and a HWDGE DMA with any engine bound must be on **SP (6)** or **Activation (2)** — both satisfy `(x & ~4) == 2`. *(CONFIRMED — decompiled body; the route-validity, feature-guard, and engine assertion strings + `dynamic_dma.cpp:143/145/172` and error id 1698 pin all three checks.)*

> **GOTCHA — `dgeEnabled` strips routes silently when DGE is disabled.** If the module-level DGE feature flag (`M.config(+12)->byte(+472)`) is clear, every `SWDGE`/`HWDGE` mark is first *flagged as an error if it exists* and then *unconditionally reset to `None`*. A reimplementation that honours stamped routes without re-reading this flag at cleanup time would emit DGE descriptors the runtime is not configured to accept.

---

## How the Route Feeds `assign_hwdge_engine`

The `+0xF8` route is the **sole gate** on whether a DMA reaches the HWDGE engine binder. `assign_hwdge_engine` (pass order 123, its own page) binds an SP/Activation engine to run the hardware descriptor generator — and its per-instruction worker is a no-op for anything that is not `HWDGE`:

```c
// AssignHWDGEEngineImpl::assignEngine(InstDMA& I)  @0x1180360
assignEngine(InstDMA& I):
    if I.DGEType /*I+0xF8*/ != HWDGE(2): return;   // FIRST LINE — no-op for None/SWDGE/Unassigned
    // ... pick an engine from ValidEngines, write to I+0x90 ...
```

`ValidEngines` is built once from the first module's `ArchLevel` (`+0xAC`): for `ArchLevel ≤ 49` (Cayman 30 / CoreV4 40) it is a fixed 2-entry set `[ SP(6), Activation(2) ]` and `assignEngine` picks SP or Activation by affinity-match against the instruction's `(engine, sub)` key at `I+0x90`/`I+0x94`, falling back round-robin; for `ArchLevel ≥ 50` (CoreV5) it is a larger target-supplied set the binder round-robins via `counter++ % size`. The hard invariant `ValidEngines.size() >= 2` (`assign_hwdge_engine.cpp:22`, FATAL "Number of valid engines for HWDGE must be greater than or equal to 2") guarantees the SP/Activation pair is always available. *(CONFIRMED via the assign_hwdge_engine page.)*

The SW/HW split this produces:

| Route | Descriptor generation | Engine | Availability |
|---|---|---|---|
| `SWDGE(1)` | Software descriptor program | Stays on the triggering compute engine (bound by `assign_trigger_engine`, order 110); **no** HWDGE engine | Any qualifying arch — the `scalar_dynamic_offset` path |
| `HWDGE(2)` | Hardware descriptor generation | Dedicated **SP(6)** or **Activation(2)** engine (bound here, order 123) | Only `ArchLevel > 29` — the `getDGEInstructionType` `cmp …,0x1d` gate |

So the scalar-vs-vector / SW-vs-HW decision made in `getDGEInstructionType` is *exactly* what decides whether a DMA reaches the HWDGE engine binder at all. On Inferentia (10) / Sunda-Trn1 (20) the HWDGE candidate is never added, every DGE DMA is `SWDGE`, and `assignEngine` no-ops on all of them.

---

## Pass Registration and Pipeline Placement

Each pass is registered by a `std::function` factory `(PassOptions → unique_ptr<BackendPass>)`. The scan factory stores `PassOptions` at `pass+96`, the source of `CurDGELevels = enable_dge_levels`:

| Registrar | Invoke addrs | Pass name |
|---|---|---|
| `register_generator_dynamic_dma_scan__` | `0xd06770` / `0xd04d40` (vtable `off_3D8F3A0`, `tc_new(104)`) | `dynamic_dma_scan` |
| `register_generator_dynamic_dma_setup__` | `0xd06990` / `0xd04d20` | `dynamic_dma_setup` |
| `register_generator_dynamic_dma_cleanup__` | `0xd06550` / `0xd04d60` | `dynamic_dma_cleanup` |
| `register_generator_assign_hwdge_engine__` | `0x1182710` / `0x1180100` | `assign_hwdge_engine` |

The relevant relative placement in the backend pipeline (the second numbers below are the D-H17 pipeline-trace sub-sequence, distinct from the alphabetical registry IDs `dynamic_dma_cleanup=49 / dynamic_dma_scan=50 / dynamic_dma_setup=51 / assign_hwdge_engine=12 / lower_dynamic_dma=92`):

```text
 ... 35 bir_splitter
  36 dynamic_dma_setup      ── inserts DynamicDMAScratchLoc   (PRE-allocators)
  37 runtime_memory_reservation
 ...  PSUM + SB + DRAM coloring allocators, address rotation  ...
  70 memory_analysis_after_address_rotation_dram
  71 dynamic_dma_cleanup    ── strip-if-disabled + route/engine verify
  72 birverifier
  73 dynamic_dma_scan       ── (re)stamp routes post-alloc (gated byte_3DEC330)
 ...
  92 lower_dynamic_dma      ── descriptor LOWERING: materializes the SWDGE program /
                               HWDGE generator using the §setup scratch buffer
 ... 110 assign_trigger_engine    123 assign_hwdge_engine    124 alloc_queues
```

> **NOTE — setup before scan, and scan twice.** `dynamic_dma_setup` (36) runs *before* the first authoritative scan because the scratch reservation must exist before the allocators color SB. The scan appears at 73 *after* allocation so that late-introduced/cloned DMAs (spill/reload, SSA re-clones) get routed; the `Unassigned` guard makes this second pass touch only the new DMAs. The cleanup at 71 runs just ahead of that post-alloc scan to strip stale marks; the authoritative route+engine verify is the cleanup that follows codegen. The exact global ordering of the cleanup/scan pair versus `assign_hwdge_engine` is the D-H17 sub-sequence numbering, distinct from the registry's global IDs. *(STRONG — pipeline trace.)*

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `getDGEInstructionType` | `0x1097330` | Route predicate: `{firstOrdinal, eligible set}` | CONFIRMED |
| `sub_1096280` | `0x1096280` | SW-vs-HW descriptor-fit feasibility | STRONG |
| `_validateOnlyOneOf…DynamicOffset…` | `0xf140a0` | Exactly-one-of scalar/vector offset | CONFIRMED |
| `DynamicDMAScan::run` | `0xd03c40` | Per-function/BB scan driver | CONFIRMED |
| `DynamicDMAScanImpl::visitInstruction` | `0xd05ba0` | The `Unassigned`→route stamp | CONFIRMED |
| `DynamicDMASetup::run` | `0xd02370` | Setup driver | CONFIRMED |
| `DynamicDMASetupImpl::visit(Module)` | `0xd05440` | Insert `DynamicDMAScratchLoc` | CONFIRMED |
| `DynamicDMACleanUp::run` | `0xd02680` | Feature-strip + route/engine verify | CONFIRMED |
| `AssignHWDGEEngineImpl::assignEngine` | `0x1180360` | HWDGE engine binder (gates `DGEType==2`) | CONFIRMED |
| `IsHWDGETransposeAPShapeSizeOk` | `0x1093990` | xbar-transpose feasibility (transpose arm) | STRONG |
| `isDescNumOk` / `getDGEMaxDescNumBasedOnShape` | `0x1093970` / `0x1093950` | Descriptor-count helpers | STRONG |
| `generateDynamicDMA(InstDMA&, DMAQoSClass)` | `0x1276b10` | Descriptor lowering consumer | CONFIRMED |
| `isScalar/VectorDynamicOffsetDMA`, `isDstReduceDGE`, `DGEType2string`, `isHWDGEDMAWithEngineSet` | PLT/GOT (libBIR) | AP-flavour probes & route helpers | INFERRED (call sites + assertion strings) |

> **NOTE — corpus provenance.** `libwalrus.so` and `libBIR.so` are present in the corpus —
> as per-symbol decompiled/disasm sidecars and as full IDA databases under `ida/` — so the fine
> addresses on this page (e.g. `getDGEInstructionType @0x1097330`, `assignEngine @0x1180360`) are
> the `libwalrus.so`-proper VA frame recovered from the `ida/…libwalrus.so/` database; the cited
> bodies are disassembled, not merely declared, and are **CONFIRMED**. The wheel snapshot
> additionally ships the same code statically linked into `walrus_bugpoint_driver`, whose IDA
> sidecar independently confirms the `cl::list<DGELevels>` parser instantiation and the
> `Unassigned` enum string. The `bir::DGEType {None0,SWDGE1,HWDGE2,Unassigned3}` ordinals and the
> `0x62d660`/`0x1c72000` libwalrus base are independently cross-checked against the
> already-published [ISA Enum Ordinals](../isa/isa-enum-ordinals.md) and [DMA Encoding](../isa/dma-encoding.md)
> pages. The same provenance stance holds on [Symbolic-AP Register-ALU](symbolic-ap-register-alu.md),
> [Backend Dependence-Distance](backend-dependence-distance.md), [the Dynamic For-Loop](dynamic-for-loop.md),
> and [Dynamic-Shape Synthesis](dynamic-shape-synthesis.md).

## Related Components

| Name | Relationship |
|---|---|
| `assign_hwdge_engine` (order 123) | Binds SP/Activation to HWDGE DMAs; no-ops unless `DGEType==2` |
| `assign_trigger_engine` (order 110) | Binds the compute engine that runs a SWDGE descriptor program |
| `lower_dynamic_dma` (order 92) | Materializes the SWDGE program / HWDGE generator into the scratch buffer |
| `birverifier` (order 72) | General BIR verify, runs between cleanup and the post-alloc scan |

## Cross-References

- [TensorCopy Dynamic Generators](tensorcopy-dynamic-generators.md) — the dynamic `tensor_copy` lowering that consumes the SWDGE route
- [Symbolic-AP → Register-ALU Materialization](symbolic-ap-register-alu.md) — how a runtime offset becomes a `bir::DynamicAPINFO` and is lowered to registers
- [Dynamic-Shape Synthesis](dynamic-shape-synthesis.md) — the broader dynamic-shape backend sub-thread (end-to-end)
- [DMA Encoding](../isa/dma-encoding.md) — `generateDynamicDMA`, the ADDR8 address modes, and how the `DGEType` field is read by the encoder
- [ISA Enum Ordinals](../isa/isa-enum-ordinals.md) — `bir::DGEType {None,SWDGE,HWDGE,Unassigned}` as an identity wire enum
- [lower_dma (walrus)](../walrus/) — the descriptor-lowering back end (Part 8)
- [Target Abstraction](target-abstraction.md) — the `ArchLevel {10,20,30,40,50}` mapping the `>29` HWDGE gate keys on
- [walrus_driver CLI](../frontend/walrus-driver-cli.md) — the `--dge-levels` and `--dynamic-dma-scratch-size-per-partition` options
