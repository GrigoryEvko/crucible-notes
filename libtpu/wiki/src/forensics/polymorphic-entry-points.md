# Polymorphic Dispatch Entry Points

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Every VA is a load address in the un-relocated image; the executable sections satisfy `VA == file-offset`. Other builds will differ.*

## Abstract

This is the navigation page for control-flow fan-out. A 745 MB stripped C++ binary is not a tree of `call sub_X` edges — its hot paths run through **indirect calls**, where the target is a function pointer read out of a vtable slot or a type-erased callable. To follow execution from the PJRT entry surface down into a TPU ISA encoder, a reverse-engineer must stand at each of these "hubs" and resolve `call qword ptr [reg+0xNN]` to a concrete implementor. This page maps the dozen hubs that matter, anchors each to its call-site address and the vtable slot it reads, and then states the general procedure for resolving any indirect call by joining the slot index back to the [RTTI census](rtti-vtable-census.md).

There are **two C++ dispatch shapes** in this binary, plus a third that is not a C++ vtable at all. (a) **Vtable-slot dispatch** — `mov (obj),%vptr ; call *0xNN(%vptr)` — is the overwhelming majority; the slot index is `0xNN / 8`. (b) **Function-pointer dispatch** — `call *%reg` — where the callee was loaded into a register first; this is how `mlir::PatternApplicator`, `llvm::function_ref`, and the `std::function`/`AnyInvocable` pools dispatch, and a navigator grepping only for `call *[reg+off]` will miss it entirely. (c) The **PJRT C-ABI surface** is a *flat function-pointer struct* (`PJRT_Api`), a C dispatch table populated once at first call — structurally distinct from a C++ vtable and resolved by reading the struct's initializer, not by RTTI.

The mechanism differs by IR layer, and the difference is the point. The XLA HLO layer dispatches the classic **visitor pattern**: `HloInstruction::Visit` is a 132-way opcode switch where each case tail-calls a different `Handle<Opcode>` slot of the `DfsHloVisitor` vtable. MLIR does *not* use one vtable per op — it uses a **concept-based Op Model**, a per-op generated dispatch object whose `foldHook`/`hasTrait` slots `mlir::Operation::fold` reads indirectly. The pass managers (`HloPassInterface::Run`, `OpToOpPassAdaptor::run`) are thin trampolines that tail-jump a single slot. The TPU codegen (`CodeGenerator::EmitInstruction`) fans out across 81 slots of a 152-slot `IsaEmitter` vtable filled per hardware generation.

For navigation, the contract is:

- **The two-and-a-half dispatch shapes** and how to recognize each in disassembly.
- **The major hubs**: call-site address, the vtable VA + slot it reads, and what the slot resolves to.
- **The resolution procedure**: slot index `->` candidate implementors via the RTTI vtable census; how to handle the C-ABI and function-pointer shapes that RTTI does not cover.

| | |
|---|---|
| **Hottest binary-wide offset** | `0x10` (slot 2 — first non-dtor virtual: `name()`/`Compute()`/`foldHook` per hierarchy) |
| **HLO visitor fan-out** | `HloInstruction::Visit` (`0x1e585660`) — 132-case opcode switch into `DfsHloVisitor` vtable |
| **Visitor vtable** | `_ZTVN3xla17DfsHloVisitorBaseIPNS_14HloInstructionEEE` @ `0x21d2c320` (address point `+0x10`) |
| **HLO pass trampoline** | `HloPassInterface::Run` (`0x1e472a60`, 6 B) — `jmp *0x28(%rax)` = slot 5 |
| **MLIR pass body** | `OpToOpPassAdaptor::run` (`0x1cb6dc20`) — `call *0x38(%rax)` = slot 7 (`runOnOperation`) |
| **MLIR Op-Model fold** | `mlir::Operation::fold` (`0x1d8cd480`) — `call *0x10(Model)` = slot 2 (`foldHook`) |
| **PJRT C-ABI** | flat `PJRT_Api` struct from `GetTpuPjrtApi` (`0xe6aa440`) — C function pointers, not a vtable |

---

## At-a-Glance: The Dispatch Hubs

Each hub is one indirect-call site that a navigator will hit repeatedly. `slot = off / 8`. Slot labels are the [RTTI census](rtti-vtable-census.md) per-slot names; "fan-out" is the count of distinct implementors a single site can reach.

| Hub | Call-site VA | Through (vtable / shape) | Slot / off | Fan-out | Confidence |
|---|---|---|---|---|---|
| HLO visitor opcode dispatch | `0x1e585660` (`Visit`) | `DfsHloVisitor` @ `0x21d2c320` | 132 distinct slots, off `0x20`–`0x438` | one `Handle<Op>` per opcode | CERTAIN |
| HLO per-node pre-hook | `0x1e5866e0` (`PostOrderDFS`) | same vtable | 137 / `0x448` (`Preprocess`) | per concrete visitor | CERTAIN |
| HLO per-node post-hook | `0x1e5866e0` | same vtable | 138 / `0x450` (`Postprocess`) | per concrete visitor | CERTAIN |
| HLO per-node gate | `0x1e5866e0` | same vtable | 139 / `0x458` (`ShouldProcessNode`) | per concrete visitor | CERTAIN |
| HLO finish-hook | `0x1e584660` (`Accept`) | same vtable | 136 / `0x440` (`FinishVisit`) | per concrete visitor | CERTAIN |
| HLO pass body | `0x1e472a60` (`Run`) | `HloPassInterface` | 5 / `0x28` (`RunImpl`) | every HLO pass | CERTAIN |
| HLO pass body (uptr) | `0x1e472a80` (`Run`) | `HloPassInterface` | 6 / `0x30` (`RunImpl` uptr) | every nested pipeline | CERTAIN |
| MLIR pass body | `0x1cb6dc20` (`OpToOpPassAdaptor::run`) | `mlir::Pass` | 7 / `0x38` (`runOnOperation`) | every MLIR pass | CERTAIN |
| MLIR Op-Model fold | `0x1d8cd480` (`Operation::fold`) | Op `Model` concept | 2 / `0x10` (`foldHook`) | every registered MLIR op | CERTAIN |
| CPU thunk execute | `0x1c0f0320` (`TracedExecute`) | `xla::cpu::Thunk` | 5 / `0x28` (`Execute`) | every thunk kind | CERTAIN |
| TPU ISA emit | `0x14043a40` (`EmitInstruction`) | `IsaEmitter` (152-slot) | 81 slots, off `0x50`–`0x490` | per-gen `{Pf,Vf,Gl,Gf}` emitters | HIGH |
| TpuHal hardware bring-up | `0x1e811ea0` (`InitializeInternal`) | `TpuHal`/`HardwareImpl` | 19 / `0x98`, 20 / `0xa0` | per-gen `HardwareImpl` | CERTAIN |
| TPU codec factory | `0x1e835fa0` (`TpuCodec::Create`) | `TpuVersion` switch | n/a (factory) | 6 per-gen `CreateTpuCodec<X>` | CERTAIN |
| TF op-kernel dispatch | `0xe99b000` (`Device::Compute`) | `OpKernel` | 2 / `0x10` (`Compute`) | every TF op kernel | CERTAIN |
| MLIR pattern match | `0x1c9971e0` (`PatternApplicator::matchAndRewrite`) | `function_ref` (`call *%reg`) | n/a (fn-ptr) | every rewrite pattern | HIGH |
| gRPC service handler | `0xf993000` (`RpcMethodHandler::RunHandler`) | `std::function`/`AnyInvocable` | `0x18` invoke (`call *%reg`) | per registered RPC | HIGH |
| PJRT C entry surface | `0xe6aa440` (`GetTpuPjrtApi`) | flat `PJRT_Api` struct | C fn-ptr table | one C callable per API slot | HIGH |

> **NOTE —** "Confidence CERTAIN" means the call-site address, the offset, and the slot label were all read directly from the IDA decompilation of the named driver function. The two `HIGH` rows where a register holds the target (`PatternApplicator`, gRPC) are certain about the *shape* but the concrete callee is loaded dynamically, so the implementor cannot be pinned from the site alone.

---

## The Two-and-a-Half Dispatch Shapes

Before any hub, learn to read the shapes. Every indirect call in this binary is one of three forms.

### Shape A — vtable-slot dispatch (the majority)

```asm
mov    (%rdi), %rax        ; load vptr from object+0  (the *(_QWORD *)obj in pseudocode)
call   *0x28(%rax)         ; call slot 5  (0x28 / 8 = 5)
```

In decompiled pseudocode this is `(*(...)(*(_QWORD *)obj + 0x28LL))(obj, ...)`. The object's first 8 bytes are the vptr; the `+0xNN` selects the slot. This is the canonical C++ virtual call, and it is what every pass/visitor/kernel hub below uses. Slot index is `0xNN / 8`.

### Shape B — function-pointer dispatch (`call *%reg`)

```asm
mov    0x18(%r13), %rax    ; load a function pointer out of a callable object
call   *%rax               ; no fixed offset on the call itself
```

The pseudocode is `(*(...)(a1 + 0x18))(...)` where the loaded value is a raw code pointer, not a vptr. This is how type-erased callables dispatch: `llvm::function_ref<>::callback_fn`, `std::function`, `AnyInvocable`, and `mlir::PatternApplicator`'s matched-pattern predicate. **A grep for `call *0xNN(%reg)` finds none of these.** To enumerate them, also sweep `call *%reg`.

### Shape C — flat C function-pointer struct (`PJRT_Api`)

The PJRT plugin ABI is a C struct of ~140 function pointers. `GetTpuPjrtApi` (`0xe6aa440`) builds it once (guarded by `__cxa_guard_acquire`) and returns the static instance; the framework then calls `api->PJRT_Client_Create(...)` etc. by reading a fixed struct offset. This is *not* a C++ vtable — there is no `this`-as-first-arg convention and no RTTI binding. It is resolved by reading the struct initializer, where each slot is assigned a named `TPU_PJRT_*` thunk (e.g. `TPU_PJRT_HostAllocator_Allocate`). Treat it as the boundary: above it is C ABI, below it is the C++ vtable world the rest of this page maps.

> **GOTCHA —** the hottest *offset* binary-wide is `0x10` (slot 2), but slot 2 means a different method in every hierarchy — `name()` for a pass, `Compute()` for an op kernel, `foldHook` for an MLIR op, `Encode` for a codec. The offset alone tells you nothing; you must know which vtable the object belongs to before the slot label is meaningful. That join is what the [RTTI census](rtti-vtable-census.md) provides.

---

## HLO Visitor Dispatch — the 132-way opcode fan-out

### Purpose

The single busiest polymorphic dispatch in the XLA layer. Every analysis and optimization pass walks the HLO graph, and at each node `HloInstruction::Visit` dispatches to the visitor's per-opcode handler. This is the classic GoF visitor: the *instruction* selects which `Handle<Opcode>` method of the *visitor* runs.

### Entry Point

```text
HloInstruction::Accept (0x1e584660)        ── public visitor entry; runs the DFS, then finish-hook
  └─ PostOrderDFS<DfsHloVisitorBase> (0x1e5866e0)   ── per-node driver loop
       ├─ *0x458 ShouldProcessNode (slot 139)   ── per-node gate
       ├─ *0x448 Preprocess        (slot 137)   ── per-node pre-hook
       ├─ HloInstruction::Visit (0x1e585660)    ── the opcode fan-out (below)
       └─ *0x450 Postprocess       (slot 138)   ── per-node post-hook
  └─ *0x440 FinishVisit (slot 136)              ── post-traversal hook
```

### Algorithm

```c
function HloInstruction_Visit(instr, visitor):     // 0x1e585660
    opcode = instr.byte[0xc]                        // HloOpcode at instr+12
    switch (opcode):                                // 132 cases, 0x00 .. 0x83
        case 0x6c:  return (*visitor.vtable[0x28])(visitor, instr)  // off 0x28 = slot 5
        case 0x1f:  return (*visitor.vtable[0x20])(visitor, instr)  // off 0x20 = slot 4 (lowest)
        case 0x05:  return (*visitor.vtable[0x438])(visitor, instr) // off 0x438 (highest)
        ...                                         // 132 distinct slots, one per opcode
        default:
            // "Unhandled HloOpcode for DfsHloVisitor: %s ... please file a bug for XLA."
            return Internal(...)                    // status error, opcode out of range
```

The compiler lowers this `switch` to a `.rodata` i32 jump table: `movzbl 0xc(%rdi),%ecx ; cmp $0x83,%rcx ; ja default ; movslq (table,%rcx,4),%rcx ; ...; jmp *0xNN(%rcx)`. Each opcode tail-jumps a *different* slot of the visitor vtable; the offsets are not contiguous (they range from `0x20` for opcode `0x1f` up to `0x438` for opcode `0x05`), because the slot layout follows the `DfsHloVisitor` method declaration order, not the opcode enum order.

### Resolving the Slots

The visitor vtable is `_ZTVN3xla17DfsHloVisitorBaseIPNS_14HloInstructionEEE` @ `0x21d2c320` (address point `0x21d2c330`). Its 132 `Handle<Op>` slots split two ways:

- **~56 slots have a default body in the base** — the elementwise opcodes (`Add`, `Multiply`, `Compare`, `Convert`, `Maximum`, `Negate`, `Tanh`, …) forward to `HandleElementwiseUnary`/`HandleElementwiseBinary`. A visitor that does not override them still works.
- **~76 slots are `__cxa_pure_virtual` in the base** — the structural opcodes with no generic default (`Convolution`, `Fusion`, `Dot`, `Reduce`, `CustomCall`, the collectives). Every concrete visitor *must* implement these or it will not link.

| Axis | Values | Source |
|---|---|---|
| Opcode | `0x00`–`0x83` (132) | `instr+0xc` byte; `switch` in `Visit` |
| Slot offset | `0x20`–`0x438`, non-contiguous | the 132 `call`/`jmp *0xNN` operands in `Visit` |
| Default vs pure | ~56 default-forwarding, ~76 pure | base vtable `0x21d2c320` slot addends |
| Hook slots | 136 `FinishVisit`, 137 `Preprocess`, 138 `Postprocess`, 139 `ShouldProcessNode` | `Accept`/`PostOrderDFS` dispatch sites |

> **QUIRK —** the opcode `->` slot permutation is *not* recoverable from the opcode enum. The `Visit` switch is the only authority: opcode `0x1f` uses the lowest slot (`0x20`), opcode `0x05` the highest (`0x438`). To rebuild the visitor contract you must read the 132 case bodies, not assume slot order matches enum order. The exact enum-name-to-slot map requires decoding the `.rodata` jump table and joining it to the `HloOpcode` enum; that join is not reproduced here.

---

## HLO Pass Dispatch — the six-byte trampoline

### Purpose

Every pass in the HLO pipeline funnels through one of two six-byte tail-jumps. `HloPassInterface::Run` is not a method with a body — it is a trampoline that loads the pass's vtable and jumps to the per-pass implementation slot.

### Algorithm

```c
function HloPassInterface_Run(pass, module_and_set):    // 0x1e472a60, 6 bytes
    return (*pass.vtable[0x28])()                         // jmp *0x28(%rax) = slot 5 = RunImpl

function HloPassInterface_Run_uptr(pass, uptr_and_set):  // 0x1e472a80, 6 bytes
    return (*pass.vtable[0x30])()                         // jmp *0x30(%rax) = slot 6 = RunImpl(uptr)
```

The pipeline driver `HloPassPipeline::RunPassesInternal<HloModule*>` (`0x1c83ddc0`) does *not* call slot 5 inline. It dispatches the surrounding metadata slots and then calls `pass->Run()` (the non-virtual trampoline), which tail-jumps the real body. The metadata dispatches observed in the driver:

| Off | Slot | Method | Use |
|---|---|---|---|
| `0x10` | 2 | `name()` | logging / dump (called ×6 in the driver) |
| `0x18` | 3 | `RunOnChangedComputations` | once per pass |
| `0x20` | 4 | `IsPassPipeline()` | once per pass (×2) |
| `0x30` | 6 | `RunImpl(uptr&)` | nested-pipeline path (×2) |

> **NOTE —** because the body is reached through the `Run` trampoline rather than inline, a static caller-graph that stops at `RunPassesInternal` misses the per-pass work entirely. Follow the trampoline at `0x1e472a60` to find slot 5, then enumerate slot-5 implementors via the RTTI census to list every concrete pass.

---

## MLIR Pass Dispatch — `OpToOpPassAdaptor::run`

### Purpose

The MLIR pass infrastructure is CRTP-based; the adaptor `mlir::detail::OpToOpPassAdaptor::run` (`0x1cb6dc20`) is the driver that invokes each concrete pass's `runOnOperation()` once per nested operation it is scheduled on.

### Algorithm

```c
function OpToOpPassAdaptor_run(pass, op, am, ...):   // 0x1cb6dc20
    if op.name.impl.typeid == UnregisteredOp:
        emitOpError("trying to schedule a pass on an unregistered operation")
        return failure
    ...
    (*pass.vtable[0x10])(pass)                        // call *0x10(%rax) = slot 2 = getName()  (logging)
    (*pass.vtable[0x20])(pass, IsIsolatedFromAbove)   // call *0x20(%rax) = slot 4 = hasTrait query
    (*pass.vtable[0x50])(pass, op)                    // call *0x50(%rax) = slot 10 = canScheduleOn
    ...
    (*pass.vtable[0x38])(pass)                        // call *0x38(%rax) = slot 7 = runOnOperation
```

The dispatch sites inside the adaptor that read the **pass** vtable (`mov (pass),%rax ; call *0xNN(%rax)`): `*0x10` (slot 2, `getName`), `*0x20` (slot 4, the `hasTrait<IsIsolatedFromAbove>` query that gates scheduling), `*0x50` (slot 10, `canScheduleOn(Operation*)`), and `*0x38` (slot 7, `runOnOperation` — the per-pass body, reached via `runOnOperationImpl`/`runOnOperationAsyncImpl`). Slot 7 is the only one that runs user pass logic; the rest are the auto-generated `*PassBase` CRTP metadata.

| Off | Slot | Method | Confidence |
|---|---|---|---|
| `0x38` | 7 | `runOnOperation()` — the pass body | CERTAIN |
| `0x10` | 2 | `getName()` | CERTAIN |
| `0x20` | 4 | `hasTrait<IsIsolatedFromAbove>()` query | CERTAIN |
| `0x50` | 10 | `canScheduleOn(Operation*)` | CERTAIN |

> **NOTE —** the driver also contains `call *0x20`, `*0x28`, and `*0x30` sites that dispatch on a *different* object — the `PassInstrumentation` list it iterates (`mov (list[i]),%rax ; call *0xNN(%rax)`), the per-pass `runBeforePass`/`runAfterPass`/`runAfterPassFailed` callbacks — not the pass vtable. A sweep that attributes every indirect call in this function to the pass vtable will mislabel those instrumentation slots; only the four sites above read the pass object itself.

---

## MLIR Op-Model Dispatch — `Operation::fold` and the concept object

### Purpose

MLIR does **not** keep one C++ vtable per operation type. Each registered op has an `OperationName::Impl` — a "Model" concept object whose vtable carries the op's hooks (`foldHook`, `hasTrait`, `getCanonicalizationPatterns`, …). `mlir::Operation::fold` (`0x1d8cd480`) is the central folding entry for all registered ops; it reaches the op's behavior through this concept indirection, not through the `Operation` object's own vtable.

### Algorithm

```c
function Operation_fold(op, attrs, results):       // 0x1d8cd480
    model = op.field[0x30]                           // mov 0x30(%rdi),%rdi  — OperationName::Impl
    ok    = (*model.vtable[0x10])(model, op, ...)    // call *0x10(%rax) = slot 2 = foldHook
    if ok: return true
    // fallback: look up a DialectFoldInterface and try its fold
    dialect = model.dialect
    if isa<DialectFoldInterface>(dialect):
        iface = dialect.interface_map[TypeID(DialectFoldInterface)]
        if iface: return (*iface.vtable[0x10])(iface, op, ...)   // a second slot-2 dispatch
    return false
```

The load chain is the proof that this is a real Model dispatch and not a member-pointer: `mov 0x30(%rdi),%rdi` (load the Model concept), `mov (%rdi),%rax` (load its vptr), `call *0x10(%rax)` (slot 2 = `foldHook`). The same concept object also answers `hasTrait` through a neighboring slot. `OperationName::Impl` is the per-op generated dispatch object that stands in for the per-op vtable MLIR deliberately does not emit.

> **QUIRK —** there are **two** `call *0x10` dispatches in `Operation::fold`: first the op's own `foldHook`, then — if it returns false — the op's dialect's `DialectFoldInterface::fold`. Both are slot-2 calls but through different concept objects (op Model vs dialect interface). A navigator who stops at the first `call *0x10` misses the dialect-level fold fallback.

---

## CPU Thunk Execution — `Thunk::Execute` = slot 5

### Purpose

The XLA:CPU backend lowers a computation to a sequence of `Thunk` objects; `ThunkExecutor::TracedExecute` (`0x1c0f0320`) runs one thunk by dispatching its `Execute` body.

### Algorithm

```c
function ThunkExecutor_TracedExecute(executor, thunk, params):   // 0x1c0f0320
    if g_trace_level > 0:
        TraceMeProducer(...)                                       // profiler scope
        (*thunk.vtable[0x28])(executor, thunk, params)             // call *0x28 = slot 5 = Execute
        AsyncValueRef<Chain>::AndThen(...)                         // chain continuation
        TraceMe::Stop(...)
    else:
        (*thunk.vtable[0x28])(executor)                            // same slot 5, untraced fast path
```

Both the traced and untraced paths dispatch the same slot — `0x28` = slot 5 = `xla::cpu::Thunk::Execute(ExecuteParams const&)`. `ThunkExecutor::ExecuteSequential` calls `TracedExecute` once per thunk in the sequence; the async continuation path feeds the same slot-5 site. `CpuExecutable::ExecuteThunks` is the top-level entry that builds the executor.

---

## TPU ISA Codegen — `EmitInstruction` fans out across `IsaEmitter`

### Purpose

`xla::jellyfish::CodeGenerator::EmitInstruction` (`0x14043a40`) is the per-`LloInstruction` codegen dispatcher. It is the densest single polymorphic dispatch region: one large function (3,652 decompiled lines) that fans out across 81 distinct slots of the 152-slot `IsaEmitter` vtable. The concrete per-generation emitters fill those slots.

### Algorithm

```c
function EmitInstruction(codegen, llo_instr, bundle):   // 0x14043a40
    emitter = codegen.isa_emitter                         // the per-gen IsaEmitter object
    switch (llo_instr.kind):                               // 81 reachable emitter slots
        ...
        case VectorMatmulMsk:
            (*emitter.vtable[0x418])(emitter, ...)          // off 0x418 = slot 131 = EmitVectorMatmulMsk
        case AccumulatorBinop:
            (*emitter.vtable[0x478])(emitter, ...)          // off 0x478 = slot 143 = EmitVectorAccumulatorBinop
        ...                                                 // EmitVectorMove/Pack, transpose, Cmem,
                                                            // BarnaCore sync/wait, event/program hooks
```

The dispatch is real virtual dispatch through the emitter's vptr: `(*(...)(*(_QWORD *)emitter_obj + 0x418LL))(IsaEmitter*, ...)`. Confirmed sites include `0x418` (slot 131, `EmitVectorMatmulMsk`) and `0x478` (slot 143, `EmitVectorAccumulatorBinop`). The slots are the per-generation ISA-encoder hooks filled by the `{Pf, Vf, Gl, Gf}` concrete emitter classes; see [per-generation function dispatch](per-gen-function-dispatcher.md) for how each generation's emitter is selected.

> **CORRECTION (PEP-2) —** the fan-out is **81** distinct `IsaEmitter` vtable-slot offsets, not ~94 or 96. Counting the distinct dispatch operands whose receiver IDA types as `xla::jellyfish::IsaEmitter *` in the decompilation of `0x14043a40` yields 81 unique emitter slots, spanning offsets `0x50`–`0x490` (not the `0xf0`–`0x478` band an earlier note assumed). A handful of other indirect calls in the same function target `LloInstruction`/helper objects, not the emitter, and are excluded. The order of magnitude — "the densest dispatch region in the binary" — is unaffected.

---

## TpuHal Hardware Bring-up — slots 19 and 20

### Purpose

`tpu::TpuHal::InitializeInternal` (`0x1e811ea0`) is the hardware bring-up driver. The genuine per-generation polymorphism is concentrated in two `HardwareImpl` slots; most of the surrounding `TpuHal::` surface is non-virtual delegation to the chip vector and memory manager.

### Algorithm

```c
function TpuHal_InitializeInternal(hal, options):     // 0x1e811ea0
    validate(options)                                   // page-alignment / power-of-2 checks
    InitializeAllocator(hal, ...)                       // non-virtual
    status = (*hal.vtable[0x98])(hal)                   // slot 19 — per-gen ValidateTopology
    if status != ok: return status
    TpuHalCommonStates::Create(...)
    (*hal.vtable[0xa0])(&out, hal, options)             // slot 20 — CreateAndInitializeChips (per-gen)
    sort(chips); for each chip: (*hal.vtable[0x48])(hal, i)   // slot 9 — GetChip (per-chip)
    ParallelForWithStatus(...)                          // bring up chips in parallel
    on error: (*hal.vtable[0x20])(hal)                  // slot 4 — TearDown
```

Slot 19 (`0x98`) is the topology validation hook and slot 20 (`0xa0`) is `CreateAndInitializeChips` — both overridden by the per-generation `HardwareImpl` subclasses. The driver also dispatches slot 9 (`0x48`, `GetChip`) per chip and slot 4 (`0x20`, `TearDown`) on the error path.

> **CORRECTION (PEP-3) —** `GetChip` was previously characterized as a non-virtual delegation to the chip vector. The decompilation of `InitializeInternal` shows it dispatched **virtually** through slot 9 (`call *0x48(%rax)`) inside the per-chip validation loop. Other named `TpuHal::` methods (`InitializeAllocator`, the page-alignment checks) are indeed non-virtual; `GetChip` is not.

---

## TPU Codec Factory — a `TpuVersion` switch, then a 6-slot vtable

### Purpose

`tpu::TpuCodec::Create` (`0x1e835fa0`) is a per-generation **factory** — a `switch(TpuVersion)`, not a virtual call. It returns a codec object that then carries the 6-slot `TpuCodec` vtable through which consumers call `Encode`/`Decode`.

### Algorithm

```c
function TpuCodec_Create(out, version):       // 0x1e835fa0
    switch (version):
        case 0: codec = CreateTpuCodecJellyfish(out)
        case 1: codec = CreateTpuCodecDragonfish(out)
        case 2: codec = CreateTpuCodecPufferfish(out)
        case 3: codec = CreateTpuCodecViperfish(out)
        case 4: codec = CreateTpuCodecGhostlite(out)
        case 5: codec = sub_1E838380(out)         // anonymous v5 codec
    out.tag   = 1                                 // variant discriminant
    out.codec = codec                             // out+8 = the codec object
    return out
```

The returned object exposes a 6-slot `TpuCodec` vtable: `Encode` (slot 2 / `0x10`), `Decode` (slot 3 / `0x18`), `EncodeBundle` (slot 4 / `0x20`), `DecodeBundle` (slot 5 / `0x28`). The encode/decode consumers route through `pro::v4::proxy` facades — a third dispatch mechanism (a proxy-table) distinct from both C++ vtables and function pointers, whose internal slot layout is not decoded here.

> **CORRECTION (PEP-4) —** the `TpuVersion` case-to-codec mapping is **`{0:Jellyfish, 1:Dragonfish, 2:Pufferfish, 3:Viperfish, 4:Ghostlite, 5:anon-v5}`**. An earlier note ordered the factory targets `{Jellyfish, Ghostlite, Pufferfish, Viperfish, Dragonfish}`, which swaps Dragonfish and Ghostlite. The decompilation of `0x1e835fa0` is authoritative: case 1 is Dragonfish, case 4 is Ghostlite.

---

## TensorFlow Op-Kernel Dispatch — `Device::Compute` = slot 2

### Purpose

`tensorflow::Device::Compute` (`0xe99b000`, 12 bytes) is the canonical TF op-kernel dispatch: a device runs a kernel by tail-jumping the kernel's `Compute` slot.

### Algorithm

```c
function Device_Compute(device, kernel, ctx):    // 0xe99b000
    return (*kernel.vtable[0x10])(kernel, ctx)     // jmp *0x10 = slot 2 = OpKernel::Compute
```

Offset `0x10` = slot 2 = `OpKernel::Compute(OpKernelContext*)`. `ThreadPoolDevice::Compute` (`0x10835420`) does the same `call *0x10`; `RenamedDevice::Compute` (`0x108a5200`) instead delegates `jmp *0xb8(%rax)` to the wrapped device. Fired once per op execution.

---

## Function-Pointer and C-ABI Hubs

These hubs do not use Shape A. RTTI cannot resolve them; you read the *caller* to find what the register/struct holds.

### MLIR pattern match — `call *%reg`

`mlir::PatternApplicator::matchAndRewrite` (`0x1c9971e0`) applies rewrite patterns. Its match predicate is a `llvm::function_ref` passed as a parameter and invoked as `a4(arg, pattern)` — a bare `call *%reg`, not an inline vtable slot. The matched pattern's own `matchAndRewrite` is likewise reached through a register-held pointer loaded from the pattern object. The slot is not fixed in the binary: it is loaded dynamically from each `Pattern`, so there is no single offset to anchor.

### gRPC service handler — `call *0x18(%reg)`

The templated `grpc::internal::RpcMethodHandler<...>::RunHandler` (`0xf993000`) invokes the registered service-method callable: `(*(...)(handler + 0x18))(...)`. Offset `0x18` selects the invoke pointer of the type-erased callable (`std::function`/`AnyInvocable`), again a function-pointer shape. The concrete service method is whatever was registered at service-build time.

### PJRT C entry surface — the flat struct

`pjrt::tpu_plugin::GetTpuPjrtApi` (`0xe6aa440`) returns the static `PJRT_Api` struct, lazily building its extension chain (raw-buffer, layouts, memory-descriptions, executable-metadata, host-allocator, cross-host-transfers) under `__cxa_guard`. The framework calls API slots by fixed struct offset; each slot holds a named `TPU_PJRT_*` C thunk. This is the top of the dispatch stack — resolve a PJRT call by reading the struct initializer, then follow the named thunk into the C++ world.

> **GOTCHA —** the function-pointer hubs are invisible to a `call *0xNN(%reg)` sweep. To enumerate the type-erasure and pattern-rewrite dispatch you must also sweep `call *%reg`. Skipping this leaves the entire MLIR pattern-rewrite and gRPC service surface unmapped.

---

## Resolving an Indirect Call — the methodology

This is the procedure these hubs illustrate. Given an unknown `call *0xNN(%reg)` you want to follow:

```text
1. CLASSIFY THE SHAPE
   a. backtrack the register: is it `mov (obj),%vptr` (Shape A) or a loaded code ptr (Shape B)?
   b. if the callee came from a flat struct read by fixed offset -> Shape C (C-ABI), stop here,
      read the struct initializer.

2. IDENTIFY THE VTABLE  (Shape A only)
   a. find where `obj` was constructed or typed (ctor call, factory return, RTTI check).
   b. the object's class names the vtable: look it up in the RTTI census by mangled name.
      e.g. an object whose vptr is 0x21d2c330 -> DfsHloVisitorBase<HloInstruction*> @ 0x21d2c320.

3. COMPUTE THE SLOT
   slot = 0xNN / 8.   (0x28 -> slot 5, 0x10 -> slot 2, 0x38 -> slot 7.)

4. LABEL THE SLOT
   the RTTI census gives the per-slot method name for that vtable
   (slot 5 of HloPassInterface = RunImpl; slot 2 of OpKernel = Compute; slot 7 of mlir::Pass
    = runOnOperation).

5. ENUMERATE IMPLEMENTORS
   read the slot addend of EVERY vtable bound to that base class in the RTTI census.
   each distinct addend at offset 0xNN is one candidate concrete target.
   the *set* of those addends is the fan-out of the call site.

6. PRUNE BY CONTEXT
   the construction site (step 2) usually fixes which concrete subclass `obj` is, collapsing
   the fan-out to the one or few implementors actually reachable from this caller.
```

For Shape B (`call *%reg`), steps 2–5 do not apply: there is no vptr and no fixed slot. Instead backtrack the register to its definition — a `callback_fn` instantiation, a `std::function` target assignment, or a `Pattern` field load — and read the captured target there. For Shape C, the implementor is named directly in the struct initializer.

> **NOTE —** the slot-index/8 rule and the RTTI-census join are the whole game. Every hub on this page was resolved by exactly steps 1–5: read the driver, compute `0xNN/8`, look the slot up in the census, enumerate addends. A reimplementer building a navigator should automate that join rather than resolving each site by hand.

---

## Related Components

| Component | Relationship |
|---|---|
| [Dispatch-Table Taxonomy](dispatch-table-taxonomy.md) | classifies *what kind* of table each hub reads (vtable, factory switch, C-ABI struct, proxy facade) |
| [RTTI / Vtable Census](rtti-vtable-census.md) | the callee-side: per-slot method labels and the implementor set this page joins to |
| [Per-Generation Function Dispatch](per-gen-function-dispatcher.md) | how `TpuCodec::Create` / `TpuHal` slot-20 pick a per-generation implementor |

## Cross-References

- [Forensics Overview](overview.md) — where polymorphic dispatch sits in the binary-anatomy map
- [Dispatch-Table Taxonomy](dispatch-table-taxonomy.md) — the sibling that enumerates the table *kinds*; this page enumerates the *call sites* into them
- [RTTI / Vtable Census](rtti-vtable-census.md) — the implementor lookup; resolution step 4–5 above reads its per-slot labels and addends
- [Per-Generation Function Dispatch](per-gen-function-dispatcher.md) — the `{Pf,Vf,Gl,Gf}` emitter and codec/HAL per-gen selection behind the `IsaEmitter` / TpuHal / TpuCodec hubs
