# Penguin AutoCastFP32 Cast-Insertion Engine

> *All addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22`, module `AutoCastFP32.cpython-310-x86_64-linux-gnu.so` (Cython 3.0.10). The cp311/cp312 wheels carry the same logic at shifted addresses. Source path embedded in the module: `neuronxcc/starfish/penguin/targets/tonga/passes/AutoCastFP32.py`.*

## Abstract

`AutoCastFP32` is the Penguin pass that **inserts** the casts which down-convert an fp32 program to a narrower arithmetic dtype. It is the *producer* in a three-stage numeric-lowering chain: this pass emits cast instructions and narrows tensor dtypes; a later DTypeMutator pass *moves* those casts to better positions; a cast-elimination pass *absorbs* the ones that become redundant. The module docstring (`@0x389a0`) states the canonical job in one line: *"A type legalization pass. At this point, the only conversion performed is fp32->bfloat16."* That docstring describes only the default; the pass actually targets one of four dtypes chosen at construction.

The pass is a `TargetLowering`-style statement walker built on the standard `beforeStmtTransform` / `transformInstruction` / `afterStmtTransform` dispatch. The whole pass is parameterized by **one field, `self.dtype`**, fixed in `__init__` from a single string knob `self.target.fp32_cast_mode`. That knob is the bare form of the `fp32-cast-<scope>-<dtype>` token reverse-marshalled by the frontend (see [Frontend Precision-Flag Marshalling](../frontend/precision-flag-marshalling.md)) — there is **no integer enum on this axis**; every gate is an interned-unicode equality test. Three pass classes share the module: `AutoCastFP32` (the base, casts interior arithmetic), `AutoCastInputs` (a lighter subclass that casts only at the I/O boundary), and `AutoCastTCInputs` (the matmul-operand subclass that delegates dtype choice to the target descriptor and then physically shrinks the backing tensors).

This page documents the *where-and-why of cast insertion*: the `fp32_cast_mode` gating cascade that picks `self.dtype`; `insertInputCasts`, which materializes the boundary cast; the `transformInstruction` skip-rules that decide which instructions are left alone; `castTCOperand`, which delegates the legal matmul-operand dtype to `target.getMatmultOperandType` and rewires; and `shrinkTensor`, the physical down-cast that re-lays-out an already-narrowed SBUF tensor at the smaller element width.

> **NOTE — Cython line-cookie caveat.** This module is Cython-compiled; the per-statement source-line cookies collapse toward each method's `def` line. Throughout this page, the **order** of attribute access / calls / comparisons is `CONFIRMED` (read directly from the decompiled `_Pyx_PyObject_GetAttrStr` / `PyObject_RichCompare` / `tp_setattro` targets), but interior source line numbers are `STRONG`, not exact. Surface Python syntax (loop/if nesting) is reconstruction consistent with that order, not byte-literal source.

For reimplementation, the contract is:

- **The gating field.** One `self.dtype` selected by a four-way string cascade on `target.fp32_cast_mode` (`all`→bf16, `all-fp32r`→fp32r + call bookkeeping, `all-fp8e4`→fp8e4, else→fp16).
- **Boundary insertion.** `insertInputCasts` clones the load at the narrow dtype, builds a `make_cast`, splices it at the block front, rewires every use except the cast, and erases the old load — toggled inline-vs-standalone by `--disable-inline-cast`.
- **The skip-rules.** `transformInstruction` leaves opaque/call ops and integer-typed paths untouched, keeps `SelectOp` predicates at `uint8`, and folds redundant casts.
- **The delegation.** `castTCOperand` never *chooses* a matmul-operand dtype; it asks `target.getMatmultOperandType(v.dtype)` and only materializes a cast when the answer differs.
- **The physical shrink.** `shrinkTensor` re-stores an already-narrowed tensor at the smaller width, behind a strict single-affine-loader safety predicate, and accumulates saved bytes in the class attribute `tensor_shrink`.

| | |
|---|---|
| **Module** | `neuronxcc.starfish.penguin.targets.tonga.passes.AutoCastFP32` |
| **Binary** | `AutoCastFP32.cpython-310-x86_64-linux-gnu.so` (Cython 3.0.10) |
| **Base pass** | `AutoCastFP32` (Cython type `12AutoCastFP32`) |
| **Subclasses** | `AutoCastInputs` (`14AutoCastInputs`), `AutoCastTCInputs` (`16AutoCastTCInputs`) |
| **Gating knob** | `self.target.fp32_cast_mode` — interned string `@0x38ef0` |
| **`__init__` (base)** | `sub_129D0` — the `fp32_cast_mode` cascade |
| **`insertInputCasts`** | `sub_16110` (3702 lines) — boundary materialization |
| **`transformInstruction`** | `sub_2B030` (1894 lines) — per-inst skip-rules |
| **`castTCOperand`** | `sub_F540` (5170 bytes) — matmul-operand delegation |
| **`shrinkTensor`** | `sub_1CB50` (3494 lines) — physical down-cast |
| **Cast dtype targets** | `bfloat16` `@0x392c0`, `float32r` `@0x392a0`, `float8e4` `@0x39290`, `fp16`/`float16` |
| **IR level** | Penguin IR (`neuronxcc.starfish.penguin.ir.ir`), pre-tensorizer |

---

## 1. The Gating Cascade — `self.dtype` from `fp32_cast_mode`

### Purpose

The entire pass casts every targeted fp32 value to **one** dtype, and that dtype is decided exactly once, in the constructor. There is no per-instruction dtype policy in the base pass — `transformInstruction`, `insertInputCasts`, and every `transform*Load/Store` read the same `self.dtype`. Getting this cascade wrong mis-targets the whole pass.

### Entry Point

```text
AutoCastFP32.__init__(self, ...)            sub_129D0
  └─ self.target.fp32_cast_mode             // GetAttr target @0x129d0+, then .fp32_cast_mode
       └─ cascade of PyObject_RichCompare    // interned-unicode equality, NO integer enum
            └─ tp_setattro self.dtype = <chosen dtype>
```

### Algorithm

```c
// AutoCastFP32.__init__ — sub_129D0  (order CONFIRMED; line numbers STRONG)
function AutoCastFP32_init(self):
    mode = self.target.fp32_cast_mode          // L480/490: GetAttr target, then n_s_fp32_cast_mode

    if mode == "all":                          // L502-503: __pyx_n_u_all, RichCompare ==
        self.dtype = bfloat16                   // L577/589: GetBuiltinName bfloat16, setattro self.dtype
    elif mode == "all-fp32r":                  // L666/678-679: __pyx_kp_u_all_fp32r ("all-fp32r" @0x391f0)
        self.dtype     = float32r               // L754: setattro self.dtype = float32r
        self.call_ins  = set()                  // L764/774: PySet_New, setattro self.call_ins
        self.call_outs = set()                  // L787/797: PySet_New, setattro self.call_outs
    elif mode == "all-fp8e4":                  // L916/928-929: __pyx_kp_u_all_fp8e4 ("all-fp8e4" @0x391e0)
        self.dtype = float8e4                    // L1001: GetBuiltinName float8e4, setattro self.dtype
    else:                                       // fall-through: the fp16 family
        self.dtype = fp16   // = float16         // L1081: GetBuiltinName fp16, setattro self.dtype
```

### Why the mode set is exactly these four

The pass only handles the **`all-*`** subset of the precision axis — the modes that ask to cast *every* fp32 op. The `matmult-*` modes do **not** drive the base pass broadly; they route through the TensorContract path (§4) so only matmul operands are cast. The three CONFIRMED comparison keys (`"all"`, `"all-fp32r"`, `"all-fp8e4"`) plus the fp16 fall-through are exactly the all-scope rows of the `fp32-cast-<scope>-<dtype>` grid that the frontend reverse-marshals.

> **QUIRK — `call_ins` / `call_outs` exist only in the `all-fp32r` branch.** Two empty sets are created (`PySet_New` at L764 / L787) *only* when `mode == "all-fp32r"`. `float32r` ("fp32 round / replicated") must survive a round-trip across call boundaries, so the pass remembers which call edges carry it. A reimplementation that allocates these sets unconditionally will track call edges it never needs; one that omits them in the fp32r branch will lose the round-trip and silently corrupt call I/O dtypes.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `AutoCastFP32.__init__` | `sub_129D0` | The four-way `fp32_cast_mode` cascade | CONFIRMED |
| `AutoCastInputs.__init__` | `sub_22170` | Subclass ctor (boundary-only pass) | CONFIRMED |
| `AutoCastTCInputs.__init__` | `sub_11B70` | Trivial — `super().__init__` only | CONFIRMED |

### Related Knobs

| Knob | Type | Source | Effect |
|---|---|---|---|
| `target.fp32_cast_mode` | interned string | `@0x38ef0` | Selects `self.dtype` (the four-way cascade above) |
| `disable_inline_cast` | `clOptBool` (`--disable-inline-cast`) | `@0x38b50` / `@0x38c30` | Standalone vs inline cast at the boundary (§2) |

---

## 2. Boundary Insertion — `insertInputCasts`

### Purpose

`insertInputCasts` is the engine that **materializes** the actual cast for an input tensor still in fp32 and rewires the tensor's users to the cast. It is the boundary half of cast insertion (the interior half is `transformInstruction`, §3).

### Entry Point

```text
beforeStmtTransform(self, f)                 sub_26BF0   // walks f.tensors, decides who needs casts
  └─ for fp32 INPUT tensor:
       self.insertInputCasts(tensor)         sub_16110   // materialize + rewire
            ├─ TensorUtils.load_insts(tensor)            // gather loads
            ├─ disable_inline_cast.value                 // L1239-1266: --disable-inline-cast gate
            ├─ clone(load); newload.dtype = self.dtype   // L2842: cloned narrow load
            ├─ make_cast(... self.dtype ...)             // L3106/3348
            ├─ with front_under_bb(...): ...             // L2956: splice at block front
            ├─ tensor.replaceAllUsesWithExcept(cast,...) // L2751
            └─ load.eraseFromParent()                    // L2795
```

### Algorithm

```c
// AutoCastFP32.insertInputCasts — sub_16110  (attr/call ORDER CONFIRMED; surface STRONG)
function insertInputCasts(self, tensor):
    loads = TensorUtils.load_insts(tensor)              // L~ .load_insts
    // classify loads: GenericLoad vs AffineLoad        // L1064: n_s_GenericLoad

    if disable_inline_cast.value:                       // L1239-1266: module global,
                                                        //   .value of the --disable-inline-cast clOptBool
        // emit a STANDALONE cast op (do NOT fold into the load)
        ...

    name = dtype2str(self.dtype) + tensor.name_str       // L1550/1679: readable cast-tensor name
    newload = clone(load)                                // L2842: clone the load
    newload.dtype = self.dtype                            //   at the narrowed dtype
    // rebuild affine access for the cloned load:
    AffineLoad(...); newload.access; loopNestFromShape    // L2432/2906

    cast = make_cast(... self.dtype ...)                  // L3106/3348: the bir cast op
    cast.allocateId()                                     // L1881/2535: fresh instr id
    cast.updateDebugLoc(load.dl)                          // L1523: carry source debug loc

    with front_under_bb(...):                             // L2956: context-mgr, splice at block front
        ...                                              //   (insert the narrow load + cast)

    tensor.replaceAllUsesWithExcept(cast, <the cast>)     // L2751: rewire users, exclude the cast itself
    load.eraseFromParent()                                // L2795: delete the original fp32 load
```

> **GOTCHA — `replaceAllUsesWithExcept` must exclude the freshly-built cast.** The cast *consumes* the value it is casting, so rewiring "all uses" of the fp32 tensor to the cast without an exception would rewire the cast's own input to its output, forming a self-cycle. The "Except" arg names the cast node so it is the one user not rewritten. The same exclusion pattern recurs in `castTCOperand` (§4) and the load transforms (§5).

> **NOTE — the inline-cast flag picks the *shape* of the emitted IR, not whether a cast happens.** `--disable-inline-cast` (string *"Do not inline cast input tensors."* `@0x386e0`) switches between folding the conversion into the load (the load itself produces the narrow dtype) and emitting a standalone `make_cast` after the load. The narrowing happens either way; only the instruction count and fusibility differ.

### Boundary dispatch — `beforeStmtTransform`

`insertInputCasts` is called from `beforeStmtTransform` (`sub_26BF0`), which runs once per statement/function before the per-instruction transforms and decides *who* gets a boundary cast:

```c
// AutoCastFP32.beforeStmtTransform — sub_26BF0  (ORDER CONFIRMED)
function beforeStmtTransform(self, f):
    for tensor in f.tensors:
        if tensor.is_reinterpreted: continue              // skip reinterpreted views
        for op in TensorUtils.load_insts(tensor):
            if isinstance(op, OpaqueOp):
                op.call_ins.add(tensor)                   // record opaque consumer
        for op in TensorUtils.store_insts(tensor):
            if isinstance(op, CallOpBase):
                op.call_outs.add(tensor)                  // record call producer
        if tensor.isInputOrOutput:
            mutate_fp32(tensor, self.dtype)               // narrow boundary tensor's declared dtype
        if tensor.isInput and tensor.dtype == np.float32:
            self.insertInputCasts(tensor)                 // splice the boundary cast
```

The opaque/call edge bookkeeping into `call_ins` / `call_outs` is what preserves the `float32r` round-trip across calls in `all-fp32r` mode (§1).

---

## 3. Interior Skip-Rules — `transformInstruction`

### Purpose

`transformInstruction` is the generic per-instruction dispatch. Its first job is to decide **which instructions to leave alone**. Casting an op that should not be cast (an opaque/call op whose I/O is handled at the boundary, an integer-typed op, a select predicate) is a correctness bug, so the skip-rules come first and are pinned exactly.

### Algorithm

```c
// AutoCastFP32.transformInstruction — sub_2B030  (ORDER CONFIRMED; line numbers STRONG)
function transformInstruction(self, inst):
    // SKIP-RULE 1: opaque / call ops are NOT transformed (boundary handles their I/O)
    if isinstance(inst, OpaqueOp) or isinstance(inst, CallOpBase):   // L597-611: two PyObject_IsInstance, OR'd
        return                                                        //   if (IsInstance) -> early return

    mutate_result_and_scalar_operands(inst, self.dtype)              // L625-629: narrow result + scalar operands

    // SKIP-RULE 2: integer-typed paths short-circuit (only float ops cast)
    if np.issubdtype(inst.dtype, np.integer):                        // L812/860: n_s_issubdtype, n_s_integer
        ...                                                          //   integer guard

    // SKIP-RULE 3: a SelectOp keeps its predicate at uint8 (do not narrow the boolean mask to fp)
    if isinstance(inst, SelectOp):                                   // L1120-1139
        ... keep predicate as np.uint8 ...                           // L1204: n_s_uint8

    // SKIP-RULE 4: a GenericStore re-checks the integer guard before narrowing
    if isinstance(inst, GenericStore) and not np.issubdtype(inst.dtype, np.integer):  // L1296-1305 + L1351/1396
        ...

    // UnaryOp: cast tensor operands (and fold redundant casts, see §5)
    if isinstance(inst, UnaryOp):                                    // L1474
        ...
```

### The skip-rules, pinned

| # | Rule | Evidence | Why |
|---|---|---|---|
| 1 | `OpaqueOp` / `CallOpBase` → return | `PyObject_IsInstance` L602/604, `if(IsInstance)` L611 | Their I/O is cast at the boundary; transforming them double-casts |
| 2 | `np.issubdtype(inst.dtype, np.integer)` → skip | `n_s_issubdtype` L812, `n_s_integer` L860 | Only float ops are subject to fp32→narrow legalization |
| 3 | `SelectOp` predicate kept `uint8` | `n_s_SelectOp` L1120, `n_s_uint8` L1204 | The boolean mask is not a numeric value; narrowing it to fp is wrong |
| 4 | `GenericStore` re-checks integer guard | `n_s_GenericStore` L1296, `issubdtype`/`integer` L1351/1396 | A store of an integer value must not be narrowed |

> **QUIRK — opaque/call ops are skipped here *and* recorded at the boundary.** The skip in `transformInstruction` is not "ignore these ops"; it is "their casts are owned by `beforeStmtTransform`, which already pushed the relevant tensors into `call_ins` / `call_outs`." A reimplementation that skips opaque/call ops *without* the boundary bookkeeping will leave fp32 values crossing call edges uncast.

### The leaf rewriters

`transformInstruction` delegates the actual mutation to two module-level helpers:

```c
// mutate_fp32(v, dtype) — sub_1AA80  : the leaf in-place dtype rewriter
function mutate_fp32(v, dtype):
    if v.dtype == np.float32:        // RichCompare vs np.float32
        v.dtype = dtype             // setattro: the in-place mutation
    elif v.dtype == np.float16:      // further float-family branches (float32r/bfloat16/float8e4)
        ...

// mutate_result_and_scalar_operands(inst, dtype) — sub_2FBE0
function mutate_result_and_scalar_operands(inst, dtype):
    mutate_fp32(inst.result, dtype)                  // narrow the RESULT in place
    for operand in inst.operands:
        if isinstance(operand, ScalarValue):         // n_s_ScalarValue
            mutate_fp32(operand, dtype)              // scalars ride along for free (in-place)
        else:
            cast = make_cast(... dtype ...)          // n_s_make_cast: tensor operands cost a cast node
            cast.allocateId(); cast.updateDebugLoc(inst.dl)
            inst.replaceUseOfWith(operand, cast)
```

> **NOTE — scalars are narrowed in place; tensors get a spliced cast.** This asymmetry is the cost model of the pass: an immediate scalar operand simply has its `dtype` rewritten (`mutate_fp32`), while a tensor operand needs a real `make_cast` node so the bytes are actually converted. A reimplementation that inserts a cast for scalars wastes IR; one that mutates tensor dtypes in place corrupts the data.

---

## 4. Matmul-Operand Delegation — `castTCOperand`

### Purpose

For a `TensorContractOp` (matmul), the legal operand dtype is **arch-specific** and is *not* decided by this pass. `AutoCastTCInputs.castTCOperand` is pure mechanism: ask the target descriptor what dtype the operand must be, and if the current dtype differs, build a cast and rewire. This is the subclass used by the `matmult-*` modes.

### Entry Point

```text
transformTensorContractOp(self, tc)          sub_20C00
  ├─ stationary, moving = tc.operands         // exactly two operands, positional roles
  ├─ builder = IRBuilder(function=tc.function)
  ├─ changed  = self.castTCOperand(stationary, builder=builder)   sub_F540
  └─ changed |= self.castTCOperand(moving,     builder=builder)   // PyNumber_InPlaceOr
```

### Algorithm

```c
// AutoCastTCInputs.castTCOperand — sub_F540  (ORDER CONFIRMED; line refs into the decompile)
function castTCOperand(self, v, builder):
    target = self.target.getMatmultOperandType(v.dtype)   // L652: self.target; L662: .getMatmultOperandType;
                                                          // L677: v.dtype; call with v.dtype as arg
    if v.dtype == target:                                 // L725: RichCompare ==; L747-749: IsTrue
        return False                                      // L772: return Py_False (already legal — no cast)

    cast = builder.cast_to(v ... target)                  // L777: builder.cast_to, called with v
    v.replaceAllUsesWithExcept(cast, <the cast>)          // L824: rewire users, exclude the cast
    return True                                           // L878: return Py_True (changed)
```

```c
// AutoCastTCInputs.transformTensorContractOp — sub_20C00
function transformTensorContractOp(self, tc):
    stationary, moving = tc.operands              // exactly two operands (len==2 fast path)
    builder = IRBuilder(function=tc.function)
    changed  = self.castTCOperand(stationary, builder=builder)   // operands[0]
    changed |= self.castTCOperand(moving,     builder=builder)   // operands[1]; |= accumulate
    return changed
```

### Why delegation, and why both operands take the same path

`castTCOperand` does **not** choose a dtype. It calls `self.target.getMatmultOperandType(v.dtype)` and lets the Tonga arch descriptor (gen2..gen4 override it) return the legal operand dtype for *that operand's current dtype*. The pass treats the two operands asymmetrically only by **position** — `operands[0]` is the stationary, `operands[1]` the moving — but both go through the identical `castTCOperand`. Stationary and moving can therefore land on *different* legal dtypes only because the target descriptor returns different answers for their respective input dtypes, never because this pass distinguishes the roles.

> **QUIRK — the matmul operand-dtype policy lives in the target, not in this pass.** A reimplementation that hard-codes "stationary = bf16, moving = fp8" inside `castTCOperand` will be wrong on any arch whose `getMatmultOperandType` returns otherwise. The single source of truth for the legal matmul-operand dtype is `target.getMatmultOperandType(v.dtype)`; `castTCOperand` is the cast-and-rewire shell around it.

> **NOTE — exact `cast_to` arg shape is partly obscured.** The decompiler shows `builder.cast_to` (L777) called with `v` as a positional arg; the destination dtype `target` is threaded through the surrounding frame and whether it reaches `cast_to` as a positional or keyword arg is **not byte-recoverable** here (`INFERRED`). The `bir::CastToNewDType` machinery itself is owned by a sibling page.

---

## 5. Load / Store Insertion and Cast Folding

The interior loads and stores have their own cast-insertion paths, symmetric in role: a **load casts its result**, a **store casts its source**. A redundant `UnaryOp` cast is folded away.

### Algorithm

```c
// AutoCastFP32.transformAffineLoad — sub_2D590  (transformGenericLoad sub_140D0 is near-identical)
function transformAffineLoad(self, inst):
    if inst.is_reinterpreted: return
    if inst.tensor.isInputOrOutput: ... record call_ins/call_outs ...
    if inst.dtype == np.float32:                     // RichCompare vs np.float32
        mutate_fp32(inst, self.dtype)                // narrow the load result dtype
        cast = make_cast(... self.dtype ...)
        cast.allocateId(); cast.updateDebugLoc(inst.dl)
        cast.insertAfter(inst)                       // load casts its RESULT (cast placed after the load)
        inst.tensor.replaceAllUsesWithExcept(cast, cast)

// AutoCastFP32.transformAffineStore — sub_32960
function transformAffineStore(self, inst):
    if inst.is_reinterpreted: return
    if inst.dst.isOutput: ... handle output dtype (mutate_fp32 / call_outs) ...
    if inst.dtype == np.float32:
        cast = make_cast(inst.src -> self.dtype)     // store casts its SOURCE
        cast.allocateId()
        inst.replaceUseOfWith(inst.src, cast)        // rewire the stored value through the cast
        mutate_fp32(inst, self.dtype)                // narrow the dst dtype

// AutoCastFP32.transformUnaryOp — sub_24EF0  : fold redundant casts
function transformUnaryOp(self, op):
    if op.is_cast:                                   // already a cast op
        // a redundant fp32->X cast whose source is already narrow / input:
        op.replaceAllUsesWith(op.data)               // bypass the cast
        op.eraseFromParent()                         //   and delete it
    else:
        mutate_result_and_scalar_operands(op, self.dtype)
```

> **QUIRK — loads and stores cast opposite ends.** A load produces a value, so its cast is placed *after* it (`insertAfter`) and rewires the tensor's users (`replaceAllUsesWithExcept`). A store consumes a value, so its cast is placed on the *source* and rewires the store's use (`replaceUseOfWith(src, cast)`). Swapping the two breaks dataflow direction.

> **GOTCHA — `transformUnaryOp` is where double-casts are avoided.** Because boundary insertion and interior insertion both emit casts, a value can acquire an fp32→narrow cast whose input is already narrow. `transformUnaryOp` detects `op.is_cast` and folds the redundant cast (`replaceAllUsesWith(op.data)` + `eraseFromParent`). This is the *first* line of cast cleanup; the heavier movement and elimination happen in the downstream passes (§7).

---

## 6. Physical Down-Cast — `shrinkTensor`

### Purpose

After matmul operands have been logically down-cast (§4), the SBUF tensor that physically backs an operand can be re-stored at the **narrower element width** to save bytes — but only if it is safe to rewrite every producer and consumer. `shrinkTensor` is that decision. Its docstring (`@0x386a0`) is exact: *"Tensor shrink for matmult down-cast (in bytes)."* It does **not** choose an arithmetic dtype (that was decided upstream by `getMatmultOperandType`); it decides whether the physical layout can follow.

### Entry Point

```text
AutoCastTCInputs.afterStmtTransform(self, f)   sub_1BBE0
  ├─ eager_any(map(self.shrinkTensor, f.ordered_all_tensors))   // visit EVERY tensor
  └─ f.dropDeadTensors()                                        // clean up after narrowing
```

`afterStmtTransform` sweeps every tensor through `shrinkTensor` eagerly (via `eager_any`, so all are visited, not short-circuited), then drops dead tensors. This is the second half of the matmul down-cast: cast operands first (`transformTensorContractOp`), then narrow the physical tensors.

### Algorithm

```c
// AutoCastTCInputs.shrinkTensor — sub_1CB50  (guard ORDER CONFIRMED; rewrite block STRONG)
function shrinkTensor(self, tensor):
    // GUARD: bail if any load/store is opaque or a call (cannot safely rewrite)
    for op in TensorUtils.load_insts(tensor):           // L719: .load_insts
        if isinstance(op, (OpaqueOp, CallOpBase)):       // L933/977
            return False
    for op in TensorUtils.store_insts(tensor):          // L1108: .store_insts
        if isinstance(op, (OpaqueOp, CallOpBase)):       // L1262/1295
            return False

    if not tensor.are_all_dim_affine:  return False      // L1415: non-affine access -> unsafe
    if tensor.n_loads != <expected>:   return False      // L1473: load-count pattern mismatch
    if tensor.isInputOrOutput:         return False      // L1591: boundary tensor cannot shrink
    if tensor.is_const:                return False      // L1637: constant tensor cannot shrink

    narrow_dt = AffineLoad.truncated_dtype               // L1722: the narrowed dtype class attr
    loaders = frozenset(map(<...>, TensorUtils.load_insts(tensor)))  // L1783: dedup loaders
    if len(loaders) > 1:               return False      //   more than one distinct loader -> bail
    (ld,) = loaders                                      //   exactly one loader expected
    if ld.dtype == narrow_dt:          return False      //   already narrow -> nothing to do
    if ld.is_reinterpreted:            return False      // L2142: reinterpreted view -> unsafe

    // SAFE: perform the physical narrowing
    AutoCastTCInputs.tensor_shrink += tensor.size_in_bytes   // L2225/2255/2272/2345:
                                                            //   GetAttr class attr tensor_shrink,
                                                            //   InPlaceAdd size_in_bytes, SetAttr back
    builder = IRBuilder(function=tensor.parent_as_function)
    new = builder.<make narrowed load/cast>(...)
    new.updateDebugLoc(ld.dl)
    new.insert_before = ld
    ld.replaceUseOfWith(ld.src, new)   // or tensor.replaceAllUsesWith(new)
    recursivelyDCE(<dead producers>)   // L3375: clean up after rewiring
    return True
```

### The safety predicate

`shrinkTensor` is a chain of guard clauses that each `return False` when rewriting is unsafe. A tensor is shrinkable **only** if all of:

| Guard | Evidence | Meaning |
|---|---|---|
| no opaque/call loader | `OpaqueOp`/`CallOpBase` L933/977 | opaque consumers can't be safely rewritten |
| no opaque/call storer | `OpaqueOp`/`CallOpBase` L1262/1295 | opaque producers can't be safely rewritten |
| `are_all_dim_affine` | L1415 | non-affine access defeats the rewrite |
| `n_loads == expected` | L1473 | load pattern must match |
| not `isInputOrOutput` | L1591 | boundary tensors are fixed-layout |
| not `is_const` | L1637 | constants are fixed-layout |
| exactly one loader | `frozenset` L1783, `len > 1` bail | a single affine loader to rewrite |
| not already narrow | `ld.dtype == truncated_dtype` | nothing to shrink |
| not `is_reinterpreted` | L2142 | reinterpreted views alias other layouts |

> **NOTE — `tensor_shrink` is a *class* attribute, not an instance field.** The running byte counter is `GetAttr`'d off the `AutoCastTCInputs` class object, `InPlaceAdd`'d with `tensor.size_in_bytes` (L2272), and `SetAttr`'d back onto the *class* (L2345). It accumulates the cumulative bytes saved by down-cast across all tensors — the "(in bytes)" in the docstring. A reimplementation that makes it per-instance loses the cumulative total.

> **CORRECTION (X02-1) — backing report Section 3 truncated the docstring.** The backing note transcribes the shrink docstring as *"Tensor shrink for matmult down-cast (in bytes)"* with a closing paren; the interned string `@0x386a0` is actually *"Tensor shrink for matmult down-cast (in bytes"* — the trailing `)` is not part of the stored literal. Immaterial to the algorithm, noted for byte-exactness.

---

## 7. End-to-End Algorithm

```text
construction:  AutoCastFP32(target).__init__               sub_129D0
                 self.dtype <- fp32_cast_mode cascade
                   "all" -> bfloat16
                   "all-fp32r" -> float32r (+ call_ins/call_outs)
                   "all-fp8e4" -> float8e4
                   else -> fp16
                                   │
per statement: beforeStmtTransform  sub_26BF0
                 walk f.tensors:
                   record opaque/call edges -> call_ins/call_outs
                   IO tensors -> mutate_fp32(self.dtype)
                   fp32 INPUT tensors -> insertInputCasts   sub_16110
                                   │                          (clone narrow load + make_cast
                                   │                           + front_under_bb + rewire + erase)
per instruction: transformInstruction  sub_2B030
                   skip opaque/call, skip integer paths
                   keep SelectOp predicate uint8
                   mutate_result_and_scalar_operands         sub_2FBE0
                     (scalars in-place; tensors get a cast)
                   loads cast RESULT / stores cast SOURCE     sub_2D590 / sub_32960
                   UnaryOp folds redundant casts              sub_24EF0
                                   │
MATMUL path:   transformTensorContractOp  sub_20C00   (AutoCastTCInputs)
                 (stationary, moving) = tc.operands
                 castTCOperand each                            sub_F540
                   target.getMatmultOperandType(v.dtype)   <- dtype DECISION
                   if differs: builder.cast_to + rewire
                                   │
after stmt:    afterStmtTransform  sub_1BBE0
                 eager_any(map(shrinkTensor, ordered_all_tensors))  sub_1CB50
                   physical down-cast behind safety predicate
                   AutoCastTCInputs.tensor_shrink += bytes
                 f.dropDeadTensors()
                                   │
                                   ▼
              DTypeMutator (moves casts) -> cast-elimination (absorbs casts)
```

The pass is the **producer** of casts. Downstream, a `DTypeMutator` pass moves the casts emitted here to better positions, and a cast-elimination pass absorbs the ones that become redundant. Those two passes are documented separately in Part 9 (Numeric Semantics) and are out of scope for this page.

---

## 8. The Subclasses

| Class | Cython code | Role | Distinguishing methods |
|---|---|---|---|
| `AutoCastFP32` | `12AutoCastFP32` | Base — casts interior arithmetic + boundary | `insertInputCasts`, `transformInstruction`, `transformUnaryOp`, `transform{Affine,Generic}Load`, `transformAffineStore` |
| `AutoCastInputs` | `14AutoCastInputs` | Boundary-only — casts inputs, leaves interior alone | `beforeStmtTransform` (`sub_23C20`), `transformAffineLoad` (`sub_29670`), `transformOffloadedMemCpy` (`sub_22BB0`) |
| `AutoCastTCInputs` | `16AutoCastTCInputs` | Matmul-operand — delegate dtype + physical shrink | `transformTensorContractOp`, `castTCOperand`, `shrinkTensor`, `afterStmtTransform` |

`AutoCastInputs` is the lighter pass: its `beforeStmtTransform` (`sub_23C20`) narrows only `isInput && dtype == np.float32` tensors via `mutate_fp32`, its `transformAffineLoad` reuses the base load-cast mechanism gated only on `tensor.isInput`, and `transformOffloadedMemCpy` (`sub_22BB0`) narrows an offloaded-DMA memcpy whose source is an input fp32 tensor so DMA'd inputs land narrowed.

---

## Related Components

| Name | Relationship |
|---|---|
| `target.getMatmultOperandType` | Owns the per-operand legal matmul dtype that `castTCOperand` delegates to |
| DTypeMutator (Part 9) | Consumes this pass's casts and *moves* them to better positions |
| Cast elimination (Part 9) | *Absorbs* the casts that become redundant after movement |
| Frontend precision marshalling | Produces the `fp32-cast-<scope>-<dtype>` token whose bare form is `fp32_cast_mode` |

---

## Cross-References

- [Frontend Precision-Flag Marshalling](../frontend/precision-flag-marshalling.md) — the `--auto-cast` / `--fp32-cast` surface that produces the `fp32_cast_mode` value this pass gates on; the *string-is-the-value* convention with no integer enum.
- [DTypeMutator](dtype-mutator.md) (§9.5) — the pass that moves the casts inserted here.
- [Cast elimination](cast-elimination.md) (§9.6) — the pass that absorbs the redundant casts this pass and DTypeMutator leave behind.
