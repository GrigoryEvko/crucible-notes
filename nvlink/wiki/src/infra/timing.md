# Timing Infrastructure

nvlink embeds a lightweight phase-timer system that measures wall-clock elapsed time for each major pipeline stage. The system has two independent output paths: a human-readable stderr trace controlled by the `--verbose` / `-edbg` flag (bit 5, mask `0x20`), and a machine-readable CSV log controlled by the `-time <file>` CLI option. Both paths share the same `gettimeofday`-based stopwatch primitives but serve different audiences -- stderr output helps developers identify slow phases during interactive builds, while the CSV path feeds automated performance-regression infrastructure at NVIDIA.

The timer is deliberately simple: a single global `struct timeval` at `0x2A5F1B0`, a boolean "started" flag at `byte_2A5F1C0`, and three small functions totaling under 200 bytes of code. There is no per-thread timing, no nested/hierarchical timers, and no high-resolution clock source -- `gettimeofday` provides microsecond resolution which is sufficient for link phases that typically run in the millisecond-to-second range.

## Key Facts

| Property | Value |
|---|---|
| Phase timer function | `sub_4279C0` at `0x4279C0` (32 bytes) |
| Start timer | `sub_45CCD0` at `0x45CCD0` (12 bytes) |
| Stop timer / compute elapsed | `sub_45CCE0` at `0x45CCE0` (52 bytes) |
| CSV header writer | `sub_432270` at `0x432270` (208 bytes) |
| CSV row writer | `sub_432340` at `0x432340` (255 bytes) |
| Timer state | `unk_2A5F1B0` -- global `struct timeval` (16 bytes) |
| Started flag | `byte_2A5F1C0` -- initialized to 0, set to 1 on first call |
| Timing file path | `qword_2A5F290` -- set from `-time <file>` CLI option |
| Arch string (column source) | `qword_2A5F318` -- written into the `arch` CSV column |
| Verbose/edbg flags | `dword_2A5F308` -- set by the `-edbg` CLI option |
| Verbose timing gate (main) | `elfw[64] & 0x20` (bit 5 of ELF wrapper flags byte) |
| Verbose timing gate (LTO) | `dword_2A5F308 & 0x20` (same bit in global debug flags) |
| Clock source | `gettimeofday(2)` -- microsecond wall-clock |
| Time unit | milliseconds (32-bit float) |
| Timing tags | 9 labels: `init`, `cicc-lto`, `ptxas-lto`, `read`, `merge`, `layout`, `relocate`, `finalize`, `write` |
| CLI flag (CSV file) | `-time <file>` (arg string at `0x1D3233D` -- shared tail of `"-compile-time"`) |
| CSV format string | `"%s , %s , %s , %s , %s , %s , %.4f , ms\n"` at `0x1D38700` |
| CSV header string | at `0x1D38698`, length `0x63` (99 bytes) |
| Stderr format string | `"%s time: %f\n"` at `0x1D32413` |

## Timer Primitives

### sub_45CCD0 -- Start Timer

```c
// 0x45CCD0 -- 12 bytes
int timer_start(struct timeval *tv) {
    return gettimeofday(tv, NULL);
}
```

A trivial wrapper around `gettimeofday`. Stores the current wall-clock time into the provided `struct timeval`. Called with a pointer to the global timer at `0x2A5F1B0`.

### sub_45CCE0 -- Stop Timer / Compute Elapsed

```c
// 0x45CCE0 -- 52 bytes
float timer_stop(struct timeval *start) {
    struct timeval now;
    gettimeofday(&now, NULL);
    float sec_ms  = (float)(now.tv_sec  - start->tv_sec)  * 1000.0f;
    float usec_ms = (float)(now.tv_usec - start->tv_usec) / 1000.0f;
    return sec_ms + usec_ms;
}
```

Reads the current time, computes the difference from `start` as a float in milliseconds. The arithmetic is done in 32-bit float, which gives roughly 6-7 significant digits -- adequate for timing measurements up to a few thousand seconds.

The exact IDA-decompiled expression is:

```c
(float)((float)(LODWORD(v2.tv_sec) - *a1) * 1000.0)
  + (float)((float)(LODWORD(v2.tv_usec) - a1[2]) / 1000.0)
```

-- seconds difference times 1000 plus microseconds difference divided by 1000, yielding total elapsed milliseconds. Note that `*a1` reads `tv_sec` as a raw 32-bit dword (`LODWORD`), which is a decompiler artifact on 64-bit `time_t`; the low 32 bits are sufficient for any realistic compile time.

### sub_4279C0 -- Phase Timer Checkpoint

```c
// 0x4279C0 -- 32 bytes
__int64 phase_timer(const char *label, float elapsed) {
    if (byte_2A5F1C0) {                    // started flag
        sub_45CCE0(&unk_2A5F1B0);          // stops timer, returns elapsed ms in xmm0
        fprintf(stderr, "%s time: %f\n", label, elapsed);
    } else {
        byte_2A5F1C0 = 1;                  // mark started
        nullsub_2();                       // stripped no-op
    }
    return sub_45CCD0(&unk_2A5F1B0);       // restart timer
}
```

This is the core timing checkpoint. On the first call (with label `"init"`), it sets the started flag and starts the timer without printing anything -- the "init" phase has no preceding checkpoint, so there is no elapsed time to report. On every subsequent call, it stops the timer, prints the elapsed time for the *preceding* phase to stderr, then restarts the timer for the next phase.

The decompiler lists `float a2` as the second parameter, but in practice `a2` is just the x87/xmm0 register holding the return value of `sub_45CCE0` from the immediately-preceding call -- IDA does not realize that `fprintf`'s `%f` argument comes from the freshly-computed `timer_stop` return. The `nullsub_2()` call is a no-op stub that was left over after stripping a logging or tracing hook from the release build.

## Stderr Timing Output

### Activation

Stderr timing is gated on bit 5 (mask `0x20`) of the verbose/debug flags. Two different flag words are checked depending on the call site:

```c
if (elfw[64] & 0x20)               // main pipeline: flags copy stored in the ELF wrapper
    phase_timer("label");

if (dword_2A5F308 & 0x20)          // LTO pipeline: global -edbg flags word
    phase_timer("label");
```

`dword_2A5F308` is the `-edbg` integer option registered in `sub_427AE0` via `sub_42E390(v2, "edbg", &dword_2A5F308, 4)`. Users pass `-edbg 32` (or any value with bit 5 set) to enable stderr phase timing. `elfw[64]` is a byte inside the ELF wrapper structure created by `sub_4438F0`; it is populated from `dword_2A5F308` when the wrapper is built, and mirrors the relevant bits so the main pipeline does not need to look up a global on every check.

The same `0x20` bit controls other verbose diagnostics such as the `"link input %s\n"` trace (which actually uses bit 0, mask `0x01`, at the same `elfw[64]` byte) -- the byte packs several verbose sub-flags together.

### Output Format

When active, each phase transition produces a single line on stderr:

```text
<phase_name> time: <elapsed_ms>
```

The format string is `"%s time: %f\n"` (verified verbatim at `0x1D32413`) using default `%f` precision (6 decimal places). The unit is always milliseconds. A typical LTO link with `-edbg 32` produces:

```text
cicc-lto time: 2456.789063
ptxas-lto time: 1234.567017
read time: 45.678001
merge time: 12.345000
layout time: 3.456000
relocate time: 8.901000
finalize time: 5.432000
write time: 1.234000
```

Note that `init` never appears on stderr -- on the first `phase_timer("init")` call, the started flag is false so only the `byte_2A5F1C0 = 1` / `nullsub_2()` / `sub_45CCD0` branch runs, and no `fprintf` is emitted. The first printed line is whichever tag follows `init`.

## CSV Output Format

### CLI Flag

The CSV timing file is controlled by the `-time <file>` option, registered in `sub_427AE0` at `0x428188`:

```c
// from sub_427AE0, around line 974:
sub_42E390(v2, "time", &qword_2A5F290, 8);  // string at 0x1D3233D
```

The option name string is at `0x1D3233D`, which is the `"time"` tail of the `"-compile-time"` string at `0x1D32334` -- the linker reuses the last five bytes (`time\0`) as the shorter option name. The 8-byte storage slot is `qword_2A5F290`, which holds a `char *` to the user-supplied filename (or NULL if `-time` was not passed).

The help text for the option (at `0x1D33380`, xref from `0x428190`) reads verbatim:

> Generate a comma separated value table with the time taken by this program and sub-component invocations, and append it at the end of the file given as the option argument. If the file is empty, the column headings are generated in the first row of the table. If the file name is `-`, the timing data is generated in stdout.

### CSV Header Line

The CSV header is written by `sub_432270` at `0x432270`. The exact header string (length `0x63` = 99 bytes, at `0x1D38698`) is:

```text
source file name , phase name , phase input files , phase output file , arch , tool, metric , unit
```

That is: eight columns separated by ` , ` (comma-space-space on each side, inconsistent -- note that the fifth separator between `tool` and `metric` is written as `, ` with no leading space, which is a typo in the NVIDIA header string that has been preserved verbatim). The row format uses a uniform ` , ` (space-comma-space) on every separator, so the header and rows are not byte-identical in their delimiter style, but CSV parsers that split on comma with whitespace trimming handle both correctly.

### Field-by-Field Description

Each CSV row is written by `sub_432340` using the format string `"%s , %s , %s , %s , %s , %s , %.4f , ms\n"` at `0x1D38700`. The fprintf argument order is `a4, a3, a5, a6, a8, a2, a7` (where `a1` is the filename and `a2..a8` are the data columns). After mapping fprintf positional arguments to header columns:

| # | Column | Source argument | Type | Notes |
|---|---|---|---|---|
| 1 | `source file name` | `a4` (rcx) | `const char *` | The logical source artifact being processed |
| 2 | `phase name` | `a3` (rdx) | `const char *` | Sub-tool name (`"ptxas"` or `"cicc"`) |
| 3 | `phase input files` | `a5` (r8) | `const char *` | Input file to the sub-tool for this phase |
| 4 | `phase output file` | `a6` (r9) | `const char *` | Output file produced by the sub-tool |
| 5 | `arch` | `a8` (stack arg) | `const char *` | Target arch string from `qword_2A5F318` (e.g. `"sm_90a"`) |
| 6 | `tool` | `a2` (rsi) | `const char *` | Always `"nvlink"` for rows emitted by nvlink itself |
| 7 | `metric` | `a7` (xmm0) | `float` | Elapsed time for this phase, printed as `%.4f` |
| 8 | `unit` | literal | string | Always the literal `"ms"` (hard-coded in the format string) |

The unit column is a literal `ms` appended by the format string rather than passed as a variable argument, so every row ends with ` , ms\n`.

### Example Output

A full CSV file produced by `nvlink -time build.csv ... -arch sm_90a foo.ptx` looks like:

```text
source file name , phase name , phase input files , phase output file , arch , tool, metric , unit
foo.ptx , ptxas , foo.ptx ,  , sm_90a , nvlink , 1234.5678 , ms
lto.nvvm , cicc , lto.nvvm , lto.ptx , sm_90a , nvlink , 2345.6789 , ms
lto.ptx , ptxas , lto.ptx , lto.cubin , sm_90a , nvlink , 3456.7891 , ms
```

Row 1 (per-input PTX JIT): `sub_432340` is called with `a3="ptxas"`, `a4=a5=<input path>` (`rbx` register holds the input filename in the loop), `a6=""`, `a8=qword_2A5F318`. The source file name and the input file name are identical because the linker is processing a single ptx input through ptxas.

Row 2 (LTO cicc compile): `a3="cicc"`, `a4="lto.nvvm"`, `a5=""`, `a6="lto.ptx"`, `a8=qword_2A5F318`. The source file is the synthetic `"lto.nvvm"` label for the merged IR module, with `"lto.ptx"` as the output of the cicc pass.

Row 3 (LTO ptxas compile): `a3="ptxas"`, `a4="lto.ptx"`, `a5=""`, `a6="lto.cubin"`, `a8=qword_2A5F318`. The synthetic `"lto.ptx"` is consumed by ptxas to produce `"lto.cubin"`.

The string literals `"lto.nvvm"`, `"lto.ptx"`, `"lto.cubin"`, `"cicc"`, `"ptxas"`, `"nvlink"` are all static strings compiled into nvlink's `.rodata`, not actual filenames on disk -- they serve as symbolic phase identifiers for the CSV consumer. The empty-string columns (`r8d = offset asc_1D410D3+2`, which points at a `\0` byte just past `..` in the rodata) are compiled as the address of a single NUL byte, so the `%s` format prints nothing for those fields.

### File Handling

`sub_432270` (header initialization, called once at `main()` entry around line 429-430):

```c
__int64 timing_csv_init(char *filename) {
    if (!filename) return nullsub_2();

    // Case 1: "-" means stdout
    if (filename[0] == '-' && filename[1] == '\0') {
        fwrite(CSV_HEADER, 1, 0x63, stdout);
        return nullsub_2();
    }

    // Case 2: existing file -- skip header, will append
    if (sub_462DF0(filename))       // file_exists check
        return nullsub_2();

    // Case 3: new file -- create and write header
    FILE *f = fopen(filename, "w");
    if (!f) sub_467460(&unk_2A5B890, filename, ...);   // fatal error
    fwrite(CSV_HEADER, 1, 0x63, f);
    fclose(f);
    return nullsub_2();
}
```

The three cases are:

1. **Filename is `"-"`**: writes the header to stdout. This is the "pipe to another tool" mode.
2. **File exists** (`sub_462DF0` returns true): the file was initialized by a previous invocation and already contains the header; nvlink will append rows to it in `sub_432340`.
3. **New file**: creates (truncates with `"w"`), writes the header, closes. Later `sub_432340` calls will reopen in append mode.

`sub_432340` (row writer, called after every sub-tool invocation):

```c
int timing_csv_row(char *filename, const char *a2_tool,
                   const char *a3_phase, const char *a4_source,
                   const char *a5_input, const char *a6_output,
                   float a7_metric, const char *a8_arch) {
    FILE *f;
    if (filename[0] == '-' && filename[1] == '\0')
        f = stdout;                       // write to stdout
    else
        f = fopen(filename, "a");         // append mode per row

    if (!f) sub_467460(&unk_2A5B890, filename, ...);   // fatal error

    fprintf(f, "%s , %s , %s , %s , %s , %s , %.4f , ms\n",
            a4_source, a3_phase, a5_input, a6_output,
            a8_arch, a2_tool, a7_metric);

    if (f != stdout) fclose(f);
    return 0;
}
```

Two important properties:

- **Open in append mode (`"a"`) per row write.** The file is opened, one row is written, and the file is closed before the next row. This is safe for sequential single-threaded writes and survives tool restart (you can point multiple nvlink invocations at the same CSV file and they will interleave rows without stepping on each other). The cost is one `open`/`close` syscall pair per row, which is negligible compared to the preceding ptxas/cicc run.
- **No quoting, no escaping.** The CSV writer uses raw `%s` format specifiers with no awareness of embedded commas, newlines, or quotes in the input strings. If a filename contains a literal comma or newline, the output will be malformed CSV. In practice this is not a problem because the source file names come from command-line arguments (no newlines) and architectures / tool names are hard-coded static strings.

### When CSV Rows Are Emitted

CSV rows are emitted at three points in `main()`, each gated by `if (qword_2A5F290)`:

| Call site | `main()` line | Phase | `a3` (phase name) | Source/Output pair |
|---|---|---|---|---|
| 1 | ~695-714 | Per-input PTX JIT (`sub_4BD760`) | `"ptxas"` | `rbx` (input path) -> `""` |
| 2 | ~983-1094 | LTO cicc compile (`sub_4BC6F0`) | `"cicc"` | `"lto.nvvm"` -> `"lto.ptx"` |
| 3 | ~1282 | LTO ptxas compile (`sub_4BD4E0`) | `"ptxas"` | `"lto.ptx"` -> `"lto.cubin"` |

At each call site the pattern is the same: `sub_45CCE0(ptr)` stops the timer and returns elapsed ms in xmm0, then `sub_432340(qword_2A5F290, "nvlink", phase, src, input, output, elapsed, arch)` writes the row, then `sub_45CCD0(ptr)` restarts the timer for the next measurement. The timer state used here is the stack-local `ptr` (a separate `struct timeval` inside `main()`'s frame), not the global `unk_2A5F1B0` used by the stderr `phase_timer` -- the CSV path and the stderr path are completely independent timers that happen to share the same `gettimeofday` primitives.

Crucially, the CSV path times **sub-tool invocations** (individual ptxas/cicc runs), while the stderr path times **pipeline phases**. A link with N input files generates N CSV rows for the per-input ptxas runs, plus up to 2 more rows for LTO cicc and LTO ptxas if LTO is active, but only 9 stderr phase tags total. The two systems answer different questions: "how long did each sub-tool invocation take?" vs. "how long did each linker phase take?".

## Pipeline Phase Timing (stderr path)

The 9 stderr timing tags map to pipeline phases described in the [pipeline overview](../pipeline/overview.md). They are emitted in this order:

| Order | Tag | Gate | Pipeline stage | Entry function |
|---|---|---|---|---|
| 1 | `"init"` | `elfw[64] & 0x20` | Option parsing, arena setup, library resolution complete | -- (first checkpoint, no elapsed time printed) |
| 2 | `"cicc-lto"` | `dword_2A5F308 & 0x20` | LTO IR compilation via libnvvm/cicc | `sub_4BC6F0` |
| 3 | `"ptxas-lto"` | `dword_2A5F308 & 0x20` | LTO PTX-to-SASS assembly | `sub_4BD4E0` |
| 4 | `"read"` | `elfw[64] & 0x20` | Input file loop complete (all cubins, ptx, fatbins read) | Input dispatch loop |
| 5 | `"merge"` | `elfw[64] & 0x20` | Section merging complete | `sub_45E7D0` loop |
| 6 | `"layout"` | `elfw[64] & 0x20` | Section layout and address assignment complete | `sub_439830` |
| 7 | `"relocate"` | `elfw[64] & 0x20` | Relocation patching complete | `sub_469D60` |
| 8 | `"finalize"` | `elfw[64] & 0x20` | ELF reindexing and header finalization complete | `sub_445000` |
| 9 | `"write"` | `elfw[64] & 0x20` | Output ELF written to disk | `sub_45C920`/`sub_45C950` |

The order depends on the link mode:

- **Non-LTO links** skip tags 2-3 and proceed directly from `init` to `read` (the LTO checkpoints are only emitted when `sub_4BC6F0` / `sub_4BD4E0` actually run).
- **LTO links** insert `cicc-lto` and `ptxas-lto` between `init` and `read`, because the LTO compilation pipeline runs before the normal merge/layout/relocate/finalize sequence.
- **Mercury targets** (sm >= 100) follow the same timing tags but the work inside `finalize` is lighter since the FNLZR post-link transform runs separately after output (see [mercury/fnlzr.md](../mercury/fnlzr.md)).

### Placement in main()

The timing checkpoints appear at the boundaries between major pipeline stages inside `main()` at `0x409800`:

```c
main() {
    parse_options();
    create_arenas();
    resolve_library_paths();

    if (qword_2A5F290)   sub_432270(qword_2A5F290);        // line ~429 (CSV header)
    if (elfw[64] & 0x20) sub_4279C0("init");               // line  593

    // --- Per-input processing loop (including per-file ptxas JIT) ---
    for (each input file) {
        if (is_ptx) {
            if (qword_2A5F290) sub_45CCD0(ptr);            // start CSV timer
            sub_4BD760(...);                                // per-input ptxas
            if (qword_2A5F290) {
                sub_45CCE0(ptr);                            // stop CSV timer
                sub_432340(qword_2A5F290, "nvlink",        // line  714 -- CSV row
                           "ptxas", rbx, rbx, "",
                           elapsed, qword_2A5F318);
            }
        }
        read_and_dispatch(file);
    }

    // --- LTO path (if -lto) ---
    if (qword_2A5F290) sub_45CCD0(ptr);                    // start CSV timer
    sub_4BC6F0(...);                                        // LTO nvvm/cicc
    if (qword_2A5F290) {
        sub_45CCE0(ptr);                                    // stop CSV timer
        sub_432340(qword_2A5F290, "nvlink",                // line 1092 -- CSV row
                   "cicc", "lto.nvvm", "", "lto.ptx",
                   elapsed, qword_2A5F318);
        sub_45CCD0(ptr);                                    // restart
    }
    if (dword_2A5F308 & 0x20) sub_4279C0("cicc-lto");      // line 1100

    sub_4BD4E0(...);                                        // LTO ptxas
    if (qword_2A5F290) {
        sub_45CCE0(ptr);
        sub_432340(qword_2A5F290, "nvlink",                // line 1282 -- CSV row
                   "ptxas", "lto.ptx", "", "lto.cubin",
                   elapsed, qword_2A5F318);
        sub_45CCD0(ptr);
    }
    if (dword_2A5F308 & 0x20) sub_4279C0("ptxas-lto");     // line 1286

    if (elfw[64] & 0x20) sub_4279C0("read");               // line 1403

    // --- Merge / Layout / Relocate / Finalize ---
    for (each object) merge_elf(object);
    if (elfw[64] & 0x20) sub_4279C0("merge");              // line 1426

    layout(elfw);        // sub_439830
    if (elfw[64] & 0x20) sub_4279C0("layout");             // line 1431

    relocate(elfw);      // sub_469D60
    if (elfw[64] & 0x20) sub_4279C0("relocate");           // line 1434

    finalize(elfw);      // sub_445000
    if (elfw[64] & 0x20) sub_4279C0("finalize");           // line 1440

    // --- Write ---
    write_elf(output_path);  // sub_45C920
    if (elfw[64] & 0x20) sub_4279C0("write");              // line 1671
}
```

## Global Timer State

The timing infrastructure uses a minimal set of global variables:

| Address | Type | Name | Description |
|---|---|---|---|
| `0x2A5F1B0` | `struct timeval` (16 bytes) | `g_timer` | stderr timer -- stores `tv_sec`/`tv_usec` of the last `phase_timer` checkpoint |
| `0x2A5F1C0` | `uint8_t` | `g_timer_started` | 0 before first `phase_timer` call, 1 after |
| `0x2A5F290` | `char *` | `timing_file_path` | CSV output path; NULL disables CSV timing; `"-"` means stdout |
| `0x2A5F308` | `uint32_t` | `edbg_flags` | `-edbg` option value; bit 5 (`0x20`) enables stderr timing in LTO paths |
| `0x2A5F318` | `char *` | `arch_name` | Target architecture string (e.g. `"sm_90a"`) written into CSV `arch` column |

Note that the CSV path does not use `unk_2A5F1B0`; it uses a separate stack-local `struct timeval` inside `main()` named `ptr` in the decompilation. This is why CSV row timings and stderr phase timings do not double-count or interfere -- they measure different intervals on different timers.

The `elfw[64]` byte in the ELF wrapper (set by `sub_4438F0` during wrapper creation) mirrors the low byte of `dword_2A5F308` so the main pipeline can check the verbose bit from the wrapper rather than a global. Both paths test the same `0x20` bit; the duplication exists because the LTO sub-pipeline runs before the ELF wrapper is fully populated for some code paths, so it must reach to the global directly.

## Reimplementation Notes

To reimplement the timing infrastructure:

1. **Two independent timers**: one for stderr phase timing (`g_timer` global, one `phase_timer` call per pipeline phase), one for CSV row timing (stack-local `struct timeval`, one pair of start/stop per sub-tool invocation). Do not share state between them.

2. **Phase timer semantics**: The first call to `phase_timer` initializes without printing. Every subsequent call prints the elapsed time since the previous call, then restarts. The time reported for tag N is the wall-clock duration between tags N-1 and N, not the duration of tag N itself.

3. **CSV output**: Opened in append mode per row. The header is written once at initialization (truncating `"w"` mode) only if the file does not already exist. The format uses space-comma-space as delimiters (except in the header, which has one `, ` typo between `tool` and `metric`). The timing precision is `%.4f` milliseconds. The `"unit"` column is always `ms` and is hard-coded in the format string.

4. **No CSV escaping**: The writer does not quote or escape strings. Assume input values contain no commas, quotes, or newlines. Filenames come from argv and are trusted.

5. **Float precision**: The decompilation shows 32-bit float arithmetic for the elapsed computation. For links exceeding ~16,000 seconds, the float loses sub-millisecond precision. This is unlikely to matter in practice.

6. **No cleanup**: There is no "finalize timer" function. The timer state is global and lives for the process lifetime. The last `phase_timer("write")` call prints the write-phase time and restarts the timer, but nothing ever reads the restarted value.

7. **Thread safety**: The global timer is not thread-safe. It is only called from `main()` on the main thread. The LTO split-compile thread pool does not use these timing functions, even for per-worker timing.

8. **Option wiring**: Register `"-time <file>"` in your option parser as an 8-byte string pointer that defaults to NULL. Register `"-edbg <int>"` as a 32-bit integer with bit 5 controlling stderr phase timing. Do not conflate `-time` and `-edbg`; they are independent switches for independent timer paths.

## Cross-References

**Internal (nvlink wiki):**

- [Pipeline Overview](../pipeline/overview.md) -- The 9 stderr timing tags map directly to the pipeline phases described here; see the phase list for the canonical ordering
- [Pipeline Entry](../pipeline/entry.md) -- `main()` timing checkpoint placement at phase boundaries, with exact line numbers and surrounding context
- [LTO Overview](../lto/overview.md) -- LTO pipeline stages that generate the `cicc-lto` and `ptxas-lto` timing tags and the corresponding CSV rows
- [Split Compilation](../lto/split-compilation.md) -- The thread pool runs between `cicc-lto` and `ptxas-lto` timing checkpoints but does not emit per-worker timings of its own
- [libnvvm Integration](../lto/libnvvm-integration.md) -- `sub_4BC6F0` is the cicc-lto entry point whose runtime is captured in the `cicc` CSV row
- [CLI Flags](../config/cli-flags.md) -- `-time <file>` for CSV output and `-edbg <int>` for stderr verbose (bit 5)
- [Environment Variables](../config/env-vars.md) -- No environment variables control the timing infrastructure; it is driven entirely by CLI flags
- [Error Reporting](error-reporting.md) -- Fatal error descriptor `unk_2A5B890` is invoked by `sub_467460` when the timing CSV file cannot be opened in either `sub_432270` (initial create) or `sub_432340` (append)

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| `timer_start` at `sub_45CCD0` wraps `gettimeofday` | HIGH | Decompiled: `return gettimeofday(a1, 0);` -- one-liner, exact match |
| `timer_stop` at `sub_45CCE0` computes milliseconds via float arithmetic | HIGH | Decompiled: `(float)(LODWORD(v2.tv_sec) - *a1) * 1000.0 + (float)(LODWORD(v2.tv_usec) - a1[2]) / 1000.0` |
| `phase_timer` at `sub_4279C0` checks `byte_2A5F1C0` started flag | HIGH | Decompiled: `if (byte_2A5F1C0)` then `fprintf(stderr, "%s time: %f\n", ...)` else `byte_2A5F1C0 = 1; nullsub_2()` |
| Stderr format string `"%s time: %f\n"` | HIGH | `sub_4279C0` decompiled shows exact string; confirmed at `0x1D32413` in strings JSON |
| Global timer state at `unk_2A5F1B0` | HIGH | Both `sub_45CCD0` and `sub_45CCE0` reference `&unk_2A5F1B0` in decompiled code via sub_4279C0 |
| Started flag at `byte_2A5F1C0` | HIGH | `sub_4279C0` decompiled: `byte_2A5F1C0 = 1` on first call path |
| CSV header string exact content | HIGH | `sub_432270` decompiled: `fwrite("source file name , phase name , phase input files , phase output file , arch , tool, metric , unit\n", 1u, 0x63u, ...)`; string located at `0x1D38698` in strings JSON |
| CSV header size is `0x63` (99) bytes | HIGH | `sub_432270` decompiled: `fwrite(..., 1u, 0x63u, ...)`; matches string length + newline |
| CSV row format `"%s , %s , %s , %s , %s , %s , %.4f , ms\n"` | HIGH | `sub_432340` decompiled fprintf; confirmed in asm at `0x4323a7` |
| CSV file opened in append mode (`"a"`) per row | HIGH | `sub_432340` decompiled: `fopen(filename, "a")`; verified in asm at `0x432364-7c` |
| CSV header writer uses write mode (`"w"`) for new files | HIGH | `sub_432270` decompiled: `fopen(filename, "w")` |
| File-exists check skips header rewrite | HIGH | `sub_432270`: `if ((unsigned __int8)sub_462DF0(filename)) return nullsub_2()` |
| `"-"` filename maps to stdout in both init and row writers | HIGH | `sub_432270` and `sub_432340`: `if (*filename == 45 && !filename[1]) f = stdout` |
| Timing file path stored at `qword_2A5F290` | HIGH | `sub_427AE0` registers it via `sub_42E390(v2, 30614333, &qword_2A5F290, 8)`; main() dereferences as CSV filename |
| CLI flag is `"-time"` (string at `0x1D3233D`) | HIGH | `0x1D3233D` is offset 9 into `"-compile-time"` at `0x1D32334`, giving `"time\0"`; verified by reading binary bytes |
| Help text at `0x1D33380` documents `-time <file>` behavior | HIGH | Exact help string extracted from strings JSON, xref at `0x428190` in `sub_427AE0` |
| CSV field order `source, phase, input, output, arch, tool, metric, unit` | HIGH | Column order derived from fprintf argument order (a4, a3, a5, a6, a8, a2, a7, literal) matched against header string column order |
| CSV `tool` column always `"nvlink"` | HIGH | All three `main()` call sites pass `rsi = offset aNvlink; "nvlink"`, verified in asm at `0x40a08a`, `0x40b1ce`, `0x40b5be` |
| CSV `phase` column is `"ptxas"` / `"cicc"` | HIGH | asm shows `edx = offset aPtxas` or `edx = offset aCicc` at each call site |
| `arch` column comes from `qword_2A5F318` | HIGH | asm at `0x40a073`, `0x40b1b2`, `0x40b5a7`: `push cs:qword_2A5F318` |
| CSV unit always `"ms"` literal in format string | HIGH | asm: format string at `0x1D38700` ends with `, ms\n` as baked-in literal |
| `-edbg` flag controls `dword_2A5F308` | HIGH | `sub_427AE0`: `sub_42E390(v2, "edbg", &dword_2A5F308, 4)` at line 949 of decompilation |
| Verbose gate `elfw[64] & 0x20` (bit 5) in main pipeline | HIGH | `main_0x409800.c` line 590, 1402, 1425, 1430, 1433, 1437, 1670 all check `(v55[64] & 0x20) != 0` before `sub_4279C0` calls |
| Verbose gate `dword_2A5F308 & 0x20` in LTO paths | HIGH | `main_0x409800.c` line 1097 checks `(dword_2A5F308 & 0x20) != 0` before `sub_4279C0("cicc-lto")` |
| 9 timing tags and their order | HIGH | Grep for `sub_4279C0` in `main_0x409800.c` returns exactly 9 calls with the listed string literals |
| CSV emitted at 3 call sites | HIGH | Grep for `sub_432340` returns exactly 3 call sites at lines 714, 1092, 1282 |
| CSV writer uses stack-local `ptr` timer distinct from global | HIGH | asm at `0x40a060-63`, `0x40b1a2`, `0x40b597` all pass `r13 = lea [rsp+offset]` to `sub_45CCE0`, not `&unk_2A5F1B0` |
| `nullsub_2` is a no-op stub | HIGH | Decompiled file `nullsub_2_0x45ccc0.c` exists and is a no-op |
| Timing CSV error uses `unk_2A5B890` descriptor | HIGH | Both `sub_432270` and `sub_432340` call `sub_467460(&unk_2A5B890, filename, ...)` on `fopen` failure |
| CSV header delimiter inconsistency (`tool,` vs `, `) | HIGH | Exact header string includes `... arch , tool, metric ...` verbatim at `0x1D38698` -- the missing space is a typo preserved across all nvlink releases |
| Empty-string fields come from `asc_1D410D3+2` | HIGH | asm: `r9d = (offset asc_1D410D3+2)` at `0x40a079` passes a pointer to a single `\0` byte |
