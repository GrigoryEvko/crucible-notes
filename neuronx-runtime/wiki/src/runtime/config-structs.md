# Configuration: nrt_config and nrt_global_config

> *All offsets, sizes, types, and addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries full DWARF v4 — every struct tag, field name, and field offset below is read verbatim from the DWARF `structures.json` export, not inferred. All four `PT_LOAD` segments are identity-mapped: `.text`, `.rodata`, **and `.data`/`.bss` are VMA == file offset** (`p_offset == p_vaddr`, verified) — read any global at its VMA directly, no `0x400000` (or any) relocation delta. All addresses are analysis VMAs. Other versions will differ.*
>
> *Evidence grade: **Confirmed (DWARF-anchored)** — field layouts are the DWARF `structures.json` truth; sizes are independently corroborated (656 B by `nm` symbol spacing `0x290`, 296 B by the `calloc(0x128,1)` at the allocation site); env-var → field → default bindings are recovered from `nrt_config_parse_init_config @0x8a1d0` (`P1-F-KNOBS`). · Part IV — Userspace Runtime Core / REFERENCE catalogue · [back to index](../index.md)*

## Abstract

The Neuron Runtime parses its entire `NEURON_RT_*` environment **once**, at `nrt_init` time, into two distinct, process-global configuration aggregates. This page is the authoritative field-by-field catalogue of both. The first is **`nrt_config_t`** (656 B), the file-scope **static singleton** `nrt_config` (DWARF/IDA name `nrt_config_0`) at `.bss 0xc5c480` — 73 members, default-constructed by a file-scope ctor and read directly everywhere as `nrt_config_0.<field>`. The second is **`nrt_global_config_t`** (296 B), a **heap buffer** `calloc`'d inside the parser, filled field-by-field, then published to the global pointer `ngc` at `.bss 0xc5c460` and read everywhere through the accessor `nrt_gconf()` (`@0x82670`, literally `return ngc;`). Both are populated in a single pass by `nrt_config_parse_init_config @0x8a1d0`, the env→struct mapper; the sibling [api-device-config](api-device-config.md) page derives the *mechanism* (how a name becomes a field), and this page owns the *catalogue* (every field, offset, type, env var, default, and consumer).

The familiar reference frame is a classic **parse-once config block**: a runtime that reads `getenv` for a fixed set of tunables at startup and freezes the result for process lifetime — the shape of an OpenMP `OMP_*` ingest or a CUDA `CUDA_*` env scan. The Neuron variant has two wrinkles a reimplementer must reproduce exactly. First, the destination is **two separate structs**, not one — the `lea` that computes a parse's destination targets either the 656 B static `nrt_config` or the 296 B heap `*ngc`, and only the base register distinguishes them. Second, the same logical concept can live in **both** structs at once: `funtime` is a member of `nrt_config_t` (at `+337`) *and* of `nrt_global_config_t` (at `+184`), each set independently — the cfg copy is the canonical operator-facing flag, the gconf copy is the mirror that the hot path reads via `nrt_gconf()->funtime`.

This is a **reference catalogue**, so it takes the deliberate style-guide exception for struct layouts: two full per-field tables, one per struct, every row anchored to a DWARF offset. Where a field is set from the environment, the env var, the parse-time default (the literal 2nd argument of the typed `nrt_config_parse` helper), and a representative consumer are given; where a field is set programmatically or its setter was not traced, the row is confidence-tagged. The page closes with the embedded sub-struct layouts (`enc_proxy_histogram_config_t`, `kbin_fp8_conv_cfg_t`, `numerical_error_notification_verbosity_t`) that the two aggregates contain.

## Reimplementation Contract

- **Two distinct structs, not one.** `nrt_config_t` (656 B, static singleton) and `nrt_global_config_t` (296 B, heap, published via `ngc`) are *separate* aggregates with *independent* layouts. `nrt_gconf()` returns the **296 B `ngc`**, never the static singleton. Do not merge them.
- **The exact sizes are load-bearing for the allocation.** The 296 B size is the literal `calloc(0x128, 1)` argument at the parser's allocation site; the 656 B size is the `nm` span between `nrt_config @0xc5c480` and the next `.bss` symbol `cached_info @0xc5c710` (`0x290`). A reimplementation that mis-sizes either struct will mis-place every field past the error.
- **The lifecycle.** The static singleton is default-constructed at load (file-scope ctor `_GLOBAL__sub_I_nrt_config.cpp @0x74920`, which only zeroes the `std::vector`/`std::map` members and registers `~nrt_config` via `__cxa_atexit`); the heap struct is allocated, filled, and published inside `nrt_config_parse_init_config @0x8a1d0`, then freed by `nrt_config_free @0x82680` (`free(ngc); ngc=0;`).
- **The DWARF offsets are authoritative.** Earlier scan passes (SCAN-01, NX-023) recorded several wrong offsets and a wrong one-struct model; every such value is corrected in place below against the DWARF truth.

| | |
|---|---|
| **Static singleton** | `nrt_config` (`nrt_config_0`) `@.bss 0xc5c480` — `nrt_config_t`, **656 B**, 73 members |
| **656 B corroboration** | `nm` span `0xc5c480`→`cached_info 0xc5c710` = `0x290` = 656 |
| **Heap global** | `nrt_global_config_t`, **296 B**, 47 members — pointer `ngc @.bss 0xc5c460` |
| **296 B corroboration** | `calloc(0x128, 1)` at the parser's allocation site (`0x128` = 296) |
| **gconf accessor** | `nrt_gconf` `@0x82670` (`return ngc;`) · free `nrt_config_free` `@0x82680` |
| **Single env parser** | `nrt_config_parse_init_config` `@0x8a1d0` — fills **both** in one pass |
| **Static-singleton ctor** | `_GLOBAL__sub_I_nrt_config.cpp` `@0x74920` (zeroes vec/map, registers dtor) |
| **Static-singleton dtor** | `~nrt_config` `@0x8fc00` (atexit) |
| **DWARF tags** | `nrt_config_t` ord 22985 ≡ tag 14357 · `nrt_global_config_t` ord 6552 ≡ tag 17627 |

> **CORRECTION (NX-023 / SCAN-01 — two-struct model) —** an earlier low-effort pass and `NX-023` recorded a **single** config object "`nrt_config_0` … accessed via `nrt_gconf()->field`, returns global `ngc`." This is wrong. `nm` lists `ngc @0xc5c460` and `nrt_config @0xc5c480` as **two separate `.bss` symbols**, and the disassembled body of `nrt_gconf @0x82670` is `return ngc;` — it returns the 296 B heap pointer, **not** the 656 B static singleton. The static singleton is read directly as `nrt_config_0.<field>` (>300 refs); the heap struct is read as `nrt_gconf()->field`. They are **distinct structs with distinct layouts**; the same offset means a different field in each. Merging them mis-places every field. Verified by: the two `nm` symbols, the `nrt_gconf`/`nrt_config_free` bodies, and the publish `ngc = <calloc'd ptr>` at the end of the parser.

---

## 1. `nrt_config_t` — the 656-Byte Static Singleton

### Purpose

`nrt_config_t` is the operator-facing config block: debug knobs, collective-compute (CC) tuning, DMA packetization, HBM scrub, microcode/HW-decode paths, the visible-core vector, and the funtime/fake-instance flags. It is a single file-scope static (`nrt_config_0 @0xc5c480`), so every consumer reads it as `nrt_config_0.<field>` with no indirection. The file-scope ctor (`@0x74920`) zero-initializes only the non-trivial members (the two `std::vector<int>` and the `std::map`); the scalar members are left for `nrt_config_parse_init_config @0x8a1d0` to set from the environment (or its inline default if the env var is unset).

### Struct Layout

Reference-catalogue table — every offset is the DWARF `structures.json` value. `Env Var` is the `NEURON_RT_*`/`NEURON_*` name the parser reads into the field (blank ⇒ no direct env parse located in `init_config`); `Default` is the literal recovered at the parse call site (the typed helper's 2nd argument), or the arch-dependent rule where noted. `init:N` in the meaning cell is the line in `nrt_config_parse_init_config` that performs the parse.

| Field | Offset | Type | Env Var | Default | Meaning | Conf |
|---|---|---|---|---|---|---|
| `metric_send_interval_secs` | `+0` | `uint32_t` | — | — | Metrics emit cadence; set by DLR/metrics init, not `init_config` | LOW |
| `dlr_worker_count` | `+4` | `uint32_t` | — | — | DLR worker-thread count; setter not in `init_config` | LOW |
| `virtual_core_size` | `+8` | `uint32_t` | `NEURON_LOGICAL_NC_CONFIG` | **2** (1 if 1 core/dev; forced 1 on SUNDA) | LNC fusion size; via `parse_vnc_config @0x83b40` (init:298) | HIGH |
| `num_cores` | `+12` | `uint32_t` | `NEURON_RT_NUM_CORES` | **0** | Requested logical-core count (0 ⇒ whole device); init:311 | HIGH |
| `visible_virtual_cores` | `+16` | `std::vector<int>` | `NEURON_RT_VISIBLE_CORES` | — (empty) | Visible-LNC list; `parse_visible_virtual_cores @0x85170` (init:326); 82 refs | HIGH |
| `cc_root_comm_id` | `+40` | `const char *` | `NEURON_RT_ROOT_COMM_ID` | NULL | Collective root comm id; `getenv`→`strdup` (init:375); 25 refs | HIGH |
| `sim_debug_flags` | `+48` | `uint32_t` | `NEURON_SIM_DEBUG_FLAGS` | **`'U'` (0x55)** | Simulator debug bitfield; init:392 | LOW |
| `scratchpad_page_size` | `+56` | `uint64_t` | `NEURON_SCRATCHPAD_PAGE_SIZE` | (value `<<20`) | Scratchpad page bytes; ×`NEURON_RT_ONE_TMPBUF_PAGE_SIZE_MB`; init:512–523 | HIGH |
| `reset_cores` | `+64` | `bool` | `NEURON_RT_RESET_CORES` | **1** | Reset cores on init; init:405 | HIGH |
| `mem_log_size` | `+72` | `size_t` | `NEURON_RT__MEMLOG_MAX_SIZE` | — | Device-memory log ring size; init:491 | LOW |
| `max_trace_entries` | `+80` | `size_t` | `NEURON_RT_TRACE_NUM_ENTRIES` | — | Trace ring entry cap; init:507 | LOW |
| `dbg_force_strict_ordering` | `+88` | `bool` | `NEURON_RT_DBG_FORCE_STRICT_ORDERING` | **0** | Strict ordering; forces `hw_decode_toggle=0` (init:534,1675) | MED |
| `dbg_io_crc` | `+89` | `bool` | `NEURON_RT_DBG_IO_CRC` | **0** | CRC I/O tensors; init:539 | LOW |
| `dbg_cc_nop` | `+90` | `bool` | `NEURON_RT_DBG_CC_NOP` | **0** | CC becomes NOP (WARN init:544,1665) | HIGH |
| `dbg_cc_force_single_rank_rg` | `+91` | `bool` | `NEURON_RT_DBG_CC_FORCE_SINGLE_RANK_RG` | **0** | Force single-rank replica group (WARN init:549,1656) | HIGH |
| `dbg_cc_check_sigs` | `+92` | `bool` | `NEURON_RT_DBG_CC_CHECK_SIGS` | **1** | CC signature checks; init:554 | MED |
| `dbg_verbose_log_dev_mem` | `+93` | `bool` | `NEURON_RT_DBG_VERBOSE_DEV_MEM` | **0** | Verbose device-mem logging; init:559 | LOW |
| `notif_error_nq_coalesce` | `+94` | `bool` | `NEURON_RT_ERROR_NQ_COALESCE` | **0** (if env present) | Coalesce error NQ entries; init:913 | LOW |
| `notif_full_evsem` | `+95` | `bool` | `NEURON_RT_DBG_DROP_EVENTSEM_NOTIF` | **1** (stored as `!drop`) | Full event-sem notif; init:888–896 | LOW |
| `v2_min_ignore_nc_memory_error_interval` | `+96` | `time_t` | `NEURON_RT_MIN_MEM_ERROR_INTERVAL` | — | v2 mem-error suppress window; init:564 | LOW |
| `dbg_hierarchical_cc_mode` | `+104` | `uint32_t` | `NEURON_RT_DBG_HIERARCHICAL_CC` | **0** | Hierarchical CC mode; init:570 | MED |
| `dbg_kangaring_cc_mode` | `+108` | `uint32_t` | `NEURON_RT_DBG_KANGARING_CC` | **0** | Kangaring CC mode; init:575 | MED |
| `dbg_single_cycle_ring_allr_mode` | `+112` | `uint32_t` | `NEURON_RT_DBG_SINGLE_CYCLE_RING_ALLR_CC` | **2** | Single-cycle ring all-reduce; init:619 | MED |
| `dbg_rdh_cc_mode` | `+116` | `uint32_t` | `NEURON_RT_DBG_RDH_CC` | **1** | RDH CC mode; init:580 | MED |
| `dbg_inter_rdh_ch_buf_size` | `+120` | `size_t` | `NEURON_RT_DBG_INTER_RDH_CHANNEL_BUFFER_SIZE` | **0x2000000** | Inter-RDH channel buffer; init:609 | MED |
| `dbg_inter_rdh_recv_window_n` | `+128` | `uint32_t` | `NEURON_RT_DBG_INTER_RDH_RECV_WINDOW_N` | **0** | Inter-RDH recv window; init:614 | LOW |
| `pool_stdout_queue_size_bytes` | `+132` | `uint32_t` | `NEURON_RT_GPSIMD_STDOUT_QUEUE_SIZE_BYTES` | **0** | GPSIMD stdout pool bytes; init:843 | LOW |
| `dbg_cc_stream_mode` | `+136` | `uint32_t` | `NEURON_RT_DBG_CC_STREAM_MODE` | **1** | CC stream mode; init:585 | MED |
| `dbg_mesh_cc_mode` | `+140` | `bool` | `NEURON_RT_DBG_MESH_CC` | **1** | Mesh CC mode; init:629 | MED |
| `dbg_mesh_double_buffer` | `+141` | `bool` | `NEURON_RT_DBG_MESH_DOUBLE_BUF` | **1** | Mesh double-buffering; init:634 | LOW |
| `dbg_rdh_double_buffer` | `+142` | `bool` | `NEURON_RT_DBG_RDH_DOUBLE_BUF` | **0** | RDH double-buffering; init:639 | LOW |
| `dbg_enforce_mesh_global_handshake` | `+144` | `uint32_t` | `NEURON_RT_DBG_ENFORCE_MESH_GLOBAL_HANDSHAKE` | **0** | Mesh global handshake; init:644 | LOW |
| `dbg_proxy_early_completion` | `+148` | `bool` | `NEURON_RT_DBG_EARLY_COMPLETION` | **1** | Proxy early completion; init:649 | MED |
| `dbg_proxy_early_posting` | `+149` | `bool` | `NEURON_RT_DBG_EARLY_POSTING` | **1** | Proxy early posting; init:654 | MED |
| `ranks_per_network_proxy` | `+152` | `uint32_t` | `NEURON_RT_RANKS_PER_NETWORK_PROXY` | **1** | Ranks per proxy; `>1` requires HW barrier (init:1687,1692) | HIGH |
| `one_thread_per_core` | `+156` | `bool` | `NEURON_RT_ONE_THREAD_PER_CORE` | **0** | One worker thread per core; init:664 | MED |
| `dbg_topsp_semaphores_clear_check` | `+157` | `bool` | — | — | TopSP semaphore clear check; setter not in `init_config` | LOW |
| `dbg_hier_cc_pipeline` | `+160` | `uint32_t` | `NEURON_RT_DBG_HIER_CC_PIPELINE` | **0** | Hierarchical CC pipelining; init:679 | LOW |
| `dbg_cc_inter_mesh_max_node_n` | `+164` | `uint32_t` | `NEURON_RT_DBG_CC_INTER_MESH_MAX_NODES` | **0x10** | Inter-mesh max nodes; init:684 | LOW |
| `dbg_cc_dma_packet_size` | `+168` | `std::vector<int>` | `NEURON_RT_DBG_CC_DMA_PACKET_SIZE` | — | Per-stream CC DMA packet sizes; init:690; clamped to 16 in `nrt_init` | MED |
| `cc_nr_channel_chunks` | `+192` | `uint32_t` | `NEURON_RT_CC_CHANNEL_BUFFER_N` | **8** | CC channel-buffer chunk count; init:695 | MED |
| `cc_chunk_size` | `+196` | `uint32_t` | `NEURON_RT_CC_CHUNK_SIZE` | **0x200000** | CC chunk size bytes; init:700 | MED |
| `cc_allowed_alg_types` | `+200` | `uint32_t` | `NEURON_RT_CC_ALG_TYPES` | — | CC algorithm-type bitmap; `parse_cc_alg_types @0x89680` (init:713) | HIGH |
| `dbg_topsp_evsem_notif_enabled` | `+204` | `bool` | — | — | TopSP event-sem notif enable; setter not in `init_config` | LOW |
| `disable_execution_barrier` | `+205` | `bool` | `NEURON_RT_DISABLE_EXECUTION_BARRIER` | **0** | Disable exec barrier; init:935 | MED |
| `enable_hw_execution_barrier` | `+206` | `bool` | `NEURON_RT_HW_EXECUTION_BARRIER` | **= is_switch_v1_family** | HW exec barrier; arch-default (init:942–955) | HIGH |
| `enable_internode_execution_barrier` | `+207` | `bool` | `NEURON_RT_INTERNODE_EXECUTION_BARRIER` | **1** | Internode exec barrier; init:969 | MED |
| `v2_disable_pack_descs` | `+208` | `bool` | `NEURON_RT_DISABLE_PACK_DESCRIPTORS` | **0** | Disable descriptor packing; init:1344 | LOW |
| `dma_packetization_size` | `+212` | `uint32_t` | `NEURON_RT_DBG_DMA_PACKETIZATION_SIZE` | **0x1000** (cap `0xFFFFF`) | DMA packet size; range-checked (init:1320,1329) | HIGH |
| `enable_memory_metrics` | `+216` | `bool` | `NEURON_RT_ENABLE_MEMORY_METRICS` | **1** | Memory-metrics emit; init:726 | LOW |
| `neuron_ucode_path` | `+224` | `const char *` | `NEURON_RT_UCODE_LIB_PATH` | NULL | GPSIMD microcode lib path; `strdup` (init:1347) | MED |
| `hw_decode_table_path` | `+232` | `const char *` | `NEURON_RT_DBG_HW_DECODE_BINS_DIR` | NULL | HW-decode table dir; init:1352 | MED |
| `hw_decode_toggle` | `+240` | `uint32_t` | `NEURON_RT_DBG_HW_DECODE_TOGGLE` | **arch: 4→11, 3→3, else 0** | HW-decode enable; forced 0 if strict-order; ERROR on v2 (init:1357–1364) | HIGH |
| `enable_no_compile_mode` | `+244` | `bool` | `NEURON_NO_COMPILE_MODE` | **0** | No-compile (pre-built) mode; init:1374 | LOW |
| `no_compile_mode_timeout` | `+248` | `uint32_t` | `NEURON_NO_COMPILE_MODE_TIMEOUT` | **0x78** | No-compile timeout secs; init:1378 | LOW |
| `fail_on_nan` | `+252` | `bool` | `NEURON_FAIL_ON_NAN` | **0** | Fail on NaN; forces `numerical_errors_verbosity>=1` (init:1382,1685) | MED |
| `dump_inputs_on_err` | `+256` | `__int64` | `NEURON_RT_DBG_DUMP_INPUTS_ON_ERR` | **0** | Dump inputs on error; init:1386 | LOW |
| `input_dump_directory` | `+264` | `const char *` | `NEURON_RT_DBG_INPUT_DUMP_DIRECTORY` | **`"/tmp/neuron-input-dump"`** | Input-dump dir (len-capped); init:1390 | LOW |
| `hbm_scrub_init_val` | `+272` | `uint32_t` | `NEURON_RT_HBM_INIT_VAL` | — | HBM scrub fill value; `parse_hbm_scrub_init_val @0x815e0` (init:1396) | MED |
| `scrub_hbm` | `+276` | `bool` | `NEURON_RT_HBM_INIT_VAL` | — | HBM-scrub enable (set with `hbm_scrub_init_val`); init:1396 | MED |
| `profile_buf_sizes` | `+280` | `std::map<notification_type,uint>` | `NEURON_RT_PROFILE_BUF_*` | — | Per-notif-type profiling buffer sizes; `nrt_config_profile_buf_size @0x828b0` | MED |
| `m2s_prefetch_threshold` | `+328` | `uint32_t` | `NEURON_RT_DBG_M2S_PREFETCH_THRESHOLD` | **0** | Mem→stream prefetch threshold; init:1400 | LOW |
| `s2m_prefetch_threshold` | `+332` | `uint32_t` | `NEURON_RT_DBG_S2M_PREFETCH_THRESHOLD` | **0x77** | Stream→mem prefetch threshold; init:1404 | LOW |
| `map_hbm` | `+336` | `bool` | `NEURON_RT_MAP_HBM` | **0** | Map HBM into address space; init:1408 | MED |
| `funtime` | `+337` | `bool` | `NEURON_RT_FAKE_INSTANCE_TYPE` | — (unset) | **Simulation/"functional" mode**; `nrt_config_parse_funtime @0x83760`; mirrored to `gconf.funtime` (init:286) | HIGH |
| `fake_family` | `+340` | `uint32_t` | `NEURON_RT_FAKE_INSTANCE_TYPE` | — | Faked instance family (funtime); from instance-family parse | HIGH |
| `fake_size` | `+344` | `uint32_t` | `NEURON_RT_FAKE_INSTANCE_TYPE` | — | Faked instance size (funtime); `nrt_get_instance_size` | HIGH |
| `virtual_server_size` | `+348` | `uint32_t` | `NEURON_RT_DBG_VIRTUAL_SERVER_SIZE` | **0** | Virtual-server core count; init:1412 | MED |
| `low_latency_tasks_cpu_affinity_mask` | `+352` | `cpu_set_t` (128 B) | `NEURON_RT_LOW_LATENCY_TASKS_CPU_AFFINITY` | — | Low-latency thread affinity; `parse_low_latency_tasks_cpu_affinity @0x85c70` (init:741) | MED |
| `cc_proxy_histogram_config` | `+480` | `enc_proxy_histogram_config_t` (168 B) | `NEURON_RT_DBG_CC_PROXY_HISTOGRAM_CONFIG` | — | Proxy histogram profiling; `get_enc_proxy_histogram_config @0x847f0` (init:1415); 35 refs | MED |
| `dbg_force_use_2dev_proxy` | `+648` | `bool` | `NEURON_RT_DBG_FORCE_2DEV_PROXY` | **0** | Force 2-device proxy; init:1498 | LOW |
| `dbg_hybrid_ring_cc_enabled` | `+649` | `bool` | `NEURON_RT_DBG_HYBRID_RING_CC_EN` | **1** | Hybrid-ring CC enable; init:1502 | LOW |
| `dbg_cc_sort_rg` | `+650` | `bool` | `NEURON_RT_DBG_CC_SORT_RG` | **0** | Sort replica groups; init:1570 | LOW |

Bytes `+651..+655` are tail padding to the 656 B (`0x290`) struct size.

> **NOTE —** `dbg_cc_dma_packet_size` (`+168`, `std::vector<int>`) and `dma_prio_pkt_size` (a `gconf` array, see §2) name **conflicting** DMA-priority knobs; `init_config` warns when both are set (init:1317). `dbg_cc_dma_packet_size` is also clamped to **16 entries** (4 streams × 4 archs) inside `nrt_init` before the copy into `tpb_init_config_t`, with a warning on overflow — see [api-lifecycle §1 Considerations](api-lifecycle.md).

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_config_parse_init_config` | `0x8a1d0` | 23088 | Master env parser; fills `nrt_config_0` and the heap gconf | HIGH |
| `_GLOBAL__sub_I_nrt_config.cpp` | `0x74920` | — | File-scope ctor: zero vec/map members, register dtor via `__cxa_atexit` | HIGH |
| `~nrt_config` | `0x8fc00` | — | Static-singleton dtor (atexit): destroys vectors/map | HIGH |
| `nrt_config_parse_funtime` | `0x83760` | 879 | `NEURON_RT_FAKE_INSTANCE_TYPE` → `funtime`/`fake_family`/`fake_size` | HIGH |
| `parse_vnc_config` | `0x83b40` | — | `NEURON_LOGICAL_NC_CONFIG` → `virtual_core_size` (`+8`) | HIGH |
| `parse_visible_virtual_cores` | `0x85170` | — | `NEURON_RT_VISIBLE_CORES` → `visible_virtual_cores` (`+16`) | HIGH |
| `parse_cc_alg_types` | `0x89680` | — | `NEURON_RT_CC_ALG_TYPES` → `cc_allowed_alg_types` (`+200`) | HIGH |
| `parse_hbm_scrub_init_val` | `0x815e0` | — | `NEURON_RT_HBM_INIT_VAL` → `hbm_scrub_init_val`/`scrub_hbm` | MED |
| `parse_low_latency_tasks_cpu_affinity` | `0x85c70` | — | env → `cpu_set_t` mask (`+352`, 128 B) | MED |
| `get_enc_proxy_histogram_config` | `0x847f0` | — | 5-token grammar → `cc_proxy_histogram_config` (`+480`) | MED |
| `nrt_config_profile_buf_size` | `0x828b0` | — | `NEURON_RT_PROFILE_BUF_*` → `profile_buf_sizes` map (`+280`) | MED |

---

## 2. `nrt_global_config_t` — the 296-Byte Heap Global

### Purpose

`nrt_global_config_t` holds the knobs the **hot path** reads: async-execution depth, DGE bounds-check bypasses, DMA priority packet sizes, FP8 numerics, stochastic rounding, P2P enable, and the gconf `funtime` mirror. It is `calloc(0x128, 1)`'d inside `nrt_config_parse_init_config @0x8a1d0`, filled field-by-field, then published with `ngc = <ptr>` at the parser's tail. Every steady-state read goes through `nrt_gconf()` (`@0x82670`, `return ngc;`), so consumers spell it `nrt_gconf()->field`. It is freed by `nrt_config_free @0x82680` (`free(ngc); ngc=0;`) during `nrt_close`.

### Struct Layout

Same column grammar as §1. The `calloc` zero-fill means an unparsed field reads as 0/NULL; `Default` is the value the parser writes when the env var is absent (the typed helper's 2nd argument), or the arch-dependent rule where noted.

| Field | Offset | Type | Env Var | Default | Meaning | Conf |
|---|---|---|---|---|---|---|
| `allow_legacy_neff` | `+0` | `uint32_t` | `NEURON_RT_ALLOW_LEGACY_NEFF` | — | Allow legacy NEFF format; **parsed in NEFF load path** (`kelf::load @0x497dc0`), not `init_config`; 2 consumers | HIGH |
| `device_oom_dump_directory` | `+8` | `const char *` | `NEURON_RT_OOM_DUMP_DIRECTORY` | **`"/tmp"`** | Device-OOM dump dir; first `getenv` in init (init:251), path capped 200 (`0xC8`) | MED |
| `scratchpad_on_single_core` | `+16` | `uint32_t` | `NEURON_RT_DBG_SCRATCHPAD_ON_SINGLE_CORE` | **0** | Force scratchpad onto one core; init:525 | MED |
| `implicit_async_mode` | `+20` | `bool` | — | — (derived) | Implicit async-exec mode; set from inflight logic (init:1652); 13 consumers | HIGH |
| `async_exec_max_inflight_requests` | `+24` | `uint32_t` | `NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS` | **0** (clamp 63) | Max in-flight async reqs; clamped if >63 (init:1624,1630) | HIGH |
| `dbg_disable_dge` | `+28` | `uint32_t` | `NEURON_RT_DBG_DISABLE_DGE` | **0** | **Disable DMA Generation Engine** (security-relevant); init:982 | HIGH |
| `dge_disable_bound_check` | `+32` | `uint32_t` | `NEURON_RT_DBG_DISABLE_DGE_BOUND_CHECK` | **0** | **Disable DGE descriptor bounds check** (security-relevant); init:996; 5 consumers | HIGH |
| `dbg_vector_dge_skip_notif` | `+36` | `bool` | `NEURON_RT_DBG_VECTOR_DGE_SKIP_NOTIF` | **0** | Skip vector-DGE notif; init:1010 | MED |
| `ultraserver_mode` | `+40` | `uint32_t` | `NEURON_RT_ULTRASERVER_MODE` | **0** | UltraServer topology mode; forced =4 if `…_SINGLE_NODE` set (init:1023,1052) | HIGH |
| `dma_prio_pkt_size` | `+44` | `uint32_t[5]` | `NEURON_RT_DBG_DMA_PRIO_PKT_SIZE` | — | Per-priority DMA packet sizes; number-range parse (init:1060,1166); 6 consumers | HIGH |
| `cc_dma_copy_prio_pkt_size` | `+64` | `uint32_t[5]` | `NEURON_RT_DBG_CC_DMA_COPY_DMA_PRIO_PKT_SIZE` | — | CC-copy DMA priority packet sizes; init:1077 | MED |
| `cc_cce_reduce_prio_pkt_size` | `+84` | `uint32_t[5]` | `NEURON_RT_DBG_CC_CCE_REDUCE_DMA_PRIO_PKT_SIZE` | — | CC-reduce DMA priority packet sizes; init:1094 | MED |
| `async_sr_boot_port` | `+104` | `uint32_t` | `NEURON_RT_ASYNC_SENDRECV_BOOTSTRAP_PORT` | — | Async send/recv bootstrap port; init:814 | LOW |
| `async_sr_experimental_enabled` | `+108` | `bool` | `NEURON_RT_ASYNC_SENDRECV_EXPERIMENTAL_ENABLED` | — | Experimental async S/R; init:828 | LOW |
| `experimental_contiguous_scratchpad` | `+112` | `uint32_t` | `NEURON_RT_DBG_EXPERIMENTAL_CONTIGUOUS_SCRATCHPAD` | **0** | Contiguous scratchpad alloc; init:530 | LOW |
| `enable_p2p` | `+116` | `bool` | `NEURON_RT_ENABLE_P2P` | **1** | Peer-to-peer enable; gates `nrt_get_dmabuf_fd` export; init:624 | MED |
| `seq_instr_block_size` | `+120` | `uint64_t[5]` | `NEURON_RT_DBG_SEQ_IRAM_BLOCK_SIZES_KB` | — | Per-engine IRAM block sizes (KB`<<10`); po2/IRAM-divisor validated (init:1507–1547) | MED |
| `br_label_align_thresh` | `+160` | `uint64_t` | `NEURON_RT_DBG_BRANCH_LABEL_ALIGN_THRESH` | — | Branch-label alignment threshold; init:1551 | LOW |
| `local_core_dump_directory` | `+168` | `const char *` | `NEURON_RT_LOCAL_CORE_DUMP_DIRECTORY` | **`"/tmp/neuron-core-dump/dt-%d-cid-%c"`** | Local core-dump dir template; init:335–345 | MED |
| `s3_core_dump_prefix` | `+176` | `const char *` | `NEURON_RT_S3_CORE_DUMP_PREFIX` | NULL | S3 core-dump prefix; `getenv`→`strdup` (init:358) | MED |
| `funtime` | `+184` | `bool` | `NEURON_RT_FAKE_INSTANCE_TYPE` | — (mirror) | **gconf mirror of `cfg.funtime`** (init:286); read as `nrt_gconf()->funtime`; 16 consumers | HIGH |
| `stochastic_rounding_seed` | `+188` | `uint32_t` | `NEURON_RT_STOCHASTIC_ROUNDING_SEED` | — | SR seed; init:420 | MED |
| `stochastic_rounding_en` | `+192` | `bool` | `NEURON_RT_STOCHASTIC_ROUNDING_EN` | — | SR enable; init:429 | MED |
| `dbg_cc_stochastic_rounding_en` | `+193` | `bool` | `NEURON_RT_DBG_CC_STOCHASTIC_ROUNDING_EN` | — | CC SR enable; init:442 | LOW |
| `enable_replica_group_validation` | `+194` | `bool` | `NEURON_RT_ENABLE_RG_VALIDATION` | — | Replica-group validation; init:765 | MED |
| `dbg_disable_rmv_dst_routing` | `+195` | `bool` | `NEURON_RT_DBG_DISABLE_RMV_DST_ROUTE` | — | Disable RMV dst routing; init:770; consumer `enc_context::check_replica_groups…@0xfbcf0` | MED |
| `embedded_sem_en` | `+196` | `bool` | `NEURON_RT_EMBEDDED_SEM_ENB` | **0** | Embedded semaphore enable; init:1555 | LOW |
| `dbg_topsp_event_include_default_algo` | `+197` | `bool` | `NEURON_RT_DBG_TOPSP_SYSTRACE_INCLUDE_DEFAULT_ALGO` | — | Include default algo in TopSP systrace; init:793 | LOW |
| `numerical_errors_verbosity` | `+200` | `numerical_error_notification_verbosity_t` | `NEURON_RT_NUMERICAL_ERRORS_VERBOSITY` | — | Numerical-error verbosity; forced `>=1` if `fail_on_nan`; init:757,1685 | MED |
| `test_zerocopy` | `+204` | `bool` | `NEURON_RT_DBG_ZEROCOPY` | — | Zero-copy test path; init:456; 2 consumers | LOW |
| `direct_bar4_write_size` | `+208` | `uint32_t` | `NEURON_RT_DBG_DIRECT_BAR4_WRITE_SIZE` | **0x1000 if arch>2, else 0** | Direct BAR4 write size; init:467–481 | MED |
| `log_coalescing_enabled` | `+212` | `bool` | `NEURON_RT_LOG_COALESCING_ENABLED` | — | Log coalescing; read by `nlog_init` in `nrt_init` | MED |
| `ndebug_stream_evts_ring_size` | `+216` | `size_t` | `NEURON_RT_DEBUG_STREAM_BUFFER_SIZE` | **0x40000** | Debug-stream event ring size; **po2-validated**, ERROR otherwise (init:1559,1565) | MED |
| `disable_bg_xpose` | `+224` | `bool` | `NEURON_RT_DISABLE_BG_XPOSE` | **0** | Disable background transpose; init:1494 | LOW |
| `instr_fetch_h2d_bitmap` | `+228` | `uint32_t` | `NEURON_RT_INSTR_FETCH_ON_H2D` | **= (arch>2)** | Instruction-fetch-on-H2D bitmap; init:1369 | MED |
| `v4_plus` | `+232` | `uint32_t` | `NEURON_RT_DBG_V4_PLUS` | **0** | v4+ arch path enable; gates `nrt_init` arch==4 (nrt_init:283); init:1463; 3 consumers | HIGH |
| `default_fp8_cfg` | `+236` | `kbin_fp8_conv_cfg_t` (16 B) | — (seeded) | (see §3) | FP8 conversion config aggregate; seeded init:1490–1493 | MED |
| `fp8_range_ext` | `+252` | `bool` | `NEURON_RT_DBG_FP8_RANGE_EXT` | **1** | FP8 range extension; init:1466 | MED |
| `ocp_fp8_sat` | `+253` | `bool` | `NEURON_RT_DBG_OCP_FP8_SAT` | **1** | OCP FP8 saturation; init:1478 | MED |
| `fp8_sns` | `+254` | `bool` | `NEURON_RT_DBG_FP8_SNS` | **0** | FP8 SNS; init:1482 | LOW |
| `fp8_sis` | `+255` | `bool` | `NEURON_RT_DBG_FP8_SIS` | **0** | FP8 SIS; init:1486 | LOW |
| `fp8_emax_e4m3` | `+256` | `uint32_t` | `NEURON_RT_DBG_FP8_EMAX_E4M3` | **8** | FP8 E4M3 emax; init:1470 | MED |
| `fp8_emax_e5m2` | `+260` | `uint32_t` | `NEURON_RT_DBG_FP8_EMAX_E5M2` | **0xF** | FP8 E5M2 emax; init:1474 | MED |
| `dbg_mesh_ch_buf_size` | `+264` | `size_t` | `NEURON_RT_DBG_MESH_CHANNEL_BUFFER_SIZE` | — | Mesh channel buffer size; init:590 | MED |
| `dbg_intra_rdh_ch_buf_size` | `+272` | `size_t` | `NEURON_RT_DBG_INTRA_RDH_CHANNEL_BUFFER_SIZE` | **40 MB (80 MB if arch==4)** | Intra-RDH channel buffer; init:595–599 | MED |
| `enable_host_cc` | `+280` | `bool` | — | — | Host-side collective enable; **no env in `init_config`**, set by collectives init | LOW |
| `dbg_enforce_rdh_global_handshake` | `+284` | `uint32_t` | `NEURON_RT_DBG_ENFORCE_RDH_GLOBAL_HANDSHAKE` | **0** | RDH global handshake; init:604 | LOW |
| `enable_model_switch_skip_tpb_config_optimization` | `+288` | `bool` | `NEURON_RT_DBG_MS_ENABLE_SKIP_TPB_CONFIG_OPT` | **1** | Skip TPB-config opt on model switch; init:1574; 3 consumers | MED |

Bytes `+289..+295` are tail padding to the 296 B (`0x128`) struct size.

> **CORRECTION (SCAN-01 / L-API — funtime, v4_plus, enable_p2p offsets) —** three gconf offsets were recorded inconsistently across earlier cells; the DWARF `structures.json` truth resolves them as follows, and every superseded value is wrong:
>
> | Field | **DWARF truth** | Superseded (wrong) | Where flagged |
> |---|---|---|---|
> | `funtime` | **`+184`** (`0xB8`) | `+176` | `api-lifecycle` flagged DWARF `+184` vs SCAN-01 `+176`; `api-tensors` read `@+0xB8` (= 184, correct) |
> | `v4_plus` | **`+232`** (`0xE8`) | `+228` | `api-lifecycle` flagged DWARF `+232` vs SCAN-01 `+228` |
> | `enable_p2p` | **`+116`** (`0x74`) | `+112` | `api-tensors` read `@+0x74` (= 116, correct) vs SCAN-01 decimal `+112` |
>
> The DWARF offsets `+184`/`+232`/`+116` are authoritative. Note `api-tensors`' hex reads (`0xB8 = 184`, `0x74 = 116`) already matched DWARF — only the SCAN-01 **decimal** values (`176`, `228`, `112`) were off by 8/4/4 respectively. The `0xB8`/`0x74` disassembly reads are confirmed correct; the SCAN-01 decimals are superseded.

> **NOTE —** `allow_legacy_neff` (`+0`) and `enable_host_cc` (`+280`) are gconf members that `nrt_config_parse_init_config` does **not** parse. `allow_legacy_neff` is read from `NEURON_RT_ALLOW_LEGACY_NEFF` later, in the NEFF load path (`kelf::load @0x497dc0` / `kelf_load_from_neff @0x4c0870`); `enable_host_cc` is set programmatically by the collectives init path. Their values are therefore 0 (the `calloc` zero-fill) until that later code runs — a reimplementer that expects `init_config` to populate them will read a spurious 0.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_config_parse_init_config` | `0x8a1d0` | 23088 | Allocates (`calloc 0x128`), fills, and publishes (`ngc=`) the gconf | HIGH |
| `nrt_gconf` | `0x82670` | — | `return ngc;` — the 296 B struct accessor | CERTAIN |
| `nrt_config_free` | `0x82680` | — | `free(ngc); ngc=0` (teardown) | HIGH |
| `nrt_config_parse_numerical_error_verbosity` | `0x81cb0` | — | `NEURON_RT_NUMERICAL_ERRORS_VERBOSITY` → `numerical_errors_verbosity` (`+200`) | MED |
| `kelf::load` / `kelf_load_from_neff` | `0x497dc0` / `0x4c0870` | — | `NEURON_RT_ALLOW_LEGACY_NEFF` → `allow_legacy_neff` (`+0`) in load path | HIGH |

---

## 3. Embedded Sub-Structs and Enums

The two aggregates above contain three nested types whose layouts a reimplementer must also reproduce. All offsets are DWARF.

### `enc_proxy_histogram_config_t` (168 B, `cfg+480`)

Parsed by `get_enc_proxy_histogram_config @0x847f0` from a 5-token string grammar (`bucket_usecs num_buckets per_neff_warmup warmup output_path`), `istringstream`-split via `std::getline`; throws `length_error "Output path too long"` if the path exceeds 127 bytes.

| Field | Offset | Type | Meaning | Conf |
|---|---|---|---|---|
| `enable` | `+0` | `bool` | Histogram capture enabled | HIGH |
| `bucket_usecs` | `+8` | `size_t` | Bucket width in microseconds | HIGH |
| `num_buckets` | `+16` | `size_t` | Histogram bucket count | HIGH |
| `per_neff_warmup` | `+24` | `size_t` | Per-NEFF warmup samples | HIGH |
| `warmup` | `+32` | `size_t` | Global warmup samples | HIGH |
| `output_path` | `+40` | `char[128]` | Output file path (cap 127 + NUL) | HIGH |

### `kbin_fp8_conv_cfg_t` (16 B, `gconf+236`)

The FP8-conversion aggregate seeded at init:1490–1493. Its scalar fields **mirror** the flat gconf FP8 fields at `+252..+260` (`fp8_range_ext`, `fp8_emax_e4m3`, `fp8_emax_e5m2`, `ocp_fp8_sat`, `fp8_sis`, `fp8_sns`); the aggregate is the bundled copy passed to the KBIN conversion path.

| Field | Offset (within struct) | Type | Meaning | Conf |
|---|---|---|---|---|
| `fp8_range_extension` | `+0` | `bool` | Range-extension flag | MED |
| `emax_fp8_e4m3` | `+4` | `uint32_t` | E4M3 emax (init seeds **8**) | MED |
| `emax_fp8_e5m2` | `+8` | `uint32_t` | E5M2 emax (init seeds **15**) | MED |
| `ocp_sat` | `+12` | `bool` | OCP saturation | MED |
| `sis` | `+13` | `bool` | SIS flag | MED |
| `sns` | `+14` | `bool` | SNS flag | MED |

### `numerical_error_notification_verbosity_t` (4 B, `gconf+200`)

| Value | Name |
|---|---|
| 0 | `NONE` |
| 1 | `CRITICAL` |
| 2 | `DEBUG` |
| 3 | `VERBOSE` |

> **NOTE —** `numerical_errors_verbosity` is force-raised to at least `CRITICAL` (1) when `cfg.fail_on_nan` (`+252` of `nrt_config_t`) is set (init:1685) — a fail-on-NaN run cannot also silence numerical-error reporting. This is one of several cross-struct couplings (the other principal one being `funtime`, which is set in `nrt_config_t+337` and mirrored to `nrt_global_config_t+184`).

---

## Related Components

| Name | Relationship |
|---|---|
| `nrt_config_parse_init_config` (`0x8a1d0`) | The single env parser that fills both structs in one pass; owns the env→field bindings |
| `nrt_gconf` (`0x82670`) / `ngc` (`0xc5c460`) | Accessor + pointer for the 296 B heap struct |
| `nrt_init` (`0x94e90`) | Sole caller of the parser; copies ~50 `nrt_config_0` fields into a stack `tpb_init_config_t` |
| `nrt_config_free` (`0x82680`) | Frees `ngc` at teardown (`nrt_close`) |
| `cached_info` (`0xc5c710`) | The next `.bss` symbol after `nrt_config`; its address pins the 656 B span |

## Cross-References

- [Environment Variable Catalog (NEURON_RT_*)](env-vars.md) — the exhaustive 138-name env table; this page is the destination-field side of that mapping
- [Public C API: Device, Config and Env Parsing](api-device-config.md) — the parse *mechanism* (typed `nrt_config_parse` helpers, getenv→field pattern) that fills these structs
- [Public C API: Lifecycle and Init/Teardown](api-lifecycle.md) — `nrt_init`'s call into the parser and the `funtime`/`v4_plus`/`virtual_core_size` fields the bring-up reads (offset reconciliation source)
- [Public C API: Tensor Surface and I/O](api-tensors.md) — the tensor entries gated by `gconf.enable_p2p` (`+116`) and `gconf.funtime` (`+184`) (offset reconciliation source)
