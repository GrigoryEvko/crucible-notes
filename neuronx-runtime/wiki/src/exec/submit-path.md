# The Submit Path: Bind → Stage → Doorbell

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64 x86-64, not stripped, DWARF present). `.text` VMA == file offset, so every `0x…` is an analysis VMA verifiable against `nm -n libnrt.so`. The one kernel citation is the IOCTL number into `aws-neuronx-dkms`. Other versions will differ.*
> *Evidence grade: **Confirmed (decompiled + struct-anchored)** — the whole bind→stage→doorbell chain was read from per-function decompilation and cross-checked against the IDA `structures.json`/`enums.json`/`callgraph.json`. · Part VII — Execution Engine · [back to index](../index.md)*

## Abstract

`nrt_execute` is the runtime's hot path: it takes a loaded model handle plus an input and output feature-map set and drives one inference on the bound NeuronCores. This page documents the **submit half** of that path — everything from the public API entry down to the single doorbell IOCTL that tells the hardware sequencer "go" — and stops exactly at the blocking completion read. Harvesting the result (the notification-queue decode, error classification, and `mark_comp_efd` write) is a sibling concern owned by [the completion engine](completion-engine.md).

The shape will be familiar to anyone who has written a GPU command submitter: validate the user's buffers, translate them into a hardware-readable descriptor block, stage that block into a pre-allocated ring, then ring a doorbell. The Neuron twist is that there is almost no per-execution kernel involvement. The descriptor ring (the `model_exec_desc_q`) and the request buffer are allocated once at model load; an execution only **patches** the per-engine instruction-block addresses into a 52-byte `device_exec_request_t`, copies it to device DRAM, and appends three DMA trigger-descriptor *pairs* to the ring. The "doorbell" is a single `ioctl(SEMAPHORE_INCREMENT)` per NeuronCore — the **only** syscall on the entire submit hot path. Everything else is userspace memory traffic into device-mapped DRAM.

The path forks once, on a process-global pointer `async_exec_workers`. When it is NULL (the default) execution is **synchronous**: the submitter stages, doorbells, publishes into a lock-free queue, then *blocks* on `read(eventfd)` until the worker thread marks the sequence complete. When it is non-NULL execution is **implicitly asynchronous**: the same stage+doorbell happens inline under a worker lock, the request is pushed onto a `std::deque`, and a request handle is returned immediately. Both forks share one bind→stage→doorbell core; the page documents that core once and then the two wrappers around it.

For reimplementation, the contract is:

- **The dispatch fork** on `async_exec_workers`, and what each fork validates (sync clones host tensors to device; async *rejects* any host-allocated tensor).
- **The `device_exec_request_t` 52-byte layout** and the per-engine address arithmetic that fills it, plus the `reload_seq_config` / `load_act_dve_tables` skip-config optimization that decides whether the sequencer re-reads its config.
- **The three trigger-descriptor pairs** — request-copy, event-poke, tail-increment — appended to the ring per execution, and the model-switch collectives chain that is built once and cached.
- **The doorbell**: one `ndl_nc_semaphore_increment` per bound TPB, lowering to `ioctl(fd, 0x80084E29, {nc, evt, 1})`, and the multi-NC/LNC lockstep fan-out (arm all, then fire all).
- **The submit boundary**: the lock-free `xu_queue` publish, the `seq_id` encoding, and the `read(mark_efd)` that is the last instruction before completion takes over.

| | |
|---|---|
| **API entry** | `nrt_execute` @`0x91de0` (export `T`, version `NRT_2.0.0`) |
| **Dispatch fork** | `kmgr_exec` @`0xdfd50`, branch on global `async_exec_workers` |
| **Submit funnel** | `tpb_xu_schedule_request` @`0xe7540` (holds `submit_work_lock`) |
| **Request struct** | `device_exec_request_t` — **52 bytes** (`0x34`), built @`0x320810` |
| **Stage core** | `hw_exec_queue_add_exec_request_impl` @`0x320810` |
| **Doorbell** | `ndl_nc_semaphore_increment` @`0xc3ba0` → `ioctl(fd, 0x80084E29, …)` |
| **IOCTL** | `0x80084E29` = `NEURON_IOCTL_SEMAPHORE_INCREMENT` (#41) — *the only syscall on submit* |
| **Publish queue** | `xu_queue_t` — 256 bytes, lock-free SPSC, `xuq_client_submit` @`0xe9f10` |
| **Submit boundary** | `read(mark_efd, …, 8)` inside `kmgr_sync_exec` @`0xdca70` |
| **Fan-out** | per-vcore: one XU, `sg_count` TPBs, `sg_count` IOCTLs, lockstep |

---

## 1. Entry Point and Dispatch Fork

### Purpose

`nrt_execute` is a thin, state-guarded wrapper. Its job is to reject calls made in the wrong runtime lifecycle state, resolve the model's logical-core binding, arm tracing/profiling, and tail-call the validation-and-dispatch layer. It carries a single visible argument — the `nrt_model_t*`; the input and output feature-map sets arrive in registers and are threaded straight through to `kmgr_exec`.

### Entry Point

```text
nrt_execute (0x91de0)                       ── state guard + trace/profile arm
  └─ nrt_execute_repeat (0x91650)           ── host validation: repeat<=1, ifmap count
       └─ kmgr_exec (0xdfd50)               ── resolve handle; THE async/sync fork
            ├─ [sync]  kmgr_exec_pre (0xdf820)  → kmgr_sync_exec (0xdca70)
            └─ [async] kmgr_exec_pre (0xdf820)  → kmgr_async_exec_add_work (0xe6d20)
```

### Algorithm

```c
NRT_STATUS nrt_execute(nrt_model_t *model):       // 0x91de0
    if nrt_init_state != NRT_STATE_INIT:           // lifecycle guard
        if nrt_init_state == NRT_STATE_CLOSED: return NRT_CLOSED   // 14
        if nrt_init_state == 0:                return NRT_UNINITIALIZED // 13
        return NRT_FAILURE                                          // 1
    if nrt_gconf()->funtime: return NRT_SUCCESS    // sim short-circuit, no device

    vcore = vtpb_get_virtual_core(model->vnc_idx)  // 0x313fb0
    if !vcore: return NRT_INVALID                  // 2  "unknown logical core"

    span = nrt_sys_trace_new_event(evt=3, vcore->vtpb_idx)   // execute span open
    if nrt_profile_continuous_is_enabled():        // arm profiling (one of two modes)
        nrt_profile_start()
        rc = nrt_execute_repeat()                  // tail into validation layer
    else:
        armed = nrt_inspect_device_profile_start(model)
        rc = nrt_execute_repeat()
        if armed:                                  // inspect-on-failure profiling
            nrt_inspect_device_profile_stop_internal(model, !on_fail | (rc != 0))
    nrt_sys_trace_new_event(close=1, evt=3, span)  // execute span close
    return rc
```

`nrt_execute_repeat` (`0x91650`) rejects host-loop counts greater than one (string *"Supports for loops on the host is deprecated"*), reads `h_nn` from the model header, checks the supplied input count against `kmgr_get_ifmap_count` (string *"Incorrect number of inputs, expected: %u, received: %lu"*), then calls `kmgr_exec`. On error it can dump input-tensor CRCs and a coredump; those diagnostics are out of scope here.

### The async/sync fork

`kmgr_exec` (`0xdfd50`) is where the path divides. It resolves the handle (`db_get_nn_ref_count`; NULL → `NRT_INVALID_HANDLE`), counts how many of the input and output tensors live in host (CPU-malloc) memory, and branches on the process-global `async_exec_workers` pointer.

```c
NRT_STATUS kmgr_exec(H_NN h_nn, in_set, out_set, tracking_id):     // 0xdfd50
    mod = db_get_nn_ref_count(h_nn)               // 0xdc0c0
    if !mod: return NRT_INVALID_HANDLE
    n_in  = kbl_tensor_set_get_malloc_tensor_count(in_set)
    n_out = kbl_tensor_set_get_malloc_tensor_count(out_set)

    if async_exec_workers == NULL:                 // ---- SYNC (default) ----
        if n_in:  clone in_set  → device via kbl_tensor_set_clone_to_physical_mem
                  copy in_set   → device via kbl_tensor_set_copy
        if n_out: clone out_set → device (alloc only; copied back after)
        rc = kmgr_exec_pre(mod, in_dev, out_dev, mode=1, &resources)   // BIND
        if rc: goto cleanup
        rc = kmgr_sync_exec(mod, &resources, tracking_id)              // SUBMIT+BLOCK
        if out_dev && rc==0: kbl_tensor_set_copy(out_dev, out_set)     // results back
    else:                                          // ---- ASYNC (implicit) ----
        if (n_in | n_out):                         // host tensors forbidden in async
            return NRT_INVALID    // "Async exec mode only supports device allocated tensors…"
        rc = kmgr_exec_pre(mod, in_set, out_set, mode=ASYNC(0), &resources)  // BIND
        if rc: goto cleanup
        assert in_dev==NULL && out_dev==NULL
        rc = kmgr_async_exec_add_work(async_exec_workers, h_nn,
                                      mod->vcore->vtpb_idx, resources, &handle, tracking_id)
        if rc==0 && mod->is_profiling: kmgr_async_exec_poll(worker, handle)
    nn_ref_decrement(mod)
    return rc
```

> **QUIRK —** the exec-mode argument passed to `kmgr_exec_pre` is computed as `(async_exec_workers == NULL)`. So the *sync* path passes mode `1` and the *async* path passes mode `0` (`KMGR_EXEC_MODE_ASYNC`). The naming inverts what you would guess: mode `0` is the asynchronous mode, mode `1` is the synchronous mode (this is the `kmgr_exec_mode_t` enum, `exec_mode` field at offset `+32` of `kmgr_exec_resources_t`). Do not assume `0 == default == sync`.

> **GOTCHA —** the async fork hard-rejects any host-resident tensor with `NRT_INVALID`, while the sync fork silently *clones-and-copies* host tensors to device DRAM and copies results back. A reimplementation that treats the two modes as a transparent latency/throughput knob is wrong: they have different input contracts. Async is device-tensor-only; sync owns the host↔device staging.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nrt_execute` | `0x91de0` | State guard, trace/profile arm, tail to repeat | HIGH |
| `nrt_execute_repeat` | `0x91650` | `repeat<=1` + ifmap-count host validation | HIGH |
| `kmgr_exec` | `0xdfd50` | Handle resolve; async/sync fork; host-tensor clone | HIGH |
| `kmgr_exec_pre` | `0xdf820` | Validate IO sets; alloc resources; bind (§2) | HIGH |
| `vtpb_get_virtual_core` | `0x313fb0` | `vnc_idx` → `virtual_core_t*` | HIGH |
| `db_get_nn_ref_count` | `0xdc0c0` | Handle → `dlr_model*` (ref-bumped) | HIGH |

---

## 2. Bind and Validate

### Purpose

`kmgr_exec_pre` (`0xdf820`) is the bind stage: it validates the tensor sets, allocates the two small resource trackers an execution needs, and wires each named input/output tensor's HBM physical address into the per-engine DMA rings. After bind, the descriptor staging in §3 needs only to patch instruction-block addresses and append triggers — the tensor↔ring wiring is already done.

### Algorithm

```c
NRT_STATUS kmgr_exec_pre(mod, in_set, out_set, mode, out_resources):   // 0xdf820
    rc = dlr_check_valid_io_sets(kelf, in_set, out_set)   // 0xe5b30
    if rc: return rc                                       // tensor_io & placement
    resources      = calloc(1, 0x38)        // kmgr_exec_resources_t
    kbl_create_reference_feature_map_set(...)              // ref-bump the fmaps
    resources->tdrv_resources = calloc(1, 0x20)            // tdrv_compute_resources_t
    kbl_init_compute_resources(tdrv_resources, sg_count)
    rc = kbl_compute_build_compute_resources(...)          // 0x306790 — IO BIND
    // first-exec only: bootstrap collectives resources under hw_exec_queue_lock
    if first_exec: kbl_exec_build_and_load_cc_resources(...)  // 0x306e30
    *out_resources = resources
    return NRT_SUCCESS
```

`dlr_check_valid_io_sets` (`0xe5b30`) is two checks: **tensor IO** (`dlr_check_valid_tensor_io` @`0xe5a70`, per-TPB `kbl_model_verify_io_tensor_set` with `in=1`/`out=0`) and **tensor placement** (`dlr_check_valid_tensor_placement` @`0xe5a40` — all tensors of a set on the same virtual TPB).

The actual IO binding is in `kbl_compute_build_compute_resources` (`0x306790`): for each of the `sg_count` bound TPBs it calls **`ddrs_build_dma_rings`** (`0x3179a0`) and **`ioqs_build_swap_data`** (`0x446060`), resolving each tensor by name through `io_mr_to_name_map` and writing its HBM physical address into that engine's DMA TX/RX descriptor rings. The result is a `tdrv_compute_ioqs_resource_t` per TPB, stored in the resource tracker's `ioqs_resources[]` array.

### Struct: `kmgr_exec_resources_t` (56 bytes, `0x38`)

Allocated once per execution in `kmgr_exec_pre`; threaded through the work item to the stage core.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `trace_exec_id` | `+0x00` | `uint64_t` | trace correlation id for this execution |
| `trace_nc_idx` | `+0x08` | `uint32_t` | `= vcore->vtpb_idx` |
| `model_id` | `+0x10` | `uint64_t` | `= mod->interned_model_id` |
| `tdrv_resources` | `+0x18` | `tdrv_compute_resources_t*` | the per-TPB ring resources |
| `exec_mode` | `+0x20` | `kmgr_exec_mode_t` | `ASYNC=0` / `EXPLICIT_ASYNC=1` (see §1 quirk) |
| `sync.{in,out}_fmap_set` | `+0x28` | `kbl_feature_map_set_t*[2]` | the bound device feature maps |

> **CORRECTION (SUBMIT-1) —** the prior pass marked these offsets MED ("accessed by name only"). The IDA `structures.json` resolves them exactly as above; offsets are now HIGH confidence.

### Struct: `tdrv_compute_resources_t` (32 bytes, `0x20`)

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `gc_tracker` | `+0x00` | `dmem_list_t` (16 B) | DMA-mem GC list, walked on free |
| `ioqs_resources` | `+0x10` | `tdrv_compute_ioqs_resource_t**` | per-TPB ring resources, indexed `[0..sg_count-1]` |
| `ptpb_count` | `+0x18` | `uint32_t` | physical TPB count (`= sg_count`) |

Each `tdrv_compute_ioqs_resource_t` holds the cached I/O-queue switch descriptors: `ioq_switch_tx_descs` / `ioq_switch_rx_descs` (`al_udma_desc*`) and `ioq_switch_num_descs` at offsets `+0/+8/+0x10`, then `num_dynamic_rings` / `num_ring_sets` at `+0x18/+0x20`.

---

## 3. Stage: Building the `device_exec_request_t`

This is the heart of the submit path. `tpb_xu_schedule_request` (the funnel, §4) calls `dlr_add_to_hw_exec_queue` → `kbl_compute_setup` → **`hw_exec_queue_add_exec_request`** → **`hw_exec_queue_add_exec_request_impl`** (`0x320810`). The impl builds one 52-byte request, copies it to device DRAM, and appends the trigger descriptors that will make the hardware fetch and execute it.

### The request struct

`device_exec_request_t` is exactly **52 bytes** (`0x34`), proven by the two `dmem_buf_copyin` lengths. The IDA layout is all `uint32_t` fields:

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `do_model_switch` | `+0x00` | `uint32_t` | `= model_switch` arg (1 if model differs from last posted) |
| `eng_addresses_lo[5]` | `+0x04` | `uint32_t[5]` | per-engine (PE/ACT/POOL/DVE/SP) instruction-block addr, low 32b |
| `eng_addresses_hi[5]` | `+0x18` | `uint32_t[5]` | high 32b |
| `reload_seq_config` | `+0x2C` | `uint32_t` | 1 ⇒ sequencer must re-read TPB config (skip-config optimization) |
| `load_act_dve_tables` | `+0x30` | `uint32_t` | 1 ⇒ ACT/DVE lookup tables changed, reload them |

`cc_dma_increment` is **not** a field of this struct — it is a separate 4-byte value copied to file-offset `0x34` of the request buffer, immediately after the 52-byte body.

> **CORRECTION (SUBMIT-2) —** an earlier framing read `do_model_switch` and the skip-config flags as 1-byte fields packed at `+0x00..+0x03`, with `eng_addresses` as interleaved `{lo,hi}` 8-byte pairs. The IDA struct and the decompiler's `+4` pointer stride show the correct layout is five `uint32` engines low (`+0x04`) then five high (`+0x18`), with `reload_seq_config`/`load_act_dve_tables` as full `uint32`s at `+0x2C`/`+0x30`. The decompiler *aliased* the loop writes through `do_model_switch` and `eng_addresses_lo[4]` because of the `+4`-byte advance, which is why the byte-field reading arose. Net size is `0x34` either way; the field layout above is HIGH confidence.

### Algorithm

```c
NRT_STATUS hw_exec_queue_add_exec_request_impl(hw_exec_q, ib_addrs, req_buf, mod, model_switch):  // 0x320810
    assert req_buf != NULL
    exec_req.do_model_switch = model_switch
    memset(exec_req.eng_addresses_lo, 0, 48)      // clears engs + both flags

    // ---- skip-config decision (reload_seq_config) ----
    if gconf->enable_model_switch_skip_tpb_config_optimization && mod:
        // diff this model's flags vs the queue's cached last_exec_req_flags
        reload = (mod->exec_req_flags != hw_exec_q->last_exec_req_flags)   // 52-byte cmp
        exec_req.reload_seq_config = reload
    else:
        exec_req.reload_seq_config = 1            // optimization off ⇒ always reload

    // ---- table-reload decision (load_act_dve_tables) ----
    if mod && (mod->act_dve_table_hash == hw_exec_q->last_exec_req_tbl_hashes):  // 64-byte cmp
        exec_req.load_act_dve_tables = 0
    else:
        exec_req.load_act_dve_tables = 1
    nlog("Execution request: reload_seq_config = %d, load_act_dve_tables = %d", …)

    // ---- patch per-engine instruction-block addresses (the 5x{lo,hi}) ----
    p = &exec_req
    for i in 0..4:                                 // PE, ACT, POOL, DVE, SP
        base = ib_addrs[i].sunda.base
        addr = base->_pa + base->align_offset + ib_addrs[i].sunda.one_offset.offset
        p += 4                                      // +4 stride; writes eng_addresses_lo[i] then hi[i]
        *(p as eng_lo) = addr & 0xffffffff
        *(p as eng_hi) = addr >> 32

    // ---- push the 52-byte request to device DRAM ----
    if dmem_buf_copyin(req_buf, &exec_req, 0, 0x34): return NRT_FAILURE   // "Unable to copy execution request to device"

    // ---- TRIGGER PAIR 1: copy the 52-byte request into device DMEM ----
    src = hw_exec_q->req_buf->_pa + hw_exec_q->req_buf->align_offset
    al_udma_m2m_build_copy_descriptor(rx[0], tx[0], len=52, last=1, sow=is_sow_supported())  // 0x45cca0

    // ---- TRIGGER PAIR 2: poke the HW-exec-queue-request event ----
    evtid = tdrv_sync_get_hw_exec_queue_request_load()        // 0x30aab0
    src   = tdrv_arch_get_evt_addr(pcore->device_tpb_idx, evtid)   // 0x309f50
    al_udma_m2m_build_copy_descriptor(rx[1], tx[1], len=4, last=1, sow=…)
    hw_exec_queue_add_descriptors(hw_exec_q, tx, rx, 2)        // stage pairs 1+2 into rings

    // ---- model-switch collectives chain (first NC only; built once, cached) ----
    if model_switch && mod->cc_ctx.enc_ctx
                    && pcore->device_tpb_idx == pcore->vcore->tpbs[0].device_tpb_idx:
        if !mod->cc_init_tx_descs:                 // not yet cached → build it
            n = encd_get_num_descs_model_switch(&mod->cc_ctx) + 1     // 0x253ac0
            tx_descs = calloc(n, 0x10); rx_descs = calloc(n, 0x10)
            encd_get_model_switch_desc_info(&mod->cc_ctx, n, src[], dst[], sizes[], &num)  // 0x253c90
            for j in 0..num-2:
                assert sizes[j] <= SDMA_MAX_BD_LENGTH   // 0x10000
                al_udma_m2m_build_copy_descriptor(rx_descs[j], tx_descs[j], sizes[j], …)
            // final desc pokes the collectives-ctx-load event
            ev = tdrv_arch_get_evt_addr(idx, tdrv_sync_get_collectives_ctx_load())  // 0x30ab10
            al_udma_m2m_build_copy_descriptor(rx_descs[num-1], tx_descs[num-1], 4, …)
            mod->cc_init_tx_descs = tx_descs; mod->cc_init_rx_descs = rx_descs; mod->cc_init_ndescs = num

    // ---- cc_dma_increment trailer (separate 4B copyin AFTER the 52B body) ----
    if dmem_buf_copyin(req_buf, &cc_dma_increment, 0x34, 4): return NRT_FAILURE  // "Unable to copy CC DMA increment to device"

    // ---- TRIGGER PAIR 3: doorbell tail-increment (two descs) ----
    get_dma_queue_tail_inc_offset(model_exec_desc_q->dma_engine_offset, qid, 1)
    get_dma_queue_tail_inc_offset(model_exec_desc_q->dma_engine_offset, qid, 0)
    al_udma_m2m_build_copy_descriptor(tail_inc[0], …, len=4)
    al_udma_m2m_build_copy_descriptor(tail_inc[1], …, len=4)
    hw_exec_queue_add_descriptors(hw_exec_q, tail_inc_tx, tail_inc_rx, 2)
    if model_switch chain: hw_exec_queue_add_descriptors(hw_exec_q, cc_tx, cc_rx, num)

    // ---- cache this model's flags/hashes for the next execution's skip-config diff ----
    hw_exec_q->last_exec_req_tbl_hashes = mod->act_dve_table_hash    // 64B
    hw_exec_q->last_exec_req_flags      = mod->exec_req_flags        // 52B
    return NRT_SUCCESS
```

### The skip-config optimization

`reload_seq_config` and `load_act_dve_tables` are the runtime's way of telling the on-device sequencer *"you can skip re-reading your configuration; nothing changed since last time."* The queue caches the last-executed model's config flags (`last_exec_req_flags`, a 52-byte `exec_req_tpb_config_t`) and table hashes (`last_exec_req_tbl_hashes`, a 64-byte `act_dve_table_hash_t`) in the `hw_exec_queue_t`. Each execution diffs the current model's values against those caches:

- `reload_seq_config = (mod->exec_req_flags != last_exec_req_flags)` — covers `hw_dge_queue_map[5]`, `hw_dge_eng_bitmap[5]`, `seq_feature_flag_bitmap`.
- `load_act_dve_tables = (mod->act_dve_table_hash != last_exec_req_tbl_hashes)` — two 32-byte hashes (activation table, DVE table).

The whole optimization is gated by `gconf->enable_model_switch_skip_tpb_config_optimization`; with it off, `reload_seq_config` is unconditionally 1. After staging, the impl writes the current model's flags/hashes back into the caches (the `_mm_loadu_si128` block at the tail) so the *next* execution diffs against *this* one.

> **NOTE —** this is the runtime equivalent of avoiding a redundant pipeline-state-object rebind on a GPU: if the same model runs back-to-back, the TPB sequencer keeps its config and the request just patches instruction-block addresses. The first execution after a model switch pays the full reload; steady-state same-model execution does not.

### The three trigger-descriptor pairs

Every execution appends three TX/RX descriptor pairs (each descriptor is a 16-byte `al_udma_desc`, built by `al_udma_m2m_build_copy_descriptor` @`0x45cca0` — see [the descriptor wire format](../dma/descriptor-format.md)). The pairs are the mechanism by which the host hands work to the device sequencer entirely through DMA, with no per-execution kernel call:

| # | Pair | Length | Source | Purpose |
|---|---|---|---|---|
| 1 | req-copy | 52 B | host `req_buf` phys addr | DMA the `device_exec_request_t` into the sequencer's DMEM |
| 2 | evt-poke | 4 B | `tdrv_arch_get_evt_addr(idx, hw_exec_queue_request_load)` | write the request-load event so the Q-engine refetches config |
| 3 | tail-inc | 4 B ×2 | `get_dma_queue_tail_inc_offset(…)` | bump the model-exec ring tail (the on-ring doorbell) |

> **QUIRK —** there are **two** completion-like signals in one execution: the 4-byte **event poke** (pair 2) and the 4-byte **tail increment** (pair 3, two descs). The poke writes a hardware event that makes the sequencer re-read its config; the tail-inc advances the DMA ring's tail pointer so the engine actually fetches the new descriptors. They are not redundant — one tells the sequencer *"new config is ready"*, the other tells the DMA engine *"there is a new descriptor to fetch"*. The third pair was under-documented in the seed scan; it is required.

> **GOTCHA —** the collectives model-switch descriptor chain is built **only on the first NeuronCore of the vcore** (`device_tpb_idx == tpbs[0].device_tpb_idx`), **only on a model switch**, and **only if the model has a collectives context** (`cc_ctx.enc_ctx`). It is then *cached* on `mod->cc_init_{tx,rx}_descs` and reused for every subsequent switch to that model. A reimplementation that rebuilds the CC chain every execution will allocate-and-leak; one that builds it on every TPB will double-issue the collectives load.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `dlr_add_to_hw_exec_queue` | `0xdd820` | thin wrapper → `kbl_compute_setup` | HIGH |
| `kbl_compute_setup` | `0x306fb0` | per-TPB stage under `compute_req_lock`; bump `compute_idx_tail` | HIGH |
| `hw_exec_queue_add_exec_request` | `0x321420` | desc budget (`≤0x1000`); alloc `req_buf`; copy `ib_addrs[5]` | HIGH |
| `hw_exec_queue_add_exec_request_impl` | `0x320810` | build request; 3 trigger pairs; CC chain; skip-config | HIGH |
| `hw_exec_queue_add_descriptors` | `0x3206f0` | reserve+set descs into TX/RX rings | HIGH |
| `al_udma_m2m_build_copy_descriptor` | `0x45cca0` | build one 16-byte TX/RX descriptor pair | HIGH |
| `dmem_buf_copyin` | `0x229820` | copy host bytes into device DRAM | HIGH |
| `encd_get_num_descs_model_switch` | `0x253ac0` | size the CC model-switch desc chain | HIGH |
| `encd_get_model_switch_desc_info` | `0x253c90` | fill the CC desc src/dst/sizes | HIGH |

---

## 4. The Submit Funnel and the Per-TPB Loop

### Purpose

`tpb_xu_schedule_request` (`0xe7540`) is the single funnel through which both sync and async submissions pass. It holds the per-XU `submit_work_lock`, runs the health/queue-full gates, computes the model-switch flag, runs the collectives barrier, then drives stage (§3) and doorbell (§5) before publishing into the lock-free queue and waking the worker.

### Entry Point

```text
kmgr_sync_exec (0xdca70)                          ── sync wrapper; BLOCKS at the end
  └─ tpb_xu_schedule_exec (0xe8040)               ── calloc(0xD0) tpb_execution_info
       └─ tpb_xu_schedule_request (0xe7540)       ── LOCK submit_work_lock — THE FUNNEL
            ├─ kbl_{sync,async}_mode_exec_enc_barrier   ── collectives barrier
            ├─ dlr_add_to_hw_exec_queue (0xdd820)       ── STAGE (§3)
            ├─ dlr_kickoff_exec (0xdd890)               ── DOORBELL (§5)
            ├─ xuq_client_submit (0xe9f10)              ── PUBLISH into xu_queue
            ├─ xuq_client_trigger_next (0xea090)        ── bump scheduled_head
            └─ write(has_work_efd, 1, 8)                ── wake worker; UNLOCK
```

### Algorithm

```c
NRT_STATUS tpb_xu_schedule_request(xu, info, exec_status, out_seq_id):   // 0xe7540
    if !out_seq_id: return NRT_INVALID            // "req_id is a required parameter"
    pthread_mutex_lock(&xu->base.submit_work_lock)

    // ---- health + capacity gates ----
    if xu->base.health_status == XU_STATUS_FATAL_SHUTDOWN: { unlock; return 101 }  // NRT_EXEC_UNIT_UNRECOVERABLE
    if (scheduled_tail - exec_head) >= capacity:  { unlock; return 7 }             // NRT_QUEUE_FULL
    if info->type == STOP: …                       // model-stop request path
    if xu->base.health_status == XU_STATUS_SHUTDOWN: { unlock; return 14 }         // NRT_CLOSED

    mod        = info->exec.mod_ref
    model_switch = (xu->last_posted_model.id != mod->h_nn.id)    // <-- the switch decision

    // ---- collectives barrier (sync vs async variant) ----
    rc = info->exec.sync_exec ? kbl_sync_mode_exec_enc_barrier(mod, trace_exec_id)
                              : kbl_async_mode_exec_enc_barrier(mod, trace_exec_id)
    if rc: { unlock; return rc }                  // "Failed to exec neff barrier"

    // ---- STAGE ----
    rc = dlr_add_to_hw_exec_queue(mod, resources, &info->exec.compute_idx, model_switch)  // §3
    if rc: { health=FATAL_SHUTDOWN; unlock; return rc }

    if scheduled_head == exec_head:               // queue was idle → timestamp this submit
        info->exec.sync_point_cpu_timestamp = time_utils_current_timestamp_ns()

    // ---- DOORBELL ----
    rc = dlr_kickoff_exec(mod)                     // §5 — one IOCTL per TPB
    if rc: { health=FATAL_SHUTDOWN; unlock; return rc }

    // ---- PUBLISH + WAKE ----
    xu->last_posted_model.id = mod->h_nn.id
    assert xuq_client_submit(&xu->base.work_queue, info, exec_status, &seq) == NRT_SUCCESS
    assert xuq_client_trigger_next(&xu->base.work_queue, 0) == NRT_SUCCESS
    if write(xu->base.has_work_efd, &one, 8) == -1: abort()     // wake the worker thread
    *out_seq_id = seq
    pthread_mutex_unlock(&xu->base.submit_work_lock)
    return NRT_SUCCESS
```

The sync wrapper `tpb_xu_schedule_exec` (`0xe8040`) allocates the work item — `calloc(1, 0xD0)` — and fills the offsets that the funnel reads back: `mod_ref` @`+0x08`, `resources` @`+0x10`, `type` @`+0x00`, `device_id` @`+0x90`, `device_tpb_idx` @`+0x92`, `sync_exec` @`+0xC8`, and a `clock_gettime` submission timestamp stored at `+0x38`.

### Struct: `tpb_execution_info_t` (208 bytes, `0xD0`)

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `type` | `+0x00` | `tpb_exec_req_type_t` | `EXECUTE=0` / `STOP=1` |
| `exec.mod_ref` | `+0x08` | `dlr_model*` | ref-bumped model |
| `exec.resources` | `+0x10` | `kmgr_exec_resources_t*` | the bound resources (§2) |
| `exec.compute_idx` | `+0x18` | `uint64_t` | filled by `kbl_compute_setup` (`compute_idx_tail++`) |
| `exec.sync_point_cpu_timestamp` | `+0x20` | `uint64_t` | set when queue was idle |
| *(submission timestamp)* | `+0x38` | `timespec` | `clock_gettime(0, …)` at schedule time |
| `device_id` | `+0x90` | `uint16_t` | `= vcore->tpbs[0].device_id` |
| `device_tpb_idx` | `+0x92` | `uint16_t` | `= vcore->tpbs[0].device_tpb_idx` |
| `exec.sync_exec` | `+0xC8` | `bool` | sync vs async barrier/path selector |

> **NOTE —** IDA names the region from `+0x20` to `+0x90` an `infer_resp_t` (completion response) overlapping `wait_event_data`; the `+0x38` `clock_gettime` store and the `+0x20` `sync_point_cpu_timestamp` are concrete from the schedule-exec disassembly, so the *store offsets* are HIGH even though the inner-struct *field names* there are MED.

### The per-TPB stage loop

`kbl_compute_setup` (`0x306fb0`) is where the multi-NC lockstep staging happens. It takes the **main** TPB's `compute_req_lock` once, then loops all `sg_count` TPBs, calling `hw_exec_queue_add_exec_request` per TPB. The first TPB (`i == 0`) additionally enqueues the collectives proxy tasks (`kbl_exec_cc_enq_proxy_tasks` @`0x306fa0`). At the end it assigns the execution's `compute_idx` from the **main** TPB's `compute_idx_tail++`:

```c
NRT_STATUS kbl_compute_setup(vcore, tpb_count, h_model, tdrv_res, trace_id, model_switch, *compute_idx):  // 0x306fb0
    main_tpb = db_physical_core_get_mla_and_tpb(vcore->tpbs[0])
    pthread_mutex_lock(&main_tpb->hw_exec_queue.compute_req_lock)   // ONE lock for the fan-out
    for i in 0..tpb_count-1:
        mod = get_model_ref_count(tpbs[i], h_model)
        ioqs = tdrv_res->ioqs_resources[i]
        rc = hw_exec_queue_add_exec_request(&tpbs[i].hw_exec_queue, mod, &tdrv_res->gc_tracker,
                                            model_switch, ioqs->switch_tx, ioqs->switch_rx, ioqs->switch_n)
        if rc: { unlock; return rc }
        if i == 0: kbl_exec_cc_enq_proxy_tasks(mod, trace_id)       // first-TPB CC proxy
        model_ref_decrement(mod)
    *compute_idx = main_tpb->hw_exec_queue.compute_idx_tail++       // single monotonic index
    pthread_mutex_unlock(&main_tpb->hw_exec_queue.compute_req_lock)
    return NRT_SUCCESS
```

> **QUIRK —** the `compute_req_lock` is taken on `tpbs[0]` only, but the loop stages descriptors into *every* TPB's queue. The staging is lockstep-serialized through that single lock; there is no per-TPB locking and no parallel staging. The `compute_idx` is a *single* monotonic counter on the main TPB, shared across the fan-out — it identifies the whole multi-NC execution, not a per-NC slot.

---

## 5. The Doorbell and Multi-NC Fan-Out

### Purpose

Staging put the request and its triggers into device-mapped DMA rings, but the hardware sequencer is still idle. The doorbell is what tells each NeuronCore's sequencer to begin: a semaphore increment on the `INFERENCE_START` event. This is the **only** kernel transition on the submit hot path.

### Algorithm

```c
NRT_STATUS dlr_kickoff_exec(mod):                  // 0xdd890
    sg_count = mod->sg_count
    tpbs     = mod->vcore->tpbs
    for i in 0..sg_count-1:                         // FAN-OUT: one doorbell per bound TPB
        rc = kbl_infer_kickoff(model_ref, &tpbs[i])   // 0x307320
        if rc: { nlog("Failed to post inference request, err: %u"); break }
    return rc

NRT_STATUS kbl_infer_kickoff(mod, pcore):          // 0x307320
    db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb)
    rc = exec_kickoff_infer(pcore)                  // 0x2632e0
    ntrace_record_event(CPU_TIMESTAMP_SYNC, mla->mla_idx, tpb->idx)
    return rc

NRT_STATUS exec_kickoff_infer(pcore):              // 0x2632e0
    db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb)
    evtid = tdrv_sync_get_inference_start()         // 0x30a5c0 — the INFERENCE_START event id
    return ndl_nc_semaphore_increment(dev, pcore->device_tpb_idx, evtid, 1) != 0

int ndl_nc_semaphore_increment(device, nc_index, semaphore_index, value):   // 0xc3ba0
    struct { uint32 nc_index; uint32 semaphore_index; uint32 value; } arg
    arg = { nc_index, semaphore_index, value }
    return ioctl(device->fd, 0x80084E29, &arg)      // NEURON_IOCTL_SEMAPHORE_INCREMENT (#41)
```

### The single IOCTL

`ndl_nc_semaphore_increment` (`0xc3ba0`) is a four-line function: pack a 12-byte struct `{nc_index, semaphore_index, value}` and call `ioctl`. Decoding the request number `0x80084E29` (standard Linux `_IOC` encoding):

| Field | Value | Meaning |
|---|---|---|
| dir | `0b10` | `_IOC_WRITE` (userspace → kernel) |
| size | `0x008` | 8-byte declared payload size in the `_IOC` field |
| type | `0x4E` (`'N'`) | the Neuron IOCTL magic |
| nr | `0x29` (41) | `NEURON_IOCTL_SEMAPHORE_INCREMENT` |

> **QUIRK —** the `_IOC` size field encodes `8`, but the struct the function actually passes is **12 bytes** (`{nc, sem, value}`, three `uint32`). The kernel reads three fields regardless; the size mismatch is benign because the driver copies a fixed-size struct. A reimplementation must pass all three `uint32`s even though the IOCTL number claims 8 bytes.

> **GOTCHA —** this is the *only* syscall on the submit hot path. Everything else — the request copy, the trigger descriptors, the ring tail bump — is plain stores into device-mapped DRAM, no kernel involvement. A reimplementer profiling submit latency should expect exactly **`sg_count` ioctls** per execution and zero other syscalls until the blocking `read` at the boundary.

### Multi-NC / LNC lockstep

A model is bound at load time to `sg_count` physical TPBs (`mod->sg_count`, `mod->vcore->tpbs[0..sg_count-1]`). Both the stage loop (`kbl_compute_setup`, §4) and the doorbell loop (`dlr_kickoff_exec`) iterate the same `tpbs[]` array in order. The pattern is **arm-all-then-fire-all**: §4 stages descriptors into every TPB's ring first, then §5 doorbells every TPB. There is exactly one XU (execution unit) per vcore (`tpb_xu_get_by_vcore` @`0xe7b10`), so the work item is enqueued **once** even though `sg_count` cores execute it. No dynamic dispatch, no work-stealing — the fan-out is static and lockstep.

```text
            ┌──── stage (kbl_compute_setup, under tpbs[0].compute_req_lock) ────┐
vcore ──────┤  tpbs[0].ring ← req+triggers   tpbs[1].ring ← req+triggers   …    │
            └────────────────────────────────────────────────────────────────  ┘
            ┌──── doorbell (dlr_kickoff_exec) ──────────────────────────────────┐
            │  ioctl(SEM_INC, tpbs[0])   ioctl(SEM_INC, tpbs[1])   …  ×sg_count  │
            └────────────────────────────────────────────────────────────────  ┘
            ┌──── publish ONCE ─────────────────────────────────────────────────┐
            │  xuq_client_submit(work_queue)   write(has_work_efd)               │
            └────────────────────────────────────────────────────────────────  ┘
```

---

## 6. Publish and the Submit Boundary

### The lock-free publish

`xuq_client_submit` (`0xe9f10`) publishes the work item into the per-XU `xu_queue_t` — a 256-byte lock-free single-producer/single-consumer ring whose cursors are placed on separate cache lines to avoid false sharing:

```c
NRT_STATUS xuq_client_submit(xq, item, exec_status, out_seq_id):   // 0xe9f10
    if (scheduled_tail - exec_head) >= capacity: return NRT_QUEUE_FULL
    seq = (scheduled_tail & 0xFFFFFFFFFFFF) | (xu_id << 48)         // 48-bit counter | 16-bit XU id
    slot = &buffer[scheduled_tail % capacity]
    slot->seq_id       = seq
    slot->exec_status  = exec_status
    slot->exec_info    = item
    slot->mark_comp_efd = -1                       // completion sets this later
    *out_seq_id = seq
    _InterlockedAdd64(&scheduled_tail, 1)          // atomic producer advance
    assert scheduled_tail < NRTA_SEQ_NUM_MAX (0xFFFFFFFFFFFE)
    return NRT_SUCCESS
```

### Struct: `xu_queue_t` (256 bytes) and slot

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `xu_id` | `+0x00` | `uint16_t` | this XU's id (goes in the high 16 bits of `seq_id`) |
| `buffer` | `+0x08` | `xuq_exec_info_t*` | ring of slots |
| `capacity` | `+0x10` | `uint64_t` | ring depth |
| `exec_head` | `+0x40` | `uint64_t` | consumer cursor (separate cache line) |
| `scheduled_head` | `+0x80` | `uint64_t` | released-to-worker cursor |
| `scheduled_tail` | `+0xC0` | `uint64_t` | producer cursor |

Each `xuq_exec_info_t` slot (32 B): `seq_id` `+0x00`, `exec_status*` `+0x08`, `mark_comp_efd` `+0x10` (init `-1`), `efd_from_pool` `+0x14`, `exec_info*` `+0x18`.

> **QUIRK —** the four 64-bit cursors are deliberately spaced 64 bytes apart (`+0x10`, `+0x40`, `+0x80`, `+0xC0`) — one per cache line. The producer touches `scheduled_tail`, the worker touches `scheduled_head`/`exec_head`; isolating them on distinct lines prevents the producer's atomic add from invalidating the consumer's cache line. The `seq_id` is monotonic and **never wraps** (it asserts `< 0xFFFFFFFFFFFE`), so a 48-bit counter is the lifetime execution count per XU.

### The submit boundary (sync)

After publish, the sync wrapper `kmgr_sync_exec` (`0xdca70`) acquires a pooled completion eventfd and blocks:

```c
// inside kmgr_sync_exec, after tpb_xu_schedule_exec returns NRT_SUCCESS:
tpb_xu_sync_exec_get_pooled_comp_efd(xu, seq_id, &mark_efd)
while read(mark_efd, &event_val, 8) != 8:          // <===== THE SUBMIT BOUNDARY
    if errno == EINTR: continue                     // retry
    else: return NRT_FAILURE
if tpb_xu_get_last_completed(xu) < seq_id: abort()  // "Runtime bug … does not reflect completion"
```

`read(mark_efd, …, 8)` is the last instruction of the submit path. Everything past it — who writes `mark_comp_efd`, the NQ-ring harvest, error decode — belongs to [the completion engine](completion-engine.md). When the pooled eventfd allocation fails (`NRT_RESOURCE`), the code falls back to busy-polling `tpb_xu_get_last_completed` with `usleep(1)` until `>= seq_id` or the `wait_time` budget expires.

> **NOTE —** the `wait_time` budget is `432000.0` seconds (5 days) on the simulator platform (`tdrv_get_platform_type() ∈ {1,2}`), and `exec_timeout * (async_exec_max_inflight_requests + 1)` on real hardware. The schedule retry loop (when `tpb_xu_schedule_exec` returns `NRT_QUEUE_FULL`) backs off with `usleep(100)` and gives up after the same budget with `NRT_TIMEOUT`.

### The async submit boundary

The async path never blocks. `kaew_post_request` (`0xe5cd0`) does the same stage+barrier+doorbell **inline** under the worker's `work_queue_lock`, then returns:

```c
__int64 kaew_post_request(worker, req, out_handle):    // 0xe5cd0
    sem_wait(&worker->inflight_limit_sema)              // in-flight backpressure
    pthread_mutex_lock(&worker->work_queue_lock)
    if req->req_type == EXECUTE:
        is_switch = (worker->last_posted_model.id != req->mod_ref->h_nn.id)
        dlr_add_to_hw_exec_queue(req->mod_ref, req->resources, &compute_idx, is_switch)  // STAGE §3
        kbl_async_mode_exec_enc_barrier(mod, trace_id)                                     // barrier
        dlr_kickoff_exec(req->mod_ref)                                                     // DOORBELL §5
        req->compute_idx = compute_idx; req->is_model_switch = is_switch
        worker->last_posted_model.id = req->mod_ref->h_nn.id
        worker->consecutive_model_counter = is_switch ? 1 : counter+1
    push req onto worker->work_queue (std::deque)
    pthread_mutex_unlock(&worker->work_queue_lock)
    sem_post(&worker->has_work)                         // wake worker thread
    return *out_handle = worker->req_handle_tracker++   // returned immediately
```

The work item is `kmgr_async_exec_req` (56 B, `0x38`): `req_id` `+0x00`, `req_type` `+0x04` (`EXECUTE=0`/`MODEL_STOP=1`/`DRAIN=2`), then the `execute` union at `+0x08` (`mod_ref`, `vnc_idx` `+0x10`, `resources` `+0x18`, `compute_idx` `+0x20`, `is_model_switch` `+0x28`, `sync_point_cpu_timestamp` `+0x30`). Built by `kmgr_async_exec_add_work` (`0xe6d20`) via `malloc(0x38)` with `req_id = id_tracker++` (an `InterlockedExchangeAdd64`).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `kmgr_sync_exec` | `0xdca70` | wait budget; schedule retry; **`read(mark_efd)` boundary** | HIGH |
| `tpb_xu_schedule_exec` | `0xe8040` | `calloc(0xD0)` work item; fill offsets | HIGH |
| `tpb_xu_schedule_request` | `0xe7540` | the submit funnel; lock+gates+barrier+stage+doorbell | HIGH |
| `xuq_client_submit` | `0xe9f10` | SPSC publish; `seq_id` encode; `mark_comp_efd=-1` | HIGH |
| `xuq_client_trigger_next` | `0xea090` | bump `scheduled_head` | HIGH |
| `tpb_xu_get_by_vcore` | `0xe7b10` | one XU per vcore | HIGH |
| `kmgr_async_exec_add_work` | `0xe6d20` | `malloc(0x38)` async req; `id_tracker++` | HIGH |
| `kaew_post_request` | `0xe5cd0` | inline stage+doorbell; deque push; return handle | HIGH |
| `ndl_nc_semaphore_increment` | `0xc3ba0` | the doorbell IOCTL `0x80084E29` | HIGH |

---

## 7. Rejection and Status Codes

The submit path returns `NRT_STATUS` values directly; a reimplementer must reproduce these exact codes at these exact gates (values from the IDA `NRT_STATUS` enum).

| Code | Value | Gate | Source |
|---|---|---|---|
| `NRT_UNINITIALIZED` | 13 | `nrt_init_state == 0` | `nrt_execute` |
| `NRT_CLOSED` | 14 | `nrt_init_state == CLOSED`; XU `SHUTDOWN` | `nrt_execute` / funnel |
| `NRT_INVALID` | 2 | unknown logical core; async + host tensor; NULL `req_id` | `nrt_execute` / `kmgr_exec` / funnel |
| `NRT_INVALID_HANDLE` | 3 | handle not in model DB; bad model in setup | `kmgr_exec` / `kbl_compute_setup` |
| `NRT_QUEUE_FULL` | 7 | `scheduled_tail - exec_head >= capacity` | funnel / `xuq_client_submit` |
| `NRT_RESOURCE` | 4 | `calloc` work item fails; out of pooled eventfds | `tpb_xu_schedule_exec` / `kmgr_sync_exec` |
| `NRT_TIMEOUT` | 5 | schedule retry or completion poll exceeds `wait_time` | `kmgr_sync_exec` |
| `NRT_EXEC_UNIT_UNRECOVERABLE` | 101 | XU `health_status == FATAL_SHUTDOWN` | funnel |
| `NRT_FAILURE` | 1 | descriptor build fail; doorbell fail; incompatible state | `…_impl` / `dlr_kickoff_exec` |

> **NOTE —** a staging or doorbell failure inside the funnel sets `xu->base.health_status = XU_STATUS_FATAL_SHUTDOWN` before unlocking, so the XU is permanently poisoned (`101` on every subsequent submit). This is a fail-stop design: a half-staged or half-fired execution cannot be retried on the same XU.

---

## 8. Reimplementation Checklist

A submit-path reimplementation is correct when it reproduces, in order:

1. **State guard** — reject non-`INIT` states with `13`/`14`/`1`; honor the `funtime` short-circuit.
2. **The fork** — branch on the workers pointer; sync clones host tensors, async rejects them (`NRT_INVALID`); pass the *inverted* exec-mode (`workers==NULL ? 1 : 0`).
3. **Bind** — validate IO sets (tensor-io ∧ placement); allocate the `0x38` + `0x20` resource trackers; wire tensor HBM addrs into per-TPB rings.
4. **Build the request** — the 52-byte `device_exec_request_t` with five `{lo,hi}` engine addresses (`base->_pa + align_offset + one_offset`), the skip-config diff for `reload_seq_config`/`load_act_dve_tables`, and the separate `cc_dma_increment` at byte `0x34`.
5. **Three trigger pairs** — req-copy (52B), evt-poke (4B), tail-inc (4B×2); plus the cached, first-NC-only CC model-switch chain on a switch.
6. **Stage lockstep** — one `compute_req_lock`, loop all `sg_count` TPBs, single `compute_idx_tail++`.
7. **Doorbell** — one `ioctl(fd, 0x80084E29, {nc, INFERENCE_START, 1})` per TPB; arm-all-then-fire-all.
8. **Publish** — SPSC `xu_queue` slot, `seq_id = (tail & 0xFFFFFFFFFFFF) | (xu_id<<48)`, `mark_comp_efd=-1`, atomic tail advance, `write(has_work_efd)`.
9. **Boundary** — sync blocks on `read(mark_efd, …, 8)` (EINTR-retry + poll fallback); async returns a handle. Stop here.

> **GOTCHA —** the descriptor rings and request buffer are allocated **at model load**, not per execution. A reimplementation that allocates a ring per `execute` call will be correct functionally but will miss the entire point of the design: submit is meant to be allocation-free on the hot path. The only per-execution allocations are the `0xD0` work item (sync) or `0x38` async request, and the one-time CC desc arrays.

---

## Related Components

| Component | Relationship |
|---|---|
| [Completion Engine](completion-engine.md) | owns everything past the `read(mark_efd)` boundary: NQ harvest, error decode, `mark_comp_efd` write |
| [XU Work-Queue and Workers](xu-workers.md) | the worker thread that drains `xu_queue` and runs completion |
| [xu_queue / device_exec_request ABI](xu-queue-abi.md) | the wire layout of the queue and request structs this page fills |
| [KMGR Facade and Model DB](kmgr-facade.md) | the model-handle resolution (`db_get_nn_ref_count`) feeding `kmgr_exec` |

## Cross-References

- [Completion Engine](completion-engine.md) — the sibling half; writes the `mark_comp_efd` this page blocks on
- [The xu_queue and device_exec_request ABI](xu-queue-abi.md) — byte-level layout of `xu_queue_t` and `device_exec_request_t`
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the trigger-descriptor wire format built by `al_udma_m2m_build_copy_descriptor`
- [Exec Manager (KMGR): Facade and Model DB](kmgr-facade.md) — handle resolution and the `async_exec_workers` global
- [The XU Work-Queue and Worker Threads](xu-workers.md) — the consumer side of `has_work_efd` and the async deque
