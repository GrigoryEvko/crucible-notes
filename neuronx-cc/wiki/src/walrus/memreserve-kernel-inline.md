# Runtime Memory Reservation and Kernel Inlining — EvalAccelReservedLoc, NKI-via-JSON, BIR-via-Factory

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical Cython rebuilds). Everything documented here lives in `neuronxcc/starfish/lib/libwalrus.so`. For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; `0x5e9020`–`0x62d650` is `.plt` thunks — every address cited is the real high-frame `.text` body, never the `0x5e…`/`0x60…` mirror thunk. Symbols beginning `bir::` (`Function::addMemoryLocationWithMemoryLocationSet`, `MemoryLocation::setAllocated`, `Module::removeFunction`, `Function::toJson`) are imported (`U`) from `libbir`. Other wheels differ — treat every address as version-pinned.*

## Abstract

Three backend passes run back-to-back around the SBUF/PSUM colorers, and all three *materialize* something the front end only promised. **`RuntimeMemoryReservation`** (`run` `@0xb68040`, pass order **39**) carves out one fixed SBUF region named **`EvalAccelReservedLoc`** before the allocator places anything else, so a runtime/accelerator-evaluation structure lands at a predictable address. **`InlineNKIKernel`** (`run` `@0xf028d0`, order **41**/**61**) and **`InlineBIRKernel`** (`run` `@0xd86510`, order **48**/**60**) both replace a single kernel *call-site instruction* with the kernel's expanded body — but by two completely different mechanisms, and that contrast is the headline of this page.

An NKI kernel (`InstructionType == 55`, `InstNKIKernel`) is KLR/NKI-AST-sourced. It cannot be spliced as-is: `InlineNKIKernel::lowerKernelInst` (`@0xefddc0`) builds a throwaway single-function `bir::Module`, **serializes the kernel function to JSON** (`bir::Function::toJson` + `nlohmann::adl_serializer<bir::Module>::to_json`), round-trips it back to re-canonicalize, runs `Unroll` / `LowerGenericIndirect` / `ConstantPropagate` on the temp module, then manually clones each instruction into the caller, re-bases its SBUF memory locations onto the call-site's output address, and rewires every operand by name (`rewireMemlocs` `@0xefa960`). A BIR kernel (`InstructionType == 54`, `InstBIRKernel`) is an *already-BIR* named library template (attention/MLP/activation/normalization). It needs no JSON, no KLR cleanup: `InlineBIRKernel::run` builds a polymorphic `BIRKernelWrapper` through a factory (`BIRKernelWrapper::createInstance` `@0xd855d0`) and drives the expansion through four virtual-table slots. **NKI = JSON round-trip + manual clone/splice; BIR = factory + vtable dispatch.** That is the single sentence to remember.

All three passes are registered as mod-parallel passes and consumed by the same downstream stage. The two inline passes each expose fresh per-channel collective-communication ops via their in-pass expansion (NKI's `Unroll`, BIR's template lowering), which is why `coalesce_multichannel_cc_ops` re-runs immediately after each (see [`address-rotation`](address-rotation.md)'s placement neighbours and the dependence model in [`dependence-graph`](dependence-graph.md)). The inline passes are the **backend counterpart** of the front-end kernel *provisioning* documented in [`codegen-helpers-kernel-provision`](../bir/codegen-helpers-kernel-provision.md): provisioning records the call-site and its argument-name bindings; these passes consume that record and materialize the body.

For reimplementation, the contract is:

- **The reservation:** single-Function (pre-linker) contract, the `arch generation > 29` gate, the two size paths (arch-capacity vs pinned-SBUF high-water-mark scan), and the dual recording (an allocated `MemoryLocation` *plus* the `runtime_reserved` bit at `+579`).
- **NKI inline:** the `IT==55` scan, the allocation-status consistency gate that makes the pass schedulable twice, the JSON re-materialize, the SBUF address-rebase of callee memlocs, the `:`/`_` name uniquification, and the name-keyed operand rebind.
- **BIR inline:** the `IT==54` scan, the `createInstance` factory, and the four-slot vtable drive (`+24` build, `+32` commit, `+16` result, `+40` cleanup) with **no** JSON and **no** KLR cleanup.

| | |
|---|---|
| **Reservation pass** | `neuronxcc::backend::RuntimeMemoryReservation::run(bir::Module&)` `@0xb68040` (end `0xb68b3e`) |
| **Reserved name** | `"EvalAccelReservedLoc"` (`.rodata` `@0x1c76658`) — one SBUF `MemoryLocation` |
| **Single-Fn guard** | `bir::reportError(false, "...should have only one Function per Module", srcloc)` (decomp line 154) |
| **Arch gate** | `*(int*)(Module + 0xAC) > 29` (decomp line 174: `> 29`) — newer-generation-only |
| **Size path 1** | `archModel` SBUF capacity − pass config `[+0x1E4]` (decomp line 196: `… - *(…+484)`) |
| **Size path 2** | `max(addr + aligned(size))` over `pinned`(`+0xF8`) ∧ `SB`(`type@+0xD8 == 16`) allocs |
| **`aligned(n)`** | `@0xb68020`: `(n&7) ? n + 8 - (n&7) : n` — round up to 8 bytes |
| **Record** | `Function::addMemoryLocationWithMemoryLocationSet` → `setAllocated(.,1)` → `+579 = 1` (`runtime_reserved`) → `buildTensorId2MemLoc` |
| **NKI driver** | `InlineNKIKernel::run(bir::Module&)` `@0xf028d0` — scans `IT==55` |
| **NKI inliner** | `lowerKernelInst(InstNKIKernel&, BasicBlock&, json&)` `@0xefddc0` (~18 KB) |
| **NKI rebind** | `rewireMemlocs(Instruction&, Instruction*, Function&, map×2, str)` `@0xefa960` (~13 KB) |
| **BIR driver** | `InlineBIRKernel::run(bir::Module&)` `@0xd86510` — scans `IT==54` |
| **BIR factory** | `BIRKernelWrapper::createInstance(Logger, InstBIRKernel*, BasicBlock*, bool)` `@0xd855d0` |
| **BIR wrapper** | ctor `@0xd8bb50`; `parseOpts` `@0xd873f0`; `initAndVerifyShapes` `@0xd87400`; `cleanup` `@0xd8dbc0` |
| **Registration** | `register_generator_runtime_memory_reservation__` `@0x3dff597`; `…inline_nki_kernel__` `@0x3e013e9`; `…inline_bir_kernel__` `@0x3e01380` (all mod-parallel) |

---

## Part 1 — Runtime Memory Reservation

`RuntimeMemoryReservation::run` (`@0xb68040`) reserves exactly one SBUF region for runtime/accelerator-evaluation scratch and gives it a fixed name so the allocator carves it out before placing live tensors. It is order **39**, immediately before the linear-scan / coloring allocators (order 40+), which is the whole point: the reserved high-water-mark is taken off the top of SBUF first.

### The single-Function (pre-linker) contract

`run` opens by walking the module's intrusive `Function` list and counting entries, then asserts the count is exactly one. The count loop sits in the prologue and its comparison drives the error branch at decomp line 154:

```c
// RuntimeMemoryReservation::run @0xb68040 (decomp lines ~143-154)
size_t n = 0;
for (Function *f = funcList.begin(); f != funcList.end(); f = f->next)
    ++n;
if (n != 1)
    bir::reportError(/*fatal=*/false,
        "this pass should run in pre-linker phase, which should have "
        "only one Function per Module",        // .rodata, 88 chars
        srcloc);
```

This pins the pass to the **pre-linker** phase: it must see the whole program as one function. The 88-char string is assembled from SSE-immediate stores, and `reportError` appears at decomp line 154.

### The arch-generation gate

```c
// decomp line 174
if ( *(int *)(Module + 0xAC) > 29 )   // 0xAC=172, 29=0x1d
    { /* create the reservation */ }
// else: no reservation is produced
```

`Module + 0xAC` is the arch *generation* / index. Only generations `> 29` (`0x1d`) get a reservation. The literal `> 29` is in the decompiled body; that the field is the arch generation index is [INFERRED], though it is the same field the rest of the backend reads as an arch selector.

### Computing the reserved size — two paths

The reserved name string is built and compared against `"EvalAccelReservedLoc"`; the comparison result selects the size path. The compared literal resolves to the reserved-loc name; the exact predicate is [INFERRED].

**Path 1 — arch-model SBUF capacity headroom** (`compare == 0`; decomp line 196):

```c
// reservedSize = per-arch SBUF partition capacity - pass config field
archModel    = getArchModel(arch);
sbCapacity   = *(u32*)( *(*(archModel + 8) + 0x10) + 0x28 );   // per-arch SBUF partition size
cfgOffset    = *(u32*)( pass[0x60] + 0x1E4 );                  // 0x1E4 = 484 (decomp: "- *(…+484)")
reservedSize = sbCapacity - cfgOffset;                          // store [rbp-0x200]
```

The decompiled expression at line 196 is exactly `**(…getArchModel(…)+8)+0x10)+0x28) - *(…+484)` — capacity minus the config offset at `+0x1E4`.

**Path 2 — pinned-SBUF high-water-mark scan** (`compare != 0`; decomp lines ~207-390): iterate `Function::allocs()` (a filtered/transformed range over the function's `MemoryLocationSet` intrusive list) and take the maximum aligned extent of every *pinned SBUF* location:

```c
// decomp line 357: if ( *(_BYTE *)v48[31] && *(_DWORD *)v48[27] == 16 )
u64 hwm = 0;
for (MemoryLocation &ml : fn.allocs()) {
    bool pinned   = *(bool*)(*(u64*)(&ml + 0xF8));   // +0xF8  pinned flag
    int  memType  = *(int *)(*(u64*)(&ml + 0xD8));   // +0xD8  MemoryType ptr (SB==16)
    if (pinned && memType == 16) {                   // 16 == SB
        u64 addr = ml.getAddress();
        u64 sz   = *(u64*)(*(u64*)(&ml + 0x108));    // +0x108 name/origin → byte size
        u64 ext  = addr + RuntimeMemoryReservation::aligned(this, sz);
        hwm      = max(hwm, ext);                     // running maximum reserved extent
    }
}
reservedSize = hwm;
```

The memory-taxonomy offsets (`+0xD8` `MemoryType`, `+0xF8` `pinned`, `+0x108` name/origin) are cross-checked against the BIR memory model; `MemoryType == 16` is SBUF. That the `+0x108` deref yields the byte size is [INFERRED].

`aligned(n)` (`@0xb68020`) is a verbatim round-up-to-8:

```c
// aligned @0xb68020 — full decompiled body
__int64 aligned(RuntimeMemoryReservation *this, __int64 n)
{ return (n & 7) ? (n + 8 - (n & 7)) : n; }
```

### Recording the reservation

The reserved extent is committed as a real `MemoryLocation` on the single function, then finalized with the `runtime_reserved` bit:

```c
// decomp lines ~405-421
v19 = bir::Function::addMemoryLocationWithMemoryLocationSet(
          /*name=*/"EvalAccelReservedLoc",
          /*uint=*/ archModel[0x1E8],     // tensor-id / partition-count field
          /*…*/,
          /*allocated=*/ 1,
          /*…optionals=0…*/,
          /*size=*/ reservedSize,         // [rbp-0x200]
          /*…MemoryType/AddressSpace/Dtype…*/ );

bir::MemoryLocation::setAllocated(v19, 1);                 // @0x60fe50
*(u8*)(v19 + 579) = 1;                                     // +579 = runtime_reserved
bir::MemoryLocationSet::buildTensorId2MemLoc(*(v19 + 0x160));  // rebuild tensorId→memloc index
```

So the reservation is recorded **twice over**: as an allocated `MemoryLocation` *and* by setting the per-location `runtime_reserved` flag at byte `+579`; the set's `tensorId → MemoryLocation` index is then rebuilt. The pushed-arg layout includes a literal `0x10`, consistent with an SBUF (`type 16`) `MemoryLocationSet` — matching the SB-only scan in path 2. The SBUF kind of the recorded set is [INFERRED] from that argument layout.

This pass writes no global `TotalRuntimeDramSize` counter — it produces one SBUF reservation. DRAM runtime accounting is the allocator's job (see [`dram-allocator`](dram-allocator.md), which is the DRAM-side counterpart).

---

## Part 2 — Inlining an NKI Kernel via JSON Round-Trip

`InlineNKIKernel::run` (`@0xf028d0`) replaces every `InstNKIKernel` call-site (`IT == 55`) with the expanded body of the NKI function it names. The NKI function is the one the front-end NKI-AST→BIR driver stored on the call-site at `inst + 0xF0`; after inlining, that function is deleted from the module — this pass is the exact consumer of the kernel-node producer in [`three-sink-kernel-model`](../nki/three-sink-kernel-model.md).

### The driver and its dual-placement gate

```c
// InlineNKIKernel::run @0xf028d0
sub_175AAF0(&path, "tensor_map.json", "");            // decomp line 235
Module[0x69] = !bir::needNoTensorMap();               // +105: must a tensor map be emitted?
if (Module[0x69]) loadJsonFile(&tensorMap, path);     // decomp line 259

for (Function &fn : moduleFns)
  for (BasicBlock &bb : fn.blocks())
    for (Instruction *inst : bb) {
      if ( *(int*)(inst + 0x50) != 55 ) continue;     // IT==55 InstNKIKernel ONLY (decomp 448)
      log("Found InstNKIKernel: %s", inst+0x10);      // name at +0x10

      // ── ALLOCATION-STATUS CONSISTENCY GATE (decomp 561-565) ──
      bool allocStatus = lookup(inst->attrMap, /*key-hash*/ 0xA);
      if ( Module[0x68] != allocStatus ) {            // +104 = pass-position bool (ctor arg)
          log("Skip lowering %s because the allocation status of the kernel "
              "is not consistent with the current location of the pass", name);
          continue;
      }

      lowerKernelInst(this, inst, bb, &tensorMap);    // decomp 572 — the actual inliner
      dropFns.push_back(*(Function**)(inst + 0xF0));  // the NKI Function to delete
      removeInsts.push_back(inst);
    }

// per BB: BasicBlock::removeInstruction(bb, eachInstNKIKernel)
// per Module: for each dropFn: log("dropping func %s"); Module::removeFunction(a3, dropFn); // decomp 659
if (Module[0x69]) bir::saveJsonFile();                // re-emit tensor_map.json (decomp 667)
```

The **allocation-status consistency gate** is why this pass is scheduled twice (order 41 *and* 61). The pass's `bool` constructor argument is stored at `Module + 104`; each call-site carries a recorded alloc-status attribute (key-hash `0xA`). A kernel is expanded *here* only if its recorded status matches the current pass position; otherwise it defers to the other placement. One pass body, two schedule slots, selected per-kernel at run time.

*Anchors: the `!= 55` test at decomp 448, the "Skip lowering" string at 561-565, `removeFunction` at 659, `saveJsonFile` at 667.*

### `lowerKernelInst` — the JSON re-materialize and manual splice

The inliner core (`@0xefddc0`, ~18 KB) is structured in eight stages. Its source path `walrus/inline_nki_kernel/src/inline_nki_kernel.cpp` and its assert sites are recovered from string literals.

**(a) JSON re-materialize** (decomp 586-604) — this is the NKI-specific step:

```c
// build a TEMP single-Function bir::Module, copy version+arch, JSON round-trip the kernel fn
bir::Module::setVersion(tmp, callerVersion);          // decomp 586
bir::Module::setArchAndHwm(tmp, …);                   // decomp 588
bir::Function::toJson(kernelFn, &j);                  // decomp 601  ← serialize to JSON
nlohmann::adl_serializer<bir::Module,void>::to_json(…, tmp);   // decomp 604
// …then parse j back into tmp to re-canonicalize.
```

Because the NKI kernel originates from KLR-sourced codegen, it must be re-canonicalized through a JSON parse before it is safe to splice. The JSON loader on the parse side is documented in [`json-loader`](../bir/json-loader.md). *Anchors: `toJson` and `adl_serializer<Module>::to_json` at decomp 601/604.*

**(b) KLR cleanup passes on the temp module** (decomp 993 / 1051 / 1159):

```c
neuronxcc::backend::Unroll::run(tmp);                 // decomp 993  — loop unroll → fresh per-channel CC ops
neuronxcc::backend::LowerGenericIndirect::run(tmp);   // decomp 1051 — indirect-AP / DGE lowering
neuronxcc::backend::ConstantPropagate::run(tmp);      // decomp 1159 — ctor takes pass[0x60] PassOptions
// dtors run at function exit (decomp 754-756) — all three are in-pass stack objects.
```

`Unroll` here is precisely what produces the fresh per-channel CC ops that `coalesce_multichannel_cc_ops` re-coalesces at order 42. All three `::run` calls and their destructors appear in the decompiled body.

**(c)–(h) callee fetch, operand binding, memloc rebase, clone/splice:**

```c
callee = bir::Module::getFunctionByName(tmp, name);   // decomp 1198

// (d) OPERAND BINDING — build two name maps from the call-site AP lists
unordered_map<string,string> inputMap, outputMap;
//   inputMap : over call-site INPUT AccessPattern list (inst[17] base, inst[17].cnt)
//   outputMap: over call-site OUTPUT AP list           (inst[19] base, inst[20].cnt)
//   keyed by callee param name → caller memloc name (the FunctionArgumentMaps)

// (f) MEMLOC RE-CREATION + SB ADDRESS REBASE (decomp 1731-1832)
for (MemoryLocationSet &set : callee.memlocSets()) {
    newSet = caller.addMemoryLocationSet("<instname>:<setname>");   // ":"-uniquified
    newSet.scope = 2;                                                // +328 = 2
    for (MemoryLocation &ml : set) {
        new = newSet.addMemoryLocation("<instname>:<mlname>");
        if (ml.memType == 16 /*SB*/) new.addr = callerOutputBaseAddr + ml.getAddress();
        else                         new.addr = ml.getAddress();
        new.updateAllocated(ml.allocatedFlag);
        // addr-space dirty bits from inst[22] = KERNEL SOURCE KIND (0/1/2);
        //   >2 → std::runtime_error("Unrecognized kernel source encountered.")  // decomp 1798
    }
}

// (g) PER-INSTRUCTION CLONE + SPLICE (decomp 2160-2543)
for (Instruction &t : callee.insts()) {
    clone = t.clone();                                  // name uniquified with "_"
    bb.spliceBefore(insertPt, clone);                   // llvm::ilist transferBeforeImpl
    insertIntoSymboltable(bb, clone);
    InlineNKIKernel::rewireMemlocs(this, &t, clone, callerFn, &inputMap, &outputMap, dbg);
    // unroll-dep rewiring: log("adding unroll dep from %s to %s")  // decomp 2269
    // branch retarget: clone sub-type +0x58 == 79 (UncondBranch) → retarget +0xF0;
    //                  == 78 (CompareAndBranch)  → retarget +0xF0 (false) and +0xF8 (true)
    clone->scope &= ~0xC;                               // clear scope bits
}

// (h) emit InstUnconditionalBranch "br_to_continuation" → caller continuation BB (+240)
```

The SBUF rebase in stage (f) is the key bookkeeping: callee SB allocations are **re-based onto the call-site's output SB address**, and the `:`-prefix uniquification prevents name collisions when the same kernel is inlined more than once. The `>2 → "Unrecognized kernel source encountered."` guard (decomp 1798) validates the kernel-source kind tag (`inst[22]`).

*Anchors: `getFunctionByName` at decomp 1198, "Unrecognized kernel source" at 1798, "adding unroll dep" at 2269.*

### `rewireMemlocs` — name-keyed operand rebind

For each operand (`PhysicalAccessPattern` or `RegisterAccess`) of a cloned instruction, `rewireMemlocs` (`@0xefa960`) looks the operand's memloc/register name up in `inputMap`/`outputMap`:

- **Bound argument** → resolve the *caller's* memloc by name (`Function::getMemoryLocationByName`) / register (`getRegisterByName`) and `PhysicalAccessPattern::setLocation` to it.
- **Callee-internal location** → resolved by its `:`-uniquified name.
- `rewireDynamicAPRegisters()` fixes dynamic access-pattern register references; the routine recurses into nested/compound instructions (self-call at decomp 1437).

This is the concrete realization of the front-end's `FunctionArgumentMap` name-bindings (recorded by `createMappingFromNames` in [`codegen-helpers-kernel-provision`](../bir/codegen-helpers-kernel-provision.md)): caller arguments are matched to callee parameters **by name**, then every operand's `AccessPattern` is re-pointed at the caller's (re-based) `MemoryLocation`.

---

## Part 3 — Inlining a BIR Kernel via Polymorphic Factory

`InlineBIRKernel::run` (`@0xd86510`) handles `InstBIRKernel` (`IT == 54`) — an **already-BIR** named library template (attention / MLP / activation / normalization). It needs no JSON, no KLR cleanup. Instead it builds a polymorphic `BIRKernelWrapper` through a factory and drives the splice through the wrapper's virtual table.

### The driver

```c
// InlineBIRKernel::run @0xd86510
log("BIR SB coloring allocator is %s",                  // decomp 220-224
    Module[265] ? "on" : "off");                        // +265 = SB-coloring-ran flag

// scan all Functions/BasicBlocks/Instructions; collect IT==54
vector<InstBIRKernel*> bks;
for (… inst …)
    if ( *(int*)(inst + 0x50) == 54 ) {                 // decomp 402: == 54
        bks.push_back((InstBIRKernel*)inst);
        log("Found InstBIRKernel: [%s]%s", inst+240, inst+24);   // decomp 426
    }
log("Scan BKs time (s): %f", …);                        // decomp 469

for (InstBIRKernel *inst : bks) {
    wrapper = BIRKernelWrapper::createInstance(logger, inst, parentBB, posFlag);  // decomp 525
    // drive inlining via the wrapper VTABLE (decomp 527-535):
    (*vt[+24])(wrapper, &out);   // build  / lower the named kernel into BIR ops
    (*vt[+32])(wrapper);         // commit / splice
    (*vt[+16])(&out);            // result / status string
    (*vt[+40])(wrapper);         // cleanup
    if (out.status != 0) return out.errorString;        // decomp 539-565: error path
}
log("Lower BKs time (s): %f", …);                       // decomp 494
```

*Anchors: the `== 54` test at decomp 402; the "BIR SB coloring allocator is" / "Found InstBIRKernel" / "Scan BKs" / "Lower BKs" strings; the `createInstance` call at 525; the four vtable calls at offsets `+24`/`+32`/`+16`/`+40` at decomp 527-535.*

### The factory and wrapper

```c
// BIRKernelWrapper::createInstance(Logger, InstBIRKernel*, BasicBlock*, bool) @0xd855d0
ptr = operator new(sizeof(BIRKernelWrapper));           // _Znwm
BIRKernelWrapper::BIRKernelWrapper(ptr, logger, inst, bb, posFlag);   // ctor @0xd8bb50
return shared_wrap(ptr);   // returns a (shared-counted) derived wrapper; error → raise_kernel_exception
```

The `bool` is the **same pass-position flag** as the NKI gate (pre- vs post-coloring). Observed members: `parseOpts` (`@0xd873f0`, parse the `InstBIRKernel` attribute options), `initAndVerifyShapes` (`@0xd87400`, validate operand shapes before expansion), `cleanup` (`@0xd8dbc0`). The wrapper carries an `ActFnType → bir::ActivationFunctionType` table (`.rodata` `@0x3e013a0`) and a `normTypeEnum2Name` table (`@0x3d751e0`) — confirming it expands *templated* BIR library kernels (activation / normalization / attention / MLP) into primitive BIR instructions in place. A `klr::ContentsKernelWrapper` shared-counted type with its own vtable also exists, i.e. there is a small wrapper class hierarchy. The wrapper performs the actual splice and bind through its virtual methods; the per-slot semantics — build / commit / result / cleanup — are [INFERRED] from the call order, not from named slots.

### The contrast, in one table

| | **NKI inline** (`IT==55`) | **BIR inline** (`IT==54`) |
|---|---|---|
| Kernel origin | KLR/NKI-AST-sourced | already-BIR library template |
| Re-canonicalize | **JSON round-trip** (`toJson` → parse) | none |
| Cleanup passes | `Unroll`/`LowerGenericIndirect`/`ConstantPropagate` | none |
| Splice mechanism | **manual** clone + `ilist` transfer | **polymorphic** factory + 4 vtable slots |
| Operand bind | two name-maps → `rewireMemlocs` | wrapper `parseOpts`/`initAndVerifyShapes` |
| Post-inline | `removeFunction` the inlined sub-Function | wrapper `cleanup` |
| Dual placement | `Module+104` bool + alloc-status attr | `Module+265` coloring flag + `bool` arg |

---

## Placement and Cross-References

All three passes are registered mod-parallel (`register_generator_runtime_memory_reservation__` `@0x3dff597`, `…inline_nki_kernel__` `@0x3e013e9`, `…inline_bir_kernel__` `@0x3e01380`). The order is: **39** reservation → 40 linear-scan allocator → **41** `inline_nki_kernel` → 42 `coalesce_multichannel_cc_ops` → … 46 `address_rotation_psum` → **48** `inline_bir_kernel` `[if enableBirKernel]` → … → **60** `inline_bir_kernel` `[!enableBirKernel earlier path]` → **61** `inline_nki_kernel` → 62 `coalesce_multichannel_cc_ops`. Both inline passes are scheduled by `BackendPassManager::addModParallelPassWithName<{InlineNKIKernel|InlineBIRKernel}, bool>(name, bool&&)`; the single `bool` ctor argument becomes the dual-placement selector consulted by the run-time consistency gate.

The reservation carving SBUF before the colorers, and the two inline passes each followed by `coalesce_multichannel_cc_ops`, are the placement constraints to honour. See:

- [`three-sink-kernel-model`](../nki/three-sink-kernel-model.md) — the `InstBIRKernel` / `InstNKIKernel` / `InstNKIKLIRKernel` node model these passes consume.
- [`codegen-helpers-kernel-provision`](../bir/codegen-helpers-kernel-provision.md) — the front-end kernel provisioning whose argument-name bindings these passes turn into the operand-rebind name maps.
- [`json-loader`](../bir/json-loader.md) — the BIR-JSON loader on the parse side of the NKI round-trip.
- [`dram-allocator`](dram-allocator.md) — the DRAM-side counterpart; the reserved SBUF loc here is the SBUF analogue of DRAM reservation.

---

## Evidence summary

Everything on this page is grounded in the decompiled and disassembled `libwalrus.so` sidecars (cp310 frame).

Read directly off the binary: the reservation arithmetic (`==16` SB filter, the `aligned` round-up-8 body, `addMemoryLocationWithMemoryLocationSet` → `setAllocated` → `+579` → `buildTensorId2MemLoc`, `getArchModel … - *(…+484)`); the `> 29` arch gate (decomp line 174); the NKI `!= 55` opcode test; the alloc-status "Skip lowering" gate; the JSON round-trip (`Function::toJson` + `adl_serializer<Module>::to_json`); the three KLR cleanup `::run` calls; `removeFunction`/`saveJsonFile`; the BIR `== 54` test; the "BIR SB coloring allocator is" flag (`Module+265`); `createInstance`; and the four vtable calls at `+24`/`+32`/`+16`/`+40`.

One inference step away: the size-path predicate keying on the reserved-loc name; the SBUF kind of the recorded `MemoryLocationSet`; that the `+0x108` deref is the byte size; that the BIR wrapper's virtual methods perform the actual splice and bind.

Still open [INFERRED]: the semantics of `Module + 0xAC` as the arch *generation* index, and the per-slot meaning (build / commit / result / cleanup) of the four BIR wrapper vtable calls, which is deduced from call order rather than from named slots.
