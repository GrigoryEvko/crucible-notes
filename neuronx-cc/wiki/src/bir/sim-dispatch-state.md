# BIR Simulator: Dispatch & Whole-Machine State

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, `neuronxcc/starfish/lib/libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`, cp310 wheel). For `.text`/`.rodata`, virtual address equals file offset (`readelf`: `.text` `sh_addr`==`sh_off`==`0xf1ad0`; `.rodata` `0x58f000`==`0x58f000`). `.data` (VMA `0x2293560`) and `.data.rel.ro` (VMA `0x22679c0`) carry a `+0x1000` VMA→file-offset delta — subtract `0x1000` before `xxd`'ing any `.data`-resident global. The `nki_klr_sim` driver addresses in [§7](#7-the-nki_klr_sim-driver--klr--bir--sim--golden-check) are from a different binary (the standalone tool, cp312 IDA DB; its own report is cp310-pinned) and use that binary's frame. Other wheels differ; treat every address as version-pinned.*

## Abstract

The BIR simulator is a **functional reference interpreter**: it walks a scheduled `bir::Module` and executes every instruction against a modelled machine state — a tensor memory, a scalar register file, a semaphore bank, and a host/device RNG — then dumps each output tensor as a NumPy `.npy` and compares it against a golden `.npy` with `numpy`-style `allclose`. It is the compiler's own answer to "did this BIR actually compute the right thing", and it is the leaf the `nki_klr_sim` standalone tool and the in-compiler `bir_sim` walrus pass both drive.

This page documents the **simulator core** — `libBIRSimulator.so`, a 36 MB shared object with full decompiled-C sidecars in the corpus — at three altitudes:

1. **Dispatch.** The opcode→kernel router is one `110`-case jump-table `switch` inside `bir::IRVisitor<birsim::InstVisitor,void>::visit` (`@0x26b380`), keyed on the libBIR `InstructionType` at `Instruction+0x58` (the same discriminant [`InstructionType`](instruction-type.md) and the JSON serializer switch on). Three structured-control-flow opcodes are intercepted *before* the switch; eleven route to a "skip + note" base handler; three are enter/leave-only no-ops; the remaining `93` each have a dedicated `visitInst<X>` kernel.
2. **State.** The entire machine state lives inside **one** C++ object — `birsim::InstVisitor`, ~3760 bytes — holding (inline or by pointer) a `birsim::Memory*` (a `DenseMap` keyed by `MemoryLocation*`), a `birsim::RegState`, a `birsim::SyncState` (a flat 256-`uint32` semaphore array), an embedded `PWPSim::Simulator` activation delegate, and an inline MT19937-64 host RNG. The visitor *is* the simulation context.
3. **Driver.** The `nki_klr_sim` tool: load a serialized KLR program (version `0.0.12`, `lnc`-tagged), lower it to BIR, run the BIR on this simulator, and golden-check the outputs.

For reimplementation, the contract is:

- The `110`-row `InstructionType`→`visitInst<X>` dispatch table, its pre-switch CF interception, and its four routing classes.
- The `InstVisitor` field map — every sub-state's byte offset, pinned to a constructor write.
- The `Memory` `DenseMap`/`MemoryObject` model (flat per-tensor byte arrays + a per-byte "written" shadow), the `RegState` per-engine register file, and the `SyncState` 256-slot bank with its five update modes.
- The MT19937-64 host generator — textbook-correct masks, with the `std::uniform_int_distribution` adapter quirk — and the device generators (the gen-2 Xorwow body is given here; the gen-1 `LFSR`/`PCG32`/`Philox` switch is on the [dedicated RNG page](sim-bn-rng-collective-dma.md)).

| | |
|---|---|
| **Binary** | `libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`; 16,821 funcs; full decompiled C) |
| **Dispatch** | `bir::IRVisitor<birsim::InstVisitor,void>::visit` `@0x26b380` — 110-case switch on `Instruction+0x58` |
| **State object** | `birsim::InstVisitor`, ctor `@0x1bc330`, ~3760 bytes |
| **Per-BB driver** | `birsim::InstVisitor::simulateWithSyncs` `@0x20cd60` (semaphore-ordered, multi-engine) |
| **Activation delegate** | `PWPSim::Simulator` (embedded `@InstVisitor+0x298`, from `libpwp_sim.so`) |
| **Standalone driver** | `nki_klr_sim` (separate binary) — `main` `@0x4bb2d0` |

---

## 1. The dispatch — one 110-case switch on `Instruction+0x58`

The router is a single jump-table `switch` inside

```
bir::IRVisitor<birsim::InstVisitor,void>::visit(bir::Instruction&)   @0x26b380
```

keyed on the 32-bit `InstructionType` field at `Instruction+0x58` — the *same* field [`InstructionType`](instruction-type.md) (`sameInst`, family masks) and the BIR-JSON serializer dispatch on. There is **no** opcode/wire remap in the dispatch: every kernel name lines up 1:1 with the canonical `InstructionType` name (`IT 4 = Activation → visitInstActivation`; `IT 8 = Matmult → visitInstMatmult`; `IT 46 = IndirectSaveAccumulate → visitInstIndirectSaveAccumulate`). **[CONFIRMED]** — `switches.json` records `func_addr 0x26b380`, `inst_addr 0x26b3fd`, `default_addr 0x26bc6c`, `ncases 110`, and the 110 `(value→target)` pairs join byte-exact to the disassembly.

### 1.1 The pre-switch control-flow interception

Before the switch is even reached, three structured-CF opcodes are pulled out by explicit compares. From the verbatim disassembly of `visit` (`@0x26b388`..`@0x26b3c6`):

```asm
0x26b380: push rbp ; mov rbp, rsi ; sub rsp, 0x10
0x26b388: mov  eax, [rsi+0x58]          ; eax = InstructionType
0x26b38b: cmp  eax, 0x69                 ; 105 = Loop
0x26b38e: jnz  0x26b3a0
0x26b399: jmp  birsim::InstVisitor::visitLoop          ; tail-jump, NEVER reaches switch
0x26b3a0: cmp  eax, 0x6A                 ; 106 = DynamicForLoop
0x26b3ae: jmp  birsim::InstVisitor::visitDynamicForLoop
0x26b3b8: cmp  eax, 0x6C                 ; 108 = DoWhile
0x26b3c6: jmp  birsim::InstVisitor::visitDoWhile
0x26b3d8: call birsim::InstVisitor::enterInstruction   ; per-inst pre-hook
0x26b3dd: cmp  dword [rbp+0x58], 0x6D    ; ja default  (switch on IT, 110 cases)
0x26b3fd: jmp  rax                       ; jpt_26B3FD, rel-table base 0x5F4E08
```

So `Loop` (105) / `DynamicForLoop` (106) / `DoWhile` (108) **tail-jump out before `enterInstruction` and before the switch** — they recurse the visitor over their region bodies (the `enterInstLoop` / `enterInstDoWhile` / `enterInstDynamicForLoop` hooks fire there instead). **[CONFIRMED — disasm + tail-jump targets.]**

> **GOTCHA — the switch table still has dead slots for 105/106/108.** The jump table emitted at `jpt_26B3FD` contains `110` value→target pairs, and the slots for values `105`, `106`, `108` *do* point at the base block `0x26b400` (IDA even annotates `0x26b400` as "cases 0,2,7,18,47,56,68,77,93,100,**104-106,108**"). Those three table entries are **never executed**: the pre-switch `cmp eax,0x69/0x6A/0x6C` intercepts them first. A naive reading of `switches.json` therefore counts **14** values routed to `0x26b400`; the true *runtime* base set is **11**. Do not conflate the static table slot with the dynamic route. **[CONFIRMED — `jq` over `switches.json` gives `base_to_400 = 14`; disasm proves the three are dead.]**

### 1.2 The four routing classes (sums to 110)

| Class | Count | Values | Behaviour |
|-------|-------|--------|-----------|
| **mapped** | 93 | (all others) | tail to a dedicated `birsim::InstVisitor::visitInst<X>` kernel |
| **preCF** | 3 | 105, 106, 108 | pre-switch tail-jump to `visitLoop` / `visitDynamicForLoop` / `visitDoWhile` |
| **base** | 11 | 0, 2, 7, 18, 47, 56, 68, 77, 93, 100, 104 | `visitInstruction` `@0x1aa750` — records opcode name into the diag buffer (`this+0xA8`), a "skip + note", **not** a hard abort |
| **noop** | 3 | 12, 13, 15 | `NoOp` / `EventSemaphore` / `AllEngineBarrier` — `enterInstruction` already done, straight to `leaveInstruction`, **no** `visitInst` call (block `0x26b412`) |
| *default* | — | IT ≥ 110 | `reportError("Unknown Instruction type encountered!")` (`Instruction.cpp:157`, `@0x26bc6c`) |

The eleven **base** ITs split into two semantic groups — this split is the signal:

- **6 abstract base-class types**, never directly instantiated: `Generic`(0), `MatmultBase`(7), `DMA`(18), `Collective`(47), `DMADescriptor`(68), `Terminator`(77). Their concrete leaves (`Matmult`, `DMACopy`, `CollectiveCompute`, `DMADescriptorCopy`, `CompareAndBranch`, …) *all* have mapped kernels.
- **5 concrete-but-sim-unsupported leaves**: `GenericRelu`(2), `NKIKLIRKernel`(56), `InlineASMBytes`(93), `Rng`(100), `NonzeroWithCount`(104). These reach the base handler at runtime and are recorded by name, not executed.

### 1.3 Dispatch mechanics — thunk indirection

Each switch case-target is a small block that does `mov rsi,rbp` (load `Instruction*`) then `call <thunk>`, where `<thunk>` is a 6-byte PLT.sec stub `jmp [GOT]` in the `0xeb000`–`0xf1fff` range. The GOT slot resolves intra-module to the **defining body**. Example, `Activation` (IT 4):

```
case-target 0x26bc27  →  call thunk 0xecb30  (`ff 25 4a 42 1a 02` = jmp cs:off_2290D80)
                       →  body 0x2012d0  (birsim::InstVisitor::visitInstActivation)
```

So the dispatch `call` hits a thunk; the thunk tail-jumps to the body. F-strand per-op pages read the **body VA**. The sidecar pairing (thunk `<0x180000`, body `≥0x180000`, same demangled name) confirms all 93 such pairs. **[CONFIRMED.]**

### 1.4 The 110-row table

Columns: `IT` = `InstructionType`; `name` = canonical [`InstructionType`](instruction-type.md) spelling; `thunk` = PLT.sec stub the case-block `call`s; `body` = defining `visitInst<X>` VA; `class` per [§1.2](#12-the-four-routing-classes-sums-to-110).

| IT | name | thunk | body | class |
|---:|------|------:|-----:|-------|
| 0 | Generic | — | `0x1aa750` | base *(abstract)* |
| 1 | GenericCopy | `0xeb8a0` | `0x1d2ac0` | mapped |
| 2 | GenericRelu | — | `0x1aa750` | base *(unsupported leaf)* |
| 3 | AbstractCopy | `0xf0940` | `0x1d2ab0` | mapped |
| 4 | Activation | `0xecb30` | `0x2012d0` | mapped *(→PWPSim)* |
| 5 | ReadActivationAccumulator | `0xf04e0` | `0x1d2ae0` | mapped |
| 6 | LoadActFuncSet | `0xee3a0` | `0x1b2070` | mapped |
| 7 | MatmultBase | — | `0x1aa750` | base *(abstract)* |
| 8 | Matmult | `0xee770` | `0x27f670` | mapped |
| 9 | MatmultSparse | `0xeca70` | `0x1f0a10` | mapped |
| 10 | Memset | `0xee570` | `0x1d5060` | mapped |
| 11 | GetGlobalRankId | `0xebfd0` | `0x1ba670` | mapped |
| 12 | NoOp | — | — | noop |
| 13 | EventSemaphore | — | — | noop |
| 14 | GroupResetSemaphores | `0xed3f0` | `0x1aa140` | mapped |
| 15 | AllEngineBarrier | — | — | noop |
| 16 | Drain | `0xf0a50` | `0x1aa160` | mapped |
| 17 | Halt | `0xee7f0` | `0x1aa170` | mapped |
| 18 | DMA | — | `0x1aa750` | base *(abstract)* |
| 19 | Load | `0xef220` | `0x1d2a80` | mapped |
| 20 | Pool | `0xf0600` | `0x1d7340` | mapped |
| 21 | Reciprocal | `0xec200` | `0x1d9e60` | mapped |
| 22 | Save | `0xef0e0` | `0x1d2aa0` | mapped |
| 23 | TensorCopy | `0xed530` | `0x1d2a90` | mapped |
| 24 | TensorCopyDynamicSrc | `0xeb050` | `0x20b280` | mapped |
| 25 | TensorCopyDynamicDst | `0xed760` | `0x20b420` | mapped |
| 26 | IndirectCopy | `0xee8a0` | `0x1e2070` | mapped |
| 27 | TensorReduce | `0xf0530` | `0x1d7720` | mapped |
| 28 | TensorScalar | `0xf16f0` | `0x1d9e50` | mapped |
| 29 | TensorScalarPtr | `0xf02b0` | `0x201fc0` | mapped |
| 30 | TensorScalarCache | `0xeb300` | `0x20a340` | mapped |
| 31 | TensorTensor | `0xeb4f0` | `0x1fd7f0` | mapped |
| 32 | DMACopy | `0xee3f0` | `0x20b7b0` | mapped |
| 33 | GPSIMDSB2SB | `0xf00d0` | `0x1f7590` | mapped |
| 34 | BNStats | `0xee5a0` | `0x1df7b0` | mapped |
| 35 | BNStatsAggregate | `0xec9c0` | `0x1dfcb0` | mapped |
| 36 | BNGradients | `0xebcc0` | `0x1dfff0` | mapped |
| 37 | BNBackprop | `0xed1b0` | `0x1e0560` | mapped |
| 38 | BNBackprop2 | `0xef5d0` | `0x1e0ab0` | mapped |
| 39 | StreamShuffle | `0xed700` | `0x1e1480` | mapped |
| 40 | StreamTranspose | `0xef310` | `0x1e1490` | mapped |
| 41 | ReadVarAddr | `0xeb9c0` | `0x1db8f0` | mapped |
| 42 | GenericIndirectLoad | `0xef7c0` | `0x1f5200` | mapped |
| 43 | IndirectLoad | `0xf1280` | `0x1e17a0` | mapped |
| 44 | GenericIndirectSave | `0xeeb60` | `0x1f3b30` | mapped |
| 45 | IndirectSave | `0xf11e0` | `0x1e1c20` | mapped |
| 46 | IndirectSaveAccumulate | `0xf0000` | `0x1e2640` | mapped *(RMW scatter)* |
| 47 | Collective | — | `0x1aa750` | base *(abstract)* |
| 48 | CollectiveCompute | `0xee2a0` | `0x1ed9b0` | mapped |
| 49 | CollectiveSend | `0xeee20` | `0x1eda20` | mapped |
| 50 | CollectiveRecv | `0xed4d0` | `0x1edba0` | mapped |
| 51 | Select | `0xf0730` | `0x206280` | mapped |
| 52 | CopyPredicated | `0xee200` | `0x206a90` | mapped |
| 53 | CustomOp | `0xf16b0` | `0x1abbd0` | mapped |
| 54 | BIRKernel | `0xebb00` | `0x1abf50` | mapped |
| 55 | NKIKernel | `0xeefb0` | `0x212930` | mapped |
| 56 | NKIKLIRKernel | — | `0x1aa750` | base *(unsupported leaf)* |
| 57 | DevicePrint | `0xec1c0` | `0x1ddf20` | mapped |
| 58 | GetRandState | `0xefd50` | `0x1d2ad0` | mapped |
| 59 | SetRandState | `0xee660` | `0x1f8490` | mapped |
| 60 | Rand | `0xeff40` | `0x1d6290` | mapped |
| 61 | Iota | `0xed040` | `0x1efbd0` | mapped |
| 62 | TensorScalarAffineSelect | `0xeba30` | `0x1efbe0` | mapped |
| 63 | RangeSelect | `0xedea0` | `0x1fe910` | mapped |
| 64 | GetSequenceBounds | `0xedb70` | `0x1f0200` | mapped |
| 65 | Dropout | `0xeb1c0` | `0x1f9310` | mapped |
| 66 | GetCurProcessingRankID | `0xf0590` | `0x1bb1f0` | mapped |
| 67 | DMATrigger | `0xeb580` | `0x2124a0` | mapped |
| 68 | DMADescriptor | — | `0x1aa750` | base *(abstract)* |
| 69 | DMADescriptorCopy | `0xf03a0` | `0x1df710` | mapped |
| 70 | DMADescriptorCCE | `0xed600` | `0x1dadd0` | mapped |
| 71 | DMADescriptorTranspose | `0xed180` | `0x1ddf10` | mapped |
| 72 | DMADescriptorReplicate | `0xed480` | `0x20c490` | mapped |
| 73 | RegisterAlu | `0xef6d0` | `0x1bee20` | mapped |
| 74 | RegisterMove | `0xed350` | `0x209000` | mapped |
| 75 | TensorLoad | `0xef400` | `0x1f1b40` | mapped |
| 76 | TensorSave | `0xeb710` | `0x1bd820` | mapped |
| 77 | Terminator | — | `0x1aa750` | base *(abstract)* |
| 78 | CompareAndBranch | `0xf0dc0` | `0x1bffb0` | mapped |
| 79 | UnconditionalBranch | `0xf08f0` | `0x1aa550` | mapped |
| 80 | BranchHint | `0xef840` | `0x1aa650` | mapped |
| 81 | Return | `0xf19d0` | `0x1aa560` | mapped |
| 82 | Exit | `0xee190` | `0x1af7b0` | mapped |
| 83 | Break | `0xf13e0` | `0x1aa640` | mapped |
| 84 | Call | `0xf04f0` | `0x26bc90` | mapped *(`ControlFlowIRVisitor::visitInstCall`)* |
| 85 | SwitchQueueInstance | `0xee970` | `0x1aa210` | mapped |
| 86 | ResetQueueInstance | `0xee4b0` | `0x1aa350` | mapped |
| 87 | CoreBarrier | `0xf1870` | `0x1aa720` | mapped |
| 88 | Max | `0xede10` | `0x1f2690` | mapped |
| 89 | MaxIndex | `0xeb950` | `0x1f2b50` | mapped |
| 90 | MatchReplace | `0xebe20` | `0x1f37c0` | mapped |
| 91 | MaxIndexAndMatchReplace | `0xebcd0` | `0x1f37d0` | mapped |
| 92 | Gather | `0xeb440` | `0x1f39d0` | mapped |
| 93 | InlineASMBytes | — | `0x1aa750` | base *(unsupported leaf)* |
| 94 | TensorCompletion | `0xedb40` | `0x1aa450` | mapped |
| 95 | MatmultMx | `0xeea50` | `0x27f680` | mapped |
| 96 | QuantizeMx | `0xed230` | `0x1f6920` | mapped |
| 97 | Rand2 | `0xeba40` | `0x1d2fa0` | mapped |
| 98 | RandSetState | `0xf0270` | `0x1d4360` | mapped |
| 99 | RandGetState | `0xeb990` | `0x1d3740` | mapped |
| 100 | Rng | — | `0x1aa750` | base *(unsupported leaf)* |
| 101 | ActivationReadAccumulator | `0xec500` | `0x1d2c90` | mapped |
| 102 | DveReadAccumulator | `0xec330` | `0x1d2f50` | mapped |
| 103 | Exponential | `0xf0770` | `0x1fe920` | mapped |
| 104 | NonzeroWithCount | — | `0x1aa750` | base *(unsupported leaf)* |
| 105 | Loop | *(pre-switch)* | `0x211010` | preCF *(`visitLoop`)* |
| 106 | DynamicForLoop | *(pre-switch)* | `0x211690` | preCF *(`visitDynamicForLoop`)* |
| 107 | DMABlock | `0xef480` | `0x212420` | mapped |
| 108 | DoWhile | *(pre-switch)* | `0x2120c0` | preCF *(`visitDoWhile`)* |
| 109 | TongaReduceMacroSymbolic | `0xf0ec0` | `0x20b1f0` | mapped |

> **NOTE — there is exactly one `TongaReduceMacroSymbolic` kernel, at IT 109** (`body 0x20b1f0`). The shared `leaveInstruction` tail block `0x26b412` is a common return path for the three no-op ITs (12/13/15) and for several kernels that `jmp short loc_26B412` after their `call`; it is *not* a Tonga call.

---

## 2. The traversal — `enterModule` → semaphore-ordered per-BB driver

The visitor exposes the standard hook set (all `birsim::InstVisitor`):

```
enterModule @0x1accc0
  → enterFunction @0x1c0a30
      → (per BasicBlock) simulateWithSyncs @0x20cd60
          → enterInstruction @0xec3b0 → visit() @0x26b380 (dispatch) → leaveInstruction
      → leaveFunction
  → leaveModule @0x1a9c20
```

`enterModule` reads the target arch (`bir::Module::getArch` / `getArchModel`) and constructs the core memory state from it — it builds a `birsim::MemoryObject` (16-byte element seed) and copies the VNC (virtual-neuron-core) manager. This is where per-arch SBUF/PSUM/DRAM sizing and core count enter the simulation (chain `*(getArchModel()+8)+16)+40)+8`). **[STRONG.]**

### 2.1 The per-BB driver is semaphore-ordered, not a linear walk

`birsim::InstVisitor::simulateWithSyncs(BasicBlock&)` `@0x20cd60` is the **multi-engine, semaphore-ordered** scheduler. Its core loop:

```c
// simulateWithSyncs @0x20cd60  (per BasicBlock) — verbatim call sequence
while (!engines.allFinished()) {
    bir::Instruction *I = engines.getNextInstruction();   // TpbEngines: pick a runnable
                                                          //   inst across per-engine queues
                                                          //   (asserts I.getEngine() !=
                                                          //    EngineType::Unassigned)
    syncState.actOnWait(I);    // block on I's semaphore WAIT set (needWait/actOnWait);
                               //   only proceed when satisfied
    visitor.visit(I);          // THE dispatch (§1): enter → 110-switch → kernel → leave
    syncState.actOnUpdate(I);  // apply I's semaphore UPDATE set (post-effects);
                               //   barrier insts also actOnUpdate(I+88)
}
syncState.checkEventsAllClear();  // assert no dangling semaphore events at BB end;
                                  //   dumpState on mismatch
```

So instruction **order** within a BB is the dynamic ready-order driven by semaphore set/wait edges across `bir::EngineType`-partitioned queues — **not** the static list order. `TpbEngines` (ctor `TpbEngines(SyncState&, BasicBlock&, size_t)`) provides `getNextInstruction` / `allFinished` / `countTotalRemainingInsts`, and `fetchAllEngineInst<InstAllEngineBarrier|InstExit>` for the cross-engine barrier/exit joins. **[STRONG — call sequence read off `0x20cd60`.]**

Inter-BB control transfer rides the `Terminator` family kernels (`UnconditionalBranch`/`CompareAndBranch`/`Return`/`Exit`/`Break`) plus the `ControlFlowIRVisitor::visitInstCall` path (IT 84 `@0x26bc90`), which pass block-argument values to the successor BB's parameters. The structured-CF ops `Loop`/`DynamicForLoop`/`DoWhile` ([§1.1](#11-the-pre-switch-control-flow-interception)) recurse the visitor over their region bodies, threading carried induction state across iterations; `visitInstInParallelLoop` `@0x20c940` / `visitInstLoopParallel` `@0x20cc60` drive the loop-parallel variants. **[INFERRED — region recursion is supported by the pre-switch tail-jumps + the `enterInst{Loop,DoWhile,DynamicForLoop}` hooks; the exact iteration/carried-state threading is left to the planned 7.40 loop-execution page.]**

---

## 3. The state object — `birsim::InstVisitor` (~3760 bytes)

The whole machine state lives inside **one** object. Every offset below is pinned to a write in the constructor `birsim::InstVisitor::InstVisitor(Memory*, SyncMode, name, Config, bool, size_t, bool)` `@0x1bc330`, or to a one-line accessor. CERTAIN unless noted.

| Offset (dec / hex) | Field | Anchor |
|---|---|---|
| +0 (`0x000`) | vptr (InstVisitor vtable) | ctor |
| +8 … +103 | two inline small-vectors (engine queues / scratch arenas) | ctor: `new(0x40)` + `new(0x200)` ×2 |
| +168 (`0xA8`) | `std::string` diag / input-file buffer | the "skip + note" name sink ([§1.2](#12-the-four-routing-classes-sums-to-110) base handler) |
| +224 (`0xE0`) | current-core `curCore` | ctor Config copy (`*(a1+216)=loadu(a5+1)`) |
| +296 (`0x128`) | qword (config; `setInstPtr` callback sink) | ctor `*(a1+296)=v44` |
| **+312 (`0x138`)** | **`birsim::Memory*` — MEMORY** | ctor `*(a1+312)=a2` |
| +320 (`0x140`) | VncManager / NeuronCoresManager (copy of `Memory::getVncManager`) | ctor `getVncManager(a1+320)` |
| **+336 (`0x150`)** | **`birsim::RegState` — REGISTER FILE** | `getRegState = this+336` |
| **+416 (`0x1A0`)** | **`birsim::SyncState` — SEMAPHORE BANK + EVENTS** | `getSyncState = this+416` |
| +656 (`0x290`) | config / syncMode dword | ctor `*(a1+656)=v67` |
| **+664 (`0x298`)** | **`PWPSim::Simulator` (EMBEDDED)** — activation delegate, from `libpwp_sim.so` | ctor |
| +760 (`0x2F8`) | `birsim::MemoryObject` SCRATCH-A (arch seed; `+872` byte=1 = its accum flag) | ctor; `enterModule` fills it |
| +880 (`0x370`) | `birsim::MemoryObject` SCRATCH-B (second scratch/accumulator) | ctor |
| **+992 (`0x3E0`)** | **RNG: MT19937-64 `mt[312]`** (`mt[0]=42`, then the `0x5851F42D4C957F2D` recurrence) | ctor seed loop ([§6](#6-rng-state--host-mt19937-64--device-xorwow)) |
| +3488 (`0xDA0`) | RNG: `mti` index = 312 | ctor `*(a1+3488)=312` |
| +3560 (`0xDE8`) | GLOBAL RANK ID = 0 (the `GetGlobalRankId` IT 11 source) | ctor `*(a1+3560)=0` |
| +3568 (`0xDF0`) | qword `a7` (size param; `setInstPtr` cb arg) | ctor `*(a1+3568)=a7` |
| +3584 … +3744 | zeroed config / run-flag tail (DenseMaps for `DMATrigger` fire-counters, etc.) | ctor |

The constructor also installs a **SIGFPE (signal 8) handler** (`sub_1A1E40` via `sigaction`) — the sim traps FP exceptions during numeric kernels — binds the Config into the Memory (`Memory::setBirSimConfig`), and asserts `PWPSim::Simulator::use_pwp_table()` unless the config disables it (`"Assertion failure: sim.use_pwp_table()"`, `inst_visitor.cpp:391/1411`).

> **GOTCHA — `InstVisitor+0x3E0` is the FIRST WORD of the MT19937-64 state (`mt[0]=42`), not a standalone "magic 42" sentinel.** The constructor's seed loop and `mti@+3488=312` ([§6.1](#61-host-mt19937-64--inline-at-instvisitor0x3e0)) prove it is the host RNG state. The two scratch `MemoryObject`s at `+0x2F8`/`+0x370` are an arch-seed scratch (`enterModule`-filled) plus a second accumulator; field-naming inside them is **[INFERRED]** (IDA has no `birsim::*` types in `structures.json` — the layout is reconstructed from ctor writes and accessors).

---

## 4. The memory model — a `DenseMap` keyed by `MemoryLocation*`

`birsim::Memory` (ctor `@0x174e10`) is the per-tensor backing store. Its central field is **not** a set of physical arenas — it is an `llvm::DenseMap`:

```cpp
// birsim::Memory  (selected fields, ctor 0x174e10)
+0       vptr (off_2276380; 13-slot read/write/readIndirect/writeIndirect ×local/remote)
+8 ..    llvm::DenseMap<const bir::MemoryLocation*,
                        std::pair<std::shared_ptr<birsim::MemoryObject>, uint32_t>>
                        // THE LOCAL backing map: tensor → (MemoryObject, refcount)
+32      std::string                       // input-file path (loadInput)
+88/+96  std::shared_ptr<NeuronCoresManager>  // the cross-core LNC manager
+104     DenseMap& (SHARED map ptr)        // cross-core / LNC-shared MemoryLocations
+144     float = 1.0f                      // default scale
+152     std::recursive_timed_mutex        // per-Memory lock
```

### 4.1 SBUF / PSUM / DRAM are a property of the key, not separate buffers

The SBUF vs PSUM vs DRAM distinction is a property of the **`MemoryLocation`** (`MemoryType` at `MemLoc+216`), not of separate physical arrays. Each tensor lazily gets **one** `MemoryObject` the first time it is written (`createIfNotExist @0x1730c0`). `getMap @0x16e5a0` routes: a **shared** (cross-core) `MemoryLocation` resolves to the second `DenseMap` (`Memory+104`); everything else to the local one (`Memory+8`). The `DenseMap` *value* is `pair<shared_ptr<MemoryObject>, uint32>` — the `uint32` is a refcount, managed by `createIfNotExist` / `decrementRef`. `birsim::PhysicalMemory` (ctor `@0x175680`) is the arch-aware subclass that sizes per-partition banks from the Module's arch. **[CONFIRMED.]**

Address-space guards in the binary spell out the model:

- `"(MemLoc.getType()==MemoryType::SB || ==MemoryType::PSUM) && \"Unexpected memory type\""` — SBUF/PSUM are the on-chip per-partition backing arrays.
- `"do not support psum multibank"` and `"Psum memory location is accessing beyong Psum physical memory size!"` — the sim models **one** PSUM bank.
- `"Only InstDMACopy/InstSave/InstLoad/InstTensorLoad can access DRAM using RegisterAP for now"` — DRAM is the off-chip arena, restricted to those four ops via `RegisterAP`.

### 4.2 The backing object — a flat byte array + a "written" shadow

`birsim::MemoryObject` (ctor `@0x28dc40` / `@0x28cbd0`, `sizeof ≈ 0x70`) owns **two** `shared_ptr<vector<uint8>>`:

```cpp
// birsim::MemoryObject  (ctor 0x28dc40)
+0/+8    shared_ptr<vector<uint8>> DATA   // backing array; NEW data memset 0xFF
                                          //   (= UNINIT sentinel; drives read-before-write)
+16/+24  shared_ptr<vector<uint8>> INIT   // OPTIONAL "written"/accumulator shadow, same
                                          //   size, memset 0x00; allocated only if +72 flag.
                                          //   runAP marks a byte 0xFF here after a copy.
+32/+40/+48 std::vector<size_t> SHAPE     // begin/end/cap; Π(dims) = numElements
+56      int Dtype                        // bir::Dtype (0..0x13); elt bytes = dword_5F7760[2*Dtype]
+64      uint64 numElements
+72      bool                             // "has init/accumulator array" (PSUM accum requires ==1)
+88/+96  std::vector<interval_map>        // boost::icl::interval_map<size_t,
                                          //   PhysicalMemMetaInfo, partial_enricher, …>
                                          //   per-byte init/provenance; binds each written
                                          //   interval to (MemoryLocation*, Instruction*)
```

The DATA array is `memset 0xFF` at construction — that `0xFF` sentinel is what the read-before-write checker keys on. The `interval_map` drives the dataflow checkers `checkUninitMemReadHelper @0x290320` / `checkWriteWithoutReadHelper @0x28b1a0` / `checkPinnedSBWriteHelper @0x291080`. **[CONFIRMED — ctor field writes + checker bodies.]**

### 4.3 AP → backing-slice resolution — `runAP`

`MemoryObject::runAP(srcAP, dstMemObj, isWrite, dtypeOverride, baseByteOff, skipChk)` `@0x298390` is the universal AP↔backing copy primitive:

```c
// MemoryObject::runAP @0x298390  (the byte-level walker)
v39 = NumElementsPerPartition * eltsize;       // per-partition byte span
v38 = getPartitionStep(ap);                    // arch per-partition byte stride
intervals = bir::linearizedElementIntervals(ap);  // [W,Z,Y,X]: drop unit dims,
                                                  //   fuse contiguous inner→run, nest outer
dataBase = **(this);       // DATA array begin
markBase = **(this+2);     // INIT/written shadow begin
for ((off, runlen) in intervals)
  for (chunk c in [0..runlen)) {
    byteAddr = base + (off % v39) + v38 * (off / v39);   // flat elem → (within-part, part idx)
    src = dataBase + byteAddr; dst = dstMemObj.data + cursor;   // swap if isWrite
    if (PSUM-accumulate path)   // opcode==95 (MatmultMx) || 7..9 (Matmult family) && this+72:
        cast_copy_or_accumulate(written ? ACCUM : COPY, dst, src, dtype);
        mark byteAddr in shadow = 0xFF;       // now "written"
    else
        memcpy(dst, src, runlen);
  }
```

The PSUM accumulate branch reads the per-byte "written" shadow to decide COPY (first write) vs ACCUMULATE (subsequent). Guards: `"MemoryObject::runAP the dtype between AP and MemObj must be the same"`; `"Reading X bytes … out of bounds"`. **[CONFIRMED.]**

For the **indirect** (gather/scatter) APs, `Memory::obtainDataAddrsInByte(targetAP, indicesAP, outAP) @0x179610` (`Memory.cpp:427`) derives the flat per-partition byte addresses from the arch model (`NumIndirectIndicesPerTile`), asserting `"TensorIndirect Indices data type must be uint16"`. The exact per-dim stride loop is the subject of the planned per-op scatter/gather pages.

### 4.4 PSUM accumulators — group-head reset

PSUM is the matmul partial-sum bank. The accumulate **state** is the `MemoryObject`'s per-byte written shadow (`+16`) plus the dtype-aware fold in `cast_copy_or_accumulate`. The PE-array group head resets it:

```
MemoryObject::resetAccumulation(PAP&, Inst*, isPSUM, accumType) @0x2931f0
  asserts "(CurMemLoc.isPSUM() && CurMemLoc.isAllocated() && isPSUM())"
    // MemLoc.MemoryType(+216)==32 (PSUM) && MemLoc.allocated(+168) && MemObj.flag(+72)
  on a group-head Matmult (EngineAccumulationType==Zero):
    memset the PSUM partition slice to 0 before the first MAC;
  subsequent MACs in the group += via runAP's accumulate branch.
```

The per-function "auto psum accumulate" attribute (Function attr key 19) overrides this; `resetAccumulation` reads it from the Function's attribute map. **[CONFIRMED — assert string + MemLoc offsets cross-checked against the [`MemoryLocation`](memory-location.md) field map.]**

---

## 5. The scalar register file & semaphore bank

### 5.1 `birsim::RegState` — per-engine 16-byte register entries

`RegState` (at `InstVisitor+0x150`) is the scalar/GPR state read/written by the register-ALU opcodes (`RegisterAlu` IT 73, `RegisterMove` IT 74, `CompareAndBranch` IT 78, `GetGlobalRankId` IT 11, `GetCurProcessingRankID` IT 66, `ReadVarAddr` IT 41, `TensorLoad`/`TensorSave` IT 75/76).

`initPhyRegSim(Module*) @0x198470`: for each **data-path** engine (`bir::isDataPathEngine`), allocate `arch.NumPhysicalRegisters` (arch model `+60`) entries of **16 bytes** each — `{uint32 value (init 0xFFFFFFFF = -1), Register* sym (init 0)}` — held in a per-engine map (Rb-tree keyed by engine id). `write() @0x19b460` indexes `base[16*RegId] = value`, `base[16*RegId+8] = Register*`; physical writes are width-checked (`"Physical register write can only be 32-bit or 64-bit for now"`). Symbolic registers (`createSymReg` / `writeSym`) live in a separate map. `get(Argument&) @0x196fe0` discriminates `Arg.kind==11` (`RegisterAccess`) vs `Arg.kind==3` (`RegisterAP`); 64-bit values use Hi/Lo `Argument` pairs. **[CONFIRMED for the entry stride + width check; the full 64-bit Hi/Lo arithmetic is read at signature level — INFERRED for the per-engine key encoding.]**

### 5.2 `birsim::SyncState` — a flat 256-`uint32` semaphore array

`SyncState` (at `InstVisitor+0x1A0`, ctor `SyncState(RegState&, shared_ptr<NeuronCoresManager>) @0x19f9e0`) holds:

| Offset (rel) | Member |
|---|---|
| +0 | `birsim::Events` (event registry; bitvector + two lists) |
| +88 | `birsim::Semaphores` (the counter bank) |
| +168/+176 | `std::shared_ptr<NeuronCoresManager>` (cross-core) |
| +184 | `std::string` (logger name) |

`birsim::Semaphores` (ctor `@0x19d8b0`) is the bank. Verbatim from the ctor:

```c
// birsim::Semaphores::Semaphores @0x19d8b0
v2 = operator new(0x400u);          // 1024 bytes
*((_QWORD*)this + 3) = v2 + 128;    // end = begin + 128 qwords = 256 uint32
... memset(begin, 0, 1024) ...      // all 256 slots zeroed
*((_QWORD*)this + 7) = (char*)this + 40;   // touched-id Rb-tree, self-rooted when empty
```

So the bank is exactly **256 `uint32` counters**, all zero. A side Rb-tree of "touched" semaphore IDs lets `checkEventsAllClear` flag a dangling event at BB end. **[CONFIRMED — `operator new(0x400)` + the `+128` end pointer.]**

Wait/update semantics, verbatim from `needWait @0x19e4b0` and `actOn(Update&) @0x19e300`:

```c
// Semaphores::needWait(Wait&) @0x19e4b0
mode 1:  return sema[Wait.getId()] < Wait.getValue();          // value-relative
mode 2:  return sema[Wait.getId()] < RegState::read(Wait.getReg());  // register-relative
else  :  assert("false && \"Unhandled semaphore wait command\"");

// Semaphores::actOn(Update&) @0x19e300  — five update modes
mode 5 → set(id, value);
mode 1 → inc(id, value);
mode 2 → dec(id, value);
mode 3 → inc(id, 1);   // asserts value==1, "Semaphore inc value must be 1"
mode 4 → dec(id, 1);   // asserts value==1
else   → assert("false && \"Unhandled semaphore update command\"");
```

`read(id) @0x19e480` bounds-checks `id < (end-begin)>>2 = 256`. `inc`/`dec`/`set` each mark the id in the touched-tree, then mutate `sema[id]`. The driver hooks `actOnWait(Inst*) @0x1a1750` / `actOnUpdate(Inst*) @0x1a0300` pull the inst's wait/update sets from its `SyncInfo`; `clearGroupSemaphores(vector<uint>) @0x19e750` is the `GroupResetSemaphores` (IT 14) kernel. The `shared_ptr<NeuronCoresManager>` makes cross-core semaphore events visible (collectives/remote-DMA route through the same manager). **[CONFIRMED — `needWait`/`actOn` decompiled bodies.]**

---

## 6. RNG state — host MT19937-64 + device Xorwow

Two generator *families* coexist: a **host** MT19937-64 (the visitor's own generator, the `SetRandState` scalar-seed source and the `Memset(Random)` fill when no device seed is present) and the **device** PRNGs (the modelled silicon generators, allocated per-op for `Rand`/`Rand2`/`Dropout`). This page documents the host MT19937-64 in full and gives the Xorwow body; the device side is *not* a single Xorwow. The dedicated [RNG kernels page](sim-bn-rng-collective-dma.md#21-gen-1--visitinstrand-it-60-3-way-prng-switch) proves it is **two device generations** — `Rand` (IT 60) is a gen-1 3-way `LFSR / PCG32 / Philox` switch keyed on `RandomAlgorithmKind` (`InstRand+0xF0`), and `Rand2` (IT 97) is the cuRAND **Xorwow** (gen-2). Treat the Xorwow body below as the gen-2 device step; see that page for the gen-1 generators.

### 6.1 Host MT19937-64 — inline at `InstVisitor+0x3E0`

The constructor seeds it (`@0x1bc330`, verbatim):

```c
*(_QWORD *)(a1 + 992) = 42;            // mt[0] = 42
v46 = 42;
do {                                   // Knuth init recurrence, i = 1..311
    v47 = 0x5851F42D4C957F2DLL * (v46 ^ (v46 >> 62)) + i;
    *(_QWORD *)(a1 + 8*i + 992) = v47;
    v46 = v47;
} while (v45 != 312);
*(_QWORD *)(a1 + 3488) = 312;          // mti = N = 312 ("needs regen")
```

So `+992 .. +3487` is `mt[312]` and `+3488` is `mti` (init 312). The draw + temper, read off `visitInstMemset @0x1d5060`:

```c
// host draw — getRandomInt<uint32_t> inlined in visitInstMemset @0x1d5060
if (mti > 0x137) {                     // 0x137 = 311 → state exhausted
    std::mersenne_twister_engine<unsigned long,64,312,156,31,
        0xB5026F5AA96619E9, 29, 0x5555555555555555, 17, 0x71D67FFFEDA60000,
        37, 0xFFF7EEE000000000, 43, 0x5851F42D4C957F2D>::_M_gen_rand(&mt);  // refill 312 words
    mti = *(a1 + 3488);
}
mti = *(a1 + 3488); *(a1 + 3488) = mti + 1;
y  = mt[mti];
y ^= (y >> 29) & 0x5555555555555555;                       // tempering, MT19937-64
y ^= (y << 17) & 0x71D67FFFEDA60000;
y ^= (y << 37) & 0xFFF7EEE000000000;
y ^= (y >> 43);
result = y + distr;                    // distr = 0xFFFFFFFF00000000 (see GOTCHA)
```

> **QUIRK — the temper masks are textbook MT19937-64; the "correction" is the `std::uniform_int_distribution` adapter, not the generator.** IDA recovers the full `std::mersenne_twister_engine` template instantiation, and its parameters are *exactly* the canonical MT19937-64 constants: word size 64, `n=312`, `m=156`, `r=31`, `a=0xB5026F5AA96619E9`, tempering `(u,d)=(29, 0x5555…)`, `(s,b)=(17, 0x71D67FFFEDA60000)`, `(t,c)=(37, 0xFFF7EEE000000000)`, `l=43`, init multiplier `f=0x5851F42D4C957F2D`. There is **no** deviation in the recurrence or temper. What *looks* like a deviation in the decompile is the trailing `+ distr` and the lazily-`__cxa_guard`-initialised static `getRandomInt<uint32_t>::distr = 0xFFFFFFFF00000000` — that is the `std::uniform_int_distribution<uint32_t>`'s un-set-params sentinel and the high-half mask the adapter folds onto a 64-bit temper to produce a 32-bit draw. The MT engine itself is bit-exact to the standard. **[CONFIRMED — the `_M_gen_rand` template instantiation and all four masks are read verbatim from `visitInstMemset`; the `distr` static and its `__cxa_guard` are in the same body.]**

### 6.2 Device Xorwow — per-op, allocated on demand

`XorwowState = {uint32 s0,s1,s2,s3,s4, uint32 counter}` (6×`uint32`). `generateXorwowRandom(XorwowState&) @0x1aa180`, verbatim:

```c
// generateXorwowRandom @0x1aa180  (classic Xorwow, via an SSE shuffle)
uint32 t = s0;
// SSE: load {s1,s2,s3,s4} and store back at {s0,s1,s2,s3} (the shift register slide)
s0=s1; s1=s2; s2=s3; s3=s4;
t ^= (t >> 2);
t ^= (t << 1);            // 2 * (...)
t ^= s4 ^ (s4 << 4);      // s4 ^ (16 * s4)
s4 = t;
counter += 362437;
return counter + t;
```

`Rand2` (IT 97) and `Dropout` (IT 65) build a `std::vector<XorwowState>` (4 lanes per partition for `Rand2`), seed each from the instruction's `Seed` operand, and stream Xorwow values. `Rand` (IT 60) is the **gen-1** path: it reads the op's PRNG **kind** field (`RandomAlgorithmKind` at `InstRand+0xF0`) and selects one of `"Supported PRNGs are LFSR/PCG32/PHILOX_1: "` — *not* Xorwow (`getDtypeSize(Seed.getType()) == 4` / `"Invalid seed type"` guard the seed dtype). All three gen-1 generators are implemented (LFSR taps `{32,22,2,1}`, PCG-XSH-RR 64/32, Philox-4x32-10) — see the [dedicated RNG kernels page](sim-bn-rng-collective-dma.md#21-gen-1--visitinstrand-it-60-3-way-prng-switch). `RandGetState`/`RandSetState` (IT 98/99) and `Get`/`SetRandState` (IT 58/59) read/write the device state vector to/from a tensor — the RNG-state save/restore opcodes. **[Xorwow body CONFIRMED here; the gen-1 LFSR/PCG32/Philox bodies are CONFIRMED on the dedicated page (per-constant `movabs`/tap anchors).]**

---

## 7. The `nki_klr_sim` driver — klr → BIR → sim → golden-check

`nki_klr_sim` is the **standalone functional-verification arm** of the beta2 KLIR path: take a serialized KLR program on the command line, lower it to BIR in-process, run that BIR on this simulator, and golden-check the outputs against `.npy` files. It is the file-driven counterpart of the in-compiler `bir_sim` walrus pass. It is a **separate binary** (not part of `libBIRSimulator.so`).

> **GOTCHA — `nki_klr_sim` does NOT embed the simulator; it dynamically links it.** `readelf -d` lists `NEEDED: libBIRSimulator.so, libpwp_sim.so, libBIR.so, …`, and the `birsim::*` symbols seen at high addresses in `nki_klr_sim` are 8-byte PLT trampolines, not bodies. Confirmed by the import table: `nki_klr_sim` imports `birsim::InstVisitor::visit(bir::Module&)`, `compareOutputs`, `writeOutputs`, `load_npy_to_object`, and `NeuronCoresManager::setInstVisitorforCore` from `libBIRSimulator`. The 110-case dispatch, the per-op math, and the `allclose` loop all live in `libBIRSimulator.so`. **[CONFIRMED — `native_imports.json` lists the five `UND birsim::…` symbols.]**

### 7.1 The driver flow

```c
// nki_klr_sim main @0x4bb2d0
main(argc, argv, envp):
    ParseCommandLineOptions(argc, argv, ...);   // populate the cl::opt/cl::list globals
    sub_4D3B20(&state);   // STAGE A: KLR → BIR load + bind .npy files
    sub_4D5550(&state);   // STAGE B: run the walrus pass pipeline (incl. bir_sim)
    sub_4F0070(&state);   // STAGE C: teardown / output flush
    return 0;
```

**STAGE A** (`sub_4D3B20 @0x4d3b20`) loads the KLR file (`sub_4D2820`, `nki_klr_sim.cpp:31`): it asserts the **exact** version `major==0 && minor==0 && patch==12` (KLR format **0.0.12**) and `contents->tag == klr::Contents::Tag::lnc`. It then mints a `bir::Module` (arch from `--target` via `string2ArchLevel`), lowers KLR→BIR (the `libwalrus` `lowerKLIRToNKI` family, statically linked here), and **binds** each `--input-files` / `--output-files` `.npy` to its `MemoryLocation` via `setFile` — the output files are the **golden** tensors. **[CONFIRMED — full body read; version + tag asserts.]**

**STAGE B** runs the pipeline; `bir_sim`'s `runPhysicalCore @0x5863b0` is the per-core driver. Per core it:

1. branches on `--mem-mode` `{0=symbolic (default), 1=physical, 2=autoMem}` — builds a `birsim::Memory` (symbolic) or `birsim::PhysicalMemory` (physical), or scans the BIR's `MemoryLocationSet`s to decide (autoMem);
2. optionally loads a bir-json tensor-map (`loadJsonFile` → `Memory+0x40`);
3. assigns instruction ordinals (walk `main` + all functions, number each `Instruction+68`);
4. **constructs** the sim visitor: `InstVisitor::InstVisitor(Memory*, SyncMode, artifactAbsPath, Config, bool, coreId, bool)`;
5. **runs** the sim: `birsim::InstVisitor::visit(module)` — the 110-case dispatch ([§1](#1-the-dispatch--one-110-case-switch-on-instruction0x58)) executes every op against the memory model;
6. **dumps** (rank-head core only, `coreId % getNumCoresPerLNC()==0`): `writeOutputs()` → `<name>-birsim.npy`;
7. **golden-checks** (if `--enable-check-outputs`, default on): `compareOutputs`.

### 7.2 The golden check

`compareOutputs(pair<float,float> tol, vector<string> goldenFiles, ostream& log, bool flatten) @0x1ad550` (in `libBIRSimulator`):

- **tolerance** `--birsim-output-tolerance` is a `cl::list<float>` — the user passes `"rel,abs"`; the default pair decodes to `tol = {1.0, 1e-05}` (the inline `xmmword 0x3727C5AC3F800000`).
- For each output `MemoryLocation`, load the matching golden `.npy` (`load_npy_to_object`); guard dtype (`"Golden and Generated Files should have same Dtype"`) and shape (`"Matrix dimension mismatch"`).
- `ArrayEqual` first; on miss → `AllClose`: pass iff `|gen − gold| <= abs + rel*|gold|` (numpy-allclose semantics), NaN/inf-aware (`"with mismatched NaNs"` / `"…infinities"`). A TBB `blocked_range` `parallel_for` runs the per-tensor compare; `CallCompare<Dtype>::computeMemoryObjectHash` gives a quick-equality hash.

> **GOTCHA — a golden mismatch FAILS compilation.** When `compareOutputs` returns "mismatch", the driver emits the captured diff at ERROR level and raises `NeuronAssertion` ErrorCode `1455` (LNC, `bir_sim.cpp:405`, fatal) or `1454` (non-LNC, `bir_sim.cpp:407`) — both resolve to `RESOLUTION_CONTACT_SUPPORT` — **unless** `--ignore-mismatch-error` is set. `--birsim-output-flatten` flattens multi-dim tensors to 1-D before the `allclose` ("FLATTENED " path). The `checkOutputs` variant (gated by `--check-inst-output-NaN`) is the NaN/inf sanity check. **[CONFIRMED — `runPhysicalCore` body + the 1454/1455 assert sites; the element-wise `allclose` inner loop is read at functor-dispatch level, INFERRED for the exact histogram binning.]**

This makes the simulator the same tool used to validate **both** an already-allocated BIR (post-allocator, physical memory, skip the SB coloring allocator) **and** an unallocated KLR (pre-allocator, symbolic memory, run coloring) — the auto-decision rides the bir-json's presence/absence of `{partition_offset, free_offset}` per tensor. Cross-references: the reference evaluator the golden tensors ultimately trace back to is [`xla_infergoldens`](../frontend/xla-infergoldens.md); the opcode enum the dispatch keys on is [`InstructionType`](instruction-type.md).

---

## 8. Adversarial self-verification

The five strongest claims were re-checked against the binary:

1. **110-case dispatch.** `switches.json` for `func_addr 0x26b380` reports `ncases 110` with 110 listed `(value→target)` pairs; the `visit` disassembly's prologue (`mov eax,[rsi+0x58]; cmp eax,0x69`) and the three pre-switch tail-jumps match byte-for-byte. **VERIFIED.** *Subtlety surfaced:* the static table routes **14** values to the base block `0x26b400` (incl. the three dead CF slots); the *runtime* base set is 11. Documented as a GOTCHA ([§1.1](#11-the-pre-switch-control-flow-interception)). **[CONFIRMED.]**
2. **State sub-models.** `getRegState = this+336` and `getSyncState = this+416` are one-line accessors; the ctor writes `Memory*` at `+312` and constructs `PWPSim::Simulator` at `+664`. **VERIFIED. [CONFIRMED.]**
3. **MT19937 corrections.** The seed loop (`mt[0]=42`, `×0x5851F42D4C957F2D`, `>>62`, terminate at 312, `mti=312`), the full `std::mersenne_twister_engine` template parameters, and the four temper masks are read verbatim from `visitInstMemset`. The only "correction" is the `uniform_int_distribution` adapter (`distr=0xFFFFFFFF00000000`, `__cxa_guard`-initialised); the generator is textbook-exact. **VERIFIED. [CONFIRMED.]**
4. **`nki_klr_sim` pipeline.** Its `native_imports.json` lists the five `UND birsim::…` symbols (visit / compareOutputs / writeOutputs / load_npy_to_object / setInstVisitorforCore) — so the tool links the sim, does not embed it. **VERIFIED. [CONFIRMED.]** *Caveat:* the `nki_klr_sim` body addresses in [§7](#7-the-nki_klr_sim-driver--klr--bir--sim--golden-check) come from the standalone tool's own report (cp310-pinned); the corpus IDA DB for that binary is cp312 (addresses drift). The *import relationship* is verified directly on the cp312 DB.
5. **DenseMap memory model.** The `Memory` ctor installs the local `DenseMap` at `+8` and the shared map at `+104`; `getMap` routes shared vs local. The `MemoryObject` ctor allocates the DATA array (`memset 0xFF`) and the optional shadow (`memset 0x00`). The `Semaphores` ctor's `operator new(0x400)` + `+128` end pointer prove the 256-slot bank. **VERIFIED. [CONFIRMED.]**

**Honest re-verify ceiling.** Pinned at the strongest level: the dispatch table, the four routing classes, the `InstVisitor`/`Memory`/`MemoryObject`/`Semaphores` field maps, the MT19937-64 generator, the Xorwow body, and the `nki_klr_sim` import relationship. **Not** fully pinned (left to the planned per-op pages 7.35–7.40 and the debugger page 7.41): the `runAP` multi-dim stride loop element-by-element; the `interval_map` node-internal merge; the loop/do-while carried-state threading; the device PRNG-kind dispatch beyond Xorwow; the `compareOutputs` element-wise histogram binning; and the `RegState` 64-bit Hi/Lo register-pair arithmetic. The `visit` *defining* C body is unavailable (Hex-Rays failed on it) — the dispatch table here is reconstructed from `switches.json` + the `.asm` + the thunk↔body sidecar pairing, which is complete and unambiguous for the table itself.

---

## See also

- [`InstructionType` — the 110-opcode enum & `sameInst` family masks](instruction-type.md) — the opcode enum this dispatch keys on (`Instruction+0x58`).
- [The `bir::Instruction` base struct & the `+0xD0` sched/dep block](instruction-base.md) — the `SyncInfo` the per-BB driver reads wait/update sets from.
- [MemoryLocation / Storage & the alias model](memory-location.md) — the `MemoryType`/`allocated`/`addr` fields the memory model resolves on.
- [`xla_infergoldens` — the reference evaluator](../frontend/xla-infergoldens.md) — where the golden tensors the simulator compares against come from.
