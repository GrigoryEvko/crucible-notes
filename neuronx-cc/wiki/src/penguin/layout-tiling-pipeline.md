# Layout-Tiling Pipeline (the "Tensorizer" Tile Transform)

> *All symbols and offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/starfish/penguin/targets/transforms/`). Every offset is **module-relative** to the named `.cpython-310-…so`; other wheels and Python ABIs (cp311/cp312) re-base. Treat every address as version-pinned.*

## Abstract

This is the Penguin middle-end pass that turns **whole-tensor** operations into **TPB-tile-sized sub-computations** — the stage everyone informally calls the *Tensorizer*. It is the first transform that physically decides how a tensor is laid across the **128 SBUF/PE partitions** and chopped into in-tile free vectors. If you have built an affine loop-tiler before, the mental anchor is: this is a *cut-based* tiler over an affine axis algebra, not a polyhedral scheduler — it ranks axes by a global priority, then makes two greedy cuts (a partition cut and a free/block cut) and emits a loop nest around what is left over.

The driver chain is three nested SuperPass pipelines: `PGLayoutTilingPipeline` (the modern top-level driver, `LayoutTilingPipeline` module `__init__` @ `0x16870`) wraps layout analysis and PAG layout selection around `PGTiling` (@ `0x109b0`), which is the graph→tile worker pipeline. Inside `PGTiling`, two passes do the actual cutting — `PComputeCutting` ("compute tiling of the **partition (P)** dimensions") drives `DAGTiler.tile_dag_par_axes`, and `BFComputeCutting` ("compute tiling of the **block (B) and free (F)** dimensions") drives `DAGTiler.tile_dag_free_axes`. All tile-shape arithmetic bottoms out in two primitives in `PGTilingHelpers`: `computeCut` (@ `0xba670`) picks which dim to cut and how big the tile is, and `cut_axes` (@ `0xbd9a0`) mechanically splits one axis list into *loop axes* (outside the tile) and *tonga axes* (in the tile). The output is a `TiledDAG`; the inter-tile dependency graph is built by `PGAnalysisForTiling`.

> **GOTCHA — "Tensorizer" names an MLIR stage, not a Penguin transform.** The `Tensorizer` name belongs to the `hlo2penguin` passes `CanonicalizeForTensorizer` and `TensorizerLegalizationPass` ([4.31](../hlo-opt/canonicalize-for-tensorizer.md) / [4.32](../hlo-opt/tensorizer-legalization.md)), which prepare MHLO for the Penguin dialect and do **not** tile. `penguin/targets/transforms/` holds `PComputeCutting`, `BFComputeCutting`, `PGTilingHelpers`, `LayoutTilingPipeline` and `PGAnalysisForTiling` — but no `Tensorizer*` module, so the collision is purely lexical.

For reimplementation, the contract is:

- The **driver composition** — `PGLayoutTilingPipeline → PGTiling → {AGOrdering, P-cut, BF-cut, LoopSplitting, MacroGeneration}` and which analyses feed the tiler.
- The **cut representation** — `DimCut(cut_dim, cut_tile_size)`, the right-to-left budget scan of `computeCut`, and the `(loop_axes, tonga_axes)` split of `cut_axes` including `swap_tile` reverse-tiling.
- The **partition cut** (`PComputeCutting`, P ≤ 128) and the **free/block cut** (`BFComputeCutting`, the sliding F↔B cut sized to one PSUM bank), plus the overflow escape hatches (decay-to-loop, vectorize-to-free, delinearize, nonlocal).
- The **dependency model** — how `PGAnalysisForTiling` groups DAGs into partition groups and records `dag_to_producer_edges` / `dag_to_consumer_edges` across TC/Transpose split-DAG boundaries for the downstream scheduler.

| | |
|---|---|
| **Top-level driver** | `PGLayoutTilingPipeline.__init__` @ `0x16870` (legacy: `OrigLayoutTilingPipeline` @ `0x136f0`) |
| **Core tiler pipeline** | `PGTiling.__init__` @ `0x109b0` — `AGOrdering → PComputeCutting → BFComputeCutting → LoopSplitting → MacroGeneration` |
| **Partition cut pass** | `PComputeCutting.transformStmts` @ `0x277d0` → `DAGTiler.tile_dag_par_axes` @ `0x9ad80` |
| **Free/block cut pass** | `BFComputeCutting.transformStmts` @ `0x155f0` → `DAGTiler.tile_dag_free_axes` @ `0x40d80` |
| **Cut primitives** | `computeCut` @ `0xba670`, `cut_axes` @ `0xbd9a0`, `compute_cut_params` @ `0x794b0` (all `PGTilingHelpers`) |
| **Cut representation** | `DimCut(cut_dim, cut_tile_size)` namedtuple |
| **Output struct** | `TiledDAG{loop_axes, partition_axes, free_axes, decayed_partition_axes, extend_loop_axes}` |
| **Dependency analysis** | `PGAnalysisForTiling` — `PartitionGroup`, union-find PGs, producer/consumer edges |
| **HW budgets (runtime Target attrs)** | `num_partitions`/`statebuf_num_partitions` = 128, `psum_par_fp32_per_bank` = 512, default `cut_size` = 512 |
| **IR level** | Penguin IR DAGs (post-`hlo2penguin`, pre-BIR lowering) |

## The Driver Composition

### Purpose

`PGLayoutTilingPipeline` is the modern top-level driver that sandwiches the core tiler between layout analysis (which decides *which* axis becomes the partition axis) and post-tiling cleanup (transposes, large-tensor offload). It is a Cython `SuperPass` subclass: its `__init__` builds an ordered sub-pass list via `PassConstructorBuilder().build_pass_constructor(<Pass>, required|optional, …)`, and the **order of pass references in the decompiled `__init__` is the emitted pass order**.

### Entry Point

```text
PGLayoutTilingPipeline.__init__  (LayoutTilingPipeline @ 0x16870)
  ├─ LowerCCOpBlockAxis              (optional)  lower collective block axes
  ├─ LayoutPreprocessingAndAnalysis  @ 0xd9d0  → {LayoutPreprocessing, LayoutRequirementAnalysis}
  ├─ InferNonlocalTensors           (optional)  mark tensors that can't fit on-chip
  ├─ PAGLayoutOpt                   @ 0xf1c0   → {ParAxesAnnotation, InsertLocalTransposes}
  ├─ ShardingPropagationAnalysis    (optional)
  ├─ InferShardAxis                 (optional)
  ├─ MaskPropagation                (optional)
  ├─ CanonicalizeDAGForPGTiling     (optional)  DAG cleanup right before tiling
  ├─ PGTiling                       @ 0x109b0  <<< THE CORE TILER
  ├─ InsertIOTransposes
  ├─ DemoteLargeTensors
  ├─ InsertOffloadedTransposes
  ├─ DramToDramTranspose
  └─ LowerCCOpBlockAxis             (optional)
```

### Algorithm

`PGTiling` is the graph→tile worker pipeline. Its six sub-passes are, in source order:

```c
function PGTiling_init():                       // LayoutTilingPipeline @ 0x109b0
    add(AGOrderingAnalysisPass)                 // compute GLOBAL axis-group (AG) ordering /
                                                //   per-dim priority the cuts rank against (§ Axis Ordering)
    add(StaticTransposeLocalTensor, required)   // materialize transposes the chosen tile layout needs
    add(PComputeCutting, required)              // PARTITION cut: tile_dag_par_axes → P-axis ≤ 128
    add(BFComputeCutting, required)             // BLOCK+FREE cut: tile_dag_free_axes → F-tile ≤ PSUM bank
    add(LoopSplitting, required)                // split the generated tile loops
    add(MacroGeneration, required)              // emit the per-tile macro / loop-nest body
```

This is the operational answer to *how whole-tensor ops become tile-sized sub-computations*: `AGOrdering` ranks axes; `PComputeCutting` cuts the partition axis to ≤ 128; `BFComputeCutting` slides the free/block cut to fit SBUF/PSUM; `LoopSplitting` + `MacroGeneration` emit the tile loop-nest.

> **QUIRK — the partition cut and the free cut are two separate passes, deliberately.** `DAGTiler.tileDAG` (@ `0x5f240`) exists and does "tile both P and BF" in one shot, yet `PGTiling` splits them into `PComputeCutting` *then* `BFComputeCutting`. The split is so the backend can interleave layout/transpose passes (e.g. `StaticTransposeLocalTensor`, `InsertLocalTransposes`) **between** the partition cut and the free cut — the partition layout must be fixed before the free dims can be blocked against it. A reimplementation that fuses both cuts loses that interleave point.

### The legacy path

`OrigLayoutTilingPipeline.__init__` (@ `0x136f0`) is the pre-partition-group driver, selected by target/flags (`enable_ipo`, `enable_ccop_compute_overlap`, …). It tiled via `SundaSizeTiling` — an old *backend-style size-fit* pass — rather than partition-group-aware cutting:

```text
OrigLayoutTilingPipeline.__init__  (@ 0x136f0)
  AliasDependencyElimination → IPGlobalLayoutOpt(enable_ipo) → LowerCCOpBlockAxis(ccop flags)
  → SundaSizeTiling  <<< OLD size-tiling → CanonicalizeDAG → FlattenAxesForTiling
  → NeuronAliasDependencyInduction → GlobalLayoutOpt
```

> **NOTE —** `SundaSizeTiling` (and the libwalrus `SBSizeLegalization`, [8.x](../walrus/)) are **size-fit** passes that split *already-laid-out* tensors to fit a hardware byte budget. The PG tiler here is the **graph-level** tiler that first decomposes whole-tensor ops into the partition×free loop nest. Distinct stages — see [the Boundary section](#boundary--graph-tiler-vs-backend-size-fit).

## The Cut Representation

### Purpose

Every tile-shape decision is expressed as a `DimCut` — *which* dimension to cut and *how big* the in-tile slice is — and applied by `cut_axes`, which splits one axis list into the part that loops and the part that stays in the tile. Two glossary terms recur:

- **loop axis** — the outer axis left *outside* the tile (the tile-loop trip dimension).
- **tonga axis** — the axis kept *within* the tile ("tonga" = the TPB/Tonga ISA target; the in-tile resident dims).

### Algorithm — `computeCut`

`computeCut(shape, cut_size, stop_condition=…, uniform_tile=False) -> DimCut` (@ `0xba670`) chooses the cut dim and the tile size on that dim. `cut_size` is a **per-tile element budget** (default 512). The recovered docstring states the priority rule: *"Left dimensions are higher priority to fit into the cut … Return None for cut dim if we can fit the whole cut dim within tensor and don't need to cut."*

The body arithmetic is compiled-opaque, so the scan below is reconstructed from the shipped docstring worked-examples rather than read off the code; those examples pin the output for every case, which makes the behaviour fully specified even though the instructions are not:

```c
function computeCut(shape, cut_size=512, uniform_tile=False):   // @ 0xba670
    // shape is PRIORITY-ORDERED: index 0 = highest priority (must stay whole),
    //   rightmost = lowest priority = natural in-tile contiguous dim.
    if product(shape) <= cut_size:
        return DimCut(cut_dim=None, cut_tile_size=None)   // whole tensor fits one tile
    committed = 1                                          // product of higher-priority (left) dims
    for i in range(len(shape)):                            // scan high→low priority (left→right)
        if committed * shape[i] overflows cut_size:        // this dim is where the budget runs out
            tile = floor(cut_size / committed)
            if tile == shape[i] or tile <= 1:
                return DimCut(cut_dim=i, cut_tile_size=None)  // take whole dim; no split needed
            if uniform_tile: tile = largest_divisor_of(shape[i]) <= tile  // avoid masked tail
            return DimCut(cut_dim=i, cut_tile_size=tile)
        committed *= shape[i]
    return DimCut(cut_dim=None, cut_tile_size=None)
```

The five verbatim examples that pin this (each is a docstring `>>> computeCut(...)` line):

| `computeCut(shape, cut_size=512)` | `→ DimCut` | Why |
|---|---|---|
| `[4, 2, 2, 2, 128]` | `(cut_dim=4, cut_tile_size=16)` | budget hits dim 4; `512 // (4·2·2·2)=16` of it fits |
| `[4, 2, 2, 2, 8]` | `(None, None)` | `4·2·2·2·8 = 256 ≤ 512` → no cut at all |
| `[8192, 2]` | `(cut_dim=0, cut_tile_size=512)` | budget hits dim 0; `512 // 1 = 512` |
| `[448, 16]` | `(cut_dim=0, cut_tile_size=None)` | `512 // 448 == 1` → only one tile fits, take whole dim |
| `[16, 8192]` | `(cut_dim=1, cut_tile_size=32)` | budget hits dim 1; `512 // 16 = 32` |
| `[]` | `(None, None)` | empty shape |

> **GOTCHA — `cut_tile_size=None` does not mean "no cut".** It means "cut on this dim but take the *whole* extent into the tile" (e.g. `[448,16] → cut_dim=0, cut_tile_size=None`). Only `cut_dim=None` means "no cut at all". A reimplementation that treats both `None`s as the same will mis-tile any dim that exactly fills the budget once.

### Algorithm — `cut_axes`

`cut_axes(axes, cut_idx, tilesize, swap_tile, tiled_axes_mapping=None) -> (loop_axes, tonga_axes)` (@ `0xbd9a0`) is the mechanical split. The recovered docstring: *"Cut a list of axes into loop axes (outside the tile) and tonga axes (within the tile)."*

```c
function cut_axes(axes, cut_idx, tilesize, swap_tile):   // @ 0xbd9a0
    if cut_idx is None:                  // no cut → everything is in-tile
        return ([], axes)
    pre  = axes[:cut_idx]                // dims left of the cut → loop (outer)
    post = axes[cut_idx+1:]              // dims right of the cut → tonga (in-tile)
    A = axes[cut_idx]
    if tilesize is None:                 // take the whole cut axis into the tile
        return (pre, [A] + post)
    outer, inner = split(A, tilesize)    // extent E → outer trip E/tilesize, inner extent tilesize
    if swap_tile:                        // REVERSE tile: high-order factor is the in-tile one
        return (pre + [inner], [outer] + post)
    else:
        return (pre + [outer], [inner] + post)
```

Verbatim examples (loop/tile split semantics):

```text
cut_axes([2,4,8], cut_idx=1, tilesize=2, swap_tile=False) → ([2,8], [2,2])   # inner 2 in-tile
cut_axes([2,4,8], cut_idx=1, tilesize=2, swap_tile=True)  → ([2,2], [2,8])   # outer 2 in-tile (reverse)
cut_axes([2,4,8], cut_idx=1, tilesize=None)              → ([8],   [2,4])   # whole axis in-tile
cut_axes([2,4,8], cut_idx=None, ...)                     → ([],    [2,4,8]) # whole DAG in tile
```

`compute_cut_params` (@ `0x794b0`) is the parameter bundle threaded into the tiler. Its fields — recovered as `__pyx` constant names — are `par_axes_cut_idx`, `par_axes_cut_size`, `free_axes_cut_idx`, `free_axes_cut_size`, `lhs_free_cut_idx`, `lhs_free_cut_tilesize`, `rhs_free_cut_idx`, `rhs_free_cut_tilesize`, `uniform_tile`, `uniform_tile_rhs_free`, `default_free_cut_size`, `free_cut_size`, `cut_size`, `stop_condition`, `override_tile_size`. It returns `Optional[Dict[DAG, DimCut]]` — a **per-DAG** cut decision map. The separate `lhs_*`/`rhs_*` free-cut params exist because a matmul's two operands have independent free dims feeding one PSUM accumulator (see [Matmul path](#matmul--tensor-contract-path)).

> **NOTE —** `cut_size` default = 512 is the per-tile *free* element budget for non-matmul DAGs, overridable via the compiler option `override-pg-tile-size` (flag string: *"use a specific tilesize for all non-MM dags in PG tiling (default is 512)"*). Matmul DAGs ignore this and size their free tile to one PSUM bank (`psum_par_fp32_per_bank`) instead — which is *also* 512 fp32, so the two budgets coincide numerically but come from different attributes.

## Axis Ordering — Ranking Dims Before the Cut

### Purpose

Before any cut, every axis list is **priority-ordered**. The repeated invariant (appears 3×): *"axes lists are always ordered from higher prio to lower prio!!"*. The cut primitives rely on this — `computeCut`'s left-to-right scan assumes index 0 is highest priority.

### Algorithm

The priority comes from `DAGTiler.orderAxes` / `getAxisForOrdering` (@ `0x9ca…` in `PGTilingHelpers`) plus `PGMetrics.orderAxesByGlobalAgOrdering`. The recovered `axisOrderingMetric` docstring: *"higher metric means higher priority, which means a dim should be more to the right … this applies to F dims, P dims, and B dims. from the AG GlobalOrderer, higher AG index also means higher priority."* The scoring rule: *"higher score means higher priority. higher priority axes: occur more to the right in tensor accesses; are more likely to be tiled as F; within loop axes and within free/par axes, higher prio axes have larger loop depth."*

Priority is the product of three factors:

1. **Tensor-access position** — rightmost = contiguous = highest priority (it is the natural in-tile vector dim).
2. **Global AG index** — from `AGOrderingAnalysisPass`; higher AG index = higher priority.
3. **Loop depth** — deeper = higher priority.

Higher-priority dims are placed to the *right* so they become in-tile contiguous F dims; lower-priority dims drift left and become outer loop axes.

> **QUIRK — the orderer is class-blind.** Its disclaimer: *"this function doesn't distinguish between P/B/F. It is assumed that when we order axes with this function, all axes are exclusively P, exclusively F, or exclusively B."* The tiler must pre-partition axes into single-class lists before calling it. The `TiledDAG` ordering contract reflects this: *"ordering for free_axes and par_axes don't matter, b.c. we will call orderAxesByGlobalAgOrdering for them. ordering for loop_axes should respect the original loop ordering."* — par/free axes get re-sorted globally; loop axes preserve source order.

### Reverse-tile candidacy

`DAGTiler.is_reverse_tile_candidate` and `PartitionGroup.is_reverse_tile_par_dim_candidate` (@ `0x2fe20`) feed `swap_tile=True` into `cut_axes`, so the in-tile factor is the *high-order* half of a split dim. This is used when the partition layout requires the outer block to be vectorized. The `PComputeCutting` assert *"Must be reverse tilable only in case where partition axis candidate is not pre-tiled"* gates it: reverse-tile is set only when the partition axis is not already tiled. The precise cost decision lives in `BaseTilingEstimator.should_swap_tile` ([Cost section](#tile-shape-cost-estimation)).

## The Partition Cut — Fitting the 128-Partition SBUF

### Purpose

`PComputeCutting` ("compute tiling of the partition (P) dimensions", `transformStmts` @ `0x277d0`) drives `DAGTiler.tile_dag_par_axes` (@ `0x9ad80`). The partition (P) axis maps to the 128-wide SBUF/PE-row dimension; this pass guarantees the in-tile partition extent is ≤ `num_partitions` and aligned to `partition_multiple`.

### Algorithm

```c
function tile_dag_par_axes(dag, cut_params):           // DAGTiler @ 0x9ad80, driven by PComputeCutting
    p_axes = order_par_axes(dag)                        // single-class P list, high→low prio
    cut    = computeCut(extents(p_axes),                // pick the partition cut
                        cut_size = num_partitions,      // = 128 (statebuf_num_partitions)
                        ...)                             // aligned to partition_multiple
    (loop, tonga) = cutParAxes(p_axes, cut.cut_dim,     // @ 0x9db90 → cut_axes
                               cut.cut_tile_size,
                               swap_tile=is_reverse_tile_candidate(dag))
    // tonga = in-tile partition axis (≤128 → maps to SBUF partitions / PE rows)
    // loop  = outer partition-block trip (one tile per 128-partition block)
    return TiledDAG(loop_axes=loop, partition_axes=tonga, ...)
```

If the flattened partition extent exceeds 128, the outer factor becomes a **loop axis** (a trip over partition blocks) and the inner factor (≤ 128) stays the in-tile partition axis.

### Overflow handling

Two recovered methods handle the case where the partition dim cannot be cleanly cut to 128:

- **`DAGTiler.can_vectorize_par_overflow_to_free`** (@ in `PGTilingHelpers`) — docstring: *"return whether we allow vectorizing overflowed partition axes into free axes, when there are no free axes."* When the partition extent overflows 128 but the op has **no free axes** to loop over, the tiler spills the >128 overflow into a *free/vector* dimension instead of a partition-block loop. Guarded by `DAGTiler.can_be_free` (@ `0xa…`) and the next method.
- **`DAGTiler.has_mixed_pf_for_non_delinearizable_dim`** — docstring: *"return if there is a non-transposable tensor access in dag which has a PF-mixed dimension that cannot be delinearized."* A single tensor dim that mixes Partition and Free index components ("PF-mixed") must be delinearized into separate P and F factors before tiling; if a non-transposable access blocks that, tiling of the axis is blocked. This couples to the delinearizers (`FactorizeFreeDims`/`FactorizeBlkDims`/`TensorTileDelinearizer`).

> **GOTCHA — `partition_multiple` is a hard alignment, not advisory.** The in-tile partition extent must be a multiple of `partition_multiple` (a per-arch Target attr). A tile of, say, 96 partitions on an arch requiring multiples of 32 is legal; 100 is not. This is read from the runtime Target — **no literal 128 or alignment value is baked into `PGTilingHelpers.so`**; only the attr *name* appears as a `__pyx_n_s_` string (see [HW geometry](#hardware-geometry-the-tiler-fits-to)).

## The Free / Block Cut — The Sliding F↔B Cut

### Purpose

`BFComputeCutting` ("compute tiling of the block (B) and free (F) dimensions", `transformStmts` @ `0x155f0`) drives `DAGTiler.tile_dag_free_axes` (@ `0x40d80`). It blocks the free dims to fit the PSUM-bank / 512-element budget, splitting each ordered free-axis list at a **sliding cut**: everything left of the cut is FREE (in-tile vector), everything right is BLOCK (outer loop block).

### Algorithm

```c
function tile_dag_free_axes(dag, cut_params):          // DAGTiler @ 0x40d80, driven by BFComputeCutting
    f_axes = order_free_axes(dag)                       // single-class F/B list, high→low prio
    // REDUCE free axes are protected — they must stay whole so the in-tile reduction completes
    cut    = computeCut(extents(non_reduce(f_axes)),
                        cut_size = free_axes_cut_size)   // = PSUM bank (512 fp32) for MM; 512 default else
    (loop, tonga) = cutFreeAxes(f_axes, cut.cut_dim,     // @ 0xa1830 → cut_axes
                                cut.cut_tile_size, swap_tile=...)
    align_cut_point_across_PG(dag)                       // make the cut consistent inside the PG
    return TiledDAG(loop_axes += loop, free_axes=tonga, ...)
```

The recovered cut-placement policy makes the F↔B continuum explicit: *"a cut is further to the left if it has more F and less B; a cut is further to the right if it has less F and more B. for example, for [a, b, c], the leftmost cut will pick all of a,b,c as free; the rightmost cut will pick all of a,b,c as block."* And the per-PG alignment: *"First cut free axes for each dag in a PG separately, then try to make the cutting point consistent inside a PG to preserve solution space for loop fusion."*

> **QUIRK — only non-reduce (batch) free axes are cut.** The docstring is explicit: *"Only non-reduce free axes (batch) are subject to BFComputeCutting's cutFreeAxes."* A **reduce** free axis must stay whole within a tile so the in-tile reduction is complete — cutting it would split a single reduction across tiles, forcing a partial-sum accumulator. This is why the "B" in BF means *batch* (a non-reduce free axis), not "block-of-anything".

> **NOTE — the per-PG cut alignment is what keeps tile loops fusable.** The cut is chosen per DAG, then dragged to a consistent point across the whole partition group. Two DAGs with the same cut point have congruent tile loop nests, so the scheduler can fuse them. A reimplementation that cuts each DAG independently will tile correctly but destroy loop-fusion opportunities downstream.

## The TiledDAG Output and Loop-Nest Generation

### Purpose

`TiledDAG` is the output of one tiling. Its axis-list properties (all confirmed as `__pyx` accessors in `PGTilingHelpers`) are the loop nest the lowering emits:

| Property | Meaning |
|---|---|
| `partition_axes` | in-tile P axes (≤ 128, map to SBUF partitions / PE rows) |
| `free_axes` | in-tile F axes (contiguous, sized to the cut budget) |
| `loop_axes` | outer tile-loop trip axes (the emitted loop nest) |
| `decayed_partition_axes` | P axes demoted to loop/free (overflow handling) |
| `extend_loop_axes` | extra loop axes appended when a P/F overflow becomes a new outer loop |
| `update_axes` / `__repr__` | mutation + debug |

### Algorithm

The emitted loop nest around each tile is exactly:

```c
for li in loop_axes:                 // outermost, ordered by source loop order
    tile_body over partition_axes × free_axes    // P = SBUF-partition dim; F = in-tile vector dims
```

`decayed_partition_axes` + `extend_loop_axes` are how a >128 partition extent or an over-budget free extent becomes *additional* outer-loop iterations. The construction precondition: *"Prerequisite: loop_axes / partition_axes / free_axes MUST BE SORTED AND DETERMINISTIC."*

A second cutting pass can resume from a partition-tiled DAG without re-sorting via `DAGTiler.from_tiled_dag` (@ in `PGTilingHelpers`): *"Reconstruct a DAGTiler from a TiledDAG / NOTE: we don't want to sort the axes here."* This is exactly why `BFComputeCutting` can run after `PComputeCutting` on the partition-tiled DAG. After a cut renames/splits an axis, `DAGTiler.generateReverseLoopMapping` rebuilds the loop-index mapping (old axis → new split axes) so downstream accesses and the scheduler see a consistent index space.

### Delinearization

A flat dim that cannot be tiled as-is is split into P×F factors first. `TensorTileDelinearizer.run` performs the mod/div split; the affine encoding is the form *"16i0_1 + i0_0 + 4"* (outer index `i0_1` scaled by tilesize 16, inner `i0_0`, offset 4). The standalone `try_delinearize_huge_1d_tensors` (recovered scope-struct @ in `PGTilingHelpers`): *"if a 1d tensor is huge, we try to delinearize it so we can spread it across P and F. if we are not able to delinearize it, set it as nonlocal tensor because it cannot fit."*

> **GOTCHA — nonlocal is the on-chip-fit escape hatch.** When a tensor cannot be factored into a tileable P×F shape, it is marked **nonlocal** (kept in DRAM, streamed) rather than tiled. This couples back to `InferNonlocalTensors` in the top-level driver. A reimplementation that assumes every tensor tiles on-chip will deadlock on a huge prime-extent 1-D tensor.

### Matmul / tensor-contract path

`TCDagTiler` plus `DAGTiler.tileTCDAG` / `tile_tc_dag_par_axes` / `tile_tc_dag_free_axes` specialise matmul:

- Separate **LHS and RHS** free-cut params (`lhs_free_cut_idx/tilesize`, `rhs_free_cut_idx/tilesize`, `uniform_tile_rhs_free`) — the two operands have independent free dims feeding one PSUM accumulator.
- `is_tile_axis_all_in_contract` checks whether a tile axis lies entirely in the contraction (K) dim, which must be **accumulated**, not looped-with-spill.
- The free tile is sized to the PSUM bank (`psum_par_fp32_per_bank`) rather than the 512 generic default.
- Legality assert: *"LHS / RHS partition axes mismatch for tensor contract"* — the LHS and RHS stationary partition axes must agree.

### Reduction and quant specializations

- `is_reduce_axis` / `is_partition_reduce_op_dag` / `par_reduce_axes` / `non_reduce_par_axes` classify reduce axes: a partition reduce (along the 128-dim) needs a PE/partition reduce; a free reduce stays in-tile.
- `DAGTiler._should_cascade_reduce` + `CascadedReductionDagsTilingEstimator` — a reduction too large for one tile is split into a **cascade** of partial reduces (tree reduction); the estimator compares `cascaded_reduce_add_cost` vs `pe_reduce_add_cost`.
- `BNMeanVarDAGTiler` (`tileDAG` @ in `PGTilingHelpers`) and `BNGradientDAGTiler` split batchnorm mean/var and gradient DAGs into stats + aggregation sub-DAGs (`insertBNStatsDAG` / `insertBNAggrDAG` / `splitBNGradDAG`).
- `QuantizeMXDAGTiler` — *"DAGTiler for QuantizeMX DAGs that protects reduce_free_axes from tiling"* (MX block-scaled quant keeps its 32-element scaling block whole).

## Tile-Shape Cost Estimation

### Purpose

`computeCut` picks a *candidate* cut; the `BaseTilingEstimator` family **ranks** candidates by tripcount cost and chooses the cheapest. It is a template-method pattern.

### Algorithm

```c
function BaseTilingEstimator_estimate(candidate):   // PGTilingHelpers
    axes = _clone_axes(candidate)        // deep-copy so estimation never mutates the real DAG
    cost = _do_estimate(axes)            // abstract; subclass computes tripcount cost
    swap = should_swap_tile(candidate)   // decide reverse-tile (§ reverse-tile candidacy)
    return (cost, swap)
// the tiler keeps the lowest-cost (swap or no-swap) choice
```

The recovered docstrings: `estimate` / `_do_estimate` — *"Template method that handles axis cloning and cleanup."*; `_do_estimate` (abstract) — *"Perform the actual estimation. Must be implemented by subclasses."*; `_clone_axes` deep-copies axes (cf. the recurring note *"Need to deepcopy axes since we don't want to cut the original axes"*).

### Cost currency — tripcounts

Cost is the product of generated loop trip counts (number of tiles) plus reduce overhead. The estimator's outputs are the `__pyx` constants `f_tripcount_after_tiling`, `inner_tile_tripcount`, `outer_tile_tripcount`, `accum_tripcount`, `loop_red_tripcount`, `decayed_red_axes_tripcount`, `partition_reduce_tiled_tripcount`, `cascaded_reduction_dags_tiled_tripcount`, `dag{1,2}_tiled_tripcount_{p,f,b}`, all capped by `max_tripcount`. The estimator minimises total tiles / reduce overhead subject to PSUM/SBUF fit — an objective read from the constant names, since the cost-formula body itself is compiled-opaque. Specialised estimators: `PartitionReduceTilingEstimator` (assert *"Exactly a single aggr idx axis of tripcount 2 must be unmapped"*) and `CascadedReductionDagsTilingEstimator` (*"Mock cascaded reduction dag tiling based on ordering information"*).

### PSUM-overflow abort

The per-axis legality check (`TensorTileDelinearizer.can_tile` / `ModDivDelinear` path), docstring verbatim:

> *"This method is used to exclude axes that cannot be tiled without overflowing PSum. ModDivDelinear calls this method for each axis when tiling a set of tensor accesses. If this method returns False for any of the axes we abort tiling. … E.g. tiling i0 into 16i0_1 + i0_0 + 4 corresponds to a tilesize of 16 with offset 4."*

If any axis's tile would overflow PSUM, that tiling choice is rejected and the estimator tries another.

## Partition-Group Analysis — The Dependency Substrate

### Purpose

`PGAnalysisForTiling` ("Partition Group analysis specific to tiling") builds the structure the tiler operates on *and* records cross-tile dependencies for the backend scheduler. The defining docstring:

> *"matching axes: 2 axes in different loopnests 'match' if they access the same dim of a tensor. axes_group: recursively find all matching axes from different loopnests and group them together. PartitionGroup (PG): all dags in a PG has matching sets of partition axes. TilingAxesGroup (AG): find all axes_group within each PG. TensorContract DAG (TC DAG): DAG that contain TensorContractOp (matmult). Transpose DAG: DAG that perform PF Transpose on a tensor. Normal DAG: DAG that is not TC or Transpose. Normal DAG only uses 1 set of layout within the DAG. TC/Transpose DAG uses exactly 2 sets of layouts within the DAG."*

### Algorithm — PG / AG construction

```c
function PGAnalysisForTiling(graph):     // PGAnalysisForTiling module
    buildPartitionGrps(graph):           // union-find on "matching" partition axes
        for each pair of DAGs with matching partition axes:
            union(dag_a, dag_b)          // → maximal PG sharing ONE partition layout
        // "all other dags are in exactly 1 PG (guaranteed by union find)"
    for each PG:
        calculateAGs(PG)                 // group matching FREE/PAR axes across loopnests → AGs
        // AG global ordering (from AGOrderingAnalysisPass) gives the right-priority order
        // "WARNING: primary partition AG within PG should not be batch or BF reduce"
    split_TC_Transpose_DAGs():           // each crosses a layout change
        // SRC_DAG (producer-side layout) + DST_DAG (consumer-side layout)
        // → one DAG lives in 2 PGs: producer PG (src) and consumer PG (dst)
```

A `PartitionGroup` is the maximal set of DAGs whose partition axes all *match* (access the same tensor dim), found by **union-find**. All DAGs in one PG share one partition layout, so they tile with the same 128-partition mapping. TC/Transpose DAGs change the partition layout, so they are split at the layout boundary into SRC_DAG and DST_DAG, bridging two PGs.

### Producer / consumer edges

`PGAnalysisForTiling.buildPGRelationGraph` builds the PG relation graph; `PartitionGroup.enumerate_loadstore_edges` enumerates the load/store edges. The recovered data structures (`__pyx_n_s_` constants) handed to the backend scheduler:

| Edge structure | Records |
|---|---|
| `pg_relation_graph` | the graph of PG-to-PG dependencies |
| `dag_to_producer_edges` | for each DAG, which DAG(s) produce its inputs |
| `dag_to_consumer_edges` | for each DAG, which DAG(s) consume its outputs |
| `pg_loadstore_edges` / `loadstore_edges` | the tensor load/store edges (the actual data deps) |
| `ag_to_consumer_pgs` | which consumer PGs read each AG |
| `source_dags` / `sink_dags` | per-PG hand-off points across a layout boundary |
| `include_nonlocal_edges` | also track deps through DRAM (nonlocal) tensors |

The source/sink definition (verbatim): *"PG source_dags: TC/Transpose DAGs whose DST partition layout matches PG partition layout. PG sink_dags: TC/Transpose DAGs whose SRC partition layout matches PG partition layout. note1: a TC/Transpose DAG can be both source and sink for a PG. note2: each Normal DAG maps exactly to 1 PG; for TC/Transpose DAG, SRC maps exactly to 1 PG, and DST maps exactly to 1 PG."*

The tile-level dependency model: tiles within a PG share a partition layout and tile loop space; data flowing *between* PGs crosses a TC/Transpose split-DAG, and those producer→consumer edges are the inter-tile dependencies recorded for the downstream scheduler. The edge structures themselves are read directly from the module; that the scheduler is their consumer is [INFERRED] from `PartitionGroup.finalize` @ `0x23870` and `PGAnalysisForTiling.finalize` emitting them, and is not traced into a scheduler-side reader.

> **NOTE — cross-PG reverse-tile legality.** `PartitionGroup.is_reverse_tile_par_dim_candidate` (@ `0x2fe20`) constrains reverse-tiling across PGs: an axis reverse-tiled in one PG forces a matching reverse-tile in its complement PG, and is forbidden if it would make the access-pattern of the same axis inconsistent elsewhere (*"only tile to the right when single partition axis; reverse tiling multiple par axes to the right can cause AP inconsistencies"*). `mapPG2Color` / `assignColorToNodes` graph-color each PG so co-tiled / conflicting PGs are distinguishable.

## Hardware Geometry the Tiler Fits To

The PG tiler does **not** hard-code 128 / 512. It reads them as attributes of the per-arch Target model, resolved at runtime — which is why **no literal 128 or 512 appears in `PGTilingHelpers.so`**; only the attribute *names* appear as `__pyx_n_s_` strings.

| Attr name (Target) | Meaning | Value | Confidence |
|---|---|---|---|
| `num_partitions` / `statebuf_num_partitions` | SBUF/PE partition count; the in-tile P axis is capped here | 128 (ArchLevel 20: Trn1/Inf2/Sunda); 64 (ArchLevel 10) | CERTAIN (name); HIGH (value) |
| `psum_par_fp32_per_bank` | PSUM bank free-column width in fp32 lanes; bounds the matmul free tile | 512 | CERTAIN (name); HIGH (value) |
| `partition_multiple` | partition-dim alignment multiple the par tile must be a multiple of | per-arch | CERTAIN (name) |
| `max_prefetch_buffer_size_in_bytes` | SBUF prefetch (outer-loop) buffer byte cap | per-arch | CERTAIN (name) |
| `max_tripcount` | cap on emitted tile-loop trip count (estimator constraint) | per-arch | CERTAIN (name) |

> **NOTE — the 512 default and the PSUM-bank bound are the same number for a reason.** The generic non-MM `cut_size` default (512 elements) is sized to *one PSUM bank column* (`psum_par_fp32_per_bank` = 512 fp32). The same overflow check (*"exclude axes that cannot be tiled without overflowing PSum"*) reappears in the libwalrus C++ backend loop-tiler — the Python tiler applies it at *graph* level to pick tile shapes; the backend re-checks it at *loop* level. Same arithmetic, two layers.

## Boundary — Graph Tiler vs Backend Size-Fit

This transform is the **graph-level** decomposition. It is DISTINCT from three downstream "already-tiled-tensor" fitters:

| Stage | What it is | Distinction |
|---|---|---|
| **This PG tiler** | graph→tile decomposition; emits `TiledDAG` + PG relation graph | the first stage that turns whole-tensor ops into the partition×free loop nest |
| **libwalrus `SBSizeLegalization`** (C++ backend) | a pre-allocator pass that *shrinks/splits* tensors already laid out, to fit the 128-partition SBUF byte budget | operates on storage shapes; does NOT decide the loop nest — that was done here |
| **walrus loop-tiling numerics** (C++) | re-applies the `psum_par_fp32_per_bank` test at loop level; emits final unrolled trip counts | refines/legalises the loops; same PSUM-bank arithmetic, lower layer |
| **NKI allocator tiling** | tiles *inside* hand-written NKI kernels (`pmax=128`, `gemm_moving_fmax=512`) | separate entry path; NKI kernels are inlined via `InlineNKIKernels` and NOT re-tiled by PG |
| **MLIR `CanonicalizeForTensorizer` / `TensorizerLegalizationPass`** | run in `hlo2penguin` *before* the Penguin dialect exists; canonicalize MHLO | do NOT tile — the name collision is purely lexical |

```text
end-to-end pipeline position:

  MHLO --[hlo2penguin: CanonicalizeForTensorizer, TensorizerLegalizationPass]--> Penguin IR
    --[PGLayoutTilingPipeline: layout analysis, PAG layout,
         *** PGTiling = PComputeCutting + BFComputeCutting + LoopSplitting + MacroGeneration ***]
                                                                              --> tiled Penguin (TiledDAGs)
    --[lowering to BIR / walrus backend: loop-tiling numerics, SBSizeLegalization, allocation]
                                                                              --> scheduled, allocated kernel
```

> **NOTE — `TileCCOps` is a *separate* collective-comm tiler, not this path.** `TileCCOps` (methods `transformAllGatherOp` / `transformAllReduceOp` / `transformReduceScatterOp`) tiles collective-communication ops across the LNC/replica dimension only (string *"weights are in FSDP setting, which will not be tiled"*). It is sometimes mistaken for "the tensorizer"; the general whole-tensor compute tiler is the `DAGTiler`/PG path on this page.

## Evidence summary

What this page rests on, claim by claim:

- **Pipeline composition.** `PGTiling.__init__` is at `LayoutTilingPipeline` @ `0x109b0` (via `function_addresses.json`), and both cut passes are real modules with their own `transformStmts` entries — `PComputeCutting.transformStmts` @ `0x277d0`, `BFComputeCutting.transformStmts` @ `0x155f0`. The *order* AGOrdering → PComputeCutting → BFComputeCutting → LoopSplitting → MacroGeneration is the source-order of pass references in the decompiled `__init__`.
- **`computeCut` behaviour.** The function is @ `0xba670` and its six verbatim docstring examples pin the output for every input case, including the empty shape and the exactly-fits case.
- **The 128 / 512 budgets.** These are Target attributes, not constants: only the attr *names* `num_partitions` / `statebuf_num_partitions` / `psum_par_fp32_per_bank` appear as `__pyx_n_s_` strings, and no literal 128 or 512 lives in `PGTilingHelpers.so`. The numeric values come from the per-arch geometry tables and `gemm_moving_fmax`.
- **The naming split.** A directory scan of `penguin/targets/transforms/` yields the five tiling modules named above and no `Tensorizer*` module; the `Tensorizer` name resolves to `hlo2penguin` MLIR passes in a different binary, operating on MHLO rather than Penguin IR.
- **Inter-tile dependency edges.** `dag_to_producer_edges` / `dag_to_consumer_edges` are `__pyx_n_s_` constants in `PGAnalysisForTiling`; `buildPGRelationGraph` and `PartitionGroup.enumerate_loadstore_edges` are recovered methods, and `finalize` sits @ `0x23870` (PartitionGroup) / `0x22e40` (PGAnalysisForTiling).

## Limits of this reading

The recovered surface is docstrings, `__pyx` qualnames, `__pyx_n_s_` attribute-name constants, and decompiled `__init__` pass-ordering. Four things are compiled-opaque and are reconstructed rather than read:

- the exact body arithmetic of `computeCut`'s budget scan (the docstring examples fully determine its outputs, but the instruction sequence is not traced);
- the precise `_do_estimate` cost formula;
- the exact `should_swap_tile` decision;
- the per-op dispatch from a Penguin op to its `DAGTiler` subclass.

One open question remains: the edge structures are read directly, but their consumption by the backend scheduler is inferred from the edge-graph's purpose and from `finalize` emitting them — no scheduler-side reader was traced.

## Cross-References

- [Penguin Layout / PAG Selection](layout-middle-end.md) — 5.11, the layout analysis that decides *which* axis becomes the partition axis before this tiler cuts
- [Penguin Loop Transforms](pass-roster-pipeline.md) — 5.15, `LoopSplitting` / `MacroGeneration` consume the `TiledDAG` and emit the loop nest
- [Partition Geometry](../arch/sbuf-psum-geometry.md) — 1.05, the 128-partition SBUF / PSUM-bank model the tiler fits to
- [Matmul Tiling](../hlo-opt/op-fusion-dot-elementwise.md) — 0.7, the PSUM-bank-derived free sizing for tensor-contract DAGs
- [CanonicalizeForTensorizer](../hlo-opt/canonicalize-for-tensorizer.md) — the MLIR stage that owns the "Tensorizer" name (and does NOT tile)
- [TensorizerLegalizationPass](../hlo-opt/tensorizer-legalization.md) — the second MLIR "Tensorizer" stage, pre-Penguin
