# Environment Variable Catalog (NEURON_RT_*)

> *All addresses, offsets, defaults, and env-var spellings on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries full DWARF v4 + a 24,864-entry `.symtab`; every variable name is read verbatim from NUL-terminated `.rodata` tokens and every destination field is the DWARF struct member the parse callsite stores into. All `PT_LOAD` segments are identity-mapped (`.text`/`.rodata`/`.bss` VMA == file offset). All addresses are analysis VMAs. Other versions will differ.*
>
> *Part IV — Userspace Runtime Core / REFERENCE catalogue · **Evidence grade:** **Confirmed (getenv→field map)** — names are verbatim `.rodata`; destination fields are the `lea OFF(%base),%rdx` argument joined to the DWARF layout of the [two config structs](config-structs.md); defaults are the inline 2nd-argument immediate of the typed `nrt_config_parse` helper where recovered. Rows whose setter is outside `nrt_config_parse_init_config` are confidence-tagged. · [back to index](../index.md)*

## Abstract

This page is the exhaustive control surface of the Neuron Runtime: every `NEURON_RT_*` and genuine `NEURON_*` environment variable the runtime recognizes, anchored to the `getenv` callsite that reads it and the config-struct field it writes. The runtime ingests its entire environment **once**, at `nrt_init` time, inside one master parser — `nrt_config_parse_init_config @0x8a1d0` — and freezes the result for process lifetime. The familiar reference frame is a classic parse-once config block (the shape of an OpenMP `OMP_*` scan or a CUDA `CUDA_*` ingest), with one structural wrinkle a reimplementer must reproduce: the destination is **two distinct structs**, and only the base register of the destination `lea` distinguishes them. The [config-structs](config-structs.md) page owns the field-by-field layout of both aggregates; this page owns the **name → callsite → field** direction of the same mapping.

The whole 123 MB object imports exactly three libc env primitives: `getenv@GLIBC_2.2.5` (PLT `0x3cfd0`, 43 call sites), `secure_getenv@GLIBC_2.17` (PLT `0x3d9d0`, 4 sites — all in vendored `std::filesystem::temp_directory_path`, **not** Neuron), and a Rust `std::sys::env::unix::getenv` closure (1 site, in the `neuron_rustime` bridge). Almost no `NEURON_RT_*` var is read at a bare `getenv` site; instead the master parser builds the variable **name** as a `std::string` from a `.rodata` literal, supplies the **default** as the helper's 2nd argument, and passes `&dest` (the 3rd argument, `%rdx`) pointing into one of the two structs — a typed `nrt_config_parse(name, default, &dest)` helper then does `getenv(name)`, falls back to the default on miss, and parses into `*dest`. The destination is either the 656 B static singleton **`nrt_config`** (`nrt_config_t @.bss 0xc5c480`, written `nrt_config_0.<field>`) or the 296 B heap **`nrt_global_config`** (`nrt_global_config_t`, `*ngc @.bss 0xc5c460`, read everywhere through `nrt_gconf() @0x82670`).

A NUL-split `.rodata` token sweep, cross-checked against the standalone string table, enumerates **138 distinct `NEURON_RT_*` tokens** (134 unique vars + the 4 `LOG_LEVEL`/`LOG_LEVEL_`/`LOG_LOCATION`/`LOG_LOCATION_` plain/per-component forms) and **9 genuine non-RT `NEURON_*`** vars. The remaining ~1,540 `NEURON_*` tokens are **enum-value constants** (`NEURON_ISA_*`, `NEURON_MEMALLOC_*`, `NEURON_DM_*`, `NEURON_DRIVER_FEATURE_*`, `NEURON_POD_*`), not env vars, and are excluded. This is a **reference catalogue**, so it takes the deliberate style-guide exception to the 40-row table rule: the catalogue table below is the body of the page, every row carrying name, type, default, destination field (struct + DWARF offset), effect, and a confidence tag.

## Reimplementation Contract

- **One parser, one pass, two structs.** All init-time ingestion is `nrt_config_parse_init_config @0x8a1d0`. It `calloc(0x128, 1)`s the gconf, fills both structs field-by-field from the environment, then publishes the gconf via `ngc = <ptr>`. Reproduce the *base-register* discriminator: a `lea OFF(%rax),%rdx` where `%rax = nrt_config @0xc5c480` writes the **656 B static** struct (notated `cfg.X` below); a `lea OFF(%rcx|%r14),%rdx` where the base is the calloc'd gconf writes the **296 B heap** struct (notated `gcfg.X`). Same offset, different struct — do not merge them.
- **The default is the helper's 2nd argument.** For every typed parse, the parse-time default is the literal immediate in `%esi`/`%rsi` at the call. Where that column reads `—`, the default is sourced from a struct constant, a prior value, or an arch-dependent branch (called out per-row), not an inline immediate.
- **A handful of vars bypass the typed family.** `LOG_LEVEL`/`LOG_LOCATION` (and their `_`-suffix per-component forms), `FAKE_INSTANCE_TYPE`, `VIRTUAL_CORE_SIZE`, the two `[NEW]` vars, and the six `FP8_*` vars at load time use **direct `getenv`** in their own functions; these rows name that function instead of an `init` line.
- **Some recognized vars are not parsed in `init_config`.** The `[MED]`-tagged rows (`NINFER`, `EXEC_TIMEOUT`, the `*_BOUND_CHECK` pair, `INST_VALIDITY_CHECK`, `ONE_TMPBUF_ENABLE`, `IO_RING_CACHE_SIZE`, …) are recognized names whose `getenv` lives in the execute/translate/load path (other cells' scope); their subsystem is known but the exact callsite/default is not owned here. A reimplementer must wire them, but their precise predicates belong to the load/exec pages.

| | |
|---|---|
| **Master parser** | `nrt_config_parse_init_config` `@0x8a1d0` (23,088 B) — builds name strings, fills both structs |
| **`getenv` PLT** | `getenv@GLIBC_2.2.5` `@0x3cfd0` (43 Neuron call sites) |
| **`secure_getenv` PLT** | `@0x3d9d0` (4 sites — vendored `std::filesystem`, **not** Neuron) |
| **Static struct (`cfg`)** | `nrt_config` `@.bss 0xc5c480` — `nrt_config_t`, **656 B**, read `nrt_config_0.<field>` |
| **Heap struct (`gcfg`)** | `nrt_global_config_t`, **296 B**, `*ngc @.bss 0xc5c460`, read `nrt_gconf()->field` |
| **gconf accessor** | `nrt_gconf` `@0x82670` (`return ngc;`) |
| **Recognized tokens** | 138 `NEURON_RT_*` (134 unique + 4 log forms) + 9 genuine `NEURON_*` |
| **Excluded** | ~1,540 `NEURON_*` enum-value constants (`NEURON_ISA_*` etc.) — not env vars |

> **CORRECTION (NX-023 — single-struct model) —** an earlier scan recorded a **single** config object and placed the FP8 fields "in the config struct" at `+252..+260`. In truth those FP8 fields live in `nrt_global_config` (`gcfg`), and `nrt_config_t+252` is `fail_on_nan` — the two structs were conflated. `nrt_gconf() @0x82670` returns the **296 B `ngc`**, not the 656 B static singleton; 117 consumer sites read `nrt_gconf()->field`. Confirmed by the DGE consumer `translate_one_pseudo_dge_instr_v2 @0x322c40` (`call nrt_gconf; and 0x1c(%rax)` = `gcfg.dbg_disable_dge+28`; `cmpb $0,0x24(%rax)` = `gcfg.dbg_vector_dge_skip_notif+36`). See [config-structs](config-structs.md) for the full two-struct reconciliation.

---

## At a Glance — the Ingestion Shape

Before the catalogue, the three ingestion mechanisms a reimplementer must reproduce. Every catalogue row uses one of these.

| Mechanism | How the value is read | Vars using it |
|---|---|---|
| **Typed parse family** | `nrt_config_parse(name, default, &dest)` — `getenv` inside the helper; default = 2nd arg; `&dest` = `%rdx` into `cfg`/`gcfg` | ~99 vars in `init_config` (the bulk) |
| **Direct `getenv`** | A named orchestrator calls `getenv(name)` itself and parses with its own grammar | `LOG_LEVEL`/`LOCATION` (+`_` forms), `FAKE_INSTANCE_TYPE`, `VIRTUAL_CORE_SIZE`, FP8 re-read, `COMPRESS_RG` `[NEW]`, `PROCESS_TAG` `[NEW]` |
| **Out-of-`init_config`** | `getenv` in the execute / translate / NEFF-load path; recognized name, setter not owned here | `[MED]`-tagged rows |

The typed family (all take a `std::string name`, `getenv` inside):

| Helper | Address | Parses to |
|---|---|---|
| `nrt_config_parse(string, long, long&)` | `0x7ff20` | signed int |
| `nrt_config_parse(string, uint, uint&)` | `0x81120` | unsigned int (most common) |
| `nrt_config_parse(string, ulong, ulong&)` | `0x813d0` | unsigned long |
| `nrt_config_parse(string, bool, bool&)` | `0x81a90` | bool/flag (most common) |
| `nrt_config_parse(string, char*, char*&)` | `0x80720` | C-string/path (`strdup`'d) |
| `nrt_config_parse_number_range<uint,true>(string, vector&)` | `0x91040` | uint range/list |
| `nrt_config_parse_numerical_error_verbosity` | `0x81cb0` | verbosity enum |
| `parse_hbm_scrub_init_val` | `0x815e0` | `hbm_scrub_init_val` + `scrub_hbm` |
| `parse_cc_alg_types` | `0x89680` | `cc_allowed_alg_types` bitmap |
| `parse_vnc_config` | `0x83b40` | `virtual_core_size` (direct `getenv`) |
| `parse_visible_virtual_cores` | `0x85170` | `visible_virtual_cores` vector |
| `parse_low_latency_tasks_cpu_affinity` | `0x85c70` | `cpu_set_t` mask |
| `get_enc_proxy_histogram_config` | `0x847f0` | `cc_proxy_histogram_config` sub-struct |

---

## The Catalogue

Reference-catalogue table (style-guide exception to the 40-row limit). Notation: **`cfg.X`** = `nrt_config_t` field at `nrt_config @0xc5c480` (656 B static); **`gcfg.X`** = `nrt_global_config_t` field at `*ngc` (296 B heap, via `nrt_gconf()`). The **Config Field** column gives the field name and its DWARF offset, consistent with [config-structs](config-structs.md). **Default** is the inline 2nd-argument immediate where recovered; `—` ⇒ sourced from a struct constant / prior value / arch branch. **Effect** notes the consumer behavior; the parse callsite address is the `getenv`-bearing helper invocation. Rows are grouped by subsystem.

### A — Core Visibility / Topology / Instance

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_NUM_CORES` | uint | **0** | `cfg.num_cores +12` | Requested logical-core count (0 ⇒ whole device); parse `@0x8a74c` | HIGH |
| `NEURON_RT_VISIBLE_CORES` | range/list | — | `cfg.visible_virtual_cores +16` | Visible-LNC list; `parse_visible_virtual_cores @0x8a845` | HIGH |
| `NEURON_RT_VIRTUAL_CORE_SIZE` | uint | — | `cfg.virtual_core_size +8` | LNC fusion size; `parse_vnc_config` direct `getenv @0x83ce7` | HIGH |
| `NEURON_LOGICAL_NC_CONFIG` | string | **2** | `cfg.virtual_core_size +8` | Logical-NC topology; `parse_vnc_config @0x8a683` | HIGH |
| `NEURON_RT_NINFER` | uint | — | (vnc inflight) | Per-vNC inflight count; `nrt_get_visible_vnc_count`, not in `init_config` | MED |
| `NEURON_RT_RESET_CORES` | bool | **1** | `cfg.reset_cores +64` | Reset cores on init; parse `@0x8acbd` | HIGH |
| `NEURON_RT_FAKE_INSTANCE_TYPE` | string | — | `cfg.fake_family +340` / `cfg.fake_size +344` / `cfg.funtime +337` | Simulation/"funtime" mode; `nrt_config_parse_funtime @0x8379f`, mirrored to `gcfg.funtime +184` | HIGH |
| `NEURON_RT_ULTRASERVER_MODE` | enum | **0** | `gcfg.ultraserver_mode +40` | UltraServer topology mode; parse `@0x8d634` | HIGH |
| `NEURON_RT_ULTRASERVER_SINGLE_NODE` | bool | — | `gcfg.ultraserver_mode +40` | Forces `ultraserver_mode=4`; direct `getenv` in init path | MED |
| `NEURON_DEVICE_COUNT` | uint | — | (device-count override) | Device-count override; init-path direct `getenv` | MED |
| `NEURON_SIM_DEBUG_FLAGS` | uint | **`'U'` (0x55)** | `cfg.sim_debug_flags +48` | Simulator debug bitfield; parse `@0x8abea` | LOW |

### B — DGE / Instruction-Validity / Bounds (Security-Relevant)

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_DBG_DISABLE_DGE` | uint | **0** | `gcfg.dbg_disable_dge +28` | Per-engine bitmask; **`0xFF` disables DMA Generation Engine** on all TPB engines → software-descriptor path; parse `@0x8d397` | HIGH |
| `NEURON_RT_DBG_DISABLE_DGE_BOUND_CHECK` | uint | **0** | `gcfg.dge_disable_bound_check +32` | **Removes index/address bounds check around gather/scatter DGE descriptors** — a NEFF out-of-range index can drive an OOB device DMA; parse `@0x8d47d` | HIGH |
| `NEURON_RT_DBG_VECTOR_DGE_SKIP_NOTIF` | bool | **0** | `gcfg.dbg_vector_dge_skip_notif +36` | Skip vector-DGE completion notif; parse `@0x8d55c` | MED |
| `NEURON_RT_ENABLE_DGE_NOTIFICATIONS` | bool | — | (model param) | Enable DGE notifications; model-param path, not `init_config` | MED |
| `NEURON_RT_DBG_INST_VALIDITY_CHECK` | bool | enabled | (sequencer param) | Per-instruction validity check; translate path | MED |
| `NEURON_RT_DBG_INDIRECT_MEMCPY_BOUND_CHECK` | bool | — | (translate param) | Indirect-memcpy range gate; translate path | MED |
| `NEURON_RT_DBG_EMBEDDING_UPDATE_BOUND_CHECK` | bool | — | (translate param) | Embedding-update range gate; translate path | MED |
| `NEURON_RT_DBG_SB_MEMSET` | bool | — | (state-buffer debug) | State-buffer memset debug; execute path | MED |

### C — DMA / Descriptor / Prefetch

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_DBG_DMA_PACKETIZATION_SIZE` | uint | **0x1000** (cap `0xFFFFF`) | `cfg.dma_packetization_size +212` | DMA packet size, range-checked; parse `@0x8dfba` | HIGH |
| `NEURON_RT_DBG_DMA_PRIO_PKT_SIZE` | uint[5] | — | `gcfg.dma_prio_pkt_size +44` | Per-priority DMA packet sizes; number-range parse in `init_config` | MED |
| `NEURON_RT_DISABLE_PACK_DESCRIPTORS` | bool | **0** | `cfg.v2_disable_pack_descs +208` | Disable descriptor packing; parse `@0x8e044` | LOW |
| `NEURON_RT_DBG_M2S_PREFETCH_THRESHOLD` | uint | **0** | `cfg.m2s_prefetch_threshold +328` | Mem→stream prefetch threshold; parse `@0x8e48f` | LOW |
| `NEURON_RT_DBG_S2M_PREFETCH_THRESHOLD` | uint | **0x77 (119)** | `cfg.s2m_prefetch_threshold +332` | Stream→mem prefetch threshold; parse `@0x8e4eb` | LOW |
| `NEURON_RT_DBG_DIRECT_BAR4_WRITE_SIZE` | uint | **0x1000 if arch>2, else 0** | `gcfg.direct_bar4_write_size +208` | Direct BAR4 write size; parse `@0x8ec29` | MED |
| `NEURON_RT_INSTR_FETCH_ON_H2D` | bitmap/bool | **= (arch>2)** | `gcfg.instr_fetch_h2d_bitmap +228` | Instruction-fetch-on-H2D; number-range + bool, parse `@0x8da92` / `@0x8e20c` | MED |
| `NEURON_RT_ENABLE_DMA_TRACING` | bool | — | (DMA tracing flag) | DMA tracing; init/inspect path | MED |
| `NEURON_RT_DBG_DISABLE_RMV_DST_ROUTING` | bool | — | `gcfg.dbg_disable_rmv_dst_routing +195` | Disable RMV dst routing; parse `@0x8c8f1`; legacy alias `…_RMV_DST_ROUTE` also present | MED |
| `NEURON_RT_IO_RING_CACHE_SIZE` | uint | — | (io_ring_cache_size) | I/O ring cache size; execute/load path | MED |
| `NEURON_RT_ERROR_NQ_COALESCE` | bool | **0** | `cfg.notif_error_nq_coalesce +94` | Coalesce error-NQ entries; parse `@0x8cfdd` | LOW |
| `NEURON_RT_DBG_ZEROCOPY` | bool | **0** | `gcfg.test_zerocopy +204` | Zero-copy test path; parse `@0x8b03b` | LOW |

### D — Collectives (CC / RDH / Mesh / Proxy)

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_CC_ALG_TYPES` | bitmap | — | `cfg.cc_allowed_alg_types +200` | CC algorithm-type bitmap; `parse_cc_alg_types @0x8c4ad` | HIGH |
| `NEURON_RT_CC_CHANNEL_BUFFER_N` | uint | **8** | `cfg.cc_nr_channel_chunks +192` | CC channel-buffer chunk count; parse `@0x8c368` | MED |
| `NEURON_RT_CC_CHUNK_SIZE` | uint | **0x200000 (2 MB)** | `cfg.cc_chunk_size +196` | CC chunk size; parse `@0x8c3e3` | MED |
| `NEURON_RT_DBG_CC_CHECK_SIGS` | bool | **1** | `cfg.dbg_cc_check_sigs +92` | CC signature checks; parse `@0x8b665` | MED |
| `NEURON_RT_DBG_CC_DMA_PACKET_SIZE` | int-list | — | `cfg.dbg_cc_dma_packet_size +168` | Per-stream CC DMA packet sizes; parse `@0x8c2ed`; clamped to 16 in `nrt_init` | MED |
| `NEURON_RT_DBG_CC_DMA_COPY_DMA_PRIO_PKT_SIZE` | uint[5] | — | `gcfg.cc_dma_copy_prio_pkt_size +64` | CC-copy DMA priority sizes (overrides PACKET_SIZE); `init_config` | MED |
| `NEURON_RT_DBG_CC_CCE_REDUCE_DMA_PRIO_PKT_SIZE` | uint[5] | — | `gcfg.cc_cce_reduce_prio_pkt_size +84` | CC-reduce DMA priority sizes; `init_config` | MED |
| `NEURON_RT_DBG_CC_FORCE_SINGLE_RANK_RG` | bool | **0** | `cfg.dbg_cc_force_single_rank_rg +91` | Force single-rank replica group (WARN, UB); parse `@0x8b5ed` | HIGH |
| `NEURON_RT_DBG_CC_INTER_MESH_MAX_NODES` | uint | **0x10 (16)** | `cfg.dbg_cc_inter_mesh_max_node_n +164` | Inter-mesh max nodes; parse `@0x8c25a` | LOW |
| `NEURON_RT_DBG_CC_NOP` | bool | **0** | `cfg.dbg_cc_nop +90` | Collectives → NOP (WARN, UB); parse `@0x8b578` | HIGH |
| `NEURON_RT_DBG_CC_PROXY_HISTOGRAM_CONFIG` | struct | — | `cfg.cc_proxy_histogram_config +480` | Proxy histogram profiling (5-token grammar); `get_enc_proxy_histogram_config @0x8e5e6` | MED |
| `NEURON_RT_DBG_CC_SORT_RG` | bool | **0** | `cfg.dbg_cc_sort_rg +650` | Sort replica groups; parse `@0x8ef46` | LOW |
| `NEURON_RT_DBG_CC_STREAM_MODE` | uint | **1** | `cfg.dbg_cc_stream_mode +136` | CC stream mode; parse `@0x8b92f` | MED |
| `NEURON_RT_DBG_HIERARCHICAL_CC` | uint | **0** | `cfg.dbg_hierarchical_cc_mode +104` | Hierarchical CC mode; parse `@0x8b7c7` | MED |
| `NEURON_RT_DBG_HIER_CC_PIPELINE` | uint | **0** | `cfg.dbg_hier_cc_pipeline +160` | Hierarchical CC pipelining; parse `@0x8c1df` | LOW |
| `NEURON_RT_DBG_HYBRID_RING_CC_EN` | bool | **1** | `cfg.dbg_hybrid_ring_cc_enabled +649` | Hybrid-ring CC enable; parse `@0x8ea07` | LOW |
| `NEURON_RT_DBG_KANGARING_CC` | uint | **0** | `cfg.dbg_kangaring_cc_mode +108` | Kangaring CC mode; parse `@0x8b83c` | MED |
| `NEURON_RT_DBG_MESH_CC` | bool | **1** | `cfg.dbg_mesh_cc_mode +140` | Mesh CC mode; parse `@0x8bd23` | MED |
| `NEURON_RT_DBG_MESH_CHANNEL_BUFFER_SIZE` | ulong | **0x800000 (8 MB)** | `gcfg.dbg_mesh_ch_buf_size +264` | Mesh channel buffer; parse `@0x8b9ad` | MED |
| `NEURON_RT_DBG_MESH_DOUBLE_BUF` | bool | **1** | `cfg.dbg_mesh_double_buffer +141` | Mesh double-buffering; parse `@0x8bd9e` | LOW |
| `NEURON_RT_DBG_RDH_CC` | uint | **1** | `cfg.dbg_rdh_cc_mode +116` | RDH CC mode; parse `@0x8b8b4` | MED |
| `NEURON_RT_DBG_RDH_DOUBLE_BUF` | bool | **0** | `cfg.dbg_rdh_double_buffer +142` | RDH double-buffering; parse `@0x8be16` | LOW |
| `NEURON_RT_DBG_INTRA_RDH_CHANNEL_BUFFER_SIZE` | ulong | **40 MB (80 MB arch4)** | `gcfg.dbg_intra_rdh_ch_buf_size +272` | Intra-RDH channel buffer; parse `@0x8ba4a` | MED |
| `NEURON_RT_DBG_INTER_RDH_CHANNEL_BUFFER_SIZE` | ulong | **0x2000000 (32 MB)** | `cfg.dbg_inter_rdh_ch_buf_size +120` | Inter-RDH channel buffer; parse `@0x8bb3d` | MED |
| `NEURON_RT_DBG_INTER_RDH_RECV_WINDOW_N` | uint | **0** | `cfg.dbg_inter_rdh_recv_window_n +128` | Inter-RDH recv window; parse `@0x8bbb5` | LOW |
| `NEURON_RT_DBG_ENFORCE_RDH_GLOBAL_HANDSHAKE` | uint | **0** | `gcfg.dbg_enforce_rdh_global_handshake +284` | RDH global handshake; parse `@0x8bac5` | LOW |
| `NEURON_RT_DBG_ENFORCE_MESH_GLOBAL_HANDSHAKE` | uint | **0** | `cfg.dbg_enforce_mesh_global_handshake +144` | Mesh global handshake; parse `@0x8be8e` | LOW |
| `NEURON_RT_DBG_SINGLE_CYCLE_RING_ALLR_CC` | uint | **2** | `cfg.dbg_single_cycle_ring_allr_mode +112` | Single-cycle ring all-reduce; parse `@0x8bc2d` | MED |
| `NEURON_RT_DBG_EARLY_COMPLETION` | bool | **1** | `cfg.dbg_proxy_early_completion +148` | Proxy early completion; parse `@0x8bf09` | MED |
| `NEURON_RT_DBG_EARLY_POSTING` | bool | **1** | `cfg.dbg_proxy_early_posting +149` | Proxy early posting; parse `@0x8bf84` | MED |
| `NEURON_RT_DBG_FORCE_2DEV_PROXY` | bool | **0** | `cfg.dbg_force_use_2dev_proxy +648` | Force 2-device proxy; parse `@0x8e9ab` | LOW |
| `NEURON_RT_RANKS_PER_NETWORK_PROXY` | uint | **1** | `cfg.ranks_per_network_proxy +152` | Ranks per network proxy; **`>1` requires HW exec barrier**; parse `@0x8bfff` | HIGH |
| `NEURON_RT_ROOT_COMM_ID` | string | NULL | `cfg.cc_root_comm_id +40` | Collective root comm id (required for collective NEFF); `nrt_cc_global_comm_init`, `getenv`→`strdup` | HIGH |
| `NEURON_RT_ENABLE_RG_VALIDATION` | bool | — | `gcfg.enable_replica_group_validation +194` | Replica-group validation; parse `@0x8c808` | MED |
| `NEURON_EXPERIMENTAL_COMPRESS_RG` `[NEW]` | bool | — | (replica-group compression) | RG compression; `enc_parse_replica_groups` direct `getenv @0x11ea54` | HIGH |

### E — Async Send/Recv & XU Compute (Experimental)

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS` | uint | **0** (clamp 63) | `gcfg.async_exec_max_inflight_requests +24` | Max in-flight async reqs; clamped if >63; parse `@0x8f10a` | HIGH |
| `NEURON_RT_ASYNC_SENDRECV_BOOTSTRAP_PORT` | uint | — | `gcfg.async_sr_boot_port +104` | Async send/recv bootstrap port; parse `@0x8cac2` | LOW |
| `NEURON_RT_ASYNC_SENDRECV_EXPERIMENTAL_ENABLED` | bool | — | `gcfg.async_sr_experimental_enabled +108` | Experimental async S/R enable; parse `@0x8cba6` | LOW |
| `NEURON_RT_XU_COMPUTE_MAX_QUEUED_REQUESTS` | ulong | **8** | (XU compute queue depth) | XU compute max queued reqs; parse `@0x8f0ad` | MED |

### F — FP8 Numerics / Stochastic Rounding

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_DBG_FP8_RANGE_EXT` | bool | **1** | `gcfg.fp8_range_ext +252` | FP8 range extension; parse `@0x8e6b7`; **re-read at load** `@0x4c33e5` | MED |
| `NEURON_RT_DBG_OCP_FP8_SAT` | bool | **1** | `gcfg.ocp_fp8_sat +253` | OCP FP8 saturation; parse `@0x8e7f5`; re-read `@0x4c3405` | MED |
| `NEURON_RT_DBG_FP8_SNS` | bool | **0** | `gcfg.fp8_sns +254` | FP8 SNS; parse `@0x8e85c`; re-read `@0x4c3425` | LOW |
| `NEURON_RT_DBG_FP8_SIS` | bool | **0** | `gcfg.fp8_sis +255` | FP8 SIS; parse `@0x8e8c3`; re-read `@0x4c3448` | LOW |
| `NEURON_RT_DBG_FP8_EMAX_E4M3` | uint | **8** | `gcfg.fp8_emax_e4m3 +256` | FP8 E4M3 emax; parse `@0x8e721`; re-read `@0x4c3462` | MED |
| `NEURON_RT_DBG_FP8_EMAX_E5M2` | uint | **0xF (15)** | `gcfg.fp8_emax_e5m2 +260` | FP8 E5M2 emax; parse `@0x8e78b`; re-read `@0x4c347c` | MED |
| `NEURON_RT_STOCHASTIC_ROUNDING_EN` | bool | — | `gcfg.stochastic_rounding_en +192` | Stochastic-rounding enable; parse `@0x8ae75` | MED |
| `NEURON_RT_STOCHASTIC_ROUNDING_SEED` | uint | — | `gcfg.stochastic_rounding_seed +188` | SR seed; parse `@0x8ad9a` | MED |
| `NEURON_RT_DBG_CC_STOCHASTIC_ROUNDING_EN` | bool | — | `gcfg.dbg_cc_stochastic_rounding_en +193` | CC SR enable; parse `@0x8af5e` | LOW |
| `NEURON_RT_NUMERICAL_ERRORS_VERBOSITY` | enum | — | `gcfg.numerical_errors_verbosity +200` | Numerical-error verbosity; forced `>=1` if `fail_on_nan`; `nrt_config_parse_numerical_error_verbosity @0x8c72d` | MED |
| `NEURON_RT_ENABLE_VERBOSE_NUMERICAL_ERRORS` | bool | — | (verbose numerical errors) | Verbose numerical errors; execute path | MED |

> **NOTE —** the six `NEURON_RT_DBG_FP8_*` vars are parsed once in `init_config` (defaults above) **and re-read by direct `getenv`** inside `kelf_load_from_neff @0x4c0870` (sites `0x4c33e5..0x4c347c`), where they **override the FP8 config baked into the NEFF's `def.json` at load time**. A reimplementer must honor both reads: the env value wins over the model file.

### G — Ucode / NEFF Loading / Arch Select

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_UCODE_LIB_PATH` | cstr | NULL | `cfg.neuron_ucode_path +224` | **GPSIMD microcode `dlopen` path** (attacker-controlled env ⇒ arbitrary `.so` load); `strdup`, parse `@0x8e0a8` | MED |
| `NEURON_RT_DBG_V4_PLUS` | uint | **0** | `gcfg.v4_plus +232` | v4+ arch path (mariana vs mariana_plus, ext-ISA); parse `@0x8e64d` | HIGH |
| `NEURON_RT_DBG_HW_DECODE_BINS_DIR` | cstr | NULL | `cfg.hw_decode_table_path +232` | HW-decode table dir; parse `@0x8e109` | MED |
| `NEURON_RT_DBG_HW_DECODE_TOGGLE` | uint | **arch: 4→11, 3→3, else 0** | `cfg.hw_decode_toggle +240` | HW-decode enable; forced 0 if strict-order; parse `@0x8e19b` | HIGH |
| `NEURON_RT_ALLOW_LEGACY_NEFF` | uint | **0** | `gcfg.allow_legacy_neff +0` | Allow legacy NEFF format; parse `@0x8a2a6`; **re-read in `kelf_load`** | HIGH |
| `NEURON_RT_VALIDATE_HASH` | bool | — | (NEFF hash validation) | NEFF hash validation; `kelf_load` path | MED |
| `NEURON_RT_DBG_SEQ_IRAM_BLOCK_SIZES_KB` | ulong[5] | — | `gcfg.seq_instr_block_size +120` | Per-engine IRAM block sizes (KB`<<10`); po2/IRAM-divisor validated; parse `@0x8ea60` | MED |

### H — Scratchpad / Tmpbuf / HBM / Memory

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_SCRATCHPAD_PAGE_SIZE` | ulong | — (value `<<20`) | `cfg.scratchpad_page_size +56` | Scratchpad page bytes; parse `@0x8b313` | HIGH |
| `NEURON_RT_ONE_TMPBUF_PAGE_SIZE_MB` | ulong | **0x200 (512)** | (one-tmpbuf page bytes) | One-tmpbuf page size; parse `@0x8b293` | MED |
| `NEURON_RT_ONE_TMPBUF_ENABLE` | bool | — | (one_tmpbuf enable) | One-tmpbuf enable; execute path | MED |
| `NEURON_RT_DBG_SCRATCHPAD_ON_SINGLE_CORE` | uint | **0** | `gcfg.scratchpad_on_single_core +16` | Force scratchpad onto one core; parse `@0x8b3a1` | MED |
| `NEURON_RT_DBG_EXPERIMENTAL_CONTIGUOUS_SCRATCHPAD` | uint | **0** | `gcfg.experimental_contiguous_scratchpad +112` | Contiguous scratchpad alloc; parse `@0x8b419` | LOW |
| `NEURON_RT_HBM_INIT_VAL` | uint | — | `cfg.hbm_scrub_init_val +272` / `cfg.scrub_hbm +276` | HBM scrub fill value + enable; `parse_hbm_scrub_init_val @0x8e436` | MED |
| `NEURON_RT_MAP_HBM` | bool | **0** | `cfg.map_hbm +336` | Map HBM into address space; parse `@0x8e544` | MED |
| `NEURON_RT_ENABLE_MEMORY_METRICS` | bool | **1** | `cfg.enable_memory_metrics +216` | Memory-metrics emit; parse `@0x8c581` | LOW |
| `NEURON_RT_DBG_VERBOSE_DEV_MEM` | bool | **0** | `cfg.dbg_verbose_log_dev_mem +93` | Verbose device-mem logging; parse `@0x8b6da` | LOW |
| `NEURON_RT_MIN_MEM_ERROR_INTERVAL` | long | **0xE10 (3600)** | `cfg.v2_min_ignore_nc_memory_error_interval +96` | v2 mem-error suppress window (s); parse `@0x8b752` | LOW |
| `NEURON_RT_DEBUG_MEMLOG_MAX_SIZE` | ulong | **0x100000 (1 MB)** | `cfg.mem_log_size +72` | Device-memory log ring size; parse `@0x8b132` | LOW |
| `NEURON_RT_DBG_VIRTUAL_SERVER_SIZE` | uint | **0** | `cfg.virtual_server_size +348` | Virtual-server core count; parse `@0x8e59d` | MED |

### I — Execution Barriers / Ordering / Sync

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_DISABLE_EXECUTION_BARRIER` | bool | **0** | `cfg.disable_execution_barrier +205` | Disable exec barrier; parse `@0x8d0d1` | MED |
| `NEURON_RT_ENABLE_HW_EXECUTION_BARRIER` | bool | **= switch_v1_family** | `cfg.enable_hw_execution_barrier +206` | HW exec barrier (arch-default); parse `@0x8d1df` | MED |
| `NEURON_RT_ENABLE_INTERNODE_EXECUTION_BARRIER` | bool | **1** | `cfg.enable_internode_execution_barrier +207` | Internode exec barrier; parse `@0x8d2c6` | MED |
| `NEURON_RT_DBG_FORCE_STRICT_ORDERING` | bool | **0** | `cfg.dbg_force_strict_ordering +88` | Strict ordering; forces `hw_decode_toggle=0`; parse `@0x8b48e` | MED |
| `NEURON_RT_EXEC_TIMEOUT` | — | — | (exec timeout) | Execution timeout; execute path | MED |
| `NEURON_RT_DBG_DROP_EVENTSEM_NOTIF` | bool | **1** (stored `!drop`) | `cfg.notif_full_evsem +95` | Drop event-sem notif; parse `@0x8ce9f` | LOW |
| `NEURON_RT_DBG_TOPSP_SEMAPHORES_CLEAR_CHECK` | bool | **0** | `cfg.dbg_topsp_semaphores_clear_check +157` | TopSP semaphore clear check; parse `@0x8c0ef` | LOW |
| `NEURON_RT_DBG_TOPSP_EVSEM_NOTIF_ENABLED` | bool | **0** | `cfg.dbg_topsp_evsem_notif_enabled +204` | TopSP event-sem notif enable; parse `@0x8c167` | LOW |
| `NEURON_RT_DBG_TOPSP_SYSTRACE_INCLUDE_DEFAULT_ALGO` | bool | — | `gcfg.dbg_topsp_event_include_default_algo +197` | Include default algo in TopSP systrace; parse `@0x8c9dc` | LOW |
| `NEURON_RT_EMBEDDED_SEM_ENB` | bool | **0** | `gcfg.embedded_sem_en +196` | Embedded semaphore enable; parse `@0x8ee6e` | LOW |
| `NEURON_RT_DBG_MS_ENABLE_SKIP_TPB_CONFIG_OPT` | bool | **1** | `gcfg.enable_model_switch_skip_tpb_config_optimization +288` | Skip TPB-config opt on model switch; parse `@0x8efb6` | MED |
| `NEURON_RT_DBG_BRANCH_LABEL_ALIGN_THRESH` | ulong | **0x10 (16)** | `gcfg.br_label_align_thresh +160` | Branch-label alignment threshold; parse `@0x8ee0a` | LOW |

### J — P2P / Misc HW / Threading

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_ENABLE_P2P` | bool | **1** | `gcfg.enable_p2p +116` | Peer-to-peer enable; gates `nrt_get_dmabuf_fd` export; parse `@0x8bca8` | HIGH |
| `NEURON_RT_DISABLE_BG_XPOSE` | bool | **0** | `gcfg.disable_bg_xpose +224` | Disable background transpose; parse `@0x8e952` | LOW |
| `NEURON_RT_GPSIMD_STDOUT_QUEUE_SIZE_BYTES` | uint | **0** | `cfg.pool_stdout_queue_size_bytes +132` | GPSIMD stdout pool bytes; parse `@0x8cc8c` | LOW |
| `NEURON_RT_ONE_THREAD_PER_CORE` | bool | **0** | `cfg.one_thread_per_core +156` | One worker thread per core; parse `@0x8c077` | MED |
| `NEURON_RT_LOW_LATENCY_TASKS_CPU_AFFINITY` | mask | — | `cfg.low_latency_tasks_cpu_affinity_mask +352` | Low-latency thread affinity; `parse_low_latency_tasks_cpu_affinity @0x8c654` | MED |
| `NEURON_PROCESS_TAG` `[NEW]` | string | — | (datastore process tag) | NDS datastore process tag; `tdrv_nds_save_process_info` direct `getenv @0x22f023` | HIGH |

### K — Logging / Tracing / Metrics

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_LOG_LEVEL` | enum | — | (nlog level) | Global nlog level; `nrt_config_log_level` direct `getenv @0x82140` | HIGH |
| `NEURON_RT_LOG_LEVEL_` | enum | — | (per-component nlog level) | Per-component level (component name appended at runtime); `nrt_config_apply_log_level @0x81515` | HIGH |
| `NEURON_RT_LOG_LOCATION` | string | — | (nlog sink) | Global nlog sink location; `nrt_config_log_level` direct `getenv` | HIGH |
| `NEURON_RT_LOG_LOCATION_` | string | — | (per-component sink) | Per-component sink; `nrt_config_apply_log_level` | HIGH |
| `NEURON_RT_LOG_COALESCING_ENABLED` | bool | — | `gcfg.log_coalescing_enabled +212` | Log coalescing; read by `nlog_init`; parse `@0x8a507` | MED |
| `NEURON_RT_TRACE_NUM_ENTRIES` | ulong | **0x100000 (1 M)** | `cfg.max_trace_entries +80` | Trace ring entry cap; parse `@0x8b218` | LOW |
| `NEURON_RT_DBG_IO_CRC` | bool | **0** | `cfg.dbg_io_crc +89` | CRC I/O tensors; parse `@0x8b503` | LOW |
| `NEURON_RT_DEBUG_STREAM_BUFFER_SIZE` | ulong | **0x40000 (256 K)** | `gcfg.ndebug_stream_evts_ring_size +216` | Debug-stream event ring size; **po2-validated**, ERROR otherwise; parse `@0x8eed5` | MED |

### L — Inspect / Profiling

Parsed by `nrt_config_parse_inspect_config @0x86760` (the `NEURON_RT_INSPECT_*` family) and the profiling orchestrators; destinations are the inspect-config aggregate, not `cfg`/`gcfg`.

| Var | Type | Default | Config Field | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_INSPECT_ENABLE` | bool | — | inspect_config | Master inspect enable | MED |
| `NEURON_RT_INSPECT_ON_FAIL` | bool | — | inspect_config | Capture-on-fail | MED |
| `NEURON_RT_INSPECT_OUTPUT_DIR` | string | — | inspect_config | Output directory | MED |
| `NEURON_RT_INSPECT_DEVICE_PROFILE` | bool | — | inspect_config | Device profile capture | MED |
| `NEURON_RT_INSPECT_SYSTEM_PROFILE` | bool | — | inspect_config | System profile capture | MED |
| `NEURON_RT_INSPECT_HOST_MEMORY` | bool | — | inspect_config | Host-memory capture | MED |
| `NEURON_RT_INSPECT_CPU_UTIL` | bool | — | inspect_config | CPU-util capture | MED |
| `NEURON_RT_INSPECT_EVENT_FILTER_NC` | list | — | inspect_config | Per-NC event filter | MED |
| `NEURON_RT_INSPECT_EVENT_FILTER_TYPE` | list | — | inspect_config | Event-type filter | MED |
| `NEURON_RT_INSPECT_SYS_TRACE_MAX_EVENTS_PER_NC` | uint | — | inspect_config | Max sys-trace events/NC | MED |
| `NEURON_RT_PROFILING_MODE` | enum | — | (profiling mode) | Profiling mode; parse `@0x8cd5f` | MED |
| `NEURON_RT_PROFILE_BUF_` | uint (prefix) | — | `cfg.profile_buf_sizes +280` | Per-notif-type buffer size; suffix appended at runtime; `nrt_config_profile_buf_size @0x828b0` | MED |

### M — Core Dumps / OOM / Failure Capture

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_LOCAL_CORE_DUMP_DIRECTORY` | cstr | **`"/tmp/neuron-core-dump/dt-%d-cid-%c"`** | `gcfg.local_core_dump_directory +168` | Local core-dump dir template; `init_config` | MED |
| `NEURON_RT_S3_CORE_DUMP_PREFIX` | cstr | NULL | `gcfg.s3_core_dump_prefix +176` | S3 core-dump prefix; `getenv`→`strdup` | MED |
| `NEURON_RT_OOM_DUMP_DIRECTORY` | cstr | **`"/tmp"`** | `gcfg.device_oom_dump_directory +8` | Device-OOM dump dir (path capped 200); first `getenv` in init | MED |
| `NEURON_RT_DBG_DUMP_INPUTS_ON_ERR` | long | **0** | `cfg.dump_inputs_on_err +256` | Dump inputs on error; parse `@0x8e387` | LOW |
| `NEURON_RT_DBG_INPUT_DUMP_DIRECTORY` | cstr | **`"/tmp/neuron-input-dump"`** | `cfg.input_dump_directory +264` | Input-dump dir (len-capped); parse `@0x8e3e9` | LOW |

### N — Genuine non-RT `NEURON_*`

| Var | Type | Default | Config Field (struct+offset) | Effect | Conf |
|---|---|---|---|---|---|
| `NEURON_NO_COMPILE_MODE` | bool | **0** | `cfg.enable_no_compile_mode +244` | No-compile (pre-built) mode; parse `@0x8e279` | LOW |
| `NEURON_NO_COMPILE_MODE_TIMEOUT` | uint | **0x78 (120)** | `cfg.no_compile_mode_timeout +248` | No-compile timeout (s); parse `@0x8e2d5` | LOW |
| `NEURON_FAIL_ON_NAN` | bool | **0** | `cfg.fail_on_nan +252` | Fail on NaN; forces `numerical_errors_verbosity>=1`; parse `@0x8e32e` | MED |

> **NOTE — experimental flags.** Four knobs are explicitly experimental and a reimplementer should treat their semantics as unstable across runtime minor versions. `NEURON_RT_ASYNC_SENDRECV_BOOTSTRAP_PORT` (`gcfg+104`) and `NEURON_RT_ASYNC_SENDRECV_EXPERIMENTAL_ENABLED` (`gcfg+108`) gate the experimental point-to-point async send/recv transport — the `EXPERIMENTAL_ENABLED` flag is the master gate, and the bootstrap port is meaningless unless it is set. `NEURON_RT_RANKS_PER_NETWORK_PROXY` (`cfg+152`, default **1**) carries an ordering precondition: a value `>1` **requires** the HW execution barrier (`enable_hw_execution_barrier`, `cfg+206`) — `init_config` aborts the combination of `ranks_per_network_proxy>1` with the HW barrier disabled. `NEURON_EXPERIMENTAL_COMPRESS_RG` `[NEW]` is read by direct `getenv` in `enc_parse_replica_groups @0x11ea54`, outside the master parser, and toggles replica-group compression in the collectives path.

> **GOTCHA — `secure_getenv` is a vendor red herring.** Four `secure_getenv` call sites exist, all inside vendored `std::filesystem::temp_directory_path`; none is a Neuron knob. A reimplementer scanning for the env surface by grepping `*getenv` PLT references must filter these out — and likewise exclude the ~1,540 `NEURON_*` **enum-value** constants (`NEURON_ISA_*`, `NEURON_MEMALLOC_*`, `NEURON_DM_*`, `NEURON_DRIVER_FEATURE_*`, `NEURON_POD_*`), which look like env vars by spelling but are never passed to `getenv`.

---

## Boundary Edges

A handful of recognized vars are owned by other subsystems; this page records the env→field write only, and the consumer semantics live elsewhere.

| Var(s) | Owning subsystem |
|---|---|
| DGE/validity/bounds consumer logic (`translate_one_pseudo_dge_instr_v2/v3`, `sequencer_setup_instr`) | [NEFF load / translate](../neff/load-pipeline.md) and [ISA pseudo-lowering](../isa/pseudo-instruction-lowering.md) |
| `INST_VALIDITY_CHECK`, `*_BOUND_CHECK` pair | model-build translate params (not in `cfg`/`gcfg`) → translate cells |
| `ALLOW_LEGACY_NEFF` gate + FP8 re-read (`kelf_load_from_neff @0x4c0870`) | [NEFF load pipeline](../neff/load-pipeline.md) |
| `DBG_V4_PLUS` ext-ISA consumption | [ext-ISA provider](../gpsimd/extisa-provider.md) |
| `PROCESS_TAG` `[NEW]` | [Neuron DataStore](../datastore/overview.md) (`tdrv_nds_save_process_info`) |
| `COMPRESS_RG` `[NEW]` | [collectives engine core](../collectives/engine-core.md) (`enc_parse_replica_groups`) |
| `LOG_LEVEL`/`LOCATION` enum decode + nlog wiring | [Logging (nlog)](logging.md) |

---

## Cross-References

- [Configuration: nrt_config and nrt_global_config](config-structs.md) — the destination-field side of this mapping: the full field-by-field layout of both `cfg` (656 B) and `gcfg` (296 B), with offsets this page's Config Field column is consistent with
- [Public C API: Device, Config and Env Parsing](api-device-config.md) — the parse *mechanism* (the typed `nrt_config_parse` helper family, the `getenv`→`&dest` `lea` pattern) that turns a name into a field write
- [Public C API: Lifecycle and Init/Teardown](api-lifecycle.md) — `nrt_init`'s single call into `nrt_config_parse_init_config`, where the whole environment is ingested once and frozen
