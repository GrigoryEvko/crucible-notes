# Python Logging Façade & Log-Level Mapping

> *All addresses and offsets on this page apply to neuronx_cc `2.24.5133.0+58f8de22` (cp310 wheel; `__buildtime__='Apr 08 2026, 21:07:10 UTC'`). The Python source cited is the readable `.py` shipped in the wheel; native addresses are VAs in `neuronxlogger_bindings.cpython-310-x86_64-linux-gnu.so`. Other versions/ABIs will differ.*

## Abstract

The neuronx-cc compiler carries **two parallel logging stacks** that share only the `LogLevel` *names*, the default logfile name `log-neuron-cc.txt`, the ANSI-red error sequence, and the STDOUT/STDERR split. This page documents the **Python** half: `neuronxcc/neuronxlogger/logging.py`, a self-contained façade built on the stdlib `logging` module, plus the thin native boundary it crosses. The companion C++ logger (`logging::Logger`, Boost.Log `v2s_mt_posix`) is documented in [3.18](neuronlogger.md).

The façade is deliberately decoupled from the native sinks: `logging.py:9` carries the comment `# TODO: migrate Logger to Boost logger`, and the module imports **exactly two** symbols from the pybind11 extension — `LogLevel` (the enum) and `to_log_level` (the int→enum mapper) — at `logging.py:7`. Every record is emitted through Python `logging.Handler`s; the native `logging::Logger::setup_*` machinery, although statically linked into the same `.so`, is never called from this path. The native side is touched only to obtain the canonical 0..70 `LogLevel` scale and the round-up mapping that lives in C++.

The centerpiece is the level mapping. The native enum spans `TRACE=0 … OFF=70` in steps of 10; `to_log_level(n)` rounds a raw integer **up** to the nearest defined level via a `std::map::lower_bound`; and the literal **35** is special-cased *before* that mapping ever runs, because `to_log_level(35)` would round 35 up to `ERROR(40)` instead of the intended user-facing `USER(60)` "progress-dots" mode. This page reconstructs all three pieces and reconciles the Python `logging` integer scale (which diverges from the native scale only at `TRACE`) against [3.18](neuronlogger.md)'s 0..70 scale.

For reimplementation, the contract is:

- The `LogLevel` enum endpoints and values: `TRACE=0, DEBUG=10, INFO=20, WARNING=30, ERROR=40, FATAL=50, USER=60, OFF=70`.
- The `to_log_level(n)` round-**up** semantics (`lower_bound`, not identity) and its out-of-range edge.
- The three-way level encoding: native `LogLevel` (0..70), Python `logging` int (5,10..70 — TRACE remapped to 5), and the compact `NeuronLogger` 0..7 index ([3.18](neuronlogger.md)) — never conflate them.
- The magic-`35` → `USER` + dots convention, produced driver-side and re-detected in `setup_console_logging`.

| | |
|---|---|
| **Façade source** | `neuronxcc/neuronxlogger/logging.py` (246 lines, readable) |
| **Native boundary** | `from neuronxlogger_bindings import LogLevel, to_log_level` (`logging.py:7`) |
| **Binding module** | `neuronxlogger_bindings.cpython-310-…so` — docstring `"neuronxlogger python bindings API."`, module name `"neuronxlogger_bindings"` |
| **Enum source (C++)** | `logging::operator<<(ostream&, LogLevel)` @`0x25ceba` (cmp ladder `{0x46,0x3c,0x32,0x28,0x1e,0x14,0xa}`) |
| **`to_log_level` (C++)** | `logging::Logger::to_log_level(int)` @`0x25d0cc`; free `logging::to_log_level` @`0x25ea7c` |
| **Mapping mechanism** | `std::map<int,LogLevel>` + `lower_bound` (round-UP) @`0x25d24d` |
| **Magic level** | `35` → `LogLevel.USER` (+ `print_dots`), special-cased at `logging.py:107` / `:119` |
| **Emission backend** | stdlib `logging.{FileHandler,StreamHandler}` — **not** the linked native sinks |

---

## The Native Boundary

### Purpose

`logging.py` is a Python logger that happens to live next to a pybind11 extension; it borrows the *level vocabulary* from C++ but does its own record emission. The boundary is one import line.

### What crosses the import

`logging.py:7` imports **only** `LogLevel` and `to_log_level`. The binding registers a far larger surface (`setLogLevel`, `getLogLevel`, `setupLogfile`, `flush_console`, `isEnabledFor`, bound classes `Rep`/`Message`/`CPP_LOGGER`, and the `CPP_LOG_*`/`LOGGER_CPP_LOG_*` free functions — see [3.18](neuronlogger.md) for the native side), but the façade ignores all of it. (CONFIRMED — `logging.py:7` is the complete import list; binding symbols verified by `nm -D`.)

```text
neuronxlogger_bindings.…so  (statically links the full logging::Logger Boost.Log stack)
  ├─ LogLevel        ──→ py::enum_<logging::LogLevel>  TRACE..OFF      [imported]
  ├─ to_log_level    ──→ logging::Logger::to_log_level(int) @0x25d0cc  [imported]
  ├─ setLogLevel / getLogLevel / setConsoleLogLevel / …               [NOT imported]
  ├─ setupLogfile / flushLogfile / setup_console_logging / shutdown   [NOT imported]
  └─ Rep / Message / CPP_LOGGER / CPP_LOG_* / LOGGER_CPP_LOG_*         [NOT imported]
```

> **GOTCHA —** the binding exports a native `setup_console_logging` whose mangled name is `_ZN7logging21setup_console_loggingEiN5boost10shared_ptrINS_10CustomSinkEEE` — arity **2**, taking `(int, boost::shared_ptr<CustomSink>)`. The Python `setup_console_logging(log_level)` (`logging.py:117`) is a **different** function of arity 1 doing pure-stdlib setup. They name-collide but never meet: `logging.py` does not import the native one, so every `neuronxlogger` caller gets the Python shadow. The native `setupLogfile` (camelCase) likewise does not even collide with the Python `setup_logfile_logging`. A reimplementer must keep these two namespaces strictly apart. (CONFIRMED — native symbol present in dynsym; `logging.py:7` omits both natives.)

> **NOTE —** the binding hard-requires CPython 3.10 (`PyInit_neuronxlogger_bindings` strncmp's `Py_GetVersion` against `"3.10"`), tagged `__pybind11_internals_v11_system_libstdcpp_gxx_abi_1xxx_use_cxx11_abi_1__`. Parallel cp311/cp312 wheels ship the matching ABI build; the `.py` façade is identical across them.

---

## The `LogLevel` Enum

### Purpose

`LogLevel` is the single shared level vocabulary. Its eight values define the round-up grid and the Python↔native crosswalk.

### Values

The enum endpoints and steps are recoverable two ways and agree exactly. The native `logging::operator<<(ostream&, LogLevel)` @`0x25ceba` is a chained-compare ladder testing each value; its immediates, read top-down, are `0x46, 0x3c, 0x32, 0x28, 0x1e, 0x14, 0xa` (and the `0` fallthrough):

```text
cmpl $0x46 (70)  → OFF
cmpl $0x3c (60)  → USER
cmpl $0x32 (50)  → FATAL
cmpl $0x28 (40)  → ERROR
cmpl $0x1e (30)  → WARNING
cmpl $0x14 (20)  → INFO
cmpl $0x0a (10)  → DEBUG
(else 0)         → TRACE
```

| name | `LogLevel` value | python `logging.X` int | stdlib derivation | conf |
|---|---:|---:|---|---|
| `TRACE` | 0 | **5** | `logging.NOTSET(0)+5` (`logging.py:18-19`) | CONFIRMED |
| `DEBUG` | 10 | 10 | stdlib `DEBUG` | CONFIRMED |
| `INFO` | 20 | 20 | stdlib `INFO` | CONFIRMED |
| `WARNING` | 30 | 30 | stdlib `WARNING` | CONFIRMED |
| `ERROR` | 40 | 40 | stdlib `ERROR` | CONFIRMED |
| `FATAL` | 50 | 50 | stdlib `FATAL`/`CRITICAL` | CONFIRMED |
| `USER` | 60 | 60 | `logging.CRITICAL(50)+10` (`logging.py:12-13`) | CONFIRMED |
| `OFF` | 70 | 70 | `logging.CRITICAL(50)+20` (`logging.py:15-16`) | CONFIRMED |

`logging.py:12-19` registers three extra names on the stdlib module (`USER=60`, `OFF=70`, `TRACE=5`) so that `logging.getLevelName`/`_nameToLevel` understand them; the stdlib already owns `DEBUG..CRITICAL`. The native↔python crosswalk is then materialized as `LIBRARY_TO_PYTHON_LOG_LEVEL_MAP` (`logging.py:28-37`), a plain dict consumed by `_library_to_python_log_level()` (`logging.py:190-191`) — a bare lookup that raises `KeyError` on an unmapped `LogLevel`, with no default.

> **QUIRK —** the native value and the python int coincide for `DEBUG..OFF` but **diverge at `TRACE`**: native `LogLevel.TRACE==0`, python `logging.TRACE==5`. So `LIBRARY_TO_PYTHON_LOG_LEVEL_MAP[LogLevel.TRACE] == 5` is a non-identity remap. The reason is structural: stdlib `NOTSET==0` is "no level set," so a python handler at level 0 would let everything through indiscriminately; bumping TRACE to `NOTSET+5` keeps it the chattiest *real* threshold while staying below `DEBUG(10)`.

> **CORRECTION (C2) —** `cli/Daemon.py:10-11` redefines `logging.TRACE = logging.NOTSET (=0)` — disagreeing with `logging.py:18-19`'s `NOTSET+5 (=5)`. Two modules set `logging.TRACE` to different values; whichever imports last wins at runtime. Do not assume a single canonical `logging.TRACE`. (CONFIRMED — both source lines read directly.)

> **NOTE —** a *third* encoding exists: the hlo-opt `NeuronLogger` uses a compact 0..7 severity index ([3.18](neuronlogger.md)), distinct from both the 0/10/…/70 `LogLevel` scale and these python ints. Three coexisting encodings; never conflate.

---

## `to_log_level` — The Round-Up Mapping

### Purpose

Turns a raw integer (a `--verbose`/`--log-level` value) into a defined `LogLevel`. `logging.py` calls it in both setup functions (`:110`, `:123`). It is the centerpiece: the round-**up** behavior is *why* the magic-35 special case exists.

### Algorithm

`logging.py`'s `to_log_level` binds to `logging::Logger::to_log_level(int)` @`0x25d0cc`. Disassembly shows a function-local `std::map<int, LogLevel>`, lazily constructed exactly once under a `__cxa_guard_acquire`/`release` pair (@`0x25d0fc`/`0x25d22b`), populated from an `initializer_list` of identity pairs (`std::map<…>::map(initializer_list)` @`0x25d1f9`), then queried with `lower_bound` (@`0x25d24d`) and dereferenced via the iterator's `operator->` (@`0x25d25d`):

```c
function to_log_level(int n):                 // logging::Logger::to_log_level @0x25d0cc
    static map<int, LogLevel> m = {            // one-time, __cxa_guard_acquire @0x25d0fc
        {0,TRACE},{10,DEBUG},{20,INFO},{30,WARNING},
        {40,ERROR},{50,FATAL},{60,USER},{70,OFF} };  // identity keys 0,10..70
    auto it = m.lower_bound(n);                // @0x25d24d — smallest key >= n
    return it->second;                         // @0x25d25d — LogLevel at that key
```

So **`to_log_level(n)` returns the smallest defined `LogLevel` whose value is ≥ n** — round-up-to-nearest-level, not identity:

| input n | `lower_bound` key | returned `LogLevel` | conf |
|---:|---:|---|---|
| `0` | 0 | `TRACE` | CONFIRMED |
| `1..10` | 10 | `DEBUG` | CONFIRMED |
| `11..20` | 20 | `INFO` | CONFIRMED |
| `21..30` | 30 | `WARNING` | CONFIRMED |
| `31..40` | 40 | `ERROR` | CONFIRMED |
| `41..50` | 50 | `FATAL` | CONFIRMED |
| `51..60` | 60 | `USER` | CONFIRMED |
| `61..70` | 70 | `OFF` | CONFIRMED |
| `>70` | `end()` | undefined — deref of past-the-end iterator | MEDIUM |

> **GOTCHA —** `to_log_level(35)` rounds 35 up to **`ERROR(40)`** — the wrong level for the intended "user-facing output + progress dots" mode (which needs `USER(60)`). This is the entire motivation for the magic-35 special case below: the façade must intercept 35 *before* it reaches `lower_bound`. A reimplementation that simply forwards every CLI integer through `to_log_level` will silently route the user/dots mode to ERROR. (CONFIRMED — round-up table + `logging.py:107,119`.)

> **NOTE —** for `n > 70` no key satisfies `key ≥ n`, so `lower_bound` returns `end()` and the `->second` deref is undefined. `OFF=70` is the maximum documented input, so no in-tree caller is known to reach this; tagged MEDIUM because the path is not proven unreachable. (G2.)

---

## Setup Flow & the Magic-35 / Dots Mode

### Purpose

The two `setup_*` functions install stdlib handlers and are where the integer level becomes a `LogLevel`, where the magic `35` is consumed, and where the STDOUT/STDERR split and progress-dots suppression are wired.

### `setup_logfile_logging` (`logging.py:105-114`)

```c
function setup_logfile_logging(log_file, log_level):   // logging.py:105
    GlobalLoggerState._initialized = True
    if log_level == 35:                                // :107 — magic sentinel
        _logfile_log_level = LogLevel.USER
    else:
        _logfile_log_level = to_log_level(log_level)   // :110 — round-up map
    h = logging.FileHandler(log_file, mode='a', encoding='utf-8')  // :111 — APPEND
    h.setFormatter(developer_formatter)                // metadata-bearing dev format
    h.setLevel(_library_to_python_log_level(_logfile_log_level))   // LogLevel→py int
    _logfile_handlers = [h]
```

> **QUIRK —** the logfile is opened in **append** mode (`mode='a'`), whereas the native `NeuronLogger::flush()` **truncates** `log-neuron-cc.txt` on each flush ([3.18](neuronlogger.md)). The two stacks, pointed at the same default filename, have opposite file semantics — a behavioral divergence a reimplementer must preserve per-stack. (CONFIRMED — `logging.py:111`.)

### `setup_console_logging` (`logging.py:117-137`)

```c
function setup_console_logging(log_level):             // logging.py:117
    GlobalLoggerState._initialized = True
    if log_level == 35:                                // :119 — magic sentinel
        _console_log_level = LogLevel.USER
        print_dots = True                              // progress-dots ON
    else:
        _console_log_level = to_log_level(log_level)   // round-up map
        print_dots = False
    is_user = (_console_log_level == LogLevel.USER)
    # stderr handler — red/user format iff USER, else developer
    stderr_h = StreamHandler(sys.stderr)
    stderr_h.setFormatter(user_err_formatter if is_user else developer_formatter)  // :126-128
    stderr_h.setLevel(_library_to_python_log_level(_console_log_level))
    stderr_h.addFilter(λ r: getattr(r,'user_type','stdout') == 'stderr')           // :130
    # stdout handler — user format iff USER, else developer; suppressed when dots ON
    stdout_h = StreamHandler(sys.stdout)
    stdout_h.setFormatter(user_formatter if is_user else developer_formatter)       // :132-134
    stdout_h.setLevel(_library_to_python_log_level(_console_log_level))
    stdout_h.addFilter(λ r: getattr(r,'user_type','stdout')=='stdout' and not print_dots)  // :136
    _console_handlers = [stderr_h, stdout_h]
```

Mechanics:

- **STDOUT/STDERR split** by a custom record attribute `user_type` (default `'stdout'`): the stderr handler accepts only `user_type=='stderr'`, the stdout handler only `user_type=='stdout'`. This mirrors the native `MergedSink` console router ([3.18](neuronlogger.md)). (CONFIRMED — `logging.py:130,136`.)
- **Dots mode** (`log_level==35` only): the stdout filter's `… and not print_dots` evaluates False for *every* record, so **all stdout log records are suppressed** — the compiler's own progress-dot writer (`CompileCommand.runPipeline.<locals>.print_dots` + the `print_dot_context` context manager, both present in `CompileCommand.cpython-310-…so`) then owns stdout exclusively. (CONFIRMED — `logging.py:136`; `print_dots`/`print_dot_context` strings in CompileCommand.)
- **USER formatting**: in USER mode the stderr handler uses the ANSI-red `\x1b[31;20m%(message)s\x1b[0m` format *iff `sys.stderr.isatty()`* (else plain `user_logging_format`), and stdout uses the plain `user_logging_format`. The same `\x1b[31;20m` red sequence is present in the binding's rodata. (CONFIRMED — `logging.py:21,26`; `[31;20m` string in `.so`.)

### Where the 35 comes from

`to_log_level` never *produces* 35 (its outputs are `{0,10,20,30,40,50,60,70}`). The sentinel is produced **driver-side** and consumed on both ends:

```text
CommandDriver.run (Cython)                       CompileCommand.runPipeline
  if self.verbose == 35:        ──┐               print_dots()  / print_dot_context()
      self.print_dots = True       │ producer       (owns stdout while dots mode active)
      self.verbose    = USER(60)   │
  setup_logfile_logging(...)       │ consumer 1 → logging.py:105 (35→USER)
  setup_console_logging(verbose)   ┘ consumer 2 → logging.py:117 (re-detects 35 → dots ON)
```

The driver flips an internal `print_dots` flag and rewrites its in-memory `verbose` attribute to `logging.USER`. Independently, `setup_console_logging` **re-detects** 35 to set its own `print_dots` and suppress stdout — a defensive duplicate of the same convention on the consumer side. (CONFIRMED — `setup_console_logging`/`setup_logfile_logging`/`print_dots`/`to_numeric_level` strings in `CommandDriver.…so`; the exact `--verbose`/internal-flag path that emits the literal 35 is MEDIUM — G1.)

> **NOTE —** the CLI's `to_numeric_level` (in `Daemon.py` and the Cython `CommandDriver.__init__.to_numeric_level`) maps `0→WARNING(30)`, `1→INFO(20)`, `2→DEBUG(10)`, a bare digit passes through, and a name resolves via `getattr(logging, NAME.upper())` so `--verbose user → USER(60)`. The bare 35 sentinel is *not* one of these outputs; it enters from an internal/interactive path before `CommandDriver.run`'s compare. See [3.20](diagnostic-error-catalog.md) for the full CLI verbosity surface.

---

## Logger Instances & Global State

### Purpose

A process-global singleton holds the installed handlers and levels; `Logger` instances are thin named wrappers that inherit that state.

### `GlobalLoggerState` (`logging.py:40-48`)

A class with mutable class-level attributes used as process globals: `_initialized`, `_logfile_log_level`, `_console_log_level`, `_logfile_handlers`, `_console_handlers`, `_default_logger`, `_loggers`. `clear_global_logging_state()` (`:199-206`) resets them for tests ("this should not be called by the compiler", `:198`).

### `Logger` (`logging.py:51-101`)

`Logger(channel="global")` wraps `logging.getLogger(channel)`, seeds `metadata_opt={"metadata_opt":""}`, and appends itself to `_loggers`. If setup already ran (`_initialized`), the constructor strips the named logger's existing handlers and re-adds the global console+logfile handlers, then sets its level to `_min_log_level()` — that is how late-constructed loggers inherit config; pre-init construction (unit tests) keeps stdlib defaults. `add_scope(metadata)` (`:74-75`) sets `metadata_opt={"metadata_opt": f"({metadata}) "}`, the parenthesized scope that fills `%(metadata_opt)s` in the developer format.

Instance methods `logDebug/logInfo/logWarning/logError` (`:78-88`) join varargs via `_concat_msgs` (`"".join(str(m) …)`, `:194-195`) and emit with `extra=self.metadata_opt`; the `logf*` variants (`:91-101`) pass `*msgs` straight to stdlib for `%`-style formatting. There is **no** `logFatal`/`logUser`/`logTrace` instance method. Module-level wrappers `logDebug…logfError` (`:216-245`) delegate to a lazily-built `_default_logger()` (channel `"global"`). (CONFIRMED — full source.)

> **GOTCHA —** `Logger.root_logger()` (`logging.py:67-71`) references `GlobalLoggerState._root_logger`, a field that is **never defined** on the class (the real field is `_default_logger`, `:47`). Calling `root_logger()` raises `AttributeError`. It is dead/buggy API with no in-tree caller in the readable sources. Do not reproduce it; use `_default_logger()`. (HIGH — G3, source bug.)

### Format constants & runtime mutation

Three formats (`logging.py:21-23`): `user_logging_format='%(asctime)s %(message)s'`; the metadata-bearing `developer_logging_format='%(asctime)s %(levelname)s %(process)d %(metadata_opt)s[%(name)s]: %(message)s'`; and the tty-only red `tty_err_user_logging_format`. `set_logfile_log_level`/`set_console_log_level` (`:140-159`) take a `LogLevel` **directly** (no int→enum mapping), re-`setLevel` every handler, then recompute `_min_log_level()` (`:181-187`, the chattier of console/logfile, in python ints) and re-`setLevel` all registered loggers. `flush_logfile`/`flush_console` flush each handler; `shutdown` (`:176-177`) calls `logging.shutdown()`.

> **NOTE —** the driver's own `developer_logging_format` (in `CommandDriver.…so`) has **no** `%(metadata_opt)s` field — there are two developer formats. The metadata-BEARING one (`logging.py:23`) raises `KeyError` on a record that omits the `metadata_opt` extra, but `Logger` always passes `extra=self.metadata_opt`, so it is safe. A reimplementer mixing record sources across the two formats must supply the extra. (HIGH.)

---

## Public Surface (`__init__.py`)

`_add_lib_path()` (`__init__.py:35-41`) appends the package dir to `sys.path` so the bare `from neuronxlogger_bindings import …` in `logging.py:7` resolves. The package re-exports from `.logging`: `Logger, LogLevel, setup_logfile_logging, setup_console_logging, shutdown, set/get_logfile_log_level, set/get_console_log_level, flush_console, flush_logfile, logDebug, logInfo, logWarning, logfDebug, logfInfo, logfWarning, clear_global_logging_state`; from `.error`: `NeuronAssertion, ErrorCode, register_namespace, neuron_external_assert, neuron_internal_assert, neuron_internal_assert_msg`; from `.error_validation`: `VerifyNeuronAssertError, verify_neuron_assert_namespace`.

> **CORRECTION (C4) —** the `__init__.py:10-15` docstring advertises a `LogType` enum ("Enumeration of output destinations (STDOUT, STDERR)"), but the binding registers **no** `py::enum_<LogType>` — `STDOUT`/`STDERR` exist only inside the native `logging::operator<<(ostream&, LogType)` console router ([3.18](neuronlogger.md)), never as a Python registration. The docstring's `LogType`/`ErrorCode` bullets are stale. Also note `logError`/`logfError` are **not** re-exported — only debug/info/warning module wrappers are public. (CONFIRMED — `__init__.py:44-63`; binding has no LogType enum.)

---

## Related Components

| Name | Relationship |
|---|---|
| `neuronxlogger_bindings.…so` | Source of `LogLevel` + `to_log_level`; statically links the full native `logging::Logger` stack the façade ignores |
| `neuronxcc/driver/CommandDriver.…so` | Produces the magic-`35`/dots convention; rewrites `verbose→USER`; defines `to_numeric_level` |
| `neuronxcc/driver/commands/CompileCommand.…so` | Hosts `print_dots`/`print_dot_context` — owns stdout while dots mode is active |
| `neuronxcc/cli/Daemon.py` | CLI verbosity surface; redefines `logging.TRACE` to `NOTSET(0)`, diverging from the façade's `5` |
| `neuronxcc/neuronxlogger/error.py` | `NeuronAssertion`/`ErrorCode` error model that emits via this façade's `Logger.logfError` |

## Cross-References

- [NeuronLogger (C++ logger)](neuronlogger.md) — 3.18, the native Boost.Log stack; owns the 0..70 scale, the `MergedSink` STDOUT/STDERR router, the truncating logfile flush, and the `NeuronLogger` 0..7 compact index
- [Diagnostic & Error-Code Catalog](diagnostic-error-catalog.md) — 3.20, the CLI `--verbose`/`--log-level` argparse surfaces and `to_numeric_level` that feed integers into this façade
