# Whole vs Partial LTO

When nvlink performs link-time optimization, it must decide between two fundamentally different compilation strategies: **whole-program** compilation, where all device code is merged into a single NVVM IR module and compiled as one unit; and **partial** (relocatable) compilation, where the LTO-compiled code is emitted as a relocatable object that will be linked conventionally against non-LTO inputs. The decision is driven by a single byte-sized global flag, `byte_2A5F286`, which starts at 0 (whole-program) and is flipped to 1 (partial) when any input object lacks LTO IR. The `--force-whole-lto` and `--force-partial-lto` CLI flags can override this automatic detection, with conflict checking at option-parse time.

| | |
|---|---|
| **Decision variable** | `byte_2A5F286` at address `0x2A5F286` (1 byte). 0 = whole-program, 1 = partial/relocatable |
| **Force-whole flag** | `byte_2A5F284` -- set by `--force-whole-lto` |
| **Force-partial flag** | `byte_2A5F285` -- set by `--force-partial-lto` |
| **Whole-program compiler** | `sub_4BD4E0` (`ptxas_whole_program`) at `0x4BD4E0` |
| **Relocatable compiler** | `sub_4BD760` (`ptxas_compile`) at `0x4BD760` |
| **IR collector** | `sub_426CD0` (`lto_collect_ir`) at `0x426CD0` |
| **NVVM compile wrapper** | `sub_4BC6F0` (`nvvm_compile_and_extract`) at `0x4BC6F0` |
| **Module registrar** | `sub_42A680` (`register_module`) at `0x42A680` |

## The Decision Variable: byte_2A5F286

`byte_2A5F286` is the central control for the whole-vs-partial decision. Its lifecycle:

1. **Initialization**: defaults to 0 (whole-program assumed).
2. **Option parsing** (`sub_427AE0`): if `--force-partial-lto` is active and no conflict, the flag is set to 1 at line 1209.
3. **Input registration** (`sub_42A680`): when a non-LTO object is encountered during the input loop, the flag is set to 1.
4. **NVVM compilation** (`sub_4BC6F0`): the flag is passed by pointer as parameter `a5`. The nvvm return code can modify it.
5. **Post-NVVM override** (main, line 1073): if `--force-whole-lto` (`byte_2A5F284`) is active, the flag is forcibly cleared to 0.
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
2. Checks whether the object is `libcudadevrt` (via `strstr(filename, "cudadevrt")`). If it is NOT cudadevrt, also sets `byte_2A5F285 = 1` (the force-partial flag) and emits a warning message. The cudadevrt exception exists because cudadevrt is always a native archive and is expected to lack LTO IR -- its presence alone should not trigger a partial-mode warning.

## Option Parsing: --force-whole-lto and --force-partial-lto

Both flags are registered in `sub_427AE0` as type-1 (bool) options with hidden visibility (flag 4):

```c
// sub_427AE0, lines 536-559
option_register(parser, "force-partial-lto", "force-partial-lto",
    type=1, multiplicity=0, flags=4,
    help="force doing partial LTO when -dlto");

option_register(parser, "force-whole-lto", "force-whole-lto",
    type=1, multiplicity=0, flags=4,
    help="force doing whole LTO when -dlto");
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

**4. --force-partial-lto with -emit-ptx** (lines 1206--1211): If `--force-partial-lto` is active (or is about to be set), and `--emit-ptx` is also active, the code takes the `LABEL_66` path which validates split-compile compatibility.

### Option Validation Summary

| Combination | Result |
|---|---|
| `--force-partial-lto` alone (no `-dlto`) | Error: requires `-dlto` |
| `--force-whole-lto` alone (no `-dlto`) | Error: requires `-dlto` |
| `--force-partial-lto` + `--force-whole-lto` | Error: mutual conflict |
| `-r` + `-lto` | Implicit `--force-partial-lto` (no error) |
| `--force-partial-lto` + `-dlto` | Valid: forces partial mode |
| `--force-whole-lto` + `-dlto` | Valid: forces whole mode |

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

This applies only when `--force-partial-lto` is NOT set and split-compile-extended threads equal 1 (single-threaded mode). The `--force-whole-lto` flag forcibly clears `byte_2A5F286` to 0, causing the whole-program compilation path to execute regardless of what the nvvm compiler decided.

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

Similarly, in `sub_426CD0` (lines 184--196), if marking is enabled and partial mode is NOT set, the function runs `sub_426AE0` (dead-code eliminate) on the IR modules before sending them to nvvm, and appends `-has-global-host-info` if host info is available. This pre-LTO DCE trims the IR before compilation.

## Complete Decision Flowchart

```
Input loop starts
byte_2A5F286 = 0 (whole-program assumed)
     |
     v
For each input object:
     |
     +-- Has LTO IR? --> registered as IR module, flag unchanged
     |
     +-- Has native cubin (no IR)?
         |
         +-- Is it cudadevrt? --> byte_2A5F286 = 1 (partial, no warning)
         |
         +-- Is it another object? --> byte_2A5F286 = 1, byte_2A5F285 = 1
                                       Warning: "requested LTO but '%s'
                                       not built for LTO so doing partial LTO"
     |
     v
After input loop, before NVVM compile:
     |
     +-- --force-partial-lto from CLI? --> byte_2A5F286 = 1 (already set at parse time)
     |
     v
NVVM compilation (sub_4BC6F0):
     |
     +-- nvvmCompileProgram returns 100 --> byte_2A5F286 = 0 (whole consolidated)
     |
     +-- nvvmCompileProgram returns 0   --> byte_2A5F286 = 1 (split modules)
     |
     v
Post-NVVM override:
     |
     +-- --force-whole-lto? --> byte_2A5F286 = 0 (forced whole)
     |
     v
Final dispatch:
     |
     +-- byte_2A5F286 == 0 --> sub_4BD4E0 (whole-program ptxas)
     |                         Log: "whole program compile"
     |                         Remove cudadevrt from module list
     |
     +-- byte_2A5F286 == 1, single module --> sub_4BD760 (relocatable ptxas)
     |                                        Log: "relocatable compile"
     |
     +-- byte_2A5F286 == 1, multi module  --> thread pool + sub_4264B0
                                              Log: "relocatable compile"
                                              Per-module merge_elf
```

## Global Variables Reference

| Address | Name | Size | Role |
|---|---|---|---|
| `0x2A5F284` | `byte_2A5F284` | 1 | `--force-whole-lto` flag |
| `0x2A5F285` | `byte_2A5F285` | 1 | `--force-partial-lto` flag (also auto-set by register\_module) |
| `0x2A5F286` | `byte_2A5F286` | 1 | Partial/relocatable compile flag: 0=whole, 1=partial |
| `0x2A5F287` | `byte_2A5F287` | 1 | `-dlto` flag |
| `0x2A5F288` | `byte_2A5F288` | 1 | `-lto` / `--link-time-opt` enabled flag |
| `0x2A5F1E8` | `byte_2A5F1E8` | 1 | `--relocatable-link` / `-r` flag (implies partial) |
| `0x2A5B514` | `dword_2A5B514` | 4 | Split-compile-extended thread count (1 = single-threaded) |
| `0x2A5B528` | `dword_2A5B528` | 4 | Compilation mode: 0=normal, 4=LTO, 6=SASS |

## Function Reference

| Address | Name | Size | Role in whole-vs-partial |
|---|---|---|---|
| `0x42A680` | `register_module` | 11,939 B | Sets `byte_2A5F286 = 1` when non-LTO input encountered |
| `0x426CD0` | `lto_collect_ir` | 7,040 B | Appends `--device-c` / `--force-device-c` based on flags |
| `0x4BC6F0` | `nvvm_compile_and_extract` | 13,602 B | Calls nvvmCompileProgram; return 100 = whole, 0 = partial |
| `0x4BD4E0` | `ptxas_whole_program` | ~3 KB | Whole-program PTX-to-cubin compilation |
| `0x4BD760` | `ptxas_compile` | ~3 KB | Relocatable PTX-to-cubin compilation |
| `0x4264B0` | `split_compile_worker` | ~2 KB | Thread pool worker for multi-module partial compile |
| `0x427AE0` | `nvlink_parse_options` | 30,272 B | Conflict detection for force flags |
| `0x467460` | `error_emit` | ~2 KB | Emits conflict/warning diagnostics |
