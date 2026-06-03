# Region → Sequencer Outliner

> *Every function address, op-name string, attribute name/length, and error-message string on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`) — from the decompiled C++ of `TileTaskOutliningPass::runOnOperation` (`@0x13606220`), its per-`TileTaskOp` walk callback (`@0x136066e0`), and the `LaunchTileTaskOp` builders. Other versions differ.*

## Abstract

The SparseCore back-end runs every offload computation across three sub-engines — **SCS** (scalar control), **TAC** (tile-access / DMA), **TEC** (vector compute) — and represents that split as ordinary MLIR functions, one per engine, each tagged with an `sc.sequencer` StringAttr. The pass that *creates* those functions is `TileTaskOutliningPass`. It is a classic region-outliner in the [`getUsedValuesDefinedAbove`](#the-per-op-callback-0x136066e0) → `FuncOp::create` → `Region::cloneInto` → replace-with-launch mould — the same shape LLVM/MLIR uses to outline a parallel region into a kernel — specialized for SparseCore by what it captures (only static memrefs), what it stamps (`sc.sequencer = "execute"`), and what it leaves behind (a `sc_tpu.launch_tile_task` op plus, on the overlayer path, a `PrefetchTileTaskOp` and an overlay `memref.alloc`).

This page owns the **pass mechanics and the launch emission**: how a `sc_tpu.tile_task` region becomes a standalone `func.func`, how its live-ins become the function's arguments, how the body is cloned, how the original op is replaced by a launch, and which attributes the outlined function carries. It deliberately does **not** re-derive the per-op→engine *selection predicate* — the rule that decides whether a given lowered op is Stream-vs-DMA and which TileTask region it lands in. That predicate is owned by [getSequencerType](getsequencertype.md) (`GetTransferKind` + the `sc.sequencer` read-back); here the engine string is treated as the pass *output*, byte-confirmed as `"execute"` on 6acc60406 (`gfc`).

The single structural fact a reimplementer must internalize: **`TileTaskOutliningPass` produces exactly one outlined function per `sc_tpu.tile_task` op, tagged `"execute"` (TEC), and replaces the op with a `sc_tpu.launch_tile_task` whose `execute_func` symbol points at it.** The enclosing function — the one that issued the `tile_task` — is the SCS control program (`sc.sequencer = "scs"`). On Viperfish/Ghostlite the same Target-parameterized pass additionally splits off an `"access"` (TAC) function; on 6acc60406 there is no TAC, so the tile-fetch work folds into `"execute"`. The page documents the gfc (6acc60406) single-`"execute"` path, which is what the decompiled callback emits, and flags the TAC split as LOW.

For reimplementation, the contract is:

- **One pass, one walk, one launch per task.** `runOnOperation` runs a Timem-conflict gate, resolves the `SparseCoreTarget`, then `walk`s every `sc_tpu.tile_task` op. Each match is outlined independently; there is no inter-task sharing of the outlined functions.
- **Captures are the region live-ins, and they must be static memrefs.** Arguments come from `getUsedValuesDefinedAbove`; each must be a `MemRefType` with a fully static shape, or the pass aborts with `"Tile tasks only support capture of static memrefs"` (`tile_task_outlining_pass.cc:62`).
- **The function name is `"execute"` + a per-pass counter.** The prefix `"execute"` is rendered with the decimal counter (`APInt::toString`) into a unique symbol; the *attribute value* `sc.sequencer` is the bare `"execute"` (7 chars).
- **The replacement op is `sc_tpu.launch_tile_task`** (23-char op name) carrying a `FlatSymbolRefAttr execute_func`, a `clear_ibuf` `UnitAttr`, the overlay-alloc operand, and the captured `ValueRange`. The original `tile_task` is `erase`d.

| | |
|---|---|
| **Pass** | `xla::tpu::sparse_core::(anon)::TileTaskOutliningPass::runOnOperation` `@0x13606220` |
| **Factory** | `CreateTileTaskOutliningPass(const jellyfish::Target&)` `@0x13605fe0` |
| **Base class** | `mlir::sparse_core::impl::TileTaskOutliningBase<…>` (TableGen pass base) |
| **Per-op callback** | `walk<TileTaskOp>` callback `@0x136066e0`; nested terminator walk `@0x136071c0` |
| **Outlined op** | `sc_tpu.tile_task` (`mlir::sparse_core::TileTaskOp`) → outlined `func::FuncOp` |
| **Launch op** | `sc_tpu.launch_tile_task` (`mlir::sparse_core::LaunchTileTaskOp`); `create` (FuncOp overload, called here) `@0x145dd0e0`, `build` `@0x1459c060` |
| **Engine tag** | `setAttr("sc.sequencer", StringAttr "execute")` — name 12 chars, value 7 chars |
| **Downstream reader** | `LowerSequencerFunctionsPass::runOnOperation` `@0x13532120` |
| **Confidence** | CONFIRMED (decompile-verified) unless a row or callout says otherwise |

For the engine-selection *decision* (Stream/DMA, the `sc.sequencer` predicates) see [getSequencerType](getsequencertype.md); for where this pass sits relative to the twelve-pass codegen pipeline see [SC Backend Pipeline](sc-backend-pipeline.md); for the per-engine bundle the outlined functions ultimately encode into see [Per-Engine Bundle Slot-Base Map](bundle-slot-base-map.md).

---

## Where the Pass Sits

Outlining runs **before** the twelve-pass SparseCore codegen pipeline (`CustomKernelEmitter::RunPasses`), not inside it. `RunPasses` consumes a module whose per-engine functions already exist and are already tagged — its first pass (`ConvertIntegerMemrefs`) operates on the `func.func`s this pass produced. The ordering is therefore:

```text
  tpu-dialect module with sc_tpu.tile_task ops
        │
        ▼  TileTaskOutliningPass::runOnOperation        @0x13606220   ── THIS PAGE
        │     for each sc_tpu.tile_task:
        │        outline region → func.func (sc.sequencer="execute")
        │        replace op with sc_tpu.launch_tile_task
        │
        ▼  module: SCS control program (sc.sequencer="scs")
        │          + one "execute" func per task, launched from it
        │
        ▼  CustomKernelEmitter::RunPasses  @0x13202780  ── see sc-backend-pipeline.md
        │     12 single-pass managers, ending in LowerToMlo
        │
        ▼  LowerSequencerFunctionsPass     @0x13532120  ── per-engine body lowering
        │     reads sc.sequencer via the ScDialect predicates
        │
        ▼  per-engine bundle codec (SCS / TAC / TEC)   ── see bundle-slot-base-map.md
```

The division of labour with [getSequencerType](getsequencertype.md) is exact: that page's *Layer 2* names this outliner as "where `sc.sequencer` is written" and explicitly defers the pass mechanics here; this page treats the `"scs"`/`"access"`/`"execute"` string set and the per-op region-assignment rule as already-decided inputs and documents only the IR surgery that materializes them.

---

## `runOnOperation` — Pass Driver (`@0x13606220`)

### Purpose

Drive the outlining over a whole `ModuleOp`: gate on a Timem/tile-task conflict, resolve the SparseCore target, then visit every `sc_tpu.tile_task` op and outline it. The pass operates on the module (it is a module-level pass, attached top-level, not func-nested).

### Algorithm

```c
// TileTaskOutliningPass::runOnOperation(this)            @0x13606220
void runOnOperation(TileTaskOutliningPass *this):
    ModuleOp module = this->getOperation();               // this+5 (masked ptr)

    // 1. Timem conflict gate.
    if (overlayer::ContainsExplicitTimemAccess(module)) {  // @0x1395bb60
        // only a problem if the module ALSO launches tile tasks:
        if (walk<TileTaskOp>(module, ContainsTileTask) == found) {  // @ ContainsTileTask lambda
            emitOpError(module)
              << "programs that launch tile tasks while also explicitly accessing "
              << "Timem are not supported";                // two appended literals
            report();
            this->signalPassFailure();                     // this+40 |= 4
            return;
        }
    }

    // 2. Resolve the SparseCore target (gen / geometry) and cache it.
    this->target_ = xla_mlo_util::SparseCoreTargetForModule( // @0x14a8b5c0
                        this->target_arg_, module);          // stored at this+43

    // 3. A naming/context seed: the module body's first op anchor + an
    //    MLIRContext handle (this+352 holds the monotonic func counter).
    ctx = module.getContext();

    // 4. Walk every sc_tpu.tile_task op; outline each via the per-op callback.
    if (walk<TileTaskOp>(module, &outline_one /* @0x136066e0 */) == interrupted)
        this->signalPassFailure();                          // this+40 |= 4
```

Two driver-level facts that a reimplementer must reproduce:

- **The Timem gate is a hard error, not a skip.** A module that both reads/writes Timem *explicitly* and launches tile tasks is rejected with a two-fragment diagnostic (`"programs that launch tile tasks while also explicitly accessing "` + `"Timem are not supported"`). The check is two `walk`s: `ContainsExplicitTimemAccess` (`@0x1395bb60`) for the Timem side and a `ContainsTileTask` walk lambda for the launch side; only their conjunction errors. This protects the tile-overlay machinery (below), which reuses the Timem region.
- **The target is resolved once, before the walk.** `SparseCoreTargetForModule` (`@0x14a8b5c0`) maps the module to a `SparseCoreTarget` and the result is cached on the pass object (`this+43`). The per-op callback reads it back to drive the overlayer decision. The target is what parameterizes 6acc60406-vs-VF/GL behaviour (single `"execute"` vs the `"access"`+`"execute"` split).

### Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `TileTaskOutliningPass::runOnOperation` | `0x13606220` | pass driver: gate → target → walk | CONFIRMED |
| `CreateTileTaskOutliningPass(Target&)` | `0x13605fe0` | pass factory (binds the `Target`) | CONFIRMED |
| `overlayer::ContainsExplicitTimemAccess` | `0x1395bb60` | Timem-conflict gate | CONFIRMED |
| `xla_mlo_util::SparseCoreTargetForModule` | `0x14a8b5c0` | module → `SparseCoreTarget` | CONFIRMED |
| `walk<TileTaskOp>` callback (outline one) | `0x136066e0` | the per-op outlining body | CONFIRMED |
| nested terminator walk (`OutlineSequencerFunction` lambda) | `0x136071c0` | rewires the cloned region's terminators | HIGH |

---

## The Per-Op Callback (`@0x136066e0`)

This is the heart of the pass: given one `sc_tpu.tile_task` op, it builds the outlined function, clones the body, replaces the op with a launch, and stamps the engine attribute. The decompiled callback runs the steps below in order.

### Algorithm

```c
// walk<TileTaskOp> callback "outline one"                @0x136066e0
WalkResult outline_one(TileTaskOp op):
    if (op == null || op.typeID != TileTaskOp::id)        // line 126 — TypeID gate
        return advance;                                   // (walk visits all ops; skip non-tasks)

    Region &region = op.getRegion();                      // op + body offset

    // 1. Live-ins → future function arguments.
    SetVector<Value> liveIns;
    getUsedValuesDefinedAbove(region, liveIns);           // @0x1c974440 (line 155)

    // 2. Read the per-task overlay budget attribute (inherent → dictionary).
    Attribute hwm = op.getInherentAttr("sc.execute_alloc_high_water_mark", 32);
    if (!hwm) hwm = op.getDictionaryAttr().get("sc.execute_alloc_high_water_mark", 32);

    // 3. Build the function name: "execute" + decimal(pass.funcCounter++).
    name = "execute";
    APInt(64, this->funcCounter++).toString(name, /*radix=*/10);   // pass+352 counter

    // 4. Argument types: each live-in must be a STATIC memref.
    SmallVector<Type> argTypes;
    for (Value v : liveIns) {
        MemRefType m = dyn_cast<MemRefType>(v.getType());
        // CHECK(memref.hasStaticShape()) — abort otherwise:
        if (!m || !m.hasRank() || hasDynamicDim(m.getShape()))     // @0x1d896e20 / @0x1d8921e0
            LOG(FATAL) << "Tile tasks only support capture of static memrefs"; // .cc:62
        argTypes.push_back(m);
    }
    FunctionType fty = FunctionType::get(ctx, argTypes, /*results=*/{});  // @0x1d891c80

    // 5. Create the func and its entry block.
    FuncOp fn = func::FuncOp::create(builder, loc, name, fty);     // @0x1d8006a0
    Block *entry = fn.addEntryBlock();                             // @0xea4b680

    // 6. Map each live-in Value → the matching entry block argument.
    IRMapping map;
    for (i in 0..liveIns.size())
        map.map(liveIns[i], entry->getArgument(i));    // DenseMap<Value,Value>

    // 7. Clone the tile_task body into the function under the mapping.
    region.cloneInto(fn.getBody(), map);               // @0x1d8dfa60

    // 8. Wire the synthetic entry to the cloned first block.
    cf::BranchOp::create(builder, clonedEntry, /*operands=*/{});   // @0x17bd69a0

    // 9. Fix up the cloned region's terminators (sequencer-function form).
    walk<TerminatorOp>(fn, OutlineSequencerFunction_terminatorCb); // @0x136071c0

    // 10. Stamp the engine + budget attributes on the outlined func.
    fn.setAttr("sc.sequencer", StringAttr::get(ctx, "execute"));   // @0xea37860, value 7 chars
    if (hwm)
        fn.setAttr("sc.alloc_high_water_mark", hwm);               // 24-char name

    // 11. Tile-overlayer path (gated on the target/module).
    Value overlayAlloc = null;
    if (overlayer::IsTileOverlayerEnabled(target)) {               // @0x1395d880
        int sz = overlayer::GetTileOverlaysSize(target);           // @0x1395ba20
        fn.setAttr("sc.func_size_limit", getI32IntegerAttr(sz));   // 18-char name
        Type ovTy = overlayer::GetTileOverlayMemRefType(target,…); // @0x1395b960
        overlayAlloc = memref::AllocOp::create(builder, loc, ovTy);// @ memref alloc
        sparse_core::PrefetchTileTaskOp::create(builder, loc, fn, overlayAlloc);
    }

    // 12. Replace the original op with a launch of the outlined func, then erase it.
    Value task = op.getOperand(0);                     // the tile-task descriptor operand
    LaunchTileTaskOp::create(builder, loc, /*execute=*/fn /*FuncOp*/, overlayAlloc, task,
                             /*captures=*/liveIns, /*clear_ibuf=*/true);   // @0x145dd0e0 (FuncOp overload)
    op.erase();                                        // @0x1d8ccd20
    return advance;
```

### Step notes

The numbered steps each carry a reimplementation subtlety worth calling out.

- **Step 1 — live-ins are the capture set.** `getUsedValuesDefinedAbove(region, SetVector)` (`@0x1c974440`) returns every SSA value the region uses but defines outside it. These become the function arguments *in iteration order*, so the launch must pass them in the same order (the callback iterates the same `SetVector` twice — once to build arg types, once to build the IRMapping and the launch operands). Order is the contract between the launch site and the function signature.
- **Step 4 — static-memref capture is enforced, fatally.** The argument-type loop requires each captured value to be a `MemRefType` (`TypeIDResolver<MemRefType>` check) with a rank and a fully static shape (the shape walk treats `0x8000000000000000` = `ShapedType::kDynamic` as the failure sentinel). A dynamic dimension trips `LOG(FATAL)` with `"Tile tasks only support capture of static memrefs"` at `tile_task_outlining_pass.cc:62`. A reimplementer cannot outline a tile task that captures a dynamically-shaped buffer — the SC tile model has no run-time-sized descriptor for it.
- **Step 7 — `cloneInto`, not move.** `Region::cloneInto(funcBody, IRMapping)` (`@0x1d8dfa60`) *clones* the body under the value mapping; the original ops are not moved, they are deleted in step 12 when the op is erased. This is why a fresh IRMapping (built in step 6 from live-in → block-arg) is required: every external use inside the clone is rewritten to the function's block argument.
- **Step 9 — the terminator fix-up is the "sequencer function" shape.** After cloning, a second `walk` (`@0x136071c0`) over the new function rewrites its region terminators into the sequencer-function form (the embedded lambda is named `OutlineSequencerFunction`). This is the step that turns a generic cloned region into a well-formed `func.func` body. It was not bit-traced line-by-line (HIGH).
- **Steps 10–11 — three attribute classes.** The outlined function carries (a) the engine tag `sc.sequencer = "execute"`; (b) the per-task allocation budget `sc.alloc_high_water_mark`, copied from the op's `sc.execute_alloc_high_water_mark` inherent attr; and, only when the tile-overlayer is enabled, (c) `sc.func_size_limit` (an i32 sized from `GetTileOverlaysSize`). The overlayer path also injects a `memref.alloc` for the overlay buffer and a `PrefetchTileTaskOp` ahead of the launch.

> **NOTE — the high-water-mark attribute is read from the op and re-stamped onto the func under a different name.** The callback reads `sc.execute_alloc_high_water_mark` (32-char name) off the `tile_task` op, then writes the value back as `sc.alloc_high_water_mark` (24-char name) on the outlined function. The earlier raw analysis recorded this as the pass *setting* `sc.execute_alloc_high_water_mark`; the decompile shows that name is the *source* (read), and `sc.alloc_high_water_mark` is the *destination* (write). Treat the 32-char name as an input attribute on the task and the 24-char name as the output on the function.

> **GOTCHA — the function name and the attribute value are not the same string.** The symbol is `"execute" + N` (e.g. `execute0`, `execute1`, …), uniquified by a per-pass counter via `APInt::toString`. The `sc.sequencer` attribute value is the bare `"execute"` (exactly 7 chars — the length the `HasExecuteSequencerTypeAttribute` predicate at `@0x1459a020` checks). A reimplementer that sets `sc.sequencer` to the *symbol name* will fail the downstream length-7 byte compare in [getSequencerType](getsequencertype.md). Name the symbol freely; tag the engine with the bare word.

### Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `getUsedValuesDefinedAbove` | `0x1c974440` | region live-in collection → arg/capture set | CONFIRMED |
| `BaseMemRefType::hasRank` | `0x1d896e20` | capture rank check | CONFIRMED |
| `MemRefType::getShape` | `0x1d8921e0` | capture static-shape check | CONFIRMED |
| `FunctionType::get` | `0x1d891c80` | build the outlined func type | CONFIRMED |
| `func::FuncOp::create` | `0x1d8006a0` | create the outlined function | CONFIRMED |
| `FunctionOpInterface::addEntryBlock` | `0xea4b680` | entry block + block args | CONFIRMED |
| `Region::cloneInto` | `0x1d8dfa60` | clone the task body under IRMapping | CONFIRMED |
| `cf::BranchOp::create` | `0x17bd69a0` | wire the synthetic entry | CONFIRMED |
| `StringAttr::get` | `0x1d85dda0` | build attr name/value StringAttrs | CONFIRMED |
| `Operation::setAttr` | `0xea37860` | stamp `sc.sequencer` / budget attrs | CONFIRMED |
| `Operation::erase` | `0x1d8ccd20` | delete the original `tile_task` | CONFIRMED |
| `overlayer::IsTileOverlayerEnabled` | `0x1395d880` | gate the overlay path | CONFIRMED |
| `overlayer::GetTileOverlaysSize` | `0x1395ba20` | i32 size for `sc.func_size_limit` | CONFIRMED |
| `overlayer::GetTileOverlayMemRefType` | `0x1395b960` | overlay-buffer memref type | CONFIRMED |
| `memref::AllocOp::create` | `0x183015a0` | overlay buffer allocation | CONFIRMED |
| `sparse_core::PrefetchTileTaskOp::create` | `0x145f4cc0` | overlay prefetch before launch | CONFIRMED |
| `LaunchTileTaskOp::create` (FuncOp overload) | `0x145dd0e0` | the launch the callback emits | CONFIRMED |

---

## The Launch Op — `sc_tpu.launch_tile_task`

### Purpose

The op left in place of the outlined `tile_task`. It names the outlined `"execute"` function by symbol and carries the operands the function will receive when it runs. A later lowering (`LaunchTileTaskOpLowering` `@0x135901c0`, the LLVM conversion pattern) turns it into the actual launch sequence; the arguments-spill pass resolves its `execute_func` symbol back to the `func::FuncOp`.

### Op shape (from `build` / `create`)

Two `create` overloads exist: the `func::FuncOp`-taking one (`@0x145dd0e0`, signature `create(OpBuilder&, Location, func::FuncOp, Value, Value, ValueRange, bool)`) — **the one the outliner callback calls** (verified at the call site `136070ec → 0x145dd0e0`) — and a pre-formed-`Value` variant (`@0x145dcfa0`, signature `create(OpBuilder&, Location, Value, Value, ValueRange, bool)`). The FuncOp overload forwards to the canonical builder `LaunchTileTaskOp::build` (`@0x1459c060`, signature `build(OpBuilder&, OperationState&, Value, Value, UnitAttr, FlatSymbolRefAttr, ValueRange)`), which resolves the func to its `FlatSymbolRefAttr` and sets `clear_ibuf` as a `UnitAttr`. Both overloads fix the same op surface:

```c
// LaunchTileTaskOp::create(builder, loc, fn /*FuncOp*/, alloc, task, captures, clear_ibuf)  @0x145dd0e0
op = OperationState("sc_tpu.launch_tile_task", 23);   // op-name literal, 23 chars
// forwards to build(@0x1459c060):
op.addOperands(alloc);         // the overlay-alloc Value (overlayer path)
op.addOperands(task);          // the tile-task descriptor Value
op.addOperands(captures);      // N — the ValueRange of region live-ins
if (clear_ibuf)
    properties.clear_ibuf = builder.getUnitAttr();    // UnitAttr present ⇔ true
properties.execute_func = FlatSymbolRefAttr(fn.getSymName());  // symbol of the outlined func
return builder.create(op);     // verified TypeID == LaunchTileTaskOp::id
```

| Element | Kind | Source | Confidence |
|---|---|---|---|
| op name | string `"sc_tpu.launch_tile_task"` (23) | `create` `@0x145dd0e0` / `@0x145dcfa0` | CONFIRMED |
| `execute_func` | `FlatSymbolRefAttr` | `build` `@0x1459c060`; accessor `getExecuteFunc` `@0x145dcf40` | CONFIRMED |
| `clear_ibuf` | `UnitAttr` (present ⇔ true) | `create` `bool` arg; accessor `getClearIbuf` `@0x145dcf20` | CONFIRMED |
| operand 0 (`task`) | `Value` | `addOperands` site 1 | CONFIRMED |
| operand 1 (`alloc`) | `Value` (overlay buffer) | `addOperands` site 2 | HIGH |
| trailing operands | `ValueRange` (captures) | `addOperands` site 3 | CONFIRMED |

### The `execute_func` symbol round-trip

The launch does not embed the function — it references it by symbol. `LaunchTileTaskOp::getExecuteFunc` (`@0x145dcf40`) returns the `FlatSymbolRefAttr`'s root-reference `StringAttr` value (the function name `"execute<N>"`). The free function `GetExecuteFunc` (`@0x136054e0`) resolves it: it builds a `SymbolTable` over the launch's parent op, `lookup`s `launch.getExecuteFunc()`, and asserts the result is non-null with `"execute_func != nullptr"` at `tile_task_arguments_spill.cc:70`. This is the binary's guarantee that every launch points at a real outlined function in the same module.

> **QUIRK — the outliner calls `create` with `clear_ibuf = true` unconditionally.** In the callback (`@0x136066e0`) the launch is built with the `bool` argument set, so the produced op always carries the `clear_ibuf` `UnitAttr`. The attribute exists so a downstream consumer can suppress instruction-buffer clearing, but the outliner itself never produces the cleared-`false` form — a reimplementer mirroring this pass should emit the unit attribute present.

---

## Engine Assignment — What the String Means

The pass writes only `"execute"` on 6acc60406 (`gfc`). The full three-value mapping (`"scs"` / `"access"` / `"execute"`) and the per-op rule that decides which region an op lands in are owned by [getSequencerType](getsequencertype.md); this section records only what the *outliner* produces and the per-generation shape.

| `sc.sequencer` | Engine | Produced by | Present on | This pass emits |
|---|---|---|---|---|
| `"scs"` | SCS (scalar control) | the *enclosing* function (the program that issues launches) | VF · GL · GF | indirectly (the parent func) |
| `"access"` | TAC (tile-access / DMA) | the VF/GL TAC split of the same pass | VF · GL only | not in the gfc callback |
| `"execute"` | TEC (vector compute) | this callback (`fn.setAttr`, value 7 chars) | VF · GL · GF | **yes — every task** |

> **CORRECTION — the 6acc60406 callback stamps only `"execute"`.** The decompiled per-op callback (`@0x136066e0`) sets `sc.sequencer = "execute"` and nothing else; there is no `"access"`-emitting branch in this `gfc` build. On Viperfish/Ghostlite the same `Target`-parameterized pass produces an `"access"` (TAC) function in addition, but that split was not bit-traced and the gfc binary cannot reach it (no `SparseCoreTacCodecBase` on 6acc60406 — see [getSequencerType](getsequencertype.md#codec-base-presence-confirms-the-missing-tac-on-gfc)). The per-op Access-vs-Execute rule is owned upstream and flagged **LOW** here.

> **NOTE — the parent function's `"scs"` tag is not set by this pass.** The outlined `"execute"` function is created here; the `"scs"` tag on the *enclosing* control program is attached separately (it is the function the `sc_tpu.launch_tile_task` ops live in after this pass runs). A reimplementer must ensure the control program carries `sc.sequencer = "scs"` so the launches and their surrounding sync/addressing code lower onto the SCS codec.

---

## Downstream Read-Back

The outlined functions are consumed by `LowerSequencerFunctionsPass::runOnOperation` (`@0x13532120`), which walks each `LLVM::LLVMFuncOp`, reads its `sc.sequencer` via the `ScDialect` predicates, and lowers the per-engine body. The string-to-engine predicates are byte-confirmed:

- `ScDialect::HasCoreSequencerTypeAttribute` (`@0x14599ec0`) — matches `"scs"` (len 3).
- `ScDialect::HasExecuteSequencerTypeAttribute` (`@0x1459a020`) — matches `"execute"` (len 7).
- `ParentHasSequencerTypeAttribute` (`@0x1353e980`) — walks `Block::getParentOp` to the enclosing `LLVMFuncOp` and requires it tagged `"scs"` or `"execute"`. The `TileTaskOp` / `LaunchTileTaskOp` / `PrefetchTileTaskOp` / `TileTaskWaitOp` family carries this trait.

The full predicate decode (the little-endian byte literals, the `"access"`-has-no-predicate asymmetry, the trait enforcement) lives on [getSequencerType](getsequencertype.md#the-attribute-values--byte-confirmed); it is not repeated here. The hand-off contract is: **this pass writes the string; that pass reads it.** The selected `TpuSequencerType` codec template parameter (`{SCS=3, TAC=4, TEC=5}`) then drives the per-engine bundle encoder — see [Per-Engine Bundle Slot-Base Map](bundle-slot-base-map.md).

---

## Reimplementation Checklist

To reproduce the SparseCore region→sequencer outliner:

1. **Run as a module-level pass before codegen.** Gate on the Timem/tile-task conflict (hard error), resolve the `SparseCoreTarget` once, then `walk` every `sc_tpu.tile_task` op.
2. **Outline by the standard region-outline recipe.** Live-ins via `getUsedValuesDefinedAbove` → arg types → `FuncOp::create` + `addEntryBlock` → `IRMapping(live-in → block-arg)` → `Region::cloneInto` → `cf::BranchOp` entry wiring → terminator fix-up.
3. **Enforce static-memref capture.** Reject (fatally) any captured value that is not a statically-shaped `MemRefType`.
4. **Name the symbol `"execute"+N`; tag the attribute bare `"execute"`.** Keep the two distinct — the downstream reader length-checks the bare 7-char value.
5. **Stamp the budget attributes.** Copy the op's `sc.execute_alloc_high_water_mark` to the func's `sc.alloc_high_water_mark`; on the overlayer path add `sc.func_size_limit` (i32), a `memref.alloc` overlay buffer, and a `PrefetchTileTaskOp`.
6. **Replace with `sc_tpu.launch_tile_task` and erase the original.** The launch references the func by `FlatSymbolRefAttr execute_func`, carries the captures as a `ValueRange` in live-in order, and sets the `clear_ibuf` `UnitAttr`.
7. **Defer the engine *decision*.** This pass materializes engine membership; it does not decide it. Drive the Access-vs-Execute split (on TAC gens) from the upstream Stream/DMA classifier (see [getSequencerType](getsequencertype.md)).

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| `runOnOperation` gates on Timem+tile-task conflict, resolves target, walks `TileTaskOp` | decompile `@0x13606220`: `ContainsExplicitTimemAccess` + two error fragments + `SparseCoreTargetForModule` + `walk` | CONFIRMED |
| Per-op callback outlines via live-ins → FuncOp → cloneInto → launch → erase | decompile `@0x136066e0` (lines 126–476) | CONFIRMED |
| Captures = `getUsedValuesDefinedAbove`, must be static memrefs | `@0x1c974440`; `hasRank`/`getShape` + `LOG(FATAL)` `tile_task_outlining_pass.cc:62` | CONFIRMED |
| Func name = `"execute"` + decimal counter (`APInt::toString`) | callback: `qmemcpy("execute",7)` + counter `pass+352` + `APInt::toString` | CONFIRMED |
| `sc.sequencer` stamped `"execute"` (12-char name, 7-char value) via `setAttr` | `setAttr` `@0xea37860` with `StringAttr "sc.sequencer"`/`"execute"` | CONFIRMED |
| Budget attr read from `sc.execute_alloc_high_water_mark` (32), written as `sc.alloc_high_water_mark` (24) | callback `getInherentAttr(…,32)` read + `setAttr(…,24)` write | CONFIRMED |
| Overlayer path adds `sc.func_size_limit` (18) + `memref.alloc` + `PrefetchTileTaskOp` | `IsTileOverlayerEnabled`/`GetTileOverlaysSize`/`GetTileOverlayMemRefType` + create calls | CONFIRMED |
| Launch op name `"sc_tpu.launch_tile_task"` (23 chars); `execute_func` FlatSymbolRef; `clear_ibuf` UnitAttr | both `create` overloads (`@0x145dd0e0` / `@0x145dcfa0`) carry the op-name literal; `build` `@0x1459c060` signature; `getExecuteFunc`/`getClearIbuf` accessors | CONFIRMED |
| Outliner always sets `clear_ibuf = true` | callback passes `bool=1` to `create` | CONFIRMED |
| `GetExecuteFunc` resolves the symbol via `SymbolTable::lookup`, asserts non-null | `@0x136054e0` `tile_task_arguments_spill.cc:70` | CONFIRMED |
| 6acc60406 callback emits only `"execute"`; VF/GL also emit `"access"` | gfc callback has no `"access"` branch; cross-gen pass is Target-parameterized (not traced) | CONFIRMED (gfc); LOW (VF/GL split) |
| Callback calls the `func::FuncOp` `LaunchTileTaskOp::create` overload `0x145dd0e0` (the `Value`-only overload `0x145dcfa0` is a sibling, not the call here) | disasm call site `136070ec → 0x145dd0e0`; `0x145dd0e0` forwards to `build` `@0x1459c060` | CONFIRMED |
| Terminator fix-up walk `@0x136071c0` (`OutlineSequencerFunction` lambda) reshapes the cloned region | callback nested `walk` call site; lambda name in symbol | HIGH |
| Per-op Access-vs-Execute region-selection rule | not bit-traced; owned by getSequencerType | LOW |

---

## Cross-References

- [getSequencerType](getsequencertype.md) — the engine-selection *decision* (Stream/DMA classifier + the `sc.sequencer` read-back predicates) that this pass's output feeds; owner of the per-op region-assignment rule.
- [SC Backend Pipeline](sc-backend-pipeline.md) — the twelve-pass codegen pipeline that runs *after* this outliner on the `func.func`s it produces.
- [SparseCore Overview](overview.md) — the three engine classes (SCS/TAC/TEC), per-gen presence, and the SCv0 deprecation context.
- [SCS (Scalar) Engine](scs-engine.md) — the `"scs"` control program that issues the `sc_tpu.launch_tile_task` ops.
- [TAC Engine](tac-engine.md) — the `"access"` tile-fetch engine and its 6acc60406 removal (why the gfc callback emits only `"execute"`).
- [TEC (Vector) Engine](tec-engine.md) — the `"execute"` vector engine the outlined functions target.
- [Per-Engine Bundle Slot-Base Map](bundle-slot-base-map.md) — the per-engine bundle the outlined, lowered functions ultimately encode into.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore engines — [back to index](../index.md)
