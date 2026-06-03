# MLIR Op-Model Contract

> *All addresses, symbols, and counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). The `.symtab` is **not stripped** (1,233,709 symbols); every claim is anchored to a demangled symbol, a relocation addend, or a decompiled body. Other versions differ — the 23-slot ordering in particular is pinned to this binary's bundled LLVM SHA.*

## Abstract

Every MLIR dialect in `libtpu` — `tpu`, `llo`, `sparse_core`, `mosaic_sc`, `xtile`, and the 57 input/off-path dialects — registers its operations through one shared, version-pinned ABI. This page documents that ABI generically: the `OperationName::Impl` interned-op record, the `RegisteredOperationName::Model<Op>` type-erasure concept that fills it, the **23-slot vtable contract** every `Model<Op>` exposes, and how an op-dispatch call (`fold`, `verify`, `getInherentAttr`) resolves through that vtable into a concrete op's ODS-generated statics. It is the contract; the per-dialect op rosters that *instantiate* it live on their own pages — the [tpu Dialect](tpu-dialect-and-ops.md) page owns the 86 `tpu` ops, their interface fan-out, and the fold/canonicalize census, and cites this page as the owner of the 23-slot contract.

The mechanism is classic C++ type erasure, recognisable to anyone who has read MLIR upstream. `OperationName` is a handle into an interned, per-op-kind `OperationName::Impl`. `Impl` is an abstract interface — a vtable of operation hooks (fold, verify, parse/print, attribute and property management) — with **two** concrete implementations in the binary: `RegisteredOperationName::Model<Op>`, a CRTP template instantiated once per *registered* op that forwards each hook to the op's compiler-generated statics, and `OperationName::UnregisteredOpModel`, the single fallback for ops parsed from text without a dialect declaration. The binary holds **6,050** distinct `Model<Op>` instantiations — exactly the total registered-op count across 61 dialect namespaces.

The page proceeds: the `OperationName::Impl` record and the `Concept`/`Model<Op>` CRTP pair; the 23-slot vtable walked symbol-by-symbol; how a concrete op binds to its Model (the `addOperations` → `insert` registration path and the three-level delegation from Model thunk to ODS static); how an interface call resolves — including the **second** type-erasure layer (`<Iface>InterfaceTraits::Model<Op>`) that the 23-slot vtable only *gates* into; and the caller-side dispatch site. Throughout, the reference op is `xla::PureCallOp`, reproduced on `tpu::IotaOp`, `llo::ConstantOp`, and `mosaic_sc::RelayoutOp` — three dialects, one ABI.

For reimplementation, the contract is:

- **The `OperationName::Impl` record and the `Concept`/`Model<Op>` CRTP** — the abstract interface base, its registered (`Model<Op>`) and unregistered (`UnregisteredOpModel`) concrete subclasses, and the Itanium PIE vtable layout that resolves them.
- **The 23-slot dispatch contract** — every slot labelled to its demangled `Model<Op>::<method>` symbol, the 1-shared + 22-per-op structure, and the four functional slot groups.
- **The Op↔Model binding** — `Dialect::addOperations<Op…>` → `RegisteredOperationName::insert`, and the three-level delegation (Model thunk → inline-property unpack → ODS static / `UniqueFunction` holder).
- **Interface resolution** — slot-4 `hasTrait` as the membership gate, and the separate `<Iface>InterfaceTraits::Model<Op>` concept layer that carries the interface bodies.

| | |
|---|---|
| **Interned-op record** | `mlir::OperationName::Impl` — abstract op-hook interface base |
| **Registered concrete impl** | `mlir::RegisteredOperationName::Model<Op>` (CRTP, one per registered op) |
| **Unregistered fallback impl** | `mlir::OperationName::UnregisteredOpModel` (single, text-parsed ops) |
| **Model vtable count (all dialects)** | **6,050** across 61 namespaces (= total registered-op count) |
| **Vtable slots per Model** | **23** = slot 0 shared base dtor + slot 1 per-op dtor + slots 2–22 dispatch |
| **Shared slot-0 dtor** | `mlir::OperationName::Impl::~Impl` @ `0xfea8820` (D2; identical addend ∀ Models) |
| **Registration sink** | `RegisteredOperationName::insert(unique_ptr<OperationName::Impl>, ArrayRef<StringRef>)` @ `0x1d8c57a0` |
| **Per-dialect registrars** | `mlir::Dialect::addOperations<Op…>` — **205** instantiations binary-wide |
| **Reference Model walk** | `Model<xla::PureCallOp>` slots @ `0x150d56c0…0x150d5c20` |
| **Caller-side dispatch** | `mlir::Operation::fold` @ `0x1d8cd480` → `call *0x10(vptr)` = slot 2 |
| **Second interface layer** | `mlir::detail::<Iface>InterfaceTraits::Model<Op>` (`Concept`-based) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The OperationName::Impl Record and the Concept/Model CRTP

MLIR identifies an op kind by `OperationName`, a thin handle wrapping a pointer to an interned, per-kind `OperationName::Impl`. The `Impl` holds the op's name string, owning `Dialect*`, `TypeID`, and an `InterfaceMap` (the set of interface concepts the op implements), and — critically — it *is* the dispatch vtable: every operation hook MLIR needs (fold, verify, print, attribute/property management) is a virtual on `Impl`. The constructor signature is recovered intact:

```text
mlir::OperationName::Impl::Impl(
    llvm::StringRef name, mlir::Dialect*, mlir::TypeID,
    mlir::detail::InterfaceMap)                       @ 0x1d8c4d80
```

`Impl` is abstract; the binary carries exactly **two** concrete implementations of its 21-hook interface:

```text
                       mlir::OperationName::Impl            (abstract interface)
                       ~Impl @ 0xfea8820 (D2)  ·  21 pure-virtual op hooks
                                  │
                ┌─────────────────┴──────────────────┐
                ▼                                     ▼
 RegisteredOperationName::Model<Op>      OperationName::UnregisteredOpModel
 CRTP, one instantiation per             single instance, the fallback for
 registered op (6,050 total)             ops parsed from text without a
 each hook forwards to Op's ODS          registered dialect; each hook is a
 statics / UniqueFunction holder         generic no-op / diagnostic stub
```

`Model<Op>` is a textbook type-erasure **Concept/Model** pair: `Impl` is the *Concept* (the abstract interface), and `Model<Op>` is the *Model* (the per-type concrete realisation), templated on the concrete op type so the compiler can stamp out a forwarding thunk for each hook at instantiation time. This is the same idiom MLIR uses for `OpInterface` dispatch — and indeed the interface layer (below) names its types `Concept` and `Model<Op>` explicitly. The 23-slot vtable documented next is the layout of one `Model<Op>` realisation.

> **NOTE — `UnregisteredOpModel` carries the identical 21-hook surface.** The fallback for unregistered (text-parsed) ops, `mlir::OperationName::UnregisteredOpModel`, implements every hook the registered `Model<Op>` does — `foldHook`, `hasTrait`, `getCanonicalizationPatterns`, `printAssembly`, `verifyInvariants`, `verifyRegionInvariants`, the four inherent-attr accessors, the nine property slots, `getParseAssemblyFn`, `populateDefaultAttrs` — confirming the surface is the abstract `Impl` interface, not a `Model`-specific extension. A reimplementer's `Impl` base must declare all 21 as virtual; both subclasses override all 21.

---

## The 23-Slot Model&lt;Op&gt; Vtable

For a *registered* op, the interned `OperationName::Impl` is a `RegisteredOperationName::Model<Op>`, and its vtable follows the Itanium PIE layout: `vtable+0x00` is offset-to-top (0), `vtable+0x08` points at the `_ZTI…Model<Op>…` typeinfo, and `vtable+0x10` is the *address point* — the value an object's vptr stores, and the location of slot 0. Slot *i* sits at `vtable+0x10+8i`. In the on-disk image the slot words are zero; they are filled at load by `R_X86_64_RELATIVE` relocations whose addends are the method addresses. Resolving each addend against the sorted symbol table recovers the method for every slot.

The reference walk is `Model<xla::PureCallOp>`; the same 23 labels appear, byte-identical in ordering and signature, on `Model<mlir::tpu::IotaOp>`, `Model<mlir::llo::ConstantOp>`, and `Model<mlir::mosaic_sc::RelayoutOp>`. Slot addends in the right column are `PureCallOp`'s (from the decompiled symbol set @ `0x150d56c0…0x150d5c20`).

```text
RegisteredOperationName::Model<Op>  vtable   (address point = +0x10 = slot 0)

 slot  off   method (Model<Op>::…)                       PureCallOp addend
 ----  ----  ------------------------------------------  -----------------
  0    0x00  OperationName::Impl::~Impl                  0xfea8820  (SHARED ∀ ops)
  1    0x08  ~Model  [deleting]                          per-op (D0)
  2    0x10  foldHook(Operation*, ArrayRef<Attribute>,   0x150d56c0
                       SmallVectorImpl<OpFoldResult>&)
  3    0x18  getCanonicalizationPatterns(                0x150d5740
                       RewritePatternSet&, MLIRContext*)
  4    0x20  hasTrait(TypeID)                            0x150d5760
  5    0x28  getParseAssemblyFn()                        0x150d57e0
  6    0x30  populateDefaultAttrs(OperationName const&,  0x150d5800
                       NamedAttrList&)
  7    0x38  printAssembly(Operation*, OpAsmPrinter&,     0x150d5820
                       StringRef)
  8    0x40  verifyInvariants(Operation*)                0x150d58a0
  9    0x48  verifyRegionInvariants(Operation*)          0x150d5940
 10    0x50  getInherentAttr(Operation*, StringRef)      0x150d59e0
 11    0x58  setInherentAttr(Operation*, StringAttr,     0x150d5a40
                       Attribute)
 12    0x60  populateInherentAttrs(Operation*,           0x150d5aa0
                       NamedAttrList&)
 13    0x68  verifyInherentAttrs(OperationName,          0x150d5ae0
                       NamedAttrList&, function_ref<…>)
 14    0x70  getOpPropertyByteSize()                     0x150d5b00
 15    0x78  initProperties(OperationName, PropertyRef,  0x150d5b20
                       PropertyRef)
 16    0x80  deleteProperties(PropertyRef)               0x150d5b40
 17    0x88  populateDefaultProperties(OperationName,    0x150d5b60
                       PropertyRef)
 18    0x90  setPropertiesFromAttr(OperationName,        0x150d5b80
                       PropertyRef, Attribute, fn_ref<…>)
 19    0x98  getPropertiesAsAttr(Operation*)             0x150d5ba0
 20    0xa0  copyProperties(PropertyRef, PropertyRef)    0x150d5be0
 21    0xa8  compareProperties(PropertyRef,PropertyRef)  0x150d5c00
 22    0xb0  hashProperties(PropertyRef)                 0x150d5c20
```

The 23 slots cluster into four functional groups, each with a distinct binding shape (next section):

- **Slots 2–9 — the dispatch surface.** Folding, canonicalization, the trait/interface gate (`hasTrait`), custom parse/print, default-attribute population, and the two verifiers. Slots 2, 3, 7, 8 — and 4 — are *indirect*: present for every op but routed through a `UniqueFunction` holder that wraps a no-op unless the op defines the hook.
- **Slots 10–13 — inherent-attribute accessors.** Inherent attributes are the op's *declared* attributes (e.g. `iota`'s `dimensions`), as opposed to discardable ones. These four slots compute the inline-property offset and tail-jump straight to the op's ODS statics.
- **Slots 14–22 — property management.** MLIR's `Properties` is the inline storage for an op's inherent attributes and variadic operand-segment sizes. These nine slots size, construct, destruct, copy, compare, hash, and (de)serialise that storage.

> **QUIRK — the "23-slot Model" is structurally 1-shared + 22-per-op, not 23 per-op.** Slot 0 — the address point that every object's vptr stores — is the *same* relocation addend `0xfea8820` (`mlir::OperationName::Impl::~Impl`, the D2 base-subobject destructor) across all 6,050 Models. It is the destructor of the common `OperationName::Impl` base subobject and is shared by construction, not duplicated per op. Only slots 1–22 are per-op template instantiations: slot 1 is `Model<Op>::~Model` (the D0 deleting dtor) and slots 2–22 are the dispatch thunks. A reimplementer should model `Model<Op>` as *one shared base destructor plus 22 op-specific entries*. (Verified directly: `OperationName::Impl::~Impl` resolves to `0xfea8820`; the count of distinct `Model<…>::~Model` D0 symbols is exactly 6,050.)

> **QUIRK — `getOpPropertyByteSize` (slot 14) is an inlined `sizeof(Op::Properties)` constant, not a computation.** The compiler folds the slot to a literal `return`. `Model<mlir::tpu::IotaOp>::getOpPropertyByteSize` (@ `0x14ac19c0`) decompiles to `return 8;` — 8 bytes, the single `dimensions` `DenseArrayAttr` handle. For an op with no `Properties` it returns 0 (`xor %eax,%eax; ret`). The two-instruction body is the cheapest diagnostic for "does this op have inline properties"; the nine property slots (14–22) only do real work when this returns nonzero.

---

## The Op↔Model Binding

A concrete op is bound to its 23-slot Model not by overriding virtuals in a hand-written subclass, but by **template instantiation + delegation**, in three levels.

### Level 1 — registration: addOperations → insert

Each dialect registers its ops with a single variadic `Dialect::addOperations<Op…>` template instantiation, called from the dialect's `initialize()`. The instantiation's *arity* is the dialect's op count — read off the symbol, not a string scan. The `tpu` registrar's head, recovered verbatim:

```text
mlir::Dialect::addOperations<
    mlir::tpu::AllReduceOp, mlir::tpu::AllocaSemaphoreOp,
    mlir::tpu::AssumeLayoutOp, mlir::tpu::AssumeMultipleOp,
    mlir::tpu::BarrierOp, mlir::tpu::BitcastOp, …>      @ 0x14aa2c40
```

For each `Op` in the pack, `addOperations` materialises a `Model<Op>` (an instance whose vtable holds the 23 `Model<Op>::*` thunks) and hands it to the single, non-template registration sink:

```text
mlir::RegisteredOperationName::insert(
    std::unique_ptr<mlir::OperationName::Impl, …>,   ← the Model IS the Impl
    llvm::ArrayRef<llvm::StringRef>)                  @ 0x1d8c57a0
```

The `Model<Op>` *is* the `OperationName::Impl` — registration is "intern this Impl under the op's name(s)." Binary-wide there are **205** `addOperations` instantiations — one per registered dialect (some split into batches). A reimplementer's dialect-init path is: build a `Model<Op>` per op, `insert` each under its mnemonic; the `Impl` ownership transfers via `unique_ptr` into the context's interning map.

### Level 2 — per-slot delegation to ODS statics

Each `Model<Op>::slotN` thunk does two things: it locates the op's `Properties` storage (inline vs. out-of-line, chosen from the `Operation` flag word), then tail-calls the ODS-generated static. The inline-property-offset idiom is identical across ops and across slots 10–13. The decompiled body of `Model<tpu::IotaOp>::getInherentAttr` (slot 10 @ `0x14ac18a0`) is exactly this shape:

```c
// Model<mlir::tpu::IotaOp>::getInherentAttr   (slot 10 @ 0x14ac18a0)
Attr getInherentAttr(Operation* op, StringRef name):
    ctx = Attribute::getContext(op->result_type_attr);  // op + 24
    props = op + ((op->flags >> 19) & 0x10) + 64;        // flags @ op+0x2c (44):
                                                         //   bit 19 picks inline (+0x10)
                                                         //   vs out-of-line storage;
                                                         //   inline base is op+0x40 (64)
    return mlir::tpu::IotaOp::getInherentAttr(ctx, *props, name);  // TAIL-CALL
```

The tail-call target is the concrete op's ODS static, recovered as a demangled symbol:

```text
mlir::tpu::IotaOp::getInherentAttr(
    mlir::MLIRContext*,
    mlir::tpu::detail::IotaOpGenericAdaptorBase::Properties const&,
    llvm::StringRef)                                  @ 0x14b220e0
```

The four inherent-attribute slots (10–13) all use this direct tail-call shape; the property slots (14–22) likewise forward to per-op `readProperties`/`writeProperties`/`computePropertiesHash` ODS statics, with slot 14 folded to the inline `sizeof` constant as noted above.

### Level 3 — indirect hooks via UniqueFunction callback-holders

The *indirect* hooks — `foldHook` (2), `getCanonicalizationPatterns` (3), `hasTrait` (4), `printAssembly` (7), `verifyInvariants` (8) — are wrapped one indirection deeper, in an `llvm::detail::UniqueFunctionBase<…>::CallbacksHolder<Op<Op,Traits…>::get<Hook>Fn()::lambda>`. The slot loads the holder pointer and does `call *(%rax)` (or `call *0x10(%rax)` for the larger holders). The decompiled `Model<tpu::IotaOp>::hasTrait` (slot 4 @ `0x14ac1620`) names the holder explicitly:

```text
llvm::detail::UniqueFunctionBase<bool, mlir::TypeID>::CallbacksHolder<
  mlir::Op<mlir::tpu::IotaOp,
           mlir::OpTrait::ZeroRegions, mlir::OpTrait::OneResult,
           mlir::OpTrait::OneTypedResult<mlir::VectorType>::Impl,
           mlir::OpTrait::ZeroSuccessors, mlir::OpTrait::ZeroOperands,
           mlir::OpTrait::OpInvariants,
           mlir::BytecodeOpInterface::Trait,
           mlir::ConditionallySpeculatable::Trait,
           mlir::OpTrait::AlwaysSpeculatableImplTrait,
           mlir::MemoryEffectOpInterface::Trait>::getHasTraitFn()::lambda, …>
```

This third level is where the **full trait and interface list of the op is encoded**: the holder symbol carries the entire `Op<OpName, Trait1, …, Interface::Trait, …>` template signature. For `IotaOp` above, the interface traits read off directly: `BytecodeOpInterface`, `ConditionallySpeculatable`, `MemoryEffectOpInterface`. Reading those template arguments off the per-op `getFoldHookFn`/`getHasTraitFn` holders is how the dialect pages recover each op's interface fan-out — and it is cross-validated against the second-layer interface Models (below).

> **GOTCHA — the indirect hooks default to a no-op, not to a crash.** `foldHook` and `getCanonicalizationPatterns` are present on all 6,050 Models, but each wraps a `UniqueFunction` whose callable is the generic no-op unless the op *defines* the method (a real `Op::fold` / `Op::getCanonicalizationPatterns` static exists). A reimplementer must not assume a populated slot implies a real folder — the slot is always populated. The *census* of which ops actually fold/canonicalize is a per-dialect fact owned by the dialect pages (for the `tpu` dialect: 6 real `fold()` + 9 real `getCanonicalizationPatterns()`; see [tpu Dialect](tpu-dialect-and-ops.md)).

---

## Interface Resolution

The 23-slot Model answers "does this op kind exist and what are its registration hooks." It does **not** carry `OpInterface` bodies — "what side effects does this op have," "what types does it infer." Those route through a **second** type-erasure layer.

### The two layers

```text
Layer 1 — op registration                Layer 2 — interface dispatch
─────────────────────────                ───────────────────────────
RegisteredOperationName::Model<Op>        mlir::detail::<Iface>InterfaceTraits
  slot 4: hasTrait(TypeID)  ───gates──▶     ::Concept           (abstract)
                                            ::Model<Op>         (per-(op,iface))
                                              e.g. getEffects(Concept const*,
                                                              Operation*, …)
```

Slot 4 (`hasTrait`) is only the **gate**: given an interface's `TypeID`, it answers yes/no whether this op implements it (via the `Op<…>::getHasTraitFn` lambda, which closes over the op's compile-time trait list). When the answer is yes, the actual call dispatches into a *separate* concept table keyed by `(op, interface)`:

```text
mlir::detail::MemoryEffectOpInterfaceInterfaceTraits::Model<xla::PureCallOp>::getEffects
mlir::detail::CallOpInterfaceInterfaceTraits::Model<xla::PureCallOp>::resolveCallable
mlir::detail::InterfaceMap::insertModel<CallOpInterfaceInterfaceTraits::Model<PureCallOp>>
```

These second-layer Models follow the same Concept/Model idiom — every method takes a `Concept const*` self-pointer as its first argument (the demangled signatures show `…::getEffects(Concept const*, Operation*, …)`). They are stored in the op's `InterfaceMap` (constructed in `OperationName::Impl::Impl`, above) and looked up by interface `TypeID`. `PureCallOp` alone carries Models for `CallOpInterface`, `MemoryEffectOpInterface`, `ConditionallySpeculatable`, `OpAsmOpInterface`, `BytecodeOpInterface`, `SymbolUserOpInterface`, and `ArgAndResultAttrsOpInterface` — confirming the layer is fully populated and per-op.

> **NOTE — a reimplementer must build both layers.** The 23-slot Model gives op registration and the `hasTrait` gate; the interface *semantics* live in the `<Iface>InterfaceTraits::Model<Op>` tables. The slot layouts of those second-layer vtables (e.g. `MemoryEffectOpInterface::getEffects`, `InferTypeOpInterface::inferReturnTypes`, the `Bytecode` read/write pair) are a separate concept surface and are out of scope here; this page establishes the gate and the lookup path, not the per-interface bodies.

### Caller-side dispatch

The Layer-1 dispatch is consumed at, e.g., `mlir::Operation::fold` @ `0x1d8cd480`, which loads the `Impl`/Model from the operation, then the vptr, then calls slot 2:

```asm
mov  0x30(%rdi),%rdi    ; %rdi = OperationName::Impl  (= the Model)
mov  (%rdi),%rax        ; %rax = vptr (address point)
call *0x10(%rax)        ; +0x10 from address point = slot 2 = foldHook   (@ 0x1d8cd4ad)
```

The `+0x10` offset from the address point is exactly slot index 2 (`0x10 / 8`), confirming the slot numbering. Every op-dispatch site in MLIR — verification, parsing, attribute access, property serialisation — has this shape: load `Impl` at `op+0x30`, deref the vptr, indirect-call the appropriate slot.

---

## The Contract Across Dialects

The 23-slot contract is dialect-agnostic: the same vtable shape backs every registered op in the binary. The `Model<Op>` instantiation count therefore *equals* the registered-op count, and summing across the 61 dialect namespaces gives **6,050**. The TPU-relevant dialects and where their rosters are owned:

```text
dialect             Models   contract owner / roster page
------------------  ------   --------------------------------------------------
sparse_core          1471    SparseCore (115 ScDialect + 1356 LlvmTpu tpu_* intrinsics)
llo                   325    LLO — TensorCore low-level machine ops
tpu                    86    tpu Dialect — TensorCore high-level (Mosaic) target
xtile                   6    XLA CPU/GPU tile IR (off the TPU device path)
mosaic_sc               1    SparseCore Mosaic — RelayoutOp only
(+ 56 more: TF 834, spirv 376, ROCDL 350, … — input/off-path dialects)
```

This page owns the *contract*; the dialect pages own the *instantiations* — the op rosters, the per-op interface fan-out, the fold/canonicalize census, and which ops carry non-trivial `Properties`. The [tpu Dialect](tpu-dialect-and-ops.md) page is the worked example: it walks `Model<mlir::tpu::IotaOp>` against this contract, lists the 86 `tpu` ops grouped by interface signature, and ties the 9 property-carrying ops to slots 14–22. Each of `Model<llo::ConstantOp>` (@ slots near `0x13e71420`), `Model<mosaic_sc::RelayoutOp>` (@ `0x132faba0…`), and the 1356 `sparse_core::tpu_*` Models was confirmed to carry the identical 23-slot layout.

> **NOTE — version-pinned ordering.** The slot *indices* (foldHook = 2, hasTrait = 4, getInherentAttr = 10, …) are specific to this binary's bundled LLVM SHA. The slot *set* is stable MLIR API, but the *order* is an ABI detail of one build. A reimplementer targeting a different MLIR version must re-walk a Model vtable to recover the ordering; do not hard-code these indices against upstream MLIR without verification.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| `OperationName::Impl` is the abstract op record; ctor takes `(StringRef, Dialect*, TypeID, InterfaceMap)` | demangled `OperationName::Impl::Impl(StringRef, Dialect*, TypeID, detail::InterfaceMap)` @ `0x1d8c4d80` | CONFIRMED |
| `Impl` has two concrete impls: `Model<Op>` and `UnregisteredOpModel` | both carry the full 21-hook surface (`UnregisteredOpModel::{foldHook,hasTrait,…}` symbols present) | CONFIRMED |
| `Model<Op>` is a 23-slot vtable; 6,050 instances binary-wide | count of distinct `Model<…>::~Model` (D0) symbols = 6,050; reference walk on `Model<xla::PureCallOp>` slots `0x150d56c0…0x150d5c20` | CONFIRMED |
| All 21 dispatch-slot symbols present per op, same order across dialects | `Model<{PureCallOp, tpu::IotaOp, llo::ConstantOp, mosaic_sc::RelayoutOp}>::<method>` resolve identically | CONFIRMED |
| Slot 0 is one shared base dtor across all Models | `OperationName::Impl::~Impl` (D2) = addend `0xfea8820`; 30-Model stratified sample shares it | CONFIRMED |
| Registration sink is `RegisteredOperationName::insert` | demangled `insert(unique_ptr<OperationName::Impl,…>, ArrayRef<StringRef>)` @ `0x1d8c57a0`; 205 `addOperations` instantiations | CONFIRMED |
| Inherent-attr slots tail-call ODS statics via inline-prop unpack | `Model<IotaOp>::getInherentAttr` body computes `op + ((flags>>19)&0x10) + 64` then calls `IotaOp::getInherentAttr(...)` @ `0x14b220e0` | CONFIRMED |
| Indirect hooks (2,3,4,7,8) wrapped in `UniqueFunction` callback-holders carrying the `Op<…>` trait list | `Model<IotaOp>::hasTrait` holder names `Op<tpu::IotaOp, …, MemoryEffectOpInterface::Trait>::getHasTraitFn()::lambda` | CONFIRMED |
| `getOpPropertyByteSize` is an inlined `sizeof(Properties)` | `Model<IotaOp>::getOpPropertyByteSize` @ `0x14ac19c0` decompiles to `return 8;`; zero-prop ops return 0 | CONFIRMED |
| Second interface layer `<Iface>InterfaceTraits::Model<Op>` carries interface bodies; slot-4 `hasTrait` gates it | `MemoryEffectOpInterfaceInterfaceTraits::Model<PureCallOp>::getEffects`, `InterfaceMap::insertModel<…>` symbols; `Concept const*` self-arg | CONFIRMED |
| Caller dispatch loads `Impl` at `op+0x30`, vptr, `call *0x10` = slot 2 | `Operation::fold` @ `0x1d8cd480`, indirect call @ `0x1d8cd4ad` | CONFIRMED |
| Slot ordering is version-pinned to this binary's LLVM SHA | indices specific to one build; not cross-validated against upstream MLIR | LOW (cross-version stability) |

---

## Cross-References

- [tpu Dialect: Ops and the Op-Model Contract](tpu-dialect-and-ops.md) — the worked instantiation of this contract: the 86 `tpu` ops, their interface fan-out, the fold/canonicalize census, and the 9 property-carrying ops. That page applies the contract; this page owns it.
- [Compiler Overview](overview.md) — where the MLIR layers sit in the five-phase descent and the six-level IR stack; the convergence point of the optimizer and Mosaic paths.
- [tpu → LLO Lowering](tpu-to-llo-ods.md) — the `llo` dialect (325 ops) below `tpu`, registered through the identical 23-slot contract; the structural lowering that transforms most ops rather than folding them in place.
- [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md) — the mid-level lowering that produces the `tpu` ops these Models register.
- [DialectConversion Legalizer](dialect-conversion-legalizer.md) — the conversion-pattern machinery that consumes registered ops during lowering; it queries the same `OperationName`/`Impl` records.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
