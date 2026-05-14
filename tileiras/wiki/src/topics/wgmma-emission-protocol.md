# WGMMA Emission Protocol

## Abstract

WGMMA is Hopper's asynchronous warp-group matrix multiply. Four warps cooperate on one accumulator tile; the multiply itself is asynchronous against the issuing warp group and only becomes visible to subsequent reads through a wait-group barrier. The legal usage contract is a four-op emission protocol — fence, one or more async MMA instructions, commit-group, wait-group — and an accumulator-lifetime contract that says: an accumulator written by a still-in-flight WGMMA cannot be read until its group has been drained. Violations are silent data races, not verifier errors.

This page is the canonical reference for the protocol. It supersedes the duplicated lower-WGMMA snippets in `codegen/tcgen05-wgmma-mbarrier-cluster.md`, `lowering/nvgpu-and-gpu-to-nvvm.md`, `dialects/nvgpu/overview.md`, and `dialects/cute_nvgpu/mma-atoms-sm70-120.md`. Those pages now defer here for the emission sequence and the lifetime contract; they keep their own descriptor-construction, dialect-pattern, and verifier content.

WGMMA exists only on `sm_90a`. Blackwell removes it: SM100 onwards uses `tcgen05.mma` over tensor memory instead.

## The Four-Op Sequence

A WGMMA region emits exactly one fence, one tile loop of MMA instructions, one commit, and one wait. The fence orders prior shared-memory writes against the first async MMA; the commit closes the current async group; the wait drains the group's accumulator results back into the warp group's visible state.

```mlir
nvvm.wgmma.fence.aligned                                  // 1. fence
%acc1 = nvvm.wgmma.mma_async  %a0, %b0, %acc0             // 2. async MMA, tile 0
%acc2 = nvvm.wgmma.mma_async  %a1, %b1, %acc1             //    async MMA, tile 1
...
%accN = nvvm.wgmma.mma_async  %ak, %bk, %accN-1           //    async MMA, tile K-1
nvvm.wgmma.commit.group.sync.aligned                      // 3. commit
nvvm.wgmma.wait.group.sync.aligned %waitN                 // 4. wait
```

```c
void emit_wgmma_region(WgmmaOp op, Rewriter *rw, int wait_n) {
    rw->create("nvvm.wgmma.fence.aligned");

    Value acc = op.accumulator();
    for (int m = 0; m < op.m / op.inst_m; ++m) {
        for (int k = 0; k < op.k / op.inst_k; ++k) {
            uint64_t da = advance_descriptor(op.a_desc, m, k, op.a_layout);
            uint64_t db = advance_descriptor(op.b_desc, m, k, op.b_layout);
            acc = rw->create("nvvm.wgmma.mma_async", {da, db, acc}, acc.getType());
        }
    }

    rw->create("nvvm.wgmma.commit.group.sync.aligned");
    rw->create("nvvm.wgmma.wait.group.sync.aligned", {rw->i32(wait_n)});
    rw->replace_op(op, acc);
}
```

The fence/commit/wait triple is non-negotiable. Skipping the fence races SMEM stores against the first async MMA. Skipping the commit means the wait drains the wrong group (a different in-flight group, or none at all). Skipping the wait reads stale or partial accumulator state.

## Accumulator Lifetime

The accumulator returned by each `mma_async` is symbolic: the SSA value is defined, but its register contents are not yet visible to the warp group. Reads of that SSA value before its group has been drained by `wait_group` are silent data-race UB — the hardware does not trap, the MLIR verifier does not flag, and the result depends on the timing of the warp scheduler.

Two rules cover this:

1. Any read of an accumulator written by an `mma_async` must follow a `wait_group` that drains that MMA's group.
2. A `wait_group N` drains every group whose commit predates the wait by more than `N` commits.

The second rule is the source of the most common subtle bug. `wait_group N` is "the number of groups still in flight *after* this wait, not the number to wait for." `wait_group 0` is the drain-everything case, and it is what most pipelined kernels emit at the tail of the WGMMA region.

A useful mental model: `commit_group` closes the current group and increments an in-flight counter. `wait_group N` blocks until the in-flight counter is at most `N`, then returns. Counter monotonicity means the wait drains every group older than the current cohort of `N`.

## SMEM Descriptor Advancement

Operand B is always an SMEM descriptor — a packed 64-bit word whose `start_addr` field carries the low 14 bits of the SMEM byte offset right-shifted by 4. WGMMA requires 16-byte-aligned SMEM addresses; the constructor stores `(smem_offset >> 4)` rather than the unshifted byte offset.

When the WGMMA region iterates over output tiles, descriptors must advance by the per-tile byte stride converted to 16-byte units:

```c
uint64_t advance_descriptor(uint64_t desc, int m_tile, int k_tile, Layout layout) {
    uint64_t byte_offset = layout_byte_offset(layout, m_tile, k_tile);
    return desc + (byte_offset >> 4);
}
```

A reimplementation that forgets the `>> 4` advances the descriptor 16x too far in the first tile and silently aliases distant SMEM regions on subsequent tiles. The verifier does not catch it because the descriptor field is opaque from the dialect's point of view.

Operand A may be either a register fragment or an SMEM descriptor, controlled by a per-atom `a_in_rf` predicate. When A rides registers, the descriptor advancement applies only to B; when A rides SMEM, both operands advance using their own layouts.

## Inline-Asm Template and Constraint String

For SM90 WGMMA atoms that bypass the NVVM op and emit PTX directly, the inline-asm template carries the constraint string `=f,=r,l,r,n` in argument order:

| Constraint | Operand | Role |
|---|---|---|
| `=f` | output | each FP register in the accumulator fragment |
| `=r` | output | the i32 register that captures the scale-D return |
| `l` | input | the i64 descriptor input (operand B, or A if SMEM-resident) |
| `r` | input | the i32 scale input that toggles accumulator update |
| `n` | input | the compile-time-known predicate that conditions the MMA |

The `=f` block expands to as many lanes as the accumulator fragment carries — `M * N / 256` per thread for FP32 accumulators, varying by atom. The `l` slot carries the WGMMA descriptor word the SMEM-descriptor constructor produced; when A is also SMEM-resident, a second `l` input precedes it.

```text
wgmma.mma_async.sync.aligned.m64nXkY.<acc>.<a>.<b>
    { %f0, %f1, ... },                       // accumulator fragment (out)
    %ra,                                     // A operand (descriptor or RF)
    %rb,                                     // B descriptor
    %scale,                                  // scale-D selector
    1, 1,                                    // transpose flags (compile-time)
    %la, %lb                                 // SMEM descriptors when A in SMEM
```

## Scale-D

The scale-D operand is a single boolean: 0 means "zero the accumulator before adding the MMA result", 1 means "add to the existing accumulator". The dialect-side WgmmaOp exposes it through a `scale_d` attribute; the lowering routes it into the `r` input of the inline-asm template.

The mainloop pattern is to issue the first WGMMA with `scale_d = 0` (zeroing the tile) and every subsequent K iteration with `scale_d = 1` (accumulating). Forgetting to clear scale-D on the leading WGMMA does not zero the accumulator; instead, the kernel multiplies into whatever values the destination registers happened to hold at warp-group start — usually garbage.

## Operand Residency

Operand B is always an SMEM descriptor. There is no register-resident-B WGMMA variant. The descriptor encodes both the SMEM base address (low 14 bits, in 16-byte units) and the leading/stride byte offsets that pin the 2D tile shape into SMEM.

Operand A is one of two residencies:
- A register fragment, when the producing pipeline has staged A into the warp group's registers (typical for warp-specialized mainloops where A is small and stays close to the MMA).
- An SMEM descriptor, with the same construction rules as operand B (used when A is large enough to want SMEM staging or when the producer is a TMA load).

The accumulator stays in registers in every WGMMA variant. The destination is the warp group's register file; that is also why each `mma_async` returns a typed accumulator SSA value the rest of the IR can thread through subsequent MMAs in the same group.

## SM Gating

WGMMA is `sm_90a` only. The architecture-conditional suffix matters: plain `sm_90` rejects WGMMA at NVVM verification. The dialect exposes WGMMA atoms through `cute_nvgpu.sm90.mma` and lowering rejects them on every other target.

Blackwell removes WGMMA. SM100 and SM103 use `tcgen05.mma` over tensor memory; SM120 and SM121 (consumer Blackwell) use a synchronous `mma.sync.aligned` with explicit per-operand scale factors. Both replacements have different operand-residency models — see the matmul-progression page for the cross-architecture story.

## Cross-References

[Matmul Progression by SM](matmul-progression-by-sm.md) places WGMMA in the broader SM70-to-SM121 lineage and explains what replaced it on each generation.
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) is the Blackwell successor; the 4-op protocol changes because the accumulator now lives in TMEM.
[mbarrier State Machine](mbarrier-state-machine.md) defines the transaction-barrier kind that producers use to publish WGMMA completion when a downstream pipeline stage needs to observe it.
[MMA Atoms SM70-SM120](../dialects/cute_nvgpu/mma-atoms-sm70-120.md) documents the WGMMA SMEM descriptor bit layout and the per-element-type GMMA-K table that drives `advance_descriptor`.
[nvgpu Dialect Overview](../dialects/nvgpu/overview.md) shows how `nvgpu.warpgroup.mma` lowers into this protocol.
[Lowering: nvgpu / gpu to NVVM](../lowering/nvgpu-and-gpu-to-nvvm.md) is the dialect-conversion path that materialises the four-op sequence.
[tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md) covers the backend-side validation of the selected WGMMA machine form.
