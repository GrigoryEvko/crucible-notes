# MhloToPythonPrinter — Heavy, Collective & Fusion/Reduce Emitters

> *All addresses on this page apply to neuronx-cc `2.24.5133.0+58f8de22` (the `hlo2penguin` binary, cp310 build). `.rodata = VA − 0x200000`, `.text = VA − 0x201000` — `VA == file-offset` is **false** for this binary. Other versions will differ.*

## Abstract

The driver page ([4.43](mhlo-to-python-printer-driver.md)) established the one fact that governs everything here: the terminal `--mhlo-to-py-penguin` pass does not lower MLIR to another dialect — it **prints Python source text** that reconstructs the graph as `neuronxcc.starfish.penguin` objects. Each MLIR op becomes one Python statement, assembled by the shared spine `printOperandsAndAttributes` @ `0x20c3e30` into the canonical shape `<dst> = m<N>.NeuronTensorOp(srcs=[…], dsts=[…], op=…, <k>=<v>, …, dl=…DebugLocation(…))`. The driver reconstructed the module skeleton and the elementwise emitters. This page reconstructs **the rest of the emission surface**: the *heavy* ops (dot / sort / topk / dropout / rmsnorm / resize / offloaded-memcpy), the *collective* ops (all-reduce / all-gather / reduce-scatter / all-to-all / collective-permute plus the Neuron kernel custom-calls), and the *fusion / reduce-window / scatter* emitters that serialize nested computations. With 4.43 (driver) and 4.45 (StableHLO twin) these three groups cover the complete `mhlo` → Penguin-Python emission map.

The map has a clean three-way shape. **Heavy ops** are either native `mhlo` ops (`dot`, `dot_general`, `sort`, the reduce/scatter family) routed by `printOperation`, or `AwsNeuron*` custom-calls routed by a 15-lambda target-name dispatcher `print<mhlo::CustomCallOp>` @ `0x20d1f10`; each marshals an op-specific `ArrayRef<pair<string,string>>` of attributes and emits one `NeuronTensorOp`. **Collective ops** survive to emission as native `mhlo` collectives (they are *not* converted to custom-calls in this binary) and funnel through one templated body `printCollectiveOp<…>` in exactly three arities (0/1/3 attr-pairs) that builds `replica_groups`, `kind`, `stream_id`, and the reduction-op string. **Fusion ops** dispatch on the `FusionKind` inherent attribute to one of four printers, three of which emit a grouping header `NeuronTensorOp` *and then inline-traverse the fusion body*; the **reduce/scatter** emitters collapse their nested add/max/min computation to a numpy ufunc *name* (`op=np.add`, `xla_op='np.add'`) plus a reduction identity.

Every symbol, size, attribute key, and emitted format-string fragment on this page was re-verified against the `hlo2penguin` function table and string pool. The one notable surprise — the `DotLogistic` fusion emits the surface name `op='DotFusion'`, not `'DotLogistic'` — is confirmed below by the string literal at `0x25a294`.

For reimplementation, the contract is:

- **The heavy-op attribute schemas** — exactly which `pair<string,string>` list each of dot/sort/topk/dropout/rmsnorm/resize pushes, where each value is read off the op, and which are hardcoded.
- **The collective `kind`/`stream_id` model** — the single 0-pair master that handles all five collective types, the 1-pair (AG/RS) and 3-pair (AllToAll) wrappers, and the mandatory `stream_id` (FATAL if missing).
- **The nested-computation serializer** — how `extractReduceFunction` + `getReduceOpStrFromOperation` turn a reduce/scatter body into `np.<fn>`, and how `printReduceDefaultInit` supplies the identity.
- **The fusion dual-string carriage** — `op="<canonical kind>"` vs `hilo_fusion_op="<actual FusionKind>"`, the header-then-inline structure, and the header-less `ScheduleFusion`.

| | |
|---|---|
| **Statement spine** | `printOperandsAndAttributes` @ `0x20c3e30` (3185 B), `opName="NeuronTensorOp"` (str @ `0x26e572`) |
| **Custom-call dispatcher** | `print<mhlo::CustomCallOp>` @ `0x20d1f10` (2762 B), 15-lambda StringMap on `getCallTargetName()` |
| **Fusion dispatcher** | `print<mhlo::FusionOp>` @ `0x20f5090` (1166 B), 8-way `FusionKind` string cascade |
| **Collective master** | `printCollectiveOp<>` @ `0x20e7cd0` (9041 B), all five collective types |
| **Nested-comp mapper** | `getReduceOpStrFromOperation` @ `0x20af670` (1244 B) → numpy ufunc name |
| **IR level** | `mhlo`/`func` dialect in → Python source text out |
| **Source anchor** | `hilo/MLIRPasses/Transforms/MhloToPythonPrinter.cc` @ `0x3cd468` |

---

## 1. Emission Surface at a Glance

Every emitter below produces a single Python statement (fusion/schedule emitters additionally inline a body). They split into three families by *how* they reach the printer and *what call name* they emit:

| Family | Routed by | Call name | Emitters |
|---|---|---|---|
| **Heavy — native** | `printOperation` (TypeID) | `NeuronTensorOp` | `printDotOp`, `printSortOp`, reduce/window/scatter (§5) |
| **Heavy — custom-call** | `print<CustomCallOp>` dispatch | `NeuronTensorOp` (most), `.NativeKernel`/`.LNCShardingConstraintOp` (two) | `printTopK`, `printDropout`, `printRmsNorm`, `printResize*`, kernel emitters (§4) |
| **Heavy — structural** | `print<Copy/Reshape/OptBarrier>` | `.OffloadedMemCpy` | `printOffloadedMemCpy` (§3.8) |
| **Collective** | `printOperation` (native `mhlo`) | `NeuronTensorOp` | `printCollectiveOp<>` ×3 arities (§4) |
| **Fusion** | `print<FusionOp>` (`FusionKind`) | `NeuronTensorOp` + body inline | `printArbitrary/MulRedSqrt/DotLogistic/ScheduleFusionOp` (§6) |

The complete function map (all addresses and sizes re-verified against `*_functions.json`; demangled names are the IDA-recovered `mlir::MhloToPythonPrinter::*` symbols, all binary-derived):

| Function | Addr | Size (B) | Emits | Conf |
|---|---|---|---|---|
| `printDotOp(Op*, ArrayRef<long>×4)` | `0x20c5af0` | 3804 | `NeuronTensorOp(xla_op='mhlo.dot', …)` | CONFIRMED |
| `printSortOp(Op*)` | `0x20db230` | 2534 | `NeuronTensorOp(xla_op='mhlo.sort', …)` | CONFIRMED |
| `printTopK(Op*)` | `0x20d9ed0` | 4581 | `NeuronTensorOp(xla_op='mhlo.top_k', …)` | CONFIRMED |
| `printDropout(Op*)` | `0x20d0d00` | 1000 | `NeuronTensorOp(xla_op='mhlo.dropout', …)` | CONFIRMED |
| `printRmsNorm(Op*, bool)` | `0x20e1a80` | 2844 | `NeuronTensorOp(xla_op='mhlo.rms_norm[_backward]', …)` | CONFIRMED |
| `printResizeNearest(Op*)` | `0x20e5d50` | 3304 | `NeuronTensorOp(xla_op='mhlo.resize_nearest', …)` | CONFIRMED |
| `printResizeBilinear(Op*)` | `0x20e27d0` | 3419 | `NeuronTensorOp(xla_op='mhlo.resize_bilinear', …)` | CONFIRMED |
| `printOffloadedMemCpy(Value, StringRef, Op*)` | `0x20bfa10` | 922 | `<prefix>.OffloadedMemCpy(…)` | CONFIRMED |
| `printCollectiveOp<>` (master) | `0x20e7cd0` | 9041 | `NeuronTensorOp(kind=…)` | CONFIRMED |
| `printCollectiveOp<pair>` (AG/RS) | `0x20ea030` | 9335 | `NeuronTensorOp(…, <dim>=…)` | CONFIRMED |
| `printCollectiveOp<pair,pair,pair>` (AllToAll) | `0x20d6e30` | 9613 | `NeuronTensorOp(…, split/concat/count)` | CONFIRMED |
| `printCollectiveMatmulKernel(Op*)` | `0x20cfff0` | 1694 | `NeuronTensorOp(target_name=…, backend_config={…})` | CONFIRMED |
| `printNativeKernel(Op*, pair)` | `0x20e3f10` | 6025 | `.NativeKernel(…)` + `.aliasTensors(…)` | CONFIRMED |
| `printMLPNKIKernel(Op*)` | `0x20d0780` | 1182 | `NeuronTensorOp(target_name=…)` | CONFIRMED |
| `printNeuronCustomOp(Op*)` | `0x20d2d70` | 5271 | `NeuronTensorOp(function_name=…, lib_file_name=…)` | CONFIRMED |
| `printLNCShardingConstraint(Op*)` | `0x20e37e0` | 1281 | `.LNCShardingConstraintOp(sharding=…)` | CONFIRMED |
| `printSendRecv(Op*, bool)` | `0x20e10f0` | 1995 | `NeuronTensorOp(xla_op='mhlo.send'/'recv', peer_id=…)` | CONFIRMED |
| `print<FusionOp>` (dispatch) | `0x20f5090` | 1166 | (routes §6) | CONFIRMED |
| `printArbitraryFusionOp(Op*, StringRef, StringRef)` | `0x20f3f00` | 3492 | header + body inline | CONFIRMED |
| `printMulRedSqrtFusionOp(Op*)` | `0x20f1f60` | 4032 | `op='MulRedSqrt', reduce_dims=[…]` + body | CONFIRMED |
| `printDotLogisticFusionOp(Op*)` | `0x20f3050` | 3441 | `op='DotFusion'` + body | CONFIRMED |
| `printScheduleFusionOp(Op*)` | `0x20f4ce0` | 869 | body-only (no header) | CONFIRMED |
| `print<ReduceOp>` | `0x20dc5a0` | 2743 | `op=np.<fn>, reduce_dims=[…]` | CONFIRMED |
| `print<ReduceWindowOp>` | `0x20d5ff0` | 3642 | `op=np.<fn>, window_shape/stride/padding` | CONFIRMED |
| `print<ScatterOp>` | `0x20cbc70` | 4388 | scatter dim-numbers + `scatter_kind=np.<fn>` | CONFIRMED |
| `print<SelectAndScatterOp>` | `0x20de190` | 5359 | dual nested-comp + `scatter_ident` | CONFIRMED |
| `getReduceOpStrFromOperation(Op*)` | `0x20af670` | 1244 | (helper: op → ufunc name) | CONFIRMED |
| `extractReduceFunction(Op*)` | `0x20afca0` | 329 | (helper: body → fn-name) | CONFIRMED |
| `extractReduceFunctionBlock(Block&)` | `0x20afbe0` | 181 | (helper: find Add/Mul/Min/Max/Or/And) | CONFIRMED |
| `printReduceDefaultInit(string&, StringRef, Type)` | `0x20bc090` | 4474 | (helper: reduction identity) | CONFIRMED |

> **NOTE —** the report drafts under-counted four of these (`printCollectiveMatmulKernel ~700→1694`, `printNativeKernel ~4100→6025`, `printNeuronCustomOp ~4400→5271`, `printLNCShardingConstraint ~430→1281`). The sizes here are the function table's, re-read in this pass; the larger bodies absorb the `.cold` siblings IDA folds into the parent extent.

---

## 2. The Custom-Call Dispatcher — `print<mhlo::CustomCallOp>` @ `0x20d1f10`

`mhlo.custom_call` is the single MLIR op that fans out to the most emitters. `print<CustomCallOp>` builds a `StringMap` once and dispatches on `CustomCallOp::getCallTargetName()` to one of 15 lambdas; the matched lambda calls the dedicated emitter. The target-name keys are verbatim `.rodata` strings, each re-verified at its cited address:

| Target name (str @ addr) | → emitter | Penguin call |
|---|---|---|
| `AwsNeuronTopK` @ `0x262790` | `printTopK` @ `0x20d9ed0` | `NeuronTensorOp(xla_op='mhlo.top_k')` |
| `AwsNeuronDropout` @ `0x286fb1` | `printDropout` @ `0x20d0d00` | `NeuronTensorOp(xla_op='mhlo.dropout')` |
| `AwsNeuronRmsNorm` @ `0x27ad8d` | `printRmsNorm(op, false)` | `…='mhlo.rms_norm'` |
| `AwsNeuronRmsNormBackward` @ `0x2628ab` | `printRmsNorm(op, true)` | `…='mhlo.rms_norm_backward'` |
| `ResizeNearest` @ `0x23e3bb` | `printResizeNearest` @ `0x20e5d50` | `…='mhlo.resize_nearest'` |
| `ResizeBilinear` @ `0x2326d5` | `printResizeBilinear` @ `0x20e27d0` | `…='mhlo.resize_bilinear'` |
| `AwsNeuronCollectiveMatmul` @ `0x2727a8` | `printCollectiveMatmulKernel` | `NeuronTensorOp(target_name=…)` |
| `AwsNeuronCustomNativeKernel` @ `0x2521b1` | `printNativeKernel` | `.NativeKernel(…)` |
| `AwsNeuronMLPNKI` @ `0x26a7c4` | `printMLPNKIKernel` | `NeuronTensorOp(target_name=…)` |
| `AwsNeuronCustomOp` @ `0x28b240` | `printNeuronCustomOp` | `NeuronTensorOp(function_name=…)` |
| `AwsNeuronLNCShardingConstraint` @ `0x360f50` | `printLNCShardingConstraint` | `.LNCShardingConstraintOp(…)` |
| `FusedSend` @ `0x24a40a` | `printSendRecv(op, true)` | `…='mhlo.send', peer_id=…` |
| `FusedRecv` @ `0x22e3d9` | `printSendRecv(op, false)` | `…='mhlo.recv', peer_id=…` |
| `AwsNeuronControlDep` @ `0x41fa30` | (control-dep reification, 4.x) | — |

> **GOTCHA —** `mhlo.dot` / `mhlo.dot_general` / `mhlo.sort` and the reduce/scatter family are **not** custom-calls. They are native `mhlo` ops reaching the printer through `printOperation`'s TypeID cascade. Only the `AwsNeuron*` / `Resize*` / `Fused*` targets above route through this dispatcher. Do not look for `mhlo.dot` as a custom-call key — it is not one.

---

## 3. Heavy-Op Emission Spec

All heavy emitters that go through the spine build an `ArrayRef<pair<string,string>>` and hand it to `printOperandsAndAttributes(op, "NeuronTensorOp", op=…, skipmask, attrs, sep="=")`. The emitter's *whole job* is to build that pair list off the op; the spine prints `srcs=[…]`, `dsts=[…]`, the pairs `k=v`, then `dl=…DebugLocation(…)`. String *values* are single-quoted via `hilo::getQuotedStr(StringRef, '\'')` unless noted.

### 3.1 `printDotOp` @ `0x20c5af0` — the matmul hitter

This is the most consequential emitter and the cleanest illustration of the *pre-decomposition* discipline: `printDotOp` takes **four `ArrayRef<long>` parameters** = `lhs_batching`, `lhs_contracting`, `rhs_batching`, `rhs_contracting`. It does **not** parse `dimension_numbers` itself — the two callers do that and forward the decomposed lists:

```text
print<mhlo::DotGeneralOp>  @0x20c6d60
    DotGeneralOp::getDotDimensionNumbers
    → DotDimensionNumbersAttr::getLhsBatchingDimensions      (CONFIRMED callee)
    → DotDimensionNumbersAttr::getLhsContractingDimensions   (CONFIRMED callee)
    → DotDimensionNumbersAttr::getRhsBatchingDimensions      (CONFIRMED callee)
    → DotDimensionNumbersAttr::getRhsContractingDimensions   (CONFIRMED callee)
    → printDotOp(op, lhsB, lhsC, rhsB, rhsC)                 (CONFIRMED callee)

print<mhlo::DotOp>         @0x20c6a40
    plain dot has no dimension_numbers → uses a __cxa_guard-protected static
    (empty batching + synthesized trailing-dim contracting pair) → printDotOp(…)
```

Inside `printDotOp`, each of the four `ArrayRef<long>` is rendered by `mlir::(anon)::printList<long>` (CONFIRMED: 4 calls in the callee graph) into a `[d0, d1, …]` literal and pushed as a pair. The asymmetric naming is a real schema hazard:

```text
getLhsBatchingDimensions    → kwarg "lhs_batching_dims"   (str @0x226331)   [full]
getLhsContractingDimensions → kwarg "lhs_contract_dims"   (str @0x246585)   [ABBREVIATED]
getRhsBatchingDimensions    → kwarg "rhs_batching_dims"   (str @0x27ef25)   [full]
getRhsContractingDimensions → kwarg "rhs_contract_dims"   (str @0x20d98e)   [ABBREVIATED]
```

> **QUIRK —** the contracting kwargs are spelled `*_contract_dims` (abbreviated), while the batching kwargs are spelled `*_batching_dims` (full). A reimplementer copying the MLIR accessor names verbatim will mis-name the contracting kwargs. The Penguin schema uses the abbreviated form. (Both literals re-verified in `*_strings.json`: `lhs_batching_dims`@`0x226331`, `lhs_contract_dims`@`0x246585`, `rhs_contract_dims`@`0x20d98e`.)

Two optional kwarg groups are read from the `mhlo.frontend_attributes` dictionary (via `StringAttr::getValue` — CONFIRMED in the callee set) and pushed only when present:

- **Quantization:** `lhs_zero_point` (@`0x22231d`), `rhs_zero_point` (@`0x2522c7`).
- **Autodiff hint:** `grad_x` (@`0x28315d`), `grad_y` (@`0x22e3ea`), rendered `True`/`False`.

Worked example — a quantized batched `mhlo.dot_general` (`[B,M,K]·[B,K,N]`, contracting K, batching B) emits:

```python
v17 = m0.NeuronTensorOp(srcs=[v3, v8], dsts=[v17], op="NeuronTensorOp",
    xla_op='mhlo.dot', lhs_batching_dims=[0], lhs_contract_dims=[2],
    rhs_batching_dims=[0], rhs_contract_dims=[1],
    lhs_zero_point='0', rhs_zero_point='0',
    dl=m9.DebugLocation('matmul', 41))
```

> **CORRECTION —** the MHLO `printDotOp` emits **no** `precision_config` / `DotAlgorithm` — precision is dropped on the MHLO path. The StableHLO twin `printDotOp` @ `0x21600f0` takes a 6th `std::string` parameter (the precision string from its dispatcher); that path is 4.45's scope, not this page's. See [0.7 (matmul walkthrough)](../index.md) for the end-to-end dot lowering.

### 3.2 `printSortOp` @ `0x20db230`

Native `mhlo.sort`; `xla_op='mhlo.sort'`. Three attrs, one of which is *derived from the comparator region* rather than read directly:

```text
"dimension"      ← SortOp::getDimension                              → int
"is_stable"      ← SortOp::getIsStable                               → True/False
"comparison_dir" ← Block::getTerminator (mhlo.return) → trace operand
                   to its defining CompareOp → getComparisonDirection
                   → one of { lt, le, gt, ge }
```

The comparator-region trace is the interesting part: the printer walks the sort's comparator block, finds the `mhlo.return`, follows the returned `i1` value to its defining `mhlo.compare`, and reads that compare's direction. Guards `NCC_PYP020`/`NCC_PYP021` fire when the comparator is not the expected single-`CompareOp` shape.

### 3.3 `printTopK` @ `0x20d9ed0`

Custom-call `AwsNeuronTopK`; `xla_op='mhlo.top_k'`. Only `k` and `axis` are dynamic — the rest are hardcoded:

```text
"k"             ← strtol(backend_config, base 10)
"axis"          ← computed from operand rank (trailing-dim default)
"is_ascend"     = False   [HARDCODED → top-K means LARGEST; no `largest`/`sorted` kwarg]
"is_barrier"    = False   [HARDCODED]
"ret_type"      = "both"  [HARDCODED → always returns (values, indices)]
"src_shape"=[…] · "dtype"=… · "indices_dtype"=…
```

> **QUIRK —** there is no `largest` or `sorted` kwarg. "Largest" is encoded as the hardcoded `is_ascend=False`, and `ret_type="both"` always returns both values and indices. Only `k` and `axis` vary at runtime; everything else is a fixed string immediate.

### 3.4 `printDropout` @ `0x20d0d00`

Custom-call `AwsNeuronDropout`; `xla_op='mhlo.dropout'`. The thinnest heavy emitter — two attrs:

```text
"dtype"      ← ShapedType::getElementType → printType
"predicates" = []   (empty control-dep list)
```

> **GOTCHA —** the dropout **rate and seed are tensor operands, not kwargs.** They appear positionally inside the spine's `srcs=[…]` list — the emitter pushes neither a `rate=` nor a `seed=` pair. A reimplementer expecting `rate`/`seed` named attributes will not find them; read them off the operand list. The op is multi-result (`{output, mask}` via `TupleType::getTypes`). The mask path is the sibling target `AwsNeuronDropoutMaskV1` @ `0x27eeaa`.

### 3.5 `printRmsNorm(Op*, bool)` @ `0x20e1a80`

One emitter, two targets: `bool=false` → `AwsNeuronRmsNorm` → `xla_op='mhlo.rms_norm'`; `bool=true` → `AwsNeuronRmsNormBackward` → `xla_op='mhlo.rms_norm_backward'`. The attribute sources are *split* — a detail worth pinning:

```text
"epsilon"     ← a ConstantOp OPERAND: ConstantOp::getValue
                → FloatAttr::getValueAsDouble → "%f"-formatted
"reduce_dims" ← backend_config (getBackendConfig → strtol)
"src_shapes"  ← operand shapes
```

> **CORRECTION —** `epsilon` comes from a `ConstantOp` *operand* (a `FloatAttr` double, `%f`-formatted), while `reduce_dims` comes from `backend_config` (`strtol`). They are **not** both read from `backend_config`. This whole-op RMSNorm path is distinct from the `MulRedSqrt` fusion-cluster denominator core (§6.2), which emits `op='MulRedSqrt'` instead.

### 3.6 `printResizeNearest` @ `0x20e5d50` / `printResizeBilinear` @ `0x20e27d0`

Both consume the same backend-config JSON parser `hilo::parseResizeNearestConfig` @ `0x21D89D0`. The scale is **implicit** in both — output spatial shape vs input spatial shape — never an explicit `scale=` kwarg.

```text
ResizeNearest  → "shape"=[…] (output spatial) · "dtype"=… ·
                 "kernel_name"=… · "kernel_config"={JSON}   [emitted as a KERNEL CALL]
ResizeBilinear → "align_corners"=True/False · "half_pixel_centers"=True/False
                 (output shape taken from result RankedTensorType, not a kwarg)
```

### 3.7 `printOffloadedMemCpy` @ `0x20bfa10` — the structural-op offload primitive

Not a custom-call emitter: this is the offloaded-DMA lowering for **structural** ops, called by `print<CopyOp>` @ `0x20c3c50`, `print<ReshapeOp>`, and `print<OptimizationBarrierOp>`. The offload path is gated — `print<CopyOp>` tests `byte ptr [op+0x2E] < 0` (the copy-elimination/offload flag) and only then takes the offloaded path. The `prefix` (a printer member at `[state+0x18]`) selects which DMA method name to emit:

```text
<lhs> = <prefix>.OffloadedMemCpy(srcs=[<src>], dsts=[<dst>], dtype=<t>, <meta>)\n
```

The `prefix` vocabulary is a 5-member family, each toggled by a `disable emit offloaded*` driver option of the Penguin copy-elimination pass: `offloadedmemcpy`, `offloadedmemcast`, `offloadedconcat`, `offloadedslice`, `offloadedtranspose`. One shared emitter; the prefix string picks the concrete Penguin DMA method.

---

## 4. Collective & Kernel Emission

### 4.1 The collective template — three arities, one master

Native `mhlo` collectives survive to emission (the HLO-side `ConvertCollectivesToCustomCall` pass runs in a *separate* binary; in `hlo2penguin` the native ops reach the printer). Every collective emits one `NeuronTensorOp` whose `kind=` attribute discriminates the collective. The templated body `printCollectiveOp<…>` exists in **exactly three instantiations** — verified by the demangled template signatures, which carry 0, 1, and 3 `std::pair<string,string>` parameters respectively:

```text
printCollectiveOp<>                @0x20e7cd0  (9041 B)  — 0-pair MASTER
    direct callers: AllReduce, CollectivePermute (no extra fixed attr-pair)
    internally handles ALL FIVE collective types via a kind switch.
printCollectiveOp<pair>            @0x20ea030  (9335 B)  — 1 extra pair
    print<AllGatherOp>     @0x20ec800 → ("all_gather_dim",     getAllGatherDim())
    print<ReduceScatterOp> @0x20ec500 → ("reduce_scatter_dim", getScatterDimension())
printCollectiveOp<pair,pair,pair>  @0x20d6e30  (9613 B)  — 3 extra pairs
    print<AllToAllOp>      @0x20d9480 → ("split_dimension",  …),
                                         ("concat_dimension", …),
                                         ("split_count",      …)
```

> **CORRECTION —** the arities are **0 / 1 / 3 pairs**, not "0/2/4". Each "pair" is one `std::pair<string,string>` attribute. There is no 2-pair or 4-pair instantiation (verified by enumerating all `printCollectiveOp<…>` template demanglings). There is also **no** `printBroadcastPartition` emitter — it does not exist; the only broadcast emitter is the ordinary shape op `print<mhlo::BroadcastInDimOp>` @ `0x20cf990`.

The 0-pair master assembles the common attribute set itself (per-op subset varies). Verbatim keys, each re-verified in the string pool:

| key (@ addr) | value source |
|---|---|
| `xla_op` @ `0x256085` | `getQuotedStr(op-mnemonic)` |
| `kind` @ `0x24a5c6` | per-op constant (table below) |
| `replica_groups` @ `0x22e3f8` | `print2DDenseIntElementAttrs(getReplicaGroups())` |
| `dtype` @ `0x2326c3` | `printType(elementType)` |
| `groupID` | `DictionaryAttr "neuron.groupID"` |
| `collective_type` | `frontend_attrs["collective_type"]` |
| `cc_type_hint` | dict-literal `{'op':'<reduce>'}` from frontend_attrs |
| `has_token` @ `0x246416` | `True`/`False` (token operand present) |
| `stream_id` @ `0x27acc5` | `stoi(frontend_attrs["stream_id"])`, decimal |

The `kind=` discriminator (verbatim, single-quoted) and the per-op extra pairs:

| HLO op | `kind=` | extra pair(s) | replica source |
|---|---|---|---|
| `mhlo.collective_permute` | `'Permute'` | (none — 0-pair) | `getSourceTargetPairs` |
| `mhlo.all_reduce` | (reduction*) | (none — 0-pair) | `getReplicaGroups` |
| `mhlo.all_to_all` | `'AlltoAll'` | `split_dimension`, `concat_dimension`, `split_count` | `getReplicaGroups` |
| `mhlo.all_gather` | `'AllGather'` | `all_gather_dim` | `getReplicaGroups` |
| `mhlo.reduce_scatter` | `'ReduceScatter'` | `reduce_scatter_dim` | `getReplicaGroups` |

\* **AllReduce / ReduceScatter** additionally carry the *reduction kind* via `getReduceOpStrFromOperation` @ `0x20af670` (§5.1), which walks the reduction computation root and yields `add`/`multiply`/`minimum`/`maximum`/`bitwise_and`/… (dtype-selected logical-vs-bitwise). This feeds the `cc_type_hint` / `collective_type` attrs.

> **GOTCHA —** `stream_id` is **mandatory.** A collective op missing the `stream_id` frontend-attr triggers a FATAL `llvm::outs() << "\nStream id missing from collective op!"` (@ `0x28f610`). The value is stamped upstream by the HLO `NeuronCollectiveStreamIdInjector`, parsed by `stoi`, and re-emitted as a decimal `stream_id=<N>`. Coded errors `NCC_PYP011`/`NCC_PYP012` cover malformed/unsupported collective shapes.

### 4.2 Collective-Matmul kernel — `printCollectiveMatmulKernel` @ `0x20cfff0`

Emits one `NeuronTensorOp` with a 4-pair attr vector; the matmul semantics live in `target_name` + a serialized `backend_config` dict:

```python
v22 = m0.NeuronTensorOp(srcs=[…], dsts=[…], op="NeuronTensorOp",
    xla_op='mhlo.custom_call', dtype=…,
    target_name="AwsNeuronCollectiveMatmul",
    backend_config={"rhs_contracting_dim":1, "tp_degree":8, "num_groups":1, "use_sb_to_sb":0},
    dl=…DebugLocation(…))
```

`backend_config` is fetched via `Operation::getInherentAttr` and run through `formatBackendConfigAsDict` @ `0x20afee0`, which turns the CSV producer string into a Python dict literal (delimiter `':'`, quote `'"'`, wrap `'{'…'}'`). The CSV keys (`rhs_contracting_dim`, `tp_degree`, `num_groups`, `use_sb_to_sb`) are produced HLO-side; this emitter is a pure serializer.

### 4.3 Native kernel — `printNativeKernel` @ `0x20e3f10`

The attention-kernel emitter. It does **not** go through the generic spine — it hand-builds a dedicated `.NativeKernel(` call:

```python
v30 = m0.NativeKernel(srcs=[…], dsts=[…], use_opaque_access=True,
    kernel_config='<backend_config_csv>', dl=…DebugLocation(…))
v30.add_dep_edge(…)                       # printControlDeps, 0..n
v30.aliasTensors(input=[…])               # in-place q/kv buffer aliasing
```

`.aliasTensors(` (str @ `0x20d95e`) is built from `CustomCallOp::getOutputOperandAliases()`: each alias resolves through `GetTupleElementOp::getIndex()` to the aliased input tensor name. This is the attention kernel's in-place q/kv handoff. The attention variant (e.g. `AttentionMMSoftmaxMM`) is carried inside `backend_config` and stored verbatim as `kernel_config='<csv>'` — no re-parse. Error codes `NCC_PYP007`–`010` guard the operand/alias/result-tuple structure.

### 4.4 The simpler kernel emitters

- **`printMLPNKIKernel` @ `0x20d0780`** — one `NeuronTensorOp` with a 3-pair vector (`xla_op='mhlo.custom_call'`, `dtype`, `target_name="AwsNeuronMLPNKI"`). **No** `backend_config` pair — the MLP op is emitted with an empty backend_config upstream, so the structure is fully captured by operands + target_name.
- **`printNeuronCustomOp` @ `0x20d2d70`** — parses a `;`-separated backend_config CSV into named attrs and emits `NeuronTensorOp(target_name='AwsNeuronCustomOp', function_name=…, name=…, lib_file_name=…, ulib_to_ucode_version=…, ulib_to_isa_version=…, dtype=…, shape=…)`. The `ulib_to_*_version` pairs pin the GPSIMD micro-lib versions; `path` segments are joined with `/`.
- **`printLNCShardingConstraint` @ `0x20e37e0`** — a dedicated `.LNCShardingConstraintOp(srcs=[…], dsts=[…], sharding=<backend_config>, target_name=…)` (no `dl=`). The Logical-Neuron-Core shard spec is consumed by the downstream Penguin layout middle-end.
- **`printSendRecv(Op*, bool)` @ `0x20e10f0`** — one `NeuronTensorOp`; the bool selects `xla_op='mhlo.send'` (@`0x211b53`) vs `'mhlo.recv'` (@`0x211b5d`). `peer_id=<N>` is the channel-id parsed from `getBackendConfig` and rendered decimal via `__to_chars_10` (CONFIRMED callee). Send/Recv pairing travels HLO `channel_id` → backend_config → Penguin `peer_id`. FATAL `NCC_PYP036` when channel_id is absent.

---

## 5. Nested-Computation Serialization (reduce / scatter / select-scatter)

The reduce-family emitters share one elegant trick: they **collapse a nested MLIR reduction region into a numpy ufunc name**, rather than emitting inner Python. The body's single binary op *is* the function identity.

### 5.1 The mapper — `getReduceOpStrFromOperation` @ `0x20af670`

`extractReduceFunction` @ `0x20afca0` → `extractReduceFunctionBlock` @ `0x20afbe0` walks the reduction block and finds the first op whose TypeID is in `{AddOp, MulOp, MinOp, MaxOp, OrOp, AndOp}`, then `getReduceOpStrFromOperation` maps it to a ufunc name (table re-verified against the string pool):

```text
mhlo::AddOp      → "add"        mhlo::MinOp  → "minimum"
mhlo::MulOp      → "multiply"   mhlo::MaxOp  → "maximum"
mhlo::DivOp      → "divide"     mhlo::AndOp  → isLogicalBoolean? "logical_and" : "bitwise_and"
mhlo::SubtractOp → "subtract"   mhlo::OrOp   → "logical_or"  / "bitwise_or"
mhlo::RemOp      → "fmod"       mhlo::XorOp  → "logical_xor" / "bitwise_xor"
<other / void>   → ""           (logical vs bitwise selected by i1 element type)
```

`extractScatterFunction` (lambda `@0x20afb60`) walks the scatter *update* computation with the same mapper. `printReduceDefaultInit` @ `0x20bc090` supplies the reduction *identity* as a Python literal per fn × dtype (`add→0`, `multiply→1`, float `0.0`/`1.0`, min/max → dtype extreme), feeding `init_value` / `scatter_ident`.

### 5.2 `print<ReduceOp>` @ `0x20dc5a0`

```python
v12 = m0.NeuronTensorOp(srcs=[v3, v5], dsts=[v12], op=np.add,
    xla_op='np.add', init_value=0.0, reduce_dims=[1], dl=m9.DebugLocation('sum', 7))
```

The `add`/`max`/`min` body is *fully captured* by `op=np.<fn>` + `xla_op='np.<fn>'` + `init_value` — **no inner-body Python is emitted.** `op=` (the keyword `optional<string>`) and `xla_op=` both carry the `<np_alias>.<fn>` reference; `getImport("numpy")` mints the `np` alias, `ReduceOp::getDimensions()` renders `reduce_dims=[…]`.

### 5.3 `print<ReduceWindowOp>` @ `0x20d5ff0` (pooling)

```python
v8 = m0.NeuronTensorOp(srcs=[…], dsts=[v8], op=np.maximum,
    xla_op='np.maximum', use_init_operand=False,
    stride=[2,2], padding=[0,0,0,0], window_shape=[3,3], dl=…)
```

Five attrs `[xla_op, use_init_operand, stride, padding, window_shape]`, the window descriptors via `printTuple<long>` from `getWindowStrides`/`getPadding`/`getWindowDimensions`. Max-pool ⇒ `fn="maximum"`; avg-pool ⇒ `fn="add"` + a downstream divide.

### 5.4 `print<ScatterOp>` @ `0x20cbc70`

Serializes the full `ScatterDimensionNumbers` tuple (8 attrs) plus the update-comp ufunc:

```python
v20 = m0.NeuronTensorOp(srcs=[…], dsts=[v20], op=np.add,
    xla_op='np.add', index_vector_dim=1, update_scatter_dims=[0],
    update_window_dims=[1], inserted_window_dims=[0],
    scatter_dims_to_operand_dims=[0], unique_indices=False,
    scatter_kind=np.add, dl=…)
```

Keys re-verified: `index_vector_dim`@`0x22a622`, `update_scatter_dims`@`0x2870d8`, `update_window_dims`@`0x25e4b4`, `inserted_window_dims`@`0x25609e`, `scatter_dims_to_operand_dims`@`0x211b13`, `unique_indices`@`0x226363`, `scatter_kind`@`0x22a615`.

### 5.5 `print<SelectAndScatterOp>` @ `0x20de190` — the dual-region emitter

The richest emitter (5359 B, 6 fatal codes `NCC_PYP029`–`034`); it builds the statement *directly* via a `raw_string_ostream`, not through the spine. It is the only emitter with **two** nested-comp extractions:

1. **Select region** must be a single `mhlo.compare`; its `getComparisonDirection()` → a comparison ufunc name (`greater`/`less`/`greater_equal`/…) → `xla_op='<np>.<cmp>'`.
2. **Scatter region** → `extractReduceFunctionBlock` → scatter ufunc + `printReduceDefaultInit` → `scatter_ident` (@`0x26a7f4`).
3. Window descriptors (`window_shape`/`stride`/`padding`) woven in via `printTuple<long>`; `src_shape` (@`0x26e581`) carries the operand shape.

Guards `NCC_PYP030`/`031` enforce the "select must be a Compare with BlockArgument lhs/rhs" precondition.

---

## 6. Fusion Emission — `print<mhlo::FusionOp>` @ `0x20f5090`

`printOperation` routes `mhlo.fusion` → `print<FusionOp>`, the **FusionKind switchboard**. Its flow (CONFIRMED callee graph: `getInherentAttr`, `getScalar`, `defScalar`, all four fusion printers):

```c
// print<mhlo::FusionOp> @0x20f5090
kind = op->getInherentAttr("FusionKind").getValue();   // StringAttr, "FusionKind" @0x266931

// PRE-REGISTER each fusion result scalar so inlined body ops can reference it:
for (Value r : op->getResults())
    defScalar(r, getScalar(r));                          // binds output names BEFORE emission

switch (kind) {                                          // 8-way std::string::compare cascade
  case "ScheduleFusion": printScheduleFusionOp(op);                       break;  // @0x20f4ce0
  case "MulRedSqrt":     printMulRedSqrtFusionOp(op);                     break;  // @0x20f1f60
  case "DotLogistic":    printDotLogisticFusionOp(op);                    break;  // @0x20f3050
  case "Expm1": case "Log1p": case "Elementwise":
                         printArbitraryFusionOp(op, "Elementwise", kind); break;  // @0x20f3f00
  case "DotSoftmax":     printArbitraryFusionOp(op, "DotSoftmax",  kind); break;
  default:               FATAL NCC_PYP053;   // MhloToPythonPrinter.cc:3727
}
```

The FusionKind value literals are all verbatim `.rodata`, re-verified: `ScheduleFusion`@`0x25e519`, `MulRedSqrt`@`0x2560e7`, `DotLogistic`@`0x215ec2`, `Expm1`, `Log1p`, `Elementwise`@`0x2669e3`, `DotSoftmax`@`0x25e528`.

> **NOTE —** the dispatcher **pre-registers fusion result scalars** (`getScalar`→`defScalar`) before any emitter runs, so that the three header-emitting printers — which inline the fusion *body* afterward — bind correctly to the fusion's output names.

### 6.1 `printArbitraryFusionOp(Op*, StringRef name, StringRef kind)` @ `0x20f3f00`

The generic header-then-inline pattern. It emits a grouping `NeuronTensorOp` with a **dual string carriage** and then re-iterates the body region calling `printOperation` per inner op (skipping the `mhlo.return`):

```text
("xla_op",         "'mhlo.fusion'")        ; "mhlo.fusion" @0x23a789
("op",             "'<name>'")             ; arg1: "Elementwise" | "DotSoftmax"
("hilo_fusion_op", "'<kind>'")             ; arg2: the ACTUAL FusionKind string
```

> **GOTCHA —** the *argument order* matters: `arg1` is the **canonical** kind (`"Elementwise"` / `"DotSoftmax"`), `arg2` is the **actual** FusionKind. So `Expm1`, `Log1p`, and `Elementwise` all surface as `op="Elementwise"` while `hilo_fusion_op` preserves the real kind. A fusion is therefore emitted as *one grouping line + the verbatim inlined body*.

### 6.2 `printMulRedSqrtFusionOp` @ `0x20f1f60` (RMSNorm denominator)

Same header-then-inline structure, plus a structural attr from the inner `mhlo.reduce`:

```python
v15 = m0.NeuronTensorOp(srcs=[…], dsts=[v15],
    xla_op='mhlo.fusion', op='MulRedSqrt', hilo_fusion_op='MulRedSqrt',
    reduce_dims=[2], dl=…)
# … inlined body: the inner mul / reduce / rsqrt ops …
```

`reduce_dims` is read from the captured inner `ReduceOp::getDimensions()`. This is the fusion-cluster RMSNorm denominator path — distinct from the whole-op `printRmsNorm` (§3.5).

### 6.3 `printDotLogisticFusionOp` @ `0x20f3050`

```python
v18 = m0.NeuronTensorOp(srcs=[…], dsts=[v18],
    xla_op='mhlo.fusion', op='DotFusion', hilo_fusion_op='DotFusion', dl=…)
# … inlined: the inner dot_general, logistic, mul ops carry their own dims …
```

> **CORRECTION —** the Penguin surface name is `op='DotFusion'` (str @ `0x25a294`, len 9), **not** `'DotLogistic'`. `DotLogistic` is the *HLO-side FusionKind* (the dispatch key only); the emitted Python `op=` literal is `DotFusion`. No dot-dim attribute is emitted by the header — the inner `DotGeneralOp` inlines as its own statement carrying its own dims.

### 6.4 `printScheduleFusionOp` @ `0x20f4ce0` (transparent)

The only **header-less** fusion printer. It emits no `NeuronTensorOp` grouping line — it is a pure transparent inliner:

```c
// printScheduleFusionOp @0x20f4ce0
ret = hilo::getOperationReturnOp(op);
if (!ret) FATAL NCC_PYP052;                 // MhloToPythonPrinter.cc:3687
for (Operation &inner : op.body())          // skip the ReturnOp
    if (&inner != ret) printOperation(&inner);   // emit rescheduled collectives inline
for (Value v : ret->getOperands())          // rewire live-outs:
    defScalar(v, getScalar(v));             // consumers bind to inner collective output names
```

`ScheduleFusion` materializes the two rescheduled collectives (e.g. `AllGather` + `ReduceScatter`) inline in their moved order; Walrus/Penguin then turns that adjacency into BIR semaphore-pipelined overlap. It is a pure physical-reschedule marker.

---

## 7. Adversarial Self-Verification

The five strongest claims on this page, each re-challenged against the binary in this pass:

1. **All 30 emitter symbols exist at the cited addresses with the stated demangled names.** — CONFIRMED. A single `*_functions.json` lookup over all addresses returned every `mlir::MhloToPythonPrinter::*` symbol with matching name; the only deltas were *larger* sizes for four kernel emitters (now corrected in §1).

2. **`printDotOp` receives pre-decomposed dim lists; the DotGeneral dispatcher calls all four `DotDimensionNumbersAttr` accessors.** — CONFIRMED. The callee set of `print<DotGeneralOp>` @ `0x20c6d60` contains `getDotDimensionNumbers`, all four `getLhs/RhsBatching/ContractingDimensions`, and the tail-call to `printDotOp`. `printDotOp`'s own callees include 4× `printList<long>` and `printOperandsAndAttributes`.

3. **`DotLogistic` fusion emits `op='DotFusion'`, not `'DotLogistic'`.** — CONFIRMED. The string `"DotFusion"` exists at `0x25a294`; `"DotLogistic"` at `0x215ec2` is the FusionKind dispatch key. Both are distinct live strings — the surface/dispatch split is real, not a transcription slip.

4. **`getReduceOpStrFromOperation` maps reduce ops to the listed numpy ufunc names.** — CONFIRMED. `add`, `multiply`, `minimum`@`0x21a2a6`, `maximum`@`0x2870ab`, `subtract`@`0x24652e`, `divide`@`0x23a6e6`, `bitwise_and`@`0x26693e`, `logical_and`@`0x22e3d9` all present at distinct addresses. The bitwise/logical split (i1 selector) is consistent with two live string pairs.

5. **The collective template has exactly three arities (0/1/3 pairs); no `printBroadcastPartition`.** — CONFIRMED. The three `printCollectiveOp<…>` demangled signatures carry 0, 1, and 3 `std::pair<string,string>` parameters; no other instantiation exists. `printBroadcastPartition` returns no symbol; `print<BroadcastInDimOp>` @ `0x20cf990` is the only broadcast emitter.

**INFERRED / not byte-pinned** (honestly flagged): the `printTopK` `axis` derivation rule (operand-rank-trailing-default vs an explicit key) — MED; the `use_init_operand` True/False gate predicate on `ReduceWindow` — HIGH (confirmed as an attr, exact gate not mapped); `printReduceDefaultInit`'s full per-fn × dtype identity table (`add→0`, `multiply→1` confirmed; min/max → dtype-extreme inferred) — MED-HIGH; the SelectAndScatter select-direction → name strings (inferred to match the `mapXla2PgDir` family) — MED. None of these affect the emitted statement *shape*.

---

## 8. Cross-References

- **[4.43 — Penguin Emission Driver & Elementwise Emitters](mhlo-to-python-printer-driver.md)** — the shared spine (`printOperandsAndAttributes`, `printSrcs`/`printDsts`/`printMeta`), the module skeleton, name/scalar helpers, and the proof that the output is Python text. This page builds on that driver and does not re-prove the textual-Python finding.
- **4.45 — StableHLO Printer & PenguinizeFunctions** — the byte-identical StableHLO twins (`printDotOp` @ `0x21600f0` with its extra precision string, the composite-op dispatch with `NCC_ISPP054`, the MX-path composite emitters) that mirror every emitter here on the `stablehlo` side.
- **[Part 5 — the Penguin op set](../index.md)** — the `Function` / `Tensor` / `NeuronTensorOp` / `DependencyEdge` objects that the Python text emitted here *constructs* at runtime.
- **[0.7 — matmul walkthrough](../index.md)** — the end-to-end dot lowering whose final emission is `printDotOp` (§3.1).
