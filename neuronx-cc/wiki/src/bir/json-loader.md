# The Two-Pass BIR-JSON Loader — `createFromJson` / `Pass2`

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310), `libBIR.so` md5 `12bb979f7ca41248252abb0f16b2da98`. cp311/cp312 VAs drift; the field offsets, dispatch structure, and string anchors are stable across them. VA == file offset for `.text` (0x1820c0–0x707a44) and `.rodata` (0x708000–0x7a118c). Most methods carry two symbol copies — a low-addr PLT/ICF thunk (the `0x176xxx`/`0x17axxx` twins) and the real high-addr body; every address cited here is the real body.*

## Abstract

A BIR module on disk is one `nlohmann::json` document (or its CBOR twin). Turning that document back into the live `Module → Function → BasicBlock → Instruction` object graph documented in [the container model](container-model.md) is **not** a single recursive descent, because the wire format is full of forward references: an instruction's `dependencies` list names instructions that may be defined later in the same array or in a sibling function; a DMA instruction names a queue declared at module scope after every function; a basic-block argument (BIR's φ-node) names predecessor blocks that may appear after the block that consumes their value. A one-pass loader would have to resolve a name to a pointer for an object that does not exist yet.

The loader solves this the way an assembler solves forward labels — **build first, resolve second**. Pass 1 (`createFromJson`, the family at `0x27ead0` / `0x2401e0` / `0x2f0c60` plus the recursive wirer `sub_27D150` @0x27d150) constructs every named object and inserts it into a name-keyed symbol table, in strict containment order, and resolves only the references that containment order *guarantees* are already built. Pass 2 (`createFromJsonPass2`, the family at `0x275e10` / `0x23de90` plus the recursive resolver `sub_273300` @0x273300) re-walks the same `functions` array and resolves exactly the cross-cutting set — typed `dependencies`, sync queues, DMA-trigger blocks, φ incoming-values, and the module-level `InstCall` argument bindings — by looking each wire name up in the symbol tables that pass 1 filled. The whole resolution model is name-keyed: every cross-object reference on the wire is a string resolved through a `std::_Hash_bytes(name, len, 0xC70F6907)` symtab. There are no numeric handles.

This page reconstructs both passes from the `from_json` bodies, names the real symbols and addresses, shows the symtab fill and the back-ref lookup, and proves the two-pass split — and *why* it must exist — directly from the binary. It is the inverse of the [BIR-JSON writer](json-writer.md) and consumes the structures minted by [the container model](container-model.md), [the Instruction base](instruction-base.md), and [MemoryLocation / Storage](memory-location.md).

| | |
|---|---|
| **Top driver** | `adl_serializer<bir::Module>::from_json` @ `0x48df10` (13116 B) |
| **Entry / format peek** | `bir::Module::load` @ `0x359850` → `loadJson` @ `0x359670` / `loadCbor` @ `0x359730` |
| **Pass-1 fn container** | `adl_serializer<NamedObjectContainer<FunctionHolder,Function>>::from_json` @ `0x4932c0` |
| **Pass-1 per-Function** | `bir::Function::createFromJson(name, FunctionHolder*, json)` @ `0x27ead0` |
| **Pass-1 wirer (recursive)** | `createFromJsonRecursively` = `sub_27D150` @ `0x27d150` (1689 lines decompiled) |
| **Pass-2 per-Function** | `bir::Function::createFromJsonPass2(Fn, Fn, json)` @ `0x275e10` |
| **Pass-2 resolver (recursive)** | `createFromJsonRecursivelyPass2` = `sub_273300` @ `0x273300` (11015 B) |
| **Pass-2 φ resolve** | `bir::BasicBlock::createFromJsonPass2(json)` @ `0x23de90` |
| **Symtab hash seed** | `0xC70F6907` (every `NamedObjectContainer`, fed to `std::_Hash_bytes`) |
| **Edge tag** | `PointerIntPair<Instruction*, 3, EdgeKind>` — `EdgeKind < 8` |

## Entry: `Module::load` and the one wire schema

`bir::Module::load(string& path)` @ `0x359850` opens the file, peeks the encoding, and dispatches to `loadJson` @ `0x359670` or `loadCbor` @ `0x359730`. Both parse the byte stream into one `nlohmann::json` and then call the **same** core — `adl_serializer<bir::Module>::from_json`. JSON and CBOR are therefore two encodings of one wire schema; the reconstructed object graph is encoding-independent. (The detection predicate itself is **[INFERRED]** — `loadJson`/`loadCbor` both feed the same driver, so the graph is provably encoding-independent, but the byte-level JSON-vs-CBOR sniff was not pinned.)

## The top driver — `adl_serializer<bir::Module>::from_json` @ `0x48df10`

This 13116-byte function *is* the loader; everything below is a callee. It reads the version int once, builds both function containers (pass 1), then re-walks `functions` for pass 2, and finally does the module-level `InstCall` binding. The construct/resolve boundary is a hard barrier: **all** objects of **all** functions are built before **any** function's pass 2 runs.

### Step D1 — version, and what it does *not* gate

The disassembly head is unambiguous about the version handling — and corrects a tempting misreading:

```text
0x48df15  lea  rsi, aVVersion+8                 ; "version" (7 chars + NUL)
0x48df3b  call basic_json::at<char const(&)[8]> ; fetch member "version"
0x48df51  call detail::from_json<…, uint, 0>    ; parse it as an unsigned int
0x48df56  mov  esi, [rsp+…var_138]              ; esi = the parsed uint
0x48df60  call bir::Module::setVersion(uint)    ; store on the Module
0x48df65  cmp  byte ptr [rbx], 1                ; *** json m_type == value_t::object?
0x48df68  jz   loc_48E160                       ;     NOT a version == 1 test
0x48df79  call adl<NamedObjectContainer<FunctionHolder,Function>>::from_json  ; <<< PASS 1
```

The Hex-Rays output renders the store as `Module::setVersion(a2, 0)` — that `0` is the decompiler losing the by-reference output buffer of the inlined `detail::from_json<…,uint>`; the disasm shows the real argument is the parsed uint moved into `esi` at `0x48df56`.
The `cmp byte ptr [rbx], 1` immediately after is **not** a version compare — `rbx` holds the json pointer and byte 0 is nlohmann's `value_t` discriminant, so this tests *"is the document a JSON object?"* before descending. The **only** thing the driver does with the version is `setVersion`. Its sole consumer in all of `libBIR` is the Pelican affine-expression deserializer (`QuasiAffineExpr::createFromJson` @ `0x3bd8d0` → `Module::getVersion`), which selects `fromJsonv1` vs `fromJsonv2`. Every other BIR layer is version-agnostic. (See [Version 1 vs 2](#version-1-old-vs-2-new--exactly-one-gated-codepath).)

### Step D2 — arch / archRev / hwm

Inside the `m_type == object` branch the driver reads `arch` (`ArchLevel`, key `[5]`) and `archRev` (`ArchRevision`, key `[9]`), builds a `bir::Arch`, and calls `Module::setArch`; for a scalar document it sets the `ArchLevel` directly. It then resolves the hardware model singleton — `bir::Hwm::getSingleton` (`0x48df10` body, near the tail at the `setHwm`/`setArtifactAbsPath` calls) — and stores it via `Module::setHwm`.
### Step D3/D4 — PASS 1: build the function containers

```c
v148 = (bir::FunctionHolder*)operator new(0x50u);
bir::FunctionHolder::FunctionHolder(v148, (bir::Module*)a2);
adl_serializer<NamedObjectContainer<FunctionHolder,Function>>::from_json();   // builds every Function
*(a2 + 560) = v148;                                                           // Module's FunctionHolder slot
…
adl_serializer<NamedObjectContainer<FunctionHolder,Function>>::from_json();   // again, for the NKI functions
```

The first container call (`0x48df79`) builds the ordinary functions; a second, unconditional call (`0x48e0xx` in the decompiled body) builds the NKI functions. Each iterates its `functions` array, and per entry constructs one `Function` (next section). The driver then reads module-level DMAQueues into `Module+80` via `adl_serializer<NamedObjectContainer<Module,DMAQueue>>::from_json`.
### Steps D6/D7 — PASS 2: re-walk and resolve

After every object exists, the driver walks the *same* `functions` array a second time and, for each entry, looks the function back up **by name** and runs its pass-2 resolver:

```c
while ( !iter_eq(it, end) ) {                          // for each entry in "functions"
    v8  = *it;                                         // the function's JSON
    v9  = entry["name"];                               // sub_483AC0 / sub_483BC0 extract the name string
    Fn  = bir::Module::getFunctionByName(a2, name);    // symtab lookup
    bir::Function::createFromJsonPass2(Fn, Fn, v8);    // <<< PASS 2 for this function
    ++it;
}
```

A second identical loop uses `getNKIFunctionByName` for the NKI functions. The re-lookup is what makes this a *resolve* pass: it does not reconstruct anything, it finds the already-built `Function` and hands it back its own JSON to resolve cross-cutting refs.
### Step D8 — the `"main"` InstCall binding (module-level pass 2)

The last block of the driver resolves the inter-function call bindings. For the `"main"` function (looked up via `getFunctionByName("main")` at `0x48e7xx`, with a fallback to the first function), it walks the call-argument list and resolves each name through *two* symtabs:

```c
sub_481450(&v392, "main");
caller = bir::Module::getFunctionByName(a2, "main");
…
I  = bir::Function::getInstructionByName(caller, callName);   // the InstCall target
target = llvm::cast<bir::InstCall>(I);                        // assert "cast<>() argument of incompatible type"
ml = bir::Function::getMemoryLocationByName(callee, argName); // the actual passed memloc
// assert "found" (NeuronAssertion code 62, Adapters/Json.cpp:195)
// result accumulated into DenseMap<InstCall const*, SetVector<MemoryLocation const*>>
```

This binds memlocs of one function as actual arguments of an `InstCall` in another — necessarily after both functions are fully built, which is why it lives in the module-level pass 2 rather than in `Function::createFromJson`. The `getInstructionByName`, `cast<InstCall>`, `getMemoryLocationByName`, and `"found"` assert sites are all present in the driver body (lines 808/908/1026/1048/1003 of the decompiled `0x48df10`).
## Pass 1 — construction and symbol-table fill

### The per-Function build order

`Function::createFromJson(name, FunctionHolder*, json)` @ `0x27ead0` failed Hex-Rays, so its build order is read from the disassembly call sequence — and it is exactly the containment order the symtabs need:

| Order | Call site | Callee | What it builds / fills |
|------:|-----------|--------|------------------------|
| (a) | `0x27eaf2` | `FunctionHolder::addFunction(name)` | registers this `Function` in the holder's **name→Function** symtab |
| (b) | `0x27eba7` | `Storage::createFromJson(name, Fn, json)` | per `storages` entry → `MemoryLocationSet` / `MemoryLocation` / `Register`; fills **name→Set**, per-Set **name→MemLoc**, **name→Register** |
| (c) | `0x27ebef` | `addMemLocAliasesFromJson(Fn, json)` | DRAM alias graph — needs **all** memlocs from (b) |
| (d) | `0x27ebfa` | `createMemLocAddrLocFromJson(Fn, json)` | memloc address-location wiring (reads `getMemoryLocationByName`) |
| (e) | `0x27ecbf` | `BasicBlock::createFromJson(name, BasicBlockHolder*, json)` | per `basic_blocks` entry → fills **name→BasicBlock**, then per `instructions` entry → **name→Instruction** |
| (f) | `0x27ed26` | `createFromJsonRecursively` = `sub_27D150` | intra-function dep edges + structured-control recursion |

Because storages (b) precede blocks/instructions (e), and (e) precedes the recursive wirer (f), **every** memloc, set, register, block, and instruction is name-addressable by the time (f) runs. That is the invariant the whole loader rests on. *Anchor: disasm of `0x27ead0`.*

### What the symbol table *is*

`addFunction` (`0x176470` thunk → real body `0x290710`) is one line: `NamedObjectContainer<FunctionHolder,Function>::insertElement<Function>(…)`. The container is the intrusive-list + `unordered_map<string,T*>` documented in [the container model](container-model.md); the map is the symtab. Its hash is fixed:

```c
// bir::BasicBlock::getInstructionByName @0x23ce80 — the lookup half, in full
v2 = std::_Hash_bytes(name.data, name.size, 0xC70F6907);     // FNV-seeded bucket hash
…
if ( v8 == v5[2] && (!v8 || !memcmp(name.data, v5[1], v8)) ) // length match + byte compare
    return *(elem);                                          // name → Instruction*
```

Every `getXByName` in the loader is this shape: hash the wire name with seed `0xC70F6907`, walk the bucket, `memcmp` the key. The `0xC70F6907` seed is identical across `getInstructionByName`, `BasicBlock::createFromJsonPass2`, and `NamedObjectContainer::insertElement` — i.e. one symtab discipline for the whole tree.
### Building an Instruction and minting its name

`Instruction::createFromJson(name, BasicBlock*, json)` @ `0x2f0c60` (decompiled) drives the per-instruction build:

```c
v9 = bir::Instruction::createFromJsonHelper();              // opcode→concrete subclass factory (0x17aec0 thunk)
adl_serializer<bir::InstructionType>::from_json(…, v9 + 88, …); // set the opcode at +0x58 (88 == 0x58)
…
for (each operand)  bir::Argument::createFromJson((Instruction*)v9);  // build AP / immediate operand
```

`createFromJsonHelper` is the *kind-string → constructor* factory: it reads the `instruction_type` opcode and `new`s the matching `Instruction` subclass, then the subclass's `readFieldsFromJson` reads its op-specific fields at `+0xF0…`. (This is the same dispatch shape the [value model](value-model.md) page documents for `Argument::createFromJson`, where the `kind` string selects Physical / Symbolic / Register / immediate.) The fact that the `InstructionType` `from_json` writes into `helper_result + 88` confirms the opcode lives at struct `+0x58`, matching [InstructionType](instruction-type.md). [INFERRED] The exact subclass jump table inside the helper: the helper body resolves to a thunk in this corpus, and the per-opcode field readers are owned by the [Instruction base](instruction-base.md) page. The dispatch *shape* above is read from the driver.

`PhysicalAccessPattern::setLocation` binds an operand to its `StorageBase` **here, in pass 1** — the memloc already exists from step (b), so an operand→location reference is *not* a forward reference. This is decisive for why operands need no pass-2 resolution.

### Pass-1 edge wiring — `createFromJsonRecursively` (`sub_27D150` @ `0x27d150`)

This runs at the tail of `Function::createFromJson`, once every block and instruction in the (sub-)holder exists. It walks `json["blocks"]` (the literal `"blocks"` is read at line 218), resolves each block by name in the holder, then walks `json["instructions"]` (line 309) and wires the **order-safe** intra-function references:

* **`loop_carried_dependencies`** (array; assert `'loop_carried_dependencies' must be a JSON array`): `getInstructionByName(Fn, depName)` (line 676) → assert `Dep && "Unknown loop-carried dependency"` (line 681) → `addDependency(I, dep, 4, 1)` (line 685) — `EdgeKind = 4` (Flow), loop-carried flag `1`.
* **dataflow predecessors / general intra-region deps**: `string2EdgeKind` (lines 743/902) maps the wire `kind` string to the `EdgeKind` enum, then `addDependency` packs it into the `PointerIntPair` (asserts `(PtrWord & ~PointerBitMask)==0` and `(Int & ~IntMask)==0`, lines 908/915 — the 3-bit edge tag).
* **control-flow successors** resolved by name in the holder: `onTrue` / `onFalse` / `target`, each guarded by `"<key> && BasicBlock does not exist!"` (lines 1096 / 1298 / 1530).
* **structured-control recursion** (line 972): switch on the opcode `*((_DWORD*)I + 22)` — `22*4 == 0x58`, the `InstructionType` field. `case 'i'` (== 105, `Loop`) descends into the nested block body via `sub_27D150(Fn, I - 11, bodyHolder)` (line 973), the `-11` being the offset from the `Instruction` to its embedded `BasicBlockHolder`; `case 'N'` (== 78, the branch family) handles its successor edges.

These edges are order-safe because both endpoints live in the same function and have been built by step (e). They are wired in pass 1 *precisely because* they need no forward lookup.
## Pass 2 — forward-reference resolution

### Per-Function driver — `createFromJsonPass2` @ `0x275e10`

This function also failed Hex-Rays; the disasm shows two calls and nothing else of substance:

```text
0x275e3a  call sub_273300                       ; createFromJsonRecursivelyPass2 (deps/queues/triggers)
…
0x276023  call bir::BasicBlock::createFromJsonPass2 ; per block: φ incoming-value resolution
```

So a function's pass 2 is exactly *"resolve the cross-cutting instruction refs, then resolve the block-argument φ-nodes."*
### The resolver — `createFromJsonRecursivelyPass2` (`sub_273300` @ `0x273300`)

It re-walks `"blocks"` → `NamedObjectContainer<BasicBlockHolder,BasicBlock>::getElementByName` (line 305) → each instruction, and dispatches on the opcode `v35 = *((_DWORD*)I + 22)` (`+0x58` again). The arms are the entire forward-reference set:

**Queue back-ref (DMA / collective family).** Gated by `(unsigned)(v35 - 19) <= 0x30 && byte_783700[v35 - 19]` for `IT ∈ [19, 0x49]`, plus `IT == 85` (a collective), the resolver reads the instruction's queue **name** and binds it:

```c
QueueByName = bir::Module::getQueueByName(module, name);     // line 501
bir::reportError(QueueByName != 0, …, "Queue does not exist!");  // line 507/508
v34[30] = QueueByName;                                        // store at I+0xF0  (30*8 == 0xF0)
```

The queue pointer lands at `Instruction + 0xF0` (`v34[30]`), matching the InstDMA/collective layout.
**DMA-trigger back-ref (`IT == 67`, `InstDMATrigger`).** After asserting the trigger already has its queue (`q && "Queue does not exist!"`), it iterates `json["dma_blocks"]` and, per block name, calls `DMAQueue::findDMABlock` (line 566) → `reportError(blk != 0, …, "Block does not exist!")` (line 571) → `InstDMABlock::setTrigger(blk, this)` (line 586) — the forward trigger↔block link.
**General typed `dependencies`.** Assert `JsonDeps.is_array() && "'dependencies' must be a JSON array"` (line 791), then per entry (decompiled lines 818–872, disasm `0x274fba`/`0x275156`):

```c
if ( entry is [name, kind] ) {                               // *entry == 2 (two-element array)
    dep  = Function::getInstructionByName(Fn, name);         // reportError "Unknown dependency" (id 495)
    kind = (entry[1] is string) ? string2EdgeKind(entry[1])
                                : from_json<int>(entry[1]);   // numeric kinds 5..7 coerce via to_string
    // pack into PointerIntPair<Instruction*,3,EdgeKind>:
    assert (dep & 7) == 0;                                   // "Pointer is not sufficiently aligned"
    assert (kind & 0xF8) == 0;                               // "Integer too large for field" (kind < 8)
    Instruction::addDependency(I, dep, kind);                // EdgePtr overload (0x275156)
} else {                                                     // bare string
    dep  = Function::getInstructionByName(Fn, name);         // reportError "Unknown dependency" (id 507)
    Instruction::addDependency(I, dep, /*EdgeKind=*/4, 0);   // DEFAULT == Flow (line 872)
}
```

So a `dependencies` entry is **either** a bare name (⇒ `EdgeKind = Flow = 4`) **or** a `[name, kind]` pair (⇒ the typed `EdgeKind`, `string2EdgeKind`-mapped, or a numeric 5..7). The `PointerIntPair<Instruction*, 3, EdgeKind>` with `IntBits = 3` is verbatim in the alignment/overflow asserts (lines 862–874), so the edge tag is a 3-bit field and `EdgeKind` must be `< 8`.
**Predicated dependencies.** A richer arm builds `std::tuple<Instruction*, EdgeKind, vector<QuasiAffineExpr>, vector<AffinePredicate>>` per entry: `getInstructionByName` + `string2EdgeKind`, then `QuasiAffineExpr(getPelicanContext())` + `createFromJson` (the version-gated Pelican path, lines 1267–1269) and `AffinePredicate::createFromJson`, appended to the instruction's predicated-dependency vector via `_M_realloc_insert` (line 1434).
**Recursion** descends into nested control-flow block bodies via the same `getElementByName` + self-call (resolving the pass-2 set), mirroring the pass-1 recursion.
### φ-node resolution — `BasicBlock::createFromJsonPass2` @ `0x23de90`

BIR block arguments are φ-nodes; their incoming `(value, predecessor-block)` pairs are resolved here because a predecessor may be defined after the block that consumes its value:

```c
for (each block argument) {
    bir::BasicBlockArgument::setLocation(arg, storage);          // bind arg to its StorageBase (line 286)
    for (each entry in json["incoming_values"]) {                // line 329
        pred = bir::Function::getBasicBlockByName(Fn, predName); // line 391
        if ( value != 0 && pred != 0 )
            bir::BasicBlockArgument::addIncomingValue(arg, value, pred);  // line 472
    }
}
```

The `getBasicBlockByName` lookup is the forward/back block reference; `addIncomingValue` stores the φ pair.
## Why two passes — proven from the binary

> **GOTCHA — the second pass exists *only* for forward and sideways references, and the binary marks each one with a "does not exist" guard.** The references resolved in pass 2 — `dependencies`, `getQueueByName`, `findDMABlock`, φ `getBasicBlockByName`, the `InstCall` `getMemoryLocationByName` — can all name an object defined **later** in the document or in a **sibling** block/function. A queue is declared at module scope *after* every function (driver step D5, after D3/D4). A φ-node's predecessor block may be emitted after the block that uses its value. A `dependency` may point forward in the instruction stream. None of these are resolvable when the consuming instruction is first constructed.
>
> The proof is the guard set: every pass-2 lookup is wrapped in a *missing-reference* check — `reportError(p != 0, …, "Queue does not exist!")`, `"Block does not exist!"`, `"Unknown dependency"`, the φ `value != 0 && pred != 0` test, the `InstCall` `"found"` assert (NeuronAssertion 62). These checks can only succeed *after* driver step D6, because only then does **every** object in **every** function exist. Pass 1's intra-region edges (`loop_carried_dependencies`, dataflow predecessors, `onTrue`/`onFalse`/`target`) carry *no* such cross-function guard — their endpoints are provably built by containment order, so they are resolved in place. The construct/resolve barrier between D5 and D6 is the line that makes the missing-reference guards meaningful.
The split is also visible in the *shape* of the two recursive functions: `sub_27D150` (pass 1) and `sub_273300` (pass 2) walk the **same** `blocks`/`instructions` structure and dispatch on the **same** opcode field (`+0x58`), but pass 1 only ever resolves names through the *current function's* symtabs (`getInstructionByName(Fn, …)`), while pass 2 additionally reaches `Module::getQueueByName`, `DMAQueue::findDMABlock`, and `Function::getBasicBlockByName` — the module- and sibling-scoped tables. The pass-1 recursion is the structural descent that *materialises* the nested blocks of `InstLoop` / `InstDMABlock` bodies; the pass-2 recursion re-descends the now-complete tree to wire their cross-cutting edges.

## Version 1 (old) vs 2 (new) — exactly one gated codepath

The module-level version int gates precisely one thing. `Module::getVersion` (`0x354e90`) has a single caller in `libBIR`: `QuasiAffineExpr::createFromJson` @ `0x3bd8d0`.

```text
0x3bd8ee  call bir::Module::getVersion()       ; ebp = version
0x3bd900  cmp  ebp, 2 ; jz fromJsonv2           ; version 2 → new Pelican schema
0x3bd909  cmp  ebp, 1 ; jnz error               ; not 1 and not 2 → fatal
0x3bd91e  call fromJsonv1(...)                  ; version 1 → old Pelican schema
0x3bd9be  lea  rdi, "Check the version of bir json file - should be 1 (old) or 2 (new)"  ; fatal on ∉{1,2}
```

So the on-wire encoding of affine address expressions (the `RefPtr<Expr>` tree and the `LoopAxis` `addrs` vector) is the *only* version-keyed part of the BIR document.
The memloc-alias loader has a *separate* "old vs new" axis that is **not** version-keyed: `addMemLocAliasesFromJson` @ `0x26f020` calls `getVersion` **zero** times and selects schema by JSON **shape** — the *set-keyed* form has `allocation["memorylocations"]` with `alias` entries that are 3-tuples `[destSet, destMemLoc, kind]` (resolved via `getMemoryLocationSetByName` → `MemoryLocationSet::getMemoryLocationByName`), versus the *memloc-keyed* form whose `alias` entries are 2-tuples `[destMemLoc, kind]` (resolved via `Function::getMemoryLocationByName`). The two "old/new" notions — numeric version 1/2 for Pelican, structural shape for aliases — are independent.
## Error and fatal paths

Two mechanisms surface, and they map cleanly onto the two failure modes (malformed document vs missing reference):

| Mechanism | Trigger | Examples |
|-----------|---------|----------|
| nlohmann `json` exceptions | structurally malformed document | `type_error` `0x12E` (wrong type), `out_of_range` `0x193`, `"cannot get value"` `0xD6` |
| `reportError` + `NeuronAssertion<ErrorCode>` + `__assert_fail` | semantic missing-reference | see below |

The semantic guards, with their source anchors:

| String | Pass | Anchor |
|--------|------|--------|
| `'loop_carried_dependencies' must be a JSON array` | 1 | `Function.cpp:0x173` |
| `Dep && "Unknown loop-carried dependency"` | 1 | `Function.cpp:0x186` |
| `<key> && "BasicBlock does not exist!"` (onTrue/onFalse/target) | 1 | `sub_27D150` 1096/1298/1530 |
| `'dependencies' must be a JSON array` | 2 | `Function.cpp:0x1EA` |
| `"Unknown dependency"` | 2 | reportError ids 495 / 507 |
| `"Queue does not exist!"` | 2 | reportError id 466-class |
| `DMATrigger must have queue and block set` | 2 | `Function.cpp:0x1CF` |
| `"Block does not exist!"` | 2 | reportError |
| `nki_func.empty() || (nki_func.size()==1 && nki_func.contains(Function::NAME))` | driver | `Adapters/Json.cpp:140`, NeuronAssertion 73 |
| `"found"` (InstCall arg) | driver D8 | `Adapters/Json.cpp:195`, NeuronAssertion 62 |
| `Check the version of bir json file - should be 1 (old) or 2 (new)` | Pelican | `0x741110`, fatal on version ∉ {1,2} |
| `(PtrWord & ~PointerBitMask)==0` / `(Int & ~IntMask)==0` | every `addDependency` | `PointerIntPair.h`, `IntBits = 3` ⇒ `EdgeKind < 8` |

Every `NeuronAssertion` emits the standard footer (`Please open a support ticket at https://github.com/aws-neuron/aws-neuron-sdk/issues/new …`), present verbatim in the driver body.
## Reconstructed algorithm

```text
loadModule(json M):
  version = M["version"];  module.setVersion(version)          # gates Pelican expr ONLY
  if M.is_object():
    setArch(M["arch"], M["archRev"]);  setHwm;  setArtifactAbsPath

  # ===== PASS 1 — build everything; resolve order-safe refs =====
  for f in M["functions"] (+ NKI "functions"):                 # adl<NamedObjectContainer<FunctionHolder,Function>>
     Fn = FunctionHolder.addFunction(f["name"])                # insertElement → name→Fn symtab (seed 0xC70F6907)
     for s in f["storages"]:                                   # Storage::createFromJson @0x27eba7
        set/memloc/register built → name→Set / name→MemLoc / name→Register
     addMemLocAliasesFromJson(Fn, f)                           # alias graph (shape-keyed v1/v2)
     createMemLocAddrLocFromJson(Fn, f)
     for b in f["basic_blocks"]:                               # BasicBlock::createFromJson @0x2401e0
        BB = BasicBlockHolder.addBasicBlock(b["name"])         # name→BB symtab
        for i in b["instructions"]:                            # Instruction::createFromJson @0x2f0c60
           I = createFromJsonHelper(opcode)                    # opcode→subclass; IT at I+0x58
           readFieldsFromJson(I) ; addInstruction(I)           # name→Instruction symtab
           for op in operands: Argument::createFromJson(op)    # setLocation binds NOW (memloc exists)
        read φ-args (incoming values deferred to pass 2)
     createFromJsonRecursively(Fn, holder, f)                  # sub_27D150: loop_carried + dataflow + ctrl, recurse
  read module DMAQueues into Module+80

  # ===== PASS 2 — resolve forward / cross-cutting refs =====
  for f in M["functions"] (+ NKI):
     Fn = Module.getFunctionByName(f["name"])                  # re-lookup, do NOT rebuild
     createFromJsonRecursivelyPass2(Fn, holder, f):            # sub_273300, dispatch on I+0x58
        DMA/collective: I[+0xF0] = Module.getQueueByName(name)         # "Queue does not exist!"
        DMATrigger:     for db in "dma_blocks": setTrigger(queue.findDMABlock(db), I)
        "dependencies": for d in deps:
                           dep  = Fn.getInstructionByName(d.name)       # "Unknown dependency"
                           kind = (d is [name,k]) ? string2EdgeKind(k) : FLOW(4)
                           I.addDependency(dep, kind)                   # EdgeKind in 3-bit PointerIntPair tag
        predicated deps: build (dep, kind, [QuasiAffineExpr], [AffinePredicate])
     for b in f.blocks: BasicBlock::createFromJsonPass2(b):
        for arg in BB.args:
           arg.setLocation(storageByName)
           for (val, predName) in arg.incoming_values:
              arg.addIncomingValue(val, Fn.getBasicBlockByName(predName))

  # module InstCall binding for "main": getInstructionByName + getMemoryLocationByName  ("found")
```

## Function map

| Function | Addr | Role | Decompile |
|----------|------|------|-----------|
| `bir::Module::load(string&)` | `0x359850` | path → ifstream → loadJson/loadCbor | ok |
| `bir::Module::loadJson` / `loadCbor` | `0x359670` / `0x359730` | parse → `adl<Module>::from_json` | ok |
| `adl_serializer<bir::Module>::from_json` | `0x48df10` | **top driver**: version + both passes + InstCall | ok |
| `bir::Module::setVersion` / `getVersion` | `0x354ea0` / `0x354e90` | version field (sole consumer: Pelican) | ok |
| `adl<NamedObjectContainer<FunctionHolder,Function>>::from_json` | `0x4932c0` | pass-1 fn container (per-fn ctor) | ok |
| `bir::Function::createFromJson` | `0x27ead0` | pass-1 per-Function builder | failed → disasm |
| `bir::Storage::createFromJson` | `0x3cc920` | builds Set / MemLoc / Register | ok |
| `addMemLocAliasesFromJson` | `0x26f020` | DRAM alias graph (shape-keyed) | ok |
| `bir::BasicBlock::createFromJson` | `0x2401e0` | pass-1 BB + instr container | ok |
| `bir::Instruction::createFromJson` | `0x2f0c60` | pass-1 per-Instruction builder | ok |
| `bir::Instruction::createFromJsonHelper` | `0x17aec0` (thunk) | opcode→subclass factory | thunk |
| `createFromJsonRecursively` | `0x27d150` | pass-1 dep wiring + ctrl recursion | ok |
| `bir::Function::createFromJsonPass2` | `0x275e10` | pass-2 per-Function driver | failed → disasm |
| `createFromJsonRecursivelyPass2` | `0x273300` | pass-2 dep / queue / trigger resolver | ok |
| `bir::BasicBlock::createFromJsonPass2` | `0x23de90` | pass-2 φ incoming-value resolver | ok |
| `bir::QuasiAffineExpr::createFromJson` | `0x3bd8d0` | Pelican v1/v2 dispatch (sole version use) | failed → disasm |
| `bir::BasicBlock::getInstructionByName` | `0x23ce80` | symtab lookup exemplar (seed `0xC70F6907`) | ok |
| `bir::string2EdgeKind` | — | wire `kind` string → `EdgeKind` | — |
| `bir::Instruction::addDependency` | — | typed dep edge (`PointerIntPair<…,3,EdgeKind>`) | — |

## Cross-references

* [NamedObject / Container Model](container-model.md) — the `ilist + unordered_map<string,T*>` symtab (seed `0xC70F6907`) this loader fills and queries; the `getUniqueId` id cascade the `insertElement` mints feed.
* [The `bir::Instruction` Base Struct](instruction-base.md) — the `+0x58` opcode field this loader dispatches on, the `+0xD0` sched/dep block the edges land in, and the per-opcode `readFieldsFromJson` readers.
* [InstructionType](instruction-type.md) — the 110-opcode enum read into `+0x58`; the `IT ∈ [19,0x49]` / `67` / `85` gates the pass-2 resolver keys on.
* [Argument / AccessPattern / Immediate / Register Value Model](value-model.md) — the `kind`-string → operand-subclass dispatch this loader reuses for instructions, and the `setLocation` operand binding done in pass 1.
* [MemoryLocation / Storage & the Alias Model](memory-location.md) — the Storage family built in step (b) and the shape-keyed alias schema of `addMemLocAliasesFromJson`.
* [The BIR-JSON Writer](json-writer.md) *(7.13)* — the `to_json` inverse this page deserializes; the 19-key header and `PointerIntPair` edge model it emits.
* [The BIR-JSON Schema](json-schema-catalog.md) *(7.14)* — the full member roster and v1/v2 wire-key catalog.
* [NEFF members](../formats/neff-json-sidecars.md) *(12.3, planned)* — where the BIR-JSON document sits inside the NEFF tar.
