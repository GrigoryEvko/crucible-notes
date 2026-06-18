# NeuronCodegen Tiling Internals & the Offloaded-FMA Path

> *All symbols, addresses, and source-line spans on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). They were read from the **unstripped 14.5 MB** `neuronxcc/nki/compiler/backends/neuron/KernelBuilder.cpython-310-x86_64-linux-gnu.so` (14,588,400 B; BuildID `9eb1020e…`; GNU C17 `-O3 -fwrapv -fPIC -g`; Cython `.py → .c → .so`). This is the **derived `NeuronCodegen`** class, not the 820 KB `neuronxcc/generated/…/KernelBuilder.so` base that the IDA sidecar covers — those are two different binaries. All VAs are `.text`/`.rodata` (VA == file-offset, verified). The BIR-side twins live in `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310…so` (11.1 MB). Other wheels differ; treat every address as version-pinned.*

## Abstract

A NKI matmul is almost never one PE pass. The contraction (`K`) axis routinely exceeds the 128-partition SBUF height and the PSUM bank width, so the compiler splits a logical GEMM into a grid of sub-tiles, runs each as its own `nc_matmul`, and **accumulates the partial products in PSUM** until the full `K` extent has been summed. This page documents the machinery in `NeuronCodegen` that builds that grid: the four `combine_*` entry points that merge tile operands, the geometry primitive that sizes the SBUF and PSUM halves of a tile, the two DMA-engine-offloaded compute emitters (`TiledOffloadedFMA` / `TiledOffloadedMemCpy`), the per-instruction `process_new_neuroninst` hook every emitter funnels through, and the `finalize_kernel` step that closes the Penguin Function.

The central object is the **combine family**, and its single most important and counter-intuitive fact is a layering split that an earlier report got wrong: `combine_tiles` is a `NeuronCodegen` **method** (mdef `#295`, `@0x16ad90`) — the generic operand normalizer — but the three matmul variants `combine_matmult_tiles`, `combine_sparse_matmult_tiles`, and `combine_trn2_double_row_matmult_tiles` are **module-level free functions** (mdef ordinals `_5`/`_9`/`_7`, no `NeuronCodegen` infix in the symbol). The matmul variants share one byte-identical *K-accumulate skeleton* — `PyObject_Size → PySlice_New → PyNumber_Add → FastCall` — that computes, for each `K` sub-tile, *where in the combined PSUM output it lands* and folds it into the running accumulation. The sparse variant reads more sub-tile dims (16 `PyObject_Size` calls vs. 6); the TRN2 double-row variant adds four `cmp $0x2` guards that hard-enforce a pack factor of 2 and restricts the dtype to fp8/uint8.

The reference frame is a weight-stationary systolic array with a fp32 accumulator bank (PSUM). If you know how a TPU MXU sums `K`-tiles into an accumulator and how the `START/ACCU/STOP` accumulate states gate that sum, you own the endpoints; the Neuron-specific middle is the tile-descriptor bookkeeping — canonical par/free index folding, the size-2 double-row pack contract, and the offloaded compute family that hands FMA/memcpy to a DMA engine instead of the PE.

| | |
|---|---|
| **Subject binary** | `KernelBuilder.cpython-310…so` (14.5 MB, unstripped, full `.debug_info`) |
| **Class** | `GeneratedNeuronCodegen` (compiled); `NeuronCodegen` is the logical name |
| **Generic combine** | `combine_tiles` — method `#295`, `@0x16ad90`, `Iterable[NDTile] → 1 canonical tile` |
| **K-accumulate combines** | `combine_matmult_tiles` `@0x120c70`, `combine_sparse_matmult_tiles` `@0x12b670`, `combine_trn2_double_row_matmult_tiles` `@0xcbc90` (all **module-level**) |
| **Geometry primitive** | `get_sb_and_psum_shape` `#93`, `@0x6ec10` → `(sb_shape, psum_shape)` |
| **Offloaded compute** | `tiled_offloaded_fma_inst` `#163` `@0x185990` → `TiledOffloadedFMA`; `tiled_offloaded_memcpy_inst` `#157` `@0x13e3c0` → `TiledOffloadedMemCpy` |
| **Per-inst hook** | `process_new_neuroninst` `#301`, `@0x236520` |
| **Finalizers** | `finalize_kernel` `#379` `@0xf9c00` (LAST method); `finalize` `#3` `@0xacba0` (super-chain) |
| **BIR lower side** | `BirCodeGenLoop.cayman_matmul_double_row_ap` / `addDoubleRowAP`; `codegenTiledOffloaded{FMA,MemCpy}` |

The reimplementation contract: given a set of `K`-split matmul sub-tiles, the combine machinery must (1) compute each sub-tile's PSUM slice and accumulation offset, (2) merge them into one tile descriptor carrying an `EngineAccumulationType`, (3) for double-row, enforce a size-2 pack axis and fp8/uint8 dtype, and (4) hand the assembled instruction to `process_new_neuroninst → insert` and ultimately to `finalize_kernel`, which validates the kernel name and stamps the Function's outputs.

---

## 1. Method Roster & Source-Line Spans

Every method below is confirmed in the 14.5 MB `.so` by `nm -C`. The Cython mdef ordinal (`__pyx_mdef_…_NNN<name>`) places each in source order; the DWARF `decodedline` table pins each to a `KernelBuilder.py` span.

| mdef | symbol | `pw` VA | `KernelBuilder.py` span | role |
|---|---|---|---|---|
| `#3` | `…NeuronCodegen_3finalize` | `0xacba0` | super-chain | base teardown (delegates up) |
| `#93` | `…NeuronCodegen_93get_sb_and_psum_shape` | `0x6ec10` | L700–L711 | SBUF+PSUM tile geometry |
| `#157` | `…NeuronCodegen_157tiled_offloaded_memcpy_inst` | `0x13e3c0` | L1758–L1779 | DMA-offloaded copy emit |
| `#163` | `…NeuronCodegen_163tiled_offloaded_fma_inst` | `0x185990` | L1928–L1951 | DMA-offloaded FMA emit |
| `#295` | `…NeuronCodegen_295combine_tiles` | `0x16ad90` | L4652–L4700 | operand normalizer |
| `#297` | `…NeuronCodegen_297insert` | `0x165f00` | — | Penguin-Function append + dep-edge wiring |
| `#301` | `…NeuronCodegen_301process_new_neuroninst` | `0x236520` | L4729–L4750 | per-new-inst hook |
| `#379` | `…NeuronCodegen_379finalize_kernel` | `0xf9c00` | L5909–L5924 | assemble final Function (LAST method) |

The three matmul combines carry **no `NeuronCodegen` infix** — their mdef symbols are `__pyx_mdef_…_KernelBuilder_5combine_matmult_tiles`, `_7combine_trn2_double_row_matmult_tiles`, `_9combine_sparse_matmult_tiles` (`@0x2d0ca0`/`0x2d0c80`/`0x2d0c60` in `.data`). Cython emits the bare module name for free functions and `…NeuronCodegen_NNN…` for bound methods; the absence of the infix is the binary proof that these three are module-private helpers, not class methods.

> **CORRECTION — combines are functions, not methods.** An earlier pass (D-P06) listed `combine_trn2_double_row_matmult_tiles` as "a tile-combine helper" without noting its layer. The binary is unambiguous: `combine_tiles` (`#295`) is the only *method*; `combine_matmult_tiles` / `combine_sparse_matmult_tiles` / `combine_trn2_double_row_matmult_tiles` are **module-level free functions** taking exactly two tiles each (the running accumulation and the new sub-tile). The matmul emitters call them as plain functions, not `self.`-dispatched methods.

The method-number adjacency `295 / 297 / 301` (`combine_tiles` / `insert` / `process_new_neuroninst`) is not incidental — these three sit side-by-side in source and form the **emit → normalize → insert → process** spine that every per-instruction emitter funnels through. Confirmed by mdef ordinals + DWARF spans.

---

## 2. The Combine Family — How Tiled Sub-Computations Merge

Four combine entry points, two tiers. The generic tier (`combine_tiles`, variadic) folds *heterogeneous operand tiles* into one canonical description; the matmul tier (three 2-arg functions) folds *`K`-split matmul sub-tiles* into one PSUM-accumulated output.

### 2.1 `combine_tiles` (#295) — the operand normalizer

`combine_tiles` is variadic (`*tiles`; the arg-count guard string is `"… takes at least …"`) and annotated `Iterable[NDTile]` (rodata `__pyx_kp_s_Iterable_NDTile`, one hit). It takes a sequence of tile operands an emitter was handed and folds them to a single canonical tile description. The body (`0x16ad90`) is a three-closure nest, all confirmed by symbol:

```c
// combine_tiles(self, *tiles) -> NDTile   (method #295, @0x16ad90)
// nested symbols: __pyx_gb_…combine_tiles_5generator43  (the genexpr)
//                 __pyx_pw_…combine_tiles_2lambda27     (the lambda)
//                 __pyx_pw_…combine_tiles_1update_tile  (closure, @0x238830)
PyObject *combine_tiles(self, PyObject *tiles_varargs) {
    list = PySequence_List(tiles_varargs);          // materialize *tiles
    // update_tile closes over free vars {self, tile, broadcast_num_to_memset}
    //   (rodata: "free variable 'broadcast_num_to_memset' referenced before
    //    assignment in enclosing scope"; same wording for 'self','tile')
    for (each operand tile via generator43) {
        kwargs = {};
        PyDict_SetItem(kwargs, k1, v1);             // 4x SetItem: rebuild each
        PyDict_SetItem(kwargs, k2, v2);             //   operand's tile descriptor
        PyDict_SetItem(kwargs, k3, v3);             //   with merged kwargs
        PyDict_SetItem(kwargs, k4, v4);
        if (is_broadcast)                            // broadcast_num_to_memset
            tile = build_memset_broadcast_tile(...); //   -> a memset broadcast tile
        merged = update_tile(self, tile, kwargs);
        acc = PyNumber_Add(acc, merged_shape);      // fold shapes (2x Add)
        PyList_Append(out, merged);
        PyObject_RichCompare(dim_a, dim_b, Py_EQ);  // per-dim compatibility test
    }
    return canonicalize(out);  // canonical par/free indices
}
```

The `broadcast_num_to_memset` free variable (6 hits in rodata) is the key behavioral detail: when an operand is a broadcast, `combine_tiles` rewrites it as a **memset broadcast tile** rather than a real data tile. The `PyObject_RichCompare` is the per-dimension equality/compatibility check across the tiles being merged — it is how the normalizer rejects shape-incompatible operands.

> **NOTE — fold semantics are STRONG, not byte-traced.** The closure nest (`generator43` + `lambda27` + `update_tile`), the 4× `PyDict_SetItem` kwarg rebuild, and the broadcast→memset handling are byte-confirmed. Whether the per-dim fold is *concat* or *accumulate* is inferred from the `Size/Add/Append/RichCompare` mix; it was not resolved to literal comparands because the attribute names route through Cython's `__pyx_mstate_global_static` struct (one symbol `@0x2d6ce0`), which defeats direct `lea`-annotation of attr operands.

### 2.2 `combine_matmult_tiles` — the K-tile PSUM accumulate

This is the heart of tiled-matmul codegen. Module-level, exactly 2 args (rodata `"… takes exactly 2 positional argument…"`). The body (`0x120c70`) is the **K-accumulate skeleton** — objdump-confirmed call counts: 6× `PyObject_Size`, 2× `PySlice_New`, 2× `PyNumber_Add`, 1× `PyTuple_New`, 8× `PyObject_FastCall`:

```c
// combine_matmult_tiles(accumulated_tile, new_k_tile)   (module fn _5, @0x120c70)
PyObject *combine_matmult_tiles(PyObject *acc, PyObject *new_k_tile) {
    dim    = PyObject_Size(new_k_tile);             // read a tile dim
    slice  = PySlice_New(start, start + dim, NULL); // WHERE this K-sub-tile lands
                                                    //   in the combined PSUM output
    // "'%.200s' object is unsliceable" guards the slice
    start  = PyNumber_Add(start, dim);              // running offset = base + size
    FastCallDict(place_into_combined, slice, ...);  // accumulate into combined tile
    // Size -> Slice -> Add -> Call cycle REPEATS (6 Size, 2 Slice, 2 Add total)
    return PyTuple_New(merged_result);              // assemble merged tile
}
```

**Semantics.** When a GEMM is split across the contraction axis into multiple `nc_matmul` sub-tiles, every sub-tile writes the *same* PSUM region and the partial products are PSUM-accumulated into one logical output. `combine_matmult_tiles(accumulated_tile, new_k_tile)` computes the slice (where in the output) and the offset (running accumulation position) and merges `new_k_tile` into the running sum. The combine carries an `Optional[EngineAccumulationType]` (rodata, 1 hit) — the PSUM accumulate-state enum `{START, ACCU, STOP}` that gates whether the matmul *initializes*, *adds to*, or *closes* the accumulator bank. Cross-ref `BirCodeGenLoop.codegenMatMulOp` `set_perf_mode` / PSUM bind on the lower side.

> **GOTCHA — the result buffer must be PSUM.** The module carries the contract `"Result buffer of matmult must be psum!"` (rodata). PSUM is the only fp32 accumulator; an `nc_matmul` whose output is bound to SBUF is rejected before this combine runs. The K-tile accumulate *is* the reason: you cannot sum partials in a buffer that is not the accumulator bank.

### 2.3 `combine_sparse_matmult_tiles` — the structured-sparsity variant

Module-level, exactly 2 args. Same `Size → Slice → Add` accumulate skeleton as §2.2, but with **16 `PyObject_Size` calls** (objdump-counted, `0x12b670`) vs. 6 in the dense matmul combine — the extra reads cover the structured-sparsity dims: the 2-D sparse tile plus the int16 tag/index tensor. The driving op `matmult_sparse` (`#103`) is **2-D-tile-only**: the rodata contracts `"matmult_sparse tile_position must be a 2D tuple"`, `"matmult_sparse tile_size must be a 2D tuple"`, and `"matmult_sparse cannot work with column tiling"` (all confirmed) forbid a column-tiling dimension. So the sparse combine merges 2-D sparse sub-tiles with the sparsity metadata preserved across the merge — no third (column) tiling axis exists for it to fold.

### 2.4 `combine_trn2_double_row_matmult_tiles` — the TRN2 double-row pack

Module-level, exactly 2 args. The body (`0xcbc90`) is the §2.2 accumulate skeleton (6× `PyNumber_Add` confirmed) **plus four `cmp $0x2,…` guards** (objdump-counted: 4 `cmp …0x2,` in the range) that enforce a **pack factor of 2** — two output rows packed per PE pass. This is the TRN2 (Trainium-2 / gen2–3) double-row PE mode: the 2× column packing that the `DoubleRow` perf-mode exploits.

The pack contracts are present **verbatim** in this `.so` (all confirmed by string search):

```text
"first F dim of LHS and RHS of the double_row matmult must be 2"
"The last dimension of double_row matmult must have size of 2"
"The first dimension of Cayman double_row matmult must have size of 2"
"The double_row matmult only support fp8e4m3 and fp8e5m2"
"The double_row matmult only support uint8"
```

So the double-row combine packs **two matmul rows at once** into one PSUM pass, with the pack axis hard-required to be size 2 (the four `cmp $0x2` guards) and the dtype restricted to **fp8e4m3 / fp8e5m2 / uint8**. The per-row PSUM merge is otherwise the same accumulate skeleton as the dense case.

```c
// combine_trn2_double_row_matmult_tiles(acc, new_row_pair)  (module fn _7, @0xcbc90)
PyObject *combine_trn2_double_row(PyObject *acc, PyObject *new_pair) {
    if (first_F_dim(lhs) != 2 || first_F_dim(rhs) != 2)        // cmp $0x2
        raise("first F dim of LHS and RHS … must be 2");
    if (last_dim(new_pair) != 2)  raise("last dimension … must be 2");   // cmp $0x2
    if (first_dim_cayman != 2)    raise("first dimension of Cayman … 2");// cmp $0x2
    if (!is_fp8(dt) && dt != uint8) raise("only support fp8e4m3/e5m2|uint8");
    // ... then the same K-accumulate skeleton (Size->Slice->Add->FastCall),
    //     merging the 2 packed rows into the combined PSUM tile.
    // LOWER: BirCodeGenLoop.cayman_matmul_double_row_ap / addDoubleRowAP
}
```

> **NOTE — gen3 double-row is rejected here.** The module also carries `"perf_mode=\`double_row_gen3\` is not supported on …"` (rodata, confirmed). The double-row path is gated by core generation; `double_row` (gen2/Cayman) is packed by this combine, while `double_row_gen3` is refused at this version of the builder.

The lower side is `BirCodeGenLoop.cayman_matmul_double_row_ap` / `addDoubleRowAP` — both confirmed present in `BirCodeGenLoop.so` (`cayman_matmul_double_row_ap` 12 hits, `addDoubleRowAP` 15 hits). This grounds D-P06 §6's pointer: `packed_cayman_pe_tp_kernel → CaymanPackedPETranspose` is the macro emit, and this module-level combine is the helper that merges the two packed rows into the combined PSUM tile before that lower. D-P06's pointer was correct; this is the body behind it.

> **Cross-ref.** The PE-side double-row mechanism — de-interleave + doubled `K`-extent — is documented in [PE engine](../arch/pe-engine.md); the Penguin tiling pipeline that produces these sub-tiles is [layout-tiling-pipeline](../penguin/layout-tiling-pipeline.md); the sparse tiling is [§6.8.8](../nki/sparse-tiling.md).

---

## 3. `get_sb_and_psum_shape` (#93) — Tile Geometry

A small helper (`KernelBuilder.py` L700–L711) that sits **just before** the matmult family (`#101`–`#107`) in source order — a geometry primitive the matmul emitters consume. The body (`0x6ec10`) builds and returns the SBUF/PSUM shape pair: **2× `PyList_New` + 1× `PyTuple_New`** (objdump-confirmed), i.e. two shape *lists* wrapped into a *tuple*:

```c
// get_sb_and_psum_shape(self, ..., **kwargs) -> (sb_shape, psum_shape)   (#93, @0x6ec10)
PyObject *get_sb_and_psum_shape(self, ...) {
    __Pyx_ParseOptionalKeywords("get_sb_and_psum_shape", ...);  // has optional kwargs
    sb   = PyList_New(n);            // per-dim SBUF shape  (builders: call *%rax)
    // RichCompare + IsTrue -> a kwarg/flag branch (psum requested? arch/transpose?)
    if (PyObject_IsTrue(PyObject_RichCompare(flag_a, flag_b, op))) { ... }
    memcmp(name_attr, expected, len);   // GetAttr cascade compares a geometry name
    psum = PyList_New(m);            // per-dim PSUM shape
    return PyTuple_New2(sb, psum);   // RETURN (sb_shape, psum_shape)
}
```

The geometry vocabulary it reads/returns is in rodata, all confirmed (hit counts in parentheses):

| side | attributes |
|---|---|
| **SBUF** | `sb_shape` (5), `sb_allocated` (4), `statebuf_num_partitions` (4), `statebuf_usable_par_size_in_bytes` (1), `partition_size` (9) |
| **PSUM** | `psum_shape` (17), `psum_allocated` (4), `psum_num_banks` (4), `psum_num_partitions` (4), `psum_par_size_in_bytes` (4) |

So `get_sb_and_psum_shape` computes, for a tile, the (partition × free) SBUF tile shape **and** the matching PSUM tile shape, checking PSUM-bank fit via `psum_num_banks` / `psum_par_size_in_bytes` and the 128-partition statebuf geometry via `statebuf_num_partitions`. It is the forward-builder echo of the upstream `SBSizeLegalization` geometry (128-partition SBModel, partition-dim maxbytes, free-dim roundup); it **presupposes** legal tiles and reports their two-buffer shapes — it does **not** run legalization itself (legalization is upstream, not a call inside the tiling internals).

> **INFERRED — the RichCompare branch comparand.** The `PyObject_RichCompare → PyObject_IsTrue` conditional is a kwarg/flag branch (likely "is PSUM requested" or an arch/transpose flag), but the comparand routes through `mstate` and was not resolved to a literal value. The return *shape* (two lists in a tuple) and the geometry attribute roster are CONFIRMED.

---

## 4. The Offloaded Family — DMA-Engine FMA / MemCpy

Two emitters, each producing a named macro-op, each with a BIR codegen twin. "Offloaded" means the op is handed to a **DMA/Pool engine** — *off* the PE/Tensor pipeline — rather than the Activation/PE datapath.

| `NeuronCodegen` emit (KB.so) | macro-op name (rodata) | BIR twin (BIR.so) |
|---|---|---|
| `tiled_offloaded_fma_inst` (`#163`, `@0x185990`) | `TiledOffloadedFMA` (4 hits) | `codegenTiledOffloadedFMA` (12) |
| `tiled_offloaded_memcpy_inst` (`#157`, `@0x13e3c0`) | `TiledOffloadedMemCpy` (4 hits) | `codegenTiledOffloadedMemCpy` (12) |

The BIR side is **tiled/untiled-paired**: alongside the `Tiled*` twins it carries `codegenOffloadedFMA` (12), `codegenOffloadedMemCpy` (12), and an extra cast variant `codegenOffloadedMemCast` (12), plus the shape helper `extract_offloaded_memcpy_shape_bases` (1) — all confirmed in `BirCodeGenLoop.so`. `Tiled*` operates on operands already SBUF-tiled; plain `Offloaded*` operates whole-tensor. This is the same tiled/untiled split the macro kernels use, here applied to the offloaded compute primitives.

```c
// tiled_offloaded_fma_inst(self, *args)   (#163, @0x185990)
// VARIADIC: rodata "… at least …" + "… at most …"
// tuple-unpack: "need more than %zd value… to unpack" / "too many values to unpack"
NeuronInst *tiled_offloaded_fma_inst(self, PyObject *bundle) {
    (src0, src1, dst, shape_bases) = unpack(bundle);   // packed operand bundle
    inst = build_inst("TiledOffloadedFMA", src0, src1, dst);  // emitted-name literal
    return process_new_neuroninst(self, inst);         // -> normalize -> insert
}
```

Both emitters are variadic (`"at least"` + `"at most"` guards) and do **tuple unpacking** (`"too many values to unpack"` / `"need more than"`, both confirmed module-wide) — they receive a packed operand bundle (src/dst tiles + a shape-base list) and unpack it. `TiledOffloadedFMA` is a fused multiply-add executed via the offload engine on already-tiled SBUF operands; `TiledOffloadedMemCpy` is a tile-to-tile copy via the DMA engine. They are the **compute-on-DMA** counterpart to the pure `DMACopyOp` / `SBAtomLoad`/`Store` memory emitters.

> **STRONG — DMA-vs-PE routing.** The variadic + operand-unpack + the two macro names are CONFIRMED in both `.so` files. The "offloaded = handed to a DMA/Pool engine, not the PE" reading is STRONG: it rests on the `Offloaded` naming and the BIR `MemCast`/`MemCpy` twin set (a cast variant only makes sense on a copy engine), not on a byte-traced engine-selection branch.

---

## 5. `process_new_neuroninst` (#301) — Per-Instruction Hook

The hook every per-instruction emitter (the ~150 `nl`/`nisa` emitters) reaches after building its Penguin `Inst`. Method `#301`, L4729–L4750, immediately after `insert` (`#297`) and `combine_tiles` (`#295`). Body `0x236520`:

```c
// process_new_neuroninst(self, inst)   (#301, @0x236520)
PyObject *process_new_neuroninst(self, PyObject *inst) {
    a = GetAttrStr(inst, ...);  PyObject_Call(method, ...);   // read inst attrs
    PyDict_SetItem(bag1, ...); PyDict_SetItem(bag1, ...);     // ~3 dicts x 3 entries:
    PyDict_SetItem(bag2, ...); ...                            //   config/attribute bags
    if (RichCompare(inst.kind, K1)) ...                       // 3x RichCompare:
    else if (RichCompare(inst.kind, K2)) ...                  //   dispatch on inst KIND
    else if (RichCompare(inst.kind, K3)) ...
    process_dep_edges(self, inst);          // interned call-target name (4 hits)
    __Pyx_PyObject_Call(downstream_handler, ...);  // forward to right handler
    // -> combine_tiles (#295) normalizes operands -> insert (#297) appends
}
```

**Role.** For each freshly-constructed `NeuronInst`: read its kind (3× `RichCompare`), build the config bag that travels with the inst (≈3 dicts of 3 entries), wire dependency edges (`process_dep_edges`, an interned call target, 4 hits), normalize its operand tiles via `combine_tiles` (`#295`), and accumulate it into the current tile/kernel state before `insert` (`#297`) appends it to the Penguin Function. The `295 / 297 / 301` adjacency plus the `GetAttr/Call/Dict/RichCompare` mix is exactly *"for each new inst: read its kind → build its config bag → dispatch → record."*

`insert` (`#297`, `@0x165f00`) is the shared plumbing: variadic (`"at least"`/`"at most"`), `RichCompare`-dispatched — the Penguin-Function append + dep-edge wiring that "every emitter ends in." `process_new_neuroninst` is the per-inst stage that runs the normalize-then-insert sequence around it.

> **STRONG/INFERRED — per-kind dispatch targets.** The method position and body mechanics are CONFIRMED. The precise per-kind dispatch destinations route through the `mstate` interned-name struct (Cython attr names are not direct-`lea`'d), so the exact branch targets are STRONG/INFERRED from the rodata name roster + line spans + the BIR twins, not byte-traced.

---

## 6. Finalizers — `finalize_kernel` (#379) & `finalize` (#3)

Two finalizers at opposite ends of the method table.

**`finalize_kernel` (#379)** — the LAST method (highest mdef ordinal), L5909–L5924, body `0xf9c00`. Self-only (`"takes exactly 1"`). It builds tuples + kwarg dicts (`PyTuple_New` + `PyDict_SetItem`), FastCall-dispatches, branches on `RichCompare`, and runs a `GetAttr` cascade. The finalization vocabulary (rodata, confirmed): `function`, `kernel_name` (8), `outputs`, `debug_kernel_function_name` (5). The kernel-name contracts `"Kernel name mismatch"` (1) and `"Unsupported kernel name "` (1) are present in this module.

`finalize_kernel` **assembles** the per-tile/per-inst ops accumulated during emission into the final Penguin kernel Function: it validates the kernel name, sets the Function's `outputs` / return tensors, and stamps `debug_kernel_function_name`.

```c
// finalize_kernel(self) -> Function   (#379, @0xf9c00, LAST method)
PyObject *finalize_kernel(self) {
    // validate kernel name (INFERRED): "Kernel name mismatch" / "Unsupported kernel name"
    if (self.kernel_name != expected) raise("Kernel name mismatch");
    fn = build_function();
    fn.outputs = collect_return_tensors(self);          // set outputs/return tensors
    setattr(fn, "debug_kernel_function_name", self.dbg_name);
    return fn;
}
```

> **ATTRIBUTION CAVEAT.** The self-only signature, the tuple/dict build + FastCall dispatch + `RichCompare` branches, and the `function`/`kernel_name`/`outputs`/`debug_kernel_function_name` vocabulary are CONFIRMED in `#379`. But `"Kernel name mismatch"` / `"Unsupported kernel name "` are stored as `mstate`/cached-value PyObjects — **not** direct-`lea`'d in the `#379` body — so they are finalization-band kernel-name contracts that were **not** byte-traced to `#379` specifically. The name-validate step is **INFERRED** from the `kernel_name` vocab + these contracts; the outputs/return-marshalling is STRONG.

**`finalize` (#3)** — a small base-chain finalizer, body `0xacba0`. It calls `super().<finalize>`: the rodata `"super(): empty __class__ cell"` is present and the `super` reference is in the body window. `NeuronCodegen.finalize` delegates up to its base class's `finalize` (the `GeneratedNeuronCodegen` / `IRBuilder` base in the 820 KB generated `.so`) — a lightweight teardown / chain hook. CONFIRMED super-call.

**Emit-to-finalize order** (INFERRED end-to-end from method roles):

```text
  emit (per-family macro / per-inst)
    -> process_new_neuroninst (#301)   [per inst: read kind, config bag, dep edges]
       -> combine_tiles (#295)         [normalize operands -> canonical par/free]
       -> insert (#297)                [append to Penguin Function]
    -> ... (repeat per inst) ...
  -> finalize_kernel (#379)            [assemble Function; validate name; set outputs]
  -> finalize (#3)                     [super()-chain base teardown]
```

---

## 7. Tile-Type Operand Vocabulary

The operand classes that `combine_tiles` and the matmul combines operate over, all confirmed in rodata: `DataTile`, `InstTile`, `NDTile`, `MemrefTile`, `MemrefTileND`, `IndirectMemrefTileND`, `NeuronPSUMTensor`. The annotation strings that bound the combine inputs/outputs:

| annotation | where |
|---|---|
| `Iterable[NDTile]` | `combine_tiles` input |
| `List[DataTile]`, `Optional[DataTile]` | tile operands |
| `Union[DataTile, Any]`, `Union[DataTile, float]`, `Union[DataTile, nki_mask]` | scalar/mask operands |
| `List[NDimSubTensorAccess]`, `List[Union[int, TileIndex]]` | sub-tensor access plumbing |
| `Optional[EngineAccumulationType]` | **the PSUM-accumulate engine type carried on matmul combines** |
| `Optional[TileMask]` | mask operand |

The operand type-guard string is `"value must be a DataTile"` (1 hit). The `canonical_par_indices` / `canonical_free_indices` AP plumbing on `InstTile` is what `combine_tiles` produces; the `MemrefTile` / `NDTile` distinction is the tiled-vs-untiled IO classification the offloaded family also keys on.

---

## 8. Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

1. **"The three matmul combines are module-level functions, not methods."** Re-checked `nm -C`: their mdef symbols are `…_KernelBuilder_5combine_matmult_tiles` / `_7combine_trn2_double_row_matmult_tiles` / `_9combine_sparse_matmult_tiles` — **no** `13NeuronCodegen` infix. `combine_tiles` is `…_13NeuronCodegen_295combine_tiles` — **with** the infix. **CONFIRMED.** The D-P06 correction stands.

2. **"The K-accumulate skeleton is `Size → Slice → Add → FastCall`, and sparse reads more dims than dense."** Re-counted via `objdump | rg`: `combine_matmult_tiles` = 6 `PyObject_Size`, 2 `PySlice_New`, 2 `PyNumber_Add`, 1 `PyTuple_New`; `combine_sparse_matmult_tiles` = **16** `PyObject_Size`; `combine_trn2_double_row` = **6** `PyNumber_Add`. **CONFIRMED** — exact counts match.

3. **"TRN2 double-row enforces a size-2 pack via `cmp $0x2` guards and restricts dtype to fp8/uint8."** Re-counted: **4** `cmp …0x2,` in the `0xcbc90–0xce510` range; all five contract strings present verbatim (`"first F dim … must be 2"`, `"last dimension … size of 2"`, `"first dimension of Cayman … size of 2"`, `"only support fp8e4m3 and fp8e5m2"`, `"only support uint8"`). **CONFIRMED.**

4. **"`get_sb_and_psum_shape` returns `(sb_shape, psum_shape)` as two lists in a tuple."** Re-counted in `0x6ec10–0x6f720`: **2× `PyList_New` + 1× `PyTuple_New`**; the full SBUF/PSUM attribute roster (`statebuf_num_partitions`, `psum_num_banks`, `psum_par_size_in_bytes`, …) present. **CONFIRMED return structure.** The RichCompare comparand remains **INFERRED**.

5. **"The offloaded family emits `TiledOffloadedFMA`/`TiledOffloadedMemCpy` with BIR twins."** Re-checked: KB.so rodata `TiledOffloadedFMA` (4), `TiledOffloadedMemCpy` (4); BIR.so `codegenTiledOffloadedFMA` (12), `codegenTiledOffloadedMemCpy` (12), `codegenOffloadedMemCast` (12), `extract_offloaded_memcpy_shape_bases` (1). **CONFIRMED both `.so`.** The DMA-engine routing is **STRONG** (naming-based).

No claim failed re-challenge. Items tagged INFERRED/STRONG in the body are the RichCompare comparands (`get_sb_and_psum_shape`, `process_new_neuroninst` per-kind dispatch), the `combine_tiles` concat-vs-accumulate fold semantics, the kernel-name-validate step in `finalize_kernel`, and the DMA-vs-PE engine routing — each gated by Cython's `mstate`-routed attribute interning (`__pyx_mstate_global_static` @ `0x2d6ce0`), which defeats direct-`lea` annotation of attribute operands. The accumulate skeletons, dtype/size contracts, returned shapes, macro names, method ordering, and BIR twins are byte-CONFIRMED.
