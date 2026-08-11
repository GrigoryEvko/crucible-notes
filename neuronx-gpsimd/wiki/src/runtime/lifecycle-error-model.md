# Host Model Lifecycle + Error-Handling Model

> **Scope.** This page reconstructs the **host-side** control loop for a
> GPSIMD/Neuron model inside `libnrt.so.2.31.24.0` — from raw NEFF bytes through
> the loader spine (`nrt_load → kmgr_load_nn_nc → dlr_kelf_* → kbl_model_add`),
> into the execute loop (`nrt_execute → kmgr_exec → sync/async → the
> exec_request_state poller`), and out through the ref-counted unload inverse
> (`nrt_unload → kmgr_unload_nn → kmgr_unstage_kelf_model`). Layered on top is the
> **error model**: the single flat `NRT_STATUS` enum that is the host's only
> return channel, the **four host state machines** that drive the lifecycle, and
> the device-fault → host-status mapper that translates an on-core
> SEQ/GPSIMD self-halt into one `NRT_STATUS` value. The runtime posture is
> **fail-stop**: there is no in-place retry and no host-driven core reset on a
> hardware fault.
>
> Everything below is read from the shipped ELF — symbols, disassembly, DWARF
> `.debug_info` enums/structs, and `.rodata` strings. Addresses are virtual
> addresses; for `.text`/`.rodata` they equal the file offset (verified:
> `.text` VMA `0x3dbc0` == file offset `0x3dbc0`; `.rodata` VMA `0x7cf000` == file
> offset `0x7cf000`; `.data` VMA `0xc07e00` == file offset `0xc07e00` — **no**
> delta on this object). Binary identity: `122,956,336 B`, SONAME `libnrt.so.1`,
> BuildID `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, 17,372 functions, with
> `.debug_info` (not stripped). Tags: `HIGH/MED/LOW` × `OBSERVED` (read from
> binary/DWARF/strings) / `INFERRED` / `CARRIED` (from a sibling page). The page
> default is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag.

The device half of this loop — the on-core SEQ/GPSIMD fault that produces the
record the host reads — lives in
[SEQ Error-Handler / Fault Reporting](../firmware/seq/error-handler.md). The
broader host-runtime map is the Part-8 anchor,
[The libnrt Surface Map](libnrt-surface.md), and the execute hot path is
detailed in [Execute-Time Dispatch](execute-time-dispatch.md). The end-to-end
synthesis ties back to [The libnrt Runtime Synthesis](runtime-synthesis.md).

---

## 0. One-screen orientation

A model's host life is a straight line with one ref-counted hinge and one
poisoning branch:

```
nrt_init ──► NRT_STATE_INIT
   │
   │ nrt_load / nrt_load_collectives
   ▼
DMSTATE_INVALID ─► DMSTATE_STARTING ─► DMSTATE_RUNNING ──┐
   (kbl_model_add installs DMA rings, mem_refs,           │  (re-executable)
    sequencer stream, Pool-Q7 ucode_stage_libs)           │
                          │                                │
                          │ nrt_execute (per inference)    │
                          ▼                                 │
   EXEC_STATE_INIT ─► WAIT_BARRIER_PROXY ─► WAIT_CORE ─► DONE
                          │                                 │
              ┌───────────┼─────────────────────┐          │
       DONE,no err   recoverable(1003/1004)   FATAL(timeout/HBM/DMA/OOB/NQ/barrier)
              │             │                     │
         NRT_SUCCESS   result flagged       ERP_STEP_FATAL, exec_fatal_status
              └─────────────┴──────► re-exec   latched, FATAL-RT log, Band-C status;
                                                model stays RUNNING-but-poisoned
                          │ nrt_unload
                          ▼
   DMSTATE_STOPPING ─(ref==0: stop worker, drain, unstage, detach, release vNC)─► DMSTATE_STOPPED
   nrt_close ─► NRT_STATE_CLOSED  (further API → NRT_CLOSED 14)
```

The host's only mechanical reaction to a hardware fault is to **log and report**.
On a `(FATAL-RT-UNDEFINED-STATE)` the model is left `DMSTATE_RUNNING` but poisoned;
the operator must `nrt_unload` (and, for an uncorrectable HBM error, reload the
driver / reboot the instance). Recovery of the engine itself is the
management-core's job, not libnrt's.

---

## 1. The four host state machines

All four enums are read verbatim from the shipped DWARF `.debug_info`
(enumerator name + `DW_AT_const_value`). They are the skeleton on which the rest
of the page hangs.

### 1.1 Global runtime state — `NRT_INIT_STATE`

```c
enum NRT_INIT_STATE {        // DWARF type "NRT_INIT_STATE"
    NRT_STATE_START  = 0,    // process pre-init; nrt_* API → NRT_UNINITIALIZED(13)
    NRT_STATE_INIT   = 1,    // nrt_init() done; devices up; load/execute legal
    NRT_STATE_CHILD  = 2,    // post-fork() child view (inherited; cannot drive devices)
    NRT_STATE_CLOSED = 3,    // nrt_close() done; nrt_* API → NRT_CLOSED(14)
    NRT_STATE_NUM    = 4,
};
```

Written by `nrt_state_set(NRT_INIT_STATE)` (mangled `_Z13nrt_state_set14NRT_INIT_STATE`)
`@0xb9090`; rendered by `nrt_state_get_string()` (`_Z20nrt_state_get_stringv`)
`@0xb9060`. The guard is at the top of every public entry. Disassembly of
`nrt_execute` shows the two terminal states materialised as immediates into the
return register:

```
91fcd:  bb 0e 00 00 00   mov $0xe,%ebx    ; NRT_CLOSED        (state == NRT_STATE_CLOSED)
92005:  bb 0d 00 00 00   mov $0xd,%ebx    ; NRT_UNINITIALIZED (state != NRT_STATE_INIT)
```

> **NOTE.** `NRT_STATE_CHILD` (2) exists so a forked child can *observe* the
> runtime object it inherited but is fenced from driving the devices. It is a
> post-`fork()` defence, not a normal transition.

### 1.2 Per-model state — `DLR_MODEL_STATE` (`dlr_model.state` @ +424)

```c
enum DLR_MODEL_STATE {       // DWARF "DLR_MODEL_STATE" / "dlr_model_state_t"
    DMSTATE_INVALID  = 0,    // container allocated, not yet device-installed
    DMSTATE_STARTING = 1,    // staging in progress (kbl_model_add running)
    DMSTATE_RUNNING  = 2,    // installed + executable (inference can kick off)
    DMSTATE_STOPPING = 3,    // unload requested, draining in-flight execs
    DMSTATE_STOPPED  = 4,    // fully unstaged; ref-count hit 0
    DMSTATE_LAST     = 5,
};
```

The field is byte-pinned in the DWARF struct layout: `dlr_model` is `size 480`,
with `state` at **offset 424** typed `volatile dlr_model_state_t`, immediately
preceded by `kelf_model` (a `dlr_kelf_model_t*` at +416) and followed by
`ref_count` (a `volatile uint64_t` at +432). The `volatile` qualifier is
meaningful: this field is read and written across the async-exec worker thread
and the API thread, so the compiler is forbidden from caching it.

### 1.3 Per-inference state — `exec_state` (`exec_request_state.state` @ +0)

```c
enum exec_state {            // DWARF "exec_state" / "exec_state_t"
    EXEC_STATE_INIT               = 0,  // request initialised (exec_request_init_state @0x260c20)
    EXEC_STATE_WAIT_BARRIER_PROXY = 1,  // waiting for collectives/barrier proxy completion
    EXEC_STATE_WAIT_CORE          = 2,  // waiting for per-NC completion notification
    EXEC_STATE_DONE               = 3,  // all NCs reported; cleanup (exec_request_cleanup_state)
};
```

The dispatcher is `exec_request_progress_one_step @0x263330`; its two state
compares are visible in the prologue:

```
26334e:  83 f8 01   cmp $0x1,%eax   ; state == EXEC_STATE_WAIT_BARRIER_PROXY
263357:  83 f8 02   cmp $0x2,%eax   ; state == EXEC_STATE_WAIT_CORE
```

`exec_request_state` is `size 472`; the discriminator `state` sits at offset 0,
and the per-NC return codes follow immediately at **offset 4** as
`NRT_STATUS exec_rets[2]` (one slot per NeuronCore of a dual-NC/LNC request).

### 1.4 Per-step poller result — `ERP_STEP`

```c
enum ERP_STEP {              // DWARF "ERP_STEP" (return of progress_one_step)
    ERP_STEP_DONE     = 0,   // request finished this step → leave the round-robin
    ERP_STEP_CONTINUE = 1,   // not complete → keep polling
    ERP_STEP_FATAL    = 2,   // unrecoverable fault → abort the wait, set fatal status
};
```

> **CORRECTION.** An earlier reading tagged `ERP_STEP` with a
> DIE address; in the shipped enum table the enumerators are **un-namespaced**
> (`ERP_STEP_DONE`, not `ERP_STEP::…`), unlike the other three machines which
> carry a `<scope>::` prefix on every member. The values are unchanged (0/1/2);
> only the qualified-name shape differs. The driving loop
> `exec_wait_round_robin @0x2655c0` iterates `progress_one_step` until it returns
> `DONE` or `FATAL`.

These four enums compose cleanly: `NRT_INIT_STATE` gates whether the API may run
at all; `DLR_MODEL_STATE` is the long-lived per-model lifecycle; `exec_state` is
the short-lived per-inference lifecycle; and `ERP_STEP` is the single-step
verdict the inner poll loop spins on. A fifth machine — the latched fatal cause —
appears in §4.4.

---

## 2. The model-load sequence

Two public entries share one loader. `nrt_load_collectives` wraps the same
`nrt_load_util` with collective-communication validation on either side.

### 2.1 Entry

`nrt_load(const void* neff, size_t, int, nrt_model**, int, int) @0xa9fe0` does:

```
aa013:  call 224ae0 <nlog_coalescing_init_thread>            ; per-thread log buffer
aa0a2:  call a9920  <_Z13nrt_load_utilPKvmiPP9nrt_modelii>   ; the real loader (§2.2)
```

`nrt_load_collectives @0xaa4c0` wraps the same `nrt_load_util @0xa9920` with
`enc_validate_global_comm @0xffa50` before, and
`kmgr_trace_set_cc_global_id @0xdf750` + `kmgr_validate_replica_groups @0xe0470`
(the replica-group ↔ comm-channel count check) after.

### 2.2 `nrt_load_util @0xa9920` — host parse + admission gate

Call order (read off the disassembly):

1. `nrt_gconf @0x82670` — global config snapshot.
2. `neff_get_header_from_buffer @0x4ca2c0` — NEFF magic/header.
3. `nrt_vnc_usage_inc @0xc17b0` — reserve a virtual NeuronCore slot.
4. `vtpb_get_virtual_core @0x313fb0` — resolve the target vNC.
5. `nrt_config_parse_model_config @0x85f50`.
6. **`kmgr_load_nn_nc @0xde280`** — the device-load orchestrator (§2.3).
7. model-id cache (`dlr_db_get_nn_cached_interned_model_id`, `nrt_save_neff @0x99be0`).
8. `enc_get_glb_device_id_and_cnt @0xffb80`.

The status return register here is `%r15d` (not `%eax`/`%ebx`), and the
admission codes are pinned to exact addresses:

```
a9995:  41 bf 01 00 00 00   mov $0x1,%r15d    ; NRT_FAILURE        (1)
a9d5d:  41 bf 0e 00 00 00   mov $0xe,%r15d    ; NRT_CLOSED         (14)
a9d95:  41 bf 0d 00 00 00   mov $0xd,%r15d    ; NRT_UNINITIALIZED  (13)
a9e10:  41 bf 02 00 00 00   mov $0x2,%r15d    ; NRT_INVALID        (2)
a9e64:  41 bf 04 00 00 00   mov $0x4,%r15d    ; NRT_RESOURCE       (4)
```

> **CORRECTION.** An earlier reading placed the load-failure
> immediates in `%ebx`/`%eax` at `0xa9a1d/0xa9ce2` (`$0x4`) and `0xa9d48/0xa9d80`
> (`$0x2`). The loader actually threads its status through **`%r15d`**,
> and the actual `NRT_RESOURCE(4)` / `NRT_INVALID(2)` stores are at **`0xa9e64`**
> and **`0xa9e10`**. The status *values* are correct; the register and the byte
> offsets in the report are not. The state-guard immediates `0xe`/`0xd` also live
> in `%r15d` here (`0xa9d5d`/`0xa9d95`), distinct from the `%ebx` copies inside
> `nrt_execute`.

The "too few cores" path resolves to a distinct admission string,
`@0x7d8920`:

```
"Insufficient number of pncs, required: %u, available: %u"   →  NRT_LOAD_NOT_ENOUGH_NC (9)
```

On any failure the loader releases the reserved vNC (`nrt_vnc_usage_dec @0xc1820`)
and dumps diagnostics (`nrt_infodump @0x94030`, `nrt_core_dump @0x92b90`).

### 2.3 `kmgr_load_nn_nc @0xde280` — NEFF → KELF → stage → install

```c
// kmgr_load_nn_nc @0xde280 — the device-load orchestrator
nrt_status_t kmgr_load_nn_nc(...) {
    neff_get_header_from_buffer(...);   neff_copy_name(...);          // header + name
    neff_parse(neff) @0x4ca3f0;         // NEFF un-archive (gzipped tar; libarchive)
    neff_get_file_content @0x4cb670;    // pull kelf/graph blobs from the archive
    // telemetry: kmetric_update_nds_generic_status / _load_model_update_agg_neff_id

    dlr_kelf_load(neff, name, &kelf_nn) @0xe0830;   // §2.4 — KELF parse
    dlr_kelf_stage(kelf_nn, vcore, ..., &kelf_model) @0xe0970;  // §2.5 — device install

    tdrv_get_unique_h_model @0x3053f0;  // allocate the device H_MODEL handle
    // nrt_get_interned_model_id / kmgr_trace_set_uuid / kmgr_set_mac_count
    dlr_model::create_nds_model_info @0xddd60;       // host model-info record

    if (failure) {                      // inverse-on-failure cleanup, OBSERVED
        kmgr_unstage_kelf_model @0xdc180;
        release_tmp_neff_cache @0xdb4d0;
        free_neff_load_info @0xdb490;
    }
}
```

`neff_parse @0x4ca3f0` drives libarchive against an in-memory NEFF: the NEFF is a
**gzipped tar** (`archive_read_support_format_tar @0x4da0c0`,
`archive_read_support_filter_gzip @0x4d1f40`, `archive_read_open_memory @0x4d1810`).
A NEFF whose version is outside the supported window is rejected with
`NRT_UNSUPPORTED_NEFF_VERSION (10)`.

### 2.4 / 2.5 KELF parse + stage

`dlr_kelf_load @0xe0830` → `kelf_load @0x49a6b0` parses the compiled KELF (kbin
sections, mem_refs, patch table); a parse failure frees the half-built object via
`kelf_free @0x497b70`.

`dlr_kelf_stage @0xe0970` allocates the shared multi-TPB staging area
(`vtpb_info_shared_alloc/init_vtpb @0x3147c0/0x3148e0`), fans out across NCs
(`dlr_kelf_stage_multi_tpb_model_add @0xe0580`), and calls
`dlr_kelf_stage_model_add @0xe0730` → **`kbl_model_add @0x3058e0`** (the per-NC
install, §2.6). On error it unwinds via `kbl_model_remove @0x306440` /
`vtpb_info_shared_free @0x3148b0`.

### 2.6 `kbl_model_add @0x3058e0` — the tdrv install phase

This is where the NEFF becomes a live device program. Call order:

```c
// kbl_model_add @0x3058e0  (range 0x3058e0..0x30643b)
db_physical_core_get_mla_and_tpb(...);  al_hal_tpb_get_arch_type(...);  // → sunda/cayman/mariana
dbtc_storage_init @0x227cf0;
dma_queue_bundle_instance_lut_init @0x22ec00;   // per-engine DMA queue bundles
drs_create_data_refill_rings @0x31c850;         // data-refill descriptor rings
io_init_mr_to_name_map / io_build_mr_to_name_lookup / mr_pointer_init_pointer_vars; // kbin pointer patching
io_create_queues @0x4453a0;                     // the I/O queue set
act_local_storage_tbls_init @0x31a4e0 / dve_dynamic_config_init @0x31df50;
hw_exec_queue_count_descs @0x3213f0;            // size the hw_exec_queue ring
sequencer_setup_instr @0x4483d0;                // program the SEQ instr stream
dma_ring_setup_queue_bundles @0x22e630 → add_model @0x2275f0;
kbl_get_fmap_set @0x446960;                     // feature-map I/O set
ucode_stage_libs @0x310ea0;                     // *** Pool-Q7 GPSIMD ucode staging ***
dml_log_dev_neff_mem @0x22ada0;                 // device-memory accounting log
// on success: dlr_model.state: DMSTATE_STARTING → DMSTATE_RUNNING
```

`ucode_stage_libs @0x310ea0` (range `0x310ea0..0x311051`) is the step that
installs the GPSIMD/Vision-Q7 Pool-engine kernel image into the engine — the host
side of the device program the rest of this wiki dissects. The error cleanup is
the full RAII-style unwind: `model_free.part.0 @0x3055f0`, `buf_free @0x265f90`,
`dmem_list_free @0x229750`, `ht_destroy @0x268170`, `kbl_free_fmap_set @0x446ad0`.

> **NOTE — arch dispatch is the navigation key.** `al_hal_tpb_get_arch_type`
> returns the DWARF enum `al_hal_tpb_arch_type`:
> `AL_HAL_TPB_ARCH_TYPE_SUNDA = 2` (NC-v2), `…_CAYMAN = 3` (NC-v3),
> `…_MARIANA = 4` (NC-v4). KaenaHal embeds the per-arch `__FILE__` build paths
> (e.g. `…/KaenaHal-2.31.0.0/…/src/cayman/sdma/aws_hal_cayman_sdma_m2m.c`,
> `…/src/common/arch/al_hal_tpb_arch.c`) which are the per-arch source key. The
> v2/v3/v4 paths are byte-grounded; **Maverick (v5) is header-observed only — tag
> any v5-interior claim INFERRED.**

### 2.7 Load-path state machine

```
require NRT_STATE_INIT  ──not──► NRT_UNINITIALIZED(13) / NRT_CLOSED(14)
  └ vNC reserved (nrt_vnc_usage_inc) ──fail──► NRT_LOAD_NOT_ENOUGH_NC(9), release vNC
  └ neff_parse ─────────────────────fail──► NRT_UNSUPPORTED_NEFF_VERSION(10) / NRT_INVALID(2)
  └ kelf_load ──────────────────────fail──► NRT_INVALID(2) + kelf_free
  └ kbl_model_add (install) ────────fail──► NRT_RESOURCE(4) / NRT_FAIL_HOST_MEM_ALLOC(11)
  │                                          + model_free/buf_free/dmem_list_free/...
  └ DMSTATE_STARTING → DMSTATE_RUNNING → NRT_SUCCESS(0), *nrt_model** filled.
```

---

## 3. The model-unload / free path (ref-counted)

### 3.1 `nrt_unload(nrt_model*) @0xaa190`

`nrt_state_get_string` (lifecycle guard) → `nrt_gconf` → **`kmgr_unload_nn @0xdc450`**
(the teardown orchestrator) → `nrt_vnc_usage_dec @0xc1820` (release the vNC slot
reserved at load).

### 3.2 `kmgr_unload_nn @0xdc450` — inverse of `kmgr_load_nn_nc`

```c
// kmgr_unload_nn @0xdc450
uint64_t rc = db_get_nn_ref_count(model) @0xdc0c0;
nn_ref_decrement(model) @0xdc110;          // appears 5x — per-handle/per-replica unwind
if (rc != 0) return;                        // *** shared model: teardown waits for ref==0 ***

model->state = DMSTATE_STOPPING;            // (DMSTATE_RUNNING → DMSTATE_STOPPING)
// kmetric_unload_model_update_agg_neff_id (telemetry)
kmgr_async_exec_model_stop @0xe6c20         // signal the async worker to drain in-flight execs
    → kaew_post_request → kmgr_exec_worker_destroy @0xe5b70;
kmgr_async_exec_poll @0xe6ab0;              // wait out the drain
kmgr_unstage_kelf_model @0xdc180;           // device un-install: DMA rings, mem_refs, staged ucode, hw_exec_queue
tpb_xu_get_by_vcore @0xe7b10;  tpb_xu_model_unstage @0xe9040;   // detach from the execution unit
model->state = DMSTATE_STOPPED;             // (DMSTATE_STOPPING → DMSTATE_STOPPED)
```

> **GOTCHA — unload is ref-counted, not idempotent-by-pointer.** A model can be
> shared (multi-replica / cached interned model-id). `nn_ref_decrement` is
> invoked **five times** in `kmgr_unload_nn` to unwind per-handle and per-replica
> references; the device-side unstage only runs once the count reaches **0**.
> Calling `nrt_unload` on a still-referenced handle decrements but does **not**
> tear down the device program.

Tensor-info is freed through a separate API pair, independent of unload:
`nrt_get_model_tensor_info @0xc0090` / `nrt_free_model_tensor_info @0xc0390`.

---

## 4. The `nrt` API error model

### 4.1 `NRT_STATUS` — the single host return channel

Read verbatim from DWARF. **28 enumerators**, in three numeric bands. The gaps at
**8** and **12** are intentional (reserved); the enum is non-contiguous by design.

```c
enum NRT_STATUS {
    // --- BAND A: generic runtime status (0..15) ---
    NRT_SUCCESS                      = 0,    // call succeeded
    NRT_FAILURE                      = 1,    // generic unspecified failure
    NRT_INVALID                      = 2,    // invalid argument / malformed input (e.g. NEFF)
    NRT_INVALID_HANDLE               = 3,    // bad nrt_model* / handle
    NRT_RESOURCE                     = 4,    // out of a device resource (queues/cores/mem)
    NRT_TIMEOUT                      = 5,    // a wait timed out (see §5.4)
    NRT_HW_ERROR                     = 6,    // generic hardware error
    NRT_QUEUE_FULL                   = 7,    // submission queue full (async backpressure)
    /* 8 reserved */
    NRT_LOAD_NOT_ENOUGH_NC           = 9,    // load: too few NeuronCores ("Insufficient pncs")
    NRT_UNSUPPORTED_NEFF_VERSION     = 10,   // load: NEFF version gate failed
    NRT_FAIL_HOST_MEM_ALLOC          = 11,   // host malloc failure during load/exec
    /* 12 reserved */
    NRT_UNINITIALIZED                = 13,   // API called before nrt_init (NRT_STATE_START)
    NRT_CLOSED                       = 14,   // API called after nrt_close (NRT_STATE_CLOSED)
    NRT_QUEUE_EMPTY                  = 15,   // async poll: no completion ready
    // --- BAND B: execution result (101, 1002..1100) ---
    NRT_EXEC_UNIT_UNRECOVERABLE      = 101,  // the execution unit (XU) is permanently wedged
    NRT_EXEC_BAD_INPUT               = 1002, // bad input tensor / shape mismatch
    NRT_EXEC_COMPLETED_WITH_NUM_ERR  = 1003, // completed but with numerical (FP) errors
    NRT_EXEC_COMPLETED_WITH_ERR      = 1004, // completed but flagged a (recoverable) error
    NRT_EXEC_NC_BUSY                 = 1005, // the target NeuronCore is busy
    NRT_EXEC_OOB                     = 1006, // (0x3ee) out-of-bounds device_ip / tensor transfer
    NRT_COLL_PENDING                 = 1100, // collectives still pending (async barrier)
    // --- BAND C: hardware-fault execution result (1200..1206) ---
    NRT_EXEC_HW_ERR_COLLECTIVES      = 1200, // (0x4b0) collectives/fabric HW fault
    NRT_EXEC_HW_ERR_HBM_UE           = 1201, // (0x4b1) uncorrectable HBM ECC error
    NRT_EXEC_HW_ERR_NC_UE            = 1202, // (0x4b2) uncorrectable NeuronCore memory error
    NRT_EXEC_HW_ERR_DMA_ABORT        = 1203, // (0x4b3) DMA engine aborted
    NRT_EXEC_SW_NQ_OVERFLOW          = 1204, // (0x4b4) host Notification-Queue overflow
    NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE= 1205, // (0x4b5) HBM UE in a repairable region
    NRT_NETWORK_PROXY_FAILURE        = 1206, // (0x4b6) EFA/network-proxy (collectives) failure
};
```

The hex in the comments is the immediate `mov`'d into the result slot at the
mapping sites (§4.3). Note the bands are not just cosmetic: Band A is the
synchronous API result; Band B is "the inference ran (or was rejected) — here is
its data verdict"; Band C is "the hardware faulted mid-inference." Only Band-C
codes carry the fail-stop posture.

### 4.2 `nrt_status_priority_t` — multi-NC error ranking

When several NeuronCores fault in one inference, the host returns the **worst**
status to the caller. The ranking enum:

```c
enum nrt_status_priority_t {
    NRT_STATUS_PRIORITY_NONE     = 0,
    NRT_STATUS_PRIORITY_MEDIUM   = 1,
    NRT_STATUS_PRIORITY_HIGH     = 2,
    NRT_STATUS_PRIORITY_CRITICAL = 3,
};
```

`nrt_get_status_priority(NRT_STATUS) @0xb9790` is a compact mapper. The full
disassembly (range `0xb9790..0xb97ff`) decodes exactly as:

```c
// nrt_get_status_priority @0xb9790 — byte-exact reconstruction
nrt_status_priority_t nrt_get_status_priority(int s) {
    if (s == 0x3ec /*1004 COMPLETED_WITH_ERR*/) return NRT_STATUS_PRIORITY_CRITICAL; // 3 (0xb97e0)
    if (s <= 0x3ec) {                       // jbe @0xb9798
        if (s == 0)              return NRT_STATUS_PRIORITY_NONE;     // NRT_SUCCESS
        // s in [1..1004): result = ((unsigned)(s-5) < 2) ? 2 : 1   (sub 5; setb; +1)
        return ((unsigned)(s - 5) < 2) ? NRT_STATUS_PRIORITY_HIGH     // 2  (codes 5,6 → TIMEOUT/HW_ERROR)
                                       : NRT_STATUS_PRIORITY_MEDIUM;  // 1
    }
    if (s == 0x4b5 /*1205 REPAIRABLE_HBM_UE*/) return NRT_STATUS_PRIORITY_CRITICAL; // 3 (0xb97e0)
    if (s > 0x4b5) {                        // ja @0xb97a2 → 0xb97f0
        return (s == 0x4b6 /*1206 NETWORK_PROXY*/) ? NRT_STATUS_PRIORITY_HIGH       // 2
                                                   : NRT_STATUS_PRIORITY_MEDIUM;    // 1
    }
    // s in (1004 .. 1205):  base = 2 (HIGH), with two special cases:
    if (s == 0x4b4 /*1204 SW_NQ_OVERFLOW*/) return NRT_STATUS_PRIORITY_HIGH;        // 2 (mov $2; je ret @0xb97c6)
    // for everything else: result = (s == 0x4b0 /*1200 COLLECTIVES*/) ? HIGH : CRITICAL
    //   sbb/and $~1/add $3 encodes: equal→+3-1...=2 (HIGH); not-equal→+3=3 (CRITICAL)
    return (s == 0x4b0) ? NRT_STATUS_PRIORITY_HIGH      // 2
                        : NRT_STATUS_PRIORITY_CRITICAL; // 3
}
```

> **CORRECTION.** An earlier reading summarised the mapper as
> "codes in `[0x3ec..0x4b5]` (1004..1205) → priority 2 (HIGH)." The actual
> disassembly is finer-grained and the endpoints are **not** HIGH:
> `0x3ec` (1004, `COMPLETED_WITH_ERR`) and `0x4b5` (1205, `REPAIRABLE_HBM_UE`)
> both map to **CRITICAL (3)** via the explicit `mov $0x3,%eax; ret` at
> `0xb97e0`. Inside the open interval, the `sbb %eax,%eax; and $0xfffffffe; add $0x3`
> idiom yields **HIGH (2)** only for `0x4b0` (1200, `COLLECTIVES`) and the
> short-circuited `0x4b4` (1204, `SW_NQ_OVERFLOW`); the remaining Band-C HW
> faults (HBM-UE 1201, NC-UE 1202, DMA-ABORT 1203) score **CRITICAL (3)**. The
> "worst-wins" intent is correct; the per-code priorities in the report are not.

`nrt_get_status_priority` is called twice inside `exec_request_process_errors`
(§4.3) — verified: `objdump … | rg -c 'call.*nrt_get_status_priority'` returns
**2** — to keep a running "worst" status across the per-NC scan.

### 4.3 The device-fault → `NRT_STATUS` mapper — `exec_request_process_errors.isra.0 @0x2615b0`

This is the host translation of a device fault record (range
`0x2615b0..0x2632d7`). It consumes the drained per-NC notifications plus the
engine/DMA register state and emits one Band-B/Band-C `NRT_STATUS`. The
result-code immediates are byte-pinned (each is a `movl $imm` into the status
slot):

| Addr        | Immediate | `NRT_STATUS`                         |
|-------------|-----------|--------------------------------------|
| `0x261899`  | `$0x3ee`  | `NRT_EXEC_OOB` (1006)                |
| `0x261a30`  | `$0x4b3`  | `NRT_EXEC_HW_ERR_DMA_ABORT` (1203)   |
| `0x261ce8`  | `$0x4b0`  | `NRT_EXEC_HW_ERR_COLLECTIVES` (1200) |
| `0x261d93`  | `$0x4b6`  | `NRT_NETWORK_PROXY_FAILURE` (1206)   |
| `0x261e0b`  | `$0x4b6`  | `NRT_NETWORK_PROXY_FAILURE` (1206)   |
| `0x261e8b`  | `$0x4b4`  | `NRT_EXEC_SW_NQ_OVERFLOW` (1204)     |
| `0x2624ce`  | `$0x4b3`  | `NRT_EXEC_HW_ERR_DMA_ABORT` (1203)   |
| `0x2624e4`  | `$0x4b1`  | `NRT_EXEC_HW_ERR_HBM_UE` (1201)      |
| `0x262520`  | `$0x4b3`  | `NRT_EXEC_HW_ERR_DMA_ABORT` (1203)   |
| `0x262f4b`  | `$0x4b0`  | `NRT_EXEC_HW_ERR_COLLECTIVES` (1200) |

The classification logic, in order:

```c
// exec_request_process_errors.isra.0 @0x2615b0
notification_consume_errors(...) @0x300350;          // 0x26179d — decode TPB_ERROR records (§4.5)
// HBM uncorrectable check:
s = exec_check_hbm_uncorrectable(...) @0x2614c0;      // 0x261951
    //   repairable  → mov $0x4b5 @0x2615a5  (REPAIRABLE_HBM_UE)
    //   unrepairable→ mov $0x4b1 @0x26154f  (HBM_UE)
    //   + tdrv_printk_hardware_error @0x308940 (the FATAL-RT log)
// DMA-abort check:
if (check_dma_queue_on_aborted_eng(...) @0x2607f0)    // 0x261a2b, 0x2624c9 (eng state == 3 ⇒ aborted)
    s = NRT_EXEC_HW_ERR_DMA_ABORT;                    // $0x4b3
// OOB device_ip / tensor       → $0x3ee (NRT_EXEC_OOB)
// collectives/fabric           → $0x4b0 ; network-proxy → $0x4b6
exec_print_engine_instruction_pointer(...) @0x260640; // 0x261ad1,0x261af0,0x261b0f,0x261b2e,0x261b4d (x5: per-engine IP dump)
nrt_get_status_priority(s) x2;                        // rank worst across NCs
return ERP_STEP_FATAL;                                // on any hardware fault
```

> **CORRECTION.** An earlier reading listed `NRT_EXEC_HW_ERR_NC_UE`
> (`0x4b2`, 1202) among the immediates emitted by this mapper. There is **no**
> `$0x4b2` store anywhere in `exec_request_process_errors`
> (`objdump … | rg 'mov.*\$0x4b2'` is empty across `0x260000..0x266000`). The
> NC-UE result is set **upstream**, in `notification_consume_error_block`
> (§4.5) — that is the function that references the NC-UE `.rodata` string
> `@0x815750` ("Uncorrectable memory error … metadata: 0x%x …"). So `0x4b2` is a
> *consume-stage* verdict, not a *classify-stage* one.

> **NOTE — the five engine-IP dumps map the five TPB engines.** The
> `exec_print_engine_instruction_pointer` call appears exactly **five** times,
> one per TPB engine of the `al_hal_tpb_eng_type` enum:
> `PE=0, ACT=1, POOL=2, DVE=3, SP=4` (`AL_HAL_TPB_MAX_ENG = 5`). **POOL=2 is the
> GPSIMD/Vision-Q7 engine** — so a fault post-mortem always includes the Q7's
> last instruction pointer.

### 4.4 `exec_fatal_status` — the latched fatal cause

```c
enum exec_fatal_status {     // DWARF "exec_fatal_status" / "exec_fatal_status_t"
    EXEC_FATAL_STATUS_NONE          = 0,
    EXEC_FATAL_STATUS_OOB           = 1,
    EXEC_FATAL_STATUS_TIMEOUT       = 2,
    EXEC_FATAL_STATUS_SW_NQ_OVERFLOW= 3,
    EXEC_FATAL_STATUS_NETWORK_PROXY = 4,
    EXEC_FATAL_STATUS_BARRIER       = 5,   // the collectives hang (§5.4)
    EXEC_FATAL_STATUS_INVALID       = 6,
    EXEC_FATAL_STATUS_COUNT         = 7,
};
```

This enum *names* the seven fatal causes that select the FATAL-RT log message and
the Band-C status returned.

> **CORRECTION.** An earlier reading describes
> `exec_request_state.exec_fatal_status @+112` as "the internal discriminator"
> as though it stores one `exec_fatal_status_t` enum value. The DWARF struct
> layout shows the field at offset 112 is typed **`bool[2][7]`** — a per-NC
> (`[2]`, dual-NC/LNC) array of **seven boolean flags** indexed by the
> `EXEC_FATAL_STATUS_*` enumerators (count `_COUNT = 7`). It is therefore a
> per-NC *bitset of which fatal causes fired*, not a single latched scalar.
> Multiple causes can be flagged on the same core simultaneously; the returned
> `NRT_STATUS` is then resolved through the priority ranking (§4.2). The enum
> values themselves are confirmed.

### 4.5 The error-record consumer — `notification_consume_error_block @0x2ff250`

The per-record decode (range `0x2ff250..0x3000d7`):

```c
// notification_consume_error_block @0x2ff250
for each 16-byte device error record:
    subtype = v2_error_get_infer_error_subtype(rec)   @0x321760;  // 0x2ff4ae
    text    = v2_infer_error_get_isa_error_text(id)   @0x321740;  // 0x2ff4d2
    seqtext = v2_infer_error_get_sequencer_error_text(id) @0x321720; // 0x2ffd4c
    ring_buffer_enqueue(exec_error_ring, entry)       @0x3021d0;  // 0x2ff39d, 0x2ff3bd (x2)
    // NC-UE record → "...metadata: 0x%x..." @0x815750  → NRT_EXEC_HW_ERR_NC_UE
```

The subtype decoder is a small, byte-readable table dispatch
(`v2_error_get_infer_error_subtype @0x321760`):

```c
// v2_error_get_infer_error_subtype @0x321760 — byte-exact reconstruction
uint32_t v2_error_get_infer_error_subtype(uint64_t rec) {
    uint8_t  error_id = rec & 0xff;       // cmp $0xb @0x321768 — valid ids 0..11 (incl 0x09, 0x0a)
    uint16_t mask     = rec >> 8;         // shr $0x8
    if (error_id > 0x0b) return 0;
    if (error_id == 4)                    // 0x321790: test $0xfd6b,%ax; setne; +2  → subtype 2 or 3
        return ((mask & 0xfd6b) != 0) ? 3 : 2;
    if (error_id == 1) {                  // 0x3217a0: per-arch DVE-spurious mask
        if (al_hal_tpb_get_arch_type() == AL_HAL_TPB_ARCH_TYPE_SUNDA /*2*/)
            return ((mask & ~(SUNDA_NOTIFICATION_DVE_SPRUIOUS_ERROR_MASK @0x9e0420 | 0x7e00)) != 0);
        /* ... cayman/mariana branches ... */
    }
    return sunda_error_subtypes[error_id];   // table @0x9e0fa0, mov (%rax,%rcx,4)
}
```

The text helper indexes a 12-entry per-arch table:

```c
// v2_infer_error_get_isa_error_text @0x321740
const char* v2_infer_error_get_isa_error_text(uint32_t id) {
    if (id > 0x0b) return <default @0x83edbd>;    // cmp $0xb; ja
    return sunda_isa_errors[id];                  // table @0xbf3840, mov (%rax,%rdi,8)
}
```

> **NOTE — the `error_id` bound is the link to the device side.** The device packs
> a 16-byte `NEURON_ISA TPB_ERROR` record with `error_id 0x09` (NONFATAL) /
> `0x0a` (FATAL) plus a subtype (see
> [SEQ Error-Handler / Fault Reporting](../firmware/seq/error-handler.md)). The
> host decoder bounds `error_id` at `cmp $0xb` (≤ 11), so both `0x09` and `0x0a`
> are in-range and indexed into the per-arch text/subtype tables. The arch
> dispatch (`al_hal_tpb_get_arch_type`, `SUNDA…` masks) is exactly the per-arch
> navigation key used throughout this wiki; the `sunda_*` tables are the NC-v2
> instances. A separate `v2_infer_error_get_sequencer_error_text @0x321720`
> renders the SEQ-specific text for the FATAL (0x0a) sequencer subtype.

---

## 5. Execution error-recovery (timeout, halt detection, poller transitions)

### 5.1 The execute spine

```c
nrt_execute @0x91de0
    // guards: NRT_CLOSED(14) @0x91fcd, NRT_UNINITIALIZED(13) @0x92005
    → nrt_execute_repeat @0x91650
        → kmgr_get_ifmap_count @0xdd650
        → kmgr_exec @0xdfd50;
```

`kmgr_exec @0xdfd50` branches on the `kmgr_exec_mode` enum
(`KMGR_EXEC_MODE_ASYNC = 0`, `KMGR_EXEC_MODE_EXPLICIT_ASYNC = 1`):

* **ASYNC:** `kmgr_exec_pre @0xdf820` → `kmgr_async_exec_add_work @0xe6d20`
  (post to the worker-thread queue) → `kmgr_async_exec_poll @0xe6ab0`.
  Backpressure → `NRT_QUEUE_FULL (7)`; nothing ready → `NRT_QUEUE_EMPTY (15)`.
* **SYNC:** `kmgr_sync_exec @0xdca70` (the blocking path; §5.2).

Resource cleanup on every exit: `kbl_free_feature_map_set @0x307d50` (in/out
sets), `kmgr_exec_resources_free @0xdd350`, `nn_ref_decrement @0xdc110`.

### 5.2 `kmgr_sync_exec @0xdca70` — the blocking path

```c
// kmgr_sync_exec @0xdca70 → kmgr_exec_wait @0xdcf80 → kbl_infer_exec_wait @0x307410
tpb_xu_schedule_exec @0xe8040;             // 0xdcbdb — push the exec descriptor; doorbell fires
                                           //   (hw_exec_queue.model_exec_desc_q)
efd = tpb_xu_sync_exec_get_pooled_comp_efd @0xe87e0;  // an eventfd from a pool; host blocks on it
exec_wait_round_robin @0x2655c0:
    exec_request_init_state @0x260c20;     // state = EXEC_STATE_INIT
    do { v = exec_request_progress_one_step @0x263330; } while (v == ERP_STEP_CONTINUE);
    exec_request_cleanup_state @0x260bf0;
tpb_xu_get_last_completed @0xe8410;        // read the last completed seq-id
tpb_xu_release_pooled_eventfd @0xe87f0;
// final status + telemetry:
kmetric_update_nds_error_stats(kbl_infer_errors*) @0xe0f60;   // infer_error_flags @+0
kmetric_update_nds_exec_stats(int,int,NRT_STATUS) @0xe0d30;   // final status logged
```

> **CORRECTION.** An earlier reading calls
> `kbl_infer_errors.infer_error_flags @+0` "the per-NC error bitmap." The DWARF
> struct shows `kbl_infer_errors` is `size 9` with `infer_error_flags` typed
> **`bool[9]`** — nine distinct boolean error flags (one byte each), not a packed
> bitmap. The "field at +0" and its name are correct; its width and encoding are
> a 9-byte array of flags.

### 5.3 The completion drain (device → host)

```c
// exec_request_progress_one_step @0x263330
notification_read_exec_queue @0x2ff170      // drain the device Notification-Queue ring
    → aws_hal_notific_nq_read @0x451040;     //   0x2ff1b3 (per-NC completion + error records)
// on error records:
exec_request_process_errors @0x2615b0;       // §4.3
nrt_get_status_priority @0xb9790 x2;         // rank worst across NCs
notification_drain @0x300170;                // flush remaining NQ entries
exec_check_intc_sw_notif_queue_overflow @0x2613c0
    → aws_hal_get_user_errtrig_block_from_tpb_idx @0x450140  // 0x2613d8
    → aws_hal_intc_read_cause @0x450990;     //   0x2613ef — interrupt-cause register
// NQ ring full ⇒ EXEC_FATAL_STATUS_SW_NQ_OVERFLOW ⇒ NRT_EXEC_SW_NQ_OVERFLOW (1204)
```

This drain consumes the host Notification-Queue / MSI-X interrupt path. The
device raises the NQ interrupt; the host reads the cause register via
`aws_hal_intc_read_cause` and treats a full ring as the fatal
`EXEC_FATAL_STATUS_SW_NQ_OVERFLOW`. *(The device→host interrupt/NQ delivery
mechanism itself — the [`control/interrupt/*`](../control/interrupt/device-host-notification.md)
Part-13 pages — is a separate topic; this page documents only the host-runtime consumer of it.)*

### 5.4 Timeout / halt detection

The budget comes from the environment (`NEURON_RT_EXEC_TIMEOUT`) via `gconf` into
the per-model exec-timeout region; `exec_request_state` carries a `timeout_ms` /
`timeout_thresh` field and a `start_ts` `timespec`. The host wall-clock wait on
the completion eventfd is bounded by it (`get_timespec_delta @0x22eed0`,
`tdrv_timestamp_to_microseconds @0x22f7d0`). When a per-engine completion
notification never arrives within budget, the per-NC scan sets `NRT_TIMEOUT`:

```
2628da:  ba 05 00 00 00   mov $0x5,%edx   ; NRT_TIMEOUT (inside the per-NC scan loop @0x2628d0)
```

and latches `EXEC_FATAL_STATUS_TIMEOUT`, then emits the FATAL-RT log line
(`.rodata @0x80c948`):

```
(FATAL-RT-UNDEFINED-STATE) [ND %u][NC %u] execution timeout (%u ms) on model %s,
 waiting for execution completion notification
```

The collectives variant (`EXEC_FATAL_STATUS_BARRIER` / `NRT_EXEC_HW_ERR_COLLECTIVES`
1200) emits one of three "Suspected hang" strings, byte-pinned in `.rodata`:

```
0x80c388  ...missing collectives status on model %s. Suspected hang in waiting for barrier
0x80c400  ...Suspected hang in collectives operation %u out of %u
0x80c4b0  ...Suspected hang between the end of collectives operation %u and the start of
          collectives operation %u (out of %u total operations)
```

> **NOTE — a host timeout *is* a device halt.** The host cannot see the on-core
> spin directly. A timeout here means the **device** is the thing that stopped:
> it is the host-side mirror of the SEQ/GPSIMD self-halt described in
> [SEQ Error-Handler / Fault Reporting](../firmware/seq/error-handler.md) — once
> the on-core handler enters its permanent self-loop it never posts the
> completion notification, so the host infers the halt from the *missing*
> notification plus the expired budget. The third "hang between end of op X and
> start of op Y" string is the precise diagnostic the host emits when the
> collectives proxy has partial progress.

### 5.5 Recovery posture = fail-stop `[HIGH/OBSERVED + INFERRED]`

```c
// On any hardware data-fault:
//   exec_request_process_errors returns ERP_STEP_FATAL;
//   exec_wait_round_robin unwinds;
//   the Band-C NRT_STATUS propagates out of nrt_execute UNCHANGED;
//   the model stays DMSTATE_RUNNING but poisoned.
// There is NO retry loop and NO core-reset call on the fault path. [OBSERVED]
```

Recovery is **operator-directed**, spelled out in `.rodata`:

* **Uncorrectable HBM (reboot variant)** `@0x80c000`:
  `(FATAL-RT-UNDEFINED-STATE) [ND %u] Uncorrectable HBM memory error is detected.
  Execution results may be invalid. Please reload the neuron driver or reboot your
  EC2 instance …`
* **Uncorrectable HBM (terminate variant)** `@0x80bf10`: `… Please terminate this
  instance …`
* **NeuronCore memory UE** `@0x815750`: `… Uncorrectable memory error is detected,
  metadata: 0x%x. … Please terminate or stop/start this instance …`

> **GOTCHA — `release_run_stall` is bring-up, not fault recovery.** The per-arch
> `aws_hal_*_release_run_stall` primitives exist (cayman/mariana/sunda) and write
> the host CSR that un-stalls a core, but they are wired into the **install/bring-up**
> path, **not** the exec fault path. There is no call to them from
> `exec_request_process_errors` or anywhere on the data-fault unwind. **[INFERRED:
> the absence of a reset call in the fault path is deliberate — the design intent
> is reload-the-model, and engine recovery is the management core's job.]**

The lone "soft" recovery is Band-B `1003`/`1004`
(`COMPLETED_WITH_NUM_ERR` / `COMPLETED_WITH_ERR`): a recoverable FP/numerical
fault lets the inference **complete** and merely flags the result — no halt, no
reload. This mirrors the device's FP-only-recoverable policy.

---

## 6. Resource cleanup on error (RAII-style unwinding)

| Layer | Unwind actions (OBSERVED) |
|-------|----------------------------|
| **LOAD failure** | vNC slot (`nrt_vnc_usage_dec`) · `kelf_free` · `model_free.part.0` · `buf_free` · `dmem_list_free` · `ht_destroy` · `kbl_free_fmap_set` · `kmgr_unstage_kelf_model` · `release_tmp_neff_cache` · `free_neff_load_info` |
| **EXEC failure** | `kbl_free_feature_map_set` (in/out fmap sets) · `kmgr_exec_resources_free` · `exec_request_cleanup_state` · `tpb_xu_release_pooled_eventfd` · `nn_ref_decrement`. The **model is not freed** on exec error — only the per-inference resources are released; the model may be re-run or explicitly unloaded. |
| **UNLOAD** | ref-counted (`nn_ref_decrement` until 0) → `kmgr_unstage_kelf_model` → `tpb_xu_model_unstage`; vNC released **last** (§3.2). |

Diagnostics on a hard failure: `nrt_infodump @0x94030` + `nrt_core_dump @0x92b90`
(load path); `tdrv_printk_hardware_error @0x308940` +
`exec_print_engine_instruction_pointer @0x260640` (exec path).

> **NOTE — the EXEC unwind keeps the model alive on purpose.** This is the
> structural difference between a recoverable exec error and a load error: a
> failed load *destroys* the half-built model (it never reached `DMSTATE_RUNNING`),
> whereas a failed exec leaves a `DMSTATE_RUNNING` model in place. Even a Band-C
> fatal exec error does not auto-free the model — it leaves it RUNNING-but-poisoned
> for the operator's `nrt_unload`.

---

## 7. End-to-end error-propagation model

```
on-core SEQ/GPSIMD fault  (device side; see firmware/seq/error-handler.md)
   │ packs 16-byte NEURON_ISA TPB_ERROR (error_id 0x09 NONFATAL / 0x0a FATAL + subtype),
   │ raises NQ interrupt (MSI-X), self-halts (never posts completion)
   ▼
host NQ ring  ──aws_hal_notific_nq_read @0x451040──► notification_read_exec_queue @0x2ff170
   ▼
notification_consume_errors @0x300350 → consume_error_block @0x2ff250
   │  → v2_error_get_infer_error_subtype @0x321760  (cmp $0xb on error_id)
   │  → v2_infer_error_get_isa_error_text @0x321740 / _sequencer_error_text @0x321720
   │  → ring_buffer_enqueue(exec_error_ring) @0x3021d0
   ▼
exec_request_process_errors.isra.0 @0x2615b0
   │  HBM-UE? (0x4b1/0x4b5)   DMA-abort? (0x4b3)   OOB? (0x3ee)
   │  NQ-overflow? (0x4b4)    collectives? (0x4b0)  network-proxy? (0x4b6)
   │  → set Band-C/Band-B NRT_STATUS + exec_fatal_status[nc][cause] + FATAL-RT log
   ▼
nrt_get_status_priority @0xb9790 x2  (rank worst across NCs)  →  ERP_STEP_FATAL
   ▼
exec_wait_round_robin → kbl_infer_exec_wait → kmgr_sync_exec → kmgr_exec
   → nrt_execute_repeat → nrt_execute RETURN
   ▼
caller sees ONE NRT_STATUS  (e.g. NRT_TIMEOUT 5, NRT_EXEC_HW_ERR_HBM_UE 1201,
   NRT_EXEC_HW_ERR_DMA_ABORT 1203, NRT_EXEC_SW_NQ_OVERFLOW 1204, NRT_EXEC_OOB 1006,
   NRT_EXEC_HW_ERR_COLLECTIVES 1200, NRT_NETWORK_PROXY_FAILURE 1206).  No auto-retry.
```

The single most important property for a re-implementer: **the device-fault
verdict crosses exactly one funnel** — the per-NC `notification` records →
`exec_request_process_errors` → a `priority`-ranked single `NRT_STATUS` — and that
value is returned to the API caller unchanged. There is no host-side state that
"holds open" a fault for retry; the fault is reported and the model is poisoned.

---

## 8. Key address table `[HIGH/OBSERVED — sidecar + disasm]`

| Symbol | Addr | Symbol | Addr |
|--------|------|--------|------|
| `nrt_load` | `0xa9fe0` | `nrt_load_collectives` | `0xaa4c0` |
| `nrt_load_util` | `0xa9920` | `nrt_unload` | `0xaa190` |
| `nrt_execute` | `0x91de0` | `nrt_execute_repeat` | `0x91650` |
| `kmgr_load_nn_nc` | `0xde280` | `kmgr_unload_nn` | `0xdc450` |
| `dlr_kelf_load` | `0xe0830` | `dlr_kelf_stage` | `0xe0970` |
| `dlr_kelf_stage_model_add` | `0xe0730` | `kbl_model_add` | `0x3058e0` |
| `kelf_load` | `0x49a6b0` | `kelf_free` | `0x497b70` |
| `neff_parse` | `0x4ca3f0` | `neff_get_header_from_buffer` | `0x4ca2c0` |
| `ucode_stage_libs` | `0x310ea0` | `hw_exec_queue_count_descs` | `0x3213f0` |
| `kmgr_exec` | `0xdfd50` | `kmgr_exec_pre` | `0xdf820` |
| `kmgr_sync_exec` | `0xdca70` | `kmgr_exec_wait` | `0xdcf80` |
| `kbl_infer_exec_wait` | `0x307410` | `exec_wait_round_robin` | `0x2655c0` |
| `exec_request_progress_one_step` | `0x263330` | `exec_request_init_state` | `0x260c20` |
| `exec_request_cleanup_state` | `0x260bf0` | `exec_request_process_errors.isra.0` | `0x2615b0` |
| `notification_read_exec_queue` | `0x2ff170` | `notification_consume_errors` | `0x300350` |
| `notification_consume_error_block` | `0x2ff250` | `notification_drain` | `0x300170` |
| `exec_check_hbm_uncorrectable` | `0x2614c0` | `exec_check_intc_sw_notif_queue_overflow` | `0x2613c0` |
| `v2_error_get_infer_error_subtype` | `0x321760` | `v2_infer_error_get_isa_error_text` | `0x321740` |
| `v2_infer_error_get_sequencer_error_text` | `0x321720` | `ring_buffer_enqueue` | `0x3021d0` |
| `nrt_get_status_priority` | `0xb9790` | `nrt_state_set` | `0xb9090` |
| `nrt_state_get_string` | `0xb9060` | `aws_hal_notific_nq_read` | `0x451040` |
| `aws_hal_intc_read_cause` | `0x450990` | `kmgr_unstage_kelf_model` | `0xdc180` |
| `tpb_xu_model_unstage` | `0xe9040` | `nn_ref_decrement` | `0xdc110` |
| `tpb_xu_schedule_exec` | `0xe8040` | `tpb_xu_get_last_completed` | `0xe8410` |
| `nrt_get_model_tensor_info` | `0xc0090` | `nrt_free_model_tensor_info` | `0xc0390` |

---

## 9. Confidence & gaps

* **HIGH × OBSERVED:** binary identity (size `122,956,336`, BuildID, SONAME,
  section VMA==fileoffset); all six DWARF enums (`NRT_INIT_STATE`,
  `DLR_MODEL_STATE`, `exec_state`, `ERP_STEP`, `exec_fatal_status`,
  `nrt_status_priority_t`) and the 28-value `NRT_STATUS`; the byte-pinned struct
  offsets (`dlr_model.state @+424` `volatile`, `exec_request_state.state @+0` +
  `exec_rets[2] @+4` + `exec_fatal_status bool[2][7] @+112`, `kbl_infer_errors
  bool[9]`); the priority-mapper reconstruction (`0xb9790`); every result-code
  immediate in `exec_request_process_errors` and `exec_check_hbm_uncorrectable`;
  the `nrt_load → nrt_load_util` and `kmgr_sync_exec → tpb_xu_schedule_exec` call
  edges; the NQ-drain / intc-overflow call spine; all FATAL-RT `.rodata` strings
  at their pinned addresses; the `error_id ≤ 0x0b` decode bound; the
  `al_hal_tpb_arch_type` (SUNDA=2/CAYMAN=3/MARIANA=4) and `al_hal_tpb_eng_type`
  (POOL=2) enums; KaenaHal per-arch `__FILE__` build paths.
* **CORRECTIONS embedded (report vs binary):** priority mapper endpoints
  (1004/1205 → CRITICAL, not HIGH; only 1200/1204 → HIGH) §4.2; `0x4b2` NC-UE set
  in the *consume* stage, not the *mapper* §4.3; `exec_fatal_status` is
  `bool[2][7]` per-NC flags, not a scalar enum §4.4; load-failure status flows
  through `%r15d` (e.g. `0xa9e64`/`0xa9e10`), not `%ebx`/`%eax` at the report's
  offsets §2.2; `kbl_infer_errors.infer_error_flags` is `bool[9]`, not a packed
  bitmap §5.2; `ERP_STEP` enumerators are un-namespaced §1.4.
* **INFERRED (flagged inline):** the fail-stop *intent* from the absence of a
  reset call on the fault path (§5.5); `release_run_stall` being a bring-up
  primitive only.
* **WALL:** Maverick (NC-v5) is header-observed only — any v5-interior claim is
  INFERRED. The device-side NQ/interrupt delivery
  ([`control/interrupt/*`](../control/interrupt/device-host-notification.md)),
  the device fault chain ([`control/security/*`](../control/security/boot-fault-overview.md)),
  and the full `exec_request_state` census
  ([`appendix/struct-exec-state-census`](../appendix/struct-exec-state-census.md)) are
  separate topics referenced here by name; this page documents
  only the host-runtime consumer.
