# The libnrt ↔ libnccom ABI

> *Two binaries, pinned together. `libnrt.so.2.31.24.0` (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, SONAME `libnrt.so.1`) and `libnccom.so.2.31.24` (`aws-neuronx-collectives 2.31.24.0-1a31ba186`, build-id `9c00176c081788c9435d27d11bb40e92495463f0`, SONAME `libnccom.so.2`). Both ELF64 x86-64 **DYN, NOT stripped**. A third file, `libnccom-net.so` (build-id `3415f096d479e7d7bef506bb68bc0fa7551a654a`), exports the `ncclNetPlugin_v4/v5/v6` structs and is catalogued here only at its boundary edge — its internals are owned by [net-plugin](net-plugin.md). Every address on a FORWARD row is a `libnccom.so.2.31.24` text VMA; every address on a REVERSE row is a `libnrt.so.2.31.24.0` text VMA; `.text`/`.rodata` VMA == file offset in both. Definer-binary names appear demangled (`nm -D --defined-only`), user-binary references with their `@version` tag (`nm -D --undefined-only --with-symbol-versions`).*
>
> *Evidence grade: **Confirmed (byte-anchored, both sides re-derived)** — the 37 / 16 / 4 counts were re-run with `nm -D` on each binary independently; every FORWARD row is anchored to its `libnccom` definer address AND its `libnrt` `.bss` slot + `call *slot(%rip)` site; every REVERSE row to its `libnrt` `@@NRT_2.0.0` definer address AND its `libnccom` `@NRT_2.0.0` UND import; `readelf -d`/`-V` confirm the asymmetric link (no DT_NEEDED libnccom in libnrt; DT_NEEDED libnrt.so.1 + VERNEED NRT_2.0.0 in libnccom). · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

The runtime and the collectives library link to each other in **two opposite directions by two different mechanisms**, and this page is the symbol-by-symbol closure of that boundary. In the FORWARD direction (`libnrt` → `libnccom`) there is **no ELF link at all**: `libnrt.so` carries no `DT_NEEDED` on `libnccom` (its `NEEDED` list is nine toolchain libraries — `libgcc_s`, `libutil`, `librt`, `libpthread`, `libdl`, `libstdc++`, `libm`, `libc`, `ld-linux`), and it holds **zero** `neuron*` symbols in its dynamic table. Instead the embedded `ncclInit` (`libnrt 0x1bff30`) `dlopen`s `"libnccom.so"` with `RTLD_NOW` at runtime and `dlsym`s **37** `neuron*` entry points into a contiguous `.bss` function-pointer table (`libnrt 0xc967b8..0xc968d8`, 8-byte stride). Thirty-eight thin `nccl*` trampolines call *through* those slots (`call *0xc968d0(%rip)` etc.). The 37 targets are plain unversioned `T` exports in `libnccom`; the only handshake is a **numeric** one — `neuronGetVersionInfo` must report compatibility id `89`, checked at `libnrt 0x1bff9b` (`cmpq $0x59,…`) against `libnccom`'s `movq $0x59,…` (`0x464a8`).

In the REVERSE direction (`libnccom` → `libnrt`) the link is **hard and versioned**: `libnccom.so` carries `DT_NEEDED libnrt.so.1` and a `.gnu.version_r` entry requiring `(libnrt.so.1, NRT_2.0.0)` — and *only* `NRT_2.0.0`, never `NRT_3.0.0`. It imports **20** symbols, all tagged `@NRT_2.0.0`: **16** `nec_*` device/topology/proxy reverse-callbacks and **4** `nrt_*` runtime queries, every one resolving name-for-name against a `libnrt` `@@NRT_2.0.0` definition. The page is organized as one catalogue with three groups — FORWARD (37 `neuron*` dlsym targets), REVERSE (16 `nec_*` + 4 `nrt_*` versioned imports), and the NET edge (`ncclNetPlugin_v4/v5/v6`, which belong to `libnccom-net`'s `ncclNet_vN` ABI and are *not* part of the `nec_`/`neuron` boundary) — followed by the resolution-mechanism notes and the asymmetry quirk that a reimplementer must reproduce exactly.

For reimplementation, the contract of the boundary is:

- **The FORWARD table is a dlsym target list, not an export list.** A reimplementation of `libnccom` must define all 37 `neuron*` symbols as plain global text exports (no version node) so `libnrt`'s `dlsym` finds them; a reimplementation of `libnrt` must `dlopen`+`dlsym` them in order, NULL-check every result, and zero **all** 37 slots on the *first* failure (collectives degrade to all-or-nothing). There is no `DT_NEEDED` on this leg in either direction.
- **The REVERSE table is a versioned linker import.** A reimplementation of `libnccom` must `DT_NEEDED libnrt.so.1` and bind all 20 `nec_*`/`nrt_*` imports at `@NRT_2.0.0`; a reimplementation of `libnrt` must export those same 20 names at `@@NRT_2.0.0`. Dropping any one of the 20 from the `NRT_2.0.0` node breaks `libnccom` at *load* time (before any `dlopen`).
- **The two skew axes are independent.** The reverse leg is gated by the ELF symbol version (`NRT_2.0.0`); the forward leg is gated by the numeric compat id `89` returned by `neuronGetVersionInfo`. They are checked by different mechanisms at different times (link-time vs. first-call). Reproduce both, or one half of the boundary silently mis-negotiates.
- **The NET structs are a different ABI.** `ncclNetPlugin_v4/v5/v6` are exported by `libnccom-net.so`, follow the upstream `ncclNet_vN` C-struct-of-function-pointers shape, and reach `libnrt` only through the single `nrt_get_libnccl_net@NRT_2.0.0` handoff. Do not conflate them with the `neuron*` dlsym set.

## At a glance

| | |
|---|---|
| **Boundary** | `libnrt.so.1` ↔ `libnccom.so.2` (two packages, two build-ids) |
| **FORWARD** | 37 `neuron*` — `libnrt` `dlopen`+`dlsym`, **no version**, no `DT_NEEDED` |
| **REVERSE** | 16 `nec_*` + 4 `nrt_*` = 20 — `libnccom` linker imports, all **`@NRT_2.0.0`** |
| **NET (edge)** | `ncclNetPlugin_v4/v5/v6` in `libnccom-net.so` — `ncclNet_vN` ABI, *not* the `nec_`/`neuron` set |
| **FORWARD definer** | `libnccom.so.2.31.24` `.text` `0x46150..0x49ac0` (plain `T`, no `@`) |
| **FORWARD user** | `libnrt` `.bss` slot table `0xc967b8..0xc968d8` (8-byte stride, 37 slots) |
| **REVERSE definer** | `libnrt.so.2.31.24.0` `.text` `0x1bfd40..0x1bff20` (`nec_`) + `0x83e10/0x84210/0x940b0/0x94400` (`nrt_`), all `@@NRT_2.0.0` |
| **REVERSE user** | `libnccom` UND imports, all `@NRT_2.0.0` |
| **Forward gate** | numeric compat id `89` (`0x59`): `libnrt 0x1bff9b cmpq` ↔ `libnccom 0x464a8 movq` |
| **Reverse gate** | ELF version `NRT_2.0.0` (`libnccom` VERNEED idx 3 → `libnrt` version_d node 2) |
| **Forward entry** | `ncclInit` `@libnrt 0x1bff30` (`dlopen "libnccom.so", RTLD_NOW`) |
| **Re-derived counts** | `nm -D`: FORWARD 37 · REVERSE 16 `nec_` + 4 `nrt_` (20 @NRT_2.0.0) · NET 3 |

---

## Resolution Mechanisms

The two halves of the boundary do not just point in opposite directions — they are resolved by two unrelated dynamic-linker facilities, and conflating them is the classic reimplementation error.

### FORWARD — runtime `dlopen` + `dlsym`, unversioned

`libnrt` never names `libnccom` in its ELF. `readelf -d libnrt.so` lists no `NEEDED libnccom`, and `nm -D libnrt.so | grep neuron` is empty — there is no `neuron*` symbol, defined or undefined, in the runtime's dynamic table. The link is forged at runtime: `ncclInit` (`0x1bff30`) calls `dlopen("libnccom.so", RTLD_NOW)`, then `dlsym`s each `neuron*` name out of the returned handle and stores the resolved pointer into a fixed `.bss` slot. Because `dlsym` matches on **name only**, the 37 targets are exported from `libnccom` as plain `T` globals with no `@version` tag — and indeed `nm -D --defined-only libnccom.so | grep neuron` shows all 37 as bare `T`, none versioned. The `nccl*` trampolines that the rest of `libnrt` calls are one-instruction indirect jumps through the slot: e.g. `ncclGetUniqueId @0x1c0a80 : call *0xad5e4a(%rip) # 0xc968d0 <neuronGetUniqueId>` and `ncclInitComm @0x1c0e57 : call *0xad5a63(%rip) # 0xc968c0 <neuronInitComm>` (both re-read from `objdump -d`).

### REVERSE — hard `DT_NEEDED` + versioned import

`libnccom` *does* name `libnrt`: `readelf -d libnccom.so` shows `NEEDED libnrt.so.1`, and `readelf -V` shows a `.gnu.version_r` entry `File: libnrt.so.1  Cnt: 1  Name: NRT_2.0.0` (version index 3). Every `nec_*`/`nrt_*` import therefore arrives at the dynamic linker tagged `@NRT_2.0.0` and is resolved by the standard symbol-versioning rule against `libnrt`'s `@@NRT_2.0.0` definitions — at **load** time, before `ncclInit` ever runs. There is exactly **one** required version node from `libnrt` (`NRT_2.0.0`); `libnccom` requires nothing from `NRT_3.0.0` (the `nrta_*` async family), so a newer `libnrt` that keeps `NRT_2.0.0` stays load-compatible.

> **QUIRK —** the boundary is asymmetric by mechanism, not just by direction. FORWARD is a *runtime* `dlsym` keyed on bare name with **no ELF version** and a separate *numeric* compat gate (id `89`); REVERSE is a *load-time* linker bind keyed on **`NRT_2.0.0`** with no numeric gate. Exporting a `nec_*` callback *un*versioned breaks `libnccom`'s load outright — its import is `@NRT_2.0.0` and an unversioned definition does not satisfy a versioned need. Versioning a `neuron*` export, by contrast, changes nothing the forward leg observes: `dlsym` ignores the version and binds the default. The same mistake fails loudly on one leg and silently passes on the other.

---

## The ABI Catalogue

One row per symbol. **Direction** is FORWARD (`libnrt`→`libnccom`, dlsym), REVERSE (`libnccom`→`libnrt`, versioned import), or NET (`libnccom-net`→host, `ncclNet_vN`). **Definer** is the binary that holds the `T`/`D` definition and its address; **User** is the binary that resolves/calls it. Signatures are from the definer-side DWARF; `NRT_STATUS` is the runtime's 28-value status enum. Every row is `HIGH` (byte-anchored on both sides) unless tagged.

### FORWARD — 37 `neuron*` dlsym targets (definer `libnccom`, user `libnrt`)

The version probe (`neuronGetVersionInfo`) is `dlsym`'d first and gates the rest. The `.bss` slots descend from `0xc968d8` (probe) to `0xc967b8` (last) in 8-byte steps; each row gives the `libnccom` definer addr, the `libnrt` `.bss` slot, and the `libnrt` `nccl*` trampoline that calls through it.

| Symbol | Definer addr (libnccom) | libnrt .bss slot | libnrt trampoline | Role / signature | Conf |
|---|---|---|---|---|---|
| `neuronGetVersionInfo` | `0x46150` | `0xc968d8` | `ncclGetVersionInfo 0x1c0a10` | version/compat probe (gate #0); `void(nec_version_info_t*, uint32_t n=56)` — writes compat=89 @off48 | HIGH |
| `neuronGetUniqueId` | `0x46660` | `0xc968d0` | `ncclGetUniqueId 0x1c0a50` | `NRT_STATUS(char id[128], int rank_n, const char *hint)` | HIGH |
| `neuronInitGlobalComm` | `0x46c40` | `0xc968c8` | `ncclInitGlobalComm 0x1c0c30` | `NRT_STATUS(int g_dev_id, int g_dev_count, const enc_neuron_device_info_t*, const char *root_id, void **comm, const char *hint)` | HIGH |
| `neuronInitComm` | `0x46d90` | `0xc968c0` | `ncclInitComm 0x1c0e10` | `NRT_STATUS(void **comm, int rank_n, char *id, int rank, const enc_neuron_device_info_t*, bool bypass_graph)` | HIGH |
| `neuronBuildGraphComm` | `0x46e90` | `0xc968b8` | `ncclBuildGraphComm 0x1c0ff0` | `NRT_STATUS(void *comm)` — post-init graph build | HIGH |
| `neuronGetCommInfo` | `0x46ec0` | `0xc968b0` | `ncclGetCommInfo 0x1c11b0` | `NRT_STATUS(void *comm, nccl_comm_info_t *out)` — fills 1376B contract | HIGH |
| `neuronFreeComm` | `0x47f90` | `0xc968a8` | `ncclFreeComm 0x1c1370` | `NRT_STATUS(void *comm)` — teardown (= ncclCommDestroy) | HIGH |
| `neuronExpandConnectivity` | `0x47fe0` | `0xc968a0` | `ncclExpandConnectivity 0x1c1530` | `NRT_STATUS(void *comm, enc_net_connectivity_t)` | HIGH |
| `neuronStartNetworkProxy` | `0x483a0` | `0xc96898` | `ncclStartNetworkProxy 0x1c16f0` | `NRT_STATUS(void *comm, u32, u32, net_ops_info_t*, int, net_src_addr_t*, int, net_dest_addr_t*, int)` | HIGH |
| `neuronStopNetworkProxy` | `0x487d0` | `0xc96890` | `ncclStopNetworkProxy 0x1c18e0` | `NRT_STATUS(void *comm, uint32_t)` | HIGH |
| `neuronNetworkProxyProgress` | `0x48930` | `0xc96888` | `ncclNetworkProxyProgress 0x1c1aa0` | `NRT_STATUS(void *comm, u32, u32, bool*, bool*)` | HIGH |
| `neuronBootstrapSend` | `0x489a0` | `0xc96880` | `ncclBootstrapSend 0x1c1af0` | `NRT_STATUS(void *state, int peer, void *data, size_t, const char *hint)` | HIGH |
| `neuronBootstrapRecv` | `0x489d0` | `0xc96878` | `ncclBootstrapRecv 0x1c1cc0` | `NRT_STATUS(void *state, int peer, void *data, size_t, const char *hint)` | HIGH |
| `neuronBootstrapAllGather` | `0x48a00` | `0xc96870` | `ncclBootstrapAllGather 0x1c1e90` | `NRT_STATUS(void *state, void *allData, size_t, const char *hint)` | HIGH |
| `neuronNetRegMr` | `0x48a30` | `0xc96858` | `ncclNetRegMr 0x1c2070` | `NRT_STATUS(void*, enc_pattern_t, int, bool, ofi_comm_type_t, void*, size_t, nccl_ptr_type, void**)` | HIGH |
| `neuronNetDeregMr` | `0x48d50` | `0xc96850` | `ncclNetDeregMr 0x1c2260` | `NRT_STATUS(void*, enc_pattern_t, int, bool, ofi_comm_type_t, void*)` | HIGH |
| `neuronGetPeerDeviceId` | `0x48ff0` | `0xc96848` | `ncclGetPeerDeviceId 0x1c2440` | `NRT_STATUS(void *comm, int peer, int *out)` | HIGH |
| `neuronSetAffinity` | `0x49020` | `0xc96840` | `ncclSetAffinity 0x1c2620` | `NRT_STATUS(void *comm, cpu_set_t *override, cpu_set_t *save, int core)` | HIGH |
| `neuronGetNetConnector` | `0x49070` | `0xc96838` | `ncclGetNetConnector 0x1c2800` | `NRT_STATUS(void *comm, enc_pattern_t, int, bool, void**)` | HIGH |
| `neuronPreferredNcRidNic` | `0x46810` | `0xc96868` | `ncclPreferredNcRidNic 0x1c29d0` | `NRT_STATUS(u32, u32, int *out)` | HIGH |
| `neuronPreferredNcRidNicBdf` | `0x46710` | `0xc96860` | `ncclPreferredNcRidNicBdf 0x1c2bb0` | `NRT_STATUS(u32, u32, char *bdf, size_t)` | HIGH |
| `neuronInitNet` | `0x49220` | `0xc96830` | `ncclInitNet 0x1c2d90` | `NRT_STATUS(char *root_comm_id)` — net-plugin lifecycle init | HIGH |
| `neuronFiniPlugin` | `0x49240` | `0xc96828` | `ncclFiniPlugin 0x1c2f50` | `NRT_STATUS(void)` — net-plugin teardown | HIGH |
| `neuronGetOFIHandle` | `0x49260` | `0xc96820` | `ncclGetOFIHandle 0x1c3110` | `NRT_STATUS(void **out)` | HIGH |
| `neuronNetListen` | `0x49280` | `0xc96818` | `ncclNetListen 0x1c32d0` | `NRT_STATUS(int, void*, void**)` | HIGH |
| `neuronNetConnect` | `0x49320` | `0xc96810` | `ncclNetConnect 0x1c34b0` | `NRT_STATUS(int, void*, void**)` | HIGH |
| `neuronNetAccept` | `0x493c0` | `0xc96808` | `ncclNetAccept 0x1c3690` | `NRT_STATUS(void*, void**)` | HIGH |
| `neuronNetRegMrOG` | `0x49460` | `0xc96800` | `ncclNetRegMrOG 0x1c3850` | `NRT_STATUS(void*, void*, size_t, int, void**)` | HIGH |
| `neuronNetGetMrKey` | `0x49500` | `0xc967f0` | `ncclNetGetMrKey 0x1c3be0` | `NRT_STATUS(void*, uint64_t *key)` | HIGH |
| `neuronNetDeregMrOG` | `0x495a0` | `0xc967f8` | `ncclNetDeregMrOG 0x1c3a20` | `NRT_STATUS(void*, void*)` | HIGH |
| `neuronNetIsend` | `0x49640` | `0xc967e8` | `ncclNetIsend 0x1c3da0` | `NRT_STATUS(void*, void*, int, void*, void**)` | HIGH |
| `neuronNetIrecv` | `0x496f0` | `0xc967e0` | `ncclNetIrecv 0x1c3f70` | `NRT_STATUS(void*, void*, int, void*, void**)` | HIGH |
| `neuronNetIflush` | `0x497f0` | `0xc967d8` | `ncclNetIflush 0x1c4140` | `NRT_STATUS(void*, void*, int, void*, void**)` | HIGH |
| `neuronNetTest` | `0x498e0` | `0xc967d0` | `ncclNetTest 0x1c4310` | `NRT_STATUS(void*, int *done, int *size)` | HIGH |
| `neuronNetCloseSend` | `0x49980` | `0xc967c8` | `ncclNetCloseSend 0x1c44f0` | `NRT_STATUS(void*)` | HIGH |
| `neuronNetCloseRecv` | `0x49a20` | `0xc967c0` | `ncclNetCloseRecv 0x1c46b0` | `NRT_STATUS(void*)` | HIGH |
| `neuronNetCloseListen` | `0x49ac0` | `0xc967b8` | `ncclNetCloseListen 0x1c4870` | `NRT_STATUS(void*)` — last slot (table base) | HIGH |

**FORWARD count = 37**, re-derived: `nm -D --defined-only libnccom.so.2.31.24 | rg -c ' neuron'` → `37`; every addr above matches the `nm` dump, and `libnrt`'s dynamic table holds **no** `neuron*` symbol (forward leg is pure `dlopen`+`dlsym`, confirmed).

> **NOTE —** `libnccom` exports these 37 as bare `T` with no `@version` — there is no `.gnu.version_d` node for them at all (it consumes `libnrt`'s `NRT_2.0.0` node and defines none of its own). The version tag a reimplementer might expect on a "collectives ABI" simply does not exist on the forward leg; the only forward compatibility token is the numeric `89`.

### REVERSE — 4 `nrt_*` runtime queries (definer `libnrt`, user `libnccom`)

All four are `libnrt` `@@NRT_2.0.0` exports; `libnccom` imports them `@NRT_2.0.0`. Addresses are `libnrt` text VMAs.

| Symbol | Definer addr (libnrt) | Version | Role / signature | Conf |
|---|---|---|---|---|
| `nrt_get_total_vnc_count` | `0x83e10` | `@@NRT_2.0.0` | bootstrap gate — `NRT_STATUS(uint32_t *out)`; `bootstrapInit/Flat/CreateRoot` require `==0` to run bootstrap | HIGH |
| `nrt_get_instance_info` | `0x84210` | `@@NRT_2.0.0` | pervasive topology metadata — `NRT_STATUS(nrt_instance_info_t *out, size_t n)`; family/size/arch_name/device_revision | HIGH |
| `nrt_get_version` | `0x940b0` | `@@NRT_2.0.0` | `NRT_STATUS(nrt_version_t *out, size_t n)` — fills the 16-byte cross-process sanity hash | HIGH |
| `nrt_get_libnccl_net` | `0x94400` | `@@NRT_2.0.0` | net-plugin handoff — `void *nrt_get_libnccl_net(int *errno_out, char *errmsg, size_t n)`; returns the `libnccl_net_handle` (`libnrt .bss 0xc96628`) that `libnrt` resolved by dlopening `libnccom-net.so` | HIGH |

### REVERSE — 16 `nec_*` device/topology/proxy callbacks (definer `libnrt`, user `libnccom`)

All `libnrt` `@@NRT_2.0.0` exports; thin thunks into `libnrt`'s NDL/encd layer (target named where traced). `libnccom` imports them `@NRT_2.0.0`.

| Symbol | Definer addr (libnrt) | libnrt impl | Role / signature | Conf |
|---|---|---|---|---|
| `nec_inc_semaphore` | `0x1bfd40` | inline `mov %esi,(%rdi); ret` | proxy completion signalling — `void(volatile uint32_t *sem, uint32_t val)` | HIGH |
| `nec_get_device_count` | `0x1bfd50` | `jmp ndl_available_devices 0xc2180` | `int(int *out, uint32_t)` — direction-proof thunk | HIGH |
| `nec_get_device_pci_bdf` | `0x1bfd60` | `call ndl_get_device_bdf_ext 0xc4b00` | `int(int dev, u32 *dom, u32 *bus, u8 *slot, u8 *func)` | HIGH |
| `nec_get_virtual_core_size` | `0x1bfdd0` | `jmp encd_get_virtual_core_size 0x242960` | `NRT_STATUS(uint32_t *out)` — most-used callback (topo/device sizing) | HIGH |
| `nec_build_port_and_rid_map` | `0x1bfdf0` | `jmp encd_arch_build_port_and_rid_map 0x256540` | `NRT_STATUS(int, int*, int*, int)` | HIGH |
| `nec_is_mla_available` | `0x1bfe00` | `jmp encd_arch_is_mla_available 0x256940` | `bool(int, int)` | HIGH |
| `nec_mla_idx_to_rid` | `0x1bfe10` | `jmp encd_arch_mla_idx_to_rid` | `int(int, int)` | HIGH |
| `nec_rid_to_mla_idx` | `0x1bfe20` | `jmp encd_arch_rid_to_mla_idx` | `int(int, int)` | HIGH |
| `nec_get_peer_mla_idx` | `0x1bfe30` | re-enters `nrt_get_instance_info` (PLT `0x3d270`) | `int(int, int, int)` — bounded re-entry, no further `nec_` | HIGH |
| `nec_get_p2p_pod_peer_node` | `0x1bfeb0` | `call encd_get_p2p_pod_peer_node 0x256010` | `int(u32, int, u32, int*)` | HIGH |
| `nec_pod_node_can_access_peer_node` | `0x1bfed0` | encd pod helper | `NRT_STATUS(nec_pod_type_t, u32, u32, u32, u32, int*)` | HIGH |
| `nec_ndl_printk` | `0x1bfee0` | `jmp ndl_printk 0xc4ca0` | `void(char *fmt, u32, u32)` — routed into `ncclDebugLog` nccom-side | HIGH |
| `nec_get_dynamic_send_size_bytes` | `0x1bfef0` | `jmp enc_get_send_count_size_bytes 0xfe980` | `unsigned long(enc_host_mem_t*, size_t, int, int)` | HIGH |
| `nec_get_dynamic_send_offset_bytes` | `0x1bff00` | `jmp enc_get_send_offset_bytes 0xfe960` | `unsigned long(enc_host_mem_t*, size_t, int, int)` | HIGH |
| `nec_get_dynamic_recv_offset_bytes` | `0x1bff10` | `jmp enc_get_recv_post_offset_bytes 0xfe9a0` | `unsigned long(enc_host_mem_t*, size_t, int, int)` | HIGH |
| `nec_set_recv_size_bytes` | `0x1bff20` | `jmp enc_set_recv_size_bytes 0xfe9e0` | `void(enc_host_mem_t*, size_t, size_t, int, int)` | HIGH |

**REVERSE count = 16 `nec_` + 4 `nrt_` = 20**, re-derived: `nm -D --undefined-only libnccom.so.2.31.24 | rg -c ' nec_'` → `16`; `… | rg -c ' nrt_'` → `4`; `… --with-symbol-versions | rg -c '@NRT_2.0.0'` → `20` (and `… | rg 'NRT_3.0.0'` → empty). Each name resolves against a `libnrt @@NRT_2.0.0` definition at the address above (`nm -D --defined-only --with-symbol-versions libnrt.so.2.31.24.0`).

> **NOTE —** the `libnrt`-local `nec_get_version_info` (`0x1bfde0`, `jmp ncclGetVersionInfo 0x1c0a10`) and the `nec_vil_*` helpers are **not** in the dynamic symbol table and are correctly absent from `libnccom`'s import list — they are intra-`libnrt` calls, not boundary symbols. The catalogue is the dynsym boundary only.

### NET (edge) — `ncclNetPlugin_v4/v5/v6` (definer `libnccom-net`)

Catalogued here only to fix the distinction: these are **not** part of the `nec_`/`neuron` boundary. They are the upstream `ncclNet_vN` plugin ABI — flat C structs of function pointers in `libnccom-net.so`'s `.data`, dlopened by `libnrt` (whose handle reaches `libnccom` via `nrt_get_libnccl_net`, the REVERSE row above). `libnccom` binds `ncclNetPlugin_v6` only.

| Symbol | Definer addr (libnccom-net) | Kind | Role | Conf |
|---|---|---|---|---|
| `ncclNetPlugin_v4` | `0x43760` | `D` (16-slot struct) | legacy `ncclNet_v4` vtable; exported, no located host bind | HIGH |
| `ncclNetPlugin_v5` | `0x437e0` | `D` (24-slot struct) | `ncclNet_v5` vtable; exported, no located host bind | HIGH |
| `ncclNetPlugin_v6` | `0x438a0` | `D` (22-slot struct) | `ncclNet_v6` vtable — the **bound** struct (`dlsym("ncclNetPlugin_v6")`, `init@+8`, `devices@+24`) | HIGH |

**NET count = 3**, re-derived: `nm -D --defined-only libnccom-net.so | rg -c ncclNetPlugin` → `3`. These three are data objects (`D`), not text — distinguishing them at a glance from the 37 `neuron*` `T` functions and the 20 `nec_`/`nrt_` `T` callbacks.

---

## Counts — re-derived from `nm` on both binaries

| Claim | Command | Result | Verdict |
|---|---|---|---|
| FORWARD 37 | `nm -D --defined-only libnccom.so.2.31.24 \| rg -c ' neuron'` | `37` | matches |
| FORWARD has no version | `nm -D --defined-only --with-symbol-versions libnccom… \| rg ' neuron.*@'` | (empty) | bare `T`, confirmed |
| libnrt holds no `neuron*` | `nm -D libnrt.so.2.31.24.0 \| rg ' neuron'` | (empty) | dlsym-only, confirmed |
| REVERSE 16 `nec_` | `nm -D --undefined-only libnccom… \| rg -c ' nec_'` | `16` | matches |
| REVERSE 4 `nrt_` | `nm -D --undefined-only libnccom… \| rg -c ' nrt_'` | `4` | matches |
| REVERSE all `@NRT_2.0.0` | `nm -D --undefined-only --with-symbol-versions libnccom… \| rg -c '@NRT_2.0.0'` | `20` | 16+4 = 20, confirmed |
| REVERSE no `NRT_3.0.0` | `… \| rg 'NRT_3.0.0'` | (empty) | binds NRT_2.0.0 only |
| NET 3 | `nm -D --defined-only libnccom-net.so \| rg -c ncclNetPlugin` | `3` | v4/v5/v6, confirmed |
| no `DT_NEEDED libnccom` in libnrt | `readelf -d libnrt… \| rg NEEDED` | 9 toolchain libs | no libnccom, confirmed |
| `DT_NEEDED libnrt.so.1` in libnccom | `readelf -d libnccom… \| rg NEEDED` | includes `libnrt.so.1` | confirmed |
| libnccom VERNEED `NRT_2.0.0` | `readelf -V libnccom… \| rg -A1 libnrt.so.1` | `Name: NRT_2.0.0` (idx 3) | confirmed, sole NRT need |

All three headline counts hold: **37 forward / 16 `nec_` + 4 `nrt_` reverse / 3 net**. No CORRECTION to the source figures.

## Cross-References

- [The libnrt ↔ libnccom Boundary (nec_ / nccl)](../collectives/nccl-boundary.md) — the conceptual boundary and the `nec_*` thunk semantics; this page is its symbol-level catalogue
- [The Net Plugin (ncclNet_v6)](net-plugin.md) — owns the `ncclNetPlugin_v4/v5/v6` structs and the `nrt_get_libnccl_net` handoff the NET edge points at
- [Communicator Init and Bootstrap](comm-init.md) — the forward call ordering (`neuronInitComm`/`neuronGetUniqueId`/bootstrap) and the compat-89 handshake in flight
- [Proxy & Progress Engine (and the libnrt Bridge)](proxy-engine.md) — the `ncclStart/Progress/Stop` seam and the `nec_inc_semaphore` proxy-completion callback
- [Symbol-Version Manifest](../appendix/symbol-versions.md) — `libnrt`'s `NRT_2.0.0` / `NRT_3.0.0` version nodes and the full exported-symbol inventory
