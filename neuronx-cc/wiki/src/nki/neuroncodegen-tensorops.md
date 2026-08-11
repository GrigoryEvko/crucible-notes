# NeuronCodegen Tensor-Op Forward Builders

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, module `neuronxcc/nki/compiler/backends/neuron/KernelBuilder.cpython-310-x86_64-linux-gnu.so` (the cp311/cp312 twins are source-identical; their Op set matches). The `.so` is an UNSTRIPPED 14.5 MB Cython extension with DWARF `debug_info`. Treat every address as version-pinned.*

## Abstract

This page documents the ~30 **tensor-op forward builders** of the NKI `KernelBuilder` module — the methods that take nl/nisa-Python arguments and **construct** a Penguin-IR `<Op>` node, then append it to the current basic block. This is the *forward* (build) direction. It is the exact inverse of the re-emit **printer** documented in [6.5.9](./nkicodegen-printer.md), which walks an existing Penguin node back out to `nisa.<prim>(...)` text. The two surfaces marshal the same enums in opposite directions, and confusing them is the single largest hazard in this part of the compiler.

Each builder follows one fixed shape: parse kwargs, validate, normalize operand tiles through `combine_tiles`, read the tile's access pattern as `(par, free)` index pairs, construct **one** `penguin.ir.<Op>` Python object with named kwargs, and append it through `self.insert` — which reaches `IRBuilder.add_named_instruction` after stamping predicates, dependency edges, bookkeeping, and a source location. The op-selector **enums are passed through verbatim**: the builder stores the Python `np.ufunc` / `ALUOpcode` / `TSOpcode` / `EngineAccumulationType` object on the node as-is. Numeric ISA-enum renumbering happens one layer down at `BirCodeGenLoop`, **not** here.

The one-to-one and many-to-one builder→Op mapping is the payload of this page. Most builders own exactly one Op; a few interesting cases collapse multiple builders onto a single Op (`tensorscalar` + `tensorvectorscalar` → one `TensorScalarPtrOp`), decompose into several nodes (`tensorscalar` of `np.power`), or split on a runtime property (`select` → `TensorSelect` *or* `AffSelTensorScalarOp`). The MX-quantize builder `quantize_mx` → `QuantizeMXOp` lives **here**, in the forward builder, not only in `BirCodeGenLoop`.

> **NOTE — the compiled class is `GeneratedNeuronCodegen`; `NeuronCodegen` is its base.** Every method wrapper mangles as `__pyx_pw_9neuronxcc_9generated_3nki_8compiler_8backends_6neuron_13KernelBuilder_22GeneratedNeuronCodegen_<idx><name>`, whose `22` length-prefix decodes to the 22-character `GeneratedNeuronCodegen` — the Cython-emitted concrete subclass the methods actually compile into. `NeuronCodegen` is the hand-written base, and is the name you will see in source-level material. This page cites symbols as `GeneratedNeuronCodegen` and speaks of the logical surface as `NeuronCodegen`; they are one builder.

| | |
|---|---|
| **Module / class** | `KernelBuilder.cpython-310…so` · class `GeneratedNeuronCodegen` (logical `NeuronCodegen`) |
| **Direction** | nl/nisa-Python args → construct `penguin.ir.<Op>` node → `self.insert` |
| **Insert entry** | `self.insert` (mdef idx 297, `@0x165f00`) → `IRBuilder.add_named_instruction` |
| **Tile normalize** | `combine_tiles` (`@0x16ad90`) + `tile.canonical_par_indices` / `canonical_free_indices(...)` |
| **Per-op modules** | `neuronxcc.starfish.penguin.targets.generated.<Op>` · ISA base `…targets.tonga.TongaISAInst` |
| **Enum policy** | **pass-through**; numeric ISA renumber deferred to `BirCodeGenLoop` codegen<Op> |
| **Builder count** | ~30 tensor-op methods (of 194 total `__pyx_mdef_…GeneratedNeuronCodegen_<N><name>`) |
| **MX builder** | `quantize_mx` (idx 161, `@0x1f9390`) → `QuantizeMXOp` — lives here, not only in `BirCodeGenLoop` |

> **NOTE — evidence base and its limits.** Method addresses, mdef indices, and `KernelBuilder.py` source lines below come from the binary's `nm` symbol table (`__pyx_pw`/`__pyx_mdef` per method) and DWARF `decodedline`; the Op-class names come from the constructed `pyx_n_s_<Op>` body locals and `.rodata`. The disassembly export available here covers a **tail window** of the class — the high-index methods `rand2`/`rng`/`exponential`/`activate2`/`nonzero_with_count`/`*_read_accumulator`, idx 1–17, which pin the class name, the import modules, and the `EngineAccumulationType`/`combine_tiles`/`insert` strings — but does not re-read each tensor-op body individually. Treat a per-method fact as directly pinned when a string for it appears in that window, and as symbol-table-level otherwise.

---

## 1. The builder pattern

Every tensor-op method is a six-step pipeline. The skeleton below is the shared shape; §2–§9 fill in the per-family specifics. Function names are the real symbols; the per-op kwargs are the confirmed body locals.

```c
// GeneratedNeuronCodegen.<tensor_op>(self, ...kwargs...)   // one method per builder
PyObject *build_tensor_op(self, /* op-specific kwargs */) {
    // (a) parse kwargs: __Pyx_ParseOptionalKeywords / __Pyx_GetKwValue_FASTCALL
    //     (pyargnames SIMD-insert block gives the exact kwarg order)
    parse_kwargs(&op, &operands, &dtype, &mask, &deps, &schedule, &name, ...);

    // (b) validate operand dtypes / engine legality (per-family guard literals)
    validate(operands, dtype);                       // e.g. check_tensor_int32_ops_supported

    // (c) normalize / broadcast / align operand tiles
    tiles = self.combine_tiles(operands);            // @0x16ad90  (shared with the matmul and memory families)

    // (d) read the access pattern off each tile as (stride,size) APPairs
    par  = tile.canonical_par_indices;               // partition-axis index
    free = tile.canonical_free_indices(...);         // free-axis indices

    // (e) construct ONE penguin.ir.<Op> node, storing enums VERBATIM
    inst = penguin.ir.<Op>(op=op, in=tiles, /* flags, engine, dtype, name */);

    // (f) append it; insert wraps add_named_instruction + predicates + deps + debugloc
    self.insert(inst, buffer, name, deps, sema, mask, ...);   // idx 297 @0x165f00
    return out_tile;
}
```

### `self.insert` — the append wrapper

`self.insert` (`@0x165f00`, shared verbatim with the matmul and memory families) does, in order:

```c
self.insert(inst, ...) {
    builder.add_named_instruction(inst);   // penguin.ir.IRBuilder.add_named_instruction — the real emit
    add_predicates(inst, mask);            // attach the `mask` predicate list
    process_dep_edges(inst, deps);         // attach the `deps` dependency edges
    process_new_neuroninst(inst);          // NeuronCodegen bookkeeping
    update_debugloc(inst);                 // stamp the source location
}
```

> **GOTCHA — `add_named_instruction` is never called raw at a build site.** The "IRBuilder.add_named_instruction → penguin.ir.<Op>" pairing is the *ctor-then-insert* pair: the method constructs the node, then `self.insert` reaches `add_named_instruction` for it. A reimplementer who hunts for a direct `add_named_instruction(...)` at each builder will not find one — it is always behind `self.insert`.

### Enum marshalling is pass-through

The three op-selector enums are stored as Python objects on the Op node. The builder does **not** assign a numeric ISA value — that is deferred to `BirCodeGenLoop.codegenAluOp` / `codegenAccumCmd`. The `.rodata` type-hint literals pin the parameter types:

| Selector | kwarg(s) | Type hint (`.rodata`) | Carried on | Confidence |
|---|---|---|---|---|
| ALU op | `op` | `Union[np.ufunc, ALUOpcode]` | `TensorTensorOp` / `TensorReduceOp` / `ActivationOp` | HIGH (type-hint) |
| TensorScalar op | `op0` / `op1` | `TSOpcode` | `TensorScalarPtrOp` | HIGH (type-hint) |
| Accumulate cmd | `reduce_cmd` | `Optional[EngineAccumulationType]` (default `Idle`; reset member `ResetReduce`) | activation / select-reduce / tensor-scalar-cache | CERTAIN — `EngineAccumulationType` string @ `0x24870` |
| Activation func | `op` (of `activation`) | `np.ufunc` | `ActivationOp` | HIGH (type-hint) |

> **NOTE — this builder is the source the printer reads.** The [6.5.9 printer](./nkicodegen-printer.md) `opcode()`/`reduce_cmd()` name-mappers (sigmoid→expit, erf→erf, etc.) read back exactly the `np.ufunc`/enum objects this builder stores verbatim. Builder = store; printer = name-map; `BirCodeGenLoop` = numeric ISA renumber (`codegenAluOp` 1→29; `codegenAccumCmd` Idle/Zero/AddAccum/ZeroAccum).

---

## 2. The complete builder → `penguin.ir.<Op>` map

All ~30 tensor-op builders, grouped by family. **mdef idx** is the `__pyx_mdef_…GeneratedNeuronCodegen_<N><name>` index; **pw addr** is the `nm` `__pyx_pw` wrapper address; **py** is the `KernelBuilder.py` source line from DWARF; **Op** is the constructed `pyx_n_s_<Op>` body local. Method addresses and lines come from `nm`/DWARF rather than from a re-read of each body; Op-class names are pinned at the named-local level.

| Family | idx | builder | pw addr | py | → `penguin.ir.<Op>` |
|---|---|---|---|---|---|
| **Activation** | 211 | `activation` | `0x1968c0` | 2882 | `ActivationOp` |
| | 213 | `activation_accu` | `0x9f810` | 2960 | `ActivationAccumulationOp` |
| **Tensor-Tensor** | 181 | `tensortensor` | `0x15b850` | 2262 | `TensorTensorOp` (`cls=`, default) |
| | 179 | `binop` | `0x177c80` | 2221 | *delegates → `tensortensor`* |
| | 215 | `tensortensorscan` | `0x262c60` | 3010 | `TensorTensorScanOp` (2-op scan) |
| **Tensor-Scalar** | 217 | `tensorscalar` | `0x140530` | 3066 | `TensorScalarPtrOp` (op0/scalar0/reverse0) |
| | 203 | `tensorvectorscalar` | `0x293fd0` | 2733 | `TensorScalarPtrOp` (op0/op1 + operand0/1 + reverse0/1) |
| | 219 | `tensorscalarcumulative` | `0x1f59b0` | 3131 | `TensorScalarCacheCumulative` |
| | 221 | `tensorscalarreduce` | `0x1ca7f0` | 3184 | `TensorScalarCacheReduce` |
| **Reduce** | 173 | `tensorreduce` | `0x239de0` | 2098 | `TensorReduceOp` (op/reduce_dims/negate/keepdims) |
| | 175 | `tensor_partition_reduce` | `0x24b420` | 2162 | `PartitionReduceOp` (reduce_all_axis) |
| **Unary** | 177 | `unary_op` | `0x1e4dd0` | 2204 | *get_unary_func → `activation`; `ActivationOp`* |
| | 223 | `simple_unary` | `0x260c50` | 3241 | *cls-parameterized; raw InstTile + insert* |
| | 225 | `reciprocal` | `0xe9270` | 3269 | `ReciprocalOp` (via `simple_unary` cls=) |
| **Select / pred** | 195 | `select` | `0x181b60` | 2507 | `TensorSelect` *or* `AffSelTensorScalarOp` |
| | 183 | `affine_select` | `0x18cf70` | 2312 | `AffSelTensorScalarOp` |
| | 199 | `select_reduce` | `0x229540` | 2619 | `SelectReduce` (ex-`CopyPredicatedReduce`) |
| | 193 | `range_select` | `0x1544c0` | 2414 | `RangeSelect` / `RangeSelectReduce` |
| | 197 | `tensor_copy_predicated` | `0x193c50` | 2574 | `TensorCopyPredicated` |
| **DVE top-8 / stats** | 259 | `max8` | `0x15e930` | 3799 | `SundaMax8` |
| | 261 | `nc_find_index8` | `0x122ce0` | 3838 | `SundaMaxIndex8` |
| | 263 | `nc_match_replace8` | `0x104540` | 3899 | `SundaMatchReplace8` |
| | 265 | `nc_match_replace_indices8` | `0xffe90` | 3973 | `MaxIndexAndMatchReplace` |
| | 245 | `bn_stats_inst` | `0xae430` | 3558 | `SundaBNStats` (Welford partial) |
| | 247 | `bn_aggr_inst` | `0xd5aa0` | 3586 | `SundaBNAggr` (Welford aggregate) |
| | 233 | `index_value_inst` | `0x13bfc0` | 3352 | `IndexValueInst` (+ `IndexValueTile`) |
| **Misc / movement** | 235 | `dropout` | `0x1c1000` | 3375 | `DropoutMaskInst` |
| | 161 | `quantize_mx` | `0x1f9390` | 1837 | `QuantizeMXOp` ⟵ correction (§7) |
| | 243 | `memset` | `0x19aac0` | 3514 | `MemsetOp` |
| | 227 | `copy` | `0x226860` | 3296 | `TensorCopyOp` |
| | 229 | `tensor_copy` | `0x227b10` | 3311 | `TensorCopyOp` (explicit engine) |
| | 267 | `tensor_copy_dynamic_src` | `0x1187d0` | — | `TensorCopyDynamicSrc` |
| | 269 | `tensor_copy_dynamic_dst` | `0xef890` | — | `TensorCopyDynamicDst` |

> **NOTE — ISA-inst module path.** The Op nodes for the DVE/stats family derive from a target-ISA inst base. This binary's string table shows the import `neuronxcc.starfish.penguin.targets.tonga.TongaISAInst` and `…tonga.TongaEnums`, and per-op codegen classes under `neuronxcc.starfish.penguin.targets.generated.<Op>` (e.g. `…generated.Exponential`, `…generated.Activate2`). The `Sunda…` node names in the table are the constructed-class locals; the import path backing them here is the Tonga/generated target, per the `tonga.TongaISAInst` and `targets.generated.*` strings.

> **QUIRK — three builders own no Op of their own (pure delegates).** `binop` routes a binary op through `tensortensor`; `unary_op` resolves a unary func and calls `activation`; `reciprocal` calls `simple_unary` with `cls=ReciprocalOp`. They appear in the roster but never `self.insert` directly — they exist for call-site ergonomics.

---

## 3. Activation family — `ActivationOp` / `ActivationAccumulationOp`

```c
// activation(self, op, tensor, bias, scale, reduce_op, reduce_res,
//            reduce_cmd, mask, dtype, schedule, buffer, name, deps)  @0x1968c0 py2882
PyObject *activation(...) {
    // op = np.ufunc activation function (gelu/silu/exp/tanh/sigmoid/…); stored VERBATIM.
    bias_ap = self.create_activation_bias_ap(bias);   // helper idx 207/209: per-partition bias AP
    require(scale_is_scalar_or_vector,                 // guard: err_activation_scale_scalar_or_v…
            "scale must be scalar or per-partition vector");
    // FUSED activation→reduce:  reduce_op (ALU) + reduce_cmd (EngineAccumulationType, default Idle)
    if (reduce_cmd != EngineAccumulationType.Idle)
        return self.activation_accu(op, ..., reduce_cmd, reduce_res);   // delegate to accumulate form
    inst = ActivationOp(op=op, in=tensor, bias=bias_ap, scale=scale,
                        reduce_op=reduce_op, reduce_cmd=reduce_cmd,
                        reduce_res=reduce_res, dtype=dtype, engine=engine, name=name);
    self.insert(inst, ...);
}
```

`activation_accu` (`@0x9f810`, py2960) builds `ActivationAccumulationOp` — the running-sum form, accumulating into `reduce_res` under `reduce_cmd ∈ EngineAccumulationType`. It shares `create_activation_bias_ap` and the scale guard. The pair `ActivationOp` / `ActivationAccumulationOp` is the **non-accumulate vs accumulate** split of the same activation→reduce; `reduce_cmd` selects Idle/Zero/AddAccumulate/ZeroAccumulate downstream. The Op names and the `EngineAccumulationType` string are pinned in this binary; the accumulate semantics are read from the pairing.

---

## 4. Tensor-Tensor family — `TensorTensorOp` / `TensorTensorScanOp`

```c
// tensortensor(self, op, lhs, rhs, mask, dtype, schedule, deps, name, cls)  @0x15b850 py2262
PyObject *tensortensor(...) {
    cls = cls or TensorTensorOp;                 // cls kwarg lets callers swap the node class
    require(check_tensor_int32_ops_supported(lhs, rhs, op));  // int32 elementwise legality
    promote_type(lhs, rhs);
    tiles = self.combine_tiles(lhs, rhs);        // broadcast/align
    engine = pick_engine();                       // NeuronEngine ∈ {Vector, GpSIMD, Unknown}
    inst = cls(op=op, lhs=tiles.l, rhs=tiles.r, engine=engine, dtype=dtype, name=name);
    self.insert(inst, ...);                       // the binary elementwise op (add/mult/sub/max/min/…)
}
```

`binop` (`@0x177c80`) is the thin wrapper that reads `partition_size` and forwards a binary op into `tensortensor`. `tensortensorscan` (`@0x262c60`, py3010) builds the **two-op cumulative scan** `TensorTensorScanOp`:

```c
// tensortensorscan(self, data0, data1, initial, op0, reverse0, reverse1, mask, dtype, schedule, deps)
//   y[i] = op1( op0(data0[i], scan_state), data1[i] )   with `initial` seed, per-op reverse flags
inst = TensorTensorScanOp(data0=data0, data1=data1, initial=initial,
                          op0=op0, op1=op1, reverse0=reverse0, reverse1=reverse1, dtype=dtype);
```

`op0`/`op1` are the two chained ALU ops; `reverse0`/`reverse1` flip operand order per chained op.

---

## 5. Tensor-Scalar family — all → `TensorScalarPtrOp` (+ cache variants)

The defining fact: **`tensorscalar` and `tensorvectorscalar` build the *same* `TensorScalarPtrOp`** — one Penguin op for "tensor ∘ up-to-two scalar/vector ops", differentiated only by how many `(op, operand)` pairs are populated.

```c
// tensorscalar(self, tensor, op0, scalar0, reverse0, mask, dtype, schedule, deps, name)  @0x140530 py3066
PyObject *tensorscalar(...) {
    // SINGLE alu op:  out = op0(tensor, scalar0)   (reverse0 swaps operands)
    sv = ScalarValue(scalar0);                    // wrap the immediate
    // op0 type = TSOpcode (stored verbatim)
    if (op0 == np.power) return self.power_tensorscalar(...);  // SPECIAL-CASE decomposition
    if (op0 in {mod, fmod, remainder})                          //  a - floor(a/b)*b
        return self.mod_from_remainder(tensor, sv);
    inst = TensorScalarPtrOp(in=tensor, op0=op0, scalar0=sv, reverse0=reverse0, dtype=dtype, name=name);
    self.insert(inst, ...);
}

// tensorvectorscalar(self, tensor, op0, operand0, reverse0, op1, operand1, reverse1,
//                    is_scalar_tensor_tensor, mask, dtype, engine, schedule, deps, name, cls)  @0x293fd0 py2733
PyObject *tensorvectorscalar(...) {
    // TWO chained alu ops:  out = op1( op0(tensor, operand0), operand1 )
    require(op0 != np.power && op1 != np.power,
            "tensorvectorscalar doesn't support power, use tensor_tensor instead.");
    self.update_op_and_ptr(inst, op0, operand0, reverse0);   // marshal (op, operand-ptr) pair #0
    self.update_op_and_ptr(inst, op1, operand1, reverse1);   // marshal pair #1
    // is_scalar_tensor_tensor flag selects the scalar-tensor-tensor variant
    inst = (cls or TensorScalarPtrOp)(in=tensor, /* the two op/operand pairs */, engine=engine, dtype=dtype);
    self.insert(inst, ...);
}
```

> **QUIRK — `np.power` and `mod` are decomposed, not emitted as one op.** `tensorscalar(op0=np.power, ...)` routes to `power_tensorscalar` and carries the guard *"Schedule is not supported for np.power, use nisa.tensortensor or nisa.activation explicitly with schedule tuple!"*; `mod`/`fmod`/`remainder` route to `mod_from_remainder` (`a − floor(a/b)·b`). Both lower to one-or-more `TensorScalarPtrOp`/`TensorTensorOp` nodes — there is no single hardware power/mod scalar op.

The two **cache** variants are the accumulate/reduce-fused forms of the same op:

- `tensorscalarcumulative` (`@0x1f59b0`, py3131) → `TensorScalarCacheCumulative` — running (cumulative) tensor-scalar under `EngineAccumulationType reduce_cmd`; inner helper `process_operand` (mdef idx 22). Keeps a running PSUM accumulate.
- `tensorscalarreduce` (`@0x1ca7f0`, py3184) → `TensorScalarCacheReduce` — tensor-scalar fused with a reduce into `reduce_res` (partition_size-tiled).

---

## 6. Reduce family — `TensorReduceOp` / `PartitionReduceOp`

```c
// tensorreduce(self, op, src, mask, dtype, negate, keepdims, schedule, …)  @0x239de0 py2098
inst = TensorReduceOp(op=op, reduce_dims=reduce_dims, npartitions=npartitions,
                      negate=negate, keepdims=keepdims);
//   op           = ALU reduce op (Union[np.ufunc, ALUOpcode])
//   negate       = negate-result flag (the HW free-axis reduce-then-negate, e.g. -max in softmax)
//   reduce_dims  = from inner local NeuronCodegen_tensorreduce_local (reducing_right_most_dims genexpr)
//   keepdims     = preserve reduced axes
```

`tensor_partition_reduce` (`@0x24b420`, py2162) → `PartitionReduceOp` is the **cross-partition** reduce (over the 128-partition axis, not free axes), carrying a `reduce_all_axis` flag + `partition_size`. The free-axis reduce (`TensorReduceOp`) and the partition-axis reduce (`PartitionReduceOp`) are **distinct Penguin nodes** — a reimplementer must not collapse them.

---

## 7. `quantize_mx` → `QuantizeMXOp` — the MX builder

The forward builder for MX quantization is `quantize_mx` (mdef idx 161, `@0x1f9390`, py1837), and it lives in this module: its body constructs a first-class `QuantizeMXOp` Penguin node and hands it to `self.insert`.

> **GOTCHA — `quantize_mx` is easy to mislocate.** `BirCodeGenLoop` carries a `quantize_mx` macro re-trace, and the `NkiCodegen` printer has no `quantize_mx` at all. Neither of those is the origin: the node is minted here, as a first-class inline op, **not** as a kernel-registry macro.

```c
// quantize_mx(self, src, dst, dst_scale, mask, deps, name, scale_mask)  @0x1f9390 py1837
PyObject *quantize_mx(...) {
    assert_dtype_in(src, {bfloat16, float16});        // input must be bf16/fp16
    // TWO outputs:
    //   dst       = x4-packed quantized data: float8_e4m3fn_x4 / float8_e5m2_x4
    //   dst_scale = E8M0 per-block scale, dtype uint8
    self.check_mx_scale(...);                         // [P/8, F/4] / block=32 OCP-MXFP E8M0 validator
                                                      // (same validator as matmult_mx)
    // build the block-scale access pattern:
    scale_ap = NeuronIndicesAP(generate_subst_map, substituteApIndices,
                               generate_ap_index, set_shape);   // over partition_dim, n_elts/div_ceil block sizing
    apply_mask(scale_ap, scale_mask);                 // scale_mask masks the scale lanes
    mask = TileMaskIntersection(data_mask, scale_mask);
    inst = QuantizeMXOp(src=src, dst=dst, dst_scale=scale_ap, name=name);
    self.insert(inst, ...);
}
```

`quantize_mx` is the **online activation-quantize-to-MXFP** builder: it lowers an fp16/bf16 tile to a `{x4-packed-fp8 data, E8M0 scale}` pair that feeds `nc_matmul_mx` (`MatMulMXOp`, also built here). The MX path therefore *originates* in this builder and is read downstream by `BirCodeGenLoop.codegenQuantizeMX`. The `check_mx_scale` geometry validator (`[P/8, F/4]`, block=32) is shared verbatim with the matmul-MX builder.

---

## 8. Select / predicate / range-select family

```c
// select(self, pred, on_true, on_false, mask, dtype, schedule, deps)  @0x181b60 py2507
PyObject *select(...) {
    if (is_affine_iteration_space_mask(pred)) {       // affine path
        lower_predicates(pred);
        return self.affine_select(pred, on_true, ...);   // → AffSelTensorScalarOp
    }
    inst = TensorSelect(pred=pred, on_true=on_true, on_false=on_false, dtype=dtype);  // data-predicate path
    self.insert(inst, ...);
}
```

> **QUIRK — `select` is a hybrid that builds two different Op classes.** A *data* predicate (a runtime tensor mask) builds `TensorSelect` directly. An *affine* iteration-space mask delegates to `affine_select`, which builds `AffSelTensorScalarOp` — a TensorScalar-class node, not a select node. Both ctor sites and the delegate call are present in the `select` body. A reimplementer who assumes `select` always yields a single node type will miss the affine fast-path.

The rest of the family:

- `affine_select` (`@0x18cf70`, py2312) → `AffSelTensorScalarOp` — affine-predicate select expressed as an iteration-space mask, lowered through the `predicates` list.
- `select_reduce` (`@0x229540`, py2619) → `SelectReduce` — predicated select **fused with a reduce** (`reduce_op` ALU + `reduce_cmd` EngineAccumulationType + `reduce_res`; `reverse_pred` inverts the predicate). A cp311 docstring leak names it: *"Implementation of the SelectReduce (formerly CopyPredicatedReduce) instruction."*
- `range_select` (`@0x1544c0`, py2414) → `RangeSelect` / `RangeSelectReduce`:

```c
// range_select(self, on_true_tile, on_false_value, range_start, comp_op0, comp_op1,
//              bound0, bound1, reduce_op, mask, dtype, schedule, deps, name)
//   keep on_true where range_start satisfies comp_op0(·,bound0) lo / comp_op1(·,bound1) hi, else on_false_value
ds = DynamicScalar(range_start);                       // runtime range_start
inst = reduce_op ? RangeSelectReduce(... reduce_cmd=EngineAccumulationType.ResetReduce ...)
                 : RangeSelect(on_true=on_true_tile, on_false=on_false_value,
                               range_start=ds, comp_op0=comp_op0, comp_op1=comp_op1,
                               bound0=bound0, bound1=bound1);
```

- `tensor_copy_predicated` (`@0x193c50`, py2574) → `TensorCopyPredicated` — conditional copy under a predicate; the non-reducing sibling of `select_reduce`.

---

## 9. DVE top-8 / stats family — Tonga ISA insts

These build target-ISA inst nodes (from `…penguin.targets.tonga.TongaISAInst`) — the Data/Vector-Engine top-8 selection and the Welford batch-norm primitives. All read `assert_elements_per_partition` / `assert_max_dimensions` and build `NDTile` + `InstTile`.

| builder | Op | semantics |
|---|---|---|
| `max8` (`@0x15e930`) | `SundaMax8` | DVE top-8 (8 largest) per partition; locals `max_topk_elements`/`min_topk_elements` (top-8 vs bottom-8) + `filter_indices` |
| `nc_find_index8` (`@0x122ce0`) | `SundaMaxIndex8` | index (uint16/uint32) of the 8 max values; builds a `NeuronIndicesAP` (substituteApIndices/generate_subst_map). Docstring: *"its usage is not limited to only finding max values, it can find the index of any value."* Tile names `nc_find_index8_data`/`_vals` |
| `nc_match_replace8` (`@0x104540`) | `SundaMatchReplace8` | find-and-replace the 8 matched values with `imm`; optionally writes `dst_idx`; can call `nc_match_replace_indices8`. Tiles `nc_match_replace8_data`/`_vals` |
| `nc_match_replace_indices8` (`@0xffe90`) | `MaxIndexAndMatchReplace` | combined max-index + match-replace, fused |
| `bn_stats_inst` (`@0xae430`) | `SundaBNStats` | BatchNorm/Welford **partial** stats (count/mean/M2 per partition tile) |
| `bn_aggr_inst` (`@0xd5aa0`) | `SundaBNAggr` | Welford **aggregate** (combine partial stats across tiles → global mean/var) |
| `index_value_inst` (`@0x13bfc0`) | `IndexValueInst` (+ `IndexValueTile`) | index↔value paired tile op (`promote_to_tile_index`); pairs a value with its argmax index — the (index,value) bundle the top-k family threads through |

*Op-class locals are pinned; the DVE instruction struct field layouts were not read, so this table gives Op names only.*

---

## 10. Misc elementwise / data-movement

```c
// dropout(self, tensor, prob, …)  @0x1c1000 py3375  → DropoutMaskInst
mask_tile = DataTile(...);
self.memset(mask_tile, seed);                     // seed the mask via memset
inst = DropoutMaskInst(in=tensor, prob=prob, mask=mask_tile, dtype=dtype);  // prob = keep/drop probability
```

```c
// memset(self, tile_shape, value, dtype, mask, dst_tile, schedule, engine, deps, name)  @0x19aac0 py3514  → MemsetOp
sv = ScalarValue(value);
require(engine in {Vector, GpSIMD, Unknown},
        "Memset engine can only be Vector or GpSIMD or Unknown.");   // Vector/GpSIMD fill, NOT DMA
inst = MemsetOp(shape=tile_shape, value=sv, engine=engine, dtype=dtype);
```

> **GOTCHA — `memset` is a compute-engine fill, not a DMA.** The guard restricts the engine to `Vector`/`GpSIMD`/`Unknown` — there is no DMA path here. DMA-class moves live in the memory family ([the forward builder page](./neuroncodegen-forward-builder.md)).

The copy variants:

- `copy` (`@0x226860`, py3296) → `TensorCopyOp` — plain tile copy.
- `tensor_copy` (`@0x227b10`, py3311) → `TensorCopyOp` — near-duplicate of `copy` adding explicit `NeuronEngine` selection.
- `tensor_copy_dynamic_src` (`@0x1187d0`) → `TensorCopyDynamicSrc`; `tensor_copy_dynamic_dst` (`@0xef890`) → `TensorCopyDynamicDst` — the runtime register-addressed copies (src/dst address from a register; the DynamicScalar/offset family at copy granularity).

---

## 11. What pins the five strongest claims

1. **The class is `GeneratedNeuronCodegen`.** The wrapper symbols are `__pyx_pw_…KernelBuilder_22GeneratedNeuronCodegen_<idx><name>`, and the `22` length prefix decodes to the 22-character name. `NeuronCodegen` is the logical base; the compiled subclass is what the methods live in.
2. **`quantize_mx` → `QuantizeMXOp` is a forward builder in this module.** idx 161 at `0x1f9390`, with a `QuantizeMXOp` ctor followed by `self.insert`. The MatMulMX consumer and the `check_mx_scale` validator corroborate it. The body is read; the address itself comes from the symbol table.
3. **`tensorscalar` and `tensorvectorscalar` both build the single `TensorScalarPtrOp`.** Both bodies construct it; the difference is one versus two `(op, operand)` pairs — `update_op_and_ptr` called once or twice.
4. **Enum marshalling is pass-through; renumbering is downstream.** The type-hint strings `Union[np.ufunc, ALUOpcode]`, `TSOpcode`, and `Optional[EngineAccumulationType]` pin the kwarg types, and `EngineAccumulationType` is present at `0x24870`. Numeric ISA mapping happens in `BirCodeGenLoop.codegenAluOp` / `codegenAccumCmd`.
5. **`self.insert` (`@0x165f00`) reaches `IRBuilder.add_named_instruction`.** Both the `add_named_instruction` symbol and the `insert` string are present; the wrap order (add_named_instruction → predicates → deps → bookkeeping → debugloc) is read from `.rodata` and the sibling forward-builder page.

**What is not pinned.** The per-method addresses, mdef indices, and py-lines in §2 come from `nm`/DWARF rather than from a re-read of each body; the disassembly window available here covers only idx 1–17 plus the class name, import modules, and the `EngineAccumulationType`/`combine_tiles`/`insert` strings. The `Sunda…` Op-class names are constructed-local names, and the import path shown is `targets.tonga.TongaISAInst` / `targets.generated.<Op>`. The exact *positional* kwarg order at each `<Op>(...)` ctor is mstate-routed and was not byte-traced — kwarg names are pinned, order is [INFERRED]. DVE instruction struct field layouts were not read at all.

---

## See also

- [6.5.1 NeuronCodegen Forward Builder — overview & matmul](./neuroncodegen-forward-builder.md) — the same class, the matmul/memory halves, and `self.insert` in detail.
- [6.5.9 NeuronCodegen Re-emit Printer](./nkicodegen-printer.md) — the **inverse** surface (Penguin node → `nisa.<prim>` text); reads back the enums this builder stores.
- [5.6 Penguin Tensor-Op Family](../penguin/tensor-op-family.md) — the Penguin-IR `<Op>` nodes these builders construct.
- **BIR codegen loop** — `BirCodeGenLoop.codegenAluOp`/`codegenAccumCmd` numeric ISA renumbering, one layer below this builder.
