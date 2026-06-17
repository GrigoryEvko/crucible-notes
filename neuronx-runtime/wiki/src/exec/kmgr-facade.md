# The KMGR Exec-Manager Facade: Model DB and the Stage Seam

> *All addresses, offsets, and struct ordinals on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64 x86-64, not stripped, DWARF present). `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA verifiable against `nm -n libnrt.so`; BSS globals are cited by their `_ZL` symbol. Source asserts name `/opt/workspace/KaenaRuntime/kmgr/{dlr.cpp, dlr_kelf.cpp}`. Other versions will differ.*
> *Evidence grade: **Confirmed (decompiled + struct-anchored)** — the model-DB refcount protocol, the `kmgr_exec` dispatch fork, the `kmgr_unload_nn` teardown state machine, and the `dlr_kelf_stage` seam were read from per-function decompilation and cross-checked against the IDA `functions.json` (frame/insn/callgraph), `structures.json` (ordinals), `names.json` (BSS symbols), and `strings.json`. · Part VII — Execution Engine · [back to index](../index.md)*

## Abstract

`kmgr` (the kernel/model manager, TU `kmgr/dlr.cpp` + `kmgr/dlr_kelf.cpp`) is the runtime's **orchestration facade**: the thin C++ layer that sits directly below the public `nrt_*` API and directly above the two execution engines — the synchronous XU work-queue ([submit path](submit-path.md)) and the implicit-async worker pool ([XU workers](xu-workers.md)) — plus the per-core kernel backend (`kbl_*`) and the device driver (`tdrv_*`). Read it the way you read a dynamic linker's runtime stub table: it owns no hardware itself, holds no descriptor rings, fires no `IOCTL`. What it owns is the **process-global model registry** and the small set of decisions that route a public call to the right lower half — resolve the handle, ref-count the model, pick sync vs async, build the per-execution resources, and on teardown drain the registry safely.

The facade is organized around one process-global object, `dlr_model_set` (`@0xc5c720`) — an `unordered_map<uint32 H_NN.id, dlr_model*>` guarded by a `pthread_rwlock`, plus a monotone `last_nn_id` allocator. Every operation on a loaded model funnels through the same two-call refcount protocol: `db_get_nn_ref_count` (`@0xdc0c0`) takes the read lock, hashes the `H_NN`, bumps `dlr_model.ref_count` (`+0x1B0`) under an interlocked add, and returns the `dlr_model*`; the caller does its work and releases with `nn_ref_decrement` (`@0xdc110`). That ref bump is the lifetime guarantee the whole facade rests on: `kmgr_unload_nn` cannot delete a model out from under an in-flight execution because the execution holds a ref, and the unload path *spins* until the count drains to zero. This page documents that registry and the four facade clusters that read or mutate it — the **refcount accessors**, the **execute fork** (`kmgr_exec`), the **unload state machine** (`kmgr_unload_nn`), and the **stage seam** (`dlr_kelf_stage`) where the parse-side model crosses into the device-side per-core records.

The facade deliberately stops at well-defined seams. The submit funnel, the doorbell `IOCTL`, the `device_exec_request_t` layout, and the lock-free `xu_queue` belong to [the submit path](submit-path.md). The NQ harvest, the 16-byte notification decode, and the `mark_comp_efd` write belong to [the completion engine](completion-engine.md). The TNC parse cache, the six-stage load sequence, and the per-core `model_t` build belong to [the load pipeline](../neff/load-pipeline.md). This page links those internals rather than re-deriving them; its job is the orchestration — the handle resolution, the dispatch decision, the resource lifecycle, and the registry mutation — that wraps them.

For reimplementation, the contract is:

- **The model registry** — `dlr_model_set` (`unordered_map<H_NN.id, dlr_model*>` + rwlock + `last_nn_id`) and the **480-byte `dlr_model`** value it holds — and the two-call `db_get_nn_ref_count` / `nn_ref_decrement` refcount protocol every accessor obeys.
- **The execute fork** — `kmgr_exec` branching on the process-global `async_exec_workers` pointer, the *inverted* exec-mode argument passed to `kmgr_exec_pre`, and the host-tensor clone (sync) vs reject (async) contract.
- **The resource lifecycle** — `kmgr_exec_pre` builds a 56-byte `kmgr_exec_resources_t`, `kmgr_exec_resources_free` tears it down, and every `kmgr_exec` error edge frees exactly what it allocated.
- **The unload state machine** — `kmgr_unload_nn`'s `state==STOPPED ∧ ref_count==0` guard, the wrlock'd hashtable erase, the ref-drain spin, and the sync (`tpb_xu_model_unstage`) vs async (`kmgr_async_exec_model_stop`) divergence.
- **The stage seam** — `dlr_kelf_stage`, the KMGR↔KBL boundary where one `kelf_nn_t` of parsed subgraphs becomes a `dlr_kelf_model_t` bridge plus one device `model_t` per core, single-core inline or multi-core fanned out one pthread per subgraph.

| | |
|---|---|
| **Model registry** | `dlr_model_set` `@0xc5c720` — `unordered_map<uint32,dlr_model*>` + rwlock + `last_nn_id`, **120 B** (ord 13218) |
| **Registry value** | `dlr_model` — **480 B** (`0x1E0`, ord 13259); freed via `operator delete(mod, 0x1E0)` |
| **Async-mode discriminator** | `async_exec_workers` `@0xc5d8c8` — non-NULL ⇒ implicit-async mode |
| **Refcount get** | `db_get_nn_ref_count` `@0xdc0c0` (73 B) — rdlock, find, `_InterlockedAdd64(ref_count,+1)` |
| **Refcount put** | `nn_ref_decrement` `@0xdc110` (59 B) — assert `ref_count>0`, `_InterlockedSub64(-1)` |
| **Execute fork** | `kmgr_exec` `@0xdfd50` (1138 B, 257 insns) — branch on `async_exec_workers` |
| **Resource build** | `kmgr_exec_pre` `@0xdf820` (1327 B) — `calloc(0x38)` + per-TPB compute resources |
| **Resource free** | `kmgr_exec_resources_free` `@0xdd350` (438 B) |
| **Unload** | `kmgr_unload_nn` `@0xdc450` (1069 B, 261 insns) — guard + erase + ref-drain spin |
| **Teardown** | `kmgr_unstage_kelf_model` `@0xdc180` (707 B) — `dlr_kelf_unstage` + `delete mod` |
| **Stage seam** | `dlr_kelf_stage` `@0xe0970` (652 B) — `calloc(0x48)` `dlr_kelf_model_t`; single/multi dispatch |
| **No IOCTL** | the entire facade is userspace orchestration; doorbell/NQ/MEM_ALLOC are below the seams |

---

## 1. The Model Registry and the Refcount Protocol

### Purpose

`dlr_model_set` (`@0xc5c720`) is the single source of truth for "which models are loaded." It is a process-global `unordered_map<uint32 H_NN.id, dlr_model*>` with a `pthread_rwlock` and a monotone `last_nn_id` id allocator. Every public-API operation that names a model by handle — execute, get-tensor-info, set-mac-count, set-profiling, unload — resolves the handle through this map under the read lock, and every such resolution bumps the target `dlr_model`'s reference count so the model cannot be freed mid-operation. This section pins the registry layout and the refcount protocol before any cluster that uses it, because every later algorithm opens with `db_get_nn_ref_count` and closes with `nn_ref_decrement`.

### Algorithm

The two-call protocol is deliberately minimal. `db_get_nn_ref_count` is a 73-byte function: take the read lock, hash the `H_NN.id`, and on a hit do one interlocked increment of `ref_count` (`+0x1B0`) before returning the pointer. The release side is even smaller — 13 instructions, no lock (the decrement is atomic and the model is kept alive by the very ref being dropped, so no map lock is needed to find it).

```c
dlr_model* db_get_nn_ref_count(H_NN h_nn):                  // 0xdc0c0
    pthread_rwlock_rdlock(&dlr_model_set.lock)              // +0x38 (offset 56)
    it = dlr_model_set.models.find(h_nn.id)                 // _Hashtable::find.isra.0
    mod = (it != end) ? it->second : NULL
    if mod:
        _InterlockedAdd64(&mod->ref_count, 1)              // +0x1B0 (offset 432), atomic
    pthread_rwlock_unlock(&dlr_model_set.lock)
    return mod                                              // NULL on miss; caller logs "Failed to find model: %u"

void nn_ref_decrement(dlr_model* mod):                      // 0xdc110
    assert mod != NULL && mod->ref_count > 0                // kmgr/dlr.cpp:0xED
    _InterlockedSub64(&mod->ref_count, 1)                   // +0x1B0, atomic; no lock
```

`dlr_db_get_nn_cached_interned_model_id` (`@0xdc150`, 33 B) is the canonical "get-and-immediately-release" shape that every metadata accessor follows: `db_get_nn_ref_count` → read one field (`interned_model_id`, `+0x118`) → `nn_ref_decrement`, returning `0` on a registry miss. The load path (`nrt_load_util`, `nrt_load_collectives`) and the async submitter (`kmgr_async_exec_add_work`) all call it to attach the interned id to traces.

> **NOTE —** the ref-bump is the entire concurrency model between execute and unload. A reimplementation that resolves the handle without holding a ref for the duration of the operation has a use-after-free: `kmgr_unload_nn` (§4) erases the map entry and then deletes the `dlr_model`, and only the `ref_count` spin keeps it from freeing memory an in-flight `kmgr_exec` still reads through `mod->vcore` / `mod->kelf_model`. The read lock protects the *map*; the ref count protects the *model*.

### Struct: `dlr_model` (480 bytes, `0x1E0`, ord 13259)

The per-process model record — the value type of `dlr_model_set.models`, built by [the load pipeline](../neff/load-pipeline.md) and consumed by every facade cluster here. Its `0x1E0` size is proven by the `operator delete(mod, 0x1E0)` in `kmgr_unstage_kelf_model`. Offsets are verbatim from `structures.json` and match the loader's field writes.

| Field | Offset | Type | Meaning | Read by |
|---|---|---|---|---|
| `h_nn` | `+0x000` | `H_NN` `{uint32}` | the map key; `= last_nn_id++` at load | registry, traces |
| `vcore` | `+0x008` | `const virtual_core_t*` | bound logical core; reached as `mod->vcore->tpbs[…]` everywhere | exec, unload, stop |
| `sg_count` | `+0x010` | `uint32_t` | #subgraphs == loop count for kickoff / compute-setup | exec, kickoff |
| `name` | `+0x014` | `char[256]` | model name | metadata, logs |
| `interned_model_id` | `+0x118` | `uint64_t` | global interned id for traces/NDS | `…cached_interned_model_id` |
| `uuid` | `+0x120` | `uint8_t[16]` | NEFF uuid; folded to 64-bit for kmetric agg id | unload, trace-set-uuid |
| `hlo_mac_count` | `+0x130` | `uint64_t` | MAC count from `hlo_stats.json` | NDS metrics |
| `exec_timeout` | `+0x138` | `uint64_t` | per-model timeout (seconds); basis of the sync wait budget | `kmgr_sync_exec` |
| `input_info` | `+0x140` | `IO_MAP` (48 B) | `std::map<string, io_node_info_t>` | metadata cluster (§5) |
| `output_info` | `+0x170` | `IO_MAP` (48 B) | output tensor map | metadata cluster (§5) |
| `kelf_model` | `+0x1A0` | `dlr_kelf_model_t*` | the parse↔stage bridge; `->h_model` to `kbl_*` | exec-pre, unstage |
| `state` | `+0x1A8` | `volatile dlr_model_state_t` | `4` = READY/STOPPED; unload requires `4` | unload guard |
| `ref_count` | `+0x1B0` | `volatile uint64_t` | interlocked refcount; unload spins to 0 | refcount protocol |
| `nds_model_node_objs` | `+0x1B8` | `nds_obj_handle_t*` | per-subgraph NDS handles | create/teardown NDS |
| `nds_model_node_count` | `+0x1C0` | `int` | length of the NDS handle array | unstage |
| `is_profiling` | `+0x1C4` | `bool` | set by `kmgr_set_profiling_status`; gates async poll | exec async arm |
| `replica_group_info` | `+0x1C8` | `vector<vector<…compressed*>>` (24 B) | CC replica subgroup info | unstage frees it |

### Struct: `dlr_model_set` (120 bytes, ord 13218)

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `models` | `+0x00` | `unordered_map<uint32, dlr_model*>` (56 B) | the registry, keyed by `H_NN.id` |
| `lock` | `+0x38` | `pthread_rwlock_t` (56 B) | guards `models` and `last_nn_id` |
| `last_nn_id` | `+0x70` | `uint32_t` | monotone `H_NN` allocator (incremented under wrlock at load) |

> **QUIRK —** the `state == 4` value is the *only* `dlr_model_state_t` enumerator this facade ever asserts on, named only by the unload diagnostic `"DLR %u state %u, must be stopped before unloading"`. The loader sets `state = 4` on a successful load (the load pipeline calls it "READY"); the unload guard calls the same `4` "STOPPED." They are the same numeric value — a loaded-and-idle model is simultaneously READY-to-execute and STOPPED-enough-to-unload. The other enumerators (any transient EXECUTING/STOPPING state) are not in this TU; treat `4` as "settled, no in-flight transition" and do not assume a richer state set (LOW confidence on the full enum; the `4` guard is HIGH).

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `db_get_nn_ref_count` | `0xdc0c0` | 73 | rdlock; `find(h_nn.id)`; `_InterlockedAdd64(ref_count,+1)` | HIGH |
| `nn_ref_decrement` | `0xdc110` | 59 | assert `ref_count>0`; `_InterlockedSub64(-1)` | HIGH |
| `dlr_db_get_nn_cached_interned_model_id` | `0xdc150` | 33 | get → read `interned_model_id(+0x118)` → put; `0` on miss | HIGH |
| `kmgr_register_async_exec_callback` | `0xdc0b0` | 5 | thunk → `kmgr_async_exec_register_async_status_callback` | HIGH |

---

## 2. The Execute Fork

### Purpose

`kmgr_exec` (`@0xdfd50`) is the facade's dispatch point for a single inference. It is reached only from `nrt_execute_repeat` (`callers == {nrt_execute_repeat}`, callgraph-confirmed); by the time control arrives the public lifecycle guards and the ifmap-count check have already run ([submit path §1](submit-path.md#1-entry-point-and-dispatch-fork)). `kmgr_exec`'s own job is narrow: resolve the handle, count how many tensors are host-resident, decide sync vs async, build resources via `kmgr_exec_pre`, dispatch to the right engine, and free everything it allocated on every edge. It is the seam between the API and the two engines — it does not itself touch a descriptor ring.

### Entry Point

```text
nrt_execute_repeat (0x91650)
  └─ kmgr_exec (0xdfd50)                          ── resolve handle; THE sync/async fork
       ├─ db_get_nn_ref_count (0xdc0c0)           ── H_NN → dlr_model* (ref-bumped)
       ├─ kbl_tensor_set_get_malloc_tensor_count  ── count host-resident in/out tensors
       ├─ kmgr_exec_pre (0xdf820)                 ── build kmgr_exec_resources_t (§3)
       ├─ [sync]  kmgr_sync_exec (0xdca70)        ── submit + BLOCK    → submit-path §6
       ├─ [async] kmgr_async_exec_add_work (0xe6d20) ── enqueue, return → xu-workers
       ├─ kmgr_exec_resources_free (0xdd350)      ── error/cleanup edges
       └─ nn_ref_decrement (0xdc110)              ── release the ref
```

### Algorithm

```c
NRT_STATUS kmgr_exec(H_NN h_nn, const fmap_set* in, fmap_set* out, uint64 tracking_id):  // 0xdfd50
    mod = db_get_nn_ref_count(h_nn)                          // 0xdc0c0
    if !mod: return NRT_INVALID_HANDLE                       // "Failed to find model: %u"
    n_in  = kbl_tensor_set_get_malloc_tensor_count(in)       // host-resident input tensors
    n_out = kbl_tensor_set_get_malloc_tensor_count(out)
    in_dev = out_dev = NULL

    if async_exec_workers == NULL:                           // ---- SYNC (default) ----
        if n_in:                                             // shadow-clone host inputs to HBM
            in_dev  = kbl_tensor_set_clone_to_physical_mem(in)    // "Failed to create device … input tensor set"
            kbl_tensor_set_copy(in, in_dev)                  // "Failed to copy input tensors to device"
        if n_out:
            out_dev = kbl_tensor_set_clone_to_physical_mem(out)   // alloc only; copied back after
        rc = kmgr_exec_pre(mod, in_dev?:in, out_dev?:out, /*mode*/1, &resources)   // BIND (§3)
        if rc: goto cleanup                                  // mode arg = (workers==NULL) = 1, see QUIRK
        rc = kmgr_sync_exec(mod, &resources, tracking_id)    // SUBMIT + BLOCK  → submit-path
        if rc == 0 && out_dev:
            kbl_tensor_set_copy(out_dev, out)                // "Failed to copy output device tensors …"
    else:                                                    // ---- ASYNC (implicit) ----
        if (n_in | n_out):                                   // host tensors forbidden
            nn_ref_decrement(mod); return NRT_INVALID        // "Async exec mode only supports device …"
        rc = kmgr_exec_pre(mod, in, out, /*mode*/0, &resources)   // BIND; mode = (workers==NULL) = 0
        if rc: goto cleanup
        assert in_dev == NULL && out_dev == NULL             // kmgr/dlr.cpp:0x4E9
        worker = async_exec_workers->workers[mod->vcore->vtpb_idx]
        assert worker != NULL                                // kmgr/dlr.cpp:0x4F4
        rc = kmgr_async_exec_add_work(async_exec_workers, h_nn,
                                      mod->vcore->vtpb_idx, resources, &handle, tracking_id)  // 0xe6d20
        if rc == 0 && mod->is_profiling:                     // +0x1C4
            kmgr_async_exec_poll(worker, handle)             // 0xe6ab0 — drain for profile capture
    nn_ref_decrement(mod)                                    // 0xdc110
    return rc

cleanup:                                                     // any kmgr_exec_pre / clone failure
    if resources: kmgr_exec_resources_free(&resources)       // 0xdd350
    if in_dev:    kbl_free_feature_map_set(in_dev)           // free shadow clones
    if out_dev:   kbl_free_feature_map_set(out_dev)
    nn_ref_decrement(mod)
    return rc
```

The shadow-clone and the cleanup edges are confirmed by `kmgr_exec`'s callee multiset: `kbl_free_feature_map_set` appears **four** times (the two clones × two error edges), `kbl_tensor_set_clone_to_physical_mem` twice (in/out), `kbl_tensor_set_copy` twice (in-copy, out-copy-back), and `kmgr_exec_resources_free` once — exactly the resource accounting above.

> **QUIRK —** the exec-mode argument to `kmgr_exec_pre` is computed as `(async_exec_workers == NULL)`. So the **sync** path passes mode `1` and the **async** path passes mode `0` — the naming inverts intuition. The `kmgr_exec_mode_t` enum labels are `KMGR_EXEC_MODE_ASYNC = 0` and `KMGR_EXEC_MODE_EXPLICIT_ASYNC = 1`, so the plain synchronous `nrt_execute` path drives the `EXPLICIT_ASYNC` enum value. `kmgr_exec_pre`'s `mode != 0` branch (the sync caller) is the one that asserts `exec_mode == EXPLICIT_ASYNC` (`kmgr/dlr.cpp:0x48C`). Do not assume `0 == sync == default`; the branch *values* are HIGH-confidence, the enum *labels* are counter-intuitive (and were flagged MED on naming in the seed). The mode lands in `kmgr_exec_resources_t.exec_mode` (`+0x20`) and later selects which fmap-set union arm `kmgr_exec_wait` / `kmgr_exec_resources_free` operate on.

> **GOTCHA —** the two forks have **different input contracts**, not a transparent latency knob. The sync fork *clones-and-copies* any host (CPU-malloc) tensor into HBM and copies device results back; the async fork *hard-rejects* any host-resident tensor with `NRT_INVALID` ("Async exec mode only supports device allocated tensors…"). A reimplementation that switches `async_exec_workers` on without also enforcing device-only tensors on the async side will fault every host-tensor execution. The discriminator is global and process-wide (set once by `kmgr_init`); a single process is either sync-mode or async-mode for all models.

### The async-mode discriminator

`async_exec_workers` (`@0xc5d8c8`, `_ZL18async_exec_workers`, a `kmgr_async_exec_worker_threads_t*`) is the one global that decides the fork. It is NULL by default and set non-NULL only when `kmgr_init` is invoked with `implicit_async_exec == true` (which first asserts `async_exec_workers == NULL`, `kmgr/dlr.cpp:0x677`, then calls `kmgr_async_exec_init`). `kmgr_destroy` clears it back to NULL on teardown. Its `workers[]` array (the per-vnc worker, indexed `mod->vcore->vtpb_idx`) lives at field `+0x800` of the struct. The same pointer also gates `kmgr_exec_pre`'s drain step and `kmgr_unload_nn`'s stop branch — it is the single switch that selects every sync-vs-async divergence in the facade.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `kmgr_exec` | `0xdfd50` | 1138 | handle resolve; sync/async fork; host-tensor clone; cleanup | HIGH |
| `kbl_tensor_set_get_malloc_tensor_count` | `0x308370` | — | count host-resident tensors in a set | HIGH |
| `kbl_tensor_set_clone_to_physical_mem` | `0x307f70` | — | shadow-clone a host tensor set to HBM | HIGH |
| `kbl_tensor_set_copy` | `0x308200` | — | copy in (to-device) / out (from-device) | HIGH |
| `kbl_free_feature_map_set` | `0x307d50` | — | free a shadow fmap set on the cleanup edges | HIGH |
| `kmgr_async_exec_add_work` | `0xe6d20` | — | enqueue async request; return handle (→ [xu-workers](xu-workers.md)) | HIGH |
| `kmgr_async_exec_poll` | `0xe6ab0` | — | drain in-flight when `is_profiling` | HIGH |

---

## 3. The Execute-Resource Lifecycle

### Purpose

Both forks of §2 first call `kmgr_exec_pre` (`@0xdf820`) to build the per-execution resource tracker, and the explicit-async API path (`kmgr_tpb_xu_schedule`, §6) calls it too — `callers == {kmgr_exec, kmgr_tpb_xu_schedule}`. `kmgr_exec_pre` validates the IO sets, allocates a 56-byte `kmgr_exec_resources_t`, ref-bumps the feature maps, builds the per-TPB compute resources, and on the first execution after a model switch bootstraps the collectives resources under the hardware-exec-queue lock. Its inverse is `kmgr_exec_resources_free` (`@0xdd350`). This pair is the leak-freedom boundary: a clean execution frees the tracker on the completion side; a failed one frees it on the error edge in `kmgr_exec`.

### Algorithm

```c
NRT_STATUS kmgr_exec_pre(dlr_model* mod, const fmap_set* in, fmap_set* out,
                         kmgr_exec_mode_t mode, kmgr_exec_resources_t** out_res):   // 0xdf820
    rc = dlr_check_valid_io_sets(mod->kelf_model, in, out)   // 0xe5b30 — tensor-io + placement
    if rc: return rc                                         // NULL set → NRT_INVALID
    if mode != 0: assert mode == EXPLICIT_ASYNC              // kmgr/dlr.cpp:0x48C
    res = calloc(1, 0x38)                                    // kmgr_exec_resources_t (56 B)
    if !res: return NRT_RESOURCE                             // "Failed to allocate memory for execution resources"
    res->trace_exec_id = atomic_fetch_add(&next_trace_exec_id, 1)   // TU-static InterlockedExchangeAdd64
    res->trace_nc_idx  = mod->vcore->vtpb_idx
    res->model_id      = mod->interned_model_id              // +0x118
    res->exec_mode     = mode                                // +0x20 — selects fmap union arm
    nrt_sys_trace_new_event(/*type*/17, …)
    kbl_create_reference_feature_map_set(in,  &res->in_fmap_set)    // ref-bump (not copy)
    kbl_create_reference_feature_map_set(out, &res->out_fmap_set)   // "Failed to create reference of … fmap set"
    res->tdrv_resources = calloc(1, 0x20)                   // tdrv_compute_resources_t
    kbl_init_compute_resources(res->tdrv_resources, mod->sg_count)
    rc = kbl_compute_build_compute_resources(mod->vcore, mod->sg_count, mod->kelf_model->h_model,
                                             in_ref, out_ref, res->tdrv_resources)   // 0x306790 — IO BIND
    if rc: { kmgr_exec_resources_free(&res); return rc }    // "Failed to build compute resources …"

    // ---- first-exec collectives bootstrap (under the hw-exec-queue lock) ----
    kbl_get_model_ref_count(mod->vcore->tpbs, h_model, &kmod)
    kbl_acquire_hw_exec_queue_lock(kmod)
    if !kbl_cc_get_model_is_first_exec(kmod):               // not first → drain then build CC
        if async_exec_workers:
            kmgr_async_drain_queued_execs(mod->vcore->vtpb_idx)        // 0xdf7d0 — quiesce in-flight
        kbl_exec_build_and_load_cc_resources(kmod, mod->sg_count, res->trace_exec_id)
            // "Failed to build and load collectives resources for %s, exec_id %lu"
    kbl_release_hw_exec_queue_lock(kmod)
    kbl_model_ref_decrement(kmod)
    *out_res = res
    return NRT_SUCCESS
```

```c
void kmgr_exec_resources_free(kmgr_exec_resources_t** res_ptr):   // 0xdd350
    res = *res_ptr
    nrt_sys_trace_new_event(…)                              // close the exec-resource span
    kbl_free_compute_resources(res->tdrv_resources)         // per-TPB rings + tdrv_resources
    // per exec_mode, unmark/free the live fmap-set union arm:
    if res->exec_mode == SYNC: kbl_free_feature_map_set(res->out_fmap_set)   // sync owns in+out
                               kbl_free_feature_map_set(res->in_fmap_set)
    else:                      kbl_tensor_set_unmark_async_exec(res->in_fmap_set)  // async out-only arm
    free(res)
    *res_ptr = NULL
```

> **NOTE —** `kbl_create_reference_feature_map_set` takes a *reference* (a ref-bump), not a deep copy — that is why teardown calls `kbl_free_feature_map_set` (sync) or `kbl_tensor_set_unmark_async_exec` (async) rather than a destructor. The `exec_mode` recorded at `+0x20` is what selects which union arm of `kmgr_exec_resources_t` is live and therefore which unmark/free path runs. Picking the wrong arm leaks the fmap reference. (HIGH on the free-path branch existing; the exact union-arm-vs-mode mapping is HIGH for sync, MED for the explicit-async out-only arm — the producers are out of this TU.)

### Struct: `kmgr_exec_resources_t` (56 bytes, `0x38`, ord 17741)

`calloc`'d once per execution in `kmgr_exec_pre`, threaded to the submit funnel and the completion-wait.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `trace_exec_id` | `+0x00` | `uint64_t` | atomic-incremented TU-static trace correlation id |
| `trace_nc_idx` | `+0x08` | `uint32_t` | `= vcore->vtpb_idx` |
| `model_id` | `+0x10` | `uint64_t` | `= mod->interned_model_id` |
| `tdrv_resources` | `+0x18` | `tdrv_compute_resources_t*` | `calloc(0x20)` per-TPB ring resources |
| `exec_mode` | `+0x20` | `kmgr_exec_mode_t` | `ASYNC=0` / `EXPLICIT_ASYNC=1` (see §2 QUIRK) |
| `union{ sync{in,out}; async{out} }` | `+0x28` | `fmap_set*[2]` | the ref'd device feature maps; arm selected by `exec_mode` |

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `kmgr_exec_pre` | `0xdf820` | 1327 | validate IO; `calloc(0x38)`; build compute resources; first-exec CC | HIGH |
| `kmgr_exec_resources_free` | `0xdd350` | 438 | free `tdrv_resources` + per-mode fmap unmark/free; `free(res)` | HIGH |
| `dlr_check_valid_io_sets` | `0xe5b30` | — | tensor-io + same-vtpb placement check | HIGH |
| `kmgr_async_drain_queued_execs` | `0xdf7d0` | — | quiesce in-flight before first-exec CC build (async) | HIGH |
| `kbl_compute_build_compute_resources` | `0x306790` | — | per-TPB IO bind (→ [submit path §2](submit-path.md#2-bind-and-validate)) | HIGH |

---

## 4. The Unload State Machine

### Purpose

`kmgr_unload_nn` (`@0xdc450`) is the inverse of load: remove a model from the registry and tear down both its device staging and its userspace record, **safely** with respect to in-flight executions. It is reached from `nrt_unload` (single model) and `kmgr_unload_all_nns` (the `nrt_close` sweep). Its correctness rests on three guards and one spin: the model must be settled (`state == 4`), its execution requests must be drained, the map entry is erased under the write lock, and the function spins until `ref_count` reaches zero before it deletes anything. This is the highest-stakes facade path — a premature delete here is a use-after-free in a concurrent `kmgr_exec`.

### Entry Point

```text
nrt_unload ─┐
            ├─ kmgr_unload_nn (0xdc450)                     ── the unload state machine
nrt_close ──┴─ kmgr_unload_all_nns (0xddbc0)
                 └─ kmgr_get_nns (0xddab0)  ── snapshot H_NN[] under rdlock
                 └─ [per id] kmgr_unload_nn

kmgr_unload_nn:
  ├─ db_get_nn_ref_count (0xdc0c0) + nn_ref_decrement (0xdc110)   ── probe existence
  ├─ pthread_rwlock_wrlock(dlr_model_set.lock)                    ── erase under write lock
  │    └─ _Hashtable::find / erase
  ├─ usleep(1000) spin until ref_count == 0
  ├─ kmetric_unload_model_update_agg_neff_id (uuid-fold)
  ├─ [async] kmgr_async_exec_model_stop + kmgr_async_exec_poll
  │  [sync]  tpb_xu_get_by_vcore + tpb_xu_model_unstage
  └─ kmgr_unstage_kelf_model (0xdc180)                            ── dlr_kelf_unstage + delete mod
```

### Algorithm

```c
NRT_STATUS kmgr_unload_nn(H_NN h_nn):                        // 0xdc450
    nlog("Unloading %u")
    mod = db_get_nn_ref_count(h_nn)                          // probe; bumps ref
    if !mod: return /* "Failed to find model: %u" */ NRT_INVALID_HANDLE
    nn_ref_decrement(mod)                                    // drop the probe ref

    // ---- guard: model must be settled and not mid-execution ----
    if mod->ref_count > (async_drained ? N : 0):
        nlog("Execution requests on model %u have not been drained before unloading")
        // async mode tolerates the in-flight refs it is about to stop; sync requires 0
    if mod->state != 4:                                      // +0x1A8
        nlog("DLR %u state %u, must be stopped before unloading"); return NRT_INVALID

    // ---- erase from the registry under the WRITE lock ----
    pthread_rwlock_wrlock(&dlr_model_set.lock)
    it = dlr_model_set.models.find(h_nn.id)
    if it == end: { unlock; return /* "Failed to find model: %u" */ }
    dlr_model_set.models.erase(it)                           // open-coded bucket relink
    pthread_rwlock_unlock(&dlr_model_set.lock)
    // model is now unreachable by new db_get_nn_ref_count callers

    // ---- ref-drain spin: wait for in-flight executions to release ----
    while mod->ref_count != 0:                               // +0x1B0
        usleep(1000)                                         // 1 ms
        if ++iters == 1e8: nlog("still waiting for model[%u] ref count 0")   // warn, keep waiting

    kmetric_unload_model_update_agg_neff_id(device_id, fold64(mod->uuid))    // _mm_loadu uuid

    // ---- stop the model on its engine: sync vs async ----
    if async_exec_workers:
        kmgr_async_exec_model_stop(worker, mod, &need_stop)  // post MODEL_STOP to the worker
        kmgr_async_exec_poll(worker, handle)                 // wait it out
    else:
        xu = tpb_xu_get_by_vcore(mod->vcore)
        tpb_xu_model_unstage(xu, mod)                        // sync XU model-stop

    rc = kmgr_unstage_kelf_model(mod)                        // teardown + delete (below)
    if rc: nlog("Failed to unstage model: %u")
    else:  nlog("Unloaded NN: %u")
    return rc
```

`kmgr_unstage_kelf_model` (`@0xdc180`) is the teardown leaf, also called on the load-failure rollback path (`callers == {kmgr_load_nn_nc, kmgr_unload_nn, tpb_xu_step, …}`). It tolerates a NULL `kelf_model` ("kelf model not loaded. skipping" → SUCCESS), and otherwise: `dlr_kelf_unstage(mod->kelf_model)` (per-TPB `kbl_model_remove`), free the `replica_group_info` vectors, delete the NDS handle objects (`nds_obj_delete` × `nds_model_node_count`), destroy the `input_info`/`output_info` RB-trees, and finally `operator delete(mod, 0x1E0)` — the delete-size that proves `dlr_model` is 480 bytes.

> **GOTCHA —** the order is **erase-then-drain**, not drain-then-erase. The write lock is dropped *before* the `ref_count` spin, so the spin runs unlocked. This is correct precisely because the erase already removed the map entry: no *new* `db_get_nn_ref_count` can find the model and take a ref, so the count is monotone-decreasing during the spin and the loop terminates. A reimplementation that holds the write lock across the spin will deadlock — an in-flight `kmgr_exec` releasing its ref via `nn_ref_decrement` does not take the lock, but any other registry operation would block behind the held writer for the entire drain. Drop the lock, then spin on the atomic.

> **QUIRK —** the ref-drain spin warns but never gives up: at `1e8` iterations (≈ 100 s at 1 ms/iter) it logs `"still waiting for model[%u] ref count 0"` and keeps spinning. There is no timeout and no forced teardown. The design assumes in-flight executions *will* complete and release their ref; a wedged execution wedges unload forever rather than risk freeing a model a live execution still reads. A reimplementer who adds a timeout-and-force-free here reintroduces exactly the use-after-free the spin exists to prevent. (HIGH — the `usleep(1000)` loop and the `1e8`-iteration warn are code-confirmed via the `usleep` callee and the warn string.)

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `kmgr_unload_nn` | `0xdc450` | 1069 | guard; wrlock erase; ref-drain spin; sync/async stop; unstage | HIGH |
| `kmgr_unstage_kelf_model` | `0xdc180` | 707 | `dlr_kelf_unstage`; free vectors/NDS/RB-trees; `delete mod` | HIGH |
| `kmgr_get_nns` | `0xddab0` | 266 | rdlock; `calloc(4·count)`; copy all `H_NN.id` | HIGH |
| `kmgr_unload_all_nns` | `0xddbc0` | 166 | `kmgr_get_nns` then loop `kmgr_unload_nn`; from `nrt_close` | HIGH |
| `tpb_xu_model_unstage` | — | — | sync XU model-stop (→ [submit path](submit-path.md)) | HIGH |
| `kmgr_async_exec_model_stop` | — | — | async worker MODEL_STOP (→ [xu-workers](xu-workers.md)) | HIGH |

---

## 5. I/O Tensor Metadata Accessors

### Purpose

The four metadata accessors read tensor shape/dtype/name out of the `dlr_model`'s `input_info` / `output_info` maps without touching any engine — they are pure registry-and-`std::map` reads behind the standard refcount protocol. They back `nrt_get_model_tensor_info`, `nrt_execute_repeat`'s ifmap-count validation, and tensor-name validity checks. They share one shape: `db_get_nn_ref_count` → traverse an `IO_MAP` → `nn_ref_decrement`.

### Algorithm

```c
NRT_STATUS kmgr_get_io_tensor_count(H_NN h_nn, u32* in_count, u32* out_count):   // 0xdc880
    mod = db_get_nn_ref_count(h_nn); if !mod: return /* "Failed to find model: %u" */
    *in_count  = mod->input_info._M_t._M_node_count          // std::map size
    *out_count = mod->output_info._M_t._M_node_count
    nn_ref_decrement(mod); return NRT_SUCCESS

NRT_STATUS kmgr_get_io_tensor_info(H_NN h_nn, bool is_input, u32 index, …out…):  // 0xdc900
    mod = db_get_nn_ref_count(h_nn); if !mod: return …
    map  = is_input ? &mod->input_info : &mod->output_info
    node = map.begin(); repeat index times: node = _Rb_tree_increment(node)      // O(index) walk
    val  = (io_node_info_t*)(node + 0x40)                   // value at node+0x40
    strncpy(out_name, *(string*)(node + 0x20), 255)         // RB key = tensor name
    *out_size = val->size; *out_dtype = val->dtype; *out_ndim = val->ndim
    out_shape = malloc(4 * val->ndim); memcpy(out_shape, val->shape, 4 * val->ndim)
    nn_ref_decrement(mod); return NRT_SUCCESS

NRT_STATUS kmgr_is_valid_ifmap(H_NN h_nn, const char* name, bool* valid):        // 0xdd680
    mod = db_get_nn_ref_count(h_nn); if !mod: return …
    if !name: __throw_logic_error(...)                      // → kmgr_is_valid_ifmap.cold @0x459e6
    *valid = (mod->input_info.find(string(name)) != end)
    nn_ref_decrement(mod); return NRT_SUCCESS
```

`kmgr_get_ifmap_count` (`@0xdd650`, 47 B) is the trivial input-only variant (`*count = input_info._M_node_count`), called by `nrt_execute_repeat` to validate the supplied input count before dispatch.

### Struct: `io_node_info_t` (144 bytes, ord 2948)

The value type of the `IO_MAP` (`std::map<string, io_node_info_t>`, 48 B, ord 13262). Offsets below are relative to the value; in the RB node the value sits at `node+0x40`, the key `std::string` at `node+0x20`.

| Field | Offset (value) | Offset (RB node) | Type | Meaning |
|---|---|---|---|---|
| `size` | `+0x00` | `node+0x40` | `size_t` | tensor byte size |
| `dtype` | `+0x08` | `node+0x48` | `nrt_dtype_t` | element type |
| `shape` | `+0x0c` | `node+0x4c` | `uint32_t[32]` | dims; first `ndim` valid |
| `ndim` | `+0x8c` | `node+0xcc` | `uint32_t` | dimension count |

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `kmgr_get_io_tensor_count` | `0xdc880` | 122 | `*in/out = input/output_info._M_node_count` | HIGH |
| `kmgr_get_io_tensor_info` | `0xdc900` | 366 | RB-walk to index-th node; copy name/size/dtype/shape/ndim | HIGH |
| `kmgr_get_ifmap_count` | `0xdd650` | 47 | input-only count for `nrt_execute_repeat` | HIGH |
| `kmgr_is_valid_ifmap` | `0xdd680` | 406 | `input_info.find(name)`; NULL name → `.cold` throw | HIGH |

---

## 6. The Stage Seam (`dlr_kelf_stage`) and the Explicit-Async Submit

### Purpose

Two facade functions sit at module boundaries the rest of this page only references. `dlr_kelf_stage` (`@0xe0970`) is the KMGR↔KBL **stage seam** invoked once from `kmgr_load_nn_nc` — the point where a parsed `kelf_nn_t` of compiler subgraphs becomes the device-side records: a `dlr_kelf_model_t` bridge (`+0x1A0` of the `dlr_model`) and one `model_t` per physical core. `kmgr_tpb_xu_schedule` (`@0xe01d0`) is the explicit-async submit analogue of §2, driving the public `nrta_execute_schedule` path. Both are documented here only as seams; their interiors belong to [the load pipeline](../neff/load-pipeline.md) and [the submit path](submit-path.md).

### Algorithm — the stage seam

```c
NRT_STATUS dlr_kelf_stage(const kelf_nn_t* kg, const virtual_core_t* vcore,
                          const kbl_model_param_t* param, H_MODEL h_model,
                          const char* name, uint8_t* uuid, dlr_kelf_model_t** out):   // 0xe0970
    core_size = vtpb_get_vtpb_core_size(vcore)
    km = calloc(1, 0x48)                                    // dlr_kelf_model_t (72 B, ord 17521)
    copy param/uuid into km; km->sg_count = kg->nkbin
    if vcore->num_tpbs < kg->nkbin:                         // "Insufficient number of pncs, required %u, available %u"
        free(km); return NRT_INVALID
    assert kg->nkbin == 1 || kg->nkbin == core_size         // kmgr/dlr_kelf.cpp:0xA7
    shared = vtpb_info_shared_alloc(...)
    vtpb_info_shared_init_vtpb(shared, …)                   // runs the mem_ref placement plan

    if kg->nkbin == 1:                                      // single-core: inline
        rc = dlr_kelf_stage_model_add(kg->kbin[0], vcore, /*pcore*/0, param, h_model, name, uuid, shared)
    else:                                                   // multi-core (LNC): pthread-per-subgraph fan-out
        rc = dlr_kelf_stage_multi_tpb_model_add(kg, vcore, param, h_model, name, uuid, shared)
            // one pthread per kbin (≤256); join all; roll back kbl_model_remove on any failure

    vtpb_info_shared_free(shared)
    if rc == 0: { km->h_model = h_model; *out = km; }       // attach the bridge
    else        { free(km); }
    return rc
```

The bridge `km` is what joins the two id spaces: keyed in by the `dlr_model` on the `H_NN` side (`dlr_model.kelf_model = km`), it carries the `H_MODEL` (`+0x00`) that every per-core `model_t` is found by on the device side. There is no direct `H_NN → model_t` map; the chain is `h_nn → dlr_model → kelf_model → h_model → model_t`. The single-vs-multi dispatch is the only branch in this function, confirmed by its callee set (`dlr_kelf_stage_model_add` and `dlr_kelf_stage_multi_tpb_model_add` both present). The full per-core build, the device boundary (first `IOCTL = MEM_ALLOC_V2MT64`), and the relocation belong to [the load pipeline §Stage 3+4](../neff/load-pipeline.md#stage-34--kbl-build-and-device-dram-stage).

### Struct: `dlr_kelf_model_t` (72 bytes, `0x48`, ord 17521)

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `h_model` | `+0x00` | `H_MODEL` `{uint32}` | device-side per-core key; the join to `model_t` |
| `vcore` | `+0x08` | `const virtual_core_t*` | bound logical core |
| `sg_count` | `+0x10` | `uint32_t` | `= kg->nkbin`; loop count for `dlr_kelf_unstage` |
| `uuid` | `+0x14` | `uint8_t[16]` | model uuid |
| `params` | `+0x28` | `kbl_model_param_t` (32 B) | load/exec parameters (timeout, scratchpad, …) |

### Algorithm — explicit-async submit

```c
NRT_STATUS kmgr_tpb_xu_schedule(const virtual_core_t* vcore, H_NN h_nn,
                                in_set, out_set, NRT_STATUS* exec_ret, nrta_seq_t* seq):  // 0xe01d0
    xu  = tpb_xu_get_by_vcore(vcore)
    mod = db_get_nn_ref_count(h_nn); if !mod: return /* "Failed to find model: %u" */
    assert vcore == mod->vcore                              // kmgr/dlr.cpp:0x527
    nrt_sys_trace_new_event(/*type*/22, …)
    if any host-malloc tensor: { nn_ref_decrement(mod); return NRT_INVALID }   // "Async APIs only supports device …"
    rc = kmgr_exec_pre(mod, in_set, out_set, /*mode*/EXPLICIT_ASYNC=1, &resources)   // §3
    if rc == 0:
        rc = tpb_xu_schedule_exec(xu, /*repeat*/0, h_nn, resources, trace_id, exec_ret, seq)  // → submit path
    nn_ref_decrement(mod)
    return rc
```

This is the explicit (caller-managed) async path reached from `nrta_execute_schedule`, distinct from the *implicit* async of §2: it always passes `EXPLICIT_ASYNC`, rejects host tensors outright, and returns a sequence id the caller later polls. The completion side (`nrta_is_completed` → `kmgr_xu_get_compl_seq`, `nrta_cc_prepare` → `kmgr_xu_get_comp_efd`) is owned by [the submit path](submit-path.md#6-publish-and-the-submit-boundary) and the NRT_3.0.0 async API.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `dlr_kelf_stage` | `0xe0970` | 652 | KMGR↔KBL seam; `calloc(0x48)` bridge; single/multi dispatch | HIGH |
| `kmgr_tpb_xu_schedule` | `0xe01d0` | 546 | explicit-async submit; `EXPLICIT_ASYNC`; host-tensor reject | HIGH |
| `dlr_kelf_stage_model_add` | `0xe0730` | — | single-subgraph leaf → `kbl_model_add(&tpbs[i], …)` | HIGH |
| `dlr_kelf_stage_multi_tpb_model_add` | `0xe0580` | — | LNC fan-out: pthread-per-kbin, join, rollback | HIGH |

---

## 7. Reimplementation Checklist

A KMGR-facade reimplementation is correct when it reproduces, in order:

1. **Registry** — a `H_NN.id → dlr_model*` map under a rwlock plus a monotone `last_nn_id`; resolution always under the read lock with a refcount bump.
2. **Refcount protocol** — `db_get_nn_ref_count` returns NULL on miss and otherwise `_InterlockedAdd64(ref_count,+1)`; `nn_ref_decrement` asserts `>0` then `_InterlockedSub64(-1)` with no lock. Every accessor brackets its work in this pair.
3. **Execute fork** — branch on the global `async_exec_workers`; sync clones host tensors to HBM and copies results back, async rejects host tensors (`NRT_INVALID`); pass the *inverted* exec-mode (`workers==NULL ? 1 : 0`) into `kmgr_exec_pre`.
4. **Resource lifecycle** — `kmgr_exec_pre` `calloc(0x38)` the tracker, ref-bump the fmaps, build per-TPB compute resources, first-exec CC bootstrap under the hw-exec-queue lock; `kmgr_exec_resources_free` frees `tdrv_resources` and the mode-selected fmap arm; every `kmgr_exec` error edge frees exactly its clones + tracker.
5. **Unload** — guard `state==4`; **erase under the write lock, drop the lock, then spin** on `ref_count==0` with `usleep(1000)` (warn at `1e8`, never time out); sync `tpb_xu_model_unstage` vs async `kmgr_async_exec_model_stop`; then `kmgr_unstage_kelf_model` → `dlr_kelf_unstage` + `operator delete(mod,0x1E0)`.
6. **Metadata** — shape/dtype/name reads are `db_get_nn_ref_count` → `IO_MAP` traversal (`_M_node_count`, RB-walk, `find`) → `nn_ref_decrement`; NULL tensor name throws.
7. **Stage seam** — `dlr_kelf_stage` `calloc(0x48)` the `dlr_kelf_model_t` bridge, require `vcore->num_tpbs ≥ kg->nkbin`, dispatch single-core inline vs multi-core pthread-fan-out, attach `h_model` on success and free the bridge on failure.

> **GOTCHA —** the facade fires **no `IOCTL` and touches no descriptor ring**. The doorbell, the `device_exec_request_t`, and the lock-free `xu_queue` are below the `kmgr_sync_exec` / `kmgr_async_exec_add_work` seams; the NQ harvest and `mark_comp_efd` are below `kmgr_exec_wait`; the device `MEM_ALLOC` is below `dlr_kelf_stage → kbl_model_add`. A reimplementer who folds any of that into the facade has mis-drawn the layer boundary — kmgr is pure orchestration: registry mutation, refcount, dispatch, and resource lifecycle. Everything device-facing is one function call away, behind a named seam.

---

## Related Components

| Component | Relationship |
|---|---|
| [The Submit Path](submit-path.md) | the sync engine below `kmgr_sync_exec`; the doorbell + `xu_queue` this facade dispatches to |
| [The Completion Engine](completion-engine.md) | `kmgr_exec_wait` drives `exec_wait_round_robin`; the NQ harvest below the facade |
| [The XU Work-Queue and Worker Threads](xu-workers.md) | the async engine below `kmgr_async_exec_add_work` / `kmgr_async_exec_model_stop` |
| [The Load Pipeline](../neff/load-pipeline.md) | builds the `dlr_model` this facade registers; owns `dlr_kelf_stage`'s interior |
| [Public C API: Lifecycle and Init/Teardown](../runtime/api-lifecycle.md) | the `nrt_*` callers (`nrt_execute`, `nrt_unload`, `nrt_close`) above the facade |

## Cross-References

- [The Submit Path (Bind → Stage → Doorbell)](submit-path.md) — the sync engine `kmgr_sync_exec` enters; the `async_exec_workers` global is the shared fork
- [The Completion Engine (NQ Harvest & Error Decode)](completion-engine.md) — `kmgr_exec_wait`, the completion-wait the facade exposes to the async worker
- [The XU Work-Queue and Worker Threads](xu-workers.md) — `kmgr_async_exec_add_work` / `_model_stop` / `_poll`, the implicit-async lower half
- [The Load Pipeline (Parse → Build → Stage → Relocate)](../neff/load-pipeline.md) — `kmgr_load_nn_nc` and the `dlr_kelf_stage` seam this page references but does not re-derive
- [Public C API: Lifecycle and Init/Teardown](../runtime/api-lifecycle.md) — the `nrt_execute` / `nrt_unload` / `nrt_close` entry points that funnel into this facade
