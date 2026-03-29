# Hopper (sm_90, sm_90a)

Hopper represents the largest single-generation feature expansion in cicc v13.0. The sm_90 gate at `qword_4F077A8 > 89999` unlocks thread block clusters, distributed shared memory, Tensor Memory Access (TMA), Warpgroup Matrix Multiply-Accumulate (WGMMA), dynamic register count control, and a new fence instruction. The sm_90a "accelerated" sub-variant shares `__CUDA_ARCH=900` with sm_90 but uses a higher PTX version and enables one additional feature gate in the EDG frontend.

## Architecture Identity

The NVVM container format registers Hopper as `NVVM_ARCH_HOPPER_9_0` with numeric value 900, assigned in `sub_CD09E0` (line 255) and `sub_1C1B150` (line 270) via the pattern `v62(a1, "NVVM_ARCH_HOPPER_9_0", v64) => *a2 = 900`.

| Variant | Subtarget Enum | `__CUDA_ARCH` | PTX Version | `-opt-arch` | `-mcpu` |
|---|---|---|---|---|---|
| `sm_90` | 38 | 900 | 5 | `sm_90` | `sm_90` |
| `sm_90a` | 39 | 900 | 6 | `sm_90a` | `sm_90a` |

Both variants share `__CUDA_ARCH=900`. The distinction lies in the `-opt-arch` and `-mcpu` flags passed through the internal pipeline (`sub_95EB40` lines 461–469, `sub_12C8DD0` lines 435–457). The `sm_90a` variant is the only pre-Blackwell SM that uses PTX version 6; all sm_20 through sm_90 base variants use PTX version 5.

The `a` flag is stored in `unk_4D045E4` and read in exactly one location: `sub_6C4D80` line 167, where the check `unk_4D045E8 != 90 || !unk_4D045E4` gates a specific sm_90a-only feature (error code 0xE90 = 3728).

## Thread Block Cluster Infrastructure

Clusters are the headline Hopper feature. The compiler gates all cluster functionality at `arch_id >= 90` (`unk_4D045E8 > 89`).

### Frontend Attributes

The EDG frontend recognizes three cluster-related kernel attributes:

**`__cluster_dims__`** — Attribute code `k` in `sub_5C79F0`. Processing in `sub_5D1FE0` validates three integer arguments (x, y, z) and stores them at offsets +20, +24, +28 of the kernel metadata structure. Error codes 3685/3686 on invalid values. On sm_89 and below, diagnostic 3687 is emitted as a warning.

**`__launch_bounds__` 3rd parameter** — The cluster dimension extension to `__launch_bounds__` is processed in `sub_5D2430`. On sm_89 and below, diagnostic 3704 is emitted.

**`__block_size__` attribute** — Handled in `sub_5D1A60`. At sm_90+, five block dimension arguments are parsed (including the cluster dimension). At sm_89 and below, diagnostic 3790 is emitted and only four arguments are accepted.

### NVVM Metadata

Cluster configuration propagates through NVVM IR via several metadata keys:

| Metadata Key | Writers | Readers |
|---|---|---|
| `nvvm.cluster_dim` | `sub_93AE30`, `sub_129A750` | `sub_A84F90`, `sub_CE8EA0` |
| `cluster_dim_x/y/z` | `sub_913C80`, `sub_1273830` | `sub_CE8C00/40/80` |
| `cluster_max_blocks` | `sub_913C80`, `sub_1273830` | (kernel metadata) |
| `nvvm.blocksareclusters` | `sub_93AE30`, `sub_129A750` | `sub_214DA90` |
| `nvvm.maxclusterrank` | (external) | `sub_A84F90`, `sub_CE9030` |

The `blocksareclusters` metadata requires `reqntid` to be set — error message: *"blocksareclusters requires reqntid"* (`sub_214DA90` line 111).

### PTX Directives

The kernel attribute emitter at `sub_214DA90` gates cluster directives at `arch_id >= 90`. When the gate passes, four directives may be emitted:

- `.blocksareclusters` — Declares that thread blocks form clusters
- `.explicitcluster` — Emitted when all three cluster dimensions are present
- `.reqnctapercluster X, Y, Z` — Required CTA count per cluster
- `.maxclusterrank N` — Maximum cluster rank

## Cluster Special Registers

The PTX emitter at `sub_21E9060` handles 15 cluster special registers via a switch statement:

| Case | Register | Description |
|---|---|---|
| 0 | `%is_explicit_cluster` | Boolean: was cluster explicitly set |
| 1 | `%cluster_ctarank` | CTA rank within the cluster |
| 2 | `%cluster_nctarank` | Number of CTAs in cluster |
| 3–5 | `%cluster_nctaid.{x,y,z}` | Cluster grid dimensions |
| 6–8 | `%cluster_ctaid.{x,y,z}` | CTA position within cluster |
| 9–11 | `%nclusterid.{x,y,z}` | Cluster grid count |
| 12–14 | `%clusterid.{x,y,z}` | Cluster ID |

## Cluster Barrier Operations

The `barrier.cluster` instruction is emitted from `sub_21E8EA0` with two operation modes and two memory ordering modes:

| Opcode (bits 0–3) | Operation | Memory Mode (bits 4–7) | Qualifier |
|---|---|---|---|
| 0 | `arrive` | 0 | (default acquire/release) |
| 1 | `wait` | 1 | `.relaxed` |

Error strings: *"bad cluster barrier op"* for invalid opcode, *"bad cluster barrier mem mode"* for invalid memory mode.

Three corresponding builtins are registered in `sub_90AEE0`:

| Builtin | ID |
|---|---|
| `__nv_cluster_barrier_arrive_impl` | 11 |
| `__nv_cluster_barrier_wait_impl` | 12 |
| `__nv_cluster_barrier_arrive_relaxed_impl` | 13 |

## Cluster Query Builtins

Nine cluster information builtins are registered in `sub_90AEE0`:

| Builtin | ID | Purpose |
|---|---|---|
| `__nv_clusterDimIsSpecifed_impl` | 8 | Check if cluster dims are set |
| `__nv_clusterRelativeBlockRank_impl` | 9 | Block rank within cluster |
| `__nv_clusterSizeInBlocks_impl` | 10 | Total blocks in cluster |
| `__nv_cluster_query_shared_rank_impl` | 203 | Query shared memory rank |
| `__nv_cluster_map_shared_rank_impl` | 365 | Map to shared memory rank |
| `__nv_clusterDim_impl` | 405 | Get cluster dimensions |
| `__nv_clusterRelativeBlockIdx_impl` | 406 | Relative block index |
| `__nv_clusterGridDimInClusters_impl` | 407 | Grid dimension in clusters |
| `__nv_clusterIdx_impl` | 408 | Cluster index |

## fence.sc.cluster Instruction

A new fence instruction is emitted from `sub_21E94F0`, the membar/fence printer. The opcode encoding uses the low 4 bits of the operand:

| Value | Instruction | Generation |
|---|---|---|
| 0 | `membar.gpu` | All |
| 1 | `membar.cta` | All |
| 2 | `membar.sys` | All |
| 4 | `fence.sc.cluster` | Hopper+ |

A duplicate implementation exists in the NVPTX backend at `sub_35F18E0`.

## Atomic Cluster Scope

At sm_90+, the atomic lowering paths (`sub_12AE930` line 255, `sub_9502D0` line 424) add cluster scope support. Scope value 2 now resolves to `"cluster"` instead of falling through to `"gpu"` as it does on sm_70–89. This enables `atom.*.cluster` operations for intra-cluster synchronization.

## setmaxnreg — Dynamic Register Count

Hopper introduces dynamic register count adjustment via `setmaxnreg.{inc,dec}.sync.aligned.u32`.

**NVVM IR validation** (`sub_BFC6A0` lines 1732–1754): Builtin IDs 9431–9432 correspond to `nvvm.setmaxnreg.inc` and `nvvm.setmaxnreg.dec`. Validation rules enforce that the register count must be a multiple of 8 and within the range [24, 256].

**Inline assembly recognition** (`sub_FCDCB0`, `sub_21EA5F0`): The compiler scans inline asm for `setmaxnreg.` followed by `.sync.aligned.u32`, extracting the immediate operand from either a `$0` placeholder or a literal integer. Backend duplicates exist at `sub_307BA30` and `sub_3953170`.

## WGMMA — Warpgroup Matrix Multiply-Accumulate

WGMMA is Hopper's primary tensor core interface, superseding HMMA for large matrix operations.

### Registered Builtins

Four type variants are registered in `sub_90AEE0` (lines 2941–2944) with a duplicate table in `sub_126A910`:

| Builtin | ID | Accumulator Type |
|---|---|---|
| `__wgmma_mma_async_f16` | 765 | FP16 |
| `__wgmma_mma_async_bf16` | 766 | BF16 |
| `__wgmma_mma_async_tf32` | 767 | TF32 |
| `__wgmma_mma_async_f8` | 768 | FP8 |

### Shape Selection

The WGMMA lowering at `sub_955A70` (lines 2850–2910+) uses a switch on the M dimension (output rows) to select MachineInstr opcodes:

| M Dimension | Opcode |
|---|---|
| 8 | 10774 |
| 16 | 10690 |
| 24 | 10734 |
| 32 | 10742 |
| 40–88 (stride 8) | 10746–10770 |

Error on invalid M: *"unexpected constant overflow in __wgmma_mma_async operand"*.

### Operand Modifiers

The NVPTX printer at `sub_35F3330` emits WGMMA operand modifiers encoded in bitfields:

- **kind** (bits 6–8): `mxf4nvf4` (0), `f8f6f4` (1), `mxf8f6f4` (2), `f16` (3), `i8` (4), `tf32` (5), `mxf4` (7)
- **cta_group** (bit 1): `cta_group::1` (clear) or `cta_group::2` (set)
- **scale** (bits 2–3): Additional scaling modifier

## TMA — Tensor Memory Access

TMA provides hardware-accelerated bulk data movement between global and shared memory. The intrinsic dispatcher at `sub_A8E250` recognizes the following TMA operations:

### Global-to-Shared Tile Copy

| Intrinsic Pattern | Dimensions | Opcode |
|---|---|---|
| `cp.async.bulk.tensor.g2s.tile.{1d..5d}` | 1D–5D | 9222–9226 |
| `cp.async.bulk.tensor.g2s.im2col.{3d..5d}` | 3D–5D | 9213–9215 |

### Bulk Memory Transfers

| Intrinsic Pattern | Opcode |
|---|---|
| `cp.async.bulk.gmem.to.dsmem` | 8316 |
| `cp.async.bulk.global.to.shared.cluster` | 8315 |
| `cp.async.bulk.tensor.gmem.to.smem.{1d..5d}` | 8324–8328 |
| `cp.async.bulk.tensor.gmem.to.smem.im2col.w.{3d..5d}` | 8329–8331 |

## Distributed Shared Memory

Hopper's cluster architecture enables distributed shared memory (DSMEM) across CTAs in a cluster. The NVPTX backend emits the following memory space qualifiers:

| Qualifier | Source |
|---|---|
| `.shared::cluster` | `sub_35F4E30`, `sub_35F4080` |
| `.async.shared::cluster` | `sub_35F4B50` |
| `.multicast::cluster` | `sub_35F4E30` |
| `.async.shared::cta` | `sub_35F4B50` |
| `.async.global` | `sub_35F4B50` |
| `.async` | `sub_35F4B50` |
| `.alias` | `sub_35F4B50` |

These qualify `cp.async.bulk` and `mbarrier` operations for cluster-level distributed shared memory access.

## Mbarrier Extensions

Hopper extends the async barrier (mbarrier) mechanism with new modifiers emitted from `sub_35F4AD0` and `sub_35F4E30`:

- `.mbarrier_init` — Barrier initialization
- `.mbarrier::arrive::one` — Single-thread arrive
- `.cta_group::1` / `.cta_group::2` — CTA group selection

These are used in conjunction with TMA operations for asynchronous data movement coordination.

## Feature Flag Configuration

The master feature configurator `sub_60E7C0` sets the following flags at the sm_90+ threshold (`qword_4F077A8 > 89999`):

| Flag | Source |
|---|---|
| `unk_4D043D0` | `sub_60E7C0` |
| `unk_4D041B0` | `sub_60E7C0` |
| `unk_4D04814` | `sub_60E7C0` |
| `unk_4D0486C` | `sub_60E7C0` (with C++ version check) |
| `dword_4F07760` | `sub_60E530` |
| `dword_4D043F8` | `sub_60E530` (at > 99999) |
| `dword_4D041E8` | `sub_60E530` (at > 99999) |

## Key Binary Locations

| Address | Symbol | Purpose |
|---|---|---|
| `0xCD09E0` | `sub_CD09E0` | NVVM arch enum (`NVVM_ARCH_HOPPER_9_0`) |
| `0x50C890` | `ctor_356` | Subtarget registration (sm_90 enum 38, sm_90a enum 39) |
| `0x214DA90` | `sub_214DA90` | Kernel attribute emitter (cluster PTX directives) |
| `0x21E9060` | `sub_21E9060` | Cluster special register PTX emission |
| `0x21E8EA0` | `sub_21E8EA0` | Cluster barrier instruction emission |
| `0x21E94F0` | `sub_21E94F0` | Membar/fence printer (`fence.sc.cluster`) |
| `0xBFC6A0` | `sub_BFC6A0` | setmaxnreg NVVM IR validation |
| `0xFCDCB0` | `sub_FCDCB0` | setmaxnreg inline asm pattern matching |
| `0x955A70` | `sub_955A70` | WGMMA lowering (M-dimension switch) |
| `0x90AEE0` | `sub_90AEE0` | Builtin registration (WGMMA, cluster barriers/queries) |
| `0xA8E250` | `sub_A8E250` | TMA intrinsic name parsing |
| `0x35F4B50` | `sub_35F4B50` | DSMEM qualifier emission |
| `0x35F4E30` | `sub_35F4E30` | Commit modifier emission (mbarrier, multicast) |
