# libnvvm Integration

nvlink does not contain its own NVVM IR compiler. Instead, it loads `libnvvm.so` at runtime via `dlopen` and drives compilation through the public nvvmAPI plus a private `__nvvmHandle` dispatch table. The integration spans four functions: `sub_4BC470` constructs the library path and opens the shared object, `sub_4BC290` creates an NVVM program context, `sub_4BC4A0` adds IR modules to that context, and `sub_4BC6F0` compiles the accumulated IR and extracts the result. A fifth function, `sub_4299E0`, serves as a callback that libnvvm invokes to write intermediate LTO bitcode when verbose-keep mode is active.

| | |
|---|---|
| **Library loader** | `sub_4BC470` at `0x4BC470` (short wrapper, calls `sub_4BC290`) |
| **Program creator** | `sub_4BC290` at `0x4BC290` (2,475 bytes / 100 lines) |
| **Module adder** | `sub_4BC4A0` at `0x4BC4A0` (2,548 bytes / 112 lines) |
| **Compiler + extractor** | `sub_4BC6F0` at `0x4BC6F0` (13,602 bytes / 489 lines) |
| **Post-link callback** | `sub_4299E0` at `0x4299E0` (writes `linked_lto.bc` / `.ptx`) |
| **dlopen wrapper** | `sub_463360` at `0x463360` (7 bytes — thin wrapper around `dlopen`) |
| **Caller** | `main()` at `0x409800`, Phase 1 (loading) and Phase 3 (compilation) |

## Linker Context Fields

The elfw (linker context) structure stores two nvvm-related pointers:

| Offset | Size | Field | Role |
|---|---|---|---|
| 640 | 8 | `nvvm_lib` | `dlopen` handle for `libnvvm.so` |
| 648 | 8 | `nvvm_prog` | opaque `nvvmProgram` created by `nvvmCreateProgram` |
| 480 | 8 | `log_context` | arena/list head used by `sub_4644C0` (list-append) for log accumulation |
| 97 | 1 | `force_device_c` | flag — when set, appends host-reference options to the compile invocation |
| 98 | 1 | `variables_flag` | flag — when set, appends `"-variables"` to the compile invocation |
| 520 | 8 | `host_ref_ek` | host-reference externally-visible kernel list (string pointer) |
| 528 | 8 | `host_ref_ik` | host-reference internally-visible kernel list |
| 536 | 8 | `host_ref_ec` | host-reference externally-visible constant list |
| 544 | 8 | `host_ref_ic` | host-reference internally-visible constant list |
| 552 | 8 | `host_ref_eg` | host-reference externally-visible global list |
| 560 | 8 | `host_ref_ig` | host-reference internally-visible global list |

## Phase 1: Library Loading (`sub_4BC470`)

Loading occurs during elfw initialization (line 513 of `main()`), only when `-lto` is active (`byte_2A5F288`). The library path is constructed from the `--nvvmpath` CLI option:

```text
path = nvvmpath + "/lib64" + "/libnvvm.so"
```

`sub_4BC470` performs two steps (decompiled at `0x4BC470`, 10 lines):

1. Calls `sub_5F5AC0(nvvmpath, "libnvvm.so", 0)` which both constructs the path **and** calls `dlopen`. Internally, `sub_5F5AC0` calls `sub_462550(nvvmpath, "libnvvm.so", 0)` to build the path, then passes that path to `sub_463360(path, 0)` to open the library.
2. Passes the `dlopen` handle to `sub_4BC290(elfw, 0, handle)`, which stores it and creates the NVVM program.

The actual `dlopen` call is in `sub_463360`:

```c
// sub_463360 -- dlopen wrapper
void *sub_463360(const char *path, char lazy) {
    return dlopen(path, lazy == 0 ? RTLD_NOW : RTLD_LAZY);
    //                    ^-- a2==0 means RTLD_NOW (flag value 2 on Linux)
    //                         a2!=0 means RTLD_LAZY (flag value 1)
}
```

The third argument of `sub_5F5AC0` is 0, which propagates as the `lazy` parameter to `sub_463360`, so libnvvm is loaded with `RTLD_NOW` — all symbols are resolved immediately at load time. This means any missing symbols in libnvvm.so cause a hard failure during loading rather than a lazy fault during compilation.

### Prerequisite Validation

Option parsing (`sub_427AE0`) validates that `--nvvmpath` is set when `-lto` is active. If the user passes `-lto` without `--nvvmpath`, nvlink emits a fatal error before reaching the loading code. In practice, `nvcc` always supplies `--nvvmpath` pointing to the CUDA toolkit's `nvvm/` directory.

## Phase 2: Program Creation (`sub_4BC290`)

`sub_4BC290` is called from `sub_4BC470` with the `dlopen` handle already resolved (by `sub_5F5AC0`). It performs two operations on the elfw context:

1. **Store the library handle.** Writes the `dlopen` handle (third argument) to `elfw[640]` at line 32. If `elfw[640]` is already non-NULL, the function returns 0 immediately (library already loaded, line 27-28).

2. **Create the NVVM program.** Resolves `nvvmCreateProgram` via `dlsym` from the stored handle (line 53), then calls it with a pointer to `elfw[648]` (line 57):

```c
// sub_4BC290, simplified from decompiled (100 lines)
int nvvm_init(elfw_t *ctx, void *unused, void *lib_handle) {
    if (ctx == NULL)        return 1;    // line 25
    if (ctx->nvvm_lib)      return 0;    // line 27-28, already loaded
    if (lib_handle == NULL) return 10;   // line 29-30, no library

    ctx->nvvm_lib = lib_handle;          // line 32

    // setjmp crash protection wraps everything below
    // (lines 34-41)

    nvvmCreateProgram_fn = dlsym(ctx->nvvm_lib, "nvvmCreateProgram");  // line 53
    if (!nvvmCreateProgram_fn)
        return 10;   // line 82-95, symbol not found

    nvvmResult_t rc = nvvmCreateProgram_fn(&ctx->nvvm_prog);   // line 57
    if (rc != NVVM_SUCCESS)
        return 1;    // line 71-81, creation failed

    return 0;        // line 48 via LABEL_8 with no error flag
}
```

The function uses `setjmp` / `longjmp` as an exception-handling mechanism: if any call into libnvvm triggers a signal or internal longjmp, control returns to the setjmp site and the function reports failure. This pattern appears in `sub_4BC290` and `sub_4BC4A0` but **not** in `sub_4BC6F0` (the compile function).

### Return Codes

| Code | Meaning |
|---|---|
| 0 | Success (or already loaded) |
| 1 | NULL context or nvvmCreateProgram failed |
| 10 | No library handle or dlsym failed |

## Phase 3: Module Addition (`sub_4BC4A0`)

Each NVVM IR module collected during the input loop is added to the NVVM program via `sub_4BC4A0`. This function does not use the public `nvvmAddModuleToBitcode` API. Instead, it resolves and calls through the private `__nvvmHandle` dispatch table:

```c
// sub_4BC4A0, simplified from decompiled (112 lines)
int nvvm_add_module(elfw_t *ctx, char *name, char *ir_data, size_t ir_size) {
    // setjmp crash protection wraps everything below

    // Resolve the dispatch table (line 55)
    __nvvmHandle_fn = dlsym(ctx->nvvm_lib, "__nvvmHandle");
    if (!__nvvmHandle_fn)
        return 10;          // line 68

    // Retrieve the "add module" function via dispatch code 0x2080 (line 70)
    add_module_fn = __nvvmHandle_fn(0x2080);
    if (!add_module_fn)
        return 10;          // line 83

    // Call: add_module(program, name, ir_data, ir_size) (line 86)
    nvvmResult_t rc = add_module_fn(ctx->nvvm_prog, name, ir_data, ir_size);
    if (rc != NVVM_SUCCESS)
        return 11;          // line 111

    return 0;               // line 52 via LABEL_3 with no error flag
}
```

### The `__nvvmHandle` Dispatch Table

`__nvvmHandle` is a private exported symbol in `libnvvm.so` that takes a numeric dispatch code and returns a function pointer. It serves as an extensibility mechanism, providing access to internal APIs that are not part of the public NVVM C API. Four dispatch codes are used across three call sites:

| Code (decimal) | Code (hex) | Call site | Returns | Signature of returned function |
|---|---|---|---|---|
| `8320` | `0x2080` | `sub_4BC4A0` line 70 | Module-add function | `(program, name, data, size) -> nvvmResult` |
| `45242` | `0xB0BA` | `sub_4BC6F0` line 175 | Multi-result getter | `(program, count, out_array) -> nvvmResult` |
| `61453` | `0xF00D` | `sub_4BC6F0` line 178 | Result-count getter | `(program, out_count) -> nvvmResult` |
| `48879` | `0xBEEF` | `main()` line 997 | Callback registrar | `(program, callback_fn, 0, 0xF00D) -> int` |

The dispatch codes are arbitrary magic numbers. Three of the four are mnemonic hex values: `0xB0BA` ("boba"), `0xF00D` ("food"), and `0xBEEF` ("beef"). The fourth, `0x2080` (8320 decimal), does not follow the same pattern.

The callback registrar (code `0xBEEF`) takes four arguments. The fourth argument is `61453` (`0xF00D`), which is the same numeric value as the result-count-getter dispatch code. Whether this is a coincidence or an intentional identifier (e.g., telling libnvvm "this callback relates to the F00D subsystem") is unknown; the decompiled code shows the literal value `61453` passed directly on line 1007 of `main()`.

### Return Codes

| Code | Meaning | Decompiled location |
|---|---|---|
| 0 | Success — module added | `sub_4BC4A0` line 52, via LABEL_3 with no error flag |
| 1 | Error flag set — `setjmp` triggered during libnvvm call (signal/longjmp from libnvvm internals) | `sub_4BC4A0` line 49, via LABEL_3 with error flag |
| 10 | `dlsym("__nvvmHandle")` returned NULL, or `__nvvmHandle(0x2080)` returned NULL | lines 68, 83 |
| 11 | `add_module_fn` returned non-zero NVVM error code | line 111 |

## Phase 4: Compilation and Result Extraction (`sub_4BC6F0`)

`sub_4BC6F0` is the largest and most complex function in the nvvm integration layer at 13,602 bytes. It orchestrates the full compile-and-extract sequence.

Unlike the other two wrappers, this function does **not** use `setjmp`/`longjmp` for crash recovery — if libnvvm crashes during compilation, the process terminates. The rationale is that compilation is the expensive operation and partial recovery would leave the program in an inconsistent state — the compiled result buffer, log buffer, and program handle would all be in undefined states after a crash.

### Symbol Resolution

The function begins by resolving eight symbols from `libnvvm.so` via `dlsym`, then dispatching `__nvvmHandle` twice for a total of ten function pointers. Every resolution must succeed or the function returns 10 immediately. The exact resolution order matches the decompiled code at lines 164--198:

```c
// Eight dlsym calls, in order (lines 164-196 of decompiled sub_4BC6F0)
nvvmCompileProgram        = dlsym(lib, "nvvmCompileProgram");       // line 164
nvvmGetCompiledResultSize = dlsym(lib, "nvvmGetCompiledResultSize"); // line 168
__nvvmHandle_fn           = dlsym(lib, "__nvvmHandle");              // line 171
nvvmGetCompiledResult     = dlsym(lib, "nvvmGetCompiledResult");     // line 181
nvvmGetErrorString        = dlsym(lib, "nvvmGetErrorString");        // line 184
nvvmGetProgramLogSize     = dlsym(lib, "nvvmGetProgramLogSize");     // line 187
nvvmGetProgramLog         = dlsym(lib, "nvvmGetProgramLog");         // line 192
nvvmDestroyProgram        = dlsym(lib, "nvvmDestroyProgram");        // line 196

// Two dispatch calls on the __nvvmHandle result (lines 175, 178)
multi_result_getter       = __nvvmHandle_fn(45242);   // 0xB0BA
result_count_getter       = __nvvmHandle_fn(61453);   // 0xF00D
```

The `__nvvmHandle` dispatch calls happen between the third and fourth `dlsym` calls — immediately after `__nvvmHandle` itself is resolved. Each dispatch result is null-checked independently; a NULL return from either dispatch causes the function to return 10.

### Option Array Construction

Before calling `nvvmCompileProgram`, the function builds a string option array. The base options come from the caller (the `a9` parameter, containing `a8` option strings). Additional options are conditionally appended:

**User-supplied options** (from `--Xnvvm` passthrough): Copied verbatim into the array. The function scans these for `--force-device-c` to detect relocatable compilation mode.

**Host-reference options** (when `elfw[97]` is set and `--force-device-c` is absent): Up to six options are appended, one for each host-reference list that is non-NULL:

| Option prefix | Source offset | Semantics |
|---|---|---|
| `-host-ref-ek=` | `elfw[520]` | Externally-visible kernel references |
| `-host-ref-ik=` | `elfw[528]` | Internally-visible kernel references |
| `-host-ref-ec=` | `elfw[536]` | Externally-visible constant references |
| `-host-ref-ic=` | `elfw[544]` | Internally-visible constant references |
| `-host-ref-eg=` | `elfw[552]` | Externally-visible global references |
| `-host-ref-ig=` | `elfw[560]` | Internally-visible global references |

Each option string is allocated from the arena, constructed as `"-host-ref-XX=" + value`, and placed into the option array. These options tell libnvvm which symbols the host code references, enabling dead-code elimination of unreferenced device functions during whole-program compilation.

**Variables flag** (when `elfw[98]` is set): The string `"-variables"` is appended to the option array. This instructs libnvvm to preserve all global variables regardless of whether they appear referenced.

The option array is heap-allocated with capacity for `a8 + 8` entries (8 extra slots for the host-ref options, the variables flag, and padding).

### Compilation Call

The public `nvvmCompileProgram` API takes three arguments `(program, numOptions, options)`, but the decompiled call at line 391 passes six register arguments:

```c
// line 391 of decompiled sub_4BC6F0
result = nvvmCompileProgram(
    elfw->nvvm_prog,       // RDI: the NVVM program handle
    option_count,          // RSI: number of option strings (a8, mutated by host-ref appending)
    option_array,          // RDX: char** option array (s)
    v21,                   // RCX: (artifact of decompiler register tracking)
    v30,                   // R8:  (artifact)
    v23                    // R9:  (artifact)
);
```

The three extra "arguments" (RCX, R8, R9) are leftover register values from the option-construction loop that the decompiler could not prove were dead. The actual libnvvm function only reads the first three. After the call, the option array is freed via `sub_431000` (arena_free) at line 392.

### Result Interpretation

The compilation return code determines the output path:

| `nvvmCompileProgram` result | Meaning | Action |
|---|---|---|
| `0` | Success | `*compile_status = 1`, proceed to extract result |
| `100` | Partial success (relocatable) | `*compile_status = 0`, proceed (partial LTO produced split modules) |
| Any other non-zero | Error | `*compile_status` unchanged, error string retrieved via `nvvmGetErrorString` |

Return code 100 is significant: it signals that libnvvm performed partial compilation rather than whole-program optimization. This happens when `--force-device-c` is present or when the IR cannot be fully merged (e.g., separate compilation units with external linkage). When nvlink sees code 100, it knows to expect multiple output modules rather than a single monolithic PTX.

### Log Extraction

Regardless of success or failure, the function extracts the compilation log:

```c
nvvmGetProgramLogSize(program, &log_size);
if (log_size > 1) {
    log_buf = arena_alloc(log_size);
    list_append(log_buf, &elfw->log_context);   // sub_4644C0
    nvvmGetProgramLog(program, log_buf);
}
```

The log is appended to the elfw log context at offset 480 via `sub_4644C0`. If compilation also produced an error string, the log and error are concatenated:

```c
if (had_error && log_size > 1) {
    combined = arena_alloc(strlen(log) + strlen(error_string) + 1);
    strcpy(combined, log);
    strcat(combined, error_string);
    *error_msg_out = combined;
    return 8;   // error with log
}
```

### Result Extraction

On success (return code 0) or partial success (return code 100), the compiled result is retrieved through a combined single-result + multi-result path. Both paths execute sequentially within a single code block (LABEL_56 at line 446):

```c
// Step 1: Get single-result size (always)
if (nvvmGetCompiledResultSize(program, &result_size))    // v13, line 447
    return 1;

// Step 2: Get multi-result count via __nvvmHandle(0xF00D)
if (__nvvmHandle_F00D(program, &module_count))           // v16, line 451
    return 1;

// Step 3: If multi-result (count > 1), allocate and fill array
if (module_count > 1) {
    module_array = arena_alloc(8 * module_count);        // line 457-462
    *cubin_array_out = module_array;                     // line 464
    if (__nvvmHandle_B0BA(program, module_count, module_array))  // v134, line 467
        return 1;   // falls through to single-result extraction
}

// Step 4: Extract single compiled result (always, even when multi-result)
result_buf = arena_alloc(result_size);                   // line 470-477
*ptx_out = result_buf;                                   // line 478
list_append(result_buf, &elfw->log_context);             // line 479
if (nvvmGetCompiledResult(program, result_buf))          // v135, line 480
    return 1;

// Step 5: Destroy program
return nvvmDestroyProgram(&elfw->nvvm_prog) != 0;       // v142, line 481
```

The single-result path produces one PTX string that `main()` feeds to the embedded ptxas. The multi-result path produces an array of module pointers that `main()` distributes across the split-compile thread pool. Both results are extracted — the multi-result array supplements rather than replaces the single result.

### Program Destruction

After extracting all results, the NVVM program is destroyed as the final operation within the success path. The `nvvmDestroyProgram` call at line 481 takes a pointer to the program handle (`a7 + 648`), which allows libnvvm to NULL out the handle after cleanup:

```c
return nvvmDestroyProgram(&elfw->nvvm_prog) != 0;   // 0=success, 1=destroy failed
```

### Return Codes

| Code | Meaning | Decompiled location |
|---|---|---|
| 0 | Success, result extracted, program destroyed | line 481 (`v142(...) != 0` evaluates to 0) |
| 1 | Any API call in the result extraction sequence failed, or program destruction returned non-zero | lines 413, 424, 485, 481 |
| 8 | Compilation produced an error; log+error concatenated and stored in `*a6` | lines 440, 487 |
| 10 | Symbol resolution failed (`dlsym` or `__nvvmHandle` dispatch returned NULL) | lines 165-198 |

## Post-Link Callback (`sub_4299E0`)

When verbose-keep mode (`-vkeep` / `byte_2A5F29B`) is active, `main()` registers `sub_4299E0` as a callback with libnvvm before compilation. The registration sequence in main:

```c
handle = dlsym(elfw->nvvm_lib, "__nvvmHandle");
callback_registrar = handle(0xBEEF);
callback_registrar(elfw->nvvm_prog, sub_4299E0, 0, 0xF00D);
```

When libnvvm finishes linking the IR modules (before PTX emission), it invokes the callback. `sub_4299E0` writes the linked bitcode to a file:

```c
// sub_4299E0 -- post-link LTO callback
int lto_post_link_callback(void *data, size_t size) {
    // Generate output filename from current context
    remove_existing(filename);            // sub_462C10
    char *path = get_temp_path(0);        // sub_462550

    printf("nvlink -lto-post-link -o %s\n", path);

    // Choose open mode based on file type
    FILE *f;
    if (strstr(path, ".ptx"))
        f = fopen(path, "w");    // text mode for PTX
    else
        f = fopen(path, "wb");   // binary mode for bitcode

    if (!f)
        error_emit(...);

    fwrite(data, 1, size, f);
    fclose(f);
}
```

The callback determines the file extension from the temp-file naming context. If the output path contains `.ptx`, the file is opened in text mode; otherwise it is opened in binary mode (for `.bc` / linked bitcode). The verbose output line `"nvlink -lto-post-link -o %s"` is printed to stdout, making the intermediate file visible in build logs.

This callback is the mechanism behind the `linked_lto.bc` and `linked_lto.ptx` files that appear in the build directory when `nvlink -vkeep` is used with LTO.

## Complete Call Sequence

The full libnvvm integration sequence within `main()`:

```text
Phase 1 (init):
  sub_4BC470(elfw, nvvmpath)
    +-- sub_5F5AC0(nvvmpath, "libnvvm.so", 0)     // path_join + dlopen
    |   +-- sub_462550(nvvmpath, "libnvvm.so", 0)  // construct path string
    |   +-- sub_463360(path, 0)                    // dlopen(path, RTLD_NOW)
    +-- sub_4BC290(elfw, 0, handle)                // nvvm_init [setjmp-protected]
        +-- elfw[640] = handle                     // store dlopen result
        +-- dlsym(lib, "nvvmCreateProgram")
        +-- nvvmCreateProgram(&elfw[648])

Phase 2 (input loop, per IR module):
  sub_4BC4A0(elfw, name, ir_data, ir_size)         // nvvm_add_module [setjmp-protected]
    +-- dlsym(lib, "__nvvmHandle")
    +-- __nvvmHandle(0x2080)                       // get add-module function
    +-- add_fn(elfw[648], name, ir_data, ir_size)

Phase 3 (compilation):
  [if -vkeep active]:
    dlsym(lib, "__nvvmHandle")                     // main() line 987
    +-- __nvvmHandle(0xBEEF)                       // get callback registrar
    |   (NULL -> fatal "could not find CALLBACK Handle")
    +-- callback_registrar(prog, sub_4299E0, 0, 0xF00D)
        (non-zero -> fatal "error in LTO callback")

  sub_4BC6F0(ptx_out, ptx_size, cubin_out,         // nvvm_compile [no setjmp]
             status, partial, error_msg,
             elfw, option_count, options)
    +-- dlsym: nvvmCompileProgram                  // line 164
    +-- dlsym: nvvmGetCompiledResultSize            // line 168
    +-- dlsym: __nvvmHandle                        // line 171
    |   +-- __nvvmHandle(0xB0BA)                   // multi-result getter, line 175
    |   +-- __nvvmHandle(0xF00D)                   // result-count getter, line 178
    +-- dlsym: nvvmGetCompiledResult               // line 181
    +-- dlsym: nvvmGetErrorString                  // line 184
    +-- dlsym: nvvmGetProgramLogSize               // line 187
    +-- dlsym: nvvmGetProgramLog                   // line 192
    +-- dlsym: nvvmDestroyProgram                  // line 196
    +-- nvvmCompileProgram(prog, argc, argv, ...)  // line 391
    +-- nvvmGetProgramLogSize + nvvmGetProgramLog  // log extraction
    +-- nvvmGetCompiledResultSize                  // result size
    +-- __nvvmHandle(0xF00D)(prog, &count)         // result count
    +-- if count > 1: __nvvmHandle(0xB0BA)(prog, count, array)  // split results
    +-- nvvmGetCompiledResult(prog, buf)           // single PTX result
    +-- nvvmDestroyProgram(&prog)                  // cleanup
```

## API Symbol Catalog

Every symbol resolved from `libnvvm.so` by nvlink via `dlsym`:

| # | Symbol | Resolution site | Decompiled line | Public API? | Description |
|---|---|---|---|---|---|
| 1 | `nvvmCreateProgram` | `sub_4BC290` | line 53 | Yes | Create compilation program handle |
| 2 | `nvvmCompileProgram` | `sub_4BC6F0` | line 164 | Yes | Compile accumulated IR modules |
| 3 | `nvvmGetCompiledResultSize` | `sub_4BC6F0` | line 168 | Yes | Query compiled PTX size |
| 4 | `nvvmGetCompiledResult` | `sub_4BC6F0` | line 181 | Yes | Retrieve compiled PTX string |
| 5 | `nvvmGetErrorString` | `sub_4BC6F0` | line 184 | Yes | Map error code to message |
| 6 | `nvvmGetProgramLogSize` | `sub_4BC6F0` | line 187 | Yes | Query compilation log size |
| 7 | `nvvmGetProgramLog` | `sub_4BC6F0` | line 192 | Yes | Retrieve compilation log |
| 8 | `nvvmDestroyProgram` | `sub_4BC6F0` | line 196 | Yes | Destroy program and free resources |
| 9 | `__nvvmHandle` | `sub_4BC4A0` line 55, `sub_4BC6F0` line 171, `main()` line 987 | — | **No** (private) | Dispatch table for internal APIs |

Nine distinct symbol names are resolved via eleven total `dlsym` calls across four call sites: one `nvvmCreateProgram` in the init function (`sub_4BC290`), one `__nvvmHandle` in the module adder (`sub_4BC4A0`), seven public API + one `__nvvmHandle` in the compile function (`sub_4BC6F0`), and one `__nvvmHandle` in `main()`. The `__nvvmHandle` symbol is resolved three times independently (once per call site) rather than cached, since each function operates in its own scope.

The eight public API symbols match the documented [NVVM IR Compiler API](https://docs.nvidia.com/cuda/libnvvm-api/) exactly. Notably, `nvvmAddModuleToProgram` from the public API is **never used** — nvlink adds modules exclusively through the private `__nvvmHandle(0x2080)` dispatch instead.

The private `__nvvmHandle` symbol provides four extensions: module addition (`0x2080`), split-compile result extraction (`0xB0BA`, `0xF00D`), and callback registration (`0xBEEF`).

## Error Handling

Two of the three wrapper functions (`sub_4BC290` and `sub_4BC4A0`) use `setjmp`/`longjmp` as a signal-safe error recovery mechanism. `sub_4BC6F0` does **not** — it relies on normal return-code checking from the nvvm API calls. The `setjmp` pattern in the first two functions:

```c
jmp_buf env;
// Save/restore error state from arena metadata
char *state = sub_44F410(ctx);
saved_jmpbuf = state[8..15];
state[8..15] = &env;
state[0..1] = 0;

if (setjmp(env)) {
    // Exception path: restore saved state, set error flag
    state[8..15] = saved_jmpbuf;
    state[0..1] = 0x0101;  // error flags
    goto check_and_return;
}

// Normal path: call into libnvvm
...
```

This protects nvlink from crashes inside libnvvm.so. If libnvvm triggers a signal (e.g., SIGSEGV from a corrupted IR module), the longjmp returns control to nvlink rather than terminating the process. The arena metadata byte at offset 1 (`sub_44F410(ptr)[1]`) is checked after each operation to detect whether an error occurred.

## Diagnostic Strings

| String | Location | Trigger |
|---|---|---|
| `"could not find __nvvmHandle"` | `main()` line 991 | `dlsym("__nvvmHandle")` returned NULL during callback setup |
| `"could not find CALLBACK Handle"` | `main()` line 1001 | `__nvvmHandle(0xBEEF)` returned NULL (callback registrar not available) |
| `"error in LTO callback"` | `main()` line 1008 | Callback registration call returned non-zero |
| `"nvlink -lto-post-link -o %s"` | `sub_4299E0` line 15 | Verbose-keep callback writing intermediate file |
| `"compile linked lto ir:"` | `main()` | Before invoking `sub_4BC6F0` |
| `"whole program compile"` | `main()` | LTO produced single output, whole-program mode |
| `"relocatable compile"` | `main()` | LTO produced single relocatable output |

## Worked Example: Complete libnvvm API Trace

This section traces every call into `libnvvm.so` for a concrete two-input LTO link:

```bash
nvcc -dlto -arch=sm_90 a.cu b.cu -o app
```

After host-compilation and fatbin extraction, nvcc invokes nvlink with two NVVM IR inputs (`a.lto.o` and `b.lto.o`), a `--nvvmpath=/usr/local/cuda/nvvm` option, and a `-arch=sm_90` compile option. The trace below shows the exact sequence of `dlopen`, `dlsym`, and API calls that nvlink makes into libnvvm, with the decompiled call-site addresses next to each call. Tags in the rightmost column identify the decompiled source file and line number so the reader can re-derive the trace from the binary.

### Step-by-Step Call Trace

```text
Step 1: PATH CONSTRUCTION                     main() line 516-518           (addr 0x40A4E4)
    path = concat(qword_2A5F278, "/lib64")    via sub_426AA0 + strcat
    // qword_2A5F278 holds "--nvvmpath" argument value,
    // e.g. "/usr/local/cuda/nvvm"
    // Result: "/usr/local/cuda/nvvm/lib64"

Step 2: ENTER LIBRARY LOADER                  main() line 519               (addr 0x40A500)
    handle_or_err = sub_4BC470(elfw, path)
        |
        +-- sub_4BC470 line 8               (addr 0x4BC470)
        |     lib = sub_5F5AC0(path, "libnvvm.so", 0)
        |         |
        |         +-- sub_5F5AC0 line 10    (addr 0x5F5AC0)
        |         |     full_path = sub_462550(path, "libnvvm.so", 0)
        |         |     // full_path = "/usr/local/cuda/nvvm/lib64/libnvvm.so"
        |         |
        |         +-- sub_5F5AC0 line 11    (addr 0x5F5AC0)
        |               return sub_463360(full_path, 0)
        |                   |
        |                   +-- sub_463360 line 6    (addr 0x463360)
        |                         // flag = (a2==0) + 1 = 0 + 1 + 1 = 2
        |                         // Linux RTLD_NOW == 2
        |                         return dlopen(full_path, RTLD_NOW)
        |
        +-- sub_4BC470 line 9               (addr 0x4BC470)
              return sub_4BC290(elfw, NULL, handle)

Step 3: nvvmCreateProgram                    sub_4BC290 line 53             (addr 0x4BC290)
    // elfw[640] = handle                    // sub_4BC290 line 32
    fn_create = dlsym(elfw[640], "nvvmCreateProgram")
    if (fn_create == NULL)  return 10;       // "nvvm dlsym failed"
    rc = fn_create(&elfw[648])               // sub_4BC290 line 57
    if (rc != NVVM_SUCCESS) return 1;
    // elfw[648] now holds the nvvmProgram handle

Step 4: ADD MODULE "a.lto.o"                  sub_4BC4A0 line 55             (addr 0x4BC4A0)
    // Called from sub_42AF40 line 270 (addr 0x42B02A) during the input loop
    handle_fn = dlsym(elfw[640], "__nvvmHandle")
    if (!handle_fn) return 10;
    add_module_fn = handle_fn(0x2080)        // sub_4BC4A0 line 70
    if (!add_module_fn) return 10;
    rc = add_module_fn(elfw[648],
                       "a.lto.o",            // module name (from input loop)
                       ir_bytes_a,           // bitcode pointer
                       ir_size_a)            // bitcode length
    if (rc != 0) return 11;                  // sub_4BC4A0 line 111

Step 5: ADD MODULE "b.lto.o"                  sub_4BC4A0 line 55             (addr 0x4BC4A0)
    // Second iteration of the same input loop
    handle_fn = dlsym(elfw[640], "__nvvmHandle")  // resolved again, not cached
    add_module_fn = handle_fn(0x2080)
    rc = add_module_fn(elfw[648], "b.lto.o", ir_bytes_b, ir_size_b)

Step 6: REGISTER POST-LINK CALLBACK           main() line 987-1008           (addr 0x40BFDA)
    // Conditional: only when -vkeep (byte_2A5F29B) is set
    handle_fn = dlsym(elfw[640], "__nvvmHandle")         // line 987
    if (!handle_fn) fatal("could not find __nvvmHandle") // line 991
    callback_reg = handle_fn(0xBEEF)                     // line 997
    if (!callback_reg) fatal("could not find CALLBACK Handle") // line 1001
    rc = callback_reg(elfw[648],
                      sub_4299E0,            // callback function pointer
                      NULL,                  // user data
                      0xF00D)                // subsystem tag (line 1007)
    if (rc != 0) fatal("error in LTO callback") // line 1008

Step 7: BUILD OPTION ARRAY                    sub_4BC6F0 line 202            (addr 0x4BC6F0)
    s = sub_4307C0(arena, 8 * (8 + 8))       // capacity = user_count + 8
    // User options from --Xnvvm (a9 parameter):
    s[0] = "-arch=sm_90"
    // Scan for "--force-device-c"; not found -> v30=1 (append host-refs OK)
    // elfw[97] (force_device_c flag) not set -> skip host-ref append
    // elfw[98] (variables_flag) not set -> skip "-variables" append
    option_count = 1

Step 8: nvvmCompileProgram                    sub_4BC6F0 line 391            (addr 0x4BC6F0)
    // Decompiled signature uses 6 registers; libnvvm reads only the first 3
    rc = fn_compile(elfw[648],               // RDI: program handle
                    option_count,            // RSI: 1
                    option_array,            // RDX: {"-arch=sm_90"}
                    <dead>, <dead>, <dead>)
    sub_431000(option_array, option_count)   // free the array (line 392)
    //
    // rc == 0                -> *a5 = 1 (full success),  goto LABEL_56
    // rc == 100              -> *a5 = 0 (partial/reloc), goto LABEL_56
    // rc == anything else    -> v93 = 1, v94 = fn_error(rc)
    //
    // In this example rc == 0. Continue to log + result extraction.

Step 9: nvvmGetProgramLogSize                 sub_4BC6F0 line 412            (addr 0x4BC6F0)
    if (fn_log_size(elfw[648], &log_size) != 0)  return 1;
    // log_size = size_including_NUL

Step 10: nvvmGetProgramLog (if log_size > 1)  sub_4BC6F0 line 423            (addr 0x4BC6F0)
    log_buf = arena_alloc(log_size)          // line 419
    list_append(log_buf, &elfw[480])         // sub_4644C0 line 422
    if (fn_log(elfw[648], log_buf) != 0) return 1;
    // Log is now owned by the elfw log context at offset 480

Step 11: nvvmGetCompiledResultSize            sub_4BC6F0 line 447            (addr 0x4BC6F0)
    if (fn_result_size(elfw[648], &ptx_size) != 0) return 1;
    // ptx_size = number of bytes in the PTX string (including NUL)

Step 12: __nvvmHandle(0xF00D) -> count        sub_4BC6F0 line 451            (addr 0x4BC6F0)
    if (fn_count(elfw[648], &module_count) != 0) return 1;
    // module_count == 1 for whole-program LTO (single PTX output)
    // module_count >  1 for split-compile (multiple PTX blobs)

Step 13: __nvvmHandle(0xB0BA) -> array        sub_4BC6F0 line 467            (addr 0x4BC6F0)
    // Only executed when module_count > 1
    module_array = arena_alloc(8 * module_count)  // line 458
    *cubin_array_out = module_array               // line 464
    if (fn_array(elfw[648], module_count, module_array) != 0) return 1;

Step 14: nvvmGetCompiledResult                sub_4BC6F0 line 480            (addr 0x4BC6F0)
    ptx_buf = arena_alloc(ptx_size)          // line 472
    *ptx_out = ptx_buf                       // line 478
    list_append(ptx_buf, &elfw[480])         // sub_4644C0 line 479
    if (fn_get_result(elfw[648], ptx_buf) != 0) return 1;
    // ptx_buf now holds:
    //     "//\n// Generated by NVIDIA NVVM Compiler\n// ...\n.version 8.3\n
    //      .target sm_90\n.address_size 64\n ... "

Step 15: nvvmDestroyProgram                   sub_4BC6F0 line 481            (addr 0x4BC6F0)
    return fn_destroy(&elfw[648]) != 0 ? 1 : 0;
    // Note: pointer to the handle, not the handle itself.
    // libnvvm nulls elfw[648] on success.
```

### API Call Ordering (All 10 libnvvm Entry Points)

The full ordered list of libnvvm API calls for a two-module LTO compile with `-vkeep`, in execution order:

| # | API | Input | Output | Call Site | Addr |
|---|---|---|---|---|---|
| 1 | `dlopen("libnvvm.so", RTLD_NOW)` | full path | lib handle | `sub_463360` line 6 | `0x463360` |
| 2 | `nvvmCreateProgram(&prog)` | — | `prog` | `sub_4BC290` line 57 | `0x4BC290` |
| 3 | `__nvvmHandle(0x2080)(prog, "a.lto.o", a_bytes, a_size)` | module 1 | — | `sub_4BC4A0` line 86 | `0x4BC4A0` |
| 4 | `__nvvmHandle(0x2080)(prog, "b.lto.o", b_bytes, b_size)` | module 2 | — | `sub_4BC4A0` line 86 | `0x4BC4A0` |
| 5 | `__nvvmHandle(0xBEEF)(prog, sub_4299E0, NULL, 0xF00D)` | callback | — | `main()` line 1007 | `0x409800` |
| 6 | `nvvmCompileProgram(prog, 1, {"-arch=sm_90"})` | options | rc | `sub_4BC6F0` line 391 | `0x4BC6F0` |
| 7 | `nvvmGetProgramLogSize(prog, &log_size)` | — | size | `sub_4BC6F0` line 412 | `0x4BC6F0` |
| 8 | `nvvmGetProgramLog(prog, log_buf)` | — | log text | `sub_4BC6F0` line 423 | `0x4BC6F0` |
| 9 | `nvvmGetCompiledResultSize(prog, &ptx_size)` | — | size | `sub_4BC6F0` line 447 | `0x4BC6F0` |
| 10 | `__nvvmHandle(0xF00D)(prog, &count)` | — | count | `sub_4BC6F0` line 451 | `0x4BC6F0` |
| 11 | `nvvmGetCompiledResult(prog, ptx_buf)` | — | PTX string | `sub_4BC6F0` line 480 | `0x4BC6F0` |
| 12 | `nvvmDestroyProgram(&prog)` | — | — | `sub_4BC6F0` line 481 | `0x4BC6F0` |

For `force_device_c` compilation or a compile that produces multiple output modules, one additional call — `__nvvmHandle(0xB0BA)(prog, count, array)` at `sub_4BC6F0` line 467 — executes between steps 10 and 11.

### Option Vector Contents

For the concrete example above the option vector passed to `nvvmCompileProgram` is:

```text
option_count = 1
option_array = {
    "-arch=sm_90"       // from the -arch option, forwarded via sub_426CD0
}
```

If the link had used `nvcc -dlto -dc` (relocatable mode), `sub_427AE0` would set `elfw[97] = 1` and `--Xnvvm="--force-device-c"` would be added to the user options. The option scanner in `sub_4BC6F0` lines 213--235 would match `"--force-device-c"` (17-byte strncmp on line 219, comparing to the string literal at the loop start), set `v25 = 1`, and compute `v30 = (~v25) & 1 = 0` — which suppresses host-reference option appending. The final vector would look like:

```text
option_count = 2
option_array = {
    "-arch=sm_90",
    "--force-device-c",
}
```

If instead `elfw[97] == 1` and `--force-device-c` is **not** in the user options (host-reference compile mode), up to seven additional options are appended:

```text
option_count = 1 + (up to 6 host-ref) + (0 or 1 "-variables")
option_array = {
    "-arch=sm_90",
    "-host-ref-ek=<kernel_list>",      // sub_4BC6F0 line 260, from elfw[520]
    "-host-ref-ik=<kernel_list>",      // line 283, from elfw[528]
    "-host-ref-ec=<const_list>",       // line 306, from elfw[536]
    "-host-ref-ic=<const_list>",       // line 329, from elfw[544]
    "-host-ref-eg=<global_list>",      // line 351, from elfw[552]
    "-host-ref-ig=<global_list>",      // line 375, from elfw[560]
    "-variables",                      // line 386, when elfw[98] is set
}
```

Each host-ref option is a concatenation of a 13-character prefix (`-host-ref-XX=`) and a symbol-list string that `sub_43FBC0` extracts from the corresponding elfw field. Options are only appended if `sub_43FBC0` returns non-NULL for that field.

### Error Handling Per API Call

Every libnvvm API invocation is wrapped by a return-code check. The decompiled code follows one of four error-handling patterns depending on the call site and severity:

| Pattern | Used by | What happens on failure |
|---|---|---|
| **Return 10 ("dlsym failed")** | All 8 `dlsym` calls + the 2 `__nvvmHandle` dispatches in `sub_4BC6F0` (lines 165, 169, 173, 176, 179, 182, 185, 190, 193, 197) | Returns 10 immediately from `sub_4BC6F0`. No cleanup — the program handle from `nvvmCreateProgram` leaks in this path but only until `main()` exits. |
| **Return 10 + setjmp rollback** | `sub_4BC4A0` line 68 (`dlsym("__nvvmHandle")` NULL) and line 83 (`__nvvmHandle(0x2080)` NULL) | Returns 10 after restoring the saved setjmp state. The caller (`sub_42AF40` line 271 or `sub_427A10` line 22) translates the code via `sub_4BC270` and emits an error via `sub_467460`. |
| **Return 1 ("API failure")** | `nvvmGetProgramLogSize` (line 412), `nvvmGetProgramLog` on >1 result (line 423), `nvvmGetCompiledResultSize` (line 447), `__nvvmHandle(0xF00D)` (line 451), `__nvvmHandle(0xB0BA)` (line 467), `nvvmGetCompiledResult` (line 480), `nvvmDestroyProgram` (line 481) | Returns 1 from `sub_4BC6F0`. Returning 1 is interpreted by `main()` as a generic libnvvm error; `main()` line 1085 translates it via `sub_4BC270(1) = "elfLink: bad argument"` and emits a diagnostic via `sub_467460`. |
| **Return 8 + concatenated log** | `nvvmCompileProgram` failure path (lines 440, 487) | Retrieves the error string via `nvvmGetErrorString` (line 402), optionally concatenates it with the program log (line 438), stores the combined string in `*a6` (line 439), and returns 8. `main()` treats code 8 as "compile failed, error text in `*a6`" and prints the stored error to stderr before aborting. |

The `sub_4BC290` init function has its own error codes:

| Code | Trigger | Location |
|---|---|---|
| 0 | Success or library already loaded | line 28 |
| 1 | NULL elfw context, or `nvvmCreateProgram` returned non-zero | lines 24, 81 |
| 10 | NULL library handle, or `dlsym("nvvmCreateProgram")` failed | lines 29, 95 |

And `sub_4BC4A0` (module-add wrapper):

| Code | Trigger | Location |
|---|---|---|
| 0 | Success | line 52 via LABEL_3 |
| 1 | setjmp fired during any libnvvm call | line 49 via LABEL_3 with error flag |
| 10 | `dlsym("__nvvmHandle")` or `__nvvmHandle(0x2080)` returned NULL | lines 68, 83 |
| 11 | `__nvvmHandle(0x2080)` returned a function pointer but calling it returned non-zero | line 111 |

When the caller sees a non-zero return from any of these three wrappers, it calls `sub_4BC270(rc)` to translate the integer code into a string. `sub_4BC270` is a simple dispatch over an offset table `off_1D489E0` for codes `0..13`; codes outside this range map to the literal string `"elfLink: unexpected error"`.

### The PTX Output

On success the PTX string stored in `*ptx_out` (first argument of `sub_4BC6F0`) is the whole-program compiled result. `main()` retains this pointer via `v119 = sub_426AA0(p_src)` around line 1041 and hands it to the split-compile thread pool (`sub_4BC6F0`'s caller in `main()` at line 1014) which allocates per-kernel copies at lines 1049-1068 and enqueues them for embedded ptxas. Each worker thread instantiates a `PtxToElf` session and runs the ptxas backend against the sliced PTX; see [Split Compilation](split-compilation.md) for the thread-pool lifecycle.

The PTX text consumed by the embedded ptxas has the exact format documented in the cicc wiki's [PTX Emission](../../cicc/pipeline/emission.html) section: a NUL-terminated ASCII string beginning with NVVM header comments, followed by `.version`, `.target`, and `.address_size` directives, then the global variable, function, and entry point definitions. Because `nvvmCompileProgram` was driven through the public API (not the raw PTX backend), the output already reflects all LTO-time optimizations — GlobalOpt, inliner, devirtualization, and (when applicable) ThinLTO import decisions that ran inside libnvvm during step 8 above.

### Verbose-Keep Intermediate Files

When `-vkeep` is active, step 6 of the trace registers `sub_4299E0` as a post-link callback. Inside libnvvm, after the LTO passes have linked and optimized the merged IR but before PTX emission, libnvvm calls the registered callback twice — once with the linked bitcode (`void *data, size_t size`) and once with the linked PTX (same signature, different data). `sub_4299E0` writes each invocation to disk:

```bash
nvlink -lto-post-link -o /tmp/.../linked_lto.bc
nvlink -lto-post-link -o /tmp/.../linked_lto.ptx
```

The filename suffix (`.bc` vs `.ptx`) is determined by the internal tempfile naming in `sub_462550`; the open mode is chosen at `sub_4299E0` lines 16-25 based on whether the filename contains `".ptx"`. These intermediate files are the primary debugging mechanism for LTO issues — they let the user inspect what libnvvm actually produced before the split-compile phase sliced and re-assembled the PTX.

## Cross-References

- [LTO Overview](overview.md) — high-level pipeline context for libnvvm within nvlink
- [Option Forwarding](option-forwarding.md) — how CLI flags are assembled into the option vector passed to `nvvmCompileProgram`
- [Whole vs Partial LTO](whole-vs-partial.md) — decision logic that determines whether libnvvm output is whole-program or relocatable
- [Split Compilation](split-compilation.md) — thread pool that parallelizes ptxas assembly of libnvvm's PTX output
- [LTO IR Format Versions](ir-format-versions.md) — `lto_` profile tags that identify NVVM IR modules fed to libnvvm
- [Dead Code Elimination](../linker/dead-code-elimination.md) — linker-level DCE that interacts with LTO via `byte_2A5F214`

### Sibling Wiki

- **cicc wiki**: [LTO & Module Optimization](../../cicc/lto/index.html) — the compiler-side LTO pipeline inside libnvvm. Documents the five-pass IR optimization (GlobalOpt, inliner, devirtualization, ThinLTO import) that runs when `nvvmCompileProgram` is called (step 8 in the worked example above). The LTO entry point is `sub_12F5F30` at `0x12F5F30` in the cicc binary
- **cicc wiki**: [Module Summary](../../cicc/lto/module-summary.html) — `NVModuleSummary` builder (`sub_D7D4E0` at `0xD7D4E0`) used by ThinLTO import decisions inside libnvvm. Runs between `__nvvmHandle(0x2080)` module addition (steps 3--4) and `nvvmCompileProgram` (step 6 of the worked example)
- **cicc wiki**: [Inliner Cost Model](../../cicc/lto/inliner-cost.html) — the cost analysis that determines which cross-module inlining decisions happen inside libnvvm after all modules are added
- **cicc wiki**: [PTX Emission](../../cicc/pipeline/emission.html) — the back-end that produces the PTX string returned via `nvvmGetCompiledResult` in step 11 of the worked example
