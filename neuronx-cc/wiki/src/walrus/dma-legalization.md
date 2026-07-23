# DMA Legalization — Strided / CCE / Generic-Indirect

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core logic). The three passes live in `neuronxcc/starfish/lib/libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; `.data` adds a `+0x400000` delta). The BIR instruction enums (`bir::InstructionType`) and `AccessPattern` layout live in `libBIR.so`. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

Three backend passes stand between the high-level BIR a kernel arrives in and the descriptor-legal IR the [DMA encoders](../isa/dma-encoding.md) can serialize. Each takes one over-general DMA opcode and rewrites it into a sequence the hardware DGE can actually issue:

- **`legalize_strided_dma`** (order 18, gated on the `gen-stride-dma` CLI flag) rewrites a partition-strided `InstLoad` (IT 19) into a *contiguous* DMA into a fresh SBUF tile **plus** an `InstTensorCopy` that re-applies the stride out of SBUF — but only when the materialized tile is `/32`-partition-aligned and fits the per-arch SBUF byte budget.
- **`legalize_cce_dma`** (order 19, unconditional) splits a collective-compute-engine `InstDMACopy` (IT 32) whose source count exceeds `board.device.core.MaxCceDmaSource` into a chain of `InstDMACopy`s, each consuming at most that many sources, splitting the per-source scale-constant vector alongside. This is the pass that makes every CCE DMA satisfy the verifier's `checkCCEDMALegality` gate.
- **`lower_generic_indirect`** (order 10, unconditional) expands a "generic" multi-dimensional indirect access (`GenericIndirectLoad` IT 42 / `GenericIndirectSave` IT 44) into the concrete `ReadVarAddr → TensorScalarPtr → Memset → TensorScalarPtr → IndirectLoad(43)/IndirectSave(45)` IR sequence the [indirect-gather encoders](../isa/indirect-descriptors.md) consume.

The bar for this page is that a reimplementer can reconstruct **exactly when each pass fires and exactly what it emits** — the split thresholds (`/32` alignment, SBUF byte budget, `MaxCceDmaSource`) are the decisive mechanism, and they are the headline content. What each pass does **not** do is equally important: none of them assigns a DGE queue or picks a wire descriptor. Queue/engine assignment is the later `assign_hwdge_engine` / `alloc_queues` passes; the 16B-vs-20B-vs-MX descriptor choice is made downstream at codegen from the lowered op's dimensionality and dtype. These three passes only make the IR DMA *concrete and hardware-shaped*.

This page is the **earlier** legalization stage. The later materialization (`lower_dma`, the DGE descriptor build) is a separate, planned page (8.34).

| | |
|---|---|
| **`legalize_strided_dma`** | factory `register_generator_legalize_strided_dma__` invoke `@0xb5d7b0`; `LegalizeStridedDMA::run` `@0xb5d440`; `visitInstLoad` `@0xb5ddc0` (thunk `0x606c70`); dispatches **IT 19 (Load)** |
| **`legalize_cce_dma`** | factory `register_generator_legalize_cce_dma__` invoke `@0xb60e70`; `LegalizeCCEDMA::run` `@0xb607b0`; `visitInstDMACopy` `@0xb61280` (thunk `0x614180`); dispatches **IT 32 (DMACopy)** |
| **`lower_generic_indirect`** | factory `register_generator_lower_generic_indirect__` invoke `@0xb548a0`; `LowerGenericIndirect::run` `@0xb544e0`; `codegenIndirectLoadSave` `@0xb580e0` (thunk `0x5f8dc0`); dispatches **IT 42/44 (Generic­IndirectLoad/Save)** |
| **Pass order** | 10 (`lower_generic_indirect`) · 18 (`legalize_strided_dma`, if `gen-stride-dma`) · 19 (`legalize_cce_dma`) |
| **Shared skeleton** | CRTP `IRVisitor`: `run()` builds a logger scope, visits Function `main` first then every other Function; per-BasicBlock visit recurses IT 105/106/108 (Loop / DynamicForLoop / DoWhile) and dispatches one DMA opcode |
| **Dtype→bytes table** | `qword_1DE10A0` / `_1DE1220` / `_1DE0E20` — three byte-identical 20-entry mirrors `{1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}` |

All seven body-frame addresses, the three factory invokes, and the dtype tables are read directly off the IDA disassembly and Hex-Rays decompilation of `libwalrus.so`.

---

## The shared pass skeleton

All three passes are the same shape, so it is worth pinning once. Each is a `neuronxcc::backend::BackendPass` subclass registered through a `register_generator_<name>__` factory lambda. The factory `__cxa_demangle`s the pass's own class name (e.g. `"N9neuronxcc7backend18LegalizeStridedDMAE"`), allocates the 96-byte `BackendPass` object, and seeds its `logging::Logger` with the registry string (`"legalize_strided_dma"` / `"legalize_cce_dma"` / `"lower_generic_indirect"`). The factory bodies were read end-to-end at the three invoke addresses above.

`BackendPass::run(bir::Module&)`:

```c
// run() — common to all three passes (decompiled in all three)
void run(bir::Module &M) {
  // push a logging attribute scope { "module_name" = M.getName() }
  Function *main = M.getFunction("main");        // "main" visited first
  visitFunction(main);
  for (Function *F : M.functions())
      if (F != main) visitFunction(F);
  // (legalize_cce_dma only) post-pass marker — see below
}
```

The CRTP `IRVisitor::visit` walks each `BasicBlock`, recurses into the structured-control bodies of `IT 105 Loop` / `IT 106 DynamicForLoop` / `IT 108 DoWhile`, and switches on the instruction node's IT field (node `+80`) to dispatch the **one** DMA opcode that pass owns. The three opcodes are mutually disjoint:

| Pass | IT | Instruction |
|---|---|---|
| `legalize_strided_dma` | 19 | `InstLoad` (HW-DGE DMA load) |
| `legalize_cce_dma` | 32 | `InstDMACopy` |
| `lower_generic_indirect` | 42, 44 | `InstGenericIndirectLoad`, `InstGenericIndirectSave` |

IT 43 (`IndirectLoad`) is intentionally **not** dispatched by `lower_generic_indirect` — it is already the concrete op the pass *produces*. `run()` and `IRVisitor::visit` were decompiled for all three passes, and the opcode ownership matches the `bir::InstructionType` ordinals.

---

## `legalize_strided_dma` — strided load → SBUF tile + TensorCopy

### What it owns

A partition-strided DMA load is an `InstLoad` (IT 19) whose **partition dimension is not contiguous** — i.e. the access has an inter-partition gap. The hardware DGE cannot issue such a load directly; the descriptor wants a partition-contiguous source. The pass's job is to manufacture one: stage the data into a freshly-allocated SBUF tile via a *contiguous* DMA, then re-apply the original strided pattern with an on-chip `InstTensorCopy`.

This pass runs **only** under the `gen-stride-dma` CLI gate (order 18); the other two are unconditional. Both facts come from the backend pass-order walk.

### The arch budget read

`run()` reads a per-arch SBUF-partition byte capacity from the hardware model and stows it on the visitor as the legalization budget. The dereference chain is `getArchModel(Module::getArch()) → (+8) → (+16) → (+40)` — the same `Board → Device → Core` singleton tree the [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md) page documents. The impl object the visitor carries is:

```c
// LegalizeStridedDMAImpl object layout — offsets reconstructed from run(), no named struct
struct Impl {
  void   *loggerScope;   // +0
  uint64  copyCounter;   // +8   init 0; ++'d per emitted copy for a unique decimal suffix
  uint64  sbufByteBudget;// +12  per-arch SBUF partition byte capacity
  Function *curFn;        // +16
};
```

### The rewrite

The `AccessPattern` field offsets below are the decompiler byte-offsets and match the `bir::AccessPattern` layout: dtype at `+0x30`, `MemoryLocation*` at `+0x38`, the `APPair[]` stride/num pairs at `+0x50` (each pair 16 B: `step@+0`, `num@+8`), the pattern dimension count at `+0x58` (1..4), the byte offset at `+0xD0`.

```c
// LegalizeStridedDMAImpl::visitInstLoad(bir::InstLoad&)  @0xb5ddc0
void visitInstLoad(InstLoad &I) {
  AccessPattern &inAP  = I.getArgument<PhysicalAccessPattern>(0);
  AccessPattern &outAP = I.getOutput<PhysicalAccessPattern>(0);

  // R0 — already-legal short-circuit: partition dim has no inter-partition gap
  if (inAP.isPartitionContiguous()) return;          // PLT off_3DCF5E0 → libBIR.so

  // R1 — output must be a placeable MemoryLocation
  MemoryLocation *outLoc = *(MemoryLocation**)((char*)&outAP + 56);
  assert(outLoc != nullptr);
  assert(*(int*)((char*)outLoc + 272) == 4);         // LLVM isa<> cast tag, not MemoryType

  // R2 — destination base partition must be on a /32 (DMA-quadrant) boundary
  int basePart = (outAP.offset / outLoc->elemStride) + outLoc->getBasePartition();
  if ((basePart & 0x1F) != 0) return;                // not /32-aligned → leave as-is

  // R3 — linearized element span of the input access pattern
  uint64 span = 1;
  for (int i = 1; i < inAP.num_dims; ++i)            // num_dims at inAP+0x58
      span += inAP.APPair[i].step * (inAP.APPair[i].num - 1);

  // R4 — SBUF byte-budget fit
  int dt = inAP.dtype;                               // inAP+0x30
  assert(dt <= 0x13);                                // else "Unknown dtype"
  uint64 bytes = DtypeSize[dt] * (inAP.offset_elems + span);   // DtypeSize = qword_1DE10A0
  if (bytes > this->sbufByteBudget) return;          // does not fit → leave as-is (R4 fail)

  // R5 — DECOMPOSE: contiguous DMA into a fresh SBUF tile + InstTensorCopy back out
  unsigned n = ++this->copyCounter;                  // unique decimal suffix
  MemoryLocation scratch = curFn->addMemoryLocation("<dtype>-tile");  // 1-D, span elems
  *(int*)((char*)&scratch + 216) = 16;               // MemoryType = SB (16)
  InstTensorCopy &tc = BB.insertElement<InstTensorCopy>("<orig>-copy");
  // tc input  ← copy of inAP's pattern (offset = inAP.offset)
  // tc output ← scratch SBUF location, contiguous
  // rewrite ORIGINAL load: source AP now reads the contiguous scratch tile; dst kept
  PhysicalAccessPattern::resetEvaluatedAps(inAP);
  PhysicalAccessPattern::resetEvaluatedAps(outAP);
}
```

### The three thresholds, in plain terms

| Guard | Condition to fire | Why |
|---|---|---|
| **R0 (partition gap)** | partition dim **non-contiguous** | nothing to legalize otherwise |
| **R2 (`/32` alignment)** | `basePart & 0x1F == 0` | DMA partition-quadrant granularity — the PE has 128 partitions, split into four 32-partition DMA groups |
| **R4 (budget fit)** | `DtypeSize[dt] · (offset + span) ≤ SBUF budget` | the staged tile must physically fit one SBUF partition's byte capacity |

If R2 or R4 fails, the load is left **unmodified** — deferred to the verifier / later legalization rather than rewritten. The `/32` figure is the DMA-quadrant constant (128 partitions ÷ 4); the SBUF byte budget is a per-arch immediate stamped into the `Statebuf` record (see [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md)). The rule structure and the `0x1F` mask are read directly; the runtime budget value lives in the arch-model constructor rather than in any shipped JSON, so its per-arch numeric value is [INFERRED].

The `DtypeSize` table `qword_1DE10A0` (`.rodata 0x1de10a0`, 20 qwords) is `{1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}` (byte size per Dtype 0..19).

> **NOTE — `isPartitionContiguous` lives in the other library.** `AccessPattern::isPartitionContiguous` resolves through PLT slot `off_3DCF5E0` into `libBIR.so`; its body is not in `libwalrus.so` `.text`. Its truth-semantics — `true` means the partition dim has no inter-partition gap — are read from the symbol name and the call site, not from the body.

---

## `legalize_cce_dma` — split a CCE DMACopy over `MaxCceDmaSource`

### What it owns

A collective-compute-engine DMA is an `InstDMACopy` (IT 32) for which `bir::isCCEDMA` is true. The hardware CCE engine can fan in only a bounded number of sources per copy. That bound is `board.device.core.MaxCceDmaSource` — and it is the **same field** the verifier's `checkCCEDMALegality` reads, asserting `I.num_arguments() <= MaxCceDmaSource` (NeuronAssertion code 329 — that is the *assertion code*, not the cap value). This pass exists precisely so that every CCE DMA satisfies that gate — the pass and the verifier perform the identical `ArchModel.device.core+0x50` dereference.

### The post-pass marker

After visiting all functions, `run()` iterates every `Function` and sets `FunctionAttribute #21 = "stage_legalize_cce_dma_completed"` (written through the `boost::variant` attribute map at `Function+240`). This is the pipeline-bookkeeping flag the later stages key off — part of the `stage_*_completed` attribute cluster. The attribute enum value is 21 and the string is present in `.rodata`.

### The split

```c
// LegalizeCCEDMAImpl::visitInstDMACopy(bir::InstDMACopy&)  @0xb61280  (full body read)
void visitInstDMACopy(InstDMACopy &I) {
  if (!bir::isCCEDMA(I)) return;                      // gate @0x607110
  Module &M  = *I.getFunction()->getModule();
  int archMax = ArchModel.device.core.MaxCceDmaSource;          // *(...core + 0x50)
  int nSrc    = count(I.args) + count(I.constants);
  if (archMax >= nSrc) return;                        // already legal

  // shape/dtype legality diagnostics over the source APs
  int   outDt    = outAP.dtype;
  Shape outShape = outAP.getShape();
  for (AccessPattern &inAP : I.sourceAPs()) {
      if (inAP.dtype != outDt)    reportError("Input AP dtype does not match output AP dtype");
      if (inAP.shape != outShape) reportError("Input AP shape does not match output AP shape");
  }
  bool hasConst = (outAP.argKind == 4) && (I.constants.size() != 0);  // CCE w/ immediate scalars

  // SPLIT LOOP — chunk sources into groups of <= MaxCceDmaSource
  int chunk = 0;
  while (count(I.sources) > archMax) {
      InstDMACopy *clone = I.clone();                 // vtable slot 18 (vt+0x90) clone
      clone->rename("<name>_imm_output_" + str(chunk));
      // move the first archMax source ARGUMENTS into the clone
      Instruction::spliceArguments(/*to*/clone, /*from*/&I, /*count*/archMax);
      // split the per-source constant (scale) vector alongside, when present:
      //   copy the first archMax floats to clone; erase them from I (SmallVector<float>::erase);
      //   the partial 1.0f scale (0x3F800000) is appended per chunk on the trailing remainder.
      MemoryLocation partLoc = M.getFunction(...)->addMemoryLocation("<name>_imm_output_" + str(chunk));
      *(int*)((char*)&partLoc + 224) = 7;             // partial-output kind marker = 7
      PhysicalAccessPattern partAP(clone);
      partAP.clone(outAP); partAP.offset = 0;
      partAP.cloneToArgument(&I);
      partAP.cloneToOutput(clone);
      // re-link clone into the BB list + symbol table (hash-table remove/insert)
      ++chunk;
  }
  // remainder (<= archMax sources) stays as the original InstDMACopy.
}
```

### The threshold, in plain terms

The single split condition is `nSrc > MaxCceDmaSource`, where `nSrc = #arguments + #constants`. Each emitted chunk consumes at most `MaxCceDmaSource` sources; the per-source scale-constant `SmallVector<float>` is partitioned in lockstep, and each chunk gets a freshly-minted `"<name>_imm_output_<N>"` partial-output `MemoryLocation` tagged kind 7 at field `+224`. No queue or DGE-engine assignment happens here. The full 920-line body was read; the helpers (clone via vtable slot 18 / `vt+0x90`, `spliceArguments`, `addMemoryLocation`, symbol-table relink) are identified by call site.

> **GOTCHA — two different tag fields, eight bytes apart.** Field `+224 = 7` (partial-output kind) is distinct from the `+216` `MemoryType` field that the strided pass sets to 16 (SB). `MemoryType = 16` is a named enum value; `+224 = 7` has no enum name in the binary and is [INFERRED] to be a kind tag from how it is written and read.

The exact numeric value of `MaxCceDmaSource` lives in the per-arch `EngineInfo`/`Target` constructor in `.text`, not in any shipped JSON; only the dereference path is pinned here.

---

## `lower_generic_indirect` — generic indirect → concrete IR sequence

### What it owns

The front end (penguin/HLO) emits a "generic" indirect access — `GenericIndirectLoad` (IT 42) or `GenericIndirectSave` (IT 44) — carrying a **multi-dimensional** index expression and unflattened address tensors. The hardware indirect DMA wants a single 1-D index, a 2-D address tensor, and one element/one address per partition. This pass bridges that gap, expanding the generic op into a concrete IR chain and forcing every index/offset/address AP to the hardware shape. `IRVisitor::visit` dispatches IT 42 → `codegenIndirectLoadSave` and IT 44 → `visitInstGenericIndirectSave`, a trivial forwarder (thunk `0x5ecf20`) into the same routine.

### Input invariants asserted

Before lowering, the pass asserts (these strings are present verbatim in `.rodata`):

- the instruction is `GenericIndirectLoad` (42) **or** `GenericIndirectSave` (44);
- *"Indirect dimensions must be one less than instruction argument number"*;
- *"Compiler middle end must have already flattened IndexAP to be only one AP"* — the index AP arrives already 1-D;
- *"GenericIndirectLoad/Save must have 1 element per partition for IndexAP"* (`getNumElementsPerPartition(indexAP) == 1`);
- *"indirect DMA's AddrAP should be 2D"*;
- *"Addr Tensor partition dim must match its tensor shape"*.

These exactly mirror the verifier's `checkIndirectShape` / `checkTensorIndirection` ruleset — the pass produces precisely the shape the verifier then re-checks. All six assertion strings are located in `libwalrus.so`.

### The lowering chain

```c
// LowerGenericIndirectImpl::codegenIndirectLoadSave(Instruction*, BasicBlock*)  @0xb580e0
AccessPattern codegenIndirectLoadSave(Instruction *inst, BasicBlock *bb) {
  bool load = (inst->IT == 42);
  // (a) per-indirect-dim flat byte address: for each indirect dim (outer→inner) emit an
  //     InstTensorScalarPtr "<base>_TSPAddGenericAddr" accumulating step*idx; the flat
  //     element multiplier = product of trailing dim sizes.
  // (b) InstReadVarAddr (IT 41) "<base>_ReadVarAddr": read the target DRAM tensor base
  //     var address into the 2-D AddrAP; scratch/dst memloc tagged "<base>_DstTensor".
  // (c) element size: dt = DstTensorMemLocSet.getDtype(); assert dt <= 0x13;
  //     elemBytes = DtypeSize[dt]  (table qword_1DE0E20, identical mirror).
  //     If the target DRAM tensor is flattened to 1-D, IndirectDims must also be 1-D
  //     ("target DRAM tensor is flattened, IndirectDims must be expanded...").
  // (d) InstTensorScalarPtr "<base>_TSPAddAddr": add the byte address (immediate ptr-type
  //     Argument, setType 15) to the AddrAP. op-mode fields 240=6 / 288=4.
  // (e) InstMemset "<base>_MemsetTileOffset": zero a per-tile offset tensor (immediate
  //     setType 14); "_DstTensor" memloc, dims updated to 4-wide.
  // (f) InstTensorScalarPtr "<base>_TSPAddOffset": add the tile offset (op-mode 240=4).
  // (g) FINAL concrete indirect op:
  if (load) {
    InstIndirectLoad &out = emit<InstIndirectLoad>("<base>_IndirectLoad");   // IT 43
    addMemoryLocation("<base>_ScratchSpaceTensor");                          // 1-wide dim
    // args = { addr-result AP, original index AP, offset AP }; output cloned from inst output
  } else {
    InstIndirectSave &out = emit<InstIndirectSave>("<base>_IndirectSave");   // IT 45
    // args = { offset-result AP, addr AP, output AP }; output ← inst arg
  }
  return sub_B54210(finalAP);   // finalize the surviving AP for load vs save
}
```

So the generic op (IT 42/44) becomes, in order: **`ReadVarAddr`(41) → per-dim `TensorScalarPtr` address accumulation → `Memset` tile-offset → `TensorScalarPtr` offset add → `IndirectLoad`(43) / `IndirectSave`(45)**, with the index, offset, and address APs all forced to "1 element / 1 address per partition, contiguous data". The impl also carries a worklist `SmallVector` (`this+1` begin / `this+4` size / `this+5` cap) to which `codegenIndirectLoadSave` appends `inst` before lowering; those three slots are read off the access pattern in `run()`, not from a named field.

### Descriptor selection is downstream — not here

This pass emits the IR-level concrete ops `IndirectLoad` (43) and `IndirectSave` (45). It does **not** choose the wire descriptor. The encoder picks among `INDIRECT16B` (3-D AP, `assignAccess3D`), `INDIRECT20B` (4-D AP, `assignAccess4D`), and `MXINDIRECT16B` (MX scale present, `assignIndirectPatternForMX`) downstream at codegen, from the lowered op's AP dimensionality, dtype, and DGE type — see [Indirect-Gather Descriptors](../isa/indirect-descriptors.md) and the [IndirectLoad/Save codegen op `0xC4`/`0xD6`](../isa/dma-encoding.md). `lower_generic_indirect`'s entire job is to make the IR indirect DMA *concrete and hardware-shaped*; the 16B-vs-20B (dim count) and MX (scale) selection happens after. The pass body is read as producing 43/45; the descriptor encoders live in codegen and are not inspected here.

The `DtypeSize` table this pass reads, `qword_1DE0E20` (`.rodata`, 20 qwords), is the byte-identical mirror `{1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}`.

---

## How the three relate

| | `legalize_strided_dma` | `legalize_cce_dma` | `lower_generic_indirect` |
|---|---|---|---|
| **Order / gate** | 18 / `gen-stride-dma` | 19 / unconditional | 10 / unconditional |
| **Opcode in** | IT 19 `InstLoad` | IT 32 `InstDMACopy` | IT 42/44 `GenericIndirect{Load,Save}` |
| **Trigger** | partition non-contiguous **and** `/32`-aligned **and** fits SBUF budget | `nSrc > MaxCceDmaSource` | always (generic op present) |
| **Emits** | contiguous DMA → SBUF tile (`MemoryType`=16) + `InstTensorCopy` ("-copy") | chain of `InstDMACopy` (≤cap each) + split scale vector + `_imm_output_<N>` partials (kind 7) | `ReadVarAddr`→`TSP`→`Memset`→`TSP`→`IndirectLoad`(43)/`IndirectSave`(45) |
| **HW limit read** | SBUF byte budget (`core +40` chain) | `MaxCceDmaSource` (`core +0x50`, == verifier code-329 field) | none (shape-only) |
| **Side marker** | — | `FunctionAttribute #21 stage_legalize_cce_dma_completed` | worklist `SmallVector` |

Three observations tie the set together. **Opcode ownership is disjoint** — strided owns Load (19), cce owns DMACopy (32), generic-indirect owns GenericIndirect (42/44) — so no DMA op is touched by two of them. **All three honor the same arch-model root** `getArchModel() → Board+0x8 → Device+0x10 → Core` for their HW limits (strided reads the SBUF byte budget; cce reads `MaxCceDmaSource`; generic-indirect reads only the dtype tables). And **none assigns a queue or DGE engine** — `bir::DGEType` stays `Unassigned(3)` from the `InstDMA` ctor; HWDGE-engine and queue assignment are the later `assign_hwdge_engine` / `alloc_queues` passes.

---

## Evidence summary

| Claim | Evidence | Confidence |
|---|---|---|
| The three-pass set and their order (10 / 18 / 19); order 18 gated on `gen-stride-dma` | the backend pass-order walk | CERTAIN |
| Every body-frame and thunk address; the three factory invokes | IDA disassembly + Hex-Rays of `libwalrus.so` | CERTAIN |
| Opcode ownership (IT 19 / 32 / 42-44), disjoint across the three passes | `run()` + `IRVisitor::visit` decompiled for all three | CERTAIN |
| The `/32` (`0x1F`) strided alignment mask | `visitInstLoad` body @ `0xb5ddc0` | CERTAIN |
| The `nSrc > MaxCceDmaSource` split condition and the full split body | 920-line `visitInstDMACopy` body @ `0xb61280` | CERTAIN |
| The generic-indirect lowering chain, its named IR ops and string literals | `codegenIndirectLoadSave` @ `0xb580e0`; strings in `.rodata` | CERTAIN |
| `FunctionAttribute #21` and the three byte-identical `DtypeSize` mirrors | attribute enum + `.rodata` tables | CERTAIN |
| The strided `Impl` object layout | offset wiring reconstructed from `run()`; no named struct | HIGH |
| `isPartitionContiguous` semantics | cross-library PLT: symbol name + call site, body not read | HIGH |
| The generic-indirect worklist slots | read off the access pattern in `run()` | HIGH |
| Descriptor selection happens downstream | the pass body produces IT 43/45; the encoders are codegen-resident | HIGH |

### Limits of this reading

Two numeric values that the thresholds turn on are not recoverable here: the per-arch SBUF byte budget and `board.device.core.MaxCceDmaSource`. Both are stamped into `EngineInfo`/`Target` constructors in `.text` rather than any shipped JSON, so only the dereference paths are pinned. Separately, the `+224 = 7` partial-output value in the CCE pass is an observed tag with no enum name in the binary.

---

## Cross-References

- [2.21 DMA-Family Encoding & Descriptors](../isa/dma-encoding.md) — the descriptor wire format these passes legalize *toward*; the IndirectLoad/Save codegen ops (`0xC4`/`0xD6`) that consume the IT 43/45 this page produces.
- [2.07 Indirect-Gather Descriptors](../isa/indirect-descriptors.md) — `INDIRECT16B` / `INDIRECT20B` / `MXINDIRECT16B`, the downstream descriptor the generic-indirect lowering feeds; the 16B-vs-20B-vs-MX selection this page defers.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the `Board → Device → Core` arch-model tree and the per-arch SBUF byte budget the strided pass tests against.
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where these three passes sit in the overall backend order.
- **8.34 `lower_dma` (DGE materialization) — *planned*** — the *later* pass that materializes the legalized DMA into concrete DGE descriptors; this page is the earlier legalization stage.
