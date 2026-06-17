# Security Posture and the Privilege-Gate Model

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The source is read directly, not reverse-engineered; every structural claim below was re-read verbatim against the shipped `.c`/`.h`. Other driver versions renumber lines and add/remove commands.*
> *Frame: **authorized white-hat / defensive**. This page maps the driver's authorization model and trust boundaries so a hardened deployment can reason about, and close, them. No exploit code, no weaponization, no inflated severities — every weakness is calibrated to its true reachability. Part XV — Security & Attack Surface · [back to index](../index.md)*

## Abstract

This is the **map** of Part XV — the section that documents the Neuron kernel driver's attack surface from a defender's seat. It does not catalog bugs; it establishes the **authorization model and the trust boundaries** that every Part-XV finding is measured against. The driver exposes one `/dev/neuronN` char node per bound PCI accelerator, and a process drives that accelerator almost entirely through `ncdev_ioctl` (`neuron_cdev.c:3188`) plus `.mmap`. The entire userspace authorization story is three things: (a) the `/dev/neuronN` file permission (default `root:neuron` group), (b) an `O_WRONLY` "free-access" split that routes to an **ungated** misc-ioctl lane, and (c) a per-device PID "attach" table (`npid_is_attached`, `neuron_pid.c:60`) that gates an enumerated subset of `DEVICE_INIT`-owner commands. There is **no Linux capability check on any ioctl path** — the single `capable()` in the whole driver is `CAP_SYS_RAWIO` on one `mmap` path (`neuron_mmap.c:362`, confirmed by whole-tree grep).

The consequence, and the reason this page exists, is that **the security boundary is the device-node permission plus an identity table, not a privilege layer**. Every finding in this section therefore presumes the attacker already holds an fd to `/dev/neuronN`; the threat model is "a process inside the `neuron` group, a container that bind-mounts the device, or any local user on a node that mis-sets the device permissions" — a **local privilege boundary** to harden, not a remote one. The honest net assessment carried forward into [the 14 findings](ioctl-attack-surface.md) is that **no single weakness is a clean unauthenticated remote RCE**; the highest-value hardening targets are the design choice itself (no capability gate) plus a handful of concrete out-of-bounds and gate-bypass bugs on lanes that need no `DEVICE_INIT`.

The page is structured to orient the four Part-XV sub-pages. It establishes the **three-tier gate model** (Gate 0 file-perm → Gate 1 access-mode split → Gate 2 attach sub-gate) with the entry→gate→handler table; it draws the two **trust boundaries** (userspace↔kernel via ioctl, and kernel↔firmware via the FW-IO MiscRAM mailbox) and states what is and is not reachable across each; and it explains the **severity-calibration philosophy** — reachability-ranked, TCB-vs-userspace kept distinct, no inflation — that the findings page applies. The per-finding detail lives on [the attack-surface page](ioctl-attack-surface.md); this map does not duplicate it.

For the threat model, the contract is:

- **Reachability is the first-class fact.** A weakness means nothing without its lane (`O_WRONLY` misc vs `O_RDONLY`/`O_RDWR` full vs `mmap` vs in-kernel-peer vs firmware-TCB) and its gate (none / sub-gate / free-access-ungated / `CAP_SYS_RAWIO` / not-reachable). A fix is only meaningful relative to its lane.
- **The boundary is file-permission + identity, not privilege.** `npid_is_attached` answers "did *this* tgid open and `DEVICE_INIT` this `nd`" (`neuron_pid.c:60`) — an identity check with no uid, capability, or namespace semantics. Reproduce that exactly; do not assume a hidden capability gate.
- **TCB vs userspace is kept distinct.** The firmware already owns the device, so the FW-IO mailbox observations are integrity/robustness notes *inside* the trust boundary, not userspace-reachable corruption. They are not severity-ranked alongside the ioctl bugs.
- **Severities are reachability-calibrated and not inflated.** An out-of-bounds *read* that steers a one-bit return is a weak info-leak / DoS, not code execution; an MMIO write whose exact landing depends on arch/HW geometry is documented with that uncertainty stated.

| | |
|---|---|
| **Surface** | `ncdev_ioctl` (`neuron_cdev.c:3188`) + `ncdev_misc_ioctl` (`:3147`) + `ncdev_mmap` |
| **Gate 0** | `/dev/neuronN` file permission — out of driver, default `root:neuron 0660` |
| **Gate 1** | `IS_NEURON_DEVICE_FREE_ACCESS(filep)` (`:52`) → free-access split `:3206-3207` |
| **Gate 2** | `npid_is_attached(nd)` attach sub-gate `:3210-3225` (~19 cmds, else `-EACCES`) |
| **Capability layer** | **none** on any ioctl; sole `capable(CAP_SYS_RAWIO)` is `neuron_mmap.c:362` (mmap) |
| **Identity table** | `nd->attached_processes[]`, `NEURON_MAX_PROCESS_PER_DEVICE` = 16 slots (`share/neuron_driver_shared.h`) |
| **Trust boundaries** | userspace→kernel (ioctl/mmap) · kernel↔firmware (FW-IO MiscRAM mailbox) |
| **Threat model** | local: `neuron`-group / container / mis-permed node — **not** remote; presumes an open device fd |
| **Findings page** | [14 findings, reachability-ranked](ioctl-attack-surface.md) — 5 MED, 4 LOW, 5 INFO |
| **Verification** | every gate line re-read verbatim against `aws-neuronx-dkms 2.27.4.0` GPL source |

---

## 1. The Authorization Model in One Paragraph

A reimplementer who internalizes one fact about this driver should internalize this: **authorization is concentric, file-permission-anchored, and capability-free.** The kernel never asks "is this caller privileged?"; it asks "can this caller open the node?" (Gate 0, set by udev/the operator out of driver), then "did this caller open `O_WRONLY`?" (Gate 1, a routing decision), then for a fixed allow-list of mutating commands "is this caller the `DEVICE_INIT` owner of this device?" (Gate 2, an identity check). There is no fourth gate. Raw MMIO (`BAR_READ`/`BAR_WRITE`), engine programming (`PROGRAM_ENGINE`), and the DMA-descriptor family are all reachable with **zero** Linux capability requirement — the only `capable()` in the tree guards a single `mmap`-of-device-memory-root path (`neuron_mmap.c:362`). That design is the root cause that makes the concrete bugs reachable "by merely having the device," and it is the structural property the [hardening page](hardening.md) recommends compensating for out-of-driver (per-tenant device isolation) or with an opt-in module-param capability gate.

### How this differs from the GPU-driver reference frame

The closest familiar reference is a GPU device driver (`/dev/nvidiaN`, DRM render nodes). Those also lean primarily on device-node permissions, so the *anchor* — file permission as the mandatory gate — is the same. The Neuron driver's two departures are worth stating explicitly for a reader who carries GPU-driver intuition:

- **No capability tier on the command path.** Where many accelerator drivers gate raw-register or firmware-update ioctls behind `CAP_SYS_ADMIN`/`CAP_SYS_RAWIO`, Neuron gates *none* of its ioctls on capabilities — the BAR/engine/DMA commands are authorized by the attach identity table alone (`neuron_pid.c:60`), and `CAP_SYS_RAWIO` appears only on one `mmap` path (`neuron_mmap.c:362`). A reimplementer porting a GPU-driver security model must *remove* the capability assumptions, not add them.
- **An access-mode routing split, not a render/primary split.** GPU drivers split privilege by *node* (primary vs render). Neuron splits by *open flag*: an `O_WRONLY` open is confined to a 14-command misc subset (Gate 1), and that subset is fully ungated. The split is a confinement, not an escalation — but the confined lane is ungated, which is where the cooperative-allocator findings (S5/S8) live.

The mitigation philosophy on the [hardening page](hardening.md) is built on closing exactly these two deltas: an opt-in capability gate restores the first tier, and per-tenant device isolation restores node-level separation that the shared `neuron`-group model dissolves.

---

## 2. The Three-Tier Gate Model

The gate model proper — the dispatcher, the overload-resolution rules, the marshalling helper — is owned by the [IOCTL dispatch page](../kernel/ioctl-dispatch.md). This section is the **security projection**: the three gates as authorization layers, what each does and does *not* check, and where each is anchored in source.

### Gate diagram

```text
                       open("/dev/neuronN", flags)
                                 │
        ┌────────────────────────┴────────────────────────┐
   GATE 0  device-node file permission  (root:neuron 0660, out of driver)
        │   the ONLY mandatory authentication — everything below presumes an open fd
        ▼
   ncdev_ioctl(filep, cmd, param)                          neuron_cdev.c:3188
        │
        ├─ neuron_log_rec_add(.., FILE_IOCTL, cmd)          :3204  audit (not a gate)
        │
   GATE 1  ACCESS-MODE SPLIT   IS_NEURON_DEVICE_FREE_ACCESS(filep)   :52, applied :3206
        │     (f_flags & O_WRONLY) == 1
        │
        ├── O_WRONLY  ───────►  ncdev_misc_ioctl(filep,cmd,param)    :3147
        │                        14 "misc" cmds · NO attach · NO capability · UNGATED
        │                        (PRINTK, CRWL mark/unmark, DMABUF_FD, POD_*,
        │                         DEVICE_BASIC_INFO, BDF_EXT, DRIVER_INFO_GET, RID_MAP,
        │                         NC_PID_STATE_DUMP, LOGICAL_TO_PHYSICAL_NC_MAP, ...)
        │
        └── O_RDONLY / O_RDWR ─► full dispatch
                 │
            GATE 2  ATTACH SUB-GATE   :3210-3225
                 │   if (cmd ∈ ~19-cmd DEVICE_INIT-owner allow-list)
                 │       if (!npid_is_attached(nd)) return -EACCES;     neuron_pid.c:60
                 │   matched by EXACT full cmd  ⇒ *64 size-overloads slip (see findings S3)
                 │
                 ▼
            giant if/else chain on cmd / _IOC_NR     :3227-3384
                 │   cmds NOT in the allow-list run UN-attach-gated on a full fd
                 │   (EVENT_*, SEMAPHORE_*, PROGRAM_ENGINE*, BAR_READ, DMA_*_GET_STATE,
                 │    NC_RESET, HBM_SCRUB, DUMP_MEM_CHUNKS, ...)
                 ▼
            ncdev_<family>(...)            [BOUNDARY: handler cells]

   CAP LAYER — NONE on ioctls.  Sole capable() = CAP_SYS_RAWIO in nmmap_dm_root  neuron_mmap.c:362
```

### What each gate checks — and does not

| Gate | Source anchor | Checks | Does **not** check |
|---|---|---|---|
| Gate 0 — file perm | out of driver; `root:neuron 0660` default | can the caller open the node | nothing inside the driver (udev/operator policy) |
| Gate 1 — access-mode split | `:52` macro, applied `:3206-3207` | `(f_flags & O_WRONLY) == 1` → misc lane | identity, capability, attach — the misc lane is **fully ungated** |
| Gate 2 — attach sub-gate | `:3210-3225`, `npid_is_attached` `neuron_pid.c:60` | is the caller the `DEVICE_INIT` owner (tgid in the 16-slot table, `open_count>0`) | uid, Linux capabilities, namespaces; only ~19 enumerated cmds; **exact-cmd** match (misses `*64`) |
| Cap layer | `neuron_mmap.c:362` | `CAP_SYS_RAWIO` on `nmmap_dm_root` only | every ioctl path (no `capable()`/`CAP_*`/`ns_capable` anywhere on ioctl) |

> **NOTE —** Gate 1 is a *routing* decision, not an *escalation*. `O_WRONLY` does not grant more — it grants *less*: the fd is confined to the 14-command misc subset and never reaches the attach-gated mutating commands. Its security relevance is the opposite of intuition: the misc lane is ungated, so any *state-changing* command that happens to live there (e.g. CRWL mark/unmark) is reachable with no attach at all. Reason about Gate 1 as "which 14-command sandbox," not "writable ⇒ more access."

> **GOTCHA —** the free-access macro is literally `((filep->f_flags & O_WRONLY) == 1)` (`:52`). Because `O_RDONLY == 0`, `O_WRONLY == 1`, `O_RDWR == 2`, the `== 1` is true **only** for an exact `O_WRONLY` open — an `O_RDWR` fd (value `2`) is *not* free-access and takes the full path. A reimplementer who rewrites this as `(f_flags & O_WRONLY) != 0` or "writable ⇒ free-access" changes the routing for `O_RDWR` and breaks the model. (Tracked as latent finding S14.)

### The attach sub-gate is identity, not privilege

`npid_is_attached(nd)` (`neuron_pid.c:60`) returns `nd->attached_processes[slot].open_count` after locating the calling tgid in the per-device table (`NEURON_MAX_PROCESS_PER_DEVICE` = 16 slots, `share/neuron_driver_shared.h`). It encodes exactly one fact — "this tgid opened and `DEVICE_INIT`'d this device" — with **no** uid, capability, or namespace component. On a free-access (`O_WRONLY`) open the attach is never performed at all: `ncdev_open` early-returns after setting `private_data`, skipping the attach bookkeeping (`neuron_cdev.c:3400-3403`). This is why Gate 2 is a *sub-gate*: it is not a privilege check, it is "are you the process that initialized this device," and it covers only an enumerated mutating subset (the ~19 cmds at `:3210-3219`). Everything not on that list runs un-attach-gated on a full fd.

---

## 3. The Entry → Gate → Handler Table

This is the security-relevant projection of the full ioctl catalog: for each representative entry point, the fd lane, the gate that applies, the kernel handler, and the Part-XV finding (if any) that rides it. It is not the complete 96-command catalog — that is the [IOCTL catalog](../kernel/ioctl-catalog.md); this table is the subset where the *authorization decision* is interesting. Finding IDs cross-reference [the attack-surface page](ioctl-attack-surface.md).

| Entry (userspace) | FD lane | Gate | Kernel handler (`neuron_cdev.c` unless noted) | Finding |
|---|---|---|---|---|
| `PRINTK` (NR 113) | `O_WRONLY` misc `:3165` | **none** | `ncdev_printk` `:2333` | S1 (OOB read) |
| `CRWL_NC_RANGE_MARK` (NR 85) | `O_WRONLY` misc `:3148` | **none** | `ncdev_crwl_nc_range_mark` `:2163` | S5 (ungated alloc) |
| `CRWL_NC_RANGE_UNMARK` (NR 86) | `O_WRONLY` misc `:3150` | **none** | `ncdev_crwl_nc_range_unmark` `:2220` | S5, S8 (DoS/amplify) |
| `DMABUF_FD` (NR 107) | `O_WRONLY` misc `:3158` | **none** | `ncdev_get_dmabuf_fd` → `ndmabuf_get_fd` | S6 (UAF window) |
| `EVENT_SET`/`GET` (NR 45/46) | `O_RDONLY`/`O_RDWR` full | **none** (not in allow-list) | `ncdev_events_ioctl` `:1457` | S2, S7 (MMIO OOB) |
| `BAR_READ` (NR 11) | full | **none** | `ncdev_bar_rw(read)` (range-checked) | S4 (no cap) |
| `BAR_WRITE` (NR 12) | full | **attach** (Gate 2) | `ncdev_bar_rw(write)` (range-checked) | S4 (no cap) |
| `PROGRAM_ENGINE`/`_NC` (NR 27/105) | full | **none** | `ncdev_program_engine[_nc]` | S4 (no cap) |
| `MEM_COPY` (NR 23) | full | **attach** (Gate 2) | `ncdev_mem_copy` `:761` | — (gated) |
| `MEM_COPY64`/`ASYNC64`/`DESC64` | full | **attach slip** | `ncdev_mem_copy`/`async`/`desc` (via `_IOC_NR`) | S3 (gate skip) |
| `DUMP_MEM_CHUNKS` (NR 116) | full | **none** | `ncdev_dump_mem_chunks` `:2390` | S12 (benign mul) |
| `mmap` special resource | `ncdev_mmap` | none / per-path | `nmap_dm_special` `:368` | S9 (uninit `bar_pa`, latent) |
| `mmap` DM root window | `ncdev_mmap` | **`CAP_SYS_RAWIO`** | `nmmap_dm_root` `:360` | — (the one cap) |
| (internal) EFA peer `register_va` | `EXPORT_SYMBOL_GPL` | n/a (in-kernel) | `neuron_p2p_register_va` `neuron_p2p.c:62` | S13 (self-mitigated) |
| (internal) FW mailbox response | MiscRAM / firmware | TCB | `fw_io_execute_request[_new]` | S10, S11 (trust) |

> **QUIRK —** the attach sub-gate enumerates by **exact full `cmd`** (size included), but the dispatch reaches the size-overload families by `_IOC_NR`. So `MEM_COPY` is attach-gated while `MEM_COPY64` — same `nr`, larger `_IOC_SIZE` — is **not**, because `cmd == NEURON_IOCTL_MEM_COPY` is false for the `*64` width. The gate and the dispatcher disagree on what "the same command" means. This is the security-relevant edge of the dispatch page's headline correctness finding; the reachability and blast-radius analysis is finding S3.

---

## 4. Trust Boundaries

Two trust boundaries cross the driver. Mapping them — and stating what is reachable across each — is what separates a userspace-reachable bug from a firmware-TCB observation, and it is the distinction the severity calibration (§5) rests on.

```text
   ┌─────────────────────────┐        Boundary A: userspace → kernel
   │   userspace process     │        crossing = ioctl(2) / mmap(2) on /dev/neuronN
   │   (libnrt.so, NDL)      │        gate    = Gate 0/1/2 above
   └───────────┬─────────────┘        copy    = neuron_copy_from_user :87-93 (raw user VA down)
               │  ioctl / mmap
               ▼
   ┌─────────────────────────┐
   │   Neuron kernel driver  │        in-kernel GPL peer (EFA/ibcore) crosses here too:
   │   (aws-neuronx-dkms)    │        neuron_p2p_* EXPORT_SYMBOL_GPL — NOT a userspace surface
   └───────────┬─────────────┘
               │  FW-IO MiscRAM mailbox     Boundary B: kernel ↔ firmware
               ▼                            crossing = request/response over device MiscRAM
   ┌─────────────────────────┐             driver computes+sends request crc32 (neuron_fw_io.c:351)
   │   device firmware (TCB)  │             firmware already owns the device ⇒ INSIDE the TCB
   └─────────────────────────┘
```

### Boundary A — userspace → kernel (ioctl / mmap)

This is the **only attacker-facing boundary**. It is crossed by `ioctl(2)` and `mmap(2)` on `/dev/neuronN`; the dispatcher copies nothing itself and passes the raw user VA down as `void *param`, each handler marshalling its own fixed struct via `neuron_copy_from_user` (`neuron_cdev.c:87-93`). Everything an unprivileged-but-device-holding process can reach lives here, gated by the three tiers of §2. The in-kernel GPL peer path (`neuron_p2p_*`, `EXPORT_SYMBOL_GPL` `neuron_p2p.c:133`) also crosses *into* the driver, but from another kernel module (EFA), not from userspace — it is listed for completeness and is not part of the userspace attack surface.

### Boundary B — kernel ↔ firmware (FW-IO MiscRAM mailbox)

The driver talks to device firmware through the FW-IO MiscRAM mailbox (the [FW-IO protocol page](../kernel/fw-io.md) owns the wire format; the [FW-IO trust page](fw-io-trust.md) owns the security analysis). The driver computes and sends a request `crc32` (`neuron_fw_io.c:351`), but the **firmware already has full control of the device** — it drives the hardware the driver is trying to protect. Anything reachable only by a malfunctioning or compromised firmware is therefore **inside the trust boundary (TCB-internal)**, not userspace-reachable. The two FW-IO observations carried into the findings (the NEW-path response `crc32` in `dw1` is never validated, `neuron_fw_io.h:45/49`; the legacy size-underflow is self-mitigated by a guard) are integrity/robustness notes that raise the bar against silent MiscRAM corruption — they are **not** privilege-boundary crossings and are not severity-ranked alongside the ioctl bugs.

### Reachability summary

| Boundary | Crossing | Attacker-reachable from userspace? | Class of items |
|---|---|---|---|
| A — userspace→kernel | `ioctl` / `mmap` on `/dev/neuronN` | **yes** (with a device fd) | all live findings S1–S9, S12 |
| A — in-kernel peer | `neuron_p2p_*` `EXPORT_SYMBOL_GPL` | no (another kernel module) | S13 (self-mitigated) |
| B — kernel↔firmware | FW-IO MiscRAM mailbox | no (firmware is TCB) | S10, S11 (integrity/robustness) |

---

## 5. Severity-Calibration Philosophy

The findings page ranks 14 items by severity. The ranking is **reachability-calibrated and deliberately deflationary** — the credibility of a defensive map rests on *not* overstating it. Four rules govern every severity assignment.

- **Rank by the lane that reaches it, not by the worst-case primitive.** A bug on the ungated `O_WRONLY` misc lane (no attach, no capability) ranks above an equivalent bug behind the attach sub-gate, because it is reachable by strictly more callers. The reachability column is the first-class fact; the lane and gate are stated for every finding.
- **Calibrate the impact to what is actually demonstrable.** An out-of-bounds *read* whose only observable is a one-bit `!= 0` return (S1) is a weak info-leak / DoS, **not** code execution. An MMIO write whose exact landing depends on `V2_PCIE_BAR0_TPB_0_SIZE × bar0_size` (S2) is documented *with that uncertainty stated*, not promoted to "arbitrary write." Where bounds checks or alignment guards mitigate (the BAR range-checks behind S4, the alignment guard that self-mitigates S13), the mitigation is part of the rating.
- **Keep TCB distinct from userspace.** Firmware-facing items (S10/S11) are `INFO`, labelled TCB-internal — the firmware already owns the device, so they are robustness notes, not privilege escalations. They never compete with userspace-reachable findings for a higher band.
- **Mark the gaps honestly.** A finding's confidence is `HIGH` only where the source body was re-read verbatim; arch/HW/firmware-dependent behaviour is tagged `MED`/`LOW` in place, and an overturned seed claim is corrected (not silently dropped) — S13 was downgraded from a defect to self-mitigated `INFO` with an in-place `CORRECTION`.

The resulting distribution is **5 MED, 4 LOW, 5 INFO** — and the explicit, honest top-line conclusion is that **no single finding is a clean unauthenticated remote RCE**. The full reachability-ranked roster, with per-finding mechanic, mitigation, and ABI-aware fix, is [the attack-surface page](ioctl-attack-surface.md).

---

## 6. Considerations for a Hardened Deployment

The gate model bounds what the *driver* enforces; the residual exposure is a property of how the *node* is configured. Three observations matter to a defender and to a reimplementer reasoning about the deployed posture.

### Multi-tenant exposure follows Gate 0

Because the entire authorization story collapses to "can the caller open `/dev/neuronN`" plus an identity table, the practical blast radius is set by who shares the node. Two callers in the same `neuron` group, or two containers that bind-mount the same `/dev/neuronN`, are peers under this model: the attach sub-gate distinguishes *which process initialized the device*, but it confers no isolation between two attached tenants beyond handle ownership (`ncdev_mem_handle_to_mem_chunk` resolves handles only from *this* `nd`'s mpset). The cross-tenant findings (S5 CRWL reservation exhaustion, S8 pod-mode reset on V3) are exactly the cases where the shared global state — `ncrwl_range_pids[]` is a single static array spanning all devices (`neuron_crwl.c:185`) — is mutated on the ungated `O_WRONLY` lane. The clean mitigation is **per-tenant device isolation** (one `/dev/neuronN` per container), which moves the boundary back to Gate 0 where the kernel can enforce it; shared group access leans on the cooperative discipline of the misc lane, which is ungated by design.

### Observability: the audit log is present but in-kernel

Every boundary-A crossing is recorded: `neuron_log_rec_add` is called on `FILE_IOCTL` (`neuron_cdev.c:3204`), `FILE_OPEN` (`:3398`), `FILE_FLUSH` (`:3461`), and `FILE_MMAP` (`:3535`). This is a per-device in-kernel ring (the [log-ring cell](../kernel/misc.md)), not a syscall-auditd feed — a defender gets a forensic trail of *which command on which fd*, but the record carries `cmd` and the `filep`/`param` value, not the caller's uid or container identity (the driver never reads those). For external monitoring, the log is a complement to, not a replacement for, `LSM`/`auditd` policy on the device node.

### Teardown closes the free-access lane's reservations

The one place the ungated misc lane is reconciled is process exit: `ncdev_misc_flush` (`neuron_cdev.c:3442`) unmarks *all* NCs for a closing free-access fd (`memset(bitmap, 0xff)` then `ncrwl_nc_range_unmark`), and the full-fd `ncdev_flush` calls `ncrwl_release_current_process(nd)` (`:3476`) on last attach. So a tenant's CRWL reservations are released when its fd closes — the S5 exposure is *exhaustion while live*, not a permanent leak. The unknown-command default on the misc lane is a hard reject (`pr_err "invalid misc IOCTL"` + `-EINVAL`, `:3183`), and the full path falls through to the misc lane for backward compatibility on unrecognized cmds (`:3387`) — neither default widens the surface.

---

## 7. The Four Part-XV Pages

This map orients the four pages of Part XV. Each owns a distinct slice of the defensive picture; this page is the glue.

| Page | Owns | Reads from this map |
|---|---|---|
| [The IOCTL Attack Surface (14 Findings)](ioctl-attack-surface.md) | the reachability-ranked finding roster — per-finding lane, mechanic line, impact, ABI-aware fix | the gate model (§2), the entry→gate→handler table (§3), the calibration rules (§5) |
| [FW-IO Trust Boundary](fw-io-trust.md) | the kernel↔firmware mailbox security analysis (response CRC, size guards) | Boundary B (§4) — why FW-IO items are TCB-internal, not userspace-reachable |
| [Hardening Recommendations](hardening.md) | the prioritized, ABI-aware fix roadmap (H1–H14) keyed to the findings | the design root cause (no capability gate, §1) and per-lane fixes implied by §2/§3 |
| [IOCTL Dispatch and the Privilege-Gate Model](../kernel/ioctl-dispatch.md) | the dispatcher proper — overload resolution, the `*64` slip, marshalling | the gate model is the *security* projection of that page's mechanics |

---

## Cross-References

- [The IOCTL Attack Surface (14 Findings)](ioctl-attack-surface.md) — the reachability-ranked findings this map's gate model and trust boundaries calibrate
- [FW-IO Trust Boundary](fw-io-trust.md) — the kernel↔firmware boundary (Boundary B) and why its items are TCB-internal
- [Hardening Recommendations](hardening.md) — the prioritized, ABI-aware fix roadmap keyed to the findings
- [IOCTL Dispatch and the Privilege-Gate Model](../kernel/ioctl-dispatch.md) — the dispatcher, the three gates, overload resolution, and the `*64` sub-gate slip in full
- [FW-IO MiscRAM Mailbox Protocol](../kernel/fw-io.md) — the firmware mailbox wire format underlying Boundary B
- [back to index](../index.md)
