# Device→Host Interrupt / Notification Path + Sync/Events

The GPSIMD **Vision-Q7** core (AWS Annapurna "Cayman" Trainium tile, `ncore2gp`
config) closes a computation by telling the x86 host that work finished. This page
is the **device→host** specification of how that signal travels: the three
physically distinct delivery paths, the 16-byte notification record the device
engines write, the host ring-drain that consumes it, and the completion→wake chain
that unblocks the calling `nrt_execute` thread. It also consolidates the
synchronization-and-events seam — how an on-core op-done becomes an `EVT_SEM`
post, then a Notification-Queue (NQ) write, then a host poll.

The single most important fact, established below and consistent with the
firmware-side pages: **none of the steady-state completion traffic reaches host
userspace as a PCIe interrupt.** The hot path is a shared-memory NQ ring the host
*polls*; the only interrupt that crosses PCIe is the kernel-driver MSI-X on the
error/overflow edge. The Vision-Q7 firmware itself is **polled, not vectored**
(it parks in `waiti` and reads a latch at a dispatch boundary), so there is no
on-core vectored completion delivery to assert anywhere in this chain.

**Scope and the two-core caveat.** "Q7" names two unrelated cores. This page
concerns the **GPSIMD compute Vision-Q7** (`ncore2gp`, XEA3) and the **host x86
runtime** `libnrt.so`. The management/"Pacific" survival core (a scalar Xtensa-LX)
appears only as the *consumer* of the on-die fault summary; where it matters the
text says so.

Related pages (cross-linked, not duplicated):
[The XEA3 interrupt / exception architecture](./xea3-interrupt-architecture.md) ·
[The interrupt/exception handler bodies](./handler-bodies.md) ·
[CSR — NOTIFIC Queue](../csr/notific-queue.md) ·
[TPB Event/Semaphore Regions (EVT_SEM)](../address/evt-sem-regions.md) ·
[PEB apex / CC / TOP_SP sources](./peb-cc-topsp-triggers.md) ·
[Physical INTC instances](./physical-intc-instances.md) ·
[Host model lifecycle + error model](../../runtime/lifecycle-error-model.md) ·
[ntff trace protobuf + NEFF parse state](../../neff/ntff-trace-parse-state.md).

**Confidence legend.** The page default is `HIGH · OBSERVED`; claims that depart
from it carry an explicit tag. `HIGH` = byte-exact from disasm / CSR schema / ISA
header. `MED` = strong inference from naming + cross-file. `LOW` = plausible,
flagged. `OBSERVED` = read from a shipped artifact. `INFERRED` = reasoned.
`CARRIED` = consolidated from a named sibling page, not re-derived here.

---

## 0. Provenance — what was read

Every fact below derives **solely** from static analysis of shipped artifacts:

| Artifact | Role | Tooling |
|---|---|---|
| `libnrt.so.2.31.24.0` (x86-64, `BuildID 8bb57aba…c102e`, with `debug_info`) | the host runtime: NQ setup/drain, completion-handle poll, `/dev/neuron` boundary | `nm`/`objdump`/`strings`/`c++filt` |
| `common/aws_neuron_isa_notification.h` (sunda / mariana / maverick arch-isa) | the 16-byte `NEURON_ISA` record + `notific_type` enum + per-class bodies | header read |
| `csrs/notific/notific_10_queue.json` (Cayman arch-regs) | the device NOTIFIC block: SW-NQ rings, overflow policy, 9 interrupt outputs | CSR schema |
| `intc/tpb_triggers.yaml`, `peb_intc_triggers.yaml` | the `tpb_notific_intr_0..8` edges; the 128-input apex bit-map | trigger manifest |
| carved `CAYMAN_NX_POOL` / `Q7_POOL` DEBUG firmware (`ncore2gp`) | the producer side (`UR#0x14` publish / `UR#0x15` error latch), anchored to handler-bodies | `xtensa-elf-objdump` (carried) |

> **Per-gen applicability.** `OBSERVED` on **Cayman / NC-v3**. The NOTIFIC block,
> the `EVT_SEM` unit, and the record header are byte-identical under `mariana`,
> `mariana_plus`, and `sunda` arch dirs. **v5 / Maverick is header-OBSERVED only**
> — the `NEURON_ISA_NOTIFICATION_NBYTES = 0x10` and the `notific_type` enum are
> present in `neuron_maverick_arch_isa/`, but no v5-specific CSR schema or firmware
> ships; **treat any statement about v5 ring/CSR interior as `INFERRED CARRIED`.**

---

## 1. The three device→host paths — the spine

The device→host surface is **three** physically distinct paths. They share no
hardware below the SoC fabric, and conflating them is the standard mistake. The
distinction the rest of the page elaborates:

| | **Path 1 — iDMA → Q7** | **Path 2 — SDMA → host** | **Path 3 — NQ-notification** |
|---|---|---|---|
| | (on-core, on-die) | (MSI-X / polled) | (exec/profile rings) |
| Source | Q7 *private* iDMA (I-fetch / scratch) | SDMA queue completion / error | any engine (PE/ACT/POOL=Q7/DVE/SP) + SDMA completion + Q7 `pool_stdio` |
| Trigger | iDMA done / iDMA error | SDMA queue completion / error | 16-byte `NEURON_ISA` record written to an NQ ring |
| First aggregator | none — direct to the Q7 | leaf INTC `errtrig`-pair → `peb_intc` apex | engine `notific` CSR → NQ ring (DRAM) |
| Device delivery | **Q7 INT35 / INT36** (XEA3 vector) | `NO_MSIX`: wire-OR → apex → Q7/"Pacific" \| `MSIX`: `intc_4grp_MSIX` → PCIe MSI-X | N/A (shared-memory ring; SDMA→NQ armed host-side) |
| In `peb_intc` apex? | no | yes — idx 7..38 `se0_sdma_nmi[31−i]` + 42..73 `se1` (bit-reversed) + 2 host summaries | no (data-plane ring, not the fault bus) |
| Reaches the Q7? | **YES** (it *is* the Q7) | no — to host MSI-X / mgmt core | no — host polls the ring |
| Reaches host userspace? | no | kernel MSI-X → driver; userspace **polls** a device-DRAM word | `notification_read_exec_queue` → ring drain |
| Host handler | Cadence libidma cb + thread-unblock | `dma_wait_for_completion_handle` @`0x22def0` (copyout + usleep) | `exec_request_progress_one_step` @`0x263330` |
| Host wake | on-core thread unblock | usleep poll → `comp_efd` | ring-drain → `comp_efd` |

`[HIGH · Paths 1/2 device-side CARRIED from XEA3/handler-bodies + peb_intc apex
keystone; Path 3 host-side OBSERVED.]`

> **NOTE — the asymmetry.** `peb_intc → Q7` and the host-MSI-X fork are the
> **fault/management** spine. The NQ ring + completion-word poll is the
> **data/completion** spine. "The device told the host the inference finished" is
> a *ring drain*, not an interrupt. The error/overflow edge is the only thing that
> ever becomes a PCIe interrupt, and the **kernel** module — not `libnrt` —
> consumes it.

### 1a. Path 1 — iDMA → Q7 (on-core) `[HIGH · CARRIED]`

The Vision-Q7 fields exactly two real DMA interrupts, both **on its own core**:
`INT35 = XTHAL_INTTYPE_IDMA_DONE` and `INT36 = XTHAL_INTTYPE_IDMA_ERR` (the Q7's
private I-fetch/scratch iDMA, channel 0). These vector through the single XEA3
dispatch path; the handler runs a Cadence-libidma callback and unblocks the
waiting on-core thread. **This DMA never leaves the core and is never seen by the
host.** See [handler-bodies](./handler-bodies.md) §2 and
[XEA3 architecture](./xea3-interrupt-architecture.md) §3 — not duplicated here.

> **GOTCHA — INT35/36 are NOT the tensor mover.** The model-tensor SDMA (Path 2)
> is a *separate* engine; INT35/36 are only the Q7's own instruction/scratch fetch
> DMA. Do not read "the Q7 takes a DMA interrupt" as "the Q7 takes the SDMA
> completion." It does not — the SDMA completion never enters the Q7's vector
> table.

### 1b. Path 2 — SDMA → host (MSI-X **or** polled) `[HIGH · OBSERVED / CARRIED]`

The 254 SDMA triggers route leaf → `errtrig`-pair → `peb_intc` apex as 64
`se{0,1}_sdma_nmi[31−i]` entries (apex idx 7..38 and 42..73, **bit-reversed**:
`se0_sdma_nmi[i] ⇒ se0_sdma_{31−i}_summary`) plus 2 host summaries — see
[peb_intc apex sources](./peb-cc-topsp-triggers.md) for the byte-exact bit-map.

The `MSIX`-vs-`NO_MSIX` choice is **per physical INTC instance**, not per source
domain:

* `intc_4grp_MSIX_unit` (host-facing) carries the full PCIe MSI-X apparatus
  (128-entry vector table, PBA) and **writes MSI-X to PCIe** → the kernel driver.
* `intc_4grp_NO_MSIX_unit` has no vector table; its `nmi_out` wire-ORs **up the
  apex tree → Q7/"Pacific"** management core.

So one SDMA error can be *both* an MSI-X to the host *and* an apex summary to the
Q7 — different physical instances on the same leaf. `[HIGH · CARRIED — physical
INTC instances + intc-4group CSR delta.]`

But the **userspace** completion wait for a bare SDMA transfer is **not** an
MSI-X wake — it is a polled device-DRAM word
(`dma_wait_for_completion_handle` @`0x22def0`, byte-exact):

```
22df16:  call tdrv_get_platform_type          ; resolve completion-word location
LOOP {
  22dfa0:  call dmem_buf_copyout  @0x2299b0    ; READ the completion word back over the BAR/DMA
  22dfb9:  call usleep@plt        @0x3d310     ; back off
}  until satisfied / timeout
  22e012:  call dmem_buf_copyin   @0x229820    ; reset the word for reuse
  22e027:  call dmem_buf_copyin                ; (second reset path)
```

The allocation side
(`dma_alloc_completion_handle` @`0x22de20`) writes the *expected* value into a
device-DRAM word via `dmem_buf_copyin`; the SDMA engine's completion write-back
updates it; the host re-reads. **No eventfd, no ioctl-wait, no interrupt** in this
path. The completion word lives in the HBM/notification region
(`tdrv_arch_get_completion_handle_mem_location_cayman` returns the `0x8006800000`
band + `cayman_sdma_base`).

> **QUIRK — the vocabulary is uniformly "polling."** Strings:
> `"Error: DMA completion timeout in: %lu usec"`,
> `"Timeout polling for async exec completion on nc"`,
> `"mmap failed for async h2t dma completion queue"`. Never `"interrupt"` /
> `"wait-on-irq"`. The runtime's mental model of SDMA completion is a poll with a
> timeout, full stop.

### 1c. Path 3 — NQ-notification (exec/profile rings)

The device engines and the SDMA completion write 16-byte `NEURON_ISA` records into
per-engine **Notification Queues** (NQ rings) in SoC RAM; the host **drains** them
by reading a ring head/tail (or polling the record's phase bit) under a poll loop.
This is the steady-state exec-completion channel *and* the profile/trace/error/
throttle channel. §2–§5 specify it end to end.

---

## 2. The kernel-driver boundary — `/dev/neuron`, ioctl, mmap

`libnrt` is userspace; the PCIe MSI-X vector actually lands in the **kernel**
`aws-neuron` module, and `libnrt` reaches the device only through the character
device `/dev/neuron%d`. Observed strings: `"/dev/neuron%d"`, `"/dev/neuron"`,
`"Specified core not accessible! is /dev/neuron%u visible to process?"`.

| Symbol | Addr | Role |
|---|---|---|
| `ndl_device_ioctl` | `0xc1d20` | `cmp $0x3f,%edi` request gate → `open("/dev/neuron%u")` (lock-`cmpxchg` fd cache) → `ioctl()` |
| `ndl_mmap_bar_region` | `0xc30e0` | resolve a BAR via ioctl → `mmap` — how the NQ ring base + doorbell CSRs enter host VA |
| `ndl_notification_init` | `0xc3ce0` | 2× `ioctl()` + `ndl_memory_get_pa` — **allocate + pin** the NQ ring backing DRAM |
| `ndl_nc_semaphore_increment` | `0xc3ba0` | the host→device `EVT_SEM` increment (host doorbell; inverse of this page) |

> **CORRECTION — `libnrt` has NO `msi`/`msix`/`irq`/`isr` symbol at all.**
> `nm -C libnrt.so | rg -icw 'msi|msix|irq|isr'` returns **0**.
> The *only* imported wakeup primitive is `eventfd@GLIBC_2.7`. A reader who expects
> a userspace IRQ/MSI-X handler in the runtime will not find one — the MSI-X is
> consumed by the closed kernel module, and userspace gets either an eventfd it
> blocks on (Path 3) or a device-DRAM word it polls (Path 2). This is the fact
> that disambiguates "MSI-X exists" (true, kernel-side) from "userspace takes an
> interrupt" (false).

The one place a device-notification class is wired to a **driver-managed** channel
is `ndl_enable_throttling_notifications` @`0xc5950` (three `ioctl()` calls behind a
`NEURON_ISA` throttle gate) — the HAM/throttle/`hw_power_monitor` class (TPB
`notific` SW-queue NUM4). The host-MSI-X fork of §1b is the natural consumer of
this RAS/throttle path. `[HIGH the ioctl arming; MED the MSI-X linkage — kernel-
side, not in `libnrt`.]`

---

## 3. The 16-byte NEURON_ISA notification record

The host-visible completion/event/error record is **16 bytes**
(`NEURON_ISA_NOTIFICATION_NBYTES = 0x10`, `aws_neuron_isa_notification.h`). The
generic layout (`NEURON_ISA_GENERIC_NOTIFICATION`):

| Off | Size | Field | Meaning |
|---|---|---|---|
| `+0x00` | 2 | `metadata_0` (`uint16`) | record-class payload `[15:0]` |
| `+0x02` | 1 | `metadata_1` (`uint8`) | payload `[23:16]` |
| `+0x03` | 1 | **`header`** | `{ notific_type:5 [28:24], software_queue_overflow:1 [29], hardware_queue_overflow:1 [30], phase:1 [31] }` |
| `+0x04` | 4 | `metadata_2` (`uint32`) | payload `[63:32]` |
| `+0x08` | 8 | `timestamp` (`{u32 low; u32 high}`) | 64-bit, **fixed 1-ps unit** `[127:64]` |

`[HIGH · OBSERVED — header struct + `NOTIFICATION_TYPE_{LSB=24,MSB=28}` defines.]`

> **QUIRK — the PHASE bit is the poll primitive.** Header comment: *"All
> notifications contain a 1 bit phase bit so that software can poll on the phase
> bit."* The producer toggles `phase` per ring wrap; a consumer can detect a fresh
> record by the phase flip **without head/tail bookkeeping** — the NQ analogue of
> the SDMA gen-tag (§5.2).

> **NOTE — timestamp is 1 ps, not clocks.** *"Unlike Tonga, the timestamp counter
> is in a fixed time unit (1ps) instead of clocks."* It is snapshotted from the
> 64-bit free-running counter at NOTIFIC `+0x00/+0x04` (`timestamp_lo/hi`); the
> host scales device ticks → wall time via `cfg_timestamp_inc` (NOTIFIC `+0x04`).

### 3.1 The `notific_type` discriminator — 24 values

The 5-bit `notific_type` (header `[28:24]`) selects the record class. The enum has
**24 values** in the 5-bit space (`NEURON_ISA_NOTIFICATION_TYPE`):

| code | name | code | name |
|---|---|---|---|
| `0x02` | `DMA` | `0x0c` | `TPB_POOL_INST_START` |
| `0x03` | `ERROR` | `0x0d` | `TPB_POOL_INST_END` |
| `0x04` | `TPB_PE_INST_START` | `0x0e` | `TPB_POOL_EXPLICIT` |
| `0x05` | `TPB_PE_INST_END` | `0x0f` | `TPB_POOL_EVT_SEM` |
| `0x06` | `TPB_PE_EXPLICIT` | `0x10` | `TPB_DVE_INST_START` |
| `0x07` | `TPB_PE_EVT_SEM` | `0x11..0x13` | `TPB_DVE_{INST_END,EXPLICIT,EVT_SEM}` |
| `0x08` | `TPB_ACT_INST_START` | `0x14..0x17` | `TPB_SP_{INST_START,INST_END,EXPLICIT,EVT_SEM}` |
| `0x09` | `TPB_ACT_INST_END` | `0x1b` | `TPB_AXI_EVT_SEM` |
| `0x0a` | `TPB_ACT_EXPLICIT` | `0x1e` | `TPB_HAM` |
| `0x0b` | `TPB_ACT_EVT_SEM` | `0x1f` | `TPB_ERROR` |

The stride is **4 codes per engine** in
`{INST_START, INST_END, EXPLICIT, EVT_SEM}` order: PE `0x04`, ACT `0x08`, **POOL
`0x0c`** (the GPSIMD compute engine), DVE `0x10`, SP `0x14`.

The four *functional* NQ classes the runtime subscribes (§4.1), mapped to records:

* **TRACE / dynamic** — `INST_START`/`INST_END` (per-engine `0x04..0x17`):
  instruction-timing brackets, with `program_counter:20` (64-B IRAM resolution),
  `debug_hint:4`, `soft_reset_counter` (the inference counter), and
  `input/output_data_crc` (CRC16 of the data stream).
* **EXPLICIT** — the op-requested notify (`0x06/0x0a/0x0e/0x12/0x16`):
  `program_counter:20 + debug_hint:4 + soft_reset_counter + debug_hint_extended`.
* **EVT_SEM** — the event-semaphore crossing into the NQ
  (`0x07/0x0b/0x0f/0x13/0x17` + `AXI_EVT_SEM 0x1b`): `event_semaphore_id`,
  `update_mode`, the new `event_semaphore_value`. This is §7 (the SP
  `NOTIFY 0x10A6`) reaching the host.
* **ERROR** — the fault record (`ERROR 0x03` / `TPB_ERROR 0x1f`): see §6.

A distinct
`NEURON_ISA_INTERRUPT_NOTIFICATION` form `{group:3, block_id:7, cause_bits}` is the
"sending interrupt" record (vs the "sending notification" data record).

---

## 4. The device NOTIFIC block — the SW-NQ rings

Each TPB engine emits its records through a **NOTIFIC** APB block that coalesces
them and writes them over AXI into up to **ten** software-owned ring queues
(SW NQ) in SoC RAM. Full register map: [CSR — NOTIFIC Queue](../csr/notific-queue.md).
The facts this page depends on:

| Property | Value | Source |
|---|---|---|
| `NUM_SW_Q` | **10** (`notific_10_queue`); the sibling `notific_1_queue` has **1** | `.RegFile.Parameters[0]` |
| `NEURON_ISA_NOTIFICATION_NBYTES` | `0x10` (16 B) | ISA header |
| per-queue descriptor stride | `0x28` at base `0x100 + i·0x28` | `notific_nq` bundle |

> **QUIRK — NUM_SW_Q is 10-or-1, not a single value.** The block ships in two
> flavors: `notific_10_queue` (the full TPB engine NOTIFIC, 10 rings, reset mask
> `0x3ff`) and `notific_1_queue` (a 1-ring variant). Cite the 10-ring block for
> engine completion/trace; do not assert "10" universally.

The per-queue ring descriptor (queue *i* at `0x100 + i·0x28`) is the device-side
counterpart of the host SW-mirror §5 drains:

| +Off | Reg | acc | Role |
|---|---|---|---|
| `+0x00/+0x04` | `base_addr_lo/hi` | RW | SoC/RAM address of this ring (`PulseOnW`) |
| `+0x08` | `size` | RW | ring size **in bytes** |
| `+0x0c` | `head_ptr` | RW | **CONSUMER** ptr; software writes it after reading (drain-ACK + re-arm) |
| `+0x10` | `tail_ptr` | RO | **PRODUCER** ptr; HW-advanced on each write |
| `+0x14` | `threshold` | RW | usage above which `tpb_notific_intr_6` fires |

Overflow policy is per-ring: `sw_backpressure` (lossless stall) vs `nq_sw_overflow`
(`ignore_full_en` — lossy ring-overwrite). A write to a *disabled* ring is dropped
and raises `tpb_notific_intr_1`.

### 4.1 Arming the rings — `notification_configure`

`libnrt` sets up the rings before execution. `notification_init` @`0x2fed70` calls
`notification_configure` @`0x2fdc40` once per class; the full arm sequence:

```c
// notification_configure @0x2fdc40  (one class)
db_physical_core_get_mla_and_tpb(...);                  // resolve the physical core
nq_ids = notifications_get_nq_ids_for_type(type);       // @0x2fd160 — which HW rings
ndl_notification_init_with_realloc(...);                // @0xc3ea0 — driver ioctl: ALLOCATE ring DRAM
mem_handle = ndl_notification_get_mem_handle(...);      // @0xc4070 — device PA / handle
aws_hal_notific_nq_init(ens_nq, base, size, rec_size);  // @0x450fd0 → _cayman: program HW NQ base
aws_hal_notific_set_coalescer_stream(...);              // @0x450e50 — coalescing
ndl_notification_map(...);                              // @0xc4090 — mmap ring into host VA
ring_buffer_init(...);                                  // @0x3020b0 — host SW ring mirror
notification_enable_v2(...);                            // @0x2fd750 — turn the ring ON
```

The **type → HW-NQ-id** table (`notifications_get_nq_ids_for_type` @`0x2fd160`, a
10-way jump on `notification_type(0..9)`, byte-decoded from `.rodata`):

| type | name | NQ id set | | type | name |
|---|---|---|---|---|---|
| 0 | TRACE | `{0, 1, 2}` | | 5 | THROTTLE |
| 1 | EVENT | `{1, 2}` | | 6 | TOPSP_TRACE |
| 2 | ERROR | `{2, …}` | | 7 | TOPSP_EVENT |
| 3 | INFER_STATUS | `{4, 5, 6, 7}` | | 8 | TOPSP_ERROR |
| 4 | DMA | (distinct band) | | 9 | TOPSP_CC |

These NQ ids index the per-engine
SW-queue numbering: **NUM2** = events/semaphores, **NUM3** = errors, **NUM4** =
`hw_power_monitor`/HAM/throttle.

`notification_enable_v2` @`0x2fd750` arms the SDMA→NQ wiring:
`aws_hal_notific_nq_enable` @`0x451010` → `aws_hal_sdma_notification_apb_en`
@`0x4514d0` → `aws_hal_sdma_notification_queue_en` @`0x4513a0`, which tail-calls
`aws_reg_write_tdma_model_notific_cfg_per_queue_m2s_trigger` — **the register that
makes an SDMA queue's completion write a notification record into the NQ ring.**
So: SDMA done ⇒ NQ-ring write ⇒ host drains.

---

## 5. The host ring drain — `aws_hal_notific_nq_read_cayman`

The host learns of a completion by **reading** the ring; there is no device
interrupt in this path. `notification_read_exec_queue` @`0x2ff170` (a thin gate)
calls `aws_hal_notific_nq_read` @`0x451040`, an arch-vtable trampoline
(`lea kaena_khal; jmp *0x438(%rax)`) dispatching to `_cayman` @`0x470ec0`.

The cayman drain operates on the `aws_hal_ens_nq` SW-mirror. The field offsets are
byte-exact against the disasm at `0x470ec0`:

| Off | Field | Disasm evidence (`%r13 = ens_nq`) |
|---|---|---|
| `+0x00` | ring **SIZE** (entry count) | `mov 0x0(%r13),%eax` @`0x470f52` |
| `+0x04` | **HEAD** index (consumer) | `mov 0x4(%r13),%r9d` @`0x470f3a`; `add 0x4(%r13),%r15d` → `mov %r15d,0x4(%r13)` @`0x470fad` |
| `+0x28` | ring **BASE** (mmap'd device DRAM) | `mov 0x28(%r13),%rdx` @`0x470f68` |
| `+0x140` | needs-flush flag (`uint8`) | `movzbl 0x140(%r13),%edi` @`0x470f60` |
| `+0x148` | flush/invalidate callback (ptr) | `call *0x148(%r13)` @`0x470f91` |
| stride | 16-byte record | `shl $0x4,%rcx` @`0x470f5c`, `shl $0x4,%r9` |

`[HIGH · OBSERVED — objdump @0x470ec0..0x470fd4, every offset matched.]`

The drain algorithm, reconstructed as reimplementation-grade C pseudocode:

```c
// aws_hal_notific_nq_read_cayman @0x470ec0
// ens_nq : the aws_hal_ens_nq SW-mirror; out_buf : caller buffer; cap_bytes : byte cap;
// *consumed : cleared up front (movl $0,(%r8) @0x470ed1)
size_t nq_read_cayman(ens_nq_t *nq, void *out_buf, uint32_t cap_bytes,
                      uint32_t *consumed)
{
    *consumed = 0;                                   // movl $0,(%r8)
    if (cap_bytes <= 0xf)                            // cmp $0xf,%ecx; jbe fail
        return /* fail — need at least one 16-B record */;

    uint32_t avail = nq_available_count_cayman(nq);  // @0x470bf0 head/tail/wrap math;
                                                     //   al_abort_program on a bad ring
    uint32_t n = min(avail, cap_bytes >> 4);         // shr $0x4,%ebx; cmovbe — clamp by 16B

    uint8_t *base = nq->base;                         // +0x28
    for (uint32_t i = 0; i < n; i++) {
        uint32_t head = nq->head;                     // +0x04
        if (nq->needs_flush)                          // +0x140
            nq->flush_cb(&nq->field_0x40);            // *(+0x148) — DMA-coherency invalidate
        memcpy(out_buf, base + (head << 4), 16);      // 16-B record (shl $0x4)
        nq->head = head + 1;                          // store back to +0x04 (consumer advance)
        out_buf += 16;
    }
    *consumed = n << 4;                               // bytes consumed
    return n;
}
```

A "read" is therefore a pure shared-memory ring dequeue — head advance + `memcpy` +
an optional DMA-coherency flush. **No syscall, no interrupt.**

> **GOTCHA — `head_ptr` is the SOFTWARE-owned drain-ACK.** The device advances
> `tail_ptr` (`+0x10`, RO); the host advances `head_ptr` (`+0x0c`, RW) *and writes
> it back to the device descriptor* to re-arm the threshold interrupt (§4). The
> SW-mirror's `+0x04 HEAD` above is the host's private copy; the device-side
> `head_ptr` write is the explicit ACK. Confusing the two leads to a "head never
> moves" misread.

### 5.1 The reap loop + the thread wake — `comp_efd`

The steady-state completion loop is `exec_request_progress_one_step` @`0x263330`:
it reads the engine count (`al_hal_tpb_get_tpb_eng_count`) and calls
`notification_read_exec_queue` per engine — draining each engine's NQ ring. It is
invoked from `kmgr_sync_exec` after the host→device doorbell fires, looping until
the expected completion record appears.

The calling `nrt_execute` thread does **not** busy-spin the ring. It blocks on a
per-XU **eventfd** (`comp_efd`):

```c
// completion-wake chain (no device interrupt anywhere)
tpb_xu_base_init @0xe7cf0     ->  eventfd()  @0xe7d52           // one comp eventfd per XU
tpb_xu_sync_exec_get_pooled_comp_efd @0xe87e0  ->  read()       // the blocked execute thread waits here

// the reap thread (running exec_request_progress_one_step):
for (;;) {
    n = notification_read_exec_queue(engine);   // drain the NQ ring (§5)
    if (saw_expected_completion(records))
        write(comp_efd, &one, 8);               // SIGNAL the eventfd  <-- host-internal
}
```

`[HIGH · OBSERVED — eventfd@GLIBC_2.7 import + the comp_efd symbol family;
strings "Failed to get completion handle File Descriptor for sequence id 0x%lx",
"Failed to create completion handle file descriptor, err: %d".]`

> **CORRECTION — the device does NOT write the userspace eventfd.** The wake is
> two-stage: the libnrt **reap thread** drains the ring (observing the record),
> then signals `comp_efd` host-internally; the blocked `nrt_execute` thread wakes
> off the eventfd. There is no kernel-MSI-X→eventfd shortcut in `libnrt` (no
> `msi`/`irq` symbol exists). The kernel module's internal MSI-X→eventfd plumbing,
> if any, is in the closed `.ko` and not byte-confirmed here. `[HIGH the efd
> usage; MED that the reap thread (not a direct kernel path) is the signaler —
> strongly supported by the poll-loop structure + the no-irq-symbol fact.]`

### 5.2 Why this works without head/tail bookkeeping — the gen-tag / phase pair `[HIGH · CARRIED]`

Both the data plane and the notification plane carry a *phase* a consumer polls,
so a fresh slot is detectable by a bit-flip without shared head/tail state:

* **SDMA gen-tag** — WORD0 of each 16-B `SDMA_CME_BD_DESC` carries
  `gen-tag[24:25] = dma_ring_id`; the depth-4 ring flips `dma_ring_id` on wrap, and
  the custom-op inline path busy-polls `comp_BD byte3 & 0x3` until it equals the
  expected gen-tag. Synchronous, no NQ.
* **NQ phase bit** — the §3 `phase:1` header bit, toggled per ring wrap.

These are the same idea in two planes. The cross-engine producer/consumer ring
(PE/ACT/POOL/DVE/SP each posting a completion semaphore the successor waits on) and
the `memw; memw` doorbell publication fence are detailed in §7.

---

## 6. The error / overflow edge — the only interrupt that crosses

The steady-state record write is shared-memory (host polls). The **interrupt** is
the overflow/error edge. From `intc/tpb_triggers.yaml`, the NOTIFIC block exposes
**nine** interrupt outputs `tpb_notific_intr_0..8` (one per the 9-bit error/queue
status). The key edges:

| bit | name | condition | line |
|---|---|---|---|
| 0 | `NOTIFIC_SW_NQ_FULL_ERR_BIT` | write to a full SW NQ (lossless path); `nq_full` says which | `tpb_notific_intr_0` |
| 1 | `NOTIFIC_QUEUE_DISABLED_ERR_BIT` | write to a disabled NQ (dropped) | `tpb_notific_intr_1` |
| 4 | `NOTIFIC_AXI_WRITE_ERR_BIT` | AXI write-response error (+ `nq_error_addr`) | `tpb_notific_intr_4` |
| 6 | `NOTIFIC_SW_NQ_THLD_REACHED_BIT` | NQ usage ≥ `threshold` (re-arm on `head_ptr` write) | `tpb_notific_intr_6` |

A record that cannot be written sets the **header** `software_queue_overflow` /
`hardware_queue_overflow` bit **and** raises the edge. There are also 30
`notify_error_wr_buffer_drop_0..29` and 30 `notify_error_wr_buffer_full_0..29`
edges (a record dropped / a source buffer full).

These edges feed the engine leaf INTC summary → the `peb_intc` **128-input apex**
(the keystone: 4-group × 32-bit MSI-X INTC, `NUM_OF_TRIGS=128`) → host MSI-X (the
kernel driver) and/or the Q7/"Pacific" management core. **The overflow is the only
thing that becomes an interrupt; the record itself is a ring write.**
`[HIGH · OBSERVED — tpb_triggers.yaml; apex CARRIED from peb_intc keystone.]`

> **NOTE — apex→Q7 vector bit-encoding is firmware-owned.** The 128-input apex
> presents one rolled-up output to the Q7/"Pacific"/GIC, but the exact final
> register-encoded hop (which apex bit maps to which on-core vector) is **not** in
> the CSR schema — it is `INFERRED`, owned by the firmware. Do not assert a
> specific apex-bit→Q7-vector mapping. `[MED · INFERRED — apex output keystone.]`

The host **also** reads this edge as an INTC cause: `exec_request_progress_one_step`
calls `exec_check_intc_sw_notif_queue_overflow` @`0x2613c0` →
`aws_hal_intc_read_cause` @`0x450990`; an NQ full ⇒ `EXEC_FATAL_STATUS_SW_NQ_OVERFLOW`
⇒ `NRT_EXEC_SW_NQ_OVERFLOW` (1204). So the overflow edge is *both* an interrupt
(kernel-side) and a readable cause (host-side), complementing the ring drain.

### 6.1 The watchdog — a hung kernel surfaces as a MISSING record `[HIGH · CARRIED]`

Because the path is poll-driven, a **hung** device does not raise a signal — it
produces **no** completion record, and the host detects the absence by **timeout**.
The budget is `NEURON_RT_EXEC_TIMEOUT` → `dlr_model.exec_timeout`; when a
completion never arrives within budget, the per-NC scan sets `NRT_TIMEOUT` (5) +
`EXEC_FATAL_STATUS_TIMEOUT` and logs *"(FATAL-RT-UNDEFINED-STATE) … execution
timeout (%u ms) … waiting for execution completion notification"*. This is the
host-side mirror of the on-core SEQ self-halt (the ErrorHandler's permanent
self-loop, which never posts the completion). The host cannot see the spin
directly; it **infers** it from the missing notification + the expired timeout.
Posture is **FAIL-STOP**: log + report + operator reload. Full taxonomy:
[host model lifecycle + error model](../../runtime/lifecycle-error-model.md).
`[HIGH · CARRIED.]`

---

## 7. The synchronization seam — op-done → EVT_SEM → NQ `[HIGH · OBSERVED / CARRIED]`

This section consolidates how an on-core completion *becomes* a host-visible
record. The synchronization substrate is **polled end to end** — no userspace
interrupt anywhere, on the Q7 or across PCIe.

### 7.1 On-core: the `memw; memw` publication fence `[HIGH · CARRIED]`

The Q7 data side is local-RAM + an 8-entry write buffer with **no D-cache**; stores
drain asynchronously to AXI, so program order is *not* visibility order at the
memory port. The one ordering rule the firmware relies on is a **double** memory-
wait fence immediately before a doorbell:

```
build BD bytes at ring + tail_idx·16   (buffered stores)
memw ;                                  ; (1) drain the descriptor STORES to ring memory
memw ;                                  ; (2) order the doorbell AFTER them
write 1 -> primary inc_reg ;            ; advance the tail (one CME COPY)
write 1 -> secondary inc_reg ;
```

Without the fence, the weak store order could let the tail-increment race ahead of
the descriptor write and the consumer (SDMA engine) would fetch a stale/partial BD.
`memw` is the Q7's universal publish/serialize fence (doorbell, window switch, op
handoff). The 8 SPMD pool cores are otherwise **data-race-free by construction** —
each core's mutable data lives in a PRID-keyed hardware-disjoint DRAM aperture
(`idx = 9 + 2·cpu_id`, step `0x10000`), so there is no shared mutable state to race;
the XEA3 `L32EX`/`S32EX` exclusive monitor exists but is essentially unexercised.
`[HIGH · CARRIED — data-side memory model + concurrency/isolation.]`

### 7.2 Cross-engine: the EVT_SEM fabric

Each TPB exposes a **1-MiB `EVT_SEM` unit of 256 events + 256 semaphores** with
separate MMIO windows. For `TPB_0` (the on-die view):

| window | off-in-unit | SoC addr (`TPB_0`) | op |
|---|---|---|---|
| events | `0x0000` | `0x2802700000` | 256 edge events |
| sem READ | `0x1000` | `0x2802701000` | snapshot read |
| sem SET | `0x1400` | `0x2802701400` | set |
| sem INC | `0x1800` | `0x2802701800` | counted increment |
| sem DEC | `0x1C00` | `0x2802701C00` | decrement |

`[HIGH · OBSERVED — address_map_flat.yaml + the host address builders.]` A semaphore
increment is a single `write32` to `inc_base + 4·sem`. Full register model:
[EVT_SEM regions](../address/evt-sem-regions.md).

> **GOTCHA — two distinct EVT_SEM bases; do not conflate.** The per-TPB EVT_SEM is
> `TPB_0_EVT_SEM @ 0x2802700000`. The standalone Top-Sync-Processor EVT_SEM is
> `TOP_SP_0_EVT_SEM @ 0x8280000000` (stride `0x40000000`) — a different unit at a
> different base. The window layout (`EVENT@+0x0 / READ@+0x1000 / SET@+0x1400 /
> INC@+0x1800 / DEC@+0x1C00`) is shared, but the **base** differs, and Cayman's
> TOP_SP EVT_SEM has **no `SEMAPHORE_CNTR_INC` port**. Mixing the two bases is a
> real regression hazard.

The sequencer drives `EVT_SEM` with a small op family: `WAIT_GE_AND_DEC`
(op `0x10A0`, sub-op `0x14` — block until `sem ≥ value`, then atomically
decrement), `WAIT_GE`, `NOTIFY` (op `0x10A6` — turns a satisfied wait into an
NQ-record write), `DRAIN` (op `0x10A2` — the per-op barrier joining a POOL/Q7 op
before the next). Every TPB instruction also carries an 8-byte EVENTS block
(`wait_mode/wait_idx`, `update_mode/update_idx`, `semaphore_value:u32`) — the
per-instruction producer/consumer handshake. `[HIGH · CARRIED — EVT_SEM substrate +
per-instruction EVENTS block.]`

### 7.3 The full completion chain (one diagram)

```
device op done
  -> EVT_SEM sem-inc        write32 to (inc_base + 4·sem)         [§7.2 cross-engine join]
  -> SP WAIT_GE_AND_DEC unblocks -> NOTIFY (0x10A6)
  -> UR#0x14 publish        -> 16-B NEURON_ISA record into SW NQ  [§3/§4; phase toggled]
                               @ TPB-local NOTIFIC (NUM2 evt / NUM3 err / NUM4 ham)
  -> (steady state: NO interrupt) host reap thread:
       exec_request_progress_one_step  @0x263330
       -> notification_read_exec_queue @0x2ff170
       -> aws_hal_notific_nq_read_cayman @0x470ec0
            (SIZE+0 / HEAD+0x4 / BASE+0x28 / 16-B stride / phase poll / flush@+0x140/+0x148)
  -> reap thread signals comp_efd (eventfd)
  -> blocked nrt_execute thread wakes
  --------------------------------------------------------------------------------
  ERROR/OVERFLOW EDGE ONLY:
       tpb_notific_intr_0..8 + notify_error_wr_buffer_{drop,full}_0..29
       -> engine leaf INTC summary -> peb_intc apex (128-input MSI-X INTC)
       -> host MSI-X (KERNEL driver)  AND/OR  Q7/"Pacific" mgmt core
       (the ONLY interrupt that crosses PCIe; never the steady completion)
```

The on-core producer (`UR#0x14` publish / `UR#0x15` error latch) is firmware-side
and detailed in [handler-bodies](./handler-bodies.md); the silicon serialization of
the `UR#0x14` WUR pair into a ring write is HW-internal (`INFERRED`). The host-side
half of this chain (`0x263330` → `0x2ff170` → `0x470ec0`) is all OBSERVED above.

---

## 8. Confidence ledger & what is inferred `[self-audit]`

**HIGH / OBSERVED (against `libnrt.so` BuildID `8bb57aba…`):**

* every host symbol address matches `nm`: `notification_read_exec_queue 0x2ff170`,
  `aws_hal_notific_nq_read 0x451040`, `_cayman 0x470ec0`,
  `exec_request_progress_one_step 0x263330`,
  `dma_wait_for_completion_handle 0x22def0`,
  `notifications_get_nq_ids_for_type 0x2fd160`, the `ndl_*` boundary, the
  `tpb_xu`/`comp_efd` family, `exec_check_intc_sw_notif_queue_overflow 0x2613c0`,
  `aws_hal_intc_read_cause 0x450990`.
* the **NO `msi`/`msix`/`irq`/`isr` symbol** fact (`nm -C | rg -icw` = 0) and the
  `eventfd@GLIBC_2.7`-only wakeup import.
* the `aws_hal_notific_nq_read_cayman` ring offsets (`SIZE+0x00 / HEAD+0x04 /
  BASE+0x28 / flush-flag+0x140 / flush-cb+0x148 / 16-B stride`) — disassembled and
  matched instruction-by-instruction at `0x470ec0`.
* `dma_wait_for_completion_handle` = `dmem_buf_copyout` + `usleep` POLL +
  `dmem_buf_copyin` reset — disassembled at `0x22df16..0x22e027`.
* the 16-B record header (`notific_type:5 / sw_ovf / hw_ovf / phase`), the **24**
  `notific_type` values, `NEURON_ISA_NOTIFICATION_NBYTES = 0x10`, the 1-ps
  timestamp, the per-class bodies — read from `aws_neuron_isa_notification.h`.
* the NOTIFIC block (`NUM_SW_Q 10`-or-`1`, the `0x28`-stride descriptor, the 9
  `tpb_notific_intr` edges) — from `notific_10_queue.json` + `tpb_triggers.yaml`.

**HIGH / CARRIED (byte-exact in a named sibling, re-stated not re-derived):** the
`peb_intc` 128-input apex + the SDMA `[31−i]` bit-reversal; the `MSIX`-vs-`NO_MSIX`
per-instance split; the iDMA INT35/36 on-core handling; the XEA3 (not XEA2) core
model; the EVT_SEM unit layout + the two distinct bases; the `memw; memw` doorbell
fence + DRF-by-construction; the FAIL-STOP watchdog/timeout posture.

**MED / INFERRED (flagged inline):** the exact apex-bit → Q7-vector mapping
(firmware-owned, not in CSR); that the libnrt **reap thread** (not a direct kernel
MSI-X→eventfd) signals `comp_efd`; the host-MSI-X consumer being the throttle/RAS
class; the silicon serialization of `UR#0x14` into a ring write.

**v5 / Maverick:** header-OBSERVED only (`NBYTES = 0x10` + the `notific_type` enum
ship in `neuron_maverick_arch_isa/`); **any v5 ring/CSR/firmware interior is
`INFERRED CARRIED`** — no v5-specific schema or image was consulted.
