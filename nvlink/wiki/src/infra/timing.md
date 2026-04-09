# Timing Infrastructure

nvlink embeds a lightweight phase-timer system that measures wall-clock elapsed time for each major pipeline stage. The system has two independent output paths: a human-readable stderr trace controlled by the `--verbose` flag (bit 5), and a machine-readable CSV log controlled by an internal timing-file option. Both paths share the same `gettimeofday`-based stopwatch primitives but serve different audiences -- stderr output helps developers identify slow phases during interactive builds, while the CSV path feeds automated performance-regression infrastructure.

The timer is deliberately simple: a single global `struct timeval` at `0x2A5F1B0`, a boolean "started" flag at `byte_2A5F1C0`, and three small functions totaling under 200 bytes of code. There is no per-thread timing, no nested/hierarchical timers, and no high-resolution clock source -- `gettimeofday` provides microsecond resolution which is sufficient for link phases that typically run in the millisecond-to-second range.

## Key Facts

| Property | Value |
|---|---|
| Phase timer function | `sub_4279C0` at `0x4279C0` (32 bytes) |
| Start timer | `sub_45CCD0` at `0x45CCD0` (12 bytes) |
| Stop timer / compute elapsed | `sub_45CCE0` at `0x45CCE0` (52 bytes) |
| CSV header writer | `sub_432270` at `0x432270` (208 bytes) |
| CSV row writer | `sub_432340` at `0x432340` (208 bytes) |
| Timer state | `unk_2A5F1B0` -- global `struct timeval` (16 bytes) |
| Started flag | `byte_2A5F1C0` -- initialized to 0, set to 1 on first call |
| Timing file path | `qword_2A5F290` -- set from internal CLI option |
| Verbose timing gate | `elfw+64 & 0x20` (bit 5 of ELF wrapper flags word) |
| LTO timing gate | `dword_2A5F308 & 0x20` (same bit in global verbose flags) |
| Clock source | `gettimeofday(2)` -- microsecond wall-clock |
| Time unit | milliseconds (float) |
| Timing tags | 9 labels: `init`, `read`, `cicc-lto`, `ptxas-lto`, `merge`, `layout`, `relocate`, `finalize`, `write` |

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

The computation uses the IDA-decompiled form `(LODWORD(tv_sec) - *a1) * 1000.0 + (LODWORD(tv_usec) - a1[2]) / 1000.0`, which confirms: seconds difference times 1000 plus microseconds difference divided by 1000, yielding total elapsed milliseconds.

### sub_4279C0 -- Phase Timer Checkpoint

```c
// 0x4279C0 -- 32 bytes
void phase_timer(const char *label, float elapsed) {
    if (g_timer_started) {                  // byte_2A5F1C0
        timer_stop(&g_timer);              // sub_45CCE0(&unk_2A5F1B0)
        fprintf(stderr, "%s time: %f\n", label, elapsed);
    } else {
        g_timer_started = 1;               // byte_2A5F1C0 = 1
        // nullsub_2() -- no-op (stripped initialization)
    }
    timer_start(&g_timer);                 // sub_45CCD0(&unk_2A5F1B0)
}
```

This is the core timing checkpoint. On the first call (typically with label `"init"`), it sets the started flag and starts the timer without printing anything -- the "init" phase time cannot be computed because there was no previous checkpoint. On every subsequent call, it stops the timer, prints the elapsed time for the preceding phase to stderr, then restarts the timer for the next phase.

The decompiler shows `float a2` as the second parameter, but in practice the elapsed value is computed inside `sub_45CCE0` and the fprintf uses the return value from that call. The `a2` parameter in the prototype is a decompiler artifact from the x87 floating-point return convention.

The `nullsub_2()` call on the first-time path is a no-op stub -- a stripped logging or tracing call that was compiled out in the release build.

## Stderr Timing Output

### Activation

Stderr timing is gated on bit 5 (mask `0x20`) of the verbose flags. In the main pipeline, the test appears as:

```c
if (elfw_flags[64] & 0x20)         // in main(), using the elfw flags word
    phase_timer("label");

if (dword_2A5F308 & 0x20)          // in the LTO path, using the global verbose flags
    phase_timer("label");
```

The `0x20` bit is set when the user passes `--verbose` or the equivalent internal `-edbg` flag at a sufficient level. This is the same flags word that controls other verbose output like `"link input %s\n"` (bit 0, mask `0x01`).

### Output Format

When active, each phase transition produces a single line on stderr:

```
<phase_name> time: <elapsed_ms>
```

For example, a typical LTO link might produce:

```
init time: 0.123000
cicc-lto time: 2456.789063
ptxas-lto time: 1234.567017
read time: 45.678001
merge time: 12.345000
layout time: 3.456000
relocate time: 8.901000
finalize time: 5.432000
write time: 1.234000
```

The format string is `"%s time: %f\n"` with default float formatting (6 decimal places). The unit is always milliseconds.

## CSV Timing Output

### Initialization -- sub_432270

When `qword_2A5F290` (the timing file path) is non-NULL, `sub_432270` is called early in `main()` to write a CSV header:

```c
// 0x432270
void timing_csv_init(char *filename) {
    if (!filename) { nullsub_2(); return; }

    if (filename[0] == '-' && filename[1] == '\0') {
        // "-" means stdout
        fwrite(CSV_HEADER, 1, 0x63, stdout);
        nullsub_2();
        return;
    }

    if (file_exists(filename))       // sub_462DF0
        { nullsub_2(); return; }     // file already has header

    FILE *f = fopen(filename, "w");
    if (!f) fatal_error(...);
    fwrite(CSV_HEADER, 1, 0x63, f);
    fclose(f);
    nullsub_2();
}
```

The CSV header line is:

```
source file name , phase name , phase input files , phase output file , arch , tool, metric , unit
```

The function handles three cases:
1. **Filename is `"-"`**: writes the header to stdout.
2. **File already exists** (`sub_462DF0` returns true): skips writing the header, because the file was initialized by a previous invocation and will be appended to.
3. **New file**: creates the file, writes the header, closes it.

### Row Output -- sub_432340

Each timing measurement writes a CSV row via `sub_432340`:

```c
// 0x432340
void timing_csv_row(char *filename, const char *arch,
                    const char *phase, const char *input_file,
                    const char *output_file, const char *tool,
                    float elapsed_ms, const char *reserved) {
    FILE *f;
    if (filename[0] == '-' && filename[1] == '\0')
        f = stdout;
    else
        f = fopen(filename, "a");    // append mode

    if (!f) fatal_error(...);

    fprintf(f, "%s , %s , %s , %s , %s , %s , %.4f , ms\n",
            input_file, phase, output_file, reserved, tool, arch,
            elapsed_ms);

    if (f != stdout)
        fclose(f);
}
```

Each row records a structured timing record with the architecture string (e.g. `"sm_90a"`), the tool name (`"nvlink"`), and the elapsed time in milliseconds with 4 decimal places. The file is opened in append mode (`"a"`) for each row, which is safe for sequential single-threaded invocations but means the file gets opened and closed once per measurement.

### CSV Usage in the Pipeline

The CSV timing path appears at three points in `main()`:

1. **Per-input PTX compilation** (around line 710): After `sub_4BD760` (ptxas JIT), if `qword_2A5F290` is set, `timer_stop` is called and the elapsed time is written via `sub_432340` with `"nvlink"` as the tool and `qword_2A5F318` (the arch string) as the architecture.

2. **LTO nvvm compilation** (around line 1088): After `sub_4BC6F0` (nvvm compile), the same pattern -- stop timer, write CSV row, restart timer.

3. **LTO ptxas compilation** (around line 1279): After the LTO-to-PTX-to-SASS compilation completes, another CSV row is emitted.

The CSV timing is strictly for sub-tool invocation timing (ptxas, nvvm), not for the phase-level timing that goes to stderr. The two systems are complementary: stderr shows coarse phase boundaries, CSV shows fine-grained tool invocations within those phases.

## Timing Checkpoints in the Pipeline

The 9 timing tags map directly to the pipeline phases described in the [pipeline overview](../pipeline/overview.md). They are emitted in this order:

| Order | Tag | Gate | Pipeline stage | Entry function |
|---|---|---|---|---|
| 1 | `"init"` | `elfw+64 & 0x20` | Option parsing, arena setup, library resolution complete | -- (first checkpoint, no elapsed time printed) |
| 2 | `"cicc-lto"` | `dword_2A5F308 & 0x20` | LTO IR compilation via libnvvm/cicc | `sub_4BC6F0` |
| 3 | `"ptxas-lto"` | `dword_2A5F308 & 0x20` | LTO PTX-to-SASS assembly | `sub_4BD760` (via LTO path) |
| 4 | `"read"` | `elfw+64 & 0x20` | Input file loop complete (all cubins, ptx, fatbins read) | Input dispatch loop |
| 5 | `"merge"` | `elfw+64 & 0x20` | Section merging complete | `sub_45E7D0` loop |
| 6 | `"layout"` | `elfw+64 & 0x20` | Section layout and address assignment complete | `sub_439830` |
| 7 | `"relocate"` | `elfw+64 & 0x20` | Relocation patching complete | `sub_469D60` |
| 8 | `"finalize"` | `elfw+64 & 0x20` | ELF reindexing and header finalization complete | `sub_445000` |
| 9 | `"write"` | `elfw+64 & 0x20` | Output ELF written to disk | `sub_45C920`/`sub_45C950` |

The order depends on the link mode:
- **Non-LTO links** skip tags 2-3 and proceed directly from `init` to `read`.
- **LTO links** insert `cicc-lto` and `ptxas-lto` between `init` and `read`, because the LTO compilation pipeline runs before the normal merge/layout/relocate/finalize sequence.
- **Mercury targets** (sm >= 100) follow the same timing tags but the work inside `finalize` is lighter since the FNLZR post-link transform runs separately after output.

### Placement in main()

The timing checkpoints appear at the boundaries between major pipeline stages inside `main()`:

```
main() {
    parse_options();
    create_arenas();
    resolve_library_paths();

    if (verbose & 0x20)  phase_timer("init");       // line ~593

    // --- LTO path (if -lto) ---
    collect_ir_modules();
    nvvm_compile();         // sub_4BC6F0
    if (verbose & 0x20)  phase_timer("cicc-lto");   // line ~1100

    lto_ptxas();            // sub_4BD760
    if (verbose & 0x20)  phase_timer("ptxas-lto");  // line ~1286

    // --- Input loop ---
    for (each input file) {
        read_and_dispatch(file);
    }
    if (verbose & 0x20)  phase_timer("read");        // line ~1403

    // --- Merge ---
    for (each object) merge_elf(object);
    if (verbose & 0x20)  phase_timer("merge");       // line ~1426

    // --- Layout ---
    layout(elfw);            // sub_439830
    if (verbose & 0x20)  phase_timer("layout");      // line ~1431

    // --- Relocate ---
    relocate(elfw);          // sub_469D60
    if (verbose & 0x20)  phase_timer("relocate");    // line ~1434

    // --- Finalize ---
    finalize(elfw);          // sub_445000
    if (verbose & 0x20)  phase_timer("finalize");    // line ~1440

    // --- Write ---
    write_elf(output_path);  // sub_45C920
    if (verbose & 0x20)  phase_timer("write");       // line ~1671
}
```

## Global Timer State

The timer uses a minimal set of global variables:

| Address | Type | Name | Description |
|---|---|---|---|
| `0x2A5F1B0` | `struct timeval` (16 bytes) | `g_timer` | Stores the `tv_sec`/`tv_usec` of the last checkpoint |
| `0x2A5F1C0` | `uint8_t` | `g_timer_started` | 0 before first `phase_timer` call, 1 after |
| `0x2A5F290` | `char *` | `timing_file_path` | Path for CSV output; NULL disables CSV timing |
| `0x2A5F308` | `uint32_t` | `verbose_flags` | Bit 5 (`0x20`) enables stderr timing in LTO paths |

The `elfw+64` flags word in the ELF wrapper mirrors the relevant bits from `dword_2A5F308` -- it is set during `elfw_create` (`sub_4438F0`) from the global verbose flags. Both check the same `0x20` bit, but the elfw copy is used in the main pipeline while the global is used in the LTO sub-pipeline (which runs before the elfw is fully set up for some code paths).

## Reimplementation Notes

To reimplement the timing infrastructure:

1. **Timer state**: A single `struct timeval` and a boolean flag. No heap allocation, no locking.

2. **Phase timer semantics**: The first call to `phase_timer` initializes without printing. Every subsequent call prints the elapsed time since the previous call, then restarts. This means the time reported for tag N is actually the wall-clock duration between tags N-1 and N.

3. **CSV output**: Opened in append mode per row write. The header is written once at initialization. The format uses comma-space (` , `) as delimiters, not bare commas. The timing precision is `%.4f` milliseconds.

4. **Float precision**: The decompilation shows 32-bit float arithmetic for the elapsed computation. For links exceeding ~16,000 seconds, the float loses sub-millisecond precision. This is unlikely to matter in practice.

5. **No cleanup**: There is no "finalize timer" function. The timer state is global and lives for the process lifetime. The last `phase_timer("write")` call prints the write-phase time and restarts the timer, but nothing ever reads the restarted value.

6. **Thread safety**: The global timer is not thread-safe. It is only called from `main()` on the main thread. The LTO split-compile thread pool does not use these timing functions.

## Cross-References

**Internal (nvlink wiki):**

- [Pipeline Overview](../pipeline/overview.md) -- The 9 timing tags map directly to pipeline phases described here
- [Pipeline Entry](../pipeline/entry.md) -- `main()` timing checkpoint placement at phase boundaries
- [LTO Overview](../lto/overview.md) -- LTO pipeline stages that generate the `cicc-lto` and `ptxas-lto` timing tags
- [Split Compilation](../lto/split-compilation.md) -- The thread pool runs between `cicc-lto` and `ptxas-lto` timing checkpoints
- [CLI Flags](../config/cli-flags.md) -- `--verbose` flag that gates stderr timing output (bit 5, mask `0x20`)
- [Environment Variables](../config/env-vars.md) -- Internal timing-file option controlling CSV output path
- [Error Reporting](error-reporting.md) -- Fatal error handler (`sub_467460`) called when CSV file cannot be opened

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| `timer_start` at `sub_45CCD0` wraps `gettimeofday` | HIGH | Decompiled: `return gettimeofday(a1, 0);` -- one-liner, exact match |
| `timer_stop` at `sub_45CCE0` computes milliseconds via float arithmetic | HIGH | Decompiled: `(float)(LODWORD(v2.tv_sec) - *a1) * 1000.0 + (float)(LODWORD(v2.tv_usec) - a1[2]) / 1000.0` |
| `phase_timer` at `sub_4279C0` checks `byte_2A5F1C0` started flag | HIGH | Decompiled: `if (byte_2A5F1C0)` then `fprintf(stderr, "%s time: %f\n", ...)` else `byte_2A5F1C0 = 1` |
| Format string `"%s time: %f\n"` | HIGH | `sub_4279C0` decompiled shows exact string; confirmed at `0x1d32413` in strings JSON |
| Global timer state at `unk_2A5F1B0` | HIGH | Both `sub_45CCD0` and `sub_45CCE0` reference `&unk_2A5F1B0` in decompiled code |
| Started flag at `byte_2A5F1C0` | HIGH | `sub_4279C0` decompiled: `byte_2A5F1C0 = 1` on first call |
| CSV header string | HIGH | `sub_432270` decompiled: `fwrite("source file name , phase name , phase input files , phase output file , arch , tool, metric , unit\n", 1u, 0x63u, ...)` -- exact match; string at `0x1d38698` |
| CSV header size is 0x63 (99) bytes | HIGH | `sub_432270` decompiled: `fwrite(..., 1u, 0x63u, ...)` |
| CSV row format `"%.4f , ms"` | HIGH | `sub_432340` decompiled: `fprintf(v10, "%s , %s , %s , %s , %s , %s , %.4f , ms\n", ...)` |
| CSV file opened in append mode ("a") per row | HIGH | `sub_432340` decompiled: `fopen(filename, "a")` |
| File-exists check skips header rewrite | HIGH | `sub_432270` decompiled: `if ((unsigned __int8)sub_462DF0(filename)) return nullsub_2()` |
| "-" filename maps to stdout | HIGH | `sub_432270`: `if (*filename == 45 && !filename[1])` writes to `stdout`; `sub_432340` same pattern |
| Timing file path at `qword_2A5F290` | MEDIUM | Referenced indirectly through main() call patterns; not directly verified in timer functions |
| Verbose gate `elfw+64 & 0x20` (bit 5) | MEDIUM | Gate condition inferred from main() decompilation; exact bit position verified at representative call sites |
| 9 timing tags: init, read, cicc-lto, ptxas-lto, merge, layout, relocate, finalize, write | MEDIUM | Tag strings inferred from main() decompilation flow; `"%s time: %f\n"` format used at each checkpoint |
| `nullsub_2` is a no-op stub | HIGH | Decompiled file `nullsub_2_0x45ccc0.c` exists and is a no-op |
| Timing CSV error uses `unk_2A5B890` descriptor | HIGH | Both `sub_432270` and `sub_432340` call `sub_467460(&unk_2A5B890, filename)` on `fopen` failure |
