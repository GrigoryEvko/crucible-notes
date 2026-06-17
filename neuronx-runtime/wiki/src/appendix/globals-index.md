# Global / Singleton Address Index

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB **DYN** x86-64, NOT stripped, DWARF v4.
>
> **Part XVI — Appendices** / REFERENCE lookup index · every address/size below is the `nm -nS --defined-only` / `readelf -rW` value carried by the [Globals and Singletons Atlas](../forensics/globals-atlas.md); this page re-sorts those audited values by address for symbol→addr→page lookup.

## Abstract

This is the **flat, address-sorted lookup index** to `libnrt.so`'s writable global state. The companion [Globals and Singletons Atlas](../forensics/globals-atlas.md) is the explanatory map — it groups the same globals by kind (config singletons, per-arch ops vtables, dlopen handle tables, error/status globals, dispatch/jump tables) and carries the full *init-by* / *read-by* provenance and the reimplementation rationale. This page does the opposite job: it dumps every significant global into **one table sorted by ascending address**, so a reader who has an address from a disassembler — or a symbol name and wants its address, size, section, and owning page — gets the answer in a single scan. The atlas explains; this page indexes. The two share one set of `nm`-verified addresses by construction.

Every address here is read from this exact binary (`nm -nS --defined-only` for symbol/addr/size/section nibble, `readelf -rW` for the `.data.rel.ro` `R_X86_64_RELATIVE` relocations that wire each pointer slot) and is identical to the value the atlas carries; where the working notes and the atlas disagreed, the atlas's `nm`-verified value is authoritative and is the one indexed here (see the CORRECTION callout). The single structural fact that governs the table: the `.bss` symbols are **NOBITS** — zero on disk. The per-arch vtables (`kaena_khal`, `tdrv_arch_ops`, the `csr_regions_v*`) and the dlsym function-pointer stores have **no bytes in the file**; their slots exist only as *stores* emitted inside the lazily-run `register_*` / `*_init` / `ucode_init_module` installers. An address into one of those symbols is reconstructable from the installer's decompiled stores, never from a static hexdump.

| | |
|---|---|
| **Index scope** | every first-party global in the [Globals Atlas](../forensics/globals-atlas.md) + the per-arch RELRO seed tables, sorted by address |
| **Section bands** | `.data.rel.ro` `0xbf2d80`–`0xc05540` · `.data` `0xc07e00`–`0xc37c20` · `.bss` `0xc37c40`–`0xcb4300` |
| **`.bss` caveat** | NOBITS — vtable/store slots are zero on disk, filled by `register_*` installers at bring-up |
| **Provenance** | init-by / read-by per global lives in the [atlas](../forensics/globals-atlas.md); this page indexes, the atlas explains |
| **Section geometry** | [Binary Layout](../reference/binary-layout.md) — VMA==file-offset (delta 0) for all loadable sections |

---

## Section-band legend

The **Kind** column classifies each global by the section discipline established in [Binary Layout](../reference/binary-layout.md) and the [atlas](../forensics/globals-atlas.md). Three bands carry first-party state; read the band to know whether a structure is static-in-file or runtime-filled.

| Band | VMA range | Holds | On disk |
|---|---|---|---|
| `.data.rel.ro` | `0xbf2d80`–`0xc05540` (`0x127c0` B) | RELRO pointer/string tables: `ucode_func_symbols`, per-arch `*_hw_port_names` / `*_instr_block_caps` seeds, C++ vtables | present (relocated, then read-only) |
| `.data` | `0xc07e00`–`0xc37c20` (`0x2fe20` B) | const-valued dispatch tables: `nrt_init_state_strings`, queue-bundle alloc tables, `sequencer_error_subtypes_str`, `all_reduce_128b` | present (initialized) |
| `.bss` | `0xc37c40`–`0xcb4300` (`0x7c6c0` B) | mutable singletons, per-arch vtables, dlsym stores, per-engine arrays, locks, error-log ring | **NOBITS — zero** |

> **NOTE — this page indexes; the atlas explains.** The *init-by* (which constructor or `register_*` installer first writes a valid value) and *read-by* (the principal consumer) of every row below are owned by the [Globals and Singletons Atlas](../forensics/globals-atlas.md), grouped by kind. Do not re-derive that provenance from this table — it carries only the lookup keys (symbol, addr, size, section, kind) plus the owning page. Follow the atlas link for the installer chain and the per-global GOTCHA/QUIRK notes.

---

## Address-sorted master index

One row per significant first-party global, **sorted by ascending address**. `Section` is the `nm` band; `Kind` is the section-discipline class (RELRO seed / RELRO dlsym dir / const dispatch / mutable singleton / per-arch vtable / dlsym store / per-engine array / lock / handle). `Owning page` is the exact `SUMMARY` path that documents the global's semantics or per-slot geometry. `Conf` mirrors the atlas's confidence.

| Symbol | Address | Size | Section | Kind | Owning page | Conf |
|---|---|---|---|---|---|---|
| `ucode_func_symbols` | `0xbf2ea0` | `0x1e0` | `.data.rel.ro` | RELRO dlsym directory (`ucode_symbol_t[30]`) | [gpsimd/ucode-facade.md](../gpsimd/ucode-facade.md) | HIGH |
| `mariana_hw_port_names` | `0xbf30a0` | `0x68` | `.data.rel.ro` | RELRO per-arch port-name seed | [runtime/hal-adapter.md](../runtime/hal-adapter.md) | HIGH |
| `cayman_hw_port_names` | `0xbf3260` | `0x68` | `.data.rel.ro` | RELRO per-arch port-name seed | [runtime/hal-adapter.md](../runtime/hal-adapter.md) | HIGH |
| `sunda_hw_port_names` | `0xbf3420` | `0x68` | `.data.rel.ro` | RELRO per-arch port-name seed | [runtime/hal-adapter.md](../runtime/hal-adapter.md) | HIGH |
| `cayman_instr_block_caps` | `0xbf3680` | `0x20` | `.data.rel.ro` | RELRO const struct → `tdrv_arch_ops +248` | [runtime/tdrv-arch-ops.md](../runtime/tdrv-arch-ops.md) | HIGH |
| `mariana_instr_block_caps` | `0xbf3760` | `0x20` | `.data.rel.ro` | RELRO const struct → `tdrv_arch_ops +248` | [runtime/tdrv-arch-ops.md](../runtime/tdrv-arch-ops.md) | HIGH |
| `all_reduce_128b` | `0xc07e20` | `0xaad` | `.data` | const collectives blob | [collectives/overview.md](../collectives/overview.md) | LOW |
| `nrt_init_state_strings` | `0xc08900` | `0x20` | `.data` | const `const char*[4]` (RELATIVE→`.rodata`) | [runtime/api-lifecycle.md](../runtime/api-lifecycle.md) | HIGH |
| `v3_queue_bundle_alloc_table` | `0xc08bc0` | `0x170` | `.data` | const DMA alloc value table (no relocs) | [neff/load-pipeline.md](../neff/load-pipeline.md) | MED |
| `v2_queue_bundle_alloc_table` | `0xc08d40` | `0x1e0` | `.data` | const DMA alloc value table (no relocs) | [neff/load-pipeline.md](../neff/load-pipeline.md) | MED |
| `sequencer_error_subtypes_str` | `0xc091a0` | `0x400` | `.data` | const string-ptr table (12 RELATIVE relocs) | [exec/completion-engine.md](../exec/completion-engine.md) | MED |
| `ccschedxu` (`_ZL9ccschedxu`) | `0xc37c60` | `0x8800` | `.bss` | per-engine array (collectives schedule xu) | [exec/xu-workers.md](../exec/xu-workers.md) | MED |
| `ccprepxu` (`_ZL8ccprepxu`) | `0xc40460` | `0x8800` | `.bss` | per-engine array (collectives prepare xu) | [exec/xu-workers.md](../exec/xu-workers.md) | MED |
| `tensoropxu` (`_ZL10tensoropxu`) | `0xc48c60` | `0x11000` | `.bss` | per-engine array (tensor-op xu state) | [exec/xu-workers.md](../exec/xu-workers.md) | MED |
| `bs` (`_ZL2bs`) | `0xc59c60` | `0x2800` | `.bss` | per-engine array (`barrier_state_t[]`) | [runtime/api-async-collectives.md](../runtime/api-async-collectives.md) | MED |
| `ngc` | `0xc5c460` | 8 | `.bss` | mutable singleton (`nrt_global_config_t*`, heap 296 B) | [runtime/config-structs.md](../runtime/config-structs.md) | HIGH |
| `nrt_config` | `0xc5c480` | `0x290` | `.bss` | mutable singleton (env/NEFF config, 656 B) | [runtime/config-structs.md](../runtime/config-structs.md) | HIGH |
| `dlr_model_set` (`_ZL13dlr_model_set`) | `0xc5c720` | `0x78` | `.bss` | mutable singleton (`std::set`) | [exec/kmgr-facade.md](../exec/kmgr-facade.md) | MED |
| `seen_neff_lock` | `0xc5c7c0` | `0x28` | `.bss` | lock (`pthread_mutex_t`) | [exec/kmgr-facade.md](../exec/kmgr-facade.md) | MED |
| `global_lock` | `0xc5c880` | `0x38` | `.bss` | lock (`pthread_mutex_t`) | [runtime/overview.md](../runtime/overview.md) | MED |
| `session_ctx` (`_ZL11session_ctx`) | `0xc5c900` | `0x840` | `.bss` | mutable singleton (profiler per-NC ctx) | [trace/inspect-profile-api.md](../trace/inspect-profile-api.md) | HIGH |
| `nrt_init_state` | `0xc5d1a0` | 4 | `.bss` | mutable singleton (`NRT_INIT_STATE` enum) | [runtime/api-lifecycle.md](../runtime/api-lifecycle.md) | HIGH |
| `host_stats_default` (`_ZL18host_stats_default`) | `0xc5d1e0` | `0x138` | `.bss` | mutable singleton (ntff host-stats default) | [trace/telemetry-errors.md](../trace/telemetry-errors.md) | MED |
| `nrt_vnc_usage_lock` | `0xc5d320` | `0x28` | `.bss` | lock (`pthread_mutex_t`) | [runtime/api-device-config.md](../runtime/api-device-config.md) | HIGH |
| `nrt_vnc_usage_vnc_count` | `0xc5d348` | 4 | `.bss` | mutable singleton (vnc count) | [runtime/api-device-config.md](../runtime/api-device-config.md) | HIGH |
| `nrt_vnc_usage` (`_ZL13nrt_vnc_usage`) | `0xc5d360` | `0x400` | `.bss` | per-engine array (per-vNC usage table) | [runtime/api-device-config.md](../runtime/api-device-config.md) | HIGH |
| `tnc_lock` (`_ZL8tnc_lock`) | `0xc5d840` | `0x28` | `.bss` | lock (`pthread_mutex_t`) | [neff/load-pipeline.md](../neff/load-pipeline.md) | HIGH |
| `tnc` (`_ZL3tnc`) | `0xc5d880` | `0x48` | `.bss` | mutable singleton (temp-NEFF-cache slot) | [neff/load-pipeline.md](../neff/load-pipeline.md) | HIGH |
| `async_exec_workers` | `0xc5d8c8` | 8 | `.bss` | mutable singleton (async-exec worker handle) | [exec/xu-workers.md](../exec/xu-workers.md) | MED |
| `dlr_model_set` (additional instance) | `0xc5d8e0` | `0x78` | `.bss` | mutable singleton (`std::set`, 2nd) | [exec/kmgr-facade.md](../exec/kmgr-facade.md) | LOW |
| `pool_count` | `0xc5d960` | 4 | `.bss` | mutable singleton (handle-pool init guard) | [exec/xu-workers.md](../exec/xu-workers.md) | HIGH |
| `handle_pools` | `0xc5d980` | `0x7000` | `.bss` | per-engine array (`comp_handle_pool_t[]`, 112 B) | [exec/xu-workers.md](../exec/xu-workers.md) | HIGH |
| `_tpb_xus` | `0xc64a40` | `0x24000` | `.bss` | per-engine array (per-TPB xu state) | [exec/xu-workers.md](../exec/xu-workers.md) | MED |
| `workers` (`_ZL7workers`) | `0xc88ae0` | `0x5800` | `.bss` | per-engine array (worker-pool) | [exec/xu-workers.md](../exec/xu-workers.md) | MED |
| `async_sr_init_mutex` (`_ZL19…`) | `0xc8e360` | `0x1400` | `.bss` | lock (async send/recv init) | [collectives/send-recv.md](../collectives/send-recv.md) | HIGH |
| `async_sr_at_exit_registered` | `0xc8f760` | 1 | `.bss` | mutable singleton (atexit flag) | [collectives/send-recv.md](../collectives/send-recv.md) | HIGH |
| `async_sr_at_exit_mutex` | `0xc8f780` | `0x28` | `.bss` | lock (async send/recv atexit) | [collectives/send-recv.md](../collectives/send-recv.md) | HIGH |
| `async_sr_ctxs` (`_ZL13async_sr_ctxs`) | `0xc8f7c0` | `0x400` | `.bss` | per-engine array (`async_sr_context*[128]`) | [collectives/send-recv.md](../collectives/send-recv.md) | HIGH |
| `async_sr_status` (`_ZL15async_sr_status`) | `0xc8fbc0` | `0x200` | `.bss` | per-engine array (per-LNC status) | [collectives/send-recv.md](../collectives/send-recv.md) | HIGH |
| `async_sr_ofi_mutex` (`_ZL18…`) | `0xc8fdc0` | `0x1400` | `.bss` | lock (async send/recv OFI) | [collectives/send-recv.md](../collectives/send-recv.md) | HIGH |
| `async_sr_thread_n` | `0xc911c0` | 4 | `.bss` | mutable singleton (service-thread count) | [collectives/send-recv.md](../collectives/send-recv.md) | MED |
| `g_shared_proxy_queues_lock` (`_ZL26…`) | `0xc912e0` | `0x28` | `.bss` | lock (`pthread_mutex_t`) | [collectives/proxy-driver.md](../collectives/proxy-driver.md) | HIGH |
| `g_shared_proxy_queues` (`_ZL21…`) | `0xc91320` | `0x30` | `.bss` | mutable singleton (`std::map<int,proxy_queue>`) | [collectives/proxy-driver.md](../collectives/proxy-driver.md) | HIGH |
| `libnccl_net_err_msg` | `0xc96420` | `0x200` | `.bss` | mutable singleton (last net-plugin error string) | [collectives/nccl-boundary.md](../collectives/nccl-boundary.md) | MED |
| `libnccl_net_errno` | `0xc96620` | 4 | `.bss` | mutable singleton (last net-plugin errno) | [collectives/nccl-boundary.md](../collectives/nccl-boundary.md) | MED |
| `libnccl_net_handle` | `0xc96628` | 8 | `.bss` | dlopen handle (libnccom net plugin) | [collectives/nccl-boundary.md](../collectives/nccl-boundary.md) | HIGH |
| `nccl_init_error` (`_ZL15nccl_init_error`) | `0xc96640` | `0x178` | `.bss` | mutable singleton (init-error capture) | [collectives/nccl-boundary.md](../collectives/nccl-boundary.md) | HIGH |
| libnccom fn-ptr store (`neuronInitComm` ..) | `0xc967b8`–`0xc968d8` | 37×8 | `.bss` | dlsym store (named slots) | [collectives/nccl-boundary.md](../collectives/nccl-boundary.md) | HIGH |
| `nlog_stdout_is_tty` | `0xc96940` | 1 | `.bss` | mutable singleton (TTY-probe flag) | [runtime/logging.md](../runtime/logging.md) | HIGH |
| `nlog_stderr_is_tty` | `0xc96941` | 1 | `.bss` | mutable singleton (TTY-probe flag) | [runtime/logging.md](../runtime/logging.md) | HIGH |
| `log_coalescing_enabled` | `0xc96942` | 1 | `.bss` | mutable singleton (log-coalescing flag, ←`ngc +212`) | [runtime/logging.md](../runtime/logging.md) | MED |
| `error_log_mutex` | `0xc96960` | `0x28` | `.bss` | lock (guards error-log ring) | [runtime/logging.md](../runtime/logging.md) | HIGH |
| `error_log` | `0xc96990` | `0x18` | `.bss` | mutable singleton (error-log ring) | [runtime/logging.md](../runtime/logging.md) | HIGH |
| `ucode_lib_handle` | `0xc96a40` | 8 | `.bss` | dlopen handle (`libnrtucode_extisa.so`) | [gpsimd/ucode-facade.md](../gpsimd/ucode-facade.md) | HIGH |
| ucode fn-ptr store (`ucode_lib_get_api_level` ..) | `0xc96a48`–`0xc96b30` | 30×8 | `.bss` | dlsym store (named slots) | [gpsimd/ucode-facade.md](../gpsimd/ucode-facade.md) | HIGH |
| `nds_instances` | `0xc96c00` | `0x100` | `.bss` | per-engine array (`nds*[32]` per mla_idx) | [datastore/userspace-libnds.md](../datastore/userspace-libnds.md) | HIGH |
| `tdrv_arch_ops` | `0xc97180` | `0x1e8` | `.bss` | per-arch vtable (61-slot, 488 B) | [runtime/tdrv-arch-ops.md](../runtime/tdrv-arch-ops.md) | HIGH |
| `tdrv_arch_ops_initialized` | `0xc97368` | 4 | `.bss` | mutable singleton (init guard) | [runtime/tdrv-arch-ops.md](../runtime/tdrv-arch-ops.md) | HIGH |
| `csr_regions_v2` | `0xc97380` | `0x300` | `.bss` | per-arch vtable (CSR region table) | [runtime/arch-csr-offsets.md](../runtime/arch-csr-offsets.md) | MED |
| `csr_regions_v3` | `0xc97680` | `0x7e00` | `.bss` | per-arch vtable (CSR region table) | [runtime/arch-csr-offsets.md](../runtime/arch-csr-offsets.md) | MED |
| `csr_regions_v4` | `0xc9f480` | `0x7e00` | `.bss` | per-arch vtable (CSR region table) | [runtime/arch-csr-offsets.md](../runtime/arch-csr-offsets.md) | MED |
| `num_owned_vcores` | `0xca7300` | 4 | `.bss` | mutable singleton (owned-vcore count) | [runtime/tdrv-lifecycle.md](../runtime/tdrv-lifecycle.md) | HIGH |
| `owned_vcores` | `0xca7320` | `0x800` | `.bss` | per-engine array (`virtual_core_t*[≤256]`) | [runtime/tdrv-lifecycle.md](../runtime/tdrv-lifecycle.md) | HIGH |
| `vcores` | `0xca7b40` | `0x4000` | `.bss` | per-engine array (`virtual_core_t[256]`, 64 B) | [runtime/tdrv-lifecycle.md](../runtime/tdrv-lifecycle.md) | HIGH |
| `ctx_vtpb_core_size` | `0xcabb40` | 4 | `.bss` | mutable singleton (vtpb init guard) | [runtime/tdrv-lifecycle.md](../runtime/tdrv-lifecycle.md) | HIGH |
| `stream_locks` | `0xcabb60` | `0x2800` | `.bss` | lock array (`pthread_mutex_t[]`) | [trace/system-monitor.md](../trace/system-monitor.md) | HIGH |
| `streams` | `0xcae360` | `0x800` | `.bss` | per-engine array (ndebug stream array) | [trace/system-monitor.md](../trace/system-monitor.md) | HIGH |
| `hal_target` | `0xcaeb60` | 4 | `.bss` | mutable singleton (`int` arch enum) | [runtime/hal-adapter.md](../runtime/hal-adapter.md) | HIGH |
| `kaena_khal` | `0xcaeb80` | `0x7a8` | `.bss` | per-arch vtable (HAL vtable-of-vtables, ~245 slots) | [runtime/hal-adapter.md](../runtime/hal-adapter.md) | HIGH |

> **GOTCHA — `ctx_vtpb_core_size` (`0xcabb40`) sorts just below `stream_locks` (`0xcabb60`).** The vtpb init-guard scalar lives at `0xcabb40`, immediately below the ndebug `stream_locks` array at `0xcabb60` — two unrelated subsystems are adjacent in `.bss`. Address adjacency in `.bss` is the linker's bookkeeping, not a semantic grouping; index by symbol, never by neighbour. The owning-page column is the only reliable grouping.

> **QUIRK — the named dlsym stores are address *ranges*, not single symbols.** The ucode store (`0xc96a48`–`0xc96b30`, 30×8) and the libnccom store (`0xc967b8`–`0xc968d8`, 37×8) each carry one named `.bss` symbol *per slot* (`ucode_lib_get_api_level`, `neuronInitComm`, …) rather than one array symbol. They are indexed here as a single row per range; the per-slot name→symbol mapping is in the [atlas dlopen-handle-tables section](../forensics/globals-atlas.md#dlopen-handle-tables) and the [ucode facade](../gpsimd/ucode-facade.md).

> **CORRECTION (GLOBALS-INDEX, nccl error block) —** the working notes placed `libnccl_net_err_msg` (`0xc96420`) as the only nccl error global and described `nccl_init_error` as an `ostringstream`. The `nm -nS`-verified atlas adds `libnccl_net_errno` @`0xc96620`, `libnccl_net_handle` @`0xc96628`, and the libnccom fn-ptr store @`0xc967b8`, and corrects `nccl_init_error` @`0xc96640` to a plain `0x178`-byte capture object (not an `ostringstream`). This index uses the atlas's verified addresses and types throughout.

> **NOTE — vendored third-party statics are out of scope here.** The protobuf `descriptor_table_*` objects, abseil `kOperatorList`/`kBuiltinTypeList`, Rust `CONTEXTS`/`MAPPINGS_CACHE`, simdjson `jump_table`, and libarchive `fileflags` that also occupy `.data.rel.ro`/`.data`/`.bss` are boundary context, indexed by the [Vendored-Library SBOM](../forensics/vendored-sbom.md) and listed in the [atlas vendored-statics section](../forensics/globals-atlas.md#vendored-statics--boundary-context). This index covers first-party runtime globals only.

---

## Cross-References

- [Globals and Singletons Atlas](../forensics/globals-atlas.md) — the explanatory map this page indexes; groups these globals by kind and carries the per-global init-by / read-by provenance, the installer chains, and the GOTCHA/QUIRK notes
- [Binary Layout](../reference/binary-layout.md) — the `.data.rel.ro` / `.data` / `.bss` section geometry, the VMA==file-offset (delta 0) invariant, and the table-location map that places each global class in its band
- [Dispatch-Table Taxonomy](../forensics/dispatch-tables.md) — the per-arch vtable dispatch shape, the 222 standing data tables, and the `.rodata` jump-offset spill arrays this index references but does not list
- [Subsystem ↔ Binary ↔ Source-TU Matrix](subsystem-matrix.md) — the subsystem-to-source-TU map that names the translation unit (`nrt_config.cpp`, `enc.cc`, `vtpb.c`, `async_sr.cc`, …) behind each owning page in the index
