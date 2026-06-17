# Hardening Recommendations

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The source is read directly, not reverse-engineered; every fix below is anchored to the exact line it patches, re-read verbatim against the shipped `.c`/`.h`. Other driver versions renumber lines and add/remove commands.*
> *Frame: **authorized white-hat / defensive**. This page is the remediation roadmap — fixes, not exploits. Each entry is a code-shape change or annotated patch sketch keyed to a finding on [the attack-surface page](ioctl-attack-surface.md), with priority calibrated to reachability × impact and **not** inflated. No exploit code, no weaponization. Part XV — Security & Attack Surface · [back to index](../index.md)*

## Abstract

This is the **defensive remediation roadmap** for the Neuron kernel driver, derived from the reachability-calibrated [14-finding attack-surface map](ioctl-attack-surface.md). It is written for one reader: a driver maintainer who has the finding roster in hand and wants the cheapest correct fix for each, tied to the precise line that is wrong. It does not re-derive the per-finding mechanics — each entry references its finding by S-id and goes straight to the patch — and it does not pad severities: the same honest distribution the findings page carries (**5 MED, 4 LOW, 5 INFO**) governs the priorities here, so a one-bit out-of-bounds *read* gets a `P1` bound-check, not a `P0` emergency.

The roadmap separates two kinds of work. The **code fixes** (H1–H14) are almost all ABI-compatible — they add a lower bound, mirror a validation call the sibling handler already makes, change a `>` to a `>=`, replace an exact-`cmd` test with an `_IOC_NR` test, or make a teardown ref-count-aware. The one design fix (S4, the absent capability layer) is explicitly **opt-in** — a module param defaulting off — so existing `neuron`-group deployments keep working. The **deployment hardening** is out-of-driver: device-node permissions, not exposing the `O_WRONLY` free-access lane to untrusted tenants, and per-tenant device isolation that moves the boundary back to Gate 0 where the kernel can enforce it. The two halves are complementary: the code fixes close the concrete bugs; the deployment guidance compensates for the structural choice (file-permission + identity, no privilege tier) that makes the bugs reachable "by merely having the device."

Priority here is `reachability × impact`, mapped onto three bands. **P1** is the high-value, low-cost set: the concrete out-of-bounds and gate-bypass bugs reachable on lanes that need no `DEVICE_INIT` (S1, S2, S3, S7) plus the loop-amplification hoist (S8) — every one is a few lines, no ABI change. **P2** is defense-in-depth: gate the ungated CRWL allocator (S5), make dma-buf teardown ref-count-aware (S6), the opt-in capability gate (S4), and the latent-bug guards (S9, S14). **P3** is firmware-trust robustness and non-exploitable hygiene (S10–S13) — none userspace-reachable, all raising the bar against silent corruption.

For the remediation work, the contract is:

- **Every fix names the line it patches and the line it mirrors.** The strongest fixes are symmetry repairs — the events handler should call `ncdev_ncid_valid` because the semaphore handler already does (`neuron_cdev.c:1437`); the event bound should be `>=` because the four semaphore bounds already are (`neuron_core.c:55/73/92/111`). A maintainer reviews these against an existing correct site, not against prose.
- **ABI compatibility is stated per fix.** A bound-add or comparator change touches no struct and no command number; the capability gate is opt-in by module param; only the firmware-CRC and length-reject entries (P3) add a computation, never a wire change.
- **Priority is reachability × impact, honestly.** A fix on the ungated `O_WRONLY` misc lane outranks the same fix behind the attach sub-gate, because strictly more callers reach it; an out-of-bounds *read* with a one-bit observable is `P1` for cheapness, not for blast radius.
- **TCB-internal items are P3, not skipped.** Firmware already owns the device, so S10/S11 are robustness — recompute the response CRC, make the length-reject explicit — that detect silent MiscRAM faults without pretending to be privilege fixes.

| | |
|---|---|
| **Subject** | the prioritized fix roadmap for the [14 findings](ioctl-attack-surface.md) |
| **Code fixes** | H1–H14, keyed to S1–S14; all ABI-compatible except S4 (opt-in module param) |
| **Priority bands** | P1 (cheap concrete OOB / gate-bypass) · P2 (defense-in-depth) · P3 (firmware-trust / hygiene) |
| **Severity distribution** | inherited from findings: **5 MED, 4 LOW, 5 INFO** — not inflated |
| **Highest-value targets** | S1/S2 (OOB on `none`-gate lanes), S3 (`*64` gate slip), S5 (ungated allocator), S4 (design) |
| **Deployment hardening** | Gate 0 device-node perms · do not expose the `O_WRONLY` free-access lane · per-tenant isolation |
| **Standing constraint** | the boundary is file-permission + identity, **not** a privilege tier (`neuron_pid.c:60`) |
| **Verification** | every patched line re-read verbatim against `aws-neuronx-dkms 2.27.4.0` GPL source |

---

## 1. At a Glance — How the Fixes Partition

The fixes partition by *what kind of change closes them*, which is the axis a maintainer plans around. Four shapes cover all fourteen: a missing **bound** (add a check that should have been there), a missing **symmetry** (call the validation the twin handler already calls / use the comparator the twin already uses), a **dispatch/gate alignment** (make the gate match the dispatcher's notion of "same command"), and a **lifetime/robustness** change (ref-count interlock, hoist a call, validate firmware data).

| Fix shape | Findings | Cost | ABI |
|---|---|---|---|
| Add a missing lower/upper bound | S1 (`size==0`), S12 (`num_entries_in` cap) | trivial | none |
| Mirror the twin handler's check | S2 (`ncdev_ncid_valid`), S7 (`>` → `>=`) | trivial | none |
| Align the gate with the dispatcher | S3 (exact-`cmd` → `_IOC_NR`) | trivial | none |
| Make a lifetime ref-count-aware | S6 (defer free while `ref_cnt>0`) | moderate | none |
| Hoist a call out of a hot loop | S8 (`npe_notify_mark` once after loop) | trivial | none |
| Gate an ungated state mutation | S5 (CRWL behind attach) | small | none |
| Add an opt-in privilege tier | S4 (module-param cap gate) | moderate | opt-in |
| Guard a latent / clarity foot-gun | S9 (`else` branch), S14 (`O_ACCMODE` mask), S13 (out-param), S11 (explicit reject) | trivial | none |
| Validate firmware-supplied data | S10 (response CRC recompute) | small | none |

> **NOTE —** the dominant shape is *symmetry repair*. S2, S7, and S3 are each a case where one handler in the file is already correct and its near-twin is not — the events handler omits the `nc_id` check the semaphore handler makes (`neuron_cdev.c:1437`), the event bound uses `>` where the semaphore bounds use `>=` (`neuron_core.c:55` ff.), and the attach sub-gate tests exact `cmd` while the dispatcher tests `_IOC_NR`. A maintainer should review each fix against the existing-correct site, which is the cheapest possible correctness argument.

---

## 2. The Prioritized Remediation Table

The full roster, ordered by priority band, then by finding severity within the band. `Priority` is `reachability × impact`: a finding on a `none`-gate or ungated `O_WRONLY` lane with a concrete out-of-bounds outranks one behind a gate or one that is firmware-TCB. `Confidence` is inherited from the findings page — `HIGH` where the patched line was re-read verbatim, lower where it depends on out-of-tree (arch/HW/firmware) behaviour. Severity in parentheses is the finding's own band (MED / LOW / INFO), kept visible so the priority is auditable against it.

| Fix | S-id (sev) | Code site (`file:line`) | Concrete fix | Priority | Conf |
|---|---|---|---|---|---|
| **H1** | S1 (MED) | `neuron_cdev.c:2342`, guard before `:2349` | reject `arg.size == 0` in the upper-bound test; force `str[arg.size-1]=0` after the copy | **P1** | HIGH |
| **H2** | S2 (MED) | `neuron_cdev.c:1462`, after the copy in `ncdev_events_ioctl` | call `ncdev_ncid_valid(arg.nc_id)` — mirror the semaphore handler at `:1437` | **P1** | HIGH |
| **H3** | S7 (LOW) | `neuron_core.c:130`, `:147` | change `event_index > event_count` to `>=` — match semaphores at `:55/73/92/111` | **P1** | HIGH |
| **H4** | S3 (MED) | `neuron_cdev.c:3212/3214/3215` (sub-gate enum) | test by `_IOC_NR(cmd)` so the `*64` width is attach-gated like the base | **P1** | HIGH |
| **H5** | S8 (LOW) | `neuron_crwl.c:242` (notify inside the 512-iter loop) | hoist `npe_notify_mark` to one call after the loop, only if ≥1 bit was cleared | **P1** | HIGH |
| **H6** | S5 (MED) | `neuron_cdev.c:2163`/`:2220` handlers; dispatch `:3149/3151` | gate CRWL mark/unmark behind `npid_is_attached`; cap per-pid reservations | **P2** | HIGH |
| **H7** | S6 (LOW) | `neuron_mmap.c:118-128`; callers `:185`, `:225` | make node teardown ref-count-aware: defer `rb_erase`/`kfree` (or revoke the peer sg) while `dmabuf_ref_cnt > 0` | **P2** | HIGH |
| **H8** | S4 (MED) | whole `ioctl` path; sole `capable()` at `neuron_mmap.c:362` | add an **opt-in** (module-param, default off) `CAP_SYS_RAWIO` gate on `BAR_*`/`PROGRAM_ENGINE*`/raw DMA | **P2** | HIGH |
| **H9** | S9 (LOW) | `neuron_mmap.c:392-398` (no `else` after `bar_num` 0/4) | add `else { pr_err(...); return -EINVAL; }`, or init `bar_pa = 0` and validate | **P2** | HIGH |
| **H10** | S14 (INFO) | `neuron_cdev.c:52` (`(f_flags & O_WRONLY) == 1`) | rewrite as `(filep->f_flags & O_ACCMODE) == O_WRONLY` for clarity | **P2** | HIGH |
| **H11** | S10 (INFO) | `neuron_fw_io.c:471-491` (only `dw0` read) | recompute and verify the response `crc32` (`dw1`) over header+data before accepting | **P3** | firmware |
| **H12** | S11 (INFO) | `neuron_fw_io.c:376`/`:382-383` | compute the data length once with an explicit `if (resp_size + sizeof < size) reject` | **P3** | firmware |
| **H13** | S12 (INFO) | `neuron_cdev.c:2402` (`kmalloc(n * 24)`) | cap `arg.num_entries_in` to a sane maximum before the multiply | **P3** | non-exploit |
| **H14** | S13 (INFO) | `neuron_p2p.c` `register_and_get_pa` (`u64` error-as-PA) | return the PA via an out-param + `int` error code so no aligned sentinel can slip | **P3** | self-mitig |

> **GOTCHA —** the priority band is **not** the finding severity. H3 (S7, LOW) and H5 (S8, LOW) sit in `P1` because the fix is one line on a lane that needs no `DEVICE_INIT`, so the cost/benefit dominates; H8 (S4, MED) sits in `P2` because it is a design change (opt-in, moderate cost) whose mitigating BAR range-checks already bound the exposure. Ordering by raw severity would misrank both. Read the band as "do these first because they are cheap and reachable," not "these are the worst bugs."

---

## 3. P1 — Cheap Concrete OOB and Gate-Bypass Fixes

These five close the concrete out-of-bounds and gate-bypass bugs reachable with no `DEVICE_INIT`. Each is a few lines, no ABI change, and each repairs a site that has a correct twin elsewhere in the same file.

### H1 — S1: Lower-Bound the PRINTK Size

The handler bounds `arg.size` from above (`:2342`) but never from below, so `arg.size == 0` makes the trailing-NUL check index `str[arg.size - 1]` = `str[0xFFFFFFFF]` (`:2349`). The fix folds a lower bound into the existing upper-bound test, and additionally NUL-terminates after the bounded copy so a non-terminated buffer cannot reach `printk`.

```c
// neuron_cdev.c:2342 — replace the upper-bound-only test
if (arg.size == 0 || arg.size > sizeof(str))      // was: if (arg.size > sizeof(str))
    return -EFAULT;
// ... neuron_copy_from_user(str, arg.buffer, arg.size)  :2345
str[arg.size - 1] = 0;                            // force termination after the bounded copy
if (str[arg.size - 1] != 0) return -EBADMSG;      // :2349 — now arg.size>=1, index is in [0,511]
```

> **NOTE —** prefer `arg.size >= sizeof(str)` over `> sizeof(str)` so the `[arg.size-1]` write to force the NUL always has room inside `str[512]`; with the strict `>` an `arg.size == 512` copy fills the buffer and the forced terminator at `[511]` is still in-bounds, so either bound is safe — the essential change is the `== 0` rejection. No ABI change: the `struct neuron_ioctl_printk` layout (`neuron_ioctl.h:519-523`) is untouched.

### H2 — S2: Mirror the Semaphore Handler's `nc_id` Validation

`ncdev_events_ioctl` (`:1457`) forwards `arg.nc_id` to `nc_event_get/set` (`:1468/1473`) with no validation, so an unbounded `nc_id` scales a BAR0 MMIO offset into `writel`. The semaphore handler a few lines above already does the right thing at `:1437`; the fix is to copy that one call into the events handler immediately after the `copy_from_user` at `:1462`.

```c
// neuron_cdev.c — in ncdev_events_ioctl, right after the copy at :1462
ret = ncdev_ncid_valid(arg.nc_id);                // mirror ncdev_semaphore_ioctl :1437
if (ret) return ret;                              // rejects nc_id >= nc_per_device with -E2BIG (:95-101)
// then the existing nc_event_get/set dispatch at :1468/:1473
```

> **NOTE —** `ncdev_ncid_valid` (`neuron_cdev.c:95-101`) is the established validator — it is already called in the semaphore handler (`:1437`) and twice more in CRWL handlers (`:2265`, `:2282`). Reusing it keeps the rejection semantics (`-E2BIG`) consistent across the family. As a defense-in-depth complement, `nc_get_event_addr_v{2,3}` should additionally clamp `event_index`/`nc_id` so the address math is robust even if a caller bypasses the handler check.

### H3 — S7: Make the Event Bound Inclusive

`nc_event_get/set` (`neuron_core.c:130/147`) use `event_index > event_count`, admitting `event_index == event_count` — one slot past `[0, event_count)`. The four semaphore checks in the same file already use `>=` (`:55/73/92/111`). The fix is the single-character change that makes the event paths match the semaphore paths.

```c
// neuron_core.c:130 and :147
if (event_index >= ndhal->ndhal_address_map.event_count)   // was: event_index >
    return -EINVAL;
// contrast — the already-correct semaphore bound at :55/73/92/111:
//   if (semaphore_index >= ndhal->ndhal_address_map.semaphore_count) ...
```

> **CORRECTION (S7 scope) —** this fix is independent of H2 even though both ride the `EVENT_*` path. H2 bounds `nc_id` (the per-NeuronCore selector that scales the BAR0 window); H3 bounds `event_index` (the within-window slot, +4 bytes). A maintainer who applies only one leaves the other out-of-bounds reachable. Apply both.

### H4 — S3: Gate the `*64` Width by `_IOC_NR`

The attach sub-gate enumerates by exact full `cmd` (`:3212` `DMA_COPY_DESCRIPTORS`, `:3214` `MEM_COPY`, `:3215` `MEM_COPY_ASYNC`), but the dispatcher reaches the `*64` siblings by `_IOC_NR` (`:3271/3294/3296`). Same `nr`, larger `_IOC_SIZE` ⇒ the exact-`cmd` test is false for the `*64` width, so `npid_is_attached` (`:3220`) is skipped. The fix makes the gate use the same `_IOC_NR` notion of "same command" the dispatcher uses.

```c
// neuron_cdev.c:3212-3215 — change the three MEM/DMA tests to NR-based
_IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_DMA_COPY_DESCRIPTORS) ||   // was: cmd == NEURON_IOCTL_DMA_COPY_DESCRIPTORS  (:3212)
_IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_MEM_COPY)            ||    // was: cmd == NEURON_IOCTL_MEM_COPY              (:3214)
_IOC_NR(cmd) == _IOC_NR(NEURON_IOCTL_MEM_COPY_ASYNC)           // was: cmd == NEURON_IOCTL_MEM_COPY_ASYNC        (:3215)
// ... if (!npid_is_attached(nd)) return -EACCES;               (:3220) now covers both 32-bit and *64 widths
```

> **GOTCHA —** the alternative — adding the explicit `*64` constants (`NEURON_IOCTL_MEM_COPY64` etc.) to the enumeration — also works, but it is fragile: any future `*64`-style overload that shares an `nr` will re-open the hole, and the maintainer must remember to add it to *both* the gate list and the dispatcher. The `_IOC_NR` form closes the class of bug, not just the three current instances. Prefer it.

### H5 — S8: Hoist `npe_notify_mark` Out of the Unmark Loop

`ncrwl_nc_range_unmark` (`neuron_crwl.c:232`) calls `npe_notify_mark(ncrwl_range_mark_cnt, false)` inside its 512-iteration loop (`:242`), once per global NC index regardless of ownership — up to 512 mutex round-trips into the pod-election singleton per `ioctl`, and on V3 a repeated `mark_cnt==0` reset of the pod mode (`v3/neuron_pelect.c:1245`). The fix tracks whether the caller actually cleared a bit and notifies once, after the loop.

```c
// neuron_crwl.c:232 — ncrwl_nc_range_unmark
void ncrwl_nc_range_unmark(volatile unsigned long *free_map) {
    bool cleared = false;
    mutex_lock(&ncrwl_range_lock);                                  // :187 lock
    for (i = 0; i < MAX_NEURON_DEVICE_COUNT * MAX_NC_PER_DEVICE; i++) {
        if (test_bit(i, free_map) && ncrwl_range_pids[i] == task_tgid_nr(current)) {
            ncrwl_range_pids[i] = 0;                                // :238
            ncrwl_range_mark_cnt--;                                 // :239
            cleared = true;
        }
        // REMOVED: npe_notify_mark(...) was here at :242 — up to 512×
    }
    if (cleared)                                                    // notify ONCE, only on real change
        ndhal->ndhal_npe.npe_notify_mark(ncrwl_range_mark_cnt, false);
    mutex_unlock(&ncrwl_range_lock);
}
```

> **NOTE —** the mark path (`ncrwl_nc_range_mark`, `:188`) already notifies exactly once, at the successful-claim site (`:214`). Hoisting the unmark notify to a single post-loop call makes the two paths symmetric and removes the amplification. On v2/STD the sink is a no-op (`v2/neuron_dhal_v2.c:1210-1212`), so this is a pure win on V3 and free elsewhere. Pair with H6 to also remove the *ungated reach* — the hoist fixes amplification, H6 fixes who can trigger it.

---

## 4. P2 — Defense-in-Depth

These four close the structural gaps: an ungated state mutation, a teardown lifetime hole, the absent privilege tier, and two latent foot-guns. They are larger than the P1 set but still ABI-compatible (S4 opt-in by module param).

### H6 — S5: Gate the CRWL Allocator Behind Attach

`CRWL_NC_RANGE_MARK`/`UNMARK` dispatch on the ungated `O_WRONLY` misc lane (`:3149/3151`) into handlers (`:2163`/`:2220`) that mutate the global `ncrwl_range_pids[]` allocator (`neuron_crwl.c:185`, a single static array spanning all devices) with no attach gate. The fix adds an attach check inside the handlers (or routes the commands off the free-access lane), and caps per-pid reservations to bound exhaustion.

```c
// neuron_cdev.c:2163 / :2220 — at the top of ncdev_crwl_nc_range_mark / _unmark
if (!npid_is_attached(nd))                         // identity gate (neuron_pid.c:60)
    return -EACCES;                                // only the DEVICE_INIT owner may mutate the global map
// plus, in ncrwl_nc_range_mark (neuron_crwl.c:188): refuse if this tgid already holds
// >= a per-pid reservation cap, to bound the 512-slot allocator against churn/exhaustion.
```

> **GOTCHA —** unmark is already pid-scoped — it clears only `ncrwl_range_pids[i] == current tgid` (`neuron_crwl.c:237`), so no cross-tenant *free* is possible even today. The attach gate is about *who may participate at all* in the global allocator, and the per-pid cap is about *exhaustion*; do not conflate the two. The clean alternative for a multi-tenant node is per-tenant device isolation (§6), which removes the shared global state from the threat model entirely.

### H7 — S6: Ref-Count-Aware Node Teardown

`nmmap_remove_node_rbtree` (`neuron_mmap.c:118`) only `pr_warn`s when `dmabuf_ref_cnt > 0` (`:123-125`) then `rb_erase`s (`:127`); the callers `nmmap_delete_node` (`:185`) and `nmmap_delete_all_nodes` (`:225`) `kfree(mmap)` regardless, while a peer importer may still hold an `sg_table` into the backing memory. The fix makes the free wait for, or be deferred until, `dmabuf_ref_cnt` drops to 0.

```c
// neuron_mmap.c:118 — nmmap_remove_node_rbtree, escalate warn-only to a real interlock
static int nmmap_remove_node_rbtree(struct rb_root *root, struct nmmap_node *mmap) {
    if (mmap->dmabuf_ref_cnt > 0) {
        // OPTION A (defer): unlink-but-do-not-free; mark for reclaim; the last
        //   ndmabuf_unmap (which decrements the ref) performs the kfree when ref==0.
        // OPTION B (revoke+wait): invoke the free_callback / revoke the peer sg,
        //   then block until ref_cnt == 0 before rb_erase + kfree.
        return -EBUSY;                              // signal "not yet freeable" to the caller
    }
    rb_erase(&mmap->node, root);                    // :127 — only when no peer DMA is in flight
    return 0;
}
// callers at :185 / :225 honor the return: only kfree(mmap) when the erase succeeded.
```

> **NOTE —** the driver already has the machinery to make Option A clean: `ndmabuf_map`/`unmap` re-search the node and tolerate a freed node (`neuron_dmabuf.c`), and the `free_callback` path is the intended teardown signal. The minimal correct change is to make `kfree` conditional on `dmabuf_ref_cnt == 0` and to drive the actual free from the ref-drop in `ndmabuf_unmap`. The race is narrow (app dies mid-DMA between register and deregister, acknowledged in the comment at `:120-122`), so a deferred-free is preferable to a hard block that could stall process exit.

### H8 — S4: Opt-In Capability Gate

There is no Linux capability check on any `ioctl` path — the sole `capable()` is `CAP_SYS_RAWIO` in an `mmap` path (`neuron_mmap.c:362`). `BAR_*`, `PROGRAM_ENGINE*`, and the raw DMA family are authorized by the identity-only `npid_is_attached` (`neuron_pid.c:60`). The fix adds an *opt-in* capability gate, defaulting off so existing `neuron`-group deployments are not broken, and documents the trust model explicitly.

```c
// a new module param, default off for back-compat:
static bool enforce_rawio_cap = false;
module_param(enforce_rawio_cap, bool, 0644);
MODULE_PARM_DESC(enforce_rawio_cap,
    "Require CAP_SYS_RAWIO for raw-MMIO / engine / DMA ioctls (default: off, file-perm only)");

// at the dispatch of BAR_*, PROGRAM_ENGINE*, raw DMA cmds (neuron_cdev.c, in ncdev_ioctl):
if (enforce_rawio_cap && !capable(CAP_SYS_RAWIO))
    return -EPERM;                                  // opt-in privilege tier above the identity table
```

> **NOTE —** the BAR access path is *already* range-checked — `ncdev_bar_read`/`write_data` bound each register to `[bar0, bar0+bar0_size)` and BAR0 writes consult `ndma_is_bar0_write_blocked`; BAR4 r/w is disabled. So this fix is defense-in-depth over a bounded surface, which is why S4 is MED, not HIGH, and the fix is `P2`, not `P1`. The structural mitigation is out-of-driver: per-tenant device isolation (§6) restores the node-level separation the shared `neuron`-group model dissolves.

### H9 — S9: Close the `nmap_dm_special` `bar_num` Default

`nmap_dm_special` (`neuron_mmap.c:368`) declares `bar_pa` uninitialized (`:374`) and assigns it only for `bar_num == 0` (`:392`) / `== 4` (`:394`), with no `else`, then uses it in `io_remap_pfn_range` (`:398`). The shipped DHAL tables only ever emit `bar_num` 0/4, so this is latent — but the fix is one cheap guard that converts a future-table foot-gun into a clean rejection.

```c
// neuron_mmap.c:392-398
if (bar_num == 0)      bar_pa = nd->npdev.bar0_pa;        // :392-393
else if (bar_num == 4) bar_pa = nd->npdev.bar4_pa;       // :394-395
else { pr_err("nmap_dm_special: unexpected bar_num %d\n", bar_num); return -EINVAL; }   // NEW
ret = io_remap_pfn_range(vma, vma->vm_start, (offset + bar_pa) >> PAGE_SHIFT, ...);      // :398, bar_pa now always set
```

> **NOTE —** initializing `bar_pa = 0` at `:374` and validating before use is an equivalent fix, but the explicit `else { return -EINVAL; }` is preferable: it fails loudly if a future DHAL table emits an unexpected `bar_num`, rather than silently mapping physical frame 0. Defensive-only — not reachable on the shipped tables.

### H10 — S14: Make the Free-Access Macro Mode-Explicit

The free-access macro `((filep->f_flags & O_WRONLY) == 1)` (`:52`) is correct by current intent (`O_WRONLY == 1` is the only value that matches; `O_RDWR == 2` is not free-access), but the `== 1` reads as a foot-gun for anyone who reasons "writable ⇒ free-access." The fix expresses the access-mode test the kernel-idiomatic way.

```c
// neuron_cdev.c:52
#define IS_NEURON_DEVICE_FREE_ACCESS(filep) \
    (((filep)->f_flags & O_ACCMODE) == O_WRONLY)     // was: ((filep->f_flags & O_WRONLY) == 1)
```

> **GOTCHA —** the new form is *behaviourally identical* to the old (both select exactly an `O_WRONLY` open and exclude `O_RDWR`), so it is a pure clarity fix — no routing change. Do **not** "fix" it to `(f_flags & O_WRONLY) != 0`, which would route `O_RDWR` (value `2`, low bit `0`) and `O_WRONLY` differently than the rest of the driver assumes and could re-classify fds across the free-access split. Apply at all five use sites consistently (`:3206`, `:3400`, `:3463`, `:3504`); the macro change covers them.

---

## 5. P3 — Firmware-Trust and Non-Exploitable Hygiene

None of these is userspace-reachable. S10/S11 are inside the firmware trust boundary (the firmware already owns the device); S12/S13 are benign-on-LP64 / self-mitigated. They are documented as robustness — fixes that detect silent corruption and remove latent smells without pretending to be privilege fixes.

### H11 — S10: Validate the FW-IO NEW-Path Response CRC

`fw_io_execute_request_new` (`neuron_fw_io.c:406`) reads only `resp_header.reg.dw0` (`:471`) — sequence number, error code, size — and never reads the `crc32` field (`dw1`), so a malfunctioning firmware response is accepted on `(seq, error_code)` alone. The driver already computes a CRC on the *request* (`:351`, via `crc32c` at `:308`); the fix reuses that routine to validate the *response*.

```c
// neuron_fw_io.c — after reading the response header/data (:471-491), before accepting:
{
    u32 expected = resp_header.hdr.crc32;            // dw1, currently never read
    u32 actual   = crc32c((const u8 *)&resp_header,  // reuse the existing crc32c (:308)
                          resp, copy_size);          // over response header + data
    if (expected != actual) {
        pr_err("fw_io: response CRC mismatch (got 0x%x want 0x%x)\n", actual, expected);
        return -EIO;                                 // detect MiscRAM corruption / silent FW fault
    }
}
```

> **NOTE —** the copy length is already bounded — `data_size = resp.size - sizeof(resp_header)` (`:480`) clamped by `min(resp_size, data_size)` (`:482`) with driver-supplied `resp_size` — so this is integrity, not memory safety. It is `P3` because firmware is in the TCB: a compromised firmware can already drive the device. The value is detecting *unintentional* corruption (a flaky MiscRAM read, a desynced response), which the request-side CRC machinery makes nearly free to add on the response side.

### H12 — S11: Make the Legacy Length-Reject Explicit

The legacy path computes `(response_hdr.hdr.size - sizeof(struct fw_io_response))` with a `u16` firmware-supplied `.size` (`:376`); if `.size < sizeof(...)` the subtraction underflows to a huge `size_t`, but the guard `(...) > resp_size` (`:376`) catches it (`huge > resp_size` ⇒ reject) and the `memcpy` (`:382-383`) uses the same expression only after the guard passes. It is self-mitigated; the fix is clarity, so the intent is not one refactor away from re-breaking.

```c
// neuron_fw_io.c:376 — compute once with an explicit, underflow-safe reject
const size_t hdr = sizeof(struct fw_io_response);
const u16    fw_size = ctx->response->response_hdr.hdr.size;
if (fw_size < hdr || (fw_size - hdr) > resp_size)    // explicit: reject undersize AND oversize
    goto done;                                        // was: only (fw_size - hdr) > resp_size
data_len = fw_size - hdr;                              // single computed length used by the memcpy at :382-383
```

> **CORRECTION (S11 status) —** the seed flagged this as a possible firmware-driven overread; re-reading the shipped source confirmed the `>resp_size` guard already catches the underflow, so it is **self-mitigated**, not a live bug. This fix changes nothing functionally on the shipped code — it makes the underflow rejection a named, intentional `fw_size < hdr` check instead of an emergent property of unsigned comparison, so a future refactor of the guard cannot silently re-introduce the overread.

### H13 — S12: Cap `num_entries_in` Before the Multiply

`ncdev_dump_mem_chunks` (`neuron_cdev.c:2390`) does `kmalloc(arg.num_entries_in * sizeof(struct neuron_ioctl_mem_chunk_info))` (`:2402`). On LP64, `u32 × size_t` promotes to 64-bit, so `0xFFFFFFFF × 24 ≈ 96 GB` does not wrap and `kmalloc` simply returns `NULL` ⇒ `-ENOMEM`. The fix caps the count, which also removes the theoretical ILP32 wrap and avoids a pointless multi-GB allocation attempt.

```c
// neuron_cdev.c:2401 — before the kmalloc at :2402
if (arg.num_entries_in > NEURON_DUMP_MAX_ENTRIES)     // a sane cap (e.g. total chunks the nd can hold)
    return -EINVAL;
if (arg.num_entries_in > 0) {
    data = kmalloc(arg.num_entries_in * sizeof(struct neuron_ioctl_mem_chunk_info), GFP_KERNEL);  // :2402
    ...
}
```

> **NOTE —** benign on the x86-64/arm64 targets — the `copy_to_user` is already clamped by `arg.num_entries_in >= num_entries_out` (`:2417`), so no over-copy is possible. The cap is hygiene: it bounds the allocation request to something physically meaningful and closes the ILP32 wrap that is out of scope for the shipped targets.

### H14 — S13: Return the P2P PA via an Out-Parameter

`neuron_p2p_register_and_get_pa` returns a `u64` that overloads "the physical address" and "an error code cast to `u64`" — `(u64)-EINVAL` on failure, `0` for not-found, the real PA on success. It is self-mitigated: the caller's alignment guard rejects `-EINVAL` (not page-aligned) and the `if (!pa)` rejects `0`. The fix removes the type confusion structurally.

```c
// neuron_p2p.c — split the channel: PA out-param + int error return
static int neuron_p2p_register_and_get_pa(..., phys_addr_t *pa_out) {  // was: u64 return
    if (offset + size > mmap->size) return -EINVAL;    // error via the int return, never as a "PA"
    if (!found)                       return -ENOENT;
    *pa_out = real_pa;                                 // PA only on success
    return 0;
}
// caller (neuron_p2p_register_va): check the int return, then use *pa_out — no aligned-sentinel risk.
```

> **CORRECTION (S13, P2P NOTE-1 reversed) —** the seed claimed the `u64` error-as-PA slipped into the page table; re-reading the shipped source overturned that — the alignment guard `if (pa % PAGE_SIZE != 0) return -EINVAL` catches every small errno (none is page-aligned) and `if (!pa)` catches the `0` not-found case. So this is **INFO / self-mitigated**, and H14 is a structural-clarity fix, not a security patch: it removes the reliance on the contingent fact that no errno is page-aligned, so a future page-aligned sentinel cannot defeat the guard. Not userspace-reachable (`EXPORT_SYMBOL_GPL`, called by the in-kernel EFA peer).

---

## 6. Deployment Hardening (Out-of-Driver)

The code fixes close the concrete bugs; the residual exposure is a property of how the *node* is configured, because the entire authorization story collapses to "can the caller open `/dev/neuronN`" plus an identity table (`neuron_pid.c:60`). Three operational measures compensate for the structural choice, and they matter most where the code fixes cannot reach: the design root cause (S4) and the cross-tenant findings (S5/S8) are fundamentally about *who shares the node*.

### Device-node permissions are Gate 0 — treat them as the mandatory gate

The `/dev/neuronN` permission (default `root:neuron 0660`) is the **only mandatory authentication** in the model; everything in the driver presumes an already-open fd. Harden it as the primary boundary:

- Keep the node `root:neuron 0660`; audit the `neuron` group membership as the *actual* trust set — every member is a peer under the attach identity table, with no further isolation beyond handle ownership.
- Do not loosen the node to world-accessible; there is no in-driver gate that would compensate (no capability tier, S4).
- Treat udev/operator policy as part of the TCB: the driver delegates Gate 0 entirely to it.

### Do not expose the `O_WRONLY` free-access lane to untrusted tenants

The `O_WRONLY` open routes to the ungated `ncdev_misc_ioctl` lane (`neuron_cdev.c:3147`, split at `:3206`) — 14 commands with **no** attach, capability, or `DEVICE_INIT` gate. That lane carries the state-changing CRWL mark/unmark (S5/S8) and `DMABUF_FD` (S6). For a hardened deployment:

- Any tenant that does not need the misc lane should be denied an `O_WRONLY` open at the application/namespace layer; the lane is a confinement, but the confined commands are ungated.
- Until H6 lands, treat CRWL reservation exhaustion (S5) as a live cross-tenant DoS on any node where untrusted processes can `open("/dev/neuronN", O_WRONLY)` — the mitigation is access control on the node, not the driver.

### Per-tenant device isolation moves the boundary back to Gate 0

The cleanest compensation for the file-permission + identity model is **one `/dev/neuronN` per container/tenant**:

- It moves the trust boundary back to Gate 0, where the kernel enforces it, instead of leaning on the cooperative discipline of the shared misc lane.
- It dissolves the cross-tenant exposure of the shared global state — `ncrwl_range_pids[]` is a single static array spanning all devices (`neuron_crwl.c:185`), so two tenants sharing a node are peers in the same allocator (S5); one node per tenant removes that sharing.
- It is the deployment analogue of H8: where the opt-in capability gate restores a privilege tier in-driver, device isolation restores node-level separation out-of-driver. Use both for defense-in-depth.

> **NOTE —** teardown already reconciles the free-access lane at process exit: `ncdev_misc_flush` unmarks all NCs for a closing free-access fd, and the full-fd `ncdev_flush` releases the attach on last close. So the S5 exposure is *exhaustion while live*, not a permanent leak — which is why the deployment mitigation (limit who holds a live `O_WRONLY` fd) is as effective as the code fix for the DoS, though only the code fix (H6) removes the ungated *mutation*.

---

## Related Components

| Name | Relationship |
|---|---|
| [The IOCTL Attack Surface (14 Findings)](ioctl-attack-surface.md) | the finding roster this page remediates; each Hn keys to an Sn there |
| [Overview and the Privilege-Gate Model](overview.md) | the three-tier gate model and trust boundaries the fixes are calibrated against |
| `ncdev_ncid_valid` (`neuron_cdev.c:95`) | the existing validator H2 reuses; already called by the semaphore/CRWL handlers |
| `npid_is_attached` (`neuron_pid.c:60`) | the identity gate H6 extends to the CRWL handlers; the boundary H8 layers a capability tier over |

## Cross-References

- [The IOCTL Attack Surface (14 Findings)](ioctl-attack-surface.md) — the reachability-ranked finding roster (S1–S14) this roadmap fixes, with per-finding mechanic, impact, and lane
- [Overview and the Privilege-Gate Model](overview.md) — the three-tier gate model, trust boundaries, and severity-calibration philosophy the priorities here inherit
- [FW-IO Trust Boundary](fw-io-trust.md) — the kernel↔firmware mailbox security analysis behind H11 (response CRC) and H12 (legacy length reject)
- [IOCTL Dispatch and the Privilege-Gate Model](../kernel/ioctl-dispatch.md) — the dispatcher whose exact-`cmd`-vs-`_IOC_NR` mismatch H4 repairs, and the marshalling helper that fronts H1/H2
- [back to index](../index.md)
