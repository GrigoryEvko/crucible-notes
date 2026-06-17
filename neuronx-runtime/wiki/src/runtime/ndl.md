# NDL: The IOCTL/mmap Shim

> *All addresses, offsets, and symbol names on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`, `.rodata`, **and `.data` are VMA == file offset** (`.bss` is `NOBITS`). DWARF `addr2line` attributes every function on this page to one translation unit — `src/ndl.c` of package **`KaenaDriverLib-2.27.4.0`** — which is *first-party AWS* C source (the "Neuron Driver Layer"), statically linked into `libnrt.so`. Other versions will differ.*
>
> *Evidence grade: **Confirmed (DWARF + disasm)** — source attribution by `addr2line` on every address; every `ioctl` request code read from the decompiled `.c`, re-verified against the objdump immediate, decoded with the Linux `_IOC` macro, and matched 1:1 to the kernel command table. · Part IV — TDRV Runtime · [back to index](../index.md)*

## Abstract

The Neuron Driver Layer (`ndl_*`) is the bottom edge of `libnrt`: the thin userspace shim that turns every device operation into an `open()`/`ioctl()`/`mmap()` against the `/dev/neuronN` char device. Above it sit the memory layer (`dmem_*`), device bring-up (`tdrv_*`), the EFA/RDMA glue, and the public `nrt_*` ABI; below it is the kernel's `ncdev_ioctl` dispatcher. Each `ndl_*` function does almost nothing of its own: it packs a fixed-size `neuron_ioctl_*` argument struct on the stack, resolves a file descriptor, issues exactly **one** `ioctl(2)` with a magic-`'N'` (`0x4E`) request code, and copies the result back out. The whole layer is a marshalling-and-dispatch table — a 1:1 mapping from a C function to a kernel command — and that 1:1-ness is the property a reimplementer reproduces.

What makes the layer non-trivial is *which fd the `ioctl` lands on*. There are **three gateways**, and the choice is not cosmetic: it selects the kernel's privilege lane (the `O_WRONLY` free-access split documented by [ioctl-dispatch](../kernel/ioctl-dispatch.md)). Gateway one, `ndl_device_ioctl` (`@0xc1d20`), is the per-device path: it opens `/dev/neuron%d` **`O_RDONLY`** and caches the fd in a 64-entry array installed with a lock-free compare-and-swap, so the `N`-th device's fd is opened once and reused by every later per-device `ioctl`. Gateway two, `ndl_misc_ioctl` (`@0xc2220`), is the device-global path: it lazily opens *one* process-wide fd `O_WRONLY` (so the kernel routes it to the ungated misc lane) and registers an `atexit` closer. Gateway three is the family of mmap wrappers (`ndl_mmap_bar_region_` `@0xc1df0` and the memory-map helpers) that pair an info `ioctl` with an `mmap(2)` on a device fd, turning a kernel-returned offset cookie into a usable virtual address.

A reimplementer should read this page as the *shape* of the shim, not the full command list. The complete per-command table — every `nr`, direction, arg struct, kernel handler, and gate tier — is owned by the [IOCTL Catalog](../kernel/ioctl-catalog.md); the dispatch and gate mechanism is owned by [ioctl-dispatch](../kernel/ioctl-dispatch.md). This page documents the three gateways, the lock-free fd cache, the uniform wrapper shape, and the `_IOC` encoding the wrappers feed — then links to the catalog for a representative slice rather than re-tabulating ~90 rows.

For reimplementation, the contract is:

- **The three-gateway model** — per-device (`O_RDONLY`, cached) vs. device-global (`O_WRONLY`, single shared) vs. mmap-pairing, and *why each fd lane is chosen* (the kernel's free-access split keys on `O_WRONLY`).
- **The lock-free fd cache** — a 64-entry `int[]` indexed by `device_index`, opened on first miss and installed by `CAS` against the sentinel `-1`, with the loser of a CAS race closing its surplus fd.
- **The wrapper shape** — pack a fixed `neuron_ioctl_*` struct, issue one `ioctl('N', …)`, return (most paths normalize to `-errno`); and the `_IOC` request-code encoding the wrapper hard-codes.
- **The version-lag** — the shim's own source package (`KaenaDriverLib 2.27.4.0`) lags the runtime it ships inside (`2.31.24.0`), which sets the kernel-ABI baseline the wrappers target.

| | |
|---|---|
| **Source TU** | `KaenaDriverLib-2.27.4.0/src/ndl.c` (DWARF `addr2line`, every address) |
| **Per-device gateway** | `ndl_device_ioctl` `@0xc1d20` (`ndl.c:178`) — `/dev/neuron%d` `O_RDONLY`, fd-cached |
| **Device-global gateway** | `ndl_misc_ioctl` `@0xc2220` (`ndl.c:224`) — one `O_WRONLY` fd, `atexit` close |
| **mmap gateway** | `ndl_mmap_bar_region_` `@0xc1df0` (`ndl.c:1055`) + the memory-map helpers |
| **fd cache** | `ndl_device_fd_cache` `@0xc08920` (`.data`, `int[64]`, `-1` = empty) |
| **fd-cache close-all** | `ndl_device_cached_fd_close_all` `@0xc4c60` (called from `nrt_close`) |
| **misc fd** | `fd_misc_devnode` `@0xc5d760` (`.bss`, `int`); closer `close_misc_devnode` `@0xc19f0` |
| **Command base** | `'N' = 0x4E` (`NEURON_IOCTL_BASE`); LP64-only; ptr-form macros ⇒ `_IOC_SIZE = 8` |
| **Error convention** | per-device path normalizes to `-errno`; raw-fd `_cm` copy/memset paths return the raw `ioctl` rc |

---

## 1. The Three-Gateway Model

Every `ndl_*` function reaches the kernel through one of three gateways, and the gateway is chosen by what *kind* of command it is — per-device, device-global, or a memory mapping. The choice fixes two things at once: the file descriptor, and (because the kernel's `IS_NEURON_DEVICE_FREE_ACCESS` macro tests `(f_flags & O_WRONLY) == 1`, [ioctl-dispatch §Gate 1](../kernel/ioctl-dispatch.md#gate-1--the-free-access-o_wronly-split)) the kernel privilege lane.

| Gateway | fd scope | open mode | cache | symbol@addr | Confidence |
|---|---|---|---|---|---|
| **per-device** | one fd per `/dev/neuronN` | `O_RDONLY` | `ndl_device_fd_cache[64]`, CAS-installed | `ndl_device_ioctl` `@0xc1d20` | HIGH |
| **per-device (handle-cached)** | fd held inside the mem-handle | `O_RDONLY`† | none (reuses the handle's captured fd) | direct `ioctl(handle->device->context.nd_fd, …)` | HIGH |
| **device-global / misc** | one process-wide fd | `O_WRONLY` | single static `fd_misc_devnode` | `ndl_misc_ioctl` `@0xc2220` | HIGH |
| **mmap** | a device fd + `mmap(2)` | (fd from the above) | the returned VA is cached on the handle/window | `ndl_mmap_bar_region_` `@0xc1df0` | HIGH |

† The handle-cached path does not itself open anything: the fd was opened by `ndl_open_device` (`@0xc30f0`, `O_RDWR`) or by the per-device gateway and captured into the device/mem-handle. Its access mode is inherited, not chosen at the `ioctl` site.

The per-device gateway carries the bulk of the traffic — **21 callers** across the NDL (DMA queue lifecycle, BAR/CSR mmap-info, board info, BDF, reset, program-engine, the `MEM_MC_GET_INFO` resolve), all reaching it as `ndl_device_ioctl(device_index, cmd, &arg)`. The misc gateway carries the device-global commands that the kernel exposes on the ungated free-access lane: `pod_*`, `printk`, `compatible_version`, `host_device_id_to_rid_map`, the CRWL NC-range mark/unmark, `get_va_placement`, `get_logical_to_physical_nc_map`. The `O_WRONLY` choice is *deliberate*: it routes these straight into `ncdev_misc_ioctl` without a `DEVICE_INIT`-owner attach, which is exactly what a topology/discovery query needs.

> **NOTE —** the per-device gateway opens **`O_RDONLY`**, which is *not* free-access (the kernel macro only matches the exact value `O_WRONLY == 1`). An `O_RDONLY` fd therefore takes the full privileged dispatch path, where some commands require the `npid_is_attached` attach sub-gate. The misc gateway opens `O_WRONLY` precisely to avoid that path. The two gateways thus straddle the kernel's Gate 1 split by design — see [ioctl-dispatch §2](../kernel/ioctl-dispatch.md#2-the-three-tier-gate-model).

> **GOTCHA —** the misc fd is opened `O_WRONLY`, yet it carries *read-type* commands (`pod_info`, `compatible_version`, `driver_info_get` are all `_IOR`/`_IOWR`). This works only because the kernel's misc lane never checks the fd's read permission against the command direction — it dispatches by `_IOC_NR` and `copy_to_user`s results regardless. A reimplementation that opens the misc fd `O_RDONLY` would land it on the *privileged* lane (wrong gate) and a `O_RDWR` (value `2`) also misses free-access. The exact-`O_WRONLY` open is what selects the gate, and the write-only mode is harmless for the read-back because the kernel ignores `_IOC_DIR` on this lane.

---

## 2. `ndl_device_ioctl` and the Lock-Free fd Cache

### Purpose

`ndl_device_ioctl` is the per-device gateway. Given a `device_index` (`0..63`), a request code, and an arg pointer, it resolves the device's fd — opening `/dev/neuron%d` `O_RDONLY` on the first call for that index and caching it — and issues one `ioctl`. The cache is a flat `int[64]` (`ndl_device_fd_cache` `@0xc08920`, `.data`), one slot per possible device, sentinel `-1` for "not yet open". Installation is lock-free: the opener writes its fd into the slot with an atomic compare-and-swap against `-1`, so concurrent first-touches of the same device do not need a mutex.

### Entry Point

```text
ndl_device_ioctl (0xc1d20, ndl.c:178)        ── per-device gateway, 21 callers
  ├─ bounds: device_index > 63 → -EINVAL
  ├─ fd = ndl_device_fd_cache[device_index]
  ├─ if fd == -1:
  │     snprintf("/dev/neuron%d", device_index)   ── 0xc1d96
  │     fd = open(path, O_RDONLY)                  ── 0x83f246 "/dev/neuron%d"
  │     CAS(&cache[index], -1, fd)                 ── _InterlockedCompareExchange
  │         └─ lost race → close(fd), reuse winner's fd
  ├─ ioctl(fd, cmd, arg)
  └─ rc < 0 → return -errno  (else return rc)
```

### Algorithm

The cache install is the only subtle part. The model below is faithful to `ndl.c:178`; the readable names replace the decompiler's `v9`/`v10` while the addresses let a verifier cross-check the immediates.

```c
function ndl_device_ioctl(device_index, cmd, arg):        // ndl_device_ioctl, 0xc1d20
    if device_index > 63:                                 // hard cap: 64 device nodes
        errno = EINVAL; return -22                         // > NEURON_MAX_DEV_NODES

    fd = ndl_device_fd_cache[device_index]                // .data int[64] @0xc08920, -1 = empty
    if fd == -1:
        char path[...]
        snprintf(path, "/dev/neuron%d", device_index)      // 0xc1d96, rodata "/dev/neuron%d" @0x83f246
        new_fd = open(path, O_RDONLY)                       // O_RDONLY ⇒ NOT free-access lane
        if new_fd < 0:
            return -errno                                   // open failed (e.g. node absent)

        // lock-free install: only the thread that finds the slot still
        // empty (-1) keeps its fd; every loser closes its surplus fd.
        if CAS(&ndl_device_fd_cache[device_index], -1, new_fd):   // _InterlockedCompareExchange
            fd = new_fd                                     // we won: our fd is now the cached one
        else:
            close(new_fd)                                   // we lost: drop ours…
            fd = ndl_device_fd_cache[device_index]          // …and use the winner's cached fd

    rc = ioctl(fd, cmd, arg)
    if rc < 0:
        return -errno                                       // normalize kernel errno to negative
    return rc
```

Three properties matter to a reimplementer. First, the cache is **never invalidated per-call** — once a slot holds an fd, every later `ioctl` for that device reuses it; the only teardown is the bulk `ndl_device_cached_fd_close_all` (`@0xc4c60`) walked from `nrt_close`, which `close()`s each non-`-1` slot and resets it. Second, the **CAS loser closes its own fd**, not the cache's — so the cache slot always holds exactly one live fd and the surplus opens are reaped immediately. Third, the gateway **normalizes errno to negative** (`-errno`) on failure, which is the per-device convention; the handle-cached `_cm` copy/memset paths (§3) instead return `ioctl`'s raw rc without normalization.

> **QUIRK —** the open is `O_RDONLY` but the commands issued through it routinely *write* device state (DMA queue init, NC reset, program-engine). The fd's read-only mode is irrelevant to the kernel's per-command behavior — the driver has no `.write`/`.read` fops and treats every `ioctl` uniformly. `O_RDONLY` is chosen only to stay *off* the `O_WRONLY` free-access lane, i.e. to land on the full privileged dispatch. Do not read `O_RDONLY` as "this gateway only reads".

> **GOTCHA —** the CAS install closes the loser's fd while another thread may already be mid-`ioctl` on the *winner's* fd — which is fine, the two fds are distinct. The narrow window worth auditing in a reimplementation is the opposite: a slot is only ever written `-1 → fd` (install) or `fd → -1` (bulk close-all). There is no per-slot `fd → fd'` replacement, so a stale/closed fd can only appear if `close_all` races a live `ioctl` at process teardown. The shim does not guard that race; callers must quiesce device traffic before `nrt_close`. (MEDIUM — the race window is structural, not observed firing.)

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ndl_device_ioctl` | `0xc1d20` | per-device gateway: bounds → cache → `O_RDONLY` open → CAS → `ioctl` → `-errno` | HIGH |
| `ndl_device_cached_fd_close_all` | `0xc4c60` | walk `fd_cache[0..63]`, `close()` each live fd, reset `-1`; from `nrt_close` | HIGH |
| `ndl_available_devices` | `0xc2180` | probe `/dev/neuron0..63` via `access(F_OK)`; record present indices, return count | HIGH |
| `ndl_misc_ioctl` | `0xc2220` | device-global gateway (§3) | HIGH |
| `close_misc_devnode` | `0xc19f0` | `atexit` handler: `close(fd_misc_devnode)` | HIGH |

---

## 3. The Wrapper Shape and the `_IOC` Encoding

### Purpose

Every leaf `ndl_*` function is the same shape: validate, pack a fixed `neuron_ioctl_*` struct, issue **one** `ioctl`, return. The struct layout is fixed per command (it *is* the kernel ABI), the request code is a compile-time constant baked into the call, and the only per-function logic is which fields to fill and which gateway to use. A reimplementer who has this shape plus the [catalog](../kernel/ioctl-catalog.md) row for a command can rebuild any wrapper.

### Algorithm

The canonical wrapper, modeled on the device-memory and pod families. The struct, the request code, and the gateway are the three things that vary; everything else is boilerplate.

```c
// Representative per-device wrapper (modeled on ndl_get_host_device_id @0xc4d20,
// ndl_hbm_scrub_start @0xc4e90, the DMA-queue family, etc.)
function ndl_<command>(device, <args...>):
    // 1. validate — guard clauses first
    if <bad arg>:
        return -EINVAL                                  // e.g. count > 64, size/offset > 0xFFFFFFFF

    // 2. marshal a FIXED neuron_ioctl_<command> struct on the stack
    struct neuron_ioctl_<command> arg = {0}
    arg.field0 = <in0>                                  // layout is the kernel ABI (see catalog)
    arg.field1 = <in1>
    // for handle-bearing commands, rewrite the user handle to the kernel handle:
    //   arg.mem_handle = ((u64*)user_handle)[1]        // handle record +8 = kernel_mem_handle

    // 3. issue ONE ioctl through the chosen gateway
    rc = ndl_device_ioctl(device->device_index,         // per-device gateway (O_RDONLY, cached)
                          NEURON_IOCTL_<COMMAND>,        // a compile-time _IOC constant
                          &arg)
    //  OR, for device-global commands:
    //   rc = ndl_misc_ioctl(NEURON_IOCTL_<COMMAND>, &arg)   // O_WRONLY free-access fd
    //  OR, for handle-cached commands (copy/memset/buf_copy):
    //   rc = ioctl(*(int*)(handle->device->context + 632), NEURON_IOCTL_<COMMAND>, &arg)

    // 4. copy any out-fields back, return
    if rc == 0:
        *<out0> = arg.<out_field0>
    return rc
```

The device fd of the handle-cached path is `*(int*)(handle->device->context)` — the `ndl_device_t` handle is 632 bytes and its trailing flexible `context` member (offset `+632`) reinterprets as `ndl_device_context_t { int nd_fd; }`. So the recurring idiom `*(_DWORD*)(device + 632)` and `handle->device->context.nd_fd` both name the same captured fd.

> **QUIRK —** handle-bearing commands rewrite the caller's opaque handle in place before the `ioctl`: `arg.mem_handle = ((u64*)user_handle)[1]`. The userspace mem-handle is a small record whose `+8` field is the *kernel* memory-handle id; the wrapper substitutes it so the kernel receives the id it issued at allocation, not the userspace pointer. `ndl_memory_buf_batch_copy` (`@0xc5a30`) does this per batch entry across a `0x28`-stride array. A reimplementer that passes the userspace handle straight through will hand the kernel a garbage id.

### The `_IOC` encoding

Every request code the wrappers issue is a Linux `_IOC` constant with magic char `'N'` (`0x4E`). The four fields pack as `dir<<30 | size<<16 | type<<8 | nr`:

```text
 31    30 29                16 15           8 7            0
 +--------+-------------------+--------------+--------------+
 |  dir   |       size        |   type='N'   |     nr       |
 +--------+-------------------+--------------+--------------+
   2 bits        14 bits          0x4E (8)       8 bits

 dir: 00=_IO  01=_IOW  10=_IOR  11=_IOWR
 size: _IOC_SIZE = sizeof(macro arg); POINTER-form macros encode 8 (LP64),
       STRUCT-form macros encode sizeof(struct) — this is how a "/64" sibling
       gets a distinct cmd from its base (same nr, different size field).
```

Worked decodes from the binary (immediates read verbatim from objdump; cross-checked to the kernel command table):

| `cmd` (hex) | `dir` | `nr` | size | command | issuing wrapper |
|---|---|---|---|---|---|
| `0xC0204E70` | `_IOWR` | 112 | 32 | `RESOURCE_MMAP_INFO` | `ndl_mmap_bar_region_` `@0xc1df0` |
| `0x80384E66` | `_IOR` | 102 | 56 | `MEM_ALLOC_V2MT64` | `ndl_memory_alloc` `@0xc2820` |
| `0x40104E71` | `_IOW` | 113 | 16 | `PRINTK` | `ndl_printk` `@0xc4ca0` |
| `0xC1144E7A` | `_IOWR` | 122 | 276 | `POD_STATUS_V2` | `ndl_pod_status` `@0xc51b0` |
| `0x40044E80` | `_IOW` | 128 | 4 | `METRICS_CTRL` | `ndl_metrics_ctrl` `@0xc5a00` |

> **NOTE —** the same `nr` can carry two or three sizes (`MEM_COPY`/`64`, `POD_STATUS`/`_V2`, `MEM_ALLOC_V2`/`V2MT`/`V2MT64`). The wrapper *selects* the variant by which struct (hence which `_IOC_SIZE`) it sends; the kernel demuxes by `_IOC_NR` at dispatch then by exact `cmd ==` or `_IOC_SIZE(cmd)` inside the handler. The full overload discipline is owned by [ioctl-dispatch §3](../kernel/ioctl-dispatch.md#3-overload-resolution--_ioc_nr-_ioc_size-_ioc_dir); the shim's role is only to pick the size.

---

## 4. The 1:1 Cross-Map (Representative Slice)

The defining property of the NDL is that it is a **1:1** map: one `ndl_*` function ⇒ one kernel command. The complete table (every `nr`, both halves of every size-overload, the kernel handler line, and the gate tier) is owned by the [IOCTL Catalog](../kernel/ioctl-catalog.md). The slice below is representative — one row per gateway and per arg-shape family — so a reimplementer learns the *pattern* without this page duplicating ~90 rows.

| `ndl_*` function (addr) | gateway | `cmd` / `nr` | arg struct (size) | Confidence |
|---|---|---|---|---|
| `ndl_get_host_device_id` `@0xc4d20` | per-device | `0x80044E72` / 114 | `neuron_ioctl_host_device_id` (4) | HIGH |
| `ndl_mmap_bar_region_` `@0xc1df0` | per-device + mmap | `0xC0204E70` / 112 | `neuron_ioctl_resource_mmap_info` (32) | HIGH |
| `ndl_memory_alloc` `@0xc2820` | handle-cached | `0x80384E66` / 102 | `neuron_ioctl_mem_alloc_v2_mem_type64` (56) | HIGH |
| `ndl_memory_buf_copy` `@0xc24d0` | handle-cached | `0xC0284E18` / 24 | `neuron_ioctl_mem_buf_copy64` (40) | HIGH |
| `ndl_memset` `@0xc2d00` | handle-cached | `0x80204E1C` / 28 | `neuron_ioctl_memset64` (32) | HIGH |
| `ndl_pod_status` `@0xc51b0` | misc (`O_WRONLY`) | `0xC1144E7A` / 122 | `neuron_ioctl_pod_status_v2` (276) | HIGH |
| `ndl_printk` `@0xc4ca0` | misc (`O_WRONLY`) | `0x40104E71` / 113 | `neuron_ioctl_printk` (16) | HIGH |
| `ndl_get_va_placement` `@0xc5af0` | misc (`O_WRONLY`) | `0x40104E83` / 131 | `neuron_ioctl_get_va_placement` (16) | HIGH |
| `ndl_get_compatible_version` `@0xc2770` | misc (`O_WRONLY`) | `0x40084E5D` / 93 | `neuron_ioctl_compatible_version` (8) | HIGH |
| `ndl_memory_buf_batch_copy` `@0xc5a30` | handle-cached | `0xC0204E81` / 129 | inline 32-byte batch arg | HIGH |

The slice shows the three arg-shape families: small fixed in/out structs (`host_device_id`, `compatible_version`), size-overloaded `/64` siblings (`mem_alloc`, `buf_copy`, `memset` — the shim picks the 64-bit width and falls back to a `_cm` variant on `-EINVAL` from an older kernel), and mmap-pairing info commands (`resource_mmap_info` feeds an `mmap(2)`).

> **CORRECTION (NDL-01) —** an earlier shorthand labeled `ndl_program_engine_cm`'s command "NR 27 `PROGRAM_ENGINE`". The decoded `nr` is `0x69 = 105`, i.e. `PROGRAM_ENGINE_NC` (`0xC0084E69`), not the legacy NR-27 `PROGRAM_ENGINE`. The kernel exposes both; the shim issues the `_NC` form. Confidence HIGH (disasm-decoded).

> **NOTE — version-lag.** The shim's own source package is **`KaenaDriverLib 2.27.4.0`** (DWARF `comp_dir`/`addr2line` on every `ndl.c` address), while the runtime it ships inside is **`aws-neuronx-runtime-lib 2.31.24.0`** (`libnrt.so.2.31.24.0`). The lag is real, not a packaging error: the NDL is versioned with the *kernel driver* it speaks to, so `2.27.4.0` is the kernel-ABI baseline the wrappers encode. This is why the `_cm` ("compat-mode") fallbacks exist — a `2.31` runtime can still drive an older `2.27`-class kernel by latching a legacy 32-bit `ioctl` variant when the modern one returns `-EINVAL`. A reimplementer pinning the command set must pin it to the *NDL* package version, not the runtime version stamped on the SONAME.

---

## NDL Infrastructure Functions

| Function | Addr | Purpose | Confidence |
|---|---|---|---|
| `ndl_device_ioctl` | `0xc1d20` | per-device gateway (open `O_RDONLY` + CAS cache + `ioctl`) | HIGH |
| `ndl_misc_ioctl` | `0xc2220` | device-global gateway (lazy single `O_WRONLY` fd + `atexit` close) | HIGH |
| `ndl_mmap_bar_region_` | `0xc1df0` | `RESOURCE_MMAP_INFO` `ioctl` + `mmap(2)` of a CSR/HBM window | HIGH |
| `ndl_available_devices` | `0xc2180` | enumerate present `/dev/neuronN` via `access(F_OK)` | HIGH |
| `ndl_device_cached_fd_close_all` | `0xc4c60` | bulk-close the fd cache at `nrt_close` | HIGH |
| `close_misc_devnode` | `0xc19f0` | `atexit` closer for `fd_misc_devnode` | HIGH |
| `ndl_get_version` | `0xc20c0` | read `/sys/module/neuron/version` (sysfs, not an `ioctl`) | HIGH |
| `ndl_register_soft_incompat_callback` | `0xc20a0` | install the soft-incompat reporter (default = no-op dummy) | HIGH |

## Related Components

| Component | Relationship |
|---|---|
| `ndl_open_device` (`@0xc30f0`) | brings up a device fd (`O_RDWR`) and the `ndl_device_t` handle that the handle-cached path reuses |
| `ndl_dal` (`@0xc5d780`) | the compat-mode fn-ptr table the wrappers latch a `_cm` fallback into on kernel-ABI mismatch |
| `tdrv_*` / `dmem_*` | the consumers above the shim: device bring-up and the memory layer that issue `ndl_*` |
| `ncdev_ioctl` (kernel) | the dispatcher every `ndl_*` `ioctl` terminates at, on `/dev/neuronN` |

## Cross-References

- [IOCTL Catalog](../kernel/ioctl-catalog.md) — the full per-command table this page links rather than duplicates: every `nr`, direction, arg struct, kernel handler, `ndl_*` caller, and gate tier
- [IOCTL Dispatch and the Privilege-Gate Model](../kernel/ioctl-dispatch.md) — the kernel side: the `O_WRONLY` free-access split that the misc gateway targets, the attach sub-gate the per-device gateway faces, and the `_IOC` overload-resolution discipline
- [Char Device, fops and mmap](../kernel/cdev-mmap.md) — the `/dev/neuronN` char device the gateways open; the `pgoff-is-the-physical-address` cookie protocol that backs `ndl_mmap_bar_region_`
- [TDRV: Device Bring-Up and Lifecycle](tdrv-lifecycle.md) — the primary consumer of the NDL: `tdrv_init` drives `ndl_open_device`, the HBM scrub, and the pod commands through these gateways
