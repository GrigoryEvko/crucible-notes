# Entry Point & CLI

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas binary has a deceptively simple entry point. The exported `main` at `0x409460` is an 84-byte wrapper that sets up unbuffered I/O and immediately tail-calls `sub_446240` -- the real compilation driver. This driver is a monolithic 11 KB function that allocates a 1,352-byte master options block on the stack, establishes `setjmp`-based error recovery, parses all command-line options through a generic framework, reads PTX input, and then loops over compile units running the full `Parse -> CompileUnitSetup -> DAGgen -> OCG -> ELF -> DebugInfo` pipeline for each. The entire error-handling strategy is non-local: any of the 2,350 call sites to the central diagnostic emitter `sub_42FBA0` can trigger a `longjmp` back to the driver's recovery point on fatal errors.

The same binary doubles as an in-process library. When nvcc loads ptxas as a shared object rather than spawning a subprocess, three extra arguments to the driver carry an output buffer pointer, an extra option count, and an extra options array. Callback function pointers at fixed offsets in the options block allow the host process to receive diagnostics and progress notifications without going through stderr.

| | |
|---|---|
| **main()** | `0x409460` (84 bytes) -- `setvbuf` + tail-call to `sub_446240` |
| **Real main** | `sub_446240` (11,064 bytes, ~900 lines) |
| **Options block** | 1,352 bytes on stack |
| **Error recovery** | `setjmp` / `longjmp` (no C++ exceptions) |
| **Option registration** | `sub_432A00` (6,427 bytes, ~100 options via `sub_1C97210`) |
| **Option parser** | `sub_434320` (10,289 bytes, ~800 lines) |
| **Diagnostic emitter** | `sub_42FBA0` (2,388 bytes, 2,350 callers, 7 severity levels) |
| **TLS context** | `sub_4280C0` (597 bytes, 3,928 callers, 280-byte per-thread struct) |
| **Pipeline phases** | Parse, CompileUnitSetup, DAGgen, OCG, ELF, DebugInfo |
| **Library mode** | `sub_446240(argc, argv, output_buf, extra_opt_count, extra_opts)` |

## Architecture

```text
main (0x409460, 84B)
  │
  ├─ nullsub_1(*argv)          // store program name (no-op)
  ├─ setvbuf(stdout, _IONBF)
  ├─ setvbuf(stderr, _IONBF)
  │
  └─ sub_446240(argc, argv, 0, 0, 0)   // REAL MAIN
       │
       ├─ setjmp(jmp_buf)               // fatal error recovery point
       │
       ├─ sub_434320(opts_block, ...)    // OPTION PARSER
       │    └─ sub_432A00(...)           // register ~100 options via sub_1C97210
       │
       ├─ sub_4428E0(...)                // PTX INPUT SETUP
       │    ├─ validate .version / .target
       │    ├─ handle --input-as-string
       │    └─ generate __cuda_dummy_entry__ if --compile-only
       │
       ├─ sub_43A400(...)                // TARGET CONFIGURATION
       │    └─ set cache defaults, texmode, arch-specific flags
       │
       ├─ FOR EACH compile unit:
       │    ├─ sub_451730(...)           // parser/lexer init + special regs
       │    ├─ sub_43B660(...)           // register constraint calculator
       │    ├─ sub_43F400(...)           // function/ABI setup
       │    └─ sub_43CC70(...)           // per-entry: DAGgen → OCG → ELF → DebugInfo
       │
       ├─ timing / memory stats output (--compiler-stats)
       │
       └─ cleanup + return exit code
```

## Pre-main Static Constructors

Before `main` executes, four static constructors run as part of the ELF `.init_array`. Three of them populate ROT13-obfuscated lookup tables that are foundational to the rest of the binary. This obfuscation is deliberate -- it prevents trivial string searching for internal opcode names and tuning knob identifiers in the stripped binary.

### ctor_001 -- Thread Infrastructure (`0x4094C0`, 204 bytes)

Initializes the POSIX threading foundation used throughout ptxas:

```c
pthread_key_create(&key, destr_function);       // TLS key for sub_4280C0
pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
pthread_mutex_init(&mutex, &attr);
dword_29FE0F4 = sched_get_priority_max(SCHED_RR);
dword_29FE0F0 = sched_get_priority_min(SCHED_RR);
__cxa_atexit(cleanup_func, ...);                // registered destruction
```

The TLS key created here is the one used by `sub_4280C0` (3,928 callers), making it the single most important piece of global state in the binary.

### ctor_003 -- PTX Opcode Name Table (`0x4095D0`, 17,007 bytes)

Populates a table at `0x29FE300+` with approximately 900 ROT13-encoded PTX opcode mnemonic strings. Each entry is a `(string_ptr, length)` pair. The ROT13 encoding maps `A-Z` to `N-Z,A-M` and `a-z` to `n-z,a-m`, leaving digits and punctuation unchanged.

| Encoded | Decoded | Instruction |
|---|---|---|
| `NPDOHYX` | `ACQBULK` | Bulk acquire |
| `NPDFUZVAVG` | `ACQSHMINIT` | Shared memory acquire init |
| `OFLAP` | `BSYNC` | Barrier sync |
| `PPGY.P` | `CCTL.C` | Cache control |
| `VFRGC` | `ISETP` | Integer set predicate |
| `QFRGC` | `DSETP` | Double-precision set predicate |
| `SFRGC` | `FSETP` | Single-precision set predicate |
| `OERNX` | `BREAK` | Break out of barrier |
| `QRCONE` | `DEPBAR` | Dependent barrier |
| `YQGENZ` | `LDTRAM` | Load from TRAM |

These decoded names are the canonical PTX opcode mnemonics used during parsing and validation. The table is consumed by the PTX lexer initialization at `sub_451730` and the opcode-to-handler dispatch table at `sub_46E000` (93 KB, the largest function in the front-end range).

### ctor_005 -- Mercury Tuning Knob Registry (`0x40D860`, 80,397 bytes)

The single largest function in the front-end address range. Registers 2,000+ ROT13-encoded internal tuning knob names, each paired with a hexadecimal default value string. These are the "Mercury" (OCG) backend tuning parameters that control every aspect of code generation, scheduling, and register allocation.

| Encoded Name | Decoded Name | Default |
|---|---|---|
| `ZrephelHfrNpgvirGuernqPbyyrpgvirVafgf` | `MercuryUseActiveThreadCollectiveInsts` | `0x3e40` |
| `ZrephelGenpxZhygvErnqfJneYngrapl` | `MercuryTrackMultiReadsWarLatency` | — |
| `ZrephelCerfhzrKoybpxJnvgOrarsvpvny` | `MercuryPresumeXblockWaitBeneficial` | — |
| `ZrephelZretrCebybthrOybpxf` | `MercuryMergePrologueBlocks` | — |
| `ZrephelTraFnffHPbqr` | `MercuryGenSassUCode` | — |

The knob system is documented in detail in the [Knobs System](../config/knobs.md) page. The ROT13 encoding applies identically to all knob name strings in all four constructors.

### ctor_007 -- Scheduler Knob Registry (`0x421290`, 7,921 bytes)

A smaller companion to ctor_005 that registers 98 scheduler-specific knobs. These control the instruction scheduler (Mercury/OCG) behavior at a finer granularity than the general knobs:

Decoded examples: `XBlockWaitOut`, `XBlockWaitInOut`, `XBlockWaitInOnTarget`, `WarDeploySyncsFlush_SW4397903`, `WaitToForceCTASwitch`, `VoltageWar_SW4981360PredicateOffDummies`, `TrackMultiReadsWarLatency`, `ScavInlineExpansion`, `ScavDisableSpilling`.

Knob names containing `_SW` followed by a number (e.g., `_SW4397903`) indicate workarounds for specific hardware bugs identified by NVIDIA's internal bug tracking system.

## Real Main -- `sub_446240`

The exported `main()` tail-calls `sub_446240` with three zero arguments appended. This function is the complete compilation orchestrator: it owns the options block, the error recovery, the compilation loop, and the statistics output.

| Field | Value |
|---|---|
| Address | `0x446240` |
| Size | 11,064 bytes |
| Stack frame | 1,352+ bytes (master options block + locals) |
| Callers | 1 (main) |
| Error recovery | `setjmp` at function entry |

### Signature and Library Mode

```c
int sub_446240(int argc, char **argv,
               void *output_buf,        // a3: cubin output buffer (NULL for standalone)
               int   extra_opt_count,   // a4: count of extra options from nvcc
               char **extra_opts);      // a5: array of extra option strings
```

When `main` calls this, a3/a4/a5 are all zero -- standalone mode. When nvcc loads ptxas as a shared library and calls the entry point directly, these arguments carry non-null values:

- **a3 (output_buf)**: Pointer to a memory buffer where the compiled cubin is written. Eliminates the need for temporary files and filesystem round-trips, which matters for large CUDA compilations where nvcc may invoke ptxas hundreds of times.
- **a4 (extra_opt_count)**: Number of additional option strings injected by nvcc beyond what appears on the command line.
- **a5 (extra_opts)**: Array of those extra option strings.

Additionally, callback function pointers at offsets 37--39 of the 1,352-byte options block (byte offsets ~296, ~304, ~312) allow the host process to receive progress notifications and diagnostic messages in-process rather than through stderr.

### Error Recovery with setjmp/longjmp

The first significant action in `sub_446240` is establishing a `setjmp` recovery point:

```c
if (setjmp(jmp_buf) != 0) {
    // Fatal error occurred somewhere in the pipeline.
    // Clean up and return non-zero exit code.
    goto cleanup;
}
```

This is the **only** error recovery mechanism in ptxas -- there are no C++ exceptions (the binary is compiled as C, not C++). Any function anywhere in the call tree that encounters an unrecoverable error calls `sub_42FBA0` with severity >= 6, which internally calls `longjmp(jmp_buf, 1)` to unwind directly back to this point. The approach is simple but has a critical implication: all resources allocated between the `setjmp` and the fatal error are leaked unless explicitly tracked and cleaned up at the recovery site.

### The 1,352-Byte Options Block

The master options block lives on the stack and accumulates all compilation state during option parsing. It is passed by pointer to virtually every subsystem. Key fields (approximate offsets based on access patterns):

| Offset Range | Purpose |
|---|---|
| 0--63 | Input/output file paths, PTX version, target SM |
| 64--127 | Optimization level, debug flags, cache modes |
| 128--255 | Register limits, occupancy constraints |
| 256--295 | Warning/error control flags |
| 296--319 | Library-mode callback function pointers (offsets 37--39) |
| 320--1351 | Per-pass configuration, knob overrides, feature flags |

### Compilation Loop

After option parsing and PTX input setup, the driver enters a loop over compile units. Each unit corresponds to one entry function (or device function in `--compile-only` mode). The per-entry processing is handled by `sub_43CC70`, which prints a separator:

```c
printf("\n# ============== entry %s ==============\n", entry_name);
```

and then sequences: DAGgen (PTX-to-Ori lowering), OCG (optimization and code generation), ELF (binary emission), and DebugInfo (DWARF generation). The special entry `__cuda_dummy_entry__` is silently skipped.

### Timing and Memory Statistics

When `--compiler-stats` is active, `sub_446240` prints per-phase timing and peak memory after all compile units complete:

```text
CompileTime = 42.3 ms (100%)
Parse-time            : 12.1 ms (28.61%)
CompileUnitSetup-time :  1.4 ms ( 3.31%)
DAGgen-time           :  8.7 ms (20.57%)
OCG-time              : 15.2 ms (35.93%)
ELF-time              :  3.8 ms ( 8.98%)
DebugInfo-time        :  1.1 ms ( 2.60%)
PeakMemoryUsage = 2048.000 KB
```

When `--compiler-stats-file` is specified, the same data is written in JSON format using the shared JSON builder (`sub_1CBA950`). When `--fdevice-time-trace` is active, `sub_439880` parses Chrome DevTools trace format JSON and merges ptxas timing events into the trace.

## Option Parser -- `sub_434320` and `sub_432A00`

Option parsing is split into two phases: registration and processing.

### Option Registration -- `sub_432A00`

This 6,427-byte function calls `sub_1C97210` approximately 100 times, once per recognized option. Each call provides the option's long name, short name, value type, help text, and default value to the generic option framework (implemented in the `0x1C96xxx`--`0x1C97xxx` range, shared with other NVIDIA tools).

| Option | Short | Type | Help Text |
|---|---|---|---|
| `--arch` | `-arch` | string | "Specify the 'sm_' name of the target architecture" |
| `--output-file` | `-o` | string | "Specify name and location of the output file" |
| `--opt-level` | `-O` | int | "Specify optimization level" |
| `--maxrregcount` | — | int | "Specify the maximum number of registers" |
| `--register-usage-level` | — | enum(0..10) | Register usage reporting level |
| `--verbose` | `-v` | bool | Verbose output |
| `--version` | `-V` | — | Print version and exit |
| `--compile-only` | — | bool | Compile without linking |
| `--compile-functions` | — | string | "Entry function name" |
| `--input-as-string` | — | string | "PTX string" (compile from memory) |
| `--fast-compile` | — | bool | Reduce compile time at cost of code quality |
| `--suppress-stack-size-warning` | — | bool | Suppress stack size warnings |
| `--warn-on-local-memory-usage` | — | bool | Warn when local memory is used |
| `--warn-on-spills` | — | bool | Warn on register spills |
| `--warn-on-double-precision-use` | — | bool | Warn on FP64 usage |
| `--compiler-stats` | — | bool | Print compilation timing |
| `--compiler-stats-file` | — | string | "/path/to/file" (JSON output) |
| `--fdevice-time-trace` | — | string | Chrome trace JSON output |
| `--def-load-cache` | — | enum | Default load cache operation |
| `--force-load-cache` | — | enum | Force load cache operation |
| `--position-independent-code` | — | bool | Generate PIC |
| `--compile-as-tools-patch` | — | bool | CUDA sanitizer/tools patch mode |
| `--extensible-whole-program` | — | bool | Whole-program compilation |
| `--cloning` | — | enum(yes/no) | Inline cloning control |
| `--ptxlen` | — | — | PTX length statistics |
| `--list-version` / `--version-ls` | — | — | List supported PTX versions |
| `--disable-smem-reservation` | — | bool | Disable shared memory reservation |
| `--generate-relocatable-object` | `-c` | bool | Generate relocatable object |

### Option Processing -- `sub_434320`

The 10,289-byte parser iterates over argv (and any extra options from library mode), matches each argument against registered options via the framework, and populates fields in the 1,352-byte options block. Special handling exists for:

- **`--version`**: Prints the identification string "Ptx optimizing assembler" followed by the version (e.g., "Cuda compilation tools, release 13.0, V13.0.88") and exits.
- **`--help`**: Delegates to `sub_403588`, which prints `"Usage  : %s [options] <ptx file>,...\n"` followed by all registered options, then exits.
- **`--fast-compile`**: Validated against conflicting optimization options.
- **`-cloning=yes`/`-cloning=no`**: Inline cloning control parsed as an equality option.

### Generic Option Framework

The option parsing library lives in the `0x1C96000`--`0x1C97FFF` range and is shared with other NVIDIA tools (nvlink, fatbinary, etc.):

| Address | Identity | Role |
|---|---|---|
| `sub_1C960C0` | Option parser constructor | Creates the option parser state |
| `sub_1C96680` | Argv processor | Matches argv entries against registered options |
| `sub_1C97210` | Option registrar | Registers one option with name, type, help |
| `sub_1C97640` | Help printer | Iterates all registered options, prints help text |

## Diagnostic System -- `sub_42FBA0`

The central diagnostic emitter is the most important error-reporting function in ptxas. With 2,350 call sites, it handles every warning, error, and fatal message in the entire binary.

### Signature

```c
void sub_42FBA0(
    int *descriptor,    // a1: points to severity level at *a1
    void *location,     // a2: source location context
    ...                 // variadic: printf-style format args
);
```

### Severity Levels

| Level | Prefix | Tag | Behavior |
|---|---|---|---|
| 0 | (none) | — | Suppressed -- message is silently discarded |
| 1 | `"info    "` | `@I@` | Informational |
| 2 | `"info    "` | `@I@` | Informational (alternate) |
| 3 | `"warning "` / `"error   "` | `@W@` / `@E@` | Warning, promoted to error if TLS[50] set |
| 4 | `"error*  "` | `@O@` | Non-fatal error with special marker |
| 5 | `"error   "` | `@E@` | Non-fatal error |
| 6 | `"fatal   "` | `@E@` | Fatal -- triggers `longjmp(jmp_buf, 1)` |

The machine-readable tags (`@E@`, `@W@`, `@O@`, `@I@`) allow nvcc and other tools to parse ptxas output programmatically, extracting severity without parsing the human-readable text.

### Warning-to-Error Promotion

Severity level 3 has context-dependent behavior controlled by two flags in the thread-local storage:

```c
v5 = *a1;   // severity
if (v5 == 3) {
    if (sub_4280C0()[49])   // TLS offset 49: suppression flag
        return;             // silently discard
    if (sub_4280C0()[50])   // TLS offset 50: Werror flag
        prefix = "error   ";
    else
        prefix = "warning ";
}
```

This implements the `--Werror` equivalent: when the Werror flag is active in the TLS context, all warnings become errors.

### Output Format

```text
<filename>, line <N>; <severity>: <message>
```

When source is available, the diagnostic emitter reads the PTX input file, seeks to line N, and prints the source line prefixed with `"# "`. To avoid O(n) seeking through large files on every diagnostic, it maintains a hash map (`sub_426150`/`sub_426D60`) that caches file byte offsets every 10 lines for fast random access to arbitrary line numbers.

### Fatal Error Handler -- `sub_42BDB0`

A 14-byte wrapper called from 3,825 sites (nearly every allocation in ptxas). It fires whenever the pool allocator `sub_424070` returns NULL:

```c
void sub_42BDB0(...) {
    return sub_42F590(&unk_29FA530, ...);   // internal error descriptor
}
```

The descriptor at `unk_29FA530` has severity 6 (fatal), so this always triggers `longjmp` back to the driver's recovery point.

## Thread-Local Storage -- `sub_4280C0`

The most-called function in the entire binary (3,928 callers). Returns a pointer to a 280-byte per-thread context struct, allocating and initializing it on first access.

```c
void *sub_4280C0(void) {
    void *ctx = pthread_getspecific(key);
    if (ctx) return ctx;

    ctx = malloc(0x118);        // 280 bytes
    memset(ctx, 0, 0x118);
    pthread_cond_init(ctx + 128, NULL);
    pthread_mutex_init(ctx + 176, NULL);
    sem_init(ctx + 216, 0, 0);
    pthread_setspecific(key, ctx);
    return ctx;
}
```

### TLS Context Layout (280 bytes)

| Offset | Size | Type | Purpose |
|---|---|---|---|
| 0 | 8 | int/flags | Error/warning state flags |
| 8 | 8 | int | `has_error` flag |
| 49 | 1 | byte | Diagnostic suppression flag |
| 50 | 1 | byte | Werror promotion flag |
| 128 | 48 | `pthread_cond_t` | Condition variable |
| 176 | 40 | `pthread_mutex_t` | Per-thread mutex |
| 216 | 32 | `sem_t` | Semaphore for synchronization |

The TLS key is created by `ctor_001` before `main` runs, and a destructor function registered via `pthread_key_create` frees the 280-byte struct when a thread terminates. This per-thread context enables concurrent compilation of multiple compile units (when the thread pool is active), with each thread maintaining independent error state and diagnostic suppression flags.

## PTX Input Setup -- `sub_4428E0`

After options are parsed, this 13,774-byte function reads and preprocesses the PTX input:

1. **Version and target validation.** Checks `.version` and `.target` directives in the input. Emits synthetic headers (`"\t.version %s\n"`, `"\t.target  %s\n"`) when needed.

2. **Compile-only mode.** When `--compile-only` is active and no real entries exist, generates a dummy entry: `"\t.entry %s { ret; }\n"` with name `__cuda_dummy_entry__`.

3. **Input-as-string mode.** When `--input-as-string` is active, PTX is read from memory (passed as a string argument) rather than from a file. This is used by the library-mode interface.

4. **Whole-program mode.** `--extensible-whole-program` enables inter-function optimization across all entries in the compilation unit.

5. **Cache and debug configuration.** Applies `--def-load-cache`, `--def-store-cache`, `--force-load-cache`, `--force-store-cache`, and `suppress-debug-info` settings.

6. **Tools-patch mode.** `--compile-as-tools-patch` activates the CUDA sanitizer compilation path, checking for `__cuda_sanitizer` symbols.

Key diagnostic strings from this function:
- `"'--fast-compile'"`
- `"calls without ABI"`
- `"compilation without ABI"`
- `"device-debug or lineinfo"`
- `"unified Functions"`

## Target Configuration -- `sub_43A400`

A 4,696-byte function that configures target-specific defaults after option parsing completes. It reads the SM architecture number from the options block and sets:

- **Texturing mode**: `texmode_unified` vs raw texture mode.
- **Cache defaults**: Based on architecture capabilities.
- **Feature flags**: Hardware-specific workaround flags (e.g., `--sw4575628`).
- **Indirect function support**: `"Indirect Functions or Extern Functions"` validation.

The function references `"NVIDIA"` and `"ptxocg.0.0"` (the internal name for the OCG optimization pass), suggesting it also initializes the pass pipeline configuration for the target architecture.

## Register Constraint Calculator -- `sub_43B660`

A 3,843-byte function that resolves potentially conflicting register limit specifications into a single register budget per function. Register constraints come from four sources with different priorities:

| Source | Directive/Option | Priority |
|---|---|---|
| PTX directive | `.maxnreg N` | Per-function, highest priority |
| CLI option | `--maxrregcount N` | Global, overridden by `.maxnreg` |
| PTX directive | `.minnctapersm N` | Occupancy target, derived limit |
| PTX directive | `.maxntid Nx,Ny,Nz` | Thread block size, derived limit |

The occupancy-derived limit is computed from `.minnctapersm` and `.maxntid`: given a minimum number of CTAs per SM and a maximum thread count per CTA, the function calculates the maximum register count that allows the requested occupancy level, accounting for per-SM register file size.

Diagnostic strings indicate the resolution process:
- `"computed using thread count"` -- derived from `.maxntid`
- `"of .maxnreg"` -- explicit per-function limit
- `"of maxrregcount option"` -- CLI override
- `"global register limit specified"` -- global cap applied

## Per-Entry Compilation -- `sub_43CC70`

A 5,425-byte function that processes each entry function through the complete backend pipeline. For each entry:

1. Skips `__cuda_dummy_entry__` (generated by compile-only mode).
2. Prints the entry separator: `"\n# ============== entry %s ==============\n"`.
3. Runs DAGgen (PTX-to-Ori lowering).
4. Runs OCG (the 159-phase optimization pipeline + SASS code generation).
5. Generates `.sass` and `.ucode` ELF sections.
6. Generates DWARF debug information if requested.

The function also handles `reg-fatpoint` configuration (the register allocation algorithm, documented in the [Fatpoint Algorithm](../regalloc/algorithm.md) page).

## Function/ABI Setup -- `sub_43F400`

A 9,078-byte function that configures the calling convention for each function before compilation. This includes:

| Resource | Diagnostic String |
|---|---|
| Parameter passing registers | `"number of registers used for parameter passing"` |
| First parameter register | `"first parameter register"` |
| Return address register | `"return address register"` |
| Scratch data registers | `"scratch data registers"` |
| Scratch control barriers | `"scratch control barriers"` |
| Call prototype | `"callprotoype"` (sic -- misspelled in binary) |
| Call target | `"calltarget"` |

The function handles both entry functions (kernels launched from the host) and device functions (callable from other device code), with different ABI requirements for each. Entry functions use a simplified ABI where parameters come from constant memory, while device functions use register-based parameter passing.

The `--compile-as-tools-patch` and `--sw200428197` flags activate a special ABI variant for CUDA sanitizer instrumentation, which inserts additional scratch registers for sanitizer state.

## Function Map

| Address | Size | Callers | Identity |
|---|---|---|---|
| `0x409460` | 84 B | — | `main` (entry point thunk) |
| `0x4094C0` | 204 B | — | `ctor_001` (thread infrastructure init) |
| `0x4095D0` | 17 KB | — | `ctor_003` (ROT13 opcode table, ~900 entries) |
| `0x40D860` | 80 KB | — | `ctor_005` (ROT13 knob registry, 2000+ entries) |
| `0x421290` | 8 KB | — | `ctor_007` (scheduler knob registry, 98 entries) |
| `0x403588` | 75 B | 1 | Usage printer (`--help`) |
| `0x4280C0` | 597 B | 3,928 | TLS context accessor (280-byte struct) |
| `0x42BDB0` | 14 B | 3,825 | OOM fatal error handler |
| `0x42FBA0` | 2.4 KB | 2,350 | Central diagnostic emitter |
| `0x42F590` | — | 1 | Internal fatal error handler |
| `0x430570` | — | 2 | Program name getter |
| `0x432A00` | 6.4 KB | 1 | Option registration (~100 options) |
| `0x434320` | 10 KB | 1 | Option parser and validator |
| `0x439880` | 2.9 KB | 1 | Chrome trace JSON parser |
| `0x43A400` | 4.7 KB | 1 | Target configuration |
| `0x43B660` | 3.8 KB | 1 | Register constraint calculator |
| `0x43CC70` | 5.4 KB | 1 | Per-entry compilation processor |
| `0x43F400` | 9 KB | 1 | Function/ABI setup |
| `0x4428E0` | 13.8 KB | 1 | PTX input setup and preprocessing |
| `0x446240` | 11 KB | 1 | Compilation driver (real main) |
| `0x451730` | 14 KB | 1 | Parser/lexer init + special registers |
| `0x46E000` | 93 KB | 1 | Opcode-to-handler dispatch table builder |
| `0x1C960C0` | — | — | Option parser constructor |
| `0x1C96680` | — | — | Argv processor |
| `0x1C97210` | — | ~100 | Option registrar (per-option) |
| `0x1C97640` | — | 1 | Help text printer |
| `0x1CBA950` | — | — | JSON context constructor |
| `0x1CBAC20` | 2.9 KB | 3 | JSON recursive descent parser |

## Cross-References

- [Pipeline Overview](overview.md) -- full PTX-to-SASS compilation flow
- [CLI Options](../config/cli-options.md) -- complete option catalog
- [Knobs System](../config/knobs.md) -- the 2,000+ Mercury tuning knobs registered in ctor_005
- [Memory Pool Allocator](../infra/memory-pools.md) -- the allocator (`sub_424070`) that calls `sub_42BDB0` on OOM
- [Hash Tables & Bitvectors](../infra/hash-bitvector.md) -- the hash map used by diagnostics for line offset caching
- [Thread Pool & Concurrency](../infra/threading.md) -- thread pool that creates the TLS contexts
- [PTX Parser](ptx-parser.md) -- the parser initialized by `sub_451730`
- [Optimization Pipeline](optimizer.md) -- the 159-phase pipeline invoked per compile unit
- [Fatpoint Algorithm](../regalloc/algorithm.md) -- register allocation referenced in per-entry compilation
