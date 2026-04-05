# Optimization Levels

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The `--opt-level` (`-O`) flag controls how aggressively ptxas optimizes during the 159-phase pipeline. The option is parsed into a 32-bit integer at options block offset `+148` by `sub_434320` (line 216: `sub_1C96470(v10, "opt-level", a3 + 148, 4)`). The default value is **3**. The documented range is 0--4, but the internal NvOpt recipe system supports levels 0--5, and the scheduler and rematerialization passes distinguish level 5 from lower values.

The optimization level propagates through the compilation context and is read by individual passes via `sub_7DDB50` (232 bytes at `0x7DDB50`), which combines the opt-level check with a knob 499 guard. Passes that call `sub_7DDB50` receive the opt-level value (stored at compilation context offset `+2104`) only if knob 499 is enabled; otherwise, the function returns 1 (effectively clamping the level to O1 behavior).

| | |
|---|---|
| **CLI option** | `--opt-level` / `-O` |
| **Options block offset** | `+148` (int32) |
| **Default** | 3 |
| **Documented range** | 0--4 |
| **Internal range** | 0--5 (NvOpt levels) |
| **Accessor** | `sub_7DDB50` (0x7DDB50, 232 bytes) |
| **Knob guard** | 499 (if disabled, accessor returns 1) |
| **Parse location** | `sub_434320` line 216 |
| **Debug override** | `sub_431A40` forces level to 0 when `-g` is set |
| **Ofast-compile override** | `sub_434320` lines 635--679 |

## Level Summary

| Level | Name | Use Case |
|---|---|---|
| 0 | No optimization | Debug builds (`-g`), maximum source fidelity |
| 1 | Minimal optimization | Fast compile, basic folding/DCE |
| 2 | Standard optimization | Balanced speed/compile-time (previous default) |
| 3 | Full optimization | Default; all standard passes enabled |
| 4 | Aggressive optimization | Extra loop peeling, speculative hoisting |
| 5 | Maximum optimization | Full SinkRemat+Cutlass, highest compile time |

## Options Block Fields Affected by Opt Level

The option parser (`sub_434320`) and debug handler (`sub_431A40`) modify several options block fields based on the optimization level. Key interactions discovered from decompiled code:

| Offset | Field | O0 | O1 | O2+ | Source |
|---|---|---|---|---|---|
| `+148` | `opt_level` | 0 | 1 | 2, 3, 4 | Direct from `-O` |
| `+160` | `register_usage_level` | Forced to 5 if set with `-O0` (warning issued) | 5 (default) | 5 (default) | `sub_434320` line 359--363 |
| `+235` | `cloning_disabled` | 0 (disabled) | per-CLI | per-CLI | `sub_434320` line 776 |
| `+288` | `device_debug` | (from `-g`) | (from `-g`) | (from `-g`) | CLI only |
| `+292` | `sp_bounds_check` | **1** (auto-enabled) | per-CLI | per-CLI | `sub_434320` line 775 |
| `+326` | `no_cloning` | 1 (when `-g`) | per-CLI | per-CLI | `sub_431A40` line 42 |
| `+408` | `allow_expensive_optimizations` | **false** | **false** | **true** | `sub_434320` line 768 |
| `+477` | `fast_compile` | forced 0 with `-g` | per-CLI | per-CLI | `sub_431A40` line 28 |

The critical line at `sub_434320` offset 768:
```c
// allow-expensive-optimizations defaults to (opt_level > 1)
if (!user_set_allow_expensive)
    options->allow_expensive_optimizations = (options->opt_level > 1);
```

## Debug Mode Override (`-g`)

When `--device-debug` (`-g`) is active, `sub_431A40` (at `0x431A40`) forces the optimization level to 0 and disables most optimization features:

```c
// sub_431A40 pseudocode
void ApplyDebugOverrides(options_block* opts, bool suppress_warning) {
    if (suppress_warning) {
        opts->device_debug = 1;
        opts->sp_bounds_check_pair = 0x0101;  // +16 = {1, 1}
    }
    opts->sp_bounds_check = 1;                // +292

    // Warn if user explicitly set opt-level with -g
    if (was_set("opt-level") && opts->opt_level != 0)
        warn("ignoring -O with -g");

    // Warn about incompatible options
    if (was_set("register-usage-level"))
        warn("'--device-debug' overrides '--register-usage-level'");

    // Force register_usage_level to {5, 5} (pair at +160)
    *(int64*)(opts + 160) = 0x500000005LL;

    if (opts->fast_compile)
        warn("'--device-debug' overrides '--fast-compile'");
    opts->fast_compile = 0;

    if (opts->ofast_compile is "max" or "mid" or "min")
        warn("'--device-debug' overrides '--Ofast-compile'");
    opts->ofast_compile = "0";
    opts->opt_level = 0;                      // +148

    // Handle cloning
    if (was_set("cloning") && opts->device_debug && !opts->no_cloning)
        warn("-cloning=yes incompatible with -g");
    opts->cloning_disabled = 0;               // +235
}
```

The `0x500000005LL` write to offset `+160` sets both the 32-bit register-usage-level and the adjacent 32-bit field to 5, resetting any user override.

## Ofast-Compile Interaction

The `--Ofast-compile` (`-Ofc`) option provides a compile-time vs code-quality tradeoff orthogonal to `-O`. It has four settings: `0` (disabled), `min`, `mid`, `max`. Each setting overrides the opt-level and related flags:

| Ofast-compile | Forces opt_level to | Cloning | Split-compile | Expensive opts | Notes |
|---|---|---|---|---|---|
| `0` | (no change) | (no change) | (no change) | (no change) | Default |
| `min` | **1** | **disabled** | (no change) | **true** (forced) | Warns if `-O` set to non-{0,1} |
| `mid` | **1** | **disabled** | (no change) | (no change) | Disables cloning when no split-compile |
| `max` | **0** | **disabled** | (no change) | (no change) | Most aggressive compile-time reduction |

From `sub_434320` lines 635--679:
```c
if (ofast_compile == "max") {
    if (was_set("cloning") && !no_cloning)
        warn("-cloning=yes incompatible with --Ofast-compile=max");
    no_cloning = 1;
    if (was_set("opt-level") && opt_level != 0)
        warn("-opt-level=<1,2,3> incompatible with --Ofast-compile=max");
    opt_level = 0;
}
if (ofast_compile == "mid") {
    no_cloning = 1;
    if (!split_compile) cloning_disabled = 1;
    if (was_set("opt-level") && opt_level != 1)
        warn("-opt-level=<0,2,3> incompatible with --Ofast-compile=mid");
    opt_level = 1;
    fast_compile_mode = 1;
}
if (ofast_compile == "min") {
    no_cloning = 1;
    if (!split_compile) cloning_disabled = 1;
    if (was_set("opt-level") && opt_level != 1)
        warn("-opt-level=<0,2,3> incompatible with --Ofast-compile=min");
    opt_level = 1;
    fast_compile_mode = 1;
    if (was_set("allow-expensive-optimizations") && !allow_expensive)
        warn("-allow-expensive-optimizations=false incompatible with --Ofast-compile=min");
    allow_expensive_optimizations = 1;
}
```

## Per-Phase Gating by Optimization Level

Optimization levels control the pipeline through two mechanisms:

1. **Static `isNoOp()` overrides** -- The AdvancedPhase gate vtables are overridden at pipeline construction time based on the target architecture and opt-level.
2. **Runtime opt-level checks** -- Individual pass execute functions call `sub_7DDB50` (the opt-level accessor) and early-return when the level is below their threshold.

### Gate Accessor: sub_7DDB50

```c
// sub_7DDB50 pseudocode (232 bytes at 0x7DDB50)
int getOptLevel(compilation_context* ctx) {
    knob_vtable* kv = ctx->knob_state;             // ctx + 1664
    query_func qf = kv->vtable[19];                // vtable + 152

    if (qf == sub_67EB60) {                         // fast-path: known vtable
        check_func cf = kv->vtable[9];              // vtable + 72
        bool knob_499;
        if (cf == sub_6614A0)
            knob_499 = (kv->state[35928] != 0);     // direct field read
        else
            knob_499 = cf(ctx->knob_state, 499);     // indirect query
        if (!knob_499)
            return ctx->opt_level;                   // ctx + 2104
        int64 state = kv->state;
        int iteration = state[35940];
        if (state[35936] > iteration) {
            state[35940] = iteration + 1;            // increment pass counter
            return ctx->opt_level;
        }
    } else if (qf(ctx->knob_state, 499, 1)) {
        return ctx->opt_level;
    }
    return 1;                                        // fallback: treat as O1
}
```

When knob 499 is disabled (or its iteration budget is exhausted), `sub_7DDB50` returns 1 regardless of the actual opt-level. This provides a master kill-switch: setting knob 499 to false effectively caps all opt-level-gated behavior at O1.

### Phase Activation Table

The following table lists every phase where the optimization level has been confirmed to affect behavior, based on decompiled `isNoOp()` methods and execute-function guard checks.

**Threshold notation**: `> N` means the phase requires `opt_level > N` (i.e., level N+1 and above).

| Phase | Name | Threshold | Effect at threshold | Source |
|---|---|---|---|---|
| 14 | `DoSwitchOptFirst` | > 0 | Branch/switch optimization enabled | `isNoOp` returns true at O0 |
| 15 | `OriBranchOpt` | > 0 | Branch folding enabled | `isNoOp` returns true at O0 |
| 18 | `OriLoopSimplification` | 4--5 | Aggressive loop peeling enabled at O4+ | `sub_78B430` checks opt_level |
| 22 | `OriLoopUnrolling` | > 1 | Loop unrolling requires at least O2 | Execute guard via `sub_7DDB50` |
| 24 | `OriPipelining` | > 1 | Software pipelining requires at least O2 | Execute guard |
| 26 | `OriRemoveRedundantBarriers` | > 1 | Barrier optimization at O2+ | Gating: `opt_level > 1` |
| 28 | `SinkRemat` | > 1 / > 4 | O2+: basic path; O5: full cutlass mode | Two-tier guard in `sub_913A30` |
| 30 | `DoSwitchOptSecond` | > 0 | Second switch pass at O1+ | `isNoOp` returns true at O0 |
| 38 | `OptimizeNestedCondBranches` | > 0 | Nested branch simplification at O1+ | `isNoOp` returns true at O0 |
| 49 | `GvnCse` | > 1 | GVN-CSE requires at least O2 | Execute guard |
| 54 | `OriDoRematEarly` | > 1 | Early rematerialization at O2+ | `sub_7DDB50` check |
| 63 | `OriDoPredication` | > 1 | If-conversion at O2+ | Execute guard |
| 69 | `OriDoRemat` | > 1 | Late rematerialization at O2+ | `sub_7DDB50` check |
| 71 | `OptimizeSyncInstructions` | > 1 | Sync optimization at O2+ | Gating: `opt_level > 1` |
| 72 | `LateExpandSyncInstructions` | > 2 | Late sync expansion at O3+ | Gating: `opt_level > 2` |
| 95 | `SetAfterLegalization` | > 1 | Post-legalization flag at O2+ | `sub_7DDB50` check |
| 99 | `OriDoSyncronization` | > 1 | Sync insertion at O2+ | Gating: `opt_level > 1` |
| 100 | `ApplyPostSyncronizationWars` | > 1 | WAR fixup at O2+ | Gating: `opt_level > 1` |
| 110 | `PostSchedule` | > 0 | Full post-schedule at O1+ | Mode selection |
| 115 | `AdvancedScoreboardsAndOpexes` | > 0 | Full scoreboard generation at O1+ | Hook activated at O1+ |
| 116 | `ProcessO0WaitsAndSBs` | == 0 | Conservative scoreboards at O0 only | Active only at O0 |

### O-Level Feature Matrix

| Feature | O0 | O1 | O2 | O3 | O4 | O5 |
|---|---|---|---|---|---|---|
| Basic block merging | off | on | on | on | on | on |
| Branch/switch optimization | off | on | on | on | on | on |
| Copy propagation + const folding | off | on | on | on | on | on |
| Dead code elimination | **partial** | on | on | on | on | on |
| Loop canonicalization | basic | basic | full | full | aggressive | aggressive |
| Loop unrolling | off | off | on | on | on | on |
| Software pipelining | off | off | on | on | on | on |
| Strength reduction | off | on | on | on | on | on |
| GVN-CSE | off | off | on | on | on | on |
| Predication (if-conversion) | off | off | on | on | on | on |
| Rematerialization (early) | off | off | on | on | on | on |
| Rematerialization (late) | off | off | on | on | on | on |
| SinkRemat (full) | off | off | off | off | off | **on** |
| Cutlass iterative remat | off | off | off | off | off | **on** |
| Loop peeling (aggressive) | off | off | off | off | **on** | **on** |
| Barrier optimization | off | off | on | on | on | on |
| Sync instruction optimization | off | off | on | on | on | on |
| Late sync expansion | off | off | off | on | on | on |
| Post-legalization mark | off | off | on | on | on | on |
| Allow expensive optimizations | off | off | **on** | on | on | on |
| Speculative hoisting | off | off | on | on | on | on |
| Hot/cold partitioning | off | on | on | on | on | on |
| Full scoreboard analysis (115) | off | **on** | on | on | on | on |
| Conservative scoreboards (116) | **on** | off | off | off | off | off |
| Stack-pointer bounds check | **auto-on** | off | off | off | off | off |
| Cloning | **disabled** | on | on | on | on | on |

Notes:
- "partial" DCE at O0: `EarlyOriSimpleLiveDead` (phase 10) still runs for basic cleanup even at O0.
- O4 and O5 are not documented in `--help` but are accepted internally. O4 is equivalent to O3 plus aggressive loop peeling. O5 adds the full SinkRemat pass with cutlass iteration support.

## Scoreboard Path Selection

The scoreboard generation subsystem has two mutually exclusive paths, selected by optimization level:

### O0: Conservative Path (Phase 116)

Phase 116 (`ProcessO0WaitsAndSBs`) inserts maximum-safety scheduling metadata:

```
For every instruction:
    stall_count = 15            // maximum stall (15 cycles)
    wait_mask   = 0x3F          // wait on all 6 barriers
    write_barrier = 7           // no barrier assignment (7 = none)
    read_mask   = 0             // no read barriers
    yield       = 1             // yield after every instruction
```

This eliminates all instruction-level parallelism. Every instruction waits the maximum time and clears all dependency barriers before executing. The result is correct but extremely slow code -- suitable only for debugging.

### O1+: Full Analysis Path (Phase 115)

Phase 115 (`AdvancedScoreboardsAndOpexes`) runs the complete dependency analysis pipeline:

1. `sub_A36360` (52 KB) -- Master control word generator with per-opcode dispatch
2. `sub_A23CF0` (54 KB) -- DAG list scheduler heuristic for barrier assignment
3. Per-field encoders for stall, yield, barrier, and scoreboard dependency fields

The full path computes precise stall counts based on actual instruction latencies from the hardware profile, assigns the minimum necessary dependency barriers (6 available per SM), and inserts yield hints only where the warp scheduler benefits from switching.

## Scheduling Direction

The scheduling infrastructure (`sub_8D0640`) selects scheduling direction based on opt-level:

| Condition | Direction | Strategy |
|---|---|---|
| `opt_level <= 2` | Forward-pass | Register-pressure-reducing: prioritizes freeing registers |
| `opt_level > 2` | Reverse-pass | Latency-hiding: prioritizes ILP and memory latency overlap |

At the default O3, the scheduler uses the reverse-pass strategy, which hides memory latencies at the cost of potentially higher register pressure. At O1--O2, the forward-pass strategy minimizes peak register usage.

The direction selection happens in `PreScheduleSetup` (`sub_8CBAD0`), called from the scheduling orchestrator with the boolean `opt_level > 2`:
```c
PreScheduleSetup(sched, opt_level > 2);    // sub_8CBAD0
```

Additionally, the `ScheduleInstructionsReduceReg` phase (mode 0x39) is enabled by default at O3 and above, providing a dedicated register-pressure-reduction scheduling pass before the ILP pass.

## Register Allocation Differences

The register allocator itself (fat-point greedy at `sub_957160`) does not directly branch on the optimization level. However, the opt-level affects register allocation indirectly through:

1. **`--register-usage-level`** (offset `+160`, range 0--10, default 5): At O0 with `-g`, this is forced to 5 regardless of user setting. The value modulates the per-class register budget at `alloc + 32*class + 884`.

2. **`allow-expensive-optimizations`** (offset `+408`): Defaults to `true` when `opt_level > 1`. When true, the allocator and related passes are permitted to spend more compile time on better solutions (e.g., more spill-retry iterations, more aggressive coalescing).

3. **Phase gating**: At O0, passes that reduce register pressure (rematerialization, predication, loop optimizations) are disabled, so the allocator receives un-optimized IR with higher register demand. This typically results in more spills at O0.

## NvOpt Recipe System

The NvOpt recipe system (Phase 1: `ApplyNvOptRecipes`, option 391) provides an additional optimization-level axis. When enabled, the PhaseManager allocates a 440-byte NvOptRecipe sub-manager that configures per-phase aggressiveness:

| NvOpt level | Behavior |
|---|---|
| 0 | Minimal optimization (fast-compile path, many phases set to `isNoOp()`) |
| 1--2 | Standard optimization |
| 3--4 | Aggressive optimization (loop unrolling, speculative hoisting enabled) |
| 5 | Maximum optimization (may significantly increase compile time) |

The NvOpt level is validated in `sub_C173E0` via the string `"Invalid nvopt level : %d."`, confirming the range 0--5. Recipe data lives at `NvOptRecipe+312` with per-phase records at stride 584 bytes.

The NvOpt level is distinct from the `-O` CLI level. The `-O` level controls which phases run at all (via `isNoOp()` and `sub_7DDB50` guards); the NvOpt level controls how aggressively the phases that do run behave (via recipe parameters).

## Knob Defaults That Change Per Level

Several knobs have default values that vary by optimization level. The most significant:

| Knob | O0 Default | O1 Default | O2+ Default | Effect |
|---|---|---|---|---|
| 487 | enabled | enabled | enabled | Master optimization enable (checked by many passes) |
| 499 | (varies) | (varies) | (varies) | Guard knob for `sub_7DDB50` opt-level accessor |
| 595 | true | true | true | Scheduling enable (but O0 uses conservative path) |
| 419 | -- | -- | -- | Forward scheduling mode (bit 3 in scheduler flags) |

Knob 487 is the most pervasive: it is checked by loop simplification, barrier optimization, sync optimization, predication, rematerialization, and scheduling passes. Disabling it overrides the opt-level and turns off the corresponding pass regardless of `-O` setting.

## Key Decompiled Evidence

### Options block opt_level field (offset +148)

```c
// sub_434320 line 216: parse opt-level from CLI
sub_1C96470(v10, "opt-level", a3 + 148, 4);
```

### allow-expensive-optimizations defaults to (opt_level > 1)

```c
// sub_434320 line 768
if (!user_set_allow_expensive)
    *(a3 + 408) = *(a3 + 148) > 1;
```

### O0 forces sp-bounds-check and disables cloning

```c
// sub_434320 lines 773-776
if (opt_level == 0) {
    *(a3 + 292) = 1;       // sp_bounds_check = true
    *(a3 + 235) = 0;       // cloning_disabled = false (misleading name: 0 = no cloning)
}
```

### Debug mode forces O0

```c
// sub_431A40 line 33
*(a3 + 148) = 0;           // opt_level = 0
```

### Ofast-compile=max forces O0

```c
// sub_434320 line 646
*(a3 + 148) = 0;           // opt_level = 0 for Ofast=max
```

### Ofast-compile=mid/min forces O1

```c
// sub_434320 lines 659, 674
*(a3 + 148) = 1;           // opt_level = 1 for Ofast=mid and min
*(a3 + 572) = 1;           // fast_compile_mode = 1
```

### Scoreboard mutually exclusive paths

```c
// Phase 115 (AdvancedScoreboardsAndOpexes): isNoOp() returns true at O0
// Phase 116 (ProcessO0WaitsAndSBs): isNoOp() returns true at O1+
```

### SinkRemat two-tier gating

```c
// sub_913A30 (SinkRemat core, phase 28)
if (getOptLevel(ctx) <= 1) return;      // O0/O1: skip entirely
if (getOptLevel(ctx) <= 4) return;      // O2-O4: skip full sink+remat
// O5: proceed to cutlass iterative mode
```

### Sync barrier gating

```c
// Phase 99 (OriDoSyncronization), Phase 71 (OptimizeSyncInstructions):
call   sub_7DDB50              ; get opt_level
cmp    eax, 1
jle    return                  ; skip if opt_level <= 1

// Phase 72 (LateExpandSyncInstructions):
// Requires opt_level > 2
```

## Cross-References

- [CLI Options](cli-options.md) -- `--opt-level`, `--device-debug`, `--Ofast-compile`, `--allow-expensive-optimizations`
- [Knobs System](knobs.md) -- Knob 487 (master enable), knob 499 (opt-level guard)
- [Pass Inventory](../passes/index.md) -- Complete 159-phase table with per-phase descriptions
- [Phase Manager](../passes/phase-manager.md) -- AdvancedPhase hooks and dispatch loop
- [Scoreboards](../scheduling/scoreboards.md) -- Phase 115/116 scoreboard generation
- [Scheduling](../scheduling/overview.md) -- Forward vs reverse scheduling direction
- [Rematerialization](../passes/rematerialization.md) -- O-level-dependent remat behavior
- [Branch & Switch](../passes/branch-switch.md) -- O0 disables all branch/switch optimization
- [Sync & Barriers](../passes/sync-barriers.md) -- Per-pass opt_level thresholds
- [Loop Passes](../passes/loop-passes.md) -- O4/O5 aggressive loop peeling
- [Optimization Pipeline](../pipeline/optimizer.md) -- Pipeline-level O0 vs O1+ gating

## Key Functions

| Address | Size | Role | Confidence |
|---------|------|------|------------|
| `sub_434320` | -- | CLI option parser; parses `--opt-level` at line 216, handles `--Ofast-compile` at lines 635--679, sets `allow-expensive-optimizations` default at line 768 | 0.95 |
| `sub_431A40` | -- | Debug mode override; forces opt-level to 0, disables cloning, resets register-usage-level when `-g` is active | 0.95 |
| `sub_7DDB50` | 232B | Opt-level accessor; returns `ctx+2104` opt-level if knob 499 is enabled, otherwise returns 1 (O1 fallback). Called by 20+ passes as the runtime opt-level gate | 0.95 |
| `sub_1C96470` | -- | Generic CLI argument reader; called by `sub_434320` to read `--opt-level` into options block offset `+148` | 0.85 |
| `sub_67EB60` | -- | Fast-path knob query vtable function; identified inside `sub_7DDB50` for knob 499 check | 0.80 |
| `sub_6614A0` | -- | Knob state direct-read function; used by `sub_7DDB50` to read knob 499 via direct field access at offset 35928 | 0.80 |
| `sub_78B430` | -- | `OriLoopSimplification` execute function; checks opt_level for aggressive loop peeling (O4+) | 0.85 |
| `sub_913A30` | -- | `SinkRemat` core (phase 28); two-tier opt-level guard: skips at O0--O1, limited at O2--O4, full cutlass mode at O5 | 0.90 |
| `sub_8D0640` | -- | Scheduling infrastructure; selects forward (O1--O2) vs reverse (O3+) scheduling direction | 0.85 |
| `sub_8CBAD0` | -- | `PreScheduleSetup`; called with `opt_level > 2` boolean to configure scheduling direction | 0.85 |
| `sub_957160` | -- | Fat-point greedy register allocator; does not directly branch on opt-level but is affected indirectly | 0.90 |
| `sub_A36360` | 52KB | Master scoreboard control word generator (phase 115, O1+ path) | 0.90 |
| `sub_A23CF0` | 54KB | DAG list scheduler heuristic for barrier assignment (phase 115, O1+ path) | 0.90 |
| `sub_C173E0` | -- | NvOpt level validator; emits `"Invalid nvopt level : %d."` for out-of-range values | 0.85 |
