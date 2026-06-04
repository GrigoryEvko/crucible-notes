# TpuConfigurationApi

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; IDA-recovered C names and demangled C++ symbols quoted verbatim). `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`TpuConfigurationApi_*` is the C-ABI face of **distributed-TPU host configuration** — the bring-up that turns a rack of TPU chips into a single addressable pod and the queries a host process issues against that pod. It is the surface the open-source TensorFlow `tpu_configuration_ops` kernels (`ConfigureDistributedTPU`, `WaitForDistributedTPU`, `_InitializeHostForDistributedTPU`, `_SetGlobalTPUArray`, `_DisconnectHostFromDistributedTPUSystem`) call through, exactly as those kernels call `TpuExecutor_*` for device work. Where [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) is the *in-process driver* init (one node loading the libtpu driver core), this cluster is the *cross-host pod* configuration: it negotiates the global TPU array, waits for every host to join, and exposes per-host capacity to the runtime.

Structurally the cluster is two tiers. The lower tier is a set of `extern "C"` free functions — the **C-ABI roster** this page owns — each a thin bridge that unmarshals a flat op-args struct into a real C++ call and marshals the resulting `absl::Status` plus output array back out. The upper tier is the C++ free functions in the `tensorflow::` namespace that the bridges delegate to (`tensorflow::ConfigureDistributedTpu`, `tensorflow::WaitForDistributedTpu`, …); those carry the actual mesh logic and are owned by the runtime/pod pages, not here. The roster splits cleanly by area: five `*Op_DoWork` *actions* (configure / wait / init-host / set-global-array / disconnect), three pod/cache *queries* (`HasTPUPodState`, `TpusPerHost`, `TpuMemoryLimit`, plus the compilation-cache server-address trio), and two array *free helpers* (`FreeCharArray`, `FreeInt32Array`).

This is a **roster page**. It inventories the C-ABI functions, their implementation symbol + address, the op-args layout each `*Op_DoWork` reads, and the C++ entry each delegates to. It does not re-derive the `ApiFn`/opaque-handle ABI model (owned by [The TfTpu C-API Shim](overview.md)), the driver bootstrap (owned by [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md)), or the megascale collective bring-up (owned by [Megascale Bootstrap](../megascale/bootstrap/overview.md)). It owns the `TpuConfigurationApi_*` roster and the distributed-pod-config entry map.

For reimplementation, the contract is:

- **The roster** — ~13 `extern "C"` free functions, their addresses, and which C++ entry each bridges to.
- **The op-args ABI** — each `*Op_DoWork` takes a single pointer to a flat struct whose fixed byte offsets hold the inputs (int32 array + length, string-view ptr + length, flags) and the output slots (status-cell ptr, `*out_size` ptr, `**out_array` ptr). The bridge owns no policy; it only copies fields, calls, and writes back.
- **The status/output-array protocol** — every action returns its `absl::Status` by storing an `absl::status_internal::StatusRep*` into a caller-owned status cell (with refcount discipline), and emits a heap-allocated output array (`int32[]` or NUL-terminated `char[]`) the caller must release through `FreeInt32Array` / `FreeCharArray`.

| | |
|---|---|
| **Source file (per `.rodata`)** | `learning/45eac/tfrc/executor/stream_executor/tpu_config_c_api.cc` |
| **Roster size** | ~13 `extern "C"` free functions (5 actions, 6 queries, 2 free helpers) |
| **Action address block** | `0xe8cd400`–`0xe8cdb80` (the five `*Op_DoWork`) |
| **Query/free address block** | `0xe8cdbc0`–`0xe8cdf80` (`TpuConfigurationApi_*`) |
| **Reached via** | `ExecutorApiFn()` table (device-runtime accessor) — see [overview](overview.md#2-the-apifn-accessor-pattern) |
| **Status type crossing seam** | `absl::status_internal::StatusRep*` written into a caller status cell |
| **Output ownership** | callee allocates with `operator new` / array `new`; caller frees via `Free{Char,Int32}Array` |
| **Evidence grade** | Byte-confirmed against IDA decompile; roster count CERTAIN |

> **Scope —** the `*ApiFn` accessor/probe model and the opaque-handle (`ToC`/`FromC`/`Destroy`) convention are on [The TfTpu C-API Shim](overview.md); this page documents only the `TpuConfigurationApi_*` cluster that rides those tables. Contrast with [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md): that is single-node driver init; this is multi-host pod configuration.

---

## 1. The Roster

Three areas, one table. "Impl symbol + address" is the `extern "C"` bridge in `libtpu.so` (IDA-recovered name); "Bridges to" is the `tensorflow::` C++ free function it delegates to. Bridge addresses come from the decompiled-symbol address suffix (`_0xADDR.c`); they are exact.

### Actions (`*Op_DoWork`)

| C-ABI function | Address | Bridges to (`tensorflow::`) | Role |
|---|---|---|---|
| `ConfigureDistributedTpuOp_DoWork` | `0xe8cd400` | `ConfigureDistributedTpu` (`0xe975cc0`) | Negotiate the global TPU mesh from this host's chip list; emit serialized pod topology |
| `WaitForDistributedTpuOp_DoWork` | `0xe8cd640` | `WaitForDistributedTpu` (`0xe9767c0`) | Block until every host's per-core mapping has arrived; emit the merged mesh-common-state blob |
| `InitializeHostForDistributedTpuOp_DoWork` | `0xe8cd9a0` | `InitializeHostForDistributedTpu` (`0xe9771c0`) | Bind this host into the configured pod; emit the local core-id `int32` array |
| `SetGlobalTPUArrayOp_DoWork` | `0xe8cda80` | `SetGlobalTPUArray` (`0xe977880`) | Install the pod-wide topology proto (string) into global state; status only |
| `DisconnectDistributedTpuChipsOp_DoWork` | `0xe8cdb80` | `DisconnectDistributedTpuChips` (`0xe977aa0`) | Tear the host out of the pod; status only |

### Queries (pod state + compilation-cache server)

| C-ABI function | Address | Bridges to (`tensorflow::`) | Role |
|---|---|---|---|
| `TpuConfigurationApi_HasTPUPodState` | `0xe8cdca0` | `HasTPUPodState` (`0xeaa17c0`) via `GetTPUConfigResourceMgr` (`0x10854020`) | Predicate: is a pod-state resource registered in the config `ResourceMgr`? |
| `TpuConfigurationApi_TpusPerHost` | `0xe8cdc00` | `TpusPerHost` (`0xe9742a0`) | Out-param `int32`: TPU chips attached to this host |
| `TpuConfigurationApi_TpuMemoryLimit` | `0xe8cdc40` | `TpuMemoryLimit` (`0xe974440`) | Out-param `int64` (a `tsl::gtl::IntType<Bytes_tag_>`): per-core HBM byte budget |
| `TpuConfigurationApi_RemoteCompilationCacheSizeInBytes` | `0xe8cdcc0` | reads `FLAGS_tpu_remote_compilation_cache_size_bytes` | Out-param `int64`: remote compile-cache max size; `CHECK`s ptr non-null and value `>= 0` |
| `TpuConfigurationApi_CompilationCacheServerAddressFromConfig` | `0xe8cdda0` | parses `tensorflow::tpu::TPUHostConfiguration` proto | Decode the compile-cache server address from a serialized `TPUHostConfiguration`; emit a `char[]` |
| `TpuConfigurationApi_GetServerAddressAndPort` | `0xe8cdf80` | `GetServerAddressAndPort` (`0xe975a60`) | Resolve the compile-cache server `host:port` from `FLAGS_uberdriver_port` + `FLAGS_tpu_hostname_override`; emit a `char[]` |

### Free helpers

| C-ABI function | Address | Body | Role |
|---|---|---|---|
| `TpuConfigurationApi_FreeCharArray` | `0xe8cdbc0` | `if (p) free(p);` | Release a `char[]` produced by a query |
| `TpuConfigurationApi_FreeInt32Array` | `0xe8cdbe0` | `if (p) free(p);` | Release an `int32[]` produced by an action |

> **NOTE —** the file-string `tpu_config_c_api.cc` (visible in the `RemoteCompilationCacheSizeInBytes` `CHECK` site at line 158 and the `CompilationCacheServerAddressFromConfig` error at line 175) confirms all three trios live in one translation unit. The `*Op_DoWork` symbols carry no `TpuConfigurationApi_` prefix in IDA, but they share the same source file and the same op-args bridging pattern, so they are documented here as one cluster.

> **GOTCHA —** the two free helpers are *raw* `free()`, not array-`delete`. The arrays they release are produced with `operator new` / `operator new[]` inside the actions (e.g. `InitializeHostForDistributedTpuOp_DoWork` does `**(a1+48) = operator new(4*n)`), then handed back as plain pointers. A reimplementer must allocate the output arrays with an allocator whose deallocation is `free`-compatible (malloc-backed `operator new`) and must *not* run destructors — these are POD `int32`/`char` buffers. Free through these helpers only; never `delete[]` them.

---

## 2. The Op-Args ABI

Every `*Op_DoWork` takes a single pointer to a flat **op-args struct** the open-source kernel fills before the call and reads after. The bridge is policy-free: it copies inputs out of fixed offsets, calls the C++ entry, then writes the status and output array back into fixed offsets. There is no C++ type on the seam — only the args pointer, POD scalars inside it, and the `StatusRep*` written into the status cell.

### `ConfigureDistributedTpuOp_DoWork` (`0xe8cd400`)

The richest layout. The input is this host's chip-coordinate `int32` array; the output is a serialized pod-topology byte string.

```c
// op-args struct read by ConfigureDistributedTpuOp_DoWork(a1)
struct ConfigureArgs {
    /* +0   */ void*    self;            // op object (unused by bridge)
    /* +16  */ uint64_t num_chips;       // length of the int32 array  (a1+16)
    /* +24  */ int32_t* chip_array;      // host chip coords           (a1+24)
    /* +32  */ int64_t  arg3;            // span/extra (>=0 guarded)    (a1+32)
    /* +40  */ void*    arg4;            // string_view-ish input       (a1+40)
    /* +48  */ uint64_t* out_size;       // *out_size = topology length (a1+48)
    /* +56  */ char**    out_blob;       // *out_blob = new char[size]  (a1+56)
    /* +64  */ StatusRep** status_cell;  // result Status               (a1+64)
};

function ConfigureDistributedTpuOp_DoWork(args):              // sub_e8cd400
    chips = copy_int32_array(args.chip_array, args.num_chips)  // grows a vector, may realloc
    status = tensorflow::ConfigureDistributedTpu(             // 0xe975cc0
                 chips, args.num_chips, args.arg4, args.arg3, &out)
    store_status(args.status_cell, status)                    // refcount discipline, §3
    n = small_string_size(out)
    *args.out_size  = n
    *args.out_blob  = operator new(n)
    memmove(*args.out_blob, small_string_data(out), n)
    free_temporaries(chips, out)
```

The chip-array copy loop (lines 40–77) is a hand-inlined `std::vector<int>` grow: it doubles capacity, guards against the `0x3FFFFFFFFFFFFFFF` element-count ceiling, and throws `vector<int>::__throw_length_error` / `__throw_bad_array_new_length` on overflow. The output blob handling (lines 109–135) reads the small-string-optimization discriminant (`v25` sign bit) to choose inline vs heap storage of the returned topology bytes — the standard `absl`/`std::string` SSO layout crossing as raw `data`+`size`.

### `WaitForDistributedTpuOp_DoWork` (`0xe8cd640`)

The input is a **2-D** structure: a vector of per-host `int32` vectors (the per-core mappings collected from every host). The bridge rebuilds it as `vector<vector<int>>`, calls `WaitForDistributedTpu`, and emits the merged mesh-common-state blob.

```c
// op-args struct read by WaitForDistributedTpuOp_DoWork(a1)
struct WaitArgs {
    /* +16 (a1[2]) */ uint64_t num_hosts;     // outer dimension
    /* +24 (a1[3]) */ uint64_t cores_per_host;// inner dimension
    /* +32 (a1[4]) */ int32_t** host_arrays;  // num_hosts pointers to int32[]
    /* +40 (a1[5]) */ void*     mesh_state;    // TpuMeshCommonState* (in/out)
    /* +48 (a1[6]) */ uint64_t* out_size;
    /* +56 (a1[7]) */ char**    out_blob;
    /* +64 (a1[8]) */ StatusRep** status_cell;
};

function WaitForDistributedTpuOp_DoWork(args):                // sub_e8cd640
    mappings = vector<vector<int>>(args.num_hosts)            // __append reserves outer
    for h in 0 .. num_hosts:                                  // nested grow loops
        for c in 0 .. cores_per_host:
            mappings[h].push_back(args.host_arrays[h][c])
    status = tensorflow::WaitForDistributedTpu(               // 0xe9767c0
                 mappings.data, mappings.size, args.mesh_state, &out)
    store_status(args.status_cell, status)
    emit_char_blob(args.out_size, args.out_blob, out)
    destroy(mappings)                                          // frees every inner vector
```

> **QUIRK —** the inner-loop trip count is re-read from `args.cores_per_host` (`v2[3]`) on every iteration of the inner body (line 117), not cached once. The decompile shows a `while (!v6)` skip when the count is momentarily zero. A reimplementer should treat `cores_per_host` as the authoritative inner bound but must tolerate the runtime mutating it — the binary defensively rereads rather than snapshotting.

### `InitializeHostForDistributedTpuOp_DoWork` (`0xe8cd9a0`)

Two-string + two-bool input; `int32[]` output (the local core ids).

```c
// op-args struct read by InitializeHostForDistributedTpuOp_DoWork(a1)
struct InitHostArgs {
    /* +16 */ int64_t  tpu_topology_len;   // (>=0 guarded)
    /* +24 */ char*    tpu_topology;       // serialized topology string_view
    /* +32 */ uint8_t  enable_whole_mesh;  // bool
    /* +33 */ uint8_t  is_master;          // bool
    /* +40 */ uint64_t* out_size;          // # of core ids
    /* +48 */ int32_t** out_core_ids;      // *out = new int32[out_size]
    /* +56 */ StatusRep** status_cell;
};

function InitializeHostForDistributedTpuOp_DoWork(args):      // sub_e8cd9a0
    status = tensorflow::InitializeHostForDistributedTpu(     // 0xe9771c0
                 args.tpu_topology, args.tpu_topology_len,
                 args.enable_whole_mesh, args.is_master, &core_ids)
    store_status(args.status_cell, status)
    n = core_ids.size
    *args.out_size     = n
    *args.out_core_ids = operator new(4 * n)                  // int32[n]
    memmove(*args.out_core_ids, core_ids.data, 4 * n)
    free(core_ids.data)
```

### `SetGlobalTPUArrayOp_DoWork` (`0xe8cda80`)

A pure string-in / status-out bridge with the SSO-aware string materialization explicit.

```c
function SetGlobalTPUArrayOp_DoWork(len, data, status_cell):  // sub_e8cda80
    // build a NUL-terminated std::string from (data, len), SSO for len <= 0x16
    s = make_string(data, len)
    status = tensorflow::SetGlobalTPUArray(s)                 // 0xe977880
    store_status(status_cell, status)
    if heap_allocated(s): free(s.data)
```

> **NOTE —** unlike the other actions, `SetGlobalTPUArray` and `DisconnectDistributedTpuChips` take their status cell as a *direct* argument (`a3` / `a2`) rather than at an offset inside an op-args struct. They produce no output array — status only — so their signatures are simpler: `(len, data, StatusRep**)` and `(self, StatusRep*-cell)` respectively. The status protocol (§3) is identical.

### `DisconnectDistributedTpuChipsOp_DoWork` (`0xe8cdb80`)

The thinnest action — one delegate call plus the status store.

```c
function DisconnectDistributedTpuChipsOp_DoWork(self, status_cell): // sub_e8cdb80
    status = tensorflow::DisconnectDistributedTpuChips(self, status_cell) // 0xe977aa0
    store_status(status_cell, status)                          // in place at *status_cell
```

---

## 3. The Status and Output-Array Protocol

Two conventions are shared by every function in the cluster and are the part a reimplementer most needs to get bit-exact.

### Status: `StatusRep*` into a caller cell, with refcount discipline

`absl::Status` crosses the seam as the single pointer `absl::status_internal::StatusRep*`. The bridge writes it into a caller-owned cell with the standard `absl::Status` move/ref protocol, identical in all five actions and the three queries that carry status:

```c
// store_status(cell, new_rep) — the inlined idiom in every bridge
function store_status(StatusRep** cell, StatusRep* new_rep):
    old = *cell
    if (new_rep != old):
        *cell = new_rep
        if ((old & 1) == 0):           // low bit set => "inlined OK", nothing to unref
            StatusRep::Unref(old)
    else:                              // same rep: drop the extra reference we hold
        if ((new_rep & 1) == 0):
            StatusRep::Unref(new_rep)
```

> **QUIRK —** the low bit of a `StatusRep*` is a tag, not an address bit. A value with bit 0 set is an *inlined* status (the OK sentinel or a small inlined code), which owns no heap `StatusRep` and must never be `Unref`'d — every branch guards `(ptr & 1) == 0` before calling `StatusRep::Unref`. A reimplementer who treats the cell as a plain pointer and unconditionally unrefs will corrupt the refcount of the OK sentinel. `GetServerAddressAndPort` additionally shows the *acquire* side: when it stores a borrowed rep it `_InterlockedIncrement`s the refcount first (line 96), proving the cell takes an owning reference.

### Output arrays: callee-allocated, caller-freed

The three array-producing functions (`Configure`, `WaitFor`, `InitializeHost`) and the two string-producing queries (`CompilationCacheServerAddressFromConfig`, `GetServerAddressAndPort`) follow one shape: write the element/byte count to `*out_size`, allocate the buffer with `operator new`, copy into it, and return. The caller later releases it through `FreeInt32Array` (for the `int32[]` outputs) or `FreeCharArray` (for the `char[]` outputs). For string outputs the allocation is `out_size + 1` and the copy is `strncpy` — i.e. the emitted `char[]` is NUL-terminated and `out_size` excludes the terminator (see `CompilationCacheServerAddressFromConfig` lines 76–81, `GetServerAddressAndPort` lines 112–117).

> **GOTCHA —** the `int32[]` outputs are sized as `operator new(4 * n)` (byte count = `4 * element_count`), but `*out_size` is set to **`n` (element count)**, not the byte count. `FreeInt32Array` ignores the size entirely (it is a bare `free(p)`), so the mismatch is harmless for freeing — but a reimplementer reading `out_size` to bound a loop must treat it as element count, while one computing the original `new` size must multiply by 4.

---

## 4. The Distributed-Pod Configuration Entry Map

Read top to bottom, the cluster is a host's lifecycle inside a TPU pod. The map below names, for each phase, the open-source op-kernel that calls in, the C-ABI bridge, and the C++ entry that holds the logic.

```text
TF op kernel (open source)          C-ABI bridge (this binary)              C++ entry (this binary, runtime/pod owned)
─────────────────────────────────   ─────────────────────────────────────  ──────────────────────────────────────────
ConfigureDistributedTPU         ──▶ ConfigureDistributedTpuOp_DoWork    ──▶ tensorflow::ConfigureDistributedTpu  0xe975cc0
                                       (0xe8cd400)                              └─ builds global mesh, emits topology blob
_SetGlobalTPUArray              ──▶ SetGlobalTPUArrayOp_DoWork          ──▶ tensorflow::SetGlobalTPUArray         0xe977880
                                       (0xe8cda80)                              └─ installs pod-wide topology proto
_InitializeHostForDistributedTPU──▶ InitializeHostForDistributedTpuOp.. ──▶ tensorflow::InitializeHostForDistributedTpu 0xe9771c0
                                       (0xe8cd9a0)                              └─ binds host, returns local core ids
WaitForDistributedTPU           ──▶ WaitForDistributedTpuOp_DoWork      ──▶ tensorflow::WaitForDistributedTpu     0xe9767c0
                                       (0xe8cd640)                              └─ barrier across hosts, merges mesh state
[runtime queries, any time]     ──▶ TpuConfigurationApi_HasTPUPodState  ──▶ tensorflow::HasTPUPodState            0xeaa17c0
                                       (0xe8cdca0)                              └─ via GetTPUConfigResourceMgr     0x10854020
                                ──▶ TpuConfigurationApi_TpusPerHost      ──▶ tensorflow::TpusPerHost              0xe9742a0
                                ──▶ TpuConfigurationApi_TpuMemoryLimit   ──▶ tensorflow::TpuMemoryLimit           0xe974440
_DisconnectHostFromDistributed.. ──▶ DisconnectDistributedTpuChipsOp..  ──▶ tensorflow::DisconnectDistributedTpuChips 0xe977aa0
                                       (0xe8cdb80)                              └─ removes host from pod
```

The ordering is the contract a multi-host launcher reproduces: one host configures the global array, every host sets it and initializes locally, all hosts rendezvous at the wait barrier, and the runtime then queries pod state and per-host capacity throughout the session before any host finally disconnects. The compile-cache trio (`RemoteCompilationCacheSizeInBytes`, `CompilationCacheServerAddressFromConfig`, `GetServerAddressAndPort`) is orthogonal to the bring-up sequence: it resolves where the *compilation cache server* lives, driven by the flags `FLAGS_tpu_remote_compilation_cache_size_bytes`, `FLAGS_uberdriver_port`, and `FLAGS_tpu_hostname_override` and by the serialized `tensorflow::tpu::TPUHostConfiguration` proto.

> **NOTE —** `HasTPUPodState` is the one query that goes through a resource manager rather than a direct free function: the bridge first calls `tensorflow::GetTPUConfigResourceMgr` (`0x10854020`) to obtain the config `ResourceMgr`, then `tensorflow::HasTPUPodState(rmgr)` (`0xeaa17c0`). The pod-state object is a registered resource keyed in that manager; "has pod state" is "is the resource present." This is how a host distinguishes "pod already configured" from "fresh bring-up."

> **QUIRK —** `RemoteCompilationCacheSizeInBytes` does not call a `tensorflow::` entry — it reads a process-global. It first `CHECK`-fails (LogMessageFatal at `tpu_config_c_api.cc:158`) if the out-pointer is null, then returns either a cached value (`unk_2225C260`, when initialized) or the live flag `FLAGS_tpu_remote_compilation_cache_size_bytes`, and `CHECK`-fails again (`:161`) if the result is negative. It is a query with hard preconditions, not a fallible status-returning call — there is no status cell in its signature.

---

## Related Components

| Name | Relationship |
|---|---|
| `tensorflow::ConfigureDistributedTpu / WaitForDistributedTpu / InitializeHostForDistributedTpu / SetGlobalTPUArray / DisconnectDistributedTpuChips` | the C++ free functions the five `*Op_DoWork` bridges delegate to; hold the mesh logic |
| `tensorflow::GetTPUConfigResourceMgr` | resolves the config `ResourceMgr` that `HasTPUPodState` probes |
| `tensorflow::tpu::TPUHostConfiguration` (proto) | parsed by `CompilationCacheServerAddressFromConfig` to extract the cache server address |
| `absl::status_internal::StatusRep` | the refcounted status object whose pointer crosses the seam into the caller's status cell |
| `ExecutorApiFn()` table | the device-runtime function-pointer struct through which these C-ABI entries are reached |
| `FLAGS_uberdriver_port / FLAGS_tpu_hostname_override / FLAGS_tpu_remote_compilation_cache_size_bytes` | the process flags the compile-cache queries read |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the `*ApiFn` accessor pattern and opaque-handle convention this cluster rides; the roster map that lists `TpuConfigurationApi_*` among the nine clusters
- [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) — the *in-process driver* init; contrast with this page's *cross-host pod* configuration
- [Megascale Bootstrap](../megascale/bootstrap/overview.md) — the collective-runtime bring-up that layers above pod configuration
- [TpuPlatform & TpuNodeContext](tpu-platform-and-topology.md) — `TpuPlatform::TpusPerHost` / `TpuMemoryLimit` are the SE-object-model siblings of the same-named queries here
- [TpuExecutor Roster](tpu-executor-roster.md) — the per-device runtime C-ABI cluster the same op kernels call for device work
- [back to index](../index.md) — Part III — Tpu C-Shim Layer
