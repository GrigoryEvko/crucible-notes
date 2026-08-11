# DebugInfoWriter & the `ir_debug_info` Debug-Info Protobuf

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 share the same `libwalrus.so` and the byte-identical proto module). The producer lives in `neuronxcc/starfish/lib/libwalrus.so`; the generated message classes are `neuroncc::debug_info::*`. For `.text` (`0x62d660+`) and `.rodata` (`0x1c72000+`) the virtual address equals the file offset; `.data` carries a `0x400000` VMA↔fileoff delta and the `0x5e…/0x60…/0x62…` aliases are ICF / `.plt` thunks — every body cited here is a real function above `0x62d660`. Other wheels differ; treat every address as version-pinned.*

## Abstract

A NEFF carries **source-line provenance**: when the Neuron profiler shows you that a given hardware instruction came from line 47 of your NKI kernel, or that a particular copy is a compiler-inserted spill, it is reading a set of protobuf files the backend emitted. This page recovers the producer of those files — `neuronxcc::backend::DebugInfoWriter` in `libwalrus.so` — end to end: the per-instruction record collector, the **five** protobuf message types (`instruction_debug_info`, `ir_debug_info`, `debug_info_stack_frame_index`, `file_location`, `stack_frame`) plus the `AttrsEntry` map-entry, and the frontend→backend bridge that carries an HLO/NKI op's `(file, line, function)` down to a serialized record. The bar is that a reimplementer can rebuild the writer and the wire format: which BIR fields each proto field reads, the exact field numbers and wire tags, the per-engine / per-layer file split, and how the files are registered into the NEFF.

The whole subsystem is gated by one hidden `cl::opt`, runs once per BIR instruction during codegen, and serializes one `ir_debug_info` message **per engine, per IR layer** as a standalone protobuf-wire `.dbg` file under the module's artifact directory. The driver that constructs the writer (`Codegen::codegen`) is the backend codegen driver (see the `walrus/codegen-driver.md` sibling — may be in-flight; if absent, the construction site is `Codegen::codegen @ 0x11d2c50`). The `.dbg` files become NEFF members during packaging (Part 12 — NEFF Container, planned). The provenance the writer reads is seeded one stage upstream on `bir::OpDebugInfo`, which is itself fed from the frontend's XLA/MHLO stack-frame machinery (the `nki/framework-kernel-lifecycle.md` sibling covers the `backend_config` / op-name provenance on the NKI side).

| | |
|---|---|
| **Producer class** | `neuronxcc::backend::DebugInfoWriter` (`libwalrus.so`) |
| **Record collector** | `createDebugInfoForInstruction(bir::Instruction&)` @ `0x11fa6a0` |
| **BB / inst walk** | `bir::IRVisitor<DebugInfoWriter>::visit<…BasicBlock…>` @ `0x11d64a0` |
| **Finalize + emit** | `leaveModule(bir::Module&)` @ `0x11d5850` |
| **Serialize → file** | `writeDebugInfoToDisk(Module&, EngineType, IRLayer)` @ `0x11f9b80` |
| **Enable gate** | `cl::opt<bool> "enable-neff-debug-info"` (hidden, **default ON**) `&& arch_level ≤ 49` |
| **Dependency cap** | `cl::opt<unsigned> "debug-info-max-dependencies-per-inst"` (hidden, default 100) |
| **Proto package** | `neuroncc.debug_info` (proto3, `optimize_for = LITE_RUNTIME`) |
| **Wire output** | per-engine/per-layer `debug_info_{backend,asm}.<eng>.<n>.dbg` |

The string evidence is direct: `rg -a` over `libwalrus.so` finds the option names `enable-neff-debug-info` / `debug-info-max-dependencies-per-inst`, the description literal `Enable writing debug info into the NEFF`, the timed-block label `debug_info_gen`, the five layer tags `debug_info_{pttf,hlo,penguin,backend,asm}`, the proto package `neuroncc.debug_info`, the generated TU name `ir_debug_info.pb.cc`, and every recovered proto field-name string (`parent_debug_info_filename`, `kernel_filenames`, `kernel_functions`, `file_names`, `function_names`, `stack_frame_index`). All writer methods resolve to real `.dynsym` symbols at the cited addresses (`nm -D`).

---

## 1. The enable gate

The writer is one guarded block at the end of the backend codegen entry. The construction, the BB walk, and all on-disk emission live behind it:

```c
// neuronxcc::backend::Codegen::codegen(bir::Module&) @ 0x11d2c50  (the sole writer caller)
if (enableNeffDebugInfo && arch_level <= 49) {       // the gate
    log("Generating debug info");                    // .rodata literal
    Stopwatch sw("debug_info_gen");                  // timed block label
    DebugInfoWriter w(module, /*…*/);
    w.initDebugInfo();                               // 0x11f8630
    // visit "main" first, then the remaining functions
    for (Function& f : module.functionsMainFirst())
        IRVisitor<DebugInfoWriter>::visit(w, f.blocks());   // 0x11d64a0
    w.leaveModule(module);                           // 0x11d5850 — finalize + emit
}                                                    // ~DebugInfoWriter @ 0x11d76a0
```

Two hidden options control the phase. Both are `cl::OptionHidden` registrars, and both carry their name and description as `.rodata` strings:

| Option | Type | Default | Storage global | Effect |
|---|---|---|---|---|
| `enable-neff-debug-info` | `cl::opt<bool>` | **1 (ON)** | `enableNeffDebugInfo` | master enable; gates construction, walk, and emission. Disable with `--enable-neff-debug-info=false`. desc = *"Enable writing debug info into the NEFF"* |
| `debug-info-max-dependencies-per-inst` | `cl::opt<unsigned>` | 100 | `debugInfoMaxDependenciesPerInst` | caps the per-inst dependency list (§2.4); over-limit counted and warned at `leaveModule`. desc = *"The maximum number of dependencies per instruction to encode into the NEFF built-in debug info"* |

The arch sub-gate `arch_level <= 49` admits Inferentia(10)/Sunda(20)/gen3(30)/CoreV4(40) and **excludes** CoreV5(50) — debug-info writing is wired for the shipping arch set only. The `<= 49` immediate is read from the gate itself; the ArchLevel↔value mapping comes from the arch-model page.

> **Why the default is ON.** Because `enableNeffDebugInfo` initializes to 1, a stock `neuronx_cc` invocation emits debug info into every NEFF unless explicitly disabled. The option is hidden, so it does not appear in `--help`. This is the source of the `.dbg` members a reimplementer will find in any normally-compiled NEFF.

---

## 2. The per-instruction record collector

`createDebugInfoForInstruction` (`0x11fa6a0`) is called once per non-control BIR instruction by the visit walk (§4.1). It builds **one** `instruction_debug_info` message and tail-appends it to the per-engine `ir_debug_info` container, keyed by the instruction's owning engine. The proto message is arena-allocated (`google::protobuf::Arena::CreateMaybeMessage<instruction_debug_info>` @ `0x1889600`) or a recycled cleared `RepeatedPtrField` slot is reused.

```c
// createDebugInfoForInstruction(bir::Instruction& inst)  @ 0x11fa6a0
EngineType eng = inst.engine();                       // Inst+0x90 (uint)
ir_debug_info& ir = container[eng];                   // _Hashtable<EngineType, ir_debug_info> at this+88
instruction_debug_info* msg = ir.add_instructions();  // Arena::CreateMaybeMessage @ 0x1889600

msg->set_id(inst.unique_id());                        // f1  <- Inst+0x48
msg->set_name(truncateName(inst.name()));             // f2  <- NamedObject Inst+0x18/+0x20, capped 0x200 / 509B
```

The collector works on the proto message's *collector-side* working offsets; the proto's own `_impl_` offsets (recovered from `hlo2penguin`) differ and are listed in §3. The semantics below are field-stable across both views.

### 2.1 id, name

`id` (f1) is the instruction's `unique_id` (`Inst+0x48`). `name` (f2) is the BIR `NamedObject` name truncated by `truncateName` (`0x11fc880`) to `0x200`, re-created at 509 bytes to bound NEFF size.

### 2.2 parent_ids — the tensorizer/HLO parent link (f3)

`parent_ids` (f3) carries the **tensorizer op id** from the instruction's `bir::OpDebugInfo`. `OpDebugInfo` lives in the dep block at `Inst+0x960` as a `boost::optional<bir::OpDebugInfo>` and is read via `bir::Instruction::getDebugInfo`. Its getters (in `libBIR`) are `getOpName / getTensorizerId / getFileName / getLineNo / getKernelName`. When `getTensorizerId()` is engaged, the id is appended to the f3 repeated-uint64 list. This is the upstream id that links the BIR inst back through the penguin/tensorizer layer toward the originating HLO op.

> **GOTCHA — f3 is dual-use.** The proto descriptor names f3 `parent_ids`, the tensorizer/HLO-parent list. The ASM layer (§4.2) reuses that same slot to carry the BIR inst id for each ISA bundle. Both uses are legal wire-wise, but a decoder that assumes "f3 ⇒ tensorizer parent" will misread every ASM-layer record.

### 2.3 file / line / function — Attrs entries that index the dedup tables (f7)

When `OpDebugInfo.getFileName()` is engaged, the source location is interned and then stored as per-inst `Attrs` map entries:

```c
// inner block @ 0x11fa6a0 :202-284
uint64 fileIdx = getOrInsertFilenameIndex(ir, filename);     // 0x11f8320 -> ir.kernel_filenames (f5)
uint64 funcIdx = getOrInsertFunctionNameIndex(ir, kernel);   // 0x11f83b0 -> ir.kernel_functions  (f7-container)
//   both: linear std::find over the RepeatedPtrField<string>; Add+assign if absent; return index
attrs.insert({key12, std::to_string(fileIdx)});              // 12-char keys, InnerMap::insert<char const(&)[12]>
attrs.insert({key12, std::to_string(lineNo)});               // getLineNo
attrs.insert({key12, std::to_string(funcIdx)});
// missing kernel name -> log "Unable to find kernel name " (warn)
```

`getOrInsertFilenameIndex` / `getOrInsertFunctionNameIndex` are `.dynsym` symbols at `0x11f8320` / `0x11f83b0`, and their mangled signatures take `neuroncc::debug_info::ir_debug_info&` — direct proof of the `neuroncc::` (not `neuronxcc::`) proto namespace. Each performs a linear find over the container's `kernel_filenames` (f5) / `kernel_functions` (f7) `RepeatedPtrField<string>`, returning an existing index or appending. So a filename/function is stored **once** per engine; everywhere else it is an integer index carried in the Attrs map.

### 2.4 dataflow + scheduling predecessors (f4/f9, f5/f10)

Two predecessor channels are walked, each producing a parallel `(id, kind)` pair. The dependency **kind** is the `EdgeKind` low-3-bit tag from the dep-edge `PointerIntPair`; `>4` triggers `llvm_unreachable("Unknown DependencyType")`.

```c
// Channel A — the per-op predecessor vector  -> f4 (ids) / f9 (kinds)   @ 0x11fa6a0 :384-483
for (auto* p = inst.preds_begin; p != inst.preds_end; ++p) {   // Inst+0xD8..+0xE0, stride 8
    if (++ndeps > debugInfoMaxDependenciesPerInst) {           // default 100
        log("Exceeded debug info data dependency limit for inst " + inst.name());
        ++this->overLimitInstCount;                            // this+448
        break;
    }
    msg->add_f4(p->id);            // dword[0]   -> dataflow_predecessors
    msg->add_f9(p->type);          // byte[4]    -> dataflow_predecessor_types
}

// Channel B — the live dependency-edge SET  -> f5 (ids) / f10 (kinds)   @ 0x11fa6a0 :301-383
for (EdgePtr e : inst.dependencies()) {                        // Inst+0xD0 (dep block) +0x20 hash-set head
    Instruction* tgt = e.target();
    msg->add_f5(tgt->unique_id());           // -> scheduling_predecessors  (Inst+0x48)
    msg->add_f10((int)(e & 7));              // EdgeKind&7 -> scheduling_predecessor_types
}
```

The proto descriptor **names** the channels: f4 = `dataflow_predecessors` (the per-op vector), f5 = `scheduling_predecessors` (the live edge set), with parallel kind arrays f9 = `dataflow_predecessor_types`, f10 = `scheduling_predecessor_types`. The edge set carries FLOW (dataflow) plus ANTI / OUTPUT / ORDERED (scheduling) kinds, so either channel can hold any kind; "dataflow/scheduling" labels the *source channel*, while the per-edge kind labels the *edge*.

The cap counter is compared against `debugInfoMaxDependenciesPerInst` (default 100). When channel B exceeds it, the writer logs the per-inst message and increments the over-limit counter at `this+448`, reported in `leaveModule`.

### 2.5 engine_type — the container key + proto remap (f4 of `ir_debug_info`)

The owning engine (`Inst+0x90`) is the container map key. The *proto* `engine_type` is set on the per-engine `ir_debug_info` (not the per-inst message) in `initDebugInfo` via the remap table `dword_1DF4B80`, verified byte-exact at file offset `0x1DF4B80`:

```
01df4b80: 0000 0000 0300 0000 0200 0000 0100 0000   {0, 3, 2, 1, ...}
01df4b90: 0400 0000 0500 0000 0600 0000 0000 0000   ..., 4, 5, 6, 0}
```

so `bir::EngineType → proto engine_type_t`: `Unassigned0→0, Pool1→3, Activation2→2, PE3→1, DMA4→4, DVE5→5, SP6→6, ALL7→0`. The proto ordinal differs from the internal `EngineType`; the proto enum is `{UNKNOWN_ENGINE=0, PE=1, ACTIVATION=2, POOL=3, DMA=4, DVE=5, SP=6}`. `>7` → `llvm_unreachable("Unknown EngineType")`.

### 2.6 instruction_type — REGULAR / SPILL / TRANSPOSE (f6)

```c
// @ 0x11fa6a0 :485-530
int it = REGULAR;                                   // 1, default
if (inst.opcode() == 8) {                           // Inst+0x58 == 8 (copy family)
    if (Instruction::canInstReadFromUninitMem(inst)) it = SPILL;        // 2
    else if (inst.flag_at(440))                      it = TRANSPOSE;    // 3 (transpose marker Inst+0x1B8)
} else if (Instruction::canInstReadFromUninitMem(inst)) it = SPILL;     // 2
msg->set_instruction_type(it);                       // f6, varint
```

The enum value pool is verified byte-exact at `0x1e1e540`: `REGULAR` `SPILL` `TRANSPOSE` `UNKNOWN_INSTRUCTION_TYPE`, with ordinals `REGULAR=1, SPILL=2, TRANSPOSE=3, UNKNOWN=0`. A spill/fill (an inst that may read uninitialized memory) is tagged `SPILL`; a transpose-introduced copy (opcode 8 with the transpose marker) is `TRANSPOSE`; everything else `REGULAR`. This is how the profiler distinguishes compiler-inserted spill/transpose ops from user ops. The selection logic is read from the calling body; `canInstReadFromUninitMem @ 0x3fd7510` does not decompile, so its exact predicate is [INFERRED] from the symbol name.

### 2.7 DMA-queue / DMA-block linkage (f3 + Attrs)

If `InstDMABlock::classof(inst)`: the writer reads the `DenseMap<InstDMABlock*, InstDMATrigger*>` at `this+424` (built during the visit walk for `InstDMATrigger` nodes, §4.1), maps `block → trigger → trigger id`, and appends the id to the f3 list. If the block's `DMAQueue` (`BBHolder+144`, kind==1) is set, an Attrs entry `"dma_queue" → <queue-name>` is inserted (the literal key `dma_queue` is a `.rodata` string).

### 2.8 byte-budget auto-flush

After each record the per-engine `ByteSizeLong()` is accumulated; when it crosses `0x70000000` (~1.875 GiB) the writer flushes that engine's BIR `ir_debug_info` to disk (layer 3), Clears the `RepeatedPtrField`, and resets the counter — producing a numbered multi-file split whose suffix comes from `getDebugFnCounterSuffix` (§4.5).

---

## 3. The protobuf schema (`neuroncc.debug_info`)

The schema is proto3, `optimize_for = LITE_RUNTIME` (so the message classes are `MessageLite` — no Descriptor/Reflection at runtime, and the `.dbg` files are raw wire). The five messages plus the `AttrsEntry` map-entry and three enums all appear as `Arena::CreateMaybeMessage<T>` instantiations in `libwalrus.so` at the addresses below — direct proof the producer constructs every type:

| Message | `Arena::CreateMaybeMessage<T>` @ | `_InternalSerialize` @ |
|---|---|---|
| `instruction_debug_info` | `0x1889600` | `0x188bf20` |
| `ir_debug_info` | `0x1889b70` | `0x188ce40` |
| `debug_info_stack_frame_index` | `0x1889750` | `0x1886ca0` |
| `file_location` | `0x1889670` | `0x1886aa0` |
| `stack_frame` | `0x18896e0` | `0x1886920` |
| `…_AttrsEntry_DoNotUse` | `0x1889590` | — (MapEntry) |

Wire tag = `(field# << 3) | wiretype`; packed-repeated scalars/enums use wiretype 2 (LEN). The `_impl_` offsets are `hlo2penguin`-recovered (the one non-stripped binary).

### message `instruction_debug_info` (core sizeof `0xd8`, cached_size @ `+0xd4`)

| f# | name | type | tag | `_impl_` off |
|---|---|---|---|---|
| 1 | `id` | uint64 | `0x08` | `+0xc0` |
| 2 | `name` | string | `0x12` | `+0xb8` (ArenaStringPtr, utf8-checked) |
| 3 | `parent_ids` | repeated uint64 (packed) | `0x1a` | `+0x10` |
| 4 | `dataflow_predecessors` | repeated uint64 (packed) | `0x22` | `+0x28` |
| 5 | `scheduling_predecessors` | repeated uint64 (packed) | `0x2a` | `+0x40` |
| 6 | `instruction_type` | enum `instruction_type_t` | `0x30` | `+0xd0` |
| 7 | `attrs` | `map<string,string>` (AttrsEntry) | `0x3a` | `+0x58` |
| 8 | `stack_frame_id` | uint64 | `0x40` | `+0xc8` |
| 9 | `dataflow_predecessor_types` | repeated enum (packed) | `0x4a` | `+0x80` |
| 10 | `scheduling_predecessor_types` | repeated enum (packed) | `0x52` | `+0xa0` |

> **NOTE — f8 `stack_frame_id` is an index, not a counter.** It is the per-inst leaf index into `ir_debug_info.stack_frame_index.stack_frames[]`, i.e. the instruction's source call-stack frame; reading the `+0xc8` slot as a sequence number will produce nonsense. The descriptor name and the consumer's de-intern fix the semantics; the producer-side store site — Attrs path versus a direct set — is [INFERRED]. Field pairing: f4↔f9 (dataflow), f5↔f10 (scheduling).

Nested `AttrsEntry` (`map_entry=true`): f1 key string (tag `0x0a`, `+0x10`), f2 value string (tag `0x12`, `+0x18`); serialized **deterministically** (MapSorterPtr sort before emit). Writer inserts 12-char keys (§2.3).

Nested enum `dependency_edge_kind_t` (f9/f10 element type): `INVALID=0, ORDERED=1, ANTI_DEPENDENCE=2, OUTPUT_DEPENDENCE=3, FLOW_DEPENDENCE=4` — identical to the BIR `EdgeKind` set (writer maps `EdgePtr&7`). Nested enum `instruction_type_t`: `UNKNOWN_INSTRUCTION_TYPE=0, REGULAR=1, SPILL=2, TRANSPOSE=3`. (Value names present in `.rodata`: `ANTI_DEPENDENCE`, `FLOW_DEPENDENCE`, `OUTPUT_DEPENDENCE`, `UNKNOWN_INSTRUCTION_TYPE`, `TRANSPOSE`.)

### message `file_location` (core sizeof `0x28`, three scalars)

| f# | name | type | tag | `_impl_` off |
|---|---|---|---|---|
| 1 | `file_name_id` | uint64 | `0x08` | `+0x10` |
| 2 | `function_name_id` | uint64 | `0x10` | `+0x18` |
| 3 | `line` | uint64 | `0x18` | `+0x20` |

A source location = `(file-name idx, function-name idx, line)`. Field 2 is `function_name_id`, **not** a column — there is no column field. The function name lives here, so `stack_frame` need not carry it.

### message `stack_frame` (two scalars)

| f# | name | type | tag | `_impl_` off |
|---|---|---|---|---|
| 1 | `file_location_id` | uint64 | `0x08` | `+0x10` |
| 2 | `parent_frame_id` | uint64 | `0x10` | `+0x18` |

`stack_frame` is a **call-stack node**: it points to its source `file_location` and to its *parent* frame (the caller). A full call stack is a chain `frame → parent → … → root`, not a flat `(loc, func)` pair. `instruction_debug_info.stack_frame_id` (f8) picks the leaf frame.

### message `debug_info_stack_frame_index` (the interned frame table; `ir_debug_info` f6)

| f# | name | type | tag | `_impl_` off |
|---|---|---|---|---|
| 1 | `file_names` | repeated string | `0x0a` | `+0x10` |
| 2 | `function_names` | repeated string | `0x12` | `+0x28` |
| 3 | `file_locations` | repeated `file_location` | `0x1a` | `+0x48` |
| 4 | `stack_frames` | repeated `stack_frame` | `0x22` | `+0x60` |

A 4-level intern: `file_names[]` / `function_names[]` are deduped string pools; `file_locations[]` index those pools; `stack_frames[]` index `file_locations[]` and chain via `parent_frame_id`. An instruction's provenance reduces to **one integer** (f8).

### message `ir_debug_info` (one per engine — the container)

| f# | name | type | tag | `_impl_` off |
|---|---|---|---|---|
| 1 | `description` | string | `0x0a` | `+0x58` |
| 2 | `parent_debug_info_filename` | string | `0x12` | `+0x60` |
| 3 | `instructions` | repeated `instruction_debug_info` | `0x1a` | `+0x18` (rep `+0x20`) |
| 4 | `engine_type` | enum `engine_type_t` | `0x20` | `+0x70` |
| 5 | `kernel_filenames` | repeated string | `0x2a` | `+0x28` |
| 6 | `stack_frame_index` | `debug_info_stack_frame_index` | `0x32` | `+0x68` |
| 7 | `kernel_functions` | repeated string | `0x3a` | `+0x40` |

f1 `description` and f2 `parent_debug_info_filename` are set in `initDebugInfo`. f5 `kernel_filenames` / f7 `kernel_functions` are the **container-level** dedup tables filled by `getOrInsert{Filename,FunctionName}Index` (§2.3) — distinct from the `stack_frame_index` (f6) `file_names` / `function_names` pools. The producer fills **both** families. All five field names appear in `.rodata` (`description`, `parent_debug_info_filename`, `kernel_filenames`, `kernel_functions`, `stack_frame_index`).

> **GOTCHA — there are two dedup families, not one.** Family A is `ir_debug_info.{kernel_filenames, kernel_functions}` (f5/f7), referenced by integer index from the per-inst Attrs map. Family B is `debug_info_stack_frame_index.{file_names, function_names, file_locations, stack_frames}` (f6), the centralized interned call-stack table keyed by f8. Both intern filenames and function names, they are filled independently, and their indices are not interchangeable.

### Serialization

`writeDebugInfoToDisk` (`0x11f9b80`) calls `MessageLite::SerializeToCodedStream(ir_debug_info, CodedOutputStream)` where the `CodedOutputStream` wraps an `OstreamOutputStream` over a `std::ofstream`. The full per-engine `ir_debug_info` is written as a **standalone protobuf-wire file** on disk — not inlined into one NEFF section at this stage.

---

## 4. The instruction walk, the ASM layer, and NEFF placement

### 4.1 The visit walk (`0x11d64a0`)

`IRVisitor<DebugInfoWriter>::visit` walks the BB list; per instruction:

- opcode 105/106/108 (Loop / DynamicForLoop / scheduling-unit wrapper) → **recurse** into the nested body.
- opcode 67 (`InstDMATrigger`) → populate the `DenseMap<InstDMABlock*, InstDMATrigger*>` at `this+424` by reading the trigger's `dma_blocks` vector (`trigger+288..+296`); each DMA block → its trigger. This is the join used in §2.7.
- else → `createDebugInfoForInstruction(inst)`.

`Codegen::codegen` visits the `main` function first, then the rest.

### 4.2 The ASM layer (`processAsmMappings` @ `0x11fb9d0`)

Gated on a per-writer "asm-mappings present" flag, this iterates a `DenseMap<EngineInfo, std::vector<Instruction*>>` populated during the engine codegen passes — the key is the engine, the value is the **emitted ISA-bundle order**. For each instruction in emit order it creates an ASM-layer `instruction_debug_info` carrying **only** the inst id (in the f3 list), appended to a separate per-engine ASM `ir_debug_info` map. Same `0x70000000` auto-flush → `writeDebugInfoToDisk(layer=4)`. The ASM records have most fields blank and occur in binary order, so every ISA instruction can be matched back to the BIR instruction that produced it.

### 4.3 DMA-function records (`createDebugInfoForDMABlocks` @ `0x11fb910`)

Walks every DMA `BasicBlockHolder`'s blocks and emits a full per-inst record for each DMA instruction (treated as a normal BIR inst with full scheduling deps) via `createDebugInfoForInstruction`.

### 4.4 `leaveModule` (`0x11d5850`) — finalize + emit

```c
// neuronxcc::backend::DebugInfoWriter::leaveModule(bir::Module& m)  @ 0x11d5850
createDebugInfoForDMABlocks(m);                 // 0x11fb910
processAsmMappings(m);                          // 0x11fb9d0
for (EngineType e : getEngineTypeStrings()) {   // skip Unassigned(0) and ALL(7)
    writeDebugInfoToDisk(m, e, /*layer=*/3);    // BIR
    if (e != DMA4)
        writeDebugInfoToDisk(m, e, /*layer=*/4);// ASM (skipped for DMA)
}
if (overLimitInstCount > 0)
    warn("Found N instructions with more than M dependencies into the built-in NEFF "
         "debug info to prevent excessive compile time and NEFF size. For those "
         "instructions, the Neuron profiler will not display the skipped dependencies.");
//   M = debugInfoMaxDependenciesPerInst; the tail literal is a .rodata string
```

### 4.5 File naming + NEFF registration (`writeDebugInfoToDisk` @ `0x11f9b80`)

```c
path = Module::getArtifactAbsPath() + "/" + getFullDebugFn(layer, engine, "dbg");  // 0x11fc070
// getFullDebugFn -> "<layer-tag>.<engine>.<counter-suffix>.dbg"
//   layer tags (.rodata): 0 debug_info_pttf, 1 debug_info_hlo,
//                                   2 debug_info_penguin, 3 debug_info_backend, 4 debug_info_asm
//   counter-suffix from getDebugFnCounterSuffix (0x11fbd90): empty for first file, "<N>" for splits
SerializeToCodedStream(ir, ofstream(path));
switch (layer) {
  case 3: ModuleArtifactInfo::addEngBIRDebugInfoFile(engine, fname); break;
  case 4: ModuleArtifactInfo::addEngASMDebugInfoFile(engine, fname); break;
  default: log("Unrecognized layer type in Backend debug info writer…");
}
// write failure -> "Failed to write debug info to <path>! The saved debug info will be incomplete"
```

`getFullDebugFn` (`0x11fc070`), `getDebugFnCounterSuffix` (`0x11fbd90`), `addEngBIRDebugInfoFile`, and `addEngASMDebugInfoFile` are all real symbols. The registered per-engine files are later collected into the NEFF by `BirLinker::symLinkDebugInfoFiles` (`0x60ca80`) — so debug info lands in the NEFF as a set of **separate per-(engine, layer) protobuf files** (the "built-in NEFF debug info"), not one monolithic inline section. The exact NEFF-membership mechanics belong to Part 12.

---

## 5. The HLO → BIR → NEFF provenance bridge

The proto package `neuroncc.debug_info` is the **backend's** serialization form. The provenance it serializes is threaded from the frontend:

```text
HLO op
  │  frontend interns op location into xla.HloModule StackFrameIndex
  │  (mlir::StackFrameIndexBuilder::AddStackFrameLocation — a SEPARATE upstream proto family,
  │   frontend-only; NOT the neuroncc proto)
  ▼
MHLO location ── penguin / tensorizer metadata ──► klr op
  │  KlirToBirCodegen seeds bir::OpDebugInfo { opName "nki-op", kernelName, fileName, lineNo, tensorizerId }
  ▼
bir::Instruction        (OpDebugInfo at Inst+0x960; unique_id +0x48; engine +0x90; dep edges at dep-block +0x20)
  │  DebugInfoWriter (this pass: enableNeffDebugInfo && arch≤49)
  ▼
neuroncc.debug_info.instruction_debug_info
  { id, name(truncated), parent_ids(tensorizer), dataflow/scheduling predecessors (id,kind),
    instruction_type REGULAR/SPILL/TRANSPOSE, attrs{file/line/func idx, dma_queue}, stack_frame_id }
  grouped per-engine into ir_debug_info
  { description, parent_debug_info_filename, engine_type (dword_1DF4B80 remap),
    kernel_filenames(f5), kernel_functions(f7), stack_frame_index(f6) }
  + an ASM-layer ir_debug_info (id-only, binary emit order)
  ▼
NEFF: debug_info_backend.<eng>.dbg / debug_info_asm.<eng>.dbg under getArtifactAbsPath(),
      registered via ModuleArtifactInfo::addEng{BIR,ASM}DebugInfoFile,
      linked by BirLinker::symLinkDebugInfoFiles → consumed by the Neuron profiler.
```

The three binaries play distinct roles, proven by RTTI/typeinfo identity: **`libwalrus.so` is the sole producer** (full generated method set + `DebugInfoWriter`); **`libBIR.so` is the bridge source** (defines `bir::OpDebugInfo` and `getDebugInfo`/`setDebugInfo`, references the 6 typeinfo names but does not build the messages); **`hlo2penguin` only links the classes** (its real provenance is the XLA/MHLO `StackFrameIndexBuilder` — a different generated proto family). The `stack_frame`/`file_location` name-symmetry between the two families is conceptual, not the same generated type.

The consumer (`neuron-explorer`, aws-neuronx-tools) statically links the **identical** schema compiled to Go and de-interns it: `tools_pkg_profiler.ingestStackFrameIndex` walks `parent_frame_id` to unwind the call stack, resolving each frame's `file_location → (file_names[file_name_id], function_names[function_name_id], line)`, and the kelf graph maps a program counter → `instruction_type` so the profiler can flag spills/transposes per PC. The 100-cap is exactly why the profiler "will not display the skipped dependencies" beyond the limit. The producer half of that story is read here; the consumer half comes from the Go-side schema, not from this binary.

---

## 6. Limits of this reading

The writer symbols, the engine remap table `{0,3,2,1,4,5,6,0}` at `0x1DF4B80`, the
`instruction_type` enum pool at `0x1e1e540`, the six `Arena::CreateMaybeMessage<neuroncc::debug_info::…>`
instantiations that fix the proto namespace as `neuroncc::` (not `neuronxcc::`), and the option
and layer-tag strings are all read directly off `libwalrus.so`.

Five things are softer:

- **The proto field numbers and wire tags, and the `_impl_` offsets.** Field *names* and the
  message-construction symbols are read here; the numeric tags come from the descriptor in
  `hlo2penguin`, which is the one non-stripped binary carrying them. The schema's shape is
  settled; a handful of tag values are inherited.
- **`canInstReadFromUninitMem` (`0x3fd7510`).** It does not decompile. The SPILL/TRANSPOSE
  selection is read from the calling body; the predicate's semantics are [INFERRED] from its name.
- **The producer store site of f8 `stack_frame_id`.** Its meaning is fixed by the descriptor and
  the consumer's de-intern; the exact write in the producer body was not isolated. [INFERRED]
- **The `arch_level <= 49` gate excluding CoreV5.** The immediate is read from the gate; the
  ArchLevel↔value mapping is external to this binary.
- **The `0x70000000` auto-flush threshold and the `truncateName` 509-byte cap.** Both come from
  the body trace rather than an independent re-read.
