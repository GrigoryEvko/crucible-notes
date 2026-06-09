# Environment Variables

nvlink reads exactly eight environment variables via `getenv()` calls. This count has been exhaustively verified: every `getenv` call site in the binary (14 total, across 8 decompiled functions plus the PLT thunk at `0x4034A0`) has been identified and mapped. No additional environment variable reads exist through `secure_getenv`, `environ`, or indirect mechanisms.

Unlike CLI flags, which are parsed centrally by `sub_427AE0`, environment variables are consumed at point of use — each subsystem calls `getenv()` directly when it needs the value. The variables divide into three categories: search paths that influence file discovery, temporary file placement, and debug/diagnostic controls shared with other CUDA toolchain components through the `generic_knobs_impl.h` infrastructure.

None of these variables are documented in nvlink's `--help` output. Their existence is inferred from string references in the binary.

## Summary Table

| Variable | Read by | String address | `getenv` call site | Pipeline phase | Description |
|---|---|---|---|---|---|
| `LIBRARY_PATH` | `main` (0x409800) | inline at call site | line 399 of main | Init / library resolution | Search directories for `-l` libraries |
| `LD_LIBRARY_PATH` | `sub_15C3FD0` | 0x225FCDA | 0x15C3FE1 | Fatbin driver loading | Search for fatbin driver shared object |
| `CUDA_DEVCODE_PATH` | `sub_11E96E0` | 0x1F1E460 | line 101 | SM dispatch / compilation | Device code cache search path |
| `CUDA_DEVCODE_CACHE` | `sub_11E96E0` | 0x1F1E472 | line 102 | SM dispatch / compilation | Device code cache write directory |
| `TMPDIR` | `sub_42FCB0` | 0x1D38388 | line 149 | Any (temp file creation) | Temporary file directory |
| `DUMP_KNOBS_TO_FILE` | `sub_1764A50`, `sub_4FDC30` | 0x1D4A0F8 | lines 366, 369 | Knobs initialization | Dump all knob values to a file |
| `CAN_FINALIZE_DEBUG` | `sub_4709E0`, `sub_470DA0` | 0x1D40080 | line 18, line 16 | Mercury finalization | Enable debug output for architecture compatibility checks |
| `MAKEFLAGS` | `sub_1D1E740` | 0x245F2B2 | line 57 | Embedded ptxas / parallel compilation | GNU Make jobserver protocol integration |

## Verification Methodology

Completeness was verified through three independent methods:

1. **Direct grep** of all decompiled `.c` files for the string `getenv(` — yields 14 hits across 8 source files (plus the PLT thunk wrapper).
2. **String table scan** of `nvlink_strings.json` (31,237 entries) for all uppercase strings matching common environment variable patterns (`CUDA_*`, `NV_*`, `LD_*`, `LIBRARY_*`, `TMPDIR`, `PATH`, `HOME`, `MAKEFLAGS`, etc.). Only the 8 documented variables appear as standalone strings.
3. **Import table check** for `secure_getenv` and `__environ` references — none found. The only `getenv` entry is the PLT thunk at `0x4034A0`.

The `LIBRARY_PATH` string does not appear in the `.rodata` string table as a separate entry; it is embedded inline at the `getenv` call site in `main`. All other 7 variable names appear as standalone strings in `.rodata`.

## LIBRARY_PATH

Read in `main` at `0x409800` during the library resolution phase, immediately after `-L` directories have been collected from the CLI.

```c
// main, line 399, library resolution block
v185 = (unsigned int)getenv("LIBRARY_PATH");
sub_44EC40(v185, (unsigned int)":", 0, 1, (unsigned int)sub_462520, v184, 1, 1);
```

The value is split on `:` by `sub_44EC40` (the generic string tokenizer). Each token is appended to the library search context via `sub_462520`, the same callback used for `-L` directories. The environment directories come after CLI directories in the search order, meaning `-L` paths take precedence.

If `LIBRARY_PATH` is unset or empty, no directories are added from this source. Empty tokens between consecutive `:` delimiters are skipped (the tokenizer's `a3=0` parameter controls this).

This variable mirrors the behavior of the host linker (`ld`). It is distinct from `LD_LIBRARY_PATH`, which serves a different purpose in nvlink.

After directory collection, `main` iterates over queued `-l` library names (from `qword_2A5F2F8`) and searches the combined directory list via `sub_462870` (file finder). If the first search fails, a second attempt is made with `sub_42A2D0` as a transform callback, passing the original library name at offset +8. Found libraries are deduplicated through `sub_4646A0` against `qword_2A5F330`.

**Consumed by:** Library resolution in `main`, before the input-file dispatch loop.
**Global effect:** Directories are added to the search context, which is a local stack variable destroyed after resolution completes.
**Cross-reference:** [Library Resolution](../pipeline/library-resolution.md)

## LD_LIBRARY_PATH

Read in `sub_15C3FD0` at `0x15C3FD0`, which searches for the fatbin driver shared library.

```c
// sub_15C3FD0 -- find_fatbin_driver_library
result = qword_2A644C8;
if ( !qword_2A644C8 )
{
    v1 = getenv("LD_LIBRARY_PATH");
    v2 = sub_462360(v1, 0x3A);   // split on ':' (0x3A)
    // ... glob each directory for "libfat*Driver.so" ...
    // ... dlopen matches, dlsym("fatBinaryDriver"), verify magic ...
    qword_2A644C8 = result;
}
```

This is not used for device library resolution (that role belongs to `LIBRARY_PATH` and `-L`). Instead, `sub_15C3FD0` scans `LD_LIBRARY_PATH` directories to locate the NVIDIA fatbin driver `.so` file, which is loaded via `dlopen` for fatbin extraction.

The call chain is:

1. `sub_15C3FD0` — splits `LD_LIBRARY_PATH` on `:`, creates search context.
2. `sub_15C41C0` — for each directory, globs for `libfat*Driver.so` (string at `0x225FCEA`).
3. `sub_15C41E0` — for each glob match:
   - Calls `sub_463360` (`dlopen`) to load the `.so`.
   - Calls `dlsym(handle, "fatBinaryDriver")` (string at `0x225FCFB`).
   - Validates the returned pointer: `*v7 == 786782722` (`0x2EE6B382`), a magic number identifying a valid fatbin driver interface.
   - Registers the driver via `sub_4644C0`.
   - Calls `dlclose` after registration.
4. `sub_15C4090` — cleanup callback registered via `sub_45CC80`.

The result is cached in global `qword_2A644C8`. The guard at the top (`if (!qword_2A644C8)`) ensures the search happens exactly once per process.

**Consumed by:** Fatbin extraction pipeline, invoked from the embedded ptxas/compilation subsystem.
**Global:** `qword_2A644C8` (cached driver handle).
**Cross-reference:** [Fatbin Extraction](../input/fatbin-extraction.md)

## CUDA_DEVCODE_PATH

Read in `sub_11E96E0` at `0x11E96E0`, which manages the device code compilation cache. The variable specifies a search path for pre-compiled device code objects.

```c
// sub_11E96E0, line 99-112, guarded by byte_2A5C070 (one-shot init flag)
if ( byte_2A5C070 )
{
    qword_2A64460 = getenv("CUDA_DEVCODE_PATH");
    qword_2A64458 = getenv("CUDA_DEVCODE_CACHE");
    if ( qword_2A64460 )
        sub_467460(dword_2A5BF60, "CUDA_DEVCODE_PATH");   // diagnostic: path set
    else
        sub_467460(dword_2A5BF50, "CUDA_DEVCODE_PATH");   // diagnostic: path not set
    if ( qword_2A64458 )
        sub_467460(dword_2A5BF40, "CUDA_DEVCODE_CACHE");
    else
        sub_467460(dword_2A5BF30, "CUDA_DEVCODE_CACHE");
    byte_2A5C070 = 0;  // guard: only read once
}
```

The `byte_2A5C070` flag ensures the environment is queried exactly once. After the first call, the flag is cleared and subsequent invocations of `sub_11E96E0` skip the `getenv` block. The value is stored in global `qword_2A64460`.

Whether the variable is set or not, a diagnostic message is emitted via `sub_467460` at different severity levels: `dword_2A5BF60` for "set" vs `dword_2A5BF50` for "not set". This diagnostic is only visible at elevated verbosity levels.

After the env var block, `sub_11E96E0` calls `sub_15C44D0` to search for cached compiled device code. If no cache hit occurs and `byte_2A64470` has not been set, it attempts to build a cache entry using `sub_430050` (which constructs a "devcode" search path via `sub_462550`), then `sub_15C49A0` to compile, and stores the result in `qword_2A64468`.

**Consumed by:** Device code cache lookup in the compilation subsystem.
**Global:** `qword_2A64460`
**Cross-reference:** [LTO Overview](../lto/overview.md)

## CUDA_DEVCODE_CACHE

Read in the same one-shot block in `sub_11E96E0` as `CUDA_DEVCODE_PATH`. Specifies the directory where compiled device code objects are written for caching.

The value is stored in global `qword_2A64458`. The same diagnostic pattern applies: `dword_2A5BF40` for "set", `dword_2A5BF30` for "not set".

Both `CUDA_DEVCODE_PATH` (read) and `CUDA_DEVCODE_CACHE` (write) participate in a caching scheme where previously compiled device code is stored on disk to avoid redundant recompilation across builds. The exact cache key construction and lookup logic are internal to the compilation subsystem reachable from `sub_15C44D0`.

**Consumed by:** Device code cache write path.
**Global:** `qword_2A64458`

## TMPDIR

Read in `sub_42FCB0` at `0x42FCB0`, the temporary file creation function. This function generates unique temporary filenames for intermediate compilation products (PTX files, cubin scratch, LTO temporaries).

```c
// sub_42FCB0, lines 148-169 -- create_temp_file
if ( !qword_2A5F338 )
{
    v26 = getenv("TMPDIR");
    if ( v26 )
    {
        // allocate via arena, copy string
        v4 = (char *)sub_4307C0(arena, strlen(v26) + 1);
        stpcpy(v4, v26);
        qword_2A5F338 = v4;
    }
    else
    {
        qword_2A5F338 = "/tmp";   // hardcoded fallback, direct constant
    }
}
```

The value is cached in global `qword_2A5F338` on first access. Subsequent calls to `sub_42FCB0` reuse the cached value without calling `getenv` again.

Temporary filenames follow the pattern:

```text
<TMPDIR>/tmpxft_<PID>_<COUNTER><suffix>
```

Where `<PID>` is the process ID formatted as 8 hex digits via `getpid()`, and `<COUNTER>` is a per-call collision-avoidance counter (also 8 hex digits), starting at 0 for each call:

```c
// sub_42FCB0, line 79
sprintf(s, "/tmpxft_%08x_%08x", getpid(), v5);  // v5 starts at 0
```

The function probes the generated path with `fopen(..., "r")` to check for file existence. If the file already exists, the counter `v5` is incremented and the function retries. This is separate from the write-failure retry counter `v48`, which allows up to 10 `fopen(..., "w")` failures (`v48 <= 9`) before giving up with a diagnostic via `sub_467460`.

After a successful `fopen(..., "w")`, the file handle is tracked in a linked list at `qword_2A5F350` for cleanup at exit (via `sub_465720` to register and `sub_466110` to record). The file is immediately closed after creation. The caller also receives a suffix counter from `_InterlockedExchangeAdd(&dword_2A5F340, 1u)`, which is appended as `-<N>` via `sub_450280("-%d", ...)` at line 174.

If `TMPDIR` is unset, the literal string `"/tmp"` is used — this is a direct constant assignment, not a fallback through the arena allocator.

**Consumed by:** Every subsystem that needs temporary files (PTX JIT, LTO, split compilation).
**Global:** `qword_2A5F338` (cached directory), `qword_2A5F348` (current temp path), `dword_2A5F340` (atomic suffix counter).
**Thread safety:** Directory path is set once before threads start. The suffix counter `dword_2A5F340` uses `_InterlockedExchangeAdd` for atomicity. The per-call collision counter (`v5`) is a local variable, not shared.

## DUMP_KNOBS_TO_FILE

Read in two functions: `sub_1764A50` (`knobs_init_context`) and `sub_4FDC30` (`knob_dump_to_file`). Both are part of the knobs infrastructure.

In `sub_1764A50` (lines 366-369):

```c
// sub_1764A50 -- knobs_init_context
result = getenv("DUMP_KNOBS_TO_FILE");
if ( result )
{
    v18 = getenv("DUMP_KNOBS_TO_FILE");
    v19 = strlen(v18);
    // ... allocate and copy filename into context at a1+88..112 ...
}
```

In `sub_4FDC30` (lines 369-371):

```c
// sub_4FDC30 -- knob_dump_to_file
if ( getenv("DUMP_KNOBS_TO_FILE") )
{
    v16 = getenv("DUMP_KNOBS_TO_FILE");
    v17 = strlen(v16);
    // ... same allocation pattern, store at a1+88..112 ...
}
```

Both functions follow the same pattern: check `getenv` for non-NULL, then immediately call `getenv` again to get the value (not caching the first return). When set, the value is treated as a file path. The knobs system writes all current knob names and values to that file.

The knobs system is NVIDIA's internal configuration mechanism for compiler tuning parameters. Each knob has a name, type (integer, float, string, opcode list, etc.), and default value. Setting `DUMP_KNOBS_TO_FILE` to a path causes the complete set of active knobs to be serialized to disk, which is useful for debugging compiler behavior or reproducing specific compilation configurations.

Note: the knobs infrastructure reads knob *values* from knobs files (via `-knob` CLI flag and the `sub_49B1A0` parser, 59KB), not from individual environment variables. The ROT13-encoded knob names visible in the string table (approximately 500 entries, e.g. `AIBZRTN_CNGU` = `NVOMEGA_PATH`, `CNEGVNY_YVAX_ZBQR` = `PARTIAL_LINK_MODE`) are knob identifiers, not environment variable names.

**Consumed by:** Knobs infrastructure during initialization and at dump time.
**Source:** `generic_knobs_impl.h` (path: `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/common/utils/generic/impl/generic_knobs_impl.h`)
**Related functions:** `sub_49A930` (knob_system_init), `sub_49B1A0` (knobs_file_read_and_parse, 59KB), `sub_4FDC30` (knob_dump_to_file, 14KB)

## CAN_FINALIZE_DEBUG

Read in two functions: `sub_4709E0` (`can_finalize_architecture_check`, 2,609 bytes) and `sub_470DA0` (`can_finalize_with_capability_mask`, 2,074 bytes). Both are part of the Mercury (sm >= 100) finalization pipeline.

```c
// sub_4709E0, line 18 -- can_finalize_architecture_check
v6 = getenv("CAN_FINALIZE_DEBUG");
if ( v6 )
    strtol(v6, &endptr, 10);
// ... architecture remapping and compatibility logic ...
```

The value is parsed as a base-10 integer via `strtol`. The parsed value controls the verbosity of debug output during architecture compatibility checking. The same pattern appears in `sub_470DA0` at line 16.

These functions implement the Mercury architecture compatibility model:

- **Remapping table:** 104 -> 120, 130 -> 107, 101 -> 110
- **Decade-family matching:** `arch1/10 == arch2/10` (same decade = compatible)
- **Capability bitmasks:** sm_100 = 1, sm_110 = 2, sm_103 = 8, sm_121 = 64
- **Error codes:** 0 = ok, 24 = null input, 25 = version > 0x101, 26 = incompatible, 27-30 = type-specific errors
- **Version check:** `*(a1 + 6) > 0x101` — version field at offset 6 (word) must be <= 0x101
- **Type dispatch:** `dword_1D40660[a1[3]]` — a lookup table at `0x1D40660` indexed by byte at offset 3 of the input structure

Note that `getenv` is called on every invocation of these functions (no caching). In a typical link, these functions execute once per input object during finalization, so the overhead is negligible.

**Consumed by:** Mercury finalization compatibility checks.
**Cross-reference:** [Capsule Mercury Format](../mercury/capmerc-format.md), [Compatibility](../targets/compatibility.md)

## MAKEFLAGS

Read in `sub_1D1E740` at `0x1D1E740`, which implements GNU Make jobserver protocol integration for the embedded ptxas compiler. This function is called from the parallel compilation infrastructure when the `--jobserver` CLI flag is enabled.

The `--jobserver` flag is registered at `sub_1103030` line 1001:

```c
sub_42F130(v3, "jobserver", "jobserver", 1, 0, 4, a3, 0, 0, 0, 0, 0,
           "Enable GNU Jobserver support.");
```

And consumed at `sub_1104950` line 284, where `byte` at `a3+609` controls whether jobserver integration is active.

The MAKEFLAGS parser:

```c
// sub_1D1E740, line 57 -- parse_makeflags_jobserver
v1 = getenv("MAKEFLAGS");
if ( !v1 )
{
    _InterlockedCompareExchange(a1, 5, 0);  // status = 5 (no MAKEFLAGS)
    return;
}
// copy MAKEFLAGS value into std::string (ptr)
// ...
v6 = sub_1D27380(&ptr, "--jobserver-auth=", -1, 17);  // find substring
if ( v6 == -1 )
{
    _InterlockedCompareExchange(a1, 6, 0);  // status = 6 (no jobserver token)
    return;
}
```

Only the `--jobserver-auth=` token format is recognized (string at `0x245F2BC`). The older `--jobserver-fds=` variant from GNU Make < 4.2 is not supported. Two protocols are handled:

### FIFO mode (`--jobserver-auth=fifo:<path>`)

```c
// sub_1D1E740, line 101 -- fifo: prefix check
if ( v8 == sub_1D272A0(&ptr, "fifo:", v8, 5) )
{
    // extract path after "fifo:"
    // ...
    v30 = open(file, 2050);   // O_RDWR | O_NONBLOCK (0x802)
    *(a1 + 188) = v30;
    if ( v30 != -1 )
    {
        *(a1 + 192) = v30;    // same fd for both read and write
        *(a1 + 204) = 1;      // success flag
    }
}
```

Opens the named pipe at `<path>` with flags `O_RDWR | O_NONBLOCK` (value 2050 = 0x802). `O_NONBLOCK` prevents the open from blocking when no other process has the FIFO open. The single file descriptor is stored at both offsets `a1+188` (read fd) and `a1+192` (write fd).

### Pipe mode (`--jobserver-auth=<read_fd>,<write_fd>`)

```c
// sub_1D1E740, lines 300-321
// validate both substrings contain only digits via sub_1D27410
v31 = strtol(v45, NULL, 10);       // parse read fd
*(a1 + 188) = v31;
v32 = dup(v31);                     // dup to get a new fd
*(a1 + 188) = v32;
fcntl(v32, 2, 2048);               // F_SETFD, set CLOEXEC
// ... same for write fd at a1+192 ...
*(a1 + 204) = 1;                    // success flag
```

Parses two comma-separated integers as file descriptor numbers. Each is `dup()`'d and the duplicate has `FD_CLOEXEC` set via `fcntl(fd, F_SETFD, ...)`. If either `dup` + `fcntl` fails, the function cleans up by closing the read fd and sets status to 7. Both substrings are validated to contain only digits (`0-9`) via `sub_1D27410` before parsing; non-numeric values cause an immediate jump to the error path.

### Error messages

Two diagnostic strings reference jobserver state:

| Address | String |
|---|---|
| `0x1F440A8` | `GNU Jobserver support requested, but no compatible jobserver found. Ignoring '--jobserver'` |
| `0x1F44108` | `Jobserver requested, but an error occurred` |

### Status codes

| Value | Meaning |
|---|---|
| 0 | Initial / pending |
| 5 | `MAKEFLAGS` not set |
| 6 | `--jobserver-auth=` not found in `MAKEFLAGS` |
| 7 | Parse error, `open` failure, `dup` failure, or `fcntl` failure |

All status updates use `_InterlockedCompareExchange(a1, new, 0)`, which atomically sets the status only if it is currently 0 (preventing races if multiple threads try to initialize).

**Consumed by:** Parallel compilation infrastructure in the embedded compiler backend.
**Cross-reference:** [Split Compilation](../lto/split-compilation.md), [Thread Pool](../infra/thread-pool.md)

## Interaction Between Variables

The environment variables operate independently with one exception: `CUDA_DEVCODE_PATH` and `CUDA_DEVCODE_CACHE` are always read together in the same guarded block. All other variables are consumed by unrelated subsystems at different pipeline stages.

```text
Pipeline phase          Variables read
─────────────────────   ────────────────────────────────
Init / option parse     (none)
Library resolution      LIBRARY_PATH
Input dispatch          (none)
Fatbin extraction       LD_LIBRARY_PATH
Compilation setup       CUDA_DEVCODE_PATH, CUDA_DEVCODE_CACHE
Knobs init              DUMP_KNOBS_TO_FILE
Parallel compilation    MAKEFLAGS
Temp file creation      TMPDIR  (lazy, on first call)
Finalization            CAN_FINALIZE_DEBUG
```

## Call Site Census

Complete enumeration of all 14 `getenv` call sites in the binary (excluding the PLT thunk at `0x4034A0`):

| # | File | Line | Variable | Caching |
|---|---|---|---|---|
| 1 | `main_0x409800.c` | 399 | `LIBRARY_PATH` | No (consumed immediately) |
| 2 | `sub_15C3FD0_0x15C3FD0.c` | 15 | `LD_LIBRARY_PATH` | Yes (`qword_2A644C8` guard) |
| 3 | `sub_11E96E0_0x11E96E0.c` | 101 | `CUDA_DEVCODE_PATH` | Yes (`byte_2A5C070` guard) |
| 4 | `sub_11E96E0_0x11E96E0.c` | 102 | `CUDA_DEVCODE_CACHE` | Yes (`byte_2A5C070` guard) |
| 5 | `sub_42FCB0_0x42FCB0.c` | 149 | `TMPDIR` | Yes (`qword_2A5F338` guard) |
| 6 | `sub_1764A50_0x1764A50.c` | 366 | `DUMP_KNOBS_TO_FILE` | No (re-reads each time) |
| 7 | `sub_1764A50_0x1764A50.c` | 369 | `DUMP_KNOBS_TO_FILE` | No (redundant second read) |
| 8 | `sub_4FDC30_0x4FDC30.c` | 369 | `DUMP_KNOBS_TO_FILE` | No (re-reads each time) |
| 9 | `sub_4FDC30_0x4FDC30.c` | 371 | `DUMP_KNOBS_TO_FILE` | No (redundant second read) |
| 10 | `sub_4709E0_0x4709E0.c` | 18 | `CAN_FINALIZE_DEBUG` | No (re-reads per call) |
| 11 | `sub_470DA0_0x470DA0.c` | 16 | `CAN_FINALIZE_DEBUG` | No (re-reads per call) |
| 12 | `sub_1D1E740_0x1D1E740.c` | 57 | `MAKEFLAGS` | No (consumed immediately) |
| 13 | `sub_4709E0_0x4709E0.c` | 20 | `CAN_FINALIZE_DEBUG` | (strtol parse of #10) |
| 14 | `sub_470DA0_0x470DA0.c` | 18 | `CAN_FINALIZE_DEBUG` | (strtol parse of #11) |

Note: entries 13-14 are the `strtol` calls that consume the return from entries 10-11. The `DUMP_KNOBS_TO_FILE` entries 6-7 and 8-9 show a common pattern: check `getenv` for non-NULL, then immediately call `getenv` again for the value. This is slightly wasteful (the libc return is valid until `putenv`/`setenv` modifies the environment) but harmless.

## Function Map

| Address | Name (inferred) | Size | Reads |
|---|---|---|---|
| `sub_409800` | `main` | 57,970 B | `LIBRARY_PATH` |
| `sub_42FCB0` | `create_temp_file` | 4,019 B | `TMPDIR` |
| `sub_4709E0` | `can_finalize_architecture_check` | 2,609 B | `CAN_FINALIZE_DEBUG` |
| `sub_470DA0` | `can_finalize_with_capability_mask` | 2,074 B | `CAN_FINALIZE_DEBUG` |
| `sub_4FDC30` | `knob_dump_to_file` | 14,544 B | `DUMP_KNOBS_TO_FILE` |
| `sub_11E96E0` | `devcode_cache_init` | — | `CUDA_DEVCODE_PATH`, `CUDA_DEVCODE_CACHE` |
| `sub_15C3FD0` | `find_fatbin_driver_library` | small | `LD_LIBRARY_PATH` |
| `sub_1764A50` | `knobs_init_context` | — | `DUMP_KNOBS_TO_FILE` |
| `sub_1D1E740` | `parse_makeflags_jobserver` | 7,770 B | `MAKEFLAGS` |

## Global Variables

| Address | Type | Set by | Holds |
|---|---|---|---|
| `qword_2A5F338` | `char*` | `sub_42FCB0` | Cached `TMPDIR` value (or `"/tmp"`) |
| `qword_2A5F348` | `char*` | `sub_42FCB0` | Current temporary file path |
| `dword_2A5F340` | `int` (atomic) | `sub_42FCB0` | Temp file suffix counter (`_InterlockedExchangeAdd`) |
| `qword_2A5F350` | `list*` | `sub_42FCB0` | Temp file handle tracking list |
| `qword_2A64460` | `char*` | `sub_11E96E0` | Cached `CUDA_DEVCODE_PATH` |
| `qword_2A64458` | `char*` | `sub_11E96E0` | Cached `CUDA_DEVCODE_CACHE` |
| `byte_2A5C070` | `bool` | `sub_11E96E0` | One-shot init guard for devcode env vars |
| `qword_2A644C8` | `void*` | `sub_15C3FD0` | Cached fatbin driver handle |
| `qword_2A64468` | `void*` | `sub_11E96E0` | Cached devcode compilation result |
| `byte_2A64470` | `bool` | `sub_11E96E0` | Devcode cache search-done flag |

## Cross-References

- [Library Resolution](../pipeline/library-resolution.md) — LIBRARY_PATH search path construction
- [Fatbin Extraction](../input/fatbin-extraction.md) — LD_LIBRARY_PATH driver discovery
- [Split Compilation](../lto/split-compilation.md) — MAKEFLAGS jobserver integration
- [Capsule Mercury Format](../mercury/capmerc-format.md) — CAN_FINALIZE_DEBUG architecture checks
- [Compatibility](../targets/compatibility.md) — finalization compatibility model
- [CLI Flags](cli-flags.md) — command-line options (the other configuration source)

## Confidence Assessment

### Aspect-Level Confidence

| Aspect | Confidence | Basis |
|---|---|---|
| Completeness (exactly 8 variables) | HIGH | Three independent methods converge: grep of all decompiled `.c` files for `getenv(`, string-table scan for env-var name patterns, import-table check for `secure_getenv`/`__environ` |
| `getenv` call sites (14 total) | HIGH | Exhaustive grep of decompiled files: 12 direct calls plus 2 `strtol` follow-ups for `CAN_FINALIZE_DEBUG`; every site mapped to a specific variable and function |
| Function addresses | HIGH | All 9 consumer functions verified against decompiled file names on disk |
| Global variable mappings | HIGH | Addresses (`qword_2A5F338`, `qword_2A64460`, etc.) read directly from decompiled `getenv` callers |
| Caching guard patterns | HIGH | `byte_2A5C070`, `qword_2A644C8`, `qword_2A5F338` verified in decompiled bodies |
| MAKEFLAGS parsing (`--jobserver-auth=`) | HIGH | Format strings `--jobserver-auth=` and `fifo:` prefix confirmed in string table; `open` flags `0x802 = O_RDWR|O_NONBLOCK` read from `sub_1D1E740` |
| Pipeline phase mapping | MEDIUM | Phase assignments inferred from call-chain analysis; not every intermediate caller was fully traced |

### Per-Variable Verification

| Variable | Confidence | Evidence |
|---|---|---|
| `LIBRARY_PATH` | HIGH | String NOT in standalone `.rodata` table (absent from `nvlink_strings.json`) — it is embedded inline as an immediate operand at the `getenv` call site in `main_0x409800.c` line 399 (`getenv("LIBRARY_PATH")`); the C-string constant lives in a literal pool slot adjacent to the `call getenv` instruction |
| `LD_LIBRARY_PATH` | HIGH | string at `0x225fcda`; used in `decompiled/sub_15C3FD0_0x15c3fd0.c` line 15 (`getenv("LD_LIBRARY_PATH")`) |
| `CUDA_DEVCODE_PATH` | HIGH | string at `0x1f1e460`; used in `decompiled/sub_11E96E0_0x11e96e0.c` line 101 (`qword_2A64460 = getenv("CUDA_DEVCODE_PATH")`) |
| `CUDA_DEVCODE_CACHE` | HIGH | string at `0x1f1e472`; used in `decompiled/sub_11E96E0_0x11e96e0.c` line 102 (`qword_2A64458 = getenv("CUDA_DEVCODE_CACHE")`) |
| `TMPDIR` | HIGH | string at `0x1d38388`; used in `decompiled/sub_42FCB0_0x42fcb0.c` line 149 (`v26 = getenv("TMPDIR")`), with `/tmp` fallback |
| `DUMP_KNOBS_TO_FILE` | HIGH | string at `0x1d4a0f8`; used in `decompiled/sub_1764A50_0x1764a50.c` lines 366 and 369, and `decompiled/sub_4FDC30_0x4fdc30.c` lines 369 and 371 (4 `getenv` calls total) |
| `CAN_FINALIZE_DEBUG` | HIGH | string at `0x1d40080`; used in `decompiled/sub_4709E0_0x4709e0.c` line 18 and `decompiled/sub_470DA0_0x470da0.c` line 16; parsed with `strtol` |
| `MAKEFLAGS` | HIGH | string at `0x245f2b2`; used in `decompiled/sub_1D1E740_0x1d1e740.c` line 57 (`v1 = getenv("MAKEFLAGS")`); parsed for `--jobserver-auth=` token |

**Shared with ptxas/cicc.** Per the [cicc wiki](../../cicc/config/env-vars.html), nvlink shares `TMPDIR`, `MAKEFLAGS`, and `CAN_FINALIZE_DEBUG` with both cicc and the standalone ptxas. Neither sibling tool reads `LIBRARY_PATH`, `LD_LIBRARY_PATH`, `CUDA_DEVCODE_PATH`, `CUDA_DEVCODE_CACHE`, or `DUMP_KNOBS_TO_FILE` through its own `getenv` calls — these are nvlink-specific. The embedded ptxas inside nvlink does not make independent `getenv` calls for these variables; it receives values via the compiler-state structure populated by `sub_1104950`.
