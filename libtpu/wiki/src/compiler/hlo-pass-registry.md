# HLO Pass Registry

> *Addresses, build-id, and symbol names apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ; treat every VA as version-pinned.*

## Abstract

Every HLO-level transformation on the TPU backend runs inside one container class — `xla::HloPassPipeline` — and one driver method: `RunPassesInternal`. There is no TPU-private pipeline type. The TPU compiler reuses the *same* upstream-XLA `HloPassPipeline` that the CPU and GPU backends use, instantiated and populated by the Jellyfish pipeline-builder functions ([`compile-phases.md`](compile-phases.md) owns those builders and the phase ordering). This page owns the *machinery* underneath that ordering: the `Run` driver loop, the `HloPassInterface` ABI every pass implements, the `AddPass`/`AddInvariantChecker` registration API that the builders call, the flag-gated pass-disable/enable filter (`GetEnabledPasses`), and the invariant-checker re-validation loop that runs after every mutating pass.

The reader who knows LLVM should map this onto the LLVM legacy `PassManager` — but the differences are central. An `HloPassPipeline` is itself an `HloPassInterface`, so pipelines nest as passes (an `HloPassFix<HloPassPipeline>` re-runs a whole sub-pipeline to a fixed point). Passes do not declare analysis dependencies and there is no analysis cache; each pass re-derives what it needs and returns a single `StatusOr<bool>` "did I change the module" bit. Invariant checkers are a second pass list that runs the *identical* `Run` ABI but must not mutate, and they re-validate after every pass that reported a change — not once at pipeline end. The whole driver is written defensively: it cross-checks each pass's self-reported change bit against an independent hash of the module and `CHECK`-fails the compiler if a pass lies about whether it mutated.

This page is the ABI-and-driver reference. The *enumeration* of which passes are added and in what order is on [`compile-phases.md`](compile-phases.md) (the ten-phase spine) and [`hlo-pre-passes.md`](hlo-pre-passes.md) (the front pre-pass table); the 372-entry `name()` RTTI surface (the `_ZNK3xla*4nameEv` symbols) is enumerated on [`hlo-pre-passes.md`](hlo-pre-passes.md); and individual pass algorithms live on their own pages ([`algebraic-simplifier.md`](algebraic-simplifier.md), [`layout-assignment.md`](layout-assignment.md), [`fusion-patterns.md`](fusion-patterns.md), [`msa-overview.md`](msa-overview.md)). Here we document only the container, the interface, and the add/gate/check mechanism.

For reimplementation, the contract is:

- **The `HloPassInterface` ABI.** Every pass and every checker is a vtable with a `name()` slot (vtable+16) returning a `string_view`, a `Run(module, execution_threads)` slot returning `StatusOr<bool>`, and an `is_pass_pipeline()` discriminator. A pipeline is just a pass whose `Run` is `RunPassesInternal`.
- **The two-list container.** An `HloPassPipeline` holds a `passes_` vector and a separate `invariant_checkers_` vector, plus a `run_called_` latch. `AddPass<T>` appends to the first; `AddInvariantChecker<T>` appends to the second; both `CHECK`-fail if called after `Run` has started.
- **The driver loop.** `RunPassesInternal` runs `GetEnabledPasses` once, then runs all checkers at "pipeline-start", then for each enabled pass: time it, run it, validate the self-reported change bit against an HLO hash, and re-run all checkers if it changed.
- **The flag gate.** `GetEnabledPasses` filters `passes_` against the `--xla_disable_hlo_passes` and `--xla_enable_hlo_passes_only` `DebugOptions` lists (the two are mutually exclusive, `CHECK`-enforced), keyed by each pass's `name()`.

| | |
|---|---|
| **Container class** | `xla::HloPassPipeline` (upstream XLA, not TPU-private) |
| **Source file** | `third_party/tensorflow/compiler/xla/hlo/pass/hlo_pass_pipeline.{cc,h}` |
| **Driver** | `HloPassPipeline::RunPassesInternal<HloModule*>` @ `0x1c83ddc0` (786 lines decompiled) |
| **Flag filter** | `HloPassPipeline::GetEnabledPasses` @ `0x1c83d0a0` |
| **Checker loop** | `HloPassPipeline::RunInvariantCheckers<HloModule*>` @ `0x1c840500` |
| **HLO dump hook** | `HloPassPipeline::MaybeDumpHloAndSaveFilenames` @ `0x1c83da00` |
| **Add (checker)** | `HloPassPipeline::AddInvariantChecker<HloCycleDetection>` @ `0x109459a0`; `<HloVerifier, TargetVerifierMetadata>` @ `0x1306d420` |
| **Fixed-point wrapper** | `HloPassFix<HloPassPipeline>::RunToFixPoint` @ `0x10955dc0`; `RunImpl` @ `0x10955d20` |
| **Pass interface** | `xla::HloPassInterface` — `name()` (vtable+16), `Run()` (`xla::HloPassInterface::Run` thunk), `is_pass_pipeline()` |
| **`passes_` vector** | offset +40 (ptr) / +48 (count) inside the pipeline object |
| **`invariant_checkers_` vector** | offset +56 (ptr) / +64 (count) |
| **`run_called_` latch** | offset +80 (byte) |
| **`name()` symbols** | 372 `_ZNK3xla*4nameEv` symbols — 137 `xla::jellyfish::*`, 24 `xla::tpu::sparse_core::*`, 211 other `xla::*` (not all are HLO passes; e.g. `xla::Executable::name()` is in the set) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The `HloPassInterface` ABI

### Purpose

`HloPassInterface` is the single abstract base every HLO pass derives from. It is the contract the driver dispatches against: the driver never knows a pass's concrete type, only its vtable. The TPU backend adds 137 `xla::jellyfish::*` and 24 `xla::tpu::sparse_core::*` `name()`-overriding classes on top of 211 other `xla::*` classes (372 `_ZNK3xla*4nameEv` symbols total — not all are HLO passes; the set also includes non-pass `name()` overrides such as `xla::Executable`), but every concrete pass shares this one vtable shape, which is why the `RunPassesInternal` loop can be type-agnostic.

### Vtable layout

Three slots matter to the driver, recovered from the call sites in `RunPassesInternal`, `RunInvariantCheckers`, and `GetEnabledPasses`:

| Slot | Offset | Signature | Used by |
|---|---|---|---|
| `name()` | vtable+16 | `string_view name() const` | `GetEnabledPasses` (filter key), driver VLOG, dump filenames |
| `Run()` | dispatched via `xla::HloPassInterface::Run` | `StatusOr<bool> Run(HloModule*, const flat_hash_set<string_view>& execution_threads)` | driver per-pass call, checker call |
| `is_pass_pipeline()` | vtable+32 | `bool is_pass_pipeline() const` | discriminates pipeline-as-pass from a leaf pass; the vtable+32 slot was not exercised on the change-verification path of `RunPassesInternal` (hash recording is gated on `DebugOptions+928\|+911`, not on this slot) [Confidence: MEDIUM on the +32 offset] |

The `name()` slot is the reverse-engineering anchor for the whole surface: the 372 `_ZNK3xla*4nameEv` symbols are the near-complete enumeration of `name()`-overriding XLA classes, because every concrete pass overrides `name()` — though the set also catches a handful of non-pass `name()` overrides (`xla::Executable`, the PjRt executables), so it is an over-set of the pass list, not an exact count. The string a pass returns is its dashed-lowercase name (`"tpu-int2-auto-up-down-caster"`, `"sharding-propagation"`, `"legalize-scheduling-annotations"` — all byte-confirmed) that the `--xla_disable_hlo_passes` flag matches against.

> **NOTE — `Run`'s boolean is the only signal a pass returns.** `Run` returns `StatusOr<bool>`: a non-OK `Status` aborts the pipeline; an OK `true` means "I changed the module," an OK `false` means "no change." There is no richer result — no list of modified computations, no invalidated-analysis set. The driver's entire change-tracking is built on this one bit, which is exactly why it independently audits the bit against a module hash (see [§The Driver Loop](#the-driver-loop)).

### A pipeline is a pass

`HloPassPipeline` itself derives from `HloPassInterface`. Its `Run` is `RunPassesInternal`; its `is_pass_pipeline()` returns true. This is the structural reason pipelines nest: a builder can `AddPass<HloPassPipeline>` a fully-populated sub-pipeline, and the outer driver runs it as one pass. The `HloPassFix<HloPassPipeline>` instantiations (`RunToFixPoint` @ `0x10955dc0`, `RunOnChangedComputations` @ `0x10955b00`) confirm this: a whole sub-pipeline is re-run to a fixed point exactly as a single pass would be.

```text
HloPassInterface (abstract)
  ├─ name() const                      → string_view   (vtable+16)
  ├─ Run(module, threads)              → StatusOr<bool> (the change bit)
  └─ is_pass_pipeline() const          → bool           (vtable+32)
        │
        ├─ TpuAlgebraicSimplifier, TpuInstructionFusion, …  (322 leaf passes)
        ├─ HloVerifier / HloCycleDetection / LegalizeSchedulingAnnotations  (checkers)
        └─ HloPassPipeline             ── is_pass_pipeline()==true, Run==RunPassesInternal
              └─ HloPassFix<HloPassPipeline>  ── re-runs the sub-pipeline to fixpoint
```

---

## The Pipeline Container

### Purpose

`HloPassPipeline` is the object the builder functions populate. It is a named bag of two ordered lists — the passes and the invariant checkers — plus the `run_called_` latch that freezes both lists once execution begins. The container does no scheduling and no dependency analysis; order is purely insertion order.

### Object layout

Recovered from the field offsets the driver and the `AddInvariantChecker` instances touch:

| Field | Offset | Type | Meaning |
|---|---|---|---|
| vtable | +0 | `HloPassInterface` vptr | the pipeline-as-pass dispatch |
| `name_` | +8…+32 | inlined `std::string` (SSO) | pipeline name, e.g. `"pre-optimization"`, `"Layout assignment"`, `"async_scheduling"` (all byte-confirmed) |
| `passes_` | +40 / +48 | `vector<unique_ptr<HloPassInterface>>` ptr / count | the ordered pass list, read by `GetEnabledPasses` (as `v15+4`/`v15+5`) |
| `invariant_checkers_` | +56 / +64 | `vector<unique_ptr<HloPassInterface>>` ptr / count | the checker list, read by `RunInvariantCheckers` (as `a1+56`/`a1+64`) and appended by `AddInvariantChecker` |
| `run_called_` | +80 | `bool` | latch — set true at `Run` entry; further `AddPass`/`AddInvariantChecker` `CHECK`-fail |

The pipeline name appears in the FATAL diagnostics ("Pass '%s' in pipeline '%s' …"). The mixed-case names (`"Layout assignment"`, `"pre-optimization"`, `"async_scheduling"`) are the `HloPassPipeline` ctor strings the Jellyfish builders pass; [`compile-phases.md`](compile-phases.md) maps each to its stage.

> **QUIRK — the checker list is separate from the pass list and runs on a different cadence.** A naive reimplementation merges verifiers into the pass sequence as ordinary passes. libtpu keeps `invariant_checkers_` in a second vector and runs the *entire* checker list before the first pass and after every changing pass — a quadratic-in-checkers, linear-in-passes validation schedule. Merging them into `passes_` would run each checker once per its own slot, not after every other pass, and would miss the malformed intermediates that the continuous schedule catches.

---

## The Add / Gate Mechanism

### `AddPass` and `AddInvariantChecker`

Both are templates the builder functions call at construction time. `AddPass<T>(args...)` constructs a `T` and appends it to `passes_` (offset +40); `AddInvariantChecker<T>(args...)` constructs a `T` and appends it to `invariant_checkers_` (offset +56). Both are `CHECK`-guarded against the `run_called_` latch.

The `AddInvariantChecker<HloCycleDetection>` instance (`0x109459a0`) shows the mechanism end to end:

```c
function AddInvariantChecker<T>(pipeline, ctor_args...):     // 0x109459a0 (HloCycleDetection)
    if pipeline->run_called_ == 1:                           // offset +80
        FATAL("!run_called_",                                // hlo_pass_pipeline.h:76
              "AddInvariantChecker cannot be called after Run")
    checker = new T(ctor_args...)                            // e.g. HloCycleDetection (vtable off_217F4558)
    pipeline->invariant_checkers_.push_back(                 // offset +56
        unique_ptr<HloPassInterface>(checker))
    return checker
```

`AddPass<T>` is the identical shape against `passes_` (offset +40); the `run_called_` `CHECK` is shared. The latch is what makes the pipeline immutable once running — there is no add-during-run.

The recovered `AddInvariantChecker` instantiations name exactly the three checker types the TPU pipeline registers (cross-referenced in [`compile-phases.md`](compile-phases.md)'s `MaybeAddInvariantCheckers`):

| Checker type | Ctor metadata | VA | Role |
|---|---|---|---|
| `HloVerifier` | `unique_ptr<TargetVerifierMetadata>` | `0x1306d420` | structural HLO verifier with the **TPU** verifier metadata |
| `HloVerifier` | `unique_ptr<CpuGpuVerifierMetadata>` + name `"…"` | `0x14bcb340` | the open-source CPU/GPU-metadata form (present but not the TPU path) |
| `HloCycleDetection` | (none) | `0x109459a0` | reject control/data cycles in the HLO graph |
| `LegalizeSchedulingAnnotations` | `Config&` | `0x10945740` | scheduling-annotation legality, registered as a checker |

> **QUIRK — the same class is both a pass and a checker.** `LegalizeSchedulingAnnotations` (`name()` = `"legalize-scheduling-annotations"`) appears in the pass list *and* as an invariant checker. As a pass it legalizes annotations (mutating); as a checker it must run read-only. The interface does not distinguish — both call `Run` — so the "checker must not mutate" rule is a convention the driver enforces only indirectly, by verifying the module hash is unchanged across the whole checker sweep, not per checker.

### `AddInvariantChecker<HloVerifier>` — the TPU verifier wiring

The TPU-metadata instance (`0x1306d420`) constructs an `HloVerifier` (vtable `off_21D2A9A0`) holding a `TargetVerifierMetadata` (moved-from the `unique_ptr` argument), with the verifier's name field initialized to `"Unknown"`. `TargetVerifierMetadata` is the TPU subclass of XLA's `VerifierMetadata`; the open-source default is `CpuGpuVerifierMetadata` (the `0x14bcb340` instance). A reimplementation must supply a target-specific metadata object so the verifier enforces TPU shape/layout invariants rather than the generic CPU/GPU set.

### The flag gate — `GetEnabledPasses`

`GetEnabledPasses` (`0x1c83d0a0`) is run once at the top of `RunPassesInternal`. It turns the full `passes_` list into the filtered list the driver will actually run, keyed on each pass's `name()`:

```c
function GetEnabledPasses(pipeline, debug_options) -> vector<HloPassInterface*>:  // 0x1c83d0a0
    if debug_options.fast_path_flag(+553) == 1:            // short-circuit: all passes enabled
        return all_of(pipeline->passes_)
    disabled = set(debug_options.xla_disable_hlo_passes)   // repeated string @ DebugOptions+64
    enabled  = set(debug_options.xla_enable_hlo_passes_only)// repeated string @ DebugOptions+112
    FATAL_IF(!disabled.empty() && !enabled.empty(),        // hlo_pass_pipeline.cc:247
             "disabled_pass_names.empty() || enabled_pass_names.empty()")

    // (1) whole-pipeline gate: the pipeline's own name() can be disabled/enabled
    if disabled.contains(pipeline->name()):                // VLOG "Disable the full pass:"  cc:251
        return {}                                          // empty → pipeline runs nothing
    if !enabled.empty() && !enabled.contains(pipeline->name()):  // cc:256
        return {}

    // (2) per-pass gate: keep a pass iff its name() is not in the disable set
    out = []
    for pass in pipeline->passes_:                         // ptr/count @ pipeline+40 / +48
        if !disabled.contains(pass->name()):
            out.push_back(pass)
    return out
```

> **GOTCHA — disable and enable-only are mutually exclusive, and disabling a *pipeline* name silences the whole stage.** Two traps. First, setting both `--xla_disable_hlo_passes` and `--xla_enable_hlo_passes_only` is a hard `CHECK`-fail (`cc:247`), not a merge — a reimplementation that lets both coexist diverges immediately. Second, the disable/enable check runs against the *pipeline's* name first: because every nested pipeline (`"Layout assignment"`, `"async_scheduling"`) is itself a named pass, disabling that name returns an empty pass list and the entire stage is skipped, not just one pass. The byte-confirmed VLOG strings `"Passes disabled by --xla_disable_hlo_passes: "` (`cc:238`), `"Passes enabled by --xla_enable_hlo_passes_only: "` (`cc:243`), `"Disable the full pass: "` (`cc:251`), and `"Enable the full pass: "` (`cc:256`) anchor each branch.

> **NOTE — this is name-based gating, not the `xla_jf_*`/`xla_tpu_*` feature flags.** `GetEnabledPasses` only honors the two debug-options *pass-name lists*. The per-feature flags (`xla_jf_*`, `xla_tpu_*`, `xla_msa_*`) that turn passes on or off are read *inside each builder function* — `if (env->flag) AddPass<T>(...)` — before the pass ever reaches the pipeline. So a flag-disabled pass is simply never added, while a name-disabled pass is added then filtered out here. The two mechanisms compose; [`compile-phases.md`](compile-phases.md) owns the builder-level flag gating, this page owns the name-level filter. The flag→pass binding predicate was not individually traced. [Confidence: HIGH on the name filter; the feature-flag gating is documented on the builder pages.]

---

## The Driver Loop

### Purpose

`RunPassesInternal` (`0x1c83ddc0`) is the heart of the registry: it walks the enabled passes once, in order, running each and re-validating invariants. It is also where the compiler's defensive change-auditing lives — the part most likely to surprise a reimplementer.

### Algorithm

```c
function RunPassesInternal(pipeline, debug_options, module, threads) -> StatusOr<bool>:  // 0x1c83ddc0
    enabled = GetEnabledPasses(debug_options)              // the filtered pass list
    PushAnnotation("pipeline: " + pipeline->name())        // tsl::profiler scope

    // (A) initial invariant sweep
    status = RunInvariantCheckers("pipeline-start")        // cc:151 on failure
    if !status.ok(): return status
    RecordPassStartMetadata("pipeline-start")
    MaybeDumpHloAndSaveFilenames("pipeline-start", next_pass_name)
    RecordPassEndMetadata()

    // verification mode: read DebugOptions hash-check toggles once
    check_hash = module.flags(+928)                        // "verify the pass changed the HLO"
              || module.flags(+911)                        //   (combined into v139)
    changed_overall = false

    for i, pass in enumerate(enabled):                     // passes+40 / count+48
        timer = ScopedLoggingTimer("HLO pass: " + pass->name())  // cc lambda#2
        VLOG(1) "  HLO pass " << pass->name()              // cc:174
        VLOG(2) "  Number of instructions: " << module.instruction_count()  // cc:180

        if check_hash:
            hash_before = AbslHashValue(module)            // independent fingerprint

        PushAnnotation("pass: " + pass->name())
        RecordPassStartMetadata(pass->name())
        status = pass->Run(module, threads)                // the only mutation point
        if status.ok():                                    // status carried the change bit
            module.Cleanup()                               // drop dead state post-pass
        else:
            return status.AddSourceLocation(cc:191)        // pass failed → abort
        changed = status.value()                           // the self-reported bit

        // (B) audit the change bit against the hash  (cc:116 / cc:125)
        if check_hash:
            hash_after = AbslHashValue(module)
            if hash_after != hash_before && !changed && module.flags(+928):
                FATAL("Pass '%s' in pipeline '%s' reported that it did not "  // cc:116
                      "change the HLO but the hash of HLO was changed …", …)
            if hash_after == hash_before && changed && module.flags(+911):
                FATAL("Pass '%s' in pipeline '%s' reported that it changed "  // cc:125
                      "the HLO but the hash of HLO was not updated …", …)

        MaybeDumpHloAndSaveFilenames(pass->name(), next_pass_name)  // dump after this pass
        RecordPassEndMetadata(changed)
        changed_overall |= changed

        // (C) re-validate invariants only if this pass actually changed the module
        if changed:
            VLOG(3) "  Pass caused changes " << pass->name()       // cc:206
            status = RunInvariantCheckers(pass->name())            // cc:213 on failure
            if !status.ok(): return status.AddSourceLocation(cc:213)

    return changed_overall
```

### What the driver guarantees

- **One pass at a time, insertion order, no reordering.** `enabled` is walked front to back; there is no priority, no dependency graph, no analysis cache. A reimplementation that reorders for "efficiency" produces a different program.
- **`module.Cleanup()` after every successful pass.** Dead computations/instructions left by a pass are dropped before the next pass sees the module — so a pass need not perfectly tidy up, but the next pass always sees a cleaned module.
- **Checkers re-run only after a *changing* pass.** A pass that returns `false` (no change) does not trigger a checker sweep — the module is assumed still valid. This is the cadence the [`compile-phases.md`](compile-phases.md) callout refers to: verification after each *mutating* pass, not at phase boundaries.
- **The change bit is audited, not trusted.** The two FATAL paths (`cc:116`, `cc:125`) catch both directions of a lying pass: changed-but-said-no, and said-yes-but-didn't. Each is independently gated by a `DebugOptions` toggle (`module+928`, `module+911`), so the audit is opt-in (debug/CI builds), but the code is always present.

> **GOTCHA — the change-bit audit hashes the *whole module* twice per pass when enabled.** With the hash-check toggles on, every pass pays two full `AbslHashValue(HloModule)` traversals plus a `RunInvariantCheckers` sweep on any change. This is a debug/verification mode, not the production path — but a reimplementation that wires the audit into the hot path will see compile time balloon on large modules. The audit exists to catch the single nastiest pass bug (a stale change bit silently disabling downstream fixed-point convergence), so it is correct to keep it, gated.

### `RunInvariantCheckers` — the checker sweep

```c
function RunInvariantCheckers(module, after_pass_name, threads) -> Status:  // 0x1c840500
    if invariant_checkers_.empty(): return OK              // a1+64 == 0 fast path
    for checker in invariant_checkers_:                    // a1+56 / a1+64
        VLOG(1) "    Invariant checker " << checker->name()  // cc:83
        status = checker->Run(module, threads)             // SAME ABI as a pass
        if status.ok():
            module.Cleanup()                               // checkers may schedule cleanup
        else:
            return status.AddSourceLocation(hlo_pass_pipeline.h:147)
    return OK
```

Checkers dispatch through the identical `HloPassInterface::Run` slot as passes; the only difference is policy (they must not mutate) and that a non-OK status from any checker aborts the whole pipeline. The sweep runs the *full* checker list each time it is invoked.

---

## Fixed-Point Wrapping — `HloPassFix`

Some passes and whole sub-pipelines must run to convergence, not once. `HloPassFix<P>` wraps a pass `P` (or a pipeline) and re-runs it until it reports no change. The recovered instantiations confirm the mechanism is generic over both leaf passes and pipelines:

| Wrapper | `RunToFixPoint` VA | What it re-runs |
|---|---|---|
| `HloPassFix<HloPassPipeline>` | `0x10955dc0` | an entire sub-pipeline to fixpoint |
| `HloPassFix<HloDCE>` | `0x1d6d7a60` | dead-code elimination to fixpoint |
| `HloPassFix<jellyfish::TpuReduceWindowRewriter>` | `0x109589e0` | TPU reduce-window rewrite |
| `HloPassFix<jellyfish::MosaicFusion>` | `0x1095e800` | Mosaic-kernel fusion |
| `HloPassFix<AllReduceReassociate>` | `0x1095f5e0` | collective reassociation |
| `HloPassFix<ReduceWindowRewriter>` | `0x14bd0980` | OSS reduce-window |
| `HloPassFix<ReduceScatterReassociate>` | `0x109603c0` | collective reduce-scatter reassoc |
| `HloPassFix<WhileLoopConstantSinking>` | `0x12ef1280` | while-loop constant sinking |
| `HloPassFix<AllReduceReduceScatterReorder>` | `0x109611a0` | collective reorder |

`HloPassFix` exposes the same `HloPassInterface` ABI (`RunImpl` / `RunOnChangedComputations` / `RunOnChangedComputationsOnce`), so the outer driver treats a fixed-point wrapper as an ordinary pass and the change bit it returns is the OR of all its iterations.

> **NOTE — non-convergence behavior.** Upstream XLA bounds `HloPassFix` iterations and, under a flag, fatals if a pass oscillates rather than converging. The flag string for the TPU crash-on-non-convergence behavior was not located in the sampled strings; the `HloPassFix` fixed-point mechanism itself is CONFIRMED (the `RunToFixPoint`/`RunOnChangedComputations` vtable triplet above), but the crash-on-divergence claim is [Confidence: LOW]. See the matching callout on [`compile-phases.md`](compile-phases.md).

---

## How the Builders Use the Registry

The Jellyfish pipeline-builder functions ([`compile-phases.md`](compile-phases.md)) are the *only* callers of `AddPass`/`AddInvariantChecker`. The construction pattern, recovered structurally, is:

```text
CreateHloPipeline (0x1093efe0)                 ── allocates the top HloPassPipeline
  ├─ AddInvariantChecker<HloVerifier(TargetVerifierMetadata)>   (via MaybeAddInvariantCheckers 0x10944600)
  ├─ AddInvariantChecker<HloCycleDetection>
  ├─ AddInvariantChecker<LegalizeSchedulingAnnotations>
  ├─ AddPass<...> × N             ── the pre-opt / sharding / layout / fusion passes, flag-gated
  └─ AddPass<HloPassPipeline>     ── nested named sub-pipelines ("Layout assignment", "async_scheduling", …)
        └─ (each nested pipeline re-runs MaybeAddInvariantCheckers at its own head)
```

`MaybeAddInvariantCheckers` (`0x10944600`) is the single helper that registers the three checkers at the head of every nested pipeline — which is why the continuous re-validation holds at every nesting level, not only the top. Once the builder returns, the populated pipeline is invoked exactly once by `RunPassesInternal`, and the `run_called_` latch then rejects any late mutation.

> **QUIRK — the registry is built fresh per compilation, not a static table.** Despite the "registry" framing, there is no global pass registry the way LLVM has `PassRegistry`. Each compile constructs a new `HloPassPipeline`, the builders `AddPass` into it imperatively (with flag gating), it runs once, and it is destroyed. The "registry" is the 372-`name()` *type* surface ([`hlo-pre-passes.md`](hlo-pre-passes.md)) plus this per-compile instantiation pattern — not a persistent registration DAG. (The `google_init_module_*` DAG that *does* persist registers emitters and HAL factories, not HLO passes.)

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| `HloPassPipeline::RunPassesInternal` is the HLO driver | decompiled `0x1c83ddc0`, source `hlo_pass_pipeline.cc` | CONFIRMED |
| Passes/checkers share `HloPassInterface::Run` (`StatusOr<bool>`) | both `RunPassesInternal` and `RunInvariantCheckers` call `xla::HloPassInterface::Run` | CONFIRMED |
| Two lists: `passes_`(+40/+48), `invariant_checkers_`(+56/+64), `run_called_`(+80) | offsets read by driver/`GetEnabledPasses`/`AddInvariantChecker` | CONFIRMED |
| `AddInvariantChecker` `CHECK`-fails after `Run` | FATAL `"!run_called_"` @ `hlo_pass_pipeline.h:76` in `0x109459a0`, `0x1306d420` | CONFIRMED |
| Three TPU checkers: `HloVerifier(TargetVerifierMetadata)`, `HloCycleDetection`, `LegalizeSchedulingAnnotations` | `AddInvariantChecker<…>` instantiations at the listed VAs | CONFIRMED |
| `GetEnabledPasses` filters on `xla_disable_hlo_passes`/`xla_enable_hlo_passes_only`, mutually exclusive | decompiled `0x1c83d0a0`, byte strings `cc:238/243/247/251/256` | CONFIRMED |
| Driver audits change bit vs HLO hash, FATAL on mismatch | `cc:116`/`cc:125` FATAL strings + `AbslHashValue(HloModule)` calls | CONFIRMED |
| Checkers re-run after every changing pass (not once) | `if changed: RunInvariantCheckers` @ `cc:213` inside the loop | CONFIRMED |
| `HloPassFix<HloPassPipeline>` re-runs a sub-pipeline to fixpoint | `RunToFixPoint` `0x10955dc0` + leaf-pass `HloPassFix` instances | CONFIRMED |
| Crash-on-non-convergence flag for `HloPassFix` | flag string not located in sampled strings | LOW |
| Builders are the only `AddPass`/`AddInvariantChecker` callers; per-compile instantiation | `CreateHloPipeline`/`MaybeAddInvariantCheckers` call structure | HIGH |

---

## Cross-References

- [Compile Phases 0–3](compile-phases.md) — the ten-phase spine and the Jellyfish builder functions (`CreateHloPipeline`, `PreOptimizationPipeline`, `MaybeAddInvariantCheckers`) that populate this container.
- [The TPU Compiler](overview.md) — Part V orientation; where the HLO pass pipeline (Family 1) sits in the five compile phases and the IR-layer stack.
- [HLO Pre-Passes](hlo-pre-passes.md) — the enumerated pass catalog (the 372-entry `name()` RTTI surface) this driver runs, with per-pass HLO invariants.
- [Algebraic Simplifier](algebraic-simplifier.md) — a representative leaf pass (`TpuAlgebraicSimplifier`) implementing the `HloPassInterface` ABI.
- [Layout Assignment](layout-assignment.md) — the codegen-gating analysis run as an `HloPassInterface` inside the through-layout pipeline.
- [Fusion Patterns](fusion-patterns.md) — `TpuInstructionFusion`, the main fusion pass added in the Phase 5 fusion stage.
- [MSA Overview](msa-overview.md) — memory-space assignment, driven post-pipeline by `RunMemorySpaceAssignment`.
- [back to index](../index.md)
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
