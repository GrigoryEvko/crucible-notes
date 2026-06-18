# Sharding Algebra

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 — the `hlo-opt` tool ELF (cp310 wheel; BuildID xxHash `93dd8bd9bd4c697b`, 231 MB, not stripped). `hlo-opt` is **VA-shifted**: `.text` VA = file offset + `0x201000`, `.rodata` VA = file offset + `0x200000`. Every address below is a virtual address. Other wheels differ; treat each address as version-pinned.*

## Abstract

Neuron's SPMD sharding propagation is built on **OpenXLA Shardy** (`mlir::sdy`), linked verbatim into `hlo-opt`. Shardy does not reason about tensor shardings directly; it reasons about an intermediate **factor space**. Every op is reduced to an `OpShardingRuleAttr` — a list of abstract *factors* (each with a global size and a type) plus, per operand and per result, a map from tensor dimension to the factor(s) that compose it. A concrete `TensorShardingAttr` (which mesh axes shard which dims) is then **projected** onto that factor space, propagation runs entirely on factors, and the result is projected back. This is the same trick LLVM's SCEV uses for loop induction or that polyhedral schedulers use for iteration domains: solve the problem in a normalized algebra, then map back.

The factor abstraction is what makes a contracting dimension of a `dot` behave correctly: the contracting factor appears in both LHS and RHS operands but **not** in the result, so it is tagged `kReduction`, and propagation refuses to push it onto the result (a sharded contracting dim produces a partial sum, not a sharded result). Batch dims and both free-dim groups are `kPassThrough` and propagate freely. The same four-valued `FactorType` enum (`kPassThrough=0`, `kReduction=1`, `kNeedReplication=2`, `kPermutation=3`) classifies every op's dims — concat dims become `kNeedReplication`, reverse dims become blocked, gather index dims become `kNeedReplication`/blocked, and so on.

This page documents three things a reimplementer must reproduce: **(1)** `createOpShardingRule` — the 26-opcode `TypeSwitch` dispatch that builds the rule, and the per-opcode factor algebra; **(2)** `ShardingProjection` — the forward map (sharding → per-factor `FactorSharding`) and inverse map (`createTensorShardingAttr`, factors → sharding), including the sub-axis splitting that distributes one mesh axis across several factors; **(3)** `FactorSharding` — the per-factor state and the conflict-resolution rules (greatest-common-prefix, no-two-factors-share-an-axis) that decide which axes a factor may take. The propagation *driver* (basic vs aggressive, direction, the fixed-point loop) is documented in [13.2 Sharding Propagation](sharding-propagation.md); this page is the algebra those passes operate on.

> **NOTE — provenance.** Everything on this page is **stock OpenXLA Shardy / StableHLO** (`mlir::sdy`, `xla::sdy`), statically linked. None of `OpShardingRuleAttr`, `ShardingProjection`, `FactorSharding`, `FactorType`, or `createOpShardingRule` is Neuron-authored. Neuron contributes only **downstream consumers** — `neuron::GetTpReplicaGroup`, the collective-rewrite passes — that read the *resulting* `HloSharding` after this machinery runs (see [§ Stock vs Neuron](#stock-shardy-vs-neuron-consumers)). Provenance D-AB05.

For reimplementation, the contract is:

- The **rule data structure** `OpShardingRuleAttr`: `factorSizes[]`, per-operand / per-result dim→factor mappings, and the three mutually-exclusive typed factor-index lists (`reduction` / `needReplication` / `permutation`) plus the orthogonal `blockedPropagation` list, with their in-memory offsets.
- The **`createOpShardingRule` dispatch**: 26 per-opcode lambdas driving an `OpShardingRuleBuilder`, falling through to `buildPointwise` for elementwise ops; the dot/reduce/gather/concat/reshape factor algebra.
- The **projection**: forward (`build` → per-tensor `DenseMap<long, FactorSharding>`), the sub-axis distribution of mesh axes across factors, and inverse (`createTensorShardingAttr`).
- The **conflict-resolution primitives** on `FactorSharding`: greatest-common-prefix across tensors, prefix-without-overlap (each mesh axis used by at most one factor), and how `FactorType` gates propagation.

| | |
|---|---|
| **Master dispatch** | `mlir::sdy::createOpShardingRule(Operation*, bool)` @ `0x2c438f0` |
| **Rule type** | `OpShardingRuleAttr` — `factorSizes[]` + per-tensor `TensorMappingAttr` + 4 factor-index lists |
| **Factor classifier** | `OpShardingRuleAttr::getFactorType(long)` @ `0x2c71550` |
| **`FactorType` enum** | `kPassThrough=0`, `kReduction=1`, `kNeedReplication=2`, `kPermutation=3` (disasm-confirmed) |
| **Forward projection** | `ShardingProjection::build(...)` @ `0x2c656c0` |
| **Inverse projection** | `TensorFactorShardings::createTensorShardingAttr(...)` @ `0x2c64200` |
| **Per-factor state** | `FactorSharding` — `DenseMap<long, FactorSharding>` entry stride `0x90` (144 B) |
| **Opcode count** | 26 distinct StableHLO opcode lambdas (symbol-confirmed) |
| **Terminal lowering** | `xla::sdy::convertToHloSharding(...)` @ `0x2bc58f0` → `HloSharding` (see [13.4](hlo-sharding.md)) |

---

## The factor model

Before any algorithm, fix the vocabulary. A **factor** is an abstract degree of freedom of an op, identified by a small integer index. Each factor has a **size** (`factorSizes[f]`, the global extent of that axis of iteration) and a **type** (`FactorType`). Every operand and every result of the op carries a `TensorMappingAttr`: a per-dimension list of the factor indices that compose that dimension. In the common case one dim maps to exactly one factor; reshape-style merge/split is the exception where a dim maps to several (gated by `hasDimensionsWithMultipleFactors()`).

The point of factors is that two *different* tensor dimensions on different operands that must be sharded *consistently* share the **same factor index**. For a `dot`, the LHS contracting dim and the RHS contracting dim are the *same* contracting factor; sharding that factor means sharding both operands along it. The result does not contain that factor at all — which is exactly why it is a reduction.

```text
  dot: [M,K] x [K,N] -> [M,N]      mesh axes {x, y}

  factors:   f0=M (size M, kPassThrough)
             f1=K (size K, kReduction)      <-- in lhs+rhs, NOT in result
             f2=N (size N, kPassThrough)

  operand0 (lhs) mapping: dim0 -> {f0}, dim1 -> {f1}
  operand1 (rhs) mapping: dim0 -> {f1}, dim1 -> {f2}
  result0       mapping: dim0 -> {f0}, dim1 -> {f2}

  project a concrete sharding {lhs: dim0->x}  onto factors:
     FactorSharding[f0] = {axisRefs:[x]}   (present in lhs and result)
     FactorSharding[f1] = {}               (K unsharded)
     FactorSharding[f2] = {}
  propagation grows f0 across tensors that contain f0 (lhs, result),
  never grows f1 onto the result (kReduction), and finally inverts
  back to per-tensor TensorShardingAttrs.
```

`FactorType` is the four-valued classifier that drives the "never grow f1 onto the result" decision and its siblings:

| Value | Name | Meaning | Set by (examples) |
|---|---|---|---|
| `0` | `kPassThrough` | Propagates freely operand↔result. The default — any factor not in a typed list. | dot batch/free dims, transpose, elementwise |
| `1` | `kReduction` | Present in inputs but reduced away; **not** pushed to results. | dot contracting dims, `reduce` reduced dims |
| `2` | `kNeedReplication` | Must stay replicated; blocks axis assignment. | concat dim, gather index/collapsed dims, broadcast-introduced dims |
| `3` | `kPermutation` | May be sharded but requires permutation-consistency before a collective is inserted. | collective axis dims (all-to-all etc.) |

> **GOTCHA — the typed lists are mutually exclusive, but "blocked" is orthogonal.** A factor is `kReduction`/`kNeedReplication`/`kPermutation` only if its index appears in the corresponding list; otherwise it is `kPassThrough`. `blockedPropagationFactors` is a *separate* list — a factor can be `kPassThrough` **and** blocked (e.g. a reversed dim). `isBlockedPropagationFactor()` probes it independently; it never contributes a `FactorType`. The binary carries the invariant string verbatim: *"a factor can only be in one of the reduction, need replication, or permutation factor sets"* (`.rodata`, confirmed).

---

## `OpShardingRuleAttr` — the rule data structure

### Purpose

`OpShardingRuleAttr` is the immutable, op-agnostic description of how an op's tensors decompose into factors. It is what `createOpShardingRule` returns and what `ShardingProjection::build` consumes. Stock `mlir::sdy`.

### Storage layout

Every field is an `ArrayRef<T>` stored as a `{begin_ptr, end_ptr}` pair. Offsets recovered from the accessor disasm (`get*Factors`/`get*Mappings` @ `0x2c72960`..); each accessor returns the pair at its offset.

| Field | begin / end offset | Type | Meaning |
|---|---|---|---|
| (header) | `0x00` | — | attr storage header / TypeID |
| `factorSizes` | `0x08` / `0x10` | `ArrayRef<long>` | global size of each factor; index = factor id |
| `operandMappings` | `0x18` / `0x20` | `ArrayRef<TensorMappingAttr>` | per-operand dim→factors |
| `resultMappings` | `0x28` / `0x30` | `ArrayRef<TensorMappingAttr>` | per-result dim→factors |
| `reductionFactors` | `0x38` / `0x40` | `ArrayRef<long>` | factor indices of type `kReduction` |
| `needReplicationFactors` | `0x48` / `0x50` | `ArrayRef<long>` | factor indices of type `kNeedReplication` |
| `permutationFactors` | `0x58` / `0x60` | `ArrayRef<long>` | factor indices of type `kPermutation` |
| `blockedPropagationFactors` | `0x68` / `0x70` | `ArrayRef<long>` | orthogonal "do-not-propagate" flag set |

`TensorMappingAttr` is itself a list of `DimMappingAttr`; each `DimMappingAttr::getFactorIndices()` returns the factor set composing one dimension.

### Algorithm — `getFactorType`

The four enum values are recovered directly from the `getFactorType` body @ `0x2c71550` (33 instructions). It probes the three typed lists in order with `llvm::is_contained` and returns the first match:

```c
FactorType OpShardingRuleAttr::getFactorType(long f):     // 0x2c71550
    // probe reductionFactors  (lists @ 0x38/0x40)
    if (llvm::is_contained(getReductionFactors(), f))      // call @0x2c7156b
        return 1;                                          // mov eax,1  @0x2c71573  -> kReduction
    // probe needReplicationFactors (@0x48/0x50)
    if (llvm::is_contained(getNeedReplicationFactors(), f)) // call @0x2c71589
        return 2;                                          // mov eax,2  @0x2c71591  -> kNeedReplication
    // probe permutationFactors (@0x58/0x60 — [r9+58h]/[r9+60h] in disasm)
    bool inPerm = llvm::is_contained(getPermutationFactors(), f); // call @0x2c715a7
    return inPerm ? 3 : 0;                                 // neg al; sbb eax,eax; and eax,3
                                                           //   -> 3 (kPermutation) or 0 (kPassThrough)
```

> **NOTE — the `3`/`0` tail is branchless.** The third return is `neg al ; sbb eax,eax ; and eax,3` at `0x2c715ac`–`0x2c715b0`: `is_contained` returns a bool in `al`, the idiom turns it into `0xFFFFFFFF` (contained) or `0` (not), masked to `3` or `0`. So `kPermutation=3` / `kPassThrough=0` is not inferred from upstream convention — it is in the instruction stream. The `[r9+58h]`/`[r9+60h]` loads are the `0x58`/`0x60` permutation-list pair from the storage table above. **(CONFIRMED by disasm.)**

### Predicate / accessor API

All probe `is_contained` on the matching list (offsets above). Stock `mlir::sdy`.

| Accessor | Returns | Source list |
|---|---|---|
| `getFactorType(f)` | `FactorType` | the three typed lists, in order |
| `isReductionFactor(f)` | bool | `reductionFactors` (`0x38`/`0x40`) |
| `isNeedReplicationFactor(f)` | bool | `needReplicationFactors` (`0x48`/`0x50`) |
| `isPermutationFactor(f)` | bool | `permutationFactors` (`0x58`/`0x60`) |
| `isPassThroughFactor(f)` | bool | NOT in any of the three |
| `isBlockedPropagationFactor(f)` | bool | `blockedPropagationFactors` (`0x68`/`0x70`) |
| `hasDimensionsWithMultipleFactors()` | bool | true if any dim maps to >1 factor (reshape merge/split) |
| `getBatchingFactors()` | derived | factors present in >1 tensor (dot/conv batch handling) |
| `getIsCustomRule()` | bool | true for `CustomCall` / user `sdy.sharding_rule` attr |

---

## `createOpShardingRule` — the 26-opcode dispatch

### Purpose

`createOpShardingRule(Operation* op, bool conservative)` @ `0x2c438f0` is the registry's get-or-create. It `TypeSwitch`-es on the StableHLO op type and calls a per-opcode lambda that drives an `OpShardingRuleBuilder`; for any elementwise op not specially handled it falls through to `buildPointwise`. For `CustomCall` it first consults a per-target-name registry, then an explicit `sdy.sharding_rule` attribute, else errors. Stock `mlir::sdy`.

### Entry Point

```text
mlir::sdy::createOpShardingRule(Operation*, bool)   @ 0x2c438f0
  └─ TypeSwitch<Operation*> over StableHLO op types
       ├─ 26 per-opcode lambdas  (each {lambda(stablehlo::XxxOp)#N})
       │     each drives an OpShardingRuleBuilder, then .build()
       └─ default: OpShardingRuleBuilder::buildPointwise(op)
                    (Add/Mul/Abs/Compare/Convert/...; Pad/Slice = pointwise-with-mismatch)
```

### The dispatch table

The 26 opcodes were enumerated by extracting every `createOpShardingRule…{lambda(stablehlo::XxxOp)}` symbol from the function table. The sorted-unique set is **exactly 26**, with no missing and no extra entries.

> **GOTCHA — `CholeskyOp`'s lambda mangles one namespace level deeper.** A naive grep for `…EPNS_9OperationEbENKUlNS_9stablehlo…` returns **25** and appears to miss Cholesky. Its symbol is `…EPNS0_9OperationEbENKUlNS0_9stablehlo10CholeskyOp…` (`PNS0_`/`NS0_`, not `PNS_`/`NS_`). A namespace-agnostic pattern (`EPNS[0-9_]*9OperationEbENKUlNS[0-9_]*9stablehlo`) recovers the full **26**. A reimplementer counting handlers off the symbol table must allow for this clone-suffix / namespace-depth variation or they will under-count the dispatch. **(CONFIRMED — corrected count is 26.)**

| Opcode (lambda) | Factor algebra | Conf. |
|---|---|---|
| `DotGeneralOp` `DotOp` | batch + LHS-free + RHS-free → `kPassThrough`; **contracting → `kReduction`** | CERTAIN |
| `ReduceOp` | non-reduced dims → `kPassThrough`; **reduced dims → `kReduction`** (operand-only) | CERTAIN |
| `GatherOp` `ScatterOp` | offset dims → pass-through; index_vector / collapsed-slice dims → `kNeedReplication`/blocked (via `addGatherScatterFactors`) | CERTAIN (symbol) |
| `ConvolutionOp` | batch factor, in/out feature factors, spatial factors; feature reduction like contracting | HIGH |
| `TransposeOp` | one pass-through factor per dim; `operandDim = perm[i]`, `resultDim = i` | HIGH |
| `ReshapeOp` | merge/split — a dim maps to multiple factors; non-factorable splits → `kNeedReplication` | HIGH |
| `BroadcastInDimOp` | mapped dims pass-through; size-1→N broadcast dims → result-only `kNeedReplication` | HIGH |
| `ConcatenateOp` | pass-through per dim **except** the concat dim → `kNeedReplication` | HIGH |
| `ReverseOp` | pass-through per dim; reversed dims → **blocked** (sharding+reverse needs a halo) | HIGH |
| `AllGatherOp` `AllToAllOp` `ReduceScatterOp` | collective axis dim → permutation / need-replication | HIGH |
| `ClampOp` `SelectOp` `BitcastConvertOp` `DynamicSliceOp` `DynamicUpdateSliceOp` `ReduceWindowOp` `SortOp` `RngBitGeneratorOp` `CholeskyOp` `TriangularSolveOp` `FftOp` | bespoke per-op rule | MEDIUM |
| `CustomCallOp` | honors registry entry / user `sdy.sharding_rule` (`getIsCustomRule()==true`) | HIGH |

Disasm-confirmed lambda anchors: `DotGeneralOp` @ `0x2c3f620`, `ReduceOp` @ `0x2c40660`, `GatherOp` @ `0x2c403e0`; the gather/scatter emit helper `addGatherScatterFactors(bool isScatter, …, std::function<void(long,long,long,long,FactorType,bool)>)` @ `0x2c3cad0`.

### Algorithm — dot factor algebra (CONFIRMED)

The DotGeneral lambda @ `0x2c3f620` reads `DotDimensionNumbersAttr`, then calls `OpShardingRuleBuilder::addFactor(operandDims, resultDims, size, FactorType, blocked)` four times, once per dim group. The `FactorType` immediate pushed before each call was read directly:

```c
void DotGeneral_lambda(stablehlo::DotGeneralOp op):       // 0x2c3f620
    dn = op.getDotDimensionNumbers();
    builder = OpShardingRuleBuilder(op);
    // BATCH dims (appear in lhs, rhs, AND result) — consistent, free to shard
    builder.addFactor(dn.lhsBatchingDims, dn.rhsBatchingDims, resultBatch,
                      size, FactorType=0 /*kPassThrough*/, blocked=false);
    // LHS non-contracting (free) dims -> result; pass-through
    builder.addFactor(lhsFreeDims, resultLhsDims, size, FactorType=0, false);
    // RHS non-contracting (free) dims -> result; pass-through
    builder.addFactor(rhsFreeDims, resultRhsDims, size, FactorType=0, false);
    // CONTRACTING dims (lhs+rhs, NOT result) -> reduction
    //   disasm @0x2c3fa70 / 0x2c3fbc1: push $0x0 (blocked); push $0x1 (FactorType)
    builder.addFactor(dn.lhsContractingDims, dn.rhsContractingDims, size,
                      FactorType=1 /*kReduction*/, blocked=false);
    return builder.build();
```

The contracting factor never appears in the result mapping, so it cannot be projected onto a result sharding — propagation later refuses to grow it onto results, which is precisely the "contraction produces a partial sum" rule. `ReduceOp` (`0x2c40660`) is the same shape: a `kReduction` factor per reduced dim (`push $0x1` before the reduced-dim `addFactor` @ `0x2c409a8`), pass-through per surviving dim.

### `OpShardingRuleBuilder` — imperative construction

The rule is built mutably, then frozen by `build()`. Field layout and the `FactorType`-routing switch recovered from `reserveFactor` @ `0x2c45300`.

```c
long OpShardingRuleBuilder::reserveFactor(long size, FactorType ty, bool blocked):  // 0x2c45300
    idx = factorSizes.size();                 // factorSizes vector @ builder+0x08/0x10
    factorSizes.push_back(size);
    switch (ty) {                             // route into the typed list
        case 1: reductionFactors.push_back(idx);       break;   // list @ +0x108/+0x110
        case 2: needReplicationFactors.push_back(idx); break;   // list @ +0x148/+0x150
        case 3: permutationFactors.push_back(idx);     break;   // list @ +0x188/+0x190
        case 0: /* kPassThrough — no typed list */     break;
    }
    if (blocked) blockedPropagationFactors.push_back(idx);      // list @ +0x1c8/+0x1d0
    return idx;

Builder& OpShardingRuleBuilder::addFactor(long opDim, long resDim,            // 0x2c45630
                                          long size, FactorType ty, bool blk):
    idx = reserveFactor(size, ty, blk);
    mapSingleDimOverAllMappingsToFactor(operandMappings@+0x48, opDim, idx);   // 0x2c44fc0
    mapSingleDimOverAllMappingsToFactor(resultMappings@+0xA8,  resDim, idx);
    return *this;
```

Higher-level builder helpers (all stock): `addFactor(ArrayRef<long> opDims, ArrayRef<long> resDims, …)` (general per-tensor form, `-1` = factor absent from that tensor); `addPointwise(shape, function<FactorType(long)>, blocked)` (one factor per dim, identical across all tensors — elementwise ops); `addPointwiseWithDiffTypeForMismatch(inShape, outShape, mismatchType, …)` (dims where in/out sizes differ get a distinct type — Pad/Slice/broadcast-ish); and the `static buildPointwise(Operation*)` default rule (one `kPassThrough` factor per dim shared by all tensors).

---

## `ShardingProjection` — forward and inverse maps

### Purpose

`ShardingProjection` is the factor-space view of an op's shardings: one `TensorFactorShardings` per operand and per result, each mapping `factorIndex → FactorSharding`. `build` projects concrete `TensorShardingAttr`s **into** factor space; `createTensorShardingAttr` projects **out**. Stock `mlir::sdy`.

### Algorithm — forward projection

```c
ShardingProjection ShardingProjection::build(                              // 0x2c656c0
        ArrayRef<TensorShardingAttr> operandShardings,
        ArrayRef<TensorShardingAttr> resultShardings,
        OpShardingRuleAttr rule, MeshAttr mesh, bool):
    factorSizes = rule.getFactorSizes();
    for (i in operands)
        proj.operands[i] = buildTensorFactorShardings(                     // 0x2c648d0
              rule.getOperandMappings()[i], operandShardings[i], factorSizes, mesh);
    for (j in results)
        proj.results[j]  = buildTensorFactorShardings(
              rule.getResultMappings()[j], resultShardings[j], factorSizes, mesh);
    return proj;   // SmallVector<TensorFactorShardings,1> operands; same for results
```

`buildTensorFactorShardings` @ `0x2c648d0` is where the sub-axis split happens. For each dim `d` with concrete axes `[a0,a1,…]` and factor list `[f0,f1,…]`, it walks the axes and **distributes them across the dim's factors in order**, sized by `factorSizes[fk]`:

```c
TensorFactorShardings buildTensorFactorShardings(                          // 0x2c648d0
        TensorMappingAttr mapping, TensorShardingAttr sharding,
        ArrayRef<long> factorSizes, MeshAttr mesh, bool):
    for (d in sharding.getDimShardings()):
        axes    = dimSharding[d].getAxes();          // ArrayRef<AxisRefAttr>
        closed  = dimSharding[d].getIsClosed();      // dim cannot grow more axes
        factors = mapping.getDimMappings()[d].getFactorIndices();
        // walk axes; give each factor exactly its share by element count
        for (fk in factors):
            need = factorSizes[fk];
            // consume axes until 'need' is covered; if one mesh axis spans
            // multiple factors, SPLIT it into a sub-axis:
            //   SubAxisInfoAttr::get(ctx, preSize, size);
            //   AxisRefAttr::get(ctx, name, subAxisInfo)
            factorShardings[fk] = FactorSharding{
                axisRefs  = that factor's share,
                isClosed  = closed && (fk is the minor-most factor of dim d),
                ... };
    return { factorIndexToSharding = factorShardings, replicated/unreduced axes };
```

> **QUIRK — one mesh axis can be split across factors.** An `AxisRefAttr` is either a full named mesh axis or a `SubAxisInfoAttr {preSize, size}` slice of one. A size-8 mesh axis `y` can shard a dim that maps to two factors as `y:(pre=1,size=2)` on the first and `y:(pre=2,size=4)` on the second. The forward projection performs this split; the inverse must **re-merge** contiguous sub-axes back into full axes. A reimplementation that treats axes atomically will mis-distribute any dim whose factor-size product exceeds a single axis.

### Algorithm — inverse projection

```c
TensorShardingAttr TensorFactorShardings::createTensorShardingAttr(        // 0x2c64200
        MLIRContext* ctx, TensorMappingAttr mapping,
        ArrayRef<long> factorSizes, StringRef meshName, MeshAttr mesh):
    for (d in tensorDims):
        dimAxes = [];
        for (f in mapping.getDimMappings()[d].getFactorIndices()):   // factor order
            addAxesToDimSharding(dimAxes, factorIndexToSharding[f].axisRefs, mesh);
            //   ^ concatenates + merges contiguous sub-axes back into full axes
        dimShardings[d] = DimensionShardingAttr::get(ctx, dimAxes, isClosed);
    return TensorShardingAttr::get(ctx, meshName, dimShardings,
                                   replicatedAxes, unreducedAxes);
```

This is the map that turns propagated factor state back into a per-tensor `TensorShardingAttr`. The propagation pass writes that attr onto the op; it later becomes an `HloSharding` via the sdy↔mhlo round-trip (see [13.4](hlo-sharding.md)).

### Mutators used by propagation

| Mutator | Address | Effect |
|---|---|---|
| `expandSharding(f, newAxes)` | `0x2c64580` | **append** axes to factor `f` in every tensor that contains it (forward growth) |
| `updateSharding(f, newAxes, overflowAxes)` | `0x2c64720` | **replace** the factor's axes + record overflow (axes exceeding the factor size that spill) |
| `getGreatestCommonPrefixAxes(f)` | `0x2c65dd0` | across all tensors containing `f`, the longest common prefix of their axis lists — the agreed sharding for `f` |

Both `expand`/`update` delegate to `TensorFactorShardings::expandShardingAxes` / `updateShardingAxes` per tensor.

---

## `FactorSharding` — per-factor state and conflict resolution

### Container shapes

```text
ShardingProjection      = { SmallVector<TensorFactorShardings,1> operands;
                            SmallVector<TensorFactorShardings,1> results; }
TensorFactorShardings   = { DenseMap<long /*factorIndex*/, FactorSharding> factorIndexToSharding;
                            (replicated / unreduced axis bookkeeping) }
```

### The `0x90` DenseMap entry stride (CONFIRMED)

The per-tensor factor map is a `DenseMap<long, FactorSharding>`. Its entry is a `DenseMapPair<long, FactorSharding>`: an 8-byte `long` key followed by the 136-byte `FactorSharding` value, **144 bytes (`0x90`) per entry**.

> **NOTE — the stride is in two independent instruction sites, not a recovered struct.** IDA did not recover a named `FactorSharding`/`DenseMapPair` struct (these are stock C++ template instantiations), so the stride cannot be read off a struct definition. It is confirmed two ways from disasm: **(1)** the grow routine `DenseMap<long, FactorSharding, …, DenseMapPair<long, FactorSharding>>::grow(uint)` @ `0x2c2da30` computes the allocation size as `lea rdi, [rax+rax*8]` (×9) followed by `shl rdi, 4` (×16) = ×144 → `allocate_buffer` (`0x2c2da9a`–`0x2c2daa2`); **(2)** the indexing in `operator[]` @ `0x2c2dd50` uses `imul …, 90h` (`0x2c2dfc4`). Both agree: 144 = 8 (key) + 136 (value). The lookup helper `getFactorSharding(TensorFactorShardings const&, long)` @ `0x2c2d200` returns the optional value via the same map. **(CONFIRMED — `lea ×9; shl 4` and `imul,0x90`.)**

### `FactorSharding` field layout

Offsets relative to the value (DenseMap entry base + `0x08`), from `getFactorSharding` @ `0x2c2d200`, `updateShardingAxes` @ `0x2c63cc0`, `getShardingSize` @ `0x2c63fb0`:

| Field | Offset (value-relative) | Type | Meaning |
|---|---|---|---|
| `axisRefs` | `+0x00` (begin `+0x08`, size `+0x10`) | `SmallVector<AxisRefAttr>` | mesh axes (or sub-axes) assigned to this factor |
| `overflowAxes` | `+0x48` (begin `+0x50`, size `+0x58`) | `SmallVector<AxisRefAttr>` | axes exceeding the factor size; kept for the inverse map to re-attach as replicated/extra |
| `isClosed` | (bool, later offset) | bool | factor's dim is closed; no further axes may be appended |
| `isMinorMost` | (bool, later offset) | bool | factor is the minor-most of its dim; only the minor-most factor may receive newly-propagated axes |

### `AxisRefAttr` algebra

The per-axis comparison primitives (`0x2c70a60`..`0x2c70cc0`) make conflict resolution work; all are sub-axis aware:

```c
long  AxisRefAttr::getSize(MeshAttr mesh):                 // 0x2c70a60
    return subAxisInfo.size  if (this ref is a sub-axis)   // field @ +0x18 set
                            else mesh.getAxisSize(name);   // MeshAttr::getAxisSize @0x2c70480

bool contains(other);        // this range covers other
bool prefixOf(other);        // non-strict prefix
bool strictPrefixOf(other);  // strict prefix
bool overlaps(other);        // ranges intersect (sub-axis aware)
```

`TensorFactorShardings::getShardingSize(mesh)` @ `0x2c63fb0` = product over all factors of (product of `axisRef` sizes + overflow axis sizes) — "how many ways is this tensor sharded", used to compare competing shardings.

### Conflict-resolution rules (`BasicFactorPropagation`)

The rules that decide which axes a factor may take. Stock `mlir::sdy`; the *driver* loop that calls them lives in [13.2](sharding-propagation.md).

```c
// trim a candidate axis to the part NOT already used by other factors of the
// same tensor — no two factors may share a mesh axis.
AxisRefAttr getPrefixWithoutOverlap(axis, existingAxes):                    // 0x2c1bde0

bool compatiblePrefixNoConflictsWithinFactor(axis, projAxes, otherAxes, FS): // 0x2c1c950
    -> uses getPrefixWithoutOverlap against axes used by OTHER factors of this tensor

bool compatiblePrefixNoConflictsAcrossFactors(axis, DenseMap<long,FactorSharding>, f): // 0x2c1c830
    -> same, but across the whole factor map

optional<AxisRefAttr> getCompatibleMajorShardingAxes(                       // 0x2c1d3c0
        proj, factor, direction, ..., mesh, bool):
    // for ONE factor: greatest-common-prefix of its axisRefs across every
    // tensor containing it, minus within/across-factor overlaps, in the
    // propagation direction (FORWARD = operands->results, BACKWARD = reverse).
    // returns the axes safe to assign to this factor this step.
```

### How `FactorType` gates propagation

From `getCompatibleFactorShardings` @ `0x2c36320` and the direction function:

- **`kReduction`** — not propagated onto results (a sharded contracting dim of a dot produces a partial sum; the result stays unsharded on that factor).
- **`kNeedReplication`** — blocks axis assignment; the factor stays replicated.
- **`kPermutation`** — may be sharded but requires permutation-consistency (`hasShardedPermutationFactorsPerTensor` gates collective insertion).
- **`kPassThrough`** — propagates freely operand↔result.
- **`blockedPropagationFactors`** — skipped entirely by the direction function.

`PropagationDirection` is `{kNone=0, kForward=1, kBackward=2, kBoth=3}`. `getDirectionBasedOnUses(Operation*, bool)` @ `0x2c11190` returns it (default `3=kBoth`); the type appears directly in the `std::function<PropagationDirection(long)>` parameter of `BasicFactorPropagation::propagateFactorShardings` @ `0x2c1d540` and `AggressiveFactorPropagation::propagateFactorShardings` @ `0x2c1ac60`.

---

## End-to-end for one op

The reimplementation-grade pipeline for a single op (each step ties to a section above):

```c
// 1. build the rule
rule = createOpShardingRule(op, conservative);            // 0x2c438f0
       // -> OpShardingRuleAttr{factorSizes, op/result mappings,
       //                       reduction/needRepl/permutation/blocked sets}

// 2. forward projection: concrete sharding -> factor space
proj = ShardingProjection::build(operandShardings,        // 0x2c656c0
            resultShardings, rule, mesh);
       // -> per-tensor DenseMap<factor, FactorSharding{axisRefs,overflow,
       //                                               isClosed,isMinorMost}>

// 3. propagate on factors (driver in 13.2; algebra here)
for (f in factorsToPropagate, skipping blocked, respecting direction & type):
    axes = getCompatibleMajorShardingAxes(proj, f, dir, mesh);   // 0x2c1d3c0
           // = greatest-common-prefix of f's axisRefs across all tensors,
           //   minus within/across-factor overlaps; kReduction not pushed to
           //   results; kNeedReplication stays replicated.
    proj.expandSharding(f, axes)  /  proj.updateSharding(...);    // 0x2c64580 / 0x2c64720
    // basic phase first (prefix-only), then aggressive (multi-axis per step)

// 4. inverse projection: factors -> concrete sharding; fixed-point loop
for (t in operands+results):
    newSharding = TensorFactorShardings::createTensorShardingAttr(  // 0x2c64200
                    ctx, rule.mapping[t], factorSizes, meshName, mesh);
    write newSharding onto op;   // until no change

// 5. lower to HLO (13.4)
hloSharding = xla::sdy::convertToHloSharding(newSharding, getMesh, manualAxes); // 0x2bc58f0
```

---

## Stock Shardy vs Neuron consumers

> **NOTE — provenance boundary.** The entire factor algebra is upstream OpenXLA, statically linked into `hlo-opt`; all target symbols resolve under `mlir::sdy` / `xla::sdy` inside `hlo-opt` itself (no `libBIR`/`libwalrus` involvement). Neuron authors only the *downstream consumers*. The boundary matters: a reimplementer porting Neuron's distribution stack reuses stock Shardy unchanged and reimplements only the replica-group / collective layer.

**Stock OpenXLA Shardy / StableHLO (this page):** `OpShardingRuleAttr`, `OpShardingRuleBuilder`, `createOpShardingRule`, `ShardingProjection`, `TensorFactorShardings`, `FactorSharding`, `FactorType`, `Basic`/`AggressiveFactorPropagation`, `convertToHloSharding`, `Import`/`ExportStablehloShardingsPass`. Also linked but distinct: the older `xla::spmd` partitioner (`SpmdPartitioningVisitor`, `PartitionConvolution`, `WindowedDotGeneralLoop`) — the tensor-level executor, see [13.1](spmd-partitioner-driver.md).

**Neuron-authored (consumers, not the algebra):** `neuron::GetTpReplicaGroup(HloComputation*/HloModule*)` derives tensor-parallel replica groups from the resulting `HloSharding`; `neuron::HasMatchingReplicaGroups`; `xla::hilo::NeuronReduceScatterCombiner`, `NeuronMoveAllGatherWhileLoop` collective rewrites. These run **after** the sdy projection produces shardings; they do not alter the factor algebra. The Logical-NeuronCore (LNC) constraint enters only via `MeshAttr` axis sizes and device order used by `convertToHloSharding` and `GetTpReplicaGroup` — a consumer concern, documented with the collective passes ([13.5](compute-handlers.md)).

### Registry and grounding strings

`createOpShardingRule` is the registry's get-or-create: for a known StableHLO op it builds the canonical rule; for `CustomCall` it looks up a per-target-name rule, else honors `sdy.sharding_rule` (`getIsCustomRule()`), else errors. `DropShardingRulesPass` strips `sdy.op_sharding_rule` attrs after propagation.

The following strings are present verbatim in `hlo-opt` `.rodata` and ground the claims above (confirmed by string-table scan):

| String | Grounds |
|---|---|
| `"a factor can only be in one of the reduction, need replication, or permutation factor sets"` | mutual exclusion of `FactorType {1,2,3}` |
| `"accumulatedFactor == Product(result)"` | reshape factor-size product invariant |
| `"basic factor propagation followed by aggressive factor propagation"` | two-phase propagation order (13.2) |
| `"custom call @{0} is unknown to SDY sharding rule registry"` / `"op '{0}' is unknown to SDY sharding rule registry"` | registry get-or-create errors |
| `"Converts the shardy attributes from strings in MHLO frontend attributes to SDY meshes, shardings and sharding rules."` | the import pass (reverse of 13.4) |
| `sdy.op_sharding_rule`, `op_sharding_rule`, `sdy.sharding`, `sdy.sharding_rule`, `sdy.sharding_per_value`, `sdy.sharding_constraint`, `need_replication`, `permutation`, `reduction`, `blocked_propagation` | attr / factor-set tokens |

> **CORRECTION (D-AB05) —** the SDY source-file path strings (`op_sharding_rule_builder.cc`, `op_sharding_rule_registry.cc`, `sharding_projection.cc`, `basic_factor_propagation.cc`, `aggressive_factor_propagation.cc`, `drop_sharding_rules.cc`) and the token `factor_sizes` were cited in early notes as `.rodata` strings. A direct string-table scan finds them **absent** — this release build stripped the SDY-internal source-loc/assert path strings (only XLA-side `.cc` names like `export_shardings.cc`, `sharding_propagation.cc` survive). The surviving assertion/registry messages above provide the grounding instead. The *symbols* and *addresses* are unaffected and remain CONFIRMED.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `mlir::sdy::createOpShardingRule(Operation*,bool)` | `0x2c438f0` | master 26-opcode dispatch | CERTAIN |
| `OpShardingRuleAttr::getFactorType(long)` | `0x2c71550` | `FactorType` classifier (enum 0/1/2/3) | CERTAIN |
| `OpShardingRuleBuilder::reserveFactor(long,FactorType,bool)` | `0x2c45300` | append factor + route into typed list | CERTAIN |
| `OpShardingRuleBuilder::addFactor(long,long,...)` | `0x2c45630` | reserve + map dim in op/result | CERTAIN |
| `createOpShardingRule::DotGeneral#11` | `0x2c3f620` | contracting → `kReduction(1)` | CERTAIN |
| `createOpShardingRule::Reduce#18` | `0x2c40660` | reduced → `kReduction(1)` | CERTAIN |
| `createOpShardingRule::Gather#16` | `0x2c403e0` | drives `addGatherScatterFactors` | HIGH |
| `addGatherScatterFactors(...)` | `0x2c3cad0` | gather/scatter factor emitter | HIGH |
| `ShardingProjection::build(...)` | `0x2c656c0` | forward projection (per-tensor) | CERTAIN |
| `buildTensorFactorShardings(...)` | `0x2c648d0` | sharding→factor split (sub-axis distribute) | CERTAIN |
| `ShardingProjection::expandSharding(...)` | `0x2c64580` | append axes to a factor | CERTAIN |
| `ShardingProjection::updateSharding(...)` | `0x2c64720` | replace axes + record overflow | CERTAIN |
| `ShardingProjection::getGreatestCommonPrefixAxes(long)` | `0x2c65dd0` | agreed axes per factor | CERTAIN |
| `TensorFactorShardings::createTensorShardingAttr(...)` | `0x2c64200` | inverse projection (factors→sharding) | CERTAIN |
| `TensorFactorShardings::getShardingSize(MeshAttr)` | `0x2c63fb0` | product of axis sizes | CERTAIN |
| `getFactorSharding(TensorFactorShardings const&,long)` | `0x2c2d200` | DenseMap lookup (stride `0x90`) | CERTAIN |
| `DenseMap<long,FactorSharding>::grow(uint)` | `0x2c2da30` | `lea×9; shl 4` = 144 B entry | CERTAIN |
| `AxisRefAttr::getSize(MeshAttr)` | `0x2c70a60` | sub-axis vs mesh axis size | CERTAIN |
| `BasicFactorPropagation::propagateFactorShardings(...)` | `0x2c1d540` | prefix-only driver | HIGH |
| `AggressiveFactorPropagation::propagateFactorShardings(...)` | `0x2c1ac60` | multi-axis driver | HIGH |
| `getCompatibleMajorShardingAxes(...)` | `0x2c1d3c0` | per-factor compatible axes | HIGH |
| `getDirectionBasedOnUses(Operation*,bool)` | `0x2c11190` | `PropagationDirection` chooser | CERTAIN |
| `xla::sdy::convertToHloSharding(...)` | `0x2bc58f0` | sdy attr → `HloSharding` | CERTAIN |
| `xla::sdy::createExportStablehloShardingsPass(bool)` | `0x2bc3dc0` | sharding → HLO string pass | CERTAIN |

---

## Related Components

| Name | Relationship |
|---|---|
| `mlir::sdy` propagation passes | the driver loop that calls this algebra ([13.2](sharding-propagation.md)) |
| `xla::spmd` partitioner | tensor-level executor (distinct, older) ([13.1](spmd-partitioner-driver.md)) |
| `xla::HloSharding` | terminal lowering of the projected factors ([13.4](hlo-sharding.md)) |
| `neuron::GetTpReplicaGroup` / collective rewrites | Neuron consumers of the resulting sharding ([13.5](compute-handlers.md)) |

## Cross-References

- [SPMD Partitioner Driver](spmd-partitioner-driver.md) — 13.1; the `xla::spmd` tensor-level partitioner, distinct from this sdy propagation algebra
- [Sharding Propagation](sharding-propagation.md) — 13.2; the basic/aggressive propagation passes and fixed-point loop that drive this algebra
- [HLO Sharding](hlo-sharding.md) — 13.4; `convertToHloSharding` and the sdy↔mhlo round-trip that consumes the inverse projection
- [Compute Handlers](compute-handlers.md) — 13.5; Neuron's replica-group / collective consumers downstream of the projected shardings
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where `hlo-opt` and the `Frontend` job sit in the driver pipeline
