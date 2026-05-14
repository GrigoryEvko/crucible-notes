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

## tcgen05 Machine Validation

The tcgen05 backend family handles ten matrix-multiply variants plus their
sparse, weight-stationary, block-scale, and scale-input-accumulator forms.
Selection packs the requested shape into a compact control word. The
machine verifier later unpacks the same word and rejects forms the selected
PTX version or SM target cannot execute.

| Control field | Meaning |
|---|---|
| `weight_stationary` | Selects the weight-stationary tcgen05 form. |
| `cta_group` | Selects one-CTA or two-CTA execution groups. |
| `satfinite` | Requests finite saturation where the instruction family supports it. |
| `scale_input_accumulator` | Enables scale-input-accumulator variants. |
| `sparsity` | Selects sparse metadata operands. |
| `scale_vec_size` | Encodes implicit, 1X, 2X, or 4X scale-vector width. |
| `ab_type` | Encodes f16, tf32, i8, mxf8/f6/f4, mxf4/nvf4, or mxf4 operands. |
| `collector_a` | Selects collector-a mode for A operands. |
| `ashift` | Enables the A-shift modifier where legal. |

The verifier is deliberately stricter than the MLIR verifier. It validates
the actual subtarget tuple, the selected family, and the packed modifier
word.

```c
void verify_tcgen05_mma(const Tcgen05MmaInst *inst, const NvptxSubtarget *target) {
    Tcgen05Ctrl ctrl = decode_tcgen05_ctrl(inst->ctrl_word);

    if (ctrl.ab_type == AB_I8) {
        require(target_supports_arch_conditional_tcgen05(target),
                "INT8 type is supported only on arch-conditional variants");
    }

    if (ctrl.sparsity && (ctrl.ab_type == AB_MXF4NVF4 || ctrl.ab_type == AB_MXF4)) {
        require(target_supports_arch_conditional_tcgen05(target),
                "MXF4 sparse variants require an arch-conditional target");
    }

    if (ctrl.scale_vec_size != SCALE_VEC_IMPLICIT) {
        require(target_supports_arch_conditional_tcgen05(target),
                "explicit scale vector size requires an arch-conditional target");
    }

    if (ctrl.scale_input_accumulator) {
        require(target_supports_scale_input_accumulator(target),
                "scale input accumulator is not supported on this architecture");
        require(ctrl.ab_type == AB_F16 || ctrl.ab_type == AB_TF32,
                "scale input accumulator requires f16 or tf32 inputs");
    }

    if (inst->family == TCGEN05_BLOCK_SCALE || inst->family == TCGEN05_SP_BLOCK_SCALE) {
        require(block_scale_allows_ab_type(ctrl.ab_type),
                "block scale does not support this input type");
        require(!ctrl.ashift, "ashift is not supported with block-scale variants");
    }

    require(!(ctrl.weight_stationary && ctrl.cta_group == CTA_GROUP_2),
            "cta_group::2 is not supported with weight-stationary mode");
    require(!(ctrl.weight_stationary && is_fp4_family(ctrl.ab_type)),
            "weight-stationary mode does not support MX or FP4 input families");
    require(!(ctrl.ashift && collector_a_uses_or_fills(ctrl.collector_a)),
            "collector::a use/fill cannot be combined with ashift");
    require(scale_vec_allowed(ctrl.ab_type, ctrl.scale_vec_size),
            "scale vector size is not legal for this input family");
}
```

After validation, tcgen05 lowering assembles the final machine operands
from the selected family. Dense variants carry the normal A/B layouts,
control word, shape, collector state, and accumulator operands. Sparse and
block-scaled variants append metadata and scale planes. The non-negotiable
invariant: selection and MC expansion agree on one packed control-word
schema.

## TMA and Im2Col Validation

The TMA verifier covers global-to-shared tensor loads, shared-to-global
tensor stores, and im2col modes. It decodes rank, mode, multicast, cache
hint, byte class, and two-CTA mode, then selects the concrete machine form
only after the architecture gates pass.

```c
void verify_tma_tensor_op(const TmaTensorInst *inst, const NvptxSubtarget *target) {
    require(1 <= inst->rank && inst->rank <= 5, "TMA rank must be in the range 1..5");

    if (inst->mode == TMA_IM2COL || inst->mode == TMA_IM2COL_W ||
        inst->mode == TMA_IM2COL_W128) {
        require(inst->rank >= 3,
                "im2col tensor copies require at least three dimensions");
    }

    if (inst->mode == TMA_IM2COL_W || inst->mode == TMA_IM2COL_W128) {
        require(target_supports_wide_im2col(target),
                "wide im2col tensor copies are not supported on this architecture");
    }

    if (inst->two_cta) {
        require(target_supports_two_cta_tma(target),
                "two-CTA TMA tensor copies are not supported on this architecture");
    }

    if (inst->multicast) {
        require(target_supports_cluster_multicast(target),
                "cluster multicast TMA requires a compatible SM target");
    }
}
```

The second verifier is what stops stale target-machine state or an illegal
feature string from producing unsupported Blackwell or Hopper instructions.

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

Selector and verifier intentionally report different classes of errors.
The selector rejects targets that cannot support tcgen05 at all; the
verifier rejects instruction-family combinations that become illegal only
after all modifiers, scale modes, sparsity bits, and collector modes have
been packed.
