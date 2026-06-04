# The tpu MLIR Dialect: Ops and the Op-Model Contract

> *All addresses, symbol names, and counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). The `.symtab` is **not stripped** — every claim below is anchored to a demangled symbol or a relocation addend. Other versions will differ; the 23-slot ordering in particular is pinned to this binary's LLVM SHA.*

## Abstract

The `tpu` dialect is the TPU's *machine dialect* — the MLIR target dialect that the optimizer path (HLO → MHLO → `tpu`) and the Mosaic side channel (`tpu_custom_call` → `tpu`) both converge on before the descent into LLO (see [Compiler Overview](overview.md)). This page documents the dialect from the angle that survives in a stripped-but-symbol-bearing binary: not the source-level ODS `.td` files (which are not in the wheel), but the **runtime op-registration ABI** that every op in every MLIR dialect is compiled into.

Concretely, MLIR registers an op not by C++ inheritance but by instantiating a type-erased concept class — `mlir::RegisteredOperationName::Model<Op>` — once per op, and inserting it as the op's `OperationName::Impl`. Each `Model<Op>` is a vtable with a fixed **23-slot contract**: a shared base destructor, a per-op deleting destructor, and 21 dispatch slots (fold, canonicalize, verify, parse/print, the four inherent-attribute accessors, and nine property-management methods). The binary holds **6,050** such `Model<Op>` vtables — one per registered op across 60-plus dialect namespaces — of which the `tpu` dialect owns **86**.

The binary-wide `Model<Op>` ABI is owned by the sibling [MLIR Op-Model Contract](mlir-op-model-contract.md) page; this page is the worked `tpu`-dialect instantiation of it and reproduces the 23-slot walk only as far as needed to read the 86 `tpu` ops against it. This page owns three things:

- **The `Model<Op>` 23-slot contract** — every slot labelled to its demangled `Model<Op>::<method>` symbol, the 1-shared + 22-per-op layout, and what each slot does. (Verified on `Model<mlir::tpu::IotaOp>`: all 21 dispatch-slot symbols present.)
- **The Op↔Model binding** — how a concrete `tpu` op fills its 23 slots: the `Dialect::addOperations<…>` registrar, the single `RegisteredOperationName::insert`, and the three-level delegation from the type-erased Model thunk to the op's ODS-generated statics.
- **The `tpu` op-family taxonomy** — the 86 ops grouped by their `OpInterface` signature, the fold/canonicalize census (the dialect's entire in-dialect rewrite surface is 15 methods), and the 9 ops that carry a variadic `AttrSizedOperandSegments` size array (a subset of the 53 ops whose `Properties` struct is non-trivial).

| | |
|---|---|
| **Op-registration concept** | `mlir::RegisteredOperationName::Model<Op>` (one per registered op) |
| **Model vtable count (all dialects)** | **6,050** across 60-plus namespaces (= total registered-op count) |
| **`tpu` dialect Model count** | **86** (exact `addOperations<>` arity; corrects the ~157 a naive `*Op`-string scan over-counts — see below) |
| **Vtable slots per Model** | **23** = slot 0 shared base dtor + slot 1 per-op dtor + slots 2–22 dispatch |
| **Shared slot-0 dtor** | `mlir::OperationName::Impl::~Impl` (addend `0xfea8820`, identical across all 6,050 Models) |
| **`tpu` op registrar** | `Dialect::addOperations<mlir::tpu::AllReduceOp, …>` @ `0x14aa2c40` (86 args) |
| **Registration sink** | `RegisteredOperationName::insert(unique_ptr<OperationName::Impl>, ArrayRef<StringRef>)` @ `0x1d8c57a0` |
| **Dialect ctor** | `mlir::tpu::TPUDialect::TPUDialect(MLIRContext*)` @ `0x14a96d40` (registers `"tpu"`, dialect-id 3, then calls `addAttributes<13>`/`addType<3>`/`addOperations<86>` inline — no separate `initialize()`) |
| **`tpu` MemoryEffect interface fan-out** | 59 ops (cross-checked two ways) |
| **`tpu` in-dialect rewrite surface** | 6 real `fold()` + 9 real `getCanonicalizationPatterns()` = 15 methods |
| **`tpu` ops with non-trivial `Properties`** | 53 (slot 14 `getOpPropertyByteSize` > 0); 9 of those carry an `AttrSizedOperandSegments` size array |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Model&lt;Op&gt; 23-Slot Contract

MLIR's `OperationName` is a handle into an interned, per-op `OperationName::Impl`. For a *registered* op (one the dialect declared, as opposed to an opaque/unregistered op parsed from text), that `Impl` is a `RegisteredOperationName::Model<Op>` — a C++ template instantiated once per op type. The `Model<Op>` is a classic type-erasure concept: it derives from the abstract `OperationName::Impl` interface and overrides every virtual with a thunk that knows the concrete `Op`'s ODS-generated statics. There is exactly one `Model<Op>` object — and therefore one vtable — per registered op, and `nm` counts **6,050** of them (`_ZTVN4mlir23RegisteredOperationName5ModelI…EEE`).

The vtable follows the Itanium PIE layout: `vtable+0x00` is offset-to-top (0), `vtable+0x08` points at the `_ZTI…Model<Op>…` typeinfo, and `vtable+0x10` is the *address point* — the value an object's vptr stores, and the location of slot 0. Slot *i* sits at `vtable+0x10+8i`. In the on-disk image the slot words are zero; they are filled at load by `R_X86_64_RELATIVE` relocations whose addends are the method addresses. Resolving each addend against the (sorted) symbol table recovers the method name for every slot.

The reference walk below is `Model<xla::PureCallOp>` @ `0x219d4e38`, reproduced byte-identically in ordering and signature on `Model<mlir::tpu::IotaOp>` @ `0x219abb28`, `Model<mlir::llo::ConstantOp>` @ `0x2193dba0`, and `Model<mlir::mosaic_sc::RelayoutOp>` @ `0x21905130` — three dialects, one ABI.

```text
RegisteredOperationName::Model<Op>  vtable  (address point = +0x10 = slot 0)

 slot  off   method (Model<Op>::…)                       nature
 ----  ----  ------------------------------------------  --------------------------------
  0    0x00  OperationName::Impl::~Impl                  SHARED base dtor (0xfea8820 ∀ ops)
  1    0x08  ~Model  [deleting]                          per-op dtor
  2    0x10  foldHook(Operation*, ArrayRef<Attribute>,   per-op; wraps Op<>::getFoldHookFn
                       SmallVectorImpl<OpFoldResult>&)            lambda (UniqueFunction)
  3    0x18  getCanonicalizationPatterns(                per-op; no-op unless op defines it
                       RewritePatternSet&, MLIRContext*)
  4    0x20  hasTrait(TypeID)                            trait / interface membership test
  5    0x28  getParseAssemblyFn()                        returns the op's custom parser fn
  6    0x30  populateDefaultAttrs(OperationName const&,  default-attr init
                       NamedAttrList&)
  7    0x38  printAssembly(Operation*, OpAsmPrinter&,     per-op; wraps getPrintAssemblyFn
                       StringRef)
  8    0x40  verifyInvariants(Operation*)                wraps Op<>::verifyInvariants
  9    0x48  verifyRegionInvariants(Operation*)          region-structure verify
 10    0x50  getInherentAttr(Operation*, StringRef)      → Op::getInherentAttr  (TAIL-JMP)
 11    0x58  setInherentAttr(Operation*, StringAttr,     → Op::setInherentAttr
                       Attribute)
 12    0x60  populateInherentAttrs(Operation*,           → Op::populateInherentAttrs
                       NamedAttrList&)
 13    0x68  verifyInherentAttrs(OperationName,          inherent-attr verify
                       NamedAttrList&, function_ref<…>)
 14    0x70  getOpPropertyByteSize()                     INLINED sizeof(Op::Properties)
 15    0x78  initProperties(OperationName, PropertyRef,  placement-construct props
                       PropertyRef)
 16    0x80  deleteProperties(PropertyRef)               destruct props
 17    0x88  populateDefaultProperties(OperationName,    default props
                       PropertyRef)
 18    0x90  setPropertiesFromAttr(OperationName,        attr → props
                       PropertyRef, Attribute, fn_ref<…>)
 19    0x98  getPropertiesAsAttr(Operation*)             props → attr (print / bytecode)
 20    0xa0  copyProperties(PropertyRef, PropertyRef)    clone props
 21    0xa8  compareProperties(PropertyRef,PropertyRef)  props equality (for CSE)
 22    0xb0  hashProperties(PropertyRef)                 props hash (for CSE / dedup)
```

The 23 slots cluster into four functional groups:

- **Slots 2–9 — the dispatch surface.** Folding, canonicalization, the trait/interface gate (`hasTrait`), custom parse/print, default-attribute population, and the two verifiers. Slots 2, 3, 7, and 8 are the *optional* hooks — they are present for every op but wrap a no-op unless the op actually defines the method (see the binding section).
- **Slots 10–13 — inherent-attribute accessors.** Inherent attributes are the op's *declared* attributes (e.g. `iota`'s `dimensions`), as opposed to discardable attributes. These four slots tail-jump straight to the op's ODS statics.
- **Slots 14–22 — property management.** MLIR's `Properties` is the inline storage for an op's inherent attributes and variadic operand-segment sizes. These nine slots construct, destruct, copy, compare, hash, and (de)serialize that storage. For ops without a `Properties` struct they are trivial (see slot 14 below).

> **NOTE — the "23-slot Model" is structurally 1-shared + 22-per-op, not 23 per-op.** Slot 0 — the address point that every object's vptr stores — is the *same* relocation addend `0xfea8820` (`mlir::OperationName::Impl::~Impl`) across all 6,050 Models, verified on a 30-Model stratified sample spanning `tpu`, `llo`, `sparse_core`, `mosaic_sc`, and `xla`. It is the destructor of the common `OperationName::Impl` base subobject. Only slots 1–22 are per-op template instantiations: slot 1 is `Model<Op>::~Model` (the deleting dtor) and slots 2–22 are the dispatch thunks. A reimplementer should model `Model<Op>` as *one shared base destructor plus 22 op-specific entries*.

> **QUIRK — `getOpPropertyByteSize` (slot 14) is an inlined `sizeof(Op::Properties)` constant, not a function call.** The compiler folds the slot to a literal. `Model<mlir::tpu::IotaOp>::getOpPropertyByteSize` is `mov $0x8; ret` — 8 bytes, the single `dimensions` `DenseArrayAttr` handle. `Model<mlir::tpu::BarrierOp>::getOpPropertyByteSize` (@ `0x14ab1960`) is `xor %eax,%eax; ret` — 0 bytes, because `BarrierOp` carries no `Properties`. This is the cheapest possible diagnostic for "does this op have inline properties": read slot 14's two-instruction body. The nine property-management slots (14–22) only do real work for ops carrying inherent attributes or `AttrSizedOperandSegments`.

---

## The Op↔Model Binding

A concrete op is bound to its 23-slot Model not by overriding virtuals in a hand-written subclass, but by **template instantiation + delegation**, in three levels.

### Level 1 — registration: `addOperations` → `insert`

Each dialect registers its ops with a single variadic `Dialect::addOperations<Op…>` template instantiation. For `tpu`, this is called directly from the dialect constructor body (`TPUDialect::TPUDialect` @ `0x14a96d40` — there is no separate `initialize()` symbol; the constructor runs `Dialect::Dialect(this, "tpu", 3, …)`, then `addAttributes<…>`, `addType<…>` ×3, then `addOperations<…>`):

```text
mlir::Dialect::addOperations<mlir::tpu::AllReduceOp, mlir::tpu::AllocaSemaphoreOp,
                             mlir::tpu::AssumeLayoutOp, …>   @ 0x14aa2c40
```

with exactly **86** type arguments — confirming the `tpu` dialect's op count from the registrar arity, not from a string scan. For each `Op` in the pack, `addOperations` materialises a `Model<Op>` (an instance whose vtable holds the 23 `Model<Op>::*` thunks) and hands it to the single, non-template registration sink:

```text
mlir::RegisteredOperationName::insert(
    std::unique_ptr<mlir::OperationName::Impl, …>,   ← the Model IS the Impl
    llvm::ArrayRef<llvm::StringRef>)                  @ 0x1d8c57a0
```

The `Model<Op>` *is* the `OperationName::Impl` — registration is "intern this Impl under the op's name(s)." The `TPUDialect` constructor (`mlir::tpu::TPUDialect::TPUDialect(MLIRContext*)` @ `0x14a96d40`) wires up attributes (`addAttributes<13>`) and types (`addType<…>` ×3) and the ops (`addOperations<86>`) all in its own body — `initialize()` is fully inlined into the constructor, so there is no standalone `TPUDialect::initialize` symbol. Binary-wide there are 243 distinct `addOperations<…>` symbols — 205 unique after demangling (38 collapse to identical demangled signatures) — one per registered dialect (some split into batches); the TPU-path registrars are `tpu` `0x14aa2c40` (86), `llo` `0x13e5f940` (325), `sparse_core` ScDialect `0x14594f60` (115), and `mosaic_sc` `0x132f9ec0` (1, `RelayoutOp`).

### Level 2 — per-slot delegation to ODS statics

Each `Model<Op>::slotN` thunk does two things: it locates the op's `Properties` storage (inline vs. out-of-line, chosen from the op flag word), then tail-jumps to the ODS-generated static. The inline-property-offset idiom is identical across ops:

```asm
; Model<tpu::IotaOp>::getInherentAttr  (slot 10 @ 0x14ac18a0)
mov    0x2c(%rdi),%ecx        ; load the Operation flag word
shr    $0x13,%ecx             ; isolate the inline-properties bit
and    $0x10,%ecx             ; → 0x10 if inline, 0 if out-of-line
lea    0x40(%rdi,%rcx,1),%rsi ; %rsi = &Properties
jmp    0x14b220e0             ; TAIL-JMP to the ODS static, below
```

The jump target is the concrete op's ODS static, recovered as a demangled symbol:

```text
mlir::tpu::IotaOp::getInherentAttr(
    mlir::MLIRContext*,
    mlir::tpu::detail::IotaOpGenericAdaptorBase::Properties const&,
    llvm::StringRef)                                  @ 0x14b220e0
```

The four inherent-attribute slots (10–13) all use this direct tail-jmp shape.

### Level 3 — optional hooks via `UniqueFunction` callback-holders

The four *optional* hooks — `foldHook` (2), `getCanonicalizationPatterns` (3), `printAssembly` (7), `verifyInvariants` (8) — are wrapped one indirection deeper, in an `llvm::UniqueFunctionBase<…>::CallbacksHolder<Op<Op,Traits…>::get<Hook>Fn()::lambda>`. The slot loads the holder pointer and does `call *(%rax)` (or `call *0x10(%rax)` for the print/parse holders). For `IotaOp`:

```text
Model<tpu::IotaOp>::foldHook         (slot 2 @ 0x14ac1580) → holder @ 0x223413f0
Model<tpu::IotaOp>::printAssembly    (slot 7 @ 0x14ac16e0) → holder @ 0x22341400
Model<tpu::IotaOp>::verifyInvariants (slot 8 @ 0x14ac1760) → Op<…>::verifyInvariants @ 0x14ac1de0
```

This third level is where the **full trait and interface list of the op is encoded**: the holder symbol carries the entire `Op<OpName, Trait1, Trait2, …, Interface::Trait, …>` template signature. Reading those template arguments off the `getFoldHookFn` holder symbol is how the per-op interface fan-out (next section) is recovered — and it is cross-validated against an independent count (the second-layer interface Models).

> **GOTCHA — two Model layers, not one.** The 23-slot `RegisteredOperationName::Model<Op>` is the **op-registration** concept. `OpInterface` *dispatch* (e.g. "what side effects does this op have?") goes through a **separate** per-(op, interface) concept table: `mlir::detail::<Iface>InterfaceTraits::Model<Op>` — for example `MemoryEffectOpInterfaceTraits::Model<mlir::tpu::IotaOp>::getEffects`. The 23-slot Model's slot-4 `hasTrait` is only the *gate* that answers "does this op implement interface X?"; the interface *body* lives in the second-layer Model. A reimplementer building op dispatch must build both: the registration vtable here, and the per-interface concept tables (the latter are catalogued for the TPU path but their slot layouts are out of scope for this page — they are the subject of the second-layer Model work).

### Caller-side join

The dispatch is consumed at, e.g., `mlir::Operation::fold` @ `0x1d8cd480`, which loads the Impl/Model, then the vptr, then calls slot 2:

```asm
mov  0x30(%rdi),%rdi    ; %rdi = OperationName::Impl  (= the Model)
mov  (%rdi),%rax        ; %rax = vptr (address point)
call *0x10(%rax)        ; +0x10 from address point = slot 2 = foldHook   (@ 0x1d8cd4ad)
```

---

## The tpu Op-Family Taxonomy

The 86 `tpu` ops (Model vtables in the range `0x219aa468 … 0x219aed**`) group naturally by their `OpInterface` signature — the set of interfaces each op implements, read from the `Op<OpName, Traits…>` template arguments in the op's `getFoldHookFn` holder. The interface abbreviations are: **MemEff** = `MemoryEffectOpInterface`, **CondSpec** = `ConditionallySpeculatable`, **Bytecode** = `BytecodeOpInterface`, **InferType** = `InferTypeOpInterface`, **OpAsm** = `OpAsmOpInterface`, **RBTerm** = `RegionBranchTerminatorOpInterface`.

```text
#ops  OpInterface signature                          representative ops
----  ---------------------------------------------  ----------------------------------------
 21   Bytecode, CondSpec, MemEff                     AllReduce, BroadcastInSublanes, Gather,
                                                     Iota, Matmul, MemRefSlice, Pack*, FPToSI,
                                                     SIToFP, Transpose, TruncF, Unpack*, …
 16   CondSpec, MemEff                               AssumeLayout, Bitcast, BitcastVreg,
                                                     CreateMask, EraseLayout, ExtF, MaskCast,
                                                     MemRefBitcast/Reshape/Squeeze, Reshape,
                                                     RollVectors, UnrollVectors, Weird, …
 13   Bytecode (only)                                EnqueueDMA, EnqueueIndirectDMA, Log,
                                                     LogBuffer, MatmulAccLhs/Pop/PushRhs,
                                                     Scan, SemaphoreSignal, Trace*, WaitDMA2
 11   (none — pure structural / side-effecting)      AllocaSemaphore, Barrier, Delay,
                                                     GetBarrierSemaphore, GetInternalScratch,
                                                     PRNGRandomBits, PRNGSeed32, Region,
                                                     SemaphoreWait, TraceStop, WaitIndirectDMA
 10   Bytecode, MemEff                               Load, ShuffledLoad/Store, Store,
                                                     StridedLoad/Store, VectorLoad/Store(Idx)
  6   Bytecode, CondSpec, InferType, MemEff          AssumeMultiple, Concatenate, DynamicGather,
                                                     DynamicRotate, Reciprocal, Rotate
  2   CondSpec, InferType, MemEff                    DeviceId, Relayout
  2   Bytecode, InferType                            GetIterationBound, SublaneShuffle
  1   InferType, MemEff                              FetchAndAddSync
  1   CondSpec, InferType, MemEff, OpAsm             ScanCount
  1   InferType (only)                               SemaphoreRead
  1   Bytecode, CondSpec, MemEff, OpAsm              Sort
  1   CondSpec, MemEff, RBTerm                       Yield  (the tpu.region terminator)
```

Aggregating across the 86 ops, the interface fan-out is: **MemoryEffect 59**, **Bytecode 53**, **ConditionallySpeculatable 48**, **InferType 13**, **OpAsm 2** (`ScanCount`, `Sort`), **RegionBranchTerminator 1** (`Yield`). The MemoryEffect count is cross-validated: counting the *independent* second-layer Models `MemoryEffectOpInterfaceTraits::Model<mlir::tpu::*>` also yields **59**.

The op families read as the TPU's machine-instruction classes: **MXU matmul** (`Matmul`, `MatmulAccLhs/Pop/PushRhs`), **DMA** (`EnqueueDMA`, `EnqueueIndirectDMA`, `WaitDMA2`, `WaitIndirectDMA`), **semaphores** (`AllocaSemaphore`, `SemaphoreSignal/Wait/Read`, `GetBarrierSemaphore`, `FetchAndAddSync`), **vector relayout / pack-unpack** (`Relayout`, `RollVectors`, `UnrollVectors`, `Pack*`, `Unpack*`, `BitcastVreg`), **memref view ops** (`MemRefSlice/Bitcast/Reshape/Squeeze`, `EraseLayout`, `AssumeLayout`), **PRNG** (`PRNGSeed32`, `PRNGRandomBits`), **conversions** (`ExtF`, `TruncF`, `FPToSI`, `FPToUI`, `SIToFP`, `UIToFP`), and **trace/region structure** (`Trace*`, `Region`, `Yield`, `Barrier`, `Delay`).

### Properties-carrying ops

Reading slot 14 (`getOpPropertyByteSize`) across all 86 ops, **53 `tpu` ops carry a non-trivial `Properties` struct** (slot 14 returns a non-zero `sizeof`; the remaining 33 return 0 via `xor %eax,%eax;ret`). The `Properties` sizes range from 8 to 40 bytes — most are the inline storage for an op's inherent attributes (e.g. `IotaOp`'s 8-byte `dimensions` `DenseArrayAttr` handle, confirmed by the demangled `mlir::tpu::IotaOp::getDimensions`, `readProperties`, and `writeProperties` statics).

A distinguished **subset of 9 ops** additionally carries an `AttrSizedOperandSegments` variadic operand-segment size array (verified by the `AttrSizedOperandSegments` trait on the demangled `Op<…>` signature): **EnqueueDMA, MemRefSlice, SemaphoreSignal, Store, VectorLoad, VectorLoadIdx, VectorStore, VectorStoreIdx, WaitDMA2**. The largest `Properties` structs — `EnqueueDMA` and `Store` at 40 bytes, then `Matmul`/`Rotate`/`UnpackSubelements`/`VectorStore`/`WaitDMA2` at 32 bytes — combine several inherent attributes (and, for the segment-bearing ops, the size array) in one inline blob.

### The in-dialect rewrite surface is 15 methods

The fold and canonicalize slots (2 and 3) are present on all 86 ops, but each wraps a no-op unless the op *defines* the method. By demangled-symbol presence of `Op::fold` / `Op::getCanonicalizationPatterns`, the entire in-dialect rewrite surface of the `tpu` dialect is:

- **6 real `fold()`:** `BitcastVregOp`, `EraseLayoutOp`, `ExtFOp`, `MemRefSliceOp`, `ReshapeOp`, `TruncFOp`. (All six confirmed by symbol; `TruncFOp::fold` shows two symbols — the fold method plus an inner `operator()(APFloat const&, bool&)` lambda doing the per-element conversion.)
- **9 real `getCanonicalizationPatterns()`:** `FPToSIOp`, `MatmulOp`, `MemRefBitcastOp`, `MemRefSliceOp`, `MemRefSqueezeOp`, `ShuffledLoadOp`, `ShuffledStoreOp`, `UnpackSubelementsOp`, `UnrollVectorsOp`.

Everything else in the `tpu` dialect is transformed *structurally* by the dialect-conversion lowering ([tpu → LLO Lowering](tpu-to-llo-ods.md)), not rewritten in place. The fold/canonicalize *bodies* (the rewrite algebra inside, e.g., `MatmulOp::getCanonicalizationPatterns`) are not decompiled on this page — only the census (which ops rewrite) is established here.

> **NOTE — the "86" corrects a naive `*Op`-string scan that over-counts to ~157.** A surface RTTI scan for `mlir::tpu::*Op` symbols returns ~157 hits, but the exact figure from the `addOperations<>` registrar arity is **86 registered ops**, each with a 23-slot Model. The difference is `*Op`-suffixed symbols that are *not* registered ops — Pass classes and helpers (e.g. `LinalgVectorizationPass`, `MosaicSerdePass`, `PreCanonicalization`, `applyLayout`) that match a naive scan but never reach `addOperations`, so they have no Model. The [Compiler Overview](overview.md) deliberately asserts no `tpu` op count for exactly this reason. For dispatch-ABI purposes, 86 is the authoritative count.

---

## The tpu Dialect in the 6,050-Model Census

The `tpu` dialect's 86 ops sit inside a binary-wide census of 6,050 `Model<Op>` instances across 60-plus dialect namespaces (the exact dialect count depends on how the `xla` sub-namespaces — `xla`, `xla::cpu`, `xla::xtile` — are partitioned), where each dialect's Model count equals its registered-op count. The TPU-relevant dialects:

```text
dialect             Models   role on the TPU path
------------------  ------   --------------------------------------------------
sparse_core          1471    SparseCore: 115 ScDialect ops + 1356 LlvmTpu tpu_* intrinsics
llo                   325    LLO — TensorCore low-level machine ops (the layer below tpu)
LLVM                  294    LLVM dialect — SparseCore tail / off the TensorCore path
linalg                 99    Mosaic hlo-conversion stage
tpu                    86    TPU — TensorCore high-level (Mosaic) target dialect  ← THIS PAGE
arith / math / memref  52 / 46 / 32  scalar/math/memref helpers used by tpu, llo, sc
xtile                   6    XLA CPU/GPU tile IR (off the TPU device path)
mosaic_sc               1    SparseCore Mosaic — RelayoutOp only
```

The other 50-odd namespaces (TF 834, spirv 376, ROCDL 350, NVVM 197, the HLO input languages mhlo/vhlo/stablehlo/chlo, etc.) are either input languages legalized away early or off-path build fallout. Two TPU-adjacent points worth flagging for a reimplementer:

- **`llo` (325 ops)** is the dialect directly *below* `tpu` — the same 23-slot Model ABI applies. Its bulk (164 ops) carries `CondSpec, InferType, MemEff` (the scalar/vector ALU), and only `llo::ConstantOp` defines a real `fold()` (the LLO constant folder); no `llo` op defines a canonicalizer, because LLO is the bottom MLIR layer — lowered, not rewritten.
- **`sparse_core` (1471 ops)** decomposes into 115 high-level `ScDialect` ops plus **1356 `LlvmTpuDialect` `tpu_*` LLVM-intrinsic ops** — the largest single op family in the binary's MLIR layer, each carrying the LLVM-op interfaces (`LLVM::AccessGroup`, `LLVM::AliasAnalysis`) because each lowers to an LLVM op. These are catalogued in [LlvmTpu Intrinsic Catalog](llvmtpu-intrinsic-catalog.md) and consumed by [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md). They share the identical 23-slot Model ABI documented here.

---

## Confidence Summary

| Claim | Evidence |
|---|---|
| `Model<Op>` is a 23-slot vtable; 6,050 instances binary-wide | `nm` count of `_ZTVN4mlir23RegisteredOperationName5ModelI…EEE` = 6,050; reference vtable `Model<xla::PureCallOp>` @ `0x219d4e38` |
| All 21 dispatch-slot symbols present on a `tpu` op | every `Model<mlir::tpu::IotaOp>::<method>` (slots 2–22) resolves to a demangled symbol |
| Slot 0 is one shared base dtor across all Models | addend `0xfea8820` = `OperationName::Impl::~Impl` on 30-Model stratified sample (tpu/llo/sc/mosaic_sc/xla) |
| `tpu` dialect has exactly 86 registered ops | `Dialect::addOperations<mlir::tpu::AllReduceOp, …>` @ `0x14aa2c40` has 86 type args |
| Registration sink is `RegisteredOperationName::insert` | demangled `insert(unique_ptr<OperationName::Impl,…>, ArrayRef<StringRef>)` @ `0x1d8c57a0` |
| Inherent-attr slots tail-jmp to ODS statics | `Model<IotaOp>::getInherentAttr` (slot 10) tail-jmp → `mlir::tpu::IotaOp::getInherentAttr(MLIRContext*, …IotaOpGenericAdaptorBase::Properties const&, StringRef)` @ `0x14b220e0` |
| Optional hooks wrapped in `UniqueFunction` callback-holders | `Model<IotaOp>::foldHook`/`printAssembly` load holder ptr + `call *(%rax)`/`*0x10(%rax)`; holders @ `0x223413f0`/`0x22341400` |
| `getOpPropertyByteSize` is an inlined `sizeof(Properties)` | `Model<IotaOp>` = `mov $0x8;ret`; `Model<BarrierOp>` @ `0x14ab1960` = `xor %eax,%eax;ret` |
| `tpu` MemoryEffect fan-out = 59 ops | `Op<>` trait scan = 59; independent `MemoryEffectOpInterfaceTraits::Model<mlir::tpu::*>` distinct count = 59 |
| `tpu` in-dialect rewrite surface = 6 fold + 9 canon | demangled `mlir::tpu::*Op::fold` (6) and `*Op::getCanonicalizationPatterns` (9) present |
| 53 `tpu` ops carry non-trivial `Properties`; 9 of those carry `AttrSizedOperandSegments` | slot 14 `getOpPropertyByteSize` non-zero on 53/86 ops (read directly: `mov $0xN;ret` vs `xor;ret`); the 9 segment-bearing ops carry the `AttrSizedOperandSegments` trait; `IotaOp` = 8-byte `dimensions` property |
| `tpu` op interface signatures (family grouping) | read from `Op<OpName,Traits…>` template args in each op's `getFoldHookFn` holder symbol |
| Naive `*Op`-scan ~157 vs 86 registered | 86 = registrar arity; the ~71 extra are unregistered Pass/helper `*Op` symbols with no Model |

---

## Cross-References

- [Compiler Overview](overview.md) — where the `tpu` dialect sits in the five-phase descent and the six-level IR stack; the convergence point of the optimizer and Mosaic paths.
- [MLIR Op-Model Contract](mlir-op-model-contract.md) — the binary-wide `Model<Op>` ABI and the full 6,050-Model census; that page owns the contract, this page is its worked `tpu`-dialect instantiation (relate, do not duplicate).
- [tpu → LLO Lowering](tpu-to-llo-ods.md) — how the 86 `tpu` ops are lowered structurally into the 325-op `llo` dialect, the layer below this one.
- [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md) — the mid-level lowering that produces the `tpu` ops this page registers.
- [DialectConversion Legalizer](dialect-conversion-legalizer.md) — the conversion-pattern machinery that consumes `tpu` ops during lowering.
- [tpu Program Serialization](tpu-program-serialization.md) — how the property storage (slots 18/19, `setPropertiesFromAttr`/`getPropertiesAsAttr`) round-trips through bytecode.
- [LlvmTpu Intrinsic Catalog](llvmtpu-intrinsic-catalog.md) — the 1356 `sparse_core::tpu_*` intrinsic ops that share this Model ABI.
- [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) — the pass that consumes the `tpu_*` intrinsic Models on the SparseCore path.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
