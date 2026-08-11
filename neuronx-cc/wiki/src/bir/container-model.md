# NamedObject and the Container Model

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310), `libBIR.so` md5 `12bb979f7ca41248252abb0f16b2da98`. cp311/cp312 VAs drift; the field offsets, template shapes, and ABI are stable across them. VA == file offset for `.text` (0x1820c0–0x707a44) and `.rodata` (0x708000–0x7a118c); `.data.rel.ro` (vtables/typeinfo) carries a −0x1000 delta (VA 0x8fcd40 == file off 0x8fcc40).*

## Abstract

Every named object in BIR — the whole program down to a single instruction — is built from exactly two C++ templates that `libBIR.so` instantiates six times each. `bir::NamedObject<T, Cont>` is the CRTP mixin that gives an IR node its name, its globally-unique id, its origin tag, and a back-pointer to whatever owns it. `bir::NamedObjectContainer<Cont, T>` is the owning structure: an intrusive doubly-linked list (insertion order) fused with an `unordered_map<string, T*>` (name lookup). The two templates compose into the four-level program tree **Module → Function → BasicBlock → Instruction**, with two side-trees hanging off it (Storage/MemoryLocation under Function, DMAQueue under Module).

If you know LLVM, the closest analogue is `Value`/`ilist` plus `SymbolTable`: a `Module` owns `Function`s, a `Function` owns `BasicBlock`s, a `BasicBlock` owns `Instruction`s, and each child carries a parent pointer. BIR differs in three reimplementation-relevant ways. First, the parent link, the name, and the id are not bespoke per level — they are a single CRTP sub-object stamped identically into every node, so one struct layout serves all six instantiated types. Second, id allocation is not per-container: a getUniqueId call walks the owner back-pointers up the tree and post-increments **one** `uint32` counter living at `Module+0x98`, so ids are globally unique across functions, blocks, instructions, storages, memory-locations, and DMA queues alike. Third, the container is not a separate node-pool — `NamedObject<T,Cont>` *is* the `llvm::ilist_node_with_parent`, so the list threads the node sub-objects directly with no auxiliary allocation.

This page reconstructs both templates field-by-field, proves the CRTP relationship from the `__vmi_class_type_info` base lists, traces the id-allocator cascade through all six `getUniqueId` bodies, and lays out the ownership graph. The struct layouts of the leaf types are owned by sibling pages: see [Instruction Base](instruction-base.md) for the `Instruction` body, [Memory Location and Storage](memory-location.md) for the Storage family, and the [BIR JSON Loader](json-loader.md) for how the two-pass deserializer drives the `from_json`/`insertElement` mints documented here.

For reimplementation, the contract is:

- **The CRTP shape** — `T : public NamedObject<T, Cont> : { NamedObjectBase, llvm::ilist_node_with_parent<T, Cont> }`, with the sub-object field offsets (name, origin, optin-mask, id, owner) that are identical across all six instantiations.
- **The container layout** — `ilist<T>` sentinel + `unordered_map<string,T*>` (fixed hash seed `0xC70F6907`, 1.0 max-load-factor, inline single-bucket SSO) + owner back-pointer.
- **The id-allocator cascade** — the chain of owner-pointer dereferences and fixed sub-object deltas (`+0x48`, `+0xA0`, `+0x118`) that bottoms out at the single `Module+0x98` counter.
- **The ownership graph** — which of the six containers owns which of the six element types, and the two side-trees.

| | |
|---|---|
| **Defining binary** | `neuronxcc/starfish/lib/libBIR.so` (22151 fns; template insts are weak `W` in `.dynsym`) |
| **`NamedObjectBase` typeinfo** | `_ZTI` @0x8fc230, `_ZTS` @0x782a10 — the one polymorphic root |
| **CRTP base shape** | `__vmi`: `{ NamedObjectBase @sub+0x00, ilist_node_with_parent<T,Cont> @sub+0x10 }` |
| **Global id counter** | `Module+0x98` (`uint32`, monotone, every getUniqueId bottoms out here) |
| **Container symtab seed** | `0xC70F6907` (fixed, every BIR symboltable) — fed to `std::_Hash_bytes` |
| **Instantiation count** | 6 × `NamedObject<T,Cont>`, 6 × `NamedObjectContainer<Cont,T>` |
| **`getElementByName`** | @0x282030 (`<BasicBlockHolder,BasicBlock>` exemplar, 183 bytes) |

---

## The NamedObject CRTP

### Purpose

`NamedObject<T, Cont>` is the universal "named IR object" mixin. It is the single place that defines what it means for an object to *have a name and an identity inside a container*: the name string, a global unique id, a producer-origin tag, a per-object pass-opt-in mask, and a back-pointer to the owning `Cont`. The owning leaf type `T` derives from `NamedObject<T, Cont>` with itself as the first template argument — the classic CRTP self-type — so a single template body services `Instruction`, `BasicBlock`, `Function`, `DMAQueue`, `Storage`, and `MemoryLocation` without per-type code.

### The CRTP relationship, proven

The self-derivation is not inferred from naming — it is in the `__vmi_class_type_info` base lists. The `Instruction` typeinfo `_ZTIN3bir11InstructionE` @0x8fcd78 is a `__vmi` record whose first base reloc (struct offset 0x8fcd90) points at `_ZTIN3bir11NamedObjectINS_11InstructionENS_10BasicBlockEEE` @0x8fcd40, and whose second base (0x8fcda0) is `_ZTIN7logging9SrcHandleE`. So `Instruction : NamedObject<Instruction, BasicBlock>, logging::SrcHandle`, read from the `readelf -rW` relocation table rather than from any naming convention.

That `NamedObject<Instruction, BasicBlock>` typeinfo @0x8fcd40 is itself a `__vmi` with exactly two bases:

```text
_ZTI NamedObject<Instruction,BasicBlock> @0x8fcd40   (__vmi_class_type_info)
  base @struct+0x28 (0x8fcd68) → _ZTIN3bir15NamedObjectBaseE                         @0x8fc230   (offset 0x00)
  base @struct+0x18 (0x8fcd58) → _ZTIN4llvm22ilist_node_with_parentI…Instruction…BasicBlock…EE @0x8fcd28   (offset 0x10)
```

So `NamedObject<T, Cont> : public NamedObjectBase, public llvm::ilist_node_with_parent<T, Cont>` — and the second base sits at sub-object offset `+0x10`, which means **the `NamedObject` *is* the intrusive list node**. The container's `ilist` threads these node sub-objects directly; there is no separate list-node allocation. The same two-base shape holds for `<DMAQueue,Module>` (_ZTI @0x8fc258), `<Function,FunctionHolder>` (@0x8fc630), `<Storage,Function>` (@0x9000a8), and `<MemoryLocation,MemoryLocationSet>` (@0x8fdb38), each with the identical two-base `__vmi` reloc pattern.

> **QUIRK —** `NamedObjectBase` (typeinfo @0x8fc230) is the *only* `NamedObject` node with its own standalone polymorphic identity. `NamedObject<T,Cont>` carries no vptr of its own — the `+0x00` base header is a non-virtual `NamedObjectBase` sub-object, ctor-zeroed. `NamedObjectBase` is where the per-object opt-in machinery lives: `NamedObjectBase::optinPassesFromJson<Instruction*>` @0x2f95c0, `optinPassesFromJson<MemoryLocation*>` @0x3411f0, `optinPassesToJson` @0x3a01d0. The id, by contrast, lives in the templated `NamedObject<T,Cont>` proper — `NamedObjectBase` has no `getUniqueId`.

### Sub-object layout

Offsets are relative to the `NamedObject<T, Cont>` sub-object base. The sub-object sits at a different absolute offset inside each leaf (`Instruction+0x08`, `BasicBlock+0x48`, `Function+0xA0`), but the *relative* field layout is byte-identical everywhere — proven by every `getUniqueId` body opening with `mov …, [rdi+0x48]` and by the leaf ctors writing name at sub+0x18, id at sub+0x40.

| Field | Off | Type | Meaning / JSON key | Confidence |
|---|---|---|---|---|
| `NamedObjectBase` header | +0x00 | (non-virtual base, no vptr) | optin/origin anchor; ctor-zeroed | CERTAIN |
| `ilist_node` prev / next | +0x10 | `T*` / `T*` | intrusive list links (this node) | CERTAIN |
| `name` | +0x18 | `std::string` (data@+0x18, size@+0x20, SSO@+0x28) | object name (SSO inline) | CERTAIN |
| `origin` | +0x30 | `int` `NamedObjectOrigin` | `"origin"` `{Internal=0, Penguin=1, NKI=2}` | CERTAIN |
| `optin_passes_mask` | +0x38 | qword (default `0xF`) | `"optin_passes"` bitmask | HIGH |
| `unique_id` | +0x40 | `int` (`-1` sentinel pre-getId) | object id (getUniqueId fills it) | CERTAIN |
| owner-container ptr | +0x48 | `Cont*` | back-pointer to owning container | CERTAIN |

The `NamedObjectOrigin` enum and the `optin_passes` key appear as literal strings in the binary (`Unknown NamedObjectOrigin`, the `StringSwitch<bir::NamedObjectOrigin>` symbol, and the `optin_passes`/`origin`/`Penguin` rodata strings). The `-1` id sentinel shows up in the leaf ctors, which pre-set `unique_id = -1` then calling `getUniqueId`; if the field is *not* `-1` afterward the ctor throws the (mis-spelled) `Id has already been initialzed` — a string that is byte-exact in the binary.

> **NOTE —** the `name`, `origin`, `optin_passes_mask`, and `unique_id` are written contiguously by the leaf ctor; whether `name`/`origin` physically belong to `NamedObjectBase` or to `NamedObject<T,Cont>` proper is not separated by a dedicated `NamedObjectBase` ctor disasm. The offsets themselves are read off the disassembly; the *partition* between base and derived is a reconstruction resting on the unified `+0x18` name offset across all instantiations.

---

## The id-allocator cascade

### Purpose

BIR ids are globally unique across the entire object graph, not per-container. There is exactly one counter — a `uint32` at `Module+0x98` — and every named object's id comes from post-incrementing it. The mechanism is a delegation cascade: each `getUniqueId` reads its owner back-pointer (`[this+0x48]`), and either bumps the Module counter directly (if the owner *is* the Module) or jumps to the owner's own `getUniqueId` after adjusting `this` to point at the owner's `NamedObject` sub-object. The cascade is the strongest single confirmation that the `+0x48` slot is the universal owner pointer, because every body dereferences exactly that slot.

### Algorithm

Each of the six bodies is tiny (13–40 bytes) and is reproduced below from its disassembly. The "delta" added before the tail-jump is the absolute offset of the owner's `NamedObject` sub-object inside the owner.

```c
// NamedObject<DMAQueue,Module>::getUniqueId           @0x256b40 (20 bytes) — the BASE case
function getUniqueId_DMAQueue(this):
    Module* m = *(this + 0x48);            // owner is the Module directly
    uint32 id = m->counter;                // [m + 0x98]
    m->counter = id + 1;                   // post-increment
    return id;

// NamedObject<Function,FunctionHolder>::getUniqueId   @0x26c9c0 (40 bytes)
function getUniqueId_Function(this):
    FunctionHolder* h = *(this + 0x48);    // owner is a FunctionHolder
    Module* m = *(h + 0x48);               // FunctionHolder's own owner slot → Module
    if (m == nullptr): throw …             // sub_26B060 — log + fatal on detached function
    uint32 id = m->counter;                // [m + 0x98]
    m->counter = id + 1;
    return id;

// NamedObject<Instruction,BasicBlock>::getUniqueId    @0x2d5be0 (13 bytes)
function getUniqueId_Instruction(this):
    BasicBlock* bb = *(this + 0x48);
    tailcall getUniqueId_BasicBlock(bb + 0x48);   // BB's NamedObject sub-object @BB+0x48

// NamedObject<BasicBlock,BasicBlockHolder>::getUniqueId @0x23c220
function getUniqueId_BasicBlock(this):
    BasicBlockHolder* h = *(this + 0x48);
    tailcall BasicBlockHolder::getUniqueId(h);    // @0x24b4a0 → getFunction → getUniqueId_Function

// NamedObject<Storage,Function>::getUniqueId          @0x3ca030 (16 bytes)
function getUniqueId_Storage(this):
    Function* f = *(this + 0x48);
    tailcall getUniqueId_Function(f + 0xA0);      // Function's NamedObject sub-object @Function+0xA0

// NamedObject<MemoryLocation,MemoryLocationSet>::getUniqueId @0x32dab0 (16 bytes)
function getUniqueId_MemoryLocation(this):
    MemoryLocationSet* mls = *(this + 0x48);
    tailcall getUniqueId_Storage(mls + 0x118);    // MLS's NamedObject<Storage,Function> sub-object @MLS+0x118
```

Every leaf therefore reaches the same `Module+0x98` counter by a chain of `+0x48` owner-dereferences. The fixed deltas — `+0x48` (Instruction→BB's NamedObject), `+0xA0` (Storage→Function's NamedObject), `+0x118` (MemoryLocation→MLS's `NamedObject<Storage,Function>`) — are byte-exact `add` constants in the disassembly and independently pin the absolute position of each owner's sub-object.

> **GOTCHA —** the Function cascade has a null-check the others lack (`@0x26c9c0` tests `[h+0x48]` and tail-calls a fatal `sub_26B060` on null). A `Function` whose `FunctionHolder` has no `Module` owner — a detached/orphaned function — will hard-fault on id assignment rather than silently producing id 0. A reimplementation that lets functions exist outside a Module will diverge here.

### Function map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `NamedObject<DMAQueue,Module>::getUniqueId` | 0x256b40 | base case: `[Module+0x98]++` | CERTAIN |
| `NamedObject<Function,FunctionHolder>::getUniqueId` | 0x26c9c0 | holder→Module, null-throw | CERTAIN |
| `NamedObject<Instruction,BasicBlock>::getUniqueId` | 0x2d5be0 | →BB+0x48 | CERTAIN |
| `NamedObject<BasicBlock,BasicBlockHolder>::getUniqueId` | 0x23c220 | →BBHolder::getUniqueId | CERTAIN |
| `NamedObject<Storage,Function>::getUniqueId` | 0x3ca030 | →Function+0xA0 | CERTAIN |
| `NamedObject<MemoryLocation,MemoryLocationSet>::getUniqueId` | 0x32dab0 | →MLS+0x118 | CERTAIN |
| `BasicBlockHolder::getUniqueId` | 0x24b4a0 | getFunction() then Function::getUniqueId | HIGH |

---

## The NamedObjectContainer

### Purpose

`NamedObjectContainer<Cont, T>` is the owning structure for a list of `T`. It is two indexes over the same set of objects: an `llvm::ilist<T>` that preserves insertion order (and is the iteration order seen by every pass), and an `std::unordered_map<string, T*>` symboltable that gives O(1) name→object lookup. The container also holds the `Cont*` owner back-pointer that the id cascade above walks. Note the template argument order is *inverted* relative to `NamedObject`: here `Cont` is the owner and `T` is the element.

### Container layout

Offsets relative to the container base, anchored to `FunctionHolder::FunctionHolder(Module*)` @0x290250 (the cleanest ctor) and `getElementByName` @0x282030.

| Field | Off | Type | Meaning | Confidence |
|---|---|---|---|---|
| ilist sentinel `next` | +0x00 | `T*` (=self at ctor) | head of the ordered list | CERTAIN |
| ilist sentinel `prev` | +0x08 | `T*` (=self at ctor) | tail (self → empty) | CERTAIN |
| symtab `buckets` | +0x10 | `T**` (=container+0x40) | unordered_map bucket array | CERTAIN |
| symtab `bucket_count` | +0x18 | `size_t` (=1 at ctor) | bucket count (mod base) | CERTAIN |
| symtab `element_count` | +0x20 | `size_t` (=0 at ctor) | live element count | CERTAIN |
| symtab `before_begin` | +0x28 | `node*` (=0) | hashtable singly-linked anchor | HIGH |
| symtab `max_load_factor` | +0x30 | `float` (=1.0f, `0x3F800000`) | `_Prime_rehash_policy` load factor | CERTAIN |
| symtab `rehash_thresh` | +0x38 | `size_t` (=0) | rehash threshold | HIGH |
| inline `bucket[0]` | +0x40 | `T*` | single-bucket SSO storage | HIGH |
| owner back-pointer | +0x48 | `Cont*` | the owning object (e.g. `Module*`) | CERTAIN |

The ctor anchor is byte-exact: `FunctionHolder::FunctionHolder(Module*)` @0x290250 writes the sentinel as `this` (empty list), sets `[+0x10]=this+0x40` (buckets point at the inline slot), `[+0x18]=1`, `[+0x20]=0`, `[+0x30]=0x3f800000`, and `[+0x48]=` the `Module*` argument. The `buckets→+0x40` inline-init is the libstdc++ small-map optimization: a fresh map has one bucket living inside the container itself.

### Name lookup

`getElementByName` (exemplar `<BasicBlockHolder,BasicBlock>` @0x282030, 183 bytes) is a textbook libstdc++ bucket probe with one fixed parameter: the hash seed is the literal `0xC70F6907`, mov'd into `edx` and passed to `std::_Hash_bytes(key.data, key.size, 0xC70F6907)`. This same seed is used by **every** BIR symboltable.

```c
function getElementByName(this, const string& name):       // @0x282030
    h    = _Hash_bytes(name.data, name.size, 0xC70F6907);   // [name+0]=data, [name+8]=size
    idx  = h % this->bucket_count;                          // div [this+0x18]
    node = this->buckets[idx];                              // [[this+0x10] + idx*8]
    for (; node != nullptr; node = node->next):             // chain walk
        if (node->cached_hash == h                          // [node+0x30] — avoids rehash
            && node->key.size == name.size                  // [node+0x10]
            && memcmp(node->key.data, name.data, len) == 0):// [node+0x08]
            return node->value;                             // [node+0x28] — the stored T*
    return nullptr;
```

The hashtable node is `new(0x38)` = 56 bytes: `{ next@+0x00, key.data@+0x08, key.size@+0x10, key.sso@+0x18, value(T*)@+0x28, cached_hash@+0x30 }`. The node is minted by `insertIntoSymboltable` @0x24d9a0, which stores the key string at `+0x08` and the `T*` at `+0x28`.

### Element insertion and name uniquing

`insertElement<X>(ilist_iterator pos, name, …)` inlines the symtab probe, then on a free name does `new X`, calls `X`'s ctor with the name and the owner, links the node into the ilist, and registers it via `insertIntoSymboltable`. On a **name collision** it does not fail — it appends an ASCII decimal suffix and retries until the name is free, so two objects can never share a name within one container. The retry is a `std::to_string`-style digit-width selector tree (the `jbe` cascade at 0x24df08..); the exact format string (`"_N"` vs bare digits) is **[INFERRED]** — it was not isolated. *Anchor: the collision branch in `insertElement<BasicBlock>` @0x24de30.*

> **QUIRK —** the `<BasicBlock, Instruction>` container has a *distinct* `insertElement<InstX>` weak symbol for every BIR opcode — `insertElement<InstMatmult>`, `<InstActivation>`, `<InstTensorTensor>`, `<InstReturn>`, `<InstQuantizeMx>`, … The exact-mangled count under `NamedObjectContainer<BasicBlock,Instruction>::insertElement` is **110** (one per opcode leaf). This is the per-opcode minting funnel: each `Inst` leaf is created through its own template stamp, not a single generic `insertElement<Instruction>`. A reimplementation can collapse these to one polymorphic insert; the binary monomorphizes them.

---

## The BIR object graph

### The four-level spine plus side-trees

The six `NamedObjectContainer` instantiations wire the program tree. Each edge below is proven by the leaf's `__vmi` base list and the matching `insertElement<>`/`getUniqueId`.

```text
Module                                  [aggregate; embeds FunctionHolder @+0x00]
 ├─ IS-A FunctionHolder @+0x00
 │     └─ NamedObjectContainer<FunctionHolder,Function>   → owns Function*  ("functions")
 ├─ NamedObjectContainer<Module,DMAQueue> @+0x50          → owns DMAQueue*  ("dma_queues")
 ├─ Module+0x98 = THE global unique-id counter
 └─ nki_function_holder (heap FunctionHolder* @+0x230)    → owns NKI Function*
        │
 Function                               [__vmi: BBHolder + NOC<Function,Storage> + NamedObject<Function,FunctionHolder>]
 ├─ IS-A BasicBlockHolder @+0x08
 │     └─ NamedObjectContainer<BasicBlockHolder,BasicBlock> → owns BasicBlock*  ("basic_blocks")
 ├─ NamedObjectContainer<Function,Storage> @+0x58          → owns Storage*  (= MemoryLocationSet | Register)
 └─ NamedObject<Function,FunctionHolder> @Function+0xA0    (name/id; owner = FunctionHolder*)
        │
 BasicBlock                             [aggregate; IS-A NOC<BasicBlock,Instruction> @+0x00]
 ├─ NamedObjectContainer<BasicBlock,Instruction> @+0x00    → owns Instruction*  (110 insertElement<InstX>)
 ├─ NamedObject<BasicBlock,BasicBlockHolder> @BB+0x48      (name/id; owner = BasicBlockHolder*)
 └─ block-argument ilist @+0xA0                            (BasicBlockArgument phi nodes — the predecessor edges)
        │
 Instruction                           [__vmi: NamedObject<Instruction,BasicBlock> + logging::SrcHandle]
 └─ NamedObject<Instruction,BasicBlock> @Instr+0x08        (name/id; owner-BB @Instr+0x50)
```

The two side-trees:

- **Storage** under Function. `NamedObjectContainer<Function,Storage>` owns `Storage*`, where `Storage` is the base of two element subtypes — `MemoryLocationSet` and `Register` — which is why this one container mints two element kinds. A `MemoryLocationSet` is itself *also* a `NamedObjectContainer<MemoryLocationSet, MemoryLocation>`, so it owns a `MemoryLocation*` list (the side-table head). See [Memory Location and Storage](memory-location.md) for the full Storage family hierarchy.
- **DMAQueue** under Module, via `NamedObjectContainer<Module,DMAQueue>`.

> **NOTE —** `Module` and `BasicBlock` have **no** standalone `_ZTS`/`_ZTI` row of their own — they are non-polymorphic aggregates. Their RTTI identity, where needed, comes through an embedded polymorphic base: `BasicBlock` via its `NamedObjectContainer<BasicBlock,Instruction>` base, `Module` via the embedded `FunctionHolder`. So although six `getUniqueId` stamps exist, only **five** `NamedObject<T,Cont>` types carry their own typeinfo. (`<BasicBlock,BasicBlockHolder>` has a `getUniqueId` weak symbol @0x23c220 but no typeinfo row.)

### DMAQueue nests a block region

The `DMAQueue` typeinfo (`_ZTI` @0x8fc290) is a `__vmi` with two bases: `BasicBlockHolder` *and* `NamedObject<DMAQueue,Module>`. So a `DMAQueue` is not a flat queue — it IS-A `BasicBlockHolder` and therefore nests its own `BasicBlock` list (the DMA-descriptor block region), reusing the exact same container machinery as a top-level Function. The `ilist_node_with_parent<DMAQueue,Module>` typeinfo chain (@0x782a40 region) names the intrusive-node parent as the `Module`.

> **QUIRK —** the same `BasicBlockHolder` base that makes a `Function` a top-level block container also lets structured-control constructs nest regions. `BasicBlockHolder::getFunction` @0x24b190 reads a `kind` discriminant at `holder+0x50`: `kind==0` means the holder *is* the Function; `kind 3..6` means it is owned by a structured-control `Instruction` (the instruction sits at `holder+0x88`) and the walk climbs through that instruction's parent BB. This is how a nested loop-body region finds its enclosing Function — the container template is reused for both top-level and nested CFGs.

### Ownership summary

| Owner | Element | Via container |
|---|---|---|
| `Module` | `Function` | `NamedObjectContainer<FunctionHolder, Function>` |
| `Module` | `DMAQueue` | `NamedObjectContainer<Module, DMAQueue>` |
| `Function` | `BasicBlock` | `NamedObjectContainer<BasicBlockHolder, BasicBlock>` |
| `Function` | `Storage` (= MLS \| Register) | `NamedObjectContainer<Function, Storage>` |
| `BasicBlock` | `Instruction` | `NamedObjectContainer<BasicBlock, Instruction>` (110 per-opcode mints) |
| `MemoryLocationSet` | `MemoryLocation` | `NamedObjectContainer<MemoryLocationSet, MemoryLocation>` |
| `DMAQueue` | `BasicBlock` | its `BasicBlockHolder` base (nested DMA block region) |

In every owned `T`, the `Cont*` owner is stored at `NamedObject-sub+0x48`; the id cascade walks these slots up to `Module+0x98`.

---

## Iteration, naming, and the loader

### Two indexes, two access patterns

A pass that walks a container in program order iterates the `ilist` (the `+0x00/+0x08` sentinel); a pass that resolves a by-name reference hits the `unordered_map`. Both index the same `T` objects: the ilist threads the `ilist_node_with_parent` sub-object (the `+0x10` base of each `NamedObject<T,Cont>`), and the map stores `T*` values keyed by the object's `name`. Accessor pairs exist at every level — e.g. `Module::getFunctionByName` @0x354a60, `Function::getBasicBlockByName` @0x26cee0 / `getInstructionByName` @0x272320 / `getMemoryLocationSetByName` @0x26cb40 / `getRegisterByName` @0x26ce10 — all of which dispatch into the shared `getElementByName` probe with the level-specific symtab offsets.

### Where the mints come from

The containers are populated by the two-pass JSON loader. Five of the six containers carry an `adl_serializer::from_json` that drives the per-element mint: `<BasicBlock,Instruction>`, `<BasicBlockHolder,BasicBlock>`, `<FunctionHolder,Function>`, `<MemoryLocationSet,MemoryLocation>`, and `<Module,DMAQueue>`. The sixth — `<Function,Storage>` — has only a `to_json`; storages are loaded *inline* by `Function::createFromJson` rather than by a generic container deserializer. The full pass1-creates / pass2-resolves model, and how forward by-name references (phi incoming values, cross-function memloc bindings) are deferred to pass 2, is documented on the [BIR JSON Loader](json-loader.md) page.

---

## Evidence summary

- **The CRTP self-derivation** (`T : NamedObject<T,Cont> : {NamedObjectBase, ilist_node_with_parent}`) comes from `readelf -rW`: `Instruction _ZTI` @0x8fcd78's base reloc points at `NamedObject<Instruction,BasicBlock>` @0x8fcd40, whose two `__vmi` bases are `NamedObjectBase` @0x8fc230 (offset 0) and `ilist_node_with_parent<Instruction,BasicBlock>` @0x8fcd28 (offset 0x10).
- **The `+0x48` universal owner slot and the id cascade** come from all six `getUniqueId` bodies: each opens with `mov …,[rdi+0x48]`; DMAQueue does `[+0x98]++` directly; Storage adds `+0xA0`, MemoryLocation adds `+0x118`, Instruction adds `+0x48` before tail-jumping.
- **The single `Module+0x98` global counter** is the only counter post-increment anywhere in the cascade — it appears in the DMAQueue and Function bodies, and every other body routes to one of those.
- **The fixed hash seed `0xC70F6907`** is `mov edx, 0C70F6907h` immediately before `call _Hash_bytes` in `getElementByName` @0x282030.
- **The 110 per-opcode `insertElement<InstX>` count** is an exact-mangled symbol count over native exports. A looser grep returns 111 by also matching the generic `insertElementI` stem; the exact figure is 110.

## Limits of this reading

- The **field partition** between `NamedObjectBase` and `NamedObject<T,Cont>` — whether `name` and `origin` live in the base or the derived template — is **[INFERRED]** from the unified `+0x18` name offset; no standalone `NamedObjectBase` ctor was disassembled.
- The collision-rename **suffix format** is **[INFERRED]**: the int-to-chars loop is present, but the format string was not isolated.
- The `optin_passes` mask default `0xF` and the `symtab.before_begin` / `rehash_thresh` zero-inits are read from ctor self-init writes rather than a dedicated getter, so their *semantics* rest on the libstdc++ layout rather than on a BIR-side accessor.

---

## Related Components

| Name | Relationship |
|---|---|
| `NamedObjectBase` | The polymorphic root; holds the `optin_passes` machinery and origin |
| `BasicBlockHolder` | The block-container base reused by Function and DMAQueue (and structured-control Instructions) |
| `FunctionHolder` | The function-container base embedded at `Module+0x00`; ctor is the cleanest container exemplar |
| `Storage` / `MemoryLocationSet` | Side-tree elements under Function; MLS is both a Storage and a container |

## Cross-References

- [Instruction Base](instruction-base.md) — the `Instruction` leaf body; its `NamedObject<Instruction,BasicBlock>` sub-object @Instr+0x08, parent-BB @Instr+0x50
- [Memory Location and Storage](memory-location.md) — the `StorageBase ⊃ {Storage ⊃ {MLS, Register}, MemoryLocation}` family that the Function/MLS containers own
- [BIR JSON Loader](json-loader.md) — the two-pass `from_json` driver that calls the `insertElement<>`/`createFromJson` mints documented here
