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

TMA provides hardware-accelerated bulk data movement between global and shared memory, driven by a tensor map descriptor that encodes the multi-dimensional layout. Three independent subsystems in cicc cooperate to implement TMA: the intrinsic name parser (`sub_A8E250`), the SelectionDAG lowering handler (`sub_33AD3D0`), and the NVPTX ISel pattern matcher for `CpAsyncBulkTensor` (`sub_36EC510`).

### TMA Descriptor Format (NVVM Container Tag 401)

The host-side tensor map descriptor is embedded in the NVVM container under tag 401. The tag is conditional on `ExtOpt.Field344` (tag 301) having value 1, which identifies the Hopper TMA path. (Blackwell uses tag 402 for TCGen05Config instead, gated by `Field344==4`; the two are mutually exclusive.)

| Component | Size | Description |
|---|---|---|
| Fixed header | 44 bytes | Tensor map metadata (dimensions, strides, element type, interleave, swizzle, fill, OOB policy) |
| Per-descriptor entry | 16 bytes each | One entry per `cp.async.bulk.tensor` call site in the kernel |
| Total struct at offset 408 | 44 + 16*N bytes | N = number of distinct TMA operations |

The compiler serializes this into the NVVM container (`sub_CDD2D0`) so ptxas can validate shared memory allocation sizes and descriptor compatibility at link time.

### TMA Descriptor ABI in Kernel Parameters

The EDG frontend detects TMA descriptor parameters during kernel registration stub generation. The detection function `sub_8D4C10` (`edg::get_tma_descriptor_flags`) checks:

```c
if (unk_4F068E0
    && arch > 0x9EFB
    && type_is_struct_or_class(type)
    && (*(type+140) & ~4) == 8
    && get_tma_descriptor_flags(type) & 4):
  insert copy_node(sub_7E7ED0, calling_convention=7)
  byte_at(node+88) |= 4   // TMA descriptor flag
```

This gives TMA descriptors a distinct ABI: calling convention 7 with flag bit 4, separate from normal struct-by-value passing. The copy node ensures the descriptor is materialized at the correct address space boundary before kernel launch.

### TMA Intrinsic Name Parsing (sub_A8E250)

The intrinsic dispatcher `sub_A8E250` (12 KB native) matches TMA intrinsic names via string comparison and assigns internal opcode IDs. Two families exist:

**Tensor-structured copies** (require a tensor map descriptor):

| Intrinsic Pattern | Dimensions | Opcode |
|---|---|---|
| `cp.async.bulk.tensor.g2s.tile.1d` | 1D | 9222 |
| `cp.async.bulk.tensor.g2s.tile.2d` | 2D | 9223 |
| `cp.async.bulk.tensor.g2s.tile.3d` | 3D | 9224 |
| `cp.async.bulk.tensor.g2s.tile.4d` | 4D | 9225 |
| `cp.async.bulk.tensor.g2s.tile.5d` | 5D | 9226 |
| `cp.async.bulk.tensor.g2s.im2col.3d` | 3D | 9213 |
| `cp.async.bulk.tensor.g2s.im2col.4d` | 4D | 9214 |
| `cp.async.bulk.tensor.g2s.im2col.5d` | 5D | 9215 |
| `cp.async.bulk.tensor.gmem.to.smem.1d` | 1D | 8324 |
| `cp.async.bulk.tensor.gmem.to.smem.2d` | 2D | 8325 |
| `cp.async.bulk.tensor.gmem.to.smem.3d` | 3D | 8326 |
| `cp.async.bulk.tensor.gmem.to.smem.4d` | 4D | 8327 |
| `cp.async.bulk.tensor.gmem.to.smem.5d` | 5D | 8328 |
| `cp.async.bulk.tensor.gmem.to.smem.im2col.w.3d` | 3D | 8329 |
| `cp.async.bulk.tensor.gmem.to.smem.im2col.w.4d` | 4D | 8330 |
| `cp.async.bulk.tensor.gmem.to.smem.im2col.w.5d` | 5D | 8331 |

**Unstructured bulk copies** (byte-level, no tensor map descriptor):

| Intrinsic Pattern | Opcode |
|---|---|
| `cp.async.bulk.global.to.shared.cluster` | 8315 |
| `cp.async.bulk.gmem.to.dsmem` | 8316 |

**Fragment-indexed TMA** (from builtin IDs 411/412 via `sub_9483E0`):

| LLVM Intrinsic | Base Opcode | Index Range |
|---|---|---|
| `llvm.nvvm.tma.load` | 9233 | 9227–9232 (6 entries, indexed by fragment count) |
| `llvm.nvvm.tma.store` | 9257 | (corresponding store entries) |

### TMA SelectionDAG Lowering (sub_33AD3D0)

The unified TMA handler `sub_33AD3D0` receives a `mode` argument from the main intrinsic lowering switch in `sub_33B0210`:

| Case | Mode | Operation | Memory Direction |
|---|---|---|---|
| `0x179` | 2 | TMA load | global -> shared |
| `0x17A` | 3 | TMA store | shared -> global |
| `0x17B` | 5 | TMA prefetch | global (read-only) |
| `0x17C` | 7 | TMA multicast load | global -> N shared (across cluster) |

Related `cp.async` handlers in the same dispatch table:

| Case | Handler | Operation |
|---|---|---|
| `0x175` | `sub_33AC2B0` | `cp.async` (non-TMA async copy) |
| `0x176` | `sub_33AC130` | `cp.async.wait` |
| `0x177` | `sub_33AB690` | `cp.async.bulk` (non-tensor bulk copy) |
| `0x178` | `goto LABEL_32` | No-op — commit/barrier (scheduling fence only) |

The `0x178` no-op is significant: it represents the `cp.async.bulk` commit/barrier intrinsic that exists purely for scheduling purposes. The compiler preserves it as a DAG ordering constraint even though it produces no data-flow SDNode.

### CpAsyncBulkTensor G2S Lowering (sub_36EC510)

The 27 KB function `sub_36EC510` (1185 lines) implements the complete `cp.async.bulk.tensor` global-to-shared lowering with full architecture gating and mode validation.

**Architecture gates** (read from offset+340 of the subtarget object):

| SM Value | Hex | Features Unlocked |
|---|---|---|
| >= 1000 | `0x3E8` | SM 90: tile mode (1D–5D), Im2Col mode (3D–5D) |
| >= 1032 | `0x408` | SM 100: adds 2CTA mode, Im2Col_W, Im2Col_W128 |

**Mode bit decoding** from operand `v11`:

| Bits | Mask | Meaning |
|---|---|---|
| 2–4 | `v11 & 0x1C` | Im2Col variant: Im2Col, Im2Col_W, Im2Col_W128 |
| 3–4 | `v11 & 0x18` | 2CTA mode flag |

**Validation error strings** (emitted as fatal diagnostics):

- *"NumDims should be at least 3 for Im2Col or Im2Col_W or Im2Col_W128 mode"* — Im2Col requires >= 3D tensors
- *"Im2Col_W and Im2Col_W128 modes are not supported on this architecture."* — SM 90 does not support Im2Col_W/W128; requires SM 100+
- *"2CTA Mode for CpAsyncBulkTensorG2S not supported on this architecture"* — 2CTA mode requires SM 100+

### TMA Builtin Codegen (EDG -> LLVM IR)

The EDG-to-LLVM builtin lowering handles TMA as builtin IDs 411 and 412 (hex `0x19B` / `0x19C`).

**ID 411 (scatter/store path)** — `sub_12A7070` extracts TMA descriptor info, then an iterative loop builds a vector of per-element store nodes. The intrinsic table `0x107A`–`0x107F` (4218–4223) selects among 6 entries indexed by element count. Approximately 300 lines of handler code (lines 1256–1501 of `sub_12A71A0`).

**ID 412 (gather/load path)** — Similar structure but for the load direction. Uses intrinsic table `0x1094`–`0x109A` (4244–4250). Includes bitcast insertion (opcode 47) for type mismatches between the descriptor element type and the destination register type. Approximately 450 lines (lines 1503–1713).

Both paths use:
- `sub_12AA280` — TMA descriptor builder (constructs the multi-operand struct from the builtin arguments)
- `sub_12A9E60` — `extractvalue` emission (decomposes aggregate returns into individual registers)
- `sub_39FAC40` — Fragment count computation (determines how many load/store fragments the TMA operation expands into)

### TMA Scheduling Constraints

TMA operations impose specific scheduling constraints visible in cicc's SelectionDAG construction:

1. **Chain dependencies by mode.** Every TMA operation produces a memory chain in the SelectionDAG. The mode parameter determines the chain direction:

   | Mode | Reads | Writes | Chain Effect |
   |---|---|---|---|
   | 2 (load) | global | shared | Load chain |
   | 3 (store) | shared | global | Store chain |
   | 5 (prefetch) | global | (none) | Load chain |
   | 7 (multicast) | global | N x shared | Load chain |

2. **Commit-as-fence.** Intrinsic ID `0x178` lowers to no-op (`goto LABEL_32`), functioning as a pure scheduling barrier. This prevents the DAG scheduler from reordering TMA operations past their commit point.

3. **Async qualifier hierarchy.** The memory space qualifiers emitted by `sub_35F4B50` form an ordered fence hierarchy:

   | Qualifier | Scope | Strength |
   |---|---|---|
   | `.async` | Unscoped | Weakest |
   | `.async.global` | Global memory domain | |
   | `.async.shared::cta` | CTA-local shared memory | |
   | `.async.shared::cluster` | Cluster shared memory (DSMEM) | Strongest |

## Distributed Shared Memory

Hopper's cluster architecture enables distributed shared memory (DSMEM) across CTAs in a cluster. The NVPTX backend emits memory space qualifiers from two functions:

**`sub_35F4B50`** — Async memory space qualifier emission (switch on operand):

| Line | Qualifier | Semantic |
|---|---|---|
| 20 | `.async` | Base async qualifier (unscoped) |
| 32 | `.async.global` | Async from global memory |
| 45 | `.async.shared::cta` | Async to CTA-local shared memory |
| 59 | `.async.shared::cluster` | Async to cluster distributed shared memory |
| 73 | `.alias` | Aliased access modifier (permits overlapping accesses) |

**`sub_35F4E30`** — Commit modifier emission (switch on operand):

| Line | Qualifier | Semantic |
|---|---|---|
| 28 | `.cta_group::1` | CTA group 1 selection |
| 38 | `.cta_group::2` | CTA group 2 selection |
| 51 | `.mbarrier::arrive::one` | Single-thread mbarrier arrive |
| 67 | `.shared::cluster` | Cluster shared memory scope |
| 80 | `.multicast::cluster` | Multicast to all CTAs in cluster |

**`sub_35F4080`** — Secondary `.shared::cluster` emission (line 68), used in non-commit contexts.

These qualifiers attach to `cp.async.bulk` and `mbarrier` instructions to specify the scope and direction of asynchronous data movement within the cluster.

## Mbarrier Extensions — DMA Fence/Arrive/Wait

Hopper extends the async barrier (mbarrier) mechanism to coordinate TMA data movement. The TMA DMA pipeline follows a three-phase synchronization protocol:

### Phase 1: Initialization

`.mbarrier_init` (emitted from `sub_35F4AD0`) initializes the async barrier with the expected transaction byte count. The `arrive_expect_tx` variant sets both the expected arrival count and the transaction byte count atomically.

### Phase 2: Arrive (Producer Signals Completion)

When a TMA operation completes, it signals the mbarrier:

- `.mbarrier::arrive::one` (`sub_35F4E30` line 51) — single-thread arrive notification. The TMA hardware auto-arrives with the transferred byte count.
- `.cta_group::1` / `.cta_group::2` (`sub_35F4E30` lines 28/38) — selects which CTA group the arrive targets, enabling pipelined producer-consumer patterns where two groups alternate roles.

### Phase 3: Wait (Consumer Blocks)

The consumer thread issues `mbarrier.try_wait` with a phase bit. The phase alternates each time the barrier completes a full cycle, enabling pipelined double-buffered access patterns. No additional cicc emission function is needed; the standard mbarrier wait path handles this.

### WGMMA Fence/Commit/Wait (Distinct Pipeline)

WGMMA has its own synchronization cycle, separate from TMA mbarriers:

| Builtin | IDs | Handler | LLVM Intrinsic |
|---|---|---|---|
| `__wgmma_fence` | 745–750 | `sub_12B1C20` | 9062 (`wgmma.fence.aligned`, 3 type overloads) |
| `__wgmma_commit_group` | (same range) | `sub_12B1C20` | (same dispatch) |
| `__wgmma_wait_group` | (same range) | `sub_12B1C20` | (same dispatch) |

WGMMA fences synchronize the tensor core accumulator pipeline; TMA mbarriers synchronize the DMA engine. A typical Hopper kernel pipelines both: TMA loads data into shared memory (mbarrier-synchronized), then WGMMA consumes the data from shared memory (fence-synchronized). The two synchronization domains must not be confused in a reimplementation.

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

| Function | Address | Size | Role |
|---|---|---|---|
| `sub_CD09E0` | `0xCD09E0` | NVVM arch enum (`NVVM_ARCH_HOPPER_9_0`) | NVVM arch enum (`NVVM_ARCH_HOPPER_9_0`) |
| `ctor_356` | `0x50C890` | Subtarget registration (sm_90 enum 38, sm_90a enum 39) | Subtarget registration (sm_90 enum 38, sm_90a enum 39) |
| `sub_214DA90` | `0x214DA90` | Kernel attribute emitter (cluster PTX directives) | Kernel attribute emitter (cluster PTX directives) |
| `sub_21E9060` | `0x21E9060` | Cluster special register PTX emission | Cluster special register PTX emission |
| `sub_21E8EA0` | `0x21E8EA0` | Cluster barrier instruction emission | Cluster barrier instruction emission |
| `sub_21E94F0` | `0x21E94F0` | Membar/fence printer (`fence.sc.cluster`) | Membar/fence printer (`fence.sc.cluster`) |
| `sub_BFC6A0` | `0xBFC6A0` | setmaxnreg NVVM IR validation | setmaxnreg NVVM IR validation |
| `sub_FCDCB0` | `0xFCDCB0` | setmaxnreg inline asm pattern matching | setmaxnreg inline asm pattern matching |
| `sub_955A70` | `0x955A70` | WGMMA lowering (M-dimension switch) | WGMMA lowering (M-dimension switch) |
| `sub_90AEE0` | `0x90AEE0` | Builtin registration (WGMMA, cluster barriers/queries) | Builtin registration (WGMMA, cluster barriers/queries) |
| `sub_A8E250` | `0xA8E250` | TMA intrinsic name parsing (12 KB native) | TMA intrinsic name parsing (12 KB native) |
| `sub_33AD3D0` | `0x33AD3D0` | TMA SelectionDAG lowering handler (modes 2/3/5/7) | TMA SelectionDAG lowering handler (modes 2/3/5/7) |
| `sub_33AB690` | `0x33AB690` | `cp.async.bulk` non-tensor handler | `cp.async.bulk` non-tensor handler |
| `sub_33AC2B0` | `0x33AC2B0` | `cp.async` handler | `cp.async` handler |
| `sub_33AC130` | `0x33AC130` | `cp.async.wait` handler | `cp.async.wait` handler |
| `sub_36EC510` | `0x36EC510` | CpAsyncBulkTensor G2S lowering (6 KB native; 1,185 lines decomp) | CpAsyncBulkTensor G2S lowering (6 KB native; 1,185 lines decomp) |
| `sub_9483E0` | `0x9483E0` | TMA descriptor extraction | TMA descriptor extraction |
| `sub_12AA280` | `0x12AA280` | TMA descriptor builder (EDG -> LLVM IR) | TMA descriptor builder (EDG -> LLVM IR) |
| `sub_12A7070` | `0x12A7070` | TMA scatter/store builtin handler | TMA scatter/store builtin handler |
| `sub_8D4C10` | `0x8D4C10` | `edg::get_tma_descriptor_flags` | `edg::get_tma_descriptor_flags` |
| `sub_35F4B50` | `0x35F4B50` | DSMEM qualifier emission | DSMEM qualifier emission |
| `sub_35F4E30` | `0x35F4E30` | Commit modifier emission (mbarrier, multicast) | Commit modifier emission (mbarrier, multicast) |
| `sub_35F4AD0` | `0x35F4AD0` | `.mbarrier_init` emission | `.mbarrier_init` emission |
| `sub_35F4080` | `0x35F4080` | Secondary `.shared::cluster` emission | Secondary `.shared::cluster` emission |
