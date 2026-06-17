# NeuronLogger — the C++ Logging Singleton & Boost.Log Sinks

> *All addresses on this page apply to neuronx_cc **2.24.5133.0+58f8de22** (cp310). `NeuronLogger` addresses (`0x759….`) are virtual addresses in the statically-linked, **unstripped** `neuronxcc/starfish/bin/hlo-opt`, where VA == file offset for `.text`/`.rodata`. The `logging::` Boost.Log symbols are cited from `neuronxcc/starfish/bin/walrus_bugpoint_driver` (which statically links `liblogging`), so those VAs are walrus-local; the standalone `liblogging.so` re-bases them. Other versions and other tools will differ.*

## Abstract

Native compiler diagnostics in neuronx-cc are produced by **two completely separate C++ loggers** that share nothing but an enum and a default filename. The first, **`NeuronLogger`**, is a Meyers singleton compiled directly into each big native tool (`hlo-opt`, `hlo2penguin`, `walrus`, …). It is a hand-rolled mini-logger: three `std::ostringstream` buffers, three integer severity thresholds, a default logfile name `"log-neuron-cc.txt"`, and a `flush()` that opens that file and dumps one buffer. It never touches Boost.Log. The second, **`logging::Logger`**, lives in `liblogging` and *is* a full `boost::log::v2s_mt_posix` severity/channel logger with a six-element attribute set, a phoenix formatter, and two asynchronous sinks (a colorized console `MergedSink` and a file `text_ostream_backend`). The Python `neuronxlogger` façade (3.19) drives the second one; library-internal `NeuronLogger::getInstance() << …` call sites drive the first.

The reason this matters to a reimplementer is that the two loggers use **two different LogLevel encodings**. `NeuronLogger` gates with a compact integer ordinal (defaults: console threshold `4`, file threshold `2`) where a lower threshold is chattier. `logging::LogLevel` is the public enum `{TRACE=0, DEBUG=10, INFO=20, WARNING=30, ERROR=40, FATAL=50, USER=60, OFF=70}` — a 0,10,…,70 scale, proven byte-for-byte from the jump table in `logging::operator<<(ostream&, LogLevel)`. A bridge table, `neuronToABSLLogLevelMapping`, maps the compact ordinals to `absl::LogSeverity`. Conflating the two scales is the single most likely reimplementation error, and an earlier analysis pass did exactly that (see the correction note in §1).

This page reconstructs both: the `NeuronLogger` struct layout (≈0x4A8 bytes, dominated by three 0x178-byte `ostringstream`s), its lazy-singleton `getInstance`, its per-record `operator<<` format, its `getCurrentTime` microsecond clock, and its `flush`; then the `logging::Logger` Boost.Log type, attribute set, formatter term order, and the two sink front-ends.

For reimplementation, the contract is:

- The `NeuronLogger` struct layout and the three threshold fields (which offset gates console vs file).
- The Meyers-singleton `getInstance` lazy init (guard byte, instance static, `__cxa_atexit` teardown).
- The per-record emission format and the developer log line it produces.
- The `logging::LogLevel` 0..70 enum and how it differs from the compact `NeuronLogger` ordinal.
- The `logging::Logger` Boost.Log source type, the six attributes, the phoenix formatter term order, and the two async sinks.

| | |
|---|---|
| **NeuronLogger singleton** | `NeuronLogger::getInstance()` `0x759c740` (123 bytes), Meyers pattern |
| **Guard byte / instance** | `_ZGVZ…E8instance` `0x9aa2828` · `_ZZ…E8instance` `0x9aa2840` (hlo-opt `.bss`) |
| **Constructor (C1/C2)** | `0x759c2f0` (1077 bytes) |
| **Destructor (D1/D2)** | `0x759d490` (475 bytes) — calls `flush()` |
| **flush()** | `0x759d060` (1065 bytes) — `open(name, in\|trunc)`, dumps file buffer |
| **Struct size** | ≈ `0x4A8` bytes (three `std::ostringstream` at +0x48 / +0x1C0 / +0x338) |
| **Default thresholds** | console `+0x14 = 4`, file `+0x18 = 2`, curLevel `+0x1C = 2` |
| **Default logfile** | `"log-neuron-cc.txt"` (hlo-opt rodata `0x232429`) |
| **liblogging logger** | `logging::Logger` = `boost::log::v2s_mt_posix` severity/channel logger |
| **liblogging LogLevel** | `{TRACE 0, DEBUG 10, INFO 20, WARNING 30, ERROR 40, FATAL 50, USER 60, OFF 70}` |
| **Console / file setup** | `Logger::setup_console_logging` `0x6ee740` · `setup_logfile_logging` `0x6ef06a` |

> **CORRECTION (LOG-A07) —** an earlier pass described "the `NeuronLogger` singleton" as *"backed by Boost.Log v2s_mt_posix severity/channel logger with attributes level_attr, pid_attr, module_attr, source_attr, smessage."* That description is correct for **`logging::Logger` in liblogging**, but **wrong for `NeuronLogger`**: the hlo-opt singleton calls only `std::filebuf`/`std::ostream`/`std::stringbuf` (proven by `flush()` @`0x759d060` referencing no `boost::log` symbol) and is a 3×`ostringstream` mini-logger. The two are distinct subsystems. This page keeps them apart by name everywhere.

---

## 1. The Two-Logger Boundary

The strongest evidence that there are two distinct loggers is symbolic and direct: the unstripped `hlo-opt` exports the entire `NeuronLogger::*` method family with its **own** singleton storage, while `walrus_bugpoint_driver` (and the standalone `liblogging.so`) export a **separate** `logging::Logger::*` family with its own static sink front-ends. There is no symbol, vtable, or data pointer linking one to the other.

| Logger | Lives in | Singleton / state | Backend | Driven by |
|---|---|---|---|---|
| **`NeuronLogger`** | each native tool (compiled in) | `getInstance()::instance` `0x9aa2840` | 3×`ostringstream` + `ofstream` | C++ call sites `NeuronLogger::getInstance() << …` |
| **`logging::Logger`** | `liblogging` (`.so`) | `consoleSinkFrontEnd` `0x26446c0`, `fileSinkFrontEnd` `0x26446e0` | Boost.Log `v2s_mt_posix` async sinks | Python `neuronxlogger` (3.19) + `logging::` free fns |

The only contract they share is the **`LogLevel` name set** and the default filename `"log-neuron-cc.txt"`. The encodings are different (§5), the storage is different, the format is different, and neither calls the other.

> **GOTCHA —** "they share the enum *names*" does not mean they share the enum *values*. `NeuronLogger`'s threshold compares use a compact ordinal (`0,1,2,3,4,…`); `logging::LogLevel` uses `0,10,20,…,70`. The `neuronToABSLLogLevelMapping` bridge (§5b) keys on the **compact** ordinal — so it is `NeuronLogger`'s scale, not `logging::Logger`'s. A reimplementer who feeds `logging::LogLevel::ERROR (40)` into a `NeuronLogger` threshold gets a value far above the `4` default and silences nothing it intended to.

---

## 2. NeuronLogger — Singleton & Lifecycle

### Purpose

`NeuronLogger` is the in-process developer logger for the native tools. A call site does `NeuronLogger::getInstance().setSourceFile(__FILE__).setSourceLine(__LINE__).setCurLogLevel(lvl) << "msg"`; the streamed record is gated by the per-instance thresholds, timestamped, prefixed with a level tag and `file:line`, and accumulated into one of three `ostringstream`s. `flush()` (also called by the destructor at exit) rewrites `log-neuron-cc.txt` with the file buffer.

### Entry Point

```text
NeuronLogger::getInstance()                  0x759c740 (123 B)   ── Meyers singleton
  ├─ guard byte _ZGVZ…E8instance @0x9aa2828  ── first-use latch
  ├─ __cxa_guard_acquire / __cxa_guard_release
  ├─ NeuronLogger::NeuronLogger() (C2)       0x759c2f0 (1077 B)  ── builds state
  └─ __cxa_atexit(NeuronLogger::~NeuronLogger (D1) @0x759d490, &instance, __dso_handle)
       └─ ~NeuronLogger() calls flush()      0x759d060 (1065 B)  ── final dump
```

### Algorithm

The disassembly at `0x759c740` is a textbook GCC Meyers singleton: a fast path that returns `&instance` when the guard byte is already set, and a slow path under `__cxa_guard_acquire` that constructs once and registers `__cxa_atexit`.

```c
NeuronLogger* NeuronLogger::getInstance():            // 0x759c740
    if (guard_byte /* @0x9aa2828 */ != 0)             // movzx eax,[guard]; test al,al
        return &instance;                             // @0x9aa2840 — fast path

    if (__cxa_guard_acquire(&guard_byte) != 0):        // 0x759c767 — we win the race
        NeuronLogger::NeuronLogger(&instance);         // C2 @0x759c2f0
        __cxa_atexit(&NeuronLogger::~NeuronLogger,     // D1 @0x759d490
                     &instance, &__dso_handle);        // 0x759c799
        __cxa_guard_release(&guard_byte);              // 0x759c7a3
    return &instance;                                  // 0x759c7ac
    // cold path (0x759c726): __cxa_guard_abort + _Unwind_Resume on ctor throw
```

> **NOTE —** the guard byte and the instance object are adjacent in `.bss`: guard at `0x9aa2828`, object at `0x9aa2840`, a 24-byte gap that is alignment padding for the singleton's `ostringstream`s. The `__cxa_atexit` argument order `(destructor, &instance, &__dso_handle)` is the standard libstdc++ contract — the same one used by the unrelated `_GLOBAL__sub_I` static initializer that builds the bridge table (§5b).

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `NeuronLogger::getInstance()` | `0x759c740` | 123 | Meyers singleton accessor | CERTAIN |
| `NeuronLogger::getInstance()::cold` | `0x759c726` | 18 | `__cxa_guard_abort` + `_Unwind_Resume` | CERTAIN |
| `NeuronLogger::NeuronLogger()` (C2/C1) | `0x759c2f0` | 1077 | ctor — thresholds, logfile string, 3 streams | CERTAIN |
| `NeuronLogger::~NeuronLogger()` (D1/D2) | `0x759d490` | 475 | dtor — calls `flush()`, tears down streams | CERTAIN |
| `NeuronLogger::flush()` | `0x759d060` | 1065 | open logfile, dump file buffer, re-sync | CERTAIN |
| `NeuronLogger::getCurrentTime[abi:cxx11]()` | `0x759c8e0` | 1764 | timestamp string with µs field | CERTAIN |
| `NeuronLogger::setSourceFile(const char*)` | `0x759c1b0` | 33 | `[+0]=strlen(s); [+8]=s` | CERTAIN |
| `NeuronLogger::setSourceLine(int)` | `0x759c1e0` | 4 | `[+0x10]=line` | CERTAIN |
| `NeuronLogger::setCurLogLevel(LogLevel)` | `0x759c210` | 4 | `[+0x1C]=level` | CERTAIN |
| `NeuronLogger::shouldLogToConsole(LogLevel)` | `0x759c1f0` | 7 | `return [+0x14] <= level` | CERTAIN |
| `NeuronLogger::shouldLogToFile(LogLevel)` | `0x759c200` | 7 | `return [+0x18] <= level` | CERTAIN |
| `NeuronLogger::operator<<<char>(const char&)` | `0x1e80d60` | 1736 | per-record format emitter | HIGH |
| `_GLOBAL__sub_I__ZN12NeuronLoggerC2Ev` | `0x759d670` | 163 | builds `neuronToABSLLogLevelMapping` | CERTAIN |

`operator<<` is instantiated per argument type — `lsIcE` (`0x1e80d60`), `lsImE` (`unsigned long`, `0x1f20380`), `lsINSt…basic_string…` (`0x1e74790`), `lsISt17basic_string_view…` (`0x1e7f3b0`), and the fixed char-array overloads `lsIA{2,3,5,6,10,11,14,17,26}_cE` (one per string-literal length). All funnel through `shouldLogToFile`/`getCurrentTime`, so the format below is shared.

---

## 3. NeuronLogger — Struct Layout

### Layout

Reconstructed from the constructor stores at `0x759c2f0`, the accessor offsets, and `flush`/dtor. The object is dominated by three libstdc++ `std::ostringstream`s, each 0x178 bytes (matching the `ios_base` + `basic_ios` + embedded `stringbuf` + `locale` stride), at +0x48, +0x1C0, +0x338. The last stream's `ios::init` reaches +0x488, so the total size is ≈`0x4A8`.

| Field | Offset | Type | Meaning | Evidence |
|---|---|---|---|---|
| `srcFileLen` | `+0x00` | `size_t` | strlen of source file | ctor `mov qword[rdi],0`; `setSourceFile`: `mov [rbx],rax` |
| `srcFile` | `+0x08` | `const char*` | source-file pointer | ctor `mov qword[rdi+8],0`; `setSourceFile`: `mov [rbx+8],r12` |
| `srcLine` | `+0x10` | `int` | source line | `setSourceLine`: `mov [rdi+0x10],esi` |
| `consoleThreshold` | `+0x14` | `int` (ordinal) | console gate, **default 4** | ctor `mov dword[rdi+0x14],4`; `shouldLogToConsole` `cmp [rdi+0x14],esi` |
| `fileThreshold` | `+0x18` | `int` (ordinal) | file gate, **default 2** | ctor lo-dword of `0x200000002` into `[rdi+0x18]`; `shouldLogToFile` `cmp [rdi+0x18],esi` |
| `curLogLevel` | `+0x1C` | `int` (ordinal) | current record level, **default 2** | ctor hi-dword of `0x200000002`; `setCurLogLevel` `mov [rdi+0x1C],esi` |
| `flag` | `+0x20` | `byte` | init 0; semantic unproven | ctor `mov byte[rdi+0x20],0` |
| `logfileName` | `+0x28` | `std::string` (SSO) | `"log-neuron-cc.txt"`; SSO buf at +0x38 | ctor `lea rax,[rdi+0x38]; mov [rdi+0x28],rax; _M_construct` |
| `streamA` | `+0x48` | `std::ostringstream` (0x178) | console/scratch buffer | ctor block `0x759c351`–`0x759c46e` |
| `streamFile` | `+0x1C0` | `std::ostringstream` (0x178) | **file buffer** (dumped by `flush`) | ctor block `0x759c46e`–`0x759c5ac`; `flush` reads `[+0x1F0]/[+0x1E0]/[+0x210]` |
| `streamC` | `+0x338` | `std::ostringstream` (0x178) | third buffer; role unconfirmed | ctor block `0x759c5ac`–`0x759c6e3` |

### Considerations

The single 64-bit store `mov rax, 0x200000002 ; mov [rdi+0x18], rax` writes **two** fields at once: `fileThreshold(+0x18)=2` (low dword) and `curLogLevel(+0x1C)=2` (high dword). `consoleThreshold(+0x14)=4` is a separate `mov dword[rdi+0x14],4`. So the default posture is *console quieter than file* (console threshold 4 > file threshold 2): more records reach the file than the console.

> **QUIRK —** which stream is the file buffer is not the first one. `flush()` dumps **`streamFile` at +0x1C0**, the *second* `ostringstream`, not `streamA` at +0x48. This is proven by `flush` reading the stringbuf data/end/base of the +0x1C0 object (`[rbx+0x1F0]` data ptr, `[rbx+0x1E0]` end, `[rbx+0x210]` base, `[rbx+0x208]` length). A reimplementer who assumes "first stream = file" dumps the wrong buffer.

> **NOTE —** the `+0x20` byte (init 0) and the role of the third stream `streamC` at +0x338 are not proven. The byte is plausibly an "enabled/initialized" flag and `streamC` a console/stderr mirror of `streamA`, but only `streamFile` (+0x1C0) is anchored by `flush`. Both are tagged INFERRED.

---

## 4. NeuronLogger — Record Format, Clock & Flush

### Algorithm — per-record `operator<<`

Every `operator<<` overload, gated by `shouldLogToFile(curLogLevel)`, emits a fixed prefix into the file buffer before the message. The char overload at `0x1e80d60` shows the sequence and the level-tag selection.

```c
NeuronLogger& operator<<(...):                         // e.g. 0x1e80d60
    gate = shouldLogToFile(this->curLogLevel);         // 0x1e80d96
    tag2 = (this->curLogLevel <= 3) ? "I " : "E ";     // 0x1e80db3 cmp [+0x1C],3; setle
                                                       //  (2-char level tag, len=2)
    stream << getCurrentTime();                        // 0x1e80e03 "YYYY-MM-DD HH:MM:SS.uuuuuu"
    stream << ": ";                                    // asc_24B70D @0x24b70d
    stream << tag2;                                    // selected level tag
    stream << this->srcFile;                           // [+0x08], strlen'd
    stream << ":";                                     // delim @0x252fee
    stream << this->srcLine;                           // [+0x10] as int
    stream << "] ";                                    // asc_25F073 @0x25f073
    stream << <message…>;
```

The produced developer line is, for example:

```text
2026-04-08 21:07:10.123456: E /path/to/file.cc:123] message text
```

> **NOTE —** the two-character tag is selected by `curLogLevel <= 3` (`setle`) at `0x1e80db3`, and the tag *length* (2) is stored alongside. The exact bytes of the two tags (`"I "` for the chatty branch, `"E "` for the severe branch) come from the level-tag rodata at `ds:0x2045 + idx*4`; this decode is HIGH, the gate logic is CERTAIN.

### Algorithm — `getCurrentTime`

```c
std::string getCurrentTime():                          // 0x759c8e0
    ns  = system_clock::now().time_since_epoch();       // nanoseconds
    sec = ns / 1'000'000'000;                            // magic 0x112E0BE826D694B3, sar 26
    usec= (ns / 1000) % 1'000'000;                       // 0..999999
    tm  = localtime(&sec);
    ss << put_time(tm, "%Y-%m-%d %H:%M:%S");             // fmt @hlo-opt rodata 0x2495BF
    ss << ".";
    ss << setw(6) << setfill('0') << usec;               // 6-digit zero-pad: mov [..],6; fill '0'
    return ss.str();
```

The verbatim format string `"%Y-%m-%d %H:%M:%S"` is confirmed present in hlo-opt rodata, followed by a literal `"."` and a 6-digit zero-padded microsecond field. (CERTAIN.)

### Algorithm — `flush`

```c
void flush():                                          // 0x759d060
    ofstream ofs;                                       // local ios_base + filebuf
    ofs.open(this->logfileName /* [rbx+0x28] */,        // 0x759d12b mov rsi,[rbx+0x28]
             mode = 0x11);                              // 0x759d12f mov edx,0x11 = in|trunc
    if (ofs.is_open()):                                 // 0x759d169 __basic_file::is_open
        ofs << streamFile.str();                        // dumps +0x1C0 buffer (data @[+0x1F0])
        ofs.put('\n'); ofs.flush();
        filebuf.close();
    // re-sync the file stringbuf so the next batch starts clean
```

> **GOTCHA —** the open mode is `0x11 = in | trunc` (decoded against libstdc++ `_Ios_Openmode{in=1, out=2, ate=4, app=8, trunc=0x10, binary=0x20}`). Because every `flush()` **truncates**, the file is *rewritten* from the accumulated buffer each time — it is not an append log. A reimplementer who opens with `app` produces a growing file with duplicated history; the original produces a single snapshot of the current buffer.

---

## 5. LogLevel — Two Encodings

### 5a. `logging::LogLevel` (liblogging) — the 0..70 scale

Decoded byte-for-byte from the `cmp`/`jz` jump table in `logging::operator<<(ostream&, LogLevel)` (`0x6ee358`) and the rodata name strings. Each case compares the level against the constant shown and emits the named string.

| LogLevel | Value | `cmp` constant | Name string | Confidence |
|---|---|---|---|---|
| TRACE | 0 | `0x00` | `"TRACE"` | CERTAIN |
| DEBUG | 10 | `0x0A` | `"DEBUG"` | CERTAIN |
| INFO | 20 | `0x14` | `"INFO"` | CERTAIN |
| WARNING | 30 | `0x1E` | `"WARNING"` | CERTAIN |
| ERROR | 40 | `0x28` | `"ERROR"` | CERTAIN |
| FATAL | 50 | `0x32` | `"FATAL"` | CERTAIN |
| USER | 60 | `0x3C` | `"USER"` | CERTAIN |
| OFF | 70 | `0x46` | `"OFF"` | CERTAIN |

The endpoints are proven: the `operator<<` switch compares the top of the chain against `0x46` (`cmp [var_C], 0x46h` at `0x6ee367`, with `jg` falling through to the no-op exit for anything above 70) and bottoms out at `cmp …, 0` → `"TRACE"`. This is the same enum the Python `neuronxlogger` façade mirrors (TRACE = `NOTSET+5`, FATAL = `CRITICAL`, USER = `CRITICAL+10`, OFF = `CRITICAL+20`; cross-ref 3.19).

### 5b. `neuronToABSLLogLevelMapping` — the bridge to absl

A `std::unordered_map<LogLevel, absl::LogSeverity>` built once at static-init time by `_GLOBAL__sub_I__ZN12NeuronLoggerC2Ev` (`0x759d670`). The init stack-builds a 6-pair array (each 8-byte pair = `{key (low dword), value (high dword)}`) and hands its `[begin, end)` to the `_Hashtable` ctor. `absl::LogSeverity = {INFO=0, WARNING=1, ERROR=2, FATAL=3}`.

| Stack pair (qword) | Key (compact ordinal) | absl severity | Confidence |
|---|---|---|---|
| `0x00000000_00000002` | 2 | 0 (INFO) | HIGH |
| `0x00000000_00000001` | 1 | 0 (INFO) | HIGH |
| `0x00000001_00000003` | 3 | 1 (WARNING) | HIGH |
| `0x00000002_00000004` | 4 | 2 (ERROR) | HIGH |
| `0x00000003_00000006` | 6 | 3 (FATAL) | HIGH |
| `0x00000003_00000005` | 5 | 3 (FATAL) | HIGH |

> **QUIRK —** these keys are the **compact 1..6 ordinal**, *not* the `0,10,…,70` scale of §5a. That is the proof that two LogLevel encodings coexist in the same process: `logging::LogLevel` is the 10-step public enum, while `NeuronLogger`'s thresholds and this bridge table speak the compact ordinal. (The exact key→severity pairing is HIGH not CERTAIN because the on-stack pair ordering versus map insertion order was not single-stepped; the six pairs themselves are read directly from the `mov` immediates at `0x759d6ba`–`0x759d6e6`.)

### 5c. `NeuronLogger` compact threshold gates

```c
bool shouldLogToConsole(int level): return consoleThreshold(+0x14) <= level;  // cmp/setle
bool shouldLogToFile(int level):    return fileThreshold(+0x18)    <= level;  // cmp/setle
```

A record reaches a sink when `threshold <= record_level` — i.e. a **lower threshold is chattier**. Defaults: console `4`, file `2`, current `2`. The `operator<<` additionally branches on `curLogLevel <= 3` to pick the 2-char tag (§4). This compact ordinal is the severity scale `NeuronLogger` actually gates on; it is unrelated to the 10-step `logging::LogLevel`.

### 5d. liblogging level accessors

| Symbol | Addr (walrus) | Role |
|---|---|---|
| `logging::Logger::verbose()` | `0x6ee4f8` | read current console verbosity |
| `logging::getConsoleLogLevel()` | `0x6ee55c` | read console level global (int) |
| `logging::setConsoleLogLevel(LogLevel)` | `0x6ee53f` | set console level global |
| `logging::Logger::to_log_level(int)` | `0x6ee56a` | one-time int→LogLevel table init |
| `logging::Logger::isEnabledFor(LogLevel)` | `0x6efc00` | open a record with `keywords::tag::severity`, let core filter decide |
| `logging::Logger::flushConsole()` | `0x6efd5c` | flush console sink |
| `logging::Logger::flushLogfile()` | `0x6efc6a` | flush file sink |
| `logging::Logger::shutdown()` | `0x6efa5c` | tear down sinks |

`isEnabledFor` does not read a local int and compare; it opens a Boost.Log record tagged with the severity keyword and lets the core's filter predicate (§6) decide — the canonical Boost.Log pattern. `to_log_level` guards a one-time int→`LogLevel` table init (touches a guard near `fileSinkFrontEnd`).

---

## 6. logging::Logger — the Boost.Log Machinery

### Logger source type

The `logging::Logger` source is a `boost::log::v2s_mt_posix` composite logger. The full demangled type (from `basic_composite_logger…C2` at walrus `0x61e6a0` and the constructor symbol) is:

```c
logging::Logger =
  basic_composite_logger< char, logging::Logger, single_thread_model,
    features<
      severity< logging::LogLevel >,         // severity feature
      channel< std::string >,                // channel feature; default channel "Logging"
      mpl::quote1< logging_internal::type_feature >   // custom LogType feature
    > >
```

So it is severity + channel + one custom feature (`type_feature`, carrying `logging::LogType`). The default channel string is `"Logging"` (rodata). The threading model is `single_thread_model` (the records are pumped into async sinks; the model refers to the *source*, not the sinks). (CERTAIN — demangled symbols.)

### Attribute set (6 members)

Each attribute tag is a `logging_internal::tag::*` with a `get_name()` export. The phoenix formatter and the value-ref machinery reference all six.

| Attribute tag | Value type | Confidence |
|---|---|---|
| `level_attr` | `logging::LogLevel` | CERTAIN |
| `source_attr` | `std::string` | CERTAIN |
| `pid_attr` | `aux::id<aux::process>` (process id) | CERTAIN |
| `module_attr` | `std::string` | CERTAIN |
| `type_attr` | `logging::LogType` | CERTAIN |
| `smessage` | Boost.Log built-in message tag | CERTAIN |

> **CORRECTION (LOG-A07) —** the earlier pass listed five attributes (`level/pid/module/source/smessage`). The demangled formatter and the `type_dispatcher<logging::LogType>` / `attribute_value_impl<logging::LogType>` symbols add a **sixth**: `type_attr (logging::LogType)`. The attribute set has six members.

### Formatter — phoenix term order

The formatter is a `boost::log::v2s_mt_posix::basic_formatter<char>` built from a phoenix `shift_left` (`<<`) expression tree. Reading the deepest-nested-first term order out of the demangled `basic_expr<…shift_left…>` type gives:

```text
stream << function_eval[ std::string(*)() ]            // timestamp function (no-arg string fn)
       << A2_c                                          // 2-char literal separator
       << attribute_actor<LogLevel, level_attr>(fallback_to_none)
       << A2_c                                          // 2-char literal separator
       << value_ref<id<process>, pid_attr>              // process id
       << value_ref<string, module_attr>                // module
       << A3_c                                          // 3-char literal separator
       << value_ref<string, source_attr>                // source
       << A4_c                                          // 4-char literal separator
       << tag::smessage                                 // the message body
```

The separator *widths* (A2 / A3 / A4 — array-of-char terminals `A2_c`, `A3_c`, `A4_c`) are read directly from the formatter type. The printable rodata neighbors of the level table give the building blocks — `" ("`, `"]: "`, the `"module"` keyword. The formatter imbues `en_US.UTF-8` (rodata string confirmed).

> **NOTE —** the exact bytes of the A2/A3/A4 literals are not byte-extracted from the compiled phoenix tree, so the fully-rendered Boost line layout is HIGH, not CERTAIN. The term *order*, the attribute *types*, and the separator *widths* are CERTAIN from the demangled formatter type.

### Two async sinks

Both sinks are `asynchronous_sink<…, unbounded_fifo_queue>` registered on `core::get()`.

| Sink | Front-end / backend | Setup fn | Confidence |
|---|---|---|---|
| **console** | `asynchronous_sink<logging::CustomSink, unbounded_fifo_queue>`; `CustomSink` is a `basic_formatting_sink_frontend<char>` subclass; default impl `logging_internal::MergedSink` (`make_shared<MergedSink>`) | `Logger::setup_console_logging(int, shared_ptr<CustomSink>)` `0x6ee740` | CERTAIN |
| **file** | `asynchronous_sink<basic_text_ostream_backend<char>, unbounded_fifo_queue>`; backend `add_stream(shared_ptr<ofstream>)` | `Logger::setup_logfile_logging(string const&, int)` `0x6ef06a` | CERTAIN |

Static sink front-end handles in liblogging `.bss`: `consoleSinkFrontEnd` (`0x26446c0` in walrus), `fileSinkFrontEnd` (`0x26446e0`).

The console write path is `MergedSink::consume(record_view const&, std::string const& formatted)`, which routes between STDOUT and STDERR by severity and wraps severe lines in ANSI color — red `"\x1b[31;20m"` (`[31;20m` confirmed in rodata) with reset `"\x1b[0m"`. The channel names `"STDOUT"` and `"STDERR"` are confirmed strings.

### One-time-setup guards

Re-registering a sink is a fatal error. The exact strings are confirmed in the binary:

```text
Duplicate attempts to setup console logging.
Duplicate attempts to setup file logging.
```

> **GOTCHA —** the duplicate-setup checks make `setup_console_logging` / `setup_logfile_logging` *one-shot*. A reimplementation that re-initializes logging on, say, a second compile pass in the same process must guard against it or it aborts. (The companion `"Can't dump trace to an empty filename"` guard is reported by the prior pass for the standalone `liblogging.so`; it was not re-located in the walrus copy and is tagged INFERRED here.)

---

## Related Components

| Name | Relationship |
|---|---|
| Python `neuronxlogger` (3.19) | Python façade that drives `logging::Logger` (the Boost.Log path), not `NeuronLogger` |
| Error catalog (3.20) | Consumes the `ERROR`/`FATAL` severities and the `logging::ErrorCode` `operator<<` (`0x7174dc`) |
| Command dispatcher (3.1) | Each sub-tool process owns its own `NeuronLogger::getInstance()` singleton and `liblogging` sink set |

## Cross-References

- [Python logging façade](python-logging-facade.md) — the `neuronxlogger` Python side that drives the Boost.Log `logging::Logger`, with the matching TRACE/USER/OFF mapping
- [Error catalog](error-catalog.md) — diagnostic codes emitted at the `ERROR`/`FATAL` severities
- [The neuronx-cc Command Dispatcher & Subcommand Model](command-dispatcher.md) — the per-process model under which each tool instantiates its own logger
