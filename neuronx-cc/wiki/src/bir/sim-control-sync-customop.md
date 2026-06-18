# BIR Simulator: Control-Flow, Sync/Semaphore & Kernel/Custom-Op Stubs

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, `neuronxcc/starfish/lib/libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`, cp310 wheel). For `.text`/`.rodata`, virtual address equals file offset (`readelf`: `.text` `sh_addr`==`sh_off`==`0xf1ad0`; `.rodata` `0x58f000`==`0x58f000`). Two body frames exist per function: the IDA per-symbol sidecar uses a `<0x180000` thunk address (e.g. `visitLoop` `@0xebe40`), the dispatch resolves to the defining body at the high VA (`visitLoop` `@0x211010`); this page cites the **defining body VAs** — the ones the 110-case dispatch jumps to. Other wheels differ; treat every address as version-pinned.*

## Abstract

The [BIR simulator dispatch](sim-dispatch-state.md) is a `110`-case opcode switch, but four families of opcodes never reach a numeric kernel: they **steer the interpreter** instead. This page documents those four families:

1. **Structured control flow** — `Loop` (105), `DynamicForLoop` (106), `DoWhile` (108): the three opcodes [intercepted *before* the switch](sim-dispatch-state.md#11-the-pre-switch-control-flow-interception) (`cmp eax,0x69/0x6A/0x6C` → tail-jump). They recurse the visitor over their region's basic blocks, one pass per trip, threading the induction value into a per-axis affine environment so symbolic addresses over the loop axis resolve.
2. **Terminators** — `CompareAndBranch` (78), `UnconditionalBranch` (79), `Return` (81), `Exit` (82), `Break` (83), `Call` (84): they don't execute the next block, they *select* it by writing a single "next-BB" slot the CFG driver follows.
3. **Sync / semaphore ops** — the SyncState two-bank model (one-shot events + counting semaphores), the `needWait`/`actOnWait`/`actOnUpdate` per-instruction engine with cross-core routing, and the per-opcode sync kernels (`GroupResetSemaphores`, `SwitchQueueInstance`, `ResetQueueInstance`). The headline: most sync opcodes have **empty kernel bodies** — their entire meaning is the base `SyncInfo` the scheduler processes.
4. **Kernel / custom-op stubs** — and here is the decisive correction to the naive "custom op = no-op" assumption: `CustomOp` (53) and `BIRKernel` (54) are **simulated out-of-process** (they serialize tensors to `.npy`, shell out to an external `kernel_sim` executable, and read the result back); `NKIKernel` (55) is **simulated by inline BIR-subfunction expansion** (a synthetic `Call`+`Return`); only `NKIKLIRKernel` (56) and `InlineASMBytes` (93) are genuinely stubbed (a silent "skip + note").

For reimplementation, the contract is: the induction-value DenseMap threading, the do-while/dynamic-for register predicate evaluation, the next-BB-slot CFG transfer, the two-bank wait/update state machine, and the three-way custom-op execution split (delegate / inline-expand / stub).

| | |
|---|---|
| **Binary** | `libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`; 16,821 funcs; full decompiled C) |
| **Pre-switch CF** | `visitLoop` `@0x211010`, `visitDynamicForLoop` `@0x211690`, `visitDoWhile` `@0x2120c0` |
| **Affine env** | `DenseMap<pelican::AffineIdx*, long>` at `InstVisitor+0xE18` (buckets `+0xE20`, count `+0xE28`) |
| **Next-BB slot** | `InstVisitor+0xE10` (`= this[450]`), written by terminators, followed by `visitBBHolderInControlFlow` `@0x20f820` |
| **SyncState** | `InstVisitor+0x1A0`; `Events` (256-bit) at `+0`, `Semaphores` (256×u32) at `+0x58` |
| **Custom-op delegate** | `simulateKernel` `@0x26ecd0` → `system("kernel_sim")` `@0x26fc14` |

---

## 1. Structured control flow — the three pre-switch loop kernels

`Loop`/`DynamicForLoop`/`DoWhile` are pulled out of the dispatch by explicit compares *before* `enterInstruction` and the switch ([dispatch §1.1](sim-dispatch-state.md#11-the-pre-switch-control-flow-interception)). The reason is structural: these three opcodes own an iteration **axis** and must re-execute their region body once per trip — they recurse `visit()` rather than dispatching a single kernel. Each runs its own `enterInst<X>` hook (`enterInstLoop`, `enterInstDynamicForLoop` `@0x1a9c00`, `enterInstDoWhile`) at entry. **[CONFIRMED — the three tail-jumps + per-kernel `enterInst<X>` call as the first instruction of each body.]**

The **central runtime datum** these kernels maintain is a `DenseMap<pelican::AffineIdx*, long>` at `InstVisitor+0xE18` (`this[451]` is the bucket array, `*((u32*)this+906)` the bucket count, `*((u32*)this+904)`/`+905` the live/tombstone counters). The map key is the loop's **axis** object; the value is the **current induction value**. Every nested instruction whose access pattern is a `QuasiAffineExpr` over this axis resolves its symbolic address by looking the axis up in this map — so updating the map entry each iteration is what makes the loop body "see" a different index on each pass. The map's full LLVM `DenseMap` rehash/grow machinery is inlined into `visitLoop` (the `"Empty/Tombstone value shouldn't be inserted"`, `"# initial buckets must be a power of two"`, and `moveFromOldBuckets` asserts are all present, instantiated for `DenseMap<pelican::AffineIdx*, long>`). **[CONFIRMED — the inlined DenseMap asserts name the exact `KeyT = pelican::AffineIdx*; ValueT = long int` instantiation.]**

### 1.1 `visitLoop` (IT 105) `@0x211010` — static `for` loop

`Loop` carries a **static** axis: integer `lb`/`ub`/`stride`. The axis pointer is `*((_QWORD*)inst + 42)` (`InstLoop+0x150`); its fields are `ub` at axis `+0x28` (`v4[5]`), `stride` at `+0x30` (`v4[6]` = `v52`), `lb` at `+0x20` (`*((int*)v4+8)`, sign-extended). The decompiled core, with the boilerplate DenseMap-grow path elided:

```c
// birsim::InstVisitor::visitLoop  @0x211010  (CONFIRMED)
enterInstLoop();
if (*((uint8_t*)this + 208))                  // "in-control-flow" mode flag (+0xD0)
    return visitBBHolderInControlFlow(this, inst);   // CFG-driver mode (§2.5)

axis = *((void**)inst + 42);                  // InstLoop->LoopAxis  (+0x150)
if (axis[5] /*ub*/ || (axis[4] | axis[6]) /*lb|stride*/) { ... }  // non-degenerate axis
else { for (BB in inst.blocks) visit(BB); return; }   // (axis with no bound: one body pass)

if (*((uint8_t*)inst + 328) /*isParallel @+0x148*/)
    return visitInstLoopParallel(this, inst+88, &axisVec);   // PARALLEL path (§1.4)

i      = (int)axis[8];                         // lb  (axis+0x20, sign-extended)
ub     = axis[5];                              // axis+0x28
stride = axis[6];                              // axis+0x30
if (i >= ub) return;                           // zero-trip guard
do {
    env[axis] = i;                             // DenseMap<AffineIdx*,long> @+0xE18: axis -> i
    for (BB = inst.blocks_head; BB != sentinel; BB = BB->next)
        visit(this, (BasicBlock*)(BB - 9 /*sub-obj @+0x48*/));   // re-execute body
    i += stride;                               // induction step
} while (ub > i);                              // continue while ub strictly above i
```

So the trip count is `ceil((ub - lb) / stride)`, realized by the `i < ub`, `i += stride` loop — matching the static `LoopAxis::getTripcount` ceiling division. The induction value `i` is written into the affine env (`*v15 = v7` in the decompile) keyed by the axis pointer *before* each body pass; that single DenseMap entry is the entire carried state across iterations. There is **no** explicit phi-copy: the loop body's instructions read `i` indirectly through the env when they evaluate their `QuasiAffineExpr` addresses. **[CONFIRMED — body @0x211010: `*v15 = v7` (env write), the `v16 != (char*)a2+8` BB walk, `v7 += v52` step, `v4[5] <= v7` exit test.]**

### 1.2 `visitDoWhile` (IT 108) `@0x2120c0` — post-tested loop

`DoWhile` is a C-style `do { body } while (reg != 0)`: the body executes at least once, then a runtime register is read and the loop repeats while it is non-zero. Between the body and the predicate it **clears the "written-this-iteration" MemoryLocation set** so each trip's dataflow provenance checks start clean.

```c
// birsim::InstVisitor::visitDoWhile  @0x2120c0  (CONFIRMED)
enterInstDoWhile();
do {
    for (BB = inst.blocks_head; BB != sentinel; BB = BB->next)
        visit(this, (BasicBlock*)(BB - 9));           // body, >= 1 pass
    // reset DenseMap<bir::MemoryLocation*, DenseSetEmpty> @ this+0xE48 (this[457]):
    //   count fields *((u32*)this+916)/+917, bucket count +918 — re-init to all-tombstone
    //   (xmmword_5F22A0 = {-4096,-4096} broadcast); this is the per-trip provenance clear
    result = evaluateDoWhileCondition(this, inst);     // §1.2.1
} while ((uint8_t)result);                              // continue while truthy
```

The provenance DenseMap is `DenseMap<bir::MemoryLocation*, DenseSetEmpty>` — the IDA-recovered `initEmpty` assert names exactly that instantiation. The empty-key fill broadcasts `xmmword_5F22A0` (`{-4096, -4096}`) across the bucket array. **[CONFIRMED — body @0x2120c0: the `do { ... evaluateDoWhileCondition } while (result)` structure and the `MemoryLocation* -> DenseSetEmpty` reset.]**

#### 1.2.1 `evaluateDoWhileCondition` `@0x1a99b0` — the register predicate

The condition is a `bir::BirIntRuntimeValue` expression at `inst[41]` (`InstDoWhile+0x148`); after a `cast<BirIntRuntimeValue>` (the `isa<>`/`cast<>` guard asserts target `bir::BirIntRuntimeValue`), the predicate reduces to a single scalar register read:

```c
// birsim::InstVisitor::evaluateDoWhileCondition  @0x1a99b0  (CONFIRMED)
cond = inst[41];                                       // InstDoWhile+0x148 (a BirIntRuntimeValue expr)
//   cast<bir::BirIntRuntimeValue>(cond)  — asserts kind in [6, 6+7]
reg  = *(bir::Register**)(cond + 64);                  // the runtime predicate register (+0x40)
return RegState::read((RegState*)(this + 336), reg, 0) != 0;   // TRUE iff reg value != 0
```

So the loop exit is decided purely by whether a scalar register (the loop-carried counter/flag, written by the body) is non-zero. The register file is the [`RegState`](sim-dispatch-state.md#51-birsimregstate--per-engine-16-byte-register-entries) at `InstVisitor+0x150` (`+336`). **[CONFIRMED — body @0x1a99b0: `RegState::read(this+336, *(Register**)(cond+64), 0) != 0`.]**

### 1.3 `visitDynamicForLoop` (IT 106) `@0x211690` — runtime trip count

`DynamicForLoop` is the runtime-bound counterpart of `Loop`: its axis (`InstDynamicForLoop+0x148`) holds `lb`/`ub`/`stride` as `QuasiAffineExpr` objects, not integers. `lb` and `stride` are evaluated against the affine env (so an *outer* loop's induction value can feed an *inner* loop's bounds — nested dynamic loops); the **upper bound** is constrained to a single-term runtime-register expression and computed by `evaluateUpperBoundExpr`.

```c
// birsim::InstVisitor::visitDynamicForLoop  @0x211690  (CONFIRMED)
enterInstDynamicForLoop();
axis   = *((void**)inst + 41);                         // InstDynamicForLoop+0x148
i      = QuasiAffineExpr::eval(axis->lb_expr   /*+0x68*/, env);   // runtime start
ub     = evaluateUpperBoundExpr(this, inst);           // runtime bound (register read)  §1.3.1
stride = QuasiAffineExpr::eval(axis->stride_expr /*+0xA8*/, env);
for (; i < ub; i += stride) {
    env[axis] = i;                                     // affine env update (as §1.1)
    for (BB in inst.blocks) visit(BB);                 // re-execute body
    // reset the written-MemoryLocation provenance set (as §1.2)
}
```

The discriminator versus the static `Loop` is exactly the axis field type (`QuasiAffineExpr` vs `int64`), and the env DenseMap is the variable environment those expressions resolve against. **[CONFIRMED — body @0x211690 calls `evaluateUpperBoundExpr` for `ub`, `QuasiAffineExpr::eval` for `lb`/`stride`, same env-write + body-recurse loop as `visitLoop`.]**

#### 1.3.1 `evaluateUpperBoundExpr` `@0x1b52c0` — the runtime bound from a register

The upper bound is an `AffineExpr` (`Expr` kind `== 17`) constrained to **one term with a runtime value**. If it has more than one term or no runtime value, the simulator throws `NeuronAssertion` ErrorCode 1910 with `"Only scalar values supported in Dynamic For Loop upper bound expressions currently"` (assert text `"ubExpr->getNumTerms() == 1 && ubExpr->hasRuntimeValue()"`, source `inst_visitor.cpp:697`/`:1910`). The bound is then `constant + coefficient × register`:

```c
// birsim::InstVisitor::evaluateUpperBoundExpr  @0x1b52c0  (CONFIRMED)
ubExpr = *(void**)(*(void**)(inst + 328) + 136);       // axis(+0x148)->ub_expr  (+0x88)
//   cast<pelican::AffineExpr>(ubExpr)  — asserts Expr kind == 17
//   assert ubExpr->getNumTerms() == 1 && ubExpr->hasRuntimeValue()   (else throw 1910)
term  = ubExpr.terms[0];                                // a single BirIntRuntimeValue term
coef  = term.coef;                                      // *((long*)&term + 1)
reg   = *(bir::Register**)(term + 64);                  // the runtime loop-bound register (+0x40)
constOff = *(int32_t*)(ubExpr + 56);                   // ubExpr.constant
return constOff + RegState::read((RegState*)(this + 336), reg, 0) * coef;
```

This is *why* a `DynamicForLoop`'s trip count is unknown until execution: the bound is a scalar register read (`RegState::read`), scaled by a static coefficient and offset by a static constant. Only the `ub` is so constrained; `lb`/`stride` go through general `QuasiAffineExpr::eval`. **[CONFIRMED — body @0x1b52c0: `*(int*)(ubExpr+56) + RegState::read(this+336, *(Register**)(term+64), 0) * coef`; the 1910/`getNumTerms()==1` assert in the same body.]**

### 1.4 Parallel loops — `visitInstLoopParallel` `@0x20cc60`

When `InstLoop.isParallel` (`+0x148`) is set, `visitLoop` tail-calls `visitInstLoopParallel` instead of the sequential walk. It pushes the loop's axis onto a carried `SmallVector<LoopAxis*>`, recurses over the body collecting nested parallel axes, and the leaf executor `visitInstInParallelLoop` `@0x20c940` runs each instruction once across the whole axis range (parallel semantics) rather than iterating. **[STRONG — entry + structure; the per-element parallel AP indexing is a separate per-op concern.]**

---

## 2. Terminators — the next-BB slot & CFG transfer

The terminator kernels do not execute a successor block; they **name** it by writing a single pointer slot, `InstVisitor+0xE10` (`= this[450]`), the "pending successor BB". After each basic block, the CFG driver reads that slot and follows it. This is the runtime realization of the [SSA block-argument CFG](instruction-base.md): there are no explicit successor/predecessor lists in these kernels — the terminator names the successor, and carried values flow through `RegState` (scalars) plus the affine env (induction), never an explicit phi-copy.

| IT | kernel | body VA | next-BB-slot effect |
|---:|--------|--------:|---------------------|
| 78 | `CompareAndBranch` | `0x1bffb0` | `slot ← taken ? on_true(+0xF8) : on_false(+0x100)` |
| 79 | `UnconditionalBranch` | `0x1aa550` | `slot ← target(+0xF0)` |
| 80 | `BranchHint` | `0x1aa650` | none — body is `ret` (pure no-op) |
| 81 | `Return` | `0x1aa560` | `slot ← 0` + pop call frame + clear all sems (§4.2) |
| 82 | `Exit` | `0x1af7b0` | throw `InstExitException` (single-core) / return (LNC) (§4.3) |
| 83 | `Break` | `0x1aa640` | `slot ← 0` (exit enclosing region) |
| 94 | `TensorCompletion` | `0x1aa450` | none — body is `ret` (marker only) |

### 2.1 `visitInstCompareAndBranch` (IT 78) `@0x1bffb0`

Hex-Rays failed on this body; read from disassembly (99 bytes). It evaluates a [`BranchCompareOp`](aluop-modes.md) predicate over two scalar operands and selects one of two successors:

```c
// visitInstCompareAndBranch  @0x1bffb0  (CONFIRMED — disasm)
lhs   = Instruction::getArgument(inst, 0);             // compared scalar
rhs   = sub_1BE610(inst);                              // rhs operand (immediate or register)
op    = *(uint32_t*)(inst + 0xF0);                     // BranchCompareOp (comp_op)
taken = compareScalarArgs(lhs, rhs, op);               // §2.2
this[450] /*+0xE10*/ = taken ? *(void**)(inst + 0xF8)  // on_true successor BB
                             : *(void**)(inst + 0x100); // on_false successor BB
```

The `comp_op@+0xF0`, `on_true@+0xF8`, `on_false@+0x100` field layout matches the static `InstCompareAndBranch` struct byte-exact. **[CONFIRMED — disasm; the immediate-vs-register split of the rhs lives in `sub_1BE610@0x1be610`, read at call-site only — INFERRED that the IMM block reads a baked `Argument` and the REG block reads `RegState`.]**

### 2.2 `compareScalarArgs` `@0x1bce50` — the per-dtype comparator

The predicate is applied at the `lhs` Dtype width through a per-dtype comparator map. The dtype must be `≤ 0x10` (the scalar widths: `u8`/`s8`/`u16`/`s16`/`u32`/`s32`/`f32`) else a FATAL (`inst_visitor.cpp:1468`). The map is `std::map<BranchCompareOp, std::function<bool(T,T)>>` (tables at `unk_2296C48..2297248`), keyed by the comparison op, yielding the standard `LT`/`LE`/`EQ`/`NE`/`GE`/`GT` relations. **[CONFIRMED at dispatch level; the per-comparator function bodies not individually transcribed — INFERRED standard relational identity from the `BranchCompareOp` key.]**

### 2.3 The pure-successor & no-op terminators

`UnconditionalBranch` is a 15-byte body: `slot ← *(void**)(inst+0xF0)` then `ret`. `Break` is 12 bytes: `qword[this+0xE10] = 0; ret` — nulling the slot signals "no successor", which the loop region walk / CFG driver reads as "exit the enclosing region" (early loop exit / break out of a structured loop). `BranchHint` (a `LikelyTaken`/`LikelyNotTaken` static-prediction annotation) and `TensorCompletion` (a production-complete marker) are both literally `ret` — pure no-ops with no simulation effect. **[CONFIRMED — all four bodies read.]**

### 2.4 `visitInstCall` (IT 84) `@0x26bc90` — push frame & recurse

`Call` is `bir::ControlFlowIRVisitor<birsim::InstVisitor>::visitInstCall`. It pushes the callsite and caller-`Function` onto two `std::deque` call-stacks held inline in the visitor, sets the callsite memloc-binding context, then **recurses** `visit()` over the entire callee region (its basic blocks run through the same dispatcher / CFG driver), and pops on return:

```c
// ControlFlowIRVisitor<birsim::InstVisitor>::visitInstCall  @0x26bc90  (CONFIRMED)
callee = *((void**)inst + 30);                         // InstCall+0xF0 = callee region (BasicBlockHolder*)
if (!callee) return;                                   // null call = no-op
assert(CurrentCallsites.size() == 0);                  // IRVisitor.h:227 — one frame at a time
assert(CurrentCallers.size()   == 0);                  // IRVisitor.h:230
callsites_deque.push_back(inst);                       // the InstCall* (the callsite)
callers_deque.push_back(Instruction::getFunction(inst));   // the caller Function*
*(void**)this = (char*)inst + 248;                     // callsite memloc-binding base (inst+0xF8)
visit(this, callee);                                   // RECURSE over the callee region's BBs
*(void**)this = 0;                                     // pop the binding context
```

The asserts guarantee a single call frame at a time (no re-entrant callsite). The callee's formal memlocs are resolved relative to `inst+0xF8` (the call-site physical-memloc map) so the callee reads/writes the caller's tensors. **[CONFIRMED — body @0x26bc90.]** This same path is re-used by `NKIKernel` (§5.2).

### 2.5 `visitBBHolderInControlFlow` `@0x20f820` — the CFG driver

When the "in-control-flow" flag (`InstVisitor+0xD0`) is set — by `visitLoop` (§1.1) and by `Call` — block visitation follows the next-BB slot rather than static list order. It builds (and caches per `bir::Function` in a `DenseMap<const bir::Function*, unique_ptr<const CFG>>`) a `neuronxcc::backend::CFG`, starts at the entry block, and after each block reads the slot:

```c
// visitBBHolderInControlFlow  @0x20f820  (CONFIRMED structure)
BB = entry;
while (BB) {
    visit(this, BB);                                   // BB body -> a terminator sets +0xE10
    next = this[450] /*+0xE10*/;                       // pending-successor slot
    if (!next) next = linear_next_BB;                  // fall-through (no terminator / Break / Return cleared it)
    BB = next;
}
```

**[CONFIRMED structure; the `CFG` node/edge internal layout read at the `DenseMap<Function*, unique_ptr<CFG>>` + follow-loop level, not transcribed.]**

---

## 3. The sync/semaphore state model

The simulator models cross-engine and cross-core ordering with **two primitive banks** inside [`birsim::SyncState`](sim-dispatch-state.md#52-birsimsyncstate--a-flat-256-uint32-semaphore-array) (embedded at `InstVisitor+0x1A0`). These are the happens-before substrate; the sync *opcodes* mostly do nothing themselves (§5).

| Bank | Offset (in SyncState) | Model |
|------|----------------------:|-------|
| `birsim::Events` | `+0x00` | 256-bit one-shot event bitset + a touched-id RB-tree. Set-then-clear rendezvous. |
| `birsim::Semaphores` | `+0x58` | dense `uint32[256]` counter array + touched-id RB-tree. add/sub/inc/dec/write-imm; wait until `value ≥ threshold`. |

The bank ctor (`Semaphores::Semaphores @0x19d8b0`) does `operator new(0x400)` (1024 bytes = 256 `uint32`, all zero) with `end = begin + 256` and a self-rooted RB-tree at `this+0x28`. The `Events` ctor (`@0x19f8f0`) allocates a 256-bit `vector<bool>` and a touched-id RB-tree. **[CONFIRMED — ctor field writes.]**

### 3.1 Counting semaphores — wait & update modes

The wait side (`Semaphores::needWait @0x19e4b0`) is a pure poll — a semaphore wait does **not** mutate the bank on consume; the value comparison is the whole story:

```c
// Semaphores::needWait(Wait&)  @0x19e4b0  (CONFIRMED)   — true means "must keep waiting"
mode 1 (sem-ge-imm): return values[Wait.id] <  Wait.getValue();
mode 2 (sem-ge-reg): return values[Wait.id] <  RegState::read(Wait.getReg());
else (0, 3):         assert("Unhandled semaphore wait command");   // SyncState.cpp:0x56
```

The update side (`Semaphores::actOn(Update&) @0x19e300`) maps the [`UpdateMode`](instruction-base.md) enum directly onto bank mutations — values are `uint32` modular (inc/dec wrap):

```c
// Semaphores::actOn(Update&)  @0x19e300  (CONFIRMED) — five update modes
mode 5 (sem-wr-imm):  set(id, Update.getValue());
mode 1 (sem-add-imm): inc(id, Update.getValue());
mode 2 (sem-sub-imm): dec(id, Update.getValue());
mode 3 (sem-inc):     assert(value == 1, "Semaphore inc value must be 1"); inc(id, 1);
mode 4 (sem-dec):     assert(value == 1, "Semaphore dec value must be 1"); dec(id, 1);
mode 0 (evt-set) / else: assert("Unhandled semaphore update command");   // evt-set routes to Events
```

`set`/`inc`/`dec` each first insert `id` into the touched RB-tree (so `checkEventsAllClear` can flag a dangling counter at BB end), then index `values[id]` with an `id < 256` range-check. **[CONFIRMED — `needWait`/`actOn` decompiled bodies.]**

### 3.2 Events — one-shot set-then-clear

An event is a strict one-shot edge with protocol asserts on both ends:

```c
// Events  (CONFIRMED)
Events::needWait(Wait&) @0x19d850:  assert(mode == 0); return bit[id] == 0;   // wait while NOT set
Events::actOn(Wait&)    @0x1a1650:  clear(id);    // CONSUME: the waiter clears the now-set event
Events::actOn(Update&)  @0x19fd00:  assert(mode == 0); set(id);   // PRODUCE: set the event
Events::set(id)         @0x19fc60:  if (bit[id]) throw runtime_error;  // FATAL double-fire
Events::clear(id)       @0x1a0410:  if (!bit[id]) NeuronAssertion "events.at(id)";  // FATAL clear-before-set
```

The double-fire and clear-before-set FATALs are the simulator's protocol checker for the synchronizer-generated event graph. **[CONFIRMED.]**

### 3.3 The per-instruction sync engine

The per-BB driver (`simulateWithSyncs @0x20cd60`) calls three SyncState entry points around every `visit()`, all driven by the instruction's `SyncInfo` (waits = `SmallVector<Wait,1>` stride 40, updates = `SmallVector<Update,1>` stride 32):

```c
// SyncState::needWait(Inst*)    @0x19e560  — POLL: does I still have an unsatisfied wait?
for (w in I.SyncInfo.waits)
    if (SyncRef::getType(w) == 0 ? Events::needWait(w) : Semaphores::needWait(w)) return true;
return false;   // all waits satisfied -> runnable

// SyncState::actOnWait(Inst*)   @0x1a1750  — CONSUME (only EVENT waits clear; sema waits are no-op)
for (w in I.SyncInfo.waits)
    (type == 0) ? Events::actOn(w) /*clear*/ : Semaphores::actOn(w) /*validity check only*/;

// SyncState::actOnUpdate(Inst*) @0x1a0300  — APPLY (set events / mutate sems), with cross-core routing
if (I.IT == 85) for (id in InstSwitchQueueInstance.semGroupA) Semaphores::actOn(reset);  // §5 pre-effect
for (u in I.SyncInfo.updates) {
    if (!Update::getTargetCore(u).present)  SyncState::actOn(this, u);          // LOCAL
    else {                                                                       // REMOTE (cross-LNC)
        iv  = NeuronCoresManager::getInstVisitorforCore(coreMgr, targetCore);
        SyncState::actOn(iv->getSyncState(), u);                                 // apply to remote bank
        clear bit[targetCore] in coreMgr.m_waitingCores;                          // wake the remote core
    }
}
```

The schedule model is **poll, not spin**: the multi-engine ready-selector (`getNextInstruction @0x14f520`) round-robins per-engine queues and skips any engine whose head still has an unsatisfied wait; an instruction runs only when `needWait == false`. A fully-stalled BB across all physical cores is a global deadlock (FATAL `m_waitingCores.count() != getNumPhysicalCores()`). **[CONFIRMED — see [dispatch §2.1](sim-dispatch-state.md#21-the-per-bb-driver-is-semaphore-ordered-not-a-linear-walk) for the driver loop.]**

---

## 4. Call/return frame teardown & exit

### 4.1 Frame state

`Call` (§2.4) pushes onto two `std::deque`s inline in the visitor: callsites (`this[7..10]`, i.e. begin/end/node-map cursors of one deque) and callers (`this[17..20]`). The deque node size is `0x200` bytes. These are the activation-frame stack.

### 4.2 `visitInstReturn` (IT 81) `@0x1aa560`

`Return` pops both deque frames, clears the function's entire local sync state, and nulls the next-BB slot so the callee region walk terminates:

```c
// birsim::InstVisitor::visitInstReturn  @0x1aa560  (CONFIRMED)
pop callsites_deque (this[7..10]);    // frees a 0x200 node on block boundary, advances cursors
pop callers_deque   (this[17..20]);   // ditto
SyncState::clearAllSemaphores((SyncState*)(this + 416 /*52 qwords = +0x1A0*/));   // wipe sem bank
this[450] /*+0xE10*/ = 0;             // next-BB slot -> 0 (return = no successor in callee CFG)
```

Clearing all semaphores on return discards the callee's local synchronization state before control unwinds back to the caller's recursion level (the `visit()` in §2.4 returns). **[CONFIRMED — body @0x1aa560: deque pops + `clearAllSemaphores(this+416)` + `this[450]=0`.]**

### 4.3 `visitInstExit` (IT 82) `@0x1af7b0`

`Exit` logs `"InstExit Instruction encountered, exiting..."` then, on a **single-core** (non-LNC) run, throws `neuronxcc::backend::InstExitException` (via `__cxa_throw`) which unwinds the whole simulation; on **LNC** (multi-core) it returns plainly, and the multi-engine exit join (`fetchAllEngineInst<InstExit> @0x16af90`, a counted scheduler rendezvous) collects all engines' `Exit` before stopping. **[CONFIRMED — body @0x1af7b0: `isLNC` branch, the throw on the non-LNC arm.]**

---

## 5. Sync opcodes — mostly empty kernels

The architectural result: most sync *opcodes* carry their entire meaning in the base `SyncInfo` (§3.3); the kernel body itself does nothing. `EventSemaphore` (13) and `AllEngineBarrier` (15) have **no `visitInst` symbol at all** ([dispatch noop class](sim-dispatch-state.md#12-the-four-routing-classes-sums-to-110)); `Drain` (16) `@0x1aa160`, `Halt` (17) `@0x1aa170`, `CoreBarrier` (87) `@0x1aa720`, `TensorCompletion` (94) `@0x1aa450` all have **empty (`;`) bodies**. Their happens-before is the on_wait/on_update sets the synchronizer attached. Only three sync kernels do real work.

### 5.1 `visitInstGroupResetSemaphores` (IT 14) `@0x1aa140`

Bulk write-zero of a named semaphore group, all local. It ignores the `mode` field (`InstGroupResetSemaphores+0xF0`, an `EventSemaphoreClearMode` that is a codegen/HW concern) and unconditionally writes 0 to each id:

```c
// visitInstGroupResetSemaphores  @0x1aa140  -> clearGroupSemaphores @0x19e750  (CONFIRMED)
for (id in InstGroupResetSemaphores.sema_group /*+0xF8 = inst+248*/)
    Semaphores::actOn(Update{ type=1 /*semaphore*/, id, mode=5 /*sem-wr-imm*/, value=0, no_core });  // = set(id, 0)
```

The `Update` ctor args are pinned in the asm (`esi=1, edx=id, ecx=5, r8d=0, r9=0`). **[CONFIRMED.]**

### 5.2 `visitInstSwitchQueueInstance` (IT 85) `@0x1aa210` / `visitInstResetQueueInstance` (IT 86) `@0x1aa350`

Both rewind a DMA queue's round-robin descriptor cursor by tombstoning the owning [`DMATrigger`](sim-elementwise-datamove.md)'s entry in the `fireCount` DenseMap (`InstVisitor+0xE60`/`+0xE68`/`+0xE70` — the same map `visitInstDMATrigger` advances per fire; tombstone sentinels `-8192` empty / `-4096` tombstone, live count `+0xE68`). `Switch` additionally clears two queue-instance semaphore groups and honours a `no_rearm` flag:

```c
// visitInstSwitchQueueInstance  @0x1aa210  (CONFIRMED — disasm)
queue = *(void**)(inst + 0xF0);
clearGroupSemaphores(SyncState, queue + 0xB8);         // queue.use_special_sema group -> set 0
clearGroupSemaphores(SyncState, queue + 0xD0);         // queue.semaphores group        -> set 0
if (*(uint8_t*)(inst + 0x100) /*no_rearm*/) return;    // switch WITHOUT rearm: leave cursors
for (blk in InstSwitchQueueInstance.instance /*+0xF8*/) {
    if (InstDMABlock::classof(blk)) {
        trig = InstDMABlock::getTrigger(blk);
        fireCount.erase(trig);                         // tombstone -> next fire restarts at block 0
    }
}

// visitInstResetQueueInstance  @0x1aa350  (CONFIRMED) — cursor rewind only, NO sema clears
for (blk in InstResetQueueInstance.instance /*+0xF8*/)
    if (InstDMABlock::classof(blk)) fireCount.erase(InstDMABlock::getTrigger(blk));
```

So `SwitchQueueInstance` = reset the queue's two semaphore groups (so the new instance's completion signalling starts fresh) **and** (unless `no_rearm`) rewind every owning trigger's block cursor to 0. `ResetQueueInstance` is the cursor-only subset. The two `clearGroupSemaphores` calls and the `InstDMABlock::classof`/`getTrigger` loop are visible in the disasm at `0x1aa232`/`0x1aa248`/`0x1aa284`/`0x1aa291`. **[CONFIRMED.]**

---

## 6. Kernel & custom-op stubs — the three execution models

This is the high-value correction. The naive expectation — "a custom op is opaque, so the simulator stubs it" — is **wrong**. The dispatch routes the kernel/custom-op family to three distinct execution models, and only two leaves are genuinely stubbed. (The `sameInst` "Not Implemented" asserts that some pages associate with these opcodes are strictly a structural-equality/CSE limitation on `{55,56,84}` — they do **not** imply the simulator skips the op.)

| IT | Op | Model | Body | Simulated? |
|---:|----|-------|-----:|-----------|
| 53 | `CustomOp` | A — external `kernel_sim` | `0x1abbd0` | **yes** (out-of-process delegate) |
| 54 | `BIRKernel` | A — external `kernel_sim` | `0x1abf50` | **yes** (+ `kernelInst2KernelConfig`) |
| 55 | `NKIKernel` | B — inline BIR expansion | `0x212930` | **yes** (synthetic `Call`+`Return`) |
| 56 | `NKIKLIRKernel` | C — skip + note | `0x1aa750` | **no** — `klir_binary` never executed |
| 93 | `InlineASMBytes` | C — skip + note | `0x1aa750` | **no** — `asm_bytes` never interpreted |

### 6.1 Model A — `CustomOp`/`BIRKernel` shell out to `kernel_sim`

Both kernels are thin marshallers around the shared delegate `simulateKernel`. `visitInstCustomOp` builds the dst-shape list from `inst[54..55]` (`CustomOp.dstsShape @+0x1B0`) and src-shape list from `inst[51..52]` (`srcsShape @+0x198`), passes the kernel name at `inst+30` (byte `+0xF0` = `CustomOp.opFunctionName`), then calls the delegate:

```c
// visitInstCustomOp  @0x1abbd0  (CONFIRMED)
dsts = build_shape_list(inst[54], inst[55]);           // per-elem ctor sub_1A2CA0
srcs = build_shape_list(inst[51], inst[52]);
simulateKernel(this, inst, inst + 30 /*kernel name string @+0xF0*/, srcs, dsts);
// then free both temp shape vectors — ALL simulation is inside simulateKernel.
```

`visitInstBIRKernel @0x1abf50` is structurally identical with the BIRKernel operand offsets (`dsts_shape @+0x128 = inst[37..38]`, `srcs_shape @+0x110 = inst[34..35]`, `kernel_name @+0xF0`). The delegate runs the external CPU reference implementation:

```c
// birsim::InstVisitor::simulateKernel  @0x26ecd0  (CONFIRMED)
runId = dword_2297380++;                                // global monotonic counter for unique filenames
print("Running kernel_sim for <kernelName>");
for (idx in [0, numInputs)) {                          // INPUT MARSHALLING
    pap = getInOutPhysicalAP(this, inst, idx, /*isOut=*/0);
    mo  = Memory::read(this->memory /*+0x138*/, pap, 0);  // vtable+24
    mo.reshape(input_shape);
    save_npy(mo, "<...>-<idx>-birsim.npy");
}
if (inst.InstructionType == 54)                        // BIRKernel only
    kernel_sim_params::kernelInst2KernelConfig(inst, jsonConfig);   // @0x26e4f0
emit nlohmann::json{ kernel_name, srcs/dsts shapes, npy paths };    // bir::saveJsonFile @0x26fba6
if (system("kernel_sim") != 0) {                       // FORK the external CPU executable  @0x26fc14
    std::cerr << "kernel_sim failed"; exit(1);
}
for (idx in [0, numOutputs)) {                         // OUTPUT UNMARSHALLING
    mo  = load_npy_to_object("<...>-<idx>-simout.npy"); // @0x297e00
    opap = getInOutPhysicalAP(this, inst, idx, /*isOut=*/1);
    Memory::write(this->memory, opap, mo, inst, 0);    // vtable+72 -> write result into output AP
}
```

The simulator itself does **not** model the kernel math — it serializes the I/O tensors to disk, runs the external `kernel_sim`, and reads the result back. Dispatch to a specific custom op is by **name** (`CustomOp.opFunctionName`) carried in the JSON config; the CPU builtin library `libbuiltincustomop_cpu0.stripped.so` (a sibling IDA target) is the inferred resolver `kernel_sim` links. **[CONFIRMED for the model — `system("kernel_sim")` @0x26fc14, the `inst.IT == 54` branch at decompile line 616, the npy save/load and `getInOutPhysicalAP`/`Memory` calls; INFERRED for the exact argv beyond the static `"kernel_sim"` string and the name→entry binding inside `kernel_sim`.]**

### 6.2 Model B — `NKIKernel` inline BIR-subfunction expansion

`NKIKernel` is far from a stub: it **expands** the kernel's embedded BIR `Function` in place by synthesizing a `Call` to it and a matching `Return`, dispatching both through the visitor's own machinery (§2.4):

```c
// birsim::InstVisitor::visitInstNKIKernel  @0x212930  (CONFIRMED)
if (!*(void**)(inst + 240 /*+0xF0 = NKIKernel.func*/))
    throw NeuronAssertion("I.getFunc() != nullptr");   // inst_visitor.cpp:7721, ErrorCode 250
callInst = buildInstCall(this, inst);                  // synthesize bir::InstCall to the embedded Function (§6.2.1)
if (this[37]) (*vtable+16)(this[37], callInst);        // progress/printer callback hook
visit(this, callInst);                                 // dispatch the Call -> IT84 -> visit(Function&): RECURSE callee BBs
funcBB  = *(BasicBlock**)(inst + 80);
retInst = NamedObjectContainer::insertElement<InstReturn>(funcBB, ...);   // synthetic Return
if (this[37]) (*vtable+16)(this[37], retInst);
visit(this, retInst);                                  // execute the Return (frame pop, §4.2)
BasicBlock::removeInstruction(funcBB, retInst);        // tear down both synthetic insts
BasicBlock::removeInstruction(funcBB, callInst);
this[449] /*+0xE08*/ = inst;                           // current-NKI-inst slot
```

So the NKI kernel's BIR sub-function is executed by the simulator's region-recursion machinery — **not** shelled out, **not** stubbed. **[CONFIRMED — body @0x212930: the `getFunc()` guard, `buildInstCall`, the two `visit()` calls, the two `removeInstruction` teardowns.]**

#### 6.2.1 `buildInstCall` `@0x1c2b70`

(Hex-Rays failed; from disasm.) Creates the `InstCall` node (`NamedObjectContainer<BasicBlock,Instruction>::insertElement<InstCall>`), builds a `bir::FunctionArgumentMap`, and wires the NKIKernel's src/dst `MemoryLocationSet`s to the callee `Function`'s parameters via `FunctionArgumentMap::setMapping(MemoryLocationSet*, MemoryLocationSet*)` — threading the kernel's tensors into the synthetic call's argument map. **[CONFIRMED — disasm call sequence.]**

### 6.3 Model C — base "skip + note" (the genuine stubs)

The base handler `visitInstruction @0x1aa750` ([dispatch base class](sim-dispatch-state.md#12-the-four-routing-classes-sums-to-110)) records the opcode name into the visitor's diagnostic buffer (`InstVisitor+0xA8`) and skips — **no** numeric effect, **no** FATAL:

```c
// base visitInstruction  @0x1aa750  (CONFIRMED)
s = bir::InstructionType2string(I.IT);
if (s.size() == SENTINEL) throw length_error;
s.append(";");                                         // asc_58FBA8 = ";"
diagBuf(this + 0xA8).append(s);                        // "<OpcodeName>;" into the diag string
goto leaveInstruction;                                 // loc_F7FDC — silent skip
```

`NKIKLIRKernel` (56) routes here → `"NKIKLIRKernel;"` note; its `klir_binary` blob (`+0xF0`) is never executed. `InlineASMBytes` (93) routes here → `"InlineASMBytes;"` note; its raw `asm_bytes` blob is never interpreted by the functional simulator. (Neither has a dedicated `visitInst` body — only `bir::Hwm::getLatency*<…>` timing-model bodies exist for them.) The standalone `nki_klr_sim` CLI is the tool that *does* run KLIR-lowered kernels, but via the `kernel_sim`/external path — not in this in-process interpreter. **[CONFIRMED — base handler body; the absence of `visitInstNKIKLIRKernel`/`visitInstInlineASMBytes` symbols.]**

---

## 7. Function map

| Function | Address | Role |
|----------|--------:|------|
| `visitLoop` (IT 105) | `0x211010` | static `for`; induction → affine env DenseMap |
| `visitDynamicForLoop` (IT 106) | `0x211690` | runtime trip; QAExpr lb/stride + register ub |
| `visitDoWhile` (IT 108) | `0x2120c0` | post-tested; register predicate |
| `evaluateUpperBoundExpr` | `0x1b52c0` | `ub = const + coef·RegState.read(reg)` |
| `evaluateDoWhileCondition` | `0x1a99b0` | `RegState.read(reg) != 0` |
| `visitInstLoopParallel` | `0x20cc60` | parallel-axis body collector |
| `visitInstInParallelLoop` | `0x20c940` | parallel-axis leaf executor |
| `visitInstCompareAndBranch` (IT 78) | `0x1bffb0` | predicate → next-BB slot |
| `compareScalarArgs` | `0x1bce50` | per-Dtype `BranchCompareOp` comparator |
| `visitInstUnconditionalBranch` (IT 79) | `0x1aa550` | slot ← target |
| `visitInstBranchHint` (IT 80) | `0x1aa650` | `ret` (no-op) |
| `visitInstReturn` (IT 81) | `0x1aa560` | pop frame + clear sems + slot ← 0 |
| `visitInstExit` (IT 82) | `0x1af7b0` | throw `InstExitException` / LNC return |
| `visitInstBreak` (IT 83) | `0x1aa640` | slot ← 0 (exit region) |
| `visitInstCall` (IT 84) | `0x26bc90` | push frame + recurse callee region |
| `visitBBHolderInControlFlow` | `0x20f820` | CFG driver — follows next-BB slot |
| `SyncState::needWait` | `0x19e560` | poll wait predicate |
| `SyncState::actOnWait` | `0x1a1750` | consume waits (clear events) |
| `SyncState::actOnUpdate` | `0x1a0300` | apply updates + cross-core route |
| `Semaphores::needWait` / `actOn(Update&)` | `0x19e4b0` / `0x19e300` | counting-sema wait / 5-mode update |
| `Events::actOn(Wait&)` / `actOn(Update&)` | `0x1a1650` / `0x19fd00` | event clear / set |
| `visitInstGroupResetSemaphores` (IT 14) | `0x1aa140` | bulk write-zero sema group |
| `visitInstSwitchQueueInstance` (IT 85) | `0x1aa210` | clear 2 sema groups + rewind triggers |
| `visitInstResetQueueInstance` (IT 86) | `0x1aa350` | rewind trigger cursors only |
| `visitInstCustomOp` (IT 53) | `0x1abbd0` | marshal → `simulateKernel` |
| `visitInstBIRKernel` (IT 54) | `0x1abf50` | marshal (+config) → `simulateKernel` |
| `simulateKernel` | `0x26ecd0` | `system("kernel_sim")` external delegate |
| `visitInstNKIKernel` (IT 55) | `0x212930` | inline BIR expansion (Call+Return) |
| `buildInstCall(InstNKIKernel&)` | `0x1c2b70` | synth `InstCall` + arg map |
| base `visitInstruction` | `0x1aa750` | "skip + note" (NKIKLIR/InlineASM stubs) |

---

## 8. Adversarial self-verification

The five strongest claims were re-checked against the decompiled/disassembled bodies:

1. **Loop carried-state threading.** `visitLoop @0x211010` writes the induction value into a `DenseMap<pelican::AffineIdx*, long>` at `InstVisitor+0xE18` keyed by the axis pointer (`*((_QWORD*)inst+42)` = `+0x150`), walks the region BB list, increments by `stride` (`v52` = axis `+0x30`), and continues while `ub` (axis `+0x28`) exceeds the induction. The inlined DenseMap asserts name the exact `KeyT=pelican::AffineIdx*; ValueT=long int` instantiation. **VERIFIED. [CONFIRMED.]**
2. **Do-while register predicate.** `evaluateDoWhileCondition @0x1a99b0` ends in `RegState::read((RegState*)(this+336), *(Register**)(cond+64), 0) != 0`, where `cond = inst[41]` (`+0x148`) cast to `bir::BirIntRuntimeValue`; `visitDoWhile @0x2120c0` loops `while ((uint8_t)result)`. **VERIFIED. [CONFIRMED.]**
3. **Dynamic-for runtime upper bound.** `evaluateUpperBoundExpr @0x1b52c0` returns `*(int*)(ubExpr+56) + RegState::read(this+336, *(Register**)(term+64), 0) * coef`, gated by the `ubExpr->getNumTerms()==1 && ubExpr->hasRuntimeValue()` assert (ErrorCode 1910, `"Only scalar values supported in Dynamic For Loop upper bound"`). **VERIFIED. [CONFIRMED.]**
4. **Custom-op `kernel_sim` shell-out.** `simulateKernel @0x26ecd0` contains a literal `system("kernel_sim")` (decompile line 836) with `std::cerr << "kernel_sim failed"; exit(1)` on non-zero return, preceded by `.npy` input marshalling and followed by `load_npy_to_object` + `Memory::write` output unmarshalling; the `inst.IT == 54` branch (line 616) gates `kernelInst2KernelConfig`. **VERIFIED. [CONFIRMED.]**
5. **NKIKernel inline expansion.** `visitInstNKIKernel @0x212930` guards `getFunc() != nullptr` (cp:7721, EC 250), calls `buildInstCall`, then `visit(callInst)` → `insertElement<InstReturn>` → `visit(retInst)` → two `removeInstruction` teardowns. It dispatches through the same `Call` path (IT 84 `@0x26bc90`) any `Call` uses. **VERIFIED. [CONFIRMED.]**

The intercept claim from [dispatch §1.1](sim-dispatch-state.md#11-the-pre-switch-control-flow-interception) (105/106/108 pulled out before the switch via `cmp eax,0x69/0x6A/0x6C` tail-jumps) holds: each of the three bodies begins with its own `enterInst<X>` hook and is reached only via those tail-jumps, never the switch table's dead slots.

**Honest re-verify ceiling.** Pinned at the strongest level: all three loop kernels' carried-state/predicate/bound logic, the terminator next-BB-slot effects, the Return frame-pop + sem-clear, the two-bank SyncState wait/update state machine, the three queue/group sync kernels, and the three custom-op execution models (delegate / inline-expand / stub) — each read off a decompiled or disassembled body. **Not** fully pinned: the `sub_1BE610` rhs immediate-vs-register split in `CompareAndBranch` (read at call-site only — INFERRED from the dual-arg comparator); the per-`BranchCompareOp` comparator function identities (INFERRED standard relational); the exact `kernel_sim` argv beyond the static `"kernel_sim"` string and the name→entry resolution inside the external executable; the `neuronxcc::backend::CFG` node/edge layout the CFG driver builds; and the per-element parallel-loop AP indexing in `visitInstInParallelLoop`. No `birsim::*` types exist in the IDA `structures.json` — the field offsets are reconstructed from constructor writes, accessor bodies, and the inlined LLVM `DenseMap`/`cast<>` assert instantiations, which name the exact key/value types and so anchor the offsets unambiguously.

---

## See also

- [BIR Sim Dispatch + the Whole-Machine State Model](sim-dispatch-state.md) — the 110-case switch, the pre-switch CF interception, the `InstVisitor`/`Memory`/`RegState`/`SyncState` field maps this page builds on.
- [Sim Core Arithmetic — Cast / Accumulate / RNE / MemoryReductionOp](sim-core-arithmetic.md) — shared scalar/accumulate arithmetic the loop bodies invoke.
- [The `bir::Instruction` base struct & the `+0xD0` sched/dep block](instruction-base.md) — the `SyncInfo` the per-instruction sync engine reads wait/update sets from.
- [`InstructionType` — the 110-opcode enum & `sameInst` family masks](instruction-type.md) — the `Instruction+0x58` discriminant the dispatch keys on.
- [ALU/branch-compare op modes](aluop-modes.md) — the `BranchCompareOp`/`UpdateMode`/`WaitMode` enums these kernels switch on.
