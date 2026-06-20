# Boot / Fault Chain Overview — the GPSIMD Security Model

This page opens the GPSIMD **security sub-lane**. It is the orientation map for everyone who
follows: it states *what kind* of security model the AWS Annapurna "Cayman"/Trainium
Vision-Q7 GPSIMD die implements, names the three structural properties that define it, and
lays out the **complete, exhaustive fault / exception / abort taxonomy** that the rest of the
lane drills into. Where a sibling page owns the byte-level detail of one mechanism, this page
owns the *thesis* and the *cross-reference spine* — it cross-links, it does not duplicate.

The model is reconstructed **solely from static analysis of shipped artifacts** — the
`ncore2gp` Vision-Q7 ISA-config DLLs and headers, the RTL-generated Cayman register schemas,
and the carved device firmware (`libnrtucode.a`) disassembled with the shipped Cadence
`xtensa-elf-objdump`. The headline finding, stated once here and defended in §1, §3, §8:

> **KEYSTONE — the GPSIMD security model is integrity-rooted, not key-rooted; its perimeter
> is armed-not-default-secure; and every enforcement boundary fails-stop.** There is no
> cryptographic root of trust *active in the shipped GPSIMD compute domain*: no signature
> check, no key, no measured/attested boot in the carved firmware (§1, §8). The Cadence
> toolchain *offers* a secure-monitor option (`secmon`, with a locked-MPU secure/nonsecure
> split) — but the shipped compute firmware **does not link it** (§8 CORRECTION). What the
> silicon and firmware enforce instead is **integrity**: a kernel/user privilege ring, an
> address/ID firewall, an AXI transaction-integrity watchdog, ECC/RAS, a 16-entry MPU, and a
> single XEA3 exception dispatch — every one of which **defaults permissive out of reset** and
> must be **armed** by privileged software (§3), and every one of which, when it fires,
> **terminates the offending transaction or halts the engine** rather than masking the error
> (§4–§6). The perimeter is a wall *built at boot*, not a vault that ships locked; and the
> wall *stops*, it does not *recover*.

**Confidence legend.** `HIGH`/`MED`/`LOW` × `OBSERVED` (read from a shipped artifact —
disasm bytes, register schema, ISA-config table/header) / `INFERRED` (reasoned over OBSERVED
facts) / `CARRIED` (consolidated from a named sibling page, not re-derived here). **v5 /
Maverick scope:** the Maverick (v5) register schemas and ISA headers are **header-OBSERVED
only**; any reading of v5 *interior* firmware/silicon behaviour is flagged `[INFERRED]`.

---

## 0. The lane map — what this page opens `[orientation]`

The security lane is eight pages. This overview is the root; the other seven each take one
axis of the model to byte-level depth. Read them in this order:

| Page | Owns | This page's relationship |
|---|---|---|
| **boot-fault-overview** (this page) | the thesis + the exhaustive fault taxonomy + the cross-reference spine | root / orientation |
| [The SoC-Fabric Perimeter](./soc-fabric-perimeter.md) | the sprot firewall ring (remapper / qos NTS / NSM) as the *spatial* perimeter | §3 summarizes; that page details the fabric |
| [Boot-Arming + Device Fault Recovery](./boot-arming-fault-recovery.md) | the boot-time arming sequence + the (reset-only) recovery model | §2/§3/§6 summarize; that page details arm/recover |
| [Custom-Op Reachability / Isolation](./reachability-isolation.md) | what a guest custom-op kernel can and cannot reach | §3 names the boundary; that page proves it |
| [Firmware Trust Chain + Threat Model](./trust-chain-threat-model.md) | the (integrity-, not key-, rooted) trust chain + adversary model | §1/§8 state the thesis; that page builds the chain |
| [Profiling / Trace / Debug + Access Gating](./profiling-trace-debug-gating.md) | the OCD/TRAX/PMU debug surface and how it is gated | §4 lists the debug-fault class; that page gates it |
| [Information-Leakage / Side-Channel Surface](./side-channel-leakage.md) | residual leakage (poison patterns, timing, scan dumps) | §4/§5 note the surfaces; that page enumerates them |
| [SEC-Lane Synthesis (boot→attest→fault)](./security-synthesis.md) | the end-to-end synthesis that closes the lane | this page opens what that page closes |

The fault taxonomy here is the **union** of three already-committed mechanisms decoded in
adjacent lanes, consolidated for the first time on one page:

* the **run-time SEQ ErrorHandler** — [`../../firmware/seq/error-handler.md`](../../firmware/seq/error-handler.md)
  (six entry points, one recoverable);
* the **XEA3 exception dispatch** — [`../interrupt/xea3-interrupt-architecture.md`](../interrupt/xea3-interrupt-architecture.md)
  + [`../interrupt/handler-bodies.md`](../interrupt/handler-bodies.md) (the 9-cause vector);
* the **fabric abort / freeze** — [`../interrupt/abort-scandump-clockstop.md`](../interrupt/abort-scandump-clockstop.md)
  + [`../interrupt/errtrig-fis-routing.md`](../interrupt/errtrig-fis-routing.md) (the RAS emergency-stop).

---

## 1. Integrity-rooted, not key-rooted — the negative proof `[HIGH · OBSERVED / INFERRED]`

The first defining property is what the model **does not** have. A key-rooted (secure-boot)
model would carry, in the firmware image or the active ISA config, at least one of: a
public-key or hash constant, a signature-verification routine, a measured-boot / attestation
register, or a "secure" privilege state. A census of the shipped GPSIMD compute-domain
artifacts finds **none** of these active:

| Key-rooted artifact | Where it would live | Present in the shipped compute domain? |
|---|---|---|
| signature / RSA / ECDSA / SHA / HMAC / AES routine or constant | carved firmware `.rodata` / `.text` | **no** — no such string or constant in the carved `CAYMAN_NX_POOL_DEBUG` image |
| public key / measured-boot digest / attestation register | firmware data / a CSR | **no** |
| a "Secure" exception or a secure/non-secure world split | the `ncore2gp` exception roster | **no** — the roster has `PrivilegedException` + `ExternalRegisterPrivilegeException` but **no `SecureException`** |
| an `AxPROT[1]`-secure / TrustZone-secure transaction attribute | the sprot remapper `master_prot` field | **no** — `master_prot` emits `AxPROT = 0x2` = **non-secure**, privileged, data (§3) |
| the Cadence `secmon` secure-monitor (locked-MPU, secure/nonsecure syscall split) | `libsecmon.a`, linked into the image | **no** — `secmon`/`nonsecure`/`xtsm` string count in the carved compute firmware = **0** (§8 CORRECTION) |

What *is* present, in abundance, is **integrity** machinery — the address/ID firewall, the
AXI transaction-integrity watchdog, ECC/RAS (the `MESR` memory-error-status register), the
16-entry MPU, and the XEA3 exception unit (§3–§4). The privilege model the core *does*
implement is a **kernel/user ring** split, not a secure/non-secure split. The XEA3 `PS`
register carries a **single-bit ring** (`PS_RING`, `PS[4]`: `0 = kernel`, `1 = user`; no
`XCHAL_NUM_RINGS` macro, no multi-ring TrustZone ladder), and the exception roster carries
`PrivilegedException` (= `EXC_TYPE_PRIV_INSTRUCTION 0x111`, a user executing a privileged
instruction) and `ExternalRegisterPrivilegeException` (= `EXC_TYPE_PRIV_ERI_ACCESS 0x211`, a
user `rer`/`wer` to a privileged external register). Privilege is enforced; secrecy is not
rooted.

> **NOTE — "non-secure, privileged" is the whole posture in one field.** The most compact
> statement of the model is the `amzn_remapper.master_prot` reset value: `arprot` / `awprot`
> = `0x2`. In the ARM AxPROT encoding that is bit `[1]` set only → **privileged, data,
> NON-secure**. The privileged supervisor stamps every transaction *privileged* (so it clears
> the firewall) and *non-secure* (because there is no secure world to enter). The model
> protects *integrity and isolation between privileged and guest*, not *confidentiality
> against a privileged attacker*. `[HIGH · OBSERVED — reset value CARRIED from
> [remapper §4.2](../csr/remapper.md); the AxPROT decode is the ARM spec.]`

> **CORRECTION — the "NSM" naming collision is a trap for a trust-chain reader.** The sprot
> [`nsm`](../csr/nsm.md) decoded in this lane is the **AXI transaction-integrity watchdog**
> ("Network-Security Monitor" as an AXI-protocol monitor), **not** a cryptographic "Security
> Monitor". A *separate*, firmware-side `NSM` (the `secmon`/`libsec.a` survival monitor built
> with `-D_BUILD_NSM`) exists in the management domain and is out of the GPSIMD-compute scope
> decoded here. Reading the integrity watchdog's `SLVERR` + `0xDEADBEEF` integrity response
> (§4) as evidence of a crypto root is the single most plausible — and wrong — inference
> about this model. `[HIGH · OBSERVED — disambiguation CARRIED from [nsm §0](../csr/nsm.md).]`

The thesis is therefore **negative-evidence-grounded** at its root (`[HIGH · OBSERVED]` for
the absence census; `[INFERRED]` for "therefore integrity-rooted" — an honest reading of a
comprehensive null result, not a fabricated positive). The full trust-chain construction is
[`trust-chain-threat-model.md`](./trust-chain-threat-model.md).

---

## 2. The boot spine — where the perimeter is built `[HIGH · OBSERVED · CARRIED]`

The perimeter does not exist at the reset vector; it is *constructed* during boot. The SEQ
engine's boot path (full decode in [`../../firmware/seq/boot.md`](../../firmware/seq/boot.md))
is a textbook Cadence Xtensa24 reset handler that, among the cache/window bring-up, programs
the **core-side memory-protection unit (MPU)** — the only enforcement primitive armed by the
*compute* firmware itself — then hands to the C runtime and the `enter_run` rendezvous with
the host:

```text
reset (IRAM 0x0) --> boot stub 0x1dc --> _ResetHandler 0x90
    wsr.isb 0x4ec0            ; I-cache state init
    wsr.vecbase 0            ; the ONE vector-base program (no leveled vectors arm)
    I-cache INVALIDATE 0x1000 ; iii loop 0xb9..0xc8
    wsr.memctl 0xff08        ; enable I$/D$ ways
    wsr.wb (1<<30)           ; initial WindowBase
    wsr.mpuenb 0             ; *** disable MPU while programming  (0xfd) ***
    wptlb loop 0x104..0x149  ; *** program the MPU foreground regions (wptlb) ***
    wsr.mpuenb <mask>        ; *** re-arm MPU with computed region-valid mask (0x12e) ***
    wsr.memctl |= 0x08       ; I-cache ENABLE
    call0 crt0 0x190c -------> __init_array ctors --> BEGIN 0x2378
                                  program hw_decode.control (CSR 0x4000)
                                  fall into RUN LOOP --> enter_run 0x2c64
                                     POLL nx.start_ctrl (CSR 0x0004) <-- host asserts StartCtrl
                                     ACK start_ctrl=0 ; publish nx.run_state=1 ; dispatch
```

Three security-relevant facts fall out of this spine:

1. **The MPU is the firmware-armed isolation root, and it is integrity- (not key-) shaped.**
   `_ResetHandler` disables the MPU (`wsr.mpuenb 0`), programs a foreground region table via a
   `wptlb` loop, and re-arms it. The MPU is a **16-entry** unit (`XCHAL_HAVE_MPU = 1`,
   `XCHAL_MPU_ENTRIES = 16`) with a kernel/user access-rights model (`XTHAL_AR_*`, 4-bit). The
   region table is a plain memory map, not a key store — the toolchain's representative
   runtime table (`nort` LSP) is:

   | Region start | access (`XTHAL_AR_*`) | mem type | role |
   |---|---|---|---|
   | `0x00000000` | `RWXrwx` | non-cacheable | IRAM (code) |
   | `0x00080000` | `RWrw` | non-cacheable | DRAM (data) — **no execute** |
   | `0x00100000` | `RWXrwx` | writeback | SRAM |
   | `0x70000000` | `RWXrwx` | writeback | IO-cached |
   | `0x80000000` / `0x90000000` | `RWXrwx` | device | RAM-bypass / IO-bypass |
   | (interleaved) | `NONE` | device | unused holes — **deny windows** |

   Out of reset the MPU is **off**; the firmware turns it **on**. This is the §3
   "armed-not-default-secure" property in microcosm. `[HIGH · OBSERVED — boot §3 disasm +
   the `XTHAL_AR_*`/region table from the `ncore2gp` LSP `mpu_table.c`; the DRAM-non-execute /
   unused-`NONE` "deny window" reading is the integrity intent, MED · INFERRED.]`
2. **`VECBASE` is programmed exactly once, and no leveled-interrupt vectors arm.** The boot
   path arms the single XEA3 exception/window vector page and nothing else — no `rsil`,
   `rfi`, or `INTENABLE`. The async surface is **polled**, not vectored (the fault *delivery*
   model of §4 follows from this). `[HIGH · OBSERVED · CARRIED ·
   [xea3-arch §3/§5](../interrupt/xea3-interrupt-architecture.md).]`
3. **`enter_run` is the host→device hand-off boundary.** The engine boots, builds its
   perimeter, then **spins on the host-asserted `nx.start_ctrl` bit** (CSR `0x0004`) before it
   will run any guest microcode. The host arms the device; the device does not self-start. The
   full arming-and-recovery treatment is [`boot-arming-fault-recovery.md`](./boot-arming-fault-recovery.md).
   `[HIGH · OBSERVED · CARRIED · boot §6.]`

> **GOTCHA — a *boot* fault diverges from the *run-time* ErrorHandler.** The §4 fault
> taxonomy is the *run-time* model — it presumes a booted engine with the ErrorHandler TU, the
> signal table, and the exception handlers all installed. A fault during early boot (before
> `register_signal_handlers @0x13fa8` / `register_exception_handlers @0x26ac` run) has **no
> installed handler** and reaches the bare XTOS default (`0x1c2ac` → `simcall` / break door)
> or the hand-written halt vector at IRAM `0x1e8` (`halt 0`). The
> [error-handler page](../../firmware/seq/error-handler.md) forward-links here precisely for
> this boot-vs-run divergence; the boot-side recovery posture is
> [`boot-arming-fault-recovery.md`](./boot-arming-fault-recovery.md). `[HIGH · OBSERVED for the
> bare vectors; MED · INFERRED for "no handler installed pre-boot".]`

> **NOTE — the toolchain's `secmon` boot-fault model is the path NOT taken.** The Cadence LSP
> ships a `secmon` secure-monitor whose `secmon_mpu_init_final()` programs nonsecure regions,
> **locks** the secure entries, then `secmon_mpu_verify()` retroactively checks that every
> locked entry sits at the table head, overlaps secure memory, and is uncacheable — any
> violation calls `secmon_illegal_mpu_entry_trap()`, a **non-returning fatal boot trap**. That
> *is* a key-monitor-shaped fail-stop boot model — and it is exactly what the shipped compute
> firmware does **not** use (§8). The shipped firmware programs the MPU inline (point 1) with
> no lock-and-verify pass. The contrast sharpens the keystone: a secmon model was available;
> the compute domain chose the unlocked, integrity-only one. `[HIGH · OBSERVED — `secmon`
> sources read; absence in the carved image verified §8.]`

---

## 3. Armed-not-default-secure — the perimeter ring `[HIGH · OBSERVED]`

The second defining property: every fabric-level enforcement block **boots permissive** and
must be armed by privileged software. This is the deliberate consequence of an *integrity*
model where the supervisor builds the wall, rather than a *key* model where the wall ships
locked. The evidence is the reset value of every gate in the **sprot (slave-protection) ring**
that wraps each Fabric Interface Slice (FIS). Spatial treatment:
[`soc-fabric-perimeter.md`](./soc-fabric-perimeter.md); byte detail on the CSR pages.

| Enforcement block | Reset posture | Field / register | Must be armed by |
|---|---|---|---|
| [`amzn_remapper`](../csr/remapper.md) (privileged addr/ID firewall) | **fail-CLOSED** | `amzn_cam_pass_on_miss` = `0x0` → a CAM miss **DENIES** | (already closed; whitelist *added* by FW) |
| [`user_remapper`](../csr/remapper.md) (guest addr/ID firewall) | **fail-OPEN** | `user_cam_pass_on_miss` = `0x1` → a CAM miss **PASSES** | FW must populate the CAM to constrain a guest |
| [`nsm`](../csr/nsm.md) (AXI integrity watchdog) | **bypassed** | `control.bypass.enable` = `0x1` → ships *bypassed* | FW clears `bypass` to arm the monitor |
| [`qos_prot`](../csr/qos-prot.md) shaper | **transparent** | `csr.control.chicken` = `0x1` → all txn modification *disabled* | FW clears `chicken` to shape |
| core MPU (§2) | **off** | `MPUENB` = `0` at reset; FW programs + re-arms | the SEQ `_ResetHandler` |

> **WALL — the two remappers are mirror-image fail-policies, and the asymmetry is the whole
> isolation model.** The **privileged** `amzn_remapper` is **fail-CLOSED** (`pass_on_miss =
> 0x0`): an un-whitelisted privileged transaction is denied, so the supervisor surface is
> deny-by-default and *adds* allowed regions. The **guest** `user_remapper` is **fail-OPEN**
> (`pass_on_miss = 0x1`): an un-configured guest CAM passes everything, so a guest is
> *unconstrained until the firmware programs its CAM*. The "armed-not-default-secure" property
> is therefore **central in the guest direction specifically**: an unarmed `user_remapper` is
> a wide-open guest. A reimplementer who flips either default inverts the security model —
> `amzn`→fail-open destroys the privileged whitelist; `user`→fail-closed bricks every guest.
> `[HIGH · OBSERVED — `pass_on_miss` `0x0`/`0x1` CARRIED from [remapper §4.1](../csr/remapper.md).]`

The "what reaches a guest custom-op" question — the practical reachability boundary this ring
enforces — is [`reachability-isolation.md`](./reachability-isolation.md). The privilege
sideband the ring stamps (`master_prot` → `AxPROT = 0x2`, AMZN-only; a guest **cannot forge
AxPROT**) is the [remapper](../csr/remapper.md)'s; `qos_prot` does **not** emit AxPROT.

---

## 4. The exhaustive fault taxonomy — fails-stop `[HIGH · OBSERVED]`

The third defining property: when an enforcement boundary fires, it **terminates** — a
transaction is poisoned-and-erred, or the engine spins forever. Recovery is the rare
exception, and is always *reset*, never *resume*. This is the consolidated, exhaustive
fault-code table the rest of the lane references. It has **four physical layers**, each with
its own delivery model:

```text
   LAYER                  DETECTOR                  EMITTER                 OUTCOME
  +--------------+----------------------------+-----------------------+--------------+
  | A. FABRIC    | sprot remapper DENY /      | qos NTS responder /   | AXI SLVERR + |
  |   integrity  | qos NTS no-target /        | nsm error-response    | 0xDEADBEEF   |
  |   (HW)       | nsm AXI-protocol fault     | injector              | poison       |
  +--------------+----------------------------+-----------------------+--------------+
  | B. RAS /     | errtrig int_cause latch    | Abort wire-OR ->      | block FREEZE |
  |   abort (HW) | (ECC, timeout, DENY, ...)  | Sunda freeze bundle   | + scan dump  |
  +--------------+----------------------------+-----------------------+--------------+
  | C. XEA3 exc  | EXCCAUSE-classed sync flt  | exc vector 0x6c ->    | FATAL spin   |
  |   (core HW)  | (illegal/addr/external/..) | dispatcher -> 0x1a64  | (code 'B')   |
  +--------------+----------------------------+-----------------------+--------------+
  | D. SEQ FW    | decoded fault / signal /   | 6 entry points ->     | FATAL spin   |
  |   ErrorHandlr| assert / FP status         | raise_FATAL 0x13e00   | (FP=RECOVER) |
  +--------------+----------------------------+-----------------------+--------------+
```

### 4a. Layer A — fabric-integrity faults (HW, terminate-the-transaction) `[HIGH · OBSERVED]`

A transaction that violates the sprot firewall, hits no target slave, or is malformed on the
AXI protocol is **terminated locally**: the responder manufactures an AXI error response and,
on reads, poisons the payload. The detector *decides*; the NTS responder *responds* (the
[remapper](../csr/remapper.md)↔[qos_prot](../csr/qos-prot.md) division).

| Fault | Detector | Response code | Poison | Source page |
|---|---|---|---|---|
| remapper CAM miss / policy DENY | `amzn`/`user_remapper` | reuses NTS path | — (decides only) | [remapper](../csr/remapper.md) |
| qos NTS no-target-slave | `qos_prot.nts_amzn` | `read/write_response` = **`0x2` SLVERR** | `read_data` = **`0xDEADBEEF`** (×1 register, replicated across the 256-bit beat) | [qos-prot](../csr/qos-prot.md) |
| NSM write-side (4 causes: no-match-AW, B-timeout, AW/W-handshake timeout) | `nsm.wr.status` | `BRESP` = `wr.cfg_1.axi_bresp` rst **`0x2` SLVERR** | — (write) | [nsm](../csr/nsm.md) |
| NSM read-side (5 causes: early/missing RLAST, no-match-AR, R/AR-handshake timeout) | `nsm.rd.status` | `RRESP` = `rd.cfg_1.axi_rresp` rst **`0x2` SLVERR** | `error_data_0..7` = **`0xDEADBEEF`** ×8 (256-bit poison) | [nsm](../csr/nsm.md) |

> **NOTE — `0x2` and `0xDEADBEEF` are the unified fabric fail-stop signature.** Across all
> three fabric blocks the error response is `SLVERR` (`2'b10 = 0x2`; `0x3 = DECERR` is the
> selectable alternative) and the read poison is the eight-word `0xDEADBEEF` sentinel
> (`3735928559`). A host that reads `0xDEADBEEF` from device memory is reading a *fail-stopped*
> fabric transaction, not data. The NSM read-poison surface (a recognizable 256-bit pattern)
> is noted as a leakage surface by [`side-channel-leakage.md`](./side-channel-leakage.md).

> **CORRECTION — qos-NTS materializes ONE poison register, not two.** An earlier
> pass tagged the `qos_prot.nts_amzn` poison "×2." The byte-grounded census is
> **8 / 1 / 0** (NSM lays the full beat in 8× `error_data_*` @`0x21c..0x238`;
> qos_prot-NTS materializes **exactly one** register `read_data` @abs `0x40c`,
> datapath-replicated across the 256-bit beat; the remapper holds zero and delegates
> to NTS). The "×2" was count-grep inflation — `rg -ci deadbeef qos_prot.json` = 2,
> but the second hit is that register's own Description text ("default=deadbeef"),
> not a second register. The "same wire bytes, different CSR materialization"
> framing is unchanged. See [`nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md)
> §9 and [`soc-fabric-perimeter.md`](./soc-fabric-perimeter.md). `[HIGH · OBSERVED]`
> `[HIGH · OBSERVED — reset values CARRIED from [nsm §4b](../csr/nsm.md) / [qos-prot](../csr/qos-prot.md).]`

### 4b. Layer B — RAS / abort faults (HW, freeze-the-block) `[HIGH · OBSERVED · CARRIED]`

A RAS-class error (ECC uncorrectable, watchdog timeout, a fabric DENY classified Abort, a
host-injected cause) sets a bit in an error-trigger `int_cause` latch; if abort-unmasked, the
block's **Abort wire-OR** drives the **`Sunda` freeze bundle** (`AddressOffset 0x300`,
`no_msix` error-triggers only — a host is structurally locked out of arming a freeze). The
freeze gates the block's clocks, write-protects its SRAM, and captures its flop state into an
ARM CoreSight **ELA-500** (1,318 instances). Full decode:
[`../interrupt/abort-scandump-clockstop.md`](../interrupt/abort-scandump-clockstop.md);
routing front-end: [`../interrupt/errtrig-fis-routing.md`](../interrupt/errtrig-fis-routing.md).

| Aspect | Value | Note |
|---|---|---|
| Freeze bundle | `Sunda` @ `0x300`, `BundleSizeInBytes 0xf0`, `HalExists = NON_EX_ONLY` | privileged-only; firmware does **not** drive it |
| Freeze actions | scan-dump / clock-stop / SRAM-write-protect / block-level-logic, each an 8-bit per-target bitmap, split local vs remote | remote = cross-die via D2D |
| Trigger | Abort = Wire-OR of `int_cause & !int_abort_msk_grp` (`@0x30`, rst `0xffffffff` = all masked) | per-source abort policy is instantiation-time |
| errtrig fabric scale (Cayman) | **962** generator PAIRs; `intc_4grp` = 4×32 = **128** inputs/unit | `[HIGH · OBSERVED]` |
| Apex (`peb_intc`) | **128** inputs, **32** critical (the SoC fatal/survival set), 24 edge | NSM critical at apex idx 111 (LEVEL) |
| Recovery | no abort-clear register — **recovery = reset + clock un-gate + W0C cause clear** | reset, not resume |
| Per-core capture | `FAULTINFOLO @0x302c` (`PFatalError[31]` sticky), `FAULTINFOHI @0x3030` (17 ECC fields) | read over OCD post-mortem |

> **QUIRK — the firmware never drives abort; abort never "halts the Q7".** The carved SEQ
> firmware has **zero** `abort`/`scan_dump`/`clock_stop`/`freeze` strings — the freeze is a
> SoC RAS/management hardware response, consistent with `HalExists = NON_EX_ONLY`. And an abort
> does not invoke the Q7's halt CSR; it gates the block's *clock*, which incidentally stops the
> Q7 that lives in the block. The Q7's *own* fault response (Layer C/D) is independent of the
> fabric freeze. `[HIGH · OBSERVED · CARRIED · [abort §4a](../interrupt/abort-scandump-clockstop.md).]`

### 4c. Layer C — XEA3 synchronous exceptions (core HW, vector→FATAL) `[HIGH · OBSERVED]`

The Vision-Q7 core implements **XEA3** (Exception Architecture 3): `XCHAL_HAVE_XEA3 = 1`,
`XCHAL_HAVE_XEA2 = 0`, `XCHAL_HAVE_NMI = 0`, single-dispatch, no leveled-interrupt registers
(full architectural decode:
[`../interrupt/xea3-interrupt-architecture.md`](../interrupt/xea3-interrupt-architecture.md)).
A synchronous fault is classed by the **`EXCCAUSE[3:0]` CAUSE nibble** (9 values, from
`corebits-xea3.h`), takes the exception vector at IRAM `0x6c`, and is demultiplexed in
software through the **9-entry `xtos_exc_handler_table`** (`XCHAL_EXCCAUSE_NUM = 9`).
Handler-body decode: [`../interrupt/handler-bodies.md`](../interrupt/handler-bodies.md).

| `EXCCAUSE[3:0]` | `corebits-xea3.h` name | Meaning | Installed handler | Outcome |
|---:|---|---|---|---|
| `0` | `EXCCAUSE_NONE` | no exception | XTOS default `0x1c2ac` | (spurious) |
| `1` | `EXCCAUSE_INSTRUCTION` | illegal / unsupported / **privileged** instruction | **custom SEQ `0x1a64`** | **FATAL** (code `'B'`) |
| `2` | `EXCCAUSE_ADDRESS` | load/store, alignment, gather/scatter | XTOS default `0x1c2ac` | FATAL / halt door |
| `3` | `EXCCAUSE_EXTERNAL` | bus / fetch error, **privileged ERI access** | **custom SEQ `0x1a64`** | **FATAL** (code `'B'`) |
| `4` | `EXCCAUSE_DEBUG` | break / single-step / stack-limit | **custom SEQ `0x1a64`** | **FATAL** (code `'B'`) |
| `5` | `EXCCAUSE_SYSCALL` | syscall | XTOS default `0x1c2ac` | FATAL / halt door |
| `6` | `EXCCAUSE_HARDWARE` | uncorrectable ECC / bus failure | XTOS default `0x1c2ac` | FATAL / halt door |
| `7` | `EXCCAUSE_MEMORY` | **MPU access violation / stack-limit** | XTOS default `0x1c2ac` | FATAL / halt door |
| `8` | `EXCCAUSE_CP_DISABLED` | coprocessor disabled | XTOS default `0x1c2ac` | FATAL / halt door |

`EXCCAUSE` is structured: `[3:0]` = CAUSE (the nibble above), `[7:4]` = TYPE, `[11:8]` =
SUBTYPE, `[13:12]` = LSFO, `[15:14]` = IMPR. The 12-bit `EXCCAUSE_FULLTYPE` (`[11:0]`) is the
full sub-cause the custom handler extracts. The `corebits-xea3.h` header refines the 9 CAUSE
nibbles into the complete **`EXC_TYPE_*` sub-cause vocabulary** the silicon can raise — the
security-relevant ones, with their exact FULLTYPE values:

| FULLTYPE | `EXC_TYPE_*` | CAUSE | Security relevance |
|---:|---|---|---|
| `0x111` | `PRIV_INSTRUCTION` | 1 | **user executing a privileged instruction** — the ring boundary |
| `0x211` | `PRIV_ERI_ACCESS` | 3 | **user `rer`/`wer` to a privileged external register** — the ERI boundary |
| `0x121` | `DIVIDE_BY_ZERO` | 1 | integer divide-by-zero → FATAL (Layer D `0x13f34`) |
| `0x141`..`0x641` | `FP_INV_OP` / `DIV0` / `OVF` / `UF` / `INEXACT` / `UF_INEXACT` | 1 | the 6 FP arithmetic sub-causes → the **one recoverable** class (Layer D `0x13eb0`) |
| `0x112` | `INVALID_PC` | 2 | PC out of range (HARD; vs the *soft* speculative-PC guard §5 #18) |
| `0x122` | `INVALID_TCM` | 2 | invalid TCM access |
| `0x422` | `CROSS_MEM_ACCESS` | 2 | **cross memory / MPU-region access** |
| `0x522` | `INVALID_MSPACE` | 2 | access outside valid memory space |
| `0x107`/`0x207`/`0x307` | `ACCESS_VIOLATION_{1,2,3}` | 7 | **MPU access-prohibited** — the memory-protection enforcement |
| `0x117`/`0x217`/`0x317` | `UNDEFINED_ATTR_{1,2,3}` | 7 | **MPU undefined-attribute** fault |
| `0x127` | `TLB_CONFLICT` | 7 | MPU/TLB multi-hit |
| `0x064` | `STACK_LIMIT` | 4 | **ISL/KSL stack-limit exceeded** — stack-overflow enforcement |
| `0x123`/`0x223` | `BUS_TIMEOUT` / `CASTOUT_TO` | 3 | bus timeout (the watchdog's core-side correlate) |
| `0x346`/`0x546` | `DATA_READ` / `CASTOUT_ADDR` | 6 | **uncorrectable ECC** read/castout (RAS, also a Layer-B source) |

> **NOTE — MPU access violations and stack-limit faults are `EXCCAUSE_MEMORY`/`DEBUG` HARD
> faults, not soft guards.** A guest kernel that strays outside its MPU regions raises
> `ACCESS_VIOLATION_*` (`EXCCAUSE_MEMORY`, CAUSE 7); a stack overrun raises `STACK_LIMIT`
> (`0x064`, CAUSE 4) — both take a real HW exception, land in the dispatcher, and FATAL-halt.
> This is the architectural enforcement behind the §3 MPU and the ISL/KSL limits — **distinct**
> from the *soft* speculative-PC-range guard (`is_pc_in_bounds @0x68d0`, skip-with-WARNING) of
> [`../../firmware/seq/pc-bounds.md`](../../firmware/seq/pc-bounds.md). `[HIGH · OBSERVED —
> FULLTYPE values from `corebits-xea3.h`; the soft/hard split CARRIED from
> [error-handler §8](../../firmware/seq/error-handler.md).]`

> **QUIRK — only three of the nine causes get a firmware handler; the rest take the default.**
> Boot's `register_exception_handlers @0x26ac` overrides the XTOS default with the custom SEQ
> handler `0x1a64` for **exactly** CAUSE 1 / 3 / 4. All six others stay on `0x1c2ac` (which
> still terminates: `simcall` / break door). The custom handler extracts the 12-bit EXCCAUSE
> FULLTYPE and tail-calls the early FATAL emitter `0x1a80` (code `'B'`). `[HIGH · OBSERVED ·
> CARRIED · [handler-bodies §4d/§4e](../interrupt/handler-bodies.md).]`

### 4d. Layer D — SEQ ErrorHandler (firmware, the one recoverable class) `[HIGH · OBSERVED · CARRIED]`

The firmware-side fault subsystem ([`../../firmware/seq/error-handler.md`](../../firmware/seq/error-handler.md))
has **six named entry points**, a uniform log→pack→raise pipeline, and a **binary
recoverable-vs-fatal policy**. It is the *producer* of the device→host fault record (§7).
**Exactly one** of the six (FP arithmetic) is recoverable; the other five halt the engine
forever.

| Fault entry point | Address | Code byte | Policy |
|---|---|---:|---|
| `HandleBadOpcode(opcode)` (dispatch-default `0x3198` miss) | `0x13f58` | `0` | **FATAL** (spin) |
| `HandleIllegalInstr(op)` (valid-opcode, unsupported sub-op) | `0x13f80` | `0` | **FATAL** (spin) |
| `HandleIntDivZero()` | `0x13f34` | `2` | **FATAL** (spin) |
| `HandleFPError(status)` (FCR-trapped FP status) | `0x13eb0` | FP-mapped `{1,2,4,8,16}` | **RECOVERABLE** (return) |
| `signal_handler(signum)` (any registered POSIX signal = the Xtensa HW-exception abstraction) | `0x14014` | `'A'` (`0x41`) | **FATAL** (spin) |
| `assert_fail(msg,file,line)` (DEBUG asserts) | `0xa304` | `0` | **FATAL** (spin) |
| *(early emitter — the exception-vector path, Layer C)* | `0x1a80` | `'B'` (`0x42`) | **FATAL** (spin) |

> **QUIRK — two FATAL emitters, two code bytes, by the *door* not the *cause*.** A fault that
> arrives through the **exception vector** (Layer C) raises code **`'B'`** via the early
> emitter `0x1a80`; a fault that arrives through the **signal** abstraction raises code
> **`'A'`** via `signal_handler`; a *decoded* fault raises its class byte (`0` / `2`). All
> three build the same `notification_t` and converge on `raise_FATAL 0x13e00` → halt-dispatch
> `0xa2e0` → infinite spin at `j 0x13e14`. The code byte tells the host *which door* the fault
> came through, not which cause fired. The six registered signals are `{2,4,6,8,0xb,0xf}` =
> `{SIGINT, SIGILL, SIGABRT, SIGFPE, SIGSEGV, SIGTERM}`. `[HIGH · OBSERVED · CARRIED ·
> [error-handler §4e/§5d](../../firmware/seq/error-handler.md),
> [handler-bodies §5](../interrupt/handler-bodies.md).]`

> **NOTE — the host-runtime result enum (`nrtucode_result_t`) is a different population.** The
> on-device fault *code bytes* above (`0`/`2`/`'A'`/`'B'`/FP-mapped) are not the host
> *loader*'s status enum. The host `libnrtucode` API returns `nrtucode_result_t` =
> {`SUCCESS 0`, `ERR_UNKNOWN_CORE 1`, `ERR_UNKNOWN_IMAGE 2`, `ERR_MISSING_IMAGE 3`,
> `ERR_VERSION 4`, `ERR_ALLOCATION 5`, `ERR_IO 6`, `ERR_ENOSPC 7`, `ERR_INVALID 8`,
> `ERR_RELOCATION 9`} — a *load-time* result space (image select, allocation, relocation),
> orthogonal to the *run-time* fault doors. Do not conflate the two: `ERR_RELOCATION 9` is a
> host loader failing to bind a symbol, not a device fault. `[HIGH · OBSERVED — `nrtucode.h`
> `nrtucode_result_t`.]`

The fault-dispatch algorithm, unifying Layers C and D into the single FATAL spin:

```c
/* The unified fault dispatch: HW exception (Layer C) and FW-decoded fault (Layer D)
 * both terminate at the same severity-2 raise -> halt -> spin.  Addresses inline.
 * [HIGH · OBSERVED — CARRIED from error-handler.md §5/§6 + handler-bodies.md §4/§5] */

void on_fault(fault_t kind, uint32_t aux) {
    uint8_t code;
    int     severity;

    switch (kind) {
        /* ---- Layer C: synchronous XEA3 exception (EXCCAUSE 1/3/4 reach 0x1a64) ---- */
        case EXC_VECTOR:                          /* exc vector 0x6c -> 0x1ec -> 0x1a64 */
            aux  = exccause & 0xFFF;              /* 0x1a71: EXCCAUSE_FULLTYPE          */
            code = 'B';  severity = 2; break;     /* early emitter 0x1a80              */

        /* ---- Layer D: firmware-decoded faults ---- */
        case SIGNAL:    code = 'A'; severity = 2; break;   /* 0x14014  (HW-exc abstraction) */
        case BAD_OPCODE:code = 0;   severity = 2; break;   /* 0x13f58  (dispatch-default)   */
        case DIV_ZERO:  code = 2;   severity = 2; break;   /* 0x13f34                       */
        case ASSERT:    code = 0;   severity = 2; break;   /* 0xa304                        */

        /* ---- the ONE recoverable class ---- */
        case FP_FAULT:  code = fp_map(aux);                /* 0x13eb0: FCR status -> {1,2,4,8,16} */
                        severity = 1; break;               /* the only severity-1 caller    */
    }

    log_S_line(kind);                             /* 0x18b84 printf-logger ('S:' ring)  */
    notification_t rec = build_error_record(code, aux);  /* 0x13e40: {block_id, code, aux} */
    wur(UR_0x15, rec);                            /* 0x13e23: publish to TIE UR#0x15 (21) */
    report_sink(rec);                             /* 0xa450:  host notify dispatch        */

    if (severity == 1)                            /* raise_RECOVERABLE 0x13e30            */
        return;                                   /*   *** RETURNS — execution CONTINUES *** */

    /* severity == 2: raise_FATAL 0x13e00 */
    halt_dispatch();                              /* 0xa2e0: "Setup Halt" -> "Entering HALT"  */
                                                  /*         -> store halt CSR @0x80400        */
    for (;;) { }                                  /* j 0x13e14 — *** ENGINE DEAD until reset *** */
}
```

> **GOTCHA — recoverable does not mean masked.** The FP fault still *logs* and *reports* a host
> notification (severity 1); the only difference from FATAL is that `raise()` returns. A host
> distinguishes "engine alive, reported FP fault" (sev 1) from "engine hung" (sev 2) by the
> severity byte and by whether notifications keep arriving. There is **no** "ignore and
> continue" path in the model — fails-stop is total except for the FP log-and-continue. `[HIGH
> · OBSERVED · CARRIED · [error-handler §4d](../../firmware/seq/error-handler.md).]`

---

## 5. The fault-policy summary table `[HIGH · OBSERVED · CARRIED]`

The single consolidated policy view across all four layers — the table the rest of the lane
points back to:

| # | Fault source | Layer | Delivery | Outcome | Owner page |
|---:|---|---|---|---|---|
| 1 | remapper CAM miss / DENY | A (HW) | AXI response | terminate txn (NTS SLVERR) | [remapper](../csr/remapper.md) |
| 2 | qos NTS no-target-slave | A (HW) | AXI response | SLVERR `0x2` + `0xDEADBEEF` | [qos-prot](../csr/qos-prot.md) |
| 3 | NSM AXI-protocol (9 causes) | A (HW) | AXI response | SLVERR + 256-bit poison | [nsm](../csr/nsm.md) |
| 4 | ECC uncorrectable / RAS / watchdog timeout | B (HW) | errtrig Abort wire-OR | block freeze + scan dump (reset to recover) | [abort](../interrupt/abort-scandump-clockstop.md) |
| 5 | host-injected cause (`int_cause_set_grp` W1S) | B (HW) | errtrig Abort wire-OR | block freeze | [abort §6b](../interrupt/abort-scandump-clockstop.md) |
| 6 | illegal / privileged instruction (CAUSE 1; `0x111`) | C (core HW) | exc vector → `0x1a64` | FATAL spin (code `'B'`) | [handler-bodies](../interrupt/handler-bodies.md) |
| 7 | external/bus / privileged-ERI (CAUSE 3; `0x211`) | C (core HW) | exc vector → `0x1a64` | FATAL spin (code `'B'`) | [handler-bodies](../interrupt/handler-bodies.md) |
| 8 | debug exception (CAUSE 4) | C (core HW) | exc vector → `0x1a64` | FATAL spin (code `'B'`) | [handler-bodies](../interrupt/handler-bodies.md) |
| 9 | addr / syscall / hw / CP (CAUSE 2,5,6,8) | C (core HW) | exc vector → XTOS default | FATAL / halt door | [handler-bodies §4f](../interrupt/handler-bodies.md) |
| 10 | **MPU access violation / stack-limit** (CAUSE 7/4; `0x107`/`0x064`) | C (core HW) | exc vector → default | FATAL spin | §4c, [error-handler §9](../../firmware/seq/error-handler.md) |
| 11 | bad opcode (dispatch default) | D (FW) | `0x3198` → `0x13f58` | FATAL spin (code `0`) | [error-handler §4a](../../firmware/seq/error-handler.md) |
| 12 | illegal sub-op (e.g. RNG algo) | D (FW) | `0x13f80` | FATAL spin (code `0`) | [error-handler §4b](../../firmware/seq/error-handler.md) |
| 13 | integer divide-by-zero | D (FW) | `0x13f34` | FATAL spin (code `2`) | [error-handler §4c](../../firmware/seq/error-handler.md) |
| 14 | POSIX signal (HW-exc abstraction) | D (FW) | `0x14014` | FATAL spin (code `'A'`) | [error-handler §4e](../../firmware/seq/error-handler.md) |
| 15 | failed assertion (DEBUG) | D (FW) | `0xa304` | FATAL spin (code `0`) | [error-handler §6c](../../firmware/seq/error-handler.md) |
| 16 | **FP arithmetic fault** | **D (FW)** | **`0x13eb0`** | **RECOVERABLE (log + return)** | [error-handler §4d](../../firmware/seq/error-handler.md) |
| 17 | unhandled "surprise" (async control) | D (FW poll) | `sunda_handle_surprises 0x6cf4` default arm | FATAL spin (assert) | [q7-surprises §3](../interrupt/q7-surprises-binding.md) |
| 18 | speculative PC-range OOB | (soft) | `is_pc_in_bounds 0x68d0` | **WARN + skip** (never a fault) | [pc-bounds](../../firmware/seq/pc-bounds.md) |

> **NOTE — exactly two non-FATAL classes in the entire model.** Of the 18 fault sources, **only
> #16 (FP arithmetic, RECOVERABLE log-and-return) and #18 (speculative PC range, SOFT
> skip-with-warning)** do not terminate. Everything else either poisons the transaction
> (Layer A), freezes the block (Layer B), or spins the engine forever (Layers C/D). The
> dominant disposition is **stop**. `[HIGH · OBSERVED — the fatal/non-fatal split CARRIED from
> the four owner pages.]`

---

## 6. Recovery — reset, not resume `[HIGH · OBSERVED · CARRIED]`

The fails-stop property has a corollary: there is **no in-place recovery register** anywhere
in the model. Recovery is always a *reset* of the offending unit, driven from outside the
faulting engine.

| Layer | "Recovery" mechanism | Driver |
|---|---|---|
| A (fabric) | host re-issues the transaction after fixing the CAM / re-arming the monitor; the poisoned response is consumed and discarded | host runtime |
| B (abort) | post-mortem readback (ELA RAM via `RRAR`/`RRDR`; `FAULTINFO`/TRAX over OCD) → `PWRCTL.CoreReset[16]` / `DebugReset[28]` → SoC un-gates clocks + W0C-clears `int_cause` + re-arms | SoC RAS / management |
| C/D (core) | the engine is spinning at `j 0x13e14`; only an external **reset** (`PWRCTL.CoreReset`) revives it — the firmware itself never leaves the spin | host / management reset |

There is no firmware code path out of the FATAL spin: `raise_FATAL @0x13e00` calls
halt-dispatch and then loops forever. The full arming-and-recovery treatment — including the
boot re-arm sequence that re-builds the §3 perimeter after a reset — is
[`boot-arming-fault-recovery.md`](./boot-arming-fault-recovery.md). `[HIGH · OBSERVED ·
CARRIED · [error-handler §6](../../firmware/seq/error-handler.md),
[abort §6c](../interrupt/abort-scandump-clockstop.md).]`

---

## 7. Device→host fault reporting `[HIGH · OBSERVED · MED]`

When a fault fires (any FATAL or the recoverable FP), the SEQ firmware publishes it on
**three** host-observable channels — so a host runtime can learn *which engine* faulted and
*why* even though the engine itself is (usually) dead:

1. **TIE user-register `UR#0x15` (21)** — the packed `notification_t` record `{block_id (the
   faulting engine), error_code (the class byte), opcode/aux}`, written by `raise_error
   @0x13e23`. The natural host-pollable error latch. `[HIGH · OBSERVED write; MED · INFERRED
   host read.]`
2. **The per-TPB error-trap notification ring** — the report sink `0xa450` dispatches a
   notification (`"S: NOTIFY"` / `"sending interrupt"` / `"sending notification"` + an
   external-register write) that climbs the notification chain to the host (MSI-X). The
   device→host notification path itself is the (sibling)
   [`../interrupt/device-host-notification.md`](../interrupt/device-host-notification.md)
   page. `[MED — dispatch OBSERVED; exact ring CSR cross-referenced, not re-derived.]`
3. **The host stdout/stderr SPSC ring** — the printf-logger `0x18b84` writes the
   human-readable `"S: ErrorHandler : …"` line (present only in DEBUG builds; the PERF build
   strips all `'S:'` strings). `[HIGH · OBSERVED logger; MED · INFERRED transport.]`

> **NOTE — the compute Q7 self-reports; it is not an apex-critical sink.** A GPSIMD compute Q7
> fault does **not** climb the 32-critical `peb_intc` apex fast-path (that is the *management*
> "Pacific" core's domain). The compute Q7 **self-reports** its job fault via the notification
> ring above and **self-halts** (FATAL spin); the fabric freeze (Layer B), if the fault is also
> a RAS source, stops it via clock-stop independently. The two-core split (compute Q7 vs
> management Pacific) is decoded on
> [`../interrupt/q7-surprises-binding.md`](../interrupt/q7-surprises-binding.md) §6.
> `[HIGH · CARRIED.]`

---

## 8. Adversarial self-verification of the five strongest claims `[HIGH]`

The five claims this page rests on, each checked against the specific artifact that would
falsify it:

1. **"Integrity-rooted, not key-rooted."** *Falsifier:* a crypto/key/signature/attestation
   artifact, or a linked secure-monitor, in the shipped compute domain. *Check:* the carved
   `CAYMAN_NX_POOL_DEBUG` firmware carries no `signature`/`rsa`/`sha`/`hmac`/`aes`/`attest`/
   `pubkey` string; the `secmon`/`nonsecure`/`xtsm` string count in the same image is **0**;
   the exception roster has `PrivilegedException` + `ExternalRegisterPrivilegeException` but
   **no `SecureException`**; `master_prot` emits `AxPROT = 0x2` (non-secure). *Result:*
   **holds** — comprehensive null result; the one positive privilege primitive is a kernel/user
   ring (`PS_RING`), not a secure world. `[HIGH · OBSERVED]`

2. **"Armed-not-default-secure (lax out of reset)."** *Falsifier:* any fabric gate that boots
   in its secure/closed state. *Check:* `user_remapper.pass_on_miss = 0x1` (fail-OPEN),
   `nsm.control.bypass.enable = 0x1` (bypassed), `qos_prot.csr.control.chicken = 0x1`
   (transparent), MPU `MPUENB = 0` at reset. *Result:* **holds** — every guest-facing gate
   boots permissive; only the *privileged* `amzn_remapper` (`0x0`) boots closed. The property
   is real and is specifically guest-directional. `[HIGH · OBSERVED]`

3. **"Fails-stop."** *Falsifier:* a fault disposition that masks-and-continues. *Check:* the
   18-row policy table (§5) — 16 of 18 terminate (poison / freeze / spin); the two exceptions
   are FP (log-and-*return*, still reports) and the soft PC-range guard (warn-and-skip).
   *Result:* **holds** — no "ignore" path; even the recoverable FP class reports a host
   notification. `[HIGH · OBSERVED · CARRIED]`

4. **"The fault taxonomy is exhaustive / EXCCAUSE = 9, refined to the full `EXC_TYPE_*` set."**
   *Falsifier:* an EXCCAUSE width ≠ 9 or an unlisted fault door. *Check:* `XCHAL_EXCCAUSE_NUM =
   9` (`core-isa.h`); the dispatcher keys on `EXCCAUSE & 0xf` (`extui …,0,4`); the install
   accessor bounds-checks `≤ 8`; the structured `corebits-xea3.h` roster refines the 9 nibbles
   into the full 12-bit `EXC_TYPE_*` table (§4c); the FW side has exactly 6 entry points + 2
   emitters. *Result:* **holds** — the 9-cause vector, the FULLTYPE sub-causes, and the 6-entry
   FW subsystem are all byte-pinned. `[HIGH · OBSERVED]`

5. **"Recovery = reset, not resume."** *Falsifier:* an in-place abort-clear / resume-from-fault
   register. *Check:* `raise_FATAL @0x13e00` ends in an infinite `j 0x13e14` with no exit edge;
   the abort path has no abort-clear register (recovery = `PWRCTL.CoreReset` + clock un-gate +
   W0C cause-clear). *Result:* **holds** — the firmware never leaves the spin; only an external
   reset revives the engine. `[HIGH · OBSERVED · CARRIED]`

All five survive. The one place a reader must keep honest is the **v5/Maverick interior**: the
Maverick register schemas and ISA headers carry the same primitives (header-OBSERVED), but no
v5 *interior* firmware was carved — every v5-interior reading above is `[INFERRED]`.

> **CORRECTION — the strongest re-grounding on this page: the Cadence `secmon` secure-monitor
> is the path NOT taken.** The toolchain LSP genuinely ships a key-monitor-shaped boot model:
> `secmon` with a secure/nonsecure syscall split (`SECMON_SYSCALL_*`, syscall-ID base
> `0x736d3000` = "sm0"), per-exception delegation gating
> (`secmon_nonsecure_exceptions[XCHAL_EXCCAUSE_NUM]`), and a **locked-MPU verify pass**
> (`secmon_mpu_init_final()` locks the secure entries; `secmon_mpu_verify()` then asserts every
> locked entry is at the table head, overlaps secure memory, and is uncacheable, else a
> non-returning `secmon_illegal_mpu_entry_trap()`). A naive reader who finds these headers in
> the gpsimd-tools tree would conclude GPSIMD boots a locked, verified, secure-monitor — a
> key-rooted model. It does not: the carved `CAYMAN_NX_POOL_DEBUG` compute firmware has **zero**
> `secmon`/`nonsecure`/`xtsm` strings (verified this pass), and its `_ResetHandler` programs the
> MPU **inline** with **no** lock-and-verify pass. The secure-monitor was available and was
> **not linked**. This is the page's load against a key-rooted reading: the model is
> integrity-only *by deliberate omission of the secure monitor the toolchain offered*, which is
> stronger evidence for the keystone than mere absence of crypto strings. `[HIGH · OBSERVED —
> `secmon` headers/sources read from the LSP; absence in the carved compute image re-verified
> by `rg -c` over the extracted DRAM/IRAM `.c.o`.]`

---

## 9. Reimplementation contract

To reproduce the GPSIMD security posture, build an **integrity-enforcing, armed-at-boot,
fail-stop** model — not a secure-boot one:

1. **No active on-die crypto root / secure-monitor in the compute domain.** Enforce a
   kernel/user ring (`PS_RING` `PS[4]`, `PrivilegedException`/`ExternalRegisterPrivilegeException`);
   stamp transactions `AxPROT = 0x2` (privileged, **non-secure**). The toolchain's `secmon`
   locked-MPU secure/nonsecure split is **available but unused** — do not link it on the
   compute path (§1, §8).
2. **Boot the perimeter permissive; arm it in privileged firmware.** Ship `user_remapper`
   fail-OPEN (`0x1`), `nsm` bypassed, `qos_prot` transparent, MPU off; ship `amzn_remapper`
   fail-CLOSED (`0x0`). The privileged `_ResetHandler` programs a 16-entry MPU inline (no
   lock/verify pass) with a deny-by-omission region map (unused holes = `XTHAL_AR_NONE`, DRAM =
   no-execute); the supervisor populates the `user_remapper` CAM, clears the `nsm` bypass, and
   only then `enter_run`-releases the engine (§2, §3).
3. **Fail-stop every boundary.** A fabric integrity fault returns `SLVERR (0x2)` + a
   `0xDEADBEEF`-poisoned read payload; a RAS fault freezes the block (clock-stop +
   SRAM-write-protect + scan-dump); a core exception or a firmware-decoded fault spins the
   engine forever. Provide exactly two non-terminating dispositions: FP-arithmetic
   (log-and-return, still reports) and the speculative-PC-range soft guard (§4, §5).
4. **A 9-cause XEA3 exception vector, three firmware-handled, 12-bit FULLTYPE refinement.**
   Dispatch on `EXCCAUSE & 0xf`; override CAUSE 1/3/4 with a FATAL handler; leave the rest on
   the XTOS default. The structured `EXC_TYPE_*` roster (§4c) carries the MPU access-violation,
   privilege, and stack-limit sub-causes the memory model enforces.
5. **Recovery is reset, not resume.** No abort-clear / resume register — revive a faulted
   engine only via `PWRCTL.CoreReset` + clock un-gate + W0C cause-clear + the §2 boot re-arm
   (§6).
6. **Report on three host channels.** Publish the `notification_t` to TIE `UR#0x15`, the
   per-TPB error-trap notification ring, and the `'S:'` stdout SPSC ring; the compute Q7
   self-reports and self-halts (it is not an apex-critical sink) (§7). Keep the host loader's
   `nrtucode_result_t` (load-time) distinct from the device fault code bytes (run-time).

---

## Cross-References

- [The SoC-Fabric Perimeter](./soc-fabric-perimeter.md) — the sprot ring as the spatial perimeter (§3 detail).
- [Boot-Arming + Device Fault Recovery](./boot-arming-fault-recovery.md) — the boot arm sequence + reset-only recovery (§2/§6 detail).
- [Custom-Op Reachability / Isolation](./reachability-isolation.md) — what a guest kernel can reach behind the ring (§3).
- [Firmware Trust Chain + Threat Model](./trust-chain-threat-model.md) — the integrity-rooted trust chain + adversary model (§1/§8).
- [Profiling / Trace / Debug + Access Gating](./profiling-trace-debug-gating.md) — the OCD/TRAX/PMU debug surface gating (§4 CAUSE 4).
- [Information-Leakage / Side-Channel Surface](./side-channel-leakage.md) — poison patterns, scan dumps, timing (§4/§5).
- [SEC-Lane Synthesis (boot→attest→fault)](./security-synthesis.md) — the end-to-end close of the lane.
- [SEQ Error-Handler / Fault Reporting](../../firmware/seq/error-handler.md) — the run-time fault producer (Layer D).
- [The XEA3 Interrupt / Exception Architecture](../interrupt/xea3-interrupt-architecture.md) — the core exception model (Layer C).
- [The Interrupt / Exception Handler Bodies](../interrupt/handler-bodies.md) — the 9-cause vector bodies (Layer C).
- [Abort / Scan-Dump / Clock-Stop Control](../interrupt/abort-scandump-clockstop.md) — the fabric freeze (Layer B).
- [errtrig / FIS Error Routing](../interrupt/errtrig-fis-routing.md) — the Abort wire-OR + apex climb (Layer B).
- [CSR — amzn_remapper / user_remapper](../csr/remapper.md) · [CSR — qos_prot](../csr/qos-prot.md) · [CSR — nsm](../csr/nsm.md) — the fabric-integrity blocks (Layer A).
- [SEQ Boot / Entry Path](../../firmware/seq/boot.md) — the boot spine that builds the perimeter (§2).
