# TfTpu_Initialize Bootstrap

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

`PJRT_Plugin_Initialize` (PJRT slot 8) is the framework's one button to press, but the work it triggers is the TPU driver bootstrap: parse a flag string, acquire a cross-process lock, and run the Google module-init DAG that finally *executes* the HAL/platform registrations the `dlopen`-time constructor storm only *recorded*. That bootstrap is a small, sharp call chain — `TryAcquireTpuLock` → `GetLibTpuInitArguments` → `InitializeDriver` → `InitGoogleExceptChangeRootAndUser` → `RealInitGoogle` → `GoogleInitializer::RunInitializers` — and it is fully idempotent: a single function-static `has_initialized` byte inside `InitializeDriver` makes every call after the first a no-op. This page owns that chain. The PJRT-side gate (the `struct_size` compat check, the `kPjRtCApiTpuInitType` selector, the error-wrapper boxing) is on [module-init-plugin-discovery.md](module-init-plugin-discovery.md) §3 and only summarized here; what this page adds is the *interior* of `InitializeDriver`, the *interior* of the option ingest, and the role of the `TfTpu_*ApiFn()` C-API function-pointer tables.

The decisive structural fact for a reimplementer is that there is **no single `TfTpu_Initialize` orchestrator that builds the runtime in one body**. The name `TfTpu_Initialize` is a real exported C symbol — at `0xe6f54a0`, exported `@@VERS_1.0`, 10 bytes — but it is a two-instruction tail-shim that forwards straight to `tpu::driver::InitializeDriver @ 0x204cecc0`, and the binary actually reaches `InitializeDriver` through the PJRT path (`PJRT_Plugin_Initialize`), not through `TfTpu_Initialize`. `InitializeDriver` itself does only three things: fold default Cloud-TPU flags into an argv, hand that argv to Google's `InitGoogle` flag-parse-and-run-modules machinery, and register a handful of telemetry gauges. Everything order-critical happens *inside* `RunInitializers`, in topological dependency order, decoupled from C++ static-init order. This is the classic Google `base/init_google` pattern: register at load, run at first init.

The third subject — the `TfTpu_*ApiFn()` tables — is the reason the bootstrap matters to the rest of the runtime. libtpu carries a *second*, older C-ABI surface beside PJRT: the StreamExecutor TPU shim, dispatched through structs of raw C function pointers (`stream_executor::tpu::ExecutorApiFn()`, `OpsApiFn()`, `ProfilerApiFn()`). Each is a leak-on-exit Meyers singleton holding the `TfTpu_*Fn` roster. The bootstrap's `RunInitializers` is what runs `RegisterTpuPlatform`, which *reads* `ExecutorApiFn()` (via `IsStreamExecutorEnabled`) and, if the table's first slot is non-null, installs the StreamExecutor `TpuPlatform` beneath PJRT. So the bootstrap is the moment the two C-ABI surfaces are wired together.

For reimplementation, the contract is:

- **The `InitializeDriver` body** — the `has_initialized` once-guard, the `"./tpu_driver"` synthetic `argv[0]`, `AppendNewCloudTPUArgs`, the `vector<string>` → `char**` argv materialization, the `InitGoogleExceptChangeRootAndUser(name="N/A", &argc, &argv, change_root=0)` call, and the four telemetry registrations.
- **The option ingest** — `GetLibTpuInitArguments` reads env `LIBTPU_INIT_ARGS`, splits on space, builds a `vector<string>` then a `vector<char const*>`; `RealInitGoogle` consumes it through `ParseCommandLineNonHelpFlags` (the `--xla_*` / `TPU_*` absl-flag parse), pre-registered by the `dlopen` constructor storm.
- **The ApiFn tables** — the three `stream_executor::tpu::*ApiFn()` Meyers singletons and the `IsStreamExecutorEnabled` / `IsInitialized` slot-0 probe that gates StreamExecutor `TpuPlatform` installation during the DAG run.

| | |
|---|---|
| **Bootstrap gate** | `pjrt::tpu_plugin::PJRT_Plugin_Initialize @ 0xe6a9d00` (303 B), PJRT slot 8 |
| **Driver bring-up** | `tpu::driver::InitializeDriver @ 0x204cecc0` (1764 B) |
| **`TfTpu_Initialize` symbol** | `@ 0xe6f54a0` (10 B, `@@VERS_1.0`) — 2-instruction tail-shim → `InitializeDriver` (alternate entry, not the PJRT path) |
| **Option ingest** | `tensorflow::tpu::GetLibTpuInitArguments @ 0x20ccca20` (851 B), env `LIBTPU_INIT_ARGS` |
| **Lock gate** | `tensorflow::tpu::TryAcquireTpuLock @ 0x20ccbc40` (3531 B), env `TPU_LOAD_LIBRARY` |
| **Flag parse + DAG run** | `RealInitGoogle @ 0x210ae860` → `ParseCommandLineNonHelpFlags` + `GoogleInitializer::RunInitializers @ 0x210b2d20` |
| **Idempotence guard** | `InitializeDriver::has_initialized` (function-static byte `@ 0x225899e0`, `.bss`) |
| **Init-type selector** | `kPjRtCApiTpuInitType` (statically `= 2`) `@ 0x22255b40` (`.data`) |
| **ApiFn tables** | `ExecutorApiFn @ 0x20819360`, `OpsApiFn @ 0x10900e80`, `ProfilerApiFn @ 0x10900ea0` (Meyers singletons) |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The Two Entries Into `InitializeDriver`

### Purpose

`tpu::driver::InitializeDriver @ 0x204cecc0` is the actual driver bootstrap body. Two distinct callers reach it, and a reimplementer must understand that they are *parallel* entries to the *same* idempotent function — not a layered call stack.

### Entry Point

```text
PATH A — the PJRT path (the one the framework drives)
  PJRT_Plugin_Initialize  0xe6a9d00  (slot 8)
    ├─ ActualStructSizeIsGreaterOrEqual("PJRT_Plugin_Initialize_Args", 27, 16, args->struct_size)
    ├─ if kPjRtCApiTpuInitType != 0:                       (statically 2)
    │    ├─ TryAcquireTpuLock("PJRT_Plugin_Initialize_Args")   0x20ccbc40  ── env TPU_LOAD_LIBRARY
    │    ├─ GetLibTpuInitArguments(&argv_vec)                  0x20ccca20  ── env LIBTPU_INIT_ARGS
    │    └─ InitializeDriver(flag=1, argc, argv,
    │                        init_type_is_2 = (kPjRtCApiTpuInitType == 2))   0x204cecc0
    └─ else: return NULL  (init-type 0 → no-op)

PATH B — the TfTpu_Initialize alternate entry
  TfTpu_Initialize  0xe6f54a0   (10 B, 2 instructions)
    mov $0x1, %ecx                                            (hardcodes 4th arg init_type_is_2 = true)
    jmp tpu::driver::InitializeDriver                         0x204cecc0   (tail-jump; rdi/esi/rdx from caller)
```

> **NOTE —** the page title names `TfTpu_Initialize`, but the live path is Path A. `TfTpu_Initialize @ 0xe6f54a0` is genuinely a 10-byte forwarding shim — its whole body is two instructions, `mov $0x1, %ecx` then `jmp InitializeDriver`. The only work it does is hardcode the 4th argument (the `bool init_type_is_2` of `InitializeDriver(bool, int, char const**, bool)`, passed in `%ecx`) to `1`; the other three arguments (`rdi`/`esi`/`rdx` = driver_flag, argc, argv) pass through untouched from the caller, and the `jmp` (not `call`) makes this a tail-shim with no own frame. (The IDA C reconstruction renders the hardcoded `%ecx` as a bogus `&dword_0+1` argv pointer; the disassembly is authoritative — `%ecx` is the trailing `bool`, not argv.) It exists so legacy `tensorflow/core/tpu/` callers (which spell driver bring-up as `TfTpu_Initialize`, the same way the legacy StreamExecutor C-ABI spells everything `TfTpu_*`) reach the same body the PJRT gate reaches. A reimplementation can ship either or both spellings; both must funnel to one `InitializeDriver` guarded by one `has_initialized` byte, or a process that touches *both* surfaces double-initializes.

### Algorithm

```c
function InitializeDriver(bool driver_flag, int argc, char const** argv_in, bool init_type_is_2):   // 0x204cecc0
    // (a) hard idempotence gate — first instruction
    if driver_flag == 0 || (has_initialized & 1) != 0:    // function-static byte
        return                                             // already up, or disabled

    // (b) synthesize argv[0] and fold in Cloud-TPU defaults
    args = new vector<string>()
    args[0] = "./tpu_driver"                               // 24-B std::string, SSO inline
    AppendNewCloudTPUArgs(&args, argc, argv_in)            // 0x204c7340 — append Cloud-TPU flags + caller argv

    // (c) flatten vector<string> -> char**  (SSO-aware: byte-23 sign bit = heap/inline)
    n   = args.size()
    cargv = malloc(8 * (n + 1))                            // +1 for the NULL terminator
    for i in 0..n:
        cargv[i] = args[i].is_long() ? args[i].heap_ptr : &args[i].inline_buf  // byte 23 < 0 ⇒ heap
    cargv[n] = NULL

    // (d) parse flags + run the module DAG (the real work)
    InitGoogleExceptChangeRootAndUser("N/A", 3, &n, &cargv, change_root=0)    // 0x210b0180
    //   → RealInitGoogle:  ParseCommandLineNonHelpFlags(&n, &cargv)          // the --xla_*/TPU_* parse
    //   → RealInitGoogle:  GoogleInitializer::RunInitializers("module")      // *** runs HAL + platform ***
    free(cargv)

    // (e) telemetry gauges
    RegisterLibtpuGaugeTelemetry("megascale.error.detected.gauge", 30, 1)
    RegisterMegascaleErrorHandler("megascale.error.detected.gauge")
    RegisterLibtpuGaugeTelemetry("slice.error.detected.gauge", 26, 1)
    if EnableRuntimeUptimeTelemetry():                                       // anon-ns predicate
        InitializeUptimeMetricViaEnvironmentVariables(0, 26)

    // (f) latch the guard, free the temporary vector<string>
    has_initialized = 1
    destroy(args)                                          // frees each long string then the buffer
    return
```

> **GOTCHA —** the synthetic `argv[0]` is the string literal `"./tpu_driver"`, written as a 24-byte inline `std::string` (the decompile shows `strcpy(v7, "./tpu_driver")` into an `operator new(0x18)` slot with a size byte of `0x0C`). `RealInitGoogle` then treats this as the program name in `SetProgramUsageMessage` and the logged `argv[0]`. A reimplementer who passes the *host* process's real `argv[0]` will change what absl sees as the program name and may alter flag-file / usage behavior; libtpu deliberately fabricates a stable name so the TPU driver's view of the command line is independent of the embedding process.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `tpu::driver::InitializeDriver` | `0x204cecc0` | 1764 B | the bootstrap body (guard, argv, InitGoogle, telemetry) | CONFIRMED |
| `TfTpu_Initialize` | `0xe6f54a0` | 10 B | 2-instruction tail-shim (`mov $1,%ecx; jmp`) → `InitializeDriver` (legacy/alternate entry) | CONFIRMED |
| `tpu::driver::AppendNewCloudTPUArgs` | `0x204c7340` | — | folds Cloud-TPU default flags into the argv `vector<string>` | CONFIRMED |
| `InitGoogleExceptChangeRootAndUser` | `0x210b0180` | 8 B | thin wrapper → `RealInitGoogle(…, change_root=0)` | CONFIRMED |
| `RealInitGoogle` | `0x210ae860` | large | flag parse + `RunInitializers` + process-wide init | CONFIRMED |
| `GoogleInitializer::RunInitializers` | `0x210b2d20` | — | the topological module-DAG run (PHASE B) | CONFIRMED |
| `RegisterLibtpuGaugeTelemetry` | — | — | registers a named telemetry gauge | CONFIRMED |
| `RegisterMegascaleErrorHandler` | — | — | installs the Megascale error-detection handler | CONFIRMED |
| `EnableRuntimeUptimeTelemetry` | — | — | anon-ns predicate gating uptime metrics | HIGH |

### Considerations

The `has_initialized` byte is the *only* idempotence guarantee in `InitializeDriver`; the PJRT gate's `TryAcquireTpuLock` once-lock and the DAG's own per-module run-state are separate, layered guards. So the bootstrap is idempotent at three independent levels — re-calling `PJRT_Plugin_Initialize` is a fast no-op because the lock is already held *and* `has_initialized` is set *and* every module's run-state is `DONE`. A reimplementation that drops any one of the three is still safe under normal single-threaded init, but loses idempotence under one of the concurrent or repeated-init scenarios the others cover.

---

## 2. The Option Ingest — `LIBTPU_INIT_ARGS` and the Flag Parse

### Purpose

The TPU runtime is configured almost entirely by absl command-line flags, but a plugin `.so` has no command line of its own. libtpu fabricates one from an environment variable. `GetLibTpuInitArguments @ 0x20ccca20` turns `LIBTPU_INIT_ARGS` into an argv-style vector; `InitializeDriver` prepends `argv[0]` and Cloud-TPU defaults; `RealInitGoogle` hands the result to `ParseCommandLineNonHelpFlags`, which binds it to the absl flag tables the `dlopen` constructor storm pre-registered.

### Algorithm

```c
function GetLibTpuInitArguments() -> {vector<string> store, vector<char const*> argv}:  // 0x20ccca20
    s = getenv("LIBTPU_INIT_ARGS")                 // str @ file 0x918c880
    if s == NULL:
        return { {}, {} }                          // empty argv

    // (a) split the env string on ' ' (space char, 0x20) into string_views
    views = absl::StrSplit(string_view(s, strlen(s)), ByChar(' '), AllowEmpty)

    // (b) deep-copy each view into an owned std::string (24-B SSO records, NUL-terminated)
    store = vector<string>()
    for v in views:
        store.push_back(string(v.data, v.len))     // long strings heap-alloc; short ones inline

    // (c) materialize a parallel vector<char const*> pointing at each owned string's data
    argv = vector<char const*>()
    for str in store:
        argv.push_back(str.is_long() ? str.heap_ptr : &str.inline_buf)

    return { store, argv }                          // store owns the bytes; argv is the C view
```

> **NOTE —** the split is on a single space with `AllowEmpty`, not a shell-style tokenizer: there is no quote handling, no escape processing, no tab/newline splitting. `LIBTPU_INIT_ARGS="--xla_tpu_foo=1  --bar"` with a double space yields an empty-string token between the two flags. Cloud-TPU production sets this var to a space-joined list of `--xla_*` / `--tpu_*` flags; the reimplementer must reproduce the *plain space split*, because any flag value that itself contains a space will be split into separate (and likely rejected) tokens.

### How the parse consumes it

`InitializeDriver` builds the final `char**` as `["./tpu_driver", <Cloud-TPU defaults…>, <LIBTPU_INIT_ARGS tokens…>, NULL]` and passes `&argc`/`&argv` into `InitGoogleExceptChangeRootAndUser`, which forwards to `RealInitGoogle @ 0x210ae860`. There the flag parse is `ParseCommandLineNonHelpFlags(&argc, &argv, remove_flags)` (the non-help variant, so `--help` is not special-cased here), bracketed by `GoogleInitializer::Require("command_line_flags_parsing")` and `Require("command_line_flags_parsed")` module gates. Unrecognized flags are handled by the absl flag machinery, not by libtpu directly.

> **QUIRK —** the flag *registry* is built at `dlopen`, not here. The `--xla_*` / `TPU_*` flags exist because hundreds of `_GLOBAL__sub_I_*_flags.cc` static constructors ran during the `INIT_ARRAY` storm and populated the absl flag tables (`debug_options_flags.cc`, `deepsea_platform_flags.cc`, and the per-module `FLAGS_tf_jf_*` definitions registered by `_GLOBAL__sub_I_tpu_platform_registration.cc`). `ParseCommandLineNonHelpFlags` only *binds values to* that pre-existing registry. A reimplementer cannot defer flag *registration* to init time and still have init-time parsing succeed — the registry must already be populated when `RealInitGoogle` runs. The set of recognized flag names is therefore defined by the constructor storm ([module-init-plugin-discovery.md](module-init-plugin-discovery.md) §2.2), not by this function.

### What `RealInitGoogle` does beyond the parse

`RealInitGoogle` is a full `InitGoogle` body, not just a flag parser. Before and after the parse it runs the standard Google process bring-up — `InitializeSymbolizer`, `StartUpWallTimer`, kernel-version logging, an `mlock_style` flag switch (`mlockall` of code pages), `nice`-priority adjustment, terminate-handler / signal-handler installation, hugepage remapping of `.data` and `.text`, RCU domain init, and CPU/wall profiler registration — then calls `GoogleInitializer::RunInitializers("module")` (the DAG run) and finally flips `init_google_state = 2` and notifies `InitGoogleDoneNotification`. For the bootstrap's purpose only two of these matter: the flag parse and the DAG run. The rest are documented as the surrounding `InitGoogle` behavior so a reimplementer knows they happen *inside* TPU bootstrap and are not separately invokable.

---

## 3. The DAG Run — Where Registration Becomes Execution

### Purpose

`GoogleInitializer::RunInitializers("module") @ 0x210b2d20`, called from inside `RealInitGoogle` at the tail of bootstrap, is where the HAL factories, XLA target functors, and the StreamExecutor `TpuPlatform` that the constructor storm only *registered* are finally *run*, in topological dependency order. This page does not re-document the DAG machinery — that is [module-init-plugin-discovery.md](module-init-plugin-discovery.md) §3 — but it owns the boundary: the bootstrap is the trigger, and one of the modules it runs is what wires in the ApiFn tables of §4.

### What runs

```text
RealInitGoogle  0x210ae860
  └─ GoogleInitializer::RunInitializers("module")   0x210b2d20      *** PHASE B ***
       └─ drive registered modules in topological dep order:
            google_init_module_tpu_hal_{jxc,pxc,vxc,glc,gfc}_*
              → TpuHalFactory::Register(PlatformType, TpuVersion, factory)  0x1fbb16a0
                 (register per-TpuVersion; NO silicon scan yet)
            google_init_module_xla_target_{jellyfish,…,ghostlite}
              → RegisterTargetCreationFunctor(N, …)
            google_init_module_tpu_platform   0x213eabc0  (jmp)
              → RegisterTpuPlatform   0xe99a3a0   ── reads ExecutorApiFn() (§4),
                                                     installs StreamExecutor TpuPlatform
            … all other registered modules in dep order …
```

> **QUIRK —** silicon is still not touched here. The HAL factories register *by `(PlatformType, TpuVersion)`*; the PCI-device-ID scan that picks the live `TpuVersion` runs two stages later, inside `PJRT_Client_Create` (slot 15). So the bootstrap leaves the runtime with a fully populated *factory registry* and a fully populated *flag state*, but no chosen device. TpuVersion detection is deferred: register at first init (here), detect at first client.

---

## 4. The `TfTpu_*ApiFn()` Function-Pointer Tables

### Purpose

Beside PJRT, libtpu carries the legacy StreamExecutor TPU C-ABI: 194 exported `Tpu<Class>_<Method>` symbols (`@@VERS_1.0`; e.g. `TpuExecutor_*` ×25, `TpuTransferManager_*` ×19, `TpuStream_*` ×8, …) dispatched through structs of raw C function pointers. The dispatch indirection is a small family of accessor functions, each returning a process-global table. The bootstrap is where these tables become relevant: the `RunInitializers` step runs `RegisterTpuPlatform`, which *reads* the executor table to decide whether to install the StreamExecutor platform. The tables' *contents* (the per-roster `TfTpu_*Fn` slot-by-slot map) are owned by the shim overview — [../shim/overview.md](../shim/overview.md); this section owns only their shape, their storage, and how the bootstrap probes them.

### The accessors are Meyers singletons

```c
// Each is a one-line return-address-of-a-function-local-static — leak-on-exit, no destructor.
TfTpu_ExecutorApiFn* stream_executor::tpu::ExecutorApiFn():    // 0x20819360
    return &ExecutorApiFn::executor_api_fn                      // process-global table

TfTpu_OpsApiFn*      stream_executor::tpu::OpsApiFn():          // 0x10900e80
    return &OpsApiFn::ops_api_fn

TfTpu_ProfilerApiFn* stream_executor::tpu::ProfilerApiFn():     // 0x10900ea0
    return &ProfilerApiFn::profiler_api_fn
```

All three are the same idiom as the PJRT `pjrt_api` singleton: a function-local static whose storage lives in `.bss`/`.lbss`, returned by address. They are immutable for process lifetime once populated and are read without a lock. The table is a flat array of C function pointers — one slot per roster entry — so dispatch through it is a single indirect call, exactly the cost model of the legacy `c_api_decl.h` surface.

### How the bootstrap probes the executor table

`RegisterTpuPlatform @ 0xe99a3a0` (run inside the DAG, §3) gates platform installation on whether the executor table is live:

```c
function RegisterTpuPlatform():                          // 0xe99a3a0
    fn = stream_executor::tpu::ExecutorApiFn()            // 0x20819360 — the table
    if IsStreamExecutorEnabled(fn)                        // 0x20819380 — probe + handshake
       and !tpu_platform_registered:                      // byte guard @ 0x224c5388
        p = new tensorflow::tpu::TpuPlatform()            // 0xe999960, sizeof 0x98
        PlatformManager::RegisterPlatform(p)              // 0x1d0fe120
        tpu_platform_registered = 1
    return 1

function IsStreamExecutorEnabled(table):                 // 0x20819380
    if table[0] == NULL: return 0                         // slot 0 = init fn-ptr unset ⇒ disabled
    handle = table[0](table)                              // call init fn; returns an opaque handle
    if handle == NULL: return 0
    table[1](handle)                                      // call finalize fn (slot+8) on the handle
    return 1

function IsInitialized(table):                           // 0x208193c0
    return table[0] != NULL                               // cheap liveness probe — slot 0 non-null
```

> **GOTCHA —** the executor table is dispatched by *slot 0 being non-null*, not by a separate "enabled" flag. `IsInitialized` is the cheap probe (`table[0] != 0`); `IsStreamExecutorEnabled` is the heavier one — it actually *calls* slot 0 (an init thunk that returns an opaque handle) and then calls slot 1 (a finalize thunk) on that handle, treating a clean round-trip as proof the shim is wired. A reimplementer populating these tables must leave slot 0 NULL to keep StreamExecutor *off* (then `RegisterTpuPlatform` registers nothing and PJRT stands alone), or populate slot 0 with a working init/finalize pair to switch it *on*. There is no third state.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `stream_executor::tpu::ExecutorApiFn` | `0x20819360` | returns `&executor_api_fn` (Meyers singleton, executor roster table) | CONFIRMED |
| `stream_executor::tpu::OpsApiFn` | `0x10900e80` | returns `&ops_api_fn` (Meyers singleton, ops roster table) | CONFIRMED |
| `stream_executor::tpu::ProfilerApiFn` | `0x10900ea0` | returns `&profiler_api_fn` (Meyers singleton, profiler roster table) | CONFIRMED |
| `stream_executor::tpu::IsStreamExecutorEnabled` | `0x20819380` | slot-0 probe + init/finalize handshake (gates `TpuPlatform`) | CONFIRMED |
| `stream_executor::tpu::IsInitialized` | `0x208193c0` | cheap `table[0] != NULL` liveness probe | CONFIRMED |
| `tensorflow::tpu::RegisterTpuPlatform` | `0xe99a3a0` | reads executor table, installs StreamExecutor `TpuPlatform` | CONFIRMED |

### Considerations

The point a reimplementer must hold onto: `InitializeDriver` does **not** populate these tables. It runs the DAG, and the DAG runs `RegisterTpuPlatform`, which *reads* an already-populated `ExecutorApiFn()`. The table's population path (which `TfTpu_*Fn` setter writes the slots, and when) was not traced on this page — it is a separate concern owned by [../shim/overview.md](../shim/overview.md). What is byte-confirmed here is the *consumption* side: the accessor shape, the slot-0 dispatch rule, and the handshake `RegisterTpuPlatform` performs during bootstrap. The SE platform that ends up sitting beneath PJRT, and how it serves the `Tpu*_*` exports, is on [../pjrt/stream-executor-host-interpreter.md](../pjrt/stream-executor-host-interpreter.md).

> **NOTE —** in a freshly loaded libtpu the executor table's slot 0 is observed empty (the `.bss` singleton is zero-filled at load and no static ctor writes it). Whether any path populates it before `RegisterTpuPlatform` runs — and therefore whether the StreamExecutor `TpuPlatform` is installed at all in the PJRT-only configuration — was not traced (LOW confidence on the populated-vs-empty outcome; the *probe logic* is CONFIRMED). The PJRT stack does not require it: PJRT reaches the driver core directly, and the `Tpu*_*` exports wrap the same backing implementation independently.

---

## 5. The Bootstrap Once-Guards

### Purpose

The bootstrap is re-entrant-safe by stacking three independent once-mechanisms. A reimplementer reproducing only the PJRT gate, or only the driver guard, gets a different idempotence profile.

| Guard | Where | Address / token | What re-calling skips | Confidence |
|---|---|---|---|---|
| `absl::Mutex` once-lock | `TryAcquireTpuLock::mu` | guard `0x225925d0` / obj `0x225925c8` | the cross-process acquisition; second call sees the lock held | CONFIRMED |
| function-static byte | `InitializeDriver::has_initialized` | `0x225899e0` (`.bss`) | the entire `InitializeDriver` body (argv build, InitGoogle, telemetry) | CONFIRMED |
| `absl::Mutex` + per-module run-state | `GoogleInitializer::RunInitializers` | inside `0x210b2d20` | re-running any module whose state is already `DONE` | HIGH |
| function-static byte | `RegisterTpuPlatform::tpu_platform_registered` | `0x224c5388` | re-installing the StreamExecutor `TpuPlatform` | CONFIRMED |
| `__cxa_guard` for `InitGoogleDoneNotification` | inside `RealInitGoogle` | guard in `.bss` | re-arming the init-done notification | HIGH |
| init-type selector | `kPjRtCApiTpuInitType` (= 2) | `0x22255b40` (`.data`) | the whole bring-up if it is 0 | CONFIRMED |
| env gate | `TPU_LOAD_LIBRARY` (in `TryAcquireTpuLock`) | str `@ file 0x887356a` | — controls whether the lock is even attempted | CONFIRMED |
| env args | `LIBTPU_INIT_ARGS` (in `GetLibTpuInitArguments`) | str `@ file 0x918c880` | — supplies the flag string | CONFIRMED |

> **NOTE —** `kPjRtCApiTpuInitType` is statically `2` in `.data`; init-type 2 takes the full bring-up (`InitializeDriver(…, init_type_is_2 = true)`), init-type 0 makes `PJRT_Plugin_Initialize` a no-op. Whether init-type `2` vs a hypothetical `1` changes `InitializeDriver`'s behavior was not traced — the `init_type_is_2` argument is computed and passed, but the decompiled `InitializeDriver` body does not visibly branch on it within the traced region (LOW confidence that init-type alters the driver path; the static selector value `2` and the pass-through are CONFIRMED).

---

## Related Components

| Component | Relationship |
|---|---|
| `PJRT_Plugin_Initialize @ 0xe6a9d00` | The PJRT-side gate; calls into this bootstrap (full gate on `module-init-plugin-discovery.md` §3) |
| `tpu::driver::InitializeDriver @ 0x204cecc0` | The bootstrap body this page owns |
| `TfTpu_Initialize @ 0xe6f54a0` | Legacy/alternate 10-byte tail-shim entry into the same `InitializeDriver` |
| `GetLibTpuInitArguments @ 0x20ccca20` | The `LIBTPU_INIT_ARGS` option ingest |
| `RealInitGoogle @ 0x210ae860` | The flag parse + module-DAG run + process bring-up |
| `GoogleInitializer::RunInitializers @ 0x210b2d20` | The DAG run that executes the registered modules (PHASE B) |
| `ExecutorApiFn / OpsApiFn / ProfilerApiFn` | The StreamExecutor C-ABI dispatch tables (Meyers singletons) |
| `RegisterTpuPlatform @ 0xe99a3a0` | Reads the executor table, installs the SE `TpuPlatform` beneath PJRT |

## Cross-References

- [overview.md](overview.md) — the lifecycle section map: from `dlopen` to a usable client
- [module-init-plugin-discovery.md](module-init-plugin-discovery.md) — the wrapper above this page: the discovery handshake, the `dlopen` constructor storm that registers the flags and modules, and the `PJRT_Plugin_Initialize` gate that calls this bootstrap
- [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md) — the `GetPjrtApi` thunk and the lazy 140-slot `PJRT_Api` build that precedes this bootstrap
- [../shim/overview.md](../shim/overview.md) — the per-roster `TfTpu_*Fn` C-API tables: the slot-by-slot contents and the population path this page only probes
- [../pjrt/stream-executor-host-interpreter.md](../pjrt/stream-executor-host-interpreter.md) — the StreamExecutor platform that consumes `ExecutorApiFn` and sits beneath the PJRT client
