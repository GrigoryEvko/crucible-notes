# CompileCommand Pipeline & the Canonical Job Order

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/driver/`). The driver is a set of `.cpython-310-x86_64-linux-gnu.so` modules compiled by Cython, NOT stripped and carrying debug info; addresses are file offsets inside `commands/CompileCommand.so` unless another module is named. Other wheels differ — treat every address as version-pinned.*

## Abstract

A `neuronx_cc` compile is not a monolithic call. It is a **process pipeline**: an ordered list of *Job* names is built once, materialised into `Job` instances, and run in sequence — most stages by forking a standalone `starfish/bin` tool ELF and waiting on it. This page documents the spine that builds that list: `CompileCommand.buildPipeline` (`0x619d0`), the three collector functions it concatenates, and the `--meta-module` / Backend gates that reshape the list. Once you can predict the name list those three collectors emit for a given argument set, you can predict the entire compile schedule; there is no hidden scheduler.

The construction is mechanical and lives in one function. `buildPipeline` parses the compile arglist into an `argparse.Namespace`, normalises a handful of flags (notably `--optlevel` and the `enable_internal_*` family), then forms the job-name list by **string-concatenating three sub-lists** — `collectFrontendPipeline` + `collectWalrusPipeline` + `collectBackendPipeline`, left to right (two `PyNumber_Add` calls). It stores the result on `self.pipeline`, filters it through a small name special-case loop (the `Watchpoint` / `BIRSim` test), and hands it to `self.jobRegistry.makePipeline`, which walks the names through a `job_factories` map and appends a `Job` instance per name. `buildPipeline` returns a `(pipeline, args)` 2-tuple; `runPipeline` (`0x7a3a0`) executes it.

Two readings are easy to get wrong and are corrected below. First, the **emission order** is `HLOToTensorizer → {goldens} → Frontend → StaticIOTranspose | WalrusDriver | Backend? Kelper NeffWrapper?` — `HLOToTensorizer` (the `hlo2penguin` lowering) is emitted *first*, before the `Frontend` job and before the golden evaluator, so the intuitive "optimize HLO, then lower" order does not match the process order. Second, `WalrusDriver` is a **single** Job whose ~18 backend stages (`lsa`, `coloring`, `pre_sched`, …) are accumulated into `self.walrus_passes` and forwarded as `--walrus-passes` to one `walrus_driver` process — they are not separate Jobs.

For reimplementation, the contract is:

- The three-collector concatenation and the **exact intra-collector order** (verified line-by-line from the decompiled list-construction, not inferred): front = `[HLOToTensorizer, {XLA}InferGoldens, Frontend, StaticIOTranspose]`, walrus = `[WalrusDriver]`, backend = `[Backend?, Kelper, NeffWrapper?]`.
- The `--meta-module` collapse: the front sub-list shrinks to `[HLOToTensorizer, Frontend]` and `self.logical_nc_config` is forced to `1`.
- The Backend **gate envelope** — a four-condition AND that empties the backend sub-list in the modular / bir-linker / legacy-standalone flows.
- The name→Job step (`makePipeline`) and the `(pipeline, args)` return shape consumed by `Daemon` / `runPipeline`.

| | |
|---|---|
| **Pipeline builder** | `CompileCommand.buildPipeline` @ `0x619d0` (py 1146–1349) |
| **Front collector** | `CompileCommand.collectFrontendPipeline` @ `0x22b00` (py 1636–1648) |
| **Walrus collector** | `CompileCommand.collectWalrusPipeline` @ `0x29e00` (py 1655–1725) |
| **Backend collector** | `CompileCommand.collectBackendPipeline` @ `0x1fd00` (py 1727–1741) |
| **Name → Job map** | `JobRegistry.makePipeline` @ `0xbb80` (args `self, jobnames`) |
| **Executor** | `CompileCommand.runPipeline` @ `0x7a3a0` (py 1513–1572) → `Pipeline.runSingleInput` @ `0xe030` |
| **Default job order** | `HLOToTensorizer → InferGoldens → Frontend → StaticIOTranspose → WalrusDriver → Kelper → (NeffWrapper)` |
| **`--meta-module` order** | `HLOToTensorizer → Frontend → WalrusDriver → Kelper → (NeffWrapper)` |
| **Return shape** | `(Pipeline, argparse.Namespace)` 2-tuple |

Cross-refs: the name→class registry and per-Job argv templates live in [3.4 — Job Registry & Per-Job Argv](job-registry.md); the orientation map and IR descent in [0.2 — The Compile Pipeline at a Glance](../front/pipeline.md); each stage's deep dive in its own Part-3/4/8/12 page.

---

## 1. `buildPipeline` — the construction algorithm

`buildPipeline` is the per-request pipeline builder. The daemon calls it once per compile (`pipeline, args = compileCommand.buildPipeline(argv, logger)`), parses the compile arglist, derives flags, then assembles and returns the schedule. The body is one large Cython-generated function (`__pyx_pw_…_9buildPipeline`, frame ~100 KB, py-lines 1146–1349); the structurally important statements are anchored to `_Pyx_AddTraceback(…, <pyline>, …)` markers and to the interned-string constant pool.

The skeleton, with each step anchored to its decompiled call site:

```python
def buildPipeline(self, argv, logger):                 # 0x619d0, py 1146
    args = self.parser.parse_args(argv)                # argparse.Namespace, py ~1150

    self.processSerialTpbOpts(args)                    # 0x619d0 decomp L3377 (n_s_processSerialTpbOpts)
    self.validateArgs(args)                            # 0x619d0 decomp L3537 (n_s_validateArgs)

    # ── flag normalisation (py ~1192…) ──────────────────────────────────
    #   reads args.optlevel  (L5519, GetAttrStr 'optlevel')
    #   '0' → enable_internal_new_backend = False  (+ dge_on_levels tweak)
    #   default '2' applied if unset
    #   sets self.walrus_passes lazily (later, in collectWalrusPipeline)

    # ── the canonical name list = concat of three collectors ────────────
    fe = self.collectFrontendPipeline(args)            # L7946, GetAttrStr, py 1241
    wd = self.collectWalrusPipeline(args)              # L8073, GetAttrStr, py 1243
    be = self.collectBackendPipeline(args)             # L8167, GetAttrStr, py 1245

    names = fe + wd + be                               # PyNumber_Add ×2: L8257, L8282

    # ── name special-case filter (the py 1252 build loop) ───────────────
    #   each name tested via _Pyx_PyUnicode_Equals against
    #   MetaInferGoldens (L8455), InferGoldens (L8460), Watchpoint (L8466),
    #   BIRSim (L8472); a _Pyx_ListComp_Append rebuilds the list (L8514)
    self.pipeline = names                              # SetAttrStr 'pipeline' L9627

    pipeline = self.jobRegistry.makePipeline(self, ...) # GetAttr 'jobRegistry' L10212,
                                                        # GetAttr 'makePipeline' L10230, py 1293
    return (pipeline, args)                             # 2-tuple, py 1349
```

> **NOTE — ordering of `processSerialTpbOpts` / `validateArgs` is firm.** Both run *before* any collector: their `GetAttrStr` call sites (L3377, L3537) precede the three collector `GetAttrStr` sites (L7946 / L8073 / L8167) in straight-line decompiled order, and their AddTraceback line numbers (around py 1146-region setup) precede py 1241–1245. So argument validation completes before the schedule is shaped — a collector never sees an unvalidated `Namespace`. (HIGH)

> **QUIRK — the three collectors are concatenated, not interleaved or merged.** The name list is literally `fe + wd + be` via two `PyNumber_Add` operations (L8257, L8282) on three Python lists. There is no sort, no dependency resolution, no topological pass. **Whatever order each collector appends its names in IS the compile order.** A reimplementation that builds a graph and topologically sorts it will produce subtly different output; the binary just splices three lists end to end. (CERTAIN — two `PyNumber_Add` on the three collector return values.)

### The name special-case loop (`Watchpoint`, `BIRSim`)

After concatenation, py 1252 rebuilds the list through a comprehension that tests each name string with `_Pyx_PyUnicode_Equals(name, K, 3)` for `K ∈ {MetaInferGoldens, InferGoldens, Watchpoint, BIRSim}` (decomp L8455–8472), appending survivors with `_Pyx_ListComp_Append` (L8514). This is a name-level filter/special-case applied while the names are still strings — **not** a collector. `Watchpoint` and `BIRSim` are therefore never emitted by `collectFrontend/Walrus/Backend`; they are debug/aux names that this loop recognises and routes specially. (HIGH — the four `Equals` constants and the comprehension append are explicit; the precise keep/drop semantics of each branch is MED.)

---

## 2. `collectFrontendPipeline` — front sub-list & the `--meta-module` collapse

`collectFrontendPipeline` (`0x22b00`, py 1636–1648) returns a 2- or 4-element name list. The branch is a single read of `args.meta_module` (`GetAttr 'meta_module'`, decomp L679, py 1640). The two arms build their lists with explicit `PyList_New` + slot stores, so the order is read directly off the decompile — **not** inferred.

### Non-`--meta-module` arm (the default, py 1646–1648)

```python
# decomp LABEL_48 .. L825
goldens = XLAInferGoldens if args.framework == XLAInterface else InferGoldens   # py 1647
return [HLOToTensorizer, goldens, Frontend, StaticIOTranspose]                  # py 1648
```

The slot stores are unambiguous (decomp L814–825):

| Slot | Name string | Decomp evidence |
|---|---|---|
| `[0]` | `HLOToTensorizer` | `*v44 = n_u_HLOToTensorizer` (L817) |
| `[1]` | `XLAInferGoldens` **or** `InferGoldens` | `v44[1] = n_u_XLAInferGoldens` (L819); the operand is the goldens choice |
| `[2]` | `Frontend` | `v44[2] = n_u_Frontend` (L822) |
| `[3]` | `StaticIOTranspose` | `v44[3] = n_u_StaticIOTranspose` (L825) |

The goldens choice (py 1647) is a `framework == XLAInterface` test: `GetAttr 'framework'` (L731) → `PyObject_RichCompare` against the `XLAInterface` builtin (L748/L764/L769). True ⇒ `XLAInferGoldens` (L802), False ⇒ `InferGoldens` (L807). `MetaInferGoldens` is a string the binary knows (in the `Watchpoint`/name-filter set and the registry) but is **not** the operand of this slot store; it is selected elsewhere (the modular path / name-filter), not by `collectFrontendPipeline`'s default arm. (HIGH for the four-slot order; the `MetaInferGoldens` vs `InferGoldens` substitution path is MED.)

### `--meta-module` arm (py 1642–1643)

```python
# decomp LABEL_39 .. L915
self.logical_nc_config = 1                    # SetAttr 'logical_nc_config' = int_1, L896, py 1642
return [HLOToTensorizer, Frontend]            # PyList_New, slots L909/L913, py 1643
```

The two slot stores are `n_u_HLOToTensorizer` (L909–912) then `n_u_Frontend` (L913–915). The golden evaluator and `StaticIOTranspose` are **dropped**, and `self.logical_nc_config` is forced to `1` immediately before the list is built.

> **GOTCHA — `--meta-module` does two things, and they are coupled.** It both collapses the front sub-list to `[HLOToTensorizer, Frontend]` *and* pins `logical_nc_config = 1` (decomp L896, the `tp_setattro(self, n_s_logical_nc_config_2, int_1)` store). A reimplementation that collapses the list but forgets the LNC pin will diverge on multi-NeuronCore configs: meta-module compilation is single-logical-NC by construction. The two effects are emitted in the same branch arm, in this order (LNC pin first, then list). (CERTAIN — both are explicit stores in the meta-module arm.)

> **CORRECTION — meta-module drops the goldens job, not just `StaticIOTranspose`.** A naive reading of the default 4-list might assume meta-module merely trims the tail. The decompile shows the meta-module arm is a *separate* `PyList_New` of length 2 containing only `HLOToTensorizer` and `Frontend` — both the golden evaluator (`{XLA}InferGoldens`) and `StaticIOTranspose` are absent. Goldens and static IO-transpose are skipped because meta-module compiles a sub-module that is linked later, where those steps are handled at link time. (HIGH)

---

## 3. `collectWalrusPipeline` — one Job, ~18 forwarded passes

`collectWalrusPipeline` (`0x29e00`, py 1655–1725) returns the **single-element** list `["WalrusDriver"]` (decomp L2646–2649, `*v255 = n_u_WalrusDriver`). Everything else the function does is build `self.walrus_passes` — a *list of `walrus_driver` internal pass names* set on `self` (`tp_setattro(self, n_s_walrus_passes, list)`, L442) and later forwarded to the one `walrus_driver` process as `--walrus-passes`.

The pass list is accumulated by repeated appends, gated by a handful of `Namespace` reads. The pass-name corpus (interned `__pyx_n_u_*` constants in this function): `lsa`, `coloring`, `unroll`, `lower_ac`, `pre_sched`, `partitioner`, `bir_splitter`, `coloring_allocator` / `linear_scan_allocator`, `dma_optimization`, `anti_dependency_analyzer`, `vn_splitter`, `post_sched`, `tensorcopy_accel`, `dep_reduction`, `print_metrics`, and finally the gated pair `birverifier` / `bir_sim` (the py 1723 tail, AddTraceback line 1723). The gate reads visible in the body:

| Gate (`Namespace` attr) | CLI flag | Selects | Decomp |
|---|---|---|---|
| `args.allocator` | `--allocator` | `coloring_allocator` vs `linear_scan_allocator` | `GetAttr 'allocator'` L458 / L758; `Unsupported allocator= %s` |
| `args.scheduler` | `--scheduler` | pre/post-sched stage shape | `GetAttr 'scheduler'` L596; `Unsupported scheduler= %s` |
| `args.enable_bir_vnsplitter` | `--enable-bir-vnsplitter` | `vn_splitter` / `bir_splitter` | body attr read |
| `args.neuroncore_pipeline_cores` | `--neuroncore-pipeline-cores` | pipeline-core-dependent stages | body attr read |

Interned alternative-stage strings also present (selected by experimental flags): `experimental_loop_lsa`, `experimental_loop_shift_left`, `shift_left`.

> **GOTCHA — `WalrusDriver` is one Job, not a chain.** The ~18 names above look like a pipeline but are *backend pass* names accumulated in `self.walrus_passes` and handed to a single `walrus_driver` invocation via `--walrus-passes`. The collector's return value is unambiguously the one-element `["WalrusDriver"]` (L2646–2649). A reimplementation that turns each pass name into a Job will fork ~18 processes instead of one. See [Part 8 — libwalrus Backend](../walrus/) for the in-process `EmbeddedWalrusDriver` alternative (`--enable-internal-fork-walrus`). (CERTAIN.)

> **NOTE — `birverifier` / `bir_sim` are pass names here, but `BIRVerifier` is a real Job elsewhere.** The `birverifier` string appended to `walrus_passes` (py 1723) is a `walrus_driver` pass, distinct from the `BIRVerifier` *Job* class that reuses the `walrus_driver` ELF in verify-only mode (see [3.4](job-registry.md)). The casing differs because one is a pass token and the other a Python class name. (HIGH.)

---

## 4. `collectBackendPipeline` — the gate envelope & B-slot order

`collectBackendPipeline` (`0x1fd00`, py 1727–1741) starts with an empty `PyList_New` (L734, py 1729) and conditionally appends `Backend`, `Kelper`, `NeffWrapper`. The whole block sits inside a **four-condition AND** gate, each a `Namespace` read with an early bail when truthy/false. Read directly off the nested-`if` structure (decomp L741–960):

```python
def collectBackendPipeline(self, args):              # 0x1fd00, py 1727
    pipeline = []                                    # PyList_New, L734, py 1729
    if (not args.enable_internal_modular_compilation # GetAttr, L743, py 1731
        and not args.enable_internal_bir_linker      # GetAttr, L769, py 1732
        and args.layer_unroll_factor > 0):           # RichCompare(GT 0), L796/L801, py 1733
        if args.internal_run_standalone_legacy_compilation:   # GetAttr, L836, py 1734
            pipeline.append("Backend")               # n_u_Backend, L946–951, py 1736
        pipeline.append("Kelper")                    # n_u_Kelper, L868–882, py 1738 (LABEL_83)
        if args.enable_internal_neff_wrapper:        # GetAttr, L886, py 1740
            pipeline.append("NeffWrapper")           # n_u_NeffWrapper, L912–918, py 1741
    return pipeline
```

The intra-collector order is firm from the append sites: `Backend` (L946–951, only on the legacy-standalone branch) → fall through to `LABEL_83` which appends `Kelper` (L868–882, **always** within the gated block) → then `NeffWrapper` (L912–918, only if the neff-wrapper flag). Each append uses the fast in-place list store when capacity allows and falls back to `PyList_Append` otherwise (e.g. L871/L929/L955).

The gate envelope and per-step gating:

| B-slot | Name | Outer gate (all four must hold) | Per-step gate | py |
|---|---|---|---|---|
| B0 | `Backend` | `not modular` ∧ `not bir_linker` ∧ `layer_unroll_factor > 0` | `internal_run_standalone_legacy_compilation` truthy | 1731–1734, 1736 |
| B1 | `Kelper` | same | none — always, within the gated block | 1738 |
| B2 | `NeffWrapper` | same | `enable_internal_neff_wrapper` | 1740–1741 |

> **GOTCHA — `Backend` is gated on the legacy-standalone flag; `Kelper` is not.** Within the four-condition envelope, `Kelper` is appended unconditionally (L868–882, reached via `LABEL_83` from *both* the legacy and non-legacy arms), whereas `Backend` is appended only when `internal_run_standalone_legacy_compilation` is truthy (L946–951). This refines the earlier reading that paired the two as a single "legacy codegen block": the NEFF-linker `Kelper` runs in the common gated flow even when the experimental `Backend` executor does not. (HIGH — the `LABEL_83` fall-through from the non-legacy arm at L865 `if (!v34) goto LABEL_83` is explicit.)

> **GOTCHA — the entire backend sub-list is empty in three flows.** If `enable_internal_modular_compilation` is true, OR `enable_internal_bir_linker` is true, OR `layer_unroll_factor <= 0`, the outer `if` is false and `collectBackendPipeline` returns `[]`. In the modular / bir-linker flows the `walrus_driver` output is consumed by those paths directly, so `Backend` / `Kelper` / `NeffWrapper` never run. The `layer_unroll_factor > 0` condition is a `PyObject_RichCompare` against `0` (L796/L801, `Py_GT`), so a zero or negative unroll factor also empties the block. (CERTAIN.)

---

## 5. The canonical ordered job table

`buildPipeline` job names = `collectFrontend + collectWalrus + collectBackend`, left-to-right. The table is the **default flow** (no special flags, `--optlevel 2`); the **Gate** column says when each job is present. Native tool / mechanism per [3.4](job-registry.md).

| Step | Job (name string) | Runs as | Gate (when emitted) | Evidence |
|---|---|---|---|---|
| **front** (`collectFrontendPipeline`, py 1636–1648) | | | | |
| F0 | `HLOToTensorizer` | `hlo2penguin` (forked) | always | slot `[0]`, L817 / L909 |
| F1 | `XLAInferGoldens` **or** `InferGoldens` | `xla_infergoldens` / in-proc goldens | unless `--meta-module`; `XLA*` iff `framework==XLAInterface` | slot `[1]`, L819; choice L802/L807 |
| F2 | `Frontend` | in-process penguin / XLA frontend | always | slot `[2]`/`[1]`, L822 / L913 |
| F3 | `StaticIOTranspose` | in-process Python (`io_transposes.json`) | unless `--meta-module` | slot `[3]`, L825 |
| **walrus** (`collectWalrusPipeline`, py 1655–1725) | | | | |
| W0 | `WalrusDriver` | `walrus_driver` (or in-proc `libwalrus.so`) | always | return list, L2646–2649 |
| **backend** (`collectBackendPipeline`, py 1727–1741) | | | | |
| B0 | `Backend` | emits `backend.bir` (experimental) | legacy-standalone flow only, within the four-cond gate | append L946–951, py 1736 |
| B1 | `Kelper` | NEFF linker/writer (`calcNeffVersion`) | within the four-cond gate (always there) | append L868–882, py 1738 |
| B2 | `NeffWrapper` | `hlo-neff-wrapper` (forked) | `--enable-internal-neff-wrapper`, within the gate | append L912–918, py 1741 |

**Default flow** (`--optlevel 2`, no special flags):
`HLOToTensorizer → InferGoldens → Frontend → StaticIOTranspose → WalrusDriver → Kelper → (NeffWrapper)`.

**`--meta-module` flow:** front collapses to `HLOToTensorizer → Frontend`, `logical_nc_config = 1`; walrus + backend unchanged:
`HLOToTensorizer → Frontend → WalrusDriver → Kelper → (NeffWrapper)`.

**Modular / bir-linker / legacy-standalone-off flow:** backend sub-list empty; the schedule ends at `WalrusDriver` (its output consumed downstream).

> **CORRECTION — `Frontend` runs the HLO frontend in-process, it does not fork `hlo-opt`.** The orientation page [0.2](../front/pipeline.md) lists the `Frontend` job as `hlo-opt (--passes=…)`. Module-level analysis of `jobs/Frontend.so` finds **no** `hlo-opt` / `opt-driver` / `hlo_opt` string and no `Popen`; the HLO/penguin frontend runs in-process via `neuronxcc.starfish.penguin.Penguin` (`runPenguin` / `runXLAFrontend`). `hlo-opt` ships as a standalone debug pass-driver ELF, unwired to any driver Job. Likewise `StaticIOTranspose` writes `io_transposes.json` with in-process Python `json`, not an ELF. Treat this page's table (which marks F2/F3 in-process) as the corrected one; [0.2] is being reconciled. (HIGH — negative string evidence + no Popen in `Frontend.so`.)

---

## 6. `makePipeline` — names → `Job` instances

`JobRegistry.makePipeline` (`0xbb80`, args `self, jobnames` — confirmed from the argparse name table at decomp L247: `n_s_self`, `n_s_jobnames`) is the only place a name string becomes a `Job`. It constructs an empty `neuronxcc.driver.Pipeline`, iterates the ordered `jobnames` list, resolves each name through `self.__getJob(name)` against a `job_factories` map, and `pipeline.addJob(job)` (`Pipeline.addJob` @ `0xd190`). The map is populated at registry init by `loadAllJobs`, which `pkgutil.iter_modules` over `neuronxcc.driver.jobs`, imports each under `sys.setdlopenflags(... | RTLD_DEEPBIND)`, and indexes every `Job` subclass by its class name. Unknown names hit the `defaultdict.__missing__` and raise a keyed `CompilerInvalidInputException`.

```python
def makePipeline(self, jobnames):                 # 0xbb80
    pipeline = Pipeline()
    for name in jobnames:                          # the concatenated collector list
        pipeline.addJob(self.__getJob(name))       # __getJob @ 0xe0d0; addJob @ 0xd190
    return pipeline
```

So the registry contributes **no ordering** — it is a pure name→class lookup plus `argparse`-group wiring. The compile order is fixed entirely by the collectors. `buildPipeline` then returns `(pipeline, args)` (py 1349), which `Daemon` / `runPipeline` unpack.

> **NOTE — `RTLD_DEEPBIND` is why every Job `.so` can carry its own XLA/penguin symbols.** `loadAllJobs` imports each job module with deep-bind dlopen flags so statically-linked duplicate symbols (XLA, penguin) do not clash across the process. This is a property of the registry, not the pipeline, but it explains why each `jobs/*.so` is a self-contained 10–60 MB module rather than a thin wrapper. (HIGH.)

---

## 7. Execution — `runPipeline` and fatal exits

`runPipeline` (`0x7a3a0`, py 1513–1572) takes the `(pipeline, args)` from `buildPipeline`, sets up `GlobalState` (`InitGlobalState` / `GetGlobalState` / `FinalizeGlobalState`), `chdir`s into the work dir (`getWorkingDir` / `getLaunchDir` / `checkArtifactDir`), logs `"Intermediate files stored in %s …"`, forks a progress-dot child (`print_dot_context` / `print_dots`, a `@contextmanager` fork wrapper, MED), runs the pipeline, then emits metrics (`logMetrics` / `submitMetrics` / `tallyMetrics`). The pipeline body runs each Job sequentially in `Pipeline.runSingleInput` (`0xe030`) with per-job timers and the log lines `"Running pipeline %s %d"`, `"Starting job %s"`, `"Finished job %s"`, `"Finished pipeline %s"`.

A native-spawning Job's failure (non-zero `subprocess` return) propagates up as a `CalledProcessError` / `'Child process job "%s" exited abnormally!'`. The top-level fatal wrap lives in `CommandDriver.run` (`0x1c510`), which runs the subcommand inside a `multiprocessing.Process`, `join()`s it, reads `process.exitcode`, and routes through `handleError` (`0x30470`):

| Code | String (interned in `CommandDriver.so`) | Trigger | Conf |
|---|---|---|---|
| `[F134]` | `neuronx-cc terminated abnormally - Please open a support ticket …` @ `0x37fa0` | generic abnormal termination | HIGH ownership |
| `[F137]` | `neuronx-cc was forcibly killed - This most commonly occurs due to insufficient system memory …` @ `0x37ee0` | child killed by signal (negative exitcode — SIGKILL/OOM) | HIGH ownership |
| `[F139]` | `neuronx-cc terminated abnormally - Please open a support ticket …` @ `0x37e40` | second abnormal-termination variant | HIGH ownership |

`run_subcommand_in_process` ends with `logging.shutdown()` + `sys.exit(<code>)`.

> **GOTCHA — the exact exitcode→`[F13x]` thresholds are a derived branch, not a clean switch.** Ownership of the three codes by `CommandDriver.run` / `handleError` is HIGH (the interned strings live in `CommandDriver.so` and are selected on `process.exitcode`), but the precise code→string mapping (e.g. which negative exitcodes map to `[F137]` vs `[F139]`) is a reconstructed branch — treat the exitcode boundaries as **MED**. The negative-exitcode → `[F137]` (OOM/kill) reading is the strongest of the three. (MED on thresholds.)

---

## 8. CLI flag → pipeline-shape map

The job list is shaped by `Namespace` attributes the collectors read. `--optlevel` is notable for *not* directly adding or removing top-level jobs.

| CLI flag (`Namespace` attr) | Effect on job list / passes | Evidence |
|---|---|---|
| `--meta-module` (`meta_module`) | front → `[HLOToTensorizer, Frontend]`; drops goldens + `StaticIOTranspose`; sets `logical_nc_config = 1` | collectFront L679/L896/L909–915, py 1640/1642/1643 |
| `--framework <…>` (`framework`) | goldens = `XLAInferGoldens` iff `framework == XLAInterface` else `InferGoldens` | collectFront L731/L769/L802/L807, py 1647 |
| `--optlevel {0,1,2,3}` (`optlevel`, default `"2"`) | string knob; tunes `--walrus-passes` + `enable_internal_*`; `"0"` ⇒ `enable_internal_new_backend = False` | buildPipeline L5519 |
| `--enable-internal-modular-compilation` | empties backend sub-list | collectBackend L743, py 1731 |
| `--enable-internal-bir-linker` | empties backend sub-list | collectBackend L769, py 1732 |
| `--layer-unroll-factor` (`> 0`) | required for backend sub-list (`RichCompare GT 0`) | collectBackend L796/L801, py 1733 |
| `internal_run_standalone_legacy_compilation` | truthy ⇒ emit `Backend` | collectBackend L836/L946, py 1734/1736 |
| `--enable-internal-neff-wrapper` | appends `NeffWrapper` | collectBackend L886/L912, py 1740/1741 |
| `--allocator` (`allocator`) | `coloring_allocator` vs `linear_scan_allocator` walrus pass | collectWalrus L458/L758 |
| `--scheduler` (`scheduler`) | pre/post-sched walrus pass shape | collectWalrus L596 |
| `--enable-bir-vnsplitter` (`enable_bir_vnsplitter`) | gates `vn_splitter` / `bir_splitter` passes | collectWalrus body |
| `--neuroncore-pipeline-cores` (`neuroncore_pipeline_cores`) | gates pipeline-core walrus passes | collectWalrus body |

> **GOTCHA — `--optlevel` is a string that tunes flags, not a job toggle.** `optlevel` is read as a string `"0".."3"` (default `"2"`, L5519) and shapes the `--walrus-passes` set plus the `enable_internal_*` derivations the collectors read. It changes the top-level job list only *indirectly* — when `"0"` flips `enable_internal_new_backend`, or when it selects the modular / bir-linker flow that empties the backend sub-list. A reimplementer wiring `--optlevel` straight into the job list is wrong. (HIGH.)

---

## 9. Adversarial self-verification

The five strongest claims, re-checked against the binary:

1. **`buildPipeline` is at `0x619d0`.** ✔ CERTAIN. The decompiled symbol is `__pyx_pw_9neuronxcc_6driver_8commands_14CompileCommand_14CompileCommand_9buildPipeline_0x619d0` — the Cython public-wrapper mangling carries both the qualified name `CompileCommand.buildPipeline` and the offset `0x619d0`; the file name itself embeds `_9buildPipeline_0x619d0`.

2. **The three-collector concatenation order is front → walrus → backend.** ✔ CERTAIN. `GetAttrStr` of `collectFrontendPipeline` (L7946), `collectWalrusPipeline` (L8073), `collectBackendPipeline` (L8167) appear in that decompiled order, each anchored to py 1241 / 1243 / 1245, joined by two `PyNumber_Add` (L8257, L8282). No reorder occurs between.

3. **Default front order is `[HLOToTensorizer, {XLA}InferGoldens, Frontend, StaticIOTranspose]`.** ✔ HIGH. The four `PyList` slot stores are explicit at L817/819/822/825 with constants `n_u_HLOToTensorizer`, the goldens choice, `n_u_Frontend`, `n_u_StaticIOTranspose`. The slot indices (`*v44`, `v44[1]`, `v44[2]`, `v44[3]`) fix the order. The only variability is `[1]` (goldens choice), driven by the `framework == XLAInterface` RichCompare (L748–807).

4. **`--meta-module` collapses front to `[HLOToTensorizer, Frontend]` and pins `logical_nc_config = 1`.** ✔ HIGH. The meta-module arm (reached when `args.meta_module` is truthy, L679/L687/L894) does `tp_setattro(self, logical_nc_config, int_1)` (L896, py 1642) then `PyList_New` with `n_u_HLOToTensorizer` (L909) and `n_u_Frontend` (L913) — exactly two slots, py 1643.

5. **The backend sub-list is gated by a four-condition AND and `Kelper` is unconditional within it.** ✔ HIGH. The nested-`if` reads `enable_internal_modular_compilation` (L743) → `enable_internal_bir_linker` (L769) → `layer_unroll_factor > 0` (L796/801) → `internal_run_standalone_legacy_compilation` (L836); `Backend` appends only on the legacy arm (L946), and *both* arms reach `LABEL_83` (the non-legacy arm via `if (!v34) goto LABEL_83`, L865) which appends `Kelper` (L868–882). `NeffWrapper` is then gated by `enable_internal_neff_wrapper` (L886/L912).

**Tagged INFERRED / not fully traced:**
- The exact code→`[F134]/[F137]/[F139]` exitcode thresholds in `handleError` (§7) — branch reconstructed, MED.
- Whether `MetaInferGoldens` ever substitutes for `InferGoldens` in slot `[1]` (the modular/meta path may route goldens through `MetaInferGoldens`; the default-arm store is `InferGoldens`/`XLAInferGoldens` only) — MED.
- The keep/drop semantics of each branch in the py-1252 name-filter loop (`Watchpoint` / `BIRSim` / goldens `Equals` tests) — the four `Equals` constants are CERTAIN, the per-branch action MED.
- `print_dots` / `print_dot_context` fork mechanics in `runPipeline` — MED (`@contextmanager` reconstruction).
