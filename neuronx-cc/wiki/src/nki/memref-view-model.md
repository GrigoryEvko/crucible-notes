# Memref / View / Access Model (Concrete Tensors)

> *All symbols, offsets, and strings on this page apply to neuronx_cc 2.24.5133.0+58f8de22, module `neuronxcc/nki/compiler/backends/neuron/tensors.cpython-310-x86_64-linux-gnu.so` (the cp310 wheel; cp311/cp312 differ only in offsets). Offsets are the Cython wrapper symbol addresses within that `.so`.*

## Abstract

The abstract NKI tile/tensor base in [`tensor.so`](tile-tensor-model.md) is almost entirely
`NotImplementedError`: it fixes the Python protocol (indexing, slicing, `tensor_ir_class`,
`create_tensor`) but provides no bodies. `tensors.so` is where every one of those hooks lands.
Its module docstring is literally *"Define the subclasses that implement nki tensor."* This page
documents the concrete layer: how a NKI program's `tensor[p_slice, f_slice, ...]` expression turns
into a [Penguin](../penguin/tensor-buffer-node.md) access-pattern node that an instruction can read
or write.

There are two orthogonal class families. **Tensors** are the lvalue roots — a `KernelTensor` is the
whole buffer the kernel allocates in one address space, and it owns exactly one Penguin IR tensor
(`NeuronSBTensor` / `NeuronPSUMTensor` / `NeuronLocalTensor`, the *same* classes documented in
[5.2](../penguin/tensor-buffer-node.md)). **Views and tiles** are everything you slice out of a
tensor: `MemrefTile`/`MemrefTileND` (an address view that owns no buffer, only the metadata to emit
an access pattern), `IndirectMemrefTileND` (gather/scatter), `DataTile`/`InstTile` (materialised
value tiles that the tile *algebra* operates on), `TensorRef`/`TensorView` (aliased and affine
views), and the dynamic-index tiles that carry runtime-valued offsets.

The whole subsystem funnels into one lowering choke point. A slice becomes a concrete view via the
factory `build_memref_tile`; that view emits a Penguin access pattern via
`MemrefTile.generate_access_impl`, which constructs `AffineExpr` index expressions
([5.4](../penguin/affine-expr-algebra.md)) over named `Axis` objects and then instantiates an
`ap_cls` — `NeuronAP` for the default case, swapped for `NeuronNordsetAP`, `NeuronIndirectAP`, or
`NeuronIndirectRMW` to change the access *kind* without changing the view machinery. This page walks
the tensor factory, the view classes, the AP emitter, the indirect path, and the bounds check, in
that order.

For reimplementation, the contract is:

- **The tensor → Penguin-IR down-map**: the 3-way `create_tensor` dispatch on buffer kind, and the
  exact attribute set passed to the IR tensor constructor.
- **The view-class taxonomy and the `build_memref_tile` dispatch** that picks one of four concrete
  view classes from the shape of the index tuple.
- **The AP emitter** (`generate_access_impl`): how `addrs` + `access_shape` + partition geometry
  become an `AffineExpr`-indexed `ap_cls` node, and the `ap_cls` polymorphism that selects the
  Neuron AP variant.
- **The indirect (gather/scatter/RMW) model**: `generic_dims`/`generic_addrs`, the single-indirect-dim
  rule, and the offset-DMA-vs-RMW lowering choice.
- **`check_buffer_overflow`**: the per-partition byte-footprint bound against SBUF/PSUM limits.

| | |
|---|---|
| **Module** | `tensors.cpython-310-x86_64-linux-gnu.so` (4.44 MB Cython ext) |
| **Tensor factory** | `KernelTensor.create_tensor` @ `0xa3a00` — 3-way buffer dispatch |
| **View factory** | `build_memref_tile` @ `0x5abe0` — 4-way view-class dispatch |
| **AP emitter** | `MemrefTile.generate_access_impl` @ `0xa8550` → `AffineExpr` → `ap_cls(...)` |
| **Bounds check** | `check_buffer_overflow` @ `0x970b0` — SBUF/PSUM per-partition limit |
| **Down-map target** | `neuronxcc.starfish.penguin.ir` — `Neuron{SB,PSUM,Local}Tensor`, `Neuron{,Nordset,Indirect}AP`, `NeuronIndirectRMW`, `AffineExpr`, `Axis` |
| **Address spaces** | `sbuf`, `psum`, `hbm`, `internal` (string table) |

---

## The Two Class Families

Before the mechanism, fix the taxonomy. The inheritance edges below are read from override patterns and `super().__init__` chains rather than from a byte-level class layout:
they are inferred from which class overrides which abstract method, from the `super().__init__` call
chains, and from the `"base ref tile"` / `"base data tile"` `NotImplementedError` diagnostics that
mark the abstract roots.

```text
tensor.so  (abstract; bodies raise NotImplementedError)        [tile-tensor-model.md]
  ├── KernelTensor            full allocated buffer — owns one Penguin tensor
  │     └── KernelBlockTensor      blocked / multi-buffer (overrides create_tensor only)
  ├── TensorRef               aliased / argument tensor — indexing DISABLED
  └── TensorView              affine view (offset + scale + sliced_dims)

tile.so  (abstract tile)
  ├── MemrefTile              base address view (tensor + tile + dtype + access_shape + ap_cls)
  │     └── MemrefTileND          ND-indexed view (indices tuple)   ← the workhorse view
  │           └── IndirectMemrefTileND   gather/scatter (generic_dims / generic_addrs)
  ├── DataTile                materialised value tile (the tile-algebra operand)
  │     └── InstTile              instruction-output tile (inst, pending_allocation)
  ├── IndexValueTile          a runtime index VALUE wrapped as a tile
  └── DynamicIndexTile        a per-dim index = (static base offset + runtime index)
```

The split matters: a `MemrefTile` is **pure address metadata** — it forbids `buffer`/`ir` until
backed — whereas a `DataTile` is a **value operand** that carries a predicate (mask) set and is what
operator overloads (`@`, `+`, `astype`, `broadcast_to`) act on. The bare `DataTile` is itself
abstract for the IR handle: it ships the diagnostics `"buffer not implemented for base data tile"`
and `"ir not implemented for base data tile"` (both string-table literals) — the concrete IR handle
arrives only via its `tile` or via the `InstTile` subclass.

> **NOTE —** the Penguin tensor classes `NeuronSBTensor` / `NeuronPSUMTensor` / `NeuronLocalTensor`
> named here are *the same classes* documented in [5.2 — Tensor / Buffer Node](../penguin/tensor-buffer-node.md);
> `tensors.so` does not redefine them, it constructs them by module-global name lookup. The names are
> Identical on both pages (cross-checked against `tensor-buffer-node.md`).

---

## KernelTensor — the Allocated Buffer

### Purpose

A `KernelTensor` is the whole physical buffer a kernel allocates in one address space — SBUF, PSUM,
DRAM/HBM, or internal-local. It is the lvalue root: every tile and view on this page is a slice of
exactly one `KernelTensor`. It is the concrete subclass that fills in the abstract `tensor` hooks
left as `NotImplementedError` in `tensor.so`.

### Construction

The `__init__` signature is read from the ordered `__pyx_pyargnames` table of the Cython
wrapper (`__pyx_pw_..._12KernelTensor_1__init` @ `0xb95f0`):

```c
// KernelTensor.__init__  @ 0xb95f0
KernelTensor(self, tile_shape, block_shape, block_indices, dtype, buffer,
             parent_scope, name, ir_type=None, tensor=None,
             init_value=..., allocation=..., volatile=..., vnc_addr_space=...)
//   tile_shape     : per-tile logical shape — the unit one instruction accesses
//   block_shape    : outer block tiling factor; multi-buffer / double-buffer count lives here
//   block_indices  : symbolic indices into the block grid (loop ivars)
//   buffer         : address-space kind ∈ {sbuf, psum, hbm, internal}   (strings)
//   parent_scope   : owning NKI scope (nki_ctx)
//   tensor         : optional pre-built Penguin IR tensor (wrap an existing one)
//   vnc_addr_space : Virtual-NeuronCore address-space partitioning attribute
```

Instance fields set in the body (`PyObject_SetAttr` targets): `self.dtype`,
`self.buffer`, `self.tensor` (the Penguin IR tensor), `self.init_value`, `self.parent_scope`,
`self.allocation`, `self.volatile`, `self.vnc_addr_space`. Crucially the body also performs
`self.tensor.name = self.name`, propagating the Python-level name down into the IR node. When the
`tensor` argument is `None`, the Penguin tensor is built lazily by `create_tensor`.

### Algorithm — create_tensor (the down-map factory)

`create_tensor` is the bridge that lowers a `KernelTensor` into a `starfish.penguin` buffer node. It
is a classmethod with a 3-way dispatch on the buffer kind. The dispatch order and line numbers below
are read directly from the decompiled wrapper
(`__pyx_pw_..._12KernelTensor_23create_tensor` @ `0xa3a00`): the module-global name lookups
`NeuronLocalTensor` (line 965), `NeuronSBTensor` (line 1402), `NeuronPSUMTensor` (line 1553) appear
in exactly that order.

```c
// KernelTensor.create_tensor  @ 0xa3a00  (classmethod — the DOWN-MAP factory)
function create_tensor(cls, ..., buffer, ...):
    access_shape = cls.compute_access_shape(tile_shape)      // per-partition footprint
    block_shape  = cls.extract_allocated_block_shape(...)    // calls nested
                                                             //   extract_multi_buffer_factor()
                                                             //   → pulls the N-buffer factor out
    // 3-way dispatch on the address space:
    if buffer in {internal, local}:
        ir_tensor = NeuronLocalTensor(...)                   // line 965  [5.2]
    elif buffer == sbuf:
        ir_tensor = NeuronSBTensor(...)                      // line 1402 [5.2]
    elif buffer == psum:
        ir_tensor = NeuronPSUMTensor(...)                    // line 1553 [5.2]

    // ctor receives (__pyx_n_s_* attr keys present in this wrapper):
    //   access_shape, block_shape, sizeinbytes, dtype, name, parent_scope,
    //   init_value, allocation, volatile, vnc_addr_space, backend, target,
    //   opt_level (OptLevel), enable_scalar_expansion, skip_allocators,
    //   resetAllocation, required_local_tensor_align_in_bytes
    return ir_tensor
```

The eight `__pyx_n_s_*` keys `access_shape`, `block_shape`, `sizeinbytes`, `vnc_addr_space`,
`opt_level`, `enable_scalar_expansion`, `skip_allocators`, `resetAllocation` are present in
the wrapper's identifier table. `access_shape` comes from `KernelTensor.compute_access_shape` (folds
`tile_shape` into the per-partition access footprint); `block_shape` comes from
`extract_allocated_block_shape`, whose nested `extract_multi_buffer_factor()` helper pulls the
multi-buffer (double-/N-buffer) factor out of the block dims.

`KernelTensor.tensor_ir_class` returns the chosen `NeuronSBTensor`/`NeuronPSUMTensor` class — the
abstract hook `tensor.so` left unimplemented. `KernelTensor.backend` is typed
`Optional[Union[Tonga, Sunda]]` (string), selecting the ISA family (Tonga vs Sunda) that
the lowered tensor and access pattern eventually target.

### Reshape / expand_dims restrictions

Two override methods carry hard diagnostics that constrain the data model:

- `KernelTensor.reshape` / `_reshape_impl`: `"reshape tensor cannot change the number of elements.
  Old shape: "` and `"Can only reshape tensors in HBM"`. Reshape is **restricted to DRAM/HBM
  buffers** — you cannot reshape an SBUF/PSUM-resident tensor, because its partition geometry is
  fixed by allocation.
- `KernelTensor.expand_dims`: `"Cannot expand dims before partition dimension for local tensors!"`.

> **QUIRK —** dimension 0 is the **partition dimension** and is special everywhere in this model.
> `expand_dims` cannot insert an axis before it, `reshape` cannot fold it on-chip, and (below) the
> overflow check bounds the buffer by *per-partition* bytes, not total bytes. A reimplementation that
> treats dim 0 as an ordinary axis will mis-size every on-chip allocation.

### KernelBlockTensor

`KernelBlockTensor` is a `KernelTensor` whose outer block dimension is explicit — a grid /
array of tiles, e.g. a `multi_buffer`'d SBUF region. It contributes exactly one distinct symbol,
`KernelBlockTensor.create_tensor`, an override that builds the blocked IR-tensor variant; all other
behaviour is inherited from `KernelTensor`.

---

## MemrefTile / MemrefTileND — the Address View

### Purpose

A `MemrefTile` is a **view** over a `KernelTensor`: it owns no buffer, only a reference to one plus
the metadata needed to emit a Penguin access pattern. `MemrefTileND` specialises it to an
N-dimensional index tuple, and is the workhorse produced when you slice a `KernelTensor`.

### Construction

```c
// MemrefTile.__init__  @ 0x9abe0   (ordered __pyx_pyargnames)
MemrefTile(self, tensor, tile, dtype, access_shape, ap_cls)
//   tensor       : the underlying Penguin IR tensor (from KernelTensor.tensor)
//   tile         : the logical tile object (shape unit; ties back to the KernelTensor)
//   access_shape : per-access footprint (partition × free dims) this view reads/writes
//   ap_cls       : THE ACCESS-PATTERN CLASS to instantiate when emitting the AP
// fields set (SetAttr): self.tensor, self.tile, self.dtype,
//                                 self.access_shape, self.ap_cls, self.shape

// MemrefTileND.__init__  @ 0xb0690
MemrefTileND(self, indices):
    super().__init__(...)                  // MemrefTile — wires tensor/tile/dtype/access_shape/ap_cls
    self.indices = indices                 // tuple of per-dim index objects
                                           //   (ints, slices, AffineExprs, dynamic-index tiles)
    self.check_indices()                   // err_indices_shape_mismatch on rank mismatch
    // folds the incoming index tiles via module helpers:
    //   _extract_shape, cached_combine_index_tiles  → single combined ND index set
```

`ap_cls` is the polymorphism knob: the *same* view machinery emits different Penguin AP node types
purely by swapping this class — `NeuronAP` (default ND), `NeuronNordsetAP` (no-reorder set),
`NeuronIndirectAP` / `NeuronIndirectRMW` (indirect). All four names are present in the module
string table.

`MemrefTile.buffer` delegates to `self.tensor.buffer`; `MemrefTile.ir` returns the lowered Penguin
handle; `MemrefTile.check_indices` validates index rank against the tile and raises
`err_indices_shape_mismatch` / `"Expect len of indices <n> indices, got <m>"`.

`MemrefTileND` adds the index-level properties: `nd_indices` (the normalised per-dim index list),
`index_exprs` (the `AffineExpr` list, one per dim, that feeds the AP), `indices_set` (the active
index dims), and `member_tuple` — the `(tensor, indices, ...)` identity tuple used as the hash/eq
key for view caching.

### Algorithm — generate_access_impl (the AP emitter)

This is the single choke point where a Python-side tiled view becomes a Penguin access-pattern node.
`MemrefTile.generate_access(addrs)` @ `0x45030` is a thin forwarder to
`self.generate_access_impl(addrs, self.ap_cls)`; the body lives in `generate_access_impl` @ `0xa8550`
(2381 lines of Cython).

```c
// MemrefTile.generate_access_impl  @ 0xa8550   (THE AP emitter — core lowering)
function generate_access_impl(self, addrs, ctor):     // ctor == ap_cls
    npart   = self.tensor.npartitions                 // partition (dim-0) extent
    pstride = self.tensor.partition_size              // per-partition byte stride

    // build one AffineExpr index expression per free dim from `addrs`:
    idx_exprs = [ AffineExpr(addr_for(dim)) for dim in free_dims ]   // [5.4], over Axis objects

    // canonicalise the AP:
    idx_exprs = keep_ap_indicies_linear_expr(idx_exprs)  // keep dims whose index is affine/linear
    idx_exprs = drop_ap_indicies(idx_exprs)              // collapse trivial/unit dims

    for addr in addrs:
        if addr is None:
            raise "NoneType address encountered while generating access for " + self.name
            //  ^ string-table literal — fires on an unbound symbol

    ap = ctor(tensor=self.tensor, npartitions=npart, partition_size=pstride,
              index_exprs=idx_exprs, dtype=self.dtype, sema=...)   // ap_cls(...) → Neuron…AP node
    nki_assert(ap is well-formed)
    return ap
```

Inputs are attrs of the wrapper: `addrs`, `ctor` (the AP constructor), `tensor`, `dtype`,
`access_shape`, `shape`, `partition_size`, `npartitions`, `sema`. The canonicalisation helpers
`keep_ap_indicies_linear_expr` and `drop_ap_indicies` and the diagnostic
`"NoneType address encountered while generating access for "` are all in the string table.

> **NOTE —** the exact affine arithmetic *inside* `generate_access_impl` (how `addrs` and the loop
> ivars are combined into each `AffineExpr`) is fully inlined/obfuscated by Cython. This page reports
> the inputs, the helper calls, and the output node, but does not invent the intermediate algebra.
> The index-expression *construction* primitives themselves (`AffineExpr`, `Axis`, `substitute`) are
> owned by [5.4](../penguin/affine-expr-algebra.md) and the `indexing.so` module, not by `tensors.so`.

### ap_tile and nordset_ap

`MemrefTileND.ap_tile` @ `0x5d0c0` builds a per-*instruction* AP tile: it assembles `NDTile` /
`TileIndex` from `access_ap_indices` + `free_indices` + `ap_addrs`, then calls `generate_subst_map`
followed by `substitute(...)` to bind symbolic loop indices into the AP, respecting `in_cur_scope`.
This is the concrete tile-level AP that instruction emission consumes.

`MemrefTileND.nordset_ap` @ `0x42510` defines a nested `nordset_ap_ctor()` (nested symbol)
that drives `nd_indices` through `generate_access_impl` with `NeuronNordsetAP` substituted for the
default `NeuronAP`. A "no-reorder set" AP forbids reordering of the set elements — used where DMA or
compute ordering across the set must be preserved, e.g. in-order reductions or ordered writes.
`DataTile.nordset_ap` builds the same `NeuronNordsetAP` over a materialised value tile.

`MemrefTileND.ndim_subtensor_access` carves a sub-region of the tile via a nested `tile_access_ctor()`
whose inner `check_stride()` guard enforces `"Access in dimension must be interval with 0 offset"`
and rejects non-contiguous strides for sub-tensor access (nested symbols + string).

`MemrefTileND._dynamic_index` builds a runtime-valued index entry, tying to `DynamicIndexTile` /
`DynamicScalar` (below). It enforces `"slice with variable size is not supported"` : only
the slice **offset** may be dynamic — the extent stays static.

---

## IndirectMemrefTileND — Gather / Scatter / RMW

### Purpose

`IndirectMemrefTileND` (subclass of `MemrefTileND`) models indexed access where one dimension is
addressed by a runtime **address tile** rather than an affine expression — i.e. gather (load),
scatter (store), or indirect read-modify-write.

### Construction and validation

```c
// IndirectMemrefTileND.__init__  @ 0x99800   (ordered args)
IndirectMemrefTileND(self, indices, generic_dims, generic_addrs):
    super().__init__(indices)             // MemrefTileND
    //   generic_dims  : the dimension(s) addressed indirectly ("generic")
    //   generic_addrs : the address tile(s) supplying per-element offsets for those dims
    combine_tiles(...)
    self.check_generic_indices()          // @ 0xba9a0

// IndirectMemrefTileND.check_generic_indices  @ 0xba9a0
function check_generic_indices(self):
    if indirect_dim is the partition dim:
        raise err_indirect_indices_free_dim     // indirect dim must be a FREE dim
    if address_tile not in required address space:
        raise err_indirect_indices_sbuf         // checks address-tile residency
    if count(self.generic_dims) > 1:
        raise "Multiple indirect dim access is not supported!"   // at most ONE
```

> **GOTCHA —** at most **one** indirect dimension per access. `last_generic_dim` simply reads
> `self.generic_dims` and returns the single entry — trivial precisely because the hard limit
> `"Multiple indirect dim access is not supported!"` (string) forbids more. A reimplementer
> who allows a two-axis gather will diverge from the hardware's addressing model.

### Lowering — offset-DMA vs indirect RMW

```c
// IndirectMemrefTileND.indirect_tensor_ref(self, ap_cls)  @ 0xb5a10
function indirect_tensor_ref(self, ap_cls):
    par_addr = generic_addrs                  // per-partition address tile
    ti = TileIndex(par_addr=par_addr, base=MemrefTileND.indices)
    require in_cur_scope(address tile)        // address tile must be live in this scope
    return ap_cls(ti, ...)                    // a NeuronIndirectAP-family node

// IndirectMemrefTileND.is_offset_dma(self, inst_cls)  @ 0xb8b50
function is_offset_dma(self, inst_cls):
    if generic_addrs.is_scalar():             // single base + runtime offset
        return OFFSET_DMA                      // cheaper: one base + runtime offset
    if inst_cls is the RMW form:
        use NeuronIndirectRMW                  // scatter-with-read-modify-write
    // a plain copy may NOT carry dynamic indirect indices:
    //   err_copy_dynamic_indirect_indices_not_natively_supported  
```

The `ap_cls` map for indirect access is pinned by name: plain gather/scatter →
`NeuronIndirectAP`; indirect RMW / scatter-add → `NeuronIndirectRMW`. The offset-DMA branch is the
cheap form (a single base plus a runtime offset, no per-element address vector); the RMW branch is
the read-modify-write scatter (e.g. indirect accumulate / scatter-add). A plain `copy` instruction
cannot carry dynamic indirect indices and must route through the DMA/RMW path
(`err_copy_dynamic_indirect_indices_not_natively_supported`).

---

## Dynamic-Index Tiles — Runtime-Valued Indices

When an index is a runtime value (a register / `DynamicScalar`) rather than a compile-time constant,
it is wrapped as a tile so it can flow through the tile algebra and be referenced by an indirect AP.

```c
// IndexValueTile.__init__(self, index)  @ 0x53960
//   wraps a single runtime index VALUE as a tile; sets self.index
//   .broadcast_to_target_tile → broadcast the scalar index across a tile shape

// DynamicIndexTile.__init__(self, index, offset)  @ 0x4eae0
//   a dimension index = (static base offset + runtime index):
//   self.index  : the dynamic index tile (runtime value)
//   self.offset : the static base offset
//   constraint: "slice with variable size is not supported" — only the OFFSET is dynamic

// DynamicIndexTile.make_in_cur_scope(cls, index, offset)  @ 0x66510  (classmethod)
//   MATERIALISES the dynamic index into the current scope: emits an InstTile holding an
//   IndexValueTile, typed nki_int_dtype, sized npartitions × partition_size, via
//   functools.partial(ctor) + in_cur_scope registration.  Turns a Python-level dynamic
//   index into a real scheduled IR value the indirect AP can reference.

// DataTile._build_dynamic_index(self, index)  @ 0x77fe0
//   bridge from a user dynamic index expression to the materialised index tile:
//   uses match_par_dim (align to partition dim), index_value_inst, and tensortensor `add`
//   to compute base+offset, then calls DynamicIndexTile.make_in_cur_scope.
```

All four offsets and the helper names (`match_par_dim`, `make_in_cur_scope`) resolve in the binary.
`make_in_cur_scope` is the step that converts a *Python-time* dynamic index into a *scheduled* IR
value, so that the indirect AP emitted later by `indirect_tensor_ref` has a concrete tile to point
its `par_addr` at.

---

## DataTile / InstTile — Materialised Value Tiles

These are the operand/result objects of compute instructions — the value side, distinct from the
address-view side above.

```c
// DataTile.__init__(self, dtype, predicates, tile, parent_scope)  @ 0x49390
//   self.dtype, self.predicates (predicate/mask list), self.tile, self.parent_scope
//   abstract for the IR handle:
//     "buffer not implemented for base data tile"   
//     "ir not implemented for base data tile"        

// InstTile.__init__(self, inst, tile, buffer, name, pending_allocation)  @ 0xaef10
//   subclass of DataTile — a tile that is the OUTPUT of an instruction (`inst`)
//   self.inst, self.tile, self.buffer, self.name, self.pending_allocation
//   (allocation deferred until scheduling)
//   guards: "Unexpected change of dtype in InstTile!", "Unexpected dst type!"  
```

The `DataTile` tile-algebra surface (all live symbols) is what makes it the operand object:
`_unary_op`, `_binop`, `_binop_scalar`, `_matmul` (`self @ other` — emits a matmul where `self` is
the stationary operand unless transposed), `_astype_impl` (dtype cast), `_broadcast_to_impl` /
`broadcast_to_target_tile` (guarded by `"Not a broadcast!"` and `"Can only broadcast when the rank
of src and dst shape match!"`), `expand_dims`, `copy`, and `mask_tile`. Its properties include
`shape_2d` (partition × flattened-free), `ndim`, `npartitions`, `partition_size`, and `nordset_ap`.

`InstTile` is the lvalue/rvalue the IRBuilder threads between ops: `generate_dst_ap_tile` builds the
destination AP tile for an instruction, and `add_missing_dst` / `update_free_shape` wire its output
into the dataflow. The two guards `"Unexpected change of dtype in InstTile!"` and `"Unexpected dst
type!"`  catch dtype/destination invariant violations when wiring instruction outputs.

---

## TensorRef / TensorView — Aliased and Affine Views

### TensorRef — the un-indexable alias

A `TensorRef` is a referenced tensor: an aliased handle to something you cannot index directly — a
kernel-function argument, or a non-local tensor.

```c
// TensorRef.__init__(self, tensor, access_shape, dtype)  @ 0x3c290
//   self.tensor, self.access_shape, self.dtype, self.shape
//   most operations are DELIBERATELY unimplemented (strings):
//     "indexing not supported on referenced tensor, use load to get tile."
//     "tile not implemented for base ref tile"
//     "nd_indices not implemented for base ref tile"
//     "indices_set not implemented for based ref tile"
```

> **GOTCHA —** a `TensorRef` must be `load()`'d into a real SBUF/PSUM tile before any compute. The
> `"indexing not supported on referenced tensor, use load to get tile."` guard is not a
> bug — it enforces that kernel arguments live in DRAM/HBM and are explicitly DMA'd on-chip before
> the tile algebra touches them.

### TensorView — the affine window, and the slice entry point

A `TensorView` is the richest view: an affine window over a base tensor that keeps the affine map
(offset + per-dim scale/stride) explicit.

```c
// TensorView.__init__(self, tensor, offsets, scales, view_shape, access_shape,
//                      def_scope, sliced_dims, dtype)  @ 0x54300
//   self.tensor (base), self.offsets (per-dim base offset), self.scales (per-dim stride),
//   self.view_shape, self.access_shape, self.def_scope, self.sliced_dims, self.dtype, self.tile

// TensorView.slice(cls, tensor, access_shape, dtype, indices, def_scope)  @ 0x8ba60  (classmethod)
function slice(cls, tensor, access_shape, dtype, indices, def_scope):
    indices = translate_indices_to_list(indices)         // [indexing.so]
    indices = [ normalize_slice(i) | colon_slice for ':' for i in indices ]
    recognise DynamicScalar / CExpr (dynamic affine index exprs)
    ti = TileIndex(indices)
    return build_memref_tile(tensor, access_shape, def_scope, dtype, ti)   // → concrete view
```

`TensorView.slice` is the canonical slicing entry: it normalises the index tuple
(`translate_indices_to_list`, `normalize_slice`, `colon_slice`), recognises dynamic/affine index
expressions (`DynamicScalar`, `CExpr`), builds a `TileIndex`, and calls `build_memref_tile`.
`append_implicit_slices_at_end` pads missing trailing dims with `:` (numpy-style implicit full
slices). These slice-canonicalisation helpers are owned by `indexing.so` and consumed here.

---

## The View Factory and the Bounds Check

### build_memref_tile — the 4-way view dispatch

```c
// build_memref_tile(tensor, access_shape, def_scope, dtype, indices)  @ 0x5abe0
function build_memref_tile(tensor, access_shape, def_scope, dtype, indices):
    scope = get_cur_scope()
    check_reference_def_in_scope(tensor, def_scope)      // base def must be live in def_scope —
                                                         //   prevents cross-scope dangling refs
    offset, name = compute_offset(indices), tensor.name  // offset_2 in the body

    // dispatch on the SHAPE of the index tuple (all four class names present):
    if indices contain an indirect / address tile:
        return IndirectMemrefTileND(indices, generic_dims, generic_addrs)
    elif indices contain a dynamic (runtime) scalar index:
        return DynamicIndexTile(...)
    elif plain affine / static ND indices:
        return MemrefTileND(indices)
    else:  // degenerate / already-a-tile
        return DataTile(...)
```

This is what `KernelTensor._index_tensor`, `KernelTensor.__setitem__`, and `TensorView.slice`
ultimately call to turn `tensor[...]` into a concrete view. All four target class names
(`IndirectMemrefTileND`, `DynamicIndexTile`, `MemrefTileND`, `DataTile`) are present as
`__pyx_n_s_*` module-global lookups in the wrapper, as are the scope helpers `get_cur_scope`,
`in_cur_scope`, `check_reference_def_in_scope`.

### Algorithm — check_buffer_overflow

After `create_tensor` builds an IR tensor, `check_buffer_overflow` bounds its **per-partition byte
footprint** against the hardware limits of the `target` (the Tonga/Sunda backend).

```c
// check_buffer_overflow(tensor, target)  @ 0x970b0
function check_buffer_overflow(tensor, target):
    if isinstance(tensor, NeuronSBTensor):
        if tensor.partition_size_in_bytes > target.statebuf_par_size_in_bytes:   // (1)
            print_error("SB tensor overflow: ...")                               // string-table literal
    elif isinstance(tensor, NeuronPSUMTensor):
        if tensor.partition_size_in_bytes > target.psum_par_size_in_bytes:       // (2)
            print_error("PSUM tensor overflow: ...")                             // string-table literal
        // PSUM additionally bounded by target.psum_num_banks (bank count)        // attr
```

The class branches (`NeuronSBTensor` / `NeuronPSUMTensor`, ×2 each), the size attrs
(`partition_size_in_bytes` ×7, `statebuf_par_size_in_bytes` ×6, `psum_par_size_in_bytes` ×5,
`psum_num_banks` ×1), and both overflow message strings are present in the decompiled wrapper and
the module string table.

> **NOTE —** the *bound* is the solid part of this check: per-partition bytes must fit one SBUF
> partition (`statebuf`) or one PSUM bank's partition size, and the size attributes plus the
> `"... tensor overflow:"` strings are all present in the binary. The comparison *operator* is not
> literally readable — Cython compiles richcompare into a helper call — so the `>` direction shown
> above is [INFERRED] from the message semantics.

`extract_buffer_shape(shape)` @ `0x59550` is the partner shape probe: it normalises a user shape into
a buffer shape, recognising `ParDim` (the partition-dimension marker attr) entries via
`nki_assert`, so `create_tensor` can separate the partition dim from the free dims and size the
per-partition footprint correctly.

---

## End-to-End: from `tensor[...]` to a Penguin AP

The call edges, assembled:

```text
KernelTensor[idx]  /  TensorView.slice                          [this page]
  ├─ normalize_slice / translate_indices_to_list / TileIndex    [indexing.so]
  └─ build_memref_tile(tensor, access_shape, def_scope, dtype, indices)   @ 0x5abe0
       └─ dispatch on index shape →
            MemrefTileND | IndirectMemrefTileND | DynamicIndexTile | DataTile
                 └─ .ap_tile / .generate_access
                      └─ generate_access_impl(addrs, ap_cls)    @ 0xa8550
                           ├─ AffineExpr index exprs over Axis  [5.4]
                           ├─ keep_ap_indicies_linear_expr / drop_ap_indicies
                           └─ ap_cls(...) →
                                NeuronAP | NeuronNordsetAP | NeuronIndirectAP | NeuronIndirectRMW
                                     └─ consumed by InstTile / IRBuilder for instruction emission

Allocation safety (parallel edge):
  KernelTensor.create_tensor  @ 0xa3a00  →  Neuron{SB,PSUM,Local}Tensor   [5.2]
       └─ check_buffer_overflow(ir_tensor, target)  @ 0x970b0  → SBUF/PSUM per-partition bound
```

The down-map, restated as a table (all names read from `__pyx_n_s_*` lookups + the module string
table):

| Python view / tensor | Penguin IR node | Built by |
|---|---|---|
| `KernelTensor(sbuf)` | `NeuronSBTensor` | `create_tensor` line 1402 |
| `KernelTensor(psum)` | `NeuronPSUMTensor` | `create_tensor` line 1553 |
| `KernelTensor(internal/local)` | `NeuronLocalTensor` | `create_tensor` line 965 |
| `MemrefTile(ND)` default | `NeuronAP` | `generate_access_impl(ap_cls=NeuronAP)` |
| `MemrefTileND.nordset_ap` / `DataTile.nordset_ap` | `NeuronNordsetAP` | nested `nordset_ap_ctor` |
| `IndirectMemrefTileND.indirect_tensor_ref(ap_cls)` | `NeuronIndirectAP` | `indirect_tensor_ref` @ `0xb5a10` |
| `IndirectMemrefTileND.is_offset_dma` (RMW form) | `NeuronIndirectRMW` | `is_offset_dma` @ `0xb8b50` |

AP indices are `AffineExpr` over `Axis`; the lowered Tonga handle is `TongaTensor` + `APIndex`. The
backend target is `Optional[Union[Tonga, Sunda]]`, selecting the ISA (`TongaISAInst` /
`SundaISAInst`).

---

## Evidence Anchors

- **`create_tensor` dispatches Local→SB→PSUM at lines 965/1402/1553.** The decompiled wrapper shows
  `__pyx_n_s_NeuronLocalTensor` (965/969), `NeuronSBTensor` (1402/1406), and `NeuronPSUMTensor`
  (1553/1557) in that order.
- **The Penguin tensor names match [5.2].** `tensor-buffer-node.md` carries `NeuronLocalTensor` (×16),
  `NeuronPSUMTensor` (×11), and `NeuronSBTensor` (×10) — identical spellings.
- **Both overflow strings and the four size constants exist.** `"SB tensor overflow: "` and
  `"PSUM tensor overflow: "` sit in the string-init table; `partition_size_in_bytes`,
  `statebuf_par_size_in_bytes`, `psum_par_size_in_bytes`, and `psum_num_banks` are all present in the
  `check_buffer_overflow` wrapper. (The comparison operator itself is not readable — see the NOTE in §5.)
- **`build_memref_tile` references all four view classes plus the scope helpers.**
  `__pyx_n_s_{MemrefTileND, IndirectMemrefTileND, DynamicIndexTile, DataTile}` and
  `__pyx_n_s_{get_cur_scope, in_cur_scope, check_reference_def_in_scope}` all resolve.
- **The four `ap_cls` AP-node names and the AP-canonicalisation helpers exist.** `NeuronAP`,
  `NeuronNordsetAP`, `NeuronIndirectAP`, `NeuronIndirectRMW`, plus `keep_ap_indicies_linear_expr`,
  `drop_ap_indicies`, `generate_subst_map`, `substitute`, `cached_combine_index_tiles`,
  `match_par_dim`, and the indirect error helpers `err_indirect_indices_sbuf` /
  `err_indirect_indices_free_dim` are all in the string table.

Two things this page does **not** reconstruct: the precise affine arithmetic inside
`generate_access_impl` and `ap_tile`, which Cython inlines (only inputs, helpers, and outputs are
recovered); and the byte-level class layout behind the inheritance edges, which are read from
override patterns, `super().__init__` chains, and abstract-base diagnostics instead.

---

## Related Components

| Name | Relationship |
|---|---|
| `tensor.so` abstract base | The `NotImplementedError` protocol that `tensors.so` concretises |
| `indexing.so` | Owns `TileIndex`, `NDTile`, `APIndex`, slice canonicalisation, `AffineExpr` index inference |
| `starfish.penguin.ir` | The down-map target — `Neuron{SB,PSUM,Local}Tensor`, `Neuron…AP` nodes |
| IRBuilder | Consumes `InstTile` / generated APs to emit instructions |

## Cross-References

- [Tile / Tensor Model (Abstract)](tile-tensor-model.md) — the abstract base whose hooks land here
- [AffineExpr Algebra](../penguin/affine-expr-algebra.md) — the index-expression algebra the AP emitter builds over `Axis`
- [Tensor / Buffer Node](../penguin/tensor-buffer-node.md) — `Neuron{SB,PSUM,Local}Tensor`, the create_tensor targets (same classes)
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the partition geometry that `check_buffer_overflow` bounds against
