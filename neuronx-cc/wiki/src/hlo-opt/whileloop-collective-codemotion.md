# While-Loop Collective Code-Motion

> *All addresses on this page are virtual addresses (VMA) for `neuronx_cc` 2.24.5133.0+58f8de22 (cp310), binary `neuronxcc/starfish/bin/hlo-opt`; resolve via `objdump --start-address` or the VMA-keyed `disasm/`/`function_addresses.json` sidecars. VA ≠ raw file offset: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000 (section headers). Other builds will differ.*

## Abstract

When a transformer or pipeline-parallel program is lowered to HLO, the loop that
walks layers (or microbatches) is a `kWhile`. Every iteration of that loop tends
to emit *the same collective* — an `all-reduce` over a gradient accumulator, a
`reduce-scatter` writing one shard slice, an `all-gather` re-materialising a
sharded weight. Issuing one collective per iteration is the worst case for the
NCC fabric: many tiny transfers, none of which the [combiner pass](collective-combiners.md)
can merge because they are separated by a loop back-edge. This page documents the
three Neuron HLO passes that lift those collectives across the `kWhile` boundary
so a single fabric operation runs on the whole accumulated tensor instead of one
per trip.

The family is three registered passes in namespace `xla::hilo`, each with its own
`Run`. **`NeuronWhileLoopAllReduceCodeMotion`** (`0x1ff39f0`) is a faithful port
of upstream XLA's `WhileLoopAllReduceCodeMotion`: it runs `HloReplicationAnalysis`,
proves a per-iteration all-reduce/reduce-scatter is equal to one post-loop
collective of the accumulated buffer, and hoists it. **`NeuronMoveReduceScatterWhileLoop`**
(`0x1fbf1a0`) and **`NeuronMoveAllGatherWhileLoop`** (`0x1fb85c0`) are bespoke
Neuron passes that walk computations directly, pattern-match a
dynamic-update-slice accumulation idiom (RS) or a tuple-element all-gather (AG),
and resize the while tuple to move the collective out. Two of the three
(`#101`, `#105`) share one structural gate — **`neuron::HasMatchingReplicaGroups`**
(`0x1f7e060`) — which only admits a collective whose `replica_groups` exactly
match a reference set, so the moved op is provably the *same* collective on every
iteration.

The reimplementation contract has three parts:

- **The loop-invariance / legality test per collective** — what makes the
  per-iteration op equal to a single post-loop op (`IsAllReduceMovable`'s
  accumulator trace for `#101`; the trip-count / DUS-dimension match for `#103`;
  the tuple-element form check for `#105`).
- **The `HasMatchingReplicaGroups` gate** — exact element-by-element replica-group
  equality, and why it is the safety belt.
- **The hoist-vs-sink rewrite** — `#101` and `#105` *hoist* (collective moves
  before / out of the loop on a smaller input); `#103` *sinks* (collective moves
  after the loop on the full accumulated tensor). Both directions feed the
  combiners.

> **NOTE —** `hlo-opt` in this build was decompiled with `NVOPEN_IDA_SKIP_DECOMPILE=1`,
> so there is **no Hex-Rays pseudocode** for any `Run` body. Everything below is
> reconstructed from the symbol table (every `Run`/helper address is a real export,
> cross-checked against `function_addresses.json`), the verbatim `.rodata`
> diagnostic strings (each cited at its address), and the disasm opcode/offset
> compares. Call-sequence and string evidence is strong; the exact data-flow
> stitching between anchors is inferred and tagged where it matters.

| | |
|---|---|
| **`#101` Run** | `_ZN3xla4hilo34NeuronWhileLoopAllReduceCodeMotion3RunE…` @ `0x1ff39f0` (~10413 B) |
| **`#103` Run** | `_ZN3xla4hilo32NeuronMoveReduceScatterWhileLoop3RunE…` @ `0x1fbf1a0` (~1361 B) |
| **`#105` Run** | `_ZN3xla4hilo28NeuronMoveAllGatherWhileLoop3RunE…` @ `0x1fb85c0` (~11915 B) |
| **Shared gate** | `neuron::HasMatchingReplicaGroups` @ `0x1f7e060` (240 B) |
| **`#101` legality** | `xla::(anon)::IsAllReduceMovable` @ `0x1fefc90` |
| **IR level** | XLA HLO (`xla::HloModule`), pre-Penguin |
| **Loop selector** | opcode byte at `HloInstruction+0x14`, `kWhile == 0x79` |
| **`name()` keys** | `neuron-while-loop-all-reduce-code-motion` (`0x2e4b10`), `neuron_move_reduce_scatter_while_loop` (`0x37bd20`), `neuron_move_all_gather_while_loop` (`0x2b81e0`) |

---

## Background — why hoist a collective at all

A `kWhile` carries state through a tuple parameter. A common training/inference
idiom accumulates a partial result into one tuple slot per iteration and runs a
collective on it:

```text
loop body (per iteration i):
    acc'   = acc + partial_i              // kAdd into the accumulator slot
    out_i  = all-reduce(acc')             // <-- one collective PER trip
```

Mathematically, `all-reduce` (a sum-reduction) commutes with the in-loop `kAdd`
accumulation: reducing every partial sum and then summing is the same as summing
all partials and reducing once. So the whole loop is equal to:

```text
acc_final = while(...) { acc' = acc + partial_i }   // pure accumulation
out       = all-reduce(acc_final)                   // ONE collective, post-loop
```

That equivalence is the entire legality argument, and it only holds under tight
conditions — the buffer must be a genuine zero-initialised sum-accumulator, the
reduction must be `add`, the dtype must be one the fabric reduces, and the
collective must use the *same* replica grouping every iteration (otherwise the
single post-loop op is over the wrong device set). Each pass below enforces a
slice of those conditions.

The payoff is downstream: once the collective is a single op outside the loop, it
sits next to the model's *other* collectives in the same computation, where the
[combiner pass](collective-combiners.md) can fuse it into one larger fabric
transfer. Hoisting is therefore not the optimisation — it is the *enabler* for
combining. (See also [while-loop unrolling](#cross-references), the alternative
that exposes the same ops by replicating the body instead of moving the op.)

---

## #101 — NeuronWhileLoopAllReduceCodeMotion

### Purpose

The XLA-port pass. Hoists **both** `all-reduce` and `reduce-scatter` out of
while bodies when the collective's input is a loop accumulator. Despite the
name, its scope covers reduce-scatter (it constructs them via `CreateReduceScatter`
with `Cast<HloReduceScatterInstruction>`, and its final summary counts both). The
legality engine is the upstream anonymous-namespace `IsAllReduceMovable`, ported
verbatim into the Neuron `.cc` (`hilo/hlo_passes/neuron_while_loop_all_reduce_code_motion.cc`,
source string @ `0x340848`).

### Entry Point

```text
NeuronWhileLoopAllReduceCodeMotion::Run            0x1ff39f0
  ├─ CallGraph::Build(module)                       0x1ff3b28
  ├─ HloReplicationAnalysis::RunWithPartialReplication  (×2)  0x1ff3da5 / 0x1ff3e56
  ├─ CallGraph::GetComputationCallers               0x1ff3ca5   (is this a while body?)
  ├─ neuron::HasMatchingReplicaGroups               0x1ff41a1   (gate)  → 0x1f7e060
  ├─ xla::(anon)::IsAllReduceMovable                0x1fefc90   (legality)
  ├─ xla::(anon)::ChangeAccumulatorShapesInLoopBodies  0x1ff2370 (resize accumulators)
  └─ CreateAllReduce / CreateReduceScatter + rewire  0x1ff5029 / 0x1ff4ac2
```

### Algorithm

```c
// NeuronWhileLoopAllReduceCodeMotion::Run @ 0x1ff39f0
StatusOr<bool> Run(HloModule* module, const flat_hash_set<string_view>& threads):
    cfg = module->config();                          // [module+0x28]
    // Entry guard (disasm 0x1ff3a3c..0x1ff3a8c): multi-partition without SPMD is unsupported.
    if (cfg->num_partitions()/*[cfg+0x178]*/ > 1     // 0x1ff3a40 cmp>1
        && !cfg->use_spmd_partitioning()/*[cfg+0x1a8]*/)  // 0x1ff3a4a cmp==0
        return false;                                // bail, changed=false

    call_graph = CallGraph::Build(module, threads);  // 0x1ff3b28
    // Prove which values are replicated across the device mesh, so a per-iter
    // collective can be shown identical for every replica/partition.
    if (cfg->num_replicas()/*[cfg+0x170]*/ > 1)      // VLOG "num_replicas: " @0x214c5a
        repl_across_replicas =                       // "...across replicas" @0x3118c8
            HloReplicationAnalysis::RunWithPartialReplication(module, ...);  // 0x1ff3da5
    if (cfg->num_partitions() > 1)                   // VLOG "num_partitions: " @0x266d1f
        repl_across_partitions =                     // "...across partitions" @0x393e88
            HloReplicationAnalysis::RunWithPartialReplication(module, ...);  // 0x1ff3e56

    int n_ar = 0, n_rs = 0;
    for (comp : module->computations(threads)):      // 0x1ff3b6d
        callers = call_graph->GetComputationCallers(comp);          // 0x1ff3ca5
        if (callers.empty() || callers[0]->opcode() != kWhile/*0x79*/) // 0x1ff3cf8
            continue;                                // only while BODIES are scanned
        while_inst = callers[0];

        for (inst : comp->MakeInstructionPostOrder()):              // 0x1ff408f
            op = inst->opcode();                     // movzx eax,[r14+0x14] @0x1ff40bb
            if (op != kAllReduce && op != kReduceScatter) continue;
            if (!neuron::HasMatchingReplicaGroups(inst, ref_groups)) // 0x1ff41a1
                continue;                            // replica grouping must match (see §gate)

            // Legality: is the collective's input a hoistable loop accumulator?
            ctx = IsAllReduceMovable(inst, comp, repl_across_replicas,
                                     repl_across_partitions);        // 0x1fefc90
            // VLOG "...all-reduce: <inst> while loop: <...> is_movable: <b> num_accumulations: <n>"
            if (!ctx.is_movable) continue;
            accum_map[while_inst].push_back(ctx);    // map<HloInstruction*, vector<AccumulationContext>>

        // ---- rewrite this while loop ----
        ChangeAccumulatorShapesInLoopBodies(while_inst, accum_map); // 0x1ff48a7 -> 0x1ff2370
        new_init = CreateTuple(...); AddInstruction(...);           // 0x1ff47d8/0x1ff47f2
        new_while = CreateWhile(shape, body, cond, new_init);       // 0x1ff4906/0x1ff4918
        for (each movable collective):
            ch = NextChannelId(module);              // fresh fabric channel
            if (is_reduce_scatter):
                outside = CreateReduceScatter(shape, {operand}, reduce_comp,
                              dev_list, constrain_layout, ch, use_gids, scatter_dim); // 0x1ff4ac2
                n_rs++;
            else:
                outside = CreateAllReduce(shape, {operand}, reduce_comp,
                              dev_list, constrain_layout, ch, use_gids);              // 0x1ff5029
                n_ar++;
            if (Shape::Equal(outside.shape, carried.shape))          // 0x1ff4cc9
                outside = CreateBinary(shape, kAdd, outside, carried);  // 0x1ff4cef
            gte = CreateGetTupleElement(new_while, idx);             // 0x1ff4e11
            ReplaceInstructionWithDifferentShape(old, gte/...);      // 0x1ff566d
        ReplaceInstruction(old_while, CreateTuple(...));             // 0x1ff551b/0x1ff559c

    VLOG("Hoisted " << n_ar << " all-reduce and "                   // @0x256fea
         << n_rs << " reduce-scatter out of while loops");          // @0x34c210
    return (n_ar > 0 || n_rs > 0);
```

### Legality — `IsAllReduceMovable` @ `0x1fefc90`

This is upstream XLA's anonymous-namespace `IsAllReduceMovable`
(`_ZN3xla12_GLOBAL__N_118IsAllReduceMovableE…`), ~9166 B.
Its job is to decide whether the collective's operand is a *loop accumulator*
that can be reduced once post-loop. It enforces, in order:

1. **Sum-reduction.** `MatchReductionComputation(to_apply)` (call @ `0x1fefd03`,
   target `xla::MatchReductionComputation` @ `0x9164750`); a `setz` records whether
   the matched reduction kind is `add`. Only sum-accumulators are movable.
2. **Supported dtype.** The element type must be in a 12-entry
   `InlinedVector<PrimitiveType,12>` searched by `c_linear_search` (@ `0x1fefde6`).
   The list is built inline (`0x1fefd2f..0x1fefda1`) as six packed qwords —
   `{0xA00000010, 0xC0000000B, 0x300000002, 0x500000004, 0x700000006, 0x900000008}`
   — decoding to `PrimitiveType {2,3,4,5,6,7,8,9,10,11,12,16}` =
   `{S8,S16,S32,S64,U8,U16,U32,U64,F16,F32,F64,BF16}` (matches upstream
   `kSupportedTypes`). The queried type is `inst->shape().element_type()`.
3. **Collective group mode.** `GetCollectiveOpGroupMode(use_global_device_ids,…)`.
4. **Accumulator trace.** Walks the GTE → parameter chain
   (`Cast<HloGetTupleElementInstruction>`, `Cast<HloParameterInstruction>`,
   `GetEffectiveScalar` @ `0x1fed220`, `IsZero` for the zero-init) under three
   CHECK invariants: `while_body->num_parameters() == 1` (`0x3b7408`),
   `parameter_number == 0` (`0x25f292`), `param_no < param_instructions_.size()`
   (`0x37ba08`).
5. **Rejection.** Any of these emit a verbatim diagnostic and mark *not movable*:
   `") is an unsupported operation on accumulation buffer."` (`0x39f650`),
   `", preventing the motion of all-reduce."` (`0x2e4b40`),
   `", preventing the motion of reduce-scatter."` (`0x2b8290`), plus a "we do not
   yet support more than 1 dynamic-slices on the accumulation buffer" guard.

> **GOTCHA —** the invariance test is **not** "the collective has loop-invariant
> operands". It is the much narrower "the operand is a zero-initialised sum
> accumulator written only by `kAdd`, touched by at most one dynamic-slice, and not
> otherwise consumed in the loop." A reimplementation that hoists any collective
> whose inputs don't change between iterations will produce wrong numerics — the
> whole point is the algebraic identity `reduce∘Σ = reduce-once∘accumulate`, which
> requires the accumulator structure, not mere invariance.

### Accumulator reshaping — `ChangeAccumulatorShapesInLoopBodies` @ `0x1ff2370`

Before the collective is pulled out, the in-loop accumulators are resized to the
per-iteration (non-reduced) shape so the body now accumulates the *un-reduced*
partials and the single post-loop collective sees the full tensor. The driver is
the `map<HloInstruction*, vector<AccumulationContext>, HloPtrComparator>` built
during the scan; `AccumulationContext` is the per-accumulator metadata
(origin tuple index, output tuple index, dynamic-slice info). A CHECK
`gte_user->opcode() == HloOpcode::kAdd` (`0x2ac0d8`) re-confirms the `+=` idiom at
rewrite time.

> **NOTE — `AccumulationContext`'s field layout is [INFERRED].** Its offsets were never
> enumerated (no `structures.json` hit); what is recovered is its container type plus the
> `get_origin_tuple_index` / `get_output_tuple_index` diagnostics, which name the fields
> without fixing their positions.

---

## #103 — NeuronMoveReduceScatterWhileLoop

### Purpose

A bespoke Neuron pass (`hilo/hlo_passes/neuron_move_reduce_scatter_while_loop.cc`,
source @ `0x28a7c8`). Despite "Move … across boundary", it **sinks** the
collective to *after* the loop: it finds an in-loop reduce-scatter (or all-reduce)
that scatters/reduces one slice per iteration into a dynamic-update-slice
accumulation, and rewrites it into a single post-loop collective over the full
trip-count-sized tensor. It handles both `kReduceScatter` and `kAllReduce`
(via two sibling movers), and does **not** use `HasMatchingReplicaGroups`.

### Entry Point

```text
NeuronMoveReduceScatterWhileLoop::Run              0x1fbf1a0
  ├─ <iterate module->computations() [module+0x40 .. +0x48]>  0x1fbf1da/0x1fbf1e5
  ├─ comp->MakeInstructionPostOrder()              0x1fbf214
  ├─ FindReduceScatterAndAllReduce                 0x1fbf30d → 0x1fbb480
  ├─ MoveReduceScatterOutOfWhileLoop               0x1fbf36d → 0x1fbee20
  │     └─ MatchReduceScatterPattern               0x1fbb6d0
  │     └─ MoveReduceScatterOutOfWhileLoopHelper    0x1fbd090
  ├─ MoveAllReduceOutOfWhileLoop                   0x1fbf3ca → 0x1fbc9a0
  │     └─ MatchAllReducePattern                   0x1fbba90
  │     └─ MoveAllReduceOutOfWhileLoopHelper        0x1fbc390
  ├─ UpdateWhileLoopTupleShapes                    0x1fbbf90
  └─ HloDCE::Run                                   0x1fbf64a
```

### Algorithm

```c
// NeuronMoveReduceScatterWhileLoop::Run @ 0x1fbf1a0
StatusOr<bool> Run(HloModule* module, const flat_hash_set<string_view>& threads):
    bool changed = false;
    for (comp : module->computations()):             // begin/end @ [module+0x40 .. +0x48]
        for (inst : comp->MakeInstructionPostOrder()):
            if (inst->opcode() != kWhile/*0x79*/) continue;   // cmp [r12+0x14],0x79 @0x1fbf248
            body = inst->while_body();               // 0x1fbf253
            cond = inst->while_condition();          // 0x1fbf25e
            vector rs_vec, ar_vec;
            FindReduceScatterAndAllReduce(body, &rs_vec, &ar_vec);   // 0x1fbf30d

            VLOG("Trying to move reduce scatters");  // @0x3407a8
            changed |= MoveReduceScatterOutOfWhileLoop(comp, inst, body, cond,
                                                       init, rs_vec);   // 0x1fbf36d
            VLOG("Trying to move all reduces");      // @0x224ba1
            changed |= MoveAllReduceOutOfWhileLoop(comp, inst, body, cond,
                                                   init, ar_vec);       // 0x1fbf3ca
    HloDCE::Run(module, threads);                    // 0x1fbf64a
    return changed;
```

### Candidate discovery — `FindReduceScatterAndAllReduce` @ `0x1fbb480`

Walks body instructions, classifying by the opcode byte and bucketing into the two
vectors, with an operand-shape gate (`cmp rsi,[r15+0x10]` @ `0x1fbb527`):

```c
// FindReduceScatterAndAllReduce @ 0x1fbb480
for (i : body->instructions()):
    switch (i->opcode()):                            // al compares
        case 0x57: /* kReduceScatter */ if (shape_ok) rs_vec->push_back(i); break;  // @0x1fbb4d0
        case 0x07: /* kAllReduce     */ if (shape_ok) ar_vec->push_back(i); break;  // @0x1fbb4d4
```

### Legality — `MatchReduceScatterPattern` @ `0x1fbb6d0`

The sink is only legal when the loop's trip count is statically known and the
accumulation tensor's leading (slice) dimension equals it — otherwise the single
post-loop collective would operate on a wrongly-sized tensor. Two verbatim guards:

- `"While loop does not have a known trip count"` (`0x340750`) — bail.
- `"Left-hand side dimension of the tensor being dynamic sliced does not match the trip count"`
  (`0x363ce8`) — the leading DUS dimension must equal the trip count.

Out-params `(HloInstruction** out, long* out_index, HloInstruction* root)` return
the matched DUS/RS chain and its tuple index. `MatchAllReducePattern` @ `0x1fbba90`
is the all-reduce sibling with the same signature.

### Rewrite — `MoveReduceScatterOutOfWhileLoopHelper` @ `0x1fbd090`

The sinking transform (~7518 B). The reduce-scatter moves *after* the loop and
runs once on the full accumulated tensor; the in-loop DUS is reshaped to the full
shape and its start indices recomputed. The trace strings expose the mechanics:

```text
"Found operand creation point: " / "Modified operand creation point: "
"Processing tuple element at index: "
"About to make a new reshape shape" / "New reshape shape" / "New full shape: "
"Made new start indices"
"Created new reshape" / "Created tuple element in while body: "
"Created new dynamic-update-slice: "     (@0x2e49f8)   // full-shape in-loop DUS
"Created new reduce-scatter: "           (@0x25b097)   // single post-loop RS
"Replaced operand in original input tuple."
"Replaced get-tuple-element instructions with new reduce-scatter."
```

The exact HLO-API rewrite ops appear as CHECK assertions (verbatim):
`original_input_tuple->ReplaceOperandWithDifferentShape(tuple_index, new_operand)`
(`0x334050`), `gte->ReplaceAllUsesWithDifferentShape(new_reduce_scatter)`
(`0x3340a8`), `dynamic_update_slice->ReplaceAllUsesWithDifferentShape(new_dus)`
(`0x393d90`). `MoveAllReduceOutOfWhileLoopHelper` @ `0x1fbc390` is the AR sibling.

> **QUIRK —** `#103` is the *dual* of `#101`. `#101` proves a per-iteration
> all-reduce equals a reduce of the accumulated buffer and hoists it; `#103`
> proves a per-iteration reduce-scatter into a sliced buffer equals one
> reduce-scatter of the full buffer and sinks it. The direction differs because
> the accumulation lives on opposite sides of the collective: `#101` accumulates
> *then* reduces (so reduce moves out behind the loop's result), `#103` reduces a
> *slice* into an accumulator (so the collective moves past the loop's exit onto
> the assembled whole). Both end with a single fabric op the combiner can merge.

---

## #105 — NeuronMoveAllGatherWhileLoop

### Purpose

A bespoke Neuron pass (`hilo/hlo_passes/neuron_move_all_gather_while_loop.cc`,
source @ `0x34c0d0`). **Hoists** an all-gather to *before* the loop: when a carried
tuple element is repeatedly all-gathered each iteration (re-materialising a
sharded weight), it gathers once on the smaller per-shard tensor outside the loop
and carries the gathered (larger) shape through the tuple. It accepts both the
`AG(gte)` and `AG(convert(gte))` forms, is gated by `HasMatchingReplicaGroups`,
and inlines pass `#106` (`NeuronDuplicateParameterAllGatherRemover`) as cleanup.

### Entry Point

```text
NeuronMoveAllGatherWhileLoop::Run                  0x1fb85c0
  ├─ <iterate module->computations()>              0x1fb85d4/0x1fb8600
  ├─ comp->MakeInstructionPostOrder()              0x1fb8627
  ├─ neuron::HasMatchingReplicaGroups              0x1fb8925 → 0x1f7e060   (gate)
  ├─ AG->CloneWithNewOperands (hoisted)            0x1fb8de5
  ├─ ShapeUtil::UpdateTupleShape                   0x1fb9043/0x1fba7c6
  ├─ ShapeUtil::MakeValidatedTupleShape            0x1fba981
  ├─ HloDCE::Run                                   0x1fba2d6   (1st)
  ├─ fixGTE   (per inst, post-resize repair)       0x1fba3ae → 0x1fb7a80
  ├─ HloDCE::Run                                   0x1fba424   (2nd)
  └─ NeuronDuplicateParameterAllGatherRemover::Run 0x1fbb0c4 → 0x1f8e890  (pass #106, inlined)
```

### Algorithm

```c
// NeuronMoveAllGatherWhileLoop::Run @ 0x1fb85c0
StatusOr<bool> Run(HloModule* module, const flat_hash_set<string_view>& threads):
    bool changed = false;
    for (comp : module->computations()):
        for (inst : comp->MakeInstructionPostOrder()):
            if (inst->opcode() != kWhile/*0x79*/) continue;   // cmp [rdx+0x14],0x79 @0x1fb864b
            VLOG("Found while loop: ");               // @0x27e850
            if (carried_param is not a tuple):        // VLOG "...not a tuple. Breaking." @0x393d28
                continue;
            body = inst->while_body();                // 0x1fb8756

            // ---- find movable all-gathers attached to the carried tuple ----
            VLOG("Number of all-gather operations found: ");   // @0x2ce8d0
            for (ag : tuple-element all-gathers):
                // accepts AG(gte)  [VLOG "All-gather on tuple element: " @0x28260d]
                //     and AG(convert(gte)) [VLOG "All-gather on convert(tuple element): " @0x2e49d0]
                if (!HasMatchingReplicaGroups(ag, ref_groups))  // 0x1fb8925
                    continue;
                if (forbidden user): continue;        // 3 "Not removing this ..." guards
                                                      //  @0x393d60 / @0x3f1838 / @0x3406f8
                VLOG("Found moveable AG ");           // @0x25b084

                // ---- hoist: clone AG outside on the pre-gather operand ----
                clone = ag->CloneWithNewOperands(new_shape, {full_operand}, ctx);  // 0x1fb8de5
                input_tuple->ReplaceOperandWithDifferentShape(idx, clone);         // 0x1fb8e6c
                gte = CreateGetTupleElement(elem_shape, param, idx);               // 0x1fb8ef8
                ShapeUtil::UpdateTupleShape(tuple_shape, idx, &elem_shape);        // 0x1fb9043
                ag->ReplaceAllUsesWithDifferentShape(gte);                         // 0x1fb9143
                changed = true;

            // rebuild body+cond parameter tuples with the new element shapes
            new_tuple_shape = ShapeUtil::MakeValidatedTupleShape(new_shapes);      // 0x1fba981
            VLOG("Moved all-gather (and convert if applicable) out of while loop: "); // @0x3c2708

    HloDCE::Run(module, threads);                     // 0x1fba2d6  (1st DCE)
    VLOG("Fix GTEs start");                           // @0x27e863
    for (inst : comp->MakeInstructionPostOrder()): fixGTE(inst);  // 0x1fba3ae -> 0x1fb7a80
    VLOG("Fix GTEs done");                            // @0x247b6d
    HloDCE::Run(module, threads);                     // 0x1fba424  (2nd DCE)
    VLOG("Remover is start");                         // @0x20ca3c
    NeuronDuplicateParameterAllGatherRemover::Run(module, threads);  // 0x1fbb0c4 -> 0x1f8e890
    VLOG("Remover is done");                          // @0x218ef2
    VLOG("NeuronMoveAllGatherWhileLoop pass complete"); // @0x340720
    return changed;
```

### Post-resize repair — `fixGTE` @ `0x1fb7a80`

Resizing a tuple slot invalidates every `get-tuple-element` that read the old
shape. `fixGTE` (~1205 B / 76 bb) re-derives each GTE's shape from the new tuple
shape (diagnostics `"New GTE "` @ `0x238513`, `"Bad GTE "` @ `0x25b07b`). It runs
between the two DCE passes.

> **GOTCHA —** the hoist *creates* duplicate parameter all-gathers — after moving
> the AG outside, the un-gathered per-shard value and the gathered value can both
> flow into the parameter tuple, leaving a redundant in-loop gather. `#105` does
> not leave this for a later pipeline stage: it inlines
> `NeuronDuplicateParameterAllGatherRemover::Run` (`#106`, @ `0x1f8e890`) directly,
> so the two passes are coupled. A reimplementer who registers `#105` and `#106`
> as independent pipeline entries will diverge from the binary, which calls the
> remover from inside the mover.

---

## The Shared Gate — `neuron::HasMatchingReplicaGroups`

### Purpose

Both `#101` and `#105` admit a collective only if its `replica_groups` exactly
match a reference set carried by the pass object (the
`vector<ReplicaGroup>` ctor argument — see `NeuronMoveAllGatherWhileLoopC1` taking
`PSt6vectorINS_12ReplicaGroupE…`). This is the safety belt: hoisting a collective
is only valid if it is the *same* collective — over the same device set — on every
iteration. A loop whose replica grouping varies per iteration must not be hoisted.

### Algorithm

At 240 B / 11 basic blocks the function was read end to end, so its semantics are exact:

```c
// neuron::HasMatchingReplicaGroups @ 0x1f7e060
//   _ZN6neuron24HasMatchingReplicaGroupsEPKN3xla14HloInstructionESt6vectorINS0_12ReplicaGroupESaIS5_EE
bool HasMatchingReplicaGroups(const HloInstruction* inst,
                              vector<ReplicaGroup> ref):
    a = inst->replica_groups();                      // 0x1f7e077
    if (ref.size() != a.size()) return false;        // 0x1f7e08a / 0x1f7e08d
    if (a[0].count(/*+0x10*/) != ref[0].count()) return false;  // 0x1f7e0b1  (per-group id-count)
    for (g = 0; g < a.size(); ++g):                  // outer loop @0x1f7e0c0
        if (a[g].count != ref[g].count) return false;   // 0x1f7e138 (re-checks +0x10)
        for (i = 0; i < a[g].count; ++i):
            // replica_ids[i] at [+0x18 + i*8]
            if (a[g].replica_ids[i] != ref[g].replica_ids[i]) return false;  // 0x1f7e123/0x1f7e127
    return true;                                      // 0x1f7e146 mov eax,1
```

Exact element-by-element equality: group count, per-group id count (`+0x10`), and
every replica id (`+0x18 + i*8`). `#103` does **not** use it — it relies on the
trip-count/DUS-dimension match instead.

---

## Legality Conditions — Consolidated

| Pass | Loop selection | Collective(s) | Invariance / legality (anchored) | Direction & trip-count |
|---|---|---|---|---|
| `#101` AllReduceCodeMotion | `caller->opcode()==kWhile` via CallGraph callers (`0x79`@`0x1ff3cf8`) | `kAllReduce` (0x07) + `kReduceScatter` | (1) multi-partition ⇒ require SPMD; (2) sum-reduction `MatchReductionComputation`; (3) dtype ∈ `{S8/16/32/64,U8/16/32/64,F16/32/64,BF16}`; (4) zero-init `kAdd` accumulator, ≤1 dynamic-slice, not otherwise used; (5) `HasMatchingReplicaGroups`; (6) `num_parameters()==1`, param 0 | **Hoist** out; `HloReplicationAnalysis` (replicas+partitions) proves replicated; `ChangeAccumulatorShapesInLoopBodies` reshapes |
| `#103` MoveReduceScatter | iterate computations; `inst->opcode()==kWhile` (`0x79`@`0x1fbf248`) | `kReduceScatter` (0x57) + `kAllReduce` (0x07) | `MatchReduceScatterPattern`: known trip count required; DUS leading dim **must equal** trip count; `HasMatchingReplicaGroups` **not** used | **Sink** after; explicit trip-count↔DUS match; reshape buffer to full |
| `#105` MoveAllGather | iterate computations; `inst->opcode()==kWhile` (`0x79`@`0x1fb864b`) | `kAllGather` on `gte` or `convert(gte)` | carried param must be a tuple; AG/convert/tuple users must permit removal (3 guards); `HasMatchingReplicaGroups` | **Hoist** before; resize tuple, `fixGTE`, then `#106` remover + DCE |

**Opcode offset.** All three read the opcode as the single byte at
`HloInstruction+0x14`. Observed constants: `kAllReduce=0x07`, `kReduceScatter=0x57`,
`kWhile=0x79` — consistent with XLA's alphabetised `HloOpcode` enum.

---

> **GOTCHA — all three pass names understate their scope.** `NeuronWhileLoopAllReduceCodeMotion`
> hoists **reduce-scatter as well as all-reduce** (its final summary counts both, and it builds
> RS via `CreateReduceScatter` @ `0x1ff4ac2`), and `NeuronMoveReduceScatterWhileLoop` sinks
> **all-reduce as well as reduce-scatter** (`FindReduceScatterAndAllReduce` collects both `0x57`
> and `0x07`, feeding a dedicated `MoveAllReduceOutOfWhileLoop` @ `0x1fbc9a0`).

## Evidence summary

What the central claims rest on:

- **The three `Run` addresses** (`0x1ff39f0` / `0x1fbf1a0` / `0x1fb85c0`) are exact entries in
  `function_addresses.json`, each carrying the full demangled
  `xla::hilo::Neuron…::Run(HloModule*, flat_hash_set<string_view>&)` symbol and a paired
  `.cold` at `0x1ff35ac` / `0x1fbf07e` / `0x1fb7f36`.
- **`IsAllReduceMovable` belongs to upstream XLA, not to Neuron.** Its symbol is
  `_ZN3xla12_GLOBAL__N_118IsAllReduceMovableEPNS_27HloAllReduceInstructionBaseE…` @ `0x1fefc90`
  — namespace `xla::(anonymous)`, taking `HloAllReduceInstructionBase*` and two
  `unique_ptr<HloReplicationAnalysis>`. `ChangeAccumulatorShapesInLoopBodies` (`0x1ff2370`) and
  the `AccumulationContext` map type are likewise `xla::_GLOBAL__N_1`, which is what pins the
  code-motion pass as a port rather than a rewrite.
- **The replica-group gate is exact equality.** The 240-byte
  `_ZN6neuron24HasMatchingReplicaGroupsE…` @ `0x1f7e060` was read end to end; the `+0x10` count
  field and `+0x18 + i*8` id stride come straight from the loop's load offsets. It is called
  from the code-motion pass (`0x1ff41a1`) and the all-gather mover (`0x1fb8925`), and is absent
  from the reduce-scatter mover's callee set.
- **Direction of motion.** The reduce-scatter mover builds `"Created new reduce-scatter: "`
  (`0x25b097`) over a full-shape `"New full shape: "` accumulation, placing it after the loop;
  the all-gather mover calls `CloneWithNewOperands` (`0x1fb8de5`) on a per-shard operand and
  emits `"Moved all-gather … out of while loop: "` (`0x3c2708`), placing it before.
- **The coupling of the mover and the remover.** `NeuronMoveAllGatherWhileLoop::Run` calls
  `NeuronDuplicateParameterAllGatherRemover::Run` @ `0x1f8e890` directly, bracketed by
  `"Remover is start"` (`0x20ca3c`) and `"Remover is done"` (`0x218ef2`).

Every diagnostic string cited on this page was re-grepped from `strings.json` and matched its
address verbatim.

## Limits of this reading

This binary was decompiled without Hex-Rays output, so no pseudocode exists for any `Run` body
and the reconstruction rests on the symbol table, `.rodata` diagnostics and disasm compares.
Three things are consequently reconstructed rather than read: the exact data-flow stitching
between call anchors inside the rewrite bodies, the `AccumulationContext` field layout, and
`MatchReduceScatterPattern`'s predicate beyond its two trip-count invariants.

---

## Related Components

| Pass | Addr | Relationship |
|---|---|---|
| `NeuronDuplicateParameterAllGatherRemover` (`#106`) | `0x1f8e890` | Inlined by `#105` to remove the duplicate parameter all-gathers the hoist creates |
| Collective combiners | — | The consumer: hoisted/sunk single collectives become combinable, the actual win |
| `HloDCE` | — | Cleanup after every rewrite (`#103` 1×, `#105` 2×) |
| `HloReplicationAnalysis` | — | `#101`'s replicated-value prover (across replicas and partitions) |

## Cross-References

- [AllReduce/ReduceScatter/AllGather Combiners & Threshold Model](collective-combiners.md) — Part 4.5, the consumer of every collective these passes expose
- [AllReduce→ReduceScatter & DynamicSlice Rewrites](allreduce-dynslice-rewrites.md) — sibling collective rewrites in the same pipeline stage
- [While-Loop Unroll & All-Gather Trip-Count Rewrite](whileloop-unroll-tripcount.md) — Part 4.11, the alternative that exposes the same collectives by replicating the loop body instead of moving the op
- [The hlo-opt Pass Registry](pass-registry.md) — where `#101`/`#103`/`#105`/`#106` are registered and ordered
- [Mesh → Replica-Group Topology Math](../distribution/mesh-replica-group-math.md) — Part 13, the replica-group model that `HasMatchingReplicaGroups` compares against
