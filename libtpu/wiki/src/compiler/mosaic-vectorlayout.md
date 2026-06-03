# Mosaic VectorLayout

> *All addresses, symbols, field offsets, op-name strings, and error strings on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Field offsets and `CHECK` line numbers are byte-exact from the decompiled binary. Other versions will differ.*

## Abstract

`mlir::tpu::VectorLayout` is the atom of the Mosaic backend: a 56-byte value type that says *how one logical `vector<…>` SSA value is packed into the TPU's hardware vector registers*. Every `vector`-typed operand and result inside a Mosaic kernel carries one of these — attached as the `in_layout` / `out_layout` array attributes — and the entire tail of the Mosaic pipeline is the machinery that consumes them. This page documents the value type itself (the `(sublane, lane)` tiling algebra, the offset/replication model, the `ImplicitDim` rank-collapse model, the per-vreg packing and vreg-count math), the `applyLayoutOp` dispatch that turns a layout-annotated op into native-vreg ops via a 49-entry rule table, and the `relayout` driver — the disassemble → shift → re-tile → reassemble engine that physically materializes a layout *change* into lane/sublane shuffles.

Scope boundary: the *inference* pass that **chooses** each value's layout (`VectorLayoutInferer`, the producer side) lives on [Mosaic Layout Inference](mosaic-layout-inference.md); this page owns the *struct* and the *applier*. The two are deliberately split in the binary: inference never inserts a relayout, so `applyLayoutOp` can assert that producer-out equals consumer-in.

For reimplementation, the layout-algebra contract is:

- **`VectorLayout` is a 56-byte POD: `{offsets[2], tiling[2], bitwidth, implicit_dim}`.** Offsets are `optional<int64>` (a missing offset means *replicated* along that hardware axis); tiling is `(sublane_tile, lane_tile)`; bitwidth is a power-of-two `≤ 32`; `implicit_dim` records which logical minor dims were collapsed. The constructor at `0x13249ba0` enforces six invariants (`layout.h:205-210`).
- **One vreg holds `packing = 32/bitwidth` tiles.** `tilesPerVreg` and `tileArrayShape` derive, from a layout plus a logical shape, exactly how many native vregs the value occupies and what concrete MLIR vreg type each is (`getNativeVregOrVmaskType`).
- **`applyLayoutOp` is a StringMap dispatch over op-name.** 49 base rules plus an out-of-tree extension set, with an `elementwise_op_rule` fallback for any op carrying `hasElementwiseMappableTraits`. Each rule unrolls its op's logical vectors into per-vreg native-shape ops.
- **A layout *change* is `relayout`: disassemble → changeOffsets → changeTiling → changeImplicitDim → assemble.** Lane/sublane shifts (column/row shift), re-tiling, and implicit-dim insert/drop are the three change primitives; the relayout pass guarantees the applier only sees a `tpu.relayout` op where one is genuinely required.

| | |
|---|---|
| **Value type** | `mlir::tpu::VectorLayout`; ctor `mlir::tpu::VectorLayout::VectorLayout` @ `0x13249ba0` (source `…/mosaic/dialect/tpu/layout.h`) |
| **Struct size** | 56 bytes; fields at `+0/+8/+16/+24` (offsets), `+32/+40` (tiling), `+48` (bitwidth, `i8`), `+52` (implicit_dim, `i32`) |
| **Invariants** | `layout.h:205-210` — single-bit `bitwidth ≤ 32`, `tiling[i] > 0`, `offset ≥ 0`, sublane offset `< tiling[0]` |
| **Textual grammar** | `<bw>,{<o0>,<o1>},(<t0>,<t1>)[,<implicit>]`; print @ `0x14a94d80`, parse @ `0x14a95b40`, `printImplicitDim` @ `0x14a94b40` |
| **Vreg-count math** | `tilesPerVreg` @ `0x1325cec0`; `tileArrayShape` @ `0x14a94160`; `getNativeVregOrVmaskType` @ `0x14b766e0` (`vreg_util.cc:58`) |
| **Applier dispatch** | `mlir::tpu::applyLayoutOp` @ `0x1325bca0`; `applyLayoutFunc` @ `0x1325cc80`; rule StringMap `rules()::$_0` @ `0x1325b100` (49 entries) |
| **Elementwise fallback** | `elementwise_op_rule` @ `0x1325c900` (any `hasElementwiseMappableTraits` op not in the table) |
| **Relayout driver** | `mlir::tpu::relayout` @ `0x1325a480` (`apply_vector_layout.cc:9865`); `disassemble` @ `0x132466a0`, `assemble` @ `0x132462c0` |
| **Change primitives** | `changeOffsets` @ `0x1324bac0`, `changeTiling` @ `0x1324c880`, `changeImplicitDim` @ `0x13253b80`; row/col shift @ `0x13248c80`/`0x13249d40` |
| **Confidence** | Confirmed (byte-anchored) unless a row or callout says otherwise |

---

## 1. The VectorLayout Value Type

A `VectorLayout` is the answer to one question: *given a logical `vector<…xT>` value, where does each of its elements live in the hardware's `(sublane × lane)` register grid?* It does **not** describe a memref (that is the `TiledLayoutAttr`, see [Mosaic Layout Inference](mosaic-layout-inference.md)); it describes a *vector SSA value* as it flows between `tpu`-dialect ops.

The constructor at `0x13249ba0` pins the byte layout exactly. The first 32 bytes are written with a single `vmovups ymm0` from the argument block (the two `optional<int64>` offsets), then tiling, bitwidth, and implicit_dim are stored individually:

```c
// VectorLayout::VectorLayout(bitwidth, offsets[2], tiling[2], implicit_dim) @0x13249ba0
__asm { vmovups ymmword ptr [rdi], ymm0 }   // +0..+31: offsets_[0..1] {value, has}
*(_QWORD *)(_RDI + 32) = a3;                 // +32: tiling_[0]  (sublane tile)
*(_QWORD *)(_RDI + 40) = a4;                 // +40: tiling_[1]  (lane tile)
*(_BYTE  *)(_RDI + 48) = a2;                 // +48: bitwidth_   (i8)
*(_DWORD *)(_RDI + 52) = a5;                 // +52: implicit_dim (i32 enum)
```

### 1.1 Field layout (byte-exact)

| off | field | type | meaning |
|----|----|----|----|
| `+0` | `offsets_[0].value` | `int64` | 2nd-minor (**sublane**) offset of the first element within a tile |
| `+8` | `offsets_[0].has` | `bool` | `false` ⇒ value is **replicated** across sublanes |
| `+16` | `offsets_[1].value` | `int64` | minor (**lane**) offset of the first element within a tile |
| `+24` | `offsets_[1].has` | `bool` | `false` ⇒ value is **replicated** across lanes |
| `+32` | `tiling_[0]` | `int64` | sublane tile size (e.g. 8 for f32, 16 for bf16) |
| `+40` | `tiling_[1]` | `int64` | lane tile size (e.g. 128) |
| `+48` | `bitwidth_` | `int8` | element bit width ∈ {1,2,4,8,16,32} |
| `+52` | `implicit_dim` | `int32` | `ImplicitDim` enum (§1.4) |

Confidence: Confirmed — every offset is read directly from the constructor store sequence above; the `optional<int64>` shape (8-byte value + 1-byte `has` flag, padded to a 16-byte slot) is what the `vmovups`/`value_or` accesses in the invariant checks confirm.

### 1.2 Constructor invariants

The constructor `CHECK`-fails on six conditions, each tagged with its `layout.h` source line (recovered verbatim from the `LogMessageFatal` call sites):

| line | condition | rationale |
|----|----|----|
| 205 | `llvm::has_single_bit(bitwidth_) && bitwidth_ <= 32` | bitwidth must be a power of two no wider than a register lane word |
| 206 | `tiling_[0] > 0` | sublane tile is positive |
| 207 | `tiling_[1] > 0` | lane tile is positive |
| 208 | `offsets_[0].value_or(0) >= 0` | sublane offset non-negative |
| 209 | `offsets_[1].value_or(0) >= 0` | lane offset non-negative |
| 210 | `offsets_[0].value_or(0) < tiling_[0]` | sublane offset stays inside one tile row |

> **NOTE — the lane offset is *not* bounded by the tiling.** Invariant 210 bounds only the *sublane* offset (`offsets_[0] < tiling_[0]`). The lane offset (`offsets_[1]`) is intentionally left unbounded by the constructor: a lane offset can span multiple lane tiles, and the tile-array math (§2) folds it across vregs. This asymmetry is real — the binary checks 208/209/210 but never `offsets_[1] < tiling_[1]`.

So the semantic reading of a `VectorLayout` is: *this value's elements are packed into vregs with sublane tile `tiling_[0]` and lane tile `tiling_[1]`; the first logical element sits at `(offsets_[0], offsets_[1])` within a tile; a missing offset means the value is broadcast (replicated) along that hardware axis; and `implicit_dim` records which logical minor dims were collapsed to fit the strictly-2-D-tiled vreg model.*

### 1.3 Textual grammar

`VectorLayout::print` @ `0x14a94d80` and `VectorLayout::parse` @ `0x14a95b40` are exact inverses. The grammar:

```text
<bitwidth>,{<o0>,<o1>},(<t0>,<t1>)[,<implicit>]
```

- `{`/`}` wrap the two offsets; a replicated (absent) offset prints as `*` (`0x2A`). `(`/`)` wrap the tiling.
- `<implicit>` is optional and emitted by `printImplicitDim` @ `0x14a94b40`: `NONE` → omitted, `MINOR` → `-1`, `SECOND_MINOR` → `-2`, `MINOR_AND_SECOND_MINOR` → `-2,-1`. `parse` accepts the same tokens.

Canonical examples:

```text
32,{0,0},(8,128)            f32, offset (0,0), native tiling 8x128, 1 tile/vreg
16,{0,0},(16,128)           bf16, packed sublane tile 16, native tiling
32,{*,0},(8,128)            f32 replicated across sublanes (sublane offset absent)
16,{0,0},(16,128),-1        bf16 with the minor logical dim implicit
```

Confidence: Confirmed — print/parse symbols exist at the cited VAs; `printImplicitDim` @ `0x14a94b40` confirmed present.

### 1.4 The ImplicitDim model

`VectorLayout::ImplicitDim` (the `int32` at `+52`) is a 4-value enum. It lets Mosaic represent a sub-rank logical vector (1-D, scalar) inside a model that is *always* at least 2-D-tiled, without special-casing every rule:

| value | name | printed | use |
|----|----|----|----|
| 0 | `NONE` | (omitted) | last two logical dims map directly to `(sublane, lane)` |
| 1 | `MINOR` | `-1` | minor (lane) logical dim is implicit (size 1); 1-D / lane-broadcast value |
| 2 | `SECOND_MINOR` | `-2` | second-minor (sublane) logical dim is implicit |
| 3 | `MINOR_AND_SECOND_MINOR` | `-2,-1` | both implicit; a scalar promoted to a vreg |

`implicitShape(shape)` @ `0x14a94080` (and the `insertImplicit<>` helpers @ `0x132458a0`) re-insert `popcount(implicit_dim_bits)` size-1 dims at the implicit positions, so all tile math runs on a `≥2-D` "implicit shape" regardless of the value's logical rank. Confidence: HIGH — the enum values are recovered from `printImplicitDim`'s output tokens; the `implicitShape`/`insertImplicit` mechanism is symbol-anchored but the per-helper unrolling was not individually decompiled.

---

## 2. Vreg-Count and Native-Type Math

A `VectorLayout` plus a logical shape determines (a) how many sub-tiles fit in one vreg, (b) how many vregs the whole value occupies, and (c) the concrete MLIR type of each vreg. These three functions are what the apply rules call to turn a logical vector into a concrete `xla::Array<Value>` of native vregs.

### 2.1 tilesPerVreg

`VectorLayout::tilesPerVreg(array<long,2> target_shape)` @ `0x1325cec0`:

```text
packing      = 32 / bitwidth_                          // 1(f32) / 2(bf16) / 4(i8) / 8(i4)
tilesPerVreg = packing * sublane * lane / (tiling_[0] * tiling_[1])   // remainder MUST be 0
```

`CHECK`s (recovered verbatim): `0 != bitwidth` ("bitwidth cannot be 0", `layout.h:245`) and the divisibility guard at `layout.h:250`. For *native* tiling (`t0 = sublane`, `t1 = lane`) the formula collapses to `tilesPerVreg == packing`: one vreg holds `packing` sublane×lane tiles, stacked along the sub-32-bit packing axis. So bf16 native → 2 tiles/vreg, int8 → 4, i4 → 8, f32 → 1. Confidence: Confirmed — the `0 != bitwidth` / `layout.h:245`/`250` CHECK strings are byte-exact in the decompile.

### 2.2 tileArrayShape

`VectorLayout::tileArrayShape(bool, bool, shape, {sublane,lane})` @ `0x14a94160` returns the number of vregs along each dim. Walking the implicit shape, for the trailing two dims:

```text
n_2nd_minor_tiles = ceil_div( offsets_[0].value_or(0) + shape[-2], tiling_[0] )
n_minor_tiles     = ceil_div( offsets_[1].value_or(0) + shape[-1], tiling_[1] * tilesPerVreg )
```

A replicated axis forces its tile-count to 1 (the `*(…)=1` branch in the decompile). Leading dims pass through unchanged; then `implicit_dim` strips the implicit axes (case 1 drops `dim[-1]`; case 2 folds `dim[-1]` into the position of `dim[-2]` then drops; case 3 drops both). `CHECK`s: `layout.cc:410` (`src_shape.size() >= layout_rank()`), `layout.cc:423` (`src_shape.size() >= 2`). Confidence: HIGH — the ceil-div formula and the replicated→1 branch are recovered from the decompiled walker; the exact implicit-dim folding cases are read from the structure but not exhaustively traced per case.

### 2.3 getNativeVregOrVmaskType

`getNativeVregOrVmaskType(elemTy, layout_bitwidth, {sublane,lane})` @ `0x14b766e0` (`vreg_util.cc:58`) produces the concrete MLIR vreg type:

| bitwidth | native vreg type |
|----|----|
| 32 | `vector<sublane × lane × T>` (2-D) |
| < 32 | `vector<sublane × lane × (32/bw) × T>` (3-D — the trailing dim is the packing axis) |
| 1 (`i1`) | a `vmask` (the bitwidth-1 special path) |

`CHECK vreg_util.cc:58`: `bitwidth == layout_bitwidth` (byte-exact in the decompile). After the apply pass every `vector` value is exactly one of these native shapes, so LowerToLLO maps it 1:1 onto an LLO `VregType` ([tpu → LLO ODS](tpu-to-llo-ods.md)). Confidence: Confirmed — the `bitwidth == layout_bitwidth` / `vreg_util.cc:58` CHECK is byte-exact; the type-shape branches follow from the bitwidth dispatch.

---

## 3. applyLayoutOp — the Per-Op Dispatch

`mlir::tpu::applyLayoutOp(ApplyVectorLayoutContext&, Operation&)` @ `0x1325bca0` is the driver invoked once per op by `applyLayoutFunc` @ `0x1325cc80` (which requires a single-region/single-block `FuncOp` — "Expected FuncOp to have a single region/block" — and walks every op). For each op, `applyLayoutOp`:

1. **Read the attached layouts.** `getOutLayouts(op)` then `getInLayouts(op)` read the per-op `out_layout` / `in_layout` array-of-`VectorLayoutAttr` that the inference pass attached. The decompile guards `if (v79 != 1) return 0;` — i.e. it returns early when no out-layouts are present.

2. **Enforce the in==out invariant.** For each vector operand, it `CHECK`s that *operand-is-vector ⇔ in-layout has_value* and that the **producer's out-layout equals this op's in-layout**. On mismatch it emits (byte-exact):

   > `Invariant violation: Input layout does not match output layout - did you forget to run relayout-insertion?`

   This is the architectural seam: inference picks one layout per value, the *separate* relayout-insertion pass bridges disagreements, so the applier never has to reconcile. The check is **skipped** for the ops that may legally change layout — `tpu.truncf`, `tpu.relayout`, `tpu.reshape`/`vector.shape_cast`, `tpu.concatenate`, `vector.extract_strided_slice`, `vector.broadcast`, `arith.trunci`, `arith.extsi`. The same set is exempt from the offset-in-first-tile guard ("Not implemented: Input offsets outside of the first tile", byte-exact in the decompile).

3. **Dispatch by op-name.** Look the op-name up in the lazily-built `rules()` StringMap (`xxh3_64bits` + `StringMapImpl::FindKey`). On hit, call the rule fn-ptr (vtable `+24`). On miss: if the op has `hasElementwiseMappableTraits` → `elementwise_op_rule` @ `0x1325c900`; otherwise emit "Not implemented: Unsupported operation: <op> in apply-vector-layout pass".

> **NOTE — what an apply rule actually does.** Each rule unrolls its op's logical vectors into per-vreg native-shape ops via `disassemble`/`Each<Value>` (it produces an `xla::Array<Value>` of native vregs from §2's math) and emits the concrete sublane/lane shuffles — `tpu.rotate`, `tpu.relayout`, `broadcast_in_sublanes` — that LowerToLLO then consumes 1:1. The rule table *is* the "`tpu`-dialect op → native-vreg op" lowering. The per-rule shuffle *bodies* (e.g. exactly how `tpu_matmul_rule` tiles K, or how `vector_transpose_rule` emits its helpers) are a per-op decompile not done here — LOW.

### 3.1 The 49-entry rule table

Built by `rules()::$_0::operator()` @ `0x1325b100` (49 base entries), then merged with `extensions::rules()` @ `0x13246180` (out-of-tree ops). Op-name → rule fn (in registration order). All 49 op-name strings were confirmed byte-exact in the StringMap's string pool:

| op-name | rule fn | @VA |
|----|----|----|
| `arith.constant` | `arith_constant_rule` | `0x132620a0` |
| `arith.extsi` | `arith_extsi_rule` | `0x13262bc0` |
| `arith.extui` | `arith_extui_rule` | `0x13262ec0` |
| `arith.trunci` | `arith_trunci_rule` | `0x13263780` |
| `func.return` | `func_return_rule` | `0x13263960` |
| `scf.for` | `scf_for_rule` | `0x13263a60` |
| `scf.while` | `scf_while_rule` | `0x13265940` |
| `scf.condition` | `scf_condition_rule` | `0x13266f40` |
| `scf.if` | `scf_if_rule` | `0x13267380` |
| `scf.yield` / `tpu.yield` | `yield_rule` (shared) | `0x13268900` |
| `tpu.rotate` | `tpu_rotate_rule` | `0x13268d40` |
| `tpu.dynamic_rotate` | `tpu_dynamic_rotate_rule` | `0x13269c00` |
| `tpu.concatenate` | `tpu_concatenate_rule` | `0x1326a900` |
| `tpu.pack_elementwise` | `tpu_pack_elementwise_rule` | `0x1326c2a0` |
| `tpu.unpack_elementwise` | `tpu_unpack_elementwise_rule` | `0x1326c420` |
| `tpu.iota` | `tpu_iota_rule` | `0x1326c520` |
| `tpu.gather` | `tpu_gather_rule` | `0x1326d440` |
| `tpu.dynamic_gather` | `tpu_dynamic_gather_rule` | `0x1326df60` |
| `tpu.reduce_index` | `tpu_reduce_index_rule` | `0x1326e860` |
| `tpu.load` | `tpu_load_rule` | `0x13270500` |
| `tpu.store` | `tpu_store_rule` | `0x13270ca0` |
| `tpu.strided_load` | `tpu_strided_load_rule` | `0x13271440` |
| `tpu.strided_store` | `tpu_strided_store_rule` | `0x13271680` |
| `tpu.vector_store` | `tpu_vector_store_rule` | `0x132718e0` |
| `tpu.matmul` | `tpu_matmul_rule` | `0x132727a0` |
| `tpu.region` | `tpu_region_rule` | `0x13274480` |
| `tpu.bitcast` | `tpu_bitcast_rule` | `0x13275040` |
| `tpu.trace` | `tpu_trace_rule` | `0x132755c0` |
| `tpu.assume_layout` | `tpu_assume_layout_rule` | `0x13275840` |
| `tpu.prng_random_bits` | `tpu_prng_random_bits_rule` | `0x13275ec0` |
| `tpu.relayout` | `tpu_relayout_rule` | `0x1325aea0` |
| `tpu.reshape` / `vector.shape_cast` | `reshape_rule` (shared) | `0x13276760` |
| `tpu.fptosi` / `tpu.fptoui` | `tpu_fptoi_rule` (shared) | `0x13278960` |
| `tpu.sitofp` / `tpu.uitofp` | `tpu_itofp_rule` (shared) | `0x13278e80` |
| `tpu.extf` | `tpu_extf_rule` | `0x132792c0` |
| `tpu.truncf` | `tpu_truncf_rule` | `0x13279680` |
| `vector.broadcast` | `vector_broadcast_rule` | `0x13279900` |
| `vector.extract` | `vector_extract_rule` | `0x1327bd00` |
| `tpu.vector_load` | `tpu_vector_load_rule` | `0x1327d020` |
| `vector.multi_reduction` | `vector_multi_reduction_rule` | `0x1327ddc0` |
| `vector.extract_strided_slice` | `vector_extract_strided_slice_rule` | `0x1327f400` |
| `tpu.transpose` | `vector_transpose_rule` | `0x1327f960` |
| `tpu.matmul_push_rhs` | `tpu_matmul_push_rhs_rule` | `0x13281e60` |
| `tpu.matmul_acc_lhs` | `tpu_matmul_acc_lhs_rule` | `0x13282020` |
| `tpu.matmul_pop` | `tpu_matmul_pop_rule` | `0x132821e0` |

(Counting shared fn-ptrs by op-name = 49 entries. "shared" = one rule fn registered under several op-names.) Ops *not* in the table but carrying `hasElementwiseMappableTraits` — `arith.addf`, `arith.mulf`, `arith.select`, `math.exp`, … — route to `elementwise_op_rule`; everything else is "Unsupported operation".

Confidence: Confirmed for the op-name set (all 49 strings byte-exact in the pool) and the dispatch (`applyLayoutOp` + the invariant strings byte-exact). The per-rule VAs are recovered from the StringMap fn-ptr table.

> **GOTCHA — these are NOT the inference rules.** The `applyLayoutOp` table is the *consumer* of layouts. A separate, `PropagationContext`-typed StringMap (`rules()` @ `0x132e15e0`) drives the *memref-tiling propagation* on the producer side, and the per-op `VectorLayoutInferer::infer(...)` dispatch *chooses* the layouts — both documented on [Mosaic Layout Inference](mosaic-layout-inference.md). The memref-side ops (`tpu.memref_slice`, `memref.cast`, `tpu.reinterpret_cast`, …) appear only in that propagation map, never here.

---

## 4. The relayout Driver — Materializing a Layout Change

When two values genuinely need different layouts, the relayout-insertion pass emits a `tpu.relayout` op between them; its apply rule (`tpu_relayout_rule` @ `0x1325aea0`) calls the central change engine. The engine is also called directly by any rule that must reconcile a layout mismatch inside its own lowering.

`mlir::tpu::relayout(ApplyVectorLayoutContext&, OpBuilder&, value, src_layout, dst_layout)` @ `0x1325a480` (`apply_vector_layout.cc:9865`):

1. **Replication-compatibility check.** A logical dim that is non-singleton and replicated in `dst` but *not* in `src` is illegal (you cannot fabricate replicated data from concrete data):

   > `Invalid relayout: Non-singleton logical dimension is replicated in destination but not in source for <v> : <src> -> <dst>`

   (byte-exact in the decompile).

2. **Disassemble.** `disassemble(builder, src_layout, value, {sublane,lane})` @ `0x132466a0` explodes the value into an `xla::Array<Value>` of native vregs (using §2's `tilesPerVreg`/`tileArrayShape`).

3. **Element-type split + three change primitives.** Masks (`i1`) go through `relayoutMasks` @ `0x13258d40`; everything else through `relayoutVregs` @ `0x13257a80`. Both apply, in sequence, the three change primitives:

   | primitive | @VA | what it emits |
   |----|----|----|
   | `changeOffsets` | `0x1324bac0` | lane/sublane shifts — `doRowShiftRelayout` @ `0x13248c80` (sublane), `doColumnShiftRelayout` @ `0x13249d40` (lane) |
   | `changeTiling` | `0x1324c880` | re-tiles vregs to the destination tiling |
   | `changeImplicitDim` | `0x13253b80` | inserts / drops implicit dims |

4. **Post-change assert.** After the change primitives the layout must now equal `dst`; the `CHECK src == dst` at `apply_vector_layout.cc:9865` enforces full reconciliation.

5. **Reassemble.** `assemble(builder, vecTy, dst_layout, vregArray, {sublane,lane})` @ `0x132462c0` rebuilds the relaid-out value.

So a relayout is: *explode to native vregs → lane/sublane shuffle (column/row shift) + re-tile + implicit-dim adjust → reassemble.* The change primitives are the lowest layer of the layout algebra — the actual gather/roll/broadcast register shuffles, expressed as the `changeOffsets`/`changeTiling`/`changeImplicitDim` triple.

Confidence: Confirmed — `relayout`, `disassemble`/`assemble`, `relayoutVregs`/`relayoutMasks`, and all three change primitives (plus `doRowShiftRelayout`) are present as real symbols at the cited VAs; the "Invalid relayout" string is byte-exact. The internal shuffle *codegen* inside each change primitive is symbol-anchored but not body-decompiled here — HIGH.

> **NOTE — the relayout op set, by intent.** The three change primitives map onto three families of register moves: `changeOffsets` → **roll/shift** (lane and sublane rotates that re-align where the first element sits); `changeTiling` → **gather/re-pack** (moving sub-tiles between vregs when the tile shape changes); `changeImplicitDim` → **broadcast/squeeze** (inserting or collapsing a size-1 axis, which on a replicated axis is a broadcast). A layout that differs from its consumer in only one of `{offsets, tiling, implicit_dim}` triggers only the matching primitive; the others are no-ops when `src == dst` along that facet.

---

## 5. Worked Example — a bf16 matmul kernel

A Mosaic kernel `main`, `{sublane = 8, lane = 128}`, lowering `tpu.matmul`:

```mlir
%a : memref<512x256xbf16, #tpu.memory_space<vmem>>
%b : memref<256x128xbf16, #tpu.memory_space<vmem>>
%o : memref<512x128xf32,  #tpu.memory_space<vmem>>
%va = tpu.vector_load %a : vector<512x256xbf16>
%vb = tpu.vector_load %b : vector<256x128xbf16>
%acc = tpu.matmul %va, %vb : vector<512x128xf32>
tpu.vector_store %acc, %o
```

After inference (see [Mosaic Layout Inference](mosaic-layout-inference.md)) the values carry these `VectorLayout`s:

```text
%va  : 16,{0,0},(16,128)     bf16 native: packed sublane tile 16 (= 8 sublanes x 2 packing), lane 128, tilesPerVreg = 2
%vb  : 16,{0,0},(16,128)
%acc : 32,{0,0},(8,128)      f32 native: 8x128, tilesPerVreg = 1
```

Vreg counts via `tileArrayShape` (§2.2):

```text
%va : ceil((0+512)/16) x ceil((0+256)/(128*2)) = 32 x 1 = 32 vregs
%vb : ceil((0+256)/16) x ceil((0+128)/(128*2)) = 16 x 1 = 16 vregs
%acc: ceil((0+512)/8)  x ceil((0+128)/(128*1)) = 64 x 1 = 64 vregs
```

`applyLayoutOp` then walks the block:

- `tpu.vector_load` → `tpu_vector_load_rule` @ `0x1327d020`: unrolls into 32 / 16 per-vreg native loads, each `vector<8x128x2xbf16>` (from `getNativeVregOrVmaskType` for `bw = 16`, §2.3).
- `tpu.matmul` → `tpu_matmul_rule` @ `0x132727a0`: materializes the matmul over native vregs (the MXU latch/matpush/matres sequence is produced downstream by LowerToLLO + the MMA functor).
- `tpu.vector_store` → `tpu_vector_store_rule` @ `0x132718e0`: 64 native `vector<8x128xf32>` stores.

Because every producer-out layout equals the matmul's required operand in-layout (`16,{0,0},(16,128)` for lhs/rhs, `32,{0,0},(8,128)` for acc/result), relayout-insertion adds **no** `tpu.relayout` op — the in==out invariant in `applyLayoutOp` (§3, step 2) holds. If instead a `tpu.transpose` fed the matmul lhs, inference would emit an out-layout that does not match the matmul's lhs in-layout, relayout-insertion would splice a `tpu.relayout` between them, and `tpu_relayout_rule` would drive §4's change primitives to physically re-align the data.

Confidence: HIGH — the layout strings, vreg counts, and rule dispatch follow directly from the byte-exact struct/dispatch math; the worked numbers are derived, not read from a runtime trace.

---

## 6. SparseCore mirror

The decompile also contains a parallel `mlir::mosaic_sc::VectorLayout` value type with its own `parse` @ `0x132fc7c0` / `print` (and a `VectorLayoutAttr` parse/print at `0x132fa0c0`/`0x132f9fa0`), used by the SparseCore layout solver that precedes LowerToMlo. It mirrors the TensorCore `mlir::tpu::VectorLayout` (same `(sublane, lane)` algebra) but is a distinct symbol namespace. The TensorCore path documented above is the one reached by general Pallas/Mosaic kernels; the SparseCore mirror is noted here for completeness and is not decompiled on this page. Confidence: HIGH — the `mosaic_sc::VectorLayout` symbols are confirmed present; their bodies were not traced.

---

## Cross-References

- [Mosaic Overview](mosaic-overview.md) — the import/serde seam, `CustomCallEmitter::Emit`, and the 16-stage `RunMLIRPasses` pipeline that runs `infer-vector-layout` → `apply-vector-layout`.
- [Mosaic Layout Inference](mosaic-layout-inference.md) — the *producer* side: `VectorLayoutInferer` per-op rules that **choose** the `in_/out_layout` attrs this page's applier consumes, the `InferMemRefLayout` memref tiling, the tiling-propagation fixpoint, and the `join`/`generalizes` lattice.
- [The tpu MLIR Dialect: Ops and the Op-Model Contract](tpu-dialect-and-ops.md) — the `tpu`-dialect op definitions whose layouts these rules manipulate.
- [MHLO → XTile → tpu Lowering](mhlo-xtile-tpu-lowering.md) — why general HLO never becomes `tpu` ops (the apply table is reached only on the Mosaic arm).
- [tpu → LLO ODS](tpu-to-llo-ods.md) — the next descent: native vregs produced by `apply-vector-layout` map 1:1 onto LLO `VregType`.
- Binary: `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`) — [back to index](../index.md)
