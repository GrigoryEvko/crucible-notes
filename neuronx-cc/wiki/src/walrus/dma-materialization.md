# DMA Materialization

> *All addresses on this page apply to `libwalrus.so` from neuronx_cc 2.24.5133.0+58f8de22 (the cp310 wheel). The dynamic symbol table is retained (`nm -DC` names every method), `.text`/`.rodata` are VA==fileoffset; `.data` carries a +0x400000 delta. `0x5e9020`–`0x62d650` is the `.plt`; a `…@plt` call resolves to the real high-VA body of the same symbol. Other versions will differ.*

## Abstract

DMA materialization is the last lowering step that turns a backend `InstDMA` op — by this point already bound to a trigger engine, a sub-id, and a queue — into the concrete on-device artifact the engine actually executes: an **`InstDMABlock`** (a block of `InstDMADescriptor`s) plus an **`InstDMATrigger`** that fires it. Two passes do this, in this order: `lower_dynamic_dma` (L14) resolves every runtime-address / data-dependent-shape DMA into static descriptors, then `lower_dma` (L17) materializes every remaining static DMA into descriptor blocks and trigger instructions. The runtime model this targets is software DGE (Descriptor Generation Engine): *a static descriptor list that a trigger fires*, exactly the form the simulator iterates as `InstDMATrigger → InstDMABlock → [InstDMADescriptor*]`.

The familiar reference frame is a scatter/gather DMA controller with a descriptor ring. On the Trainium TPB, a *hardware* DGE (HWDGE) can synthesize descriptors on-device from a compact offset-register access pattern; a *software* DGE (SWDGE) needs the compiler to emit the explicit descriptor list. The ring has a hard depth: **SWDGE caps a block at 1020 descriptors, HWDGE at 65520**. The whole point of these two passes is to take an op whose descriptor count or address is not statically known and reduce it — by peeling, unrolling, splitting, and finally checking — to descriptor blocks that fit that ring and carry only concrete `(stride, num)` access-pattern pairs.

This page documents three things a reimplementer must reproduce: (1) the `InstDMABlock`/`InstDMADescriptor` build performed by `lower_dma` (`LowerDMAImpl`), including the per-queue block-reuse state and the descriptor-type dispatch; (2) the **Peel → Unroll → Split → Check** four-stage sub-pipeline of `lower_dynamic_dma` (`LowerDynamicDMAImpl` and its four subclasses), which is the headline algorithm; and (3) the **SWDGE=1020 / HWDGE=65520** ring-depth limits and the `≤16`-engine factor distribution that the split stage enforces. These two limits are the page's anchoring constants and are pinned to live disassembly.

For reimplementation, the contract is:

- The **`InstDMABlock` build**: a static DMA becomes `InstDMABlock { InstDMADescriptor{Copy|CCE|Transpose|Replicate} × N }` on a per-queue `BasicBlock`, paired with an `InstDMATrigger` on the assigned engine; the access pattern is *unrolled* into concrete `(stride, num)` descriptor pairs.
- The **Peel → Unroll → Split → Check pipeline**: four `IRVisitor<LowerDynamicDMAImpl>` passes over the module, each a subclass overriding `legalizeAP`, that progressively turn a dynamic-AP DMA into in-budget static descriptors.
- The **DGE ring-depth budget**: `SWDGE=1020`, `HWDGE=65520`, distributed across `MAX_PARALLEL_DMA_ENGINE_NUM=16` engines by `calcFreeSplits`; and the rule that **dynamic addresses and transpose force SWDGE** because HWDGE cannot carry an arbitrary runtime address list.

| | |
|---|---|
| **Driver (static)** | `LowerDMAImpl` (a `ControlFlowIRVisitor`); entry `LowerDMA::run` @ `0x119adc0` |
| **Driver (dynamic)** | `LowerDynamicDMAImpl` + 4 subclasses; entry `LowerDynamicDMA::run` @ `0xcd9620` |
| **Pass names (rodata)** | `"lower_dma"`, `"lower_dynamic_dma"` (registered generators) |
| **Pipeline order** | L14 `lower_dynamic_dma` runs **before** L17 `lower_dma` (low-level sub-pipeline) |
| **SWDGE ring depth** | **1020** (`0x3fc`), pinned @ `0x1093813` |
| **HWDGE ring depth** | **65520** (`0xfff0`), pinned @ `0x1093818` |
| **Parallel engines** | `MAX_PARALLEL_DMA_ENGINE_NUM` = **16** (factor `≤16` @ `0x1092b56`) |
| **IR produced** | per-engine `[InstDMATrigger]` + `[InstDMABlock → InstDMADescriptor*]` stream |

The engine binding these passes consume is set upstream (see [DMA Legalization](dma-legalization.md) and the engine-assign passes); DGE level (SWDGE/HWDGE/None) is selected in the [DGE-Level Dynamic DMA](../penguin/dge-level-dynamic-dma.md) pass family in the penguin front; the runtime-address expressions come from [Dynamic-Shape Synthesis](../penguin/dynamic-shape-synthesis.md); the descriptor wire format these passes fill is documented in [DMA Encoding](../isa/dma-encoding.md). The engine-binding sibling pass (planned `walrus/` page) is the consumer of the trigger stream.

---

## Where Both Passes Sit — The Invariant `lower_dma` Relies On

### Purpose

`lower_dma` does **not** decide the engine. By the time L14/L17 run, the earlier backend ([DMA Legalization](dma-legalization.md) for strided/CCE DMAs, the DMA-optimization and engine-assign passes, and queue allocation) has already:

- bound each `InstDMA` to a **trigger engine** — engine-id at `InstDMA+0x90`, sub-id at `InstDMA+0x94`;
- for HWDGE candidates, refined the engine and possibly forced non-HWDGE IO DMAs onto the SP engine via the IO-queue limiter;
- set the **DGEType** at `InstDMA+0xF8` ∈ `{None=0, SWDGE=1, HWDGE=2, Unassigned=3}` (the constructor leaves it at `Unassigned=3`);
- allocated one `DMAQueue` per `(engine, QoS)` slot.

So both passes **materialize an already-bound** engine+queue into descriptor blocks and triggers placed on that engine — not allocate one. The default DMA-trigger engine in the roster is the Pool engine (`EngineType=1`); the full `EngineType` roster is `{0 Unassigned, 1 Pool, 2 Activation, 3 PE, 4 DMA, 5 DVE, 6 SP, 7 ALL}`.

### Considerations

The descriptor model is fixed by the simulator's execution shape: SWDGE is *a static descriptor list*, and the simulator runs it as `InstDMATrigger → InstDMABlock → [InstDMADescriptor*]`. Everything `lower_dma` emits exists to satisfy that shape — a block of concrete descriptors, owned by a queue's basic block, fired by a trigger on the engine.

> **NOTE —** `getDgeType()` reads `InstDMA+0xF8`; the engine id at `+0x90` and sub-id at `+0x94` are set by the engine-assign pass *before* lowering. A reimplementation that defers engine assignment to lowering will have no engine to place the trigger on.

---

## `lower_dma` (L17) — Static DMA → Descriptor Block + Trigger

### Purpose

`lower_dma` walks the module and replaces each static `InstDMA{Copy, Load, Save, TensorCopy, CCE, Transpose, Replicate}` with an `InstDMABlock` of concrete descriptors on its queue's basic block, plus an `InstDMATrigger` on the assigned engine. It is driven by `LowerDMAImpl`, a `ControlFlowIRVisitor` subclass whose visit follows the standard backend walk: `enterModule → enterFunction → {enterInstLoop / visitInstruction per inst} → leaveInstLoop → leaveModule`.

### Entry Point

```text
LowerDMA::run(bir::Module&)            0x119adc0   ── pass entry
  └─ LowerDMAImpl::LowerDMAImpl(M, Logger&, PassOptions&)   ── ctor (0x119aee8)
  └─ IRVisitor<LowerDMAImpl>::visit(M)                      ── 0x118f280
       ├─ LowerDMAImpl::enterModule(M)        0x1195650     ── state init (4 DenseMaps)
       ├─ LowerDMAImpl::visitInstruction(I)   0x1199080     ── per-DMA descriptor emit
       └─ LowerDMAImpl::leaveModule(M)        0x1194e70     ── split + trigger placement
```

### Algorithm

```c
function LowerDMA_run(M):                       // 0x119adc0
    push_log_channel(M.getName())               // 0x119ade5 boost-log per-pass tag
    LowerDMAImpl impl(M, logger, opts);         // 0x119aee8 ctor
    impl.visit(M);                              // 0x118f280 ControlFlowIRVisitor dispatch
    return Success;
```

`enterModule` (`0x1195650`) initializes four `DenseMap`s that index the lowering and carry the "which queue already has an open descriptor block" state, so consecutive DMAs to the same queue reuse one block (`reuseBlock`):

```c
function LowerDMAImpl_enterModule(M):           // 0x1195650
    openBlockOf  : DenseMap<DMAQueue*,  InstDMABlock*>   = {}   // 0x11959ce
    descBlockBB  : DenseMap<DMAQueue*,  BasicBlock*>     = {}   // 0x1195b38 (one BB / queue)
    sectionStart : DenseMap<BasicBlock*, bool>           = {}   // 0x1195be9
    blockId      : DenseMap<BasicBlock*, unsigned>       = {}   // 0x1195c99 (per-BB counter)
    emittedBlocks: DenseMap<InstDMA*, vector<InstDMABlock*>> = {} // 0x1195d31 (DMA→blocks)
```

`visitInstruction` (`0x1199080`) is the per-DMA emitter. It reads the opcode at `Instruction+0x58` and routes the DMA family (opcodes `(op-0x13) ∈ [0,0x30]`, i.e. 19..73) into the **descriptor-type dispatch** at `0x1199740`:

```c
function LowerDMAImpl_visitInstruction(I):              // 0x1199080
    op = *(I + 0x58)                                    // opcode
    if op == 18 (Load): handle_load_arm();              // separate arm
    if not in_dma_range(op): return                     // 19..73 → DMA arm @0x1199420
    for each PhysicalAccessPattern ap in I.arguments<PAP>(): // 0x612a20
        reAssignMemLoc(ap, memlocSet);                  // §rebind, 0x1197210
    // --- descriptor-type dispatch (0x1199740) ---
    if !isCopyOrCastDMA(I) and isReadVarAddr(I):        // isCopyOrCast 0x5ecac0
        return createDescForReadVarAddr(I);             // 0x1198860 (dynamic-offset static DMA)
    elif isTransposeDMA(I):    createDescs<Transpose>(I);   // isTranspose 0x604120 → 0x11a4180
    elif isCCEDMA(I):          createDescs<CCE>(I);         // isCCE 0x607110 → 0x11a4d40
    elif isReplicateDMA(I):    emit InstDMADescriptorReplicate(I); // isReplicate 0x5fb770
    else /* plain copy */:     createDescs<Copy>(I);        // → 0x11a3790
    if isRemoteUpdateInstruction(I):                    // 0x61ced0
        block.setRemoteNotification(ids)                // 0x6192e0 remote-DMA notify list
    emittedBlocks[I].push_back(block);                  // 0x600bc0
    if isDynamicOffsetDMA(I): gate_dyn_path();          // 0x617be0 tail re-test @0x1199f50
```

Guard checks woven into the dispatch compare the descriptor sub-kind (`+0x50`/`+0x58`) against `41`/`69`/`70`/`71` and consult `Instruction::evalMask` (`0x619060`, masked DMAs) and `canInstReadFromUninitMem` (`0x5fc320`).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LowerDMA::run` | `0x119adc0` | Pass entry; ctor + visit | CERTAIN |
| `LowerDMAImpl::enterModule` | `0x1195650` | Init 4 indexing DenseMaps | CERTAIN |
| `LowerDMAImpl::visitInstruction` | `0x1199080` | Per-DMA descriptor emitter + dispatch | CERTAIN |
| `LowerDMAImpl::createDescArgumentsOutputs` | `0x11974a0` | AP → descriptor fields | CERTAIN |
| `LowerDMAImpl::reAssignMemLoc` | `0x1197210` | Physical memloc rebind | CERTAIN |
| `LowerDMAImpl::createDescForReadVarAddr` | `0x1198860` | Single-dynamic-offset static DMA | CERTAIN |
| `LowerDMAImpl::leaveModule` | `0x1194e70` | Split + trigger placement | CERTAIN |
| `LowerDMAImpl::createTriggers` | `0x1194c90` | Per-BB trigger conversion | CERTAIN |
| `LowerDMAImpl::convertDMA2Trigger` | `0x11941b0` | `InstDMA` → `InstDMATrigger` | CERTAIN |
| `SplitLargeDMAs::splitIfNeeded` | `0x11ab840` | Per-block descriptor-count cap | CERTAIN |
| `createDescs<Copy/Transpose/CCE>` | `0x11a3790`/`0x11a4180`/`0x11a4d40` | Descriptor-block builders | CERTAIN |

### Considerations

The opcode roster the dispatch keys on (`32 DMACopy`, `67 DMATrigger`, `68 DMADescriptor` base with IT ∈ {68..72}, `69 …Copy`, `70 …CCE`, `71 …Transpose`, `72 …Replicate`, `107 DMABlock`) is the same set the simulator's `visitInstruction` switches on, also keyed at `Instruction+0x58`. A reimplementation must use the descriptor opcode, not the source-op opcode, when materializing.

---

## Descriptor Block Build — `createDescs<T>`

### Purpose

`createDescs<T>` is the template that actually allocates an `InstDMABlock` and its `InstDMADescriptor`s, one specialization per descriptor opcode `T ∈ {Copy69, Transpose71, CCE70}` (Replicate72 takes a dedicated emit at `0x604d80`). It is where a single static DMA becomes the concrete descriptor block.

### Algorithm

```c
function createDescs<Copy>(I):                          // 0x11a3790 (Transpose 0x11a4180, CCE 0x11a4d40)
    q  = I.getDMAQueue();
    bb = descBlockBB[q];                                // 0x62af00
    if bb absent:
        bb = BasicBlockHolder::addBasicBlock(name);     // 0x600a40 — one BB per queue
        descBlockBB[q] = bb;
    n   = blockId[bb]++;                                // per-BB id (0x60f5e0)
    blk = bb.insertElement<InstDMABlock>("dma_block_" + n);  // 0x624350
    blk.setBlockId(n);                                  // 0x61c3c0
    blk.setSectionStart(is_first_in_section);           // 0x61fe00
    for each descriptor slot:
        d = bb.insertElement<InstDMADescriptorCopy>(name);  // 0x62ab70  (IT69)
        d.setDebugInfo(I.getDebugInfo());               // 0x5f5d40
        if isRemoteUpdateInstruction(I):
            blk.setRemoteNotification(ids);             // 0x6192e0
    createDescArgumentsOutputs(I, blk);                 // 0x11974a0 — fill descriptor APs
    emittedBlocks[I].push_back(blk);                    // 0x600bc0 / 0x61a900
    return blk;
```

The result is `InstDMABlock { InstDMADescriptor{Copy|CCE|Transpose|Replicate} × num_descriptors }` sitting on the per-queue basic block — exactly the SWDGE descriptor list the simulator iterates.

### `createDescArgumentsOutputs` — AP → descriptor fields

```c
function createDescArgumentsOutputs(I, blk):            // 0x11974a0
    for each in/out PhysicalAccessPattern ap:
        kind = Argument::ArgumentKind2string(ap);       // 0x6087f0 classify
        if input:  AccessPattern::unrollToArgument(I, idxMap);  // 0x5ed560
        if output: AccessPattern::unrollToOutput  (I, idxMap);  // 0x5ee6e0
        // "unroll" resolves the loop AffineIdx (pelican) into CONCRETE
        // (stride,num) APPair sequences per descriptor — the wire-form
        // ADDR4 + (stride,num) pairs (TENSOR1D..4D; the 4-D case spills
        // the 4th pair past the 16-byte slot).
        reAssignMemLoc(ap, memlocSet);                  // 0x5f8d30, called 3×
        if isReplicateDMA(I):                           // 0x5fb770
            build_broadcast_pattern();   // replCount = dst.partSize/src.parts, bcast stride
```

The string literal `"DMA descriptor is unrolled and thus can re-assign Function Argument…"` confirms the unrolled-descriptor model: each descriptor carries fully static `(stride, num)` pairs, no symbolic loop index. The wire fields these produce (ADDR4 start-addr, `(stride,num)` pairs, the TENSOR4D 4-D spill) are documented in [DMA Encoding](../isa/dma-encoding.md).

### `reAssignMemLoc` — physical rebind

```c
function reAssignMemLoc(physAP, memlocSet):             // 0x1197210
    ml = MemoryLocationSet::getMemlocByTensorId(id);    // 0x6171a0
    physAP.setLocation(ml);   // re-point to post-allocation physical MemoryLocation
    // descriptor's ADDR4 start-addr + SB/PSUM/DRAM space now concrete
```

The allocator has already run, so this re-points each descriptor's `PhysicalAccessPattern` to its final physical `MemoryLocation` — making the descriptor's start address and storage space (SB / PSUM / DRAM) concrete.

### Considerations

> **QUIRK —** `createDescForReadVarAddr` (`0x1198860`) is the *only* dynamic path inside the static pass. A DMA whose address is a single runtime variable (`ReadVarAddr`) but is otherwise static-shaped is legal as a `Copy` descriptor whose address field is a *reference memloc*: `getMemoryLocationByName`/`addMemoryLocation` (`0x5f9a90`/`0x62d5c0`) materialize the named ref, then `MemoryLocation::setReferenceMemLoc` (`0x614d30`) makes the descriptor's address field point at it. The runtime patches that field from the variable. A reimplementation that assumes the static pass only sees fully-static addresses will reject this legal case.

---

## Split + Trigger Placement — `leaveModule`

### Purpose

After the per-inst walk has built every `InstDMABlock`, `leaveModule` (`0x1194e70`) finalizes the function: it splits any over-budget descriptor block, converts the surviving `InstDMA`s into triggers, and terminates each data-path engine's basic block.

### Algorithm

```c
function LowerDMAImpl_leaveModule(M):                   // 0x1194e70
    for each BB in BasicBlockHolder::blocks():          // 0x5ee930
        for each inst where InstDMABlock::classof(inst): // 0x625200
            SplitLargeDMAs::splitIfNeeded(blk);         // 0x6024a0 → §budget split
        createTriggers(BB);                             // 0x5ff8c0
    for each data-path engine (isDataPathEngine(eng)):  // 0x617a50
        bb.insertElement<InstNoOp>(...);                // 0x623490 terminator
        // placed at getFirstPostambleInstruction (0x5ed130)
```

### `SplitLargeDMAs::splitIfNeeded` — per-block descriptor budget

```c
function SplitLargeDMAs_splitIfNeeded(blk):             // 0x11ab840
    descs = blk.descriptors();                          // 0x5ff910
    switch (descriptor opcode):
        case 71 (Transpose): splitTranspose(...);       // 0x5ff000 / body 0x11ab110
        case 72 (Replicate): ...;
        default (Copy/CCE):  collectSplitMetaInfoCopyCCE(...);  // 0x5ed470
    if isLocationDRAM(arg) and isLocationDRAM(out):     // 0x600070
        pts = AccessPattern::IsContiguous(...);         // 0x602580 contiguous split points
    generateSplittedDMAs(inst, SplitMetaInfo[], contiguous);  // 0x611410
    // rewrites one over-large descriptor block into several within budget
```

`splitIfNeeded` is the *static* per-block cap — distinct from the *dynamic* DGE ring-depth split in `lower_dynamic_dma` (next section). The rodata `"Unable to find … DMA Block/descriptor, use DMA trigger instead"` is its fallback diagnostic.

### `convertDMA2Trigger` — `InstDMA` → `InstDMATrigger`

```c
function convertDMA2Trigger(I):                         // 0x11941b0
    qos = getDMAQoSClass(I);                            // 0x614390
    t = new InstDMATrigger;
    Instruction::setPredicates(t, I.predicates);        // 0x62cfe0
    t.setDebugInfo(I.getDebugInfo());                   // 0x5f5d40
    replaceInstWithInstArr<InstDMATrigger>(I, names);   // 0x60b1d0 — replace in place
    InstDMABlock::setTrigger(blk, t);                   // 0x617ef0 — bind block→trigger
```

`createTriggers` (`0x1194c90`) first calls `collectInstrForTriggerConversion` (`0x613cd0`) to recursively gather every `InstDMA` still present in the BB and its nested blocks, then converts each one. The kind is re-asserted (`isCopyOrCastDMA / isTransposeDMA / isCCEDMA / isReplicateDMA / isDynamicOffsetDMA`) — a DMA must match exactly one descriptor family.

> **NOTE —** `replaceInstWithInstArr` replaces the `InstDMA` *in place* on the **same engine BB**. The engine was fixed by the engine-assign pass (`InstDMA+0x90`); the trigger inherits it. That the engine value originates at `+0x90` is [INFERRED] — the convert path inherits it through `replaceInstWithInstArr` rather than re-reading the field, so no direct load of `+0x90` appears here.

The net result for a static `InstDMACopy` on engine `E`, queue `Q`:

```text
InstDMABlock_Q { InstDMADescriptorCopy × N }   +   InstDMATrigger(→block)  on E
```

---

## `lower_dynamic_dma` (L14) — The Peel → Unroll → Split → Check Pipeline

### Purpose

`lower_dynamic_dma` resolves every DMA whose access pattern carries a *dynamic* (runtime-computed) dimension — a pelican `QuasiAffineExpr` offset that no static descriptor field can hold. It is the headline of this page: a four-stage sub-pipeline that **peels** the dynamic dimension into descriptor-sized chunks, **unrolls** the dynamic dims into per-iteration static DMAs, **splits** any result that exceeds the DGE ring depth, and **checks** that no dynamic AP survives. It runs *before* `lower_dma`, so by the time the static pass runs, every DMA has a concrete `AccessPattern`.

### Entry Point

```text
LowerDynamicDMA::run(bir::Module&)     0xcd9620   ── 4-stage pass entry
  ├─ PeelDynamicDMAImpl  {M}.visit(M)  0xcd97b3 → 0x61e510  ── §Peel
  ├─ UnrollDynamicDMAImpl{M}.visit(M)  0xcd97f6 → 0x61e510  ── §Unroll
  ├─ SplitDynamicDMAImpl {M}.visit(M)  0xcd9839 → 0x61e510  ── §Split
  └─ CheckDynamicDMAImpl {M}.visit(M)  0xcd9877 → 0x61e510  ── §Check
```

All four call sites jump to the **same** `IRVisitor<LowerDynamicDMAImpl>::visit(Module&)` (`0x61e510`) with a different subclass `this` (vtables `0x3d8f0e0` / `0x3d8f0f8` / `0x3d8f110` / `0x3d8f128`). The four stages share the base `visitInstruction`, `cloneInstruction`, and `updateAP`; each overrides `legalizeAP`.

### Algorithm

```c
function LowerDynamicDMA_run(M):                        // 0xcd9620
    push_log_channel(M.getName());                      // boost-log per-pass tag
    PeelDynamicDMAImpl{M}.visit(M);                     // 0xcd97b3
    UnrollDynamicDMAImpl{M}.visit(M);                   // 0xcd97f6
    SplitDynamicDMAImpl{M}.visit(M);                    // 0xcd9839
    CheckDynamicDMAImpl{M}.visit(M);                    // 0xcd9877
```

The shared driver detects a dynamic-AP DMA and hands it to the stage's `legalizeAP`:

```c
function LowerDynamicDMAImpl_visitInstruction(I):       // 0xce4740
    op = *(I + 0x58)
    arg = getArgument<PhysicalAccessPattern>(i);        // 0x613b60
    out = getOutput  <PhysicalAccessPattern>(j);        // 0x6103f0
    if arg.isTensorIndirectDynamicAP() ||               // 0x60f310
       out.isTensorIndirectDynamicAP():
        legalizeAP(out, arg);                           // virtual — the stage's override
    if isTransposeDMA(I): special_case_transpose();     // 0x604120 (forced SWDGE)
    on illegality:
        reportError / NeuronAssertion<ErrorCode>(...)   // 0x60b770 / 0x61bbc0 + fmt::vformat
```

#### Stage 1 — Peel (`PeelDynamicDMAImpl::legalizeAP` @ `0xcdf320`)

```c
function PeelDynamicDMAImpl_legalizeAP(dst, src):       // 0xcdf320
    maxElems = getMaxDMADescElements(I);                // 0x609c50 — max elems/descriptor
    canon    = DynamicAPINFO::getCanonicalPattern();    // 0x5ec2b0
    isDram   = Argument::isLocationDRAM();              // 0x600070
    newPairs = [];
    // PEEL the dynamic dimension into chunks each ≤ maxElems,
    // so every peeled chunk fits ONE concrete descriptor:
    for each chunk of the dynamic dim sized ≤ maxElems:
        newPairs.push_back(chunk_APPair);               // 0x61d2d0
    updateAP(dst, newPairs, off);                       // 0xcdebf0 — write pattern back
```

Peel takes a dynamic dimension that may exceed one descriptor's element capacity and slices it into descriptor-sized boundary chunks. This handles the leading/trailing partial iterations the loop's affine index would otherwise leave non-uniform.

#### Stage 2 — Unroll (`UnrollDynamicDMAImpl::legalizeAP` @ `0xce0ff0`)

```c
function UnrollDynamicDMAImpl_legalizeAP(dst, src):     // 0xce0ff0
    dims    = calcUnrolledDims(APPair[], count);        // introsort over {lambda} comparator
                                                        //   (0xcdc3e0 / 0xcde750 / 0xcde900)
    offsets = calcOffsets(APPair[], n);                 // 0xcdb2c0 — per-iter byte offsets
                                                        //   ({lambda(u32,long)} ti @0x3d8f0c0)
    for each unrolled index k:
        clone = cloneInstruction(targetBB, I, name_k);  // 0xcde4f0
        updateAP(clone_pattern, resolved_subpattern_k); // 0xcdebf0
    // dynamic AP → N static-AP DMAs (which lower_dma then lowers to descriptors)
```

Unroll picks which dynamic dims to materialize (sorted by a cost comparator), computes the per-iteration byte offset list, and emits one cloned static DMA per unrolled index. After this stage a data-dependent-shape DMA is `N` ordinary static DMAs.

#### Stage 3 — Split (`SplitDynamicDMAImpl::legalizeAP` @ `0xce2d00`, budget in `calcFreeSplits` @ `0xcddcd0`)

This is the **DGE ring-depth limit**. When the resolved descriptor count exceeds the ring depth, the DMA is split into multiple passes, each within budget:

```c
function SplitDynamicDMAImpl_calcFreeSplits(numElems, granule, dgeType, I):  // 0xcddcd0
    if granule == 1:
        maxDesc = getDGEMaxDescNumWithFastestShape(dgeType, I);   // 0x1093940
    else:
        maxDesc = getDGEMaxDescNumBasedOnShape(numElems, granule, dgeType, I); // 0x1093950
    // ... body shared at sub_10934D0, called with MAX_PARALLEL_DMA_ENGINE_NUM=16:
    //   @0x1093808  cmpl $0x20, 0x58(rdx)     // opcode 0x20 = 32 = DMACopy gate
    //   @0x1093813  mov  $0x3fc,  %eax        //  1020 = SWDGE ring depth
    //   @0x1093818  mov  $0xfff0, %ecx        // 65520 = HWDGE ring depth
    //   @0x109381d  cmpl $0x1, 0x178(rdx)     // DGEType == SWDGE(1) selects 1020
    //   @0x1093828  cmp  $0x2, %esi           //         == HWDGE(2) selects 65520
    //   @0x1093832  call getLargestFactorWithThreshold(maxFactor)  // 0x1092b10
    //   @0x109383f  imul %r14, %rax           // descriptors = factor * shape
    return vector<u64> per-split sizes;
```

`getLargestFactorWithThreshold` (`0x1092b10`) returns the largest factor of `N` that is `≤16` (`cmp $0x10,%eax` @ `0x1092b56`, with the divisor loop bounded by `cmp $0x11` @ `0x1092b38`), then `imul`s — so the descriptor count distributes evenly across the `≤16` parallel DMA engines (`MAX_PARALLEL_DMA_ENGINE_NUM`). `legalizeAP` (`0xce2d00`) then clones/rewires the DMA into `≤depth` descriptors per pass; `isDescNumOk` (`0x1093970`) is the post-check that `descCount ≤ DGE depth`.

> **GOTCHA —** the depth selector is gated on `opcode == 32` (DMACopy) at `0x1093808` *before* the SWDGE/HWDGE branch. The `1020`/`65520` literals are both loaded unconditionally (`mov` into `eax`/`ecx`) and then the `DGEType` compares at `0x109381d`/`0x1093828` pick which one survives. A reimplementation that branches on DGEType *before* loading both constants will diverge on the non-DMACopy fallthrough.

#### Stage 4 — Check (`CheckDynamicDMAImpl::legalizeAP` @ `0xcdc550`)

```c
function CheckDynamicDMAImpl_legalizeAP(dst, src):      // 0xcdc550
    // final re-validation: no remaining unresolved dynamic AP,
    // desc counts in budget. The enforced invariant (rodata):
    //   "DMA descriptor cannot have dynamic AP"
    assert no descriptor still carries a dynamic AP;
```

After lowering, **no descriptor may still carry a dynamic AP** — every dynamic dim must have been peeled, unrolled, or register-rewired. The rodata string and the stage's terminal position pin that invariant; the full set of checks this stage performs beyond it was not exhaustively traced.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LowerDynamicDMA::run` | `0xcd9620` | 4-stage pass entry | CERTAIN |
| `LowerDynamicDMAImpl::visitInstruction` | `0xce4740` | Shared driver; dynamic-AP detect | CERTAIN |
| `PeelDynamicDMAImpl::legalizeAP` | `0xcdf320` | Peel dyn dim to desc budget | CERTAIN |
| `UnrollDynamicDMAImpl::legalizeAP` | `0xce0ff0` | Unroll dyn dims → N static DMAs | CERTAIN |
| `UnrollDynamicDMAImpl::calcOffsets` | `0xcdb2c0` | Per-iteration byte offsets | CERTAIN |
| `SplitDynamicDMAImpl::legalizeAP` | `0xce2d00` | Over-budget DGE split | CERTAIN |
| `SplitDynamicDMAImpl::calcFreeSplits` | `0xcddcd0` | Ring-depth budget + 16-way factor | CERTAIN |
| `getDGEMaxDescNumWithFastestShape` | `0x1093940` | Depth for granule==1 | CERTAIN |
| `getDGEMaxDescNumBasedOnShape` | `0x1093950` | Depth for granule≠1 | CERTAIN |
| `isDescNumOk` | `0x1093970` | Post-check descCount ≤ depth | CERTAIN |
| `getLargestFactorWithThreshold` | `0x1092b10` | Largest factor ≤16 | CERTAIN |
| `CheckDynamicDMAImpl::legalizeAP` | `0xcdc550` | Final dynamic-AP invariant | HIGH |
| `LowerDynamicDMAImpl::updateAP` | `0xcdebf0` | Write resolved pattern back | CERTAIN |
| `LowerDynamicDMAImpl::cloneInstruction` | `0xcde4f0` | Clone DMA per chunk/index | CERTAIN |
| `rewireDynamicAPRegisters` | `0xef91d0` | Rename per-clone offset register | CERTAIN |

### Considerations

`updateAP` (`0xcdebf0`) calls `DynamicAPINFO::setActualPattern` (`0x600230`): each stage replaces the DMA's symbolic pelican expression with the concrete `(stride, num)` `APPair` list it produced. `cloneInstruction` (`0xcde4f0`) copies arg/out APs (`cloneArgumentsOutputs` `0x5f3970`) and registers the clone in the symbol table (`insertIntoSymboltable` `0x61c940`); Peel/Unroll/Split all use it to spawn per-chunk static DMAs.

---

## Dynamic-Address Register Rewiring

### Purpose

The one truly-dynamic form that survives lowering is the **offset-register-driven** descriptor: the descriptor's address field is patched at runtime from a control register whose value is computed by `InstRegisterAluOp` / `ReadVarAddr` upstream. Each peeled/unrolled clone must read its *own* register.

### Algorithm

```c
function rewireDynamicAPRegisters(physAP, F, regNameMap):   // 0xef91d0
    physAP.forEachDynamicExpr([&](QuasiAffineExpr& e){      // 0x5f8aa0
        rename e's offset register per regNameMap;          // per-clone control register
    });

function createDGEDynamicAPINFO(M, a, b, c):                // 0x10a6a30
    // builds the DynamicAPINFO that drives the DGE (runtime offset-register descriptor)
```

The validator `_validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided` (`0xf140a0`) ensures a DMA is *either* scalar-dynamic-offset (one runtime offset) *or* vector-dynamic-offset (a gather index AP) — never both. This is the "RegisterAP must be offset register" mechanism.

### Considerations

> **QUIRK —** dynamic addresses and transpose **force SWDGE**. The rodata literals are verbatim in the binary: `"DGE must be enabled when dynamic src access in DMA is used"`, `"DGE's RegisterAP must be offset register"`, `"DGE transpose must have 4D AP"`, `"transpose only supported for HBM->SB"`. A transpose DMA may not be HWDGE (`checkDMATranspose` asserts `getDgeType() == Unassigned || SWDGE`); HWDGE needs a *bound engine id* (`isHWDGEDMAWithEngineSet`) and synthesizes descriptors on-device from a compact offset-register AP — it **cannot** express an arbitrary runtime address list. So a DMA whose descriptor list is data-dependent takes the software-DGE path: the compiler materializes the explicit `InstDMADescriptor` list (the peel/unroll above) and the engine just fires a static trigger. The only HWDGE-ish dynamic the runtime tolerates is the offset-register gather (vector-indirect), which the simulator itself software-materializes.

---

## Static vs Dynamic Split + Codegen Handoff

```text
lower_dynamic_dma (L14, FIRST)                lower_dma (L17, SECOND)
─────────────────────────────                ───────────────────────
runtime-address / data-dep-shape DMA          every DMA now STATIC-AP + bound engine/queue
  Peel  → desc-sized chunks                   createDescs<Copy69|CCE70|Transpose71|Replicate72>
  Unroll→ N static-AP DMAs                       → InstDMABlock_Q { InstDMADescriptor × N }
  Split → ≤ DGE ring depth                     SplitLargeDMAs caps per-block desc count
            (SWDGE 1020 / HWDGE 65520, ≤16)    convertDMA2Trigger:
  Check → no dynamic AP left                     InstDMA → InstDMATrigger on assigned engine
  rewireDynamicAPRegisters: offset regs        single-runtime-offset survivor →
  Dynamic ⇒ SWDGE                                createDescForReadVarAddr (reference memloc)
```

The single dynamic-offset static DMA that survives into L17 is handled *inside* `lower_dma` by `createDescForReadVarAddr` (a Copy descriptor whose address field is a reference memloc patched at runtime). The post-`lower_dma` IR is a per-engine stream of `[InstDMATrigger]` + `[InstDMABlock → InstDMADescriptor*]`, queue-grouped. Downstream low-pipeline passes (block coalescing, queue-switch optimization, the per-engine expanders, then codegen) emit each engine's descriptor blocks + triggers into the 64-byte TPB bundles; the descriptor wire fields (ADDR4 start-addr, `(stride,num)` pairs, indirect/gather records, TENSOR4D 4-D spill) are the forms documented in [DMA Encoding](../isa/dma-encoding.md), and the simulator re-executes them as trigger→block→descriptor-list copies.

---

## Evidence summary

Read from disassembled bodies, named dynamic symbols and `.rodata`: the headline constants `1020` / `65520` / `16` at `0x1093813` / `0x1093818` / `0x1092b56`; the four `IRVisitor<LowerDynamicDMAImpl>::visit` call sites at `0xcd97b3` / `f6` / `39` / `77`, which are what fix the Peel→Unroll→Split→Check ordering; `LowerDMA::run` `0x119adc0` and `LowerDynamicDMA::run` `0xcd9620`, both named in `nm -DC`; `convertDMA2Trigger` `0x11941b0`, `createDescForReadVarAddr` `0x1198860`, `splitIfNeeded` `0x11ab840`, `calcFreeSplits` `0xcddcd0` and `rewireDynamicAPRegisters` `0xef91d0`; and all four SWDGE-forcing rodata literals, quoted verbatim.

> **GOTCHA — a `.plt` thunk and the real body are two addresses for one symbol.** `libwalrus.so` retains its dynamic symbol table, so a symbol like `isDescNumOk` appears both as a `.plt` thunk in the `0x5e9020`–`0x62d650` band and as its real body — here `neuronxcc::backend::isDescNumOk` @ `0x1093970`, against a thunk at `0x6296d0`. Citing the thunk address looks plausible and disassembles to a jump; always follow it to the high-VA body.

## Limits of this reading

Two things on this page are reconstruction rather than direct reads, both flagged where they appear: the exact descriptor field byte offsets, which are carried over from the [DMA Encoding](../isa/dma-encoding.md) wire-form work rather than re-derived here, and the origin of the engine value inside `convertDMA2Trigger`.

The `CheckDynamicDMAImpl` stage is pinned only to its central invariant — the rodata `"DMA descriptor cannot have dynamic AP"` plus its terminal position in the pipeline. Whatever else it validates was not traced.

---

## Related Components

| Name | Relationship |
|---|---|
| [DMA Legalization](dma-legalization.md) | Earlier walrus legalization (strided/CCE); runs before materialization |
| Engine binding (planned `walrus/` page) | Sets `InstDMA+0x90` engine / `+0x94` sub-id that materialization consumes |
| [DGE-Level Dynamic DMA](../penguin/dge-level-dynamic-dma.md) | Penguin front: selects SWDGE/HWDGE/None DGEType |
| [Dynamic-Shape Synthesis](../penguin/dynamic-shape-synthesis.md) | Penguin front: emits the runtime-address `QuasiAffineExpr` offsets |
| [DMA Encoding](../isa/dma-encoding.md) | The descriptor wire format these passes fill (ADDR4, (stride,num), gather records) |

## Cross-References

- [DMA Legalization](dma-legalization.md) — the shipped earlier pass; materialization assumes its strided/CCE legality
- [DGE-Level Dynamic DMA](../penguin/dge-level-dynamic-dma.md) — where the DGEType at `InstDMA+0xF8` is chosen
- [Dynamic-Shape Synthesis](../penguin/dynamic-shape-synthesis.md) — origin of the dynamic access-pattern expressions Peel/Unroll resolve
- [DMA Encoding](../isa/dma-encoding.md) — descriptor wire fields the unrolled `(stride,num)` pairs become
- [Symbolic AP and Register ALU](../penguin/symbolic-ap-register-alu.md) — the offset-register computation `rewireDynamicAPRegisters` patches
