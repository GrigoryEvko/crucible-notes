# TDRV: Tensor Object Layer

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`; build-id `8bb57aba…`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF `debug_info`; `.text` VMA == file offset, so every `0x30…` is an analysis VMA. Struct offsets are DWARF `data_member_location` values (decimal). Provenance string `/opt/workspace/KaenaRuntime/tdrv/tensor.c` (`@0x819428`) roots every function. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — struct layout from DWARF, refcount/dispatch logic from x86-64 disassembly, call edges from the IDA call graph. · Part IV — TDRV Runtime · [back to index](../index.md)*

## Abstract

`tdrv/tensor.c` is the object layer behind the public `nrt_tensor_*` API. It owns one thing: the `nrt_tensor_t` — a runtime handle for a contiguous span of bytes that the device can read or write. Every host↔device transfer (`nrt_tensor_read`/`_write`/`_copy`/`_memset`), every model input/output binding, every checksum and input-dump in the runtime resolves to a `nrt_tensor_t` and passes through the 24 functions in this band (`0x30e1d0`–`0x310418`).

The data model is a deliberate two-level split that a reimplementer must reproduce exactly. A **`nrt_tensor_t` (192 bytes)** is a *view*: a name, a pointer to backing storage, a `(_offset, _size)` window into it, and a view-level refcount. A **`nrt_tensor_storage_t` (320 bytes)** is the *backing*: a tagged union over three memory kinds (`MALLOC` host buffer / `DMA` device-DRAM `dmem_t` / `FAKE` size-only placeholder), guarded by its own refcount, mutex and condition variable. Many views can share one storage — this is how `tensor_set_slice` makes a sub-tensor without copying. The split is the LLVM analogue of an `ArrayRef`/`SmallVector` separation: the view is cheap and copyable, the storage owns the bytes and the lifetime. Two **independent** refcounts implement the two sharing axes: `tensor->ref_count` (view sharing) and `storage->ref_count` (storage sharing across slices).

The single most important property — and the one most likely to surprise a reimplementer — is that **a tensor is byte-untyped at runtime**. There is no `dtype` field and no `shape` array anywhere in `nrt_tensor_t` or `nrt_tensor_storage_t`. The layer copies, slices, checksums and memsets raw bytes; element type and dimensionality live only in NEFF metadata and are surfaced by a *different* call path (`nrt_get_model_tensor_info`, see [api-tensors](api-tensors.md)). This page documents the object model, the alloc/slice/free lifecycle and its two refcounts, the type-dispatched host/device I/O, host-VA / device-PA addressing, and the async-execution fence bookkeeping (`pending_exec_count_*` and the process-global `output_completion_count` plane).

For reimplementation, the contract is:

- **The two-struct object model** — `nrt_tensor_t` (view: `name / sto / _offset / _size / extra / ref_count@64 / output_completion_count@128`) over `nrt_tensor_storage_t` (backing: `hbm_idx@0 / allocated_size@8 / type@16 / union@24 / ref_count@64 / mem_owned_by_tensor@72 / mutex@80 / cond@120 / pending_read@192 / pending_write@256 / vtpb_idx@264`).
- **The tagged-union discriminant** — `type ∈ {INVALID=0, MALLOC=1, DMA=2, FAKE=3}` selects how `union@24` is interpreted and how *every* operation dispatches.
- **Two refcounts, two destructors** — `tensor_get_reference`/`tensor_free` atomically move the view count at `tensor+0x40`; `tensor_set_slice`/`tensor_free_storage` atomically move the storage count at `sto+0x40`; storage is freed only when *its* count hits zero, and only then are its mutex/cond destroyed.
- **The byte-untyped invariant** — no dtype, no shape; size is the only quantity the layer knows, and `tensor_get_size` returns `_size`.
- **The async fence plane** — `tensor_async_update` bumps the per-storage in-flight read/write counters under `tensor_op_cv_lock` and broadcasts on drain; the I/O ops fence against it before touching device memory.

| | |
|---|---|
| **Source** | `/opt/workspace/KaenaRuntime/tdrv/tensor.c` (`@0x819428`); `inc/tdrv/tensor.h` (`tensor_get_size`) |
| **Band** | `0x30e1d0`–`0x310418` — 24 functions (`nm -n` confirmed) |
| **View struct** | `nrt_tensor_t` — **192 B** (`calloc(1, 0xC0)`), DWARF ordinal 8714 |
| **Backing struct** | `nrt_tensor_storage_t` — **320 B** (`calloc(1, 0x140)`), DWARF ordinal 8710 |
| **Union discriminant** | `type` @+16: `{INVALID=0, MALLOC=1, DMA=2, FAKE=3}` (`nrt_tensor_mem_type_t`) |
| **View refcount** | `tensor+0x40` — `lock add/sub`, get_reference / free |
| **Storage refcount** | `sto+0x40` — `lock add/sub`, set_slice (++) / free_storage (--) |
| **Async fence counters** | `sto+0xC0` (read) / `sto+0x100` (write), under mutex `sto+0x50`, cond `sto+0x78` |
| **Completion plane** | process-global `output_completion_lock` @`0xca7280`, `output_completion_cond` @`0xca72c0` |
| **Allocator** | `tensor_allocate` @`0x30e8a0` — DMA→`dmem_alloc`, MALLOC→`malloc`, FAKE→none |

---

## 1. The Object Model

### Purpose

A `nrt_tensor_t` answers one question for the rest of the runtime: *where are these bytes, and how do I read or write them?* It deliberately separates the **handle** (cheap, refcounted, sliceable) from the **backing** (owns the bytes and the OS resources). The separation lets the loader build hundreds of feature-map views over a handful of physical DRAM allocations, and lets a slice alias its parent's storage with a single atomic increment instead of a copy.

### `nrt_tensor_t` — the view (192 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `name` | +0 (0x00) | `char*` | `strdup` of caller name, or `"_unknown_"` if NULL | HIGH |
| `sto` | +8 (0x08) | `nrt_tensor_storage_t*` | backing storage; NULL until built/attached | HIGH |
| `_offset` | +16 (0x10) | `size_t` | byte offset of this view into `sto`; `!=0` only for slices | HIGH |
| `_size` | +24 (0x18) | `size_t` | logical view length; the value `tensor_get_size` returns | HIGH |
| `extra` | +32 (0x20) | `void*` | zero-initialized; untouched by this layer | HIGH |
| `ref_count` | +64 (0x40) | `volatile uint64_t` | **view** refcount; atomic in get_reference/free | HIGH |
| `output_completion_count` | +128 (0x80) | `volatile uint64_t` | async-exec completion counter (§5) | HIGH |

> **QUIRK —** there is **no dtype and no shape** in this struct. The gaps (0x28–0x3F, 0x48–0x7F, 0x88–0xBF) are not padding the layer ignores arbitrarily — a `nrt_cc_context_t` member appears in the DWARF DIE within that range but is never touched by `tensor.c`. The element type, element count, and dimensionality of the tensor live exclusively in NEFF metadata; see the byte-untyped callout in §3.

### `nrt_tensor_storage_t` — the backing (320 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `hbm_idx` | +0 (0x00) | `uint32_t` | HBM/DRAM channel index; `-1` (`0xFFFFFFFF`) for host buffers | HIGH |
| `allocated_size` | +8 (0x08) | `size_t` | full size of the backing allocation | HIGH |
| `type` | +16 (0x10) | `nrt_tensor_mem_type_t` | union discriminant `{INVALID,MALLOC,DMA,FAKE}` | HIGH |
| `dmem` / `vmem` (union) | +24 (0x18) | `dmem_t*` \| `void*` | DMA→`dmem_t*`; MALLOC→raw host buffer | HIGH |
| `ref_count` | +64 (0x40) | `volatile uint64_t` | **storage** refcount; slices share | HIGH |
| `mem_owned_by_tensor` | +72 (0x48) | `bool` | `1` if `tensor_allocate` owns the backing (frees it) | HIGH |
| `tensor_op_cv_lock` | +80 (0x50) | `pthread_mutex_t` (40 B) | guards the fence counters | HIGH |
| `tensor_op_cv` | +120 (0x78) | `pthread_cond_t` (48 B) | broadcast on fence drain | HIGH |
| `pending_exec_count_read` | +192 (0xC0) | `volatile uint64_t` | in-flight device **reads** of this storage | HIGH |
| `pending_exec_count_write` | +256 (0x100) | `volatile uint64_t` | in-flight device **writes** of this storage | HIGH |
| `vtpb_idx` | +264 (0x108) | `int32_t` | owning virtual-TPB index; `-1` default | HIGH |

> **NOTE —** IDA aliases `nrt_tensor_storage_t*` as `pthread_mutex_t*` in several build paths (the embedded mutex at +0x50 dominates the type inference), so decompiled writes like `v17->__data.__kind = 2` or `__data.__list.__prev = …` are really stores to `type` @+16 and the `union` @+24. The offsets above are reconciled against DWARF `data_member_location`, not the IDA struct alias.

### The tagged union and its discriminant

`type` @+16 is the discriminant for the union at +24 and the dispatch key for *every* operation in the layer. The DWARF enum (`nrt_tensor_mem_type_t`):

| Value | Name | `union@24` is… | Host/device I/O |
|---|---|---|---|
| 0 | `INVALID` | — (uninitialized) | asserted against everywhere |
| 1 | `MALLOC` | raw host `void*` (libc `malloc` or user buffer) | `memcpy` |
| 2 | `DMA` | a real `dmem_t*` (device DRAM) | `dmem_buf_copyin`/`copyout` |
| 3 | `FAKE` | NULL (size-only placeholder) | silent no-op, returns `NRT_SUCCESS` |

> **GOTCHA —** in `tensor_get_device_allocation_info` the decompiler prints `type != NRT_INVALID`, but `NRT_INVALID == 2` in the *status*-code enum collides numerically with `NRT_TENSOR_MEM_TYPE_DMA == 2`. The test actually means `type == DMA`. This is an IDA enum-aliasing artifact, not a runtime bug — a reimplementer reading the decompile literally would invert the predicate.

### The `dmem_t` backing (referenced, owned elsewhere)

`DMA` storage points at a `dmem_t` (192 B, owned by [tdrv-dmem](tdrv-dmem.md)). Only six fields are read by this layer; their offsets are now DWARF-exact (the original notes marked them MED — disassembly of `tensor_get_pa`/`_get_va` confirms them HIGH):

| Field | Offset | Used by | Confidence |
|---|---|---|---|
| `_pa` | +24 (0x18) | `tensor_get_pa` (device physical base) | HIGH |
| `_va` | +32 (0x20) | `tensor_get_va` via `DMEM_GET_VA` | HIGH |
| `align_offset` | +40 (0x28) | `tensor_get_pa` (added into PA) | HIGH |
| `mem_type` | +56 (0x38) | `tensor_get_tensor_placement` (`dma_mem_location_t`) | HIGH |
| `tdram_channel` | +64 (0x40) | `tensor_get_device_allocation_info` → `hbm_index` | HIGH |
| `allocated_size` | +48 (0x30) | — | HIGH |

`dma_mem_location_t` is `{DMA_MEM_LOC_INVALID=0, HOST_DRAM=1, TONGA_DRAM=2}` (DWARF). Device-resident tensors carry `TONGA_DRAM`.

---

## 2. Lifecycle: Alloc, Slice, Free

### Purpose

Three operations create a `nrt_tensor_t` (allocate / attach a user buffer / slice an existing tensor) and one destroys it (`tensor_free`). All four route storage construction and teardown through a small set of helpers so that the two refcounts stay balanced. The unifying rule: **`tensor_allocate_empty` always makes the 192-byte view; `tensor_build_user` or `dmem_alloc` makes the 320-byte storage; `tensor_free` unwinds the view, and `tensor_free_storage` unwinds the storage when its refcount drains.**

### Entry Point

```text
nrt_tensor_allocate (0xbc320)                       ── public alloc
  └─ tensor_allocate (0x30e8a0)                      ── central allocator, type-dispatched
       ├─ db_physical_core_get_mla_and_tpb (0x2272a0)   [DMA] resolve owning TPB
       ├─ vtpb_get_default_hbm_idx (0x314440)           [DMA] pick HBM channel
       ├─ dmem_alloc (0x228ed0)                          [DMA] device-DRAM allocation
       ├─ malloc                                         [MALLOC] host buffer
       ├─ tensor_allocate_empty (0x30e310)               make the 192B view
       └─ tensor_build_user.part.0 (0x30e730)            [MALLOC/FAKE] make + attach storage

nrt_tensor_allocate_slice (0xbf980)                 ── public slice
  └─ tensor_allocate_empty (0x30e310)
  └─ tensor_set_slice (0x30e530)                     ── share parent storage (++sto->ref_count)

nrt_tensor_free (0xbd0e0)
  └─ tensor_free (0x30e630)
       └─ tensor_free_storage (0x30e1d0)             ── runs only when sto->ref_count hits 0
```

### Algorithm — `tensor_allocate_empty` (make the view)

```c
// tensor_allocate_empty @0x30e310 — the only 192-byte view constructor.
function tensor_allocate_empty(name, out_tensor):
    *out_tensor = NULL                               // 0x30e32d
    t = calloc(1, 0xC0)                               // 192 bytes, zeroed (0x30e339)
    if t == NULL:
        nlog_write("Failed to allocate memory for tensor %s")
        return NRT_RESOURCE                           // 4  (0x30e3b0)
    t->name = strdup(name ? name : "_unknown_")       // 0x30e349 — NULL ⇒ "_unknown_"
    if t->name == NULL:
        free(t); nlog_write("…tensor name %s"); return NRT_RESOURCE
    t->ref_count = 1                                  // +0x40 (0x30e356) — view starts at 1
    t->extra     = 0                                  // +0x20
    t->output_completion_count = 0                    // +0x80
    t->_offset = 0; t->_size = 0                       // +0x10/+0x18 (movaps, 16B)
    *out_tensor = t
    return NRT_SUCCESS                                // 0
```

The view is born with `ref_count == 1` and **no storage** (`sto` stays NULL until a builder attaches one). A bare empty tensor is therefore a valid-but-unbacked handle.

### Algorithm — `tensor_build_user` (make MALLOC/FAKE storage)

```c
// tensor_build_user.part.0 @0x30e730 — sole caller is tensor_allocate.
// `mem` is the host buffer pointer (NULL ⇒ FAKE placeholder).
function tensor_build_user(out_tensor_slot, name, mem, size):
    sto = calloc(1, 0x140)                            // 320 bytes, zeroed (0x30e74e)
    if sto == NULL: { nlog_write("…tensor storage %s"); return NRT_RESOURCE }
    // type = mem ? MALLOC(1) : FAKE(3)  — computed by cmp/sbb/and 2/+1 (0x30e75c..0x30e77a)
    sto->ref_count      = 1                            // +0x40
    sto->union@24       = mem                          // +0x18 — host buffer or NULL
    sto->allocated_size = size                         // +0x08
    sto->type           = mem ? MALLOC : FAKE          // +0x10
    sto->hbm_idx        = 0xFFFFFFFF                   // +0x00 — host, no HBM channel
    sto->vtpb_idx       = -1                           // +0x108
    sto->pending_exec_count_read  = 0                  // +0xC0
    sto->pending_exec_count_write = 0                  // +0x100
    if pthread_mutex_init(&sto->tensor_op_cv_lock, NULL) != 0:   // +0x50
        free(sto); tensor_free(*out_tensor_slot)
        nlog_write("Failed to allocate async lock for tensor %s"); return NRT_FAILURE  // 1
    if pthread_cond_init(&sto->tensor_op_cv, NULL) != 0:          // +0x78
        … (symmetric cleanup) …
    t = *out_tensor_slot                              // attach storage to the view
    t->_offset = 0; t->_size = size                   // +0x10/+0x18
    t->sto     = sto                                  // +0x08
    t->output_completion_count = 0                    // +0x80
    return NRT_SUCCESS
```

> **NOTE —** `tensor_build_user` does **not** set `mem_owned_by_tensor`. Only `tensor_allocate` sets `sto->mem_owned_by_tensor = 1` (`@0x30e962`) after a successful build, because only the allocate path owns the buffer it created. A user-attached buffer (`nrt_tensor_attach_buffer` → `tensor_set_to_user_buffer`) leaves it `0`, so `tensor_free_storage` will *not* free the caller's memory.

### Algorithm — `tensor_allocate` (the central, type-dispatched allocator)

```c
// tensor_allocate @0x30e8a0 — dispatches on the requested mem_type.
function tensor_allocate(name, type, size, …, out_tensor):
    nrt_sys_trace_new_event(ev=27 /*0x1B alloc*/, …) // 0x30e8fe
    switch type:
      case DMA:
        core = db_physical_core_get_mla_and_tpb(…)    // 0x30e9fe
        hbm  = vtpb_get_default_hbm_idx(…)            // 0x30ea0d
        dm   = dmem_alloc(tpb_allocator, size, …, DMA_MEM_USAGE_TYPE_TENSOR/*=12*/)  // 0x30ea38
        if dm == NULL: nlog_write("Failed to allocate %lu bytes on %s …"); goto fail
        tensor_allocate_empty(name, out_tensor)       // 0x30ecc-class path
        sto = calloc(1, 0x140)                         // 0x30eb52
        sto->dmem = dm; sto->type = DMA (=2)           // +0x18 / +0x10 (0x30eb63)
        sto->vtpb_idx = -1; pthread_mutex_init/cond_init …
      case MALLOC:
        buf = malloc(size)                             // 0x30eab3
        tensor_allocate_empty(name, out_tensor)        // 0x30eacc
        tensor_build_user(out_tensor, name, buf, size) // 0x30eafe
      case FAKE:
        tensor_allocate_empty(name, out_tensor)
        tensor_build_user(out_tensor, name, NULL, size)
      default:
        nlog_write("Unknown tensor mem type %d"); __assert_fail   // 0x30ec84
    sto->mem_owned_by_tensor = 1                       // +0x48 (0x30e962) — allocate owns it
    sto->vtpb_idx = resolved_vtpb                      // +0x108 (0x30e976)
fail:
    dmem_free / tensor_free on the partially-built object; nlog_write
```

### Algorithm — `tensor_set_slice` (share storage, no copy)

```c
// tensor_set_slice @0x30e530 — make `slice` a sub-view of `source`'s storage.
function tensor_set_slice(slice, source, offset, size):
    assert(slice != NULL)                                       // (0x30e53a)
    assert(source != NULL)                                      // line 0xDB=219 (0x30e601)
    assert(source->sto != NULL)                                 // line 0xDC=220 (0x30e5e2)
    assert(source->_size >= offset + size)                      // line 0xDD=221 (0x30e5c3)
    tensor_free_storage(slice)                                  // drop slice's prior backing (0x30e569)
    lock add qword ptr [source->sto + 0x40], 1                  // ++storage->ref_count (0x30e572)
    slice->sto     = source->sto                                // +0x08 — SHARE the backing (0x30e580)
    slice->_offset = source->_offset + offset                   // +0x10 — compose offsets (0x30e57c)
    slice->_size   = size                                       // +0x18 (0x30e58a)
    slice->output_completion_count = 0                          // +0x80 (0x30e58e)
    return NRT_SUCCESS
```

> **QUIRK —** the slice's `_offset` is `source->_offset + offset`, *not* `offset`. Slices compose: a slice of a slice addresses the original storage with the summed offset, so the storage never needs to know how deep the view chain is. The bounds check is against `source->_size` (the parent view length), so a slice can never reach past its parent even if the underlying storage is larger.

### Algorithm — `tensor_free` and `tensor_free_storage` (the two destructors)

```c
// tensor_free @0x30e630 — drops the VIEW refcount; frees view+storage on last view.
function tensor_free(tensor):
    if tensor == NULL: return                                   // (0x30e633)
    vtpb = tensor->sto ? tensor->sto->vtpb_idx : 0               // +0x108 for trace (0x30e650)
    nrt_sys_trace_new_event(ev=28 /*0x1C free*/, name, _size, ref_count, vtpb)  // 0x30e6a7
    lock sub qword ptr [tensor + 0x40], 1                        // --view ref_count (0x30e6af)
    if result != 0: { trace(ev=28, edi=1); return }             // other views still hold it
    tensor_free_storage(tensor)                                 // 0x30e70b — may free storage
    free(tensor->name)                                          // 0x30e713
    free(tensor)                                                // 0x30e718
    trace(ev=28, edi=1)
```

```c
// tensor_free_storage @0x30e1d0 — drops the STORAGE refcount; frees backing on last slice.
function tensor_free_storage(tensor):
    if tensor == NULL || tensor->sto == NULL: goto detach
    assert(tensor->sto->type != INVALID)                        // line 0x29=41 (0x30e28c)
    lock sub qword ptr [sto + 0x40], 1                          // --storage ref_count (0x30e1f1)
    if result != 0: goto detach                                 // other slices still share it (0x30e1f7)
    switch sto->type:                                           // +0x10 (0x30e203)
      case MALLOC: if sto->vmem:  free(sto->vmem)               // +0x18, assert !=NULL line 0x2D=45
      case DMA:    if sto->dmem:  dmem_free(sto->dmem)          // +0x18, assert !=NULL line 0x30=48
      case FAKE:   /* no backing to free */
      default:     __assert_fail (line 0x33=51)
    if sto->mem_owned_by_tensor:                                // +0x48 (0x30e1fd)
        pthread_mutex_destroy(&sto->tensor_op_cv_lock)          // +0x50 (0x30e21d)
        pthread_cond_destroy(&sto->tensor_op_cv)                // +0x78 (0x30e22a)
        free(sto)                                               // 0x30e233
detach:
    tensor->sto = NULL                                          // +0x08 (0x30e238)
    tensor->output_completion_count = 0                         // +0x80 (0x30e244)
    tensor->_offset = 0; tensor->_size = 0                      // +0x10/+0x18 (movaps, 16B)
```

> **GOTCHA —** the OS resources (mutex, cond) and the storage block itself are destroyed **only inside the `mem_owned_by_tensor` branch**. For a user-attached buffer (`mem_owned_by_tensor == 0`), the MALLOC/DMA backing free is *skipped at the type switch* (the buffer belongs to the caller) and the storage struct is leaked-by-design unless a higher layer reclaims it. A reimplementer who frees the storage unconditionally will double-free user buffers; one who never frees it will leak runtime-owned allocations. The ownership bit is the hinge.

### Function Map — lifecycle

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `tensor_allocate_empty` | `0x30e310` | `calloc(0xC0)` view, strdup name, `ref_count=1` | HIGH |
| `tensor_build_user.part.0` | `0x30e730` | `calloc(0x140)` MALLOC/FAKE storage, init mutex+cond | HIGH |
| `tensor_allocate` | `0x30e8a0` | central allocator; DMA/MALLOC/FAKE dispatch; sets ownership+vtpb | HIGH |
| `tensor_set_to_user_buffer` | `0x30e3d0` | re-back with MALLOC storage over caller buffer (`mem_owned=0`) | HIGH |
| `tensor_set_slice` | `0x30e530` | share storage (`++sto->ref_count`), compose `_offset` | HIGH |
| `tensor_free` | `0x30e630` | atomic `--tensor->ref_count`; free storage+name+view on 0 | HIGH |
| `tensor_free_storage` | `0x30e1d0` | atomic `--sto->ref_count`; type-dispatched backing free on 0 | HIGH |

---

## 3. The Refcount Model

### Purpose

Two orthogonal sharing relationships demand two refcounts. A single backing buffer may be observed by many *views* (a tensor handed to several subsystems) **and** by many *slices* (sub-views into the same storage). The layer keeps these on separate words so that freeing a view never accidentally frees storage another slice still uses, and vice versa.

### The two counters

| Counter | Word | Incremented by | Decremented by | Frees when 0 |
|---|---|---|---|---|
| **view** `ref_count` | `tensor+0x40` | `tensor_get_reference` (`lock add …,1`) | `tensor_free` (`lock sub …,1`) | view struct + name (+ a `tensor_free_storage` pass) |
| **storage** `ref_count` | `sto+0x40` | `tensor_set_slice` (`lock add …,1`) | `tensor_free_storage` (`lock sub …,1`) | backing buffer, mutex, cond, storage struct (if owned) |

### Algorithm — `tensor_get_reference`

```c
// tensor_get_reference @0x30e620 — 10 bytes; the entire body.
function tensor_get_reference(tensor):
    lock add qword ptr [tensor + 0x40], 1            // atomic ++view ref_count (0x30e623)
    return tensor                                    // returns the same handle
```

A bare `lock add` on the 64-bit word at `+0x40`, then return the pointer — no null check, no allocation. Its sole observed caller is `kbl_create_reference_feature_map_set` (the loader building a reference feature-map set that aliases existing tensors). There is **no exported `nrt_tensor_get_reference`** in the public ABI; view-refcount bumping is an internal loader operation.

### The decrement is on the critical free path

The decrement lives at the top of `tensor_free` (`lock sub qword ptr [tensor+0x40], 1; setz`). The `setz` captures whether the count reached zero in one instruction; only the zero case proceeds to `tensor_free_storage` + `free(name)` + `free(tensor)`. The storage decrement nests inside that, in `tensor_free_storage` (`lock sub [sto+0x40], 1`). So a `free` of the last view triggers *two* atomic decrements in sequence — view then storage — and the backing is reclaimed only if the storage decrement also hits zero.

> **QUIRK —** both counters use plain `lock add`/`lock sub` with no acquire/release fencing beyond the `lock` prefix's implicit full barrier — adequate on x86-64 TSO, but a reimplementation on a weakly-ordered ISA (AArch64) must add the release-on-decrement / acquire-on-final-read pattern that x86's `lock` provides for free, or it will race the destructor against a concurrent `get_reference`.

### The byte-untyped invariant

> **QUIRK —** the tensor object stores **only bytes and a size**. `tensor_get_size` (`@0xbe9d0`, `inc/tdrv/tensor.h`) returns `tensor->_size` and nothing else; there is no field for element type, element count, or rank in either struct. Slicing, copying, memset and checksum all operate on raw byte ranges. The element `dtype` (`nrt_dtype_t`: `FLOAT32=10`, `BFLOAT16=6`, `FP8_E4=14`, …) and the `shape`/`ndim` of a model tensor are produced by a *separate* metadata path — `nrt_get_model_tensor_info` reads them out of the loaded NEFF via `kmgr_get_io_tensor_info` (see [neff/dtype-system](../neff/dtype-system.md) and [api-tensors](api-tensors.md)). A reimplementer who expects to find `dtype` on the handle will look forever: the object layer is intentionally type-agnostic so that one buffer can be reinterpreted by metadata without re-allocation.

---

## 4. Host/Device I/O and Addressing

### Purpose

Every byte that moves between host and device passes through one of five operations: `read`, `write`, `read_batch`, `write_batch`, `memset` (plus device↔device `copy`). All six are **type-dispatched** on `sto->type` and all six **fence** against in-flight device execution before touching the bytes. Two addressing accessors (`get_va`, `get_pa`) expose the buffer to callers who DMA it directly.

### Entry Point

```text
nrt_tensor_read (public)
  └─ tensor_read (0x30ed40)
       ├─ tensor_block_while_exec.isra.0 (0x30df80)   ── FENCE vs device writes
       └─ { DMA: dmem_buf_copyout (0x2299b0)
            MALLOC: memcpy
            FAKE: no-op → NRT_SUCCESS }

nrt_tensor_write (public)
  └─ tensor_write (0x30efb0)
       ├─ tensor_block_while_exec (FENCE vs ALL device access)
       └─ { DMA: dmem_buf_copyin (0x229820) | MALLOC: memcpy | FAKE: no-op }
```

### Algorithm — `tensor_read` (representative I/O path)

```c
// tensor_read @0x30ed40 — write path (0x30efb0) is the mirror image.
function tensor_read(tensor, offset, dst, size):
    assert(tensor->_size >= offset + size)                       // bounds (view)
    assert(sto->allocated_size >= tensor->_size + tensor->_offset)  // bounds (storage)
    assert(sto->type != INVALID)
    nrt_sys_trace_new_event(ev=9 /*0x09 read*/, …)               // 0x30edcf
    tensor_block_while_exec(sto, /*reads-vs-writes*/)            // 0x30ee17 — fence
    switch sto->type:                                            // +0x10
      case DMA:    dmem_buf_copyout(sto->dmem, tensor->_offset+offset, dst, size)
      case MALLOC: memcpy(dst, (char*)sto->vmem + tensor->_offset + offset, size)
      case FAKE:   /* no-op */                                   // returns NRT_SUCCESS
    nrt_sys_trace_new_event(ev=9, api_level=1, …)
    return status
```

The trace event IDs are byte-anchored from the `mov esi, IMM` immediates before each `nrt_sys_trace_new_event` call: **read=9 (0x09), write=10 (0x0A), alloc=27 (0x1B), free=28 (0x1C)** (read_batch=11, write_batch=12 per the decompile). A reimplementer reproducing the trace stream must emit a start/end pair per op with these IDs.

> **NOTE —** `read` fences reads-against-writes (it only needs to wait for in-flight device *writes* to the storage to drain), whereas `write`, `memset` and `write_batch` fence against **all** in-flight access (they must not race a concurrent device read or write). The asymmetry is the producer side of the §5 counter pair: the I/O ops are the *waiters*; `tensor_async_update` is the *producer*. The waiter `tensor_block_while_exec` (`0x30df80`) lives in an adjacent band and is documented with the [submit path](../exec/submit-path.md).

### Algorithm — addressing (`get_va`, `get_pa`)

```c
// tensor_get_va @0x30f9c0 — host virtual address of the view's first byte.
function tensor_get_va(tensor):
    switch sto->type:                                           // +0x10 (0x30f9f7)
      case MALLOC: return (char*)sto->vmem + tensor->_offset    // +0x18 + _offset (0x30fa10)
      case DMA:    return DMEM_GET_VA(sto->dmem) + tensor->_offset  // dmem->_va + _offset (0x30fa34)
      case FAKE:   return NULL

// tensor_get_pa @0x30fae0 — device physical address; DMA only.
function tensor_get_pa(tensor):
    assert(sto->type == DMA)                                    // line tag "…== NRT_TENSOR_MEM_TYPE_DMA"
    assert(sto->allocated_size >= tensor->_size + tensor->_offset)
    return tensor->_offset + sto->dmem->_pa + sto->dmem->align_offset   // _offset + dmem[0x18] + dmem[0x28]
```

`tensor_get_pa` is the device-address counterpart: it sums the view `_offset`, the `dmem` physical base `_pa` (+0x18), and the allocator's `align_offset` (+0x28). `get_va` returns NULL for FAKE (no bytes exist); `get_pa` asserts DMA (host buffers have no device PA).

### Function Map — I/O and addressing

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `tensor_read` | `0x30ed40` | bounds+fence, then `dmem_buf_copyout` / `memcpy` / no-op | HIGH |
| `tensor_write` | `0x30efb0` | mirror of read; `dmem_buf_copyin` / `memcpy` / no-op | HIGH |
| `tensor_read_batch` | `0x30f220` | validate batch, optional fence, `dmem_buf_batch_copyout` | HIGH |
| `tensor_write_batch` | `0x30f3a0` | fence-all each, `dmem_buf_batch_copyin` | HIGH |
| `tensor_memset` | `0x30f520` | bounds+fence-all, `dmem_memset` / `memset` / no-op | HIGH |
| `tensor_copy` | `0x30f6c0` | type-dispatched; DMA↔DMA `dmem_copy` (same `hbm_idx`) else read/write | HIGH |
| `tensor_get_va` | `0x30f9c0` | host VA = buffer/`_va` + `_offset`; FAKE → NULL | HIGH |
| `tensor_get_pa` | `0x30fae0` | device PA = `_offset + dmem->_pa + dmem->align_offset` (DMA only) | HIGH |
| `tensor_get_tensor_placement` | `0x30fbb0` | out `hbm_idx`, `mem_loc = dmem->mem_type` (DMA\|FAKE) | HIGH |
| `tensor_get_device_allocation_info` | `0x30fe60` | DMA+TONGA_DRAM → `{physical_address, allocated_size, hbm_index=dmem->tdram_channel}` | HIGH |
| `tensor_checksum` | `0x30fc40` | `malloc`+`tensor_read` whole tensor, `adler32_z` (zlib); asserts `!FAKE` | HIGH |

> **GOTCHA —** `tensor_copy` (`0x30f6c0`) requires both tensors on the **same `hbm_idx`** for the fast DMA↔DMA `dmem_copy` path (asserts `src->sto->type == DMA && dst->sto->type == DMA` and emits `"Tensors must be allocated on same HBM (SRC: %u) (DST: %u)"` otherwise). A cross-channel copy is *not* serviced here — it falls back to a host round-trip (`MALLOC`-src → `tensor_write`, `MALLOC`-dst → `tensor_read`). The cross-HBM / cross-LNC enforcement that emits `"Tensor %s allocated on HBM %u was passed to a model loaded on lnc %u."` lives in `nrta_tensor_copy`, not this layer.

---

## 5. Async-Exec Bookkeeping and the Completion Plane

### Purpose

A tensor's bytes may be in flight on the device while the host wants to read or write them. Two mechanisms guard this: a **per-storage** in-flight counter pair (fences host I/O against device execution), and a **process-global** completion counter plane (lets the host poll for "the device has finished writing this output N times"). The first is internal to the I/O path; the second is exposed to the public API.

### The per-storage fence counters

```c
// tensor_async_update @0x30fda0 — the PRODUCER half of the I/O fence.
// in      (esi): true ⇒ a device READ;   false ⇒ a device WRITE.
// release (edx): false ⇒ op STARTING (++); true ⇒ op FINISHING (--).
function tensor_async_update(tensor, in, release):
    assert(tensor != NULL)                                       // line 0x30A=778 (0x30fe1f)
    assert(tensor->sto != NULL)                                  // line 0x30B=779 (0x30fe3e)
    pthread_mutex_lock(&sto->tensor_op_cv_lock)                  // +0x50 (0x30fdc2)
    counter = in ? &sto->pending_exec_count_read                 // +0xC0  (0x30fdcb)
                 : &sto->pending_exec_count_write                // +0x100 (0x30fdd2)
    if release:
        lock sub qword ptr [counter], 1                          // op finished (0x30fde4)
        if *counter == 0:
            pthread_cond_broadcast(&sto->tensor_op_cv)           // +0x78 — wake waiters (0x30fe18)
    else:
        lock add qword ptr [counter], 1                          // op started (0x30fe00)
    pthread_mutex_unlock(&sto->tensor_op_cv_lock)
    return NRT_SUCCESS
```

The matching **waiter** is `tensor_block_while_exec` (`0x30df80`, adjacent band): it blocks on `tensor_op_cv` while the relevant `pending_exec_count_*` is nonzero. `tensor_read` waits only on `pending_exec_count_write` (read-vs-write); `tensor_write`/`memset` wait on both. The producer (`tensor_async_update`) is driven from the loader/exec marking path — `kbl_tensor_set_mark_async_exec_internal` and `kbl_create_reference_feature_map_set` (confirmed call edges). This is the classic readers-writers fence, with the counters living on the storage so all views/slices of one buffer share the fence.

> **QUIRK —** the broadcast fires **only on the `0` transition** (`jz` after the `lock sub`). A reimplementation that broadcasts on every decrement is functionally correct but wakes waiters spuriously; one that broadcasts only when the *other* counter is also zero will deadlock a writer waiting behind a drained reader. The predicate is "this counter reached zero", evaluated under the lock.

### The process-global completion plane

```text
exec_request_progress_one_step          ── exec engine bumps tensor->output_completion_count
  └─ tensor_get_output_completion_lock (0x310410) → &output_completion_lock (0xca7280)
  └─ tensor_get_output_completion_cond (0x310400) → &output_completion_cond (0xca72c0)

nrt_tensor_check_output_completion (0xc0cc0)   ── poll until count >= expected (opt. timeout)
nrt_tensor_reset_output_completion (0xc12f0)   ── lock; tensor->output_completion_count = 0; unlock
```

```c
// Both getters are 8-byte LEA-and-return stubs.
function tensor_get_output_completion_cond(): return &output_completion_cond  // 0x310400 → 0xca72c0
function tensor_get_output_completion_lock(): return &output_completion_lock  // 0x310410 → 0xca7280
```

The two getters are one-instruction `lea` accessors that hand out a **single process-global** mutex/cond pair (resolved from the RIP-relative `lea` displacements: `0x310407 + 0x996eb9 = 0xca72c0` for cond, `0x310417 + 0x996e69 = 0xca7280` for lock). Per-tensor completion state lives in `tensor->output_completion_count` (`+0x80`); the device-execution path increments it as each output write completes, and the public `nrt_tensor_check_output_completion` waits under the global lock until the count reaches the caller's expected value (with a 30-second heartbeat log and an optional µs timeout). `nrt_tensor_reset_output_completion` zeroes it under the same lock. The producer of the increment lives in the [completion engine](../exec/completion-engine.md), not here.

> **GOTCHA —** the completion plane uses **one** global lock/cond for **all** tensors, not a per-tensor pair like the fence counters. A reimplementer must not assume the completion lock is `sto->tensor_op_cv_lock` — it is a distinct `.bss` global (`0xca7280`). Mixing the two will either over-serialize all output polling or fail to wake the right waiter.

### Function Map — async / completion

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `tensor_async_update` | `0x30fda0` | producer: `++/--` per-storage read/write fence counter; broadcast on drain | HIGH |
| `tensor_get_output_completion_cond` | `0x310400` | return `&output_completion_cond` (global `0xca72c0`) | HIGH |
| `tensor_get_output_completion_lock` | `0x310410` | return `&output_completion_lock` (global `0xca7280`) | HIGH |

---

## 6. Debug: Input Dump

Two functions implement the `NEURON_RT_DBG_INPUT_DUMP_DIRECTORY` feature — orthogonal to the object model but resident in the same band.

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `tensor_input_dump_init` | `0x30feb0` | parse env var (cap 256, default `/tmp/neuron-input-dump`), `mkdir 0777`, store basedir global (`0xca72f0`) | HIGH |
| `tensor_dump_inputs` | `0x30ff80` | walk input-tensor hashtable (`ht_get_next`), `tensor_read` each, write `<name>.bin` + `model_name.txt` into `<basedir>/input_dump_<rand>_h_nn_<nn>/` | HIGH |

`tensor_dump_inputs` is driven from the exec worker (`kmgr_exec_worker_do_work`, `nrt_execute_repeat`) and reuses `tensor_read` to materialize each input. The hashtable value-aliasing (`node[-1].next` holds the `nrt_tensor_t*`) is the same pattern documented for tensor-sets in [api-tensors](api-tensors.md) (MED confidence on the exact `ht_node_t` value-slot semantics).

---

## Function Map — Complete Band

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `tensor_free_storage` | `0x30e1d0` | drop storage refcount; type-dispatched backing free; destroy mutex/cond | HIGH |
| `tensor_allocate_empty` | `0x30e310` | `calloc(0xC0)` view; strdup name; `ref_count=1` | HIGH |
| `tensor_set_to_user_buffer` | `0x30e3d0` | re-back with MALLOC storage over caller buffer (`mem_owned=0`) | HIGH |
| `tensor_set_slice` | `0x30e530` | sub-view: `++sto->ref_count`, compose `_offset`, share `sto` | HIGH |
| `tensor_get_reference` | `0x30e620` | atomic `++tensor->ref_count`; return handle | HIGH |
| `tensor_free` | `0x30e630` | trace; atomic `--tensor->ref_count`; free on 0 | HIGH |
| `tensor_build_user.part.0` | `0x30e730` | `calloc(0x140)` MALLOC/FAKE storage; init mutex+cond | HIGH |
| `tensor_allocate` | `0x30e8a0` | central type-dispatched allocator; sets ownership+vtpb | HIGH |
| `tensor_read` | `0x30ed40` | bounds+fence; `dmem_buf_copyout` / `memcpy` / no-op | HIGH |
| `tensor_write` | `0x30efb0` | bounds+fence-all; `dmem_buf_copyin` / `memcpy` / no-op | HIGH |
| `tensor_read_batch` | `0x30f220` | batch validate + `dmem_buf_batch_copyout` (trace ev 11) | HIGH |
| `tensor_write_batch` | `0x30f3a0` | batch fence-all + `dmem_buf_batch_copyin` (trace ev 12) | HIGH |
| `tensor_memset` | `0x30f520` | bounds+fence-all; `dmem_memset` / `memset` / no-op | HIGH |
| `tensor_copy` | `0x30f6c0` | DMA↔DMA `dmem_copy` (same HBM) else read/write fallback | HIGH |
| `tensor_get_va` | `0x30f9c0` | host VA = buffer/`_va` + `_offset` | HIGH |
| `tensor_get_pa` | `0x30fae0` | device PA = `_offset + dmem->_pa + dmem->align_offset` | HIGH |
| `tensor_get_tensor_placement` | `0x30fbb0` | out `hbm_idx`, `mem_loc = dmem->mem_type` | HIGH |
| `tensor_checksum` | `0x30fc40` | `tensor_read` whole + `adler32_z`; asserts `!FAKE` | HIGH |
| `tensor_async_update` | `0x30fda0` | fence-counter producer; broadcast on drain | HIGH |
| `tensor_get_device_allocation_info` | `0x30fe60` | DMA+TONGA_DRAM → `{pa, size, hbm_index}` | HIGH |
| `tensor_input_dump_init` | `0x30feb0` | env-var debug-dump basedir setup | HIGH |
| `tensor_dump_inputs` | `0x30ff80` | dump each input tensor to disk | HIGH |
| `tensor_get_output_completion_cond` | `0x310400` | global completion cond accessor | HIGH |
| `tensor_get_output_completion_lock` | `0x310410` | global completion lock accessor | HIGH |

### Inferred / not-fully-traced

- **`nrt_sys_trace_event_data_t` union layout** — the per-op trace payload passed to `nrt_sys_trace_new_event` is opaque; only the event-type integer IDs (9/10/11/12/27/28) are byte-anchored, their enum *names* are unresolved (LOW).
- **`ht_node_t` value-slot semantics** in `tensor_dump_inputs` — `node[-1].next` is read literally from the decompile; "value adjacent to node" is inferred (MED).
- **`tensor_block_while_exec` predicate** (the fence waiter) lives in an adjacent band; this page documents only the producer (`tensor_async_update`). The exact cv-predicate over `pending_exec_count_read`/`_write` is owned by the [submit path](../exec/submit-path.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `tensor_block_while_exec` (`0x30df80`) | the consumer/waiter half of the §5 fence; this layer is the producer |
| `dmem_alloc` / `dmem_buf_copy*` family | `tdrv/dma_memory.c` — the DMA backing the `DMA` storage kind |
| `kbl_*` feature-map / tensor-set ops | the loader callers that slice, reference and async-mark tensors |
| `exec_request_progress_one_step` | bumps `output_completion_count`; consumes the global completion plane |

## Cross-References

- [Public C API: Tensor Surface and I/O](api-tensors.md) — the `nrt_tensor_*` wrappers that drive this layer; tensor-set hashtable; model-tensor-info (dtype/shape)
- [TDRV: Device-Memory Allocator (dmem)](tdrv-dmem.md) — the `dmem_t` backing the `DMA` storage kind; `_pa`/`align_offset`/`tdram_channel` source
- [The Submit Path: Bind → Stage → Doorbell](../exec/submit-path.md) — where tensors are bound for execution and the fence waiter (`tensor_block_while_exec`) blocks
- [The Completion Engine](../exec/completion-engine.md) — the NQ-harvest path that increments `output_completion_count` under the global lock/cond
- [NEFF dtype system](../neff/dtype-system.md) — where element `dtype` actually lives, since the tensor object is byte-untyped
- [NEFF metadata schema](../neff/metadata-schema.md) — the `shape`/`ndim`/`usage` metadata surfaced by `nrt_get_model_tensor_info`
