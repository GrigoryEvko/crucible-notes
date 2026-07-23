# Sparse Matmul — `MatMulSparseOp` Producer and Lowering

> *All symbols and addresses on this page apply to neuronx-cc 2.24.5133.0+58f8de22 (cp310; cp311/cp312 differ in offsets only). Cython `__pyx_pw_…` wrapper symbols and `.rodata` interned strings are read from the shipped `.so` files: `BirCodeGenLoop.cpython-310-…so`, `KernelBuilder.cpython-310-…so`, `MatMulSparseOpGen.cpython-310-…so`, `private_api.cpython-310-…so`; the BIR hardware-instruction class is read from `libBIR.so` / `libwalrus.so` / `libBIRSimulator.so` (native C++, `nm -DC`).*

## Abstract

Structured sparsity on the NeuronCore PE array is a **first-class, user-driven** matmul, not a compiler optimization. There is exactly one entry — the NKI intrinsic `nc_matmul_sparse(moving, stationary, tags, compress_ratio)` — and from there a single node type, **`MatMulSparseOp`**, threads the whole compiler: the forward builder shapes its three tiles, the IR node carries the user's `compress_ratio` and tag metadata verbatim, `BirCodeGenLoop.codegenMatMulSparseOp` lowers it to one BIR `InstMatmultSparse`, and the walrus backend expands that into the PE-array micro-op sequence (`LoadTagsSparse` opcode 6 + `MatmultSparse` opcode 7). At no point does the compiler *decide* which weights are sparse — it trusts the externally-supplied structured mask. This is the deliberate opposite of NVIDIA 2:4, where ASP / cuSPARSELt prunes by magnitude.

This page owns the **producer and Penguin-IR side** of that pipeline: the user intrinsic, the `KernelBuilder` tile combiner, the `MatMulSparseOp` IR node, and the `BirCodeGenLoop` lowering down to `InstMatmultSparse` with its three operand access patterns. It does **not** re-document the on-device compression datapath (the `SparsityCompress`/`SparsityCompressTag` DVE ops, the uint16 nibble-tag bit-layout, and the host-side `to_compressed_sparse` format) — that is the consumer/format story owned by [Sparsity Compress](sparsity-compress.md). It also does not re-derive the per-byte wire encoding of opcodes 6/7 — that is [PE Matmul Encoding §Sparse](../isa/pe-matmul-encoding.md). The boundary is the BIR `InstMatmultSparse` object: this page builds it, those pages consume/encode it.

The distinctive shape of this op is its **three-operand contract** carried through every layer. A dense matmul is `(moving, stationary) → dst`. A sparse matmul is `(moving, stationary_compressed, tags) → dst`, where the stationary weight operand is reduced ~4× along its contraction dimension and the `tags` tensor records which dense column each kept value maps to. The single most counter-intuitive fact for a reimplementer: the `compress_ratio` is read off the IR node and handed to the BIR instruction *unchanged* in `codegenMatMulSparseOp`, and is *not* recomputed by the tile combiner — geometry and ratio are deliberately split across two layers.

For reimplementation, the contract is:

- The producer path: `nc_matmul_sparse` → `nki_ctx().matmult_sparse` → `MatMulSparseOp(tags, compress_ratio, compress_indices, tag_indices)`, and the `combine_sparse_matmult_tiles` three-tile shape check that runs alongside it.
- The IR node `MatMulSparseOp ⊂ MatMulOpBase`: which fields are sparse-specific (`tags`, `compress_ratio`, `compress_indices`, `tag_indices`) and which are inherited (`is_transpose`, `is_lhs_onezero`, `is_rhs_onezero`, replication, tile geometry, `dst`).
- The lowering `codegenMatMulSparseOp`: the exact setter sequence onto `InstMatmultSparse`, the float32r dtype assert on the weight operand, and the operand-AP routing where **only operand[0]** goes through `addSparseMatmulAP`.
- The BIR `InstMatmultSparse` class: its accessor / serialization / latency surface that proves it is a timed hardware instruction, not a pseudo-op.

| | |
|---|---|
| **User intrinsic** | `nc_matmul_sparse` `@0x23480` (`nki/_private/private_api.so`) |
| **Forward tile combiner** | `combine_sparse_matmult_tiles` `@0x12b670` (`KernelBuilder.so`) |
| **Penguin IR node** | `MatMulSparseOp.__init__` `@0x150b0` (`MatMulSparseOpGen.so`), `⊂ MatMulOpBase` |
| **Lowering** | `BirCodeGenLoop.codegenMatMulSparseOp` (`_37`) `@0x102cd0` → bir `Opcode.MatmultSparse` |
| **Sparse operand AP** | `BirCodeGenLoop.addSparseMatmulAP` (`_281`) `@0xf2430` |
| **BIR HW instruction** | `bir::InstMatmultSparse` (`libBIR.so` ctor `@0x176340`); four `Hwm` latency overloads (`getLatency{,Exec,ReadInit,WriteDrain}`) |
| **Detection model** | **User-annotated** (the `tags`/`compress_ratio` pair is structural input — *not* a boolean mask; no magnitude prune, no `backend_config` lowering) |
| **Compression design point** | `compress_ratio = 4` (4:1); `stationary_K → K/4`, 4-bit nibble tag per kept column |

---

## The Pipeline at a Glance

```text
USER NKI kernel
  └─ nc_matmul_sparse(moving, stationary, tags, compress_ratio)     private_api  @0x23480
        └─ nki_ctx().matmult_sparse(moving=, stationary=, tags=, compress_ratio=)
              └─ combine_sparse_matmult_tiles(lhs, rhs, tag)        KernelBuilder @0x12b670
              │     (asserts lhs/rhs/tag share contract shape; builds AP index subst map)
              └─ MatMulSparseOp(tags, compress_ratio,              MatMulSparseOpGen @0x150b0
                                compress_indices, tag_indices)      (⊂ MatMulOpBase)
                    └─ codegenMatMulSparseOp(self, inst)            BirCodeGenLoop @0x102cd0
                          ├─ bb.addInstruction(Opcode.MatmultSparse, …)
                          ├─ set_compress_ratio(inst.compress_ratio)        ← verbatim
                          ├─ set_is_transpose / set_is_fmap_onezero / set_is_weight_onezero
                          ├─ set_replication_{resolution,shift_amnt,num_rows}  (if replicate_ap)
                          ├─ setTilePosition / set_tile_size
                          ├─ addSparseMatmulAP(BIR, operand[0]=weight+tag)   @0xf2430
                          └─ addAP(BIR, operand[1]=ifmap) ; addAP(operand[2]) ; dst AP
                                └─ bir::InstMatmultSparse                    libBIR @0x176340
                                      └─ walrus visitInstMatmultSparse → LoadTagsSparse(6) + MatmultSparse(7)
                                                                         (see pe-matmul-encoding.md, sparsity-compress.md)
```

> **NOTE —** "lhs/rhs" (Penguin IR) ≡ "fmap/weight" (BIR hardware) ≡ "moving/stationary" (NKI intrinsic). The **stationary / rhs / weight** is the compressed, structured-sparse operand (`float32r`); the **moving / lhs / fmap / ifmap** is the dense activation. The setter rename `is_lhs_onezero → set_is_fmap_onezero` and `is_rhs_onezero → set_is_weight_onezero` happens inside the lowering (§3).

---

## 1. The Producer — `nc_matmul_sparse`

### Purpose

`nc_matmul_sparse` is the user-facing NKI intrinsic that *creates* a sparse matmul. It is the only path into the sparse-matmul machinery; the public dense matmul never dispatches to it (§5). It is a thin trampoline: it forwards its four arguments to the active codegen context's `matmult_sparse` builder.

### Entry Point

```text
neuronxcc/nki/_private/private_api.py : nc_matmul_sparse           @0x23480
  symbol: __pyx_pw_9neuronxcc_3nki_8_private_11private_api_1nc_matmul_sparse
```

### Algorithm

```c
// nc_matmul_sparse  —  private_api.so @0x23480.  body interns
// matmult_sparse(1×), nki_ctx(2×), moving(2×), stationary(2×), tags(11×), compress_ratio(19×).
PyObject* nc_matmul_sparse(moving, stationary, tags, compress_ratio) {
    ctx = nki_ctx();                                  // module-global active codegen ctx
    return ctx.matmult_sparse(                         // getattr → 4 keyword args set
        moving        = moving,                        // dense activation (lhs/fmap)
        stationary    = stationary,                    // structured-sparse weight (rhs)
        tags          = tags,                          // sparsity metadata (4-bit indices)
        compress_ratio= compress_ratio);               // user-chosen ratio (design point 4)
}
```

The argument names are `{moving, stationary, tags, compress_ratio}`, read from the `__pyx_pyargnames` table. The user passes **both** the `tags` tensor and the `compress_ratio` explicitly — neither is inferred by the compiler. `matmult_sparse` is resolved by `getattr` on the `nki_ctx()` object (the `KernelBuilder.NeuronCodegen` forward builder; see [Forward Builder](neuroncodegen-forward-builder.md)) and called with the four keyword arguments.

> **GOTCHA — the matmul cost `4×` is the fp32 penalty, not the compression factor.** The shipped NKI ISA stub gives the Tensor-Engine `nc_matmul` cost as `max(min(64, N_stationary), N_moving)` cycles for low-precision inputs (`float8_e4m3/e5m2/bfloat16/float16/tfloat32`) and `4 * max(min(64, N_stationary), N_moving)` for `float32` (`neuronxcc-stubs/nki/isa/__init__.pyi:957–959`), where `N_stationary` / `N_moving` are elements-per-partition. That `4×` is a float32 cost multiplier, not a `/4` compression term, and is no evidence for `compress_ratio = 4`. The 4:1 design point rests on two independent facts: the element-count `4*out == in` invariant and the 4-bit (16-position) nibble tag that selects one of four lanes per group ([Sparsity Compress §tag layout](sparsity-compress.md)). `compress_ratio` itself stays a free user parameter on both the intrinsic and the IR node.

---

## 2. The Forward-Builder Tile Combiner — `combine_sparse_matmult_tiles`

### Purpose

Inside the `KernelBuilder` forward builder, once the moving / stationary / tag tensors are tiled, `combine_sparse_matmult_tiles` aligns the three tiles' access patterns so the PE array reads `ifmap[i]`, `weight[i]`, `tag[i]` in lockstep. Its job is purely **tile geometry**: assert the three tiles share contract shape, build the flattened AP index list, filter it, and emit an index-substitution map. It does **not** compute the compression ratio.

### Entry Point

```text
neuronxcc/nki/compiler/backends/neuron/KernelBuilder.py : combine_sparse_matmult_tiles  @0x12b670
  symbol: __pyx_pw_9neuronxcc_3nki_8compiler_8backends_6neuron_13KernelBuilder_9combine_sparse_matmult_tiles
```

### Algorithm

```c
// combine_sparse_matmult_tiles(lhs_tile, rhs_tile, tag_tile)  —  KernelBuilder.so @0x12b670
// THREE tiles: lhs = ifmap/activation, rhs = compressed weight, tag = sparsity tag tile.
void combine_sparse_matmult_tiles(lhs_tile, rhs_tile, tag_tile) {
    lps = lhs_tile.par_shape;  lfs = lhs_tile.free_shape;     // par/free of each tile
    rps = rhs_tile.par_shape;  rfs = rhs_tile.free_shape;
    tps = tag_tile.par_shape;  tfs = tag_tile.free_shape;

    // (1) shape-equality guard across all THREE operands  — .rodata literal:
    //     "LHS, RHS and TAG should has the same number of contract dim!"
    assert (lps,lfs) == (rps,rfs) == (tps,tfs);

    // (2) build the flattened AP index list over reversed par/free shapes
    idx = generate_ap_index(reversed(par_shape), reversed(free_shape));   // global helper
    idx = tuple(par_idx) + tuple(free_idx);                               // concat

    // (3) filter, then slice
    filtered = filter_indices(idx)[:N];                                   // 14 refs in body

    // (4) per-tile index equality guard  — .rodata literal: "Indices mismatch!"
    for tile in (lhs, rhs, tag): read tile.par_indices / tile.free_indices;
    assert indices_match_across(lhs, rhs, tag);

    // (5) emit the substitution map that lines up the three operands' APs
    subst = generate_subst_map(...);                                      // global helper
    return subst;
}
```

> **NOTE — this combiner is not part of `BirCodeGenLoop`.** Its symbol is `…KernelBuilder…9combine_sparse_matmult_tiles` and its `__FILE__` is `KernelBuilder.py`: it belongs to the NKI *forward builder* ([three-sink model](three-sink-kernel-model.md)), which runs before any `BirCodeGenLoop` lowering.

> **QUIRK — no `compress_ratio` here.** The token does not appear anywhere in this function body. The 4:1 ratio is carried on the `MatMulSparseOp` IR node (from the user's `nc_matmul_sparse` call) and is an *input* to the geometry, not something this combiner derives. Compression ratio and tile geometry are deliberately split across two layers — a reimplementer must not fold them.

The third operand being a **first-class TAG tile of equal shape** is the forward-builder mirror of the on-device tag datapath: at this layer the tag is a tile combined with lhs+rhs; downstream the walrus backend's `generateLoadTagsSparse` streams it into the PE array as the uint16 nibble tag ([Sparsity Compress](sparsity-compress.md)). The tag is therefore *supplied* by the user / intrinsic, never synthesised by a detection pass.

---

## 3. The Penguin IR Node — `MatMulSparseOp`

### Purpose

`MatMulSparseOp` is the high-level Penguin operator node for a structured-sparse matmul. It is a generated class (`MatMulSparseOpGen.c` → `MatMulSparseOpGen.so`) that **inherits the dense matmul base** `MatMulOpBase`, adding only the sparse-specific fields. It is the node `BirCodeGenLoop` later lowers; it sits in the [TensorOp family](../penguin/tensor-op-family.md) as a contraction op whose stationary operand carries compressed structure.

### Class Shape

```text
class MatMulSparseOp(MatMulOpBase):                # __FILE__ = …/generated/MatMulSparseOpGen.c
    __init__(self, tags, compress_ratio, compress_indices, tag_indices, **kwargs)
    # sparse-specific fields : tags, compress_ratio, compress_indices, tag_indices, ap_indices
    # inherited from MatMulOpBase : is_transpose, is_lhs_onezero, is_rhs_onezero,
    #                               replicate_ap, pe_tile_position/size, dst, operands
    # methods : compress_ratio, compress_indices, tag_indices, ap_indices, operands,
    #           loadTensor, serialize, verify, replaceUseOfWith, enumerate_ap_indices,
    #           remove_ap_index, updateAPIndicies, _updateAllIndicesList
```

The class name, base class `MatMulOpBase`, method roster, and the field names `tags` / `compress_ratio` / `compress_indices` / `tag_indices` / `ap_indices` all appear as interned strings in `MatMulSparseOpGen.so`.

### Construction

```c
// MatMulSparseOp.__init__  —  MatMulSparseOpGen.so @0x150b0
// argnames: {self, tags, compress_ratio, compress_indices, tag_indices, **kwargs}
void MatMulSparseOp.__init__(self, tags, compress_ratio, compress_indices, tag_indices, **kwargs) {
    MatMulOpBase.__init__(self, **kwargs);             // dense base: operands, dst, tile, onezero
    self.tags            = tags;                        // sparsity tag tensor (4-bit indices)
    self.compress_ratio  = compress_ratio;             // verbatim from nc_matmul_sparse (≡4)
    self.compress_indices= compress_indices;           // per-tile kept-row index lists
    self.tag_indices     = tag_indices;
    self.set_access_mode(AccessMode.LOAD);             // operands are loads
    self.link_operands(...);                            // wire operand use-def edges
}
```

`compress_ratio` is the most-referenced token in the constructor body (interned `36×`), reflecting that it is stored, exposed via a `compress_ratio` accessor method, and threaded into the AP-index machinery. The operands are marked `AccessMode.LOAD`, and `link_operands` wires the use-def edges.

> **NOTE — `compress_indices` / `tag_indices` are not seen at lowering time.** These per-tile index lists (which rows are kept) are consumed by the forward-builder geometry (§2) and become the nibble-tag stream downstream; `codegenMatMulSparseOp` (§4) reads `compress_ratio`, the onezero/transpose flags, tile geometry and the operand APs — not `compress_indices`/`tag_indices` directly. They are derived from the user's `tags` tensor, **not** from weight magnitude. (The fields themselves are read off the class; where exactly they are consumed is [INFERRED] from the absence of any reference at lowering time.)

---

## 4. The Lowering — `codegenMatMulSparseOp`

### Purpose

`BirCodeGenLoop.codegenMatMulSparseOp` is the per-instruction codegen that turns one tiled `MatMulSparseOp` into one BIR `InstMatmultSparse`. It is the 37th codegen method (`_37`) of `BirCodeGenLoop`, sitting beside `codegenMatMulOp` and `codegenMatMulMxOp` in [BirCodeGenLoop compute codegen](bircodegen-compute.md). It creates the instruction, copies the sparse flags and ratio, sets tile placement, and attaches the four access patterns.

### Entry Point

```text
neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.py : codegenMatMulSparseOp(self, inst)  @0x102cd0
  symbol: __pyx_pw_…_14BirCodeGenLoop_14BirCodeGenLoop_37codegenMatMulSparseOp
  (pw+pf inlined: Hex-Rays reports "local variable allocation has failed"; 3761-line body)
  scope: __pyx_scope_struct_8 (closure — two nested genexprs → generator5 @0x70e40 / generator6 @0x715d0)
```

### Algorithm

```c
// codegenMatMulSparseOp(self, inst)  —  BirCodeGenLoop.so @0x102cd0
// Penguin MatMulSparseOp → ONE bir Opcode.MatmultSparse instruction, engine PE.
Instruction codegenMatMulSparseOp(self, inst) {
    bb   = self.current_basic_block;
    name = "I %s" % inst.id;                            // __pyx_kp_u_I_s
    di   = build_debuginfo(inst);                       // module-global
    BIR  = bb.addInstruction(Opcode.MatmultSparse, name, di);   // *** create the sparse inst ***
                                                        // "MatmultSparse" .rodata literal

    // ---- replication block (activation broadcast; guarded on inst.replicate_ap) ----
    if (inst.replicate_ap) {                            // INFERRED guard condition
        BIR.set_replication_resolution(...);            // mirrors the dense MatMul replication path
        BIR.set_replication_shift_amnt(...);
        BIR.set_replication_num_rows(...);
    }

    // ---- sparse / onezero / transpose flags ----
    if (inst.is_transpose)   BIR.set_is_transpose();    // no-arg setter
    if (inst.is_lhs_onezero) BIR.set_is_fmap_onezero();   // LHS(=fmap/ifmap) one-zero fast path
    if (inst.is_rhs_onezero) BIR.set_is_weight_onezero();  // RHS(=weight) one-zero fast path
    BIR.set_compress_ratio(inst.compress_ratio);        // *** verbatim copy, no derivation ***

    (lhs, rhs, third) = inst.operands;                  // EXACTLY 3 operands unpacked

    // ---- assertion guards (only when NOT python -O; gated on Py_OptimizeFlag) ----
    assert inst.dst.free_ap_size % 2 == 0;              // dst free-AP size must be even
    assert operand[0].dtype == dt.float32r;             // WEIGHT operand must be float32r ('r' = packed fp32)
    assert inst.dst.start_partition % N == 0,           // literal:
           "Illegal start partition in MatMulOp: %s" % …;

    // ---- PE-array tile placement ----
    if (inst.pe_tile_position_x is not None && inst.pe_tile_position_y is not None) {
        BIR.setTilePosition(inst.pe_tile_position_x, inst.pe_tile_position_y);
        BIR.set_tile_size(inst.pe_tile_size_x, inst.pe_tile_size_y);
    }

    // ---- operand access patterns (THE LOWERING PAYOFF) ----
    num = (operand.num_result_elts for operand in …);   // genexpr (closure generator5/6)
    self.addSparseMatmulAP(BIR, operand[0], inst);      // *** operand[0] = compressed weight + tag ***
    self.addAP(BIR, operand[1], …);                     // plain AP: ifmap / activation (moving)
    self.addAP(BIR, operand[2], …);                     // plain AP: 3rd operand
    if isinstance(inst.dst, NeuronAP) BIR.addAP(dst …); // result / output AP
    return BIR;
}
```

The setter sequence — `set_compress_ratio`, `set_is_transpose`, `set_is_fmap_onezero`, `set_is_weight_onezero`, the three `set_replication_*`, `setTilePosition`, `set_tile_size` — and the calls `addSparseMatmulAP` (`1×`), `addAP` (`3×`), the `MatmultSparse` literal, `float32r` (`3×`), `num_result_elts` (`2×`), `build_debuginfo` (`2×`) are all present in the decompiled body. The `"Illegal start partition in MatMulOp: %s"` assert string is interned in the `BirCodeGenLoop` string table.

> **GOTCHA — this op carries no mask.** Neither `mask` nor `shuffle` appears anywhere in the `@0x102cd0` body, and there is no `setMask` call. The interned literal `"Mask pattern only support 1 step multiplier"` belongs to a *different* op — it lives in the module-wide `…CreateStringTabAndInitStrings…` init and is never referenced from this codegen. The sparse matmul carries its structure through `compress_ratio`, the `tags` / `compress_indices` / `tag_indices` node fields, and `addSparseMatmulAP` — never through a mask setter.

> **QUIRK — three operands, one sparse AP.** `inst.operands` is unpacked as exactly three (the decompile asserts the unpack count). Only `operand[0]` — the compressed, stationary, `float32r` weight — is routed through `addSparseMatmulAP`; `operand[1]` (ifmap/moving) and `operand[2]` use the plain `addAP`. The Cython layer emits **one** `addInstruction(Opcode.MatmultSparse)` and never separately emits `Ldweights`/`Ldtags`; those are expanded later by the walrus backend from this single fused instruction (§6, [PE Matmul Encoding](../isa/pe-matmul-encoding.md)).

> **NOTE — float32r weight.** The `operand[0].dtype == dt.float32r` assert (3 `float32r` references in-body) requires the stationary compressed weight to be in the `r` (packed/replicated fp32) representation — the same representation the dense matmul uses for its fp32 two-pass marker ([PE Matmul Encoding §dtype remap](../isa/pe-matmul-encoding.md), wire tag `0x0b`).

---

## 5. Detection vs Annotation — Why the Compiler Never Prunes

A central design question: does the compiler *detect* sparsity (magnitude-prune a dense matmul into a sparse one) or does the user *annotate* it? The binary answer is unambiguous: **annotation**. Four independent negatives establish it.

| Evidence | Finding | Confidence |
|---|---|---|
| `nc_matmul_sparse` args | The user passes `tags` **and** `compress_ratio` explicitly; the body forwards them verbatim to `matmult_sparse`. The compiler never derives either. | CERTAIN |
| Public dense matmul | `nki tensor._matmul` `@0x42510` (`backends/neuron/tensor.py`) has **zero** sparse / compress / tag interned names — no "if weight is sparse → sparse path" branch. Dense and sparse matmuls are separate user-callable ops. | CERTAIN |
| HLO frontend | `codegenMatMulSparseOp` has **no** `backend_config` interned name (0 hits); no `hlo2penguin` decompile references `MatMulSparse`. There is no path rewriting a `backend_config={sparse:…}` annotation on a dense HLO matmul into a `MatMulSparseOp`. | CERTAIN |
| `combine_sparse_matmult_tiles` | The tag is a first-class **input** tile of equal shape (§2), and the on-device compress op's mask is also an external input ([Sparsity Compress §mask](sparsity-compress.md)). No `topk`/`argsort`/`abs`/`threshold` magnitude logic anywhere. | CERTAIN |

> **GOTCHA — opposite of NVIDIA 2:4.** On NVIDIA, ASP / cuSPARSELt performs the magnitude prune and *produces* the 2:4 mask at quantization time. Here, the mask is assumed to already exist (training produced a structured-sparse weight per the L:R pattern), and the compiler is a faithful **carrier**: it trusts the user's `tags` and `compress_ratio` and never makes a hardware magnitude decision. A reimplementer porting from a CUDA mental model must not add a prune pass — there is none.

The `prune` / `PruneFunctions` passes that *do* exist in `hlo2penguin` are MLIR dead-**function** elimination, unrelated to weight pruning. The "PruneFunctions" name is a false friend.

---

## 6. The BIR Hardware Instruction — `bir::InstMatmultSparse`

### Purpose

`codegenMatMulSparseOp` (§4) materializes one `bir::InstMatmultSparse`. This C++ class (in `libBIR.so`, simulated by `libBIRSimulator.so`, encoded by `libwalrus.so`) is a **first-class hardware instruction** with its own validators, latency models, and JSON serialization — not a pseudo-op. It is the boundary object: the producer side builds it; the consumer/encoder side ([PE Matmul Encoding](../isa/pe-matmul-encoding.md), [Sparsity Compress](sparsity-compress.md)) lowers it to the PE micro-ops.

### Class Surface (from `libBIR.so` `nm -DC`)

```text
bir::InstMatmultSparse                                       libBIR @0x176340 (ctor C1/C2)
  — operand accessors : getIfmap, getWeights, getTags, getDst
  — dtype validators  : getIfmapValidDtypes, getWeightsValidDtypes,
                        getTagsValidDtypes, getDstValidDtypes
  — ratio (affine)    : getCompressRatioEvalIfNeeded(DenseMap<AffineIdx*,…>)  ← see QUIRK
  — engine binding    : validEngines, getValidEngines, getDefaultEngine
  — affine / clone    : evalFieldsInto, updateAffineExprs, updateCloneAffineExprs, clone
  — serialization     : toJson, readFieldsFromJson, createFromJson  (nlohmann::json)
  — misc              : getLatency, isSymbolic, ArithOps
```

Every symbol above appears in `libBIR.so`'s native-exports table. `getWeights` / `getTags` / `getIfmap` / `getDst` make the **three-operand-plus-dst** contract explicit at the C++ level; the four `get*ValidDtypes` accessors gate each operand's dtype independently (the moving-operand restriction lives in `getIfmapValidDtypes`, `libwalrus @0x5eb200`).

### Serialization & Latency

The class round-trips through JSON via `toJson` / `readFieldsFromJson` / `createFromJson` (`createFromJson @0x181790`). All nine serialized field names are present as interned strings in `libBIR.so`: `compress_ratio`, `is_transpose`, `accumulation_flag`, `psum_zero_region`, `replication_num_rows`, `replication_resolution`, `replication_shift_amnt`, `tile_position`, `tile_size` (the onezero flags ride in the shared `MatMul` base serialization). The de/serializer resolves them through rodata string pointers rather than inlined C literals — standard for `nlohmann::json` field lookups — so the names show in the string pool, not the decompiled function body.

Latency is modeled per-architecture — **all four** overloads exist for `InstMatmultSparse` across `Gen3Hwm`, `CoreV4Hwm`, and `bir::Hwm`:

```text
ZNK7Gen3Hwm10getLatency           …InstMatmultSparse     ZNK9CoreV4Hwm10getLatency           …InstMatmultSparse
ZNK7Gen3Hwm14getLatencyExec       …InstMatmultSparse     ZNK9CoreV4Hwm14getLatencyExec       …InstMatmultSparse
ZNK7Gen3Hwm18getLatencyReadInit   …InstMatmultSparse     ZNK9CoreV4Hwm18getLatencyReadInit   …InstMatmultSparse
ZNK7Gen3Hwm20getLatencyWriteDrain …InstMatmultSparse     ZNK9CoreV4Hwm20getLatencyWriteDrain …InstMatmultSparse
```

Distinct read-init / exec / write-drain latencies prove the hardware-model treats the sparse matmul as a real PE-array instruction with its own pipeline timing.

> **QUIRK — `compress_ratio` is an affine expression, not a plain int.** The accessor is `getCompressRatioEvalIfNeeded(DenseMap<pelican::AffineIdx*, long>)` — i.e. the BIR `compress_ratio` field is a `variant<int, QuasiAffineExpr>` that may require evaluation against the loop-index map before it is a concrete number (consistent with [PE Matmul Encoding §sparse](../isa/pe-matmul-encoding.md), which reads it from `InstMatmultSparse+0x328`). `codegenMatMulSparseOp` (§4) sets it from `inst.compress_ratio`, which for the common path is the literal `4` from the user, but the BIR layer keeps the general affine form so the ratio can be a function of a tiling axis. (The accessor symbol is exported; reading its `variant<int, QuasiAffineExpr>` shape off that signature is [INFERRED].)

### Simulation

`birsim::InstVisitor::visitInstMatmultSparse` (`libBIRSimulator @0x1f0a10`, `libwalrus @0x5f6000`) executes the instruction in software: it re-expands the tag stream to dense columns (`dense_col(lane) = (tag_word >> (4*lane)) & 0xF`) and accumulates over each group of 4. The asserts `num_active_rows % 4 == 0` and `num_active_rows == 128` reinforce the group-of-4 structure and the single supported 128-active-row case. The tag bit-layout and the consumer-side codegen are documented in [Sparsity Compress §tag layout](sparsity-compress.md).

---

## 7. Reimplementation Checklist

A faithful reimplementation of the producer/lowering side must reproduce:

1. **A single user intrinsic** `nc_matmul_sparse(moving, stationary, tags, compress_ratio)` that forwards verbatim to the codegen context — no dense-matmul auto-dispatch, no magnitude prune (§1, §5).
2. **A three-tile geometry combiner** that asserts moving/weight/tag share contract shape (`"LHS, RHS and TAG should has the same number of contract dim!"`), builds an AP index list, and emits a substitution map — **without** touching `compress_ratio` (§2).
3. **An IR node** `MatMulSparseOp ⊂ MatMulOpBase` carrying `{tags, compress_ratio, compress_indices, tag_indices}` as constructor args, with `AccessMode.LOAD` operands (§3).
4. **A lowering** that creates one `Opcode.MatmultSparse`, copies `compress_ratio` and the onezero/transpose flags (with the `lhs→fmap` / `rhs→weight` rename), asserts the weight operand is `float32r` and `dst.free_ap_size` is even, sets tile geometry, and routes **only operand[0]** through the sparse AP builder while the other two use plain `addAP` (§4).
5. **A sparse AP builder** (`addSparseMatmulAP`, [BirCodeGenLoop AP §3.1](bircodegen-ap.md)) that reads `compress_ratio` to size/stride the compressed-weight AP and folds the companion tag/index address stream into the same `createAP` operand — so the BIR instruction's operand[0] is the weight+tag pair (§4 QUIRK). The address concatenation itself is [INFERRED] from the `PyNumber_Add` of two `addrs` streams feeding one `createAP`.
6. **A BIR class** `InstMatmultSparse` with `getIfmap`/`getWeights`/`getTags`/`getDst`, per-operand dtype validators, an affine `compress_ratio`, JSON round-trip, and per-arch latency overloads (§6).

> **NOTE — what this page does not own.** The on-wire bit-layout of the emitted `LoadTagsSparse` (opcode 6) and `MatmultSparse` (opcode 7) bundles, the row-group LUT, and the dtype-remap legality gates are in [PE Matmul Encoding](../isa/pe-matmul-encoding.md). The `SparsityCompress`/`SparsityCompressTag` DVE ops, the uint16 4×nibble tag format, the flipped tag ordering, and the host-side `to_compressed_sparse` packing are in [Sparsity Compress](sparsity-compress.md). The shared sparse access-pattern builder body is in [BirCodeGenLoop AP Builders](bircodegen-ap.md).

---

## Evidence Ledger

| Claim | Confidence | Basis |
|---|---|---|
| `codegenMatMulSparseOp` setter sequence + `addSparseMatmulAP(operand[0])` + 3× `addAP` + `MatmultSparse` literal + `float32r` asserts + "Illegal start partition…" | CERTAIN | Token counts in `@0x102cd0` body; assert string in BirCodeGenLoop strtab |
| No mask / no `setMask` in `codegenMatMulSparseOp` | CERTAIN | zero `mask` hits in `@0x102cd0`; mask string only in module strtab-init |
| `nc_matmul_sparse(moving, stationary, tags, compress_ratio)` → `nki_ctx().matmult_sparse` | CERTAIN | argnames + body interns (`matmult_sparse`, `nki_ctx`, `compress_ratio`×19) |
| `MatMulSparseOp ⊂ MatMulOpBase`; ctor `{tags, compress_ratio, compress_indices, tag_indices}`; `AccessMode.LOAD` | CERTAIN | `MatMulSparseOpGen.so` strings + ctor body |
| `combine_sparse_matmult_tiles` shape asserts, helpers, **no** compress_ratio | CERTAIN | `@0x12b670` body; both assert literals verbatim; zero `compress_ratio` hits |
| Annotation, not detection (no auto-dispatch / `backend_config` / magnitude prune) | CERTAIN | 4 negatives (§5) |
| `bir::InstMatmultSparse` class surface + four `Hwm` latency overloads (`getLatency{,Exec,ReadInit,WriteDrain}`) + JSON symbols + affine `compress_ratio` | CERTAIN | `libBIR.so` / `libwalrus.so` native-exports |
| `addSparseMatmulAP` packs weight+tag addresses into one `createAP` operand | HIGH | tokens (`createAP`, `neuron_ap`, `compress_ratio`, `addArgumentOrOutput`) present; address-stream concatenation inferred |
| `nc_matmul` cost table `max(min(64,N_stationary),N_moving)`, `4×` for fp32 (not a compression term) | CERTAIN | shipped stub `nki/isa/__init__.pyi:957–959` |
| BIR JSON field list (9 names present in `libBIR.so` strings); replication-block guard condition | MEDIUM | field set resolved via rodata pointers in `toJson`/`createFromJson`, not inlined; `replicate_ap` truthiness guard not byte-pinned |
