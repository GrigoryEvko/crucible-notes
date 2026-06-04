# PJRT Client Allocator Integration

> *All addresses, vtable slots, and struct sizes on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`, clang 9999.0.0). Other versions will differ.*

## Abstract

This page documents the **stream-executor allocator bridge** inside the TPU PJRT plugin: the layer that sits between PJRT's device-memory API and the on-core best-fit engine. In a StreamExecutor backend a `se::DeviceMemoryAllocator::Allocate(ordinal, size, …)` call returns a `se::OwningDeviceMemory` (a `ScopedDeviceMemory`) that wraps a `DeviceMemoryBase`. libtpu has no `se::StreamExecutorMemoryAllocator` on the hot path — it ships a bespoke chain that fills the same role. `xla::TpuClient::AllocateRawBuffer` is the `Allocate` entry; the `TpuSharedMemoryLocation` is the `DeviceMemoryBase` identity (ordinal + memory-space tier); the `tpu::TpuBuffer` (wrapped by `xla::TpuRawBuffer`) is the owning handle whose destructor frees, playing the `ScopedDeviceMemory` role; and `tpu::System::Allocate` is the per-ordinal router that dispatches into the right core's allocator.

The bridge is what a StreamExecutor reader must learn to re-map their mental model. `AllocateRawBuffer` routes by `PjRtMemorySpace::kKindId` (HBM device, pinned host, unpinned-host/CPU staging) to one of three backends — the analogue of a multi-memory-space `Allocate`. The device branch threads through a four-deep indirection: `TpuClient::AllocateBuffer` → `tpu::AllocateBuffer` → `tpu::System::Allocate` (the per-core map lookup, the `ordinal → allocator` step) → a virtual `tpu::TpuAllocator::Allocate` → `tpu::TpuSharedMemory::AllocateLocked` → the HBM best-fit `Allocate`. The `Deallocate` side is symmetric and runs from the `TpuBuffer` destructor through the same `TpuAllocator` vtable. This page owns that chain, the `Allocate`/`Deallocate` ABI, and the memory-space routing; it deliberately stops at the best-fit `Allocate` call boundary.

> **NOTE —** the best-fit-with-coalescing *algorithm* (the free-list structure, the RB-tree search, the eager coalesce, the split policy, the 1024 B HBM quantum) lives on [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md). This page calls into `BestFitAllocator::Allocate` (vt+0x30, `0x1e817820`) and `Deallocate` (vt+0x38, `0x1e819dc0`) and stops there — it does **not** re-derive what that engine does internally.

For reimplementation, the bridge contract is:

- **The `Allocate` ABI.** `TpuClient::AllocateRawBuffer(PjRtMemorySpace*, size, bool, AsyncValueRef<bool>)` routes by memory-space kind. The device branch reaches the engine through exactly four indirections, ending in a vtable tail-call at slot `+0x10`.
- **The per-ordinal router.** `tpu::System::Allocate` looks up a `flat_hash_map<TpuSharedMemoryLocation, unique_ptr<TpuAllocator>>` and tail-calls the matched allocator's virtual `Allocate`. The `TpuSharedMemoryLocation` is the `DeviceMemoryBase`-equivalent key.
- **The `TpuAllocator` 6-slot virtual interface.** Two concrete strategies (`ReusingTpuAllocator`, `DeferredTpuAllocator`) share one vtable shape; both delegate physical placement to the per-core `TpuSharedMemory`.
- **The `ScopedDeviceMemory` ownership.** `tpu::TpuBuffer` (variant `{Owned, Sliced, Unsafe}`) is the owning handle; `xla::TpuRawBuffer` wraps it for PJRT; destruction routes back through `TpuAllocator::Deallocate` (vt+0x30).
- **Where the call lands but stops.** `TpuSharedMemory::AllocateLocked` dispatches `BestFitAllocator::Allocate` (vt+0x30); the engine internals are [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md).

| | |
|---|---|
| **`Allocate` entry (PJRT)** | `xla::TpuClient::AllocateRawBuffer` `0xf7fb1e0` — routes by `PjRtMemorySpace` kind |
| **Device sub-entry** | `xla::TpuClient::AllocateBuffer` `0xf7fc5a0` → `tpu::AllocateBuffer` `0xf8d51c0` |
| **Per-ordinal router** | `tpu::System::Allocate(loc, size)` `0x1d0aeea0` — `flat_hash_map` lookup → `jmp *vt[+0x10]` |
| **Allocator key (`DeviceMemoryBase`)** | `tpu::TpuSharedMemoryLocation` — ctor `0x20ad6ae0`, `operator==` `0x20ad6be0`, hash `0x1d0ba2a0` |
| **`TpuAllocator` interface** | abstract base, typeinfo `0x21ca8508`; 6-slot vtable; `Allocate` = vt+0x10 |
| **Concrete strategies** | `ReusingTpuAllocator` (56 B, vt `0x21ca85d8`) · `DeferredTpuAllocator` (176 B, vt `0x21ca84a8`) |
| **Owning handle (`ScopedDeviceMemory`)** | `tpu::TpuBuffer` (`xla::TpuRawBuffer` wraps); on-device size at `TpuBuffer+0x50` |
| **Engine boundary (NOT this page)** | `tpu::BestFitAllocator::Allocate` vt+0x30 `0x1e817820` → [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md) |
| **Confidence** | HIGH (byte-anchored) unless a row or callout says otherwise |

---

## Scope and Boundaries

This page is the **stream-executor allocator bridge** — the `Allocate`/`Deallocate` ABI and the memory-space routing into the per-core allocator. Adjacent concerns live on their own pages; do not duplicate them here.

| Concern | Owner page |
|---|---|
| The best-fit free-list structure, RB-tree search, coalescing, split policy, alignment quantum | [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md) |
| The `tpu::TpuBuffer` field layout, `TpuSharedMemoryLocation` encoding, slice/view semantics | [`../memory/tpu-buffer-layout.md`](../memory/tpu-buffer-layout.md) |
| Buffer donation, input/output aliasing, `ScopedHold`, `HloInputOutputAliasConfig` | [`../memory/buffer-donation-aliasing.md`](../memory/buffer-donation-aliasing.md) |
| Where `ExecuteAsyncOnStream` calls output allocation, the dispatch/launch flow | [`execute-async-on-stream.md`](execute-async-on-stream.md) |
| Program load, scratch binding, the in-flight-execution semaphore | [`load-program-enqueue.md`](load-program-enqueue.md) |

What this page owns: the `AllocateRawBuffer` → `…` → `AllocateLocked` call chain, the `Allocate`/`Deallocate` vtable ABI, the `TpuSharedMemoryLocation` ordinal/tier routing, and the `TpuBuffer`-as-`ScopedDeviceMemory` ownership wiring. The output-buffer allocation that `ExecuteAsyncOnStream` performs is shown here only at the point where it *enters* this bridge.

---

## The StreamExecutor Mental Map

A reader coming from XLA's StreamExecutor backend should hold this correspondence in mind throughout. libtpu does not instantiate `se::StreamExecutorMemoryAllocator` for runtime buffers; it reimplements the same responsibilities with TPU-specific types.

| StreamExecutor concept | libtpu bridge object |
|---|---|
| `se::DeviceMemoryAllocator::Allocate(ordinal, size, …)` | `xla::TpuClient::AllocateRawBuffer` (memory-space routed) |
| `Allocate` device-ordinal selection | `tpu::System::Allocate` map lookup `loc → allocator` |
| `se::DeviceMemoryBase` (opaque base + size) | `tpu::TpuSharedMemoryLocation` (chip, tier, host index) |
| `se::OwningDeviceMemory` / `ScopedDeviceMemory` (RAII) | `tpu::TpuBuffer` (`xla::TpuRawBuffer` PJRT wrapper) |
| `se::DeviceMemoryAllocator::Deallocate` | `tpu::TpuAllocator::Deallocate` (vt+0x30) |
| The concrete backing allocator | per-core `tpu::TpuAllocator` → `tpu::BestFitAllocator` |

> **QUIRK —** the `DeviceMemoryBase` analogue (`TpuSharedMemoryLocation`) is not a raw pointer-plus-size. It is a structured *identity* — `(chip, on-chip-memory-segment, host index)` — that doubles as the **hash-map key** selecting which core's allocator services the request (`operator==` `0x20ad6be0`, `AbslHashValue` `0x1d0ba2a0`). The "ordinal" and the "memory space" are fused into one key. A reimplementation that models the ordinal as a bare integer will mis-route VMEM/SMEM/CMEM tiers, because the tier is part of the same key, not a separate argument.

---

## The `Allocate` Path (PJRT_Buffer → Best-Fit Engine)

### Purpose

This is the bridge's forward direction: a PJRT buffer-create, an `ExecuteAsyncOnStream` output buffer, or a transfer-staging request enters at `AllocateRawBuffer`, is routed by memory space, and (for the device tier) descends to the per-core best-fit `Allocate`. Each hop is a thin adapter; the indirection exists so that one process-wide `System` can pool one allocator per core and share it across every PJRT client.

### Entry Point

```text
xla::TpuClient::AllocateRawBuffer (0xf7fb1e0)        ── Allocate ABI; routes by PjRtMemorySpace kind
  ├─ [HBM]  TpuClient::AllocateBuffer (0xf7fc5a0)    ── builds TpuSharedMemoryLocation (the ordinal/tier key)
  │    └─ tpu::AllocateBuffer (0xf8d51c0)            ── sync vs AllocateAfter; TraceMe; latency log
  │         └─ tpu::System::Allocate (0x1d0aeea0)    ── per-core map lookup → jmp *vt[+0x10]
  │              └─ TpuAllocator::Allocate (virtual) ── Reusing (0x1d0d2480) | Deferred (0x1d0ce900)
  │                   └─ TpuSharedMemory::AllocateLocked (0x1d4be920 / inner 0x1d4c0f40)
  │                        └─ BestFitAllocator::Allocate vt+0x30 (0x1e817820)  ── ENGINE (other page)
  ├─ [PINNED HOST]   tpu::System::AllocateHostBuffer (0x1d0af180)  ── pinned host (separate engine)
  └─ [UNPINNED HOST] xla::CpuRawBuffer::Allocate (0xf911680)       ── CPU-resident staging
```

### Algorithm

The router and the per-ordinal lookup are the two decisions a reimplementer must reproduce exactly. The rest of the chain is forwarding.

```c
function TpuClient_AllocateRawBuffer(memspace, size, is_tuple, dep):  // 0xf7fb1e0
    // The Allocate-ABI entry. Routing is by PjRtMemorySpace::kKindId, the
    // multi-memory-space equivalent of se::DeviceMemoryAllocator::Allocate.
    // `dep` is the AsyncValueRef<bool> allocate-after gate.
    // Tested in this order (no default device branch — HBM is the fallthrough):
    if memspace.kind() == UnpinnedHostMemorySpace::kKindId:   // CPU-resident staging
        CHECK(!dep)                               // LogFatal: allocate_after unsupported (unpinned host)
        return CpuRawBuffer_Allocate(memspace, size,
                                     CpuDeviceMemory_DefaultAllocator())  // 0xf911680
    if memspace.kind() != TpuHbmMemorySpace::kKindId:
        if memspace.kind() == PinnedHostMemorySpace::kKindId: // pinned host staging
            CHECK(!dep)                           // LogFatal: allocate_after unsupported (pinned host)
            return System_AllocateHostBuffer(loc, size, deleter)         // 0x1d0af180
        return error("Unsupported memory space: %s.")
    // else: TpuHbmMemorySpace  -> DEVICE
    return TpuClient_AllocateBuffer(memspace.device(), size, is_tuple, dep) // 0xf7fc5a0

function TpuClient_AllocateBuffer(dev, size, is_tuple, dep):           // 0xf7fc5a0
    // Build the DeviceMemoryBase-equivalent key: (chip, tier, host index).
    loc = dev.LocalSharedMemory(kHbm).index_on_host()  // 0x20ad6840 / 0x20ad6e00
    pel = PendingEventLoggers_get(loc.index())         // 0x1d0a89a0
    jitter = SharedBitGen()()                          // 0xf822540  (retry/backoff seed)
    return tpu_AllocateBuffer(loc, size, /*sync*/, /*async*/,
                              dep, System*, work_queue, pel)            // 0xf8d51c0
    // on ResourceExhausted: LogOnResourceExhausted (0xf7fc4c0) then
    //   error::RuntimeBufferAllocationFailure (0xf7fd3a0)

function System_Allocate(this, loc, size):             // 0x1d0aeea0  (byte-confirmed)
    // The per-ordinal router == se::DeviceMemoryAllocator's ordinal step.
    // map type: flat_hash_map<TpuSharedMemoryLocation, unique_ptr<TpuAllocator>>
    alloc = map.at(this->allocators_, loc)             // raw_hash_map::at(*loc+48, ...)
    // tail-call the matched allocator's virtual Allocate (vtable slot +0x10):
    return alloc->vtable[+0x10](this, alloc, size)     // jmp *0x10(%rax)
```

> **NOTE —** `tpu::AllocateBuffer` (`0xf8d51c0`) chooses between `System::Allocate` (synchronous, `0x1d0aeea0`) and `System::AllocateAfter` (`0x1d0af060`) based on whether an `AsyncValueRef<bool>` dependency gates the allocation — the `tpu_allow_async_allocations` config path. `AllocateAfter` queues the allocation behind an in-flight free; the two `bool` arguments select sync-vs-after and the async toggle. The vtable also carries `AllocateAfter` at slot +0x18 so the async variant routes through the same per-core allocator.

### Decompile Cross-Check — the router tail-call

The central claim — that `System::Allocate` is a map lookup followed by a vtable tail-call to slot `+0x10` — is byte-confirmed:

```c
// _ZN3tpu6System8AllocateERKNS_23TpuSharedMemoryLocationEl @ 0x1d0aeea0
__int64 tpu::System::Allocate(System *this, const TpuSharedMemoryLocation *a2,
                              __int64 size, __int64 a4) {
  _QWORD *v5 = raw_hash_map<FlatHashMapPolicy<TpuSharedMemoryLocation,
                 unique_ptr<TpuAllocator>>>::at(*(_QWORD*)a2 + 48LL, size);
  //          map handle lives at *loc+48; .at() returns &unique_ptr<TpuAllocator>
  return (*(__int64(**)(System*, _QWORD, __int64))
            (*(_QWORD*)*v5 + 16LL))(this, *v5, a4);   // jmp *vt[+0x10] = TpuAllocator::Allocate
}
```

Slot `+0x10` of the `TpuAllocator` vtable is `Allocate`; the function tail-calls it with the unwrapped `unique_ptr` payload (`*v5`) as `this`. This is the `Allocate` ABI boundary between the router and the strategy.

### Function Map

| Function | Address | Role |
|---|---|---|
| `xla::TpuClient::AllocateRawBuffer` | `0xf7fb1e0` | `Allocate` ABI entry; memory-space routing |
| `xla::TpuClient::AllocateBuffer` | `0xf7fc5a0` | Device branch; builds `TpuSharedMemoryLocation` |
| `tpu::AllocateBuffer` | `0xf8d51c0` | sync/`AllocateAfter` select; trace + latency |
| `tpu::System::Allocate` | `0x1d0aeea0` | Per-ordinal map lookup → vt+0x10 tail-call |
| `tpu::System::AllocateAfter` | `0x1d0af060` | Async-gated variant (vt+0x18) |
| `tpu::TpuSharedMemory::AllocateLocked` | `0x1d4be920` / `0x1d4c0f40` | Dispatches `BestFitAllocator::Allocate` (vt+0x30) |
| `xla::CpuRawBuffer::Allocate` | `0xf911680` | CPU-resident memory-space branch |

---

## The `TpuAllocator` Virtual Interface (the `Allocate`/`Deallocate` ABI)

### Purpose

`tpu::TpuAllocator` is the abstract base whose 6-slot vtable *is* the bridge ABI between the per-ordinal router and the engine. It is "FFI-shaped" — a stable virtual table that the router tail-calls without knowing which concrete strategy backs a given core. Two strategies implement it; both ultimately hand physical placement to the per-core `TpuSharedMemory` (and thus to `BestFitAllocator`).

### The vtable

Recovered from the `DeferredTpuAllocator` vtable at `0x21ca84a8` (the `ReusingTpuAllocator` mirror at `0x21ca85d8` has the same shape):

| Slot | Method | Deferred impl | Reusing impl |
|---|---|---|---|
| `vt[+0x00]` | `~dtor` (deleting) | `0x1d0ce4c0` | — |
| `vt[+0x08]` | `~dtor` (complete) | `0x1d0ce8c0` | — |
| `vt[+0x10]` | `Allocate(long)` | `0x1d0ce900` | `0x1d0d2480` |
| `vt[+0x18]` | `AllocateAfter(long, RCRef<AsyncValue>)` | `0x1d0ce920` | `0x213ba520` |
| `vt[+0x20]` | `RegisterSequencedOutOfMemoryHold(RCRef<AsyncValue>)` | `0x1d0cec80` | `0x1d0d28c0` |
| `vt[+0x28]` | `Shutdown(bool)` | `0x1d0cf360` | `0x1d0d28e0` |
| `vt[+0x30]` | `Deallocate(TpuBuffer*, InlinedVector<RCRef<AsyncValue>,2>)` | `0x1d0cf460` | `0x1d0d2c00` |

> **QUIRK —** `Allocate` is at vt+0x10, but `Deallocate` is at vt+**0x30**, not adjacent. The two intervening slots are `AllocateAfter` and the OOM-hold/`Shutdown` pair. A reimplementation that assumes `Allocate`/`Deallocate` are vtable-adjacent (as in many `se::DeviceMemoryAllocator` layouts) will mis-dispatch frees. The free side carries an `InlinedVector<RCReference<AsyncValue>,2>` of completion events so a deallocation can be sequenced after in-flight uses — this is the deferred-deallocation hook.

### The two strategies

```c
function TpuAllocator_Create(shared_mem, strategy, tracker):  // 0x1d0ce420 (byte-confirmed)
    if strategy == 2:                          // kReusing
        a = operator new(0x38)                 //  56-byte instance
        a.vptr      = ReusingTpuAllocator_vtable + 0x10   // off_21CA85E8
        a[+0x08]    = shared_mem                // the per-core TpuSharedMemory
        a[+0x10..]  = { 0, 1, 0 }              // reuse-cache bookkeeping
    else:                                      // kDeferred (default)
        a = operator new(0xB0)                 // 176-byte instance
        a.vptr      = DeferredTpuAllocator_vtable + 0x10   // off_21CA84B8
        a[+0x08]    = shared_mem
        zero a[+0x10 .. +0xA0]                 // tracker/bitset state
        a[+0xA8]    = tracker                  // SystemEventTracker* (result[21])
    return a
```

- **`ReusingTpuAllocator::Allocate`** (`0x1d0d2480`) → `AllocatorFor(size)` (`0x1d0d2e80`): picks a per-size-class sub-allocator / reuse cache under a shared mutex and hands back a recycled freed buffer when one fits, allocating fresh otherwise. This is the size-class reuse strategy — the closest analogue to a caching `se` allocator.
- **`DeferredTpuAllocator::Allocate`** (`0x1d0ce900`) → `AllocateImpl(size)` (`0x1d0cf620`): the verified body is a one-line forward to `AllocateImpl` then `return this`. `AllocateImpl` takes a mutex (`absl::Mutex::lock` on `this+0x10`), tracks the allocation, and guards against a missing core with `MakeErrorImpl<13>` (`absl::StatusCode::kInternal`, message `"No attached TPU to allocate with."` — string-anchored to `tpu_allocator.cc`); physical placement is delegated through indirect vtable calls into the owning `TpuSharedMemory`/driver object, where the actual no-room `ResourceExhausted`/fragmentation diagnostic is produced (in `AllocateLocked`, below). Deferred frees are batched and reaped — the "deferred deallocation" name.

```c
// _ZN3tpu12_GLOBAL__N_120DeferredTpuAllocator8AllocateEl @ 0x1d0ce900 (byte-confirmed)
DeferredTpuAllocator *Allocate(DeferredTpuAllocator *this, __int64 size, __int64 a3) {
    DeferredTpuAllocator::AllocateImpl(this, size, a3);   // 0x1d0cf620
    return this;
}
```

> **NOTE —** `TpuAllocator::Create` (`0x1d0ce420`) is byte-confirmed: `strategy == 2` selects the 56-byte `ReusingTpuAllocator`, anything else the 176-byte `DeferredTpuAllocator`, with the `TpuSharedMemory*` stored at `+0x08` and (deferred only) a `SystemEventTracker*` at `+0xA8`. The exact concrete field layout beyond these three slots — the reuse-cache map, the per-allocation tracker bitset — was not field-decoded (LOW confidence on field meanings past `+0x08`/`+0xA8`).

### Where the call lands (and stops)

`TpuSharedMemory::AllocateLocked` (`0x1d4be920`, inner `0x1d4c0f40`) is the last hop this page owns. It is mutex-protected, fetches the engine's stats for the OOM diagnostic (`BytesAllocated` vt+0x18, `BytesReserved` vt+0x20, `BytesAvailable` vt+0x68, `BytesAllocatable` vt+0x78, `GetFragmentation` vt+0x80), then dispatches:

```c
function TpuSharedMemory_AllocateLocked(this, size):   // 0x1d4be920 / inner 0x1d4c0f40
    engine = this->best_fit_                            // per-tier BestFitAllocator
    offset = engine->vtable[+0x30](engine, aligned_size) // call *0x30(%rax) -> 0x1e817820
    //  ^^^ BEST-FIT ALLOCATE — algorithm owned by ../memory/hbm-allocator.md
    return TpuBuffer(loc, offset, ...)                  // wrap offset into the owning handle
```

The decompile confirms the `+ 48` (0x30) dispatch in the `AllocateLocked` body — `(**(this+192) + 48LL)(…)`, where `this+192` is the per-tier `best_fit_` engine pointer (the inner `$_0` at `0x1d4c0f40` is the OOM-stats diagnostic builder, reading `BytesReserved` vt+0x20, `BytesAllocated` vt+0x18, `BytesAvailable` vt+0x68, `BytesAllocatable` vt+0x78, `GetFragmentation` vt+0x80). Everything past the `+0x30` call — the RB-tree best-fit search, `SplitBlock`, coalescing, the alignment round-up — is on [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md).

---

## The `Deallocate` Path (`ScopedDeviceMemory` Release)

### Purpose

The `tpu::TpuBuffer` is the owning handle — the `se::OwningDeviceMemory` / `ScopedDeviceMemory` analogue. When the last reference drops, the free routes back through the same `TpuAllocator` vtable as the allocation came in, at slot `+0x30`. There is no separate `se::DeviceMemoryAllocator::Deallocate(ordinal, mem)`; the buffer carries its own `allocator()` and `location()` so the free is self-routing.

### Algorithm

```c
function ~TpuBuffer():                                  // owning-handle destructor (Owned variant)
    if variant == Sliced or variant == Unsafe: return   // views/unsafe do not own
    alloc = this->allocator()                           // 0x1d102680  -> TpuAllocator*
    loc   = this->location()                            // 0x1d102740  -> TpuSharedMemoryLocation
    alloc->vtable[+0x30](alloc, this, pending_events)   // TpuAllocator::Deallocate (vt+0x30)
        // Deferred: 0x1d0cf460 — may batch the free, gated by the deferred-dealloc flags
        // Reusing:  0x1d0d2c00 — returns the block to its size-class reuse cache
            // -> TpuSharedMemory::DeallocateLocked -> BestFitAllocator::Deallocate (vt+0x38)
            //    coalesce-on-free is the engine's job (other page)
```

> **GOTCHA —** the `Owned`/`Sliced`/`Unsafe` variant tag (`TpuBufferTraits`) gates whether the destructor frees. A `Sliced` buffer is a sub-buffer *view* (`Slice` `0x1d1017c0` / `0x1d101f60` / `0x1d102f60`) that shares the parent's allocation; an `Unsafe` buffer (`IsUnsafe` `0x1d102800`, `UnsafeReleaseBuffer` `0x1d102660`) has had ownership released. Only `Owned` frees. A reimplementation that frees on every destructor will double-free every sliced view. The variant model is on [`../memory/tpu-buffer-layout.md`](../memory/tpu-buffer-layout.md).

Deferred deallocation is the one behavior that diverges from a plain `ScopedDeviceMemory`: instead of freeing immediately, `DeferredTpuAllocator::Deallocate` (`0x1d0cf460`) can enqueue the free, gated by `FLAGS_tpu_deferred_deallocation` (`0x22396ee0`) / `FLAGS_tpu_reap_deferred_deallocations` (`0x22396f40`) / `…_try_wait` (`0x22397008`). A pending free can then satisfy a blocked allocation via `RegisterSequencedOutOfMemoryHold` (vt+0x20) — the "sequenced" hold chained on an `AsyncValue`.

---

## Memory-Space and Ordinal Routing

### Purpose

The bridge's routing has two axes a StreamExecutor reader collapses into one: the **memory-space kind** (which backend) and the **ordinal/tier** (which core, which on-chip tier). `AllocateRawBuffer` decides the first; `System::Allocate`'s map key decides the second.

### Axis 1 — memory-space kind (the backend switch)

| `PjRtMemorySpace::kKindId` | Backend | Engine | This page? |
|---|---|---|---|
| `TpuHbmMemorySpace` (fallthrough) | `TpuClient::AllocateBuffer` → `System::Allocate` | `tpu::BestFitAllocator` | OWNS the bridge |
| `PinnedHostMemorySpace` | `tpu::System::AllocateHostBuffer` (`0x1d0af180`) | premapped pool / `HostBufferPool` | adjacent |
| `UnpinnedHostMemorySpace` | `xla::CpuRawBuffer::Allocate` (`0xf911680`) | `CpuDeviceMemory::DefaultAllocator` | adjacent |
| anything else | `MakeRep` + `"Unsupported memory space: %s."` | — | error |

The device branch is the one this page traces end-to-end. The host and CPU-staging branches are named here because `AllocateRawBuffer` is the shared `Allocate` entry that selects among them by `kKindId`; their engines are separate subsystems. The `allocate_after` async dependency is rejected (`LogFatal`) for both host kinds — only the HBM device branch carries it.

### Axis 2 — the `TpuSharedMemoryLocation` key (the ordinal/tier)

The router's map is `flat_hash_map<TpuSharedMemoryLocation, unique_ptr<TpuAllocator>>` — **one `TpuAllocator` per core**. The key encodes `(chip, on-chip-memory-segment, host index)`:

```c
struct tpu::TpuSharedMemoryLocation {   // the DeviceMemoryBase identity
    // ctor 0x20ad6ae0: (TpuTopology*, TpuDimensions, TpuDimensions, TpuSharedMemoryOnChip)
    // accessors: Chip(), index_on_host() (0x20ad6e00), ToString() (0x20ad6f00)
    // operator== 0x20ad6be0 ; AbslHashValue 0x1d0ba2a0  -> these make it a hash key
};
```

Because the tier (`TpuSharedMemoryOnChip`) is *inside* the key, HBM, VMEM, SMEM, CMEM, and SFLAG each map to a different `TpuAllocator`/`BestFitAllocator` instance — the same `System::Allocate` code path serves every tier, distinguished only by the key. This is why [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md) can say one `BestFitAllocator` class backs every tier: the bridge selects the per-tier instance here, by key, before the engine ever runs.

---

## How `ExecuteAsyncOnStream` Enters This Bridge

`ExecuteAsyncOnStream`'s output-buffer allocation is the primary runtime caller of this bridge. The full dispatch/launch flow is on [`execute-async-on-stream.md`](execute-async-on-stream.md); here is only the entry seam.

For each output `ShapeIndex`, the TPU output allocator decides reuse-vs-fresh, and fresh allocations call straight into this bridge:

```text
ExecuteAsyncOnStream  (execute-async-on-stream.md)
  └─ tfrt::tpu::AllocateOutputBuffersWithInputReuse (0xf7ba9a0)
       ├─ aliased output  → REUSE the donated input's TpuBuffer in place   (no allocation)
       │                    (HloInputOutputAliasConfig::GetAliasedParameter 0x1e580200)
       │                    → buffer-donation-aliasing.md
       └─ non-aliased     → tfrt::tpu::AllocateTpuBufferWithRetry (0xf7ec6a0)
                              └─ tpu::System::Allocate (0x1d0aeea0)  ── THIS BRIDGE
```

> **NOTE —** the donation/aliasing decision (which outputs reuse which inputs, the `ScopedHold` pins, `HloInputOutputAliasConfig`) belongs to [`../memory/buffer-donation-aliasing.md`](../memory/buffer-donation-aliasing.md). This page's interest is only the *non-aliased* branch — the one that performs a real allocation through `System::Allocate`. The aliased branch performs **no** allocation; it rebinds the donated input's `TpuBuffer` as the output handle.

`AllocateTpuBufferWithRetry` (`0xf7ec6a0` → `0xf7ed620` → `0xf7ed980` → `0xf7edd80`, a recursive chain) wraps `System::Allocate` with defragment-and-retry: on `ResourceExhausted` it calls `tpu::System::CompactMemory` (`0x1d0b6000`) then retries. The exact retry count and `SharedBitGen` backoff jitter were not byte-traced (LOW). The terminal PJRT-visible error is `xla::error::RuntimeBufferAllocationFailure` (`0xf7fd3a0`); the leaf message is the `BestFitAllocator` fragmentation diagnostic on [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md).

---

## Allocator Lifetime (one engine, every client)

The bridge's allocators are **not per-PJRT-client**. They are pooled per-core at a process-wide singleton and shared:

```text
xla::GetSingletonTpuStatesManager (0xf958360, mutex + __cxa_guard)
  └─ xla::TpuStatesManager (one per process)
       └─ GetOrCreateTpuSystemState (0xf956e40) → xla::TpuSystemState
            └─ xla::CreateTpuSystemState → the singleton tpu::System
                 └─ flat_hash_map<TpuSharedMemoryLocation, unique_ptr<TpuAllocator>>
                      (ONE TpuAllocator per core, shared across every TpuClient)
```

Every `xla::TpuClient` in the process shares the same `TpuSystemState`/`System` (gated by the `use_global_tpu_system` config, string `0xa2e9960`). Therefore the device allocators are per-device(core), pooled at the `System` layer, and shared across all PJRT clients — the opposite of a per-`se::StreamExecutor` allocator instance.

> **QUIRK —** this is the deepest divergence from the StreamExecutor model. A `se::StreamExecutorMemoryAllocator` is typically owned by one client/executor; here the `Allocate`/`Deallocate` ABI is intentionally routed to a *shared* per-core allocator so two PJRT clients targeting the same core contend for one free-list. A reimplementation that instantiates one allocator per client will not see the same fragmentation or OOM behavior, because the real one pools across clients.

---

## Considerations for a Reimplementer

- **The four-hop indirection is deliberate, not accidental.** `AllocateRawBuffer` → `AllocateBuffer` → `tpu::AllocateBuffer` → `System::Allocate` exists so the PJRT-visible ABI is stable while the per-core allocator, the async-vs-sync choice, and the trace/latency instrumentation are layered in. Collapsing it loses the `AllocateAfter` async path and the per-core sharing.
- **Routing is by key, twice.** Memory-space kind picks the backend at `AllocateRawBuffer`; the `TpuSharedMemoryLocation` picks the per-core allocator at `System::Allocate`. Both are required; neither is a plain ordinal.
- **The handle owns the free.** Unlike `se::DeviceMemoryAllocator::Deallocate(ordinal, mem)`, libtpu's `TpuBuffer` carries `allocator()` and `location()`, so the free is self-routing through vt+0x30 — but only for the `Owned` variant.
- **Do not re-derive the engine.** The call lands at `BestFitAllocator::Allocate` (vt+0x30) and `Deallocate` (vt+0x38); the algorithm is [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md). This page stops at the dispatch.

---

## Cross-References

- [`../memory/hbm-allocator.md`](../memory/hbm-allocator.md) — the best-fit-with-coalescing engine this bridge calls into (`Allocate` vt+0x30, `Deallocate` vt+0x38); owns the free-list algorithm
- [`../memory/tpu-buffer-layout.md`](../memory/tpu-buffer-layout.md) — `tpu::TpuBuffer` field layout, the `Owned`/`Sliced`/`Unsafe` variant, `TpuSharedMemoryLocation` encoding
- [`../memory/buffer-donation-aliasing.md`](../memory/buffer-donation-aliasing.md) — `ScopedHold`, `HloInputOutputAliasConfig`, the aliased-output reuse path that bypasses allocation
- [`execute-async-on-stream.md`](execute-async-on-stream.md) — the runtime caller; where output-buffer allocation enters this bridge
- [`load-program-enqueue.md`](load-program-enqueue.md) — program load, the pre-planned static scratch binding, and the in-flight-execution semaphore (not a device-memory reservation)
- [`overview.md`](overview.md) — the runtime/execution subsystem map
