# NVVM WGMMA Ops

## Abstract

`nvvm.wgmma.*` is the warp-group asynchronous MMA family used on Hopper (sm_90a). A warp group is four contiguous warps cooperating on one `m64nNkK` accumulator tile, with B always resident in shared memory through a 64-bit SMEM descriptor and A either in registers or in SMEM through a second descriptor. The 9 ops in this family pair into a four-stage pipeline: fence, `mma_async`, commit, wait. See [WGMMA Emission Protocol — The Four-Op Sequence](../../topics/wgmma-emission-protocol.md#the-four-op-sequence) for the pipeline timing and [WGMMA Emission](../../codegen/tcgen05-wgmma-mbarrier-cluster.md#wgmma-emission) for the codegen side.

Blackwell (sm_100+) does not extend this family. The Hopper WGMMA path is the only `wgmma.*` PTX surface; Blackwell MMA lives in [`nvvm.tcgen05.*`](tcgen05-ops.md).

## Op Roster

The "Properties slots used" column tracks where each op stores its attribute payload in the inline Properties record; see [Properties Blob — Per-op-family slot maps](properties-blob-and-attr-parsers.md#per-op-family-properties-slot-maps) for the exact byte offsets.

| Op | Role | Properties slots used |
|---|---|---|
| `nvvm.wgmma.fence.aligned` | producer-side fence before `mma_async` | none |
| `nvvm.wgmma.fence.sync.aligned` | consumer-side fence after wait | none |
| `nvvm.wgmma.mma_async.sync.aligned` | the MMA itself | `typeA`, `b1Op`, `typeB`, `shape`, `typeC`, `scaleIn`, `scaleOut`, `layoutA`, `layoutB` |
| `nvvm.wgmma.commit.group.sync.aligned` | close the current MMA group | `wgmma_type`, `wgmma_layout` |
| `nvvm.wgmma.wait.group.sync.aligned` | wait for the group with depth `N` | `wgmma_type`, `wgmma_layout`, shape-N |

The two extra "commit/wait carry attributes" entries exist because the printed PTX carries the type+layout suffix even though no register operand survives — the suffix selects which earlier `mma_async` group the wait drains.

## Operand Tables

### `nvvm.wgmma.fence.aligned` / `nvvm.wgmma.fence.sync.aligned`

No operands and no result. Both lower to a single PTX `wgmma.fence.sync.aligned;` instruction.

### `nvvm.wgmma.mma_async.sync.aligned`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `descA` | `i64` | [WGMMA SMEM descriptor](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) for A — or, for the A-in-registers form, an `!llvm.struct` register fragment |
| operand 1 | `descB` | `i64` | [WGMMA SMEM descriptor](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) for B (always SMEM-resident) |
| operand 2 | `accumIn` | `!llvm.struct<(T, ..., T)>` of accumulator regs | accumulator input tile |
| attribute | `typeA` | enum `wgmma_type` | `f16` / `bf16` / `tf32` / `e4m3` / `e5m2` / `s8` / `u8` / `s4` / `u4` / `b1` |
| attribute | `typeB` | enum `wgmma_type` | mirror of `typeA` |
| attribute | `typeC` | enum `wgmma_type` | usually `f32`; `f16` allowed for `f16xf16` |
| attribute | `shape` | enum `shape` | `m64nNkK` selector — `N ∈ {8, 16, 24, ..., 256}` step 8 |
| attribute | `scaleIn` | enum `wgmma_scale_in` | `+1` / `-1` for A and B |
| attribute | `scaleOut` | enum `wgmma_scale_out` | `0` (init) or `1` (accumulate) |
| attribute | `layoutA` | enum `mma_layout` | `row` / `col` |
| attribute | `layoutB` | enum `mma_layout` | `row` / `col` |
| attribute | `b1Op` | enum `mma_b1op` | `xor_popc` / `and_popc` / `none` |
| result 0 | `accumOut` | same struct type as `accumIn` | accumulator after the MMA |

The accumulator struct width depends on `N` and `typeC`. For `m64n128k16.f32.f16.f16` the accumulator is 64 `f32` registers laid out as `struct<(f32) x 64>`; for `m64n64k16.f32.f16.f16` it is 32 `f32`. The verifier rejects any struct width that does not match `N * typeC_bits / 32`.

### `nvvm.wgmma.commit.group.sync.aligned`

| Position | Name | Type | Notes |
|---|---|---|---|
| attribute | `wgmma_type` | enum | echoes the `mma_async` `typeA`/`typeB` selector |
| attribute | `wgmma_layout` | enum | echoes the layout pair |

No operands; closes the current outstanding-MMA group.

### `nvvm.wgmma.wait.group.sync.aligned`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `groupDepth` | `i32` | number of older groups the wait keeps alive |
| attribute | `wgmma_type` / `wgmma_layout` / shape-N | enums | propagated through to the PTX suffix |

A depth-zero wait drains every outstanding group; non-zero values keep older groups in flight while ensuring the current one is complete.

## WGMMA SMEM Descriptor

The 64-bit value passed as `descA` (when A is SMEM-resident) and `descB` packs the SMEM tile origin and stride into a single word. The bit layout is shared with `cute_nvgpu`'s [WGMMA descriptor construction](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction):

```c
typedef union WgmmaDescriptor {
    uint64_t raw;
    struct {
        uint64_t start_addr   : 14;   /* low 14 bits of (smem_byte_offset >> 4) */
        uint64_t lbo          : 16;   /* leading byte offset (per-warp tile)    */
        uint64_t sbo          : 16;   /* stride byte offset (between warp tiles)*/
        uint64_t base_offset  : 3;    /* per-CTA SMEM base offset (>>3)         */
        uint64_t reserved     : 3;    /* always zero                            */
        uint64_t swizzle_mode : 2;    /* 0=none, 1=128B, 2=64B, 3=32B           */
        uint64_t pad          : 10;
    };
} WgmmaDescriptor;
```

`start_addr` requires 16-byte SMEM alignment because the field stores the offset shifted right by 4. `lbo` and `sbo` together encode the two-dimensional warp-tile stride layout. The swizzle field selects the canonical Hopper 128-byte mode, with 64-byte and 32-byte modes available for sub-tile widths.

The descriptor reaches `nvvm.wgmma.mma_async.sync.aligned` as a plain `i64` operand. The pattern that builds it sits in `nvgpu.warpgroup.descriptor` (see the [nvgpu overview](../nvgpu/overview.md#nvgpuwarpgroupdescriptor-also-spelled-warpgroupgeneratedescriptor)).

## LLVM Intrinsic Mapping

| Op | LLVM intrinsic |
|---|---|
| `nvvm.wgmma.fence.aligned` | `llvm.nvvm.wgmma.fence.sync.aligned` |
| `nvvm.wgmma.mma_async.sync.aligned` (m64n128k16, f32.f16.f16) | `llvm.nvvm.wgmma.mma_async.sync.aligned.m64n128k16.f32.f16.f16` |
| `nvvm.wgmma.mma_async.sync.aligned` (m64n256k32, f32.e4m3.e4m3) | `llvm.nvvm.wgmma.mma_async.sync.aligned.m64n256k32.f32.e4m3.e4m3` |
| `nvvm.wgmma.commit.group.sync.aligned` | `llvm.nvvm.wgmma.commit.group.sync.aligned` |
| `nvvm.wgmma.wait.group.sync.aligned` | `llvm.nvvm.wgmma.wait.group.sync.aligned` |

The intrinsic name is built by concatenating the shape, accumulator type, A type, and B type tokens. Tile counts (`m64nNkK`) are enumerated: every `N ∈ {8, 16, 24, ..., 256}` exposes a separate intrinsic. The verifier rejects any `N` outside that lattice.

## PTX Templates

```text
wgmma.fence.sync.aligned;

wgmma.mma_async.sync.aligned.m64nNkK.{accT}.{aT}.{bT}
    { %d0, %d1, ..., %d{accW-1} },
    %da, %db, %p,
    %scale_a, %scale_b,
    %trans_a, %trans_b;

wgmma.commit_group.sync.aligned;

wgmma.wait_group.sync.aligned N;
```

`%da` and `%db` are the 64-bit SMEM descriptors. `%p` is the immediate scale-D predicate (compile-time `0` or `1`) that selects between init (overwrite accumulator) and accumulate. `%scale_a` and `%scale_b` are the immediate `+1`/`-1` selectors that bind to `scaleIn`. `%trans_a` and `%trans_b` are the immediate transpose flags bound to `layoutA` / `layoutB`. The accumulator register list `%d0..%d{accW-1}` expands per `N` and accumulator type.

For the canonical `m64n128k16.f32.f16.f16` shape:

```text
wgmma.mma_async.sync.aligned.m64n128k16.f32.f16.f16
    { %f0, %f1, ..., %f63 },
    %da, %db, %p, 1, 1, %la, %lb;
```

## Per-Arch Availability

| Op | SM floor | `ptx_min` |
|---|---|---|
| `wgmma.fence.aligned` | sm_90a | 8.0 |
| `wgmma.mma_async.sync.aligned` | sm_90a | 8.0 |
| `wgmma.commit.group.sync.aligned` | sm_90a | 8.0 |
| `wgmma.wait.group.sync.aligned` | sm_90a | 8.0 |

Plain `sm_90` is rejected; the WGMMA family requires the architecture-qualified `sm_90a` variant. Blackwell (`sm_100+`) does not extend WGMMA — the Blackwell tensor-memory MMA path is [`nvvm.tcgen05.mma.sync`](tcgen05-ops.md#nvvmtcgen05mma-dense). See [Per-SM Emission Templates — SM90](../../codegen/per-sm-emission-templates.md#sm90) for the Hopper PTX templates and [WGMMA Descriptor Round-Trip](../../codegen/per-sm-emission-templates.md#wgmma-descriptor-round-trip) for the descriptor hex walk-through.

## Verifier Invariants

- `shape` is `m64nNkK` with `N ∈ {8, 16, ..., 256}` and `K = 256 / typeA_bits` (or 16 for `tf32`).
- `descA` is `i64` only when `layoutA` matches an SMEM tile; an A-in-registers fragment must be a typed struct.
- `descB` is always `i64`.
- Accumulator struct width equals `N * sizeof(typeC) / 4` 32-bit registers.
- `scaleOut` is a compile-time `i1`; runtime values are rejected.
- `commit.group` and `wait.group` carry the same `wgmma_type` and layout as the in-flight `mma_async`.
- Wait depth is non-negative and fits in 6 bits.
