# Pipeline Overview

cudafe++ is a source-to-source compiler. It reads a `.cu` file, parses it as C++ with CUDA extensions using a modified EDG 6.6 frontend, then emits a transformed `.int.c` file where device code is suppressed and host-side stubs replace kernel launch sites. The entire binary is a single-threaded, single-pass-per-stage pipeline controlled from `main()` at `0x408950`.

## Pipeline Diagram

```
  input.cu
     |
     v
 [1] fe_pre_init          sub_585D60   fe_init.c
     9 subsystem pre-initializers
     |
     v
     * sub_5AF350(v7) ---- capture "Total compilation time" start
     |
     v
 [2] proc_command_line     sub_459630   cmd_line.c
     276 CLI flags parsed, mode selection
     |
     v
 [3] fe_one_time_init      sub_585DB0   fe_init.c
     38 subsystem initializers + keyword registration
     |--- fe_init_part_1 (sub_585EE0): per-unit inits, output file open
     |--- keyword_init + fe_translation_unit_init (sub_5863A0)
     |
     v
     * sub_5AF350(v8) ---- capture "Front end time" start
     |
     v
 [4] reset_tu_state        sub_7A4860   trans_unit.c
     Zero all TU globals
     |
     v
 [5] process_trans_unit    sub_7A40A0   trans_unit.c
     Allocate 424-byte TU descriptor, parse source,
     build EDG IL tree, CUDA attribute propagation
     |
     v
 [6] fe_wrapup             sub_588F90   fe_wrapup.c
     5-pass IL finalization: needed-flags, keep-in-IL marking,
     dead entity elimination, scope cleanup
     |
     v
     * sub_5AF350(v9) ---- capture "Front end time" end
     * sub_5AF390("Front end time", v8, v9)
     |
     v
     * sub_5AF350(v10) --- capture "Back end time" start
     |
     v
 [7] Backend entry         sub_489000   cp_gen_be.c
     Walk source sequence, emit .int.c, device stubs,
     lambda wrappers, registration tables
     |
     v
     * sub_5AF350(v11) --- capture "Back end time" end
     * sub_5AF390("Back end time", v10, v11)
     |
     v
     * sub_5AF350(v12) --- capture "Total compilation time" end
     * sub_5AF390("Total compilation time", v7, v12)
     |
     v
 [8] exit_with_status      sub_5AF1D0   host_envir.c
     Map internal status to exit code, terminate

     |----- "Front end time" covers stages 4-6 ----------|
     |----- "Back end time" covers stage 7 ---------------|
     |----- "Total compilation time" covers stages 2-8 ---|
```

## Call Hierarchy from main()

The decompiled `main()` at `0x408950` calls the pipeline stages in this exact order:

```c
void main(int argc, char **argv, char **envp)
{
    sub_585D60(argc, argv, envp);      // [1] fe_pre_init
    sub_5AF350(v7);                     //     capture_time (total start)
    sub_459630(argc, argv);             // [2] proc_command_line
    // [stack limit adjustment via setrlimit]
    sub_585DB0();                       // [3] fe_one_time_init
    if (dword_106C0A4)
        sub_5AF350(v8);                 //     capture_time (frontend start)
    sub_7A4860();                       // [4] reset_tu_state
    sub_7A40A0(qword_126EEE0);         // [5] process_translation_unit
    sub_588F90(v5, 1);                  // [6] fe_wrapup
    if (dword_106C0A4) {
        sub_5AF350(v9);
        sub_5AF390("Front end time", v8, v9);
    }
    // --- error-recovery re-compilation loop ---
    if (qword_126ED90) {               //     errors present?
        dword_106C254 = 1;             //     skip backend
    }
    while (1) {
        sub_6B8B20(0);                  //     reset file state
        sub_589530();                   //     write signoff + cleanup
        // exit code computation
        if (dword_106C0A4)
            sub_5AF390("Total compilation time", ...);
        sub_5AF1D0(exit_code);          // [8] exit
        // --- if dword_106C254 == 0, backend runs ---
        if (!dword_106C254) {
            if (dword_106C0A4)
                sub_5AF350(v10);        //     capture_time (backend start)
            sub_489000();               // [7] process_file_scope_entities
            if (dword_106C0A4) {
                sub_5AF350(v11);
                sub_5AF390("Back end time", v10, v11);
            }
        }
    }
}
```

The `while(1)` loop with `sub_5AF1D0` (which calls `exit()` / `abort()`) never actually iterates -- the call to `sub_5AF1D0` is `__noreturn`. The compiler just arranged the basic blocks this way: the backend stage at label `LABEL_16` falls through from a `goto` at the top of the loop when `dword_106C254 == 0` (no errors).

## Stage Details

### Stage 1: fe_pre_init -- `sub_585D60` (0x585D60)

**Source:** `fe_init.c`

Performs absolute minimum initialization before anything else can run. Called with the raw `argc`, `argv`, `envp` from the OS.

| Call | Address | Identity | Purpose |
|------|---------|----------|---------|
| 1 | `sub_48B3C0` | error_handling_init | Zero error counters |
| 2 | `sub_6BB290` | source_file_mgr_init | File descriptor table setup |
| 3 | `sub_5B1E70` | scope_symbol_pre_init | Scope stack index = -1 |
| 4 | `sub_752C90` | type_system_pre_init | Type table allocation |
| 5 | `sub_45EB40` | cmd_line_pre_init | Register CLI flag table |
| 6 | `sub_4ED530` | declaration_pre_init | Declaration state zeroing |
| 7 | `sub_6F6020` | il_pre_init | IL node allocator setup |
| 8 | `sub_7A48B0` | tu_tracking_pre_init | Zero all TU globals |
| 9 | `sub_7C00F0` | template_pre_init | Template engine state |

Sets `dword_126C5E4 = -1` (current scope index = "none") and `dword_126C5C8 = -1` (secondary scope index = "none").

**Data flow:** No input beyond process args. Output: global state zeroed and ready for CLI parsing.

### Stage 2: proc_command_line -- `sub_459630` (0x459630)

**Source:** `cmd_line.c` (4105 decompiled lines)

Parses all 276 CLI flags. Populates global configuration variables that control every subsequent stage. Key outputs:

| Global | Address | Meaning |
|--------|---------|---------|
| `dword_126EFB4` | `0x126EFB4` | Language mode: 1=K&R C, 2=C++ |
| `dword_126EF68` | `0x126EF68` | C++ standard version (`__cplusplus` value) |
| `dword_106C0A4` | `0x106C0A4` | Timing enabled (print stage durations) |
| `dword_126E1D8` | `0x126E1D8` | MSVC host compiler |
| `dword_126E1F8` | `0x126E1F8` | GNU/GCC host compiler |
| `dword_126E1E8` | `0x126E1E8` | Clang host compiler |
| `dword_106BF38` | `0x106BF38` | Extended lambda mode |
| `qword_126EEE0` | `0x126EEE0` | Output filename (or `"-"` for stdout) |
| `qword_106BA00` | `0x106BA00` | Primary source filename |
| `dword_106C29C` | `0x106C29C` | Preprocessing-only mode |
| `dword_106C064` | `0x106C064` | Stack limit adjustment flag |

The parser builds four hash tables for macro defines (`qword_106C248`), include paths (`qword_106C240`), and system includes (`qword_106C238`, `qword_106C228`). It also suppresses a default set of diagnostic numbers (1257, 1373, 1374, 1375, 1633, 2330, 111, 185, 175).

**Data flow:** Input: `argv`. Output: ~150+ global configuration variables populated.

### Stage 3: fe_one_time_init -- `sub_585DB0` (0x585DB0)

**Source:** `fe_init.c`

The heaviest initialization stage. Calls 38 subsystem initializers in dependency order, then validates the function pointer dispatch table (a sentinel check: `off_D560C0` must equal the address of `nullsub_6`). After validation, calls `sub_585EE0` (fe_init_part_1) which:

1. Records compilation timestamp via `time()`/`ctime()` into `byte_106B5C0`
2. Runs 26 per-compilation-unit initializers
3. Opens the output file (`qword_106C280` = stdout or file)
4. Writes the output file header via `sub_5AEDB0`
5. Calls the keyword registration function `sub_5863A0` which registers 200+ C/C++ keywords plus NVIDIA CUDA-specific type traits (`__nv_is_extended_device_lambda_closure_type`, etc.)

**38 subsystem initializers** (in call order):

| # | Address | Subsystem |
|---|---------|-----------|
| 1 | `sub_752DF0` | types |
| 2 | `sub_5B1D40` | scopes |
| 3 | `sub_447430` | errors |
| 4 | `sub_4B37F0` | preprocessor |
| 5 | `sub_4E8ED0` | declarations |
| 6 | `sub_4C0840` | attributes |
| 7 | `sub_4A1B60` | names |
| 8 | `sub_4E9CF0` | declarations (part 2) |
| 9 | `sub_4ED710` | declarations (part 3) |
| 10 | `sub_510C30` | statements |
| 11 | `sub_56DC90` | expression utilities |
| 12 | `sub_5A5160` | expressions |
| 13 | `sub_603B00` | parser |
| 14 | `sub_5CF7F0` | classes |
| 15 | `sub_65DC50` | overload resolution |
| 16 | `sub_69C8B0` | templates |
| 17 | `sub_665A00` | template instantiation |
| 18 | `sub_689550` | exception handling |
| 19 | `sub_68F640` | implicit conversions |
| 20 | `sub_6B6510` | IL |
| 21 | `sub_6BAE70` | source file manager |
| 22 | `sub_6F5FC0` | IL walking |
| 23 | `sub_6F8300` | IL (part 2) |
| 24 | `sub_6FDFF0` | lowering |
| 25 | `sub_726DC0` | name mangling |
| 26 | `sub_72D410` | name mangling (part 2) |
| 27 | `sub_74B9A0` | type checking |
| 28 | `sub_710B70` | IL (part 3) |
| 29 | `sub_76D630` | code generation |
| 30 | `nullsub_11` | debug (no-op) |
| 31 | `sub_7A4690` | allocation |
| 32 | `sub_7A3920` | memory pools |
| 33 | `sub_6A0E90` | templates (part 2) |
| 34 | `sub_418F80` | diagnostics |
| 35 | `sub_5859C0` | extended asm |
| 36 | `sub_751540` | types (part 2) |
| 37 | `sub_7C25F0` | templates (part 3) |
| 38 | `sub_7DF400` | CUDA-specific init |

**Data flow:** Input: populated config globals. Output: all subsystems initialized, keyword table built, output file open.

### Stage 4: reset_tu_state -- `sub_7A4860` (0x7A4860)

**Source:** `trans_unit.c`

Zeroes all translation unit tracking globals to prepare for processing:

```c
qword_106BA10 = 0;   // current_translation_unit
qword_106B9F0 = 0;   // primary_translation_unit
qword_12C7A90 = 0;   // tu_chain_tail
dword_106B9F8 = 0;   // has_module_info
qword_106BA18 = 0;   // tu_stack_top
dword_106B9E8 = 0;   // tu_stack_depth
```

**Data flow:** No input. Output: TU state clean-slated.

### Stage 5: process_translation_unit -- `sub_7A40A0` (0x7A40A0)

**Source:** `trans_unit.c`

The main frontend workhorse. This single call parses the entire `.cu` source file into the EDG intermediate language. Workflow:

1. Debug trace: `"Processing translation unit %s"`
2. Clean up any previous TU state (`sub_7A3A50`)
3. Reset error state (`sub_5EAEC0`)
4. Allocate 424-byte TU descriptor via `sub_6BA0D0`
5. Initialize TU scope state (offsets 24..192 via `sub_7046E0`)
6. Set as primary TU (`qword_106B9F0`) if first
7. Link into TU chain
8. Call `sub_586240` -- parse the source file (this enters the EDG parser, which handles all of C++ plus CUDA extensions: `__device__`, `__host__`, `__global__`, `__shared__`, `__managed__`, etc.)
9. Depending on mode:
   - Module compilation: `sub_6FDDF0`
   - Standard compilation: `sub_6F4AD0` (header-unit) + `sub_4E8A60` (standard)
10. Post-processing: `sub_588E90` (translation_unit_wrapup -- scope closure, template wrapup, IL output)
11. Debug trace: `"Done processing translation unit %s"`

At the end of this stage, the EDG IL tree is fully built. Every declaration, type, expression, and statement from the source has been parsed into IL nodes. CUDA execution-space attributes (`__device__`, `__host__`, `__global__`) have been recorded on entity nodes at byte offset +182 (bit 6 = device/global, bits 4-5 = execution space).

**Data flow:** Input: source filename from `qword_126EEE0`. Output: complete EDG IL tree anchored at `qword_106BA10` (TU descriptor), source sequence list at `*(qword_106BA10 + 8)`.

### Stage 6: fe_wrapup -- `sub_588F90` (0x588F90)

**Source:** `fe_wrapup.c`

Five-pass finalization over all translation units. Each pass iterates the TU chain (`qword_106B9F0`). Passes 2-4 are per-TU error-gated (skip TUs with `qword_126ED90 != 0`); passes 1 and 5 run unconditionally.

| Pass | Function | Purpose | Error-gated? |
|------|----------|---------|--------------|
| 1 | `sub_588C60` | Per-file IL wrapup: template/exception cleanup, IL tree walk (`sub_706710`), IL finalize (`sub_706F40`), destroy temporaries | No |
| 2 | `sub_707040` | Needed-flags computation: determine which entities must be preserved for backend consumption | Per-TU skip |
| 3 | `sub_610420(23)` | Keep-in-IL marking: mark entities for device code preservation with guard flag `dword_106B640` | Per-TU skip |
| 4 | `sub_5CCA40` + `sub_5CC410` + `sub_5CCBF0` | Dead entity elimination (C++ gate on `sub_5CCA40`): clear unneeded instantiation flags, remove dead function bodies, remove unneeded IL entries | Per-TU skip |
| 5 | `sub_588D40` | Statement finalization, scope assertions, IL output + template output | No |

Between Pass 1 and Pass 2, if no errors have occurred, `sub_796C00` runs cross-TU entity marking.

Post-pass operations:
- Cross-TU consistency (`sub_796BA0`, error-gated)
- Scope renumbering (`sub_707480` double-loop)
- Template validation (`sub_765480`)
- File index cleanup (`sub_6B8B20` for indices 2..`dword_126EC80`)
- Output flush + close three output files (IDs 1513, 1514, 1515)
- Memory statistics: sums 10 `space_used()` callbacks
- State teardown

**Data flow:** Input: fully built IL tree. Output: finalized IL with dead entities eliminated and device-needed entities marked. The source sequence list (`qword_1065748`) is the ordered list of top-level declarations the backend will walk.

### Stage 7: Backend Code Generation -- `sub_489000` (0x489000)

**Source:** `cp_gen_be.c` (723 decompiled lines, the largest single function in the backend)

This is the host-side C++ code generator. It walks the EDG source sequence and emits the `.int.c` file that the host compiler (gcc/cl.exe/clang) will compile. The backend is gated by `dword_106C254`: if set to 1 (errors occurred), stage 7 is skipped entirely.

Initialization:
1. Zeros output state: `dword_1065834` (indent level), stream handle, counters
2. Clears four 512KB hash tables (`memset 0x7FFE0` bytes each)
3. Sets up gen_be_info callback table (`xmmword_1065760..10657B0`)
4. Creates output file: `<input>.int.c` (or stdout for `"-"`)

Boilerplate emission:
- `#pragma GCC diagnostic` push/pop blocks for suppressing host compiler warnings
- `__nv_managed_rt` initialization boilerplate (for `__managed__` variables)
- Lambda type-trait macro definitions

Main processing loop:
- Walks `qword_1065748` (global source sequence list)
- For each entry: dispatches to `sub_47ECC0` (gen_template/process_source_sequence)
- Kind 57 entries are pragma interleavings (handled inline)

CUDA-specific transformations performed:
1. **Device stub generation:** For `__global__` kernels, emit `__wrapper__device_stub_<name>()` forwarding, wrap original body in `#if 0`/`#endif`
2. **Device-only suppression:** Device-only declarations wrapped in `#if 0`/`#endif`
3. **Lambda wrappers:** `__nv_dl_wrapper_t<>` for device lambdas, `__nv_hdl_create_wrapper_t<>` for host-device lambdas
4. **Runtime header injection:** `#include "crt/host_runtime.h"` at first CUDA entity
5. **Registration tables:** `sub_6BCF80` called 6 times for device/host/managed/constant combinations
6. **Anonymous namespace:** `_NV_ANON_NAMESPACE` macro for unique global symbols

Trailer:
- Empty-file guard: `int __dummy_to_avoid_empty_file;`
- Re-inclusion of original source via `#include "<original_file>"`
- `#undef _NV_ANON_NAMESPACE`

**Data flow:** Input: finalized source sequence from stage 6. Output: `.int.c` file on disk.

### Stage 8: exit_with_status -- `sub_5AF1D0` (0x5AF1D0)

**Source:** `host_envir.c`

Maps internal compilation status to process exit codes:

| Internal Status | Meaning | Exit Code | Action |
|----------------|---------|-----------|--------|
| 3, 4, 5 | Success | 0 | `exit(0)` |
| 8 | Warnings only | 2 | `exit(2)` |
| 9, 10 | Errors | 4 | `exit(4)` + `"Compilation terminated."` |
| 11 | Internal error | -- | `abort()` + `"Compilation aborted."` |

In SARIF mode (`dword_106BBB8`), text messages are suppressed but exit codes remain the same.

## Key Global Variables Controlling Flow

| Variable | Address | Type | Role |
|----------|---------|------|------|
| `dword_106C254` | `0x106C254` | int | Skip-backend flag. Set to 1 when `qword_126ED90` (error count) is nonzero after frontend. Prevents stage 7 from running. |
| `dword_106C0A4` | `0x106C0A4` | int | Timing flag. When set, `sub_5AF350`/`sub_5AF390` bracket each phase with CPU + wall-clock timestamps. |
| `dword_126EFB4` | `0x126EFB4` | int | Language mode. 1=K&R C, 2=C++. Controls C++ class finalization in pass 4 of `fe_wrapup`, keyword set selection, and backend behavior. In CUDA mode, always 2. |
| `qword_126ED90` | `0x126ED90` | qword | Error count. Checked after stages 5-6 to decide whether to run backend. Nonzero skips needed-flags, keep-in-IL marking, and dead entity elimination passes in fe_wrapup. |
| `qword_126EEE0` | `0x126EEE0` | char* | Output filename. Passed to `sub_7A40A0` for TU naming. Used by backend to construct `.int.c` path. |
| `dword_1065850` | `0x1065850` | int | Device stub mode. Toggled during backend generation: 1 = currently emitting device stub code (changes parameter types, suppresses bodies). |
| `dword_106C064` | `0x106C064` | int | Stack limit flag. When set, main adjusts RLIMIT_STACK to max before entering frontend (deep recursion in parser/template engine). |

## Timing Regions

When `dword_106C0A4` is set (via `--timing` or equivalent flag), three timing regions are printed:

```
Front end time                     12.34 (CPU)     15.67 (elapsed)
Back end time                       3.45 (CPU)      4.56 (elapsed)
Total compilation time             15.79 (CPU)     20.23 (elapsed)
```

Format string: `"%-30s %10.2f (CPU) %10.2f (elapsed)\n"`

The timing is implemented via `sub_5AF350` (capture_time: records `clock()` as CPU milliseconds and `time()` as wall seconds) and `sub_5AF390` (report_timing: computes deltas and prints).

| Region | Start | End | Covers |
|--------|-------|-----|--------|
| Front end | After `sub_585DB0` (fe_one_time_init) | After `sub_588F90` (fe_wrapup) | Stages 4-6: TU reset, parse, IL build, wrapup |
| Back end | Before `sub_489000` | After `sub_489000` | Stage 7: .int.c generation |
| Total | After `sub_585D60` (fe_pre_init), before `sub_459630` (CLI) | Before `sub_5AF1D0` (exit) | Stages 2-8: CLI parsing through exit |

## Error Recovery Loop

The `main()` function contains a `while(1)` loop that appears to support re-compilation (the TU processing infrastructure has a `dword_106BA08` "is_recompilation" flag and `sub_7A40A0` checks an `a2` recompilation parameter). In practice, for the standard CUDA compilation flow, this loop executes exactly once: `sub_5AF1D0` is `__noreturn` and terminates the process.

The loop body:
1. `sub_6B8B20(0)` -- reset file state for the source file manager
2. `sub_589530()` -- write output signoff (`sub_5AEE00`) + close source manager (`sub_6B8DE0`)
3. Compute exit code from `qword_126ED90` (errors) and `qword_126ED88` (additional status)
4. Print total timing if enabled
5. Restore stack limit if it was raised
6. `sub_5AF1D0(exit_code)` -- terminate

## Cross-References

- [Entry Point & Initialization](./entry.md) -- detailed breakdown of stages 1-3
- [CLI Processing](./cli.md) -- all 276 flags parsed in stage 2
- [Frontend Invocation](./frontend.md) -- stage 5 (parse + IL build) in depth
- [Frontend Wrapup](./fe-wrapup.md) -- 5-pass architecture of stage 6
- [Backend Code Generation](./backend.md) -- stage 7 (`.int.c` emission) in depth
- [Timing & Exit](./timing-exit.md) -- stage 8 and exit code mapping
- [Device/Host Separation](../cuda/device-host-separation.md) -- how the backend filters device vs host code
- [Kernel Stub Generation](../cuda/kernel-stubs.md) -- `__wrapper__device_stub_` pattern
- [Extended Lambda Overview](../lambda/overview.md) -- lambda wrapper generation in backend
- [.int.c File Format](../output/int-c-format.md) -- structure of the backend output
