# tcgen05 / WGMMA / mbarrier / Cluster Emission

## Abstract

Blackwell `tcgen05` matrix multiply, Hopper WGMMA, transactional mbarriers,
and cluster-scope synchronization all enter through MLIR `nvvm.*` or
`nvgpu.*` operations. None of them become ordinary PTX strings immediately.
They pass through feature checks, operand packing, target-specific
MachineInstr construction, and finally PTX printing.

The central reimplementation idea is two-stage validation. The MLIR
verifier checks the operation shape visible at the dialect level. The
backend validates the final selected machine form again, because
arch-conditional tcgen05 variants, TMA modes, cluster scope, and mbarrier
transactions depend on subtarget details that are fully known only after
target selection.

For the structural model behind each family see
[tcgen05 Tensor Memory Model](../topics/tcgen05-tensor-memory-model.md),
[WGMMA Emission Protocol](../topics/wgmma-emission-protocol.md),
[mbarrier State Machine](../topics/mbarrier-state-machine.md), and
[Cluster Sync and DSMEM Handshake](../topics/cluster-sync-and-dsmem-handshake.md).
This page covers the backend-side validation and PTX-emission detail those
topic pages defer here.

## tcgen05 Machine Validation

The tcgen05 backend family handles ten matrix-multiply variants plus their sparse, weight-stationary, block-scale, and scale-input-accumulator forms. Selection packs the requested shape into a compact control word. The machine verifier later unpacks the same word and rejects forms the selected PTX version or SM target cannot execute.

### Control-Word Bit Layout

Two packed 32-bit words travel through tcgen05 lowering: the primary control word that records shape, kind, and CTA grouping, and a smaller collector word that records collector mode and ashift. Both fields are read by the selector to pick a machine opcode and by the verifier to reject illegal combinations. The bit ranges are stable across the dense, sparse, and weight-stationary families.

| Bits | Field | Width | Encoding |
|---:|---|---:|---|
| 0-1 | `cta_group` | 2 | 0 = single-CTA, 1 = 2-CTA, 2 = 4-CTA, 3 = reserved |
| 2-3 | `scale_vec_size` | 2 | 00 = implicit, 01 = 1X, 10 = 2X, 11 = 4X |
| 4 | `scale_input_acc` | 1 | enables scale-input-accumulator path |
| 5 | `block_scale` | 1 | selects the block-scale variants |
| 6-8 | `mma_kind` | 3 | one of seven kind values for the dense-MMA family |
| 9-31 | reserved | 23 | must be zero; the verifier rejects any non-zero bit |

The seven `mma_kind` values cover `f16` / `tf32` / `i8` / `f8f6f4` / `mxf8f6f4` / `mxf4` / `mxf4nvf4`. The selector reads the abstract MMA kind from the SDNode operand and maps to this enum before packing.

A separate collector word carries the operand-A modifiers:

| Bits | Field | Width | Encoding |
|---:|---|---:|---|
| 0 | `collector_a_valid` | 1 | distinguishes the explicit collector path from the default |
| 1-2 | `collector_a` | 2 | 0 = fill, 1 = use, 2 = fill+use, 3 = reserved |
| 2 | `ashift` | 1 | enables the A-shift modifier (overlaps with the high bit of `collector_a`) |

The overlap on bit 2 is deliberate: the encoder treats `ashift` and the `collector_a` "fill+use" mode as mutually exclusive, so a single byte position carries both with the verifier rejecting any combination that would set them at once. The remaining bits stay reserved and must be zero on entry to the verifier.

Each `Tcgen05MmaInst` carries one control word and one collector word side by side in its operand list. The packing layout matches the bit ranges:

```c
typedef union Tcgen05Ctrl {
    uint32_t raw;
    struct {
        uint32_t cta_group       : 2;   /* bits 0-1   */
        uint32_t scale_vec_size  : 2;   /* bits 2-3   */
        uint32_t scale_input_acc : 1;   /* bit  4     */
        uint32_t block_scale     : 1;   /* bit  5     */
        uint32_t mma_kind        : 3;   /* bits 6-8   */
        uint32_t reserved        : 23;  /* bits 9-31  */
    };
} Tcgen05Ctrl;

typedef union Tcgen05Collector {
    uint32_t raw;
    struct {
        uint32_t valid           : 1;   /* bit  0     */
        uint32_t collector_a     : 2;   /* bits 1-2   */
        /* ashift overlays bit 2; encoder rejects the conflicting combination */
        uint32_t reserved        : 29;  /* bits 3-31  */
    };
} Tcgen05Collector;
```

A reimplementation that mirrors the binary layout must mask the reserved fields explicitly. Selection sometimes leaves uninitialized scratch bits in the upper half of the SDNode operand, and the verifier reads the full 32-bit word.

### Subtarget Feature Probe

The verifier validates against the subtarget feature bitmap, not against an opaque target descriptor. Each tcgen05 capability the verifier needs corresponds to a single bit in the bitmap: `has_tmem` (datacenter Blackwell has the tensor-memory storage), `has_wgmma` (Hopper warp-group MMA is reachable), `has_arch_conditional` (sm_*a-suffixed variants are allowed), `has_family_conditional` (sm_*f-suffixed variants are allowed), and `has_scale_input_accumulator` (the SIA variant is implemented in hardware). The selector reads `subtarget.features` once per intrinsic; the verifier reads the same bitmap immediately after the packed control word lands in the machine operand list.

The bitmap is the same one consulted by the MatcherTable predicate row (see [iseldag-and-matchertable.md](iseldag-and-matchertable.md) for the 507-byte stride matrix probe). The contract is: every feature gate the verifier rejects is also reachable as a MatcherTable predicate, so a fall-through from custom selection to the MatcherTable cannot accidentally produce an opcode that fails the verifier later.

### Verifier Rules

The verifier is deliberately stricter than the MLIR verifier. It validates the actual subtarget tuple, the selected family, and the packed modifier word. The rules below operate on the decoded control and collector words.

```c
void verify_tcgen05_mma(const Tcgen05MmaInst *inst, const NvptxSubtarget *target) {
    Tcgen05Ctrl       ctrl = decode_tcgen05_ctrl(inst->ctrl_word);
    Tcgen05Collector  coll = decode_tcgen05_collector(inst->collector_word);
    SubtargetFeatures sf   = target->features;

    /* INT8 inputs require arch-conditional tcgen05. */
    if (ctrl.mma_kind == TCGEN05_KIND_I8 && !sf.has_arch_conditional)
        diag("INT8 type is supported only on arch-conditional variants");

    /* MXF4 sparse variants require arch-conditional tcgen05. */
    if (inst->sparse && (ctrl.mma_kind == TCGEN05_KIND_MXF4NVF4
                         || ctrl.mma_kind == TCGEN05_KIND_MXF4)
                    && !sf.has_arch_conditional)
        diag("MXF4 sparse variants require an arch-conditional target");

    /* Explicit scale-vector size requires arch-conditional tcgen05. */
    if (ctrl.scale_vec_size != SCALE_VEC_IMPLICIT && !sf.has_arch_conditional)
        diag("explicit scale vector size requires an arch-conditional target");

    /* Scale-input-accumulator requires a hardware feature and f16/tf32 inputs. */
    if (ctrl.scale_input_acc) {
        if (!sf.has_scale_input_accumulator)
            diag("scale input accumulator is not supported on this architecture");
        if (ctrl.mma_kind != TCGEN05_KIND_F16 && ctrl.mma_kind != TCGEN05_KIND_TF32)
            diag("scale input accumulator requires f16 or tf32 inputs");
    }

    /* Block-scale-only restrictions. */
    if (ctrl.block_scale) {
        if (!block_scale_allows_kind(ctrl.mma_kind))
            diag("block scale does not support this input type");
        if (coll.valid && coll.collector_a /* ashift overlay */ == COLLECTOR_ASHIFT)
            diag("ashift is not supported with block-scale variants");
    }

    /* Cross-field invariants. */
    if (inst->weight_stationary && ctrl.cta_group == CTA_GROUP_2)
        diag("cta_group::2 is not supported with weight-stationary mode");
    if (inst->weight_stationary && is_fp4_kind(ctrl.mma_kind))
        diag("weight-stationary mode does not support MX or FP4 input families");
    if (coll.valid && coll.collector_a == COLLECTOR_FILL_USE
                   && coll.collector_a == COLLECTOR_ASHIFT)
        diag("collector::a use/fill cannot be combined with ashift");
    if (!scale_vec_allowed(ctrl.mma_kind, ctrl.scale_vec_size))
        diag("scale vector size is not legal for this input family");
}
```

After validation, tcgen05 lowering assembles the final machine operands from the selected family. Dense variants carry the normal A/B layouts, control word, shape, collector state, and accumulator operands. Sparse and block-scaled variants append metadata and scale planes. The non-negotiable invariant: selection and MC expansion agree on one packed control-word schema.

## TMA and Im2Col Validation

The TMA verifier covers global-to-shared tensor loads, shared-to-global tensor stores, and im2col modes. It decodes rank, mode, multicast, cache hint, byte class, and two-CTA mode, then selects the concrete machine form only after the architecture gates pass.

The verifier consults the same subtarget feature bitmap the tcgen05 verifier uses. Three bits matter here: `has_wide_im2col` (Hopper supports the W and W128 wide variants), `has_two_cta_tma` (the 2-CTA TMA instruction surface), and `has_cluster_multicast` (the `multicast::cluster` modifier on TMA copies). The verifier reads the bitmap once and rejects the instruction at the first mismatched gate.

```c
void verify_tma_tensor_op(const TmaTensorInst *inst, const NvptxSubtarget *target) {
    SubtargetFeatures sf = target->features;

    if (inst->rank < 1 || inst->rank > 5)
        diag("TMA rank must be in the range 1..5");

    if (inst->mode == TMA_IM2COL
        || inst->mode == TMA_IM2COL_W
        || inst->mode == TMA_IM2COL_W128) {
        if (inst->rank < 3)
            diag("im2col tensor copies require at least three dimensions");
    }

    if ((inst->mode == TMA_IM2COL_W || inst->mode == TMA_IM2COL_W128)
        && !sf.has_wide_im2col)
        diag("wide im2col tensor copies are not supported on this architecture");

    if (inst->two_cta && !sf.has_two_cta_tma)
        diag("two-CTA TMA tensor copies are not supported on this architecture");

    if (inst->multicast && !sf.has_cluster_multicast)
        diag("cluster multicast TMA requires a compatible SM target");
}
```

The second verifier is what stops stale target-machine state or an illegal feature string from producing unsupported Blackwell or Hopper instructions.

## WGMMA Emission

Hopper WGMMA lowering turns `nvgpu.warpgroup.mma` into the standard
four-part protocol: fence, one or more async MMA instructions, commit,
wait. Descriptor offsets are expressed in 16-byte units, so every tile
step divides the byte offset by 16 before updating the shared-memory
descriptors.

```c
void lower_wgmma(WgmmaOp op, Rewriter *rewriter) {
    emit_nvvm_wgmma_fence_aligned(rewriter);

    for (int m_tile = 0; m_tile < op.m / op.inst_m; ++m_tile) {
        for (int k_tile = 0; k_tile < op.k / op.inst_k; ++k_tile) {
            uint64_t a_desc = advance_smem_desc(op.a_desc, m_tile, k_tile, op.a_layout);
            uint64_t b_desc = advance_smem_desc(op.b_desc, m_tile, k_tile, op.b_layout);

            emit_nvvm_wgmma_mma_async(rewriter, op, a_desc, b_desc);
        }
    }

    emit_nvvm_wgmma_commit_group_sync_aligned(rewriter);
    emit_nvvm_wgmma_wait_group_sync_aligned(rewriter, 0);
}

uint64_t advance_smem_desc(uint64_t desc, int m_tile, int k_tile, WgmmaLayout layout) {
    uint64_t byte_offset = layout_byte_offset(layout, m_tile, k_tile);
    return desc + (byte_offset >> 4);
}
```

Operand-B type inference feeds the PTX descriptor form. Bit-level operands
take the smallest selector class; i4/i8/u8 take the byte-class path;
f16/bf16/tf32/f8 take the half/float class; sparse selectors take the
extended selector form.

## mbarrier Emission

The mbarrier phase protocol coordinates TMA-load completion, WGMMA commit,
and tcgen05 producer/consumer handoff. The finalizer computes the expected
transaction count, emits an initialization fence on SM90 and newer targets,
invalidates the barrier when the enclosing scope requires it, then pairs
that invalidation with a cluster-release fence.

| mbarrier field | Purpose |
|---|---|
| `smem_base` | Shared-memory address of the barrier object. |
| `kind` | Distinguishes ordinary barriers from TMA transaction barriers. |
| `phase` | Tracks parity / phase for wait operations. |
| `expected_txn` | Number of expected transaction completions. |
| `arrive_count` | Arrival count used by the producer side. |
| `tag` | Pipeline bookkeeping tag. |

```c
void finalize_mbarrier_phase(MBarrierHandle *barrier, PhaseContext ctx) {
    if (ctx.sm >= 90) {
        emit_nvvm_fence_mbarrier_init();
    }

    barrier->expected_txn = barrier->kind == MBARRIER_TMA ? 32 * ctx.size_minor : 1;

    if (ctx.requires_shared_invalidation) {
        emit_nvvm_mbarrier_inval_shared(barrier->smem_base);
    }

    emit_fence_mbarrier_init_release_cluster();
}
```

## Cluster Sync Emission

Cluster synchronization passes through three gates: target must be SM90 or
newer, launch must actually use more than one CTA per cluster, and the
Tileiras barrier scope must request cluster behavior. Single-CTA clusters
fall back to ordinary `nvvm.barrier`; multi-CTA clusters take the
arrive/wait pair.

```c
void emit_cluster_sync(ClusterSyncRequest req, Rewriter *rewriter) {
    if (req.sm < 90 || req.cluster_size == 1 || req.scope == BARRIER_SCOPE_CTA) {
        emit_nvvm_barrier(rewriter);
        return;
    }

    emit_nvvm_fence_mbarrier_init(rewriter);
    emit_nvvm_cluster_arrive_relaxed(rewriter, req.aligned);
    emit_nvvm_cluster_wait(rewriter, req.aligned);
}
```

Two-CTA Blackwell tensor-memory paths also read the cluster rank special
register. For paired CTAs, `cluster.ctarank ^ 1` selects the peer CTA.

## End-To-End Lowering

The tcgen05 path is a closed pipeline. The selector chooses a candidate
machine family from the intrinsic and subtarget. The machine verifier
rechecks the packed control word. The builder then materializes the
MachineInstr the asm printer will later render as PTX.

```c
MachineInstr *lower_tcgen05_mma(IntrinsicInst *intrin, const NvptxSubtarget *target) {
    Tcgen05MmaInst inst = select_tcgen05_candidate(intrin, target);

    verify_tcgen05_mma(&inst, target);

    MachineOperand operands[MAX_TCGEN05_OPERANDS];
    int num_operands = build_tcgen05_operands(&inst, operands);

    return build_machine_instr(inst.machine_opcode, operands, num_operands);
}
```

Selector and verifier intentionally report different classes of errors. The selector rejects targets that cannot support tcgen05 at all; the verifier rejects instruction-family combinations that become illegal only after all modifiers, scale modes, sparsity bits, and collector modes have been packed.

## Cross-References

[per-sm-emission-templates.md](per-sm-emission-templates.md) documents the actual PTX text the printer emits for `tcgen05.mma` and WGMMA, including the four-part WGMMA protocol and the worked WGMMA descriptor hex round-trip. [iseldag-and-matchertable.md](iseldag-and-matchertable.md) documents the selector dispatcher that lands on these emitters and the MatcherTable predicate-row probe that shares the subtarget feature bitmap with the verifier. [tma-tensormap-and-cp-async-bulk.md](tma-tensormap-and-cp-async-bulk.md) covers the descriptor encoder for `cp.async.bulk.tensor` that the TMA verifier sits in front of.
