# The VNC Cross-Core Link — `vnc_remote_addr_map` and `vnc_link`

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical Cython rebuilds, with a few `run`-entries shifted ±0x10). The pass bodies live in `neuronxcc/starfish/lib/libwalrus.so`; the `bir::MemoryLocation`, `bir::sync::Update`, and `bir::InstDMABlock` accessor bodies are imported from `libBIR.so`. For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; `0x5e9020`–`0x62d650` is the `.plt` thunk region — every `*@plt` callee below is an import trampoline, so cite the high-band body, not the thunk. `libwalrus.so` was stripped of its static symtab; method names here are recovered from the IDA `names.json` and from the full dynamic symtab of `libBIR.so` (`nm -DC`). Other wheels differ — treat every address as version-pinned.*

## Abstract

A model that the LNC splitter has cut into `N` per-core modules ends with two physical halves that do not yet agree on anything concrete. A tensor that core A produced and core B consumes is, on core B's side, only a *name* — a `RemoteLocalTarget {remote_name, ownerCore}` annotation with no physical address. A semaphore that core A signals on completion is, on core B's side, only a *symbolic remote reference* — a `sync::Update` tagged with a destination core but carrying core B's own (wrong) semaphore id. The two passes documented here close both gaps at the lowest BIR level, after `bir_linker` has reassembled the per-core subgraphs and immediately before codegen.

`vnc_remote_addr_map` (the address side) walks every `MemoryLocation` carrying a `RemoteLocalTarget`, looks up the owner core's real `MemoryLocation` by name, and copies its **entire physical allocation tuple** — `{MemoryType, MemoryAddressSpace, address, bankId, basePartition, flag}` — verbatim onto the local alias via `allocate_no_check`. After this pass the local handle physically references the owner core's memory cell. `vnc_link` (the sync side) walks every remote-notifying DMA block, descends into the destination core's mirror instruction, finds the matching *local* `sync::Update` there, and copies that peer's concrete **semaphore id** back into the local remote update. After this pass the cross-core handshake names the exact semaphore the destination core's hardware will signal and wait on.

The headline hardware fact is that this is **not** an off-chip NeuronLink fabric address. VNC ("Virtual NeuronCore") cross-core traffic is a *same-die LNC split* over **shared HBM** plus **DMA-carried remote semaphore updates**: the cores share one device's HBM (one of them owns the physical cell, every other core reuses that same physical `{address, bank, partition}` because the cell lives in the cross-core-visible `Shared` address space), and a remote semaphore signal is just a small DMA write into the sibling's semaphore address. There is no inter-core base-plus-offset arithmetic in either pass — the address is copied, not computed.

## Key Facts

| Item | Value |
|---|---|
| **Pass class (L34)** | `neuronxcc::backend::VncRemoteAddrMap` |
| **L34 real body** | `run(vector<unique_ptr<Module>>&)` `@0x164e300` (753 lines) — the multi-core worker |
| **L34 shim** | `run(bir::Module&)` `@0x164cf20` — sets status `2` then throws `std::logic_error` (always aborts) |
| **L34 ctor** | `VncRemoteAddrMap(PassOptions const&)` `@0x164f940` |
| **L34 register** | `register_generator_vnc_remote_addr_map__` (`.bss 0x3e0530d`); lambda `_M_invoke @0x164fba0` |
| **Pass class (L35)** | `neuronxcc::backend::VncLink` |
| **L35 real body** | `run(vector<unique_ptr<Module>>&)` `@0x1648de0` (1395 lines) — the multi-core worker |
| **L35 shim** | `run(bir::Module&)` `@0x16477c0` — sets status `2` then throws (always aborts) |
| **L35 ctor** | `VncLink(PassOptions const&)` `@0x1647a30` |
| **L35 register** | `register_generator_vnc_link__` (`.bss 0x3e052fc`); lambda `_M_invoke @0x164c020` |
| **Config** | `vncNcCount` (`PassOptions+0x1A4`, offset 420); `cl::opt vnc-nc-per-sengine` |
| **Producers of `RemoteLocalTarget`** | `DRAM_Allocator::mark_remote @0xa76080`; `LowerLocalCollectives::getMemoryLocation` |
| **Source paths** | `neuronxcc/walrus/vnc_remote_addr_map/src/vnc_remote_addr_map.cpp` (`:19`, `:43`); `neuronxcc/walrus/vnc_link/src/vnc_link.cpp` (`:25`, `:92`, `:103`) |
| **VNC sim hook** | `birsim::BirSim::test_vnc(bir::Module&) @0x110f030` (string `vnc-birsim`) |

Both passes are VNC-only: their single-module `run(bir::Module&)` overload is an abort shim. This is the same shim/real split that `LncSplitter` uses — a pass that only makes sense over the *vector* of per-core modules refuses to run on a lone module. Disassembly of `@0x164cf20` shows the shim setting the result-status word to `2`, then calling a helper that constructs a diagnostic string and routes to `__throw_logic_error`; `@0x16477c0` is byte-for-byte the same shape.

## The Data Structure — `RemoteLocalTarget` on `bir::MemoryLocation`

The cross-core alias lives directly on `bir::MemoryLocation` as an embedded `boost::optional<std::pair<std::string, unsigned int>>` — `(remote_name, ownerCore)`. It is not a side container; each `MemoryLocation` carries at most one. The accessors are imported from `libBIR.so` (dynamic symtab present):

| Symbol | `libBIR.so` addr |
|---|---|
| `MemoryLocation::getRemoteLocalTarget[abi:cxx11]()` | `0x32e380` |
| `MemoryLocation::setRemoteLocalTarget(opt<pair<string,uint>> const&)` | (set sibling) |
| `MemoryLocation::getRemoteLocalTargetHierarchicalName[abi:cxx11]()` | `0x3313e0` |
| `MemoryLocation::isRemote()` | `0x32e390` |

The recovered struct layout (from the set/get bodies and the `allocate_no_check` argument harvest below):

```c
struct bir_MemoryLocation {              // partial; offsets CONFIRMED from libBIR disasm
    // ...
    void  *memoryType;     // +0xD8  -> int kind   (getMemoryType reads *(+0xD8))
    void  *addressSpace;   // +0xF0  -> int space  (Local/Shared/Debug)
    uint8_t allocFlag;     // +0xF8  -> byte        (allocated/space flag)
    // ...
    bool   rlt_engaged;    // +0x170 boost::optional has_value flag
    char  *rlt_name_data;  // +0x178 std::string name .data
    size_t rlt_name_size;  // +0x180 std::string name .size
    char   rlt_name_sso[16];// +0x188 SSO buffer
    uint   rlt_ownerCore;  // +0x198 the pair's unsigned int — WHICH core owns the cell
    // ...
    long   bankId;         // +0x210 getBankId()      = *(+0x210)
    ulong  address;        // +0x218 getAddress()     = *(+0x218)
    uint   basePartition;  // +0x230 getBasePartition()= *(+0x230)
};
```

`MemoryLocation::isRemote()` is simply `*(byte*)(this+0x170)` — "is remote" is defined as "has a `RemoteLocalTarget`". `getRemoteLocalTargetHierarchicalName()` resolves the *name to use* for the remote: if no target is engaged it returns the location's own hierarchical name; otherwise, for external-input/output tensor kinds (the boundary tensors that actually cross cores), it returns the remote target's name; internal tensors fall back to their own name. The string half of the pair is therefore the qualified name of the peer tensor this location aliases. *(CONFIRMED — `getRemoteLocalTarget`/`isRemote`/`getRemoteLocalTargetHierarchicalName` symbols in `libBIR.so` dynamic symtab; offsets from set/get disasm.)*

### Who sets it — the producers

`RemoteLocalTarget` is *minted* upstream and *consumed* here. The primary producer is `DRAM_Allocator::mark_remote @0xa76080`, called from `DRAM_Allocator::allocate` on the dual-allocation path. For each shared `MemoryLocation` that should **not** be physically placed on this (non-owning) core, it clears the allocation flag, reuses the tensor's own (SPMD-identical) name, and stamps the remote-alias annotation:

```c
// DRAM_Allocator::mark_remote(ArrayRef<MemoryLocation*>)   @0xa76080
for (MemoryLocation *loc : locs) {
    loc->setAllocated(false);                 // do NOT place a second copy on this core
    name = loc->name();                       // own hierarchical name (SPMD: same on every core)
    loc->setRemoteLocalTarget({name, ownerCore});  // mark as a cross-core alias
    reset_cached_partition_addr(loc);         // *(int*)*(loc+240) = 0
}
```

This is the path behind the recovered log line **`"Skipping shared tensor allocations on core 1, marking as remoteLocalTarget instead"`** (verified present in `libwalrus.so`). The tensor is allocated once, on the owner core; every other core gets a same-named `MemoryLocation` marked remote. Because the name is reused verbatim across the SPMD cores, `vnc_remote_addr_map` can find the owner's copy by name in the owner's module. The second producer is `LowerLocalCollectives::getMemoryLocation @0x161ab30`, which mints `RemoteLocalTarget`s for collective (all-reduce / all-gather) staging buffers — tying this link to [Part 8's collectives lowering]. *(CONFIRMED — both producer symbols in the `setRemoteLocalTarget` call set; the log string is in the binary.)*

Note the division of labour with the earlier shared-DRAM machinery documented on [the DRAM allocator page](dram-allocator.md): `sync_shared_allocations` (order 83, `run(vector<Module>&) @0x16b2da0`) already equalizes the *owner's* physical `{address, partition, bank}` across the sibling modules **before** the linker runs, and `bir_linker` unifies the per-core *names*. `vnc_remote_addr_map` is the **post-link, low-level finalizer** (roster position L34): after the linker has reassembled the per-core subgraphs, it re-derives the concrete cross-core `MemoryLocation` on each consuming core so that codegen emits real address targets. The two passes share a mechanism (copy the owner's physical tuple onto the alias) but run at different points and for different reasons.

## `vnc_remote_addr_map` (L34) — the Address Alias Copy

### Precondition: one module per virtual core

The first thing the worker does is gate `modules.size() == options.vnc_nc_count`. The disassembled check reads the count from `PassOptions+0x1A4` (offset 420) and compares it to the module-vector span:

```c
// run(vector<unique_ptr<Module>>&)   @0x164e300, head
v4 = modules.begin();  v3 = modules.end();
if ( *(uint*)(*(PassOptions**)(this+40) + 420) != (v3 - v4) ) {   // vnc_nc_count != modules.size()
    log( ".../vnc_remote_addr_map/src/vnc_remote_addr_map.cpp:19" );
    NeuronAssertion<ErrorCode>(/*id*/ 1513, "modules.size() == options.vnc_nc_count", ...);
    // fmt binds { core_count = vnc_nc_count, mod_count = modules.size() }
}
```

So **one `bir::Module` per virtual NeuronCore** — the module vector *is* the per-core split, and `vnc_nc_count` is the virtual-core count. *(CONFIRMED — `ErrorCode` value `1513`, source path `vnc_remote_addr_map.cpp:19`, and `+420` offset all read from the `@0x164e300` body. As in L35, the `1513`/`1514` are the `ErrorCode` enum argument to `NeuronAssertion`, **not** line numbers — the line is the separate `.cpp:NN` path string.)*

### The walk

For each module (core), iterate its `Storage` entries filtered to `MemoryLocationSet`, and for each `MemoryLocation` in the set apply a two-part filter, then resolve and copy:

```c
for (Module *m : modules)                                    // outer: per core
  for (Storage *s : m where isa<MemoryLocationSet>(s)) {
    MemoryLocationSet *mls = cast<MemoryLocationSet>(s);
    for (MemoryLocation *loc : mls) {                        // list walk
        if ( node.kind != 16 ) continue;                    // entry-kind filter (see below)
        if ( !loc->getRemoteLocalTarget().engaged ) continue;// only remote-aliased locs

        auto tgt = loc->getRemoteLocalTarget();             // (name, ownerCore); asserts engaged
        std::string remote_name = tgt.first;                // copy the peer tensor name
        uint        owner_core   = tgt.second;              // = *(tgt+40)

        // --- resolve the OWNER's real MemoryLocation by name ---
        Function       *f   = m->getFunctionByName(remote_name);
        MemoryLocation *rml = f->getMemoryLocationByName(remote_name);
        if (!rml)                                            // fallback: rescan the module's MLOC sets
            for (Storage *s2 : ... ) {
                rml = cast<MemoryLocationSet>(s2)->getMemoryLocationByName(remote_name);
                if (rml) break;
            }
        if (!rml) {
            log( ".../vnc_remote_addr_map/src/vnc_remote_addr_map.cpp:43" );
            NeuronAssertion<ErrorCode>(1514, "remote_mloc != nullptr", ...);
            // fmt binds { remote = remote_name, core = owner_core, local = loc.name }
        }

        // --- materialize: copy the owner's full physical tuple onto the local alias ---
        rml->getBasePartition();
        rml->getBankId();
        rml->getAddress();
        loc->allocate_no_check( /* every arg harvested from rml — see disasm below */ );
    }
  }
```

The `node.kind != 16` test reads a kind tag at list-node `-0x8` of the iterator (`*(uint*)*(v11-8) != 16`). `16` is the `Storage`/entry-kind enum value that owns a `MemoryLocation`. *(Value `0x10` is CONFIRMED from disasm; the enum-name is INFERRED.)* The `remote_mloc != nullptr` assert (`ErrorCode=1514`) at `vnc_remote_addr_map.cpp:43` fires when the owner's named copy cannot be found. *(CONFIRMED — `ErrorCode` values `1513`/`1514`, the `getFunctionByName → getMemoryLocationByName → MemoryLocationSet::getMemoryLocationByName` fallback chain, and the `getRemoteLocalTarget` filter all read from the body.)*

### The alias copy — byte-anchored

The decompiler elides `allocate_no_check`'s arguments (calling-convention loss), so the headline claim is pinned from the disassembly at `@0x164e5bc`–`0x164e635`. Every register loaded for the call is harvested from the **remote** location (`var_2D8` = `rml`); the call `this` is the **local** alias (`var_2E0` = `loc`):

```text
0x164e5bc  mov  rcx, [rbp+var_2D8]        ; rcx = remote_mloc (rml)
0x164e5c3  mov  rax, [rcx+0F8h]           ; rax = rml->allocFlag ptr   (+0xF8)
0x164e5cd  movzx eax, byte ptr [rax]      ; var_2E8 = *(byte*)flag
0x164e5d6  call MemoryLocation::getBasePartition(rml)   ; -> r15d (partition)
0x164e5e5  call MemoryLocation::getBankId(rml)          ; -> var_300 (bank)
0x164e5f8  call MemoryLocation::getAddress(rml)         ; -> rcx     (address)
0x164e612  mov  rdi, [rbp+var_2E0]        ; rdi = this = LOCAL alias (loc)   <-- destination
0x164e619  mov  r9d, r15d                 ; r9  = basePartition  (from rml)
0x164e61c  mov  rdx, [rsi+0F0h]           ; rdx = rml->addressSpace ptr  (+0xF0)
0x164e623  mov  rax, [rsi+0D8h]           ; rax = rml->memoryType   ptr  (+0xD8)
0x164e631  mov  edx, [rdx]                ; rdx = *addressSpace  (MemoryAddressSpace)
0x164e633  mov  esi, [rax]                ; rsi = *memoryType    (MemoryType)
           push var_2E8                   ; stack arg = flag
0x164e635  call MemoryLocation::allocate_no_check(MemoryType, MemoryAddressSpace,
                                                   ulong addr, long bank, uint part, bool flag)
```

Mapped to the signature `allocate_no_check(MemoryType, MemoryAddressSpace, u64 addr, long bank, u32 partition, bool flag)`:

| arg | source | meaning |
|---|---|---|
| `this` (rdi) | `loc` (var_2E0) | **local alias** — the destination of the copy |
| MemoryType (rsi) | `*(uint*)(rml+0xD8)` | remote memory type |
| MemoryAddressSpace (rdx) | `*(uint*)(rml+0xF0)` | remote address space (Local/Shared/Debug) |
| addr (rcx) | `rml->getAddress()` (`*(rml+0x218)`) | remote byte address |
| bank (r8) | `rml->getBankId()` (`*(rml+0x210)`) | remote bank id |
| partition (r9) | `rml->getBasePartition()` (`*(rml+0x230)`) | remote partition base |
| flag (stack) | `*(byte*)(rml+0xF8)` | remote allocated/space flag |

Every operand is the owner core's value; nothing is added, scaled, or offset. **`vnc_remote_addr_map` copies the owner location's full physical allocation tuple onto the local handle verbatim.** After it runs, `loc` physically references the owner core's memory cell. `allocate_no_check` is the force-assign primitive — it stamps `{type, space, addr, bank, partition, flag}` without the allocator's usual placement check (the address is dictated by the owner, not chosen here) and resets the location's cached `PhysicalAccessPattern` intervals. The worker returns an empty `ErrorResult` on success. *(CONFIRMED — disasm `@0x164e5bc`–`0x164e635`; this is the single strongest claim on the page and it is byte-anchored at the call site.)*

### Why the verbatim copy is valid — the `Shared` address space

The copy is correct only because the address it reuses is *already* globally meaningful. The `MemoryAddressSpace` enum, recovered from `libBIR.so`'s `MemoryAddressSpace2string`:

| Value | Name | Meaning |
|---|---|---|
| `0` | `Local` | per-core private window — **not** directly cross-core-addressable |
| `1` | `Shared` | the cross-core-visible window — every virtual core addresses it at the same physical address |
| `2` | `Debug` | debug space |
| (else) | — | `"Unknown memory address space"` |

Because L34 copies the owner's `MemoryAddressSpace`, a tensor the producer placed in `Shared` keeps `Shared` on the alias, and the alias's reused `{partition, bank, addr}` is valid on the consuming core too. `Local`-space tensors are **not** cross-core-addressable; only locations already in `Shared` — in practice the DRAM/HBM-resident boundary tensors the DRAM allocator marks — get a valid remote address. This is why the producer is the *DRAM* allocator and why the [LNC memory model](../arch/lnc-memory-model.md) finds the entire cross-core machinery keyed to DRAM alone: there is no `sharedSBUF`/`sharedPSUM` — the only cross-core data channel is shared HBM, and an alias into it reuses the owner's physical address. *(CONFIRMED enum from `libBIR` string table; the verbatim-copy / no-base-add observation is from the L34 disasm — there is no arithmetic between the `getAddress` result and the `allocate_no_check` `addr` argument.)*

## `vnc_link` (L35) — the Semaphore Relocator

`vnc_remote_addr_map` placed the remote *data* addresses; `vnc_link` links the remote *sync*. After a producer core's DMA writes a shared buffer, it must signal a semaphore that the consumer core waits on — but the cross-core `sync::Update` initially names only `(targetCore, symbolic)`. L35 walks into the destination core's mirror instruction, finds the matching local update there, and patches the real semaphore id into the update. This is the same relocation idea as L34 but in the synchronization domain. The mechanism it links is the one behind the recovered string **`"Replace finishing CoreBarrier instruction with remote update of semaphore by DMACopy"`** (in `.rodata @0x1d7fef0`, referenced by `sub_7E0F10` — an *upstream* pass that lowers a cross-core barrier into a remote-semaphore update piggybacked on a `DMACopy`; it is **not** emitted by `VncLink::run` itself). L35's own diagnostics are a `fort::table` summary (`"DMA Blocks: "` per-core, `"Found N remote updates"`). *(CONFIRMED — the CoreBarrier string's sole xref is `sub_7E0F10 @0x7e10a1`, a different function; L35's `"DMA Blocks: "`/`"Found … remote updates"` libfort table is in the `@0x1648de0` body.)*

### Collect the remote-notifying DMA blocks

Same precondition gate (`modules.size() == vnc_nc_count`, `ErrorCode=1509`, source `vnc_link.cpp:25`). Then per module, walk blocks and instructions and collect the DMA blocks that carry a remote notification:

```c
for (Module *m : modules)
  for (Instruction *I : m's blocks)
    if ( dyn_cast<InstDMABlock>(I) && bir::isRemoteUpdateInstruction(*I) )   // @0x1649... call
        perCore[core].push_back(I);
```

`bir::isRemoteUpdateInstruction(Instruction const&) @0x30f930` (`libBIR`) returns true iff the instruction has `SyncInfo`, is an `InstDMABlock`, and `InstDMABlock::getRemoteNotification() @0x2518e0` returns a non-empty `vector<unsigned int>`. The notification vector holds the cross-core semaphore/core ids the DMA must signal on completion — the hardware handshake L35 fills in concretely. *(CONFIRMED — `isRemoteUpdateInstruction`, `getRemoteNotification`, `setRemoteNotification @0x2518f0` all in `libBIR` dynamic symtab.)*

### The all-equal lockstep check

Across cores, the collected remote-update instructions occupy the **same ordinal index** — the program is SPMD, so the instruction at slot `v157` is the same instruction on every core. L35 enforces this: it builds the per-core count vector and, if not all equal, emits a `fort::table` summary (each row `Module::getName()` and its remote-update count) and raises:

```c
// @0x1648de0, ~vnc_link.cpp:92
if ( !allEqual(perCoreCounts) ) {
    fort::table report;  for (m : modules) report << m->getName() << count(m);
    NeuronAssertion<ErrorCode>(ErrorCode/*=1512, esi=0x5E8*/, "allEqual", ...);   // fmt binds { sizes }
}
```

This invariant is *why* L35 can index the peer's mirror instruction by the shared ordinal. *(CONFIRMED — `ErrorCode` enum value `1512` (`esi=0x5E8 @0x1649ccf`), string `"allEqual"`, source path `vnc_link.cpp:92`, and the `fort::table` report path all read from the body at lines ~647–693. Note: the `1509`/`1511`/`1512` values are the **`ErrorCode` enum argument** to `NeuronAssertion`, **not** source line numbers — the line is carried separately in the `.cpp:NN` path string. The pairing in this pass is `:25↔1509`, `:92↔1512`, `:103↔1511`.)*

### The relink — copy the peer's semaphore id

The `sync` structs (32-byte stride, recovered from `libBIR` disasm):

```c
struct SyncRef {  void *vtbl; uint syncType /*+0x08*/; uint id /*+0x0C*/; };
struct Update : SyncRef {                              // 32-byte stride
    uint mode  /*+0x10*/;  uint value /*+0x14*/;
    boost::optional<uint> targetCore /*+0x18, engaged byte @+0x18*/;
};
// on Instruction: SyncInfo at Instruction+88; SyncInfo+0x40 = updates begin; +0x48 = count(uint)
bool Update::isRemote()      { return targetCore.engaged; }   // remote iff a target core is set
auto Update::getTargetCore() { return &targetCore; }
```

For each core, for each collected remote-DMA instruction, rebuild its update list. Local updates are copied through unchanged; each *remote* update is relinked by descending into the destination core's mirror instruction:

```c
SyncInfo *si = I->getSyncInfo();                       // Instruction+88
SmallVector<sync::Update> newUpdates;                  // v202
for (Update &u : si->updates) {                        // begin @+0x40, count @+0x48, stride 32
    if ( u.isRemote() ) {                              // @0x164985b
        uint tc = *u.getTargetCore();                  // destination core index
        Instruction *tgtI = modules[tc]'s same-ordinal instr (v157);
        SyncInfo *tsi = tgtI->getSyncInfo();           // peer SyncInfo  (+88)
        Update *tu  = tsi->updates_begin;              // *(tsi+0x40)
        Update *end = tu + tsi->count;                 // tu + 32 * *(uint*)(tsi+0x48)
        while ( tu->isRemote() ) tu += 32/sizeof? ;    // skip leading remote updates  (@1253)
        if ( tu == end )                               // no local match
            NeuronAssertion<ErrorCode>(ErrorCode/*=1511, esi=0x5E7*/, ".../vnc_link.cpp:103", ...);
        uint resolved_id = SyncRef::getId(tu);         // peer's REAL semaphore id   (@1259)

        newUpdates.push_back( sync::Update(
            SyncRef::getType(u),    // type      — from the ORIGINAL remote update
            resolved_id,            // id        — LINKED from the peer  <-- the relocation
            u.getMode(),            // mode      — from the original
            u.getValue(),           // value     — from the original
            u.getTargetCore() ) );  // targetCore— from the original (kept as the dest tag)
    } else {
        newUpdates.push_back(u);                        // local update: copy through
    }
}
I->getSyncInfo()->updates = newUpdates;                 // SmallVectorImpl::operator= — WRITE BACK (@1327)
```

### The ctor arg map — byte-anchored

The `Update` ctor call site `@0x164992e` pins exactly which fields come from where. The ctor is `Update(SyncRef::SyncType, uint id, UpdateMode, uint value, boost::optional<uint> targetCore)`:

```text
0x164986b  call SyncRef::getType(u)        ; esi = var_518  (type   <- ORIGINAL remote update, rbx=u)
0x1649900  call Update::getMode(u)         ; r12d          (mode   <- original)
0x164990b  call Update::getValue(u)        ; ebp           (value  <- original)
0x1649915  call Update::getTargetCore(u)   ; r9 = [rax]    (targetCore <- original)
            ...                            ; edx = var_4E0 (id <- PEER's getId, walked from tgt core)
0x1649921  mov  r8d, ebp                   ; value
0x1649924  mov  ecx, r12d                  ; mode
0x1649927  mov  esi, [rsp+...var_518]      ; type
0x164992b  mov  rdi, r15                   ; this = the new Update
0x164992e  call bir::sync::Update::Update(SyncType, uint, UpdateMode, uint, optional<uint>)
```

Only `id` (edx, `var_4E0`) is resolved from the destination core; `{type, mode, value, targetCore}` are preserved from the original remote update. **`vnc_link` is a pure relocation of the symbolic remote-semaphore reference: it copies the peer core's concrete semaphore id into the local remote update and leaves everything else.** After it runs, the instruction's sync update carries the exact semaphore the destination core's hardware will signal/wait on, and `InstDMABlock::setRemoteNotification` binds the DMA block's notification vector to the resolved target-core id(s). The `ErrorCode=1511` assert at `vnc_link.cpp:103` fires if the peer instruction has no matching local update. *(CONFIRMED — ctor call `@0x164992e` with the five register loads; the peer-`getId` source (`getId(peer)@0x16498f4 → var_4E0`) and the `while(isRemote) skip → getId` at decompiled lines 1253–1259; `ErrorCode` value `1511` (`esi=0x5E7`), source path `:103`.)*

## The Hardware Mechanism — Same-Die Shared HBM, DMA-Carried Remote Semaphores

The headline correction: VNC cross-core is **not** an off-chip NeuronLink fabric address. It is a *same-device LNC split* — the virtual cores are partitions of one physical Trainium/Inferentia die, sharing that die's HBM and its on-chip sync. The evidence chain:

1. **Shared address space, not fabric.** The cross-core-visible window is `MemoryAddressSpace::Shared (1)`. A tensor the owner placed in `Shared` is addressable by every virtual core at the owner's own `{partition, bank, addr}`, and L34 reuses that physical address *verbatim* — there is no per-core base added in this pass (the disasm shows a copy, not an arithmetic combine). The base-plus-offset that makes the address global was realized **earlier**, when the owner allocated into `Shared`. Supporting strings (verified in the binary): `"Skipping shared tensor allocations on core 1, marking as remoteLocalTarget instead"`, `" based on its remote local target "`, plus the `cl::opt<bir::MemoryAddressSpace>` parser family. *(STRONG — the Shared enum is CONFIRMED; that the cross-core address is a verbatim reuse rather than a fabric address is the structural reading of the L34 copy.)*

2. **The shared window lives in HBM.** The owner of the remote address is the *DRAM* allocator; the [LNC memory model](../arch/lnc-memory-model.md) verifies the negative directly — there is no `sharedSBUF`/`sharedPSUM` symbol or string in either `libwalrus.so` or `libBIR.so`; the entire shared/cross-core machinery is keyed to DRAM/HBM alone. Inter-core data movement therefore has exactly one channel: shared HBM. *(STRONG — corroborated by the sibling page's exhaustive negative sweep and by the DRAM allocator owning `mark_remote`.)*

3. **The remote semaphore is a DMA write.** Cross-core sync is `"remote update of semaphore by DMACopy"`: the DMA engine, on completion, writes a semaphore at the destination core's address. `InstDMABlock::getRemoteNotification()` carries the target sem/core ids; L35 links those to the peer's real semaphore id. A remote semaphore signal is thus a small DMA write into the sibling's semaphore address in shared HBM — **not** a fabric message. *(STRONG — `isRemoteUpdateInstruction`/`getRemoteNotification`/`setRemoteNotification` symbols + the barrier-to-DMACopy string.)*

What this binary does **not** pin (upstream-of-binary): the physical base address of each per-core `Shared` window (a runtime/firmware constant — the compiler reuses the owner's already-assigned address, it does not add a per-core base in either pass), and the provenance of the owner-core index inside `mark_remote`'s pair (it comes from `DRAM_Allocator` state not visible in the `@0xa76080` body). *(INFERRED / UPSTREAM-OF-BINARY.)*

## Configuration and Registration

| Knob | Source | Effect |
|---|---|---|
| `vncNcCount` / `PassOptions+0x1A4` (offset 420) | both passes' preconditions | number of virtual NeuronCores = required `modules.size()` |
| `cl::opt vnc-nc-per-sengine` | rodata `0x15291a` | VNC cores-per-sequencer-engine |
| `vncNcCount == 1 || (allocator != "lsa" && optlevel != 0 && optlevel != 8)` | pipeline constraint | LSA / opt0 / opt8 are **single-vNC only** — the cross-core passes only run when `vncNcCount > 1` |

Both passes register through a `std::function` generator lambda in the backend pass registry: `register_generator_vnc_remote_addr_map__` (`.bss 0x3e0530d`) and `register_generator_vnc_link__` (`.bss 0x3e052fc`); the lambdas construct the pass from `PassOptions const&` and store the pass name. The VNC-mode BIR simulator hook `birsim::BirSim::test_vnc(bir::Module&) @0x110f030` (string `vnc-birsim`) validates cross-core behaviour in simulation. The roster positions are L34 (`vnc_remote_addr_map`) then L35 (`vnc_link`) — a classic produce/consume split: L34 builds the address aliases, L35 consumes the per-core layout to link the sync.

## The Surrounding Low-Level Tail — `mem2reg`, `seq_inst_opt`, `branch_hint`

The VNC pair sits inside the post-codegen low-level pass tail (`sub_805870`, the `L#` band). Three neighbours are worth pinning because they bracket and follow the VNC link and operate on the same per-core BIR.

**`mem2reg` (L25, `Mem2Reg::run(bir::Module&) @0x1720dd0`)** is textbook Cytron SSA construction applied to BIR *register* operands — it promotes scalar SP/control values written-then-read through a `MemoryLocation`-backed `Register` into single-def SSA form. `run()` drives three CRTP visitors per function: `JoinAnalysisImpl` (builds a `CFG`, runs `ComputeDominatorFrontier` for phi placement, keying a `DenseMap<pair<BasicBlock*,Register*>, PhiInfo>` — one placed phi per merge-block/register), `SSARenameImpl` (DFS the dominator tree, mint versioned registers via `Function::addRegister`, rewrite each use to its reaching SSA name — semaphore-wait register reads via `sync::Wait::getReg()` are renamed too, keeping sync operands consistent with the new SSA names), and `CheckRegOpSSAImpl` (post-condition verifier). `Mem2Reg::checkSSA(bir::Module&) @0x1723130` re-runs only the third visitor as a standalone legality re-check after later passes. The recovered `SmallVector` inline capacities (6 blocks / 6 regs) are the tuning constants. *(CONFIRMED — the three `enterFunction` call sequences and DenseMap key types are byte-anchored; see Strand-H D-H40.)*

**`seq_inst_opt` (L26, `SeqInstOpt::run(bir::Module&) @0x155e0e0`, `visitInstRegisterMove @0x155e980`)** is block-local copy-propagation + dead-copy-elimination over the sequencer register file. `enterModule @0x155e090` reads `ModuleAttribute` index 18 (a per-module enable/safety gate) and `run()` skips the walk if set. `enterBasicBlock @0x155c7c0` `memset`s a 0x20-byte control block — the copy-tracking map is cleared per block, so the analysis is strictly block-local. `visitInstRegisterMove` tracks a `_Hashtable<pair<uint, EngineType>, Register*>` keyed by `(register-slot, EngineType)` valued by the last `Register` written there; a move whose source already equals the tracked last-def is redundant, its readers are rewritten to the source via `ConvertReaderWriter<RegisterAccess>`, and the dead move is queued. `leaveBasicBlock @0x155d860` performs the batched `BasicBlock::removeInstruction` deletions. *(CONFIRMED — map key type, `memset` clear, and `removeInstruction` all byte-anchored.)*

**`branch_hint` (L36, `BranchHint::run(bir::Module&) @0x16913b0`)** stamps a static-prediction `InstBranchHint` onto branches so the SP sequencer can prefetch the predicted target. Gated by the global `enableBranchHint` (`.bss 0x3df7fa0`). `run()` builds a `CFG`, and for each branch terminator queries `CFG::isBackEdge(from, to)` — the heuristic is "predict the loop back-edge **taken**" — then calls `BranchHintImpl::insertBranchHint @0x1694060`, which inserts the hint via `NamedObjectContainer::insertElement<InstBranchHint>` at `getFirstWithin` of the predicted target region. The `InstBranchHint` carries a `BranchOutcomeHint ∈ {0,1}` = not-taken / taken (an `InstSyncType=sequencer` op); back-edge ⇒ taken=1. *(STRONG→CONFIRMED — `CFG::isBackEdge` + `insertElement<InstBranchHint>` are the load path; the non-loop fallback prediction value is INFERRED = not-taken.)*

This trio brackets the VNC link in the L-band roster (`L24 expand_inst_late → L25 mem2reg → L26 seq_inst_opt → … → L34 vnc_remote_addr_map → L35 vnc_link → L36 branch_hint → L37 expand_all_engine_final_pre_codegen → L40 codegen`).

## Confidence Ledger

**CONFIRMED (disasm / symbol grounded):**
- All symbol names and addresses (IDA `names.json`; `libBIR.so` `nm -DC`).
- L34 alias copy — `allocate_no_check` argument harvest, every operand from the remote location, `this` = local alias (disasm `@0x164e5bc`–`0x164e635`). **The single strongest claim; byte-anchored at the call site.**
- L34 precondition (`vnc_nc_count` at `+420`, `ErrorCode=1513`, source `:19`), the `node.kind==16` + `getRemoteLocalTarget.engaged` filter, the `getFunctionByName → getMemoryLocationByName` resolve with fallback, `ErrorCode=1514` (`remote_mloc != nullptr`, source `:43`).
- L35 collect (`InstDMABlock` + `isRemoteUpdateInstruction`), all-equal check (`ErrorCode=1512`, `"allEqual"`, source `:92`, `fort::table` report), the relink (`getSyncInfo` at +88, updates at +0x40/count +0x48 stride 32, `isRemote`/`getTargetCore`/`getId`, write-back via `SmallVectorImpl::operator=`), and the `Update` ctor arg map — only `id` from the peer (call `@0x164992e`), `ErrorCode=1511` (relink failure, source `:103`).
- The `ErrorCode` enum values are the **`NeuronAssertion` arguments** (`esi` immediates: 1509=`0x5E5`, 1511=`0x5E7`, 1512=`0x5E8`, 1513/1514), distinct from the `.cpp:NN` source-line path strings; the L35 pairing is `:25↔1509`, `:92↔1512`, `:103↔1511`.
- `RemoteLocalTarget` = `optional<pair<string,uint>>` at `MemoryLocation +0x170/+0x178/+0x198`; `isRemote = *(byte*)(this+0x170)`.
- `MemoryAddressSpace` enum `{Local=0, Shared=1, Debug=2}` (`libBIR` string table).
- Producers `DRAM_Allocator::mark_remote @0xa76080` and `LowerLocalCollectives::getMemoryLocation @0x161ab30` (the two `setRemoteLocalTarget` callers); the `"Skipping shared tensor allocations…"` / `"based on its remote local target"` strings present in the binary. The `"Replace finishing CoreBarrier…by DMACopy"` string is present (`@0x1d7fef0`) but emitted by `sub_7E0F10`, **not** by `VncLink::run`.
- `mem2reg` / `seq_inst_opt` algorithm structure.

**STRONG (multiple corroborating strings + structure):**
- HW = same-die `Shared` (DRAM/HBM) window + DMA-carried remote semaphore (the verbatim-copy reading of L34, the DRAM-only negative from the LNC page, the DMA-notification symbols).
- `getRemoteNotification` vector = cross-core notify list.
- `branch_hint` back-edge-taken heuristic.

**INFERRED:**
- Storage/entry-kind value `16` (`0x10` CONFIRMED; enum-name unknown).
- L34 reuses an already-global address (verbatim copy, no base+offset add *in this pass*); the base+offset was realized earlier at owner allocation into `Shared`.
- `branch_hint` non-loop fallback = not-taken default.

**UPSTREAM-OF-BINARY (cannot be pinned from these `.so`s):**
- Physical base of each per-core `Shared` HBM window (runtime/firmware constant).
- Provenance of the owner-core index in `mark_remote`'s pair (`DRAM_Allocator` internal state).

## Cross-References

- [The DRAM Allocator — Interval Tree and Shared-DRAM Partitioning](dram-allocator.md) — `DRAM_Allocator::mark_remote` (the producer of `RemoteLocalTarget`), `getRemoteLocalTarget`, and `sync_shared_allocations` (the earlier address-equalization this pass finalizes post-link).
- [The multi-core (LNC) memory model](../arch/lnc-memory-model.md) — `N` cores, DRAM partitioned with one shared cross-core region, and the verified negative that no core can name another core's SBUF/PSUM — the architectural basis for "cross-core = shared HBM only".
- [The LNC Splitter](lnc-splitter.md) — produces the per-core module vector (`modules.size() == vnc_nc_count`) these passes consume.
- [Backend Pass Registry](backendpass-registry.md) — where `vnc_remote_addr_map` and `vnc_link` register.
- [Pass Pipeline and Optimization Levels](pass-pipeline-optlevels.md) — the L-band ordering and the single-vNC constraint that gates these passes off at opt0/opt8/LSA.
- The collectives lowering (`LowerLocalCollectives`, the second `RemoteLocalTarget` producer) and the distribution / multi-device story are covered in their own Part 8 / Part 13 pages.
