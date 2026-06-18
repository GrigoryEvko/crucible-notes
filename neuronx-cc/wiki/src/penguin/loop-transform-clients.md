# Penguin Loop-Transform Clients

> *All symbols, docstrings, and qualnames on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310 wheel, GNU C17 11.5.0, `-O3 -fwrapv -fPIC`). Every loop transform ships as a Cython-compiled `.so` whose `__pyx_k_` identifier pool, docstrings, and Python qualnames survive in `.rodata`; the IDA decompiles for these modules are address stubs only, so the recon below is anchored to the surviving string/symbol pool, not to per-instruction decompilation.*

## Abstract

The Penguin middle-end restructures loop nests with a family of eight passes — `NeuronLoopInterchange`, `LoopFusion`, `NeuronLoopFusion` (TongaLoopFusion), `PartialLoopFusion`, `LoopSplitting`, `FlattenLoop`, `PerfectLoopNest`, and the shared `LoopTransformUtils` toolbox. Each is a `DotTransform` tree-rewriter over the Penguin loop-nest IR (`Stmt` / `Axis` / `Operator` / `AffineLoad` / `AffineStore`), run inside the Tonga and Sunda `CodeGenFlow` before lowering to BIR. They are the *decision* layer that decides which loops to reorder, merge, split, or coalesce.

The reflex assumption — that a loop transform asks an Integer-Set-Library (ISL) dependence engine "is this reorder legal?" the way Pluto or the LLVM polyhedral path would — is **wrong for seven of the eight passes here, and only partly right for the eighth.** An exhaustive symbol sweep shows the ISL validator entry points (`check_valid_schedule`, `DependenceViolation`, `get_dependency_map`) live in exactly two modules — `TongaIslDependenceAnalysis` (the definition) and `SoftwarePipelineCodeGen` (the sole caller) — and in *none* of the loop transforms catalogued here. Of the eight, **`PartialLoopFusion` is the only one that imports the ISL layer at all** (`IntegerSetAnalysis`, the Penguin wrapper around stock `islpy~=2023.1`); the other seven carry zero ISL tokens. Legality for the rest is a **dataflow / access-pattern test on the Penguin DAG**: producer-store address ≡ consumer-load address (`LoopFusion`), PSUM-tile + accumulation-axis compatibility (`NeuronLoopFusion`), affine index-representability (`FlattenLoop`), or a generic dependency-function-parameterized order check (`LoopTransformUtils.check_schedule`). ISL's actual role in this compiler is narrow: it validates the *later, instruction-level* software-pipelining reorder, not the loop-restructuring decisions documented here.

This page enumerates each transform, its method roster, the algorithm it runs, and — the central deliverable — the exact legality test it applies, every claim anchored to a surviving docstring, rejection string, assert message, or `__pyx_k_` qualname.

For reimplementation, the contract is:

- **Where legality lives.** The ISL engine is *not* on the loop-restructuring path. Each transform either applies a known-safe canonical reorder, an access-pattern match, an index-representability check, or `check_schedule` with a caller-supplied dependence predicate. Reproducing these passes against an ISL May-dependence relation would be over-engineering and would not match the binary.
- **The per-transform algorithm and its gate.** For each pass: what it rewrites, the candidate it enumerates, and the precise predicate that accepts or rejects the rewrite — reproduced as annotated pseudocode naming the real symbol.
- **The shared toolbox.** `LoopTransformUtils` (`interchange`, `licm`, the `split*` family, `check_schedule`) is the low-level mutator every client drives; its primitives do the IR surgery and the clients supply the policy.

| | |
|---|---|
| **Pass base class** | `neuronxcc.starfish.penguin.DotTransform` (statement/axis visitor) |
| **IR level** | Penguin loop-nest tree — `Stmt` / `Axis` / `Operator` / `AffineLoad` / `AffineStore`, pre-BIR |
| **ISL validator symbols** | `check_valid_schedule`, `DependenceViolation` — present ONLY in `TongaIslDependenceAnalysis` + `SoftwarePipelineCodeGen` |
| **Only ISL-importing loop transform** | `PartialLoopFusion` — imports `penguin.IntegerSetAnalysis` (2 string hits); other 7 = 0 hits |
| **Generic legality gate** | `LoopTransformUtils.check_schedule(schedule_fn, dep_check_fn, break_dataflow_fn, exclude_dependency, earliest_schedule, latest_schedule)` |
| **Pipeline host** | Tonga / Sunda `CodeGenFlow`; `LoopSplitting` runs in `LayoutTilingPipeline` |

---

## Where Legality Lives — the ISL-Absence Result

### Purpose

Before the per-pass detail, fix the architectural fact that governs all of it: the loop transforms do not call the ISL dependence validator. This is the single result that reorients a reimplementer away from a polyhedral design.

### The symbol sweep

An exhaustive search of the surviving string pools across the loop-transform `.so` set returns the ISL validator entry points only in the ISL subsystem itself and in the software pipeliner:

```text
check_valid_schedule   →  TongaIslDependenceAnalysis (defines) + SoftwarePipelineCodeGen (sole caller)
DependenceViolation    →  same two modules ONLY
get_dependency_map     →  IntegerSetAnalysis / TongaIsl family (returns Tuple[isl.UnionMap, isl.UnionMap])
```

None of `{TongaLoopInterchange, TongaLoopFusion, LoopFusion, PartialLoopFusion, LoopSplitting, FlattenLoop, PerfectLoopNest, LoopTransformUtils×2}` reference `check_valid_schedule` or `DependenceViolation`. The ISL engine is a separate subsystem wired only into `SoftwarePipelineCodeGen` for instruction-level reordering.

> **QUIRK —** `IntegerSetAnalysis` is the Penguin module that wraps stock `islpy~=2023.1` (it translates each statement's iteration domain into `isl.Set` and its access map into `isl.UnionMap`). Of the eight loop transforms, the import of `IntegerSetAnalysis` appears in **`PartialLoopFusion` and nowhere else** — measured directly: `PartialLoopFusion` strings = 2 hits on `IntegerSetAnalysis`, all seven others = 0. So "PartialLoopFusion is the only ISL-touching loop transform" is not a guess from naming — it is a binary count. Even there, PLF imports the *wrapper* (`IntegerSetAnalysis`), not `islpy` directly, and its primary legality gate is its own `checkLoopCarriedDep` (see §PartialLoopFusion).

### The two real legality mechanisms

```text
(a) LoopTransformUtils.check_schedule  — generic, dependency-FUNCTION-parameterized DAG order gate.
        Its scope struct (__pyx_scope_struct_14_check_schedule) carries:
          schedule_fn, dep_check_fn, break_dataflow_fn, exclude_dependency,
          earliest_schedule, latest_schedule, _always_true (default dep_check_fn).
        Walks the DAG, applies a caller-supplied dep predicate; imports no isl.

(b) PartialLoopFusion.checkLoopCarriedDep(s) — PLF's OWN loop-carried-dependence test
        (loop_carried_deps, dep_check_scope, lex_schedule, static_lex_order).
        Validates the fused order against the static lexicographic schedule, not isl.lex_lt.
```

Penguin loop restructuring is validated against the Penguin **dataflow / DAG dependence graph** (the load/store access patterns), not the ISL May-dependence relation. ISL is the later, instruction-level scheduler's validator.

---

## NeuronLoopInterchange (TongaLoopInterchange)

### Purpose

Reorder the axes of a single operator's loop nest into a hardware-locality-optimal order — canonically, push the reduction/PSUM-accumulation loop innermost and lift the shard/thread axis outermost. It is an **op-aware canonicalizer**, not a dependence-vector-driven interchange optimizer.

### Class docstring (CONFIRMED, verbatim)

```text
TongaAffineLoopXform - Loop interchange and skewing.
Affine loop transformation with tonga cost model, e.g. loop interchange to put
the reduction loop on psum into innermost loop to improve psum locality.
Disabled because scheduler actually failed in some cases with this
```

The "+ skewing / tonga cost model" affine-search path — the part that *would* need a real legality test — is the disabled path. The live behavior is the per-op canonical reorder.

### Algorithm

```c
// class NeuronLoopInterchange(DotTransform), targets/transforms/TongaLoopInterchange
function transformAxis(stmt):                       // per-axis visitor driver
    nest = find_perfect_nested_loops(stmt)          // LoopTransformUtils — maximal perfect sub-nest
    op   = operator_of(nest)
    switch op.kind:
        case MatMul:        order = transformMatMulOp(nest)        // -> matmul_loopnest_order
        case Transpose:     order = transformTransposeOp(nest)     // -> transpose_loopnest_order
        case TiledSoftmax*: order = transformTiledSoftmax*(nest)
        case TiledRmsNorm:  order = transformTiledRmsNormOp(nest)
        case ReduceMacro:   order = transformNeuronReduceMacro(nest)
        default:            return                                  // unknown op: leave as-is
    // shard/thread axes handled across all ops:
    order = interchange_shard_thread_axis(order)    // shard axis on top, thread/cc axis on top
    morph_nest_to(nest, order):
        for (outer, inner) in adjacent_swaps(current_order -> order):
            LoopTransformUtils.interchange(outer, inner)           // pure mechanical swap
    run_licm(nest)                                  // re-hoist now-invariant insts

// transformMatMulOp: classify axes, build reduce-innermost order
function transformMatMulOp(nest):                   // -> matmul_loopnest_order
    classify nest axes into { lhs_free_axes, rhs_free_axes, contract_axes (= reduce_axes) }
    if is_cross_nc_reduce(reduce_axes): keep reduce OUT of innermost  // crosses NeuronCore boundary
    return [ ...free axes... , contract_axes ]      // contraction (PSUM accumulation) INNERMOST
                                                    // => consecutive tiles accumulate into same PSUM bank
```

### The per-op order builders

The MatMul handler's `__pyx_k_` vocabulary — `lhs_free_axes`, `rhs_free_axes`, `contract_axes`, `reduce_axes`, `is_cross_nc_reduce`, `matmul_loopnest_order` — confirms it classifies the nest's axes into LHS-free / RHS-free / contraction groups and constructs `matmul_loopnest_order` with the contraction (PSUM-accumulation) loop innermost, so consecutive matmul tiles accumulate into the same PSUM bank ("improve psum locality"). `is_cross_nc_reduce` flags a reduction crossing NeuronCore boundaries, which is kept out of the innermost-reduce form. The shard/thread handlers (`transformShardAxis`, `transformThreadAxis`, vocab `shard_axis`, `thread_axis`, `shard_axis_on_top`, `thread_or_cc`, `enable_shard_axis_verifier`) move the SPMD-partition (shard) axis and the collective-comm (thread/cc) axis to the top of the nest, so each core executes a contiguous outer slab.

> **GOTCHA —** the shard/thread hoist requires a perfect nest. When it cannot hoist because a sibling sub-loop sits between the axis and the body, the pass raises `The shard/thread axis is not in perfect loopnest!!` (CONFIRMED string). This is precisely why `PerfectLoopNest` and `find_perfect_nested_loops` must run first — the interchange cannot canonicalize an imperfect nest.

### Legality model

There is **no dependence-direction-vector test and no ISL call** in this module (0 ISL string hits). Correctness rests on three facts: (a) the per-op order is a known-safe canonical reorder — matmul reduce-innermost and shard/thread-outermost are always legal because the op is commutative in those axes; (b) `LoopTransformUtils.interchange` only swaps axes *within one operator's perfect nest*, so there is no cross-statement dependence to violate; (c) the affine-skew + cost-model search that would need a real legality check is the disabled path. This pass is a canonicalizer toward HW-locality-optimal loop order, not a legality-gated interchange optimizer. *(INFERRED legality reasoning; STRONG on the disabled-path and per-op-order facts via docstring + vocab.)*

---

## LoopFusion

### Purpose

Merge two loop nests that share their outer dimensions when a producer's **store** feeds a consumer's **load** — and, in the trivial case, eliminate a pure copy loop outright. Operates at the generic `AffineLoad` / `AffineStore` level (before HW-instruction lowering).

### Class docstrings (CONFIRMED, verbatim)

```text
LoopFusion - Merge loops (loopnests) that share the outer dimensions.

eliminate copy loops of the form:
    $1 = load A   /   B = store $1       (i.e. for(i){ B[i] = A[i] })
  where load and store have the exact same access pattern
```

The `_is_loop_too_large_for_fusion` docstring spells out the size gate: fusion is refused when the merged loop would carry too many instructions, "because then [scheduling] actually relevant for performance is no longer done … due to long def-use chains within a loop. The current number was chosen by some non-scientific experiments."

### Algorithm

```c
// class LoopFusion(LoopFusionBase), transforms/LoopFusion
function checkStmt(consumer_load):                   // find a fusable producer for this load
    if isGatherLoad(load) or loads_constant_or_input(load):  return REJECT
    store = findFusionCandidateForLoad(load)         // the single producing store
    if store == None:                                return REJECT  // "No candidate found"
    if not isLoadStoreAccessMatchForFusion(load, store):  return REJECT  // ": Accesses do not match"
    if _is_loop_too_large_for_fusion(merged):        return REJECT  // size gate
    return store

function fuse(load, store):
    if same_access_pattern(load, store):             // exact copy
        fuseTrivialCopy(load, store); propagateCopy()        // Stat: "Number of trivial copy eliminated"
    elif can_move_to_load(store):  fuseStoreToLoad(store, load)   // move producer down
    elif can_move_to_store(load):  fuseLoadToStore(load, store)   // move consumer up
    copyInstSchedule(...)                            // merged insts inherit recomputed per-inst schedule
```

### Fusion legality = access-pattern match

Legality is an **access-pattern compatibility test** on the producer-store address versus the consumer-load address — `isLoadStoreAccessMatchForFusion` compares the two access maps — not a dependence-relation test. The complete rejection-reason set (each a CONFIRMED verbatim `": …"` / `Skip:` log string that aborts a candidate):

| Rejection string | Meaning |
|---|---|
| `: Accesses do not match` / `: access not match` | producer-store and consumer-load addresses are incompatible |
| `: complex access pattern` / `: complex loop mapping` | access map too complex to align |
| `: Definition not a perfect nested loop` | producer must be a perfect nest |
| `: more than 1 stores` | tensor must be single-def |
| `: too many loads` | consumer side too fanned-out |
| `: broadcast load` / `: has broadcast on load` / `: broadcast becomes free dim in matmul` | broadcast access can't fuse |
| `: bad producer consumer addr mapping` | the store→load index remap is invalid |
| `: overlapping with` | overlapping live ranges |
| `: padded access` | padded access map |
| `: single_def_single_use` | gate: only single-def-single-use tensors (`fuse_single_def_single_use_only`) |
| `: loading constant or input (or undef)` | can't fuse a load of a module input/constant |
| `: Cannot fuse with pseudo_elementwise_op` | pseudo-elementwise op blocks fusion |
| `Skip: loading non local tensor` / `Skip: loading output` / `Skip: Cannot find tensor definition of` | producer not a local, fusable tensor |

Special pattern paths fuse a producer MatMul with a consumer Softmax / RmsNorm (`check_matmult_softmax_pattern`, `check_matmult_rmsnorm_pattern`) and batchnorm-grad reduce chains (`check_bn_grad`, `check_reduces`). Statistics counters confirmed: `Number of loops fused`, `Number of trivial copy eliminated`, `Number of rematted instructions we skipped`.

> **NOTE —** the module also carries the string `On the complexity of loop fusion` — a nod to the well-known result that optimal loop fusion is NP-hard. The pass does not solve it optimally; the size gate and the single-def-single-use restriction are the pragmatic cutoffs that keep it tractable.

### Schedule preservation

After fusion the merged insts inherit a recomputed per-inst **schedule** (the Penguin execution-order annotation), copied and remapped from the originals via `copySchedule` / `copyInstSchedule` / `cached_schedule_tuple`. This is the same per-inst integer order tuple validated by `check_schedule` — not an ISL schedule tree. `fuseOnBarrier` fuses across an `OptBarrier` (the alias-opt barrier inst); `transformOptBarrier` rewrites the barrier afterward (`Invalid OptBarrier - must have 1 input and 1 output`).

---

## NeuronLoopFusion (TongaLoopFusion)

### Purpose

The post-lowering analogue of `LoopFusion`. It runs after lowering to `TongaISAInst`, so it reasons about PSUM banks and matmul accumulation rather than abstract `AffineLoad` / `AffineStore`. Its purpose: fuse the loops of **matmuls that accumulate into the same PSUM tile** so they form one `AccumulationGroup`.

### Class docstring (CONFIRMED, verbatim)

```text
NeuronLoopFusion - Loop fusion at TongaISAInst level, has extra heuristics/cost
model like fuse loops such that the loops matmul writting the same PSUM results are
fused together.
```

Statistics: `Number of matmul fused for psum locality`.

### Algorithm

```c
// class NeuronLoopFusion(DotTransform), targets/transforms/TongaLoopFusion
function afterStmtTransform(stmt):
    for cand in enumerateFusionCandidate(stmt):      // (producer_loop, consumer_loop, tensor)
        if not canFuse(cand):              continue  // perfect-nest / size / access gate
        if cand.is_psum_accumulation:
            if not canFusePSum(cand):      continue  // PSUM-tile + acc-axis compatibility
        if check_alias_opt_barrier_violation_with_dag(cand): continue  // alias safety on DAG
        fuse(cand)                                    // -> one AccumulationGroup

function canFusePSum(cand):                           // the PSUM-fusion gate
    if not access_compatible_psum_tile(cand):  return false   // must write compatible PSUM tiles
    if not match_accumulation_axes(cand):      return false   // contraction (acc) axes must line up
    if not has_compatible_free_ap(cand):       return false   // free access-patterns compatible
    if is_cross_nc_reduce(cand):               return false   // reduce crossing NeuronCores can't PSUM-fuse
    return true
```

### PSUM-fusion legality

`canFusePSum` gates on `access_compatible_psum_tile` / `has_compatible_free_ap` / `match_accumulation_axes` (vocab `acc_axes`, `contract_axes`, `is_full_tile_ap`, `fuse_psum_before`): two matmul loops may fuse iff they write compatible PSUM tiles with matching accumulation (contraction) axes and compatible free access-patterns. The CONFIRMED verbatim rejection strings:

```text
: Can only fuse with matmul if it is the last inst   ← matmul must terminate the loop body
: already in the same loop
: complex loop mapping  /  : complex loop mapping in thread/cc axis
: incompatible axis type                              ← is_thread_or_cc_axis / acc_axes mismatch
: Definition not a perfect nested loop
: too many instructions in loop                       ← size gate (cf. LoopFusion)
: Accesses do not match / Cannot find tensor definition of / No candidate found
: loading constant or input (or undef)
```

The alias-opt-barrier checks (`check_alias_opt_barrier_violation_with_dag`, `check_load_store_alias_opt_barrier`, `buildAliasTensorMap`) ensure that fusing across a barrier does not reorder two aliasing tensors — the Tonga-level analogue of a dependence check, done on the DAG + alias map, **not via ISL**. The module imports `TongaLoopInterchange` (`interchange_reduce_axes` is referenced), so it may re-interchange reduce axes to expose a fusable PSUM-accumulation order before fusing.

---

## PartialLoopFusion — the heavy engine, and the only ISL importer

### Purpose

Fuse only a **tile** of a producer loop into a consumer, rather than two whole nests. The fusion is controlled by a fusion factor (tripcount) and an extra tiling factor that keeps the fused inner region inside one `AccumulationGroup` / StateBuffer budget. Crucially, PLF can fuse even across a **loop-carried dependence** by affine-shifting the iteration space so the dependence becomes forward-only.

### Class + helper docstrings (CONFIRMED, verbatim)

```text
PartialLoopFusion - Partially fuse two loopnests together.
   Prerequisites:
     1. all loads of the to-be-fused tensor are in the same load_loop to be fused
     2. all stores of the to-be-fused tensor are in the same store_loop to be fused

calculateExtraTilingFactor:
   tripcount: the loop fusion factor … load_loop: the load_loop to be fused …
   fusing the entire load_loop might not be optimal, b/c it might cause AccumulationGroup
   to be too small, which is inefficient. We want to calculate an extra tile factor for
   load_loop, to keep within AccumulationGroup and not fuse [the whole thing].
```

### Class structure

`PartialLoopFusion` (the pass) drives `PartialLoopFusionCtx` (per-function state: `build_barrier_map`, `enumerate_tensor` ranked by `fusion_priority`, `is_barrier`, `should_skip_tensor`). A `FusionCandidate` is one `(store_loop, load_loop, tensor)` triple carrying `how_good`, `calculateMotionDirection`, `calculateExtraTilingFactor`, `checkLoopCarriedDep(s)`, `fuseStoreToLoad`, `tileLoops`, `enumerate_remat_loads`. The `FusionCandidateGenerator` (and its `GreedyFusionCandidateGenerator` / `LimitedFusionCandidateGenerator` subclasses) enumerates and selects candidates. `LoopXFormBuilder` builds the affine iteration-space alignment; `TransformState` does the tile/pad mechanics.

### Algorithm

```c
// class PartialLoopFusion(DotTransform), targets/transforms/PartialLoopFusion
function transformStmts(fn):
    ctx = PartialLoopFusionCtx(fn); ctx.build_barrier_map()
    gen = (Greedy|Limited)FusionCandidateGenerator(ctx)
    gen.iterative_fuse_any():                        // greedily apply until none remain
        for tensor in ctx.enumerate_tensor():        // ordered by fusion_priority
            cand = choose_fusion_candidate(tensor):
                rank candidates by max_coef_min_depth // maximize affine coef, minimize loop depth
            if cand == None: continue
            if should_split_non_perfect_load/store_loopnest(cand):    // enforce prereqs 1 & 2
                splitLoopnestForInst(...)            // isolate the fused accesses (loop fission)
                if not can_split_without_remat(cand):
                    rematerialize(enumerate_remat_loads(cand))        // Stat: "Number of instruction rematerialized"
            xf = LoopXFormBuilder(cand).build()       // affine remap of producer indices into consumer ivs
            if not checkLoopCarriedDep(cand, xf):     // loop-carried-dep gate on the DAG
                if not xf.makes_dependence_forward_only(): 
                    REJECT  // "cannot fuse loop!"
            cand.fuseStoreToLoad(extra_tile = calculateExtraTilingFactor(cand))
```

### LoopXFormBuilder — the affine loop-alignment

To fuse two loops whose iteration spaces are not identically indexed, PLF builds an affine transform re-expressing the producer-store indices in the consumer-load's iteration variables. The per-axis relation it solves is a linear remap:

```text
store_index = coef * load_index * scale + shift + offset
```

`match_scale` aligns the per-axis stride scale; `shift` aligns the constant offset; `calculate_offset` computes the base offset (vocab `coef`, `coef_scale`, `axis_coef`, `load_coef`, `store_coef`, `load_shift`, `load_xforms`, `scaled_max_delta`, `split_offset`). The affine fit is guarded by CONFIRMED asserts: `Bad coef value!`, `scale mismatch!`, `Unexpected coef!`. This is the loop-skewing/alignment that the interchange pass's title advertised but had disabled — executed here, for fusion, instead of for interchange.

### Legality = checkLoopCarriedDep on the DAG

`FusionCandidate.checkLoopCarriedDep` / `checkLoopCarriedDeps` test, over `dep_check_scope`, whether fusing introduces an illegal loop-carried dependence (`loop_carried_deps`). Unlike the other fusion passes, PLF does *not* outright reject a carried dependence — it can still fuse if the `LoopXFormBuilder` shift makes the dependence forward-only, with `lex_schedule` + `static_lex_order` verifying the fused insts keep a valid lexicographic order. Rejection only fires (`cannot fuse loop!`) when no legal alignment/order exists. Statistics: `Number of loops fused`, `Number of loops fused with loop-carried dependencies`, `Number of instruction rematerialized`.

> **CORRECTION (Y07-PLF) —** the backing recon framed PLF's legality as ISL-free ("its own checkLoopCarriedDep, no isl symbols"). A direct string count refines this: PLF's strings reference `IntegerSetAnalysis` (the `islpy~=2023.1` wrapper) — 2 hits, and it is the *only* one of the eight loop transforms to do so (all others = 0). The refined claim is therefore stronger and more precise than "no ISL anywhere": **PLF is the single loop transform that imports the ISL layer**, used in support of its integer-set / iteration-space reasoning, while its primary loop-carried-dependence gate (`checkLoopCarriedDep`) and its order check (`lex_schedule` / `static_lex_order`) are its own static-schedule machinery, not `check_valid_schedule`. PLF imports the `IntegerSetAnalysis` wrapper, not `islpy` directly.

> **QUIRK —** `lex_schedule` and `static_lex_order` are PLF's own lexicographic-order legality check on the static schedule tuple — the Penguin-DAG analogue of ISL's `lex_lt` test, but implemented over the integer schedule order, not over an `isl.UnionMap`.

### Nest prep + rematerialization

Prerequisites #1/#2 (all loads in one `load_loop`, all stores in one `store_loop`) are enforced by `should_split_non_perfect_{load,store}_loopnest` + `check_load_store_loop`: a non-perfect nest is split (`LoopTransformUtils.splitLoopnestForInst`) so the fused accesses are isolated. `can_split_without_remat` decides whether splitting needs rematerialization; if so, `enumerate_remat_loads` / `find_remat_axes` / `rematerialize` / `rematerializeDstOnAxes` duplicate the producing loads into the consumer loop. `check_acc_grp_splitting` keeps fused matmuls within a valid `AccumulationGroup`; the DMA-coalesce checks (`calculate_dma_coalesce_factor`, `calculate_dma_stride_in_bytes`, `check_dma_coalesce_factor`) keep the fused DMA coalesced. Block-accounting asserts: `there should be less blocks after fusing`, `there should be more blocks after split`. Imports: `TongaISAInst`, `TongaLICM`, `LoopTransformUtils`, `IntegerSetAnalysis`, `SCEV`, `DataflowUtils`.

---

## Nest-Restructuring Transforms — Split / Flatten / Perfect

### LoopSplitting

### Purpose

Split one large reduction over a too-big reduce axis into a **cascade** of smaller reductions, with a partition-free (PF) transpose between stages — because Neuron HW reduces along the partition dimension and a 12800-long reduce axis does not fit one tile. It is *not* a generic strip-mining pass; it materializes a tree reduction as separate tiled DAGs.

### Class docstring (CONFIRMED, verbatim, abbreviated)

```text
Split cascaded reduction dags. Example: transform a dag with a reduce max op of
[56, 12800 (reduce axis)]. The layout algorithm should pick [12800 P, 56 F], and
after cutting we should see [12 B, 128 P, 100 F, 5 F]. The goal is to split this dag
into 4 dags:
  1. [12 B, 128 P, 5 F, 100 F] => reduce max => [12 B, 128 P, 5 F, 1]
  2. [12 B, 128 P, 5 F]        => PF transpose => [12 B, 5 P, 128 F]
  3. [12 B, 5 P, 128 F]        => reduce max => [12 B, 5 P, 1 F]
  4. [12 B, 5 P]               => PF transpose => [12 B, 5 F]
```

### Algorithm

```c
// class LoopSplitting(DotTransform), targets/transforms/LoopSplitting
function transformStmts(stmt):
    if is_partition_reduce(stmt) and reduce_axis_too_big:
        first, second = _split_dags(stmt)            // split_reduce_loop primitive
        _process_first_reduction_dag(first)          // reduce over inner tile
        par_axes = _get_second_reduce_par_axes(first, second)
        _process_second_reduction_dag(second, par_axes)
        if not is_reduce_add(stmt):                  // reduce_add is commutative across partitions
            _insert_transposes(...)                  // insertPFTransposeLoop between stages
        _generate_tiled_dags(...)                    // TiledDAG per new dag
    elif is_batchnorm(stmt):
        _handle_batchnorm_mean_var(stmt) | _handle_batchnorm_gradient(stmt)
```

### Legality

There is **no dependence legality test** — a tree reduction is value-equivalent to the flat reduction, so the only decision is partition-axis selection for the second DAG. `_get_second_reduce_par_axes` picks the P dims under CONFIRMED constraints: "For reduce add, we can use the partition reduce macro, so the second dag par axes should be the same as the first reduce dag … For other reduce ops … 4. Do not pick any free axes that have been reduced away in the first reduce dag  5. Do not pick any block axes that were loop reduce axes in the first reduce dag." `reduce_add` skips the PF transpose entirely. Asserts: `Could not find first/second reduce load`, `Could not find second reduce store`. `LoopSplitting` is wired into `LayoutTilingPipeline`, not the main codegen loop-opt block.

---

### FlattenLoop — newest (2025)

### Purpose

Coalesce adjacent perfectly-nested outer loop axes into a single flattened axis (ND nest → fewer-D nest) when every instruction's access expression can be rewritten over the flattened index. Shorter loop nests mean fewer instructions and better DMA shapes.

### Algorithm

```c
// class FlattenLoop(DotTransform), transforms/FlattenLoop   (Copyright 2025)
function flattenOuterLoops(stmt):
    for group in enumerateOuterLoopCandidateGroups(stmt):   // adjacent axis groups
        dims = checkFlattenDims(group)                       // candidate axes to coalesce
        if tryFlattenAxesImpl(stmt, dims):                   // succeeds iff legal (below)
            flattenAxes(dims)                                // Stat: "Number of axes coalesced"
            reshapeInstTensor(...)                           // Stat: "Number of tensors reshaped"

function tryFlattenAxesImpl(stmt, dims):
    if not canUpdateAllExprsAfterFlatten(stmt, dims):  return false
    return true

function canUpdateAllExprsAfterFlatten(stmt, dims):
    for inst in stmt.insts_touching(dims):
        if not (canUpdateExprAfterFlatten(inst, dims)          // rewrite AffineExpr over coalesced index
                or canUpdateExprApproximate(inst, dims)):      // predicated / approximate case
            return false
    return true
```

### Legality = index representability

This is purely an **index-representability** test (affine / SCEV), not a dependence test. `tryFlattenAxesImpl` flattens a candidate group iff `canUpdateAllExprsAfterFlatten` — i.e. for every instruction touching those axes, `canUpdateExprAfterFlatten` (or `canUpdateExprApproximate` for predicated/approximate cases) can rewrite its `AffineExpr` index over the single coalesced index without loss. A non-contiguous stride or an unmergeable predicate rejects the group. `flattenPredicatedAxes` / `flattenPredicatedAxesInInsts` handle the predicated-axis case by merging the predicate too. Imports `ir.AffineExpr`, `SCEV`, `DataflowUtil`, `Statistics`, `DotTransform`, `TongaMacro`.

---

### PerfectLoopNest

### Purpose

The nest-perfecting precondition pass. A loop with multiple child sub-loops (an imperfect nest) is rewritten so each loop level has ≤1 child, by distributing the loop — each child subtree gets its own clone of the enclosing loop (loop distribution / fission).

### Class docstring (CONFIRMED, verbatim)

```text
PerfectLoopNest - Split loop tree into perfect nested loop, i.e. there are at most 1
child for [each] loop in the loopnest.
```

### Algorithm

```c
// class PerfectLoopNest(DotTransform), transforms/PerfectLoopNest
function transformAxis(loop):
    children = enumerate_substmts(loop)
    if len(children) <= 1: return                    // already perfect
    if not allow_mixed_children_types and mixed_types(children): return
    for child in children:
        new_parent = IRCloner.clone(loop)            // fresh copy of the enclosing loop header
        beforeMoveToNewParent(child, new_parent)
        moveToNewParent(child, new_parent)           // distribute: each subtree under its own clone
        child.removeFromParent()
```

### Legality

Distribution is always legal here: each child subtree is moved whole into a fresh clone of its parent loop, no statements are reordered within a subtree, and the subtrees were already siblings under the same loop bounds. The only check is `allow_mixed_children_types` — whether two children of differing axis/statement type may share a distributed parent. **No dependence test, no ISL.** This is the precondition that Interchange (`The shard/thread axis is not in perfect loopnest!!`), NeuronLoopFusion (`: Definition not a perfect nested loop`), and PartialLoopFusion (prereqs #1/#2) all require.

---

## LoopTransformUtils — the shared toolbox

### Purpose

The low-level loop-IR mutators every client above drives. Two variants ship: `transforms/LoopTransformUtils` (the full toolbox) and `targets/transforms/LoopTransformUtils` (a Tonga-target add-on for perfect-nest queries and rematerialization-candidate finders). Module docstring: *"LoopTransformUtils contains utility functions to manipulate the loop transformations. Current utility functions includes: licm -- Loop Invariant Code Motion …"*

### Primitive catalog

| Primitive | Role | Confidence |
|---|---|---|
| `interchange(outer_axis, inner_axis [, new_inner_axis])` | Swap two adjacent loop levels in a perfect nest (`outer` is `inner`'s parent). Asserts `Incorrect outer axis!`. Pure mechanical IR swap; tests no legality. | CONFIRMED |
| `licm` / `run_licm` / `licm_children` / `hoistNullary` | Loop-invariant code motion. `calculate_licm_parent` finds the most-recent common loop of an op's operands; `hoistNullary` lifts zero-operand ops (constants) to the top. | CONFIRMED |
| `getHoistOrSinkLoop` / `hoistOrSinkInst` / `sinkStore` | Hoist/sink an inst across loop levels. "Currently only ElementwiseOp can be hoisted or sink." | CONFIRMED |
| `can_move_to_{earliest,latest,load,store}` | Bound the legal motion range of an inst from its def/use on the DAG (`earliest_schedule` / `latest_schedule`). | CONFIRMED |
| `splitAxisAt` / `splitAxisAtWithMapping` | Strip-mine one axis into (outer, inner), optionally returning the old→new axis map. | CONFIRMED |
| `splitLoopnestForInst` | "Split insts into a new loopnest … inserted after `inner_axis`." Loop fission for a chosen inst set. | CONFIRMED |
| `splitDAGLoopnest` | Split a whole DAG loopnest; assert `Two splited DAGs should have no overlaps!`. | CONFIRMED |
| `split_reduce_loop` | "Split one reduce op into 2 consecutive reduce ops, and split the second into a separate loopnest." The primitive `LoopSplitting` and PLF use (FIXME NCC-5367 noted). | CONFIRMED |
| `cloneLoopNest` / `find_perfect_nested_loops` | Deep-clone a loop subtree (IRCloner); return the maximal perfect sub-nest. | CONFIRMED |
| `check_schedule` | The generic legality gate (below). | CONFIRMED |

### check_schedule — the generic legality gate

```c
// LoopTransformUtils.check_schedule  (scope: __pyx_scope_struct_14_check_schedule)
function check_schedule(insts,                       // candidate post-transform ordering
                        schedule_fn,                 // inst -> its static schedule position
                        dep_check_fn = _always_true, // caller predicate: is there a dep A->B?
                        break_dataflow_fn,           // may this edge be broken / ignored?
                        exclude_dependency,          // set of dep edges to skip
                        earliest_schedule,           // legal position window (low)
                        latest_schedule):            // legal position window (high)
    for (a, b) in pairs(insts):
        if (a, b) in exclude_dependency:  continue
        if break_dataflow_fn(a, b):        continue
        if dep_check_fn(a, b):                        // a dependence A -> B exists
            if schedule_fn(a) >= schedule_fn(b):      // producer must precede consumer
                return ILLEGAL
    return LEGAL
```

This is the DAG-level, dependency-function-parameterized analogue of the ISL subsystem's `check_valid_schedule` — the same job (reject an order that violates a dependence) operating on the Penguin dataflow graph with a *caller-supplied* dependence predicate and the per-inst integer schedule tuple, **with no `islpy`**. When the caller wants no real dependence check, `dep_check_fn` defaults to `_always_true` and the gate degenerates to a pure position-window check. This is the bridge the loop transforms validate through — not the ISL engine.

The `targets/` variant adds `is_perfect_nested` / `lowest_perfect_nested` (perfect-nest queries over `TongaISAInst` / `SundaISAInst` nests, used by NeuronLoopFusion and PartialLoopFusion to locate the fusable region), and the rematerialization-candidate finders `collect_rematerialization_candidates` / `is_rematerialization_candidate` / `nontrivial_remat` that feed PLF's remat path.

---

## Legality-Mechanism Table — the central deliverable

| Transform | How a candidate transform is validated (NONE invoke the ISL `check_valid_schedule`) | ISL import |
|---|---|---|
| `NeuronLoopInterchange` | No legality test — canonical per-op order is known-safe; swaps stay within one op's perfect nest. (Skew + cost path disabled.) | none |
| `LoopFusion` | Access-pattern match: producer-store addr ≡ consumer-load addr (`isLoadStoreAccessMatchForFusion`) + perfect-nest / single-def / size / broadcast / locality rejections. | none |
| `NeuronLoopFusion` | PSUM-tile + accumulation-axis compatibility (`access_compatible_psum_tile`, `match_accumulation_axes`) + alias-opt-barrier DAG check; matmul-last gate. | none |
| `PartialLoopFusion` | `checkLoopCarriedDep(s)` on the DAG + `lex_schedule` / `static_lex_order`; `LoopXFormBuilder` shifts the iteration space to make a carried dep forward-only. **The only ISL-importing pass.** | `IntegerSetAnalysis` (2 hits) |
| `LoopSplitting` | No legality test — tree reduction is value-equivalent; only par-axis selection constraints. | none |
| `FlattenLoop` | Index-representability: `canUpdateAllExprsAfterFlatten` (every `AffineExpr` survives the axis coalesce). | none |
| `PerfectLoopNest` | No dependence test — loop distribution of whole sibling subtrees into cloned parents. | none |
| `LoopTransformUtils.check_schedule` | Generic DAG order test; dependence predicate supplied by the caller; per-inst integer schedule tuple. | none |

All legality here is dataflow / access-pattern based on the Penguin DAG. The "drive ISL to accept/reject a reorder" model applies to `SoftwarePipelineCodeGen`, a different, later, instruction-scheduling client — not to the loop-restructuring passes catalogued here.

---

## Pipeline Placement

Every pass is a `DotTransform` subclass (statement/axis visitor): the visitor walks the loop tree, the transform mutates it in place, and `DotTransform` handles fixpoint/iteration (`LoopFusion` additionally has its own `.iterate`). It is a tree rewriter over the Penguin loop-nest IR, not a worklist/PassManager over a CFG. The passes co-occur in the Tonga and Sunda `CodeGenFlow` string pools alongside `FlattenAxesForTiling`. `LoopSplitting` is wired into `LayoutTilingPipeline` (it runs during layout/tiling); `PartialLoopFusion` is also pulled by `FineGrainedCCOpFusion`, `VectorizeDMA`, and model-specific tuning; `FlattenLoop` is pulled by `FlattenAxesForTiling`, `DataLocalityOpt`, `SimplifySlice`, `InsertIOTransposes`, and `FlattenMacroLoop`.

```text
Typical loop-opt order (INFERRED from data-deps):
  PerfectLoopNest  ──► NeuronLoopInterchange  ──► { LoopFusion | NeuronLoopFusion | PartialLoopFusion }  ──► FlattenLoop
   (make perfect)       (canonical loop order)          (merge)                                            (coalesce)
  LoopSplitting runs separately, during LayoutTilingPipeline.
```

> **NOTE —** these Penguin (Python/Cython) loop restructurers are a *distinct layer* from the libwalrus C++ backend loop optimizer (`neuronxcc::backend::LoopOptimization`, with `check_loop_interchange(bir::InstLoop*, …)` and `checkLoopFusionGreedy(bir::BasicBlock&, int)`). The Walrus optimizer operates on the BIR CFG of basic blocks and is the *final* hardware-loop optimizer with its own profitability cost model; the Penguin passes here operate on the loop-nest tree *before* lowering to BIR. Same family of transforms (LICM, fusion, interchange), different IR, different language, different stage. They are two distinct loop-opt layers, not the same code recompiled. The Penguin-side LICM is `NeuronLICM` (in `TongaLICM`); `LoopTransformUtils.licm` is the primitive both `NeuronLICM` and the in-pass `run_licm` calls share.

---

## Confidence Ledger

- **CONFIRMED** (docstring / explicit string / qualname / `__pyx_k_` symbol / direct count): every class name and docstring; the full method roster of all nine modules; all rejection-reason and assert strings; the ISL-absence symbol sweep; the `IntegerSetAnalysis`-import count (PLF=2, others=0); the `LoopXFormBuilder` coef/scale/shift asserts; the `check_schedule` parameter set and scope-struct name.
- **STRONG** (vocab-ordering, no contradiction): per-op order construction in NeuronLoopInterchange; PSUM-fuse compatibility logic; PLF ranking (`how_good` / `max_coef_min_depth`); the `check_schedule` mechanism.
- **INFERRED** (semantics / cross-ref): exact interchange call flow; the affine-remap algebra form (`store = coef·load·scale + shift + offset`); the typical pass order.
- **Not recoverable without full decompilation** (IDA sidecars are address stubs): exact per-method instruction sequences; the integer thresholds (max insts per loop, `AccumulationGroup` size, fusion-factor formula); the precise `how_good` cost arithmetic.

---

## Cross-References

- [Penguin Axis / Loop-Axis Model](axis-loop-model.md) — the `Axis` / loop-axis objects these passes reorder, split, and coalesce
- [Penguin Dependency Model — DependencyEdge & EdgeKind](dependency-model.md) — the DAG dependence edges that the access-pattern and `check_schedule` legality tests walk
- [Penguin AffineExpr Algebra over pelican::Expr](affine-expr-algebra.md) — the `AffineExpr` index expressions that `FlattenLoop`'s `canUpdateExprAfterFlatten` and PLF's `LoopXFormBuilder` rewrite
- [Penguin Layout-Tiling Pipeline](layout-tiling-pipeline.md) — the `LayoutTilingPipeline` host that runs `LoopSplitting` and the tiling that sizes the reduce cascade
