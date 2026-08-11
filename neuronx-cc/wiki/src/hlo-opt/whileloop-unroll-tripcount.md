# While-Loop Unroll & All-Gather Trip-Count Rewrite

> *All addresses on this page are virtual addresses (VMA) for `neuronx_cc` 2.24.5133.0+58f8de22 (cp310), binary `neuronxcc/starfish/bin/hlo-opt`; the disasm/JSON sidecars are already in VA and resolve via `objdump --start-address`. VA ≠ raw file offset: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000 (section headers). Other wheels differ — treat every address as version-pinned.*

## Abstract

`hlo-opt` carries **two** distinct while-loop unrollers and they are not the same pass twice. `xla::UnrollWhileLoop` (registration #25, `--passes` key `unroll-while-loop`) is a **bespoke minimal** unroller: for every `kWhile` it asks the shared trip-count engine for a static count (capped at 128), and if the count is known it explodes the loop into one cloned-and-`Call`ed body per iteration — no body-size budget, no `WhileLoopConfig`, no partial unroll. `xla::hilo::NeuronWhileLoopUnroller` (registration #112, key `while_loop_unroller`) is a **port of upstream XLA `WhileLoopUnroller`** with the full apparatus: module canonicalisation, structural+trip-count eligibility, a three-gate instruction-explosion budget, a full-vs-partial (`wrap_in_trivial_loop`) dispatch, and two Neuron-specific deltas — the budget *defaults* baked into the pass constructor and a boundary-marker cleanup step. Both unrollers share exactly one engine, `while_loop_analysis.cc`'s `ComputeWhileLoopTripCount` / `MatchTrivialLoopTripCount`.

A third pass, `xla::hilo::NeuronRewriteAllGatherTripCount` (registration #111, key `aws_neuron_rewrite_all_gather_trip_count`), is an `OpExpander` that is *trip-count-aware but does not unroll*. It matches the `all_gather(reshape(dynamic_slice(get_tuple_element(parameter))))` idiom inside a known-trip-count while body — the pattern that gathers one slice of a loop-carried tensor each iteration — and rewrites it to gather the **full trip-count-sized** tensor once, then re-derive the per-iteration view by dynamic-slice. It is the dual of the collective-motion family ([4.13](whileloop-collective-codemotion.md)): instead of hoisting the collective out of the loop body wholesale, it hoists the *gathered extent* up to the whole trip count.

This page reconstructs all three: the `UnrollWhileLoop::Run` clone-and-`Call` rewrite, the shared `ComputeWhileLoopTripCount` analysis (induction-var detection → init evaluation → closed-form match → bounded brute-force simulation), the `NeuronWhileLoopUnroller` driver and its budget constants (trip-count ≤ 1000, body ≤ 100000, trip×body ≤ 800000), the full/partial split, and the all-gather rewrite. The boundary-marker coupling that both #112 and the unrolled output depend on is owned by [4.12](boundary-markers-layer-cut.md); the HLO opcode/channel-id/replica-group concepts are owned by [Part 13](../distribution/mesh-replica-group-math.md).

For reimplementation, the contract is:

- The **two unrollers' architectural split**: a thin `kWhile`-scan + `ComputeWhileLoopTripCount(while, 128)` + clone-body-per-iteration pass (#25) versus a budgeted XLA-port driver (#112), and the fact that they share *only* the analysis engine.
- The **shared trip-count engine**: `GetLoopInductionVarTupleIdx` → `EvaluateIndvarInit` → `MatchTrivialLoopTripCount` (closed-form `i<N`/`i<=N`) → `HloEvaluator` brute-force fallback capped at `max`. It does **not** read `WhileLoopBackendConfig.known_trip_count`.
- The **#112 budget**: the three `InitialFeasibilityCheck` reject gates and the three constructor-baked threshold immediates that feed them.
- The **all-gather trip-count rewrite**: the matcher's dimension invariants and the expander's field reads (`all_gather_dimension @+0x258`, `channel_id @+0x48` **reused**, `constrain_layout @+0x250`, `use_global_device_ids @+0x260`) and the full-extent recomputation.
- The **channel-id policy contrast**: #25 and #112 assign a *fresh* `NextChannelId` to every unrolled collective; #111 *reuses* the original all-gather's channel-id.

| | |
|---|---|
| **#25 `UnrollWhileLoop::Run`** | `0x1f733a0` (3010 B, 161 bb) — key `unroll-while-loop` (17) @ name `0x1f72f00` |
| **#112 `NeuronWhileLoopUnroller::Run`** | `0x20013a0` (2691 B, 149 bb) — key `while_loop_unroller` (19) @ name `0x1ff62b0` |
| **#111 `NeuronRewriteAllGatherTripCount`** | matcher `0x1fd7300` (2012 B) / expander `0x1fd7cc0` (4217 B) — key (40) @ name `0x1fd7050` |
| **Shared engine** | `ComputeWhileLoopTripCount` @ `0x8aaee70` (3013 B) · `MatchTrivialLoopTripCount` @ `0x8aac420` (4619 B) |
| **#112 budget factory** | `_M_invoke` @ `0x1e70200` — writes `[obj+0x18]=1000`, `[obj+0x20]=800000`, `[obj+0x28]=100000` |
| **Boundary toggle** | env `NEURON_DISABLE_BOUNDARY_MARKER` → `hlo_utils::EnvVarSetToOne` @ call `0x2001652` |
| **kWhile opcode byte** | `HloInstruction+0x14 == 0x79` (cmp @ `0x1f73458` / `0x1f7349f`) |

---

## #25 — `xla::UnrollWhileLoop` (the bespoke minimal unroller)

### Purpose

A small, budget-free pass that fully unrolls any while loop whose static trip count is at most 128. It exists as the simplest possible "if you know how many times it runs, write it out flat" transform — no profitability model, no partial unroll, no config object. It carries **no** `while_loop_unroller.cc` source string; its only distinguishing assertion is the `CHECK` text `while_op->parent()->ReplaceInstruction(while_op, call_op)` (string @ `0x34bf60`, referenced at `0x1f73c54`). This is what disambiguates it from the upstream-port #112: they are genuinely different passes that happen to share the trip-count analyser.

### Entry Point

```text
xla::UnrollWhileLoop::Run (0x1f733a0)            ── scan computations, full-unroll known loops
  ├─ scan instructions, gate on  [HloInstruction+0x14] == 0x79 (kWhile)   @0x1f7349f
  ├─ xla::ComputeWhileLoopTripCount(while, 128)  (0x8aaee70)              ── shared engine, §"Trip-Count Engine"
  ├─ record (while → trip_count) into a DenseMap                          @insert 0x1f7358a
  └─ per recorded loop:  clone body × trip_count, wire Calls, replace
       ├─ HloComputation::Clone("clone")                                  @0x1f739be
       ├─ HloModule::AddEmbeddedComputation                               @0x1f73931 / 0x1f739d1
       ├─ hlo_query::NextChannelId(module)                                @0x1f73a1d  (fresh id per clone)
       ├─ HloInstruction::CreateCall(shape, {params}, embedded)           @0x1f73b1b / 0x1f73bdd
       └─ HloComputation::ReplaceInstruction(while, call)                 @0x1f73c45
```

### Algorithm

```c
function UnrollWhileLoop_Run(module):                       // 0x1f733a0
    known = DenseMap<HloInstruction*, int64>{}              // grow @0x1f72fe0
    for comp in module.computations():
        for inst in comp.instructions():
            if inst[0x14] != 0x79:        continue          // kWhile gate; cmp @0x1f7349f
            // The ONLY budget #25 applies: cap the brute-force sim at 128.
            tc = ComputeWhileLoopTripCount(inst, /*max=*/128)   // mov esi,0x80 @0x1f734a7; call 0x8aaee70
            if !tc.has_value():           continue          // unknown count -> leave loop intact @0x1f734bb
            known.insert(inst, *tc)                          // mov [rax],while; [rax+8],tc @0x1f7358a

    for (while_op, N) in known:                              // gated  cmp [..],0; jle  @0x1f7395a
        body = while_op.while_body()                         // @0x1f7397f
        // Build one Call to a fresh clone of the body per iteration.
        for i in 0 .. N:
            clone    = body.Clone("clone")                   // @0x1f739be  (name "clone" @0x218ed2)
            embedded = module.AddEmbeddedComputation(clone)  // @0x1f739d1
            ch       = hlo_query::NextChannelId(module)      // @0x1f73a1d
            // Renumber collectives so unrolled copies stay distinct participants:
            for c in embedded.instructions():
                if DynCast<HloChannelInstruction>(c):        // @0x1f73a53
                    c.set_channel_id(ch)                     // fresh id per clone
            call = HloInstruction::CreateCall(shape, {params}, embedded)  // @0x1f73b1b
            comp.AddInstruction(call)
        // Splice the per-iteration Call chain in place of the while:
        while_op.parent().ReplaceInstruction(while_op, last_call)         // @0x1f73c45
            // CHECK "while_op->parent()->ReplaceInstruction(while_op, call_op)" @0x34bf60
    return changed
```

> **NOTE — 128 is a simulation cap, not a "max unroll" budget.** `ComputeWhileLoopTripCount`'s second argument bounds only the brute-force iteration-count *simulation* (the fallback path). A loop whose count is recovered in *closed form* by `MatchTrivialLoopTripCount` reports that count even if it exceeds 128 — but #25 then unrolls all of it, because it has no `InitialFeasibilityCheck`. In practice #25 is the small-loop path; the budgeted #112 is what protects against explosion on large counts.

> **QUIRK — `set_channel_id` per clone is mandatory for correctness, not cosmetic.** A collective's `channel_id` is the cross-replica rendezvous handle. If every unrolled copy kept the body's original id, the runtime would try to pair *N* distinct iteration-`i` collectives onto one handle and hang or mis-route. Drawing a fresh `NextChannelId` per clone is the same idiom #112's `UnrollSingleIterationOfTrivialLoop` uses and the direct inverse of #111's *reuse* policy (see [4.13](whileloop-collective-codemotion.md)).

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `xla::UnrollWhileLoop::Run` | `0x1f733a0` | 3010 B | scan + full-unroll-via-Call | CERTAIN |
| `xla::UnrollWhileLoop::name` | `0x1f72f00` | — | `"unroll-while-loop"` (len 17, `mov eax,0x11`) | CERTAIN |

The clone-wire data-flow (which params feed which Call) is **INFERRED** from the call/string anchors above; `hlo-opt` is `NVOPEN_IDA_SKIP_DECOMPILE`, so there is no Hex-Rays for the exact operand stitching. The call *set* and the names `"clone"`/`"callee"`/`"param"` are read from the binary; the wiring order is not.

---

## The Trip-Count Engine (shared by #25, #111, #112)

### Purpose

`xla/hlo/analysis/while_loop_analysis.cc`'s `ComputeWhileLoopTripCount` is the one analyser all three passes consult. It answers "how many times does this loop run, as a compile-time constant, or unknown?" Seven call sites reference it in this binary (incl. #25, #111, and the reduce-scatter-motion pass ×2). It is a pure-IR analysis: it reconstructs the induction variable, evaluates it with `HloEvaluator`, tries a closed-form match, and falls back to bounded simulation.

> **GOTCHA — it never reads `WhileLoopBackendConfig.known_trip_count`.** A reimplementer who assumes the trip count is pulled from a backend-config proto field is wrong. `ComputeWhileLoopTripCount` *always* recomputes from the IR. The `WhileLoopBackendConfig_KnownTripCount` proto and the separate `WhileLoopTripCountAnnotator` pass are a different (annotation) path and are **not** consulted by any pass on this page.

### Algorithm — `ComputeWhileLoopTripCount` @ `0x8aaee70`

```c
function ComputeWhileLoopTripCount(while_op, max):          // 0x8aaee70  -> optional<int64>
    idx = GetLoopInductionVarTupleIdx(while_op)             // 0x8aaa980 — which tuple slot is the indvar
    if idx < 0:                       return nullopt

    init = EvaluateIndvarInit(while_op, idx)                // 0x8aaaa20 — HloEvaluator on the init operand
        // VLOG "Getting trip count for loop "  @0x271c9c ; "indvar_init_result: " @0x24af26
    if init.failed():
        // VLOG "Couldn't evaluate induction variable init, " @0x2c2f38
        return nullopt

    // (1) Closed-form fast path:
    tc = MatchTrivialLoopTripCount(while_op, idx, init)     // 0x8aac420  (next section)
    if tc.has_value():
        // VLOG "Loop has static trip count of " @0x3faeb0
        return tc

    // (2) Bounded brute-force simulation fallback:
    eval = HloEvaluator()                                   // 0x8c1f9f0
    for n in 0 .. max:                                       // <-- the `max` arg caps THIS loop (#25 passes 128)
        if !eval.evaluate(while_condition):                 // fail -> "Couldn't evaluate while cond: " @0x30f1a8
            return nullopt
        if condition == false:        return n              // loop exits here -> trip count = n
        if !eval.evaluate(indvar_update):                   // fail -> "Couldn't evaluate induction variable update: " @0x3b4f70
            return nullopt
    // VLOG "Loop has unknown trip count." @0x22be47
    return nullopt                                          // hit the cap without exiting -> unknown
```

The parameter access is bounds-checked by `param_no < (int64)param_instructions_.size()` (string @ `0x37ba08`). Every quoted VLOG/diagnostic string above was read verbatim from `.rodata`.

*Anchors: the callee set plus every quoted string.*

### Algorithm — `MatchTrivialLoopTripCount` @ `0x8aac420` (closed form)

Pattern-matches the canonical counted loop `for (i = init; i < N; i += step)` (or `i <= N`) and returns `(N − init)/step` (`+1` for `<=`) in closed form, rejecting non-natural steps and `INT64_MAX` overflow. The match is a chain of XLA pattern matchers, each rejection carrying a verbatim string:

```c
function MatchTrivialLoopTripCount(while_op, idx, init):    // 0x8aac420
    // condition must be op(i, N) or op(N, i):
    if !Match(cond, Comparison(indvar, ConstantScalar)):    // HloConstantScalarImpl<int>, ComparisonDirectionImpl
        reject "while condition is not of the form op(i, N) or op(N, i)."  // @0x3853e8
    dir = comparison_direction                              // recognises:
        //  "loop condition is i < N: "  @0x331e80  /  "i <= N: " @0x29efd8
    // indvar must be updated by  add(i, const):
    if !is_add_update:        reject "...not getting updated by an add operation: "        // @0x331e28
    if !step_is_const:        reject "...not getting incremented by constant: "            // @0x2b5c80
    if step <= 0:             reject "trip count step is not a natural number: "           // @0x293978
    if !step_integral:        reject "...not an integral type: "                           // @0x391a88
    // init and N must be int64-representable constant scalars:
    if !init_is_scalar_int64: reject "induction variable init is not a constant scalar ... int64_t: "  // @0x379a28
    if !N_is_scalar_int64:    reject "induction variable is not a constant scalar ... int64_t."         // @0x349e78
    tc = (N - init) / step  (+1 if dir == LE)
    if tc > INT64_MAX:        reject "Trip count exceeds INT64_MAX."                        // @0x39d580
    return tc                                               // else "while condition follows unknown pattern: " @0x356298
```

Helper matchers, from the demangled callees: `LiteralUtil::LiteralAsScalarInt64`, `TraceThroughCopyAndGteTupleChain` (`0x8aa4930`), `GetUniqueGteInstruction` (`0x8ab18c0`), `ShapeUtil::TrueNumDimensions`, `NonConstantOperand`.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `xla::ComputeWhileLoopTripCount` | `0x8aaee70` | 3013 B | indvar → init eval → trivial → sim | CERTAIN |
| `xla::MatchTrivialLoopTripCount` | `0x8aac420` | 4619 B | closed-form `i<N`/`i<=N` matcher | CERTAIN |
| `xla::GetLoopInductionVarTupleIdx` | `0x8aaa980` | 122 B | find indvar tuple slot | CERTAIN |
| `xla::EvaluateIndvarInit` | `0x8aaaa20` | 270 B | `HloEvaluator` on indvar init | CERTAIN |
| `xla::ComputeWhileLoopTripCountUpperBound` | `0x8ab06c0` | — | bound-only sibling variant | HIGH |

---

## #112 — `xla::hilo::NeuronWhileLoopUnroller` (the budgeted XLA-port driver)

### Purpose

The full Neuron unroll pipeline: canonicalise the module so trip counts become visible, collect every unrollable loop with its analysis state and budget verdict, unroll each (full or partial), inline the per-iteration `Call`s, and stamp boundary markers. It is a faithful port of upstream XLA `WhileLoopUnroller` (source string `hilo/hlo_passes/neuron_while_loop_unroller.cc` @ `0x3f1940`); the Neuron-specific deltas are (1) the budget *defaults* in the constructor and (2) the `NeuronAddBoundaryMarker` step.

### Entry Point

```text
NeuronWhileLoopUnroller::Run (0x20013a0)
  ├─ PrepareModuleForUnrolling(module)                 (0x1ffb680)  ── ConstantSink + TupleSimplifier + CSE
  ├─ unroll_cfg = optional<UnrollConfig>{ this+0x18,+0x20,+0x28,+0x30,+0x32 }   ── ctor defaults
  ├─ GetUnrollableLoops(module, cfg)                   (0x1fff850)  ── vector<pair<while*, WhileLoopConfig>>
  │     ├─ IsLoopUnrollable(while)                     (0x1ff8880)  ── eligibility -> optional<WhileLoopConfig>
  │     └─ InitialFeasibilityCheck(while, cfg, ucfg)   (0x1fff130)  ── 3-gate budget  (only if ucfg present)
  ├─ EnvVarSetToOne("NEURON_DISABLE_BOUNDARY_MARKER")  (call 0x2001652)
  ├─ per loop:  [this+0x30] ? UnrollInternalWrappedAndReturnReplacement   (0x2000020)  ── partial/wrapped
  │                         : UnrollInternal                              (0x1ffea60)  ── full
  ├─ CallInliner::Run(module)                          (call 0x2001bb4)
  └─ if boundary: NeuronAddBoundaryMarker::Run(module) (call 0x20019df)   ── see 4.12
```

### Algorithm — `Run` @ `0x20013a0`

```c
function NeuronWhileLoopUnroller_Run(module, exec_threads):     // 0x20013a0
    PrepareModuleForUnrolling(module, exec_threads)             // 0x2001427  (canonicalise; §below)

    // Budget config assembled from the ctor-baked fields (§"Budget"):
    cfg = UnrollConfig{ max_trip_count       = this[0x18],      // read @0x20015de
                        max_total_instructions= this[0x20],
                        max_body_instructions = this[0x28],
                        wrap_in_trivial_loop  = this[0x30],
                        flag2 = this[0x31], flag3 = this[0x32] }

    loops = GetUnrollableLoops(module, exec_threads, optional{cfg})   // 0x200161b
        // VLOG "Number of while instructions in the module to unroll: " @0x28a8a8

    boundary = !EnvVarSetToOne("NEURON_DISABLE_BOUNDARY_MARKER")      // edi=@0x2959a0; call @0x2001652
        // VLOG "Boundary markers enabled" @0x25700b (str @0x25700b) / "...disabled" @0x28268f

    changed = false
    for (while_op, wcfg) in loops:
        if this[0x30] != 0:                                          // wrap_in_trivial_loop -> partial
            repl = UnrollInternalWrappedAndReturnReplacement(while_op, wcfg)   // 0x20016ab -> 0x2000020
            changed |= repl.ok()
        else:                                                        // full
            UnrollInternal(while_op, wcfg)                           // 0x200170a -> 0x1ffea60
            changed = true

    CallInliner::Run(module, exec_threads)                          // 0x2001bb4 — inline per-iteration Calls
    if boundary:
        NeuronAddBoundaryMarker::Run(module, exec_threads)          // 0x20019df — Neuron-only, see 4.12
    return StatusOr{changed}
```

> **NOTE — `wrap_in_trivial_loop` defaults to 1, so the *default* path is partial/wrapped.** Both flag and threshold immediates are ctor-baked; unless something clears `[this+0x30]`, every loop goes through `UnrollInternalWrappedAndReturnReplacement`, which keeps a trivial outer `while` and unrolls the body by a factor (partial unroll), *not* `UnrollInternal` (full straight-line). See §"Full vs Partial".

### Eligibility — `IsLoopUnrollable` @ `0x1ff8880` (4183 B)

Returns `optional<WhileLoopConfig>` (nullopt ⇒ not unrollable). This is upstream's `IsLoopUnrollableInternal`: a structural gate followed by a *trivial* (closed-form) trip-count requirement. Each rejection has a verbatim string:

```c
function IsLoopUnrollable(while_op):                            // 0x1ff8880
    CHECK(while_op.opcode() == kWhile)                          // CHECK str @0x358330
    if while_op.operands().size() != 1:                         // @0x393eb8
        reject "While loop must have a single tuple operand, instead has more than one operand: "  // @0x2d98f8
    if !operand.is_tuple():        reject " because the operand is not a tuple: "                   // @0x358358
    if condition.HasSideEffect():  reject "...condition contains side-effecting instructions: "     // @0x2efa10
        // HloComputation::HasSideEffect @0x9628730
    if body.ContainsSendRecv():    reject " because it contains a send/recv node: "                 // @0x34c238
        // hlo_query::ContainsInstrWithOpcode @0x8ab16b0 over a fixed opcode set
    if has_control_deps:           reject " due to control dependency: "                            // @0x21cdbf
    idx = GetLoopInductionVarTupleIdx(while_op)
    if idx < 0:                    reject "...could not be found."                                  // @0x363e28
    // The decisive gate: must have a *trivial* (closed-form) trip count, not just any:
    tc = MatchTrivialLoopTripCount(while_op, idx, init)         // 0x8aac420 (direct, NOT via ComputeWhileLoopTripCount)
    if !tc.has_value():            reject "Loop doesn't have trivial trip count"                    // @0x306518
        // VLOG "Loop trip count " @0x224bf5 / "Not attempting to unroll " @0x238574 / "Cannot unroll while loop " @0x214c77
    return WhileLoopConfig{ induction_var_tuple_idx=idx, init, step, trip_count=*tc }
```

> **GOTCHA — #112 requires a *trivial* trip count; #25 accepts the simulated one too.** `IsLoopUnrollable` calls `MatchTrivialLoopTripCount` **directly**, so a loop whose count is only recoverable by brute-force simulation (closed-form match fails) is **ineligible** for #112 but would still be unrolled by #25. The two passes therefore cover overlapping-but-different loop sets. The `WhileLoopConfig` it returns carries the trip count so `UnrollInternal` never recomputes.

### Budget — `InitialFeasibilityCheck` @ `0x1fff130` (1731 B)

Called from `GetUnrollableLoops` (@ `0x1fffa93`) **only when** the `optional<UnrollConfig>` is present (it always is for #112). Three reject gates; `[while_body+0x58]` is the body instruction count:

```c
function InitialFeasibilityCheck(while_op, wcfg, ucfg):        // 0x1fff130
    body_n = while_op.while_body()[0x58]
    if body_n > ucfg.max_body_instructions:                    // cmp [rax+58h],r13 ; jg  @0x1fff258
        reject "Cannot unroll while loop. Too many instructions in the body: "          // @0x2b82c0
    if wcfg.trip_count > ucfg.max_trip_count:                  // cmp [var],rbx ; jle keep  @0x1fff269
        reject "Cannot unroll while loop. The trip count is greater than the threshold: "  // @0x31cc40
    new_count = wcfg.trip_count * body_n                       // imul @0x1fff2af
    if new_count > ucfg.max_total_instructions:               // cmp ; jle keep  @0x1fff2b9
        reject "Not attempting to unroll due to instruction count increase explosion. New instruction count: "  // @0x3f1998
        // logged as "<new_count> vs <max>"  (" vs " @0x224c06)
    // VLOG "Trying to unroll " @0x220d21
    return true
```

### Budget Constants — factory `_M_invoke` @ `0x1e70200`

The registration factory constructs the pass (object size `0x38`) and writes the default `UnrollConfig`/flags inline. These are the Neuron tuning of the upstream defaults — **read directly off the constructor**:

| Field | Offset | Immediate | Value | Reject string it feeds |
|---|---|---|---|---|
| `max_trip_count` | `[obj+0x18]` | `0x3E8` | **1000** | "...trip count is greater than the threshold" |
| `max_total_instructions` | `[obj+0x20]` | `0xC3500` | **800000** | "...instruction count increase explosion" |
| `max_body_instructions` | `[obj+0x28]` | `0x186A0` | **100000** | "...Too many instructions in the body" |
| `wrap_in_trivial_loop` | `[obj+0x30]` | `1` (byte) | partial-vs-full select | — |
| flag2 | `[obj+0x31]` | `1` (byte) | force / unroll-factor [INFERRED] | — |
| flag3 | `[obj+0x32]` | `0` (byte) | [UNRESOLVED] | — |

```asm
0x1e7023e: mov qword ptr [rax+18h], 3E8h     ; max_trip_count        = 1000
0x1e70246: mov qword ptr [rax+20h], 0C3500h  ; max_total_instructions= 800000
0x1e7024e: mov qword ptr [rax+28h], 186A0h   ; max_body_instructions = 100000
```

> **QUIRK — the explosion gate is trip×body, not trip+body.** A loop with trip count 900 (under the 1000 cap) and a 1000-instruction body passes the trip-count gate and the body gate, but `900 × 1000 = 900000 > 800000` *rejects* on the explosion gate. The three thresholds are not independent ceilings; the third is a product that can veto a loop both of the others would admit. The two `[obj+0x08]`/`[obj+0x10]` fields (sourced from registrar `config->[0x1000]`/`[0x10C0]`) are **[UNRESOLVED]** — plausibly a run-budget/version plus a bool, but neither was pinned.

### Module Canonicalisation — `PrepareModuleForUnrolling` @ `0x1ffb680`

Runs three sub-passes in order so the induction variable becomes a recognisable body constant (which is what lets `MatchTrivialLoopTripCount` succeed):

```c
function PrepareModuleForUnrolling(module):                    // 0x1ffb680
    HloPassFix<WhileLoopConstantSinking, 25>::Run(module)      // sink loop-invariant consts into body
        // VLOG "Applied constant sinking to module " @0x334178
    TupleSimplifier(/*ctor bool*/)::Run(module)                // VLOG "Applied tuple simplifier to module " @0x2c2f68
    HloCSE::Run(module)                                        // VLOG "Applied hlo cse to module " @0x210aa6
```

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `NeuronWhileLoopUnroller::Run` | `0x20013a0` | 2691 B | driver: prepare → collect → unroll → inline → mark | CERTAIN |
| `NeuronWhileLoopUnroller::name` | `0x1ff62b0` | — | `"while_loop_unroller"` (len 19) | CERTAIN |
| `NeuronWhileLoopUnroller::IsLoopUnrollable` | `0x1ff8880` | 4183 B | eligibility → `optional<WhileLoopConfig>` | CERTAIN |
| `NeuronWhileLoopUnroller::GetUnrollableLoops` | `0x1fff850` | 1013 B | collect `(while, WhileLoopConfig)` pairs | CERTAIN |
| `NeuronWhileLoopUnroller::PrepareModuleForUnrolling` | `0x1ffb680` | 1307 B | ConstantSink + TupleSimplifier + CSE | CERTAIN |
| `(anon)::InitialFeasibilityCheck` | `0x1fff130` | 1731 B | 3-gate explosion budget | CERTAIN |
| `(anon)::UnrollInternal` | `0x1ffea60` | 1474 B | full straight-line unroll | CERTAIN |
| `(anon)::UnrollInternalWrappedAndReturnReplacement` | `0x2000020` | 4699 B | partial/wrapped unroll (default) | HIGH |
| `(anon)::UnrollSingleIterationOfTrivialLoop` | `0x1ffde80` | 2836 B | clone body + substitute indvar const | CERTAIN |
| `…NeuronWhileLoopUnroller` factory `_M_invoke` | `0x1e70200` | 112 B | ctor defaults (1000/800000/100000) | CERTAIN |

---

## Full vs Partial Unroll (#112)

`[this+0x30]` (`wrap_in_trivial_loop`, ctor default **1**) selects the rewrite per loop (branch at the per-loop dispatch `0x2001698` vs `0x200170a`).

### Full — `UnrollInternal` @ `0x1ffea60`

```c
function UnrollInternal(while_op, wcfg):                       // 0x1ffea60
    // VLOG "Unrolling while instruction " @0x24f66d " with body instruction count " @0x218f68
    for i in 0 .. wcfg.trip_count:                             // count from WhileLoopConfig — NOT recomputed
        iter = UnrollSingleIterationOfTrivialLoop(while_op, wcfg, i)   // 0x1ffde80
    FlattenCallGraph::Run(module)                             // de-nest the per-iteration Call comps
    CreateCall + AddInstruction + ReplaceInstruction(while_op, …)     // wire & replace
```

The while is fully replaced by `trip_count` straight-line iteration bodies (chained via `Call`, later inlined by the driver's `CallInliner::Run`).

### Partial / Wrapped — `UnrollInternalWrappedAndReturnReplacement` @ `0x2000020`

The larger variant (4699 B, 233 bb). It unrolls the body by an unroll-factor but **keeps a trivial outer `while`** that iterates `trip_count / factor` times — a partial unroll. It returns the replacement instruction (`StatusOr<HloInstruction*>`) so the driver can OR the change flag. Selected when `wrap_in_trivial_loop` is set; since the ctor default is 1, **this is the default path** unless the flag is cleared. The name, size, and dispatch byte are read from the binary; the exact factor arithmetic is **[INFERRED]**, not traced basic-block by basic-block because of the no-Hex-Rays constraint.

### Per-Iteration Substitution — `UnrollSingleIterationOfTrivialLoop` @ `0x1ffde80`

```c
function UnrollSingleIterationOfTrivialLoop(while_op, cfg, i):  // 0x1ffde80
    clone = while_op.while_body().Clone(...)                   // HloComputation::Clone
    // Replace the induction-var parameter with its iteration-i constant (init + i*step):
    clone.ReplaceOperandWith(cfg.induction_var_tuple_idx, const_i)
    // Same channel-renumber idiom as #25 §"Algorithm":
    for inst in clone:
        if IsOrHasCollectiveWithChannelId(inst):
            inst.set_channel_id(hlo_query::NextChannelId(module))   // fresh id per iteration
    return clone
```

*Anchors: the `Clone` / `ReplaceOperandWith` / `IsOrHasCollectiveWithChannelId` / `set_channel_id` / `NextChannelId` callees.* The `init + i*step` value derivation is **[INFERRED]** from the `WhileLoopConfig` fields.

---

## #111 — `NeuronRewriteAllGatherTripCount` (the all-gather trip-count rewrite)

### Purpose

An `OpExpander` (shares `OpExpanderPass::Run` @ `0x29f0bb0`) that recognises the "gather one slice per iteration" idiom inside a known-trip-count while body and replaces it with a single gather of the **full** trip-count-sized tensor, then re-derives the per-iteration view by dynamic-slice. It does not unroll; it changes *what gets gathered*. The direction is the dual of `NeuronMoveAllGatherWhileLoop` ([4.13](whileloop-collective-codemotion.md)): rather than moving the collective out of the body, it widens the gathered extent to the whole loop-carried tensor.

### Matcher — `InstructionMatchesPattern` @ `0x1fd7300` (2012 B)

Matched HLO shape:

```text
  all_gather( reshape( dynamic_slice( get_tuple_element( parameter ) ) ) )
        inside  while_body   with   ComputeWhileLoopTripCount(while) == known
```

```c
function InstructionMatchesPattern(inst):                      // 0x1fd7300
    if inst.opcode() != kAllGather:        return false        // entry opcode gate
    comp = inst.parent()
    while_op = comp.GetUniqueCaller(kWhile)                    // 0x1f861d0
    if !is_while_body:    reject "AllGather is not inside a while loop body computation"   // @0x2d9850
    if !while_op:         reject "Failed to get the while loop instruction"                // @0x3582d8
    if !ComputeWhileLoopTripCount(while_op, …).has_value():    // 0x8aaee70
        reject "While loop does not have a known trip count"                               // @0x340750
    // operand chain:
    if reshape.operand(0) != DynamicSlice:  reject "First operand of Reshape is not a DynamicSlice"      // @0x311898
    if ds.operand(0)      != GetTupleElement: reject "First operand of DynamicSlice is not a GetTupleElement" // @0x2ce968
    if gte.operand(0)     != Parameter:     reject "First operand of GetTupleElement is not a Parameter"  // @0x3064e0
    // dimension invariants (Shape::dimensions @0x1fd70d0 / @0x1f4fad0):
    if ds.dimensions(0) != trip_count:
        reject "Left-hand side dimension of the tensor being dynamic sliced does not match the trip count"  // @0x363ce8
    if reshape.rank() != ds.rank() - 1:
        reject "Reshape rank ( ... ) must be one less than the dynamic sliced rank ( ... )"  // @0x243e89 / @0x363d70
    if trailing_dims_mismatch:  reject "Dimension mismatch following the leading dimension"   // @0x3f18e0
        // VLOGs ": reshape dim=" @0x23fe90 , ", slice dim=" @0x26a7e8
    // success: VLOG "I matched everything" @0x220ce0
    return true
```

> **NOTE — the DS-leading-dim == trip-count invariant is shared with the reduce-scatter motion pass.** The string "...dynamic sliced does not match the trip count" is the same invariant `MatchReduceScatterPattern` enforces (see [4.13](whileloop-collective-codemotion.md)). Both passes key on "the loop-carried tensor's leading dimension is exactly the trip count", because that is what makes "this slice is iteration *i*'s view" provable.

### Rewrite — `ExpandInstruction` @ `0x1fd7cc0` (4217 B)

```c
function ExpandInstruction(inst):                              // 0x1fd7cc0
    ag = Cast<HloAllGatherInstruction>(inst)                   // r14 = ag
    // Read the all-gather config off its own fields (offsets confirmed by disasm loads):
    ag_dim        = ag[0x258]    // int64 all_gather_dimension    @0x1fd7e5b mov rax,[r14+258h]
    channel_id    = ag[0x48]     // the AG's own channel id       @0x1fd7ee7 mov rax,[r14+48h]
    constrain     = byte ag[0x250]                               // @0x1fd7f03 movzx r15d,byte[r14+250h]
    use_gdi       = byte ag[0x260]                               // @0x1fd7eeb movzx ecx,byte[r14+260h]
    replica_grps  = ag.replica_groups()  // CollectiveDeviceList @+0x208/+0x218  (@0x1fd7efa/0x1fd7ef3)

    // New gathered extent = replica-group span count * per-iteration dim:
    new_dim = group_count * per_iter_dim                        // imul rax,rdx @0x1fd7f6d
    new_shape = ShapeUtil::MakeValidatedShape(elem_type, new_dims)

    new_ag = HloInstruction::CreateAllGather(new_shape, {full_operand},
                 ag_dim, replica_grps, constrain,
                 /*optional<long>*/ channel_id,   // REUSED — not a fresh NextChannelId
                 use_gdi)                                       // @0x1fd7f8c
    // Re-derive iteration-i view from the full gather:
    zero   = CreateConstant(LiteralUtil::Zero(idx_type))
    new_ds = CreateDynamicSlice(slice_shape, new_ag, {idx…}, sizes)   // @0x1fd84c4
    new_rs = CreateReshape(reshape_shape, new_ds, /*inferred_dim*/)   // @0x1fd870e
    new_rs.metadata().CopyFrom(inst.metadata())                // preserve OpMetadata
    return new_rs                                              // OpExpanderPass::Run replaces inst with this
```

> **QUIRK — #111 *reuses* the original all-gather's `channel_id`; #25/#112 assign fresh ones.** The expander reads `[ag+0x48]` and threads it straight into `CreateAllGather`'s `std::optional<long>` channel-id parameter (call @ `0x1fd7f8c`). This is correct precisely *because* #111 does not multiply the collective into *N* copies the way the unrollers do — there is still exactly one all-gather, just over a bigger tensor, so the one rendezvous handle stays valid. A reimplementer who blindly "renumbers all unrolled/rewritten collectives" would break the trip-count rewrite. The `imul rax,0xCC…CD; sar` idiom at `0x1fd7f59`–`0x1fd7f6d` is the compiler's division of the replica-group span byte-length into a group count, then `× per-iteration dim` for the full extent. The field reads and create-calls are read off the disassembly; the new-dim arithmetic stitching is **[INFERRED]**.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `…NeuronRewriteAllGatherTripCount::InstructionMatchesPattern` | `0x1fd7300` | 2012 B | AG-in-known-trip-count-loop matcher | CERTAIN |
| `…NeuronRewriteAllGatherTripCount::ExpandInstruction` | `0x1fd7cc0` | 4217 B | rebuild AG over full trip count | CERTAIN |
| `…NeuronRewriteAllGatherTripCount::name` | `0x1fd7050` | — | `"aws_neuron_rewrite_all_gather_trip_count"` (40) | CERTAIN |

---

## Evidence summary

Where each central claim is anchored in the binary:

1. **#112's budget defaults are 1000 / 800000 / 100000.** The factory disasm `…RegisterNeuronWhileLoopUnroller…_M_invoke` @ `0x1e70200` writes the immediates off the constructor: `mov [rax+18h],3E8h` (1000), `mov [rax+20h],0C3500h` (800000), `mov [rax+28h],186A0h` (100000) at `0x1e7023e`/`0x1e70246`/`0x1e7024e`.

2. **#25 caps the trip-count engine at 128 and gates on kWhile.** `mov esi,80h` (@ `0x1f734a7`) precedes the `ComputeWhileLoopTripCount` call; `cmp byte ptr [r12+14h],79h` at `0x1f73458` and `0x1f7349f` is the `kWhile` opcode gate.

3. **#25 renumbers channels and replaces the while with a Call chain.** The disasm shows `NextChannelId` (@ `0x1f73a1d`), two `CreateCall` sites, `AddEmbeddedComputation`, and `ReplaceInstruction` (@ `0x1f73c45`) immediately followed by the `mov esi, offset aWhileOpParentReplaceinstruction…` CHECK string.

4. **#111 reuses the AG channel-id and reads gather-dim @+0x258.** Expander disasm: `mov rax,[r14+258h]` (ag_dim), `mov rax,[r14+48h]` (channel_id), `movzx …[r14+250h]` (constrain), `movzx …[r14+260h]` (use_gdi), then `CreateAllGather(…, std::optional<long>, bool)` @ `0x1fd7f8c` consuming the `[+0x48]` value. The signature's `std::optional<long>` channel arg is the reuse path.

5. **The two unrollers are distinct passes sharing only the analysis engine.** Distinct name strings (`unroll-while-loop` len 17 vs `while_loop_unroller` len 19, both present in the string table), distinct `Run` addresses, and only #112 carries `NEURON_DISABLE_BOUNDARY_MARKER` / `GetUnrollableLoops` / `CallInliner` / `NeuronAddBoundaryMarker` calls — #25 has none of these. Both call `ComputeWhileLoopTripCount` (@ `0x8aaee70`).

All 13 diagnostic/name string literals cited on this page were confirmed present (exactly once each, except `while_loop_unroller` twice) in the `hlo-opt` string table. Residual gaps, marked in place and all **[INFERRED]**: the partial-unroll factor arithmetic (`UnrollInternalWrappedAndReturnReplacement`), the `[obj+0x08]`/`[obj+0x10]` registrar fields, and the exact clone-wire data-flow in #25 / §"Per-Iteration Substitution" / #111 new-dim arithmetic (no Hex-Rays for any of them).

---

## Reconstructed Signatures

```cpp
namespace xla {
// while_loop_analysis.cc — shared engine
std::optional<int64_t> ComputeWhileLoopTripCount(const HloInstruction* while_op, int64_t max);   // 0x8aaee70
std::optional<int64_t> MatchTrivialLoopTripCount(const HloInstruction* while_op, int64_t idx,
                                                 const Literal& init);                            // 0x8aac420
StatusOr<bool> UnrollWhileLoop::Run(HloModule*, const flat_hash_set<string_view>&);               // 0x1f733a0  (#25)

namespace hilo {
// neuron_while_loop_unroller.cc — port of xla::WhileLoopUnroller  (#112)
struct WhileLoopConfig { int64_t induction_var_tuple_idx; int64_t init; int64_t step; int64_t trip_count; /*…*/ };
struct UnrollConfig    { int64_t max_trip_count        /*+0x18 = 1000*/;
                         int64_t max_total_instructions /*+0x20 = 800000*/;
                         int64_t max_body_instructions  /*+0x28 = 100000*/;
                         bool    wrap_in_trivial_loop   /*+0x30 = 1*/;
                         bool /*+0x31 = 1*/; bool /*+0x32 = 0*/; };
StatusOr<bool> NeuronWhileLoopUnroller::Run(HloModule*, const flat_hash_set<string_view>&);        // 0x20013a0
std::optional<WhileLoopConfig> NeuronWhileLoopUnroller::IsLoopUnrollable(HloInstruction*);         // 0x1ff8880

// neuron_rewrite_all_gather_trip_count.cc — OpExpander  (#111)
bool NeuronRewriteAllGatherTripCount::InstructionMatchesPattern(HloInstruction*);                  // 0x1fd7300
StatusOr<HloInstruction*> NeuronRewriteAllGatherTripCount::ExpandInstruction(HloInstruction*);     // 0x1fd7cc0
} // namespace hilo
} // namespace xla
```

`UnrollConfig` field order is read off the constructor (the three int thresholds at `+0x18/+0x20/+0x28`, three bool flags at `+0x30/+0x31/+0x32`). `WhileLoopConfig` field *names* come from the demangled analysis symbols; its exact offsets are **[INFERRED]** — the pair is passed by value through `GetUnrollableLoops` and carries the trip count `UnrollInternal` consumes.

---

## Related Components

| Name | Relationship |
|---|---|
| `NeuronAddBoundaryMarker` (Run @ `0x2002370`) | #112's final cleanup step; stamps boundary markers around the unrolled region — see [4.12](boundary-markers-layer-cut.md) |
| `NeuronMoveAllGatherWhileLoop` / `MoveReduceScatterWhileLoop` | collective-motion duals of #111; share the DS-leading-dim==trip-count invariant — see [4.13](whileloop-collective-codemotion.md) |
| `WhileLoopTripCountAnnotator` (@ `0x2731e94`) | the *annotation* path (`WhileLoopBackendConfig.known_trip_count`); **not** consulted by any pass here |
| `CallInliner` | inlines the per-iteration `Call` computations both unrollers emit |
| Backend `full_unroll` (libwalrus) | the separate later-stage full unroll — out of scope here (HLO-level only) |

## Cross-References

- [Collective Motion across While Loops](whileloop-collective-codemotion.md) — 4.13, the all-gather/reduce-scatter motion duals of #111's trip-count rewrite
- [Boundary Markers](boundary-markers-layer-cut.md) — 4.12, the `NeuronAddBoundaryMarker` step #112 runs after unrolling
- [Collective Stream-ID & Channel-ID Family](collective-stream-channel-id.md) — `hlo_query::NextChannelId` and the channel-id uniqueness model the unrollers feed
- [The hlo-opt Pass Registry](pass-registry.md) — registration orders #25 / #111 / #112 and the `--passes` keys
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where the `Frontend` (`hlo-opt`) job sits in the descent
