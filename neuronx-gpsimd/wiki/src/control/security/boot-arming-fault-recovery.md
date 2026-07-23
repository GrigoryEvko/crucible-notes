# Boot-Arming + Device Fault Recovery

This page documents the **two operational sequences that bracket** the GPSIMD
security model: the **arming** that turns the lax-out-of-reset SoC-fabric perimeter
into a live firewall, and the **fault→recovery** state machine that runs once a
fault fires. The static perimeter register map — *what* each block enforces in
steady state — is owned by [The SoC-Fabric Perimeter](soc-fabric-perimeter.md); the
SEQ fault handler's instruction-exact internals are owned by
[SEQ Error-Handler / Fault Reporting](../../firmware/seq/error-handler.md). This
page owns the **transitions between states**: the boot arming order (RESET → ARMED),
and the on-core fault flow (TRIGGER → REPORT → HALT-or-CONTINUE → device→host
record), plus the separate fabric-fault recovery loop that targets the management
core.

Two facts drive everything below, and they are *opposite* in spirit:

- **Security is firmware-configured, not reset-default-secure.** Out of reset, four
  of the five perimeter blocks are open/off; exactly one boots fail-closed. The
  device is **born permissive** and is hardened by a privileged controller before
  it is exposed to an untrusted PCIe host.
- **Fault posture is fail-stop.** Every on-core fault except a single recoverable
  class (FP arithmetic) is a permanent halt-and-report. There is no retry, no
  fault-tolerant continuation, no watchdog-driven restart of the faulting engine —
  the engine spins until reset, having published *why* it died.

Everything below is **byte-pinned to a shipped artifact**. The firmware
anchor is the carved `CAYMAN_NX_POOL_DEBUG` device image extracted from
`libnrtucode.a` (`iram.bin` SHA-256 `8e4412b9…`, `dram.bin` `7bdf6ed7…` — the same
hashes the [error-handler](../../firmware/seq/error-handler.md) page anchors to),
disassembled with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Cairo
µarch, Xtensa24, `IsaMaxInstructionSize=32` FLIX/VLIW). The perimeter register facts
are read by `jq`/`rg` straight from the shipped Cayman arch-regs
(`csrs/sprot/{nsm,amzn_remapper,user_remapper,qos_prot}.json`,
`sprot/block_{host,internal}_access.yaml`, `intc/{pcie,peb_intc}_triggers.yaml`).

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

> **NOTE — generation coverage.** Every register reset value and every firmware
> address on this page is read from the **Cayman** authoritative artifacts
> (`cayman_golden_tapeout_candidate_2`). The arming *posture* (amzn fail-closed /
> user fail-open / NSM bypassed / qos shaper off) is a frozen multi-gen contract
> verified Sunda→Cayman→Mariana. The **v5/Maverick** image is **header-OBSERVED
> only** — its interior register strides and firmware bodies are **INFERRED** by
> structural analogy and flagged inline where they appear.

---

## 1. The two planes, stated once

There are **two distinct cores** and **two distinct fault planes**, and conflating
them is the single most reconcile-prone framing on this page. State it once:

| Plane | Core | What it handles | Where the fault goes |
| --- | --- | --- | --- |
| **Job-fault** (§5) | COMPUTE Q7 + NX SEQ | bad opcode, div0, illegal sub-op, FP, HW exception, assert, Q7 exception | **self-halt + self-report to the HOST** (UR#`0x15` + notification ring + `'S:'` ring) |
| **Fabric-fault** (§6) | "Pacific" management Q7 | NSM AXI timeout, remapper deny, NTS no-target, PCIe link events | **critical apex IRQ to the MANAGEMENT core** → isolate / drain / reset / re-arm |

The job-fault plane is the engine reporting *its own* fault upward to the host
runtime; the fabric-fault plane is the management core *recovering the fabric*
after a misbehaving host master trips the perimeter. The **arming** sequence (§2–§4)
is what makes the fabric-fault plane live in the first place — until the privileged
controller arms the perimeter, the bottom row's triggers do not fire. **HIGH
structure** — carried from the layered model in
[Trust Chain / Threat Model](trust-chain-threat-model.md).

---

## 2. The lax reset posture — what is NOT armed at power-on

`jq` over the Cayman `csrs/sprot/*.json`, every reset value byte-read:

| Block / register | Offset | Reset | Posture at power-on |
| --- | --- | --- | --- |
| `nsm` `bypass.enable` | `+0x4` `[0]` | `0x1` | **NSM BYPASSED** — no AXI-protocol policing |
| `nsm` `cfg_auto_iso.{err,long,short,spurious,timeout}` | — | all `0` | no cause auto-isolates the interface |
| `nsm` `cfg_isolation_enter.count` | `[15:0]` | `0` | zero debounce (isolate immediately if armed) |
| `qos_prot` `control.chicken` | — | `0x1` | **shaper OFF** (all LFSR/limiter/fairness enables = `0`) |
| `qos_prot.nts` `control.enable` | `[1]` | `0x0` | no-target responder SW-disabled |
| `user_remapper` `user_cam_pass_on_miss` rd`[4]`/wr`[0]` | `+0x0` | `0x1` / `0x1` | **guest CAM miss = PASS → fail-OPEN** |
| `amzn_remapper` `amzn_cam_pass_on_miss` rd`[4]`/wr`[0]` | `+0x0` | `0x0` / `0x0` | **priv CAM miss = DENY → fail-CLOSED** (the ONE armed block) |
| `amzn_remapper` `master_prot` ar/awprot | `+0x10` | `0x2` / `0x2` | AxPROT = non-secure privileged data |

So **exactly one** of the five perimeter blocks (`amzn_remapper`) boots secure;
NSM, qos/NTS, and the guest `user_remapper` are all open/off. The device is born
permissive and hardened by firmware — every reset value byte-read by `jq`.

> **QUIRK — fail-closed amzn boots a deny-all that firmware must *open*.** Because
> `amzn_cam_pass_on_miss = 0` *and* the CAM is empty at reset, the privileged plane
> denies **everything** at power-on. The first job of arming is not to *close* the
> amzn block (it is already closed) but to **program its whitelist** so the
> privileged paths firmware itself needs are allowed. The amzn block is the one that
> arms *itself* by reset; the rest of arming opens controlled holes in it and closes
> the other four blocks.

---

## 3. Who arms it — the trust transition

The arming agent is the **privileged firmware controller** reaching the sprot CSRs
on the secure PEB_APB_IO plane — i.e. the "Pacific" Q7 **management** core / its
boot firmware, **not** the PCIe host and **not** a guest. The trust boundary that
keeps the host out is enforced **twice over**:

1. **PLANE gating.** The host's PCIe AXI master traffic reaches only the
   APB_IO(user) plane the `user_remapper` gates; the privileged sprot *control*
   registers live on the PEB_APB_IO(secure) plane the fail-closed `amzn_remapper`
   gates. A guest cannot even *address* the arming CSRs. **HIGH/OBSERVED** — carried
   from [The SoC-Fabric Perimeter](soc-fabric-perimeter.md).
2. **CSR-BUS policy.** `sprot/block_internal_access.yaml` → `_sprot_allow: False`
   (deny-all); `sprot/block_host_access.yaml` → `_sprot_allow: False` **except**
   `p_0.apb.misc_ram._sprot_allow: True`. So the host's CSR protocol reaches **only
   MISC_RAM**; the perimeter's own control registers are register-bus-gated away
   from the host. The arming writes therefore originate from the privileged internal
   controller.

The **supervisor hooks** confirm the asymmetry: the `amzn_remapper` holds
management hooks *over* the guest CAM — `user_cam_ctl` `@+0x20` (`cam_clr` WIPE),
`user_cam_wr_byp_id` `@+0x30` / `user_cam_rd_byp_id` `@+0x34` (a trusted 10-bit
AXI-ID whose traffic bypasses the guest policy), `addr_denied_lo/hi` `@+0x60/+0x64`
(the `[57:0]` denied-address violation latch), `axi_rd/wr_timeout` `@+0x50/+0x54`.
The privileged twin **supervises** the guest twin: the guest may program
`user_cam`, but firmware holds the wipe + the bypass-ID + the
fail-open→fail-closed switch over it.

> **CORRECTION — there is NO global perimeter-lock bit.** A reimplementer expecting
> a write-once `MPU_LOCK`-style latch will not find one. `rg` over the sprot JSONs
> returns **zero** perimeter-wide lock/freeze fields. The perimeter is "locked" by
> the **combination** of `{amzn fail-closed CAM populated, NSM bypass cleared, user
> CAM tightened}` *plus* the CSR-bus deny-all policy that keeps the host out of the
> arming registers. The "lock" **is** the trust boundary (only the privileged plane
> can write these), not a hardware latch. **HIGH** for the facts; **MED** for
> "no global lock" (a claim by absence). This is also why a compatible host runtime
> **cannot arm the fabric itself** — it must rely on the management firmware having
> already run §4.

---

## 4. The arming sequence (C pseudocode)

The register **writes** are each OBSERVED (the registers and their lax reset values
are byte-read); the precise **order** between blocks is **INFERRED** from the
steady-state dependencies plus the *one* place the firmware order is documented
verbatim — the NSM recovery drain (§6c). A consistent, safe arming order, with the
rationale that fixes it:

```c
/* Runs on the privileged "Pacific" management core / boot firmware, over the
 * secure PEB_APB_IO plane. The host CANNOT execute this — its CSR protocol
 * reaches only MISC_RAM (sprot deny-all). [HIGH writes / MED order] */
void arm_perimeter(void)
{
    /* STEP A — PROGRAM THE FIREWALL TABLES BEFORE OPENING THE GATE.
     * amzn already boots fail-closed with an EMPTY CAM => deny-all. Until the
     * CAM is populated, EVERYTHING is denied; programming it is what OPENS the
     * privileged paths firmware needs. The CAM is INDIRECT (wr_buf_0..5):     */
    for (int n = 0; n < n_priv_entries; ++n) {
        amzn.wr_buf_0 = cmp_addr[57:0];          /* match address              */
        amzn.wr_buf_1 = cmp_addr_mask[57:0];     /* match mask                 */
        amzn.wr_buf_2 = remap_addr[45:0];        /* optional remap target      */
        amzn.wr_buf_3 = id[25:16];               /* 10-bit AXI-ID  (Cayman)    */
        amzn.wr_buf_4 = flags;                   /* valid|wr_pass|rd_pass|...  */
        amzn.wr_buf_5 = (n << 24) | misc;        /* wr_index[31:24]=N: COMMIT  */
    }
    amzn.bound_chk_cfg   = SPAN_CAP;             /* parallel checkers          */
    amzn.txn_len_chk_cfg = BURST_CAP;            /* (Cayman-NEW over Sunda)    */
    amzn.axi_rd_timeout  = RD_CAP;  /* @+0x50 */
    amzn.axi_wr_timeout  = WR_CAP;  /* @+0x54 */
    /* leave amzn_cam_pass_on_miss = 0 (fail-closed; already armed by reset)   */

    /* STEP B — ARM THE NSM WATCHDOG. Configure FIRST, clear bypass LAST, so the
     * monitor never goes live before its timers/injection/policy are set.     */
    nsm.cfg_req_threshold = ...; nsm.cfg_req_tick = ...;  /* request-stall timer */
    nsm.cfg_rsp_threshold = ...; nsm.cfg_rsp_tick = ...;  /* response-latency    */
    nsm.wr_cfg_1.axi_bresp = 0x2;                /* SLVERR inject (reset 0x2)  */
    nsm.rd_cfg_1.axi_rresp = 0x2;
    /* rd.error_data_0..7 already 0xDEADBEEF at reset — the 256-bit poison      */
    nsm.cfg_auto_iso = {err,long,short,spurious,timeout}; /* reset all 0: SET   */
    nsm.cfg_isolation_enter_count = DEBOUNCE;    /* reset 0 = isolate immediately */
    nsm.bypass.enable = 0;        /* *** THE ARM: reset 0x1 -> 0 *** @+0x4[0]   */

    /* STEP C — TIGHTEN THE GUEST (user_remapper) LAST, after the legitimate host
     * paths are whitelisted (closing it first would deny the host).           */
    program_user_cam_or_let_guest_do_it();
    user.user_cam_pass_on_miss = 0;  /* OPTIONAL: 0x1 -> 0 (fail-open->closed)  */
    /* OPTIONAL: user_cam_wr/rd_byp_id for a trusted DMA master; cam_clr to wipe */

    /* STEP D — ARM THE qos/NTS RESPONDER (if used). The qos shaper (chicken=1)
     * is a THROTTLE, not a security gate — armed only if QoS is desired.       */
    nts.control.enable = 1;          /* reset 0x0 -> 1 (no-target responder)    */
    /* nts read/write_response already 0x2 SLVERR; read_data already 0xDEADBEEF */
    /* nts_isolation slice/timeout ctl: arm the recovery slice resets          */
}
```

The ordering rationale — the part that is **MED/INFERRED**:

- **amzn CAM first** because the empty fail-closed CAM denies the very paths the
  rest of arming uses.
- **NSM bypass cleared last** within Step B so the timers/injection/auto-iso are all
  configured before the monitor begins policing the 9 protocol-shape causes inline.
- **user CAM tightened last** overall because flipping the guest to fail-closed
  before whitelisting the legitimate host paths would deny the host.

The one **HIGH/OBSERVED** ordering fact is the recovery drain (§6c), whose verbatim
register description *("after entering isolation mode, before resetting the
iofabric")* anchors the inference that this firmware respects an explicit
configure-before-activate discipline.

> **GOTCHA — the AxPROT default is non-secure.** `master_prot ar/awprot` boots
> `0x2` = *non-secure* privileged data, not secure. Arming does not flip the device
> into an ARM TrustZone "secure" mode — there is no secure monitor here (the
> arming controller is **plain privileged firmware**, confirmed by the dead
> `SECMON_SYSCALL` path elsewhere). A reimplementer must not expect AxPROT `[1]`
> (the NS bit) to be cleared by arming; the privilege boundary is the **plane +
> CSR-bus policy**, not the AxPROT secure bit. **HIGH/OBSERVED** for the reset;
> **MED** for the "no secure monitor" framing (carried).

---

## 5. The device fault→recovery state machine (job-fault plane)

This is the on-core path: a SEQ or Q7 fault → log → build record → raise → **halt
or continue** → device→host record. The instruction-exact bodies of the SEQ
handlers live on [SEQ Error-Handler](../../firmware/seq/error-handler.md); this
section assembles them into **one** state machine and adds the boot-time
exception-handler install and the Q7 self-halt path that the error-handler page does
not own.

### 5a. The boot install — how HW exceptions reach the SEQ handler

Before any fault can route, boot firmware installs the SEQ exception handler. Two
distinct install paths arm two distinct fault surfaces:

| Install site | What it arms | Coverage |
| --- | --- | --- |
| `register_exception_handlers` `@0x26ac` | the **Xtensa HW-exception** handler `0x1a64` | **causes 1, 3, 4 only** |
| `register_signal_handlers` `@0x13fa8` | the **POSIX-signal** catch-all `0x14014` | the 6-signal table `{6,2,4,8,0xb,0xf}` |
| `enable_fp_exceptions` `@0x13e98` | the **FP exception-enable** bits (`wur.fcr`) | FP arithmetic traps → `HandleFPError` |

The HW-exception install at `0x26ac`:

```text
000026ac <register_exception_handlers>:
    26ac:  366100      entry  a1, 48
    26af:  b40000      const16 a11, 0
    26b2:  b4641a      const16 a11, 0x1a64      ; a11 = SEQ exc handler 0x1a64
    26b5:  a2a001      movi   a10, 1            ; *** cause 1 ***
    26b8:  c2a000      movi   a12, 0
    26bb:  a5bb19      call8  0x1c274           ; install(cause=1, handler=0x1a64)
    ...
    26db:  0c3a        movi.n a10, 3            ; *** cause 3 ***
    26df:  65b919      call8  0x1c274           ; install(cause=3, handler=0x1a64)
    ...
    26ff:  0c4a        movi.n a10, 4            ; *** cause 4 ***
    2703:  25b719      call8  0x1c274           ; install(cause=4, handler=0x1a64)
    271e:  900000      retw                     ; only THREE causes registered
```

Exactly **causes 1, 3, 4** are
overridden with the SEQ handler `0x1a64`; the function `retw`s after cause 4. The
SEQ handler `0x1a64` reads the 12-bit `EXCCAUSE` and tail-calls the early-FATAL
emitter:

```text
00001a64 <seq_exc_handler>:
    1a64:  366100      entry  a1, 48
    1a6f:  2862        l32i.n a2, a2, 24        ; load the exception frame's cause word
    1a71:  2020b4      extui  a2, a2, 0, 12     ; a2 = EXCCAUSE[11:0]
    1a7b:  650000      call8  0x1a80            ; -> early-FATAL emitter (code 'B')

00001a80 <early_fatal_emit>:
    1a80:  366100      entry  a1, 48
    1a85:  4c22        movi.n a2, 66            ; *** error code 'B' (0x42) ***
    1a8a:  653012      call8  0x13d90           ; get_block_id (which engine)
    ...                                          ; pack block_id<<4 | code-nibble
    1ac4:  a53312      call8  0x13e00           ; FATAL raise(sev=2) -> Halt -> SPIN
```

So an Xtensa HW exception in cause class **{1,3,4}** raises code **`'B'` (0x42)** and
hard-faults. `0x1a80` ends at `call8 0x13e00`, the FATAL raise
wrapper, exactly as the [error-handler](../../firmware/seq/error-handler.md) page's
`0x1a80` "early emitter, code `'B'`" row records.

> **CORRECTION — only three HW-exception causes get the SEQ override; the rest fall
> to the XTOS default.** A reimplementer scanning for a full per-cause dispatch
> array will not find one. `register_exception_handlers @0x26ac` overrides **only**
> causes 1, 3, 4 (→ `0x1a64` → `0x1a80` early-FATAL code `'B'` → `0x13e00` raise →
> spin). The other classes (causes **0, 2, 5, 6, 7, 8**) are left on the **XTOS
> default handler `0x1c2ac`**, not on a SEQ override. The dispatch is **polled, not
> vectored** for the async surface, and the HW-exception surface is **partial
> override + library default**, not a complete custom table. **HIGH/OBSERVED** for
> the three overrides; **CARRIED** for the XTOS-default routing of the remaining
> causes and the polled/XEA3 framing
> ([XEA3 Interrupt Architecture](../interrupt/xea3-interrupt-architecture.md)).

The **POSIX-signal** install and the FP-enable install are owned by the
[error-handler](../../firmware/seq/error-handler.md) page (§4e / boot wiring) and not
re-derived here; the signal table `{6,2,4,8,0xb,0xf}` @ DRAM `0x83b70` (SIGABRT,
SIGINT, SIGILL, SIGFPE, SIGSEGV, SIGTERM) is byte-exact.

### 5b. The six entry points → the binary policy

All six fault entry points converge on the same three-stage pipeline (log → build →
raise) and then split on a **single severity immediate**: `movi a10,1` (recoverable,
`0x13e30`, `retw.n`) vs `movi a10,2` (fatal, `0x13e00`, infinite `j 0x13e14`).
At `0x13e00`:

```text
00013e00 <raise_FATAL sev=2>:
    13e00:  366100      entry  a1, 48
    13e07:  a2a002      movi   a10, 2            ; *** severity 2 = FATAL ***
    13e0a:  e50000      call8  0x13e18           ; raise_error (wur UR#0x15 + report)
    13e0d:  254df6      call8  0xa2e0            ; Halt-dispatch (Setup-Halt + Entering HALT)
    13e14:  06ffff      j      0x13e14           ; *** PERMANENT SELF-LOOP ***

00013e18 <raise_error>:
    13e20:  222102      l32i   a2, a1, 8         ; a2 = packed record
    13e23:  20 15 f3    wur    a2, UR#0x15       ; *** host-pollable TIE error latch ***
    13e29:  6562f6      call8  0xa450            ; report sink (host notify dispatch)
```

The six classes and their policy — the **only** recoverable class is FP arithmetic:

| Entry point | Address | Trigger | Code | Policy |
| --- | --- | --- | --- | --- |
| `HandleIntDivZero` | `0x13f34` | `QUOS/QUOU/REMS/REMU` divisor = 0 | `2` | **FATAL** spin |
| `HandleBadOpcode` | `0x13f58` | dispatch-table default (unknown opcode) | `0` | **FATAL** spin |
| `HandleIllegalInstr` | `0x13f80` | valid opcode, unsupported sub-op | — | **FATAL** spin |
| `HandleFPError` | `0x13eb0` | FCR-trapped FP arithmetic | FP-mapped | **RECOVER** (`retw.n`) |
| `signal_handler` | `0x14014` | any of the 6 POSIX signals (HW-exception bridge) | `'A'`=`0x41` | **FATAL** spin |
| `seq_exc_handler` → `early_fatal_emit` | `0x1a64`→`0x1a80` | Xtensa HW exception cause {1,3,4} | `'B'`=`0x42` | **FATAL** spin |

> **NOTE — two FATAL emitters, two codes, one wrapper.** The signal bridge
> (`0x14014`, code `'A'`) and the HW-exception bridge (`0x1a80`, code `'B'`) are
> **distinct** emitters that build the *same* record shape via the *same*
> `get_block_id` packing and both terminate at the *same* FATAL wrapper `0x13e00`.
> They differ only in the code byte — `'A'` = signal-delivered, `'B'` = HW-exception
> early-emit. A host runtime distinguishes them by that byte.

The fatal egress, after raise: `0xa2e0` Halt-dispatch → `0x1cf8` Setup-Halt (saves
the resume PC, logs `"S: Setup Halt"`) → `0x3a44` Entering HALT (logs `"S: Entering
HALT"`, writes the halt CSR `@0x80400`) → return to `j 0x13e14` spin. The engine is
**dead until reset**, with the resume PC saved for post-mortem.
The instruction-exact bodies are on the
[error-handler](../../firmware/seq/error-handler.md) page (§6).

### 5c. The COMPUTE Q7 self-halt path (distinct TU)

The 8 GPSIMD **compute** Q7 cores run their **own** exception handler in the Q7-POOL
firmware image — not the SEQ ErrorHandler. The DEBUG DRAM strings name it:
`exception.hpp:172 !ret_val`, `"GPSIMD EXCEPTION OCCURRED: "`, `"ILLEGAL
INSTRUCTION"`, `"DIVIDE BY ZERO"`, `"GENERIC GPSIMD EXCEPTION"`, `"Exception PC:
0x"`. A compute-core fault is caught by `exception.hpp`, classified (ILLEGAL / DIV0 /
GENERIC), the **Exception PC** captured, and the core **self-halts and self-reports**
(the `!ret_val` assert at line 172 is the fail-stop). The Q7 maps to a `TPB_ERROR`
record with a `Q7_ERROR_SUBTYPE` `{BAD_VERSION 0x08, ASSERT 0x40, UNKNOWN 0x7f}`.
**HIGH/OBSERVED** for the strings + enum; **MED/INFERRED** for the exact per-cause
subtype byte.

Both planes depend on the **boot sentinel**: the POOL ucode self-identifies by
writing `0x6099CB34` to `DRAM[0]` — LE `34 cb 99 60` at
offset 0 of the carved DRAM blob; the host claims the core via
`nrtucode_core_on_ucode_booted @0x9b0ab0`. The fault machine cannot run until this
boot-ready handshake completes.

### 5d. The device→host record + the three channels

Every fault is published as a **16-byte NEURON_ISA `TPB_ERROR` notification**
(re-read from the clean ISA header `aws_neuron_isa_notification.h`). The
discriminator `notific_type` is `ERROR (0x03)` or `TPB_ERROR (0x1f)`; the `error_id`
is `SEQUENCER_FATAL (0x0a)` or `SEQUENCER_NONFATAL (0x09)`; the subtype ties the §5b
firmware codes to the host record:

| §5b firmware path | `SEQUENCER_ERROR_SUBTYPE` |
| --- | --- |
| `HandleBadOpcode` (code 0) | `BAD_OPCODE 0x00` |
| `HandleFPError` | `FP 0x01` (+ FP mask `{INVALID 0x01, DIVZERO 0x02, OVERFLOW 0x04, UNDERFLOW 0x08, INEXACT 0x10}`) |
| `HandleIntDivZero` (code 2) | `INT 0x02` (mask `DIVZERO 0x02`) |
| `signal_handler` (`'A'`=0x41) | `SIGNAL 0x41` |
| `seq_exc_handler` (HW exc, `'B'`=0x42) | `EXCEPTION 0x42` (`{Type:4, Cause:4}`) |
| `assert_fail` | `ASSERT 0x40` |
| §5c Q7 `exception.hpp` | Q7 `ASSERT 0x40` / `BAD_VERSION 0x08` |
| loader version mismatch | `BAD_VERSION 0xb0` (seq) / `0x08` (q7) |

The record reaches the host on **three** channels (the device WRITE is OBSERVED; the
host READ binding is owned by
[the runtime error model](../../runtime/lifecycle-error-model.md), RT-07):

1. **TIE `UR#0x15` (21)** — the packed record, written by `raise_error` at
   `0x13e23` (`wur a2, UR#0x15`); host-pollable.
2. **The per-TPB error-trap notification ring** (`errors_NT`) → the 16-byte record →
   **MSI-X** → host, drained into the host `notification_t.exec_error_ring`.
   Owned by [Device→Host Notification](../interrupt/device-host-notification.md)
   (#945).
3. **The host stdout/stderr SPSC `'S:'` printf ring** — the human-readable
   `"S: ErrorHandler : …"` / `"S: Entering HALT"` lines via `0x18b84`.

The recoverable-vs-fatal split lets the host distinguish "engine alive, reported an
FP fault" (sev 1, `retw.n`) from "engine hung" (sev 2, self-loop) by the severity
byte alone. **HIGH** for the device writes + the enum; **MED** for the host reads.

---

## 6. The fabric-fault plane → the "Pacific" apex fast-path

The §2–§4 perimeter's faults flow on a **separate** plane from §5: they target the
"Pacific" **management** Q7, not the host runtime. This is the recovery loop that
runs once a host master trips the now-armed perimeter.

### 6a. The PCIe isolation state machine

`intc/pcie_triggers.yaml` → `reset_handshake_intr`:

| Bit | Trigger |
| --- | --- |
| `[8]` | `isolation_sm_linkdown_detected` |
| `[9]` | `isolation_sm_flr_detected` |
| `[10]` | `isolation_sm_sbr_detected` |
| `[11]` | `isolatio_sm_nsm_axi_timeout_detected` *(verbatim "isolatio" typo preserved)* |
| `[12]` | `isolation_sm_nts_axi_timeout_detected` |
| `[13]` | `isolation_sm_pir_detected` |
| `[14]` | `isolation_sm_isolation_mode_enter` |
| `[15]` | `isolation_sm_isolation_mode_exit` |

State progression (the bits/causes are **OBSERVED**; the transition arrows are
**INFERRED**):

```text
NORMAL
  | cause [8..13] fires AND its enter_iso_on_*/cfg_auto_iso bit is set
  v
DEBOUNCE  (count cfg_isolation_enter.count)
  v
ISOLATED  (enter[14]: stop new txns; NSM injects SLVERR/DECERR + 0xDEADBEEF;
           NTS drains absent targets)
  v
RESET/DRAIN  (the §6c VERBATIM order: reset_staging_fifo{aw,w,ar} -> reset iofabric;
              nts_isolation slv/mstr_slice_reset)
  v
CFG_CLEAR  ({wr,rd}.cfg_clear.errors clear the sticky causes)
  v
EXIT (exit[15]) -> NORMAL
```

This recovery path is **host-PCIe only** — d2d carries none of these triggers.
**HIGH** for the inputs/states; **MED** for the arrows.

### 6b. The critical apex IRQ to "Pacific"

NSM's AXI timeout reaches the management core by two paths; the direct one is the
critical fast-path. `intc/peb_intc_triggers.yaml`:

```yaml
- trigger: intr_peb_nsm_axi_timeout
  name: intr_peb_nsm_axi_timeout
  description: PEB SPROT NSM timeout or error interrupt
  edge_triggered: false      # LEVEL
  nmi_mask: 0
  nmi_msix_mask: 0
  critical: 1                 # *** the critical fast-path bit ***
```

The 128-input PEB_INTC apex = **96 summary rollups + 32 critical** fast-path bits.
The firmware cause binding is
`aws_intc_critical_cause_peb_intc_intr_peb_nsm_axi_timeout = 111` (Cayman/Mariana;
**96** on Sunda's 97-wide apex). **HIGH/OBSERVED** for the apex bit + the FW cause
number.

> **NOTE — the apex→Q7/GIC vector hop is INFERRED.** The final hop from the
> apex-pending bit to the "Pacific" Q7 / GIC vector is firmware/HW-owned and **not
> register-encoded** — an explicit non-claim inherited across the interrupt Part.
> A GIC exists in the SoC; the exact apex→GIC wiring is not visible in the config
> DB. **LOW** for the hop; everything upstream of it (the apex bit, the critical
> flag, the cause number) is **HIGH/OBSERVED**. See
> [XEA3 Interrupt Architecture](../interrupt/xea3-interrupt-architecture.md).

### 6c. The recovery teardown order (the one VERBATIM ordering)

The single place the firmware order is documented **verbatim** in the artifact is the
NSM recovery drain. The `jq` `reset_staging_fifo` descriptions:

```text
control.reset_staging_fifo [0]aw [1]w [2]ar   (each rst=0, RW)
  "clear <aw|w|ar> bus staging fifo. This reset should be done AFTER entering
   isolation mode, BEFORE resetting the iofabric."
```

So the recovery order is **fixed by the hardware contract**: (1) enter isolation,
(2) drain the AW/W/AR staging FIFOs via `reset_staging_fifo`, (3) reset the
iofabric, (4) `cfg_clear.errors` clears the sticky causes, (5) re-arm (§4). This is
the teardown the isolation SM (§6a) hands off to.

### 6d. The most-severe fabric outcome — abort freeze

On the fabric side, an **Abort-severity** errtrig classification drives the per-block
`Sunda` `abort_cntl` bundle at `0x300`: local/remote **SCAN-DUMP** (capture flop
state), **CLOCK-STOP** (gate block clocks), **SRAM-WRITE-PROTECT** (lock SRAM for
post-mortem). The Abort wire-OR is gated by `int_abort_msk_grp @0x30` — **RW** in the
`no_msix` (privileged) error-trigger, **RO** in the host-facing `msix` twin, so
abort-freeze is a privileged, management-driven post-mortem. This is the perimeter's
freeze — **distinct** from the Q7/SEQ self-halt of §5. **CARRIED HIGH** — owned by
[Abort / Scan-Dump / Clock-Stop Control](../interrupt/abort-scandump-clockstop.md)
(#940).

---

## 7. The unified picture — one diagram

```text
=== JOB-FAULT PLANE (COMPUTE Q7 + NX SEQ -> HOST; §5) =========================
  TRIGGER (one of):
    HW exception {1,3,4}   POSIX signal       div0/bad-op/illegal     Q7 fault
    -> 0x1a64 -> 0x1a80    -> 0x14014          -> 0x13f34/58/80         -> exception.hpp
       code 'B'(0x42)         code 'A'(0x41)      codes 0/2/-             self-halt + PC
        |                      |                   |                        |
        +----------+-----------+-------------------+                        |
                   v                                                        |
        raise_error 0x13e18: wur UR#0x15  +  0xa450 report sink            |
                   |                                                        |
            +------+--------------------------------+                       |
            v sev1 (FP arithmetic ONLY)             v sev2 (everything else)|
        0x13e30 -> retw.n (CONTINUE)         0x13e00 -> 0xa2e0 Halt-disp    |
                                             -> 0x1cf8 Setup-Halt (save PC) |
                                             -> 0x3a44 write halt CSR 0x80400|
                                             -> j 0x13e14 SPIN  <-----------+
                   |
                   v  16-byte NEURON_ISA TPB_ERROR (§5d)
        UR#0x15 latch + errors_NT ring + MSI-X + "S:" printf ring
        -> host notification_t.exec_error_ring
           {ERROR 0x03 / TPB_ERROR 0x1f}, {SEQUENCER_FATAL 0x0a / NONFATAL 0x09},
           subtype {BAD_OPCODE 0x00 / FP 0x01 / INT 0x02 / SIGNAL 0x41 /
                    EXCEPTION 0x42 / ASSERT 0x40 / Q7 0x08/0x40}

=== FABRIC-FAULT PLANE (perimeter -> "Pacific" MANAGEMENT Q7; §2-§4,§6) =======
  ARM (§4, privileged firmware, host CANNOT):
    amzn CAM populated (fail-closed) -> NSM bypass cleared + timers/auto-iso set
    -> user CAM tightened -> NTS armed.   (sprot deny-all keeps host out; §3)
        |
  PCIe master AXI fault -> remapper(deny) / NSM(9 causes) / NTS(no-target)
    -> SLVERR/DECERR + 0xDEADBEEF inject -> reset_handshake_intr[8..13]
    -> ISOLATION SM enter[14] -> DRAIN (reset_staging_fifo{aw,w,ar} AFTER iso,
       BEFORE iofabric; nts slice resets) -> cfg_clear -> exit[15] -> re-ARM
        |
    (a) pcie_*_nmi apex summary    (b) DIRECT critical intr_peb_nsm_axi_timeout
            \_________________________________/   (peb_intc, critical:1, cause=111)
                          v
    PEB_INTC apex (96 summary + 32 critical) -> [apex->Q7/GIC hop INFERRED]
    -> "Pacific" MANAGEMENT Q7 ISR (reads NSM state + remapper addr_denied;
       drives recovery; acks reset_done).
    Abort severity -> scan-dump / clock-stop / SRAM-WP freeze (§6d).
```

**Two cores, two fault planes.** The COMPUTE Q7 + NX SEQ self-halt and self-report
**their** job faults to the host (top); the perimeter faults target the management
"Pacific" Q7 (bottom). The §4 arming is what makes the bottom plane live.

---

## 8. Negative / boundary findings (explicit non-claims)

1. **No global perimeter-lock bit.** The perimeter is locked by the *combination* of
   the three armed states + the CSR deny-all policy, not a write-once latch (§3
   CORRECTION). **HIGH by absence.**
2. **The §4 arming write-order is INFERRED** — the registers and lax reset values
   are OBSERVED; the precise inter-block ordering is reasoned from the steady-state
   dependencies + the one verbatim NSM ordering (§6c). **registers HIGH / order
   MED.**
3. **Only causes 1, 3, 4 get the SEQ HW-exception override** (`0x26ac`); causes
   0, 2, 5, 6, 7, 8 fall to the XTOS default `0x1c2ac`. **HIGH for the three
   overrides; CARRIED for the default routing.**
4. **The apex→"Pacific"/GIC vector hop is firmware/HW-owned, not register-encoded**
   (§6b). **LOW.**
5. **The host-side read binding** of UR#`0x15` vs the notification ring (§5d) is out
   of this page's scope — the device WRITE is OBSERVED, the consumer is on
   [RT-07](../../runtime/lifecycle-error-model.md). **MED.**
6. **The fabric recovery path is host-PCIe only** — d2d has no isolation SM (§6a).
   **HIGH/OBSERVED.**
7. **v5/Maverick interior is INFERRED.** Maverick is header-OBSERVED only; its
   per-block register strides and firmware bodies are reasoned by analogy to Cayman
   and flagged wherever cited. **LOW for v5 interior.**

---

## 9. Confidence ledger

**HIGH / OBSERVED** (read by `jq`/`rg`/disasm/`xxd` over the shipped artifacts):

- Carve reproduced: `iram.bin 8e4412b9…` / `dram.bin 7bdf6ed7…` match the FW
  anchors exactly.
- Perimeter reset posture: `nsm bypass.enable @+0x4[0] rst=0x1`;
  `amzn pass_on_miss rd[4]/wr[0] = 0x0` (fail-closed); `user pass_on_miss = 0x1`
  (fail-open); `qos chicken rst=0x1`; `reset_staging_fifo aw/w/ar` rst=0 with the
  verbatim "after iso, before iofabric" descriptions.
- `sprot block_internal_access _sprot_allow:False`; `block_host_access` deny-all +
  `apb.misc_ram allow:True` (host reaches only MISC_RAM).
- `pcie reset_handshake_intr[8..15]` (the `isolatio` typo at `[11]`);
  `peb_intc intr_peb_nsm_axi_timeout critical:1 LEVEL nmi_mask:0`;
  `int_abort_msk_grp @0x30` RW (`no_msix`).
- `register_exception_handlers @0x26ac` overrides **causes 1, 3, 4** with handler
  `0x1a64`; `0x1a64` reads `EXCCAUSE[11:0]` → `0x1a80` (code `'B'`=0x42) → `0x13e00`
  raise_FATAL → `j 0x13e14` spin.
- `raise_FATAL @0x13e00` (`movi a10,2` → `0x13e18` → `0xa2e0` → spin); `raise_error
  @0x13e18` (`20 15 f3` = `wur UR#0x15` @ `0x13e23` → `call8 0xa450`).
- Signal table `{6,2,4,8,0xb,0xf}` @ DRAM `0x83b70`; boot sentinel `0x6099CB34`
  (LE `34 cb 99 60`) @ `DRAM[0]`.
- `TPB_ERROR` envelope + the full `notific_type` / `error_id` / subtype / FP+INT
  mask enums (CARRIED from the ISA header re-read).

**MED / INFERRED:**

- The §4 inter-block arming write-order; "clear NSM bypass last" / "tighten user CAM
  last"; "host cannot arm" (follows from CSR deny-all + plane gating); the
  POSIX-signal ↔ XEA3-EXCCAUSE map; the isolation-SM transition arrows (§6a); the
  Q7 exception→subtype byte (§5c); the host-side read binding (§5d).

**LOW / UNCERTAIN:**

- The apex→"Pacific"/GIC vector hop (firmware/HW-owned, not register-encoded); the
  synthesized amzn/user CAM entry depth; the v5/Maverick interior.

**CARRIED** (consolidated from a named sibling, not re-derived here): the
instruction-exact SEQ handler bodies and the six entry points
([SEQ Error-Handler](../../firmware/seq/error-handler.md)); the abort/scan-dump
freeze ([Abort / Scan-Dump / Clock-Stop](../interrupt/abort-scandump-clockstop.md));
the XEA3 polled/partial-override interrupt model
([XEA3 Interrupt Architecture](../interrupt/xea3-interrupt-architecture.md)); the
steady-state perimeter map ([SoC-Fabric Perimeter](soc-fabric-perimeter.md)); the
device→host notification ring
([Device→Host Notification](../interrupt/device-host-notification.md)); the host
error consumer ([runtime error model](../../runtime/lifecycle-error-model.md)); the
layered trust model ([Trust Chain / Threat Model](trust-chain-threat-model.md)).

**Divergences flagged for the per-Part reconcile:** (a) the "6 entry points" framing
— this page enumerates the *runtime* six (div0 / bad-opcode / illegal / FP / signal /
HW-exception) and notes that `assert_fail` and the early emitter are *implementation
detail* of the FATAL wrapper, consistent with the error-handler page's superset
table; (b) the cause-{1,3,4} HW-exception override vs the cause-{0,2,5,6,7,8}
XTOS-default split (§5a CORRECTION), to be re-confirmed against the XEA3 page when it
gains the `exccause`→handler binding.
