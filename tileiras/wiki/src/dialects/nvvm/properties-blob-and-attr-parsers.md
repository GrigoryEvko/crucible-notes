# NVVM Properties Blob and Attr Parsers

## Abstract

Every `nvvm.*` op that carries inline-data attributes gets a uniform **Properties** record bump-allocated next to its `Operation*`. The NVVM-to-LLVM lowering dispatcher shares one blob layout across the whole dialect, and five access patterns (A..E) cover every family: [`tcgen05.mma`](tcgen05-ops.md), [`ldmatrix` / `stmatrix`](overview.md#ldmatrix-stmatrix-and-miscellaneous), [`wgmma`](wgmma-ops.md) / `mma.sync`, [`cp.async.bulk` / TMA](tma-ops.md), `atomicrmw` / `red`, the `prefetch` / `fence` / `elect.sync` triad, and block-scaled MMA.

`NVVMDialect::initialize` installs a 67-element enum-attr registrar chain that registers the enum namespaces those patterns consume. The slot tables below are normative — they pin each op mnemonic to its enum / unit / int / array slot positions, so a reimplementer can wire `getAttrOfType<EnumAttr>` (or `OpAdaptor`) to the offsets the upstream dispatcher already reads.

## Properties record layout

Every dispatcher arm opens with the same property fetch. The `Operation*` header's discriminator byte at `op[46]` carries one high bit that selects inline Properties storage versus out-of-line bump-allocator storage. The bit is set for every NVVM op in this binary, so the effective offset is `16` and the fetch collapses to a single pointer load:

```text
props_ptr = *(qword*)(op + 16 * (op[46] >> 7) + 0)        // = +0
```

The first 16 bytes hold an `OperandSegmentSizes` inline buffer for ops with variadic operand groups; the bytes are zero otherwise. `+16..+47` is reserved padding. Attribute slots start at `+64` and march on an 8-byte stride. Each slot is an `Attribute*` — either null (optional attribute absent) or a pointer to an `AttrStorage` header whose `i32` payload sits at `+8`:

```text
+0..+15   OperandSegmentSizes (16 B, inline; zero when op has no segments)
+16..+47  zero-pad / reserved inline storage
+64       slot 0  Attribute*    (8 B)
+72       slot 1  Attribute*
+80       slot 2  Attribute*
+88       slot 3  Attribute*
+96       slot 4  Attribute*
+104      slot 5  Attribute*
+112      slot 6  Attribute*
+120      slot 7  Attribute*
+128      slot 8  Attribute*
+136      slot 9  Attribute*
+144      slot 10 Attribute*
+152      slot 11 Attribute*
+160      slot 12 Attribute*  (rare; only block-scaled MMA reaches here)
```

The biggest record observed reads 13 slots: `nvvm.mma.block_scale` touches `+64..+136` plus `+144`. Every per-arm offset lands on `64 + 8*k` for `k ∈ [0,12]` — no half-pointer storage, no odd alignment. Other observed slot counts: `nvvm.ldmatrix`=4, `tcgen05.mma`=9, `wgmma.mma_async`=12. The out-of-line bump-allocator path goes unused for this NVVM op set; lowering rejects any op whose discriminator says its properties are out-of-line.

## The five access patterns

All 199 dispatcher arms reach into their Properties record through one of five inline-templated helpers. They share the slot-fetch arithmetic from the layout above and differ only in what they do with the slot's `Attribute*` and which payload they pull out.

| Pattern | Attribute kind | Slot read | Stored result | Used for |
|---|---|---|---|---|
| A | `EnumAttr` | load slot pointer, then read the padded `i32` enum payload | `uint32_t` enum | `shape`, `typeA/B/C`, `layout`, `trans`, `eltype`, `scale_in/out`, `kind`, `sparsity`, `cta_group`, `collectorA/B`, `cp_size`, `cache_modifier`, `red_op`, `red_type`, `mem_order` |
| B | `Optional<EnumAttr>` | Pattern A, but null-tolerant | tagged `uint64_t` with present flag plus value | optional `layout`, `trans`, `sparsity` |
| C | `UnitAttr` / `BoolAttr` | test whether the slot pointer is non-null | `bool` | `satfinite`, `transA/B`, `has_write_disable`, `tcgen05.fence` direction, prefetch L2 marker |
| D | `IntegerAttr` | read the `APInt` value | `u32`, or `u64` when active bits exceed 32 | `mask` on `elect.sync`, `cache_level` on `prefetch`, `num` on `ldmatrix` |
| E | `ArrayAttr` | read the first element of the array | first `i32` of array | `kernel` bool, `maxntid` first element |

Pattern A is the workhorse: more than half of every per-arm slot read follows it, because NVVM EnumAttrs uniformly pad their payload to a full 32-bit word at `slot+8` regardless of cardinality. Pattern B's tagged-int return is what feeds the present-flag inspections scattered through the dispatcher. Pattern C never touches the attribute payload at all. Pattern D bottoms out in `APInt::getValue`. Pattern E is the rarest — only the `nvvm.kernel` / `nvvm.maxntid` function-attribute decoders use it.

## Per-op-family Properties slot maps

The 199 dispatcher arms divide into the eight families below. Access patterns reuse the A..E labels from the table above.

### tcgen05.mma family (Blackwell sm_100a / sm_100f, 16 arms)

| Op mnemonic | Slot | Pattern | Field |
|---|---|---|---|
| `nvvm.tcgen05.mma` | +64 / +72 / +80 / +88 | A / A / A / A | typeA/cType, collectorA, scale_d, layout-bits |
| `nvvm.tcgen05.mma.block_scale` | +64 / +72 / +80 / +88 | A / A / A / A | cType, collectorA, scale_d, layout, kindA, kindB |
| `nvvm.tcgen05.mma.sp` | +64 / +72 / +80 / +88 | A / A / A / A | same as `tcgen05.mma` plus the metadata operand slot |
| `nvvm.tcgen05.mma.ws` | — | — | operand-only |
| `nvvm.tcgen05.mma.ws.sp` | — | — | operand-only |
| `nvvm.tcgen05.mma.sp.block_scale` | +64 / +72 / +80 / +88 | A / A / A / A | merge of `sp` and `block_scale` fields |
| `nvvm.tcgen05.shift` | — | — | operand-only |
| `nvvm.tcgen05.commit` | +64 | A | cta_group |
| `nvvm.tcgen05.commit.arrive` | +64 | A | cta_group |
| `nvvm.tcgen05.cp` | +64 / +72 / +80 | A / A / A | multicast, shape, src_fmt |
| `nvvm.tcgen05.alloc` | +64 | A | cta_group |
| `nvvm.tcgen05.dealloc` | +64 | A | cta_group |
| `nvvm.tcgen05.relinquish_alloc_permit` | +64 | A | cta_group |
| `nvvm.tcgen05.wait` | +64 | A | wait_kind (`load` or `store`) |
| `nvvm.tcgen05.fence` | +64 | C | fence-kind marker |
| `nvvm.tcgen05.{ld,st}matrix` | — | — | indexed-operand walker only |

`tcgen05.mma` is the only family where the first 16 Properties bytes aren't idle. The op carries a variable-arity operand list, so the dispatcher reserves `+0..+15` for a packed `OperandSegmentSizes` buffer plus a second 16-byte running-offset buffer at `+96..+111`.

### ldmatrix / stmatrix (Volta+ tensor-core fragment ops, 3 arms)

| Op mnemonic | Slot | Pattern | Field |
|---|---|---|---|
| `nvvm.ldmatrix` | +64 / +72 / +80 / +88 | A / D / A / A | eltype/size, `num`, shape, trans |
| `nvvm.stmatrix` | +64 / +72 | A / A | shape, trans; `num` is the SSA-vector cardinality, not a property |
| `nvvm.stmatrix` alternate selector | +64 | A | trans encoded as a 0/1 enum |

The alternate stmatrix selector disambiguates intrinsic variants from the `trans` enum alone. It fires when the operand vector matches the narrower selector shape.

### wgmma / mma.sync (Hopper sm_90a, 4 arms)

| Op mnemonic | Slot | Pattern | Field |
|---|---|---|---|
| `nvvm.wgmma.mma_async` | +64 / +72 / +80 / +88 / +96 / +112 / +120 / +128 / +136 | A × 8, D × 1 | typeA, b1Op, typeB, shape, typeC, scaleIn, scaleOut, layoutA, layoutB |
| `nvvm.wgmma.commit_group_sync_aligned` | +64 / +72 | A / A | wgmma_type, wgmma_layout |
| `nvvm.wgmma.wait_group_sync_aligned` | +64 / +72 / +88 | A / A / A | type, layout, shape-N selector |
| `nvvm.mma.sync` | +64 / +72 / +80 / +88 / +96 / +104 | A × 6 | b1Op, multiplicandAPtxType, layoutA, layoutB, multiplicandBPtxType, intOverflowBehavior |
| `nvvm.wmma` family | — | — | operand-only; eltype/k/m/n/layout are baked into the resolved intrinsic name at build time |

### cp.async.bulk / TMA (Hopper+ sm_90a / Blackwell sm_100, 8 arms)

| Op mnemonic | Slot | Pattern | Field |
|---|---|---|---|
| `nvvm.cp.async.bulk.tensor.reduce` | +64 / +72 + rank-dependent slot | C / C / A | multicast presence, cache-hint, reduce_kind |
| `nvvm.cp.async.bulk.tensor.prefetch` | +64 / +72 | A / C | im2col-type, multicast |
| `nvvm.cp.async.bulk.tensor.shared.cta.to.global` | — | — | operand-only |
| `nvvm.cp.async.bulk.tensor.shared.cta.to.global.ext` | — | — | im2col / cache-hint operands |
| `nvvm.cp.async.bulk.tensor.shared.cluster.to.global` | — | — | operand-only |
| `nvvm.cp.async.bulk.tensor.base` | +64 / +72 / +80 | C / C / C | has_im2col, has_multicast, has_cache_hint |
| `nvvm.cp.async.shared.*.global` | +64 / +80 / +72 | A / A / C | cp_size, ca/cg cache modifier, L2-hint presence |
| `nvvm.cp.async.commit_group` | +64 | A | ca/cg modifier |

### atomicrmw / red (sm_60+, 5 arms)

| Op mnemonic | Slot | Pattern | Field |
|---|---|---|---|
| `nvvm.atomicrmw` | +64 / +72 | A / A | mem_order, atomic_op |
| `nvvm.red` variant 1 | +64 / +72 | A / A | red_op, red_type |
| `nvvm.red` variant 2 | +64 / +72 | A / A | red_op, red_type |
| `nvvm.red` variant 3 | +64 / +72 | A / A | red_op, red_type |
| `nvvm.atomic.cas` / `nvvm.red.b128` | +64 / +72 / +80 / +88 | A / A / A / A | four enum slots |

### prefetch / fence / elect.sync (5 arms)

| Op mnemonic | Slot | Pattern | Field |
|---|---|---|---|
| `nvvm.tcgen05.fence` before | +64 | C | `before` unit marker |
| `nvvm.tcgen05.fence` after | +64 | C | `after` unit marker |
| `nvvm.elect.sync` | +64 | D | mask |
| `nvvm.prefetch` / `nvvm.prefetch.tensormap` | +64 / +72 / +80 / +88 / +96 | D / C / A / A / A | cache_level, L2 marker, to-tensormap flag, evict-priority, prefetch-mode |
| `nvvm.cvt.packfloat.f32` | helper-decoded | A | property-decoded and emitted through a helper |

### Block-scaled MMA (`nvvm.mma.block_scale`, sm_100a, 1 arm)

| Op mnemonic | Slot | Pattern | Field |
|---|---|---|---|
| `nvvm.mma.block_scale` | +64 / +72 / +80 / +88 / +96 / +112 / +120 / +128 / +136 | A × 9 | typeA, b1Op, typeB, shape, typeC, scaleAFmt, scaleBFmt, scale_vec, layoutA |

Block-scaled MMA reuses the `wgmma.mma_async` prologue shape but rewires the slots: `+128` swaps `layoutA` for `scale_vec`, and `+112`/`+120` swap `scaleIn`/`scaleOut` for `scaleAFmt`/`scaleBFmt`. The slot index, not the byte offset, is the canonical identifier.

## The 67-element enum-attr registrar chain

`NVVMDialect::initialize` installs 68 attribute registrars. Sixty-seven are single-namespace `EnumAttr` registrars; the sixty-eighth is the `NVVMTargetAttr` registrar carrying `chip`, `features`, `link-files`, and `flags`. Every enum registrar has the same shape — assemble an attribute-class definition tuple, add it to the dialect, attach the printer/parser pair to the attribute-name table.

The 67 namespaces cover every enum-typed Properties slot read by the
dispatcher. Grouped by family, the chain registers cache / memory hints
(`cache_eviction_priority`, `load_cache_modifier`,
`load_cache_modifier_ext`, `store_cache_modifier`, `l2_prefetch`,
`evict_kind`, `prefetch_cache_level`); address spaces and scopes
(`state_space`, `shared_space`, `mem_scope`, `mbar_scope`,
`mbar_space`); memory ordering and fences (`mem_order`, `proxy_kind`,
`action`, `tcgen05_fence`, `tcgen05_wait`); warp-level collectives
(`shfl_kind`, `vote_sync_kind`, `match_sync_kind`, `redux_kind`,
`barrier_redux_kind`); mbarrier / FP / cvt (`mbar_txn_kind`,
`mbar_wait`, `fp_rnd_mode`, `sat_mode`, `rnd`, `sat`,
`convert_fp4_type`, `convert_fp6_type`, `convert_fp8_type`,
`packfloat_type`); MMA / WMMA / WGMMA (`shape`, `mma_layout`,
`mma_type`, `mma_frag`, `mma_b1op`, `mma_int_overflow`,
`mma_cta_count`, `sparsity_format`, `load_shape`, `store_shape`,
`load_src_format`, `wgmma_scale_in`, `wgmma_scale_out`, `wgmma_type`);
block-scaled and tcgen05 (`scale_vec_size`, `block_scale_format`,
`tcgen05_mma_kind`, `tcgen05_mma_collectorop`,
`tcgen05_mma_scale_vec`, `tcgen05_mma_collectorb`, `TmemLayout`,
`TCBarParam`, `tcgen05_group`, `tcgen05_cp_shape`,
`tcgen05_cp_multicast`, `tcgen05_cp_src_fmt`, `tcgen05_ldst_shape`,
`load_mode`); TMA / atomic / reduction (`tma_store_mode`,
`tma_redux_kind`, `red_op`, `red_type`, `mul_mode`, `atomic_op`,
`dot_accumulate_type`).

These namespaces are exactly the enums whose `i32` payloads Pattern A pulls from the slot trailers above. The chain only registers parse-side machinery; constant materialization is a later lowering concern. During the NVVM-to-LLVM rewrite, any enum payload that needs to become an SSA constant materializes as `llvm.mlir.constant %c : i32`. For inline-asm slots that bypass the intrinsic table, see [NVVM Overview — Inline-PTX Templates and Constraint Strings](overview.md#inline-ptx-templates-and-constraint-strings).

## Reimplementation Notes

A clean implementation drives off a generated slot schema, not a hand-written switch on every op:

```text
for op in nvvm_ops:
    props = read_inline_properties(op)
    schema = schema_for(op.name)

    for field in schema.fields:
        slot = props.slots[field.index]
        value = decode(slot, field.pattern)
        emit_lowering_operand_or_intrinsic_selector(field.name, value)
```

The invariants are small. Properties are inline for this op set. Slots start at byte 64 and advance by one pointer. Enum attributes decode through padded 32-bit payloads. Optional enum attributes carry a presence bit separate from the value.
