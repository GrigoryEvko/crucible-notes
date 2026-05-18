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

The selection rule is a tier-keyed lookup: the SM major version names a single emission path. SM70 and SM75 emit the NVVM intrinsic and let the NVPTX backend pick the final PTX spelling. SM80 and SM89 build a `mma.sync.aligned` inline-asm template at IR time. SM90 builds a four-part WGMMA inline-asm protocol. SM100 and SM103 emit `tcgen05.mma` as a `MachineInstr` directly. SM120 and SM121 emit warp-synchronous `mma.sync.aligned.*.block_scale` as a `MachineInstr`.

## SM70 / SM75

Volta and Turing need no Tileiras-owned inline-assembly templates for their baseline MMA surface. The dialect registers the SM70 and SM75 atoms, then lowers them to the corresponding `llvm.nvvm.mma.*` intrinsics. The downstream NVPTX backend owns final PTX spelling.

| Tier | Shape families | Lowering rule |
|---|---|---|
| SM70 | `m8n8k4` | Use NVVM MMA intrinsic. |
| SM75 | `m8n8k16`, `m8n8k32`, `m8n8k128`, BF16 additions | Use NVVM MMA intrinsic. |

The PTX spelling produced by the NVPTX backend matches the SM tier:

```
mma.sync.aligned.m16n8k8.row.col.f32.f16.f16.f32
    {%fd0, %fd1, %fd2, %fd3},
    {%r0, %r1, %r2, %r3},
    {%r4, %r5},
    {%fd4, %fd5, %fd6, %fd7};
```

The exact register count per operand fragment depends on the shape and element type; the table-driven NVPTX printer reads it from the per-opcode operand-class enumeration.

## SM80

Ampere is the first tier where Tileiras builds the PTX template directly inside `llvm.inline_asm`. Dense MMA emits `mma.sync.aligned`. Sparse MMA emits `mma.sp.sync.aligned` with a metadata register and a sparsity-selector immediate. The INT8 `m16n8k32` sparse form has a `.sp::ordered_metadata` fast path that pins the selector to zero.

| Family | Shape examples | Accumulator | Extra operands |
|---|---|---|---|
| Dense f16/bf16/tf32 | `m16n8k8`, `m16n8k16` | f16 or f32 | none |
| Dense integer | `m16n8k32`, `m16n8k64` | s32 | optional `.satfinite` |
| Sparse f16/bf16/tf32 | `m16n8k8`, `m16n8k16` | f16 or f32 | metadata + selector |
| Sparse integer | `m16n8k32`, `m16n8k64` | s32 | metadata + selector |
| Ordered metadata | `m16n8k32` INT8 sparse | s32 | metadata, selector fixed to zero |

Dense `m16n8k16.f32.f16.f16.f32` emits:

```
mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32
    {%fd0, %fd1, %fd2, %fd3},
    {%r0, %r1, %r2, %r3},
    {%r4, %r5},
    {%fd4, %fd5, %fd6, %fd7};
```

The dense INT8 `m16n8k32.s32.s8.s8.s32` form emits the same shape with `s32`/`s8` type suffixes and an optional `.satfinite` modifier on the destination side.

Sparse `m16n8k16.f32.f16.f16.f32` emits the same operand list plus a metadata register and a selector immediate:

```
mma.sp.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32
    {%fd0, %fd1, %fd2, %fd3},
    {%r0, %r1},
    {%r2, %r3},
    {%fd4, %fd5, %fd6, %fd7},
    %r4,
    0x0;
```

The metadata operand is logically two `i16` values packed into one `i32` register. The selector is a one-bit immediate.

The INT8 ordered-metadata fast path swaps `.sp` for `.sp::ordered_metadata` and elides the explicit selector:

```
mma.sp::ordered_metadata.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32
    {%r0, %r1, %r2, %r3},
    {%r4, %r5, %r6, %r7},
    {%r8, %r9},
    {%r10, %r11, %r12, %r13},
    %r14;
```

Dense integer forms can request `.satfinite`; floating forms have no such modifier at the MMA level.

## SM89

Ada extends the SM80 dynamic builders with FP8 types. The shape is `m16n8k32`, the accumulator is `f32`, and the input type product is one of `e4m3 x e4m3`, `e4m3 x e5m2`, `e5m2 x e4m3`, `e5m2 x e5m2`. The emitted PTX form is:

```
mma.sync.aligned.m16n8k32.row.col.f32.e4m3.e4m3.f32
    {%fd0, %fd1, %fd2, %fd3},
    {%r0, %r1, %r2, %r3},
    {%r4, %r5},
    {%fd4, %fd5, %fd6, %fd7};
```

Register arity follows the SM80 INT8 `k32` layout: four D registers, four A registers, two B registers, four C registers. Sparse FP8 adds one metadata register and reuses the `.sp` modifier shape from SM80. No FP16 accumulator path exists for this tier's FP8 `mma.sync` — that belongs to the later WGMMA surface.

## SM90

Hopper introduces WGMMA. Tileiras emits `wgmma.mma_async.sync.aligned` inside a four-part inline-assembly protocol: fence, one or more async MMA instructions, commit group, wait group. The accumulator-update bit is carried by a predicate register (`%p`) computed from the `scale_d` operand. Shared-memory operands ride as 64-bit descriptors built by the per-atom descriptor constructor.

| Input family | D type | K | Notes |
|---|---|---:|---|
| f16 x f16 | f16 or f32 | 16 | Optional scale and transpose operands. |
| bf16 x bf16 | f32 | 16 | Same operand structure as f16/f32. |
| tf32 x tf32 | f32 | 8 | TF32-specific K width. |
| e4m3/e5m2 FP8 pairs | f32 | 32 | Four FP8 type combinations. |
| s8/u8 integer pairs | s32 | 32 | Forced `.satfinite`, no scale-a/b. |
| b1 x b1 | s32 | 256 | Uses `.xor.popc` or `.and.popc`. |

The four-part protocol for one tile of `m64n128k16.f32.f16.f16` is:

```
wgmma.fence.sync.aligned;

wgmma.mma_async.sync.aligned.m64n128k16.f32.f16.f16
    {%fd0, %fd1, %fd2, %fd3, %fd4, %fd5, %fd6, %fd7,
     %fd8, %fd9, %fd10, %fd11, %fd12, %fd13, %fd14, %fd15,
     %fd16, %fd17, %fd18, %fd19, %fd20, %fd21, %fd22, %fd23,
     %fd24, %fd25, %fd26, %fd27, %fd28, %fd29, %fd30, %fd31},
    %rd0,                       // descriptor A
    %rd1,                       // descriptor B
    %p0,                        // scale-D (accumulator-update predicate)
    1, 1,                       // scale-A, scale-B (FP families only)
    0, 0;                       // transpose-A, transpose-B (FP families only)

wgmma.commit_group.sync.aligned;
wgmma.wait_group.sync.aligned 0;
```

The float families append `scale-A`, `scale-B`, `transpose-A`, `transpose-B` immediates after the scale-D predicate; the integer families omit those and force `.satfinite`. The `b1` family substitutes `.xor.popc` or `.and.popc` for the type suffix.

The A operand can be a register fragment instead of an SMEM descriptor — in that case it appears as a register-tuple `{ %r0, %r1, ... }` and the constraint list switches `l` to `r`. The B operand is always an SMEM descriptor. Descriptor offsets are expressed in 16-byte units, so the constructor shifts byte offsets right by four before packing. See [SM70-120 MMA Atoms — SMEM-Descriptor Construction](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) for the 64-bit descriptor bit layout, and the [WGMMA Descriptor Round-Trip](#wgmma-descriptor-round-trip) section below for a worked hex example.

## SM100 / SM103

Datacenter Blackwell uses tensor memory and emits `tcgen05.mma` through the `MachineInstr` layer rather than `llvm.inline_asm`. The instruction is warp-group-uniform and operates on TMEM operands. The packed control word carries instruction family, CTA group, sparsity, block scale, scale-vector size, input family, collector mode, and optional scale-input-accumulator state. See [tcgen05 Control-Word Bit Layout](tcgen05-wgmma-mbarrier-cluster.md#control-word-bit-layout) for the bit-layout of the control word and [Verifier Rules](tcgen05-wgmma-mbarrier-cluster.md#verifier-rules) for the verifier rules.

A dense `tcgen05.mma` for one tile emits:

```
tcgen05.mma.cta_group::1.kind::f16.f32.f16.f16
    [%r0],                      // TMEM destination (D)
    [%r1],                      // TMEM source (A)
    %rd2,                       // SMEM descriptor (B)
    %r3;                        // packed control word
```

The control-word operand encodes scale-vector size, MMA kind, scale-input-accumulator, and block-scale bits. A sparse variant adds a metadata operand:

```
tcgen05.mma.sp.cta_group::1.kind::f16.f32.f16.f16
    [%r0], [%r1], %rd2, [%r3], %r4;
```

A block-scaled variant adds two TMEM scale operands and a scale-vec modifier:

```
tcgen05.mma.cta_group::1.kind::mxf8f6f4.scale_vec::1X.f32.e4m3.e4m3
    [%r0], [%r1], %rd2,
    [%r3],                      // SFA scale (TMEM)
    [%r4],                      // SFB scale (TMEM)
    %r5;
```

The weight-stationary variant prefixes the mnemonic with `.ws` and rejects two-CTA grouping. The two-CTA variant is encoded as `cta_group::2` in the modifier. The arch-conditional variants (sm_100a, sm_100f) accept different subsets of the kind tag and scale-vec width than the base variant.

SM103 follows the same structural path with a different accepted target tuple. Drive the algorithm with subtarget feature predicates, not a separate forked emitter.

## SM120 / SM121

Consumer Blackwell removes tensor memory and therefore drops `tcgen05.mma` entirely. Its block-scaled matmul surface is warp-synchronous `mma.sync.aligned.*.block_scale`. The public operation has nine attributes: `a_type`, `b_type`, `byte_id_a`, `byte_id_b`, `sf_type`, `shape_MNK`, `thread_id_a`, `thread_id_b`, `vec_size`.

The verifier accepts exactly three shape/vector families:

| K | `vec_size` | Kind | A/B types | Scale-factor type |
|---:|---:|---|---|---|
| 32 | 32 | MXFP8 | e4m3, e5m2, e3m2, e2m3, e2m1 | E8M0 |
| 64 | 16 | MXFP4 | e2m1 | E8M0 or E4M3 |
| 64 | 32 | NVFP4 | e2m1 | E8M0 |

Dense and sparse forms share one set of operand families: A fragment, B fragment, C accumulator, D output, SFA scale fragment, SFB scale fragment. Sparse forms add ordered metadata. SFA and SFB are warp-register fragments, unlike SM100 where the scale operands live in tensor memory.

A dense `m16n8k32` MXFP8 block-scale tile emits:

```
mma.sync.aligned.m16n8k32.row.col.kind::mxf8f6f4.scale_vec::1X.block_scale.f32.e4m3.e4m3.f32
    {%fd0, %fd1, %fd2, %fd3},
    {%r0, %r1, %r2, %r3},
    {%r4, %r5},
    {%fd4, %fd5, %fd6, %fd7},
    %r6,                        // SFA scale fragment (register)
    %r7;                        // SFB scale fragment (register)
```

The NVFP4 `m16n8k64.scale_vec::4X` form emits the same operand layout with `e2m1` type suffixes and a `kind::mxf4nvf4` tag. The MXFP4 `m16n8k64.scale_vec::2X` form pairs `kind::mxf4` with E8M0 or E4M3 scale-factor type.

Sparse variants prepend `.sp::ordered_metadata` and add a metadata register slot:

```
mma.sp::ordered_metadata.sync.aligned.m16n8k64.row.col.kind::mxf4nvf4.scale_vec::4X.block_scale.f32.e2m1.e2m1.f32
    {%fd0, ..., %fd3},
    {%r0, %r1},
    {%r2},
    {%fd4, ..., %fd7},
    %r3,                        // metadata
    %r4, %r5;                   // SFA, SFB
```

The MMA verifier rejects shape/vector/type combinations outside the three accepted families. Compression from the SM100 tcgen05 lattice to the SM120 surface is intentional: no CTA group, no collector mode, no A-shift, no weight-stationary mode, no scale-input accumulator, no tensor-memory destination, no write-disable modifier. Only shape, element family, scale-factor family, scale-vector width, and the sparse/dense choice remain.

## WGMMA Descriptor Round-Trip

The SM90 inline-asm template above threads an SMEM descriptor through the `l` constraint slot for operand B (and for operand A when the atom is fully SMEM-resident). The descriptor is a 64-bit packed word built by the per-atom constructor from the abstract tile shape and swizzle mode. A worked example shows the round-trip from logical fields to the hex value that lands in the inline-asm input.

Consider a representative atom: `m64n128k16.f32.f16.f16` with `swizzle=128B`, `lbo=2048`, `sbo=0`, and a starting SMEM byte offset chosen so `(smem_off >> 4) = 0x1000`. The constructor packs the fields according to the [WGMMA descriptor layout](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction):

| Field | Bits | Width | Logical value | Encoded |
|---|---|---:|---|---|
| `start_addr` | 0-13 | 14 | `smem_off >> 4 = 0x1000` (low 14 bits) | `0x1000` |
| `lbo` | 14-29 | 16 | `2048` (`0x800`) | `0x800` |
| `sbo` | 30-45 | 16 | `0` | `0x0` |
| `base_offset` | 46-48 | 3 | `0` | `0` |
| `reserved` | 49-51 | 3 | `0` (mandatory) | `0` |
| `swizzle_mode` | 52-53 | 2 | `128B` | `1` |
| `pad` | 54-63 | 10 | unused | `0` |

The composition is straightforward:

```c
uint64_t raw = 0;
raw |= ((uint64_t)0x1000) <<  0;   // start_addr at bits  0-13
raw |= ((uint64_t)0x0800) << 14;   // lbo         at bits 14-29
raw |= ((uint64_t)0x0000) << 30;   // sbo         at bits 30-45
raw |= ((uint64_t)0x0000) << 46;   // base_offset at bits 46-48
raw |= ((uint64_t)0x0001) << 52;   // swizzle 128B at bits 52-53
```

The resulting `WgmmaDescriptor.raw` value is `0x0010_0000_0200_1000`. Decomposed back: bits 0-13 hold `0x1000`, bits 14-29 hold `0x800` (the four bytes `0x02000` overlap into the lbo window because the field starts at bit 14), bits 52-53 hold the `128B` swizzle code, and every reserved bit is clear. A round-trip through `decode_descriptor(0x00100000_02001000)` produces the exact original logical-field set.

The constructor passes this hex value into the inline-asm fragment as an `i64` input bound to the `l` constraint slot. The PTX template that consumes it is the WGMMA form documented in [SM90](#sm90); the runtime register-allocator sees the constant as a 64-bit GPR (`%rd1` in the example) and the WGMMA hardware decodes it back into the canonical Hopper SMEM descriptor on each `wgmma.mma_async.sync.aligned` issue. A mismatch between the constructor's swizzle mode and the verifier's swizzle mode produces silently wrong results, which is why the constructor and verifier must read the same swizzle table — see the cross-reference paragraph at the end of [SMEM-Descriptor Construction](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction).

## Cross-References

[PTX Version and Target Selection — Architecture-Conditional Instructions](../topics/ptx-version-and-target-selection.md#architecture-conditional-instructions)
documents the upstream subtarget gating that decides which of these
templates is reachable for a given `.target` line. Plain `sm_NN` rows
admit only the SM70-SM80-SM89 surfaces; `sm_NNa` and `sm_NNf` rows
unlock WGMMA, `tcgen05`, and `block_scale` MMA per the suffix grid in
that page. [AsmPrinter — MC Switch Shape Population Table](asm-printer-monster-and-windows.md#mc-switch-shape-population-table) documents the dispatcher and [AsmWriter String Pools and the XOR-3 Walking Cipher](asm-printer-monster-and-windows.md#asmwriter-string-pools-and-the-xor-3-walking-cipher) covers the mnemonic pool that finally prints the template strings shown above. [tcgen05 Control-Word Bit Layout](tcgen05-wgmma-mbarrier-cluster.md#control-word-bit-layout) covers the SM100/SM103 control word and [tcgen05 mbarrier Emission](tcgen05-wgmma-mbarrier-cluster.md#mbarrier-emission) plus [Cluster Sync Emission](tcgen05-wgmma-mbarrier-cluster.md#cluster-sync-emission) cover the mbarrier/cluster wiring around `tcgen05.mma`. [ISelDAG and MatcherTable — Selector Layers](iseldag-and-matchertable.md#selector-layers) shows where the selector chooses between inline-asm and `MachineInstr` paths for each SM tier. The MMA atom registry in [SM70-120 MMA Atoms](../dialects/cute_nvgpu/mma-atoms-sm70-120.md) is the dialect-level entry point that feeds shape and operand types into these templates.
