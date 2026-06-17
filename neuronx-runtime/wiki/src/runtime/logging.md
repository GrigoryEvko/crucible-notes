# Logging (nlog), ntrace and the Rust Logger Bridge

> *All addresses, offsets, and symbols on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`, version namespace `NRT_2.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present and `.text`/`.rodata`/`.data` VMA equals file offset, so every `0x…` is both an analysis VMA and a file offset. The C facility is `KaenaRuntime/nlog/nlog.c` (GCC 14.2.1); the Rust bridge is statically linked from `rustc 1.91.1` (`neuron_rustime::logging`, `log 0.4.28`). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — all 15 `nlog_*` functions, the 3 `NeuronLogger` trait methods and `init_rust_logger` are each pinned to a `.symtab` symbol + `.text` address; the 12-byte `nrt_log_config_t`, 24-byte `error_log_t`, 32-byte `NlogErrorContextManager` and the per-thread TLS offsets are cross-checked against the IDA Hex-Rays decompile. The `2314`/`asc_84CC87` framing bytes are read directly from `.rodata` (`0x0A09` and `0x0A0A`). The exact set of 12 submodule ids in the anon `module` enum is `[LOW]` (array bound is firm; member strings are not enumerable from an anon-hash enum). · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

`nlog` is the runtime's single diagnostic emitter: every `nrt_*` API, every HAL bridge (`al_hal_log`), and every Rust crate routes through one variadic core, `nlog_vwrite @0x7a3e0`. A log call is not a `printf` — it is a **fan-out to five sinks** gated by a per-submodule level pair. After formatting a timestamped `pid:tid LEVEL submodule:func` header plus the caller's body, the emitter writes the line to: (1) the **ANSI-colored console** (`stdout` for INFO/DEBUG/TRACE, `stderr` for the ERROR coalesced path — red `\x1B[91m` for ERROR, magenta `\x1B[95m` for WARN, only when the target is a TTY); (2) **syslog** (`syslog(1, …)`); (3) the **kernel `ndl_printk` sink** (an `ioctl 0x40104E71` into `/dev/neuron<N>`, surfacing runtime lines in `dmesg`); (4) a **per-thread TLS coalescing ring** that batches non-fatal lines for a single fail-time flush; and (5) a **process-wide error-log ring buffer** (`error_log @0xc96990`, sized `num_vtpbs << 17` = 128 KiB per virtual TPB) that `nrt_core_dump` writes to a crash file. The level gate is two-dimensional — each of 12 submodules carries an independent `syslog_level` and `console_level` (`nrt_log_config_t`, `log_configs @0xc08a40`), so a reimplementer who models a single global level will mis-route every line.

The familiar reference frame is a **leveled logging facade with a coalescing tail buffer**, the shape of `glog` or a `tracing-subscriber` fan-out, but with two Neuron-specific twists. First, every public API wrapper is bracketed by a stack-resident RAII guard, `NlogErrorContextManager` (32 bytes, one `std::string api`), whose constructor builds the API-name string with the GCC small-string-optimization (SSO) idiom and whose destructor (`@0x8fc80`) checks `nlog_has_unflushed_logs()` and, if the thread accumulated coalesced lines without an explicit flush, emits one `"Generic API Failure"` ERROR. This guard is inlined into essentially every `nrt_*` decompile, so the SSO string-build prologue is the single most repeated code shape in the binary's API surface — recognizing it is what lets a reader skip past it to the real API body. Second, the Rust side has **no `stdout`/`stderr` writer of its own**: `init_rust_logger @0x508860` installs `neuron_rustime::LOGGER` into the `log` crate, and the `NeuronLogger::log` impl (`@0x50a4a0`) formats the record and calls straight back into `nlog_write("RUST", …)`, gated by `nlog_get_log_level("RUST")`. The C facility is therefore the sole terminal for both languages.

This page documents the facility to reimplementation accuracy: the **two-axis level/location config model** and its parse path (§1); the **five-sink emit pipeline** as annotated pseudocode, with the exact level-gate ladder, the ANSI/console framing, and the two writer halves — direct (non-coalesced) and TLS-batched (§2); the **per-thread coalescing ring** layout and its `nlog_coalescing_init_thread` seed (§3); the **`NlogErrorContextManager` RAII guard** and why its SSO build dominates the API decompiles (§4); and the **Rust logger bridge** wiring `NeuronLogger::{log,enabled,flush}` into `nlog` (§5). The host-side `ntrace` event ring (`KaenaRuntime/tdrv/ntrace.c`) and the `pool_stdio` GPSIMD stdout capture are the two non-`nlog` log producers in this band and are summarized in §6; the Rust `sys_trace` capture engine and the deterministic interner are owned by [trace/rust-capture](../trace/rust-capture.md) and [runtime/interned-strings](interned-strings.md) and are not re-derived here.

For reimplementation, the contract is:

- **The config model** — 12 `nrt_log_config_t` slots (`log_configs @0xc08a40`, 12 B each, sentinel `algn_C08AD0 @0xc08ad0`), keyed on the first 4 bytes of the submodule name (`*(u32*)submodule`), each carrying an independent `syslog_level` and `console_level` ∈ `{DISABLED,ERROR,WARN,INFO,DEBUG,TRACE}` = `0..5`. A scalar `nlog_cur_max_log_level @0xc08b00` is the global short-circuit ceiling.
- **The emit pipeline** — `nlog_vwrite @0x7a3e0` (2878 B, 104 blocks): global-ceiling gate → submodule lookup → per-axis level gate → header format (`"%4d-%3s-%02d %02d:%02d:%02d.%06d"` + `" %5d:%-5d %5s %5s:%-40s"`) → fan-out to console (ANSI, TTY-only) / syslog / `ndl_printk` / `print_to_error_log` / TLS coalescing ring.
- **The TLS coalescing ring** — a ~16 KiB per-thread region (TLS base `+0x0..+0x4000`) filled **downward** (`+16388` write pointer counts down from `0x4000`), with a pending-count at `+16396`, an enable byte at `+16392`, an error-cause/resolution index at `+0x4000`, and the `0x3FFEFFFFFFFF` reset sentinel; framed with `0x0A09` (`"\n\t"`) line joiners and flushed as one block when an API-level ERROR fires.
- **The RAII error-context** — `NlogErrorContextManager` (32 B, one SSO `std::string api`): ctor builds the API-name string inline; dtor `@0x8fc80` flushes a `"Generic API Failure"` if `nlog_has_unflushed_logs()`. Inlined into every API wrapper.
- **The Rust bridge** — `init_rust_logger @0x508860` → `log::set_logger(LOGGER)` + `MAX_LOG_LEVEL_FILTER = 5`; `NeuronLogger::log @0x50a4a0` formats and calls `nlog_write("RUST", …)` gated by `nlog_get_log_level("RUST")`. `flush` is a no-op (nlog writes synchronously).

| | |
|---|---|
| **Source TU** | `KaenaRuntime/nlog/nlog.c` (C, GCC 14.2.1); Rust bridge in `neuron_rustime::logging` (`src/logging.rs`) |
| **Core emitter** | `nlog_vwrite @0x7a3e0` (2878 B, 104 bb); variadic shim `nlog_write @0x224d40` |
| **Config array** | `log_configs @0xc08a40` — `nrt_log_config_t[12]` (12 B each); sentinel `algn_C08AD0 @0xc08ad0` |
| **Global ceiling** | `nlog_cur_max_log_level @0xc08b00` (`nrt_log_level_t`) |
| **Level enum** | `nrt_log_level_t` = `{DISABLED 0, ERROR 1, WARN 2, INFO 3, DEBUG 4, TRACE 5}` |
| **Error-log ring** | `error_log @0xc96990` (24 B `error_log_t`), capacity `num_vtpbs << 17` (128 KiB/vtpb), guarded by `error_log_mutex @0xc96960` |
| **TLS coalescing** | per-thread `__tls_get_addr()` region; write-ptr `+16388`, enable `+16392`, count `+16396`, cause `+0x4000` |
| **Console TTY flags** | `nlog_stdout_is_tty @0xc96940` / `nlog_stderr_is_tty @0xc96941` (cached by `nlog_constructor @0x77b60`) |
| **Coalescing enable** | `log_coalescing_enabled @0xc96942` (set by `nlog_init`) |
| **HW-error sink** | `nlog_printk_hardware_error @0x224dd0` → `ndl_printk` (ISO-8601 `NEURON_HW_ERR=` line) |
| **RAII guard** | `NlogErrorContextManager` (32 B); dtor `@0x8fc80` → `nlog_has_unflushed_logs @0x2250d0` |
| **Rust bridge** | `init_rust_logger @0x508860`; `NeuronLogger::{log @0x50a4a0, enabled @0x50a760, flush @0x508820}` |

---

## 1. The Level / Location Config Model

### Purpose

`nlog` does not have a level — it has a **12 × 2 matrix** of levels. The static array `log_configs @0xc08a40` holds 12 `nrt_log_config_t` entries, one per submodule; each entry carries an independent `syslog_level` *and* `console_level`. A log line is gated against the submodule's two levels separately, which is how the runtime can (for example) drive an INFO stream to `dmesg`/syslog while keeping the console at ERROR, or silence one noisy submodule's console without touching the rest. A reimplementer who collapses this to a single global level will route every line to both sinks at one threshold and lose the per-submodule and per-sink independence that the env knobs (`NEURON_RT_LOG_LEVEL_<MOD>`, `NEURON_RT_LOG_LOCATION_<MOD>`) exist to express.

### `nrt_log_config_t` (12 bytes)

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `module` | `+0x00` | anon `u32` enum | submodule id; the key, compared as `*(u32*)submodule` — the first 4 bytes of the submodule name | HIGH |
| `syslog_level` | `+0x04` | `nrt_log_level_t` | threshold for the `syslog()` sink (`0` = DISABLED ⇒ no syslog) | HIGH |
| `console_level` | `+0x08` | `nrt_log_level_t` | threshold for the `stdout`/`stderr` console sink | HIGH |

The array is iterated to the symbol-derived sentinel `algn_C08AD0 @0xc08ad0` (`0xc08ad0 − 0xc08a40 = 0x90 = 12 × 12` bytes ⇒ exactly 12 slots). The `module` key is the *integer reinterpretation of the first four characters* of the submodule string: both `nlog_set_log_level` and `nlog_vwrite` compare `v13->module.id == *(u32*)submodule`, never a `strcmp`. So `"RUST"`, `"NLOG"`, `"NRT"` etc. each collapse to one 4-byte key, and two submodules sharing a 4-byte prefix would alias the same slot. `[HIGH]`

> **GOTCHA —** the submodule key is `*(u32*)submodule`, **not** a string compare. Two submodule names with the same first four bytes select the **same** `log_configs` slot. The runtime's tags are chosen to be 4-byte-distinct, but a reimplementer adding a tag must keep the first four bytes unique or silently share another module's level pair. `[HIGH]`

### `nrt_log_level_t`

```text
nrt_log_level_t (DWARF enum, verified):
   NRT_LOG_LEVEL_DISABLED = 0     // sink off
   NRT_LOG_LEVEL_ERROR    = 1
   NRT_LOG_LEVEL_WARN     = 2
   NRT_LOG_LEVEL_INFO     = 3
   NRT_LOG_LEVEL_DEBUG    = 4
   NRT_LOG_LEVEL_TRACE    = 5
```

`nlog_cur_max_log_level @0xc08b00` is a single scalar holding the **maximum** level enabled across all 12 submodules and both sinks; `nlog_vwrite` early-outs on `level > nlog_cur_max_log_level` before any submodule lookup, so a fully-quiet runtime pays one comparison per log call. `nlog_set_log_level` bumps it monotonically (`if (nlog_cur_max_log_level < v4) nlog_cur_max_log_level = v4`) — it is never lowered, so it is a ceiling for the fast-path, not an exact maximum once a level has been reduced.

### Algorithm — `nlog_set_log_level`

```c
// Models nlog_set_log_level @0x224b30.
// Parses a level NAME and a location NAME, writes both axes into log_configs[].
function nlog_set_log_level(submodule, location, level_name):     // -> NRT_STATUS
    // 1. level: case-insensitive substring probe, ERROR-first, default ERROR
    if      strcasestr(level_name, "ERROR"): lvl = ERROR
    else if strcasestr(level_name, "WARN"):  lvl = WARN
    else if strcasestr(level_name, "INFO"):  lvl = INFO
    else if strcasestr(level_name, "DEBUG"): lvl = DEBUG
    else if strcasestr(level_name, "TRACE"): lvl = TRACE
    else: printf("invalid level %s, default to ERROR\n", level_name); lvl = ERROR

    // 2. location: SYSLOG and/or CONSOLE; default CONSOLE if neither present
    has_syslog  = strcasestr(location, "SYSLOG") != NULL
    has_console = strcasestr(location, "CONSOLE") != NULL
    if !has_syslog && !has_console:
        printf("invalid log location setting %s, default to CONSOLE\n", level_name)  // [sic] prints level_name
        has_console = 1

    syslog_lvl  = has_syslog  ? lvl : DISABLED      // axis off when its location absent
    console_lvl = has_console ? lvl : DISABLED

    // 3. apply to one submodule (key = *(u32*)submodule) or "all"
    key = *(u32*)submodule
    if submodule != "all":
        for cfg in log_configs[0..12]:             // sentinel algn_C08AD0
            if cfg.module.id == key:
                cfg.syslog_level  = syslog_lvl
                cfg.console_level = console_lvl
    else:
        for cfg in log_configs[0..12]:             // wildcard: every submodule
            cfg.syslog_level  = syslog_lvl
            cfg.console_level = console_lvl

    if nlog_cur_max_log_level < lvl:               // raise the fast-path ceiling
        nlog_cur_max_log_level = lvl
    return NRT_SUCCESS
```

> **QUIRK —** the "invalid log location" diagnostic prints `level_name`, not `location` (`printf("invalid log location setting %s …", level_name)` in the decompile). This is a verbatim bug in the source, reproduced here for fidelity — a reimplementer copying the message format should pass the *location* string; the binary passes the wrong one. `[HIGH]`

### Algorithm — `nlog_get_log_level`

```c
// Models nlog_get_log_level @0x224cd0. Read side, also the Rust bridge's gate.
function nlog_get_log_level(submodule, out_level):     // -> NRT_STATUS
    key = *(u32*)submodule
    for i in 0..12:                                     // hard bound 12
        if log_configs[i].module.id == key:
            lvl = log_configs[i].syslog_level
            if lvl == DISABLED:
                lvl = log_configs[i].console_level      // fall back to console axis
            if lvl == DISABLED:
                puts("At least one of syslog_level or console_level should be enabled")
                return NRT_INVALID
            *out_level = lvl
            return NRT_SUCCESS
    return NRT_INVALID                                  // unknown submodule
```

`nlog_get_log_level` reports a *single* effective level per submodule — syslog-preferred, console-fallback — and is the function the Rust `NeuronLogger` consults (§5). It returns `NRT_INVALID` both for an unknown submodule and for one whose two axes are both `DISABLED`; the latter prints the `"At least one of …"` diagnostic.

---

## 2. The Emit Pipeline — `nlog_vwrite`

### Purpose

`nlog_vwrite @0x7a3e0` is the single emit core (2878 bytes, 104 basic blocks); every other path is a shim into it. `nlog_write @0x224d40` is the variadic public entry — `va_start` then a direct tail into `nlog_vwrite`. The signature carries two booleans that steer the fan-out: `coaleced_msg` (route through the per-thread TLS batch rather than emit immediately) and `api_level` (this is a top-level API line, which makes the coalesced batch eligible for the synchronous fail-time flush). The function's job is: gate the level twice (global ceiling, then per-submodule per-sink), build the header + body once, then write to the enabled sinks.

### Entry Point

```text
nlog_write (0x224d40)                       ── variadic shim: va_start -> nlog_vwrite
  └─ nlog_vwrite (0x7a3e0)                   ── CORE EMITTER (2878 B, 104 bb)
       ├─ log_configs[] lookup (0xc08a40)    ── *(u32*)submodule key
       ├─ gettimeofday + localtime_r         ── timestamp header
       ├─ snprintf/vsnprintf                  ── header + body into 1080-B stack buf
       ├─ fwrite/fprintf -> stdout|stderr     ── ANSI console (TTY-gated)
       ├─ syslog(level, "%s", buf)            ── syslog sink
       ├─ ndl_printk (0xc4ca0)                ── kernel dmesg sink (ioctl 0x40104E71)
       ├─ print_to_error_log (0x224940)       ── process-wide error_log ring
       └─ TLS coalescing ring (__tls_get_addr)── per-thread batch (§3)
al_hal_log -> nlog_vwrite                     ── HAL bridge feeds the same core
```

### The level-gate ladder

`nlog_vwrite` gates in three stages. (1) The global ceiling: `if (nlog_cur_max_log_level < level || (level - 1) > 4) return;` — the second clause rejects `DISABLED (0)` and anything outside `1..5`, so only ERROR..TRACE ever emit. (2) The submodule lookup: walk `log_configs[]` for `module.id == *(u32*)submodule`, bail after 12. (3) The per-axis gate: compare the line's `level` against this submodule's `syslog_level` and `console_level` independently — a line at INFO with `syslog_level=ERROR, console_level=TRACE` goes to console only. The ERROR/WARN/INFO/DEBUG/TRACE arms each re-check `console_level` against the matching threshold (`console_level <= INFO` rejects a DEBUG line, etc.) before choosing the stream and color. `[HIGH]`

### The two writer halves

There are two distinct emit shapes inside `nlog_vwrite`, selected by `coaleced_msg` and the TLS enable byte (`+16392`):

- **Direct (non-coalesced)** — used for WARN/INFO/DEBUG/TRACE and for the ERROR path when coalescing is off. Format the header with `snprintf` into a 1080-byte stack `buf`, append the body with `vsnprintf`, then: if `syslog_level` permits, `syslog(level, "%s", buf)`; unconditionally `ndl_printk(buf, …)` and `print_to_error_log(buf, …)`; and if `console_level` permits, write the timestamp+header+body to `stdout`/`stderr` framed with the ANSI color (red `\x1B[91m` for ERROR, magenta `\x1B[95m` for WARN) when the stream is a TTY, terminated by `\x1B[0m` + `'\n'` + `fflush`.
- **Coalesced (TLS-batched)** — used for the ERROR/API path when `coaleced_msg && TLS[+16392]`. The line is built into `buf` (with an `api_level`-dependent header: the full timestamp header when `api_level`, else a compact `"%s:%s "` submodule:func prefix), an optional `" Resolution: %s"` suffix from `NLOG_ERR_RESOLUTIONS[cause]` is appended, then the whole line is **copied into the per-thread ring** (§3) rather than emitted. The ring is flushed to all sinks only when an `api_level` ERROR fires and `log_coalescing_enabled`.

### Algorithm — the emit core

```c
// Models nlog_vwrite @0x7a3e0 (condensed: the 5-arm level ladder collapsed to its shape).
function nlog_vwrite(submodule, func, level, coaleced_msg, api_level, fmt, argp):
    if level > nlog_cur_max_log_level || (level - 1) > 4:    // global ceiling; rejects 0 and >5
        return
    cfg = find log_configs[i] where module.id == *(u32*)submodule   // bail after 12
    if not found: return

    // gate per axis; if neither permits this level, return
    if cfg.syslog_level < level && cfg.console_level < level:
        return

    pid = getpid(); tid = syscall(186 /*gettid*/)
    gettimeofday(&tv); localtime_r(&tv.tv_sec, &cur_time)
    dup argp into console_argp + buf_argp                   // two independent va_list copies

    // ---- COALESCED API/ERROR PATH ----------------------------------------
    if coaleced_msg && TLS[+16392 /*enabled*/]:
        if api_level:
            n  = snprintf(buf, 1024, "%4d-%3s-%02d %02d:%02d:%02d.%06d", year, mon3, …, usec)
            n += snprintf(buf+n, …, " %5d:%-5d %5s %5s:%-40s", pid, tid, "ERROR", submodule, func)
            n += vsnprintf(buf+n, …, fmt, buf_argp)
            cause = TLS[+0x4000]
            if cause != -1:                                 // append resolution hint
                n += snprintf(buf+n, …, " Resolution: %s", NLOG_ERR_RESOLUTIONS[cause])
        else:
            n  = snprintf(buf, 1024, "%s:%s ", submodule, func)   // compact non-api prefix
            n += vsnprintf(buf+n, …, fmt, buf_argp)
        if TLS[+16396 /*pending count*/] != 0:
            *(u16*)(buf+n) = 0x0A09; n += 2                 // "\n\t" line joiner between batched lines
        // push DOWNWARD into the ring: write-ptr (+16388) counts down from 0x4000
        strncpy(TLS_base + TLS[+16388] - n, buf, n)
        TLS[+16388] -= n
        TLS[+16396] += 1                                    // one more pending line

        if api_level && log_coalescing_enabled && prev_count != -1 && TLS[+16392]:
            // FLUSH the whole accumulated batch to every sink, once
            line = TLS_base + TLS[+16388]; len = 0x4000 - TLS[+16388]
            if cfg.console_level:
                fwrite("\x1B[91m", stderr); fprintf(stderr, line)
                fwrite("\n\n", stderr);     fwrite("\x1B[0m", stderr); fflush(stderr)
            if cfg.syslog_level: syslog(1, "%s", TLS_base)
            ndl_printk(line, len, 0)
            print_to_error_log(line, len)
            TLS[+0x3FFF] = 0x3FFEFFFFFFFF00     // reset coalesce sentinel
            TLS[+16396] = 0; TLS[+16391] = 0    // clear pending count + flush word
        return

    // ---- DIRECT (non-coalesced) PATH -------------------------------------
    n  = snprintf(buf, 1024, " %5d:%-5d %5s %5s:%-40s", pid, tid, LEVELTAG[level], submodule, func)
    n += vsnprintf(buf+n, …, fmt, argp)
    if cfg.syslog_level >= level: syslog(level, "%s", buf)
    ndl_printk(buf, min(n,1023)+1, 0)
    print_to_error_log(buf, min(n,1023)+1)
    if cfg.console_level >= level:
        stream = (level == ERROR) ? stderr : stdout
        color  = (level == ERROR) ? "\x1B[91m" : (level == WARN) ? "\x1B[95m" : NULL
        if color && is_tty(stream): fputs(color, stream)
        fprintf(stream, "%4d-%3s-%02d %02d:%02d:%02d.%06d", year, mon3, …, usec)
        fprintf(stream, " %5d:%-5d %5s %5s:%-40s", pid, tid, LEVELTAG[level], submodule, func)
        vfprintf(stream, fmt, console_argp)
        if color: fwrite("\x1B[0m", stream)
        fputc('\n', stream); fflush(stream)
```

> **NOTE —** `tid` is read by raw `syscall(186)` (`gettid` on x86-64), not `pthread_self()`, so the header's `%-5d` tid is the kernel thread id that matches `dmesg`/`ndl_printk` and `/proc`. The header is built **twice** in the worst case (once into the stack `buf` for the byte sinks via `snprintf`, once streamed to the console via `fprintf`) — the console path does not reuse `buf`, it re-formats from a second `va_list` copy (`console_argp`), which is why `nlog_vwrite` duplicates `argp` into two `__va_list_tag` copies up front. `[HIGH]`

> **GOTCHA —** the stack buffer is 1080 bytes but every `snprintf`/`vsnprintf` is bounded to **1024** (`0x400`) and the byte-sink length is clamped to `1023+1`. A formatted line longer than ~1 KiB is silently truncated at the byte sinks (`ndl_printk`, `error_log` ring, syslog). The 56-byte slack (1080 − 1024) is headroom for the `" Resolution: %s"` suffix and the `0x0A09` joiner, not for the body. `[HIGH]`

### The error-log ring writer

`print_to_error_log @0x224940` is the writer behind sink (5): a flat byte ring `error_log.log` of `error_log.capacity` bytes, guarded by `error_log_mutex @0xc96960`. It appends `message_size − 1` bytes (dropping the NUL), replaces the terminator with `'\n'`, and advances `error_log.tail`; on wrap it splits the copy into two arcs (`capacity − tail`, then the remainder from offset 0) and bumps `error_log.num_wraps`. The ring is a *post-mortem tail* — `nlog_dump_error_log @0x224ef0` (called by `nrt_core_dump`) `fopen(path,"a")` + `flock`s it, dumps the wrapped arc (`tail..capacity`) then the head (`0..tail`), reports the wrap count, and resets. `error_log_t` is `{num_wraps@+0, tail@+4, capacity@+8, log@+16}` (24 B); `nlog_init @0x224a30` clears `num_wraps`/`tail` as one `_QWORD` and `malloc`s `num_vtpbs << 17` bytes (128 KiB per virtual TPB).

---

## 3. The Per-Thread Coalescing Ring

### Purpose

The coalescing ring is the mechanism that lets the runtime accumulate a *causal chain* of non-fatal diagnostics on one thread and emit them as a single block the instant a fatal API error occurs — so a crash dump shows the lead-up, not just the final line. It is **per-thread TLS**, seeded by `nlog_coalescing_init_thread @0x224ae0`, which every public API entry and every worker thread calls. There is no lock: the ring is thread-private, addressed off `__tls_get_addr()`.

### The TLS layout

The ring occupies a ~16 KiB TLS region; the bytes are filled **downward** from `+0x4000`, and a cluster of control words sits just past it. Offsets are read directly from the `nlog_coalescing_init_thread` seed and the `nlog_vwrite` coalesced path.

| TLS offset | Type | Role | Confidence |
|---|---|---|---|
| `+0x0000 .. +0x4000` | bytes | the coalescing buffer (16 KiB), written **downward** | HIGH |
| `+0x3FFF` (16383) | `u64` scratch | seeded to `0`; flush sentinel set to `0x3FFEFFFFFFFF00` after a flush | HIGH |
| `+0x4000` (16384) | `u32` | error-cause / resolution index; `-1` = UNSET (indexes `NLOG_ERR_RESOLUTIONS[]`) | HIGH |
| `+16388` (`0x4004`) | `u32` | write pointer / remaining space; counts **down** from `0x4000` | HIGH |
| `+16391` (`0x4007`) | `u16` | flush word; cleared to 0 on flush | MED |
| `+16392` (`0x4008`) | `u8` | coalescing-enabled flag (`= log_coalescing_enabled` at thread init) | HIGH |
| `+16396` (`0x400C`) | `u32` | pending coalesced-line count (`nlog_has_unflushed_logs` reads this) | HIGH |

The write pointer at `+16388` is the *remaining free space*, initialized to `0x4000`; each line is `strncpy`'d to `TLS_base + write_ptr − n` and the pointer decremented by `n`. Lines therefore stack from the high end downward, and at flush the live region is `[TLS_base + write_ptr, TLS_base + 0x4000)` of length `0x4000 − write_ptr`. Consecutive lines are joined by the 2-byte `0x0A09` literal (`"\n\t"` — newline then tab, a `0x09` continuation indent) inserted when the pending count is already non-zero. `[HIGH]`

### Algorithm — `nlog_coalescing_init_thread`

```c
// Models nlog_coalescing_init_thread @0x224ae0. Per-thread reset of the ring.
function nlog_coalescing_init_thread():
    base = __tls_get_addr()
    base[+16392] = log_coalescing_enabled    // adopt the process-wide enable into this thread
    base[+0x3FFF] = 0                          // clear the scratch/flush sentinel byte
    *(u32*)(base + 16396) = 0                  // pending line count -> 0
    *(u64*)(base + 0x4000) = 0x3FFEFFFFFFFF    // error-cause slot -> UNSET-ish seed (low 32b = -1)
```

> **QUIRK —** `nlog_coalescing_init_thread` does **not** reset the write pointer at `+16388` to `0x4000`; it resets the count, enable byte, scratch, and cause slot. The write pointer is reset only at flush time inside `nlog_vwrite` (the `TLS[+0x3FFF] = 0x3FFEFFFFFFFF00` / `TLS[+16396] = 0` epilogue). A reimplementer must seed `+16388 = 0x4000` at first use (or at flush) or the very first thread's downward writes underflow. The `0x3FFEFFFFFFFF` seed at `+0x4000` is a packed sentinel whose low 32 bits read as `0xFFFFFFFF` = the `UNSET (-1)` cause, so the cause slot reads "unset" immediately after init. `[HIGH]`

### `nlog_set_error_cause` and the resolution suffix

`nlog_set_error_cause @0x2250f0` writes the per-thread cause slot at `+0x4000` *only if it is still UNSET* (`-1`); a second set logs a WARN `"Error cause is already defined as %d"` and is ignored. The stored cause indexes `NLOG_ERR_RESOLUTIONS @0xc08b80`, whose entry is appended to the next coalesced API ERROR as `" Resolution: %s"`. The one observed cause is `NEFF_ARCH_INCOMPAT (0)`, set by `kelf::load` when a NEFF's arch does not match the device — so the chain "load failed → ERROR with a human-readable resolution hint" is produced without the load site knowing the message text. `nlog_has_unflushed_logs @0x2250d0` is a one-line read of the pending count at `+16396`; it is the dtor's signal that the thread accumulated lines that were never flushed (§4).

---

## 4. The `NlogErrorContextManager` RAII Guard

### Purpose

`NlogErrorContextManager` is a 32-byte stack object holding a single `std::string api` (the API name), constructed at the top of essentially every public `nrt_*` entry and destroyed on every exit path. Its job is the **fail-time safety net**: if the API body logged coalesced lines but never explicitly flushed them (i.e. the thread still has unflushed logs at scope exit), the destructor emits one `"Generic API Failure"` ERROR carrying the API name, so a silent-but-failed call still produces a diagnostic and a flush. It is the RAII complement to `nlog_coalescing_init_thread`: init at entry, flush-or-warn at exit.

### Why it dominates every API-wrapper decompile

The guard's *constructor* is the SSO (small-string-optimization) `std::string` build: for an API name short enough to live inline (≤15 bytes), libstdc++ stores the bytes in the object's own 16-byte local buffer and points `_M_dataplus._M_p` at `_M_local_buf`; for longer names it heap-allocates. Either way the ctor is a fixed prologue of "set `_M_p = _M_local_buf`, copy the literal, set `_M_string_length`" that GCC inlines into *every* wrapper. Because it appears identically at the head of all ~377 public API functions, the SSO build is the single most repeated instruction shape across the API surface — and the matching destructor epilogue (the `if (_M_p != _M_local_buf) operator delete(...)` SSO teardown) is equally ubiquitous. Recognizing this prologue/epilogue pair is what lets a reader visually strip the RAII frame from an API decompile and find the real body.

### `NlogErrorContextManager` (32 bytes)

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `api` | `+0x00` | `std::string` (32 B) | the API name; SSO — inline ≤15 B, heap otherwise | HIGH |

The 32 bytes are the full libstdc++ `std::string`: `_M_dataplus._M_p @+0x00` (pointer), `_M_string_length @+0x08`, and the 16-byte `_M_local_buf @+0x10` (the SSO inline buffer, which on heap strings holds `_M_allocated_capacity` in its first 8 bytes). The guard carries nothing else — its entire state is the name string. `[HIGH]`

### Algorithm — the guard lifecycle

```c
// Constructor (INLINED into every nrt_* wrapper; no standalone symbol).
// Models the SSO std::string build at the head of an API function.
function NlogErrorContextManager_ctor(this, api_name):
    this->api._M_dataplus._M_p = this->api._M_local_buf       // SSO: point at inline buffer
    len = strlen(api_name)
    if len <= 15:
        memcpy(this->api._M_local_buf, api_name, len+1)        // store inline
    else:
        buf = operator new(len+1); this->api._M_dataplus._M_p = buf
        this->api._M_allocated_capacity = len; memcpy(buf, api_name, len+1)
    this->api._M_string_length = len
    nlog_coalescing_init_thread()                              // (paired at entry; see §3)

// Destructor @0x8fc80 (standalone symbol _ZN23NlogErrorContextManagerD1Ev).
function NlogErrorContextManager_dtor(this):
    if nlog_has_unflushed_logs():                             // TLS[+16396] != 0
        // the API logged coalesced lines but never flushed -> emit + flush now
        nlog_write("NLOG", this->api._M_dataplus._M_p,        // submodule "NLOG"; func = API name
                   NRT_LOG_LEVEL_ERROR, /*coaleced*/1, /*api_level*/1,
                   "Generic API Failure")
    // SSO teardown: free only if heap-allocated
    if this->api._M_dataplus._M_p != this->api._M_local_buf:
        operator delete(this->api._M_dataplus._M_p, this->api._M_allocated_capacity + 1)
```

> **NOTE —** the dtor's `nlog_write` passes `submodule = (const char*)&stru_83CF07._M_parent + 3` in the decompile — an IDA pointer-display artifact for the short `"NLOG"` literal; the same `+3` adjustment appears in `nlog_init`'s `openlog()` ident. The `func` argument is the API name string the guard holds, so the failure line reads `NLOG  <api_name>: Generic API Failure`. `[HIGH]`

> **GOTCHA —** the guard fires `"Generic API Failure"` purely on `nlog_has_unflushed_logs()` — the *presence of unflushed coalesced lines at scope exit*, not on a checked error return. An API path that logs a coalesced WARN/INFO for an entirely successful call and exits without an explicit flush will trip the dtor into emitting a spurious ERROR. The runtime avoids this by flushing on its real error paths; a reimplementer reusing the coalescing ring must flush (or clear the pending count) on every success path that logged into it. `[HIGH]`

---

## 5. The Rust Logger Bridge

### Purpose

The Rust crate `neuron_rustime` has **no log writer of its own** — no `stdout`/`stderr` sink, no file. Instead `init_rust_logger @0x508860` installs a static `neuron_rustime::LOGGER` into the `log` crate as the global logger, and the `NeuronLogger` `log::Log` impl forwards every Rust `info!`/`warn!`/`error!` record straight into the C `nlog_write("RUST", …)` sink, gated by `nlog_get_log_level("RUST")`. This makes `nlog` the single terminal for both languages and unifies their level config: the same `NEURON_RT_LOG_LEVEL_RUST` knob that fills `log_configs`' `"RUST"` slot governs Rust diagnostics. `init_rust_logger` is called from `nlog_init` (← `nrt_init`), so the bridge is live before any Rust code runs.

### Entry Point

```text
nrt_init -> nlog_init (0x224a30)
  └─ init_rust_logger (0x508860)
       ├─ log::set_logger(neuron_rustime::LOGGER, &unk_BF9378)
       ├─ on Ok:  log::MAX_LOG_LEVEL_FILTER (0xcb0788) = 5  (Trace -> never pre-filter)
       └─ on Err: WARN "logger already set"

<any neuron_rustime fn>::{info!/warn!/error!/…}
  └─ <NeuronLogger as log::Log>::log (0x50a4a0)
       ├─ nlog_get_log_level("RUST", &lvl)            ── C gate
       ├─ alloc::fmt::format::format_inner -> CString  ── format the record args
       └─ nlog_write("RUST", target, level, 1, 0, "%s", msg)  ── C sink
```

### The level mapping

`log::Level {Error 1, Warn 2, Info 3, Debug 4, Trace 5}` coincides numerically with `nrt_log_level_t {ERROR 1 .. TRACE 5}`, so the bridge compares the record level directly against the C level — no translation table. `init_rust_logger` sets the `log` crate's static `MAX_LOG_LEVEL_FILTER @0xcb0788` to `5` (Trace) so the crate's compile-time fast-path filter never drops a record; **all** filtering is delegated to `nlog` per-call via `nlog_get_log_level("RUST")`. The actual drop test in `NeuronLogger::log` is `if ((lvl − 1) < 4 && record.level > lvl) return;` — i.e. when the configured level is a finite `ERROR..DEBUG`, drop records above it; a `TRACE` (5) or `DISABLED` (0) configuration bypasses this branch, and `DISABLED` is still suppressed by `nlog` itself downstream.

### Algorithm — the bridge

```c
// init_rust_logger @0x508860
function init_rust_logger():
    if log::set_logger(neuron_rustime::LOGGER, &unk_BF9378) == Err:   // already set
        log!(warn, "logger already set")                              // via the crate's own logger
    else:
        log::MAX_LOG_LEVEL_FILTER = 5                                 // Trace: never pre-filter

// <neuron_rustime::logging::NeuronLogger as log::Log>::log @0x50a4a0
function NeuronLogger_log(record):
    lvl = DISABLED
    nlog_get_log_level("RUST", &lvl)                                  // C gate, "RUST" slot
    if (lvl - 1) < 4 && record.level > lvl:                           // finite gate -> drop above it
        return
    msg = format!(record.args)                                       // alloc::fmt::format -> String
    cstr = CString::new(msg)                                          // panics "msg contained nul byte" on interior NUL
    nlog_write("RUST", record.target, record.level, /*coaleced*/1, /*api*/0, "%s", cstr.ptr)
    drop(cstr); drop(msg)                                             // __rust_dealloc both

// <… NeuronLogger …>::enabled @0x50a760
function NeuronLogger_enabled(metadata):                              // -> bool
    lvl = DISABLED
    nlog_get_log_level("RUST", &lvl)
    return (lvl - 1 >= 4) || (lvl >= metadata.level)                  // TRACE/unfiltered, or level permits

// <… NeuronLogger …>::flush @0x508820
function NeuronLogger_flush():
    return                                                            // no-op: nlog writes synchronously
```

> **NOTE —** `NeuronLogger::log` calls `nlog_write` with `coaleced_msg = 1, api_level = 0`, so Rust lines take the **compact** coalesced prefix (`"RUST:<target> "`) and join the per-thread ring (§3) like any non-API line — they are flushed together with the C lines on the next API-level ERROR, not emitted standalone. The record's `target` (the Rust module path, e.g. `neuron_rustime::sys_trace::capture`) becomes the header's `func` field. `[HIGH]`

> **QUIRK —** a panic inside any Rust code does **not** reach this bridge. `neuron_rustime` installs no custom panic hook, so a panic prints via `std::panicking::default_hook` to **`stderr` directly**, bypassing `nlog` entirely — it will not appear in syslog, `dmesg`, or the `error_log` ring. The bridge carries only `log!`-macro diagnostics; abort-on-unwind panic text is a separate, un-bridged stream (see [trace/rust-capture](../trace/rust-capture.md) for the FFI panic model). `[HIGH]`

---

## 6. The Two Non-`nlog` Log Producers

Two other producers in this band emit log-shaped output without going through `nlog_vwrite`'s console/syslog fan-out, and a reimplementer mapping the runtime's diagnostic surface must account for both:

- **`ntrace` host event ring** (`KaenaRuntime/tdrv/ntrace.c`) — a mutex-guarded circular array of `ntrace_data_t` (48 B/slot) behind one file-scope `neuron_trace_ctx trace_ctx` (80 B). It is *not* a text logger: `ntrace_record_event @0x300710` appends a typed event (`trace_type`, `clock_gettime` timestamp, `nd/nc` index, a `strdup`'d hint) to the ring, driven by `nrt_trace_start`/`nrt_trace_stop`. It records exec/tensor/dmem milestones for offline replay, with 11 instrumentation callers. It shares the "ring buffer" shape with `nlog`'s `error_log` but is a structured-event store, not a byte tail.
- **`pool_stdio` GPSIMD capture** (`KaenaRuntime/tdrv/pool_stdio.c`) — drains the on-device GPSIMD/Pool engine's `stdout`/`stderr` (HBM-resident `SUNDA_UCODE_FILE_IO` queues) to the host. `pool_stdio_queue_consume_all_entries @0x3011d0` copies 240-byte device entries out via `dmem_buf_copyout`, prefixes `"Printing stdout/stderr from GPSIMD:\n"`, and emits the block via `nlog_write` — so device-side print output *does* land in `nlog`, but only after a device→host drain, not as a direct log call.

These are summarized here only to delimit the band; their full derivations (the trace ring, the GPSIMD queue wire format) belong to their own pages.

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `nlog_vwrite` | `0x7a3e0` | core emitter (2878 B): gate → header → 5-sink fan-out + coalescing | HIGH |
| `nlog_write` | `0x224d40` | variadic shim: `va_start` → `nlog_vwrite` | HIGH |
| `nlog_init` | `0x224a30` | `openlog` + `init_rust_logger` + alloc `error_log` ring (`num_vtpbs<<17`) | HIGH |
| `nlog_close` | `0x224aa0` | `closelog` + free `error_log.log` under mutex | HIGH |
| `nlog_constructor` | `0x77b60` | library ctor: cache `isatty(stdout/stderr)` → tty flags | HIGH |
| `nlog_set_log_level` | `0x224b30` | parse level + location names; write both axes into `log_configs[]` | HIGH |
| `nlog_get_log_level` | `0x224cd0` | effective level per submodule (syslog-preferred); Rust bridge gate | HIGH |
| `nlog_coalescing_init_thread` | `0x224ae0` | per-thread TLS seed: enable byte, count, cause slot | HIGH |
| `nlog_has_unflushed_logs` | `0x2250d0` | read TLS pending count `+16396`; dtor's flush signal | HIGH |
| `nlog_set_error_cause` | `0x2250f0` | write TLS cause slot if UNSET; indexes `NLOG_ERR_RESOLUTIONS` | HIGH |
| `nlog_printk_hardware_error` | `0x224dd0` | ISO-8601 `NEURON_HW_ERR=` line → `ndl_printk` (HW-error dmesg) | HIGH |
| `nlog_clear_error_log` | `0x224ec0` | reset `error_log.{num_wraps,tail}` under mutex (no free) | HIGH |
| `nlog_dump_error_log` | `0x224ef0` | `fopen`+`flock` dump of wrapped ring; called by `nrt_core_dump` | HIGH |
| `print_to_error_log` | `0x224940` | error-log ring writer: two-arc wrap copy, `num_wraps` accounting | HIGH |
| `ndl_printk` | `0xc4ca0` | kernel dmesg sink: `ioctl 0x40104E71` into `/dev/neuron<N>` | HIGH |
| `init_rust_logger` | `0x508860` | `log::set_logger(LOGGER)` + `MAX_LOG_LEVEL_FILTER = 5` | HIGH |
| `<NeuronLogger as log::Log>::log` | `0x50a4a0` | format Rust record → `nlog_write("RUST", …)`, gated | HIGH |
| `<NeuronLogger as log::Log>::enabled` | `0x50a760` | gate test against `nlog_get_log_level("RUST")` | HIGH |
| `<NeuronLogger as log::Log>::flush` | `0x508820` | no-op (nlog is synchronous) | HIGH |
| `NlogErrorContextManager::~` | `0x8fc80` | RAII dtor: flush `"Generic API Failure"` if unflushed; SSO teardown | HIGH |

> **NOTE — gaps.** The anon `module` enum (`nrt_log_config_t+0`) is sized 12 by the array bound and the 12-iteration loop, but the member *strings* are not enumerable from the anon-hash enum name, so the exact submodule id set is `[LOW]` — only `"RUST"`, `"NLOG"` and the `"all"` wildcard are directly observed. The `openlog()` syslog ident in `nlog_init` is passed as `(const char*)&stru_83CF07._M_parent + 3`, an IDA pointer-display artifact over a short literal that is not cleanly isolated; treat the ident value as unverified (`[MED]`). `NLOG_ERR_RESOLUTIONS @0xc08b80` has one observed cause (`NEFF_ARCH_INCOMPAT`); the full resolution table was not enumerated. The TLS flush word at `+16391` (`u16`, cleared on flush) is `[MED]` — its read side was not located.

## Cross-References

- [Interned String Database](interned-strings.md) — the *other* C↔Rust boundary in this runtime; same cbindgen-shim discipline and the `log!`-into-`nlog` sink shared by the interner's diagnostics
- [neuron_rustime: sys_trace Capture Engine](../trace/rust-capture.md) — the Rust producer whose `log!` diagnostics flow through this bridge, and the FFI abort-on-unwind panic model that bypasses `nlog`
- [Error and Status Codes (NRT_STATUS)](error-codes.md) — the status enum the API wrappers (bracketed by `NlogErrorContextManager`) return, and the error layers that feed `nlog_printk_hardware_error`
- [Userspace Runtime Core — Internal Architecture Map](overview.md) — where `nlog` sits as the cross-cutting diagnostic infrastructure every layer leans on
- [back to index](../index.md)
