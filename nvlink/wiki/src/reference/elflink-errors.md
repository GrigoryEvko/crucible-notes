# elfLink Error Codes

The elfLink subsystem is nvlink's internal library for loading, compiling, and linking device code modules. It wraps libnvvm for NVVM IR compilation, manages cubin/fatbinary extraction, and interfaces with the linker library (`libnvvm.so`). When any of these operations fail, elfLink returns an integer error code in the range 0--13. The function `sub_4BC270` at `0x4BC270` translates these codes into human-readable strings via a lookup table at `off_1D489E0`.

## Error Code Table

| Code | String | Symbolic Name |
|---:|---|---|
| 0 | *(success -- no error)* | `ELFLINK_OK` |
| 1 | `elfLink internal error` | `ELFLINK_INTERNAL` |
| 2 | `elfLink error on cubin` | `ELFLINK_CUBIN_ERROR` |
| 3 | `elfLink fatbinary error` | `ELFLINK_FATBIN_ERROR` |
| 4 | `elfLink memory error` | `ELFLINK_MEMORY_ERROR` |
| 5 | `elfLink JIT compile failed` | `ELFLINK_JIT_COMPILE` |
| 6 | `elfLink JIT link failed` | `ELFLINK_JIT_LINK` |
| 7 | `elfLink nvvm error` | `ELFLINK_NVVM_ERROR` |
| 8 | `elfLink error cubin not relocatable` | `ELFLINK_NOT_RELOCATABLE` |
| 9 | `elfLink error cubin not compatible` | `ELFLINK_NOT_COMPATIBLE` |
| 10 | `elfLink cubin arch not compatible` | `ELFLINK_ARCH_MISMATCH` |
| 11 | `elfLink linker library load error` | `ELFLINK_LIB_LOAD` |
| 12 | `elfLink error incompatible formats` | `ELFLINK_FORMAT_MISMATCH` |
| 13 | `elfLink error finalization failed` | `ELFLINK_FINALIZE_FAILED` |
| >13 | `elfLink: unexpected error` | *(default fallback)* |

The symbolic names above are inferred from the string content; the binary uses raw integer constants.

## Lookup Function

```c
// sub_4BC270 at 0x4BC270
// Table: off_1D489E0 -- 14 const char* entries (codes 0..13)
const char *elflink_error_string(unsigned int code)
{
    if (code <= 13)
        return error_table[code];   // off_1D489E0[code]
    return "elfLink: unexpected error";
}
```

The function is called from four sites in the binary:

| Caller | Address | Context |
|---|---|---|
| `sub_4297B0` | `0x4297B0` | Generic error handler -- translates any non-zero elfLink code (except 4 and 7) into a fatal diagnostic |
| `sub_42AF40` | `0x42AF40` | Input processing loop -- handles per-module load failures during LTO and cubin extraction |
| `sub_427A10` | `0x427A10` | LTO add-module path -- loads NVVM IR into the LTO program object |
| `main` | `0x409800` | Top-level pipeline -- reports errors from library loading (`sub_4BC470`) and final link (`sub_4BC4A0`) |

## Detailed Error Code Reference

### Code 0 -- Success

No error. Returned when module loading, compilation, or linking completes without failure. Never passed to `sub_4BC270` in practice.

### Code 1 -- Internal Error

**String:** `elfLink internal error`

**Trigger:** Returned by `sub_4BDAC0` (host-ELF cubin extraction at `0x4BDAC0`) and `sub_4BDAF0` (host-ELF iterator at `0x4BDAF0`) when the underlying ELF parser (`sub_487C20`, `sub_487E10`) returns an unrecognized status code (value > 2). Also returned by `sub_4BC290` (library initialization at `0x4BC290`) when the top-level context pointer is null or when `nvvmCreateProgram` fails.

**Diagnosis:** This is a catch-all for unexpected internal states. The input file may be corrupt, or an internal invariant was violated. Check that input object files are valid ELF. If the error persists with known-good inputs, it indicates a linker bug.

### Code 2 -- Cubin Error

**String:** `elfLink error on cubin`

**Trigger:** Produced when cubin extraction from a host ELF fails. The secondary table `dword_1D48A50` maps parser status 1 to elfLink code 2. The parser returns status 1 when it encounters a malformed cubin section inside a host object file.

**Diagnosis:** The input `.o` file contains embedded device code (in `.nv_fatbin` or similar sections) that could not be parsed as a valid cubin. Recompile the source with the same toolkit version as nvlink.

### Code 3 -- Fatbinary Error

**String:** `elfLink fatbinary error`

**Trigger:** Produced when fatbinary extraction or decompression fails. The secondary table maps parser status 2 to elfLink code 3. This covers failures in the fatbinary header parsing, unsupported compression formats, or truncated fatbinary data.

**Diagnosis:** The fatbinary container embedded in the host object is invalid. Verify that the input was compiled with a compatible nvcc version. Truncated files (e.g., from interrupted builds) commonly produce this error.

### Code 4 -- Memory Error

**String:** `elfLink memory error`

**Special handling:** Code 4 receives unique treatment in `sub_4297B0` -- instead of passing through `sub_4BC270`, the handler constructs a custom message by prepending a descriptive prefix (loaded from `xmmword_1D34750` / `xmmword_1D34760`) and appending ` code in <filename>`. This produces a message like `"elfLink found incompatible code in foo.o"` rather than the generic table string.

**Trigger:** Returned by `sub_4BD0A0` (NVVM IR compilation driver at `0x4BD0A0`) when the compilation pipeline fails at any stage: target architecture setup (`sub_4CE2F0`), debug mode configuration (`sub_4CE380`), 64-bit mode configuration (`sub_4CE640`), module addition (`sub_4CE070`), or final compilation (`sub_4CE8C0`). Also returned by `sub_4BD240` (cubin post-processing at `0x4BD240`) when ABI validation fails (`-m32`/`-m64` mismatch) or when the cubin bytecode extractor (`sub_4BE350`) fails.

**Diagnosis:** Usually indicates a toolkit version mismatch. The cubin or NVVM IR module was compiled with options incompatible with the current link target. Check that all input objects target the same `sm_` architecture and address size (32-bit vs 64-bit).

### Code 5 -- JIT Compile Failed

**String:** `elfLink JIT compile failed`

**Trigger:** Returned by `sub_4BD0A0` when `sub_4CE8C0` (the NVVM compilation call) returns a failure status other than 3 (which maps to code 7 instead). Also returned by `sub_4BD240` when ABI checks fail (`-m32`/`-m64` validation against `sub_4CE3E0`), or when a pass-through option string is rejected.

**Diagnosis:** The embedded NVVM IR or PTX could not be compiled to SASS for the target architecture. Check that the source was compiled for a compatible `compute_` capability. If `-dlto` is in use, verify that all LTO objects were compiled with the same major CUDA toolkit version.

### Code 6 -- JIT Link Failed

**String:** `elfLink JIT link failed`

**Trigger:** This code is only reachable through the error table. In the analyzed binary, no producer was found that explicitly returns 6. It is reserved for the case where the libnvvm link step (post-compilation module merging) fails, as distinct from compilation failure.

**Diagnosis:** If encountered, it means the NVVM linker was unable to merge compiled modules. This can happen when symbol visibility or linkage conflicts prevent merging. Ensure that device-side `extern` declarations match across translation units.

### Code 7 -- NVVM Error

**String:** `elfLink nvvm error`

**Special handling:** Code 7 receives unique treatment parallel to code 4. In `sub_4297B0` and `sub_42A2D0`, when the error code is 7 the handler checks `byte_2A5F298` (the `-lto-allow-unresolved` flag) and whether the filename contains `"cudadevrt"`. If the flag is set or the input is libcudadevrt, the error is silently suppressed. Otherwise, a diagnostic is emitted referencing the target architecture and the failing input file.

**Trigger:** Returned by `sub_4BD0A0` when `sub_4CE8C0` returns status 3, indicating that the NVVM compilation produced NVVM IR output (not SASS) -- meaning the module contains unresolved references that require a subsequent link step. This is expected for LTO intermediate objects and libcudadevrt.

**Diagnosis:** Typically not a true error. It indicates the module contains NVVM IR that cannot be finalized to SASS in isolation because it has unresolved references. When linking with `-dlto`, this is normal for all LTO input objects. It becomes a user-visible error only when encountered during non-LTO linking without `-lto-allow-unresolved`.

### Code 8 -- Cubin Not Relocatable

**String:** `elfLink error cubin not relocatable`

**Trigger:** Returned by `sub_4BD240` (cubin post-processing at `0x4BD240`) when the cubin extraction via `sub_4BE350` fails and the resulting context indicates no compilation output was produced (the compilation output pointer at offset `+16` of the context is null). This means the cubin was compiled without relocatable device code (`-rdc` / `-dc`), making it unlinkable.

**Diagnosis:** The input cubin was compiled as a standalone (whole-program) object without separate compilation. Recompile with `nvcc -dc` (device code compilation) to produce relocatable device objects. Alternatively, if linking a static library, ensure it was built with `-rdc=true`.

### Code 9 -- Cubin Not Compatible

**String:** `elfLink error cubin not compatible`

**Trigger:** Produced during cubin validation when the object's ELF metadata does not match the link-time target. This covers ABI version mismatches, size mismatches (32-bit vs 64-bit), and general compatibility flags that prevent linking.

**Diagnosis:** The input cubin was compiled with an incompatible ABI version or addressing mode. Check the nvcc version used to compile each input. The linker emits more specific diagnostics before this error: `"Input file '%s' abi does not match"`, `"Input file '%s' size does not match target '%s'"`, or `"Input file '%s' ABI version '%u' is incompatible with target ABI version '%u'"`.

### Code 10 -- Architecture Not Compatible

**String:** `elfLink cubin arch not compatible`

**Trigger:** Returned by `sub_4BC290` (library initialization at `0x4BC290`) when `libnvvm.so` cannot be loaded (the `dlopen` handle is null) or when the `__nvvmHandle` or `nvvmCreateProgram` symbols cannot be resolved via `dlsym`. Also returned by `sub_4BC4A0` when similar symbol resolution failures occur for `__nvvmHandle` during the add-module call.

**Diagnosis:** Despite the name suggesting architecture incompatibility, this code primarily means the linker library infrastructure is unavailable. The `libnvvm.so` library could not be loaded or is from an incompatible toolkit version. Verify that the CUDA toolkit installation is complete and that `libnvvm.so` is on the library search path (set via `--libnvvm-path` or the toolkit's `lib64/` directory).

### Code 11 -- Linker Library Load Error

**String:** `elfLink linker library load error`

**Trigger:** Returned by `sub_4BC4A0` (the add-module-to-program call at `0x4BC4A0`) when the `__nvvmHandle` callback returns a non-null function pointer but calling that function with the module data returns a non-zero status. This means `libnvvm.so` was loaded successfully but rejected the specific module being added.

**Diagnosis:** The libnvvm runtime rejected the NVVM IR module. The module may be in an unsupported bitcode format version, or contain constructs not supported by the installed libnvvm. Ensure that all input objects were compiled with the same CUDA toolkit version as the nvlink binary.

### Code 12 -- Incompatible Formats

**String:** `elfLink error incompatible formats`

**Trigger:** This code is reachable through the error table but no direct producer was identified in the analyzed call paths. It is reserved for cases where the input module format (cubin vs PTX vs NVVM IR) is incompatible with the requested link operation.

**Diagnosis:** The linker encountered a module in a format it cannot process in the current mode. For example, attempting to link a Mercury (capmerc) object in a CUDA-only link, or mixing incompatible ELF object formats.

### Code 13 -- Finalization Failed

**String:** `elfLink error finalization failed`

**Trigger:** This code is reachable through the error table. It maps to failures in the finalization (off-target to on-target compilation) step of the pipeline.

**Diagnosis:** The finalizer could not convert the intermediate representation to native SASS for the target architecture. This typically occurs during forward-compatibility finalization when the input cubin targets a different SM architecture. See also the separate `"Internal FNLZR error '%s'"` diagnostic which provides more detail.

## Error Dispatch Logic

The generic error handler `sub_4297B0` at `0x4297B0` implements a three-way dispatch:

```
sub_4297B0(error_code, filename):
    if error_code == 0:
        return                          // success, nothing to report
    if error_code == 7:
        if lto_allow_unresolved or "cudadevrt" in filename:
            return                      // silently suppress
        fatal("nvvm IR for arch %s not found in %s", target_arch, filename)
    if error_code == 4:
        msg = "elfLink found incompatible code in " + filename
        fatal(msg)
    else:
        msg = elflink_error_string(error_code)   // sub_4BC270
        fatal(msg)
```

All fatal diagnostics go through `sub_467460`, which formats the message with the `"error   "` severity prefix and writes to stderr. When `--Werror` is active, warnings are promoted to the `"error*  "` level.

## Broader Diagnostic Message Catalog

Beyond the 14 elfLink error codes, nvlink emits a comprehensive set of user-facing diagnostics during linking. These are organized by severity.

### Severity Levels

The diagnostic system at `0x1D3C660` defines five severity levels:

| Level | Prefix | Meaning |
|---|---|---|
| warning | `warning ` | Non-fatal issue, link continues |
| info | `info    ` | Informational message |
| error | `error   ` | Fatal error, link aborted |
| error* | `error*  ` | Warning promoted to error by `--Werror` |
| fatal | `fatal   ` | Unrecoverable internal failure |

### Fatal Linker Errors

These errors abort the link:

| Message | Trigger |
|---|---|
| `Cannot open file '%s'` | Input file does not exist or is not readable |
| `Bad file name '%s'` | Input filename is empty or contains invalid characters |
| `Unknown arch name '%s'` | The `-arch` value is not a recognized `sm_` target |
| `Internal error: Aborting` | Unrecoverable internal assertion failure |
| `Internal error: %s` | Internal assertion with detail string |
| `Bailing out due to earlier errors` | Accumulated error count exceeded threshold |
| `Could not open output file '%s'` | Output path is not writable |
| `Could not open input file '%s'` | Input file cannot be opened |
| `Must specify output file with -o option` | Missing `-o` flag |
| `No input files specified; ...` | No object files provided on command line |
| `Unknown option '%s'` | Unrecognized CLI flag |
| `Unsupported file type '%s'` | Input is not ELF, archive, PTX, or fatbinary |
| `Could not write file '%s'` | I/O error during output |
| `Could not read file '%s'` | I/O error during input |
| `Multiple definition of '%s' in '%s', first defined in '%s'` | Symbol multiply defined across objects |
| `Undefined reference to '%s' in '%s'` | Unresolved symbol at link time |
| `Internal FNLZR error '%s'` | Finalizer returned an error string |

### Compatibility Errors

| Message | Trigger |
|---|---|
| `Cannot target %s when input '%s' is SASS` | SASS object cannot be retargeted to a different architecture |
| `Input file '%s' abi does not match` | ELF ABI version mismatch |
| `Input file '%s' size does not match target '%s'` | 32-bit vs 64-bit object/target mismatch |
| `Input file '%s' arch does not match target '%s'` | Object compiled for wrong `sm_` target |
| `Input file '%s' ABI version '%u' is incompatible with target ABI version '%u'` | Detailed ABI version incompatibility |
| `Target format and SM Arch ('%u') mismatch` | Cubin format does not match SM generation |
| `SM Arch ('%s') not found in '%s'` | Fatbinary does not contain code for the target arch |
| `SM Arch ('%s') must be >= 20` | Target architecture too old |
| `Input file '%s' must be recompiled with toolkit >= Cuda 12.0` | Object from pre-12.0 toolkit |
| `Input file '%s' must be recompiled with toolkit >= Cuda 7.0` | Object from pre-7.0 toolkit |
| `Conflicting options '%s' and '%s'` | Mutually exclusive CLI flags |

### Warnings

| Message | Trigger |
|---|---|
| `Option '%s' is not fully implemented for gpu archtecture '%s' and may not work as expected` | Feature partially implemented |
| `Option '%s' not supported for gpu architecture '%s'` | Feature not available for target |
| `Ignoring -dlto option because no LTO objects found` | `-dlto` specified but no LTO inputs |
| `Some objects do not have '%s' specified but others do; will build everything with '%s=%d'` | Mixed compilation options across objects |
| `Skipping incompatible '%s' when searching for -l%s` | Library found but wrong architecture |
| `Input file '%s' newer than toolkit (%d vs %d)` | Forward-compatibility concern |
| `option '%s' has been deprecated` | Deprecated CLI option used |
| `incompatible redefinition for option '%s', the last value of this option was used` | Option specified multiple times with different values |
| `Stack size for entry function '%s' cannot be statically determined` | Indirect calls prevent stack analysis |
| `Function '%s' uses %d bytes stack but limited to %d` | Stack usage exceeds `--maxrregcount` budget |
| `Prototype doesn't match for '%s' in '%s', first defined in '%s'` | Kernel parameter signature mismatch |
| `Size doesn't match for '%s' in '%s', first specified in '%s'` | Symbol size conflict |

### Resource Limit Errors

| Message | Trigger |
|---|---|
| `File uses too much global %s data (0x%llx bytes, 0x%x max)` | Global/constant memory overflow |
| `Entry function '%s' uses too much %s data (0x%llx bytes, 0x%x max)` | Per-kernel resource limit exceeded |
| `More than %d %s used in entry function '%s'` | Register/barrier count exceeded |
| `Entry function '%s' uses too much data for compiler-generated constants; please recompile with -Xptxas --disable-optimizer-constants` | Constant bank overflow from optimizations |

### Warning Suppression Flags

Several CLI options control warning emission:

| Flag | Effect |
|---|---|
| `--disable-warnings` | Suppress all warning diagnostics |
| `--Werror` / `--warning-as-error` | Promote all warnings to errors |
| `--suppress-stack-size-warning` | Suppress stack-size-related warnings |
| `--suppress-arch-warning` | Suppress architecture mismatch warnings |
| `--extra-warnings` | Enable additional advisory warnings |

## Error Flow Architecture

```
Input Processing (sub_42AF40 / sub_42A2D0)
    |
    +-- sub_4BD0A0  (NVVM IR compilation)    --> codes 0, 5, 7
    +-- sub_4BD240  (cubin post-processing)  --> codes 0, 1, 5, 8
    +-- sub_4BDAC0  (host-ELF extraction)    --> codes 0, 1, 2, 3
    +-- sub_4BDAF0  (host-ELF iteration)     --> codes 0, 1, 2, 3
    |
    v
sub_4297B0  (generic error dispatch)
    |
    +-- code 0:  no-op
    +-- code 4:  custom "incompatible code in <file>" message
    +-- code 7:  conditional suppress or arch-specific fatal
    +-- other:   sub_4BC270(code) --> error string --> sub_467460 (fatal)

Library Loading (main / sub_427A10)
    |
    +-- sub_4BC470  (dlopen libnvvm.so)      --> sub_4BC290 --> codes 0, 1, 10
    +-- sub_4BC4A0  (add module to program)  --> codes 0, 10, 11
    |
    v
    sub_4BC270(code) --> error string --> sub_467460 (fatal)

LTO Compilation (sub_4BC6F0)
    |
    +-- nvvmCompileProgram via dlsym         --> code 8 (log + compile error)
    +-- nvvmGetProgramLog                    --> code 1 (API failure)
    +-- nvvmGetCompiledResult                --> code 10 (symbol lookup failure)
    |
    v
    Returns raw code to caller for sub_4BC270 translation
```

## Key Implementation Details

**Secondary mapping table (`dword_1D48A50`):** Functions `sub_4BDAC0`, `sub_4BDAF0`, and `sub_4BDB30` use a 3-entry mapping table at `0x1D48A50` to convert their internal parser return codes (0, 1, 2) into elfLink codes (0, 2, 3). Values > 2 map to code 1 (internal error).

**setjmp/longjmp error recovery:** Several elfLink functions (`sub_4BC290`, `sub_4BC4A0`, `sub_4BD240`) use `setjmp`/`longjmp` for non-local error recovery. This allows deep call stacks within libnvvm to unwind cleanly back to the elfLink entry point. The longjmp path always returns code 1 (internal error), protecting the linker from crashes inside the dynamically loaded libnvvm.

**Code 7 suppression logic:** The NVVM error code (7) is the only error code with conditional suppression. The flag `byte_2A5F298` (`-lto-allow-unresolved`) and the filename check for `"cudadevrt"` together determine whether the error is fatal or silently ignored. This allows libcudadevrt's NVVM IR to pass through the non-LTO path without aborting.

**Code 4 custom message construction:** Rather than using the generic table string, code 4 constructs a message from two SSE-loaded 16-byte constants (`xmmword_1D34750`, `xmmword_1D34760`) forming the prefix `"elfLink found incompatible"`, followed by `" code in "` and the filename. This provides file-specific context that the generic string lacks.
