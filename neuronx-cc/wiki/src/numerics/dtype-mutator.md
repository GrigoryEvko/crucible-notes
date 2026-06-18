# Penguin DTypeMutator — dtype propagation by in-place dst-retype

> *All symbols, addresses, and source-line numbers on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The pass is a Cython 3.0.10 extension module: `neuronxcc/starfish/penguin/targets/transforms/DTypeMutator.cpython-310-x86_64-linux-gnu.so` (sha256 `d16b13a1…380743`, md5 `d658d73d…6aea6`, corpus prefix hash `47d005642b9e59a4`; 285 functions, 178 decompiled). Addresses are file/VA offsets into the module's `.text`. cp311/cp312 ship the same Python logic at different addresses; treat every address as version-pinned. "Source line `:N`" means the `DTypeMutator.py` line that Cython's `_Pyx_AddTraceback` markers (filename string `"neuronxcc/starfish/penguin/targets/transforms/DTypeMutator.py"` @ `_full.c:1294`) attribute the instruction to — these are CONFIRMED ground truth even though no `.py` ships.*

## Abstract

`DTypeMutator` is the **dtype-propagation** half of Penguin's numeric-legalization wave. Where **[AutoCastFP32](autocast-fp32.md)** (§9.4) *inserts* explicit cast ops at fp32 boundaries, `DTypeMutator` **moves a chosen dtype through the IR by retyping the destination of existing instructions in place** — and, for constant weights, *folds the cast into the weight bytes at compile time* so no runtime cast survives. The two passes are complementary: AutoCastFP32 creates the cast; DTypeMutator absorbs it into the producer (or into a free DMA-cast), and the downstream [cast-elimination pass](cast-elimination.md) (§9.6) deletes whatever is now redundant. The module docstring is exactly that: `"DTypeMutator - Mutate dtype for TongaISAInst."` (string pool, CONFIRMED).

Three facts make this page worth its weight. First, the entire "may I retype this op's dst?" decision is **one function** — `matchOrMutateDstTy` (`0x10ed0`, the module's largest body) — an **8-gate predicate** that returns a **3-valued** `MutateState` rather than a bool, so the propagation driver can distinguish *touched* (`Mutated`), *already-compatible* (`Match`), and *blocked* (`Mismatch`). Second, the headline name **`canMutateDstTy` is not a function at all** — it has no compiled body; it is a **boolean attribute read off the IR instruction** via a single `getattr` (`_full.c:11398`/`:1649`), the eighth and final gate. Third, the load path branches on **where the cast happens**: a constant ("weight") load is cast *once at build time* on its literal bytes (`staticCastLoadTensor`, guarded by `assert tensor.is_const, "Can only cast weight"`, the result **memoized in `tensor_map`**), while a runtime load needs the DMA engine to carry the cast — gated by the hardware bit **`enable_dma_cast`** (`mutateLoad`, `:152`). If `enable_dma_cast` is false, a runtime retype is simply refused.

| | |
|---|---|
| **Class** | `DTypeMutator(mutate_multi_users, f, target)` — `__init__` @ `0xe9a0` (`:41`) |
| **Core predicate** | `matchOrMutateDstTy(self, operand, dtype) -> MutateState` @ `0x10ed0` (`:81`) |
| **Bool wrapper** | `tryMutateDstTy(self, operand, dtype) -> bool` @ `0xdb20` (`:162`) — `return not result.mismatch` |
| **Load path** | `mutateLoad(self, load, dtype) -> MutateState` @ `0x185f0` (`:127`) |
| **Weight fold** | `staticCastLoadTensor(self, load)` @ pf `0x13c90` / pw `0x17b10` (`:54`) |
| **Result enum** | `MutateState{Mismatch, Mutated, Match}` — accessors @ `0x10150` / `0x10810` / `0xfa90` |
| **HW/op gates** | `allowed_dtypes` (op-legal set), `enable_dma_cast` (`target` bit), `mutate_multi_users` (ctor knob) |
| **Quirk** | `canMutateDstTy` = a property on the inst, **not** a compiled function (string-pool only) |

The low-level byte conversion this pass ultimately drives — `dt.static_cast` on weight bytes, `mutateTensorTy` retypes — bottoms out in [`bir::CastToNewDType`](cast-to-new-dtype.md) (Part 9.3, the canonical cast/saturate engine).

## Construction and lifecycle

`__init__` (`0xe9a0`, `:41`) is a flat four-attribute store sequence (CONFIRMED, direct `tp_setattro` chain `:42..:46`):

```c
// def __init__(self, mutate_multi_users, f, target):   // DTypeMutator.py:41
self.tensor_map        = {}                  // :42  PyDict_New -> setattr "tensor_map"
self.f                 = f                    // :43  the function / IR being mutated
self.mutate_multi_users = mutate_multi_users  // :44  bool gate (Section "Multi-user gate")
self.target            = target               // :46  hw target descriptor (queried for enable_dma_cast)
```

`tensor_map` is the **memo of already-cast weights**: a const tensor cast to a given dtype is built once and shared across all its loads (see `staticCastLoadTensor`). `set_function(f)` (`0xc9d0`, `:51`) re-points the pass at a new IR with a single `setattr "f"` — no state re-allocation. `reset()` (`0xd2f0`, `:48`) is `self.tensor_map.clear()` (CONFIRMED: getattr `tensor_map` → getattr `clear` → call), discarding the cast memo between runs. **Note** (CORRECTION below) that `allowed_dtypes` and `enable_dma_cast` are **not** set in `__init__` — they are read elsewhere off `operand` and `self.target` respectively.

> **CORRECTION (backing report §1).** The report says "`allowed_dtypes` and `enable_dma_cast` are NOT set in `__init__` — read as attributes elsewhere." Confirmed and sharpened by the per-function decompiles: `allowed_dtypes` is read off the **operand** (`v11`, `matchOrMutateDstTy_0x10ed0.c:1184`), and `enable_dma_cast` is reached via a `_GetModuleGlobalName`/getattr chain in **`mutateLoad`** (`mutateLoad_0x185f0.c:1280`), i.e. off `self.target`, not `self`. The receivers are the report's STRONG reading; the attribute *names* and *call sites* are CONFIRMED.

## `MutateState` — the three-valued result

`MutateState` is a small enum-like result type with three members. The per-member accessor methods are compiled at `MutateState_1mismatch` @ `0x10150`, `MutateState_5mutated` @ `0x10810`, `MutateState_3match_or_mutated` @ `0xfa90` (CONFIRMED in `_function_addresses.json`).

| Member | Meaning | Driver action |
|---|---|---|
| `Match` | the op already had / accepts the target dtype | propagation passes through, **no change** |
| `Mutated` | the op's dst dtype was changed in place | keep propagating; bump `operator_type_mutated`; a feeding cast may now be deletable (9.6) |
| `Mismatch` | dtype cannot/should not flow here | **stop** propagating through this edge; the inserted cast must stay |

The lowercase string members `mismatch` / `mutated` / `match_or_mutated` are the compiled accessor names; `tryMutateDstTy` reads `result.mismatch` directly. `match_or_mutated` is the convenience predicate `state in {Match, Mutated}` — "the dtype is satisfiable here." (INFERRED from caller usage; member objects and accessor addresses are CONFIRMED.)

`tryMutateDstTy` (`0xdb20`, `:162`) is the thin boolean adapter (CONFIRMED body): it calls the core predicate with **keyword** args and negates the mismatch flag:

```c
// def tryMutateDstTy(self, operand, dtype):     // DTypeMutator.py:162
result = self.matchOrMutateDstTy(operand=operand, dtype=dtype)  // :163 (PyDict_SetItem x2, keyword call)
return not result.mismatch                                       // :163/:164 -> True iff Match or Mutated
```

## `matchOrMutateDstTy` — the 8-gate predicate

This is the deliverable. `matchOrMutateDstTy` (`0x10ed0`, `:81`) walks an ordered conjunction of gates; it returns `Mismatch` at the *first* failing gate, `Match` on the early dtype-equality exit, and only performs the mutation + returns `Mutated` if **all** gates pass. The ordering below is the order the `getattr`/`isinstance`/`RichCompare` operations execute in the decompiled body. Every attribute/method **name** is CONFIRMED; the source-line numbers are CONFIRMED where Cython emits a *literal* `_Pyx_AddTraceback` marker (`:81`, `:82`, `:85`, `:86`, `:89`, `:117` are literal in `_full.c`) and STRONG-reconstructed where the line is carried in a runtime variable (`vNN`) rather than a literal — the report's `:95/:101/:104/:107/:110/:113/:120/:122/:124` fall in the latter class. The exact receiver of two reads (`operand` vs `self`) is STRONG where Cython temp aliasing (`v11`/`v37`/`obj`) obscures it.

```c
// def matchOrMutateDstTy(self, operand, dtype) -> MutateState:   // DTypeMutator.py:81

// ── GATE 1: dtype already equal → the "match" half of matchOrMutate ─────────
if (operand.dtype == dtype)                 // :82  RichCompare on getattr "dtype"
    return MutateState.Match;                // :83

// ── GATE 2: NeuronAP reinterpret rejected ─────────────────────────────────
if (isinstance(operand, NeuronAP))          // :85
    if (operand.reinterpret)                 // :86  getattr "reinterpret"   (AP aliases bits)
        return MutateState.Mismatch;          // :86

// ── producer-tensor branch (operand is NOT a NeuronInst) ──────────────────
if (!isinstance(operand, NeuronInst)) {     // :88
    if (operand.tensor.single_def_single_use) {            // :89
        store = single(TensorUtils.store_insts(operand.tensor)); // :89  unpack the sole producer
        // ── GATE 3: scalar/GEP producer → dtype must be in the op's legal set ─
        if (isinstance(store, TensorScalarGEPOp))           // :95
            if (dtype not in operand.allowed_dtypes)         // :95  PySequence_Contains
                return MutateState.Mismatch;                  // :96
    }
}

// ── INSTRUCTION (NeuronInst) branch ───────────────────────────────────────
dst = operand.dst;                          // :101 getattr "dst"
// ── GATE 4: dst tensor must feed exactly one load ─────────────────────────
if (dst == NULL || dst.tensor.n_loads != 1) // :101/:107  int-eq to __pyx_int_1
    return MutateState.Mismatch;             // :107/:108

// ── GATE 5: instruction reinterpret rejected ──────────────────────────────
if (operand.reinterpret)                    // :110 getattr "reinterpret"
    return MutateState.Mismatch;             // :111

// ── GATE 6: a state-buffer atomic load → hand off to the load-specialized path ─
if (isinstance(operand, SBAtomLoad))        // :113
    return self.mutateLoad(operand, dtype);  // :114  DMA-cast / const-fold path

// ── GATE 7: multi-user gate ───────────────────────────────────────────────
if (!operand.has_single_user)               // :104 getattr "has_single_user"
    if (!self.mutate_multi_users)            // :104 ctor knob off self
        return MutateState.Mismatch;          // :105

// ── GATE 8: per-op legality PROPERTY (NOT a function) ──────────────────────
if (!operand.canMutateDstTy)                // :117 getattr "canMutateDstTy" + PyObject_IsTrue
    return MutateState.Mismatch;             // :118

// ── all gates passed → perform the in-place mutation ──────────────────────
operand.dtype = dtype;                       // :120 setattr "dtype"
operand.dst.mutateTensorTy(dtype);           // :122 retype the dst tensor object
DTypeMutator.operator_type_mutated += 1;     // :124 class-level Statistics counter
return MutateState.Mutated;                  // :125
```

Gate-by-gate, with the decompiled evidence line in `matchOrMutateDstTy_0x10ed0.c`:

| # | Gate | Fails → | Source | Decompile evidence |
|---|---|---|---|---|
| 1 | `operand.dtype == dtype` | returns `Match` (not Mismatch) | `:82/:83` | RichCompare on getattr `dtype` |
| 2 | not (`NeuronAP` and `reinterpret`) | `Mismatch` | `:85/:86` | `isinstance NeuronAP`; getattr `reinterpret` `:2318` |
| 3 | `TensorScalarGEPOp` producer ⇒ `dtype in allowed_dtypes` | `Mismatch` | `:95/:96` | `TensorScalarGEPOp` `:1147/:1154`; `allowed_dtypes` on `v11` `:1184` |
| 4 | `dst.tensor.n_loads == 1` | `Mismatch` | `:101/:107` | getattr `n_loads` `:1426`, int-eq `__pyx_int_1` |
| 5 | not `operand.reinterpret` | `Mismatch` | `:110/:111` | getattr `reinterpret` `:1464` |
| 6 | `SBAtomLoad` ⇒ delegate to `mutateLoad` | (returns its `MutateState`) | `:113/:114` | `isinstance SBAtomLoad` `:2003/:2010` |
| 7 | `has_single_user` OR `mutate_multi_users` | `Mismatch` | `:104/:105` | `has_single_user` on operand `:2113`; `mutate_multi_users` on obj `:2142` |
| 8 | `operand.canMutateDstTy` | `Mismatch` | `:117/:118` | getattr on `v37` `:1649` (the property) |

The two `reinterpret` guards (gates 2 and 5) exist because a reinterpret op aliases the *same bits* under a different view — you cannot silently retype its dst without changing what those bits mean, so it must `Mismatch`. The mutation itself is two writes (`operand.dtype = dtype` `:120`; `operand.dst.mutateTensorTy(dtype)` `:122`) followed by the class-level statistic bump `operator_type_mutated += 1` (`:124`; read at `:1804`, write at `:1859`), which is the Statistics counter **"Number of operator whose dst type is mutated for legalization"** (CONFIRMED string pool).

> **The `canMutateDstTy` quirk (the task headline).** `canMutateDstTy` has **no standalone compiled function**: it appears in the string pool exactly once (string-tab slot 43, `_full.c:1036`) and is consumed exactly once — `_Pyx_PyObject_GetAttrStr(v37, …canMutateDstTy)` at `matchOrMutateDstTy_0x10ed0.c:1649`, immediately followed by `PyObject_IsTrue`. It is a **boolean property on the IR instruction object**, not a method this module calls. There is nothing to disassemble: the "can I mutate this op's dst?" legality lives on the node as a flag, computed by the op classes elsewhere. (CONFIRMED: string present in `_strings.json`; **absent** from `_function_addresses.json`.) The sibling helper names `mutateDstTy` / `mutateTensorTy` are likewise string-only here (invoked via `getattr`), not separately compiled in this module.

## `mutateLoad` — the load-specialized path (`enable_dma_cast`)

When gate 6 fires (`operand` is an `SBAtomLoad`), control transfers to `mutateLoad` (`0x185f0`, `:127`). This is where the **DMA-carries-the-cast vs. separate-cast** decision lives. The entry line `:127` is a literal `AddTraceback` marker (`mutateLoad_0x185f0.c:458/620`); the per-statement lines `:130..:160` below are reconstructed from the report's source map (the in-body tracebacks store their line in a runtime `vNN`, so e.g. `:130`/`:131` are STRONG, not literal — see the verification ceiling). Every *attribute/op* at each step is CONFIRMED by the decompile line cited:

```c
// def mutateLoad(self, load, dtype) -> MutateState:   // DTypeMutator.py:127

// ── partition-broadcast loads constrain the target dtype ──────────────────
if (load.has_partition_broadcast && dtype == np.float16)       // :130  getattr :629
    if (self.dtype == dt.bfloat16 || self.dtype == dt.float32r) // :131
        return MutateState.Mismatch;                             // :131

// ── fp8 e5m2 is an unsupported target combo for a load ─────────────────────
if (dtype == dt.float8_e5m2)                 // :136  RichCompare dt.float8_e5m2 :958
    return MutateState.Mismatch;              // :137

// ── CONSTANT (weight) load → const-fold the cast at build time ────────────
if (load.load_const) {                        // :141  getattr "load_const" :998
    if (load.src.reinterpret)                 // :143  bit-reinterpreted const can't recast
        return MutateState.Mismatch;           // :144
    if (load.tensor.dtype == self.dtype)      // :146  already target dtype
        return MutateState.Match;              // :147
    self.staticCastLoadTensor(dtype);          // :149  *** weight const-fold (memoized) ***
    return MutateState.Mutated;                // :150
}

// ── RUNTIME load → needs the HW DMA engine to carry the cast ──────────────
if (!self.target.enable_dma_cast)            // :152  getattr chain :1280  *** THE SWITCH ***
    return MutateState.Mismatch;              // :153  HW can't DMA-cast → refuse to propagate
if (load.dst)                                 // :155
    load.dst.mutateTensorTy(dtype);           // :156  retype dst in place (DMA carries the cast)
else
    self.dtype = dtype;                        // :158
return MutateState.Mutated;                   // :160
```

The `load_const` split (`:141`) is the heart of the pass:

- **Const (weight) load** → `staticCastLoadTensor` re-encodes the literal bytes *once at compile time* (`:149`). No runtime cast, no DMA-cast op. This is why a const-fold path needs no `enable_dma_cast` check.
- **Runtime load** → the dtype conversion must be performed by the DMA engine itself. **`enable_dma_cast` true** (`target` HW bit, read at `:1280`) ⇒ the DMA *carries* the conversion: just retype the dst tensor (`:156`) / set the dtype (`:158`) and return `Mutated` — **no separate cast op is emitted**. **`enable_dma_cast` false** ⇒ the hardware cannot fuse the cast into the load, so `mutateLoad` returns `Mismatch` (`:153`) and `matchOrMutateDstTy` refuses to propagate the dtype through that load. (CONFIRMED: `enable_dma_cast` getattr at `mutateLoad_0x185f0.c:1280`; `has_partition_broadcast` `:629`; `float8_e5m2` `:958`; `load_const` `:998`; `mutateTensorTy` `:1381`.) The load path bumps the Statistics counter `n_loads` — **"Number of load whose tensor type mutated"** (CONFIRMED string pool; note this stat name collides with the per-tensor `n_loads` attribute read at gate 4 — the description string disambiguates them, both distinct uses CONFIRMED present).

The `src.reinterpret` guard (`:143/:144`) mirrors gates 2/5: you cannot statically recast the bytes of a const whose source is a bit-reinterpretation.

## `staticCastLoadTensor` — the weight const-fold ("Can only cast weight")

`staticCastLoadTensor` (pf `0x13c90` / pw `0x17b10`, `:54`) is the compile-time weight-cast. Its first argument `load` is the **target Dtype** (the weight tensor itself is reached via `self.tensor`). The entry `:54` and `:67` are literal `AddTraceback` markers (`:67` at `staticCastLoadTensor_0x13c90.c:1389`); the weight-guard line `:56` and the other per-statement lines are STRONG-reconstructed (their tracebacks carry the line in a runtime `vNN`). The guard string, getattrs, and call sites are all CONFIRMED at the decompile lines cited:

```c
// def staticCastLoadTensor(self, load):       // DTypeMutator.py:54   (load == target Dtype)
tensor = self.tensor;                           // :55
assert tensor.is_const, "Can only cast weight"; // :56  *** THE GUARD ***  getattr is_const :561
self.dma_access.dtype = load;                   // :57  point the DMA access at the new dtype
self.mutateDstTy(load);                         // :58  push dst-type onto the load op

if (isinstance(tensor, SingleValueTensor)) {    // :61  splat / scalar const fast-path :869
    tensor.dtype = load;                        // :62  retype in place, done
    return;
}

// ── general weight tensor: build a casted COPY, memoized in tensor_map ─────
try {
    new_tensor = self.tensor_map[tensor];       // :63  reuse if already cast  (getattr :970/:2694)
} except KeyError {                             // :64
    new_name   = tensor.name + "_8"...;         // :67-:69  uniquely-named clone
    uid        = self.f.allocateId();           // :70/:71  fresh id  (allocateId :1941)
    new_tensor = clone(tensor, name=new_name);  // :73  clone :2314
    new_tensor.value = dt.static_cast(tensor.value, ...); // :73  *** compile-time byte cast *** :2413
    self.tensor_map[tensor] = new_tensor;       // :74  MEMOIZE
    DTypeMutator.casted_weights += tensor.size_in_bytes;  // :75  stat (read :2780 / size :2800 / write :2897)
}
self.dma_access.replaceTensor(new_tensor);      // :77  swap casted tensor into the DMA  :3015/:3026
DTypeMutator.folded_cast_load += 1;             // :79  stat  (read :3177 / write :3239)
return;
```

The guard `assert tensor.is_const, "Can only cast weight"` (`:56`) is the reason weights are special: a weight is a **compile-time constant**, so casting it = re-encoding its stored bytes once at build time (`dt.static_cast` on `tensor.value`, `:73`) and substituting the new tensor (`replaceTensor`, `:77`). A runtime/activation tensor has no compile-time bytes, so a static cast is forbidden — it would have to be a runtime DMA-cast instead (the `mutateLoad` `enable_dma_cast` path). The result is **memoized in `tensor_map`** (`:63`/`:74`) keyed by the original tensor, so the same weight cast to the same dtype is built once and shared across all its loads. The `SingleValueTensor` fast-path (`:61/:62`) skips the clone entirely — a splat const just flips its dtype in place. Two Statistics counters track the work: `casted_weights` = **"Number of bytes statically casted in weight tensor"** (`+= size_in_bytes`, `:75`), and `folded_cast_load` counts the number of folded load sites (`:79`). All getattr/clone/static_cast/replaceTensor call sites are CONFIRMED in `staticCastLoadTensor_0x13c90.c` at the lines cited above.

## How this fits the numeric-legalization wave

`DTypeMutator` sits between cast *insertion* and cast *elimination*:

1. A policy (the `--fp32-cast` / `auto_cast` / `auto_cast_type ∈ {bf16, fp16, fp32r, fp8e5}` knob) selects a target dtype. The four dtype literals `bfloat16`, `float16`, `float32r`, `float8_e5m2` are CONFIRMED in this module's string pool and are the dtypes it actually compares against (`mutateLoad` `:131/:136`).
2. **[AutoCastFP32](autocast-fp32.md)** (§9.4) may introduce explicit cast ops to realise that dtype at fp32 boundaries.
3. **DTypeMutator** (this pass) *propagates / absorbs* the dtype: `matchOrMutateDstTy` retypes op dsts in place, `mutateLoad` either folds a free DMA-cast or refuses, and `staticCastLoadTensor` const-folds weight casts — so an inserted cast is pushed back into its producer or into a free DMA-cast instead of executing at runtime.
4. **[Cast-elimination](cast-elimination.md)** (§9.6) is the downstream cleanup that deletes the casts this pass made redundant.

The three-way `MutateState` is exactly what such a propagation driver needs: `Mutated` (op retyped, keep going, maybe delete the feeding cast), `Match` (already compatible, pass through), `Mismatch` (blocked here, the inserted cast must stay). The two hardware/op gates `allowed_dtypes` (the op's legal dst set, gate 3) and `enable_dma_cast` (the target's DMA-cast capability, `mutateLoad`) bound how far a chosen dtype can flow. Everything this pass writes — `mutateTensorTy`, `dt.static_cast`, the DMA-cast — bottoms out in the single byte-conversion engine [`bir::CastToNewDType`](cast-to-new-dtype.md) (Part 9.3).

The pass registers its four counters through `register_stats` imported from `neuronxcc.starfish.penguin.Statistics` (CONFIRMED: `_Pyx_ImportFrom "register_stats"` in the module-exec init; both strings present in the pool).

## Verification ceiling

Re-verified directly against the cp310 module artifacts (not the backing report alone):

- **CONFIRMED** — all 11 method/accessor addresses match the report byte-for-byte (`matchOrMutateDstTy 0x10ed0`, `mutateLoad 0x185f0`, `staticCastLoadTensor 0x13c90/0x17b10`, `tryMutateDstTy 0xdb20`, `__init__ 0xe9a0`, `reset 0xd2f0`, `set_function 0xc9d0`, `MutateState.mismatch/mutated/match_or_mutated 0x10150/0x10810/0xfa90`) in `_function_addresses.json`.
- **CONFIRMED** — `canMutateDstTy` present as a single `.rodata` string-pool constant at `0x1c6b0` (`_full.c:1036`; referenced from code at `0x5711`), consumed by one `getattr` (`matchOrMutateDstTy_0x10ed0.c:1649` = `_full.c:11398`), and **absent** from the function table → it is a property, not a function.
- **CONFIRMED** — the module docstring `"DTypeMutator - Mutate dtype for TongaISAInst."` is a substring of the full Copyright docstring in the pool; all four dtype literals and all four Statistics description strings present in `_strings.json`.
- **CONFIRMED** — every gate's `getattr`/`isinstance`/`RichCompare` and every dtype literal located at the decompile lines cited; `enable_dma_cast` read at `mutateLoad_0x185f0.c:1280`; `"Can only cast weight"` guard + `is_const` at `staticCastLoadTensor_0x13c90.c:561`; `tensor_map` memo subscript at `:970/:2694`; `dt.static_cast` at `:2413`.
- **STRONG (not CONFIRMED)** — most per-statement Python source-line numbers (`:95`, `:101`, `:104`–`:160`, `:56`, etc.). Cython emits a *literal* line in `_Pyx_AddTraceback` only at a subset of sites — CONFIRMED literals in this module are `41, 48, 51, 162, 54, 67, 81, 82, 85, 86, 89, 117, 127`; every other line number carries through a runtime `vNN` variable and is reconstructed from the report's source map. The line *ordering* and the *attribute name* at each step are CONFIRMED.
- **STRONG (not CONFIRMED)** — the exact *receiver* of a few attribute reads (`allowed_dtypes` off `operand` vs `self`; `mutate_multi_users` off `self`) where Cython temp aliasing (`v11`/`v37`/`obj`) blurs the object. The attribute *names* and source *line numbers* are CONFIRMED via `AddTraceback`; only the receiver disambiguation is reasoned.
- **STRONG** — the `"_8"` infix in the clone-naming (`:67-:69`) and its `n_loads`-dependent arithmetic; the kp constant was not byte-recovered from the dumped pool, and Cython unicode-width temps obscure the precise string build. Structure (name + marker + `allocateId`) is CONFIRMED; the literal suffix is the report's STRONG reading, carried forward unverified here.
- **INFERRED** — the `Match`/`Mutated`/`Mismatch` *semantics* relative to a propagation driver, and the pipeline ordering vs AutoCastFP32 / cast-elimination (drawn from caller usage and sibling passes, not from this module's body).
