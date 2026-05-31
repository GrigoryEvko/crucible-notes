# Entry Point & Initialization

`main()` at `0x408950` is a 488-byte `__noreturn` function that orchestrates the entire cudafe++ compilation pipeline. It takes the standard POSIX signature `(int argc, char **argv, char **envp)`, performs two phases of subsystem initialization, optionally raises the process stack limit, then runs the frontend, backend, and exit sequence in a linearized loop that executes exactly once. The function has 22 direct callees (including `getrlimit`, `setrlimit`, and library calls) and never returns -- `sub_5AF1D0` at the bottom of the loop calls `exit()` or `abort()`.

## Key Facts

| Property | Value |
|---|---|
| Address | `0x408950` |
| Size | 488 bytes |
| Source file | `fe_init.c` / `host_envir.c` (initialization); `fe_wrapup.c` (finalization) |
| Signature | `void __noreturn main(int argc, char **argv, char **envp)` |
| Direct callees | 22 (9 pre-init + CLI + heavy-init + 5 pipeline stages + timing/exit helpers) |
| Stack frame | `0x88` bytes (136 bytes: 6 timing stamps + rlimit struct + alignment) |
| Attribute | `__noreturn` -- the `while(1)` loop terminates via `sub_5AF1D0` which calls `exit()`/`abort()` |

## Annotated Decompilation

```c
void __noreturn main(int argc, char **argv, char **envp)
{
    rlim_t original_stack;
    bool stack_was_raised;
    uint8_t exit_code;
    struct rlimit rlimits;
    timestamp_t t_total_start, t_fe_start, t_fe_end, t_be_start, t_be_end, t_total_end;

    // --- Redirect diagnostic output to stderr ---
    s = stderr;                            // 0x126EDF0 alias
    qword_126EDF0 = stderr;                // diagnostic stream

    // === PHASE 1: Pre-initialization (9 subsystem calls) ===
    sub_585D60(argc, argv, envp);          // fe_pre_init

    // --- Capture total compilation start time ---
    sub_5AF350(&t_total_start);            // capture_time

    // === PHASE 2: Command-line parsing ===
    sub_459630(argc, argv);                // proc_command_line (276 flags)

    // === Stack limit adjustment ===
    if (dword_106C064                      // --modify-stack-limit (default: ON)
        && !getrlimit(RLIMIT_STACK, &rlimits))
    {
        original_stack = rlimits.rlim_cur;
        rlimits.rlim_cur = rlimits.rlim_max;  // raise to hard limit
        stack_was_raised = (setrlimit(RLIMIT_STACK, &rlimits) == 0);
    }

    // === PHASE 3: Heavy initialization (38 subsystem calls + validation) ===
    sub_585DB0();                          // fe_one_time_init
    //   └─ sub_585EE0()  fe_init_part_1  (33 per-unit inits, output file, keywords)

    if (dword_106C0A4)                     // --timing enabled?
        sub_5AF350(&t_fe_start);           // capture frontend start

    // === PHASE 4: Translation unit setup ===
    sub_7A4860();                          // reset_tu_state (zero 6 TU globals)

    // === PHASE 5: Frontend parse + IL build ===
    sub_7A40A0(qword_126EEE0);            // process_translation_unit

    // === PHASE 6: Frontend wrapup (5-pass IL finalization) ===
    sub_588F90(qword_126EEE0, 1);         // fe_wrapup

    if (dword_106C0A4) {
        sub_5AF350(&t_fe_end);
        sub_5AF390("Front end time", &t_fe_start, &t_fe_end);
    }

    // --- Error gate: skip backend if frontend had errors ---
    if (!qword_126ED90) goto backend;     // no errors → run backend
    dword_106C254 = 1;                    // skip-backend flag

    // === Linearized exit loop (executes once) ===
    while (1) {
        exit_code = 8;                    // default: warnings
        sub_6B8B20(0);                    // reset file state
        sub_589530();                     // write signoff + close source mgr

        if (!qword_126ED90)               // re-check after wrapup
            exit_code = qword_126ED88 ? 5 : 3;  // success codes

        if (dword_106C0A4) {
            sub_5AF350(&t_total_end);
            sub_5AF390("Total compilation time", &t_total_start, &t_total_end);
        }

        if (stack_was_raised) {           // restore original stack limit
            rlimits.rlim_cur = original_stack;
            setrlimit(RLIMIT_STACK, &rlimits);
        }

        sub_5AF1D0(exit_code);            // __noreturn: exit() or abort()

    backend:
        if (!dword_106C254) {             // backend not skipped
            if (dword_106C0A4)
                sub_5AF350(&t_be_start);
            sub_489000();                 // process_file_scope_entities (backend)
            if (dword_106C0A4) {
                sub_5AF350(&t_be_end);
                sub_5AF390("Back end time", &t_be_start, &t_be_end);
            }
        }
    }
}
```

The `while(1)` never actually loops. The call to `sub_5AF1D0` is `__noreturn` (it calls `exit()` or `abort()` internally), so control never reaches the second iteration. The compiler arranged the basic blocks this way because the backend code at `backend:` is reached via a `goto` from the error-gate check, placing it logically "after" the exit call in the CFG.

## Phase 1: fe_pre_init -- `sub_585D60` (0x585D60)

The first thing `main()` does after redirecting stderr is call `sub_585D60`, which performs the absolute minimum initialization needed before command-line parsing can proceed. This function lives in `fe_init.c` and makes 9 sequential calls to subsystem pre-initializers, plus two inline global assignments.

### Pre-Init Call Table

| # | Address | Identity | Source | Purpose |
|---|---------|----------|--------|---------|
| 1 | `sub_48B3C0` | error_pre_init | error.c | Zero 4 error-tracking globals: `qword_1065870`=0, `qword_1065868`=0, `dword_1065860`=-1, `qword_1065858`=0 |
| 2 | `sub_6BB290` | source_file_mgr_pre_init | srcfile.c | Zero 10 file descriptor table globals: file chain head, file count, file hash, include stack |
| 3 | `sub_5B1E70` | host_envir_early_init | host_envir.c | **Heaviest pre-init call.** Signal handlers, locale, CWD capture, env vars. See below. |
| 4 | `sub_752C90` | type_system_pre_init | type.c | Set `dword_126E4A8`=-1 (dialect version unset), call `sub_7515D0` (type table alloc), set host compiler defaults (`qword_126E1F0`=70300 = GCC 7.3.0 default), init 3 type comparison descriptor pools via `sub_465510` |
| 5 | `sub_45EB40` | cmd_line_pre_init | cmd_line.c | Zero the 272-flag was-set bitmap (`byte_E7FF40`, 0x110 bytes), set `dword_E7FF20`=1 (skip argv[0]), initialize ~350 global config variables to defaults. Notable: `dword_106C064`=1 (stack limit adjustment ON by default) |
| 6 | `sub_4ED530` | declaration_pre_init | decls.c | Set `stderr` into two global stream pointers, zero error/warning counters (`qword_126ED80..qword_126EDE0`), set diagnostic defaults (`byte_126ED69`=5, `byte_126ED68`=8, `qword_126ED60`=100 max errors), clear 15.2KB diagnostic severity table (`byte_1067920`, 0x3B50 bytes) |
| 7 | `sub_6F6020` | il_pre_init | il.c | Zero 3 globals: `dword_12C6C8C`=0 (PCH event counter), `qword_12C6EC0`=0, `qword_12C6EB8`=0 |
| -- | *(inline)* | scope_index_init | fe_init.c | `dword_126C5E4 = -1` (current scope stack index = "none"), `dword_126C5C8 = -1` (secondary scope index = "none") |
| 8 | `sub_7A48B0` | tu_tracking_pre_init | trans_unit.c | Zero 13 TU tracking globals: source filename, compilation mode flags, TU stack pointers, PCH state |
| 9 | `sub_7C00F0` | template_pre_init | template.c | Single assignment: `dword_106BA20 = 0` (template nesting depth = 0) |

### host_envir_early_init (sub_5B1E70) Detail

This is the most substantial pre-init call. It initializes the host environment interface layer from `host_envir.c`:

**Signal handlers** (one-time, guarded by `dword_E6E120`):

| Signal | Handler | Behavior |
|--------|---------|----------|
| SIGINT (2) | `handler` at `0x5AF2C0` | Write newline to stderr, call `sub_5AF2B0(9)` which writes signoff then `exit(4)` |
| SIGTERM (15) | `handler` at `0x5AF2C0` | Same as SIGINT |
| SIGXCPU (24) | `sub_5AF270` | Print `"Internal error: CPU time limit exceeded.\n"`, call `sub_5AF1D0(11)` which calls `abort()` |
| SIGXFSZ (25) | `SIG_IGN` | Ignored (prevents crash on large output files) |

After signal setup, `dword_E6E120` is set to 0 so handlers are registered only once.

**Locale:** Calls `newlocale(LC_NUMERIC, "C", 0)` then `uselocale()` to force the `C` locale for numeric output. If either call fails, asserts with `"could not set LC_NUMERIC locale"` at `host_envir.c:264`.

**Working directory:** Iteratively calls `getcwd()` with a growing buffer (starting at 256 bytes, expanding by 256 on `ERANGE`) until it fits, then copies the result into `qword_126EEA0` via permanent allocation.

**Environment variables:**
- `EDG_BASE` -- read into `qword_126EE38` (base path for EDG data files; empty string if unset)
- `EDG_SUPPRESS_ASSERTION_LINE_NUMBER` -- if set and not `"0"`, sets `dword_126ED40 = 1` (suppress line numbers in internal assertion messages)

**CPU time limit:** Calls `getrlimit(RLIMIT_CPU)` then `setrlimit()` with `rlim_cur = RLIM_INFINITY` to disable the CPU time limit.

**Global zeroing:** Zeros ~50 host-environment globals including file descriptors, path buffers, platform flags, output filename pointers.

**Language mode:** Sets `dword_126EFB4 = 2` (default to C++ mode -- this is later overridden by CLI parsing if `-x c` is specified).

**Sentinel validation:** Checks `off_E6E0E0` against the string `"last"` to verify that the `predef_macro_mode_names` table was properly initialized at link time. On mismatch, asserts with `"predef_macro_mode_names not initialized properly"` at `host_envir.c:6927`.

## Stack Limit Adjustment

Between CLI parsing and heavy initialization, `main()` conditionally raises the process stack limit:

```c
if (dword_106C064 && !getrlimit(RLIMIT_STACK, &rlimits)) {
    original_stack = rlimits.rlim_cur;
    rlimits.rlim_cur = rlimits.rlim_max;   // raise soft to hard limit
    stack_was_raised = (setrlimit(RLIMIT_STACK, &rlimits) == 0);
}
```

The flag `dword_106C064` is set to 1 by default in `sub_45EB40` (cmd_line_pre_init) and can be disabled via the `--modify_stack_limit=false` CLI flag. The purpose is to prevent stack overflow during deep recursion in the C++ parser, template instantiation engine, and constexpr interpreter. After compilation completes (just before exit), `main()` restores the original `rlim_cur` value.

## Phase 3: fe_one_time_init -- `sub_585DB0` (0x585DB0)

This is the heaviest initialization stage. It zeroes the token state (`qword_126DD38` -- 6 bytes packed as a dword + word), optionally calls `sub_5AF330` for profiling init if `dword_106BD4C` is set, then makes 38 sequential calls to subsystem one-time initializers.

### One-Time Init Call Table

| # | Address | Identity | Source file |
|---|---------|----------|-------------|
| 1 | `sub_752DF0` | type_one_time_init | type.c |
| 2 | `sub_5B1D40` | scope_one_time_init | scope.c |
| 3 | `sub_447430` | error_one_time_init | error.c |
| 4 | `sub_4B37F0` | preprocessor_one_time_init | preproc.c |
| 5 | `sub_4E8ED0` | declaration_one_time_init | decls.c |
| 6 | `sub_4C0840` | attribute_one_time_init | attribute.c |
| 7 | `sub_4A1B60` | name_one_time_init | lookup.c |
| 8 | `sub_4E9CF0` | declaration_one_time_init_2 | decl_spec.c |
| 9 | `sub_4ED710` | declaration_one_time_init_3 | declarator.c |
| 10 | `sub_510C30` | statement_one_time_init | stmt.c |
| 11 | `sub_56DC90` | exprutil_one_time_init | exprutil.c |
| 12 | `sub_5A5160` | expression_one_time_init | expr.c |
| 13 | `sub_603B00` | parser_one_time_init | parse.c |
| 14 | `sub_5CF7F0` | class_one_time_init | class_decl.c |
| 15 | `sub_65DC50` | overload_one_time_init | overload.c |
| 16 | `sub_69C8B0` | template_one_time_init | template.c |
| 17 | `sub_665A00` | instantiation_one_time_init | instantiate.c |
| 18 | `sub_689550` | exception_one_time_init | except.c |
| 19 | `sub_68F640` | conversion_one_time_init | convert.c |
| 20 | `sub_6B6510` | il_one_time_init | il.c |
| 21 | `sub_6BAE70` | srcfile_one_time_init | srcfile.c |
| 22 | `sub_6F5FC0` | il_walk_one_time_init | il_walk.c |
| 23 | `sub_6F8300` | il_one_time_init_2 | il.c |
| 24 | `sub_6FDFF0` | lower_one_time_init | lower_il.c |
| 25 | `sub_726DC0` | mangling_one_time_init | lower_name.c |
| 26 | `sub_72D410` | mangling_one_time_init_2 | lower_name.c |
| 27 | `sub_74B9A0` | typecheck_one_time_init | typecheck.c |
| 28 | `sub_710B70` | il_one_time_init_3 | il.c |
| 29 | `sub_76D630` | codegen_one_time_init | cp_gen_be.c |
| 30 | `nullsub_11` | debug_one_time_init | debug.c (no-op) |
| 31 | `sub_7A4690` | allocation_one_time_init | il_alloc.c |
| 32 | `sub_7A3920` | pool_one_time_init | il_alloc.c |
| 33 | `sub_6A0E90` | template_one_time_init_2 | template.c |
| 34 | `sub_418F80` | diagnostics_one_time_init | diag.c |
| 35 | `sub_5859C0` | extasm_one_time_init | extasm.c |
| 36 | `sub_751540` | type_one_time_init_2 | type.c |
| 37 | `sub_7C25F0` | template_one_time_init_3 | template.c |
| 38 | `sub_7DF400` | cuda_one_time_init | nv_transforms.c |

The call order reflects dependency constraints: types before scopes, scopes before declarations, declarations before expressions, expressions before the parser, etc. Template initialization is split across three calls (#16, #33, #37) because different phases of template support depend on different subsystems being initialized first.

### Function Pointer Table Validation

After all 38 initializers complete, `sub_585DB0` performs a critical integrity check:

```c
if (funcs_6F71AE || off_D560C0 != nullsub_6)
    sub_4F21C0("function_pointers is incorrectly initialized");
```

This validates two conditions:

1. **`funcs_6F71AE` must be zero.** This global acts as a "dirty flag" -- if any initializer wrote a nonzero value here, the table was not properly zeroed during static initialization.

2. **`off_D560C0` must point to `nullsub_6` (0x585B00).** The address `off_D560C0` is the last entry in a function pointer dispatch table in `.rodata`. The empty function `nullsub_6` acts as a sentinel -- its known address is compared against the table's last slot to verify that the table was correctly populated at link time. If the linker reordered or dropped entries, the sentinel would not match.

If either check fails, `sub_4F21C0` emits a fatal diagnostic (`"function_pointers is incorrectly initialized"`) and then falls through to `sub_585EE0` (fe_init_part_1) regardless -- this is a non-recoverable error that will likely cause crashes later, but the code attempts to continue.

On successful validation, `sub_585DB0` returns without calling `sub_585EE0`. However, `sub_585EE0` is actually called from a *different* path: the normal flow is that `sub_585DB0` returns, and `main()` proceeds. The `sub_585EE0` call on the error path in `sub_585DB0` appears to be a fallthrough from the panic handler.

**Correction from the sweep report:** Examination of the actual decompiled code shows that `sub_585EE0` (fe_init_part_1) is called only on the *error path* of the sentinel check within `sub_585DB0`. On the normal (no-error) path, `sub_585DB0` returns `sub_7DF400()`'s return value directly. This means `fe_init_part_1` is called from the sentinel-check error handler, not from the main success path of `sub_585DB0`. The actual invocation of `fe_init_part_1` in the normal flow must occur elsewhere in the pipeline (likely called from within one of the subsystem initializers or from `sub_7A40A0`).

## fe_init_part_1 -- `sub_585EE0` (0x585EE0)

This function performs per-compilation-unit initialization. It is identified by the debug trace string `"fe_init_part_1"` at level 5 and an assertion path `fe_init.c:2007`. Its responsibilities:

### Compilation Timestamp

```c
time(&timer);
char *t = ctime(&timer);
if (!t) t = "Sun Jan 01 00:00:00 1900\n";
if (strlen(t) > 127)
    assert("fe_init.c", 2007, "fe_init_part_1");  // buffer overflow guard
strcpy(byte_106B5C0, t);   // 128-byte timestamp buffer
dword_126EE48 = 1;         // init-complete flag
```

### Per-Unit Initializer Call Table

After the timestamp, `sub_585EE0` calls 33 per-compilation-unit initializers:

| # | Address | Identity |
|---|---------|----------|
| 1 | `sub_4ED7C0` | declaration_unit_init |
| 2 | `nullsub_7` | *(no-op placeholder)* |
| 3 | `sub_65DC20` | overload_unit_init |
| 4 | `sub_6BB350` | srcfile_unit_init |
| 5 | `sub_5B22E0` | scope_unit_init |
| 6 | `sub_603B30` | parser_unit_init |
| 7 | `sub_5D0170` | class_unit_init |
| 8 | `sub_61EBD0` | expression_unit_init |
| 9 | `sub_68A0D0` | exception_unit_init |
| 10 | `sub_74BFF0` | typecheck_unit_init |
| 11 | `sub_710DE0` | il_unit_init |
| 12 | `sub_4E8F10` | declaration_unit_init_2 |
| 13 | `sub_4C0860` | attribute_unit_init |
| 14 | `nullsub_2` | *(no-op placeholder)* |
| 15 | `sub_4474D0` | error_unit_init |
| 16 | `sub_665A60` | instantiation_unit_init |
| 17 | `sub_4E9D10` | decl_spec_unit_init |
| 18 | `sub_76D780` | codegen_unit_init |
| 19 | `sub_7C0300` | template_unit_init |
| 20 | `sub_7A3980` | pool_unit_init |
| 21 | `sub_56DEE0` | exprutil_unit_init |
| 22 | `nullsub_10` | *(no-op placeholder)* |
| 23 | `sub_6B6890` | il_unit_init_2 |
| 24 | `sub_726EE0` | mangling_unit_init |
| 25 | `sub_6F5DA0` | il_walk_unit_init |
| 26 | `sub_6F8320` | il_unit_init_3 |
| 27 | `sub_6FE130` | lower_unit_init |
| 28 | `sub_752FC0` | type_unit_init |
| 29 | `sub_4660B0` | folding_unit_init |
| 30 | `sub_5943E0` | float_unit_init |
| 31 | `sub_6A0F40` | template_unit_init_2 |
| 32 | `sub_4190B0` | diagnostics_unit_init |
| 33 | `sub_7C2640` | template_unit_init_3 |

### Compilation Mode Flags

After the per-unit initializers, `sub_585EE0` copies global configuration values (set during CLI parsing) into the compilation-mode descriptor at `0x126EB88`:

| Field | Address | Source | Meaning |
|-------|---------|--------|---------|
| `byte_126EB88` | `0x126EB88` | `dword_126E498` | Dialect flags |
| `byte_126EBB0` | `0x126EBB0` | `dword_126EFB4 == 1` | K&R C mode |
| `dword_126EBA8` | `0x126EBA8` | `dword_126EFB4 != 2` | Not-C++ flag |
| `dword_126EBAC` | `0x126EBAC` | `dword_126EF68` | C standard version |
| `byte_126EBB8` | `0x126EBB8` | `dword_126EFB0` | Strict C mode |
| `byte_126EBB9` | `0x126EBB9` | `dword_126EFAC` | EDG GNU-compat extensions |
| `byte_126EBBA` | `0x126EBBA` | `dword_126EFA4` | Clang extensions enabled |
| `xmmword_126EBC0` | `0x126EBC0` | `qword_126EF90` | Clang + GNU version thresholds (16 bytes packed) |

### Output File Setup

```c
if (dword_106C298) {                          // output enabled
    if (qword_106C278)                        // output path specified
        qword_106C280 = sub_4F48F0(path, 0, 0, 16, 1513);  // open file (ID 1513)
    else
        qword_106C280 = stdout;               // default to stdout
}
sub_5AEDB0();                                 // write output header
```

The output file ID 1513 is one of three output file slots used during compilation (1513, 1514, 1515).

## Initialization Summary

The total initialization sequence before parsing begins involves **80+ subsystem init calls** across three layers:

```text
main()
 ├─ sub_585D60()  fe_pre_init           9 subsystem pre-inits
 │   ├─ sub_48B3C0   error              4 globals zeroed
 │   ├─ sub_6BB290   srcfile            10 globals zeroed
 │   ├─ sub_5B1E70   host_envir         signals, locale, CWD, env vars, ~50 globals
 │   ├─ sub_752C90   types              type table alloc, compiler defaults
 │   ├─ sub_45EB40   cmd_line           272-flag bitmap, ~350 config defaults
 │   ├─ sub_4ED530   declarations       error counters, diagnostic severity table (15KB)
 │   ├─ sub_6F6020   il                 3 globals zeroed
 │   ├─ [inline]     scope indices      dword_126C5E4 = dword_126C5C8 = -1
 │   ├─ sub_7A48B0   tu_tracking        13 globals zeroed
 │   └─ sub_7C00F0   templates          1 global zeroed
 │
 ├─ sub_459630()  proc_command_line     276 flags → ~150 config globals
 │
 ├─ [RLIMIT_STACK adjustment]           raise soft limit to hard limit
 │
 └─ sub_585DB0()  fe_one_time_init      38 subsystem one-time inits
     ├─ token state zeroing             qword_126DD38 = 0 (6 bytes)
     ├─ 38 subsystem calls              types → scopes → errors → ... → CUDA
     ├─ sentinel check                  funcs_6F71AE == 0 && off_D560C0 == nullsub_6
     └─ sub_585EE0()  fe_init_part_1    (on error path, or called from subsystem)
         ├─ compilation timestamp       byte_106B5C0 via ctime()
         ├─ 33 per-unit inits           declarations → overload → ... → templates
         ├─ compilation mode flags      copy CLI config into descriptor struct
         ├─ output file open            stdout or file (ID 1513)
         └─ sub_5AEDB0()               write output header
```

## Global State Set Before Parsing

By the time `sub_7A40A0` (process_translation_unit) is called, the following critical globals have been established:

| Global | Address | Value | Set by |
|--------|---------|-------|--------|
| `dword_126EFB4` | `0x126EFB4` | 2 (C++) | `sub_5B1E70` default, may be overridden by CLI |
| `dword_126EF68` | `0x126EF68` | C/C++ standard version | CLI parsing |
| `dword_106C064` | `0x106C064` | 1 (stack limit ON) | `sub_45EB40` default |
| `dword_106C0A4` | `0x106C0A4` | 0 or 1 | CLI `--timing` flag |
| `qword_126EEE0` | `0x126EEE0` | source filename | CLI parsing |
| `qword_106C280` | `0x106C280` | output FILE* | `sub_585EE0` |
| `qword_126EDF0` | `0x126EDF0` | stderr | `main()` + `sub_4ED530` |
| `dword_126EE48` | `0x126EE48` | 1 | `sub_585EE0` (init-complete flag) |
| `byte_106B5C0` | `0x106B5C0` | ctime string | `sub_585EE0` (compilation timestamp) |
| `dword_126C5E4` | `0x126C5E4` | -1 then updated | `sub_585D60` then scope init |
| `qword_126F120` | `0x126F120` | C locale handle | `sub_5B1E70` |
| `qword_126EEA0` | `0x126EEA0` | CWD string copy | `sub_5B1E70` |

## The Error Gate

The transition from frontend to backend is controlled by a simple error check:

```c
if (!qword_126ED90)          // qword_126ED90 = error count from frontend
    goto backend_label;      // no errors → run backend
dword_106C254 = 1;           // errors → set skip-backend flag
```

When `dword_106C254 == 1`, the backend stage (`sub_489000`) is skipped entirely. The process still writes a signoff trailer and exits with a nonzero status code. This means a cudafe++ compilation with frontend errors produces no `.int.c` output file -- the backend never runs.

## Exit Code Mapping

The exit function `sub_5AF1D0` at `0x5AF1D0` maps internal status codes to process exit codes:

| Internal Code | Meaning | Process Exit | Message |
|---------------|---------|-------------|---------|
| 3, 4, 5 | Success (various) | `exit(0)` | *(none)* |
| 8 | Warnings only | `exit(2)` | *(none)* |
| 9, 10 | Compilation errors | `exit(4)` | `"Compilation terminated.\n"` |
| 11 | Internal error | `abort()` | `"Compilation aborted.\n"` |
| *(other)* | Unknown/fatal | `abort()` | *(none)* |

In SARIF mode (`dword_106BBB8` set), the text messages (`"Compilation terminated."`, `"Compilation aborted."`) are suppressed, but exit codes remain identical.

## Cross-References

- [Pipeline Overview](./overview.md) -- complete 8-stage pipeline diagram
- [CLI Processing](./cli.md) -- detailed breakdown of `sub_459630` and all 276 flags
- [Frontend Invocation](./frontend.md) -- `sub_7A40A0` (process_translation_unit) internals
- [Frontend Wrapup](./fe-wrapup.md) -- 5-pass architecture of `sub_588F90`
- [Backend Code Generation](./backend.md) -- `sub_489000` (.int.c emission)
- [Timing & Exit](./timing-exit.md) -- `sub_5AF350`/`sub_5AF390`/`sub_5AF1D0` details
- [EDG Overview](../edg/overview.md) -- EDG 6.6 source tree and NVIDIA modifications
- [EDG Lexer](../edg/lexer.md) -- keyword registration performed during `sub_5863A0`
