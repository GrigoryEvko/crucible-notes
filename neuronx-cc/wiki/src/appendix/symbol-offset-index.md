# Symbol & Offset Index

> *Every address on this page is a virtual address (VMA) for `neuronx_cc` 2.24.5133.0+58f8de22, recovered from the **cp310** wheel. cp311/cp312 carry byte-near-identical twins of every binary, but renumber addresses — treat every value as version-pinned. The per-binary VA frame is **not** uniform: in the `.so` libraries (`libBIR`, `libwalrus`, …) `.text`/`.rodata` are loaded at VA == file-offset, so `nm`/`objdump` VAs index the file directly; in the standalone `hlo-opt`/`hlo2penguin` ELFs the section base is shifted (`.text` file-off = VA − `0x201000`, `.rodata` file-off = VA − `0x200000`). See [§0.5](#05-the-two-va-framing-conventions). Sources: the cross-binary type-identity matrix (RTTI typeinfo + RELA + `DT_NEEDED`), the per-binary `nm -DC` dynamic-symbol tables, and the IDA `rtti.json` typeinfo sets.*

## Abstract

This appendix is the **reverse lookup** for the whole wiki: given a symbol name or a hex address cited anywhere in the book, it names the binary that owns the symbol and the page that documents it. The normative pages anchor every claim to a `sub_ADDR` / `0xNNNN`; this index lets a reader go the other way — from an address found in `nm`, `objdump`, or a crash backtrace to the page that explains what that code does. It is **curated, not exhaustive**: it carries the few hundred symbols a reader will actually look up (the dispatch cores, the per-engine encoders, the allocator drivers, the typeinfo anchors), not every one of the 59,466 functions in `libwalrus` or the 44,794 typeinfos in `hlo2penguin`.

The index is organized **by binary**, because which artifact a symbol lives in is the first disambiguator — the backend is split across eight `.so` plus two standalone ELFs and a galaxy of Cython modules, and the same short name (`run`, `visit*`, `schedule`) recurs in several of them. [§0.1](#01-the-binary-roster) gives the binary roster and its ownership boundaries; [§0.5](#05-the-two-va-framing-conventions) gives the two VA-framing conventions and the two-VA-frame sidecar artifact that must be understood before any address on this page is unambiguous. [§1](#1-libbirso--the-bir-core)–[§8](#8-the-cython-codegen-modules) are the per-binary symbol tables. [§9](#9-cross-binary-classsymbol-matrix) reproduces the cross-binary class/namespace matrix — who *defines* each shared type and how it crosses binary boundaries.

This is a reference catalog; its value is being a faithful, navigable index. Every row points at a page that **actually exists** in [SUMMARY.md](../SUMMARY.md). Where a symbol's primary page has not been written yet, the row says so rather than inventing a target. [§10](#10-symbol-table-spot-checks--five-highest-traffic-symbols) re-reads the five highest-traffic symbols straight out of the binaries' dynamic symbol tables.

| | |
|---|---|
| **Target** | `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel) |
| **Binaries indexed** | `libBIR.so`, `libwalrus.so`, `libBIRSimulator.so` + the `hlo-opt` / `hlo2penguin` ELFs + the NKI/Penguin Cython `.so` |
| **Address frame** | `.so`: VA == file-offset · `hlo-opt`/`hlo2penguin`: `.text` − `0x201000`, `.rodata` − `0x200000` |
| **Owner map source** | the cross-binary type-identity matrix (RTTI typeinfo + RELA + DT_NEEDED) |
| **Symbol source** | per-binary `nm -DC` dynamic-symbol tables; IDA `rtti.json` typeinfo sets |
| **Coverage** | curated — the *notable, cited* symbols, not an undifferentiated symbol dump |

---

## 0.1. The binary roster

The compiler's logic is split across the artifacts below. "Owns" means the canonical *definer* of a shared C++ namespace (`OWN`), not merely a binary that references it. Sizes and paths are catalogued on [0.4 Binary Inventory & the .so Map](../reference/binary-inventory.md); this table is the per-binary index header.

| Binary | Path under `neuronxcc/` | Owns | Indexed in |
|---|---|---|---|
| `libBIR.so` | `starfish/lib/` | `bir` (371 types) + `pelican` (29) — the leaf IR-core owner | [§1](#1-libbirso--the-bir-core) |
| `libwalrus.so` | `starfish/lib/` | the backend; `klr` (199), `dap` (201), `birsim::debugger` (39), `neuroncc::debug_info` (producer) | [§2](#2-libwalrusso--the-backend) |
| `libBIRSimulator.so` | `starfish/lib/` | `birsim::engine` (10: Memory/SyncState/TpbEngines/…) | [§3](#3-libbirsimulatorso--the-functional-simulator) |
| `hlo-opt` | `starfish/bin/` | the HLO/MHLO/StableHLO optimizer + the `--passes` registry | [§4](#4-hlo-opt--the-hlo-optimizer) |
| `hlo2penguin` | `starfish/bin/` | the MLIR front-half; `hilo` Neuron device-model (151), `xla::hilo` (84) | [§5](#5-hlo2penguin--the-mlir-front-half) |
| `libpwp_sim.so` | `starfish/lib/` | `PWPSim` (4 types) — isolated analog/power sim | [§6](#6-libpwp_simso--the-pwp-simulator) |
| `libBIRParserDumper.so` | `starfish/lib/` | `parserdumper` / `bir_roundtrip` (BIR-JSON round-trip) | [§7](#7-libbirparserdumperso--the-bir-json-round-trip) |
| `KernelBuilder.so` / `BirCodeGenLoop.so` / `NkiCodegen.so` (Cython) | `nki/…` / `starfish/penguin/targets/codegen/` | the NKI forward builder, the beta3 Penguin→BIR driver, the re-emit printer | [§8](#8-the-cython-codegen-modules) |

> **NOTE —** `libBIR.so` is the **leaf / owner**: its `DT_NEEDED` lists only system/util libraries, and it exports `bir`/`pelican` typeinfos as weak (`V`) symbols. Every other backend binary reaches `bir`/`pelican` by **dynamic link** to `libBIR` (DT_NEEDED + weak-symbol coalescing), not by carrying a static copy — so a `bir::*` typeinfo address in those binaries' `rtti.json` resolves to `libBIR`'s definition at load. When this index lists a `bir`/`pelican` address it is `libBIR`'s, the definitional one.

---

## 0.5. The two VA-framing conventions

Two distinct conventions decide whether an `nm`/`objdump` VA can be used as a file offset, and a third artifact (the two-VA-frame sidecar) can make one function appear at two addresses. Resolve all three before reading any address below as authoritative.

```text
(A) the .so libraries  (libBIR, libwalrus, libBIRSimulator, …)
      .text VA == file offset      .rodata VA == file offset
      ⇒ an nm/objdump VA indexes the file directly; jump tables read by VA.
      e.g. setupHeader @ 0x143f440 IS at file offset 0x143f440 in libwalrus.

(B) the standalone ELFs  (hlo-opt, hlo2penguin)
      .text  file_off = VA − 0x201000
      .rodata file_off = VA − 0x200000
      ⇒ a .text VA must have 0x201000 subtracted before xxd/objdump --start-offset.
      e.g. NextChannelId @ 0x8ab1ac0 (VA) is at file_off 0x8aaa0c0 (.text) in hlo-opt.

(C) the two-VA-frame sidecar artifact  (any binary with per-symbol decompile)
      The ELF symbol-table VA (cited HERE) is the authoritative frame.
      The per-symbol IDA/Hex-Rays sidecar may carry a SECOND internal frame
      (a thunk wrapper) — e.g. is_valid_s3_lw @ 0x14ac170 (symtab) vs 0x5fbd40
      (sidecar thunk). BOTH denote the same function; a sidecar's 0x5fbd40 is
      NOT a wrong address. The .plt thunk band (libwalrus 0x5e9020–0x62d650) is
      this same effect: each thunk tail-jumps to a real body > 0xf00000.
```

> **GOTCHA —** the convention is **per binary, not global**. Over-generalising "VA == file offset" onto an `hlo-opt`/`hlo2penguin` address is off by `0x201000`/`0x200000`, and onto a `.data`-resident struct is off by the `.data` VMA/file-offset delta (a separate `0x400000`-class shift not used by any `.text`/`.rodata` symbol below). Every address in this index is the **symbol-table VA** in frame (A)/(B); when a page cites a smaller sidecar number for the same symbol, that is frame (C), not a contradiction.

---

## 1. libBIR.so — the BIR core

The definitional root: `bir::Instruction` and the 110-opcode hierarchy, the `pelican` symbolic algebra, the Dtype tables, the cast engine, and the BIR-JSON SerDe. `.text`/`.rodata` VA == file offset.

### Core IR & opcode enum

| Symbol | Address | Page |
|---|---|---|
| `typeinfo for bir::Instruction` (`_ZTIN3bir11InstructionE`) | `0x8fcd78` (weak `V`) | [7.1 instruction-base](../bir/instruction-base.md) |
| `vtable for bir::Instruction` (`_ZTVN3bir11InstructionE`; vptr = sym `+0x10` = `0x8fcee8`) | `0x8fced8` | [7.1 instruction-base](../bir/instruction-base.md) |
| `bir::Instruction::Instruction(string&, BasicBlock*, InstructionType)` base ctor | `0x2dafb0` | [7.1 instruction-base](../bir/instruction-base.md) |
| `bir::Instruction::sameInst(Instruction*)` | `0x2db7b0` | [7.2 instruction-type](../bir/instruction-type.md) |
| `bir::InstructionType2string[abi:cxx11]` | `0x2d5bf0` | [7.2 instruction-type](../bir/instruction-type.md) · [14.1 master-opcode](master-opcode-table.md) |
| `bir::string2InstructionType` | `0x2da0b0` | [7.2 instruction-type](../bir/instruction-type.md) |
| per-leaf `bir::Inst*::sameInst` (weak `W`, e.g. `InstActivation` `0x2f3ff0`, `InstTensorScalar` `0x2f41a0`) | `0x2f3ff0+` | [7.2 instruction-type](../bir/instruction-type.md) |
| `bir::Module::getArch[abi:cxx11]()` | `0x354ef0` | [1.1 arch-object-model](../arch/arch-object-model.md) |
| `bir::Module::getHwm()` | `0x3555a0` | [1.1 arch-object-model](../arch/arch-object-model.md) |
| `getArchModel` (libBIR twin of the libwalrus door) | `0x478f90` | [1.1 arch-object-model](../arch/arch-object-model.md) |

### pelican symbolic algebra

| Symbol | Address | Page |
|---|---|---|
| `typeinfo for pelican::Expr` (`_ZTIN7pelican4ExprE`) | `0x90a908` | [7.16 pelican-hierarchy](../bir/pelican-hierarchy.md) |
| `pelican::AffineExpr` vtable (39-slot concrete leaf) | `0x90abe0` (ti-ptr `0x90abe8 → 0x90aa80`) | [7.16 pelican-hierarchy](../bir/pelican-hierarchy.md) |
| `pelican::ModuloExpr` vtable | `0x90ba10` | [7.21 pelican-moduleexpr](../bir/pelican-moduleexpr.md) |
| `pelican::Expr::getIndicesSet` (slot-14, the un-stripped vtable symbol) | (vtable slot) | [7.16 pelican-hierarchy](../bir/pelican-hierarchy.md) |

### Cast engine & BIR-JSON

| Symbol | Address | Page |
|---|---|---|
| `bir::CastToNewDType` family (cast/saturate) | `0x716f0`+ region (`0x782ca0`, `0x781928`) | [9.3 cast-to-new-dtype](../numerics/cast-to-new-dtype.md) |
| `adl_serializer<bir::InstructionType>::to_json` | `0x484db0` | [7.12 json-writer](../bir/json-writer.md) |
| `adl_serializer<bir::InstructionType>::from_json` | `0x484070` | [7.11 json-loader](../bir/json-loader.md) |

---

## 2. libwalrus.so — the backend

The 65 MB backend: ~150 passes, the dependence graph, the three schedulers, the SB/PSUM/DRAM/Reg allocators, loop optimization, the LNC splitter, `lower_dma`, the `CoreV{2,3,4}Gen` per-engine code generators, the BIR linker, `NeffPackager`, the `DebugInfoWriter`, the legality cascade, and `getArchModel`. `.text` (`0xf00000+`) / `.rodata` (`0x1c72000+`) VA == file offset. Retains its full dynamic symbol table (`nm -DC` is authoritative).

### Arch model & pass registry

| Symbol | Address | Page |
|---|---|---|
| `getArchModel(string)` — the only door into the Board/Device/Core tree | `0x17344c0` | [1.1 arch-object-model](../arch/arch-object-model.md) |
| `Pacific()` deprecated default-arch stub | `0x17345d0` | [1.1 arch-object-model](../arch/arch-object-model.md) |
| arch-model `.bss` singletons (`_inferentia` `0x3e05800`, `_sunda` `0x3e05980`, `_cayman` `0x3e05b00`, `_core_v4` `0x3e05c80`) | `0x3e05800+` | [1.1 arch-object-model](../arch/arch-object-model.md) |
| `BackendPass` vtable | `0x3d98548` | [8.1 backendpass-registry](../walrus/backendpass-registry.md) |
| `BackendPass::run(vector<unique_ptr<Module>>&)` | `0x1736d40` | [8.1 backendpass-registry](../walrus/backendpass-registry.md) |
| `GeneratorRegistration::getInstance()` | `0x17356e0` | [8.1 backendpass-registry](../walrus/backendpass-registry.md) |
| `registerGenerator` / `getGenerator` / `hasGenerator` | `0x1735910` / `0x1735740` / `0x1735630` | [8.1 backendpass-registry](../walrus/backendpass-registry.md) |

### Per-engine code generators (the L3 wire path)

| Symbol | Address | Page |
|---|---|---|
| `CoreV2GenImpl::setupHeader(void*, void*)` | `0x1172120` | [8.39 opcode-master](../walrus/opcode-master.md) · [14.1 master-opcode](master-opcode-table.md) |
| `CoreV3GenImpl::setupHeader(void*, void*)` | `0x1369280` | [8.39 opcode-master](../walrus/opcode-master.md) |
| `CoreV4GenImpl::setupHeader(void*, void*)` | `0x143f440` | [8.39 opcode-master](../walrus/opcode-master.md) |
| `core_v4::enum_variant_string_opcode(int, char*, int)` | `0x143fd80` | [2.24 isa-enum-ordinals](../isa/isa-enum-ordinals.md) |
| `core_v2` / `core_v3` `enum_variant_string_opcode` twins | `0x127aea0` / `0x1369a40` | [2.24 isa-enum-ordinals](../isa/isa-enum-ordinals.md) |
| `CoreV2GenImpl::visitInstCustomOp(InstCustomOp&)` — the `0x85`/`0x86` emitter | `0x12613c0` | [11.8 customop-codegen](../custom-ops/customop-codegen.md) |

### Schedulers & allocators

| Symbol | Address | Page |
|---|---|---|
| `PostSched::run(Module&)` (LNC=1) | `0xc3fc90` | [8.11 post-sched-schedulers](../walrus/post-sched-schedulers.md) |
| `PostSched::run(vector<unique_ptr<Module>>&)` (LNC dispatch) | `0xc41c10` | [8.11 post-sched-schedulers](../walrus/post-sched-schedulers.md) |
| `post_scheduler::schedule_block(BasicBlock&)` (greedy list) | `0xc2b3b0` | [8.11 post-sched-schedulers](../walrus/post-sched-schedulers.md) |
| `TimeAwareScheduler` ctor (cycle-accurate) | `0xc549b0` | [8.11 post-sched-schedulers](../walrus/post-sched-schedulers.md) |
| `ColoringAllocator` ctor | `0x9870c0` | [8.16 allocator-drivers](../walrus/allocator-drivers.md) |
| `ColoringAllocatorWithLoop` ctor (optlevel-6 only) | `0xa84fc0` | [8.16 allocator-drivers](../walrus/allocator-drivers.md) |
| `flattern_loop` (loop-flattening linearize) | `0xa89a60` | [8.16 allocator-drivers](../walrus/allocator-drivers.md) |

### Legality cascade

| Symbol | Address | Page |
|---|---|---|
| `birverifier::InstVisitor::visitInstCustomOp(InstCustomOp&)` | `0xfa7bd0` | [8.41 birverifier-per-op](../walrus/birverifier-per-op.md) |
| `is_valid_s3_lw` (L2 wire validator entry; sidecar frame `0x5fbd40`) | `0x14ac170` | [8.42 l2-wire-validator](../walrus/l2-wire-validator.md) |

### KLR→BIR codegen (beta2) & NEFF packaging

| Symbol | Address | Page |
|---|---|---|
| `TranslateNKIASTToBIR::run(Module&)` (driving pass) | `0xf0dbc0` | [7.27 klir-codegen-dispatch](../bir/klir-codegen-dispatch.md) |
| `KlirToBirCodegen::codegenStmt` (dispatch core) | `0xf33520` | [7.27 klir-codegen-dispatch](../bir/klir-codegen-dispatch.md) |
| `KlirToBirCodegen::codegenOperator` | `0xf30db0` | [7.27 klir-codegen-dispatch](../bir/klir-codegen-dispatch.md) |
| `KlirToBirCodegen` master switch jump table (`jpt`, 77 cases) | `0x1de95f8` (`.rodata`) | [7.27 klir-codegen-dispatch](../bir/klir-codegen-dispatch.md) |
| `KlirToBirCodegen(bir::Function*)` ctor | `0xf0faa0` | [7.27 klir-codegen-dispatch](../bir/klir-codegen-dispatch.md) |
| `NeffFileWriter::initializeNeffHeader` | `0x1540a00` | [12.2 neff-header-bom-writer](../formats/neff-header-bom-writer.md) |
| `NeffFileWriter::writeArchiveFile` (PAX write loop) | `0x153e030` | [12.2 neff-header-bom-writer](../formats/neff-header-bom-writer.md) |
| `addToBom` (BOM upsert) | `0x153fb80` | [12.2 neff-header-bom-writer](../formats/neff-header-bom-writer.md) |
| `NeffPackager::run` → `writePackageFile` → `writeFile` | `0x15307e0` → `0x15200e0` → `0x1541040` | [12.2 neff-header-bom-writer](../formats/neff-header-bom-writer.md) |

### DebugInfoWriter (the provenance producer)

| Symbol | Address | Page |
|---|---|---|
| `DebugInfoWriter::createDebugInfoForInstruction(Instruction&)` | `0x11fa6a0` | [8.40 debuginfo-writer](../walrus/debuginfo-writer.md) |
| `IRVisitor<DebugInfoWriter>::visit<…BasicBlock…>` | `0x11d64a0` | [8.40 debuginfo-writer](../walrus/debuginfo-writer.md) |
| `DebugInfoWriter::leaveModule(Module&)` | `0x11d5850` | [8.40 debuginfo-writer](../walrus/debuginfo-writer.md) |
| `writeDebugInfoToDisk(Module&, EngineType, IRLayer)` | `0x11f9b80` | [8.40 debuginfo-writer](../walrus/debuginfo-writer.md) |

> **NOTE —** `DebugInfoWriter` is the **sole producer** of the `neuroncc::debug_info` protobuf. Its six core classes are byte-identical in `libBIR`, `libwalrus`, and `hlo2penguin`, but they cross the frontend↔backend cut by **independent compilation of the same generated `.proto`**, not by a link — `hlo2penguin` links none of the backend `.so`. This is the single cross-cut domain type ([§9](#9-cross-binary-classsymbol-matrix) row `neuroncc::debug_info`).

---

## 3. libBIRSimulator.so — the functional simulator

The BIR functional simulator (`birsim`), the whole-machine state model, and the DAP debugger half. Owns the **engine** half of the `birsim` namespace (10 disjoint classes: `Memory`, `SyncState`, `TpbEngines`, `NeuronCoresManager`, `PhysicalMemory`, `BaseLogger`) — disjoint (shared=0) from `libwalrus`'s `birsim::debugger` half. Links `libBIR` (dynamic) + `libpwp_sim`. `.text`/`.rodata` VA == file offset.

| Symbol class | Owner note | Page |
|---|---|---|
| `birsim::engine::{Memory, SyncState, TpbEngines, …}` | `LOCAL(10)` — engine state, defined here | [7.34 sim-dispatch-state](../bir/sim-dispatch-state.md) |
| BIR sim dispatch + whole-machine state model | the sim driver | [7.34 sim-dispatch-state](../bir/sim-dispatch-state.md) |
| Sim core arithmetic (Cast / Accumulate / RNE / MemoryReductionOp) | per-op evaluators | [7.35 sim-core-arithmetic](../bir/sim-core-arithmetic.md) |
| `birsim::InstVisitor::visitInstCustomOp` (sim-side custom-op) | (undefined `U` in libwalrus; defined here) | [7.39 sim-control-sync-customop](../bir/sim-control-sync-customop.md) |
| `birsim::debugger` DAP host (in **libwalrus**, not here) | `OWN(39)` libwalrus | [7.41 birsim-dap-debugger](../bir/birsim-dap-debugger.md) |

> **QUIRK —** `birsim` is a **split namespace**. The debugger/driver half (39 classes, `DebuggerCallbacks @ 0x1109430`) lives in `libwalrus`; the engine half (10 classes) lives here. A reimplementer looking for `birsim::Memory` in `libwalrus` will not find it, and vice-versa for `birsim::debugger`. The two halves share zero typeinfos — `birsim` is not a single-binary namespace.

---

## 4. hlo-opt — the HLO optimizer

The standalone HLO/MHLO/StableHLO optimizer ELF and its `--passes` registry. **Frame (B):** `.text` file_off = VA − `0x201000`, `.rodata` file_off = VA − `0x200000`. Statically links upstream OpenXLA/MLIR/LLVM — the Neuron-authored passes are a thin layer over that bulk.

### Collective stream/channel-id family

| Symbol | Address (VA) | Page |
|---|---|---|
| `xla::hlo_query::NextChannelId(module)` | `0x8ab1ac0` | [4.4 collective-stream-channel-id](../hlo-opt/collective-stream-channel-id.md) |
| `NeuronCollectiveStreamIdInjector::Run` (stamps `stream_id` — a 2-way `'0'`/`'1'` replica-group partition, **not** a `NextStreamId` counter) | `0x1f951a0` | [4.4 collective-stream-channel-id](../hlo-opt/collective-stream-channel-id.md) |
| `NeuronUniqueChannelIdEnforcer::Run` | `0x1fecb40` | [4.4 collective-stream-channel-id](../hlo-opt/collective-stream-channel-id.md) |
| `CollectiveStreamIdChecker::Run` (registration #58, read-only) | `0x1e8c800` | [4.4 collective-stream-channel-id](../hlo-opt/collective-stream-channel-id.md) |
| `neuron::IsCollective` (masks `0x20000410` / `0x80011`, high rebase −`0x53`) | `0x1f7e010` | [4.4 collective-stream-channel-id](../hlo-opt/collective-stream-channel-id.md) |
| `ReplicaGroupsEqual` | `0x9163ca0` | [4.4 collective-stream-channel-id](../hlo-opt/collective-stream-channel-id.md) |

> **GOTCHA — three separate predicates, three separate mask sets.** The masks `0x650` / `0x810000000000001` belong to `NextChannelId @ 0x8ab1ac0` and to the inlined checker #58. `IsCollective @ 0x1f7e010` uses a different pair, `0x20000410` / `0x80011` (high band indexed by `op − 0x53`) plus `op == 7`, selecting opcodes `{0x4, 0x7, 0xa, 0x1d, 0x53, 0x57, 0x66}`. Do not cross-apply the masks between them. See [4.4 collective-stream-channel-id](../hlo-opt/collective-stream-channel-id.md).

### Pass registry

| Symbol class | Note | Page |
|---|---|---|
| the `--passes` pass-registry table | the `hlo-opt` dispatch surface | [4.1 pass-registry](../hlo-opt/pass-registry.md) |
| HLO/mhlo/stablehlo ingestion (stock-vs-Neuron boundary) | the ingestion cut | [4.2 hlo-ingestion-boundary](../hlo-opt/hlo-ingestion-boundary.md) |

---

## 5. hlo2penguin — the MLIR front-half

The standalone MLIR front-half ELF: mhlo/stablehlo → Penguin Python IR. **Frame (B)** (same `−0x201000` / `−0x200000` shift). 44,794 unique typeinfos, of which **~0.5 %** are Neuron-authored — the rest is statically-linked upstream MLIR/XLA/LLVM/oneDNN, with oneDNN **present but dead** on the Trainium path. Links **none** of the backend `.so`.

| Symbol class | Owner note | Page |
|---|---|---|
| `hilo` Neuron device model (Mariana/Tonga/Sunda/Cayman cost tables) | `LOCAL(151)` | [4.42 mhlo-to-python-printer-driver](../hlo-opt/mhlo-to-python-printer-driver.md) |
| `xla::hilo` | `link(84)` upstream | [4.42 mhlo-to-python-printer-driver](../hlo-opt/mhlo-to-python-printer-driver.md) |
| `MhloToPythonPrinter` Penguin emission driver | the emitter front | [4.42 mhlo-to-python-printer-driver](../hlo-opt/mhlo-to-python-printer-driver.md) |
| `MhloToPythonPrinter` heavy/collective/fusion emitters | the heavy-emitter half | [4.43 mhlo-to-python-printer-heavy](../hlo-opt/mhlo-to-python-printer-heavy.md) |
| hlo2penguin MLIR pipeline order & entry flow | the pipeline driver | [4.34 hlo2penguin-mlir-pipeline](../hlo-opt/hlo2penguin-mlir-pipeline.md) |
| `neuroncc::debug_info` (6 protobuf classes) | `dyn(6)` — independent-compile bridge | [8.40 debuginfo-writer](../walrus/debuginfo-writer.md) |

> **NOTE —** `mlir` (10,328), `xla` (4,634), `dnnl` (13,786 substring / 2,100 `_ZTI`), `absl` (1,899), and `llvm` (7,033) typeinfos are **upstream, statically linked** — not a Neuron domain boundary. They are deliberately *not* indexed per-symbol here: a reimplementer wanting `mlir::*` reads upstream MLIR, not this wiki. The oneDNN bulk is dead code on the Trainium path.

---

## 6. libpwp_sim.so — the PWP simulator

A fully isolated domain: `PWPSim::Simulator` + `PWPSim::AFTable`, 4 types total. `NEEDED` by `libwalrus`/`libBIRSimulator`/`nki_klr_sim` for the numeric activation model, but exposes **no** shared `bir`/`pelican`/`klr` type. `.text`/`.rodata` VA == file offset.

| Symbol class | Note | Page |
|---|---|---|
| `PWPSim::Simulator` / `evaluate_generic` | the piecewise-polynomial activation evaluator | [7.40 sim-activation-pwp](../bir/sim-activation-pwp.md) · [10.1 pwp-model](../activation/pwp-model.md) |
| `PWPSim::AFTable` | the activation-function table | [10.6 loadactfuncset](../activation/loadactfuncset.md) |

---

## 7. libBIRParserDumper.so — the BIR-JSON round-trip

`parserdumper` / `bir_roundtrip` — the BIR-JSON read-write round-trip serializers. Links `libBIR` (dynamic). 557 typeinfos. Carries 26/29 of the `pelican` classes and a `bir` subset + its own `bir::IRVisitor<DumperPass>` instantiations.

| Symbol class | Note | Page |
|---|---|---|
| `parserdumper` / `bir_roundtrip` round-trip serializer | BIR-JSON debug + round-trip | [8.44 parserdumper](../walrus/parserdumper.md) |

---

## 8. The Cython codegen modules

The NKI forward builder, the beta3 Penguin→BIR driver, and the re-emit printer ship as per-Python-version Cython extension `.so`. **Frame (A)** (VA == file offset for `.text`/`.rodata`); the `__pyx_pw_*` / `__pyx_pf_*` symbols are the Cython-mangled method labels. `KernelBuilder.so` ships **unstripped with DWARF** — the most readable binary in the NKI stack.

| Symbol | Address | Binary | Page |
|---|---|---|---|
| `NeuronCodegen.matmult` arg-parse wrapper (largest single method, 64,948 B) | `0x266520` | `KernelBuilder.so` | [6.5.1 neuroncodegen-forward-builder](../nki/neuroncodegen-forward-builder.md) |
| `NeuronCodegen.matmult_mx` (106,470 B) | `0x279fe0` | `KernelBuilder.so` | [6.5.1 neuroncodegen-forward-builder](../nki/neuroncodegen-forward-builder.md) |
| `BirCodeGenLoop` matmul codegen / driver entry | `0x975f0` (driver), `0xc0840` (`codegenCustomOp`, `_215`) | `BirCodeGenLoop.so` | [6.5.10 bircodegenloop](../nki/bircodegenloop.md) |
| `BirCodeGenLoop.codegenSundaCustomOp` (`_259`, IMPL Sunda override, 12,192 B) | `0x652d0` | `BirCodeGenLoop.so` | [11.8 customop-codegen](../custom-ops/customop-codegen.md) |
| `BirCodeGenLoopGen.codegenSundaCustomOp` (`_69`, GEN allocator) | `0x46d00` | `BirCodeGenLoopGen.so` | [11.8 customop-codegen](../custom-ops/customop-codegen.md) |
| `NkiCodegen` re-emit printer (penguin.ir → NKI text) | (Cython `__pyx_pf_*`) | `NkiCodegen.so` | [6.5.9 nkicodegen-printer](../nki/nkicodegen-printer.md) |

> **QUIRK —** `KernelBuilder.so`/`NeuronCodegen` (the **forward** builder, NKI Python → Penguin IR) and `NkiCodegen.so`/`NkiCodegen` (the **reverse** printer, Penguin IR → NKI text) are different classes in different files. They are not the two ends of one pass — `NkiCodegen` is the inverse direction. Confusing them is the most common NKI-layer error; see [0.4 binary-inventory](../reference/binary-inventory.md).

---

## 9. Cross-binary class/symbol matrix

The ownership matrix — namespace (row) × binary (col). `OWN` = canonical definer (weak `V` typeinfo); `dyn` = present by dynamic link to the owner (DT_NEEDED + weak coalescing); `LOCAL` = binary-local, not shared; `·` = absent, zero hits. Numbers in `()` are distinct-type counts measured in that binary's `rtti.json`. This is what disambiguates *which* binary's copy a typeinfo address refers to.

| Namespace | libBIR | libwalrus | libBIRSim | nki_klr_sim | hlo2penguin |
|---|---|---|---|---|---|
| `bir` | **OWN(371)** | dyn(95) | dyn(21) | dyn(14) | · |
| `pelican` | **OWN(29)** | dyn(29) | dyn(26) | dyn(29) | · |
| `klr` | · | **OWN(199)** | · | link(199) | · |
| `dap` | · | **LOCAL(201)** | · | · | · |
| `birsim::debugger` | · | **OWN(39)** | · | · | · |
| `birsim::engine` | · | · | **LOCAL(10)** | · | · |
| `neuroncc::debug_info` | dyn(6) | **OWN(6)** | · | · | dyn(6)* |
| `hilo` (Neuron device model) | · | · | · | · | **LOCAL(151)** |
| `xla::hilo` | · | · | · | · | link(84) |
| `mlir` / `xla` / `dnnl` | · | · | · | · | link (upstream) |
| `llvm` | link | link | link | link | link |
| `PWPSim` | · | NEEDED | NEEDED | via sim | · |

`*` — `neuroncc::debug_info` in `hlo2penguin` is **not** reached by a link (it links no backend `.so`); the 6 classes are the same generated protobuf compiled independently into each binary. The 8 `_ZTI` names are byte-identical across `libBIR ∩ libwalrus ∩ hlo2penguin` — the only frontend↔backend crossing type.

The domain boundaries: **frontend ↔ backend is a clean cut** — `hlo2penguin` has `bir=0, pelican=0, klr=0, dap=0, birsim=0` (absolute zero), and the backend `.so` have `xla=0, mlir=0, dnnl=0`. The single exception is the six `neuroncc::debug_info` classes. The `DT_NEEDED` DAG is rooted at `libBIR` (leaf) → `libBIRParserDumper`/`libBIRSimulator` → `libwalrus` (top), with `libpwp_sim` isolated and `hlo2penguin` linking only `{libm, libstdc++, libgcc_s, libc}`.

---

## 10. Symbol-table spot checks — five highest-traffic symbols

Each was read from the binary's own dynamic symbol table (`nm -DC`), and the cited page checked for existence. All five resolved exactly.

| Symbol | Address | Binary | Page exists? | `nm -DC` entry | Confidence |
|---|---|---|---|---|---|
| `getArchModel(string)` | `0x17344c0` | libwalrus | yes — [1.1](../arch/arch-object-model.md) | `T getArchModel(std::__cxx11::basic_string…)` @ `0x17344c0` | CERTAIN |
| `KlirToBirCodegen::codegenStmt` (dispatch core) | `0xf33520` | libwalrus | yes — [7.27](../bir/klir-codegen-dispatch.md) | `nm -DC … rg KlirToBirCodegen` = 176 rows; dispatch core `0xf33520`, jpt `0x1de95f8` | CERTAIN |
| `NextChannelId(module)` | `0x8ab1ac0` | hlo-opt | yes — [4.4](../hlo-opt/collective-stream-channel-id.md) | VA is in frame (B); see the mask GOTCHA above for which predicate owns which mask | HIGH |
| `visitInstCustomOp` (`CoreV2GenImpl`) | `0x12613c0` | libwalrus | yes — [11.8](../custom-ops/customop-codegen.md) | `T …CoreV2GenImpl::visitInstCustomOp(bir::InstCustomOp&)` @ `0x12613c0` (distinct from `birverifier` `0xfa7bd0`) | CERTAIN |
| `setupHeader` (`CoreV4GenImpl`) | `0x143f440` | libwalrus | yes — [8.39](../walrus/opcode-master.md) | `W …CoreV4GenImpl::setupHeader(void*, void*)` @ `0x143f440`; V2 `0x1172120`, V3 `0x1369280` | CERTAIN |

> **NOTE —** `visitInstCustomOp` is the canonical example of why this index is **per binary**: the name appears at `0xfa7bd0` (`birverifier::InstVisitor`, the verifier — [8.41](../walrus/birverifier-per-op.md)), at `0x12613c0` (`CoreV2GenImpl`, the encoder — [11.8](../custom-ops/customop-codegen.md)), and as an undefined `U` (`birsim::InstVisitor`, defined in `libBIRSimulator` — [7.39](../bir/sim-control-sync-customop.md)). A bare "`visitInstCustomOp @ …`" is ambiguous until the binary and the address fix which one.

---

## Cross-References

- [0.4 Binary Inventory & the .so Map](../reference/binary-inventory.md) — the artifact atlas this index keys on: sizes, paths, the five distinct ~230 MB tool ELFs, the eight `.so`, the Cython galaxy.
- [0.6 Build & Version Provenance](../reference/versions.md) — the cp310/311/312 cross-wheel parity argument and why addresses drift across wheels.
- [14.1 Master Opcode Reference](master-opcode-table.md) — the opcode-space sibling appendix; `setupHeader` / `InstructionType2string` / `enum_variant_string_opcode` are documented there in full.
- [14.2 Master Dtype / Enum Reference](master-enum-dtype-table.md) — the dtype/enum-ordinal sibling appendix.
- [1.1 The Arch Object Model](../arch/arch-object-model.md) — `getArchModel` and the Board/Device/Core accessor chain.
- [7.27 KlirToBirCodegen Dispatch Core](../bir/klir-codegen-dispatch.md) — the 77-case master routing table behind `codegenStmt`.
- [8.39 The Opcode Master](../walrus/opcode-master.md) — the `setupHeader` L3 wire path.
