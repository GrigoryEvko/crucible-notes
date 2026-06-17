# JobRegistry & the Sub-Tool Process Model

> *All symbols, byte offsets, and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/driver/`, build root `/opt/workspace/KaenaCompilerNativeBuild-310/build/private/src-3.10.16`). The Job classes are `*.cpython-310-x86_64-linux-gnu.so` ELF — **not stripped, with `debug_info`** — so method-level reconstruction is from `__pyx_*` interned constants, qualnames, and disasm. Other wheels differ; treat every address as version-pinned.*

## Abstract

A `neuronx_cc` compile is a sequence of **Jobs**. [3.3](compilecommand-pipeline.md) describes how `CompileCommand.buildPipeline` concatenates three collectors into an ordered list of job-*name* strings; this page describes the other half: how `JobRegistry` turns each name into a `Job` instance, and how each instance actually *runs* — either **in-process** (pure Python/Cython, no fork) or by **spawning a standalone tool ELF** out of `neuronxcc/starfish/bin/` and waiting on it.

Two mechanisms carry the whole model. First, **discovery**: `JobRegistry.loadAllJobs` walks `neuronxcc.driver.jobs` with `pkgutil.iter_modules`, and `JobRegistry.__getJobFactory` imports each module **wrapped in `sys.setdlopenflags(... | RTLD_DEEPBIND)`** — a per-job dlopen flag that lets every job `.so` carry its own statically-linked copy of XLA/penguin/walrus symbols without clashing in the one process. Second, **resolution + spawn**: a native-spawning Job stores only a *bare tool name* (e.g. `walrus_driver`); `Job.getFullyQualifiedLocation` resolves it with `shutil.which` over `starfish/bin/private` **then** `starfish/bin` (private searched first), and `Job.shellCommand` runs the resolved path through `subprocess.Popen`.

Of the eleven shipped Job classes, exactly **seven spawn a native tool** (`HLOToTensorizer`, `WalrusDriver`, `BIRVerifier`, `NeffWrapper`, `XLAInferGoldens`, `Backend`, `Watchpoint`) and the rest run **in-process** (`Frontend`, `StaticIOTranspose`, `Kelper`, `SaveTemps`). The `private/` search arm is a **dormant hook**: no shipped Job names a tool that lives only there — in this build it holds a single GoogleTest harness that no Job invokes. That dormancy is a structural finding, tagged below.

| | |
|---|---|
| **Registry class** | `JobRegistry` — `neuronxcc/driver/JobRegistry.cpython-310…so` (BuildID `4949b3ac…` family; not stripped) |
| **Base class** | `Job` / `SingleInputJob` — `neuronxcc/driver/Job.cpython-310…so` (22 methods) |
| **Discovery** | `JobRegistry.loadAllJobs` @ `0x14390` → `pkgutil.iter_modules` over `neuronxcc.driver.jobs` |
| **Per-job dlopen** | `JobRegistry.__getJobFactory` @ `0x10c00` → `getdlopenflags` / `setdlopenflags(│RTLD_DEEPBIND)` / `import_module` |
| **Bin resolver** | `Job.getFullyQualifiedLocation` @ `0x242e0` → `shutil.which(path=pathsep.join([…/private, …/bin]))` |
| **Spawn** | `Job.shellCommand` @ `0x1bed0` → `subprocess.Popen(stdout=PIPE, …, shell=False)` |
| **Native-spawning Jobs** | 7: `HLOToTensorizer`, `WalrusDriver`, `BIRVerifier`, `NeffWrapper`, `XLAInferGoldens`, `Backend`, `Watchpoint` |
| **In-process Jobs** | 4: `Frontend`, `StaticIOTranspose`, `Kelper`, `SaveTemps` |
| **Native tools dir** | `neuronxcc/starfish/bin/` (+ dormant `starfish/bin/private/`) |

---

## 1. The two execution classes

Every `Job` ultimately runs through `Job.run` → `runOnState` → `runSingleInput` (`Job.so`). What differs is what `runSingleInput` *does* per input: build an argv and fork a tool, or call into a Cython/Python routine in this same process. The split is structural and visible in the binary — a native-spawning Job references both `getFullyQualifiedLocation` (to resolve the tool) and a `Popen`/`shellCommand` site; an in-process Job references neither.

The reference count is unambiguous. Grepping each `driver/jobs/*.so` for the resolver and the spawn primitive:

```text
Job                  getFullyQualifiedLocation refs   spawn refs   ⇒ class
HLOToTensorizer      3                                3            native
WalrusDriver         3                                3            native
BIRVerifier          3                                3            native
NeffWrapper          3                                3            native
XLAInferGoldens      3                                3            native
Backend              3                                3            native   (experimental)
Watchpoint           3                                3            native   (debug)
StaticIOTranspose    0                                0            in-process
SaveTemps            0                                0            in-process
Kelper               0                                3 (import)   in-process†
Frontend             0                                3 (import)   in-process†
```

> **GOTCHA — "references `subprocess`" ≠ "spawns a tool".** `Kelper` and `Frontend` (†) each carry a handful of `subprocess` symbol references, but **zero** `getFullyQualifiedLocation` references and **no** `Popen`/`shellCommand` call site of their own — the `subprocess` hits are module-level imports, not native-tool spawns. The discriminating evidence is `getFullyQualifiedLocation`: only the seven native Jobs call it. A reimplementer must classify by the *resolver* call, never by an `import subprocess` line. (Confidence HIGH — ref counts read directly off the `.so` literal pools.)

### In-process Jobs — what they do without forking

| Job | In-process work (verbatim symbols) | Inputs → Outputs |
|---|---|---|
| `Frontend` | runs the penguin / XLA HLO frontend: `runPenguin`, `runXLAFrontend`, `neuronxcc.starfish.penguin.Penguin` — writes HH-IR (`Generated HH IR key`), merged tensor map, static io_transposes. **No `hlo-opt`/`opt-driver`/`Popen` string exists in `Frontend.so`.** | input HLO → HH-IR key, penguin partitions, `*.kelp`, merged tensor map |
| `StaticIOTranspose` | `dumpTransposedFiles` / `simplifyWeightTransform` / `link_root_npys_to_sg` — emits `io_transposes.json` via Python `json` | penguin io_transpose map → per-sg `io_transposes.json` |
| `Kelper` | assembles the NEFF/kelf: `genKelfFiles`, `add_{pwp,dve,one_pwp,ucode_lib}_files`, `addKelpHeader`, `calcNeffVersion`, `computeKelpMetrics` | per-sg `neff.json`, `def.json`, pwp/dve ucode → final NEFF + kelf metrics |
| `SaveTemps` | `run` copies temp files when `isUsingTemp` | work dir → preserved temps |

> **CORRECTION (vs S2-01 §3 call tree) — `Frontend` does not spawn `hlo-opt`.** An earlier call tree claimed `Frontend.so → hlo-opt` and `StaticIOTranspose.so → io_transpose JSON producer` as **native** handoffs. Both are false in this binary: `Frontend.so`'s only frontend symbols are `runPenguin` / `runXLAFrontend` / `Penguin` (verified: 4 / 15 / 5 references; zero `Popen`, zero `getFullyQualifiedLocation`), and `StaticIOTranspose` writes `io_transposes.json` with Python `json`. The `hlo-opt` ELF ships in `starfish/bin/` but **no Job in this driver invokes it**. (Confidence HIGH.) [3.3](compilecommand-pipeline.md) reads the `Frontend` row of the pipeline table as `hlo-opt (--passes=…)`; that describes the *conceptual* HLO-optimization stage, not a fork from `Frontend.so` — the two pages are consistent once the in-process detail is separated from the IR-descent label.

---

## 2. The job table — name → class → tool → resolver

Each native-spawning Job stores its tool basename as a `__pyx_n_s_*` / `__pyx_k_*` interned literal in its own `.so`. The basenames below are read straight from those literal pools.

| Job class | Native tool (basename literal) | argv built by | spawn site | Gate |
|---|---|---|---|---|
| `HLOToTensorizer` | `hlo2penguin` | own `subprocess.Popen` (STDOUT-merged, `universal_newlines`) | proc `h2p_proc` | always |
| `WalrusDriver` | `walrus_driver` (or in-proc `libwalrus.so`) | base `shellCommand` (×3) | `./sg%02d/` per subgraph | always |
| `BIRVerifier` | `walrus_driver` (verify-only) | base `shellCommand` (×3) | — | unless `--internal-disable-birverifier-validation` |
| `XLAInferGoldens` | `xla_infergoldens` | base `shellCommand` (×3) | — | framework-gated |
| `NeffWrapper` | `hlo-neff-wrapper` | own `subprocess.Popen` (STDOUT-merged) | proc `neff_wrapper_proc` | `--enable-internal-neff-wrapper` |
| `Backend` | `nn_executor` (main) + `kgraph2dot` (SVG only) | base `shellCommand` (×3) | `ulimit -t … && nn_executor …` | `--enable-internal-new-backend` / `--enable-experimental-bir-backend` |
| `Watchpoint` | `watchpoint_insert` | base `shellCommand` (×3) | — | `--mode`-gated (debug) |

> **CORRECTION (vs D-A06 §9.3) — `Backend`'s main executable name `nn_executor` IS a literal.** The earlier report read `nn_executor` as a config-driven local var with "no `__pyx_n_u_` string". In `Backend.cpython-310…so` it is present as **both** `__pyx_n_s_nn_executor` and `__pyx_k_nn_executor` — an interned identifier *and* a raw string constant. The same `nn_executor` basename also appears in `WalrusDriver.so`. So `Backend`'s argv (`ulimit -t <sec> && nn_executor --bir backend.bir --tensor_map <…> --sunda --json <…> …`, confirmed by the `backend.bir` / `--sunda` / `ulimit` literals) names a concrete tool — it merely is not *shipped* in this wheel's `starfish/bin/`. (Confidence HIGH — string presence is direct.)

> **NOTE — three of the seven native tools are absent from this wheel.** `starfish/bin[/private]` ships only `walrus_driver`, `hlo2penguin`, `hlo-neff-wrapper`, `xla_infergoldens`, `hlo-opt`, `snapshot-unpack`, `coloring_allocator_with_loop`, `full_unroll`, `walrus_bugpoint_driver` (+ `private/tensor_indirect_test`). `nn_executor`, `kgraph2dot`, and `watchpoint_insert` are **not present**, so `Backend` (new-backend path) and `Watchpoint` (debug) would fail `getFullyQualifiedLocation` ("Could not find …") unless an internal build drops those bins in. They are experimental/debug Jobs, off by default. See [3.6/3.7](../) for the four production tools' internals.

`HLOToTensorizer` and `NeffWrapper` are the two Jobs that do **not** route through the base `shellCommand` — each opens its own `subprocess.Popen` with `stderr=STDOUT` merged and `universal_newlines=True`. The other five native Jobs share the base `Job.shellCommand` path (§4).

---

## 3. The bin resolver — `Job.getFullyQualifiedLocation` (private-first)

This is the single source of native-binary resolution. It is the only routine in the wheel that carries the two path constants `u"starfish/bin"` and `u"starfish/bin/private"` (`Job.so` is the sole binary holding them). The decompiled call sequence at `0x242e0` is, in order: `getPackageDir` → `get_exec_path` → `os.path.join` → `os.pathsep` → `os.path.join` → `shutil.which`.

```python
# Job.getFullyQualifiedLocation  (pyx_pw_…_21getFullyQualifiedLocation @ 0x242e0)
# locals: relPath, packageDirectory, pathDirectories
def getFullyQualifiedLocation(self, executableLocation):
    relPath = executableLocation
    if os.path.isabs(relPath):
        return relPath                                 # absolute → use as-is
    packageDirectory = self.getPackageDir()            # dir of neuronxcc package, via __file__
    pathDirectories = [
        os.path.join(packageDirectory, "starfish/bin/private"),  # SEARCHED FIRST
        os.path.join(packageDirectory, "starfish/bin"),
    ]
    found = shutil.which(relPath, path=os.pathsep.join(pathDirectories))
    if found is None:
        raise CompilerInvalidInputException("Could not find " + relPath)
    return found
```

The two `os.path.join` calls (disasm `0x24700`, `0x248a1`) bracket the single `os.pathsep` load (`0x24860`), and exactly one `shutil.which` follows (`0x24943`) — matching "build two dirs, join with `os.pathsep`, run one `which`." On miss the routine loads `__pyx_kp_u_Could_not_find` (`0x25b38`) and raises.

> **STRONG — `private/` is searched *before* `starfish/bin`.** The ordering is fixed by the Cython string-pool layout. Cython lays out `__pyx_k_*` raw-string constants in **first-textual-use order**, and the data-section symbol addresses settle it: `__pyx_k_starfish_bin_private` resides at **`0x35d90`**, `__pyx_k_starfish_bin` at **`0x363f8`** — `private` is emitted *first*, so it is the first element of `pathDirectories` and the first segment of the `os.pathsep`-joined `which` path. The raw string bytes corroborate (`starfish/bin/private` at file offset `220560` < `starfish/bin` at `222200`). A `shutil.which` scans its `path` left-to-right and returns the first hit, so any tool present in *both* dirs resolves out of `private/` — `private` **overrides** the shipped tool.

> **CORRECTION (reconciles D-A10 §3) — the resolver is `which`-over-a-joined-path, not a manual `os.path.exists` loop.** A sibling inventory pass (D-A10) reconstructed `getAbsPath` as `for d in ["starfish/bin","starfish/bin/private"]: if os.path.exists(...)` — i.e. bin-first, hand-rolled. The actual `getFullyQualifiedLocation` body disproves both halves: it references `shutil` / `which` / `pathsep` / `get_exec_path` (4 / 5 / 4 / 4 times respectively in `Job.so`) — a `shutil.which` over a single `os.pathsep`-joined string — and the constant ordering makes it **private-first**. `getAbsPath` is a *different* method (§4) that normalizes user-supplied command-line paths against the launch directory; it is not the bin resolver. (Confidence HIGH — disproved by symbol presence + the `0x242e0` call sequence.)

> **NOTE — the build-root string is not a runtime path.** `Job.so` also embeds `/opt/workspace/KaenaCompilerNativeBuild-310/build/private/src-3.10.16` — that is the Cython-embedded **build-machine** source root, not a search directory. Do not conflate its `…/private/…` substring with the `starfish/bin/private` runtime arm.

### `getPackageDir` / `getAbsPath` — the two path helpers it leans on

Both are docstring-confirmed in `Job.so`:

- **`getPackageDir`** *("Return full path of neuronxcc python package, e.g. to access data files")* — derives the package root from `__file__` via `dirname`, giving the prefix the two `starfish/bin*` segments hang off.
- **`getAbsPath`** *("Returns an absolute path for paths and filenames specified on the command line … relative to the neuronx-cc launch directory, not the CWD which changes during execution")* — uses `getLaunchDir` / `isabs` / `join`. This is for *user* paths (the CWD changes as the driver `chdir`s into `sg%02d/` dirs), and is unrelated to tool resolution.

---

## 4. The spawn — `Job.shellCommand` → `Popen`

Five of the seven native Jobs (`WalrusDriver`, `BIRVerifier`, `XLAInferGoldens`, `Backend`, `Watchpoint`) reach the child through the shared base `Job.shellCommand` (`0x1bed0`). Its keyword surface and call shape are read from the `expose_stderr` / `propagate_exit` / `useShell` / `close_fds` / `communicate` / `Popen` literals in `Job.so`.

```python
# Job.shellCommand  (pyx_pw_…_25shellCommand @ 0x1bed0; nested log_fn @ 0x16760)
def shellCommand(self, cmd, *, env=None, useShell=False,
                 expose_stderr=False, propagate_exit=True, use_logger=True):
    self.log("Executing " + cmd)                       # also "Working directory is " + cwd
    args = ["/bin/bash", "-c", cmd] if useShell else cmd
    proc = subprocess.Popen(args,
                            stdout=subprocess.PIPE,
                            stderr=(subprocess.STDOUT if expose_stderr else subprocess.PIPE),
                            stdin=…, close_fds=…, env=env, shell=False)
    out, err = proc.communicate()
    if propagate_exit and proc.returncode != 0:
        raise CalledProcessError(...)                  # 'Child process job "%s" exited abnormally!'
    return out, err
```

Anchored literals: `"Executing "`, `"Working directory is "`, `/bin/bash` (the `useShell` wrapper, e.g. for `Backend`'s `ulimit -t … && nn_executor …`), `"Child process job \"%s\" exited abnormally!"`, and four `CalledProcessError` references.

- **No per-Job env vars.** Confirmed negative: no Job sets a bespoke `os.environ[...]=` before `Popen`; children inherit `os.environ`. (The daemon's process-wide `OMP_NUM_THREADS=1` stands, but it is set outside any Job.)
- **`shell=False` always.** Even the `useShell` branch passes an explicit `["/bin/bash","-c",cmd]` argv with `shell=False` — there is no `shell=True` injection surface.
- **Replay.** `Job.getReplayCommandLine` (nested `addquotes`, `0x15c70`) re-quotes the same argv with `shlex.quote` and emits `'Replay this job by calling: %s %s'` — the source of `replay_walrus.sh`.

For the full per-Job argv templates (the ~200-flag `walrus_driver` corpus, the `hlo2penguin` flag set, etc.), see [3.5](subtool-argv.md).

---

## 5. Discovery & per-job dlopen — `JobRegistry`

`JobRegistry.__init__` (`0xf200`) builds a `collections.defaultdict`-with-key map `job_factories` whose `__missing__` (the nested `default_dict_with_key`) raises a keyed error for unknown job names, then runs discovery. Discovery is two methods working together: `loadAllJobs` enumerates module names, and `__getJobFactory` does the actual `RTLD_DEEPBIND`-wrapped import per name.

```python
# JobRegistry.loadAllJobs  (pyx_pw_…_1loadAllJobs @ 0x14390)
def loadAllJobs(self):
    pkg = neuronxcc.driver.jobs                                  # via importlib.util / find_spec
    for _, modname, is_pkg in pkgutil.iter_modules(pkg.__path__):
        if modname in self.rejected_job_names:                  # skip rejects
            continue
        self.__getJobFactory(modname)                            # ← the per-job import

# JobRegistry.__getJobFactory  (pyx_pw_…_3__getJobFactory @ 0x10c00)
def __getJobFactory(self, name):
    old = sys.getdlopenflags()
    sys.setdlopenflags(old | os.RTLD_DEEPBIND)                   # isolate native .so symbols
    try:
        mod = importlib.import_module("neuronxcc.driver.jobs." + name)
        self.log.debug("Found " + name + " at " + inspect.getfile(mod))
    except ImportError as exception:
        sys.setdlopenflags(old)                                 # restore on failure
        warning("Could not import '" + name + "'"); return None
    finally:
        sys.setdlopenflags(old)                                 # restore on success
    for cls in inspect.getmro(...):                             # walk to the Job subclass
        ...; cls.RegisterArgs(...)                              # register --<job> argparse switches
        self.job_factories[name] = cls
    return cls
```

> **CORRECTION (vs D-A06 §8) — the `RTLD_DEEPBIND` dance lives in `__getJobFactory`, not `loadAllJobs`.** D-A06 placed the `setdlopenflags(│RTLD_DEEPBIND)` wrap around the whole `loadAllJobs` enumeration loop. The binary puts it inside **`__getJobFactory`** (`0x10c00`): that method's interned-symbol stream is, in order, `getdlopenflags → setdlopenflags → RTLD_DEEPBIND → "neuronxcc.driver.jobs." → import_module → getfile → "Found"/"at" → "Could not import" → setdlopenflags (×2 restore)`. `loadAllJobs` itself references **no** RTLD/`setdlopenflags` symbol — it only carries `find_spec` / `get_filename` / `iter_modules` and a call to `JobRegistry__getJobFactory`. So the deep-bind flag is toggled **per job import** (set → import → restore), three `setdlopenflags` sites in all (set-deepbind, restore-on-success, restore-on-except). The effect is the same — each job `.so` is dlopened under `RTLD_DEEPBIND` — but the *scope* is per-job, not per-loop. (Confidence HIGH — the symbol stream is read directly from `__getJobFactory`'s disasm.)

`RTLD_DEEPBIND` makes the dynamic linker prefer a loaded object's *own* symbol definitions over already-loaded globals. Each `driver/jobs/*.so` statically links its slice of XLA / penguin / walrus C++ symbols; without deep-bind, the first job loaded would export those globals and later jobs would silently bind to the wrong copy. With it, every job's import is symbol-isolated. This is why the jobs can be Cython `.so`s with overlapping statically-linked C++ surfaces in one Python process.

The rest of the registry is name→instance plumbing:

| Method | Role |
|---|---|
| `__getJobFactory` / `__getJob` | name → factory → instance; raises `CompilerInvalidInputException` / `"Could not import '"` on miss |
| `makePipeline(names)` | `neuronxcc.driver.Pipeline([self.__getJob(n) for n in names])` |
| `RegisterArgs` | argparse `--<job>` switches; `Input files` / `is an input` metavars |

---

## 6. The dormant `private/` branch — structural finding

The `starfish/bin/private` arm of `getFullyQualifiedLocation` (§3) is a **latent extension point that never fires in this build**. The evidence chain:

```text
starfish/bin/          → 6 pipeline ELF + small backend tools + 4 .py helpers
starfish/bin/private/  → EXACTLY ONE file: tensor_indirect_test (8,902,064 B, stripped ELF)
```

`tensor_indirect_test` is a **GoogleTest unit-test harness** for the walrus dynamic-DMA / `TensorIndirect` access-pattern backend (source `neuronxcc/walrus/bir_sim/test/tensor_indirect_test.cpp`; gtest fingerprints `"All tests in the same test suite must use the same test fixture"`, `"--gtest_filter="`; system-under-test symbols `assignIndirectPattern<…INDIRECT12B/16B/20B>`, `LowerDMAImpl::createDescForReadVarAddr`, `generateDynamicDMA`). It links the backend simulator stack (`libBIRSimulator.so`, `libpwp_sim.so`, `libBIR.so`) via two-level RUNPATH `$ORIGIN/../../lib` — consistent with living one directory deeper than `bin/`.

> **STRONG (structural finding) — no shipped Job resolves into `private/`.** All four production native tools (`hlo2penguin`, `walrus_driver`, `hlo-neff-wrapper`, `xla_infergoldens`) live in plain `starfish/bin/` and resolve on the *first existing match* `which` finds — which, even with private searched first, lands in `bin/` because none of them exists in `private/`. None of `{tensor_indirect_test, bir_roundtrip, nki_klr_sim, loop_optimization, sb_size_legalization}` appears as an `executableLocation` literal in **any** `driver/jobs/*.so`. Therefore the `private/` search arm is **dead in this build** — a dormant hook, most plausibly for internal AWS builds that drop additional gated bins there so they override the shipped ones (per the private-first ordering of §3). The single file actually under `private/` is a test binary the compile pipeline never executes. (Confidence: CERTAIN that `private/` holds only `tensor_indirect_test` and that no Job names a private-only tool — both are direct directory + literal-pool reads; INFERRED for the *intent* "extension point for internal builds.")

> **NOTE — the four "phantom" dev tools are not in `private/`.** `bir_roundtrip`, `loop_optimization`, `sb_size_legalization`, `nki_klr_sim` live in the wheel data-payload `neuronx_cc-2.24.5133.0+58f8de22.data/data/bin/` (pip-installed onto `$VENV/bin/`), reachable only via the venv PATH, **never** via the Job machinery. `loop_optimization` and `sb_size_legalization` are also `BackendPass` generators compiled into `libwalrus.so` (`register_generator_loop_optimization`, `register_generator_sb_size_legalization`); the standalone bins are thin CLI front-ends. None is pipeline-invoked.

---

## 7. Reimplementation contract

To rebuild this layer, a reimplementer needs:

1. **A `Job` base** exposing `getPackageDir` (package root from `__file__`), `getFullyQualifiedLocation(name)` (the **private-first** `shutil.which` of §3, raising `"Could not find "` on miss), `shellCommand(cmd, *, env, useShell, expose_stderr, propagate_exit, use_logger)` (the `Popen` of §4, always `shell=False`, inherited env), and `getReplayCommandLine` (`shlex.quote` replay).
2. **Eleven Job subclasses** split into the seven native-spawning (each storing one tool basename and routing through `shellCommand`, except `HLOToTensorizer`/`NeffWrapper` which own their `Popen`) and the four in-process (`Frontend`/`StaticIOTranspose`/`Kelper`/`SaveTemps`, calling Cython/Python directly with no fork).
3. **A `JobRegistry`** whose `loadAllJobs` does `pkgutil.iter_modules` over `neuronxcc.driver.jobs` (honoring `rejected_job_names`) and whose `__getJobFactory` imports each module **wrapped in `setdlopenflags(│RTLD_DEEPBIND)`** (set → import → restore, per job), indexing the `Job` subclass into a key-raising `defaultdict` `job_factories`, plus `makePipeline(names)` building a `Pipeline` of instances.
4. **The dormant private arm** kept as the *first* `pathDirectories` entry, even though no shipped Job uses it — to preserve the override semantics for gated internal bins.

Cross-references: [3.3 — the CompileCommand pipeline](compilecommand-pipeline.md) (who builds the name list); [3.5 — sub-tool argv templates](subtool-argv.md) (the full per-Job flag corpus); [3.6 / 3.7](../) (the four production tool ELFs' internals).
