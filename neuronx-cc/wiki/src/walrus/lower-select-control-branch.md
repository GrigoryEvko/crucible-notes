# Select, Control, and Branch Lowering

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, binary `neuronxcc/starfish/lib/libwalrus.so` (the cp310 wheel; cp311/cp312 carry the same backend re-linked elsewhere). `.text` is VA==fileoffset for the backend (`>=0x62d660`), `.rodata` from `0x1c72000`; plain `.data` carries a `+0x400000` delta. **Two-VA-frame note:** every `._ZN…` symbol in the `0x5e9020–0x62d650` band is a 6-byte `.plt` re-export thunk; the real bodies live high (`0xbexxxx`, `0x118xxxx`, `0x11bxxxx`). Addresses below are always the real bodies. Treat every address as version-pinned.*

## Abstract

Three distinct walrus passes turn `bir` ops that an engine cannot encode directly into ops it can. They are easy to conflate — the task that motivated this page conflated all three — so the first job here is to keep them apart. **`lower_select`** (order 64, pre-codegen) rewrites the ternary `Select(51)` op into a `GenericCopy(1)` + `CopyPredicated(52)` pair, because `Select` has no machine emitter. **`lower_control`** (order 128, the last pre-codegen pass) is **not** a branch emitter: it is a per-basic-block scheduling-fence pass that erases cross-BB data dependencies and inserts a fixed barrier/drain sequence before every terminator, at the basic-block ordering granularity. **`lower_branch`** (low-level order L20, post-codegen, after the per-engine split) is the pass that actually emits SP-engine branch micro-ops — predicate compares, loop back-edges, pre-headers.

The reference frame is LLVM's lowering pipeline. `lower_select` is a legalization pass exactly like LLVM's `expand-select` / `select → cmov` expansion: an op with no target instruction is split into target-legal primitives. `lower_control` has no clean LLVM analogue — it is closer to a software-fence insertion pass on a VLIW/spatial machine, where crossing a control edge requires every engine to quiesce. `lower_branch` is the structured-CFG-to-machine-branch lowering (LLVM's `StructurizeCFG` reversed: structured loops are *exploded* into explicit branches), specialized to the SP sequencer's `BranchCompareOp` encoding.

This page documents the three passes in pipeline order, the `Select → GenericCopy + CopyPredicated` rewrite as annotated pseudocode, the order-128 fence sequence (`AllEngineBarrier · GroupResetSemaphores · AllEngineBarrier · Drain · [CoreBarrier]`) and the 128-granularity it operates at, and `addBranchToLoop` — how a structured loop gets its back-edge SP branch.

For reimplementation, the contract is:

- The `Select` lowering rule: seed the destination with `onFalse` via an unconditional `GenericCopy`, then overwrite the true lanes via a predicated copy — and *why* it splits into exactly two ops.
- The order-128 fence: which instructions are collected, which are skipped, the exact 4-(+1)-op sequence inserted, and the `lnc_size>1` multi-core epilogue.
- The boundary: `lower_control` decides *where* control flows (op-level, all engines, pre-codegen); `lower_branch` decides *how* the SP sequencer encodes that flow (SP micro-ops, post-split).
- `addBranchToLoop`: structured `Loop(105)` → `{pre-header, body, counter-vs-limit back-edge}` SP branches with per-engine counter registers.

| | |
|---|---|
| **`lower_select`** | `LowerSelectPass::run(bir::Module&)` @ `0xbef6b0` (order 64, pre-codegen) |
| ↳ core rewrite | `SplitSelectVisitor::visitInstSelect(bir::InstSelect&)` @ `0xbf1b30` |
| **`lower_control`** | `LowerControl::run(bir::Module&)` @ `0x118cab0` (order 128, last pre-codegen) |
| ↳ fence insert | `LowerControlImpl::leaveBasicBlock(bir::BasicBlock&)` @ `0x118e0c0` |
| ↳ inst collector | `LowerControlImpl::visitInstruction(bir::Instruction&)` @ `0x118d4c0` |
| **`lower_branch`** | `LowerBranch::run(bir::Module&)` @ `0x11c02d0` (low-level L20, post-codegen) |
| ↳ loop back-edge | `LowerBranch::addBranchToLoop(bir::BasicBlock&)` @ `0x11be560` (0x17a0 B, heaviest helper) |
| ↳ predicate → branch | `LowerBranch::convertPredToBranch(bir::BasicBlock&)` @ `0x11bca90` |
| **Source path** | `…/neuronxcc/walrus/lower_control/src/lower_control.cpp` (`.rodata`, CONFIRMED) |
| **Key opcodes** | `Select=51`, `GenericCopy=1`, `CopyPredicated=52`; `AllEngineBarrier=15`, `GroupResetSemaphores=14`, `Drain=16`, `CoreBarrier=87` |

---

## 1. `lower_select` — Select → GenericCopy + CopyPredicated

### Purpose

`Select(51)` is the ternary `out = preds ? onTrue : onFalse`. It is a DVE-family op conceptually, but it has **no ISA encoder** anywhere in the binary — there is no `CoreV{2,3,4}GenImpl::visitInstSelect`, only sim, verifier, const-prop, and simplify handlers (CONFIRMED — the only `visitInstSelect` emitter-class symbol absent from the `CoreV*GenImpl` family). By contrast `CoreV2GenImpl::visitInstCopyPredicated` @ `0x125eb20` *is* a real DVE bundle emitter. `lower_select` therefore exists to rewrite every `Select` into the two encodable primitives before codegen runs. It is the **sole and final** `Select` legalizer: nothing else in the pipeline splits a `Select`, and no pass after order 64 re-introduces one, so codegen never sees opcode 51.

> **CORRECTION (H20-C2) —** an earlier analysis attributed the `SplitSelectVisitor` split to `early_peephole_opts` (order 31). That is wrong. `EarlyPeepholeOpts::run` @ `0xbf81c0` calls only `ActivationAccumulate` fusion and `SimplifyPermuteImplicitReduceAdd` — its body contains zero `SplitSelect` call sites. `LowerSelectPass::run` @ `0xbef6b0` is the *only* function in the entire binary that calls `IRVisitor<SplitSelectVisitor>::visit` (call sites `0xbef86b`, `0xbef9dd`, `0xbefa79`). There is no early-peephole Select split; all Selects are lowered at order 64.

### Entry Point

```text
LowerSelectPass::run(bir::Module&)                 @0xbef6b0  ── order-64 driver
  └─ IRVisitor<SplitSelectVisitor>::visit(beg,end)  @0xbf2240  ── recursive op walk
       └─ SplitSelectVisitor::visitInstSelect(...)  @0xbf1b30  ── the rewrite (1798 B)
            ├─ replaceInstWithInstArr<InstGenericCopy>  @0xbf1038  ── seed out←onFalse
            └─ insertElement<InstCopyPredicated>        ── overwrite true lanes
```

### Algorithm

`run` is a logging/driver shell around the recursive visitor; the substance is in `visitInstSelect`. The visitor descends into structured-control bodies (`Loop=105`, `DynamicForLoop=106`, `DoWhile=108`) so a `Select` nested at any loop depth is still rewritten; all other leaf opcodes are skipped, and an unknown opcode trips `llvm_unreachable("Unknown Instruction type encountered!")` (CONFIRMED — string lives in `.rodata`).

```c
// LowerSelectPass::run  @0xbef6b0  (CONFIRMED, 1151 B)
function LowerSelectPass_run(Module& M):
    Logger.pushAttr("module_name", M.getName())       // boost-log scope only
    SplitSelectVisitor V;                              // V.count=0, V.removal=∅
    fn = M.getFunctionByName("")                       // entry fn @0xbef832; else iterate all BBs
    for region in fn.topLevelRegions:
        IRVisitor<SplitSelectVisitor>(V).visit(region.begin(), region.end())   // @0xbf286b
        for inst in V.removal:                         // drained after each region
            inst.parentBB.removeInstruction(inst)      // delete the now-detached Selects
        V.removal.clear()
    addMetric(BackendMetricType=38, M.name, V.count, agg)   // @0xbef902 — "lower_select" stat
```

The core rewrite reproduces `out = preds ? onTrue : onFalse` as two ops: an **unconditional** copy that seeds the destination with the false branch, then a **predicated** copy that overwrites only the lanes where the mask is set with the true branch.

```c
// SplitSelectVisitor::visitInstSelect  @0xbf1b30  (CONFIRMED, 1798 B; read at instruction level —
//   Hex-Rays failed on this body, so all field offsets are disasm-pinned)
function visitInstSelect(InstSelect& S):
    preds   = S.getArgument<AccessPattern>(0)   // arg0 — the 0/1 MASK access pattern  @0x625b20
    onTrue  = S.getArgument(1)                  // arg1 — Argument (AP or scalar imm)  @0x60a5f0
    onFalse = S.getArgument(2)                  // arg2 — Argument (AP or scalar imm)  @0x5fe770
    out     = S.getOutput<AccessPattern>(0)
    sT = onTrue.isScalar();  sF = onFalse.isScalar()        // @0xbf1b86 / 0xbf1b92
    immPath = sT && checkImmValueTypeCompatible(onTrue.dtype, onFalse.dtype)   // @0xbf1bb9

    // (1) SEED the destination with the FALSE branch, replacing S in place:
    gc = replaceInstWithInstArr<InstGenericCopy>(&S, {name(S)+"-copy"}, false, false)  // @0xbf1038
    gc.flag90 = 0                                          // @0xbf1c5b
    onFalse.cloneToArgument(gc, /*append=*/true)           // gc.arg ← onFalse        @0xbf1c68
    out.cloneToOutput(gc, true)                            // gc.out ← out (unconditional copy)

    // (2) CONDITIONALLY OVERWRITE the TRUE lanes, just after the GenericCopy:
    ip = S.parentBB.checkInsertionPointValid(after gc)     // @0xbf1cdb
    cp = S.parentBB.insertElement<InstCopyPredicated>(ip, name(S)+"-predicated")  // @0xbf1ceb
    preds.cloneToArgument(cp, true)                        // cp.arg0 ← mask          @0xbf1d2a
    onTrue.cloneToArgument(cp, true)                       // cp.arg1 ← onTrue        @0xbf1d3a
    out.cloneToOutput(cp, true)                            // cp.out  ← out (SAME dst) @0xbf1d4a
    cp.flag90 = S.flag90                                   // carry the 32-bit flag   @0xbf1d4f
    if immPath:  cp.useFalseFill = 1; cp.varTag138 = 0     // PATH B: scalar-imm false fill @0xbf2079
    else:        cp.predScalarBool = 1; cp.varTag110 = 0   // PATH A: plain bool predicate  @0xbf1d7d
    if S.debugInfo: cp.setDebugInfo(S.debugInfo)           // @0xbf1da7

    rewireDeps(&S, {gc}, true, true)                       // redirect every use of S  @0xbf1dfb
    V.removal.push_back(&S);  V.count++                    // queue S for deletion      @0xbf1e1c
```

The net effect across the two ops is, per lane `i`:

```text
GenericCopy:     out[i] = onFalse[i]                       ∀ i        (unconditional seed)
CopyPredicated:  out[i] = preds[i] ? onTrue[i] : out[i]    (overwrite where pred set)
  ⟹            out[i] = preds[i] ? onTrue[i] : onFalse[i]              (Select semantics)
```

This byte-matches the executable sim `Select` golden (`if pred==1: out=onTrue; elif pred==0: out=onFalse; else: runtime_error`): the GenericCopy supplies the `pred==0` lane, the CopyPredicated supplies the `pred==1` lane. **STRONG.**

> **QUIRK —** the mask is **not** computed here. `arg0` (`preds`) is consumed verbatim as a pre-existing strict-0/1 tensor — the visitor never synthesizes a comparison or a 0/1 conversion. The producing compare (a `TensorTensor`/`TensorScalar` compare → 0/1 tensor) ran upstream. A reimplementation that expects `lower_select` to materialize the predicate will produce wrong results for every `Select`.

### The Two Paths

Both paths build the same two instructions; they differ only in how the false value is supplied and which `std::variant` discriminator bytes on the `CopyPredicated` are set.

| Path | Gate | False supply | `CopyPredicated` variant bytes |
|---|---|---|---|
| **A — main** | tensor `onTrue`/`onFalse`, or scalar but dtype-incompatible | `GenericCopy` seeds the false tensor; CopyPredicated leaves false lanes untouched | `+0xF0 = 1` (scalar-bool predicate), `+0x110 = 0` |
| **B — imm-compatible** | `onTrue.isScalar() && checkImmValueTypeCompatible(onTrue,onFalse)` | `onFalse` is a scalar immediate the CopyPredicated can broadcast-fill directly into the false lanes | `+0x118 = 1` (`useFalseFill`), `+0x138 = 0` |

The four byte offsets are `std::variant`/predicate discriminators, not data values (cross-checked against the `InstCopyPredicated` `sameInst` guards on bytes +240/+272/+280/+320). PATH B's `useFalseFill` (byte +280) is the same field the sim's CopyPredicated golden reads when `pred==0: dst[i]=optImm[i]`. **STRONG.**

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `LowerSelectPass::run` | `0xbef6b0` | order-64 driver; sole caller of the visitor | CERTAIN |
| `SplitSelectVisitor::visitInstSelect` | `0xbf1b30` | the Select→GenericCopy+CopyPredicated rewrite | CERTAIN |
| `IRVisitor<SplitSelectVisitor>::visit` | `0xbf2240` | recursive op walk; descends Loop/DynForLoop/DoWhile | CERTAIN |
| `replaceInstWithInstArr<InstGenericCopy>` | `0xbf1038` | in-place replace Select with the seed copy | HIGH |
| `CoreV2GenImpl::visitInstCopyPredicated` | `0x125eb20` | the DVE ISA emitter for the lowered CopyPredicated | CERTAIN |
| `KlirToBirCodegen::codegenCopyPredicated` | `0xf1a9c0` | KLIR→BIR codegen path for CopyPredicated | CERTAIN |

> **NOTE —** the prior report cited `CoreV2GenImpl::visitInstCopyPredicated @0x5eb690` and `KlirToBirCodegen::codegenCopyPredicated @0x5f8080`; those are the `.plt` thunk-frame addresses. The real bodies are `0x125eb20` and `0xf1a9c0` (two-VA-frame artifact). Both pairs name the same symbol.

---

## 2. `lower_control` — the order-128 scheduling fence

### Purpose

`lower_control` is the **last** pre-codegen pass (order 128) and is widely mis-described as the structured-control-to-branch lowering. It is not. Its source path `…/neuronxcc/walrus/lower_control/src/lower_control.cpp` is intact in `.rodata`, and the body emits **no SP branches and no register-ALU micro-ops**. What it does is install the per-basic-block synchronization boundary that makes a control edge safe: it (a) erases every cross-BB data dependency module-wide, then (b) inserts a fixed barrier/drain fence immediately before each real terminator and chains every real instruction in the block into that fence. The structured carriers `Loop(105)`/`DynamicForLoop(106)`/`DoWhile(108)` are descended but left **structurally intact** — their explosion into SP back-edges is deferred to `lower_branch` (§3).

> **CORRECTION (H35-1.1) —** the SP-branch work (`convertPredToBranch`, `addBranchToLoop`, `addLoopEntry`, loop-counter register init) lives in a distinct class `LowerBranch` (@ `0x11c02d0` etc.), added to the pipeline *after* codegen by `sub_805870`. `lower_control` is op-level and multi-engine; `lower_branch` is SP-microcode. They are different passes at different pipeline positions on different opcodes (§3 boundary).

### The 128 — what the granularity is

The "128" is the pass's **pipeline order number**, not a tile size or a lane count. `lower_control` is registered at builder slot 128 (the tail of the pre-codegen pipeline `sub_80A6E0`, added via `addModParallelPass` — module-parallel granularity, matching its `run(bir::Module&)` signature), gated only by `!unk_3DFA5B8` (a default-off skip flag, so the pass **runs by default**). Slots 130 (`lnc_barriercheck`) and 131 (`bir_racecheck`) follow it and *verify* the barriers it mints. The ordering granularity at which the pass operates is the **basic block**: the fence is inserted once per BB that ends in a real terminator, and the cross-BB dependency erasure means inter-BB order is thereafter carried *solely* by these fences. **CONFIRMED.**

### Entry Point

```text
LowerControl::run(bir::Module&)                       @0x118cab0  ── driver
  ├─ EraseInterBbDeps(Module&, Logger&)               @0x118c310  ── STEP 1 (whole module)
  ├─ IRVisitor<LowerControlImpl>::visit(Instruction&) @0x118e940  ── opcode dispatch (descends 105/106/108)
  │    └─ LowerControlImpl::visitInstruction(...)      @0x118d4c0  ── collect real leaf insts
  ├─ LowerControlImpl::leaveBasicBlock(BasicBlock&)    @0x118e0c0  ── STEP 2 fence insert (per BB)
  └─ renumberCoreBarriers(Module&)                    @0x1089250  ── STEP 3 (iff lnc_size>1)
```

### Algorithm

```c
// LowerControl::run  @0x118cab0  (CONFIRMED)
function LowerControl_run(Module& M):
    Logger.pushAttr("module_name", M.getName())
    EraseInterBbDeps(M, Logger)                        // §2.1 — drop all cross-BB data deps
    impl = LowerControlImpl{ worklist = ∅ }
    F = M.getFunctionByName("main")                    // "main" processed first, then the rest
    for G in [F] ++ (M.functions \ {F}):
        for BB in G.blocks:
            impl.worklist.clear()
            for inst in BB.instructions:
                IRVisitor<LowerControlImpl>::visit(inst)   // collect (descends carriers)
            impl.leaveBasicBlock(BB)                        // §2.2 — insert the fence
    if M.config.lnc_size > 1:                          // cmpl $1, 0x1a4(cfg)  @0x118e2b6
        renumberCoreBarriers(M)                        // re-index every CoreBarrier(87) module-wide
```

The opcode dispatch descends the three structured carriers but does not rewrite them; everything else falls to the collector.

```c
// IRVisitor<LowerControlImpl>::visit(Instruction&)  @0x118e940  (CONFIRMED)
switch (inst.IT):                                      // dword at inst+0x58
    case 105 Loop:           for childBB in inst.region: visit(childBB)   // descend, intact
    case 106 DynamicForLoop: for childBB in inst.region: visit(childBB)
    case 108 DoWhile:        for childBB in inst.region: visit(childBB)
    default:
        if inst.IT > 109: llvm_unreachable("Unknown Instruction type encountered!")
        return LowerControlImpl::visitInstruction(inst)  // collect
```

The collector keeps every real leaf instruction *except* the terminator-class control ops, so the fence can be made to happen-after all real work without depending on the terminator itself.

```c
// LowerControlImpl::visitInstruction(Instruction&)  @0x118d4c0  (CONFIRMED — the COLLECTOR)
function visitInstruction(Instruction& inst):
    inst.realDescendents(&range)
    if not range.empty: return                         // only leaves are collected
    keep = 1
    op = inst.IT
    if op == 83 (Break): keep = 0
    elif (op - 77) <= 5:                               // op in [77..82]
        keep = byte_1DF3AD0[op-77] ^ 1                 // mask @ .rodata 0x1DF3AD0
    if keep: worklist.push_back(inst)
// mask byte_1DF3AD0 = {01,01,01,00,01,01,...} ; with the ^1:
//   77 Terminator→0  78 CompareAndBranch→0  79 UncondBranch→0
//   80 BranchHint→1(KEEP)  81 Return→0  82 Exit→0  83 Break→0(explicit)
// ⇒ collects every leaf EXCEPT the 6 terminator-class ops {77,78,79,81,82,83}; BranchHint(80) IS kept.
```

The fence itself is the heart of the pass — a fixed 4-op prologue plus a multi-core CoreBarrier epilogue, inserted just before the terminator.

```c
// LowerControlImpl::leaveBasicBlock(BasicBlock& BB)  @0x118e0c0  (CONFIRMED — the TRANSFORM)
function leaveBasicBlock(BB):
    T = BB.getFirstTerminator()
    if !T or (T.IT - 81) <= 2: return                  // skip if Return(81)/Exit(82)/Break(83)
    ip = before T                                       // insert ahead of the terminator

    // --- the fixed 4-op fence, all on the engine-ALL sync path (mask=ALL=7) ---
    b1 = insertElement<InstAllEngineBarrier>   (BB, ip, name+"-barrier1");     b1[+136]=ALL  // @0x118db30 tmpl
    rs = insertElement<InstGroupResetSemaphores>(BB, b1, name+"-sema-reset");  rs[+232]=1; rs[+136]=ALL  // @0x118d5a0 tmpl
    b0 = insertElement<InstAllEngineBarrier>   (BB, rs, name+"-barrier0");     b0[+136]=ALL
    dr = insertElement<InstDrain>              (BB, b0, name+"-drain");        dr[+144]=1; dr[+240]=1
    tail = dr

    // --- multi-core epilogue: a CoreBarrier rendezvous over ALL logical cores ---
    if BB.func.module.config.lnc_size > 1:              // *((QWORD)+1)+0x1a4 > 1
        cb = insertElement<InstCoreBarrier>(BB, dr, name+"-end-cb")
        cb.barrierIndex(+280) = 0
        cb.coreIdList(+320..) = [0,1,...,lnc_size-1]    // all cores participate
        cb[+144] = 1;  cb.addDependency()
        tail = cb

    // --- make the fence a TRUE barrier: every collected inst happens-before it ---
    for inst in worklist:  inst.addDependency(tail)

    // --- detach the structured-control descendents from the dep graph ---
    for d in (T.next() .. BB.end()):
        assert d.numDescendents()==0
            : reportError("Terminator instruction should not have any descendents", lower_control.cpp)
        if d.IT != 78 (CompareAndBranch):              // CmpAndBranch keeps its predicate deps
            rewireDeps(d); d.removeAllDependencies()
```

The resulting order at a control-transfer BB tail is:

```text
<all real engine work>  →  AllEngineBarrier  →  GroupResetSemaphores  →  AllEngineBarrier
                        →  Drain  →  [CoreBarrier(all cores)]  →  <terminator>
```

The two `AllEngineBarrier`s bracket the semaphore reset so no engine ever observes a half-reset semaphore across the edge; `Drain` forces in-flight DMA/queue traffic to retire; the `CoreBarrier` (only when `lnc_size>1`) makes the inter-core rendezvous explicit. **CONFIRMED** — the literals `-barrier1`, `-sema-reset`, `-barrier0`, `-drain`, `-end-cb`, the assertion `Terminator instruction should not have any descendents`, the `lnc_size` gate `cmpl $1,0x1a4`, and the `EraseInterBbDeps removed` log are all live in the binary.

> **GOTCHA —** `CompareAndBranch(78)` is the **one** terminator whose data dependencies are preserved (the `d.IT != 78` guard). Its predicate operand chain must survive into `lower_branch`'s `convertPredToBranch`. A reimplementation that strips deps from *all* terminators uniformly will sever the predicate and produce an unconditional branch where a conditional one was intended.

### EraseInterBbDeps + renumberCoreBarriers

`EraseInterBbDeps` @ `0x118c310` walks every instruction in every BB, removes each data-dependency edge whose target lives in a *different* BB, marks the function with `FunctionAttribute` id 18 (INFERRED name `controlLowered`), and logs `"EraseInterBbDeps removed <N> inter-BB deps"`. Rationale: after this step cross-BB order is enforced *solely* by the §2.2 fences, so the scheduler must not also see explicit inter-BB edges that would over-constrain or contradict the fence model. `renumberCoreBarriers` @ `0x1089250` (called only when `lnc_size>1`) re-sequences every `CoreBarrier(87)` index (`+272`) densely module-wide, because `lower_control` just minted new `-end-cb` barriers and the order-130 `lnc_barriercheck` needs a dense, consistent id space to range them. **CONFIRMED.**

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `LowerControl::run` | `0x118cab0` | order-128 driver: erase → visit → fence → renumber | CERTAIN |
| `EraseInterBbDeps` | `0x118c310` | drop all cross-BB data deps; mark FunctionAttribute#18 | CERTAIN |
| `IRVisitor<LowerControlImpl>::visit` | `0x118e940` | opcode dispatch; descends carriers 105/106/108 | CERTAIN |
| `LowerControlImpl::visitInstruction` | `0x118d4c0` | collect every real leaf except terminator-class | CERTAIN |
| `LowerControlImpl::leaveBasicBlock` | `0x118e0c0` | insert the 4(+1)-op fence before each terminator | CERTAIN |
| `renumberCoreBarriers` | `0x1089250` | re-index CoreBarrier(87) module-wide (multi-core only) | CERTAIN |

---

## 3. `lower_branch` — SP-branch microcode + `addBranchToLoop`

### Purpose

`lower_branch` (low-level order L20) runs **after** codegen-stage per-engine split (`expand_all_engine_pre_alloc`, L19), so every op it sees already lives on a concrete engine. On the SP engine (`EngineType 6`, ext "Sync") it rewrites the already-normalized control ops into **actual SP branch micro-instructions**: it materializes predicates into SP registers, picks the `BranchCompareOp` IMM/REG form, and emits physical back-edges and pre-headers. This is the pass the task's framing ("structured Loop/DoWhile → explicit branch + loop-counter register ops on the SP engine") actually describes — *not* `lower_control`. **CONFIRMED.**

### Entry Point

```text
LowerBranch::run(bir::Module&)              @0x11c02d0  ── per-fn driver, 5 helpers in order:
  ├─ addBBExit(BasicBlockHolder&)           @0x11bda70  ── ensure each BB ends with an explicit exit
  ├─ addBranchToLoop(BasicBlock&)           @0x11be560  ── loop BACK-EDGE (heaviest, 0x17a0 B)
  ├─ splitBBForPredicate(BasicBlockHolder&) @0x11bfd10  ── cut a BB at a predicated terminator
  ├─ convertPredToBranch(BasicBlock&)       @0x11bca90  ── predicate → CompareAndBranch
  └─ addLoopEntry(BasicBlockHolder&)        @0x11bc840  ── insert loop pre-header + entry branch
```

### Algorithm — the predicate → SP form

`convertPredToBranch` materializes the comparison operands onto SP registers (via the shared `lowerAffineExpr` + `addRegAlu*` emitters), then chooses a `BranchCompareOp` by whether the right-hand side is a baked constant (IMM block, ops 0–5) or a runtime register (REG block, ops 6–11), and emits an `addCompareAndBranch`.

```c
// convertPredToBranch(BB)  @0x11bca90  (the predicate → SP form)
for I in BB.terminators:                               // CompareAndBranch / structured predicate
    r = F.addRegister(name, EngineType::SP /*6*/, width)
    lowerAffineExpr(I, condExpr, r, tmp, BB)            // affine cond → SP register-ALU ops
    if rhs is a baked constant:
        op = IS_<pred>IMM   (BranchCompareOp 0..5)      // rhs immediate
        addCompareAndBranch(BB, SP, name, op, takenBB, fallBB)            // @0x109bea0
    elif rhs is a runtime register:
        op = IS_<pred>REG   (BranchCompareOp 6..11)     // rhs in a scalar register
        addCompareAndBranch(BB, SP, name, op, takenBB, fallBB, r, rhsReg) // @0x109c110/240
    else:
        op = Unsupported(12); reportError(...)          // engine cannot encode this predicate
addUnconditionalBranch(BB, SP, name, target)           // @0x109bcf0 — fallthroughs
```

The 13-arm `BranchCompareOp` enum factors as `predicate {LT,LE,EQ,NE,GE,GT}` in the low triple and `rhs-source {IMM=block 0, REG=block 1}` in `(val/6)`; the three `addCompareAndBranch` overloads map exactly to (no-reg = static 2-target), (+reg = REG form), (+reg,+reg = both operands register). `comp_op` lands at `InstCompareAndBranch+0xF0`. **CERTAIN** (cross-ref [SP Sync / Branch / Control Encoding](../isa/sp-sync-encoding.md)).

### `addBranchToLoop` — the loop back-edge

`addBranchToLoop` @ `0x11be560` is the heaviest helper (0x17a0 B). It is **per-engine**: it calls `BasicBlockHolder::getEngines()` and walks an `Rb_tree<EngineType, pair<Register*,Register*>>` — a per-engine `{loop-counter, limit}` register pair. It inserts an `InstAllEngineBarrier(15)` at the loop join, calls `Instruction::update_loopnest()` to refresh the loop-nest metadata, then emits the back-edge as a counter-vs-limit `addCompareAndBranch` (`IS_LTREG`/`IS_LTIMM`) followed by an `addUnconditionalBranch` back to the header.

```c
// addBranchToLoop(BB)  @0x11be560  (the loop BACK-EDGE, per-engine counters)
function addBranchToLoop(BB):
    if BB is not a loop body: return
    engines = BB.holder.getEngines()                   // per-engine {counter, limit} reg pairs
    insertElement<InstAllEngineBarrier>(BB, join)      // IT 15 — quiesce at the loop join
    for (E, (counterReg, limitReg)) in engines:        // Rb_tree<EngineType, pair<Register*,Register*>>
        Instruction::update_loopnest(BB)               // refresh loop-nest metadata
        // back-edge: branch to header while counter < limit
        addCompareAndBranch(BB, E, name, IS_LTREG/IS_LTIMM, headerBB, exitBB, counterReg, limitReg)
    addUnconditionalBranch(BB, SP, name, headerBB)     // unconditional return to header
```

Paired with `addLoopEntry` @ `0x11bc840` — which inserts a fresh pre-header `BasicBlock` (`insertBasicBlockBefore`) carrying the loop-init `regMov` plus the entry `addUnconditionalBranch` into the body — a structured `Loop(105)` is exploded into `{pre-header, body, back-edge}` SP branches with per-engine counter registers. **STRONG** (symbol-anchored; the per-engine `Rb_tree` walk and `update_loopnest` call are disasm-confirmed, the exact `IS_LT*` choice is inferred from the counter-vs-limit pattern).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `LowerBranch::run` | `0x11c02d0` | per-fn driver; 5 helpers in fixed order | CERTAIN |
| `LowerBranch::addBranchToLoop` | `0x11be560` | per-engine loop back-edge + barrier + counter branch | HIGH |
| `LowerBranch::convertPredToBranch` | `0x11bca90` | predicate → SP `CompareAndBranch` (IMM/REG form) | HIGH |
| `LowerBranch::addLoopEntry` | `0x11bc840` | insert loop pre-header BB + entry branch | HIGH |
| `LowerBranch::addBBExit` | `0x11bda70` | append explicit BB exit branch | HIGH |
| `LowerBranch::splitBBForPredicate` | `0x11bfd10` | split a BB at a predicated terminator | HIGH |

---

## 4. The Boundary — three passes, one decision tree

The single fact a reimplementer must internalize: these are three passes, not one, separated by pipeline stage and engine scope.

| | `lower_select` | `lower_control` | `lower_branch` |
|---|---|---|---|
| **Order** | 64 (pre-codegen) | 128 (last pre-codegen) | L20 (post-codegen, after per-engine split) |
| **Granularity** | per-instruction | per-basic-block | per-engine (SP) per-BB |
| **Opcodes touched** | `Select(51)` | terminator family `{77,78,79,81,82,83}` + all leaves | control ops on SP engine |
| **Emits** | `GenericCopy(1)` + `CopyPredicated(52)` | `AllEngineBarrier`/`GroupResetSemaphores`/`Drain`/`CoreBarrier` | SP `CompareAndBranch`/`UnconditionalBranch` + register-ALU |
| **Touches loops?** | descends, rewrites Selects inside | descends, leaves carriers intact | explodes `Loop(105)` into branches |
| **Decides** | how to express a ternary | *where* control flows / when engines quiesce | *how* the SP sequencer encodes the flow |

`lower_control` decides **where** control flows and erects the synchronization scaffold; `lower_branch` decides **how** the SP sequencer encodes that flow. `lower_control` is op-level and all-engines and runs before codegen; `lower_branch` is SP-microcode and runs after the per-engine split. They meet at the canonical control ops `lower_control` leaves behind, which `lower_branch` then serializes into physical SP branches. **STRONG, byte-anchored on the opcode tests and emitter call sets of both.**

---

## Related Passes

| Order | Name | Relationship |
|---|---|---|
| 64 | `lower_select` | rewrites `Select(51)` → `GenericCopy`+`CopyPredicated`; must run before codegen because `Select` has no emitter |
| 128 | `lower_control` | last pre-codegen pass; mints the barriers/drains the two checkers then prove sufficient |
| 130 | `lnc_barriercheck` | `LncBarrierCheck` — ranges the `-end-cb` CoreBarriers `lower_control` minted, proves cross-core separation (multi-core only) |
| 131 | `bir_racecheck` | `BirRaceCheck` — dead-last; proves no intra-module physical-access race remains (gated `--enable-data-race-checker`, default off) |
| L20 | `lower_branch` | SP-branch microcode: predicate compares, loop back-edges, pre-headers |
| L31 | `lower_dve` | `LowerDVE` — runs the `CoreV*Gen` encoder over DVE ops, including the `CopyPredicated` that `lower_select` produced |

---

## Cross-References

- [Backend Pass Generator Registry](backendpass-registry.md) — the `register_generator_*` factory infra these three passes register through
- [The Walrus Pass Pipeline & the Optlevel Planes](pass-pipeline-optlevels.md) — where orders 64 / 128 and the low-level L20 sit in the builder dispatch
- [SP Sync / Branch / Control Encoding](../isa/sp-sync-encoding.md) — the `BranchCompareOp` enum and the SP `CompareAndBranch` wire form `lower_branch` emits
- [SP Engine — the TPB Control Processor](../arch/sp-engine.md) — the EngineType-6 sequencer that executes the branch/barrier micro-ops
- [Mask / Predicate Algebra](../nki/mask-predicate-algebra.md) — the 0/1 predicate tensor `lower_select` consumes as `arg0`
