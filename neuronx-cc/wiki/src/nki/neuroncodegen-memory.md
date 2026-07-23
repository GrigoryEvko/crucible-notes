# NeuronCodegen Memory / DMA Forward Builders

> *All symbols, offsets, and strings on this page apply to neuronx_cc 2.24.5133.0+58f8de22, module `neuronxcc/nki/compiler/backends/neuron/KernelBuilder.cpython-310-x86_64-linux-gnu.so` (the 14.5 MB cp310 wheel; cp311/cp312 differ only in offsets). Addresses are the Cython wrapper symbol addresses (`__pyx_pw_…NeuronCodegen_<n><name>`) within that `.so`. Every Penguin Inst class name, kwarg name and error literal cited below was read directly out of the binary's symbol/string table (`nm`, `strings`).*

## Abstract

[`NeuronCodegen`](neuroncodegen-forward-builder.md) is the NKI trace-time *forward builder*:
it runs while a NKI kernel's Python body executes, and every memory/DMA call on the codegen object
constructs one (or a few) **Penguin Inst** objects and appends them to the current Penguin Function
block. It does **not** emit BIR. The Penguin → BIR step is the *next* stage down
([Penguin DGE-level dynamic DMA](../penguin/dge-level-dynamic-dma.md) and the `BirCodeGenLoop` `codegen<Op>`
roster); the klr → BIR C++ leaves below *that* are documented in [2.21 DMA encoding](../isa/dma-encoding.md).
This page is the topmost layer: the Python tracer that turns `nki.load`, `nki.store`,
`dma_copy`, `dma_transpose`, the indirect gather/scatter family, the on-engine gathers, the
const/identity materializers, and `memset`/`stream_shuffle` into Penguin Inst objects.

There are two things to learn here, and they are orthogonal. First, the **shared plumbing**: a
single append choke point (`self.insert`), a single operand-AP normalizer (`combine_tiles`), the
`InstTile`/`NDTile` whose `canonical_par_indices`/`canonical_free_indices` *are* the
[`AccessPattern`](memref-view-model.md) `(step,num)` pair the view layer already built, and a DMA
QoS gate (`validate_dma_qos`). Second, the **per-method Inst construction**: which Penguin Inst class
each method builds, what dispatch picks between variants, and which kwargs (`dge_mode`, `oob_mode`,
`rmw_op`, `dma_qos`) ride on the Inst straight down to the BIR fields.

The map, each row pinned by the constructed class-name string in the binary:

| Method | Penguin Inst | Note |
|---|---|---|
| `load` | `SBAtomLoad` | HBM→SBUF DMA-load; `other`→`tile.init_value` OOB fill |
| `store` | `SBAtomStore` | SBUF→HBM DMA-store; carries `rmw_op` |
| `dma_copy` | `DMACopyOp` | generic DMA; 3-way indirect dispatch |
| `dma_transpose` | `DMATransposeLoad` / `DMATransposeCopy` / `DMAIndirectTranspose` | XZYW perm `(3,1,2,0)` |
| `indirect_load` | `NeuronIndirectLoad` | index/offset-driven gather DMA |
| `indirect_save` | `NeuronIndirectSave` \| `NeuronIndirectRMW` | `rmw_op=np.add` → RMW scatter |
| `local_gather` | `IndirectCopy` | GpSIMD on-engine gather, `uint16` index |
| `gather_flattened` | `PoolGather` | Pool-engine flattened gather, `uint32` index |
| `load_tensor_to_register` | `LoadTensorToRegister` | + `DynamicScalar` dst register |
| `broadcast_partition` | *(delegates to `simple_unary`)* | partition-broadcast AP, not its own Inst |
| `stream_shuffle` | `StreamShuffleInst` | + `DynamicScalar` `shuffle_mask` |
| `shared_constant` | `NeuronWeightTensor` \| `Tensor` | DRAM const, or graph Tensor when `enable_const_rewrite` |
| `shared_identity_matrix` | `IdentityWeightTensor` | `np.identity(n)` DRAM const |
| `get_identity_tensor` | `SBAtomLoad` | front-of-kernel load of cached identity into SBUF |
| `create_tensor` | *(fresh `Tensor`, no Inst)* | tensor/temporary factory |
| `memset` | `MemsetOp` | `value`=`ScalarValue`; engine ∈ {Vector, GpSIMD, Unknown} |

### Method address table

All entries are `__pyx_pw_…NeuronCodegen_<name>` wrapper addresses in the cp310 `.so`:

```
load                    0x24e330    store                   0x250f90
dma_copy                0x25a430    dma_transpose           0x20bc50
indirect_load           0x25d880    indirect_save           0x256b80
local_gather            0x0dfea0    gather_flattened        0x2762e0
load_tensor_to_register 0x111740    broadcast_partition     0x0ad480 (inner 0x1beaf0)
stream_shuffle          0x187ea0    memset                  0x19aac0
shared_constant         0x0aa940    shared_identity_matrix  0x07b490
get_identity_tensor     0x0a3470    create_tensor           0x08b0e0
helpers: insert 0x165f00 · combine_tiles 0x16ad90 · legalize_store_value 0x1585c0
```

> **NOTE — two KernelBuilder `.so` files.** The cp310 wheel ships *two* `KernelBuilder.cpython-310-x86_64-linux-gnu.so`: a 820 KB one under `neuronxcc/generated/…` and the 14.5 MB one under `neuronxcc/nki/…`. The addresses on this page are from the **14.5 MB `nki/` binary** (`.text` runs to ~`0x299530`, which is the only one large enough to hold `load` at `0x24e330`). Its method-def symbols are numbered exactly as cited — `…NeuronCodegen_251load`, `_255store`, `_159dma_copy`, `_113dma_transpose`, `_243memset` — all present in `nm`. The `generated/` `.so` is the stub façade.

---

## 1. The shared plumbing

Every memory method bottoms out in the same four helpers. Learn these once and the per-method bodies
are short.

### 1.1 `self.insert(inst, …)` — the single append point — `@0x165f00`

```c
// __pyx_pw_…NeuronCodegen_insert  @0x165f00
// the Penguin-Function equivalent of BIR InstBuilder::insertElement / addInstToBir
PyObject *NeuronCodegen_insert(self, inst, buffer, name, deps, ...) {
    add_named_instruction(builder, inst, name);   // name the Inst into the Function
    add_predicates(inst, mask);                    // attach the `mask` as predicates
    process_dep_edges(inst, deps);                 // wire dependency edges from `deps`
    process_new_neuroninst(self, inst);            // NeuronCodegen-side bookkeeping
    update_debugloc(inst, cur_scope, ...);         // stamp the debug location
    // → inst is now live in cur_function_scope / cur_scope
}
```

The symbols `add_named_instruction`, `add_predicates`, `process_dep_edges`,
`process_new_neuroninst`, `update_debugloc`, `cur_function_scope`, `cur_scope`, `builder`, and
`NeuronInst` are all present in the binary's name table (`__pyx_n_s_*`). **This is the only place an
Inst is appended.** Every method below ends `self.insert(<Inst>, …)`; the `mask` argument becomes the
Inst's predicate, the `deps` argument drives `process_dep_edges`, and `process_new_neuroninst` is the
hook where the codegen object updates its own per-Function state (live tiles, register binding, etc.).

### 1.2 `self.combine_tiles(…)` — the operand-AP normalizer — `@0x16ad90`

```c
// __pyx_pw_…NeuronCodegen_combine_tiles  @0x16ad90
PyObject *NeuronCodegen_combine_tiles(self, *operand_tiles, mask, sema, ir, ...) {
    if (is_number(op))                               // NumPy-style scalar broadcast →
        op = broadcast_num_to_memset(op, ...);       //   turn a bare scalar into a memset tile
    broadcast_operands(operand_tiles);               // broadcast shapes to a common AP
    for (t : operand_tiles) align_tile(t, ...);      // align tile shapes
    collect extra_tiles;
    if (op has indirect indices)                     // REJECT indirect indices here
        raise(err_indirect_indices_are_not_sup);     // "…indirect_indices_are_not_sup[ported]"
    return common-AP operand list;
}
```

The symbols `broadcast_operands`, `align_tile`, `extra_tiles`, `broadcast_num_to_memset`,
`is_number`, `err_indirect_indices_are_not_sup`, `mask`, `sema`, and `ir` are all present.
`combine_tiles` appears in
the binary's string table **61 times** — it is the universal operand-AP normalizer called by
`load`/`store`/`dma_copy`/`dma_transpose`/`indirect_load`/`indirect_save`/`gather_flattened`/
`memset`/`load_tensor_to_register`/`stream_shuffle`. The key thing it does *not* do: it explicitly
**rejects indirect indices**, so the indirect path is fully separated from the direct path before
any Inst is built.

### 1.3 `InstTile`/`NDTile` and the canonical-indices AP

The memory methods build an `InstTile` (an SBUF/PSUM value tile, via
`get_tile_with_local_tensor(…)` with an `in_cur_scope` flag) or an `NDTile` (an N-D shape view), then
read two attributes off it:

- `tile.canonical_par_indices` — the **partition** axis index (the highest, outermost stride);
- `tile.canonical_free_indices(…)` — the **free** axes index list (the inner DMA-descriptor dims).

This `(par, free)` index pair *is* the `(step,size)` `AccessPattern` vector the view layer already
emitted: see [6.2.2 memref/view model](memref-view-model.md) for how `TensorView.get_view()` builds
it, and the `bir::AccessPattern {step@+0, num@+8}` struct in [2.21](../isa/dma-encoding.md). So
`NeuronCodegen` does **not** recompute the access pattern — it *reads back* the AP the
allocator/view built and hands it to the Penguin Inst constructor. The partition (par) index is the
partition step; the free indices are the inner descriptor dims. Both symbols resolve in `nm`; the
mapping onto the `(step,num)` pair comes from the cross-strand AP model rather than from this `.so`.

### 1.4 `validate_dma_qos(…)` — the DMA legality gate

A module function called by `load`/`store`/`dma_copy`/`dma_transpose`/`indirect_load` *before* the
Inst is built — the QoS-class legality check. It appears **24 times** in the string table
(`validate_dma_qos`, `dma_qos`). The QoS value rides on the Inst as the `dma_qos` kwarg but is left
at its BIR default downstream — QoS-class assignment is deferred to a later pass.

---

## 2. `load` / `store` — `SBAtomLoad` / `SBAtomStore`

These are the plain static-access-pattern DMA load/store. They are byte-symmetric: a load fills an
SBUF tile from HBM, a store drains an SBUF tile to HBM, and both reroute to the indirect path the
moment the pointer is an `IndirectMemrefTileND`.

### 2.1 `load` — `@0x24e330` — `SBAtomLoad`

```c
// load(self, pointer, mask, dtype, other, schedule, deps, name, mode, dma_qos)  @0x24e330
PyObject *NeuronCodegen_load(self, pointer, mask, dtype, other,
                             schedule, deps, name, mode, dma_qos) {
    if (isinstance(pointer, IndirectMemrefTileND))           // (a) gather fork
        return self.indirect_load(pointer, mask, ..., mode, dma_qos);

    validate_dma_qos(dma_qos);                               // (b) QoS gate
    combine_tiles(pointer, mask=mask, ...);                  //     normalize operand AP
    nki_assert(tile.has_legal_dma_access);                   //     DMA-legality assert
    dst = InstTile(in_cur_scope=..., get_tile_with_local_tensor(...));
    dst.tensor.init_value = other;                           // ⭐ OOB / default fill (SetAttrStr init_value)
    par  = dst.canonical_par_indices;                        // §1.3 partition step
    free = dst.canonical_free_indices(...);                  // §1.3 inner dims
    inst = SBAtomLoad(dst, par, free, dtype, mode, ...);     // Penguin Inst
    self.insert(inst, sema=..., deps=deps, mask=mask, name=name, mode=mode);
}
```

⭐ The interesting line is `dst.tensor.init_value = other` — `init_value` is in the name table,
written via `PyObject_SetAttrString`. `other` is the **out-of-bounds / default fill** value:
where a predicated or strided load does not reach a tile element, the element takes `init_value`.
This is the trace-time source of the OOB-fill the BIR gather path concretizes.

`SBAtomLoad` is the static-AP HBM→SBUF DMA load, lowered by `codegenSBAtomLoad`
in the BirCodeGenLoop and eventually the BIR plain `InstLoad(19)` family (which
[2.21](../isa/dma-encoding.md) distinguishes from `TensorLoad(75)`). `mode` selects the DMA
transfer/dge mode.

### 2.2 `store` — `@0x250f90` — `SBAtomStore`

```c
// store(self, pointer, value, mask, rmw_op, schedule, deps, name, mode, dma_qos)  @0x250f90
PyObject *NeuronCodegen_store(self, pointer, value, mask, rmw_op,
                              schedule, deps, name, mode, dma_qos) {
    value = self.legalize_store_value(value);                // §2.3 coerce to a tile
    if (isinstance(pointer, IndirectMemrefTileND))           // scatter fork
        return self.indirect_save(pointer, value, mask, rmw_op, ...);

    combine_tiles(pointer, value, mask=mask, ...);
    src  = InstTile(...);  par = src.canonical_par_indices; free = src.canonical_free_indices(...);
    inst = SBAtomStore(pointer, src, par, free, rmw_op, ...); // rmw_op rides the store
    self.insert(inst, sema=..., deps=deps, mask=mask, name=name);
}
```

`SBAtomStore` is the static-AP SBUF→HBM DMA store, byte-symmetric to `load`. The
`rmw_op` kwarg makes it a read-modify-write store. The `IndirectMemrefTileND` fork is the same
mechanism as `dma_copy`'s (§3): an indirect destination is rerouted to the scatter.

### 2.3 `legalize_store_value` — `@0x1585c0`

```c
// legalize_store_value(self, value, ...)  @0x1585c0
PyObject *NeuronCodegen_legalize_store_value(self, value, ...) {
    if (is_number(value))                                    // scalar →
        return memset_or_broadcast_to(value, shape, dtype);  //   memset / broadcast_to tile
    value = expand_dims(value);                              // bare array → add dims
    value = copy_into(DataTile(value));                      //   into a DataTile, or…
    if (PSUM-resident) value = NeuronPSUMTensor(value);      //   a NeuronPSUMTensor
    return value;                                            // always a proper Penguin tile
}
```

The symbols `is_number`, `expand_dims`, `copy`, `DataTile`, `NeuronPSUMTensor`, `shape`, and
`dtype` are all present. The guarantee: `SBAtomStore` / `indirect_save` always receive a proper Penguin tile, never
a raw Python scalar.

---

## 3. `dma_copy` — `DMACopyOp` + indirect dispatch — `@0x25a430`

```c
// dma_copy(self, src, dst, mask, rmw_op, oob_mode, dge_mode, dma_qos, deps, name)  @0x25a430
PyObject *NeuronCodegen_dma_copy(self, src, dst, mask, rmw_op,
                                 oob_mode, dge_mode, dma_qos, deps, name) {
    bool si = isinstance(src, IndirectMemrefTileND);
    bool di = isinstance(dst, IndirectMemrefTileND);
    if (si && di)
        raise AssertionError(
          "``src`` and ``dst`` operands cannot both be indirect memory accesses");
    if (si) return self.indirect_load(src, ..., dst=dst, ...);     // gather fork
    if (di) return self.indirect_save(dst, value=src, ...);        // scatter fork

    validate_dma_qos(dma_qos);
    combine_tiles(src, dst, mask=mask, ...);
    inst = DMACopyOp(dst=dst, src=src, dtype=..., dge_mode=dge_mode,
                     oob_mode=oob_mode, rmw_op=rmw_op, dma_qos=dma_qos, ...);
    self.insert(inst, sema=..., deps=deps, mask=mask, name=name);
}
```

`DMACopyOp` is the generic Penguin DMA copy (any space↔space). Its
`{dge_mode, oob_mode, rmw_op}` kwargs — all three present in the name table — are exactly the
`klr::NcDmaCopy` fields that `codegenNcDmaCopy` lowers: `dge_mode`→`translateDGEMode`→`DGEType@+0xF8`,
`rmw_op(add)`→reduce field, `oob_mode`→`oob_is_err`. **The Penguin layer sets these at trace time;
the BIR layer transcribes them.** The `IndirectMemrefTileND` fork mirrors `codegenNcDmaCopy`'s own
fork into the indirect-save path.

> **GOTCHA — both-indirect is a hard error.** A DMA cannot gather *and* scatter in one descriptor.
> The literal `"``src`` and ``dst`` operands cannot both be indirect memory accesses"` is byte-exact
> in the binary. This is the trace-time guard that keeps the indirect dispatch
> single-sided.

---

## 4. `dma_transpose` — `DMATransposeLoad` / `DMATransposeCopy` / `DMAIndirectTranspose` — `@0x20bc50`

This is the largest method body in the set. It builds one of **three** Inst variants, chosen by
indirect-ness and SBUF-residency, and enforces a per-rank axes constraint with byte-exact literals.

```c
// dma_transpose(self, src/dst, axes, mask, schedule, engine, dma_qos, deps, name, ...)  @0x20bc50
PyObject *NeuronCodegen_dma_transpose(self, ..., axes, mask, engine, dma_qos, ...) {
    // ---- axes / ndim gate, keyed on the tile rank (argd.ndim) ----
    switch (tile.ndim) {
      case 2: require axes == (1, 0)       else "…provided for 2D transpose, only (1, 0) supported.";
      case 3: require axes == (2, 1, 0)    else "…provided for 3D transpose, only (2, 1, 0) supported.";
      case 4: require axes == (3, 1, 2, 0) else "…provided for 4D transpose, only (3, 1, 2, 0) supported.";
      default: raise "unsupported tile.ndim";
    }
    // gather-transpose has its own rank constraint:
    //   "…provided for gather transpose, only 3D supported."

    // ---- variant dispatch ----
    if (src.indirect_tensor_ref) {                       // (1) gather + transpose in one DMA
        ap = NeuronIndirectAP(...);
        inst = DMAIndirectTranspose(ap, ...);
    } else if (dst is sbuf) {                             // (2) HBM→SBUF transpose-load
        inst = DMATransposeLoad(par_shape, free_shape, shape_2d, ...);
    } else {                                             // (3) SBUF↔SBUF transpose-copy
        inst = DMATransposeCopy(par_shape, free_shape, shape_2d, ...);
    }
    self.insert(inst, ...);
}
```

The transpose Inst is built from the reshaped `{par_shape, free_shape, shape_2d}` of an `NDTile`
(via `np.prod`/reshape). The `engine` kwarg exists at the Penguin level here (the BIR side pins this
to the Pool engine). All three class names — `DMATransposeLoad`, `DMATransposeCopy`,
`DMAIndirectTranspose` — are live strings in the binary.

> ⭐ **The XZYW permutation is byte-exact across layers.** The 4-D axes literal `(3, 1, 2, 0)` sits
> in the binary, and it is the *same* permutation the BIR `DmaTranspose` pins as **XZYW**
> (`TransposeOps=11=XZYW`, source `[W,Z,Y,X]`→`[X,Z,Y,W]`, perm `[3,1,2,0]`). This is an independent
> cross-confirmation that the Penguin `dma_transpose` and the BIR `DmaTranspose` are the same
> operation — the trace-time tracer and the C++ codegen leaf agree on the permutation byte-for-byte.
> See [2.21 DMA encoding](../isa/dma-encoding.md) and [Penguin DGE-level dynamic DMA](../penguin/dge-level-dynamic-dma.md).

> **GOTCHA — `oob_mode.skip` is rejected for direct DMA.** The literal, byte-exact:
> `"oob_mode.skip is not supported for direct memory access. Only oob_mode.error is allowed (got …)"`.
> A direct (non-indirect) transpose-DMA may only carry `oob_mode.error`; `skip` is reserved for the
> indirect/gather path, where OOB indices are legitimately skipped.

> **NOTE — gather-transpose is 3D-only.** The literal `"…provided for gather transpose, only 3D
> supported."` gates the `DMAIndirectTranspose` variant to a 3-D tile rank — narrower
> than the 2/3/4-D the direct transpose accepts.

---

## 5. `indirect_load` / `indirect_save` — `NeuronIndirectLoad` / `NeuronIndirectSave` / `NeuronIndirectRMW`

These are the **index-driven gather/scatter**. Both read the indirect access pattern off the
`IndirectMemrefTileND` pointer rather than from `canonical_*_indices`:

- `pointer.indirect_tensor_ref` — the index tensor;
- `pointer.generic_addrs` — the per-index base-address list;
- `pointer.generic_dims` — the indirect-dim coefficients;
- `pointer.is_offset_dma` — the register/offset-DMA flag (dynamic offset supplied by a register).

All four are in the name table. They are the Penguin-level twins of the BIR index-AP
coefficient (indirect-dim) and the offset-DMA path.

### 5.1 `indirect_load` — `@0x25d880` — `NeuronIndirectLoad`

```c
// indirect_load(self, pointer, mask, dst, schedule, deps, name, mode, dge_mode, dma_qos)  @0x25d880
PyObject *NeuronCodegen_indirect_load(self, pointer, mask, dst, ..., mode, dge_mode, dma_qos) {
    validate_dma_qos(dma_qos);
    idx  = pointer.indirect_tensor_ref;
    addr = pointer.generic_addrs;  dims = pointer.generic_dims;
    off  = pointer.is_offset_dma;                            // offset-DMA (register) form?
    combine_tiles(...);
    dst_tile = InstTile(...);
    inst = NeuronIndirectLoad(dst_tile, idx, addr, dims, is_offset_dma=off, mode=mode, ...);
    self.insert(inst, ...);
}
```

`NeuronIndirectLoad` is the Penguin gather-load; `codegenNeuronIndirectLoad` lowers
it to the BIR `GenericIndirectLoad(42)` family, which concretizes to a `ReadVarAddr`→TSP→Memset→
IndirectLoad chain. `is_offset_dma` selects the offset-DMA (register-supplied dynamic offset) form.

### 5.2 `indirect_save` — `@0x256b80` — `NeuronIndirectSave` / `NeuronIndirectRMW`

```c
// indirect_save(self, pointer, value, mask, rmw_op, schedule, deps, name, mode, dge_mode)  @0x256b80
PyObject *NeuronCodegen_indirect_save(self, pointer, value, mask, rmw_op, ...) {
    idx  = pointer.indirect_tensor_ref; addr = pointer.generic_addrs;
    dims = pointer.generic_dims; off = pointer.is_offset_dma; oob = pointer.oob_mode;
    if (rmw_op is set) {
        assert rmw_op == np.add,
            "scatter op can only support bypass or add for now";
        assert oob_mode in {error},
            "scatter add cannot have other modes except for error";
        // (and: "``dst_rmw_op`` is not supported indirect memory access on source (``src``) operand")
        inst = NeuronIndirectRMW(value, idx, addr, dims, rmw_op, oob, ...); // accumulate scatter
    } else {
        inst = NeuronIndirectSave(value, idx, addr, dims, oob, ...);        // overwrite scatter
    }
    self.insert(inst, ...);
}
```

Both class names and all three error literals are live strings. The semantics map exactly
to the BIR scatter: `compute_op=none`→overwrite (`NeuronIndirectSave`), `compute_op=add`→accumulate
(`NeuronIndirectRMW`). `rmw_op=np.add` here is `DgeComputeOp 2 (add)` there.

> **GOTCHA — scatter accumulation is add-only.** `"scatter op can only support bypass or add for
> now"` means the only RMW the scatter supports is `np.add`; any other reduction op is a
> trace-time `AssertionError`. And when accumulating, the OOB mode is pinned to `error`
> (`"scatter add cannot have other modes except for error"`).

---

## 6. `local_gather` / `gather_flattened` — `IndirectCopy` / `PoolGather` (on-engine gathers)

These two are *not* DMA — they are on-engine gathers. The distinction matters: `local_gather` runs on
GpSIMD with a `uint16` index, `gather_flattened` runs on the Pool engine with a `uint32` index.

### 6.1 `local_gather` — `@0xdfea0` — `IndirectCopy`

```c
// local_gather(self, src_buffer, index, num_elem_per_idx, num_valid_indices, mask, schedule, deps, name)  @0xdfea0
PyObject *NeuronCodegen_local_gather(self, src_buffer, index, num_elem_per_idx,
                                     num_valid_indices, mask, ...) {
    assert num_valid_indices <= total_indices_per_core,
        "num_valid_indices cannot exceed total indices per core";
    assert num_valid_indices <= 4096,
        "number of indices per core must be <= 4096";
    assert num_elem_per_idx in [1,2,4,8,16,32],
        "num_elem_per_idx must be one of [1, 2, 4, 8, 16, 32]";
    assert src_buffer.partition_size == index.partition_size,
        "src_buffer and index must have same partition dimension size";
    src_ap = NeuronAP / APIndex over src_buffer;
    idx_ap = NeuronIndicesAP(linearize(index),                           // uint16 index
                substituteApIndices(generate_subst_map(...)));           // index→address subst
    inst = IndirectCopy(src_ap, idx_ap, num_elem_per_idx, num_valid_indices, ...);
    self.insert(inst, ...);
}
```

`IndirectCopy` is the GpSIMD/on-engine **local** gather: intra-SBUF, partition-parallel, `uint16`
index, with `num_elem_per_idx` being the contiguous run pulled per index. `codegenIndirectCopy`
lowers it to the BIR `IndirectCopy`. All four bounds literals are byte-exact, as are `NeuronAP`,
`NeuronIndicesAP`, `substituteApIndices`, and `generate_subst_map`.

### 6.2 `gather_flattened` — `@0x2762e0` — `PoolGather`

```c
// gather_flattened(self, data, indices, mask, dtype, schedule)  @0x2762e0
PyObject *NeuronCodegen_gather_flattened(self, data, indices, mask, dtype, schedule) {
    assert_dtype_in(indices, uint32);                                    // index dtype gate
    assert_min_dimensions(data); assert_max_dimensions(data);            // ≤ max dims
    free = extend_free_indices(data);                                    // flatten data free axes
    idx_ap = NeuronIndicesAP(indices.par_shape, indices.free_shape,
                substituteApIndices(generate_subst_map(...)));
    inst = PoolGather(data="gather_data", indices="gather_indices", free, idx_ap, dtype, ...);
    self.insert(inst, ...);
}
```

`PoolGather` is the **Pool-engine** flattened gather: `uint32` index, multi-dim `data` flattened to
free indices. The internal tile names `"gather_data"`/`"gather_indices"` are literals
(`__pyx_k_gather_data`, `__pyx_k_gather_indices`). The dtype/dimension asserts
(`assert_dtype_in`, `assert_min_dimensions`, `assert_max_dimensions`) are the legality gate.
`codegenPoolGather` lowers it to the BIR `PoolGather`.

---

## 7. `shared_constant` / `shared_identity_matrix` / `get_identity_tensor` — const & identity materialization

### 7.1 `shared_constant` — `@0xaa940` — `NeuronWeightTensor` | `Tensor`

```c
// shared_constant(self, constant, dtype)  @0xaa940
PyObject *NeuronCodegen_shared_constant(self, constant, dtype) {
    arr = static_cast(constant);                            // → numpy array view
    if (self.opts.enable_const_rewrite) {                   // path A: const-rewrite middle-end folds it
        t = Tensor(shape=arr.shape, ...) off self.function.tensor;
        return t;
    } else {                                                // path B: DRAM constant section
        w = NeuronWeightTensor(arr, dtype, shape, ...);
        self.function.constants.append(w);                  // push onto the constant/weight section
        return TensorRef(w);
    }
}
```

`enable_const_rewrite`, `NeuronWeightTensor`, `constants`, and `TensorRef` are all in the
name table. The False path is the constant→DRAM materialization: a numpy/scipy constant becomes a
`NeuronWeightTensor` pushed onto `function.constants` (the kernel's constant/weight section, a
DRAM/HBM resident at lowering), surfaced to callers as a `TensorRef`. `splitNeuronWeightTensor`
handles weight-tile splitting downstream; that symbol lives in the BIR codegen binary, **not** this
one — it is absent from this `.so`'s string table, consistent with it being a later-stage name.

> **GOTCHA — `shared_constant` has two sinks, not one.** "Constant → DRAM resident" describes only
> the `enable_const_rewrite=False` path shown above. With `enable_const_rewrite=True`,
> `shared_constant` returns a plain graph `Tensor` off `self.function.tensor` for the const-rewrite
> middle-end to fold — no `NeuronWeightTensor`, no `function.constants` append.

### 7.2 `shared_identity_matrix` — `@0x7b490` — `IdentityWeightTensor`

```c
// shared_identity_matrix(self, n, dtype)  @0x7b490
PyObject *NeuronCodegen_shared_identity_matrix(self, n, dtype) {
    ident = np.identity(n);                                 // n×n identity
    w = IdentityWeightTensor(ident, dtype, shape, ...);     // identity-specialized weight
    return TensorRef(w);                                    // DRAM-resident n×n identity
}
```

`IdentityWeightTensor` is the identity-specialized sibling of `NeuronWeightTensor`
— the DRAM-resident `n×n` identity matrix.

### 7.3 `get_identity_tensor` — `@0xa3470` — lazy front-of-kernel `SBAtomLoad`

```c
// get_identity_tensor(self, ...)  @0xa3470
PyObject *NeuronCodegen_get_identity_tensor(self, ...) {
    if (self.identity_weight_hbm == NULL) {                 // (a) cached DRAM identity attr
        self.identity_weight_hbm =                          // (b) materialize once
            self.shared_identity_matrix(n=statebuf_num_partitions, dtype=int8);
    }
    enter kernel_scope_2 / spmd_block;                      // (c) at kernel head
    builder.set_insert_position(FRONT of block);            //     so it loads ONCE at the top
    dst = InstTile(...);
    inst = SBAtomLoad(self.identity_weight_hbm.as_tile() → SBUF, ...);
    self.insert(inst, FRONT);
    return dst.as_tensor();
}
```

This is the identity-into-SBUF materialization: the DRAM identity (`IdentityWeightTensor`) is loaded
by **one** front-of-kernel `SBAtomLoad` into a cached SBUF tile, which is then used as the
**stationary matmul operand for transpose** (transpose-via-matmul). The lazy cache key is
`self.identity_weight_hbm`, and `builder.set_insert_position` to the block front is
why it loads exactly once at the kernel head. This is the Python/Penguin source of the BIR
`getIdentityMatrix` (the per-dtype 128×128 SBUF identity, name-cached, pushed to the
`legalizeIdentityMatrices` vector).

> **NOTE — partition count `n`.** `n = statebuf_num_partitions`, with `dtype=int8`. The integer is
> *not* a literal in this binary; it resolves from the compiled arch/target module. It is almost
> certainly **128**, by the BIR `getIdentityMatrix` 128×128 geometry — but read from *this* `.so`
> alone the value is [INFERRED].

---

## 8. `create_tensor` / `memset` / `broadcast_partition` / `stream_shuffle` / `load_tensor_to_register`

### 8.1 `create_tensor` — `@0x8b0e0` — the tensor factory (no Inst)

```c
// create_tensor(self, parent_scope)  @0x8b0e0
PyObject *NeuronCodegen_create_tensor(self, parent_scope) {
    read self.opts.enable_scalar_expansion;
    base = self.block.tensor  // or self.function.tensor
    return fresh Tensor scoped under parent_scope;          // NO Inst emitted
}
```

`enable_scalar_expansion`, `block.tensor`, and `function.tensor` are all in the name table. This is the
tensor/temporary **factory** the other emitters draw fresh tiles from — it emits no Inst.

### 8.2 `memset` — `@0x19aac0` — `MemsetOp`

```c
// memset(self, tile_shape, value, dtype, mask, dst_tile, schedule, engine, deps, name)  @0x19aac0
PyObject *NeuronCodegen_memset(self, tile_shape, value, dtype, mask, dst_tile, ..., engine, ...) {
    assert_shape_valid(tile_shape); assert_num_partition(...);
    assert engine in {NeuronEngine.Vector, NeuronEngine.GpSIMD, NeuronEngine.Unknown},
        "Memset engine can only be Vector or GpSIMD or Unknown.";
    dst = NDTile(statebuf_num_partitions, shape_2d, ...);
    sv  = ScalarValue(value);                                            // wrap the fill value
    inst = MemsetOp(dst, value=sv, dtype=dtype, engine=engine, ...);
    self.insert(inst, mask=mask, deps=deps, name=name);
}
```

`MemsetOp` is the Penguin constant-fill; `value` is wrapped in a `ScalarValue`.
`codegenMemsetOp` lowers it to the BIR `Memset`.

> **GOTCHA — memset is a compute op, not a DMA.** The engine restriction
> `"Memset engine can only be Vector or GpSIMD or Unknown."` reflects that
> memset is executed on a vector or GpSIMD engine — there is no DMA-engine memset path. `Unknown`
> defers the engine choice to a later assignment pass.

### 8.3 `broadcast_partition` — `@0xad480` (inner `@0x1beaf0`) — delegates to `simple_unary`

```c
// broadcast_partition(self, tensor, mask, dtype, name, schedule, deps)  @0xad480
PyObject *NeuronCodegen_broadcast_partition(self, tensor, mask, dtype, name, ...) {
    // inner closure @0x1beaf0:
    ap build_broadcast_partition() {
        ap = NeuronIndicesAP(par_indices, free_indices);
        create_broadcast_partition(ap); set_shape(ap);     // replicate one partition across the band
        return ap;
    }
    return self.simple_unary(tensor, ..., ap_builder=build_broadcast_partition);
}
```

`create_broadcast_partition`, `simple_unary`, and `NeuronIndicesAP` are all in the name table.

Unlike every other method in this section, `broadcast_partition` constructs no memory Inst of its
own. It builds a partition-broadcast access pattern — the inner `build_broadcast_partition` closure
at `0x1beaf0`, which replicates one partition across the band — and then defers to `simple_unary`,
so it lowers as a **unary op** whose AP broadcasts across partitions. `codegenBroadcastPartition` is
the BIR-side name.

### 8.4 `stream_shuffle` — `@0x187ea0` — `StreamShuffleInst`

```c
// stream_shuffle(self, src, dst, shuffle_mask, mask, dtype, schedule, deps, name)  @0x187ea0
PyObject *NeuronCodegen_stream_shuffle(self, src, dst, shuffle_mask, mask, dtype, ...) {
    validate err_valid_size;
    assert src.dtype == dst.dtype, err_src_dst_same_dtype;
    ap = NeuronIndicesAP(...);
    ds = DynamicScalar(shuffle_mask); set_shape(ds);                     // mask as DynamicScalar
    inst = StreamShuffleInst(src, dst, shuffle_mask=ds, ap, ...);
    self.insert(inst, ...);
}
```

`StreamShuffleInst` is the cross-partition stream shuffle (DVE/stream engine); `shuffle_mask` is
the per-lane permutation carried as a `DynamicScalar`.
`codegenStreamShuffleInst` lowers it.

### 8.5 `load_tensor_to_register` — `@0x111740` — `LoadTensorToRegister`

```c
// load_tensor_to_register(self, pointer, mask, dtype, deps, name)  @0x111740
PyObject *NeuronCodegen_load_tensor_to_register(self, pointer, mask, dtype, deps, name) {
    reg = DynamicScalar(...);                               // destination register
    self.dst_registers.append(reg);                        // bind into dst_registers
    combine_tiles(pointer, ...);
    par = pointer.canonical_par_indices; free = pointer.canonical_free_indices(...);
    inst = LoadTensorToRegister(reg, pointer, par, free, dtype, ...);
    self.insert(inst, deps=deps, name=name);
}
```

`LoadTensorToRegister` is the Penguin twin of BIR `InstTensorLoad(75)`: a runtime/register-addressed
scalar load. It reads a tensor element into a named `DynamicScalar` register (`dst_registers`) — the
dynamic address/offset source for
`indirect_load`'s `is_offset_dma` path. `codegenLoadTensorToRegister` lowers it.

> **NOTE — two distinct indirection mechanisms.** The **dynamic-offset family**
> (`indirect_load`/`indirect_save` with `is_offset_dma`, plus `load_tensor_to_register`'s
> `DynamicScalar`) is the *register-addressed* path → BIR `TensorLoad(75)`/offset-DMA. The
> **index-vector family** (`NeuronIndirectLoad`/`NeuronIndirectSave`/`NeuronIndirectRMW`,
> `PoolGather`, `IndirectCopy`) is the *index-AP gather/scatter* → BIR `GenericIndirect*`/`PoolGather`.
> Both are present in this module; they are not interchangeable.

---

## 9. The full lowering arc

Where these Penguin Insts go after `self.insert`:

```
NeuronCodegen.<method>  (THIS page)
    builds a Penguin Inst (SBAtomLoad / DMACopyOp / MemsetOp / NeuronIndirect* /
    PoolGather / IndirectCopy / DMATranspose* / StreamShuffleInst / …) + self.insert
    into the Penguin Function.
        │
        ▼
BirCodeGenLoop  (6.5.12 / Penguin schema)
    walks the Function; per-Inst calls codegen<Op>  (codegenSBAtomLoad, codegenDMACopyOp,
    codegenMemsetOp, codegenNeuronIndirectLoad/Save/RMW, codegenPoolGather, codegenIndirectCopy,
    codegenDMATranspose, codegenStreamShuffleInst, codegenLoadTensorToRegister,
    codegenBroadcastPartition) → builds bir::Instruction (AP algebra via createAP/addAP/evalIndices).
        │
        ▼
libwalrus KlirToBirCodegen  (klr → BIR; see 2.21)
    codegenNcDmaCopy → InstDMACopy(32),  codegenDmaTranspose → InstDMACopy CopyMode=Transpose XZYW,
    codegenGenericIndirectSave → IT44,  codegenTensorLoad/Store → IT75/76.
        │
        ▼
strided/indirect concretize → engine-id → lower_dma descriptor materialization → encoders → sim.
```

The `{dge_mode, oob_mode, rmw_op}` kwargs set on the Penguin Inst *here* flow straight to the BIR
fields the DMA codegen documents (`DGEType@+0xF8`, `oob_is_err@+0x130`, reduce field). QoS-class and
engine-id remain deferred to later passes.

---

## Evidence Anchors

- **`dma_transpose` 4-D perm `(3,1,2,0)` == BIR XZYW.** `strings` on the `nki/` `.so` returns
  `" provided for 4D transpose, only (3, 1, 2, 0) supported."` byte-exact, alongside the 2-D `(1,0)`
  and 3-D `(2,1,0)` siblings and the gather-transpose 3-D-only literal.
- **`load`→`SBAtomLoad`, `store`→`SBAtomStore`.** Both class-name strings appear four times each; the
  `init_value` / `SetAttrStr` OOB-fill on `load` rests on the `init_value` name symbol.
- **Scatter is add-only.** `"scatter op can only support bypass or add for now"`,
  `"scatter add cannot have other modes except for error"`, and
  `"``src`` and ``dst`` operands cannot both be indirect memory accesses"` are all byte-exact, with
  the `NeuronIndirectSave` / `NeuronIndirectRMW` strings present alongside them.
- **`memset` engine restriction.** `"Memset engine can only be Vector or GpSIMD or Unknown."` plus
  the `MemsetOp` / `ScalarValue` strings.
- **`get_identity_tensor` = front-of-kernel `SBAtomLoad` of a cached DRAM identity.**
  `identity_weight_hbm`, `statebuf_num_partitions`, `IdentityWeightTensor`, `set_insert_position`,
  `as_tile`/`as_tensor`, and `spmd_block`/`kernel_scope` all resolve.
- **The remaining surface.** `local_gather` bounds (`<= 4096`, `[1, 2, 4, 8, 16, 32]`, partition-size
  equality), the indirect-AP fields (`indirect_tensor_ref`, `generic_addrs`, `generic_dims`,
  `is_offset_dma`), `gather_flattened` (`PoolGather`, `gather_data`/`gather_indices`,
  `assert_dtype_in`), and the plumbing symbols (`add_named_instruction`, `process_dep_edges`,
  `process_new_neuroninst`, `combine_tiles`×61, `validate_dma_qos`×24,
  `canonical_par_indices`/`canonical_free_indices`) each resolve directly in the binary's
  symbol/string table.

The one quantity on this page that is *not* readable from this `.so` is the identity partition-count
integer `n`, flagged in §7.3.
