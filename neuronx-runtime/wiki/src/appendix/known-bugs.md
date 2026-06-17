# Known-Bugs and Anomalies Catalog

> **Userspace binary:** `libnrt.so` `2.31.24.0-0b044f4ce` (BuildID[sha1] `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, ELF64 x86-64, not stripped, DWARF v4) · **Kernel driver source:** `aws-neuronx-dkms 2.27.4.0` (GPL-2.0 C, SHA `1c7ed9bd…`, `MODULE_VERSION("2.27.4.0")`) · DKMS root `extracted/aws-neuronx-dkms_2.27.4.0_all/usr/src/aws-neuronx-2.27.4.0/`.
>
> **Part XVI — Appendices** / REFERENCE · **Evidence grade:** GPL findings cited `file:line` + symbol against the shipped source re-read in place; `libnrt.so` findings cited `addr+symbol` from non-stripped disassembly. Hardware-bug workarounds are anchored to the DHAL callback that arms them. · [back to appendix index](subsystem-matrix.md)

## Abstract

This page is the consolidated catalog of every *defect* the Neuron runtime either **works around** or **carries**. It collects two kinds of finding that are otherwise scattered across the per-subsystem pages: (A) **silicon/firmware bugs** that the driver mitigates in software — the DMA-in-reset-window hang and the aggressive single-descriptor prefetch on V2, plus the sequencer/firmware anomalies the reset and FW-IO paths tolerate; and (B) **software defects in the shipped code itself**, confirmed during the reverse-engineering sweep — two are outright bugs with byte-confirmed evidence (a permanent mutex deadlock in the DMA-ring layer, a success-on-error return in the microcode loader), the rest are source warts and a 14-entry kernel-driver attack surface.

The split matters to a reimplementer. A class-(A) workaround is mandatory: omit `ndma_retry_memcpy` and a model under concurrent reset will hang the DMA engine; omit the `DESC_PER_PACKET=64` cap and the UDMA prefetch corrupts. A class-(B) defect is the opposite — it is code a reimplementer must **not** copy: the `return -EIO;` paths in `ndmar_queue_get_descriptor_info` leak a per-engine mutex forever, and `ucode_alloc_new_staged_libs` reports `NRT_SUCCESS` while leaving its out-param dangling. Each entry below states the symptom, the root cause, the in-code workaround or current status, the owning page that documents it in depth, and a confidence grade.

This is a reference catalog, not a tutorial: it does not re-derive the mechanisms (the owning pages do that), it indexes them with enough anchor that a reader can find the exact line and verify the claim. Where this sweep overturned an earlier seed claim, the reversal is recorded in place as a `CORRECTION` callout rather than silently dropped.

| | |
|---|---|
| **HW-workaround entries** | 4 (V2 BUG-A, V2 BUG-B, V3 reset-map FIXME, FW-IO trust) |
| **Confirmed SW defects** | 2 (K-RING-2 deadlock, UCODE success-on-error) — both **CERTAIN** |
| **Source warts** | 4 (commented-out TX/RX validation, kmgr ordering, GEN-01 stale alias, NDS doc-vs-code) |
| **Security findings** | 14 (S1–S14), owned by [ioctl-attack-surface](../security/ioctl-attack-surface.md) |
| **Single `capable()` in driver** | `CAP_SYS_RAWIO` @ `neuron_mmap.c:362` — *every* ioctl path is capability-free |
| **Evidence convention** | GPL → `file:line`+symbol · binary → `addr`+symbol · arch-gated rows tagged V2/V3/V4 |

---

## 1. At a glance — the three defect classes

A reimplementer treats the three classes differently, so the catalog table in [§2](#2-the-bug-catalog) carries a Class column drawn from this triage.

| Class | What it is | How to treat it |
|---|---|---|
| **HW-workaround** | A silicon or firmware bug the runtime mitigates in software | **Reproduce the workaround.** It is mandatory; the device misbehaves without it. |
| **SW-defect** | A bug in the shipped driver/runtime code itself | **Do not copy.** Fix it in any reimplementation; the cited line is wrong. |
| **security** | A reachability/hardening finding in the kernel ioctl surface | Defensive hardening; most are gated by device-node permissions. See the owning page. |

> **NOTE —** "HW-workaround" rows describe code that is *correct because the hardware is broken*. The DMA retry loop looks like a livelock risk and the `DESC_PER_PACKET=64` cap looks like a magic number; both are deliberate mitigations whose rationale lives in the device, not the source. Removing them to "clean up" the code re-introduces the silicon bug.

---

## 2. The bug catalog

One row per defect. **Workaround / Status** states what the shipped code does about it (for HW bugs) or whether the defect is live (for SW bugs). **Owning page** is where the mechanism is documented in full. **Conf** grades the *claim*, not the severity.

| ID | Class | Symptom | Root cause | Workaround / Status | Owning page | Conf |
|---|---|---|---|---|---|---|
| **V2 BUG-A** | HW-workaround | An in-flight H2T DMA **hangs** if a NeuronCore reset overlaps the transfer (V2 silicon) | DMA engine wedges when a reset enters its window mid-copy | `ndma_retry_memcpy=true` (`dhal_v2.c:1444`) → retry loop rebuilds the H2T ring + re-issues descs (`neuron_dma.c:378-429`); **side effect: zerocopy disabled on V2** | [dma-rings](../kernel/dma-rings.md), [dhal-v2](../kernel/dhal-v2.md) | HIGH |
| **V2 BUG-B** | HW-workaround | UDMA prefetch **corrupts** when the descriptors/packet threshold equals the 128-entry prefetch buffer size | Aggressive single-descriptor prefetch at the buffer boundary | `V2_ALLOWED_DESC_PER_PACKET=64` (`v2/address_map.h:65-71`); `ndma_init` passes `64+1=65` to `udma_m2m_init_engine` (`dhal_v2.c:1096-1097`) | [dma-rings](../kernel/dma-rings.md), [dhal-v2](../kernel/dhal-v2.md) | HIGH |
| **V3 B1** | SW-defect | `ndhal_register_funcs_v3()` return value **discarded** in the V4 arch path | `ret = ndhal_register_funcs_v3(); ret = ndhal_register_funcs_v4();` — base registration error masked by the override call | Live; V4-only; a failing V3 base registration is silently swallowed | [dhal-v2](../kernel/dhal-v2.md) (V4 inherits V3) | HIGH |
| **RST-FIXME** | HW-workaround | V3 reset-completion poll references **V2-named** FW-status macros | Shared FW-status register constant never renamed for V3 (`v3:364,371` source FIXME) | Cosmetic; constant value is correct, only the name is V2-scoped | [reset](../kernel/reset.md) | HIGH |
| **RST-POLARITY** | anomaly | `reset_in_progress` local is assigned the **DEVICE_READY** bit — naming inverts intuition | `reset_in_progress = status & …DEVICE_READY_MASK` then `if(!reset_in_progress) return 0` (`dhal_v2.c:196-203`) | Behaviorally correct (READY-bit-clear ⇒ reset done); a confusing-but-correct idiom | [reset](../kernel/reset.md) | MED |
| **FWIO-CRC** | HW-workaround (trust) | FW-IO NEW-path response **CRC32 never validated** (`dw1` unread, `neuron_fw_io.c:469-496`) | Response trusted on `(seq, error_code)` alone; firmware is inside the TCB | Accepted as-is; firmware already owns the device (S10) | [ioctl-attack-surface](../security/ioctl-attack-surface.md) | HIGH |
| **K-RING-2** | SW-defect | **Permanent per-engine mutex deadlock**: six error paths in `ndmar_queue_get_descriptor_info` leave `eng->lock` held forever | `return -EIO;` at `neuron_ring.c:633/637/641/651/655/659` execute *before* the unreachable `goto done;`, bypassing `ndmar_release_engine` | **Live, CERTAIN.** Intended `goto done;`; see [§3.1](#31-k-ring-2--permanent-mutex-deadlock-in-ndmar_queue_get_descriptor_info) | [dma-rings](../kernel/dma-rings.md) | CERTAIN |
| **UCODE** | SW-defect | `ucode_alloc_new_staged_libs` returns **`NRT_SUCCESS` on allocation failure** with `*ulib_set_info` unwritten/dangling | All 5 post-`calloc` failure branches funnel to cleanup `@0x310810` then `jmp 0x3107f4` (`xor eax,eax; ret`), skipping the out-param write `@0x3107f0` | **Live, CERTAIN.** Intended `NRT_RESOURCE`; see [§3.2](#32-ucode--success-on-error-in-ucode_alloc_new_staged_libs) | [microcode-loader](../gpsimd/microcode-loader.md) | CERTAIN |
| **RING-VAL** | SW-defect (wart) | TX/RX descriptor-count **size validation commented out** in `ndmar_queue_init` | `neuron_ring.c:178-183` (TX) and `:187-192` (RX) are dead `/* … */`; only RXC is checked (`:196-199`) | Live asymmetry; an oversized TX/RX desc count is no longer rejected against `mc->size` | [dma-rings](../kernel/dma-rings.md) | HIGH |
| **KMGR-ORD** | SW-defect (wart) | `kmgr_unload_nn` erase-then-drain ordering | NN state erased before outstanding work is drained | Flagged; ordering hazard, not yet root-caused to a failure | [dma-rings](../kernel/dma-rings.md) | MED |
| **GEN-01** | SW-defect (wart) | Stale PCI **autoload alias**: `MODULE_ALIAS` device-ID `0x7064` matches **no** Neuron device | `neuron_module.c:24` `"pci:v00001d0fd00007064…"`; real IDs are TRN1 `0x7164` / INF2 `0x7264` / TRN2 `0x7364` (`neuron_device.h:36-37`) | Live; autoload alias never updated; harmless because the PCI driver table gates real binding | [reset](../kernel/reset.md) (driver init) | HIGH |
| **NDS-REF** | doc-vs-code | NDS kernel "refcount" doc-vs-code **mismatch** | Documentation describes a refcount the code does not maintain symmetrically | Flagged for reconciliation; no functional bug confirmed | [deep-dive-backlog](deep-dive-backlog.md) | LOW |
| **S1–S14** | security | 14 ioctl attack-surface findings (OOB MMIO, gate bypass, UAF window, ungated allocator) | No capability gate on any ioctl; identity-only PID attach table | Hardening recommendations; see row group in [§4](#4-security-findings-s1s14) | [ioctl-attack-surface](../security/ioctl-attack-surface.md) | mixed |

---

## 3. Confirmed software defects — the evidence

Two SW-defects are byte-confirmed and CERTAIN. They are documented here with the exact code path because they are the entries a reimplementer is most likely to copy by accident.

### 3.1 K-RING-2 — permanent mutex deadlock in `ndmar_queue_get_descriptor_info`

`ndmar_queue_get_descriptor_info` (`neuron_ring.c:612`) reads the device-side TX/RX descriptor-ring physical addresses and lengths out of UDMA registers. It acquires the per-engine mutex up front and is *structured* to release it through a single `done:` label:

```c
// ndmar_queue_get_descriptor_info  (neuron_ring.c:612-672)
int ndmar_queue_get_descriptor_info(struct neuron_device *nd, u8 eng_id, u8 qid, ...) {
    int ret = 0;
    struct ndma_eng *eng = ndmar_acquire_engine(nd, eng_id);   // :622 — mutex_lock(eng->lock)
    if (eng == NULL) return -EINVAL;

    if (udma_q_handle_get(udma, qid, UDMA_TX, &hw_q) != 0) {
        ret = -EINVAL; goto done;                              // :629-630 — CORRECT path
    }
    if (reg_read32(&hw_q->q_regs->rings.drbp_high, &high)) {
        return -EIO;                                           // :633 — BUG: bypasses done:
        goto done;                                            //  :634 — UNREACHABLE (dead)
    }
    if (reg_read32(&hw_q->q_regs->rings.drbp_low,  &low))  { return -EIO; goto done; }  // :637
    if (reg_read32(&hw_q->q_regs->rings.drl,    tx_size)) { return -EIO; goto done; }  // :641
    // ... UDMA_RX ...
    if (reg_read32(&hw_q->q_regs->rings.drbp_high, &high)) { return -EIO; goto done; }  // :651
    if (reg_read32(&hw_q->q_regs->rings.drbp_low,  &low))  { return -EIO; goto done; }  // :655
    if (reg_read32(&hw_q->q_regs->rings.drl,    rx_size)) { return -EIO; goto done; }  // :659
done:
    ndmar_release_engine(eng);                                 // :670 — mutex_unlock(eng->lock)
    return ret;
}
```

**Root cause.** Six `reg_read32` failure paths (`:633, :637, :641, :651, :655, :659`) do `return -EIO;` *immediately*, before the `goto done;` that follows each of them. The `goto done;` lines are dead code — control never reaches them. Because `ndmar_release_engine` is only called at the `done:` label (`:670`), and `ndmar_release_engine` is exactly `mutex_unlock(&eng->nd->ndma_engine[eng->eng_id].lock)` (`neuron_ring.c:52-55`), every `-EIO` return **leaves the per-engine mutex permanently held**. The mutex is the engine-serialization lock initialized by `mutex_init(&eng->lock)` in `ndmar_preinit` (`:702`); once leaked, every subsequent `ndmar_acquire_engine(nd, eng_id)` for that engine blocks forever → the DMA engine is wedged for the lifetime of the driver.

**Trigger.** A `reg_read32` of a descriptor-ring register fails (MMIO read error). The two `udma_q_handle_get` failures (`:629, :647`) take the *correct* `ret = -EINVAL; goto done;` path and do release the mutex — the asymmetry between those and the six register-read failures is the tell that the `return -EIO;` lines are a copy-paste error, not intent.

> **GOTCHA —** the `goto done;` on the line *after* each `return -EIO;` makes the bug look harmless on a skim — "there is a `goto done`, the mutex is released." It is not: a `return` statement before a `goto` makes the `goto` unreachable. The intended fix is to delete the `return -EIO;` and let the existing `goto done;` (with `ret` set to `-EIO`) run, exactly as the `-EINVAL` paths do. **Owning page: [dma-rings](../kernel/dma-rings.md).** CONFIDENCE: **CERTAIN** — GPL source re-read in place.

### 3.2 UCODE — success-on-error in `ucode_alloc_new_staged_libs`

`ucode_alloc_new_staged_libs` (`@0x310660` in `libnrt.so`, DWARF-attributed to `tdrv/ucode_lib.c:224`) allocates device DRAM for staged microcode libraries and returns the resulting set via the `*ulib_set_info` out-parameter. The struct-`calloc` failure path is correct, but every *later* allocation failure is mishandled.

```text
ucode_alloc_new_staged_libs  (@0x310660, tdrv/ucode_lib.c:224)
  struct calloc fails  → je 0x31089e → mov $0x4,%eax (NRT_RESOURCE) → jmp 0x3107f6 → ret   [CORRECT, status 4]
  lib_dmem      fails  → jne 0x310810 (@0x3106e8) ─┐
  ucode_table   fails  → (@0x310868)               │
  q7_extram     fails  → (@0x31075f)               ├─→ cleanup @0x310810:
  EXTRAM align  fails  → jne 0x310808 (@0x3107ca)   │     nlog_write + dmem_free×3 + free(struct)
  dmem_copyin   fails  → (@0x3107e9)               ─┘     then jmp 0x3107f4   (@0x310863)

  0x3107f0:  mov %rbx,0x0(%r13)     ← writes *ulib_set_info = struct   (SKIPPED by the jmp)
  0x3107f4:  xor %eax,%eax; ...; ret  ← returns 0 (NRT_SUCCESS)
```

**Root cause.** All five post-`calloc` failure branches funnel into the cleanup block at `0x310810` (free everything), then `jmp 0x3107f4` (`@0x310863`). `0x3107f4` is `xor %eax,%eax; ret` — it returns **0 / `NRT_SUCCESS`**. Critically, the jump target `0x3107f4` is *past* the `mov %rbx,0(%r13)` at `0x3107f0` that writes `*ulib_set_info = struct`. So on the failure path the function returns SUCCESS while (a) never writing the out-param and (b) having just `free`d the struct that `%rbx` still points at.

**Effect.** A transient device-DRAM exhaustion during ucode staging returns SUCCESS to the caller (`ucode_stage_libs`), which treats SUCCESS as "`ulib_set_info` populated" and proceeds to `ucode_stage_libs_impl`, dereferencing a NULL/stale/dangling pointer → use-after-free, NULL-deref, or silent corruption instead of a clean `NRT_RESOURCE` (4). The intended code was almost certainly `return NRT_RESOURCE;` on the cleanup path.

> **GOTCHA —** the correct `calloc` path (`mov $0x4,%eax; jmp 0x3107f6`) and the broken later paths (`jmp 0x3107f4`) differ by a **two-byte jump target** — `0x3107f6` lands on the `ret` *after* the status is set, `0x3107f4` lands on the `xor %eax,%eax` that *clears* it. A reimplementer of the `ucode_lib.c` consumer side must return a non-zero status and leave `*ulib_set_info` NULL on every cleanup path. **Owning page: [microcode-loader](../gpsimd/microcode-loader.md).** CONFIDENCE: **CERTAIN** — `xor %eax,%eax; ret` at `0x3107f4` reached from the `0x310863` cleanup `jmp`, confirmed by disasm of a DWARF-attributed function.

---

## 4. Security findings (S1–S14)

The 14 kernel-driver ioctl attack-surface findings are owned in full by [ioctl-attack-surface](../security/ioctl-attack-surface.md); this catalog summarizes them as a row group so a reader scanning *all* defects sees them in one place. Every finding presumes the attacker already holds an fd to `/dev/neuronN` — the driver's only mandatory authentication is the device-node permissions (default `root:neuron 0660`), and there is **zero** Linux capability gate on any ioctl path (the single `capable(CAP_SYS_RAWIO)` is in an mmap path, `neuron_mmap.c:362`).

| ID | Sev | Title | Reach / gate | Class |
|---|---|---|---|---|
| S1 | MED | PRINTK `size==0` → `str[arg.size-1]` OOB stack read (u32 wrap to `0xFFFFFFFF`) | O_WRONLY misc, none | info-leak / DoS |
| S2 | MED | EVENTS ioctl: unvalidated `nc_id` → OOB MMIO address (no `ncdev_ncid_valid`) | O_RDONLY, none | mem-corruption (MMIO) |
| S3 | MED | `*64` size-overload skips the npid attach sub-gate (dispatch by `_IOC_NR`, gate by exact cmd) | O_RDONLY, sub-gate | priv (intra-device) |
| S4 | MED | No capability gate on raw MMIO / DMA / engine ioctls (design) | file-perm only | priv (design) |
| S5 | MED | CRWL mark/unmark on the ungated O_WRONLY misc lane | O_WRONLY misc, none | DoS (cross-tenant) |
| S6 | LOW | dma-buf node-teardown UAF window (`dmabuf_ref_cnt` is warn-only) | importer + munmap | mem-corruption (window) |
| S7 | LOW | `nc_event_get/set` off-by-one (`>` admits `index==count`; should be `>=`) | O_RDONLY, none | mem-corruption (4B MMIO) |
| S8 | LOW | CRWL unmark `npe_notify_mark` amplification (×512) + cross-process pod mode reset (V3) | O_WRONLY misc (V3) | DoS / state-corrupt |
| S9 | LOW | `nmap_dm_special` uninitialized `bar_pa` (no `else`; not reachable on shipped tables) | mmap (latent) | mem-corruption (latent) |
| S10 | INFO | FW-IO NEW-path response CRC never validated (`dw1` unread) | firmware (TCB) | integrity (trust) |
| S11 | INFO | FW-IO legacy response size underflow (guarded; self-mitigated) | firmware (TCB) | (self-mitigated) |
| S12 | INFO | `DUMP_MEM_CHUNKS` kmalloc multiply (benign on LP64; ILP32-only wrap) | O_RDONLY, none | (non-exploitable) |
| S13 | INFO | P2P `register_va` error-as-PA (alignment-mitigated; seed NOTE-1 reversed) | in-kernel EFA peer | (self-mitigated) |
| S14 | INFO | `IS_NEURON_DEVICE_FREE_ACCESS == 1` fragile macro | n/a | latent foot-gun |

> **CORRECTION (W2-SEC-ATTACK, NOTE-1) —** S13 was originally seeded (P1-K-P2P NOTE-1) as a real defect: `neuron_p2p_register_and_get_pa` returns `(u64)-EINVAL` on failure and the caller checks `if (!pa)`, which would let the error sentinel through. Re-reading the shipped source shows the *next* check — `if (pa % PAGE_SIZE != 0) return -EINVAL;` (`neuron_p2p.c:93-97`) — rejects it, because `-EINVAL` (`0xFFFFFFFFFFFFFFEA`) is not page-aligned and no small errno value ever is. Downgraded from MED defect to **INFO / self-mitigated** (latent type-confusion smell only).

> **NOTE —** S2 and S7 are the same EVENTS ioctl; S5 and S8 the same CRWL unmark. They are listed separately because they are *distinct* bugs with distinct fixes (add `ncdev_ncid_valid`, change `>` to `>=`; add an attach gate, hoist `npe_notify_mark` out of the loop). The full per-finding reachability/mitigation/fix analysis is on the [owning page](../security/ioctl-attack-surface.md).

---

## 5. Hardware workarounds, grouped by architecture

The HW-workaround rows are the ones a reimplementer must reproduce. They are grouped here by the silicon generation whose bug they mitigate.

### V2 (Trn1 / Inf2)

**BUG-A — DMA hang in the reset window.** V2 silicon can hang an in-flight H2T DMA if a NeuronCore reset overlaps the transfer. The mitigation is the retry loop in `_ndma_memcpy_wait_for_completion` (`neuron_dma.c:378-429`), armed by `ndma_retry_memcpy=true` (`dhal_v2.c:1444`): on a wait timeout, if the op is still inside the reset window (`nr_op_in_reset_wnd`, `neuron_reset.c:366-379`), it re-stamps the start time, **rebuilds the H2T ring** (`ndmar_h2t_ring_init`), and **re-issues all descriptors** (`ndma_memcpy_chunks`), looping until success, until the op leaves the reset window, or until a sub-call errors.

> **QUIRK —** arming the retry has a capability cost: `ndma_zerocopy_supported` (`neuron_dma.c:1375-1378`) returns `!ndma_retry_memcpy || zerocopy_trn1_override`, so **zerocopy is disabled on V2** unless the `zerocopy_trn1_override` module param is set. The reset-hang workaround and zerocopy are mutually exclusive on V2 silicon — a real, documented capability gap, not an oversight. The retry loop also has no explicit cap (bounded only by the reset ending), a livelock candidate flagged for the [deep-dive backlog](deep-dive-backlog.md).

**BUG-B — aggressive single-descriptor prefetch.** The UDMA prefetch threshold must not equal the 128-entry prefetch buffer size, or the prefetcher corrupts at the boundary. V2 caps descriptors-per-packet at 64 (`V2_ALLOWED_DESC_PER_PACKET`, `v2/address_map.h:65-71`) and `ndma_init_v2` passes `64+1 = 65` to `udma_m2m_init_engine` (`dhal_v2.c:1096-1097`) — the `+1` reserves an MD descriptor; the `65 ≠ 128` keeps the threshold off the buffer size.

### V3 (Trn2) / V4 (Trn3)

V3 does **not** arm the retry (`ndma_retry_memcpy=false`, `v3:1937`) — the BUG-A silicon issue is V2-only, so zerocopy is available on V3/V4. Two V3 source warts are recorded:

- **RST-FIXME** — `nr_wait_for_reset_completion_v3` still references `V2_FW_IO_REG_FW_STATUS_OFFSET` / `…DEVICE_READY_MASK` (source FIXME at `v3:364, 371`). The constant *value* is shared and correct; only the V2-scoped *name* is wrong.
- **B1** — the V4 arch path discards the V3 base-registration return: `ret = ndhal_register_funcs_v3(); ret = ndhal_register_funcs_v4();` (`neuron_dhal.c`, V4 case). A failing V3 base registration is masked by the override call. V4-only; see [dhal-v2](../kernel/dhal-v2.md) (V4 inherits the full V3 vtable, then replaces 8 slots).

Also dead but harmless across V3/V4: `routing_id < 0` on a `u32` (always false, `v3:1177` / `v4:355`, source TODO acknowledges it), and a redundant qemu/emu branch in `fw_io_read_csr_array_v3` (`v3:910`, both paths identical).

### Firmware (TCB-internal)

The FW-IO findings (S10, S11) are inside the trust boundary — firmware already owns the device, so an unvalidated response CRC (S10, `neuron_fw_io.c:469-496`) or a guarded legacy size underflow (S11, `:376`) is not userspace-reachable. They are catalogued as robustness items, not exploitable bugs: validating the response `crc32` would catch silent MiscRAM corruption / FW faults, but their absence is a trust assumption, not a vulnerability.

---

## Cross-References

- [DMA Rings](../kernel/dma-rings.md) — owns **K-RING-2** (the mutex deadlock), the V2 BUG-A retry mechanism, BUG-B prefetch cap, and the commented-out TX/RX validation
- [Microcode Loader](../gpsimd/microcode-loader.md) — owns the **UCODE** success-on-error defect with full disasm of the `0x310810` cleanup path
- [ioctl Attack Surface](../security/ioctl-attack-surface.md) — owns the 14 security findings (S1–S14), the gate model, and the FW-IO trust items
- [Reset State Machine](../kernel/reset.md) — owns the reset-window predicate, the V2/V3 FW-status poll, the READY-bit-polarity idiom, and the GEN-01 stale `MODULE_ALIAS`
- [DHAL V2 (Trn1 / Inf2)](../kernel/dhal-v2.md) — owns the V2 DHAL callback bodies that arm BUG-A/BUG-B and the V4-inherits-V3 (B1) registration path
- [Phase-3 Deep-Dive Backlog](deep-dive-backlog.md) — the retry-loop livelock, the NDS refcount doc-vs-code reconciliation, and the kmgr-unload ordering hazard
