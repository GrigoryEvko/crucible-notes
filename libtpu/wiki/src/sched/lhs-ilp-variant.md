# LHS: ILP Variant

> *Addresses and offsets apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`libtpu.so` ships a class literally named for an integer-linear-programming schedule — `xla::ILPMemoryScheduler` — together with a flag family named `xla_tpu_enable_ilp_latency_hiding_scheduler` and a six-field protobuf named `xla::jellyfish::IlpLatencyHidingSchedulerOptions`. A reader expecting "the ILP variant of the LatencyHidingScheduler" to be a single solver-backed replacement for the greedy list scheduler will be misled. Static analysis shows the name covers **two structurally unrelated code paths**, gated by the same flag family, and the one that actually contains an ILP formulation is **dead code** in this build.

The first path is a per-computation memory-scheduling MIP — `xla::ILPMemoryScheduler::Run` (`@0x10acd020`) delegating to the worker `ILPMemorySchedulerForComputation::Run` (`@0x10acdf00`). This is a genuine integer program: it builds an `operations_research::math_opt::Model`, declares binary placement/liveness variables and one continuous `peak_memory` variable, emits eleven linear-constraint families, minimizes `peak_memory`, and solves it through OR-Tools `math_opt::Solve` with `SolverType = CP_SAT (4)`. It is fully implemented but **unreachable**: its vtable (`@0x217fa460`) has zero code consumers and the `MemorySchedulerProto::Value` enum dispatched by `xla::GetMemorySchedulerAlgorithm` (`@0x10abd6a0`) has **no `ILP` value** — case 4 is taken by `BrkgaMemoryScheduler`.

The second path is **live** but contains no ILP: inside the canonical scheduler bring-up `RunHloScheduler` (`@0x1096fac0`), `xla::jellyfish::EnableIlpLatencyHidingScheduler` (`@0x1d6b7e00`) selects between two async-op classifier lambdas. When enabled, the wider lambda `$_1` exposes more instruction kinds as async-overlap candidates to the **canonical** `xla::LatencyHidingScheduler` — which is the pass that runs in both cases. The "ILP-LHS" flag, as wired in 0.0.40, means "expose more async candidates," not "switch to an ILP scheduler."

For reimplementation, the contract is:

- The `xla::ILPMemoryScheduler` object layout (112 bytes) and its hard-coded `≤ 8 HloValues` fallback to `xla::BacktrackScheduler`, gated additionally by the `use_backtrack_fallback` flag at object offset +104.
- The full ILP model: three binary variable families (`I_`, `Vs_`, `Ve_`) plus continuous `peak_memory`, eleven `AddLinearConstraint` families, the single-term `min peak_memory` objective, and the CP-SAT solve with default (unbounded) `SolveParameters`.
- The dispatcher gap: `GetMemorySchedulerAlgorithm` has no `ILP` case, so the MIP path is constructed nowhere.
- The `EnableIlpLatencyHidingScheduler` gate inside `RunHloScheduler` and the `$_1` vs `$_2` classifier semantics.
- The `IlpLatencyHidingSchedulerOptions` proto: which of its six fields is read (one) and which five are inert.

| | |
|---|---|
| **MIP outer Run** | `xla::ILPMemoryScheduler::Run(HloComputation*, ...)` @ `0x10acd020` |
| **MIP worker Run** | `(anon)::ILPMemorySchedulerForComputation::Run` @ `0x10acdf00` |
| **MIP vtable / consumers** | `0x217fa460` — relocation only, **zero** code references |
| **Dispatcher** | `xla::GetMemorySchedulerAlgorithm` @ `0x10abd6a0` — no `ILP` enum case |
| **Live gate** | `xla::jellyfish::EnableIlpLatencyHidingScheduler` @ `0x1d6b7e00` |
| **Gate caller** | `(anon)::RunHloScheduler` @ `0x1096fac0` (gate at line 1084) |
| **Async classifiers** | `$_1` (ILP-on) @ `0x10977140`, `$_2` (regular) @ `0x10977420` |
| **Options proto** | `xla::jellyfish::IlpLatencyHidingSchedulerOptions` (6 fields, 1 read) |
| **Solver backend** | `math_opt::Solve` @ `0x10af9d20`, `SolverType = CP_SAT (4)` only |

---

## The Two Paths Behind One Name

The flag family `xla_tpu_enable_ilp_latency_hiding_scheduler` does not select a scheduler. Tracing every reference to the symbols carrying the string `ILP` and `IlpLatencyHiding` lands on two disjoint subsystems:

```text
xla_tpu_enable_ilp_latency_hiding_scheduler  (+ IlpLatencyHidingSchedulerOptions)
   │
   ├── (A) LIVE  : async-op classifier switch in canonical LHS
   │       EnableIlpLatencyHidingScheduler(env) @0x1d6b7e00
   │          └─ RunHloScheduler @0x1096fac0  →  picks lambda $_1 vs $_2
   │                └─ AddPass<xla::LatencyHidingScheduler>   (canonical, unchanged)
   │
   └── (B) DEAD  : per-computation memory-scheduling MIP
           xla::ILPMemoryScheduler::Run @0x10acd020
              └─ ILPMemorySchedulerForComputation::Run @0x10acdf00
                    └─ math_opt::Solve(..., CP_SAT, ...) @0x10af9d20
           reached only via MemorySchedulerProto::Value::ILP  — which does NOT exist
```

The two paths share no state, no cost model, and no entry point. Path (B) is the only one that contains an integer program; path (A) is the only one that runs. Naming both "ILP latency-hiding scheduler" is a build-time artifact of the upstream source: the proto field that switches path (A) lives in the same options message as the (inert) knobs that would tune path (B).

> **GOTCHA —** the `IlpLatencyHidingSchedulerOptions` proto is a single message whose *only* live consumer is path (A)'s classifier switch. Five of its six fields (`max_solver_deterministic_time`, `computation_size_threshold`, `use_ilp_schedule_sequence`, `also_minimize_total_lifetime`, `min_compute_latency`) describe path (B)'s solver, but path (B) never reads them — and path (B) is itself unreachable. A reimplementer wiring the options proto to a CP-SAT solver would reproduce nothing the binary actually executes.

---

## Path (B): the `ILPMemoryScheduler` MIP

### Class hierarchy and dispatcher gap

`xla::ILPMemoryScheduler` is a subclass of `xla::ComputationSchedulerAlgorithm`, the same base shared by every memory scheduler. Its deleting destructor (`@0x10ad6900`) resets the base vptr to the `ComputationSchedulerAlgorithm` vtable, confirming the inheritance. The base is selected by an enum dispatch in `xla::GetMemorySchedulerAlgorithm` (`@0x10abd6a0`), which switches on the `MemorySchedulerProto::Value` stored at `env + 268`:

```c
// sub_10ABD6A0 — verbatim switch structure
switch ( *(_DWORD *)(a1 + 268) ) {
  case 0: ...  // DEFAULT       → xla::DefaultMemoryScheduler
  case 1: ...  // LIST          → xla::ListMemoryScheduler
  case 2: ...  // DFS           → xla::DFSMemoryScheduler
  case 3: ...  // POST_ORDER    → xla::PostOrderScheduler
  case 4: ...  // BRKGA         → xla::BrkgaMemoryScheduler   (vptr off_217FA2C0)
  case 5: ...  // BACKTRACKING  → xla::BFScheduler
  case 6: ...  // RandomOrderScheduler (no proto string)
  case 7: ...  // BacktrackMemoryScheduler
  case 8: ...  // BRUTE_FORCE   → xla::BruteForceMemoryScheduler
  case 9: ...  // LOCAL_ORDER   → xla::LocalOrderScheduler
  default:
    LogMessage("hlo_scheduling_selector.cc", 62);
    CopyToEncodedBuffer("Unexpected memory scheduler: ", 29);
    return make_unique<xla::DefaultMemoryScheduler>(...);   // fallback
}
```

There are cases `0..9`; there is no case that constructs `xla::ILPMemoryScheduler`. The default branch logs *"Unexpected memory scheduler: "* at `hlo_scheduling_selector.cc:62` and falls back to `DefaultMemoryScheduler`.

> **NOTE —** case 4 (`BRKGA`) builds a 112-byte object with the **identical layout** to `ILPMemoryScheduler` — base vptr `off_21CF7F08`, `AliasInfo*` at +8, the size-function `AnyInvocable` at +16/+64, an empty post-process `std::function` hook at +88/+96, and a bool at +104 — then patches the final vptr to `off_217FA2C0`. The two schedulers are siblings sharing one storage shape; only the vptr and the dispatcher wiring differ. The `ILP` scheduler simply has no slot to be built from.

A file-wide search for the three absolute addresses of the `ILPMemoryScheduler` vtable region (`0x217fa460` vptr, `0x217fa468` typeinfo, `0x217fa470` first virtual slot) finds **one occurrence each — the relocation entry only, no code reference**. The class is constructed nowhere in this build.

### Object layout and the hard fallback

The outer `Run` (`@0x10acd020`, ~861 lines) has the reconstructed signature:

```cpp
StatusOr<HloInstructionSequence>
xla::ILPMemoryScheduler::Run(HloComputation* comp,
                             TuplePointsToAnalysis const& pts,
                             HloAliasAnalysis const& alias) const;
```

| Offset | Field (inferred) | Confidence |
|-------:|------------------|------------|
| 0 | vptr → `ILPMemoryScheduler` vtable (`0x217fa460`) | CERTAIN |
| 8 | `AliasInfo const*` | HIGH |
| 16 / 64 | size-function `AnyInvocable<int64_t(BufferValue const&)>` (RAII storage + live callable) | HIGH |
| 48 | bool — alive marker for the inlined `AnyInvocable` | MEDIUM |
| 88 / 96 | post-process `std::function<HloInstructionSequence(...)>` hook (empty by default) | HIGH |
| 104 | bool — `use_backtrack_fallback` | CERTAIN |

The fallback test is byte-exact in the decompile (outer Run line 122):

```c
// a3 = TuplePointsToAnalysis&;  a3+88 = values().size()
// a2 = this;  a2+104 = use_backtrack_fallback
if ( *(__int64 *)(a3 + 88) <= 8 && *(_BYTE *)(a2 + 104) )
    return xla::BacktrackScheduler::Run(this, &fallback_state, ...);   // greedy
```

The ILP path is taken only when **(number of HloValues > 8) OR (`use_backtrack_fallback` is clear)**. The threshold `8` is **hard-coded** — it is *not* driven by the proto field `computation_size_threshold`, which exists but is never read. For graphs above 8 values, or with the fallback toggle cleared, the MIP runs unconditionally with no problem-size guard.

> **QUIRK —** the `use_backtrack_fallback` member (offset +104) is the only bool that gates the fallback, and the comparison operand `8` is a literal in the instruction stream. The options-proto field that was clearly *intended* to drive this (`computation_size_threshold`, `int64`) is inert. A reimplementation reading `computation_size_threshold` to size the fallback would diverge from the binary, which ignores it.

### Pre-solve model construction (outer Run)

```c
// outer Run, line 168
operations_research::math_opt::Model::Model(&model, "ilp_memory_scheduling", 21);
// line 175 — the single continuous variable
ModelStorage::AddVariable(model_storage,
                          /*is_int=*/0, "peak_memory", /*name_len=*/11,
                          /*lb=*/0.0, /*ub=*/+inf);
// ... walk HloAliasAnalysis::buffers(), group HloValues by defining-instruction id
//     into absl::btree_map<HloValue const*, vector<HloInstruction*>, OrderHloValuesById>,
//     keeping only values whose defining instruction lives in *this* computation ...
// line 788 — delegate
ILPMemorySchedulerForComputation::Run(worker);
```

The model name string `"ilp_memory_scheduling"` (length 21) and the continuous-variable name `"peak_memory"` (length 11, `is_int = 0`) are both literal in the decompile. The size of each `HloValue` is priced by the user-supplied `AnyInvocable<int64_t(BufferValue const&)>` at offset 64 — there is no `HighWaterMark`-style streaming estimator; the peak is recovered from the MIP solution itself.

---

## The ILP Formulation (worker Run)

The worker `ILPMemorySchedulerForComputation::Run` (`@0x10acdf00`, ~6764 lines) builds and solves the model. All anchors below are byte-verified against the decompile; source-line numbers refer to `platforms/xla/service/jellyfish/hlo_scheduling/memory_schedulers.cc`, recovered from `LogMessage`/`AddSourceLocationImpl` call sites.

### Decision variables

All three families are binary, built via `ModelStorage::AddVariable(model, /*is_int=*/1, name, name_len, /*lb=*/0, /*ub=*/1)`. The continuous `peak_memory` (built in the outer Run, `is_int = 0`) is the fourth.

| Family | Name pattern | Decompile line | Indexed over | Meaning | Confidence |
|--------|--------------|---------------|--------------|---------|------------|
| `I_` | `"I_<inst>,<slot>"` | 817 / 843 | (instruction id, slot `0..N-1`) | `I_i,t = 1` iff instruction `i` is placed at slot `t` | CERTAIN |
| `Vs_` | `"Vs_<value>,<slot>"` | 1685 / 1710 | (HloValue id, slot `0..N-1`) | `Vs_v,t = 1` iff value `v` has *started being live* by slot `t` | HIGH |
| `Ve_` | `"Ve_<value>,<slot>"` | 1807 / 1833 | (HloValue id, slot `0..N-1`) | `Ve_v,t = 1` iff value `v` has *ended* by slot `t` | HIGH |
| — | `peak_memory` | outer Run 175 | scalar | continuous; lower-bounded by every per-slot live total | CERTAIN |

The prefix string literals `"I_"`, `"Vs_"`, `"Ve_"` are emitted by `absl::StrCat` with `absl::numbers_internal::FastIntToBuffer` for the index and slot. Variable-to-`Variable` maps are `absl::flat_hash_map<std::pair<HloInstruction*,int>, math_opt::Variable>` for instructions and `flat_hash_map<std::pair<long,int>, math_opt::Variable>` for values. `Vs_`/`Ve_` model liveness as monotone step functions (the cumulative "started by" / "ended by" indicators), which is what lets a per-slot memory sum be written as a linear expression.

### Constraints

There are exactly **eleven** `Model::AddLinearConstraint` call sites in the worker, at the byte-verified lines below. The functional grouping is inferred from the surrounding loop structure and the variable family each site iterates; the logical statements are reconstruction, not literal coefficient recovery.

| # | Line | Family | Logical statement | Confidence |
|---|------|--------|-------------------|------------|
| 1 | 1362 | per-instruction | `Σ_t I_i,t == 1` — every instruction in exactly one slot | HIGH |
| 2 | 1573 | per-slot | `Σ_i I_i,t == 1` — every slot holds exactly one instruction | HIGH |
| 3 | 2252 | start init | `Vs_v,0 == I_def(v),0` — start CDF seeded by defining instruction | MEDIUM |
| 4 | 2584 | start step | `Vs_v,t − Vs_v,t-1 == I_def(v),t` — monotone start stepped at defining slot | MEDIUM |
| 5 | 2952 | start coupling | a user of `v` cannot run before `v` starts | MEDIUM |
| 6 | 3356 | end step | `Ve_v,t − Ve_v,t-1 ≥ I_last_use(v),t` — end CDF stepped at last-use slot | MEDIUM |
| 7 | 3583 | end ≤ start | `Ve_v,t ≤ Vs_v,t` — a value cannot end before it starts | MEDIUM |
| 8 | 4084 | precedence (data) | `Σ_{t'≤t} I_pred,t' ≥ I_succ,(t+1)` for each HLO data edge | MEDIUM |
| 9 | 4483 | precedence (control) | same shape for control dependencies | MEDIUM |
| 10 | 4887 | per-slot memory | `Σ_v size(v)·(Vs_v,t − Ve_v,t) ≤ peak_memory` for each slot `t` | MEDIUM |
| 11 | 5491 | objective coupling | closes `peak_memory` against the bounded per-slot expression | MEDIUM |

Sites 1–2 (the `I_` placement constraints) are the assignment-problem core — a permutation of instructions onto slots. Sites 3–5 iterate the `Vs_` maps and sites 6–7 the `Ve_` maps (both two-loop: outer HloValue, inner slot). Sites 8–9 iterate the `OrderHloValuesById`-grouped predecessor/user map. Site 10 is the liveness-to-memory reduction.

> **NOTE —** the exact coefficient vectors and RHS values inside each `LinearExpression` constructor are not recovered: the constructors are inlined and stamped with anonymous `flat_hash_map<Variable, double>` arguments that require per-call symbolic inspection. The variable *families*, the *count* (eleven), and the call-site *line numbers* are CERTAIN; the precise algebra of each constraint is the MEDIUM-confidence reconstruction above.

### Objective

The objective is a single continuous variable, minimized (worker Run lines 5567–5573):

```c
v1196 = 0x3FF0000000000000LL;        // IEEE-754 double 1.0  — peak_memory coefficient
LinearExpression::LinearExpression(expr, /*terms=*/{peak_memory}, /*n=*/1, /*offset=*/0.0);
Model::SetObjective(model, expr, /*maximize=*/0);   // 0 == minimize
```

`min peak_memory`, coefficient `1.0`, offset `0.0`, `maximize = false`. There is **no** secondary objective: the proto field `also_minimize_total_lifetime` is never read.

### Logging

The worker emits a one-shot model summary at `memory_schedulers.cc:283`:

```text
Computation: <name>
Number of instructions: <N>
Number of variables: <model.Variables().size()>
Number of constraints: <model.LinearConstraints().size()>
Number of values related to computation: <values.size()>
```

The string literals (`"Computation: "` len 13, `"Number of variables: "` len 21, `"Number of constraints: "` len 23, `"Number of values related to computation: "` len 41) are byte-verified at worker lines 5594/5606/5611/5616. On a successful solve it logs `"Peak memory: "` at line 300; at `VLOG ≥ 2` it dumps per-instruction `" scheduled at "` (line 423) and per-value `Value defined at ... with size : ...` (line 454).

---

## Solver Backend, Parameters, Termination

### CP-SAT is the only backend

`Solve` dispatches through `math_opt::Solver::New` (`@0x10b259a0`), which validates the init args and model, then calls `AllSolversRegistry::Create(registry, solver_type, model, init_args)`. The registry is populated exactly once, at static init in `_GLOBAL__sub_I_cp_sat_solver.cc` (`@0x212ca300`):

```c
v4[0] = operations_research::math_opt::CpSatSolver::New;
operations_research::math_opt::AllSolversRegistry::Register(registry, /*solver_type=*/4u, v4);
```

`AllSolversRegistry::Register` (`@0x10b273c0`) has this single caller. `SolverType = 4` is `SOLVER_TYPE_CP_SAT`. Every other `SolverTypeProto` value resolves to a missing registry entry and a failed status. The MIP's solve site passes `4` literally (worker line 5833):

```c
operations_research::math_opt::Solve(result, model, /*solver_type=*/4, args, init_args);
```

### Parameters: no bounds applied

The `SolveArguments` block constructed before the solve (worker lines ~5780–5832) is zero-initialized: empty message-callback `std::function`, empty solver-callback `std::function`, null `SolveInterrupter`. The `SolveParametersProto` is left at defaults — `enable_output = false` (raised only under `VLOG`), unbounded time, no iteration/node limit. The per-solver parameter structs (`SatParameters`, `GScipParameters`, `GlopParameters`, etc.) are constructed but only the CP-SAT `SatParameters` is consumed, and it is left at defaults.

> **GOTCHA —** the flag `xla_tpu_ilp_scheduler_max_solver_deterministic_time` (data `@0x223c1db0`, registered in `tpu_compilation_environment.cc`) and the proto field `max_solver_deterministic_time` both have a stored value, but **neither is read** in the worker Run. The `SatParameters` deterministic-time and wall-time limits stay at their defaults (CP-SAT default: no limit). The solver runs to completion or to a hard failure — there is no convergence bound.

### Termination

```c
SolveResult r = Solve(...);
if ( !r.ok() )                                  // → memory_schedulers.cc:297
    return r.status().AddSourceLocation(...);
if ( !r->termination.EnsureIsOptimalOrFeasible().ok() )   // → memory_schedulers.cc:298
    return ...;
// success: read peak_memory and every I_i,t via SolveResult::variable_values()
```

`EnsureIsOptimalOrFeasible` (worker line 5872) accepts both `OPTIMAL` and `FEASIBLE` — any feasible primal is consumed even if non-optimal. `INFEASIBLE`, `UNBOUNDED`, `IMPRECISE`, and `NO_SOLUTION_FOUND` all convert to a failed `absl::Status` at source line 298. The schedule is then recovered by `RetrieveSchedule`: for every recorded `(instruction, slot)` pair, read the variable's optimal value, build `std::pair<long /*slot*/, HloInstruction*>`, and `std::__stable_sort` (instantiations `@0x10adf140` / `@0x10adf3a0` / `@0x10adf5e0`) by slot to emit the final `HloInstructionSequence`.

---

## Path (A): the live async-op classifier switch

The only caller of `EnableIlpLatencyHidingScheduler` (`@0x1d6b7e00`) is `RunHloScheduler` (`@0x1096fac0`). The gate is at decompile line 1084:

```c
if ( EnableIlpLatencyHidingScheduler(env) ) {
    is_async_pred = &RunTensorCoreAsyncOpScheduler::$_1;   // 0x10977140 — wider
} else {
    is_async_pred = &RunTensorCoreAsyncOpScheduler::$_2;   // 0x10977420 — narrower
    use_full_kind_set = EnableSchedulingAnnotationPropagation(env);   // line 1113
}
// both paths land here:
pipeline.AddPass<xla::LegalizeSchedulingAnnotations>(cfg);   // line 1136
pipeline.AddPass<xla::LatencyHidingScheduler>(ctx, core);    // line 1137 — canonical
pipeline.AddPass<xla::ConstantDeferring>();                  // line 1138
```

The canonical `xla::LatencyHidingScheduler` runs in **both** cases (line 1137). The gate is *ahead* of the `AddPass`, so the scheduler pass itself cannot tell which classifier was used — only the async-candidate set it receives changes. See [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md).

### Classifier `$_1` (ILP-on, `@0x10977140`)

Byte-verified from the decompile:

```c
// is_async($_1)(inst):
attr = inst->get_frontend_attribute("keep_original_sequence_order_in_group", 37);
if ( attr.present && attr.value_len == 4 && *(uint32*)attr.value == 1702195828 )  // "true"
    return true;                       // honour user-pinned ordering
return inst->IsOutputFusion()
    || inst->IsLoopFusion()
    || inst->opcode() == 0x82
    || inst->IsCustomCall("tpu_custom_call")
    || inst->IsCustomFusion()
    || inst->opcode() == 40;           // kDot
```

The literal `1702195828` is the little-endian dword for the ASCII bytes `"true"`; the value-length guard (4) is the comparison against the 4-byte string region. The 37-char attribute name is interned.

### Classifier `$_2` (regular, `@0x10977420`)

```c
// is_async($_2)(inst):
return inst->IsOutputFusion()
    || inst->IsCustomCall("tpu_custom_call")
    || inst->IsCustomFusion()
    || inst->opcode() == 40;           // kDot
```

The delta is exactly: `$_1` additionally treats `IsLoopFusion` and opcode `0x82` as async, and honours the `keep_original_sequence_order_in_group="true"` pin. The ILP-LHS flag therefore widens the async-overlap surface the canonical scheduler sees — which changes the recurrence latencies `DefaultSchedulerCore` works with — but does not change the scheduling algorithm. The cost model is the regular LHS cost model (`xla::DefaultSchedulerCore` + `xla::LatencyEstimator`); see [Scheduler Overview](overview.md) and [Cost Model Overview](../cost/overview.md).

---

## The `IlpLatencyHidingSchedulerOptions` Proto

The message is registered, parseable from a flag, and reachable through `GetIlpLatencyHidingSchedulerOptions` (`@0x1d6b7e60`). Its `Clear` (`@0x1db24ea0`) zeroes a 6-bit presence bitmap at struct offset +16 (`& 0x3F`) and a payload region spanning offsets `0x18..0x32`; the `enable_ilp_latency_hiding_scheduler` bool sits at offset +48 (`0x30`), inside that payload.

```proto
message IlpLatencyHidingSchedulerOptions {
  optional bool   enable_ilp_latency_hiding_scheduler = 1;  // C++ struct +48 — LIVE
  optional double max_solver_deterministic_time      = 2;   // inert
  optional int64  computation_size_threshold         = 3;   // inert
  optional bool   use_ilp_schedule_sequence          = 4;   // inert
  optional bool   also_minimize_total_lifetime       = 5;   // inert
  optional double min_compute_latency                = 6;   // inert
}
```

The live consumer reads only the bool at +48 (gate `@0x1d6b7e00`):

```c
bool EnableIlpLatencyHidingScheduler(env) {
    auto opts = GetIlpLatencyHidingSchedulerOptions(env);
    char field48 = ((char*)&opts)[48];               // enable_ilp_latency_hiding_scheduler
    ~opts.~IlpLatencyHidingSchedulerOptions();
    if ( field48 ) return true;                       // proto override on
    AutoOr<bool> f = AutoOr<bool>::FromProtoOrDie(env + 1608);  // else fall back to the flag
    return (~f & 0x101) == 0;
}
```

Either the proto override or the `xla_tpu_enable_ilp_latency_hiding_scheduler` absl flag (data `@0x223c1d50`) switches the classifier to `$_1`. The `(~v & 0x101) == 0` idiom is the `AutoOr<bool>` unwrap (the value bit and the present bit both set).

| Field | Read in 0.0.40? | Effect | Confidence |
|-------|-----------------|--------|------------|
| `enable_ilp_latency_hiding_scheduler` (+48) | YES | switches the canonical-LHS async classifier `$_2 → $_1` | CERTAIN |
| `max_solver_deterministic_time` | no | none — `SatParameters` left at defaults | HIGH |
| `computation_size_threshold` | no | none — fallback threshold is hard-coded `8` | HIGH |
| `use_ilp_schedule_sequence` | no | none | HIGH |
| `also_minimize_total_lifetime` | no | none — single-term objective | HIGH |
| `min_compute_latency` | no | none | HIGH |

The five inert fields appear only in generated reflection code (`_table_` `@0x21cfa308`, `Clear`, `MergeImpl`, `_InternalSerialize`, `ByteSizeLong`, `CopyFrom`, `InternalSwap`, `GetMetadata`, `GetClassData`). No `set_*`/`get_*` accessor is called from any non-generated function.

### Flags

| Flag (data symbol) | Reachable consumer in 0.0.40 |
|--------------------|------------------------------|
| `FLAGS_xla_tpu_enable_ilp_latency_hiding_scheduler` (`@0x223c1d50`) | `EnableIlpLatencyHidingScheduler` (via `AutoOr<bool>`) |
| `FLAGS_xla_tpu_ilp_latency_hiding_scheduler_options` (`@0x223c4750`) | `GetIlpLatencyHidingSchedulerOptions` (via `AutoOr<message>`) |
| `FLAGS_xla_tpu_ilp_scheduler_max_solver_deterministic_time` (`@0x223c1db0`) | **none** — registered but unread |

---

## When Does It Replace the Greedy List Scheduler?

Direct answer for the reimplementer: in `libtpu-0.0.40`, **never as an ILP scheduler**.

- The MIP path (B) would replace the greedy memory scheduler only if `MemorySchedulerProto::Value::ILP` were dispatched by `GetMemorySchedulerAlgorithm`. That enum value does not exist; the class is constructed nowhere; its vtable has zero consumers. The greedy `BacktrackScheduler` is reachable from path (B) only as the `≤ 8 HloValues` fallback, and path (B) is itself unreachable.
- The live path (A) does **not** replace the list scheduler at all. It keeps the canonical `xla::LatencyHidingScheduler` pass and only widens the async-candidate set fed to it. The greedy `DefaultSchedulerCore` walk is unchanged; only its input changes.

The MIP code is preserved verbatim in the binary — `Model`, eleven constraints, `min peak_memory`, CP-SAT solve — ready to be re-wired the moment an `ILP` enum value is added to the dispatcher. Until then it is dead weight that documents Google's intended ILP memory scheduler without ever running it.

> **NOTE —** `xla::ILPMemoryScheduler` is the only memory-scheduling MIP in `libtpu.so` and one of only three OR-Tools `math_opt::Solve` consumers (the others being auto-sharding's `FormulateAndSolveMIPFromProblem`, which is live, and the MSA ILP pass, which is also unreachable). The single scheduling-related solver call reachable from the standard compilation pipeline is the auto-sharding solver, not this one.

---

## Function & Symbol Map

| Symbol | Address | Role | Confidence |
|--------|---------|------|------------|
| `xla::ILPMemoryScheduler::Run` | `0x10acd020` | outer Run: layout, `≤8` fallback, model bootstrap | CERTAIN |
| `(anon)::ILPMemorySchedulerForComputation::Run` | `0x10acdf00` | worker: variables, 11 constraints, objective, solve | CERTAIN |
| `xla::ILPMemoryScheduler::~ILPMemoryScheduler` | `0x10ad6900` | deleting dtor; resets base vptr | HIGH |
| vtable `xla::ILPMemoryScheduler` | `0x217fa460` | relocation only; **zero** code consumers | CERTAIN |
| `xla::GetMemorySchedulerAlgorithm` | `0x10abd6a0` | enum dispatch; **no `ILP` case** | CERTAIN |
| `xla::jellyfish::EnableIlpLatencyHidingScheduler` | `0x1d6b7e00` | live gate; reads proto +48 then flag | CERTAIN |
| `xla::jellyfish::GetIlpLatencyHidingSchedulerOptions` | `0x1d6b7e60` | options accessor | HIGH |
| `(anon)::RunHloScheduler` | `0x1096fac0` | gate caller; gate at line 1084 | CERTAIN |
| classifier `$_1` (ILP-on) | `0x10977140` | wider async predicate + sequence-order pin | CERTAIN |
| classifier `$_2` (regular) | `0x10977420` | narrower async predicate | CERTAIN |
| `IlpLatencyHidingSchedulerOptions::Clear` | `0x1db24ea0` | 6-bit presence mask, payload `0x18..0x32` | HIGH |
| `math_opt::Solve` | `0x10af9d20` | CP-SAT solve entry | HIGH |
| `math_opt::Solver::New` | `0x10b259a0` | registry dispatch over `SolverType` | HIGH |
| `math_opt::AllSolversRegistry::Register` | `0x10b273c0` | single caller registers CP-SAT only | CERTAIN |
| `math_opt::CpSatSolver::New` | `0x10adff20` | the one registered backend | HIGH |
| `_GLOBAL__sub_I_cp_sat_solver.cc` | `0x212ca300` | static-init: `Register(..., 4u, CpSatSolver::New)` | CERTAIN |
| `RetrieveSchedule` stable-sort | `0x10adf140` / `0x10adf3a0` / `0x10adf5e0` | slot-keyed merge of the solved schedule | HIGH |

---

## Cross-References

- [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md) — the canonical scheduler pass that path (A) feeds; the only LHS algorithm that runs
- [LHS: post_layout / final Variant](lhs-post-layout.md) — the final post-layout scheduling variant in the same pipeline
- [Scheduler Overview](overview.md) — where scheduling sits between lowering and encoding
- [ResourceType Taxonomy](scheduler-resourcetype-model.md) — the scheduler's 47-ID concurrency model, distinct from this MIP's peak-memory objective
- [Cost Model Overview](../cost/overview.md) — the HLO-level cost model behind `GetLatencyBetween`, used by path (A)'s canonical scheduler (this MIP uses only a peak-memory size function, not the cost model)
- [back to index](../index.md) — Part VIII — Instruction Scheduling & Bundle Packing
