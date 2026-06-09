# elfLink Error Codes and Linker Diagnostic Catalog

The elfLink subsystem is nvlink's internal library for loading, compiling, and linking device code modules. It wraps libnvvm for NVVM IR compilation, manages cubin/fatbinary extraction, and interfaces with the linker library (`libnvvm.so`). When any of these operations fail, elfLink returns an integer error code in the range 0--13. The function `sub_4BC270` at `0x4BC270` translates these codes into human-readable strings via a lookup table at `off_1D489E0`.

Beyond the elfLink subsystem's 14 return codes, nvlink emits over 100 distinct user-facing diagnostic messages through the `diag_emit` (`sub_467460`) pipeline. This page catalogs every error string, maps each to its source function and triggering conditions, and provides user-facing fix suggestions.

## Part 1: elfLink Error Code Table

| Code | String (addr) | Symbolic Name |
|---:|---|---|
| 0 | *(success — no error)* | `ELFLINK_OK` |
| 1 | `elfLink internal error` (`0x1D4883F`) | `ELFLINK_INTERNAL` |
| 2 | `elfLink error on cubin` (`0x1D48856`) | `ELFLINK_CUBIN_ERROR` |
| 3 | `elfLink fatbinary error` (`0x1D4886D`) | `ELFLINK_FATBIN_ERROR` |
| 4 | `elfLink memory error` (`0x1D48885`) | `ELFLINK_MEMORY_ERROR` |
| 5 | `elfLink JIT compile failed` (`0x1D4889A`) | `ELFLINK_JIT_COMPILE` |
| 6 | `elfLink JIT link failed` (`0x1D488B5`) | `ELFLINK_JIT_LINK` |
| 7 | `elfLink nvvm error` (`0x1D488CD`) | `ELFLINK_NVVM_ERROR` |
| 8 | `elfLink error cubin not relocatable` (`0x1D488E0`) | `ELFLINK_NOT_RELOCATABLE` |
| 9 | `elfLink error cubin not compatible` (`0x1D48908`) | `ELFLINK_NOT_COMPATIBLE` |
| 10 | `elfLink cubin arch not compatible` (`0x1D48930`) | `ELFLINK_ARCH_MISMATCH` |
| 11 | `elfLink linker library load error` (`0x1D48958`) | `ELFLINK_LIB_LOAD` |
| 12 | `elfLink error incompatible formats` (`0x1D48980`) | `ELFLINK_FORMAT_MISMATCH` |
| 13 | `elfLink error finalization failed` (`0x1D489A8`) | `ELFLINK_FINALIZE_FAILED` |
| >13 | `elfLink: unexpected error` (`0x1D48760`) | *(default fallback)* |

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

The function is called from five sites in the binary:

| Caller | Address | Context |
|---|---|---|
| `sub_4297B0` | `0x4297B0` | Generic error handler — translates any non-zero elfLink code (except 4 and 7) into a fatal diagnostic |
| `sub_42AF40` | `0x42AF40` | Input processing loop — handles per-module load failures during LTO and cubin extraction |
| `sub_42A2D0` | `0x42A2D0` | Host-ELF extraction path — iterates embedded cubins and reports per-cubin elfLink errors |
| `sub_427A10` | `0x427A10` | LTO add-module path — loads NVVM IR into the LTO program object |
| `main` | `0x409800` | Top-level pipeline — reports errors from library loading (`sub_4BC470`) and final LTO compilation (`sub_4BC6F0`) |

## Detailed Error Code Reference

### Code 0 — Success

No error. Returned when module loading, compilation, or linking completes without failure. Never passed to `sub_4BC270` in practice.

### Code 1 — Internal Error

**String:** `elfLink internal error` (`0x1D4883F`)

**Trigger:** Returned by `sub_4BDAC0` (host-ELF cubin extraction at `0x4BDAC0`) and `sub_4BDAF0` (host-ELF iterator at `0x4BDAF0`) when the underlying ELF parser (`sub_487C20`, `sub_487E10`) returns an unrecognized status code (value > 2). Also returned by `sub_4BC290` (library initialization at `0x4BC290`) when the top-level context pointer is null or when `nvvmCreateProgram` fails. In the LTO compilation path (`sub_4BC6F0`), returned when `nvvmGetProgramLog` API calls fail.

**Diagnosis:** This is a catch-all for unexpected internal states. The input file may be corrupt, or an internal invariant was violated. Check that input object files are valid ELF. If the error persists with known-good inputs, it indicates a linker bug.

**User fix:** Verify input files with `readelf -h` to confirm valid ELF headers. Recompile inputs with the same toolkit version as nvlink.

### Code 2 — Cubin Error

**String:** `elfLink error on cubin` (`0x1D48856`)

**Trigger:** Produced when cubin extraction from a host ELF fails. The secondary table `dword_1D48A50` maps parser status 1 to elfLink code 2. The parser returns status 1 when it encounters a malformed cubin section inside a host object file.

**Diagnosis:** The input `.o` file contains embedded device code (in `.nv_fatbin` or similar sections) that could not be parsed as a valid cubin. Recompile the source with the same toolkit version as nvlink.

**User fix:** Recompile the failing source file. If the file was produced by an older CUDA toolkit, upgrade the compilation to match the linker version.

### Code 3 — Fatbinary Error

**String:** `elfLink fatbinary error` (`0x1D4886D`)

**Trigger:** Produced when fatbinary extraction or decompression fails. The secondary table maps parser status 2 to elfLink code 3. This covers failures in the fatbinary header parsing, unsupported compression formats, or truncated fatbinary data.

**Diagnosis:** The fatbinary container embedded in the host object is invalid. Verify that the input was compiled with a compatible nvcc version. Truncated files (e.g., from interrupted builds) commonly produce this error.

**User fix:** Rebuild from scratch (`make clean && make`). Check for filesystem corruption or incomplete file copies.

### Code 4 — Incompatible Code (Memory Error)

**String:** `elfLink memory error` (`0x1D48885`)

**Special handling:** Code 4 receives unique treatment in `sub_4297B0` — instead of passing through `sub_4BC270`, the handler constructs a custom message by prepending a descriptive prefix (loaded from `xmmword_1D34750` / `xmmword_1D34760`) and appending `" code in <filename>"`. This produces a user-facing message like `"elfLink found incompatible code in foo.o"` rather than the generic table string. The same custom-message logic is duplicated in `sub_42A2D0` for the host-ELF iteration path.

**Trigger:** Returned by `sub_4BD0A0` (NVVM IR compilation driver at `0x4BD0A0`) when the compilation pipeline fails at any stage: target architecture setup (`sub_4CE2F0`), debug mode configuration (`sub_4CE380`), 64-bit mode configuration (`sub_4CE640`), module addition (`sub_4CE070`), or final compilation (`sub_4CE8C0`). Also returned by `sub_4BD240` (cubin post-processing at `0x4BD240`) when ABI validation fails (`-m32`/`-m64` mismatch) or when the cubin bytecode extractor (`sub_4BE350`) fails.

**Diagnosis:** Usually indicates a toolkit version mismatch. The cubin or NVVM IR module was compiled with options incompatible with the current link target. Check that all input objects target the same `sm_` architecture and address size (32-bit vs 64-bit).

**User fix:** Ensure all `.o` files were compiled with the same `-arch=sm_XX` and `-m64`/`-m32` flags. Check `nvcc --version` matches the nvlink version.

### Code 5 — JIT Compile Failed

**String:** `elfLink JIT compile failed` (`0x1D4889A`)

**Trigger:** Returned by `sub_4BD0A0` when `sub_4CE8C0` (the NVVM compilation call) returns a failure status other than 3 (which maps to code 7 instead). Also returned by `sub_4BD240` when ABI checks fail (`-m32`/`-m64` validation against `sub_4CE3E0`), or when a pass-through option string is rejected.

**Diagnosis:** The embedded NVVM IR or PTX could not be compiled to SASS for the target architecture. Check that the source was compiled for a compatible `compute_` capability. If `-dlto` is in use, verify that all LTO objects were compiled with the same major CUDA toolkit version.

**User fix:** Verify the `compute_` and `sm_` targets are compatible. With LTO (`-dlto`), all objects must use the same CUDA toolkit major version.

### Code 6 — JIT Link Failed

**String:** `elfLink JIT link failed` (`0x1D488B5`)

**Trigger:** This code is only reachable through the error table. In the analyzed binary, no producer was found that explicitly returns 6. It is reserved for the case where the libnvvm link step (post-compilation module merging) fails, as distinct from compilation failure.

**Diagnosis:** If encountered, it means the NVVM linker was unable to merge compiled modules. This can happen when symbol visibility or linkage conflicts prevent merging.

**User fix:** Ensure that device-side `extern` declarations match across translation units. Check for conflicting function prototypes in device code headers.

### Code 7 — NVVM Error (Unresolved References)

**String:** `elfLink nvvm error` (`0x1D488CD`)

**Special handling:** Code 7 receives unique treatment parallel to code 4. In `sub_4297B0`, `sub_42A2D0`, and `sub_42AF40`, when the error code is 7 the handler checks `byte_2A5F298` (the `-lto-allow-unresolved` flag at `0x2A5F298`) and whether the filename contains `"cudadevrt"`. If the flag is set or the input is libcudadevrt, the error is silently suppressed. Otherwise, a fatal diagnostic is emitted via descriptor `unk_2A5B660`:

```text
nvvm IR for arch <target_arch> not found in <filename>
```

**Trigger:** Returned by `sub_4BD0A0` when `sub_4CE8C0` returns status 3, indicating that the NVVM compilation produced NVVM IR output (not SASS) — meaning the module contains unresolved references that require a subsequent link step. This is expected for LTO intermediate objects and libcudadevrt.

**Diagnosis:** Typically not a true error. It indicates the module contains NVVM IR that cannot be finalized to SASS in isolation because it has unresolved references. When linking with `-dlto`, this is normal for all LTO input objects. It becomes a user-visible error only when encountered during non-LTO linking without `-lto-allow-unresolved`.

**User fix:** If you see this error, add `-dlto` to enable LTO linking. For libraries containing device runtime code, this is expected behavior.

### Code 8 — Cubin Not Relocatable

**String:** `elfLink error cubin not relocatable` (`0x1D488E0`)

**Trigger:** Returned by `sub_4BD240` (cubin post-processing at `0x4BD240`) when the cubin extraction via `sub_4BE350` fails and the resulting context indicates no compilation output was produced (the compilation output pointer at offset `+16` of the context is null). This means the cubin was compiled without relocatable device code (`-rdc` / `-dc`), making it unlinkable.

**Diagnosis:** The input cubin was compiled as a standalone (whole-program) object without separate compilation. Recompile with `nvcc -dc` (device code compilation) to produce relocatable device objects.

**User fix:** Add `-dc` (or equivalently `-rdc=true`) to the `nvcc` compile command. For static libraries, ensure they were built with `-rdc=true`.

### Code 9 — Cubin Not Compatible

**String:** `elfLink error cubin not compatible` (`0x1D48908`)

**Trigger:** Produced during cubin validation when the object's ELF metadata does not match the link-time target. This covers ABI version mismatches, size mismatches (32-bit vs 64-bit), and general compatibility flags that prevent linking.

**Diagnosis:** The input cubin was compiled with an incompatible ABI version or addressing mode. Check the nvcc version used to compile each input. The linker emits more specific diagnostics before this error:
- `"Input file '%s' abi does not match"` (`0x1D34C68`)
- `"Input file '%s' size does not match target '%s'"` (`0x1D34C90`)
- `"Input file '%s' ABI version '%u' is incompatible with target ABI version '%u'"` (`0x1D34CF0`)

**User fix:** Recompile all objects with the same toolkit version and matching address-size flags.

### Code 10 — Architecture Not Compatible

**String:** `elfLink cubin arch not compatible` (`0x1D48930`)

**Trigger:** Returned by `sub_4BC290` (library initialization at `0x4BC290`) when `libnvvm.so` cannot be loaded (the `dlopen` handle is null) or when the `__nvvmHandle` or `nvvmCreateProgram` symbols cannot be resolved via `dlsym`. Also returned by `sub_4BC4A0` when similar symbol resolution failures occur for `__nvvmHandle` during the add-module call. In the LTO compilation path (`sub_4BC6F0`), returned when `nvvmGetCompiledResult` symbol lookup fails.

**Diagnosis:** Despite the name suggesting architecture incompatibility, this code primarily means the linker library infrastructure is unavailable. The `libnvvm.so` library could not be loaded or is from an incompatible toolkit version.

**User fix:** Verify that the CUDA toolkit installation is complete and that `libnvvm.so` is on the library search path (set via `--libnvvm-path` or the toolkit's `lib64/` directory). Check `LD_LIBRARY_PATH` if needed.

### Code 11 — Linker Library Load Error

**String:** `elfLink linker library load error` (`0x1D48958`)

**Trigger:** Returned by `sub_4BC4A0` (the add-module-to-program call at `0x4BC4A0`) when the `__nvvmHandle` callback returns a non-null function pointer but calling that function with the module data returns a non-zero status. This means `libnvvm.so` was loaded successfully but rejected the specific module being added.

**Diagnosis:** The libnvvm runtime rejected the NVVM IR module. The module may be in an unsupported bitcode format version, or contain constructs not supported by the installed libnvvm.

**User fix:** Ensure that all input objects were compiled with the same CUDA toolkit version as the nvlink binary. Check `nvcc --version` and `nvlink --version` output.

### Code 12 — Incompatible Formats

**String:** `elfLink error incompatible formats` (`0x1D48980`)

**Trigger:** This code is reachable through the error table but no direct producer was identified in the analyzed call paths. It is reserved for cases where the input module format (cubin vs PTX vs NVVM IR) is incompatible with the requested link operation.

**Diagnosis:** The linker encountered a module in a format it cannot process in the current mode. For example, attempting to link a Mercury (capmerc) object in a CUDA-only link, or mixing incompatible ELF object formats.

**User fix:** Ensure all inputs are of the same format type. Do not mix Mercury objects with standard CUDA objects unless the linker is configured for Mercury mode.

### Code 13 — Finalization Failed

**String:** `elfLink error finalization failed` (`0x1D489A8`)

**Trigger:** This code maps to failures in the finalization (off-target to on-target compilation) step of the pipeline.

**Diagnosis:** The finalizer could not convert the intermediate representation to native SASS for the target architecture. This typically occurs during forward-compatibility finalization when the input cubin targets a different SM architecture. See also the separate `"Internal FNLZR error '%s'"` diagnostic (`0x1D34F74`) which provides more detail.

**User fix:** Ensure the target architecture (`-arch`) matches the `sm_` target that inputs were compiled for. If using forward compatibility, verify the toolkit supports the target.

## Error Dispatch Logic

The generic error handler `sub_4297B0` at `0x4297B0` implements a three-way dispatch:

```c
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

The same dispatch logic is replicated in `sub_42A2D0` (host-ELF extraction path), which additionally handles the host-ELF iterator loop (`sub_4BDAF0`) — calling the dispatch for each extracted cubin.

All fatal diagnostics go through `sub_467460`, which formats the message with the `"error   "` severity prefix and writes to stderr. When `--Werror` is active, warnings are promoted to the `"error*  "` level.

## Part 2: Broader Diagnostic Message Catalog

Beyond the 14 elfLink error codes, nvlink emits a comprehensive set of user-facing diagnostics during linking. These are organized by severity and category.

### Severity Levels

The diagnostic system at `0x1D3C660` defines six severity levels:

| Level | Value | Prefix | Meaning |
|---|---|---|---|
| note | 0 | *(none)* | Silent; `diag_emit` returns immediately |
| info | 1 | *(empty)* | Informational; suppressed by `--disable-infos` |
| info | 2 | `info    ` | Informational with explicit label |
| warning | 3 | `warning ` | Non-fatal issue, link continues |
| error* | 4 | `error*  ` | Warning promoted to error by `--Werror` |
| error | 5 | `error   ` | Fatal error, link aborted |
| fatal | 6 | `fatal   ` | Unrecoverable internal failure |

### Category 1: Input File Errors

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `Cannot open file '%s'` | `0x1D39250` | fatal | `sub_426570` | Input file does not exist or is not readable | Verify file path and permissions |
| `Bad file name '%s'` | `0x1D39266` | fatal | `sub_427AE0` | Input filename is empty or contains invalid characters | Check filename for special characters |
| `Could not open input file '%s'` | `0x1D34E10` | error | `sub_426570` | Input file `fopen` returns null | Verify file exists and is not locked |
| `Could not read file '%s'` | `0x1D34FE0` | error | main | I/O error during input read | Check disk and filesystem health |
| `Could not find fatbinary in '%s'` | `0x1D34C40` | error | main | Fatbinary not present in input file | Verify input was compiled with `-rdc` or `-dc` |
| `Library file '%s' not found in paths` | `0x1D34BF0` | error | main | `-l` library not found in any `-L` path | Add `-L<dir>` with the correct library directory |
| `Unsupported file type '%s'` | `0x1D34FAB` | fatal | `sub_427AE0` | Input is not ELF, archive, PTX, or fatbinary | Verify file type with `file <input>` |
| `No input files specified; use option --help for more information` | `0x1D34ED0` | fatal | main | No object files provided on command line | Provide at least one input `.o` file |
| `Too many options file opened '%s'` | `0x1D357A0` | warning | `sub_42EC10` | Response-file nesting exceeds 14 levels | Reduce `@file` nesting depth |
| `Failed to read contents of options file` | `0x1D357E8` | error | `sub_42EC10` | I/O error reading `@file` | Check response file path and permissions |
| `Could not open options file '%s'` | `0x1D35810` | error | `sub_42EC10` | Response file `fopen` failed | Verify `@file` path is accessible |
| `Skipping incompatible '%s' when searching for -l%s` | `0x1D34AB8` | warning | main | Library found but wrong architecture | Provide architecture-correct library |

### Category 2: Output File Errors

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `Must specify output file with -o option` | `0x1D34E30` | fatal | main | Missing `-o` flag | Add `-o <output>` to command line |
| `Could not open output file '%s'` | `0x1D34DC8` | error | main | Output path is not writable | Check write permissions on target directory |
| `Could not write file '%s'` | `0x1D34FC6` | error | main | I/O error during output write | Check disk space and filesystem |
| `Host linker script creation failed.` | `0x1D34E58` | error | main | Internal failure generating host linker script | Report as bug; verify toolkit installation |

### Category 3: Architecture and Compatibility Errors

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `Unknown arch name '%s'` | `0x1D39279` | fatal | `sub_427AE0` | The `-arch` value is not a recognized `sm_` target | Use a valid arch like `sm_70`, `sm_80`, `sm_90`, etc. |
| `SM Arch ('%s') must be >= 20` | `0x1D34F8E` | fatal | `sub_427AE0` | Target architecture too old | Use `sm_20` or newer |
| `SM Arch ('%s') not found in '%s'` | `0x1D34BC8` | error | main | Fatbinary does not contain code for the target arch | Recompile input for the correct `-arch` target |
| `Cannot target %s when input '%s' is SASS` | `0x1D34850` | error | `sub_427AE0` | SASS object cannot be retargeted to a different architecture | Recompile source for the desired target arch |
| `Input file '%s' arch does not match target '%s'` | — | error | `sub_426570` | Object compiled for wrong `sm_` target | Recompile with matching `-arch` flag |
| `Input file '%s' abi does not match` | `0x1D34C68` | error | `sub_426570` | ELF ABI version mismatch | Recompile with matching toolkit version |
| `Input file '%s' size does not match target '%s'` | `0x1D34C90` | error | `sub_426570` | 32-bit vs 64-bit object/target mismatch | Match `-m32`/`-m64` across all objects |
| `Input file '%s' ABI version '%u' is incompatible with target ABI version '%u'` | `0x1D34CF0` | error | `sub_426570` | Detailed ABI version incompatibility | Recompile with current toolkit |
| `Target format and SM Arch ('%u') mismatch` | `0x1D34D40` | error | `sub_426570` | Cubin format does not match SM generation | Verify `-arch` and `-code` flags are consistent |
| `Input file '%s' must be recompiled with toolkit >= Cuda 12.0` | `0x1D34AF0` | error | `sub_426570` | Object from pre-12.0 toolkit | Recompile with CUDA 12.0 or later |
| `Input file '%s' must be recompiled with toolkit >= Cuda 7.0` | `0x1D34B30` | error | `sub_426570` | Object from pre-7.0 toolkit | Recompile with CUDA 7.0 or later |
| `Object '%s' cannot be linked due to version mismatch. Objects using tcgen05 in 12.x cannot be linked with 13.0 or later, they must be rebuilt with latest compiler` | `0x1D39330` | error | main | tcgen05 objects from CUDA 12.x linked with 13.0+ | Rebuild all objects with CUDA 13.0+ toolkit |
| `Cannot link sanitized object '%s' from version %d with sanitized object from a different toolkit version (%d)` | `0x1D393D8` | error | main | Sanitizer-instrumented objects from different toolkit versions | Compile all sanitized objects with same toolkit |
| `For kernel functions with parameter size higher than 4k bytes on sm_7x and sm_8x, all objects must be compiled with 12.1 or later` | `0x1D39488` | error | main | Large kernel params require recent toolkit | Recompile all objects with CUDA 12.1+ |
| `%s '%s' is not compatible with driver version %s` | `0x1D3ADE0` | warning | `sub_451D80` | Output binary not driver-compatible | Update GPU driver or target older `sm_` arch |

### Category 4: Symbol Resolution Errors

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `Multiple definition of '%s' in '%s', first defined in '%s'` | `0x1D39A20` | error | `sub_440BE0` | Symbol multiply defined across objects | Remove duplicate definitions; use `static`/`__device__` scope |
| `Undefined reference to '%s' in '%s'` | `0x1D39A60` | error | `sub_445000` | Unresolved symbol at link time | Add the missing object file or library to the link |
| `Prototype doesn't match for '%s' in '%s', first defined in '%s'` | `0x1D398B0` | warning | `sub_450ED0` | Kernel parameter signature mismatch across TUs | Ensure consistent kernel declarations in all TUs |
| `Size doesn't match for '%s' in '%s', first specified in '%s'` | `0x1D398F0` | warning | `sub_450ED0` | Symbol size conflict between definitions | Check for struct layout differences between TUs |
| `Duplicate weak parameter bank for '%s' is not the same size` | `0x1D39690` | warning | `sub_437BB0` | Weak symbol parameter banks differ | Ensure weak symbols have consistent parameter sizes |
| `Cannot link shared object '%s' with partially linked kernel '%s'` | `0x1D396D0` | error | main | Shared object conflicts with partial link | Restructure the link to avoid mixing shared and partial objects |
| `Could not replace weak symbol '%s'` | `0x1D39888` | warning | `sub_450ED0` | Weak symbol replacement failure | Check for conflicting weak/global declarations |
| `Section '%s' not found in object '%s'` | `0x1D39610` | warning | `sub_451D80` | Expected ELF section missing from input | Verify input was compiled correctly for the link mode |
| `Output is limited to run on SM Arch '%u' only, due to missing attribute '%s' in input file '%s'` | `0x1D39550` | warning | main | Input file lacks feature attribute | Recompile input with current toolkit for full compatibility |
| `Output is limited to run on SM Arch '%u' only due to missing attribute '%s' in input file '%s'` | `0x1D395B0` | warning | main | Variant of above with slightly different punctuation | Same as above |

### Category 5: Resource Limit Errors

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `File uses too much global %s data (0x%llx bytes, 0x%x max)` | `0x1D39B88` | error | `sub_445000` | Global/constant memory overflow | Reduce global variable sizes; split across modules |
| `Entry function '%s' uses too much %s data (0x%llx bytes, 0x%x max)` | `0x1D39BC8` | error | `sub_445000` | Per-kernel resource limit exceeded | Reduce per-kernel memory usage |
| `More than %d %s used in entry function '%s'` | — | error | `sub_438DD0` | Register/barrier count exceeded | Reduce register pressure; use `__launch_bounds__` |
| `Entry function '%s' uses too much data for compiler-generated constants; please recompile with -Xptxas --disable-optimizer-constants` | `0x1D39B00` | error | `sub_445000` | Constant bank overflow from optimizations | Add `-Xptxas --disable-optimizer-constants` |
| `Stack size for entry function '%s' cannot be statically determined` | `0x1D39A88` | warning | `sub_44AD40` | Indirect calls prevent stack analysis | Use `--suppress-stack-size-warning` to silence |
| `Function '%s' uses %d bytes stack but limited to %d` | — | warning | `sub_44C030` | Stack usage exceeds budget | Reduce function stack usage or increase limit |

### Category 6: CLI Option Errors

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `Unknown option '%s'` | `0x1D34F45` | fatal | `sub_427AE0` | Unrecognized CLI flag | Check `nvlink --help` for valid options |
| `Invalid argument for option '%s'` | `0x1D34DE8` | error | `sub_427AE0` | Option value cannot be parsed | Provide a valid value for the option |
| `Conflicting options '%s' and '%s'` | `0x1D34BA0` | error | `sub_427AE0` | Mutually exclusive CLI flags | Remove one of the conflicting options |
| `Incompatible options '%s' and '%s', using '%s'` | `0x1D347C8` | warning | `sub_427AE0` | Options conflict but one takes precedence | Review option interactions |
| `option '%s' has been deprecated` | `0x1D357C8` | warning | `sub_427AE0` | Deprecated CLI option used | Migrate to the recommended replacement option |
| `incompatible redefinition for option '%s', the last value of this option was used` | `0x1D358E8` | warning | `sub_427AE0` | Option specified multiple times with different values | Specify each option only once |
| `--cuda-api-version major number must be == toolkit version` | `0x1D33DF0` | error | `sub_429BA0` | API version mismatch with toolkit | Use matching `--cuda-api-version` |
| `--cuda-api-version value cannot be parsed` | `0x1D33E30` | error | `sub_429BA0` | Malformed version string | Provide version as `major.minor` |
| `Option for ABI >= 8 is not compatible with -m32, hence ignored` | `0x1D34788` | warning | `sub_427AE0` | 32-bit mode incompatible with ABI 8+ | Use `-m64` for modern ABI levels |

### Category 7: LTO-Specific Errors

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `Ignoring -dlto option because no LTO objects found` | `0x1D34998` | warning | main | `-dlto` specified but no LTO inputs | Remove `-dlto` or ensure inputs contain NVVM IR |
| `Some objects do not have '%s' specified but others do; will build everything with '%s=%d'` | — | warning | main | Mixed compilation options across LTO objects | Compile all objects with the same options |
| `error in LTO callback` | `0x1D3425D` | fatal | main | `__nvvmHandle` callback returned error | Internal error; report bug with reproduction steps |
| `could not find __nvvmHandle` | `0x1D34241` | fatal | main | `dlsym` for `__nvvmHandle` failed | Verify `libnvvm.so` is from the correct toolkit version |
| `could not find CALLBACK Handle` | `0x1D345C0` | fatal | main | Second-level callback resolution failed | Same as above; toolkit installation issue |
| `Call to ptxjit failed in extended split compile mode` | `0x1D345E0` | fatal | main | Thread pool work item failed during split compile | Reduce parallelism or report bug |
| `Ptxjit compilation failed in extended split compile mode` | `0x1D34618` | fatal | main | PTX-to-SASS compilation failed in split mode | Check PTX compatibility with target arch |
| `Cannot allocate pthread data` | `0x1D342BE` | fatal | main | `malloc` failed for thread pool data | Increase system memory or reduce parallelism |
| `Unable to create thread pool` | `0x1D342DB` | fatal | main | Thread pool initialization failed | Check system thread limits (`ulimit -u`) |
| `unexpected object after cudadevrt` | `0x1D346D8` | fatal | main | Input ordering violation in LTO pipeline | Internal error; report with input files |

### Category 8: Warning-Level Diagnostics

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `Option '%s' is not fully implemented for gpu architecture '%s' and may not work as expected` | — | warning | `sub_427AE0` | Feature partially implemented for target | Check release notes for arch-specific feature support |
| `Option '%s' not supported for gpu architecture '%s'` | `0x1D348E0` | warning | `sub_427AE0` | Feature not available for target | Remove the unsupported option |
| `Input file '%s' newer than toolkit (%d vs %d)` | `0x1D34B70` | warning | `sub_426570` | Forward-compatibility concern | Update nvlink to match input file toolkit version |
| `Function '%s' and function '%s' have conflicting cache preference, falling back to use cache preference of entry '%s'` | `0x1D39760` | warning | `sub_451D80` | Cache preference conflict across functions | Use consistent cache preference attributes |

### Category 9: Internal Fatal Errors

These indicate linker bugs or unexpected states. They are routed through descriptor `unk_2A5B670` (severity 6, fatal) or `unk_2A5B990` (severity 6, internal assertion):

| Message | Addr | Source | Context |
|---|---|---|---|
| `Internal error: Aborting` | `0x1D39290` | various | Unrecoverable internal assertion failure |
| `Internal error: %s` | `0x1D39C80` | various | Internal assertion with detail string |
| `Internal FNLZR error '%s'` | `0x1D34F74` | `sub_4275C0` | Finalizer returned an error string |
| `Bailing out due to earlier errors` | `0x1D3B978` | main | Accumulated error count exceeded threshold |
| `merge_elf failed` | `0x1D34360` | main | ELF merge phase set fatal-error flag |
| `expected libcudadevrt object` | — | main | LTO pipeline expected cudadevrt but found different input |
| `linking with -ewp objects requires using current toolkit` | — | `sub_426570` | EWP objects from different toolkit version |
| `cubin not an elf?` | — | main | Input claimed to be cubin but failed ELF validation |
| `cubin not a device elf?` | — | main | ELF is valid but not a device ELF |
| `fatbin wrong format?` | — | main | Fatbinary header validation failed |
| `should never see bc files` | — | main | Unexpected bitcode file in input stream |
| `unexpected cpuArch` | `0x1D34002` | `sub_42A2D0` | Host CPU architecture not recognized |

### Category 10: External Tool Errors

| Message | Addr | Sev | Source | Trigger | User Fix |
|---|---|---|---|---|---|
| `exec() failed with '%s'` | `0x1D3B902` | fatal | `sub_42FA70` | External tool launch failed | Verify tool path and permissions |
| `wait() failed with '%s'` | `0x1D3B91A` | fatal | `sub_42FA70` | `waitpid` returned error | System-level error; check `ulimit` and process limits |
| `fork() failed with '%s'` | `0x1D3B932` | fatal | `sub_42FA70` | Process fork failed | Reduce system load or increase process limits |
| `Finalization failed '%s'` | `0x1DFCBC4` | error | finalizer | External finalizer returned error | Check target arch compatibility |

## Warning Suppression Flags

Several CLI options control diagnostic emission:

| Flag | Effect |
|---|---|
| `--disable-warnings` (`0x1D325F0`) | Suppress all warning diagnostics |
| `--Werror` / `--warning-as-error` (`0x1D3261E` / `0x1D32625`) | Promote all warnings to errors |
| `--suppress-stack-size-warning` (`0x1D32450`) | Suppress stack-size-related warnings |
| `--suppress-arch-warning` (`0x1D3246C`) | Suppress architecture mismatch warnings |
| `--extra-warnings` (`0x1D32735`) | Enable additional advisory warnings |
| `--allow-undefined-globals` (`0x1D325C3`) | Allow undefined globals and their relocations in linked executable |

## Error Flow Architecture

```text
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
    +-- code 4:  custom "elfLink found incompatible code in <file>" message
    +-- code 7:  conditional suppress (cudadevrt or -lto-allow-unresolved) or arch-specific fatal
    +-- other:   sub_4BC270(code) --> error string --> sub_467460 (fatal via unk_2A5B670)

Library Loading (main / sub_427A10)
    |
    +-- sub_4BC470  (dlopen libnvvm.so)      --> sub_4BC290 --> codes 0, 1, 10
    +-- sub_4BC4A0  (add module to program)  --> codes 0, 10, 11
    |
    v
    sub_4BC270(code) --> error string --> sub_467460 (fatal via unk_2A5B670)

LTO Compilation (sub_4BC6F0)
    |
    +-- nvvmCompileProgram via dlsym         --> code 8 (log + compile error)
    +-- nvvmGetProgramLog                    --> code 1 (API failure)
    +-- nvvmGetCompiledResult                --> code 10 (symbol lookup failure)
    |
    v
    Returns raw code to caller for sub_4BC270 translation

Split Compilation (main, thread pool)
    |
    +-- sub_43FDB0 / sub_43FF50             --> "Call to ptxjit failed"
    +-- sub_4264B0  (worker function)        --> per-item error codes
    +-- sub_4297B0  (per-result dispatch)    --> <lto ptx> as filename
    |
    v
    Per-item errors aggregated; fatal on first failure

ELF Merge + Layout + Relocate + Finalize (main phases)
    |
    +-- sub_439830  (merge)                  --> via unk_2A5B990 internal assertions
    +-- sub_469D60  (layout)                 --> resource limits (unk_2A5BA60..BA80)
    +-- sub_445000  (relocate)               --> undef refs, resource limits, section errors
    +-- sub_4475B0  (finalize / output)      --> write errors (unk_2A5B710)
```

## Key Implementation Details

**Secondary mapping table (`dword_1D48A50`):** Functions `sub_4BDAC0`, `sub_4BDAF0`, and `sub_4BDB30` use a 3-entry mapping table at `0x1D48A50` to convert their internal parser return codes (0, 1, 2) into elfLink codes (0, 2, 3). Values > 2 map to code 1 (internal error).

**setjmp/longjmp error recovery:** Several elfLink functions (`sub_4BC290`, `sub_4BC4A0`, `sub_4BD240`) use `setjmp`/`longjmp` for non-local error recovery. This allows deep call stacks within libnvvm to unwind cleanly back to the elfLink entry point. The longjmp path always returns code 1 (internal error), protecting the linker from crashes inside the dynamically loaded libnvvm.

**Code 7 suppression logic:** The NVVM error code (7) is the only error code with conditional suppression. The flag `byte_2A5F298` (`-lto-allow-unresolved`) and the filename check for `"cudadevrt"` together determine whether the error is fatal or silently ignored. This allows libcudadevrt's NVVM IR to pass through the non-LTO path without aborting. The check is performed identically in three functions: `sub_4297B0`, `sub_42A2D0`, and `sub_42AF40`.

**Code 4 custom message construction:** Rather than using the generic table string, code 4 constructs a message from two SSE-loaded 16-byte constants (`xmmword_1D34750`, `xmmword_1D34760`) forming the prefix `"elfLink found incompatible"`, followed by `" code in "` and the filename. This provides file-specific context that the generic string lacks. The 42-byte allocation (`strlen(filename) + 42`) accounts for the 33-byte prefix plus `" code in "` (9 bytes). This construction is duplicated in `sub_42A2D0`'s iterator loop.

**Multiple errors aggregation:** The string `"Multiple errors:\n"` at `0x1D486CE` indicates nvlink can aggregate multiple NVVM errors into a single diagnostic. The `nvvmGetErrorString` function (`0x1D487F1`) is resolved via `dlsym` to retrieve libnvvm-specific error details.

**Descriptor unk_2A5B990 dominance:** The internal-assertion descriptor `unk_2A5B990` accounts for 230 of the 380+ total `diag_emit` call sites. It serves as the universal internal-error descriptor (always fatal, severity 6). Every call site passes a literal string describing the violated invariant. 40+ unique assertion messages have been cataloged; see [Error Reporting](../infra/error-reporting.md) for the complete list.

## Complete String Address Index

All error-related strings confirmed in `nvlink_strings.json`, organized by address:

| Address | String |
|---|---|
| `0x1D321C0` | `specified arch exceeds buffer length` |
| `0x1D323AE` | `Internal error` |
| `0x1D33DF0` | `--cuda-api-version major number must be == toolkit version` |
| `0x1D33E30` | `--cuda-api-version value cannot be parsed` |
| `0x1D33E5F` | `unexpected data in module_ids` |
| `0x1D34002` | `unexpected cpuArch` |
| `0x1D34241` | `could not find __nvvmHandle` |
| `0x1D3425D` | `error in LTO callback` |
| `0x1D342BE` | `Cannot allocate pthread data` |
| `0x1D342DB` | `Unable to create thread pool` |
| `0x1D34360` | `merge_elf failed` |
| `0x1D345C0` | `could not find CALLBACK Handle` |
| `0x1D345E0` | `Call to ptxjit failed in extended split compile mode` |
| `0x1D34618` | `Ptxjit compilation failed in extended split compile mode` |
| `0x1D346D8` | `unexpected object after cudadevrt` |
| `0x1D34788` | `Option for ABI >= 8 is not compatible with -m32, hence ignored` |
| `0x1D347C8` | `Incompatible options '%s' and '%s', using '%s'` |
| `0x1D34850` | `Cannot target %s when input '%s' is SASS` |
| `0x1D348E0` | `Option '%s' not supported for gpu architecture '%s'` |
| `0x1D34998` | `Ignoring -dlto option because no LTO objects found` |
| `0x1D34AB8` | `Skipping incompatible '%s' when searching for -l%s` |
| `0x1D34AF0` | `Input file '%s' must be recompiled with toolkit >= Cuda 12.0` |
| `0x1D34B30` | `Input file '%s' must be recompiled with toolkit >= Cuda 7.0` |
| `0x1D34B70` | `Input file '%s' newer than toolkit (%d vs %d)` |
| `0x1D34BA0` | `Conflicting options '%s' and '%s'` |
| `0x1D34BC8` | `SM Arch ('%s') not found in '%s'` |
| `0x1D34BF0` | `Library file '%s' not found in paths` |
| `0x1D34C40` | `Could not find fatbinary in '%s'` |
| `0x1D34C68` | `Input file '%s' abi does not match` |
| `0x1D34C90` | `Input file '%s' size does not match target '%s'` |
| `0x1D34CF0` | `Input file '%s' ABI version '%u' is incompatible with target ABI version '%u'` |
| `0x1D34D40` | `Target format and SM Arch ('%u') mismatch` |
| `0x1D34DC8` | `Could not open output file '%s'` |
| `0x1D34DE8` | `Invalid argument for option '%s'` |
| `0x1D34E10` | `Could not open input file '%s'` |
| `0x1D34E30` | `Must specify output file with -o option` |
| `0x1D34E58` | `Host linker script creation failed.` |
| `0x1D34ED0` | `No input files specified; use option --help for more information` |
| `0x1D34F45` | `Unknown option '%s'` |
| `0x1D34F74` | `Internal FNLZR error '%s'` |
| `0x1D34F8E` | `SM Arch ('%s') must be >= 20` |
| `0x1D34FAB` | `Unsupported file type '%s'` |
| `0x1D34FC6` | `Could not write file '%s'` |
| `0x1D34FE0` | `Could not read file '%s'` |
| `0x1D357A0` | `Too many options file opened '%s'` |
| `0x1D357C8` | `option '%s' has been deprecated` |
| `0x1D357E8` | `Failed to read contents of options file` |
| `0x1D35810` | `Could not open options file '%s'` |
| `0x1D358E8` | `incompatible redefinition for option '%s', the last value of this option was used` |
| `0x1D39250` | `Cannot open file '%s'` |
| `0x1D39266` | `Bad file name '%s'` |
| `0x1D39279` | `Unknown arch name '%s'` |
| `0x1D39290` | `Internal error: Aborting` |
| `0x1D39330` | `Object '%s' cannot be linked due to version mismatch...` |
| `0x1D393D8` | `Cannot link sanitized object '%s' from version %d...` |
| `0x1D39488` | `For kernel functions with parameter size higher than 4k bytes...` |
| `0x1D39550` | `Output is limited to run on SM Arch '%u' only, due to missing attribute...` |
| `0x1D395B0` | `Output is limited to run on SM Arch '%u' only due to missing attribute...` |
| `0x1D39610` | `Section '%s' not found in object '%s'` |
| `0x1D39690` | `Duplicate weak parameter bank for '%s' is not the same size` |
| `0x1D396D0` | `Cannot link shared object '%s' with partially linked kernel '%s'` |
| `0x1D39760` | `Function '%s' and function '%s' have conflicting cache preference...` |
| `0x1D39888` | `Could not replace weak symbol '%s'` |
| `0x1D398B0` | `Prototype doesn't match for '%s' in '%s', first defined in '%s'` |
| `0x1D398F0` | `Size doesn't match for '%s' in '%s', first specified in '%s'` |
| `0x1D39A20` | `Multiple definition of '%s' in '%s', first defined in '%s'` |
| `0x1D39A60` | `Undefined reference to '%s' in '%s'` |
| `0x1D39A88` | `Stack size for entry function '%s' cannot be statically determined` |
| `0x1D39B00` | `Entry function '%s' uses too much data for compiler-generated constants...` |
| `0x1D39B88` | `File uses too much global %s data (0x%llx bytes, 0x%x max)` |
| `0x1D39BC8` | `Entry function '%s' uses too much %s data (0x%llx bytes, 0x%x max)` |
| `0x1D39C80` | `Internal error: %s` |
| `0x1D3ADE0` | `%s '%s' is not compatible with driver version %s` |
| `0x1D3B902` | `exec() failed with '%s'` |
| `0x1D3B91A` | `wait() failed with '%s'` |
| `0x1D3B932` | `fork() failed with '%s'` |
| `0x1D3B978` | `Bailing out due to earlier errors` |
| `0x1D486CE` | `Multiple errors:` |
| `0x1D48760` | `elfLink: unexpected error` |
| `0x1D487F1` | `nvvmGetErrorString` |
| `0x1D4883F` | `elfLink internal error` |
| `0x1D48856` | `elfLink error on cubin` |
| `0x1D4886D` | `elfLink fatbinary error` |
| `0x1D48885` | `elfLink memory error` |
| `0x1D4889A` | `elfLink JIT compile failed` |
| `0x1D488B5` | `elfLink JIT link failed` |
| `0x1D488CD` | `elfLink nvvm error` |
| `0x1D488E0` | `elfLink error cubin not relocatable` |
| `0x1D48908` | `elfLink error cubin not compatible` |
| `0x1D48930` | `elfLink cubin arch not compatible` |
| `0x1D48958` | `elfLink linker library load error` |
| `0x1D48980` | `elfLink error incompatible formats` |
| `0x1D489A8` | `elfLink error finalization failed` |

## Cross-References

**Internal (nvlink wiki):**

- [Error Reporting](../infra/error-reporting.md) — `diag_emit` (`sub_467460`) diagnostic pipeline with the complete 88-descriptor catalog and 40+ internal assertion messages
- [LTO Overview](../lto/overview.md) — LTO pipeline context where elfLink codes 5--7 arise from NVVM IR compilation
- [libnvvm Integration](../lto/libnvvm-integration.md) — `dlsym`/`dlopen` lifecycle for `libnvvm.so` loading (codes 10--11)
- [Split Compilation](../lto/split-compilation.md) — LTO split-compile path where elfLink errors propagate from parallel PTX-to-SASS workers
- [Fatbin Extraction](../input/fatbin-extraction.md) — Fatbinary parsing path where code 3 (fatbin error) originates
- [Cubin Loading](../input/cubin-loading.md) — Input cubin validation where codes 8 (not relocatable) and 9 (not compatible) originate
- [Architecture Compatibility](../targets/compatibility.md) — Arch mismatch checks that produce code 10 (arch not compatible)
- [Pipeline Entry](../pipeline/entry.md) — `main()` top-level error handling that translates elfLink codes to user diagnostics
- [Symbol Resolution](../linker/symbol-resolution.md) — Symbol merge/resolve logic that produces multiple-definition and undefined-reference errors
- [Dead Code Elimination](../linker/dead-code-elimination.md) — DCE callgraph logic that can trigger "reference to deleted symbol" assertions

## Confidence Assessment

| Aspect | Confidence | Basis |
|---|---|---|
| elfLink error strings (all 14 codes + fallback) | HIGH | All 14 strings found verbatim in `nvlink_strings.json` at addresses `0x1D4883F`--`0x1D489A8` plus fallback at `0x1D48760`; exact match confirmed |
| Lookup function (`sub_4BC270` at `0x4BC270`) | HIGH | Decompiled function confirms: `if (a1 <= 0xD) return off_1D489E0[a1]; return "elfLink: unexpected error";` |
| Error table address (`off_1D489E0`) | HIGH | Confirmed in decompiled `sub_4BC270`; 14 `const char*` entries |
| Caller sites (5 callers) | HIGH | 5 callers confirmed via grep of decompiled/ for `sub_4BC270`: `sub_4297B0`, `sub_42AF40`, `sub_42A2D0`, `sub_427A10`, and `main` (at two sites within main) |
| Code 4 / Code 7 special handling | HIGH | Conditional branches in decompiled `sub_4297B0` and `sub_42A2D0` explicitly check `error_code == 4` and `error_code == 7` with distinct handling paths |
| Secondary mapping table (`dword_1D48A50`) | HIGH | Referenced in decompiled `sub_4BDAC0`/`sub_4BDAF0`/`sub_4BDB30` as a 3-entry index mapping |
| User-facing diagnostic catalog (90+ messages) | HIGH | All strings found in `nvlink_strings.json`; source functions confirmed via grep of 380+ `sub_467460` call sites across decompiled code |
| Severity level prefixes | HIGH | Prefix strings (`warning `, `info    `, `error   `, `error*  `, `fatal   `) confirmed at `0x1D3C660`--`0x1D3C690` |
| Descriptor-to-severity mapping | HIGH | 88 descriptors in BSS range `0x2A5B530`--`0x2A5BB70` confirmed; severity inferred from `diag_emit` dispatch logic and caller behavior |
| Version-mismatch errors (tcgen05, sanitizer, 4k params) | HIGH | Strings at `0x1D39330`, `0x1D393D8`, `0x1D39488` confirmed; these are CUDA 13.0 additions |
| Symbolic names (`ELFLINK_OK`, etc.) | LOW | Inferred from string content; no symbol table or debug info in the binary contains these names |
| Error flow architecture diagram | MEDIUM | Call chain reconstructed from decompiled code; covers primary paths but may miss indirect callers through function pointers |
| Split-compile error paths | MEDIUM | Thread pool error propagation confirmed via `sub_43FF50` call sites; per-worker error codes routed through `sub_4297B0` |
