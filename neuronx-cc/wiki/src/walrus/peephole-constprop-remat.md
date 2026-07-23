# Mid-Pipeline Cleanup: Peephole, Constant-Propagate, Remat, Redundancy

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310 build), in `neuronxcc/starfish/lib/libwalrus.so`. `.text` (0x62d660+) and `.rodata` (0x1c72000+) are VA==file-offset; `.data` carries a +0x400000 VA−file delta. The 0x5e9020–0x62d650 band is the intra-library `.plt` JMP-thunk region — every pass symbol has a 6-byte forwarder there; the real bodies live in the high 0xbc/0xbd/0xbe/0x107/0x159/0x15a/0x15b/0x9e bands and are what this page transcribes.*

## Abstract

After the Penguin/NKI front-end lowers a graph into BIR and `bir_linker` merges modules, the walrus backend runs roughly a hundred numbered passes (see [The Walrus Pass Pipeline & the Optlevel Planes](pass-pipeline-optlevels.md)). This page documents the **mid-pipeline cleanup and optimization cluster** that sits around the SRAM/PSUM allocation boundary: a three-point *peephole* engine, the *constant-propagate* partial evaluator, the matmul-transpose *rematerializer*, the *redundancy* (dead-store) eliminator, the backend *tensor-copy elimination* pass, and SBUF *weight pinning* inside the coloring allocator. These are the passes that shrink the IR the scheduler and allocators must process, fold compile-time-known tensors, relieve SBUF pressure, and kill writes that are overwritten before they are read.

The cluster is deliberately *not* one monolithic "algebraic rewrite suite," and its most important structural property is this: the classic identities a reader expects from a peephole pass (add-0, mul-1, select-of-constant-mask, zero propagation, iota evaluation) do **not** live in the peephole passes at all. They live in `ConstantPropagate`, whose visitor **derives from the BIR functional simulator `birsim::InstVisitor`** and folds constants by *literally executing the module into a simulated memory* — see [BIR Simulator: Dispatch & Whole-Machine State](../bir/sim-dispatch-state.md). The "lattice" is the simulator's value store; "folding" is recognising that a simulated op produced a known tensor. That is the headline quirk of the entire cluster.

The matcher infrastructure shared by the peephole passes is a fixed `bir::IRVisitor<Derived>` CRTP switch on the `InstructionType` field (`Instruction+0x58`), single-shot per pass, with no module-level fixpoint; dead remnants (memsets, reduces, copies) are left for the separate `dead_code_elim` passes to reap. The allocation boundary organises the whole cluster: passes that change an op's engine, dataflow shape, or buffer set run *before* scheduling/allocation; passes that need physical access patterns, final dtypes, or assigned engines run *after* it.

For reimplementation, the contract is:

- The three-point peephole engine (`pre_opts` / `early_peephole_opts` / `peephole_opts`), each a fixed `IRVisitor` switch over a small handful of `InstructionType` cases, and *why* each sits early, late, or modular-only.
- The birsim-backed `ConstantPropagate`: the visitor-is-a-simulator architecture, the per-`MemoryLocation` value-store lattice, the alias-safe availability test, the static pre-passes, and the fixpoint sim loop.
- The matmul-transpose `RematOpt`: the `[GenericCopy→Matmult→Load]` template match, the `recompute_cost ≥ 2·latency` cost gate, the SSA/dominance legality window, and the clone+reload rewrite.
- `remove_redundancies` as a *dead-store* (clobbered-write) eliminator (not a load/memset remover), and the `tensor_copy_elim` copy-propagator that runs twice across the allocation boundary.
- SBUF weight pinning: the rematable-load candidate predicate, the policy-flag gate (not a cost model), preallocated-node insertion into the interference graph, and the unpin-on-pressure fallback.

| | |
|---|---|
| **Binary** | `libwalrus.so` (cp310), BuildID 92b4d331… |
| **Peephole entry points** | `PreOpts::run` 0x16069c0 · `EarlyPeepholeOpts::run` 0xbf81c0 · `PeepholeOpts::run` 0xbea260 |
| **ConstantPropagate** | `run` 0xbd8930 · visitor ctor 0xbe0130 · `enterInstruction` 0xbe3080 |
| **RematOpt** | `run` 0x1078010 (pass #30) |
| **remove_redundancies** | `RemoveRedundancies::run` 0x15a4ce0 → free-fn 0x15a48b0 (pass #75) |
| **tensor_copy_elim** | `TensorCopyElim::run` 0x15bb4f0 → worker 0x15a9a10 (passes #34, #77) |
| **SB pinning** | `find_loads` 0x9e99f0 · `is_rematable_load` 0x9e8180 |
| **IR level** | BIR — `bir::Module`/`Function`/`BasicBlock`/`Instruction`, `InstructionType` @+0x58 |
| **Matcher** | `bir::IRVisitor<Derived,void>` CRTP switch on `Inst+0x58`; single-shot, no fixpoint |

---

## The Peephole Trio

### Purpose

Three passes share the `*_opts` naming convention but are *three different, narrowly-scoped* passes, not one suite. `pre_opts` (order 20) is a modular-compilation boundary cleanup; `early_peephole_opts` (order 31, pre-allocation) is an engine-fusion group; `peephole_opts` (orders 58–59, post-allocation) is an engine-routing group. Each runs a fixed `IRVisitor` switch over a small handful of `InstructionType` cases. None of them perform DCE — they leave dead memsets/copies for the downstream `dead_code_elim_o0/o1` passes — objdump of both peephole `run` bodies shows zero calls into DCE, ConstantPropagate, RemoveRedundancies, or TensorCopyElim.

> **QUIRK —** the classic algebraic identities live in `ConstantPropagate`, not here. Neither `proxyreg` nor `taginv` appears as a string or symbol anywhere in `libwalrus.so`; the nearest real construct is `SeqInstOpt` (a separate sequencer pass that folds redundant `InstRegisterMove` ops, `visit` @0x155e980), unrelated to this trio.

### Entry Point

```text
PreOpts::run                0x16069c0   ── order 20, modular-only boundary cleanup
  └─ RemoveBoundaryCcBuffer::run   0x1609cc0
EarlyPeepholeOpts::run      0xbf81c0    ── order 31, pre-allocation engine fusion
  ├─ SplitSelectVisitor::visitInstSelect            0xbf1b30
  ├─ ActAcc::visitInstTensorReduce                  0xbf6c60
  └─ SimplifyPermuteImplicitReduceAdd::visit        0xbf6010
PeepholeOpts::run           0xbea260    ── orders 58-59, post-allocation engine routing
  ├─ SimplifyMemset::run                            0xbe94a0
  └─ MoveToAct (IRVisitor<MoveToAct>::visit)        0xc4e680
```

### Algorithm

`pre_opts` is a no-op outside modular compilation. Its real work removes cross-module collective-communication staging buffers whose only access is a DMA copy:

```c
function PreOpts_run(Module& M):                     // 0x16069c0
    if !enablePreOpts: return                        // global @0x3df79e0, CLI "enable-pre-opts"
    if !is_modular_compilation(M):                   // logs "Not modular compilation"
        return                                       // → no-op for single-module compiles
    flow = ModularFlowInfo::processNetlist()         // 0x5f6590  cross-module dataflow
    RemoveBoundaryCcBuffer_run(flow)                 // 0x1609cc0

function RemoveBoundaryCcBuffer_run(...):            // 0x1609cc0
    for each boundary cc MemoryLocation L:
        // both helpers must return false: only access is a DMA copy
        if !hasNonDmaCopyReader(L) and !hasNonDmaCopyWriter(L):  // 0x16092a0 / 0x16087c0
            delete L and its boundary DMA copies; rewire the two sides directly
```

`early_peephole_opts` runs three engine-fusion rules in fixed order, each a pre-allocation transform that changes an op's engine assignment, dataflow shape, or buffer set:

```c
function EarlyPeepholeOpts_run(Module& M):           // 0xbf81c0
    fn = M.getFunctionByName("main")

    // (1) SplitSelect — produce the predicated-copy form the lowerer expects
    for each InstSelect s (IT 51) in fn:             // visitInstSelect 0xbf1b30
        if isScalar(value_operand) and checkImmValueTypeCompatible(dt,dt):
            replace s with InstGenericCopy(else-value)        // cloneToOutput
                       + InstCopyPredicated(true-value, mask) // insertElement

    // (2) ActAcc — fold Activation+TensorReduce(add) onto the HW accumulator
    for each InstTensorReduce r (IT 27) in fn:        // visitInstTensorReduce 0xbf6c60
        if r.AluOp@+0xf0 == 4 /*add*/                          // single source...
           and writer_of(r.src) is InstActivation (IT 4)      // getWritersFromBB 0x629070
           and doAccessesOverlap and sameAccessPatterns:      // 0x5edc50 / 0x628560
            drop r; set Activation to use the PE/Act built-in accumulator;
            insert InstReadActivationAccumulator (IT 5)        // 0x61e4b0
    // batched drops applied in removeInstructions() 0xbf33b0

    // (3) SimplifyPermuteImplicitReduceAdd — drop a zero-init memset
    for each permute/transpose op with an implicit reduce-add accumulator:  // 0xbf6010
        if producer(init) is Memset (IT 0xA) and isFullyZeroed(AP):  // 0xbf5660
            Instruction::removeArgument(zero_init_arg)   // HW auto-zeroes; memset → dead
```

`peephole_opts` runs after allocation/scheduling and touches *physical* access patterns and physical memory-location dtypes. It rewrites Memset-fed scalars to immediates, then folds standalone ops onto the Activation engine:

```c
function PeepholeOpts_run(Module& M):                // 0xbea260
    SimplifyMemset_run(M)                            // 0xbe94a0
    MoveToAct(M)                                     // IRVisitor<MoveToAct>::visit 0xc4e680

function SimplifyMemset_visit(inst):                 // {visitInstSelect 0xbe9420,
                                                     //  visitInstCopyPredicated 0xbe9480}
    // inst is Select (IT 51) or CopyPredicated (IT 52)
    if producer(operand) is Memset (IT 0xA)
       and isScalar(value) and memloc_dtype@+0x110 != 4 and operand_kind@+0x18 != 6:
        imm = bir::ImmediateValue(...)               // 0x609630
        ReplaceMemsetToScalar(operand, imm)          // 0xbe8e90; feeding Memset → dead

function MoveToAct_visit(inst):                       // 0xc4e680, dispatch on IT @+0x58
    switch inst.type:
      case 21 /*Reciprocal*/: if enable@+0xe and canMove(inst) /*0xbe7dc0*/:
            setupActivation(inst, ActFnType=0x19 /*25 reciprocal*/)   // "Recip:"
      case 23 /*TensorCopy*/: if enable@+0xc and canMove(inst) /*0xbe7d20*/:
            setupActivation(inst, ActFnType=0x17 /*23 copy/identity dtype-cast*/) // "Tc:"
      case 29 /*TensorScalarPtr*/: if enable@+0xd and canMove(inst,&bias,&scale) /*0xbe8750*/:
            replaceInstWithInstArr<InstActivation>(inst)  // 0x5ea320; affine in act pipe "Tsp:"
```

`canMove(Reciprocal)` requires the in/out AccessPattern element-type field (@+0x30) == 16, `getNumElementsPerPartition() ≤ 63`, and a single fusible consumer; `canMove(TensorCopy)` checks the dtype-converting/identity-copy shape the Act engine can absorb.

### Rule Catalog

| Rule | Match (InstructionType + operand condition) | Rewrite | Phase | Body | Confidence |
|---|---|---|---|---|---|
| `RemoveBoundaryCcBuffer` | boundary cc `MemoryLocation`, only reader AND writer is a DMA-copy | delete buffer + boundary DMA copies, rewire directly | pre_opts (20, modular-only) | 0x1609cc0 | CERTAIN |
| `SplitSelect` | Select(51), scalar/imm value operand, type-compatible | → GenericCopy(else) + CopyPredicated(true) | early (31) | 0xbf1b30 | CERTAIN |
| `ActAcc` | TensorReduce(27, AluOp@+0xf0==4 add) reading exactly an Activation(4) output | drop reduce; HW accumulator; insert ReadActivationAccumulator(5) | early (31) | 0xbf6c60 | CERTAIN |
| `SimplifyPermuteImplicitReduceAdd` | permute/transpose with implicit reduce-add init'd by a fully-zero Memset(0xA) | `removeArgument`(zero-init); memset → dead | early (31) | 0xbf6010 | CERTAIN |
| `SimplifyMemset` | Select(51)/CopyPredicated(52) operand from Memset(0xA), scalar, dtype@+0x110≠4 | replace operand with `ImmediateValue`; memset → dead | late (58–59) | 0xbe9420/0xbe9480 | CERTAIN |
| `MoveToAct(Recip)` | Reciprocal(21), AP dtype@+0x30==16, NumElems≤63, single use | InstActivation, ActFn=25 | late (58–59) | 0xbea1f0 | CERTAIN |
| `MoveToAct(TensorCopy)` | TensorCopy(23), dtype-converting/identity | InstActivation, ActFn=23 | late (58–59) | 0xbea180 | CERTAIN |
| `MoveToAct(TensorScalarPtr)` | TensorScalarPtr(29), bias+scale map to Act scale/bias | replaceInstWithInstArr<InstActivation> | late (58–59) | 0xbe95a0 | CERTAIN |

### Considerations

The early/late split is organised purely around the SRAM/PSUM/register **allocation + scheduling boundary** (orders ~33–57). EARLY (31) rules *change* an op's engine, dataflow shape, or buffer set and must run before the scheduler serializes and the allocators lay out addresses — `ActAcc` deletes a reduce buffer, `SplitSelect` creates new ops, `PermuteImplicitReduceAdd` deletes an init dependency. LATE (58–59) rules need *physical* access patterns and final dtypes — `SimplifyMemset` must prove a memset covers exactly a consumed scalar region; `MoveToAct` coalesces ops the scheduler already placed adjacent on the activation engine. `pre_opts` (20) is on a different axis entirely: modular-link cleanup run right after module merging.

> **GOTCHA —** the trio runs each rule as a *single-shot* `IRVisitor` walk per pass — there is no re-run-until-stable wrapper. A rule that consumes a producer walks only that producer's use-list (the `llvm::DenseMap<Instruction*,int>` use-counts in `canMove`/`ReplaceMemsetToScalar`) to check safety; it does not re-queue the function. A reimplementation that expects a worklist-to-fixpoint will mis-model cascades — cascades are handled by the *next* pass in the pipeline, not by re-iterating this one.

---

## ConstantPropagate — the birsim-Backed Partial Evaluator

### Purpose

`constant_propagate` (registered name `"constant_propagate"` @0x1c77112; pass order 26) is the real home of every classic identity: add-0, mul-1, select-of-constant-mask, zero propagation, iota evaluation, dtype-change folding. But it is **not** a classic abstract-interpretation sparse-lattice pass. `ConstantPropagateVisitor` *derives from* the BIR functional simulator `birsim::InstVisitor` — the same visitor the bir_sim simulator uses — and it **functionally executes the module into a simulated `birsim::Memory`**. An instruction's output is "constant" iff the simulator could compute it: all its source `MemoryLocation`s already hold concrete bytes and no aliasing writer invalidated them. "Folding" is recognising that a simulated op's output is a known compile-time tensor (all-zero, identity-element, uniform mask) and rewriting the BIR op to a cheaper form.

> **QUIRK —** the constant lattice is the *simulator's value store*. Seeding is by execution: a `Memset` materialises its literal fill, an `Iota` materialises its affine ramp, an `ImmediateValue` operand contributes its scalar, and *any* op all of whose inputs are already constant materialises its output transitively — the fold is "free" because the sim already ran the op. There is no separate constant-lattice transfer function; the transfer function *is* the hardware-faithful simulator. Reimplementing CP means reimplementing (or reusing) the BIR simulator. See [BIR Simulator: Dispatch & Whole-Machine State](../bir/sim-dispatch-state.md).

### Entry Point

```text
ConstantPropagate::run            0xbd8930   ── pass driver (the 0x5e* alias is a PLT thunk)
  ├─ constant_propagate_for_select        0xbd4bf0   ── static pre-pass A
  ├─ constant_propagate_for_AffineSelect  0xbd6be0   ── static pre-pass B (iota_evaluation)
  ├─ constant_propagate_for_memset0       0xbd5fe0   ── static pre-pass C (propagate_zero)
  └─ ConstantPropagateVisitor  (sim loop)
       ├─ ctor(birsim::Memory*, name, birsim::Config, PassOptions&)   0xbe0130
       ├─ visit(Module,bool) → visit(Function) → visit(BasicBlock)    0xbe3b10..
       ├─ enterInstruction  (110-case dispatch)                       0xbe3080
       ├─ skipComputation 0xbe1bc0 / force_visit 0xbdefa0 / valid_to_use 0xbe1270
       └─ 6 fold detectors (visitInst{GenericCopy,Matmult,TensorReduce,
                             TensorScalarPtr,TensorTensor,Select})
```

### Algorithm

The driver runs three static pre-passes (no simulation), then a fixpoint sim loop:

```c
function ConstantPropagate_run(Module& M):           // 0xbd8930
    // (1) STATIC PRE-PASSES — each returns #removed; if >0, run dce(M)
    if constant_propagate_for_select(M) > 0:        dce(M)   // 0xbd4bf0
    if constant_propagate_for_AffineSelect(M) > 0:  dce(M)   // 0xbd6be0
    if enable_memset0 /*LOBYTE(dword_3DFFD88[12])*/:
        if constant_propagate_for_memset0(M) > 0:   dce(M)   // 0xbd5fe0

    // (2) SIM LOOP — gated by cl::opt "enable-sim-based-constant-propagate"
    if LOBYTE(dword_3DFFCC8[12]):
        for iteration = 0, 1, 2, ...:
            mem = new birsim::Memory(M.getArtifactAbsPath())   // fresh per round
            V   = ConstantPropagateVisitor(mem, ..., PassOptions)
            V.visit(M, build_flow_graph = (iteration == 0))    // SIMULATE whole module
            for (inst,b)        in V.SELECT_set:  simplify_select(M,inst,b,b!=1,&n)
            for (inst,fill)     in V.MEMSET_set:  replace_with_memset(M,inst,fill); remove(inst)
            for (inst,operandI) in V.REWIRE_set:  replace_with_tensorCopy(M,inst,loc,pap); remove(inst)
            dce(M)
            if MEMSET_set was empty this round: break          // fixpoint
```

The per-instruction simulation protocol decides whether to execute an op and which fold detector (if any) inspects the result:

```c
function enterInstruction(Instruction& inst):        // 0xbe3080 (disasm; decomp-fail)
    skip   = skipComputation(inst)                   // 0xbe1bc0
    forced = force_visit(inst)                        // 0xbdefa0
    if forced or !skip:
        birsim::InstVisitor::enterInstruction(inst)  // << EXECUTE op into sim Memory
        switch inst.type@+0x58 {                      // 110 cases
            // 6 cases → ConstantPropagateVisitor::visitInst<X>  (FOLD detectors)
            // ~104   → birsim::InstVisitor::visitInst<X>        (pure simulation step)
        }
    // else: not simulated; output stays UNKNOWN in the lattice

function skipComputation(inst):                       // 0xbe1bc0
    v = inst.type
    if v==33 /*GPSIMDSB2SB*/ or (v-47 <=u 3) /*Collective {47,48,49,50}*/: return SKIP
    return sub_BE1690(inst)   // skip unless every input PAP is valid_to_use (a known constant)

function force_visit(inst):                            // 0xbdefa0
    if inst.type==51 /*Select*/ or inst.type==31 /*TensorTensor*/: return true  // ALWAYS fold
    if inst.type==8  /*Matmult*/: return inst[+440]   // iff "has a fully-constant operand"
    return false
```

`Select` and `TensorTensor` are force-visited because their fold (uniform-mask / identity-element / absorbing-zero) is worth running even when a generic skip would bail.

### The Lattice and Availability

The value store at `visitor+0x138` is the lattice:

```c
DenseMap<const bir::MemoryLocation*,
         std::pair<std::shared_ptr<birsim::MemoryObject>, unsigned /*generation*/>>
```

Granularity is whole-tensor / per-`MemoryLocation` (not per-scalar SSA). `valid_to_use(PAP)` (0xbe1270) is the meet/availability test and the alias-safety rule: look up the PAP's `MemoryLocation` (PAP+0x38, require StorageBase kind==4); for every writer whose access *overlaps* (`doAccessesOverlap`), require that writer be in the produced-constant set. **Any non-constant overlapping writer ⇒ not usable as a constant.** A non-zero `const_sub_tensor_id` (`MemoryLocation`+512) also blocks folding (the tensor is a sub-tile of a larger constant whose other parts may not be const).

### The Six Fold Detectors

The detectors *record candidates* into three `DenseMap`s during the walk; `run()` drains them afterwards (only `Select` simplifies inline):

| Detector | Body | Constant condition | Records into |
|---|---|---|---|
| `visitInstGenericCopy` | 0xbe0620 | src all elements == 0.0f | MEMSET set (fill 0) |
| `visitInstMatmult` | 0xbe1c30 | a constant operand all == 0.0f (gated on flag +440) | MEMSET set (fill 0) |
| `visitInstTensorTensor` | 0xbe2190 | AluOp ∈ {10 bitwise_and, 11 bitwise_or}; const operand all-0 (absorb) or identity-0 (OR) | MEMSET set (absorb) / REWIRE set (keep other) |
| `visitInstSelect` | 0xbe26f0 | mask (operand 0) uniformly true or uniformly false | SELECT set (bool) |
| `visitInstTensorScalarPtr` | 0xbe1bf0 | (skip-guard then forward to sim) | — |
| `visitInstTensorReduce` | 0xbdefe0 | (pure forward to sim) | — |

> **GOTCHA —** the `Select` mask scan asserts every element is 0 or 1 (`constant_propagate_visitor.cpp:312` "mask must be boolean"); a non-boolean mask is a hard error, not a missed fold. The `TensorTensor` detector only fires for `bitwise_and`/`bitwise_or` — arithmetic identities (add-0, mul-1) are folded by the *simulator executing the op*, not by this detector.

### The Rewrites

| Rewrite | Body | Effect |
|---|---|---|
| `replace_with_memset` | 0xbd3be0 | insert `InstMemset` named `<orig>_constant_propagate_memset`, fill = recognised constant; re-attach orig's dep edges; `run()` removes orig |
| `replace_with_tensorCopy` | 0xbd3ee0 | insert `InstGenericCopy` named `<orig>_ConstantPropagate_copy` whose source = surviving constant operand; `rewireDeps` re-points consumers |
| `replace_with_rewire` | 0xbd3730 | zero-copy version — directly rewires consumers to the surviving operand's location/AP (gated on layout compatibility: same MemoryType class, same dtype, addr-space ≤1, partition-contiguous) |
| `simplify_select` | 0xbd41d0 | pick surviving operand; tensor → rewire/tensorCopy, `ImmediateValue` → `replace_with_memset` |

`simplify_select`/`AffineSelect` always try `replace_with_rewire` first and fall back to `replace_with_tensorCopy` if rewire returns 0.

### Considerations

The static pre-passes exist to catch the easy structural cases without paying for simulation. `constant_propagate_for_AffineSelect` reasons *symbolically* on iota ramps: `iota_evaluation` (0xbd0370) computes the closed-form min/max of an affine ramp (base + Σ (count−1)·step over the AP) and, if the whole ramp lies one side of the select threshold, the select is uniformly one branch. `constant_propagate_for_memset0` flood-fills "all-zero region" through the def→use graph via `propagate_zero` (0xbd5230) using `boost::icl` byte-interval sets, recognising zero-preserving structure (TensorScalar-add with a zero operand, Matmult with a zero operand).

Every fold round is followed by `backend::dce` — CP only *inserts* memset/copy and detaches the original; dead originals and now-unused constant producers are reclaimed by DCE, not by CP itself. `constant_propagate_for_dataType_change` (0xbd9c00, the largest helper at 9183 B) **exists but is not called from `run()`** in this build (INFERRED: dead/feature-flagged, or a sibling entry for a precision/dtype-rewrite mode).

---

## RematOpt — Matmul-Transpose Rematerialization

### Purpose

`RematOpt` (pass #30) is **not** a classic "recompute a cheap op at the use" pass. As shipped in 2.24 it is a **spill-and-reload deduplicator specialised for the matmul-transpose DRAM round-trip**. It finds SBUF (`MemoryType` 16) values produced by a `[GenericCopy→Matmult→Load]` recompute chain whose recompute cost exceeds 2× the value's memory latency, proves the chain is single-source and bottoms out at a live DRAM anchor, then **removes the redundant in-SBUF recompute block and replaces the consumer's read with a fresh `InstLoad` reload** of a cloned DRAM `MemoryLocation` (`_RematSpillSave` / `_Reload` naming). The transform shortens the SBUF live range, relieving SB pressure.

> **GOTCHA —** the whole pass is **disabled by default**. It runs only under `--enable-mm-transpose-remat-optimization`; when off, `run` logs "MM transpose remat optimization disabled" and returns without touching the module. A reimplementer measuring "what RematOpt does" on a default compile sees nothing.

### Entry Point

```text
RematOpt::run                 0x1078010   ── pass #30 (after input_dma_coalescing #29, before pre_sched #33)
  ├─ build_ids                0x10716d0   ── inst flat order @+0x4C, memloc id @+0x15C
  ├─ label_first_write        0x10720a0   ── single-def map (SSA precondition)
  ├─ pick_initial_remat_chain 0x10755d0   ── candidate select + cost gate
  │    └─ match_parent_inst_type 0x1074f80   ── typed chain walk + Σ cost
  ├─ filter_out_non_ssa       0x1073e30   ── dominance/liveness window
  └─ remove_remat_chain       0x1075d10   ── CLONE + reload + erase
```

### Algorithm

```c
function RematOpt_run(Module& M):                    // 0x1078010
    if !unk_3E01A58 /*enable-mm-transpose-remat-optimization*/:
        log("MM transpose remat optimization disabled"); return
    for each Function fn in M:
        clean_up(); build_ids(fn); label_first_write(fn)
        tmpl = [InstGenericCopy, InstMatmult /*transpose variant cleared*/, InstLoad]
        pick_initial_remat_chain(fn, /*space=*/16 /*SB*/, tmpl, chainMap)  // 0x10755d0
        filter_out_non_ssa(chainMap)                                       // 0x1073e30
        remove_remat_chain(chainMap)                                       // 0x1075d10

function pick_initial_remat_chain(fn, space, tmpl, out):   // 0x10755d0
    for each MemoryLocation ml in fn.allocs():
        if !isSingleBBAccessed(ml): continue           // long live range in ONE BB (pressure)
        if space != ml.MemoryType@+216: continue       // SB only (MemoryLocation+0xd8 → type word)
        threshold = 2.0 * MemoryLocation::getLatency(ml)   // DMA round-trip proxy (×2)
        cost = 0
        matched = match_parent_inst_type(fn, ml, tmpl, depth=0, &cost)  // 0x1074f80
        // ACCEPT iff: recompute cost >= 2*latency  AND matched  AND <=1 terminal memloc
        if matched and cost >= threshold and MLout.size_bytes <= 8:
            out[ source_memloc_id ].push({dst=MLout[0], producer=ml, papList})

function match_parent_inst_type(this, ml, tmpl, depth, &cost):   // 0x1074f80
    if depth == tmpl.size(): mlOut.push(ml); return TRUE   // terminal source
    producer = writer_of(ml)
    if !same_inst_type0(tmpl[depth], producer): return FALSE  // opcode (+matmul transpose flag) gate
    cost += producer->vtable[0x68]()                       // InstProfiler per-op cost
    for each source PAP of producer:
        if ignore_src_arg(pap): continue                   // skip the DRAM transposed matmul input
        recurse match_parent_inst_type(..., src_ml, depth+1, &cost)
```

The cost model is purely additive over the recompute chain: `recompute_cost = Σ (producer cost @vtable+0x68)`; the keep/spill cost is modelled as `2 × MemoryLocation::getLatency()`. There are no inline magic constants beyond the literal factor 2 and the single-source cap (≤1 terminal memloc). `ignore_src_arg` (0x1072f30) skips the DRAM (MemoryType 8) transposed-input feeding a matmul — that DRAM transposed weight is the *leaf* of the recompute, treated as a free live anchor. `same_inst_type0` (0x10703b0) additionally requires two Matmults to have the *same* transpose flag (+440) — which is exactly why the feature is named "mm-transpose-remat."

### Legality and Rewrite

`filter_out_non_ssa` (0x1073e30) is the structural SSA/dominance/liveness guard: for each chain, gather the source-AP use window `[imin, imax]` over inst flat order (+0x4C), and accept iff the chain producer's single writer flat-order `w` satisfies `imin ≤ w ≤ imax` — the definition dominates all uses and the inputs are still live at the reload site. Any chain without a single dominating writer is erased.

`remove_remat_chain` (0x1075d10) is the transform: locate the last write of the original tensor; clone a fresh DRAM `MemoryLocation` named `<v>_RematSpillSave` (the writer is cast to `InstSave`, opcode 22); insert a new `InstLoad` named `<v>_Reload` at the use site; bind fresh `PhysicalAccessPattern`s (`setLocation`/`setType`/`setOffset(0)`/`setPattern`) so the reload reads the cloned DRAM memloc into a fresh SBUF slot; then `removeInstruction` the redundant remat-writer block. A per-pass counter at `this+232` tracks clones. Structural dedup (`equal_producer`/`equal_mem`/`mem_chain_match`) lets identical chains share one clone, bottoming out at the live DRAM anchor.

### Considerations

> **NOTE —** there are *two* remat layers sharing the same economic intuition ("a DRAM→SB reload is cheaper than holding/spilling the SBUF tile"). RematOpt #30 is the pre-RA, IR-level, matmul-transpose-specific member. The coloring allocator carries its own decision via `SB_Allocator::is_rematable_load` (0x9e8180) — see [SBUF Weight Pinning](#sbuf-weight-pinning) below, which uses the *same* DRAM8→SB16 predicate for the "keep resident" arm. They are complementary, not the same code; the cost currencies differ (InstProfiler vtable cost + `getLatency` vs the allocator's coloring spill cost).

Other remat surfaces exist for completeness: `DMAOptimization::dmacopy_remat_optimization` (DMA-engine side, `_CopyRematSpillSave`) and `backend::rematSoftmax` (0xe91890, an InstBuilder softmax recompute helper) — neither is reached from `RematOpt::run`.

---

## remove_redundancies — Dead-Store Elimination

### Purpose

Pass #75 (`RemoveRedundancies`, registered at optlevels {1,2,3,6}) is named like a CSE-lite "redundant-load / memset remover," but its per-`Function` body calls **exactly two** routines, unconditionally: `remove_clobbered_writes` then `remove_useless_insts(…, true)`. Its actual semantic is **dead-store (clobbered-write) elimination at access-pattern granularity** plus a light intra-function DCE sweep.

The load and memset removers the name suggests do exist, in the *same* translation unit, but they are wired into *different* passes. `remove_redundant_loads` (0x159fcb0) runs from `NonSSALeg::run` #17 (pre-allocation) and from `pre_schedule`; `remove_redundant_memsets` (0x15a0e90) runs only from `pre_schedule`. Neither is reachable from #75.

> **GOTCHA —** the pass name promises load and memset elision; the body delivers dead-store elimination. Load/memset elision belongs at #17 and in `pre_schedule` — roughly 58 pipeline positions earlier than where the name would put it.

### Entry Point

```text
RemoveRedundancies::run     0x15a4ce0   ── thin wrapper (reads optlevel int, builds Logger)
  └─ remove_redundancies    0x15a48b0   ── per-Function loop
       ├─ remove_clobbered_writes  0x159cd00   ── THE real #75 work (dead-store elim)
       └─ remove_useless_insts     0x159d760   ── light TBB intra-fn DCE
```

### Algorithm — the Clobber Proof

`remove_clobbered_writes` constructs a `DeadCodeElim` object *only* to borrow its `dont_touch_inst` preservation predicate (it is never run). It linearises the function in program order and does a pairwise adjacent (I, J) scan; I's write is dead iff *all* of L1–L7 hold:

```c
function remove_clobbered_writes(Function& F, Logger& log):   // 0x159cd00
    DeadCodeElim dce(opts, 20, true)          // 0x6127e0 — only for dont_touch_inst
    get_inst_in_order(F, insts)               // program-order linearisation
    for I in insts:
        if is_save|is_mm|is_loop_inst|is_rmw(I): continue
        if opcode(I)==48 /*TensorScalarCache*/ and field@0xf8 != 9: continue
        if I has empty arg list: continue
        J = next inst in program order after I
        if is_save|is_mm|is_loop_inst|is_rmw(J) or J empty: continue
        // J clobbers I (I's write is dead) iff ALL of:
        if  used_between(I).field@0x18 == 1                  // L1: no intervening reader
        and used_between(J).field@0x18 == 1
        and !isDstPotentialPartialAccess(I)                  // L2: not a partial write
        and !isDstPotentialPartialAccess(J)
        and J.out.memloc.type@0xd8 != 0x20 /*PSUM*/          // L3: PSUM never clobber-elim'd
        and same_partitions(AP_I, AP_J)                      // L4: identical physical lanes
        and (for each J input AP: type != PSUM and !doAccessesOverlap(AP_I, that)) // L5
        and !dont_touch_inst(I)                              // L6: preservation veto
        and supersetAP(AP_J, AP_I, 0):                       // L7: J fully covers I
            // REWRITE: reroute I's WAW (EdgeKind=2) dep edges onto J
            for each I edge with hasEdge(EdgeKind=2): J.addDependency(target, 2, false)
            BasicBlock::removeInstruction(I); count++
```

L3 is the memory-space guard: PSUM (`MemoryType` 32) is accumulator memory — a later write to PSUM does not necessarily kill the earlier one (RMW / matmul-bias semantics), so PSUM stores are never clobber-eliminated. The WAW (EdgeKind=2) reroute keeps the dep model consistent for the downstream `build_fdeps` (#74) and `anti_dependency_analyzer` (#76).

`remove_useless_insts` (0x159d760) is a TBB `parallel_for` intra-function userless-instruction sweep sharing the same `dont_touch_inst` predicate and the multi-BB escape set (`getAllMultiBBAccessedMemLocs`), pruning dead allocs via `Function::removeMemoryLocation`. It is a cheap cleanup right after the clobber removal; the full module DCEs (#11/#50/#66/#97) run later.

### Considerations

> **NOTE —** #75 is the *only* pass that kills a *clobbered store* — a write that does produce a value but is overwritten before any read. This is distinct from its neighbours: DCE (#11) removes insts with **no user at all**; `tensor_copy_elim` (#34/#77) removes **copies** and retargets consumers onto the copy source. The clobber-write remover removes writes and only *reroutes* dep edges — by construction (L1 guarantees no intervening read) a clobbered store has no surviving consumer, so nothing needs retargeting.

The redundant-load sibling (`remove_redundant_loads`) reasons at the `MemoryLocation` level using `same_dram_src`/`same_sb_dst` reaching-definition residency maps (`DenseMap<Inst*,long>` order timelines) — "this is a re-load of a value already resident in SBUF." It is glued to `NonSSALeg` precisely because residency is reasoned *before* physical addresses exist; once coloring runs, the physical-slot equivalent is handled by `tensor_copy_elim` #77.

---

## tensor_copy_elim — Backend Copy Propagation

### Purpose

`tensor_copy_elim` (worker `tensor_cp_elim::run` 0x15a9a10, driver `TensorCopyElim::run` 0x15bb4f0) **removes a copy entirely by making its consumers read the copy's source directly** (copy propagation on BIR memory locations + access patterns), then `BasicBlock::removeInstruction`s the copy. It is a distinct, separately-registered backend pass from the Penguin-side copy elimination documented in [Penguin Data-Movement: Fusion + Copy-Elimination](../penguin/data-movement-fusion.md) — that one operates on the Penguin/affine IR before BIR; this one operates on BIR access patterns and runs across the allocation boundary.

> **QUIRK —** the *same* `run` body runs **twice** — pass #34 (pre-allocation) and pass #77 (post-allocation) — and the legality regime is switched silently by whether allocation has happened. The candidate filter `all_aps_are_physical` (0x10e8bb0, requires every AP `kind`@+24 == 1 Physical) is FALSE pre-alloc and TRUE post-alloc, and `MemoryLocation::allocated` (+168) flips from false to true. Pre-alloc it does *value-level* coalescing (drop copies whose src/dst are logical aliases) to shrink the graph the allocators color; post-alloc it does *physical-slot aliasing* (recompute consumer base partition/offset from the source's physical layout, enforce alignment) to mop up copies that only became redundant after coloring.

### Algorithm

```c
function tensor_cp_elim_run(Function& F, ...):       // 0x15a9a10
    for each candidate inst in program order:
        if !all_aps_are_physical(inst): continue     // 0x10e8bb0 — the #34 vs #77 switch
        if !is_copy(inst): continue                  // 0x10ef620 — opcode {1,23}, or identity Activation(4)
        if different_dtypes(inst):    excuse[inst]=6 /*diff_dtypes*/; continue
        if !isContiguousCopy(inst):   excuse[inst]=5 /*incontiguous*/; continue
        uses = get_uses(inst)                        // 0x15a93e0 — forward overlapping readers
        if uses empty: excuse[inst]=9 /*no_users*/; continue
        for each use: run the 22-entry excuse veto chain (dep_between, select, mm_use,
            read_sb, clobber, out_of_bounds, act_psum_bias, partition_mismatch,
            diff_bb, mismatch_alignment[post-alloc], ...)  // excuse2str 0x15a65b0
        if any veto fired: skip (keep copy, record the reason)
        // REWRITE: retarget each surviving user AP onto the copy SOURCE
        for each user arg that is a supersetAP of the copy output:
            setLocation(user_arg, copy.SOURCE)
            setOffset(user_arg, translate(userOffset - copyOutOffset) into source frame)
            // POST-ALLOC: rebuild base partitions (getBasePartition), require >= 2 SB partitions
        BasicBlock::removeInstruction(copy)
```

The 22-entry "excuse" enum (`excuse2str` @0x15a65b0) is a built-in diagnostic ledger of *why a copy could not be eliminated* — each value is the int written into a per-copy `DenseMap` at the run-body line where that legality test fails (e.g. `dep_between`=0, `multi_user`=1, `diff_dtypes`=6, `clobber`=11, `out_of_bounds`=12, `mismatch_alignment`=20, `psum_pressure`=21). This vocabulary parallels the `remove_redundancies` excuse pool (same TU family) — the redundancy and copy-elim families share the same legality words but each removes a different op kind.

> **NOTE —** the sibling `tensorcopy_accel` (#58, `accelerateTensorCopy` 0xb88f20) is *not* a copy remover. It takes a *necessary* copy and bit-casts it to the widest power-of-2 element width that the GCD of physical src-addr, dst-addr, and byte-length permits (e.g. 64×int8 → 16×int32), cloning it as `<name>-accel` so the data-move engine issues fewer, wider ops. It must run post-address-rotation because the GCD is over *physical* addresses. "Routing to the fast path" here means *width* selection, not engine selection.

---

## SBUF Weight Pinning

### Purpose

"Pinning" in `SB_Allocator` means **electing to keep a static (DRAM-backed, re-loadable) weight tensor resident in SBUF for the whole function** instead of letting it be evicted and reloaded each iteration — trading the per-iteration DRAM→SB reload against fast context switching (a pinned footprint is dead weight that must be saved/restored on a context switch). It is implemented inside the coloring allocator as a pre-pass (`find_loads`), not as a standalone pipeline pass.

> **QUIRK —** "static weight" is identified *structurally*, not by a named attribute. A pin candidate is the SBUF destination of a **rematable load** — a single-shot DRAM→SB load whose source can simply be re-read. The predicate `is_rematable_load` (0x9e8180) is *identical* to the one RematOpt uses; pinning is the "keep resident" arm and remat is the "reload" arm of the same economic test.

### Algorithm

```c
function is_rematable_load(Instruction* inst):       // 0x9e8180
    op = inst.type@+0x58
    if not (op==0x13 /*19 InstLoad*/
            or (op==0x20 /*32 DMACopy*/ and inst[+0x128] <= 1 and !isDynamicOffsetDMA(inst))):
        return false
    require num_arguments()==1 and num_outputs()==1   // asserts sb_loads.cpp:104/108
    require input.MemoryType == 8  /*DRAM*/
    require output.MemoryType == 16 /*SB*/
    return true                                        // a DRAM(8)→SB(16) single-in/out load

function find_loads(Function* fn, vector<Info>& info, CFG&):   // 0x9e99f0
    // GATE A — global master enable (the no-memory-pinning cl::opt @0x3dc8510)
    if cl::opt_no_memory_pinning.value@+0x78 != 0:   // --fast-context-switch flips it
        walk loads for remat-cost bookkeeping but DO NOT pin
    else:
        for each inst in fn:
            if is_rematable_load(inst):
                id = info_id_of(inst.SB_dest)
                info[id].pinned@+0x10C = 1            // PIN optimistically
                tally per-partition bytes into +0x4C0 (tried) / +0x4C8 (pinned)
    // GATE B — per-allocator runtime disable (arch / option driven)
    for each pinned node:
        if allocator.pinning_disabled@+0x4B8:
            log "pinning disabled to support fast context switching"  // sb_loads.cpp:278
            info[id].pinned@+0x10C = 0               // un-pin
```

The decision is a **policy flag, not a cost comparison** — there is no per-tensor benefit-vs-context-switch cost model in `find_loads` (no `comiss`/cost branch on the pin decision; only flag tests). Gate A is the global `cl::opt` whose help text is "avoid pinning static weights in favor of faster context switching" (name `fast-context-switch`, `PassOptions.fast_context_switch`); Gate B is the per-allocator `+0x4B8` runtime flag, which also carries an arch-specific unconditional disable ("[Temporary] Do not pin sb tensors for Sunda."). The byte budget is *implicit* — pinned nodes enter the ordinary interference graph and consume ordinary SBUF capacity; the cost saved is reported post-hoc ("pinning saved approximately").

### Graph Insertion and Unpin Fallback

A pinned tensor is not excluded from the interference graph — it becomes a fixed-placement (preallocated) node:

```c
// build (sb_build.cpp:100): admit a node iff it has uses OR is pinned
admit(info[i])  iff  info[i].def_use_vec.size() > 0  ||  info[i].pinned
// → a pinned node enters the graph even with no ordinary def/use range,
//   forcing the colorer to reserve real SBUF for it.

// find_costs (0x9e3ff0): pinned nodes get fixed/precolored cost
if info[i].pinned@+0x10C: cost@+0xA0 = 0; flag@+0x10E = 1   // never a spill victim
```

Under SBUF pressure the spill machinery can evict a pinned weight (turn a pin back into a reloadable spill) in `insert_spill_code` (0xa27280): increment the unpinned-tensor count (+0x488), add the tensor's bytes (Info+0x15C) to the total unpinned size (+0x490), clear `pinned` (+0x10C), and re-insert a DRAM→SB reload at the use. The "surprised to see pinned value" diagnostic guards the invariant that a spill set should not contain a value still believed pinned.

> **NOTE —** the pin/unpin economics close the loop with RematOpt and the allocator remat: `find_loads` pins *optimistically* (all rematable-load dests), the colorer treats pinned nodes as fixed/infinite-cost, and only `insert_spill_code` rolls a pin back when forced. The "tried to pin" → "successfully pinned" / "unpinned" delta is exactly this rollback — the same DRAM↔SB reload-vs-resident trade RematOpt #30 makes one pass earlier.

---

## Where the Allocation Boundary Puts Each Pass

```text
order  pass                         regime          this page
─────  ───────────────────────────  ──────────────  ──────────────────────────────
  20   pre_opts                     modular-link    RemoveBoundaryCcBuffer (modular only)
  26   constant_propagate           pre-alloc       birsim partial evaluator
  30   remat_optimization           pre-alloc       MM-transpose spill+reload (default OFF)
  31   early_peephole_opts          pre-alloc       SplitSelect / ActAcc / PermuteImplicitReduceAdd
  33   pre_sched                    ─ boundary ─
  34   tensor_copy_elim             pre-alloc        logical value coalescing
  44   coloring_allocator_psum      ─ alloc ─        (SBUF pinning lives in the SB allocator, #51)
  51   coloring_allocator_sb        ─ alloc ─        find_loads pins; insert_spill_code unpins
  58   tensorcopy_accel             post-alloc       width-widening (sibling, not a remover)
58-59  peephole_opts                post-alloc       SimplifyMemset / MoveToAct
  75   remove_redundancies          post-alloc       clobbered-write (dead-store) elim
  77   tensor_copy_elim             post-alloc       physical-slot aliasing
```

The single organising principle: a pass that *changes* an op's engine, dataflow shape, or buffer set runs before #33; a pass that needs *physical* access patterns, final dtypes, or assigned engines runs after the allocators. `constant_propagate` and `remat_optimization` run early because they restructure the IR the allocators must color; `peephole_opts`, `remove_redundancies`, and the second `tensor_copy_elim` run late because their legality proofs need real addresses.

---

## Evidence Summary

| Claim | Confidence | Anchor |
|---|---|---|
| The three `*_opts` passes are distinct (not one suite); algebraic identities are NOT in them | CERTAIN | objdump of all three `run` bodies; zero DCE/CP calls |
| `ConstantPropagateVisitor` derives from `birsim::InstVisitor`; folds by simulation | CERTAIN (structure) / HIGH ("is the sim") | ctor 0xbe0130 takes `birsim::Memory*`; `enterInstruction` calls `birsim::InstVisitor::enterInstruction` then 110-case switch |
| Sim loop gated by `enable-sim-based-constant-propagate`; pre-passes each DCE | CERTAIN | run() decomp; string @0x1cece60; `dword_3DFFCC8[12]` branch |
| `RematOpt` default-OFF; template `[GenericCopy,Matmult,Load]`; cost = Σ vtable+0x68 vs 2·getLatency | CERTAIN | run gate `unk_3E01A58`; "MM transpose remat optimization disabled"; COMISS/JA @0x107588d |
| `remove_redundancies` #75 calls ONLY clobbered-writes + useless-insts | CERTAIN | 0x15a48b0 body; callgraph for the load/memset removers (NonSSALeg/pre_schedule) |
| Clobber proof L1–L7 (supersetAP ⊇, same_partitions, no read, ¬PSUM, ¬partial, ¬dont_touch) | CERTAIN | 0x159cd00 body; the `0x20` PSUM guard matches the memory-type taxonomy |
| `tensor_copy_elim` runs twice; regime switched by `all_aps_are_physical` / `allocated`+168 | CERTAIN | same `run` body; pipeline orders #34/#77 |
| SB pinning predicate DRAM8→SB16 single-in/out; policy-flag gate (no cost model) | CERTAIN | `is_rematable_load` 0x9e8180; no comiss on pin decision; Gates A/B flag tests |
| `constant_propagate_for_dataType_change` exists but is off the `run()` path | MEDIUM | xrefs show def + internal refs only |
| `getLatency` / vtable+0x68 numeric per-opcode cost values | LOW | tables live in TrainiumHwm/InstProfiler, out of scope here |

### Limits of this reading

Every address above is anchored to a named symbol, an offset, a `.rodata` string, or a disassembly branch in the cp310 `libwalrus.so` sidecar set. Three things are weaker than the rest. The exact `cl::opt` symbol names behind the two ConstantPropagate enable bytes are tied to their strings by the `run()` branch structure rather than walked separately. The derivation of pinning Gate B — how `PassOptions` reaches the allocator's `+0x4B8` flag, and where the `arch == Sunda` compare sits — is not fully traced. And the per-opcode numeric cost and latency tables that RematOpt consumes live in a different binary region and are not transcribed here. The structural claims — birsim-backed folding, default-off remat, #75 as a dead-store eliminator, the twice-run copy elim, and the rematable-load pin predicate — each rest on a read function body or a callgraph edge.

---

## Related Components

| Pass | Order | Relationship |
|---|---|---|
| `dead_code_elim_o0/o1` | 11/50/66/97 | reaps the dead memsets/reduces/copies this cluster leaves behind |
| `SeqInstOpt` | own | sequencer-engine `InstRegisterMove` folding — the only register-level peephole in the library |
| `tensorcopy_accel` | 58 | width-widening sibling of `tensor_copy_elim`; bit-casts necessary copies, removes none |
| `NonSSALeg` | 17/65 | runs `remove_redundant_loads`; re-legalizes `tensorcopy_accel`'s `-accel` clones |
| `pre_schedule` | ~32 | runs `remove_redundant_loads` and `remove_redundant_memsets` (the #75 "siblings") |
| `coloring_allocator_sb` | 51 | hosts `find_loads` pinning and `insert_spill_code` unpin |

## Cross-References

- [The Walrus Pass Pipeline & the Optlevel Planes](pass-pipeline-optlevels.md) — where every pass in this cluster sits in the numbered pipeline and which optlevels enable it
- [BIR Simulator: Dispatch & Whole-Machine State](../bir/sim-dispatch-state.md) — the `birsim::InstVisitor` dispatch and value store that `ConstantPropagate` executes the module into
- [Penguin Data-Movement: Fusion + Copy-Elimination](../penguin/data-movement-fusion.md) — the front-end (pre-BIR) copy elimination, distinct from the backend `tensor_copy_elim` here
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the SB (MemoryType 16) / PSUM (32) / DRAM (8) memory taxonomy the pinning and clobber-write guards key on
