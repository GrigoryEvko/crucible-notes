# The Net Plugin (ncclNet_v6)

> *All addresses on this page apply to `libnccom-net.so` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `3415f096d479e7d7bef506bb68bc0fa7551a654a`; SONAME `libnccom-net.so`). ELF64 x86-64, **DYN, NOT stripped** — full `.symtab`, 322 `t`/`T` text functions. `.text`/`.rodata` VMA == file offset, so every `0x…` text/rodata address is both a file offset and an analysis VMA; the exported plugin structs live in `.data` (sh_addr `0x433c0`, sh_offset `0x423c0`) and `.data.rel.ro` (sh_addr `0x42d10`, sh_offset `0x41d10`), a constant **`+0x1000` VMA − fileoffset delta** — subtract `0x1000` to `xxd` a struct. The plugin reaches the fabric through `DT_NEEDED libfabric.so.1` (versioned `FABRIC_1.0`/`1.1`/`1.8`) and reads PCIe topology through `DT_NEEDED libhwloc.so.15`. Source-file evidence (demangled symbols / build-string): `nccl_ofi_net.cpp`, `nccl_ofi_rdma.cpp`, `nccl_ofi_sendrecv.cpp`, `platform_aws.cpp`; internal tree `/opt/workspace/KaenaNCCL/build/develop/share/aws-ofi-nccl/xml`.*
> *Evidence grade: **Confirmed (byte-anchored)** — build-id, SONAME, NEEDED, libfabric versioned imports, and the three exported structs' `R_X86_64_RELATIVE` slot tables re-read from `objdump -R`; the base-class `call *N(reg)` dispatch offsets, the `fi_version > 0x10015` gate, the loader `dlsym`+`init@+8`/`devices@+24` sequence in `libnccom.so`, and the vtable-install stores in `plugin_init`/`endpoint_init` read from `objdump -d`. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

`libnccom-net.so` is the **EFA/libfabric net plugin** that `libnccom.so` (the NCCL fork) dlopens to reach the inter-node fabric. This page owns its **control plane** — the part that publishes the plugin to the host, discovers EFA devices, negotiates the libfabric provider, and wires up `listen`/`connect`/`accept`. The per-op **data path** that those wireups produce — `send_progress`, `ofi_process_cq`, RDMA-WRITE-with-immediate, the rail/req/cq internals — is the sibling [transport-efa](transport-efa.md); this page deliberately stops at the ABI shim and the base-class dispatch and hands off there. If you already hold a mental model of upstream **aws-ofi-nccl**'s plugin layer — a `ncclNet_vN` struct of C function pointers, a `nccl_net_ofi_plugin`→`device`→`domain`→`endpoint` object hierarchy, a provider filter that keeps only `"efa"`, and a `getProperties` that fills a `ncclNetProperties` from `fi_info` — then you already know the shape; the fork keeps it nearly intact and re-homes the EC2-specific tuning in `platform_aws.cpp`.

The plugin exports the classic NCCL net-plugin ABI as **three** C structs — `ncclNetPlugin_v4` (`0x43760`, 16 slots), `ncclNetPlugin_v5` (`0x437e0`, 24 slots), `ncclNetPlugin_v6` (`0x438a0`, 22 slots) — each a flat array of `R_X86_64_RELATIVE`-relocated function pointers in `.data`. `libnccom.so` binds **exactly** `ncclNetPlugin_v6`: its loader (`initNetPlugin` `@0x417a0` in `libnccom.so`) calls `nrt_get_libnccl_net@NRT_2.0.0` to dlopen this `.so`, `dlsym`s the single symbol `"ncclNetPlugin_v6"`, then calls `init` at struct `+8` and `devices` at struct `+24`. Behind the exported pointers sit thin **ABI shims** (`listen_v5`, `connect_v10`, `accept_v5`, `devices_v2`, …) that null-check a singleton `plugin`, walk `plugin->get_device(dev) → device->get_ep(&ep)`, and invoke the protocol's virtual op (`ep->listen` / `ep->connect` / `listen_comm->accept` / `device->get_properties`). The protocol behind those virtuals is chosen once at init: **RDMA** (`nccl_ofi_rdma.cpp`, the EFA production transport) or **SENDRECV** (`nccl_ofi_sendrecv.cpp`, tagged), selected by `OFI_NCCL_PROTOCOL`, the platform default, or a two-way auto-probe.

The page is organized as the control-plane spine a reimplementer must reproduce: the **three exported vtables** and how the host binds v6; the **`nccl_net_ofi_{plugin,device,domain,ep}` base-class hierarchy** and its virtual-dispatch chain; the **init/discovery flow** (`create_plugin` → protocol init → `fi_getinfo`("efa") → version gate → self-test); and the **GDR/dmabuf registration policy**. The data path each `listen`/`connect`/`accept` opens — and the RDMA-WRITE-with-immediate it carries — is documented end-to-end on [transport-efa](transport-efa.md), which this page links rather than duplicates.

For reimplementation, the contract of the control plane is:

- **The three exported struct layouts and the v6 bind.** A reimplementer must publish `ncclNetPlugin_v6` (22 slots, `name`/`init`/`fini`/`devices`/`getProperties`/`listen`/`connect`/`accept`/… in canonical `ncclNet_v6` order) as a single dlsym-resolvable symbol; the host binds v6 *only*, calling `init` at `+8` and `devices` at `+24`. The v5 and v4 structs are exported (v5 = v6 minus `fini`; v4 = v5 minus the RMA/dmabuf tail) but no host bind site for them was located.
- **The base-class dispatch chain.** Every ABI shim resolves the same way: null-check `plugin`, `plugin->get_device(dev)` (`plugin+16`), then either `device->get_properties` (`device+40`) or `device->get_ep(&ep)` (`device+64`) → `domain->get_ep` (`domain+8`) → the protocol's `ep->listen`/`ep->connect` (`ep+8`/`ep+16`), with `ep->release` (`ep+24`) on error. `accept` is the exception — it lives on the `listen_comm` (`+24`), not the ep.
- **The init/discovery sequence and the EFA gate.** `create_plugin` selects the protocol, runs its init (which builds `fi_info` hints via `fi_dupinfo` and calls `fi_getinfo` with `provider_filter="efa"`), requires `fi_version() > 0x10015` (≥ libfabric 1.22.0), then runs a `get_device(0)`/`get_ep`/`get_properties` **self-test** before returning the singleton.
- **The GDR/dmabuf policy.** `support_gdr` is initialized 0 on this Trainium build; DMA-BUF is gated by `OFI_NCCL_DISABLE_DMABUF` + a kernel-version uname check **and** an EFA PCI-ID match (`0xefa0`/`0xefa1`/`0xefa2`); MR-key ownership is decided by `mr_keys_need_own_key` from the `fi_info` caps/mr_mode. When GDR is unsupported the plugin forces `NCCL_PROTO=simple`.

## At a glance

| | |
|---|---|
| **What it is** | Inter-node EFA/libfabric net plugin — control plane (init / discovery / wireup) |
| **Plugin name** | `"AWS Libfabric"` (`0x3b470`); provider `"efa"`; libfabric ≥ 1.22.0 |
| **Exported ABI** | `ncclNetPlugin_v6` `.data 0x438a0` (22 slots) · `_v5 0x437e0` (24) · `_v4 0x43760` (16) |
| **Bound struct** | **v6 only** — `dlsym("ncclNetPlugin_v6")`, `init@+8`, `devices@+24` |
| **Loader (host side)** | `initNetPlugin` `@0x417a0` in `libnccom.so` → `nrt_get_libnccl_net@NRT_2.0.0` → `dlsym` |
| **Init top** | `nccl_net_ofi_create_plugin` `0xabe0` — protocol select + self-test |
| **Protocols** | RDMA `nccl_net_ofi_rdma_init` `0x171d0` · SENDRECV `nccl_net_ofi_sendrecv_init` `0x10960` |
| **Base classes** | `plugin` (`init 0xa610`) → `device` (`0xa6b0`) → `domain` → `ep` (`endpoint_init 0xaa40`) |
| **Provider gate** | `query_provider_capabilities` `0xa4f0` — `"efa"` name + `fi_version()>0x10015` (≥ 1.22.0) |
| **Platform** | `platform_init` `0x2cc00` (`provider_filter="efa"`) · `platform_config_endpoint` `0x2cf80` |
| **Discovery** | `ofiutils_get_providers` `0x26b80` → `fi_getinfo@FABRIC_1.8`; hints via `fi_dupinfo@FABRIC_1.8` |
| **dmabuf gate** | `dmabuf_viable` `0x276b0` = `!OFI_NCCL_DISABLE_DMABUF && kernel_ok 0x275e0` && EFA-PCI match |
| **GDR state** | `support_gdr` initialized **0** on this build; `!GDR → NCCL_PROTO=simple` (`configure_nccl_proto_simple 0xab20`) |
| **EFA PCI IDs** | `0xefa0` / `0xefa1` / `0xefa2` (rodata; dmabuf gate in `info_properties 0xa070`) |
| **NIC boundary** | `libfabric.so.1` — 6 versioned PLT syms + per-`fid` inline op vectors |

> **QUIRK — the host binds *one* version, and it binds it by symbol name, not by `getVersion`.** Upstream NCCL probes for the highest `ncclNetPlugin_vN` symbol the plugin exports. This fork's loader (`initNetPlugin` `@0x417a0` in `libnccom.so`) is constant-folded to a **single** `dlsym(handle, "ncclNetPlugin_v6")`; on miss it `dlclose`s and logs `"Failed to find ncclNetPlugin_v6 symbol."` — there is **no** v5/v4 fallback in the observed bind path. The plugin still *exports* `ncclNetPlugin_v4` (`0x43760`) and `_v5` (`0x437e0`) for ABI completeness, but nothing in `libnccom.so` resolves them. A reimplementer who ships only v4/v5, or who relies on the host falling back, will fail to load against this runtime. Conversely, a reimplementer who ships v6 must place `init` at struct `+8` and `devices` at struct `+24` exactly — the loader calls `*0x8(%rax)` and `*0x18(%rbx)` by hard offset (`@0x41895`/`@0x418a5`), it does not read field names.

---

## 1. The Exported ABI: Three Structs, One Bind

### Purpose

The plugin's entire public surface is three `.data` arrays of relocated function pointers. This unit pins their layout slot-by-slot (from the `R_X86_64_RELATIVE` table), shows which one the host binds and how, and fixes the canonical `ncclNet_vN` slot order so a reimplementer can publish a byte-compatible plugin. The function *bodies* behind these pointers are the ABI shims of §2 (control plane) and the data-path ops of [transport-efa](transport-efa.md); this unit is only the table.

### The bind handshake

```text
libnccom.so  initNetPlugin @0x417a0   (net_plugin.cc)
  uname() → parse "maj.min" (strtok "." ; strtol)         @0x417cd/0x417f1/0x41809
     if kernel < 5.14 && !getenv("FI_EFA_FORK_SAFE"):
        warn "Linux kernel %d.%d requires setting FI_EFA_FORK_SAFE=1 … Multi-node support will be disabled."
  handle = nrt_get_libnccl_net(&handle, buf)              @0x41856   ── this dlopens libnccom-net.so
     on fail: log "NET/Plugin: libnccom-net.so load returned %d"
  sym = dlsym(handle, "ncclNetPlugin_v6")                 @0x41871
     if NULL: dlclose(handle); log "Failed to find ncclNetPlugin_v6 symbol."
  (*sym->init)(ncclDebugLog)        == call *0x8(%rax)    @0x41895   ── v6 init  slot (+8)
  (*sym->devices)(&ndev)            == call *0x18(%rbx)   @0x418a5   ── v6 devices slot (+24)
     on fail: log "OFI plugin initNet() failed is EFA enabled?"
  ── thereafter: getProperties/listen/connect/accept/regMr/isend/… through the v6 vtable
```

Direction: `libnccom.so` is the **caller**, `libnccom-net.so` the **callee**. The conn handles (EFA endpoint names) the host feeds back into `connect`/`accept` are exchanged out-of-band by libnccom's bootstrap; their wire layout is owned by [transport-efa](transport-efa.md).

### The ncclNetPlugin_v6 vtable

`.data 0x438a0`, 22 slots, all `R_X86_64_RELATIVE` (verified slot-by-slot against `objdump -R`). The last reloc is `0x43948`; canonical `ncclNet_v6` field order. The first eight slots are control plane (this page); slots `+64` onward are the data path ([transport-efa](transport-efa.md)).

| Slot | Field | Target | Role | Confidence |
|---|---|---|---|---|
| `+0` | `name` | `0x3b470` `"AWS Libfabric"` | plugin display name | HIGH |
| `+8` | `init` | `init_no_atexit_fini_v6` `0x59a0` | register logger, `create_plugin` (no atexit-fini) — **host calls this** | HIGH |
| `+16` | `fini` | `fini_v6` `0x5b00` | plugin teardown (v6-only slot) | HIGH |
| `+24` | `devices` | `devices_v2` `0x5c40` | `*ndev = plugin->get_num_devices()` — **host calls this** | HIGH |
| `+32` | `getProperties` | `getProperties_v5` `0x2e420` | fill `ncclNetProperties_v6_t` (→ §3) | HIGH |
| `+40` | `listen` | `listen_v5` `0x5f00` | passive open (→ §2) | HIGH |
| `+48` | `connect` | `connect_v5` `0x6690` | active open (→ §2) | HIGH |
| `+56` | `accept` | `accept_v5` `0x6ab0` | passive accept (→ §2) | HIGH |
| `+64` | `regMr` | `regMr_v8` `0x7000` | register MR — *data path* | HIGH |
| `+72` | `regMrDmaBuf` | `regMrDmaBuf_v6` `0x7310` | register dmabuf MR — *data path* | HIGH |
| `+80` | `deregMr` | `deregMr_v2` `0x7670` | deregister MR — *data path* | HIGH |
| `+88` | `isend` | `isend_v5` `0x7a30` | post send — *data path* | HIGH |
| `+96` | `irecv` | `irecv_v5` `0x80d0` | post recv — *data path* | HIGH |
| `+104` | `iflush` | `iflush_v5` `0x84b0` | GDR flush — *data path* | HIGH |
| `+112` | `test` | `test_v2` `0x81d0` | progress / completion test — *data path* | HIGH |
| `+120` | `closeSend` | `closeSend_v2` `0x88f0` | teardown send comm | HIGH |
| `+128` | `closeRecv` | `closeRecv_v2` `0x8a50` | teardown recv comm | HIGH |
| `+136` | `closeListen` | `closeListen_v2` `0x8bb0` | teardown listen comm | HIGH |
| `+144` | `getMrKey` | `get_mr_key_v5` `0x8d10` | rkey accessor — *data path* | HIGH |
| `+152` | `iwrite` | `iwrite_v5` `0x8f80` | one-sided RMA write — *data path* | HIGH |
| `+160` | `iwriteInline` | `iwrite_inline_v5` `0x9170` | inline RMA write — *data path* | HIGH |
| `+168` | `iread` | `iread_v5` `0x9370` | one-sided RMA read — *data path* | HIGH |

### v5 and v4 — the ABI deltas

The two older structs are the same canonical layout minus a tail. They are exported but, as the QUIRK notes, not bound on this runtime.

| Struct | Addr | Slots | Delta from v6 | Confidence |
|---|---|---|---|---|
| `ncclNetPlugin_v5` | `0x437e0` | 24 | **no `fini` slot** — `init_v2` `0x5770` sits at `+8`, every later field shifts down one slot (devices `+16`, getProperties `+24`, …, iread `+160`) | HIGH |
| `ncclNetPlugin_v4` | `0x43760` | 16 | no `fini`, **no RMA/dmabuf tail** (`regMrDmaBuf`/`getMrKey`/`iwrite`/`iwriteInline`/`iread` absent); `init_v4` `0x2e560` `+8`, `getProperties_v4` `0x2e4d0` `+24`, `listen_v2` `0x61d0`, `connect_v2` `0x66a0`, `accept_v2` `0x67d0` | HIGH |

> **CORRECTION (NET-1) — v4's `regMr` is `0x7000` and its `getProperties` is `0x2e4d0`.** An earlier seed reading placed v4 `regMr` at `0x6cf0` and reused the v5 `getProperties`. The `R_X86_64_RELATIVE` table resolves the v4 slot at `0x43798` to `0x7000` (the *same* `regMr` body v5/v6 use), the slot at `0x437a0` to `0x7670` (`deregMr`, also shared), and the `getProperties` slot at `0x43778` to the distinct `getProperties_v4 0x2e4d0`. The v4 ABI translates the internal `nccl_ofi_properties` into the narrower `ncclNetProperties_v4_t` — that translation is the only behavioral difference from v5/v6 in the control path.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `initNetPlugin` (`libnccom.so`) | `0x417a0` | host loader: uname gate, `nrt_get_libnccl_net`, `dlsym(v6)`, `init@+8`/`devices@+24` | HIGH |
| `init_no_atexit_fini_v6` | `0x59a0` | v6 `init`: store logger, `abort_on_error`, `create_plugin`, errno→`ncclResult` map | HIGH |
| `nccl_net_ofi_init_v2` | `0x5770` | v4/v5 `init` (same, with atexit-fini registration) | HIGH |
| `fini_v6` | `0x5b00` | v6 `fini` (v6-only slot) | HIGH |
| `devices_v2` | `0x5c40` | `*ndev = plugin->get_num_devices()` (`call *0x18(%rax)`) | HIGH |

---

## 2. The Base-Class Hierarchy and the Dispatch Chain

### Purpose

Behind the flat exported vtable sits a small C++-style class hierarchy, allocated and pointer-installed at construction: `nccl_net_ofi_plugin_t` → `device_t` → `domain_t` → `ep_t`. The ABI shims are thin: they walk this hierarchy to reach a *protocol* virtual (`ep->listen`/`ep->connect`/`listen_comm->accept`/`device->get_properties`) that RDMA or SENDRECV installed. This unit pins the base-class vtable offsets — each confirmed both by the **installer** (`plugin_init`/`device_init`/`endpoint_init`, which store the pointers) and by the **consumer** (the shims' `call *N(reg)`) — and the exact dispatch chain a `listen`/`connect`/`accept` takes.

### Entry Point

```text
                        singleton `plugin`  (set by create_plugin)
listen_v5 0x5f00 ──┐
connect_v10 0x62e0 ─┤  null-check plugin  (else "Error accessing plugin. Plugin has not been initialized yet.")
accept_v5 0x6ab0 ──┘
   │
   ├─ listen/connect:  dev = plugin->get_device(dev)      call *0x10(%rax)   ── plugin+16
   │                   dev->get_ep(&ep)                    call *0x40(%rax)   ── device+64
   │                      └─ device_get_ep 0xb620:
   │                            lock dev mutex (device+88)
   │                            domain = device_get_domain_impl(dev,tid)
   │                            ep = domain->get_ep(tid)    call *0x8(%rax)    ── domain+8 → domain_get_ep 0x9740
   │                   rc = ep->listen(handle,&lcomm)       call *0x8(%rax)    ── ep+8   (rdma listen 0x19dd0)
   │                   rc = ep->connect(handle,&scomm,tc)   call *0x10(%rax)   ── ep+16  (rdma connect 0x1ac00)
   │                   on error: ep->release(ep,0,0)        call *0x18(%rax)   ── ep+24
   │
   └─ accept:          rc = lcomm->accept(lcomm,&rcomm)     call *0x18(%rdi)   ── listen_comm+24
                       on error: ep = lcomm->ep (lcomm+8) ; ep->release        call *0x18(%rax)   ── ep+24
```

> **QUIRK — `accept` is not on the endpoint, it is on the listen-comm.** `listen` and `connect` dispatch through the `ep` vtable (`ep+8`/`ep+16`), but `accept` dispatches through the **`listen_comm`** at `+24` (`call *0x18(%rdi)` at `0x6ad2`, where `%rdi` is the listen-comm passed as `a1`). The error-recovery `ep` is then read from `listen_comm+8`, not from a device walk. A reimplementer who hangs `accept` off the ep vtable, mirroring `listen`/`connect`, will dispatch through a slot the protocol never installed. The asymmetry is structural: `listen` produces the `listen_comm`, and `accept` is that comm's own method.

### Algorithm — the ABI shim spine

```c
// listen_v5 — 0x5f00   [HIGH: all four call-slots read from objdump -d]
// Representative of every control-plane shim: null-check, walk to ep, call the protocol virtual.
int listen_v5(int dev, void *handle, void **listen_comm):
    if (plugin == NULL):                                  // singleton null guard
        log("Error accessing plugin. Plugin has not been initialized yet.");   // 0x2f170
        return ncclInternalError;
    device = plugin->get_device(dev);                     // call *0x10(%rax)  plugin+16  (0x5f42)
    if (device == NULL):
        log("Error accessing device %i."); return ncclInternalError;
    if (handle == NULL):
        log("Provided handle is NULL"); return ncclInvalidArgument;

    ep = NULL;
    rc = device->get_ep(&ep);                             // call *0x40(%rax)  device+64  (0x5f5d)
    if (rc != 0 || ep == NULL):
        log("Error accessing endpoint. Endpoint has not been initialized.");
        return rc ? rc : ncclInternalError;

    rc = ep->listen(handle, listen_comm);                 // call *0x8(%rax)   ep+8       (0x5f76)
    if (rc != 0):
        ep->release(ep, /*lock*/0, /*force*/0);           // call *0x18(%rax)  ep+24      (0x5f8a)
    return ofi_to_nccl_result(rc);

// connect_v10 — 0x62e0   [HIGH: resume-vs-fresh branch + tc passthrough read from disasm]
// The v10 form adds trafficClass; v5/v2 omit it. Resume reuses the handle-cached ep.
int connect_v10(int dev, void *handle, void **send_comm, int trafficClass):
    if (plugin == NULL || handle == NULL): return guard_error;
    if (handle->state /*+80*/ != 0):                      // connect already in progress
        ep = *(*(handle + 8) + 8);                        // cached ep  (handle+64 → ep)
        rc = ep->connect(handle, send_comm, trafficClass);// call *0x10(%rdi)  ep+16      (0x634b)
    else:                                                 // first call
        device = plugin->get_device(dev);                 // call *0x10(%rax)  plugin+16  (0x6388)
        rc = device->get_ep(&ep);                         // call *0x40(%rax)  device+64  (0x639a)
        rc = ep->connect(handle, send_comm, trafficClass);//                   ep+16
    if (rc != 0):
        ep->release(ep, 0, 0);                            // call *0x18(%rax)  ep+24      (0x640b)
    return ofi_to_nccl_result(rc);
```

The `device->get_ep` body (`device_get_ep 0xb620`) is itself a two-hop dispatch: it takes the device mutex (`device+88`), resolves the per-thread `domain` (`device_get_domain_impl`), then calls `domain->get_ep(tid)` at `domain+8` (`call *0x8(%rax)` at `0xb656`). A `pthread_mutex_lock`/`unlock` failure here is fatal — the shim aborts with `"pthread_mutex_lock failed: %s"` (`nccl_ofi_net.cpp`).

### Base-class vtable tables

`nccl_net_ofi_plugin_t` (base; `calloc 0x48` = 72 B; vtable installed by `plugin_init` `0xa610`). The installer packs the four function pointers into `+8/+16/+24` via `movq …,%xmm` and stores `+32` directly (`mov %rax,0x30(%rbx)` at `0xa62d` = `plugin_fini` at `+32`).

| Offset | Slot | Installed target | Role | Confidence |
|---|---|---|---|---|
| `+0` | `complete_init` | `rdma_plugin_complete_init 0x11d30` / `sendrecv_plugin_complete_init 0xc840` | post-protocol-init device materialization (called in `create_plugin`) | HIGH |
| `+8` | `assign_device` | `plugin_assign_device 0x9580` | install a `device_t*` into `devices[idx]` | HIGH |
| `+16` | `get_device` | `plugin_get_device 0x96e0` | bounds-checked `devices[idx]` — **used by every shim** | HIGH |
| `+24` | `get_num_devices` | `plugin_get_num_devices 0x95a0` | returns `plugin+56` (device count) | HIGH |
| `+32` | `fini` | `plugin_fini 0x95b0` | teardown; also frees the *loser* protocol in auto-probe | HIGH |
| `+40` | `domain_per_thread` (byte) | — | set in `create_plugin` from `OFI_NCCL_DOMAIN_PER_THREAD` / platform default | HIGH |
| `+48` | `devices[]` | `calloc(num_dev, 8)` | `device_t*` array | HIGH |
| `+56` | `num_devices` | — | device count (read by `devices_v2`) | HIGH |

`nccl_net_ofi_device_t` (base; vtable installed by `device_init` `0xa6b0`). The two control-plane targets a reimplementer must hit are `get_properties` (`+40`) and `get_ep` (`+64`).

| Offset | Slot | Installed target | Role | Confidence |
|---|---|---|---|---|
| `+0` | `plugin*` | parent | back-pointer | HIGH |
| `+8` | `dev id` (int) | — | device index | HIGH |
| `+16` | `guid` | `platform_device_set_guid` / `device_set_guid 0x111f0` | NIC GUID (→ props `+16`) | HIGH |
| `+24` | `name` | `strdup(fabric_attr->prov_name)` | provider name | HIGH |
| `+40` | `get_properties` | `get_properties 0x117a0` | the `getProperties` chain target (→ §3) | HIGH |
| `+48` | `get_domain` | `device_get_domain 0xb6f0` | per-thread domain (mutex-guarded) | HIGH |
| `+64` | `get_ep` | `device_get_ep 0xb620` | the `listen`/`connect` chain target | HIGH |
| `+80` | `fini` | `device_fini` | device teardown | HIGH |
| `+88` | device mutex | `pthread_mutex_t` | guards `get_domain`/`get_ep` | HIGH |
| `+136` | `release_all` | `device_release_all_domain_and_ep 0x98b0` | release all domains/eps | HIGH |
| `+160` | `fi_info*` | set by `rdma_device_init` | the NIC `fi_info` (read by `get_properties` as `**(device+160)`) | HIGH |

`nccl_net_ofi_ep_t` (base; vtable installed by `endpoint_init` `0xaa40`). The installer stores `domain*` at `+0` (`mov %rdi,(%rsi)`), `endpoint_release` at `+24` (`mov %rax,0x18(%rsi)`), and zeroes the refcount at `+40` (`movl $0x0,0x28(%rsi)`); the protocol then overwrites `+8`/`+16` with its `listen`/`connect`.

| Offset | Slot | Installed target | Role | Confidence |
|---|---|---|---|---|
| `+0` | `domain*` | parent (set by base) | back-pointer | HIGH |
| `+8` | `listen` | `rdma listen 0x19dd0` / `sendrecv_endpoint_listen 0xc240` | passive open (protocol-installed) | HIGH |
| `+16` | `connect` | `rdma connect 0x1ac00` / `sendrecv_endpoint_connect 0xf6e0` | active open (protocol-installed) | HIGH |
| `+24` | `release` | `endpoint_release 0x9ab0` (base) | refcount drop / teardown | HIGH |
| `+40` | `refcount` (int) | `0` (set by base) | endpoint refcount | HIGH |

`nccl_net_ofi_domain_t` (base) carries `get_ep` at `+8` (`domain_get_ep 0x9740`, confirmed by the `call *0x8(%rax)` in `device_get_ep`). `listen_comm` carries `ep*` at `+8` and `accept` at `+24` (confirmed by `accept_v5`). The RDMA-subclass field layouts of `domain`/`listen_comm` (mr_cache, idpool, rail arrays) belong to [transport-efa](transport-efa.md).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nccl_net_ofi_plugin_init` | `0xa610` | install plugin base vtable + `devices[]` array | HIGH |
| `nccl_net_ofi_device_init` | `0xa6b0` | install device base vtable | HIGH |
| `nccl_net_ofi_endpoint_init` | `0xaa40` | install ep base vtable (`domain*`, `release`, refcount) | HIGH |
| `plugin_get_device` | `0x96e0` | bounds-checked `devices[idx]` (plugin+16) | HIGH |
| `plugin_get_num_devices` | `0x95a0` | returns `plugin+56` (plugin+24) | HIGH |
| `device_get_ep` | `0xb620` | mutex + domain resolve + `domain->get_ep` (device+64) | HIGH |
| `domain_get_ep` | `0x9740` | per-domain ep accessor (domain+8) | HIGH |
| `endpoint_release` | `0x9ab0` | base ep release (ep+24) | HIGH |
| `listen_v5` / `connect_v10` / `accept_v5` | `0x5f00` / `0x62e0` / `0x6ab0` | the control-plane ABI shims | HIGH |

---

## 3. Init, Discovery, and Provider Negotiation

### Purpose

`create_plugin` (`0xabe0`) is the top of init, reached from the v6 `init` slot. It picks a protocol, runs its init (which is where libfabric discovery happens), wires the base struct's `domain_per_thread`, and runs a `get_device(0)`/`get_ep`/`get_properties` **self-test** before publishing the singleton. This unit pins the protocol-select decision, the RDMA discovery flow (`fi_dupinfo` hints → `fi_getinfo`("efa") → version gate), and the `getProperties` chain. The rail-build and per-op discovery internals (`init_connection`, `rdma_domain_create_endpoint`) belong to [transport-efa](transport-efa.md); this unit owns the *selection and gating*.

### Entry Point

```text
v6.init (init_no_atexit_fini_v6 0x59a0)
  guard !plugin ; store ofi_log_function ; abort_on_error = ofi_nccl_abort_on_error()
  rc = nccl_net_ofi_create_plugin(&plugin)
  └─ create_plugin 0xabe0
       fi_version() → "Using Libfabric version %u.%u"
       sysconf(_SC_PAGESIZE) → system_page_size, mr_cache_alignment
       nic_dup_conns / net_latency / cq_read_count  ← env
       platform_init(&provider_filter) 0x2cc00       ── sets provider hint string "efa"
       protocol = OFI_NCCL_PROTOCOL ? : platform-default(platform_data+40) ? : AUTO
         strcasecmp "RDMA"     → rdma_init(s)      @0xad47 → 0xae17
         strcasecmp "SENDRECV" → sendrecv_init(s)  @0xad5e → 0xafa2
         AUTO: try rdma_init THEN sendrecv_init     @0xb036 / @0xb05b
               keep whichever set plugin; free the loser via plugin->fini (plugin+32)
       plugin->domain_per_thread (plugin+40) ← OFI_NCCL_DOMAIN_PER_THREAD / platform default
       plugin->complete_init()           call *0x0(plugin)   ── materialize devices
       ── SELF-TEST ──
       dev0 = plugin->get_device(0)      call *0x10(plugin)
       dev0->get_ep(&ep)                 call *0x40(dev0)
       dev0->get_properties(&props)      call *0x28(dev0)     ── device+40
       log "Support for global registrations: %s" / "Support for DMA-BUF registrations: %s"
       ep->release(ep,0,0)               call *0x18(ep)
       if !support_gdr: configure_nccl_proto_simple("GDR")  → setenv NCCL_PROTO=simple
       *out = plugin
```

### Algorithm — protocol select and the EFA version gate

```c
// nccl_net_ofi_create_plugin — 0xabe0   [HIGH: two strcasecmp + dual init calls read from disasm]
int create_plugin(nccl_net_ofi_plugin **out):
    log("NET/OFI Initializing aws-ofi-nccl GitHub-dev");
    log("NET/OFI Using Libfabric version %u.%u", fi_version()>>16, fi_version()&0xffff);
    system_page_size = sysconf(_SC_PAGESIZE);
    provider = NULL;
    platform_init(&provider);                            // 0x2cc00 → provider = "efa"

    plugin = NULL;
    const char *proto = ofi_nccl_protocol();             // OFI_NCCL_PROTOCOL
    if (proto && strcasecmp(proto, "RDMA") == 0):        // @0xad47
        rdma_init(provider, &plugin, &topo_written);     // 0x171d0  @0xae17
    elif (proto && strcasecmp(proto, "SENDRECV") == 0):  // @0xad5e
        sendrecv_init(provider, &plugin);                // 0x10960  @0xafa2
    else:                                                // AUTO-PROBE
        rdma_init(provider, &plugin, &topo_written);     // @0xb036
        if (plugin == NULL):
            sendrecv_init(provider, &plugin);            // @0xb05b  fall back to tagged
        // (if both populated a plugin, the loser is freed via plugin->fini, plugin+32)

    plugin->domain_per_thread = ofi_nccl_domain_per_thread()    // plugin+40
                              ?: platform_default_domain_per_thread();

    plugin->complete_init();                             // call *0x0(plugin) — materialize devices
    // --- self-test: prove device 0 can produce an ep and properties ---
    dev0 = plugin->get_device(0);                        // plugin+16
    dev0->get_ep(&ep);                                   // device+64
    dev0->get_properties(&props);                        // device+40
    log("Support for global registrations: %s", props.regIsGlobal ? "yes":"no");
    log("Support for DMA-BUF registrations: %s", props.dmabufSupport ? "yes":"no");
    ep->release(ep, 0, 0);                               // ep+24

    if (!support_gdr):                                   // GDR unavailable on this build
        configure_nccl_proto_simple("GDR");             // 0xab20 → setenv NCCL_PROTO=simple
    *out = plugin;
    return 0;

// RDMA discovery + EFA gate (rdma_init 0x171d0 → query_provider_capabilities 0xa4f0)  [HIGH: version cmp]
int rdma_init(const char *provider, nccl_net_ofi_plugin **out, bool *topo_written):
    hints = fi_dupinfo(NULL);                            // FABRIC_1.8
    hints->caps           = 0x0018800000000006;          // FI_MSG|FI_RMA + EFA bits  (xmmword 0x37A20)
    hints->mode           = 0x0810000000000000;
    hints->ep_attr->type  = FI_EP_RDM;                   // = 3
    hints->domain_attr->mr_mode    = 0x474;              // FI_MR_LOCAL|VIRT_ADDR|ALLOCATED|…
    hints->domain_attr->mr_key_size = ofi_nccl_mr_key_size();
    support_gdr = 0;                                     // tri-state, 0 on this build
    nccl_ofi_dmabuf_viable();                            // 0x276b0
    ofiutils_get_providers(provider /*"efa"*/, version, hints, &info, &num);  // 0x26b80 → fi_getinfo
        // rc == -61 → "No eligible providers"; else fi_strerror
    fi_freeinfo(hints);

    // query_provider_capabilities 0xa4f0:
    log("NET/OFI Selected provider is %s, fabric is %s (found %d nics)", prov, fab, num);
    if (strcmp(prov_name, "efa") == 0):
        if (fi_version() <= 0x10015):                    // cmp $0x10015,%eax @0xa5b6 (require > 1.21)
            log("EFA provider requires at least libfabric version 1.22.0.");
            return -95;                                  // -EOPNOTSUPP
    endpoint_mr        = (mr_mode & 0x200) != 0;         // FI_MR_ENDPOINT
    virt_addr_mr       = (mr_mode & 0x10)  != 0;         // FI_MR_VIRT_ADDR
    data_progress_auto = (threading == 1);
    if (endpoint_mr): fatal("RDMA protocol does not support endpoint memory registration.");
    // ... topo_create + topo_group (multi-rail) → plugin_init(ndev) ...   (→ transport-efa)
```

> **NOTE — the version gate is `> 0x10015`, a packed libfabric version word.** `fi_version()` returns `(major<<16)|minor`; `0x10015` is `1.21`, so the `cmp $0x10015,%eax` (`0xa5b6`) followed by a `ja` requires **strictly greater** than `1.21`, i.e. ≥ **1.22.0**, matching the string `"EFA provider requires at least libfabric version 1.22.0."` (`0x30288`). A reimplementer must compare against the *packed* word — and with strict-greater, not greater-or-equal, against `0x10015` — not parse the dotted string.

### The getProperties chain

`getProperties` (slot `+32`, `getProperties_v5 0x2e420`) → `nccl_net_ofi_get_properties 0x5d40` → `device->get_properties` (`device+40`, `get_properties 0x117a0`) → `info_properties 0xa070`. The device-level call reads `dev->fi_info` (`**(device+160)`) and the plugin's device count (rail count), fills the internal `nccl_ofi_properties` via `info_properties`, then scales `speed *= rail_count`, sets `port`/`maxComms`/`maxWriteInline`, and clamps `maxP2pBytes`/`maxCollBytes` to `INT_MAX`. `getProperties_v5` finally repacks the internal struct into `ncclNetProperties_v6_t` (the v4 shim `getProperties_v4 0x2e4d0` repacks into the narrower `ncclNetProperties_v4_t`). The exact `ncclNetProperties_v6_t` field offsets (the SSE-packed `ptrSupport`/`regIsGlobal`/`speed`/`port` block) are MED — field order matches `ncclNet_v6` but some sub-fields are inferred from the SSE packing.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nccl_net_ofi_create_plugin` | `0xabe0` | init top: protocol select + auto-probe + self-test | HIGH |
| `nccl_net_ofi_rdma_init` | `0x171d0` | RDMA init: hints, `fi_getinfo`, version gate, multi-rail | HIGH |
| `nccl_net_ofi_sendrecv_init` | `0x10960` | SENDRECV init (tagged `fi_tsend`/`fi_trecv`); tag-bit check | HIGH |
| `platform_init` | `0x2cc00` | `provider_filter="efa"`, NCCL_TOPO_FILE, nic_dup_conns, latency | HIGH |
| `query_provider_capabilities` | `0xa4f0` | `"efa"` name + `fi_version()>0x10015` + mr_mode decode | HIGH |
| `ofiutils_get_providers` | `0x26b80` | thin wrapper → `fi_getinfo@FABRIC_1.8` | HIGH |
| `nccl_net_ofi_get_properties` | `0x5d40` | shim → `device->get_properties` (device+40) | HIGH |
| `info_properties` | `0xa070` | the real props filler (name/pciPath/guid/speed/dmabuf) | HIGH |
| `configure_nccl_proto_simple` | `0xab20` | `setenv NCCL_PROTO=simple` when GDR unsupported | HIGH |

---

## 4. The GDR / DMA-BUF Registration Policy

### Purpose

The control plane decides, *before any MR is registered*, whether the plugin advertises GPU-Direct RDMA and DMA-BUF support to the host, and whether it owns its own MR keys. On this Trainium build `support_gdr` is 0 and there is no CUDA path — device memory reaches the NIC through libfabric's `FI_HMEM` iface field plus a DMA-BUF fd, not a CUDA peer-memory call. This unit pins the three gates a reimplementer must reproduce: the dmabuf-viability gate, the EFA-PCI-ID gate, and the MR-key-ownership decision; the actual `fi_mr_regattr` registration is on [transport-efa](transport-efa.md).

### Algorithm — the three gates

```c
// nccl_ofi_dmabuf_viable — 0x276b0   [HIGH: both calls read from disasm]
// Gate 1: env override AND kernel-version capability.
bool dmabuf_viable():
    if (ofi_nccl_disable_dmabuf())                      // OFI_NCCL_DISABLE_DMABUF  @0x276b8
        return false;
    return kernel_version_rdma_dmabuf_ioctl_ok();        // 0x275e0  (uname + sscanf "maj.min")  @0x276c7

// info_properties — 0xa070 (dmabuf advertisement)   [MED: EFA-PCI compare is rodata-string driven]
// Gate 2: even if viable, only advertise dmabuf when the NIC is a recognized EFA device.
void info_properties(... nccl_ofi_properties *props):
    props->regIsGlobal = (support_gdr == 1);                       // props+24
    props->dmabufSupport = (efa_pci_id in {0xefa0,0xefa1,0xefa2})  // props+25
                         && dmabuf_viable()
                         && (ext_mem_caps > 0x10013);
    props->speed = link_attr.speed / 1e6;                          // p5en.48xlarge override → 200000
    props->gdrFlushDisable = support_gdr ^ 1;                      // props+48

// nccl_ofi_mr_keys_need_own_key — 0x27310   [HIGH: caps/mr_mode bit tests read from disasm]
// Gate 3: does the plugin allocate its own MR keys from an idpool?
bool mr_keys_need_own_key(fi_info *info, bool *out):
    bool fi_rma     = (info->caps    & 0x4)  != 0;       // testb $0x4,0x8(%rdi)  @0x2731b  (FI_RMA)
    bool prov_key   = (info->mr_mode & 0x40) != 0;       // and $0x40,%r12d       @0x27329  (FI_MR_PROV_KEY)
    *out = fi_rma && !prov_key;                           // own keys iff RMA set & provider does NOT assign
    if (ofi_nccl_mr_key_size() < provider_key_size)       // 0x28ed0
        log("Provider %s supports MR key size of %zu, but %zu was requested");
    return *out;
```

### Per-endpoint validation

When each `fid_ep` is created (inside `ofiutils_init_connection`), `platform_config_endpoint` (`0x2cf80`) runs two checks via the endpoint's `fi_getopt` op (ep ops `+16`, `call *0x10(%rax)` at `0x2d03b`):

```c
// platform_config_endpoint — 0x2cf80   [HIGH: getopt slot + string anchors]
int platform_config_endpoint(fi_info *info, fid_ep *ep):
    if (strcmp(prov_name, "efa") != 0): return 0;
    // GDR-required check: a GDR-capable instance with GDR off is an error (unless overridden)
    if (platform_supports_gdr /*platform_data+32*/ && support_gdr != 1
        && !ofi_nccl_disable_gdr_required_check()):
        log("GDR disabled on GDR-supported instance type %s");   // 0x3aef0
        return -22;
    // native-RDMA-write check: EFA must do real RDMA write, not emulate it
    if (protocol == "RDMA" && !ofi_nccl_disable_native_rdma_check()):
        flag = 0;
        fi_getopt(ep, FI_OPT_ENDPOINT, FI_OPT_EFA_EMULATED_WRITE /*0xF1080002*/, &flag, &len);
        if (flag != 0):
            log("FI_OPT_EFA_EMULATED_WRITE is true when the communication protocol is RDMA write.");  // 0x3b000
            return -22;
    return 0;
```

> **GOTCHA — `support_gdr` is tri-state, and the value `2` branch is dead on this build.** `support_gdr` is a global initialized to `0` in `rdma_init`; the control plane tests it against both `1` (GDR enabled) *and* `2` (a NIC_DUP_CONNS-coupled GDR mode). On this Trainium image it never leaves `0`, so the `== 2` branch and the `"NIC_DUP_CONNS … not supported"` warning are unreachable. A reimplementer should treat `support_gdr` as a tri-state knob (`0` off / `1` on / `2` dup-conns-coupled) but recognize that only the `0` path is exercised here, and that DMA-BUF — not GDR — is how device memory reaches the EFA NIC. The `ptrSupport` the plugin advertises is `NCCL_PTR_HOST` (or `HOST|DMABUF` when the EFA-PCI + viability gates pass), never a CUDA pointer-support flag.

> **GOTCHA — the MR key can legitimately be provider-assigned, and `mr_keys_need_own_key` is the *only* arbiter.** When `FI_MR_PROV_KEY` is set in `mr_mode`, the EFA provider assigns keys and the plugin must **not** allocate from its idpool; `mr_keys_need_own_key` (`0x27310`) returns false and the data path reads the key back off the `fid_mr` instead. A reimplementer who unconditionally allocates application keys (or who asserts the idpool key is valid) breaks every provider configuration that owns its own keys — the policy is decided here, in the control plane, from `caps & FI_RMA` AND `!(mr_mode & FI_MR_PROV_KEY)`, not in the registration path ([transport-efa](transport-efa.md) §5 documents the `-1` key the idpool then hands the data path).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nccl_ofi_dmabuf_viable` | `0x276b0` | `!OFI_NCCL_DISABLE_DMABUF && kernel_ok` | HIGH |
| `kernel_version_rdma_dmabuf_ioctl_ok` | `0x275e0` | uname + version parse for dmabuf ioctl support | HIGH |
| `nccl_ofi_mr_keys_need_own_key` | `0x27310` | `FI_RMA & !FI_MR_PROV_KEY` → own keys from idpool | HIGH |
| `platform_config_endpoint` | `0x2cf80` | per-ep GDR-required + native-RDMA-write checks | HIGH |
| `info_properties` | `0xa070` | dmabuf/gdr advertisement (props `+24`/`+25`/`+48`) | HIGH |
| `get_inject_rma_size_opt` | `0xaa70` | `fi_getopt FI_OPT_INJECT_RMA_SIZE` → `max_write_inline` (ep ops+16) | HIGH |
| `configure_nccl_proto_simple` | `0xab20` | force `NCCL_PROTO=simple` when GDR/LL unsupported | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `ncclNetPlugin_v6` (`.data 0x438a0`) | the struct this page documents; its data-path slots (`isend`/`irecv`/`iwrite`/`test`) are implemented by [transport-efa](transport-efa.md) |
| `initNetPlugin` (`libnccom.so 0x417a0`) | the host loader that dlopens this `.so` and binds v6; the caller side of the ABI |
| `nrt_get_libnccl_net@NRT_2.0.0` | the libnrt export that actually dlopens `libnccom-net.so` (the runtime side of the load) |
| `nccl_ofi_sendrecv.cpp` (`init 0x10960`) | the alternate tagged protocol; same base-class hierarchy, different `ep->listen`/`connect` installs |
| `libfabric.so.1` (EFA provider) | the NIC boundary — `fi_getinfo`/`fi_dupinfo`/`fi_version` (PLT) + per-`fid` inline op vectors |
| `platform_aws.cpp` (`platform_init 0x2cc00`) | the EC2 instance-type tuning table that sets provider filter, GDR support, default protocol |

## Cross-References

- [Inter-Node Transport: EFA / libfabric](transport-efa.md) — the data path this page's `listen`/`connect`/`accept` opens: `send_progress`, `ofi_process_cq`, RDMA-WRITE-with-immediate, the rail/req/cq internals; same build-id, same libfabric/hwloc deps
- [Intra-Node Transport (P2P)](transport-intra.md) — the *other* transport edge (local DMA between NeuronCores), selected by `p2pCanConnect` where this plugin's transport is selected by `netNeuronCanConnect`; no plugin, no libfabric
- [The libnrt ↔ libnccom ABI](abi.md) — the `NRT_2.0.0` boundary and `nrt_get_libnccl_net`, the reverse-import that loads this plugin; the two-axis compat gate above it
- [Overview: the NCCL Fork (2.31.24+nrt2.0)](overview.md) — the Part-XII map; where the net plugin sits in the `ncclTransports[2]` table and the `nrt_get_libnccl_net` handoff
- [back to index](../index.md)
