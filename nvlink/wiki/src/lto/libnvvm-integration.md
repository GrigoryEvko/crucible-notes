# libnvvm Integration

nvlink does not contain its own NVVM IR compiler. Instead, it loads `libnvvm.so` at runtime via `dlopen` and drives compilation through the public nvvmAPI plus a private `__nvvmHandle` dispatch table. The integration spans four functions: `sub_4BC470` constructs the library path and opens the shared object, `sub_4BC290` creates an NVVM program context, `sub_4BC4A0` adds IR modules to that context, and `sub_4BC6F0` compiles the accumulated IR and extracts the result. A fifth function, `sub_4299E0`, serves as a callback that libnvvm invokes to write intermediate LTO bitcode when verbose-keep mode is active.

| | |
|---|---|
| **Library loader** | `sub_4BC470` at `0x4BC470` (short wrapper, calls `sub_4BC290`) |
| **Program creator** | `sub_4BC290` at `0x4BC290` (2,475 bytes / 100 lines) |
| **Module adder** | `sub_4BC4A0` at `0x4BC4A0` (2,548 bytes / 112 lines) |
| **Compiler + extractor** | `sub_4BC6F0` at `0x4BC6F0` (13,602 bytes / 489 lines) |
| **Post-link callback** | `sub_4299E0` at `0x4299E0` (writes `linked_lto.bc` / `.ptx`) |
| **dlopen wrapper** | `sub_463360` at `0x463360` (7 bytes -- thin wrapper around `dlopen`) |
| **Caller** | `main()` at `0x409800`, Phase 1 (loading) and Phase 3 (compilation) |

## Linker Context Fields

The elfw (linker context) structure stores two nvvm-related pointers:

| Offset | Size | Field | Role |
|---|---|---|---|
| 640 | 8 | `nvvm_lib` | `dlopen` handle for `libnvvm.so` |
| 648 | 8 | `nvvm_prog` | opaque `nvvmProgram` created by `nvvmCreateProgram` |
| 480 | 8 | `log_context` | arena/list head used by `sub_4644C0` (list-append) for log accumulation |
| 97 | 1 | `force_device_c` | flag -- when set, appends host-reference options to the compile invocation |
| 98 | 1 | `variables_flag` | flag -- when set, appends `"-variables"` to the compile invocation |
| 520 | 8 | `host_ref_ek` | host-reference externally-visible kernel list (string pointer) |
| 528 | 8 | `host_ref_ik` | host-reference internally-visible kernel list |
| 536 | 8 | `host_ref_ec` | host-reference externally-visible constant list |
| 544 | 8 | `host_ref_ic` | host-reference internally-visible constant list |
| 552 | 8 | `host_ref_eg` | host-reference externally-visible global list |
| 560 | 8 | `host_ref_ig` | host-reference internally-visible global list |

## Phase 1: Library Loading (`sub_4BC470`)

Loading occurs during elfw initialization (line 513 of `main()`), only when `-lto` is active (`byte_2A5F288`). The library path is constructed from the `--nvvmpath` CLI option:

```
path = nvvmpath + "/lib64" + "/libnvvm.so"
```

`sub_4BC470` performs three steps:

1. Calls `sub_5F5AC0(nvvmpath, "libnvvm.so", 0)` to concatenate the path components. The zero third argument selects the `/lib64/` intermediate directory.
2. Passes the constructed path to `sub_4BC290`, which stores the `dlopen` handle and creates the NVVM program.
3. Returns 0 on success, non-zero on failure.

The actual `dlopen` call is in `sub_463360`:

```c
// sub_463360 -- dlopen wrapper
void *sub_463360(const char *path, char lazy) {
    return dlopen(path, lazy == 0 ? RTLD_NOW : RTLD_LAZY);
    //                    ^-- a2==0 means RTLD_NOW (flag value 2 on Linux)
    //                         a2!=0 means RTLD_LAZY (flag value 1)
}
```

The second argument is 0 in all observed call sites, so libnvvm is loaded with `RTLD_NOW` -- all symbols are resolved immediately at load time. This means any missing symbols in libnvvm.so cause a hard failure during loading rather than a lazy fault during compilation.

### Prerequisite Validation

Option parsing (`sub_427AE0`) validates that `--nvvmpath` is set when `-lto` is active. If the user passes `-lto` without `--nvvmpath`, nvlink emits a fatal error before reaching the loading code. In practice, `nvcc` always supplies `--nvvmpath` pointing to the CUDA toolkit's `nvvm/` directory.

## Phase 2: Program Creation (`sub_4BC290`)

`sub_4BC290` is called from `sub_4BC470` after path construction. It performs two operations on the elfw context:

1. **Store the library handle.** Writes the `dlopen` result to `elfw[640]`. If `elfw[640]` is already non-NULL, the function returns 0 immediately (library already loaded).

2. **Create the NVVM program.** Resolves `nvvmCreateProgram` via `dlsym` from the loaded library handle, then calls it with a pointer to `elfw[648]`:

```c
// sub_4BC290, simplified
int nvvm_init(elfw_t *ctx, void *unused, void *lib_handle) {
    if (ctx == NULL)        return 1;
    if (ctx->nvvm_lib)      return 0;   // already loaded
    if (lib_handle == NULL) return 10;   // no library

    ctx->nvvm_lib = lib_handle;

    // Resolve and call nvvmCreateProgram
    nvvmCreateProgram_fn = dlsym(ctx->nvvm_lib, "nvvmCreateProgram");
    if (!nvvmCreateProgram_fn)
        return 10;   // symbol not found

    nvvmResult_t rc = nvvmCreateProgram_fn(&ctx->nvvm_prog);
    if (rc != NVVM_SUCCESS)
        return 1;    // creation failed

    return 0;
}
```

The function uses `setjmp` / `longjmp` as an exception-handling mechanism: if any call into libnvvm triggers a signal or internal longjmp, control returns to the setjmp site and the function reports failure. This pattern appears in all three nvvm wrapper functions.

### Return Codes

| Code | Meaning |
|---|---|
| 0 | Success (or already loaded) |
| 1 | NULL context or nvvmCreateProgram failed |
| 10 | No library handle or dlsym failed |

## Phase 3: Module Addition (`sub_4BC4A0`)

Each NVVM IR module collected during the input loop is added to the NVVM program via `sub_4BC4A0`. This function does not use the public `nvvmAddModuleToBitcode` API. Instead, it resolves and calls through the private `__nvvmHandle` dispatch table:

```c
// sub_4BC4A0, simplified
int nvvm_add_module(elfw_t *ctx, char *name, char *ir_data, size_t ir_size) {
    // Resolve the dispatch table
    __nvvmHandle_fn = dlsym(ctx->nvvm_lib, "__nvvmHandle");
    if (!__nvvmHandle_fn)
        return 10;

    // Retrieve the "add module" function via dispatch code 8320
    add_module_fn = __nvvmHandle_fn(8320);
    if (!add_module_fn)
        return 10;

    // Call: add_module(program, name, ir_data, ir_size)
    nvvmResult_t rc = add_module_fn(ctx->nvvm_prog, name, ir_data, ir_size);
    if (rc != NVVM_SUCCESS)
        return 11;

    return 0;
}
```

### The `__nvvmHandle` Dispatch Table

`__nvvmHandle` is a private exported symbol in `libnvvm.so` that takes a numeric dispatch code and returns a function pointer. It serves as an extensibility mechanism, providing access to internal APIs that are not part of the public NVVM C API. Three dispatch codes are used:

| Code | Context | Returns | Purpose |
|---|---|---|---|
| `8320` | `sub_4BC4A0` | Module-add function | Adds NVVM IR bitcode to the program; takes `(program, name, data, size)` |
| `45242` | `sub_4BC6F0` | Multi-result getter | Retrieves compiled result as an array of module pointers; takes `(program, count, out_array)` |
| `61453` | `sub_4BC6F0` | Result-count getter | Returns the number of compiled result modules; takes `(program, out_count)` |
| `0xBEEF` | `main()` | Callback registrar | Registers a post-link callback; called with `(program, callback_fn, 0, 0xF00D)` |

The dispatch codes appear to be arbitrary magic numbers rather than a sequential enumeration. The `0xBEEF` / `0xF00D` pair used for callback registration in main are particularly notable as mnemonic hex values.

### Return Code 11

A return value of 11 from `sub_4BC4A0` indicates that the `add_module_fn` call returned a non-zero NVVM error code. This is distinct from return code 10 (symbol resolution failure) and return code 0 (success).

## Phase 4: Compilation and Result Extraction (`sub_4BC6F0`)

`sub_4BC6F0` is the largest and most complex function in the nvvm integration layer at 13,602 bytes. It orchestrates the full compile-and-extract sequence:

### Symbol Resolution

The function begins by resolving nine symbols from `libnvvm.so`. All nine must succeed or the function returns 10 immediately:

```c
// All resolved from elfw->nvvm_lib via dlsym
nvvmCompileProgram      = dlsym(lib, "nvvmCompileProgram");
nvvmGetCompiledResultSize = dlsym(lib, "nvvmGetCompiledResultSize");
nvvmGetCompiledResult   = dlsym(lib, "nvvmGetCompiledResult");
nvvmGetErrorString      = dlsym(lib, "nvvmGetErrorString");
nvvmGetProgramLogSize   = dlsym(lib, "nvvmGetProgramLogSize");
nvvmGetProgramLog       = dlsym(lib, "nvvmGetProgramLog");
nvvmDestroyProgram      = dlsym(lib, "nvvmDestroyProgram");
__nvvmHandle(45242)     = handle_fn(45242);   // multi-result getter
__nvvmHandle(61453)     = handle_fn(61453);   // result-count getter
```

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

```c
nvvmResult_t rc = nvvmCompileProgram(
    elfw->nvvm_prog,   // the NVVM program handle
    option_count,       // number of option strings
    option_array,       // char** option array
    ...
);
```

After the call, the option array is freed via `sub_431000` (arena_free).

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

On success (return code 0) or partial success (return code 100), the compiled PTX result is retrieved:

```c
// Single-result path (whole-program):
nvvmGetCompiledResultSize(program, &result_size);
result_buf = arena_alloc(result_size);
list_append(result_buf, &elfw->log_context);
nvvmGetCompiledResult(program, result_buf);
*ptx_out = result_buf;
*ptx_size = result_size;

// Multi-result path (split/partial):
__nvvmHandle_61453(program, &module_count);   // get count
if (module_count > 1) {
    module_array = arena_alloc(8 * module_count);
    __nvvmHandle_45242(program, module_count, module_array);  // get pointers
    *cubin_array_out = module_array;
}
```

The single-result path produces one PTX string that `main()` feeds to the embedded ptxas. The multi-result path produces an array of module pointers that `main()` distributes across the split-compile thread pool.

### Program Destruction

After extracting all results, the NVVM program is destroyed:

```c
nvvmResult_t rc = nvvmDestroyProgram(&elfw->nvvm_prog);
return rc != 0;   // 0=success, 1=destroy failed
```

### Return Codes

| Code | Meaning |
|---|---|
| 0 | Success, result extracted, program destroyed |
| 1 | Result extraction or program destruction failed |
| 8 | Compilation error with log message |
| 10 | Symbol resolution failed (dlsym returned NULL) |

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

```
Phase 1 (init):
  sub_4BC470(elfw, nvvmpath)
    +-- sub_5F5AC0(nvvmpath, "libnvvm.so", 0)     // path_join
    +-- sub_4BC290(elfw, 0, path)                  // nvvm_init
        +-- sub_463360(path, 0)                    // dlopen(path, RTLD_NOW)
        +-- dlsym(lib, "nvvmCreateProgram")
        +-- nvvmCreateProgram(&elfw[648])

Phase 2 (input loop, per IR module):
  sub_4BC4A0(elfw, name, ir_data, ir_size)         // nvvm_add_module
    +-- dlsym(lib, "__nvvmHandle")
    +-- __nvvmHandle(8320)                         // get add-module function
    +-- add_fn(elfw[648], name, ir_data, ir_size)

Phase 3 (compilation):
  [optional] __nvvmHandle(0xBEEF)                  // get callback registrar
             callback_registrar(prog, sub_4299E0, 0, 0xF00D)

  sub_4BC6F0(ptx_out, ptx_size, cubin_out,         // nvvm_compile
             status, partial, error_msg,
             elfw, option_count, options)
    +-- dlsym 7 public API functions
    +-- __nvvmHandle(45242)                        // multi-result getter
    +-- __nvvmHandle(61453)                        // result-count getter
    +-- nvvmCompileProgram(prog, argc, argv)
    +-- nvvmGetProgramLogSize + nvvmGetProgramLog
    +-- nvvmGetCompiledResultSize + nvvmGetCompiledResult
    +-- OR: __nvvmHandle(61453) + __nvvmHandle(45242) for split results
    +-- nvvmDestroyProgram(&prog)
```

## API Symbol Catalog

Every symbol resolved from `libnvvm.so` by nvlink:

| Symbol | Resolution site | Public API? | Purpose |
|---|---|---|---|
| `nvvmCreateProgram` | `sub_4BC290` | Yes | Create compilation program handle |
| `nvvmCompileProgram` | `sub_4BC6F0` | Yes | Compile accumulated IR modules |
| `nvvmGetCompiledResultSize` | `sub_4BC6F0` | Yes | Query compiled PTX size |
| `nvvmGetCompiledResult` | `sub_4BC6F0` | Yes | Retrieve compiled PTX string |
| `nvvmGetErrorString` | `sub_4BC6F0` | Yes | Map error code to message |
| `nvvmGetProgramLogSize` | `sub_4BC6F0` | Yes | Query compilation log size |
| `nvvmGetProgramLog` | `sub_4BC6F0` | Yes | Retrieve compilation log |
| `nvvmDestroyProgram` | `sub_4BC6F0` | Yes | Destroy program and free resources |
| `__nvvmHandle` | `sub_4BC4A0`, `sub_4BC6F0`, `main()` | **No** (private) | Dispatch table for internal APIs |

The public API symbols match the documented [NVVM IR Compiler API](https://docs.nvidia.com/cuda/libnvvm-api/) exactly. The private `__nvvmHandle` symbol provides extensions for module addition (code 8320), split-compile result extraction (codes 45242, 61453), and callback registration (code 0xBEEF).

## Error Handling

All three wrapper functions (`sub_4BC290`, `sub_4BC4A0`, `sub_4BC6F0`) use `setjmp`/`longjmp` as a signal-safe error recovery mechanism. The pattern:

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
| `"could not find __nvvmHandle"` | `main()` | `dlsym("__nvvmHandle")` returned NULL during callback setup |
| `"error in LTO callback"` | `main()` | Callback registration via `__nvvmHandle(0xBEEF)` failed |
| `"nvlink -lto-post-link -o %s"` | `sub_4299E0` | Verbose-keep callback writing intermediate file |
| `"compile linked lto ir:"` | `main()` | Before invoking `sub_4BC6F0` |
| `"whole program compile"` | `main()` | LTO produced single output, whole-program mode |
| `"relocatable compile"` | `main()` | LTO produced single relocatable output |
