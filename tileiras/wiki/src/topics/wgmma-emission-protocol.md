# WGMMA Emission Protocol

## Abstract

WGMMA is Hopper's asynchronous warp-group matrix multiply. Four warps cooperate on one accumulator tile; the multiply itself is asynchronous against the issuing warp group and only becomes visible to subsequent reads through a wait-group barrier. The legal usage contract is a four-op emission protocol — fence, one or more async MMA instructions, commit-group, wait-group — and an accumulator-lifetime contract that says: an accumulator written by a still-in-flight WGMMA cannot be read until its group has been drained. Violations are silent data races, not verifier errors.

This page is the canonical reference for the protocol. It supersedes the duplicated lower-WGMMA snippets in [tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md), [Lowering: nvgpu / gpu to NVVM](../lowering/nvgpu-and-gpu-to-nvvm.md), [nvgpu Dialect Overview](../dialects/nvgpu/overview.md), and [MMA Atoms SM70-SM120](../dialects/cute_nvgpu/mma-atoms-sm70-120.md). Those pages now defer here for the emission sequence and the lifetime contract; they keep their own descriptor-construction, dialect-pattern, and verifier content.

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

## SMEM Descriptor Bit Layout

Operand B is always an SMEM descriptor — a packed 64-bit immediate-style word built once per operand before the tile loop, then threaded through the inline-asm fragment as an `l`-constraint i64 input. The same bit layout serves every Hopper WGMMA shape; the constructor is one routine fed by per-atom shape and swizzle metadata, not a family of per-shape variants. The canonical 64-bit packing layout is:

| Bits | Field | Width | Meaning |
|---:|---|---:|---|
| 0-13 | `start_addr` | 14 | Low 14 bits of SMEM byte offset right-shifted by 4 (16-byte alignment) |
| 14-29 | `lbo` | 16 | Leading byte offset between rows of a warp tile |
| 30-45 | `sbo` | 16 | Stride byte offset between consecutive warp tiles along K |
| 46-48 | `base_offset` | 3 | Per-CTA SMEM offset, scaled by 8 |
| 49-51 | reserved | 3 | Must be zero; constructor masks explicitly |
| 52-53 | `swizzle_mode` | 2 | 0 = none, 1 = 128B, 2 = 64B, 3 = 32B |
| 54-63 | pad | 10 | Unused |

The bit ranges come from the constructor in `cute_nvgpu` and are mirrored by the operand-layout verifier — see [SMEM-Descriptor Construction](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) for the same table from the dialect side.

```c
typedef union WgmmaDescriptor {
    uint64_t raw;
    struct {
        uint64_t start_addr   : 14;   /* bits 0-13  */
        uint64_t lbo          : 16;   /* bits 14-29 */
        uint64_t sbo          : 16;   /* bits 30-45 */
        uint64_t base_offset  : 3;    /* bits 46-48 */
        uint64_t reserved     : 3;    /* bits 49-51 */
        uint64_t swizzle_mode : 2;    /* bits 52-53 */
        uint64_t pad          : 10;   /* bits 54-63 */
    };
} WgmmaDescriptor;

uint64_t make_smem_desc(uint32_t smem_byte_off,
                       uint16_t lbo, uint16_t sbo,
                       uint8_t base_offset, uint8_t swizzle_mode) {
    WgmmaDescriptor d = {0};
    d.start_addr   = (smem_byte_off >> 4) & 0x3FFF;   /* keep low 14 bits */
    d.lbo          = lbo;
    d.sbo          = sbo;
    d.base_offset  = base_offset & 0x7;
    d.swizzle_mode = swizzle_mode & 0x3;              /* 0/1/2/3 = none/128B/64B/32B */
    return d.raw;
}
```

The constructor must mask the reserved field. Selection sometimes leaves uninitialised scratch bits in the upper half of the SDNode operand, and the WGMMA hardware does not ignore them: a non-zero reserved field is silently UB.

### Worked Decode

Take the canonical Hopper choice: `m64n128k16.f32.f16.f16` with `swizzle = 128B`, `lbo = 2048`, `sbo = 0`, `base_offset = 0`, and an SMEM byte offset whose `(>> 4)` value lands at `0x1000`. The packed bit fields are:

| Field | Logical | Hex | Encoded position |
|---|---|---|---|
| `start_addr` | `smem_off >> 4 = 0x1000` | `0x1000` | bits 0-13 |
| `lbo` | `2048 = 0x800` | `0x800` | bits 14-29 |
| `sbo` | `0` | `0x0` | bits 30-45 |
| `base_offset` | `0` | `0x0` | bits 46-48 |
| `swizzle_mode` | `128B` | `1` | bits 52-53 |

Composing them:

```c
uint64_t raw = 0;
raw |= ((uint64_t)0x1000) <<  0;   /* start_addr   */
raw |= ((uint64_t)0x0800) << 14;   /* lbo          */
raw |= ((uint64_t)0x0000) << 30;   /* sbo          */
raw |= ((uint64_t)0x0000) << 46;   /* base_offset  */
raw |= ((uint64_t)0x0001) << 52;   /* swizzle 128B */
/* raw == 0x0010_0000_0200_1000 */
```

The decode is the inverse: bits 0-13 hold `0x1000`, bits 14-29 hold `0x800` (which spills into nibble `0x02000` of the raw word because the field starts at bit 14), bits 52-53 hold `1`, and every reserved bit is clear. A reimplementation that round-trips through `decode_descriptor(0x00100000_02001000)` produces the exact original logical-field set.

The swizzle table the constructor consults:

| `swizzle_mode` | Row width | Typical use |
|---:|---:|---|
| 0 | none | Plain row-major SMEM tile |
| 1 | 128 B | Canonical Hopper choice for full-width A and B tiles |
| 2 | 64 B  | Smaller tensor-core operand (sub-canonical tile) |
| 3 | 32 B  | Sub-tile WGMMA |

The 128 B mode is the canonical choice for `m64n{128, 192, 256}k{8, 16, 32}` tiles. The 64 B and 32 B modes kick in when the operand element width or warp-tile footprint is smaller than a canonical 128 B row.

## Descriptor Advancement

When the WGMMA region iterates over output tiles, descriptors advance by the per-tile byte stride converted to 16-byte units:

```c
uint64_t advance_descriptor(uint64_t desc, int m_tile, int k_tile, Layout layout) {
    uint64_t byte_offset = layout_byte_offset(layout, m_tile, k_tile);
    return desc + (byte_offset >> 4);
}
```

The advancement adds to `start_addr` and may carry through into the `lbo` field if the M or K extent crosses a 14-bit boundary — the field aliasing is intentional, since `start_addr` and `lbo` together carry the SMEM offset for the next warp tile. A reimplementation that forgets the `>> 4` advances the descriptor 16x too far on the first tile and silently aliases distant SMEM regions on subsequent tiles. The verifier does not catch it because the descriptor field is opaque from the dialect's point of view.

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

## Per-Shape Lattice

WGMMA fixes M at 64 — that is the warp-group dimension (4 warps × 16-thread tile = 64 rows of output per instruction). N steps in multiples of 8 up to 256, and K is fixed per input element type at `256 / elem_bits`. The per-input-family availability is:

| Input family | Accumulator | Legal (M, N, K) shapes | K |
|---|---|---|---:|
| `f16 × f16` | `f16` or `f32` | `{64} × {8, 16, 24, ..., 256} × {16}` | 16 |
| `bf16 × bf16` | `f32` | `{64} × {8, 16, 24, ..., 256} × {16}` | 16 |
| `tf32 × tf32` | `f32` | `{64} × {8, 16, 24, ..., 256} × {8}` | 8 |
| `e4m3 × e4m3` (FP8) | `f32` | `{64} × {8, 16, 24, ..., 256} × {32}` | 32 |
| `e5m2 × e5m2` (FP8) | `f32` | `{64} × {8, 16, 24, ..., 256} × {32}` | 32 |
| Mixed `e4m3 × e5m2` | `f32` | `{64} × {8, 16, 24, ..., 256} × {32}` | 32 |
| `s8 × s8` / `u8 × u8` | `s32` | `{64} × {8, 16, 24, ..., 256} × {32}` | 32 |
| `s4 × s4` / `u4 × u4` | `s32` | `{64} × {8, 16, 24, ..., 256} × {64}` | 64 |
| `b1 × b1` (popcount) | `s32` | `{64} × {8, 16, 24, ..., 256} × {256}` | 256 |

The K column reflects the canonical `256 / elem_bits` rule, with one exception: `b1` rides a `.xor.popc` or `.and.popc` reduction over 256 bits of K, well past the canonical 256-bit-element budget. The b1 path is the only WGMMA variant that does not multiply-accumulate in the conventional sense.

The N step of 8 is the WGMMA hardware constraint on the output tile size — there is no N=12 or N=20 variant. Lowering rejects any N that is not a multiple of 8 with "WGMMA N must be a multiple of 8". The K column entry is a hard match — the lowering does not synthesise a K=24 `f16` WGMMA by issuing one K=16 and one K=8 instruction; the K=8 form is `tf32`-only, and the K extent for `f16` must be exactly 16 per instruction.

The largest single-instruction tile is `m64n256k16.f16` for FP16 inputs (8192 output elements per warp-group instruction) and `m64n256k32.e4m3` for FP8 (8192 outputs over twice the K extent). Lowering tiles a logical matmul into per-instruction tiles by stepping along N in chunks bounded by the largest legal N and along K in chunks of the per-family K column; the M axis stays at 64 for the entire warp group's lifetime and the loop nest threads tiles into the four-op sequence one at a time.

For comparison against earlier and later tiers, see [Matmul Progression by SM](matmul-progression-by-sm.md) for the cross-architecture shape lattice that places WGMMA between Ampere's `m16n8k*` register MMA and Blackwell's `tcgen05.mma`.

## SM Gating

WGMMA is `sm_90a` only. The architecture-conditional suffix matters: plain `sm_90` rejects WGMMA at NVVM verification. The dialect exposes WGMMA atoms through `cute_nvgpu.sm90.mma` and lowering rejects them on every other target.

Blackwell removes WGMMA. SM100 and SM103 use `tcgen05.mma` over tensor memory; SM120 and SM121 (consumer Blackwell) use a synchronous `mma.sync.aligned` with explicit per-operand scale factors. Both replacements have different operand-residency models — see [Matmul Progression by SM](matmul-progression-by-sm.md) for the cross-architecture story.

## Cross-References

[Matmul Progression by SM](matmul-progression-by-sm.md) places WGMMA in the broader SM70-to-SM121 lineage and explains what replaced it on each generation.
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) is the Blackwell successor; the 4-op protocol changes because the accumulator now lives in TMEM.
[mbarrier State Machine](mbarrier-state-machine.md) defines the transaction-barrier kind that producers use to publish WGMMA completion when a downstream pipeline stage needs to observe it.
[MMA Atoms SM70-SM120](../dialects/cute_nvgpu/mma-atoms-sm70-120.md) documents the WGMMA SMEM descriptor bit layout and the per-element-type GMMA-K table that drives `advance_descriptor`.
[nvgpu Dialect Overview](../dialects/nvgpu/overview.md) shows how `nvgpu.warpgroup.mma` lowers into this protocol.
[Lowering: nvgpu / gpu to NVVM](../lowering/nvgpu-and-gpu-to-nvvm.md) is the dialect-conversion path that materialises the four-op sequence.
[tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md) covers the backend-side validation of the selected WGMMA machine form.
