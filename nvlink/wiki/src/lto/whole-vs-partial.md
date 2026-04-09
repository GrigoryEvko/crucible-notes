# Whole vs Partial LTO

When nvlink performs link-time optimization, it must decide between two fundamentally different compilation strategies: **whole-program** compilation, where all device code is merged into a single NVVM IR module and compiled as one unit; and **partial** (relocatable) compilation, where the LTO-compiled code is emitted as a relocatable object that will be linked conventionally against non-LTO inputs. The decision is driven by a single byte-sized global flag, `byte_2A5F286`, which starts at 0 (whole-program) and is flipped to 1 (partial) when any input object lacks LTO IR. The `--force-whole-lto` and `--force-partial-lto` CLI flags can override this automatic detection, with conflict checking at option-parse time -- but as shown below, `--force-whole-lto` is only effective when every non-LTO input is `libcudadevrt`.

| | |
|---|---|
| **Decision variable** | `byte_2A5F286` at address `0x2A5F286` (1 byte). 0 = whole-program, 1 = partial/relocatable |
| **Force-whole flag** | `byte_2A5F284` -- set by `--force-whole-lto` |
| **Force-partial flag** | `byte_2A5F285` -- set by `--force-partial-lto` (also auto-set by `register_module`) |
| **Whole-program compiler** | `sub_4BD4E0` (`ptxas_whole_program`) at `0x4BD4E0` |
| **Relocatable compiler** | `sub_4BD760` (`ptxas_compile`) at `0x4BD760` |
| **IR collector** | `sub_426CD0` (`lto_collect_ir`) at `0x426CD0` |
| **NVVM compile wrapper** | `sub_4BC6F0` (`nvvm_compile_and_extract`) at `0x4BC6F0` |
| **Module registrar** | `sub_42A680` (`register_module`) at `0x42A680` |
| **Option parser (flag extraction + conflict check)** | `sub_427AE0` at `0x427AE0` |
| **Main dispatch (whole/partial branch)** | `main` at `0x409800`, lines 1155--1202 |

## Mode Decision Matrix

The following matrix captures every documented path from user input and flag state to the final value of `byte_2A5F286` (the whole-vs-partial decision variable). Rows are tested in the order shown; the first matching row wins. The **Effective mode** column assumes no error is raised; the **Source** column gives the exact decompiled location where the decision is made.

| # | CLI flags | Input composition | Parse-time result | Runtime decision | Effective mode | Source |
|---|-----------|-------------------|-------------------|------------------|----------------|--------|
| 1 | no `-lto` | any | `byte_2A5F286=0` (unused) | -- | **No LTO** (pipeline skipped) | `sub_427AE0` does not enter the `byte_2A5F288` branch |
| 2 | `-lto --force-partial-lto --force-whole-lto` | any | **Error** -- mutual conflict | -- | -- | `sub_427AE0` line 1194--1202 emits `-force-partial-lto vs -force-whole-lto` conflict |
| 3 | `--force-partial-lto` **without** `-dlto`/`-lto` | any | **Error** -- requires `-dlto` | -- | -- | `sub_427AE0` line 1231--1232 |
| 4 | `--force-whole-lto` **without** `-dlto`/`-lto` | any | **Error** -- requires `-dlto` | -- | -- | `sub_427AE0` line 1233--1234 |
| 5 | `-lto -r` (`--relocatable-link`) | any | `byte_2A5F285=1` forced | register\_module + dispatch pick partial | **Partial** (forced) | `sub_427AE0` line 1151--1153: `if (byte_2A5F1E8) byte_2A5F285 = 1;` |
| 6 | `-lto --force-partial-lto` | any | `byte_2A5F285=1`, flows to `LABEL_71` | `byte_2A5F286=1` at parse time | **Partial** (explicit) | `sub_427AE0` line 1209 sets `byte_2A5F286 = 1` |
| 7 | `-lto --force-whole-lto` | all inputs have LTO IR | `byte_2A5F284=1`, `byte_2A5F285=0` | nvvmCompileProgram returns 100 -> `byte_2A5F286=0`; also main line 1074 override | **Whole** (redundant force) | `sub_4BC6F0` line 393--395 and `main` line 1073--1074 |
| 8 | `-lto --force-whole-lto` | some inputs are **native cubins**, all of them `libcudadevrt` | `byte_2A5F284=1`, `byte_2A5F285` unchanged; `register_module` sets `byte_2A5F286=1` but not `byte_2A5F285` (cudadevrt exception) | main line 1070 test `!byte_2A5F285 && dword_2A5B514==1` succeeds -> `byte_2A5F286=0` forced | **Whole** (override wins; cudadevrt is stripped) | `main` line 1073--1074, then line 1346--1366 removes cudadevrt |
| 9 | `-lto --force-whole-lto` | some inputs are **native cubins**, at least one is NOT `libcudadevrt` | `byte_2A5F284=1`; `register_module` sets both `byte_2A5F286=1` and `byte_2A5F285=1` + warning | main line 1070 test `!byte_2A5F285` fails -> override skipped | **Partial** (force-whole silently ineffective) | `sub_42A680` line 485--493; `main` line 1070 guard fails |
| 10 | `-lto` only, no force flags | all inputs have LTO IR | defaults: `byte_2A5F286=0` | `sub_4BC6F0` -> nvvmCompileProgram returns 100 -> `*a5=0` | **Whole** (auto) | `sub_4BC6F0` line 393--395 |
| 11 | `-lto` only, no force flags | all inputs have LTO IR, but nvvm splits the IR into multiple modules | defaults: `byte_2A5F286=0` | `sub_4BC6F0` -> nvvmCompileProgram returns 0 -> `*a5=1` | **Partial** (nvvm decided to split) | `sub_4BC6F0` line 405--410 |
| 12 | `-lto` only | some inputs are native cubins, all of them `libcudadevrt` | defaults | `register_module` sets `byte_2A5F286=1`, `byte_2A5F285` stays 0, no warning | **Partial** (cudadevrt-only silent) | `sub_42A680` line 485--488 |
| 13 | `-lto` only | some inputs are native cubins, at least one not `libcudadevrt` | defaults | `register_module` sets `byte_2A5F286=1` and `byte_2A5F285=1`, emits warning | **Partial** (auto with warning) | `sub_42A680` line 485--493 |
| 14 | `-lto --emit-ptx --force-partial-lto` | any | `byte_2A5F286=1`, enters `LABEL_66` (split-compat check) | -- | **Partial** (with split-compile compatibility validation) | `sub_427AE0` line 1206--1225 |

### Simplified User-Facing Matrix

The original task-requested matrix, after collapsing the runtime details:

| Condition | Mode Selected | Reason |
|-----------|---------------|--------|
| All inputs are LTO IR, no force flags | **Whole** | All functions eligible; `nvvmCompileProgram` returns 100, `byte_2A5F286=0` |
| Some inputs are SASS cubins (not cudadevrt) | **Partial** (auto) | Cannot re-optimize SASS; `register_module` sets `byte_2A5F286=1` + warning |
| `--force-partial-lto` | **Partial** | Forced by user at parse time (`sub_427AE0` line 1209) |
| `-r` / `--relocatable-link` + `-lto` | **Partial** | Relocatable link implies `--force-partial-lto` (`sub_427AE0` line 1151--1153) |
| `--force-whole-lto`, all non-LTO inputs are cudadevrt | **Whole** | Override fires, cudadevrt stripped from module list |
| `--force-whole-lto`, non-cudadevrt native cubin present | **Partial** (silently) | `register_module` sets `byte_2A5F285`, which blocks the main-line-1074 override |
| `--force-partial-lto` + `--force-whole-lto` | **Error** | Mutual conflict |

The non-obvious case is row 9 / the last silent-fallback row: **`--force-whole-lto` is not a hard override.** It only wins when `register_module` hasn't also forced `byte_2A5F285=1` on a non-cudadevrt input. Because the CLI conflict check in `sub_427AE0` runs at parse time but `register_module` runs during the later input loop, this combination never triggers a diagnostic -- the partial-mode decision just silently sticks.

## CLI Flags That Control the Mode

| Flag | Short | Type | Global | Behavior |
|---|---|---|---|---|
| `--link-time-opt` | `-lto` | bool | `byte_2A5F288` | Master LTO enable. Required for any mode decision to be meaningful. Implied by `--dlto` (`sub_427AE0` line 1075--1076) |
| `--dlto` | -- | bool | `byte_2A5F287` | Distributed LTO mode. Sets `byte_2A5F288` as a side effect at line 1076 |
| `--force-whole-lto` | -- | bool (hidden) | `byte_2A5F284` | Requests whole-program mode. Help text: `"force doing whole program LTO when -dlto"`. Only effective if `!byte_2A5F285` (see row 8 vs row 9 above) |
| `--force-partial-lto` | -- | bool (hidden) | `byte_2A5F285` | Requests partial/relocatable mode. Help text: `"force doing partial LTO when -dlto"`. Also auto-set by `register_module` and by `-r` |
| `--relocatable-link` | `-r` | bool | `byte_2A5F1E8` | Generate relocatable object. When combined with `-lto`, implicitly forces `byte_2A5F285=1` (partial) |
| `--emit-ptx` | -- | bool | `byte_2A5F29A` | Emit intermediate PTX. Under `-lto`, triggers the `LABEL_66` split-compile compatibility check |
| `--nvvmpath` | -- | string | `qword_2A5F278` | Path to `libnvvm.so`. Required when `-lto` is active (`"-nvvmpath should be specified with -lto"` at line 1146) |
| `--split-compile-extended` | -- | int | `dword_2A5B514` | Per-module parallel ptxas. Interacts with partial mode to enable the thread-pool path (see [Split Compilation](split-compilation.md)) |

**Visibility flag.** Both `--force-whole-lto` and `--force-partial-lto` are registered with flag value 4 (hidden from `--help`). They exist primarily for CUDA-toolchain internal use and debug workflows. The public expectation is that `nvcc` selects the correct mode automatically based on the object mix it produces.

**Parse-time flag extraction** (`sub_427AE0` lines 979--982):

```c
sub_42E390(parser, "link-time-opt",     &byte_2A5F288, 1);
sub_42E390(parser, "dlto",              &byte_2A5F287, 1);
sub_42E390(parser, "force-partial-lto", &byte_2A5F285, 1);
sub_42E390(parser, "force-whole-lto",   &byte_2A5F284, 1);
```

## The Decision Variable: byte_2A5F286

`byte_2A5F286` is the central control for the whole-vs-partial decision. Its lifecycle:

1. **Initialization**: defaults to 0 (whole-program assumed).
2. **Option parsing** (`sub_427AE0`): if `--force-partial-lto` is active and no conflict, the flag is set to 1 at line 1209.
3. **Input registration** (`sub_42A680`): when a non-LTO object is encountered during the input loop, the flag is set to 1.
4. **NVVM compilation** (`sub_4BC6F0`): the flag is passed by pointer as parameter `a5`. The nvvm return code can modify it.
5. **Post-NVVM override** (main, line 1073): if `--force-whole-lto` (`byte_2A5F284`) is active *and* `byte_2A5F285` is still 0, the flag is forcibly cleared to 0.
6. **Dispatch** (main, lines 1155--1202): the flag's value determines which compilation backend is invoked.

## Automatic Detection in register_module

The most common way `byte_2A5F286` gets set is through `sub_42A680` (`register_module`), called for every input object during the input file loop. The relevant logic at lines 481--496:

```c
// Inside sub_42A680 (register_module)
// a2 = filename, a3 = cubin_data (non-NULL if this is a native cubin, not LTO IR)
if (byte_2A5F288) {              // LTO is enabled
    if (a3) {                    // this object has native code, NOT LTO IR
        byte_2A5F286 = 1;       // switch to partial mode
        if (!strstr(a2, "cudadevrt")) {
            byte_2A5F285 = 1;   // also set force-partial flag
            snprintf(buf, len,
                "requested LTO but '%s' not built for LTO so doing partial LTO",
                a2);
            warning(buf);        // sub_467460 with warning severity
        }
    }
}
```

When LTO is active (`byte_2A5F288 == 1`) and `register_module` receives an input that already has compiled cubin data (parameter `a3` is non-NULL), the object was not compiled with `-dc` / device-code separation and therefore has no LTO IR. The linker:

1. Sets `byte_2A5F286 = 1` to switch to partial mode.
2. Checks whether the object is `libcudadevrt` (via `strstr(filename, "cudadevrt")`). If it is NOT cudadevrt, also sets `byte_2A5F285 = 1` (the force-partial flag) and emits a warning message. The cudadevrt exception exists because cudadevrt is always a native archive and is expected to lack LTO IR -- its presence alone should not trigger a partial-mode warning, and should not disable the subsequent `--force-whole-lto` override.

The cudadevrt vs non-cudadevrt distinction is the only reason `--force-whole-lto` can still have an effect after partial mode has been auto-selected: a cudadevrt-only partial trigger leaves `byte_2A5F285=0`, satisfying the override's guard at main line 1070.

## Option Parsing: --force-whole-lto and --force-partial-lto

Both flags are registered in `sub_427AE0` as type-1 (bool) options with hidden visibility (flag 4):

```c
// sub_427AE0, lines 536-559
option_register(parser, "force-partial-lto", "force-partial-lto",
    type=1, multiplicity=0, flags=4,
    help="force doing partial LTO when -dlto");

option_register(parser, "force-whole-lto", "force-whole-lto",
    type=1, multiplicity=0, flags=4,
    help="force doing whole program LTO when -dlto");
```

The flags are extracted into their globals:

```c
option_get_value(parser, "force-partial-lto", &byte_2A5F285, 1);  // line 981
option_get_value(parser, "force-whole-lto",   &byte_2A5F284, 1);  // line 982
```

### Conflict Detection

Option parsing validates flag combinations with several checks:

**1. Mutual exclusion of force flags** (lines 1194--1204): If both `--force-partial-lto` and `--force-whole-lto` are specified together with `-dlto`, nvlink emits an error via `sub_467460` with the `unk_2A5B650` severity (conflict error):

```c
if (byte_2A5F285) {               // --force-partial-lto is set
    if (byte_2A5F284) {           // --force-whole-lto also set
        error("-force-partial-lto", "-force-whole-lto");  // conflict
    }
}
```

**2. Requires -dlto** (lines 1231--1234): Both `--force-partial-lto` and `--force-whole-lto` require `-dlto` mode. Without it, each triggers a separate error:

```c
if (!byte_2A5F287) {              // no -dlto
    if (byte_2A5F285)
        error("-force-partial-lto", "no -dlto");
    if (byte_2A5F284)
        error("-force-whole-lto", "no -dlto");
}
```

**3. Relocatable link implies partial** (line 1151--1153): When `--relocatable-link` / `-r` (`byte_2A5F1E8`) is active with LTO, partial mode is forced unconditionally:

```c
if (byte_2A5F288) {               // -lto active
    if (byte_2A5F1E8) {           // -r active
        byte_2A5F285 = 1;         // force partial
    }
}
```

**4. --force-partial-lto with -emit-ptx** (lines 1206--1225): If `--force-partial-lto` is active (or is about to be set), and `--emit-ptx` is also active, the code takes the `LABEL_66` path which validates split-compile compatibility: `-split-compile-extended` must be 1, otherwise a warning is emitted and `dword_2A5B514` is demoted to 1 (its previous value is migrated to `dword_2A5B518`).

### Option Validation Summary

| Combination | Result |
|---|---|
| `--force-partial-lto` alone (no `-dlto`) | Error: requires `-dlto` |
| `--force-whole-lto` alone (no `-dlto`) | Error: requires `-dlto` |
| `--force-partial-lto` + `--force-whole-lto` | Error: mutual conflict |
| `-r` + `-lto` | Implicit `--force-partial-lto` (no error) |
| `--force-partial-lto` + `-dlto` | Valid: forces partial mode at parse time |
| `--force-whole-lto` + `-dlto` | Valid: forces whole mode *only if* no non-cudadevrt native input appears later |

## NVVM Compilation: How byte_2A5F286 Flows Through sub_4BC6F0

`sub_4BC6F0` (`nvvm_compile_and_extract`) receives `byte_2A5F286` by pointer as its 5th parameter (`a5`). The function:

1. Resolves all required libnvvm API symbols via `dlsym` from the loaded `libnvvm.so` handle at `a7 + 640`:
   - `nvvmCompileProgram`
   - `nvvmGetCompiledResultSize`
   - `nvvmGetCompiledResult`
   - `nvvmGetErrorString`
   - `nvvmGetProgramLogSize`
   - `nvvmGetProgramLog`
   - `nvvmDestroyProgram`
   - `__nvvmHandle` (NVIDIA-internal callback registration)

2. Builds the option array. Scans the provided option strings for `--force-device-c` -- if present, sets a flag (`v25 = 1`). If absent AND the linker context byte at `a7 + 97` is set, appends host-reference export/import keys (`-host-ref-ek=`, `-host-ref-ik=`, `-host-ref-ec=`, `-host-ref-ic=`, `-host-ref-eg=`, `-host-ref-ig=`).

3. Calls `nvvmCompileProgram` with the assembled options.

4. Interprets the return code:

```c
v92 = nvvmCompileProgram(program_handle, option_count, options, ...);

if (v92 == 100) {
    *a5 = 0;    // byte_2A5F286 = 0: whole-program succeeded (no split output)
    // No compiled result to extract
}
else if (v92 != 0) {
    // Error path: retrieve error string via nvvmGetErrorString(v92)
    *error_msg = error_string;
}
else {
    // v92 == 0: success
    *a5 = 1;    // byte_2A5F286 = 1: compilation produced split modules
    // Proceeds to extract compiled result and split-module size array
}
```

Return code 100 from `nvvmCompileProgram` is a special NVIDIA-internal code meaning "whole-program consolidation succeeded: all IR was merged and compiled as a single unit, no split modules produced." The flag is cleared to 0 (whole-program).

Return code 0 is the standard success. In the LTO context, this means the compilation produced one or more split modules. The flag is set to 1 (partial). The function then extracts the compiled PTX result(s) and the per-module size array.

## Post-NVVM Override: --force-whole-lto

After `sub_4BC6F0` returns, and after extracting split-module data, the code checks for the `--force-whole-lto` override:

```c
// main, lines 1070-1074
if (!byte_2A5F285 && dword_2A5B514 == 1) {
    // Reached LABEL_396:
    if (byte_2A5F284)          // --force-whole-lto
        byte_2A5F286 = 0;     // override to whole-program
}
```

This override applies only when:

- `byte_2A5F285` is 0 (neither `--force-partial-lto` nor an auto-set from non-cudadevrt native input).
- `dword_2A5B514 == 1` (split-compile-extended threading not active in multi-thread mode).

Because `register_module` sets `byte_2A5F285=1` for any non-cudadevrt native input, `--force-whole-lto` silently fails to override partial mode whenever a real non-LTO object (e.g. a `.o` compiled without `-dc`) is in the link. The only scenarios in which `--force-whole-lto` actually wins are:

1. All inputs are LTO IR (override is redundant; nvvm returns 100 anyway).
2. All native-cubin inputs are `libcudadevrt` (override trims the cudadevrt entry and compiles the rest whole-program).

## Dispatch: Whole-Program vs Partial Compilation

After the NVVM IR-to-PTX phase and the force-flag override, `byte_2A5F286` determines which ptxas backend is called:

### Whole-Program Path (byte_2A5F286 == 0)

```c
// main, lines 1155-1178
if (!byte_2A5F286) {
    if (verbose)
        fwrite("whole program compile\n", 1, 0x16, stderr);

    dword_2A5B528 = byte_2A5F225 ? 6 : 0;   // compilation mode
    arch_options = sub_429BA0(...);

    exit_code = sub_4BD4E0(                    // ptxas_whole_program
        &cubin_output,     // output cubin pointer
        ptx_data,          // input PTX
        sm_version,        // dword_2A5F314
        has_half_prec,     // byte_2A5F2C0
        is_64bit,          // dword_2A5F30C == 64
        debug_flag,        // byte_2A5F310
        arch_options,      // from sub_429BA0
        comp_mode          // dword_2A5B528
    );
}
```

`sub_4BD4E0` is the whole-program ptxas backend. It creates a compilation context (`sub_4CDD60`), configures the target architecture, sets 64-bit mode, feeds the PTX, compiles, and extracts the resulting cubin. The whole-program path produces a single, fully-linked cubin that is written directly to the output file. Since all symbols are resolved, no further ELF merging is needed.

After whole-program compilation, if the output had cudadevrt in the module list (lines 1337--1366), it is removed:

```c
if (!byte_2A5F286) {   // whole-program: all code was LTO'd
    if (verbose)
        fwrite("LTO on everything so remove libcudadevrt from list\n",
               1, 0x33, stderr);
    assert(strstr(cudadevrt_module->name, "cudadevrt"));
    // Remove from module list and free
}
```

### Partial Path (byte_2A5F286 == 1)

```c
// main, lines 1180-1202
if (byte_2A5F286) {
    if (verbose)
        fwrite("relocatable compile\n", 1, 0x14, stderr);

    if (dword_2A5B514 == 1) {
        // Single-module partial: use relocatable ptxas
        exit_code = sub_4BD760(             // ptxas_compile (relocatable)
            &cubin_output,
            ptx_data,
            sm_version,
            has_half_prec,
            is_64bit,
            debug_flag,
            arch_options,
            comp_mode
        );
    } else {
        // Multi-module partial: thread pool split compile
        // Allocate work items, dispatch to thread pool
        for (i = 0; i < module_count; i++) {
            work_item[i] = { &output[i], ptx[i], sm, ... };
            thread_pool_enqueue(pool, sub_4264B0, work_item[i]);
        }
        thread_pool_wait(pool);
        thread_pool_join(pool);

        // Merge each compiled cubin back into the ELF
        for (i = 0; i < module_count; i++) {
            validate_and_add(elfw, cubin[i], "lto.cubin", ...);
            if (sm > 0x59) post_link_transform(...);
            merge_elf(elfw);
        }
    }
}
```

`sub_4BD760` is the relocatable ptxas backend. Unlike `sub_4BD4E0`, it passes additional flags that tell the embedded ptxas to produce a relocatable object (`.o`) rather than a fully-linked cubin. The key difference is the use of `setjmp`/`longjmp` for error recovery -- if compilation fails, the function can recover gracefully (lines 114--152 of `sub_4BD760`).

In partial mode, the compiled cubin is a relocatable ELF that must be merged into the output alongside the non-LTO objects. The merge happens through `sub_45E7D0` (merge\_elf), the same 89KB function used for all input cubins.

### Split-Compile Partial Path

When `dword_2A5B514` (split-compile-extended thread count) is greater than 1 AND `byte_2A5F286` is 1, the linker uses a thread pool for parallel compilation. Each split module gets its own ptxas invocation dispatched through `sub_4264B0`. This is the most complex path, combining partial-mode semantics with split-compilation parallelism. See [Split Compilation](split-compilation.md) for details on the thread pool mechanics.

## Performance Implications

The mode choice has significant impact on compile time, output size, and runtime performance of the generated device code. All three dimensions favor whole-program mode when it is available.

| Dimension | Whole-program (`byte_2A5F286 == 0`) | Partial (`byte_2A5F286 == 1`) |
|---|---|---|
| **IR-level optimization scope** | Cross-module. The inliner, devirtualizer, and global optimizer see every function from every translation unit simultaneously | Per-module. The IR compiler operates on each module in isolation or in small groups; cross-module inlining is limited to what ThinLTO summary imports can replicate |
| **Dead-code elimination** | Done once inside libnvvm with full visibility. Linker-level DCE (`sub_426AE0`) is skipped (guard at main line 1427: `byte_2A5F214 && (!byte_2A5F288 \|\| byte_2A5F285)`) | Done twice: first by libnvvm on each module, then by `sub_426AE0` at merge time to trim unused functions introduced by the non-LTO objects |
| **Output cubin structure** | Single monolithic cubin. No inter-module fixups needed, fewer symbol table entries, smaller file | N relocatable cubins merged via `merge_elf`. Each carries its own symbol/relocation tables before merging |
| **Wall-clock compile time** | Longer per-invocation because the IR compiler sees the full program, but no merge pass needed afterward | Shorter per-module compile, can be parallelized via `--split-compile-extended`; merge pass adds overhead |
| **Register pressure** | Computed globally: the ptxas register allocator can balance across all kernels | Computed per-module: each module gets its own `--maxrregcount` treatment, so hot kernels may spill unnecessarily |
| **Cudadevrt handling** | Stripped from the module list entirely (`"LTO on everything so remove libcudadevrt from list"`) because its runtime helpers have been inlined by the whole-program compile | Retained as a linked-in archive member; runtime helpers remain callable at runtime |
| **`--maxrregcount` forwarding** | Applied once to the merged program | Applied to each module individually; can produce inconsistent results if modules disagree |
| **Symbol visibility changes** | Internal symbols can be promoted to static/hidden aggressively | Internal symbols that cross module boundaries must keep external linkage |
| **Debug info quality** | One DWARF context covers all functions; line-table merging is not needed | Each module contributes its own DWARF; line tables must be merged at link time |
| **Error recovery** | `sub_4BD4E0` has no `setjmp` wrapper -- a ptxas crash terminates the linker | `sub_4BD760` uses `setjmp`/`longjmp` so a single-module ptxas failure is isolated |

**Rule of thumb.** Partial mode is always strictly weaker than whole-program mode on optimization quality, and is slower in total wall-clock time once merge overhead is counted. It exists solely to support mixed builds where not every input was compiled with `-dc`. The linker emits the `"requested LTO but '%s' not built for LTO so doing partial LTO"` warning specifically to flag this performance regression to the user.

## What Happens to Non-LTO Inputs in Whole-LTO Mode

This is worth spelling out because the behavior is not documented in user-facing material and the code paths are not obvious from a flag description.

### There is no "bypass" or error path for ordinary native cubins

When `register_module` (`sub_42A680`) encounters a non-LTO input object under `-lto`, it unconditionally **switches to partial mode** (`byte_2A5F286 = 1`). It does not:

- emit a fatal error,
- skip the non-LTO object,
- attempt to decompile the cubin back to IR,
- or preserve whole-program mode under any default code path.

The only user-visible acknowledgement is the warning `"requested LTO but '%s' not built for LTO so doing partial LTO"`, followed by continued execution in partial mode.

### The three specific cases

| Non-LTO input type | `byte_2A5F286` | `byte_2A5F285` | Warning | Final mode |
|---|---|---|---|---|
| `libcudadevrt` archive member (contains `"cudadevrt"` in filename) | set to 1 | unchanged (stays 0) | none | Partial, or **Whole** if `--force-whole-lto` is also passed (override guard passes) |
| Any other compiled cubin / `.o` without `-dc` | set to 1 | set to 1 | `"requested LTO but '%s' not built for LTO so doing partial LTO"` | **Partial, permanently.** `--force-whole-lto` is silently ignored |
| `.nvvm` / `.ltoir` IR file | unchanged (stays 0 unless already flipped) | unchanged | none | Whole (if everything else is IR) |

### The "should only see nvvm files when -lto" error

There is one hard-error path. In `main` line 767, if nvlink encounters a `.nvvm` file while `-lto` is **not** active, it fatals with `"should only see nvvm files when -lto"`. This is the mirror case: you cannot feed IR inputs to a non-LTO link. But there is no mirror error for the opposite direction -- you *can* feed native cubins to an `-lto` link, you just get partial mode.

### Why the override design is this way

The asymmetry (partial-lto auto-force blocks whole-lto override; whole-lto does not block anything) exists because the linker cannot re-optimize pre-compiled SASS. If a `.o` was built without `-dc`, its functions have already been through the full IR-to-SASS pipeline and are stored as opaque machine code in the cubin. There is no path to add those functions back into the NVVM IR program for whole-program consolidation. The only honest behaviors are:

1. Merge them at the cubin level (partial mode), or
2. Fatal out.

NVIDIA picked (1) with a warning, under the reasoning that `nvcc`-driven builds are the primary use case and `nvcc` knows how to feed mode-consistent inputs. For the `libcudadevrt` special case, the runtime-helper library is so small and so universally inlined that stripping it wholesale after whole-program compile is safe -- hence the exception.

## IR Collection: How byte_2A5F286 Affects sub_426CD0

`sub_426CD0` (`lto_collect_ir`) builds the option array passed to nvvm. The partial flag affects two specific options:

```c
// sub_426CD0, lines 162-176
if (byte_2A5F286) {
    // Partial mode: tell nvvm this is device-separate-compilation
    option_list.append("--device-c");
}
if (byte_2A5F285) {
    // Force-partial: also add "--force-device-c"
    option_list.append("--force-device-c");
}
```

When `byte_2A5F286` is 1, the `--device-c` flag tells the nvvm compiler to produce relocatable output that preserves external symbol references rather than resolving them. When `byte_2A5F285` is also set, the stronger `--force-device-c` flag is added.

Additionally, the Xnvvm option deduplication in `sub_426CD0` (lines 226--236) strips `--device-c` and `--force-device-c` from user-provided `-Xnvvm` options if the corresponding flags are already set, preventing duplicate conflicting flags from reaching the nvvm compiler.

## Dead Code Elimination Interaction

The partial flag also affects dead code elimination timing. At line 1427 of main:

```c
if (byte_2A5F214 && (!byte_2A5F288 || byte_2A5F285))
    dead_code_eliminate(elfw, module_list);   // sub_426AE0
```

Dead code elimination runs at merge time only when: (a) marking is enabled (`byte_2A5F214`), AND (b) either LTO is not active OR partial LTO is in effect. In whole-program LTO mode, the nvvm compiler itself handles dead code elimination internally, so running it again at link time would be redundant. In partial mode, the non-LTO objects still need traditional DCE.

Similarly, in `sub_426CD0` (lines 184--196), if marking is enabled and partial mode (`byte_2A5F285`) is NOT set, the function runs `sub_426AE0` (dead-code eliminate) on the IR modules before sending them to nvvm, and appends `-has-global-host-info` if host info is available. This pre-LTO DCE trims the IR before compilation.

## Complete Decision Flowchart

```
Option parse (sub_427AE0)
  byte_2A5F286 = 0  (default: whole-program)
  byte_2A5F285 = --force-partial-lto   (or 1 if -r + -lto)
  byte_2A5F284 = --force-whole-lto
  conflict checks: --force-partial-lto vs --force-whole-lto
                   --force-{partial,whole}-lto requires -dlto
  if (byte_2A5F285) byte_2A5F286 = 1     (line 1209)
     |
     v
Input loop (sub_42A680 per object)
  byte_2A5F286 = 0 still possible
     |
     +-- Has LTO IR? --> registered as IR module, flag unchanged
     |
     +-- Has native cubin (no IR)?
         |
         +-- Is it cudadevrt? --> byte_2A5F286 = 1 (partial, no warning,
         |                                          byte_2A5F285 untouched)
         |
         +-- Is it another object? --> byte_2A5F286 = 1, byte_2A5F285 = 1
                                       Warning: "requested LTO but '%s'
                                       not built for LTO so doing partial LTO"
     |
     v
LTO pipeline entry (main line 1010-1023)
     |
     +-- sub_426CD0 assembles option vector
     |     adds "--device-c"       if byte_2A5F286
     |     adds "--force-device-c" if byte_2A5F285
     |
     +-- sub_4BC6F0(&byte_2A5F286, ...) drives libnvvm
     |     nvvmCompileProgram returns 100 --> byte_2A5F286 = 0 (whole consolidated)
     |     nvvmCompileProgram returns 0   --> byte_2A5F286 = 1 (split modules)
     |     nvvmCompileProgram returns !=0,!=100 --> error path
     |
     v
Post-NVVM override (main lines 1070-1074)
     |
     +-- if (!byte_2A5F285 && dword_2A5B514 == 1)
     |       and (byte_2A5F284)
     |         --> byte_2A5F286 = 0 (forced whole)
     |
     v
Final dispatch (main lines 1155-1202)
     |
     +-- byte_2A5F286 == 0 --> sub_4BD4E0 (whole-program ptxas)
     |                         Log: "whole program compile"
     |                         Remove cudadevrt from module list (line 1346)
     |
     +-- byte_2A5F286 == 1, dword_2A5B514 == 1 --> sub_4BD760 (relocatable ptxas)
     |                                             Log: "relocatable compile"
     |
     +-- byte_2A5F286 == 1, dword_2A5B514 > 1 --> thread pool + sub_4264B0
                                                  Log: "relocatable compile"
                                                  Per-module merge_elf
```

## Global Variables Reference

| Address | Name | Size | Role |
|---|---|---|---|
| `0x2A5F284` | `byte_2A5F284` | 1 | `--force-whole-lto` flag |
| `0x2A5F285` | `byte_2A5F285` | 1 | `--force-partial-lto` flag (also auto-set by register\_module and by `-r`) |
| `0x2A5F286` | `byte_2A5F286` | 1 | Partial/relocatable compile flag: 0=whole, 1=partial |
| `0x2A5F287` | `byte_2A5F287` | 1 | `-dlto` flag |
| `0x2A5F288` | `byte_2A5F288` | 1 | `-lto` / `--link-time-opt` enabled flag |
| `0x2A5F1E8` | `byte_2A5F1E8` | 1 | `--relocatable-link` / `-r` flag (implies partial) |
| `0x2A5F29A` | `byte_2A5F29A` | 1 | `--emit-ptx` flag |
| `0x2A5B514` | `dword_2A5B514` | 4 | Split-compile-extended thread count (1 = single-threaded) |
| `0x2A5B518` | `dword_2A5B518` | 4 | Split-compile (nvvm) thread count |
| `0x2A5B528` | `dword_2A5B528` | 4 | Compilation mode: 0=normal, 4=LTO, 6=SASS |
| `0x2A5F214` | `byte_2A5F214` | 1 | Symbol-marking / DCE-enabled flag |
| `0x2A5F244` | `byte_2A5F244` | 1 | `-inline-info` flag |

## Function Reference

| Address | Name | Size | Role in whole-vs-partial |
|---|---|---|---|
| `0x42A680` | `register_module` | 11,939 B | Sets `byte_2A5F286 = 1` when non-LTO input encountered; conditionally sets `byte_2A5F285 = 1` unless cudadevrt |
| `0x426CD0` | `lto_collect_ir` | 7,040 B | Appends `--device-c` / `--force-device-c` based on flags; runs pre-LTO DCE when `!byte_2A5F285` |
| `0x4BC6F0` | `nvvm_compile_and_extract` | 13,602 B | Calls nvvmCompileProgram; return 100 = whole (`*a5=0`), 0 = partial (`*a5=1`) |
| `0x4BD4E0` | `ptxas_whole_program` | ~3 KB | Whole-program PTX-to-cubin compilation, no crash isolation |
| `0x4BD760` | `ptxas_compile` | ~3 KB | Relocatable PTX-to-cubin compilation with `setjmp` crash isolation |
| `0x4264B0` | `split_compile_worker` | ~2 KB | Thread pool worker for multi-module partial compile |
| `0x427AE0` | `nvlink_parse_options` | 30,272 B | Conflict detection for force flags; sets `byte_2A5F286 = 1` at line 1209 under `--force-partial-lto` |
| `0x409800` | `main` | large | Lines 1070--1074 implement post-nvvm `--force-whole-lto` override; lines 1155--1202 dispatch to whole/partial backends |
| `0x467460` | `error_emit` | ~2 KB | Emits conflict/warning diagnostics |
| `0x4BD1F0` | `lto_add_module` | ~800 B | Registers a single IR module (distinct from `register_module`) |

## Cross-References

- [LTO Overview](overview.md) -- pipeline context showing the whole-vs-partial dispatch in the main flow
- [libnvvm Integration](libnvvm-integration.md) -- `sub_4BC6F0` returns 100 (whole) or 0 (partial) to determine the path; exact `dlsym` resolution order
- [Option Forwarding](option-forwarding.md) -- `--force-partial-lto` maps to `--force-device-c` when forwarded to cicc; `--Xnvvm` option deduplication
- [Split Compilation](split-compilation.md) -- partial mode with `split_compile_extended > 1` uses the thread pool; work item lifecycle
- [LTO IR Format Versions](ir-format-versions.md) -- `lto_` profile tags (`lto_75` through `lto_121f`) that identify LTO-eligible targets
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- guard condition `(!lto || force_partial_lto)` controls whether linker DCE runs alongside LTO
- [Merge Phase](../pipeline/merge.md) -- compiled cubins from partial LTO are merged via `merge_elf`
- [Symbol Resolution](../linker/symbol-resolution.md) -- partial-mode merges keep cross-module externals live
- [Pipeline Entry](../pipeline/entry.md) -- where option parsing (`sub_427AE0`) and the LTO pipeline call fit into the 14-phase flow
- [CLI Options](../pipeline/cli-options.md) -- complete nvlink CLI option catalog including the hidden `--force-*-lto` flags

### Sibling Wiki

- **cicc wiki**: [LTO & Module Optimization](../../cicc/lto/index.html) -- the compiler-side LTO pipeline inside libnvvm. Documents the five-pass IR optimization (GlobalOpt, inliner, devirtualization, ThinLTO import) that fires when `nvvmCompileProgram` is called in whole-program mode
- **cicc wiki**: [Module Summary](../../cicc/lto/module-summary.html) -- NVModuleSummary builder used by ThinLTO import decisions that run inside libnvvm during partial-mode compiles
