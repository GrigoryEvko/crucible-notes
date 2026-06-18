# Codegen Driver and the CodeGenMode Mechanism

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, binary `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331…`). VA == file offset in `.text` (base `0x62d660`) and `.rodata`. Other wheels differ; treat every address as version-pinned.*

## Abstract

`backend::Codegen` is the final BIR-to-silicon pass in the walrus backend. By the time it runs, the BIR linker has merged the per-engine functions into a single `Module` with every instruction already engine-assigned, scheduled, and register-coloured. Codegen's job is the last mile: walk that scheduled `Module`, encode each instruction into its 64-byte TPB wire bundle, and stream the bundles into per-engine `<EngineInfo>.bin` files that the NEFF packager later wraps. This page documents the **driver** — the arch-select, the per-engine emit loop, the three-mode `CodeGenMode` mechanism, and the post-emit ISA-legality flush. The bundle bit-layout itself lives in [Instruction Bundle](../isa/instruction-bundle.md); the per-engine `.bin` writer (createBin/setupHeader) and the silicon-legality validator (`runSingleISACheck`) are sibling pages in this part (planned).

The mental model from LLVM is `MCStreamer` / `AsmPrinter`: a CRTP visitor walks the IR, each visit method hands an encoded instruction to a streamer, and the streamer fans bytes into output sections. neuronx-cc's codegen is structurally the same — a `bir::IRVisitor<CoreVxGen>` CRTP walk over `Module → Function → BasicBlock → Instruction`, with each `visitInst<Op>` appending a fixed-size bundle to a `DenseMap<EngineInfo, FILE*>` of open streams. Two things diverge from the LLVM picture and are easy to get wrong. First, the encoder family is selected **once** at the top by hardware generation (`CoreV2Gen` / `CoreV3Gen` / `CoreV4Gen`) and thereafter reached purely through the vtable, so the driver is arch-agnostic past construction. Second, ISA wire-legality is **not** checked inline during emission — it is *deferred*: every encoded bundle is snapshotted into a queue, and a single TBB-parallel `flushISAChecks` replays the whole queue through the silicon validator at module end. Validation happens *after* the bytes are already written. A wire-illegal instruction does not abort the encoder mid-walk; it makes the *pass* throw at the end.

The `CodeGenMode` tristate is the headline. Each per-op `generate*` helper opens with a four-way branch on a single `Generator` field; the value selects **COLLECT_OPCODES** (a bundle-free opcode census), **RUN_ISA_CHECKS** (build + emit the bundle *and* enqueue it for validation), or **GENERATE_ISACODE** (build + emit, no enqueue). The production `Codegen::codegen` driver only ever drives modes 0 and 1; mode 2 exists in the mechanism but is not selected by this pass.

For reimplementation, the contract is:

- The arch-select: a switch on `Module+0xAC` (ArchLevel) → one of three encoder subclasses, else throw; and the two-pass / one-pass gate that decides how many times the `Module` is walked.
- The `CodeGenMode` enum: its three integer values (fixed by the dispatch, *not* by the error-string order), where the value is stored, and exactly what each mode does and skips.
- The per-engine emit loop: walk order (`main` first, then schedule order inside each BB), how bundles fan out into per-engine streams, and the program-counter bookkeeping.
- `flushISAChecks`: the queue structure, the 10,000-record auto-flush, the TBB parallel replay through vtable slot 0, and the single-`exception_ptr` fail-the-pass semantics.

| | |
|---|---|
| **Pass driver** | `backend::Codegen::codegen(bir::Module&)` @ `0x11d2c50` (5388 B) |
| **Pass wrapper** | `backend::Codegen::run(bir::Module&)` @ `0x11d4160` (scopes `module_name` log attr) |
| **Generator base ctor** | `Generator::Generator(Logger&, Module&, PassOptions const&, CodeGenMode, uint)` @ `0x11f19e0` |
| **Arch discriminator** | `Module+0xAC` ArchLevel: `0x14`→CoreV2, `0x1E`→CoreV3, `0x28`→CoreV4, else throw |
| **CodeGenMode field** | `Generator+0x270` (=+624, dword); arch tag at `+628` |
| **Mode encoding** | `COLLECT_OPCODES=0` · `RUN_ISA_CHECKS=1` · `GENERATE_ISACODE=2` |
| **Emit loop** | `bir::IRVisitor<CoreV2Gen,void>::visit(Module&)` @ `0x11d6dd0` |
| **Per-op dispatch** | `bir::IRVisitor<CoreV2Gen,void>::visit(Instruction&)` @ `0x1178790` (opcode @ `Inst+0x58`) |
| **ISA flush** | `Generator::flushISAChecks()` @ `0x11efb00` (gated `mode != 0`) |
| **Per-engine stream map** | `DenseMap<EngineInfo, FILE*>` @ `Generator+88` (createBin `0x11f3db0`) |
| **Bundle size** | 64 bytes (`0x40`) per instruction |

---

## The Pass — backend::Codegen

### Purpose

`Codegen` is a `BackendPass` registered by name through `GeneratorRegistration` (a `Hashtable<string, function<unique_ptr<BackendPass>(PassOptions const&)>>` at `0x1735740` / `0x1735910`). Its ctor (`0x11d11c0`) demangles its own type name and registers the pass string `neuronxcc::backend::Codegen`. The backend pass driver instantiates and runs it by name on the single merged `Module` the BIR linker produced. `Codegen::run` is a thin wrapper; all work is in `Codegen::codegen`.

### Entry Point

```text
BackendPass::run (vtable+0x28)                          ── pass driver, post-bir_linker
  └─ Codegen::run(Module&)            @0x11d4160         ── scope "module_name" boost.log attr
       └─ Codegen::codegen(Module&)   @0x11d2c50  ⭐      ── the orchestrator (this page)
            ├─ <arch-select> → CoreV{2,3,4}Gen ctor @0x11f19e0
            ├─ IRVisitor<CoreVxGen>::visit(Module&) @0x11d6dd0   ── the emit loop
            │     └─ visit(Instruction&) @0x1178790 → visitInst<Op> → generate*<Op>
            ├─ DescGen(Logger&, Module&)                         ── DMA descriptor stream
            └─ DebugInfoWriter (optional)                        ── protobuf debug levels
```

`Codegen::run` (`0x11d4160`) opens a boost.log scoped attribute `module_name = Module::getName()` so every codegen log line is tagged with the module, calls `Codegen::codegen`, then erases the attribute. The wrapper carries no codegen logic — it is "scope the logger by module name, then run codegen". `Codegen::run` confirmed: `getName` + `get_id_from_string("module_name")` + tail call to `codegen` (decompiled `0x11d4160`).

### Algorithm

`Codegen::codegen` (`0x11d2c50`, 5388 B, 285 basic blocks) opens by refreshing var-ids on the module, starting the `isa_gen` timer, and reading the arch discriminator. The body is one large switch on the ArchLevel; the CoreV3 arm is shown as the representative shape (CoreV2/V4 are identical modulo the subclass and object size).

```c
function Codegen_codegen(this, Module):                  // 0x11d2c50
    AssignVarId(this.logger, Module)                     // refresh BIR var-ids at codegen
    t0 = steady_clock::now()                             // "isa_gen" timer
    arch = *(uint*)(Module + 0xAC)                       // ArchLevel  (0x11d2c91: mov ebp,[rbp+0ACh])
    cfg  = this.passOptions                              // r13 — config object

    switch (arch):                                       // 0x11d2cc1: cmp ebp,14h/1Eh/28h
      case 0x14:  Gen = CoreV2Gen ; objsz = 0x340        // sunda / core_v2  (Trn1·Inf2)
      case 0x1E:  Gen = CoreV3Gen ; objsz = 0x380        // core_v3  (Cayman)
      case 0x28:  Gen = CoreV4Gen ; objsz = 0x380        // core_v4
      default:    throw runtime_error(ArchLevel2string(arch))  // 0x11d2cd6: jnz → __cxa_throw

    // --- the one-pass / two-pass gate (config flag) ---
    if (cfg[0x19C] != 0):                                // 0x11d2cdc: cmp byte [r13+19Ch],0
        gen = tc_new(objsz)
        Gen::ctor(gen, logger, Module, cfg, /*mode=*/1, archTag)   // RUN_ISA_CHECKS
        IRVisitor<Gen>::visit(gen, Module)               // single pass: emit + validate
    else:
        gen0 = tc_new(objsz)
        Gen::ctor(gen0, logger, Module, cfg, /*mode=*/0, archTag)  // COLLECT_OPCODES
        IRVisitor<Gen>::visit(gen0, Module)              // pass 0: opcode census + engine seed
        gen1 = tc_new(objsz)
        Gen::ctor(gen1, logger, Module, cfg, /*mode=*/1, archTag)  // RUN_ISA_CHECKS
        copy_state(gen0 -> gen1)                         // PairHash<Inst,Engine> map @+0x140, …
        IRVisitor<Gen>::visit(gen1, Module)              // pass 1: emit + validate

    deepcopy(gen.engineInstLists -> Codegen.result)      // DenseMap<EngineInfo,vector<Inst*>>
    addMetric("isa_gen", now() - t0)
    DescGen(logger, Module)                              // DMA descriptor stream; "dma_desc_gen"
    if (debug_info_enabled): DebugInfoWriter(...)         // "debug_info_gen"
    teardown()
```

> **CORRECTION (CGD-01) —** an earlier strand summary (D-J29 §2c) labelled the codegen passes "MEASURE pass mode 0 (GENERATE)" then "GENERATE pass mode 1". That conflates the *source name* `GENERATE_ISACODE` with the *integer* the pass passes. The disassembly is unambiguous: the production `Codegen::codegen` emits **only** `mov r8d,1` (mode 1 = RUN_ISA_CHECKS) and `xor r8d,r8d` (mode 0 = COLLECT_OPCODES) at every CoreVxGen ctor site — there is **no** `mov r8d,2` (GENERATE_ISACODE) anywhere in `0x11d2c50`. The "emit" pass in production is RUN_ISA_CHECKS, not GENERATE_ISACODE. See [the mode table](#the-codegenmode-tristate) below. [CONFIRMED — `rg 'r8d, 2'` over the codegen disasm returns nothing; only mode 0/1 sites present.]

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `Codegen::run(Module&)` | `0x11d4160` | Wrapper: scope `module_name` log attr, call codegen | CERTAIN |
| `Codegen::codegen(Module&)` | `0x11d2c50` | Driver: arch-select + pass loop + DescGen/DebugInfo | CERTAIN |
| `Codegen::Codegen(PassOptions&)` | `0x11d11c0` | Ctor; registers pass name `neuronxcc::backend::Codegen` | CERTAIN |
| `Generator::Generator(...)` | `0x11f19e0` | Base of all CoreVxGenImpl; writes the field layout | CERTAIN |
| `GeneratorRegistration::getGenerator(string&)` | `0x1735740` | Name → BackendPass factory lookup | HIGH |
| `CoreV2Gen::CoreV2Gen(...)` | `0x1340870` | Thin subclass ctor (vtable patch over base) | CERTAIN |
| `CoreV3Gen::CoreV3Gen(...)` | `0x1429000` | core_v3 encoder ctor | CERTAIN |
| `CoreV4Gen::CoreV4Gen(...)` | `0x150ea60` | core_v4 encoder ctor | CERTAIN |

---

## Generator and the Arch Select

### Purpose

The arch-select picks the encoder *family* for the target's hardware generation and constructs it on the heap. From that point the driver holds a `CoreV2Gen*` upcast pointer (the most-derived class derives from `CoreV2GenImpl`), and every per-instruction encode is a vtable dispatch — so the driver code is the same regardless of arch. The selection mirrors the same `Module+0xAC` ArchLevel discriminator the arch object model uses (see [Arch Object Model](../arch/arch-object-model.md)); the codes are `generation × 10`.

### Algorithm

The discriminator and the three target classes are read directly from the disassembly:

```c
// 0x11d2c91 .. 0x11d2cd6 (disasm of Codegen::codegen)
mov  ebp, [rbp+0ACh]        // ArchLevel  (Module+0xAC)
cmp  ebp, 14h               // 20 → sunda / core_v2 (Trn1·Inf2)
jz   CoreV2_arm
cmp  ebp, 1Eh               // 30 → core_v3 (Cayman)
jz   CoreV3_arm
cmp  ebp, 28h               // 40 → core_v4
jnz  throw_unknown_arch     // else: runtime_error(ArchLevel2string(arch))
```

> **QUIRK —** there are **two** arch-select sites in libwalrus that read `Module+0xAC` with the same `0x14/0x1E/0x28` codes, and they differ in `CodeGenMode`. The one on this page is `Codegen::codegen` (`0x11d2c50`), the *real emit*, which `tc_new`s a raw heap object and constructs with mode 0/1. The other is `birverifier::InstVisitor::initCodegen` (`0xfc5d00`), reached only from `Verifier::run` (`0xf9f120`); it builds an **embedded** `std::variant<monostate, CoreV2Gen, CoreV3Gen, CoreV4Gen>` (tag stored at `this+1600`) with `CodeGenMode = 2` (GENERATE_ISACODE) and lives in the *Verifier* pass, not the codegen pass. A reimplementer who assumes one arch-select will miss that the verifier runs its own Generator with a different mode. [CONFIRMED — callgraph: only `Verifier::run → 0xfc5d00`; codegen.c uses `tc_new` + raw ctor.]

### The Generator Field Layout

The base `Generator` ctor (`0x11f19e0`) writes the object layout shared by all three subclasses. Only the offsets the driver and the encoders read are listed; this is the *shape*, not a full struct dump.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| vtable | `+0` | ptr | Subclass vtable; **slot 0 = `runSingleISACheck`** | 
| engine inst-lists | `+8` | `DenseMap<EngineInfo, vector<Inst*>>` | Per-engine emitted-instruction lists |
| bundle staging | `+64 / +72 / +80` | `vector<void*>` begin/cur/cap | In-memory 64-byte bundle word buffer (reset per inst) |
| per-engine streams | `+88` (=176?) | `DenseMap<EngineInfo, FILE*>` | The `.bin` output streams (createBin/findBin) |
| "have bundles" flag | `+128` (dword) | flag | Set by RUN_ISA path; read by enqueueISAChecks |
| ISA-check queue | `+264 / +272 / +280` | `vector<ISAMapping>` begin/end/cap | The deferred-validation queue (stride 152) |
| per-opcode stats | `+480` | `map<uint,uint>` | Per-opcode emitted-instruction counts |
| logger | `+536` | `Logger*` | Threaded into every log line |
| PassOptions | `+688` | `PassOptions*` | The config object (arg `a4`) |
| **CodeGenMode** | `+624` (dword) | enum | ⭐ The mode field — gates ISA checks |
| arch tag | `+628` (dword) | uint | core-version / arch tag (ctor arg `a6`) |
| per-inst `.bin` flag | `+697` (byte) | bool | Debug: also write per-instruction `.bin` files |

> **NOTE —** the `DenseMap<EngineInfo, FILE*>` is cited at both `+88` (D-J28) and `+176` (D-J29). The discrepancy is a byte-vs-qword indexing artifact in the two strands (`a1 + 22·q` = `+176`); the field itself is the per-engine stream map, confirmed by the `at()` assertion string in `findBin` naming `DenseMap<bir::EngineInfo, _IO_FILE*>`. Treat the exact numeric offset as MEDIUM; the *role* is CERTAIN. [createBin `0x11f3db0` fopen + the DenseMap assertion at `findBin 0x11f4b90`.]

### The Inheritance Ladder

`CoreV4GenImpl ▸ CoreV3GenImpl ▸ CoreV2GenImpl` is a single-inheritance chain (RTTI `__si_class_type_info`). Per-op `visitInst<Op>` slots resolve most-derived-wins: CoreV2 fills the base set, CoreV3 patches a handful (e.g. its own `generateMatMul` at `0x13643d0`), CoreV4 patches the MX/quant ops. Visible in `codegen.c`: the CoreV3/CoreV4 visit calls are typed `IRVisitor<CoreV3Gen|CoreV4Gen>::visit((CoreV2GenImpl*)gen, …)` — the upcast to the `CoreV2GenImpl` base subobject is direct binary evidence of the chain. The actual per-instruction encoder is always reached through the vtable, so the driver never branches on arch again after construction. [CONFIRMED.]

---

## The CodeGenMode Tristate

### Purpose

`CodeGenMode` (a nested type `Generator::CodeGenMode`, the 4th ctor argument) selects what each per-op `generate*` helper does with an instruction: census its opcode only, build+emit the bundle and queue it for validation, or build+emit without queueing. The value is stored once at `Generator+624` and read by every encoder as `*((uint*)this + 156)` (156 × 4 = 624).

### The Three Values

The values are fixed by the **branch order** in every encoder, not by the human-readable error-string order. This is the single most important fact on the page and the one most likely to be transcribed wrong.

| Value | Name | What it does | Bundle built? | fwrite to engine? | Queued for ISA check? |
|---|---|---|---|---|---|
| **0** | `COLLECT_OPCODES` | Insert the opcode index into `DenseMap<Inst*, set<uint>>` @ `this+32`, call `updateBranchHintTargetPC`, return | no | no | no |
| **1** | `RUN_ISA_CHECKS` | Build the 64-byte bundle, `fwrite` to the engine stream, **emplace into the per-inst validation SmallVector** + set `this+128` | yes | yes | yes |
| **2** | `GENERATE_ISACODE` | Build the 64-byte bundle (run the two-pass weight-tile guards), `fwrite` to the engine stream | yes | yes | no |

> **GOTCHA —** the rodata error string lists the modes as *"one of GENERATE_ISACODE, RUN_ISA_CHECKS, or COLLECT_OPCODES"* — i.e. 2, 1, 0. The **integers are the reverse of the message order.** A reimplementer who assigns ordinals from the string (`GENERATE=0`) inverts the whole scheme: COLLECT would emit and GENERATE would do nothing. The authoritative encoding is the dispatch, transcribed below. [CONFIRMED — `generateLoadWeights` disasm `0x1258548`.]

### Algorithm — the per-op dispatch

The four-way branch lives in the per-op `generate*` bundle builders (not in the thin `visitInst*` orchestrators). `generateLoadWeights` (`0x1258500`) is the cleanest transcription — a switch right at function entry:

```c
// generateLoadWeights @0x1258500 — disasm, verbatim mode dispatch
mov  r14d, [rdi+270h]        // 0x1258524: r14d = this->CodeGenMode  (offset 0x270 = 624)
cmp  r14d, 1                 // 0x1258548: == RUN_ISA_CHECKS ?
jz   RUN_ISA_path            //   → build bundle, emit, enqueue for validation
cmp  r14d, 2                 // 0x1258552: == GENERATE_ISACODE ?
jz   GENERATE_path           //   → build bundle, emit (no enqueue)
test r14d, r14d             // 0x125855c: == 0 (COLLECT_OPCODES) ?
jz   COLLECT_path            //   → record opcode index, return
// 0x1258568: lea rsi, "Wrong CodeGenMode. It must be one of GENERATE_ISACODE, ..."
call bir::reportError(...)   //   default: hard error (defensive; unreachable in normal flow)
```

`generateMatMul` (`0x1248650`) and `CoreV3GenImpl::generateMatMul` (`0x13643d0`) carry the identical head (`mov eax,[rdi+270h] / cmp eax,1 / cmp eax,2`, same error string) — the mechanism is arch-universal, baked into every bundle builder rather than centralised. [CONFIRMED across V2 decompiled + V2/V3 disasm.]

```c
// the three arms, distilled (generateLoadWeights / generateMatMul)
COLLECT_path:                                            // mode 0
    map[I].insert(opcode)        // DenseMap<Inst*,set<uint>> @this+32 (LoadStationary=1, MatMul=2)
    updateBranchHintTargetPC(I)
    return                       // NO bundle, NO setupHeader, NO fwrite, NO validation

RUN_ISA_path:                                            // mode 1
    bundle = validationVec.emplace_back()     // 0x125863a: fresh 64-byte slot in per-inst SmallVector
    zero(bundle); bundle.setupHeader()         // vtable slot → byte[0]=opcode, byte[1]=inst_word_len
    build_fields(bundle)                       // assignAccess<TENSOR3D>, dtype LUT, …
    this[128] = 1                              // "have bundles to snapshot" flag
    goto shared_tail

GENERATE_path:                                           // mode 2
    run_two_pass_weight_tile_guards()          // NeuronAssertion 965/966 — GENERATE only
    bundle = staging_buffer                     // stack buffer, NOT emplaced into validationVec
    build_fields(bundle)
    goto shared_tail

shared_tail:                                             // modes 1 AND 2 converge here
    push(this+64, &bundle)                      // in-memory bundle word buffer
    Bin = findBin(this, I)                       // 0x11f4b90 → per-engine FILE*
    fwrite(bundle, 1, 0x40, Bin)                 // 64-byte bundle → engine .bin stream
    if (this[697]) fwrite(bundle, 1, 0x40, createInstBin(this, I))   // debug per-inst .bin
    stats[opcode]++ ; per_engine_count++
```

> **QUIRK —** RUN_ISA_CHECKS (mode 1) and GENERATE_ISACODE (mode 2) **both** `fwrite` the bundle to the engine stream. The fwrite is *not* mode-gated between them. The only differences are (a) GENERATE runs the two-pass weight-tile guards that RUN_ISA skips, and (b) RUN_ISA `emplace_back`s the bundle into the validation SmallVector so it gets checked later. The actual ISA wire-validation does **not** happen inline in the encoder — it is deferred to `flushISAChecks`. Only COLLECT_OPCODES (mode 0) avoids the bundle and the fwrite entirely. [CONFIRMED — disasm divergence at the three arms; shared tail at `generateMatMul` lines 1184-1206.]

### The One-Pass / Two-Pass Gate

The driver's `cfg[0x19C]` byte decides how many `Module` walks run (see the [driver algorithm](#algorithm)):

- **flag SET** → one walk with mode 1 (RUN_ISA_CHECKS): emit + validate in a single pass.
- **flag CLEAR** → two walks. Pass 0 with mode 0 (COLLECT_OPCODES) seeds the opcode census and the engine-assignment maps; pass 1 with mode 1 (RUN_ISA_CHECKS) does the real emit. Between them, three containers are copied from `gen0` to `gen1` — the `PairHash<Instruction*, EngineType>` map at `+0x140` (via `_M_assign_elements`), plus the small inline vectors at `+0x178/+0x198` — so pass-1 inherits pass-0's engine assignment and branch-target maps without recomputing them.

```c
// CoreV2 arm, two-pass block (disasm 0x11d3398..)
cmp  byte [r13+19Ch], 0      // the gate
jnz  one_pass_mode1          // flag set → single mode-1 pass
gen0 = tc_new(0x340); xor r8d,r8d              // 0x11d33b7: mode 0 = COLLECT_OPCODES
CoreV2Gen::ctor(gen0, …, mode=0, archTag)
IRVisitor<CoreV2Gen>::visit(gen0, Module)       // pass 0
gen1 = tc_new(0x340); mov r8d, 1               // 0x11d33ea: mode 1 = RUN_ISA_CHECKS
CoreV2Gen::ctor(gen1, …, mode=1, archTag)
_M_assign_elements(gen1+0x140, gen0+0x140)      // copy PairHash<Inst,Engine> map
… copy gen0+0x178 / gen0+0x198 …
IRVisitor<CoreV2Gen>::visit(gen1, Module)       // pass 1: emit + validate
```

[CONFIRMED — disasm `0x11d3398`: `xor r8d,r8d` ctor visit, then `mov r8d,1` ctor, `_M_assign_elements`, visit.]

---

## The Per-Engine Emit Loop

### Purpose

A single CRTP walk over the scheduled `Module` drives every encode. Engine separation is **not** done by the walk order — the walk is a flat schedule-order traversal of each basic block. Separation happens downstream: each `visitInst<Op>` writes its bundle into *that instruction's* engine stream, so one interleaved walk naturally fans bundles out into per-engine `.bin` files.

### Algorithm — visit(Module)

`bir::IRVisitor<CoreV2Gen,void>::visit(Module&)` (`0x11d6dd0`) is the walker the driver calls once per Generator pass:

```c
function IRVisitor_visit_Module(this, Module):           // 0x11d6dd0
    CoreV2GenImpl::enterModule(this, Module)             // checkSbIO etc.
    F = Module::getFunctionByName("main")                // "main" emitted FIRST
    if (F):
        enterFunction(this, F)
        for BB in F.basicblocks:                         // per-BasicBlock
            enterBasicBlock(this, BB)
            for I in BB.instructions:                    // per-Instruction, SCHEDULE order
                IRVisitor<CoreV2Gen>::visit(this, I)     // → per-op dispatch + leaveInstruction
        leaveFunction(this, F)
        for f in Module.functions where f != main:       // then the remaining functions
            <same enter / visit / leave>
    else:
        for f in Module.functions: <same>                // no "main" present

    // --- END-OF-WALK GATE ---
    mode = *((uint*)this + 156)                          // = this+624 = CodeGenMode
    if (mode != 0):                                      // 0x11d6dd0 tail: if(result)
        Generator::flushISAChecks(this)                  // run + clear the ISA-check queue
        Generator::assertAsmMappingsCorrect(this)        // sanity: BIR-Inst ↔ asm mapping
        Generator::printInstrStats(this, Module)         // log per-engine instr counts
    return mode
```

> **NOTE —** the order is `Function → BasicBlock → Instruction` in **schedule order** (the linked instruction list inside each BB), with `main` first. It is *not* "emit all PE instructions, then all Activation". The per-engine `.bin` files end up grouped only because `findBin` routes each bundle by the instruction's engine. [CONFIRMED — full `visit(Module)` decompiled.]

### Per-Op Dispatch

`visit(Instruction&)` (`0x1178790`, 1438 B) reads the opcode kind at `Instruction+0x58` and runs a compare-chain: opcodes `0x69/0x6A/0x6C` are basic-block-boundary handling (`enterBasicBlock`); everything else falls to `enterInstruction(this, I)` then the matching `visitInst<Op>`. ~136 distinct `visitInst<Op>` targets are reached (the per-op encoders documented across the encoder pages). The most-derived vtable slot is taken, so CoreV3/V4 overrides win for their patched ops. [CONFIRMED — disasm dispatch chain; 136 `visitInst` symbols counted.]

### Per-Engine Stream Setup

`Generator::initCodegen(Module&)` (`0x11f4ef0`, virtual, no static caller) is the per-engine setup: it calls `bir::listArchEngineInfos(ArchLevel)` to get the live engine set, then for each `EngineInfo` calls `createBin(eng)` and seeds the per-engine program counter. The engine set (internal names, from the engine-type model): `0 Unassigned, 1 Pool, 2 Activation, 3 PE, 4 DMA, 5 DVE, 6 SP, 7 ALL`.

```c
function createBin(this, EngineInfo eng):                // 0x11f3db0
    name = EngineInfo2string(eng) + ".bin"               // e.g. "PE0.bin", "Act0.bin"
    f = fopen(name, "wb")                                // 0x11f3db0 line ~172
    streams[eng] = f                                     // DenseMap<EngineInfo,FILE*>
    emitPreamble(f)                                       // per-engine header
    pc[eng] = 0                                           // seed program counter

function findBin(this, Instruction I):                   // 0x11f4b90
    eng = EngineInfo{ I+0x90, I+0x94 }
    if (eng in {DMA, Unassigned, ALL}):
        reportError("…, but DMA, Unassigned and ALL are not valid TPB engines")
    if (eng not in streams): return createBin(this, eng)  // lazy open
    return streams[eng]
```

`incrementProgramCounter(EngineType)` (`0x11f6d10`) bumps the per-engine PC as bundles append; this cursor is what branch-target resolution (`updateBranchHintTargetPC`) reads. `createInstBin` (`0x11f12b0`) is a *separate* debug path that opens one file per instruction under `Module::getArtifactAbsPath()`, gated by the `+697` flag — distinct from the main per-engine streams. The per-engine `.bin` writer and `setupHeader` are detailed in the sibling `.bin`-emission and setupHeader pages (planned). [CONFIRMED — createBin fopen + EngineInfo2string; findBin engine reject; callgraph: createBin caller = {initCodegen}.]

---

## flushISAChecks — The Deferred ISA-Legality Flush

### Purpose

`flushISAChecks` is the L2 validate-after-emit gate. During the walk, each instruction's bundle is snapshotted into a queue (under RUN_ISA_CHECKS); at module end (or every 10,000 records), `flushISAChecks` replays every queued bundle through the per-arch silicon-legality validator in parallel and rethrows the first failure. This is the codegen pass acting as its own validator: it builds the exact 64-byte wire bundle and feeds it to the same `is_valid_*` cascade the verifier and simulator use, so what is checked is byte-identical to what is emitted. The validator body (`runSingleISACheck`) is a sibling page (planned).

### How bundles reach the queue

```c
function enterInstruction(this, I):                      // 0x1207860
    this[9] = this[8]                                    // reset bundle staging buffer (this+64) to empty

function leaveInstruction(this, I):                      // CoreV2Gen 0x1340890 — thunk →
function enqueueISAChecks(this, I):                      // 0x11f1fe0
    rec = ISAMapping{ I, SmallVector<array<uchar,64>>{} }   // stride 152
    if (this[32] /* have-bundles flag */):
        copy_bundles(&rec.bundles, this+15)              // memmove the staged 64-byte bundles in
    vector<ISAMapping>@(this+264).push_back(rec)         // or _M_realloc_insert on grow
    this[32] = 0
    if ((this[34] - this[33]) > 0x173180):               // 1,520,000 bytes = 10,000 records (×152)
        flushISAChecks(this)                             // ⭐ batch auto-flush every 10k insts
```

The `ISAMapping` record (152 bytes): `+0x00 Instruction*`, `+0x08 SmallVector<array<uchar,64>>` (the bundles), `+0x10 (dword)` bundle count, `+0x14` inline cap. Every instruction that emitted bundles is queued with a *copy* of those exact 64-byte bundles, paired with its `Instruction*`. [CONFIRMED — `enqueueISAChecks 0x11f1fe0`: `if (result > 0x173180) return flushISAChecks`.]

### Algorithm — the TBB parallel replay

```c
function flushISAChecks(this):                           // 0x11efb00
    n = (this[34] - this[33]) / 152                      // element count (152-byte reciprocal-mul)
    tbb::detail::r1::initialize(arena)
    if (n):
        task = tbb::allocate(192)
        task[0]   = off_3D75800        // parallel-for task vtable (body @ sub_11EE460 slot+0x10)
        task[64]  = n                  // blocked_range size
        task[80]  = 1                  // grainsize = 1
        task[88]  = this               // ← the Generator (holds the validator vtable)
        task[112] = &exc_ptr_out       // OUT: aggregated std::exception_ptr (v15)
        task[128] = 2 * max_concurrency()
        tbb::execute_and_wait(task, arena, &simple_partitioner)   // run the dry run
    tbb::destroy(arena)
    if (exc_ptr_out):                  // ANY instruction failed ISA validation:
        exc_ptr_out._M_addref()
        std::rethrow_exception()        // ⇒ propagate the FIRST captured failure → aborts the pass
    // success: free all 152-byte ISAMapping records, reset this[34] = this[33]  (clear queue)
```

The per-range task body (`sub_11EE460`, vtable `off_3D75800+0x10`) recursively bisects the `blocked_range`, then for each `ISAMapping` in its sub-range runs the inner loop:

```c
// sub_11EE460 inner loop (decompiled), per ISAMapping[idx]
gen = task[88]                                           // the Generator
map = gen[264] + 152*idx                                 // ISAMapping[idx]
b   = map[8]                                             // bundle array begin
e   = b + (map[16] << 6)                                 // + count*64 (64-byte stride)
for (; b != e; b += 64):
    (**(void(***)(...))gen)(gen, *(void**)map, b)        // VIRTUAL slot 0 of gen's vtable
                                                         //   = runSingleISACheck(Instruction*, bundle64)
```

> **QUIRK —** the validator is reached by a **virtual call to slot 0** of the Generator's vtable, not a named call. data_tables confirms the `CoreV2Gen` vtable (`0x3d94d90`) slot 0 = `CoreV2GenImpl::runSingleISACheck` (`0x1211cf0`); CoreV3/V4 override slot 0 with their own (`0x134c060` / `0x1435010`). This is how the *per-arch* wire validator is selected — the same arch projection as the encoder family, resolved once at construction. A reimplementer who hard-codes a single validator misses the per-generation override. [CONFIRMED — `b += 64` inner loop + virtual dispatch; data_tables slot 0.]

### Fail-the-pass semantics

A failed ISA check does **not** abort mid-emission. The encoders run to completion for the whole module first (bundles are already `fwrite`-ten to the engine streams). `flushISAChecks` runs *last*, collects errors across all instructions in parallel into a single `std::exception_ptr`, and rethrows the *first* captured one. So the engine `.bin` streams are produced, but the codegen pass as a whole **throws** if any instruction is wire-illegal — the throw escapes `IRVisitor<CoreVxGen>::visit(Module&)` → the `Codegen` BackendPass → a compiler error. Validate-after-emit, fail-the-pass. [CONFIRMED — D-J28 §3.5; `flushISAChecks` rethrow path.]

### The Gate, Restated

| CodeGenMode | flushISAChecks at module end? | Why |
|---|---|---|
| `COLLECT_OPCODES` (0) | **no** | `if (mode != 0)` is false — collect is a bundle-free, validation-free census |
| `RUN_ISA_CHECKS` (1) | **yes** | bundles were enqueued; the whole point is to validate them |
| `GENERATE_ISACODE` (2) | **yes** | `mode != 0` is true; runs the flush even though it did not enqueue |

> **GOTCHA —** the end-of-walk flush is gated `mode != 0`, i.e. it runs for *both* mode 1 and mode 2. But GENERATE_ISACODE (mode 2) does **not** enqueue bundles (it uses a stack staging buffer), so for mode 2 the queue is empty and `flushISAChecks` finds `n == 0` and is a no-op. In the production codegen pass this is moot — `Codegen::codegen` only drives modes 0 and 1, so the flush either skips (mode 0) or has a populated queue (mode 1). Mode 2's empty-queue flush only matters to the Verifier's separate Generator. [CONFIRMED — visit(Module) tail gate; GENERATE arm uses staging buffer, no `emplace_back`.]

---

## The Three-Layer Validation Picture

`flushISAChecks` is the L2 layer of a three-layer legality stack; the codegen pass is L2's own driver.

| Layer | What | Where | Status in 2.24 |
|---|---|---|---|
| L0 | `getValidEngines` | registered but dormant | no-op |
| L1 | structural legality (engine assignment, SbIO, FP8 consistency) | `birverifier::InstVisitor` — a separate pass | active, `NeuronAssertion 400` |
| L2 | silicon wire-legality (`instruction_engine_check` + `is_valid_neuron_instruction`) | `runSingleISACheck`, replayed by `flushISAChecks` | active, the final gate |

The divergence worth flagging for the simulator/verifier strands: the codegen wire model is "one 64-byte bundle per opcode, validated post-emit in parallel"; the sim and structural verifier walk the *structural* BIR. RUN_ISA_CHECKS is the mode that makes codegen run as a wire validator — it still fwrites, but the run exists to surface the silicon `NeuronAssertion`s on the byte-exact emitted bundles. The L2 validator body and its `is_valid_*` cascade are the planned sibling page. [STRONG — cross-checked against the L2 wire-validator strand.]

---

## Residual Uncertainty

- **CodeGenMode ordinals** (COLLECT=0 / RUN_ISA=1 / GENERATE=2): CERTAIN — fixed by the `cmp r14d,1 / cmp r14d,2 / test r14d` branch order in `generateLoadWeights` (`0x1258548`), identical in `generateMatMul` and `CoreV3GenImpl::generateMatMul`. The error-string name order (GENERATE, RUN_ISA, COLLECT) is human-readable, not numeric.
- **Production mode selection**: CERTAIN — `Codegen::codegen` emits only `mov r8d,1` and `xor r8d,r8d`; no `mov r8d,2`. GENERATE_ISACODE(2) is used only by the Verifier's `initCodegen` (`0xfc5d00`).
- **DenseMap<EngineInfo,FILE*> offset** (`+88` vs `+176`): MEDIUM — byte-vs-qword indexing artifact between strands; role CERTAIN via the `findBin` `at()` assertion string.
- **CodeGenMode field offset +624**: CERTAIN — confirmed in the ctor + three encoders (`mov …,[rdi+270h]`).
- **flushISAChecks TBB body / slot-0 dispatch**: CERTAIN — decompiled `0x11efb00` + `sub_11EE460` inner loop + data_tables slot 0.
- **Whether a standalone production pass ever runs RUN_ISA_CHECKS as a separate pre-pass** (vs the two modes `Codegen::codegen` drives): SPECULATIVE — the mode is set by the constructing pass; the *mechanism* is fully confirmed, the *call-site policy* is a pipeline-builder question deferred to that strand.

---

## Related Components

| Name | Relationship |
|---|---|
| BIR linker (merged Module) | Produces the single scheduled, engine-assigned Module `Codegen::codegen` walks |
| `birverifier::InstVisitor::initCodegen` (`0xfc5d00`) | The *Verifier* pass's own Generator (mode 2), a second arch-select site |
| `DescGen` | DMA descriptor stream generation, run after the emit loop inside `Codegen::codegen` |
| `DebugInfoWriter` | Optional protobuf debug-info levels, run after DescGen |
| `NeffPackager` (`writePackageFile` `0x15200e0`) | Consumes the per-engine `.bin` streams + var definitions into the NEFF archive |

The per-engine `.bin` emission / `setupHeader` (8.36/8.37) and the L2 wire validator `runSingleISACheck` (8.41) are sibling pages in this part (planned) — this page is the driver over them.

## Cross-References

- [Instruction Bundle](../isa/instruction-bundle.md) — the 64-byte TPB wire format the `visitInst<Op>` encoders produce and `flushISAChecks` validates
- [Arch Object Model](../arch/arch-object-model.md) — the `Module+0xAC` ArchLevel discriminator (`0x14/0x1E/0x28`) the arch-select reads
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where `WalrusDriver` / the codegen pass sits in the overall job schedule
