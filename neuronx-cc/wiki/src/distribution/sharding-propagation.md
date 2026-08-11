# ShardingPropagation Engine

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22, front-end binary `neuronxcc/starfish/bin/hlo-opt` (cp310 wheel). The cp311/cp312 wheels carry the same symbols at the same VAs. Other versions will differ.*

## Abstract

`ShardingPropagation` is the pass that turns a sparse set of user-annotated tensor
shardings into a complete sharding assignment across every HLO instruction in a
module, *before* the SPMD partitioner rewrites the graph into per-device shapes.
A reader who knows XLA's GSPMD will recognize this immediately: it is the classic
forward/backward sharding-inference fixpoint from `xla/service/sharding_propagation.cc`,
and in this binary it is **byte-identical stock OpenXLA** — there is not one Neuron
opcode, attribute, or branch anywhere in the 33 `ShardingPropagation` symbols or
their direct callees. Neuron's customization of SPMD lives *downstream*, in the
`SpmdPartitioner` that consumes this pass's output (see the
[SPMD partitioner driver](spmd-partitioner-driver.md)). The file literal
`"xla/service/sharding_propagation.cc"` and the full set of debug/CHECK strings are
the same byte sequences upstream XLA emits.

The mechanism is a bounded `while(changed)` loop. Each turn sweeps every computation
twice — once in post-order propagating each instruction's sharding *from its operands*
(forward), once in reverse post-order propagating *from its users* (backward) — plus a
third sweep that aligns explicit shard-groups. An instruction's sharding is only ever
*refined* (made strictly more specific) or newly *added*; it never regresses, which is
what makes the fixpoint monotone and guarantees termination. The whole loop is driven
at increasing `aggressiveness` levels so that cheap, unambiguous inferences win before
risky heuristic ones.

This page documents the driver (`Run`), the fixpoint loop (`RunToFixPoint`, which the
compiler fused with upstream's `ShardingPropagationIteration`), the two per-op
inference engines and their opcode switches (101 forward cases, 100 backward cases),
the seeding pass that parses `Sharding` custom-calls into the initial annotations, the
two `vector<bool>` masks that gate parameter/output seeding, and the aggressiveness
gate. The sharding *algebra* itself — `HloSharding`, tile assignments, `MergeSharding`,
`OpShardingRule` — is owned by [Sharding Algebra](sharding-algebra.md); this page treats
it as a called surface.

For reimplementation, the contract is:

- The monotone fixpoint: forward (operands→result) + backward (users→operand) +
  shard-group sweeps per iteration, converging when a whole sweep adds nothing.
- The two opcode-dispatch switches: ~20 op-specific handlers, ~80 elementwise
  pass-through, and the merge/accept rule (`IsShardingMoreSpecific` + `MergeSharding`).
- Seeding: how `Sharding` / `SPMDFullToShardShape` / `SPMDShardToFullShape` /
  `ShardBarrier*` custom-calls and the two `allow_spmd_..._to_{parameters,output}`
  masks bootstrap and bound the propagation.
- The `aggressiveness` ladder and the two orthogonal admission gates
  (`CanPropagateThroughAtAggressiveLevel`, `SupportSpatialPartitioning`).

| | |
|---|---|
| **Pass name** | `"sharding-propagation"` (`name()` @ `0x2b0fb30`) |
| **Driver** | `ShardingPropagation::Run` @ `0x2b2a140` (874 bb, 106 callees, 168 try-blocks) |
| **Fixpoint loop** | `RunToFixPoint` @ `0x2b24ec0` (360 bb, 37 callees) |
| **Forward infer** | `InferShardingFromOperands` @ `0x2b220f0` — switch @ `0x2b22241`, **101 cases** |
| **Backward infer** | `GetShardingFromUser` @ `0x2b1d200` — switch @ `0x2b1d2bd`, **100 cases** |
| **Seeding** | `ProcessShardingInstruction` @ `0x2b27ff0` |
| **Constructed at** | `xla::cpu::CpuCompiler::RunHloPassesThroughLayoutAssn` (sole caller of ctor) |
| **IR level** | HLO (stable HLO / `HloModule`), pre-partition |
| **Provenance** | **stock OpenXLA** — `xla/service/sharding_propagation.cc`, zero Neuron deltas |

> **NOTE —** the IDA export for `hlo-opt` was produced with decompilation skipped, so
> the `decompiled/*.c` files are stubs. Every claim below is grounded in the
> per-function `context/*.md` sidecars (demangled signatures, callee tables with
> sizes/bb-counts, caller lists, referenced strings, and the extracted switch jump
> tables) and in `function_addresses.json`/`callgraph.json` — not statement-level
> decompilation. Where a claim rests on upstream-XLA knowledge filling a gap the local
> evidence is merely consistent with, it is marked **[INFERRED]** at the point of use.

---

## Run — the Driver

### Purpose

`Run` is the pass entry point. It seeds the module with the user's explicit shardings,
builds the call graph, runs the fixpoint loop, aligns shard-groups, inserts and then
folds CSE-prevention copies, canonicalizes the entry layout against the propagated
sharding, and strips the bookkeeping metadata. It returns `true` if any sharding was
added or refined.

### Entry Point

```text
ShardingPropagation::Run (0x2b2a140)            ── driver, StatusOr<bool>
  ├─ AssignShardingMetadata                     ── stamp provenance (if propagate_metadata_)
  ├─ CallGraph::Build                           ── walk while/cond/call boundaries
  ├─ ProcessShardingInstruction (0x2b27ff0)     ── SEED: parse Sharding custom-calls
  ├─ FindCommonSharding / MergeShardingIfCompatible ── shard-group alignment
  ├─ RunToFixPoint (0x2b24ec0)                   ── fwd/bwd/shard-group fixpoint
  ├─ GetRelatedInstructions (0x2b147d0)         ── computation-boundary tie-up
  ├─ MaybeComputationPropagation (0x2b156a0)    ── push sharding across boundaries
  ├─ HloInstruction::CreateUnary(kCopy)         ── CSE-prevention copies
  ├─ CanonicalizeLayoutAfterShardingPropagation (0x90ebee0)
  └─ RemoveShardingMetadata                     ── inverse of AssignShardingMetadata
```

### Signature

```c
// demangled @ 0x2b2a140
StatusOr<bool> ShardingPropagation::Run(
    HloModule* module,
    const flat_hash_set<string_view>& execution_threads);
```

### Algorithm

```c
// ShardingPropagation::Run — 0x2b2a140 (874 bb / 106 callees / 168 try-blocks)
function Run(module, execution_threads):

    // (1) optional provenance stamping — ctor flag propagate_metadata_
    if propagate_metadata_:
        AssignShardingMetadata(module, execution_threads)   // stock XLA callee

    // (2) call graph: needed to cross while/conditional/call boundaries
    call_graph = CallGraph::Build(module, execution_threads) // stock XLA callee

    // (3) SEEDING: canonicalize user Sharding custom-calls onto operands,
    //     record ShardBarrierFrom/To, build Shard-As / Shard-Like groups,
    //     and save the user-annotated entry-param / root shardings.
    ProcessShardingInstruction(module, execution_threads,    // @ 0x2b27ff0
        &unspecified_dims, &saved_root_shardings, &saved_param_shardings,
        &instruction_to_shard_group_id,
        &shard_group_id_to_shard_as_group,
        &shard_group_id_to_shard_like_group,
        allow_spmd_sharding_propagation_to_parameters_,      // ctor vector<bool> mask
        allow_spmd_sharding_propagation_to_output_)          // ctor vector<bool> mask

    // (4) align each explicit shard-group to one representative sharding
    for group in shard_groups:                               // "Aligning shard group: " (verbatim)
        rep = hlo_sharding_util::FindCommonSharding(group)   // stock XLA callee
        for member in group:
            MergeShardingIfCompatible(rep, &member.sharding) // stock XLA callee

    // (5) the fixpoint — see RunToFixPoint. Run drives the aggressiveness ladder
    //     and re-invokes after CSE-prevention copies are inserted.
    int64 iterations = 0;
    for aggressiveness in 0..3:                              // arg is an int64; [INFERRED] ladder span
        RunToFixPoint(aggressiveness, propagate_metadata_, computation_map,
                      provided_shardings, *call_graph, module, execution_threads,
                      unspecified_dims, instruction_to_shard_group_id,
                      shard_group_id_to_group, /*...*/, &iterations) // @ 0x2b24ec0

    // (6) tie shardings across computation in/out boundaries once local fixpoint settles
    GetRelatedInstructions(inst, computation_map)            // @ 0x2b147d0
    MaybeComputationPropagation(computation_map, ..., &changed_set) // @ 0x2b156a0

    // (7) CSE-prevention: tag divergently-sharded values with kCopy so the later
    //     CSE pass will not collapse them; folded back after the fixpoint re-converges.
    copy = HloInstruction::CreateUnary(shape, kCopy, value)  // stock XLA callee
    copy.set_name(value.name() + "_sharding_propagation_cse_prevention") // verbatim string
    // ... rerun fixpoint so the new copies get sharded ...
    HloComputation::ReplaceInstruction(copy, value)          // stock XLA callee (fold back)

    // (8) force entry params / root tuple layout to agree with propagated sharding,
    //     but ONLY for the elements whose mask bit is set.
    hlo_sharding_util::CanonicalizeLayoutAfterShardingPropagation(  // @ 0x90ebee0
        module, allow_spmd_sharding_propagation_to_parameters_,
                allow_spmd_sharding_propagation_to_output_)

    // (9) strip bookkeeping metadata if it was added in (1)
    if propagate_metadata_:
        RemoveShardingMetadata(module, execution_threads)    // stock XLA callee

    // (10) "Sharding propagation completed after " << iterations << " iterations"  (verbatim)
    return changed   // any sharding added or refined
```

> **QUIRK —** `RunToFixPoint` is called up to **twice** from `Run`: once for the real
> propagation, and again after the CSE-prevention `kCopy` instructions are inserted, so
> that the freshly-introduced copies themselves receive a sharding before they are
> folded away. A reimplementation that runs the fixpoint only once will leave the
> CSE-prevention copies unsharded and either crash the partitioner or silently lose the
> divergent-sharding guarantee.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ShardingPropagation::Run` | `0x2b2a140` | Driver | CERTAIN |
| `ShardingPropagation::name` | `0x2b0fb30` | Returns `"sharding-propagation"` | CERTAIN |
| `ShardingPropagation` ctor | `0x2038370` | Captures flags + masks + helper | CERTAIN |
| `ProcessShardingInstruction` | `0x2b27ff0` | Seeding | CERTAIN |
| `GetRelatedInstructions` | `0x2b147d0` | Computation-boundary counterpart lookup | CERTAIN |
| `MaybeComputationPropagation` | `0x2b156a0` | Push sharding across one boundary | CERTAIN |
| `CanonicalizeLayoutAfterShardingPropagation` | `0x90ebee0` | Entry-layout canonicalization | CERTAIN |

### The constructor and its masks

```c
// demangled @ 0x2038370
ShardingPropagation::ShardingPropagation(
    bool is_spmd,
    bool propagate_metadata,
    Span<const bool> allow_spmd_sharding_propagation_to_output,
    Span<const bool> allow_spmd_sharding_propagation_to_parameters,
    bool cse_prevention_only,
    unique_ptr<CustomCallShardingHelper> sharding_helper);
```

The two `Span<const bool>` arguments are the per-element **output** and **parameter**
propagation masks consumed in step (8): for an element whose mask bit is `false`,
propagation *from the body into that entry parameter / root element* is forbidden — the
user's annotation on it is authoritative and frozen. `is_spmd` selects the SPMD vs
non-SPMD inference rules. `cse_prevention_only` selects a degenerate mode where `Run`
inserts the tagged copies but does *not* run the full ladder — used as a late mini-pass
purely to stop CSE from collapsing divergently-sharded values. The strings
`"allow_spmd_sharding_propagation_to_parameters"` and
`"allow_spmd_sharding_propagation_to_output"` are present in the binary.

> **NOTE —** the ctor at `0x2038370` has exactly **one** caller:
> `xla::cpu::CpuCompiler::RunHloPassesThroughLayoutAssn`. That same compiler
> stage also constructs `xla::spmd::SpmdPartitioner`, which binary-grounds the pipeline
> ordering "ShardingPropagation runs first, then SpmdPartitioner consumes its output."
> The `is_spmd=true` value is set at that call site; because it is a bool argument rather
> than a string, that value is **[INFERRED]** from the `SpmdPartitioner` co-construction
> rather than read off a literal. The linkage through the *CPU* compiler is an artifact
> of how Neuron reuses XLA's host-compiler scaffolding; the partitioner that follows is
> where Neuron customization lives.

---

## RunToFixPoint — the Fixpoint Loop

### Purpose

`RunToFixPoint` is the bounded `while(changed)` loop. In upstream XLA this is two source
functions — the outer loop and `ShardingPropagationIteration` — but the compiler inlined
the iteration body, so the single binary symbol at `0x2b24ec0` realizes both. Each turn
sweeps every computation forward, then backward, then over shard-groups, counting how
many shardings it inferred from each source, and stops when an entire sweep adds nothing.

### Signature

```c
// demangled @ 0x2b24ec0 (maps simplified)
void ShardingPropagation::RunToFixPoint(
    int64 aggressiveness,
    bool propagate_metadata,
    const flat_hash_map<HloComputation*, HloInstruction*>& computation_map,
    const flat_hash_set<const HloInstruction*>& provided_shardings,
    const CallGraph& call_graph,
    HloModule* module,
    const flat_hash_set<string_view>& execution_threads,
    flat_hash_map<HloInstruction*, vector<int64>>& unspecified_dims,
    flat_hash_map<HloInstruction*, int64>& instruction_to_shard_group_id,
    flat_hash_map<int64, set<HloInstruction*>>& shard_group_id_to_group,
    /* ... */,
    int64& iterations);
```

### Algorithm

```c
// ShardingPropagation::RunToFixPoint — 0x2b24ec0 (360 bb / 37 callees / 51 try-blocks)
// Called ONLY by Run.  Inlined ShardingPropagationIteration body marked below.
function RunToFixPoint(aggressiveness, ..., &iterations):
    bool changed = true;
    while (changed):
        changed = false;
        ++iterations;
        VLOG("Sharding propagation iteration " << iterations)          // verbatim string

        int64 from_operands = 0, from_users = 0, from_shard_group = 0;

        // ---- ShardingPropagationIteration (inlined) ----
        for comp in module->computations(execution_threads):           // stock XLA callee
            VLOG("Consider computation: " << comp->name())             // verbatim string

            // FORWARD: operand -> result, in post-order
            for inst in comp->MakeInstructionPostOrder():              // stock XLA callee
                if InferShardingFromOperands(inst, computation_map,
                        aggressiveness, call_graph, execution_threads): // @ 0x2b220f0
                    // "Add sharding (forward-pass): " / "Refined partial sharding (forward-pass): "
                    changed = true; ++from_operands

            // BACKWARD: users -> operand, in REVERSE post-order
            for inst in reverse(comp->MakeInstructionPostOrder()):
                if InferShardingFromUsers(inst, computation_map, aggressiveness,
                        is_spmd_, sharding_helper_.get(), call_graph):  // @ 0x2b1ff00
                    // "Add sharding (backward-pass): " / "Refined partial sharding (backward-pass): "
                    changed = true; ++from_users

            // SHARD-GROUP cross-flow (Shard-As / Shard-Like)
            for inst in shard_group_members(comp):
                group = shard_group_id_to_group[instruction_to_shard_group_id[inst]];
                if InferShardingFromShardGroup(inst, aggressiveness, group): // @ 0x2b18380
                    // "Add sharding (shard group): " / "Refined partial sharding (shard group): "
                    changed = true; ++from_shard_group
        // ---- end iteration ----

        VLOG("\n  total instructions: " << N                          // all six fragments
          << "\n  instructions already sharded: " << k                //   appear as string
          << "\n  shardings inferred from operands: " << from_operands //   literals in this
          << "\n  shardings inferred from users: " << from_users      //   function
          << "\n  shardings inferred from shard group: " << from_shard_group
          << "\n  aggressiveness: " << aggressiveness)
    // converges when a full forward+backward+shard-group sweep adds nothing
```

### Unspecified-dim handling

Manual-sharding boundaries leave some dims open (the `"?"` dims). Inside the iteration,
two helpers reconcile them:

```c
InferUnspecifiedDimsFromOperand(inst, unspecified_dims, &operand)
InferUnspecifiedDimsFromUsers(inst, unspecified_dims, aggressiveness,
                              is_spmd, &user, call_graph)
```

These handle the partially-specified shardings produced by the
`SPMDFullToShardShape` / `SPMDShardToFullShape` manual-sharding boundary custom-calls
(both strings appear in this function) and the `ShardBarrierFrom` / `ShardBarrierTo`
barriers. An unspecified dim is never resolved by inference; it stays
open until the partitioner sees the explicit boundary.

### Merge primitives

The forward/backward engines do not write a sharding directly; they hand a candidate to
the merge layer, which accepts it only if it is strictly more specific than what the
instruction already carries:

```c
hlo_sharding_util::MergeSharding(new_sharding, &existing, may_combine_partial_with_partial)
hlo_sharding_util::MergeShardingIfCompatible(new_sharding, &existing)
hlo_sharding_util::IsShardingMoreSpecific(candidate, current)        // the accept test
hlo_sharding_util::IsSpatiallyPartitioned(sharding)
hlo_sharding_util::PartiallyReplicateTiledShardingOnAllDimsExcept(...)
```

The `"Refined partial sharding"` log path is taken when `MergeShardingIfCompatible`
returns a strictly more-defined-but-compatible sharding (a partial merge); the
`"Add sharding"` path is taken when the instruction had none and now gets one.

> **GOTCHA —** the accept rule is *monotone refinement*, not overwrite. A reimplementation
> that lets a later iteration replace an instruction's sharding with a *different but
> equally specific* one will never converge — the loop will oscillate. The `changed`
> flag must be set only when a sharding is genuinely added or made strictly more
> specific, which is exactly what `IsShardingMoreSpecific` gates.

---

## InferShardingFromOperands — Forward Inference

### Purpose

The forward engine computes a candidate sharding for `instruction` from the shardings of
its *operands* and merges it in. Most opcodes are shape-preserving and simply copy the
most-specific operand sharding; a minority of structurally interesting opcodes (dot,
conv, gather, scatter, reduce, reshape, transpose, …) have dedicated handlers that call
op-specific utilities in `hlo_sharding_util` / `dot_as_convolution_util`.

### Signature

```c
// demangled @ 0x2b220f0 (527 bb / 102 callees / 80 try-blocks)
bool ShardingPropagation::InferShardingFromOperands(
    HloInstruction* instruction,
    const flat_hash_map<HloComputation*, HloInstruction*>& computation_map,
    int64 aggressiveness,
    const CallGraph& call_graph,
    const flat_hash_set<string_view>& execution_threads);
```

### Algorithm

```c
// InferShardingFromOperands — 0x2b220f0.  Called ONLY by RunToFixPoint.
function InferShardingFromOperands(instruction, computation_map, aggressiveness, ...):

    // PRIORITY GATE: at low aggressiveness only "safe" ops may receive a forward sharding
    if !CanPropagateThroughAtAggressiveLevel(*instruction, aggressiveness): // @ 0x2b0fc40
        return false

    HloSharding candidate;
    switch (instruction->opcode()):    // 101-case switch @ 0x2b22241, default 0x2b22580

      // DEFAULT (0x2b22580): ~80 elementwise / shape-preserving opcodes share this target.
      // Copy the most-specific operand sharding straight through.
      default:
          candidate = MostSpecificOperandSharding(instruction)   // IsShardingMoreSpecific picks
          break

      // ~20 dedicated handlers (each op identified by the op-specific util it calls):
      case kDot:        // also kConvolution via the conv-as-dot view
          dims = dot_as_convolution_util::ParseDotGeneralFromDot(instruction)
          candidate = hlo_sharding_util::InferDotShardingFromOperands(
                          instruction, call_graph, dims, may_combine_partial, ...)
          // contracting dims -> partial/replicated; batch+non-contracting -> tiled
      case kGather:
          batch = GetGatherParallelBatchDims(instruction, call_graph)
          candidate = InferGatherParallelShardingFromOperands(instruction, dims, ...)
                   || GatherOutputShardingFromIndexIndexPassthroughDimensions(...)
      case kScatter:
          batch = GetScatterParallelBatchDims(instruction, call_graph)
          candidate = InferScatterParallelShardingFromOperands(...)
                   || ScatterOutputShardingFromUpdate(update_sharding, scatter)
      case kReduce:     // + variadic reduce
          candidate = PartiallyReplicateTiledShardingOnDims(operand_sharding, reduced_dims)
      case kReduceWindow:
          // bails on window dilation != 1:
          // "Not applying sharding to reduce window because dilatation isn't supported yet: "
      case kSelectAndScatter:
          // "Not applying sharding to select-and-scatter because base dilation isn't supported yet: "
      case kTranspose:  candidate = TransposeSharding(operand_sharding, permutation)
      case kReshape:    candidate = PropagateShardingThroughReshape(in_shape, out_shape, operand_sharding)
      case kReverse:    candidate = ReverseSharding(operand_sharding, dims)
      case kPad: case kSlice: case kDynamicSlice:
      case kDynamicUpdateSlice: case kBroadcast:
          // tile-assignment reshaping (TileAssignment::Reshape) + ReplicateAllDataDims
          // for added/dropped dims

    // MERGE: accept only if strictly more specific than the existing sharding
    if MergeSharding(candidate, &instruction->sharding, may_combine_partial)   // stock XLA callee
       && IsShardingMoreSpecific(candidate, old):                              // stock XLA callee
        instruction->set_sharding(candidate)                                   // stock XLA callee
        return true
    return false
```

> **QUIRK —** IDA labels the switch by integer case, not by opcode name; the
> opcode→case-number mapping is **[INFERRED]** from the op-specific utility each branch
> calls — each `case kDot`/`case kGather`/… above is identified by its callee, not by a
> labelled constant. The **count** of 101 cases, the jump table at `0x486540`, and the
> default target `0x2b22580` come directly from the extracted jump
> table (`jmp ds:jpt_2B22241[rax*8]`; bounds `sub eax,0x15; cmp al,0x64; ja default`).
> The bulk of those
> 101 cases collapse onto the single default (elementwise) target — only ~20 are
> distinct handlers. A reimplementation that builds 101 distinct per-opcode handlers is
> doing ~80× too much work; the right shape is "one passthrough handler + ~20 special
> cases."

### Confirmed op-specific callees

| Opcode | Forward utility (callee) |
|---|---|
| `kDot`, `kConvolution` | `InferDotShardingFromOperands` + `ParseDotGeneralFromDot` |
| `kGather` | `GetGatherParallelBatchDims`, `InferGatherParallelShardingFromOperands`, `GatherOutputShardingFrom*` |
| `kScatter` | `GetScatterParallelBatchDims`, `InferScatterParallelShardingFromOperands`, `ScatterOutputShardingFromUpdate` |
| `kReduce` | `PartiallyReplicateTiledShardingOnDims` over reduced dims |
| `kReduceWindow`, `kSelectAndScatter` | bail on dilation `!= 1` (the guard strings above) |
| `kTranspose` | `TransposeSharding(perm)` |
| `kReshape` | `PropagateShardingThroughReshape` |
| `kReverse` | `ReverseSharding(dims)` |
| `kPad`/`kSlice`/`kDynamic*Slice`/`kBroadcast` | `TileAssignment::Reshape` + `ReplicateAllDataDims` |
| ~80 elementwise | default `0x2b22580` — most-specific operand passthrough |

---

## InferShardingFromUsers + GetShardingFromUser — Backward Inference

### Purpose

Backward inference is two-tiered. `InferShardingFromUsers` is the **aggregator**: it
walks every user of `instruction`, asks the per-(op,user) oracle `GetShardingFromUser`
for the sharding each user implies, and merges the results. `GetShardingFromUser` is the
**oracle**: a 100-case switch over the *user's* opcode that back-propagates the user's
sharding onto the operand we are inferring.

### Signatures

```c
// demangled @ 0x2b1ff00 (101 bb / 17 callees) — the AGGREGATOR
bool ShardingPropagation::InferShardingFromUsers(
    HloInstruction* instruction,
    const flat_hash_map<HloComputation*, HloInstruction*>& computation_map,
    int64 aggressiveness, bool is_spmd,
    const CustomCallShardingHelper* sharding_helper,
    const CallGraph& call_graph);

// demangled @ 0x2b1d200 (442 bb / 88 callees) — the per-(op,user) ORACLE
HloSharding ShardingPropagation::GetShardingFromUser(
    const HloInstruction& instruction,
    const HloInstruction& user,
    int64 aggressiveness, bool is_spmd,
    const CallGraph& call_graph,
    const CustomCallShardingHelper* helper);
```

### Algorithm

```c
// InferShardingFromUsers — 0x2b1ff00.  Called ONLY by RunToFixPoint.
function InferShardingFromUsers(instruction, computation_map, aggressiveness,
                                is_spmd, sharding_helper, call_graph):

    // TILING GATE (orthogonal to aggressiveness): may this op be partitioned at all,
    // or is it replicate-only (some custom-calls / control ops)?
    if !SupportSpatialPartitioning(instruction, computation_map, is_spmd,    // @ 0x2b142d0
            allow_partial, may_combine, sharding_helper):
        // default to replicated:
        candidate = HloSharding(/*replicated*/true, ..., metadata)           // stock XLA ctor callee

    bool changed = false;
    for user in instruction->users():
        HloSharding s = GetShardingFromUser(*instruction, *user, aggressiveness,
                                            is_spmd, call_graph, sharding_helper); // 0x2b1d200
        if s.is_valid():
            changed |= MaybeImproveInstructionSharding(move(s), instruction,    // stock XLA callee
                           may_combine_partial_with_partial, allow_aggressive)
    return changed


// GetShardingFromUser — 0x2b1d200.  4 callers: InferShardingFromUsers,
// InferUnspecifiedDimsFromOneUser, InferDotShardingFromOperands, and the SPMD
// GetWindowedEinsumConfiguration lambda — i.e. the single backward oracle reused
// throughout SPMD.
function GetShardingFromUser(instruction, user, aggressiveness, ...):
    switch (user.opcode()):       // 100-case switch @ 0x2b1d2bd, default 0x2b1d2c8

      default:                    // elementwise/passthrough: user sharding flows straight back
          return user.sharding()

      case kDot: case kConvolution:
          dims = ParseDotGeneralFromDot(user)   // or ParseConvolutionDimsInfo
          return hlo_sharding_util::InferDotOperandSharding(user, operand_index, dims, ...)
      case kGather:
          return GatherIndexShardingFromOutput(...) | GatherOperandShardingFromOutput(...)
      case kScatter:
          return ScatterIndexShardingFromUpdate(...) | ScatterUpdateShardingFromIndex(...)
                 | ScatterUpdateShardingFromOutput(output_sharding, scatter, call_graph)
      case kReduce:        return reinsert_reduced_dims_as_replicated(user.sharding())
      case kReduceWindow:  return window_aware_backprop(user.sharding())
      case kTranspose:     return TransposeSharding(user.sharding(), inverse_perm)
      case kReshape:       return PropagateShardingThroughReshape(user_shape, op_shape, user.sharding())
      case kReverse:       return ReverseSharding(user.sharding(), dims)
      case kSlice:
          // dim-bounds checks (strings "starts[i] >= 0", "limits[i] <= dim(i)",
          //                    "limits.size() == num_dimensions()") — copy sharding for full dims
      case kPad: case kBroadcast: case kDynamicSlice: case kDynamicUpdateSlice:
          return PartiallyReplicateTiledShardingOnDims(non_passthrough_dims)  // else ReplicateAllDataDims
    // FindCommonSharding merges across multiple users; IsShardingMoreSpecific picks the winner.
```

> **NOTE —** the backward switch's jump table is **100 entries at `0x486540`'s
> neighbor `0x486220`**, and the forward switch's table is **101 entries at
> `0x486540`**. IDA's `data_tables` exporter groups both tables — plus three
> unrelated leading slots — into one contiguous 204-pointer `.rodata` block it tags
> `0x486208`; that tag is an export grouping, **not** a single 204-case switch. Slots
> `0x486208`–`0x486218` belong to a neighboring `HloCSE` / absl `raw_hash_set` helper
> (the first resolves to an `HloCSE` `hash_slot_fn`); the `GetShardingFromUser` case
> targets start at `0x486220` (e.g. `0x486228` → `0x2b1d2c8`, the default case). A
> reimplementer should read 100 entries from `0x486220` for the backward switch and 101
> from `0x486540` for the forward switch — never the whole 204-slot block.

> **GOTCHA —** the two gates are independent. `CanPropagateThroughAtAggressiveLevel`
> (forward, @ `0x2b0fc40`, 14 bb) answers "is the current aggressiveness *level* high
> enough for this op to receive a fresh sharding?" `SupportSpatialPartitioning` (backward,
> @ `0x2b142d0`) answers "may this op be **tiled** at all, vs replicate-only?" as a
> function of `(is_spmd, op type, CustomCallShardingHelper)`, regardless of
> aggressiveness. Conflating them produces either over-replication or premature heuristic
> guessing.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `InferShardingFromUsers` | `0x2b1ff00` | Backward aggregator over all users | CERTAIN |
| `GetShardingFromUser` | `0x2b1d200` | Per-(op,user) backward oracle, 100-case switch | CERTAIN |
| `SupportSpatialPartitioning` | `0x2b142d0` | Tiling-vs-replicate gate | CERTAIN |
| `MaybeImproveInstructionSharding` | — | Merge-and-accept (`MergeShardingIfCompatible` + `set_sharding`) | CERTAIN (callee) |

---

## InferShardingFromShardGroup — Shard-Group Cross-Flow

### Purpose

`Shard-As` groups force identical shardings across their members; `Shard-Like` groups
force compatible ones. This sweep is what makes them converge: for each member, it takes
that member's sharding and tries to improve `instruction` toward it.

### Algorithm

```c
// demangled @ 0x2b18380
bool ShardingPropagation::InferShardingFromShardGroup(
        HloInstruction* instruction, int64 aggressiveness,
        const vector<HloInstruction*>& shard_group):
    bool changed = false;
    for member in shard_group:                                  // Shard-As or Shard-Like peers
        if member.has_sharding():
            changed |= MaybeImproveInstructionSharding(         // stock XLA callee
                           member.sharding(), instruction,
                           may_combine_partial, allow_aggressive)
    return changed   // counted by "shardings inferred from shard group: "
```

The group ids come from `ProcessShardingInstruction`'s
`instruction_to_shard_group_id` / `shard_group_id_to_{shard_as,shard_like}_group` maps.

---

## ProcessShardingInstruction — Seeding

### Purpose

Before the fixpoint can run, the user's explicit shardings — expressed as `CustomCall`
instructions named `"Sharding"` — must be turned into `HloSharding` attributes on real
instructions. `ProcessShardingInstruction` (@ `0x2b27ff0`, 189 bb / 50 callees) is that
seeding pass, called once by `Run`.

### Algorithm

```c
// demangled @ 0x2b27ff0
function ProcessShardingInstruction(module, execution_threads, ...,
        &unspecified_dims, &saved_root_shardings, &saved_param_shardings,
        &instruction_to_shard_group_id, &shard_as_groups, &shard_like_groups,
        param_mask, output_mask, /*remove_unknown=*/...):

    for inst in module->instructions():
        if inst->IsCustomCall("Sharding"):                      // stock XLA callee + "Sharding" string
            cc = Cast<HloCustomCallInstruction>(inst)           // stock XLA callee
            CHECK(cc->has_sharding())                           // "Sharding instruction must have a
                                                                //  sharding attribute: " (verbatim)
            // parse the "?" unspecified dims out of the opaque attr string
            sharding_op_util::ParseAttributes(cc->opaque(), &unspecified_dims[operand]) // stock XLA callee
            // replace the custom-call with its operand, moving the parsed sharding onto it
            HloComputation::ReplaceInstruction(cc, cc->operand(0))   // stock XLA callee
            cc->operand(0)->set_sharding(parsed_sharding)           // stock XLA callee
            // record shard-group bookkeeping + ShardBarrierFrom/To
        // save the user-annotated entry-parameter / root shardings for later restoration
    VLOG("ProcessShardingInstruction: ")                        // verbatim string
```

`AssignShardingMetadata` (@ `0x2b29018`) and `RemoveShardingMetadata` (@ `0x2b272ea`)
bracket the whole `Run` when `propagate_metadata_` is set; they stamp and later strip an
`OpMetadata` provenance tag on each sharding so that merges can track where a sharding
originated. Both CHECK strings are present verbatim: `"Check failed: has_sharding() "`
and `"Sharding instruction expected for: "`.

### The ToParameters / ToOutput seeding

There is **no** separately-named `...ToParameters` / `...ToOutput` function in this
build. The entry-parameter and root-tuple seeding is governed entirely by the two ctor
`vector<bool>` masks, whose names appear as the strings
`"allow_spmd_sharding_propagation_to_parameters"` and
`"allow_spmd_sharding_propagation_to_output"`. The user-annotated entry-param
shardings and root sharding are taken as ground-truth seeds and counted among
`"instructions already sharded:"`; for elements whose mask bit is `false`, propagation
*into* that param/output from the body is forbidden, leaving the user's annotation
authoritative. After the fixpoint,
`CanonicalizeLayoutAfterShardingPropagation(module, param_mask, output_mask)`
(@ `0x90ebee0`) copies the resulting tile layout back onto the entry computation's
`ShapeLayout` (via the callees `ShapeLayout::CopyLayoutFromShape` and `LayoutIsSet`)
for exactly the masked-in elements.

---

## Computation-Boundary Flow

`GetRelatedInstructions` (@ `0x2b147d0`) and `MaybeComputationPropagation` (@ `0x2b156a0`)
move shardings across control-flow boundaries — `while` body ↔ condition, conditional
branches, `call` — so a sharding on any one of a `{call-site, computation parameter,
computation root}` triple propagates to the others.

```c
// GetRelatedInstructions — 0x2b147d0
function GetRelatedInstructions(inst, computation_map):
    // uses while_body / while_condition / called_computations /
    //      parameter_instruction / parameter_number / mutable_operand (all stock XLA callees)
    // returns the counterpart instruction(s) at the boundary

// MaybeComputationPropagation — 0x2b156a0 (17 bb), calls MaybeImproveInstructionSharding only
function MaybeComputationPropagation(computation_map, ..., inst, &changed_set):
    // push a settled sharding across one boundary (e.g. while-body root -> while inst
    //  -> while-body param) and record which instructions changed
```

The `computation_map` (`HloComputation*` → defining `HloInstruction*`) threaded through
every inference function is how the iteration knows which call-site a computation belongs
to.

---

## Aggressiveness Ladder

`aggressiveness` is a single `int64` threaded through `RunToFixPoint`,
`InferShardingFromOperands`, `InferShardingFromUsers`, `GetShardingFromUser`, and
`InferShardingFromShardGroup`, and logged each iteration via the `"aggressiveness: "`
string. The dedicated forward admission gate is
`CanPropagateThroughAtAggressiveLevel` (@ `0x2b0fc40`, 14 bb), consulted before the
opcode switch in `InferShardingFromOperands`.

```text
level 0 : loss-free / unambiguous only (elementwise, exact passthrough)
level 1 : allow ops where one of several legal shardings must be chosen heuristically
level 2 : enable more backward (user->operand) propagation
level 3 : most aggressive — reshapes/transposes/dots may guess;
          allow_aggressive_resharding permitted in ReturnImprovedShardingImpl
```

> **NOTE —** the single `int64` parameter, its per-iteration log line, and the dedicated
> `CanPropagateThroughAtAggressiveLevel` gate are all read directly from the binary. The
> exact `0..3` ladder *span* is **[INFERRED]** from upstream XLA — the bound values are
> not distinct string literals here. `Run` driving `RunToFixPoint` once per level (cheap,
> safe inferences before risky ones) is the upstream design the evidence is consistent
> with.

---

## Provenance — Stock vs Neuron

This entire engine is **stock upstream XLA**, compiled into the Neuron front-end with no
modification:

- The file literal `"xla/service/sharding_propagation.cc"` is referenced throughout.
- Every log/CHECK string is byte-identical to upstream OpenXLA:
  `"Sharding propagation iteration"`, `"Add sharding (forward-pass)"`,
  `"Refined partial sharding"`, `"Sharding propagation completed after N iterations"`,
  `"Aligning shard group"`, `"Shard-As group "` / `"Shard-Like group "`,
  `"ShardBarrierFrom"` / `"ShardBarrierTo"`,
  `"SPMDFullToShardShape"` / `"SPMDShardToFullShape"`,
  `"_sharding_propagation_cse_prevention"`.
- No Neuron-specific opcode, attribute, or branch appears in any of the 33
  `ShardingPropagation` symbols or their direct callees.

Neuron's customization of SPMD is downstream, in the `SpmdPartitioner` that consumes
this pass's propagated shardings — documented in the
[SPMD partitioner driver](spmd-partitioner-driver.md). A reimplementer can treat this
page as a faithful description of OpenXLA's `ShardingPropagation` at the
`absl-20230802` / stable-HLO vintage; there is nothing Neuron-specific to special-case.

---

## Related Components

| Name | Relationship |
|---|---|
| `SpmdPartitioner` | Consumes the propagated shardings; co-constructed with this pass in `RunHloPassesThroughLayoutAssn`; where Neuron SPMD customization lives |
| `hlo_sharding_util` | Owns the sharding algebra (`MergeSharding`, `FindCommonSharding`, `*ShardingFromOperands`, reshape/transpose/reverse sharding) this pass calls |
| `CustomCallShardingHelper` | Ctor-injected helper; gates custom-call tiling in `SupportSpatialPartitioning` |
| `CallGraph` | Built once in `Run`; lets inference cross while/conditional/call boundaries |
| `HloCSE` | The pass whose merging the `_sharding_propagation_cse_prevention` copies defend against |

## Cross-References

- [SPMD Partitioner Driver](spmd-partitioner-driver.md) — the consumer of this pass; runs immediately after, holds the Neuron deltas
- [Sharding Algebra](sharding-algebra.md) — `HloSharding`, tile assignments, `MergeSharding` / `OpShardingRule`, the algebra surface this pass calls
- [AwsNeuronLNCShardingConstraint and the SPMD↔LNC Coupling](lnc-sharding-constraint.md) — 13.9; how Neuron seeds and pins the user `Sharding` annotations this pass propagates
- [SPMD Programming Model](../nki/spmd-programming-model.md) — `program_id` / `num_programs` / grid, the NKI-level view of distributed execution
