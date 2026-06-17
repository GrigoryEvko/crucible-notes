# The System Monitor and Debug Stream

> *All addresses, offsets, and symbols on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, soname `libnrt.so.1`, version namespace `NRT_2.0.0`, NTFF package `KaenaProfilerFormat-2.31.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present, and `.text`/`.rodata` VMA equals file offset, so every `0xb9…`/`0xba…`/`0xbb…` is both an analysis VMA and a file offset. The C surface is first-party C++ from `/opt/workspace/KaenaRuntime/nrt/` — TUs `nrt_sys_trace.cpp`, `nrt_sys_trace_api.cpp`, `nrt_sys_trace_capture.cpp` (the C wrappers), and the system-monitor / status-string slice of `nrt_api.cpp` (GNU C++17 14.2.1, `-O2 -fPIC`). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every C-API function is pinned to a `.symtab` symbol + `.text` address and read from its Hex-Rays decompile; the `system_monitor`(424 B)/`host_stats_t`(312 B) layouts are `structures.json` sizes matched to `operator new(0x1A8)`/`(0x138)`; the `sample_stats` append loop (whose decompile Hex-Rays failed to produce) is recovered byte-level from `disasm/…Z12sample_statsP14system_monitor_0xbb760.asm` and corrects a prior "wrap behavior unknown" note. The C→Rust marshaling shims at `0x509xxx` are owned by [rust-ffi](rust-ffi.md); the ring engine they reach by [rust-capture](rust-capture.md). · Part XIII — Profiling, Trace & Telemetry · [back to index](../index.md)*

## Abstract

This page owns the **C entry surface of the sys_trace producer** and the **host system monitor** — the two pieces of the trace lane that are plain C/C++ and that a runtime consumer (or a reimplementer) actually calls. It is deliberately the *thin* side of producer (2): the `nrt_sys_trace_*` exports here are not the capture engine — they are a band of thin C wrappers (`0xb9800..0xb9ac0`) that log an `API:IN/OUT` debug line and tail-call across the cbindgen FFI bridge into the first-party Rust crate `neuron_rustime::sys_trace`. The two read-out entrypoints — `nrt_sys_trace_fetch_events` @`0xb9880` (drain the rings to a JSON `String`) and `nrt_sys_trace_get_event_types` @`0xb9920` (enumerate the 46 type names) — are the surface a debug/inspection client drives; everything they touch downstream of the `0x509xxx` shim is Rust, owned elsewhere.

The familiar reference frame is a **C ABI veneer over a foreign-language runtime**, the shape of a `cgo`/`PyO3`/`cbindgen` export header: each `extern "C"` function validates the C-side arguments, marshals C pointers/structs into the foreign calling convention, calls one foreign impl, and translates the foreign result back into an `NRT_STATUS`. The crucial reimplementation fact is the **ownership boundary** for the JSON buffer: `fetch_events` returns a Rust-allocated `CString` whose lifetime crosses back into C, and the caller must hand it to `nrt_sys_trace_buffer_free` @`0xb9910` — *not* libc `free` — because it is released by `alloc::ffi::CString::from_raw` + `__rust_dealloc`. Mixing the two allocators corrupts the Rust heap. The `get_event_types`/`free_event_types` pair, by contrast, allocates with libc (`calloc`/`strdup`/`free`) entirely on the C side and never crosses the bridge — two adjacent APIs with two *opposite* free contracts.

The second half of the page is the **host system monitor** (`nrt_system_monitor_*` @`0xba9e0..0xbbf00`): a background `std::thread` that polls `/proc/stat`, `/proc/self/statm`, `/proc/cpuinfo`, and `sysinfo()` on a fixed cadence and appends per-core CPU% and RSS time-series into a 424-byte `system_monitor` singleton. It is armed by the inspect session (activity bit3, `CPU_UTIL`, [inspect-profile-api §1](inspect-profile-api.md)), not by sys_trace, and its samples are the source of the `ntff::host_stats` / `cpu_util.pb` / `host_mem.pb` records the harvest emits. The page closes with the status-string decoders (`nrt_get_status_as_str` / `nrt_get_status_priority`) that turn an `NRT_STATUS` into the human-readable debug stream.

For reimplementation, the contract is:

- **The sys_trace C-API band** — seven `nrt_sys_trace_*` thin wrappers (`0xb9800..0xb9ac0`): which log, which validate, and which is a bare `thunk`; and exactly where each crosses into the `0x509xxx` cbindgen bridge.
- **The two read-out paths** — `fetch_events` (C ptr-check → FFI → JSON buffer, freed Rust-side) and `get_event_types` (pure C `calloc(46)` of `strdup`'d names, freed C-side) — with their *opposite* free contracts.
- **The category classifier** — `get_event_type_category`'s `_bittest64` against the literal `0x70021C0` that splits 46 types into HARDWARE / SOFTWARE.
- **The host sampler** — the `system_monitor`/`host_stats_t` layout, the `run`→`sample_stats` polling loop, the four `/proc` parsers, and the **non-wrapping** ring append (`num_samples >= max_samples` ⇒ stop, *not* overwrite).

| | |
|---|---|
| **C-API band** | `nrt_sys_trace_*` @`0xb9800..0xb9ac0` (TUs `nrt_sys_trace.cpp` / `nrt_sys_trace_api.cpp`) |
| **Arm / disarm** | `nrt_sys_trace_start` @`0xb9800` → `nrt_sys_trace_capture_start` @`0x509740` · `nrt_sys_trace_stop` @`0xb9870` (thunk) → `…_capture_stop` @`0x509980` |
| **Read-out (JSON)** | `nrt_sys_trace_fetch_events` @`0xb9880` → `…_fetch_events_internal` @`0x5099a0` → `api::fetch_events` @`0x5aa3b0` |
| **Free JSON buffer** | `nrt_sys_trace_buffer_free` @`0xb9910` (thunk) → `…_buffer_free_internal` @`0x509650` → `CString::from_raw` + `__rust_dealloc` |
| **Enumerate types** | `nrt_sys_trace_get_event_types` @`0xb9920` — `calloc(46,8)` of `strdup`'d names; `…_free_event_types` @`0xb99f0` |
| **Classify type** | `nrt_sys_trace_get_event_type_category` @`0xb9ac0` — `_bittest64` vs `0x70021C0` |
| **FFI bridge (owner)** | cbindgen shims `0x509650..0x509d08` — **owned by [rust-ffi](rust-ffi.md)**; ring engine by [rust-capture](rust-capture.md) |
| **System monitor** | `system_monitor` (424 B = `0x1A8`); `g_sys_monitor` @`0xc5d318`; sampler `run` @`0xbb850` / `sample_stats` @`0xbb760` |
| **Monitor cadence** | `nrt_system_monitor_start(max_samples)`; inspect arms with `10000`; period `host_stats_t.sample_period_sec` (init `1.0` s) |
| **Status decode** | `nrt_get_status_as_str` @`0xb95c0` (enum → string) · `nrt_get_status_priority` @`0xb9790` (enum → severity) |

---

## 1. The sys_trace C-API Band

### Purpose

The seven `nrt_sys_trace_*` functions in `0xb9800..0xb9ac0` are the public C entry to the host software-span producer. None of them captures, drains, or serializes anything — each is a wrapper that (a) emits a debug-level `API:IN`/`API:OUT` log line, (b) does minimal C-side argument validation, and (c) forwards to one cbindgen shim at `0x509xxx`, which marshals into `neuron_rustime::sys_trace`. The division of labor is exact: **the wrappers own the C ABI and the debug stream; the shims own the marshaling; the Rust crate owns the rings.** A reimplementer rebuilding this layer rebuilds the wrappers and the marshaling contract, and treats the ring engine ([rust-capture](rust-capture.md)) as a black box reached through five entrypoints.

The reference-frame analogy is a generated FFI header: the bodies are mechanical, but two details are *not* generated and must be reproduced — the per-function logging tag (the wrapper's own name string, passed to `nlog_write`) and the buffer-ownership contract (§2). Everything else is forwarding.

### Entry Point

```text
nrt_sys_trace_start (0xb9800)               ── log IN; → capture_start; log OUT
  └─ nrt_sys_trace_capture_start (0x509740)         [rust-ffi]  marshal config, clamp ring size
       └─ neuron_rustime::sys_trace::capture::capture_start (0x5b0590)   [rust-capture]

nrt_sys_trace_stop (0xb9870, thunk)         ── bare tail-call, no log
  └─ nrt_sys_trace_capture_stop (0x509980)          [rust-ffi]
       └─ capture::capture_stop (0x5afd30)           [rust-capture]

nrt_sys_trace_fetch_events (0xb9880)        ── log IN; → fetch_internal; log OUT(written_size)
  └─ nrt_sys_trace_fetch_events_internal (0x5099a0)  [rust-ffi]  FetchOptions; JSON CString
       └─ api::fetch_events (0x5aa3b0)               [rust-serde]

nrt_sys_trace_buffer_free (0xb9910, thunk)  ── bare tail-call
  └─ nrt_sys_trace_buffer_free_internal (0x509650)   [rust-ffi]  CString::from_raw + __rust_dealloc
```

### Algorithm — the wrapper shape

The wrappers come in two flavours: **logged** (`start`, `fetch_events`) and **bare thunk** (`stop`, `buffer_free`). The logged shape is the one to reproduce; the thunk is a one-line tail-call the compiler emits as a jump.

```c
// Models nrt_sys_trace_start @0xb9800 and nrt_sys_trace_fetch_events @0xb9880.
// Tag string = the wrapper's own name; level = NRT_LOG_LEVEL_DEBUG.
NRT_STATUS nrt_sys_trace_start(nrt_sys_trace_config_t *config) {
    nlog_write(MODULE_TAG, "nrt_sys_trace_start", DEBUG, "API:IN: ()");
    NRT_STATUS rc = nrt_sys_trace_capture_start(config);   // 0x509740 — FFI bridge
    nlog_write(MODULE_TAG, "nrt_sys_trace_start", DEBUG, "API:OUT: ()");
    return rc;                                              // status flows straight through
}

NRT_STATUS nrt_sys_trace_fetch_events(char **buffer, size_t *written_size,
                                      const nrt_sys_trace_fetch_options_t *options) {
    nlog_write(MODULE_TAG, "nrt_sys_trace_fetch_events", DEBUG, "API:IN: ()");
    NRT_STATUS rc = nrt_sys_trace_fetch_events_internal(buffer, written_size, options);  // 0x5099a0
    nlog_write(MODULE_TAG, "nrt_sys_trace_fetch_events", DEBUG,
               "API:OUT: (written_size=%zu)", *written_size);   // reads back *written_size
    return rc;
}

// Models nrt_sys_trace_stop @0xb9870 and nrt_sys_trace_buffer_free @0xb9910 — bare thunks.
NRT_STATUS nrt_sys_trace_stop(void)         { return nrt_sys_trace_capture_stop(); }      // 0x509980
void       nrt_sys_trace_buffer_free(char *b){ nrt_sys_trace_buffer_free_internal(b); }   // 0x509650
```

> **GOTCHA —** `nrt_sys_trace_fetch_events` reads `*written_size` in its `API:OUT` log **after** the internal call returns, unconditionally. On the error paths inside `…_fetch_events_internal` (NULL `buffer`/`written_size`, return `NRT_INVALID`=2), `*written_size` may be left untouched, so the logged `written_size=%zu` can print a stale/garbage value when the call failed. A reimplementer mirroring the diagnostics must not treat the logged size as meaningful unless the returned status is `NRT_SUCCESS`. The validation that actually rejects NULL output pointers lives in the *internal* shim (`0x5099a0`, two `log::…::log` sites), not in this wrapper — the wrapper is unconditional.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nrt_sys_trace_start` | `0xb9800` | log IN/OUT; forward config → `capture_start` (FFI) | HIGH |
| `nrt_sys_trace_stop` | `0xb9870` | bare thunk → `capture_stop` (no log) | HIGH |
| `nrt_sys_trace_fetch_events` | `0xb9880` | log IN/OUT(written_size); forward → `fetch_events_internal` | HIGH |
| `nrt_sys_trace_buffer_free` | `0xb9910` | bare thunk → `buffer_free_internal` (Rust dealloc) | HIGH |
| `nrt_sys_trace_get_event_types` | `0xb9920` | pure-C enumerate: `calloc(46)` of `strdup`'d names (§2) | HIGH |
| `nrt_sys_trace_free_event_types` | `0xb99f0` | pure-C free: per-name `free` + array `free` (§2) | HIGH |
| `nrt_sys_trace_get_event_type_category` | `0xb9ac0` | classify type → UNKNOWN/HARDWARE/SOFTWARE via `_bittest64` (§3) | HIGH |

### Considerations

The `config` argument to `nrt_sys_trace_start` is the 312-byte `nrt_sys_trace_config_t` (allocate/setter API and the embedded enable bitmaps owned by [inspect-profile-api §3](inspect-profile-api.md)). This wrapper does **not** validate it — the NULL-tolerance, the `max_events_per_nc` clamp into `[1024, 0x100000]`, and the copy of the two enable bitmaps all happen inside `nrt_sys_trace_capture_start` @`0x509740` (the marshaling shim, [rust-ffi](rust-ffi.md)). Likewise `nrt_sys_trace_stop` is a bare thunk because the idempotency check ("tracing has already been stopped…") is enforced Rust-side. The wrappers are intentionally hollow; the logic is one layer down.

---

## 2. The Two Read-Out Paths and Their Opposite Free Contracts

### Purpose

A debug/inspection client reads the producer two ways, and the two ways have **opposite memory-ownership rules** — the single most error-prone fact on this page. `fetch_events` returns a *Rust-owned* JSON buffer that must be freed by `nrt_sys_trace_buffer_free` (which calls back across the FFI into `CString::from_raw` + `__rust_dealloc`). `get_event_types` returns a *C-owned* array of `strdup`'d names that must be freed by `nrt_sys_trace_free_event_types` (plain libc `free`). The APIs sit four addresses apart and look symmetric; their free contracts are not.

### Algorithm — `get_event_types` (pure C, libc-owned)

`get_event_types` never crosses the FFI bridge. It allocates a `char*[46]` and fills it with `strdup`'d copies of the interned event-type names (the 46-variant taxonomy, owned by [event-taxonomy](event-taxonomy.md)), pulled one at a time from `nrt_interned_string_db_get_event_name(0..45)`. The error policy is all-or-nothing: any single `strdup` failure frees everything already allocated and returns `NRT_RESOURCE`.

```c
// Models nrt_sys_trace_get_event_types @0xb9920.
// out: *event_types = char*[46] of strdup'd names; *count = 46.  ALL libc allocation.
NRT_STATUS nrt_sys_trace_get_event_types(const char ***event_types, size_t *count) {
    if (event_types == NULL) return NRT_INVALID;          // (2)
    if (count == NULL)       return NRT_INVALID;
    *event_types = NULL; *count = 0;                       // clear outputs first

    const char **arr = calloc(0x2E, 8);                    // 46 slots * 8 B
    if (arr == NULL) return NRT_RESOURCE;

    for (uint64_t i = 0; ; ) {
        const char *name = nrt_interned_string_db_get_event_name(i);  // [event-taxonomy]
        arr[i] = strdup(name);                             // libc copy
        if (arr[i] == NULL) {                              // OOM mid-fill
            for (uint64_t j = 0; j < i; j++) free(arr[j]); // unwind every prior strdup
            free(arr);
            return NRT_RESOURCE;
        }
        if (++i == 46) {                                   // dense 0..45, no gaps
            *event_types = arr; *count = 46;
            return NRT_SUCCESS;
        }
    }
}

// Models nrt_sys_trace_free_event_types @0xb99f0 — the libc mirror.
void nrt_sys_trace_free_event_types(const char **types, size_t count) {
    if (types == NULL) return;
    for (size_t i = 0; i < count; i++)                     // per-name free (NULL-tolerant)
        if (types[i]) free(types[i]);
    free(types);                                           // then the array
}
```

### The FFI read-out path — `fetch_events` (Rust-owned buffer)

`fetch_events` is the JSON drain. The wrapper (§1) forwards to `nrt_sys_trace_fetch_events_internal` @`0x5099a0` (the cbindgen shim, owned by [rust-ffi](rust-ffi.md)); that shim is where the C↔Rust dispatch happens, and it is worth tracing the *boundary* even though the marshaling itself is rust-ffi's to own:

```c
// Models the C->Rust dispatch in nrt_sys_trace_fetch_events_internal @0x5099a0.
// (Owned by rust-ffi; shown here only as the bridge this page's wrapper crosses.)
NRT_STATUS fetch_events_internal(char **out_ptr, size_t *out_len, const fetch_options *opt) {
    if (out_ptr == NULL || out_len == NULL)                // two distinct log::log sites
        return NRT_INVALID;                                // (2)

    // Build the Rust api::FetchOptions from the C fetch_options POD:
    //   nc_idx  = opt ? opt->nc_idx : 0                    (fetch_options+8, int32)
    //   valid   = opt ? (opt->nc_idx != -1) : false        (-1 => "all NCs")
    FetchOptions fo = { .nc_idx = (opt ? opt->nc_idx : 0),
                        .valid  = (opt && opt->nc_idx != -1) };

    Result<CString> r = neuron_rustime::sys_trace::api::fetch_events(&fo);  // 0x5aa3b0
    if (r.is_err()) return r.status;                        // inner NRT_STATUS flows out

    CString s = r.ok;                                       // Rust-allocated, NUL-terminated
    // memchr for an interior NUL (rejects a JSON body containing '\0'):
    //   len > 0xF -> memchr_aligned; else byte scan
    *out_ptr = s.into_raw();                                // OWNERSHIP MOVES TO C
    *out_len = s.len - 1;                                   // length WITHOUT the trailing NUL
    return NRT_SUCCESS;
}
```

> **QUIRK —** the buffer `fetch_events` hands back is **Rust-allocated and must be returned to Rust to free.** `nrt_sys_trace_buffer_free` @`0xb9910` is not a libc `free` — it thunks to `…_buffer_free_internal` @`0x509650`, which reconstitutes the buffer with `alloc::ffi::c_str::CString::from_raw` and releases it via `__rust_dealloc`. Calling libc `free` on this pointer (or `nrt_sys_trace_free_event_types` on it) frees a Rust-allocator block with the C allocator and corrupts the heap. The inverse is equally fatal: a `get_event_types` array passed to `nrt_sys_trace_buffer_free` sends a libc block into `__rust_dealloc`. The two free functions are **not interchangeable** despite the producer exposing both. The `*out_len` written by `fetch_events` is `CString.len − 1` — the JSON length **excluding** the trailing NUL — so a reader copying `written_size` bytes gets the document without the terminator.

> **NOTE —** the `fetch_options` POD (16 B: `max_events_per_nc` @`+0`, `nc_idx` @`+8` int32, `-1` = all NCs) and its allocate/set-defaults/setter family (`nrt_sys_trace_fetch_options_*` @`0xb9a40..0xb9ab0`) are the small C bag a caller fills before `fetch_events`. They are pure-C POD setters (NULL-guarded `calloc(1,0x10)` + field stores); the bridge reads only `nc_idx` and derives the `valid` flag from `nc_idx != -1`. The marshaling into the Rust `FetchOptions` is owned by [rust-ffi](rust-ffi.md).

---

## 3. The Event-Type Category Classifier

### Purpose

`nrt_sys_trace_get_event_type_category` @`0xb9ac0` maps a `nrt_sys_trace_event_type` (0..45) to one of `{UNKNOWN, HARDWARE, SOFTWARE}`. It is the only place the 46-variant taxonomy is partitioned into a hardware/software dichotomy, and it does so with a single 64-bit bit-test against a literal mask rather than a table — a compact encoding a reimplementer must reproduce exactly to match the classification.

### Algorithm

The classifier has two ranges. For types `0..5` and `> 26` the answer is a constant (`SOFTWARE`), with `> 45` rejected as invalid. For the *middle* range `6..26` the answer is `2 − bit(mask, type)`: the literal mask `0x70021C0` (decimal `117449152`) has a 1-bit set for exactly the seven hardware types, so `bit==1` ⇒ `2−1=1` (`HARDWARE`) and `bit==0` ⇒ `2−0=2` (`SOFTWARE`).

```c
// Models nrt_sys_trace_get_event_type_category @0xb9ac0.
// category enum: 0 UNKNOWN, 1 HARDWARE, 2 SOFTWARE, 3 COUNT.
nrt_sys_trace_event_type_category_t get_event_type_category(nrt_sys_trace_event_type_t t) {
    if ((uint32_t)t > NC_MODEL_SWITCH /* 26 */) {
        if ((uint32_t)(t - 46) <= 0x7FFFFFD1)              // t > 45 (and not the 27..45 band)
            { nlog("Could not get category for invalid event type %d", t); return UNKNOWN; }
        return SOFTWARE;                                   // types 27..45: all software
    }
    if ((uint32_t)t <= CC_LOAD /* 5 */)
        return SOFTWARE;                                   // types 0..5: all software
    // middle band 6..26: bit-test the hardware mask
    uint64_t mask = 0x70021C0;                             // bits {6,7,8,13,24,25,26}
    return (nrt_sys_trace_event_type_category_t)(2 - _bittest64(&mask, t));
    //  bit set  -> 2-1 = HARDWARE(1)
    //  bit clear-> 2-0 = SOFTWARE(2)
}
```

The seven hardware types decoded from `0x70021C0`:

| Bit | Type | Name |
|---|---|---|
| 6 | 6 | `CC_EXEC_BARRIER` |
| 7 | 7 | `DEVICE_EXEC` |
| 8 | 8 | `CC_EXEC` |
| 13 | 13 | `NUM_ERR` |
| 24 | 24 | `HW_NOTIFY` |
| 25 | 25 | `TIMESTAMP_SYNC` |
| 26 | 26 | `NC_MODEL_SWITCH` |

> **GOTCHA —** the mask is only consulted for the middle band `6..26`; the constant-`SOFTWARE` arms for `0..5` and `27..45` are *not* in the mask, so a reimplementer who tries to drive the whole 0..45 range off a single 46-bit mask must extend `0x70021C0` to cover those constant arms (they happen to be all-software, i.e. all-zero, so the mask value is unchanged — but the *range check* matters: `t > 45` must reject, and the decompiler's `(t-46) <= 0x7FFFFFD1` is the unsigned-wrap idiom for "t outside `[27,45]` and `> 45`"). The bit-test is `_bittest64`, so the mask must be a single `uint64_t` even though only bits 6..26 are live. The seven hardware types and their `(*)`-marked taxonomy entries are cross-checked in [event-taxonomy §2](event-taxonomy.md).

---

## 4. The Host System Monitor

### Purpose

The system monitor is a self-contained host telemetry sampler: a background `std::thread` that wakes on a fixed cadence, reads four kernel sources, and appends a per-core CPU% row plus an RSS row into a time-series held in a 424-byte `system_monitor` singleton. It is **independent of the sys_trace ring** — different data structure, different lifecycle, different arming — and exists to feed the `ntff::host_stats` / `cpu_util.pb` / `host_mem.pb` records the inspect harvest emits. It is armed not by `nrt_sys_trace_start` but by the inspect session's activity bit3 (`CPU_UTIL`), which calls `nrt_system_monitor_start(10000)` ([inspect-profile-api §1](inspect-profile-api.md)).

The reference-frame analogy is a `sar`/`pidstat` collector embedded in-process: a periodic `/proc` scrape that diffs jiffy counters against the previous sample to compute utilization. The divergence is that the result is not printed — it is buffered into a fixed-depth ring for later protobuf serialization, and the ring **does not wrap** (§ the append below).

### Layout

```text
system_monitor   size 424 (0x1A8)   operator new(0x1A8)   g_sys_monitor @0xc5d318
  +0x000  312  host_stats_t       stats           ── live, written by the sample thread
  +0x138  8    std::thread        monitor_thread  ── _M_id (0 when not running)
  +0x140  1    std::atomic<bool>  running         ── run-loop guard; stop() sets 0
  +0x148  96   cpu_state          cpu_state_prev  ── 4 vectors: prev /proc/stat snapshot

host_stats_t   size 312 (0x138)   operator new(0x138)
  +0x000  4    uint32             num_cpus
  +0x004  256  char[256]          cpu_name              ── /proc/cpuinfo "model name" (strncpy 255 + NUL)
  +0x108  8    uint64             mem_capacity          ── sysinfo().totalram * mem_unit
  +0x110  4    float              sample_period_sec     ── init 1.0; run() sleeps 1000*this ms
  +0x118  8    size_t             num_samples           ── append cursor (advances; never wraps)
  +0x120  8    size_t             max_samples           ── ring depth == start() arg
  +0x128  8    cpu_util_t **      cpu_util_measurements ── num_cpus rows x max_samples
  +0x130  8    host_mem_usage_t * mem_usage_measurements── max_samples rows

cpu_util_t (16)  { +0 float util; +8 uint64 timestamp }
host_mem_usage_t (16) { +0 int64 usage(RSS bytes); +8 uint64 timestamp }
cpu_state (96)   { 4 x std::vector<uint64_t> user/nice/system/idle (24 B each) }
```

The 424-byte total and the field offsets are byte-verified: `nrt_system_monitor_start` @`0xbbf00` does `operator new(0x1A8)`, sets the thread word `+312`, and allocates the four `cpu_state_prev` vectors at `+328/+352/+376/+400` (each `8 * num_cores` bytes), matching `+0x148` + 4×24-byte vector triples. `[HIGH]`

### Entry Point

```text
nrt_inspect_begin_with_options (0x99050)  [act & CPU_UTIL]
  └─ nrt_system_monitor_start (0xbbf00, arg = max_samples; inspect passes 10000)
       ├─ get_num_cores (0xbaca0)              ── std::thread::hardware_concurrency
       ├─ operator new(0x1A8) -> g_sys_monitor ── 424 B singleton
       ├─ 4x operator new(8*num_cores)         ── cpu_state_prev vectors
       ├─ alloc_host_stats(live)  (0xbbdd0)    ── num_cpus, cpu_name, mem_capacity, rows
       ├─ alloc_host_stats(read_copy)          ── reader snapshot target
       ├─ running := 1
       └─ std::thread::_M_start_thread(run)    ── 0xbb850 sampler body

nrt_inspect_stop (0x9f130)
  ├─ nrt_system_monitor_stop (0xba9e0)         ── running := 0; join
  └─ nrt_system_monitor_get_stats (0xbac60)    ── copy_host_stats(live -> read_copy) snapshot
```

### Algorithm — the sampler loop and the non-wrapping append

`run` @`0xbb850` is the thread body: while `running`, call `sample_stats`, then `nanosleep` for `1000 * sample_period_sec` ms (EINTR-retried). `sample_stats` @`0xbb760` is one tick — and its append cursor is the subtle part. The Hex-Rays decompile of `sample_stats` **failed** (`idaapi.decompile returned no cfunc`); the logic below is recovered byte-level from `disasm/…Z12sample_statsP14system_monitor_0xbb760.asm`.

```c
// Models run @0xbb850.
void run(system_monitor *m) {
    while (m->running) {
        sample_stats(m);                                   // 0xbb760
        float ms = 1000.0f * m->stats.sample_period_sec;   // +0x110; default 1000 ms
        if ((int)ms > 0) {
            struct timespec t = { ms/1000, 1000000*(ms%1000) };
            while (nanosleep(&t,&t) == -1)
                if (errno != EINTR /*4*/) {                // only EINTR retries; else abandon sleep
                    if (m->running) break_to_resample;     // (goto LABEL_2)
                    return;
                }
        }
    }
}

// Models sample_stats @0xbb760 — recovered from disasm (decompile failed).
void sample_stats(system_monitor *m) {
    host_stats_t *s = &m->stats;
    if (s->num_samples >= s->max_samples)                  // +0x118 vs +0x120
        return;                                            // RING IS FULL -> DROP, do NOT wrap

    cpu_state cur;                                         // [rsp] local
    get_cpu_util_per_core(cur /*, prev=&m->cpu_state_prev @+0x148*/);  // 0xbace0 (/proc/stat diff)
    uint64_t ts = time_utils_current_timestamp_ns();       // 0x5cad40 — ONE timestamp for both rows
    size_t idx16 = s->num_samples << 4;                    // *16 = cpu_util_t / host_mem_usage_t stride

    for (uint32_t c = 0; c < s->num_cpus; c++) {           // per-core cpu_util row
        cpu_util_t *row = s->cpu_util_measurements[c] + idx16;  // (+0x128)[c] + idx16
        row->util      = cur.util[c];                      // movss from get_cpu_util output
        row->timestamp = ts;                               // [row+8] = ts
    }
    host_mem_usage_t *mrow = (void*)s->mem_usage_measurements + idx16;  // +0x130 + idx16
    mrow->usage     = get_mem_usage();                     // 0xbb560 (/proc/self/statm field0 * pagesize)
    mrow->timestamp = ts;                                  // same ts as the cpu rows

    s->num_samples += 1;                                   // +0x118 advance; saturating at max_samples
    // (cur cpu_state vector freed via operator delete on exit)
}
```

> **CORRECTION (SYSMON-01) —** the prior consolidated note marked `sample_stats`'s append as "wrap behavior vs max_samples INFERRED, DECOMPILE FAILED" and left it a Phase-2 gap. The byte-level disassembly resolves it: the function's *first* act is `cmp [rdi+0x118], [rdi+0x120]; jb` — i.e. it appends **only while `num_samples < max_samples`** and returns immediately once full. The ring **does not wrap or overwrite**; it saturates. A reimplementer who builds a circular buffer here will diverge — the monitor silently stops sampling after `max_samples` ticks (with the inspect arg `10000` and the default 1 s period, ~2.7 hours of samples before saturation). `[HIGH — disasm-anchored]`

> **QUIRK —** one `time_utils_current_timestamp_ns()` call stamps **both** the per-core `cpu_util_t.timestamp` and the `host_mem_usage_t.timestamp` for a tick (the same `r13` register, @`0xbb79d`). The two series are therefore co-timestamped by construction — a consumer can join a CPU sample to its memory sample by equal timestamp without interpolation. A reimplementation that stamps the two reads independently breaks that invariant.

### The four `/proc` parsers

| Source | Function | Address | Reads | Confidence |
|---|---|---|---|---|
| `/proc/stat` | `get_cpu_util_per_core` | `0xbace0` | per-core `user/nice/system/idle` jiffies; diffs vs `cpu_state_prev`, writes `% busy` | HIGH |
| `/proc/self/statm` | `get_mem_usage` | `0xbb560` | field 0 (resident pages) × `sysconf(_SC_PAGESIZE)` = RSS bytes | HIGH |
| `/proc/cpuinfo` | `get_cpu_name` | `0xbb900` | first `"model name"` value → `std::string` (cxx11) | HIGH |
| `sysinfo()` | `get_mem_capacity` | `0xbacb0` | `totalram * mem_unit` (bytes), or 0 | HIGH |

`get_cpu_util_per_core` reads the *previous* jiffy snapshot from `cpu_state_prev` (`system_monitor+0x148`, four `vector<uint64_t>`), diffs against the fresh `/proc/stat` read to compute `% busy = (Δbusy)/(Δtotal)` per core, then updates the prev snapshot in place. `get_mem_usage` confirms the RSS formula `field0 * sysconf(30)` (`_SC_PAGESIZE`) byte-for-byte. `[HIGH]`

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nrt_system_monitor_start` | `0xbbf00` | `new(0x1A8)`; 4 prev vectors; `alloc_host_stats` ×2; spawn `run` | HIGH |
| `nrt_system_monitor_stop` | `0xba9e0` | `running := 0`; `join` the sample thread | HIGH |
| `nrt_system_monitor_get_stats` | `0xbac60` | no monitor → `&host_stats_default`; else `copy_host_stats` → snapshot | HIGH |
| `nrt_system_monitor_clear_data` | `0xbaa20` | free all `host_stats_t` arrays + `cpu_state_prev`; `delete` singleton | HIGH |
| `run` | `0xbb850` | thread body: `sample_stats` then `nanosleep(period)`, EINTR-retried | HIGH |
| `sample_stats` | `0xbb760` | one tick: cpu/mem read, co-timestamp, **non-wrapping** append | HIGH (disasm) |
| `get_cpu_util_per_core` | `0xbace0` | `/proc/stat` per-core diff vs prev → `% busy` | HIGH |
| `get_mem_usage` | `0xbb560` | `/proc/self/statm` field0 × pagesize | HIGH |
| `get_cpu_name` | `0xbb900` | `/proc/cpuinfo` first `model name` | HIGH |
| `get_mem_capacity` | `0xbacb0` | `sysinfo().totalram * mem_unit` | HIGH |
| `get_num_cores` | `0xbaca0` | `std::thread::hardware_concurrency` | HIGH |
| `alloc_host_stats` | `0xbbdd0` | size fields; `new[]` cpu_util + mem rows; `strncpy` cpu_name(255) | HIGH |
| `copy_host_stats` | `0xbab40` | deep-copy scalars + per-cpu series (num_samples rows) | HIGH |

### Considerations

The monitor keeps **two** `host_stats_t`: the *live* one the sample thread writes, and a *read_copy* snapshot. `nrt_system_monitor_get_stats` @`0xbac60` returns the read_copy (via `copy_host_stats`) so a reader never sees a half-written row mid-tick — a snapshot-on-read discipline, not a lock. The inspect harvest takes that snapshot at `nrt_inspect_stop` and serializes it into `ntff::host_stats` (a *separate* protobuf POD from the in-memory `host_stats_t` — same fields, different layout; do not conflate them). `nrt_system_monitor_clear_data` @`0xbaa20` is the teardown that frees every measurement array and deletes the 424-byte singleton; it has no recorded external caller and is reached only on the inspect-close path. The entire family is internal-linkage (`_ZL` / no dynamic export) — it is *not* a public `nrt_*` API; clients reach it only by arming the inspect activity bit.

---

## 5. The Status Debug Stream

### Purpose

The runtime's debug stream renders an `NRT_STATUS` two ways: a stable string name (for logs and core dumps) and a severity bucket (for routing/error-priority). Both are pure leaf switches with no allocation — the cheapest possible decoders, called from the error paths (`nrt_core_dump`, `nrt_infodump`, `tpb_xu_base_report_complete`, the exec error path).

### Algorithm

`nrt_get_status_as_str` @`0xb95c0` is a dense `switch` recovering the full extended `NRT_STATUS` enum (`0..15`, `101`, `1002..1006`, `1100`, `1200..1206`) to its verbatim name, defaulting reserved/unknown codes to `"UNKNOWN"`. `nrt_get_status_priority` @`0xb9790` maps the same enum to `nrt_status_priority_t` `{NONE=0, MEDIUM=1, HIGH=2, CRITICAL=3}` via a compact comparison cascade:

```c
// Models nrt_get_status_priority @0xb9790.
nrt_status_priority_t nrt_get_status_priority(NRT_STATUS s) {
    if (s == NRT_EXEC_COMPLETED_WITH_ERR /*1004*/)  return CRITICAL;  // (3)
    if (s >  NRT_EXEC_COMPLETED_WITH_ERR) {
        if (s == NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE /*1205*/) return CRITICAL;
        if (s >  NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE)
            return (s == NRT_NETWORK_PROXY_FAILURE /*1206*/) + 1;     // 1206->MEDIUM(1), else not >1205
        if (s == NRT_EXEC_SW_NQ_OVERFLOW /*1204*/) return HIGH;       // (2)
        return (s < NRT_EXEC_HW_ERR_COLLECTIVES) ? MEDIUM : CRITICAL; // 1200..1202 vs 1203+
    }
    if (s == 0 /*NRT_SUCCESS*/) return NONE;                          // (0)
    return ((unsigned)(s - 5) < 2) + 1;  // TIMEOUT(5)/HW_ERROR(6) -> MEDIUM, else MEDIUM baseline
}
```

| Status | Value | Priority | Source |
|---|---|---|---|
| `NRT_SUCCESS` | 0 | `NONE` (0) | `s == 0` arm |
| `NRT_TIMEOUT` / `NRT_HW_ERROR` | 5 / 6 | `MEDIUM` (1) | `(s-5) < 2` |
| `NRT_EXEC_COMPLETED_WITH_ERR` | 1004 | `CRITICAL` (3) | explicit |
| `NRT_EXEC_SW_NQ_OVERFLOW` | 1204 | `HIGH` (2) | explicit |
| `NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE` | 1205 | `CRITICAL` (3) | explicit |
| `NRT_NETWORK_PROXY_FAILURE` | 1206 | `MEDIUM` (1) | `(s==1206)+1` |
| `NRT_EXEC_HW_ERR_*` (1203+) | ≥1203 | `CRITICAL` (3) | `>= COLLECTIVES` arm |
| most others | — | `MEDIUM` (1) | default `+1` |

> **NOTE —** both decoders are internal-linkage (not in the dynamic export table) and stateless; the priority map's literals (`1`/`2`/`3`) are read directly from the decompile and the enum names attached from `enums.json`. The full string table (`"NRT_SUCCESS"` … `"NRT_NETWORK_PROXY_FAILURE"`, default `"UNKNOWN"` @`0x83edbd`) is the `nrt_get_status_as_str` switch; it is a verbatim catalogue of the extended status enum and is not reproduced here in full because it is a direct 1:1 enum→name map with no logic to reimplement beyond the switch itself.

---

## Related Components

| Name | Relationship |
|---|---|
| `nrt_sys_trace_capture_*` @`0x509650..0x509980` | the cbindgen marshaling shims this band's wrappers tail-call ([rust-ffi](rust-ffi.md)) |
| `capture::capture_start` @`0x5b0590` | the ring engine `nrt_sys_trace_start` ultimately arms ([rust-capture](rust-capture.md)) |
| `api::fetch_events` @`0x5aa3b0` | the serde JSON drain `fetch_events` reaches; returns the Rust `CString` ([rust-serde](rust-serde.md)) |
| `nrt_interned_string_db_get_event_name` | the 0..45 name source `get_event_types` `strdup`s ([event-taxonomy](event-taxonomy.md)) |
| `nrt_sys_trace_config_t` (312 B) | the config `nrt_sys_trace_start` forwards; setters owned by [inspect-profile-api §3](inspect-profile-api.md) |
| `ntff::host_stats` | the protobuf POD the monitor's snapshot serializes into at `nrt_inspect_stop` ([inspect-profile-api §5](inspect-profile-api.md)) |

## Cross-References

- [sys_trace Capture Engine](rust-capture.md) — the Rust lock-free ring the `nrt_sys_trace_*` wrappers arm and drain; the engine this page deliberately does not re-derive
- [SysTraceEventType Taxonomy (46 Variants)](event-taxonomy.md) — the 46 type names `get_event_types` enumerates and the `(*)`-marked hardware types `get_event_type_category` classifies
- [The Inspect / Profile API](inspect-profile-api.md) — the umbrella that arms `nrt_sys_trace_start` (bit0) and `nrt_system_monitor_start` (bit3), and harvests the monitor snapshot into `ntff::host_stats`
- [neuron_rustime: the cbindgen FFI Boundary](rust-ffi.md) — the `0x509xxx` shims that own the C↔Rust marshaling these wrappers cross, and the buffer-ownership `from_raw`/`__rust_dealloc` contract
- [Overview: the Three Trace Producers](overview.md) — where producer (2)'s C surface sits among the three trace engines
- [back to index](../index.md)
