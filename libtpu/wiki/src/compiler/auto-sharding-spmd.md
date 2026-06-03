# Auto-Sharding and SPMD Partitioner

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

Two coupled engines turn a partially-annotated XLA program into a per-partition SPMD program. The first, **auto-sharding** (`xla::TpuAutoSharding` wrapping the open-source `xla::AutoSharding`), *chooses* a sharding for every instruction that lacks one: it enumerates a discrete set of candidate strategies per op, attaches a cost vector to each, builds a strategy/cost graph over the instruction DAG, and hands the whole thing to a true mixed-integer program solved with OR-Tools' `operations_research::MPSolver` (CP-SAT integer backend, GLOP for the LP relaxation). The second, **`xla::jellyfish::TpuSpmdPartitioner`**, *materializes* the decision: it walks the HLO instruction-by-instruction and rewrites each op into the per-partition slice it computes, inserting the collectives (AllReduce / AllGather / AllToAll / CollectivePermute) and the halo exchanges that make the rewrite numerically correct.

A reader who knows XLA-on-GPU already owns most of the frame: this is the same GSPMD machinery upstream ships, retargeted with a TPU cost model (an alpha-beta link model over the ICI mesh) and a thin TPU partitioner subclass. The auto-sharding ILP is the [Alpa](https://arxiv.org/abs/2201.12023) formulation — one binary strategy variable per (node, strategy), one binary resharding variable per (edge, strategy-pair), a peak-memory constraint, and a linear objective summing per-node compute/communication cost and per-edge resharding cost. The partitioner is the open-source `SpmdPartitioner` driver with a `SpmdPartitioningVisitor` dispatching ~53 `Handle*` methods; the TPU subclass overrides only four of them and supplies no special collective creator — it uses `GetDefaultCollectiveOpsCreator`, and all TPU-specific collective shaping happens *downstream* in the post-SPMD collective rewrites (see [Collectives](../collectives/overview.md)).

This page owns the **solver, the partition rewrite, and the collective materialization**. The propagation pass that *infers* shardings from explicit annotations — its forward/backward inference rules, the fixed-point loop, the custom-call sharding helpers — is documented on [Sharding Propagation](sharding-propagation.md); this page links it and does not restate its rules. The page is structured as: (1) the auto-sharding strategy/cost/solve pipeline, (2) the MIP formulation, (3) the TPU partitioner driver, (4) the per-op visitor and partition rewrite, (5) collective insertion and halo exchange, (6) the TPU-specific extensions (windowed-einsum, scaled-dot, SparseCore two-level partitioning), and (7) the flag catalog.

For reimplementation, the contract is:

- **The strategy-enumeration model.** Per-op candidate generation (`DotHandler`/`ConvHandler` plus the builtin enumerators), the `ShardingStrategy` = (sharding, cost) tuple, and the `StrategyGroup` that holds one candidate set per instruction.
- **The cost vector and the ILP.** The alpha-beta communication-cost model, node vs edge cost, and the integer program (variables, constraints, objective) solved by `FormulateAndSolveMIPFromProblem`.
- **The partition rewrite.** The `SpmdPartitioner::RunImpl` preprocess→partition→finalize spine and the `SpmdPartitioningVisitor` per-op dispatch that produces per-partition HLO.
- **Collective insertion.** The 5-callback `SPMDCollectiveOpsCreator`, the per-opcode rule that decides which collective to emit, and the `ExchangeHalo` family for windowed ops.

| | |
|---|---|
| **Auto-sharding entry** | `xla::TpuAutoSharding::RunImpl` @ `0x1118d3a0` → `xla::AutoSharding::RunImpl` @ `0x1280a180` |
| **Auto-sharding core** | `xla::AutoShardingImplementation::RunAutoSharding` @ `0x128055a0` (12.4 KB) |
| **MIP solver entry** | `xla::spmd::FormulateAndSolveMIPFromProblem` @ `0x128407c0` (22.5 KB) |
| **Solver backend** | OR-Tools `operations_research::MPSolver` — CP-SAT (`SatInterface`) integer + GLOP (`GLOPInterface`) LP |
| **Partitioner entry** | `xla::jellyfish::TpuSpmdPartitioner::RunImpl` @ `0x127a2a80` (4.8 KB) → base `xla::spmd::SpmdPartitioner::RunImpl` @ `0x1c7fe400` |
| **Per-op dispatch** | `xla::spmd::SpmdPartitioningVisitor` — ~53 `Handle*` methods |
| **Collective creator** | `xla::spmd::GetDefaultCollectiveOpsCreator` @ `0x1c7fc120` (5 `std::function` fields; no ReduceScatter callback) |
| **Pass-pipeline host** | `xla::jellyfish::AddTpuPartitioningPasses` @ `0x1278a440` |
| **Pass names** | `"auto-sharding-automatic-partition"`, `"tpu-spmd-partitioning"`, `"tpu-partition-assignment"` |
| **Auto entry gate** | flag `xla_tpu_spmd_auto_partitioning` (default **false**) |
| **Confidence** | HIGH (symbols byte-anchored; key bodies decompile-verified) unless a row/callout says otherwise |

---

## The Partitioning Pipeline

### Purpose

Sharding and partitioning is not one pass but a nested pipeline added inside `RunHloPasses`. `AddTpuPartitioningPasses` (`0x1278a440`, ~3.7 KB) is its host. Its job is to take an HLO module that may carry *some* sharding annotations and produce a module where every instruction is annotated and then rewritten to its per-partition form.

### Entry Point

The decompiled `AddTpuPartitioningPasses` (`0x1278a440`) references each pass below by name. `ShardingPropagation` appears three times — it is re-run between transformations that create new propagation opportunities. Two upstream passes (`TpuAutoSharding` / `ShardyXLA`) are added by the sibling stage `AddAutoShardingAndRelatedPasses`; this page covers the auto path and the partitioner, and links propagation/Shardy out.

```text
RunHloPasses
  AddAutoShardingAndRelatedPasses        ── choose shardings
    ShardingPropagation                  ── manual / partial-annotation flow  (see sharding-propagation.md)
    TpuAutoSharding                       ── auto flow ("auto-sharding-automatic-partition")
    ShardyXLA                             ── Shardy (JAX-native) flow  (see sharding-propagation.md)
  AddTpuPartitioningPasses  0x1278a440   ── materialize per-partition HLO
    SpmdPrepare              0x12dfc300
    ConvOperandSwapper
    TpuSpmdConcatRewriter    0x1278f900   ── "tpu-spmd-concat-rewriter"
    HloConstantSplitter
    TpuPartitionAssignment   0x1278f040   ── "tpu-partition-assignment"  (gated, default off)
    TpuSpmdPartitioner       0x127a2a80   ── "tpu-spmd-partitioning"  ← the rewrite
    RecognizeReduceWindow
    CollectivePermuteCSE
    WholeGraphManualPass
```

> **NOTE —** there are three sharding *producers* and one *consumer*. The producers — `ShardingPropagation` (GSPMD inference), `TpuAutoSharding` (ILP search), and `ShardyXLA` (Shardy import) — are mutually exclusive per module and all converge on the same product: every `HloInstruction` carries an `HloSharding`. The consumer, `TpuSpmdPartitioner`, does not care which producer ran. A reimplementer can build the partitioner against the sharding annotation alone.

---

## Auto-Sharding — Strategy Search

### Purpose

`TpuAutoSharding` ("auto-sharding-automatic-partition", vtable `0x21822a80`) is the auto path: when the user supplies no shardings, it *derives* the best one by formulating the choice as a discrete optimization. It is a thin TPU wrapper around the open-source `xla::AutoSharding`; the wrapper adds `ApplyShardingConfig` (`0x1118c100`, load a stashed config) and `ExtractShardingConfig` (`0x1118c820`, serialize the chosen shardings for replay), then `RunImpl` (`0x1118d3a0`) dispatches into the base. Entered only when `xla_tpu_spmd_auto_partitioning` is set (default false).

### Entry Point

```text
TpuAutoSharding::RunImpl                 0x1118d3a0 (0.85 KB)  ── apply config, dispatch
  AutoSharding::RunImpl                  0x1280a180 (11.0 KB)
    AutoShardingImplementation::RunAutoSharding  0x128055a0 (12.4 KB)  ── the multi-phase core
```

### Algorithm

`RunAutoSharding` (`0x128055a0`) carries ten source-location `::site` data objects (`$_2`..`$_11`, at `0x2230c9c0`..`0x2230ca98`, 24 bytes apart) — the status-annotation sites that tag each phase's error path. The phase order, recovered from those sites and the helper symbols around them:

```c
function RunAutoSharding(module, exec_threads):              // 0x128055a0
    SaveAndRemoveShardingAnnotation(module)                  // 0x128020a0 — stash user shardings, clear them
    CanonicalizeLayouts(module)                              // 0x12803860
    cost = BuildHloCostAnalysis(module, tpu_shape_size_fn)   // custom shape-size lambda
    meshes = ComputeMeshShapeCandidates(option)              // try_multiple_mesh_shapes
    for mesh in meshes:                                       // single mesh unless search enabled
        aliases = BuildAliasSet(module)                      // 0x12e174a0 — params/outputs forced equal
        strat_graph = BuildStrategyAndCost(module, cost, mesh)  // per-op StrategyGroup + edge costs
            // DotHandler/ConvHandler register matmul/conv strategies
        ComputeAliasCompatibility(strat_graph)               // 0x12e188e0 — trim alias-incompatible
        TrimOrGenerateStrategiesBasedOnExistingSharding(...)  // honor surviving user shardings
        problem = LowerToIopddl(strat_graph, mesh, budget)    // iopddl::Problem (the MIP IR)
        FindShavedStrategies(problem)                         // 0x12851a60 — drop infeasible strategies
        output = CreateAutoShardingSolverRequestAndCallSolver(problem)  // 0x127f24a0 → MIP solve
        if output.feasible: break                            // else try next mesh
    if !output: error "could not find a solution for any of the mesh shapes tried"
    GenerateReduceScatter(strat_graph, output)               // 0x127fefc0 — RS opportunities
    InsertReshardReshapes(module, output)                    // 0x127f7ee0
    SetHloSharding(module, output)                           // 0x127f7540 — commit chosen shardings
```

> **GOTCHA —** auto-sharding does **not** support `shard_as`/`shard_like` group annotations (string `"Auto-sharding currently does not support shard_as/shard_like sharding annotations"`). Those are a propagation-only feature ([Sharding Propagation](sharding-propagation.md)). A reimplementer wiring the auto path must reject shard-group inputs, not silently ignore them.

### Strategy Enumeration

A strategy is a candidate sharding for one op together with its cost; the set of candidates for one op is a `StrategyGroup` (recursive for tuples). Candidates come from per-op-family generators. `DotHandler` (matmul, confirmed at `DotHandler::RegisterStrategies` `0x12825140`) enumerates one strategy per way of sharding the dot's batch / contracting / non-contracting dimensions, naming each via `GenerateNameForDotSharding(lhs_spec, rhs_spec)`:

| Dot strategy | Sharding pattern | Collective implied at partition time |
|---|---|---|
| `Replicated` | all dims replicated | none |
| `Tile[batch]` | batch-parallel | none (already per-partition) |
| `Tile[contracting] + AllReduce` | Megatron tensor-parallel | AllReduce on output |
| `Tile[non-contracting] + AllGather` | output-only shard | AllGather on the sharded operand |
| `Tile[contracting] + ReduceScatter` | FSDP-style | ReduceScatter (materialized downstream) |
| `ag_windowed_einsum_o%dt%dm%d` | windowed-einsum (AllGather) | per-partition Dot + intra-loop CollectivePermute |
| `rs_windowed_einsum_t%dm%d` | windowed-einsum (ReduceScatter) | intra-loop AllReduce → DynamicSlice |

The two windowed-einsum appenders are confirmed as their own TU-private functions: `DotHandler::AppendAllGatherWindowedEinsumStrategyForOperand` (`0x12826780`) and `DotHandler::AppendReduceScatterWindowedEinsumStrategy` (`0x128270a0`). `ConvHandler` shares the same `HandlerBase` base. Other op families use the builtin enumerators:

| Generator | Address | Generates for |
|---|---|---|
| `EnumerateAllPartition` | `0x127ec800` | n-D tilings of an array op |
| `EnumerateAll1DPartition` | `0x127eaa60` | single-axis tilings |
| `AddReplicatedStrategy` | `0x127e8940` | the always-present replicated fallback |
| `FollowReduceStrategy` | `0x127e4640` | reduce ops following operand sharding |
| `CreateReshapeStrategies` | `0x127f1ce0` | reshape relayouts |
| `GenerateOutfeedStrategy` | `0x127e7780` | outfeed |
| `HandlePartialReduce` | `0x127e1e00` | TPU `partial_reduce_handler::kPartialReduce` custom-call |
| `MaybeFollowInsStrategyGroup` | `0x127e4340` | tuple / get-tuple-element (inherit followed op's set) |

### Cost Model — Alpha-Beta over the ICI Mesh

`xla::spmd::ClusterEnvironment` (ctor `0x12808800`) owns the cost model. It holds the physical 1-D `DeviceMesh`, the reshaped N-D logical mesh, and per-mesh-dim `device_mesh_alpha` (fixed latency, seconds) and `device_mesh_beta` (per-byte cost, seconds/byte) vectors. Each collective cost is the standard alpha-beta ring-cost formula, with `n[d]` = mesh size along dim `d` and `B` = bytes moved:

```text
AllReduceCost(B, d)     = α[d] + 2·(n[d]-1)/n[d]  · B · β[d]    // 0x12de7980
AllGatherCost(B, d)     = α[d] +   (n[d]-1)/n[d]  · B · β[d]    // 0x12de77a0
ReduceScatterCost(B, d) = α[d] +   (n[d]-1)/n[d]  · B · β[d]    // 0x12de7b80
AllToAllCost(B, d)      = α[d] +   (n[d]-1)/n[d]² · B · β[d]    // 0x12de7d60
```

> **NOTE —** the four cost-method addresses are byte-confirmed; the closed-form expressions are the canonical Alpa/XLA ring-cost forms reconstructed from the method names and operand shapes, **not** read off the arithmetic of the disassembly (MEDIUM confidence on the exact coefficients). `ReshardingCost` (`0x12de9860`) combines these with `CollectivePermuteCost` (`0x12de8f00`) to price an arbitrary source→destination sharding change; `GetMeshDimPermutationOrderInShardingSpec` (`0x12e2ed00`) decides whether a reshard goes through AllToAll or CollectivePermute. The TPU mesh's default alpha/beta values are version-specific (Jellyfish v3 vs Pufferfish v5 …) and were **not pinned** in the binary (LOW); the string `"If not sure how to set device_mesh_alpha and device_mesh_beta, please leave them empty and default values will be used."` shows defaults exist.

These per-strategy costs become two cost arrays: a **node cost** (compute + the communication a strategy forces on the op itself) and an **edge cost** (the resharding cost when a producer's strategy and a consumer's strategy disagree), filled by `ComputeCommunicationCost`, `CommunicationReshardingCostVector`, and `MemoryReshardingCostVector`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuAutoSharding::RunImpl` | `0x1118d3a0` | TPU wrapper, dispatch to base | HIGH |
| `TpuAutoSharding::ApplyShardingConfig` | `0x1118c100` | apply stashed config | HIGH |
| `TpuAutoSharding::ExtractShardingConfig` | `0x1118c820` | serialize chosen shardings | HIGH |
| `AutoSharding::RunImpl` | `0x1280a180` | base entry | HIGH |
| `AutoShardingImplementation::RunAutoSharding` | `0x128055a0` | multi-phase core | HIGH |
| `SaveAndRemoveShardingAnnotation` | `0x128020a0` | stash + clear user shardings | HIGH |
| `BuildAliasSet` | `0x12e174a0` | force aliased params/outputs equal | HIGH |
| `DotHandler::RegisterStrategies` | `0x12825140` | matmul strategy enumeration | CONFIRMED |
| `DotHandler::AppendAllGatherWindowedEinsumStrategyForOperand` | `0x12826780` | AG windowed-einsum candidate | CONFIRMED |
| `DotHandler::AppendReduceScatterWindowedEinsumStrategy` | `0x128270a0` | RS windowed-einsum candidate | CONFIRMED |
| `ClusterEnvironment::ClusterEnvironment` | `0x12808800` | cost model state | HIGH |
| `StrategyShaverForProblem::FindShavedStrategies` | `0x12851a60` | drop infeasible strategies | HIGH |
| `SetHloSharding` | `0x127f7540` | commit | HIGH |

---

## The MIP Formulation

### Purpose

The strategy choice is solved as a genuine mixed-integer program, not a greedy heuristic. The decompiled `FormulateAndSolveMIPFromProblem` (`0x128407c0`, ~22.5 KB) is built directly on OR-Tools — its locals are `operations_research::MPSolver*`, `MPObjective*`, `MPConstraint*`, and `MPVariable*`, and it constructs row constraints (`MakeRowConstraint`) and an objective. Two solver backends are linked: `operations_research::SatInterface` (CP-SAT, `Solve` `0x1285dce0`) for the integer phase, the default, and `operations_research::GLOPInterface` (LP simplex, `Solve` `0x12ef4760`) for the relaxation. None of the third-party LP solvers (Gurobi, HiGHS, …) are linked as code — only as descriptor protos — so the live backend is **GLOP + CP-SAT**.

### Algorithm — the integer program

The model is the Alpa formulation: one one-hot strategy vector per node, one one-hot resharding vector per edge, a peak-memory constraint, and a linear objective. The intermediate representation is `iopddl::Problem` (nodes, edges, strategies), confirmed as the first formal parameter of the solver entry — its full signature is `FormulateAndSolveMIPFromProblem(const iopddl::Problem&, const xla::spmd::AutoShardingSolverParams&)`.

```text
DECISION VARIABLES
  s[v][k] ∈ {0,1}        per node v, per candidate strategy k
  e[(u,v)][i,j] ∈ {0,1}  per edge (u,v), per (u-strategy i, v-strategy j) pair

CONSTRAINTS
  Σ_k s[v][k] = 1                      ∀ v                    // exactly one strategy
  Σ_{i,j} e[(u,v)][i,j] = 1            ∀ (u,v)                 // one resharding per edge
  e[(u,v)][i,j] ≤ s[u][i]                                     // edge–node consistency
  e[(u,v)][i,j] ≤ s[v][j]
  Σ_{v live at t} mem(s[v][·]) ≤ memory_budget_per_device     // peak-memory, per time step t
  alias-follow: aliased params/outputs must take matching strategies   // BuildAliasSet
  group: shard_as/shard_like members forced identical (propagation path only)

OBJECTIVE  (minimize)
  Σ_v Σ_k  node_cost[v][k]·s[v][k]
+ Σ_(u,v) Σ_(i,j)  edge_cost[(u,v)][i,j]·e[(u,v)][i,j]
```

Two TPU-relevant preprocessing steps shrink the program before the solve. `CheckDominance` (`0x12850cc0`, ~2 KB) removes strategies dominated on every cost axis; `StrategyShaverForProblem::FindShavedStrategies` (`0x12851a60`) — a Google-internal extension — removes strategies that cannot appear in any feasible solution; and `ReduceMemoryTerms` folds the peak-memory constraint into fewer variables. After solving, `Evaluate` (`0x128473c0`, ~7.7 KB) re-prices the chosen assignment for reporting.

The request crossing into the solver is the `AutoShardingSolverRequest` proto (parse table `AutoShardingSolverRequest::_table_` @ `0x218f17c0`, vtable `0x218f13e8`): nested `Nodes` (per-node strategy lists), `Edges` (endpoint pairs), `Costs`/`Coeff` (cost vectors and coefficient matrices), `Pair` ((i,j) entries), `Group` (shard-group constraints), `SolverTimeout`, and `Names` (debug). `solver_type` selects the backend (`SOLVER_TYPE_CP_SAT` is the default), and `solver_specific_parameters` carries a text-format `SatParameters` or `GlopParameters` proto.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `FormulateAndSolveMIPFromProblem` | `0x128407c0` | build + solve the MIP via OR-Tools `MPSolver` | CONFIRMED |
| `CreateAutoShardingSolverRequestAndCallSolver` | `0x127f24a0` | build request, dispatch | HIGH |
| `(anon)::SolveAndExtractSolution` | `0x2139b540` | call `MPSolver::Solve`, read variables | HIGH |
| `CheckDominance` | `0x12850cc0` | prune dominated strategies | HIGH |
| `Evaluate` | `0x128473c0` | post-solve re-pricing | HIGH |
| `operations_research::SatInterface::Solve` | `0x1285dce0` | CP-SAT integer backend | HIGH |
| `operations_research::GLOPInterface::Solve` | `0x12ef4760` | GLOP LP relaxation | HIGH |

> **QUIRK —** the request carries a `SolverTimeout` and the code emits three distinct failure strings: "could not find a valid solution within the given time limit. Please report this as a bug!", "could only find a non-optimal solution within the given time limit.", and the no-mesh-feasible message. A reimplementer must treat the CP-SAT solve as fallible and on timeout fall back (the `use_sharding_propagation_for_default_shardings` option computes a default via [propagation](sharding-propagation.md) when a node's cost is infinite).

---

## The Partitioner — `TpuSpmdPartitioner`

### Purpose

`xla::jellyfish::TpuSpmdPartitioner` ("tpu-spmd-partitioning", ctor `0x127a1e40`) is the consumer: given a module where every instruction has a sharding, it rewrites the global program into the SPMD program a single partition runs. It is a thin subclass of the open-source `xla::spmd::SpmdPartitioner`; the decompiled `RunImpl` (`0x127a2a80`) calls the base `SpmdPartitioner::RunImpl` (`0x1c7fe400`) and adds only TPU layout/accuracy fixups. Source path confirmed: `platforms/xla/service/jellyfish/spmd/tpu_spmd_partitioner.cc`.

### Entry Point

```text
TpuSpmdPartitioner::RunImpl   0x127a2a80 (4.8 KB)
  SpmdPartitioner::RunImpl    0x1c7fe400 (7.7 KB)            ── base driver
    PreprocessSharding        0x1c804ae0 (2.9 KB)            ── fill Replicated, validate
    PreprocessCallSites       0x1c800260 (5.1 KB)            ── flatten call/while/conditional
    PreprocessHlos            0x1c8016c0 (9.5 KB)            ── canonicalize for emission
    PartitionComputation      0x1c7fda60                     ── per-computation visitor walk
      SpmdPartitioningVisitor  ── ~53 Handle* methods (the rewrite)
    ConvertUnreducedSharding  0x1c803d00 (3.5 KB)            ── unreduced → AllReduce/ReduceScatter
    RecordInputsOutputsSharding  0x1c7fe0a0                  ── annotate alias config
```

### Algorithm

The driver is a fixed three-stage spine: canonicalize the graph, walk every computation through the visitor, then finalize.

```c
function TpuSpmdPartitioner::RunImpl(module, exec_threads):   // 0x127a2a80
    status = SpmdPartitioner::RunImpl(module, exec_threads)   // 0x1c7fe400 — base does the work
        PreprocessSharding(module)        // every op gets a sharding; Replicated default
        PreprocessCallSites(module)       // push shardings through kCall/kWhile/kConditional
        PreprocessHlos(module)            // unfold tuple Sharding annotations, etc.
        for comp in module.computations():
            visitor = CreateVisitor(comp, num_partitions, collective_ops_creator)  // 0x127a2520
            PartitionComputation(comp, visitor)   // dispatch each op to Handle<Op>
        ConvertUnreducedSharding(module)  // 0x1c803d00 — IsUnreduced outputs → collective
        RecordInputsOutputsSharding(module)
    return status                         // tpu_spmd_partitioner.cc source-loc on the error paths
```

### TPU Overrides

The TPU subclass overrides exactly four base methods; everything else, including the visitor, is the open-source base — TPU-specific behavior is funneled through the collective creator and the downstream rewrites instead of through subclassing.

| Override | Address | What it changes |
|---|---|---|
| `AllGatherShards` | `0x127a2880` | 37-byte (`0x25`) stub → forwards to base `AllGatherShardsInternal`, passing the trailing per-dim-communication bool through |
| `AllReduceAlongShardingDims` | `0x127a28c0` (0.43 KB) | F32-vs-BF16 accumulation choice via `MayIncreaseBF16AllReduceAccumulationAccuracy` (`0x127a22c0`) |
| `CreateVisitor` | `0x127a2520` | constructs the base `SpmdPartitioningVisitor` (no TPU visitor subclass) |
| `UpdateLayout` | `0x127a4100` (0.42 KB) | rewrites per-partition shapes to TPU-canonical (sublane/lane) layouts |

> **NOTE —** `AllReduceAlongShardingDims` is byte-confirmed with the signature `(SpmdBuilder*, HloInstruction*, const HloSharding&, int64_t*, absl::Span<const int64_t>, SPMDCollectiveOpsCreator)`. Its accuracy gate `MayIncreaseBF16AllReduceAccumulationAccuracy` is also confirmed, taking `ObjectView<TpuCompilationEnvironment>` and the creator; it queries `xla_tpu_spmd_f32_accum_for_bf16_ar` and the `_min_subgroup_size` companion flag to decide whether to upcast a BF16 reduction to F32 accumulation. Whether the decision is purely threshold-driven or also profile-driven was not resolved (LOW).

---

## Per-Op Rewrite — `SpmdPartitioningVisitor`

### Purpose

The visitor is where the global→per-partition rewrite happens, one HLO at a time. Each `Handle<Op>` takes the global instruction plus its operands' `PartitionedHlo` wrappers, computes the per-partition replacement, and inserts whatever collective makes that replacement correct. ~53 handlers were recovered. There is no TPU subclass of the visitor — the base handles all opcodes, and TPU specialization lives in the collective callbacks and the post-SPMD rewrites.

### Algorithm — the rewrite axes

Rather than 53 rows of opcode→handler, the handlers cluster into a small number of rewrite shapes. The reimplementation-relevant axis is *what the handler does about a sharding disagreement on the op's dimensions*:

| Rewrite shape | Representative handlers | What it emits |
|---|---|---|
| **Pass-through** (sharding already final) | `HandleElementwise`, `HandleBroadcast`, `HandleTranspose`, `HandleOptimizationBarrier`, `HandleCollectivePermute` | per-partition op, no collective |
| **Contracting-dim reduction** | `HandleDotHelper`, `HandleConvolution`, `HandleReduce` | per-partition compute + **AllReduce** on output |
| **Resharding before compute** | `HandleDotWithoutConflicts`, `HandleReshape`, `HandleSlice`/`HandleDynamicSlice` | reshard operand (AllGather / SliceValidData) then op |
| **Index-data split** | `HandleGather`, `HandleScatter`, `HandleSort` | gather indices (AllGather) / per-partition op + AllReduce / AllToAll |
| **Spatial halo** | `HandleConvolution` (spatial), `HandleReduceWindow`, `HandleSelectAndScatter` | **halo exchange** (CollectivePermute) + per-partition windowed op |
| **Recursive** | `HandleConditional`, `HandleWhile`, `HandleCall`, `HandleTuple` | partition each sub-region / element |
| **Custom-call dispatch** | `HandleCustomCall` (`0x1c716540`, 5.1 KB) | dispatch table over `custom_call_target` → a `_SPMDInternal_*` / TopK / partial-reduce handler |

The dot/conv handlers share one kernel, `PartitionDot<...>`, parameterized by a functor:

| Functor instantiation | Address | Used for |
|---|---|---|
| `HandleDotHelper<CreateShardedDotFunctor>` | `0x1c7191c0` | generic matmul |
| `HandleDotHelper<CreateShardedConvolutionFunctor>` | `0x1c7200e0` | convolution (verified: `HandleConvolution` calls this) |
| `HandleDotHelper<CreateShardedScaledDotFunctor>` | `0x1c71c420` | scaled-dot (NVFP4 / scaled-FP8; `PartitionedHloMX`) |

> **QUIRK —** `HandleConvolution` (`0x1c703120`) does not implement convolution partitioning itself — the decompiled body shows it calling `HandleDotHelper<CreateShardedConvolutionFunctor>`. Convolution is partitioned *as a dot*; only the spatial halo-exchange part (`PartitionConv` `0x1c76bea0`) is conv-specific. A reimplementation that writes a separate conv partitioner is duplicating the dot kernel.

### The PartitionedHlo abstraction

Each operand entering a handler is a `PartitionedHlo`: the per-partition HLO value plus its current sharding. A handler that needs a different sharding calls a reshard, which prices the change through `ClusterEnvironment::ReshardingCost` and emits the corresponding collective. The visitor-internal helpers that perform reshards: `ShuffleDataWithAllToAll` (`0x1c791340`, full-replication via per-rank AllToAll), `GetAllToAllSharding` (`0x1c7d8400`), `ClampGatherIndices` (`0x1c793b20`), and `GetPerGroupCollectiveOpsCreator` (`0x1c824140`, pushes a sub-creator for hierarchical resharding).

---

## Collective Insertion

### Purpose

The partitioner never constructs an `HloInstruction` collective directly — it calls through the `xla::spmd::SPMDCollectiveOpsCreator` callback table, so the same partitioner serves every backend. The TPU compiler installs the *default* creator; it adds no custom callbacks. All TPU-specific collective shaping (combining, async, F32 accumulation, cross-slice MegaScale) happens downstream in the [collective rewrites](../collectives/overview.md), not here.

### The creator struct

The decompiled `GetDefaultCollectiveOpsCreator` (`0x1c7fc120`) builds a struct of exactly **five** `std::function` fields, each a TU-private `$_N` closure, with `num_replicas`/`num_partitions` captured in. The field layout and signatures are byte-confirmed:

```c
struct SPMDCollectiveOpsCreator {                 // built at 0x1c7fc120
  // $_0  CollectivePermute — source-target pair list
  fn<HloInstruction*(SpmdBuilder*, HloInstruction* operand,
                     vector<pair<int64,int64>>& source_target_pairs,
                     int64 next_channel_id)>            create_cross_partition_collective_permute;
  // $_1  PartitionId — emits the kPartitionId constant for the visitor
  fn<HloInstruction*(SpmdBuilder*)>                     create_partition_id;
  // $_2  AllReduce — pure reduction, no group split
  fn<HloInstruction*(SpmdBuilder*, HloInstruction* operand,
                     HloComputation* reduction,
                     const CollectiveDeviceListBase& device_list,
                     int64 next_channel_id)>            create_cross_partition_all_reduce;
  // $_3  AllGather — concat per-partition slices along all_gather_dim
  fn<HloInstruction*(SpmdBuilder*, Span<HloInstruction* const> operands,
                     const CollectiveDeviceListBase& device_list,
                     int64 all_gather_dim,
                     optional<int64> next_channel_id)>   create_cross_partition_all_gather;
  // $_4  AllToAll — operand split / output gather
  fn<HloInstruction*(SpmdBuilder*, HloInstruction* operand,
                     const Shape& output_shape,
                     const CollectiveDeviceListBase& device_list,
                     int64 split_dim, int64 concat_dim)> create_cross_partition_all_to_all;
};
```

> **GOTCHA —** there is **no ReduceScatter callback** in the 5-field struct (verified — only `$_0`..`$_4` are written). ReduceScatter is *not* emitted at visitor time. It is materialized later, either as "AllReduce then DynamicSlice" or by the dedicated downstream `TpuAllReduceScatterFusion` pass (see [ReduceScatter](../collectives/reduce-scatter.md)). A reimplementer who adds a sixth callback diverges from the binary; the TPU partitioner relies on the fusion pass to recover RS from AR+DS.

### Per-opcode insertion rules

The decision of which collective an op needs is not in the creator — it is in the strategy generators (for the auto path) and the visitor handlers (for the rewrite). The recovered rule set:

| Sharded HLO pattern | Inserted collective | Emitted by |
|---|---|---|
| `dot` / `conv` with sharded contracting dim | AllReduce on output | `PartitionDot` + `HandleAllReduce` |
| `dot` with sharded batch, replicated contracting | none (already per-partition) | `HandleDotWithoutConflicts` |
| `dot` shard-by-output-only | AllGather on sharded operand | `HandleDotHelper` |
| `dot` windowed-einsum (AG) | per-partition Dot + intra-loop CollectivePermute | `AppendAllGatherWindowedEinsumStrategyForOperand` |
| `dot` windowed-einsum (RS) | intra-loop AllReduce → DynamicSlice | `AppendReduceScatterWindowedEinsumStrategy` |
| `reduce` along sharded dim | AllReduce | `HandleReduce` |
| `convolution` sharded across spatial dims | halo exchange (CollectivePermute) + per-part conv | `PadEachPartitionWithHaloExchange` |
| `reduce-window` sharded spatial | halo exchange + per-partition reduce-window | `HandleReduceWindow` + `ExchangeHaloAndGetValidData` |
| `slice` / `dynamic-slice` across partition | `SliceValidData` + AllGather | `HandleSlice`, `HandleDynamicSlice` |
| `gather` with sharded indices | AllGather indices, then per-partition gather | `PartitionGather` |
| `scatter` with sharded indices | per-partition scatter + AllReduce | `PartitionScatter` |
| `concat` along sharded dim | replicate + concat (TPU rewrite) | `TpuSpmdConcatRewriter` |
| `sort` on sharded dim | per-partition sort + AllToAll | `HandleSort` |
| `unreduced` output | AllReduce or ReduceScatter (consumer-dependent) | `ConvertUnreducedSharding` |
| `partial-reduce` (TPU custom-call) | per-partition partial-reduce + AllReduce | partial-reduce visitor |
| FFT across partition | per-partition FFT + CollectivePermute | `GetFinalFftUsingCollectivePermute` |

### Halo Exchange

Windowed ops (convolution, reduce-window, select-and-scatter) need each partition to see a *halo* of the neighbouring partitions' boundary data. The `ExchangeHalo` family implements this with sliding-window CollectivePermutes. `ExchangeHaloAndGetValidData` (`0x1c825660`) is the entry: its byte-confirmed signature takes the operand, its `Shape`, two `OffsetCalculation`s (the window bounds), four `int64` halo sizes, the `HloSharding`, padding values, and the `SPMDCollectiveOpsCreator`. The family:

| Helper | Address | Role |
|---|---|---|
| `ExchangeHaloAndGetValidData` | `0x1c825660` | full halo exchange + valid-data mask (the entry) |
| `ExchangeHalo` | `0x1c822340` | core CollectivePermute-based boundary swap |
| `ExchangeHaloCompact` | `0x1c81d3e0` | compacted variant (fewer permutes) |
| `PadEachPartitionWithHaloExchange` | `0x1c790640` | pad each partition with its neighbours' edge |
| `TileToPartialReplicateHaloExchange` | `0x1c81ccc0` | halo when transitioning tile → partial-replicate |
| `GetFinalFftUsingCollectivePermute` | `0x1c791980` | FFT-specific sliding window |

> **NOTE —** the halo width is computed from the window's dilation/stride/padding, not from the sharding alone — `ExchangeHaloAndGetValidData` takes `OffsetCalculation` arguments precisely so the per-partition valid region can be masked after the exchange. A reimplementer that exchanges a fixed-width halo will read garbage at the array edges; the valid-data mask is mandatory.

---

## TPU-Specific Extensions

Beyond the standard GSPMD primitives, the TPU partitioner adds several sharding-aware constructs. None of these introduce a new collective callback — they shape *where* the standard collectives go.

- **Windowed-einsum.** A matmul where one operand is too large to AllGather up-front is partitioned as a `while` loop that overlaps compute with communication. The AG variant Dots a per-partition LHS slice each iteration, then CollectivePermute-shifts the slice by one partition; output ends up full-replicated. The RS variant accumulates a per-partition output slice via partial AllReduce each iteration. Both are chosen at strategy time (the `ag_/rs_windowed_einsum_*` strategies) and gated by `xla_tpu_enable_windowed_einsum_for_all_gather` / `_for_reduce_scatter`, with `xla_tpu_spmd_unroll_windowed_einsum`, `_bidirectional_windowed_einsum`, and the `xla_jf_spmd_threshold_for_windowed_einsum_mib` size threshold controlling the loop shape. `WindowedEinsumLoopConfig` records the chosen config.

- **Scaled-dot (`CreateShardedScaledDotFunctor`).** For NVFP4 / scaled-FP8 matmuls the partitioner treats the `(operand, scale)` pair as a tagged tuple via `PartitionedHloMX`, co-sharding the scale tensor with its operand so a reshard moves both together.

- **`MultiPad` / `MultiSlice` / `MultiRotate` / `RotateRight`.** TPU `xla.spmd_internal.*` custom-calls for batched per-partition manipulation, each with its own `HandleCustomCallSPMDInternal_*` (`0x1c70e7e0`..`0x1c715b60`). `RotateRight` is a right-cyclic shift across partitions, implemented over CollectivePermute.

- **`partial_reduce_handler::kPartialReduce`.** The only TPU custom-call with non-trivial sharding propagation; it registers its own `SpmdPartitioningVisitor` and emits a `Sort+TopK` skeleton governed by `kReductionDimKey`, `kLog2ReductionKey`, `kRecallTargetKey`.

- **Hierarchical SparseCore partitioning.** For embedding-heavy models the entry computation is partitioned at two granularities — TensorCore and SparseCore. `SparseCoreHierarchicalSpmdPartitioner` (`RunImpl` `0x13c7ee20`, ~10.6 KB) pads SC inputs (`PadSparseCoreProgramInputs`), unpads outputs, and explicitly partitions the SC entry computation; the inner `SparseCoreSpmdPartitioner` (ctor `0x13c818a0`) and `SparseCorePartitioningVisitor` override `HandleSort`/`HandleScatter`/`HandleAllToAll` and add `PartitionSharedMemoryParallelScatter`. Source: `platforms/xla/sparse_core/hlo/sparse_core_spmd_partitioning.cc`.

- **Shard-barriers & custom-call helpers.** `ShardBarrierFromPartitioner` / `ShardBarrierToPartitioner`, `TpuLogCustomCallPartitioner` (`_xla_log` debug), and the megascale `MetadataCustomCallPartitioner` are `CustomCallShardingHelper` subclasses; they freeze or pass through sharding for specific custom-call targets (consulted during propagation — see [Custom-Call Lowering](custom-call-lowering.md)).

---

## Flag Catalog

Sharding/SPMD/collective flags read through `TpuCompilationEnvironment` (recovered from the `AbslFlag*Gen*` symbol family; defaults from `AbslFlagDefaultGenFor*`). The entry gates and partitioner-relevant subset:

| Flag | Type | Default | Controls |
|---|---|---|---|
| `xla_tpu_spmd_auto_partitioning` | bool | **false** | enter the auto-sharding (ILP) path |
| `xla_tpu_spmd_auto_partitioning_search_mesh_shapes` | bool | **false** | `try_multiple_mesh_shapes` |
| `xla_tpu_spmd_run_partition_assignment` | bool | **false** | run `TpuPartitionAssignment` |
| `xla_tpu_spmd_skip_partitioning` | bool | **false** | skip SPMD entirely (debug) |
| `xla_tpu_spmd_decompose_sharded_concats` | bool | **true** | `TpuSpmdConcatRewriter` |
| `xla_tpu_spmd_f32_accum_for_bf16_ar` | bool | — | F32 accumulation for BF16 AllReduce |
| `xla_tpu_spmd_f32_accum_for_bf16_ar_min_subgroup_size` | int64 | — | minimum subgroup for the above |
| `xla_tpu_enable_windowed_einsum_for_all_gather` | bool | — | allow AG windowed-einsum |
| `xla_tpu_enable_windowed_einsum_for_reduce_scatter` | bool | **false** | allow RS windowed-einsum |
| `xla_tpu_spmd_unroll_windowed_einsum` | bool | **false** | unroll the WE loop |
| `xla_tpu_spmd_bidirectional_windowed_einsum` | bool | **false** | forward+backward shift schedule |
| `xla_jf_spmd_threshold_for_windowed_einsum_mib` | int64 | — | size threshold (MiB) to enable WE |
| `xla_tpu_auto_spmd_partitioning_memory_budget_gb` | int64 | — | `memory_budget_per_device` |
| `xla_tpu_auto_spmd_partitioning_memory_budget_ratio` | double | — | `memory_budget_ratio` |
| `xla_use_shardy` | bool | **false** | use the Shardy producer instead of GSPMD |

> **NOTE —** dashes mark defaults not pinned from `AbslFlagDefaultGenFor*` in this binary; the gating booleans default off, so an out-of-the-box compile uses *propagation*, not the ILP. A reimplementer should treat auto-sharding as an opt-in path.

---

## What Was Not Resolved

- The exact `AutoShardingOption` C++ field offsets (names recovered; layout requires walking `CheckAndSetup` at `0x12e0ce00`). LOW.
- The exact `MPSolver::OptimizationProblemType` enum value the solver is constructed with — inferred CP-SAT from `SatInterface` linkage and the `auto_sharding_cpsat_for_problem.cc` source path, not read off the constructor argument. MEDIUM.
- The per-TpuVersion default `device_mesh_alpha` / `device_mesh_beta` values. LOW.
- Whether `MayIncreaseBF16AllReduceAccumulationAccuracy` is threshold-only or also profile-driven. LOW.
- `TpuExp0PartitioningAlgorithm` (the only registered `PartitioningAlgorithm`, `Run` at `0x1278eea0`, 0xd1 bytes) delegates to a `$_0` lambda; what experimental heuristic it implements was not traced. It is gated off by default. LOW.

---

## Related Components

| Component | Relationship |
|---|---|
| `ShardingPropagation` | the GSPMD producer; infers shardings the partitioner consumes |
| `ShardyXLA` | the Shardy (JAX-native) producer; alternative to GSPMD |
| `TpuPartitionAssignment` | gated pre-pass that can pick a partitioning algorithm before the partitioner |
| `TpuSpmdConcatRewriter` | TPU pre-pass that decomposes sharded concatenations |
| `TpuAllReduceScatterFusion` (and the collective rewrites) | downstream; recover ReduceScatter and shape all TPU-specific collectives |

## Cross-References

- [Sharding Propagation](sharding-propagation.md) — the inference rules, fixed-point loop, and custom-call sharding helpers; this page's sibling producer
- [The TPU Compiler](overview.md) — where the partitioning pipeline sits in `RunHloPasses`
- [Compile Phases](compile-phases.md) — the five-phase spine that hosts `AddTpuPartitioningPasses`
- [Custom-Call Lowering](custom-call-lowering.md) — the `partial_reduce_handler`, Mosaic, and shard-barrier custom-call surfaces
- [RaggedDot and Convolution Geometry Lowering](raggeddot-convolution.md) — how the per-partition dot/conv the partitioner emits is lowered further
- [Collectives Overview](../collectives/overview.md) — the post-SPMD collective rewrites that shape AllReduce/AllGather/AllToAll for the TPU mesh
- [ReduceScatter](../collectives/reduce-scatter.md) — where ReduceScatter is materialized, since the partitioner emits none directly
