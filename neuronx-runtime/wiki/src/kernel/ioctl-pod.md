# Pod and Misc IOCTL Handlers

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The handler bodies are read from `neuron_cdev.c` (4043 lines), the arg structs from `neuron_ioctl.h` (876 lines), and the pod enums / feature-flag bits from `share/neuron_driver_shared.h`. The source is read directly, not reverse-engineered; every constant, offset, and dispatch line is a `#define`, a struct field, or a literal source line. Other driver versions renumber lines and add/remove commands. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

This page documents the "information / misc" subset of the `/dev/neuronN` `ioctl` surface — the family that asks the driver *what it is* and *where it sits*, and the one state-changing outlier (`POD_CTRL`) that drives pod formation. It splits into three groups: **pod / UltraServer membership** (`POD_INFO`, `POD_STATUS`, `POD_CTRL`), **device info / version / feature / BDF** (`DEVICE_INFO`, `DEVICE_BASIC_INFO`, `COMPATIBLE_VERSION`, `DRIVER_INFO_GET/SET`, `DEVICE_BDF`, `DEVICE_BDF_EXT`), and **topology / diagnostic** (`HOST_DEVICE_ID`, `HOST_DEVICE_ID_TO_RID_MAP`, `DUMP_MEM_CHUNKS`, `PRINTK`). The catalogue rows for all of these live on [ioctl-catalog](ioctl-catalog.md); this page is the handler-body deep-dive they cross-reference.

Every handler in the family is a thin wrapper around one template: `neuron_copy_from_user` the arg struct → apply a size / sanity gate → delegate to a subsystem, almost always through the `ndhal` indirection vtable (`ndhal->ndhal_npe` / `ndhal_pci` / `ndhal_cdev` / `ndhal_fw_io`) → `copy_to_user` the result back. The pod handlers do **not** run the election — they `memset` the arg after copy and then **thunk** to `ndhal->ndhal_npe.npe_pod_info` / `npe_pod_status` / `npe_pod_ctrl`, which is the real state machine owned by [pod-election](pod-election.md). The pivot a reimplementer must internalize is the **size demux**: three command numbers carry V1/V2 variants that share one `_IOC_NR` and direction and differ only in `_IOC_SIZE` — `POD_STATUS` (122), `POD_CTRL` (123), and `DRIVER_INFO` (110, which instead demuxes on `_IOC_DIR`). The handler resolves the variant *before* it copies, choosing the copy width from the exact `cmd`, then validates the user-supplied `sz` against that width.

Most of this family is reachable on the **free-access** lane: an `O_WRONLY` open of `/dev/neuronN` routes to `ncdev_misc_ioctl` (`neuron_cdev.c:3147`), which dispatches the pod and info commands with no `npid_is_attached` attach gate and no capability check. That makes the family the unprivileged discovery surface — and makes two of its members security-relevant: `PRINTK` carries a size-`0` out-of-bounds stack read (finding S1), and `POD_CTRL` is an ungated state-changer (finding S5/S8-adjacent), both on [ioctl-attack-surface](../security/ioctl-attack-surface.md). The handlers take a per-fd `struct neuron_device *`, but several ignore it entirely and act on globally-latched state (`narch_*` arch/revision, `npe_*` pod state).

For reimplementation, the contract is:

- **The common handler template** — `copy_from_user` → size/sanity gate → `ndhal` indirection → `copy_to_user`. Every handler is an instance; reproduce the four-step shape and the `ndhal` vtable dispatch.
- **The V1/V2 size demux** — for `POD_STATUS`/`POD_CTRL`, the copy width is chosen by exact `cmd ==` (V1 = `sizeof(arg.v1)`, V2 = `sizeof(arg)`) *before* the copy; the V1 path zero-extends the V2-only fields (`POD_CTRL` V1 forces `mode = UNSET`). `DRIVER_INFO` instead keys on `_IOC_DIR`.
- **The `npe_*` thunk** — the pod handlers are pure marshalling; the election lives behind `ndhal->ndhal_npe.npe_*`. Do not re-implement the state machine here ([pod-election](pod-election.md) owns it).
- **The post-copy `memset` discipline** — `POD_INFO`/`POD_STATUS` `memset(&arg, 0, ...)` after the copy and re-stamp only `.sz`, so every `[in]` field except `sz` is discarded by design; the result is `copy_to_user`d back at the same width.

| | |
|---|---|
| **Handler region** | `neuron_cdev.c:1784-1924` (info), `:2321-2418` (printk/topo/dump), `:2804-2918` (pod) |
| **Common template** | `neuron_copy_from_user` → size/sanity gate → `ndhal->ndhal_*` → `copy_to_user` |
| **Free-access lane** | `ncdev_misc_ioctl` (`:3147`), entered when `IS_NEURON_DEVICE_FREE_ACCESS` (`:52`, `O_WRONLY`) |
| **Pod thunk target** | `ndhal->ndhal_npe.npe_pod_info` / `npe_pod_status` / `npe_pod_ctrl` → [pod-election](pod-election.md) |
| **Size-demuxed cmds** | `POD_STATUS` (122) / `POD_CTRL` (123) by `_IOC_SIZE`; `DRIVER_INFO` (110) by `_IOC_DIR` |
| **`DRIVER_INFO` feature bitmap** | `feature_flags1 = 0x1FF` (all 9 bits, `:1912-1916`); `version = 0`, `size = 24` |
| **Security-relevant** | `PRINTK` size-`0` OOB stack read (S1, `:2349`); `POD_CTRL` ungated state-change (S5/S8) |
| **Confidence** | HIGH — every handler body, `_IOC` macro, struct, and dispatch line read verbatim from the shipped source |

---

## 1. The Common Handler Template

Every handler in this family is an instance of one four-step shape. The differences between handlers are entirely in *which* `ndhal` slot they call and *whether* they read a variant width first — the skeleton is invariant. Naming the skeleton once lets the per-handler sections name only their delta.

### Algorithm

```c
// the family skeleton -- each ncdev_* below is a specialization of this
function ncdev_<info_handler>(nd_or_cmd, param):
    // STEP 1 -- copy the request in (some handlers size-demux first; see §2)
    neuron_copy_from_user(__func__, &arg, param, copy_size)     // thin copy_from_user + pr_err on fault
    if ret: return ret

    // STEP 2 -- gate: a size/sanity/direction check before any work
    if <size or _IOC_SIZE or sz mismatch>: return -EINVAL       // e.g. arg.sz != sizeof(arg)

    // STEP 3 -- delegate through the ndhal indirection vtable (or a narch/pci/fw_io helper)
    memset(&arg, 0, sizeof(arg)); arg.sz = sizeof(arg)          // pod handlers only: discard [in], re-stamp sz
    ret = ndhal->ndhal_<sub>.<fn>(... &arg.out_fields ...)      // npe_* / pci / cdev / fw_io / narch

    // STEP 4 -- copy the result back out at the request width
    return copy_to_user(param, &arg, copy_size)
```

The four `ndhal` sub-vtables this family reaches, and what each fills:

| `ndhal` slot | Reached by | Fills |
|---|---|---|
| `ndhal_npe.npe_pod_info` / `npe_pod_status` / `npe_pod_ctrl` | `POD_INFO`/`STATUS`/`CTRL` | pod type / id / size / state / node-id / mode — [pod-election](pod-election.md) |
| `ndhal_cdev.ncdev_compatible_version` | `COMPATIBLE_VERSION` (`:2324`) | `{max, min}` RT version (per-arch impl) |
| `ndhal_pci.neuron_pci_device_id_to_rid_map` | `HOST_DEVICE_ID_TO_RID_MAP` (`:2386`) | `{count, host_did_to_rid_map[64]}` |
| `ndhal_fw_io.fw_io_topology` | `DEVICE_INFO` (`:1857`) | `connected_devices[]` + count |

The `DEVICE_BASIC_INFO` / `DRIVER_INFO` / `DEVICE_INFO` handlers do not go through `ndhal_*` for the arch/revision fields — they call the latched `narch_get_arch()` / `narch_get_revision()` directly (`narch_fill_device_basic_info`, `:1784-1788`); the `narch` accessors are owned by the arch cell ([arch/overview](../arch/overview.md)).

> **NOTE —** "delegate through `ndhal`" is the whole point of the family. None of these handlers contains the logic it reports; each is a marshalling stub in front of a subsystem vtable. A reimplementer who tries to compute the pod state, the connected-device list, or the RID map *inside* the handler has mis-modeled the boundary — the handler's only jobs are copy-in, gate, vtable-call, copy-out.

---

## 2. The V1/V2 Size Demux

Three commands carry multiple struct widths under one `_IOC_NR`. The dispatcher reaches the handler by `_IOC_NR(cmd)` (so both widths land in the same function); the handler then resolves the exact variant from the full `cmd` and chooses the copy width *before* it copies. This is the single counter-intuitive mechanic of the family.

### `POD_STATUS` / `POD_CTRL` — `_IOC_SIZE` demux

`ncdev_pod_status` is the cleanest illustration: the copy width is `sizeof(arg.v1)` for the V1 `cmd` and `sizeof(arg)` (the V2 superset) for the V2 `cmd`, and the user-supplied `arg.v1.sz` must equal that width.

```c
// neuron_cdev.c:2832 -- POD_STATUS V1/V2 size demux + npe_ thunk
function ncdev_pod_status(cmd, param):
    static_assert(POD_STATUS != POD_STATUS_V2)             // :2834  constants must differ (same nr, diff size)
    struct neuron_ioctl_pod_status_v2 arg                 // :2839  always the WIDER struct on the stack

    if cmd == POD_STATUS:    size = sizeof(arg.v1)         // :2841-2842  V1: copy only the v1 prefix
    elif cmd == POD_STATUS_V2: size = sizeof(arg)          // :2843-2844  V2: copy the full struct
    else: return -EINVAL                                  // :2846

    neuron_copy_from_user(__func__, &arg, param, size)    // :2849  copy at the chosen width
    if ret: return ret
    if arg.v1.sz != size: return -EINVAL                  // :2854  caller's sz must match the cmd's width

    memset(&arg, 0, size); arg.v1.sz = size               // :2858-2859  discard [in], re-stamp sz

    // -- THE THUNK: status pulled before info so info is valid (comment :2861) --
    ret  = ndhal->ndhal_npe.npe_pod_status(&arg.v1.state, &arg.v1.node_id)             // :2863
    ndhal->ndhal_npe.npe_pod_info(&arg.v1.pod_type, arg.v1.pod_id, &arg.v1.pod_sz,
                                  &arg.mode, &arg.modes_supported)                     // :2864

    cret = copy_to_user(param, &arg, size)                // :2866  copy back at the SAME width
    if cret != 0: return cret                             // :2867-2869  copy-out error overrides…
    return ret                                            // :2870  …else return the npe_pod_status rc
```

The V1 path declares the *wider* `pod_status_v2` struct on the stack but copies in and out only its `v1` prefix (`size = sizeof(arg.v1)`). The two V2-only fields (`mode`, `modes_supported`) are filled by `npe_pod_info` unconditionally — they simply never cross the user boundary on a V1 call because the copy width stops at the prefix. `POD_CTRL` uses the identical demux (`:2884-2891`) but additionally **forces the V1 default for the V2-only field** instead of leaving it zero:

```c
// neuron_cdev.c:2884 -- POD_CTRL V1 zero-extends mode to UNSET
if cmd == POD_CTRL:    size = sizeof(arg.v1); arg.mode = NEURON_ULTRASERVER_MODE_UNSET  // :2884-2886
elif cmd == POD_CTRL_V2: size = sizeof(arg)                                             // :2887-2888
```

`POD_CTRL` is also the only pod handler that needs the per-fd device: it resolves `nd = filep->private_data->ndev` (`:2897-2904`, `-EINVAL` if either pointer is NULL) and passes it into the thunk, because `npe_pod_ctrl` acts on a specific device's election:

```c
ret = ndhal->ndhal_npe.npe_pod_ctrl(nd, arg.v1.ctrl, arg.mode, arg.v1.timeout, &arg.v1.state)  // :2911
```

### `DRIVER_INFO` — `_IOC_DIR` demux

`DRIVER_INFO` overloads the *direction* rather than the size: `GET` is `_IOR` and `SET` is `_IOW`, same `nr 110`. `ncdev_driver_info` reads `_IOC_DIR(cmd)` and rejects the write direction outright (set is unsupported), then for read fills the feature struct if the encoded `_IOC_SIZE` is at least the current struct's:

```c
// neuron_cdev.c:1896 -- DRIVER_INFO: direction demux, GET-only
function ncdev_driver_info(cmd, param):
    dir = _IOC_DIR(cmd); size = _IOC_SIZE(cmd)            // :1899-1900
    if dir == _IOC_WRITE: return -ENOTSUPP                // :1902-1903  SET is unimplemented
    if dir == _IOC_READ && size >= _IOC_SIZE(DRIVER_INFO_GET):   // :1907  fwd/bkwd-compat width gate
        driver_info.architecture = narch_get_arch()       // :1908
        driver_info.revision     = narch_get_revision()   // :1909
        driver_info.version      = NEURON_DEVICE_DRIVER_INFO_VERSION0   // :1910  = 0
        driver_info.size         = sizeof(driver_info)     // :1911  = 24
        driver_info.feature_flags1 = DMABUF | ASYNC_DMA | BATCH_DMAQ_INIT | BIG_CORE_MAPS
                                   | MEM_ALLOC_TYPE | HBM_SCRUB | MEM_ALLOC64
                                   | CONTIGUOUS_SCRATCHPAD | ZEROCOPY    // :1912-1916  → 0x1FF
        return copy_to_user(param, &driver_info, sizeof(driver_info))   // :1918
    return -EINVAL                                        // :1923
```

The `feature_flags1` bitmap is the driver's capability advertisement: all nine bits of `enum neuron_driver_feature_flag` (`neuron_driver_shared.h:11-20`) are OR-set, so a 2.27.4.0 driver reports `0x1FF`. `libnrt` probes individual bits to decide whether to use the wider (`>=4 GB`) allocation/copy commands — the same feature-probe purpose served by the `*64` struct-pad trick on the mem family ([ioctl-catalog §mem](ioctl-catalog.md#family-mem)).

> **GOTCHA —** the V1/V2 split is resolved by `_IOC_SIZE`, not by any in-band version field. `static_assert(POD_STATUS != POD_STATUS_V2)` (`:2834`) and `static_assert(POD_CTRL != POD_CTRL_V2)` (`:2875`) enforce that the two `cmd` constants actually differ — they share `nr` and direction, so the *only* thing that makes them distinct is the struct's `sizeof` in the `_IOC_SIZE` field. A reimplementer who packs the V2 struct but issues the V1 `cmd` constant fails the `arg.v1.sz != size` gate (`:2854`); one who packs V1 and issues V2 over-reads past the user buffer. The width is a property of the `cmd`, validated against `sz` — get both right or the gate rejects.

---

## 3. Pod Membership Handlers

### `POD_INFO` (`:2804`)

The simplest pod handler: no variant demux (single struct), re-checks the exact `cmd` defensively, gates on `sz`, then thunks. It discards every `[in]` field after the copy.

```c
// neuron_cdev.c:2804 -- POD_INFO
function ncdev_pod_info(cmd, param):
    enum neuron_ultraserver_mode mode      // :2808  //unused
    u32 modes_supported                    // :2809  //unused
    if cmd != POD_INFO: return -EINVAL     // :2811  defensive re-check (dispatched by _IOC_NR :3173)
    neuron_copy_from_user(__func__, &arg, param, sizeof(arg))   // :2815
    if arg.sz != sizeof(arg): return -EINVAL                    // :2820  sanity gate
    memset(&arg, 0, sizeof(arg)); arg.sz = sizeof(arg)          // :2824-2825  discard [in], re-stamp
    ret = ndhal->ndhal_npe.npe_pod_info(&arg.pod_type, arg.pod_id,
                                        &arg.pod_sz, &mode, &modes_supported)   // :2827
    return copy_to_user(param, &arg, sizeof(arg))               // :2829
```

> **NOTE —** `POD_INFO` calls the same `npe_pod_info` that returns `mode` and `modes_supported`, but passes *local* `//unused` variables for them (`:2808-2809`, `:2827`) — they are discarded. Only `pod_type`, `pod_id`, and `pod_sz` cross back to userspace. The full mode/modes-supported pair is what `POD_STATUS_V2` exposes (`:2864`); `POD_INFO` is the lighter query that omits them. A reimplementer should not expect `POD_INFO` to report operating mode.

### `POD_STATUS` (`:2832`) and `POD_CTRL` (`:2873`)

Both are walked through in [§2](#2-the-v1v2-size-demux) above — `POD_STATUS` is the V1/V2 `_IOC_SIZE` demux exemplar, and `POD_CTRL` adds the per-fd `nd` resolution and the `npe_pod_ctrl` thunk that actually *triggers* election. The `ctrl` field is an `enum neuron_pod_ctrl_req` (`neuron_driver_shared.h:38`): `REQ_POD` (0, on-demand election), `REQ_SINGLE_NODE` (1), `REQ_KILL` (2), and `SET_MODE` (3). What each request does to the state machine — the `mark_cnt==0` gate, the lame-duck retry, the mode lock — is owned by [pod-election §state-machine](pod-election.md#the-internal-state-machine); this handler only marshals the request into the thunk.

> **GOTCHA —** `POD_CTRL` is **state-changing on the free-access lane**. It is dispatched on the `O_WRONLY` misc path (`:3177`) with no `npid_is_attached` attach gate and no capability check, yet `npe_pod_ctrl` can trigger a pod-election state transition (`ctrl ∈ {REQ_POD, REQ_SINGLE_NODE, REQ_KILL, SET_MODE}`). Any process that can `open("/dev/neuronN", O_WRONLY)` can request, kill, or re-mode the pod election; the *only* serialization against a model that is mid-execution is the `ncrwl_range_mark_cnt == 0` gate inside `npe_pod_ctrl` itself ([pod-election](pod-election.md#the-internal-state-machine)). A reimplementation that assumes the runtime library is the sole caller mis-models the reachability — this is finding S5/S8-adjacent on [ioctl-attack-surface](../security/ioctl-attack-surface.md). The V1 path forcing `mode = UNSET` (`:2886`) means a V1 `SET_MODE` request can only ever clear the mode.

---

## 4. Device Info, Version, and BDF Handlers

### `DEVICE_INFO` (`:1845`) — the topology query

The one info handler that takes a lock and reaches firmware. It fills arch/revision from `narch`, then under a global discovery mutex calls `fw_io_topology` to enumerate connected devices, sanity-caps the count at 8, and fills the two BAR windows:

```c
// neuron_cdev.c:1845 -- DEVICE_INFO
function ncdev_device_info(nd, param):
    narch_fill_device_basic_info(&result)                 // :1852  arch, revision
    mutex_lock(&ncdev_discovery_lock)                     // :1854  DEFINE_MUTEX :1844 -- one discoverer at a time
    ret = ndhal->ndhal_fw_io.fw_io_topology(nd->fw_io_ctx, nd->pdev->device,
              nd->device_index, connected_devices, &connected_device_count)   // :1857  → fw-io cell
    if ret: connected_device_count = 0; pr_err("Unable to get connected devices...")  // :1858-1861  soft-fail
    mutex_unlock(&ncdev_discovery_lock)                   // :1862
    if connected_device_count > NEURON_IOCTL_MAX_CONNECTED_DEVICES: return -EINVAL    // :1866  cap at 8
    copy connected_devices[] into result; result.bar_address[0]=bar0/size, [1]=bar2/size  // :1871-1879
    return copy_to_user(param, &result, sizeof(result))   // :1881
```

The BAR mapping is `bar_address[0]/bar_size[0] = bar0` and `bar_address[1]/bar_size[1] = bar2` (`:1876-1879`) — the two-entry array (`NEURON_MAX_BARS 2`, `neuron_ioctl.h:479`) carries BAR0 and BAR2, not BAR0/BAR1. A topology read failure is **soft**: the handler zeros the count and returns success rather than failing the whole query, so `ndl_open_device` does not fail on a transient topology miss (comment `:1855-1856`).

### `DEVICE_BDF` (`:1797`) and `DEVICE_BDF_EXT` (`:1806`)

`DEVICE_BDF` (deprecated) reads the PCI coordinates straight off `nd->pdev` — `{bus->number, PCI_SLOT(devfn), PCI_FUNC(devfn)}` (`:1800-1802`) — with no input. `DEVICE_BDF_EXT` is the container-aware successor: it resolves a device by a **container-relative** index, offsetting the caller's `arg.nd_index` by the index of the device the misc fd was opened on (always device 0 from the container's view), then looks the real device up via `neuron_pci_get_device`:

```c
// neuron_cdev.c:1806 -- DEVICE_BDF_EXT (container-relative)
function ncdev_device_bdf_ext(filep, param):
    nd = filep->private_data->ndev; if NULL: return -EINVAL    // :1817-1824
    offset = nd->device_index                                  // :1825  container base
    neuron_copy_from_user(__func__, &arg, param, sizeof(arg))  // :1827
    nd = neuron_pci_get_device(arg.nd_index + offset)          // :1831  → K-PCI cell
    if !nd: pr_err("Invalid nd index %d, offset %d", ...); return -EINVAL    // :1832-1835
    arg.domain = pci_domain_nr(nd->pdev->bus); arg.bus_number = nd->pdev->bus->number  // :1836-1837
    arg.slot = PCI_SLOT(nd->pdev->devfn); arg.func = PCI_FUNC(nd->pdev->devfn)         // :1838-1839
    return copy_to_user(param, &arg, sizeof(arg))              // :1840
```

> **QUIRK —** `DEVICE_BDF_EXT` shares `_IOC_NR 106` with the deprecated `DEVICE_RESET_STATUS` (`__u8`, `neuron_ioctl.h:800` vs `:803`) — a genuine `nr` collision. They are disambiguated by *both* `_IOC_SIZE` (different `cmd` constants) *and* lane: `RESET_STATUS` is matched by exact `cmd` on the main path (`:3239`), `BDF_EXT` by exact `cmd` on the misc/free-access lane (`:3156`). A reimplementer enumerating by `nr` alone will conflate them; the size and lane together resolve it ([ioctl-catalog](ioctl-catalog.md#family-misc--info--lifecycle)).

### `DEVICE_BASIC_INFO` (`:1790`) and `COMPATIBLE_VERSION` (`:2321`)

`DEVICE_BASIC_INFO` is the minimal handler — a pure `narch` fill plus `copy_to_user`, no input, no `nd` (`:1790-1795`). `COMPATIBLE_VERSION` thunks the highest/lowest supported runtime version through the per-arch cdev vtable: `ndhal->ndhal_cdev.ncdev_compatible_version(&arg)` fills `{max, min}` (`:2324`), then `copy_to_user` (`:2325`). The per-arch implementation behind that slot is owned by the DHAL-cdev cell.

---

## 5. Topology and Diagnostic Handlers

### `HOST_DEVICE_ID` (`:2362`) and `HOST_DEVICE_ID_TO_RID_MAP` (`:2376`)

`HOST_DEVICE_ID` returns the per-fd device's host index — `arg.host_device_id = nd->device_index` (`:2366`) — for container topology discovery; it reads no input. `HOST_DEVICE_ID_TO_RID_MAP` is a textbook template instance with an `_IOC_SIZE` gate: it rejects a `cmd` whose encoded size does not match the struct, `memset`s, thunks to the PCI vtable, and copies back:

```c
// neuron_cdev.c:2376 -- device-index → routing-id map
function ncdev_host_device_id_to_rid_map(cmd, param):
    if _IOC_SIZE(cmd) != sizeof(arg): return -EINVAL           // :2380  size gate (no copy_from_user — pure [out])
    memset(&arg, 0, sizeof(arg))                               // :2384
    ndhal->ndhal_pci.neuron_pci_device_id_to_rid_map(&arg.count, arg.host_did_to_rid_map)  // :2386  → K-PCI cell
    return copy_to_user(param, &arg, sizeof(arg))              // :2387
```

The map is `host_did_to_rid_map[64]` (`NEURON_IOCTL_MAX_DEVICES 64`, `neuron_ioctl.h:544`); the per-arch PCI implementation behind the vtable slot is the DHAL/PCI cell.

### `DUMP_MEM_CHUNKS` (`:2390`) — the mem-dump diagnostic

A privileged-path diagnostic that walks the device's mem-chunk bookkeeping into a caller-sized array. It allocates a kernel buffer sized by the caller's `num_entries_in`, fills it via the mempool walker, returns the actual count, and copies the data back only if the caller's buffer was large enough:

```c
// neuron_cdev.c:2390 -- DUMP_MEM_CHUNKS
function ncdev_dump_mem_chunks(nd, param):
    neuron_copy_from_user(__func__, &arg, param, sizeof(arg))  // :2397
    if arg.num_entries_in > 0:                                 // :2401
        data = kmalloc(arg.num_entries_in * sizeof(mem_chunk_info), GFP_KERNEL)  // :2402  ← see GOTCHA
        if !data: ret = -ENOMEM; goto done                    // :2403-2406
    ret = mc_dump_all_chunks(nd, arg.hbm_index, arg.num_entries_in, data, &num_entries_out)  // :2408  → K-MEM
    if ret: goto done
    arg.num_entries_out = num_entries_out; copy_to_user(param, &arg, sizeof(arg))  // :2412-2413
    if arg.num_entries_in >= num_entries_out:                 // :2417  caller had room → copy data
        copy_to_user(arg.data, data, num_entries_out * sizeof(mem_chunk_info))    // :2419
    // (else: the partial-result branch signals the caller to re-call with num_entries_out capacity)
done: kfree(data)                                             // all exits free the buffer
```

The `mc_dump_all_chunks` walker is owned by the mempool cell ([ioctl-mem](ioctl-mem.md)); the `mem_chunk_info` element is `{u64 pa, u64 size, u32 mem_type}` (`neuron_driver_shared.h:181`). The two-pass count-then-fill protocol (return the needed count, let the caller re-allocate and re-call) is the standard kernel idiom for variable-length output.

> **GOTCHA —** the `kmalloc(arg.num_entries_in * sizeof(struct neuron_ioctl_mem_chunk_info))` at `:2402` multiplies a user-controlled `__u32` by 24 with **no ceiling check**. On LP64 (x86-64 / arm64) this is benign: `u32 * size_t` promotes to a 64-bit `size_t`, so the maximum `0xFFFFFFFF × 24 ≈ 96 GB` does *not* wrap — `kmalloc` simply returns NULL and the handler returns `-ENOMEM` (`:2403-2406`). It is finding S12 (INFO, non-exploitable on LP64) on [ioctl-attack-surface](../security/ioctl-attack-surface.md#12-s10--s11--s12--firmware-tcb-and-non-exploitable-observations); a reimplementer targeting a 32-bit (ILP32) build, where the multiply *can* wrap, must add `if (arg.num_entries_in > CAP) return -EINVAL;` before the multiply.

### `PRINTK` (`:2333`) — the kernel-log passthrough

A userspace error-string passthrough to the kernel log / serial console. It copies a fixed-size header, copies the string into a 512-byte stack buffer guarded by an *upper* bound, requires NUL-termination, then `printk`s:

```c
// neuron_cdev.c:2333 -- PRINTK
function ncdev_printk(param):
    char str[512]                                         // :2336
    neuron_copy_from_user(__func__, &arg, param, sizeof(arg))   // :2338
    if arg.size > sizeof(str): return -EFAULT             // :2342  UPPER bound only — `>`, not `>=`, allows 512
    neuron_copy_from_user(__func__, str, arg.buffer, arg.size)  // :2345  size==0 ⇒ copies nothing, str UNINIT
    if str[arg.size - 1] != 0: return -EBADMSG            // :2349  size==0 ⇒ str[0xFFFFFFFF] OOB read
    printk(KERN_ERR "%s\n", str)                          // :2352
    return 0
```

> **GOTCHA —** `arg.size` (`__u32`) is bounded only from *above* (`:2342`), never from below. With `arg.size == 0`, the trailing-NUL check `str[arg.size - 1]` evaluates `str[0u - 1] = str[0xFFFFFFFF]` — an out-of-bounds read at a fixed `+4 GB` offset from a 512-byte kernel-stack buffer, on the ungated `O_WRONLY` misc lane that needs no attach, no `DEVICE_INIT`, and no capability. (The preceding `copy_from_user(str, buffer, 0)` returns success and copies nothing, so `str[]` is uninitialized stack.) On most layouts the address is unmapped → kernel OOPS (local DoS); if mapped, the `!= 0` branch is a 1-bit oracle (weak info-leak). This is finding **S1** on [ioctl-attack-surface §3](../security/ioctl-attack-surface.md#3-s1--printk-size-underflow-oob-stack-read). The fix is one line — `if (arg.size == 0 || arg.size > sizeof(str)) return -EFAULT;` before the index. A reimplementation that copies only the "upper-bound only" check inherits the bug. Note the `>` at `:2342` (not `>=`) deliberately admits `arg.size == 512`, so `str[511]` is the in-bounds last byte — that boundary is correct; only the missing lower bound is the bug.

---

## 6. Per-Handler Reference

### Arg structs

The `[in]`/`[out]` shape per handler. Field offsets are nominal LP64 (declaration order; not `pahole`-verified, hence the trailing-padding of `pod_status` is MED — the field *set* is HIGH). All pod structs are in `neuron_ioctl.h`; the pod enums in `neuron_driver_shared.h`.

| Handler | Arg struct (`neuron_ioctl.h`) | Key `[in]` fields | Key `[out]` fields |
|---|---|---|---|
| `ncdev_pod_info` | `pod_info` `:568` (260 B) | `sz` | `pod_type`, `pod_id[256]`, `pod_sz` |
| `ncdev_pod_status` | `pod_status` `:575` / `_v2` `:585` | `sz` | + `state`, `pod_type`, `pod_sz`, `node_id`; `_v2` adds `mode`, `modes_supported` |
| `ncdev_pod_ctrl` | `pod_ctrl` `:591` / `_v2` `:599` | `sz`, `ctrl`, `timeout`; `_v2` adds `mode` | `state` |
| `ncdev_device_info` | `device_info` `:480` (80 B) | — | arch, rev, `connected_device_count`, `connected_devices[8]`, `bar_address[2]`, `bar_size[2]` |
| `ncdev_device_basic_info` | `device_basic_info` `:452` (8 B) | — | `architecture`, `revision` |
| `ncdev_compatible_version` | `compatible_version` `:443` (8 B) | — | `max`, `min` |
| `ncdev_driver_info` | `device_driver_info` `:496` (24 B) | (dir/size only) | arch, rev, `version`(=0), `size`(=24), `feature_flags1`(=`0x1FF`) |
| `ncdev_device_bdf` | `device_bdf` `:456` (8 B) | — | `bus_number`, `slot`, `func` |
| `ncdev_device_bdf_ext` | `device_bdf_ext` `:462` (8 B ptr-form) | `nd_index` | `domain`, `bus_number`, `slot`, `func` |
| `ncdev_get_host_device_id` | `host_device_id` `:525` (4 B) | — | `host_device_id` |
| `ncdev_host_device_id_to_rid_map` | `host_device_id_to_rid_map` `:545` (260 B) | (size only) | `count`, `host_did_to_rid_map[64]` |
| `ncdev_dump_mem_chunks` | `dump_mem_chunks` `:529` (24 B) | `hbm_index`, `num_entries_in`, `data*` | `num_entries_out`, `*data[]` |
| `ncdev_printk` | `printk` `:519` (16 B) | `buffer*`, `size`, `action`(ignored) | — |

### Function Map

| Handler | file:line | Arg / variant demux | Gate | Variants | Confidence |
|---|---|---|---|---|---|
| `ncdev_pod_info` | `:2804` | single; `cmd != POD_INFO` re-check `:2811` | `arg.sz == sizeof` `:2820` | — | HIGH |
| `ncdev_pod_status` | `:2832` | `cmd ==` (V1/V2) `:2841-2844`; `_IOC_SIZE` | `arg.v1.sz == size` `:2854` | `POD_STATUS_V2` | HIGH |
| `ncdev_pod_ctrl` | `:2873` | `cmd ==` (V1/V2) `:2884-2888`; V1 `mode=UNSET` | `arg.v1.sz == size` `:2907`; `nd != NULL` | `POD_CTRL_V2` | HIGH |
| `ncdev_device_info` | `:1845` | single (`nr 3`) | `connected_device_count <= 8` `:1866` | — | HIGH |
| `ncdev_device_basic_info` | `:1790` | single (`nr 100`) | none (pure `narch` fill) | — | HIGH |
| `ncdev_compatible_version` | `:2321` | single (`nr 93`) | none (vtable fill) | — | HIGH |
| `ncdev_driver_info` | `:1896` | `_IOC_DIR` (GET/SET) `:1902-1904` | `size >= _IOC_SIZE(GET)` `:1907`; WRITE→`-ENOTSUPP` | `DRIVER_INFO_SET` | HIGH |
| `ncdev_device_bdf` | `:1797` | single (`nr 101`, deprecated) | none | — | HIGH |
| `ncdev_device_bdf_ext` | `:1806` | single (`nr 106`, NR-collision) | `nd != NULL`; `neuron_pci_get_device` NULL `:1832` | `DEVICE_RESET_STATUS` (NR-collision) | HIGH |
| `ncdev_get_host_device_id` | `:2362` | single (`nr 114`) | none (pure `[out]`) | — | HIGH |
| `ncdev_host_device_id_to_rid_map` | `:2376` | single (`nr 115`) | `_IOC_SIZE(cmd) == sizeof` `:2380` | — | HIGH |
| `ncdev_dump_mem_chunks` | `:2390` | single (`nr 116`) | `kmalloc` rc; `num_entries_in >= out` `:2417` | — | HIGH |
| `ncdev_printk` | `:2333` | single (`nr 113`) | `arg.size > 512` `:2342` (**no lower bound** → S1) | — | HIGH |

> **CORRECTION (POD-sz) —** the exact `sizeof` of `pod_status` (V1) is computed, not `pahole`-extracted: `__u16 sz` (off 0) + `__u8 pod_id[256]` (off 2..257) + alignment pad to off 260 + `__u32 state` (off 260) + `__u8 pod_type` (264) + `__u8 pod_sz` (265) + `__s8 node_id` (266), trailing-padded to a 4-byte multiple → 268 B. The field *set and order* are verbatim from `neuron_ioctl.h:575-582` (HIGH); the 268-byte tail depends on the compiler's trailing alignment of the struct after the embedded `u32`, so treat the exact byte size as MED until `pahole`-verified. The handler never relies on the absolute size — it uses `sizeof(arg.v1)` symbolically and validates `arg.v1.sz` against it — so the demux is correct regardless of the exact tail.

---

## Cross-References

- [IOCTL Catalog](ioctl-catalog.md) — the per-command `nr`/direction/arg-struct/`ndl_*`-caller/gate table for the pod and misc families; this page is the handler-body deep-dive the catalog's pod/misc rows cross-reference
- [Pod Election (UltraServer / NUTD)](pod-election.md) — the `npe_pod_info`/`npe_pod_status`/`npe_pod_ctrl` engine the pod handlers thunk into: the state machine, the node-id election, the MiscRAM result format, and the `mark_cnt==0` gate that `POD_CTRL` rides
- [Memory IOCTL Handlers](ioctl-mem.md) — the `mc_dump_all_chunks` mempool walker behind `DUMP_MEM_CHUNKS`, the `mem_chunk` bookkeeping object it dumps, and the mem family's own size-overload (`*64`) demux this page's V1/V2 demux parallels
- [IOCTL Dispatch and the Privilege-Gate Model](ioctl-dispatch.md) — the `_IOC_NR`/`_IOC_SIZE`/`_IOC_DIR` overload resolution this page's demux handlers depend on, the `O_WRONLY` free-access split (`:3206`) that routes the pod/info commands to `ncdev_misc_ioctl`, and the `npid_is_attached` attach gate these handlers skip
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the security projection: `PRINTK` size-`0` OOB stack read (S1), `POD_CTRL` as an ungated free-access state-changer (S5/S8), and the benign `DUMP_MEM_CHUNKS` `kmalloc` multiply (S12)
- [Silicon & Architecture Model](../arch/overview.md) — the `narch_get_arch`/`narch_get_revision` latched arch state the info handlers report, and the STD/ULTRASERVER/PDS platform taxonomy the pod thunks branch on
