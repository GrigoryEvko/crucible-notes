# Timing & Exit

The timing and exit subsystem lives in `host_envir.c` and handles three responsibilities: measuring CPU and wall-clock time for compilation phases, formatting the compilation summary (error/warning counts), and mapping internal status codes to process exit codes. All functions write to `qword_126EDF0` (the diagnostic output stream, initialized to `stderr` in `main()`).

## Key Facts

| Property | Value |
|---|---|
| Source file | `host_envir.c` (EDG 6.6) |
| Timing functions | `sub_5AF350` (capture_time), `sub_5AF390` (report_timing) |
| Exit function | `sub_5AF1D0` (exit_with_status), 145 bytes, `__noreturn` |
| Signoff function | `sub_5AEE00` (write_signoff), `sub_589530` (write_signoff + free_mem_blocks) |
| Timing enable flag | `dword_106C0A4` at `0x106C0A4`, set by CLI flag `--timing` (case 20) |
| Diagnostic stream | `qword_126EDF0` at `0x126EDF0` (stderr) |
| SARIF mode flag | `dword_106BBB8` at `0x106BBB8` |

## Timing Infrastructure

### capture_time -- `sub_5AF350` (0x5AF350)

A 48-byte function that samples both CPU time and wall-clock time into a 16-byte timestamp structure.

```c
// Annotated decompilation
void capture_time(timestamp_t *out)    // sub_5AF350
{
    out->cpu_ms  = (int)((double)(int)clock() * 1000.0 / 1e6);  // [0]: CPU milliseconds
    out->wall_s  = time(NULL);                                    // [1]: wall-clock seconds
}
```

**Timestamp structure layout** (16 bytes, two 64-bit fields):

| Offset | Size | Type | Content |
|--------|------|------|---------|
| +0 | 8 | `int64_t` | CPU time in milliseconds: `clock() * 1000 / CLOCKS_PER_SEC` |
| +8 | 8 | `time_t` | Wall-clock time via `time(0)` (epoch seconds) |

The CPU time computation `clock() * 1000.0 / 1000000.0` normalizes the `clock()` return value (microseconds on Linux where `CLOCKS_PER_SEC` = 1000000) to milliseconds, then truncates to integer. This means CPU time resolution is 1 ms.

### report_timing -- `sub_5AF390` (0x5AF390)

Computes deltas between two timestamps and prints a formatted timing line.

```c
// Annotated decompilation
void report_timing(const char *label,       // sub_5AF390
                   timestamp_t *start,
                   timestamp_t *end)
{
    double elapsed = difftime(end->wall_s, start->wall_s);    // wall seconds
    double cpu_sec = (double)(end->cpu_ms - start->cpu_ms) / 1000.0;  // CPU seconds

    fprintf(qword_126EDF0,
            "%-30s %10.2f (CPU) %10.2f (elapsed)\n",
            label, cpu_sec, elapsed);
}
```

The decompiled code contains explicit unsigned-to-double conversion handling for 64-bit values (the `v6 & 1 | (v6 >> 1)` pattern followed by doubling). This is the compiler's standard idiom for converting unsigned 64-bit integers to `double` on x86-64 when the value might exceed `INT64_MAX`. In practice, `clock()` millisecond values fit comfortably in signed 64-bit range, so this path is never taken.

**Output format:** `"%-30s %10.2f (CPU) %10.2f (elapsed)\n"`

```
Front end time                     12.34 (CPU)     15.67 (elapsed)
Back end time                       3.45 (CPU)      4.56 (elapsed)
Total compilation time             15.79 (CPU)     20.23 (elapsed)
```

The label is left-justified in a 30-character field. CPU and elapsed times are right-justified in 10-character fields with 2 decimal places.

### Timing Flag Activation

The timing flag `dword_106C0A4` is registered in the CLI flag table as flag ID 20:

```c
// In sub_452010 (register_internal_flags)
sub_451F80(20, "timing", 35, 0, 0, 1);
//         ^id  ^name    ^case ^  ^ ^undocumented
```

When `--timing` is passed on the command line, the CLI parser (`sub_459630`) hits case 20 in its switch statement, which sets `dword_106C0A4 = 1`. The flag defaults to 0 (disabled), set explicitly in `sub_45EB40` (cmd_line_pre_init).

## Timing Brackets in main()

`main()` at `0x408950` allocates six 16-byte timestamp slots on its stack frame:

| Variable | Stack offset | Purpose |
|----------|-------------|---------|
| `v7` | `[rsp+0x00]` | Total compilation start |
| `v8` | `[rsp+0x10]` | Frontend start |
| `v9` | `[rsp+0x20]` | Frontend end |
| `v10` | `[rsp+0x30]` | Backend start |
| `v11` | `[rsp+0x40]` | Backend end |
| `v12` | `[rsp+0x50]` | Total compilation end |

Three timing regions are measured:

### Region 1: Frontend

Captured after `sub_585DB0` (fe_one_time_init) and reported after `sub_588F90` (fe_wrapup). Covers stages 3-6 of the pipeline: heavy initialization, TU state reset, source parsing + IL build, and the 5-pass wrapup.

```c
if (dword_106C0A4)
    capture_time(&t_fe_start);      // v8

reset_tu_state();                   // sub_7A4860
process_translation_unit(filename); // sub_7A40A0
fe_wrapup(filename, 1);            // sub_588F90

if (dword_106C0A4) {
    capture_time(&t_fe_end);        // v9
    report_timing("Front end time", &t_fe_start, &t_fe_end);
}
```

### Region 2: Backend

Captured around `sub_489000` (process_file_scope_entities). Only executed when `dword_106C254 == 0` (no frontend errors).

```c
if (!dword_106C254) {
    if (dword_106C0A4)
        capture_time(&t_be_start);  // v10

    process_file_scope_entities();  // sub_489000

    if (dword_106C0A4) {
        capture_time(&t_be_end);    // v11
        report_timing("Back end time", &t_be_start, &t_be_end);
    }
}
```

### Region 3: Total

Starts before CLI parsing (`sub_459630`) and ends just before exit. Always uses `v7` (captured once at the very beginning) as the start timestamp.

```c
capture_time(&t_total_start);       // v7 — captured before CLI parsing

// ... entire compilation ...

if (dword_106C0A4) {
    capture_time(&t_total_end);     // v12
    report_timing("Total compilation time", &t_total_start, &t_total_end);
}
```

Note that the "Total compilation time" region begins before command-line parsing and includes the CLI parsing overhead, all initialization, frontend, backend, and signoff. The "Front end time" region does NOT include CLI parsing or pre-init -- it starts after `fe_one_time_init`.

## Compilation Summary -- write_signoff

### sub_5AEE00 (0x5AEE00) -- write_signoff

This 490-byte function writes the compilation summary trailer to the diagnostic stream. It has two completely separate code paths: SARIF mode and text mode.

#### SARIF Mode (`dword_106BBB8 == 1`)

Closes the SARIF JSON document started by `sub_5AEDB0` (write_init):

```c
fwrite("]}]}\n", 1, 5, qword_126EDF0);
```

This closes the `results` array, the `run` object, the `runs` array, and the top-level SARIF document. If `dword_106BBB8` is set but not equal to 1, the function hits an assertion: `write_signoff` at `host_envir.c:2203`.

#### Text Mode (`dword_106BBB8 == 0`)

The text-mode path assembles a human-readable summary from four counters:

| Global | Address | Meaning |
|--------|---------|---------|
| `qword_126ED90` | `0x126ED90` | Error count |
| `qword_126ED98` | `0x126ED98` | Warning count |
| `qword_126EDB0` | `0x126EDB0` | Suppressed error count |
| `qword_126EDB8` | `0x126EDB8` | Suppressed warning count |

The function uses EDG's message catalog (`sub_4F2D60`) for all translatable strings:

| Message ID | Purpose | Likely content |
|------------|---------|----------------|
| 1742 | Error (singular) | `"error"` |
| 1743 | Errors (plural) | `"errors"` |
| 1744 | Warning (singular) | `"warning"` |
| 1745 | Warning (plural) | `"warnings"` |
| 1746 | Conjunction | `"and"` |
| 1747 | Source file indicator (format) | `"in compilation of \"%s\""` |
| 1748 | Generated indicator | `"generated"` |
| 3234 | Suppressed intro | `"of which"` |
| 3235 | Suppressed verb | `"were suppressed"` / `"was suppressed"` |

**Output assembly logic** (simplified pseudocode):

```c
void write_text_signoff(void)     // text-mode path of sub_5AEE00
{
    int64_t errors   = qword_126ED90;
    int64_t warnings = qword_126ED98;
    int64_t total    = errors + warnings;

    // Debug: module declaration count (only if dword_126EFC8 + "module_report")
    if (dword_126EFC8 && is_debug_enabled("module_report") && qword_106B9C8)
        fprintf(s, "%lu modules declarations processed (%lu failed).\n",
                qword_106B9C8, qword_106B9C0);

    if (total == 0)
        return;                   // nothing to report

    int64_t suppressed_warn = qword_126EDB8;
    int64_t suppressed_total = suppressed_warn + qword_126EDB0;
    int     displayed = total - suppressed_total;

    // --- Print displayed counts ---
    if (displayed != suppressed_total) {  // there ARE unsuppressed diagnostics
        if (errors)
            fprintf(stream, "%lu %s", errors, msg(errors != 1 ? 1743 : 1742));
        if (errors && warnings)
            fprintf(stream, " %s ", msg(1746));   // " and "
        if (warnings)
            fprintf(stream, "%lu %s", warnings, msg(warnings != 1 ? 1745 : 1744));
    }

    // --- Print suppressed counts ---
    if (suppressed_total > 0) {
        // Assertion: suppressed_warn must be 0 if we reach here
        // (i.e., only suppressed errors, not suppressed warnings, trigger assert)
        if (suppressed_warn)
            assert(0);  // host_envir.c:2141, "write_text_signoff"

        if (displayed) {
            fprintf(stream, " (%s ", msg(3234));        // " (of which "
            fprintf(stream, "%lu %s %s",
                    suppressed_total,
                    msg(3235),                          // "was/were suppressed"
                    msg(suppressed_total == 1 ? 1742 : 1743));
            fputc(')', stream);                         // close paren
        } else {
            // All diagnostics were suppressed -- just print suppressed count
            fprintf(stream, "%lu %s %s",
                    suppressed_total,
                    msg(3235),
                    msg(suppressed_total == 1 ? 1742 : 1743));
        }
    }

    // --- Print source filename ---
    fputc(' ', stream);
    if (qword_126EEE0 && *qword_126EEE0 && strcmp(qword_126EEE0, "-") != 0) {
        char *display_name = qword_106C040 ? qword_106C040 : qword_126EEE0;
        char *basename = normalize_path(display_name) + 32;  // sub_5AC020 returns
                                                              // buffer, basename at +32
        fprintf(stream, msg(1747), basename);  // "in compilation of \"%s\""
    } else {
        fputs(msg(1748), stream);              // "generated" (stdout mode)
    }
    fputc('\n', stream);
}
```

**Example output:**

```
2 errors and 1 warning in compilation of "kernel.cu"
```

```
3 errors (of which 1 was suppressed error) in compilation of "main.cu"
```

### sub_589530 (0x589530) -- write_signoff + free_mem_blocks

A thin wrapper (13 bytes) called from `main()`'s exit path. Performs two operations:

```c
void fe_finish(void)          // sub_589530
{
    write_signoff();          // sub_5AEE00 — print summary
    free_mem_blocks();        // sub_6B8DE0 — release all frontend memory pools
}
```

`sub_6B8DE0` (`free_mem_blocks`) is the master memory deallocation function from `mem_manage.c` (assertion at line 1438, function name `"free_mem_blocks"`). It operates in two modes depending on the global `dword_1280728`:

**Pool allocator mode** (`dword_1280728` set): Walks three linked lists of allocated memory blocks:

1. **Current block** at `qword_1280720`: freed first, looked up in the free-block hash chain at `qword_1280748`, then the block descriptor itself is freed.
2. **Hash table** at `qword_126EC88` with `dword_126EC80` buckets: each bucket is a singly-linked list of block descriptors. Blocks with nonzero size (field `[4]`) are freed; blocks with zero size trigger the `mem_manage.c:1438` assertion (invariant: a complete block must have a recorded size).
3. **Overflow list** at `qword_1280730`: same walk-and-free logic.

Each block deallocation decrements `qword_1280718` (total allocated bytes) and optionally updates `qword_1280710` (low-water mark). At debug level > 4, each free prints: `"free_complete_block: freeing block of size %lu\n"`.

**Non-pool mode** (`dword_1280728` == 0): Iterates source file entries via `sub_6B8B20(N)` for each entry `N` from `dword_126EC80` down to 0, then walks the permanent allocation array at `qword_126EC58`, calling `sub_5B0500` for each (which wraps `munmap` or `free`).

## Exit Handling

### exit_with_status -- `sub_5AF1D0` (0x5AF1D0)

A 145-byte `__noreturn` function that maps internal compilation status codes to POSIX exit codes. This is the only exit point for normal compilation flow -- every path through `main()` ends here.

```c
// Full annotated decompilation
__noreturn void exit_with_status(uint8_t status)  // sub_5AF1D0
{
    // --- Text-mode messages (suppressed in SARIF mode) ---
    if (!dword_106BBB8) {           // not SARIF mode
        if (status == 9 || status == 10) {
            fwrite("Compilation terminated.\n", 1, 0x18, qword_126EDF0);
            exit(4);                // goto LABEL_8
        }
        if (status == 11) {
            fwrite("Compilation aborted.\n", 1, 0x15, qword_126EDF0);
            fflush(qword_126EDF0);
            abort();                // goto LABEL_10
        }
    }

    // --- Exit code mapping (both text and SARIF modes) ---
    switch (status) {
        case 3:
        case 4:
        case 5:  exit(0);          // success
        case 8:  exit(2);          // warnings only
        case 9:
        case 10: exit(4);          // errors (SARIF mode reaches here)
        default: fflush(qword_126EDF0);
                 abort();          // internal error (11, or any unknown)
    }
}
```

**Status-to-exit-code mapping:**

| Internal Status | Meaning | Text Output | Exit Code | Termination |
|----------------|---------|-------------|-----------|-------------|
| 3 | Clean success (no warnings, no additional status) | *(none)* | 0 | `exit(0)` |
| 4 | Success variant | *(none)* | 0 | `exit(0)` |
| 5 | Success with additional status (`qword_126ED88 != 0`) | *(none)* | 0 | `exit(0)` |
| 8 | Warnings present (`qword_126ED90 != 0`) | *(none)* | 2 | `exit(2)` |
| 9 | Errors | `"Compilation terminated.\n"` | 4 | `exit(4)` |
| 10 | Errors (variant) | `"Compilation terminated.\n"` | 4 | `exit(4)` |
| 11 | Internal error / fatal | `"Compilation aborted.\n"` | *(n/a)* | `abort()` |

In SARIF mode (`dword_106BBB8 != 0`), the text messages `"Compilation terminated."` and `"Compilation aborted."` are suppressed. The exit codes remain the same -- the function falls through to the `switch` which dispatches identically.

The `default` case handles status 11 and any unexpected status value by calling `abort()` after flushing the diagnostic stream. This generates a core dump for debugging.

### Control flow note

The code structure looks unusual because the decompiler linearizes a two-phase dispatch. First, text-mode messages are emitted for statuses 9/10 and 11 (with early `exit(4)` or `abort()` respectively). If SARIF mode is active OR status is not 9/10/11, execution falls through to the `switch` statement. This means statuses 9/10 reach `exit(4)` via two different paths depending on SARIF mode, but the exit code is always 4.

## Exit Code Determination in main()

The exit code passed to `sub_5AF1D0` is computed in `main()` based on two global counters:

```c
// From main() at 0x408950
uint8_t exit_code = 8;              // default: warnings (errors present → v6=8)

sub_6B8B20(0);                      // reset file state
sub_589530();                       // write_signoff + free_mem_blocks

if (!qword_126ED90)                 // no errors?
    exit_code = qword_126ED88 ? 5 : 3;   // success codes

// ... timing, stack restore ...
exit_with_status(exit_code);
```

**Decision tree:**

```
qword_126ED90 != 0  (errors present)
  └── exit_code = 8  →  exit(2)   "warnings only" path
      NOTE: This is counterintuitive. When errors exist, the exit
      code defaults to 8 (which maps to exit(2), not exit(4)).
      However, this path is only reachable when qword_126ED90 was
      nonzero at the error gate (dword_106C254 = 1, skip backend),
      but became zero by the time we reach the exit code check.
      In practice, errors set qword_126ED90 and it stays nonzero.

qword_126ED90 == 0  (no errors)
  ├── qword_126ED88 != 0  →  exit_code = 5  →  exit(0)  (success w/ status)
  └── qword_126ED88 == 0  →  exit_code = 3  →  exit(0)  (clean success)
```

The variable `qword_126ED88` at `0x126ED88` is initialized to 0 in `sub_4ED530` (declaration_pre_init) and `sub_4ED7C0`. It appears to track whether any notable conditions occurred during compilation that are not errors or warnings -- possibly informational remarks or specific compiler actions taken. When nonzero, the exit code changes from 3 to 5, but both map to `exit(0)`.

## Stack Limit Restoration

Before calling `exit_with_status`, `main()` restores the process stack limit if it was raised during initialization:

```c
if (stack_was_raised) {
    rlimits.rlim_cur = original_stack;   // restore saved soft limit
    setrlimit(RLIMIT_STACK, &rlimits);
}
```

The boolean `stack_was_raised` (stored in `rbp`, variable `v4`) is set during startup when `dword_106C064` (the `--modify_stack_limit` flag, default ON) causes `main()` to raise `RLIMIT_STACK` from its soft limit to the hard limit. This restoration is a defensive measure -- it ensures any child processes spawned during cleanup (or signal handlers) inherit a normal stack size.

## Signal-Driven Exit Paths

Three additional paths reach `exit_with_status`:

### SIGINT / SIGTERM Handler -- `handler` (0x5AF2C0)

Registered in `sub_5B1E70` (host_envir_early_init) for signals 2 (SIGINT) and 15 (SIGTERM). The registration is one-shot, guarded by `dword_E6E120` (set to 0 after first call). SIGINT registration is conditional: the code first calls `signal(SIGINT, SIG_IGN)` and checks the return value. If the previous handler was already `SIG_IGN` (meaning the parent process -- typically nvcc -- has set the child to ignore interrupts), it stays ignored. Otherwise, the custom handler is installed. SIGTERM always gets the handler unconditionally.

```c
__noreturn void handler(void)           // 0x5AF2C0
{
    fputc('\n', qword_126EDF0);         // newline to stderr
    terminate_compilation(9);           // sub_5AF2B0
}
```

### terminate_compilation -- `sub_5AF2B0` (0x5AF2B0)

Bridge function: writes signoff then exits.

```c
__noreturn void terminate_compilation(uint8_t status)  // sub_5AF2B0
{
    write_signoff();                    // sub_5AEE00
    exit_with_status(status);           // sub_5AF1D0
}
```

When called from `handler`, status is 9 (errors), which produces `"Compilation terminated.\n"` followed by `exit(4)`.

### SIGXCPU Handler -- `sub_5AF270` (0x5AF270)

Registered for signal 24 (SIGXCPU):

```c
__noreturn void cpu_time_limit_handler(void)  // sub_5AF270
{
    fputc('\n', qword_126EDF0);
    fwrite("Internal error: CPU time limit exceeded.\n", 1, 0x29, qword_126EDF0);
    exit_with_status(11);               // sub_5AF1D0 → abort()
}
```

This handler fires if the process receives SIGXCPU despite `sub_5B1E70` having set `RLIMIT_CPU` to `RLIM_INFINITY` at startup. A SIGXCPU could still arrive if an external resource manager (e.g., batch scheduler) overrides the limit after initialization. Status 11 causes `abort()` with a core dump.

### SIGXFSZ

Set to `SIG_IGN` in `sub_5B1E70` (`signal(25, SIG_IGN)`). This prevents the process from being killed when writing a `.int.c` file that exceeds the filesystem's file-size limit. Without this, large compilation outputs could trigger an unhandled SIGXFSZ (25) and terminate with a core dump.

## SARIF Output Bookends

The SARIF JSON output is bracketed by two functions:

| Function | Address | When Called | Output |
|----------|---------|-------------|--------|
| `sub_5AEDB0` (write_init) | `0x5AEDB0` | During `fe_init_part_1` (stage 3) | `{"version":"2.1.0","$schema":"...","runs":[{"tool":{"driver":{"name":"EDG CPFE","version":"6.6",...}},"columnKind":"unicodeCodePoints","results":[` |
| `sub_5AEE00` (write_signoff) | `0x5AEE00` | During `sub_589530` (pre-exit) | `]}]}` + newline |

The tool metadata identifies the frontend as `"EDG CPFE"` version `"6.6"` from `"Edison Design Group"`, with `fullName` `"Edison Design Group C/C++ Front End - 6.6"` and `informationUri` `"https://edg.com/c"`. The column kind is `"unicodeCodePoints"` (not byte offsets). Individual diagnostics are appended to the `results` array by the error subsystem between these two calls.

The `write_init` function (`sub_5AEDB0`) has the same assertion guard as `write_signoff`: if `dword_106BBB8` is set but not equal to 1, it triggers an assertion at `host_envir.c:2017` (`"write_init"`). Both assertions enforce the invariant that SARIF mode is exactly 0 or 1, never any other value.

## Profiling Init -- `sub_5AF330` (0x5AF330)

A separate but related mechanism. During `sub_585DB0` (`fe_one_time_init`), if `dword_106BD4C` is set, `sub_5AF330` is called:

```c
int profiling_init(void)             // sub_5AF330
{
    int was_initialized = dword_126F110;
    if (!dword_126F110)
        dword_126F110 = 1;           // mark as initialized
    return was_initialized;          // 0 on first call, 1 on subsequent
}
```

This is a one-shot initializer for a profiling subsystem distinct from the `--timing` flag. The `dword_106BD4C` gate is set by a different CLI flag and controls a more granular, per-function profiling infrastructure (used by the EDG debug trace system, not the phase-level timing brackets). The `dword_126F110` flag prevents double-initialization if `fe_one_time_init` is called more than once.

## Signal Handler Registration Detail

The full signal setup in `sub_5B1E70` (`host_envir_early_init`):

```c
if (dword_E6E120) {                              // one-shot guard (starts nonzero)
    if (signal(SIGINT, SIG_IGN) != SIG_IGN)       // was SIGINT not already ignored?
        signal(SIGINT, handler);                   //   install interrupt handler
    signal(SIGTERM, handler);                      // always install
    signal(SIGXFSZ, SIG_IGN);                     // ignore file-size limit signals
    signal(SIGXCPU, sub_5AF270);                  // CPU time limit → abort
    dword_E6E120 = 0;                             // prevent re-registration
}
```

| Signal | Number | Handler | Behavior |
|--------|--------|---------|----------|
| SIGINT | 2 | `handler` (0x5AF2C0) | Conditional: only if not inherited as `SIG_IGN`. Writes newline, calls `terminate_compilation(9)`. |
| SIGTERM | 15 | `handler` (0x5AF2C0) | Always installed. Same handler as SIGINT. |
| SIGXFSZ | 25 | `SIG_IGN` | Ignored. Prevents crash on large `.int.c` output. |
| SIGXCPU | 24 | `sub_5AF270` (0x5AF270) | Prints `"Internal error: CPU time limit exceeded.\n"`, then `exit_with_status(11)` (abort). |

After signal setup, `sub_5B1E70` also disables the CPU time limit by setting `RLIMIT_CPU` soft limit to `RLIM_INFINITY`:

```c
getrlimit(RLIMIT_CPU, &rlimits);
rlimits.rlim_cur = RLIM_INFINITY;    // -1 = unlimited
setrlimit(RLIMIT_CPU, &rlimits);
```

This prevents normal compilations from hitting SIGXCPU. The handler at `sub_5AF270` is a safety net for cases where an external resource manager re-imposes the limit after initialization.

## Complete Exit Sequence

The full sequence from compilation completion to process termination:

```
1.  sub_6B8B20(0)           Reset source file manager state
2.  sub_589530()            Write signoff + free memory
    ├── sub_5AEE00()        Print error/warning summary (or close SARIF JSON)
    └── sub_6B8DE0()        Free all frontend memory pools
3.  Compute exit_code       Based on qword_126ED90, qword_126ED88
4.  [If timing enabled]
    ├── sub_5AF350(v12)     Capture total end timestamp
    └── sub_5AF390(...)     Print "Total compilation time"
5.  [If stack was raised]
    └── setrlimit(...)      Restore original stack soft limit
6.  sub_5AF1D0(exit_code)   Map status → exit code, terminate
    ├── 3,4,5 → exit(0)
    ├── 8     → exit(2)
    ├── 9,10  → exit(4) + "Compilation terminated."
    └── 11    → abort()  + "Compilation aborted."
```

## Global Variable Reference

| Variable | Address | Size | Role |
|----------|---------|------|------|
| `dword_106C0A4` | `0x106C0A4` | 4 | Timing enable flag. CLI flag 20 (`--timing`). |
| `dword_106BBB8` | `0x106BBB8` | 4 | SARIF output mode. 0=text, 1=SARIF JSON. |
| `qword_126EDF0` | `0x126EDF0` | 8 | Diagnostic output FILE* (stderr). |
| `qword_126ED90` | `0x126ED90` | 8 | Total error count. |
| `qword_126ED98` | `0x126ED98` | 8 | Total warning count. |
| `qword_126ED88` | `0x126ED88` | 8 | Additional status (nonzero changes exit code from 3 to 5). |
| `qword_126EDB0` | `0x126EDB0` | 8 | Suppressed error count. |
| `qword_126EDB8` | `0x126EDB8` | 8 | Suppressed warning count. |
| `qword_126EEE0` | `0x126EEE0` | 8 | Output filename (for source display in signoff). |
| `qword_106C040` | `0x106C040` | 8 | Display filename override (used if set, else falls back to `qword_126EEE0`). |
| `dword_106C254` | `0x106C254` | 4 | Skip-backend flag. Set to 1 when errors detected after frontend. |
| `dword_106C064` | `0x106C064` | 4 | Stack limit adjustment flag (`--modify_stack_limit`, default ON). |
| `dword_E6E120` | `0xE6E120` | 4 | One-shot guard for signal handler registration in `sub_5B1E70`. |
| `dword_126F110` | `0x126F110` | 4 | Profiling initialized flag. Set to 1 by `sub_5AF330`. |
| `dword_106BD4C` | `0x106BD4C` | 4 | Profiling gate flag. When set, `fe_one_time_init` calls `sub_5AF330`. |
| `qword_106B9C8` | `0x106B9C8` | 8 | Module declarations processed count (for debug `module_report`). |
| `qword_106B9C0` | `0x106B9C0` | 8 | Module declarations failed count. |
| `dword_1280728` | `0x1280728` | 4 | Memory manager mode flag. Controls pool vs non-pool deallocation in `sub_6B8DE0`. |

## Cross-References

- [Pipeline Overview](./overview.md) -- placement of timing/exit in the 8-stage pipeline
- [Entry Point & Initialization](./entry.md) -- `main()` structure, signal handler registration, stack limit setup
- [CLI Processing](./cli.md) -- flag 20 (`--timing`) registration and parsing
- [Backend Code Generation](./backend.md) -- the "Back end time" measurement target
- [SARIF & Pragma Control](../diagnostics/sarif-pragmas.md) -- SARIF JSON format details
- [Diagnostic Overview](../diagnostics/overview.md) -- error/warning counting infrastructure
