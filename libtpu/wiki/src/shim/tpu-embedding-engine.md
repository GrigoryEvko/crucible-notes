# TpuEmbeddingEngine ABI

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped). IDA-recovered C names and demangled C++ symbols are quoted verbatim; `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`TpuEmbeddingEngine_*` is the C-ABI cluster that fronts the **SparseCore embedding-offload subsystem** — the host-side entry through which XLA/TensorFlow drives the on-device embedding pipeline (large lookup tables, sparse gather/scatter, segmented combiner reductions, deduplication) without sharing a single C++ type across the `.so` boundary. Where the other shim rosters back `stream_executor` device-runtime abstractions reached through `ExecutorApiFn`, this roster is reached through the **`OpsApiFn` accessor** (`0x10900e80`), the partition of the surface dedicated to embedding/SparseCore ops. Everything on this page is the *seam*: how a serialized `TPUEmbeddingConfiguration` proto and a flat `*_Params` struct cross into the plugin, which `tensorflow::*` / `BarnaCoreManager` / `barna_core_util` core function each C call dispatches into, and what flows back out. The on-device primitives those functions ultimately drive — minibatching decomposition, the segmented-scan combiner, dedup multiplicity — are owned by [`../sparsecore/`](../sparsecore/overview.md) and are linked, not re-derived, here.

The fifteen `extern "C"` free functions split cleanly into four source files, and the split is the subsystem's lifecycle. **Configuration** (`tpu_embedding_engine_configuration_ops_c_api.cc`) brings the engine up: partition the tables across cores, size and collate HBM, configure and connect hosts, finalize, probe initialization. **Load/retrieve** (`tpu_embedding_engine_load_retrieve_ops_c_api.cc`) moves parameter tensors to and from device through the BarnaCore driver. **XLA ops** (`tpu_embedding_engine_xla_ops_c_api.cc`) do *not* execute on device at all — they each build an `xla::XlaBuilder` computation that the host splices into its HLO graph (recv activations, send gradients, the dedup-data triple). **Enqueue** (`tpu_embedding_engine_enqueue_ops_c_api.cc`) feeds a sparse input batch into the engine's per-host batch creators. A sixteenth concern, the engine's *resource handle*, is the small `TpuEmbeddingEngineState_*` trio that wraps the long-lived `tensorflow::TpuEmbeddingEngineState` C++ object the configuration calls operate on.

Two C-ABI shapes recur and a reimplementer must reproduce both. Every function takes exactly one argument: a pointer to a flat **`*_Params` struct** the host fills, whose fields are read by byte offset (`a1+16`, `a1+24`, …) for inputs and written through indirected out-pointers (`**(a1+40) = operator new(...)`) for outputs, with a trailing `TF_Status*` slot the callee populates via `tsl::Set_TF_Status_from_Status`. And every status-returning core call uses the `absl::Status` "ok is the sentinel `(char*)&dword_0 + 1`" idiom: equal-to-OK means success, otherwise marshal the error into the out-status and `Unref` the rep. This is the same opaque-handle/`*ApiFn` discipline established on the [shim overview](overview.md); this page documents only the embedding cluster's slots.

For reimplementation, the contract is:

- **The `*_Params` struct convention** — one in-pointer per call; inputs read by fixed offset; outputs returned through `operator new` + `memcpy` into host-owned out-pointers; a final `TF_Status*` slot.
- **The proto/serialized-config ingest** — most entries first `stream_executor::tpu::DeserializeProto<TPUEmbeddingConfiguration, TpuSerializedProto>` from a `{ptr,len}` slot, then dispatch on the decoded config.
- **The four-family dispatch map** — which `tensorflow::*` / `BarnaCoreManager::*` / `barna_core_util::*` core function each C entry calls, and whether it *executes* (configuration / load / enqueue) or *lowers to HLO* (XLA ops).
- **The state handle** — `TpuEmbeddingEngineState_*` as the resource the configuration family mutates, distinct from the per-call `*_Params`.

| | |
|---|---|
| **Accessor** | `stream_executor::tpu::OpsApiFn() @ 0x10900e80` (embedding/SparseCore partition) |
| **C-ABI free functions** | 15 (`TpuEmbeddingEngine_*`) + 3 state-handle (`TpuEmbeddingEngineState_*`) |
| **Address span (engine)** | `0xf6a5b20` – `0xf769be0` |
| **Call convention** | single `*_Params*` arg; offset-addressed in/out fields; trailing `TF_Status*` |
| **Config ingest** | `DeserializeProto<tensorflow::tpu::TPUEmbeddingConfiguration, TpuSerializedProto>` |
| **Status idiom** | `absl::Status` OK-sentinel `(char*)&dword_0 + 1`; error → `tsl::Set_TF_Status_from_Status` |
| **Core driver** | `platforms_deepsea::jellyfish::barna_core::BarnaCoreManager` (load/retrieve, enqueue) |
| **XLA lowering** | `tensorflow::barna_core_util::*Computation` via `xla::XlaBuilder` |
| **State handle type** | `tensorflow::TpuEmbeddingEngineState` (resource-mgr backed) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **Scope —** the on-device embedding ops this C-API ultimately drives — the minibatching decomposition, the segmented-add combiner, deduplication multiplicity — are owned by [`../sparsecore/`](../sparsecore/overview.md) (see [Embedding Minibatching](../sparsecore/embedding-minibatching.md), [Dedup Multiplicity](../sparsecore/dedup-multiplicity.md)). The BarnaCore scalar engine that `BarnaCoreManager` programs is owned by [`../barnacore/`](../barnacore/overview.md). The `*ApiFn` accessor / opaque-handle pattern is owned by the [shim overview](overview.md). This page owns the `TpuEmbeddingEngine_*` function roster and the host→SparseCore offload entry map only.

---

## 1. The `*_Params` Call Convention

### Purpose

Every embedding C-ABI function takes a single pointer to a host-allocated **parameter struct** and returns `void` (the few that return a value return a status rep that the body itself stores). There is no name at the call site — the host fills a struct, calls through an `OpsApiFn` slot, and reads the struct's output fields back. The struct layout *is* the ABI contract: a reimplementer who shifts a field offset breaks every caller silently, because there is nothing else to fall back on.

### Algorithm

The shape is uniform. `TpuEmbeddingEngine_IsInitialized` is the smallest complete example: read a serialized config string from two input slots, call the core predicate, write one `bool` out, set the status.

```c
function TpuEmbeddingEngine_IsInitialized(Params* p):       // sub_f6a6ba0
    // optional VLOG(1) site, file tpu_embedding_engine_configuration_ops_c_api.cc:121
    len = p->config_len            // p+16
    buf = p->config_ptr            // p+24 — serialized TPUEmbeddingConfiguration bytes
    // small-string-optimization copy into a local std::string (len>22 => heap)
    status = tensorflow::IsTPUEmbeddingInitialized(buf, len, &out_bool)
    if status != OK_SENTINEL:                                // (char*)&dword_0 + 1
        // store into p->status (p+40), Unref old rep
        store_status(p->status_slot, status)
    *(*(p+32)) = out_bool          // **p->out_initialized = result
    if heap_allocated: free(local)
```

> **NOTE —** the OK sentinel is the literal pointer value `(char*)&dword_0 + 1` — i.e. an `absl::Status` whose inline representation encodes "OK" (low bit set, no heap rep). Every body in this roster compares the returned rep against that constant; *equal* is success. A reimplementation that treats a non-null pointer as an error rep will misread every successful call.

The input side uses libc++'s small-string optimization in reverse: the host passes `{len, ptr}` and the callee reconstructs a `std::string` on its stack, heap-allocating only when `len > 22`. The output side mirrors it: results that are `std::string`/byte-blobs (`ExecutePartitioner`, `ConfigureMemory`, `CollateMemory`, `ConfigureHost`) are returned by writing the byte count to one out-slot and a freshly `operator new`'d copy to another:

```c
// output marshalling shared by ExecutePartitioner / ConfigureMemory / CollateMemory   (e.g. sub_f6a5b20)
**(p+32) = result.size                       // out: byte count
**(p+40) = operator new(result.size)         // out: host receives an owned copy
memcpy(**(p+40), result.data, result.size)   // caller frees later via TpuConfigurationApi free
```

> **GOTCHA —** the output buffer is `operator new`'d by the *plugin* and handed to the host as a raw pointer; the host owns it and must free it through the matching array-free entry (the `TpuConfigurationApi_*` free functions, see [TpuConfigurationApi](tpu-configuration-api.md)), not its own allocator. Mixing allocators across the seam corrupts the heap. This is why the engine roster has no `_Free` of its own — frees are centralized.

### Considerations

The `*_Params` field offsets differ per function (each struct is bespoke), but the *grammar* is fixed: leading input scalars/`{len,ptr}` pairs, then `out*` double-pointers, then a `TF_Status*` last. The trailing-status convention is what lets these functions return `void` — failure is reported in-band through the struct, never by return value.

---

## 2. The Configuration Family

### Purpose

Seven entries in `tpu_embedding_engine_configuration_ops_c_api.cc` form the engine bring-up sequence. They are stateful in spirit — they partition the embedding tables across SparseCores, reserve and lay out HBM, wire the hosts of a pod together, and flip the engine to "initialized" — but the C surface stays stateless-per-call: each ingests the serialized `TPUEmbeddingConfiguration` (and, for the memory calls, the partitioner's prior output) and returns the next stage's serialized result.

### Entry Point

```text
host (XLA TPU embedding configuration ops)
  └─ OpsApiFn()[slot]  ── 0x10900e80
       └─ TpuEmbeddingEngine_ExecutePartitioner   0xf6a5b20  ── partition tables → partitions blob
       └─ TpuEmbeddingEngine_ConfigureMemory      0xf6a5d80  ── size HBM for one core
       └─ TpuEmbeddingEngine_CollateMemory        0xf6a5fa0  ── merge per-core HBM into pod layout
       └─ TpuEmbeddingEngine_ConfigureHost        0xf6a6340  ── per-host config blob
       └─ TpuEmbeddingEngine_ConnectHosts         0xf6a6660  ── wire pod hosts
       └─ TpuEmbeddingEngine_Finalize             0xf6a6960  ── commit; engine ready
       └─ TpuEmbeddingEngine_IsInitialized        0xf6a6ba0  ── bool probe
```

### Algorithm

`ExecutePartitioner` is the canonical configuration entry — deserialize the config proto, run the partitioner, marshal the partitions blob back:

```c
function TpuEmbeddingEngine_ExecutePartitioner(Params* p):   // sub_f6a5b20
    cfg = DeserializeProto<TPUEmbeddingConfiguration>(p+16)   // TpuSerializedProto {ptr,len}
    status = tensorflow::ExecuteTpuEmbeddingPartitioner(cfg, &out_blob)
    if status == OK_SENTINEL:
        **(p+32) = out_blob.size
        **(p+40) = operator new(out_blob.size); memcpy(...)   // partitions proto bytes
    else:
        Set_TF_Status_from_Status(p->status, status)          // p+48
    ~TPUEmbeddingConfiguration(cfg)
```

`ConfigureMemory` (`0xf6a5d80`) reads a core index (`p+16`) plus a serialized partitions blob (`p+24/+32`) and calls `tensorflow::ConfigureTpuEmbeddingMemory(core, blob, len, &out)`. `CollateMemory` (`0xf6a5fa0`) ingests a *vector* of per-core memory blobs — it loops `p[+16]` times, copying each `{len,ptr}` into a `std::vector<std::string>` (24-byte stride per element) before calling `tensorflow::CollateTpuEmbeddingMemory(vec.data, vec.size, &out)`, then frees the temporary vector element-by-element. `ConfigureHost` (`0xf6a6340`) mirrors `ConfigureMemory`'s string-in/blob-out shape for a host index, dispatching into `tensorflow::ConfigureTpuEmbeddingHost`. `ConnectHosts` dispatches into `tensorflow::ConnectTpuEmbeddingHosts`, `Finalize` into `tensorflow::FinalizeTpuEmbedding` — both consume the config and report status only (no large output blob).

### Function Map

| Function | Addr | Size | Core callee |
|---|---|---|---|
| `TpuEmbeddingEngine_ExecutePartitioner` | `0xf6a5b20` | 363 | `tensorflow::ExecuteTpuEmbeddingPartitioner` |
| `TpuEmbeddingEngine_ConfigureMemory` | `0xf6a5d80` | 540 | `tensorflow::ConfigureTpuEmbeddingMemory` |
| `TpuEmbeddingEngine_CollateMemory` | `0xf6a5fa0` | 908 | `tensorflow::CollateTpuEmbeddingMemory` |
| `TpuEmbeddingEngine_ConfigureHost` | `0xf6a6340` | 774 | `tensorflow::ConfigureTpuEmbeddingHost` |
| `TpuEmbeddingEngine_ConnectHosts` | `0xf6a6660` | 738 | `tensorflow::ConnectTpuEmbeddingHosts` |
| `TpuEmbeddingEngine_Finalize` | `0xf6a6960` | 549 | `tensorflow::FinalizeTpuEmbedding` |
| `TpuEmbeddingEngine_IsInitialized` | `0xf6a6ba0` | 385 | `tensorflow::IsTPUEmbeddingInitialized` |

> **QUIRK —** `ConfigureMemory` → `CollateMemory` is a fan-in. `ConfigureMemory` is called once *per SparseCore* and each returns that core's HBM-layout blob; `CollateMemory` then takes the whole *array* of those blobs and merges them into one pod-wide layout. A reimplementation that calls `CollateMemory` with a single core's output will produce a layout that ignores cross-core table sharding. The 24-byte per-element loop in `CollateMemory` is reconstructing the `std::vector<std::string>` the host flattened into the `*_Params` struct.

### Considerations

There is **no** `TpuEmbeddingEngine_PartitionStateVariables` entry in this build; the partitioning concern is `ExecutePartitioner` (table partitioning) dispatching into `tensorflow::ExecuteTpuEmbeddingPartitioner`. The configuration family never touches the `TpuEmbeddingEngineState` handle directly through these C calls — the state object is resolved separately on the host side via `tensorflow::GetAndInitializeTpuEmbeddingEngineState(ResourceMgr*)` (`0xf78aec0`), an internal C++ helper, not part of the flat C roster.

---

## 3. The Load / Retrieve Family

### Purpose

Two entries in `tpu_embedding_engine_load_retrieve_ops_c_api.cc` move embedding-table parameter tensors between host and device. Unlike the configuration family, these call into the live **`BarnaCoreManager`** driver — they require an initialized engine and operate on device memory, not on serialized config blobs.

### Entry Point

```text
TpuEmbeddingEngine_WriteParameters   0xf6a6d40
  └─ tensorflow::GetBarnaCoreManager(&mgr)                       ── resolve live driver
       └─ BarnaCoreManager::WriteParameters(mgr)                 ── host → device tables
TpuEmbeddingEngine_ReadParameters    0xf6a7160
  └─ tensorflow::GetBarnaCoreManager(&mgr)
       └─ BarnaCoreManager::ReadParameters(mgr)                  ── device → host tables
```

### Algorithm

```c
function TpuEmbeddingEngine_WriteParameters(Params* p, TF_Status* status):   // sub_f6a6d40
    // build std::array<std::vector<absl::Span<float const>>, 8> from p
    //   outer dimension 8 == per-SparseCore fan-out; inner vector grows via
    //   2x reallocation (operator new(16*cap)) holding {ptr,len} float spans
    for core in 0..8:
        for slot in 0..p->count(p+64):
            span = { p[core][slot].data, p[core][slot].len }   // absl::Span<const float>
            push_back(spans[core], span)
    if GetBarnaCoreManager(&mgr) == OK and mgr != null:
        status = BarnaCoreManager::WriteParameters(mgr)         // pushes spans to device
    else if mgr == null:
        status = MakeErrorImpl<3>("TpuEmbeddingEngine not initialized.", file:43)
    Set_TF_Status_from_Status(status_out, status)
    ~array(spans)                                                // free inner vectors
```

`ReadParameters` (`0xf6a7160`, 1000 bytes) is the symmetric inverse: resolve the manager, call `BarnaCoreManager::ReadParameters`, marshal device tables back into host buffers.

### Function Map

| Function | Addr | Size | Core callee |
|---|---|---|---|
| `TpuEmbeddingEngine_WriteParameters` | `0xf6a6d40` | 750 | `BarnaCoreManager::WriteParameters` |
| `TpuEmbeddingEngine_ReadParameters` | `0xf6a7160` | 1000 | `BarnaCoreManager::ReadParameters` |

> **GOTCHA —** the explicit `"TpuEmbeddingEngine not initialized."` error (string at file line 43) fires when `GetBarnaCoreManager` succeeds but returns a null manager. A reimplementation that only checks `GetBarnaCoreManager`'s status and not the manager pointer will dereference null and crash instead of returning a clean `INVALID_ARGUMENT` (`MakeErrorImpl<3>`).

### Considerations

The `std::array<..., 8>` outer dimension is the per-SparseCore fan-out (eight cores per chip on the targeted generation); each core's parameter spans are gathered separately because the on-device tables are sharded by core. The inner `std::vector<absl::Span<const float>>` uses standard libc++ geometric growth (the `2*cap` / `__throw_length_error` guards in the decompile are vector reallocation, not engine logic) — a reimplementer can ignore the reallocation arithmetic and model it as `push_back`.

---

## 4. The XLA-Ops Family

### Purpose

Five entries in `tpu_embedding_engine_xla_ops_c_api.cc` are categorically different: they **emit XLA HLO**, they do not run on device. Each builds an `xla::XlaBuilder` computation that the host's XLA op-kernel splices into the surrounding graph, so the embedding recv/send and dedup operations become first-class HLO the XLA compiler can schedule and fuse. They are the bridge between the embedding subsystem and the main TPU program.

### Entry Point

```text
TpuEmbeddingEngine_RecvActivationsComputation                   0xf767960
  └─ GetEmbeddingPartitionsProtoAndHbmBuffersConfig(...)        ── partitions + HBM layout
  └─ DeserializeProto<TPUEmbeddingConfiguration>(...)
  └─ xla::XlaBuilder::XlaBuilder(...)                           ── build HLO computation
TpuEmbeddingEngine_SendTPUEmbeddingGradientsComputation         0xf768d80
  └─ tensorflow::barna_core_util::LowerSendTPUEmbeddingGradientsComputation
TpuEmbeddingEngine_RecvTPUEmbeddingDeduplicationDataComputation 0xf7683e0
  └─ tensorflow::barna_core_util::LowerRecvTPUEmbeddingDeduplicationDataComputation
TpuEmbeddingEngine_DedupDataSizeComputation                     0xf7697e0
  └─ tensorflow::barna_core_util::DedupDataSizeComputation
TpuEmbeddingEngine_DedupDataTupleMaskComputation               0xf769be0
  └─ tensorflow::barna_core_util::DedupDataTupleMaskComputation
```

### Algorithm

The dedup-size entry shows the shared preamble: resolve the embedding partitions proto and HBM-buffers config, resolve the topology, then call the `barna_core_util` lowering.

```c
function TpuEmbeddingEngine_DedupDataSizeComputation(Params* p):   // sub_f7697e0
    cfg = DeserializeProto<TPUEmbeddingConfiguration>(p+16)
    status = GetEmbeddingPartitionsProtoAndHbmBuffersConfig(
                 &partitions /*out, p+32*/, &hbm /*out, p+48*/)
    if status != OK: { Set_TF_Status(p+88, status); cleanup; return }
    if p->has_explicit_topology(p+72) == 0:
        topo = tfrt::GetGlobalTpuTopology()              // fall back to global
    else:
        topo = GetTpuTopology(p->serialized_topology)    // from p+72
    result = tensorflow::barna_core_util::DedupDataSizeComputation(
                 cfg, partitions, hbm, topo)             // file:324
    **(p+80) = result.size                               // out: dedup data size (int)
    // ~HbmBuffersConfig, ~EmbeddingPartitionsProto, ~TPUEmbeddingConfiguration
```

`RecvActivationsComputation` (`0xf767960`) performs the same preamble, then constructs a full `xla::XlaBuilder` and assembles the recv-activations HLO (it manipulates `xla::OpSharding` to place the result), returning a serialized computation. `SendTPUEmbeddingGradientsComputation` and `RecvTPUEmbeddingDeduplicationDataComputation` dispatch into the matching `barna_core_util::Lower*Computation` builders; `DedupDataTupleMaskComputation` into `barna_core_util::DedupDataTupleMaskComputation`.

### Function Map

| Function | Addr | Size | Core callee | Emits |
|---|---|---|---|---|
| `TpuEmbeddingEngine_RecvActivationsComputation` | `0xf767960` | 2463 | `xla::XlaBuilder` recv-activations build | HLO computation |
| `TpuEmbeddingEngine_RecvTPUEmbeddingDeduplicationDataComputation` | `0xf7683e0` | 2440 | `barna_core_util::LowerRecvTPUEmbeddingDeduplicationDataComputation` | HLO computation |
| `TpuEmbeddingEngine_SendTPUEmbeddingGradientsComputation` | `0xf768d80` | 2645 | `barna_core_util::LowerSendTPUEmbeddingGradientsComputation` | HLO computation |
| `TpuEmbeddingEngine_DedupDataSizeComputation` | `0xf7697e0` | 999 | `barna_core_util::DedupDataSizeComputation` | size (int out) |
| `TpuEmbeddingEngine_DedupDataTupleMaskComputation` | `0xf769be0` | 1325 | `barna_core_util::DedupDataTupleMaskComputation` | tuple mask |

> **QUIRK —** the `*Computation` suffix is literal — these return *HLO*, not device results. `RecvActivationsComputation` instantiating an `xla::XlaBuilder` and emitting `xla::OpSharding` is the tell: the embedding lookup is expressed as XLA ops the main compiler schedules, so the SparseCore recv/send is fused into the surrounding step rather than being a separate dispatch. A reimplementer who models these as device calls (like the load/retrieve family) will miss that their output is a subgraph the XLA pipeline still has to compile. The dedup triple (`Size` → `TupleMask` → recv `DeduplicationData`) is the host-side metadata the deduplication scheme needs; the on-device multiplicity math is in [Dedup Multiplicity](../sparsecore/dedup-multiplicity.md).

### Considerations

The topology resolution is a two-way branch every XLA-op entry shares: if the `*_Params` carries an explicit serialized topology (`p+72` non-zero) it is deserialized via `GetTpuTopology`; otherwise the engine falls back to the process-global `tfrt::GetGlobalTpuTopology()`. The global path takes a shared-ref on the topology (`_InterlockedExchangeAdd64` ref-count decrement on cleanup), the explicit path heap-wraps a fresh `TpuTopology`. Both feed the same `barna_core_util` lowering; a reimplementer must keep both paths because configured-pod and single-host runs take different ones.

---

## 5. EnqueueTensorBatch and the State Handle

### Purpose

The largest engine function (`EnqueueTensorBatch`, 4001 bytes) and the three-function state-handle cluster do not fit the configure/load/lower families. `EnqueueTensorBatch` feeds a sparse input minibatch into the engine's per-host batch creators; the `TpuEmbeddingEngineState_*` trio is the resource handle the configuration family's *internal* C++ side operates on.

### Algorithm

```c
function TpuEmbeddingEngine_EnqueueTensorBatch(Params* p):       // sub_f6a9680
    // file tpu_embedding_engine_enqueue_ops_c_api.cc
    GetBarnaCoreManager(&mgr)
    creators = BarnaCoreManager::GetUserBatchCreators(mgr)       // per-host batch builders
    // pack the input tensor batch (indices/values/weights) into creators
    status = BarnaCoreManager::Run(mgr, ...)                     // drive minibatching
    Set_TF_Status_from_Status(p->status, status)
```

`EnqueueTensorBatch` is the host entry into the **minibatching** decomposition: `BarnaCoreManager::GetUserBatchCreators` returns the per-host objects that translate a ragged input batch into the per-SparseCore minibatch format, and `BarnaCoreManager::Run` drives the on-device combine. The decomposition itself is [Embedding Minibatching](../sparsecore/embedding-minibatching.md).

The state handle is trivial-by-design:

```c
TpuEmbeddingEngineState_GetState(handle):   return *(void**)handle    // sub_f766fe0, 4 bytes
TpuEmbeddingEngineState_Create(...):        // sub_21389240 — allocate + init State
TpuEmbeddingEngineState_Free(...):          // sub_f766ea0 — destroy + free
```

### Function Map

| Function | Addr | Size | Role |
|---|---|---|---|
| `TpuEmbeddingEngine_EnqueueTensorBatch` | `0xf6a9680` | 4001 | enqueue sparse batch → `BarnaCoreManager::Run` (minibatching) |
| `TpuEmbeddingEngineState_Create` | `0x21389240` | 1295 | allocate + initialize `TpuEmbeddingEngineState` |
| `TpuEmbeddingEngineState_Free` | `0xf766ea0` | 292 | destroy + free the state handle |
| `TpuEmbeddingEngineState_GetState` | `0xf766fe0` | 4 | dereference handle → inner state pointer |

> **NOTE —** `TpuEmbeddingEngineState` is a `tensorflow::TpuEmbeddingEngineState` C++ object reached through the TF `ResourceMgr` on the host side (`tensorflow::GetAndInitializeTpuEmbeddingEngineState`, `0xf78aec0`; `GetTpuEmbeddingEngineState`, `0xf78b1e0`). Its constructor takes either a `tpu::TpuTopology` (`0xf961920`) or a `tpu::System` (`0xf9619e0`), and on first use it runs `InitializeBarnaCoreManager` (`0xf961dc0`, 3204 bytes) — the bridge from this engine into the [BarnaCore](../barnacore/overview.md) scalar driver. These mangled `tensorflow::TpuEmbeddingEngineState*` symbols are internal C++, not part of the flat C roster; only `Create` / `Free` / `GetState` cross the seam.

### Considerations

`GetState`'s 4-byte body (`return *a1`) confirms the handle is a thin pointer-to-pointer wrapper: the C `Tpu...State*` the host holds is one indirection above the real `tensorflow::TpuEmbeddingEngineState`. A reimplementer reproduces the handle as an opaque single-pointer struct whose first word *is* the state pointer; `Create` allocates both, `Free` tears down both, `GetState` reads the inner word. The heavy lifting (BarnaCore manager init) happens lazily inside the C++ state object, not at `Create`.

---

## Related Components

| Name | Relationship |
|---|---|
| `stream_executor::tpu::OpsApiFn()` | the accessor (`0x10900e80`) whose slots hold these `TpuEmbeddingEngine_*` pointers |
| `tensorflow::TpuEmbeddingEngineState` | the resource-mgr-backed C++ state the configuration family operates on |
| `platforms_deepsea::jellyfish::barna_core::BarnaCoreManager` | the live driver behind load/retrieve and enqueue |
| `tensorflow::barna_core_util::*Computation` | the HLO lowering helpers behind the XLA-ops family |
| `stream_executor::tpu::DeserializeProto<TPUEmbeddingConfiguration, …>` | the serialized-config ingest shared across most entries |
| `TpuConfigurationApi_*` (array frees) | frees the `operator new`'d output blobs these functions hand back |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the `*ApiFn` accessor / opaque-handle pattern; this cluster is reached via `OpsApiFn`
- [TpuExecutor Roster](tpu-executor-roster.md) — the device-runtime sibling cluster reached via `ExecutorApiFn`
- [TpuConfigurationApi](tpu-configuration-api.md) — the runtime-config C entry points that free the engine's output blobs
- [SparseCore Overview](../sparsecore/overview.md) — the on-device embedding engine these C calls drive
- [Embedding Minibatching](../sparsecore/embedding-minibatching.md) — the minibatch decomposition `EnqueueTensorBatch` feeds
- [Dedup Multiplicity](../sparsecore/dedup-multiplicity.md) — the on-device math behind the `DedupData*` XLA ops
- [BarnaCore Overview](../barnacore/overview.md) — the scalar driver `BarnaCoreManager` programs for load/retrieve and enqueue
