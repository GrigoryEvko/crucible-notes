# Per-Engine `.bin` Emission

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The emission path lives in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; `0x5e9020`–`0x62d650` is the `.plt` thunk band, so a `.text` symbol such as `findBin` appears twice — a thunk at `0x5f....` and the real body at `0x11f....`; every address below is the real body). The `bir::EngineInfo`/`EngineType` types come from `libBIR.so`. Other wheels differ — treat every address as version-pinned. See [versions](../reference/versions.md).*

## Abstract

This page documents the **byte-output tail** of the libwalrus encoder: the point where a fully-encoded 64-byte instruction bundle leaves the in-register staging buffer and becomes a physical record in a per-engine `.bin` file. Everything upstream — the `std::array<uchar,64>` layout, the `setupHeader` opcode word, the per-opcode field writes — is owned by [the 64-byte bundle page](../isa/instruction-bundle.md) and the codegen driver (8.35, in-flight). What happens *here* is three things: the **64-byte `fwrite`** that commits a bundle to disk, the **`findBin` router** that picks which engine's file it lands in, and the **`COLLECT_OPCODES` dry pass** that gathers the per-engine opcode coverage set the DVE-table check consumes.

The shape is unusual and worth stating up front: there is **no central emit loop that fans out per engine**. The encoder is template-expanded — the same write block is inlined into ~85 distinct `CoreV{2,3,4}GenImpl::visitInst<Op>` / `generate<Op>` bodies. Each body, after building its bundle, calls `findBin(this, Inst)` keyed on *the instruction's own* `bir::EngineInfo` (read from `Inst+0x90`), gets back a `FILE*`, and `fwrite`s 64 bytes to it. The per-engine separation is therefore a **per-instruction map lookup**, not a driver responsibility. The driver ([IRVisitor `visit(Module&)`](#the-driver--program-order-walk)) is a flat program-order walk; routing is incidental to each write.

The same per-`visitInst` block carries a 4-way `CodeGenMode` switch. `RUN_ISA_CHECKS` (mode 1) is the production write path above — it builds the bundle, `fwrite`s it, and enqueues it for the deferred L2 wire-validation pass. `GENERATE_ISACODE` (mode 2) also builds and `fwrite`s the bundle but does *not* enqueue it (verifier-only path). `COLLECT_OPCODES` (mode 0) skips the bundle entirely and records the instruction's **L2 opcode integer** (the `NEURON_ISA_TPB_OPCODE` enum value) into a per-instruction `std::set<unsigned int>`; that set is what `LowerDVE` reads to size the DVE activation-function table to exactly the opcodes a program references. Any fourth value is a hard error naming all three modes.

> **CORRECTION —** an earlier draft of this page labelled the write+census arm `GENERATE_ISACODE` (mode 1) and the validator-dry-run arm `RUN_ISA_CHECKS` (mode 2) — the mode↔name mapping was **inverted**. The codegen driver page ([8.35 — Codegen Driver and the CodeGenMode Mechanism](codegen-driver.md)) establishes the CONFIRMED ordinals from the branch order in `generateLoadWeights` (`0x1258548`: `cmp r14d,1` → mode 1 = `RUN_ISA_CHECKS`; `cmp r14d,2` → mode 2 = `GENERATE_ISACODE`; `test r14d` → mode 0 = `COLLECT_OPCODES`). The rodata error string lists the *names* human-readable as `GENERATE_ISACODE, RUN_ISA_CHECKS, COLLECT_OPCODES` (i.e. 2, 1, 0) — **not** in numeric order. The production codegen emit pass selects mode 1 (`RUN_ISA_CHECKS`); mode 2 (`GENERATE_ISACODE`) is reached only by the Verifier's own Generator. The mode labels below have been corrected to match 8.35.

For reimplementation, the contract is:

- **The write primitive** — `fwrite(bundle, 1, 0x40, engineFILE)`. Exactly 64 bytes, no length prefix, no record framing; the file is a flat concatenation of 64-byte bundles in program order.
- **The router** — `findBin`: `EngineInfo{EngineType@Inst+0x90, streamId@Inst+0x94}` → `FILE*` via a `DenseMap`, lazily opening `<engine>.bin` on first miss; rejecting DMA/Unassigned/ALL as non-TPB engines.
- **The COLLECT_OPCODES layer** — the per-instruction `set<u32>` of `NEURON_ISA_TPB_OPCODE` values at `Generator+0x20`, and the `LowerDVE` aggregation (`sub_116F3A0` → `checkMissingOpcodes`) that turns it into the per-engine DVE opcode-coverage manifest.

| | |
|---|---|
| **Write primitive** | `fwrite(buf, 1u, 0x40u, FILE*)` — 64 bytes/bundle, no framing |
| **Router** | `Generator::findBin(bir::Instruction&)` @ `0x11f4b90` |
| **File opener** | `Generator::createBin(bir::EngineInfo)` @ `0x11f3db0` (`fopen(path,"wb")`) |
| **Per-inst debug dump** | `Generator::createInstBin(bir::Instruction&)` @ `0x11f12b0` |
| **Driver** | `IRVisitor<CoreV2Gen,void>::visit(bir::Module&)` @ `0x11d6dd0` |
| **Mode selector** | `CodeGenMode` u32 @ `Generator+0x270` — `{0,1,2}` = COLLECT_OPCODES / RUN_ISA_CHECKS / GENERATE_ISACODE (see [8.35](codegen-driver.md)) |
| **Engine→FILE\* map** | `DenseMap<bir::EngineInfo,_IO_FILE*>` @ `Generator+0x58` |
| **COLLECT map** | `DenseMap<bir::Instruction const*, std::set<unsigned int>>` @ `Generator+0x20` |
| **DVE consumer** | `LowerDVE::findCoreDVETable` @ `0x116f8a0` → `sub_116F3A0` → `checkMissingOpcodes` @ `0x116b260` |

---

## The Emit Block

### Purpose

Every `GENERATE_ISACODE`-capable `visitInst<Op>` ends with the same byte-output block. It is **not** factored into a shared helper — the encoder is template-expanded per opcode, so the block is copy-inlined into many distinct decompiled bodies. The exact `fwrite(buf, 1u, 0x40u, FILE*)` form occurs at **86 call sites across 61 distinct decompiled files** (45 `CoreV2GenImpl`, 11 `CoreV3GenImpl`, 5 `CoreV4GenImpl`; 59 of the 61 are `visitInst*`/`generate*` bodies and 58 also call `findBin` — `generateDynamicDMA` and `visitInstDMATrigger` emit *two* writes each, hence 86 > 61). The canonical, smallest exemplar is `CoreV2GenImpl::visitInstHalt` @ `0x122d620`; the block is byte-identical in `visitInstTensorTensor` @ `0x12356d0`, `generateDynamicDMA` @ `0x1276b10`, and every other generate arm.

### Algorithm

```c
function visitInst<Op>(Generator *this, bir::Instruction *inst):   // canonical: visitInstHalt @0x122d620
    mode = *(u32*)(this + 0x270)                  // CodeGenMode @this+156 dwords

    if mode == RUN_ISA_CHECKS:                    // == 1  (production emit path)
        bundle = this->stagingVec.emplace_back()  // SmallVector<array<u8,64>> @this+0x78
        memset(bundle, 0, 64)                      // 4x movups xmm0
        this->vtbl[72](this, bundle, &opcode)      // setupHeader: byte0=opcode, byte1=0x10 — see isa/instruction-bundle
        <per-op field writes>                      // the D-J* encoders (sub_122D280, sub_122CFC0 for Halt)
        this->postProcess(bundle)                  // sub_12095A0 — staging-list bookkeeping @this+0x40

        FILE *bin = findBin(this, inst)            // <<< ENGINE ROUTE  @0x11f4b90
        fwrite(bundle, 1, 0x40, bin)               // <<< THE 64-BYTE WRITE   (visitInstHalt line 46)

        if *(u8*)(this + 0x2B9):                    // dumpBinPerInst flag (captured cl::opt)
            FILE *dbg = createInstBin(this, inst)  // open <eng>.<seq>.<name>.bin  @0x11f12b0
            fwrite(bundle, 1, 0x40, dbg)            // additive 1:1 debug duplicate

        ++ opcodeCountMap[opcode]                   // std::map<u32,u32> @this+0x1E0 (this+480)
        ++ perEngineCount[*(EngineInfo*)(inst+0x90)] // DenseMap<EngineInfo,u32> @this+0x1C8 (this+456)
        perEngineList[*(EngineInfo*)(inst+0x90)].push_back(inst)  // DenseMap<EngineInfo,vector<Inst*>> @this+0x08

    else if mode == GENERATE_ISACODE:             // == 2  (verifier-only path)
        <build same bundle ON STACK, setupHeader, field writes>
        this->vtbl[0](this, inst, bundle)          // L2 wire validator — NO fwrite, NO file

    else if mode != 0:                            // any other value → hard error
        reportError("Wrong CodeGenMode. It must be one of "
                    "GENERATE_ISACODE, RUN_ISA_CHECKS, or COLLECT_OPCODES")

    else:                                          // COLLECT_OPCODES == 0
        collectSet[inst].insert(opcode)            // DenseMap<Inst const*, set<u32>> @this+0x20 (this+32)
        updateBranchHintTargetPC(this, inst)       // branch-target bookkeeping; return
```

The four arms are the literal if-ladder in `visitInstHalt` (lines 24–95): `v3 == 1` RUN_ISA_CHECKS (the write path), `v3 == 2` GENERATE_ISACODE (build-on-stack + `vtbl[0]` validator, no fwrite — verifier-only), `v3` (non-zero) error, `else` (`v3 == 0`) COLLECT_OPCODES. The opcode for Halt is the constant `161` (`0xA1`), visible both as the RUN_ISA `setupHeader` argument (`LOBYTE(v16[0]) = -95` = `0xA1`, line 39) and as the COLLECT integer (`LODWORD(v15[0]) = 161`, line 92) — the same value taken twice, which is the cross-arm invariant (see [the L2 layer](#collect_opcodes--the-l2-coverage-layer)).

> **QUIRK —** the per-engine *separation* is done inside the per-instruction emit block by `findBin`, not by the driver. A reimplementation that fans the driver out into one loop per engine and writes a homogeneous stream will produce the wrong program order: real `.bin` files interleave instructions for one engine in the order the *single* program-order walk visited them, with each write routed by the instruction's pinned `EngineInfo`.

### Considerations

The bundle is built into a `SmallVector<std::array<uchar,64>>` at `this+0x78` (`emplace_back` per instruction). That vector is an in-memory accumulator used both to assemble the bytes before the `fwrite` and to feed post-codegen passes (walked via the staging lists at `this+0x40` / `this+0x08`); the `fwrite` itself goes straight to the `FILE*`, so the on-disk stream and the in-memory vector are populated in lock-step but are independent sinks.

---

## CodeGenMode — the 4-Way Dispatch

### Purpose

`CodeGenMode` is a `u32` stored at `Generator+0x270` (read as `*((u32*)this + 156)`). It is the constructor's 5th argument (the mangled ctor name `...Generator11CodeGenModeEj` fixes the signature `Generator(Logger&, Module&, PassOptions const&, CodeGenMode, uint)`). It selects which of the three sinks every emit block uses.

### Encoding

| Value | Mode | Sink | Bytes? |
|---|---|---|---|
| `0` | `COLLECT_OPCODES` | `set<u32>` insert into `DenseMap<Inst*,set<u32>>` @`this+0x20` | none |
| `1` | `RUN_ISA_CHECKS` | 64-byte bundle → `findBin` → `fwrite(0x40)` (production emit path) | 64/bundle |
| `2` | `GENERATE_ISACODE` | build bundle on stack, run `vtbl[0]` L2 validator (verifier-only; no fwrite in `visitInstHalt`) | none |
| other | — | `reportError(...)` naming all three modes | — |

The numeric ordinals `{0,1,2}` are **CONFIRMED** (per [8.35 — Codegen Driver and the CodeGenMode Mechanism](codegen-driver.md)) from the branch order in `generateLoadWeights` (`0x1258548`: `cmp r14d,1` jz → mode 1 = `RUN_ISA_CHECKS`; `cmp r14d,2` jz → mode 2 = `GENERATE_ISACODE`; `test r14d` jz → mode 0 = `COLLECT_OPCODES`), reproduced in this page's own `visitInstHalt` (`0x122d620`) if-ladder. They are **not** pinned to a `libBIR` enum table, and the `reportError` string's enumeration `GENERATE_ISACODE, RUN_ISA_CHECKS, COLLECT_OPCODES` is human-readable (2, 1, 0) — **not** the numeric order. The production codegen emit pass selects mode 1; mode 2 is reached only by the Verifier's own Generator (see 8.35). The mode also gates the [driver tail](#the-driver--program-order-walk): when `mode != 0` the driver runs `flushISAChecks` + `assertAsmMappingsCorrect` + `printInstrStats` after the walk.

---

## `findBin` — The Engine Router

### Purpose

`findBin` @ `0x11f4b90` maps an instruction to the `FILE*` of its engine's `.bin`. It is the only routing logic in the emission path; called once per RUN_ISA_CHECKS (mode 1) write, it reads the instruction's pinned engine, validates it is a real TPB compute engine, then probes (or lazily populates) the engine→`FILE*` map.

### Entry Point

```text
visitInst<Op> (RUN_ISA_CHECKS / mode-1 write arm)
  └─ Generator::findBin(this, inst)         @0x11f4b90
       ├─ bir::EngineType2string(engine)    ── for the rejection diagnostic
       ├─ [non-TPB engine] bir::reportError("…not valid TPB engines")
       └─ [miss / empty map] Generator::createBin(EngineInfo)   @0x11f3db0  (lazy open)
```

### Algorithm

```c
function findBin(Generator *this, bir::Instruction *inst) -> FILE*:   // @0x11f4b90
    EngineType engine = *(u32*)(inst + 0x90)     // *((u32*)inst + 36) — line 138
    uint       stream = *(u32*)(inst + 0x94)     // *((u32*)inst + 37) — line 139
    // EngineInfo = the 8-byte key {EngineType@+0, uint streamId@+4}

    // VALIDATION: only the five TPB compute engines have a .bin
    name = bir::EngineType2string(engine)
    if engine not in {PE, Pool, Activation, DVE, SP}:
        reportError(name + ", but DMA, Unassigned and ALL are not valid TPB engines")  // line 158

    map = this->engineFileMap            // DenseMap<EngineInfo,_IO_FILE*> @this+0x58 (this+11 qwords)
    nb  = *(u32*)(this + 0x68)           // NumBuckets (this+26 dwords) — line 181
    if nb == 0:
        return createBin(this, {engine, stream})        // empty map → lazily open (line 183)

    h = llvm::hash_combine<EngineType,uint>(engine, stream)
    for (i = (nb-1) & h; ; i = (nb-1) & probe):          // linear-probe DenseMap
        bucket = map.buckets + 16*i
        if bucket.key.engine == engine && bucket.key.stream == stream:
            return bucket.value                          // HIT — the engine's open FILE* (line 433)
        if bucket.key == EMPTY{0, -1}:                   // empty bucket key {0, 0xFFFFFFFF}
            return createBin(this, {engine, stream})     // MISS → lazily open (line 303)
        probe = ...                                       // quadratic-ish reprobe
```

The map's value type is proved by the embedded `DenseMap::at` assert string that names `DenseMap<bir::EngineInfo, _IO_FILE*, bir::EngineInfoDenseMapInfo>` verbatim (lines 426–432). The empty-bucket sentinel key is `{0, -1}` (engine `0` = Unassigned, stream `0xFFFFFFFF`), tested at line 302.

> **GOTCHA —** the IDA listing hoists the entire `EngineType2string` + `reportError` string-construction sequence to the *top* of the function (lines 137–179), making it look unconditional. It is not: that block is the **error path** for an invalid engine. The real control flow validates the engine, and only on a non-TPB engine does `reportError` fire. The lookup loop (lines 288–433) is the common path.

### Considerations

The valid engine set is the five TPB compute engines `{PE, Pool, Activation, DVE, SP}` — the full `bir::EngineType` enum minus `DMA`, `Unassigned`, and `ALL`. The rejection string is the headline anchor: there is **no DMA `.bin`**. DMA descriptor and trigger bundles ride the issuing compute engine's stream instead (see [the DMA reconciliation](#dma--no-separate-bin)).

---

## `createBin` — Lazy Per-Engine File Open

### Purpose

`createBin` @ `0x11f3db0` is `findBin`'s miss handler: it opens exactly one `.bin` per `EngineInfo`, inserts it into the engine→`FILE*` map, and registers the file (plus a parallel `.json` sidecar) with the module artifact manifest. Called lazily on the first instruction of each engine, so a function that touches only Pool and Act produces exactly two `.bin` files.

### Algorithm

```c
function createBin(Generator *this, bir::EngineInfo eng) -> FILE*:   // @0x11f3db0
    name = bir::EngineInfo2string(eng) + ".bin"          // "PE.bin", "Pool.bin", … (line 108-111)
    dir  = bir::Module::getArtifactAbsPath()             // nc<core>/sg<subgraph> artifact dir (line 130)
    path = dir + "/" + name

    FILE *bin = fopen(path, "wb")                         // line 172
    if bin == NULL:
        // NeuronAssertion, errcode 1007, generator.cpp:58
        throw NeuronAssertion("bin != __null", errno, strerror)   // lines 175-326

    this->engineFileMap[eng]   = bin                     // DenseMap<EngineInfo,_IO_FILE*> @this+0x58 (a1+22) — line 392
    this->perEngineOffset[eng] = 0                        // DenseMap<EngineInfo,u32>       @this+0x1C8 (a1+114) — line 436
    this->vtbl[24](this, bin, eng)                        // per-engine virtual init hook (line 439)

    ModuleArtifactInfo::addEngInstrFile(eng, name)        // register "<engine>.bin"        — line 442
    ModuleArtifactInfo::addEngDMADescFile(eng,
        bir::EngineInfo2string(eng) + ".json")            // register parallel "<engine>.json" — line 463
    return bin
```

### Considerations

Two artifacts are registered **per engine**: the binary instruction stream `<engine>.bin` (`addEngInstrFile`, the file every `fwrite` appends to) and a JSON sidecar `<engine>.json` (`addEngDMADescFile`, a path placeholder the NEFF packager later fills with DMA-queue / table descriptors — NEFF assembly is 12.4, planned). The `bin != __null` assert is anchored to source path `…/neuronxcc/walrus/codegen/src/generator.cpp:58` (line 175) with `ErrorCode` `1007` (`v88 = 1007`, line 249), formatted through `fmt::v10::vformat` with `errno`/`strerror` and the `lookup_cause`/`lookup_resolution` diagnostic text.

The parallel offset map at `this+0x1C8` is zero-initialised here and bumped in each emit block (the same `DenseMap<EngineInfo,u32>` the census increments); it tracks the per-engine instruction count / running record index, consumed by `printInstrStats`.

> **QUIRK —** the JSON sidecar is registered by a method named `addEngDMADescFile`, but it is the generic per-engine table sidecar (DMA queues for compute engines that issue DMA, Act/DVE tables otherwise), not DMA-specific. The name reflects its first use; the registration is unconditional for every engine `.bin`.

---

## The Per-Instruction Debug Dump

### Purpose

When the `dumpBinPerInst` flag is set, the emit block writes each bundle a **second** time to a freshly-opened per-instruction file, giving a 1:1 BIR-instruction ↔ 64-ISA-bytes mapping for diffing. This is a debug side channel — additive, not a replacement; the engine `.bin` is still written.

### Algorithm

```c
function createInstBin(Generator *this, bir::Instruction *inst) -> FILE*:   // @0x11f12b0
    seq = *(u32*)(this + 0x3C)                  // *((u32*)this+15) — per-codegen counter
    *(u32*)(this + 0x3C) = seq + 1              // POST-increment (monotonic across the whole walk) — lines 52-53
    seqStr = format_integer(seq)                 // 24-digit zero-pad (width 24, fill '0') @0x11f0e90

    name = EngineInfo2string(inst.engineInfo)   // engine prefix
         + "." + seqStr                          // 24-digit zero-padded seq
         + "." + inst.name                        // *((QWORD*)inst + 3) = inst+0x18 — line 169
         + ".bin"                                 // line 186
    path = bir::Module::getArtifactAbsPath() + "/" + name   // line 214-232
    return fopen(path, "wb")                      // line 251 — raw return, no null assert
    // e.g.  Pool.000000000000000000000042.matmul_3.bin
```

### Related Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `dumpBinPerInst` | BOOL | **false** | "Dump a .bin file per BIR instruction, for matching BIR instruction to generated ISA." |

The flag is an `llvm::cl::opt<bool>` (object @ `0x3e03f00`, arg-string `"dumpBinPerInst"` @ `0x29059a`, description @ `0x1d63a40`), registered by the static initialiser `sub_7DC0D0` @ `0x7dc0d0`. The constructor captures its value into a per-instance byte at `Generator+0x2B9`: `LOBYTE(result) = qword_3E04000[7]` (enableLDWOpt → `+0x2B8`), `HIBYTE(result) = qword_3E03F40[7]` (dumpBinPerInst → `+0x2B9`), `*(u16*)(this+696) = result`. So `*(u8*)(this+0x2B9)` is the byte the emit block tests.

### Considerations

`createInstBin` does **not** register its file in `ModuleArtifactInfo` and does **not** track an offset — it is a side-channel dump, not part of the NEFF. It is also **not** closed by the destructor (which only closes the engine-bin map); the per-inst `FILE*` leaks unless a higher layer closes it. Acceptable for a debug-only path that is off by default; a reimplementation should `fclose` it explicitly.

---

## `COLLECT_OPCODES` — the L2 Coverage Layer

### Purpose

`COLLECT_OPCODES` (mode 0) is the dry pass. It emits no bytes and runs no validator: it computes its **opcode integer exactly as the emit (mode-1 RUN_ISA) arm does**, then inserts it into a per-instruction `std::set<unsigned int>`. That integer is the **`NEURON_ISA_TPB_OPCODE`** enum value (the "L2" identifier) — the same value the emit arm hands to `setupHeader` as the low opcode byte, and the same value the L2 wire validator bit-tests against a per-engine legality bitmap. The collected sets become the input to the DVE-table coverage check.

### The three id layers

```text
L1  bir::InstructionType ordinal   (e.g. QuantizeMx = 96)        ── the BIR op class
L2  NEURON_ISA_TPB_OPCODE enum     (QuantizeMX = 227 = 0xE3)     ── what COLLECT inserts;
                                                                     == setupHeader's low byte
                                                                     == opcode-count-map key
L3  16-bit wire header word         (0x10 << 8 | L2 = 0x10E3)    ── what setupHeader stamps at bundle[0:2]
```

`L2 ≡ low byte of L3`, and `L2 ≠ L1` (separate enums; the encoder is the non-injective projection `L1 → {L2}`). The relation is **CONFIRMED** by transcription: in `visitInstQuantizeMx` @ `0x143dc60` the COLLECT arm stores `v46.m128i_i32[0] = 227` before `_M_insert_unique` (line 78) while the emit (mode-1 RUN_ISA) arm stamps `*(WORD*)bundle = 4323` = `0x10E3` (line 160) — `227 == 0xE3 == low byte of 0x10E3`. In `visitInstTensorTensor` @ `0x12356d0` the same value is carried twice: `v5 = isBitVec ? 81 : 65` (the COLLECT int, line 58) and `v8 = (-(isBitVec==0) & 0xF0) + 81` (the emit-arm byte, line 61), where `(u8)v8 == v5`.

### Algorithm

```c
// COLLECT arm of every visitInst<Op> (mode == 0); canonical: visitInstTensorTensor @0x12356d0
function collect(Generator *this, bir::Instruction *inst):
    opcode = <computed exactly as in the emit (mode-1 RUN_ISA) arm>   // the L2 int (e.g. 65/81, 227)
    set<u32> &s = collectMap[inst]                              // DenseMap<Inst const*, set<u32>> @this+0x20
    s.insert(opcode)                                            // _Rb_tree::_M_insert_unique<unsigned int>
    updateBranchHintTargetPC(this, inst)
```

The map value is a `std::set<unsigned int>` per instruction, so one instruction may record **more than one** opcode — e.g. a `Matmul` lowering pushes both `Ldweights` (1) and `Matmul` (2) into its set, and `MatmultMx` pushes `LdweightsMX` (9) and `MatmulMX` (10).

### The DVE consumer

```text
LowerDVE::run(bir::Module&)                          @0x1170b10
  └─ LowerDVE::findCoreDVETable(Generator&, Module&)  @0x116f8a0
       │  v19 = empty DenseMap<EngineInfo, DenseSet<u32>>
       ├─ for each BasicBlock / Instruction:  sub_116F3A0(inst, &v19, Generator)   @0x116f3a0
       └─ LowerDVE::checkMissingOpcodes(v19)           @0x116b260   (lower_dve.cpp)
```

`sub_116F3A0` @ `0x116f3a0` is the aggregator (per-instruction set → per-engine set). It walks the basic-block instruction ilist, recurses into `InstLoop` bodies (`*(inst+80) == 105`, line 59), skips sync ops (`getInstSyncType(inst) != 0`, line 89) and non-ISA instructions (`*(inst+136) != 5`, line 86), then reads the **same** COLLECT map via `DenseMap::at` at `Generator+0x20` (`a3+32`, lines 91–92). The `at` assert string names `DenseMap<const bir::Instruction*, std::set<unsigned int>>` verbatim (lines 127–133) — proving it is the identical map the COLLECT arm populated. It takes the instruction's `EngineInfo` (`*(inst+136)`, line 139) and unions every opcode in the instruction's set into the per-engine `DenseSet<unsigned int>` `v19[EngineInfo]`. The result is "which `NEURON_ISA_TPB_OPCODE`s actually appear on each engine."

`checkMissingOpcodes` @ `0x116b260` then compares the observed per-engine opcode set against the DVE engine's table capacity and `reportError`s on a coverage violation. `COLLECT_OPCODES` is thus the analysis that tells `LowerDVE` which DVE micro-ops the lowered program references, so the DVE config / activation-function table can be sized to cover exactly the opcodes in use — the per-NEFF DVE opcode manifest.

> **NOTE —** the L2 opcode integer is the *cross-arm key*: it is the value inserted into the COLLECT set (`this+0x20`), the key into the opcode-count histogram `std::map<u32,u32>` (`this+0x1E0`), and the byte stamped into the wire header. Three Generator structures keyed by one integer. A reimplementation that derives the opcode independently per arm risks the arms diverging; derive it once.

---

## DMA — No Separate `.bin`

`findBin` rejects DMA as a non-TPB engine, so there is **no dedicated DMA `.bin` stream**. DMA descriptor and trigger bundles ride the `.bin` of the compute engine that *issues* them:

- `generateDynamicDMA` @ `0x1276b10` (the `PseudoDmaDirect2d` / opcode `0xD4` descriptor emitter) writes its 64-byte descriptor through the **same** `findBin` + `fwrite(0x40)` path, keyed on the issuing instruction's `EngineInfo` (`inst+0x90` — typically Pool/Act/SP).
- `visitInstDMATrigger` @ `0x1229710` (opcode `0xC1`) likewise emits its 64-byte trigger bundle via `findBin` + `fwrite` to its engine's `.bin`.

The DMA-queue metadata that pairs trigger ↔ descriptor is carried in the per-engine `<engine>.json` sidecar (`addEngDMADescFile`), assembled into first-class NEFF DMA-queue definitions at package time. The separation of the "DMA queue stream" is therefore a **NEFF-JSON-layer** concern, not a codegen `.bin` concern: at the `.bin` layer DMA descriptors are interleaved into the issuing compute engine's stream.

---

## File Lifecycle

`.bin` files open **lazily** (first instruction of each engine: `findBin` miss → `createBin` → `fopen "wb"`) and close at Generator teardown. The destructor `~Generator` @ `0x11ef0b0` iterates the `DenseMap<EngineInfo,_IO_FILE*>` at `this+0x58` (bucket array @ `this+11` qwords, count @ `this+26` dwords), skips empty/tombstone buckets via `AdvancePastEmptyBuckets`, and `fclose`s every `FILE*` (`v46 = *(FILE**)(v44+8); fclose(v46)`, lines 79–81). This is the **only** place the engine `.bin`s are flushed and closed; the `AdvancePastEmptyBuckets` assert names the map type `DenseMap<bir::EngineInfo, _IO_FILE*>` (lines 61–66).

> **GOTCHA —** the destructor closes only the engine-file map. The per-instruction debug files opened by `createInstBin` are never closed here — they are not in this map. A faithful reimplementation must close them at the dump site (or accept the leak, as the binary does, since the path is debug-only and default-off).

---

## The Driver — Program-Order Walk

The codegen driver is `IRVisitor<CoreV2Gen,void>::visit(bir::Module&)` @ `0x11d6dd0` (8.35 covers it in full; in-flight, mentioned without link). It is **not** a per-engine loop — it is a single program-order IR walk; per-engine separation happens per-instruction inside the emit block, as above. Structure: `enterModule`; then `main`-first function ordering (walk `main`'s BBs+instructions, then every other per-core `call_def_<coreId>_instance` function the BIR linker merged in); per BB it dispatches each instruction through `IRVisitor::visit(Inst)` @ `0x1178790` into the `visitInst<Op>` emit block. After the walk, **if `CodeGenMode != 0`**, it runs `flushISAChecks` (drain the L2 validation queue), `assertAsmMappingsCorrect` (verify the BIR↔ASM debug mapping), and `printInstrStats` (emit the per-engine instruction-count census from the `DenseMap<EngineInfo,u32>` at `this+0x1C8`).

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `Generator::findBin(bir::Instruction&)` | `0x11f4b90` | `EngineInfo` → `FILE*` router; miss → `createBin` | CERTAIN |
| `Generator::createBin(bir::EngineInfo)` | `0x11f3db0` | opens one `<engine>.bin` per engine; `fopen "wb"` | CERTAIN |
| `Generator::createInstBin(bir::Instruction&)` | `0x11f12b0` | per-inst debug `<eng>.<seq24>.<name>.bin` | CERTAIN |
| `Generator::format_integer(int)` | `0x11f0e90` | 24-digit zero-pad seq formatter | HIGH |
| `Generator::~Generator()` | `0x11ef0b0` | `fclose` loop over the engine `FILE*` map | CERTAIN |
| `Generator::Generator(...)` | `0x11f19e0` | member-layout + cl::opt capture (`+0x2B8`/`+0x2B9`) | CERTAIN |
| `Generator::printInstrStats(Module&)` | `0x11f2320` | per-engine instruction census dump | HIGH |
| `IRVisitor<CoreV2Gen>::visit(Module&)` | `0x11d6dd0` | the codegen driver / program-order walk | CERTAIN |
| `CoreV2GenImpl::visitInstHalt` | `0x122d620` | canonical emit-block exemplar (opcode `0xA1`) | CERTAIN |
| `CoreV2GenImpl::visitInstTensorTensor` | `0x12356d0` | canonical COLLECT/GENERATE pair (L2 65/81) | CERTAIN |
| `generateDynamicDMA` | `0x1276b10` | DMA descriptor via shared `findBin`+`fwrite` | HIGH |
| `LowerDVE::findCoreDVETable` | `0x116f8a0` | drives the COLLECT-set aggregation | CERTAIN |
| `sub_116F3A0` | `0x116f3a0` | per-instruction set → per-engine `DenseSet<u32>` | CERTAIN |
| `LowerDVE::checkMissingOpcodes` | `0x116b260` | DVE-table opcode-coverage check | HIGH |
| `ModuleArtifactInfo::addEngInstrFile` | `0x60bcc0` | registers the `<engine>.bin` path | HIGH |
| `ModuleArtifactInfo::addEngDMADescFile` | `0x626070` | registers the parallel `<engine>.json` | HIGH |

## Generator Object — Member Offsets

| Field | Offset | Type | Role |
|---|---|---|---|
| per-engine inst list | `+0x08` | `DenseMap<EngineInfo, vector<Inst*>>` | census list |
| COLLECT set map | `+0x20` | `DenseMap<Inst const*, set<u32>>` | `COLLECT_OPCODES` opcode sets |
| inst seq counter | `+0x3C` | `u32` | `createInstBin` seq (post-inc) |
| engine `FILE*` map | `+0x58` | `DenseMap<EngineInfo, _IO_FILE*>` | the per-engine `.bin` map |
| map NumBuckets | `+0x68` | `u32` | `findBin` probe bound |
| bundle accumulator | `+0x78` | `SmallVector<array<u8,64>>` | in-mem staging |
| per-engine offset/count | `+0x1C8` | `DenseMap<EngineInfo, u32>` | record index / census |
| opcode-count histogram | `+0x1E0` | `std::map<u32,u32>` | per-opcode emission tally |
| `CodeGenMode` | `+0x270` | `u32` | `{0,1,2}` mode selector |
| `dumpBinPerInst` | `+0x2B9` | `u8` | captured cl::opt gate |

## Adversarial Self-Verification

Five strongest claims, re-checked against the binary:

1. **`fwrite(buf, 1u, 0x40u, FILE*)` is the write primitive** — CONFIRMED. The literal `0x40` appears in `visitInstHalt` line 46, `visitInstTensorTensor` line 229, `visitInstQuantizeMx` line 211, and the triplet (findBin → fwrite → optional createInstBin+fwrite) is byte-identical across them.
2. **`findBin` reads engine from `Inst+0x90`, stream from `Inst+0x94`, rejects non-TPB engines, returns `_IO_FILE*`** — CONFIRMED. Lines 138/139 read `*((u32*)a2+36)`/`+37`; the rejection string is line 158; the value type is named in the `at` assert (lines 426–432); the FILE* return is line 433.
3. **`createBin` does `fopen "wb"`, `bin != __null` / `generator.cpp:58` / errcode 1007, and registers `.bin` + `.json`** — CONFIRMED. `fopen` line 172, assert line 175 + errcode `1007` line 249, `addEngInstrFile` line 442, `.json` append line 448 + `addEngDMADescFile` line 463.
4. **L2 = low byte of L3** — CONFIRMED for QuantizeMx (`227` collect int line 78, `4323` = `0x10E3` word line 160) and TensorTensor (`65/81` collect / `0xF0+81` byte).
5. **The DVE consumer reads the same `Generator+0x20` map** — CONFIRMED. `sub_116F3A0` reads `a3+32` (lines 91–92) and its `at` assert names `DenseMap<const bir::Instruction*, std::set<unsigned int>>` (lines 127–133), the identical type the COLLECT arm writes.

Re-verify ceiling — what is **not** pinned: the `CodeGenMode` ordinals `{0,1,2}` are **CONFIRMED** from the branch order (this page's `visitInstHalt` + 8.35's `generateLoadWeights` `0x1258548`), but are not pinned to a `libBIR` enum *table* (no named enum constant in rodata; the names come only from the error string). The `EngineInfo2string` output for multi-stream engines (`"<engine>"` vs `"<engine>.<id>"`) is INFERRED; `streamId` is non-zero only for split queues. The `vtbl+0x18` per-engine init hook in `createBin` (line 439) is identified by slot but its body is not transcribed. The exact NEFF archive member naming for each `<engine>.bin` is deferred to NEFF packaging (12.4, planned). The `86 fwrite / 61 file` count is the exact decompiled-corpus figure for this wheel; it counts the literal `1u, 0x40u` `fwrite` form and may miss any write hidden behind an un-inlined helper, of which none were found.

## Cross-References

- [The 64-Byte Instruction Bundle](../isa/instruction-bundle.md) — the `std::array<uchar,64>` layout and `setupHeader` opcode word written here
- [DVE Opcode Table](../isa/dve-opcode-table.md) — the `NEURON_ISA_TPB_OPCODE` (L2) enum the COLLECT pass gathers per engine
- Codegen driver (8.35) — `IRVisitor<CoreV2Gen>::visit(Module&)`, the program-order walk that calls this emit path (in-flight)
- Opcode master (8.37) — `setupHeader` and the opcode-word table this path emits (in-flight)
- NEFF `.bin` section members (12.4) — what the per-engine `.bin`/`.json` streams become in the NEFF archive (planned)
