# DMA Queue Allocation — alloc-queues / chain-dma-transposes / insert-switch-queue

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core logic but the absolute `.text` addresses drift by a few `0x10`s per build — every address below is the **cp310** frame). The three passes live in `neuronxcc/starfish/lib/libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; `.data` adds a `+0x400000` delta). The `bir::DMAQueue` / `bir::AccessPattern` / `bir::TensorKind` layouts live in `libBIR.so`. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

Three backend passes — pipeline orders **124, 125, 126** — close the DMA half of the walrus backend. By the time they run, every DMA already has a materialized physical access pattern, a DGE engine and `engine_id` (bound the pass before, `assign_hwdge_engine` at order 123), a QoS class, and dependence edges. What is still missing is the `bir::DMAQueue` **object** each DMA fires on, the inter-transpose ordering that lets the hardware keep one descriptor context warm, and the queue-instance multiplexing instructions. These three passes supply all three, in that order, and codegen then serializes the bindings into the per-engine NEFF instruction stream.

- **`alloc_queues`** (order 124) assigns every DMA-class instruction a `bir::DMAQueue`. The decisive mechanism is **value-identity dedup**: each DMA derives a `QueueTuple` (engine identity + `DMAQueue::Type` + an engine-count tuple), `QueueTuple::getFullName()` flattens that tuple to a `std::string`, and `Module::getQueueByName(name)` is the dedup lookup. Two DMAs whose `QueueTuple` produces the **same full-name string** share **one** physical queue — dedup by *value*, never by pointer and never round-robin. The queue count is therefore data-dependent: it equals the number of distinct full-name tuples, clamped per type/engine to the hardware DMA-engine count.
- **`chain_dma_transposes`** (order 125) walks each basic block in program order and links consecutive same-queue transpose DMAs with an `Output` (WAW) dependence edge, so the scheduler serializes them on their shared queue and the queue re-uses one warm descriptor context instead of re-arming per transpose. A companion sub-pass orders cross-bar (XBar) transposes against the SB collectives / remote-SB pushes they feed and consume.
- **`insert_dma_switch_queue_instance`** (order 126) inserts one `InstSwitchQueueInstance` (IT 85) at the **front of every non-loop-body basic block**, one per **DATA-type** DMA queue the block uses. The op carries `{Q, Instance}` where `Instance` is a fresh `BasicBlockHolder` owned by `Q`'s `dma_blocks` list — re-pointing the hardware at a fresh descriptor ring so producers in different blocks do not stomp each other's in-flight descriptors. Loop bodies are deliberately skipped.

The bar for this page is that a reimplementer can reconstruct **exactly how a queue is identified and deduplicated, when a transpose chain forms, and where a switch is inserted**. The `QueueTuple` value-identity dedup is the headline; the `{Q, Instance}` insertion is the second deliverable. This page is the queue *binding* stage; the earlier per-pass DMA legalization (`alloc_queues`'s siblings at orders 18/19 — DMA materialization, page 8.27) and the engine binding it consumes (`assign_hwdge_engine` — DMA engine binding, page 8.28) are separate pages, and the wire encoding of the switch/reset ops is documented in [DMA Encoding](../isa/dma-encoding.md). The `def.json` DMA-queue attribute table is a planned formats page (12.3).

| | |
|---|---|
| **`alloc_queues`** | `AllocQueues::run` `@0x111c1c0`; impl ctor `@0x111bfb0`; `visitInstruction` `@0x111be20`; `findQueue` `@0x111b730`; `findQueueTuple` `@0x111ae30`; `fillQueueTuple` `@0x111ab80`; `determineEngine` `@0x1118540` |
| **`QueueTuple`** | ctor `@0x11182f0`; `less_than` `@0x1118330`; `getFullName[abi:cxx11]` `@0x11183b0`; `operator bool` `@0x1118530` |
| **`chain_dma_transposes`** | `ChainDMATransposes::run` `@0x16a3880`; `chainDMATransposes(BB&)` `@0x169f330`; `chainXbarTransposeAndRemoteDMA(BB&)` `@0x16a0db0` |
| **`insert_dma_switch_queue_instance`** | `InsertDMASwitchQueueInstance::run(Module&)` `@0x169b080`; `run(vector<Module>&)` `@0x169c420`; impl `enterBasicBlock(BB&)` `@0x169d1d0`; `BirLinker::fpassInsertSwitchQueueInstances` `@0x15d9080` |
| **Dedup primitives** | `Module::getQueueByName` `@plt 0x5fbad0`; `Module::addQueue(name, DMAQueue::Type)` `@plt 0x62bf90` |
| **Config knobs (.bss)** | `optNumDMAEngines` `@0x3e032e0`; `optEnableMaxIndirectDMAQueues` `@0x3e033a0`; log category `aqCat` `@~0x3e03460` |
| **Source paths (baked `.rodata`)** | `…/neuronxcc/walrus/alloc_queues/src/alloc_queues.cpp` · `…/walrus/chain_dma_transposes/src/chain_dma_transposes.cpp` (verbatim) · `…/walrus/insert_dma_switch_queue/…` (truncated in assert) |

All body-frame addresses are read off the `nm -DC` dynamic table and `objdump -d` bodies of the cp310 `libwalrus.so`; the EdgeKind constant, engine-id arms, queue-field stamps, and dedup call chain are byte-verified below. CONFIRMED unless a row says otherwise.

---

## alloc-queues (order 124) — assign every DMA a `bir::DMAQueue`

### Purpose

`alloc_queues` is the single authority that turns a DMA instruction into a queue binding. It is a CRTP `bir::IRVisitor<AllocQueuesImpl, void>` (`visit` `@0x111efc0`); `run()` walks the whole `Module` → every `Function` → every `BasicBlock` → every `Instruction`, dispatching `visitInstruction` / `visitInstActivation`. `input_dma_coalescing` (order 29) deliberately left QoS/queue untouched precisely because this pass owns them.

### Entry Point

```text
AllocQueues::run(Module&)  @0x111c1c0
  └─ open "aqCat" boost::log scope; build libfort STATS table
  └─ AllocQueuesImpl(logger, module, opts)  @0x111bfb0
  └─ IRVisitor<AllocQueuesImpl>::visit  @0x111efc0   // Module → Func → BB → Inst
       ├─ visitInstruction(Instruction&)     @0x111be20
       └─ visitInstActivation(InstActivation&) @0x111bd50
  └─ post-walk: read optEnableMaxIndirectDMAQueues→[0x78]; optionally one queue
     per indirect DMA; print {Num Queues, Num instructions} census into libfort
```

The `run()` body is otherwise pure diagnostics: a libfort (`ft_*`) table with columns `"Num Queues"` / `"Num instructions"` / `"Alloc Queue info"` (`.rodata @0x1c83094`) and a trailing `"maximize-indirect-dma"` / `"num-queues"` note (`@0x1c8309f`). CONFIRMED.

### The DMA-class gate

`visitInstruction` is byte-verified end-to-end (`@0x111be20`):

```c
// AllocQueuesImpl::visitInstruction(Instruction &I)  @0x111be20  [CONFIRMED]
void visitInstruction(Instruction &I) {
  uint32_t it = *(uint32_t*)(&I + 0x58);          // InstructionType  (D-D01)
  // gate — only DMA-family opcodes are queued:
  //   mov 0x58(%rsi),%eax ; cmp $0x12,%eax ; je accept            // IT 18 == DMA
  //   lea -0x13(%rax),%edx ; cmp $0x30,%edx ; jbe maskcheck       // idx = it-0x13, ≤48
  //   lea 0x1df22c0(%rip),%rcx ; cmpb $0,(%rcx,%rdx) ; jne accept // accept mask
  //   sub $0x2f,%eax ; cmp $0x3,%eax ; ja skip                    // it ∈ {0x2F..0x32}
  if (!( it == 0x12
         || acceptMask_0x1df22c0[it - 0x13] != 0
         || (uint32_t)(it - 0x2f) <= 3 ))
      return;                                       // not a DMA → carries no queue

  DMAQueue *q = *(DMAQueue**)(&I + 0xF0);          // already-assigned queue (idempotency)
  if (q == nullptr)
      q = findQueue(I);                            // ← the real allocation

  // "Num instructions" census: unordered_map<string,size_t> keyed by queue NAME:
  //   lea 0x68(%r12),%rsi ; call _Map_base::operator[] @0x5ec800 ; addq $1,(%rax)
  perQueueInstrCount[ q->name /* q+0x68 */ ] += 1;

  *(uint32_t*)(&I + 0x90) = *(uint32_t*)(q + 0xAC); // stamp queue TYPE onto the inst
  *(DMAQueue**)(&I + 0xF0) = q;                     // store queue ptr back on the inst
}
```

> **The accept set is the bir DMA-opcode taxonomy, reused verbatim.** The 49-byte mask at `0x1df22c0` (index `it-0x13`) has `1` at `it ∈ {0x13 Load, 0x16 Save, 0x20 TensorCopy, 0x29..0x2E Generic/Abstract-DMA family, 0x3B}`; the trailing `it ∈ {0x2F..0x32}` arm catches the remaining DMA-copy opcodes. This is the same family the D-D01 "IT 18 DMA" predicate accepts — `alloc_queues` does not invent its own opcode set. CONFIRMED (`cmp`/`cmpb` immediates read directly).

> **Correction over the field note.** An earlier read recorded the third arm as a bare `it == 3`. The disassembly is `sub $0x2f,%eax; cmp $0x3,%eax; ja` — a *range* `it ∈ {0x2F, 0x30, 0x31, 0x32}`, not a single equality. Use the range.

`visitInstActivation` (`@0x111bd50`) queues an activation **only** when its accumulate/spill flag is set:

```c
// AllocQueuesImpl::visitInstActivation(InstActivation &I)  @0x111bd50  [CONFIRMED]
void visitInstActivation(InstActivation &I) {
  if (*(uintptr_t*)(I.payload + 0x1F0) != 0)        // accumulator-dump flag set
      findQueue(I);                                 // the activation itself drives a DMA
  // otherwise: carries no queue
}
```

### Algorithm — `findQueue`, the allocate-or-reuse core

This is the dedup. CONFIRMED — the entire call chain is read off `findQueue`'s body:

```c
// AllocQueuesImpl::findQueue(Instruction &I)  @0x111b730  [CONFIRMED]
DMAQueue *findQueue(Instruction &I) {
  QueueTuple t   = findQueueTuple(I);               // call @0x61a200 → impl 0x111ae30
  Module    *m   = I.getModule();                   // call @0x5ede60
  string    name = t.getFullName();                 // call @0x625c20 → impl 0x11183b0
                                                    //   ↑ THE DEDUP KEY (§dedup-key)
  DMAQueue *q    = m->getQueueByName(name);          // call @0x5fbad0  — hash lookup
  if (q != nullptr)
      return q;                                      // ← REUSE: identical tuple ⇒ one queue

  // miss: build the queue's display name (appends a "q"<idx>-style suffix via
  //   basic_string::_M_construct @0x5f9fb0 + _M_append @0x600aa0, three appends)
  q = m->addQueue(name, t.type /* DMAQueue::Type */); // call @0x62bf90 — CREATE
  return q;
}
```

> **Queue identity is value-identity, not pointer-identity and not round-robin.** Two DMAs land on the **same** `DMAQueue` iff `getFullName()` returns **equal strings** — i.e. same engine identity ∧ same `Type` ∧ same engine-count tuple. The number of queues = the number of **distinct** full-name strings that appear, clamped per type/engine to `optNumDMAEngines`. There is no allocation counter, no `engine_id % N`; the hash table in `Module` *is* the allocator. This is the page's single most important fact: a reimplementer who allocates per-instruction, or round-robins, produces a different (and wrong) queue assignment.

`Module::addQueue`'s demangled signature confirms the second argument is `bir::DMAQueue::Type` (`@plt _ZN3bir6Module8addQueueE…NS_8DMAQueue4TypeE`), so the `Type` the tuple computed is baked into the queue at creation. CONFIRMED.

### Building the tuple — `findQueueTuple`

```c
// AllocQueuesImpl::findQueueTuple(Instruction const &I)  @0x111ae30  [STRONG]
QueueTuple findQueueTuple(const Instruction &I) {
  QueueTuple t;                                     // default ctor @0x11182f0
  // iterate I's Arguments (boost filter/join iterators), llvm::cast<AccessPattern>;
  // for the AccessPattern that names the DMA endpoint:
  fillQueueTuple(ap, t);                             // Type + QoS + engine-count
  determineEngine(I, t);                             // engine identity (writes t+0x30)
  // arch-aware clamp: getEngineCount(EngineType, ArchLevel) @plt
  return t;                                          // less_than @0x1118330 totally
                                                    //   orders tuples for census sort
}
```

`fillQueueTuple` computes the three keying fields. CONFIRMED — the `TensorKind → DMAQueue::Type` map and the engine-count clamp are read directly:

```c
// AllocQueuesImpl::fillQueueTuple(AccessPattern const &ap, QueueTuple &t) @0x111ab80
bool fillQueueTuple(const AccessPattern &ap, QueueTuple &t) {
  MemoryLocation *ml = ap.getLocation();             // vtable call unless static-null
  // assert ml->classTag(+0x110) == 4  (llvm::cast<MemoryLocation> guard)
  int kind = ml->field[0xE0]->[0];                   // TensorKind  (r14d)

  determineEngine(...);                              // fills t's engine string (t+0..t+0x18)

  // DRAM(8) vs not — the QoS/location-alt bit:
  t.field[0x2C] = (ml->field[0xD8]->[0] == 8) ? rawByte(ap+0x2C)
                                              : (rawByte(ap+0x2C) ^ 1);

  // kind2QueType(TensorKind) → DMAQueue::Type  (assert names the helper verbatim:
  //   "bir::DMAQueue::Type {anonymous}::kind2QueType(bir::TensorKind)"):
  if      ((unsigned)(kind - 6) <= 1) t.type = 2;    // kinds 6,7 → DATA
  else if (kind == 9)                 t.type = 2;    //          9 → DATA
  else if (isTensorKindInput(kind))   t.type = 0;    //            → INPUT
  else if (isTensorKindOutput(kind))  t.type = 1;    //            → OUTPUT
  else  assert(0 && "Unexpected tensor kind");       // .rodata string CONFIRMED

  // engine-count clamp (caps requested queues at the HW DMA-engine count):
  t.field[0x24] = optNumDMAEngines->[0x78];
  t.field[0x28] = min(t.field[0x28], optNumDMAEngines->[0x78]);   // SSE pminud
  if (kind == 1 || kind == 4)                                     // Output / Const
      t.field[0x28] = impl->field[0x50];             // HWM override
  return true;
}
```

The `DMAQueue::Type` enum (cross-checked against D-E16): `0 input, 1 output, 2 data, 3 pinned_weight, 4 indirect_loadsave, 5 embedding_update, 6 collective_compute, 7 dynamic_act_table, 8 dynamic`. CONFIRMED.

### Picking the descriptor-gen engine — `determineEngine`

`determineEngine` reconciles the H33 engine hint (`I+0x90`) with the `DGEType` (`I+0xF8`) to decide which engine owns the queue, and writes the engine id to `t+0x30`. CONFIRMED — the engine-id arms are byte-verified:

```c
// AllocQueuesImpl::determineEngine(Instruction const &I, QueueTuple &t) @0x1118540
void determineEngine(const Instruction &I, QueueTuple &t) {
  // reportError pre-check on I.field[0x90]; the diagnostic immediates spell
  //   "...engine assigned to..."  (movl $0x64656e67="gned", movabs "ine assi" @0x11185b1)

  if (t.field[0x20] == 8 /*Type::dynamic*/ && !t.qosByte[0x2D]) {
      t.field[0x30] = 1;                             // movq $0x6/$0x1,0x30(%rbx) — engine 1
  } else if (I.field[0x90] == 4) {
      t.field[0x30] = 6;                             // special-case engine 6
  } else {
      t.field[0x30] = I.field[0x90];                 // default: the H33 engine hint
      uint32_t it = I.field[0x58];
      if (it == 0x12 || isDMAFamily(it)) {
          int dge = I.field[0xF8];                   // DGEType (None0/SWDGE1/HWDGE2/Unassigned3)
          if ((unsigned)(dge - 1) <= 1) {            // dge ∈ {1 SWDGE, 2 HWDGE}
              if (I.getModule()->field[0xAC] == 0x31) { /* arch gate, STRONG */ }
              t.field[0x30] = 6;                     // route SW/HW-DGE DMAs to engine 6
          }
      }
  }
  // getFullName later flattens EngineInfo2string(engineInfo) + the size fields.
}
```

> **`cmpl $0x8,0x20(%rbx)` (dynamic) and `cmpl $0x31,0xac(%rax)` (arch id) are byte-proven; the engine-1 and engine-6 stores are literal `movq $0x1,0x30(%rbx)` / `movq $0x6,0x30(%rbx)`.** The exact branch *structure* (which guard reaches which store) is STRONG: the two arch-id paths are inferred from the `getModule()->[0xAC] == 0x31` compare and are not exhaustively traced through every predecessor block.

### The dedup key — `QueueTuple::getFullName`

`getFullName` (`@0x11183b0`) concatenates `EngineInfo2string(engineInfo)` (`@plt`) with the `Type` and the two size fields into one `std::string`. That string is the argument to `Module::getQueueByName`, so the equivalence class "same queue" is precisely **same engine-info ∧ same Type ∧ same size tuple**. Queue identity is ENGINE+TYPE keyed. CONFIRMED. (`QueueTuple::shorten(string)` `@0x1118300` is a name-truncation helper used for the human-readable display name, not for the dedup key.)

### Why order 124

`assign_hwdge_engine` (order 123) sets the per-DMA `engine_id` (`I+0x90`) and `DGEType` (`I+0xF8`); `alloc_queues` consumes **both** (`determineEngine`). It cannot run earlier — the engine binding does not exist yet — and must run before chaining/switch-insertion (125/126), which operate on the `DMAQueue*` this pass stamps onto each inst (`I+0xF0`). STRONG (ordering inferred from the field-dependency chain; the order numbers are CONFIRMED from the pass registry).

---

## chain-dma-transposes (order 125) — amortise transpose-descriptor setup

### Purpose

A DMA transpose programs a 4-D transpose descriptor (the verifier `checkDMATranspose` requires a 4-D in/out AP, `TranOp ∈ {XZYW, XYZW}`, HBM→SB only). Re-arming that descriptor per transpose is expensive. `chain_dma_transposes` forces consecutive **same-queue** transposes into one ordered chain so the queue executes them back-to-back, setting the descriptor preamble once and re-using the queue's warm descriptor context. The source path `…/walrus/chain_dma_transposes/src/chain_dma_transposes.cpp` is baked verbatim in `.rodata`. CONFIRMED.

### Entry Point

```text
ChainDMATransposes::run(Module&)  @0x16a3880
  for each Function:                              // run gate reads Func→[…]→[0xAC]==0x1D
    for each BasicBlock bb:
      chainDMATransposes(bb)                @0x169f330   // §local transpose chain
      chainXbarTransposeAndRemoteDMA(bb)    @0x16a0db0   // §xbar↔SB ordering
```

### Algorithm — local transpose chain

CONFIRMED — the same-queue compare, the null guards, and the `EdgeKind` constant are byte-verified:

```c
// ChainDMATransposes::chainDMATransposes(BasicBlock &bb)  @0x169f330  [CONFIRMED]
void chainDMATransposes(BasicBlock &bb) {
  DMAQueue    *prevQueue      = nullptr;   // rsp+0x48
  Instruction *prevTransposeI = nullptr;   // rsp+0x30
  for (node : bb.instructions /* forward program order */) {
      Instruction *I = node - 0x8;
      uint8_t it = I->field[0x50];                 // list-node-relative IT
      if (!(it == 0x12 || acceptMask_0x1e03340[it])) continue;   // DMA-family gate
      if (!bir::isTransposeDMA(I)) continue;       // call @0x604120

      DMAQueue *curQueue = I->field[0xE8];          // the DMAQueue* alloc_queues stamped
                                                   //   (diag "cur queue "/"prev queue ")
      // null guards:  cmpq $0,0x48(rsp) ; test 0x30(rsp) ; je
      if (prevTransposeI != nullptr && curQueue != nullptr) {
          // cmp %rax,%rbx ; je  → same queue as last chained transpose: skip re-add
          if (curQueue == prevQueue) {
              /* already linked into this chain */
          } else {
              // chain THIS transpose after the previous one:
              //   xor %ecx,%ecx ; mov $0x3,%edx ; call addDependency @0x5eff00
              I->addDependency(prevTransposeI, /*EdgeKind=*/3 /*Output/WAW*/, /*=*/false);
              prevQueue = curQueue;
          }
      }
      prevTransposeI = I;                           // advance the chain head
  }
}
```

> **CHAIN CRITERION = adjacency in program order ∧ `isTransposeDMA` ∧ same `DMAQueue`.** The `mov $0x3,%edx` immediately before `addDependency` is byte-proven: `EdgeKind = 3 = Output` (D-E19 precedence `Invalid0 < Ordered1 < Anti2 < Output3 < Flow4`). It is a strict *ordering* edge, **not** a data edge — the scheduler serialises the transposes on their shared queue without inventing a false data dependence. The diagnostic `"DMATranspose dependency check: I …"` (`@0x1d85e60`) dumps each link.

### Algorithm — XBar transpose ↔ SB ordering

`chainXbarTransposeAndRemoteDMA` (`@0x16a0db0`) orders cross-bar transpose DMAs against the SB collectives and remote-SB pushes they exchange data with. Predicates (all `backend::` `@plt`): `isTransposeDMATrigger`, `isSBDstCollective`, `isRemoteSBPush`, plus `bir::isTransposeDMA`. It is a one-pass scan tracking latches; the verbatim diagnostic strings (`@0x1d85e88..`) enumerate the edges:

```text
[xbar-transpose] Got no DMA transpose, skipping chaining
[xbar-transpose] Adding dependency: xbar transpose -> SB collective:
[xbar-transpose] Resetting JustGotSBCollective after xbar transpose:
[xbar-transpose] Adding dependency: xbar transpose -> sb DMA:
[xbar-transpose] Resetting JustGotRemoteSBPush after xbar transpose:
[xbar-transpose] Adding dependency: sb DMA -> xbar transpose:           // reverse edge
[xbar-transpose] Adding dependency: sb collective -> xbar transpose:    // reverse edge
[xbar-transpose] Setting JustGotRemoteSBPush after remote SB DMA:
[xbar-transpose] LastXbarTranspose:   LastRemoteSBPush:   LastSBCollective:
```

> **The XBar ordering is BIDIRECTIONAL.** The latches `JustGotSBCollective` / `JustGotRemoteSBPush` plus `LastXbarTranspose` / `LastSBCollective` / `LastRemoteSBPush` produce edges in **both** directions: `xbar transpose → SB collective/sb DMA` **and** `sb DMA/collective → xbar transpose`. A reimplementer who only adds the forward `transpose → consumer` edge (the simpler reading) under-constrains the schedule — the binary tracks the *last* of each kind on both sides and emits the reverse edge when an XBar transpose follows an SB push/collective. Two internal diagnostic queue labels, `"TransposeInstQueue"` and `"RemoteSBQueue"` (`@0x1c892b1` / `@0x1c892c4`), name the streams. CONFIRMED (string roster read directly).

---

## insert-switch-queue-instance (order 126) — multiplex DATA queues

### Purpose

Two opcodes carry the queue-context switch (D-D01 / D-G03):

- **IT 85 `SwitchQueueInstance`** — directs subsequent DMAs of a queue to a specific queue-**instance** (a `BasicBlockHolder` in the `DMAQueue`'s `dma_blocks` list).
- **IT 86 `ResetQueueInstance`** — re-arms / resets the queue-instance pointer (same `{Q, Instance}` compare, no new holder).

Both carry two operands: `Q` (the `DMAQueue`) and `Instance` (a `BasicBlockHolder`) whose parent must be exactly `Q`. The verifier (`check{Switch,Reset}QueueInstance` `@0xfe6690` / `@0xfe7170`) asserts `Q != nullptr`, `Instance != nullptr`, and `llvm::cast<DMAQueue>(Instance->getParent()) == Q`. CONFIRMED.

### Entry Point

```text
InsertDMASwitchQueueInstance::run(Module&)  @0x169b080
  └─ IRVisitor<…Impl>::visit(BasicBlock&)  @0x169e8c0  → enterBasicBlock(bb)
InsertDMASwitchQueueInstance::run(vector<Module>&)  @0x169c420   // multi-module variant
BirLinker::fpassInsertSwitchQueueInstances()  @0x15d9080         // pipeline wiring (fpass)
```

`run()` itself is fmt-based logging; the work is in `enterBasicBlock`. The `BirLinker` hook (`@0x15d9080`) iterating `BasicBlockHolder::blocks()` confirms this is a finalisation fpass, consistent with order 126. CONFIRMED.

### Algorithm — the insertion worker

CONFIRMED — every call and every field stamp is byte-verified:

```c
// InsertDMASwitchQueueInstanceImpl::enterBasicBlock(BasicBlock &bb)  @0x169d1d0
void enterBasicBlock(BasicBlock &bb) {
  vector<DMAQueue*> queues = bb.getStaticDMAQueuesUsed();   // call @0x5f2860
  if (CFG::isLoopBody(&bb)) return;                          // call @0x5f4240 — SKIP loops

  for (DMAQueue *q : queues) {
      // cmpl $0x2,0xa8(%r14)  — process only DATA-class queues; skip the rest.
      //   (assert string "queue->getType() == DMAQueue::Type::DATA" @0x1d85e30
      //    guards the kept path.)
      if (q->field[0xA8] == 2 /*non-DATA discriminant*/) continue;

      // build a fresh queue-instance (a BasicBlockHolder inside q's dma_blocks list):
      BasicBlockHolder *holder = bb.parentHolder.addBasicBlock(name);   // call @0x600a40

      // build the SQI name "<…>_sqi" (suffix "_sqi" @0x1c89273):
      bb.checkInsertionPointValid(bb.front);                            // call @0x603af0
      auto *sqi = bb.insertElement<InstSwitchQueueInstance>(bb.front, name); // call @0x62ab30

      sqi->field[0xF8] = holder;            // mov %rbx,0xf8(%r14)  — Instance (new ring)
      sqi->field[0xF0] = q;                 // mov %r13,0xf0(%r14)  — Q (referenced queue)
      sqi->field[0x90] = q->field[0xAC];    // mov 0xac(%r13),%rax ; mov %rax,0x90(%r14)
  }
  // diag keys while emitting: "queueName" @0x1c8925f, "queueType" @0x1c89269.
}
```

> **One `SwitchQueueInstance` at the FRONT of every non-loop-body block, per DATA queue.** Loop bodies are excluded (`isLoopBody → return`) on purpose: a loop re-uses the same instance across iterations, and inserting a switch per iteration would defeat the warm-descriptor reuse `chain_dma_transposes` just set up. Each non-loop block that re-enters a DATA queue points the hardware at a fresh descriptor ring so producers in different blocks do not stomp each other's in-flight descriptors. CONFIRMED — `cmpl $0x2,0xa8` and the three field stores (`+0xF8`, `+0xF0`, `+0x90`) are read directly.

> **Reconciliation: which operand is `+0xF8`.** This pass writes `Instance → sqi+0xF8` and `Q → sqi+0xF0`. The codegen consumer ([DMA Encoding](../isa/dma-encoding.md)) treats, on the `InstSwitchQueueInstance`, `I+0xF8` as the `DMAQueue` object and `I+0xF0` as the `DMAQueue*` map key — the **same two slots**, read from the codegen side. On an ordinary `InstDMA`, by contrast, `I+0xF8` is the `DGEType`; the slot is overloaded by instruction layout. A reimplementer must not assume `+0xF8` has one fixed meaning across opcodes. CONFIRMED on both sides; the apparent `Instance` vs `DMAQueue` naming difference is the same physical field viewed at insertion vs. encode time.

### Relationship to `ResetQueueInstance` (86)

`SwitchQueueInstance(85)` **binds** a queue to a new instance at a block boundary; `ResetQueueInstance(86)` **re-arms** an existing instance (same `{Q, Instance}` compare, no new holder). They share the operand pair and the verifier rule. This pass emits only the SQI; `Reset` ops are emitted/consumed at descriptor-ring re-arm points, gated by the NEFF feature flag (next section).

---

## Codegen handoff (J-strand)

Each DMA now carries (a) `DMAQueue* @+0xF0` + queue-type `@+0x90` (`alloc_queues`), (b) `Output`-edge transpose chains (`chain_dma_transposes`), (c) front-of-block `SwitchQueueInstance` ops referencing `{Q, Instance}` (`insert_dma_switch_queue_instance`). The codegen consumers are present and CONFIRMED:

| Consumer | Address | Role |
|---|---|---|
| `LowerDMAImpl::visitInstSwitchQueueInstance` | `@0x1193300` | lower the switch op against `InstDMABlock::classof` (resolve the `Instance` holder to its DMABlock list) |
| `LowerDMAImpl::visitInstResetQueueInstance` | `@0x1193630` | lower the re-arm op |
| `CoreV2GenImpl::visitInstSwitchQueueInstance` | `@0x1264240` | emit instruction binary (`createInstBin`/`findBin`), length-guarded by `checkQueueNameLen` |
| `CoreV2GenImpl::visitInstResetQueueInstance` | `@0x1264830` | emit the re-arm binary; opens with a hard pre-assert on `ModuleAttribute::neff_feature_SQI_no_rearm` |

> **The no-rearm feature gate.** `CoreV2Gen` is gated by `ModuleAttribute::neff_feature_SQI_no_rearm` (assert `@0x1d6b670`): when the NEFF carries this feature, codegen suppresses the implicit queue re-arm — the SQI alone switches the instance without an automatic `Reset`. `SwitchQueue` *writes* a feature attr; `ResetQueue` *reads* the no-rearm attr (errcode 1106 if absent) — they are the set/read pair of the hardware Swap-Queue-Instance feature. CONFIRMED. The full wire encoding (op `0xCF` SwitchQueue / `0xC2` ResetQueue, the `+0x3B` instance flag, the per-trigger `DenseMap<DMAQueue*,BasicBlock*>` queue-name lookup) is documented in [DMA Encoding](../isa/dma-encoding.md).

This closes the DMA-resource binding: `assign_hwdge_engine` bound the **engine**, and these three passes bind the **queue** + **instance** + inter-transpose **order** that codegen serialises into the per-engine NEFF DMA stream (trigger → DMABlock → descriptors), partitioned by `DMAQueue`.

---

## Confidence and Gaps

| Claim | Confidence | Anchor |
|---|---|---|
| `findQueue` allocate-or-reuse chain (`findQueueTuple → getFullName → getQueueByName → addQueue`) | CONFIRMED | `@0x111b730` body; calls `@0x61a200/0x625c20/0x5fbad0/0x62bf90` |
| `getFullName` string == dedup key; queue identity is value-identity | CONFIRMED | `getQueueByName(name)` is the only lookup; `addQueue(name,Type)` on miss |
| DMA-class gate (`it==0x12 ∨ mask[it-0x13] ∨ it∈{0x2F..0x32}`) | CONFIRMED | `cmp`/`cmpb`/`sub` immediates `@0x111be51..` |
| `kind2QueType` TensorKind→Type map + engine-count `pminud` clamp | CONFIRMED | `fillQueueTuple` `@0x111ab80`; assert string `"…kind2QueType…"` |
| Transpose chain criterion (same-queue adjacency, `EdgeKind=3`) | CONFIRMED | `mov $0x3,%edx` + `cmp %rax,%rbx;je` `@0x169fb5f` |
| XBar ordering is bidirectional | CONFIRMED | reverse-edge diag strings `"sb DMA -> xbar transpose"` etc. |
| SQI inserted at front of non-loop-body block, DATA queues only; `{Q@+0xF0, Instance@+0xF8}` stamps | CONFIRMED | `cmpl $0x2,0xa8`; `mov …,0xf8/0xf0/0x90(%r14)` `@0x169dead..` |
| `neff_feature_SQI_no_rearm` codegen gate | CONFIRMED | assert `@0x1d6b670`; `CoreV2Gen` `@0x1264240/0x1264830` |
| `determineEngine` exact branch structure (engine 1 vs 6, arch-id `0x31` gate) | STRONG | engine-id `movq` arms read; predecessor reachability not exhaustively traced |
| Pass-ordering rationale (124 after 123, before 125/126) | STRONG | order numbers CONFIRMED; "must precede" inferred from field-dependency chain |
| `DMAQueue` discriminant `+0xA8` vs type `+0xAC` | INFERRED | both read in this pass (skip on `+0xA8==2`, stamp from `+0xAC`); a 4-byte aliasing in the walrus view of `DMAQueue` is the likely reconciliation, not byte-proven here |
| `DMAQueueAttribute` member names (`num_queues`/`sync_type`/`priority_class`) | GAP | written structurally; enumerator names unrecoverable from this binary |

**Re-verify ceiling.** The three pass entry points, worker bodies, the `findQueue` dedup chain, the `EdgeKind=3` chain constant, the DATA-only SQI insertion and its `{Q,Instance}` field stamps, and the codegen gate are all byte-anchored — these will not move. The two places a re-implementer should re-derive rather than trust verbatim are `determineEngine`'s exact engine-selection branch tree (STRONG, not every guard's predecessor traced) and the `DMAQueue +0xA8`/`+0xAC` field reconciliation (INFERRED). Nothing on this page was taken from any source other than the cp310 `libwalrus.so` disassembly and its baked `.rodata` strings.

---

## Cross-References

- [DMA Legalization — Strided / CCE / Generic-Indirect](dma-legalization.md) — the order-10/18/19 DMA legalization passes whose concrete IR `alloc_queues` later queues (the DMA-materialization stage, page 8.27).
- *DMA Engine Binding* (page 8.28, may be in-flight under `walrus/dma-engine-binding.md`) — `assign_hwdge_engine` at order 123 sets the `engine_id` (`+0x90`) and `DGEType` (`+0xF8`) that `determineEngine` consumes.
- [DMA Encoding](../isa/dma-encoding.md) — the wire encoding of `SwitchQueueInstance` (op `0xCF`) / `ResetQueueInstance` (op `0xC2`) this pass inserts, and the per-trigger queue-name lookup.
- *def.json DMA-queue attribute table* (formats, page 12.3, planned) — the serialized property-bag keys whose enumerator names are the GAP above.
