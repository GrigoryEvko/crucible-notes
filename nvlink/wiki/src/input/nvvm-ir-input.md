# NVVM IR / LTO IR Input

When nvlink is invoked with `-lto`, input files carrying NVVM IR bitcode are not linked directly. Instead, they are collected into a per-program IR module pool, tracked with per-module compiler option metadata, and later fed to the embedded libNVVM compiler for whole-program or partial LTO compilation. The IR arrives either as standalone `.nvvm` / `.ltoir` files on the command line or as type-8 members extracted from fatbin containers. Both paths converge on `sub_427A10` (register IR module), which delegates to `sub_4BC4A0` (add module to libNVVM program) for the actual handoff. The compiled result is produced later in the pipeline by `sub_4BC6F0` (compile and extract), which drives the full `nvvmCompileProgram` / `nvvmGetCompiledResult` cycle. The bytes returned by libNVVM are PTX assembly text, which is then fed to the embedded ptxas driver (`sub_4BD4E0` for whole-program, `sub_4BD760` for per-split relocatable compile) to produce the final cubin(s) that proceed into the ELF merge phase.

| | |
|---|---|
| **File extensions** | `.nvvm`, `.ltoir` |
| **Magic number** | `0x1EE55A01` (4 bytes, little-endian: `01 5A E5 1E`) stored as decimal `518347265` in the decompiled source |
| **Padded variant** | 4 zero bytes followed by `0x1EE55A01` at byte offset 4 (fatbin-wrapped form) |
| **Required flag** | `-lto` (`byte_2A5F288`) -- absent triggers fatal error |
| **Registration function** | `sub_427A10` at `0x427A10` |
| **Module add function** | `sub_4BC4A0` at `0x4BC4A0` (2,548 bytes) |
| **Program create function** | `sub_4BC290` at `0x4BC290` (2,475 bytes) |
| **Compile + extract function** | `sub_4BC6F0` at `0x4BC6F0` (13,602 bytes) |
| **Option collection function** | `sub_426CD0` at `0x426CD0` (7,040 bytes) |
| **Module counter** | `dword_2A5F280` -- count of non-libdevice IR modules |
| **PTX emit (whole-program)** | `sub_4BD4E0` at `0x4BD4E0` -- feeds whole LTO PTX through embedded ptxas |
| **PTX emit (per-split)** | `sub_4BD760` at `0x4BD760` -- per-thread ptxas invocation during split compile |
| **Split-compile work item** | `sub_4264B0` at `0x4264B0` -- thread-pool callback that unpacks the work item and calls `sub_4BD760` |

## Detection

NVVM IR can enter the linker through two dispatch layers. The outer layer runs in `main()` on each command-line input and dispatches purely on the file-name extension. The inner layer runs inside the embedded ptxas engine (`sub_4CE070`) and dispatches on the content magic; this layer is only reached when a fatbin member is extracted or when the embedded ptxas driver is invoked directly. All NVVM/LTO IR processing is gated on the `-lto` flag (`byte_2A5F288`).

### Outer Dispatch (`main()`)

The top of the input loop in `main()` (`main_0x409800.c`, around line 608) opens each file with `fopen(path, "rb")`, reads a 56-byte header probe via `fread(ptr, 1, 0x38, f)`, and closes the file. The probe is used only for the `sub_487A90` archive magic check (`!<arch>\n` / `!<thin>\n`). Classification then proceeds by extension as extracted by `sub_462620` (path splitter):

| Extension | Branch | Destination |
|---|---|---|
| `cubin` | line ~640 | `sub_4275C0` uplift if needed, then `sub_426570` / `sub_42A680` into the object list |
| `ptx` | line ~681 | Whole buffer loaded via `sub_476BF0(path, 1)`, then `sub_4BD760` (embedded ptxas) to produce a cubin |
| `fatbin` | line ~740 | Assert `ptr[0] == 0xBA55ED50` (32-bit magic), then `sub_42AF40` for member-by-member dispatch |
| `nvvm` | line 761 | `sub_427A10` to register the IR module with the LTO program |
| `ltoir` | line 761 | Same as `nvvm` (identical file format, different conventional suffix) |
| `bc` | line 780 | Fatal: `"should never see bc files"` (no support for unwrapped LLVM bitcode) |
| _other_ | line ~789 | Archive probe via `sub_487A90`; otherwise generic ELF via `sub_4BDB70` |

The `main()` dispatch code for the IR case reads literally (line 761):

```c
if (!strcmp(ext, "nvvm") || !strcmp(ext, "ltoir")) {
    if (!byte_2A5F288)
        fatal_error("should only see nvvm files when -lto", ...);
    void *buf  = sub_476BF0(path, 0);        // load entire file into arena
    size_t sz  = returned_length;
    sub_427A10(lto_ctx, buf, sz, path);      // register IR module
    goto LABEL_133;
}
```

The buffer loader `sub_476BF0(path, 0)` slurps the whole file into an arena-allocated block (the second parameter selects binary vs text mode) and returns the pointer plus length. No magic check happens at this layer; the extension alone determines the route.

### Inner Dispatch (`sub_4CE070`)

The content-magic classification lives inside the embedded ptxas engine and is called by the fatbin extractor (`sub_42AF40` -> `sub_4BD240`) and by the ptxas driver entry points (`sub_4BD0A0`, `sub_4BD4E0`, `sub_4BD760`). `sub_4CE070` stores the input pointer at offset +72 of the 168-byte ptxas context and writes an integer type tag to offset +80:

| Content match | Type tag (ctx+80) | Notes |
|---|---|---|
| First qword masked to 48 bits == `0x1BA55ED50` | 2 | Fatbin (but flagged differently from the outer-loop fatbin path) |
| ELF header + `e_machine == 0xBE` (`EM_CUDA`) | 3 | Cubin (device ELF) |
| First dword == `0x1EE55A01` | 1 | NVVM IR / LTO IR (unpadded) |
| Bytes 0-3 are zero AND bytes 4-7 == `0x1EE55A01` | 1 | NVVM IR / LTO IR (padded variant used inside fatbin envelopes) |
| `sub_4CDF80` matches -- whitespace-skipped prefix `.version` | 4 | PTX source text |
| None of the above | error 30675157 | "unsupported input format" diagnostic |

The padded-variant test encodes the alternative as a 64-bit comparison: `LODWORD(*p) == 0x1EE55A01 || (LODWORD(*p) == 0 && HIDWORD(*p) == 0x1EE55A01)`. This accommodates IR modules that are embedded in fatbin members with a 4-byte alignment pad before the magic.

Important: the type tag `1` returned by `sub_4CE670` at this layer is NOT the same as the fatbin-member type tag `8` used in the outer loop's `sub_42AF40` switch. `sub_42AF40` classifies fatbin members using a dedicated header parser (`sub_4BD0A0` -> `sub_4CE670`) that returns the fatbin-member type, where `8` is NVVM IR and `16` is Mercury IR. The integer space is internal to each layer.

### Fatbin Container Route

When a fatbin member is classified as type 8 (NVVM IR) by the member parser, the extraction dispatch in `sub_42AF40` calls `sub_4BC4A0` directly (not `sub_427A10`) to register it. The reason is that the fatbin path also needs to parse the embedded `-Xnvvm`-style option string from the IR module header to update the per-module consensus tracking state. The `sub_42AF40` body for case `v77 == 8` does roughly (lines 244-520):

```c
// 1. Save IR buffer pointer if this module is libcudadevrt (handled specially)
if (strstr(path, "cudadevrt")) {
    *libdevrt_buf_out = ir_buf;
    libdevrt_buf_len_out = ir_len;
    /* resolved later in main() after all real inputs are registered */
    return;
}

// 2. Require -lto flag
if (!byte_2A5F288)
    fatal_error("should only see nvvm files when -lto");

// 3. Add module to libNVVM program
unsigned int rc = sub_4BC4A0(lto_ctx, ir_buf, ir_len, path);
if (rc) fatal_error(sub_4BC270(rc));

// 4. Increment non-libdevice module counter
if (!strstr(path, "nvvm") || !strstr(path, "libdevice"))
    ++dword_2A5F280;

// 5. Verbose-keep trace
if (byte_2A5F29B)
    printf("nvlink -lto-add-module %s.nvvm\n", path);

// 6. Parse embedded option string via sub_4BD1F0 and update consensus machine
char *opt_str = NULL;
sub_4BD1F0(&opt_str, ctx, header_ptr);
/* scan opt_str for: -ftz=, -prec_div=, -prec_sqrt=, -fmad=,
                     -maxreg , -split-compile , -generate-line-info, -inline-info */
```

The scanned option substrings update eight consensus state machines (`dword_2A5F270`, `dword_2A5F26C`, `dword_2A5F268`, `dword_2A5F264`, `dword_2A5F250`, `dword_2A5F260`, `dword_2A5F248`, `dword_2A5F240`). Each machine has four states (see "Per-Module Option Consensus" below).

The `cudadevrt` branch is the reason that fatbin-route extraction skips `sub_427A10` and calls `sub_4BC4A0` directly: libcudadevrt IR must be stashed and injected last, after the main-input pass has decided whether any user module actually references cudadevrt. This deferred registration happens in `main()` around line 922-938 via a direct `sub_427A10(lto_ctx, libdevrt_buf, len, "libcudadevrt")` call.

## Module Registration: sub\_427A10

| | |
|---|---|
| **Address** | `0x427A10` |
| **Size** | ~208 bytes |
| **Signature** | `int register_ir_module(void *ir_data, size_t ir_size, void *lto_ctx, const char *name, int flags, int extra)` |

This function is the single entry point for adding an IR module to the LTO program. It is called once per `.nvvm` / `.ltoir` input file and once per type-8 fatbin member.

```c
// Reconstructed from sub_427A10
int register_ir_module(void *ir_data, size_t ir_size, void *lto_ctx,
                       const char *name, int flags, int extra) {
    // If -lto flag not set, emit fatal error
    if (!g_lto_flag)                               // byte_2A5F288
        fatal_error(&err_nvvm_requires_lto, ...);  // "should only see nvvm files when -lto"

    // Add module to the libNVVM program handle
    unsigned int rc = nvvm_add_module(ir_data, ir_size, lto_ctx, name);  // sub_4BC4A0
    if (rc) {
        int err_code = nvvm_translate_error(rc);   // sub_4BC270
        fatal_error(&err_nvvm_add_failed, err_code, ...);
    }

    // Check if this module is libdevice (libdevice IR excluded from count)
    char *p = strstr(name, "nvvm");
    if (p && strstr(name, "libdevice")) {
        // libdevice module -- do not increment module counter
    } else {
        ++g_nvvm_module_count;                     // dword_2A5F280
    }

    // Verbose-keep mode: print registration trace
    if (g_verbose_keep)                            // byte_2A5F29B
        printf("nvlink -lto-add-module %s.nvvm\n", name);

    return 0;
}
```

Key behaviors:

- **Fatal without `-lto`**: If `byte_2A5F288` is not set, the error `"should only see nvvm files when -lto"` is raised through `sub_467460`. This is a hard requirement -- there is no fallback.
- **libdevice exclusion**: Modules whose path contains both `"nvvm"` and `"libdevice"` are excluded from `dword_2A5F280`. This counter drives the whole-vs-partial LTO decision: if zero non-libdevice IR modules remain after dead code elimination, LTO compilation is skipped.
- **Verbose trace**: Under `--verbose-keep` (`byte_2A5F29B`), the line `nvlink -lto-add-module <name>.nvvm` is printed to stdout, recording every IR module added to the LTO program.

## Adding a Module to libNVVM: sub\_4BC4A0

| | |
|---|---|
| **Address** | `0x4BC4A0` |
| **Size** | 2,548 bytes / 112 lines |
| **Signature** | `unsigned int nvvm_add_module(void *ir_data, size_t ir_size, void *lto_ctx, const char *name)` |

This function mediates between nvlink and the dynamically loaded libNVVM. It does not compile anything; it only registers IR bytes with the NVVM program handle for later batch compilation.

```c
// Reconstructed from sub_4BC4A0
unsigned int nvvm_add_module(void *ir_data, size_t ir_size,
                             void *lto_ctx, const char *name) {
    // Save/restore error handler state (setjmp/longjmp pattern)
    char *handler = get_error_handler(ir_data, ir_size);   // sub_44F410
    // ... save handler state ...

    if (setjmp(env))
        return handler_error_code;      // longjmp target: return error from callback

    // Resolve __nvvmHandle from the loaded libnvvm.so
    void *lib_handle = *(void **)(lto_ctx + 640);
    auto fn_handle = (func_ptr)dlsym(lib_handle, "__nvvmHandle");
    if (!fn_handle) {
        // "__nvvmHandle" not found in libnvvm.so
        return 10;                      // error: symbol resolution failed
    }

    // Call __nvvmHandle(8320) to get the "add module" callback
    auto add_fn = fn_handle(8320);
    if (!add_fn) {
        return 10;                      // error: handle dispatch returned NULL
    }

    // Call the add-module callback with the NVVM program handle and IR data
    void *program_handle = *(void **)(lto_ctx + 648);
    if (add_fn(program_handle, ir_size, lto_ctx, name)) {
        return 11;                      // error: add-module failed
    }

    return 0;                           // success
}
```

### The \_\_nvvmHandle Dispatch Mechanism

libNVVM does not expose its internal API functions directly. Instead, it exports a single dispatcher function `__nvvmHandle` that takes an integer opcode and returns a function pointer for the requested operation. This is a form of vtable-like dispatch that allows the library to version its internal API without changing the export table.

Opcodes observed in nvlink:

| Opcode | Returns | Used in |
|---|---|---|
| 8320 | Add-module callback | `sub_4BC4A0` -- registers an IR module with the program |
| 45242 | Get-module-list callback | `sub_4BC6F0` -- retrieves compiled module list |
| 61453 | Get-variable-count callback | `sub_4BC6F0` -- queries per-module variable counts |

The program handle is stored at offset 648 within the LTO context structure. The library handle (from `dlopen`) is at offset 640.

## Program Creation: sub\_4BC290

| | |
|---|---|
| **Address** | `0x4BC290` |
| **Size** | 2,475 bytes / 100 lines |
| **Signature** | `unsigned int nvvm_create_program(void *lto_ctx, size_t unused, void *lib_handle)` |

Before any modules can be added, the NVVM program must be created. This function is called once during LTO initialization, before the first `sub_427A10` call.

```c
// Reconstructed from sub_4BC290
unsigned int nvvm_create_program(void *lto_ctx, size_t unused, void *lib_handle) {
    if (!lto_ctx) return 1;

    // Already initialized? Return success (idempotent)
    if (*(void **)(lto_ctx + 640))
        return 0;

    if (!lib_handle) return 10;

    // Store the library handle
    *(void **)(lto_ctx + 640) = lib_handle;

    // Resolve nvvmCreateProgram from libnvvm.so
    auto create_fn = (func_ptr)dlsym(lib_handle, "nvvmCreateProgram");
    if (!create_fn) {
        return 10;                      // error: symbol not found
    }

    // Create the NVVM program, storing the handle at lto_ctx + 648
    if (create_fn(lto_ctx + 648)) {
        return 1;                       // error: creation failed
    }

    return 0;
}
```

The function uses `dlsym` directly for `nvvmCreateProgram` -- this is one of the few libNVVM API functions resolved by name rather than through `__nvvmHandle`. The reason is that program creation must happen before the dispatcher is available.

## LTO Option Collection: sub\_426CD0

| | |
|---|---|
| **Address** | `0x426CD0` |
| **Size** | 7,040 bytes / 275 lines |
| **Signature** | `char **collect_lto_options(void *linker_state, void **module_list, int *option_count)` |

After all IR modules are registered, `main()` calls this function to assemble the compiler options array that will be passed to `nvvmCompileProgram`. The function builds an option list in a linked-list accumulator (`sub_464AE0` creates, `sub_464C30` appends), then converts it to a flat `char **` array.

### Fixed Options (Always Added)

| Option string | Source |
|---|---|
| `-arch=compute_<N>` | `dword_2A5F314` (SM version number) |
| `-link-lto` | Always present (this is an LTO compilation) |
| `-split-compile-extended=<N>` | `dword_2A5B514` if not 1 (split-compile thread count) |
| `-split-compile=<N>` | `dword_2A5B518` if not 1 (split-compile NVVM threads) |
| `-Ofast-compile=<level>` | `qword_2A5F258` ("min", "mid", or "max") |
| `-maxreg=<N>` | `dword_2A5F22C` if > 0 |
| `-generate-line-info` | `byte_2A5F24C` if set |
| `-inline-info` | `byte_2A5F244` if set |
| `--device-c` | `byte_2A5F286` if set (relocatable compile) |
| `--force-device-c` | `byte_2A5F285` if set (force partial LTO) |
| `-g` | `byte_2A5F310` if set (debug info) |
| `-has-global-host-info` | `byte_2A5F214` AND `byte_2A5F211` if set |

### Per-Module Consensus Options

Four floating-point math options are tracked across all IR modules using a consensus state machine (documented in the fatbin extraction page). If the user has not overridden them via `-Xnvvm`, their values are injected from the consensus:

| Option | Consensus state global | Consensus value global |
|---|---|---|
| `-ftz=<0\|1>` | `dword_2A5F270` | `dword_2A5F274` |
| `-prec-div=<0\|1>` | `dword_2A5F26C` | `dword_2A5B524` |
| `-prec-sqrt=<0\|1>` | `dword_2A5F268` | `dword_2A5B520` |
| `-fma=<0\|1>` | `dword_2A5F264` | `dword_2A5B51C` |

The consensus logic: for each option, as IR modules are added, the fatbin extraction code records whether the option is present and what value it carries. If all modules agree on a value, that value is used. If modules disagree, nvlink falls back to a default. The `-prec-sqrt` option is always emitted (even without consensus checking) because it must be explicitly set for deterministic math behavior.

### User-Supplied Options (-Xnvvm)

Options passed via `-Xnvvm` on the nvlink command line are stored in `qword_2A5F230`. During option collection, `sub_426CD0` iterates these user-supplied options and appends them to the option array. However, it applies deduplication: if a user-supplied option matches one of the fixed options already added (like `-link-lto`, `-generate-line-info`, `-inline-info`, `--device-c`, `--force-device-c`, `-g`, `-Ofast-compile=*`, `-compile-time`, or `-has-global-host-info`), the duplicate is suppressed.

The user-supplied `-Xnvvm` options are also scanned for `-ftz=`, `-prec-div=`, `-prec-sqrt=`, and `-fma=` prefixes. If any of these are found in the user options, the corresponding consensus-derived value is NOT injected -- the user's explicit setting takes priority.

## Compilation and Result Extraction: sub\_4BC6F0

| | |
|---|---|
| **Address** | `0x4BC6F0` |
| **Size** | 13,602 bytes / 489 lines |
| **Signature** | `unsigned int nvvm_compile_and_extract(void *result_buf, size_t *result_size, void **module_list, void *lto_ctx, bool *success_flag, void **error_msg, void *linker_state, unsigned int option_count, char **options)` |

This is the largest function in the libNVVM integration layer. It drives the entire compilation-and-extraction cycle by resolving and calling nine libNVVM API functions through `dlsym`.

### API Function Resolution

All nine functions are resolved from the library handle at `lto_ctx + 640` via `dlsym` at the start of `sub_4BC6F0`. If any resolution fails, the function returns 10 immediately (cannot proceed without the full API).

| dlsym name | Description |
|---|---|
| `nvvmCompileProgram` | Compiles all registered IR modules |
| `nvvmGetCompiledResultSize` | Queries the byte size of compiled output |
| `nvvmGetCompiledResult` | Copies compiled bytes into a caller-provided buffer |
| `nvvmGetErrorString` | Translates an NVVM error code to human-readable text |
| `nvvmGetProgramLogSize` | Queries the byte size of the compilation log |
| `nvvmGetProgramLog` | Copies the log (warnings, errors) into a buffer |
| `nvvmDestroyProgram` | Destroys the NVVM program handle and frees resources |
| `__nvvmHandle(45242)` | Returns callback for retrieving the compiled module list |
| `__nvvmHandle(61453)` | Returns callback for querying per-module variable counts |

### Compilation Flow

```c
// Reconstructed high-level flow of sub_4BC6F0
unsigned int nvvm_compile_and_extract(
    void *result_buf, size_t *result_size, void **module_list,
    void *lto_ctx, bool *success_flag, void **error_msg,
    void *linker_state, unsigned int option_count, char **options)
{
    // 1. Resolve all 9 API functions (return 10 if any missing)
    // ... dlsym calls ...

    // 2. Build the options array
    //    Copies user options, appends "--force-device-c" scan flag,
    //    appends host-ref options for external/internal kernels/constants/globals:
    //      -host-ref-ek=<value>   (external kernel refs)
    //      -host-ref-ik=<value>   (internal kernel refs)
    //      -host-ref-ec=<value>   (external constant refs)
    //      -host-ref-ic=<value>   (internal constant refs)
    //      -host-ref-eg=<value>   (external global refs)
    //      -host-ref-ig=<value>   (internal global refs)
    //    Appends "-variables" if variables-used tracking is active

    // 3. Call nvvmCompileProgram(program_handle, option_count, options)
    unsigned int rc = nvvmCompileProgram(program_handle, option_count, options);

    // 4. Interpret the result code
    if (rc == 100) {
        *success_flag = false;        // special code: "no output needed"
    } else if (rc == 0) {
        *success_flag = true;         // compilation succeeded
    } else {
        // rc != 0 and rc != 100: compilation error
        const char *err_str = nvvmGetErrorString(rc);
        *error_msg = err_str;
    }

    // 5. Retrieve compilation log (always, even on success -- may contain warnings)
    size_t log_size;
    nvvmGetProgramLogSize(program_handle, &log_size);
    if (log_size > 1) {
        char *log_buf = arena_alloc(log_size);
        nvvmGetProgramLog(program_handle, log_buf);
        // If there was also an error, concatenate log + error string
        if (error) {
            char *combined = arena_alloc(strlen(log_buf) + strlen(err_str) + 1);
            strcpy(combined, log_buf);
            strcat(combined, err_str);
            *error_msg = combined;
            return 8;
        }
        *error_msg = log_buf;
    } else if (error) {
        return 8;                     // error with no log
    }

    // 6. Retrieve compiled result
    nvvmGetCompiledResultSize(program_handle, result_size);

    // 7. Query per-module variable counts (via __nvvmHandle(61453))
    unsigned int var_count;
    get_variable_count(program_handle, &var_count);
    if (var_count > 1) {
        void **var_list = arena_alloc(8 * var_count);
        *module_list = var_list;
        get_module_list(program_handle, &var_count, var_list);  // __nvvmHandle(45242)
    }

    // 8. Copy compiled result into caller's buffer
    void *compiled = arena_alloc(*result_size);
    *result_buf = compiled;
    nvvmGetCompiledResult(program_handle, compiled);

    // 9. Destroy the program handle
    return nvvmDestroyProgram(&program_handle) != 0;
}
```

### Host Reference Options

When `linker_state + 97` is set (host-info mode is active) and no `--force-device-c` flag appears in the user options, six host-reference options are conditionally appended. These pass symbol reference lists from the host compilation to the NVVM compiler so it can perform cross-module dead code elimination:

| Option prefix | Offset in linker state | Meaning |
|---|---|---|
| `-host-ref-ek=` | +520 | External kernel references |
| `-host-ref-ik=` | +528 | Internal kernel references |
| `-host-ref-ec=` | +536 | External constant references |
| `-host-ref-ic=` | +544 | Internal constant references |
| `-host-ref-eg=` | +552 | External global references |
| `-host-ref-ig=` | +560 | Internal global references |

Each value is retrieved via `sub_43FBC0` and only appended if non-NULL. After the option string is constructed (e.g., `"-host-ref-ek=__cuda_module_0_kernel_list"`), the temporary source string is freed with `arena_free`.

### Return Codes

| Code | Meaning |
|---|---|
| 0 | Compilation succeeded, result extracted, program destroyed |
| 1 | API function call failed (result retrieval, variable query, or destroy) |
| 8 | Compilation error -- `*error_msg` contains the diagnostic text |
| 10 | One or more libNVVM API functions could not be resolved via `dlsym` |

The special `nvvmCompileProgram` return code 100 is handled as a "no output" signal -- `*success_flag` is set to false but no error is raised. This occurs when the compiler determines all input is dead code and there is nothing to emit.

## Per-Module Option Consensus

When IR modules are extracted from fatbin containers, `sub_42AF40` parses embedded option strings from the IR module metadata. Each module may carry its own compilation settings for floating-point behavior. nvlink implements a four-state consensus machine to reconcile these across all modules:

| State | Value | Meaning |
|---|---|---|
| 0 | Not seen | No module has provided this option yet |
| 1 | Present | First module provided a value |
| 2 | Agreed | Multiple modules, all with the same value |
| 3 | Conflict | Modules disagree -- fall back to default |

The eight tracked options and their global variable pairs:

| Option | State global | Value global |
|---|---|---|
| `-ftz` | `dword_2A5F270` | `dword_2A5F274` |
| `-prec_div` | `dword_2A5F26C` | `dword_2A5B524` |
| `-prec_sqrt` | `dword_2A5F268` | `dword_2A5B520` |
| `-fmad` | `dword_2A5F264` | `dword_2A5B51C` |
| `-maxreg` | `dword_2A5F250` | `dword_2A5F254` |
| `-split-compile` | `dword_2A5F260` | `dword_2A5B518` |
| `-generate-line-info` | `dword_2A5F248` | `byte_2A5F24C` |
| `-inline-info` | `dword_2A5F240` | `byte_2A5F244` |

The consensus values are consumed by `sub_426CD0` during option collection. When the consensus state is "agreed" (2), the agreed-upon value is injected into the nvvmCompileProgram options. When the state is "conflict" (3), a fallback default is used.

## LTO Context Structure Layout

The LTO context object is passed through most functions in this subsystem. Key field offsets:

| Offset | Type | Field | Description |
|---|---|---|---|
| +97 | byte | host_info_mode | Host-info mode active (controls host-ref option injection) |
| +98 | byte | variables_used | Variables-used tracking active (adds `-variables` option) |
| +480 | structure | string_table | String table context (used by `sub_4644C0` for log buffering) |
| +520 | pointer | host_ref_ek | External kernel reference list |
| +528 | pointer | host_ref_ik | Internal kernel reference list |
| +536 | pointer | host_ref_ec | External constant reference list |
| +544 | pointer | host_ref_ic | Internal constant reference list |
| +552 | pointer | host_ref_eg | External global reference list |
| +560 | pointer | host_ref_ig | Internal global reference list |
| +640 | pointer | lib_handle | `dlopen` handle for `libnvvm.so` |
| +648 | pointer | program_handle | NVVM program handle (from `nvvmCreateProgram`) |

## Error Handling

The libNVVM integration functions use a combination of error reporting strategies:

1. **Return codes**: All functions return unsigned integers. Zero means success. Non-zero values are specific error codes (10 = symbol resolution failure, 11 = add-module failure, etc.).

2. **setjmp/longjmp**: `sub_4BC4A0` and `sub_4BC290` both install `setjmp` frames before calling into libNVVM. If the library triggers a longjmp (e.g., due to a fatal internal error), the calling function catches it and returns an error code rather than crashing the linker process.

3. **Error handler state save/restore**: Before each libNVVM call, the current error handler context is saved via `sub_44F410` and restored afterward. This prevents libNVVM's error handling from interfering with nvlink's own error state.

4. **nvvmGetErrorString**: When `nvvmCompileProgram` returns a non-zero, non-100 code, the error is translated to a human-readable string via `nvvmGetErrorString` and returned through the `error_msg` output parameter.

## PTX Emission Phase (Whole vs Partial vs Split)

The bytes returned by `nvvmGetCompiledResult` are PTX assembly text, not cubin. After `sub_4BC6F0` returns, `main()` feeds that PTX into the embedded ptxas driver to produce one or more cubins. The dispatch is a three-way branch based on `byte_2A5F286` (`--device-c`), `byte_2A5F285` (`--force-device-c`), and `dword_2A5B514` (split-compile thread count).

### Whole-Program LTO (`main_0x409800.c` ~1155-1178)

When `byte_2A5F286 == 0` (no `--device-c`), the entire LTO program compiles to a single cubin:

```c
if (!byte_2A5F286) {
    if (dword_2A5F308 & 1)
        fprintf(stderr, "whole program compile\n");

    int arch_flag = byte_2A5F225 ? 6 : 0;
    v241 = sub_429BA0();                     // start ptxas timing
    v341 = sub_4BD4E0(
             &compiled_cubin,
             lto_ptx_bytes,
             dword_2A5F314,                  // compute_<N>
             byte_2A5F2C0,                   // -G / debug info
             dword_2A5F30C == 64,            // -m64
             byte_2A5F310,                   // -g
             ptxas_timing_ctx,
             arch_flag);
}
```

`sub_4BD4E0` creates a 168-byte ptxas context (`sub_4CDD60`), sets arch options via `sub_4CE3B0`/`sub_4CE2F0`/`sub_4CE380`/`sub_4CE640`, feeds the PTX buffer via `sub_4CE070`, runs the embedded ptxas pipeline via `sub_4CE8C0`, and retrieves the cubin through `sub_4CE670` -> `sub_4BE350`. The resulting context is destroyed via `sub_4BE400` and the cubin is copied into the caller's arena. See [PTX Input](ptx-input.md) for the shared ptxas context layout.

### Relocatable LTO (`main_0x409800.c` ~1186-1202)

When `byte_2A5F286` is set and `dword_2A5B514 == 1` (no split-compile), a single relocatable cubin is produced:

```c
if (dword_2A5F308 & 1)
    fprintf(stderr, "relocatable compile\n");

v305 = sub_429BA0();
v341 = sub_4BD760(&reloc_cubin, lto_ptx_bytes,
                  dword_2A5F314, byte_2A5F2C0, dword_2A5F30C == 64,
                  byte_2A5F310, ptxas_timing_ctx, arch_flag);
```

The difference between `sub_4BD4E0` and `sub_4BD760` is that `sub_4BD760` appends two extra options to the ptxas context:

| Option (decimal) | Meaning | Function |
|---|---|---|
| `30614221` | `--device-c` (relocatable) | `sub_4CE3E0(ctx, 30614221)` |
| `30616008` | Conditional extended split-compile helper option | `sub_4CE3E0(ctx, 30616008)` (added when `a6` is set) |

Both decimal numbers are arena-resident string pointers that decode to short CLI-style flag strings within the ptxas flag arena. The `--device-c` flag forces ptxas to emit a relocatable ELF cubin with `ET_REL`-equivalent semantics and unresolved symbols for cross-module references.

### Extended Split-Compile (`main_0x409800.c` ~1203-1270)

When `byte_2A5F286` is set AND `dword_2A5B514 != 1`, split-compile kicks in. libNVVM has produced N independent PTX slices (one per thread configured via `-split-compile-extended=<N>`), and nvlink runs them through ptxas in parallel using a thread pool. The work item layout is a 40-byte struct:

| Offset | Size | Field | Source |
|---|---|---|---|
| +0 | 8 | Output cubin pointer slot (`char **result`) | `v343 + 8*i` slot |
| +8 | 8 | PTX slice buffer pointer | `v119[i]` (slice buffer list) |
| +16 | 4 | SM compute version | `dword_2A5F314` |
| +20 | 1 | `byte_2A5F2C0 != 0` flag (debug info) | `byte_2A5F2C0` |
| +21 | 1 | `dword_2A5F30C == 64` (`-m64`) | |
| +22 | 1 | `byte_2A5F310 != 0` (`-g`) | |
| +24 | 8 | ptxas timing context | `sub_429BA0()` returned value |
| +32 | 4 | arch flag (0 or 6) | `dword_2A5B528` |
| +36 | 4 | Return code slot (filled by worker) | initially 0 |

The construction loop creates `v351` work items (where `v351` is the number of PTX slices returned from libNVVM through the module-list callback `__nvvmHandle(45242)`):

```c
v251 = 40LL * v351;
v256 = sub_426AA0(v251);                    // allocate work item array
v257 = sub_429BA0();                        // shared timing context
if (!dword_2A5B514)
    dword_2A5B514 = sub_43FD90();           // resolve thread count
filenamea = sub_43FDB0(dword_2A5B514);      // create thread pool
if (!filenamea)
    fatal_error("Unable to create thread pool");

v343 = sub_426AA0(8 * v351);                // array of output cubin slots
for (int i = 0; i < v351; ++i) {
    work[i].result_slot   = &v343[i];
    work[i].ptx_slice     = v119[i];
    work[i].compute_ver   = dword_2A5F314;
    work[i].debug_info    = byte_2A5F2C0 != 0;
    work[i].m64           = (dword_2A5F30C == 64);
    work[i].g_flag        = byte_2A5F310 != 0;
    work[i].timing_ctx    = v257;
    work[i].arch_flag     = dword_2A5B528;

    if (!sub_43FF50(filenamea, sub_4264B0, &work[i]))
        fatal_error("Call to ptxjit failed in extended split compile mode");
}

sub_43FFE0(filenamea);                      // wait for all workers
sub_43FE70(filenamea);                      // destroy thread pool

for (int i = 0; i < v351; ++i) {
    sub_4297B0(work[i].return_code, "<lto ptx>");   // check worker rc
    char *cubin_bytes = v343[i];
    sub_426570(lto_ctx, cubin_bytes, "lto.cubin", &v350);
    if (arch_is_blackwell && needs_uplift && !v350)
        sub_4275C0(&cubin_bytes, "lto.cubin", dword_2A5F314, &v367, 0);
}
```

Each worker thread dequeues a 40-byte work item and invokes `sub_4264B0`, which is a three-line trampoline that unpacks the struct and calls `sub_4BD760` with matching arguments:

```c
// sub_4264B0 at 0x4264B0
__int64 sub_4264B0(char *work_item) {
    *(uint32_t *)(work_item + 36) = sub_4BD760(
        *(void **)(work_item + 0),     // result slot
        *(void **)(work_item + 8),     // PTX slice
        *(uint32_t *)(work_item + 16), // compute_<N>
        *(uint8_t *)(work_item + 20),  // debug_info
        *(uint8_t *)(work_item + 21),  // m64
        *(uint8_t *)(work_item + 22),  // g_flag
        *(void **)(work_item + 24),    // timing_ctx
        *(uint32_t *)(work_item + 32)  // arch_flag
    );
    return 0;
}
```

This is the only place in nvlink where `sub_4BD760` (relocatable-ptxas driver) is called from a worker thread. The thread pool is constructed via `sub_43FDB0` which is the standard nvlink thread pool factory (see [Split Compilation](../lto/split-compilation.md) for the pool implementation).

## Complete Pipeline Flow

The NVVM IR path through the linker, from input to compiled output:

```
1. INIT
   main() creates LTO context (lto_ctx, 656+ bytes)
   sub_4BC290 creates NVVM program via dlsym("nvvmCreateProgram")
     Library handle stored at lto_ctx + 640
     Program handle stored at lto_ctx + 648

2. INPUT COLLECTION (main loop, lines ~600-900)
   For each .nvvm / .ltoir file:
     sub_427A10 called (register IR module)
       Validates -lto flag (byte_2A5F288) or fatal error
       sub_4BC4A0 calls __nvvmHandle(8320) -> add-module callback
       Callback registers IR bytes with the program handle
       libdevice modules excluded from dword_2A5F280 counter
   For each fatbin:
     sub_42AF40 per-member dispatch
       Type 8 (NVVM IR) -> sub_4BC4A0 directly
       Updates ftz/prec-div/prec-sqrt/fmad/maxreg/split-compile consensus
       cudadevrt modules stashed for deferred injection

3. DEFERRED CUDADEVRT (main, ~line 922-938)
   If any module referenced cudadevrt and libcudadevrt was found:
     sub_427A10(lto_ctx, libdevrt_buf, len, "libcudadevrt")
   Append sentinel "libcudadevrt" string to v353 module-name list

4. PRE-COMPILE CHECK (main, ~line 945-982)
   Error on consensus conflicts (state 4):
     -lineinfo, -maxrregcount, -ftz, -prec-div, -prec-sqrt, -fmad, -split-compile
   If dword_2A5F280 == 0 (no non-libdevice IR): skip LTO, clear byte_2A5F288

5. OPTION ASSEMBLY (main, ~line 1010)
   sub_426CD0 builds the options array:
     Fixed: -arch=compute_<N>, -link-lto
     Conditional: -split-compile-extended=<N>, -split-compile=<N>,
                  -Ofast-compile={min|mid|max}, -maxreg, -generate-line-info,
                  -inline-info, --device-c, --force-device-c, -g,
                  -has-global-host-info
     Consensus: -ftz=<N>, -prec-div=<N>, -prec-sqrt=<N>, -fma=<N>
     User: -Xnvvm options (deduplicated against fixed set)
     Returns char** array + count in (*option_count)

6. COMPILATION (main, ~line 1014)
   sub_4BC6F0 invoked with LTO context, option array, result slots
     Resolves 9 API functions via dlsym
     Appends host-ref options if host-info mode active (lto_ctx+97)
     Appends "-variables" if variables tracking active (lto_ctx+98)
     Calls nvvmCompileProgram(program_handle, count, options)
     Retrieves log via nvvmGetProgramLogSize + nvvmGetProgramLog
     Retrieves result: nvvmGetCompiledResultSize + nvvmGetCompiledResult
     Queries module slice list via __nvvmHandle(45242) (for split-compile)
     Destroys program via nvvmDestroyProgram
     Returns: result_buf (PTX bytes), result_size, slice_list, slice_count

7. PTX EMISSION (main, ~line 1155-1270)
   Three-way branch on byte_2A5F286 and dword_2A5B514:
     Case A (whole-program): sub_4BD4E0 -> single cubin
     Case B (relocatable, 1 thread): sub_4BD760 -> single reloc cubin
     Case C (split-compile, N threads): thread pool + sub_4264B0 + sub_4BD760
                                        produces N cubins in parallel
     For compute >= 0x5A (sm_90+), optionally uplift via sub_4275C0

8. MERGE (main, later phase)
   Each cubin is classified via sub_426570 and handed to sub_42A680 for
   registration in the object list, then the standard ELF merge phase
   (sub_45E7D0) runs as for any other input object.
```

## Function Map

| Address | Size | Identity | Role |
|---|---|---|---|
| `0x427A10` | ~208 B | `register_ir_module` | Validates `-lto`, calls `nvvm_add_module`, counts non-libdevice modules |
| `0x4BC290` | 2,475 B | `nvvm_create_program` | Creates NVVM program handle via `dlsym("nvvmCreateProgram")` |
| `0x4BC4A0` | 2,548 B | `nvvm_add_module` | Adds IR to program via `__nvvmHandle(8320)` dispatch |
| `0x4BC6F0` | 13,602 B | `nvvm_compile_and_extract` | Full compile + result extraction + log retrieval + cleanup |
| `0x426CD0` | 7,040 B | `collect_lto_options` | Builds `-arch`, `-link-lto`, consensus, and `-Xnvvm` option array |
| `0x426AE0` | 2,178 B | `lto_mark_used_symbols` | Marks used symbols for dead-code elimination with LTO |
| `0x4BC270` | ~128 B | `nvvm_translate_error` | Translates NVVM error code to string |
| `0x4297B0` | ~500 B | `report_lto_error` | Reports LTO-related errors with context (error code 4 = inline code message) |
| `0x4BD4E0` | ~700 B | `ptxas_compile_whole` | Embedded ptxas driver for whole-program LTO (no `--device-c`) |
| `0x4BD760` | ~900 B | `ptxas_compile_reloc` | Embedded ptxas driver for relocatable / per-slice split-compile LTO |
| `0x4264B0` | ~40 B | `split_compile_worker` | Thread-pool callback: unpacks 40-byte work item and invokes `sub_4BD760` |
| `0x476BF0` | ~200 B | `load_file_to_arena` | Loads file into arena buffer (second arg = text mode 0 or 1) |
| `0x462620` | ~150 B | `path_split` | Extracts base name and extension from a file path |
| `0x487A90` | ~80 B | `is_archive` | Checks `!<arch>\n` / `!<thin>\n` prefix |
| `0x4CDD60` | ~800 B | `ptxas_ctx_create` | Creates 168-byte embedded-ptxas context with magic `0x1464243BC` |
| `0x4CE070` | ~1 KB | `ptxas_ctx_set_input` | Classifies input buffer (fatbin/cubin/NVVM/PTX) and stores into context |
| `0x4CE3E0` | ~100 B | `ptxas_ctx_add_option` | Appends an option string to the ptxas context option list |
| `0x4CE670` | ~300 B | `ptxas_ctx_get_output` | Retrieves compiled cubin pointer/size from context |
| `0x4CE8C0` | ~6 KB | `ptxas_ctx_compile` | Runs the embedded ptxas pipeline (parse, isel, alloc, emit) |
| `0x4BE350` | ~200 B | `ptxas_ctx_finalize` | Finalizes compilation and extracts the cubin output buffer |
| `0x4BE400` | ~600 B | `ptxas_ctx_destroy` | Destroys the ptxas context and frees its arena |

## Confidence Assessment

High confidence (read from decompiled source):

- Magic number `0x1EE55A01` (decimal `518347265`) is verified in `sub_4CE070` lines 71-75. The padded variant is confirmed by the `(lo == 0 && hi == 0x1EE55A01)` branch.
- The `-lto` flag requirement is verified in `sub_427A10` line 15 (`if (!byte_2A5F288)`) and again in `sub_42AF40` line 268.
- The `__nvvmHandle(8320)` dispatch opcode for add-module is verified in `sub_4BC4A0` line 70 (`v13 = ... v12(8320)`).
- The nine libNVVM API function names resolved by `sub_4BC6F0` are verified by direct string-table inspection of lines 164-196.
- The whole-program vs relocatable vs split-compile three-way dispatch is verified in `main()` lines 1155-1270.
- The 40-byte split-compile work item layout is verified by field-by-field inspection of the build loop (`main()` lines 1224-1249) cross-referenced with the `sub_4264B0` unpack.
- The libcudadevrt deferred-injection mechanism is verified from `sub_42AF40` lines 246-266 and `main()` lines 922-938.
- `sub_4BC270` error table is capped at 14 entries (`off_1D489E0` array) per direct code inspection.

Medium confidence (inferred from structural patterns):

- The `sub_4CE070` type-tag values (1=NVVM, 2=fatbin, 3=cubin, 4=PTX) reflect the internal ptxas-context type space and differ from the fatbin member type (8=NVVM) returned by `sub_4CE670` at the member layer. The tag namespaces do not collide only because each value is stored at a different context offset (+80 vs +96).
- `sub_4BD1F0` parses the per-IR-module option string by calling `sub_4CE880` (set input) and `sub_4CE840` (extract option header). The field layout of the option header has been reconstructed from the strstr probes in `sub_42AF40` lines 283-468 but not verified by touching the raw header bytes.

Low confidence / speculation (flagged accordingly):

- The decimal constants `30614221` and `30616008` passed to `sub_4CE3E0` in `sub_4BD760` are treated as arena pointers into a flag-string pool but the exact string content has not been extracted. `--device-c` is inferred from the relocatable vs whole branch context.
- The "variables" option at offset `lto_ctx + 98` is documented as "variables-used tracking" based on the conditional append at `sub_4BC6F0` lines 383-390 but no source of the flag has been verified from command-line parsing.

## Diagnostic Strings

| String | Context |
|---|---|
| `"should only see nvvm files when -lto"` | `sub_427A10`: IR input without `-lto` flag |
| `"should never see bc files"` | `main()`: `.bc` extension on command line |
| `"nvlink -lto-add-module %s.nvvm"` | `sub_427A10`: verbose-keep trace for each registered module |
| `"could not find __nvvmHandle"` | `main()`: `dlsym` for `__nvvmHandle` returned NULL |
| `"error in LTO callback"` | `main()`: `nvvm_compile_and_extract` returned non-zero |
| `"compile linked lto ir:"` | `main()`: diagnostic prefix before LTO compilation |
| `"whole program compile"` | `main()`: no `--device-c`, compiling entire program |
| `"relocatable compile"` | `main()`: `--device-c` mode, producing relocatable output |

## Cross-References

- [File Type Detection](file-type-detection.md) -- Magic `0x1EE55A01` detection and extension dispatch
- [Fatbin Extraction](fatbin-extraction.md) -- Type-8 member extraction and per-module option consensus state machine
- [PTX Input](ptx-input.md) -- The embedded ptxas driver (`sub_4BD760`, `sub_4CE070`, context layout) shared with LTO PTX emission
- [LTO Overview](../lto/overview.md) -- The full LTO pipeline including whole/partial/split dispatch and the thread pool
- [libNVVM Integration](../lto/libnvvm-integration.md) -- `dlopen`/`dlsym` resolution, `__nvvmHandle` opcode dispatch, error propagation
- [Split Compilation](../lto/split-compilation.md) -- Thread pool lifecycle and work item queueing for `-split-compile-extended`
- [Whole vs Partial LTO](../lto/whole-vs-partial.md) -- Decision logic for `--device-c` / `--force-device-c` and consensus fallback
- [CLI Options](../pipeline/cli-options.md) -- `-lto`, `-Xnvvm`, `-nvvmpath`, `--force-partial-lto`, `--force-whole-lto`
- [Entry Point & Main](../pipeline/entry.md) -- The `main()` function that orchestrates the LTO phases
