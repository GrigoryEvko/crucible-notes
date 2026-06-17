# The Device Book (db / dbtc)

> *All addresses, offsets, struct layouts, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`, `.rodata`, **and `.data` are VMA == file offset** (`.bss` is `NOBITS`). Provenance strings `/opt/workspace/KaenaRuntime/tdrv/{db.c, dbtc.c, vtpb.c}` root every function on this page. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every symbol is pinned to a `.text` address from `functions.json`; every struct offset is verbatim from DWARF `structures.json`, cross-checked against the IDA `loc_4CDxx` / `node[-N]` decompiler magics; every diagnostic string is present in `strings.json` and tied to its owning function; call edges are from `callgraph.json`. The one open defect (`db_get_mla_from_host_device_id` missing its NULL guard) is flagged LOW. · Part IV — TDRV Runtime, DEEP · [back to index](../index.md)*

## Abstract

The **device book** is the runtime's in-process registry of "which silicon is mine." It lives in three small first-party translation units under `tdrv/` — `db.c` (15 functions), `dbtc.c` (7 functions), and the closely-coupled `vtpb.c` (15 functions) — and answers the three questions every other layer asks before it can touch hardware: *given an opaque core handle, which device and NeuronCore is it?* (`db.c`); *for this model on this core, how many DMA descriptor blocks of each direction does its refill ring need?* (`dbtc.c`); and *which physical cores make up this virtual core?* (`vtpb.c`). It is the indirection [overview §4](overview.md) lists as cross-cutting plumbing: `dmem_alloc_internal`, every `exec_*`/`kbl_*`/`notification_*` path, the collectives encoder, and the profiler all funnel through `db_physical_core_get_mla_and_tpb` (`@0x2272a0`) — its ~50 distinct callers make it the single most-called function the device book owns.

The whole registry is one process-global object, **`tdrv_ctx_0`** (`.bss`), an `~18.0 MB` `tdrv_ctx_t` (size `18,884,656`) dominated by an inline `mla_t mla[32]` array at stride `590,008` B — one `mla_t` per Neuron device (a *Multi-Logical-Accelerator*), each carrying eight inline `tpb_t` cores. [tdrv-lifecycle](tdrv-lifecycle.md) owns the *bring-up* that allocates and populates this object inside `tdrv_init`; **this page owns the lookup and bookkeeping logic that reads it afterward.** Three id spaces meet inside `tdrv_ctx_0` and the device book is the only thing that translates between them: the **host-device-id** (the platform's stable device number), the **device-id** / **MLA index** (this process's dense `[0,32)` slot), the **TPB index** (the NeuronCore-within-device `[0,8)`), and the fabric **routing-id (RID)** the collectives layer addresses cores by. The book holds the host-did↔RID map, scans `mla[]` by host-device-id, and resolves any `physical_core_t {device_id, device_tpb_idx}` to its `(mla_t*, tpb_t*)` by flat pointer arithmetic.

Layered on the registry are two refcount/cache subsystems. The **per-TPB model database** (`db.c`) is a `tpb_t.model_db` hash table (`ht_t*` @`+20032`) under a per-TPB mutex (`+19992`); `add_model`/`remove_model`/`get_model_ref_count`/`model_ref_decrement` plumb an atomic `model_t.ref_count` (`+6488`), and `remove_model` *spins* (`_mm_pause`) until that count drains to zero before handing the model back to its caller — the runtime's model-eviction barrier. The **db tag cache** (`dbtc.c`, "db tag cache") is a two-level hierarchy of named hash tables that memoizes, per model, the DMA-descriptor-block counters `{m2s, s2m, num_packets}[16]` keyed on `{function_name[256], block_id}` — the bookkeeping the DMA refill-ring builder (`drs_create_data_refill_rings`, `fill_io_dma_desc_template`, `translate_one_pseudo_instr_v2`) accumulates so it can size descriptor templates without re-walking the instruction stream. This page derives all three to reimplementation accuracy: the id-map and its structs (§1), the `db_*` lookup + model refcount (§2), the `dbtc_*` tag cache (§3), and the `vtpb_*` virtual-core translation (§4).

For reimplementation, the contract is:

- **The registry object and its strides.** `tdrv_ctx_0` is `tdrv_ctx_t` (`18,884,656` B): `num_mla@+0`, `mla[32]@+8` (stride `590,008`), `ptpbs[256]@+18,880,272`, the `host_did_to_rid_map[32]@+18,884,368`. Every `db_*` accessor reaches a field by `base + 590008*device_id + field_off`; the IDA `loc_4CD18`/`loc_4CD20` magics decode to the `mla.device_id`/`mla.routing_id` offsets and confirm the stride.
- **The three id spaces and their translations.** host-device-id (platform-stable) → scan `mla[]`; device-id == MLA index (dense `[0,32)`) → direct `&mla[device_id]`; TPB index (`[0,8)`) → `&mla->_tpbs[idx]` gated by `tpb_map.map[idx]`; RID → the `host_did_to_rid_map[]` built at init from the driver or a per-chip-family fallback permutation.
- **The per-TPB model refcount protocol.** `tpb_t.model_db` (`ht_t*@+20032`) under `model_db_lock@+19992`; `get_model_ref_count` is *acquire-style* (it increments and returns the model, not a count); `remove_model` ht-removes then **spins on a CAS of `ref_count@+6488` until 0** before returning the model base.
- **The dbtc two-level cache.** `model_t.dbtc_storage@+6296` → `dbtc_node_lut` (`ht_t*`, name→`dbtc_node_t`) → per-name `dbtc_hash` (`ht_t*`, key→`dbtc_desc_count_t`). Key is the 260-byte `{block_id:u32@+0, function_name[256]@+4}`; value is the 240-byte `{m2s_inc[16], s2m_inc[16], num_packets[16]}` accumulator; `dbtc_tail_inc_update` += into an existing node or `malloc(0xF0)` a fresh one.
- **The vtpb collapse/expand arithmetic.** A virtual core (LNC) of `core_size` physical cores: `ptpb = vtpb*core_size + k`, `vtpb = ptpb/core_size`, `vtpb_count = ceil(ptpb_count/core_size)`. `ctx_vtpb_core_size@0xcabb40` is the runtime-wide factor; every stateful wrapper gates on it being non-zero ("lnc context has not been initialized yet").

| | |
|---|---|
| **Registry object** | `tdrv_ctx_0` (`.bss`) — `tdrv_ctx_t`, **18,884,656 B**; `mla[32]@+8` stride **590,008** |
| **Ctor / dtor / getter** | `db_tdrv_ctx_init @0x226ca0` · `db_tdrv_ctx_clear @0x226fa0` · `db_tdrv_ctx_get @0x226fb0` |
| **The hub** | `db_physical_core_get_mla_and_tpb @0x2272a0` — `(mla*,tpb*)` from `physical_core`; **~50 callers** |
| **Model DB** | `tpb_t.model_db` `ht_t*@+20032`, lock `@+19992`; `model_t.ref_count@+6488` (atomic) |
| **Model add/remove** | `add_model @0x2275f0` · `remove_model @0x227750` (spins on ref_count→0) |
| **Tag cache anchor** | `model_t.dbtc_storage@+6296` → `dbtc_node_lut` (`ht_t*`); `dbtc.c` 7 fns `0x227900..0x227cc0` |
| **Tag cache key/value** | key `dbtc_hash_key_t` (260 B `{block_id, function_name[256]}`); value `dbtc_desc_count_t` (240 B `{m2s,s2m,num_packets}[16]`) |
| **vtpb factor** | `ctx_vtpb_core_size @0xcabb40` (u32); `owned_vcores @0xca7320` (32×`virtual_core_t`), `num_owned_vcores @0xca7300` |
| **vtpb translate** | `vtpb_translate_ptpb_to_vtpb @0x314610` · `vtpb_translate_vtpb_to_ptpb @0x3145a0` · `vtpb_physical_core_get_peer @0x314660` |
| **Source TUs** | `tdrv/db.c` · `tdrv/dbtc.c` · `tdrv/vtpb.c` (all first-party KaenaRuntime C, DWARF-rooted) |

---

## 1. The Registry: `tdrv_ctx`, the Id-Map, and the Core Handles

### Purpose

The device book's data spine is a single `.bss` object, `tdrv_ctx_0`, of type `tdrv_ctx_t`. It is the per-process truth of "every device and NeuronCore this process owns, and how the four id spaces map onto each other." `db_tdrv_ctx_init` (`@0x226ca0`) constructs it once during `tdrv_init`; `db_tdrv_ctx_get` (`@0x226fb0`) is the bare getter every other accessor calls first; `db_tdrv_ctx_clear` (`@0x226fa0`) nulls the global on teardown. Because the whole thing is one flat object with inline arrays — `mla[32]` inside `tdrv_ctx_t`, `_tpbs[8]` and `top_sps[16]` inside each `mla_t` — every lookup is pointer arithmetic against fixed strides, never a pointer chase. That is the structural fact a reimplementer must mirror: the strides *are* the API.

### The id spaces

Four distinct identifiers name a core, and the registry is the only thing that translates between them:

```text
host-device-id   platform-stable device number (survives across processes); the key the
                 driver and the collectives RID map use.            → scan mla[] (linear)
device-id        == MLA index; this process's dense slot in [0,32). → &mla[device_id] (direct)
TPB index        NeuronCore within a device, [0,8).                 → &mla->_tpbs[idx] (gated)
routing-id (RID) fabric address the switch/collectives layer uses.  → host_did_to_rid_map[]
```

`device_id` and `MLA index` are the *same* number — the dense slot — which is why `db_get_mla_from_device_id` (`@0x227060`) is a single index `&ctx->mla[device_id]` with a self-consistency check (`entry.device_id != device_id ⇒ NULL`), while `db_get_mla_from_host_device_id` (`@0x2270c0`) must *scan* all 32 slots for the matching `host_device_id`. The RID map is the odd one out: it is not derivable from geometry but is *loaded* — at init from the driver IOCTL `tdrv_get_host_device_id_rid_map`, and on IOCTL-not-implemented from a hardcoded per-chip-family permutation table (see the §2 init callout).

### The structs

The registry record and the two core handles, verbatim from DWARF `structures.json`. Offsets are byte offsets within each struct; absolute offsets within `tdrv_ctx_t` follow the stride arithmetic in the at-a-glance table.

`tdrv_ctx_t` (size `18,884,656` ≈ 18.0 MB) — the process-global device book:

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `num_mla` | `+0` | `u32` | live MLA count | HIGH |
| `mla[32]` | `+8` | `mla_t[32]` | inline device records, **stride 590,008** | HIGH |
| `num_ptpb` | `+18,880,264` | `u32` | live physical-TPB count | HIGH |
| `ptpbs[256]` | `+18,880,272` | `ptpb_info_t[256]` | `{tpb_idx, mla*}` registry for `db_get_ptpb_from_mla_tpb` | HIGH |
| `host_did_to_rid_map[32]` | `+18,884,368` | `u32[32]` | host-device-id → RID translation | HIGH |
| `host_did_to_rid_map_size` | `+18,884,496` | `u32` | valid entries in the RID map | HIGH |
| `reservation_id` | `+18,884,504` | `u64` | UltraServer pod reservation | HIGH |
| `pod_type/sz/node_id/mode` | `+18,884,512…` | mixed | UltraServer pod descriptor | HIGH |
| `target` | `+18,884,528` | `al_hal_tpb_arch_type_t` | cached arch type (2/3/4) | HIGH |
| `dbg_*` tail | `+18,884,532…` | mixed | collectives tuning flags; populated by env-parse cells elsewhere | MEDIUM |

`mla_t` (size `590,008` = `0x900B8`) — one device (Multi-Logical-Accelerator):

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `is_used` | `+0` | `bool` | slot occupied by this process | HIGH |
| `init_state` | `+4` | `u32` | bring-up phase | HIGH |
| `mla_idx` | `+8` | `u32` | this MLA's index (== `device_id`); the NDS slot key | HIGH |
| `bars` | `+16` | `mla_bars_t` (64 B) | BAR descriptors | HIGH |
| `_tpbs[8]` | `+80` | `tpb_t[8]` | inline NeuronCores (8 × 39,320 = 314,560) | HIGH |
| `tpb_map` | `+314,640` | `tpb_map_t` (8 B) | `.map[8]` — nonzero byte ⇒ TPB owned by this process | HIGH |
| `device_id` | `+314,648` | `u32` | dense device id (`loc_4CD18` base) | HIGH |
| `host_device_id` | `+314,652` | `u32` | platform host-device-id (`-1` = `INVALID_HOST_DEVICE_ID`) | HIGH |
| `routing_id` | `+314,656` | `u32` | fabric RID (`loc_4CD20`) | HIGH |
| `device` | `+314,664` | `ndl_device_t*` | NDL device handle (used by close/NDS) | HIGH |
| `top_sps[16]` | `+314,672` | `top_sp_t[16]` | TopSP descriptors | HIGH |
| `bdf[80]` | `+576,688` | `char[80]` | PCI BDF string | HIGH |

`physical_core_t` (size `24`) — the logical core handle passed everywhere:

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `device_id` | `+0` | `u32` | owning device (== MLA index) | HIGH |
| `device_tpb_idx` | `+4` | `u32` | NeuronCore index `[0,8)` | HIGH |
| `vcore` | `+8` | `const virtual_core_t*` | back-pointer to owning LNC | HIGH |
| `fixes_gcc_bug` | `+16` | `u8` | trailing pad | HIGH |

`tpb_t` (size `39,320`) — the members the device book reads (the full layout is owned by [tdrv-lifecycle](tdrv-lifecycle.md)):

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `idx` | `+24` | `int` | core index (used by `db_get_ptpb_from_mla_tpb`) | HIGH |
| `model_db_lock` | `+19,992` | `pthread_mutex_t` (40 B) | guards `model_db` | HIGH |
| `model_db` | `+20,032` | `ht_t*` | per-TPB model hash table (§2) | HIGH |

> **NOTE —** the IDA `loc_4CDxx` magics are the cross-check that pins the stride. The indexing idiom is `ctx->mla[i].FIELD == *(&loc_4CDxx + ctx + i*590008)`: `loc_4CD18 == 0x4CD18 == 314648` is the absolute offset of `mla[0].device_id` within `tdrv_ctx_t`, and `loc_4CD20 == 314656` is `mla[0].routing_id`. A reimplementer reading the decompile sees these as raw `loc_` labels; they are not real symbols, they are *offsets within the inline `mla[0]`* that the `i*590008` index advances. `[HIGH]`

> **QUIRK —** `device_id` is *both* a field inside each `mla_t` (`+314,648`) *and* the array index into `mla[]`. They are kept in sync, and `db_get_mla_from_device_id` (`@0x227060`) exploits that redundancy as a sanity gate: it indexes `&ctx->mla[device_id]` directly, then returns `NULL` if `entry.device_id != device_id`. The redundant store is what lets the direct index detect a stale or never-populated slot without a separate `is_used` read. A reimplementer who stores `device_id` only as the array index loses that self-check.

### Function Map — registry accessors

| Function | Address | Role | Confidence |
|---|---|---|---|
| `db_tdrv_ctx_init` | `0x226ca0` | construct `tdrv_ctx_0`; load host-did→RID map (§2 callout) | CERTAIN |
| `db_tdrv_ctx_clear` | `0x226fa0` | `tdrv_ctx_0 = NULL` (dtor) | CERTAIN |
| `db_tdrv_ctx_get` | `0x226fb0` | return `tdrv_ctx_0` (bare getter) | CERTAIN |
| `db_get_host_device_id_to_rid_map` | `0x226fc0` | out `(&map, &size)`; errors on NULL ctx or size 0 | HIGH |
| `db_get_mla_from_device_id` | `0x227060` | `&ctx->mla[device_id]`; `NULL` if `entry.device_id != device_id` | HIGH |
| `db_get_mla_from_host_device_id` | `0x2270c0` | linear scan `mla[0..31]` for `is_used && host_device_id==arg` | HIGH |
| `db_get_tpb_from_mla` | `0x227120` | `&mla->_tpbs[idx]`; asserts `idx<8`, `tpb_map.map[idx]!=0` | HIGH |
| `db_get_host_device_id_from_mla` | `0x2271e0` | `mla->host_device_id`; asserts `!= -1` | HIGH |
| `db_get_ptpb_from_mla_tpb` | `0x227210` | scan `ptpbs[0..num_ptpb)` for `{mla, tpb->idx}` → ptpb index | HIGH |
| `db_physical_core_get_mla_and_tpb` | `0x2272a0` | **the hub** — `(mla*,tpb*)` from `physical_core` | CERTAIN |
| `db_get_hbm_group_pcores` | `0x227350` | owned pcores sharing a member's HBM group (≤2) | HIGH |

---

## 2. The Hub Lookup and the Per-TPB Model Refcount

### Purpose

This section owns two things `db.c` does once the registry exists: resolving a `physical_core_t` to its `(mla_t*, tpb_t*)` — the operation ~50 callers across `exec`/`notification`/`dmem`/`kbl`/`encd`/`sequencer`/`tensor`/`ucode`/`profile` perform before any device access — and the per-TPB **model database** that refcounts loaded models so the runtime knows when a model is safe to evict. The hub is pure arithmetic; the model DB is a hash table under a mutex with an atomic refcount and a spin-to-drain eviction barrier.

### Entry Point

```text
~50 cross-module callers (exec_*, notification_*, dmem_*, dma_queue_*, kbl_*, encd_*,
 sequencer_dma_*, tensor_allocate, ucode_*_core_create, tsync_*, nrt_profile_*, …)
  └─ db_physical_core_get_mla_and_tpb (0x2272a0)        ── (mla*, tpb*) from physical_core
       └─ db_tdrv_ctx_get (0x226fb0)                    ── fetch tdrv_ctx_0

kbl_model_add    → db_physical_core_get_mla_and_tpb → add_model (0x2275f0) → ht_insert
kbl_model_remove → remove_model (0x227750) → ht_remove + spin on ref_count CAS
{kbl_compute_setup, kbl_infer_exec_wait, kbl_instruction_stream_*, nrt_profile_*}
  → get_model_ref_count (0x2274c0)  [++ref] … → model_ref_decrement (0x2275c0)  [--ref]
```

### Algorithm — the hub

```c
// db_physical_core_get_mla_and_tpb @0x2272a0 — resolve a physical_core to (mla*, tpb*).
// The single most-called function the device book owns (~50 distinct callers).
function db_physical_core_get_mla_and_tpb(pcore, out_mla, out_tpb):     // physical_core_t*
    ctx = db_tdrv_ctx_get()                                    // 0x226fb0
    if !ctx:  nlog("TDRV not initialized!");  return NRT_FAILURE
    // flat pointer arithmetic — NO pointer chase: base + stride*device_id
    m = (mla_t*)((char*)&ctx->mla[0] + 590008 * pcore->device_id)
    // gate: slot must be live AND this TPB must be owned by this process
    assert(m->is_used == true &&
           m->tpb_map.map[pcore->device_tpb_idx] != 0)         // db.c assert :0xEB
    *out_mla = m
    *out_tpb = &m->_tpbs[pcore->device_tpb_idx]                // inline core, stride 39320
    return NRT_SUCCESS
```

The hub never allocates and never locks; it is a bounds-checked address computation. The `tpb_map.map[idx] != 0` gate is the ownership test — a device may be open while only a subset of its eight NeuronCores belong to this process, and the `tpb_map` byte array is the per-TPB ownership bitmap. The sibling `db_get_tpb_from_mla` (`@0x227120`) performs the same gate when the caller already holds the `mla_t*` and only needs the core (`"NC %u from device: %u does not belong to this process"` on failure).

### Algorithm — the model refcount

```c
// The per-TPB model database: tpb_t.model_db (ht_t*@+20032) under model_db_lock (@+19992).
// model_t.ref_count (@+6488) is the atomic; model_t.ht_node (@+7496) is the intrusive node;
// model_t.h_model (@+6384) carries the .id used as the hash key.

// get_model_ref_count @0x2274c0 — ACQUIRE-style despite the name.
function get_model_ref_count(tpb, h_model, out_model):
    lock(tpb->model_db_lock)                                   // @+19992
    node = ht_find(tpb->model_db, h_model.id)                  // tdrv/ht.c
    if !node:  unlock(); nlog("Unknown model %u"); return NRT_FAILURE
    model = ht_node_to_model(node)                             // node@+7496 → base: node[-157].next
    InterlockedAdd64(&model->ref_count, 1)                     // atomic ++ (NOT just a read)
    unlock(tpb->model_db_lock)
    *out_model = model
    return NRT_SUCCESS

// model_ref_decrement @0x2275c0 — pair of the above; no lock needed (atomic only).
function model_ref_decrement(model):
    assert(model != NULL)                                      // db.c assert :0x13A
    InterlockedSub64(&model->ref_count, 1)                     // atomic --

// add_model @0x2275f0 — insert a model into its TPB's database.
function add_model(tpb, model):
    lock(tpb->model_db_lock)
    if ht_find(tpb->model_db, model->h_model.id):              // already present?
        unlock(); nlog("Model: %u already in the DB of nd%d:nc%d"); return NRT_INVALID_HANDLE
    ht_insert(tpb->model_db, &model->ht_node)                  // intrusive node @+7496
    unlock(tpb->model_db_lock)
    return NRT_SUCCESS

// remove_model @0x227750 — ht-remove, then SPIN until no reader holds the model.
function remove_model(tpb, h_model, out_model):
    lock(tpb->model_db_lock)
    node = ht_remove(tpb->model_db, h_model.id)                // unlink from the table first
    if !node:  unlock(); nlog("Unknown model: %u"); return NRT_FAILURE
    model = ht_node_to_model(node)
    unlock(tpb->model_db_lock)
    // eviction barrier: wait for every outstanding get_model_ref_count to be paired
    while InterlockedCompareExchange64(&model->ref_count, 0, 0) != 0:
        _mm_pause()                                            // busy-wait; SMT yield
    *out_model = model                                         // safe to hand back / free
    return NRT_SUCCESS
```

> **QUIRK —** `get_model_ref_count` (`@0x2274c0`) does **not** read a count — it *increments* one and returns the `model_t*`. The name is misleading: it is the **acquire** half of an acquire/release pair, and `model_ref_decrement` (`@0x2275c0`) is the release half. A reimplementer who treats it as a pure query (e.g. caches the returned model without ever decrementing) leaks a reference, and `remove_model`'s spin will then **hang forever** because `ref_count` never drains to zero. Treat the call as `acquire_model()`, not `peek_count()`. (The decompile confirms the `InterlockedAdd64`; the "misnamed" judgement is `[MED]`, the increment behavior is `[HIGH]`.)

> **GOTCHA —** `remove_model` (`@0x227750`) unlinks the model from the hash table *first* (under the lock), then spins on `ref_count → 0` *outside* the lock with `_mm_pause`. The order matters: removing from the table before spinning guarantees no *new* `get_model_ref_count` can find the model and acquire a fresh reference, so the count is monotonically non-increasing while the spin runs — otherwise the loop could livelock against a steady arrival of new readers. The spin runs outside the lock precisely so the `model_ref_decrement` callers it waits on (which release atomically, without the lock) can make progress. A reimplementer who spins *before* unlinking, or who holds the mutex across the spin while a decrement path needs it, gets a hang. The unlink-then-drain order is the correctness invariant.

> **NOTE —** the intrusive-node arithmetic is decoded from the decompiler magics and is `[HIGH]`: `model_t.ht_node` sits at `+7496`, and the hash callback recovers the model base via `v6[-157].next` (`7496 − 157*48 + 40(next) = 0`) and `ref_count` via `v6[-21]` (`7496 − 21*48 = 6488`), where `48` is `sizeof(ht_node_t)`. A reimplementer using a non-intrusive table (separate key→`model*` map) sidesteps the arithmetic entirely but must replicate the lock/refcount discipline.

### Init: building the host-did→RID map

`db_tdrv_ctx_init` (`@0x226ca0`) is where the RID id-space is populated. It asserts the global is not already set (`"tdrv_ctx already set"`), memset-pattern-inits all 32 `mla[].num_mla/device_id` to `-1`, then loads the host-device-id→RID map from the driver via `tdrv_get_host_device_id_rid_map` (`tdrv/init.c:188`). When that IOCTL is **not implemented** the query returns `NRT_INVALID` and the code falls back to a hardcoded per-chip-family permutation table.

```c
// db_tdrv_ctx_init @0x226ca0 — RID-map load with per-family fallback (the fallback is [MED]).
function db_tdrv_ctx_init(...):
    assert(tdrv_ctx_0 == NULL)                                // "tdrv_ctx already set"
    memset_pattern(ctx);  ctx->mla[i].device_id = -1 for i in 0..31
    rc = tdrv_get_host_device_id_rid_map(&map, &size)         // tdrv/init.c:188 (driver IOCTL)
    if rc == NRT_INVALID:                                     // IOCTL not implemented in this driver
        family = nrt_get_instance_info().family               // chip family enum {1,2,3,4,7}
        switch family:
            case 2, 3, 7:                                     // 16-entry permutation @0x850d90
                perm = {0,4,1,5,3,7,2,6,12,8,13,9,15,11,14,10}
                for i in 0..15:  host_did_to_rid_map[perm[i]] = i
            case 4:  host_did_to_rid_map = identity {0..11}    // 12-entry @0x850a00
            case 1:  host_did_to_rid_map = {[0]=0}, size = 0   // single null map
    else if rc != NRT_SUCCESS:
        nlog("Failed to query host device ID to routing ID map from driver")
```

> **NOTE —** the per-chip-family fallback (`[MED]`) is a real PCIe-lane↔RID swizzle, not an identity: families `{2,3,7}` install the 16-entry permutation `{0,4,1,5,3,7,2,6,12,8,13,9,15,11,14,10}` (`@0x850d90`, written as `map[perm[i]] = i`), family 4 uses a 12-entry identity, family 1 a single null map. The family→codename binding lives in `nrt_get_instance_info` (the API lane), not here. A reimplementer targeting only the modern driver can rely on the IOCTL path and treat the permutation table as a legacy-driver compatibility shim — but must reproduce the swizzle to interoperate with a driver that lacks the RID-map IOCTL. The init diagnostic carries a verbatim typo, `"...in case unterlying IOCTL is not implemented"` (`[sic]`), useful as a grep anchor.

### Function Map — hub and model DB

| Function | Address | Role | Confidence |
|---|---|---|---|
| `db_physical_core_get_mla_and_tpb` | `0x2272a0` | `(mla*,tpb*)` via `base+590008*device_id`; the hub | CERTAIN |
| `db_tdrv_ctx_init` | `0x226ca0` | construct ctx + load host-did→RID map (+ family fallback) | HIGH |
| `get_model_ref_count` | `0x2274c0` | **acquire**: lock, `ht_find`, atomic `++ref_count`, return model | HIGH |
| `model_ref_decrement` | `0x2275c0` | release: atomic `--ref_count` | HIGH |
| `add_model` | `0x2275f0` | lock, dup-check, `ht_insert` intrusive node | HIGH |
| `remove_model` | `0x227750` | lock, `ht_remove`, then **spin on `ref_count→0`** | HIGH |
| `db_get_hbm_group_pcores` | `0x227350` | collect ≤2 owned pcores in a member's HBM group | HIGH |

> **CORRECTION (DB-01) —** an early pass read `db_get_mla_from_host_device_id` (`@0x2270c0`) as a sibling-symmetric scan carrying the same NULL-ctx guard every other accessor has. Disassembly shows it **omits** that guard: unlike `db_get_mla_from_device_id`, `db_physical_core_get_mla_and_tpb`, and the rest — which `test rax,rax; je <fail>` after `db_tdrv_ctx_get` — this function dereferences `ctx->mla[]` with no `test/je`. If TDRV is uninitialized it faults; it is safe only because its sole caller (`nec_vil_get_nec_dev_for_host_nec_dev`) is invariably reached post-init. Flagged `[LOW]` pending a reachability proof that no pre-init path exists; a reimplementer should add the guard for safety.

---

## 3. The DBTC Tag Cache (db tag cache)

### Purpose

`dbtc.c` memoizes the DMA-descriptor-block counters a model needs so the refill-ring builder does not re-walk the instruction stream every time it sizes a descriptor template. It is a **two-level hierarchy of named hash tables**, anchored per-model at `model_t.dbtc_storage` (`+6296`): the outer table (`dbtc_node_lut`) maps a *name* to a `dbtc_node_t`, and each `dbtc_node_t` owns an inner table (`dbtc_hash`) mapping a key `{function_name[256], block_id}` to a 240-byte counter triple `{m2s_inc[16], s2m_inc[16], num_packets[16]}`. The DMA refill-ring path (`drs_create_data_refill_rings`, `fill_io_dma_desc_template`, `translate_one_pseudo_instr_v2`) creates a named child hash per descriptor-name, then accumulates per-block counters into it.

### Entry Point

```text
io_create_queues / drs_create_data_refill_rings
  └─ dbtc_storage_add_hash (0x227ba0)        ── calloc dbtc_node_t; ht_init child hash; name it; LUT-insert

drs_create_data_refill_rings / fill_io_dma_desc_template / translate_one_pseudo_instr_v2
  ├─ dbtc_storage_find_hash (0x227cc0)        ── ht_name_find(name) → child dbtc_hash
  ├─ dbtc_find_block (0x227920)               ── build key; ht_buf_find(0x104) → dbtc_desc_count_t*
  └─ dbtc_tail_inc_update (0x2279b0)          ── += counters into existing, else malloc(0xF0) new

ht_destroy free-callbacks:
  ├─ dbtc_free_node (0x227b80)                ── per dbtc_node: free its dbtc_hash, then free node base
  ├─ dbtc_free_hash_table (0x227b60)          ── if(h) ht_destroy(h)
  └─ dbtc_free_one (0x227900)                 ── per desc-count node: free(&node[-4])
```

### The two-level structure

```text
model_t.dbtc_storage  @+6296   dbtc_storage_t {ht_t* dbtc_node_lut}
        │
        ▼  ht_name_insert / ht_name_find  (keyed on a name string)
  dbtc_node_t (336 B / 0x150)
    +0    char  name[276]
    +280  ht_t* dbtc_hash          ──┐  child hash, sized next_pow2(block_count) capped 0x10000
    +288  ht_node_t ht_node          │  (intrusive node inside dbtc_node_lut)
                                      │
                                      ▼  ht_buf_find / ht_buf_insert  (keyed on {function_name,block_id})
                                dbtc_desc_count_t (240 B / 0xF0)
                                  +0    u32 m2s_inc[16]      (64 B)
                                  +64   u32 s2m_inc[16]      (64 B)
                                  +128  u32 num_packets[16]  (64 B)
                                  +192  ht_node_t ht_node    (intrusive; &node[-4] == payload base)
```

The key `dbtc_hash_key_t` (260 B / `0x104`) is **not** field-ordered the way the description reads — the `block_id` comes first:

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `block_id` | `+0` | `u32` | descriptor-block id within the named group | HIGH |
| `function_name` | `+4` | `char[256]` | the named-group key (`strncpy` 0x100) | HIGH |

### Algorithm

```c
// dbtc_find_block @0x227920 — build the 260-byte key and probe the child hash.
function dbtc_find_block(child_hash, function_name, block_id, out_count):
    key = {0}                                                 // memset dbtc_hash_key_t (0x104)
    key.block_id = block_id                                   // @+0
    strncpy(key.function_name, function_name, 0x100)          // @+4, 256-byte field
    node = ht_buf_find(child_hash, &key, 0x104)               // keylen = 260
    if !node:  return NULL
    *out_count = (dbtc_desc_count_t*)&node[-4]                // payload base: node@+192, 192/48=4
    return NRT_SUCCESS

// dbtc_tail_inc_update @0x2279b0 — accumulate counters; create-on-miss.
function dbtc_tail_inc_update(child_hash, function_name, block_id, m2s_inc, s2m_inc, num_packets):
    cnt = dbtc_find_block(child_hash, function_name, block_id)
    if cnt:                                                   // key present → accumulate in place
        for i in 0..15:
            cnt->m2s_inc[i]     += m2s_inc[i]
            cnt->s2m_inc[i]     += s2m_inc[i]
            cnt->num_packets[i] += num_packets[i]
        return NRT_SUCCESS
    node = malloc(0xF0)                                        // 240-byte dbtc_desc_count_t
    if !node:  nlog("Failed to allocate new node"); return NRT_FAILURE
    memcpy_sse(node, {m2s_inc, s2m_inc, num_packets})         // copy 3×64B via SSE
    ht_buf_insert(child_hash, build_key(function_name, block_id), node->ht_node)
    return NRT_SUCCESS

// dbtc_storage_add_hash @0x227ba0 — register a new named child hash.
function dbtc_storage_add_hash(storage, name, desc_block_count, out_node):
    node = calloc(1, 0x150)                                   // dbtc_node_t (336 B)
    if !node:  nlog("Failed to allocate dbtc node"); return NRT_FAILURE
    size = min(next_power_of_two(desc_block_count), 0x10000)  // cap at 65536 buckets
    if ht_init(&node->dbtc_hash, size) != 0:                  // child hash
        free(node); nlog("Failed to init dbtc hash"); return NRT_FAILURE
    strncpy(node->name, name, ...)
    ht_name_insert(storage->dbtc_node_lut, &node->ht_node)    // outer LUT, keyed by name
    *out_node = node
    return NRT_SUCCESS

// dbtc_storage_find_hash @0x227cc0 — name → child dbtc_hash.
function dbtc_storage_find_hash(storage, name):
    node = ht_name_find(storage->dbtc_node_lut, name)
    if !node:  return NULL
    return node[-1].next                                      // == dbtc_hash (288−48+40=280)
```

> **QUIRK —** the key struct `dbtc_hash_key_t` is **not** stored in the order it reads. The 256-byte `function_name` is the dominant identity, but `block_id` (a `u32`) sits at offset `+0` and `function_name[256]` at `+4`, giving a `260`-byte key (`0x104`) probed by `ht_buf_find`/`ht_buf_insert`. A reimplementer who lays the struct out `{char name[256]; u32 block_id;}` (the "natural" order) produces a different byte image and every probe misses, because the hash is over the raw `0x104` bytes. The layout is `{u32 block_id; char function_name[256];}` — `block_id` first.

> **GOTCHA —** the intrusive-node free callbacks index *backward* from the `ht_node`, and the displacements differ per table because the node sits at a different offset in each payload. For `dbtc_desc_count_t` the node is at `+192` so the payload base is `&node[-4]` (`192/48`); `dbtc_free_one` (`@0x227900`) frees `&node[-4]`. For `dbtc_node_t` the node is at `+288` so the base is `&node[-6]` (`288/48`), and `dbtc_free_node` (`@0x227b80`) first frees the child `dbtc_hash` (recovered as `node[-1].next == +280`) *then* frees `&node[-6]`. A reimplementer must register the right free callback per table — a `dbtc_node`'s callback must tear down its child hash before freeing the node, or the child table leaks.

### Function Map — dbtc

| Function | Address | Role | Confidence |
|---|---|---|---|
| `dbtc_find_block` | `0x227920` | build `{function_name,block_id}` key; `ht_buf_find(0x104)` → counter | HIGH |
| `dbtc_tail_inc_update` | `0x2279b0` | `+=` into existing counter, else `malloc(0xF0)` + insert | HIGH |
| `dbtc_storage_add_hash` | `0x227ba0` | `calloc` `dbtc_node_t`; `ht_init` child (next_pow2, cap `0x10000`); name + LUT-insert | HIGH |
| `dbtc_storage_find_hash` | `0x227cc0` | `ht_name_find(name)` → child `dbtc_hash` | HIGH |
| `dbtc_free_one` | `0x227900` | ht free-cb: `free(&node[-4])` (desc-count payload base) | HIGH |
| `dbtc_free_node` | `0x227b80` | ht free-cb: free child `dbtc_hash` then `free(&node[-6])` (node base) | HIGH |
| `dbtc_free_hash_table` | `0x227b60` | `if(h) ht_destroy(h)` | HIGH |

---

## 4. The vtpb Translation (Virtual-TPB / LNC)

### Purpose

`vtpb.c` translates between **virtual cores** (LNC — Logical NeuronCore, a group of physical cores presented as one) and the **physical cores** (ptpb/TPB) that constitute them. A virtual core of `core_size` physical cores expands to physical indices `vtpb*core_size .. +core_size-1` and collapses back by integer division. The runtime-wide factor is `ctx_vtpb_core_size` (`u32 @0xcabb40`); the live virtual cores are held in `owned_vcores` (`@0xca7320`, 2048 B = 32×`virtual_core_t`) with `num_owned_vcores` (`@0xca7300`). Every *stateful* wrapper gates on `ctx_vtpb_core_size != 0` and logs `"lnc context has not been initialized yet"` otherwise; the `_no_state` variants take `core_size` as an argument and are callable before context init.

### The struct and the arithmetic

`virtual_core_t` (size `64`) — one LNC:

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `vtpb_idx` | `+0` | `u32` | this LNC's index | HIGH |
| `vtpb` | `+4` | `u32` | virtual-core number | HIGH |
| `nec_dev_id` | `+8` | `u32` | Neuron device id | HIGH |
| `num_tpbs` | `+12` | `u32` | LNC size (== `ctx_vtpb_core_size` at runtime) | HIGH |
| `tpbs[2]` | `+16` | `physical_core_t[2]` | inline constituent cores; `tpbs[0]` = owner | HIGH |

```text
ptpb       = vtpb * core_size + k     (k in 0..core_size-1)   vtpb → ptpb expansion
vtpb       = ptpb / core_size                                 ptpb → vtpb collapse
vtpb_count = ceil(ptpb_count / core_size)                     ceil-div
ptpb_count = vtpb_count * core_size
```

### Algorithm

```c
// vtpb_translate_ptpb_to_vtpb_no_state @0x314600 — stateless collapse.
function ptpb_to_vtpb_no_state(ptpb, core_size, out_vtpb):
    *out_vtpb = ptpb / core_size                              // integer divide
    return NRT_SUCCESS

// vtpb_translate_ptpb_to_vtpb @0x314610 — stateful wrapper; gate on ctx_vtpb_core_size.
function ptpb_to_vtpb(ptpb, out_vtpb):
    if ctx_vtpb_core_size == 0:                               // @0xcabb40
        nlog("lnc context has not been initialized yet"); return NRT_INVALID
    *out_vtpb = ptpb / ctx_vtpb_core_size
    return NRT_SUCCESS

// vtpb_translate_vtpb_to_ptpb_no_state @0x314580 — stateless expand.
function vtpb_to_ptpb_no_state(vtpb, core_size, out_ptpbs):
    for k in 0..core_size-1:
        out_ptpbs[k] = vtpb * core_size + k                   // fill the constituent set
    return NRT_SUCCESS

// vtpb_ptpb_num_cores_to_vtpb_num_cores_no_state @0x3144b0 — ceil-div core counts.
function ptpb_num_to_vtpb_num_no_state(ptpb_count, core_size, out):
    *out = (ptpb_count + core_size - 1) / core_size           // ceil-div

// vtpb_get_physical_core_index @0x314340 — which slot in the LNC is this pcore?
function get_physical_core_index(vcore, pcore, out_index):
    for i in 0..vcore->num_tpbs-1:
        // compare the first 8 bytes {device_id, device_tpb_idx} as one qword
        if *(u64*)&vcore->tpbs[i].device_id == *(u64*)&pcore->device_id:
            *out_index = i;  return NRT_SUCCESS
    nlog("Failed to find physical core ND%u NC%u in logical core %u")
    __assert_fail("…/tdrv/vtpb.c", 0xA6)                     // miss is fatal

// vtpb_physical_core_get_peer @0x314660 — the peer core in the same LNC.
function physical_core_get_peer(vcore, rel_sg_id, out_pcore):
    if rel_sg_id >= vcore->num_tpbs:
        nlog("Invalid sg id %u for LNC size %u"); return NRT_INVALID
    *out_pcore = &vcore->tpbs[rel_sg_id]
    return NRT_SUCCESS

// vtpb_ptpb_is_ccop_owner @0x3146c0 — is this pcore the collective-comm-op owner (index-0)?
function ptpb_is_ccop_owner(vcore, pcore, out_result):
    idx = vtpb_get_physical_core_index(vcore, pcore)
    *out_result = (idx == 0)                                  // tpbs[0] is the LNC owner
    return NRT_SUCCESS
```

> **NOTE —** `vtpb_get_physical_core_index` (`@0x314340`) compares the first **8 bytes** of `physical_core_t` — `{device_id, device_tpb_idx}` read as one `u64` — as the identity key, not a field-by-field compare. The `vcore` back-pointer (`+8`) and the `fixes_gcc_bug` pad (`+16`) are excluded. A reimplementer can collapse the two `u32`s into a single 64-bit compare exactly as the binary does; the back-pointer must not participate or two handles for the same core reached via different vcores would mis-compare. A miss is **fatal** (`__assert_fail` at `vtpb.c:0xA6`), so callers must only pass a pcore that belongs to the vcore.

> **QUIRK —** the LNC owner is always `tpbs[0]`, and "is this the collective-comm-operation owner" reduces to "is its index 0" (`vtpb_ptpb_is_ccop_owner @0x3146c0`). The collectives and exec planes use this to elect the single core that drives an AllReduce/AllGather for the whole logical core; `vtpb_get_default_tpb` (`@0x314450`) returns `&vcore->tpbs[0]` for the same reason. A reimplementer must keep `tpbs[0]` as the canonical owner across the whole stack — the ownership test is positional, not a stored flag.

### Function Map — vtpb

| Function | Address | Role | Confidence |
|---|---|---|---|
| `vtpb_get_physical_core_index` | `0x314340` | linear-scan LNC for `{device_id,device_tpb_idx}` qword → index | HIGH |
| `vtpb_get_default_tpb` | `0x314450` | `&vcore->tpbs[0]` (the LNC owner) | HIGH |
| `vtpb_get_vtpb_core_size` | `0x314460` | out `ctx_vtpb_core_size`; `NRT_INVALID` if zero (most-called) | HIGH |
| `vtpb_ptpb_num_cores_to_vtpb_num_cores_no_state` | `0x3144b0` | stateless ceil-div ptpb-count → vtpb-count | HIGH |
| `vtpb_vtpb_num_cores_to_ptpb_num_cores_no_state` | `0x314520` | stateless `core_size * vtpb_count` | HIGH |
| `vtpb_translate_vtpb_to_ptpb_no_state` | `0x314580` | stateless expand vtpb → `core_size` ptpbs | HIGH |
| `vtpb_translate_vtpb_to_ptpb` | `0x3145a0` | stateful expand; also writes back `vtpb_core_size` | HIGH |
| `vtpb_translate_ptpb_to_vtpb_no_state` | `0x314600` | stateless collapse `ptpb / core_size` | HIGH |
| `vtpb_translate_ptpb_to_vtpb` | `0x314610` | stateful collapse via `ctx_vtpb_core_size` | HIGH |
| `vtpb_physical_core_get_peer` | `0x314660` | bounds-check + `&vcore->tpbs[rel_sg_id]` | HIGH |
| `vtpb_ptpb_is_ccop_owner` | `0x3146c0` | `physical_core_index(pcore) == 0` | HIGH |
| `vtpb_get_all_virtual_cores` | `0x3143e0` | out `owned_vcores` + `num_owned_vcores`; `NRT_INVALID` if uninit | HIGH |
| `vtpb_get_default_hbm_idx` | `0x314440` | tail-call `get_default_hbm_index(vcore->tpbs[0].device_tpb_idx)` | HIGH |

> **NOTE —** the stateless `_no_state` family (`@0x3144b0`, `0x314520`, `0x314580`, `0x314600`) takes `core_size` as a parameter and is callable *before* `ctx_vtpb_core_size` is set — `tdrv_init` and `nrt_init` use them during early bring-up to size the vcore→ptpb map before the context factor exists ([tdrv-lifecycle §1](tdrv-lifecycle.md)). The stateful wrappers (`@0x3145a0`, `0x314610`, …) are the post-init hot path; they read the global and gate on it. A reimplementer must keep both forms: the bootstrap path has no context to read.

## Related Components

| Name | Relationship |
|---|---|
| `tdrv_init` (`0x26a310`) | allocates and populates `tdrv_ctx_0` via `db_tdrv_ctx_init`; per-NC `ht_init` builds each `tpb_t.model_db` ([tdrv-lifecycle](tdrv-lifecycle.md)) |
| `tdrv_destroy` (`0x269a70`) | calls `db_tdrv_ctx_get` / `db_get_tpb_from_mla` / `db_tdrv_ctx_clear`; `ht_destroy`s each model DB |
| `kbl_model_add` / `kbl_model_remove` | the model-lifecycle callers of `add_model` / `remove_model` (the KMGR exec manager) |
| `drs_create_data_refill_rings` / `fill_io_dma_desc_template` / `translate_one_pseudo_instr_v2` | the DMA refill-ring builders that drive the `dbtc_*` tag cache |
| `vtpb_get_pcores_from_hbm_idx` (`tdrv/vtpb.c:124`) | `db_get_hbm_group_pcores` calls it to enumerate an HBM group before filtering to owned cores |
| `nec_vil_get_nec_dev_for_host_nec_dev` | sole caller of the unguarded `db_get_mla_from_host_device_id` (the §2 CORRECTION) |

## Cross-References

- [TDRV: Device Bring-Up and Lifecycle](tdrv-lifecycle.md) — the `tdrv_init`/`tdrv_destroy` layer that owns `tdrv_ctx` allocation and per-TPB `model_db` `ht_init`/`ht_destroy`; this page reads the object that page builds (the boundary)
- [Per-Arch Device Layer: Geometry and Address Map](arch-geometry.md) — the TPB/LNC geometry the device book's id-spaces sit on (`num_seng`, SBUF windows); `ctx_vtpb_core_size` is the LNC size this page's vtpb math divides by
- [Interned String Database](interned-strings.md) — the sibling named-hashtable idiom (`ht_name_insert`/`ht_name_find`) the dbtc `dbtc_node_lut` shares, and the stable-id pattern models are keyed by
- [Userspace Runtime Core — Internal Architecture Map](overview.md) — §4 lists the device book as cross-cutting infrastructure; this page is its full derivation
- [back to index](../index.md)
