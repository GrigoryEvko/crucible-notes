# Profiler Factory Registry and Collector Pipeline

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

The PJRT Profiler extension is only a thin C-ABI veneer. The machine that actually does the work sits one layer down: a process-global **factory registry**, a **`tsl::profiler::ProfilerCollection`** that fans a single lifecycle call out to every assembled collector, a set of **concrete `ProfilerInterface` implementations** that drain host and device trace state into a shared `XSpace`, and a **`TpuProfilerControlListener`** singleton that gates which TPU chips actually participate per run. This page owns that machine. The extension struct, the 8-slot `PLUGIN_Profiler_Api` vtable, and the `PLUGIN_Profiler` handle live on [`../pjrt/ext-profiler.md`](../pjrt/ext-profiler.md) — here we pick up at the `tsl::profiler::CreateProfilers(opts)` call that `PLUGIN_Profiler_Create` makes and follow the dataflow to the wire.

The shape is the standard TSL/xprof profiler architecture, the same one TensorFlow ships. A collector registers a factory `std::function<unique_ptr<ProfilerInterface>(const ProfileOptions&)>` via `RegisterProfilerFactory` @ `0x1CF50780` into one global vector guarded by an `absl::Mutex`. `CreateProfilers` @ `0x1CF50860` walks that vector under the same mutex, invokes every factory, and wraps each non-null result in a **`ProfilerController`** (an order/crash-isolation guard) before collecting it into the returned `vector<unique_ptr<ProfilerInterface>>`. `ProfilerCollection` takes that vector and exposes the same three-method `ProfilerInterface` surface — `Start`/`Stop`/`CollectData` — by iterating its inner vector and calling the matching vtable slot on each controller. The collection *is itself* a `ProfilerInterface`, so the design is a composite: a profiler-of-profilers.

The concrete collectors are heterogeneous. `xprof::tpu::TpuProfilerImpl::CollectData` @ `0xEF34860` is the device drain: it allocates an `XprofResponse` proto on the XSpace arena, pulls per-chip trace data over an inner collector, and folds the result into device `XPlane`s via `ConvertResponseToTpuXSpace`. `tsl::profiler::ThreadpoolProfilerInterface::CollectData` @ `0xF3326C0` is a near-trivial host collector that only appends a diagnostic string when its session ended non-OK. `HostTracer` captures host `TraceMe` events. None of these is reached per trace event — they run only at `CollectData` time, draining state accumulated by lock-free `TraceMe` macros and hardware ring buffers. Orthogonal to the collector path, the `TpuProfilerControlListener` (`GetOrCreateTpuProfilerControlListener` @ `0xF332800`) lets each TPU chip driver ask `CanStartProfiler`/`MustStopProfiler` before opening or after draining its trace buffer, so profiling can run concurrently with execution while the listener decides chip-by-chip participation.

For reimplementation, the contract is:

- The **registry**: one lazily-constructed `vector<std::function<...>>` (`GetFactories()::factories`), mutated by `RegisterProfilerFactory` under `mu`, never cleared.
- The **factory walk**: `CreateProfilers` holds `mu` across the *entire* walk, invokes each factory, drops null results, wraps the rest in `ProfilerController`.
- The **`ProfilerController` FSM**: a phase counter and a sticky last-status that enforce Start→Stop→CollectData ordering and short-circuit after any error.
- The **`ProfilerCollection` fan-out**: 32-byte object, iterate inner vector, call vtable `+0x10/+0x18/+0x20`; first-non-trivial-status-wins merge; destructive vector clear inside `CollectData`.
- The **collector semantics**: device drain (`TpuProfilerImpl`) vs. host append (`ThreadpoolProfilerInterface`, `HostTracer`).
- The **run-gate**: `TpuProfilerControlListener` delegating to `xprof::tpu::Profiler::Register/UnregisterChipProfiler`.

| | |
|---|---|
| **Registry mutator** | `tsl::profiler::RegisterProfilerFactory` @ `0x1CF50780` |
| **Registry storage** | `GetFactories()::factories` @ `0x2257C830` — `vector<std::function<...>>`, lazily `new`-ed |
| **Registry mutex** | `tsl::profiler::(anonymous)::mu` @ `0x2257C828` (`absl::Mutex`) |
| **Factory walk** | `tsl::profiler::CreateProfilers` @ `0x1CF50860` |
| **Crash/order guard** | `tsl::profiler::ProfilerController` ctor @ `0x1CF50CE0`, 32 bytes |
| **Composite** | `tsl::profiler::ProfilerCollection`, 32 bytes, vtable @ `0x217738A0` |
| **Fan-out** | `Start` @ `0xF6A1640`, `Stop` @ `0xF6A16C0`, `CollectData` @ `0xF6A1740` |
| **Device collector** | `xprof::tpu::TpuProfilerImpl::CollectData` @ `0xEF34860` |
| **Host collectors** | `ThreadpoolProfilerInterface::CollectData` @ `0xF3326C0`; `HostTracer` (factory @ `0xF32F7C0`) |
| **Run-gate singleton** | `GetOrCreateTpuProfilerControlListener` @ `0xF332800`, 16 bytes, vtable symbol @ `0x2175C1A0` (installed vptr `0x2175C1B0`) |
| **Output** | one `tensorflow.profiler.XSpace` proto, host + per-device planes |

---

## The Factory Registry

### Purpose

A profiler is plugged in at link time, not at call time. Each backend — the host tracer, the TPU device tracer, the threadpool tracer, the megascale RPC tracer — contributes one factory function during C++ static initialization. The registry is the single global list those factories land in, and `CreateProfilers` is the only reader. This is what makes the same backend reachable from both the modern PJRT extension and the legacy [`TpuProfiler_*` C-ABI](tpu-profiler-abi.md): both call `CreateProfilers`, which walks the one shared list.

### Entry Point

```text
<static init>  _GLOBAL__sub_I_*profiler*.cc
  └─ RegisterProfilerFactory(std::function<...>)   ── 0x1CF50780  (under mu)
        └─ GetFactories()::factories                ── 0x2257C830  (lazy `new(0x18)`)

PLUGIN_Profiler_Create / TpuProfiler_Create
  └─ CreateProfilers(opts)                          ── 0x1CF50860  (under mu)
```

### Algorithm

`RegisterProfilerFactory` is a guarded append. The `factories` vector is a 24-byte `{begin, end, cap_end}` header, allocated on first use under the same mutex the reader uses, then move-emplaced with each incoming `std::function` (32-byte slot).

```c
function RegisterProfilerFactory(fn /* std::function, 32 bytes */):   // 0x1CF50780
    mu.Lock();                                          // 0x2257C828
    if (!guard(factories)):                             // one-time init
        factories = operator new(0x18);                 // {0,0,0} vector header
        zero(factories);
    if (factories.size == factories.cap):               // grow
        emplace_back_slow_path(factories, fn);
    else:                                               // in-place move
        slot = factories.begin + 32 * factories.size;
        move_construct(slot, fn);                       // steals fn's callable; clears fn
        factories.size += 1;
    mu.Unlock();
    // No return value. The vector is never shrunk or cleared for the process lifetime.
```

> **NOTE —** the registry has no deregistration path and no clear. A factory registered during static init lives until the process exits. `CreateProfilers` is therefore idempotent across handles: every `PLUGIN_Profiler_Create` (or `TpuProfiler_Create`) sees the identical factory set.

### Function Map

| Function | Addr | Role |
|---|---|---|
| `RegisterProfilerFactory` | `0x1CF50780` | append one factory under `mu` |
| `GetFactories()::factories` | `0x2257C830` | the global `vector<std::function<...>>` |
| `(anonymous)::mu` | `0x2257C828` | `absl::Mutex` guarding the vector |

---

## The Factory Walk — `CreateProfilers`

### Purpose

`CreateProfilers` is the constructor of the per-session collector set. It is called once per `PLUGIN_Profiler_Create`, takes the parsed `tensorflow::ProfileOptions`, and returns a `vector<unique_ptr<ProfilerInterface>>` — one `ProfilerController`-wrapped collector per factory that opted in. The `ProfileOptions` is *not* inspected here; it is passed verbatim to each factory, which decides for itself whether to participate (a factory returns `NULL` to opt out, e.g. a device tracer on a CPU-only `device_type`).

### Entry Point

```text
PLUGIN_Profiler_Create  (0xE6F0C60, see ../pjrt/ext-profiler.md)
  └─ CreateProfilers(opts)                       ── 0x1CF50860
        ├─ mu.Lock()                              ── held across the whole walk
        ├─ for each factory slot:
        │     ├─ result = factory(opts)           ── indirect call, slot+16
        │     └─ if result: ProfilerController(new(0x20), result)   ── 0x1CF50CE0
        └─ mu.Unlock()
  └─ new ProfilerCollection(move(result))          ── 0xF6A15E0
```

### Algorithm

```c
function CreateProfilers(out /* vector<unique_ptr<ProfilerInterface>> */, opts):  // 0x1CF50860
    out = {begin:0, size:0, cap:0};
    mu.Lock();                                          // 0x2257C828
    lazy_init(factories);                               // same one-time path as the mutator
    for slot in factories:                              // 32 bytes per std::function
        raw = slot.invoke(opts);                        // *(slot+16)(slot, &opts) — the factory call
        if (raw != NULL):                               // factory opted in
            ctrl = operator new(0x20);                  // ProfilerController, 32 bytes
            ProfilerController(ctrl, raw);              // 0x1CF50CE0 — takes ownership of raw
            push_back(out, ctrl);                       // grows out (2x) as needed
    mu.Unlock();
    return out;
```

> **NOTE —** `mu` is **held across the entire walk** at `0x1CF50860`, including the indirect `factory(opts)` calls — `mu.Unlock()` is the last statement before the return. A factory body therefore runs with `mu` held and **must not** call `RegisterProfilerFactory` or `CreateProfilers` re-entrantly (`mu` is a non-recursive `absl::Mutex`); doing so self-deadlocks. Distinct PJRT handles share no mutable state beyond this serialized walk.

> **QUIRK —** a factory returning `NULL` is silently dropped, not an error. Combined with the no-inspection-here rule, a `ProfileOptions` whose `device_type` no factory matches yields an *empty* `ProfilerCollection`. Every subsequent `Start`/`Stop`/`CollectData` then succeeds against zero collectors and produces an empty `XSpace`. A reimplementation must treat empty-collection success as valid, not a misconfiguration to reject.

### Function Map

| Function | Addr | Role |
|---|---|---|
| `CreateProfilers` | `0x1CF50860` | walk registry, wrap, collect |
| `ProfilerController::ProfilerController(unique_ptr)` | `0x1CF50CE0` | wrap one collector for isolation |
| `ProfilerCollection::ProfilerCollection(vector)` | `0xF6A15E0` | take the result vector inline |

---

## The `ProfilerController` Isolation Guard

### Purpose

Every collector returned by a factory is wrapped in a `ProfilerController` before it enters the collection. The controller is a finite-state guard around an inner `ProfilerInterface`: it enforces the legal call order (Start → Stop → CollectData), caches the last status so a failed phase short-circuits all later phases, and logs an `absl::Status` on any violation. This is the crash-isolation layer — a collector that errors or is called out of order cannot poison the rest of the collection or the lifecycle state machine on the PJRT handle.

### Layout

```c
/* tsl::profiler::ProfilerController — 32 bytes (operator new(0x20)). */
struct ProfilerController {
    /* +0x00 */ void**         vtable;       /* ProfilerInterface vtable                       */
    /* +0x08 */ uint32_t       phase;        /* FSM: 0=created, 1=started, 2=stopped, 3=collected */
    /* +0x0C */ uint32_t       _pad;
    /* +0x10 */ ProfilerInterface* inner;    /* the wrapped collector; owned                   */
    /* +0x18 */ absl::Status   last_status;  /* sticky; 1 == inline OkStatus                   */
};
```

### Algorithm

The `Start` and `CollectData` bodies (`0x1CF50DE0`, `0x1CF51060`) share one shape: check the phase, advance it, verify the last status was OK, then call the inner collector's matching vtable slot and cache the new status. `Stop` (`0x1CF50F20`) follows the same pattern at phase 1→2.

```c
function ProfilerController::Start(this):                 // 0x1CF50DE0
    if (this.phase != 0):                                 // wrong order
        return Log(MakeErrorImpl<10 ABORTED>("Start called in the wrong order"));   // profiler_controller.cc:51
    this.phase = 1;
    if (this.last_status != OK):                          // a prior phase already failed
        return Log("Previous call returned an error.");
    s = this.inner->Start();                              // vtable +0x10
    this.last_status = s;                                 // cache (ref-counted)
    if (s != OK): Log(s);
    return s;

function ProfilerController::CollectData(this, xspace):    // 0x1CF51060
    if (this.phase != 2):                                 // must follow a Stop
        return Log("CollectData called in the wrong order.");
    this.phase = 3;
    if (this.last_status != OK):
        return Log("Previous call returned an error.");
    s = this.inner->CollectData(xspace);                  // vtable +0x20
    this.last_status = s;
    if (s != OK): Log(s);
    return s;
```

> **GOTCHA —** the controller, not `ProfilerCollection`, is what makes order violations *survivable*. The PJRT handle's `ready` byte (see [`../pjrt/ext-profiler.md`](../pjrt/ext-profiler.md)) is a coarse, overloaded gate that does not detect, e.g., a `CollectData` before `Stop`. The controller catches that here, logs to `profiler_controller.cc:84`, and returns an error status *without* invoking the inner collector — so a misordered call cannot drive a half-initialized device tracer. A reimplementation that omits the controller and dispatches directly to collectors will surface those bugs as crashes instead of logged statuses.

### Function Map

| Function | Addr | Role |
|---|---|---|
| `ProfilerController::Start` | `0x1CF50DE0` | phase 0→1, guarded inner `Start` |
| `ProfilerController::Stop` | `0x1CF50F20` | phase 1→2, guarded inner `Stop` |
| `ProfilerController::CollectData` | `0x1CF51060` | phase 2→3, guarded inner `CollectData` |
| `ProfilerController::~ProfilerController (D2/D0)` | `0x1CF50D20` / `0x1CF50DA0` | drop inner, unref status |

---

## The `ProfilerCollection` Fan-Out

### Purpose

`ProfilerCollection` is the composite: it implements `ProfilerInterface` by forwarding each call to every member. The PJRT handle owns exactly one of these at `+0x70`; the five PJRT lifecycle methods marshal into its three real methods. Because it is itself a `ProfilerInterface`, the design nests cleanly — though in practice the collection holds `ProfilerController`s, not nested collections.

### Layout

```c
/* tsl::profiler::ProfilerCollection — 32 bytes. vtable @ 0x217738A0. */
struct ProfilerCollection {
    /* +0x00 */ void**              vtable;   /* {top, RTTI, D2, D0, Start, Stop, CollectData} */
    /* +0x08 */ ProfilerController** begin;   /* inner vector data                              */
    /* +0x10 */ size_t              size;     /* element count                                  */
    /* +0x18 */ size_t              capacity;
};
```

The vtable at `0x217738A0` is the source of the offsets the [PJRT layer](../pjrt/ext-profiler.md) calls: `Start` at `+0x10` (`0xF6A1640`), `Stop` at `+0x18` (`0xF6A16C0`), `CollectData` at `+0x20` (`0xF6A1740`), and the destroying dtors at `+0x08` (`0xF6A1840` D2 / `0xF6A18E0` D0).

### Algorithm

All three fan-out methods share a status-merge idiom: walk the inner vector in order, call the member's matching vtable slot, and keep the **first** non-trivial status while `Unref`-ing every later non-inline status. `CollectData` adds a destructive clearing pass after the walk.

```c
function ProfilerCollection::Start(this):                 // 0xF6A1640
    if (this.size == 0): return OkStatus;                 // empty collection — trivially OK
    merged = OkStatus;                                    // sentinel 1
    for ctrl in this[0 .. size):                          // forward order
        s = ctrl->Start();                                // vtable +0x10 (the controller's Start)
        if (merged == OkStatus): merged = s;              // first status wins
        else if (s is heap-rep):  Unref(s);               // discard later statuses
    return merged;

function ProfilerCollection::CollectData(this, xspace):    // 0xF6A1740
    if (this.size != 0):
        merged = OkStatus;
        for ctrl in this[0 .. size):
            s = ctrl->CollectData(xspace);                // vtable +0x20 — SAME xspace, members APPEND
            if (merged == OkStatus): merged = s; else if (s is heap-rep): Unref(s);
        for ctrl in this[size-1 .. 0]:                    // REVERSE destroy pass
            slot = take(ctrl); set slot = NULL;
            if (slot): slot->~()                          // vtable +0x08 — destroying dtor
        this.size = (begin_after - begin) >> 3;           // collapses to 0 — vector cleared
    else:
        merged = OkStatus;
    return merged;
```

`Stop` @ `0xF6A16C0` is structurally identical to `Start` at vtable slot `+0x18` and has no destroy pass.

> **NOTE —** the status merge is **first-status-wins**: in `Start`/`Stop`/`CollectData`, `merged` is initialized to the inline Ok sentinel and only overwritten while it still equals that sentinel, so it captures the **first** member to return a status (OK or not) and `Unref`s all subsequent ones. With the empty-collection early-out returning OK, the first collector that produces a real status determines the merged result.

> **GOTCHA —** the single shared `XSpace*` is the append contract. Every member writes into the *same* proto: `TpuProfilerImpl` adds device `XPlane`s, `ThreadpoolProfilerInterface` may add an `errors` string, `HostTracer` adds the host plane. Member order therefore determines plane order in the output. The destructive reverse-destroy pass then resets the collection to empty, which is exactly the one-shot behavior the PJRT `CollectData` relies on — a second call runs against an empty vector and re-serializes the cached bytes.

### Function Map

| Function | vtable slot | Addr | Role |
|---|---|---|---|
| `ProfilerCollection::Start` | `+0x10` | `0xF6A1640` | fan `Start` to all members |
| `ProfilerCollection::Stop` | `+0x18` | `0xF6A16C0` | fan `Stop` to all members |
| `ProfilerCollection::CollectData` | `+0x20` | `0xF6A1740` | fan `CollectData`, then destroy members |
| `ProfilerCollection::~ (D2/D0)` | `+0x08` | `0xF6A1840` / `0xF6A18E0` | drop remaining members + heap |
| `ProfilerCollection::ProfilerCollection(vector)` | — | `0xF6A15E0` | take the `CreateProfilers` result |

---

## The Concrete Collectors

### `xprof::tpu::TpuProfilerImpl` — device drain

This is the only collector that touches TPU hardware. Its `CollectData` @ `0xEF34860` is the bridge from the device-side trace transport (`XprofResponse`) to the `XSpace` device planes. It allocates the response on the *XSpace's* arena (so the response's lifetime is tied to the output proto), stamps it with the session's `run_id` and a second metadata field, drains the inner per-chip collector, then converts.

```c
function TpuProfilerImpl::CollectData(this, xspace):       // 0xEF34860
    arena = arena_of(xspace);                              // xspace+8, untag low bit
    resp  = Arena::DefaultConstruct<XprofResponse>(arena);
    if (this.field_0x10 && this.field_0x18):               // session identifiers present
        resp.set_at(296, this.field_0x10);  resp.flags |= 0x100000;     // e.g. run_id
        resp.set_at(336, this.field_0x18);  resp.flags |= 0x8000000;
    inner = this.field_0x08;                               // per-chip collector
    if (inner):
        s = inner->vtable[+40](inner, resp);               // drain into resp
        this.field_0x08 = NULL; inner->~();                // release the inner collector
        if (s != OK):
            if (resp on heap): XprofResponse::SharedDtor(resp); free(resp);
            return s;
    wrapper = XprofResponseWrapper(resp, arena, {...});
    ConvertResponseToTpuXSpace(wrapper, xspace, &nptr, 0); // resp -> device XPlanes appended to xspace
    DropExcessBytes(xspace, xspace);                       // trim oversized event payloads
    return OkStatus;
```

> **NOTE —** `ConvertResponseToTpuXSpace` is where per-chip `TraceEntry` blobs become `XEvent`/`XEventMetadata` on `/device:TPU:N` planes; `DropExcessBytes` enforces a size cap on event metadata. Both are owned by the [trace-entries coder](trace-entries-coder.md) and [XPlane/XStat/TraceMe](xplane-xstat-traceme.md) pages — here they are the opaque tail of the device drain.

### `tsl::profiler::ThreadpoolProfilerInterface` — host append

A near-trivial host collector. Its `CollectData` @ `0xF3326C0` does no tracing work at collect time; it only reports a failed session by appending the stringified status to the XSpace's `errors` repeated field (`XSpace.errors = 2`, at struct offset `+40`).

```c
function ThreadpoolProfilerInterface::CollectData(this, xspace):  // 0xF3326C0
    if (this.status != OK):                                // this+0x08
        msg = this.status.ToStringSlow();
        slot = xspace.errors.Add(arena_of(xspace));        // repeated string errors, +40
        xspace.has_bits |= 2;
        assign(slot, msg);
    return OkStatus;                                        // always OK
```

> **QUIRK —** `ThreadpoolProfilerInterface` contributes no `XPlane`. Its only visible output is an entry in `XSpace.errors` when its threadpool-tracing session failed. A consumer scanning planes for "what the threadpool tracer captured" finds nothing; the signal is in `errors`, not `planes`.

### `xprof::cpu::HostTracer` — host plane

`HostTracer` is the host-side `TraceMe` collector, registered by `CreateHostTracer` @ `0xF32F7C0` (which wraps `CreateHostTracer(HostTracerOptions)` @ `0xF32F820`). Its `Start`/`Stop`/`CollectData` live at `0xF32FA40` / `0xF32FAC0` / `0xF32FB40`. On `CollectData` it flushes the per-thread `TraceMe` recorders into the `/host:0` `XPlane`. The TraceMe capture path itself is on [XPlane/XStat/TraceMe](xplane-xstat-traceme.md).

### Collector Map

| Collector | Factory | `CollectData` | Output into `XSpace` |
|---|---|---|---|
| `xprof::tpu::TpuProfilerImpl` | (legacy `TpuProfiler_Create` path) | `0xEF34860` | `/device:TPU:N` planes via `ConvertResponseToTpuXSpace` |
| `ThreadpoolProfilerInterface` | (static-init) | `0xF3326C0` | `errors` string on failure only |
| `xprof::cpu::HostTracer` | `0xF32F7C0` | `0xF32FB40` | `/host:0` plane from `TraceMe` |

> **NOTE —** the complete factory inventory was not exhaustively enumerated. These three are confirmed by symbol and decompiled body; additional factories (e.g. a megascale RPC tracer gated by `FLAGS_enable_megascale_profiler` @ `0x2236E238`) are populated across several `_GLOBAL__sub_I_*profiler*.cc` static-init blocks and are LOW confidence on completeness.

---

## The Run-Gate — `TpuProfilerControlListener`

### Purpose

Collection (`CollectData`) is a one-shot drain at the end of a session, but tracing runs *concurrently* with `PJRT_LoadedExecutable_Execute`. The coordination point between the two is the `TpuProfilerControlListener` singleton, installed into every TPU chip driver at construction. Before a chip opens its per-core trace ring buffer it asks `CanStartProfiler`; to support mid-execution stop it polls `MustStopProfiler`. The PJRT collector path has no direct knowledge of which chips participate — that is entirely the listener's decision, delegated to the global `xprof::tpu::Profiler` singleton.

### Layout and Construction

```c
/* xprof::tpu::TpuProfilerControlListener — 16 bytes, vtable symbol @ 0x2175C1A0 (installed vptr 0x2175C1B0). */
struct TpuProfilerControlListener {
    /* +0x00 */ void**             vtable;
    /* +0x08 */ xprof::tpu::Profiler* profiler;   /* the global profiler singleton */
};

function GetOrCreateTpuProfilerControlListener():          // 0xF332800
    if (guard(singleton)): return singleton;               // singleton @ 0x224C5D78, __cxa_guard @ 0x224C5D80
    p = operator new(0x10);
    p->profiler = Profiler::GetOrCreateProfilerSingleton(); // 0xF336640
    p->vtable   = off_2175C1B0;  // installed vptr = vtable symbol 0x2175C1A0 + 0x10
    singleton   = p;
    return singleton;
```

### Algorithm

Both gate methods are thin adapters over the `xprof::tpu::Profiler` singleton. `CanStartProfiler` gathers chip identity and config and calls `RegisterChipProfiler`; `MustStopProfiler` calls `UnregisterChipProfiler`. The register/unregister pair is the actual gate — the boolean returned to the chip driver is whether the singleton accepted (start) or demands a stop.

```c
function TpuProfilerControlListener::CanStartProfiler(this, chip_loc, chip_profiler, run_id):  // 0xF3328C0
    VLOG(1) "CanStartProfiler: chip_ordinal=" << chip_loc.index_on_host();  // deepsea_listeners.cc:33
    cfg   = chip_loc.chip->config;
    flags = TpuChipConfig::GetSpecialPurposeSyncFlags(cfg);
    // builds a request {chip_id, run_id, sync_flag_value, megacore} ...
    return Profiler::RegisterChipProfiler(this.profiler);  // delegate: accepts or vetoes the chip

function TpuProfilerControlListener::MustStopProfiler(this, chip_loc):       // 0xF332A00
    VLOG(1) "MustStopProfiler: chip_ordinal=" << chip_loc.index_on_host();   // deepsea_listeners.cc:56
    return Profiler::UnregisterChipProfiler(this.profiler, chip_loc.index_on_host());
```

> **NOTE —** the listener carries several compiler-side registration methods adjacent to the gate — `RegisterLloModule` @ `0xF332BC0`, `RegisterCompilerMetadata` @ `0xF332AE0`, `RegisterDebugMetadata` @ `0xF332D20`, `RegisterBarnaCoreSyncFlagMetadata` @ `0xF333140` — that enrich device-plane `XEventMetadata` with HLO source locations during compilation. They are part of the same singleton but are inputs to the device drain, not the start/stop gate; their detail belongs to the device-plane pages.

### Function Map

| Function | Addr | Role |
|---|---|---|
| `GetOrCreateTpuProfilerControlListener` | `0xF332800` | `__cxa_guard` singleton, wraps `Profiler*` |
| `CanStartProfiler(chip_loc, profiler, run_id)` | `0xF3328C0` | gate chip-in: delegate to `RegisterChipProfiler` |
| `MustStopProfiler(chip_loc)` | `0xF332A00` | poll chip-out: delegate to `UnregisterChipProfiler` |
| `Profiler::GetOrCreateProfilerSingleton` | `0xF336640` | the wrapped `xprof::tpu::Profiler` |

> **NOTE —** the listener's vtable slot ordering relative to its abstract base was not extracted; the four registration methods and two gate methods are confirmed at the cited addresses, but the position of each within the vtable (symbol `0x2175C1A0`, installed vptr `0x2175C1B0`) is LOW confidence.

---

## End-to-End Dataflow

```text
JAX / PT-XLA ──Create──► PLUGIN_Profiler_Create  (0xE6F0C60)
                              └─ CreateProfilers(opts)  (0x1CF50860, under mu)
                                   walk factories ─► [HostTracer][TpuProfilerImpl][Threadpool]...
                                   each wrapped in ProfilerController (0x1CF50CE0)
                              └─ ProfilerCollection(move(vec))  (0xF6A15E0)  ─► handle+0x70

           ──Start──► collection->Start (0xF6A1640) ─► each ctrl->Start ─► inner->Start
                              (meanwhile, per chip:  TpuProfilerControlListener::CanStartProfiler)

           ──Stop───► collection->Stop  (0xF6A16C0) ─► each ctrl->Stop  ─► inner->Stop
                              (per chip:  MustStopProfiler poll)

           ──CollectData──► collection->CollectData(&xspace) (0xF6A1740)
                              ├─ each ctrl->CollectData(&xspace)  (APPEND to one XSpace)
                              │     TpuProfilerImpl  ─► /device:TPU:N planes  (0xEF34860)
                              │     HostTracer       ─► /host:0 plane
                              │     Threadpool       ─► errors[] on failure   (0xF3326C0)
                              └─ destroy all members; collection now empty (one-shot)
                              ─► XSpace serialized to caller buffer (PJRT layer)
```

---

## Relationship to Adjacent Layers

| Layer | Owns | Relationship |
|---|---|---|
| [PJRT Profiler extension](../pjrt/ext-profiler.md) | extension struct, `PLUGIN_Profiler_Api` vtable, handle, 5-method lifecycle | calls `CreateProfilers` and the `ProfilerCollection` fan-out documented here |
| [Legacy `TpuProfiler_*` ABI](tpu-profiler-abi.md) | the 5 C exports + 120-byte handle | reaches the *same* registry/collection via `CreateProfilers` |
| [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) | the XSpace event hierarchy + host capture | the proto these collectors append into |
| [Trace-entries coder](trace-entries-coder.md) | per-chip `TraceEntry` decode | the device payload `ConvertResponseToTpuXSpace` folds in |

---

## Cross-References

- [PJRT Profiler Extension](../pjrt/ext-profiler.md) — the extension struct, `PLUGIN_Profiler_Api` vtable, handle layout, and 5-method lifecycle that sit *above* this factory/collection machine; this page documents the held-across-walk mutex scope and first-status-wins merge that govern it
- [Profiling Overview](overview.md) — where this collector pipeline sits in libtpu's overall telemetry architecture
- [Legacy TpuProfiler C-ABI](tpu-profiler-abi.md) — the second consumer of the same `CreateProfilers` registry, contrasted by handle size and error channel
- [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) — the `XSpace` proto the collectors fan their planes into
- [Trace-entries Coder](trace-entries-coder.md) — the per-chip `TraceEntry` decode behind `TpuProfilerImpl`'s `ConvertResponseToTpuXSpace`
