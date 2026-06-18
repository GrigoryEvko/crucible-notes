# Autotuner Orchestration and the Reward Backends — the Closed Loop

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The orchestration and reward machinery is **not** in `libwalrus.so` — it is three Cython extension modules under `neuronxcc/starfish/penguin/targets/transforms/autotune/`: `Autotuner.cpython-310-x86_64-linux-gnu.so`, `_Compiler.cpython-310-x86_64-linux-gnu.so`, and `_PerformanceMetric.cpython-310-x86_64-linux-gnu.so` (all cp310, full DWARF, NOT stripped). Every `@addr` below is the file offset of the `__pyx_pw_*` wrapper (the public method entry) in its own `.so`, recovered by `nm`; every rodata offset was recovered by `grep -abo` over that `.so`. The cp311/cp312 wheels carry the same classes at different offsets — treat every address as version- and ABI-pinned. The only piece of this loop that lives in `libwalrus.so` is the *producer* of the reward scalar (`PerfSimPass`, `addMetric(42…)` @ `0x1742020`), which is covered by [`perf-sim-wiring.md`](./perf-sim-wiring.md) and referenced, not re-derived, here.*

## Abstract

The penguin autotuner is a **closed compile→profile→reward→retune loop that crosses a process boundary**. A search strategy (MCTS / Exhaustive / Greedy / Random) proposes a *design* — an ordered list of plan IDs. To score one design, the autotuner does not simulate it in-process: it **re-invokes `neuronx-cc compile` as a child subprocess** that recompiles the kernel with that design applied, and that child writes its predicted latency to a JSON file. The parent reads the file back, turns the latency into a scalar reward, and feeds it to the search. The loop is therefore closed *across two `neuronx-cc` processes* — the reward is never an in-memory return value and never an IPC socket; it is a file on disk (`global_metric_store.json`, key `["metrics"]["nc_latency"]`) that the child writes and the parent reads.

This page recovers the orchestration spine and the two reward backends. The spine (`Autotuner.so`) owns the plan model, an `os.fork()` design-space fan-out, and the strategy dispatch; the child-compile backend (`_Compiler.so`) owns the `subprocess.Popen` re-invocation with its forced flag set and the partial-passlist replay that lets each candidate skip the front-end; the reward layer (`_PerformanceMetric.so`) owns the two backends — `PostSchedMetric` (in-process, reads the perf-sim JSON estimate) and `NeuronBenchMetric` (out-of-process, shells `neuron-bench` to a real NeuronCore and reads measured latency back). The MCTS engine that drives the search is the subject of the in-flight Part 8.50 sibling (`walrus/penguin-autotuner.md`) and is named, not re-derived, here.

The recovery target is reimplementation grade: a reader should be able to rebuild the two distinct process fan-outs (the parent's `os.fork()` + `Semaphore` + `fcntl.flock` design-space generation versus the metric layer's `ThreadPoolExecutor` + `Popen` per-candidate compile), the exact child argv the parent injects, the byte-exact JSON path and key the reward is read from, the `+inf` failed-compile sentinel and the percent reward formula, and — the deliverable — the **three reward regimes**, of which exactly one is this measured closed loop.

| | |
|---|---|
| **Orchestration module** | `Autotuner.cpython-310…so` (1,874,320 B, BuildID `c8e11d47…`) |
| **Compile backend** | `_Compiler.cpython-310…so` (1,164,176 B, BuildID `e4ed7d7b…`) |
| **Reward backend** | `_PerformanceMetric.cpython-310…so` (1,016,328 B, BuildID `c3b87ce3…`) |
| **Build root** | `/opt/workspace/KaenaCompilerNativeBuild-310/build/private/src-3.10.16` (DWARF) |
| **Orchestrator driver** | `_MainAutotuner._do_tuning` @ `0x46f30` (`Autotuner.so`) |
| **Fork fan-out** | `_DesignSpaceGenerationAutotuner._tuning` @ `0x22f70` (`Autotuner.so`) |
| **Child-result writer** | `_DesignSpaceGenerationAutotuner._return` @ `0x48960` (`Autotuner.so`) |
| **Compile-and-measure core** | `PerformanceMetric.evaluate_with_subprocess_result` @ `0x20a60` (`_PerformanceMetric.so`) |
| **Batch (parallel) eval** | `PerformanceMetric.evaluate_batch` @ `0x25220`; `future_task` closure @ `0x17290` |
| **Child compile** | `MainCompiler.compile_subprocess` @ `0x187a0`; base `Compiler.compile_subprocess` @ `0x25820` |
| **Argv builder** | `Compiler._generate_popen_command` @ `0x23e90` (`_Compiler.so`) |
| **Command recovery** | `Compiler._find_compiler_cmd` @ `0x14590`; regex `neuronx-cc compile(.*?)(?=\n|$)` @ rodata `0x33a60` |
| **Reward — PostSched** | `PostSchedMetric._evaluate` @ `0x236c0` (`_PerformanceMetric.so`) |
| **Reward — device** | `NeuronBenchMetric._evaluate_on_remote` @ `0x1cb00` (`_PerformanceMetric.so`) |
| **Reward file** | `<comp_dir>/[sgLnk/]global_metric_store.json` (rodata `0x2d4a0` / `0x2d4a7`), key `["metrics"]["nc_latency"]` |
| **Failed-compile sentinel** | `+inf` (rodata `0x2dfb4`); percent divisor `100.0` (rodata `0x2dfbc`) |
| **Final design file** | `autotune_result_design.json` (`RESULT_DESIGN_FILE`) |

> **CORRECTION (vs the task brief, "the autotune_id (uuid generation)"):** `Autotuner.so` does **not** generate UUIDs. It imports no `uuid` module and contains no `uuid4`/`getnode` symbol. `autotune_id` is a **client-provided** stable field that the autotuner only *reads* (or substitutes the plan's list index when absent). The word "uuid" in the `add_plan` docstring is about *cross-process plan identity* — so a child subprocess can name the same plan the parent named — not in-process uniqueness. This is corrected throughout. [CONFIRMED — no `uuid` import in the DWARF module-const table; verified `nm` symbol set.]

---

## 1. The reimplementation contract

To rebuild this loop you must reproduce, in order:

- **The plan / design model.** A *plan* is an opaque client object; a *design* is an ordered list of plan IDs (one per tuning depth). Plan IDs are either client-supplied (`plan.autotune_id`) or the plan's list index — and the two cannot be mixed.
- **Two distinct process fan-outs.** (A) The parent's `os.fork()` + `Semaphore(max_processes)` + `fcntl.flock` loop that generates the design space (`Autotuner.so`). (B) The metric layer's `concurrent.futures.ThreadPoolExecutor` that fans candidate *compiles*, each of which `Popen`s one child `neuronx-cc` (`_PerformanceMetric.so` + `_Compiler.so`). These are different boundaries; conflating them is the classic mistake.
- **The child argv.** Recover the original `neuronx-cc compile …` from the build log via regex, then inject the forced flag set (`--internal-autotune-subprocess`, `--no-internal-autotune`, `--no-fork-subcommand`, `--run-pass-list=remaining_passes.json`, `--internal-tensorizer-opt-level=nki`, …).
- **The partial-passlist replay.** `dump_passes.json` → `remaining_passes.json` (slice from the autotuned pass) so the child resumes from the tuned pass instead of re-running the front-end.
- **The reward read-back.** Open `<comp_dir>/[sgLnk/]global_metric_store.json`, `json.load`, read `["metrics"]["nc_latency"]`; a failed child scores `+inf`; reward is `((baseline − val) / baseline) · 100`.
- **The three reward regimes** — and the fact that only the subprocess-measured `nc_latency` one is this closed loop.

---

## 2. The plan and design model `[CONFIRMED]`

A **plan** is an opaque client object — for the one real consumer (Tritium fusion, §7) it is a `FusionPlan`, a fusion/tiling decision for one matmult. `Autotuner.so` treats it duck-typed: it reads at most an optional `.autotune_id` (the stable cross-process key) and an optional `.json()` (for human-readable design dumps). `None` is a valid plan meaning "no change."

A **design** is an *ordered list of plan IDs*, one entry per tuning-call depth. The search builds a design incrementally (`design = parent.design + [plan_id]`), and the autotuner replays a design by feeding the depth-`d` plan ID back on the `d`-th tuning call.

The three plan-entry methods are mutually exclusive with the one-shot `tune()`:

| Method | `@addr` | Role |
|---|---|---|
| `Autotuner.add_plan` | `0x25180` | append one plan; reads `.autotune_id` or assigns the list index |
| `Autotuner.add_plans` | `0x35e50` | append a list **or** store a lazy `Callable[[], list]` plan generator |
| `Autotuner.add_no_change_plan` | `0x1e6c0` | append `None` (the baseline candidate) |
| `Autotuner.tune` | `0x386f0` | one-shot: `tune(plans, baseline, baseline_behavior)`; `tune.<lambda>` @ `0x38170` is the default baseline closure |

### Plan-ID assignment and the mixing guard

`_AutotunerBase._generate_plan_dict` @ `0x473a0` enumerates the plans and builds a `{plan_id → plan}` dict. For each plan: if it carries `.autotune_id`, that is the (manual) ID; otherwise the ID is the enumeration index `i`. The dict is what `_fetch_plan_from_design` @ `0x290b0` uses to map an ID *received from a child subprocess* back to the live plan object — which is the entire reason plan IDs must be process-stable.

The guard string `"Mixing automatic and manual plan ids is not supported."` (rodata `0x52ee0`) is raised if some plans carry `autotune_id` and others do not. This is the model's one hard invariant: a design's IDs mean the same thing in every process only if the whole plan set uses one ID scheme.

```c
// Autotuner.so — _generate_plan_dict @ 0x473a0  (reconstructed; STRONG)
//   plan_dict maps a process-stable id -> the live plan object.
static PyObject *generate_plan_dict(self, PyObject *plans) {
    PyObject *plan_dict = PyDict_New();
    int use_automatic_ids = -1;                 // tri-state: undecided
    for (Py_ssize_t i = 0; i < len(plans); i++) {
        PyObject *p   = plans[i];
        int has_manual = PyObject_HasAttrStr(p, "autotune_id");   // CONFIRMED attr read
        if (use_automatic_ids == -1)
            use_automatic_ids = !has_manual;
        else if (use_automatic_ids == has_manual)                 // mixed -> fatal
            PyErr_SetString("Mixing automatic and manual plan ids is not supported.");
        PyObject *id = has_manual ? getattr(p, "autotune_id")
                                  : PyLong_FromSsize_t(i);        // index fallback
        PyDict_SetItem(plan_dict, id, p);
    }
    return plan_dict;                            // -> _fetch_plan_from_design
}
```

`_record_plan_to_design` @ `0x2c5d0` is the inverse on the write side (append the chosen plan's ID to the running design). Together with `_fetch_plan_from_design` these are the read/write ports of the design that the `Autotuning` context manager (§6) uses to dictate plans on replay.

---

## 3. The class taxonomy — three modules, one loop `[CONFIRMED]`

The loop is split across three `.so` by responsibility. `nm` recovers every class and method as a `__pyx_pw_*` wrapper symbol; the offsets are firsthand.

### `Autotuner.so` — orchestration

`Autotuner` is the public facade (`build`/`is_active`/`add_plan*`/`tune`/`tuning`/`release`). `build` @ `0x32870` is the gated factory: it reads the enable bit and returns a real `Autotuner` or a no-op `_BypassedAutotuner`. Behind the facade is a role tree, each role a `_create`/`_tuning`/`_return` triple selected by the per-process discriminator (§5):

| Role class | Purpose | Key method `@addr` |
|---|---|---|
| `_MainAutotuner` | **original process**: owns the search | `_do_tuning` @ `0x46f30`, `_generate_design_space` @ `0x2da50` |
| `_DesignSpaceGenerationAutotuner` | **extraction child**: forks, emits the design tree | `_tuning` @ `0x22f70`, `_return` @ `0x48960` |
| `_MCTSHelperAutotuner` | **candidate child** (MCTS): replays a partial design | `_tuning` @ `0x3e160`, `result_info` @ `0x40700` |
| `_SequentialHelperAutotuner` | **candidate child** (flat search) | `_tuning` @ `0x417e0` |
| `_SubprocessAutotuner` | base for the helpers; builds the `_Compiler` command | `build` @ `0x3b040`, `result_info` @ `0x206c0` |
| `_DesignedAutotuner` | **replay role**: applies a frozen winning design | `_tuning` @ `0x21960`, `_create` @ `0x3f970` |
| `_BaselineAutotuner` | baseline-only run | `_tuning` @ `0x1d300` |
| `_BypassedAutotuner` | no-op stand-in when disabled | `tuning` @ `0x2aa70`, `release` @ `0x371b0` |

The imports `Autotuner.so` actually pulls (DWARF module consts) are `os` (`fork`/`cpu_count`/`_exit`/`getcwd`), `fcntl` (`flock`/`LOCK_EX`/`LOCK_UN`), `gc`, `json`, `contextlib.contextmanager`. Crucially: **no `subprocess`/`Popen` in `Autotuner.so` itself** — the parent's fan-out here is raw `os.fork`; the `neuronx-cc` re-invocation lives one module over in `_Compiler.so`. [CONFIRMED — `fork`@rodata `0x553b3`, `flock`@`0x552ee`, `LOCK_EX`@`0x551f8`, `LOCK_UN`@`0x551f0`, `cpu_count`@`0x54fd0`, `_exit`@`0x55291`; `os.fork`/`os.cpu_count`/`os._exit` all called firsthand in `_tuning` @ `0x22f70`, decompiled body lines for `cpu_count`/`fork`/`_exit`.]

### `_Compiler.so` — the child compile

`Compiler` (base) / `MainCompiler` (the live path) / `SubprocessCompiler` (dormant). Methods recover the original command, build the child argv, run it, and extract the partial passlist:

| Method | Class | `@addr` |
|---|---|---|
| `_find_compiler_cmd` | `Compiler` | `0x14590` |
| `_generate_popen_command` | `Compiler` | `0x23e90` |
| `compile_subprocess` | `Compiler` | `0x25820` |
| `compile_subprocess` (override) | `MainCompiler` | `0x187a0` |
| `_do_penguin_extraction` | `MainCompiler` | `0x1e820` |
| `_extract_partial_passlist` | module fn | `0x2d5e0` |
| `_copy_partial_compilation_files` | module fn | `0x2c540` |
| `_copy_kernels_and_weights` | module fn | `0x2b3c0` |

`SubprocessCompiler` is explicitly dormant — its docstring (rodata `0x33503`) reads *"This class is unused, but it could be useful someday to be able to recursively call subprocesses (?)"*. The live per-candidate compile is always `MainCompiler.compile_subprocess` @ `0x187a0`. [CONFIRMED]

### `_PerformanceMetric.so` — the reward

`PerformanceMetric` is the abstract base (`_evaluate` raises `NotImplementedError`); the two concrete leaves are the two backends (§8):

| Class | Hierarchy | Override | `@addr` |
|---|---|---|---|
| `PerformanceMetric` | base (abstract `_evaluate`) | — | `_evaluate` @ `0x14be0` |
| `PostSchedMetric` | ⊂ `PerformanceMetric` | `_evaluate` | `0x236c0` |
| `RemotePerformanceMetric` | ⊂ `PerformanceMetric` | `__init__`, `_evaluate`, `_evaluate_on_remote` | `_evaluate` @ `0x1a250` |
| `NeuronBenchMetric` | ⊂ `RemotePerformanceMetric` | `_evaluate_on_remote`, `verify_machine_baselines` | `_evaluate_on_remote` @ `0x1cb00` |

The compile-and-measure core and the two parallel-eval entry points are on the base:

| Method | `@addr` | Role |
|---|---|---|
| `evaluate_with_subprocess_result` | `0x20a60` | compile one design → `(metric, sp_result)` |
| `evaluate_batch` | `0x25220` | thread-pool fan a batch of compilations |
| `evaluate_designs` | `0x13ed0` | designs → compilations → `evaluate_batch` |
| `evaluate_baseline` | `0x15c10` | the no-tuning reference metric |
| `build` | `0x1b9c0` | select the backend class by `metric_name` |
| `_new_compilation_dir` | `0x166e0` | mint a fresh per-candidate `comp_dir` |

`PerformanceMetric.build` @ `0x1b9c0` walks `cls.__subclasses__()` (a genexpr) and matches the class name against the config `metric_name` (`"PostSchedMetric"` / `"NeuronBenchMetric"`). The base `_evaluate` @ `0x14be0` loads `NotImplementedError` and raises — it is a pure abstract hook the leaves replace. [CONFIRMED]

---

## 4. The two process fan-outs — keep them separate `[CONFIRMED]`

This is the most-confused part of the system, so it is the first concrete mechanism. There are **two** independent process/thread fan-outs, in two different modules, at two different boundaries.

### Fan-out A — `os.fork()` design-space generation (`Autotuner.so`)

When the chosen search strategy needs a pre-enumerated design space (Exhaustive does; MCTS does not — it explores lazily), `_MainAutotuner._generate_design_space` @ `0x2da50` builds the design tree by forking one child per design branch. The fork loop is in `_DesignSpaceGenerationAutotuner._tuning` @ `0x22f70`, and it is raw POSIX: `os.cpu_count` bounds the width, `os.fork` spawns, `os._exit` terminates the child, a `Semaphore` throttles concurrency, and `fcntl.flock(LOCK_EX … LOCK_UN)` guards the shared result files. [CONFIRMED — `os.cpu_count`, `os.fork`, `os._exit` all called firsthand in the decompiled `_tuning`; `Semaphore.release` and `flock` interned; log `"Forking off child processes to explore the design space"` @ rodata `0x52f20`.]

```c
// Autotuner.so — _DesignSpaceGenerationAutotuner._tuning @ 0x22f70  (reconstructed; STRONG)
//   PARENT-SIDE fan-out: one forked child per design branch, throttled by a semaphore.
void design_space_tuning(self, PyObject *plans_list) {
    long max_processes = min(self->max_processes, os.cpu_count());   // CONFIRMED cpu_count call
    Sem  *sem = Semaphore(max_processes);
    log("Forking off child processes to explore the design space");
    for (each (plan_id, plan) in plans_list) {
        sem.acquire();
        pid_t pid = os.fork();                       // CONFIRMED os.fork call @ body
        if (pid == 0) {                              // CHILD
            evaluate_branch(plan_id, plan);          // walks into _Compiler (fan-out B)
            write_result_file_locked(plan_id);       // see _return @ 0x48960
            os._exit(0);                             // CONFIRMED os._exit call @ body
        }
        // PARENT loops; sem.release() runs on child reap
    }
}
```

The child writes its branch result under an `fcntl.flock` exclusive lock so concurrent siblings do not corrupt the shared design dump; the writer path is `_DesignSpaceGenerationAutotuner._return` @ `0x48960` (which references the `fcntl`/`flock` builtins). The aggregated tree is dumped to `generated_design_space_raw.txt` (rodata `0x53b00`), and `_generate_design_space` warns *"WARNING: IF THE MODEL IS VERY LARGE DESIGN SPACE GENERATION MAY USE UP ALL RAM…"*.

> **QUIRK — this fan-out compiles nothing.** Fan-out A only *enumerates and trees* the design space. The actual measurement (compile + perf-sim) happens in fan-out B, one level down. A reimplementer who fuses them will double-fork.

### Fan-out B — `ThreadPoolExecutor` per-candidate compile (`_PerformanceMetric.so`)

The metric layer fans the candidate *compiles* across a `concurrent.futures.ThreadPoolExecutor`. `evaluate_batch` @ `0x25220` submits one `future_task` per compilation, collects via `as_completed`, and re-indexes results back to input order through a `future_to_idx` dict. Each thread is I/O-bound because the real work — the child `neuronx-cc` — runs in a separate `Popen`ed process (§5). [CONFIRMED — `ThreadPoolExecutor`@rodata `0x2d810`, `as_completed`@`0x2dad8`, `future_to_idx`@`0x2da48`, `concurrent`@`0x2db90`; `future_task` closure wrapper @ `0x17290`.]

```c
// _PerformanceMetric.so — evaluate_batch @ 0x25220  (reconstructed; CONFIRMED primitives)
//   compilations: list of (_SubprocessAutotuner, args). Threads are I/O-bound on the child.
PyObject *evaluate_batch(self, PyObject *compilations, int return_sp_result) {
    ThreadPoolExecutor *ex = ThreadPoolExecutor();
    dict future_to_idx = {};                                  // Future -> input index
    for (i, comp) in enumerate(compilations) {
        Future *f = ex.submit(future_task, comp);             // future_task @ 0x17290
        future_to_idx[f] = i;                                 // restore input order later
    }
    PyObject *results = list_of_len(len(compilations));
    for (Future *f : as_completed(future_to_idx)) {
        long idx = future_to_idx[f];
        results[idx] = f.result();                            // (metric[, sp_result])
    }
    return results;
}
// future_task = evaluate_with_subprocess_result(comp)        // the per-future work item
```

So: **fan-out A is `os.fork` in `Autotuner.so` for design-space *enumeration*; fan-out B is `ThreadPoolExecutor`+`Popen` in `_PerformanceMetric.so`/`_Compiler.so` for candidate *compilation*.** Different module, different primitive, different purpose.

---

## 5. The child compile — `subprocess.Popen` of `neuronx-cc compile` `[CONFIRMED]`

`evaluate_with_subprocess_result` @ `0x20a60` is the compile-and-measure core (docstring rodata `0x2cc45`: *"Evaluate a compilation and return both its metric result and subprocess result."*). It builds a `_Compiler.Compiler`, runs the child, evaluates the result, and returns the tuple the search consumes:

```c
// _PerformanceMetric.so — evaluate_with_subprocess_result @ 0x20a60  (STRONG)
PyObject *evaluate_with_subprocess_result(self, PyObject *design_or_compilation) {
    Compiler *compiler = MainCompiler(*compilation_args);    // a _Compiler.Compiler
    SubprocessResult *sp = compiler.compile_subprocess();    // §5.2 — Popen the child
    PyObject *metric = self._evaluate(sp->comp_dir);         // §8 — read the reward
    return PyTuple_Pack(2, metric, sp);                      // (metric, sp_result)
}
```

### 5.1 Recovering and re-assembling the child argv

The child must run *the same* `neuronx-cc compile` the user ran, plus a forced delta. `Compiler._find_compiler_cmd` @ `0x14590` recovers the original by scraping the build log `log-neuron-cc.txt` (rodata `0x33c40`) with the verbatim regex `neuronx-cc compile(.*?)(?=\n|$)` (rodata `0x33a60`); on failure it raises *"No neuronx-cc compile command found in the log file "* (rodata `0x333c0`).

`Compiler._generate_popen_command` @ `0x23e90` then rewrites the argv through two inner closures — `ensure_flags` (dedups/forces required flags, guarding "arg passed twice") and `modify_tensorizer_opts` (edits the tensorizer-options slot to force `--internal-tensorizer-opt-level=nki`). The forced/injected flags, every fragment confirmed in `_Compiler.so` rodata:

| Flag (stored fragment) | rodata offset | Meaning |
|---|---|---|
| `internal-autotune-subprocess` | `0x336c2` | marks this child as the subcompile for plan/design `<id>` |
| `internal-autotune-extraction` | `0x33762` | extraction-process mode (design-tree gen) |
| `internal-autotune-config` | `0x33882` | pass-by-pass enable map / config path |
| `no-internal-autotune` | `0x33a42` | child must **not** recurse autotuning |
| `no-fork-subcommand` | `0x33b63` | child does not fork further |
| `internal-tensorizer-opt-level=nki` | `0x334c2` | forced on by `modify_tensorizer_opts` |
| `run-pass-list` | `0x32ea4` | partial-passlist replay (§6) |
| `dump-pass-list-and-exit` | `0x33902` | extraction-time passlist capture |
| `tensorizer-options` | `0x33b22` | the tensorizer-options slot |
| `verbose=info` | `0x33eca` | child verbosity |
| `pipeline` | `0x341b2` | pipeline selector |

> **QUIRK — the flags are stored without their `--` prefix.** The rodata fragments are `internal-autotune-subprocess`, `no-internal-autotune`, etc.; the `--` is prepended at argv-assembly time. Grepping the binary for `--internal-autotune-subprocess` returns nothing — search the bare fragment.

The two flags that make the loop *terminate* are `--no-internal-autotune` and `--no-fork-subcommand`: without them the child would itself autotune and fork, and the loop would not close. The `--internal-autotune-subprocess=<id>` flag is how the child learns *which plan it is the subcompile for* — the child re-decodes that plan ID through the same `apply_plan` the parent uses (§7), which is why the ID must be process-stable.

### 5.2 Running the child and the result struct

`Compiler.compile_subprocess` @ `0x25820` (`MainCompiler` override @ `0x187a0`) runs the child with `subprocess.Popen`, routes `stdout`/`stderr` to `DEVNULL`, then `.wait()`s and reads `.returncode`. It logs `"Launching " <cmd>` on entry and `"Subprocess returned non-0 return code: "` on a non-zero child, and returns a `SubprocessResult` carrying `returncode + log_content + comp_dir`. [CONFIRMED — `Popen` + module-global `subprocess`; `DEVNULL`@rodata `0x342b8`, `wait`/`returncode` interned; `"Launching "`@`0x34160`, `"Subprocess returned non-0 return code: "`@`0x33240`.]

```c
// _Compiler.so — compile_subprocess @ 0x25820 / MainCompiler override @ 0x187a0  (CONFIRMED)
SubprocessResult *compile_subprocess(self) {
    PyObject *argv = self._generate_popen_command();     // §5.1
    log("Launching ", argv);
    Popen *child = subprocess.Popen(argv, stdout=DEVNULL, stderr=DEVNULL);
    child.wait();                                         // block until the child compile finishes
    long rc = child.returncode;
    if (rc != 0) log("Subprocess returned non-0 return code: ", rc);
    // the child has by now written comp_dir/global_metric_store.json (§8)
    return SubprocessResult(returncode=rc, log_content=..., comp_dir=self->comp_dir);
}
```

---

## 6. Partial-passlist replay — the speed optimization `[CONFIRMED]`

Recompiling each candidate from scratch would re-run the entire front-end for a change that only affects one late pass. The replay machinery avoids that. `_extract_partial_passlist` @ `0x2d5e0` (docstring rodata `0x32e83`: *"Extracts a dump-pass-list into a run-pass-list which can be used to compile penguin from the start"*) reads the full penguin pass list the parent dumped (`dump_passes.json`, rodata `0x33d00`), slices from `start_index` (the autotuned pass) to the end, and writes `remaining_passes.json` (rodata `0x331ec`). The child is then launched with `--run-pass-list=<remaining_passes.json>` and resumes from the tuned pass instead of the front-end. On failure: *"Partial compilation extraction failed."* [CONFIRMED]

`MainCompiler._do_penguin_extraction` @ `0x1e820` produces the hermetic replay bundle (copied into `../tmp`): `command.json` (the serialized child command, rodata `0x331b5`), `command_output.json` (the child's emitted result, rodata `0x33ac1`), `compilation_info.json`, `remaining_passes.json` + `dump_passes.json`, `penguin.py` (the replay driver), and `replay.sh` (a standalone reproducer, rodata `0x3320c`). `_copy_partial_compilation_files` @ `0x2c540` and `_copy_kernels_and_weights` @ `0x2b3c0` copy the `*.npy` kernels/weights and `.neff`/penguin artifacts so the replay is self-contained. The cross-process file round-trip is therefore: parent dumps `dump_passes.json` → extracts `remaining_passes.json` → child runs `--run-pass-list` → child writes `command_output.json` + `global_metric_store.json` → parent reads them back.

---

## 7. Strategy dispatch and what actually drives the loop `[CONFIRMED / STRONG]`

`_MainAutotuner._do_tuning` @ `0x46f30` is the orchestrator. Its control flow is fixed by the verbatim ordering of its log messages:

```c
// Autotuner.so — _MainAutotuner._do_tuning @ 0x46f30  (STRONG; log order is verbatim)
PyObject *do_tuning(self) {
    log("Starting MainAutotuner for ", ...);
    metric = PerformanceMetric.build(metric_name, ...);          // §3, §8 — PostSched|NeuronBench
    log("Evaluating metric of baseline...");
    baseline_metric = metric.evaluate_baseline(...);             // §8 — no-tuning reference
    search = Search.build(search_name, search_settings);         // Exhaustive|Greedy|Random|MCTS
    if (search.requires_design_space)
        design_tree = self._generate_design_space();             // §4 fan-out A (forks)
    log("Starting search. Search has timeout limit of ", search_timeout);
    best_design = search.run(metric, design_tree, timeout);      // each design -> §5 child compile
    // ... §9 aggregate + apply
}
```

`search_name` (config key `search_algorithm`) selects the strategy: `ExhaustiveSearch` / `GreedySearch` / `RandomSearch` resolve in the sibling `_Search.so`; `MCTS` resolves to `_TreeSearch.so` (the `MonteCarloTreeSearch` / `MCTSTuneState` engine — Part 8.50, `walrus/penguin-autotuner.md`, in flight). `Search.requires_design_space` decides whether fan-out A runs first: Exhaustive needs the pre-enumerated tree; MCTS explores lazily and skips it (warning *"…CONSIDER RUNNING SEARCH ALGORITHM WHICH DOES NOT REQUIRE DESIGN SPACE GENERATION."*). Whichever strategy runs, every candidate design it proposes is scored the same way — `metric.evaluate_with_subprocess_result(design)` (§5) → child compile → reward.

The per-process role (§3) is gated by the discriminator consts `is_original` / `tuning_with_design` / `tuning_depth`: the **original** process is `_MainAutotuner` (owns the search); an **extraction** child is `_DesignSpaceGenerationAutotuner` (emits the tree); a **candidate** child is `_MCTSHelperAutotuner` / `_SequentialHelperAutotuner` (receives a partial design via `--internal-autotune-subprocess`, replays it through `_fetch_plan_from_design` until `tuning_depth` exceeds the partial design, then falls back to baseline behavior, and finally `_return`s the complete design with its metric carried in `command_output.json`).

### The one real consumer

The entire `os.fork` + `Popen` + `nc_latency` machinery exists to autotune **one** thing: Tritium fusion plans. The autotune package is imported by exactly one non-autotune module — `TritiumFusion.so` — whose `AutotuneFusionPlanGenerator.best_fusion_plan` adds the fusion plans and opens `with self.autotuner.tuning():`. A plan ID is a `FusionPlan` ID of the form `"subgraph:id"` (e.g. `sg0001:7041`, from the `internal-autotune-tritium-only-with-id` help string); inside both the parent and each child the same ID decodes through `FusionPlanGenerator.apply_plan` (`vectorize_matmult → iterative_fuse_consumers → tile → fuse`). That decode lives in `TritiumFusionBase`, **not** in `transforms.Region` (a CFG dominator-tree utility with zero plan-ID strings). [CONFIRMED — sourced from D-L10's firsthand `TritiumFusion`/`apply_plan` trace; named here for context, not re-derived on this page.]

---

## 8. The two reward backends `[CONFIRMED]`

`PerformanceMetric.build` @ `0x1b9c0` selects the leaf by `metric_name`. The two leaves are the two reward backends — one in-process estimate, one out-of-process measurement.

### 8.1 `PostSchedMetric` — the in-process perf-sim estimate (default)

`PostSchedMetric._evaluate` @ `0x236c0` (docstring rodata `0x2cf40`: *"Should extract Post Schedule Estimated Latency from metrics store. "*) reads the reward from a **file the child compile wrote**. It builds the path by `PyNumber_Add` of `comp_dir` + `"/[sgLnk/]global_metric_store.json"`, `json.load`s it, and reads `["metrics"]["nc_latency"]` (via chained `.get`). The scalar is `PostSchedEstLatency` in perf-sim cycles — `BackendMetricType` #42, the same value `libwalrus` `PerfSimPass` writes via `addMetric(42…)` @ `0x1742020` ([`perf-sim-wiring.md`](./perf-sim-wiring.md)). There is no IPC: the reward surface is a per-candidate JSON file. [CONFIRMED — both path strings referenced firsthand in `_evaluate`'s disasm (`/sgLnk/global_metric_store.json` and `global_metric_store.json`); `json.load` then a `.get` chain firsthand in the decompiled body; `nc_latency`@rodata `0x2db70`, `PostSchedEstLatency`@`0x2d710`, `metric_store_file_path`@`0x2d690`.]

```c
// _PerformanceMetric.so — PostSchedMetric._evaluate @ 0x236c0  (CONFIRMED path/key; STRONG arithmetic)
double postsched_evaluate(self, PyObject *comp_dir) {
    PyObject *path = PyNumber_Add(comp_dir, "/global_metric_store.json");  // or "/sgLnk/..."
    PyObject *data = json.load(open(path));                                // CONFIRMED json.load
    PyObject *m    = data.get("metrics");                                  // CONFIRMED .get chain
    PyObject *val  = m.get("nc_latency");                                  // == PostSchedEstLatency #42
    return PyFloat_AsDouble(val);                                          // perf-sim cycles
}
```

> **QUIRK — `global_metric_store.json` and `hlo_metrics.json` are the same store.** Both are the singleton `MetricStore` serialized to disk; `_PerformanceMetric.so` names the autotuner-facing copy `global_metric_store.json`. The `/sgLnk/` variant is the symlinked per-compile working dir. [INFERRED — `/sgLnk/` meaning from path shape.]

### 8.2 `NeuronBenchMetric` — the out-of-process device benchmark

`NeuronBenchMetric` ⊂ `RemotePerformanceMetric` is the real-hardware path. Instead of reading a perf-sim estimate, `_evaluate_on_remote` @ `0x1cb00` runs `neuron-bench` on a benchmark machine via `neuronxcc.kra.profile_lib` — the *same* `NeuronBench` / `profile_lib` path the public `nki.benchmark` entrypoint uses to populate `.benchmark_result.nc_latency` ([`../nki/entrypoints.md`](../nki/entrypoints.md)). The measured latency reads back the same way the estimate does — into the autotuner's scalar reward, keyed `nc_latency`. There is no raw `ssh`/`scp` (no `paramiko`/`ssh`/`scp` strings); device access is mediated by `profile_lib`. [CONFIRMED — `neuron-bench`@rodata `0x2d32f`, `profile_lib`@`0x2d55e`, `remote_benchmark_machines`@`0x2d2ec`, `verify_machine_baselines`@`0x2c33c`, `run-as-cc-neff`@`0x2d9f0`/`/file.neff`@`0x2dc28` interned.]

The device path adds three things the perf-sim path does not need:

- **A retry loop** keyed on config `max_retries` / `cooldown_between_retries`: *" failed to run neuron-bench. Retrying in "* then *" failed to run neuron-bench after " … " attempts. Giving up."*.
- **Per-machine locking** (`machine_lock` / `_machine_locks`) to serialize device access across the thread pool.
- **Baseline verification** (`verify_machine_baselines` @ `0x22160`): benches a known baseline on each machine and warns if machines disagree — *"WARNING: Baseline metrics are inconsistent between machines (differ by >= " … "%). Autotuning results cannot be trusted. Metrics: "* — at a ≥50% divergence threshold. [STRONG — `50` const + `%`-format present; comparator from the WARNING string.]

If `metric_name == "NeuronBenchMetric"` but `remote_benchmark_machines` is empty, it errors: *"A PerformanceMetric which uses remote benchmarking machines was used, but the 'remote_benchmark_machines' setting was empty."* [CONFIRMED]

### 8.3 The failed-compile sentinel and the reward formula

A failed (non-zero) child compile is **not dropped** — it is scored `+inf`, so it is the worst possible latency, gets the minimum reward, and competes-and-loses. The relevant float constants sit contiguously in `_PerformanceMetric.so` rodata: `+inf` at `0x2dfb4` (bytes `00 00 f0 7f` little-endian = `0x7ff0000000000000`) immediately followed by `100.0` at `0x2dfbc` (bytes `00 00 59 40` = `0x4059000000000000`). The `+inf` is loaded in the subprocess-result region (load sites `0x1fb1e`/`0x1fb6e`/`0x200b5`), accompanied by the log `"Subcompilation failed in "` (rodata `0x2d610`). [CONFIRMED — float bytes read firsthand from rodata `0x2dfb0` row: `00000000 0000f07f 00000000 00005940`.]

The reward turns a latency into a percent improvement against the baseline:

```text
reward_f(val) = ((baseline_metric − val) / baseline_metric) · 100      // percent; lower latency wins
```

where `baseline_metric` is `PerformanceMetric.evaluate_baseline` @ `0x15c10` (the no-tuning reference compile; log *"Completed compilation had performance metric "* @ rodata `0x2d200`). [STRONG — the only reward/baseline float consts in the module are `{1.0, −1.0, ±inf, 100.0}`; `100.0` is the percent divisor used in `divsd`.]

> **CORRECTION (vs the prior framing in D-L02 that the reward scale `K` was "1 or 100"):** the divisor is **100** — the reward is a *percent* improvement. The only float constants in the reward/baseline math are `{1.0, −1.0, ±inf, 100.0}`, and `100.0` is the divisor. MCTS *maximises* `reward_f` (lower latency → higher reward); the flat strategies *argmin* the raw `nc_latency`. Same orientation, inverse arithmetic. [STRONG]

---

## 9. Aggregate, apply, and the three reward regimes `[CONFIRMED]`

### 9.1 Pick best, apply to the real compile

After `search.run` returns `(best_design, best_metric)`, `_do_tuning` computes `perf_change = (best_metric − baseline_metric) / baseline_metric` and decides (verbatim format strings; `{:2.2%}` is `perf_change`):

- improvement → *"This is an improvement of {:2.2%}. Using autotuned design."* → keep best.
- regression → *"This was a regression of {:2.2%}. Using baseline."* → keep baseline.
- no change → *"There was no change in the metric. Using baseline."* → keep baseline.

An incomplete winning design is guarded (*"WARNING. The resultant design is incomplete (need plan at depth `<X>`, but design only goes to depth `<Y>`)"*), then the chosen design is persisted (*"Saving autotuned design to "* `autotune_result_design.json`, the `RESULT_DESIGN_FILE`).

The winning design is then **replayed on the in-process compile** through the `Autotuning` context manager (`Autotuner.tuning` @ `0x20d20`; the `Autotuning` generator @ `0x21240`). Each `tuning()` call inside the `with autotuner.tuning():` body returns the plan dictated by `final_design` at the current depth (`_fetch_plan_from_design` @ `0x290b0` / `_record_plan_to_design` @ `0x2c5d0`); past the design's depth it uses `baseline_behavior`. `release` @ `0x36c70` (`_MainAutotuner`) tears down workers/loggers and flushes at pass end — *"Must be called at the end of the autotuner's pass…"*. `_DesignedAutotuner` @ `0x21960` is the role that applies the frozen winning design. [CONFIRMED log strings; STRONG body order.]

### 9.2 The three reward regimes — only one is this loop

The deliverable: the system has **three** distinct reward regimes, and only the first is the subprocess-measured `nc_latency` closed loop documented above.

| Regime | Driver | Search | Reward source | Compile? |
|---|---|---|---|---|
| **A** | `AutotuneFusionPlanGenerator.best_fusion_plan` (autotune **on**) | MCTS / Exhaustive / Greedy / Random / Sequential | `nc_latency` (#42) from `global_metric_store.json` — **measured** | **YES**, subprocess |
| **B** | `best_fusion_plan_search` (autotune **off** — the default) | rtol-band lexicographic beam | static roofline cost (spill → kernel-ii → dma-ii, `rtol=0.01`) | NO |
| **C** | `LayoutState` + `penguin/MCTS.so` (`--layout-transform-heuristic=mcts`) | MCTS (UCT) | `get_action_cost` from a **static** `CycleBasedLayoutCostModel` | NO |

- **Regime A** is the closed loop on this page: each Tritium fusion candidate is compiled in a subprocess and scored by its measured `nc_latency` (or device benchmark). It runs only when `--autotune` (tritium) is on, and it *wraps* Regime B — the static beam still runs *inside* each subprocess as the per-subprocess plan picker; the outer cross-subprocess selection is by `nc_latency`.
- **Regime B** is the default with autotuning off: a coefficient-free roofline beam over `FusionPlan` cost attributes, no compile and no `nc_latency`. It reuses Regime A's `FusionPlan` cost code.
- **Regime C** is a *separate* MCTS over tensor layouts (`LayoutState`, `tonga/LayoutDecisionTree.so`, driven by `penguin/MCTS.so`) scored by a static analytic layout-cycle cost. It does **not** import the autotune package and does **not** read `global_metric_store.json`.

> **CORRECTION (vs the "MCTS layout autotuner" framing in prior reports):** the autotune-package MCTS (`_TreeSearch`) tunes **Tritium fusion** plans by measured `nc_latency`; the **layout** MCTS (`--layout-transform-heuristic=mcts`) is a *different* MCTS instance over a different state type, scored by a *static* `CycleBasedLayoutCostModel`, structurally unrelated to the subprocess-compile loop. They are two unrelated "mcts" knobs and must not be conflated. [CONFIRMED — sourced from D-L10's grep-exhaustive import trace; named here for completeness.]

---

## 10. Confidence, evidence, and what was not traced

**CONFIRMED (firsthand this page):** every `@addr` (recovered by `nm` over the three `.so`); every rodata string offset (`grep -abo`); the `os.fork` / `os.cpu_count` / `os._exit` calls in `_DesignSpaceGenerationAutotuner._tuning` @ `0x22f70` (decompiled body); the `json.load` + `.get` chain in `PostSchedMetric._evaluate` @ `0x236c0` (decompiled body); both reward-file path strings referenced in that body's disasm; the `+inf` (`0x2dfb4`) and `100.0` (`0x2dfbc`) float bytes (rodata dump); the full forced-flag fragment set and the `neuronx-cc compile(.*?)(?=\n|$)` regex in `_Compiler.so`; the `flock`/`LOCK_EX`/`LOCK_UN`/`Semaphore.release` interns; the `ThreadPoolExecutor`/`as_completed`/`future_to_idx` interns.

**STRONG (multi-anchor reconstruction):** statement-level bodies of `_generate_plan_dict`, `_tuning`, `evaluate_batch`, `compile_subprocess`, `_do_tuning` (anchored on log-message ordering + DWARF local-var sets + call-target profiles); the `reward_f` arithmetic and `K = 100` (the only reward float consts are `{1.0, −1.0, ±inf, 100.0}`); the `NeuronBench ⊂ Remote` hierarchy (override set; classes are runtime `Py3ClassCreate`, so `tp_base` is not statically readable); the `verify_machine_baselines` 50% threshold.

**INFERRED / not traced:** the `/sgLnk/` prefix as "symlinked per-compile working dir" (path-shape only); the exact argv *order* `ensure_flags` produces (the slot map is stack-built, not a static string table); the `get_modular_flow_runs(module+420)` trip-count multiplier inside `PerfSimPass` (that is `libwalrus`, [`perf-sim-wiring.md`](./perf-sim-wiring.md) territory, not re-derived here). The MCTS engine internals (UCT, backprop, `compute_exploration_const`) belong to Part 8.50 (`walrus/penguin-autotuner.md`, in flight) and are named, not derived, on this page. The `FusionPlan` → graph-edit decode (`apply_plan`) belongs to the Tritium fusion strand and is summarized in §7 for context only.

**Adversarial self-check of the five strongest claims:**
1. *"Reward is read from a file, not IPC."* — both path strings (`global_metric_store.json`, `/sgLnk/global_metric_store.json`) are referenced firsthand in `_evaluate`'s disasm, and the body calls `json.load(open(path))`. CONFIRMED.
2. *"The parent fan-out is `os.fork`, not `Popen`."* — `os.fork`/`os.cpu_count`/`os._exit` are called firsthand in `_tuning` @ `0x22f70`, and `Autotuner.so` imports no `subprocess` module. CONFIRMED.
3. *"Failed compile → `+inf`."* — the `+inf` float bytes sit at rodata `0x2dfb4` adjacent to the `100.0` divisor, in the subprocess-result region, with `"Subcompilation failed in "` nearby. CONFIRMED (bytes) + STRONG (sentinel semantics).
4. *"Two reward backends, `PostSchedMetric` vs `NeuronBenchMetric`."* — both classes' `_evaluate`/`_evaluate_on_remote` symbols recovered by `nm`; `neuron-bench`/`profile_lib`/`remote_benchmark_machines` interned in the device leaf. CONFIRMED.
5. *"Three regimes, one closed loop."* — Regime A is firsthand here; Regimes B and C are sourced from D-L10's grep-exhaustive import trace (only `TritiumFusion.so` imports the autotune package; the layout MCTS is a separate module) and named here, not independently re-derived this pass. **Ceiling: STRONG, not CONFIRMED, for the B/C delineation** — a reimplementer building only Regime A from this page is fully covered; the existence and separateness of B and C rest on the backing trace, which I did not re-run module-by-module on this page.
