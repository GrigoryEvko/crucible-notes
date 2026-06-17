# Userspace Side (libnds.a)

> *Addresses on this page apply to `libnds.a` and to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (ELF64, non-stripped, DWARF-v4). `libnds.a` is the standalone static archive (6 TUs under `KaenaRuntime/nds/`, 810 674 B, compiler "GNU C11 14.2.1 ... -O2 -std=gnu11 -fPIC"); it is byte-for-byte the same code statically linked into `libnrt.so`, where it occupies the `.text 0x507070..0x508160` band as 36 ELF `LOCAL FUNC` symbols. Archive offsets are quoted as `member.o+off`; the linked copy as a `libnrt.so` VMA (`.text` VMA == file offset). The two are the same instructions — every one of the 16 functions L-INFRA-09/10 sized from the linked copy matches the `.o` to the byte (§Verification).*
> *Evidence grade: **Confirmed (byte-anchored)** — function inventory, sizes, and per-TU dependencies are `nm`/`objdump` verbatim; all struct and enum names are DWARF-v4 `DW_TAG_member`, not inferred. Other versions will differ. · Part XIV — The Neuron DataStore (NDS) · [back to index](../index.md)*

## Abstract

`libnds.a` is the **userspace client library** for the Neuron DataStore — the C accessor layer that sits directly on top of the shared-memory blob whose byte layout is owned by **[the NDS wire format](wire-format.md)**. Where the wire-format page answers *"what is at byte `0xda80`"*, this page answers *"which function reads it, under what guard, and how does it survive a torn read."* The mental model is a thin handle-and-accessor library — like a `/proc` parser fused with a lock-free shared-ring reader — split cleanly into six translation units: lifecycle (`neuron_ds.c`), counters (`neuron_ds_counters.c`), hash + bitmap helpers (`neuron_ds_helpers.c`), the model-node slot machinery (`neuron_ds_models.c`), the generic object lifecycle and dispatch (`neuron_ds_object.c`), and the process-info handle accessors (`neuron_ds_process.c`).

Three API families cover the whole surface. **Lifecycle** is `nds_open`/`nds_close`/`nds_get_ptr`/`nds_is_read_only`: `nds_open` calls into the NDL mapping primitive (`ndl_nds_open`, the boundary owner of the `mmap`), allocates a 72-byte host handle, and forks on `pid` — `pid == 0` is the producer/RW owner (it stamps the header), `pid != 0` is a reader/RO attaching to another process's slab (it validates the header). **Counters** are eight NC-scoped and five ND-scoped accessors that resolve `(pnc, counter)` to a `u64*` and do `LOCK ADD`/`SUB`/`OR` (writers) or a plain 8-byte load (readers) — no hash, relying on natural 64-bit atomicity. **Objects** are the `nds_obj_*` handle lifecycle (`new`/`commit`/`delete`/`read`/`type-check`) plus the per-type leaf accessors, dispatched by 1-byte type tag through two static jump tables — these carry the FNV-1a-32 hash protocol.

The single thing a reimplementer must get exactly right is the **producer/reader asymmetry threaded through every accessor**: the same library is the writer in the inference process and the reader in `neuron-monitor`, and the only thing distinguishing them is `inst->pid`. Every mutating call (`nds_obj_commit`, every counter `increment`/`decrement`/`set`/`or`) consults `nds_is_read_only` (or the inlined `pid != 0` test) and refuses if the handle is a reader; every reader call recomputes the FNV hash and retries exactly once. This page maps each API function onto that asymmetry and gives the read-path, lifecycle, and commit/read algorithms as annotated pseudocode. It does **not** re-derive the region map, the struct offsets, or the NC-addressing math — those live in **[the wire format](wire-format.md)**; this page calls them by their region name and links.

For reimplementation, the API contract is:

- **The 72-byte `nds_instance_t` handle** (`calloc(1, 0x48)`) and its single discriminator `pid` (`+0`): `0 ⇒` producer/RW, `≠ 0 ⇒` reader/RO. `nds_get_ptr` (`+0x18`) and `nds_is_read_only` (`pid != 0`) are the two leaves every other accessor funnels through.
- **The lifecycle fork in `nds_open`** — NDL map → size gate (`> 0x1067f`) → `calloc` handle → producer `mutex_init + commit_header` vs reader `check_header` (free + `-EINVAL` on mismatch).
- **The counter resolver + `LOCK`/plain-load split**, with the read-only gate on every mutator and the `NULL`-ptr (`-1`) / out-of-range (`-EINVAL`) / read-only (`-EPERM`) error returns.
- **The `nds_obj_*` dispatch**: 1-byte type tag (0 = model, 1 = process-info, 2 = process-info-ext) routing `commit`/`delete` through two `.data.rel.ro` jump tables, and the FNV-hash stamp (producer) / verify-and-retry (reader) protocol the object reads ride on.
- **The 64-slot model bitmap allocator** (`nds_get_empty_map_slot` first-clear-bit, `nds_mark_map_slot` set/clear) under the *process-local* `pthread_mutex` at `nds_instance_t+0x20`.

| | |
|---|---|
| **Archive** | `libnds.a` — 6 TUs, 36 global `T` functions, **0** `.rodata` string literals (NDS emits no diagnostics) |
| **Linked band** | `libnrt.so .text 0x507070..0x508160`, all 36 symbols `LOCAL FUNC` |
| **Handle** | `nds_instance_t` = `calloc(1, 0x48)` = 72 B; `pid@+0`, `device@+8`, `nds_size@+0x10`, `nds_ptr@+0x18`, `pthread_mutex_t lock@+0x20` |
| **Lifecycle** | `nds_open` @`0x5070d0` (216 B), `nds_close` @`0x5071b0` (75 B) |
| **Discriminators** | `nds_get_ptr` @`0x507200` (5 B, leaf), `nds_is_read_only` @`0x507210` (8 B, leaf) |
| **Hash** | `nds_hash` @`0x5080c0` (57 B) — FNV-1a-32, basis `0x811c9dc5`, prime `0x01000193` (owned by [wire format](wire-format.md) §7) |
| **Header proto** | `"nds\0"` + `100000` = `u64 0x000186a06e6473`, `.data header_proto` @`0xc0c820` ([wire format](wire-format.md) §2) |
| **Mapping boundary** | `ndl_nds_open`/`ndl_nds_close` @`0xc4110`/`0xc41b0` ([NDL](../runtime/ndl.md) owns the `mmap`) |
| **Object dispatch** | `obj_commit_funcs` @`0xbf9210`, `obj_delete_funcs` @`0xbf91f0` (`.data.rel.ro`, 3 × 8 B) |

---

## 1. Library Shape and the Six Translation Units

### Purpose

`libnds.a` is partitioned into six TUs by concern, and the partition is worth internalising because the call graph respects it: the lifecycle TU owns the handle, the helpers TU is a pure leaf (hash + bitmap, no NDS-specific calls), and everything else funnels through `nds_get_ptr`/`nds_is_read_only` to reach the shared blob. There are **no static/local-only functions** — all 36 symbols are global `T` in the archive; the `LOCAL FUNC` binding in `libnrt.so` is the static linker hiding them, not a source-level distinction. There are also **zero `.rodata` string literals** in the entire archive: NDS never logs. Every diagnostic a reimplementer might expect lives in the callers (`dlr_model::create_nds_model_info`, `tdrv_nds_save_process_info`), not in NDS itself.

### TU map

| TU (`KaenaRuntime/nds/`) | `.o` size | Role | Public surface |
|---|---|---|---|
| `neuron_ds.c` | 143 904 B | Lifecycle / header | `nds_open`, `nds_close`, `nds_get_ptr`, `nds_is_read_only`, `nds_commit_header`, `nds_check_header` |
| `neuron_ds_counters.c` | 150 304 B | NC + ND atomic counters | 4 NC (`inc/dec/get/set`) + 5 ND (`inc/dec/or/get/set`) accessors |
| `neuron_ds_helpers.c` | 104 688 B | Hash + bitmap (pure leaf) | `nds_hash`, `nds_get_empty_map_slot`, `nds_mark_map_slot` |
| `neuron_ds_models.c` | 158 304 B | Model-node slot map | `nds_commit/delete/read_model_node_info*`, `nds_read_all_model_nodes`, slot helpers, handle→info accessors |
| `neuron_ds_object.c` | 159 696 B | Generic object lifecycle + dispatch | `nds_obj_new/commit/delete`, `nds_commit/read_generic_obj`, `nds_obj_handle_check_type` |
| `neuron_ds_process.c` | 92 248 B | Process-info handle accessors | `nds_obj_handle_to_process_info[_ext]`, `nds_read_process_info[_ext]` |

### The funnel

Every accessor that touches shared memory reaches it through exactly two leaves, which is the whole reason `pid`/`nds_ptr` need no locking to read — they live in the *host-local* handle, not the shared blob:

```text
nds_get_ptr      (0x507200, 5B, leaf)  ── return inst->nds_ptr (handle+0x18) = shared blob base
nds_is_read_only (0x507210, 8B, leaf)  ── return inst->pid (handle+0x00) != 0

counters ─┐
objects  ─┼─► nds_get_ptr ──► shared blob   (read/write the region)
models   ─┘     ▲
mutators ───► nds_is_read_only ──► reject if reader  (× before any store)
```

> **NOTE —** `nds_get_ptr` returns the shared-blob base verbatim with no validation — it is a 5-byte `mov 0x18(%rdi),%rax ; ret`. A `NULL` here means "handle never opened a slab"; the counter accessors treat that as `-1` (`0xffffffff`), distinct from the `-EPERM` read-only rejection and the `-EINVAL` out-of-range. The three error returns are not interchangeable — a reimplementer must keep them separate so callers can distinguish "no datastore" from "you are a reader" from "bad index." (CONFIDENCE HIGH — three distinct `mov $...,%eax` constants in the counter accessor disasm.)

---

## 2. Lifecycle — Open, Map, Close

### Purpose

`nds_open` is the only entry that allocates anything and the only place the producer/reader fork is decided. It is a thin orchestration over the NDL mapping primitive: NDL (`ndl_nds_open`, [the NDL page](../runtime/ndl.md) owns it) does the ioctl + `mmap` and returns `{base ptr, size}`; `nds_open` validates the size, allocates the host handle, and either stamps the header (producer) or validates it (reader). `nds_close` is the mirror: NDL unmaps, and the handle is torn down — with the mutex destroyed only on the producer side, because only the producer ever initialised it.

### Entry Point

```text
PRODUCER:                                         READER (monitor attaching to TARGET pid):
tdrv_init_nds_for_device                          neuron-monitor / dlr_model attach
  └─ nds_open(device, pid=0)        (0x5070d0)       └─ nds_open(device, pid=TARGET)   (0x5070d0)
       ├─ ndl_nds_open  (0xc4110, ioctl#71+mmap)          ├─ ndl_nds_open(TARGET)  (RO map)
       ├─ size > 0x1067f  else fail                       ├─ size > 0x1067f  else fail
       ├─ calloc(1, 0x48)  else -ENOMEM                   ├─ calloc(1, 0x48)  else -ENOMEM
       ├─ pthread_mutex_init(inst+0x20)                   └─ nds_check_header   (0x507080)
       └─ nds_commit_header (0x507070)                          └─ usleep(500)×1; -EINVAL + free on fail
```

### Algorithm

`nds_open` (`0x5070d0`, archive `neuron_ds.c.o+0x60`, 216 B) takes `(device, pid, out_instance)` and returns `0` / `-ENOMEM` / `-EINVAL`. The size gate and the producer/reader fork are the two decisions that matter:

```c
// nds_open — libnrt.so 0x5070d0 / neuron_ds.c.o+0x60
int nds_open(ndl_device_t *device, pid_t pid, nds_instance_t **out):
    void  *ptr  = NULL;
    size_t size = 0;
    int rc = ndl_nds_open(device, pid, &ptr, &size);   // BOUNDARY: NDL owns ioctl#71 + mmap
    if (rc != 0)             return rc;
    if (size <= 0x1067f)     return -EINVAL;            // require >= NDS_REQUIRED_SIZE (0x10680); cmp 0x1067f,%r15; jbe
    nds_instance_t *inst = calloc(1, 0x48);            // 72-byte host handle (zeroed)
    if (inst == NULL)        return -ENOMEM;           // -12
    inst->pid      = pid;                              // handle+0x00  — THE discriminator
    inst->device   = device;                           // handle+0x08
    inst->nds_size = size;                             // handle+0x10
    inst->nds_ptr  = ptr;                              // handle+0x18  — shared blob base
    if (pid == 0):                                     // PRODUCER / RW owner
        pthread_mutex_init(&inst->lock, NULL);         // handle+0x20 — process-local, model-slot only
        nds_commit_header(inst);                       // stamp "nds\0"+100000 (single 8-byte store)
    else:                                              // READER / RO of TARGET pid
        if (nds_check_header(inst) != 0):              // validate magic, usleep(500)×1 retry
            free(inst);                                //   producer never committed (or wrong slab)
            return -EINVAL;                            //   -22
    *out = inst;
    return 0;
```

`nds_commit_header` and `nds_check_header` are the header stamp/validate pair; their bodies — the single 8-byte `header_proto` store and the compare-with-`usleep(500)`-retry — are documented byte-by-byte in **[the wire format](wire-format.md) §2** and not repeated here. `nds_close` (`0x5071b0`, `neuron_ds.c.o+0x140`, 75 B) is the mirror; the only asymmetry is the producer-only mutex teardown:

```c
// nds_close — libnrt.so 0x5071b0 / neuron_ds.c.o+0x140
int nds_close(nds_instance_t *inst):
    ndl_nds_close(inst->device, inst->pid, inst->nds_size, inst->nds_ptr);  // BOUNDARY: NDL unmap (0xc41b0)
    if (inst->pid == 0):                              // producer only — only it init'd the mutex
        pthread_mutex_destroy(&inst->lock);           // handle+0x20
    free(inst);
    return 0;
```

> **GOTCHA —** the `pthread_mutex_t` at `nds_instance_t+0x20` is **process-local and producer-only**. It is `init`'d / `destroy`'d solely on the `pid == 0` path and serialises only *same-process threads* during model-slot allocation (§5). It is **never** a cross-process lock — readers in another process never touch it, and it does not live in the shared mapping. A reimplementer who places this mutex in shared memory, or who init's it on the reader path, has mismodelled the synchronisation: cross-process consistency rests entirely on the FNV-hash protocol (§4), not on this lock. (CONFIDENCE HIGH — `pthread_mutex_init`/`_destroy` appear only under the `pid == 0` branch in `nds_open`/`nds_close`.)

### Function Map

| Function | Symbol+addr | Size | Role | Confidence |
|---|---|---|---|---|
| `nds_open` | `0x5070d0` / `neuron_ds.c.o+0x60` | 216 B | NDL map → size gate → `calloc` handle → producer stamp / reader check | HIGH |
| `nds_close` | `0x5071b0` / `+0x140` | 75 B | NDL unmap → producer mutex destroy → free handle | HIGH |
| `nds_get_ptr` | `0x507200` / `+0x190` | 5 B | return `inst->nds_ptr` (handle+0x18), leaf | HIGH |
| `nds_is_read_only` | `0x507210` / `+0x1a0` | 8 B | return `inst->pid != 0`, leaf | HIGH |
| `nds_commit_header` | `0x507070` / `+0x000` | 15 B | single 8-byte `header_proto` store (producer) | HIGH |
| `nds_check_header` | `0x507080` / `+0x010` | 75 B | compare magic, `usleep(500)`×1 retry, else `-EINVAL` | HIGH |

### Considerations

The size gate (`> 0x1067f`) is the userspace half of the kernel/userspace size contract: the kernel hands out a fixed `262144`-byte slab, but `nds_open` only requires the `67200`-byte map fits — it does not require the slab be exactly that size, so a future producer can extend the layout into the ~195 KiB tail without breaking the gate. The `-EINVAL` on `size <= 0x1067f` is therefore a *truncation* guard (a mapping too small to hold the defined regions), not an exact-size check. The reader's `nds_check_header` failure is also `-EINVAL` but for a different cause — the producer has not yet stamped the magic — which is why the reader path frees the handle and returns: there is nothing to read.

---

## 3. Counters — Atomic Accessors

### Purpose

The counter TU is the hot-path surface: the runtime bumps NC and ND counters on every inference step, error, and mem-usage update. Each accessor resolves `(pnc_index, counter_index)` (NC) or `idx` (ND) to a `u64*` inside the shared blob and applies one operation — `LOCK ADD`/`SUB`/`OR` for mutators, a plain 8-byte load/store for get/set. There is **no hash** on the counter path; correctness rests on natural 64-bit atomicity for the aligned `u64` and the `LOCK` prefix for read-modify-write. The address resolution — the 3-band NC dispatch (`32 + pnc*31 + ctr`, `6992 + pnc*64 + (ctr-31)`, `7248 + (pnc-4)*96 + ctr`) and the ND `(idx+1)*8` form — is owned by **[the wire format](wire-format.md) §4**; this page documents the *accessor wrapper* around it: the guards, the atomic op, and the error returns.

### Entry Point

```text
PRODUCER hot path                                  READER (monitor)
exec_request_progress_one_step ─┐                  neuron-monitor poll
tdrv_update_ds_mem_usage      ──┼─► nds_increment_nc_counter (0x507220)   nds_get_nc_counter (0x507380)
kmetric_update_nds_error_stats ─┘     └─ nds_get_ptr                       └─ nds_get_ptr
tdrv_set_feature_bitmap ──────────► nds_or_nd_counter (0x507560)               (plain u64 load, no lock)
                                      └─ LOCK OR (1<<bit)
```

### Algorithm

All nine counter accessors share a skeleton: get the base pointer, reject readers (for mutators), resolve the address, apply the op. The NC read path — `nds_get_nc_counter` — is the cleanest illustration because it shows the resolver *without* the read-only gate (reads are allowed for any opener):

```c
// nds_get_nc_counter — libnrt.so 0x507380 / neuron_ds_counters.c.o+0x160  (172 B)
//   reader-safe: no read-only gate; plain u64 load (no LOCK, no hash)
int nds_get_nc_counter(nds_instance_t *inst, int pnc_index, int counter_index, uint64_t *value):
    uint8_t *base = nds_get_ptr(inst);              // handle+0x18
    if (base == NULL)            return -1;          // 0xffffffff — no datastore mapped
    // ── address resolution: identical 3-band dispatch to wire-format §4 ──
    long idx;
    if (pnc_index <= 3):                             // original cores 0-3
        if (counter_index <= 30):
            idx = 32   + pnc_index*31 + counter_index;          // NC band   @0x100  (32*8=256)
        else:
            idx = 6992 + pnc_index*64 + (counter_index - 31);   // EXT band  @0xda80
    else if (pnc_index <= 15):                       // extended cores 4-15
        if (counter_index > 94)  return -EINVAL;     // 0xffffffea
        idx = 7248 + (pnc_index-4)*96 + counter_index;          // EXT-data  @0xe280
    else:
        return -EINVAL;                              // 0xffffffea — pnc out of range
    *value = *(uint64_t*)(base + idx*8);             // plain 8-byte load
    return 0;
```

The mutator twin `nds_increment_nc_counter` (`0x507220`) is identical *except* it inserts the read-only gate before the store and uses `LOCK ADD`:

```c
// nds_increment_nc_counter — libnrt.so 0x507220 / neuron_ds_counters.c.o+0x000  (172 B)
int nds_increment_nc_counter(nds_instance_t *inst, int pnc, int ctr, uint64_t *delta):
    uint8_t *base = nds_get_ptr(inst);
    if (base == NULL)            return -1;
    if (inst->pid != 0)          return -EPERM;      // 0xffffffff path? — see GOTCHA; reader cannot mutate
    uint64_t *slot = nc_counter_addr(base, pnc, ctr);// same 3-band resolver as above
    if (slot == NULL)            return -EINVAL;      // bad index
    __sync_fetch_and_add(slot, *delta);              // x86 LOCK ADD — atomic RMW
    return 0;
```

The ND accessors are the simple case — a single index `idx <= 30` at element `idx+1` (the `+1` skipping the 8-byte header), no banding:

```c
// nds_or_nd_counter — libnrt.so 0x507560 / neuron_ds_counters.c.o+0x340  (78 B)
//   the ONE counter written with OR (FEATURE_BITMAP, ND idx 3); arg is a BIT index
int nds_or_nd_counter(nds_instance_t *inst, int idx, int bit_index):
    uint8_t *base = nds_get_ptr(inst);
    if (base == NULL)            return -1;
    if (inst->pid != 0)          return -EPERM;      // mutator → reader-gated
    if (idx > 30)                return -EINVAL;      // 0xffffffea  (cmp 0x1e; ja)
    uint64_t *slot = (uint64_t*)base + (idx + 1);    // ptr[idx+1]
    __sync_fetch_and_or(slot, 1ULL << bit_index);    // x86 LOCK OR
    return 0;
```

### Function Map

| Function | Symbol+addr | Size | Op | Scope | Confidence |
|---|---|---|---|---|---|
| `nds_increment_nc_counter` | `0x507220` / `counters.o+0x000` | 172 B | `LOCK ADD` | NC, mutator | HIGH |
| `nds_decrement_nc_counter` | `0x5072d0` / `+0x0b0` | 172 B | `LOCK SUB` (add of `-v`) | NC, mutator | HIGH |
| `nds_get_nc_counter` | `0x507380` / `+0x160` | 172 B | plain load | NC, reader-safe | HIGH |
| `nds_set_nc_counter` | `0x507430` / `+0x210` | 172 B | plain store | NC, mutator | HIGH |
| `nds_increment_nd_counter` | `0x5074e0` / `+0x2c0` | 62 B | `LOCK ADD` | ND, mutator | HIGH |
| `nds_decrement_nd_counter` | `0x507520` / `+0x300` | 62 B | `LOCK SUB` | ND, mutator | HIGH |
| `nds_or_nd_counter` | `0x507560` / `+0x340` | 78 B | `LOCK OR (1<<bit)` | ND, mutator (FEATURE_BITMAP) | HIGH |
| `nds_get_nd_counter` | `0x5075b0` / `+0x390` | 62 B | plain load | ND, reader-safe | HIGH |
| `nds_set_nd_counter` | `0x5075f0` / `+0x3d0` | 62 B | plain store | ND, mutator | HIGH |

> **GOTCHA —** the **read-only gate sits on the mutators, not the readers**. `nds_get_nc_counter`/`nds_get_nd_counter` have *no* `pid != 0` check — any opener (producer or monitor) may read any counter. `nds_increment/decrement/set/or_*` all reject a reader handle. The error code on the rejection path is read from the same `mov $0xffffffff` slot as the `NULL`-base path in the inc/dec accessors, so a caller cannot always distinguish "no datastore" from "you are a reader" by return value alone — it must check `nds_is_read_only` itself if it needs to tell them apart. Out-of-range index is the distinct `-EINVAL (0xffffffea)`. (CONFIDENCE HIGH on the gate placement; MED-HIGH on the exact `-1` vs `-EPERM` distinction since both surface as `0xffffffff` in the inc path disasm — the wire-format §4 GOTCHA labels the read-only return `-EPERM`, consistent with that being the same `0xffffffff` constant.)

> **CORRECTION (NDS-API R1) —** an early pass labelled `nds_set_nc_counter` "reader-safe" by analogy to `nds_get_nc_counter` (both are non-`LOCK`). It is **not**: `set` writes the slot and is read-only gated like the other mutators; only `get` is ungated. The naming (`get`/`set`) is the discriminator, not the absence of a `LOCK` prefix. (CONFIDENCE HIGH — `nds_set_nc_counter` @`0x507430` carries the `pid != 0` branch; `nds_get_nc_counter` @`0x507380` does not.)

### Considerations

The counter *semantics* — which NC index is `INFER_COMPLETED`, `MAC_COUNT`, `LATENCY_TOTAL`, which ND index is `FEATURE_BITMAP` — are the DWARF enums `NDS_NC_COUNTER` / `NDS_EXT_NC_COUNTER` / `NDS_ND_COUNTER`, enumerated index-by-index in **[the wire format](wire-format.md) §3**. `nds_or_nd_counter` is the only counter written with `OR` rather than `ADD`, and it exists solely for the `FEATURE_BITMAP` (ND idx 3) — a reimplementation that `ADD`s into that slot corrupts the bitmap. The mutators take their value through a `uint64_t*` (not by value), an ABI quirk consistent across all four NC mutators and used identically by the runtime call sites.

---

## 4. Generic Objects — Commit (Producer) and Read (Reader)

### Purpose

The object layer is where the FNV-hash torn-read protocol lives. It wraps the three self-describing records — process-info (type 1, 64 B @`0x4e0`), process-info-ext (type 2, 260 B @`0xad28`), and (via the model TU) the 64 model entries (type 0, 672 B) — behind a uniform `nds_obj_*` lifecycle dispatched by a 1-byte type tag. The producer builds a record in a private staging buffer, stamps the FNV hash, and `memcpy`s it into shared memory; the reader copies it out, re-verifies the hash, and retries exactly once on mismatch. The hash *scheme* (basis, prime, zeroed-field rule) is owned by **[the wire format](wire-format.md) §7**; this page documents the *accessor flow* — the dispatch, the staging, the read-only gate on commit, and the one-retry loop.

### Entry Point

```text
PRODUCER (writer):                                 READER (any opener):
tdrv_nds_save_process_info ─┐                      nds_read_process_info     (0x5080a0, tail→read_generic 1)
dlr_model::create_nds_model_info ─┼─► nds_obj_commit  nds_read_process_info_ext (0x5080b0, tail→read_generic 2)
                              │     (0x507cd0)             └─ nds_read_generic_obj (0x507e00)
                              │      ├─ nds_is_read_only  × reject reader            ├─ nds_get_ptr
                              │      ├─ nds_get_ptr                                  ├─ malloc(len) + memcpy snapshot
                              │      └─ obj_commit_funcs[type]                       ├─ zero hash; nds_hash; compare
                              │           type0 → nds_commit_model_node_info         ├─ mismatch → usleep(200) ×1 retry
                              │           type1/2 → nds_commit_generic_obj           └─ ok → nds_obj_new + SSE-copy payload
                              └─► nds_obj_delete (0x507d40)
                                    obj_delete_funcs[type]  (type0 only; 1/2 = NULL no-op)
```

### Dispatch

`nds_obj_commit` (`0x507cd0`, `neuron_ds_object.c.o+0x200`, 105 B) and `nds_obj_delete` (`0x507d40`, `+0x270`, 75 B) are pure routers: validate, gate, then tail-jump through a `.data.rel.ro` jump table indexed by the handle's type byte. The two tables (reloc-decoded from `obj_commit_funcs` @`0xbf9210`, `obj_delete_funcs` @`0xbf91f0`):

| `type` byte | Object | `obj_commit_funcs[type]` | `obj_delete_funcs[type]` |
|---|---|---|---|
| 0 | model-node-info | `nds_commit_model_node_info` @`0x5076c0` | `nds_delete_model_node_info` @`0x507820` |
| 1 | process-info | `nds_commit_generic_obj` @`0x507ad0` | `NULL` (no-op) |
| 2 | process-info-ext | `nds_commit_generic_obj` @`0x507ad0` | `NULL` (no-op) |

```c
// nds_obj_commit — libnrt.so 0x507cd0 / neuron_ds_object.c.o+0x200  (105 B)
int nds_obj_commit(nds_obj_handle_t *obj):
    if (obj == NULL)                     return -EINVAL;     // 0xffffffea
    if (nds_is_read_only(obj->inst))     return -EINVAL;     // reader cannot publish
    if (obj->type > 2)                   return -EINVAL;     // tag out of range
    uint8_t *base = nds_get_ptr(obj->inst);
    if (base == NULL)                    return -1;          // 0xffffffff
    return obj_commit_funcs[obj->type](base, obj);           // tail-jmp through .data.rel.ro
// nds_obj_delete (0x507d40): same shape; obj_delete_funcs[type] is NULL for 1/2 (no-op), then free(obj)
```

> **QUIRK —** types 1 and 2 share the **same committer** (`nds_commit_generic_obj` @`0x507ad0`) — the table has the same target twice. The committer distinguishes them internally by `type`: type 1 stages a 64-byte record at blob `+0x4e0`, type 2 a 260-byte record at `+0xad28`. Their delete slots are both `NULL` — process-info is *overwritten*, never deleted, so there is no deleter. Only the model object (type 0) has a real deleter (it must free a bitmap slot). A reimplementer who writes three distinct committers, or who supplies a process-info deleter, has over-built: the binary shares one committer and no-ops the deletes. (CONFIDENCE HIGH — reloc-decoded jump-table targets, L-INFRA-10 §3.)

### Algorithm — commit (producer)

`nds_commit_generic_obj` (`0x507ad0`, `neuron_ds_object.c.o+0x000`, 496 B) is the producer half. It builds the record in a heap staging buffer, stamps the hash, and publishes it:

```c
// nds_commit_generic_obj — libnrt.so 0x507ad0 / neuron_ds_object.c.o+0x000  (496 B)
//   reached only from nds_obj_commit (already read-only-gated, base resolved)
int nds_commit_generic_obj(uint8_t *base, nds_obj_handle_t *obj):
    size_t  rec_len;  uint8_t *dest;
    switch (obj->type):
        case 1:  rec_len = 64;   dest = base + 0x4e0;   break;   // process-info        ([wire-format] §6)
        case 2:  rec_len = 260;  dest = base + 0xad28;  break;   // process-info-ext
        default: return -EINVAL;                                  // 0xffffffea
    uint8_t *staging = calloc(1, rec_len);                        // private buffer (64 or 260 B)
    if (staging == NULL)         return -ENOMEM;                  // 0xfffffff4
    // copy the payload from the host wrapper (obj+24..) into staging+4 via SSE moves
    sse_copy(staging + 4, (uint8_t*)obj + 24, rec_len - 4);      // 60 B (type1) / 256 B (type2)
    *(uint32_t*)staging = 0;                                      // zero the hash word FIRST
    *(uint32_t*)staging = nds_hash(staging, rec_len);            // FNV-1a-32 over the whole record
    memcpy(dest, staging, rec_len);                              // PUBLISH: hash word stored first, then payload
    free(staging);
    return 0;
```

### Algorithm — read (reader)

`nds_read_generic_obj` (`0x507e00`, `neuron_ds_object.c.o+0x330`, 570 B) is the reader half and the canonical one-retry loop. `nds_read_process_info`/`_ext` (`0x5080a0`/`0x5080b0`) are 10-byte tail-calls into it with `type = 1`/`2`:

```c
// nds_read_generic_obj — libnrt.so 0x507e00 / neuron_ds_object.c.o+0x330  (570 B)
//   reader-safe: no read-only gate (any opener may read)
nds_obj_handle_t *nds_read_generic_obj(nds_instance_t *inst, int type):
    uint8_t *base = nds_get_ptr(inst);
    if (base == NULL)            return NULL;
    size_t  rec_len;  uint8_t *src;
    switch (type):
        case 1:  rec_len = 64;   src = base + 0x4e0;   break;
        case 2:  rec_len = 260;  src = base + 0xad28;  break;
        default: return NULL;
    uint8_t *local = malloc(rec_len);                            // scratch snapshot buffer
    if (local == NULL)           return NULL;
    for (int attempt = 0; attempt < 2; attempt++):               // at most TWO reads
        memcpy(local, src, rec_len);                             // snapshot the shared record
        uint32_t stored = *(uint32_t*)local;
        *(uint32_t*)local = 0;                                   // zero hash field before recompute
        if (nds_hash(local, rec_len) == stored):                 // consistent snapshot
            nds_obj_handle_t *h = nds_obj_new(inst, type);       // calloc wrapper (88/280 B by type)
            h->src = src;                                         // wrapper+16 ← published-record ptr
            sse_copy((uint8_t*)h + 24, local + 4, rec_len - 4);  // copy payload into wrapper+24..
            free(local);
            return h;
        usleep(200);                                             // 0xc8 — torn (or unpublished) read; retry once
    free(local);
    return NULL;                                                 // 2nd failure → caller sees no snapshot
```

> **GOTCHA —** the retry budget is **one** (`attempt < 2` ⇒ two reads total), with a fixed `usleep(200)` (`0xc8`) back-off — distinct from the `usleep(500)` (`0x1f4`) of the *header* check (§2). On the second failure the generic-object reader returns `NULL` (it `malloc`'d scratch and frees it), whereas the model reader (§5) falls through to the last copy. A reimplementation that loops unboundedly can spin against a producer stuck mid-`memcpy`; the bounded retry deliberately *tolerates* a missed read rather than blocking. The producer's correctness hinges on storing the hash word **first** in the published record and the payload after, so on x86-TSO a reader that fails the hash on attempt 0 and succeeds on attempt 1 has caught the producer between two whole-record publishes. (CONFIDENCE HIGH on the loop bound and sleep window; the memory-ordering soundness is the [overview](overview.md) §2 race-audit candidate, MED.)

### Function Map

| Function | Symbol+addr | Size | Role | Confidence |
|---|---|---|---|---|
| `nds_obj_new` | `0x507d90` / `object.o+0x2c0` | 105 B | `calloc` host wrapper by type (0→712, 1→88, 2→280 B); set `type@0`, `inst@8`, `NULL@16` | HIGH |
| `nds_obj_commit` | `0x507cd0` / `+0x200` | 105 B | validate + read-only gate + `obj_commit_funcs[type]` dispatch | HIGH |
| `nds_obj_delete` | `0x507d40` / `+0x270` | 75 B | validate + `obj_delete_funcs[type]` (type0 only) + `free` | HIGH |
| `nds_obj_handle_check_type` | `0x507cc0` / `+0x1f0` | 14 B | `(obj->type == type) ? 0 : 22`, leaf | HIGH |
| `nds_commit_generic_obj` | `0x507ad0` / `+0x000` | 496 B | stage + FNV stamp + publish (type1 @`0x4e0`, type2 @`0xad28`) | HIGH |
| `nds_read_generic_obj` | `0x507e00` / `+0x330` | 570 B | snapshot + FNV verify + 1-retry + `nds_obj_new` populate | HIGH |
| `nds_read_process_info` | `0x5080a0` / `process.o+0x060` | 10 B | tail-call `nds_read_generic_obj(inst, 1)` | HIGH |
| `nds_read_process_info_ext` | `0x5080b0` / `+0x070` | 10 B | tail-call `nds_read_generic_obj(inst, 2)` | HIGH |
| `nds_obj_handle_to_process_info` | `0x508040` / `process.o+0x000` | 43 B | `check_type(obj,1)` → `(char*)obj+24` payload | HIGH |
| `nds_obj_handle_to_process_info_ext` | `0x508070` / `+0x030` | 43 B | `check_type(obj,2)` → `(char*)obj+24` payload | HIGH |

> **NOTE —** the host wrappers `nds_obj_new` returns (88 B type1, 280 B type2, 712 B type0) are **not** in shared memory — they are heap staging/result copies with a 24-byte header (`type@0`, `inst@8`, `src@16`) followed by the payload. The shared-memory records are the 64/260/672-byte *internal* records. The `_to_process_info[_ext]` accessors do nothing but a type-check and a `+24` offset into the wrapper — they are the typed view onto the payload a reader extracts. Confusing the wrapper sizes (88/280/712) with the wire-record sizes (64/260/672) is the easy slip; the wrapper carries the extra 24-byte handle header and different alignment. (CONFIDENCE HIGH — `calloc` sizes verbatim in `nds_obj_new`.)

---

## 5. Model Nodes — Slot Allocation, Commit, and Read

### Purpose

The model TU is the most elaborate object path because it manages an *array* of 64 records behind a `u64` occupancy bitmap rather than a single fixed slot. Committing a model node allocates a free slot under the process-local mutex, marks the bitmap, and publishes a 672-byte record at `0x528 + slot*672`; deleting zeroes the record and clears the bit; reading-all popcounts the bitmap and snapshots each occupied slot. The record layout (`nds_model_node_data_internal`, the 284-byte node-info, the 2×12 mem-usage grid) is owned by **[the wire format](wire-format.md) §5**; this page documents the *slot machinery* and the commit/read flow.

### Entry Point

```text
PRODUCER:                                          READER:
dlr_model::create_nds_model_info                   neuron-monitor model enumeration
  └─ nds_obj_commit (type 0)                         └─ nds_read_all_model_nodes (0x5079e0)
       └─ nds_commit_model_node_info (0x5076c0)           ├─ nds_get_ptr
            ├─ pthread_mutex_lock (inst+0x20)              ├─ map = *(u64*)(base+0x520)
            ├─ nds_get_empty_model_slot ──► nds_get_empty_map_slot (0x508100)   ── first clear bit
            ├─ nds_mark_model_slot      ──► nds_mark_map_slot      (0x508130)   ── set bit
            ├─ nds_hash(rec, 0x2a0)  + publish (rep movsq)         ├─ __popcountdi2(map) → calloc handle array
            └─ pthread_mutex_unlock                                └─ per set bit: nds_read_model_node_info_data
```

### The bitmap allocator

The occupancy bitmap is a single `u64` at blob `+0x520`. Two helpers in the pure-leaf helpers TU operate on it — `nds_get_empty_map_slot` (first clear bit) and `nds_mark_map_slot` (set/clear). They take the bitmap *value* / *pointer*; the model wrappers `nds_get_empty_model_slot`/`nds_mark_model_slot` (12-/19-byte tail-calls) bind them to the `+0x520` address:

```c
// nds_get_empty_map_slot — libnrt.so 0x508100 / neuron_ds_helpers.c.o+0x040  (47 B, leaf)
int nds_get_empty_map_slot(uint64_t map):
    if (~map == 0)              return -1;          // all 64 bits set → caller maps to 28 (ENOSPC)
    int slot = 0;
    while (map & 1):                                // shr/test loop: skip set bits
        map >>= 1;  slot++;
        if (slot > 63)          return -1;          // cmp 0x3f; jg — cap at 64 slots
    return slot;                                    // index of first CLEAR bit
// nds_mark_map_slot — 0x508130 / +0x070 (39B): *map = used ? (*map | 1<<slot) : (*map & ~(1<<slot))
```

### Algorithm — commit and read

`nds_commit_model_node_info` (`0x5076c0`, `neuron_ds_models.c.o+0x090`, 343 B) is the producer; it differs from the generic committer in three ways — it takes the *process-local mutex*, allocates a *slot*, and builds a *672-byte* record:

```c
// nds_commit_model_node_info — libnrt.so 0x5076c0 / neuron_ds_models.c.o+0x090  (343 B)
int nds_commit_model_node_info(uint8_t *base, nds_obj_handle_t *obj):
    if (obj->slot_unset):                                   // first commit only
        pthread_mutex_lock(&obj->inst->lock);               // PROCESS-LOCAL, model-slot only
        uint64_t map = *(uint64_t*)(base + 0x520);
        int slot = nds_get_empty_map_slot(map);             // first clear bit
        if (slot < 0):
            pthread_mutex_unlock(&obj->inst->lock);
            return 28;                                       // ENOSPC — all 64 slots full
        nds_mark_map_slot((uint64_t*)(base + 0x520), slot, /*used=*/1);
        obj->slot       = slot;                             // wrapper+24
        obj->data_source = base + 0x528 + slot*672;         // wrapper+16 ← published-record ptr
        pthread_mutex_unlock(&obj->inst->lock);
    // build the 672-byte record (node_info @+4, mem_usage @+288) from the wrapper, stamp, publish
    uint8_t staging[0x2a0];
    build_model_record(staging, obj);                       // node_info + 2×12 mem-usage grid
    *(uint32_t*)staging = 0;
    *(uint32_t*)staging = nds_hash(staging, 0x2a0);         // FNV-1a-32 over 672 B
    rep_movsq(obj->data_source, staging, 0x2a0);            // publish (hash word first)
    return 0;
```

`nds_read_all_model_nodes` (`0x5079e0`, `+0x3b0`, 234 B) is the reader's enumeration entry: it reads the bitmap, popcounts it to size a handle array, then reads each occupied slot through `nds_read_model_node_info_data` (`0x507890`, `+0x260`, 330 B) — which is the model-shaped twin of `nds_read_generic_obj`, with `len = 672` and a `usleep(200)`×1 retry:

```c
// nds_read_all_model_nodes — libnrt.so 0x5079e0 / neuron_ds_models.c.o+0x3b0  (234 B)
nds_obj_handle_t **nds_read_all_model_nodes(nds_instance_t *inst, int *out_count):
    uint8_t *base = nds_get_ptr(inst);
    uint64_t map  = *(uint64_t*)(base + 0x520);             // 64-slot occupancy bitmap
    int n = __popcountdi2(map);                             // count occupied slots
    nds_obj_handle_t **arr = calloc(n, sizeof(void*));
    int k = 0;
    for (int slot = 0; slot < 64; slot++):                  // tzcnt-style iterate set bits
        if (map & (1ULL << slot)):
            arr[k++] = nds_read_model_node_info_data(inst, slot);   // 672-B snapshot + FNV verify + 1 retry
    *out_count = n;
    return arr;
```

### Function Map

| Function | Symbol+addr | Size | Role | Confidence |
|---|---|---|---|---|
| `nds_commit_model_node_info` | `0x5076c0` / `models.o+0x090` | 343 B | mutex + slot alloc + 672-B FNV stamp + publish; `28` if full | HIGH |
| `nds_delete_model_node_info` | `0x507820` / `+0x1f0` | 105 B | zero 672-B record + clear bitmap bit under mutex | HIGH |
| `nds_read_model_node_info_data` | `0x507890` / `+0x260` | 330 B | 672-B snapshot + FNV verify (`usleep(200)`×1) + `nds_obj_new` populate | HIGH |
| `nds_read_all_model_nodes` | `0x5079e0` / `+0x3b0` | 234 B | bitmap popcount → `calloc` array → per-bit read | HIGH |
| `nds_get_empty_model_slot` | `0x507690` / `+0x060` | 12 B | tail-call `nds_get_empty_map_slot(*(base+0x520))` | HIGH |
| `nds_mark_model_slot` | `0x5076a0` / `+0x070` | 19 B | tail-call `nds_mark_map_slot(base+0x520, slot, used)` | HIGH |
| `nds_get_empty_map_slot` | `0x508100` / `helpers.o+0x040` | 47 B | first-clear-bit of `u64`, cap 63, `-1` if full, leaf | HIGH |
| `nds_mark_map_slot` | `0x508130` / `+0x070` | 39 B | set/clear bit `slot` in `*map`, leaf | HIGH |
| `nds_obj_handle_to_model_node_info` | `0x507630` / `models.o+0x000` | 43 B | `check_type(obj,0)` → `(char*)obj+40` | HIGH |
| `nds_obj_handle_to_model_node_mem_usage` | `0x507660` / `+0x030` | 43 B | `check_type(obj,0)` → `(char*)obj+328` | HIGH |

> **GOTCHA —** the model slot bitmap is allocated under the producer's `pthread_mutex`, but the **per-record publish is not** — only the *slot allocation* (read bitmap, find clear bit, mark) is serialised. A second producer thread cannot grab the same slot, but the 672-byte `rep movsq` that fills the record races a concurrent reader, and that race is closed by the FNV hash + one-retry (§4), not by the mutex. The mutex is purely an *intra-process allocation* lock; it does nothing for cross-process reads. A reimplementer who assumes the mutex makes the publish atomic for readers has mismodelled it — readers in another process never see the mutex. (CONFIDENCE HIGH — the `mutex_lock`/`unlock` pair brackets only the `get_empty_slot`/`mark_slot` calls, not the `rep movsq`.)

> **NOTE —** the handle→info accessors return offsets `+40` (node-info) and `+328` (mem-usage) into the *712-byte model wrapper* (type 0), not into the 672-byte wire record. The wrapper's larger header (`type@0`, `inst@8`, `data_source@16`, `slot@24`, `map_source@32`, then `node_info@40`) is why the offsets differ from the wire record's `node_info@4`/`mem_usage@288`. Both layouts are DWARF-verbatim; the wrapper is the [wire format](wire-format.md) §5's `nds_model_node_data_wrapper_t`, the record its `nds_model_node_data_internal`.

---

## 6. The Producer/Reader Decision Tree

Pulling the whole library together: the API surface is a single decision tree keyed on `inst->pid` and, for objects, the type tag. A reimplementer can validate an implementation against this dispatch:

```text
nds_open(device, pid)
  │
  ├─ pid == 0 ──► PRODUCER (RW owner)
  │                ├─ commit_header (stamp "nds\0"+100000)
  │                ├─ counters: inc/dec/set/or  ── LOCK RMW into shared blob
  │                ├─ nds_obj_commit            ── type0 model (mutex+slot) / type1,2 generic
  │                │     └─ FNV stamp + publish (hash word first)
  │                └─ nds_obj_delete            ── type0 zero+clear-bit / type1,2 no-op
  │
  └─ pid != 0 ──► READER (RO of TARGET)
                   ├─ check_header (usleep 500 ×1; -EINVAL if unstamped)
                   ├─ counters: get only        ── plain u64 load (set/inc/dec/or → -EPERM)
                   ├─ nds_read_process_info[_ext]── FNV verify + usleep 200 ×1 retry
                   └─ nds_read_all_model_nodes  ── popcount bitmap + per-slot FNV verify
```

The asymmetry is total: every mutating verb checks `nds_is_read_only` (or the inlined `pid != 0`) and refuses for a reader; every reading verb runs the hash-verify/one-retry protocol against a producer that may be mid-publish. There is exactly one host-local lock (the producer's model-slot mutex) and exactly one cross-process integrity mechanism (the FNV-1a-32 record hash). Counters use neither — they ride x86 `LOCK` atomics and natural 64-bit alignment. Get those three facts right and the rest of the library follows mechanically.

---

## Verification

> The function inventory, sizes, and per-TU dependency edges are `nm`/`objdump`/`readelf` verbatim from `libnds.a`; all struct/enum *names* are DWARF-v4 `DW_TAG_member`, not inferred.
> - **Archive == linked copy** — every one of the 16 functions whose size L-INFRA-09/10 measured from the `libnrt.so 0x507xxx` band matches the `libnds.a` `.o` size to the byte: `nds_open` `0xd8`=216, `nds_commit_model_node_info` `0x157`=343, `nds_read_generic_obj` `0x23a`=570, `nds_commit_generic_obj` `0x1f0`=496, `nds_obj_new`/`nds_obj_commit`/`nds_delete_model_node_info` `0x69`=105, `nds_read_all_model_nodes` `0xea`=234, etc. No source or code drift — `libnds.a` is the standalone archive form of the same TUs. (CONFIDENCE HIGH, 16/16 byte-exact.)
> - **Handle geometry** — `nds_open` disasm: `calloc(1, 0x48)` (72 B), `pthread_mutex_init`/`_destroy` only under the `pid == 0` branch, mutex at `inst+0x20`; size gate `cmp $0x1067f,%r15 ; jbe fail`. (HIGH.)
> - **Dispatch tables** — `obj_commit_funcs` @`0xbf9210` / `obj_delete_funcs` @`0xbf91f0` reloc-decoded from `R_X86_64_RELATIVE`: commit `{0x5076c0, 0x507ad0, 0x507ad0}`, delete `{0x507820, NULL, NULL}`. (HIGH.)
> - **Error returns** — distinct `mov $imm,%eax` constants in the counter/object accessors: `-1 (0xffffffff)` no-ptr, `-EINVAL (0xffffffea)` bad index / null obj / bad type, `-ENOMEM (0xfffffff4)` staging alloc fail, `28` ENOSPC slot-full. (HIGH; MED-HIGH only on the `-1` vs `-EPERM` read-only distinction — see §3 GOTCHA.)
>
> **[MEDIUM]** The NC counter band names (A/B/C) and the C-source meaning of the `0x1b50`/`6992`-element overflow sub-band are this analysis's labels, not DWARF; the addressing arithmetic itself is unambiguous and owned by [the wire format](wire-format.md) §4. The exact `-1` vs `-EPERM` return on the read-only mutator path surfaces as the same `0xffffffff` constant in the inc/dec disasm — the wire-format §4 GOTCHA labels it `-EPERM`, consistent with the shared constant.

---

## Cross-References

**NDS sub-pages (Part XIV)**
- [Overview: the Shared-Memory Counter Plane](overview.md) — the kernel/userspace split and the torn-read safety model this page's accessors implement
- [The NDS Wire Format](wire-format.md) — the byte-exact region map, every wire struct, the NC-counter addressing math, and the FNV-hash scheme this page calls by name but does not re-derive
- [Kernel Side (Per-Process Slabs)](kernel-side.md) — the 16-slab array, LRU acquire/release, and the metrics-aggregation read path behind the `mmap` this library opens

**Userspace runtime (Part IV)**
- [NDL: Neuron Driver Layer (IOCTL/mmap Wrappers)](../runtime/ndl.md) — `ndl_nds_open`/`ndl_nds_close` @`0xc4110`/`0xc41b0`, the mapping primitive `nds_open`/`nds_close` sit on top of and the boundary this page does not cross

**Trace & telemetry consumers (Part XIII)**
- [The System Monitor and Debug Stream](../trace/system-monitor.md) — `neuron-monitor`-class readers that open `pid != 0` (RO) and drive `nds_get_*_counter` / `nds_read_all_model_nodes`
- [back to index](../index.md)
