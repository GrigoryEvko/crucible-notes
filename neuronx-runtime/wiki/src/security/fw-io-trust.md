# FW-IO Trust Boundary

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The analysis reads `neuron_fw_io.c` (895 lines) and `neuron_fw_io.h` (580 lines) directly — every CRC span, register read, and firmware-published value below is transcribed from the shipped `.c`/`.h`, not reverse-engineered. Other driver versions renumber lines.*
> *Frame: **authorized white-hat / defensive**. This page analyzes a trust boundary that lies **inside** the trusted computing base (TCB) — the kernel↔firmware MiscRAM mailbox — so a hardened deployment can reason about firmware-trust assumptions. It documents **no userspace-reachable bug**: every item here is a hardening / defense-in-depth observation about how much the kernel trusts the on-device management CPU, deliberately kept distinct from the userspace-reachable findings on [the attack-surface page](ioctl-attack-surface.md). No exploit code, no inflated severity. Part XV — Security & Attack Surface · DEEP · [back to index](../index.md)*

## Abstract

The FW-IO MiscRAM mailbox ([protocol page](../kernel/fw-io.md)) is the kernel's request/response channel to the device's Q7 management CPU. This page is the **security projection** of that channel: it treats the mailbox as a trust boundary and asks the only question a defender cares about — *what does the kernel accept from firmware without independent validation, and what would a malfunctioning or compromised firmware be able to influence?* The protocol mechanics (the wire format, the two engines, the register map) are owned by the protocol page and are not re-derived here; this page consumes them and analyzes the trust placed across the boundary.

The single most important framing — and the reason this page is INFO-class, never severity-ranked alongside the ioctl bugs — is that **the firmware is inside the TCB**. The management CPU already drives the silicon the driver is trying to protect: it owns power, ECC reporting, reset orchestration, and pod election. A "compromised firmware" adversary that could forge a mailbox response could equally well drive the hardware directly. So the FW-IO trust observations are *not* a privilege-boundary crossing the way a userspace ioctl is (contrast [Boundary A on the overview page](overview.md#4-trust-boundaries)); they are robustness notes about a boundary *within* the TCB. Two facts must therefore be held simultaneously and never conflated: (a) the mailbox is genuinely a trust boundary — the kernel consumes firmware-published values without re-deriving them; and (b) crossing it requires already being firmware, so nothing here is reachable by an unprivileged process holding `/dev/neuronN`.

Within that frame, three observations matter. First, the legacy request carries a **CRC32C** (`crc32c`, `neuron_fw_io.c:308`), and analyzing what that CRC actually buys is the crux: a CRC is an **integrity** control (it detects accidental corruption of bits in transit through MiscRAM) — it is emphatically **not** an **authenticity** control (it does not, and cannot, distinguish a firmware that forges a well-formed message from the real one). Second, a broad set of firmware-published values — power utilization, API version, ECC counters, HBM repair state, serial/server/reservation IDs — is read by the public helpers and consumed by the kernel's telemetry, power, reset, and pod-election paths with **no independent validation** of the value itself. Third, the security sweep flagged that the new-engine response carries a `crc32` field the firmware computes but the driver **never reads** ([finding S10](ioctl-attack-surface.md#12-s10--s11--s12--firmware-tcb-and-non-exploitable-observations), which this page owns the trust analysis of). §1 is the trust-boundary table; §2 dissects the CRC32C as integrity-not-authenticity; §3 enumerates the unvalidated firmware-published inputs and the consequence-if-compromised; §4 states the TCB-internal framing explicitly and contrasts it with the userspace attack surface.

For the trust analysis, the contract is:

- **Distinguish the boundary from its reachability.** The MiscRAM mailbox *is* a trust boundary (firmware-sourced data the kernel consumes), but it is reachable only from the firmware side, which is inside the TCB. Both facts are true; do not collapse one into the other.
- **Distinguish integrity from authenticity.** The CRC32C detects transmission corruption (integrity). It does not authenticate the sender (authenticity). A firmware adversary defeats it trivially by computing the correct CRC over a forged message; only random corruption is caught.
- **Name what the kernel trusts unvalidated.** Every firmware-published value the kernel reads and acts on without re-deriving is a trust assumption; §3 lists them with the consumer that acts on each.
- **Keep TCB-internal items out of the userspace severity ranking.** These are hardening / defense-in-depth notes (raise the bar against silent MiscRAM corruption and silent firmware faults), not CVE-class userspace bugs.

| | |
|---|---|
| **Boundary** | kernel ↔ firmware, via the FW-IO MiscRAM mailbox (`neuron_fw_io.c`/`.h`) |
| **Trust class** | **TCB-internal** — firmware already owns the device; not userspace-reachable |
| **Request integrity** | CRC32C over header+payload (`crc32c` `:308`, applied legacy `:351` / new `:432`) |
| **Integrity vs authenticity** | CRC32C = corruption-detection (integrity) **only**; **not** sender authentication |
| **Request CRC coverage** | header(8, `crc32` zeroed) + payload — both engines (`:350-351`, `:431-432`) |
| **Response CRC (legacy)** | **none** — legacy response header is `{seq, error_code, size}`, no `crc32` (`:29-36`) |
| **Response CRC (new)** | firmware computes it (`:45`, comment `:23-25`); driver **never reads** `dw1` (`:471`) |
| **Unvalidated inputs** | power-util, API version, ECC counts, HBM repair state, serial/server/reservation IDs |
| **Not-ready sentinel** | `0xdeadbeef` — a value-domain signal, not an integrity check (`:583`, `:806`) |
| **Severity class** | INFO (firmware-TCB); hardening / robustness, **not** a userspace privilege escalation |

> **NOTE — what this page is not.** It is not a claim that the Neuron firmware is malicious, nor a path by which an unprivileged user could become firmware. It is the standard defender's exercise of mapping a trust boundary: writing down exactly what is trusted across it so that (a) a reimplementer reproduces the trust model faithfully, and (b) a hardener knows which firmware-published values to treat as untrusted if the threat model ever expands to include a compromised management CPU (e.g. supply-chain or a firmware bug). Everything here is `INFO` precisely because, under the *shipped* threat model, the firmware is trusted.

---

## 1. The Trust-Boundary Table

### Purpose

This is the inventory the rest of the page rests on: for each input the kernel takes from firmware across the mailbox, what *source* produced it, what *validation* the driver applies, and what *trust class* it falls into. The validation column is the crux — most rows read `none`, meaning the kernel consumes the firmware-published value as ground truth. The CRC32C row is the one integrity control, and §2 dissects exactly what it does and does not cover.

### Inputs Crossing the Boundary

Every row is an input the kernel reads from firmware-owned MiscRAM and acts on. "Validation" is what the driver does to the *value* before trusting it — a CRC on the framing, a bounds/decode mask, or nothing. The trust class is **TCB-internal** for every row, because the source is always the firmware, which is inside the TCB; the column is kept to make that uniformity explicit and to contrast with the userspace-reachable rows on the [attack-surface table](ioctl-attack-surface.md#2-the-findings-table).

| Input | Source | Validation | Trust class | Confidence |
|---|---|---|---|---|
| Legacy **request** frame (on the wire) | kernel-built, firmware-read | CRC32C over header+payload (`:351`) — *integrity, sent to FW* | TCB-internal | HIGH |
| New **request** frame (on the wire) | kernel-built, firmware-read | CRC32C over header+payload (`:432`) — *integrity, sent to FW* | TCB-internal | HIGH |
| Legacy **response** frame | firmware | seq match (`:364`) + `error_code` (`:375`); **no CRC** in the header (`:29-36`) | TCB-internal | HIGH |
| New **response** frame | firmware | seq match (`:473`) + `error_code` (`:479`); `crc32` present but **never read** (`:471`) | TCB-internal | HIGH |
| Response **payload bytes** | firmware | length clamped `min(resp_size, data_size)` (`:482`) — *bounds only, not content* | TCB-internal | HIGH |
| `POWER_UTIL_D0/D1` (power util) | firmware | none on value; only `die < dice_per_device` bound (`:107`) | TCB-internal | HIGH |
| `API_VERSION` | firmware | none on value; gates new protocol/ECC/power decode (`:128`) | TCB-internal | HIGH |
| `SRAM_ECC` / `HBM0..3_ECC` (ECC counts) | firmware | none; bit-decode + `0xdeadbeef` skip (`:806`), then summed | TCB-internal | HIGH |
| `HBM_REPAIR_STATE` | firmware | none on value; `& 0x3` decode mask only (`:57`) | TCB-internal | HIGH |
| `SERIAL_NUMBER_LO/HI` | firmware | none; composed `(hi<<32)\|lo` (`:78`) | TCB-internal | HIGH |
| `SERVER_RACK_ID` | firmware | valid-bit gate per field, else `-1` (`:136`) — *self-describing, not authenticated* | TCB-internal | HIGH |
| `RESERVATION_ID_LO/HI` | firmware | none; composed 64-bit (`:157`) | TCB-internal | HIGH |
| `INSTANCE_PARTITION_SZ` | firmware | part gated by valid bit[31] (`:181`) — *self-describing* | TCB-internal | HIGH |
| `RESET` reg readback (`==0`) | firmware | the reset-accepted handshake; truth = FW cleared it (`:643-645`) | TCB-internal | HIGH |
| `0xdeadbeef` not-ready sentinel | firmware | a value-domain "not populated yet" signal (`:583`) — *not an integrity check* | TCB-internal | HIGH |

> **GOTCHA — the CRC32C protects the *request*, not the *response*, and even on the request it is integrity, not authenticity.** Two distinct gaps live in this table and a reimplementer must not blur them. (1) **Direction**: both engines CRC the request the kernel *sends* (`:351`, `:432`); the legacy response has no CRC field at all (`:29-36`), and the new response's `crc32` is computed by firmware but never read by the driver (`:471` reads only `dw0`). So the channel's only integrity check runs FW-inbound, not FW-outbound. (2) **Property**: even the request CRC is a corruption detector, not a sender authenticator (§2). The reliability the framing actually delivers is "the firmware can detect a request mangled in transit"; it delivers *nothing* about "is this response really from the firmware," because in this trust model there is no other possible sender.

> **NOTE — the "valid-bit" rows are self-description, not authentication.** `SERVER_RACK_ID` and `INSTANCE_PARTITION_SZ` carry firmware-set validity bits (`:136`, `:181`) that the decode honours: an invalid field reads back `-1` / is suppressed. This is the firmware *describing whether it populated a field*, which the kernel trusts — it is not the kernel *verifying* the field. A firmware that sets the valid bit and a bogus value is believed. The validity bit raises confidence against a half-initialized register, not against a wrong one.

### Considerations

The uniform `TCB-internal` column is the whole point: there is **no** row in this table whose source is userspace. Userspace cannot write MiscRAM, cannot forge a response, and cannot reach `fw_io_execute_request[_new]` except indirectly by issuing an ioctl that *causes the kernel* to run a legitimate mailbox transaction (e.g. a metrics post or a power-profile set). Even then, the response that comes back is firmware-authored. The contrast with the [ioctl attack surface](ioctl-attack-surface.md), where the *source* of the dangerous input is an attacker-controlled `copy_from_user`, is the line that separates this page's `INFO` items from that page's `MED`/`LOW` findings.

---

## 2. The CRC32C — Integrity, Not Authenticity

### Purpose

The mailbox's one cryptographic-looking control is the CRC32C on the request. Because "CRC32C" reads as a checksum and checksums are easily mistaken for a security control, this section states precisely what it defends against and what it does not. The conclusion is unambiguous and must survive into any reliability or trust write-up: **CRC32C is an integrity (corruption-detection) control; it is not, and a CRC structurally cannot be, an authenticity (sender-authentication) control.**

### Algorithm — what is actually computed

The CRC is the standard reflected Castagnoli polynomial, seeded `0xffffffff` and finalized `^ 0xffffffff`, over the 8-byte header (with the `crc32` field pre-zeroed) followed by the payload. The protocol page owns the [byte-level walk](../kernel/fw-io.md#algorithm--crc32c); reproduced here only to anchor the trust argument to the exact span.

```c
// crc32c — neuron_fw_io.c:308 (the request-integrity primitive)
function crc32c(hdr, data, len) -> u32:
    csum = 0xffffffff                       // :310  standard CRC32C init
    if hdr != NULL:
        crc32c_add(hdr, 8, &csum)           // :312  the 8-byte header, crc32 field zeroed (:350/:431)
    crc32c_add(data, len, &csum)            // :313  the payload
    return csum ^ 0xffffffff                // :314  standard CRC32C final-XOR

// applied to the REQUEST the kernel sends:
//   legacy: ctx->request->...crc32 = crc32c(&hdr, data, size - 8)     // :350-351
//   new:    req_header.hdr.crc32  = crc32c(&req_header, req, req_size) // :431-432
```

### Why this is integrity, not authenticity

A CRC is a **fixed, public, keyless** function of the message bytes. Three properties follow directly, and each is the difference between integrity and authenticity:

- **No secret is involved.** Authenticity requires something the sender knows and an impostor does not — a key, a signature, a shared secret. `crc32c` (`:308`) takes only the message bytes and public constants (`0xffffffff`, the public Castagnoli table at `:237-275`). Any party that can write a message can compute its correct CRC. There is nothing for an impostor to *not* know.
- **It detects accidental change, by design.** A CRC's guarantee is "if a small number of bits flip in transit, the recomputed CRC will (with high probability) differ from the carried one." That is exactly corruption detection — bits mangled by a flaky MiscRAM read, a bus glitch, or a partial write. This is real and useful **integrity**: the firmware can reject a request that arrived garbled.
- **It is trivially forgeable by an active adversary.** Any party substituting a *message* recomputes the CRC over its substituted bytes and the check passes. The CRC adds zero cost to a deliberate forger. Authenticity is precisely the property a CRC lacks: it cannot distinguish "the genuine sender produced these bytes" from "someone who can write bytes produced these bytes and recomputed the trivial checksum."

> **NOTE — the correct reading of the request CRC in this driver.** Within the shipped TCB model the request CRC is doing its real and only job: it lets the **firmware** detect a request the **kernel** sent that got corrupted crossing the MiscRAM aperture. Both endpoints are trusted; the threat the CRC addresses is *the medium*, not *the sender*. That is a perfectly sound use of a CRC — it is an integrity control protecting against transmission corruption, deployed exactly where integrity (not authenticity) is what's needed. The error to avoid is the upgrade in the reader's mind from "the message is intact" to "the message is from who I think" — the CRC says only the former.

> **GOTCHA — `0xdeadbeef` is a value sentinel, not an integrity check.** The direct-read path retries while a register reads `0xdeadbeef` (`:583`) and the ECC aggregator skips channels reading `0xdeadbeef` (`:806`). This is sometimes misread as a validity check on firmware data. It is not an integrity mechanism: it is a firmware-chosen in-band magic value meaning "I have not populated this register yet." A firmware that published `0xdeadbeef` as a genuine ECC count would be silently dropped, and a firmware that published a *wrong* count that is not `0xdeadbeef` is fully trusted. The sentinel disambiguates not-ready from ready; it says nothing about whether a ready value is correct.

### Considerations

The asymmetry of the channel makes the integrity-vs-authenticity distinction concrete. **Request** direction: CRC-protected (integrity), so the firmware can catch a corrupted kernel request. **Response** direction: legacy has no CRC at all (`:29-36`), and the new path leaves the firmware-computed `crc32` unread (`:471`, §3). So even the *integrity* half is one-directional — the kernel does not currently detect a corrupted *response* (recommended fix: [H11 on the hardening page](hardening.md#h11--s10-validate-the-fw-io-new-path-response-crc) recomputes it). Neither direction has, or could have via a CRC, an authenticity control. If the threat model ever needed sender authentication across this boundary (it does not today, because the firmware is in the TCB), a CRC would be the wrong primitive — a keyed MAC or signature would be required, which is out of scope for a mailbox between two endpoints that already fully trust each other.

---

## 3. Firmware-Published Values the Kernel Trusts Unvalidated

### Purpose

The second trust assumption is broader and quieter than the CRC question: a large set of firmware-published register values is read by the public helpers and consumed by the kernel's telemetry, power, reset, and pod-election subsystems **without any independent re-derivation or cross-check of the value**. Each is a place where the kernel takes the management CPU's word for a fact about the device. This section enumerates them with the consumer that acts on each and the consequence if the firmware were to publish a wrong value.

### The unvalidated inputs and their consumers

Every helper here builds a MiscRAM address and reads a firmware-owned register through the DHAL direct path; none re-derives the value from an independent source. The "consequence if firmware compromised" column is the defender's exercise — it is *not* a userspace-reachable outcome (§4), only what a wrong firmware value would propagate into.

| Value | Helper (`:line`) | Consumer | Validation on the value | Consequence if firmware publishes a wrong value |
|---|---|---|---|---|
| Power utilization | `fw_io_device_power_read` `:103` | [metrics](../kernel/metrics.md) / `NC_UTILIZATION` | none (only `die` bound `:107`) | wrong telemetry → bad CloudWatch utilization, bad throttling input |
| API version | `fw_io_api_version_read` `:123` | gates new-protocol/ECC/power decode | none | wrong gate: e.g. a too-high version steers the kernel down the new engine or `api≥6` ECC decode |
| SRAM/HBM ECC counts | `fw_io_ecc_read` `:36`, `…_total_ecc…` `:790` | [metrics](../kernel/metrics.md) / ECC error reporting | none (decode + `0xdeadbeef` skip) | under/over-reported ECC errors → masked or spurious health alarms |
| HBM repair state | `fw_io_hbm_uecc_repair_state_read` `:57` | repair-state / health reporting | none (`& 0x3` mask) | wrong repair status reported to operators |
| Serial number | `fw_io_serial_number_read` `:78` | identity, [pod-election](../kernel/pod-election.md) | none | mis-identified device in pod neighbor matching |
| Server/rack ID | `fw_io_server_info_read` `:136` | topology / placement | valid-bit gate only | wrong topology placement (firmware self-describes the field) |
| Reservation ID | `fw_io_reservation_id_read` `:157` | reservation reporting | none | wrong reservation identity surfaced |
| Instance/partition size | `fw_io_instance_partition_sz_read` `:181` | partitioning | part valid-bit gate | wrong partition geometry consumed |
| Reset accepted (`RESET==0`) | `fw_io_is_reset_initiated` `:637` | [reset](../kernel/reset.md) handshake | the readback *is* the signal | firmware that never clears (or prematurely clears) `RESET` desyncs the reset state machine |
| New-path response `crc32` | (unread) `dw1` at `:471` | — | **never read** | a corrupted/forged response is accepted on `(seq, error_code)` alone — [S10](ioctl-attack-surface.md#12-s10--s11--s12--firmware-tcb-and-non-exploitable-observations) |

> **NOTE — the response-CRC gap (S10) is the sharpest item in this section, and it is still TCB-internal.** The firmware was updated to compute a response `crc32` (header comment `:23-25`; the field exists at `neuron_fw_io.h:45`), but `fw_io_execute_request_new` reads back only `resp_header.reg.dw0` (`:471`) — sequence number, error code, size — and never reads `reg.dw1`, the response CRC at `0x1f4`. So the response is accepted on a sequence-number match (`:473`) plus a success `error_code` (`:479`) alone. The driver computes a CRC it asks the firmware to verify (request), and ignores the CRC the firmware computes for it to verify (response). The payload copy is length-bounded (`min(resp_size, data_size)`, `:482`), so this is an **integrity** gap (an undetected corrupt/forged response), not a memory-safety bug — and because the only entity that can author a response is the firmware (TCB), it is not a userspace-reachable corruption. The hardening fix recomputes and checks it ([H11](hardening.md#h11--s10-validate-the-fw-io-new-path-response-crc)).

### Why the kernel trusts these — and what hardening would add

There is a sound reason the kernel consumes these unvalidated: the firmware *is* the authority for them. There is no independent on-host source for the device's serial number, its ECC counters, or its real-time power draw — the management CPU is the only sensor. The kernel cannot "re-derive" power utilization; it can only ask. So the trust is not negligence, it is the architecture: the management CPU is the device's source of truth, and it is in the TCB.

The hardening value is narrow and honest: **detect silent corruption, not malice.** Validating the response CRC (H11) and making the legacy length-reject explicit (H12) raise the bar against a *flaky* MiscRAM read or a *desynced* firmware response — accidental faults that a CRC genuinely catches — without pretending to defend against a firmware that is actively lying, which no in-driver check can do while the firmware drives the hardware. That is the correct scope for a TCB-internal robustness fix.

---

## 4. Why FW-IO Findings Are TCB-Internal, Not Userspace Attack Surface

### Purpose

The distinction this section draws is the one that keeps a defensive map honest: an item that requires *being the firmware* to exploit is categorically different from an item a process holding `/dev/neuronN` can reach. The former is a robustness/hardening note inside the TCB; the latter is an attack-surface finding. This page's items are all the former. Stating *why* — and contrasting with the userspace boundary — is what prevents the firmware-trust observations from being mis-promoted into CVE-class claims.

### The two boundaries, side by side

The [overview page](overview.md#4-trust-boundaries) draws both boundaries; here is the security-relevant contrast that determines the class of every FW-IO item.

```text
   Boundary A — userspace → kernel            Boundary B — kernel ↔ firmware (THIS PAGE)
   ─────────────────────────────────          ──────────────────────────────────────────
   crossing:  ioctl(2) / mmap(2)              crossing:  request/response over MiscRAM
   source of input:  copy_from_user           source of input:  firmware-owned MiscRAM
                     (ATTACKER-CONTROLLED)                       (FIRMWARE-AUTHORED)
   gate:  Gate 0/1/2 (file-perm + identity)   gate:  none needed — to cross, you ARE firmware
   reachable by:  any process with the fd     reachable by:  only the on-device management CPU
   class:  ATTACK SURFACE (S1–S9, S12)        class:  TCB-INTERNAL (S10, S11) — hardening note
```

### The argument, stated plainly

Three facts make every FW-IO item TCB-internal rather than userspace-reachable:

- **The firmware authors the input.** A userspace attacker controls the bytes that cross Boundary A (the ioctl struct, marshalled by `neuron_copy_from_user`). It does **not** control the bytes that cross Boundary B — those are written by the management CPU into MiscRAM. The only way to supply a malicious mailbox response is to *be* the firmware that writes the response register.
- **The firmware already owns what the trust would protect.** The whole point of a privilege boundary is to keep a less-trusted side from reaching a more-trusted resource. But the firmware is the *most*-trusted side here: it drives power, reset, ECC, and the cores. A firmware that could forge a response could just as well act on the hardware directly — validating its responses harder buys nothing against a firmware that has decided to misbehave, because it controls the device the validation is trying to protect.
- **Userspace can only *trigger* a legitimate transaction, never forge one.** An ioctl can cause the kernel to *run* a mailbox transaction (post a metric, set a power profile), but the response that returns is firmware-authored and the request is kernel-built. There is no ioctl that lets userspace inject a chosen mailbox response. So the userspace reach stops at "make the kernel talk to firmware," which is the intended API, not at "control what firmware says back."

> **NOTE — the honest bottom line.** The FW-IO trust items (the unvalidated response CRC, the firmware-published values) are real properties of the driver and worth fixing for robustness — they harden against silent corruption and silent firmware faults. They are **not** userspace-reachable privilege escalations and must not be ranked alongside the ioctl findings. This is why they sit at `INFO` on the [findings table](ioctl-attack-surface.md#2-the-findings-table) and at `P3` on the [hardening roadmap](hardening.md#5-p3--firmware-trust-and-non-exploitable-hygiene): firmware-trust robustness, not attack-surface reduction. The defensive value of writing them down is to document the *firmware-trust assumption* explicitly, so a deployment whose threat model includes a compromised management CPU (supply-chain, a firmware bug) knows precisely which published values it is currently trusting blind.

### Considerations

The cleanest way to state the residual exposure for a hardened deployment: under the shipped threat model, **trust the firmware**, and these items are robustness fixes (H11/H12) that cost almost nothing and detect accidental corruption. If a future threat model expands to **distrust the firmware** — at which point the entire device, not just this mailbox, is outside the TCB — then §3's table is the list of values to treat as untrusted, and the right response is not in-driver CRC checks (a CRC cannot authenticate a sender) but out-of-band attestation of the firmware image itself, which is a platform concern beyond this driver. The page's job is to make the current trust assumption explicit and to keep its items in the robustness lane where they belong.

---

## Related Components

| Name | Relationship |
|---|---|
| `fw_io_execute_request` / `_new` (`neuron_fw_io.c:317` / `:406`) | the two engines whose request CRC and response trust this page analyzes |
| `crc32c` (`neuron_fw_io.c:308`) | the integrity primitive §2 dissects as integrity-not-authenticity |
| `fw_io_*_read` helpers (`:36`–`:201`) | the public readers of the firmware-published values §3 enumerates |
| `npid_is_attached` (`neuron_pid.c:60`) | the userspace identity gate — Boundary A's gate, contrasted in §4 |

## Cross-References

- [FW-IO MiscRAM Mailbox Protocol](../kernel/fw-io.md) — the wire format, both engines, the register map, and the §4-GOTCHA that the response CRC (`dw1`) is never read; this page is its security projection
- [Security Posture and the Privilege-Gate Model](overview.md) — the overview that draws both trust boundaries; §4 here is the deep treatment of its Boundary B
- [The IOCTL Attack Surface (14 Findings)](ioctl-attack-surface.md) — the userspace-reachable findings (Boundary A) this page is deliberately kept distinct from; owns S10/S11 in its roster
- [Hardening Recommendations](hardening.md) — H11 (validate the response CRC) and H12 (explicit legacy length reject), the P3 robustness fixes for the items here
- [Metrics Aggregation](../kernel/metrics.md) — a primary consumer of the firmware-published values (power, ECC) the kernel trusts unvalidated (§3)
- [back to index](../index.md)
