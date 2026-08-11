# The neuronx-cc Command Dispatcher & Subcommand Model

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython module `neuronxcc/driver/CommandDriver.cpython-310-x86_64-linux-gnu.so`, 1,204,064 bytes). cp311/cp312 share the layout; addresses differ. Treat every offset as version-pinned. Evidence is the IDA full decompile (`__pyx_pf`/`__pyx_pw` bodies, `__pyx_n_s_*` attribute interns, `_Pyx_AddTraceback(...)` source-line markers) cross-checked against the binary's `*_strings.json` / `*_functions.json` sidecars.*

## Abstract

`neuronx-cc` is a **multiplexing front door**, not a monolithic compiler binary. A single ELF entry point (`CommandDriver`) parses a small set of *driver-level* arguments, peels off the first positional token as a **subcommand name**, looks that name up in a `known_commands` dictionary, and hands the remaining argv to the selected `CommandInterface` subclass. The public banner advertises two subcommands — `compile` and `list-operators` — but a third command class, `neff-info`, ships in `driver/commands/` and is dispatchable even though it is omitted from the help text.

The other half of this page is **process isolation**. Under `--fork-subcommand`, the dispatcher does not run the subcommand inline; it spawns a `multiprocessing.Process`, joins it, reads its `exitcode`, and maps abnormal exits onto three operator-facing diagnostic codes — `[F134]`, `[F137]`, `[F139]` — so that a segfault or an OOM-kill inside the compile child surfaces as a clean error banner instead of a Python traceback on a dead interpreter. This is the boundary between "the driver crashed" and "the compile crashed," and the wiki treats it as the contract every subcommand runs behind.

| | |
|---|---|
| **Module entry** | `CommandDriver.main` @ `0x17a90` (`neuronxcc.driver.CommandDriver.main`) |
| **Dispatcher** | `CommandDriver.run` @ `0x25a70` (pw) / `0x1c510` (pf body, py-line 323) |
| **Subcommand registry** | `self.known_commands` (interned `known_commands` @ str `0x38970`) |
| **In-process runner** | `CommandDriver.run_subcommand` @ `0x1a7c0` (py ~?) |
| **Forked-child runner** | `CommandDriver.run_subcommand_in_process` @ `0x19110` (py 366–367) |
| **Error/exit formatter** | `CommandDriver.handleError` @ `0x30470` (py-line 113) |
| **Version printer** | `VersionAction.__call__` @ `0x2c830`; `getVersionString` @ `0x2d690` |
| **Usage string** | `%(prog)s [args] <subcommand> [<subcommand args>]` @ `0x37ac0` |

The subcommand → `Pipeline` → `Job` descent that `compile` triggers is [3.3 The CompileCommand Pipeline](compilecommand-pipeline.md); the *two* argparse systems behind it ([3.2 Two-Parser Architecture](two-parser-architecture.md)); the whole-program orientation is [0.2 The Compile Pipeline at a Glance](../front/pipeline.md).

## 1. The CLI shape — one usage string, two-and-a-half subcommands

The top-level usage is a single interned literal at `0x37ac0`, the complete text of which is (verbatim from the string pool):

```text
%(prog)s [args] <subcommand> [<subcommand args>]
Run the AWS Neuron compiler.
The following subcommands are supported:
   compile         Compile a neural network model
   list-operators  List supported operators
See '%(prog)s <subcommand> --help' for more information about a
specific subcommand.
```

This is the **`[args] <subcommand> [<subcommand args>]`** model: a left band of driver-global options (`--version`/`-V`, `--verbose`, `--logfile`, `--fork-subcommand`, help flags), then one positional subcommand token, then everything to its right belongs to the subcommand. The split point is `parse_known_args` (below), not a position count.

Three command classes are present in `driver/commands/`, each a Cython `.so`:

| Subcommand name | `CommandInterface` subclass | Binary | In public banner? |
|---|---|---|---|
| `compile` | `CompileCommand` | `commands__CompileCommand.cpython-310-…so` | yes |
| `list-operators` | `ListOperatorsCommand` | `commands__ListOperatorsCommand.cpython-310-…so` | yes |
| `neff-info` | `NeffInfoCommand` | `commands__NeffInfoCommand.cpython-310-…so` | **no** |

> **QUIRK — `neff-info` is a banner-omitted command class.** The two-line help block at `0x37ac0` lists only `compile` and `list-operators`, yet `NeffInfoCommand.cpython-310-…so` ships in the same `driver/commands/` directory as the other two and exports the same `CommandInterface` surface. It is reachable by name (the `known_commands` dict is built from the available command classes, not from the help banner), but it is intentionally absent from the public usage text — treat it as a semi-private inspection command (NEFF-container dump) rather than a compile entry point. The banner is documentation, not the dispatch table. All three command binaries ship; the omission from the banner is structural rather than a build artifact.

`--version` / `-V` short-circuits dispatch entirely: it is wired to `VersionAction` (`0x2c830`), a custom `argparse.Action` whose `__call__` prints `getVersionString()` and exits before any subcommand is selected. `getVersionString` (`0x2d690`) assembles the three-line block `"NeuronX Compiler version " … "Python version " … "NumPy version "` from `KccVersion`. *Anchors: `--version` @ str `0x38d80`, `VersionAction` @ str `0x38af8`, and the version literals in `getVersionString`'s string refs.*

## 2. Entry: `main` → logging → `run`

`CommandDriver.main` (`0x17a90`) is the wheel's console-script entry point. Its body (from interned attribute names) does three things before dispatch:

```python
# neuronxcc.driver.CommandDriver.main  @ 0x17a90  (reconstructed from __pyx_n_s_* interns)
def main():
    # 1. Reset SIGINT to the default disposition so Ctrl-C kills the
    #    process group cleanly (interns: signal, SIGINT, SIG_DFL).
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # 2. Wire stdlib + absl logging: UTC timestamps, a single root handler.
    logging.Formatter.converter = time.gmtime            # interns: Formatter, converter, gmtime
    logging.captureWarnings(True)                        # intern: captureWarnings
    logging.root.removeHandler(absl_handler)             # interns: root, removeHandler, absl_handler

    # 3. Build the driver and dispatch on the real argv.
    driver = CommandDriver()                             # __init__ @ 0x28160
    sys.exit(driver.run(sys.argv))                       # interns: run, sys, argv, exit
```

*Interns in `main`: `signal`/`SIGINT`/`SIG_DFL`, `Formatter`/`converter`/`gmtime`, `captureWarnings`, `root`/`removeHandler`/`absl_handler`, `CommandDriver`, `run`, `sys`/`argv`/`exit`.* The `gmtime` converter forces UTC log timestamps; the `absl_handler` removal de-duplicates absl's auto-installed root handler.

`CommandDriver.__init__` (`0x28160`, 18,113 bytes, 271 callees) constructs the top-level `InterceptingArgumentParser` (the recording argparse subclass detailed in [3.2](two-parser-architecture.md)), registers the global `[args]` flags and the subcommand positional, builds the `known_commands` name→class dict, and seeds the per-level logging helpers (`to_numeric_level` @ `0x26c40` maps a log-level *name* — `DEBUG`/`INFO`/`WARNING` — to a numeric level). It is the largest non-dispatch function in the module.

## 3. Dispatch: `parse_known_args → known_commands[name] → CommandInterface`

The dispatcher is `CommandDriver.run`. Its public-wrapper is `0x25a70`; the real body is the `pf` function at `0x1c510` (6,716 decompiled lines, py-line 323). The dispatch is a four-step pipeline, each step anchored to a concrete call site in the `0x1c510` decompile:

```python
# CommandDriver.run  (pf body @ 0x1c510)  — annotated with decompile line numbers
def run(self, argv):
    # ── STEP 1 · split driver args from subcommand args ───────────────────
    #   argparser.parse_known_args(argv)  →  (known_args, delegated_args)
    known_args, delegated_args = self.argparser.parse_known_args(argv)   # decomp L631
    #   `known_args`     = the driver-level Namespace (verbose, logfile, fork, …)
    #   `delegated_args` = leftover tokens = the subcommand's own argv
    #   (interns: parse_known_args @0x387d0, known_args @0x38d20, delegated_args @0x389d0)

    # ── STEP 2 · configure logging from the parsed Namespace ──────────────
    #   verbose → numeric level; console vs logfile handler selection.
    #   (interns: setup_console_logging, setup_logfile_logging, getLogger,
    #    StreamHandler, FileHandler, Formatter, setLevel, isatty)

    # ── STEP 3 · select the subcommand class by name ──────────────────────
    subcommand = self.known_commands[command_name]                       # decomp L1075
    #   command_name = known_args's subcommand positional;
    #   known_commands maps 'compile'→CompileCommand, 'list-operators'→…,
    #   'neff-info'→NeffInfoCommand.  Each subclass returns its own name via
    #   CommandInterface.getCommandName() (intern getCommandName @0x389a0).

    # ── STEP 4 · run it, in-process or forked (see §4) ────────────────────
    if known_args.fork_subcommand:                                       # decomp L5150
        exitcode = <run in a child Process>                              # §4
    else:
        exitcode = self.run_subcommand(subcommand, delegated_args, logfile)
    return exitcode
```

The key structural fact: **`parse_known_args` (not `parse_args`)** is what makes the `[args] <subcommand> [<subcommand args>]` grammar work. The top parser is *deliberately permissive* — it knows only the driver-global flags, so any token it does not recognise (every subcommand flag) falls into `delegated_args` instead of triggering an "unrecognized arguments" error. That leftover list becomes the subcommand's argv unchanged. The two-parser design ([3.2](two-parser-architecture.md)) is precisely so the driver parser and the subcommand parser can each `parse_known_args` their own slice of the same flat argv without fighting over flags they do not own.

> **NOTE — `getCommandName` is the registry's source of truth.** `CommandInterface` (base class @ `0x8830` in `commands__CommandInterface.cpython-310-…so`) declares `getCommandName` @ `0x9150`; each subclass overrides it to return its subcommand string (`'compile'`, `'list-operators'`, `'neff-info'`). The `known_commands` dict in `CommandDriver.__init__` is keyed by exactly these return values, so adding a command is a matter of registering one more class whose `getCommandName` yields a fresh name — there is no separate name table to keep in sync. *Anchors: `getCommandName` in both `CommandDriver` (str `0x389a0`) and `CommandInterface` (`0x9150`); the `known_commands` intern at `0x38970`.*

`run_subcommand` (`0x1a7c0`) is the in-process path: it calls `subcommand.run(delegated_args, …)` (intern `run`, `self`, `subcommand`, `delegated_args`, `logfile`), wrapping failures through `handleError` and falling back to `os.EX_SOFTWARE` on error (interns `os`, `EX_SOFTWARE`, `handleError`). For `compile`, `subcommand.run` is `CompileCommand.run` → `buildPipeline` → `runPipeline` — see [3.3](compilecommand-pipeline.md).

## 4. Process isolation: `--fork-subcommand`

The flag `--fork-subcommand` (interned literal `0x38890`; Namespace attr `fork_subcommand` @ str `0x386a0`) switches dispatch from inline to a forked child. The decompile in `run` (`0x1c510`) shows the exact sequence:

```python
# CommandDriver.run, fork path (pf body @ 0x1c510)  — decompile line anchors
if known_args.fork_subcommand:                                        # L5150
    multiprocessing.set_start_method(...)          # interns: multiprocessing, set_start_method
    child = multiprocessing.Process(                                  # L5505  (intern Process)
        target = self.run_subcommand_in_process,                     # L5538
        args   = (subcommand, delegated_args, logfile),
    )
    child.start()                                  # intern: start
    child.join()                                   # intern: join
    code = child.exitcode                                            # L5708  (intern exitcode @0x37493)
    # log "Subcommand returned with exitcode=" + code               (str @0x37800)
    if <code indicates failure>:
        handleError(...)                                            # L6174 / L6181
        sys.exit(os.EX_SOFTWARE)                                    # L6367 (intern EX_SOFTWARE @0x38c80)
    return code
```

The child target, `run_subcommand_in_process` (`0x19110`, py-lines **366–367**), is a thin shim around the same in-process runner, terminated by an explicit interpreter teardown so the child exits cleanly with the subcommand's status:

```python
# CommandDriver.run_subcommand_in_process(self, subcommand, delegated_args, logfile)
#   @ 0x19110   (py 366–367; interns: run_subcommand, logging, shutdown, sys, exit)
def run_subcommand_in_process(self, subcommand, delegated_args, logfile):
    rc = self.run_subcommand(subcommand, delegated_args, logfile)   # py 366
    logging.shutdown()          # flush+close every handler before the child dies
    sys.exit(rc)                # py 367 — child's exitcode == subcommand status
```

*Anchors: the arg names `subcommand`/`delegated_args`/`logfile` come from `__pyx_pyargnames`; the body's calls from the `run_subcommand` / `logging` / `shutdown` / `sys` / `exit` interns; the py-lines from the `_Pyx_AddTraceback(..., 366, 366, ...)` and `..., 367, 367, ...` markers.*

The rationale is the ICE/OOM banner. `run_subcommand_in_process` and `handleError` carry the operator text (interned in this module):

```text
 An Internal Compiler Error has occurred
 Processes may be terminated if they encountered an internal error or if there
 was insufficient memory. Please review the log file for any error conditions.
 Please check the available memory on the compile instance. Current memory
 usage for neuronxcc is …
```

`handleError` (`0x30470`, py-line 113) is where a child failure is *classified*: it inspects the broken-process condition (interns `BrokenProcessPool`, `RemoteTraceback`, `TracebackException`, `from_exception`, `origin_job`), reads resident-set sizes for the memory report (interns `resource`, `getrusage`, `ru_maxrss`, `RUSAGE_CHILDREN`, `RUSAGE_SELF`), tears down global compiler state (intern `FinalizeGlobalState`), and emits one of the `[F134]/[F137]/[F139]` banners. *Anchors: `BrokenProcessPool` @ str `0x38770`, `RUSAGE_CHILDREN` @ `0x388e0`, `RUSAGE_SELF` @ `0x38c50`.*

> **GOTCHA — `--fork-subcommand` is the only thing that makes `exitcode` meaningful.** Without the flag, `run_subcommand` runs the subcommand in the *driver's own* interpreter; a hard crash (SIGSEGV in a native pass) or an OOM-kill takes the whole `neuronx-cc` process down and there is no surviving `exitcode` to inspect. With the flag, the crash is confined to the `multiprocessing.Process` child; the parent reads `child.exitcode`, recognises the negative (signal) or non-zero status, and converts it into a support-ticket banner. Framework integrations that compile many graphs in one host process rely on this isolation so one bad graph does not abort the batch.

## 5. The crash-exit codes — `[F134]`, `[F137]`, `[F139]`

Three diagnostic banners are interned in this module (symbol names from `*_names.json`, message text from `*_strings.json`):

| Code | Symbol | Addr | Message (verbatim) |
|---|---|---|---|
| `[F134]` | `__pyx_k_F134_neuronx_cc_terminated_abno` | `0x37fa0` | `[F134] neuronx-cc terminated abnormally - Please open a support ticket at https://github.com/aws-neuron/aws-neuron-sdk/issues/new` |
| `[F137]` | `__pyx_k_F137_neuronx_cc_was_forcibly_ki` | `0x37ee0` | `[F137] neuronx-cc was forcibly killed - This most commonly occurs due to insufficient system memory. Using a smaller data type, dimensions, batch size, or a larger instance type may help.` |
| `[F139]` | `__pyx_k_F139_neuronx_cc_terminated_abno` | `0x37e40` | `[F139] neuronx-cc terminated abnormally - Please open a support ticket at https://github.com/aws-neuron/aws-neuron-sdk/issues/new` |

The codes are not arbitrary — they decode the child's exit status under the POSIX `128 + signal` convention, which is exactly what a `multiprocessing.Process.exitcode` reports as a *negative* number for a signal-killed child:

- **`F134` = 128 + 6 = SIGABRT** — the child aborted (a C++ `assert`/`abort()`, an uncaught `std::terminate`, or a Python-level fatal in a native pass). Generic "terminated abnormally."
- **`F137` = 128 + 9 = SIGKILL** — the child was *forcibly* killed, almost always the OOM-killer reclaiming memory. The message is the only one of the three that suggests a remedy (smaller dtype / batch / bigger instance), because OOM is the dominant cause.
- **`F139` = 128 + 11 = SIGSEGV** — the child segfaulted. Second "terminated abnormally" variant, distinguished from `F134` only by the originating signal.

> **GOTCHA — don't pair `F137` with SIGSEGV.** The two "terminated abnormally" banners (`F134`, `F139`) look interchangeable, which makes it easy to slot `F137` into the segfault role. The suffix arithmetic settles it: 134→SIGABRT(6), 137→SIGKILL(9), 139→SIGSEGV(11), and `F137`'s own text ("was forcibly killed … insufficient system memory") is the OOM-kill banner.

> **NOTE — the exact `exitcode → [F13x]` branch is derived, not a clean switch.** The three banner constants are interned by `__Pyx_CreateStringTabAndInitStrings` (`0x4bbf`) and selected on the child's `exitcode` (read at `run` decompile L5708) inside the failure path that calls `handleError` (L6174). The decompiler renders the per-code selection as opaque interned-const loads from `_pyx_mstate_global_static` rather than a readable `switch`, so the precise comparison thresholds are **[INFERRED]** from the `128 + signo` arithmetic and the message semantics. The ownership — `CommandDriver`'s `run` / `run_subcommand_in_process` / `handleError` triad — and the banner identities are read directly. The final driver-level exit on a failed fork is `os.EX_SOFTWARE` (str `0x38c80`, decompile L6367).*

## 6. End-to-end flow

```text
 sys.argv
   │
   ▼
 CommandDriver.main  (0x17a90)
   │  SIGINT→SIG_DFL; UTC logging; build CommandDriver
   ▼
 CommandDriver.run  (pf 0x1c510)
   │  ① argparser.parse_known_args(argv) → (known_args, delegated_args)     L631
   │  ② configure logging from known_args
   │  ③ subcommand = known_commands[command_name]                           L1075
   │        compile → CompileCommand   list-operators → ListOperatorsCommand
   │        neff-info → NeffInfoCommand  (banner-omitted)
   │  ④ dispatch ↓
   ├───────────────── known_args.fork_subcommand?  (L5150) ─────────────────┐
   │  NO (inline)                                  │  YES (isolated)         │
   ▼                                               ▼                          │
 run_subcommand (0x1a7c0)               multiprocessing.Process(             │
   subcommand.run(delegated_args)         target=run_subcommand_in_process,  │
   handleError on failure;                args=(subcommand, delegated_args,  │
   os.EX_SOFTWARE                              logfile))    (L5505/L5538)     │
                                          child.start(); child.join()        │
                                          code = child.exitcode  (L5708)      │
                                          ┌── code abnormal? ───────────────┐ │
                                          │  handleError (L6174) →          │ │
                                          │  [F134]/[F137]/[F139] banner;   │ │
                                          │  sys.exit(os.EX_SOFTWARE) L6367 │ │
                                          └─────────────────────────────────┘ │
   │                                                                          │
   ▼ (compile only)                                                          │
 CompileCommand.run → buildPipeline → runPipeline  ── see 3.3 ───────────────┘
```

## 7. Evidence summary

- **Two public subcommands plus `neff-info`.** The usage banner at `0x37ac0` lists only `compile` / `list-operators`, verbatim; `NeffInfoCommand.cpython-310-…so` ships alongside the other two command binaries.
- **`parse_known_args` is the argv split.** The `parse_known_args` intern (`0x387d0`) is referenced at `run` decompile **L631**, producing `known_args` (`0x38d20`) and `delegated_args` (`0x389d0`).
- **`known_commands[name]` selects the class.** The `known_commands` intern (`0x38970`) is indexed at `run` decompile **L1075**, keyed on `getCommandName` (`0x389a0`).
- **The fork path** — `fork_subcommand` checked at **L5150**, `Process` at **L5505**, `run_subcommand_in_process` as target at **L5538**, `exitcode` read at **L5708**, `handleError` at **L6174**, `EX_SOFTWARE` at **L6367** — is a chain of call-site anchors in the `0x1c510` decompile.
- **The three banners** are interned at `0x37fa0` / `0x37ee0` / `0x37e40` with the message text quoted above.

## Limits of this reading

- That `neff-info` is *dispatchable* but un-bannered is **[INFERRED]**: the registry is built from command classes via `getCommandName` rather than from the help text, but the precise registry-population call was not isolated at the byte level.
- The `[F134]` / `[F137]` / `[F139]` → signal mapping is **[INFERRED]** from the `128 + signo` suffix arithmetic and the `F137` "forcibly killed / insufficient memory" text. The `exitcode → banner` comparison renders as interned-const loads rather than a readable switch, so the threshold logic is not directly read.
- Every symbol, offset, string, and py-line here comes from the shipped Cython `.so` and its IDA sidecars.
