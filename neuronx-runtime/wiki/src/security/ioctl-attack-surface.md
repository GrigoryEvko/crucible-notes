# The IOCTL Attack Surface (14 Findings)

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The source is read directly, not reverse-engineered; every finding was re-read verbatim against the shipped `.c`/`.h` before it was written here. Principal files: `neuron_cdev.c` (4043 lines), `neuron_core.c`, `neuron_crwl.c`, `neuron_mmap.c`, `neuron_dmabuf.c`, `neuron_p2p.c`, `neuron_fw_io.c`, `neuron_pid.c`, `neuron_ioctl.h`, and the per-arch DHAL (`v2/`, `v3/`). Other driver versions renumber lines and add/remove commands.*
> *Frame: **authorized white-hat / defensive**. This page documents weaknesses in order to fix them. Every finding is calibrated to its **true** reachability — firmware-facing items are labelled TCB-internal, not userspace-reachable — and carries a concrete, ABI-aware fix. No exploit code, no weaponization. Part — Security & Attack Surface · [back to index](../index.md)*

## Abstract

This is the consolidated hardening evidence for the Neuron driver's `ioctl` surface. It builds directly on the [three-tier gate model](../kernel/ioctl-dispatch.md): Gate 0 is the `/dev/neuronN` file permission (default `root:neuron 0660`), Gate 1 is the `O_WRONLY` "free-access" split that routes to an **ungated** misc lane (`ncdev_misc_ioctl`, `neuron_cdev.c:3147`), and Gate 2 is the `npid_is_attached` "attach" sub-gate (`:3210-3225`) that protects an enumerated set of `DEVICE_INIT`-owner commands. There is **no Linux capability layer** on any `ioctl` path — the sole `capable()` in the entire tree is `CAP_SYS_RAWIO` in an `mmap` path (`neuron_mmap.c:362`, verified by whole-tree grep). Every finding therefore presumes the attacker already holds an fd to `/dev/neuronN`; the threat model is a **local privilege boundary** — a process inside the `neuron` group, a container that bind-mounts the device, or any local user on a node that mis-sets the device permissions — not a remote one.

The honest net assessment is that **no single finding is a clean unauthenticated remote RCE**. The driver leans entirely on file permissions plus a per-device 16-slot PID table, and the highest-value hardening targets are the design choice itself (S4) plus concrete out-of-bounds bugs on lanes that need no `DEVICE_INIT` (S1, S2) and gate-bypass / ungated state mutation (S3, S5). The five MED findings are all `CONF HIGH` and source-anchored to a line and a mechanic. Below them sit four LOW findings (a dma-buf teardown race, an off-by-one MMIO poke, a notify-amplification DoS, a latent uninitialized `bar_pa`) and five INFO items — three firmware-TCB observations, one benign `kmalloc` multiply, and one **reversed** seed claim (the P2P "error-as-PA" was self-mitigated by an alignment guard; see the CORRECTION in §13).

Severities here are **not inflated**: an out-of-bounds *read* that steers a 1-bit return is documented as a weak info-leak / DoS, not as code execution; an MMIO write whose exact landing depends on arch/HW geometry is documented with that uncertainty stated. The reimplementation value of this page is the gate-vs-mechanic mapping: for each finding it names which lane reaches it, the precise line where the check is missing or wrong, and the cheap fix that closes it.

For the threat model, the contract is:

- **The reachability column is the first-class fact** — every finding states which fd mode (`O_WRONLY` misc vs `O_RDONLY`/`O_RDWR` full) and which gate (none / sub-gate / free-access-ungated / firmware-TCB / not-reachable) admits it. A fix is only meaningful relative to its lane.
- **Each finding is anchored to a single faulty line and a single concrete mechanic** — the integer wrap, the missing bound, the exact-cmd-vs-`_IOC_NR` mismatch, the `pr_warn`-and-free, the `>` that should be `>=`.
- **Every fix is ABI-aware** — the P0 fixes add a bound or change a comparator with no struct or command-numbering change; the one design fix (S4) is explicitly opt-in (module param, default off) so existing `neuron`-group deployments do not break.
- **TCB vs userspace is kept distinct** — firmware already owns the device, so S10/S11 are integrity/robustness notes inside the trust boundary, not userspace-reachable corruption.

| | |
|---|---|
| **Surface** | `ncdev_ioctl` (`neuron_cdev.c:3188`) + `ncdev_misc_ioctl` (`:3147`) + `ncdev_mmap` |
| **Gate model** | Gate 0 file-perm · Gate 1 `O_WRONLY` free-access split (`:3206`) · Gate 2 `npid_is_attached` (`:3210-3225`) |
| **Capability layer** | **none** on `ioctl`; sole `capable(CAP_SYS_RAWIO)` is `neuron_mmap.c:362` (mmap) |
| **Live MED findings** | S1 (PRINTK OOB read), S2 (`EVENT_*` `nc_id`), S3 (`*64` gate slip), S4 (no cap), S5 (ungated CRWL) |
| **Reversed seed claim** | S13 — P2P "error-as-PA" is self-mitigated by an alignment guard (`neuron_p2p.c:93-97`) → INFO |
| **Threat model** | local: `neuron`-group / container / mis-permed node — **not** remote; presumes an open device fd |
| **Verification** | every line re-read verbatim against `aws-neuronx-dkms 2.27.4.0` GPL source |

---

## 1. At a Glance — The Gate Lanes That Make Findings Reachable

The findings partition cleanly by the lane that reaches them. The [dispatch page](../kernel/ioctl-dispatch.md) owns the full gate model; this table is the security-relevant projection — which lane, what it skips, and which findings ride it.

| Lane | Entry | Gates skipped | Findings on this lane |
|---|---|---|---|
| `O_WRONLY` misc (free-access) | `ncdev_misc_ioctl` `:3147` | attach + capability (all) | S1 (PRINTK), S5/S8 (CRWL mark/unmark), S6 (DMABUF_FD) |
| `O_RDONLY`/`O_RDWR` full, none-gate | main switch `:3227-3384` | attach (cmd not in list) + capability | S2/S7 (`EVENT_*`), S4 (`BAR_READ`, `PROGRAM_ENGINE*`), S12 (`DUMP_MEM_CHUNKS`) |
| `O_RDONLY`/`O_RDWR` full, sub-gate slip | `:3294`/`:3296`/`:3271` by `_IOC_NR` | attach (only for the `*64` width) | S3 (`MEM_COPY64`/`ASYNC64`/`DESC64`) |
| `mmap` | `ncdev_mmap` `:437` | — (S9 not reachable on shipped tables) | S9 (uninit `bar_pa`, latent) |
| in-kernel GPL peer | `EXPORT_SYMBOL_GPL` `neuron_p2p.c:133` | n/a (not userspace) | S13 (self-mitigated) |
| firmware / MiscRAM | `fw_io_execute_request[_new]` | n/a (TCB) | S10, S11 |

---

## 2. The Findings Table

The full roster, severity-ranked, calibrated to reachability as shipped on LP64 x86-64 / arm64. `Reachability` names the lane and gate; `Mechanic` names the faulty line; `Confidence` is `HIGH` where the source body was re-read verbatim, lower where it depends on out-of-tree (arch/HW/firmware) behaviour.

| ID | Sev | Reachability (lane / gate) | Mechanic (file:line) | Fix | Conf |
|---|---|---|---|---|---|
| **S1** | MED | `O_WRONLY` misc, **none** | `arg.size==0` ⇒ `str[arg.size-1]` = `str[0xFFFFFFFF]` OOB stack read (`neuron_cdev.c:2349`); only upper bound checked (`:2342`) | add `arg.size==0` lower bound | HIGH |
| **S2** | MED | `O_RDONLY` full, **none** | `ncdev_events_ioctl` omits `ncdev_ncid_valid` (`:1457-1476`); unbounded `nc_id` scales a BAR0 MMIO offset (`v2:393-398`) → `writel` (`neuron_core.c:156`) | call `ncdev_ncid_valid` (mirror `:1437`) | HIGH |
| **S3** | MED | `O_RDONLY` full, **sub-gate slip** | sub-gate tests exact cmd (`:3212/3214/3215`); dispatch is `_IOC_NR` (`:3271/3294/3296`) ⇒ `*64` width skips `npid_is_attached` | make sub-gate tests `_IOC_NR`-based | HIGH |
| **S4** | MED | any device fd, **file-perm only** | zero capability checks on `ioctl`; sole `capable()` is `neuron_mmap.c:362`; auth is identity-only `npid_is_attached` (`neuron_pid.c:60`) | opt-in cap gate (module param) | HIGH |
| **S5** | MED | `O_WRONLY` misc, **none** | CRWL mark/unmark (`:3148-3151`) mutate the global `ncrwl_range_pids[]` allocator (`neuron_crwl.c:185`) with no attach gate | route behind `npid_is_attached`; cap per-pid | HIGH |
| **S6** | LOW | importer + `munmap` race | `nmmap_remove_node_rbtree` only `pr_warn`s on `dmabuf_ref_cnt>0` then frees (`neuron_mmap.c:118-128`, `:185`/`:225`) while a peer holds the sg | defer/​block free while `ref_cnt>0` | HIGH |
| **S7** | LOW | `O_RDONLY` full, **none** | `nc_event_get/set` use `event_index > event_count` (`neuron_core.c:130/147`); semaphores correctly use `>=` ⇒ +1-slot 4-byte MMIO OOB | change `>` to `>=` | HIGH |
| **S8** | LOW | `O_WRONLY` misc (V3) | `npe_notify_mark` called **inside** the 512-iter unmark loop (`neuron_crwl.c:242`); V3 sink resets pod mode (`v3/neuron_pelect.c:1244-1246`) | hoist the notify out of the loop | HIGH |
| **S9** | LOW | `mmap` (not reachable today) | `nmap_dm_special` sets `bar_pa` only for `bar_num` 0/4, **no else**, then uses it (`neuron_mmap.c:374,392-398`) | add `else { return -EINVAL; }` | HIGH (bug) |
| **S10** | INFO | firmware (TCB) | NEW-path response reads only `dw0`; `crc32` (`dw1`) never validated (`neuron_fw_io.c:469-496`, hdr `:45/49`) | recompute + verify response CRC | firmware |
| **S11** | INFO | firmware (TCB) | legacy `size - sizeof(resp)` could underflow but the `>resp_size` guard rejects it (`neuron_fw_io.c:376-383`) | compute length once, explicit reject | firmware |
| **S12** | INFO | `O_RDONLY` full, **none** | `kmalloc(num_entries_in * 24)` (`neuron_cdev.c:2402`); u32×size_t ⇒ no 64-bit wrap on LP64 | cap `num_entries_in` | non-exploit |
| **S13** | INFO | in-kernel EFA peer | seed "error-as-PA" — **self-mitigated** by the page-align guard (`neuron_p2p.c:93-97`); see CORRECTION §13 | PA via out-param + int error | self-mitig |
| **S14** | INFO | n/a (latent foot-gun) | `IS_NEURON_DEVICE_FREE_ACCESS` uses `(f_flags & O_WRONLY) == 1` (`neuron_cdev.c:52`) | use `(f_flags & O_ACCMODE) == O_WRONLY` | latent |

---

## 3. S1 — PRINTK Size-Underflow OOB Stack Read

> **GOTCHA —** the PRINTK handler bounds `arg.size` from *above* but never from *below*. With `arg.size == 0`, the trailing NUL-check `str[arg.size - 1]` indexes `str[0xFFFFFFFF]` — a wild kernel-stack read at a fixed +4 GB offset — on the ungated `O_WRONLY` misc lane that needs no attach, no `DEVICE_INIT`, and no capability. A reimplementation that copies the "upper-bound only" check inherits the bug.

### Reachability

Any process that can `open("/dev/neuronN", O_WRONLY)`. `O_WRONLY` makes `IS_NEURON_DEVICE_FREE_ACCESS` true (`neuron_cdev.c:52`, applied `:3206`), routing to `ncdev_misc_ioctl`, whose PRINTK arm (`:3165-3166`) calls `ncdev_printk` with **no** attach, `DEVICE_INIT`, or capability gate. PRINTK is `NR 113`, `_IOW` (`neuron_ioctl.h:827`); the input struct is `neuron_ioctl_printk { void *buffer; __u32 size; __u32 action; }` (`:519-523`) — `size` is `__u32`.

### Mechanic

```c
function ncdev_printk(param):                                  // neuron_cdev.c:2333
    char str[512]                                              // :2336
    neuron_copy_from_user(&arg, param, sizeof(arg))            // :2338
    if arg.size > sizeof(str): return -EFAULT                 // :2342  UPPER bound only
    neuron_copy_from_user(str, arg.buffer, arg.size)          // :2345  size==0 ⇒ copy nothing, str UNINIT
    if str[arg.size - 1] != 0: return -EBADMSG                // :2349  size==0 ⇒ str[0xFFFFFFFF]
    printk("%s\n", str)                                       // :2352
```

`arg.size` is `__u32`. When `arg.size == 0`: `neuron_copy_from_user(..., 0)` wraps `copy_from_user(.,.,0)`, which returns `0` (success) and copies nothing — so `str[]` is **uninitialized kernel stack** (the wrapper is a thin `copy_from_user` + `pr_err`, `:87-93`). Then `arg.size - 1` evaluates to `0xFFFFFFFF` (u32 wrap), and `str` is `char[512]`, so `str[0xFFFFFFFF]` dereferences `str + 4294967295` — an out-of-bounds read whose byte is compared to `0`. There is no `arg.size != 0` guard; the `> sizeof(str)` test at `:2342` does not exclude `0`.

### Impact

A single out-of-bounds kernel **read** at a fixed +4 GB offset from a stack buffer. On most layouts that address is unmapped ⇒ a kernel OOPS (local DoS). If it happens to be mapped, the only observable is the `!= 0` branch — a 1-bit oracle, a weak info-channel. Not a write, not a control-flow hijack. Class: info-leak (weak) / DoS.

### Mitigation

Single-byte read only; result steers only a `-EBADMSG` return (the read byte is not exfiltrated beyond the equality test); reachable only by a holder of an `O_WRONLY` fd (Gate 0).

### Fix

Add a lower bound before the index (P0, no ABI change):

```c
if (arg.size == 0 || arg.size > sizeof(str)) return -EFAULT;
// prefer arg.size >= sizeof(str) to keep room for the NUL, and force str[arg.size-1] = 0 after the copy
```

---

## 4. S2 — EVENTS IOCTL: Unvalidated `nc_id` → OOB MMIO Address

> **GOTCHA —** the semaphore handler validates `nc_id` with `ncdev_ncid_valid`; the events handler, a near-twin a few lines below it, does **not**. The unchecked `nc_id` is multiplied into a raw BAR0 MMIO offset and fed to `writel`. The two handlers diverge in exactly the check that matters — an asymmetry the dispatcher cannot catch.

### Reachability

A non-free-access (`O_RDONLY`/`O_RDWR`) fd. `EVENT_GET` (`NR 46`, `neuron_ioctl.h:743`) and `EVENT_SET` (`NR 45`, `:742`) dispatch at `neuron_cdev.c:3321/3323` with **gate = none** — they are not in the attach sub-gate list (`:3210-3219`) and not free-access. So no `npid_is_attached` is required. Input: `struct neuron_ioctl_event { nc_id, event_index, value }` copied at `:1462`.

### Mechanic

`ncdev_events_ioctl` (`:1457-1476`) — unlike `ncdev_semaphore_ioctl`, which calls `ncdev_ncid_valid(arg.nc_id)` at `:1437` — forwards `nc_id` straight to `nc_event_get/set` (`:1468/1473`):

```c
function ncdev_events_ioctl(nd, cmd, param):                   // neuron_cdev.c:1457
    neuron_copy_from_user(&arg, param, sizeof(arg))            // :1462
    // NO ncdev_ncid_valid(arg.nc_id) — present in semaphore handler at :1437
    if cmd == EVENT_GET: nc_event_get(nd, arg.nc_id, arg.event_index, &arg.value)   // :1468
    if cmd == EVENT_SET: return nc_event_set(nd, arg.nc_id, arg.event_index, arg.value)  // :1473

function nc_get_event_addr_v2(nd, nc_id, event_index, &ev_addr):   // v2/neuron_dhal_v2.c:393
    base = bar0 + V2_PCIE_BAR0_TPB_0_OFFSET
                + (V2_PCIE_BAR0_TPB_0_SIZE * nc_id)           // :396  nc_id scales the BAR0 offset
                + mmap_nc_event_offset
    *ev_addr = base + (event_index * NC_EVENT_SIZE)           // :397  no bounds re-check

// nc_event_set then: writel(value, addr)                     // neuron_core.c:156
```

A large `nc_id` scales the BAR0 offset by `V2_PCIE_BAR0_TPB_0_SIZE`, producing an MMIO address far outside the intended per-NC window. `ncdev_ncid_valid` (`:95-101`) would have rejected `nc_id >= nc_per_device` with `-E2BIG`.

### Impact

An attacker-controlled-offset 32-bit MMIO **write** (`EVENT_SET`) or read (`EVENT_GET`) into BAR0 register space. Within BAR0 this is an arbitrary device-register poke ⇒ device-state corruption / DoS; past the mapped BAR it faults. Class: memory-corruption (MMIO), device-state.

### Mitigation

`nc_id` is `u8` in the wrapper signature (`nc_event_*(... u8 nc_id ...)`, `neuron_core.c:125/142`), so it is bounded `0..255` — the scaled offset is bounded but can still exceed `nc_per_device` (typically 1–8). The target is BAR0 MMIO, not host RAM, so this is device-register tampering, not kernel-heap corruption. (`CONF MED` on exact MMIO landing — it depends on `V2_PCIE_BAR0_TPB_0_SIZE` × `bar0_size`, which is arch/HW-specific.)

### Fix

Add, immediately after the `copy_from_user`, the same check the semaphore path already runs:

```c
ret = ncdev_ncid_valid(arg.nc_id); if (ret) return ret;     // mirror neuron_cdev.c:1437
```

Independently, `nc_get_event_addr_v{2,3}` should clamp `event_index`/`nc_id` defensively.

> **CORRECTION (event-index `__u32`→`u16` truncation) — HIGH GOTCHA —** this page previously called out only the `nc_id` narrowing on the NQ-engine boundary; there is a *second* silent narrowing on the very same handler. `struct neuron_ioctl_event.event_index` is `__u32` (`neuron_ioctl.h:341`), but `ncdev_events_ioctl` (`neuron_cdev.c:1457-1476`) passes it straight into `nc_event_get`/`nc_event_set`, whose `event_index` parameter is `u16` (`neuron_core.h:70,82`). The upper 16 bits are dropped at the call (`:1468`/`:1473`) with no error — a userspace `event_index` of `0x0001_0000` aliases index `0`, and any value `≥ 0x1_0000` wraps mod 65536. So an attacker cannot reach indices beyond `0..0xFFFF`, but the truncation is *silent* (no `-EINVAL`), which both masks out-of-range inputs and lets a high `event_index` collide with a low one — a correctness hazard for any reimplementer who widens the wrapper to `u32` expecting the old aliasing, or who relies on the driver to reject oversized indices. Pair the `ncdev_ncid_valid` fix above with an explicit `event_index <= U16_MAX` (or per-arch event-count) range check before the call.

---

## 5. S3 — `*64` Size-Overload Skips the Attach Sub-Gate

This is the security projection of the dispatch page's headline correctness finding; the gate-vs-dispatch mismatch is established there ([the `*64` sub-gate slip](../kernel/ioctl-dispatch.md#the-64-sub-gate-slip)). Here it is the reachability and blast-radius analysis.

### Reachability

A **non-free-access** (`O_RDONLY`/`O_RDWR`) fd that has opened `/dev/neuronN` but is **not** the `DEVICE_INIT`/attached owner. Variants: `MEM_COPY64` (`NR 23`, `neuron_ioctl.h:687`), `MEM_COPY_ASYNC64`, `DMA_COPY_DESCRIPTORS64` (`NR 38`, `:728`). It is **not** a free-access bypass: `O_WRONLY` short-circuits to `ncdev_misc_ioctl`, which has no `MEM_COPY*` arm at all.

### Mechanic

The attach sub-gate (`neuron_cdev.c:3210-3219`) enumerates by **exact full cmd**: `cmd == NEURON_IOCTL_DMA_COPY_DESCRIPTORS` (`:3212`), `cmd == NEURON_IOCTL_MEM_COPY` (`:3214`), `cmd == NEURON_IOCTL_MEM_COPY_ASYNC` (`:3215`). But the dispatch matches by `_IOC_NR(cmd)`:

```c
// gate: exact full cmd (size included)
if (... cmd == NEURON_IOCTL_MEM_COPY ...) { if (!npid_is_attached(nd)) return -EACCES; }   // :3214,:3220-3224
// dispatch: by NR — true for BOTH the 32-bit base AND the *64 sibling
else if (_IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_MEM_COPY))        return ncdev_mem_copy(...);        // :3294
else if (_IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_MEM_COPY_ASYNC))  return ncdev_mem_copy_async(...);  // :3296
else if (_IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_DMA_COPY_DESCRIPTORS)) ...                           // :3271
```

`MEM_COPY` (`NR 23`, size 28) and `MEM_COPY64` (`NR 23`, larger size) share `nr`, differ only in `_IOC_SIZE`. For the `*64` cmd, `cmd == NEURON_IOCTL_MEM_COPY` is **false** (size differs) ⇒ the `npid_is_attached` block is skipped, yet `ncdev_mem_copy`/`async`/`desc` still runs.

### Impact

A process that opened the device but never became the `DEVICE_INIT` owner can drive `MEM_COPY64` / `ASYNC64` / `DESC64` against memory handles. Class: privilege (intra-device sub-gate bypass) — it lets a non-owner-attached process issue DMA-class ops the owner-gate was meant to reserve.

### Mitigation

Handle ownership bounds the blast radius: `ncdev_mem_handle_to_mem_chunk` resolves handles only from *this* `nd`'s mpset (a non-owner cannot name another process's handles unless it can guess/forge them), and `mc_access_is_within_bounds` clamps `offset+size` to the chunk. Still requires an `O_RDONLY` device fd. The effect is "skip the `DEVICE_INIT`-owner check", not "arbitrary kernel write".

### Fix

Make the three sub-gate tests `_IOC_NR`-based to cover both widths, or add the explicit `*64` constants to the enumeration at `:3210-3219`:

```c
_IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_MEM_COPY) || _IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_MEM_COPY_ASYNC)
|| _IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_DMA_COPY_DESCRIPTORS)
```

---

## 6. S4 — No Capability Gate on Raw MMIO / DMA / Engine IOCTLs

This is the design root cause that makes S1/S2/S3/S5 reachable by merely holding the device. It is documented as MED because the mitigating range-checks (below) are real, but the absence of a privilege layer is the structural weakness a hardened deployment must compensate for out-of-driver.

### Reachability

Any holder of a `/dev/neuronN` fd. High-power commands reachable with **zero** capability check: `BAR_READ` (`NR 11`, none-gate), `BAR_WRITE` (`NR 12`, npid-gate only), `PROGRAM_ENGINE`/`_NC` (`NR 27/105`, none-gate), the DMA family, `HBM_SCRUB`, `POD_CTRL`.

### Mechanic

A whole-tree grep for `capable(` / `CAP_` / `ns_capable` returns exactly **one** hit:

```c
static int nmmap_dm_root(struct neuron_device *nd, struct vm_area_struct *vma) {  // neuron_mmap.c:360
    if (!capable(CAP_SYS_RAWIO)) return -EPERM;                                   // :362  the ONLY capable()
    return nmmap_dm(nd, vma, NULL);
}
```

Every `ioctl` authorization is `npid_is_attached` (`neuron_pid.c:60`) — `attached_processes[slot].open_count`, i.e. "did *this* tgid open + `DEVICE_INIT` this `nd`". It is an identity check with **no** privilege semantics: no uid, caps, or namespaces. There is no `CAP_SYS_ADMIN`/`CAP_SYS_RAWIO` on `BAR_WRITE` (raw MMIO `writel`, `neuron_cdev.c:1572`) or `PROGRAM_ENGINE` (engine DMA).

### Impact

The security boundary is entirely the device-node permission plus the PID table. Any process granted the device — the `neuron` group, or a container that bind-mounts `/dev/neuronN` — gets raw MMIO and DMA-class access with no further kernel privilege gate. Class: privilege (over-broad grant).

### Mitigation

BAR access **is** range-checked: `ncdev_bar_read`/`write_data` bound each `reg_addresses[i]` to `[bar0, bar0+bar0_size)` (`neuron_cdev.c:1497`, `:1566`), BAR0 writes additionally consult `ndma_is_bar0_write_blocked(off)` (`:1569`), and BAR4 r/w is disabled (`-EINVAL`, `:1522`/`:1582`). So "raw MMIO" is BAR0-within-blocklist, not truly arbitrary. Default perms are `root:neuron 0660`. These are *bounds* checks, not *authorization* checks.

### Fix

Defense-in-depth, ABI-compatible: add an **opt-in** capability gate (e.g. `CAP_SYS_RAWIO` for `BAR_*`, `PROGRAM_ENGINE*`, raw DMA) controlled by a module param defaulting **off**, so existing `neuron`-group deployments are not broken; document the file-permission trust model explicitly; and recommend per-tenant device isolation (one `/dev/neuronN` per container) over shared group access.

---

## 7. S5 — CRWL NC-Range Mark/Unmark on the Ungated O_WRONLY Lane

> **GOTCHA —** `CRWL_NC_RANGE_MARK`/`UNMARK` are *state-changing* commands on the `O_WRONLY` free-access lane with no attach gate. They mutate a single static array shared across **all** devices. An `O_WRONLY` opener who never attached can participate in the cooperative NeuronCore reservation allocator and exhaust it.

### Reachability

The `O_WRONLY` free-access misc lane. `CRWL_NC_RANGE_MARK`/`_EXT0` (`NR 85`, `neuron_ioctl.h:766`) dispatch at `neuron_cdev.c:3148-3149`; `CRWL_NC_RANGE_UNMARK`/`_EXT0` (`NR 86`, `:768`) at `:3150-3151`. No attach, no `DEVICE_INIT`, no capability.

### Mechanic

`ncdev_crwl_nc_range_mark` (`:2163`) takes attacker-controlled `arg.nc_count` / `arg.start_nc_index` / `arg.end_nc_index`, adds a per-device offset, and calls the device-spanning allocator `ncrwl_nc_range_mark` (`neuron_crwl.c:188`), which writes `ncrwl_range_pids[]` — a single static array shared across all devices (`:185`) — under `ncrwl_range_lock`. Unmark (`neuron_crwl.c:232`) clears the caller-owned bits.

```c
static pid_t ncrwl_range_pids[MAX_NEURON_DEVICE_COUNT * MAX_NC_PER_DEVICE] = {0};  // :185  global, 512 slots
// unmark clears only the caller's own bits:
if (test_bit(i, free_map) && ncrwl_range_pids[i] == task_tgid_nr(current)) {       // :237  pid-scoped
    ncrwl_range_pids[i] = 0; ncrwl_range_mark_cnt--;
}
```

### Impact

An unprivileged `O_WRONLY` opener participates in the cooperative reservation allocator with no attach/privilege gate. It can reserve cores (subject to first-fit availability) and free its own. It **cannot** steal another tgid's marks — unmark checks `ncrwl_range_pids[i] == current tgid` (`:237`) — but it **can** exhaust the 512-slot allocator (`ncrwl_range_mark_cnt`) and starve legitimate model-load reservations. Class: DoS (cross-tenant cooperative allocator); on V3 it carries an additional side-effect (S8).

### Mitigation

Unmark is pid-scoped (no cross-tenant free); an over-large `nc_count` simply yields `-EBUSY` (the inner loop is bounded by `j <= end && j < i+nc_count`, `neuron_crwl.c:205-209`); `start`/`end` are validated `< 512` (`:193-196`). The exposure is reservation exhaustion / churn, not memory corruption.

### Fix

Route CRWL mark/unmark through the non-free-access path with an `npid_is_attached` gate (or add an attach check inside `ncdev_crwl_nc_range_mark/unmark`), so only an attached process can mutate the global reservation map; alternatively cap per-pid reservations.

---

## 8. S6 — DMA-Buf Node-Teardown Use-After-Free Window

### Reachability

The peer/import path. (1) A user issues `DMABUF_FD` (`NR 107`, `O_WRONLY` misc lane `:3158` → `ndmabuf_get_fd`, `neuron_dmabuf.c:290`); (2) an importer (EFA/ibcore) does `attach` → `map` (`ndmabuf_map` bumps `dmabuf_ref_cnt`, `:208`) and is mid-DMA; (3) the same process `munmap`s the underlying buffer (or dies), invoking `nmmap_delete_node` / `nmmap_delete_all_nodes`.

### Mechanic

`nmmap_remove_node_rbtree` (`neuron_mmap.c:118-128`) only `pr_warn`s when `dmabuf_ref_cnt > 0` (`:123-125`), then `rb_erase`s (`:127`); the callers `nmmap_delete_node` (`:185`) and `nmmap_delete_all_nodes` (`:225`) `kfree(mmap)` **regardless** of `dmabuf_ref_cnt`.

```c
static void nmmap_remove_node_rbtree(root, mmap):              // neuron_mmap.c:118
    if (mmap->dmabuf_ref_cnt > 0)
        pr_warn("mmap node is being removed while dmabuf is in progress ...")   // :123-125  warn only
    rb_erase(&mmap->node, root)                               // :127  freed regardless of in-flight DMA
// caller then: kfree(mmap)                                   // :185 / :225
```

The peer still holds an `sg_table` whose `sg_dma_address(sg) = pa` (`neuron_dmabuf.c:202`), where `pa = mmap->pa + (private_data->va - mmap->va)` (`:189`), and the backing device memory is being torn down. `ndmabuf_map`/`unmap` re-search the node and tolerate a freed node (`:170`), but there is **no ref-count interlock** that blocks the free while a peer DMA is in flight.

### Impact

A window in which a peer device DMAs into/out of physical addresses whose driver-side tracking node has been freed and whose memory may be reallocated. Class: memory-corruption (device-DMA into freed/recycled memory) — but only in the "app terminated midway between register and deregister" race, which the authors explicitly acknowledge (comment `:120-122`). Not a deterministic primitive; depends on importer timing.

### Mitigation

Requires an external peer importer (EFA) actively mid-DMA; the `free_callback` mechanism is the intended teardown signal; `ndmabuf_detach` re-checks `npid_is_attached` before the FD-recycle (`:116`); single-attach is CAS-enforced (`__sync_bool_compare_and_swap`, `:69`). The hole is specifically the node-free-vs-in-flight-DMA ordering, not the common path.

### Fix

Make node teardown ref-count-aware: defer `rb_erase`/`kfree` (or block) while `dmabuf_ref_cnt > 0`, or force-invalidate the peer mapping (revoke sg / call the `free_callback` and wait) before `kfree`. At minimum, escalate the `pr_warn` to a refusal-to-free that defers cleanup to `ndmabuf_unmap` when `ref_cnt` drops to 0.

---

## 9. S7 — `nc_event_get/set` Off-by-One Bound Check

### Reachability

Same as S2 (`EVENT_GET`/`SET`, none-gate, `O_RDONLY` fd). The attacker sets `event_index == event_count`.

### Mechanic

`nc_event_get`/`set` (`neuron_core.c:130`/`:147`) use `if (event_index > event_count) return -EINVAL`, whereas the four semaphore paths correctly use `>=` (`neuron_core.c:55,73,92,111`). The `>` admits `event_index == event_count`, one past the valid `[0, event_count)` range; `nc_get_event_addr_v2` (`v2:397`) then computes `base + event_index*NC_EVENT_SIZE` (4 bytes) with no re-check.

```c
if (event_index > ndhal->ndhal_address_map.event_count) return -EINVAL;   // :130/:147  should be >=
// contrast — semaphore:
if (semaphore_index >= ndhal->ndhal_address_map.semaphore_count) ...      // :55,73,92,111  correct
```

### Impact

A single 4-byte MMIO read/write exactly one event-slot past the valid window. Class: memory-corruption (MMIO), tightly bounded (+4 bytes) — much smaller blast radius than the unvalidated `nc_id` of S2.

### Mitigation

Only +1 index (4 bytes) past the array; in practice still lands within the BAR0 region; bounded.

### Fix

Change `>` to `>=` at `neuron_core.c:130` and `:147` to match the semaphore checks.

---

## 10. S8 — CRWL Unmark: `npe_notify_mark` Amplification + Cross-Process Mode Reset

### Reachability

`O_WRONLY` misc lane, `CRWL_NC_RANGE_UNMARK` (`NR 86`) → `ncrwl_nc_range_unmark` (`neuron_crwl.c:232`). Only material on V3 pod hardware; on v2/STD the sink is a no-op.

### Mechanic

`npe_notify_mark(ncrwl_range_mark_cnt, false)` is called **inside** the 512-iteration loop (`:242`), once per global NC index, regardless of whether the caller owns that index:

```c
void ncrwl_nc_range_unmark(free_map):                          // neuron_crwl.c:232
    for (i = 0; i < MAX_NEURON_DEVICE_COUNT * MAX_NC_PER_DEVICE; i++) {   // 512 iterations
        if (test_bit(i, free_map) && ncrwl_range_pids[i] == task_tgid_nr(current)) { ... }   // :237
        ndhal->ndhal_npe.npe_notify_mark(ncrwl_range_mark_cnt, false)     // :242  INSIDE the loop: up to 512×
    }

// V3 sink (v3/neuron_pelect.c:1241):
void npe_notify_mark(mark_cnt, mark):
    mutex_lock(&ndhal_pelect_data.lock)
    if (!mark && mark_cnt == 0) ndhal_pelect_data.mode = NEURON_ULTRASERVER_MODE_UNSET   // :1244-1246
    mutex_unlock(&ndhal_pelect_data.lock)
```

On V3, `npe_notify_mark_v3` (`v3/neuron_dhal_v3.c:1616-1621`) forwards to `npe_notify_mark` (`v3/neuron_pelect.c:1241`), which takes `ndhal_pelect_data.lock` and, when `(!mark && mark_cnt==0)`, sets the pelect mode to `NEURON_ULTRASERVER_MODE_UNSET`. On v2/STD `npe_notify_mark_v2` is an empty body (`v2/neuron_dhal_v2.c:1210-1212`).

### Impact

Two effects: (1) up to 512 mutex lock/unlock cycles into the pod-election singleton per single unmark `ioctl` — minor CPU / lock-contention DoS amplification on the shared pelect lock; (2) functional — an `O_WRONLY` caller that owns 0 cores, issued when global `range_mark_cnt` is already 0, repeatedly hits the `mark_cnt==0` branch and resets the pod operating **mode** set by another process. Class: DoS + cross-process pelect state corruption (the mode is re-derivable, so impact is transient).

### Mitigation

Only on V3 ULTRASERVER/PDS; `npe_notify_mark` itself is cheap; the mode is recomputed by a subsequent `POD_CTRL` set; no memory corruption. On v2/STD the sink is a no-op.

### Fix

Hoist `npe_notify_mark` to a single call after the loop, and only when the caller actually cleared ≥1 bit (track a `cleared` flag). Combine with the S5 attach-gate to remove the ungated reach.

---

## 11. S9 — `nmap_dm_special` Uninitialized `bar_pa`

### Reachability

`mmap()` of an EFA "special" resource whose table entry has `bar_num` **not** in `{0, 4}`. In the shipped DHAL builders the special table only ever sets `bar_num` 0 or 4 (e.g. `v2/neuron_dhal_v2.c:31-34` via the `DM_SPECIAL_MM_ENT` macro), so this branch is **not reachable as configured** — it is a latent bug, not a live one.

### Mechanic

`nmap_dm_special` (`neuron_mmap.c:368-398`) declares `bar_pa` uninitialized (`:374`) and sets it only for `bar_num == 0` / `== 4`, with **no else**, then uses it:

```c
phys_addr_t bar_pa;                                            // :374  uninitialized
if (bar_num == 0)      bar_pa = nd->npdev.bar0_pa;             // :392-393
else if (bar_num == 4) bar_pa = nd->npdev.bar4_pa;            // :394-395   NO else
io_remap_pfn_range(vma, vma->vm_start, (offset + bar_pa) >> PAGE_SHIFT, size, vma->vm_page_prot);  // :398
```

### Impact

If a future or buggy DHAL table ever emits another `bar_num`, an uninitialized stack value would drive `io_remap_pfn_range` ⇒ mapping an arbitrary physical frame into userspace. Class: memory-corruption / info-leak (latent). Not reachable on shipped tables.

### Mitigation

The table is driver-built, not user-controlled; only 0/4 are ever emitted today.

### Fix

Add `else { pr_err(...); return -EINVAL; }` after the `bar_num` branches, or initialize `bar_pa = 0` and validate. Defensive only.

---

## 12. S10 / S11 / S12 — Firmware-TCB and Non-Exploitable Observations

These three are documented for completeness and robustness; none is a userspace-reachable corruption.

### S10 — FW-IO NEW-Path Response CRC Never Validated (INFO, firmware TCB)

The mailbox response comes from device **firmware** over MiscRAM (triggered by `SET_POWER_PROFILE`/`GET_DATA`/`SET_FEATURE` via `fw_io_execute_request_new`), not from userspace. `fw_io_execute_request_new` (`neuron_fw_io.c:469-496`) reads only `resp_header.reg.dw0` (`:471`) — the sequence number + error_code + size. The `crc32` field (`dw1` in `union fw_io_response_hdr_new`, `neuron_fw_io.h:45/49`) is **never read or checked**: the request CRC is computed and sent, but the response is trusted on `(seq, error_code)` alone. Since firmware already has full device control, this is **inside** the trust boundary — not userspace-reachable corruption. The copy length is bounded: `data_size = resp.size - sizeof(resp)` (`:480`) is clamped by `min(resp_size, data_size)` (`:482`), `resp_size` being driver-supplied. Fix (robustness): recompute and verify the response `crc32` over header+data before accepting, to detect MiscRAM corruption / silent FW faults.

### S11 — FW-IO Legacy Response Size Underflow (INFO, guarded / self-mitigated)

`neuron_fw_io.c:376` computes `(response_hdr.hdr.size - sizeof(struct fw_io_response))` where `.size` is a `u16` firmware-supplied field. If FW returns `size < sizeof(struct fw_io_response)`, the subtraction underflows to a huge `size_t` — but the guard `(...) > resp_size` (`:376`) then evaluates `huge > resp_size` ⇒ true ⇒ `goto done` (rejected). The `memcpy` at `:382-383` uses the same expression and is reached only when the guard passed (value `<= resp_size`), so the length is valid. Impact: none as written; verified self-mitigated. Fix (clarity): compute the data length once with an explicit `if (resp_size + sizeof(...) < size) reject`.

### S12 — `DUMP_MEM_CHUNKS` kmalloc Multiply (INFO, benign on LP64)

`DUMP_MEM_CHUNKS` (`NR 116`, none-gate, `O_RDONLY` fd) → `ncdev_dump_mem_chunks` (`neuron_cdev.c:2390`) does `kmalloc(arg.num_entries_in * sizeof(struct neuron_ioctl_mem_chunk_info))` (`:2402`). `num_entries_in` is `__u32` and `sizeof == 24`; `u32 * size_t` promotes to `size_t` (64-bit), so the max `0xFFFFFFFF * 24 ≈ 96 GB` has **no 64-bit wrap** — `kmalloc` returns `NULL` ⇒ `-ENOMEM` (`:2403-2406`). The `copy_to_user` is clamped by `num_entries_in >= num_entries_out` (`:2417`). Impact: none on LP64 (would overflow only on a 32-bit ILP32 build, out of scope for x86-64/arm64). Fix (defensive): cap `arg.num_entries_in` before the multiply.

---

## 13. S13 — P2P `register_va` Error-as-PA (CORRECTION: Self-Mitigated)

> **CORRECTION (P2P NOTE-1) —** the seed claimed `neuron_p2p_register_va`'s `if (!pa)` check (`neuron_p2p.c:83`) lets a `-EINVAL`-returned-as-a-PA value slip through into the page table. Re-reading the shipped source overturns this: the immediately following alignment guard `if (pa % PAGE_SIZE != 0) return -EINVAL` (`:93-97`) catches the error-as-PA case, because `-EINVAL` (`0xFFFFFFFFFFFFFFEA`) is **not** page-aligned (`% 4096 == 0xFEA`). No small errno value is page-aligned, so every error code is rejected here. This finding is downgraded from a real defect to **INFO / self-mitigated**.

### Reachability

**Not userspace.** `neuron_p2p_register_va` is `EXPORT_SYMBOL_GPL` (`neuron_p2p.c:133`), called by an in-kernel GPL peer driver (EFA). Listed for completeness.

### Mechanic

`neuron_p2p_register_and_get_pa` returns `(u64)-EINVAL` on the `offset+size > mmap->size` failure (`:44-48`), returns `0` for the not-found case (`:59`), and returns the real PA on success (`:54`). In `neuron_p2p_register_va`:

```c
pa = neuron_p2p_register_and_get_pa(...);                      // :81
if (!pa) return -EINVAL;                                       // :83-89   catches the not-found (0) case
if (pa % PAGE_SIZE != 0) return -EINVAL;                       // :93-97   catches the -EINVAL-as-PA case
```

The not-found `0` is caught by `if (!pa)`; the `-EINVAL`-as-PA is caught by the alignment guard. The type-confusion is masked.

### Impact

None as shipped. It remains a latent smell — a `u64` return cannot structurally distinguish an error from a PA, relying on the contingent fact that no small errno is page-aligned.

### Fix

Return the PA via an out-parameter and use the `int` return for error, so a future page-aligned sentinel cannot slip through.

---

## 14. S14 — `IS_NEURON_DEVICE_FREE_ACCESS` `== 1` Fragile Macro

A latent foot-gun, not a live bug. The free-access macro is:

```c
#define IS_NEURON_DEVICE_FREE_ACCESS(filep) ((filep->f_flags & O_WRONLY) == 1)   // neuron_cdev.c:52
```

With `O_RDONLY == 0`, `O_WRONLY == 1`, `O_RDWR == 2`, `(f_flags & O_WRONLY) == 1` is true **only** for an exact `O_WRONLY` open; an `O_RDWR` fd (value `2`) is **not** free-access. Correct by current intent, but the `== 1` (rather than a mask/`!= 0` test or an explicit access-mode switch on `(f_flags & O_ACCMODE)`) is a foot-gun for anyone who reasons "writable ⇒ free-access". Fix: `((filep->f_flags & O_ACCMODE) == O_WRONLY)` for clarity.

---

## Hardening Priority Summary

The prioritized, ABI-aware fixes (full recommendations on [the hardening page](hardening.md)):

| Priority | Finding | Fix | ABI impact |
|---|---|---|---|
| P0 | S1 | reject `arg.size == 0` in `ncdev_printk`; force NUL after copy | none |
| P0 | S2 | call `ncdev_ncid_valid` in `ncdev_events_ioctl` (mirror `:1437`) | none |
| P0 | S7 | change `>` to `>=` at `neuron_core.c:130,147` | none |
| P0 | S3 | make the sub-gate tests `_IOC_NR`-based (cover `*64`) | none |
| P0 | S8 | hoist `npe_notify_mark` out of the unmark loop | none |
| P1 | S5 | gate CRWL mark/unmark behind `npid_is_attached`; cap per-pid | none |
| P1 | S6 | ref-count-aware node teardown (defer free while `ref_cnt>0`) | none |
| P1 | S4 | opt-in capability gate (module param, default off) | none (opt-in) |
| P1 | S9 | add `else { return -EINVAL; }` in `nmap_dm_special` | none |
| P1 | S14 | rewrite the free-access macro as `(f_flags & O_ACCMODE) == O_WRONLY` | none |
| P2 | S10/S11/S12/S13 | firmware CRC validate · explicit length reject · cap `num_entries_in` · PA out-param | none |

---

## Cross-References

- [Overview and the Privilege-Gate Model](overview.md) — the security framing and three-tier gate model this page's findings exploit
- [Hardening Recommendations](hardening.md) — the full prioritized fix roadmap (H1–H14) keyed to these findings
- [IOCTL Dispatch and the Privilege-Gate Model](../kernel/ioctl-dispatch.md) — the gate model proper: the `O_WRONLY` free-access split, the `npid_is_attached` sub-gate, the `*64` slip (S3), and the marshalling helper that fronts S1/S2
- [FW-IO MiscRAM Mailbox Protocol](../kernel/fw-io.md) — the firmware mailbox whose NEW-path CRC (S10) and legacy size guard (S11) are analyzed here
