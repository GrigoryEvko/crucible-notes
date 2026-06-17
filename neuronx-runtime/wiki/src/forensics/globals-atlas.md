# Globals and Singletons Atlas

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB **DYN** x86-64, NOT stripped, DWARF v4.
>
> **Part II — Binary Anatomy & Forensics** / REFERENCE atlas · **Evidence grade:** every address/size below is read from this exact binary — `nm -nS --defined-only` for the symbol address + size + section nibble; `readelf -SW` for the section bounds; `readelf -rW` for the `.data.rel.ro` `R_X86_64_RELATIVE` relocations that wire each function-pointer / string-pointer slot; and the IDA decompiled sidecars under `ida/**/decompiled/` for the *init-by* / *read-by* call graph. · [back to forensics index](overview.md)

## Abstract

This is the **map of `libnrt.so`'s writable global state** — every standing singleton, per-arch vtable, and dlopen handle table that lives across the three writable data sections, with provenance for *who initializes it* and *who reads it*. The runtime is a single 117 MB C++/Rust shared object, but its process-global control plane is small and named: a `nrt_config` config singleton, a heap `nrt_global_config_t` reached through the `ngc` pointer, two coexisting per-arch HAL vtables (`kaena_khal`, `tdrv_arch_ops`), two dlsym-bound external-library function tables (the libnrtucode microcode loader and the libnccom collective-network plugin), a clutch of error/status globals, and a set of compiler-emitted dispatch/jump tables. Everything else in the writable sections is statically-vendored third-party state (protobuf descriptor pools, abseil, Rust std, simdjson, libarchive), flagged here as boundary context rather than re-derived.

The atlas separates the four writable sections by *what they hold*. `.data.rel.ro` (`0xbf2d80`–`0xc05540`, `0x127c0` B) is RELRO: read-only after the loader applies relocations, so it holds the function-pointer and string-pointer tables that must be immutable at runtime — the `ucode_func_symbols` dlsym directory, the per-arch `*_hw_port_names` arrays, the `*_instr_block_caps` const structs, and the C++ vtables. `.data` (`0xc07e00`–`0xc37c20`, `0x2fe20` B) holds const-valued dispatch tables that are *not* pointer arrays — `nrt_init_state_strings`, the queue-bundle allocation tables, `sequencer_error_subtypes_str`, the `all_reduce_128b` blob. `.bss` (`0xc37c40`–`0xcb4300`, `0x7c6c0` B) is NOBITS — **zero on disk** — and holds every mutable singleton and per-engine array: `nrt_config`, `ngc`, `kaena_khal`, `tdrv_arch_ops`, the vcore tables, the async-send/recv contexts, the proxy-queue map, the resolved dlsym function-pointer stores, and the error-log ring. The single most consequential structural fact for a reimplementer is that **the per-arch vtables are `.bss` symbols** — they contain no bytes in the file; their slot contents exist only as *stores* inside the lazily-run `register_funcs` / `register_*` installers, and so cannot be hexdumped.

This page groups the atlas by kind — config singletons, per-arch ops vtables, dlopen handle tables, error/status globals, dispatch/jump tables — and anchors each entry to its `nm` symbol and the function that fills the zero bytes. The per-slot geometry of the big subsystem structs is owned by the subsystem pages cross-linked at the foot; the atlas records the writable-global view (address, size, type, init-by, read-by, section) rather than re-deriving their internals.

For reimplementation, the contract is:

- **The section discipline** — RELRO for immutable function/string-pointer tables, `.data` for const-valued tables, `.bss` for mutable singletons and the lazily-filled per-arch vtables. A port that places a per-arch vtable in `.data.rel.ro` and tries to relocate it statically will not match: these tables are written *after* RELRO is applied, by code, at device bring-up.
- **The init-by / read-by provenance per global** — which is built by a static constructor ([Static-Init](static-init.md)), which is installed lazily at `nrt_init` / device bring-up, and which is filled on demand. The atlas tags every entry with its installer function.
- **The two-config split** — the static `nrt_config` singleton (`0xc5c480`, 656 B) versus the heap `nrt_global_config_t` reached via `ngc` (`0xc5c460`, 296 B). They are distinct objects with distinct lifetimes; both are populated from env/NEFF at init, neither is built complete by a constructor.
- **The dlsym table shape** — an immutable `.data.rel.ro` name/slot directory (`ucode_func_symbols`) paired with a `.bss` block of resolved function pointers a loader stub fills via `dlsym`. The libnccom path uses the same shape with individually-named slots.

| | |
|---|---|
| **`.data.rel.ro`** | `0xbf2d80`–`0xc05540` (`0x127c0` B) — RELRO ptr/string tables + C++ vtables |
| **`.data`** | `0xc07e00`–`0xc37c20` (`0x2fe20` B) — const-valued dispatch tables |
| **`.bss`** | `0xc37c40`–`0xcb4300` (`0x7c6c0` B, NOBITS) — mutable singletons + per-engine arrays |
| **Config singleton** | `nrt_config` @`0xc5c480` (`.bss`, `0x290` = 656 B) + `ngc` @`0xc5c460` → heap `nrt_global_config_t` (296 B) |
| **Per-arch HAL vtables** | `kaena_khal` @`0xcaeb80` (`.bss`, `0x7a8`) + `tdrv_arch_ops` @`0xc97180` (`.bss`, `0x1e8` = 488 B) |
| **Arch selector** | `hal_target` @`0xcaeb60` (`.bss`, 4 B) — set by `al_hal_tpb_set_arch_type` @`0x44bc00` |
| **dlsym tables** | `ucode_func_symbols` @`0xbf2ea0` (`.data.rel.ro`, 30 × 16 B) → store @`0xc96a48`; libnccom slots @`0xc967b8`–`0xc968d8` → handle @`0xc96628` |
| **Proxy-queue map** | `g_shared_proxy_queues` @`0xc91320` (`.bss`, `0x30`) + lock @`0xc912e0` |
| **Error-log ring** | `error_log` @`0xc96990` (`.bss`, `0x18`) + `error_log_mutex` @`0xc96960` |

---

## How to read this atlas

Each group below is a table with a fixed column grammar: **symbol** (the `nm` name; `_ZL`-prefixed names are file-local statics, demangled in the cell), **addr**, **size** (bytes; hex where the `nm` size is hex), **type** (the recovered C type or struct role), **init-by** (the function that first writes a valid value — *not* `.bss` zeroing), **read-by** (the principal consumer), **section**, and **Confidence**. The init-by column is the page's central claim and carries the provenance; where a global is built by a static constructor the slot number references [Static-Init](static-init.md), and where it is installed lazily the installer is named.

Two recurring init-by phrases mean specific things. **"static-init ctor #N"** means a `.init_array` constructor writes it at load time, *before* the first `nrt_*` call — this is the protobuf/abseil/iostream surface and the empty-container init of `nrt_config`. **"lazy @ bring-up"** (named by installer) means the global is `.bss`-zero until a device is opened and the named installer runs during `nrt_init`; these globals are deliberately *not* in `.init_array` so they are not subject to load-order constraints against the device being present (see the static-init [GOTCHA](static-init.md#the-init_array-walk)).

---

## Config singletons

The runtime's tunable state is split across two objects with different storage classes. `nrt_config` is a static `.bss` singleton; its containers are emptied by a constructor at load (ctor #11) and its scalar fields are filled at `nrt_init` by the env/NEFF parser. `ngc` is a single `.bss` pointer to a *heap*-allocated `nrt_global_config_t` — the parser allocates the 296-byte struct and stores it; `nrt_gconf()` returns it and `nrt_config_free()` frees and NULLs it.

| Symbol | Addr | Size | Type | Init-by | Read-by | Section | Conf |
|---|---|---|---|---|---|---|---|
| `nrt_config` | `0xc5c480` | `0x290` (656) | `nrt_config` env/NEFF config singleton | static-init ctor #11 (`_GLOBAL__sub_I_nrt_config.cpp` @`0x74920`, empty-init) → fields by `nrt_config_parse_init_config` @`0x8a1d0` | every config-gated path (cc/dma/profile) | `.bss` | HIGH |
| `ngc` | `0xc5c460` | 8 | `nrt_global_config_t*` (heap, 296 B) | `nrt_config_parse_init_config` @`0x8a1d0` (allocs + stores) | `nrt_gconf()` @`0x82670` accessor | `.bss` | HIGH |
| `nrt_init_state` | `0xc5d1a0` | 4 | `NRT_INIT_STATE` enum {START,INIT,CHILD,CLOSE} | `nrt_state_set(NRT_INIT_STATE)` @`0xb9090` | `nrt_state_is_init` @`0xb9080`, `nrt_state_get_string` @`0xb9060` | `.bss` | HIGH |
| `nrt_init_state_strings` | `0xc08900` | `0x20` | `const char*[4]` → `"NRT_STATE_{START,INIT,CHILD,CLOSE}"` | static (4 RELATIVE relocs → `.rodata` `0x83ed2f`..) | `nrt_state_get_string` @`0xb9060` | `.data` | HIGH |
| `nrt_vnc_usage` (`_ZL13nrt_vnc_usage`) | `0xc5d360` | `0x400` | per-vNC usage table | `nrt_vnc_usage_init(uint)` @`0xc1740` | `nrt_vnc_usage_{inc,dec,find_and_inc}` @`0xc17b0`/`0xc1820`/`0xc18a0` | `.bss` | HIGH |
| `session_ctx` (`_ZL11session_ctx`) | `0xc5c900` | `0x840` | profiler per-NC session context | `nrt_profile_session_init` @`0xad4a0` | profiler emit path | `.bss` | HIGH |

The `nrt_config` constructor is examined in full on the [Static-Init](static-init.md#a-representative-constructor--the-nrt_config-singleton) page; it only wires the two `std::vector<int>` and one `std::map` headers and arms `~nrt_config` via `__cxa_atexit`. The full 656-byte field layout and the 296-byte `ngc` layout are owned by [config-structs](../runtime/config-structs.md).

> **NOTE — two configs, two lifetimes.** `nrt_config` (static, 656 B) and the `ngc`-pointed `nrt_global_config_t` (heap, 296 B) are **distinct** objects, not two views of one struct. `nrt_config` lives for the process; the heap struct is allocated by the parser and torn down by `nrt_config_free()` @`0x82680`, which also NULLs `ngc`. A reimplementation that folds both into one allocation mishandles the free path: `nrt_config_free` frees only the heap half.

> **GOTCHA — `nrt_init_state_strings` lives in `.data`, but the strings it points to live in `.rodata`.** The four-pointer array at `0xc08900` is `R_X86_64_RELATIVE`-relocated; each slot resolves to a `.rodata` byte string (`0xc08900`→`0x83ed2f` = `"NRT_STATE_START"`, then `_INIT`/`_CHILD`/`_CLOSE`). `nrt_state_get_string` indexes the array by the `nrt_init_state` enum value; an out-of-range enum reads a wild pointer because there is no bounds guard in the stringifier — the writer (`nrt_state_set`) is the only place the enum is validated.

---

## Per-arch ops vtables

`libnrt.so` dispatches device-specific work through **two coexisting per-arch function-pointer tables**, each a `.bss` singleton bound once at bring-up by the device's architecture id (2 = sunda, 3 = cayman, 4 = mariana). `kaena_khal` is the wide HAL "vtable-of-vtables" (~245 qword slots: register/STPB/notific/intc/udma/TopSP accessors); `tdrv_arch_ops` is a narrower, *named* 488-byte struct (61 slots: topology counts, DMA-engine descriptors, CSR/event ops). Their division of labour and per-slot geometry are owned by [tdrv-arch-ops](../runtime/tdrv-arch-ops.md) and [hal-adapter](../runtime/hal-adapter.md); the atlas records the writable-global view — address, size, and the installer that fills the zero bytes.

| Symbol | Addr | Size | Type | Init-by | Read-by | Section | Conf |
|---|---|---|---|---|---|---|---|
| `kaena_khal` | `0xcaeb80` | `0x7a8` | HAL vtable-of-vtables (~245 fn-ptr slots; slot 0 = arch int) | `kaena_khal_init(arch)` @`0x462290` → `kaena_khal_register_funcs_v{2,3,4}` @`0x468740`/`0x46ed70`/`0x4622e0` | every `aws_hal_*` call site | `.bss` | HIGH |
| `hal_target` | `0xcaeb60` | 4 | `int` arch enum (HAL target) | `al_hal_tpb_set_arch_type` @`0x44bc00` | HAL arch-decode switches | `.bss` | HIGH |
| `tdrv_arch_ops` | `0xc97180` | `0x1e8` (488) | named 61-slot per-arch op struct (47 members + 2 nested) | `tdrv_arch_ops_init` @`0x308e80` → `tdrv_arch_register_{sunda,cayman,mariana}` @`0x30b6a0`/`0x30c7d0`/`0x30d900` | tdrv topology/DMA/CSR paths | `.bss` | HIGH |
| `tdrv_arch_ops_initialized` | `0xc97368` | 4 | init guard (0/1) | `tdrv_arch_ops_init` @`0x308e80` | every `tdrv_arch_ops.*` access (asserts !=0) | `.bss` | HIGH |
| `mariana_hw_port_names` | `0xbf30a0` | `0x68` | per-arch HW-port name ptr array (seed) | static (RELRO relocs) | KaenaHal arch port-name lookup | `.data.rel.ro` | HIGH |
| `cayman_hw_port_names` | `0xbf3260` | `0x68` | per-arch HW-port name ptr array (seed) | static | KaenaHal arch port-name lookup | `.data.rel.ro` | HIGH |
| `sunda_hw_port_names` | `0xbf3420` | `0x68` | per-arch HW-port name ptr array (seed) | static | KaenaHal arch port-name lookup | `.data.rel.ro` | HIGH |
| `cayman_instr_block_caps` | `0xbf3680` | `0x20` | const struct → `tdrv_arch_ops +248` | static | `tdrv_arch_register_cayman` (seeds slot) | `.data.rel.ro` | HIGH |
| `mariana_instr_block_caps` | `0xbf3760` | `0x20` | const struct → `tdrv_arch_ops +248` | static | `tdrv_arch_register_mariana` (seeds slot) | `.data.rel.ro` | HIGH |
| `csr_regions_v2` | `0xc97380` | `0x300` | per-arch CSR region table | tdrv CSR bring-up | `csr_get_regions` (tdrv_arch_ops +432) | `.bss` | MED |
| `csr_regions_v3` | `0xc97680` | `0x7e00` | per-arch CSR region table | tdrv CSR bring-up | `csr_get_regions` | `.bss` | MED |
| `csr_regions_v4` | `0xc9f480` | `0x7e00` | per-arch CSR region table | tdrv CSR bring-up | `csr_get_regions` | `.bss` | MED |

> **GOTCHA — the per-arch vtables are empty in the file; do not hexdump them.** `kaena_khal` (`0xcaeb80`) and `tdrv_arch_ops` (`0xc97180`) are `.bss` (NOBITS) symbols: `objdump -s` shows zero bytes. Their slots exist only as *stores* inside the `register_funcs_v{2,3,4}` / `tdrv_arch_register_*` installers, which run **lazily at device bring-up — not in `.init_array`**. A reimplementer reconstructs them from the installers' decompiled stores (seeded from the `.data.rel.ro` `*_hw_port_names` / `*_instr_block_caps` source arrays), not from a static dump. This is also why these tables carry no `_ZTV` RTTI entry — they are C function-pointer structs, not C++ vtables ([RTTI Class Hierarchy](rtti-class-hierarchy.md)).

> **QUIRK — the install is single-shot and unguarded against re-arch.** `kaena_khal` has no separate `initialized` guard (its slot 0 holds the arch int and doubles as the marker); `tdrv_arch_ops` has `tdrv_arch_ops_initialized` @`0xc97368` but it is a plain 0/1 flag, not a lock. The install runs once, early in `nrt_init`, on the single thread that brings the device up. There is no re-entrancy protection: a process that opened two devices of *different* arch would clobber the singleton, because the vtable is process-global, not per-device. The runtime's model is one arch per process; the guard exists to assert use-after-init, not to serialize a second install.

> **QUIRK — arch enum and version suffix do not align across the two tables.** `kaena_khal_init` maps arch 2→`v2` (sunda), 3→`v3` (cayman), 4→`v4` (mariana); but `tdrv_arch_ops_init` switches in source order `{3 cayman, 4 mariana, 2 sunda}` and the seed arrays are named by codename (`sunda`/`cayman`/`mariana`), not by `v2`/`v3`/`v4`. Drive the selector off the arch enum from `al_hal_tpb_get_arch_type`, never off the numeric suffix.

---

## dlopen handle tables

Two external shared objects are loaded at runtime through `dlopen`/`dlsym` and bound into function-pointer tables. The **microcode loader** (`libnrtucode_extisa.so`) uses a clean two-part shape: an immutable `.data.rel.ro` directory of `{void** slot, const char* name}` pairs (`ucode_func_symbols`), and a `.bss` block of resolved function pointers the loader fills by iterating the directory and calling `dlsym(name)`. The **collective-network plugin** (libnccom) uses the same idea but its slots are individually-named `.bss` statics rather than a directory.

| Symbol | Addr | Size | Type | Init-by | Read-by | Section | Conf |
|---|---|---|---|---|---|---|---|
| `ucode_func_symbols` | `0xbf2ea0` | `0x1e0` (30×16) | `ucode_symbol_t[30]` = `{void** func; const char* symbol}` | static (60 RELATIVE relocs: even→`.bss` slot, odd→`.rodata` name `0x844350`..) | `ucode_init_module` @`0x225940` (iterates → `dlsym`) | `.data.rel.ro` | HIGH |
| `ucode_lib_handle` | `0xc96a40` | 8 | `dlopen` handle for `libnrtucode_extisa.so` | `ucode_init_module` @`0x225940` | ucode teardown (`dlclose`) | `.bss` | HIGH |
| ucode fn-ptr store (`ucode_lib_get_api_level` .. `ucode_lib_core_disable_pc_bounds_check`) | `0xc96a48`–`0xc96b30` | 30×8 | resolved `dlsym` targets (named slots) | `ucode_init_module` @`0x225940` | every `nrtucode_*` call site | `.bss` | HIGH |
| `libnccl_net_handle` | `0xc96628` | 8 | `dlopen` handle for the libnccom net plugin | net-plugin loader | plugin teardown | `.bss` | HIGH |
| libnccom fn-ptr table (`neuronInitComm`, `neuronNetIsend`, `neuronNetworkProxyProgress`, …) | `0xc967b8`–`0xc968d8` | 37×8 | resolved `dlsym` targets (named slots) | net-plugin loader (`dlsym` each) | collectives proxy/net path | `.bss` | HIGH |

> **NOTE — `ucode_func_symbols` is a directory, the store is the payload.** The 60 `R_X86_64_RELATIVE` relocs over `0xbf2ea0`..`0xbf3080` interleave two pointer kinds: even offsets point *into `.bss`* (`0xbf2ea0`→`0xc96b30`, the `func` slot to fill), odd offsets point *into `.rodata`* (`0xbf2ea8`→`0x844350` = `"nrtucode_get_api_level"`, the symbol name). `ucode_init_module` walks the 30 entries, calls `dlsym(handle, entry.symbol)`, and stores the result through `entry.func`. So `ucode_func_symbols[0]` resolves `"nrtucode_get_api_level"` into the named `.bss` slot `ucode_lib_get_api_level` @`0xc96b30`. The directory is RELRO (immutable after load); the store is plain `.bss` (written at `dlsym` time). A reimplementation keeps the name table const and the resolved-pointer block writable.

> **GOTCHA — both tables are zero until the plugin loads; calling through an unresolved slot is a NULL jump.** The `.bss` fn-ptr stores (`0xc96a48`.. and `0xc967b8`..) hold NULL until `ucode_init_module` / the net-plugin loader runs. If the external `.so` is absent or `dlopen` fails, the slots stay NULL and any call site that does not first check the handle (`ucode_lib_handle` / `libnccl_net_handle`) jumps through NULL. The handle is the liveness gate; the resolved-pointer block is not self-describing.

---

## Error & status globals

A small set of process-global error/status objects back logging and the collective-network error path. The error-log is a fixed-capacity ring guarded by a mutex; the nccl error globals capture the last network-plugin failure for diagnostics.

| Symbol | Addr | Size | Type | Init-by | Read-by | Section | Conf |
|---|---|---|---|---|---|---|---|
| `error_log` | `0xc96990` | `0x18` | ring `{char* log; int capacity; int tail; int num_wraps}` | first `print_to_error_log` @`0x224940` (lazy alloc) | `nlog_dump_error_log` / `nlog_clear_error_log` | `.bss` | HIGH |
| `error_log_mutex` | `0xc96960` | `0x28` | `pthread_mutex_t` guarding the ring | static (zero-init mutex) | `print_to_error_log` (locks each write) | `.bss` | HIGH |
| `nccl_init_error` (`_ZL15nccl_init_error`) | `0xc96640` | `0x178` | collective init-error capture object | static-init ctor #22 (`_GLOBAL__sub_I_neuron_nccl.cc` @`0x75040`) + `__cxa_atexit` | nccl init diagnostics | `.bss` | HIGH |
| `libnccl_net_err_msg` | `0xc96420` | `0x200` | last net-plugin error string buffer | net-plugin path (on error) | error reporting | `.bss` | MED |
| `libnccl_net_errno` | `0xc96620` | 4 | last net-plugin errno | net-plugin path | error reporting | `.bss` | MED |
| `nlog_stderr_is_tty` / `nlog_stdout_is_tty` | `0xc96941` / `0xc96940` | 1 / 1 | TTY-probe flags | static-init ctor #24 (`nlog_constructor` @`0x77b60`, `isatty`) | nlog color/format gating | `.bss` | HIGH |
| `log_coalescing_enabled` | `0xc96942` | 1 | log-coalescing flag | config parse (`ngc +212`) | nlog write path | `.bss` | MED |

> **QUIRK — `error_log` allocates its backing store lazily, not in any constructor.** The ring header at `0xc96990` is `.bss`-zero at load; the `char* log` buffer is allocated on the *first* `print_to_error_log` call and sized by `capacity`. The wrap logic increments `num_wraps` and resets `tail` when `tail + len >= capacity`. A reimplementation that pre-allocates the ring in a constructor changes the first-write timing but not behavior; the binary defers it.

> **CORRECTION (GLOBALS, nccl_init_error type) —** an earlier inventory described `nccl_init_error` as an `ostringstream` (which would imply a `std::__cxx11::basic_ostringstream` of ~`0x1c8` B). `nm -nS` reports `_ZL15nccl_init_error` @`0xc96640` with size **`0x178`** (376 B); the smaller footprint and the absence of a stream `_ZTV` reference at that address indicate a plain capture object/string, not a full `ostringstream`. The init-by claim (ctor #22 `_GLOBAL__sub_I_neuron_nccl.cc` + `__cxa_atexit`) is unchanged; only the type is corrected.

---

## Dispatch / jump tables

The standing dispatch tables that live in the writable sections (as opposed to the `.rodata` jump-offset spill arrays owned by [Dispatch-Table Taxonomy](dispatch-tables.md)). These are the per-engine state arrays, the proxy-queue map, the vcore tables, and the const-valued allocation/error tables. The big per-engine arrays are zeroed by constructors (ctors #10/#18/#19/#21/#25, see [Static-Init](static-init.md)) and filled per-vNC at execution time; the vcore tables and async contexts are installed lazily at bring-up.

| Symbol | Addr | Size | Type | Init-by | Read-by | Section | Conf |
|---|---|---|---|---|---|---|---|
| `g_shared_proxy_queues` (`_ZL21g_shared_proxy_queues`) | `0xc91320` | `0x30` | `std::map<int, proxy_queue>` | static-init ctor #21 (`_GLOBAL__sub_I_enc.cc` @`0x74ff0`) + `__cxa_atexit` | collective proxy driver | `.bss` | HIGH |
| `g_shared_proxy_queues_lock` (`_ZL26…`) | `0xc912e0` | `0x28` | `pthread_mutex_t` | static | proxy enqueue/dequeue | `.bss` | HIGH |
| `owned_vcores` | `0xca7320` | `0x800` | `virtual_core_t*[≤256]` per-LNC table | `vtpb_ctx_init` @`0x313dc0` | `vtpb_get_virtual_core` @`0x313fb0` | `.bss` | HIGH |
| `num_owned_vcores` | `0xca7300` | 4 | `u32` count | `vtpb_ctx_init` @`0x313dc0` | `vtpb_get_virtual_core` (bounds) | `.bss` | HIGH |
| `vcores` | `0xca7b40` | `0x4000` | `virtual_core_t[256]` (64 B each) backing store | `vtpb_ctx_init` (memset + fill) | LNC mapping path | `.bss` | HIGH |
| `ctx_vtpb_core_size` | `0xcabb40` | 4 | init guard | `vtpb_ctx_init` @`0x313dc0` | `vtpb_get_virtual_core` (asserts !=0) | `.bss` | HIGH |
| `async_sr_ctxs` (`_ZL13async_sr_ctxs`) | `0xc8f7c0` | `0x400` | `async_sr_context*[128]` per host-LNC | `enc_async_sr_init` @`0xeade0` | async send/recv service thread | `.bss` | HIGH |
| `async_sr_status` (`_ZL15async_sr_status`) | `0xc8fbc0` | `0x200` | per-LNC async status | `enc_async_sr_init` @`0xeade0` | async send/recv path | `.bss` | HIGH |
| `nds_instances` | `0xc96c00` | `0x100` | `nds*[32]` per mla_idx | `tdrv_init_nds_for_device` @`0x22f230` (`nds_open` @`0x5070d0`) | NDS query path | `.bss` | HIGH |
| `handle_pools` | `0xc5d980` | `0x7000` | `comp_handle_pool_t[]` (112 B each) | `xu_comp_handle_pool_init` @`0xe55e0` | kmgr completion-handle alloc | `.bss` | HIGH |
| `pool_count` | `0xc5d960` | 4 | pool init guard | `xu_comp_handle_pool_init` @`0xe55e0` | handle alloc (bounds) | `.bss` | HIGH |
| `tnc` (`_ZL3tnc`) | `0xc5d880` | `0x48` | temp-NEFF-cache slot singleton | `kmgr/dlr.cpp` `enter_cache` | NEFF load cache | `.bss` | HIGH |
| `tnc_lock` (`_ZL8tnc_lock`) | `0xc5d840` | `0x28` | `pthread_mutex_t` | static | `enter_cache` / `release_tmp_neff_cache` | `.bss` | HIGH |
| `_tpb_xus` | `0xc64a40` | `0x24000` | per-TPB execution-unit state | static-init ctor #18 (`_GLOBAL__sub_I_tpb_xu.cc` @`0x74e60`) | kmgr/xu execute | `.bss` | MED |
| `tensoropxu` (`_ZL10tensoropxu`) | `0xc48c60` | `0x11000` | tensor-op xu state | static-init ctor #10 (`_GLOBAL__sub_I_nrt_async.cpp` @`0x74510`, 127-entry zero) | kmgr/xu execute | `.bss` | MED |
| `workers` (`_ZL7workers`) | `0xc88ae0` | `0x5800` | worker-pool array | static-init ctor #19 (`_GLOBAL__sub_I_xu_worker.cc` @`0x74f00`) | kmgr/xu worker threads | `.bss` | MED |
| `streams` | `0xcae360` | `0x800` | ndebug stream array | static-init ctor #25 (`ndebug_stream_module_init` @`0x77bb0`) | ndebug stream path | `.bss` | HIGH |
| `stream_locks` | `0xcabb60` | `0x2800` | `pthread_mutex_t[]` | static-init ctor #25 (`pthread_mutex_init` loop) | ndebug stream path | `.bss` | HIGH |
| `v2_queue_bundle_alloc_table` | `0xc08d40` | `0x1e0` | const-valued DMA queue-bundle alloc table (no relocs) | static (const data) | `model_dma_engine_and_queue_bundle_alloc` @`0x22cb40` | `.data` | MED |
| `v3_queue_bundle_alloc_table` | `0xc08bc0` | `0x170` | const-valued DMA queue-bundle alloc table (no relocs) | static (const data) | `model_dma_engine_and_queue_bundle_alloc` | `.data` | MED |
| `sequencer_error_subtypes_str` | `0xc091a0` | `0x400` | error-subtype string ptr table (12 RELATIVE relocs) | static (ptr table in `.data`) | `v2_infer_error_get_sequencer_error_text` @`0x321720` | `.data` | MED |
| `all_reduce_128b` | `0xc07e20` | `0xaad` | collectives const blob | static (const data) | collective all-reduce path | `.data` | LOW |

> **QUIRK — `v2/v3_queue_bundle_alloc_table` are value tables, not pointer tables.** `readelf -rW` shows **zero** relocations over `0xc08d40`/`0xc08bc0`, and the companion `*_table_len` symbols are `.rodata` (`v2_queue_bundle_alloc_table_len` @`0x9ba590`). These tables hold packed alloc records (engine/queue indices), not function pointers — they are indexed by the model DMA allocator, not jumped through. Contrast with `sequencer_error_subtypes_str` @`0xc091a0`, which *is* a pointer array (12 RELATIVE relocs into `.rodata`). When porting, place value tables in `.rodata`; only the string-pointer table needs relocation.

> **NOTE — the big per-engine arrays are zeroed early but filled late.** `_tpb_xus` (`0x24000` B), `tensoropxu` (`0x11000`), `workers` (`0x5800`), and the barrier-state array are brought to a zero/empty state by their constructors (ctors #10/#18/#19, [Static-Init](static-init.md)), but their *contents* are written per-vNC during execution. The constructor cost is the zeroing; the meaningful population is in the execute path. A reimplementer who only ports the constructors gets correctly-sized but empty arrays — the per-vNC indexing logic is owned by the kmgr/xu execution pages.

---

## Vendored statics — boundary context

The bulk of `.data.rel.ro` and the proto `descriptor_table_*` objects in `.data`/`.bss` are statically-linked third-party state, not first-party runtime globals. They are catalogued by their owning pages and listed here only so the atlas is complete about *what else* occupies the writable sections.

| Family | Where | What | Owning page |
|---|---|---|---|
| protobuf 5.26.1 | `.data` `descriptor_table_ntff_2eproto` @`0xc0b6a0`, `_default_instance_` objects @`0xcaf340`.. (`.bss`), `file_default_instances` @`0xbf4500`/`0xbf8020` (`.data.rel.ro`); `::_table_` TcParse tables | [vendored-sbom](vendored-sbom.md), [dispatch-tables](dispatch-tables.md) |
| abseil LTS 20230802 | `.data.rel.ro` `kOperatorList`/`kBuiltinTypeList` @`0xbf5c40`..; arena storage @`0xcb3e68`..; `status`/`cord` globals | [vendored-sbom](vendored-sbom.md) |
| Rust std / memchr / neuron_rustime | `ARGV_INIT_ARRAY` (ctor #1), `CONTEXTS` @`0xc0d300` (`.data`, `0x28000`), `MAPPINGS_CACHE`, `ENV_LOCK` | [vendored-sbom](vendored-sbom.md) |
| simdjson 0.9.0 | fallback `jump_table` @`0xc0c520`, `error_codes` @`0xbf3da0` | [vendored-sbom](vendored-sbom.md) |
| libarchive 3.8.0dev | reader vtables (`archive_read_vtable` @`0xbf3cc0`, `gzip_reader_vtable` @`0xbf3d30`), `fileflags` @`0xbf3a20` | [vendored-sbom](vendored-sbom.md) |

---

## Cross-References

- [Static-Init Pipeline](static-init.md) — the 77 `.init_array` constructors that bring the config singleton, the proxy-queue map, the nccl error object, the ndebug stream locks, and the big per-engine arrays to their initial state; and the rationale for why `kaena_khal`/`tdrv_arch_ops`/`ngc`/vcores are *not* built there
- [Dispatch-Table Taxonomy](dispatch-tables.md) — the 222 standing data tables + 460 compiler switches; the per-arch vtable dispatch shape and the `.rodata` jump-offset spill arrays this page only references
- [C++ Class Hierarchy and RTTI](rtti-class-hierarchy.md) — why the per-arch ops vtables carry no `_ZTV` entry (C function-pointer structs, not C++ vtables) and the `_default_instance_` DefaultTypeInternal objects
- [CRT / PLT / Loader Surface](crt-plt.md) — the RELRO mechanism that makes `.data.rel.ro` read-only after relocation, and the `dlopen`/`dlsym` PLT path the ucode/libnccom tables resolve through
- [Configuration: nrt_config and nrt_global_config](../runtime/config-structs.md) — the full 656-byte `nrt_config` and 296-byte `nrt_global_config_t` field layouts this page summarizes
- [TDRV Arch-Ops](../runtime/tdrv-arch-ops.md) — the 61-slot `tdrv_arch_ops` per-slot geometry and the `tdrv_arch_register_*` installers that fill it
- [HAL Adapter (kaena_khal)](../runtime/hal-adapter.md) — the ~245-slot `kaena_khal` HAL vtable and the `register_funcs_v{2,3,4}` arch fan-out
- [Collective Proxy Driver](../collectives/proxy-driver.md) — the consumer of `g_shared_proxy_queues` and the libnccom net-plugin function table
