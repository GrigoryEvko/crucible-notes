# Cast-Elimination Numeric Passes

> *All symbols and addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 Cython modules). The numeric reconciler is `neuronxcc/starfish/penguin/targets/tonga/passes/TongaEliminateCasts.py`, compiled to `TongaEliminateCasts.cpython-310-x86_64-linux-gnu.so` (class `NeuronEliminateCasts`); the offloaded-memory cast pass is `neuronxcc/starfish/penguin/transforms/CastOpElimination.py`. The cp311/cp312 wheels carry identical logic at shifted addresses — treat every address as version-pinned.*

## Abstract

This is the **third and final** stage of Penguin's fp32-narrowing chain. The first stage, [AutoCastFP32](autocast-fp32.md), *inserts* casts to legalize an fp32 program to a narrower arithmetic dtype; the second stage, the [DTypeMutator](dtype-mutator.md) (§9.5), *moves and propagates* those dtypes across the graph; this stage **absorbs** the casts that the first two left redundant. The pass is named in its own docstring (`@_rodata`): *"NeuronEliminateCasts — Eliminate unnecessary casts (tensor copies) where producer and consumer have the same src type."* Concretely it removes a `bf16` precision round-trip: a producer that down-casts `fp32→bf16`, a cast that up-casts `bf16→fp32`, and a consumer that requires `fp32` — by mutating the *producer's* destination dtype to `fp32` and deleting the cast outright. No precision is lost, because the lost-precision hop (the `bf16` truncation) is exactly what is removed.

One naming trap dominates this area: **`CastOpElimination` is not the numeric reconciler.** Despite the name, that `DotTransform`'s docstring reads *"Transform pass to eliminate unnecessary offloaded memory casts"* — it targets offloaded/DMA memory-cast ops, delegates the actual fold to a `CastOpOptimizer` engine, and drops dead tensors. All of the `fp32↔bf16` producer-dst numeric reconciliation lives entirely in `TongaEliminateCasts` / `NeuronEliminateCasts`. The two are complementary cast-removers at different IR altitudes; only the latter does what this page describes. See [§`CastOpElimination`](#castopelimination--the-offloaded-memory-sibling) below.

A second distinction worth pinning up front: this is a **numeric** cast-elimination — it removes casts that *change dtype*, reconciling a producer's dst dtype to the consumer-required dtype. It is distinct from the **geometric** copy-elimination of [Penguin Data-Movement: Fusion + Copy-Elimination](../penguin/data-movement-fusion.md) (5.12), which removes copies/reshapes/transposes that *move data with identical dtype*. The invariants differ: this pass keys on *src-type equality across a cast* (a numeric round-trip); the geometric family keys on *same-dtype layout fusion* (a structural no-op). They share the word "cast" and nothing else.

## Reimplementation contract

A faithful reimplementation must reproduce:

- **`shouldMutateInstDstType(inst, dtype)`** — the legality predicate. Four gates in order: `canMutateDstTy` capability flag, the three-class op whitelist, the `src.dtype != dtype` reconcilable-distance check, and the **int/float-class consistency guard** that forbids silently flipping integer↔float numeric semantics.
- **`tryEliminateCast(cast)`** — the per-cast driver, with its **two branches**: the single-producer `NeuronInst` branch (direct producer, `assert`-guarded) and the all-producers `NeuronAP` branch (a tensor with ≥1 store-inst, every store must be legally mutable before any rewrite).
- **`eliminateCast(cast, src_inst, dtype)`** — the rewrite primitive: `mutateDstTy` → `count_cast` → `replaceAllUsesWith`, in that order.
- **`transformStmts(f)`** — the collect-then-rewrite walk: snapshot all candidate `TensorCopyOp` casts into a list, then iterate calling `tryEliminateCast`.
- **`count_cast(cast)`** + the byte-accounting class counter `eliminated_casts`, registered as the stat *"Number of bytes of casts eliminated"* in `Unit.Bytes`.

| | |
|---|---|
| **Pass class** | `NeuronEliminateCasts(TargetLowering)` — `TongaEliminateCasts.cpython-310.so` |
| **Source path** | `neuronxcc/starfish/penguin/targets/tonga/passes/TongaEliminateCasts.py` (Copyright 2021) |
| **Pass driver** | `transformStmts` @ `0xffd0` (TargetLowering override; py 84–89) |
| **Legality predicate** | `shouldMutateInstDstType` @ `0x15b20` (py 32–41) |
| **Per-cast driver** | `tryEliminateCast` @ `0x12630` (py 53–~85) |
| **Rewrite primitive** | `eliminateCast` @ `0xcc40` (py 47–50) |
| **Byte accounting** | `count_cast` @ `0x111a0` (py 24–28) |
| **Constructor** | `__init__` @ `0x14690` (thin `super().__init__`) |
| **Genexpr helper** | `tryEliminateCast.<locals>.genexpr` generator @ `0xecd0` |
| **IR level** | Penguin / Tonga target NeuronInst graph (post-AutoCastFP32, post-DTypeMutator) |
| **Offloaded-mem sibling** | `CastOpElimination(DotTransform)` — delegates to `CastOpOptimizer` (NOT this pass) |

## The numeric reconciliation, in one chain

The class docstring survives contiguous in `_rodata` and is the canonical spec, verbatim:

```text
NeuronEliminateCasts - Eliminate unnecessary casts (tensor copies) where producer
                       and consumer have the same src type
in cases such as:
  producer inst: dst bf16, src fp32
  cast:          dst fp32, src bf16
  consumer inst: src must be fp32
we can eliminate the cast, and change dst of producer inst to fp32
```

Read as a dataflow chain `P ──(bf16)──▶ C(cast) ──(fp32)──▶ U`:

- **P** (producer) reads `src=fp32` but writes `dst=bf16` — it down-casts.
- **C** is a `CastToNewDType` / `TensorCopyOp` that up-casts `bf16 → fp32`.
- **U** (consumer) requires its `src` to be `fp32`.

The intermediate `bf16` is a needless precision truncation: P narrowed to `bf16`, C widened back to `fp32`, and the only numeric effect is the lost low bits in the round-trip. If P's dst dtype can *legally* be mutated to `fp32`, the pass:

1. mutates `P.dst : bf16 → fp32` (`mutateDstTy`),
2. redirects every use of C's result to P's now-`fp32` result (`replaceAllUsesWith`),
3. deletes the cast C (`eraseFromParent`).

The result is `P ──(fp32)──▶ U` directly, one `TensorCopyOp` fewer, and *more* precision retained, not less — the truncating hop is gone.

> **WHY THIS IS SAFE —** the elimination only fires when P's *source* type already equals the dtype U demands (the "same src type" invariant). P was reading `fp32` all along; it was merely told to *store* `bf16`. Telling it to store `fp32` instead removes a truncation without inventing any new precision. The guards below exist to ensure that "merely told to store a narrower dtype" is genuinely the only thing happening.

## `shouldMutateInstDstType` — the legality predicate

`shouldMutateInstDstType(self, inst, dtype)` @ `0x15b20` answers: *may producer `inst` have its dst dtype mutated to `dtype` so we can absorb a downstream cast?* The arg names `(self, inst, dtype)` are confirmed from the pyargnames pool; the source span is `TongaEliminateCasts.py:32–41`. The body is four short-circuit gates:

```c
// shouldMutateInstDstType(self, inst, dtype)  @0x15b20  (py 32-41)
int shouldMutateInstDstType(inst, dtype) {
    // GATE 1 (py 33): the inst must advertise that its dst dtype is mutable at all.
    //   'canMutateDstTy' is a per-NeuronInst bir property (string @_rodata): an op
    //   whose dst feeds a fixed-format consumer answers False here.
    if (!getattr(inst, "canMutateDstTy"))           // @15438 getattr canMutateDstTy
        return False;                                //   -> Py_False (LABEL_56 @15588)

    // GATE 2 (py 36): only three op KINDS may have their dst dtype mutated.
    //   Three chained PyObject_IsInstance, short-circuit on first True.
    if (!isinstance(inst, ActivationOp) &&          // @15498 GetBuiltin ActivationOp
        !isinstance(inst, TensorScalarPtrOp) &&     // @15531 GetBuiltin TensorScalarPtrOp
        !isinstance(inst, TensorCopyOp))            // @15543 ModuleGlobal TensorCopyOp
        return False;                                //   -> Py_False @15558-15563

    // GATE 3 (py 38): if the producer's SOURCE dtype already equals the target dtype,
    //   there is nothing to reconcile -> not a candidate. (Distinct from cast.dtype
    //   == cast.src.dtype in tryEliminateCast; this compares inst.src.dtype to the ARG.)
    if (getattr(getattr(inst, "src"), "dtype") == dtype)   // @15595 inst.src, @15606 .dtype,
        return False;                                       //   @15614 RichCompare -> LABEL_326

    // GATE 4 (py 40-41): INT/FLOAT-CLASS CONSISTENCY GUARD.
    //   is_int_type imported from neuronxcc.starfish.support.dtype. The mutation is
    //   legal only when the producer's CURRENT dst dtype and its src dtype belong to
    //   the SAME numeric class (both int, or both float). This blocks absorbing a cast
    //   that would silently flip int<->float numeric semantics.
    return is_int_type(getattr(inst, "dtype"))      // @15671 is_int_type(inst.dtype)
        == is_int_type(getattr(getattr(inst,"src"),"dtype"));  // @15858 is_int_type(inst.src.dtype)
}
```

**The int/float-class guard (GATE 4) is the numeric heart of the predicate.** `is_int_type` is called on *both* operands: `inst.dtype` (the producer's current dst dtype, via `getattr 'dtype'` on the inst @15671/15804/15944) and `inst.src.dtype` (the producer's src dtype, via `getattr 'dtype'` on `inst.src` @15856–15858). The symmetric branch fan-in — `Py_True` on the class-match path (LABEL_178 @15984, reached @15846) and `Py_False` on the class-mismatch path (LABEL_326 @15989, reached @15982) — makes the faithful reading an **equality of the two int-class predicates**: the mutation is allowed iff `is_int_type(dst) == is_int_type(src)`.

*Grounding: `is_int_type` is applied to both `inst.dtype` and `inst.src.dtype`, and the True/False targets are symmetric on class match versus mismatch. The exact `==`-of-two-predicates **surface** (as against a flattened nested-and) is HIGH, not byte-perfect: Cython lowers the boolean into four `is_int_type` call sites plus branch fan-in, so the form `a == b` is reconstructed from the symmetric targets rather than read off one opcode. The semantic content — "same numeric class required" — is unambiguous either way.*

The predicate pairs with a mutation **action**: `canMutateDstTy` (read, GATE 1) is the capability flag; `mutateDstTy` (called by `eliminateCast`) is the primitive that actually flips the dtype. `shouldMutateInstDstType` is the *check*; `mutateDstTy` is the *act*. They share the `MutateDstTy` stem on the same bir concept (a producer inst whose dst dtype is mutable).

## `tryEliminateCast` — the two-branch driver

`tryEliminateCast(self, cast)` @ `0x12630` (py 53–~85) is the per-cast predicate-plus-rewrite driver. It first bails when the cast is a numeric no-op, then **dispatches on the kind of the cast's source** — a single producer instruction, or an access pattern over a tensor with possibly multiple producers. The split is an `isinstance(cast.src, NeuronAP)` test.

```c
// tryEliminateCast(self, cast)  @0x12630  (py 53-~85)   returns True iff cast eliminated
int tryEliminateCast(cast) {
    src   = getattr(cast, "src");                   // @12364 getattr 'src'   (py 54)
    dtype = getattr(cast, "dtype");                 // @12377 getattr 'dtype' (py 55) = cast DST dtype

    // py 56: if the cast's dst dtype already equals its src's dtype, it is a numeric
    //   no-op here -> nothing to reconcile, bail.
    if (getattr(cast,"dtype") == getattr(getattr(cast,"src"),"dtype"))  // @12392/@12404 cmp
        return False;                                //   v26 -> LABEL_233 (Py_False)

    // py 59: dispatch on the source kind
    if (!isinstance(getattr(cast,"src"), NeuronAP)) {   // @12460 NeuronAP, @12476 IsInstance
        // ---- BRANCH A: src is a SINGLE producer instruction ----
        assert isinstance(getattr(cast,"src"), NeuronInst);  // @12871 NeuronInst, @12504 IsInstance,
                                                             //   @12525 AssertionError raise (py 77)
        if (shouldMutateInstDstType(cast.src, dtype)) {     // @12532 call; @12620 branch on !result
            eliminateCast(cast, cast.src, dtype);           // @12631 'eliminateCast'
            getattr(cast,"eraseFromParent")();              // @12708 'eraseFromParent' (py 80)
            return True;                                     // @12739 Py_True
        }
        return False;                                        // @12620 -> LABEL_233 (Py_False)
    }

    // ---- BRANCH B: src is a NeuronAP backed by a tensor (>= 1 producer) ----
    tensor = getattr(cast.src, "tensor");           // @12889 getattr 'tensor' (py 60)
    if (getattr(tensor, "isInput"))                 // @12901 getattr 'isInput' (py 61)
        return False;                                //   @12931 -> LABEL_341 (bail: graph input, dtype fixed)
    if (isinstance(tensor, NeuronPSUMTensor))       // @12941 NeuronPSUMTensor (py 64)
        return False;                                //   @12968 -> LABEL_341 (bail: hw-fixed PSUM dtype)

    // py 69-70: gather all instructions that WRITE this tensor
    stores = TensorUtils.store_insts(tensor);       // @12978 TensorUtils, @12995 'store_insts',
                                                    //   @13011 call(tensor) -> list of producers
    // py 70: EVERY producing store must be legally mutable to 'dtype' (all-of)
    if (all(shouldMutateInstDstType(store, dtype)   // genexpr @8780/@9189 shouldMutateInstDstType
            for store in stores)) {                 //   arg0=store @9204, arg1=outer dtype @9207
        for (store_inst in TensorUtils.store_insts(tensor))  // @13154 'store_insts' (2nd call)
            eliminateCast(cast, store_inst, dtype);          // @13238 'eliminateCast' (per store)
        getattr(cast,"eraseFromParent")();          // erase once, after all producers mutated
        return True;
    }
    return False;
}
```

**Branch A — single producer.** When `cast.src` is not a `NeuronAP`, the source is a direct producer instruction. An `assert isinstance(cast.src, NeuronInst)` (py 77; the `AssertionError` raise path is at @12525) pins the invariant, then the single producer is checked with `shouldMutateInstDstType` and, on success, mutated-and-erased in one step. This is the simple case: one producer, one decision.

**Branch B — all producers behind an access pattern.** When `cast.src` *is* a `NeuronAP`, the cast's source is an access pattern over a backing tensor that may be written by several store-insts. Two guards exclude tensors whose dtype cannot be moved: `tensor.isInput` (a graph input — its dtype is externally fixed) and `isinstance(tensor, NeuronPSUMTensor)` (a PSUM accumulator — its dtype is hardware-fixed). The pass then gathers `TensorUtils.store_insts(tensor)` and requires that **every** producing store individually satisfy `shouldMutateInstDstType` before committing — the `all(...)` over the inlined generator. Only if all pass does it loop and `eliminateCast` each store, then erase the cast once.

The `all(...)` is realized as a Cython generator (`tryEliminateCast.<locals>.genexpr` @ `0xecd0`) with two scope structs — an outer `scope_struct__tryEliminateCast` (holds `self` + `dtype`) and an inner `scope_struct_1_genexpr` (holds the per-element `store` and the `store_insts` iterable). The generator body per element calls `outer.self.shouldMutateInstDstType(store, outer.dtype)` (@9189) and **short-circuits to `False`** on the first failure (@9255–9262) — exactly `all`-semantics. This is the code realization of the docstring's "consumer have the same src type": every producing store must be able to adopt the reconciled dtype before a single cast is removed.

*Grounding: the early `cast.dtype == cast.src.dtype` bail (RichCompare @12404), the two-branch `isinstance(cast.src, NeuronAP)` split (@12476), the `NeuronInst` assert (@12504/@12525), the Branch-B `isInput` / `NeuronPSUMTensor` guards, the two `store_insts` call sites (@13011, @13154), and the `all`-generator short-circuit (@9255) all come off the body. The exact placement of `eraseFromParent` in Branch B — once after the per-store loop, rather than per store — is HIGH: the erase getattr is shared codegen that Cython inlines, and the single-erase reading is what matches the docstring's singular "eliminate the cast".*

## `eliminateCast` — the rewrite primitive

`eliminateCast(self, cast, src_inst, dtype)` @ `0xcc40` (py 47–50) performs the rewrite. Arg names come from pyargnames @7106–7107. Three operations in a fixed order:

```c
// eliminateCast(self, cast, src_inst, dtype)  @0xcc40  (py 47-50)
void eliminateCast(cast, src_inst, dtype) {
    // py 48: mutate the PRODUCER's dst dtype to the reconciled dtype (bf16 -> fp32).
    getattr(src_inst, "mutateDstTy")(dtype);        // @7146 getattr 'mutateDstTy', arg dtype @7211

    // py 49: record bytes-eliminated stats for THIS cast (see count_cast).
    self.count_cast(cast);                          // @7241 getattr 'count_cast', arg cast

    // py 50: redirect every consumer of the cast's result to the cast's src,
    //   which is now the producer output already at the right dtype.
    getattr(cast, "replaceAllUsesWith")(getattr(cast,"src"));  // @7336 'replaceAllUsesWith',
                                                               //   arg cast.src @7346
    // NOTE: cast.eraseFromParent() is done by the CALLER tryEliminateCast (py 80).
}
```

**The order matters:** `mutateDstTy` → `count_cast` → `replaceAllUsesWith`. The producer dst dtype is changed *first*, so that when uses are redirected to `cast.src` the dtype already matches; `count_cast` runs between, reading the cast's tensor size before the cast is unlinked. The actual `eraseFromParent` is intentionally left to the caller — in Branch B that lets `eliminateCast` run once per store while the cast is erased exactly once.

## `transformStmts` — the collect-then-rewrite walk

`transformStmts(self, f)` @ `0xffd0` (py 84–89) is the `TargetLowering` override the pass manager invokes per function/block. It is a **single forward pass with no inner fixpoint**:

```c
// transformStmts(self, f)  @0xffd0  (py 84-89)
void transformStmts(f) {
    elim_candidates = [                              // @9907 PyList_New (py 85)
        inst for inst in getattr(f, "insts")         // @9916 getattr 'insts', @9945 GetIter
        if isinstance(inst, TensorCopyOp)            // @10000/@10021 TensorCopyOp isinstance
        and !getattr(inst, "dst")                    // @10031 getattr 'dst', @10052 !IsTrue
    ];                                               //   (append when 'dst' is falsy @10057)
    for (cast in elim_candidates)                    // py 89 loop
        tryEliminateCast(cast);                      // @10164 getattr 'tryEliminateCast'
}
```

The walk **collects first into a materialized list, then rewrites** — deliberately, because `tryEliminateCast` calls `eraseFromParent`, so iterating the live `f.insts` while mutating it would be "modified during iteration". The snapshot is stable. There is **no `while-changed` loop inside `transformStmts`**; repeat-to-fixpoint, if any, is driven by the pass manager re-running the pass, not by this method — the module-level `changed` string is not referenced in this body.

*Grounding: on the `not inst.dst` filter, only the *truthiness* of `inst.dst` is read (`getattr 'dst'` @10031, `!IsTrue` @10052), so the list keeps casts whose `dst` is falsy — the candidates for absorption. [INFERRED] anything about the dst predicate beyond that truthiness; only the getattr and its negation are present.*

## Byte accounting — `count_cast` and the `eliminated_casts` stat

`count_cast(self, cast)` @ `0x111a0` (py 24–28) accumulates a class-level byte counter:

```c
// count_cast(self, cast)  @0x111a0  (py 24-28)
PyObject* count_cast(cast) {
    bytes_per_partition =                            // py 25-26  (named intermediate @_rodata)
        getattr(cast,"tile_size_in_bytes") // getattr(cast,"npartitions");   // @11086/@11093,
                                                     //   @11098 PyNumber_FloorDivide
    eliminated_bytes =                               // py 27
        bytes_per_partition * getattr(cast,"num_dynamic_instances");  // @11111, @11127 InPlaceMultiply
    NeuronEliminateCasts.eliminated_casts += eliminated_bytes;        // py 28: CLASS attr
                                                     //   @11149 getattr 'eliminated_casts',
                                                     //   @11158 PyNumber_InPlaceAdd,
                                                     //   @11174 setattr (global @11263 = the CLASS)
    return cast;                                     // @11255
}
```

The accumulator is the **class attribute** `NeuronEliminateCasts.eliminated_casts` (the global resolved @11263/11267 is the class object), so the byte count is shared across all instances and invocations of the pass. The unit is bytes: `tile_size_in_bytes / npartitions` = per-partition bytes, times `num_dynamic_instances` = total bytes spanned by the eliminated cast tensor.

At module body (py ~20) the pass registers this stat: `register_stats(**{"Number of bytes of casts eliminated": Unit.Bytes})` — `register_stats` resolves @2832/2836, `Unit.Bytes` @2866, the key string @2882. `register_stats` and `Unit` are imported from `neuronxcc.starfish.penguin.Statistics` (@2057/2066/2084); the registration surfaces the bytes accumulated by `count_cast` under that human-readable label.

## Module shape — imports, base class, constructor

From `__pyx_pymod_exec` (@full.c 2011–2154):

| Import | Source module | Provides |
|---|---|---|
| `is_int_type` | `neuronxcc.starfish.support.dtype` | the int/float-class predicate (GATE 4) |
| `register_stats`, `Unit` | `neuronxcc.starfish.penguin.Statistics` | stat registration + `Unit.Bytes` |
| `TargetLowering` | `…penguin.targets.transforms.TargetLowering` | **base class** of `NeuronEliminateCasts` |
| `*` (wildcard) | `…penguin.targets.tonga.TongaISAInst` | `ActivationOp`, `TensorScalarPtrOp`, `TensorCopyOp`, `NeuronAP`, `NeuronInst`, `NeuronPSUMTensor`, `TensorUtils` |

`NeuronEliminateCasts(TargetLowering)` — the base is bound at class creation @2389 — inherits the target-lowering pass framework; `transformStmts` is the per-block override invoked by the pass manager. `__init__` @ `0x14690` is a thin constructor: `super().__init__(*args, **kwargs)` forwarding to `TargetLowering.__init__` (super @14004, `.init` getattr @14031, kwargs via `.items()` @14056–14078), with no extra state beyond the base plus the class-level `eliminated_casts` counter (body @full.c 13637–14702).

## Pipeline placement — insert → propagate → eliminate

This pass is the *consumer* of the casts the earlier numeric stages produce:

```text
AutoCastFP32 (X02)    DTypeMutator (X03)        TongaEliminateCasts (this, X04)
  INSERTS casts   ->    PROPAGATES dtypes   ->    ELIMINATES redundant casts
  (fp32 -> bf16)        (moves casts to            (absorbs the now-redundant
                         better positions)          fp32<-bf16 round-trips)
```

- **[AutoCastFP32](autocast-fp32.md) (9.4)** inserts `CastToNewDType` ops (surfacing as `TensorCopyOp` here) to feed an fp32-only consumer from a narrowed producer. Those are precisely the casts this pass removes.
- **[DTypeMutator](dtype-mutator.md) (§9.5)** propagates dtypes across the graph, deciding each op's dst dtype and possibly setting the `canMutateDstTy` capability. Its `shouldMutateInstDstType` is a **sibling predicate, not the same body** — DTypeMutator's API is `tryMutateDstTy`/`matchOrMutateDstTy` in a *separate* `.so`; this pass's `shouldMutateInstDstType` (@0x15b20, in *this* `.so`) is specialized to the cast-elimination legality question. They share the bir `canMutateDstTy`/`mutateDstTy` capability surface but are separately compiled bodies.
- **This pass (9.6)** cleans up: when a whitelisted producer can have its dst dtype mutated (`canMutateDstTy` + numeric-class match), the inserted cast is absorbed into the producer (`mutateDstTy`) and erased.

This pass does **not** re-derive dtype propagation; it only consults the already-decided `canMutateDstTy` flag plus the numeric class, then performs a local rewrite.

## `CastOpElimination` — the offloaded-memory sibling

> **GOTCHA — `CastOpElimination` is not the numeric reconciler.** The class in `neuronxcc/starfish/penguin/transforms/CastOpElimination.py` (Copyright **2025**) targets **offloaded / DMA memory casts**: its docstring is *"Transform pass to eliminate unnecessary offloaded memory casts."* It is a `DotTransform` adapter that delegates the actual fold to a `CastOpOptimizer` and then drops dead tensors. Every `fp32↔bf16` producer-dst reconciliation lives in `TongaEliminateCasts` / `NeuronEliminateCasts` instead. The names are close; the passes are not.

`CastOpElimination(DotTransform)` — the base is imported from `neuronxcc.starfish.penguin.DotTransform` (@1285/1302) — is a thin transform whose three method bodies are:

```c
// CastOpElimination(DotTransform)  —  the OFFLOADED-MEM pass, NOT the numeric one

// transformOffloadedMemCast(self, op)  @0x8e10  (py 17)
PyObject* transformOffloadedMemCast(op) {
    return self.optimizer.fuse(op);     // @5057 self.optimizer, @5067 .fuse, @5082 op
}   // the entire fold is DELEGATED to CastOpOptimizer.fuse(); this .so is a thin adapter.

// afterStmtTransform(self, ...)  @0x9920
void afterStmtTransform(...) {
    self.dropDeadTensors();             // @5658 getattr 'dropDeadTensors'
}   // post-transform cleanup: tensors orphaned by cast fusion are dropped.

// __init__(self, *args, **kwargs)  @0xa410
//   super().__init__(...) (DotTransform.__init__, .init @6430) forwarding kwargs incl.
//   error_category; constructs self.optimizer = CastOpOptimizer(...) (@7053 GetBuiltinName).
```

The real fusion/elimination logic for offloaded-mem casts therefore lives **inside `CastOpOptimizer`** — a different module (overlapping the `bir::CastToNewDType` / `TensorOpUtils` territory) not present in this `.so`. The split is:

| Concern | Pass | Mechanism |
|---|---|---|
| **NUMERIC** `fp32↔bf16` producer-dst-mutation cast removal | `TongaEliminateCasts` (this page, 9.6) | `shouldMutateInstDstType` + `mutateDstTy` + `replaceAllUsesWith` + `eraseFromParent` |
| **OFFLOADED-MEM** cast fusion + dead-tensor cleanup | `CastOpElimination` → `CastOpOptimizer` | `optimizer.fuse(op)` + `dropDeadTensors()` |

The two are complementary cast-removal passes at different IR levels. Only the former matches the numeric charter, and only the former has its full reconciliation body on this page.

*Anchors for the sibling: `transformOffloadedMemCast` @ `0x8e10`, `afterStmtTransform` @ `0x9920`, `__init__` @ `0xa410`; the docstring plus the `CastOpOptimizer` / `dropDeadTensors` / `DotTransform` strings in `_rodata`.*

## Distinction from geometric copy-elimination (5.12)

It is easy to confuse this NUMERIC cast-elimination with the GEOMETRIC copy-elimination of [Penguin Data-Movement: Fusion + Copy-Elimination](../penguin/data-movement-fusion.md) (5.12). They are different machines with different invariants:

| | **This pass (9.6, numeric)** | **5.12 (geometric copy-elim)** |
|---|---|---|
| Removes | a cast that **changes dtype** | a copy/reshape/transpose with **identical dtype** |
| Invariant | producer & consumer share **src type** (a numeric round-trip) | producer & consumer share **dtype**, differ in **geometry** |
| Mechanism | mutate producer dst dtype (`mutateDstTy`), redirect uses | fuse/rewrite load/store addresses (`fuse_src`/`fuse_dst`), or polyhedral ISL access-map fold (`MemcpyElimination`) |
| What survives | the producer, now writing the wider dtype | the producer, now writing the consumer's geometry |

5.12's NOTE forwards the *numeric* side of cast handling to this page. Precisely: the genuinely numeric reconciliation is `TongaEliminateCasts` (here), while 5.12's own `{Cast,…}OpElimination` family is the geometric/structural fold — and `CastOpElimination` specifically is the offloaded-memory fold that delegates to `CastOpOptimizer`, as above.

## Grounding ledger

| Claim | Confidence | Anchor |
|---|---|---|
| Class docstring + producer/cast/consumer example | CERTAIN | verbatim `_rodata` string, Copyright 2021 |
| All method addresses (`shouldMutate@0x15b20`, `tryEliminate@0x12630`, `eliminateCast@0xcc40`, `count_cast@0x111a0`, `transformStmts@0xffd0`, `__init__@0x14690`, generator@0xecd0) | CERTAIN | `_function_addresses.json` |
| `shouldMutateInstDstType` four gates | CERTAIN | full.c body; getattr/isinstance/RichCompare line nums |
| `is_int_type` applied to both `inst.dtype` and `inst.src.dtype` | CERTAIN | call sites @15671 / @15858 |
| GATE 4 exact `==`-of-two-predicates surface | HIGH | symmetric True/False fan-in; Cython-flattened boolean |
| `tryEliminateCast` two-branch split on `isinstance(cast.src, NeuronAP)` | CERTAIN | @12476 IsInstance |
| Branch B `all(...)` over store-insts (short-circuit) | CERTAIN | generator @9189/@9255 |
| Branch B `eraseFromParent` placement (once, after loop) | HIGH | shared erase codegen; matches singular docstring |
| `eliminateCast` order `mutateDstTy → count_cast → replaceAllUsesWith` | CERTAIN | @7146/@7241/@7336 |
| `transformStmts` collect-then-rewrite, no inner fixpoint | CERTAIN | full.c body; `changed` not referenced |
| `not inst.dst` filter exact predicate beyond truthiness | MEDIUM | only `dst` getattr + `!IsTrue` present |
| `count_cast` byte arithmetic + class-counter accumulation | CERTAIN | full.c body; FloorDivide/Multiply/InPlaceAdd |
| `register_stats("Number of bytes of casts eliminated", Unit.Bytes)` | CERTAIN | @2832/@2866/@2882 |
| Base class `TargetLowering`; imports | CERTAIN | `__pyx_pymod_exec` @2011–2389 |
| `CastOpElimination` is offloaded-mem, delegates to `CastOpOptimizer` | CERTAIN | docstring + strings + method addrs |
| X03 DTypeMutator is a separate `.so`/body sharing the bir capability | HIGH | shared `canMutateDstTy`/`mutateDstTy` vocabulary; cross-module |
