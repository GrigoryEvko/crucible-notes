# Public C API: Tensor Surface and I/O

> *All addresses, offsets, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`; build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce917b633a70eb3d0bc460f34ac3da620`). The ELF is **not** stripped and retains DWARF — function names, struct field names, and enum constants survive. `.text` VMA equals file offset; `.data`/`.rodata` are identity-mapped too (`p_offset == p_vaddr`), so every `0x…` is an analysis VMA. Export version is `@@NRT_2.0.0` unless noted; provenance strings root every body at `/opt/workspace/KaenaRuntime/{nrt,tdrv}/`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — entry addresses and export tags from `native_exports.json`, worker targets from the IDA call graph, the wrapper template from the shared decompile shape across all entries, guard semantics from `nrt_init_state` dispatch. · Part IV — Runtime Core · [back to index](../index.md)*

## Abstract

This page documents the **public tensor C-API surface** of `libnrt.so` — the ~25 `nrt_tensor_*` / `nrt_*_tensor_set` / `nrt_get_model_tensor_info` entries that a framework integrator links against to allocate device buffers, move bytes host↔device, slice and reference tensors, export DMA-buf file descriptors, group tensors into named sets, and introspect a loaded model's I/O. It is the *surface*, not the engine: almost every entry here is a thin stable ABI wrapper whose only original work is argument validation, lifecycle gating, and error logging, after which it forwards to an internal `tensor_*` (or `kbl_*` / `kmgr_*` / `dmem_*`) worker in the TDRV layer. The object model those workers operate on — the 192-byte `nrt_tensor_t` view over the 320-byte `nrt_tensor_storage_t` backing, the two refcounts, the tagged union, the byte-untyped invariant, the async fence plane — is **owned by [tdrv-tensor](tdrv-tensor.md)** and is not re-derived here; this page links to it and treats the workers as named boundaries.

The familiar reference frame is a hand-written stable C ABI laid over a versioned internal C++ core — the same pattern as CUDA's `cuda*` driver entries delegating to internal `cu*` implementations, or `libc`'s public symbols wrapping `__`-prefixed internals. Every entry is generated from one **API-shell template**: initialize per-thread log coalescing, build a heap `std::string` of the API name into a stack `NlogErrorContextManager` (an RAII error-context whose SSO/`_M_create` inline-expansion is the *bulk of each function's byte size and is pure boilerplate*), dispatch on the global four-state `nrt_init_state`, call the worker, and on the way out flush a `"Generic API Failure"` log if any coalesced log went unflushed. Because the template is uniform, this page shows it **once** as annotated C pseudocode and then collapses the entire surface into a single **Function Map** table that pins each public entry to `symbol+addr`, its worker target, and a confidence grade — the dispatch-dimension approach for a space whose rows are mechanically similar.

What remains after the template is the genuinely-distinct minority, and that is where the page spends its depth: `nrt_tensor_allocate_slice` (a two-worker compose: make-empty then set-slice), `nrt_get_dmabuf_fd` (gated *off* under P2P/sim, the EFA peer-direct export path), the output-completion `check`/`reset` pair (which **bypass the lifecycle guard** and operate on a process-global lock/cond plane), the tensor-set hashtable family, and `nrt_get_model_tensor_info` (the one path that surfaces `dtype`/`shape` — data that does **not** live on the tensor object). A handful of true exceptions to the template (`nrt_memcpy_to_device`, `nrt_tensor_get_size`/`_get_va`, `nrt_set_pool_eng_ucode`'s *inverse* guard) are called out explicitly.

For reimplementation, the contract is:

- **The API-shell template** — the fixed five-step wrapper (log-init → name-string into `NlogErrorContextManager` → four-way `nrt_init_state` dispatch → worker call → infodump+`"Generic API Failure"` on error) that every state-gated entry is an instance of.
- **The four-way lifecycle gate** — `INIT(1)`→work, `START(0)`→`NRT_UNINITIALIZED(13)`, `CLOSED(3)`→`NRT_CLOSED(14)`, `CHILD(2)`→`NRT_FAILURE(1)`, with the exact strings and status codes; shared with every other public entry (see [api-lifecycle](api-lifecycle.md)).
- **The entry→worker map** — which `tensor_*`/`kbl_*`/`kmgr_*`/`dmem_*` body each public symbol forwards to, so the wrapper layer can be regenerated mechanically once the workers exist.
- **The template exceptions** — the entries that are *not* state-gated (`check`/`reset_output_completion`), are *inverse*-gated (`set_pool_eng_ucode`), or do no worker call at all (`memcpy_to_device`, `get_size`).
- **The error-context RAII** — the `NlogErrorContextManager(name)` whose scope-exit emits `"Generic API Failure"`, and the `nrt_infodump` + `nrt_core_dump` calls on a nonzero worker status.

| | |
|---|---|
| **Surface** | ~25 entries across `0xbde50–0xc193b` (+ outliers); exports `@@NRT_2.0.0` |
| **Wrapper template** | log-init → name-string → 4-way guard → worker → infodump/`"Generic API Failure"` |
| **State word** | `nrt_init_state` `@0xc5d1a0` (`.bss`, `NRT_INIT_STATE` enum) |
| **Guard codes** | `INIT→work` · `START→13` · `CLOSED→14` · `CHILD/other→1` |
| **Error-context** | stack `NlogErrorContextManager(api_name)` → RAII `"Generic API Failure"` |
| **Object model** | `nrt_tensor_t` (192 B) / `nrt_tensor_storage_t` (320 B) — owned by [tdrv-tensor](tdrv-tensor.md) |
| **Worker bands** | `tensor_*` `0x30e1d0–0x310418` · `kbl_*`/`ht_*` · `kmgr_get_io_tensor_*` · `dmem_*` |
| **Source** | `nrt/{nrt_async,nrt_vnc_usage}.cpp`, `tdrv/tensor.c`, `inc/tdrv/tensor.h` |

---

## 1. The API-Shell Wrapper Template

### Purpose

Every state-gated tensor entry is a syntactic instance of one template. The template does no tensor work itself — it sets up the per-thread logging context, names the call for error attribution, gates on the runtime lifecycle, calls exactly one internal worker, and tears down the logging context (emitting a failure breadcrumb if the worker left logs unflushed). A reimplementer who writes this shell *once* as a macro or code-generator, then fills in the per-entry worker call and the per-entry "Failed to …" string, reproduces the whole surface. The 700–1600 byte sizes in the [Function Map](#function-map) are almost entirely the inlined `std::string` build of the API name, not logic.

### Algorithm

```c
// THE API-SHELL TEMPLATE — modeled from the shared decompile shape of every
// state-gated nrt_tensor_* / nrt_*_tensor_set entry. Shown ONCE; the Function
// Map below names the per-entry <worker> and <"Failed to …"> string that vary.
// State word: nrt_init_state @0xc5d1a0 (NRT_INIT_STATE). Returns NRT_STATUS.
NRT_STATUS nrt_<entry>(/* api args */):
    nlog_coalescing_init_thread()                       // 0x224ae0 — per-thread log buffer

    // (1) RAII error-context: a stack NlogErrorContextManager carrying a heap
    //     std::string of the API name. The SSO/_M_create dance here is the bulk
    //     of the function's bytes and is pure boilerplate (string::_M_create 0x3d8a0).
    NlogErrorContextManager ctx("nrt_<entry>")          // name → "Generic API Failure" tagging

    // (2) FOUR-WAY LIFECYCLE GATE — identical to api-lifecycle.md §3.
    switch nrt_init_state:                               // @0xc5d1a0
      case NRT_STATE_INIT:                               // 1 — the only working state
          status = <worker>(/* forwarded args */)        // the one real call (see Function Map)
          if status != NRT_SUCCESS:
              vtpb = resolve_vtpb(/* tensor or -1 */)    // tensor->sto->vtpb_idx (+0x108) if available
              nrt_infodump(ERROR, vtpb, status, "nrt_<entry>()")   // 0x94030
              nrt_core_dump(/* err ctx */)                          // 0x92b90 — fork/exec neuron-dump
              nlog_write("Failed to <verb> nrt tensor %s", name)    // per-entry string
          break
      case NRT_STATE_START:                              // 0 — never inited
          nlog_write("NRT uninitialized");        status = NRT_UNINITIALIZED   // 13
          break
      case NRT_STATE_CLOSED:                             // 3 — after nrt_close
          nlog_write("NRT already closed");       status = NRT_CLOSED          // 14
          break
      default:                                           // CHILD(2) and any other
          nlog_write("Incompatible runtime state: %s", nrt_state_get_string()) // 0xb9060
          status = NRT_FAILURE                            // 1
    // (3) ctx RAII scope-exit: destroy the api-name std::string; if
    //     nlog_has_unflushed_logs() (0x2250d0) → nlog_write("Generic API Failure")
    return status
```

Three structural facts a reimplementer must keep:

- **The gate is on the global word, not on any argument.** A NULL tensor or a bad offset is the *worker's* problem; the shell only decides whether the runtime is in a state where the worker may run at all. The codes (`13`/`14`/`1`) and strings are the same ones [api-lifecycle §3](api-lifecycle.md) documents for load/unload/execute — this is one guard machine shared across the whole public ABI.
- **The error tail is three calls, in order.** On a nonzero worker status the shell runs `nrt_infodump` (one-shot support bundle, gated on improving severity), then `nrt_core_dump` (forks `/opt/aws/neuron/bin/neuron-dump` if enabled — see [api-lifecycle §5](api-lifecycle.md)), then the per-entry `nlog_write`. The `vtpb` passed to `infodump`/`core_dump` is resolved from `tensor->sto->vtpb_idx` (`+0x108`) when a tensor is in hand, else `-1`.
- **`NlogErrorContextManager` is RAII.** The `"Generic API Failure"` line is emitted by the *destructor* if `nlog_has_unflushed_logs()` is true at scope exit — it is a catch-all breadcrumb, not a per-branch log. The name string passed to the constructor is what tags every coalesced log line for this call.

> **NOTE —** the "name string" each entry builds (e.g. `"nrt_tensor_copy"`, `"nrt_get_tensor_from_tensor_set"`) is assembled inline from `.rodata` fragments (`xmmword_8508xx` + a tail qword) rather than a single string literal, which is why the decompile shows a large SSO/`_M_create` block per function. This is compiler string-builder output, not semantically interesting; treat it as the constant API name.

> **GOTCHA —** the four-way gate's `default` arm catches `NRT_STATE_CHILD(2)` — a process that `fork()`ed after `nrt_init`. It returns `NRT_FAILURE(1)` with `"Incompatible runtime state: NRT_STATE_CHILD"`, **not** a tensor-specific error. A reimplementer who collapses the gate to a boolean "is initialized?" will both lose the distinct `13`/`14` codes and silently let a forked child proceed. The child state is terminal (no API clears it); see [api-lifecycle](api-lifecycle.md).

### Function Map

Every row below is an instance of the template above except where the Notes column flags a deviation. `Entry` is the public `@@NRT_2.0.0` symbol; `Addr` is the body VMA (the `0x3c…`/`0x3d…` `.name` forwarding thunks are internal aliases, not the export targets); `Worker` is the single internal body it forwards to; `Cell` names the analysis source.

| Entry (`nrt_*`) | Addr | Worker target | Cell | Confidence |
|---|---|---|---|---|
| `tensor_allocate` | `0xbc320` | `tensor_allocate` `0x30e8a0` | L-API-08 | HIGH |
| `tensor_allocate_empty` | `0xbc910` | `tensor_allocate_empty` `0x30e310` | L-API-08 | HIGH |
| `tensor_attach_buffer` | `0xbcd00` | `tensor_set_to_user_buffer` `0x30e3d0` | L-API-08 | HIGH |
| `tensor_free` | `0xbd0e0` | `tensor_free` `0x30e630` | L-API-08 | HIGH |
| `tensor_read` | `0xbd280` | `tensor_read` `0x30ed40` (+ ntrace IO_COPY) | L-API-08 | HIGH |
| `tensor_write` | `0xbd570` | `tensor_write` `0x30efb0` (+ ntrace IO_COPY) | L-API-08 | HIGH |
| `tensor_read_batch` | `0xbd9e0` | `tensor_read_batch` `0x30f220` (+ ntrace) | L-API-08 | HIGH |
| `tensor_write_batch` | `0xbde50` | `tensor_write_batch` `0x30f3a0` (+ ntrace) | L-API-09 | HIGH |
| `tensor_copy` | `0xbe2c0` | `tensor_copy` `0x30f6c0`; err resolves `dst→vtpb` | L-API-09 | HIGH |
| `tensor_memset` | `0xbe590` | `tensor_memset` `0x30f520`; err resolves `t→vtpb` | L-API-09 | HIGH |
| `tensor_get_size` | `0xbe9d0` | *(inline)* returns `tensor->_size` (+0x18) | L-API-09 | HIGH |
| `allocate_tensor_set` | `0xbeae0` | `kbl_init_feature_map_set` | L-API-09 | HIGH |
| `destroy_tensor_set` | `0xbeea0` | `kbl_free_feature_map_set` (`is_init` probe) | L-API-09 | HIGH |
| `add_tensor_to_tensor_set` | `0xbf1b0` | `kbl_add_fmap_to_set` | L-API-09 | HIGH |
| `get_tensor_from_tensor_set` | `0xbf580` | `ht_name_find` `0x268000` → `node[-1].next` | L-API-03/09 | HIGH |
| `tensor_allocate_slice` | `0xbf980` | `tensor_allocate_empty` + `tensor_set_slice` `0x30e530` | L-API-03/09 | HIGH |
| `tensor_get_va` | `0xbfdb0` | `tensor_get_va` `0x30f9c0` (`is_init` probe) | L-API-03/09 | HIGH |
| `get_model_tensor_info` | `0xc0090` | `kmgr_get_io_tensor_count` + `kmgr_get_io_tensor_info` | L-API-09 | HIGH |
| `free_model_tensor_info` | `0xc0390` | *(inline)* per-entry `free(shape)` + `free(array)` | L-API-09 | HIGH |
| `memcpy_to_device` | `0xc04b0` | *(no worker)* `memcpy` + `al_data_memory_barrier` | L-API-09 | HIGH |
| `get_dmabuf_fd` | `0xc0500` | `dmem_get_dmabuf_fd` `0x229bb0` (P2P/sim-gated) | L-API-03/09 | HIGH |
| `tensor_get_device_allocation_info` | `0xc08e0` | `tensor_get_device_allocation_info` `0x30fe60` | L-API-03/09 | HIGH |
| `tensor_check_output_completion` | `0xc0cc0` | *(no state gate)* global lock/cond poll | L-API-03/09 | HIGH |
| `tensor_reset_output_completion` | `0xc12f0` | *(no state gate)* global lock; `t[+0x80]=0` | L-API-09 | HIGH |
| `set_pool_eng_ucode` | `0xc1630` | `tdrv_set_pool_eng_ucode` (*inverse* gate: `START` only) | L-API-09 | HIGH |

> **NOTE —** `nrt_tensor_read`/`_write`/`_read_batch`/`_write_batch` add one thing to the template: they bracket the worker call with `ntrace_record_event(IO_COPY_START)` … `(IO_COPY_END)` (`0x300710`) so the I/O shows up in the Neuron trace stream. This is additive — the gate, the worker call, and the error tail are unchanged.

### Template exceptions

Five entries are *not* faithful template instances. A reimplementer must special-case each:

- **`nrt_tensor_get_size` (`0xbe9d0`) and `nrt_tensor_get_va` (`0xbfdb0`)** read a single field with no worker error path. `get_size` returns `tensor->_size` (+0x18) inline and `__assert_fail`s on a NULL tensor (`tensor.h:0xC8`); it does **not** consult `nrt_init_state` at all. `get_va` does gate, but on the *lighter* `nrt_state_is_init()` probe (`0xb9080`) — on non-`INIT` it logs `"Unexpected runtime state: %s"` and returns `NULL` (the return type is `volatile void*`, not `NRT_STATUS`).
- **`nrt_memcpy_to_device` (`0xc04b0`, 66 bytes)** is not a wrapper and touches no tensor object: it is a plain host `memcpy(dst, src, n)` followed by `al_data_memory_barrier()`, skipped entirely under `gconf->funtime` (simulation), and **always returns `NRT_SUCCESS`** with no state check.
- **`nrt_set_pool_eng_ucode` (`0xc1630`)** has the *inverse* gate: it does work **only** in `NRT_STATE_START(0)` (pre-init), calling `tdrv_set_pool_eng_ucode(ucode_info)`; any `INIT`/`CLOSED` state is rejected. It is a configuration hook that must run before bring-up.
- **`nrt_tensor_check_output_completion` (`0xc0cc0`) and `nrt_tensor_reset_output_completion` (`0xc12f0`)** bypass the lifecycle gate entirely — see [§3](#3-the-output-completion-pair).

---

## 2. Why the Surface Is This Shape

The split between this page and [tdrv-tensor](tdrv-tensor.md) is the deliberate boundary between a *stable ABI* and a *versioned core*. The public symbols carry `@@NRT_2.0.0` version tags and must not change signature across runtime releases; the `tensor_*` workers in `0x30e1d0–0x310418` are internal C++ that the runtime is free to reshuffle. The template is what makes the two layers cleave cleanly: the shell owns the cross-cutting concerns (logging context, lifecycle policy, error reporting, ABI versioning) so the worker can be a focused, untagged C function. This is the same factoring as a CUDA driver `cuMemAlloc` over an internal allocator, or `pthread_create` over `__pthread_create_2_1` — the public name is a thin, stable, instrumented façade.

The instrumentation is uniform on purpose. Because every entry builds a `NlogErrorContextManager(name)` and emits `"Generic API Failure"` on unflushed logs, a support bundle pulled after *any* failed tensor call carries a consistent breadcrumb naming the exact API entry, and `nrt_infodump`/`nrt_core_dump` fire from one place rather than being scattered through the workers. A reimplementer who pushes the logging into the workers instead will both duplicate it across every `tensor_*` body and lose the single point where the API name is known. The cost — the inlined `std::string` build that dominates each function's byte size — is paid for the uniformity, not for any tensor logic.

The handful of exceptions exist where the uniform policy would be wrong. The output-completion pair ([§3](#3-the-output-completion-pair)) must work *after* `nrt_close` set the state away from `INIT`, because a host polling for the device to finish writing an output is a teardown-adjacent operation; gating it on `INIT` would deadlock the drain. `set_pool_eng_ucode` must run *before* `INIT` because it configures pool-engine microcode consumed during bring-up. `memcpy_to_device` is a raw helper with no tensor object to gate on. Each deviation is a place where the template's assumption ("the runtime is `INIT` and we are about to touch a tensor") does not hold.

---

## 3. The Output-Completion Pair

### Purpose

`nrt_tensor_check_output_completion` and `nrt_tensor_reset_output_completion` are the host's window onto async-execution progress: after submitting work that writes a model output tensor, the host calls `check` to block until the device has completed that write *N* times, and `reset` to zero the counter for the next round. They are the two entries that **do not consult `nrt_init_state`** — they validate the tensor pointer directly and operate on `tensor->output_completion_count` (`+0x80`) under a **process-global** lock/cond pair (not the per-storage fence; see the distinction in [tdrv-tensor §5](tdrv-tensor.md)).

### Algorithm

```c
// nrt_tensor_check_output_completion @0xc0cc0 (1570 B). NOT lifecycle-gated.
// a2: timeout in microseconds; a2 < 0 ⇒ wait forever. a3: expected count.
NRT_STATUS nrt_tensor_check_output_completion(tensor, timeout_us, expected):
    if tensor == NULL:
        nlog_write("The given tensor is null. Please provide valid function arguments.")
        return NRT_INVALID                              // 2
    cond = tensor_get_output_completion_cond()          // 0x310400 → global cond 0xca72c0
    lock = tensor_get_output_completion_lock()          // 0x310410 → global lock 0xca7280
    clock_gettime(deadline if timeout_us >= 0)
    pthread_mutex_lock(lock)
    while tensor->output_completion_count < expected:   // +0x80, read under lock
        if timeout_us < 0:
            pthread_cond_wait(cond, lock)               // infinite
        else:
            rc = pthread_cond_timedwait(cond, lock, deadline)
            if rc == ETIMEDOUT:
                nlog_write("The output tensor (… name=%s) does not reach the expected "
                           "completion count %lu until timeout. …")
                pthread_mutex_unlock(lock); return NRT_TIMEOUT       // 5
        if 30 s elapsed since last heartbeat:           // 30000000 µs
            nlog_write("Waiting on the completion count … Already waited for %d microseconds.")
    nlog_write("The expected tensor completion count is reached. … current=%lu expected=%lu")
    pthread_mutex_unlock(lock)
    return NRT_SUCCESS                                   // 0   (or NRT_FAILURE=1 on lock misuse)

// nrt_tensor_reset_output_completion @0xc12f0 (824 B). NOT lifecycle-gated.
NRT_STATUS nrt_tensor_reset_output_completion(tensor):
    if tensor == NULL:
        nlog_write("The given tensor is null. Please provide a valid function argument.")
        return NRT_INVALID
    lock = tensor_get_output_completion_lock()          // 0x310410 → 0xca7280
    pthread_mutex_lock(lock)
    nlog_write("The current tensor completion count on tensor %s is %lu. Now reset to 0 as requested")
    tensor->output_completion_count = 0                 // +0x80
    pthread_mutex_unlock(lock)
    return NRT_SUCCESS
```

> **GOTCHA —** the completion lock/cond are a **single process-global pair** (`output_completion_lock @0xca7280`, `output_completion_cond @0xca72c0`), shared by *every* tensor, returned by the one-instruction `lea` accessors `tensor_get_output_completion_lock/cond` (`0x310410`/`0x310400`). They are **not** `sto->tensor_op_cv_lock` (the per-storage I/O fence at `sto+0x50`). A reimplementer who reuses the per-storage lock for output polling will either over-serialize all output waiters onto one tensor's mutex or fail to wake the right waiter — the two planes are distinct by design. The producer that *bumps* `+0x80` lives in the [completion engine](../exec/submit-path.md), not in this API.

> **QUIRK —** `check` distinguishes three exits with three different codes: count reached → `NRT_SUCCESS(0)`, deadline hit → `NRT_TIMEOUT(5)`, and a `pthread` lock/ownership error (`"output_completion_lock was not owned by the current thread"`) → `NRT_FAILURE(1)`. The 30-second heartbeat (`30000000 µs`) is *informational* — it re-logs progress without affecting the wait. A reimplementer must not treat the heartbeat interval as a timeout.

---

## 4. Distinct Forwarders

The remaining non-template-pure entries each compose or gate their worker call in a way worth pinning.

### `nrt_tensor_allocate_slice` — two-worker compose

`nrt_tensor_allocate_slice` (`0xbf980`) is the only entry that calls **two** workers in sequence: `tensor_allocate_empty(name, &out)` to mint a fresh 192-byte view, then `tensor_set_slice(out, src, off, size)` (`0x30e530`) to point that view at `src`'s storage with a composed offset and a bumped storage refcount. On the first failure it logs `"Failed to allocate empty nrt tensor %s"`; on the second, `"Failed to set nrt tensor slice with src: %s"`. The actual offset composition (`slice->_offset = src->_offset + off`) and the storage-refcount increment are [tdrv-tensor §2](tdrv-tensor.md) detail — this entry is the public composer.

### `nrt_get_dmabuf_fd` — gated *off* under P2P / sim

`nrt_get_dmabuf_fd` (`0xc0500`) forwards `(va, size, *fd)` to `dmem_get_dmabuf_fd` (`0x229bb0`) to export a DMA-buf file descriptor for EFA peer-direct / cross-node RDMA. It is the only tensor entry gated by `nrt_global_config_t` flags rather than (only) by `nrt_init_state`: if `gconf->enable_p2p` **or** `gconf->funtime` is set, it short-circuits to `NRT_INVALID(2)` without calling the worker. The dmabuf IOCTL path and the EFA export struct are owned by [dmabuf-p2p](../kernel/dmabuf-p2p.md).

> **NOTE —** the `nrt_global_config_t` offsets that gate this entry are read in the disassembly as `enable_p2p @+0x74` and `funtime @+0xB8` (cell L-API-03). An earlier seed (SCAN-01) recorded `enable_p2p @+112` / `funtime @+176`; trust the L-API-03 disassembly here. The exact `nrt_global_config_t` layout is owned by [config-structs](config-structs.md) — this page records only that two flags gate the export and which ones.

### The tensor-set family — a hashtable, not a container

`nrt_allocate_tensor_set` / `_destroy_` / `_add_tensor_to_` / `_get_tensor_from_` (`0xbeae0`/`0xbeea0`/`0xbf1b0`/`0xbf580`) wrap the `kbl_*` feature-map-set workers; the set **is** a hashtable (`kbl_feature_map_set_t` and `ht_t` share a 32-byte layout — `size`/`size_mask`/`count`/`free_node_fn`/`nodes[]`). `get_tensor_from_tensor_set` looks up by name via `ht_name_find` (`0x268000`) and returns the tensor pointer from the node's value slot, which the decompile reads as `node[-1].next` (the value lives adjacent to the `ht_node_t`; MED confidence on that exact slot arithmetic). `destroy_tensor_set` frees the *set wrappers*, **not** the tensors — a tensor added to a set is not owned by the set.

### `nrt_get_model_tensor_info` — where dtype and shape live

`nrt_get_model_tensor_info` (`0xc0090`) is the one path that produces the data the tensor object deliberately lacks. It allocates an `nrt_tensor_info_array_t` (8-byte `tensor_count` header + `N × 296`-byte `nrt_tensor_info_t`), then fills it from `kmgr_get_io_tensor_count` + `kmgr_get_io_tensor_info` — **inputs first** (`usage=INPUT=0`), then outputs (`usage=OUTPUT=1`). Each entry carries `name[256]`, `usage`, `size`, `dtype` (`nrt_dtype_t`: `FLOAT32=10`, `BFLOAT16=6`, `FP8_E4=14`, …), a heap `shape` array, and `ndim`. Under `gconf->funtime` it returns an empty array. `nrt_free_model_tensor_info` (`0xc0390`) frees each entry's `shape` then the array (NULL-safe, `INIT`-only).

> **QUIRK —** the tensor *object* (`nrt_tensor_t`/`nrt_tensor_storage_t`) has **no dtype and no shape** — it is byte-untyped (see [tdrv-tensor §3](tdrv-tensor.md)). Element type and dimensionality exist **only** in NEFF metadata and are surfaced **only** here, through `kmgr_get_io_tensor_info`, which reads them out of the loaded model. A reimplementer who expects `dtype` on the handle will look forever; it is a property of the *model's* I/O binding, not of the buffer. The caller owns the returned `shape` arrays (hence `kmgr` `malloc`s them and `nrt_free_model_tensor_info` frees them).

### Internal-linkage neighbors (not public exports)

The `nrt_vnc_usage_*` family (`0xc1740`/`0xc17b0`/`0xc1820`/`0xc18a0`) and `nrt_interned_string_db_combine_shards` (`0x508960`) sit in this band but are **not** in the dynamic export table (mangled `_Z…` symbols). `nrt_vnc_usage_*` is a mutex-guarded `uint32` per-VNC usage refcount table (`init`/`inc`/`dec`/`find_and_inc`, the last a least-loaded-VNC picker for load balancing, capped at `MAX_VIRTUAL_TPB=0x100`); it is driven by the load path, not by a framework caller, and is documented with the load/lifecycle cells. They are listed here only so a symbol-table reader does not mistake them for public tensor API.

---

## Related Components

| Name | Relationship |
|---|---|
| `tensor_*` workers (`0x30e1d0–0x310418`) | the TDRV bodies every state-gated entry forwards to; object model owned by [tdrv-tensor](tdrv-tensor.md) |
| `kbl_*` feature-map / `ht_name_find` | the hashtable workers behind the tensor-set family |
| `kmgr_get_io_tensor_count` / `_info` | the model-introspection workers behind `nrt_get_model_tensor_info` |
| `dmem_get_dmabuf_fd` (`0x229bb0`) | the DMA-buf export worker behind `nrt_get_dmabuf_fd` |
| `nrt_init_state` guard (`0xc5d1a0`) | the four-state lifecycle machine the template gates on; owned by [api-lifecycle](api-lifecycle.md) |
| `nrt_infodump` / `nrt_core_dump` | the error-tail support-bundle + crash-dump forwarders the template calls on nonzero status |
| `nrta_tensor_copy/_write/_read` | the *explicit-async* (`@@NRT_3.0.0`) producer-side siblings; owned by [api-async-collectives](api-async-collectives.md) |

## Cross-References

- [TDRV: Tensor Object Layer](tdrv-tensor.md) — the `nrt_tensor_t`/`nrt_tensor_storage_t` object model, two refcounts, tagged union, byte-untyped invariant, and the per-storage fence vs the global completion plane that the workers on this page implement
- [Public C API: Lifecycle and Init/Teardown](api-lifecycle.md) — the four-state `nrt_init_state` guard, `NlogErrorContextManager`, and the `nrt_infodump`/`nrt_core_dump` error tail that the wrapper template reuses verbatim
- [Error and Status Codes (NRT_STATUS)](error-codes.md) — the `NRT_UNINITIALIZED(13)`/`NRT_CLOSED(14)`/`NRT_FAILURE(1)`/`NRT_INVALID(2)`/`NRT_TIMEOUT(5)` returns this surface emits
- [DMA-buf Export and P2P](../kernel/dmabuf-p2p.md) — the `dmem_get_dmabuf_fd` IOCTL path and EFA peer-direct export struct behind `nrt_get_dmabuf_fd`
- [The Submit Path (Bind → Stage → Doorbell)](../exec/submit-path.md) — where output tensors are bound for execution and where `tensor->output_completion_count` (+0x80) is bumped, the producer the §3 `check`/`reset` pair waits on
