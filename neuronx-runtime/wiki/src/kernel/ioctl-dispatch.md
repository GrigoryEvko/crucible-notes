# IOCTL Dispatch and the Privilege-Gate Model

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0` — `neuron_cdev.c` (4043 lines) and `neuron_ioctl.h` (876 lines), shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The source is read directly, not reverse-engineered; every claim is line-anchored. Other driver versions renumber lines and add/remove commands.*
> *Evidence grade: **Confirmed (source-anchored)** — the dispatcher, the three gates, every overload-resolution site, and the marshalling helper are transcribed from the shipped `.c`/`.h`. Part — Kernel Driver · [back to index](../index.md)*

## Abstract

`ncdev_ioctl` (`neuron_cdev.c:3188`) is the single security-critical front door of the Neuron kernel driver. One `/dev/neuronN` char node exists per bound PCI accelerator, and userspace (`libnrt.so`) drives the device almost entirely through this one `.unlocked_ioctl` handler (`ncdev_fops`, `:3540-3547`) plus `.mmap`. There is no `.compat_ioctl`, so 32-bit callers are unsupported and the ABI is LP64-only — every pointer-typed `_IOR`/`_IOW` macro encodes `_IOC_SIZE == 8`. The command space uses base char `'N'` (`NEURON_IOCTL_BASE`, `neuron_ioctl.h:656`) and spans ~96 distinct command macros over a sparse `_IOC_NR` set `{1..135}`.

The dispatcher is best understood as a Linux `unlocked_ioctl` of the most ordinary shape — a giant `if`/`else-if` chain on `cmd` (`:3227-3384`) — wrapped in **three concentric authorization gates** and complicated by **two orthogonal overload schemes**. The gate model is the part a reimplementer must get exactly right, because it *is* the driver's entire userspace authorization story: there is no Linux capability check anywhere on the ioctl path (no `capable()`, `CAP_SYS_ADMIN`, or `ns_capable` — the sole `capable(CAP_SYS_RAWIO)` lives in an mmap path, `neuron_mmap.c:362`). Gate 0 is the device-node file permission (out of driver, default `root:neuron 0660`). Gate 1 is an access-mode split: a fd opened `O_WRONLY` is "free-access" and is routed *unconditionally* to a restricted, **ungated** misc-ioctl lane (`ncdev_misc_ioctl`, `:3147`). Gate 2 is the `npid_is_attached` "attach" sub-gate (`:3210-3225`): on a non-free-access fd, a fixed allow-list of ~19 mutating commands requires the calling process to be the `DEVICE_INIT` owner, else `-EACCES`. Everything not on that list runs un-PID-gated.

The two overload schemes are what make the dispatch chain non-trivial. **`_IOC_NR` collisions** put two distinct full commands at the same `nr` (e.g. `#2 DEVICE_READY` vs the historical `#2 DEVICE_RESET_STATUS`, and the current `#106 RESET_STATUS` vs `BDF_EXT`), resolved by dispatch *order* and by access-mode lane. **Struct-version overloads** put two or three differently-sized structs at the same `nr` and same direction (`MEM_COPY`/`64`, `MEMSET`/`64`, `MEM_ALLOC_V2`/`V2MT`/`V2MT64`, `MEM_COPY_ASYNC`/`64`, `DMA_COPY_DESCRIPTORS`/`64`, …); the dispatcher matches these by `_IOC_NR(cmd) == _IOC_NR(BASE)` and the handler re-disambiguates by exact `cmd ==` (which includes `_IOC_SIZE`) or by `_IOC_SIZE(cmd)` directly. The interaction between exact-cmd gating and `_IOC_NR` dispatch produces the page's headline correctness finding: the attach sub-gate, which tests *exact full cmd values*, silently fails to cover the `*64` size-overload siblings that the switch reaches by `_IOC_NR` — the **`*64` sub-gate slip**.

For reimplementation, the contract is:

- **The three-gate model** — the `O_WRONLY` free-access split (`IS_NEURON_DEVICE_FREE_ACCESS`, `:52`, applied at `:3206`), the `npid_is_attached` attach sub-gate allow-list (`:3210-3225`), and the absence of any capability layer.
- **The dispatch chain** — the exact ordering rules (`DEVICE_READY` before `RESET_STATUS`), the `_IOC_NR`-vs-exact-`cmd` matching policy per command, and the `ncdev_misc_ioctl` fall-through for backward compatibility (`:3387`).
- **The `_IOC` overload-resolution discipline** — how struct-version siblings are demultiplexed inside each handler by exact `cmd ==` or by `_IOC_SIZE(cmd)`, and why the `*64` variants slip the attach gate.
- **The marshalling helper** — `neuron_copy_from_user` (`:87-93`); the dispatcher itself copies nothing and passes the raw user VA down as `void *param`.

| | |
|---|---|
| **Dispatch entry** | `ncdev_ioctl(filep, cmd, param)` — `neuron_cdev.c:3188`, `.unlocked_ioctl` |
| **Misc / free-access lane** | `ncdev_misc_ioctl(filep, cmd, param)` — `:3147` (14 commands, no gate) |
| **fops** | `ncdev_fops` — `:3540-3547`; `open`/`flush`/`release`/`unlocked_ioctl`/`mmap`; **no `.compat_ioctl`** |
| **Gate 0** | `/dev/neuronN` file permission (out of driver, default `root:neuron 0660`) |
| **Gate 1** | `IS_NEURON_DEVICE_FREE_ACCESS(filep)` `:52` → free-access split `:3206-3207` |
| **Gate 2** | `npid_is_attached(nd)` attach sub-gate `:3210-3225` (~19 cmds, else `-EACCES`) |
| **Capability layer** | **none** on any ioctl; sole `capable(CAP_SYS_RAWIO)` is `neuron_mmap.c:362` (mmap) |
| **Command base** | `'N' = 0x4E` (`neuron_ioctl.h:656`); LP64-only; ptr-form macros ⇒ `_IOC_SIZE = 8` |
| **Overload schemes** | `_IOC_NR` collision (order/lane-resolved) + struct-version (size-resolved) |
| **Marshalling** | `neuron_copy_from_user` `:87-93`; dispatcher passes raw user VA, copies nothing |

---

## 1. The Dispatcher

### Purpose

`ncdev_ioctl` is the body of `.unlocked_ioctl`. Its job is to (a) recover the per-node state and the `neuron_device`, (b) audit-log the call, (c) apply the access-mode split and the attach sub-gate, and (d) route `cmd` to one of ~70 family handlers, falling back to the misc lane for unrecognized commands. It holds **no lock** itself — `ncdev_lock` guards only the open/flush/release lifecycle (`open_count` and attach/detach); per-command locking is each handler's responsibility.

### Entry Point

```text
userspace ioctl(fd, cmd, &arg)
  └─ ncdev_ioctl (neuron_cdev.c:3188)            ── .unlocked_ioctl
       ├─ ncd = filep->private_data               ── NULL-guard :3193-3197
       ├─ nd  = ncd->ndev                         ── NULL-guard :3199-3202
       ├─ neuron_log_rec_add(.., FILE_IOCTL, cmd) ── audit :3204  [BOUNDARY: log cell]
       ├─ if FREE_ACCESS  → ncdev_misc_ioctl      ── GATE 1 split :3206-3207
       ├─ if cmd ∈ attach allow-list              ── GATE 2 sub-gate :3210-3225
       │      → require npid_is_attached(nd) else -EACCES
       ├─ giant if/else chain on cmd / _IOC_NR    ── dispatch :3227-3384
       │      → ncdev_<family>(nd[,cmd], (void*)param)   [BOUNDARY: handler cells]
       └─ default → ncdev_misc_ioctl(...)          ── B/W compat :3387
```

### Algorithm

The whole entry is short; the bulk is the dispatch chain. The structure (with the gates) is faithful to `:3188-3388`:

```c
function ncdev_ioctl(filep, cmd, param):                 // neuron_cdev.c:3188
    ncd = filep->private_data
    if ncd == NULL: return -EINVAL                        // :3194-3197

    nd = ncd->ndev
    if nd == NULL:  return -EINVAL                        // :3199-3202

    neuron_log_rec_add(nd, NEURON_LOG_TYPE_FILE_IOCTL, cmd)   // :3204  audit every call

    // ---- GATE 1: access-mode split ----
    if IS_NEURON_DEVICE_FREE_ACCESS(filep):               // :3206  ((f_flags & O_WRONLY) == 1)
        return ncdev_misc_ioctl(filep, cmd, param)        // :3207  ungated misc lane, NO further checks

    // ---- GATE 2: DEVICE_INIT-owner attach sub-gate (EXACT cmd values) ----
    if cmd in { DMA_ENG_INIT, DMA_ENG_SET_STATE, DMA_QUEUE_INIT, DMA_ACK_COMPLETED,
                DMA_QUEUE_RELEASE, DMA_COPY_DESCRIPTORS, MEM_ALLOC, MEM_FREE,
                MEM_COPY, MEM_GET_PA, MEM_COPY_ASYNC, MEM_COPY_ASYNC_WAIT,
                MEM_GET_INFO, BAR_WRITE, POST_METRIC, NOTIFICATIONS_INIT_V1,
                NOTIFICATIONS_INIT_V2, DRIVER_INFO_SET,
                NOTIFICATIONS_INIT_WITH_REALLOC_V2 }:     // :3210-3219  (19 entries)
        if !npid_is_attached(nd):                         // :3220
            npid_print_usage(nd); return -EACCES          // :3221-3223

    // ---- dispatch: exact-cmd OR _IOC_NR per command ----
    if cmd == NEURON_IOCTL_DEVICE_RESET:        return ncdev_device_reset_deprecated(nd)        // :3227
    else if cmd == NEURON_IOCTL_DEVICE_READY:   return ncdev_device_ready_deprecated(nd,param)  // :3229  (ORDER: before RESET_STATUS)
    else if cmd == NEURON_IOCTL_NC_RESET_READY: return ncdev_nc_reset_ready(nd, param)          // :3237
    else if cmd == NEURON_IOCTL_DEVICE_RESET_STATUS: return ncdev_device_reset_status_deprecated(nd,param) // :3239
    ...
    // struct-version families dispatched by NR (size demuxed inside the handler):
    else if _IOC_NR(cmd) == _IOC_NR(DMA_COPY_DESCRIPTORS): return ncdev_dma_copy_descriptors(nd, cmd, param) // :3271
    else if _IOC_NR(cmd) == _IOC_NR(MEM_ALLOC_V2MT):       return ncdev_mem_alloc_libnrt(nd, cmd, param)      // :3284
    else if _IOC_NR(cmd) == _IOC_NR(MEM_COPY):             return ncdev_mem_copy(nd, cmd, param)              // :3294
    else if _IOC_NR(cmd) == _IOC_NR(MEM_COPY_ASYNC):       return ncdev_mem_copy_async(nd, cmd, param)        // :3296
    else if _IOC_NR(cmd) == _IOC_NR(MEM_BUF_COPY):         return ncdev_mem_buf_copy(nd, cmd, param)          // :3300
    else if _IOC_NR(cmd) == _IOC_NR(MEM_BUF_ZEROCOPY64):   return ncdev_mem_buf_zerocopy64(nd, cmd, param)    // :3302
    else if _IOC_NR(cmd) == _IOC_NR(PROGRAM_ENGINE_NC):    return ncdev_program_engine_nc(nd, cmd, param)     // :3308
    else if _IOC_NR(cmd) == _IOC_NR(MEMSET):               return ncdev_memset(nd, cmd, param)                // :3310
    else if _IOC_NR(cmd) == _IOC_NR(MEM_MC_GET_INFO):      return ncdev_mem_get_mc_mmap_info(nd, cmd, param)  // :3358
    ... // ~70 handler arms total
    else
        return ncdev_misc_ioctl(filep, cmd, param)        // :3387  B/W-compat fall-through
```

Two design choices deserve the reimplementer's attention. First, the **fall-through to `ncdev_misc_ioctl` is deliberate** (`:3386-3387` "B/W compatibility"): a command that the main switch does not recognize gets a second chance in the misc lane, so commands that *belong* to the misc set (e.g. `DRIVER_INFO_GET`, `HOST_DEVICE_ID_TO_RID_MAP`, the POD family) remain reachable from a non-free-access fd too. Second, the dispatch is **not** keyed on `_IOC_DIR` except in one handler (`ncdev_driver_info`, §3): the same `nr` with two directions is normally split by being placed in different lanes, not by the dispatcher inspecting the direction.

### Function Map

| Function | Location | Role | Confidence |
|---|---|---|---|
| `ncdev_ioctl` | `:3188` | `.unlocked_ioctl`; gates + main switch + B/W fall-through | HIGH |
| `ncdev_misc_ioctl` | `:3147` | free-access / B/W-compat subset (14 cmds, no gate) | HIGH |
| `ncdev_fops` | `:3540` | `open`/`flush`/`release`/`unlocked_ioctl`/`mmap`; no `compat_ioctl` | HIGH |
| `neuron_copy_from_user` | `:87` | `copy_from_user` wrapper, `pr_err` on fault | HIGH |
| `ncdev_mem_handle_to_mem_chunk` | `:75` | handle → `mem_chunk` lookup + `MEMCHUNK_MAGIC` validate | HIGH |
| `ncdev_mem_chunk_to_mem_handle` | `:65` | `mc` → handle (lazy `nmch_handle_alloc`) | HIGH |
| `ncdev_ncid_valid` | `:95` | bounds-check `nc_id < nc_per_device` → `-E2BIG` | HIGH |
| `npid_is_attached` | `neuron_pid.c:60` | attach sub-gate predicate (identity, not privilege) | HIGH (boundary) |

---

## 2. The Three-Tier Gate Model

This is the security spine of the page. There are exactly three gates, applied in order, and **no Linux capability layer** above them. Once a process holds an fd to `/dev/neuronN`, the only remaining checks are the access-mode lane it chose and the attach table.

```text
GATE 0  — DEVICE-NODE PERMISSION  (out of driver)
          /dev/neuronN, default root:neuron 0660. The ONLY mandatory authentication.
          Everything below presumes an open fd.
                                   │
        ┌──────────────────────────┴──────────────────────────┐
   O_WRONLY fd                                            O_RDONLY / O_RDWR fd
   (free-access)                                          (full path)
        │                                                      │
GATE 1  ▼  IS_NEURON_DEVICE_FREE_ACCESS (:52, :3206)    GATE 2  ▼  attach sub-gate (:3210-3225)
   → ncdev_misc_ioctl (:3147)                            if cmd ∈ allow-list (19 cmds):
   14 misc cmds, NO npid / uid / cap gate AT ALL            require npid_is_attached(nd)
   (CRWL mark/unmark, PRINTK, POD_*, DMABUF_FD,             else -EACCES
    DEVICE_BASIC_INFO, BDF_EXT, DRIVER_INFO_GET,         everything NOT in the list runs
    RID_MAP, NC_PID_STATE_DUMP, L2P_NC_MAP,                un-PID-gated on this fd
    GET_VA_PLACEMENT, COMPATIBLE_VERSION)              → main switch (:3227-3384)

CAP LAYER — NONE on ioctls. Sole capable() = CAP_SYS_RAWIO, neuron_mmap.c:362 (mmap path).
```

### Gate 1 — the free-access (`O_WRONLY`) split

The split is a macro and a single `if`:

```c
#define IS_NEURON_DEVICE_FREE_ACCESS(filep) ((filep->f_flags & O_WRONLY) == 1)   // :52
...
if (IS_NEURON_DEVICE_FREE_ACCESS(filep))                                          // :3206
    return ncdev_misc_ioctl(filep, cmd, param);                                   // :3207
```

A fd opened **exactly `O_WRONLY`** short-circuits the *entire* main path: it can only ever reach `ncdev_misc_ioctl` and its 14-command subset, and that subset has **no `npid`, uid, or capability check of any kind**. This is by design — `libnrt` opens one process-global `O_WRONLY` fd precisely to reach device-global, attach-free commands (topology/discovery: `DEVICE_BASIC_INFO`, `BDF_EXT`, `COMPATIBLE_VERSION`, `DRIVER_INFO_GET`, the POD info family, `GET_VA_PLACEMENT`) without first becoming a `DEVICE_INIT` owner. The matching open-path setup is in `ncdev_open` (`:3400-3403`): a free-access fd sets `private_data` and **early-returns without `npid_attach`**.

> **GOTCHA —** the free-access lane is not purely read-only topology. Three of its commands are *state-changing* with no attach gate: `CRWL_NC_RANGE_MARK`/`UNMARK` (`:3148-3151`, NeuronCore reservation against a device-spanning allocator), `POD_CTRL` (`:3177`, pod-election control), and `PRINTK` (`:3165`). A reimplementation that assumes "writable fd ⇒ harmless info queries" is wrong: an `O_WRONLY` opener can mutate the cooperative core-reservation map and pod state without ever attaching. The exploitability of these is deferred to [the attack-surface page](../security/ioctl-attack-surface.md) (findings S5/S8, and the `PRINTK` OOB read S1).

> **QUIRK —** the macro tests `(f_flags & O_WRONLY) == 1`, not a mask against `O_ACCMODE`. Because `O_RDONLY == 0`, `O_WRONLY == 1`, `O_RDWR == 2`, this is true *only* for an exact `O_WRONLY` open; an `O_RDWR` fd (value `2`) is **not** free-access and takes the full path. Correct by current intent, but a latent foot-gun for anyone who reasons "writable ⇒ free-access". The clean form is `((f_flags & O_ACCMODE) == O_WRONLY)`.

### Gate 2 — the `npid_is_attached` attach sub-gate

On a non-free-access fd, a fixed allow-list of ~19 mutating commands is checked against the attach table before dispatch (`:3210-3225`). `npid_is_attached(nd)` (`neuron_pid.c:60`) returns the open-count of the current `tgid`'s slot in the device's 16-slot PID table — i.e. "did *this* process open and `DEVICE_INIT` this `nd`". It is an **identity** check, carrying no privilege semantics: it does not consult uid, caps, or namespaces. The full allow-list (transcribed verbatim from `:3210-3219`) is the DMA/mem/notification mutators plus `BAR_WRITE`, `POST_METRIC`, and `DRIVER_INFO_SET`:

| Group | Commands gated by `npid_is_attached` |
|---|---|
| DMA control | `DMA_ENG_INIT`, `DMA_ENG_SET_STATE`, `DMA_QUEUE_INIT`, `DMA_ACK_COMPLETED`, `DMA_QUEUE_RELEASE`, `DMA_COPY_DESCRIPTORS` |
| Memory | `MEM_ALLOC`, `MEM_FREE`, `MEM_COPY`, `MEM_GET_PA`, `MEM_COPY_ASYNC`, `MEM_COPY_ASYNC_WAIT`, `MEM_GET_INFO` |
| Raw MMIO / metric | `BAR_WRITE`, `POST_METRIC` |
| Notifications / driver | `NOTIFICATIONS_INIT_V1`, `NOTIFICATIONS_INIT_V2`, `NOTIFICATIONS_INIT_WITH_REALLOC_V2`, `DRIVER_INFO_SET` |

Every command **not** in this list runs un-PID-gated on a non-free-access fd. Notably ungated: `BAR_READ` (raw MMIO read, `#11`), `PROGRAM_ENGINE`/`_NC` (`#27`/`#105`, arbitrary engine DMA), `EVENT_GET`/`SET` (`#45`/`#46`), `SEMAPHORE_*`, `DMA_QUEUE_COPY_START`, the `*_GET_STATE` queries, `NC_RESET`, and `HBM_SCRUB`. The access boundary for these is *purely* the device-node permission (Gate 0).

> **NOTE —** there is no capability check on any of the high-power commands. `BAR_WRITE` is a raw MMIO `writel` (`ncdev_bar_rw`, `:3326`); `PROGRAM_ENGINE` drives engine DMA (`:3306`). They are reachable by any holder of the device fd, gated only by the attach table (for `BAR_WRITE`) or nothing at all (for `BAR_READ`/`PROGRAM_ENGINE`). BAR access *is* range-checked inside the handler (BAR0-only, blocklist-filtered) — but that is a bounds check, not an authorization check. The design consequence: per-tenant device isolation (one node per container) is the only real privilege boundary. This is finding S4 on [the attack-surface page](../security/ioctl-attack-surface.md).

### The `*64` sub-gate slip

The single most important interaction on this page is between Gate 2's *exact-cmd* tests and the dispatcher's *`_IOC_NR`* matching for struct-version families. The attach sub-gate at `:3210-3219` lists base commands by **exact full value**:

```c
if (... cmd == NEURON_IOCTL_MEM_COPY              // :3214   = 0x801C4E17 (size 28)
       cmd == NEURON_IOCTL_MEM_COPY_ASYNC         // :3215   (base async)
       cmd == NEURON_IOCTL_DMA_COPY_DESCRIPTORS   // :3212   (base) ...) {
    if (!npid_is_attached(nd)) return -EACCES;    // :3220-3224
}
```

But the dispatcher reaches these same families by **`_IOC_NR`** (`:3294`, `:3296`, `:3271`), which is true for *both* the 32-bit base and the 64-bit sibling — they share `nr`, differing only in `_IOC_SIZE`. So for `MEM_COPY64` / `MEM_COPY_ASYNC64` / `DMA_COPY_DESCRIPTORS64`, the test `cmd == NEURON_IOCTL_MEM_COPY` is **false** (the size field differs) ⇒ `npid_is_attached` is **skipped**, yet `ncdev_mem_copy`/`async`/`desc` still runs.

> **GOTCHA —** the attach sub-gate covers the 32-bit `MEM_COPY`/`MEM_COPY_ASYNC`/`DMA_COPY_DESCRIPTORS` but **not** their `*64` siblings. A process that opened the device but never became the `DEVICE_INIT` owner can issue `MEM_COPY64`/`ASYNC64`/`DESC64` against memory handles that the owner-gate was meant to reserve. This is *not* a free-access bypass — `O_WRONLY` short-circuits to `ncdev_misc_ioctl`, which has no `MEM_COPY*` branch at all — so it requires a non-free-access (`O_RDONLY`) fd. The blast radius is bounded by handle ownership (`ncdev_mem_handle_to_mem_chunk` resolves only *this* `nd`'s handles, `:75-83`) and by `mc_access_is_within_bounds` (`:805`/`:810`). The clean fix is to make the three sub-gate tests `_IOC_NR(cmd) == _IOC_NR(...)` (or add the `*64` constants). Full reachability analysis is finding S3 on [the attack-surface page](../security/ioctl-attack-surface.md).

---

## 3. Overload Resolution — `_IOC_NR`, `_IOC_SIZE`, `_IOC_DIR`

The command space is intentionally overloaded in two orthogonal ways, and the resolution discipline differs per scheme. A reimplementer must reproduce both, because the userspace `libnrt` selects which struct/variant to send and the kernel demultiplexes on the encoded `dir`/`size`/`nr`.

### Scheme A — `_IOC_NR` collisions (order- and lane-resolved)

Two genuinely distinct full commands share the same `nr`. There are two live cases.

**`#2 DEVICE_READY` vs `DEVICE_RESET_STATUS` — resolved by dispatch order.** These are *different* `nr` today (`READY = _IOR('N',2,__u8)` `neuron_ioctl.h:660`; `RESET_STATUS = _IOR('N',106,__u8)` `:800`), but older drivers assigned **both to ioctl 2**, and the source comment (`:3230-3235`) preserves the contract: `DEVICE_READY` must be tested *before* `RESET_STATUS` so a legacy `cmd` value resolves to the real wait-for-reset call (`READY`) rather than the no-op (`RESET_STATUS`). The dispatcher honors this by checking `DEVICE_READY` at `:3229` ahead of `DEVICE_RESET_STATUS` at `:3239`.

**`#106 DEVICE_RESET_STATUS` (`__u8`, size 1) vs `DEVICE_BDF_EXT` (ptr, size 8) — resolved by `_IOC_SIZE` + lane.** Both encode `nr = 106` (`:800`, `:803`), but their full `cmd` values differ in `_IOC_SIZE`. `RESET_STATUS` is matched by exact `cmd` in the main path (`:3239`); `BDF_EXT` is matched by exact `cmd` in the misc lane (`:3156`). The size field plus the access-mode lane disambiguate them cleanly.

### Scheme B — struct-version overloads (size-resolved inside the handler)

Same `nr`, same direction, two or three differently-sized structs. The dispatcher matches by `_IOC_NR(cmd) == _IOC_NR(BASE)` and the handler re-disambiguates. Two handler idioms exist.

**Idiom 1 — exact `cmd ==` (size is folded into the constant).** `ncdev_mem_copy` (`:761`) is the canonical example. The two macros `MEM_COPY` (`:686`) and `MEM_COPY64` (`:687`) share `nr = 23` but differ in `_IOC_SIZE` (28 vs 40 bytes), so an exact compare against each constant works:

```c
function ncdev_mem_copy(nd, cmd, param):                  // neuron_cdev.c:761
    static_assert(NEURON_IOCTL_MEM_COPY != NEURON_IOCTL_MEM_COPY64)   // :763  guards the constants differ
    if cmd == NEURON_IOCTL_MEM_COPY:                       // :774  32-bit size/offset struct
        copy_from_user(&arg32, param, sizeof(arg32))       // :776  28 bytes
        size = arg32.size; src_off = arg32.src_offset; ...
    else if cmd == NEURON_IOCTL_MEM_COPY64:                // :785  64-bit size/offset struct
        copy_from_user(&arg64, param, sizeof(arg64))       // :787  40 bytes
        size = arg64.size; src_off = arg64.src_offset; ...
    else
        return -EINVAL                                     // :796
    // both widen to u64 locals; mc_access_is_within_bounds clamps offset+size :805,:810
```

The same idiom appears in `ncdev_memset` (`:709`, `MEMSET`/`64`), `ncdev_mem_alloc_libnrt` (`:456`, a 3-way `V2`/`V2MT`/`V2MT64` split, each guarded by a `static_assert` at `:458-460`), and `ncdev_mem_copy_async` (`:823`), which additionally uses a C `union { a; a64; }` (`:837-840`) and a runtime `arg_size = sizeof(a | a64)` (`:845`/`:858`) so the result `copy_to_user` writes back exactly the width the caller sent (`:897`).

**Idiom 2 — `_IOC_SIZE(cmd)` compare.** `ncdev_mem_get_mc_mmap_info` (`:628`) keys directly on the encoded size rather than the full constant:

```c
function ncdev_mem_get_mc_mmap_info(nd, cmd, param):      // neuron_cdev.c:628
    static_assert(sizeof(arg) != sizeof(arg_v2))           // :632  sizes MUST differ for this to work
    if _IOC_SIZE(cmd) == sizeof(arg):                      // :637  v1
        copy_from_user(&arg, param, sizeof(arg))           // :638
        ... return copy_to_user(param, &arg, sizeof(arg))  // :650
    else if _IOC_SIZE(cmd) == sizeof(arg_v2):              // :651  v2 (adds mem_handle out-field)
        copy_from_user(&arg_v2, param, sizeof(arg_v2))     // :652
        ncdev_mem_chunk_to_mem_handle(nd, mc, &arg_v2.mem_handle)  // :664
        ... return copy_to_user(param, &arg_v2, sizeof(arg_v2))    // :668
    else
        return -EINVAL                                     // :670
```

A third, more defensive variant appears in `ncdev_crwl_nc_range_mark`/`unmark` (`:2163`/`:2220`): an incoming `_IOC_SIZE` equal to `sizeof(ptr)` is *folded* to the v1 struct size, and any other size must equal `sizeof(_ext)` or `-EINVAL` (`:2186-2190`). This tolerates both the legacy pointer-form macro and the extended 8-qword-bitmap form against one handler.

> **QUIRK —** the `_IOC_SIZE`-only difference is sometimes *manufactured*. `neuron_ioctl_mem_alloc_v2_mem_type64` is byte-identical to `..._v2_mem_type` except for a trailing `__u32 pad` (`neuron_ioctl.h:52-62`). The header comment (`:46-51`) is explicit: the pad exists *solely* to bump `_IOC_SIZE` so that `libnrt` (NDL) can feature-probe whether the driver has `>=4GB` allocation/copy support, by checking which size the driver accepts. The field is never read by the handler — its only purpose is to make the command constant differ.

### `_IOC_DIR` — the lone direction-keyed handler

Only one command is resolved by `_IOC_DIR`: `DRIVER_INFO` (`#110`), which has a `GET` (`_IOR`, `:816`) and a `SET` (`_IOW`, `:817`) sharing `nr = 110`. `ncdev_driver_info` (`:1896`) inspects the direction and the size:

```c
function ncdev_driver_info(cmd, param):                   // neuron_cdev.c:1896
    dir  = _IOC_DIR(cmd); size = _IOC_SIZE(cmd)            // :1899-1900
    if dir == _IOC_WRITE:                                  // :1902  SET
        return -ENOTSUPP                                   // :1903  effectively unimplemented
    else if dir == _IOC_READ:                              // :1904  GET
        if size >= _IOC_SIZE(DRIVER_INFO_GET):             // :1907  fwd/bwd-compat size gate
            fill driver_info { arch, revision, version=0, feature_flags1 }   // :1908-1916
            return copy_to_user(param, &driver_info, sizeof(driver_info))    // :1918
    return -EINVAL                                         // :1923
```

`DRIVER_INFO_GET` is dispatched in the misc lane by `_IOC_NR` (`:3163`), so it is reachable on a free-access fd; `DRIVER_INFO_SET` is in the Gate-2 allow-list (`:3218`) but the handler returns `-ENOTSUPP` for the write direction — so `SET` is effectively a no-op regardless of how it arrives. The reported `feature_flags1` advertises every capability bit `DMABUF…ZEROCOPY` set (`:1912-1916`), with `version = NEURON_DEVICE_DRIVER_INFO_VERSION0` (`:1910`).

---

## 4. Argument Marshalling

The dispatcher copies **nothing**. It passes the raw user VA down as `void *param`, and every handler performs its own fixed-struct `copy_from_user` via one wrapper:

```c
function neuron_copy_from_user(fname, to, from, n):       // neuron_cdev.c:87
    ret = copy_from_user(to, from, n)                      // :88
    if ret: pr_err("copy_from_user failed: %s", fname)     // :89-91  (logs the caller name)
    return ret                                             // :92
```

The uniform pattern in every handler is: `neuron_copy_from_user(&arg, param, sizeof(arg))` onto a fixed stack struct, do the HW/state work (resolving any handle via `ncdev_mem_handle_to_mem_chunk`, `:75`, which validates `MEMCHUNK_MAGIC` at `:79`), then `copy_to_user(param, &result, sizeof(result))` for out-fields. The wrapper adds nothing but a diagnostic `pr_err` carrying `__func__`; it does **not** validate `n`, bound the pointer, or pre-check `access_ok` beyond what `copy_from_user` itself does.

> **GOTCHA —** because the size passed to the second copy is taken from the *handler's* struct (`sizeof(arg)`), not from the ioctl's `_IOC_SIZE`, a handler that derives a *count* or *index* from the copied struct without re-validating it can read out of bounds. The concrete instance is `ncdev_printk` (`:2333`): it bounds `arg.size` from above (`> sizeof(str)`, `:2342`) but not from below, so `arg.size == 0` makes the trailing `str[arg.size - 1]` index `str[0xFFFFFFFF]` — a wild stack read (`:2349`). That is finding S1 on [the attack-surface page](../security/ioctl-attack-surface.md); the marshalling helper is structurally fine, but it is not a validation layer, and reimplementers must not treat it as one. The symmetric per-handler `nc_id` check `ncdev_ncid_valid` (`:95`) is present in `ncdev_semaphore_ioctl` (`:1437`) but **absent** in `ncdev_events_ioctl` (`:1457`) — the kind of asymmetry the dispatcher cannot catch (finding S2).

---

## 5. Command Lanes at a Glance

The dispatch space is too large to table per-command here (that is the [IOCTL Catalog](ioctl-catalog.md)'s job); the *shape* is three lanes plus the deprecated stubs. The dimensions a reimplementer needs:

| Axis | Values | Source |
|---|---|---|
| Lane | main-path (`O_RDONLY`/`O_RDWR`) · misc/free-access (`O_WRONLY`) · B/W-compat fall-through | `:3206`, `:3147`, `:3387` |
| Gate | none · attach (`npid_is_attached`) · free-access-ungated | `:3210-3219`, `:3206` |
| Match | exact `cmd ==` · `_IOC_NR ==` · `_IOC_SIZE ==` · `_IOC_DIR ==` | per-arm, §3 |
| Overload | single · struct-version (`/64`, `/V2`, `/V2MT*`) · `nr`-collision | `neuron_ioctl.h:686-803` |
| Status | live handler · deprecated inline `return 0` · removed `-ENOIOCTLCMD`/`-1` stub | `:3249`, `:3280`, `:3340` |

The 14 commands reachable on the free-access (`O_WRONLY`) misc lane (`ncdev_misc_ioctl`, `:3148-3180`) are: `CRWL_NC_RANGE_MARK`/`UNMARK` (+`_EXT0`), `COMPATIBLE_VERSION`, `DEVICE_BASIC_INFO`, `DEVICE_BDF_EXT`, `DMABUF_FD`, `DRIVER_INFO_GET`, `PRINTK`, `HOST_DEVICE_ID_TO_RID_MAP`, `NC_PID_STATE_DUMP`, `GET_LOGICAL_TO_PHYSICAL_NC_MAP`, `POD_INFO`, `POD_STATUS`, `POD_CTRL`, `GET_VA_PLACEMENT`. An unrecognized `cmd` in this lane returns `-EINVAL` with a `pr_err` dumping `dir/type/nr/size` (`:3183-3185`).

The deprecated/stub arms are worth naming because a reimplementer will otherwise hunt for missing handlers: `DEVICE_INIT`/`DEVICE_RELEASE`/`DMA_ENG_INIT` inline `return 0` (`:3250`/`:3252`/`:3258`); `NOTIFICATIONS_DESTROY_V1` `return 0` (`:3339`); `NOTIFICATIONS_QUEUE_INFO` `return -1` (`:3341`); `DMA_QUIESCE_QUEUES` returns `-ENOIOCTLCMD` with a "no longer supported" log (`:3279-3281`).

---

## 6. Lifecycle Coupling

The dispatcher's gates depend on state set up by the open/flush/release path (owned in detail by [the cdev/mmap page](cdev-mmap.md)); the relevant coupling is summarized here because it determines whether Gate 2 can pass.

- **`ncdev_open` (`:3390`)** — a free-access fd sets `private_data` and **returns without attaching** (`:3400-3403`); a full-path fd increments `open_count`, **waits** while `device_state == RESET` (busy-loop on `schedule()`, signal-checked, `:3411-3420`), then `npid_attach(nd)` (`:3430`) which populates the 16-slot table that Gate 2 later tests. A failed attach yields `-EBUSY` (`:3435`).
- **`ncdev_flush` (`:3452`)** — on the last attach (`attach_cnt == 1`, `:3470`) tears down per-process DMA/CRWL/datastore/mmap state and `npid_detach`s (`:3472-3492`); a free-access fd instead unmarks all its CRWL cores (`ncdev_misc_flush`, `:3442`).
- **`ncdev_release` (`:3499`)** — on `open_count == 0` clears the datastore and frees all-process MCs (`:3512-3516`).

> **NOTE —** Gate 2 can only pass for a process that completed `ncdev_open`'s attach. The busy-wait in `ncdev_open` means an fd opened while the device is mid-reset blocks in `open()`, not in the ioctl — so by the time any gated command is issued, the device is past `RESET` and the attach slot is live. There is no per-command device-state recheck in the dispatcher.

---

## Related Components

| Component | Relationship |
|---|---|
| `ncdev_misc_ioctl` (`:3147`) | the free-access / B/W-compat lane that Gate 1 routes to |
| `npid_*` (`neuron_pid.c`) | the 16-slot attach table backing Gate 2; identity, not privilege |
| `neuron_log_rec_add` | per-call audit record (`FILE_IOCTL`) emitted before any gate (`:3204`) |
| per-family handlers (`mem`/`dma`/`nq`/`crwl`/`pod`/`hbm`/`power`) | dispatch targets; each owns its own marshalling and locking |

## Cross-References

- [Char Device, fops and mmap](cdev-mmap.md) — `ncdev_open`/`flush`/`release`/`mmap`, the free-access open path, and the `nmmap_mem` delegation that backs `.mmap`
- [IOCTL Catalog](ioctl-catalog.md) — the full per-command table: `nr`, direction, arg struct, `_IOC_SIZE`, handler line, lane, and gate for all ~96 commands
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the security analysis deferred from this page: the `*64` sub-gate slip (S3), the ungated free-access state-changers (S5/S8), the `PRINTK` size-0 OOB read (S1), the unvalidated `EVENT_*` `nc_id` (S2), and the absent capability layer (S4)
