# LowerToSparseCoreLlvm

> *All addresses, symbols, and offsets on this page were read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset). The `.symtab` is **not** stripped; every claim anchors to a demangled symbol or a decompiled body. Other versions will differ — treat every VA as version-pinned.*

## Abstract

`LowerToSparseCoreLlvmPass` is the terminal MLIR lowering on the SparseCore path: it takes a module that still carries the `mlir::sparse_core` (`ScDialect`) op surface — gather/scatter streams, circular-buffer registers, sync-flag waits, simple DMAs, EUP transcendentals, address-space casts — and drops it into the `llvm` dialect plus the SparseCore-specific `llvm_tpu` intrinsic dialect (the 1356-intrinsic `tpu_*` catalog documented in [LlvmTpu Intrinsic Catalog](llvmtpu-intrinsic-catalog.md)). It is the SparseCore sibling of the TensorCore [tpu → LLO ODS](tpu-to-llo-ods.md) descent and the stage immediately after the [LowerToMlo DMA bridge](lower-to-mlo-dma-bridge.md) has expanded tiled memrefs. Where the upstream MLIR `convert-to-llvm` lowers `arith`/`memref`/`func` to LLVM, this pass adds ~118 SparseCore `*OpLowering` conversion patterns that select the right `tpu_*` intrinsic for each `ScDialect` op, all riding on the type map built by the [SCTypeConverter](sc-type-converter.md).

The reader who knows MLIR's `DialectConversion` should hold one structural fact: this is **not one `applyFullConversion`** but a three-substage driver inside `runOnOperation` (`0x13566d00`). First a *standalone* `scf` → `cf` lowering runs on the whole module (no type conversion); then a per-`func::FuncOp` `ScDialect` → `llvm`/`llvm_tpu` conversion runs (this is where the 118 patterns and the `SCTypeConverter` live, inside `lowerFunc` `0x13568280`); then a per-`LLVM::LLVMFuncOp` *assert + memref-finalize* pass cleans up `cf.assert` and any residual memref ops (`lowerAsserts`). A `lower-scf-to-cfg-only` test flag can stop after the first substage. This page owns the **per-class rewrite bodies** (the `matchAndRewrite` algebra: operand mapping, attribute filtering, intrinsic selection, `replaceOp`), the **scf→cfg lowering** (`lowerScfToCfg`, with its two custom `ForLowering`/`IndexSwitchLowering` patterns), and the **pass driver itself** (the three substages, the factory, the conversion targets).

The pieces this page deliberately does **not** re-derive, because a sibling owns each: the address-space-ID → `!llvm.ptr<N>` type map and the sequencer-context flatten, the `CheckAddressSpaces` legality gate, and the 42-instantiation EUP roster all live on [SCTypeConverter](sc-type-converter.md); the full AS-id ↔ `MemorySpace` ↔ pool table is on [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md); the per-cast `addrspacecast` instruction selection is on [addrspacecast ISel](../sparsecore/addrspacecast-isel.md). This page documents how the rewrite bodies *consume* the converted types and *reach* the intrinsics.

For reimplementation, the contract is:

- **The three-substage driver.** `scf→cf` (module-wide, untyped) → per-func `ScDialect→llvm`/`llvm_tpu` (typed) → per-func assert/memref-finalize. The order is mandatory — `scf` ops must be control-flow before the typed conversion runs, and `cf.assert` must survive into the LLVM-func substage.
- **The uniform rewrite shape.** Every `*OpLowering::matchAndRewrite` is "resolve operands to LLVM values → `FilterLLVMAttributes` (drop `access_groups`) → select the leaf `tpu_*` intrinsic by dtype/memspace/predicate → `tpu_X::create(b, loc, …)` → `replaceOp`". Six representative bodies are byte-decoded.
- **The dispatch keys per class.** DMA = (srcMemSpaceID, dstMemSpaceID) tuple table; stream = (dtype, off-tile memspace, verb) lambda table; wait = comparison-predicate attr; sync = local/tile/remote attr; addrspacecast = `convertType(src) == convertType(dst)` elide-or-fail.

| | |
|---|---|
| **Pass class** | `xla::tpu::sparse_core::(anon)::LowerToSparseCoreLlvmPass` (ODS base `mlir::sparse_core::impl::LowerToSparseCoreLlvmBase`) |
| **Pass kind / scope** | `builtin.module` pass (object size `0x298`) |
| **Driver** | `LowerToSparseCoreLlvmPass::runOnOperation` @ `0x13566d00` |
| **Factory** | `CreateLowerToSparseCoreLlvmPass(Target&, InlinedVector<long,4>, SparseCoreConfig&, DebugInfoTracker*)` @ `0x135667c0` |
| **scf→cfg substage** | `lowerScfToCfg` (collector lambda `0x13572640`); custom `ForLowering` `0x135707c0`, `IndexSwitchLowering` `0x13571ec0` |
| **per-func typed substage** | `LowerToSparseCoreLlvmPass::lowerFunc(func::FuncOp)` @ `0x13568280` |
| **assert/memref substage** | `lowerAsserts` (walks `LLVM::LLVMFuncOp`); `AssertOpLowering::matchAndRewrite` @ `0x135b7080` |
| **Test-only flag** | `lower-scf-to-cfg-only` (`"Only lower scf ops to cf ops (for testing)"`, 42 chars) |
| **Attribute filter** | `FilterLLVMAttributes` @ `0x135b7a20` — drops `access_groups` (13 chars) |
| **Pattern count** | ~118 `*OpLowering` classes (templated families instantiate more) |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

---

## The Pass Driver — Three Substages

### Purpose

`runOnOperation` (`0x13566d00`) is a `builtin.module` pass driver, not a single conversion. It runs three distinct `applyFullConversion` campaigns in sequence, each with its own `ConversionTarget` and pattern set, separated by IR walks. The split exists because the three campaigns operate on three different op universes — structured control flow (`scf`), the `ScDialect` op surface (per `func.func`), and the finalized `llvm.func` bodies — and each needs a clean legalizer state. A reimplementer who collapses these into one `applyFullConversion` will deadlock the legalizer: the typed `ScDialect` patterns expect control flow already lowered, and the assert/memref finalize expects the SC ops already gone.

### Entry Point

```text
RegisterAllPhases (0xf849ec0)  →  Phase 2a (SparseCore custom-call path)
  └─ CreateLowerToSparseCoreLlvmPass (0x135667c0)   ── builds the pass object (0x298 B)
       └─ runOnOperation (0x13566d00)               ── module pass driver
            ├─ [substage 1] lowerScfToCfg           ── scf → cf, module-wide, untyped
            │     ForLowering (0x135707c0)           ──   scf.for → cf branches (benefit 2)
            │     IndexSwitchLowering (0x13571ec0)   ──   scf.index_switch → cf (benefit 2)
            │     populateSCFToControlFlowConversionPatterns  ── upstream scf.{if,while,parallel}
            ├─ [substage 2] per func::FuncOp: lowerFunc (0x13568280)
            │     SCTypeConverter + ~118 *OpLowering patterns → llvm / llvm_tpu
            └─ [substage 3] per LLVM::LLVMFuncOp: lowerAsserts
                  AssertOpLowering (0x135b7080) + populateFinalizeMemRefToLLVMConversionPatterns
```

### Algorithm

```c
function runOnOperation(pass):                          // 0x13566d00
    module = pass.getOperation()                        // builtin.module
    ctx    = module.getContext()

    // ---- substage 1: scf -> cf, module-wide, NO type conversion ----
    scfPatterns = RewritePatternSet(ctx)
    scfPatterns.add<ForLowering>(benefit=2)             // 0x135707c0  "scf.for"
    scfPatterns.add<IndexSwitchLowering>(benefit=2)     // 0x13571ec0  "scf.index_switch"
    populateSCFToControlFlowConversionPatterns(scfPatterns)   // upstream scf.{if,parallel,while}
    frozen1 = FrozenRewritePatternSet(scfPatterns)

    // collect every scf.{for,if,parallel,while,index_switch} op into a worklist
    worklist = []
    module.walk(lowerScfToCfg_collect)                  // callback 0x13572640 (lambda #1)
    for op in worklist:                                 // one applyFullConversion PER root op
        target = ConversionTarget(ctx)
        target.addIllegalOp("scf.for")                  // setOpAction(..., 1)  via lambda #2
        target.addDynamicallyLegalOp<scf.{If,Parallel,While,IndexSwitch}>(...)
        target.markOpRecursivelyLegal("scf.for","scf.if","scf.parallel","scf.while","scf.index_switch")
        target.markUnknownOpDynamicallyLegal(lambda #3)  // setLegalityCallback (catch-all legal)
        if applyFullConversion(op, target, frozen1) != success:
            pass.signalPassFailure(); return            // sets bit 2 of pass flags
    if pass.lowerScfToCfgOnly:                           // test flag, *(pass+456)
        return                                          // stop after scf->cf

    // ---- substage 2: per-func ScDialect -> llvm / llvm_tpu (TYPED) ----
    pass.target = SparseCoreTargetForModule(pass.cfg, module)   // xla_mlo_util, 0x...
    for fn in module.getOps<func::FuncOp>():
        if !lowerFunc(pass, fn):                        // 0x13568280
            pass.signalPassFailure(); return

    // ---- substage 3: per-llvm.func assert + memref finalize ----
    if module.walk<LLVM::LLVMFuncOp>(lowerAsserts) == interrupt:
        opts = LowerToLLVMOptions(ctx); opts.useBarePtrCallConv = true
        opts.dataLayout = DataLayout("…-S64")           // 64-bit stack alignment
        tc = LLVMTypeConverter(ctx, opts)
        tc.registerTypeAttributeConversion(             // SC memory-space attr -> i64 addrspace
            lowerAsserts_memSpaceLambda)                //   (no sequencer flatten; see SCTypeConverter)
        assertPatterns = RewritePatternSet(ctx)
        assertPatterns.add<AssertOpLowering>(benefit=1, // 0x135b7080  "cf.assert"
            granuleMask = -1 << (log2(stackBytes) - log2(target.GranuleBytes())))
        populateFinalizeMemRefToLLVMConversionPatterns(tc)
        llvmTarget = LLVMConversionTarget(ctx)
        llvmTarget.addLegalDialect("llvm_tpu")          // setDialectAction, 1
        llvmTarget.addIllegalOp("builtin.module")       // setOpAction, 0
        llvmTarget.addDynamicallyLegalOp("llvm.alloca", "builtin.unrealized_conversion_cast")
        if applyFullConversion(module, llvmTarget, assertPatterns) != success:
            pass.signalPassFailure()
    return
```

### Function Map

| Function | VA | Role |
|---|---|---|
| `runOnOperation` | `0x13566d00` | the three-substage driver |
| `CreateLowerToSparseCoreLlvmPass` | `0x135667c0` | factory; takes `Target&`, `InlinedVector<long,4>`, `SparseCoreConfig&`, `DebugInfoTracker*` |
| `lowerFunc` | `0x13568280` | per-`func.func` typed `ScDialect`→LLVM conversion (substage 2) |
| `lowerScfToCfg` collector lambda | `0x13572640` | walk callback; pushes `scf.{for,if,parallel,while,index_switch}` to worklist |
| `ForLowering::matchAndRewrite` | `0x135707c0` | `scf.for` → `cf` branches (custom, benefit 2) |
| `IndexSwitchLowering::matchAndRewrite` | `0x13571ec0` | `scf.index_switch` → `cf` (custom, benefit 2) |
| `AssertOpLowering::matchAndRewrite` | `0x135b7080` | `cf.assert` lowering with granule mask (substage 3) |
| `getArgument` / `getDescription` | `0x13566cc0` / `0x13566ce0` | ODS pass registration metadata |
| `getDependentDialects` | `0x13566c00` | declares the dialects the pass instantiates |

### The Factory and the Test Flag

`CreateLowerToSparseCoreLlvmPass` (`0x135667c0`) `new`s a `0x298`-byte pass object whose op-name anchor is `"builtin.module"` (14 chars, stored at `+16`). Three pieces of state are captured into the object: the `xla::jellyfish::Target&` (at `+536`), an `absl::InlinedVector<long,4>` of core IDs (at `+552`, copied via `Storage::InitFrom` when it spills past the inline 4), and a `SparseCoreConfig` (placement-constructed at `+592`), plus a `DebugInfoTracker*` at `+656`. The one `cl::opt<bool>` it registers is `lower-scf-to-cfg-only` (21-char name, description `"Only lower scf ops to cf ops (for testing)"`, 42 chars) — when set, the driver returns after substage 1. This is the only tunable on the pass; it exists so a test can inspect the `scf`→`cf` output without descending into the (target-dependent) intrinsic selection.

> **NOTE —** the pass is a `builtin.module` pass, but substages 2 and 3 iterate *functions* internally rather than relying on a `func`-scoped pass manager. The reason is the substage ordering: a `func`-pass manager would interleave the three campaigns per-function, but the driver needs all of substage 1 (`scf`→`cf`) done module-wide *before* any function enters substage 2's typed conversion. Keeping the whole thing a module pass with explicit inner loops is what enforces "all control flow flat, then all SC ops lowered, then all asserts finalized."

> **QUIRK —** substage 1 runs `applyFullConversion` **once per collected root `scf` op**, not once over the module. The collector lambda (`0x13572640`) gathers each top-level `scf.{for,if,parallel,while,index_switch}` into a worklist, then the driver loops, building a fresh `ConversionTarget` and calling `applyFullConversion` on each root individually. A reimplementer who runs a single module-wide `applyFullConversion` for `scf`→`cf` will get the same *result* on well-formed input but a different failure granularity — the per-root loop lets one malformed loop fail without aborting the others' diagnostics.

---

## The scf → cf Lowering (Substage 1)

### Purpose

Before any `ScDialect` op is typed-converted, all structured control flow must become unstructured `cf` branches, because the `tpu_*` intrinsics and the LLVM dialect have no notion of `scf.for`/`scf.if` regions. Substage 1 is a plain (untyped) `scf` → `cf` legalization: it reuses MLIR's upstream `populateSCFToControlFlowConversionPatterns` for `scf.if`/`scf.while`/`scf.parallel` but supplies **two custom higher-benefit patterns** for `scf.for` and `scf.index_switch`, which the SparseCore path needs lowered differently from upstream (e.g. SparseCore's loop induction and the sequencer-aware index switch).

### The two custom patterns

| Pattern | Source op | VA | Benefit | Anchor string |
|---|---|---|---|---|
| `ForLowering` | `scf.for` | `0x135707c0` | 2 | `"…ForLowering]"` (57 chars) |
| `IndexSwitchLowering` | `scf.index_switch` | `0x13571ec0` | 2 | `"…IndexSwitchLowering]"` (65 chars) |

Both are registered with `PatternBenefit(2)` — higher than the upstream `populateSCFToControlFlowConversionPatterns` patterns (default benefit 1) — so they win the match for `scf.for` and `scf.index_switch` while upstream handles the rest. The collector lambda (`0x13572640`) is a `function_ref<void(Operation*)>` callback that, on each visited op, compares the op's `TypeID` against `scf::{IndexSwitchOp, WhileOp, ParallelOp, ForOp, IfOp}` (a five-way `TypeIDResolver` identity check) and, on a hit, appends the op pointer to the worklist `SmallVector`.

### The per-root conversion target

For each collected root op, the driver builds a `ConversionTarget` that:

- marks `scf.for` **illegal** (`setOpAction(..., 1)`) so it must be rewritten;
- marks `scf.{If, Parallel, While, IndexSwitch}` **dynamically legal** (legal iff already lowered);
- marks all five `scf.{for, if, parallel, while, index_switch}` **recursively legal** as *containers* (`markOpRecursivelyLegal`) so nested regions are walked;
- installs a catch-all `markUnknownOpDynamicallyLegal` (legality callback lambda #3) so any non-`scf` op is legal as-is.

`applyFullConversion(rootOp, target, frozenScfPatterns)` then drives the rewrite. A `false` return signals pass failure (sets bit 2 of the pass flags at `pass+40`).

---

## Per-Class Rewrite Bodies (Substage 2 — `lowerFunc`)

### The uniform rewrite shape

`lowerFunc` (`0x13568280`) builds the `SCTypeConverter` (see that page) and registers ~118 `*OpLowering` conversion patterns, then runs `applyFullConversion` over a single `func::FuncOp`. Every one of those patterns follows the **same five-step `matchAndRewrite` shape**, regardless of class:

```c
function matchAndRewrite(op, adaptor, rewriter):        // generic ScDialect *OpLowering
    // (a) resolve operands: memref+index -> raw LLVM pointer/offset Value
    ptr   = getStridedElementPtr(adaptor.memref, adaptor.indices)   // SC-specialised GEP
    vals  = [ptr, adaptor.scalarOperands…]
    // (b) filter the op's attribute dictionary
    attrs = FilterLLVMAttributes(op.getAttrs())          // 0x135b7a20 — drops "access_groups"
    // (c) pick the leaf tpu_* intrinsic by dtype / memspace / predicate
    intr  = selectIntrinsic(op, target)                  // class-specific dispatch key
    // (d) create the intrinsic
    res   = intr::create(rewriter, op.getLoc(), {resultType}, vals, attrs)
    // (e) replace
    rewriter.replaceOp(op, res)
    return success
```

The attribute filter is uniform: `FilterLLVMAttributes` (`0x135b7a20`) forwards the `ScDialect` op's attribute dictionary to the new `tpu_*` intrinsic but **drops `access_groups`** — a 13-char attribute name decoded from the inlined string-compare immediates `0x675f737365636361` (`"access_g"`) / `0x7370756f72675f73` (`"s_groups"`). The intrinsic regenerates its own LLVM `AccessGroup` metadata at LLVM lowering, so threading the source attribute would double-tag it.

Operand mapping is uniform too: an `ScDialect` `memref`+`index` operand becomes a single raw `!llvm.ptr`/offset `Value` via the SC-specialised `getStridedElementPtr` (or `ConvertToLLVMPattern::getStridedElementPtr`); scalar operands pass through. The "1:N" character of some classes is **op-identity selection**, not operand explosion: the SC type system encodes each HW variant as a *distinct intrinsic*, and the dispatch key picks which one.

> **GOTCHA —** "the rewrite is 1:1" and "the class lowers to N intrinsics" are both true and not contradictory. A single `matchAndRewrite` call emits exactly one intrinsic (plus any helper ops); the "N" is the *static* number of candidate intrinsics the dispatch can choose among. `DmaSimpleStartOp` has one rewrite body but a 12-entry table of candidate `tpu_dma_<src>_to_<dst>_sc_simple` intrinsics (out of 16 such `_sc_simple` intrinsics registered in the binary); the body picks one per call. A reimplementer building a 1:1 op-to-intrinsic map will miss the dispatch and emit the wrong DMA.

### Table 1 — the six representative rewrite bodies

One representative per functional class, byte-decoded from `matchAndRewrite`. `→ intrinsic` is the `tpu_*::create` the body tail-calls; `dispatch key` is what selects the leaf.

| Class | Rewriter (`matchAndRewrite` @VA) | → leaf intrinsic(s) | Dispatch key | Algebra |
|---|---|---|---|---|
| CBREG advance | `AdvanceCbOffsetOpLowering` `0x1353bf80` | `tpu_cbreg_add_offset::create` | none (1:1) | descriptor read-modify-write (below) |
| CBREG read | `ReadCbOffsetOpLowering` `0x1353c400` | `tpu_rdcbreg_offset::create` `0x14734820` | none (1:1) | read OFFSET sub-register from cbreg → `replaceOp` |
| sync add | `SyncAddOpLowering` `0x13591660` | `tpu_syncadd` / `_tile` / `_remote` | `SflagCoreType`/`SflagLocal` + sequencer attr | local (V,V) / tile (V,V) / remote (V×5) |
| sync wait | `SyncWaitOpLowering` `0x13593040` | `tpu_wait{ge,eq,ne,lt,le,gt}` / `waitdone` / `waitnotdone` | comparison-predicate attr | predicate → pick wait intrinsic → `replaceOp` |
| DMA simple | `DmaSimpleStartOpLowering` `0x135a9100` | `tpu_dma_<src>_to_<dst>_sc_simple` (12-lambda table) | `(srcMemSpaceID, dstMemSpaceID)` tuple | `vector<tuple<u32,u32,fn>>` dispatch (below) |
| stream gather/scatter | `LinearStreamStartOpLowering::rewriteSparseCoreStreamOpToLLVM` `0x13542000` | `tpu_stream_linear_<verb>_<src>_to_<dst>[_add]` | `(dtype, off-tile memspace, verb)` lambda table | 16-entry dispatch (below) |
| addrspacecast | `MemorySpaceCastOpLowering` `0x135a5c20` | none (elide) **or** generic `llvm.addrspacecast` | `convertType(src) == convertType(dst)` | elide-or-fail (below) |

### CBREG — the descriptor read-modify-write

`AdvanceCbOffsetOpLowering` (`0x1353bf80`) is richer than a flat 1:1 replace. The cbreg lives inside a `CircularBufferDescriptor` (a `StructBuilder` over the converted memref), so the body is a read-modify-write of that struct:

```c
function AdvanceCbOffset::matchAndRewrite(op, adaptor, rewriter):   // 0x1353bf80
    desc   = StructBuilder(adaptor.cbDescriptor)           // dereference operand 0
    cbreg  = desc.CbReg()                                  // CircularBufferDescriptor::CbReg
    delta  = adaptor.deltaOperand                          // operand 1
    newReg = tpu_cbreg_add_offset::create(rewriter, loc,   // 0x146d7e60 (in_place @0x146d7f60)
                 cbreg.getType(), cbreg, delta)            //   offset += delta mod size in HW
    out    = StructBuilder(adaptor.cbDescriptor)
    out.SetCbReg(newReg)                                   // write new register field back
    out.SetMemRef(desc.MemRef())                           // carry the memref field through
    rewriter.replaceOp(op, out.value())
    return success
```

The HW semantics (offset wraps modulo the buffer size, the `{base, offset, size}` register bit layout) belong to the [CBREG](../sparsecore/cbreg.md) page; this body's contribution is the descriptor plumbing — the new offset is written into the *same descriptor struct* the memref already lives in, so the rest of the function keeps using one SSA value for the circular buffer.

### Sync / wait — attribute-driven dispatch

`SyncWaitOpLowering` (`0x13593040`) reads the op's comparison-predicate attribute and selects one of eight wait intrinsics: `tpu_waitge` (`0x14a30aa0`), `waiteq` (`0x14a30a00`), `waitne` (`0x14a30dc0`), `waitlt` (`0x14a30d20`), `waitle` (`0x14a30c80`), `waitgt` (`0x14a30be0`) — all 2-operand `{sflag, threshold}` compare forms — plus `waitdone` (`0x14a30960`) and `waitnotdone` (`0x14a30e60`), the 1-operand `{sflag}` done forms. `SyncAddOpLowering` (`0x13591660`) reads `getSflagCoreType`/`getSflagLocal` and the parent sequencer type to pick `tpu_syncadd` (local, V×2), `tpu_syncadd_tile` (tile bank, V×2), or `tpu_syncadd_remote` (ICI peer, V×5 — building a `ChipIdOp` + `arith.IndexCast` + `LLVM::ConstantOp` for the `{device, core, id}` routing). Both first resolve the sflag memref to a pointer via `getStridedElementPtr` and run `ValidateSyncFlagsIndices`.

### DMA — the memspace-pair tuple table

`DmaSimpleStartOpLowering` (`0x135a9100`) builds a `std::vector<std::tuple<u32, u32, std::function<void()>>>` of `(srcMemSpaceID, dstMemSpaceID, create-lambda)` rows, reads `getSrcBufferMemorySpace`/`getDstBufferMemorySpace` off the op, and runs the lambda whose `(src, dst)` pair matches. Each lambda creates the specific `tpu_dma_<src>_to_<dst>_sc_simple` intrinsic. The decompile of this body carries **12 distinct lambda closures** (lambdas `#1`…`#12`), each materialized as a `__call_func`/`__large_clone`/`__large_destroy` triple — **36 `std::function` thunk symbols** in the range `0x135ac0c0`…`0x135acb20` — confirming the table is built inline rather than as a static dispatch array. Before the dispatch, `CastTileSmemPointerToSmem` normalises any tile-resident endpoint to generic SMEM and the result feeds the `CheckAddressSpaces` legality gate (the "simple DMA on SMEM ⇒ SCS only" contract) — both owned by [SCTypeConverter](sc-type-converter.md) and the [LowerToMlo DMA bridge](lower-to-mlo-dma-bridge.md).

> **NOTE —** the create arity encodes the descriptor tier: `tpu_dma_*_sc_simple` takes 8 `Value`s, `_single_strided` takes 11, `_general` takes 16 (`tpu_dma_hbm_to_hbm_sc_simple` `0x146d8c60`; `tpu_dma_hbm_to_hbm_sc_single_strided` `0x146d9080`; `tpu_dma_hbm_to_hbm_sc_general` `0x146d8820`). The per-field role of those operands (base vs offset vs size vs stride vs sflag) is **not** decoded here — only the count and the memspace-pair dispatch are confirmed. The field semantics are charged to the descriptor-encoder analysis, not this lowering.

### Stream — the (dtype, memspace, verb) lambda table

`LinearStreamStartOpLowering::rewriteSparseCoreStreamOpToLLVM` (`0x13542000`) builds a 16-entry `vector<tuple<dtype, off-tile-memspace, verb, lambda>>` keyed by `(dtype, off-tile memspace, verb=gather/scatter)` and creates `tpu_stream_linear_<verb>_<src>_to_<dst>` (gather forms `0x148eb0a0`; `_hbm4b_` variants `0x148eae40`; scatter `0x149368c0`; indirect-gather-add `0x147922c0`, V×8). It runs `adjustOffsetForHbm4b` (HBM_4B granularity), `CheckLinearStreamIsValid`, and `CheckStartAndEndWithinMemref` (bounds) before the dispatch. The per-leaf HW stream-engine opcode for each of the ~834 stream intrinsics is the SparseCoreStream slot encoding — see [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md); this body only selects *which* intrinsic.

### addrspacecast — elide-or-fail

`MemorySpaceCastOpLowering` (`0x135a5c20`) is the smallest and most consequential body. It converts the source operand's type and the result type and compares:

```c
function MemorySpaceCast::matchAndRewrite(op, adaptor, rewriter):   // 0x135a5c20
    srcT = TypeConverter::convertType(op.getSource().getType())     // -> !llvm.ptr<N_src>
    resT = TypeConverter::convertType(op.getType())                 // -> !llvm.ptr<N_dst>
    if srcT != resT:
        return failure                                              // -> generic emits llvm.addrspacecast
    rewriter.replaceOp(op, adaptor.source)                          // ELIDE: same ptr type, no cast
    return success
```

If the two converted types are **equal** the cast is elided (`replaceOp(op, sourceValue)` — no instruction emitted); if they **differ** the pattern returns failure, and MLIR's generic `ConvertOpToLLVMPattern` for `memref.memory_space_cast` emits a real `llvm.addrspacecast`. The decision is therefore *entirely* the `SCTypeConverter`'s — the sequencer-context flatten (which collapses `{sflag_tile, sflag_scs, sflag_tc, sflag} → 204` and `{smem_tile, smem_scs, smem} → 0` outside sequencer functions) is what makes two distinct `MemorySpace`s convert to the *same* `!llvm.ptr<N>` and thus elide. That flatten table lives on [SCTypeConverter](sc-type-converter.md); the downstream `llvm.addrspacecast` instruction selection is on [addrspacecast ISel](../sparsecore/addrspacecast-isel.md).

> **QUIRK —** this pattern emits **zero or one** op, never the intrinsic family the other classes reach. It is the only `*OpLowering` whose "intrinsic" is sometimes *nothing*. A reimplementer must register the SC-specific elide pattern at higher benefit than the upstream generic `memref.memory_space_cast` → `llvm.addrspacecast` pattern, so the elide gets first refusal; only on its failure does the generic cast fire. Getting the benefit ordering wrong emits a redundant same-address-space cast that LLVM then has to fold away.

### The ~118-pattern roster (by class)

`lowerFunc` registers roughly 118 distinct `(anonymous namespace)::*OpLowering` classes (templated families — `UnaryFloatVectorOpLowering`, `AluEpOpLowering` — instantiate more than their class-name count). Rather than table all 118, the dispatch-dimension view:

| Group | Count | Representative members | Dispatch axis |
|---|---|---|---|
| STREAM | ~10 | `LinearStream[Add]Start`, `StridedStream[Add]Start`, `IndirectStream[Add]Start`, `IndirectVectorStream[Add]Start` | (dtype, off-tile memspace, verb) |
| DMA | 4 | `DmaSimpleStart`, `DmaSingleStridedStart`, `DmaGeneralStart`, `DmaWait` | (srcMemSpace, dstMemSpace) tuple |
| SYNC/WAIT | ~10 | `SyncAdd`, `SyncSet`, `SyncSetRemote`, `SyncWait`, `MemoryWait`, `Sfence`, `FetchAndAdd`, `TileWaitScsSmem` | predicate / local-tile-remote attr |
| CBREG | ~5 | `CreateCb`, `ReshapeCb`, `AdvanceCbOffset[InPlace]`, `ReadCbOffset` | none (descriptor RMW) |
| TRANSCENDENTAL/EUP | ~42 | `UnaryFloatVector` (12), `AluEp` (30) — see SCTypeConverter | packed-operand → 1:1 macro vs 1:N unpack/pack |
| PACK/UNPACK | ~8 | `PackF/SI/UI`, `UnpackF/SI/UI`, `UnpackI32Pair`, `CarryOut` | element format |
| SCAN/SORT | ~4 | `Scan`, `SegmentedScan`, `Sort`, `DuplicateCountUnique` | — |
| VECTOR MEM | ~10 | `VectorLoad[Idx]`, `VectorStore[Idx]`, `VolatileLoad/Store`, `ZeroMem`, `VectorBroadcast/Extract` | memspace |
| LANE/PERMUTE | ~6 | `Permute`, `Vlaneseq`, `VShiftInsert`, `VectorMaskCount{TrailingZeros,Population}` | — |
| ADDRESS/PTR | ~12 | `AddressOf`, `AllocateAtOffset`, `FoldOffsetIntoPtr`, `GetRemoteMemRef`, `MemRefView`, `MemorySpaceCast`, `SCSubView`, `SCView`, `HbmStackStartOffset` | type-converter equality (cast) |
| CONTROL/TASK | ~14 | `Barrier`, `Lock`, `Unlock`, `LaunchTileTask`, `Assert`, `GetCoreLocation`, `GetDynamic{DeviceAssignment,DimensionSize}`, `SetPTState`, `SetTag`, `SetTraceMark` | — |
| TRACE/TELEMETRY | ~8 | `Trace`, `LogEvent`, `LogMemRef`, `Read{Global,Local}CycleCount`, `SetDmaCredit`, `SetIndirectFilterValue` | — |

The EUP group (`UnaryFloatVectorOpLowering` 1:1 macro vs `AluEpOpLowering` 1:N unpack/compute/pack, selected by `IsDynamicallyLegal`) is fully tabled on [SCTypeConverter](sc-type-converter.md) (Tables B/C) — it is not re-tabled here.

---

## The Assert / MemRef-Finalize Lowering (Substage 3)

### Purpose

After substage 2 has turned every `func.func` body into `llvm.func` with `llvm`/`llvm_tpu` ops, two residues remain: `cf.assert` ops (which substage 1's `scf`→`cf` may have produced, or which were already present) and any unfinalized `memref` ops. Substage 3 walks each `LLVM::LLVMFuncOp`, and if the walk signals work is needed, runs one final `applyFullConversion` that lowers `cf.assert` and finalizes memref-to-LLVM.

### The conversion campaign

```c
function lowerAsserts_finalize(module):                  // tail of runOnOperation
    opts = LowerToLLVMOptions(ctx)
    opts.useBarePtrCallConv = true
    opts.dataLayout = DataLayout("…-S64")                // "-S64": 64-bit natural stack alignment
    tc = LLVMTypeConverter(ctx, opts)
    tc.registerTypeAttributeConversion(lowerAsserts_memSpaceLambda)   // SC mem-space attr (no flatten)
    patterns.add<AssertOpLowering>(benefit=1)            // 0x135b7080  "cf.assert"
    populateFinalizeMemRefToLLVMConversionPatterns(tc)
    target = LLVMConversionTarget(ctx)
    target.addLegalDialect("llvm_tpu")                   // SC intrinsic dialect is the legal target
    target.addIllegalOp("builtin.module")
    target.addDynamicallyLegalOp("llvm.alloca")          // alloca legal only post-finalize
    target.addDynamicallyLegalOp("builtin.unrealized_conversion_cast")  // bridge-cast passthrough
    applyFullConversion(module, target, patterns)
```

`AssertOpLowering` (`0x135b7080`) is a `ConvertToLLVMPattern` over `cf.assert` that carries the target's `GranuleBytes` — it computes a granule mask `-1 << (log2(stackBytes) − log2(GranuleBytes))` (the `_BitScanReverse64` pair on the target's `+88` field versus `GranuleBytes`), which aligns the assert's spill/scratch use to the SparseCore granule. The `lowerAsserts` memory-space lambda is the **same** `MemorySpaceAttr → i64 addrspace` conversion as substage 2's `SCTypeConverter` but **without** the sequencer-context flatten override — assertion text wants to name the exact per-tile/per-SCS bank, so it uses the un-flattened IDs. Both lambdas are documented on [SCTypeConverter](sc-type-converter.md).

> **NOTE —** the `llvm_tpu` dialect is marked a **legal** dialect in this final target, not illegal. By substage 3, the SC intrinsics emitted in substage 2 are the *desired* output, so the assert/memref finalize must treat them as legal-as-is and only rewrite the remaining `cf.assert` / `memref` / `builtin.module` structure. The `builtin.unrealized_conversion_cast` is held dynamically legal so any bridge-cast still in transit from the [LowerToMlo DMA bridge](lower-to-mlo-dma-bridge.md) passes through untouched.

---

## Related Components

| Name | Relationship |
|---|---|
| `SCTypeConverter` (`lowerFunc` `0x13568280`) | builds the type map and EUP roster this page's rewrite bodies ride on |
| `FilterLLVMAttributes` (`0x135b7a20`) | the uniform attribute filter every rewrite body calls (drops `access_groups`) |
| `getStridedElementPtr` | memref+index → raw `!llvm.ptr` used by DMA/sync/stream operand resolution |
| `CheckAddressSpaces` (`0x135b8e00`) | the DMA legality gate `DmaSimpleStartOpLowering` consults (owned by SCTypeConverter) |
| `tpu_*::create` family | the 1356-intrinsic leaf set the rewrite bodies tail-call (LlvmTpu catalog) |
| `populateSCFToControlFlowConversionPatterns` | upstream scf→cf patterns the custom `ForLowering`/`IndexSwitchLowering` augment |

## Cross-References

- [SCTypeConverter](sc-type-converter.md) — the address-space → `!llvm.ptr` type map, the sequencer-context flatten, `CheckAddressSpaces`, and the 42-instantiation EUP roster the rewrite bodies consume (do not duplicate).
- [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md) — the complete AS-id ↔ `MemorySpace` ↔ pool table the dispatch keys index.
- [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) — the `llvm.addrspacecast` instruction selection that `MemorySpaceCastOpLowering` falls through to on a type mismatch.
- [LowerToMlo DMA Bridge-Cast](lower-to-mlo-dma-bridge.md) — the prior two-stage DMA lowering whose tile-expanded output this pass finalizes; source of any in-transit bridge-cast.
- [DialectConversion Legalizer](dialect-conversion-legalizer.md) — the `ConversionTarget`/`applyFullConversion` machinery the three substages drive.
- [ConversionPatternRewriter](conversion-pattern-rewriter.md) — the speculative-apply/rollback rewriter the `matchAndRewrite` bodies call `replaceOp` on.
- [LlvmTpu Intrinsic Catalog](llvmtpu-intrinsic-catalog.md) — the 1356 `tpu_*` intrinsics that are the leaf targets of every rewrite body.
- [CBREG](../sparsecore/cbreg.md) — the `{base, offset, size}` circular-buffer register the CBREG rewrite bodies read and write.
- [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md) — the SparseCoreStream slot encoding behind the ~834 stream intrinsics the stream dispatch selects.
- [SCS Engine](../sparsecore/scs-engine.md) — the Scalar Core Sequencer the sync/DMA legality contracts reference.
- [The `tpu` MLIR Dialect](tpu-dialect-and-ops.md) — the op-registration ABI for the `tpu`/`sparse_core` ops these patterns rewrite.
- [Compiler Overview](overview.md) — Part V orientation; where SC lowering sits in the five-phase descent.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
