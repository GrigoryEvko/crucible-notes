# Sub-Tool argv Construction & Replay

> *All symbols, addresses and string literals on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 share the layout). The driver layer ships as Cython `.so` modules — `neuronxcc/driver/Job.cpython-310-…so`, `neuronxcc/driver/commands/CompileCommand.cpython-310-…so`, `neuronxcc/driver/CommandDriver.cpython-310-…so` — so "addresses" here are `.text` offsets into those modules, and "literals" are interned `__pyx_kp_u_*` / `__pyx_n_s_*` constants.*

## Abstract

`neuronx-cc compile` is not one process. It is a `Pipeline` of `Job` objects, several of which are *external executables* (the BIR round-trip tool, the simulator, the Walrus C backend, the tensorizer). Each external `Job` is launched by building an argv list and handing it to `subprocess.Popen`. The interesting structural fact — the one this page exists to nail down — is that **there is no `Options.to_argv()`**. The compiler never serializes a single resolved options object back into a flag list. Instead, argv is assembled *per sub-tool* by three collector methods on `CompileCommand`, each of which reads the argparse `Namespace` (plus the chosen opt level) field-by-field and appends the flags that tool understands. The result flows `collect{Frontend,Backend,Walrus}Pipeline → Job → Job.shellCommand → subprocess.Popen`.

A second fact about the same `Job` is that it can re-emit its own command line as a copy-pasteable string for debugging. `Job.getReplayCommandLine` walks the same assembled argv, shell-quotes each token (the nested `addquotes` helper), and formats `Replay this job by calling: %s %s`. This is the "replay command line" the docstring promises, and it is the only place the driver shell-quotes argv tokens for human consumption. (The top-level `CommandDriver` separately imports `shlex.quote` for its own diagnostic echo, a different lane.)

The reference frame for a reader: think of LLVM's `clang` driver building a `Command` (an argv vector for `cc1`, `as`, `ld`) per `Tool`, then `ExecuteJobs` forking each. neuronx-cc is structurally the same — `Job` ≙ `Command`, `collect*Pipeline` ≙ the `Tool::ConstructJob` overloads, `shellCommand` ≙ `Compilation::ExecuteCommand` — except the flag list is built straight off the argparse `Namespace` rather than off a typed `ArgList`, and the "print the commands" mode (`clang -###`) is `getReplayCommandLine`.

For reimplementation, the contract is:

- **The three collectors** — `collectFrontendPipeline` / `collectBackendPipeline` / `collectWalrusPipeline` on `CompileCommand` — and the rule that each *appends its tool's flags from the Namespace*, with no unified serializer.
- **`Job.shellCommand`** — how `[executableLocation] + flags` becomes a process: the `subprocess.Popen` call site, the `Executing %s` echo, `expose_stderr` / `propagate_exit` semantics, and how the child's return code is parsed.
- **`Job.getReplayCommandLine`** — the `addquotes` shell-quoting helper and the `Replay this job by calling: %s %s` format string.

| | |
|---|---|
| **Collectors** | `CompileCommand.collectFrontendPipeline` / `collectBackendPipeline` / `collectWalrusPipeline` |
| **Launch** | `Job.shellCommand` @ `0x1bed0` (`.so` size `0x5a4c`) → `subprocess.Popen` |
| **Replay** | `Job.getReplayCommandLine` @ `0x25f70` → nested `addquotes` @ `0x135f0` |
| **Replay format** | `__pyx_kp_u_Replay_this_job_by_calling_s_s` — `"Replay this job by calling: %s %s"` |
| **Echo format** | `Executing ` (interned), `Child process job "%s" exited abnormally!` |
| **Arg flow** | argparse `Namespace` → collectors → `Job` → `shellCommand` → `Popen` (no `to_argv`) |
| **No config/response file** | zero `fromfile_prefix_chars`, zero `'@'`, zero config reader (D-AF05) |

---

## The "no unified `to_argv()`" structure

This is the central claim, so it is made structurally, not by assertion.

A `Job` does not receive a serialized options blob. It receives its flags because `CompileCommand` *constructs them in place*. Three pieces of binary evidence pin this:

1. **The flag strings are constants inside `CompileCommand.so`, not data emitted by an `Options` serializer.** The `.rodata` string pool of `CompileCommand.cpython-310-…so` contains the literal flag tokens themselves — `--cc-pipeline-tiling-factor=2`, `--run-simulator-after=BirCodeGenLoop`, `--enable-internal-walrus-replay-script`, `--internal-tensorizer-opt-level`, `--partitioner-opts='--transformer'`, `--optlevel`, `-O`, `-O1`, and the full `--internal-*` / `--enable-internal-*` / `--enable-experimental-*` catalog. A `to_argv()` design would store *field names* and reflect them into `--field` at runtime; here the flag spelling is hard-baked at each emission site, which is only possible if each collector appends its own literals.

2. **The collectors read the `Namespace` field-by-field.** `getInStateFromCmdline` carries the assertion literal `args.arch != "sunda" or args.logical_nc_config == 1` — i.e. it dereferences `args.arch` and `args.logical_nc_config` off the argparse `Namespace` (`args`) directly. The opt level is read as `internal_tensorizer_opt_level` (the `Namespace` attribute) and re-emitted as `--internal-tensorizer-opt-level`. There is no intermediate typed `Options` object between `Namespace` and the flag list.

3. **There is no `Options.to_argv` / `serialize` / `as_args` symbol anywhere in the driver `.so` files.** The Penguin backend's `Options.so` (`CommandLineParser`) is a *parser* of a flat option string, not a serializer; it exposes `parseOptions` / `parseKnownOptions` but no inverse. (See D-AF05 Part A: Penguin's `Options` holds no field→default map and emits no argv.)

> **QUIRK —** the absence of a serializer is *why* the three collectors must each know their tool's flag spelling. A reimplementation that introduces a single `Options.to_argv()` and routes every sub-tool through it will diverge: neuronx-cc deliberately keeps Frontend/Backend/Walrus flag-emission in three separate methods because each sub-tool consumes a *different, overlapping* subset of the Namespace (the tensorizer wants `--internal-tensorizer-opt-level`; Walrus wants `--enable-internal-walrus-replay-script`; the BIR/simulator round-trip wants `--run-simulator-after=…`). The Namespace is the single source of truth; the collectors are three lossy projections of it.

> **NOTE —** "no unified options object" does not mean "no defaults". Defaults live in argparse's per-flag `default=`, mirrored into `Arguments._ArgumentRegistry.arguments_by_context` (tagged `argument,kind,job,default`) at registration time. The *resolved* state is just the `Namespace`. See [CompileCommand Pipeline](compilecommand-pipeline.md) for the registry side.

---

## collect{Frontend,Backend,Walrus}Pipeline — building the flag list

### Purpose

`buildPipeline` is the dispatcher: it decides which sub-pipelines run (frontend always; backend for the BIR/simulator path; Walrus for the C backend) and concatenates their `Job` lists into `compilepipeline` / `expanded_pipeline`. The three `collect*Pipeline` methods are the workers — each returns a list of `Job`s with their argv pre-populated from the `Namespace` and opt level.

### Entry Point

```text
CompileCommand.run
  └─ CompileCommand.buildPipeline                 ── selects sub-pipelines, concatenates
       ├─ collectFrontendPipeline                 ── HLO/tensorizer-facing Jobs
       ├─ collectBackendPipeline                  ── BIR round-trip + BirCodeGenLoop + BIRSim Jobs
       └─ collectWalrusPipeline                   ── WalrusDriver C-backend Jobs
  └─ CompileCommand.runPipeline                   ── drives each Job.run (print_dots progress)
       └─ Job.run → Job.shellCommand → Popen
```

`buildPipeline` carries a `.genexpr` (a generator-expression closure in the Cython qualname pool) used to flatten the per-collector lists; the pipeline-variable names interned in `CompileCommand.so` are `fe_pipeline`, `be_pipeline`, `basepipeline`, `compilepipeline`, `expanded_pipeline`, `walrus_pipeline`, `walrus_passes`.

### Algorithm

The collectors are not individually decompiled here, but their shape is fully determined by (a) the flag literals they own in `.rodata` and (b) the `Namespace` attributes they read. The pseudocode below models that shape; every emitted flag is a verbatim `.rodata` literal, and every `args.X` read is a confirmed interned attribute name.

```c
function buildPipeline(self, args):                 // CompileCommand.buildPipeline
    state = getInStateFromCmdline(args)              // marshals Namespace -> initial State
    fe_pipeline   = self.collectFrontendPipeline(args, state)
    be_pipeline   = self.collectBackendPipeline(args, state)
    walrus_pipeline = self.collectWalrusPipeline(args, state)
    // concatenated via the .genexpr flatten closure
    compilepipeline = basepipeline + fe_pipeline + be_pipeline + walrus_pipeline
    return Pipeline(compilepipeline)

function collectFrontendPipeline(self, args, state):    // emits HLO/tensorizer flags
    flags = []
    // opt level: read off Namespace, re-spelled as the tensorizer flag
    flags += ["--internal-tensorizer-opt-level", str(args.internal_tensorizer_opt_level)]
    if args.internal_hlo2tensorizer_options:            // a passthrough option STRING
        flags += split_passthrough(args.internal_hlo2tensorizer_options)  // -> Penguin Options
    flags += ["--partitioner-opts='--transformer'"]     // when the transformer partitioner is on
    // ... one append per enabled --enable-internal-* / --enable-experimental-* Namespace bool
    return [ Job(exe=tensorizer_exe, flags=flags, ...) ]

function collectBackendPipeline(self, args, state):     // BIR round-trip + simulator
    flags = []
    flags += ["--cc-pipeline-tiling-factor=2"]          // literal default in .rodata
    if args.<run_simulator>:
        flags += ["--run-simulator-after=BirCodeGenLoop"]
    // birverifier / birsim validation toggles read off Namespace:
    //   --internal-disable-birverifier-validation, --internal-disable-birsim-validation
    return [ Job(exe=bir_roundtrip_exe, flags=flags, ...),
             Job(exe=birsim_exe,        flags=..., ...) ]

function collectWalrusPipeline(self, args, state):      // WalrusDriver C backend
    flags = []
    if args.enable_internal_walrus_replay_script:
        flags += ["--enable-internal-walrus-replay-script"]
    walrus_exe = args.<internal_walrus_c_binary>        // help: "Specify internal Walrus C binary ..."
    return [ Job(exe=walrus_exe, flags=flags, ...) ]
```

> **GOTCHA —** the opt level is *not* re-emitted as the user-facing `-O` / `--optlevel` flag the user typed. The driver reads the resolved level off the `Namespace` and re-spells it for each sub-tool — the tensorizer gets `--internal-tensorizer-opt-level`, not `-O2`. The `-O` / `-O1` / `--optlevel` literals in `CompileCommand.so` are the *inbound* parse side; the outbound side uses the per-tool spelling. A reimplementer who forwards the raw `-O` token to the sub-tools will hand them a flag they do not recognize.

### Function Map

| Method (qualname) | Role | Confidence |
|---|---|---|
| `CompileCommand.buildPipeline` (`.genexpr`) | Selects + concatenates the three sub-pipelines | CERTAIN |
| `CompileCommand.collectFrontendPipeline` | Builds the HLO/tensorizer Jobs' argv from Namespace | HIGH |
| `CompileCommand.collectBackendPipeline` | Builds the BIR round-trip / `BirCodeGenLoop` / BIRSim Jobs' argv | HIGH |
| `CompileCommand.collectWalrusPipeline` | Builds the `WalrusDriver` C-backend Job's argv | HIGH |
| `CompileCommand.getInStateFromCmdline` | Marshals `Namespace` → initial pipeline `State` (`args.arch`, `args.logical_nc_config`) | CERTAIN |
| `CompileCommand.processSerialTpbOpts` | Pre-processes serial-TPB options before collection | MEDIUM |
| `CompileCommand.runPipeline` (`.print_dots`, `.print_dot_context`) | Drives `Job.run` for each Job, prints progress | CERTAIN |
| `CompileCommand.str2bool` | Local bool coercion (mirrors `Actions.str2bool`) | HIGH |

---

## Job.shellCommand — argv → subprocess.Popen

### Purpose

`shellCommand` turns one `Job`'s `[executableLocation] + flags` into a running child process and waits on it. It is the single point where the driver shells out; `CommandDriver` itself never calls `Popen` (D-AF05 C.5).

### Entry Point

```text
Job.run                                   ── per-Job driver (also SingleInputJob.run / runSingleInput / runOnState)
  └─ Job.shellCommand                     ── 0x1bed0, size 0x5a4c
       ├─ build argv  = [executableLocation] + getArgs()   ── flags from the collector
       ├─ log "Executing %s"                                ── interned "Executing "
       ├─ subprocess.Popen(argv, ...)                       ── __pyx_n_s_Popen / __pyx_n_s_subprocess
       ├─ proc.communicate()                                ── capture stdout/stderr
       └─ inspect proc.returncode                           ── parseReturnCode (CompileCommand)
  └─ Job.shellCommand.<locals>.log_fn     ── 0x16760, the per-line log callback
```

### Algorithm

```c
function shellCommand(self, ...):                    // Job.shellCommand @ 0x1bed0
    exe   = self.executableLocation                  // resolved sub-tool path
    args  = self.getArgs()                            // the collector-built flag list
    string_cmd = " ".join([exe] + args)              // __pyx_n_s_string_cmd (the echo form)
    log("Executing " + string_cmd)                   // interned "Executing "

    // expose_stderr decides whether the child's stderr is piped+captured
    // or inherited (passed through to the user's terminal):
    stderr_dest = PIPE if not self.expose_stderr else None     // __pyx_k_expose_stderr

    proc = subprocess.Popen([exe] + args,            // __pyx_n_s_Popen on __pyx_n_s_subprocess
                            stdout=PIPE,
                            stderr=stderr_dest,
                            cwd=os.getcwd())          // __pyx_n_s_getcwd — launch dir, not job CWD
    out, err = proc.communicate()                    // __pyx_n_s_communicate
    rc = proc.returncode                             // __pyx_n_s_returncode

    if rc != 0:
        if self.propagate_exit:                      // __pyx_k_propagate_exit
            // surface child's exit code to the parent command
            raise/forward rc to parent_command       // __pyx_n_s_parent_command
        else:
            // "FAILURE in %s IGNORED. Exception %s" path
            log_failure_ignored(self, ...)
    // child-died-abnormally diagnostic:
    //   "Child process job \"%s\" exited abnormally!"
    return rc
```

The argv is exactly `[executableLocation] + getArgs()`. `getArgs` returns the flag list the collector populated; `executableLocation` is the resolved path to the sub-tool binary. There is no shell interpretation — `Popen` is given a list, so the `" ".join` `string_cmd` exists only for the `Executing %s` log line (and, separately, for replay).

### `expose_stderr` and `propagate_exit`

These two `Job` attributes (both interned with `=` forms `expose_stderr=` / `propagate_exit=`, i.e. constructor keywords) govern the child's I/O and failure handling:

- **`expose_stderr`** — when set, the child's stderr is *inherited* (the user sees the sub-tool's errors live); when clear, stderr is piped and captured so the driver can format it. This is the difference between a sub-tool's diagnostics appearing inline vs. being buffered and re-emitted by the driver's error formatter.
- **`propagate_exit`** — when set, a non-zero child return code is propagated up to `parent_command` (the compile fails); when clear, failure is logged as `FAILURE in %s IGNORED. Exception %s` and the pipeline continues. This lets best-effort jobs (e.g. an optional validation or metrics pass) run without aborting the compile.

> **NOTE —** `cwd` is `os.getcwd()` — the neuronx-cc *launch* directory. The `getReplayCommandLine` docstring is explicit about this: "A relative path specified on the command line must be interpreted relative to the neuronx-cc launch directory, not the CWD which changes during execution." The driver may `chdir` into an artifact directory mid-pipeline, but sub-tool argv paths stay anchored to the original launch CWD.

> **QUIRK —** `SingleInputJob` (`run` / `runSingleInput` / `runOnState`, plus a `_requires_fork` interned attribute) is the in-process variant — a `Job` whose work is a Python callable rather than an external exe, so it never reaches `Popen`. The `shellCommand` path is for the *external* sub-tools only. Whether a given `SingleInputJob` runs in a forked child is gated by `--fork-subcommand` at the `CommandDriver` level (D-AF05 C.3), not by `shellCommand`.

---

## Job.getReplayCommandLine — the quoted replay string

### Purpose

`getReplayCommandLine` re-emits a `Job`'s exact argv as a single shell-safe string, so a developer can copy it and re-run that one sub-tool in isolation. The `Job` docstring states the method has two jobs: "to fill in the input state from the command line arguments if the job is the first in the pipeline. Secondly, it is used to produce a replay command line."

### Algorithm

```c
function getReplayCommandLine(self, ...):            // Job.getReplayCommandLine @ 0x25f70
    exe  = self.executableLocation
    args = self.getArgs()                            // same flag list as shellCommand
    quoted = [ addquotes(tok) for tok in args ]      // shell-quote each token
    cmdline = " ".join(quoted)
    return ("Replay this job by calling: %s %s") % (exe, cmdline)
                                                     // __pyx_kp_u_Replay_this_job_by_calling_s_s

function addquotes(s):                               // nested @ 0x135f0 (.constprop.0, size 0x497)
    // scans s for characters that require quoting (whitespace / shell metachars),
    // returns s unchanged if safe, else wraps/escapes it.
    // Implemented as an inlined char-class scan (memcmp / per-char RichCompare),
    // i.e. a shlex.quote-equivalent quoter.
    ...
```

`addquotes` is a *nested local* of `getReplayCommandLine` (`Job.getReplayCommandLine.<locals>.addquotes`), confirmed by the qualname and a dedicated wrapper at `0x15c70`. Its body (the `.constprop.0` at `0x135f0`) is a branch-dense char scan — `memcmp` plus per-character `PyObject_RichCompare` — which is the signature of a manual "does this token contain a space / special char that needs quoting?" routine. `shlex` and `quote` both appear in the module's import/string pool, so `addquotes` is the project's shell-quoting helper around (or in place of) `shlex.quote`.

> **CORRECTION (ARGV-1) —** D-AF05 D.3 described the replay path as "`shlex.quote`-style". That is right in *intent* but precise in mechanism: the per-token quoting is the nested `addquotes` helper inside `getReplayCommandLine` (a char-scan quoter), not a bare call to `shlex.quote`. The `shlex.quote` symbol that D-AF05 C.6 attributes to the driver lives in a *different* module — `CommandDriver.so` imports `shlex`/`quote` for its own top-level diagnostic command echo, separate from `Job.getReplayCommandLine`'s `addquotes`.

> **GOTCHA —** the replay string is derived from the *same* `getArgs()` flag list as `shellCommand`, so it faithfully reproduces what the sub-tool actually received — including the per-tool re-spelled opt level and all the `--internal-*` flags the collectors injected. It is **not** a reconstruction of the user's original `neuronx-cc` command line. A reimplementer must build replay off the assembled Job argv, not off `sys.argv`, or the replay will silently differ from what ran.

### Replay vs. launch — one argv, two consumers

| Consumer | Method | Output | Quoting |
|---|---|---|---|
| Process launch | `Job.shellCommand` | `subprocess.Popen([exe] + args])` (a list) | none (list form) |
| Echo log | `Job.shellCommand` | `Executing <exe> <args joined>` | plain `" ".join` |
| Replay string | `Job.getReplayCommandLine` | `Replay this job by calling: <exe> <quoted args>` | `addquotes` per token |
| Driver diagnostic | `CommandDriver` (separate) | top-level command echo | `shlex.quote` |

The argv tokens (`[executableLocation] + getArgs()`) are computed once per Job; the three consumers differ only in how they render those tokens for their audience (the kernel, the log, the human).

---

## What is NOT in this path

To prevent a reimplementer from inventing machinery that does not exist (all CONFIRMED-absent in D-AF05 Parts B/C/F):

- **No response-file / `@file` mechanism.** No `fromfile_prefix_chars`, no `'@'` handling, in any driver `.so`. Sub-tool argv is built entirely from the `Namespace`.
- **No config-file reader.** No `.cfg` / `.ini` / `.json` / `configparser` path feeds argv.
- **No `getenv` in the argv path.** Environment-supplied flags (`NEURON_CC_FLAGS`) are *prepended to `sys.argv` outside the wheel* by the framework integration, then parsed as ordinary argv — they are not read inside `shellCommand` or the collectors. See [NEURON_CC_FLAGS Prepend](neuron-cc-flags.md).
- **No unified `Options.to_argv()`.** Re-stated for emphasis: argv is built per-Job by the collectors from the `Namespace`, not serialized from one options object.

---

## Related Components

| Component | Relationship |
|---|---|
| `CompileCommand` (commands) | Owns the three `collect*Pipeline` methods that build each Job's argv |
| `Job` / `SingleInputJob` (driver) | Owns `shellCommand` (launch) and `getReplayCommandLine` (replay); the argv carrier |
| `CommandDriver` (driver) | Dispatches to `CompileCommand`; does *not* `Popen`; owns the separate `shlex.quote` diagnostic echo |
| `Arguments` / `_ArgumentRegistry` (driver) | Source of the `Namespace` and the per-flag defaults the collectors read |
| `Pipeline` (driver) | The ordered `Job` list `buildPipeline` produces and `runPipeline` drives |
| Penguin `Options` (`CommandLineParser`) | The *separate* parser for passthrough option strings (`internal_hlo2tensorizer_options`); a consumer of argv, never a producer |

## Cross-References

- [CompileCommand Pipeline](compilecommand-pipeline.md) — 3.3, `buildPipeline` selection logic and the argparse registry that supplies the Namespace + defaults
- [Job Registry](job-registry.md) — 3.4, the `Job` model, `executableLocation` resolution, and `expose_stderr` / `propagate_exit` defaults per registered job
- [NEURON_CC_FLAGS Prepend](neuron-cc-flags.md) — 3.11, how environment flags reach `sys.argv` before any collector runs
