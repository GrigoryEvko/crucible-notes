# Sharding Propagation

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ; treat every VA as version-pinned.*

## Abstract

`xla::ShardingPropagation` is the GSPMD *inference* pass: given an HLO module where the user (or the front end) has annotated *some* instructions with an `HloSharding`, it fills in a sharding for every other instruction by propagating the known ones along the dataflow graph until nothing changes. It is the manual / partial-annotation producer of the three that feed the partitioner — the other two being the auto-sharding ILP search and the Shardy import, both documented on [Auto-Sharding and SPMD Partitioner](auto-sharding-spmd.md). This pass does **not** *choose* shardings by optimization; it *deduces* the unique-or-most-specific sharding implied by the annotations already present, exactly like a type-inference pass deduces types from a partial annotation.

A reader who knows XLA-on-GPU already owns the frame: this is the same `sharding_propagation.cc` machinery upstream ships, retargeted only by the custom-call sharding helper (`TpuCustomCallShardingHelper`) that the TPU pipeline injects. The core is a worklist dataflow analysis run to a fixed point. Each iteration does two sweeps over the instruction graph: a **forward** sweep that infers an instruction's sharding from its *operands* (post-order, operands before users), and a **backward** sweep that infers it from its *users* (reverse post-order, users before operands). Every candidate is committed through one gate — `MaybeImproveInstructionSharding` — which accepts the candidate only if it is strictly more specific than (or compatibly merges with) whatever the instruction already carries. Because the gate is monotone (shardings only ever get *more* specific, never less), the fixed point is guaranteed to terminate. An `aggressiveness` level (0..3) progressively relaxes the per-op rules across iterations so that conservative inferences settle first and speculative ones only later.

This page owns the **propagation pass itself**: the pre-pass annotation scan, the forward and backward inference dataflow, the per-op inference rules (elementwise, dot, conv, reshape, gather, reduce-window), the `MaybeImproveInstructionSharding` commit gate, the shard-group and cross-computation propagation, domain normalization, and the iterate-to-fixed-point loop. It does **not** own the ILP strategy search or the SPMD partitioner that materializes collectives — those live on the sibling page. The structure is: (1) the pass driver and pre-pass scan, (2) the commit gate, (3) forward propagation + per-op rules, (4) backward propagation, (5) shard-group / cross-computation / domain propagation, (6) the fixed-point loop, (7) the custom-call helper dispatch.

For reimplementation, the contract is:

- **The inference dataflow.** Two directed sweeps per iteration — forward from operands, backward from users — over the instruction graph, with the post-order / reverse-post-order visit orders that make a single sweep converge as far as possible.
- **The commit gate.** `MaybeImproveInstructionSharding(new, instr, may_combine_partial, replace_existing)` — the monotone rule that decides whether a candidate sharding replaces, refines, or is rejected, and why monotonicity guarantees termination.
- **The per-op rules.** How each opcode family maps an operand/user sharding to the instruction's sharding (and back): elementwise pass-through, dot/conv contracting-vs-batch dimension mapping, reshape dimension splitting/merging, gather/scatter index handling, reduce-window's dilation guard.
- **Seeding and constraint.** How `Sharding` / `SPMDFullToShardShape` / `SPMDShardToFullShape` custom calls, shard-group (`shard_as` / `shard_like`) annotations, and `ShardBarrier` markers seed the inference and constrain where it may flow.

| | |
|---|---|
| **Pass name** | `"sharding-propagation"` (`name()` @ `0x1c8615a0`) |
| **Pass entry** | `xla::ShardingPropagation::RunImpl(HloModule*, exec_threads)` @ `0x213aa140` (33.6 KB — largest `RunImpl` in the file) |
| **Fixed-point loop** | `ShardingPropagation::RunToFixPoint` @ `0x1c85ae60` (22.4 KB) |
| **Forward sweep** | `InferShardingFromOperands` @ `0x1c856780` (9.9 KB) |
| **Backward sweep** | `InferShardingFromUsers` @ `0x1c859fa0` (2.2 KB) + `GetShardingFromUser` @ `0x1c8531e0` (7.3 KB) |
| **Commit gate** | `(anonymous)::MaybeImproveInstructionSharding` @ `0x1c84e980`; sub-shape variant `MaybeImproveInstructionSubSharding` @ `0x1c850820` |
| **Source path** | `third_party/tensorflow/compiler/xla/service/sharding_propagation.cc` (rodata) |
| **Custom-call helper** | `xla::jellyfish::TpuCustomCallShardingHelper` (`InferShardingFromOperands` @ `0x1278bf80`) over base `xla::CustomCallShardingHelper` @ `0x1c864120` |
| **Related flag** | `xla_tpu_sharding_metadata` — registered command-line flag in `tpu_compilation_environment.cc`; governs the constructor's `propagate_metadata` (op-name provenance carried with shardings). Default value not pinned in the decompile (LOW). |
| **Confidence** | HIGH (symbols byte-anchored; key bodies decompile-verified) unless a row/callout says otherwise |

---

## The Pass Driver

### Purpose

`ShardingPropagation::RunImpl` (`0x213aa140`) is the single entry point. It runs once per call but internally iterates to a fixed point. Its job is to take an HLO module carrying a sparse set of `HloSharding` annotations and densify them: when it returns, every instruction the pass can reach carries an inferred sharding, and the boolean return reports whether anything changed (so the surrounding `HloPassPipeline` knows whether to re-run dependents). The decompiled body anchors to `sharding_propagation.cc` and emits the completion diagnostic `"Sharding propagation completed after N iterations"`.

The TPU pipeline runs this pass **three times** inside the partitioning pipeline (see [Auto-Sharding and SPMD Partitioner](auto-sharding-spmd.md) — `AddTpuPartitioningPasses` references `ShardingPropagation` three times), because intervening rewrites (e.g. concat decomposition) create new instructions whose shardings must be re-inferred. A reimplementer must make the pass idempotent: re-running on an already-converged module must return `false` (no change).

### Constructor

The pass carries the configuration that shapes its rules. There is a single constructor (`xla::ShardingPropagation::ShardingPropagation` @ `0x1094a2c0`); the call sites are `AddPass<ShardingPropagation>` template instantiations at `0x1094a180` / `0x1093a300` (the latter passing the spans as `absl::InlinedVector<bool, 1>`):

```c
// xla::ShardingPropagation::ShardingPropagation  @ 0x1094a2c0
ShardingPropagation(
    bool is_spmd,                  // SPMD (true) vs replicated/MPMD mode
    bool propagate_metadata,       // carry sharding "metadata" (op-name provenance) forward
    absl::Span<const bool> allow_spmd_sharding_propagation_to_output,      // per-output: may infer onto root?
    absl::Span<const bool> allow_spmd_sharding_propagation_to_parameters,  // per-param:  may infer onto param?
    bool cse_prevention_only,      // restricted mode: only annotate to block bad CSE
    std::unique_ptr<CustomCallShardingHelper> sharding_helper);  // TPU passes TpuCustomCallShardingHelper
```

> **NOTE —** `cse_prevention_only` is a restricted mode where the pass does *not* attempt full inference; it only attaches shardings whose sole purpose is to prevent the CSE pass from merging two ops that must stay distinct per partition. The CSE-prevention annotation is tagged with the rodata suffix `"_sharding_propagation_cse_prevention"` (seen in `RunImpl`). A reimplementer who skips this mode will get *correct* shardings but may see later CSE collapse partition-distinct computations.

The TPU pipeline always constructs the pass with `std::make_unique<TpuCustomCallShardingHelper>()`. A four-template-argument `AddPass` instantiation (`0x14bbc2e0`, `<ShardingPropagation, bool, bool, Span<bool>, Span<bool>>`) passes only the first four constructor arguments — `cse_prevention_only` and the helper take their defaults — and uses plain `absl::Span<const bool>` for both span arguments rather than the `InlinedVector` form.

### Entry Point

```text
ShardingPropagation::RunImpl                 0x213aa140 (33.6 KB)  ── driver: scan + fixpoint
  ├─ (pre-pass linear scan, inline)                                ── gather annotations, domains, groups
  └─ RunToFixPoint                           0x1c85ae60 (22.4 KB)  ── iterate forward+backward to convergence
       ├─ InferShardingFromShardGroup        0x1c856420 (0.83 KB)  ── shard_as / shard_like group seeding
       ├─ InferShardingFromOperands          0x1c856780 (9.9 KB)   ── FORWARD: operand → instr
       │     └─ MaybeImproveInstructionSharding  0x1c84e980        ── commit gate (forward)
       ├─ InferShardingFromUsers             0x1c859fa0 (2.2 KB)   ── BACKWARD driver
       │     └─ GetShardingFromUser          0x1c8531e0 (7.3 KB)   ── BACKWARD: user → operand sharding
       ├─ MaybeComputationPropagation        0x1c85a860 (0.2 KB)   ── cross-computation (call/while/cond)
       ├─ GetRelatedInstructions             0x1c861140 (0.84 KB)  ── shard-group reachability
       └─ NormalizeDomain                    0x1c852dc0 (1.0 KB)   ── domain-boundary conflict resolution
```

### Algorithm — the pre-pass scan

Before the fixed-point loop, `RunImpl` walks the module once to collect the seeds and the constraints. The decompiled body references each marker string below.

```c
function RunImpl(module, execution_threads):                  // 0x213aa140
    // 1. Seed: collect explicit annotations and the boundary markers.
    for instr in module.instructions (post-order):
        if instr.has_sharding():
            provided_shardings.insert(instr)                  // frozen — never overwritten
        if instr.IsCustomCall("Sharding"):                    // consumer-side sharding hint
            register_domain_bracket(instr)                    // re-attached later by HloDomainRemover
        if instr.IsCustomCall("SPMDFullToShardShape") or       // manual-region entry
           instr.IsCustomCall("SPMDShardToFullShape"):        // manual-region exit
            mark_manual_boundary(instr)                       // inference stops at the boundary
        if instr.IsCustomCall("ShardBarrierFrom") or
           instr.IsCustomCall("ShardBarrierTo"):              // user-frozen sharding fence
            mark_shard_barrier(instr)                         // propagation may not cross
        scan_shard_group(instr, &instruction_to_shard_group_id,   // shard_as / shard_like
                         &shard_group_id_to_shard_as_group,
                         &shard_group_id_to_shard_like_group)

    // 2. Validate per-output / per-parameter propagation permission.
    CHECK(allow_spmd_sharding_propagation_to_output_.size() matches root tuple arity)

    // 3. Run to fixed point.
    iterations = 0
    changed = RunToFixPoint(aggressiveness=0..3, ..., module, ..., &iterations)
    LOG("Sharding propagation completed after " << iterations << " iterations")
    return changed
```

> **GOTCHA —** instructions in `provided_shardings` (those the user/front end annotated) are **frozen**. Forward and backward inference may *read* them but the commit gate refuses to overwrite them — they are the boundary conditions of the dataflow problem. A reimplementer that treats a user annotation as just-another-candidate will let inference clobber the user's intent. The set is threaded through every helper as `const absl::flat_hash_set<const HloInstruction*>& provided_shardings`.

---

## The Commit Gate — `MaybeImproveInstructionSharding`

### Purpose

Every candidate sharding produced by forward or backward inference is funneled through one function, `(anonymous)::MaybeImproveInstructionSharding` (`0x1c84e980`). It is the monotonicity guarantee: it commits a candidate to an instruction only if doing so makes the instruction's sharding *strictly more specific*, or *merges compatibly* with the existing one. Because shardings only climb a lattice (replicated → partially-tiled → fully-tiled) and never descend, the worklist must reach a fixed point.

### Algorithm

The decompiled signature is `MaybeImproveInstructionSharding(HloSharding sharding, HloInstruction* instr, bool may_combine_partial_sharding, int replace_existing, ...)`. Reconstructed logic:

```c
// (anonymous)::MaybeImproveInstructionSharding  @ 0x1c84e980
function MaybeImproveInstructionSharding(new_sharding, instr,
                                         may_combine_partial_sharding,
                                         replace_existing):
    if not instr.has_sharding():
        instr.set_sharding(new_sharding)                  // first assignment — always commit
        return true
    old = instr.sharding()
    if old == new_sharding:
        return false                                       // no progress
    if may_combine_partial_sharding and
       both old and new are partially-tiled and compatible:
        merged = MergeSharding(old, new_sharding)          // union of tiled dims — strictly tighter
        instr.set_sharding(merged)
        return true                                        // "Refined partial sharding"
    if IsStrictlyMoreSpecific(new_sharding, old) or replace_existing:
        instr.set_sharding(new_sharding)
        return true
    return false                                           // candidate rejected — keep old
```

The two log strings the fixed-point loop emits on success — `"Add sharding (forward-pass): "` / `"Refined partial sharding (forward-pass): "` (and the `(backward-pass)` / `(shard group)` variants) — distinguish the *first assignment* path from the *partial merge* path. The `MaybeImproveInstructionSubSharding` variant (`0x1c850820`) does the same for one element of a tuple-shaped instruction, indexed by a `ShapeIndex`.

> **QUIRK —** `may_combine_partial_sharding` is what lets two *different* partial shardings inferred from two different directions co-exist by *intersecting* their tiled-dimension sets, rather than one rejecting the other. Without it, a tensor that is row-sharded according to its producer and column-sharded according to its consumer would oscillate (each sweep rejecting the other's choice) instead of converging to the row-and-column (2-D tiled) sharding both imply. The merge is the reason the forward and backward sweeps *cooperate* rather than fight.

---

## Forward Propagation — Inferring from Operands

### Purpose

The forward sweep, `InferShardingFromOperands` (`0x1c856780`, the largest helper at 10.1 KB), computes a candidate sharding for an instruction from the shardings already on its *operands*, then offers it to the commit gate. It visits instructions in **post-order** (every operand before its users) so that within a single sweep a freshly-inferred operand sharding immediately feeds its user.

### The per-op inference rules

The forward rule is opcode-dependent. The shape of the rule space — confirmed by the opcode-specific diagnostics in the decompiled body (`"Not applying sharding to reduce window because dilatation isn't supported yet"`, the `"sort"` handling, the `SPMDShardToFullShape` boundary check) — is a switch over `HloOpcode` families:

| Opcode family | Forward rule (operand sharding → instruction sharding) | Confidence |
|---|---|---|
| Elementwise (`add`, `mul`, `select`, unary math, …) | Pass-through: instruction takes the (merged) sharding of its operands; all operands and the result share dims one-to-one | HIGH |
| `broadcast` | Operand sharding lifted onto the broadcast result, mapping operand dims to their broadcast positions; new dims replicated | HIGH |
| `reshape` | Map operand tiled dims through the reshape: a split dim propagates to the split factors, a merged dim only if both inputs agree; otherwise no inference (left for backward) | MEDIUM |
| `transpose` / `reverse` | Permute / mirror the operand's tile assignment by the dimension map | HIGH |
| `dot` | Map LHS/RHS batch dims → output batch dims; a sharded **contracting** dim implies the output is *unreduced* (partial-sum) and forces an AllReduce later; non-contracting dims map to output dims | HIGH |
| `convolution` | Same dimension-numbers machinery as dot: batch/feature/spatial dims mapped LHS→output; sharded spatial dims propagate to output spatial dims | HIGH |
| `reduce` | Drop the reduced dims from the operand sharding; a sharded reduced dim yields an unreduced result | HIGH |
| `reduce-window` | Like `reduce` but **guarded**: if the window has dilation the rule bails (`"… because dilatation isn't supported yet"`) and leaves the op unsharded | HIGH |
| `gather` | Operand-data sharding maps to the gathered output through the gather dim-numbers; index sharding handled separately | MEDIUM |
| `scatter` | Map the updates/operand shardings; the index operand constrains which output dims may shard | MEDIUM |
| `pad` / `slice` / `dynamic-slice` | Tiling pass-through on untouched dims; padded/sliced dims only propagate when the boundary aligns to the tile | MEDIUM |
| `sort` | Sorted dim must be replicated (cannot tile the comparison axis); other dims pass through | HIGH |
| `kCustomCall` | Delegated to the `CustomCallShardingHelper` vtable (see below) | HIGH |

```c
// InferShardingFromOperands  @ 0x1c856780  (post-order visit)
function InferShardingFromOperands(instr, computation_map, is_spmd,
                                   aggressiveness, sharding_helper,
                                   call_graph):
    if instr in provided_shardings: return false           // frozen
    if instr.IsCustomCall("SPMDShardToFullShape"): return  // boundary: do not look inside

    switch opcode_family(instr):
        ELEMENTWISE:   cand = MergeOperandShardings(instr)
        DOT, CONV:     cand = MapByDimensionNumbers(instr)  // batch/contracting/spatial map
        REDUCE:        cand = DropReducedDims(operand_sharding, instr.dimensions())
        REDUCE_WINDOW: if HasDilation(instr.window()): return false   // guard
                       cand = DropReducedDims(...)
        RESHAPE:       cand = ReshapePropagate(operand_sharding, instr)  // may be no-op
        GATHER/SCATTER:cand = IndexAndDataPropagate(instr)
        SORT:          cand = ReplicateSortedDim(operand_sharding, instr)
        CUSTOM_CALL:   cand = sharding_helper.InferShardingFromOperands(instr)
        ...
    if cand.has_value():
        may_combine = (aggressiveness >= 1)                 // partial merge enabled past iter 0
        return MaybeImproveInstructionSharding(cand, instr, may_combine, /*replace=*/false)
    return false
```

> **NOTE —** the `aggressiveness` (0..3) parameter, threaded from `RunToFixPoint`, relaxes the rules monotonically across iterations. At level 0 only the unambiguous, no-merge inferences fire (so the most certain shardings settle first); higher levels enable partial-sharding merges and speculative propagations (e.g. propagating onto an op whose operands only *partially* agree). The exact per-opcode threshold table is encoded in the switch arms and was not exhaustively decompiled — the level-gating is HIGH confidence, the precise per-opcode cutoffs are LOW.

---

## Backward Propagation — Inferring from Users

### Purpose

The backward sweep is the dual: `InferShardingFromUsers` (`0x1c859fa0`) drives it and `GetShardingFromUser` (`0x1c8531e0`) does the work, computing what sharding a *user* implies for one of its operands, then offering it to the same commit gate. It visits in **reverse post-order** (every user before its operands) so a user's sharding flows back to its inputs in one sweep. This is what lets an output sharding (annotated on the root) reach all the way back to the parameters.

### Algorithm

`GetShardingFromUser` is the inverse of the forward per-op map: given `user` and which operand index `instr` occupies, it back-projects `user.sharding()` onto that operand's shape. The decompiled body's diagnostic `"update_index <= operand_count"` confirms the dynamic-update-slice / scatter back-projection path (the update operand's sharding is derived from the output sharding).

```c
// InferShardingFromUsers  @ 0x1c859fa0  (reverse-post-order)
function InferShardingFromUsers(instr, ..., aggressiveness, is_spmd, sharding_helper):
    if instr in provided_shardings: return false
    for user in instr.users():
        if not user.has_sharding(): continue
        cand = GetShardingFromUser(*instr, *user, aggressiveness,   // 0x1c8531e0
                                   is_spmd, call_graph, sharding_helper)
        if cand.has_value():
            changed |= MaybeImproveInstructionSharding(
                           cand, instr, /*may_combine=*/aggressiveness >= 1, false)
    return changed

// GetShardingFromUser  @ 0x1c8531e0 — inverse per-op map
function GetShardingFromUser(operand, user, ...):
    switch user.opcode():
        ELEMENTWISE: return user.sharding()                 // same shape, same tiling
        TRANSPOSE:   return InversePermute(user.sharding(), user.dimensions())
        RESHAPE:     return ReshapeBackProject(user.sharding(), operand.shape())
        DOT/CONV:    return BackProjectByDimNumbers(user, operand_index)
        DYNAMIC_UPDATE_SLICE / SCATTER:
                     return user.sharding()  // operand inherits output tiling; index handled separately
        GATHER:      return DataOperandSharding(user)        // only for the data operand
        ...
        default:     return nullopt                          // cannot back-infer
```

> **GOTCHA —** backward inference is intentionally *weaker* than forward: many opcodes back-project to `nullopt` (e.g. there is no safe way to infer a reduce operand's sharding from the reduced result). Forward inference is the workhorse; backward inference exists primarily to push an **output** sharding (root-annotated or `SPMDShardToFullShape`-implied) toward the parameters, and to resolve shape-changing ops the forward sweep left blank. A reimplementer who makes backward as eager as forward will introduce spurious shardings that the forward sweep then has to fight.

---

## Group, Cross-Computation, and Domain Propagation

Three auxiliary mechanisms run alongside the two main sweeps inside `RunToFixPoint`.

### Shard groups — `InferShardingFromShardGroup`

`shard_as` and `shard_like` annotations declare that a *set* of instructions must share a sharding regardless of dataflow. `InferShardingFromShardGroup` (`0x1c856420`) propagates: for each shard-group ID with at least one member carrying an explicit sharding, it copies that sharding to every other member. The two flavors differ in their shape precondition (recovered from the `"Aligning shard group: "`, `"Shard-As group "`, `"Shard-Like group "` strings in the loop):

- `shard_as` requires the members to have **identical shapes** (`ShapeUtil::SameDimensions` plus element-type match).
- `shard_like` requires only **same dimensions** (element type may differ).

`GetRelatedInstructions` (`0x1c861140`) collects the transitive closure of a group through its edges, so a change to one member re-queues the whole group on the next iteration. Commits go through the same gate, logged as `"Add sharding (shard group): "` / `"Refined partial sharding (shard group): "`.

> **QUIRK —** shard groups are bounded by the `ShardBarrierFrom` / `ShardBarrierTo` custom calls. A barrier marks a sharding the user has *frozen* (typically inserted by the Shardy import to preserve a user constraint); propagation — forward, backward, or group — may not cross it. The pre-pass scan records barrier instructions and the sweeps skip them, so a reimplementer must treat a barrier as an inference firewall, not a no-op.

### Cross-computation — `MaybeComputationPropagation`

When a `kCall`, `kWhile`, or `kConditional` instruction acquires a sharding, the implied sharding on the *called computation's* parameters and root must be propagated into that computation, and vice-versa. `MaybeComputationPropagation` (`0x1c85a860`) does this and re-adds the affected computation to the worklist (it logs `"Consider computation: "`). This is what threads sharding through control-flow boundaries; the partitioner's `PreprocessCallSites` later relies on these being consistent.

### Domain normalization — `NormalizeDomain`

`Sharding` custom calls create `kDomain` brackets (paired enter/exit instructions delimiting a region of one sharding). After inference, `NormalizeDomain` (`0x1c852dc0`) resolves any conflict between the sharding *inferred inside* a domain and the *explicit* sharding on the domain boundary, picking the tightest (replicated < partial < tiled). `HloDomainRemover("sharding", …)` then strips the brackets. The pass cooperates with `hlo_sharding_util::CanonicalizeLayoutAfterShardingPropagation` (`0x1e3d55a0`), which fixes up tensor layouts to be consistent with the chosen tiling once propagation has converged.

---

## The Fixed-Point Loop — `RunToFixPoint`

### Purpose

`RunToFixPoint` (`0x1c85ae60`) is the outer driver that repeats the sweeps until convergence. Its reconstructed signature (recovered from the demangled symbol and the surrounding by-reference arguments) is the long parameter list carrying the worklist state, the shard-group maps, and the `int64_t& iterations` out-parameter.

### Algorithm

The decompiled body confirms a per-iteration structure that logs `"Sharding propagation iteration N"` and the running counters `"\n  instructions already sharded: "`, `"\n  shardings inferred from operands: "`, `"\n  shardings inferred from users: "`, `"\n  shardings inferred from shard group: "`, `"\n  total instructions: "`, plus the current `"\n  aggressiveness: "`.

```c
// ShardingPropagation::RunToFixPoint  @ 0x1c85ae60
function RunToFixPoint(aggressiveness_max, propagate_shard_group,
                       computation_map, provided_shardings, call_graph,
                       module, exec_threads, unspecified_dims,
                       shard_group maps..., &iterations):
    any_changed = false
    for aggressiveness in 0 .. aggressiveness_max:        // 0..3 outer schedule
        do:                                               // inner fixpoint at this level
            changed = false
            if propagate_shard_group:
                changed |= InferShardingFromShardGroup(...)         // 0x1c856420
            for instr in post_order(module):                        // FORWARD sweep
                changed |= InferShardingFromOperands(instr, ..., aggressiveness)
            for instr in reverse_post_order(module):                // BACKWARD sweep
                changed |= InferShardingFromUsers(instr, ..., aggressiveness)
            for comp_instr in control_flow_instrs:
                changed |= MaybeComputationPropagation(...)          // 0x1c85a860
            iterations += 1
            LOG("Sharding propagation iteration " << iterations)
            any_changed |= changed
        while changed                                     // until no instruction's sharding moved
    NormalizeDomain(...)                                  // 0x1c852dc0 — boundary cleanup
    return any_changed
```

The decompiled control flow shows the nested `do { … } while (changed)` over the aggressiveness schedule (the outermost `do` at the top of the loop body wrapping the per-level convergence). Termination has two guarantees: the **monotone commit gate** (no sharding ever loosens, and the lattice has finite height bounded by the number of tensor dimensions), and an internal iteration cap.

> **NOTE —** the iteration cap is passed/observed as a `long&` (`iterations`) but the *constant* it is compared against was not pinned in the decompile — the loop primarily relies on the no-change condition, with the cap as a safety net. Treat the existence of a cap as HIGH and its exact value as LOW; a reimplementer should bound iterations defensively (the lattice-height bound is the principled limit) rather than trusting an unbounded loop.

---

## Custom-Call Sharding Helper Dispatch

### Purpose

`kCustomCall` instructions are opaque to the generic per-op rules, so both sweeps delegate them to a `CustomCallShardingHelper` virtual table threaded through the pass. The base `xla::CustomCallShardingHelper::InferShardingFromOperands` (`0x1c864120`) returns **no sharding** (`std::nullopt` — its body sets the result's presence byte to 0 and returns), leaving the instruction for the rest of the dataflow to handle. The TPU pipeline injects `xla::jellyfish::TpuCustomCallShardingHelper` (`InferShardingFromOperands` @ `0x1278bf80`), which overrides the rule for the TPU custom calls it recognizes.

### The TPU helper's handled targets

The decompiled `TpuCustomCallShardingHelper::InferShardingFromOperands` (`0x1278bf80`) tests `IsCustomCall` against a fixed set of targets, in this order; any target it does not match returns `std::nullopt` (the no-sharding sentinel, same as the base). The matched branches:

| Custom-call target | Sharding rule | Confidence |
|---|---|---|
| `xla-sdc-checker-get-checksums` | Returns `std::nullopt` — the SDC-checker debug custom call is left unsharded (falls to the same exit as an unmatched target) | HIGH |
| `QrDecompositionBlock` | Non-trivial: when operand 0 carries a sharding, builds a **two-element tuple sharding** — the operand's sharding plus a derived block sharding (`(anonymous)::DeriveQrBlockShardingFromOtherSharding`); otherwise `nullopt` | HIGH |
| `MoveToHost` | Operand-following: result takes operand 0's sharding — **unless** the instruction's own sharding is already replicated, in which case it returns `nullopt` | HIGH |
| `PartialReduce` | Non-trivial: when operand 0 is sharded, consults `reduction_dim` from the backend config (`"PartialReduce backend config cannot be null."` guard); the reduced dim drops, the rest pass through | HIGH |

The function also matches one further target compared with length 7 (via a float-taking `IsCustomCall` overload) whose branch is operand-following; its literal was not recovered from the decompile (the comparison reads through a `.data` pointer, not a `.rodata` string), so it is omitted here.

> **NOTE —** `PartialReduce` is *not* the only non-trivial rule: `QrDecompositionBlock` is also dimension/shape-changing (it synthesizes a tuple sharding). There is **no `MoveToDevice`** branch in this function, and `xla-sdc-checker-get-checksums` returns `nullopt` rather than following its operand.

The sibling helpers `ShardBarrierFromPartitioner` / `ShardBarrierToPartitioner` (`InferShardingFromOperands` @ `0x1c863740` / `0x1c863800`) and `InspectShardingCallPartitioner` (`jax::…` @ `0xe8b8cc0`) implement the barrier-fence and debug-inspect behaviors referenced above.

---

## Related Components

| Component | Relationship |
|---|---|
| `TpuAutoSharding` / `AutoSharding` | Alternative sharding *producer* (ILP search) — mutually exclusive with this pass per module; see [Auto-Sharding and SPMD Partitioner](auto-sharding-spmd.md) |
| `ShardyXLA` (`"shardy-xla"`) | Alternative producer (Shardy/JAX-native import); falls back to GSPMD propagation when both Shardy and `xla.sharding` attrs co-exist |
| `TpuSpmdPartitioner` (`"tpu-spmd-partitioning"`) | The *consumer*: materializes the inferred shardings into per-partition HLO + collectives; see the sibling page |
| `HloDomainRemover` | Strips the `kDomain` brackets `Sharding` custom calls created, after `NormalizeDomain` resolves them |
| `CanonicalizeLayoutAfterShardingPropagation` (`0x1e3d55a0`) | Fixes tensor layouts to be consistent with the chosen tiling once propagation converges |

## Cross-References

- [Auto-Sharding and SPMD Partitioner](auto-sharding-spmd.md) — the ILP strategy *search* and the SPMD *partitioner* (collective materialization); the two other sharding producers and the consumer this pass feeds
- [The TPU Compiler](overview.md) — Part V orientation; where sharding/SPMD sits in the five-phase descent
- [Compile Phases](compile-phases.md) — the PjRt partial-program phase spine that hosts `RunHloPasses`
- [HLO Pre-Passes](hlo-pre-passes.md) — the HLO-level passes that run before sharding inference
- [HLO Pass Registry](hlo-pass-registry.md) — how `ShardingPropagation` is added to the pipeline (the `AddPass<>` instantiations)
- [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md) — the downstream lowering of the dot/conv ops whose contracting/batch-dim shardings this pass infers
- [Collectives Overview](../collectives/overview.md) — the post-SPMD collectives the partitioner emits once shardings are inferred
