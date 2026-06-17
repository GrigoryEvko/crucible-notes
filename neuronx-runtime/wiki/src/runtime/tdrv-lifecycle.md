# TDRV: Device Bring-Up and Lifecycle

> *All addresses, offsets, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`, `.rodata`, **and `.data` are VMA == file offset** (`.bss` is `NOBITS`). All `0x2…` / `0x3…` are analysis VMAs. Provenance strings `/opt/workspace/KaenaRuntime/tdrv/{init.c, tdrv_arch_type.c, ds.c}` and `/opt/workspace/KaenaRuntime/ucode/ucode.c` root every function. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — struct layouts from DWARF, vtable wiring from `R_X86_64_RELATIVE` relocations on the real ELF, call order from the IDA call graph. · Part IV — TDRV Runtime · [back to index](../index.md)*
>
> **CORRECTION —** an earlier revision of the line above claimed a `0x400000` `.data`/`.bss` VMA↔file-offset delta. That is **wrong** for `libnrt.so`. `readelf -lW` shows the RW `LOAD` segment as `LOAD 0xbeeaa0 ... 0xbeeaa0 ... RW` (Offset == VirtAddr, delta **zero**); `.data` sits at `Address 0xc07e00 / Off c07e00`. The `0x400000` delta belongs to a *different* binary (the libtpu / Kaena-profiler image), not libnrt — read `.data` globals at their VMA directly.

## Abstract

TDRV (the **tonga device-runtime**) is the device-driver core that sits directly below the public runtime API and directly above NDL (the IOCTL/mmap driver layer) and the KaenaHal register layer. When `nrt_init` ([api-lifecycle](api-lifecycle.md)) reaches its subsystem-init phase it calls `kmgr_init`, and `kmgr_init` calls **`tdrv_init` (`@0x26a310`)** — the single master function (11,922 bytes, 427 basic blocks, 244 distinct callees) that opens every Neuron device, resets its NeuronCores, scrubs HBM, builds a per-process **`tdrv_ctx`** holding one `mla_t` per device, and brings each NeuronCore from cold silicon to a running idle program. `tdrv_destroy` (`@0x269a70`, 2,204 B) unwinds that bring-up in reverse. This page owns the device-level bring-up that `nrt_init` calls into; it does **not** re-derive `nrt_init` itself (owned by [api-lifecycle](api-lifecycle.md)) nor the `nrt_tensor_t` object (owned by [tdrv-tensor](tdrv-tensor.md)).

The reference frame is a classic layered device-driver core with a **per-arch ops vtable**. Two dispatch tables anchor the whole layer. The first is **`tdrv_arch_ops` (`@0xc97180`, 488 B / 61 physical 8-byte slots (47 named members))** — a struct-of-function-pointers populated exactly once, lazily, by one of `tdrv_arch_register_{sunda,cayman,mariana}` according to the silicon's arch type; every `tdrv_arch_get_*` geometry accessor (`get_num_tpb`, `get_default_hbm_index`, the CSR register/deregister hooks, the sync-event tables) routes through a slot in this table. The second is the **nrtucode platform-ops vtable** — two small statically-relocated `.data` tables (`platform_memhandle_impl @0xbf6c40`, `platform_rw_impl @0xbf6c80`) that hand the Q7/GPSIMD microcode runtime ([gpsimd](../gpsimd/extisa-provider.md)) a set of callbacks to allocate, read, write, and free device DRAM through TDRV's `dmem` allocator. TDRV registers these by passing both table pointers into `ucode_core_create` for every microcode core it builds during bring-up.

The page applies the recurring H3 vocabulary to five units: **(1)** the `tdrv_init` master bring-up sequence and its ordered call-tree; **(2)** `ensure_arch_ops_initialized` / `tdrv_arch_ops_init` — the lazy arch-ops install; **(3)** the nrtucode platform-ops registration (the `platform_memhandle_*` + `device_close_fn` callbacks); **(4)** the NDS-for-device hookup (`tdrv_init_nds_for_device` → `save_process_info`, and its `tdrv_close_nds_for_device` inverse); and **(5)** `tdrv_destroy` teardown in reverse order, including the parallel `pthread`-driven device close.

For reimplementation, the contract is:

- **The bring-up order** — identify platform/arch → negotiate UltraServer mode → map vcores→ptpb → alloc `tdrv_ctx` → per-device {open / reset NCs / HBM scrub / `dmem` allocator / DMA-memory} → per-NC {INTC / notific / HAL / ucode cores / scratchpad / hw-exec-queue / sync-point / stochastic-rounding seed} → tsync timestamps → NDS-for-device → start exec-ready program. A reordering that, e.g., builds ucode cores before the per-TPB `dmem` allocator exists will fault.
- **The arch-ops lazy-singleton** — `tdrv_arch_ops_initialized @0xc97368` gates a one-shot `switch(arch_type){2→sunda,3→cayman,4→mariana}`; every accessor double-checks the flag and on failure tail-calls the `__noreturn` `ensure_arch_ops_initialized`.
- **The platform-ops vtable layout** — `nrtucode_platform_memhandle_t` (40 B, 5 slots: `device_malloc/device_free/read_memhandle/write_memhandle/get_memhandle_soc_addr`) and `nrtucode_platform_rw_t` (32 B, 4 slots), each a `dmem`-backed shim; the `nrtucode_memhandle_t` opaque handle is a `dmem_t*` and `soc_addr = dmem->_pa + dmem->align_offset`.
- **The `mla_t` device record** — `tdrv_ctx` is an array of `mla_t` (stride 590,008 B); the device handle for close/NDS lives at `mla->device` (`+314664`, IDA `loc_4CD28`), the NDS slot key at `mla->mla_idx`.
- **The teardown order** — halt the ready-exec program (semaphore-increment + consume HALT notification), destroy ucode cores/libs, notifications, scratchpad, DMA rings, hw-exec-queue, `dmem` allocator, then close devices in parallel and free the ctx.

| | |
|---|---|
| **Master init** | `tdrv_init` `@0x26a310` — 11,922 B, 427 BBs, 244 callees, 2072-B frame |
| **Teardown** | `tdrv_destroy` `@0x269a70` — 2,204 B, 83 BBs, 66 callees |
| **Caller** | `kmgr_init` `@0xde080` (`nrt_init` → `kmgr_init` → `tdrv_init`) |
| **Arch-ops vtable** | `tdrv_arch_ops` `@0xc97180` (.bss, **488 B / 61 physical 8-byte slots (47 named members)**), gate `tdrv_arch_ops_initialized @0xc97368` |
| **Arch-ops install** | `tdrv_arch_ops_init` `@0x308e80`; cold assert `ensure_arch_ops_initialized @0x308e50` |
| **Platform-ops tables** | `platform_memhandle_impl @0xbf6c40` (40 B), `platform_rw_impl @0xbf6c80` (32 B) — static `.data`, relocated at load |
| **Platform-ops register** | `ucode_nx_core_create @0x269620` / `ucode_pooling_q7_core_create @0x2697d0` → `ucode_core_create` |
| **Device record** | `mla_t` — stride **590008**; `device@+314664` (`loc_4CD28`), `device_id@+314648`, `mla_idx@+8` |
| **NDS hookup** | `tdrv_init_nds_for_device @0x22f230` → `tdrv_nds_save_process_info @0x22efa0` |
| **HW-platform id** | `tdrv_identify_hw_platform @0x2692f0` — rev-id buckets `≤0xEF HW / ≤0xF9 EMU / ≤0xFF QEMU` |

---

## 1. `tdrv_init` — Master Device Bring-Up

### Purpose

`tdrv_init` is the one function that takes a process from "no devices opened" to "every visible NeuronCore is running its idle program." It is invoked once per process by `kmgr_init` (`@0xde09a`), is guarded against double-init (`"TDRV already initialized!"`), and on any failure path calls `tdrv_destroy` to unwind the partially-built state (`tdrv_destroy` lists `tdrv_init @0x26a963` among its three callers — the error-unwind edge). Its 244 callees are the entire device-bring-up surface of the runtime; this section reproduces the ordered skeleton, not every leaf, and links each sub-phase to the unit that owns it.

### Entry Point

```text
nrt_init (0x94e90)  →  kmgr_init (0xde080)  →  tdrv_init (0x26a310)
│
├─ PLATFORM / ARCH IDENTIFICATION
│    ├─ tdrv_identify_hw_platform (0x2692f0)   ── rev-id → {HW,EMU,QEMU}; caches arch_type
│    └─ tdrv_identify_board_info  (0x268280)   ── ndl_get_board_info → cached_arch_type / al_hal_tpb_set_arch_type
│
├─ ULTRASERVER MODE NEGOTIATION
│    ├─ ndl_pod_status (0xc51b0)               ── query pod/server type, reservation, election state
│    └─ ndl_pod_ctrl   (0xc5110)               ── enter requested UltraServer mode
│
├─ VCORE → PTPB MAP
│    ├─ vtpb_ctx_init (0x313dc0) / vtpb_get_all_virtual_cores (0x3143e0)
│    └─ vtpb_translate_vtpb_to_ptpb_no_state (0x314580)
│
├─ CONTEXT ALLOCATION
│    └─ db_tdrv_ctx_init (0x226ca0)            ── alloc tdrv_ctx (mla_t[]); → tdrv_get_host_device_id_rid_map
│
├─ PER-DEVICE OPEN  (for each attached device)
│    ├─ tdrv_get_bdf_from_device_id (0x269950) ── format "bb:ss.f" PCI BDF
│    ├─ ndl_open_device (0xc30f0)
│    ├─ ndl_reset_ncs (0xc3a50) → ndl_ready_ncs (0xc3a90)
│    ├─ ndl_hbm_scrub_start (0xc4e90) → ndl_hbm_scrub_wait (0xc4f10)   ── ("Skipping … no support in old driver")
│    ├─ dmem_allocator_create (0x2284c0)       ── per-TPB device-DRAM allocator
│    └─ dma_memory_init (0x228470) / dml_init (0x22aa50)
│
├─ PER-NEURONCORE INIT   (phase tags: tpb_eng_sw_init / tdrv_init_one_mla_phase2)
│    ├─ aws_hal_intc_init (0x4501b0) / aws_hal_intc_errtrig_init (0x4502a0)
│    ├─ aws_hal_notific_init (0x450a10)
│    ├─ tpb_eng_init_hals_v2 (0x2687e0)        ── assemble + aws_hal_stpb_init the ~0x450 STPB config
│    ├─ ucode_nx_core_create (0x269620) ×5     ── PE/ACT/POOL/DVE/SP microcode cores  [§3 registers platform-ops]
│    ├─ ucode_pooling_q7_core_create (0x2697d0) ×8   ── Q7 pooling cores              [§3]
│    ├─ ucode_ll_create (0x2269a0)             ── loadable-lib set
│    ├─ notification_init (0x2fed70) / pool_stdio_block_init (0x300c70)
│    ├─ ht_init (0x267d80)                     ── per-TPB model_db hashtable
│    ├─ hw_exec_queue_init (0x3201c0) / sync_point_init (0x303690)
│    ├─ tdrv_scratchpad_init (0x302900)        ── HBM scratchpad pool        [tdrv-scratchpad]
│    ├─ reset_evt_accel_sbuf_addr (0x265640)
│    └─ dma_ring_allocate_seed_set_ring (0x22e9b0) …  ── stochastic-rounding seed DMA dance
│
├─ TIMESTAMP CALIBRATION
│    ├─ tsync_timestamps_start  (0x304610)     ── load per-engine tsync programs    [tdrv-scratchpad §tsync]
│    └─ tsync_timestamps_finish (0x304b90)     ── read back, compute per-engine offsets
│
├─ NDS-FOR-DEVICE HOOKUP   (per device)
│    └─ tdrv_init_nds_for_device (0x22f230)    ── nds_open + tdrv_nds_save_process_info    [§4]
│
└─ READY-EXEC PROGRAM        (per vcore)
     └─ tdrv_start_exec_ready_program (0x26d300)   ── the persistent idle program  [exec/overview]
```

### Algorithm

```c
// Models tdrv_init @0x26a310. Skeleton only — 244 callees compressed to the ordered phases.
// Phase tags ("tpb_eng_sw_init", "tdrv_init_one_mla_phase2", "tdrv_tsync_timestamps") are
// the source-file nlog tags embedded in the binary.
function tdrv_init(vnc_start, count, tpb_init_config_t *cfg, reset_cores, async_mode, …):
    if tdrv_initialized:                                        // global guard
        nlog("TDRV already initialized!");  return NRT_FAILURE
    if count > MAX_CORES:
        nlog("maximum supported number of core is %u - received %u cores"); return NRT_INVALID

    // (1) PLATFORM + ARCH — first touch of the arch-ops lazy singleton (§2)
    if tdrv_identify_hw_platform() == TDRV_PLATFORM_UNKNOWN:    // 0x2692f0
        nlog("Failed to identify hw platform"); return NRT_INVALID
    tdrv_identify_board_info(&arch, &rev)                       // 0x268280 — sets cached_arch_type

    // (2) ULTRASERVER MODE — negotiate pod membership before reserving devices
    ndl_pod_status(&pod)                                        // 0xc51b0
    if requested_mode != 0 && !pod.available:
        nlog("TDRV Unable to initialize, UltraServer mode requested (%d) is not available …"); return …
    nlog("TDRV UltraServer information server_type:%d reservation_id:0x%lx pod_sz:%d election_state:%d node_id:%d")
    ndl_pod_ctrl(requested_mode)                               // 0xc5110

    // (3) VCORE→PTPB MAP + CONTEXT
    vtpb_ctx_init(); vtpb_get_all_virtual_cores(&vcores)        // 0x313dc0 / 0x3143e0
    ctx = db_tdrv_ctx_init(...)                                 // 0x226ca0 — alloc tdrv_ctx (mla_t[])

    // (4) PER-DEVICE: open / reset / scrub / allocator
    for dev in attached_devices:
        mla = &ctx[dev]
        tdrv_get_bdf_from_device_id(mla->device_id, bdf)        // 0x269950
        if ndl_open_device(mla->device) != 0: goto fail         // 0xc30f0
        if reset_cores:
            nlog("Reset triggered for nd %d nc_map 0x%x")
            ndl_reset_ncs(...); ndl_ready_ncs(...)              // 0xc3a50 / 0xc3a90
        if hbm_scrub_supported:
            nlog("Initializing nd%d HBM %d with value %u")
            ndl_hbm_scrub_start(...); ndl_hbm_scrub_wait(...)  // 0xc4e90 / 0xc4f10
        else:
            nlog("Skipping HBM initialization/scrubbing due to no support in old driver")
        dmem_allocator_create(&mla->tpbs[*].allocator, …)       // 0x2284c0 — per-TPB device-DRAM allocator
        dma_memory_init(...); dml_init(...)                     // 0x228470 / 0x22aa50

    // (5) PER-NEURONCORE: HAL + ucode cores + per-NC subsystems
    for nc in cores:                                            // tag "tpb_eng_sw_init"
        aws_hal_intc_init(nc); aws_hal_intc_errtrig_init(nc)    // 0x4501b0 / 0x4502a0
        aws_hal_notific_init(nc)                                // 0x450a10
        if tpb_eng_init_hals_v2(nc) != 0:                       // 0x2687e0 — STPB engine config
            nlog("nd%d nc%d HAL init failed. error:%d"); goto fail
        nlog("nd%d nc%d HAL init success.")
        for eng in {PE,ACT,POOL,DVE,SP}:
            if ucode_nx_core_create(pcore, &core, eng) != 0:    // 0x269620 — REGISTERS platform-ops (§3)
                nlog("Failed to create NX core for eng %u"); goto fail
        for q in [0, 8):
            if ucode_pooling_q7_core_create(pcore, &core, q):   // 0x2697d0 — REGISTERS platform-ops (§3)
                nlog("Failed to create pooling q7 core idx %d"); goto fail
        ucode_ll_create(...)                                    // 0x2269a0 — loadable-lib set
        notification_init(nc); pool_stdio_block_init(nc)        // 0x2fed70 / 0x300c70
        ht_init(&tpb->model_db)                                 // 0x267d80
        hw_exec_queue_init(nc); sync_point_init(&tpb->sync_point) // 0x3201c0 / 0x303690
        tdrv_scratchpad_init(tpb, cfg->scratchpad_page_size)    // 0x302900
        // stochastic-rounding seed: build + run a seed-set DMA ring
        nlog("Configuring stochastic rounding seed for device: %u, nc %u. seed: %u")
        dma_ring_allocate_seed_set_ring(...); dma_ring_set_seed_set_descriptors(...)  // 0x22e9b0 / 0x22e7a0
        ndl_dma_queue_copy_start(...); dma_wait_for_completion_handle(...)            // 0xc3930 / 0x22def0
        xt_cc_init(nc); xt_cc_queue_init(nc)                    // 0x314fb0 / 0x3150d0 — collectives ctx

    // (6) TIMESTAMP CALIBRATION   (tag "tdrv_tsync_timestamps", arch_type==2 only)
    for nc in cores:
        nlog("nd%u nc%u Collecting timestamp adjustment details ...")
        tsync_timestamps_start(nc); tsync_timestamps_finish(nc)   // 0x304610 / 0x304b90

    // (7) NDS-FOR-DEVICE   (§4) — publish this process into each device's datastore
    for dev in attached_devices:
        tdrv_init_nds_for_device(&ctx[dev])                       // 0x22f230

    // (8) READY-EXEC PROGRAM   — launch the persistent idle program per vcore
    for vcore in vcores:
        tdrv_start_exec_ready_program(vcore)                      // 0x26d300
        nlog("Initialized vcore: %u")

    tdrv_initialized = 1;  return NRT_SUCCESS
fail:
    tdrv_destroy()                                                // 0x269a70 — unwind partial state
    return rc
```

> **GOTCHA —** the per-NeuronCore loop (5) and the per-device loop (4) are **separate passes**, and the per-TPB `dmem` allocator must exist before any `ucode_nx_core_create` runs. `ucode_nx_core_create` reads `tpb->tpb_allocator` (the `dmem_allocator_t*`) out of the `tpb_t` and stuffs it into the `nrtucode_userdata_t` it builds for the platform-ops callbacks (§3). A reimplementer who fuses the two loops, or who builds microcode cores before `dmem_allocator_create`, hands the platform-ops a NULL allocator and the first `device_malloc` callback faults.

> **QUIRK —** HBM scrubbing is gated on driver capability, not arch: when the installed driver lacks scrub support `tdrv_init` logs `"Skipping HBM initialization/scrubbing due to no support in old driver"` and proceeds with **un-scrubbed** HBM. Timestamp calibration (`tsync_timestamps_*`, phase 6) is gated the other way — it asserts `arch_type == 2` (SUNDA) and is a no-op shaped path on V3/V4 (see [tdrv-scratchpad](tdrv-scratchpad.md) §tsync). Two different gates, two different conditions; conflating them mis-builds either path.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `tdrv_init` | `0x26a310` | 11922 | Master per-process device bring-up; the only forward init | CERTAIN |
| `tdrv_identify_hw_platform` | `0x2692f0` | 300 | Rev-id → `{HW,EMU,QEMU,UNKNOWN}`; caches arch/rev | CERTAIN |
| `tdrv_identify_board_info` | `0x268280` | — | `ndl_get_board_info` → `cached_arch_type`, sets HAL arch | HIGH |
| `tpb_eng_init_hals_v2` | `0x2687e0` | 2493 | Assemble + `aws_hal_stpb_init` the per-NC STPB config | HIGH |
| `tdrv_start_exec_ready_program` | `0x26d300` | 1943 | Build + launch the persistent idle program per vcore | HIGH |
| `tdrv_get_bdf_from_device_id` | `0x269950` | 103 | Format `"bb:ss.f"` PCI BDF; `"ff:ff.f"` literal on miss | HIGH |
| `kmgr_init` | `0xde080` | — | Caller; drives `tdrv_init` and (on shutdown) `tdrv_destroy` | HIGH |

### Considerations

The bring-up is **not idempotent at the TDRV layer** — the `"TDRV already initialized!"` guard returns `NRT_FAILURE`, unlike `nrt_init`'s idempotent fast-path; the idempotency lives entirely in the `nrt_init` state machine above it ([api-lifecycle](api-lifecycle.md) §3). The `tpb_init_config_t cfg` (DWARF ordinal 9288, 192 B, 51 members) is the flat field-by-field copy `nrt_init` assembles on its stack; TDRV reads `hw_decode_toggle` (per-engine bitmask), `notifications_performance_mode`, `scratchpad_page_size`, and the stochastic-rounding seed from it. The exact per-arch STPB-config assembly inside `tpb_eng_init_hals_v2` (the ~0x450-byte `aws_hal_stpb` struct, per-engine ring PA / desc-count / queue-CSR-offset table, the `hw_decode_toggle` bit map) is owned by [arch-stpb](arch-stpb.md); this page records only that it is one fallible call per NeuronCore and that its failure (`"nd%d nc%d HAL init failed"`) aborts bring-up.

---

## 2. Arch-Ops: the Lazy Per-Arch Vtable

### Purpose

Every piece of device geometry that varies by silicon generation — TPB/TopSP/DMA counts, BAR offsets, DMA-queue CSR offsets, the CSR register/deregister hooks, the sync-event ID tables — is reached through **`tdrv_arch_ops` (`@0xc97180`, 488 B / 61 physical 8-byte slots (47 named members))**. The table is a single process-wide singleton, installed lazily on first use by `tdrv_arch_ops_init` (`@0x308e80`), which dispatches on `al_hal_tpb_get_arch_type()` to one of three populators. This is the runtime's equivalent of an LLVM `TargetSubtargetInfo` vtable: one indirect call per geometry query, resolved once per process at the first `tdrv_arch_get_*` access.

### Entry Point

```text
tdrv_arch_get_num_tpb (0x309050)  [representative — ~78 accessors share this prologue]
  ├─ if !tdrv_arch_ops_initialized && tdrv_arch_ops_init():   ── lazy install + error check
  │      └─ ensure_arch_ops_initialized (0x308e50)            ── __noreturn assert on failure
  └─ return tdrv_arch_ops.get_num_tpb()                       ── indirect call through slot +32

tdrv_arch_ops_init (0x308e80)
  └─ switch al_hal_tpb_get_arch_type():
       case 2: tdrv_arch_register_sunda   (0x30b6a0)          ── populate all 61 physical slots (47 named members) for SUNDA
       case 3: tdrv_arch_register_cayman  (0x30c7d0)          ── CAYMAN
       case 4: tdrv_arch_register_mariana (0x30d900)          ── MARIANA
       default: __assert_fail("0 && \"Unknown architecture\"", tdrv_arch_type.c:0x41)
```

### Algorithm

```c
// tdrv_arch_ops_init @0x308e80 — install the per-arch ops table exactly once.
// Globals: tdrv_arch_ops @0xc97180 (.bss, the 488-B / 61-physical-slot table; 47 named members),
//          tdrv_arch_ops_initialized @0xc97368 (.bss, the one-shot flag).
function tdrv_arch_ops_init():
    if tdrv_arch_ops_initialized:                              // already installed
        return 0
    switch al_hal_tpb_get_arch_type():                        // 2=SUNDA, 3=CAYMAN, 4=MARIANA
        case 3:  tdrv_arch_register_cayman()                  // 0x30c7d0
        case 4:  tdrv_arch_register_mariana()                 // 0x30d900
        case 2:  tdrv_arch_register_sunda()                   // 0x30b6a0
        default: __assert_fail("0 && \"Unknown architecture\"",
                               "…/tdrv/tdrv_arch_type.c", 0x41, "tdrv_arch_ops_init")
    tdrv_arch_ops_initialized = 1
    return 0

// The accessor prologue every tdrv_arch_get_* / tdrv_sync_get_* / tdrv_add_ev_* shares.
function tdrv_arch_get_num_tpb():                             // 0x309050
    if !tdrv_arch_ops_initialized && tdrv_arch_ops_init() != 0:   // install on first touch
        ensure_arch_ops_initialized()                        // 0x308e50 — __noreturn
    return tdrv_arch_ops.get_num_tpb()                       // slot +32, e.g. SUNDA → returns 2

// ensure_arch_ops_initialized @0x308e50 — the cold failure tail (.part.0). It never returns.
function ensure_arch_ops_initialized():                      // __noreturn
    __assert_fail("ret == 0 && \"Failed to initialize arch ops\"",
                  "…/tdrv/tdrv_arch_type.c", 0x51, "ensure_arch_ops_initialized")
```

```c
// tdrv_arch_register_sunda @0x30b6a0 (871 B) — populate all 61 physical slots (47 named members) for SUNDA.
// CAYMAN (0x30c7d0) and MARIANA (0x30d900) are the same shape, differing only in the
// *_sunda → *_cayman / *_mariana leaf-function pointers and the embedded caps/sync tables.
function tdrv_arch_register_sunda():
    tdrv_arch_ops.get_num_tpb              = tdrv_arch_get_num_tpb_sunda        // +32 → const 2
    tdrv_arch_ops.get_default_hbm_index    = tdrv_arch_get_default_hbm_index_sunda // +56
    tdrv_arch_ops.get_tpb_mem_bar_offset   = …_sunda                            // +104
    …                                                                          // 61 physical slots total (47 named members)
    tdrv_arch_ops.instr_block_caps         = &sunda_instr_block_caps            // +248 (table ptr)
    tdrv_arch_ops.tpb_reg_offset           = { …_sunda × 9 }                    // +280 (72-B subtable)
    tdrv_arch_ops.seq_refill               = { …_sunda × 7 }                    // +368 (56-B subtable)
    tdrv_arch_ops.csr_register_device      = tdrv_arch_csr_register_device_sunda   // +424
    tdrv_arch_ops.csr_deregister_device    = tdrv_arch_csr_deregister_device_sunda // +440
    tdrv_arch_ops.sync_events              = &sunda_sync_events                 // +472 (20-B table ptr)
```

The slot layout is fixed across arches (DWARF struct `tdrv_arch_ops`, 488 B / 61 physical 8-byte slots, 47 named members). Three slots are themselves nested sub-tables: `tpb_reg_offset` (72 B / 9 slots, `@+280`), `seq_refill` (56 B / 7 slots, `@+368`), and the data-pointer slots `instr_block_caps` (`@+248`), `sync_events` (`@+472`).

> **CORRECTION —** the slot count is standardized on [tdrv-arch-ops](tdrv-arch-ops.md): **488 B / 61 physical 8-byte slots (47 named members)** (gap `0xc97368-0xc97180 = 0x1e8 = 488 = 61` slots). Earlier "47 slots" phrasing undercounts the physical layout by 14 — 47 is the count of *named* members, not slots.

| Offset | Slot group | Role |
|---|---|---|
| +0…+152 | `get_num_dma_per_tpb` … `get_q7_pool_core_type` | core/engine/HBM geometry counts |
| +104/+112/+120 | `get_tpb_{mem,csr_apb,notific_apb}_bar_offset` | BAR-region offsets used by `tpb_eng_init_hals_v2` |
| +184…+232 | `get_dma_*` family | DMA-engine SDMA/ENS bases, UDMA relbase, H2D engine id |
| +248 | `instr_block_caps` | → per-arch `*_instr_block_caps` (32 B / 12 flags) |
| +280 | `tpb_reg_offset` | 72-B sub-vtable (TPB top/notific/APB/DMA reg offsets) |
| +368 | `seq_refill` | 56-B sub-vtable (sequencer refill-ring create/hw-init, queue head/tail offsets) |
| +424/+432/+440 | `csr_{register,get_regions,deregister}_device` | CSR-region (de)registration — see §5 teardown |
| +448/+456/+464 | `add_ev_{wait,set,clr}` | event-semaphore instruction emitters |
| +472 | `sync_events` | → per-arch `*_sync_events` (20-B sync-event ID table) |
| +480 | `get_inf_init_start` | inference-init start sync-event |

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `tdrv_arch_ops_init` | `0x308e80` | 118 | One-shot `switch(arch)` populator dispatch | CERTAIN |
| `ensure_arch_ops_initialized` | `0x308e50` | 35 | `__noreturn` assert tail on install failure | CERTAIN |
| `tdrv_arch_register_sunda` | `0x30b6a0` | 871 | Fill all 61 physical slots (47 named members) with `*_sunda` leaves | CERTAIN |
| `tdrv_arch_register_cayman` | `0x30c7d0` | 871 | Fill all 61 physical slots (47 named members) with `*_cayman` leaves | CERTAIN |
| `tdrv_arch_register_mariana` | `0x30d900` | 871 | Fill all 61 physical slots (47 named members) with `*_mariana` leaves | CERTAIN |
| `tdrv_arch_get_num_tpb` (and ~77 peers) | `0x309050` | 16 | Lazy-init prologue + indirect slot call | CERTAIN |

> **QUIRK —** the install is not done at static-init or at the top of `tdrv_init` — it is **lazy, on the first geometry query**, and `tdrv_init` itself triggers it (its first `tdrv_arch_get_num_tpb` at the device-open phase forces the populate). The double-check `!tdrv_arch_ops_initialized && tdrv_arch_ops_init()` means a successful install returns `0` and the `&&` short-circuits *before* `ensure_arch_ops_initialized`; the `__noreturn` tail is reached only if `tdrv_arch_ops_init` returns non-zero, which in this build it never does (it either succeeds or `__assert_fail`s on an unknown arch). A reimplementer can fold this into an eager init, but must preserve the "unknown arch → abort" semantics; there is no fourth fallback arch.

> **CORRECTION (TDRV-CORE-08) —** an earlier sweep flagged the nrtucode platform-ops install site as LOW-confidence "not located." It is now byte-anchored: `platform_memhandle_impl @0xbf6c40` and `platform_rw_impl @0xbf6c80` are statically-initialized `.data` tables populated by `R_X86_64_RELATIVE` relocations (slot→callback wiring confirmed in §3), and they are registered indirectly via `ucode_core_create`. Distinct from the *arch-ops* table documented here, which is `.bss` and populated by code, not relocations.

---

## 3. nrtucode Platform-Ops Registration

### Purpose

The Q7/GPSIMD microcode runtime (`nrtucode`, see [gpsimd/extisa-provider](../gpsimd/extisa-provider.md)) must allocate, read, write, and free device DRAM, and must be able to close a device — but it has no knowledge of TDRV's `dmem` allocator or NDL. TDRV bridges this with a **platform-ops vtable**: two small `.data` tables of callbacks (`platform_memhandle_impl`, `platform_rw_impl`) handed to every microcode core at creation, plus a per-core **`nrtucode_userdata_t`** carrying the `dmem_allocator_t*`, the owning `physical_core_t*`, and the SoC address base. An opaque `nrtucode_memhandle_t` is, underneath, a `dmem_t*`.

### Entry Point

```text
tdrv_init (per NeuronCore)
  ├─ ucode_nx_core_create (0x269620)            ── PE/ACT/POOL/DVE/SP cores
  │    ├─ db_physical_core_get_mla_and_tpb       resolve mla + tpb
  │    ├─ malloc(0x18) → nrtucode_userdata_t {tpb->tpb_allocator, pcore, soc_addr_base}
  │    └─ ucode_core_create(&core, tpb->nrtucode_context, core_type, userdata,
  │                         &platform_rw_impl,            ── r8 (32-B rw vtable)
  │                         &platform_memhandle_impl,     ── r9 (40-B memhandle vtable)
  │                         name, iram_base, dram_base, apb_base)
  └─ ucode_pooling_q7_core_create (0x2697d0)     ── 8× Q7 pooling cores; same userdata + both tables

device_close_fn (0x2691a0)                       ── pthread entry; tdrv_destroy launches it (§5)
```

### Algorithm

```c
// The platform-ops vtables — STATIC .data tables, relocated at load (R_X86_64_RELATIVE).
// platform_memhandle_impl @0xbf6c40 (nrtucode_platform_memhandle_t, 40 B):
struct nrtucode_platform_memhandle_t {                        // DWARF: 5 slots
  +0   device_malloc          = platform_memhandle_device_malloc          // 0x2685d0
  +8   device_free            = platform_memhandle_device_free            // 0x269200
  +16  read_memhandle         = platform_memhandle_read_memhandle         // 0x268710
  +24  write_memhandle        = platform_memhandle_write_memhandle        // 0x269270
  +32  get_memhandle_soc_addr = platform_memhandle_get_memhandle_soc_addr // 0x2691d0
};
// platform_rw_impl @0xbf6c80 (nrtucode_platform_rw_t, 32 B):
struct nrtucode_platform_rw_t {                               // DWARF: 4 slots
  +0   read_device       = platform_rw_read_device           // 0x268320
  +8   write_device      = platform_rw_write_device          // 0x268420
  +16  log               = platform_rw_log                   // 0x2681a0
  +24  log_level_enabled = platform_rw_log_level_enabled     // 0x268550
};

// nrtucode_userdata_t (24 B) — built per core; how each callback reaches the dmem allocator.
struct nrtucode_userdata_t {
  +0   tpb_allocator  : dmem_allocator_t*    // copied from tpb->tpb_allocator
  +8   pcore          : const physical_core_t*
  +16  soc_addr_base  : uint64_t             // = aws_get_tpb_addr(pcore)
};
```

```c
// platform_memhandle_device_malloc @0x2685d0 — the alloc callback.
// memhandle is, underneath, a dmem_t*; soc_addr = dmem->_pa + dmem->align_offset.
function platform_memhandle_device_malloc(ctx, size, alignment, out_handle):
    *out_handle = NULL
    if size == 0: return NRTUCODE_SUCCESS
    ud = ucode_context_get_userdata(ctx)                     // → nrtucode_userdata_t*
    snprintf(name, 0x32, "tpb-%d-memhandle_device_malloc", ud->pcore->device_tpb_idx)
    hbm = get_default_hbm_index(ud->pcore->device_tpb_idx)
    rc = dmem_alloc_aligned(ud->tpb_allocator, &dmem, size, TONGA_DRAM, hbm,
                            0, DMA_MEM_USAGE_TYPE_UCODE_LIB, name, alignment)
    if rc:  nlog("[ND %d NC %d] Failed to allocate memhandle device memory"); return NRTUCODE_ERR_ALLOCATION
    nlog("… memhandle malloc: created new memhandle object … at 0x%lx, size %ld",
         dmem->_pa + dmem->align_offset, size)               // soc addr
    *out_handle = (nrtucode_memhandle_t)dmem                 // opaque = dmem_t*
    return NRTUCODE_SUCCESS

// platform_memhandle_read_memhandle @0x268710 — bytes from device DRAM into a host buffer.
function platform_memhandle_read_memhandle(ctx, handle, offset, size, out):
    if !handle: __assert_fail("dmem_handle", "…/tdrv/init.c", 0x31A, …)
    if dmem_buf_copyout((dmem_t*)handle, out, offset, size):
        nlog("Failed to read_memhandle"); return NRTUCODE_ERR_IO
    // soc addr in trace = *(u64*)(handle+24) + *(u64*)(handle+40) = dmem->_pa + dmem->align_offset
    return NRTUCODE_SUCCESS

// platform_memhandle_write_memhandle @0x269270 — mirror; dmem_buf_copyin → NRTUCODE_ERR_IO on fail.
// platform_memhandle_device_free @0x269200 — dmem_free((dmem_t*)handle); logs "Freed memhandle 0x%lx, size %ld".
// platform_memhandle_get_memhandle_soc_addr @0x2691d0 — return dmem->_pa(+24) + dmem->align_offset(+40).

// device_close_fn @0x2691a0 — pthread thread-entry; tdrv_destroy launches one per device (§5).
function device_close_fn(device):                            // void* (*)(void*)
    if device == NULL: return 0
    return (void*)(ndl_close_device((ndl_device_t*)device) != 0)   // ret as truthy void*
```

### Platform-Ops Vtable — Slot Table

| Offset | Slot | Callback | Addr | Role |
|---|---|---|---|---|
| +0 | `device_malloc` | `platform_memhandle_device_malloc` | `0x2685d0` | `dmem_alloc_aligned(TONGA_DRAM, USAGE_UCODE_LIB)`; opaque handle = `dmem_t*` |
| +8 | `device_free` | `platform_memhandle_device_free` | `0x269200` | `dmem_free(handle)`; DEBUG-logs soc-addr + size |
| +16 | `read_memhandle` | `platform_memhandle_read_memhandle` | `0x268710` | `dmem_buf_copyout`; `NRTUCODE_ERR_IO` on fail |
| +24 | `write_memhandle` | `platform_memhandle_write_memhandle` | `0x269270` | `dmem_buf_copyin`; `NRTUCODE_ERR_IO` on fail |
| +32 | `get_memhandle_soc_addr` | `platform_memhandle_get_memhandle_soc_addr` | `0x2691d0` | `dmem->_pa + dmem->align_offset` |

`platform_rw_impl @0xbf6c80` is the parallel 4-slot table: `read_device@+0` (`0x268320`), `write_device@+8` (`0x268420`), `log@+16` (`0x2681a0`), `log_level_enabled@+24` (`0x268550`) — direct device-window read/write plus a logging bridge into `nlog`. All nine pointers across the two tables are confirmed by `R_X86_64_RELATIVE` relocations at `0xbf6c40`–`0xbf6c98`.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `platform_memhandle_device_malloc` | `0x2685d0` | — | alloc cb → `dmem_alloc_aligned` | CERTAIN |
| `platform_memhandle_device_free` | `0x269200` | 104 | free cb → `dmem_free` | CERTAIN |
| `platform_memhandle_read_memhandle` | `0x268710` | 196 | read cb → `dmem_buf_copyout` | CERTAIN |
| `platform_memhandle_write_memhandle` | `0x269270` | 114 | write cb → `dmem_buf_copyin` | CERTAIN |
| `platform_memhandle_get_memhandle_soc_addr` | `0x2691d0` | 46 | soc-addr cb (`_pa + align_offset`) | CERTAIN |
| `device_close_fn` | `0x2691a0` | 35 | pthread close-entry → `ndl_close_device` | CERTAIN |
| `ucode_nx_core_create` | `0x269620` | 426 | build NX core, register both vtables | HIGH |
| `ucode_pooling_q7_core_create` | `0x2697d0` | 377 | build Q7 pooling core, register both vtables | HIGH |

> **NOTE —** the `nrtucode_memhandle_t` is opaque to the microcode runtime but is a plain `dmem_t*` underneath, so every callback reads `dmem_t` fields directly: `size@+16`, `_pa@+24`, `align_offset@+40`. The trace soc-addr `*(u64*)(handle+24) + *(u64*)(handle+40)` is exactly `dmem->_pa + dmem->align_offset`. The `dmem_t` layout (192 B) is owned by [tdrv-dmem](tdrv-dmem.md); this layer only consumes those three offsets.

> **QUIRK —** `device_close_fn` returns its `ndl_close_device` non-zero status **cast to a `void*`** (`(void*)(rc != 0)`), so the `pthread_join` retrieves `(void*)1` on close failure and `(void*)0` on success. A reimplementer must read the join return as a boolean error flag, not a status code — the actual `NRT_STATUS` is lost across the thread boundary (§5).

---

## 4. NDS-for-Device Hookup and `save_process_info`

### Purpose

Once a device is open, TDRV publishes the current process into that device's **NDS** (Neuron DataStore — the per-device shared-memory counter/object store consumed by `neuron-monitor` and sysfs; see [datastore/overview](../datastore/overview.md)). `tdrv_init_nds_for_device` opens an NDS handle keyed on the device, stores it in the process-global `nds_instances[]` array indexed by `mla_idx`, and calls `tdrv_nds_save_process_info` to stamp the process tag, runtime version, framework, and FAL versions into two NDS objects. `tdrv_close_nds_for_device` (called from `tdrv_destroy`) is the inverse.

### Entry Point

```text
tdrv_init (per device)
  └─ tdrv_init_nds_for_device (0x22f230)
       ├─ nds_open(mla->device, 0, &nds_instances[mla->mla_idx])    ── handle keyed on device
       └─ tdrv_nds_save_process_info (0x22efa0)
            ├─ nds_obj_new(nds, 1) → process_info     ; nds_obj_new(nds, 2) → process_info_ext
            ├─ tag = getenv("NEURON_PROCESS_TAG") | "%d"%getpid()
            ├─ tdrv_parse_version_number("2.31.24.0", …) → nds_set_nd_counter(#0 runtime version)
            ├─ framework type/version → nds_set_nd_counter(#1) ; FAL version → counter(#2)
            └─ nds_obj_commit / nds_obj_delete (handles)

tdrv_destroy (per device)
  └─ tdrv_close_nds_for_device (0x22f2c0) → nds_close(nds_instances[mla_idx]); slot = NULL
```

### Algorithm

```c
// tdrv_init_nds_for_device @0x22f230 — open NDS + publish process. Arg is the mla_t*.
// nds_instances @0xc96c00 (.bss array of nds_instance_t*, keyed by mla_idx).
function tdrv_init_nds_for_device(mla):
    // mla->device  @+314664 (IDA loc_4CD28);  mla->device_id @+314648 (loc_4CD18); mla->mla_idx @+8
    if nds_open(mla->device, 0, &nds_instances[mla->mla_idx]) != 0:
        nlog("unable to open nds for device_id %d", mla->device_id);  return NRT_FAILURE
    if tdrv_nds_save_process_info(mla) != 0:
        nlog("unable to save process info %d", rc);  return NRT_FAILURE
    return NRT_SUCCESS

// tdrv_nds_save_process_info @0x22efa0 — stamp this process into the device NDS.
function tdrv_nds_save_process_info(mla):
    nds = nds_instances[mla->mla_idx]
    h1 = nds_obj_new(nds, 1)                                 // type 1: nds_process_info_t (60 B)
    h2 = nds_obj_new(nds, 2)                                 // type 2: nds_process_info_ext_t (256 B)
    pi  = nds_obj_handle_to_process_info(h1)
    pix = nds_obj_handle_to_process_info_ext(h2)
    tag = getenv("NEURON_PROCESS_TAG")
    if tag:  strncpy(pi->tag, tag, 31); pi->tag[31]=0; strncpy(pix->tag, tag, 255); pix->tag[255]=0
    else:    snprintf(pi->tag, 32, "%d", getpid()); snprintf(pix->tag, 256, "%d", getpid())
    tdrv_parse_version_number("2.31.24.0", &ver, 0)          // runtime version → packed nmetric_version_t
    pi->runtime_version = { ROL2(ver.major_minor, 8), ver.build }
    nds_set_nd_counter(nds, 0, &ver)                         // ND counter #0 = runtime version
    if (ft = tdrv_get_framework_type()) != 0:
        … pack framework type/version → nds_set_nd_counter(nds, 1, …)   // #1 = framework
        … pack FAL version            → nds_set_nd_counter(nds, 2, …)   // #2 = FAL
    nds_obj_commit(h1); nds_obj_commit(h2); nds_obj_delete(h1); nds_obj_delete(h2)
    return NRT_SUCCESS

// tdrv_close_nds_for_device @0x22f2c0 — inverse, from tdrv_destroy.
function tdrv_close_nds_for_device(mla):
    rc = nds_close(nds_instances[mla->mla_idx])
    nds_instances[mla->mla_idx] = NULL                       // clear slot
    if rc:  nlog("Failed to close NDS for device_id %d", mla->device_id); return NRT_FAILURE
    return NRT_SUCCESS
```

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `tdrv_init_nds_for_device` | `0x22f230` | 142 | `nds_open` + `save_process_info`; keyed by `mla_idx` | CERTAIN |
| `tdrv_nds_save_process_info` | `0x22efa0` | 655 | stamp tag + runtime/framework/FAL version into NDS | CERTAIN |
| `tdrv_close_nds_for_device` | `0x22f2c0` | 130 | `nds_close`; clear `nds_instances[mla_idx]` | CERTAIN |
| `tdrv_parse_version_number` | `0x22ef20` | — | parse `"a.b.c"` → packed `nmetric_version_t` | HIGH |

> **NOTE —** the NDS counter assignments are fixed and shared with the accessor cell: **#0** = runtime version, **#1** = framework, **#2** = FAL version, **#3** = device feature bitmap, **#11** = sysfs-metric bitmap (the last two are read/set by `tdrv_get/set_feature_bitmap` and `tdrv_get/set_sysfs_metric_bitmap`, documented in [datastore/userspace-libnds](../datastore/userspace-libnds.md)). The runtime version string is the compiled-in `"2.31.24.0"`, consistent with this build's package suffix; the high word of the packed `nmetric_version_t` is byte-swapped via `ROL2(…, 8)` before the counter write.

---

## 5. `tdrv_destroy` — Teardown in Reverse

### Purpose

`tdrv_destroy` (`@0x269a70`) unwinds everything `tdrv_init` built, in roughly reverse order. It is reached three ways: the normal `kmgr_destroy` shutdown path, the `kmgr_init` failure path, and `tdrv_init`'s own error-unwind (so it must tolerate a *partially* built ctx). The teardown halts each NeuronCore's running idle program, destroys the microcode cores and loadable libraries, tears down per-NC subsystems, frees the per-TPB `dmem` allocator, then closes all devices **in parallel** with one `pthread` per device, and finally frees the context and deregisters CSR regions.

### Entry Point

```text
(kmgr_destroy | kmgr_init-fail | tdrv_init-unwind)  →  tdrv_destroy (0x269a70)
├─ db_tdrv_ctx_get (0x226fb0)                       ── fetch ctx, else "TDRV not initialized"
├─ PER ACTIVE MLA (stride 590008):
│    ├─ hw_exec_queue_add_halt_request (0x321690)
│    ├─ ndl_nc_semaphore_increment(mla->device, tpb->idx, tdrv_sync_get_inference_start, 2)  ── 0xc3ba0
│    ├─ consume_ready_exec_notification_v2 (0x2fcce0)   ── drain INIT then HALT notification
│    ├─ ucode_core_print_logs (0x226770) / ucode_core_destroy (0x2267d0) ×5      ── NX cores
│    ├─ ucode_ll_destroy (0x226a70) / ucode_core_destroy ×8 (Q7) / ucode_free_lib_set (0x311060)
│    ├─ notification_destroy (0x2ff030) / pool_stdio_block_destroy (0x300f50)
│    ├─ ht_destroy (0x268170)                        ── model_db
│    ├─ dma_queue_free_h2d_queue (0x22ced0) …        ── instr-fetch DMA rings / h2d queues
│    ├─ tdrv_scratchpad_destroy (0x302ac0)
│    ├─ dmem_allocator_destroy (0x228550)            ── per-TPB device-DRAM allocator
│    ├─ aws_hal_intc_destroy (0x450230)
│    └─ tdrv_close_nds_for_device (0x22f2c0)         ── §4 inverse
├─ PARALLEL DEVICE CLOSE:
│    ├─ pthread_create(device_close_fn, mla->device)  ── one thread per device (0x3c750)
│    └─ pthread_join(...) → close_ret  ("close device %u failed" if truthy)   ── (0x3c600)
├─ tdrv_free_instance_info_cache (0x3087c0)
├─ vtpb_ctx_destroy (0x313f60)
├─ free(ctx) ; db_tdrv_ctx_clear (0x226fa0)
└─ csr_deregister (0x315550)                         ── unwind csr_register_device (arch-ops slot +440)
```

### Algorithm

```c
// tdrv_destroy @0x269a70 — reverse of tdrv_init. Tolerant of a partially-built ctx.
function tdrv_destroy():
    ctx = db_tdrv_ctx_get()                                  // 0x226fb0
    if !ctx:  nlog("TDRV not initialized");  return
    for mla in ctx.active_mlas:                              // stride 590008
        tpb = db_get_tpb_from_mla(mla)
        // (1) halt the running idle program: arm halt, kick inference-start sema, drain notification
        hw_exec_queue_add_halt_request(...)                 // 0x321690
        ndl_nc_semaphore_increment(mla->device, tpb->idx,   // loc_4CD30 device handle (see GOTCHA)
                                   tdrv_sync_get_inference_start(), 2)   // 0xc3ba0 / 0x30a5c0
        consume_ready_exec_notification_v2(...)             // 0x2fcce0 — INIT then HALT
        // (2) destroy microcode cores + loadable libs
        for eng in {PE,ACT,POOL,DVE,SP}:
            ucode_core_print_logs(...)                      // 0x226770
            if ucode_core_destroy(...) : nlog("Failed to destroy core %d eng %u")   // 0x2267d0
        ucode_ll_destroy(...)                                // 0x226a70
        for q in [0, 8): ucode_core_destroy(...)            // Q7 pooling cores
        ucode_free_lib_set(...)                              // 0x311060
        // (3) per-NC subsystems
        notification_destroy(...); pool_stdio_block_destroy(...)  // 0x2ff030 / 0x300f50
        ht_destroy(&tpb->model_db)                          // 0x268170
        for q in h2d_queues: dma_queue_free_h2d_queue(q)    // 0x22ced0
        tdrv_scratchpad_destroy(tpb)                        // 0x302ac0
        dmem_allocator_destroy(tpb->tpb_allocator)          // 0x228550
        aws_hal_intc_destroy(...)                           // 0x450230
        tdrv_close_nds_for_device(mla)                      // 0x22f2c0  (§4)
    // (4) close all devices in parallel
    for mla in ctx.active_mlas:
        pthread_create(&th[i], NULL, device_close_fn, mla->device)   // loc_4CD30; (§3)
    for i: pthread_join(th[i], &close_ret)
            if close_ret: nlog("close device %u failed")
    // (5) free context + deregister CSR
    tdrv_free_instance_info_cache()                         // 0x3087c0
    vtpb_ctx_destroy()                                      // 0x313f60
    free(ctx); db_tdrv_ctx_clear()                          // 0x226fa0
    csr_deregister()                                        // 0x315550 — arch-ops csr_deregister_device slot
```

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `tdrv_destroy` | `0x269a70` | 2204 | Full teardown; reverse-order; partial-ctx tolerant | CERTAIN |
| `tdrv_close_nds_for_device` | `0x22f2c0` | 130 | `nds_close`; clear `nds_instances[mla_idx]` | CERTAIN |
| `device_close_fn` | `0x2691a0` | 35 | per-device pthread close entry → `ndl_close_device` | CERTAIN |
| `csr_deregister` | `0x315550` | 13 | thunk → `tdrv_arch_ops.csr_deregister_device` (arch slot +440) | HIGH |
| `tdrv_free_instance_info_cache` | `0x3087c0` | — | free the process-wide instance-info singleton | MEDIUM |

> **GOTCHA —** the device handle passed to both `ndl_nc_semaphore_increment` and `device_close_fn` decompiles as `*(void**)(mla + 0x4CD30)` (`+314672`), which is the base of `mla->top_sps`, **not** the canonical `mla->device` at `+314664` (`loc_4CD28`). This is an IDA struct-overlap artifact: the device pointer the runtime actually closes is `mla->device`. A reimplementer should pass `mla->device`, not `&mla->top_sps` — the two offsets are 8 bytes apart and the alias is a decompiler quirk, not a real field at `+314672`. (Flagged MEDIUM pending byte-level confirmation in [the deep-dive backlog](../appendix/deep-dive-backlog.md).)

> **QUIRK —** devices are closed **in parallel** — `tdrv_destroy` spawns one `pthread` per device running `device_close_fn`, then joins them all. On a multi-device UltraServer this overlaps the (slow, IOCTL-bound) `ndl_close_device` calls instead of serializing them. The halt/drain of each NeuronCore's idle program in phase (1), by contrast, is **sequential per MLA** and must complete before any device close, or a close races a still-running core.

---

## Cross-References

- [Public C API: Lifecycle and Init/Teardown](api-lifecycle.md) — `nrt_init`/`nrt_close`, the public surface above TDRV; `nrt_init` → `kmgr_init` → `tdrv_init`, and the `tdrv_override_hw_revision` / framework-(de)registration callees
- [TDRV: Tensor Object Layer](tdrv-tensor.md) — the `nrt_tensor_t` built atop the `dmem` allocations TDRV bring-up creates; beside this page in Part IV
- [TDRV: Device-Memory Allocator (dmem)](tdrv-dmem.md) — the `dmem_t` (192 B) and `dmem_allocator_t` behind every platform-ops callback and the per-TPB allocator `tdrv_init`/`tdrv_destroy` create/free
- [TDRV: DMA Descriptor Rings and Swap-Queue](tdrv-dma-rings.md) — the instr-fetch DMA rings and h2d queues `tdrv_init` allocates and `tdrv_destroy` frees; the seed-set ring dance
- [TDRV: HBM Scratchpad and Sync-Point](tdrv-scratchpad.md) — `tdrv_scratchpad_init`/`sync_point_init` and the `tsync_timestamps_*` calibration phase `tdrv_init` runs
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](tdrv-arch-ops.md) — the full 47-slot `tdrv_arch_ops` table and the `tdrv_arch_get_*` / `tdrv_sync_get_*` / `tdrv_add_ev_*` accessor family installed in §2
- [NDL: Neuron Driver Layer (IOCTL/mmap Wrappers)](ndl.md) — `ndl_open_device`/`ndl_close_device`/`ndl_reset_ncs`/`ndl_hbm_scrub_*`/`ndl_pod_*` the bring-up drives
- [The ext-ISA Provider (nrtucode_* API)](../gpsimd/extisa-provider.md) — the microcode runtime that consumes the platform-ops vtable registered in §3
- [Overview: the Shared-Memory Counter Plane](../datastore/overview.md) — the NDS that §4 publishes process info into; `nds_instances[]` and the ND-counter slots
