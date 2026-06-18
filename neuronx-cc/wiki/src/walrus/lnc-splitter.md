# lnc_splitter / expand_replication — Multi-Core Graph Split

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 share the `.text` logic). The two passes live in `neuronxcc/starfish/lib/libwalrus.so`; the BIR primitives they call (`saveJson`/`loadJson`, `evalMask`, `QuasiAffineExpr::update_shard_id`) are externs resolved from `libBIR.so`. For `.text` (base `0x62d660`) and `.rodata` (base `0x1c72000`) the virtual address equals the file offset; `.data` carries a `+0x400000` delta (not used here). Other wheels differ — treat every address as version-pinned.*

## Abstract

A Trainium kernel compiled for **N logical NeuronCores** (an *LNC* program of size `lnc_size`) starts life as **one** symbolic program. The instruction set never mentions a concrete core: cross-core spans are written with a symbolic **shard-id** — a `pelican` affine index that stands for "this core's slot" — and any op that should fire only on some cores carries an **affine mask** over that shard-id. Two backend passes turn that single symbolic module into `lnc_size` concrete per-core programs, and they must run in a fixed order:

- **`expand_replication`** (`ExpandReplication::run` @ `0x1553500`, pipeline order 7) — a *single-module* normalization. It grows replicated IFMAP buffers and access patterns by the replication factor and inserts explicit replicated `Copy` ops, **on the one symbolic module, before any clone**. After it runs, replication is explicit and symbolic.
- **`lnc_splitter`** (`LncSplitter::run(vector<…Module>&)` @ `0x16d5ca0`, pipeline order 8) — the *multi-module* worker. It **clones** the symbolic module `lnc_size` times and **concretizes** each clone to a concrete core index: it stamps a `NeuronCoreId`, binds every symbolic shard-id to the core number, and **prunes** instructions whose affine mask evaluates false on that core.

The headline model is **clone → concretize → prune**. Cloning is a JSON serialize/deserialize round-trip, so the `lnc_size` copies are structurally byte-identical until concretization diverges them; concretization binds the shard symbol and deletes off-core ops; the result is `lnc_size` SPMD-identical control skeletons that differ only in shard-dependent fields. The ordering is not incidental: running replication **after** the split would force each clone to replicate independently and break that SPMD identity. The output `vector<Module>` is exactly what the concretized `lnc_verifier` then legality-checks (cross-ref [13.9 `AwsNeuronLNCShardingConstraint`], the planned `distribution/` page; the symbolic↔concretized verifier split is a sibling concern).

For reimplementation, the contract is:

- `lnc_size` is cached on the pass object at `+0x60` (from `PassOptions+0x1A4`), and the **`lnc==1` fast path skips the whole clone loop** — a single-core module is never given a concrete `NeuronCoreId` and stays symbolic.
- The clone is a `saveJson → stringstream → loadJson` round-trip (`cloneModule` @ `0x16d3820`), and the **last core reuses the original module in place** (one allocation saved).
- Concretization order: `setAttribute(NeuronCoreId)` → per-core rename `nc_symbolic → nc{id}` → walk every instruction → bind shard-id + decide keep/delete via `evalMask` → remove the deleted ops → clear each function's symbolic shard-id set.
- The prune predicate `evalMask` is the **same** affine-mask evaluator the symbolic verifier uses; the splitter *materializes* per core what the verifier *predicts*.

| | |
|---|---|
| **Split worker** | `LncSplitter::run(vector<unique_ptr<bir::Module>>&)` @ `0x16d5ca0` |
| **Abort shim** | `LncSplitter::run(bir::Module&)` @ `0x16d2f50` (`__noreturn`, throws code 738) |
| **Clone** | `LncSplitter::cloneModule(Module const*)` @ `0x16d3820` (JSON round-trip) |
| **Concretize module** | `LncSplitter::concretizeModule(Module*, ulong coreId)` @ `0x16d3bf0` |
| **Concretize instruction** | `LncSplitter::concretizeInstruction(Instruction&, ulong)` @ `0x16d23d0` |
| **Concretize argument** | `LncSplitter::concretizeArgument(Argument&, ulong)` @ `0x16d1170` |
| **Ctor (caches `lnc_size`)** | `LncSplitter::LncSplitter(PassOptions const&)` @ `0x16d6240` (→ `this+0x60`) |
| **Replication normalizer** | `ExpandReplication::run(bir::Module&)` @ `0x1553500` (pipeline order 7) |
| **`lnc_size` field** | `PassOptions+0x1A4` (dword 420) → `LncSplitter+0x60`, default `1` |
| **Module size** | 784 bytes (`tc_new(784)` in clone; `operator delete[](…, 0x310)`) |
| **Shard-bind primitive** | `bir::QuasiAffineExpr::update_shard_id(long)` @ `0x3fda438` (libBIR) |
| **Prune predicate** | `bir::Instruction::evalMask(DenseMap<AffineIdx*,long>&)` (libBIR) |

The naming, `.cpp:line` anchors, and the `^nc[0-9]{2}$` module-name regex are read from `.rodata` (`"lnc_splitter"`, `"expand_replication"`, `"nc_symbolic"`, `"^nc[0-9]{2}$"`, `"(nc[0-9]+)/sg[0-9]+"`); the source filenames `lnc_splitter.cpp` / `expand_replication.cpp` appear in the assertion strings. The 8 logical NeuronCores and the symbolic-shard address model are described in [the multi-core (LNC) memory model](../arch/lnc-memory-model.md).

## 1. Where `lnc_size` comes from, and the `lnc==1` fast path

`lnc_size` is a single dword in `PassOptions` at offset `0x1A4` (dword index 420), defaulting to `1` for an ordinary single-core compile. The `LncSplitter` constructor caches it onto the pass object so the per-module split loop never re-reads `PassOptions`. The ctor tail at `0x16d6240` is unambiguous:

```c
// LncSplitter::LncSplitter(PassOptions const& opts) @ 0x16d6240  [CONFIRMED]
result = *((unsigned int *)opts + 105);   // 105 * 4 = 0x1A4 = lnc_size
*((_QWORD *)this + 12) = result;           // this + 12*8 = this+0x60 = cached lnc
```

`run(vector)` then reads `this+0x60` (`v7 = *((_QWORD *)a2 + 12)`, disasm `mov rsi,[rsi+0x60]` @ `0x16d5cbc`) as the per-module replication factor. This ties the splitter's clone count to the **same** `PassOptions+0x1A4` field the verifier gate reads, so the two passes can never disagree on `N`. **[CONFIRMED — ctor body + run body.]**

The `lnc==1` case is a hard fast path: the entire clone/concretize loop is guarded by `if (v7 != 1)`. A single-core module therefore **passes through untouched** — it is never cloned, never renamed, and never given a concrete `NeuronCoreId`. This is precisely why downstream code treats `getNeuronCoreId().has_value() == false` as the signal "this module is still symbolic." **[CONFIRMED — `if (v7 != 1)` guard in `run(vector)`.]**

## 2. `LncSplitter::run(vector)` @ `0x16d5ca0` — the clone + concretize loop

The pass manager dispatches the splitter on the *vector* of subgraph modules (`M` modules, one per subgraph after the subgraph-fork). The output is `M · lnc_size` modules grouped `[origModuleIdx][coreIdx]`, with slot index `coreIdx + origModuleIdx·lnc_size`. The body — decompiler register names preserved — places clones into a freshly allocated `M·lnc` pointer block:

```c
// LncSplitter::run(__int64 result, this, vector& inMods) @ 0x16d5ca0  [CONFIRMED]
lnc   = *((_QWORD *)this + 12);              // this+0x60, cached lnc_size
M     = (inMods.end - inMods.begin) >> 3;     // #unique_ptr<Module>
out   = tc_new(8 * M * lnc); memset(out, 0, …);  // zeroed pointer block, M*lnc slots
if (lnc != 1) {                               // multi-core only
  coreIdx = 0;
  while (begin != end) {                      // cores 0 .. lnc-2  (last core handled below)
    for (m = 0; m < M; ++m) {
      clone = cloneModule(inMods[m]);          // JSON round-trip deep clone  (§3)
      concretizeModule(clone, coreIdx);        // specialize → core coreIdx    (§4)
      slot  = &out[coreIdx] + m*lnc;           // = out[coreIdx + m*lnc]
      destroy(*slot); *slot = clone;           // place (free any prior tenant)
    }
    if (++coreIdx >= lnc - 1) break;           // stop before the last core
  }
  // LAST core reuses the ORIGINAL module in place — no clone, one alloc saved:
  for (m = 0; m < M; ++m) {
    orig = move(inMods[m]); inMods[m] = 0;
    concretizeModule(orig, lnc - 1);           // specialize original → last core
    slot = &out[ (m+1)*lnc - 1 ];              // = out[ m*lnc + (lnc-1) ]
    destroy(*slot); *slot = orig;
  }
  drain(inMods);                               // destroy any residual originals
}
inMods._M_range_insert(out);                   // splice out[] back into the caller vector
result.status = 0; result.string = "";         // BackendPass success
```

The two clone-vs-reuse halves are visible directly in the decompiled body: the inner `cloneModule(v62, a2)` → `concretizeModule(a2, v62[0], v11)` pair with slot `&v58[v11] + v12*v13` for cores `0 .. lnc-2` (`++v11 >= *((_QWORD*)a2+12) - 1` exits the outer loop), then the `LABEL_12` block that does `concretizeModule(a2, v62[0], lnc-1)` on the moved-out original with slot `&v58[++v18 * lnc - 1]`. The `operator delete[](…, 0x310u)` on every freed slot pins **Module size = 784 bytes** (`0x310 == 784`, matching `tc_new(784)` in §3). **[CONFIRMED — full body, both loops, slot arithmetic.]**

> **GOTCHA — the last core is the *original*, not a clone.** Cores `0 .. lnc-2` get fresh JSON clones; core `lnc-1` is the source module object concretized in place. All `lnc` copies are nonetheless serialize-identical *at the moment of concretization* (the original has not yet been mutated when the clones are taken), so the SPMD-identity the verifier checks holds regardless of which copy is the original.

> **GOTCHA — `0x16d2f50` does no splitting.** The task index and some prior notes cite `LncSplitter::run(bir::Module&)` @ `0x16d2f50`. That overload is a `__noreturn` **abort shim**: it builds `NeuronAssertion<ErrorCode>` code **738** with message `"false"` at source `lnc_splitter.cpp:162` and `throw_with_nested`s it. It exists only to satisfy the `BackendPass::run(Module&)` single-module virtual and to hard-fail if the pass manager ever dispatches the splitter on a single `Module` instead of the multi-module `vector`. The real work is the `vector` overload @ `0x16d5ca0`. **[CONFIRMED — body builds 738/"false"/`lnc_splitter.cpp:162`, `__noreturn`.]**

## 3. `cloneModule` @ `0x16d3820` — a JSON round-trip deep clone

The clone is **not** a field-copy or a `Module` copy-ctor. It allocates a fresh module, serializes the source to JSON text in a local `stringstream`, then deserializes that text back into the new module:

```c
// LncSplitter::cloneModule(bir::Module const* src) @ 0x16d3820  [CONFIRMED]
cloneModule(src):
  std::stringstream ss;
  newMod = (bir::Module*) tc_new(784);
  bir::Module::Module(newMod);            // default-construct
  bir::Module::saveJson(src, ss);          // serialize src → JSON text
  bir::Module::loadJson(newMod, ss);       // deserialize JSON text → newMod
  return newMod;                           // structurally == src, fresh object
```

Because the clone passes through the BIR JSON schema, it faithfully reproduces **every** serialized field — functions, basic blocks, instructions, arguments, access patterns, memory-location sets, shard-id symbols, and attributes — except those that `concretizeModule` subsequently overwrites. All `lnc` clones come from the same source, so they are byte-identical until concretization diverges them. This is the structural precondition the verifier's shape-match assertions (function/BB count and names match core 0) rely on, and it is the same round-trip the pipeline's standalone `verifyBirSerDes` invariant pass exercises. **[CONFIRMED — `tc_new(784)`, `saveJson`/`loadJson` calls in body.]**

## 4. `concretizeModule` @ `0x16d3bf0` — specialize a clone to one core

Concretization runs four phases in order on the clone for `coreId`.

**(4a) Stamp the core id.** At entry the module gets a concrete `NeuronCoreId` via `bir::Module::setAttribute(ModuleAttribute=2, variant<long>=coreId)`. Attribute key `2` is `NeuronCoreId` — the same key `bir::Module::getNeuronCoreId` reads, whose `has_value()` flips `true` only after this call. **[CONFIRMED for `setAttribute`; key-`2`==`NeuronCoreId` is INFERRED-STRONG from the get/set pairing — the `ModuleAttribute` enum body is libBIR-side.]**

**(4b) Per-core rename.** `getName()` is scanned for the substring `"nc_symbolic"` and the module is renamed to the concrete `"nc"+coreId` via `setName()`; `setArtifactAbsPath()` is rewritten to a per-core output directory, guarded by `std::filesystem` `exists` / `is_directory` / `create_directory` assertions (codes 1648/1649/1650). Module names follow `^nc[0-9]{2}$` and pair with subgraph ids as `(nc[0-9]+)/(sg[0-9]+|sgLnk)` — i.e. `nc00`, `nc01`, … (regexes `.rodata`-confirmed). **[CONFIRMED — `getName`/`"nc_symbolic"`/`setName`/`setArtifactAbsPath`/`create_directory` all present in body.]**

**(4c) Walk, concretize, prune.** For every function (function list at `Module+560`), every basic block, every instruction:

```c
// concretizeModule walk  @ 0x16d3bf0  [CONFIRMED]
for (inst in all functions/BBs):
    if ( !concretizeInstruction(this, inst, coreId) )   // returns 1 ⟺ DELETE
        deleteList.push_back(inst);                       // collected, not yet removed
for (inst in deleteList):
    inst.parentBB->removeInstruction(inst);               // prune off-core ops
for (fn in functions):
    fn.shardIds.clear();                                  // empty the ShardId DenseSet
```

The decompiled body shows the deletion test as `if ( !concretizeInstruction(this, inst, coreId) )` — note the `!`: `concretizeInstruction` returns the **delete** decision, so the `!` selects the *kept* ops to skip, and the false branch schedules the rest for `removeInstruction` (@ `0x16d5364` in the body, `bir::BasicBlock::removeInstruction(parentBB, inst)`). After the walk, each function's `pelican::RefPtr<pelican::ShardId>` `DenseSet` is cleared, so no residual symbolic shard-id survives in any function. **[CONFIRMED — `concretizeInstruction` calls, `removeInstruction`, ShardId-set `initEmpty` all in body.]**

**(4d) The prune *is* the SPMD selection.** An instruction whose affine mask is true only on certain cores survives only on those cores' clones; an op active on all cores (a `CoreBarrier`, a cross-core `SB2SB`) survives identically on every clone by construction. There is no separate "which cores does this run on" table — it falls out of evaluating the mask per core.

> **POLARITY — disasm-verified.** `concretizeModule` does `call concretizeInstruction; test al,al; jz <skip-add-to-removal>`. `concretizeInstruction` returns `evalMask() ^ 1`. Therefore an instruction is **removed ⟺ `evalMask()==0` ⟺ the affine mask is FALSE on this core ⟺ the op does not fire on `coreId`**. Kept ⟺ `evalMask()==1`. This is the canonical SPMD predicate-prune; any reading that "the splitter keeps every op on every core" is wrong. **[CONFIRMED.]**

## 5. `concretizeInstruction` @ `0x16d23d0` — shard-bind + mask decision

Per instruction, the routine reads the instruction type at `inst+0x58` and branches:

```c
// LncSplitter::concretizeInstruction(Instruction& inst, ulong coreId) @ 0x16d23d0  [CONFIRMED]
v6 = *((int*)inst + 22);                 // inst+0x58 = InstructionType
if (v6 == 105) {                          // IT105 = nested-region container
    keep = 1;
    for (bb in inst.nestedBlocks())        // recurse into the region body
      for (child in bb.instructions())
        keep &= concretizeInstruction(child, coreId);   // AND children's KEEP
    return keep ^ 1;                        // region deleted iff EVERY child deleted
}
if (v6 == 48) {                            // IT48 = collective / shard-carrying op
    for (qae in inst.shardDims[+0x1E0 .. +0x1E8])
        qae.update_shard_id(coreId);        // pre-bind its own AP shard dims
    ((QuasiAffineExpr*)(inst+504)).update_shard_id(coreId);
}
for (arg in inst.inputs)      concretizeArgument(arg, coreId);   // bind shard-id in every
for (arg in inst.outputs)     concretizeArgument(arg, coreId);   // argument's access pattern (§6)
for (arg in inst.controlArgs) concretizeArgument(arg, coreId);
for (pred in inst.predicates[+0x78 .. +0x80]) {
    pred.update_shard_id(coreId);           // bind shard symbol → coreId
    if (!AffinePredicate::eval(pred, DenseMap))   // predicate false on this core?
        inst.predicates.erase(pred);        // drop the now-dead predicate
}
return bir::Instruction::evalMask(DenseMap<pelican::AffineIdx*, long>) ^ 1;
       // ^1  ⇒  return value is "should this instruction be DELETED on core coreId"
```

Three confirmed details worth pinning. First, the **IT105 region recursion** AND-folds its children: the body's `v7 &= v14` over each child's result means a region is kept iff some child is kept (it is pruned only when *all* its children prune away). Second, the **IT48** pre-bind of the op's own access-pattern dims at `inst+0x1E0..0x1E8` and `inst+504` happens *before* the generic argument walk, because a collective op carries shard-dependent geometry on the instruction itself. Third, the return is `evalMask() ^ 1`, the source of the §4 prune polarity. The `DenseMap<pelican::AffineIdx*, long>` passed to `evalMask`/`AffinePredicate::eval` is the **same** shard-id→core binding map the symbolic verifier builds — the splitter realizes per core what the verifier counterfactually evaluates. **[CONFIRMED — IT read at `+0x58`, IT105 recurse with `&=`, IT48 `update_shard_id`, `eval` @ body, `evalMask() ^ 1` return.]**

## 6. `concretizeArgument` @ `0x16d1170` — local address specialization

Argument concretization is where the symbolic shard-id is resolved into a concrete per-core **local** address. It fires only on `SymbolicAccessPattern` arguments (kind tag `2` at `arg+0x18`):

```c
// LncSplitter::concretizeArgument(Argument& arg, ulong coreId) @ 0x16d1170  [CONFIRMED]
if (*((int*)arg + 6) == 2) {                 // arg+0x18 == 2  ⇒ SymbolicAccessPattern
    for (qae in arg.apDims[+0xD8 .. +0xE0])
        qae.update_shard_id(coreId);          // each access-pattern dim expr
    if (SymbolicAccessPattern::getTileAddrs(arg))
        getTileAddrs(arg).update_shard_id(coreId);   // tile base-address expr
    if (SymbolicAccessPattern::getBlockAddrs(arg))
        getBlockAddrs(arg).update_shard_id(coreId);  // block base-address expr
}
```

Every shard-id-dependent component of a symbolic access pattern — the partition/index dims **and** the tile/block base-address expressions — has its shard-id symbol bound to the concrete `coreId`. After this, the access pattern addresses a concrete per-core SBUF/PSUM/DRAM region. Cross-core **remote** addresses (`RemoteLocalTarget`, `vnc_remote_addr_map`) are *not* minted here — those are the job of the bracketing collective-lowering passes (`LowerLocalCollectives::createRemoteAP`/`getMemoryLocation`; cross-ref [8.31 `extend-shared-lifetimes`], the planned `walrus/` page, and the planned `walrus/bir_linker` re-assembly counterpart, 8.33). This function resolves only the **local** per-core addressing. **[CONFIRMED — kind test `arg+0x18==2`, `update_shard_id` over dims/TileAddrs/BlockAddrs.]**

## 7. `expand_replication` @ `0x1553500` — why it runs first (order 7)

`ExpandReplication::run(Module&)` is a single-module normalization that runs on the symbolic module **before** `lnc_splitter` ever clones it. Its job is to make multi-LNC replication explicit and symbolic on the one module, so the splitter only has to clone-and-concretize an already-replicated graph.

```c
// ExpandReplication::run(bir::Module& mod) @ 0x1553500  [CONFIRMED]
main = mod.getFunctionByName("main");
visitor = MatmultVisitor();                         // collects InstMatmult* per BB
if (main) { visit(main.blocks);                      // main first
            for (fn in mod.functions if fn != main) visit(fn.blocks); }
else      { for (fn in mod.functions) visit(fn.blocks); }
// visitor → DenseMap<MemoryLocationSet*, ReplicationParams>, keyed by each
// replicated matmult's arg-0 SymbolicAccessPattern MemoryLocationSet.
for (entry in replMap)                               // skip DenseMap empty/tombstone
    if (entry.key != -4096 && entry.key != -8192)     // sentinels for empty/deleted bucket
        increase_ifmap_memset_size(entry.key, entry.value);
```

`increase_ifmap_memset_size` @ `0x1552900` is the heavy lifter (Hex-Rays failed on it; read from the disasm call sequence): it `getTensorShape → setTensorShape` to grow the buffer, `updateDimensions` to resize the IFMAP dimension, then for each reader/writer argument either `offset_replicated_ap` (@ `0x15505f0`, shift this replica's AP) or `expand_replicated_ap` (@ `0x154ff50`, widen the AP partition span), and `replace_with_replicated_copy` (@ `0x1551850`, emit an explicit replicated `Copy`). `expand_replicated_ap` literally adds the replication factor to the leading AP-dim count and stride (`*(_QWORD*)src += a4; *((_QWORD*)src+3) += a4`) so the access pattern spans all replicas. **[CONFIRMED for `run` body / `getFunctionByName("main")` / MatmultVisitor / sentinel skip; STRONG for `increase_ifmap_memset_size` internals — decomp failed, read from disasm call order.]**

> **ORDERING — `expand_replication` (7) MUST precede `lnc_splitter` (8).** Replication is made explicit and symbolic on the *one* module: buffers sized for all replicas, access patterns spanning the replica partition, replicated copies inserted as ordinary shard-predicated instructions. The splitter then only clones + concretizes, and each core's clone takes its slice of the already-grown buffers by binding the shard symbol. If replication ran **after** the split, each of the `lnc_size` clones would replicate independently — risking divergent buffer shapes and instruction counts, which is exactly the SPMD identity the concretized verifier checks. Running expansion first is what *makes* the splitter output legal. **[CONFIRMED for the mechanism; ordering STRONG via the pass-pipeline roster — orders 7/8 from [`pass-pipeline-optlevels.md`](pass-pipeline-optlevels.md).]**

## 8. End-to-end: single symbolic → N physical

```text
[symbolic module: shard-ids are pelican AffineIdx symbols; module name *nc_symbolic*]
   │   order 3-4: birverifier (per-op) + lnc_verifier (SYMBOLIC) —
   │              "is this graph legally splittable into lnc_size replicas?"
   ▼
expand_replication (order 7, ExpandReplication::run @ 0x1553500, ONE module):
   • MatmultVisitor → ReplicationParams per replicated IFMAP MemoryLocationSet
   • increase_ifmap_memset_size: grow buffer shapes for all replicas
   • expand/offset_replicated_ap: widen / shift access patterns
   • replace_with_replicated_copy: emit explicit replicated Copy ops
   ▼
[symbolic module, replication now EXPLICIT — still ONE module, still symbolic]
   ▼
lnc_splitter (order 8, LncSplitter::run(vector) @ 0x16d5ca0):
   for coreId in 0 .. lnc-1:
       clone = (coreId < lnc-1) ? cloneModule(orig)   // JSON round-trip (§3)
                                : move(orig)            // last core reuses original
       concretizeModule(clone, coreId):                // (§4)
           setAttribute(NeuronCoreId=2, coreId)         // (4a) stamp core id
           rename nc_symbolic → nc{coreId}              // (4b)
           for inst in all functions/BBs:
               del = concretizeInstruction(inst, coreId)   // (§5) bind shard-id; del = !evalMask
               if del: schedule inst for removal
           removeInstruction(each scheduled)            // (4c) prune off-core ops
           clear each function's ShardId set            // (4c)
       out[coreId + m*lnc] = clone
   ▼
[vector of lnc_size per-core modules: concrete NeuronCoreId, no symbolic shard-ids,
 SPMD-identical control skeletons, per-core LOCAL addresses]
   │   (cross-core collective lowering — LowerLocalCollectives / ExtendSharedLifetimes:
   │    enumerate peer cores; mint RemoteLocalTarget cross-core addresses + barriers)
   │   order 13-14: birverifier (per-op, per core) + lnc_verifier (CONCRETIZED)
   ▼
[N physical per-core programs, legality-gated]
```

The split's output is the `vector<Module>` the concretized `lnc_verifier` checks. Each of its shape assertions is satisfied by a specific splitter act: the `NeuronCoreId` stamp (4a) gives every clone a concrete core; the JSON clone (§3) makes function/BB counts and names match core 0; the per-function shard-id clear (4c) leaves no residual symbolic shard; the shard-uniform prune (§4d) keeps the barrier and cross-core-`SB2SB` inventories identical across cores. The symbolic verifier (orders 3-4) proves splittability **before** the split using the *same* `evalMask` primitive the splitter (§5) then uses to prune — so the prediction and the realization share one affine-mask evaluator.

## 9. Verification ceiling

| Claim | Status | Evidence |
|---|---|---|
| `run(vector)` clone+concretize loop, slot `coreIdx + m·lnc`, last-core reuse, 784-byte module | CONFIRMED | full decompiled body; `0x310u` deletes; `if (v7 != 1)` guard |
| `0x16d2f50` is a `__noreturn` abort shim (code 738, `"false"`, `lnc_splitter.cpp:162`) | CONFIRMED | body builds `NeuronAssertion(738,…)` + `throw_with_nested` |
| `cloneModule` = `saveJson → stringstream → loadJson` round-trip | CONFIRMED | `tc_new(784)` + `saveJson`/`loadJson` calls in body |
| concretize 4 phases; prune polarity `removed ⟺ evalMask()==0` | CONFIRMED | `setAttribute`/`getName`/`nc_symbolic`/`setName`/`removeInstruction`; `!concretizeInstruction`; `evalMask() ^ 1` |
| `concretizeInstruction` IT read `+0x58`, IT105 AND-recurse, IT48 dispatch, `evalMask ^ 1` | CONFIRMED | body: `*((int*)a2+22)`, `v7 &= v14`, `v6==48`, `return … ^ 1` |
| `concretizeArgument` SAP kind `2`, bind dims/TileAddrs/BlockAddrs | CONFIRMED | body: `*((int*)a2+6)==2`, `update_shard_id` ×3 |
| `expand_replication` order-7-first; `getFunctionByName("main")`→MatmultVisitor→ReplicationParams | CONFIRMED (mechanism) / STRONG (ordering) | run body + sentinel skip; orders from pass roster |
| `lnc_size = PassOptions+0x1A4 → LncSplitter+0x60`, default 1 | CONFIRMED | ctor `*((uint*)opts+105)` → `this+12` |
| `ModuleAttribute` key `2 == NeuronCoreId` | INFERRED-STRONG | `setAttribute(2,…)` ↔ `getNeuronCoreId` read; enum body libBIR-side |
| `increase_ifmap_memset_size` / `replace_with_replicated_copy` internals | STRONG | Hex-Rays decomp failed; read from disasm call sequence |
| `evalMask` / `update_shard_id` / `saveJson` AffineIdx→core mechanics | MED | libBIR externs (8-byte stubs here); internal binding is libBIR-side |

The five strongest claims (the clone+concretize loop, the abort shim, the JSON-clone round-trip, the `evalMask ^ 1` prune polarity, and the expand-replication-first ordering) were each re-checked against the decompiled body and disasm; all hold. The honest ceiling is the libBIR boundary: the exact internal mechanics of `evalMask`, `update_shard_id`, and the JSON serde live in `libBIR.so` and are reached here only as externs, and the `ModuleAttribute` enum value `2` is inferred from the get/set pairing rather than read from an enum table.
