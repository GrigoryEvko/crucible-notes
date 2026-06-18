# Penguin High-Level Operator (TensorOp) Family

> *All symbols on this page apply to neuronx-cc 2.24.5133.0+58f8de22 (cp310). Evidence is the `__pyx_pw_…` method-wrapper symbols and embedded docstring/format strings in `neuronxcc/starfish/penguin/ir/Operator.cpython-310-x86_64-linux-gnu.so` (Cython, shipped with debug_info, not stripped). Other versions/ABIs will differ in offsets but not in the class roster.*

## Abstract

`Operator` (`ir/Operator.py` → `Operator.so`) is the **HLO-facing op layer** of the Penguin IR: the subclass of `Instruction` that carries *op semantics* — what the op computes — before the middle-end tiles it and `BirCodeGenLoop` lowers each node to an engine instruction. This is the node set the C-strand Python printer emits (the `hlo2penguin` `MhloToPythonPrinter` writes a Python constructor call per op; see [Part 4 — C-strand emitters](../hlo-opt/mhlo-to-python-printer-heavy.md)) and the set NKI codegen builds when it materializes a kernel ([Part 6 — NKI codegen](../nki/bir-codegen-loop.md)).

Every `Operator` inherits the full `Instruction` contract — `operands` (each a `Tensor`+`Access` pair), `results` (`Tensor`s), `attrs`, `predicates` (`AffinePredicate` guards), and `axes` (the `AffineAxis` loop-nest) — and adds two things: **op-specific axis roles** and **op-specific scalar params**. The axis-role model is the distinctive part: an `Operator` does not name "the M dim" or "the K dim"; it tags each `AffineAxis` of the loop-nest with a *role* (`contract` / `lhs_free` / `rhs_free` / `reduce` / `batch`), and the layout solver and tiler ([§5 axis model](axis-loop-model.md)) read those roles to assign each axis to a Partition / Free / Block hardware dimension. A matmul is therefore "a contraction over the axes tagged `contract_axes`", not a fixed-arity `dot(A,B)`.

The family splits into elementwise (`UnaryOp`/`BinaryOp`/`SelectOp`/…), reduction & contraction (`ReduceOp`/`TensorContractOp`/`ScaledTensorContractOp`), fused macros (`Softmax*`/`BatchNorm*`/`DropoutMaskOp`), the FP8 microscaling node (`QuantizeMXOperator`), collectives, fusion regions, and the custom/NKI-kernel bridge. This page enumerates the node set and reproduces the op-specific fields of each, with `TensorContractOp` (the matmul) and `QuantizeMXOperator` given in full.

For reimplementation, the contract is:

- The `Operator` base contract every op shares, and the four module-level helpers (`custom_op`, `act_identity`, `make_cast`, `used_as_*_axis`) the tiler/scheduler call to classify axes.
- The `TensorContractOp` axis model (`contract_axes` / `lhs_free_axes` / `rhs_free_axes` / `batch_mm_axes`) and its params (`is_quantized`, `is_swapped` / `swap_operands`, `reduce_op`).
- The op-specific fields of the remaining families (reduce, softmax, quantize-MX, collective, fusion, custom/kernel) — enough to know what each node carries before it is tiled and lowered.

| | |
|---|---|
| **Module** | `penguin/ir/Operator.cpython-310-…so` (≈5.28 MB, debug_info, not stripped) |
| **Base class** | `Operator` ⊂ `Instruction` ⊂ `User`+`ComputeValue` ([§5.1 node model](ir-node-model.md)) |
| **Matmul node** | `TensorContractOp` — 29 methods; axis roles `contract`/`lhs_free`/`rhs_free`/`batch` |
| **Op-shared method shape** | `__init__`, `operands`, `indices_dfs`, `src_indices_dfs`, `replaceUseOfWith`, `rhs_str`, `serialize`, `verify`, `verifyOperandType`, `update_axes`/`update_indices` |
| **Emitted by** | `MhloToPythonPrinter` (Python ctor per op), [Part 4](../hlo-opt/mhlo-to-python-printer-heavy.md) |
| **Lowered by** | `BirCodeGenLoop` (one `codegen<Op>` per tiled Inst), [Part 6](../nki/bir-codegen-loop.md) |

---

## The Operator Base Contract

### Purpose

`Operator` is the thin layer that turns a generic `Instruction` into an op with *semantics*. It adds no new SSA machinery — operands, results, use-lists, predicates, and the loop-nest all come from `Instruction` — only the per-op verify/serialize hooks and the axis-role vocabulary.

### Method Map

Confirmed `__pyx_pw_…Operator_<idx><method>` wrappers in `Operator.so`:

| Symbol group | Members | Role | Confidence |
|---|---|---|---|
| Base methods | `__init__`, `rhs_str`, `serialize`, `verify`, `verifyOperandType` | per-op text + structural verify hooks every subclass overrides | CONFIRMED |
| Module helpers | `custom_op`, `act_identity`, `make_cast` (`.cast`), `_isint` | op-factory + activation-identity + cast-builder free functions | CONFIRMED |
| Axis-role queries | `used_as_reduce_axis`, `used_as_contract_axis`, `used_as_lhs_free_axis`, `used_as_rhs_free_axis`, `used_as_reduce_like_axis` | classify one `AffineAxis` of an op into its hardware role | CONFIRMED |

> **NOTE —** the `used_as_*_axis` free functions are how the layout solver and tiler ([§5 axis model](axis-loop-model.md), D-U02 P/F/B vocabulary) read an op's intent **without** depending on its concrete class. They answer "is this axis a contraction dim of *this* op?" for any op, so the tiler can cut contract axes to the 128-lane PE depth and free axes to the tile width uniformly. A reimplementation that hard-codes per-op dim names instead of exposing these role predicates will not interoperate with the shared tiler.

### Considerations

`Operator` does not own the operand/result lists; those are `Instruction.operands`/`Instruction.results`. The `*_or_none` accessors seen on subclasses (`lhs_load_or_none`, `rhs_load_or_none`) reflect that an operand slot may be unbound during construction — the printer/`IRBuilder` fills them, so a node can exist transiently with `None` operands. Verify only after `update_axes`/`update_indices` has run.

---

## Reduction and Contraction

### TensorContractOp — the Matmul Node

`TensorContractOp` is the PE-array contraction node — the Penguin matmul. It is the node `mhlo.dot`/`dot_general` lowers to (`printDotOp`, [Part 4](../hlo-opt/mhlo-to-python-printer-heavy.md)) and that `BirCodeGenLoop` lowers to `codegenMatMulOp` / `codegenMatMulMXOp` / `codegenMatMulSparseOp` ([Part 6](../nki/bir-codegen-loop.md); the sparse variant is detailed at [6.8.8 — MatMulSparseOp](../nki/matmul-sparse.md)).

All 29 method wrappers below are CONFIRMED from `__pyx_pw_…TensorContractOp_<idx><name>` symbols in `Operator.so`.

```c
// class TensorContractOp(ReduceLikeOp):   // Operator.so, 29 pyx-wrapped methods
//
// AXIS MODEL — each AffineAxis of the op's loop-nest is tagged with a role.
// The matmul shape is NOT (M,K)x(K,N); it is "contract over contract_axes,
// hold lhs_free/rhs_free as the two output free dims, replicate over batch".
struct TensorContractOp {
    // --- axis roles (each a list of AffineAxis; see used_as_*_axis above) ---
    AffineAxis[] contract_axes;     // the reduction (K) dims fed into the PE array
    int          n_contract_axes;   // == len(contract_axes)
    bool         contract_axes_empty;
    AffineAxis[] lhs_free_axes;      // the LHS (stationary) output free dim(s)  (M)
    int          n_lhs_free_axes;
    bool         lhs_free_axes_empty;
    AffineAxis[] rhs_free_axes;      // the RHS (moving)     output free dim(s)  (N)
    int          n_rhs_free_axes;
    bool         rhs_free_axes_empty;
    AffineAxis[] batch_mm_axes;      // batched-matmul outer dims (replicate the GEMM)

    // --- operands: two access bindings, not raw tensors ---
    Access  lhs_load;               // (Tensor, Access) for the LHS read
    Access  rhs_load;               // (Tensor, Access) for the RHS read
    Access  lhs_load_or_none;       // None-tolerant accessor (operand may be unbound)
    Access  rhs_load_or_none;
    Subscripts lhs_subscripts;      // the LHS index expression

    // --- params (op semantics) ---
    bool    is_quantized;           // quantized MM: lhs_scale/rhs_scale/lhs_zero_point present
    bool    is_swapped;             // docstring: "whether TC underwent stationary-streaming swap"
    ReduceOp reduce_op;             // the accumulation op (add for plain GEMM)

    // --- methods ---
    void  swap_operands();          // perform the stationary<->streaming operand swap
    void  infer_axes();             // derive axis roles from operand shapes
    void  refresh_axes();           // recompute roles after a rewrite
    Tensor evalMatMul();            // constant-fold the contraction
    // + Operator base: __init__ operands rhs_str serialize verify
    //   verifyOperandType src_indices_dfs replaceUseOfWith update_axes eval
};
```

> **QUIRK — `is_swapped` / `swap_operands` is the stationary-vs-streaming choice, not operand commutativity.** The verbatim docstring is *"return whether TC underwent stationary-streaming swap"*. The systolic PE array holds one operand **stationary** (loaded into the array) while the other **streams** through; which of LHS/RHS is stationary changes the load cost and the achievable tile shape. `swap_operands()` flips that assignment and re-tags `lhs_free_axes`↔`rhs_free_axes` accordingly. Treating it as a mathematical A·B → B·A transpose (it is not — the contraction is unchanged) will mis-derive the output layout.

> **NOTE —** `TensorContractOp` derives from `ReduceLikeOp` (the contraction *is* a reduction over `contract_axes`). It therefore also answers `has_reduce`, `is_reduce_add`, `enumerate_reduce_axes`, and `is_conv` (a convolution is a contraction with windowed access) — see the `ReduceLikeOp` map below.

### ScaledTensorContractOp — Scaled Matmul

`ScaledTensorContractOp` is a matmul with per-operand scale tensors (the FP8/blockwise-scaled GEMM the SHLO scale-matmul path emits via `printScaleMatmult`). Confirmed methods: `__init__`, `operands`, `serialize`, `rhs_str`, `replaceUseOfWith`, `swap_operands`. It shares the `TensorContractOp` axis model and adds the `lhs_scale`/`rhs_scale` operand bindings (the scale reads), and carries the same `swap_operands` stationary-streaming control.

### ReduceOp / ReduceLikeOp

`ReduceLikeOp` is the shared base for everything that contracts an axis (plain reductions, matmul, conv, BN-stats). `ReduceOp` is the elementwise reduction.

```c
// class ReduceLikeOp(Operator):           // Operator.so
struct ReduceLikeOp {
    bool   reduce_axes_empty;
    bool   has_reduce, is_reduce_add;       // is this a sum-reduction (the fast PE path)?
    bool   is_conv;                         // a windowed contraction (conv as reduce)
    int[]  reduce_windows_size;             // window extents for reduce_window/conv
    LoopNest canonical_loopnest;
    iter   enumerate_reduce_axes();         // the axes being reduced
    iter   enumerate_parallel_axes();       // the axes carried through unreduced
    // + indices_dfs src_indices_dfs reversed_result_indices verify
};

// class ReduceOp(ReduceLikeOp):
struct ReduceOp {
    AffineAxis[] reduce_axes;               // split downstream into
                                            //   new_reduce_partition_axes / new_reduce_free_axes
    Scalar       identity;                  // the reduction identity (0 for add, -inf for max)
    void   set_input(...);
    Tensor eval();                          // constant-fold
    Tensor eval_depth_without_reduce_axes();
};
```

CONFIRMED: `ReduceLikeOp` wrappers `reduce_axes_empty`, `has_reduce`, `is_reduce_add`, `is_conv`, `reduce_windows_size`, `canonical_loopnest`, `enumerate_reduce_axes`, `enumerate_parallel_axes`, `indices_dfs`, `src_indices_dfs`, `reversed_result_indices`, `verify`; `ReduceOp` wrappers `identity`, `set_input`, `eval`, `eval_depth_without_reduce_axes`, `update_axes`, plus the base shape. Upward: `mhlo.reduce`/`reduce_window` → `ReduceOp`/`ReduceWindowTensorOp` ([Part 4](../hlo-opt/mhlo-to-python-printer-heavy.md)).

---

## Elementwise

`NullaryOp` / `UnaryOp` / `UnaryCmpOp` / `BinaryOp` / `BinaryLikeOp` / `CmpOp` / `ElementwiseOp` / `SelectOp` — all CONFIRMED as classes in `Operator.so`. These carry no axis roles (elementwise ops apply per-element across the full loop-nest); their semantics is the **opcode**, held in the shared `attrs` dict as an `ALUOpcode`/`TSOpcode` ([Part 5 attr model](ir-node-model.md), `targets.Opcodes`).

```c
// class UnaryOp(Operator):                 // Operator.so
struct UnaryOp {
    // opcode lives in Instruction.attrs (ALUOpcode / activation func id)
    bool   is_cast();                       // is this a dtype cast?
    Value  stripCast();                     // peel the cast to the source value
    // + indices_dfs operands rhs_str serialize verify verifyOperandType
};

// class SelectOp(Operator):                // the ternary select (predicate ? a : b)
struct SelectOp {
    Predicate pred;                         // the boolean selector operand
    void replacePred(Predicate p);          // CONFIRMED — rewrite the selector
    // + indices_dfs operands rhs_str serialize verifyOperandType
};

// class ElementwiseOp(Operator):
struct ElementwiseOp {
    Value eval();                           // CONFIRMED — the constant-folder for the family
};
```

CONFIRMED methods: `UnaryOp.{is_cast, stripCast, indices_dfs, operands, rhs_str, serialize, verify, verifyOperandType}`; `SelectOp.replacePred`; `ElementwiseOp.eval`. The cast model lives on `UnaryOp` (`is_cast`/`stripCast`), matching `Instruction.is_cast`/`stripCast` and the module helper `make_cast`. Upward: `mhlo` elementwise add/mul/… → `BinaryOp`/`UnaryOp`/`ElementwiseOp` (`printUnary`/`printBinaryTensorOp`/`printTernaryTensorOp`).

---

## Fused Macros

The softmax/batchnorm/dropout nodes are single ops that stand for a multi-instruction macro; the middle-end (`MacroGeneration`) expands each into a tiled loop body.

### Softmax Family

`SoftmaxOp` / `SoftmaxDxOp` / `SoftmaxExpOp` / `SoftmaxRSumOp` — all CONFIRMED classes in `Operator.so`. `SoftmaxOp` carries `compute_dtype` (CONFIRMED wrapper) and a `reduce_axes` set (the axis the softmax normalizes over); `SoftmaxExpOp`/`SoftmaxRSumOp` are the split exp / row-sum sub-steps. Upward: `AwsNeuronSoftmax`/`Backward` → `SoftmaxOp`/`SoftmaxDxOp`.

```c
// class SoftmaxOp(Operator):               // Operator.so
struct SoftmaxOp {
    DType  compute_dtype;                    // CONFIRMED — accumulation precision (exp/sum in fp32)
    AffineAxis[] reduce_axes;                // the normalization axis
    // + indices_dfs operands rhs_str serialize verify
};
```

### Dropout / BatchNorm / Rng

- `DropoutMaskOp` (CONFIRMED class): the dropout-mask generator — fields `p` / `is_keep_rate` / `set_keep_rate` / `data` (INFERRED owner from D-U08 §8.3; class CONFIRMED in pool).
- `BatchNorm*` family lives in the sibling `ir/BatchNorm.cpython-310-…so`: `BNStatsOp` / `BNAggrOp` / `BNReduceLikeOp` + `BatchNorm{Tensor,Training,Gradient,Backprop,MeanVar}Op`. Lowered to `codegenSundaBNStats`/`BNAggr`/`BNGradient`/`BNBackprop` ([Part 6](../nki/bir-codegen-loop.md)).
- `RandOp` / `RngUniformTensorOp` live in `ir/RngOp.cpython-310-…so`.

> **GOTCHA —** the softmax/BN/dropout/rng *families* are split across **multiple** `ir/*.so` modules, not all in `Operator.so`. `SoftmaxOp` and `DropoutMaskOp` are in `Operator.so`; `BatchNorm*` is in `BatchNorm.so`; `RandOp` is in `RngOp.so`. A reimplementer sweeping only `Operator.so`'s pyx symbols will miss the BN and Rng nodes.

---

## QuantizeMXOperator — the FP8 Microscaling Node

`QuantizeMXOperator` produces an MX (microscaling) FP8 tensor: a packed FP8 value stream **plus** a per-block scale tensor. It is a two-output op. CONFIRMED methods and docstrings below are from `Operator.so` (`__pyx_pw_…QuantizeMXOperator_…` and embedded format/docstring strings).

```c
// class QuantizeMXOperator(Operator):      // Operator.so, 14 methods
struct QuantizeMXOperator {
    Tensor  data;                           // the input to quantize
    DType   quant_dtype;                    // docstring: "Output dtype for quantized data (FP8_x4)."
    DType   scale_dtype;                    // the per-block scale dtype (FP8 E4M3)
    DType   mx_dtype;                        // = float8_e4m3fn  (the MX element format)
    AffineAxis[] reduce_partition_axes;     // the block axis the scale is computed over

    DType   get_output_dtype(int idx);
    Indices get_output_indices(int idx);    // idx 0 = quantized FP8_x4 packed value
                                            // idx 1 = scale output
    void    set_input(...);
    void    update_axes();
    // + indices_dfs operands rhs_str replaceUseOfWith
};
// Serialize (verbatim, from .rodata):
//   qmx = QuantizeMXOperator(data=src, mx_dtype=float8_e4m3fn,
//         reduce_partition_axes=[pack], reduce_axes={...})
// Output[0]: "Quantized FP8_x4 packed value (reduced over elem dimension)"
```

CONFIRMED wrappers: `quant_dtype`, `scale_dtype`, `get_output_dtype`, `get_output_indices`, `indices_dfs`, `operands`, `rhs_str`, `replaceUseOfWith`, `__init__`. The verbatim strings `"Output dtype for quantized data (FP8_x4)."`, `float8_e4m3fn`, `"Quantized FP8_x4 packed value (reduced over elem dimension)"`, and the `qmx = QuantizeMXOperator(...)` serialize template are all present in `Operator.so`. Upward: SHLO QuantizeMX → `QuantizeMXOperator` (`printQuantizeMX`); lowered to `codegenQuantizeMXOp`.

---

## Collectives, Fusion, Custom / Kernel

These op groups carry op semantics for distribution, region-fusion, and external-kernel embedding. The nodes live in sibling `ir/*.so` modules but are part of the same `Operator`-rooted family.

| Group | Module | Key nodes | Op-specific fields | Confidence |
|---|---|---|---|---|
| Collective | `ir/CollectiveOp.so` | `AllGatherOp`, `AllReduceOp`, `AlltoAllOp`, `ReduceScatterOp`, `CollectivePermute(Reduce)Op`, `CollectiveComputeOp`, `SendOp`/`RecvOp`, `ShardOp` | `all_gather_dim`/`dim` (concat/scatter dim), `replica_groups`, `channel_id`, `reduce_op` | CONFIRMED (D-U08 §8.5) |
| Fused elementwise+CC | `Operator.so` | `ElementwiseAllGatherOp`, `ElementwiseAllReduceOp`, `ElementwiseCustomCall` | inherit collective + elementwise fields | CONFIRMED (classes in `Operator.so`) |
| Fusion region | `ir/FusionOp.so` | `FusionOp` ⊃ `ElementwiseFusionOp`/`ReduceFusionOp`/`TensorContractFusionOp` | `formal_inputs`/`formal_outputs`, `local_insts`/`local_tensors`, `default_loopnest_shape` — a **nested Function** treated as one schedulable unit | CONFIRMED (D-U08 §8.6) |
| Custom / opaque | `ir/CustomOp.so`, `ir/OpaqueOp.so` | `CustomOp`, `OpaqueOp` | opaque external call (`OpaqueAccess` non-affine access) | CONFIRMED |
| Native / NKI kernel | `ir/NativeKernel.so` | `NativeNkiKernel`, `Internal/ExternalNativeNkiKernel(+Klir)`, `BIRKernel`, `MLPKernel`, `RMSNormQuantKernel`, `NormQKV`, `AttentionMMSoftmaxMM`, `BackwardsAttention` | the NKI→Penguin embedding nodes ([Part 6](../nki/bir-codegen-loop.md) builds these) | CONFIRMED (D-U08 §8.7) |
| Index / predicate value | `ir/IndexValue.so` | `IndexValueOp`/`IndexValueBaseOp`, `PredicateValueOp`, `AggregateValueOp` | materialize a loop-index or an `AffinePredicate` as a tensor value (argmax/topk/mask) | CONFIRMED (D-U08 §8.8) |

> **NOTE —** `ElementwiseAllGatherOp`, `ElementwiseAllReduceOp`, and `ElementwiseCustomCall` are CONFIRMED **in `Operator.so`** (their pyx wrappers appear there), not in `CollectiveOp.so` — they are the *fused* elementwise+collective ops, so they live with the elementwise family. The pure collectives (`AllGatherOp`/`AllReduceOp`/…) are in `CollectiveOp.so`.

---

## Lowering Path

Each `Operator` is built by the C-strand printer, tiled by the middle-end, then lowered one-Inst-at-a-time by `BirCodeGenLoop` ("Generate Backend IR from tensoriser IR at the TongaISAInst level"). The op-node → bir-inst correspondence for this family:

```text
mhlo.dot / dot_general      → TensorContractOp     → codegenMatMulOp / MatMulMXOp / MatMulSparseOp
mhlo elementwise            → Unary/Binary/Elementwise → codegenActivationOp / TensorTensorOp / ...
mhlo.reduce / reduce_window → ReduceOp / ReduceWindowTensorOp → codegenTensorReduceOp / PartitionReduceOp
AwsNeuronSoftmax(/Backward) → SoftmaxOp / SoftmaxDxOp → (macro-expanded) codegen of tiled body
QuantizeMX (SHLO)           → QuantizeMXOperator    → codegenQuantizeMXOp
scale matmul (SHLO)         → ScaledTensorContractOp → codegenMatMulMXOp
all_reduce/all_gather/...   → AllReduceOp/AllGatherOp/... → codegen{AllReduce,AllGather,...}Op
mhlo.fusion / composite     → FusionOp (+variants)  → lowered as a nested Function
AwsNeuronCustomOp / NKI     → CustomOp / NativeNkiKernel → codegen{BIRKernel,MLPKernel,...}
```

> **QUIRK —** the op-specific *fields* documented here (axis roles, `is_quantized`, `compute_dtype`, `reduce_partition_axes`, …) are **consumed during tiling**, not at lowering. By the time `BirCodeGenLoop` runs, the contraction/reduction/normalization axes have already been split into partition/free/block tiles and rewritten as a `TileAccess` ([§5 access model](ir-node-model.md)). `codegenMatMulOp` sees tiled accesses, not `contract_axes`. A reimplementation must read the axis roles **before** the tiler erases them.

---

## Related Components

| Name | Relationship |
|---|---|
| `Instruction` ([§5.1](ir-node-model.md)) | base class — supplies operands/results/attrs/predicates/axes the Operator shares |
| `AffineAxis` ([§5 axis model](axis-loop-model.md)) | the loop-axis the `used_as_*_axis` role queries classify |
| `Access` / `TileAccess` ([§5.1](ir-node-model.md)) | the operand binding (`lhs_load`/`rhs_load`) and its tiled form |
| `MhloToPythonPrinter` ([Part 4](../hlo-opt/mhlo-to-python-printer-heavy.md)) | emits a Python constructor per op in this family |
| `BirCodeGenLoop` ([Part 6](../nki/bir-codegen-loop.md)) | lowers each tiled Inst to a bir instruction |
| `MatMulSparseOp` ([6.8.8](../nki/matmul-sparse.md)) | the sparse lowering of `TensorContractOp` |

## Cross-References

- [Penguin IR Node Model](ir-node-model.md) — the `Instruction`/`Operator` SSA base every op extends
- [Penguin Axis / Loop-Axis Model](axis-loop-model.md) — the `AffineAxis` + `AxisType` the op axis-roles tag
- [Penguin AffineExpr Algebra](affine-expr-algebra.md) — the quasi-affine address algebra each `Access` carries
- [mhlo-to-py-penguin](../hlo-opt/mhlo-to-python-printer-heavy.md) — the C-strand printer that emits these op constructors
- [BirCodeGenLoop](../nki/bir-codegen-loop.md) — Penguin → BIR lowering, one `codegen<Op>` per Inst
