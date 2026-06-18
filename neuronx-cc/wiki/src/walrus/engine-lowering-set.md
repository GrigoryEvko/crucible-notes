# The Engine-Lowering Set — `lower_sync` / `lower_act` / `lower_dve` / `lower_ap`

> **Version pin —** neuronx-cc `2.24.5133.0+58f8de22`. All symbol names and virtual
> addresses are taken from `libwalrus.so` (the heavy back-end the `walrus_driver`
> ELF declares `NEEDED`). This `.so` **retains its full dynamic symbol table**, so
> every `name → VA` here is `nm -D[C]`-confirmed; for `.text`/`.rodata` the VA equals
> the file offset and the bodies were disassembled in place (`objdump -d -M intel`).
> The flat decompiled sidecars for these classes are PLT *thunks* (low addresses
> `0x5e9020–0x62d650`); the **real** bodies live high (`0x114xxxx–0x11cxxxx`) and
> were read from disassembly. Addresses below are the cp310 frame; the cp312 wheel
> ships the same symbols a few bytes apart (e.g. `convertSymAP` at `0x11b7ee0` vs the
> cp310 `0x11b7f80`) — this is the two-VA-frame artifact, treat each address as
> build-pinned.

## Abstract

After the walrus back-end has split every instruction onto a concrete engine
(`expand_all_engine_pre_alloc_sema_and_reg`, pass **L19**), four of the remaining
low-level passes do the *per-engine final lowering*: each takes an op that already
lives on engine `E` and rewrites it into `E`'s native micro-op form. They are **not**
"pick the engine" passes — the engine is already chosen — they are "given this op on
engine `E`, encode it the way `E`'s sequencer / datapath wants it" passes.

This page documents the four:

- **`lower_sync`** (L27, SP engine) — the **semaphore peephole**: it does not invent
  semaphores (`alloc_semaphores`@L22 did that); it converts each instruction's
  `SyncInfo` wait/update records into the *minimal* set of sequencer micro-ops,
  exploiting threshold monotonicity and per-DMA-queue FIFO ordering.
- **`lower_act`** (L28, Activation/PWP engine) — a **thin shell** that constructs and
  runs `LowerPWPImpl`, the piecewise-polynomial-LUT lowering machinery. The shell is
  trivial; the LUT-set-cover logic is `LowerPWPImpl` and is documented in depth in
  Part 10 (`activation/`, planned) — here we pin only the handoff.
- **`lower_dve`** (L31, DVE/Vector engine) — runs the **per-arch `CoreV*Gen` encoder**
  directly over the DVE ops after selecting a datapath microcode *profile*
  (`default` / `transformer` / `saturate`) whose opcode coverage is verified.
- **`lower_ap`** (L32, engine-agnostic) — the **AP → wire-descriptor bridge** and the
  deepest of the set. `convertSymAP` lowers a `SymbolicAccessPattern` (kind 2, whose
  strides are *expression trees* over loop axes) into a `RegisterAccessPattern`
  (kind 3, register-driven) by **emitting** the register-ALU sequence that computes
  the runtime address into a freshly-minted virtual register. It runs **last before
  register allocation** precisely because it mints the registers the allocator colors.

The sibling SP-control pass `lower_branch`@L20 (predicates → SP `CompareAndBranch`
micro-ops, structured `Loop`→back-edge explosion) and its boundary with the
pre-codegen `lower_control` pass are covered separately; this page is the *sync / act /
dve / ap* quartet.

| | |
|---|---|
| **Pipeline stage** | back-end *low-level* pipeline (`sub_805870`), all **after** the per-engine split at L19 |
| **L27 `lower_sync`** | `LowerSync::run(Module&)` @ `0x11b1a20` → `LowerSync::run(BasicBlock&)` @ `0x11afa00` |
| **L28 `lower_act`** | `LowerAct::run(Module&)` @ `0x114e280` (shell) → `LowerPWPImpl` |
| **L31 `lower_dve`** | `LowerDVE::run(Module&)` @ `0x1170b10` |
| **L32 `lower_ap`** | `LowerAP::run(Module&)` @ `0x11ba970` → `convertSymAP` @ `0x11b7f80` |
| **Engine crosswalk** | SP=6, Activation=2, DVE=5, AP=any (`EngineType`, D-D03) |
| **Ordering spine** | `lower_ap`@L32 → `coloring_allocator_reg`@L33 → `codegen`@L40 (hard chain) |

## Where the four passes sit

The walrus back-end is split (per the recovered `walrus_pass_list_complete` ordering)
into a **high-level** pre-codegen pipeline (passes ~1–131, ending at `lower_control`@128)
and a **low-level** pipeline (`sub_805870`) run after it. All four passes here are in
the low-level pipeline. The byte-confirmed local order is:

```text
L17  lower_dma                                  [→ Pool engine]
L19  expand_all_engine_pre_alloc_sema_and_reg   ← per-engine SPLIT happens here
L20  lower_branch        [SP(6) branches]        LowerBranch::run
L21  synchronizer
L22  alloc_semaphores
L27  lower_sync          [SP(6) sync]            LowerSync::run        ◄ this page
L28  lower_act           [ACT(2)]                LowerAct::run→LowerPWPImpl  ◄
L29  optimize_prefetch_act_control              OptimizePrefetchActLoad
L30  optimize_act_control                        OptimizeActControl
L31  lower_dve           [DVE(5)]                LowerDVE::run         ◄ this page
L32  lower_ap            [AP lowering]           LowerAP::run          ◄ this page
L33  coloring_allocator_reg  [REG alloc]         ← consumes lower_ap's regs
L37  expand_all_engine_final_pre_codegen
L40  codegen             [Generator/CoreV*Gen → 64-B TPB bundles]
```

> **Structural fact (CONFIRMED).** Because L27/L28/L31/L32 all run **after** the L19
> per-engine split, every instruction they touch already carries a concrete
> `EngineInfo`. That is why the passes are engine-specialised (sync→SP, act→ACT,
> dve→DVE) rather than engine-selecting. And `lower_ap`@L32 sits **immediately before**
> register allocation@L33 because it *mints* the virtual registers
> (`Function::addRegister`) that L33 colors — that ordering is the spine of the whole
> stage. `lower_ap`@L32 → regalloc@L33 → codegen@L40 is a hard dependency chain.

### Shared SP-microcode emitters

`lower_sync`, `lower_branch`, and `lower_ap` all draw on the same set of
`neuronxcc::backend::` free functions — the register-ALU + branch primitive set
(all `nm -T`-confirmed):

| Emitter | VA | Role |
|---|---|---|
| `lowerAffineExpr(Inst*, QuasiAffineExpr*, Register* dst, Register* tmp, BB*)` | `0x109b5b0` | affine index expr → reg-ALU op sequence |
| `addRegMov(Inst*, name, ValueUnion, Register*, BB*, EngineType)` | `0x109b220` | immediate / copy into a register |
| `addRegAluOneRegArgOneImm(...)` | `0x109aad0` | `dst = reg op imm` |
| `addRegAluTwoRegArg(Inst*, name, AluOpType, Reg a, Reg b, Reg dst, BB*)` | (T) | `dst = a op b` |
| `addRegAluTwo64bitRegArg(Inst*, name, AluOpType, Register*×5)` | (T) | 64-bit ALU (DRAM byte-address path) |
| `addCompareAndBranch(...)` | `0x109bea0` / `0x109c110` / `0x109c240` | 3 overloads (IMM / +reg / +reg,reg) |
| `addUnconditionalBranch(BB*, EngineType, name, BB* target)` | `0x109bcf0` | fallthrough / loop entry branch |

These are the substrate the per-engine passes call to splice physical micro-ops into a
basic block. (The cp312 binary confirms the exact same set, with the three
`addCompareAndBranch` overloads distinguished by their trailing operands —
`(…, BB*, BB*)`, `(…, BB*, BB*, Register*, ValueUnion)`, `(…, BB*, BB*, Register*,
Register*)` — i.e. the static / register-rhs / both-register comparison forms.)

---

## `lower_sync` (L27) — the SP semaphore peephole

**Entry:** `LowerSync::run(Module&)` @ `0x11b1a20` (outer; logger banner + per-BB loop)
→ `LowerSync::run(BasicBlock&)` @ `0x11afa00` (the core, `0x2020` B). It runs **after**
`synchronizer`@L21 and `alloc_semaphores`@L22, so the dependency→`SyncInfo` derivation
and the semaphore-id allocation are already done. `lower_sync`'s job is to **minimize**
the wait set and **emit** the wire form — it never allocates a semaphore.

### Input model — `SyncInfo`

Each `Instruction` carries a `SyncInfo` (a `boost::optional`) read via
`Instruction::getSyncInfo()`:

```c
// SyncInfo = optional{ SmallVector<sync::Wait,1> on_wait;
//                      SmallVector<sync::Update,1> on_update }   [D-E19]
struct sync::SyncRef { /* +0x08 */ u32 type;   // event=0 / semaphore=1
                       /* +0x0C */ u32 id; };   // allocated sema / event id
struct sync::Wait   { /* +0x10 */ u32 mode;    // evt-set-clear / sem-ge-imm / sem-ge-reg
                       /* +0x14 */ u32 value;   // immediate threshold
                       /* +0x18 */ Register* reg;
                       /* +0x20 */ Inst* from; }; // producer instruction
struct sync::Update { /* +0x10 */ u32 mode;    // evt-set / sem add/sub/inc/dec / wr-imm
                       /* +0x14 */ u32 value;
                       /* +0x18 */ u32 target_core; };  // → isRemote()
```

The core (`LowerSync::run(BasicBlock&)`) walks the block, pulls each instruction's
`SyncInfo`, runs the two elimination rules over `on_wait`, then back-links the surviving
waits as dependency edges (`Instruction::addDependency(Inst*, EdgeKind, bool)`, using the
`Invalid<Ordered<Anti<Output<Flow` precedence from the dependency model). The wait/update
records that survive are what codegen@L40 later stamps into the SP sequencer's
`wait sem >= value` / `update sem += value` bundle (`visitInstEventSemaphore` /
`GroupResetSemaphores`, IT 13/14) — `lower_sync` itself stops at the elimination +
edge-relink.

```c
// LowerSync::run(BasicBlock& BB):
for (Instruction& I : BB) {
    SyncInfo s = I.getSyncInfo();
    if (!s) continue;
    elimWaitByDMATriggerOrder(s.on_wait);   // (b) per-DMA-queue FIFO order
    elimDeadWait(s.on_wait);                // (a) value-threshold dominance
    // surviving Wait/Update become SP sequencer wait/post micro-ops,
    // back-linked via addDependency(from, EdgeKind, …)
}
```

### Rule (a) — value-dominance: `elimDeadWait` @ `0x11ae8e0`

The substance of the pass. It **groups waits by `SyncRef` `{type,id}`** and, for two
waits on the *same* semaphore, the higher `value` threshold subsumes the lower:
a `sem >= 5` wait already implies `sem >= 3`, so the `sem >= 3` wait is dropped from the
`SmallVector`.

> **Binary confirmation (CONFIRMED).** Disassembling `elimDeadWait` (cp312 `0x11ae840`)
> shows the exact shape: it calls `sync::SyncRef::getType` and `sync::SyncRef::getId`
> (the group key), `sync::Wait::getMode`, walks an Rb-tree
> (`std::_Rb_tree_increment`) keyed on that `SyncRef`, and performs **two**
> `sync::Wait::getValue()` calls back-to-back at `0x11aec46` / `0x11aec52` — the
> pairwise threshold compare. That is the value-dominance test on the binary.

### Rule (b) — DMA-queue FIFO order: `elimWaitByDMATriggerOrder` @ `0x11aee20`

On a *single* DMA queue the hardware completes blocks **in order**, so a wait on a later
block of that queue implicitly satisfies waits on earlier blocks of the *same* queue —
the earlier waits are redundant and can be eliminated (possibly with a consolidated
`sync::Update`).

> **Binary confirmation (CONFIRMED).** `elimWaitByDMATriggerOrder` (cp312 `0x11aed80`)
> calls `sync::Wait::getFrom()` (the producer), `InstDMABlock::classof` (is the producer
> a DMA block), then `InstDMABlock::getDMAQueue()` and `InstDMABlock::getBlockId()`, and
> builds a `DenseMap<const DMAQueue*, u32>` plus a
> `SmallVector<pair<const DMAQueue*, std::map<u32 blockId, sync::Wait*>>>` — i.e. *per
> queue*, an ordered map from block-id to the wait on it. Walking that map drops every
> wait whose block-id is dominated by a later wait's block-id on the same queue.

**Net:** `lower_sync` is the SP-engine semaphore **peephole + relinker**. It exploits
(a) threshold monotonicity and (b) per-queue DMA completion ordering to cut the wait set
to the minimum that still enforces the program's dependencies, and leaves the byte-level
sequencer emit to codegen@L40.

---

## `lower_act` (L28) — the LowerPWP shell

**Entry:** `LowerAct::run(Module&)` @ `0x114e280`. This pass is a **thin shell**: it
constructs a `LowerPWPImpl(Module&, PassOptions const&, Logger&)` and runs it. "PWP" is
the Activation/Scalar engine's programmable piecewise-polynomial LUT processor
(`EngineType` 2; external name "Scalar"). All the real work — the LUT-set cover, the
`LoadActFuncSet` insertion, the resident-set tracking — is `LowerPWPImpl` and is the
subject of Part 10 (`activation/`, planned); this page pins **only the shell and the
handoff**, and does not re-derive the PWP machinery.

> **Binary confirmation (CONFIRMED — shell).** Disassembling `LowerAct::run`
> (cp312 `0x114e1e0`) shows it call the
> `LowerPWPImpl::LowerPWPImpl(bir::Module&, PassOptions const&, logging::Logger&)`
> constructor at `0x114e39f`, bracketed by `InstPrinter::visitInstruction` calls (the
> `--debug` IR dumps). There is no per-instruction lowering logic in `LowerAct::run`
> itself — it builds the impl and runs it.

The downstream impl, for the record (addresses CONFIRMED via `nm`, semantics deferred to
Part 10): `LowerPWPImpl::visitInstruction` @ `0x1151110` dispatches on the opcode at
`inst+0x58` over `IT 6` `LoadActFuncSet`, `IT 4` `Activation` (function selected by
`ActivationFunctionType` @ `inst+0x90`), `IT 101` `ActivationReadAccumulator`, and the
`IT 5` Activation-family neighbor; `calculateBestSets` @ `0x11597e0` runs a set-cover
over the 29 LUT functions; `addLoadInstructionBefore` @ `0x1155620` inserts the minimal
`IT-6` loads. The two companion passes `optimize_prefetch_act_control`@L29 (hoist a LUT
load to hide latency) and `optimize_act_control`@L30 (`OptimizeActControl` @ `0x11603b0`,
across-BB dedup of resident sets via a `0xFFFFFFFF` "no active set" sentinel) round out
the trio, but they are *separate* passes, not part of the `lower_act` shell.

---

## `lower_dve` (L31) — the DVE encoder run

**Entry:** `LowerDVE::run(Module&)` @ `0x1170b10`. Unlike the other three, `LowerDVE`
does **not** stop at a BIR-level rewrite — it drives the per-arch `Generator` visitor
*directly*, the same encoder infrastructure codegen@L40 uses. It selects the right
datapath microcode profile, verifies coverage, then runs `CoreV2Gen` / `CoreV3Gen` /
`CoreV4Gen` over the DVE-engine ops. DVE is `EngineType` 5 (external name "Vector");
its ops are `TensorTensor` / `Select` / `CopyPredicated` / reduce / compare /
`ReadAccumulator` etc.

### Datapath-table selection — the distinguishing step

```c
// LowerDVE::run drives the encoder per arch level:
fillAllDVEInfos(json, profileName);     // @0x116f9d0 — parse dve_info.json → DVETableInfo
                                        //   per EngineInfo (DenseMap<EngineInfo,DVETableInfo>)
findCoreDVETable(generator, module);    // @0x116f8a0 — pick the matching profile
checkMissingOpcodes(usedOpcodeMap);     // @0x116b260 — DenseMap<EngineInfo,DenseSet<u32>>
                                        //   coverage guarantee, else error
// then, per arch:
CoreV2Gen / CoreV3Gen / CoreV4Gen ctor;        // arch-specific encoder
.enterModule(M); .enterFunction(F); .enterBasicBlock(BB);
IRVisitor<CoreV*Gen>::visit(I);                // stamp each DVE op into its bundle
// finally: flushISAChecks() + printInstrStats() + assertAsmMappingsCorrect()
```

`findCoreDVETable` walks the module's basic blocks, builds a
`DenseMap<EngineInfo, DenseSet<u32 opcode>>` of every DVE op actually used, then
`checkMissingOpcodes` verifies the chosen table's `ops` list is a superset (else it
errors). The microcode itself comes from the shipped JSON tables.

> **Binary confirmation (CONFIRMED — encoder dispatch).** `LowerDVE::run`
> (cp312 `0x1170a70`) calls `fillAllDVEInfos` (twice), constructs `CoreV2Gen`
> (`0x11710c6`), `CoreV4Gen` (`0x117169e`), and `CoreV3Gen` (`0x1171793`), and for each
> calls `CoreV2GenImpl::enterModule` / `enterFunction` / `enterBasicBlock` followed by
> `IRVisitor<CoreV2Gen|CoreV3Gen|CoreV4Gen>::visit(Instruction&)`. The three encoder
> classes are dispatched by arch level — `lower_dve` is the only one of the four that
> reaches all the way into the codegen-grade `Generator` encoders.

### The shipped per-gen profiles

**Source:** `neuronxcc/dve/dve_bin_gen{2,3,4}/dve_info.json` (one per ArchLevel). Each
`dve_info.json` declares `"dve_table_keys": ["opcode_table","control_table",
"datapath_table"]` and a list of named `tables`, each carrying a `<name>_opcode_table.bin`
/ `<name>_control_table.bin` / `<name>_datapath_table.bin` triple plus the `ops` list it
covers. The named profiles are the *workload specialization* `findCoreDVETable` chooses
between.

> **Binary confirmation (CONFIRMED — profile counts, via `jq` on the shipped JSON).**

| Gen | Profiles (`name` : `len(ops)`) |
|---|---|
| **gen2** | `default`:46 · `transformer`:47 · `transformer_fwd`:44 · `transformer_bwd`:45 · `transformer_bwd2`:44 |
| **gen4** | `default`:59 · `saturate`:59 · `transformer`:43 |

`findCoreDVETable` picks the named profile (`default` vs a `transformer`-specialised
table vs `saturate`) whose `ops` superset covers the module's used-DVE-op set; the
selected `datapath_table.bin` is the microcode the `CoreV*Gen` visitor stamps into each
bundle, and `checkMissingOpcodes` is the guarantee the chosen profile can express every
DVE op the module emits.

> **Scope note (INFERRED boundary).** The internal byte layout of
> `datapath_table.bin` / `control_table.bin` is *not* decoded here — this page
> establishes the **selection** mechanism (`findCoreDVETable` + `checkMissingOpcodes` +
> the per-gen JSON profiles), not the microcode contents.

---

## `lower_ap` (L32) — the SymAP → RegisterAP bridge

This is the access-pattern → wire-descriptor bridge and the deepest pass of the set.
It runs **last before register allocation** because it mints the registers the allocator
colors. `lower_ap` is engine-agnostic — it rewrites the AP operand of *any* op,
regardless of which engine that op landed on.

### The AP class hierarchy it transforms

```c
// bir::AccessPattern (base, kind 0) : public bir::Argument(+0) + SrcHandle(+0x48)   [D-E12]
//   ├ PhysicalAccessPattern   kind 1 "physical_ap"  0x1E0 B  (concrete strides + offset)
//   ├ SymbolicAccessPattern   kind 2 "symbolic_ap"  0x1B8 B  (pelican::Expr step/num)
//   └ RegisterAccessPattern   kind 3 "register_ap"  0x118 B  (register-offset, DYNAMIC)
//
// A SymbolicAccessPattern's `addrs` @+0xE0 is a std::vector<QuasiAffineExpr>; each
// QuasiAffineExpr is 32 B = { +0x00 pelican::RefPtr<Expr> ; SmallVector<LoopAxis*> }.
// i.e. the step/num of a symbolic AP are EXPRESSION TREES over loop axes, not ints.
```

`lower_ap` lowers **kind-2** (symbolic) APs into **kind-3** (register) APs, plus the
SP register-ALU instruction sequence that computes the runtime offset. (The Penguin
middle-end has its own twin of this kind-2 → kind-3 materialization; this is the
*back-end* counterpart — see the cross-reference below.)

### Driver and recursion

```c
// LowerAP::run(Module&) @0x11ba970:
//   logger banner; `cmp ebx,0x13` arch guard (same sentinel as lower_branch);
//   for each Function: convertSymAPtoRegAP(BB) over every BasicBlock.

// LowerAP::convertSymAPtoRegAP(BasicBlock& BB) @0x11ba8b0  — RECURSIVE:
for (Instruction& I : BB) {
    if (opcode(I) == 0x69 /* 105 = Loop */)         // structured Loop body:
        for (BasicBlock& inner : I.holder.blocks()) //   recurse into nested blocks
            convertSymAPtoRegAP(inner);
    else
        convertSymAPForInst(I);                      // per-inst orchestration
}
```

`convertSymAPForInst` @ `0x11b9fd0` walks the instruction's operand / symbolic-arg
chains (`Instruction` fields `+0x80` AP-list ptr, `+0x88` count, `+0xA8/+0xB8/+0xC8`
operand chains). For each symbolic entity it: dedups it
(`collectSymArg` @ `0x11b7a60`, into a `DenseSet<SymbolicImmediateValue*>` and a
`DenseSet<SymbolicAccessPattern*>` so each is lowered exactly once per inst); resolves a
`SymbolicImmediateValue` to exactly one value (else `reportError` "…must evaluate to a
single value") and replaces it with a fresh `bir::ImmediateValue`; and calls
`convertSymAP` for the AP geometry.

### The SymAP → RegisterAP geometry — `convertSymAP` @ `0x11b7f80` (0x2050 B)

This is the central body. Verbatim from the disassembled call sequence, the algorithm is:

```c
// LowerAP::convertSymAP(SymbolicAccessPattern* symAP):
for (QuasiAffineExpr* axisExpr : symAP->getBlockAddrs() ++ symAP->getTileAddrs()) {
    if (arg->isLocationDRAM()) {
        // 64-bit byte-address path (full DRAM address):
        off = lowerAffineExpr64(axisExpr);          // addRegAluTwo64bitRegArg
    } else { /* arg->isLocationSB() || isLocationPSUM() */
        // 32-bit ALU + partition/bank split:
        off = lowerAffineExpr(axisExpr, dst, tmp, BB);   // Horner: step_k*axis_k + offset
    }
    if (axisExpr /* tensor-indirect */)
        off = addTensorLoad(I, name, idxMemLoc, regs…); // load index bytes from memory
}
Register* baseReg = F.addRegister(name, EngineType, width);  // the VIRTUAL reg L33 colors
MemoryLocation* loc = F.addMemoryLocation(name, …, Dtype, …, MemoryType, AddressSpace);

RegisterAccessPattern* regAP = new RegisterAccessPattern(I);   // kind 3
regAP->setLocation(storageBase, /*…*/);     // bind the MemoryLocation
regAP->setRegLocation(baseReg);             // → wire key "regref"        (+0xE8)
regAP->setRegAPOffset(offReg);              // → wire key "reg_ap_offset"  (+0xF0)
// regAP.const_ap_offset @+0x100 / const_sub_tensor_id @+0x108 = the static residue
replace symAP operand with regAP;           // Instruction::insertArgument/removeArgument
```

> **Binary confirmation (CONFIRMED — the entire algorithm).** Disassembling
> `convertSymAP` (cp312 `0x11b7ee0`) shows the call sites in *exactly* this order:
> `Argument::isLocationDRAM` (`0x11b7f54`); three `Function::addRegister` calls
> minting the offset registers; `SymbolicAccessPattern::getTileAddrs` (`0x11b8727`) and
> `getBlockAddrs` (`0x11b87a9`); `lowerAffineExpr` (`0x11b881f`, `0x11b8847`) lowering
> each affine index; `addTensorLoad` (`0x11b8fd0`, `0x11b92a3`) for the indirect path;
> `Argument::isLocationPSUM` (`0x11b934b`) / `isLocationSB` (`0x11b935b`) for the
> SB/PSUM branch; then the result build — `RegisterAccessPattern::RegisterAccessPattern`
> ctor (`0x11b95cd`), `setLocation` (`0x11b95f9`), `setRegLocation` (`0x11b9626`),
> `setRegAPOffset` (`0x11b9797`), and `Function::addMemoryLocation` (`0x11b9978`).
> Every step of §"geometry" is a real call in the body, in this sequence.

### What `lower_ap` computes vs what the codegen `CoreV*Gen` encoders compute

This is the bridge — the *division of labor* between L32 and L40. After `lower_ap`,
every AP operand is either **Physical** (kind 1, static byte address) or **Register**
(kind 3, runtime register-driven). The codegen@L40 encoders then **stamp** these into
the wire descriptor:

```c
// codegen@L40 (the CoreV*Gen encoder, NOT lower_ap):
assignStartAddr<NEURON_ISA_TPB_ADDR4>(slot, AP, allowed_in_psum);
assignAccess<TENSOR{1..4}D> / assignStaticPattern<TENSOR{1..4}D>;
assignAccess<MEM_PATTERN{2,3}D> / assignAccessForMX<MXMEM_PATTERN1D>;
```

The `ADDR4` dword carries a 2-bit MODE field (byte-3 bits 29..30) plus a register-mode
flag (bit 31, `0x80`):

| MODE | Meaning |
|---|---|
| `0b00` (`0x00`) | STATIC / immediate — Physical AP, plain SBUF/PSUM byte address |
| `0b01` (`0x20`) | TENSOR-INDIRECT — gather/scatter (index + data + scale ADDR4s) |
| `0b10` (`0x40`) | ACTIVE / explicit — register-allowed / 4-D active start path |
| `bit31` (`0x80`) | REGISTER-MODE (orthogonal): low byte = register id (`< 64`) |

For a kind-3 `RegisterAccessPattern`, `assignStartAddr<ADDR4>` does
`*(u8*)(slot+3) |= 0x80; *(u8*)(slot+0) = getRegId(regref)` — it reads the register id
that `lower_ap` put in the `RegisterAP` and stamps bit-31 + the reg id into the
descriptor. Decoders test `if (a1 >= 0)` (bit-31 clear ⇒ immediate) vs the register
branch.

```text
DIVISION OF LABOR
  lower_ap (L32, SEMANTIC half): decides STATIC-vs-DYNAMIC; for a dynamic AP it EMITS
      the register-ALU sequence that COMPUTES the runtime address into a Register, and
      records it as a kind-3 RegisterAccessPattern (regref + reg_ap_offset + const
      residue). It does NOT touch the ADDR4 bits — it works at the BIR operand-graph
      level.
  CoreV*Gen (L40, SYNTACTIC half): reads the now-non-symbolic AP kind and serialises the
      actual ADDR4 / TENSORnD descriptor bytes — kind 1 → static address + static
      (step,num) pairs; kind 3 → bit-31 register-mode + the reg id lower_ap minted +
      reg_ap_offset; tensor-indirect → bit-29 + the index/data/scale tri-ADDR4.
```

They **meet at the `RegisterAccessPattern` kind-3 object**: `lower_ap` produces it,
`assignStartAddr<ADDR4>` consumes it.

> **Why `lower_ap` must precede reg-alloc (CONFIRMED ordering).** The registers
> `lower_ap` mints (`Function::addRegister`) are *virtual*. `coloring_allocator_reg`@L33
> assigns them physical ids; codegen@L40's `assignStartAddr<ADDR4>` reads
> `getRegId()` = the **colored** id. `lower_ap`@L32 → regalloc@L33 → codegen@L40 is
> therefore a hard chain — which is exactly why `lower_ap` is the last BIR-rewrite pass
> before allocation.

---

## Adversarial self-verification

The five strongest claims on this page, re-checked against the binary, and the honest
ceiling on each:

1. **`convertSymAP`'s full call sequence (§geometry) — CONFIRMED.** Every call site
   (`isLocationDRAM` → `addRegister`×3 → `getTileAddrs`/`getBlockAddrs` →
   `lowerAffineExpr`×2 → `addTensorLoad`×2 → `isLocationPSUM`/`isLocationSB` →
   `RegisterAccessPattern` ctor → `setLocation` → `setRegLocation` → `setRegAPOffset` →
   `addMemoryLocation`) is a real, ordered call in the cp312 body at `0x11b7ee0`. This
   is the page's highest-value claim and it is byte-anchored end to end.

2. **`lower_sync`'s two peephole rules — CONFIRMED.** `elimDeadWait` calls
   `SyncRef::getType`/`getId` (group key) + an Rb-tree walk + **two** back-to-back
   `Wait::getValue()` (the pairwise threshold compare); `elimWaitByDMATriggerOrder`
   calls `Wait::getFrom` + `InstDMABlock::classof`/`getDMAQueue`/`getBlockId` and builds
   the per-queue `DenseMap<DMAQueue*,u32>` + `map<blockId, Wait*>`. Both shapes are in
   the binary.

3. **`lower_act` is a shell over `LowerPWPImpl` — CONFIRMED.** `LowerAct::run` calls the
   `LowerPWPImpl(Module&, PassOptions&, Logger&)` ctor and contains no per-inst lowering.
   The PWP set-cover internals are deferred to Part 10 and are tagged there, not here.

4. **`lower_dve` drives `CoreV{2,3,4}Gen` after `findCoreDVETable`/`checkMissingOpcodes`
   — CONFIRMED.** All three encoder ctors + their `enterModule`/`enterFunction`/
   `enterBasicBlock` + `IRVisitor<CoreV*Gen>::visit` are calls in `LowerDVE::run`, and
   the profile counts (gen2 5 tables 46/47/44/45/44, gen4 3 tables 59/59/43) match the
   shipped `dve_info.json` exactly under `jq`.

5. **The ADDR4 kind-3 register-mode stamp — STRONG (cross-strand).** The `bit-31 (0x80)`
   register-mode + reg-id stamp in `assignStartAddr<ADDR4>` is a **codegen** (L40)
   behavior verified in the ISA-encoding strand, not re-disassembled on this page;
   `lower_ap`'s half (producing the kind-3 `RegisterAccessPattern` with `regref` /
   `reg_ap_offset`) is CONFIRMED here, and the two-sided "meet at the kind-3 object"
   claim rests on the L32 producer being byte-anchored + the L40 consumer being
   byte-anchored in its own strand. The *division of labor* statement is therefore
   STRONG, not independently re-confirmed on this page.

**Ceiling / what is NOT pinned here.** The `cmp ebx,0x13` arch guard's precise
`ArchLevel → 0x13` mapping is INFERRED (the guard + `reportError` are CERTAIN; the exact
sentinel level was not chased into `ArchLevel2string`). The DVE `datapath_table.bin`
byte layout is out of scope (selection mechanism only). The PWP set-cover *cost model*
(which set to spill when over budget) is Part 10 territory and tagged STRONG-not-CERTAIN
there. The `value`/`mode` wire encodings of `sync::Wait`/`Update` are taken from the
dependency-model strand; `lower_sync` reads `getMode`/`getValue` but the final SP
sequencer byte emit is codegen@L40's.

---

## Cross-references

- **[Symbolic-AP → Register-ALU Materialization](../penguin/symbolic-ap-register-alu.md)**
  — the Penguin middle-end twin of the kind-2 → kind-3 materialization. `convertSymAP`
  on this page is the *back-end* counterpart of that work; both produce a register-driven
  AP, but at different IR layers.
- **`activation/` (Part 10, planned)** — the in-depth `LowerPWPImpl` piecewise-polynomial
  LUT machinery (`calculateBestSets` set-cover, `LoadActFuncSet` insertion, the resident-
  set sentinel). `lower_act` on this page is only the shell that hands off to it.
- **`walrus/lower-select-control-branch.md` (8.5, planned)** — the sibling SP-control
  lowering pass `lower_branch` (predicates → `CompareAndBranch`, structured-`Loop`
  back-edge explosion) and its boundary with the pre-codegen `lower_control` pass.
- **[The walrus Pass Pipeline & Optlevel Planes](pass-pipeline-optlevels.md)** — where
  L27/L28/L31/L32 sit in the full back-end pass sequence.
- **[BackendPass Hierarchy & the 150-Name→121-Class Registry](backendpass-registry.md)**
  — how the `lower_*` pass names map to their `BackendPass` subclasses.
