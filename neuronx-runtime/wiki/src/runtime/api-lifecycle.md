# Public C API: Lifecycle and Init/Teardown

> *All addresses, offsets, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped and retains DWARF — function names, struct field names, and enum constants survive. `.text` VMA equals file offset (`0x3dbc0`); `.rodata` and `.data` are also VMA==fileoffset in this build. All addresses are analysis VMAs. Other versions will differ.*

## Abstract

This page documents the **bring-up and teardown spine** of the Neuron Runtime: `nrt_init` (`@0x94e90`) — the one master entry that negotiates driver compatibility, parses the environment, discovers devices, allocates Logical Neuron Cores, and starts every subsystem — and `nrt_close` (`@0x93c20`), which unwinds that bring-up in the reverse order. Around them sit a small cluster of helpers every reimplementer needs: the **4-state lifecycle guard** (`NRT_INIT_STATE`) that every public API entry consults before doing work, the **version/info reporters** (`nrt_get_version`, `nrt_infodump`), and the **error-time core-dump forwarder** (`nrt_core_dump`) that templates a per-process path and `fork`/`exec`s the external `neuron-dump` helper.

The familiar reference frame is a classic guarded-singleton runtime: think of a CUDA-context or an OpenMP-runtime `init()` that may be called more than once, must reject use after a `fork()`, and must fail loudly on a kernel-driver ABI mismatch. `nrt_init` is exactly that shape. It is **idempotent** — a second call while already `INIT` is a logged no-op returning `NRT_SUCCESS` — and it is **gated on a global `NRT_INIT_STATE`** kept in a single `.bss` word (`nrt_init_state @0xc5d1a0`). The same word, set to `NRT_STATE_CHILD` by the registered `pthread_atfork` child handler, is how every API entry refuses to run in a forked child that never re-initialized. The driver-compat negotiation is a compatibility-**id** range check (not a SemVer compare): the runtime carries a per-arch compat id and demands that the installed `aws-neuronx-dkms` advertise a `[min, max]` id range that contains it, printing a precise upgrade message via `nrt_display_compat_msg` (`@0x93160`) on mismatch.

The page is organized as the recurring H3 vocabulary applied to five units: **(1)** the `nrt_init` bring-up sequence, with its full ordered call-tree; **(2)** `nrt_close` teardown; **(3)** the `NRT_INIT_STATE` guard machine shared by every entry point; **(4)** the version/info reporters; and **(5)** the core-dump path templater and `fork`/`exec` forwarder. The config-struct field offsets (`nrt_config_0`, `nrt_global_config_t`) and the env-var parsing that fills them are owned by sibling pages; this page treats those structs as inputs and cites only the fields the lifecycle code reads.

For reimplementation, the contract is:

- **The 4-state machine**: `NRT_INIT_STATE = {START=0, INIT=1, CHILD=2, CLOSED=3}` in a single global word; the legal transitions; and the guard predicate every public entry runs *before* its work.
- **The `nrt_init` order**: soft-incompat callback registration → state guard → device discovery → env parse → HW-revision override → config copy into `tpb_init_config_t` → driver-compat-id negotiation → LNC allocation (`ndl_crwl_nc_range_mark` bitmap) → the fixed subsystem init order → atfork handlers → exec-ready start → `nrt_state_set(INIT)`.
- **The `nrt_close` order**: the reverse — `enc_*` async/inspect/profile close → `kmgr_*` shutdown → per-vcore comm/lock free + NC-range unmark → `tdrv_deregister_framework` → `ucode_teardown_module` → `nrt_config_free` → `nlog_close` → `state=START`.
- **The driver-compat-id model**: runtime carries `(vtpb, v25)` compat ids per arch; reject when `vtpb < min` or `max < v25`.
- **The core-dump path grammar**: `%p/%t/%l/%c/%d/%i` → `nrt_path_variables_t` fields, expanded by a bounded templater, then `fork`/`execv` of `/opt/aws/neuron/bin/neuron-dump`.

| | |
|---|---|
| **Master init** | `nrt_init` `@0x94e90` (6503 B, 1399 insns, 808-byte frame, 209 BBs) |
| **Teardown** | `nrt_close` `@0x93c20` (1035 B) — export `@@NRT_2.0.0` |
| **State global** | `nrt_init_state` `@0xc5d1a0` (.bss, `NRT_INIT_STATE` enum) |
| **State strings** | `nrt_init_state_strings` `@0xc08900` → `{"NRT_STATE_START","NRT_STATE_INIT","NRT_STATE_CHILD","NRT_STATE_CLOSED"}` |
| **State setter / probe / namer** | `nrt_state_set` `@0xb9090` · `nrt_state_is_init` `@0xb9080` · `nrt_state_get_string` `@0xb9060` |
| **Version reporter** | `nrt_get_version` `@0x940b0` (export `@@NRT_2.0.0`; PLT thunk `@0x3c400`) |
| **Version-string parser** | `set_version` `@0x938f0` (digit-validate → `strtoll`) |
| **Info / support bundle** | `nrt_infodump` `@0x94030` (one-shot guard) → `nrt_infodump_0` `@0x93220` |
| **Compat-mismatch printer** | `nrt_display_compat_msg` `@0x93160` · soft-incompat WARN `nrt_soft_incompat_callback` `@0x93120` |
| **atfork child mark** | `atfork_child` `@0x93110` → `nrt_state_set(NRT_STATE_CHILD)` |
| **Core-dump forwarder** | `nrt_core_dump` `@0x92b90` · path templater `nrt_core_dump_format_string` `@0x924f0` |
| **External helper** | `/opt/aws/neuron/bin/neuron-dump` (`fork`/`execv`, gated on `access(…, X_OK)`) |

> **NOTE —** `nrt_init` is the single densest function in the runtime's public surface (1399 instructions). The decompile at `@0x94e90` confirms the entire ordered sequence reproduced in §1; every `nlog_write` site below is quoted verbatim from that body, and every callee address was resolved against `nrt_init_0x94e90.json` (108 call edges).

---

## 1. `nrt_init` — Master Bring-Up

### Purpose

`nrt_init(framework_type, framework_version, fal_version)` brings the entire runtime from `NRT_STATE_START` to `NRT_STATE_INIT`. It is the only function that may transition the state forward to `INIT`, and it is the one place driver compatibility, device discovery, LNC allocation, and the per-subsystem init order are decided. It is idempotent on re-entry while already `INIT` (a fast-path no-op), and it carries a stack-local `NlogErrorContextManager` (the `func-name` log tag) whose RAII scope-exit emits the trailing `"Generic API Failure"` if any coalesced log went unflushed.

### Entry Point

```text
nrt_init (0x94e90)                                    framework_type a1, fw_ver a2, fal_ver a3
│
├─ [state==INIT] log "API:IN/OUT", return NRT_SUCCESS   ── idempotent fast path (lines 128-143)
│
├─ nlog_coalescing_init_thread()                        ── per-thread log coalescing
├─ ndl_register_soft_incompat_callback(nrt_soft_incompat_callback)   ── fail → ret NRT_INVALID(2)
├─ STATE GUARD                                          ── CLOSED→14 ; non-START→NRT_FAILURE(1)
├─ nrt_get_dev_info(&nd_count,&nc_per_device,&arch,available_devices[32])   (0x83ad0)
│      └─ err | arch==INVALID → "Cannot find Neuron devices…" + 2 URLs → ret 2
├─ nrt_config_parse_init_config()                       (0x8a1d0)  env NEURON_RT_* → nrt_config_0
├─ HW-revision override (platform / arch==4 / v4_plus)  ── tdrv_override_hw_revision(REV_A|REV_B)
├─ copy ~50 nrt_config_0 fields → stack tpb_init_config_t v129  (rbp-0xF0)
├─ banner: "Neuron Runtime 2.31.24.0 built on Mar 25 2026"
│
├─ DRIVER-COMPAT NEGOTIATION  (skipped if gconf.funtime → jumps to LABEL_55)
│      ├─ ndl_get_version(&version)          → "Found neuron driver: %d.%d"
│      │      └─ driver_major<=1 → "Neuron Runtime 2.x requires … driver version 2.1 or above." → ret 2
│      ├─ compat ids: default (vtpb=10, v25=11) ; SUNDA → (6, 6)
│      └─ ndl_get_compatible_version(&min,&max)
│             ├─ vtpb<min → nrt_display_compat_msg(vtpb,min,max,"…runtime-lib and …collectives") → ret 2
│             └─ max<v25  → nrt_display_compat_msg(v25 ,min,max,"aws-neuronx-dkms")            → ret 2
│
├─ LABEL_55: CORE ALLOCATION
│      ├─ nrt_get_dev_info(again) → "Instance info:: device count:%d cores per device:%d architecture:TRN"
│      ├─ [NEURON_RT_VISIBLE_CORES empty]   → compute count, vtpb_*_no_state translate,
│      │       ndl_crwl_nc_range_mark(bitmap,0x20) → tzcnt start-core → build visible_virtual_cores
│      └─ [explicit]  → "Using NEURON_RT_VISIBLE_CORES…", std::set dedupe, contiguity check,
│              translate first/last lnc→pnc, ndl_crwl_nc_range_mark | ndl_dump_nc_pid_info on conflict
│
└─ LABEL_105: SUBSYSTEM INIT  (fixed order)
       ├─ nrt_vnc_usage_init(num_visible_vnc)
       ├─ nlog_init(num_visible_vnc, gconf.log_coalescing_enabled)
       ├─ ucode_init_module(nrt_config_0.neuron_ucode_path)
       ├─ encd_libncfw_init()
       ├─ [hw_decode_toggle] hw_decode_init_tables(nrt_config_0.hw_decode_table_path)
       ├─ enc_init()
       ├─ kmgr_register_framework(framework_type, fw_ver, fal_ver)            (0xddff0)
       ├─ [!funtime] kmgr_init(vnc_start, count, &v129, reset_cores,
       │                       implicit_async_mode, async_max_inflight, input_dump_dir)  (0xde080)
       │                  └─ err → "Failed to initialize devices, error:%u"
       ├─ pthread_atfork(atfork_prepare, atfork_parent, atfork_child)  → err "Failed to set atfork handlers"
       ├─ nrt_profile_session_init()
       ├─ nrt_inspect_config_allocate(&options) → nrt_config_parse_inspect_config(options)
       │        └─ [enable_inspect | enable_inspect_on_fail] nrt_inspect_begin_with_options()
       ├─ [funtime] return SUCCESS here (skip exec-ready)
       └─ nrt_start_exec_ready_program()
              └─ done: log "API:OUT: (ret=%u)" ; on err nrt_infodump(ERROR,…,"nrt_init()")
                       on success nrt_state_set(NRT_STATE_INIT) + nrt_infodump(INFO,…)
```

### Algorithm

```c
// Models nrt_init @0x94e90. Verified line-by-line against the decompile.
// State word: nrt_init_state @0xc5d1a0 (NRT_INIT_STATE). Returns NRT_STATUS.
function nrt_init(framework_type, fw_ver, fal_version):      // a1, a2, a3
    // (0) IDEMPOTENT FAST PATH — lines 128-143
    if nrt_init_state == NRT_STATE_INIT:                     // already up
        log_dbg("API:IN: (cc_root_comm_id=%s, num_cores=%u, reset_cores=%u)", ...)
        log_dbg("API:OUT: (ret=%u)", 0)
        return NRT_SUCCESS                                   // 0

    NlogErrorContextManager ctx("init")                     // func-name log tag; RAII flush at exit
    nlog_coalescing_init_thread()

    // (1) register the soft-incompat WARN callback into NDL
    if ndl_register_soft_incompat_callback(nrt_soft_incompat_callback) != 0:  // 0x93120
        log_err("Failed to register nrt_soft_incompat_callback")
        return NRT_INVALID                                  // 2

    // (2) STATE GUARD — only NRT_STATE_START may proceed (lines 234-248)
    if nrt_init_state != NRT_STATE_START:
        if nrt_init_state == NRT_STATE_CLOSED:
            log_warn("NRT already closed");  return 14       // NRT_CLOSED
        log_err("Incompatible runtime state: %s", nrt_state_get_string())  // e.g. NRT_STATE_CHILD
        return NRT_FAILURE                                   // 1

    // (3) DEVICE DISCOVERY (0x83ad0)
    arch = AL_HAL_TPB_ARCH_TYPE_INVALID
    rc = nrt_get_dev_info(&nd_count, &nc_per_device, &arch, available_devices[32])
    if rc != 0 || arch == INVALID:
        log_err("Cannot find Neuron devices. … inf2 or trn1.")  // + 2 troubleshoot URLs
        return NRT_INVALID                                  // 2  (line 909, LABEL_23)

    // (4) ENV PARSE → nrt_config_0 (0x8a1d0)  [owned by api-device-config.md]
    rc = nrt_config_parse_init_config();  if rc: return rc

    // (5) HW-REVISION OVERRIDE
    if tdrv_get_platform_type() != TDRV_PLATFORM_HW:
        tdrv_override_hw_revision(AL_HAL_TPB_REVISION_A)
    if al_hal_tpb_get_arch_type() == 4 && (gconf.v4_plus || !on_real_hw):
        tdrv_override_hw_revision(gconf.v4_plus ? REVISION_B : REVISION_A)

    // (6) COPY ~50 nrt_config_0 debug/cc/dma/hbm fields → stack tpb_init_config_t v129
    //     incl. dbg_cc_*, dma_packetization_size, hw_decode_toggle, scrub_hbm,
    //     virtual_core_size, map_hbm; dbg_cc_dma_packet_size vector clamped to 16:
    //       if items>16: log_warn("NEURON_RT_DBG_CC_DMA_PACKET_SIZE has more items…ignoring extra")
    enc_config.enable_no_compile_mode = nrt_config_0.enable_no_compile_mode
    enc_config.timeout_seconds = enable_no_compile_mode ? nrt_config_0.no_compile_mode_timeout : 0
    enable_fail_on_nan = nrt_config_0.fail_on_nan
    log_info("Neuron Runtime %s built on %s", "2.31.24.0", "Mar 25 2026")

    // (7) DRIVER-COMPAT-ID NEGOTIATION — skipped entirely if gconf.funtime
    if !gconf.funtime:
        if ndl_get_version(&ver) != 0:
            log_err("Unable to determine Neuron Driver version. …"); return NRT_INVALID
        log_info("Found neuron driver: %d.%d", ver.driver_major, ver.driver_minor)
        if ver.driver_major <= 1:
            log_err("Neuron Runtime 2.x requires Neuron driver(aws-neuron-dkms) version 2.1 or above.")
            return NRT_INVALID                              // 2
        (vtpb, v25) = (arch == SUNDA) ? (6, 6) : (10, 11)   // per-arch runtime compat ids
        if ndl_get_compatible_version(&min, &max) != 0:
            log_err("Unable to read compatible Driver version. …"); return NRT_INVALID
        if vtpb < min:
            nrt_display_compat_msg(vtpb, min, max, "aws-neuronx-runtime-lib and aws-neuronx-collectives")
            return NRT_INVALID                              // 2
        if max < v25:
            nrt_display_compat_msg(v25, min, max, "aws-neuronx-dkms")
            return NRT_INVALID                              // 2

    // (8) LABEL_55 — LOGICAL-NEURON-CORE ALLOCATION
    nrt_get_dev_info(&v116,&v117,&v115,v128)                 // re-read (TRN banner)
    log_info("Instance info:: device count:%d cores per device:%d architecture:TRN", v116, v117)
    if nrt_config_0.visible_virtual_cores is empty:          // implicit allocation
        if nrt_config_0.num_cores == 0:
            vtpb_ptpb_num_cores_to_vtpb_num_cores_no_state(total_pnc, virtual_core_size, &num_cores)
        vtpb_vtpb_num_cores_to_ptpb_num_cores_no_state(num_cores, virtual_core_size, &ptpb_core_count)
        if arch > SUNDA && virtual_core_size != ptpb_core_count
                       && ptpb_core_count % tdrv_arch_get_num_tpb() != 0:
            log_err("`NEURON_RT_NUM_CORES` must request one core, or the whole device (multiple of %u)")
            return NRT_INVALID
        if gconf.funtime: set bitmap bits directly           // sim: no driver reservation
        else if ndl_crwl_nc_range_mark(ptpb_core_count, base, base+span-1, &avail, bitmap, 0x20):
            log_err("Logical Neuron Core(s) not available - Requested:%d Available:%d Logical Core size %u")
            return NRT_FAILURE
        start_pnc = tzcnt(bitmap)                            // first reserved physical core
        if start_pnc % virtual_core_size != 0:
            log_err("Cannot start process with LNC Size of %u. … NEURON_RT_VISIBLE_CORES=%u-%u")
            return NRT_INVALID
        vtpb_translate_ptpb_to_vtpb_no_state(start_pnc, virtual_core_size, ptpbs)
        for i in [0, num_cores): visible_virtual_cores.push_back(ptpbs[0] + i)
    else:                                                    // explicit NEURON_RT_VISIBLE_CORES
        if num_cores: log_warn("Using NEURON_RT_VISIBLE_CORES to allocate Logical Neuron Cores…")
        set<int> s(visible_virtual_cores)                    // dedupe + sort
        // contiguity check: adjacent entries must differ by 1
        for k in [1, n): if cores[k] - cores[k-1] != 1:
            log_err("Logical Neuron cores are not contiguous"); return NRT_INVALID
        vtpb_translate_vtpb_to_ptpb_no_state(first_lnc, vsize, ptpbs); start_pnc = ptpbs[0]
        vtpb_translate_vtpb_to_ptpb_no_state(last_lnc , vsize, ptpbs); end_pnc   = ptpbs[vsize-1]
        vtpb_vtpb_num_cores_to_ptpb_num_cores_no_state(n, vsize, &ptpb_core_count)
        if !gconf.funtime && ndl_crwl_nc_range_mark(ptpb_core_count, start_pnc, end_pnc, &avail, bitmap, 0x20):
            log_err("Logical Neuron Core(s) not available - Requested:lnc%d-lnc%d …")
            ndl_dump_nc_pid_info(start_pnc, 1, 0x80); return NRT_FAILURE
        log_info("LNC info: first lnc %d last lnc %d first pnc %d last pnc %d", …)

    // (9) LABEL_105 — SUBSYSTEM INIT, in declared order
    nrt_vnc_usage_init(num_visible_vnc)
    nlog_init(num_visible_vnc, gconf.log_coalescing_enabled)
    if (rc = ucode_init_module(nrt_config_0.neuron_ucode_path)) != 0: goto done
    if (rc = encd_libncfw_init()) != 0: goto done
    if nrt_config_0.hw_decode_toggle && (rc = hw_decode_init_tables(...)) != 0: goto done
    if (rc = enc_init()) != 0: goto done
    if (rc = kmgr_register_framework(framework_type, fw_ver, fal_version)) != 0: goto done   // 0xddff0
    if !gconf.funtime:
        rc = kmgr_init(vnc_start, count, &v129, reset_cores,
                       gconf.implicit_async_mode, gconf.async_exec_max_inflight_requests,
                       nrt_config_0.input_dump_directory)                                     // 0xde080
        if rc: log_err("Failed to initialize devices, error:%u", rc); goto done
    if pthread_atfork(atfork_prepare, atfork_parent, atfork_child) != 0:
        log_err("Failed to set atfork handlers, ret=%u"); rc = NRT_FAILURE; goto done
    if (rc = nrt_profile_session_init()) != 0: goto done
    nrt_inspect_config_allocate(&options); nrt_config_parse_inspect_config(options)
    if options->enable_inspect || options->enable_inspect_on_fail:
        nrt_inspect_begin_with_options()
    nrt_inspect_config_free(options)
    if gconf.funtime: rc = 0; goto done                      // sim: skip exec-ready
    rc = nrt_start_exec_ready_program()

done:                                                        // line 612
    log_dbg("API:OUT: (ret=%u)", rc)
    if rc != 0:
        nrt_infodump(ERROR, -1, rc, "nrt_init()")            // dump support bundle on failure
    else:
        nrt_state_set(NRT_STATE_INIT)                        // THE forward transition (line 620)
        nrt_infodump(INFO, -1, NRT_SUCCESS, "nrt_init()")
    // ctx RAII: if nlog_has_unflushed_logs() → log_err("Generic API Failure")
    return rc
```

> **GOTCHA —** the `gconf.funtime` flag (simulation/"functional" mode) **short-circuits the entire driver-compat negotiation** — when set, `nrt_init` jumps straight from the banner to `LABEL_55` core allocation (line 389), and again skips `kmgr_init` and `nrt_start_exec_ready_program` at the tail. A reimplementer who runs the compat-id check unconditionally will reject valid simulation runs that have no driver. The funtime path also marks the NC bitmap *itself* (line 449) instead of calling `ndl_crwl_nc_range_mark`, because there is no driver to arbitrate reservations.

> **QUIRK —** the per-arch compat ids are `(vtpb, v25) = (10, 11)` by default and `(6, 6)` for `AL_HAL_TPB_ARCH_TYPE_SUNDA` (lines 890-896). They are **two distinct ids** — `vtpb` is checked against `min`, `v25` against `max` — so the acceptance window is `vtpb >= min && max >= v25`. This is a range-containment test, *not* a single-version compare; a naive `installed == required` check is wrong in both directions.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_init` | `0x94e90` | 6503 | Master bring-up; the only forward `→INIT` transition | CERTAIN |
| `nrt_get_dev_info` | `0x83ad0` | — | Device count / cores-per-device / arch discovery | HIGH |
| `nrt_config_parse_init_config` | `0x8a1d0` | — | `NEURON_RT_*` env → `nrt_config_0` master parser | HIGH |
| `kmgr_register_framework` | `0xddff0` | — | Register `(framework_type, fw_ver, fal_ver)` with exec mgr | HIGH |
| `kmgr_init` | `0xde080` | — | Device init (skipped under `funtime`) | HIGH |
| `ndl_crwl_nc_range_mark` | — | — | Cooperative RW-lock reservation of the physical-core range | HIGH |
| `nrt_display_compat_msg` | `0x93160` | 182 | Prints the dkms compat-id mismatch (single id or `range %u - %u`) | CERTAIN |
| `nrt_soft_incompat_callback` | `0x93120` | 51 | WARN-logs "driver version is old, please upgrade" (registered into NDL) | CERTAIN |
| `nrt_start_exec_ready_program` | `0x94050` | 89 | Per-vcore `tdrv_start_exec_ready_program`; first non-SUCCESS wins | HIGH |

### Considerations

The `dbg_cc_dma_packet_size` vector is clamped to **16 entries** (4 streams × 4 archs) with a warning before the copy into `v129` (lines 330-345); the maximum-possible-streams constant is `4`. The LNC bitmap is `uint64_t bitmap[2]` plus a trailing `__int128`, swept by `tzcnt` to find the first reserved physical core (lines 470-480); the `0x20` argument to `ndl_crwl_nc_range_mark` is the bit width of the reservation namespace. The fields copied into `tpb_init_config_t v129` and their offsets are owned by [Configuration](config-structs.md); this page records only that the copy is a flat field-by-field move and that `virtual_core_size`, `hw_decode_toggle`, `scrub_hbm`, and `map_hbm` are among them.

---

## 2. `nrt_close` — Teardown

### Purpose

`nrt_close` (`@0x93c20`, export `@@NRT_2.0.0`) reverses `nrt_init`. It is guarded by `nrt_state_is_init()` — it does real work **only** if the runtime is currently `INIT`; otherwise it logs `API:IN/OUT` and returns. It unwinds in roughly the reverse order of bring-up: collective/inspect/profile producers first, then the exec manager, then per-vcore communicator state, then the driver-layer reservation, then the framework registration, microcode, config, and logging.

### Entry Point

```text
nrt_close (0x93c20)                                    export @@NRT_2.0.0
├─ nlog_coalescing_init_thread() ; build func-name tag ; log "API:IN: ()"
└─ [nrt_state_is_init()]                               ── guard: only act when state==INIT
     ├─ enc_async_sr_close_all()
     ├─ nrt_inspect_close()
     ├─ nrt_sys_trace_capture_close()
     ├─ nrt_profile_session_close()
     ├─ nrt_state_set(NRT_STATE_CLOSED)                ── enter CLOSED before heavy teardown
     ├─ [!gconf.funtime]
     │     ├─ kmgr_shutdown_xus() ; kmgr_unload_all_nns()
     │     ├─ for vcore in visible_virtual_cores:
     │     │      vtpb_translate_vtpb_to_ptpb(vcore) → set NC bitmap bits
     │     │      enc_free_cc_context_pool(vcore) ; enc_free_global_comm(vcore)
     │     ├─ enc_destroy_gcomm_locks() ; enc_fini_plugin() ; kmgr_destroy()
     │     ├─ ndl_crwl_nc_range_unmark(bitmap, 0x20)   ── release the reservation marked in nrt_init
     │     └─ ndl_device_cached_fd_close_all()
     ├─ tdrv_deregister_framework()
     ├─ ucode_teardown_module()
     ├─ nrt_config_free()
     ├─ nlog_close()
     └─ nrt_state_set(NRT_STATE_START)                 ── return to START (re-init is allowed)
```

### Algorithm

```c
// Models nrt_close @0x93c20. Returns void (the public symbol returns NRT_STATUS=0 by ABI).
function nrt_close():
    NlogErrorContextManager ctx("close")
    nlog_coalescing_init_thread()
    log_dbg("API:IN: ()")
    if nrt_state_is_init():                              // 0xb9080 — only tear down if up
        enc_async_sr_close_all()                        // collective async send/recv
        nrt_inspect_close()
        nrt_sys_trace_capture_close()
        nrt_profile_session_close()
        nrt_state_set(NRT_STATE_CLOSED)                 // CLOSED *before* device teardown
        if !nrt_gconf()->funtime:
            kmgr_shutdown_xus(); kmgr_unload_all_nns()
            uint64 bitmap[2] = {0}
            for vcore in visible_virtual_cores:
                vtpb_translate_vtpb_to_ptpb(vcore, ptpbs, &n)
                for j in [0, n): bitmap[ptpbs[j] >> 6] |= 1 << ptpbs[j]   // rebuild NC bitmap
                enc_free_cc_context_pool(vcore)
                enc_free_global_comm(vcore)
            enc_destroy_gcomm_locks(); enc_fini_plugin(); kmgr_destroy()
            ndl_crwl_nc_range_unmark(bitmap, 0x20)      // mirror of nrt_init's range_mark
            ndl_device_cached_fd_close_all()
        tdrv_deregister_framework()
        ucode_teardown_module()
        nrt_config_free()
        nlog_close()
        nrt_state_set(NRT_STATE_START)                  // back to START — a fresh nrt_init may follow
    log_dbg("API:OUT: ()")
    // ctx RAII: "Generic API Failure" on unflushed logs
```

> **NOTE —** `nrt_close` transitions `INIT → CLOSED → START`, **not** `INIT → CLOSED`. The brief `CLOSED` window (line 125) exists so that any concurrent API entry that sneaks in mid-teardown sees a deterministic non-`INIT` state and bails; the final `→START` (line 162) makes the process re-initializable — a subsequent `nrt_init` will pass its `!= NRT_STATE_START` guard. By contrast, the `CHILD` state (set in a forked child) is terminal: there is no API that clears it, so a forked child can never call `nrt_init` successfully.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_close` | `0x93c20` | 1035 | Full teardown; guarded by `nrt_state_is_init` | CERTAIN |
| `ndl_crwl_nc_range_unmark` | — | — | Release the physical-core reservation (mirror of `range_mark`) | HIGH |
| `tdrv_deregister_framework` | — | — | Drop the framework registration made in `kmgr_register_framework` | HIGH |
| `nrt_config_free` | — | — | Free the parsed `nrt_config_0` heap fields | MEDIUM |

---

## 3. The `NRT_INIT_STATE` Guard Machine

### Purpose

A single `.bss` word, `nrt_init_state @0xc5d1a0`, holds the runtime's lifecycle state. Every public API entry consults it before doing work, and the value decides whether the call proceeds, is rejected with `NRT_UNINITIALIZED`/`NRT_CLOSED`, or is refused because the process forked. The enum and its string table are DWARF-recovered and binary-verified.

### Algorithm

```c
// DWARF enum NRT_INIT_STATE (binary-verified values):
enum NRT_INIT_STATE { NRT_STATE_START = 0, NRT_STATE_INIT = 1,
                      NRT_STATE_CHILD = 2, NRT_STATE_CLOSED = 3, NRT_STATE_NUM = 4 };

// nrt_init_state_strings @0xc08900 — 4 char* into .rodata @0x83ed1f, binary-verified:
const char* nrt_init_state_strings[] =
    { "NRT_STATE_START", "NRT_STATE_INIT", "NRT_STATE_CHILD", "NRT_STATE_CLOSED" };

NRT_STATUS nrt_state_set(NRT_INIT_STATE s):  nrt_init_state = s; return NRT_SUCCESS;  // 0xb9090
bool       nrt_state_is_init():              return nrt_init_state == NRT_STATE_INIT; // 0xb9080
const char* nrt_state_get_string():          return nrt_init_state_strings[nrt_init_state]; // 0xb9060

// The guard pattern every PUBLIC entry runs (canonical form from nrt_unload/nrt_load_util):
function API_GUARD():
    switch nrt_init_state:
        case NRT_STATE_INIT:   proceed                                  // the only working state
        case NRT_STATE_START:  log_err("NRT uninitialized");  return NRT_UNINITIALIZED   // 13
        case NRT_STATE_CLOSED: log_warn("NRT already closed"); return NRT_CLOSED          // 14
        case NRT_STATE_CHILD:  log_err("Incompatible runtime state: %s",
                                       nrt_state_get_string());          return NRT_FAILURE  // 1
```

```text
                 nrt_init (success, line 620)
   ┌──────────────────────────────────────────────────┐
   │                                                   ▼
START(0) ────────────────────────────────────────► INIT(1)
   ▲                                                 │   │
   │   nrt_close final set (line 162)                │   │  pthread_atfork CHILD handler
   │ ◄───────────────── CLOSED(3) ◄──────────────────┘   │  (atfork_child @0x93110)
   │   nrt_close enter (line 125)                        ▼
   │                                                 CHILD(2)  ── terminal: no API clears it
   └─ a CHILD or CLOSED process re-init is rejected by nrt_init's START-only guard ─┘
```

> **GOTCHA —** the only state in which any public API does real work is `NRT_STATE_INIT`. `nrt_state_is_init()` (`@0xb9080`) is the fast probe used by `nrt_close` and several getters; the full four-way guard (above) is what load/unload/execute run. A reimplementation that checks "initialized?" as a boolean instead of distinguishing `START` (never inited → `NRT_UNINITIALIZED=13`), `CLOSED` (→ `NRT_CLOSED=14`), and `CHILD` (forked → `NRT_FAILURE=1`) will return the wrong status code and mislead every caller's error path. The exact spellings returned for `%s` are the `NRT_STATE_*` symbol names, not human prose — `nrt_state_get_string()` indexes the table directly with the raw enum value.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_state_set` | `0xb9090` | — | `nrt_init_state = state; return NRT_SUCCESS` | CERTAIN |
| `nrt_state_is_init` | `0xb9080` | — | `return nrt_init_state == NRT_STATE_INIT` | CERTAIN |
| `nrt_state_get_string` | `0xb9060` | — | `return nrt_init_state_strings[nrt_init_state]` | CERTAIN |
| `atfork_child` | `0x93110` | 10 | `nrt_state_set(NRT_STATE_CHILD)` — marks forked child | CERTAIN |
| `atfork_prepare` / `atfork_parent` | `0x93100` / `0x938e0` | 1 / 1 | Empty bodies (`ret`) — no pre/post-fork work | CERTAIN |

---

## 4. Version and Info Reporters

### Purpose

`nrt_get_version` fills a caller buffer with the four split version components, a human banner string, and the git sha; `nrt_infodump` writes a one-shot support bundle. Both are read-only with respect to lifecycle state. The version string `"2.31.24.0"` and git sha `"0b044f4ce917b633a70eb3d0bc460f34ac3da620"` are compiled-in constants, consistent with this build's package suffix.

### Algorithm

```c
// Models nrt_get_version @0x940b0 (export @@NRT_2.0.0; PLT thunk @0x3c400).
function nrt_get_version(buf, size):
    if size < 0xA0:                                     // 160
        log_err("Size is < %ld", 160);  return 1
    // split "2.31.24.0" on '.' into <=4 components, each parsed by set_version → uint64
    write 4 qwords at buf+0                              // major/minor/patch/maintenance
    snprintf(buf+32, 127, "libnrt version %s", "2.31.24.0")
    if size > 0xC8:                                      // 200
        copy git sha "0b044f4ce917b633a70eb3d0bc460f34ac3da620" at buf+160
    return 0

// Models set_version @0x938f0 — one "x" component → uint64.
function set_version(out, component_string):            // .cold @0x40184/0x40190
    if component_string is all-digit and non-empty:
        *out = strtoll(component_string, &end, 10)       // base 10
        if end == start:        throw_invalid_argument("stoll")   // .cold @0x40184
        if errno == ERANGE(34): throw_out_of_range("stoll")       // .cold @0x40190
    else:
        *out = 0                                          // non-numeric → silently 0

// Models nrt_infodump @0x94030 — one-shot severity-gated guard.
function nrt_infodump(level, a, status, who):
    if level < nrt_infodump::dumped_level @0xc088d8:     // monotonic: dump once per improving severity
        nrt_infodump_0(level, a, status, who)            // 0x93220 — the real bundle builder
```

> **QUIRK —** `nrt_get_version` writes into a *raw caller buffer at fixed offsets*, not a struct: four `uint64` components at `+0`, the `"libnrt version 2.31.24.0"` C-string at `+32`, and the 40-char git sha at `+160`. The size thresholds are hard: `< 160` is rejected outright, and the git sha is only written when the buffer exceeds **200** bytes. A reimplementer must size the buffer to at least 200 to receive the sha, and must not assume a packed struct — the `+32`/`+160` gaps are intentional padding for forward-compatible component growth.

> **NOTE —** `set_version` treats a **non-numeric component as the value 0** (it never reaches `strtoll` — the digit-validate loop at lines 49-58 returns `result=0`). Only the `strtoll` path itself can throw, and only for a component that *is* all-digit but overflows `long long` (`ERANGE` → out-of-range) — an edge a `"2.31.24.0"`-shaped string never hits. The throw trampolines exist for robustness against a malformed compiled-in or future version string, not for normal operation.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_get_version` | `0x940b0` | 836 | Fill buffer: 4 components + banner + git sha | CERTAIN |
| `.nrt_get_version` (thunk) | `0x3c400` | 8 | PLT jump into the hot body | CERTAIN |
| `set_version` | `0x938f0` | 347 | Digit-validate one component then `strtoll` | CERTAIN |
| `nrt_infodump` | `0x94030` | 21 | One-shot guard on `dumped_level @0xc088d8` | CERTAIN |
| `nrt_infodump_0` | `0x93220` | 1565 | Support-bundle builder (versions, instance, env) | HIGH |

---

## 5. Core-Dump Forwarding

### Purpose

When a load/execute/tensor path hits an error, it calls `nrt_core_dump` (`@0x92b90`). The forwarder builds a per-process set of path variables, expands the user's path templates, and `fork`/`execv`s the external `/opt/aws/neuron/bin/neuron-dump` helper with a long `--flag` argv to capture a core dump locally and/or to S3. The whole machinery is gated by `core_dumps_are_enabled()` (`@0x92410`): both a non-empty `ngc->local_core_dump_directory` *or* `ngc->s3_core_dump_prefix`, **and** `access("/opt/aws/neuron/bin/neuron-dump", X_OK) == 0`.

### Entry Point

```text
<load/execute/tensor error>
  └─ nrt_core_dump(err_loc, err_code, neff_name, neff_uuid, vtpb)   (0x92b90)
       ├─ core_dumps_are_enabled()                                  (0x92410)
       │      └─ (ngc dirs set) && access("/opt/aws/neuron/bin/neuron-dump", X_OK)==0
       ├─ init_paths → init_path_variables                          (0x92650 / 0x922c0)
       │      └─ pid, tid(syscall 186), log_id(atomic++), cluster_id, datetime, instance_id
       ├─ nrt_core_dump_format_string(template, out, cap, vars)     (0x924f0)  %p%t%l%c%d%i expand
       ├─ for core in vtpb->tpbs[0..num_tpbs):  fork → execv neuron-dump  --device-id --core-id …
       ├─ nrt_core_dump_format_file_name → nlog_dump_error_log(path)
       └─ nrt_core_dump_report_generated_file(vtpb, path)           (0x92920)
              └─ realpath → fork → execv neuron-dump --runtime-generated <path> ; parent waitpid
```

### Algorithm

```c
// Models nrt_core_dump_format_string @0x924f0 — the path templater.
// Bounded printf-like expansion of %X tokens from nrt_path_variables_t.
function nrt_core_dump_format_string(in, out, out_len, vars):
    if in == NULL: *out = 0; return
    o = 0
    for (i = 0; in[i] && o < out_len - 1; ):
        if in[i] == '%':
            switch in[i+1]:
                case 'p': src = vars.pid          // offset +0   getpid()
                case 't': src = vars.tid          // offset +14  syscall(186)=gettid
                case 'l': src = vars.log_id       // offset +28  atomic ++log_id @0xc5c798
                case 'c': src = vars.cluster_id   // offset +42  enc_get_cluster_id → "%016lx" | "none"
                case 'd': src = vars.datetime     // offset +62  strftime "%Y%m%d-%H%M%S"
                case 'i': src = vars.instance_id  // offset +78  tdrv_get_instance_info
                default:  out[o++] = '%'; i++; continue          // unknown %X → literal '%'
            n = snprintf(out + o, out_len - o, "%s", src)
            if (out_len - o) <= n: return         // truncation guard: stop, leave NUL
            o += n; i += 2
        else:
            out[o++] = in[i++]
    out[o] = 0

// Models nrt_core_dump_report_generated_file @0x92920 — the fork/exec forwarder tail.
function nrt_core_dump_report_generated_file(vtpb, file):
    if !core_dumps_are_enabled(): return                          // 0x92410
    if !realpath(file, resolved): log_err("Unable to find file: %s", file); return
    init_paths(...)                                               // re-derive path vars + dirs
    build argv = { "/opt/aws/neuron/bin/neuron-dump",
                   "--pid", pid, "--tid", tid, "--log-id", log_id, "--instance-id", instance_id,
                   "--local-output-", local_dir, ["--s3-output-prefix", s3_prefix,]
                   "--cluster-id", cluster_id, "--date-time", datetime,
                   "--runtime-generated", resolved, NULL }
    pid_t c = fork()
    if c == 0: execv(argv[0], argv); _exit               // child
    else:      waitpid(c, NULL, 0)                        // parent blocks until helper finishes
```

> **GOTCHA —** the `--s3-output-prefix` flag is present **only when `ngc->s3_core_dump_prefix` is set**, which shifts every later argv index by 2. A reimplementer hard-coding fixed argv positions for `--cluster-id`/`--date-time`/`--runtime-generated` will mis-parse the local-only case. Build the argv by appending, not by fixed index. (The exact index arithmetic for the per-core `nrt_core_dump` argv is owned by the [Telemetry](../trace/telemetry-errors.md) page.)

> **NOTE —** the default local dump-dir template, when `ngc->local_core_dump_directory` is unset but core dumps are enabled via the S3 prefix, is `"/tmp/neuron-core-dump/dt-%d-cid-%c"` (from `init_paths @0x92650`) — i.e. datetime + cluster-id. The templater is bounded (cap `0x1000` for dirs, `0xFF` for file names) and never overflows: it stops at `out_len - 1` and always NUL-terminates.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_core_dump` | `0x92b90` | 1373 | Primary error-time entry; per-core argv + fork/exec | HIGH |
| `nrt_core_dump_format_string` | `0x924f0` | 346 | Bounded `%p/%t/%l/%c/%d/%i` templater | CERTAIN |
| `core_dumps_are_enabled` | `0x92410` | 219 | Gate: ngc dirs set + `access(neuron-dump, X_OK)` | CERTAIN |
| `init_path_variables` | `0x922c0` | 321 | Fill `nrt_path_variables_t` (pid/tid/log_id/cluster/datetime/instance) | HIGH |
| `init_paths` | `0x92650` | 321 | Expand local-dir + s3-prefix templates | HIGH |
| `nrt_core_dump_report_generated_file` | `0x92920` | 612 | `realpath` → fork/execv `--runtime-generated` | HIGH |

### Considerations

The `nrt_path_variables_t` struct (101 bytes) and its field offsets are recovered from DWARF (`pid@+0`, `tid@+14`, `log_id@+28`, `cluster_id@+42`, `datetime@+62`, `instance_id@+78`); the templater reads these directly. `log_id` is an atomically-incremented global (`@0xc5c798`) so concurrent dumps get distinct ids. The `neuron-dump` helper path is hard-coded; there is no env override for it, so a reimplementation targeting a different layout must patch `core_dumps_are_enabled` and the argv builder together.

---

## Related Components

| Name | Relationship |
|---|---|
| `nrt_config_parse_init_config` (`0x8a1d0`) | The `NEURON_RT_*` env parser `nrt_init` calls at step (4); owns `nrt_config_0` |
| `kmgr_init` / `kmgr_register_framework` | The exec-manager bring-up `nrt_init` drives; owns the device-init detail |
| `ndl_crwl_nc_range_mark` / `_unmark` | The cooperative RW-lock that arbitrates physical-core reservation across processes |
| `nrt_load` / `nrt_unload` | Public entries that run the §3 guard before touching the device |
| `nrt_infodump_0` (`0x93220`) | The support-bundle body invoked from the `nrt_init`/`nrt_close` tails |

## Cross-References

- [Overview](overview.md) — the runtime-core map; where lifecycle sits among the public API surfaces
- [Public C API: Device, Config and Env Parsing](api-device-config.md) — `nrt_config_parse_init_config`, `nrt_get_dev_info`, and the `NEURON_RT_*` env grammar `nrt_init` consumes
- [Public C API: Tensor Surface and I/O](api-tensors.md) — tensor entries that run the §3 state guard and call `nrt_core_dump` on error
- [Public C API: Async, SendRecv and Collectives Thunks](api-async-collectives.md) — `nrta_cc_*` entries gated by the same state machine
- [Configuration: nrt_config and nrt_global_config](config-structs.md) — field offsets of `nrt_config_0` and `nrt_global_config_t` (`funtime`, `v4_plus`, `virtual_core_size`) the lifecycle code reads
- [Error and Status Codes (NRT_STATUS)](error-codes.md) — the `NRT_UNINITIALIZED(13)`/`NRT_CLOSED(14)`/`NRT_FAILURE(1)`/`NRT_INVALID(2)` returns the guard emits
- [TDRV: Device Bring-Up and Lifecycle](tdrv-lifecycle.md) — `tdrv_*` callees: device discovery, HW-revision override, framework (de)registration
- [The Load Pipeline (Parse → Build → Stage → Relocate)](../neff/load-pipeline.md) — what `nrt_load` does after the guard, downstream of init
- [Overview: the Hot Inference Path](../exec/overview.md) — `kmgr_init`/`kmgr_register_framework`, the exec-manager bring-up `nrt_init` starts
