# Per-SM Emission Templates

## Abstract

Tileiras emits tensor-core matrix instructions through a different path on
each SM generation. The useful public model is not "which helper printed a
string" but "which instruction surface is available, which operands it
expects, and whether emission goes through inline assembly or an NVPTX
machine instruction."

Older Volta and Turing MMA operations take the NVVM intrinsic path. Ampere
and Ada take `llvm.inline_asm` templates for dense and sparse `mma.sync`.
Hopper adds WGMMA templates. Datacenter Blackwell moves tensor-core matmul
into tensor-memory `tcgen05` machine instructions. Consumer Blackwell has
no tensor memory and falls back to warp-level block-scaled `mma.sync`
machine instructions.

## Capability Matrix

| SM tier | Public surface | Emission path | Main instruction family |
|---|---|---|---|
| SM70 / SM75 | `nv_tileas.mma.sm70`, `nv_tileas.mma.sm75` | NVVM intrinsic | `mma.sync.m8n8k*` |
| SM80 | dense/sparse MMA atoms | inline asm | `mma.sync.aligned`, `mma.sp.sync.aligned` |
| SM89 | FP8 MMA atoms | inline asm | `mma.sync.aligned.m16n8k32` |
| SM90 | warp-group MMA | inline asm / NVVM ops | `wgmma.mma_async.sync.aligned` |
| SM100 / SM103 | tensor-memory MMA | MachineInstr | `tcgen05.mma` |
| SM120 / SM121 | block-scaled warp MMA | MachineInstr | `mma.sync.aligned.*.block_scale` |

```c
MmaEmissionPath select_mma_emission_path(SmVersion sm, MmaAtom atom) {
    if (sm.major == 70 || sm.major == 75) {
        return MMA_EMIT_NVVM_INTRINSIC;
    }
    if (sm.major == 80 || sm.major == 89) {
        return MMA_EMIT_INLINE_ASM_MMA_SYNC;
    }
    if (sm.major == 90) {
        return MMA_EMIT_INLINE_ASM_WGMMA;
    }
    if (sm.major == 100 || sm.major == 103) {
        return MMA_EMIT_MACHINE_TCGEN05;
    }
    if (sm.major == 120 || sm.major == 121) {
        return MMA_EMIT_MACHINE_BLOCK_SCALE;
    }

    fail("unsupported tensor-core target");
}
```

## SM70 / SM75

Volta and Turing need no Tileiras-owned inline-assembly templates for
their baseline MMA surface. The dialect registers the SM70 and SM75 atoms,
then lowers them to the corresponding `llvm.nvvm.mma.*` intrinsics. The
downstream NVPTX backend owns final PTX spelling.

There is no template catalog to copy for these tiers. Emit the intrinsic
with the correct shape, layout, and element types, then let NVPTX perform
final instruction selection.

| Tier | Shape families | Lowering rule |
|---|---|---|
| SM70 | `m8n8k4` | Use NVVM MMA intrinsic. |
| SM75 | `m8n8k16`, `m8n8k32`, `m8n8k128`, BF16 additions | Use NVVM MMA intrinsic. |

## SM80

Ampere is the first tier where Tileiras emits MMA text directly. Dense MMA
runs through runtime assembly of a `mma.sync.aligned` template. Sparse MMA
takes `mma.sp.sync.aligned`, with a special ordered-metadata fast path for
the INT8 `m16n8k32` sparse form.

| Family | Shape examples | Accumulator | Extra operands |
|---|---|---|---|
| Dense f16/bf16/tf32 | `m16n8k8`, `m16n8k16` | f16 or f32 | none |
| Dense integer | `m16n8k32`, `m16n8k64` | s32 | optional `.satfinite` |
| Sparse f16/bf16/tf32 | `m16n8k8`, `m16n8k16` | f16 or f32 | metadata + selector |
| Sparse integer | `m16n8k32`, `m16n8k64` | s32 | metadata + selector |
| Ordered metadata | `m16n8k32` INT8 sparse | s32 | metadata, selector fixed to zero |

```c
void emit_sm80_mma(const MmaAtom *atom, InlineAsmBuilder *asm_builder) {
    require(atom->layout_a == ROW && atom->layout_b == COL);

    if (atom->sparse && atom->ordered_metadata) {
        require(atom->shape == SHAPE_M16N8K32);
        require(atom->dtype_a == S8 && atom->dtype_b == S8 && atom->dtype_d == S32);
        emit_ordered_metadata_mma_sp(asm_builder, atom);
        return;
    }

    if (atom->sparse) {
        require_metadata_vector_2xi16(atom->metadata_type);
        require(atom->sparsity_selector == 0 || atom->sparsity_selector == 1);
        emit_generic_mma_sp_sync(asm_builder, atom);
        return;
    }

    emit_generic_mma_sync(asm_builder, atom);
}
```

The metadata operand is logically two i16 values packed into one i32
register. The selector is a one-bit immediate. Dense integer forms can
request `.satfinite`; floating forms have no such modifier at the MMA
level.

## SM89

Ada extends the SM80 dynamic builders with FP8 types. The shape is
`m16n8k32`, the accumulator is f32, and the input type product is one of
`e4m3 x e4m3`, `e4m3 x e5m2`, `e5m2 x e4m3`, `e5m2 x e5m2`.

Register arity follows the SM80 INT8 `k32` layout: four D registers, four
A registers, two B registers, four C registers. Sparse FP8 adds one
metadata register. No FP16 accumulator path exists for this tier's FP8
`mma.sync` — that belongs to the later WGMMA surface.

## SM90

Hopper introduces WGMMA. Tileiras emits `wgmma.mma_async.sync.aligned`
inside a small inline-assembly block with a predicate register used for
scale-D. The lowering sequence is protocol-shaped rather than
single-instruction-shaped: fence, async MMA instructions, commit group,
wait group.

| Input family | D type | K | Notes |
|---|---|---:|---|
| f16 x f16 | f16 or f32 | 16 | Optional scale and transpose operands. |
| bf16 x bf16 | f32 | 16 | Same operand structure as f16/f32. |
| tf32 x tf32 | f32 | 8 | TF32-specific K width. |
| e4m3/e5m2 FP8 pairs | f32 | 32 | Four FP8 type combinations. |
| s8/u8 integer pairs | s32 | 32 | Forced `.satfinite`, no scale-a/b. |
| b1 x b1 | s32 | 256 | Uses `.xor.popc` or `.and.popc`. |

```c
void emit_wgmma_template(WgmmaTemplate *tpl, const WgmmaAtom *atom) {
    tpl_begin_block(tpl);
    tpl_emit_predicate_from_scale_d(tpl, atom->scale_d_operand);

    tpl_emit("wgmma.mma_async.sync.aligned");
    tpl_emit_shape(tpl, atom->m, atom->n, atom->k);
    tpl_emit_type_suffixes(tpl, atom->d_type, atom->a_type, atom->b_type);

    tpl_emit_d_register_list(tpl, atom);
    tpl_emit_a_operand(tpl, atom);
    tpl_emit_b_descriptor(tpl, atom);
    tpl_emit_scale_d_predicate(tpl);

    if (wgmma_uses_scale_ab(atom)) {
        tpl_emit_scale_ab_immediates(tpl, atom);
    }
    if (wgmma_uses_transpose_bits(atom)) {
        tpl_emit_transpose_immediates(tpl, atom);
    }

    tpl_end_statement_and_block(tpl);
}
```

The A operand can be either a register fragment or a shared-memory
descriptor. The B operand is always a shared-memory descriptor. Descriptor
offsets are expressed in 16-byte units, matching the WGMMA addressing
rules.

## SM100 / SM103

Datacenter Blackwell uses tensor memory and emits `tcgen05.mma` through
the MachineInstr layer rather than `llvm.inline_asm`. The instruction is
warp-group-uniform and operates on TMEM operands. The packed control word
carries instruction family, CTA group, sparsity, block scale, scale-vector
size, input family, collector mode, and optional scale-input-accumulator
state.

| Feature | Reimplementation rule |
|---|---|
| TMEM destination | Encode as a tensor-memory operand, not a GPR fragment. |
| B operand | Build a shared-memory descriptor and pass it as a 64-bit operand. |
| Sparse variants | Append the metadata operand and select the sparse instruction family. |
| Block scale | Append SFA/SFB tensor-memory scale operands. |
| `scale_in_acc` | Permit only on supported arch-conditional targets and f16/tf32 inputs. |
| Weight stationary | Forbid two-CTA mode and MX/FP4 input families. |

```c
MachineInstr *emit_sm100_tcgen05(const Tcgen05Atom *atom, const NvptxSubtarget *target) {
    Tcgen05Ctrl ctrl = pack_tcgen05_ctrl(atom);
    verify_tcgen05_mma_ctrl(ctrl, atom, target);

    MachineOperand ops[MAX_TCGEN05_OPERANDS];
    int n = 0;

    ops[n++] = encode_tmem_destination(atom->d);
    ops[n++] = encode_tmem_source(atom->a);
    ops[n++] = build_tcgen05_smem_descriptor(atom->b_smem);

    if (atom->sparse) {
        ops[n++] = encode_sparse_metadata(atom->metadata);
    }
    if (atom->block_scale) {
        ops[n++] = encode_tmem_scale_operand(atom->scale_a);
        ops[n++] = encode_tmem_scale_operand(atom->scale_b);
    }

    return build_machine_instr(select_tcgen05_opcode(atom, ctrl), ops, n);
}
```

SM103 follows the same structural path with a different accepted target
tuple. Drive the algorithm with subtarget feature predicates, not a
separate forked emitter.

## SM120 / SM121

Consumer Blackwell removes tensor memory and therefore drops `tcgen05.mma`
entirely. Its block-scaled matmul surface is warp-synchronous
`mma.sync.aligned.*.block_scale`. The public operation has nine attributes:
`a_type`, `b_type`, `byte_id_a`, `byte_id_b`, `sf_type`, `shape_MNK`,
`thread_id_a`, `thread_id_b`, `vec_size`.

The verifier accepts exactly three shape/vector families:

| K | `vec_size` | Kind | A/B types | Scale-factor type |
|---:|---:|---|---|---|
| 32 | 32 | MXFP8 | e4m3, e5m2, e3m2, e2m3, e2m1 | E8M0 |
| 64 | 16 | MXFP4 | e2m1 | E8M0 or E4M3 |
| 64 | 32 | NVFP4 | e2m1 | E8M0 |

Dense and sparse forms share one set of operand families: A fragment, B
fragment, C accumulator, D output, SFA scale fragment, SFB scale fragment.
Sparse forms add ordered metadata. SFA and SFB are warp-register fragments,
unlike SM100 where the scale operands live in tensor memory.

```c
void verify_sm120_block_scale(const Sm120BlockScaleAtom *atom) {
    require(atom->m == 16 && atom->n == 8, "SM120 block-scale MMA requires 16x8 tiles");

    if (atom->k == 32 && atom->vec_size == 32) {
        require(is_mxfp8_type(atom->a_type) && is_mxfp8_type(atom->b_type),
                "K32 vec32 block-scale MMA requires MXFP8 inputs");
        require(atom->sf_type == SF_E8M0, "MXFP8 block-scale MMA requires E8M0 scale");
        return;
    }

    if (atom->k == 64 && atom->vec_size == 16) {
        require(is_e2m1_pair(atom), "K64 vec16 block-scale MMA requires E2M1 inputs");
        require(atom->sf_type == SF_E8M0 || atom->sf_type == SF_E4M3,
                "MXFP4 block-scale MMA requires E8M0 or E4M3 scale");
        return;
    }

    if (atom->k == 64 && atom->vec_size == 32) {
        require(is_e2m1_pair(atom), "K64 vec32 block-scale MMA requires E2M1 inputs");
        require(atom->sf_type == SF_E8M0, "NVFP4 block-scale MMA requires E8M0 scale");
        return;
    }

    fail("unsupported SM120 block-scale MMA shape");
}
```

Compression from the SM100 tcgen05 lattice to the SM120 surface is
intentional: no CTA group, no collector mode, no A-shift, no
weight-stationary mode, no scale-input accumulator, no tensor-memory
destination, no write-disable modifier. Only shape, element family,
scale-factor family, scale-vector width, and the sparse/dense choice
remain.
